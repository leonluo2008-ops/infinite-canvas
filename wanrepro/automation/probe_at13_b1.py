#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AT-13 B1 探针：配置接纳（approve 扩 config）+ 快照 v2 版本分流 + 退回清理键 + 任务锁。

跑法：wanrepro/ 下 `python -m automation.probe_at13_b1`（纯离线：不碰网络、不付费、
不动真实任务 auto-0001/0002/0006/0012——探针前后核那四个任务文件的 SHA256 必须一致）。

判据（docs/nodes/AT-13.md v3 §2.1/§2.2/§2.5/§2.7 + §三/§四，B1 批次）：
  [1] 正例：approve 带 config → gen_params/gen_channel/snapshot_version=2 落账，
      approval.snapshot_sha256 == v2 哈希（prompt+params+渠道身份+model+mode），
      同输入重复算稳定、key 不落盘；v1 旧算法逐字节不变。
  [2] v1 老任务不动：无 config 批准 → v1 语义（无 snapshot_version、旧哈希）；
      approved/generating/done/failed 带 config → 拒且任务文件字节不变。
  [3] 反例 400：非法 apiFormat / baseUrl 带 userinfo / baseUrl query 带凭据 /
      config 带 apiKey / wan 带 model / wan 带 mode / 渠道不接的画幅 / 越界 vquality、
      duration → HubInputError(status=400)，任务仍在 awaiting_approval 且没落 gen_channel；
      params 白名单外键与 config 顶层未知键被丢弃（不污染 gen_params）。
  [4] 退回置换清理：approval/approval_snapshot/gen_params/gen_channel/submit_unknown/
      snapshot_version 全清（提示词/自检一并作废，既有语义不变）。
  [5] 并发双 approve 同任务只成一次（另一条如实报「已批准/状态不对」，不许二次落账）。
  [6] 任务锁：同任务互斥、跨任务不阻塞、同线程重入直通不自锁；_write_task 并发改写不丢键。
  [7] 提交侧版本分流：v2 任务改 gen_channel 后 submit → 审批作废；v1 任务改 gen_params →
      审批作废；未改动的 v2 任务能过快照、只因缺 key 停在「缺渠道配置」（派发归 B2）。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "ui") not in sys.path:
    sys.path.insert(0, str(ROOT / "ui"))

from automation import generate, state          # noqa: E402
import hub_api                                  # noqa: E402

PROJECTS = ROOT / "automation" / "projects"
PROMPT_TEXT = ("镜头一：产品特写，冷光，水珠沿瓶身滑落，慢推。\n" * 12)
REAL_TASKS = ("auto-0001", "auto-0002", "auto-0006", "auto-0012")
PROBE_IDS = tuple(f"auto-93{n:02d}" for n in range(1, 25))
FAKE_KEY = "sk-probe-at13-do-not-persist-9f8e"


def check(name: str, cond: bool, detail: str = "") -> bool:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))
    return cond


# ---------------------------------------------------------------- 夹具（自建自清理）

def make_task(local_id: str, state_name: str, *, prompt: bool = True, **extra) -> Path:
    tdir = PROJECTS / local_id
    if tdir.is_dir():
        shutil.rmtree(tdir)
    tdir.mkdir(parents=True, exist_ok=True)
    if prompt:
        (tdir / "提示词.md").write_text(PROMPT_TEXT, encoding="utf-8")
    task = {"local_id": local_id, "bridge_id": f"probe-at13b1-{local_id}",
            "created_at": "2026-01-01T00:00:00+08:00", "state": state_name,
            "history": [{"at": "2026-01-01T00:00:00+08:00", "from": None, "to": state_name,
                         "note": "probe-at13-b1 夹具"}],
            "intent": "AT-13 B1 探针夹具", "assets": []}
    task.update(extra)
    state.save(tdir, task)
    return tdir


def v1_awaiting(local_id: str, **extra) -> Path:
    """老任务：render 过的 awaiting_approval（v1 快照，无 snapshot_version）。"""
    params = {"duration": 5, "ratio": "9:16", "resolution": "720", "audio": True}
    snap = state.snapshot_hash(PROMPT_TEXT, params)
    return make_task(local_id, "awaiting_approval", gen_params=params,
                     approval_snapshot=snap, **extra)


