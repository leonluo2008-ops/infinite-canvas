#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AT-13 B2 探针：提交结果分类与受控迁移（**纯离线**：bridge 全部打桩、不付费、不动真实任务）。

跑法：wanrepro/ 下 `python -m automation.probe_at13_b2`。
真实任务 auto-0001/0002/0006/0012 只读：探针前后任务文件 SHA256 必须一致（含 auto-0012 这个真孤儿）。

判据（docs/nodes/AT-13.md v3 §2.3/§2.4/§2.6/§2.8 + §三/§四，B2 批次）：
  [1] 外呼前持久标记：创建替身先记「调用时盘上 dispatch 已是 dispatched」，随后模拟进程中断
      （抛 BaseException 跳过 catch）→ 盘上 approved + dispatch=dispatched、无 gen_job/云端 id。
  [2] 跨进程守卫：中断后二次提交（generate 与 runner 两条入口）创建计数仍为 1，任务标 submit_unknown。
  [3] 成功回执原子：generating / gen_job / 云端 id / dispatch=confirmed 同一次 save（逐次快照断言
      「凡是 generating 就必有 gen_job+cloud id+confirmed」），成片落盘 → done。
  [4] 前置拒绝（HTTP 400/409）→ 受控回退 awaiting_approval：清 approval/approval_snapshot/提交链键，
      保留 assets_confirmed/empty_confirm/提示词/gen_params/gen_channel，gen_error 原文可见、history 可溯。
  [5] 未知收单（502/504/断连）→ submit_unknown 持久阻断 + gen_error 脱敏原因；再提交不再创建。
  [6] 解除阻断（人工动作）→ 清 submit_unknown/dispatch=cleared + history，之后可重新提交。
  [7] 孤儿三步判定 + generating 受控恢复四守卫（活跃提交/云端在途/成片/未核对解除）各有正反例。
  [8] failed/error 双方言终态；「超时未知」不判 failed、不给盲目重提。
  [9] wan 派发适配：URL→dataUrl 按原图号顺序；失败四阶段（前置拒绝/提交未知/已知 id 等待超时/
      确认失败）；cloud_task_id=记录.json.taskId（**不是本地 wan-* id**）；重启后按承载目录续查。
  [10] 脱敏：假 key 在 600 字节截断边界上 → 响应/任务文件/job 记录/history 里既无 key 也无前缀。
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "ui") not in sys.path:
    sys.path.insert(0, str(ROOT / "ui"))

from automation import bridge, generate, runner, state      # noqa: E402
import hub_api                                              # noqa: E402

PROJECTS = ROOT / "automation" / "projects"
PROMPT_TEXT = ("镜头一：产品特写，冷光，水珠沿瓶身滑落，慢推。\n" * 12)
REAL_TASKS = ("auto-0001", "auto-0002", "auto-0006", "auto-0012")
PROBE_IDS = tuple(f"auto-94{n:02d}" for n in range(1, 25))
FAKE_KEY = "sk-" + "probe-" + "AT13B2" + "-9f3c8a1e"      # 唯一假 key（拼出来，不当真实串用）
SDGF_GEN = {"baseUrl": "https://api.juxin.example", "apiFormat": "sdgf",
            "渠道名": "聚鑫-SDGF", "model": "SD2.5-满血-GF-720P", "mode": "frames"}
WAN_GEN = {"baseUrl": "http://127.0.0.1:8776", "apiFormat": "wan",
           "渠道名": "本机万相", "model": "", "mode": ""}
SDGF_PARAMS = {"duration": 5, "ratio": "9:16", "resolution": "720", "audio": True}
WAN_PARAMS = {"duration": 5, "ratio": "16:9", "resolution": "1080", "audio": True}


def check(name: str, cond: bool, detail: str = "") -> bool:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))
    return cond


# ---------------------------------------------------------------- 夹具（自建自清理）

def task_bytes(tdir: Path) -> bytes:
    return (tdir / "任务.json").read_bytes()


def real_task_digests() -> dict:
    out = {}
    for tid in REAL_TASKS:
        f = PROJECTS / tid / "任务.json"
        out[tid] = hashlib.sha256(f.read_bytes()).hexdigest() if f.is_file() else ""
    return out


def cleanup() -> None:
    for tid in PROBE_IDS:
        shutil.rmtree(PROJECTS / tid, ignore_errors=True)


