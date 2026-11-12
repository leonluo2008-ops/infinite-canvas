#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AT-14 B3 B 探针：本地引用单点 + 装配时序 + 三阶段图床计数 + 故障矩阵（**纯离线**：不花钱）。

跑法：wanrepro/ 下 `python -m automation.probe_at14_b3`。
真实任务 auto-0002/0006/0012 只读：跑前跑后对 任务.json 做 sha256 对账，不一致即 FAIL。

判据（合同 §五.3）：
  [1] 本地引用落盘哈希：store 同字节两次 → 同引用（内容寻址幂等）；盘上 sha1 == 原字节 sha1；
      ref_state=local；删文件后 ref_state=expired；_assemble_images 抛 GenError 含「本地资产缺失」。
  [2] 三阶段图床计数 0：①带 X-Task-Id 的上传（隔离实例走 HTTP，读 /api/hub/where 的
      asset_counters）；②replace.run_replace(confirm=False) 上传回填（bridge.upload_asset 打桩
      返回 {local: 引用}）；③hub_api.do_approve——三阶段图床计数必须 0。
  [3] 装配时序：_assemble_images 与 _dispatch_write 换记录器 → assemble < prepared < dispatched < post。
  [4] 故障矩阵 10 条（state / 创建次数 / 装配次数 / dispatch 阶段逐条对表）。
  [5] 旧 URL 三态：stub HTTP 404/500/200 → ref_state = expired/unknown/ok；
      url_to_data_url 对 404 报「图已过期，请重传」、对断连报「图片不可用，请重试」。
  [6] wan 路径 dataUrl 直组：local 引用提交体 dataUrl 解码 == 本地字节；全程 urlopen 次数 0。
  [7] 收尾：真实任务 sha256 未变、夹具零残留、真实任务集合 ⊆ 跑前。

守卫（硬要求）：夹具只用 auto-9501..auto-9520；清理只走 probe_guard.cleanup_created
（禁裸 rmtree）；跑后 assert_no_loss。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "ui") not in sys.path:
    sys.path.insert(0, str(ROOT / "ui"))

from automation import bridge, config, generate, local_assets, probe_guard, replace, state  # noqa: E402
import hub_api  # noqa: E402
import server  # noqa: E402

PROJECTS = Path(config.PROJECTS_DIR)
PROMPT_TEXT = ("镜头一：产品特写，冷光，水珠沿瓶身滑落，慢推。\n" * 12)
REAL_TASKS = ("auto-0002", "auto-0006", "auto-0012")
PROBE_IDS = tuple(f"auto-95{n:02d}" for n in range(1, 21))     # auto-9501..auto-9520
PNG_A = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c626001000000ffff03000006000557bfabd4"
    "0000000049454e44ae426082")
FAKE_KEY = "sk-" + "probe-" + "AT14B3" + "-77c1d2e0"
SDGF_GEN = {"baseUrl": "https://api.juxin.example", "apiFormat": "sdgf",
            "渠道名": "聚鑫-SDGF", "model": "SD2.5-满血-GF-720P", "mode": "frames"}
WAN_GEN = {"baseUrl": "http://127.0.0.1:8776", "apiFormat": "wan",
           "渠道名": "本机万相", "model": "", "mode": ""}
SDGF_PARAMS = {"duration": 5, "ratio": "9:16", "resolution": "720", "audio": True}
WAN_PARAMS = {"duration": 5, "ratio": "16:9", "resolution": "1080", "audio": True}
CHECKLIST_MD = (
    "| # | 类别 | 模型看到什么 | 命名 | 资产图 | 次数 | 建议 |\n"
    "|---|------|------------|------|--------|------|------|\n"
    "| 1 | 主体 | 产品特写冷光瓶身 |  |  | 1 | 必须换 |\n")

_pass = {"n": 0}
_fail = {"n": 0}


def check(name: str, cond: bool, detail: str = "") -> bool:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))
    _pass["n"] += 1 if cond else 0
    _fail["n"] += 0 if cond else 1
    return cond


# ---------------------------------------------------------------- 守卫与夹具

def real_task_digests() -> dict:
    out = {}
    for tid in REAL_TASKS:
        f = PROJECTS / tid / "任务.json"
        out[tid] = hashlib.sha256(f.read_bytes()).hexdigest() if f.is_file() else ""
    return out


CREATED: set[str] = set()
BEFORE_SET: frozenset[str] = frozenset()


def guard_preflight() -> None:
    """跑前守卫：夹具号被占即拒跑；真实任务集合快照。"""
    global BEFORE_SET
    BEFORE_SET = probe_guard.projects_snapshot(PROJECTS)
    occupied = [tid for tid in PROBE_IDS if tid in BEFORE_SET]
    if occupied:
        raise probe_guard.GuardError(f"夹具号已被占（跑前已存在）：{occupied}——拒跑（只删自建）")


def cleanup_created() -> None:
    for tid in PROBE_IDS:
        ok, why = probe_guard.cleanup_created(PROJECTS, tid, before=BEFORE_SET, created=CREATED)
        if ok:
            print(f"    [cleanup] {why}")