def sdgf_config(**over) -> dict:
    cfg = {"渠道身份": {"baseUrl": "https://api.juxin.example", "apiFormat": "sdgf",
                        "渠道名": "聚鑫-SDGF"},
           "model": "SD2.5-满血-GF-720P", "mode": "frames",
           "params": {"duration": 8, "size": "9:16", "vquality": "720", "audio": False}}
    for key, value in over.items():
        if key == "params":
            cfg["params"] = {**cfg["params"], **value}
        elif key == "identity":
            cfg["渠道身份"] = {**cfg["渠道身份"], **value}
        else:
            cfg[key] = value
    return cfg


def wan_config(**over) -> dict:
    """万相配置基线（model/mode 留空=固定 wan3.0 语义），可逐键覆盖做反例。"""
    cfg = sdgf_config(identity={"baseUrl": "http://127.0.0.1:8776", "apiFormat": "wan",
                                "渠道名": "本机万相"},
                      model="", mode="",
                      params={"duration": 5, "size": "16:9", "vquality": "1080",
                              "audio": True})
    for key, value in over.items():
        if key == "params":
            cfg["params"] = {**cfg["params"], **value}
        elif key == "identity":
            cfg["渠道身份"] = {**cfg["渠道身份"], **value}
        else:
            cfg[key] = value
    return cfg


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


def expect_hub_error(fn, *, status: int, contains: str = "", label: str = "") -> bool:
    """跑 fn，断言抛 HubError 且状态码/文案符合；返回是否通过。"""
    try:
        fn()
    except hub_api.HubError as e:
        code = getattr(e, "status", 409)
        ok = code == status and (contains in str(e) if contains else True)
        return check(label, ok, f"{code} / {str(e)[:90]}")
    except Exception as e:  # noqa: BLE001
        return check(label, False, f"抛了别的异常 {type(e).__name__}: {e}")
    return check(label, False, "没有抛错，居然通过了")


# ---------------------------------------------------------------- [1] 正例

def probe_positive() -> None:
    print("1) 正例：approve 带 config → 固化 + v2 快照")
    tdir = v1_awaiting("auto-9301")
    cfg = sdgf_config()
    out = hub_api.do_approve("auto-9301", config=cfg)
    task = state.load(tdir)
    gen_params = {"duration": 8, "ratio": "9:16", "resolution": "720", "audio": False}
    gen_channel = {"baseUrl": "https://api.juxin.example", "apiFormat": "sdgf",
                   "渠道名": "聚鑫-SDGF", "model": "SD2.5-满血-GF-720P", "mode": "frames"}
    check("状态 approved", task["state"] == "approved", task["state"])
    check("snapshot_version=2 落账", task.get("snapshot_version") == 2,
          str(task.get("snapshot_version")))
    check("gen_params 由画布配置派生（size→ratio、vquality→resolution）",
          task.get("gen_params") == gen_params, json.dumps(task.get("gen_params"), ensure_ascii=False))
    check("gen_channel 白名单构造（baseUrl/apiFormat/渠道名/model/mode，无 key）",
          task.get("gen_channel") == gen_channel,
          json.dumps(task.get("gen_channel"), ensure_ascii=False))
    want = state.snapshot_hash_v2(PROMPT_TEXT, gen_params, gen_channel)
    got = task["approval"]["snapshot_sha256"]
    check("approval.snapshot_sha256 = v2 哈希(prompt+params+渠道身份+model+mode)", got == want,
          f"{got[:16]}…")
    check("v2 哈希与 v1 不可比（换了结构就不会撞）",
          got != state.snapshot_hash(PROMPT_TEXT, gen_params), "")
    check("哈希稳定（同输入两次一致）",
          state.snapshot_hash_v2(PROMPT_TEXT, gen_params, gen_channel) == want, "")
    check("approve 响应透出 snapshot_version/gen_channel",
          out.get("snapshot_version") == 2 and out.get("gen_channel") == gen_channel,
          json.dumps(out, ensure_ascii=False)[:120])
    raw = (tdir / "任务.json").read_text(encoding="utf-8")
    check("key 不落盘（任务文件里没有 apiKey / 假 key 串）",
          "apiKey" not in raw and FAKE_KEY not in raw, "")
    h1 = state.snapshot_hash_v2(PROMPT_TEXT, gen_params, gen_channel)

    # 确定性：另一条同输入任务 → 同一个 v2 快照
    v1_awaiting("auto-9302")
    hub_api.do_approve("auto-9302", config=sdgf_config())
    h2 = state.load(PROJECTS / "auto-9302")["approval"]["snapshot_sha256"]
    check("同配置同提示词 → 同快照（可复算，非随机盐）", h1 == h2, f"{h1[:16]}…")

    # 渠道身份入哈希：同名模型换渠道名 → 快照变
    v1_awaiting("auto-9303")
    hub_api.do_approve("auto-9303", config=sdgf_config(identity={"渠道名": "聚鑫-SDGF-2"}))
    h3 = state.load(PROJECTS / "auto-9303")["approval"]["snapshot_sha256"]
    check("同名模型换渠道 → 快照失效（渠道身份进哈希，§2.2）", h3 != h1, f"{h3[:16]}…")

    # v1 旧算法逐字节不变（独立重建 blob 再比）
    params = {"duration": 5, "ratio": "9:16", "resolution": "720", "audio": True}
    blob = json.dumps({"prompt": PROMPT_TEXT, "params": params},
                      ensure_ascii=False, sort_keys=True)
    check("v1 快照逐字节兼容（独立算 sha256 与 state.snapshot_hash 一致）",
          state.snapshot_hash(PROMPT_TEXT, params)
          == hashlib.sha256(blob.encode("utf-8")).hexdigest(), "")
    check("snapshot_for 对 v1 任务（无版本字段）= 旧算法",
          state.snapshot_version({"gen_params": params}) == 1
          and state.snapshot_for({"gen_params": params}, PROMPT_TEXT)
          == state.snapshot_hash(PROMPT_TEXT, params), "")