def make_approved(local_id: str, *, api_format: str = "sdgf", assets: list | None = None,
                  state_name: str = "approved", ch_override: dict | None = None,
                  **extra) -> Path:
    """造一个「已批准」任务（v2 快照自洽——submit 能过快照比对）。"""
    tdir = PROJECTS / local_id
    if tdir.is_dir():
        shutil.rmtree(tdir)
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / "提示词.md").write_text(PROMPT_TEXT, encoding="utf-8")
    params = dict(WAN_PARAMS if api_format == "wan" else SDGF_PARAMS)
    gen_channel = dict(WAN_GEN if api_format == "wan" else SDGF_GEN)
    if ch_override:
        gen_channel.update(ch_override)           # 改配置要在算快照之前（否则是「批准后改过」）
    snap = state.snapshot_hash_v2(PROMPT_TEXT, params, gen_channel)
    task = {"local_id": local_id, "bridge_id": f"probe-at13b2-{local_id}",
            "created_at": "2026-01-01T00:00:00+08:00", "state": state_name,
            "history": [{"at": "2026-01-01T00:00:00+08:00", "from": None, "to": state_name,
                         "note": "probe-at13-b2 夹具"}],
            "intent": "AT-13 B2 探针夹具",
            "gen_params": params, "gen_channel": gen_channel,
            "approval_snapshot": snap, "snapshot_version": 2,
            "approval": {"snapshot_sha256": snap, "at": "2026-01-01T00:00:00+08:00",
                         "by": "probe"},
            "assets_confirmed": assets if assets is not None else [],
            "empty_confirm": True}
    task.update(extra)
    state.save(tdir, task)
    return tdir


class BridgeStub:
    """bridge 打桩：记调用、按需返回/抛错；**创建调用时读盘上 dispatch** 证明「先落盘再外呼」。"""

    def __init__(self):
        self.posts: list[tuple[str, dict]] = []
        self.gets: list[str] = []
        self.bytes_calls: list[str] = []
        self.dispatch_at_create: list[str] = []
        self.task_dir: Path | None = None
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
        if self.task_dir is not None:
            try:
                self.dispatch_at_create.append(
                    state.dispatch_phase(state.load(self.task_dir)))
            except Exception:  # noqa: BLE001 —— 夹具读不到就当空
                self.dispatch_at_create.append("")
        if self.post_impl is not None:
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


_ORIG = (bridge.post_json, bridge.get, bridge.get_bytes)


def restore_bridge() -> None:
    bridge.post_json, bridge.get, bridge.get_bytes = _ORIG


def expect_gen_error(tdir: Path, *, contains: str = "", label: str = "", **kw) -> str:
    kw.setdefault("api_key", FAKE_KEY)          # key 一次性透传（不落盘），探针统一用假 key
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


def expect_hub_error(fn, *, contains: str = "", label: str = "", status: int | None = None) -> bool:
    try:
        fn()
    except hub_api.HubError as e:
        ok = (contains in str(e) if contains else True) and (
            status is None or getattr(e, "status", 409) == status)
        return check(label, ok, f"{getattr(e, 'status', '?')} / {str(e)[:90]}")
    except BaseException as e:  # noqa: BLE001
        return check(label, False, f"抛了别的异常 {type(e).__name__}: {e}")
    return check(label, False, "没有抛错，居然通过了")


def history_pairs(task: dict) -> list[tuple[str, str]]:
    return [(h.get("from"), h.get("to")) for h in task.get("history") or []]


# ---------------------------------------------------------------- [1] 外呼前落盘 + 崩溃注入

def probe_prepare_then_crash() -> None:
    print("1) 外呼前持久标记：创建替身先记盘上 dispatch，随后模拟进程中断")
    tdir = make_approved("auto-9401")
    stub = BridgeStub().install()
    stub.task_dir = tdir

    def _crash(path, body):                       # 「云端收单后进程崩了」：跳过所有 except
        raise SystemExit("probe: 模拟进程中断（跳过 catch/finally）")

    stub.post_impl = _crash
    try:
        generate.do_submit(tdir, verbose=False, api_key=FAKE_KEY)
    except SystemExit:
        pass
    finally:
        restore_bridge()
    task = state.load(tdir)
    check("外呼时盘上 dispatch 已是 dispatched（先落盘再发请求）",
          stub.dispatch_at_create == ["dispatched"], str(stub.dispatch_at_create))
    check("中断后状态仍 approved（没进 generating）", task["state"] == "approved", task["state"])
    check("中断后 dispatch=dispatched（无 confirmed 回执 = 未知）",
          state.dispatch_phase(task) == "dispatched", state.dispatch_phase(task))
    check("中断后无 gen_job / gen_task_id / cloud_task_id（不会假装有回执）",
          not any(task.get(k) for k in ("gen_job", "gen_task_id", "cloud_task_id")),
          json.dumps({k: task.get(k) for k in ("gen_job", "gen_task_id", "cloud_task_id")},
                     ensure_ascii=False))
    check("任务文件可解析（无半截写）", isinstance(json.loads(task_bytes(tdir)), dict), "")
    check("创建调用计数=1（中断前只发出一次）", len(stub.posts) == 1, str(len(stub.posts)))


# ---------------------------------------------------------------- [2] 跨进程守卫