def make_task(local_id: str, *, state_name: str = "approved", api_format: str = "sdgf",
              assets: list | None = None, **extra) -> Path:
    """造夹具任务（v2 快照自洽——submit 能过快照比对）。"""
    tdir = PROJECTS / local_id
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / "提示词.md").write_text(PROMPT_TEXT, encoding="utf-8")
    params = dict(WAN_PARAMS if api_format == "wan" else SDGF_PARAMS)
    ch = dict(WAN_GEN if api_format == "wan" else SDGF_GEN)
    snap = state.snapshot_hash_v2(PROMPT_TEXT, params, ch)
    task = {"local_id": local_id, "bridge_id": f"probe-at14b3-{local_id}",
            "created_at": "2026-01-01T00:00:00+08:00", "state": state_name,
            "history": [{"at": "2026-01-01T00:00:00+08:00", "from": None, "to": state_name,
                         "note": "probe-at14-b3 夹具"}],
            "intent": "AT-14 B3 探针夹具",
            "gen_params": params, "gen_channel": ch,
            "approval_snapshot": snap, "snapshot_version": 2,
            "approval": {"snapshot_sha256": snap, "at": "2026-01-01T00:00:00+08:00",
                         "by": "probe"},
            "assets_confirmed": assets if assets is not None else [],
            "empty_confirm": True}
    task.update(extra)
    state.save(tdir, task)
    CREATED.add(local_id)
    return tdir


def local_ref_fixture(local_id: str, blob: bytes = PNG_A) -> str:
    """给夹具任务落一张本地图 → 引用串（同时登记夹具目录）。"""
    ref = local_assets.store(local_id, blob, "ref.png")
    CREATED.add(local_id)
    return ref


class BridgeStub:
    """bridge 打桩：记 post/get/bytes 调用，按 impl 回或抛。"""

    def __init__(self):
        self.posts: list[tuple[str, dict]] = []
        self.gets: list[str] = []
        self.bytes_calls: list[str] = []
        self.post_impl = None
        self.get_map: dict[str, object] = {}
        self.bytes_map: dict[str, object] = {}

    def install(self) -> "BridgeStub":
        bridge.post_json = self.post_json
        bridge.get = self.get
        bridge.get_bytes = self.get_bytes
        return self

    def post_json(self, path: str, payload: dict) -> dict:
        self.posts.append((path, payload))
        if isinstance(self.post_impl, BaseException):
            raise self.post_impl
        if callable(self.post_impl):
            return self.post_impl(path, payload)
        return {"job": "job-1", "taskId": "cloud-1"}

    def get(self, path: str) -> dict:
        self.gets.append(path)
        val = self.get_map.get(path)
        if isinstance(val, BaseException):
            raise val
        if val is None:
            err = bridge.BridgeError(f"bridge GET {path} → HTTP 404: 没有这个任务号")
            err.status = 404
            raise err
        return val

    def get_bytes(self, path: str) -> bytes:
        self.bytes_calls.append(path)
        val = self.bytes_map.get(path)
        if isinstance(val, BaseException):
            raise val
        if val is None:
            err = bridge.BridgeError(f"bridge GET {path} → HTTP 404: 还没有成片")
            err.status = 404
            raise err
        return val


_BRIDGE_ORIG = (bridge.post_json, bridge.get, bridge.get_bytes)


def restore_bridge() -> None:
    bridge.post_json, bridge.get, bridge.get_bytes = _BRIDGE_ORIG


def expect_gen_error(tdir: Path, *, contains: str = "", label: str = "", **kw) -> str:
    kw.setdefault("api_key", FAKE_KEY)
    try:
        generate.do_submit(tdir, verbose=False, **kw)
    except generate.GenError as e:
        check(label, contains in str(e) if contains else True, str(e)[:110])
        return str(e)
    except BaseException as e:  # noqa: BLE001
        check(label, False, f"抛了别的异常 {type(e).__name__}: {str(e)[:80]}")
        return ""
    check(label, False, "没有抛错（可能真发起了提交——探针禁止）")
    return ""


def dispatch_phase_of(tdir: Path) -> str:
    return state.dispatch_phase(state.load(tdir))


def analysis_fixture(bridge_id: str, md: str = CHECKLIST_MD) -> Path:
    """拆解清单夹具：analysis/reverse/<bridge_id>/待确认清单.md（跑后有界清理）。"""
    d = ROOT / "analysis" / "reverse" / bridge_id
    d.mkdir(parents=True, exist_ok=True)
    (d / replace.LIST_NAME).write_text(md, encoding="utf-8")
    return d


def cleanup_analysis_fixtures() -> None:
    """有界清理本探针的 analysis 夹具（只删 probe-at14b3- 前缀、父目录必须直系）。"""
    base = (ROOT / "analysis" / "reverse").resolve()
    if not base.is_dir():
        return
    for d in base.glob("probe-at14b3-*"):
        if d.is_dir() and not d.is_symlink() and d.parent == base:
            shutil.rmtree(d)
            print(f"    [cleanup] 已删 analysis 夹具 {d.name}")


# ---------------------------------------------------------------- [1] 本地引用落盘哈希

