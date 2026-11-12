# -*- coding: utf-8 -*-
"""强制结束「已提交远程拆解」的任务 → 立即重试 的人话闭环探针（auto-0067 现场）。

跑法（wanrepro/ 下）：`python automation/probe_force_remote_busy.py`，退出码 0 = 全绿。

锁的是卡面四条验收（不是函数返回值好看）：

  [1] 强制结束回执三分语义：本地已终止 / 远程拆解**无法取消仍在跑**（带已跑时长与预计剩余）/
      结果返回后按**作废丢弃**。回执与 任务.json 的 remote_wait 快照同一句话（详情页刷新后不丢）；
  [2] 重试入口前置引导（方案 a）：上一轮远程还在跑 → 立刻 409 拒绝，文案说清「在等什么、
      预计还需多久、为什么只能等」，并带 retry_after_seconds/remote 自述；**零派发、零迁移**
      （任务仍是 failed，HUB_JOBS 不增记录）——不许再出现「点了排队、刷失败计数」的死循环；
  [3] 作废栅栏仍在：已终止 run 晚到的结果/失败写盘被拒（write_failed 幂等 + failed 不可跳
      awaiting_assets），本次人工失败原因不被覆盖；
  [4] 槽释放后重试照旧成功（前置引导不误拦，产物优先不占槽），且 discuss 侧排队日志说人话
      （不再刷「第 N 次失败」；同任务/别任务/读不到持有者三种口径都按唯一文案源出句）。

隔离手法：`WANREPRO_AUTO_DIR` → 本次临时根（任务根唯一来源 automation.config.PROJECTS_DIR）；
`WANREPRO_BRIDGE` → 本探针自起的**可编排假桥**（真 HTTP，/api/tasks、/api/lapian/slot、
/api/lapian/run、/api/job/<hex>）。派发边界在 [4] 被替身接住（不起子进程）；夹具一律 auto-99xx、
桥 id 一律 probe-frb-*；跑完整个临时根删除，真实 projects/analysis/uploads 顶层集合跑前跑后比对。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]                 # wanrepro/
TMP_ROOT = Path(tempfile.mkdtemp(prefix="probe-force-remote-busy-"))
PROJECTS = TMP_ROOT / "projects"
PROJECTS.mkdir(parents=True, exist_ok=True)
REAL_PROJECTS = ROOT / "automation" / "projects"
REAL_ANALYSIS = ROOT / "analysis" / "reverse"
REAL_UPLOADS = ROOT / "uploads"

RESULTS: list[tuple[str, bool]] = []


def check(name: str, cond, detail: str = "") -> bool:
    ok = bool(cond)
    RESULTS.append((name, ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail and not ok else ""))
    return ok


def snapshot(root: Path) -> set[str]:
    try:
        return {p.name for p in root.iterdir()}
    except OSError:
        return set()


BEFORE = {"projects": snapshot(REAL_PROJECTS), "analysis": snapshot(REAL_ANALYSIS),
          "uploads": snapshot(REAL_UPLOADS)}


# ---------------------------------------------------------------- 可编排假桥

class ProbeBridge:
    """假桥：/api/tasks（产物标志可切）+ /api/lapian/slot（槽真相可编排）+ run/job。

    slot 是**唯一事实源**：hub 与 discuss 两侧都只从它读「谁在占槽、跑了多久」。
    """

    def __init__(self, bridge_id: str) -> None:
        self.bridge_id = bridge_id
        self.products = {"has_disc": False, "has_check": False, "has_prompt": False}
        self.busy = True
        self.holder_task = bridge_id
        self.holder_elapsed = 300.0          # 5 分钟：足够让「预计还需」落在正区间
        self.holder_job = "0123456789abcdef"
        self.run_calls = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                return

            def _send(self, code: int, payload: dict) -> None:
                raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):                       # noqa: N802
                if self.path == "/api/tasks":
                    self._send(200, {"tasks": [{"id": outer.bridge_id, **outer.products}]})
                    return
                if self.path == "/api/lapian/slot":
                    holder = None
                    if outer.busy:
                        holder = {"job": outer.holder_job, "task_id": outer.holder_task,
                                  "started_at": "", "elapsed_seconds": outer.holder_elapsed,
                                  "stage": "投影"}
                    self._send(200, {"busy": outer.busy, "holder": holder,
                                     "analyzing": [outer.bridge_id] if outer.busy else [],
                                     "checked_at": "2026-11-12T22:55:00+08:00"})
                    return
                if self.path.startswith("/api/job/"):
                    self._send(200, {"status": "running", "elapsed": 300, "stage": "投影"})
                    return
                self._send(404, {"error": "probe bridge 不认这个 GET"})

            def do_POST(self):                      # noqa: N802
                if self.path == "/api/lapian/run":
                    n = int(self.headers.get("Content-Length") or 0)
                    if n:
                        self.rfile.read(n)
                    outer.run_calls += 1
                    if outer.busy:
                        # 桥端 409 自述（与真实 server.py 同形）：error + slot + task_id
                        self._send(409, {"error": f"{outer.holder_task} 正在拆解，等它跑完（约 30–60 秒）",
                                         "slot": {"busy": True,
                                                  "holder": {"job": outer.holder_job,
                                                             "task_id": outer.holder_task,
                                                             "elapsed_seconds": outer.holder_elapsed,
                                                             "started_at": "", "stage": "投影"},
                                                  "analyzing": [outer.holder_task]},
                                         "task_id": outer.holder_task})
                        return
                    self._send(200, {"job": "feedface"})
                    return
                self._send(404, {"error": "probe bridge 不认这个 POST"})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = int(self.httpd.server_address[1])
        threading.Thread(target=self.httpd.serve_forever, daemon=True,
                         name="probe-frb-bridge").start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except OSError:
            pass


BRIDGE = ProbeBridge("probe-frb-0001")
os.environ["WANREPRO_AUTO_DIR"] = str(PROJECTS)
os.environ["WANREPRO_BRIDGE"] = BRIDGE.base
os.environ["WANREPRO_WATCHDOG"] = "0"
os.environ["WANREPRO_BOOT_RECONCILE"] = "0"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ui"))
import hub_api                                            # noqa: E402
from automation import discuss, remote_wait, state        # noqa: E402

TASK = "auto-9901"
STAGE_TEXT = "已提交拆解（job=0123456789abcdef），远程模型通常 5–15 分钟，慢时更久，不用重开"


def seed(local_id: str, st: str = "discussing") -> Path:
    d = PROJECTS / local_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "任务.json").write_text(json.dumps({
        "local_id": local_id, "state": st, "history": [], "bridge_id": BRIDGE.bridge_id,
        "intent": "探针：强制结束已提交远程拆解的任务", "created_at": "2026-11-12T22:50:00+08:00",
        "lapian_stage": STAGE_TEXT, "ingestion": {"video_name": ""},
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return d


def task_of(d: Path) -> dict:
    return json.loads((d / "任务.json").read_text(encoding="utf-8"))


def reset_slot_cache() -> None:
    hub_api._slot_cache.update({"at": 0.0, "data": None})


# ================================================================ [1] 强制结束：三分语义
print("\n[1] 强制结束一个已提交远程拆解、仍在跑的任务 → 回执三分说清（本地停/远程停不了/结果作废）")
D1 = seed(TASK)
# 现场形态：第一轮的**本地等待循环**还是一条活 job（执行体进程 + 心跳都活着）
hub_api.HUB_JOBS["probe-run-1"] = {"status": "running", "kind": "hub-discuss",
                                   "started": hub_api._now(), "local_id": TASK,
                                   "mode": "process", "pid": None}
out1 = hub_api.force_finish(TASK, confirm=True, confirm_name=TASK)
remote1 = out1.get("remote") or {}
print(f"    回执 remote：{json.dumps(remote1, ensure_ascii=False)}")
check("[1] 文件侧落 failed", task_of(D1).get("state") == "failed", str(task_of(D1).get("state")))
check("[1] 内存 job 当场置 force_finished",
      hub_api.HUB_JOBS["probe-run-1"].get("status") == "force_finished",
      str(hub_api.HUB_JOBS.get("probe-run-1")))
check("[1] 远程半边: running=True（远程拆解还在跑）", remote1.get("running") is True, str(remote1))
check("[1] 远程半边: cancellable=False（平台事实：聚鑫侧无法取消）",
      remote1.get("cancellable") is False, str(remote1))
check("[1] 远程半边: 有已跑时长与预计剩余",
      isinstance(remote1.get("elapsed_seconds"), int) and isinstance(remote1.get("eta_seconds"), int)
      and remote1.get("eta_seconds") > 0, str(remote1))
msg1 = str(remote1.get("message") or "")
for frag in ("本地已终止", "无法取消", "预计还需", "作废丢弃"):
    check(f"[1] 回执人话含「{frag}」", frag in msg1, msg1)
snap1 = (task_of(D1).get("remote_wait") or {})
check("[1] 任务.json 落 remote_wait 快照（详情页刷新后同一句话）",
      snap1.get("running") is True and str(snap1.get("source")) in ("bridge_live", "snapshot"), str(snap1))
reset_slot_cache()
view1 = hub_api.get_task(TASK).get("remote") or {}
check("[1] 详情读面 remote.running=True 且文案同一口径",
      view1.get("running") is True and "作废丢弃" in str(view1.get("message") or ""), str(view1))

# ================================================================ [2] 立即重试：拒绝 + 说清等多久
print("\n[2] 立即重试 → 409 人话拒绝（在等什么、预计还需多久），零派发零迁移")
spawned: list[str] = []
hub_api._spawn_discuss = lambda task_dir, first_line: (spawned.append(Path(task_dir).name),
                                                       {"job_id": "probe-spawn"})[1]
JOBS_BEFORE = len(hub_api.HUB_JOBS)
retry_err = None
try:
    hub_api.start_retry(TASK)
except hub_api.HubError as exc:
    retry_err = exc
print(f"    拒绝原文：{retry_err}")
check("[2] 重试被拒（HubError）", retry_err is not None)
check("[2] 状态码 409", getattr(retry_err, "status", None) == 409, str(getattr(retry_err, "status", None)))
msg2 = str(retry_err or "")
for frag in ("上一轮远程拆解仍在跑", "预计还需", "无法取消", "本次没有提交"):
    check(f"[2] 拒绝人话含「{frag}」", frag in msg2, msg2)
extra2 = getattr(retry_err, "extra", {}) or {}
check("[2] 结构化自述带 retry_after_seconds（界面可倒计时）",
      isinstance(extra2.get("retry_after_seconds"), int) and extra2["retry_after_seconds"] > 0,
      str(extra2))
check("[2] 结构化自述带 slot 真相", isinstance(extra2.get("slot"), dict)
      and bool((extra2.get("slot") or {}).get("busy")), str(extra2.get("slot")))
check("[2] 零派发（没有起第二个执行体）", spawned == [], str(spawned))
check("[2] 零迁移（任务仍是 failed）", task_of(D1).get("state") == "failed",
      str(task_of(D1).get("state")))
check("[2] HUB_JOBS 不增记录", len(hub_api.HUB_JOBS) == JOBS_BEFORE,
      f"{JOBS_BEFORE} -> {len(hub_api.HUB_JOBS)}")

# ================================================================ [3] 作废栅栏：晚到结果不覆盖
print("\n[3] 作废栅栏：已终止 run 晚到的失败/结果写盘被拒，人工失败原因不被覆盖")
manual_reason = str(task_of(D1).get("failed_reason") or "")
late = state.write_failed(D1, reason="桥侧拆解失败（原文）: 上一轮 run 的结果", source="bridge_exit",
                          run_id="probe-run-1", probe={"reason_source": "bridge_exit"})
check("[3] 晚到失败写盘被拒（changed=False）", late.get("changed") is False, str(late))
check("[3] 人工失败原因原样保留", str(task_of(D1).get("failed_reason") or "") == manual_reason,
      str(task_of(D1).get("failed_reason")))
late_transition = None
try:
    state.transition(D1, "awaiting_assets", note="晚到的远程结果想推进状态")
except state.StateError as exc:
    late_transition = exc
check("[3] 晚到结果无法把 failed 推进到 awaiting_assets", late_transition is not None,
      str(task_of(D1).get("state")))

# ================================================================ [4] discuss 侧排队说人话
print("\n[4] discuss 排队循环：说「在等谁的远程返回 / 预计还需多久」，不再刷「第 N 次失败」")
stage_lines: list[str] = []
task4 = state.load(D1)
task4["state"] = "discussing"
old_max, old_interval = discuss.BUSY_RETRY_MAX, discuss.BUSY_RETRY_INTERVAL
discuss.BUSY_RETRY_MAX, discuss.BUSY_RETRY_INTERVAL = 2, 0.01
busy_err = None
try:
    discuss.start_and_wait(task4, on_stage=stage_lines.append)
except discuss.SlotBusy as exc:
    busy_err = exc
finally:
    discuss.BUSY_RETRY_MAX, discuss.BUSY_RETRY_INTERVAL = old_max, old_interval
print(f"    日志行：{stage_lines[:1]}；终态：{busy_err}")
check("[4] 排队到上限 → SlotBusy（环境级，不是任务失败）", busy_err is not None)
check("[4] 排队日志首行就是人话（含已跑/预计还需）",
      bool(stage_lines) and "远程拆解仍在跑" in stage_lines[0] and "预计还需" in stage_lines[0],
      str(stage_lines[:1]))
check("[4] 排队日志不再出现「排队重试（1/60」这类失败计数",
      all("排队重试" not in line for line in stage_lines), str(stage_lines[:2]))
check("[4] 到达上限的终态原因说清「本次未提交」且不谎报远程失败",
      "本次未提交" in str(busy_err) and "没有被远程判失败" in str(busy_err), str(busy_err))

BRIDGE.busy = True
BRIDGE.holder_task = "auto-9999"          # 别任务占槽（槽忙，但持有者不是本任务）
stage_lines2: list[str] = []
discuss.BUSY_RETRY_MAX, discuss.BUSY_RETRY_INTERVAL = 1, 0.01
try:
    discuss.start_and_wait(task4, on_stage=stage_lines2.append)
except discuss.SlotBusy:
    pass
finally:
    discuss.BUSY_RETRY_MAX, discuss.BUSY_RETRY_INTERVAL = old_max, old_interval
check("[4] 别任务占槽：文案指名对方任务号（不说成「本任务上一轮」）",
      bool(stage_lines2) and "auto-9999" in stage_lines2[0] and "上一轮" not in stage_lines2[0],
      str(stage_lines2[:1]))

BRIDGE.busy = True
BRIDGE.holder_task = BRIDGE.bridge_id
BRIDGE.holder_elapsed = 900.0              # 超过典型时长：预计剩余走下限，不谎称「马上好」
lines3 = remote_wait.busy_line({"busy": True, "holder": {"task_id": BRIDGE.bridge_id,
                                                        "elapsed_seconds": 900}},
                               bridge_id=BRIDGE.bridge_id)
check("[4] 超过典型时长仍有明确预计（约 1 分钟，不说「马上」）",
      "约 1 分钟" in lines3 and "马上" not in lines3, lines3)

# ================================================================ [5] 槽释放 → 重试成功
print("\n[5] 远程返回、槽释放 → 重试照旧成功（前置引导不误拦）")
BRIDGE.busy = False
reset_slot_cache()
spawned.clear()
out5 = hub_api.start_retry(TASK)
print(f"    重试回执：{out5}")
check("[5] 重试放行并派发执行体", spawned == [TASK], str(spawned))
check("[5] 回执 state=discussing + 心跳重置",
      out5.get("state") == "discussing" and out5.get("heartbeat_reset") is True, str(out5))
# 状态迁移归执行体（run_discuss 里 failed→discussing）：本探针的派发替身不起子进程，
# 所以磁盘必须**原样不动**——这也是合同（server 不越权替执行体写状态）。
check("[5] 状态迁移归执行体：替身不动盘，磁盘仍 failed",
      task_of(D1).get("state") == "failed", str(task_of(D1).get("state")))

# 产物优先路径：产物齐时即使槽忙也必须放行（重试不占槽，run_discuss 直接迁 awaiting_assets）
back = state.load(D1)
back["state"] = "failed"
back["failed_reason"] = manual_reason
state.save(D1, back)
BRIDGE.busy = True
BRIDGE.holder_task = "auto-9999"
BRIDGE.products = {"has_disc": True, "has_check": True, "has_prompt": True}
reset_slot_cache()
spawned.clear()
out6 = hub_api.start_retry(TASK)
check("[5] 产物优先：产物齐时槽忙也放行（重试不占槽）", spawned == [TASK], str(spawned))

# ================================================================ [8] 桥端槽真相面（真 server 代码）
print("\n[8] 桥端 /api/lapian/slot 与 409 自述同源（直接跑 ui/server.py 的这段代码，不起服务）")
import server as bridge_server                       # noqa: E402  —— 真桥代码：本段就是它的单元面
bridge_server.JOBS_LOCK.acquire()
bridge_server.JOBS["probe-slot-job"] = {"status": "running", "started": time.time() - 90,
                                       "task_id": "probe-slot-task"}
bridge_server.JOBS_LOCK.release()
bridge_server.ANALYZE_BUSY.acquire()
try:
    slot8 = bridge_server.lapian_slot_view()
    check("[8] 槽忙时能点名持有者（task_id + 已跑时长）",
          slot8.get("busy") is True
          and (slot8.get("holder") or {}).get("task_id") == "probe-slot-task"
          and 80 <= int((slot8.get("holder") or {}).get("elapsed_seconds") or 0) <= 100, str(slot8))
    check("[8] 槽真相带 checked_at（界面可显示读数时间）",
          bool(str(slot8.get("checked_at") or "")), str(slot8))
    api_err = None
    try:
        bridge_server.start_analyze_job("probe.mp4", "", "")
    except bridge_server.ApiError as exc:
        api_err = exc
    check("[8] 槽被占时 409 且错误体自带同一份槽真相",
          api_err is not None and getattr(api_err, "status", None) == 409
          and ((getattr(api_err, "extra", {}) or {}).get("slot") or {}).get("busy") is True,
          str(getattr(api_err, "extra", {})))
finally:
    bridge_server.ANALYZE_BUSY.release()
    bridge_server.JOBS_LOCK.acquire()
    bridge_server.JOBS.pop("probe-slot-job", None)
    bridge_server.JOBS_LOCK.release()
slot8b = bridge_server.lapian_slot_view()
check("[8] 槽释放后自述 busy=false / holder=null（不残留假持有者）",
      slot8b.get("busy") is False and slot8b.get("holder") is None, str(slot8b))

# ================================================================ [6] 幂等 + 清理
print("\n[6] 幂等：已 failed 再强制结束 → already=true，远程半边照旧说人话")
BRIDGE.products = {"has_disc": False, "has_check": False, "has_prompt": False}
BRIDGE.busy = True
BRIDGE.holder_task = BRIDGE.bridge_id
reset_slot_cache()
task6 = state.load(D1)
task6["state"] = "failed"
task6["failed_reason"] = manual_reason
state.save(D1, task6)
out7 = hub_api.force_finish(TASK, confirm=True, confirm_name=TASK)
check("[6] already=true 且不重复写 history",
      out7.get("already") is True and out7.get("changed") is False, str(out7))
check("[6] 幂等回执仍带远程半边", (out7.get("remote") or {}).get("running") is True, str(out7.get("remote")))
check("[6] 失败原因未被覆盖", str(task_of(D1).get("failed_reason") or "") == manual_reason,
      str(task_of(D1).get("failed_reason")))

BRIDGE.close()
shutil.rmtree(TMP_ROOT, ignore_errors=True)
AFTER = {"projects": snapshot(REAL_PROJECTS), "analysis": snapshot(REAL_ANALYSIS),
         "uploads": snapshot(REAL_UPLOADS)}
check("[9] 真实 projects/analysis/uploads 顶层集合零变化", BEFORE == AFTER, f"{BEFORE} -> {AFTER}")

# ---------------------------------------------------------------- 汇总
PASSED = sum(1 for _, ok in RESULTS if ok)
FAILED = len(RESULTS) - PASSED
print(f"\n{'=' * 60}\n探针结果：{PASSED}/{len(RESULTS)} PASS，{FAILED} FAIL")
if FAILED:
    print("FAIL 明细：")
    for name, ok in RESULTS:
        if not ok:
            print(f"  - {name}")
    raise SystemExit(1)
print("全绿（exit 0）")