def probe_cross_process_guard() -> None:
    print("2) 跨进程守卫：重启（无 HUB_JOBS）后 generate / runner / hub 三条入口都不再创建")
    tdir = PROJECTS / "auto-9401"                 # 接着 [1] 的现场：approved + dispatched
    stub = BridgeStub().install()                 # 全新实例 = 模拟进程重启，HUB_JOBS 空
    try:
        expect_gen_error(tdir, contains="未知", label="generate 二次提交被拒（dispatch 未完结）")
        check("二次提交创建计数=0（一次创建都没发）", len(stub.posts) == 0, str(len(stub.posts)))
        task = state.load(tdir)
        check("被拒后落 submit_unknown（持久阻断，不随时间消失）",
              isinstance(task.get("submit_unknown"), dict), str(task.get("submit_unknown"))[:60])
        blocked = False
        try:
            runner.advance(tdir, "approved")      # runner/CLI 是同一条链
        except generate.GenError as e:
            blocked = "未知" in str(e)
        except BaseException as e:  # noqa: BLE001
            print(f"    runner 抛了别的异常：{type(e).__name__}: {e}")
        check("runner 入口同样被守卫挡住（不只 hub 一层）", blocked, "")
        check("runner 也没创建（计数仍 0）", len(stub.posts) == 0, str(len(stub.posts)))
    finally:
        restore_bridge()
    expect_hub_error(lambda: hub_api.start_submit("auto-9401"), contains="dispatch",
                     label="hub start_submit 同样被拒（dispatch 未完结）")
    # 只有 submit_unknown、没有 dispatch 的形态（比如重启后 dispatch 已清但阻断还在）也要挡
    make_approved("auto-9409",
                  submit_unknown={"at": "2026-01-01T00:00:00+08:00", "reason": "未知收单（探针）"})
    expect_hub_error(lambda: hub_api.start_submit("auto-9409"), contains="submit_unknown",
                     label="hub 侧单看 submit_unknown 也挡（守卫不靠单一标记）")
    stub9 = BridgeStub().install()               # 万一实现漏挡，也绝不真发请求（离线铁律）
    try:
        expect_gen_error(PROJECTS / "auto-9409", contains="submit_unknown",
                         label="generate 侧单看 submit_unknown 也挡")
    finally:
        restore_bridge()
    check("generate 侧漏挡也没发出任何创建（离线）", not stub9.posts, str(len(stub9.posts)))


# ---------------------------------------------------------------- [3] 成功回执原子写

def probe_atomic_success() -> None:
    print("3) 成功回执：generating/gen_job/云端 id/confirmed 同一次 save；成片落盘 → done")
    tdir = make_approved("auto-9402", assets=[{"no": 1, "url": "https://img.example/a.png"},
                                              {"no": 2, "url": "https://img.example/b.png"}])
    stub = BridgeStub().install()
    stub.task_dir = tdir
    stub.post_impl = lambda p, b: {"job": "job-a", "taskId": "cloud-a"}
    stub.get_map["/api/sdgf/video/job/job-a"] = {"status": "done", "taskId": "cloud-a"}
    stub.bytes_map["/api/sdgf/video/file/cloud-a"] = b"MP4-BYTES"
    snapshots: list[dict] = []
    real_save = state.save

    def spy_save(task_dir, task):                 # 每次落盘留快照（含 transition 内部那次）
        snapshots.append(json.loads(json.dumps(task, ensure_ascii=False)))
        return real_save(task_dir, task)

    state.save = spy_save
    try:
        out = generate.do_submit(tdir, verbose=False, api_key=FAKE_KEY)
    finally:
        state.save = real_save
        restore_bridge()
    task = state.load(tdir)
    check("提交成功 → done", out == "done" and task["state"] == "done", f"{out}/{task['state']}")
    bad = [s for s in snapshots
           if s.get("state") == "generating"
           and (not s.get("gen_job") or not s.get("cloud_task_id")
                or (s.get("dispatch") or {}).get("phase") != "confirmed")]
    check("所有 generating 快照都同盘带 gen_job+cloud_task_id+dispatch=confirmed（原子）",
          not bad, f"{len(bad)} 个半截快照")
    check("外呼那一刻盘上 dispatch=dispatched（生命周期 prepared→dispatched→confirmed）",
          stub.dispatch_at_create == ["dispatched"], str(stub.dispatch_at_create))
    check("成片落盘且非空", (tdir / "成片.mp4").is_file()
          and (tdir / "成片.mp4").stat().st_size > 0, "")
    check("cloud_task_id = 云端 id（不是 bridge job 号）", task.get("cloud_task_id") == "cloud-a",
          str(task.get("cloud_task_id")))
    check("提交体带 channel 与按图号序的 images",
          stub.posts[0][1]["channel"]["model"] == SDGF_GEN["model"]
          and [i["url"] for i in stub.posts[0][1]["images"]]
          == ["https://img.example/a.png", "https://img.example/b.png"],
          json.dumps({k: v for k, v in stub.posts[0][1].items() if k != "prompt"},
                     ensure_ascii=False)[:160])
    raw = (tdir / "任务.json").read_text(encoding="utf-8")
    check("api_key 不落盘（任务文件里没有假 key）", FAKE_KEY not in raw, "")
    check("提交成功不落 submit_unknown / gen_error",
          not task.get("submit_unknown") and not task.get("gen_error"), "")


# ---------------------------------------------------------------- [4] 前置拒绝 → 受控回退

