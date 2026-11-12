#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AT-16 B4b 探针：拆解**远程等待心跳**（60s 追加日志 + 不改阶段锚 + 绕开 _stage_recorder 覆盖）。

跑法：wanrepro/ 下 `python automation/probe_at16_b4b.py`。
慢跑（默认 60s 节奏实测，约 +80s）：`WANPROBE_B4B_SLOW=1 python automation/probe_at16_b4b.py`。

缺陷（同 B4a 的上游）：远程等待段在 UI 上「假冻结」——lapian_stage 与日志一行不动，
用户以为卡死。B4b 的辅修是**等待心跳**：等待期每 60s 往 拆解日志.txt 追一行
「本地等待循环仍在运行（总耗时 M分S秒）」。心跳是日志行，不是阶段行。

**AT-19 之后为什么必须改造**：执行体是独立子进程（automation/discuss_runner.py），旧版
「只在探针进程内把 POLL_INTERVAL/HEARTBEAT_INTERVAL 调小加速」对真正的执行体恒等于没设
（进程内补丁打不到子进程），旧版在 42e3c11 上只剩 16/22（探针窗口内等不到第 2 行心跳与 done）。

改造后端到端段**不打任何补丁**：hub 起真子进程（POST /api/hub/discuss/<id> →
_spawn_process → discuss_runner），加速只走环境变量 `WANREPRO_POLL_INTERVAL` /
`WANREPRO_HEARTBEAT_INTERVAL`（由 _spawn_process 透传；默认值 = 模块常量 3s / 60s，
不设即生产节奏）。[5][6] 两条是**单元级**断言（直接对 discuss.start_and_wait 验回调
分离合同，不经进程），保持进程内直调——它们锁的是 discuss 模块自身的合同，不是 hub 链路。

锁的点（原 22 项断言编号与判据逐条保留；末项为本卡新增，证明「执行体确为独立子进程」）：

  [0] 隔离与同源：自起 ui/server.py（临时 WANREPRO_AUTO_DIR + 临时 WANREPRO_ANALYSIS_ROOT
      + 假 bridge），/api/hub/where 自述 TASKS_DIR == 探针根，不一致拒跑。
  [1] 心跳真的出：等待期 ≥2 行心跳落日志，行内总耗时**严格递增**（M分S秒可解析）。
  [2] 心跳不改阶段/锚：只含心跳行的采样窗口里，lapian_stage 与 lapian_stage_at **逐字不变**
      ——即心跳没走 _stage_recorder（走一次就会把阶段等待计时清零）。
  [3] 阶段变化照旧：等待期夹一次真 stage 变化 → 阶段锚前进；其后的心跳窗口里不再变。
  [4] 终态后心跳停止：job done → ended_at 落值，再等 3 个心跳周期，日志行数一格不涨。
  [5] 默认 60s：HEARTBEAT_INTERVAL == 60；不传 heartbeat_interval 时短跑不出心跳（不是秒级刷屏）。
  [6] 回调分离（单元级）：on_stage 只收阶段行、on_heartbeat 只收心跳行，互不串门。
  [7] 文案诚实：只说「本地等待循环仍在运行」+ 总耗时 + 5–15 分钟预期；不出现百分比/远程进度暗示。
  [9] 生产默认 60s 实测（慢跑，env 开关；默认跑法跳过）：不动心跳常量，第二个隔离实例
      等待 66s 看真心跳恰 1 行。