# ---------------------------------------------------------------- [2] v1 老任务四分支

def probe_legacy() -> None:
    print("2) 老任务四分支：v1 批准不动 / 非 awaiting_approval 带 config 一律拒且文件不变")
    tdir = v1_awaiting("auto-9304")
    before_snap = state.load(tdir)["approval_snapshot"]
    out = hub_api.do_approve("auto-9304")
    task = state.load(tdir)
    check("无 config 批准 → approved（旧语义）", task["state"] == "approved", task["state"])
    check("v1 任务不写 snapshot_version（不擅自升级）", "snapshot_version" not in task,
          str(task.get("snapshot_version")))
    check("approval 快照 = render 时的 v1 快照",
          task["approval"]["snapshot_sha256"] == before_snap, "")
    check("approve 响应 snapshot_version=1（老任务如实标 v1）", out.get("snapshot_version") == 1,
          str(out.get("snapshot_version")))

    # approved(v1) 带 config → 拒（回退边归 B2），文件字节不变
    raw_before = task_bytes(tdir)
    expect_hub_error(lambda: hub_api.do_approve("auto-9304", config=sdgf_config()),
                     status=409, contains="B2", label="approved(v1) 带 config → 拒（B2 回退边）")
    check("拒绝后任务文件字节不变（没半途改写）", task_bytes(tdir) == raw_before, "")

    for state_name, extra, label in (
            ("generating", {"gen_job": "job-x", "gen_task_id": "task-x"}, "generating 且有 gen_job"),
            ("generating", {}, "generating 无 gen_job（孤儿，B2 处理）"),
            ("done", {}, "done"),
            ("failed", {}, "failed")):
        tid = f"auto-93{10 + len(label) % 9:02d}"
        tdir = make_task(tid, state_name, gen_params={"duration": 5}, **extra)
        raw_before = task_bytes(tdir)
        expect_hub_error(lambda tid=tid: hub_api.do_approve(tid, config=sdgf_config()),
                         status=409, label=f"{label} 带 config → 拒")
        check(f"{label} 任务文件字节不变", task_bytes(tdir) == raw_before, "")
        shutil.rmtree(tdir, ignore_errors=True)


# ---------------------------------------------------------------- [3] 反例 400

