#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AT-16 B4a 探针：拆解进度**双锚持久化**（写侧 + 读侧，自起隔离实例，**执行体跑真子进程**）。

跑法：wanrepro/ 下 `python automation/probe_at16_b4a.py`。

背景缺陷：拆解远程等待段进度假冻结——`lapian_stage` 是静态阶段字符串，UI 上的「已 Xs」
是日志行里的 job 总耗时快照，10 分钟不动。本探针锁**锚**，不锁像素（前端走表由
web/scripts/probe-at16-b4a.mjs 用可控时钟验）。

**AT-19 之后为什么必须改造**：执行体不再是 server 进程内的线程，而是独立子进程
（automation/discuss_runner.py，由 ui/hub_api.py:_spawn_process 拉起）。旧版探针靠
`hub_api.discuss.run_discuss = fake_run`（进程内 monkeypatch）与在探针进程内调小
POLL_INTERVAL 来驱动流程——这两类补丁对真正的执行体**恒等于没设**，于是旧版在
42e3c11 上只剩 17/23（假执行体从没跑过，子进程按真代码打桥 → 404 → 环境级路径）。

改造后**一处补丁都不打**（这是本探针的核心证据，不是手段）：
  · 夹具桥（本机 HTTP）按**轮询次数**编排出「阶段A → 阶段B → done / error」，执行体按真
    代码轮询，阶段变化由桥给，双锚由 hub_api._stage_recorder 真写；
  · 加速只走环境变量 `WANREPRO_POLL_INTERVAL`（由 _spawn_process 随 Popen 透传子进程；
    默认值就是 discuss.POLL_INTERVAL=3s，不设即生产节奏）；
  · 写路径全经隔离实例的 HTTP 入口（`POST /api/hub/discuss/<id>`、
    `POST /api/hub/task/<id>/retry`），读路径 get_task / 列表 / 详情三面同读一份磁盘。

锁的点（原 23 项断言编号与判据逐条保留；末项为本卡新增，证明「执行体确为独立子进程」）：

  [0] 隔离与同源：自起 ui/server.py（临时 WANREPRO_AUTO_DIR + 临时 WANREPRO_ANALYSIS_ROOT
      + 假 bridge），/api/hub/where 自述 TASKS_DIR == 探针根，不一致拒跑（防误写/误删真实任务）。
  [1] 首拆双锚：起手落 lapian_started_at / lapian_stage_at（同一时刻起步），且 **ended_at 为空**
      （运行中不停表）；阶段变化只重置 stage_at，started_at 不动（总耗时不随阶段归零）；
      两次阶段之间的等待窗里 ended_at 仍为空。
  [2] 终态停表依据：job 成功 → lapian_ended_at 落值；失败 → 同样落值（成功/失败分别可停表）。
  [3] 读侧透出：get_task / GET /api/hub/tasks / GET /api/hub/task/<id> 的 lapian 三值与
      任务.json 完全一致（列表与详情同一份数据源）。
  [4] 重拆重置：failed → 重试起新 job，started_at/stage_at 按新 job 重置、上一轮 ended_at 作废
      （否则新 job 一开就停表）。
  [5] 缺锚诚实：旧任务（有 lapian_stage 字符串、created_at、日志 [HH:mm:ss] 行、行内「已 600s」，
      但**没有**锚字段）→ lapian 三值恒为空串。**禁止**用 created_at / 日志首行 / 「已 Xs」凑锚。
  [6] 透传合同：lapian_stage 被写成对象（异构历史值）→ 摘要/详情降级为空串，绝不把对象 repr
      漏给前端（前端 hubText() 同口径）。
  [7] 真实数据根零写入：automation/projects、analysis/reverse、uploads 跑前跑后指纹一致。

夹具任务只用 auto-9701..auto-9704（隔离根内，跑完连根删）；真实任务零写入，跑前跑后对账。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]          # wanrepro/
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ui"))                 # hub_api.py 在 ui/ 下（与 probe_hub 同法）

TASK_RUN = "auto-9701"      # 首拆 → 阶段 → 终态 → 重拆
TASK_LEGACY = "auto-9702"   # 旧任务：无锚字段
TASK_ODD = "auto-9703"      # 异构 lapian_stage（对象）
TASK_FAIL = "auto-9704"     # 失败终态
DISCUSS_LOG = "拆解日志.txt"