def probe_pre_rejection() -> None:
    print("4) 前置拒绝（HTTP 400/409）→ 受控回退 awaiting_approval，保留资产/提示词/草稿配置")
    for idx, (status, label) in enumerate(((400, "HTTP 400（校验/缺配置）"),
                                           (409, "HTTP 409（并发满）")), start=3):
        tdir = make_approved(f"auto-94{idx:02d}",
                             assets=[{"no": 1, "url": "https://img.example/a.png"}],
                             pending_replace=[{"no": 1, "url": "https://img.example/a.png"}])
        stub = BridgeStub().install()

        def _reject(path, body, status=status):
            err = bridge.BridgeError(f"bridge POST {path} → HTTP {status}: 渠道前置拒绝")
            err.status = status
            raise err

        stub.post_impl = _reject
        try:
            expect_gen_error(tdir, contains="前置拒绝", label=f"{label} → 提交被拒（受控回退）")
        finally:
            restore_bridge()
        task = state.load(tdir)
        check(f"{label}：状态回 awaiting_approval", task["state"] == "awaiting_approval",
              task["state"])
        check("清 approval / approval_snapshot（必须重批）",
              not task.get("approval") and not task.get("approval_snapshot"), "")
        check("清 dispatch / gen_job（不留阻断，改好可重试）",
              not state.dispatch_phase(task) and not task.get("gen_job"), "")
        check("保留 assets_confirmed / empty_confirm / 提示词",
              bool(task.get("assets_confirmed")) and task.get("empty_confirm") is True
              and (tdir / "提示词.md").is_file(), "")
        check("保留 gen_params / gen_channel（起草配置还在）",
              bool(task.get("gen_params")) and bool(task.get("gen_channel")), "")
        check("gen_error 落盘且有原文（前端红条数据源）",
              "前置拒绝" in str(task.get("gen_error") or ""), str(task.get("gen_error"))[:80])
        check("history 记 approved→awaiting_approval",
              ("approved", "awaiting_approval") in history_pairs(task), str(history_pairs(task)[-1]))


# ---------------------------------------------------------------- [5] 未知收单 → 阻断

def probe_unknown_receipt() -> None:
    print("5) 未知收单（502 / 504 / 断连）→ submit_unknown 持久阻断，不许重提")
    for idx, (name, exc) in enumerate((
            ("HTTP 502（响应非 JSON / 无 id）",
             bridge.BridgeError("bridge POST /api/sdgf/video/run → HTTP 502: 创建响应里没有任务 id")),
            ("HTTP 504（超时）",
             bridge.BridgeError("bridge POST /api/sdgf/video/run → HTTP 504: 超时")),
            ("断连（无状态码）",
             bridge.BridgeError("bridge POST /api/sdgf/video/run 连不上: Connection refused"))),
            start=5):
        tdir = make_approved(f"auto-94{idx:02d}")
        stub = BridgeStub().install()

        def _unknown(path, body, exc=exc):
            raise exc

        stub.post_impl = _unknown
        try:
            expect_gen_error(tdir, contains="未知", label=f"{name} → 结果未知（阻断）")
        finally:
            restore_bridge()
        task = state.load(tdir)
        check("落 submit_unknown（持久阻断）", isinstance(task.get("submit_unknown"), dict),
              str(task.get("submit_unknown"))[:70])
        check("状态**不**进 generating、也不判 failed", task["state"] == "approved", task["state"])
        check("dispatch 保持 dispatched（未知收单不能自证清白）",
              state.dispatch_phase(task) == "dispatched", state.dispatch_phase(task))
        check("gen_error 有原因", bool(task.get("gen_error")), str(task.get("gen_error"))[:70])


# ---------------------------------------------------------------- [6] 解除阻断

def probe_release() -> None:
    print("6) 解除阻断（人工动作）→ 清 submit_unknown、dispatch=cleared，之后可重新提交")
    tdir = PROJECTS / "auto-9405"                 # [5] 第一个未知收单现场
    expect_hub_error(lambda: hub_api.release_submit_unknown("auto-9405", confirm=False),
                     label="未确认就解除 → 拒（人工动作，不许脚本静默放行）", status=400)
    check("未确认时阻断仍在（没被静默清掉）", bool(state.load(tdir).get("submit_unknown")), "")
    out = hub_api.release_submit_unknown("auto-9405", confirm=True,
                                         note="探针：已核对渠道无这一单")
    task = state.load(tdir)
    check("解除后 submit_unknown / gen_error 清空",
          not task.get("submit_unknown") and not task.get("gen_error"), "")
    check("dispatch 记 cleared（审计留痕，不是无声删除）",
          state.dispatch_phase(task) == "cleared", str(task.get("dispatch"))[:80])
    notes = " | ".join(str(h.get("note")) for h in task.get("history") or [])
    check("解除动作记 history：人工说明 + 原阻断原因都在",
          "已核对渠道无这一单" in notes and "原阻断" in notes, notes[-160:])
    check("返回 dispatch=cleared", out.get("dispatch", {}).get("phase") == "cleared", "")
    stub = BridgeStub().install()
    stub.task_dir = tdir
    stub.post_impl = lambda p, b: {"job": "job-after", "taskId": "cloud-after"}
    stub.get_map["/api/sdgf/video/job/job-after"] = {"status": "failed", "error": "渠道说不行"}
    try:
        expect_gen_error(tdir, contains="云端判失败", label="解除后重新提交 → 走到云端（不再被守卫拦）")
    finally:
        restore_bridge()
    check("解除后确实又创建了一次（1 次）", len(stub.posts) == 1, str(len(stub.posts)))