def probe_rejections() -> None:
    print("3) 反例：配置不合法 → 400，任务不落半份配置")
    cases = [
        ("apiFormat=gemini（画布专用）", sdgf_config(identity={"apiFormat": "gemini"}), "画布专用"),
        ("baseUrl 带 userinfo", sdgf_config(identity={
            "baseUrl": "https://user:pass@api.juxin.example"}), "userinfo"),
        ("baseUrl query 带 key", sdgf_config(identity={
            "baseUrl": "https://api.juxin.example/v1?key=sk-live-1"}), "凭据参数"),
        ("config 带 apiKey", {**sdgf_config(), "apiKey": FAKE_KEY}, "敏感键"),
        ("wan 带 model", wan_config(model="SD2.5-满血-GF-720P"), "wan3.0"),
        ("wan 带 mode", wan_config(mode="frames"), "没有 mode"),
        ("wan 不接 21:9", wan_config(params={"size": "21:9"}), "万相只接"),
        ("sdgf 不接 21:9（画布专用值）", sdgf_config(params={"size": "21:9"}), "SDGF 不接画幅"),
        ("vquality 越界", sdgf_config(params={"vquality": "2048"}), "vquality"),
        ("duration 越界", sdgf_config(params={"duration": 45}), "4–30"),
        ("渠道身份缺渠道名", sdgf_config(identity={"渠道名": ""}), "渠道名"),
        ("params 不是对象", {**sdgf_config(), "params": "9:16"}, "params"),
    ]
    tid = "auto-9305"
    tdir = v1_awaiting(tid)
    raw_before = task_bytes(tdir)
    for label, cfg, needle in cases:
        expect_hub_error(lambda cfg=cfg: hub_api.do_approve(tid, config=cfg),
                         status=400, contains=needle, label=f"{label} → 400")
    task = state.load(tdir)
    check("全部反例后任务仍在 awaiting_approval", task["state"] == "awaiting_approval",
          task["state"])
    check("全部反例后没有落 gen_channel / snapshot_version",
          "gen_channel" not in task and "snapshot_version" not in task, "")
    check("全部反例后任务文件字节不变", task_bytes(tdir) == raw_before, "")

    # 白名单：params 外键 + config 顶层未知键丢弃
    tdir = v1_awaiting("auto-9306")
    cfg = sdgf_config(params={"sendParam": True, "watermark": True})
    cfg["debug"] = {"verbose": True}
    cfg["渠道身份"] = {**cfg["渠道身份"], "note": "未知键"}
    hub_api.do_approve("auto-9306", config=cfg)
    task = state.load(tdir)
    check("params 白名单外键丢弃（gen_params 只有四键）",
          sorted(task["gen_params"]) == ["audio", "duration", "ratio", "resolution"],
          str(sorted(task["gen_params"])))
    check("config 顶层/渠道身份未知键丢弃（gen_channel 只有五键）",
          sorted(task["gen_channel"]) == sorted(["apiFormat", "baseUrl", "model", "mode",
                                                 "渠道名"]),
          str(sorted(task["gen_channel"])))

    # wan 合法例：model/mode 空 → gen_channel 里落空串（固定 wan3.0 语义）
    tdir = v1_awaiting("auto-9307")
    hub_api.do_approve("auto-9307", config=wan_config())
    task = state.load(tdir)
    check("wan 合法：state approved + model/mode 落空串 + apiFormat=wan",
          task["state"] == "approved" and task["gen_channel"]["model"] == ""
          and task["gen_channel"]["mode"] == "" and task["gen_channel"]["apiFormat"] == "wan",
          json.dumps(task["gen_channel"], ensure_ascii=False))
    check("wan 合法：快照仍是 v2（版本不随协议变）", task.get("snapshot_version") == 2, "")


# ---------------------------------------------------------------- [4] 退回清理

def probe_rollback() -> None:
    print("4) 退回置换：新键一并清理（§2.5 清理键表扩展）")
    tdir = v1_awaiting("auto-9308", gen_channel={"baseUrl": "https://x", "apiFormat": "sdgf",
                                                 "渠道名": "c", "model": "m", "mode": ""},
                       snapshot_version=2, submit_unknown={"at": "x", "why": "probe"})
    (tdir / "自检.json").write_text('{"items": []}', encoding="utf-8")
    out = hub_api.do_rollback("auto-9308")
    task = state.load(tdir)
    gone = [k for k in ("approval", "approval_snapshot", "gen_params", "gen_channel",
                        "submit_unknown", "snapshot_version") if k in task]
    check("状态回到 awaiting_assets", task["state"] == "awaiting_assets" and out["state"]
          == "awaiting_assets", task["state"])
    check("新键全清（approval/approval_snapshot/gen_params/gen_channel/submit_unknown/"
          "snapshot_version）", not gone, str(gone))
    check("提示词/自检随提示词一起作废（既有语义不变）",
          not (tdir / "提示词.md").is_file() and not (tdir / "自检.json").is_file(), "")