RUN_BRIDGE = "probe-b4a-run"
FAIL_BRIDGE = "probe-b4a-fail"
FAIL_TEXT = "探针假错误：桥侧拆解失败"

STAGE_A_POLLS = 8           # 轮询 1..8 = 阶段A（0.2s 间隔 → 1.6s，跨秒必 ≥1 秒）
DONE_POLL = 31              # 轮询 31 起 done（≈6.2s；阶段B 等待窗 ≈4.4s）
ERROR_POLL = 10             # 失败夹具：第 10 轮回 error

RESULTS: list[tuple[str, bool]] = []


def check(name: str, cond, detail: str = "") -> bool:
    ok = bool(cond)
    RESULTS.append((name, ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail and not ok else ""))
    return ok


def summary() -> int:
    fails = [n for n, ok in RESULTS if not ok]
    print(f"\n[probe-at16-b4a] {len(RESULTS) - len(fails)}/{len(RESULTS)} PASS")
    for n in fails:
        print(f"  FAIL {n}")
    return 1 if fails else 0


# ── 夹具桥：按轮询次数编排阶段/终态（真执行体照真代码轮询）──────────────────
class B4aBridge:
    """只实现拆解要用的两个面：lapian/run、job 轮询，外加产物标志 /api/tasks。

    产物标志**故意**在 done 之前为假：run_discuss 起手会先查产物（产物优先），
    若一开始就齐，它会跳过整个等待循环，双锚/停表就无从验证。新 job 起手即回假
    （真桥语义：新一次拆解开始，has_* 还不存在）。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, dict] = {}
        self._done: dict[str, bool] = {}
        self._seq = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):        # 不污染探针输出
                return

            def _send(self, code: int, payload: dict) -> None:
                raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self) -> None:        # noqa: N802
                if self.path != "/api/lapian/run":
                    self._send(404, {"error": f"stub bridge 不认 {self.path}"})
                    return
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                try:
                    bridge_id = str(json.loads(raw or b"{}").get("id") or "")
                except (json.JSONDecodeError, ValueError):
                    bridge_id = ""
                with outer._lock:
                    outer._seq += 1
                    job = f"b4a{outer._seq:04d}"
                    outer._jobs[job] = {"bridge_id": bridge_id, "polls": 0}
                    outer._done[bridge_id] = False       # 新 job 起手：产物标志回假
                self._send(200, {"job": job})

            def do_GET(self) -> None:         # noqa: N802
                if self.path == "/api/tasks":
                    with outer._lock:
                        rows = [{"id": bid, "has_disc": ok, "has_check": ok, "has_prompt": ok}
                                for bid, ok in outer._done.items()]
                    self._send(200, {"tasks": rows})
                    return
                if self.path.startswith("/api/job/"):
                    job = self.path.rsplit("/", 1)[-1]
                    with outer._lock:
                        rec = outer._jobs.get(job)
                        if rec is None:
                            self._send(404, {"error": f"stub bridge 不认 job {job}"})
                            return
                        rec["polls"] += 1
                        n = rec["polls"]
                        bid = rec["bridge_id"]
                        elapsed = round(n * 0.2, 1)
                        if bid == FAIL_BRIDGE and n >= ERROR_POLL:
                            self._send(200, {"status": "error", "error": FAIL_TEXT})
                            return
                        if bid != FAIL_BRIDGE and n >= DONE_POLL:
                            outer._done[bid] = True
                            self._send(200, {"status": "done", "elapsed": elapsed, "stage": "完成"})
                            return
                        stage = ("阶段A：提交拆解" if n <= STAGE_A_POLLS
                                 else "阶段B：远程等待")
                        self._send(200, {"status": "running", "stage": stage, "elapsed": elapsed})
                    return
                self._send(404, {"error": f"stub bridge 不认 {self.path}"})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = int(self.httpd.server_address[1])
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True,
                                       name="probe-b4a-bridge")

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


# ── 夹具与读盘 ────────────────────────────────────────────────────────
def make_task(local_id: str, state_name: str, bridge_id: str, **extra) -> Path:
    """隔离根内的任务夹具（与其它探针同形，跑完连根删）。"""
    from automation import config

    tdir = Path(config.PROJECTS_DIR) / local_id
    shutil.rmtree(tdir, ignore_errors=True)
    (tdir / "inbox").mkdir(parents=True, exist_ok=True)
    task = {
        "local_id": local_id, "bridge_id": bridge_id,
        "created_at": "2026-01-01T00:00:00+08:00", "state": state_name,
        "history": [{"at": "2026-01-01T00:00:00+08:00", "from": None, "to": state_name,
                     "note": "probe-at16-b4a 夹具"}],
        "intent": "AT-16 B4a 探针夹具", "assets": [],
        "ingestion": {"zip_sha256": f"probe-{local_id}", "zip_name": "probe",
                      "video_name": "", "text_name": "", "ignored": []},
    }
    task.update(extra)
    (tdir / "任务.json").write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
    return tdir


def read_task(tdir: Path) -> dict:
    try:
        return json.loads((tdir / "任务.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def fingerprint(root: Path) -> dict[str, tuple[int, int]]:
    """目录指纹（相对路径 → (字节数, mtime_ns)）：跑前跑后一致 = 零写入。"""
    out: dict[str, tuple[int, int]] = {}
    if not root.is_dir():
        return out
    for p in sorted(root.rglob("*")):
        if p.is_symlink() or not p.is_file():
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        out[str(p.relative_to(root))] = (st.st_size, st.st_mtime_ns)
    return out


def wait_terminal(tdir: Path, timeout: float = 45.0,
                  marks: list[tuple[str, dict]] | None = None,
                  mark_prefix: str = "") -> dict:
    """轮询任务文件到终态（子进程真写），顺手按阶段前缀抓快照 + 等待窗样本。"""
    deadline = time.time() + timeout
    mid_window = None
    seen_B = False
    while time.time() < deadline:
        task = read_task(tdir)
        stage_now = str(task.get("lapian_stage") or "")
        if marks is not None and mark_prefix and stage_now.startswith(mark_prefix) and not marks:
            marks.append((mark_prefix, dict(task)))
        if stage_now.startswith("阶段B"):
            if not seen_B:
                seen_B = True
                if marks is not None:
                    marks.append(("B", dict(task)))
            elif task.get("lapian_ended_at") == "" and mid_window is None:
                mid_window = dict(task)
        if stage_now.startswith("终态"):
            return {"task": task, "mid_window": mid_window}
        time.sleep(0.03)
    return {"task": read_task(tdir), "mid_window": mid_window}


def main() -> int:
    iso_root = Path(tempfile.mkdtemp(prefix="wanprobe-at16b4a-"))
    iso_projects = iso_root / "projects"
    iso_analysis = iso_root / "analysis" / "reverse"
    iso_projects.mkdir(parents=True, exist_ok=True)
    iso_analysis.mkdir(parents=True, exist_ok=True)

    stub = B4aBridge()
    stub.thread.start()

    # 隔离根走环境变量（父与子同源）：hub_api.TASKS_DIR / discuss._ANALYSIS_ROOT 都在 import
    # 期按 env 求值，所以必须先设 env 再 import。**不在探针进程内改模块属性**。
    os.environ["WANREPRO_AUTO_DIR"] = str(iso_projects)
    os.environ["WANREPRO_ANALYSIS_ROOT"] = str(iso_analysis)
    os.environ["WANREPRO_BRIDGE"] = stub.base
    os.environ["WANREPRO_WATCHDOG"] = "0"                 # 隔离：不起看门狗
    os.environ["WANREPRO_BOOT_RECONCILE"] = "0"          # 隔离：服务端启动不对账夹具

    from automation import config, probe_guard  # noqa: E402
    import hub_api  # noqa: E402  —— 只用于读侧 get_task（与 server 同一份磁盘/同一函数）
    from automation import state  # noqa: E402

    port = probe_guard.free_port()
    base = f"http://127.0.0.1:{port}"

    # 真实数据根（只读）：探针自己的 config.PROJECTS_DIR 已被 env 指到隔离根，所以这里显式取仓内
    real_projects = ROOT / "automation" / "projects"
    real_analysis = ROOT / "analysis" / "reverse"
    real_uploads = ROOT / "uploads"
    real_before = {name: fingerprint(p) for name, p in
                   (("projects", real_projects), ("analysis", real_analysis),
                    ("uploads", real_uploads))}

    proc = None
    try:
        # 服务端 env：隔离根 + 假桥 + 加速位（执行体子进程由 _spawn_process 透传继承）
        env = {**os.environ, "WANREPRO_POLL_INTERVAL": "0.2"}
        proc = subprocess.Popen(
            [sys.executable, str(ROOT / "ui" / "server.py"), "--port", str(port), "--no-browser"],
            cwd=str(ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace")
        where = None
        for _ in range(200):
            if proc.poll() is not None:
                break
            code, payload = probe_guard.http_req(base, "/api/hub/where", timeout=5)
            if code == 200 and isinstance(payload, dict):
                where = payload
                break
            time.sleep(0.3)
        try:
            probe_guard.assert_same_root(where or {}, iso_projects)
            check("[0] 隔离实例起来且读写同源（/api/hub/where == 探针根）", where is not None,
                  f"{base} 无响应" if where is None else str(where)[:120])
            print(f"     数据根 {where.get('tasks_dir')}")
        except probe_guard.GuardError as err:
            check("[0] 隔离实例起来且读写同源（/api/hub/where == 探针根）", False, str(err))
            print("[probe-at16-b4a] 拒跑：数据根不同源，后续写入/清理一律不执行。")
            return summary()

        # ── [1] 首拆双锚 + 阶段重置 + 运行中不停表 ─────────────
        print("1) 首拆双锚：起手两锚落盘；阶段变化只重置阶段锚，启动锚不动；运行中无终态锚")
        tdir_run = make_task(TASK_RUN, "ingested", RUN_BRIDGE)
        code, job = probe_guard.http_req(base, f"/api/hub/discuss/{TASK_RUN}", method="POST",
                                         timeout=20)
        # 起手快照：POST 返回时父进程已同步写完「重置双锚 + 首行阶段」，子进程此后才推进
        first = read_task(tdir_run)
        print(f"     起手阶段 {first.get('lapian_stage')!r}；job={job}")
        check("[1] 执行体确为独立子进程（hub 只派发：mode=process 且有 pid；"
              "旧版此处是进程内补丁执行体）",
              code == 200 and job.get("mode") == "process" and bool(job.get("pid")),
              f"{code} {job}")

        marks: list[tuple[str, dict]] = []
        run_info = wait_terminal(tdir_run, timeout=45, marks=marks, mark_prefix="阶段A")
        rec = probe_guard.http_req(base, f"/api/hub/job/{job.get('job_id')}", timeout=10)[1] or {}
        task_after = read_task(tdir_run)
        a_mark = dict(marks[0][1]) if marks else {}

        check("[1] 首拆起手即落启动锚（lapian_started_at 非空）",
              bool(first.get("lapian_started_at")),
              json.dumps(first, ensure_ascii=False)[:200])
        check("[1] 首拆起手即落阶段锚（lapian_stage_at == 启动锚，同一时刻起步）",
              bool(first.get("lapian_started_at"))
              and first.get("lapian_stage_at") == first.get("lapian_started_at"),
              f"started={first.get('lapian_started_at')} stage_at={first.get('lapian_stage_at')}")
        check("[1] 阶段变化重置阶段锚（stage_at 前进）",
              bool(a_mark) and (run_info["task"].get("lapian_stage_at") or "")
              > (a_mark.get("lapian_stage_at") or ""),
              f"A={a_mark.get('lapian_stage_at')} B={run_info['task'].get('lapian_stage_at')}")
        check("[1] 阶段变化**不**动启动锚（总耗时不随阶段归零）",
              bool(a_mark) and run_info["task"].get("lapian_started_at") == a_mark.get("lapian_started_at")
              == first.get("lapian_started_at"),
              f"起手={first.get('lapian_started_at')} A={a_mark.get('lapian_started_at')} "
              f"B={run_info['task'].get('lapian_started_at')}")
        check("[1] 运行中（阶段B 等待窗）终态锚恒空 —— 不停表",
              run_info["mid_window"] is not None
              and run_info["mid_window"].get("lapian_ended_at") == ""
              and str(run_info["mid_window"].get("lapian_stage") or "").startswith("阶段B"),
              json.dumps(run_info["mid_window"] or {}, ensure_ascii=False)[:200])
        print(f"     job 终态 {rec.get('status')} exit={rec.get('exit_code')}；"
              f"阶段A快照 stage_at={a_mark.get('lapian_stage_at')}")

        # ── [2] 终态停表依据 ──────────────────────────────────
        print("2) 终态停表依据：成功/失败分别落 lapian_ended_at")
        check("[2] 成功终态：ended_at 落值且 ≥ 阶段锚（停表依据可算）",
              bool(task_after.get("lapian_ended_at"))
              and task_after["lapian_ended_at"] >= (task_after.get("lapian_stage_at") or ""),
              f"stage_at={task_after.get('lapian_stage_at')} ended_at={task_after.get('lapian_ended_at')}")
        check("[2] 成功终态：阶段文案仍是纯字符串且为终态行",
              isinstance(task_after.get("lapian_stage"), str)
              and task_after["lapian_stage"].startswith("终态：awaiting_assets"),
              str(task_after.get("lapian_stage"))[:120])

        tdir_fail = make_task(TASK_FAIL, "ingested", FAIL_BRIDGE)
        probe_guard.http_req(base, f"/api/hub/discuss/{TASK_FAIL}", method="POST", timeout=20)
        fail_info = wait_terminal(tdir_fail, timeout=45)
        task_fail = fail_info["task"]
        check("[2] 失败终态：ended_at 落值（失败也停表，不继续走）",
              bool(task_fail.get("lapian_ended_at")),
              json.dumps({k: task_fail.get(k) for k in ("lapian_stage", "lapian_ended_at")},
                         ensure_ascii=False))
        check("[2] 失败终态：阶段文案以「终态：失败」开头（人话可读）",
              str(task_fail.get("lapian_stage") or "").startswith("终态：失败"),
              str(task_fail.get("lapian_stage"))[:120])

        # ── [3] 读侧透出：get_task / 列表 / 详情 ──────────────
        print("3) 读侧透出：get_task、列表、详情三处 lapian 与任务文件同值")
        detail = hub_api.get_task(TASK_RUN)
        want = {k: task_after.get(k) or "" for k in
                ("lapian_started_at", "lapian_stage_at", "lapian_ended_at")}
        got = detail.get("lapian") or {}
        check("[3] get_task.lapian 三值 == 任务.json（读层不加工时间戳）",
              got == {"started_at": want["lapian_started_at"], "stage_at": want["lapian_stage_at"],
                      "ended_at": want["lapian_ended_at"]},
              f"got={got} want={want}")
        code, listing = probe_guard.http_req(base, "/api/hub/tasks", timeout=10)
        rows = [r for r in ((listing or {}).get("tasks") or []) if r.get("local_id") == TASK_RUN]
        check("[3] 列表摘要带 lapian（列表页不拉详情也能走表）",
              code == 200 and bool(rows)
              and (rows[0].get("lapian") or {}).get("started_at") == want["lapian_started_at"],
              f"{code} {json.dumps(rows[:1], ensure_ascii=False)[:220]}")
        code, one = probe_guard.http_req(base, f"/api/hub/task/{TASK_RUN}", timeout=10)
        drow = ((one or {}).get("task") or (one or {}))
        check("[3] 详情端点 lapian 与列表/任务文件同值（同源一份数据）",
              code == 200 and (drow.get("lapian") or {}) == got,
              f"{code} {json.dumps(drow.get('lapian'), ensure_ascii=False)[:200]}")

        # ── [4] 重拆重置 ─────────────────────────────────────
        print("4) 重拆：failed → 新 job，两锚按新 job 重置、上一轮终态锚作废")
        state.transition(tdir_run, "failed", note="probe-at16-b4a 造 failed 以走重试入口")
        old = read_task(tdir_run)
        time.sleep(1.1)                      # 让新锚一定落在下一秒（ISO 秒精度）
        code, retry = probe_guard.http_req(base, f"/api/hub/task/{TASK_RUN}/retry",
                                           method="POST", timeout=20)
        retry_first = read_task(tdir_run)   # 父进程在 POST 内同步写锚，返回即已落盘
        check("[4] 重拆起手：启动锚按新 job 重置（新 > 旧）",
              code == 200 and retry_first.get("lapian_started_at", "")
              > (old.get("lapian_started_at") or ""),
              f"{code} old={old.get('lapian_started_at')} new={retry_first.get('lapian_started_at')}")
        check("[4] 重拆起手：上一轮 ended_at 作废（新 job 一开不停表）",
              retry_first.get("lapian_ended_at") == ""
              and str(retry_first.get("lapian_stage") or "").startswith("人工重试"),
              json.dumps(retry_first, ensure_ascii=False)[:200])
        retry_info = wait_terminal(tdir_run, timeout=45)
        new = retry_info["task"]
        check("[4] 重拆跑完：新终态锚落值且 ≥ 新启动锚",
              bool(new.get("lapian_ended_at")) and new["lapian_ended_at"] >= new["lapian_started_at"],
              f"started={new.get('lapian_started_at')} ended={new.get('lapian_ended_at')}")

        # ── [5] 缺锚诚实（旧任务不伪造） ──────────────────────
        print("5) 旧任务缺锚：created_at / 日志 [HH:mm:ss] / 行内「已 600s」都不许当锚")
        tdir_legacy = make_task(
            TASK_LEGACY, "discussing", "probe-b4a-legacy",
            lapian_stage="阶段B：远程等待（已 600s）")   # 旧数据：只有字符串，没有锚字段
        (tdir_legacy / DISCUSS_LOG).write_text(
            "[20:31:02] 拆解启动\n[20:31:05] 已提交拆解（job=abc），通常 1–3 分钟，远程模型慢时会更久\n"
            "[20:41:05] 阶段B：远程等待（已 600s）\n", encoding="utf-8")
        legacy = hub_api.get_task(TASK_LEGACY)
        check("[5] 旧任务 lapian 三值恒空串（不拿 created_at 凑启动锚）",
              (legacy.get("lapian") or {}) == {"started_at": "", "stage_at": "", "ended_at": ""},
              json.dumps(legacy.get("lapian"), ensure_ascii=False))
        check("[5] 旧任务阶段字符串照旧透传（不因缺锚丢文案）",
              legacy.get("lapian_stage") == "阶段B：远程等待（已 600s）",
              str(legacy.get("lapian_stage"))[:120])
        check("[5] 旧任务日志尾照旧透出（tail 首行不是启动行，前端不得据此反推）",
              "拆解启动" in (legacy.get("log_tail") or "")
              and legacy.get("lapian", {}).get("started_at") == "",
              (legacy.get("log_tail") or "")[:120])
        code, listing = probe_guard.http_req(base, "/api/hub/tasks", timeout=10)
        rows = [r for r in ((listing or {}).get("tasks") or []) if r.get("local_id") == TASK_LEGACY]
        check("[5] 列表摘要同样不造锚（缺锚 → 前端诚实显示「计时不可用」）",
              code == 200 and bool(rows) and (rows[0].get("lapian") or {}).get("started_at") == ""
              and (rows[0].get("lapian") or {}).get("stage_at") == "",
              json.dumps(rows[:1], ensure_ascii=False)[:220])

        # ── [6] 透传合同：lapian_stage 异构值降级 ──────────────
        print("6) 透传合同：lapian_stage 被写成对象时降级为空串（不漏 repr 给前端）")
        make_task(TASK_ODD, "discussing", "probe-b4a-odd",
                  lapian_stage={"stage": "阶段B", "at": "2026-01-01T00:00:00+08:00"})
        odd = hub_api.get_task(TASK_ODD)
        check("[6] 详情：异构 lapian_stage → 空串（不出 \"[object Object]\" / dict repr）",
              odd.get("lapian_stage") == "" and "{" not in str(odd.get("lapian_stage")),
              repr(odd.get("lapian_stage"))[:120])
        code, listing = probe_guard.http_req(base, "/api/hub/tasks", timeout=10)
        rows = [r for r in ((listing or {}).get("tasks") or []) if r.get("local_id") == TASK_ODD]
        check("[6] 摘要：异构 lapian_stage → 空串（str() 口径不把对象糊成 repr）",
              code == 200 and bool(rows) and rows[0].get("stage") == "",
              json.dumps(rows[:1], ensure_ascii=False)[:200])

        # ── [7] 真实数据根零写入 ──────────────────────────────
        real_after = {name: fingerprint(p) for name, p in
                      (("projects", real_projects), ("analysis", real_analysis),
                       ("uploads", real_uploads))}
        bad = {}
        for name in real_before:
            lost = sorted(set(real_before[name]) - set(real_after[name]))
            added = sorted(set(real_after[name]) - set(real_before[name]))
            changed = [k for k in real_before[name]
                       if k in real_after[name]
                       and real_before[name][k] != real_after[name][k]]
            if lost or added or changed:
                bad[name] = {"lost": lost[:5], "added": added[:5], "changed": changed[:5]}
        check("[7] 真实数据根零写入（automation/projects + analysis/reverse + uploads 指纹一致）",
              not bad, json.dumps(bad, ensure_ascii=False)[:400])
        return summary()
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        stub.close()
        shutil.rmtree(iso_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