# ---------------------------------------------------------------- [7] 孤儿三步 + 受控恢复

def probe_orphan() -> None:
    print("7) 孤儿三步判定 + generating 受控恢复四守卫（正反例各一）")
    tdir = make_approved("auto-9410", state_name="generating")
    d1 = generate.diagnose(tdir, state.load(tdir), active_submit=True)
    check("第①步：有活跃提交 → 显示进行中、禁重试（submit_lost=False）",
          d1["step"] == 1 and not d1["submit_lost"] and "提交进行中" in d1["reason"],
          str(d1["reason"]))
    task = state.load(tdir)
    task["gen_job"] = "job-x"
    task["gen_task_id"] = "cloud-x"
    task["cloud_task_id"] = "cloud-x"
    task["dispatch"] = {"phase": "confirmed", "job": "job-x", "carrier": ""}
    state.save(tdir, task)
    d2 = generate.diagnose(tdir, state.load(tdir))
    check("第②步：有云端 taskId → 续查（不恢复、不重提）",
          d2["step"] == 2 and d2["cloud_in_flight"] and not d2["submit_lost"], str(d2["reason"]))
    expect_hub_error(lambda: hub_api.recover_orphan("auto-9410", confirm=True),
                     contains="云端在途", label="恢复守卫②：有云端在途 → 拒")

    tdir2 = make_approved("auto-9411", state_name="generating")
    (tdir2 / "成片.mp4").write_bytes(b"VIDEO")
    expect_hub_error(lambda: hub_api.recover_orphan("auto-9411", confirm=True),
                     contains="成片已在", label="恢复守卫③：成片已在 → 拒（走完成/续查）")

    tdir3 = make_approved("auto-9412", state_name="generating",
                          dispatch={"phase": "prepared", "note": "外呼前标记"})
    d3 = generate.diagnose(tdir3, state.load(tdir3))
    check("第③步：无在途证据且无成片 → 疑似孤儿（submit_lost=True）",
          d3["step"] == 3 and d3["submit_lost"], str(d3["reason"]))
    expect_hub_error(lambda: hub_api.recover_orphan("auto-9412", confirm=True),
                     contains="未核对解除", label="恢复守卫④：dispatch 未解除 → 拒")

    make_approved("auto-9413", state_name="generating",
                  submit_unknown={"at": "x", "reason": "未知收单"})
    expect_hub_error(lambda: hub_api.recover_orphan("auto-9413", confirm=True),
                     contains="解除阻断", label="恢复守卫④：submit_unknown 未解除 → 拒")

    job = hub_api._spawn("probe-hold", lambda: None)
    hub_api.HUB_JOBS[job["job_id"]].update({"local_id": "auto-9414", "status": "running"})
    tdir5 = make_approved("auto-9414", state_name="generating")
    expect_hub_error(lambda: hub_api.recover_orphan("auto-9414", confirm=True),
                     contains="活跃提交", label="恢复守卫①：有活跃提交占用 → 拒")
    hub_api.HUB_JOBS.pop(job["job_id"], None)

    expect_hub_error(lambda: hub_api.recover_orphan("auto-9414", confirm=False),
                     label="未确认就恢复 → 400（人工动作）", status=400)

    task = state.load(tdir5)
    task["assets_confirmed"] = [{"no": 1, "url": "https://img.example/a.png"}]
    state.save(tdir5, task)
    out = hub_api.recover_orphan("auto-9414", confirm=True)
    task = state.load(tdir5)
    check("正例：generating → awaiting_approval", task["state"] == "awaiting_approval"
          and out["state"] == "awaiting_approval", task["state"])
    check("清 approval/approval_snapshot/提交链键（含 dispatch/submit_unknown/gen_job）",
          not any(k in task for k in ("approval", "approval_snapshot", "dispatch", "submit_unknown",
                                      "gen_job", "gen_task_id", "cloud_task_id", "gen_error")),
          json.dumps(sorted(task), ensure_ascii=False)[:120])
    check("保留资产/提示词/草稿配置",
          bool(task.get("assets_confirmed")) and (tdir5 / "提示词.md").is_file()
          and bool(task.get("gen_params")) and bool(task.get("gen_channel")), "")
    check("history 记 generating→awaiting_approval",
          ("generating", "awaiting_approval") in history_pairs(task), str(history_pairs(task)[-1]))
    check("恢复后诊断不再报孤儿", not generate.diagnose(tdir5, task)["submit_lost"], "")

    # 孤儿三步判定要「透出」——前端只能看 get_task，不能自己猜
    d = hub_api.get_task("auto-9412")             # generating + dispatch=prepared 的孤儿现场
    need = ("gen_error", "submit_unknown", "dispatch", "cloud_task_id", "active_submit",
            "submit_lost")
    check("get_task 透出提交链六项（gen_error/submit_unknown/dispatch/cloud_task_id/"
          "active_submit/submit_lost）", all(k in d for k in need),
          str([k for k in need if k not in d]))
    check("get_task 的三步结论可读（step=3 疑似孤儿 + 原因文案）",
          isinstance(d.get("submit_lost"), dict) and d["submit_lost"].get("step") == 3
          and bool(d["submit_lost"].get("reason")), str(d.get("submit_lost"))[:120])
    d9 = hub_api.get_task("auto-9409")            # 只有 submit_unknown 的现场
    check("get_task 同时透出 submit_unknown 原文（前端红条 + 解除入口用）",
          isinstance(d9.get("submit_unknown"), dict)
          and "未知收单（探针）" in str(d9["submit_unknown"].get("reason")), "")