夹具任务只用 auto-9711..9713（隔离根内，跑完连根删）；真实任务零写入，跑前跑后对账
（automation/projects、analysis/reverse、uploads 三根指纹）。
"""
from __future__ import annotations

import json
import os
import re
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

# 单元级段（[5][6]）要验「模块常量默认 60s」：探针进程内必须无 env 加速位，
# 否则 env 会盖过常量、[5] 的「不传参不出心跳」失真。服务端加速位只在它的 env dict 里给。
os.environ.pop("WANREPRO_POLL_INTERVAL", None)
os.environ.pop("WANREPRO_HEARTBEAT_INTERVAL", None)

ROOT = Path(__file__).resolve().parents[1]          # wanrepro/
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ui"))                 # hub_api.py 在 ui/ 下（与 probe_hub 同法）

TASK_LIVE = "auto-9711"          # 心跳端到端（真子进程 + env 加速）
TASK_UNIT = "auto-9712"          # 回调分离单元级（进程内直调 discuss，不经 hub）
TASK_SLOW = "auto-9713"          # 生产默认 60s 慢跑（WANPROBE_B4B_SLOW=1 时启用）
DISCUSS_LOG = "拆解日志.txt"
BRIDGE_ID = "probe-b4b-run"
UNIT_BRIDGE_ID = "probe-b4b-unit"
SLOW_BRIDGE_ID = "probe-b4b-slow"
HEARTBEAT_MARK = "本地等待循环仍在运行"
HEARTBEAT_RE = re.compile(r"总耗时 (?:(\d+)分)?(\d+)秒")

E2E_POLLS = 25                   # 0.2s × 25 ≈ 5s 等待；心跳 1s → 4 行（1/2/3/4 秒，整秒递增）
E2E_BEAT = 1.0                   # 端到端心跳间隔（秒；服务端 env 透传给子进程）
UNIT_POLLS = 12                  # 单元级默认节奏：0.2s × 12 ≈ 2.4s < 60s → 0 行心跳

RESULTS: list[tuple[str, bool]] = []


def check(name: str, cond, detail: str = "") -> bool:
    ok = bool(cond)
    RESULTS.append((name, ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail and not ok else ""))
    return ok


def summary() -> int:
    fails = [n for n, ok in RESULTS if not ok]
    print(f"\n[probe-at16-b4b] {len(RESULTS) - len(fails)}/{len(RESULTS)} PASS")
    for n in fails:
        print(f"  FAIL {n}")
    return 1 if fails else 0


def hb_seconds(line: str) -> int | None:
    """心跳行 → 总耗时秒数（解析不出返回 None，断言里当失败处理）。"""
    m = HEARTBEAT_RE.search(line)
    if not m:
        return None
    return int(m.group(1) or 0) * 60 + int(m.group(2))


# ── 假 bridge：慢返回 + 阶段变化 + done 后产物齐（按 bridge_id 分账）────────
class SlowLapianBridge:
    """只实现拆解要用的三个面：lapian/run、job 轮询（慢返回）、tasks（产物标志）。

    产物标志**故意**在 done 之前为假：run_discuss 起手会先查产物（产物优先），
    若一开始就齐，它会跳过整个拆解，根本走不到等待循环→心跳就无从验证。
    每个 POST /api/lapian/run 开一个新 job：该 bridge_id 的轮询计数归零、产物标志回假
    （真桥语义：新一次拆解开始，has_* 还不存在）。
    """

    def __init__(self, polls_budget: dict[str, int] | None = None) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, dict] = {}     # job_id -> {"bridge_id","polls"}
        self._done: dict[str, bool] = {}     # bridge_id -> 产物齐?
        self._budget = dict(polls_budget or {})   # bridge_id -> done 前的轮询次数
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
                    job = f"b4b{outer._seq:04d}"
                    outer._jobs[job] = {"bridge_id": bridge_id, "polls": 0}
                    outer._done[bridge_id] = False
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
                        limit = outer._budget.get(bid, UNIT_POLLS)
                        if n >= limit:
                            outer._done[bid] = True
                            self._send(200, {"status": "done", "elapsed": round(n * 0.2, 1),
                                             "result": "probe"})
                            return
                        stage = f"阶段{1 + (n - 1) // 4}"
                        self._send(200, {"status": "running", "stage": stage,
                                         "elapsed": round(n * 0.2, 1)})
                    return
                self._send(404, {"error": f"stub bridge 不认 {self.path}"})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = int(self.httpd.server_address[1])
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True,
                                       name="probe-b4b-bridge")

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


# ── 夹具与读盘 ────────────────────────────────────────────────────────
def make_task(local_id: str, bridge_id: str) -> Path:
    from automation import config

    tdir = Path(config.PROJECTS_DIR) / local_id
    shutil.rmtree(tdir, ignore_errors=True)
    (tdir / "inbox").mkdir(parents=True, exist_ok=True)
    task = {
        "local_id": local_id, "bridge_id": bridge_id,
        "created_at": "2026-01-01T00:00:00+08:00", "state": "ingested",
        "history": [{"at": "2026-01-01T00:00:00+08:00", "from": None, "to": "ingested",
                     "note": "probe-at16-b4b 夹具"}],
        "intent": "AT-16 B4b 探针夹具", "assets": [],
        "ingestion": {"zip_sha256": f"probe-{local_id}", "zip_name": "probe",
                      "video_name": "", "text_name": "", "ignored": []},
    }
    (tdir / "任务.json").write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
    return tdir


def read_task(tdir: Path) -> dict:
    try:
        return json.loads((tdir / "任务.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def read_log(tdir: Path) -> list[str]:
    try:
        return [l for l in (tdir / DISCUSS_LOG).read_text(encoding="utf-8").splitlines() if l.strip()]
    except OSError:
        return []


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


def start_server(base_env: dict[str, str]) -> tuple[subprocess.Popen, str, str]:
    """起一个隔离 ui/server.py → (proc, base, tasks_dir)。起不来/不同源由调用方拒跑。"""
    from automation import probe_guard

    port = probe_guard.free_port()
    base = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "ui" / "server.py"), "--port", str(port), "--no-browser"],
        cwd=str(ROOT), env=base_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
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
    return proc, base, (where or {}).get("tasks_dir") or ""


def wait_job(base: str, job_id: str, timeout: float = 60.0) -> dict:
    """轮询服务端 /api/hub/job/<id> 到非 running（job 记录由 _reap 回写）。"""
    from automation import probe_guard

    deadline = time.time() + timeout
    rec = {}
    while time.time() < deadline:
        code, rec = probe_guard.http_req(base, f"/api/hub/job/{job_id}", timeout=10)
        if code == 200 and isinstance(rec, dict) and rec.get("status") != "running":
            return rec
        time.sleep(0.1)
    return rec if isinstance(rec, dict) else {}


def main() -> int:
    iso_root = Path(tempfile.mkdtemp(prefix="wanprobe-at16b4b-"))
    iso_projects = iso_root / "projects"
    iso_analysis = iso_root / "analysis" / "reverse"
    iso_projects.mkdir(parents=True, exist_ok=True)
    iso_analysis.mkdir(parents=True, exist_ok=True)

    stub = SlowLapianBridge(polls_budget={BRIDGE_ID: E2E_POLLS, SLOW_BRIDGE_ID: 330})
    stub.thread.start()

    # 隔离根走环境变量（父与子同源）：hub_api.TASKS_DIR / discuss._ANALYSIS_ROOT 都在 import
    # 期按 env 求值。探针进程内的单元级段（[5][6]）也走这份 env（真 config，不补丁）。
    os.environ["WANREPRO_AUTO_DIR"] = str(iso_projects)
    os.environ["WANREPRO_ANALYSIS_ROOT"] = str(iso_analysis)
    os.environ["WANREPRO_BRIDGE"] = stub.base
    os.environ["WANREPRO_WATCHDOG"] = "0"
    os.environ["WANREPRO_BOOT_RECONCILE"] = "0"

    from automation import config, probe_guard  # noqa: E402
    import hub_api  # noqa: E402  —— 只用于读侧 get_task（与 server 同一份磁盘/同一函数）
    from automation import discuss  # noqa: E402

    # 真实数据根（只读）：config.PROJECTS_DIR 已被 env 指到隔离根，这里显式取仓内
    real_projects = ROOT / "automation" / "projects"
    real_analysis = ROOT / "analysis" / "reverse"
    real_uploads = ROOT / "uploads"
    real_before = {name: fingerprint(p) for name, p in
                   (("projects", real_projects), ("analysis", real_analysis),
                    ("uploads", real_uploads))}

    proc = None
    slow_proc = None
    try:
        # 服务端 env：隔离根 + 假桥 + 加速位（执行体子进程由 _spawn_process 透传继承）。
        # 轮询 0.2s × 25 ≈ 5s 等待；心跳 1s → 4 行（总耗时 1→2→3→4 秒，整秒严格递增）。
        env = {**os.environ,
               "WANREPRO_POLL_INTERVAL": "0.2",
               "WANREPRO_HEARTBEAT_INTERVAL": str(E2E_BEAT)}
        proc, base, tasks_dir = start_server(env)
        try:
            probe_guard.assert_same_root({"tasks_dir": tasks_dir}, iso_projects)
            check("[0] 隔离实例起来且读写同源（/api/hub/where == 探针根）", bool(tasks_dir),
                  f"{base} 无响应或没给 tasks_dir")
            print(f"     数据根 {tasks_dir}")
        except probe_guard.GuardError as err:
            check("[0] 隔离实例起来且读写同源（/api/hub/where == 探针根）", False, str(err))
            print("[probe-at16-b4b] 拒跑：数据根不同源，后续写入/清理一律不执行。")
            return summary()

        # ── [5] 默认间隔 60s（单元级：直接对模块常量与默认行为）──────────
        print("5) 默认间隔：HEARTBEAT_INTERVAL=60s，不传参时短跑不出心跳（不刷屏）")
        check("[5] HEARTBEAT_INTERVAL == 60（等待期 60s 一行）",
              discuss.HEARTBEAT_INTERVAL == 60, str(discuss.HEARTBEAT_INTERVAL))
        tdir_unit = make_task(TASK_UNIT, UNIT_BRIDGE_ID)
        unit_stage, unit_beat = [], []
        keep_poll = discuss.POLL_INTERVAL
        discuss.POLL_INTERVAL = 0.2
        try:
            discuss.start_and_wait({"bridge_id": UNIT_BRIDGE_ID, "intent": ""},
                                   on_stage=unit_stage.append, on_heartbeat=unit_beat.append)
        except Exception as err:  # noqa: BLE001 —— 不传 heartbeat_interval：默认 60s，跑完 ≈2.4s
            check("[5] 默认间隔下短跑（不传 heartbeat_interval）", False, f"{type(err).__name__}: {err}")
        discuss.POLL_INTERVAL = keep_poll
        check("[5] 默认 60s 下 ≈2.4s 的等待**不许**出心跳（心跳不是每次轮询都刷）",
              unit_beat == [], f"{len(unit_beat)} 行: {unit_beat[:2]}")

        # ── [6] 回调分离（单元级，直接对 start_and_wait）──────────────────
        print("6) 回调分离：on_stage 只收阶段行、on_heartbeat 只收心跳行，互不串门")
        unit_stage.clear()
        unit_beat.clear()
        keep_beat = discuss.HEARTBEAT_INTERVAL
        discuss.POLL_INTERVAL, discuss.HEARTBEAT_INTERVAL = 0.2, 0.4
        try:
            discuss.start_and_wait({"bridge_id": UNIT_BRIDGE_ID, "intent": ""},
                                   on_stage=unit_stage.append, on_heartbeat=unit_beat.append)
        finally:
            discuss.POLL_INTERVAL, discuss.HEARTBEAT_INTERVAL = keep_poll, keep_beat
        check("[6] on_heartbeat 收到的每行都是心跳文案（没有阶段行借道）",
              bool(unit_beat) and all(HEARTBEAT_MARK in m for m in unit_beat),
              f"{len(unit_beat)} 行: {unit_beat[:3]}")
        check("[6] on_stage 收到的每行都不是心跳文案（心跳没走 _stage_recorder 那条链）",
              bool(unit_stage) and not any(HEARTBEAT_MARK in m for m in unit_stage),
              f"{len(unit_stage)} 行: {unit_stage[:3]}")
        check("[6] 阶段行里没有「总耗时」口径（阶段行只报桥给的单阶段耗时）",
              not any("总耗时" in m for m in unit_stage),
              f"{unit_stage[:3]}")

        # ── [1]~[4] 端到端：hub 起真子进程，等待期采样 ────────────────────
        print("1) 端到端：hub 起拆解（真子进程 + env 加速），等待期日志追加心跳且总耗时递增")
        tdir = make_task(TASK_LIVE, BRIDGE_ID)
        code, job = probe_guard.http_req(base, f"/api/hub/discuss/{TASK_LIVE}", method="POST",
                                         timeout=20)
        check("[1] 执行体确为独立子进程（hub 只派发：mode=process 且有 pid；"
              "旧版此处是进程内补丁执行体）",
              code == 200 and job.get("mode") == "process" and bool(job.get("pid")),
              f"{code} {job}")
        samples: list[tuple[list[str], dict]] = []
        deadline = time.time() + 30
        while time.time() < deadline:
            log_now = read_log(tdir)
            task_now = read_task(tdir)
            samples.append((log_now, task_now))
            if str(task_now.get("lapian_stage") or "").startswith("终态"):
                break
            time.sleep(0.03)
        rec = wait_job(base, job.get("job_id", ""))
        final_log = read_log(tdir)
        task_after = read_task(tdir)

        beats = [(i, l) for i, l in enumerate(final_log) if HEARTBEAT_MARK in l]
        beat_secs = [hb_seconds(l) for _, l in beats]
        check("[1] 等待期出现 ≥2 行心跳（日志里真的在动）", len(beats) >= 2, f"{len(beats)} 行")
        check("[1] 心跳行总耗时严格递增（M分S秒可解析，不是复制粘贴）",
              len(beat_secs) >= 2 and all(s is not None for s in beat_secs)
              and all(b > a for a, b in zip(beat_secs, beat_secs[1:])),
              f"{beat_secs}")
        check("[1] job 走到 done 终态（心跳在真实等待循环里出的，不是假路径）",
              rec.get("status") == "done", json.dumps(rec, ensure_ascii=False)[:200])

        # 采样窗口分析：只含心跳、没有阶段行的窗口里，阶段与阶段锚必须逐字不变
        hb_only_windows, anchor_moves, hb_only_bad = 0, [], []
        for (log_a, task_a), (log_b, task_b) in zip(samples, samples[1:]):
            new = log_b[len(log_a):]
            if not new:
                continue
            new_beats = [l for l in new if HEARTBEAT_MARK in l]
            new_others = [l for l in new if HEARTBEAT_MARK not in l]
            if new_beats and not new_others:
                hb_only_windows += 1
                if (task_a.get("lapian_stage"), task_a.get("lapian_stage_at")) != \
                        (task_b.get("lapian_stage"), task_b.get("lapian_stage_at")):
                    hb_only_bad.append({
                        "stage_before": task_a.get("lapian_stage"),
                        "stage_after": task_b.get("lapian_stage"),
                        "anchor_before": task_a.get("lapian_stage_at"),
                        "anchor_after": task_b.get("lapian_stage_at"),
                    })
            if [l for l in new_others if "阶段" in l]:
                anchor_moves.append((task_a.get("lapian_stage_at"), task_b.get("lapian_stage_at")))

        check("[2] 纯心跳窗口里 lapian_stage / lapian_stage_at 逐字不变（心跳没重置阶段等待）",
              hb_only_windows >= 2 and not hb_only_bad,
              f"窗口 {hb_only_windows} 个，变锚 {hb_only_bad[:2]}")
        stage_texts = {str(t.get("lapian_stage") or "") for _, t in samples}
        check("[2] lapian_stage 从没被写成心跳文案（阶段字段只装阶段）",
              not any(HEARTBEAT_MARK in s for s in stage_texts),
              str([s for s in stage_texts if HEARTBEAT_MARK in s])[:160])
        check("[2] 等待期（终态前）lapian_ended_at 恒空 —— 心跳不误停表",
              all(t.get("lapian_ended_at") == "" for _, t in samples
                  if t.get("lapian_started_at")
                  and not str(t.get("lapian_stage") or "").startswith("终态")),
              json.dumps([t.get("lapian_ended_at") for _, t in samples][:3], ensure_ascii=False))

        check("[3] 阶段变化照旧重置阶段锚（心跳不吞阶段行）",
              bool(anchor_moves) and any(b > a for a, b in anchor_moves),
              f"{anchor_moves[:3]}")
        started_set = {t.get("lapian_started_at") for _, t in samples if t.get("lapian_started_at")}
        check("[3] 启动锚全程一个值（心跳与阶段都不动总耗时起点）",
              len(started_set) == 1 and bool(started_set), str(started_set))

        # ── [4] 终态后心跳停止 ───────────────────────────────────────────
        print("4) 终态后：心跳停止、停表锚落值")
        n_before = len(read_log(tdir))
        time.sleep(1.2)                    # > 3 个心跳周期（1.0s × 3 取整窗口余量）
        n_after = len(read_log(tdir))
        check("[4] 终态后 1.2s（3 个心跳周期）日志行数一格不涨 —— 心跳已停",
              n_after == n_before, f"{n_before} → {n_after}")
        check("[4] 终态后不再出现新心跳行",
              len([l for l in read_log(tdir) if HEARTBEAT_MARK in l]) == len(beats),
              f"{len(beats)} → {len([l for l in read_log(tdir) if HEARTBEAT_MARK in l])}")
        check("[4] 终态停表依据仍在（lapian_ended_at 落值 ≥ 阶段锚）",
              bool(task_after.get("lapian_ended_at"))
              and task_after["lapian_ended_at"] >= (task_after.get("lapian_stage_at") or ""),
              f"stage_at={task_after.get('lapian_stage_at')} ended_at={task_after.get('lapian_ended_at')}")
        check("[4] 终态阶段文案仍是纯字符串终态行（心跳没覆盖终态）",
              isinstance(task_after.get("lapian_stage"), str)
              and task_after["lapian_stage"].startswith("终态：awaiting_assets"),
              str(task_after.get("lapian_stage"))[:120])

        # ── [7] 文案诚实 ────────────────────────────────────────────────
        print("7) 文案：只报本地等待循环 + 总耗时 + 5–15 分钟预期，不暗示远程真实进度")
        beat_lines = [l for _, l in beats]
        check("[7] 每行心跳都含「本地等待循环仍在运行」",
              bool(beat_lines) and all(HEARTBEAT_MARK in l for l in beat_lines),
              f"{beat_lines[:2]}")
        check("[7] 每行心跳都含总耗时（人话 M分S秒）且带 5–15 分钟预期口径",
              bool(beat_lines) and all(hb_seconds(l) is not None and "5–15 分钟" in l
                                       for l in beat_lines),
              f"{beat_lines[:2]}")
        check("[7] 心跳行不含百分比 / 远程进度暗示（% / 进度 / 已完成 / 剩余）",
              bool(beat_lines) and not any(("%" in l or "进度" in l or "已完成" in l or "剩余" in l)
                                           for l in beat_lines),
              f"{[l for l in beat_lines if '%' in l or '进度' in l][:2]}")

        # ── [9] 生产默认 60s 实测（慢跑，env 开关；默认跑法跳过）────────────
        if os.environ.get("WANPROBE_B4B_SLOW") == "1":
            print("9) 生产默认间隔实测（WANPROBE_B4B_SLOW=1）：第二个隔离实例**不带**心跳 env，"
                 "等 66s 看真心跳")
            tdir_slow = make_task(TASK_SLOW, SLOW_BRIDGE_ID)
            slow_env = {**os.environ, "WANREPRO_POLL_INTERVAL": "0.2"}   # 心跳 env 不给 → 默认 60s
            slow_proc, slow_base, _ = start_server(slow_env)
            code, slow_job = probe_guard.http_req(slow_base, f"/api/hub/discuss/{TASK_SLOW}",
                                                  method="POST", timeout=20)
            slow_rec = wait_job(slow_base, slow_job.get("job_id", ""), timeout=180)
            slow_beats = [l for l in read_log(tdir_slow) if HEARTBEAT_MARK in l]
            slow_secs = [hb_seconds(l) for l in slow_beats]
            check("[9] 默认 60s：66s 等待里恰 1 行心跳（第 2 次要到 120s，不多发）",
                  slow_rec.get("status") == "done" and len(slow_beats) == 1,
                  f"status={slow_rec.get('status')} 心跳 {len(slow_beats)} 行")
            check("[9] 该行总耗时落在 60–66s（真按 60s 节奏出，不是 1s 刷屏）",
                  len(slow_secs) == 1 and slow_secs[0] is not None and 60 <= slow_secs[0] <= 66,
                  f"{slow_secs}")
            slow_task = read_task(tdir_slow)
            check("[9] 默认间隔下心跳同样不动阶段锚（阶段行之外的唯一变化是日志）",
                  bool(slow_task.get("lapian_stage_at")) and bool(slow_task.get("lapian_ended_at")),
                  f"stage_at={slow_task.get('lapian_stage_at')} ended_at={slow_task.get('lapian_ended_at')}")

        # ── [8] 真实数据根零写入 ────────────────────────────────────────
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
        check("[8] 真实数据根零写入（automation/projects + analysis/reverse + uploads 指纹一致）",
              not bad, json.dumps(bad, ensure_ascii=False)[:400])
        return summary()
    finally:
        for p in (proc, slow_proc):
            if p is not None and p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    p.kill()
        stub.close()
        shutil.rmtree(iso_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
