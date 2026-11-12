# -*- coding: utf-8 -*-
"""AT-19 探针：执行体进程化 + 启动即对账（**真子进程、真 server 进程**，隔离根，自清理）。

跑法（wanrepro/ 下）：
    python automation/probe_at19_proc.py
退出码 0 = 全绿。

锁的是卡面三条验收（不是函数返回值）：
  [1] 子进程被 kill -9（Windows taskkill /F）→ 对账立即 failed，证据 = PID 已死 + 心跳
  [2] 子进程正常退出非 0（真执行体，exit=3）→ 启动对账/扫描 failed，证据 = 退出码 3
  [3] server 自身重启后接续对账 → 真起 ui/server.py，[OK] 后 ≤10s 落成 failed + 摘要行
  [4] 幂等：同一任务连扫三次只迁移一次（history 不增行、failed_at 不变）；无僵尸 running 态
  [5] 真重试：换 run 即作废旧退出码 → 新 run 死证 run_id=本次 exec_run_id（E1 可命中）；
      旧 run 迟到写仍被栅栏拒绝（t_9eab1c56 两条验收判据的自动化版本）

隔离手法：
  · WANREPRO_AUTO_DIR → 本次临时根（任务根唯一来源 automation.config.PROJECTS_DIR）；
  · WANREPRO_BRIDGE → 本探针自起的可编排假桥（子进程与 server 都连它，走真 HTTP）；
  · 夹具一律 auto-98xx、桥 id 一律 probe-at19-proc-*；跑完整个临时根删除。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]              # wanrepro/
TMP_ROOT = Path(tempfile.mkdtemp(prefix="probe-at19-proc-"))
PROJECTS = TMP_ROOT / "projects"
PROJECTS.mkdir(parents=True, exist_ok=True)

RESULTS: list[tuple[str, bool]] = []
CHILDREN: list[subprocess.Popen] = []


def check(name: str, cond, detail: str = "") -> bool:
    ok = bool(cond)
    RESULTS.append((name, ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail and not ok else ""))
    return ok


# ---------------------------------------------------------------- 可编排假桥

class ProbeBridge:
    """进程内假 bridge：任务列表（产物全缺）+ lapian/run + job 永远 running。"""

    def __init__(self, bridge_id: str) -> None:
        self.bridge_id = bridge_id
        self.job_status = "running"      # [5] 重试轮用 "error" 让第二轮快速落任务级失败
        self.job_error = "probe：任务级失败（云端 429 限流）"
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

            def do_POST(self):                       # noqa: N802
                if self.path == "/api/lapian/run":
                    n = int(self.headers.get("Content-Length") or 0)
                    if n:
                        self.rfile.read(n)
                    self._send(200, {"job": "0badc0de"})
                    return
                self._send(404, {"error": "probe bridge 不认这个 POST"})

            def do_GET(self):                        # noqa: N802
                if self.path == "/api/tasks":
                    self._send(200, {"tasks": [{"id": outer.bridge_id, "has_disc": False,
                                                "has_check": False, "has_prompt": False}]})
                    return
                if self.path.startswith("/api/job/"):
                    if outer.job_status == "error":
                        self._send(200, {"status": "error", "elapsed": 2,
                                         "error": {"message": outer.job_error}})
                        return
                    self._send(200, {"status": "running", "elapsed": 2, "stage": "等待远程"})
                    return
                self._send(404, {"error": "probe bridge 不认这个 GET"})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = int(self.httpd.server_address[1])
        threading.Thread(target=self.httpd.serve_forever, daemon=True,
                         name="probe-at19-proc-bridge").start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"


BRIDGE = ProbeBridge("probe-at19-proc-0001")
os.environ["WANREPRO_AUTO_DIR"] = str(PROJECTS)
os.environ["WANREPRO_BRIDGE"] = BRIDGE.base
os.environ["WANREPRO_WATCHDOG"] = "0"            # 本进程内不起 watchdog（隔离）
os.environ["WANREPRO_BOOT_RECONCILE"] = "0"     # 场景 1/2 手动 scan；场景 3 另起 server 开

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ui"))
import hub_api  # noqa: E402
from automation import state  # noqa: E402


def seed(local_id: str, st: str = "discussing") -> Path:
    d = PROJECTS / local_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "任务.json").write_text(json.dumps({
        "local_id": local_id, "state": st, "history": [], "bridge_id": BRIDGE.bridge_id,
        "intent": "探针：拆解测试", "ingestion": {"video_name": ""},
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return d


def task_of(d: Path) -> dict:
    return json.loads((d / "任务.json").read_text(encoding="utf-8"))


def wait_for(pred, timeout: float, step: float = 0.2):
    end = time.time() + timeout
    while time.time() < end:
        try:
            value = pred()
        except (OSError, json.JSONDecodeError, KeyError):
            value = None
        if value:
            return value
        time.sleep(step)
    return None


def kill_pid(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
    else:
        os.kill(pid, 9)


# ================================================================ [1] 子进程被 kill
print("\n[1] 子进程被 kill -9（taskkill /F）→ 一次扫描内 failed，证据=PID 已死 + 心跳")
D1 = seed("auto-9801")
job1 = hub_api.start_discuss("auto-9801")
print(f"    派发：job={job1.get('job_id')} pid={job1.get('pid')} mode={job1.get('mode')}")
check("[1] 派发形态=独立子进程（有 pid、mode=process）",
      bool(job1.get("pid")) and job1.get("mode") == "process", str(job1))
hb1 = wait_for(lambda: (lambda h: h if h.get("pid") and h.get("beat_count") else None)(
    state.heartbeat_read(D1)), 20)
check("[1] 子进程接管：心跳有 pid 且 beat_count≥1",
      bool(hb1) and hb1.get("exec") == "child", str(hb1))
pid1 = int((hb1 or {}).get("pid") or 0)
check("[1] 任务.json 落 exec_run_id 令牌（X2）",
      str(task_of(D1).get("exec_run_id") or "") == str(job1.get("job_id")),
      str(task_of(D1).get("exec_run_id")))
kill_pid(pid1)
wait_for(lambda: state.pid_alive(pid1) is False, 10)
check("[1] taskkill 后 PID 实测已死", state.pid_alive(pid1) is False, str(state.pid_alive(pid1)))
scan1 = hub_api.scan_lost_runs(apply=True)
t1 = task_of(D1)
ev1 = t1.get("failed_evidence") or {}
print(f"    扫描：scanned={scan1.get('scanned')} lost={[i['local_id'] for i in scan1.get('lost') or []]} "
      f"applied_count={scan1.get('applied_count')}")
check("[1] 任务落成 failed", t1.get("state") == "failed", t1.get("state"))
check("[1] reason_code=exec_pid_gone（E2，不等 900s）",
      ev1.get("reason_code") == "exec_pid_gone", str(ev1.get("reason_code")))
check("[1] failed_evidence.trigger=exec_dead",
      ev1.get("trigger") == "exec_dead", str(ev1.get("trigger")))
check("[1] 证据齐：pid / 心跳时刻 / beat_count",
      (ev1.get("exec") or {}).get("pid") == pid1
      and bool((ev1.get("exec") or {}).get("heartbeat_at"))
      and int((ev1.get("exec") or {}).get("beat_count") or 0) >= 1,
      str(ev1.get("exec")))
check("[1] 只迁移一次（history 里只有一条 →failed）",
      sum(1 for h in t1.get("history") or [] if h.get("to") == "failed") == 1,
      str(t1.get("history")))
print(f"    failed_reason: {t1.get('failed_reason')!r}")

# ================================================================ [2] 子进程正常退出非 0
print("\n[2] 子进程正常退出非 0（真执行体 exit=3，未写任务态）→ 扫描 failed，证据=退出码 3")
D2 = seed("auto-9802", st="rendering")
job2 = hub_api._spawn_process(
    D2, kind="hub-discuss",
    argv_builder=lambda rid: hub_api._discuss_argv(rid, "auto-9802"))
ex2 = wait_for(lambda: (lambda e: e if e.get("exists") else None)(state.exec_read(D2)), 30)
print(f"    退出码文件：{ex2}")
check("[2] 执行体收尾写了退出码文件", bool(ex2), str(ex2))
check("[2] exit_code=3 / outcome=failed",
      (ex2 or {}).get("exit_code") == 3 and (ex2 or {}).get("outcome") == "failed", str(ex2))
check("[2] 执行体没写任务态（仍是 rendering，死证归对账）",
      task_of(D2).get("state") == "rendering", task_of(D2).get("state"))
scan2 = hub_api.scan_lost_runs(apply=True)
t2 = task_of(D2)
ev2 = t2.get("failed_evidence") or {}
check("[2] 任务落成 failed", t2.get("state") == "failed", t2.get("state"))
check("[2] reason_code=exec_exit_written（E1）",
      ev2.get("reason_code") == "exec_exit_written", str(ev2.get("reason_code")))
check("[2] 证据含退出码 3",
      (ev2.get("exec") or {}).get("exit_code") == 3, str(ev2.get("exec")))
check("[2] 只迁移一次", sum(1 for h in t2.get("history") or [] if h.get("to") == "failed") == 1,
      str(t2.get("history")))
# 执行体自己的 stdout 合流到 拆解子进程日志.txt（父进程不持有执行体，日志是唯一现场）
child_log = D2 / state.CHILD_LOG_FILE
check("[2] 子进程日志文件已落盘（父进程不持有执行体）", child_log.is_file(),
      str(child_log))

# ================================================================ [3] server 重启后接续对账
print("\n[3] server 自身重启后接续对账（真起 ui/server.py，[OK] 后 ≤10s 落成 failed）")
D3 = seed("auto-9803")
job3 = hub_api.start_discuss("auto-9803")
hb3 = wait_for(lambda: (lambda h: h if h.get("pid") and h.get("beat_count") else None)(
    state.heartbeat_read(D3)), 20)
pid3 = int((hb3 or {}).get("pid") or 0)
kill_pid(pid3)
wait_for(lambda: state.pid_alive(pid3) is False, 10)
check("[3] 夹具：执行体已死、任务仍是 discussing（= 重启前现场）",
      state.pid_alive(pid3) is False and task_of(D3).get("state") == "discussing",
      f"pid_alive={state.pid_alive(pid3)} state={task_of(D3).get('state')}")

sock = socket.socket()
sock.bind(("127.0.0.1", 0))
port = sock.getsockname()[1]
sock.close()
server_log = TMP_ROOT / "server.log"
env = {**os.environ, "WANREPRO_BOOT_RECONCILE": "1", "WANREPRO_WATCHDOG": "0"}
server = subprocess.Popen([sys.executable, "ui/server.py", "--port", str(port),
                           "--no-browser", "--no-watchdog"],
                          cwd=str(ROOT), env=env, stdout=server_log.open("ab"),
                          stderr=subprocess.STDOUT)
CHILDREN.append(server)
landed = wait_for(lambda: task_of(D3).get("state") == "failed", 20)
t3 = task_of(D3)
ev3 = t3.get("failed_evidence") or {}
check("[3] 启动对账在 20s 内把僵尸任务落成 failed", bool(landed), t3.get("state"))
check("[3] reason_code=exec_pid_gone", ev3.get("reason_code") == "exec_pid_gone",
      str(ev3.get("reason_code")))
time.sleep(1.0)
log_text = server_log.read_text(encoding="utf-8", errors="replace") if server_log.is_file() else ""
check("[3] server 打印了 [OK] 与「启动即对账」",
      "[OK]" in log_text and "启动即对账" in log_text, log_text[-400:])
check("[3] server 打印了启动对账完成摘要行（含 scanned/exec_unknown/errors）",
      "启动对账：done" in log_text, log_text[-400:])
server.terminate()
try:
    server.wait(timeout=10)
except subprocess.TimeoutExpired:
    server.kill()

# ================================================================ [4] 幂等 + 无僵尸
print("\n[4] 幂等：连扫三次只迁移一次；无僵尸 running 态")
hist_before = len(t3.get("history") or [])
again = []
for _ in range(3):
    again.append(hub_api.scan_lost_runs(apply=True))
t3b = task_of(D3)
check("[4] 后三次扫描没有再次迁移（history 不增、failed_at 不变）",
      len(t3b.get("history") or []) == hist_before and t3b.get("failed_at") == t3.get("failed_at"),
      f"{len(t3b.get('history') or [])} vs {hist_before}")
check("[4] 已降级任务进 skipped{not_running}（可观测的幂等形态）",
      all(any(s.get("local_id") == "auto-9803" and s.get("reason_code") == "not_running"
              for s in r.get("skipped") or []) for r in again),
      str([r.get("skipped") for r in again]))
zombies = []
for d in sorted(PROJECTS.glob("auto-*/任务.json")):
    task = json.loads(d.read_text(encoding="utf-8"))
    hb = state.heartbeat_read(d.parent)
    if task.get("state") in ("discussing", "rendering") and not state.pid_alive(
            int(hb.get("pid") or 0)):
        zombies.append(d.parent.name)
check("[4] 无僵尸 running 态（运行态但执行体已死）", not zombies, str(zombies))
check("[4] 三个夹具全部 failed",
      all(task_of(d).get("state") == "failed" for d in (D1, D2, D3)),
      str({d.name: task_of(d).get("state") for d in (D1, D2, D3)}))

# ================================================================ [5] 真重试：凭证轮转
print("\n[5] 真重试（failed → POST task/retry）：换 run 即作废旧退出码；新死证 run_id=本次 exec_run_id")
D5 = seed("auto-9804", st="rendering")
job5a = hub_api.start_discuss("auto-9804")
ex5a = wait_for(lambda: (lambda e: e if e.get("exists") else None)(state.exec_read(D5)), 40)
print(f"    第一轮：job={job5a.get('job_id')} 退出码={ex5a}")
check("[5] 第一轮执行体收尾写了退出码（run_id=第一轮 job）",
      bool(ex5a) and ex5a.get("run_id") == str(job5a.get("job_id")), str(ex5a))
hub_api.scan_lost_runs(apply=True)
check("[5] 第一轮对账落 failed（重试前置状态）",
      task_of(D5).get("state") == "failed", task_of(D5).get("state"))
check("[5] 重试前任务目录挂着第一轮死证（缺陷现场）",
      state.exec_read(D5).get("run_id") == str(job5a.get("job_id")), str(state.exec_read(D5)))
# 第一轮子进程写完退出码后，reaper 线程才把 HUB_JOBS 记录落终态。这中间的重试守卫仍会按
# 「60s 启动宽限期」把这条 running 记录判活（产品语义：刚起的 job 一律拦），探针不等这一步
# 就会随机红在「任务已有拆解任务在跑」上（2026-11-12 实测 1/3 概率）。等 reaper 收尾，
# 顺带把「终态出口真的闭环」变成一条断言，而不是把守卫放松。
final5a = wait_for(lambda: (lambda r: r if r.get("status") != "running" else None)(
    hub_api.HUB_JOBS.get(str(job5a.get("job_id"))) or {}), 30) or {}
check("[5] 第一轮 job 已落终态（reaper 闭环；此后重试不受启动宽限期影响）",
      final5a.get("status") in ("failed", "done", "force_finished"), str(final5a))
BRIDGE.job_status = "error"          # 第二轮快速落任务级失败（不等 1800s 轮询上限）
retry5 = hub_api.start_retry("auto-9804")
job5b = str(retry5.get("job_id") or "")
check("[5] 重试起了新 run（job 与第一轮不同）",
      bool(job5b) and job5b != str(job5a.get("job_id")), str(retry5))
rot5 = state.exec_read(D5)
check("[5] 重试 T0 即作废旧死证（不再挂第一轮的 exit）",
      (not rot5.get("exists")) or rot5.get("run_id") == job5b, str(rot5))
check("[5] 任务.json.exec_run_id 已换成新 run",
      str(task_of(D5).get("exec_run_id") or "") == job5b,
      str(task_of(D5).get("exec_run_id")))
ex5b = wait_for(lambda: (lambda e: e if e.get("exists") else None)(state.exec_read(D5)), 60)
rec5b = wait_for(lambda: (lambda r: r if r.get("status") in ("done", "failed") else None)(
    hub_api.HUB_JOBS.get(job5b) or {}), 30) or {}
print(f"    第二轮：job={job5b} 退出码={ex5b} job 记录 exit_code={rec5b.get('exit_code')}")
check("[5] 新 run 死证 run_id == 本次 exec_run_id（判据 1）",
      bool(ex5b) and ex5b.get("run_id") == job5b
      and str(task_of(D5).get("exec_run_id")) == ex5b.get("run_id"), str(ex5b))
check("[5] 新 run exit_code 与 job 记录一致（判据 1）",
      ex5b.get("exit_code") is not None
      and ex5b.get("exit_code") == rec5b.get("exit_code"),
      f"exit={ex5b.get('exit_code')} job={rec5b.get('exit_code')}")
old5 = str(job5a.get("job_id") or "")
state.exec_write(D5, run_id=old5, pid=int(job5a.get("pid") or 0), exit_code=3,
                 outcome="failed", error_tail="late-write-from-old-run")
after5 = state.exec_read(D5)
check("[5] 旧 run 迟到写被拒：文件仍是新 run 的死证（判据 2）",
      after5.get("run_id") == job5b and after5.get("error_tail") != "late-write-from-old-run",
      str(after5))
view5 = state.exec_snapshot(D5)                 # 真跑完后的快照（心跳 + 退出码 + 所有者令牌）
dead5, code5 = state.judge_exec_dead({"state": "discussing"}, view5)
print(f"    重试后快照：exit_run={view5.get('exit_run_id')} owner={view5.get('task_exec_run_id')} "
      f"→ {code5}")
check("[5] 重试后 E1 可命中（exec_exit_written，非退化到 E2）",
      dead5 and code5 == "exec_exit_written"
      and str(view5.get("exit_run_id") or "") == job5b
      and str(view5.get("task_exec_run_id") or "") == job5b, f"{code5} / {view5}")
BRIDGE.job_status = "running"

# ================================================================ 汇总
total = len(RESULTS)
passed = sum(1 for _, ok in RESULTS if ok)
print(f"\n共 {total} 项断言：PASS {passed} / FAIL {total - passed}")
print(f"隔离根：{TMP_ROOT}")
print(f"server 日志片段（最后 12 行）：")
for line in (server_log.read_text(encoding="utf-8", errors="replace").splitlines()[-12:]
             if server_log.is_file() else []):
    print("    " + line)

for proc in CHILDREN:
    if proc.poll() is None:
        proc.kill()
shutil.rmtree(TMP_ROOT, ignore_errors=True)
raise SystemExit(0 if passed == total else 1)