# ---------------------------------------------------------------- [8] failed/error 方言

def probe_dialects() -> None:
    print("8) failed/error 双方言终态；「超时未知」不判 failed")
    for idx, (status, label) in enumerate((("failed", "status=failed（万相/站方方言）"),
                                           ("error", "status=error（SDGF 老方言）")), start=15):
        tdir = make_approved(f"auto-94{idx:02d}")
        stub = BridgeStub().install()
        stub.task_dir = tdir
        stub.post_impl = lambda p, b, idx=idx: {"job": f"job-{idx}", "taskId": f"cloud-{idx}"}
        stub.get_map[f"/api/sdgf/video/job/job-{idx}"] = {"status": status,
                                                         "error": "渠道说这条不行"}
        try:
            expect_gen_error(tdir, contains="失败", label=f"{label} → 任务 failed")
        finally:
            restore_bridge()
        task = state.load(tdir)
        check(f"{label}：状态 failed + gen_error 落盘（不吊死在 generating）",
              task["state"] == "failed" and "渠道说这条不行" in str(task.get("gen_error")),
              f"{task['state']} / {str(task.get('gen_error'))[:50]}")
        check("失败终态保留云端 id（对账/找回用）", bool(task.get("cloud_task_id")),
              str(task.get("cloud_task_id")))

    tdir = make_approved("auto-9417")
    stub = BridgeStub().install()
    real_max = generate.GEN_POLL_MAX
    stub.post_impl = lambda p, b: {"job": "job-t", "taskId": "cloud-t"}
    generate.GEN_POLL_MAX = 0                    # 不真等一小时
    try:
        expect_gen_error(tdir, contains="未完成", label="等待超时 → 停轮询、不判失败")
    finally:
        generate.GEN_POLL_MAX = real_max
        restore_bridge()
    task = state.load(tdir)
    check("超时未知：状态仍 generating（云端可能还在跑）", task["state"] == "generating",
          task["state"])
    check("超时未知：gen_error 写明「未完成/不重提」", "未完成" in str(task.get("gen_error")),
          str(task.get("gen_error"))[:70])
    check("超时未知：不判 failed、不丢云端 id",
          task["state"] != "failed" and task.get("cloud_task_id") == "cloud-t", "")
    check("超时未知：dispatch 仍 confirmed（不解除守卫）",
          state.dispatch_phase(task) == "confirmed", state.dispatch_phase(task))


# ---------------------------------------------------------------- [9] wan 派发适配

def _patch_wan_downloads(record: list[tuple[int, str]]):
    real = generate.url_to_data_url

    def fake(url, no):
        record.append((no, url))
        return f"data:image/png;base64,PROBE{no}"

    generate.url_to_data_url = fake
    return real