def probe_local_ref_hash() -> None:
    print("1) 本地引用落盘哈希：内容寻址幂等 + ref_state 三态 + 装配缺失人话")
    tid = "auto-9501"
    ref1 = local_ref_fixture(tid)
    ref2 = local_assets.store(tid, PNG_A, "ref.png")
    check("store 同字节两次 → 同名同引用（内容寻址幂等）", ref1 == ref2, ref1)
    _lid, name = local_assets.parse_ref(ref1)
    disk = PROJECTS / tid / "asset-uploads" / name
    check("盘上文件 sha1 == 原字节 sha1", disk.is_file()
          and hashlib.sha1(disk.read_bytes()).hexdigest() == hashlib.sha1(PNG_A).hexdigest(),
          f"{disk.name} {disk.stat().st_size}B")
    check("generate.ref_state(引用) == 'local'", generate.ref_state(ref1) == "local",
          generate.ref_state(ref1))
    disk.unlink()
    check("删文件后 ref_state == 'expired'", generate.ref_state(ref1) == "expired",
          generate.ref_state(ref1))
    try:
        generate._assemble_images([ref1], "sdgf")
        check("_assemble_images 缺失文件 → GenError「本地资产缺失」", False, "没有抛错")
    except generate.GenError as e:
        check("_assemble_images 缺失文件 → GenError「本地资产缺失」", "本地资产缺失" in str(e),
              str(e)[:90])
    # 恢复文件（后续不依赖，但保持夹具自洽）
    local_assets.store(tid, PNG_A, "ref.png")


# ---------------------------------------------------------------- [2] 三阶段图床计数 0

def _counting_host_stubs():
    """把 server 的两条图床出路换成计数器（调一次 +1）。返回 (calls, restore)。"""
    calls = {"store": 0, "uguu": 0}
    real_store, real_uguu = server.store_public_image, server.upload_uguu_bytes

    def fake_store(data, name):
        calls["store"] += 1
        return real_store(data, name)

    def fake_uguu(data, name):
        calls["uguu"] += 1
        return real_uguu(data, name)

    server.store_public_image = fake_store
    server.upload_uguu_bytes = fake_uguu

    def restore():
        server.store_public_image = real_store
        server.upload_uguu_bytes = real_uguu

    return calls, restore


def _isolated_upload_phase() -> bool:
    """阶段①：隔离实例走 HTTP——带 X-Task-Id 上传只落本地引用，图床计数全 0。"""
    print("  阶段① 隔离实例 HTTP 上传（带 X-Task-Id → 只落本地引用）")
    ok = True
    tmp = Path(tempfile.mkdtemp(prefix="at14b3-iso-"))
    iso_projects = tmp / "projects"
    (iso_projects / "auto-9502").mkdir(parents=True)           # 服务端只验目录存在
    stub = probe_guard.StubBridge()
    port = probe_guard.free_port()
    env = dict(os.environ, WANREPRO_AUTO_DIR=str(iso_projects),
               WANREPRO_BRIDGE=stub.base)
    proc = None
    try:
        with stub:
            proc = subprocess.Popen(
                [sys.executable, str(ROOT / "ui" / "server.py"), "--port", str(port),
                 "--no-browser"],
                cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
            base = f"http://127.0.0.1:{port}"
            where = None
            for _ in range(200):
                code, payload = probe_guard.http_req(base, "/api/hub/where", timeout=5)
                if code == 200 and isinstance(payload, dict):
                    probe_guard.assert_same_root(payload, iso_projects)
                    where = payload
                    break
                time.sleep(0.3)
            ok &= check("隔离实例起来且读写同源（/api/hub/where == 探针根）", where is not None, "")
            body, ctype = probe_guard.multipart_body({}, {"file": ("ref.png", PNG_A)})
            req = urllib.request.Request(
                base + "/api/asset/upload", data=body, method="POST",
                headers={"Content-Type": ctype,
                         "X-Filename": urllib.parse.quote("ref.png", safe=""),
                         "X-Task-Id": urllib.parse.quote("auto-9502", safe="")})
            op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            resp = json.loads(op.open(req, timeout=30).read())
            ref = str(resp.get("local") or "")
            ok &= check("响应 source=local、local 是合法引用、url 是显示 URL",
                        resp.get("source") == "local" and local_assets.is_ref(ref)
                        and str(resp.get("url") or "").startswith("/api/hub/asset-file/"),
                        json.dumps(resp, ensure_ascii=False)[:140])
            _lid, name = local_assets.parse_ref(ref)
            ok &= check("字节落在隔离根的任务 asset-uploads（内容寻址名）",
                        (iso_projects / "auto-9502" / "asset-uploads" / name).is_file(), name)
            code, where2 = probe_guard.http_req(base, "/api/hub/where", timeout=10)
            counters = (where2 or {}).get("asset_counters") or {}
            ok &= check("隔离实例三阶段计数：local=1 且 uguu/tencent 全 0",
                        counters.get("local") == 1 and counters.get("uguu") == 0
                        and counters.get("tencent") == 0, json.dumps(counters))
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)
    return ok