# ---------------------------------------------------------------- [5] 并发双 approve

def probe_concurrent_approve() -> None:
    print("5) 并发双 approve 同任务：只成一次")
    tdir = v1_awaiting("auto-9309")
    barrier = threading.Barrier(2)
    results: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        try:
            hub_api.do_approve("auto-9309", config=sdgf_config())
            with lock:
                results.append("ok")
        except hub_api.HubError as e:
            with lock:
                results.append(f"err:{str(e)[:40]}")

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    task = state.load(tdir)
    approved_hist = [h for h in task.get("history") or [] if h.get("to") == "approved"]
    check("恰好一次成功、一次如实报错", sorted(r.split(":")[0] for r in results) == ["err", "ok"],
          str(results))
    check("任务 approved 且历史只有一条 approved", task["state"] == "approved"
          and len(approved_hist) == 1, f"{task['state']} / {len(approved_hist)} 条")
    check("任务文件可解析（无半截写）", isinstance(json.loads((tdir / "任务.json")
                                                     .read_text(encoding="utf-8")), dict), "")


# ---------------------------------------------------------------- [6] 任务锁

def probe_task_lock() -> None:
    print("6) 任务锁：同任务互斥 / 跨任务不阻塞 / 同线程重入直通 / 短持改写不丢键")
    order: list[str] = []
    started = threading.Event()

    def holder() -> None:
        with hub_api.task_lock("auto-9311"):
            started.set()
            order.append("A-in")
            threading.Event().wait(0.15)
            order.append("A-out")

    t = threading.Thread(target=holder)
    t.start()
    started.wait(2)
    with hub_api.task_lock("auto-9311"):
        order.append("B-in")
    t.join(timeout=10)
    check("同任务互斥（A 出锁后 B 才进）",
          order.index("A-out") < order.index("B-in"), str(order))

    with hub_api.task_lock("auto-9311"):
        other_ok = True
        try:
            with hub_api.task_lock("auto-9312"):
                pass
        except Exception as e:  # noqa: BLE001
            other_ok = False
            print(f"    跨任务取锁异常：{e}")
    check("跨任务不互相阻塞", other_ok, "")

    nested_ok = True
    try:
        with hub_api.task_lock("auto-9311"):
            with hub_api.task_lock("auto-9311"):      # 非重入 Lock 下的同线程重入
                pass
    except Exception as e:  # noqa: BLE001
        nested_ok = False
        print(f"    重入取锁异常：{e}")
    check("同一线程重入直通（不自锁）", nested_ok, "")

    # _write_task 并发改不同键，两个键都要在
    tdir = make_task("auto-9312", "awaiting_assets")
    bar = threading.Barrier(2)

    def writer(key: str) -> None:
        bar.wait()
        hub_api._write_task(tdir, lambda task: task.__setitem__(key, key))

    ws = [threading.Thread(target=writer, args=(k,)) for k in ("probe_a", "probe_b")]
    for w in ws:
        w.start()
    for w in ws:
        w.join(timeout=10)
    task = state.load(tdir)
    check("短持读-改-写不丢键（并发两次改写都留住）",
          task.get("probe_a") == "probe_a" and task.get("probe_b") == "probe_b",
          json.dumps({k: task.get(k) for k in ("probe_a", "probe_b")}, ensure_ascii=False))


# ---------------------------------------------------------------- [7] 提交侧版本分流