def probe_wan() -> None:
    print("9) wan 派发适配：四阶段 + cloud_task_id 语义 + URL→dataUrl 顺序 + 重启续查")
    tdir = make_approved("auto-9418", api_format="wan", ch_override={"baseUrl": ""})
    stub = BridgeStub().install()
    try:
        expect_gen_error(tdir, contains="缺渠道配置", label="(a) 万相缺 baseUrl → 前置拒绝（不创建）")
        check("(a) 一次创建都没发", len(stub.posts) == 0, str(len(stub.posts)))
    finally:
        restore_bridge()
    task = state.load(tdir)
    check("(a) 回 awaiting_approval（受控回退）", task["state"] == "awaiting_approval", task["state"])

    tdir = make_approved("auto-9419", api_format="wan")
    stub = BridgeStub().install()

    def _reject(path, body):
        err = bridge.BridgeError("bridge POST /api/wan/video/run → HTTP 400: prompt 不能为空")
        err.status = 400
        raise err

    stub.post_impl = _reject
    try:
        expect_gen_error(tdir, contains="前置拒绝", label="(b) 万相承载 400 → 前置拒绝")
    finally:
        restore_bridge()
    check("(b) 回 awaiting_approval", state.load(tdir)["state"] == "awaiting_approval",
          state.load(tdir)["state"])

    tdir = make_approved("auto-9420", api_format="wan",
                         assets=[{"no": 1, "url": "https://img.example/a.png"},
                                 {"no": 2, "url": "https://img.example/b.png"}])
    stub = BridgeStub().install()
    stub.task_dir = tdir
    stub.post_impl = lambda p, b: {"job": "wjob-1", "taskId": "wan-1730000001"}
    stub.get_map["/api/wan/video/job/wjob-1"] = {
        "status": "failed", "phase": "submit_unknown", "cloudTaskId": "",
        "taskId": "wan-1730000001", "error": "CLI 提交失败：网络断了"}
    seen: list[tuple[int, str]] = []
    real_dl = _patch_wan_downloads(seen)
    try:
        expect_gen_error(tdir, contains="未知", label="(c) 万相提交未知（无 taskId）→ 阻断")
    finally:
        generate.url_to_data_url = real_dl
        restore_bridge()
    task = state.load(tdir)
    check("(c) submit_unknown 落盘、状态仍 approved",
          isinstance(task.get("submit_unknown"), dict) and task["state"] == "approved",
          f"{task['state']}")
    check("(c) cloud_task_id 为空（**不许**把本地 wan-* 当云端 id）",
          not task.get("cloud_task_id") and not task.get("gen_task_id"),
          f"cloud={task.get('cloud_task_id')!r} gen_task_id={task.get('gen_task_id')!r}")
    check("(c) 提交体走 /api/wan/video/run（不派 SDGF 体）",
          stub.posts[0][0] == "/api/wan/video/run" and "channel" not in stub.posts[0][1],
          stub.posts[0][0])
    check("(c) 参考图按原图号顺序下载转 dataUrl",
          seen == [(1, "https://img.example/a.png"), (2, "https://img.example/b.png")]
          and [i["dataUrl"] for i in stub.posts[0][1]["images"]]
          == ["data:image/png;base64,PROBE1", "data:image/png;base64,PROBE2"],
          f"{seen} / {json.dumps(stub.posts[0][1]['images'], ensure_ascii=False)[:100]}")

    tdir = make_approved("auto-9421", api_format="wan")
    stub = BridgeStub().install()
    stub.task_dir = tdir
    stub.post_impl = lambda p, b: {"job": "wjob-2", "taskId": "wan-2"}
    stub.get_map["/api/wan/video/job/wjob-2"] = {
        "status": "failed", "phase": "wait_timeout", "cloudTaskId": "cloud-real-2",
        "taskId": "wan-2", "error": "等了 30 分钟没拿到成片"}
    try:
        expect_gen_error(tdir, contains="失败", label="(d) 已知 id 等待超时 → 停轮询、给超时原因")
    finally:
        restore_bridge()
    task = state.load(tdir)
    check("(d) 状态 generating（云端可能还在跑，不判 failed）", task["state"] == "generating",
          task["state"])
    check("(d) cloud_task_id = 记录里的云端 id（不是本地 wan-2）",
          task.get("cloud_task_id") == "cloud-real-2", str(task.get("cloud_task_id")))
    check("(d) gen_task_id 空（本地承载号不冒充云端 id）", not task.get("gen_task_id"),
          str(task.get("gen_task_id")))
    check("(d) dispatch=confirmed + gen_job 落盘",
          state.dispatch_phase(task) == "confirmed" and task.get("gen_job") == "wjob-2", "")

    tdir = make_approved("auto-9422", api_format="wan")
    stub = BridgeStub().install()
    stub.task_dir = tdir
    stub.post_impl = lambda p, b: {"job": "wjob-3", "taskId": "wan-3"}
    stub.get_map["/api/wan/video/job/wjob-3"] = {
        "status": "failed", "phase": "cloud_failed", "cloudTaskId": "cloud-real-3",
        "taskId": "wan-3", "error": "万相判失败：内容审核"}
    try:
        expect_gen_error(tdir, contains="万相失败", label="(e) 确认失败 → 任务 failed")
    finally:
        restore_bridge()
    task = state.load(tdir)
    check("(e) 状态 failed + 云端 id 留痕",
          task["state"] == "failed" and task.get("cloud_task_id") == "cloud-real-3", task["state"])

    tdir = make_approved("auto-9423", api_format="wan")
    stub = BridgeStub().install()
    stub.task_dir = tdir
    stub.post_impl = lambda p, b: {"job": "wjob-4", "taskId": "wan-4"}
    stub.get_map["/api/wan/video/job/wjob-4"] = {"status": "done", "phase": "done",
                                                 "cloudTaskId": "cloud-real-4", "taskId": "wan-4"}
    stub.bytes_map["/api/wan/video/file/wan-4"] = b"WAN-MP4"
    try:
        out = generate.do_submit(tdir, verbose=False, api_key=FAKE_KEY)
    finally:
        restore_bridge()
    task = state.load(tdir)
    check("(f) done + 成片落盘",
          out == "done" and (tdir / "成片.mp4").read_bytes() == b"WAN-MP4",
          f"{out}/{task['state']}")
    check("(f) 成片按承载目录取（bridge file 路由），cloud_task_id=云端 id",
          any("wan-4" in c for c in stub.bytes_calls)
          and task.get("cloud_task_id") == "cloud-real-4",
          f"{stub.bytes_calls} / {task.get('cloud_task_id')}")

    tdir = make_approved("auto-9424", api_format="wan", state_name="generating",
                         gen_job="wjob-lost", cloud_task_id="cloud-real-9", gen_task_id="",
                         dispatch={"phase": "confirmed", "job": "wjob-lost", "carrier": "wan-9"})
    stub = BridgeStub().install()
    stub.get_map["/api/wan/video/state/wan-9"] = {"status": "done", "phase": "done",
                                                  "cloudTaskId": "cloud-real-9", "taskId": "wan-9"}
    stub.bytes_map["/api/wan/video/file/wan-9"] = b"WAN-MP4-9"
    try:
        out = generate.do_submit(tdir, verbose=False, api_key=FAKE_KEY)
    finally:
        restore_bridge()
    check("(g) job 丢了（重启）→ 按承载目录续查成片 → done",
          out == "done" and (tdir / "成片.mp4").read_bytes() == b"WAN-MP4-9", out)
    check("(g) 续查确实打 state 端点（不是重提）",
          "/api/wan/video/state/wan-9" in stub.gets and not stub.posts,
          f"gets={stub.gets[:2]} posts={len(stub.posts)}")