def probe_three_phase_counters() -> None:
    print("2) 三阶段图床计数 0（上传回填 / 确认批准全链不碰 uguu）")
    ok = _isolated_upload_phase()
    calls, restore = _counting_host_stubs()
    try:
        # 阶段②：replace.run_replace(confirm=False) 上传回填（bridge.upload_asset 打桩回 local）
        print("  阶段② run_replace(confirm=False) 上传回填（upload_asset 打桩回 local 引用）")
        tid = "auto-9503"
        ref = local_ref_fixture(tid)
        bid = f"probe-at14b3-{tid}"
        analysis_fixture(bid)
        tdir = make_task(tid, state_name="awaiting_assets")

        def fake_upload_asset(image, task_id=None):
            return {"local": local_assets.store(str(task_id), image.read_bytes(), image.name),
                    "bytes": image.stat().st_size, "source": "local"}

        real_up = bridge.upload_asset
        bridge.upload_asset = fake_upload_asset
        img = PROJECTS / f"{tid}.png"
        img.write_bytes(PNG_A)
        try:
            st = replace.run_replace(tdir, assets=[str(img)], map_spec=None, name_spec=None,
                                     confirm=False, verbose=False)
        finally:
            bridge.upload_asset = real_up
        ok &= check("run_replace 回填成功且状态留 awaiting_assets", st == "awaiting_assets", str(st))
        task = state.load(tdir)
        pending = task.get("pending_replace") or []
        ok &= check("流水/清单落的是 local 引用（不是公网 URL）",
                    bool(pending) and pending[0].get("url") == ref
                    and ref in (ROOT / "analysis" / "reverse" / bid / replace.LIST_NAME)
                    .read_text(encoding="utf-8"),
                    str(pending[:1]))
        ok &= check("阶段② 图床计数 0（store/uguu 一次都没调）",
                    calls["store"] == 0 and calls["uguu"] == 0, json.dumps(calls))

        # 阶段③：hub_api.do_approve（awaiting_approval → approved）
        print("  阶段③ hub_api.do_approve（批准不碰图床）")
        make_task("auto-9504", state_name="awaiting_approval")
        out = hub_api.do_approve("auto-9504")
        ok &= check("do_approve → approved", out.get("state") == "approved",
                    str(out.get("state")))
        ok &= check("阶段③ 图床计数仍 0", calls["store"] == 0 and calls["uguu"] == 0,
                    json.dumps(calls))
    finally:
        restore()
    img = PROJECTS / "auto-9503.png"
    if img.is_file():
        img.unlink()
    check("三阶段结束 uguu/store 计数全 0", calls["uguu"] == 0 and calls["store"] == 0,
          json.dumps(calls))


# ---------------------------------------------------------------- [3] 装配时序

def probe_assembly_order() -> None:
    print("3) 装配时序：assemble 必须在 prepared 之前，prepared < dispatched < post")
    tid = "auto-9505"
    ref = local_ref_fixture(tid)
    tdir = make_task(tid, assets=[{"no": 1, "url": ref}])
    events: list[str] = []
    real_assemble, real_dispatch = generate._assemble_images, generate._dispatch_write

    def spy_assemble(refs, api_format):
        events.append("assemble")
        return real_assemble(refs, api_format)

    def spy_dispatch(task_dir, guard, phase, **kw):
        events.append(phase)                       # 只记录，不落盘（时序断言不受写盘影响）
        return {}

    stub = BridgeStub()
    stub.post_impl = lambda p, b: events.append("post") or {"job": "job-t", "taskId": "cloud-t"}
    stub.get_map["/api/sdgf/video/job/job-t"] = {"status": "done", "taskId": "cloud-t"}
    stub.bytes_map["/api/sdgf/video/file/cloud-t"] = b"MP4-T"
    generate._assemble_images = spy_assemble
    generate._dispatch_write = spy_dispatch
    stub.install()
    try:
        out = generate.do_submit(tdir, verbose=False, api_key=FAKE_KEY)
    finally:
        generate._assemble_images = real_assemble
        generate._dispatch_write = real_dispatch
        restore_bridge()
    check("提交成功走完全链（done）", out == "done", str(out))
    check("事件表齐全（assemble/prepared/dispatched/post）",
          events == ["assemble", "prepared", "dispatched", "post"], str(events))
    order = events.index("assemble") < events.index("prepared") < \
        events.index("dispatched") < events.index("post")
    check("顺序：assemble < prepared < dispatched < post", order, str(events))


# ---------------------------------------------------------------- [4] 故障矩阵 10 条

MATRIX_EXPECT = {
    # 序号: (期望 state, 期望创建次数, 期望装配次数, 期望 dispatch 阶段)
    1: ("awaiting_approval", 0, 1, ""),
    2: ("awaiting_approval", 1, 1, ""),
    3: ("approved", 1, 1, "dispatched"),
    4: ("（装配链·服务端）", 0, 0, "-"),
    5: ("approved", 0, 1, ""),
    6: ("approved", 1, 1, "dispatched"),
    7: ("done", 0, 0, "confirmed"),
    8: ("done", 1, 1, "confirmed"),
    9: ("done", 1, 1, "confirmed"),
    10: ("done", 1, 1, "confirmed"),
}