def probe_submit_dispatch() -> None:
    print("7) 提交侧版本分流：改配置/改参数 → 作废；未改 → 过快照只缺 key")
    os.environ.pop("WANREPRO_SDGF_CHANNEL", None)

    # v2 任务：批准后改 gen_channel（换模型）→ submit 作废
    tdir = v1_awaiting("auto-9313")
    hub_api.do_approve("auto-9313", config=sdgf_config())
    task = state.load(tdir)
    task["gen_channel"]["model"] = "SD2.5-满血-GF-1080P"
    state.save(tdir, task)
    expect_gen_error(tdir, contains="作废", label="v2：批准后换模型 → submit 作废")

    # v2 任务：批准后只换 key（key 不在快照里）→ 不作废（走到缺 key 的报错）
    tdir = v1_awaiting("auto-9314")
    hub_api.do_approve("auto-9314", config=sdgf_config())
    expect_gen_error(tdir, contains="渠道配置", label="v2：未改配置 → 过快照，只报缺渠道配置")

    # v1 任务：批准后改 gen_params → 作废（旧语义零回归）
    tdir = v1_awaiting("auto-9315")
    hub_api.do_approve("auto-9315")
    task = state.load(tdir)
    task["gen_params"]["duration"] = 12
    state.save(tdir, task)
    expect_gen_error(tdir, contains="作废", label="v1：批准后改参数 → submit 作废")

    # v1 任务：未改 → 过快照，只缺 key
    tdir = v1_awaiting("auto-9316")
    hub_api.do_approve("auto-9316")
    expect_gen_error(tdir, contains="渠道配置", label="v1：未改 → 过快照，只报缺渠道配置")

    # submit 用已批准 gen_channel 的 baseUrl/model（不静默换渠道）——不真发网络，只验解析层。
    # key 的提交时来源仍是 env/file（前端一次性透传归 B3），这里用假 key 只验合并规则。
    os.environ["WANREPRO_SDGF_CHANNEL"] = json.dumps(
        {"baseUrl": "https://fallback.example", "apiKey": FAKE_KEY, "model": "fallback-model"})
    try:
        ch = generate.channel({"baseUrl": "https://approved.example", "apiFormat": "sdgf",
                               "model": "SD2.5-满血-GF-720P"})
    finally:
        os.environ.pop("WANREPRO_SDGF_CHANNEL", None)
    check("channel() 取已批准 baseUrl/model（不退回 fallback 渠道），key 仍按提交时来源给",
          ch["baseUrl"] == "https://approved.example"
          and ch["model"] == "SD2.5-满血-GF-720P" and ch["apiKey"] == FAKE_KEY,
          json.dumps({k: ("***" if k == "apiKey" else v) for k, v in ch.items()},
                     ensure_ascii=False))
    try:
        generate.channel({"baseUrl": "https://approved.example", "apiFormat": "wan",
                          "model": ""})
    except generate.GenError as e:
        check("wan 配置走到 submit → 如实报「派发归 B2」而不是误发 SDGF 端点",
              "B2" in str(e), str(e)[:80])
    else:
        check("wan 配置走到 submit → 如实报「派发归 B2」而不是误发 SDGF 端点", False,
              "没抛错——wan 可能被误派到 sdgf 链")
    check("submit_params 兼容画布词表（size/vquality → ratio/resolution）",
          generate.submit_params({"duration": 6, "size": "16:9", "vquality": "1080",
                                  "audio": False})
          == {"duration": 6, "ratio": "16:9", "resolution": "1080", "audio": False}, "")


def expect_gen_error(tdir: Path, *, contains: str, label: str) -> None:
    try:
        generate.do_submit(tdir, verbose=False)
    except generate.GenError as e:
        check(label, contains in str(e), str(e)[:90])
        return
    except Exception as e:  # noqa: BLE001
        check(label, False, f"抛了别的异常 {type(e).__name__}: {str(e)[:80]}")
        return
    check(label, False, "没有抛错（可能真发起了提交——探针禁止）")


def run() -> int:
    if not PROJECTS.is_dir():
        PROJECTS.mkdir(parents=True, exist_ok=True)
    real_before = real_task_digests()
    cleanup()
    ok = True
    try:
        probe_positive()
        probe_legacy()
        probe_rejections()
        probe_rollback()
        probe_concurrent_approve()
        probe_task_lock()
        probe_submit_dispatch()
    finally:
        cleanup()
    real_after = real_task_digests()
    changed = [tid for tid in REAL_TASKS if real_before[tid] != real_after[tid]]
    ok &= check("真实任务只读：auto-0001/0002/0006/0012 任务文件 SHA256 未变", not changed,
                str(changed))
    residue = [tid for tid in PROBE_IDS if (PROJECTS / tid).exists()]
    ok &= check("探针夹具零残留", not residue, str(residue))
    print("\n" + ("全部通过" if ok else "有 FAIL——如实标注，不降级"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(run())