# ---------------------------------------------------------------- [10] 脱敏

def probe_masking() -> None:
    print("10) 脱敏：先脱敏后截断（假 key 在 600 字节边界上）——响应/任务文件/job 都不带前缀")
    echo = "x" * 590 + " " + FAKE_KEY + " tail"
    masked = generate.safe_error(echo, FAKE_KEY, 600)
    check("safe_error：key 本体不出现", FAKE_KEY not in masked, f"{len(masked)} 字")
    check("safe_error：key 前 8 位前缀也不出现（截断边界不泄前缀）",
          FAKE_KEY[:8] not in masked, FAKE_KEY[:8])
    check("safe_error：截断到指定长度", len(masked) <= 600, str(len(masked)))
    form_echo = "上游原样回显：{\"err\":\"Bearer " + FAKE_KEY + "\"}"
    check("未知 key 也能按形态抹掉（Bearer/sk- 规则）",
          FAKE_KEY not in generate.mask_text(form_echo)
          and "***" in generate.mask_text(form_echo), generate.mask_text(form_echo)[:60])

    tdir = make_approved("auto-9408")
    stub = BridgeStub().install()

    def _echo(path, body):
        err = bridge.BridgeError(f"bridge POST /api/sdgf/video/run → HTTP 502: {echo}")
        err.status = 502
        raise err

    stub.post_impl = _echo
    try:
        expect_gen_error(tdir, label="回显 key 的 502 → 提交未知", api_key=FAKE_KEY)
    finally:
        restore_bridge()
    raw = (tdir / "任务.json").read_text(encoding="utf-8")
    check("任务文件里无 key、无前缀（gen_error/submit_unknown/history 全走安全出口）",
          FAKE_KEY not in raw and FAKE_KEY[:8] not in raw, "")

    def _boom():
        raise RuntimeError(f"渠道回显了 {FAKE_KEY} 这串 key")

    job = hub_api._spawn("probe-mask", _boom)
    for _ in range(200):
        if hub_api.HUB_JOBS.get(job["job_id"], {}).get("status") != "running":
            break
        time.sleep(0.02)
    rec = hub_api.HUB_JOBS.get(job["job_id"], {})
    blob = json.dumps(rec, ensure_ascii=False)
    check("job 记录（可 GET）里无 key、无前缀、traceback 已脱敏",
          FAKE_KEY not in blob and FAKE_KEY[:8] not in blob
          and "Traceback" in str(rec.get("stderr")), str(rec.get("error"))[:70])
    hub_api.HUB_JOBS.pop(job["job_id"], None)

    src = (ROOT / "ui" / "server.py").read_text(encoding="utf-8")
    check("sdgf_http 是「先读全量→脱敏→截 600」（不是先截再脱敏）",
          "mask_secret(raw, key)[:600]" in src and "err.read(65536)" in src, "")
    check("成片下载错误体同样先脱敏后截断", "mask_secret(resp.read(65536)" in src, "")


# ---------------------------------------------------------------- 收口

def run() -> int:
    if not PROJECTS.is_dir():
        PROJECTS.mkdir(parents=True, exist_ok=True)
    real_before = real_task_digests()
    cleanup()
    ok = True
    try:
        probe_prepare_then_crash()
        probe_cross_process_guard()
        probe_atomic_success()
        probe_pre_rejection()
        probe_unknown_receipt()
        probe_release()
        probe_orphan()
        probe_dialects()
        probe_wan()
        probe_masking()
    finally:
        restore_bridge()
        cleanup()
    real_after = real_task_digests()
    changed = [tid for tid in REAL_TASKS if real_before[tid] != real_after[tid]]
    ok &= check("真实任务只读：auto-0001/0002/0006/0012 任务文件 SHA256 未变（含真孤儿 auto-0012）",
                not changed, str(changed))
    residue = [tid for tid in PROBE_IDS if (PROJECTS / tid).exists()]
    ok &= check("探针夹具零残留", not residue, str(residue))
    print("\n" + ("全部通过" if ok else "有 FAIL——如实标注，不降级"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(run())