def probe_fault_matrix() -> None:
    print("4) 故障矩阵 10 条（state / 创建 / 装配 / dispatch 逐条对表）")
    rows: list[tuple[int, str, int, int, str]] = []
    assemble_calls = {"n": 0}
    real_assemble = generate._assemble_images

    def counting_assemble(refs, api_format):
        assemble_calls["n"] += 1
        return real_assemble(refs, api_format)

    generate._assemble_images = counting_assemble
    try:
        # ---- ① 本地缺失：删本地文件 → awaiting_approval，创建 0
        tid = "auto-9506"
        ref = local_ref_fixture(tid)
        tdir = make_task(tid, assets=[{"no": 1, "url": ref}])
        _lid, name = local_assets.parse_ref(ref)
        (PROJECTS / tid / "asset-uploads" / name).unlink()
        stub = BridgeStub().install()
        assemble_calls["n"] = 0
        try:
            expect_gen_error(tdir, contains="本地资产缺失", label="①本地缺失 → GenError（前置拒绝）")
        finally:
            restore_bridge()
        rows.append((1, state.load(tdir)["state"], len(stub.posts), assemble_calls["n"],
                     dispatch_phase_of(tdir)))

        # ---- ② 创建被拒（HTTP 400）→ awaiting_approval，创建 1
        tid = "auto-9507"
        ref = local_ref_fixture(tid)
        tdir = make_task(tid, assets=[{"no": 1, "url": ref}])
        stub = BridgeStub()
        err = bridge.BridgeError("bridge POST /api/sdgf/video/run → HTTP 400: 参数不合法")
        err.status = 400
        stub.post_impl = err
        stub.install()
        assemble_calls["n"] = 0
        try:
            # AT-16 B11 起：创建阶段 4xx 的 GenError 原文是「创建请求被明确拒绝（HTTP 4xx）：…」
            # （「前置拒绝」只进 note/state，异常串里没有了）。断言跟 B11 合同走「明确拒绝」，
            # 不为旧断言去改产品文案；行为仍是 _pre_reject 受控回退 → awaiting_approval，
            # 由下面 rows 的 state/dispatch 逐条核。
            expect_gen_error(tdir, contains="明确拒绝", label="②创建被拒(400) → 明确拒绝·前置拒绝回退")
        finally:
            restore_bridge()
        rows.append((2, state.load(tdir)["state"], len(stub.posts), assemble_calls["n"],
                     dispatch_phase_of(tdir)))

        # ---- ③ 网络未知（无 status 的 BridgeError）→ submit_unknown，创建 1
        tid = "auto-9508"
        ref = local_ref_fixture(tid)
        tdir = make_task(tid, assets=[{"no": 1, "url": ref}])
        stub = BridgeStub()
        stub.post_impl = bridge.BridgeError("bridge POST 连不上: Connection refused")
        stub.install()
        assemble_calls["n"] = 0
        try:
            expect_gen_error(tdir, contains="未知", label="③网络未知 → submit_unknown")
        finally:
            restore_bridge()
        task = state.load(tdir)
        check("③ submit_unknown 落盘", bool(task.get("submit_unknown")),
              str(task.get("submit_unknown"))[:80])
        rows.append((3, task["state"], len(stub.posts), assemble_calls["n"],
                     dispatch_phase_of(tdir)))

        # ---- ④ 第二图源降级：uguu 空串 → 走腾讯云 videocache（不伪称 uguu）
        # AT-16 B12f 起：出站隧道降级已废弃移除，第二顺位是腾讯云 videocache。拆两条：
        #   ④a uguu 挂且未配 TENCENT_VC_TOKEN → ImageHostError（skipped 如实自述，本地前置拒绝；
        #      「格式对但拉不到不放行」的真 HTTP 版归 probe_at16_b12）
        #   ④b 配了凭证且直链验活通过（打桩）→ 来源如实标 tencent-videocache（本条原本要验的「不伪称 uguu」）
        tid = "auto-9509"                        # 本条不建任务目录，占号只作登记顺位
        real_uguu, real_uploads = server.upload_uguu_bytes, server.UPLOADS
        real_alive = server.image_url_alive
        real_vc, real_token = server.upload_tencent_vc, os.environ.pop("TENCENT_VC_TOKEN", None)
        tmp_up = Path(tempfile.mkdtemp(prefix="at14b3-up-"))
        server.upload_uguu_bytes = lambda data, name: ""
        server.UPLOADS = tmp_up
        try:
            png = "data:image/png;base64," + base64.b64encode(PNG_A).decode()
            plan = server.sdgf_plan({"prompt": "ok", "duration": 8, "ratio": "9:16",
                                     "resolution": "720", "audio": True,
                                     "channel": {"baseUrl": "https://api.example.com",
                                                 "apiKey": "K", "model": "sd2.5-720p-ap"},
                                     "images": [{"name": "ref.png", "dataUrl": png}]})
            refused, why = False, ""
            try:
                server.sdgf_host_images(plan)
            except server.ImageHostError as err:
                refused, why = True, err.msg
            check("④a uguu 挂且未配腾讯云凭证 → 前置拒绝（skipped 如实自述）",
                  refused and "图床不可用" in why and "未配置 TENCENT_VC_TOKEN，已跳过" in why,
                  why[:160])
            os.environ["TENCENT_VC_TOKEN"] = "probe-vc-token"      # 只验来源标注，PUT 打桩不出网
            server.upload_tencent_vc = (
                lambda data, name, key, token: (f"https://vc.probe.invalid/vc/{key}", ""))
            server.image_url_alive = lambda url, timeout=8.0: (True, "HTTP 200")   # 只验来源标注
            try:
                body, sources = server.sdgf_host_images(plan)
            finally:
                server.image_url_alive = real_alive
                server.upload_tencent_vc = real_vc
            check("④b uguu 空串 + 腾讯云直链验活通过 → sources 含 tencent-videocache（不伪称 uguu）",
                  "tencent-videocache" in sources and "uguu" not in sources, str(sources))
            check("④b 降级直链仍是 https（腾讯云前缀）",
                  all(str(u).startswith("https://") for u in body.get("images") or []),
                  str(body.get("images"))[:80])
        finally:
            server.upload_uguu_bytes = real_uguu
            server.UPLOADS = real_uploads
            server.image_url_alive = real_alive
            server.upload_tencent_vc = real_vc
            if real_token is not None:
                os.environ["TENCENT_VC_TOKEN"] = real_token
            shutil.rmtree(tmp_up, ignore_errors=True)
        rows.append((4, "（装配链·服务端）", 0, 0, "-"))

        # ---- ⑤ dispatch 前中断：_assemble_images 抛 BaseException → 无 dispatch 标记、approved、创建 0
        tid = "auto-9510"
        ref = local_ref_fixture(tid)
        tdir = make_task(tid, assets=[{"no": 1, "url": ref}])
        generate._assemble_images = lambda refs, fmt: (_ for _ in ()).throw(
            KeyboardInterrupt("probe: 模拟进程被打断（BaseException 跳过 catch）"))
        stub = BridgeStub().install()
        try:
            try:
                generate.do_submit(tdir, verbose=False, api_key=FAKE_KEY)
                check("⑤ 中断确实抛出（BaseException 不被 GenError 吞）", False, "没有抛错")
            except KeyboardInterrupt:
                check("⑤ 中断确实抛出（BaseException 不被 GenError 吞）", True, "")
            except BaseException as e:  # noqa: BLE001
                check("⑤ 中断确实抛出（BaseException 不被 GenError 吞）", False,
                      f"抛了 {type(e).__name__}")
        finally:
            generate._assemble_images = counting_assemble
            restore_bridge()
        raw = (tdir / "任务.json").read_text(encoding="utf-8")
        task = json.loads(raw)
        check("⑤ 盘上无 dispatch 标记", not task.get("dispatch"), str(task.get("dispatch")))
        check("⑤ 状态仍 approved", task["state"] == "approved", task["state"])
        rows.append((5, task["state"], len(stub.posts), 1, dispatch_phase_of(tdir)))

        # ---- ⑥ 创建后丢回执（无 taskId 的 {}）→ submit_unknown，创建 1
        tid = "auto-9511"
        ref = local_ref_fixture(tid)
        tdir = make_task(tid, assets=[{"no": 1, "url": ref}])
        stub = BridgeStub()
        stub.post_impl = lambda p, b: {}
        stub.install()
        assemble_calls["n"] = 0
        try:
            expect_gen_error(tdir, contains="未知", label="⑥丢回执 → submit_unknown 阻断")
        finally:
            restore_bridge()
        task = state.load(tdir)
        check("⑥ submit_unknown 落盘", bool(task.get("submit_unknown")),
              str(task.get("submit_unknown"))[:80])
        rows.append((6, task["state"], len(stub.posts), assemble_calls["n"],
                     dispatch_phase_of(tdir)))

        # ---- ⑦ known-id 续查：generating + cloud_task_id → 不装配、不创建、走续查
        tid = "auto-9512"
        tdir = make_task(tid, state_name="generating", gen_job="job-k",
                         gen_task_id="cloud-k", cloud_task_id="cloud-k",
                         dispatch={"phase": "confirmed", "job": "job-k", "carrier": ""})
        stub = BridgeStub()
        stub.get_map["/api/sdgf/video/job/job-k"] = {"status": "done", "taskId": "cloud-k"}
        stub.bytes_map["/api/sdgf/video/file/cloud-k"] = b"MP4-K"
        stub.install()
        assemble_calls["n"] = 0
        try:
            out = generate.do_submit(tdir, verbose=False, api_key=FAKE_KEY)
        finally:
            restore_bridge()
        check("⑦ 续查直接到 done", out == "done", str(out))
        check("⑦ 走续查端点（job 查询）且零创建", "/api/sdgf/video/job/job-k" in stub.gets
              and not stub.posts, f"gets={stub.gets[:2]} posts={len(stub.posts)}")
        rows.append((7, state.load(tdir)["state"], len(stub.posts), assemble_calls["n"],
                     dispatch_phase_of(tdir)))

        # ---- ⑧ approved 解除重提：release + 撤销重批后再 submit → 新创建 1、装配被调
        tid = "auto-9513"
        ref = local_ref_fixture(tid)
        tdir = make_task(tid, assets=[{"no": 1, "url": ref}],
                         submit_unknown={"at": "x", "reason": "未知收单（探针）"})
        stub = BridgeStub().install()             # 阻断期一发都不许
        try:
            expect_gen_error(tdir, contains="submit_unknown", label="⑧阻断期重提被拒")
            check("⑧阻断期创建 0", not stub.posts, str(len(stub.posts)))
        finally:
            restore_bridge()
        hub_api.release_submit_unknown(tid, confirm=True, note="探针：已核对渠道无单")
        hub_api.revoke_approval(tid)
        gen = state.load(tdir)
        generate.do_approve(tdir, config=(gen["gen_params"], gen["gen_channel"]), verbose=False)
        stub = BridgeStub()
        stub.post_impl = lambda p, b: {"job": "job-r", "taskId": "cloud-r"}
        stub.get_map["/api/sdgf/video/job/job-r"] = {"status": "done", "taskId": "cloud-r"}
        stub.bytes_map["/api/sdgf/video/file/cloud-r"] = b"MP4-R"
        stub.install()
        assemble_calls["n"] = 0
        try:
            out = generate.do_submit(tdir, verbose=False, api_key=FAKE_KEY)
        finally:
            restore_bridge()
        check("⑧解除重批后再提交 → done", out == "done", str(out))
        rows.append((8, state.load(tdir)["state"], len(stub.posts), assemble_calls["n"],
                     dispatch_phase_of(tdir)))

        # ---- ⑨ 孤儿恢复重批：generating 无证据 → 标 submit_unknown；release + 恢复重批后才新创建
        tid = "auto-9514"
        ref = local_ref_fixture(tid)
        tdir = make_task(tid, state_name="generating", assets=[{"no": 1, "url": ref}])
        stub = BridgeStub().install()
        assemble_calls["n"] = 0
        try:
            expect_gen_error(tdir, contains="孤儿", label="⑨generating 无证据 → 标 submit_unknown")
            check("⑨孤儿期创建 0 / 装配 0", not stub.posts and assemble_calls["n"] == 0,
                  f"posts={len(stub.posts)} assemble={assemble_calls['n']}")
            check("⑨submit_unknown 落盘", bool(state.load(tdir).get("submit_unknown")), "")
        finally:
            restore_bridge()
        hub_api.release_submit_unknown(tid, confirm=True, note="探针：已核对渠道无单")
        hub_api.recover_orphan(tid, confirm=True)
        gen = state.load(PROJECTS / tid)
        generate.do_approve(PROJECTS / tid, config=(gen["gen_params"], gen["gen_channel"]),
                            verbose=False)
        stub = BridgeStub()
        stub.post_impl = lambda p, b: {"job": "job-o", "taskId": "cloud-o"}
        stub.get_map["/api/sdgf/video/job/job-o"] = {"status": "done", "taskId": "cloud-o"}
        stub.bytes_map["/api/sdgf/video/file/cloud-o"] = b"MP4-O"
        stub.install()
        try:
            out = generate.do_submit(tdir, verbose=False, api_key=FAKE_KEY)
        finally:
            restore_bridge()
        check("⑨恢复重批后 → done", out == "done", str(out))
        rows.append((9, state.load(tdir)["state"], len(stub.posts),
                     assemble_calls["n"] + 0, dispatch_phase_of(tdir)))

        # ---- ⑩ 正常链：local 引用 → 装配 dataUrl → 创建 1 → done
        tid = "auto-9515"
        ref = local_ref_fixture(tid)
        tdir = make_task(tid, assets=[{"no": 1, "url": ref}])
        stub = BridgeStub()
        stub.post_impl = lambda p, b: {"job": "job-n", "taskId": "cloud-n"}
        stub.get_map["/api/sdgf/video/job/job-n"] = {"status": "done", "taskId": "cloud-n"}
        stub.bytes_map["/api/sdgf/video/file/cloud-n"] = b"MP4-N"
        stub.install()
        assemble_calls["n"] = 0
        try:
            out = generate.do_submit(tdir, verbose=False, api_key=FAKE_KEY)
        finally:
            restore_bridge()
        check("⑩正常链 → done + 成片打桩字节", out == "done"
              and (tdir / "成片.mp4").read_bytes() == b"MP4-N", str(out))
        rows.append((10, state.load(tdir)["state"], len(stub.posts), assemble_calls["n"],
                     dispatch_phase_of(tdir)))
    finally:
        generate._assemble_images = real_assemble

    print("\n  故障矩阵对表（实际值 / 期望值）：")
    ok = True
    for no, st, creates, assembles, phase in rows:
        exp = MATRIX_EXPECT[no]
        good = (st == exp[0] and creates == exp[1] and assembles == exp[2] and phase == exp[3])
        ok &= check(f"矩阵{no}：state={st} 创建={creates} 装配={assembles} dispatch={phase or '∅'}",
                    good,
                    f"期望 state={exp[0]} 创建={exp[1]} 装配={exp[2]} dispatch={exp[3] or '∅'}")
        print(f"      #{no:>2}  {st:<22} 创建={creates}  装配={assembles}  "
              f"dispatch={phase or '∅'}  {'✓' if good else '✗'}")
    check("故障矩阵 10 条全对表", ok and len(rows) == 10, f"{len(rows)} 行")


# ---------------------------------------------------------------- [5] 旧 URL 三态 + [6] wan

class _TriStubHandler(BaseHTTPRequestHandler):
    """三条路径分别 404 / 500 / 200（ref_state 与 url_to_data_url 的三态夹具）。"""
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):                    # 不污染探针输出
        return

    def _reply(self, code: int) -> None:
        body = PNG_A if code == 200 else b"err"
        self.send_response(code)
        self.send_header("Content-Type", "image/png" if code == 200 else "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self) -> None:                    # noqa: N802
        self._reply({"/ok.png": 200, "/404.png": 404}.get(self.path, 500))

    def do_GET(self) -> None:                     # noqa: N802
        self._reply({"/ok.png": 200, "/404.png": 404}.get(self.path, 500))


_URLOPEN_REAL = urllib.request.urlopen
NET = {"n": 0}


def _direct_urlopen(req, *a, **kw):
    """直连替身：绕系统代理（探针夹具全在 127.0.0.1），并计数（[6] 零网络下载断言用）。"""
    NET["n"] += 1
    return probe_guard.OP.open(req, *a, **kw)


def probe_url_tri_state() -> None:
    print("5) 旧 URL 三态：stub 404/500/200 → expired/unknown/ok；两种人话文案")
    httpd = ThreadingHTTPServer(("127.0.0.1", probe_guard.free_port()), _TriStubHandler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    ok = True
    try:
        base = f"http://127.0.0.1:{port}"
        st404 = generate.ref_state(f"{base}/404.png")
        st500 = generate.ref_state(f"{base}/500.png")
        st200 = generate.ref_state(f"{base}/ok.png")
        ok &= check("404 → ref_state expired", st404 == "expired", st404)
        ok &= check("500 → ref_state unknown", st500 == "unknown", st500)
        ok &= check("200 → ref_state ok", st200 == "ok", st200)
        try:
            generate.url_to_data_url(f"{base}/404.png", 1)
            ok &= check("url_to_data_url 对 404 报「图已过期，请重传」", False, "没有抛错")
        except generate.GenError as e:
            ok &= check("url_to_data_url 对 404 报「图已过期，请重传」", "图已过期，请重传" in str(e),
                        str(e)[:90])
        try:
            generate.url_to_data_url("http://127.0.0.1:1/x.png", 2)   # 端口 1 必拒连
            ok &= check("url_to_data_url 对断连报「图片不可用，请重试」", False, "没有抛错")
        except generate.GenError as e:
            ok &= check("url_to_data_url 对断连报「图片不可用，请重试」",
                        "图片不可用，请重试" in str(e), str(e)[:90])
    finally:
        httpd.shutdown()
        httpd.server_close()
    check("旧 URL 三态全对", ok, "")


def probe_wan_dataurl() -> None:
    print("6) wan 路径 dataUrl 直组：本地字节进提交体，全程零网络下载")
    tid = "auto-9516"
    ref = local_ref_fixture(tid)
    tdir = make_task(tid, api_format="wan", assets=[{"no": 1, "url": ref}])
    stub = BridgeStub()
    stub.post_impl = lambda p, b: {"job": "wj", "taskId": f"wan-{tid}"}
    stub.get_map[f"/api/wan/video/job/wj"] = {"status": "done", "phase": "done",
                                              "cloudTaskId": "cloud-w", "taskId": f"wan-{tid}"}
    stub.bytes_map[f"/api/wan/video/file/wan-{tid}"] = b"WAN-MP4"
    stub.install()
    NET["n"] = 0
    try:
        out = generate.do_submit(tdir, verbose=False, api_key=FAKE_KEY)
    finally:
        restore_bridge()
    task = state.load(tdir)
    body = stub.posts[0][1] if stub.posts else {}
    imgs = body.get("images") or []
    decoded = None
    if imgs and imgs[0].get("dataUrl"):
        decoded = base64.b64decode(imgs[0]["dataUrl"].split(",", 1)[1])
    check("提交体 images 元素都带 dataUrl", out == "done" and bool(imgs)
          and all("dataUrl" in i for i in imgs),
          json.dumps([{k: str(v)[:32] for k, v in i.items()} for i in imgs], ensure_ascii=False))
    check("dataUrl base64 解码 == 本地文件字节", decoded == PNG_A,
          f"{len(decoded or b'')}B vs {len(PNG_A)}B")
    check("全程 urlopen 次数 0（本地引用零网络下载）", NET["n"] == 0, str(NET["n"]))
    check("asset_host 如实落 local-dataurl（不伪称 uguu）",
          task.get("asset_host") == ["local-dataurl"], str(task.get("asset_host")))
    check("wan 提交体走 /api/wan/video/run", stub.posts[0][0] == "/api/wan/video/run",
          stub.posts[0][0])


# ---------------------------------------------------------------- 收口

def run() -> int:
    if not PROJECTS.is_dir():
        PROJECTS.mkdir(parents=True, exist_ok=True)
    guard_preflight()
    real_before = real_task_digests()
    urllib.request.urlopen = _direct_urlopen       # 直连替身：绕系统代理 + 计数
    ok = True
    try:
        probe_local_ref_hash()
        probe_three_phase_counters()
        probe_assembly_order()
        probe_fault_matrix()
        probe_url_tri_state()
        probe_wan_dataurl()
    finally:
        urllib.request.urlopen = _URLOPEN_REAL
        restore_bridge()
        cleanup_created()
        cleanup_analysis_fixtures()
    real_after = real_task_digests()
    changed = [tid for tid in REAL_TASKS if real_before[tid] != real_after[tid]]
    ok &= check("真实任务只读：auto-0002/0006/0012 任务.json SHA256 跑前跑后一致",
                not changed, str(changed))
    residue = [tid for tid in PROBE_IDS if (PROJECTS / tid).exists()]
    ok &= check("夹具零残留（auto-9501..auto-9520）", not residue, str(residue))
    try:
        probe_guard.assert_no_loss(BEFORE_SET, probe_guard.projects_snapshot(PROJECTS))
        ok &= check("真实任务集合 ⊆ 跑前（probe_guard.assert_no_loss）", True, "")
    except probe_guard.GuardError as e:
        ok &= check("真实任务集合 ⊆ 跑前（probe_guard.assert_no_loss）", False, str(e))
    total = _pass["n"] + _fail["n"]
    fail_n = _fail["n"]
    print("\n" + (f"{total} 项断言：全部通过" if fail_n == 0
                  else f"{total} 项断言：{fail_n} FAIL"))
    print(f"{_pass['n']}/{total} PASS" + ("" if fail_n == 0 else " —— 有 FAIL，如实标注"))
    return 0 if fail_n == 0 else 1


if __name__ == "__main__":
    raise SystemExit(run())



