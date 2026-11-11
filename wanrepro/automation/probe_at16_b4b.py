#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AT-16 B4b 探针：拆解**远程等待心跳**（60s 追加日志 + 不改阶段锚 + 绕开 _stage_recorder 覆盖）。

跑法：wanrepro/ 下 `python -m automation.probe_at16_b4b`。

缺陷（同 B4a 的上游）：远程等待段在 UI 上「假冻结」——lapian_stage 与日志一行不动，
用户以为卡死。B4b 的辅修是**等待心跳**：等待期每 60s 往 拆解日志.txt 追一行
「本地等待循环仍在运行（总耗时 M分S秒）」。心跳是日志行，不是阶段行。

本探针锁四件事（真路径：hub_api.start_discuss → discuss.run_discuss → start_and_wait
→ HTTP 慢返回假 bridge；只在探针进程内把 POLL_INTERVAL/HEARTBEAT_INTERVAL 调小加速）：

  [0] 隔离与同源：自起 ui/server.py（临时 WANREPRO_AUTO_DIR + 假 bridge），
      /api/hub/where 自述 TASKS_DIR == 探针根，不一致拒跑（防误写/误删真实任务）。
  [1] 心跳真的出：等待期 ≥2 行心跳落日志，行内总耗时**严格递增**（M分S秒可解析）。
  [2] 心跳不改阶段/锚：只含心跳行的采样窗口里，lapian_stage 与 lapian_stage_at **逐字不变**
      ——即心跳没走 _stage_recorder（走一次就会把阶段等待计时清零）。
  [3] 阶段变化照旧：等待期夹一次真 stage 变化 → 阶段锚前进；其后的心跳窗口里不再变。
  [4] 终态后心跳停止：job done → ended_at 落值，再等 3 个心跳周期，日志行数一格不涨。
  [5] 默认 60s：HEARTBEAT_INTERVAL == 60；不传 heartbeat_interval 时短跑不出心跳（不是秒级刷屏）。
  [6] 回调分离（单元级）：on_stage 只收阶段行、on_heartbeat 只收心跳行，互不串门。
  [7] 文案诚实：只说「本地等待循环仍在运行」+ 总耗时 + 5–15 分钟预期；不出现百分比/远程进度暗示。

夹具任务只用 auto-9711..9712（隔离根内，跑完连根删）；真实任务零写入，跑前跑后对账。
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

ROOT = Path(__file__).resolve().parents[1]          # wanrepro/
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ui"))                 # hub_api.py 在 ui/ 下（与 probe_hub 同法）

from automation import config, probe_guard  # noqa: E402

RESULTS: list[tuple[str, bool]] = []
TASK_LIVE = "auto-9711"          # 心跳端到端
TASK_UNIT = "auto-9712"          # 回调分离单元级
TASK_SLOW = "auto-9713"          # 生产默认 60s 慢跑（env 开关）
DISCUSS_LOG = "拆解日志.txt"
BRIDGE_ID = "probe-b4b-run"
HEARTBEAT_MARK = "本地等待循环仍在运行"
HEARTBEAT_RE = re.compile(r"总耗时 (?:(\d+)分)?(\d+)秒")


def check(name: str, cond: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" —— {detail}" if detail and not cond else ""))
    return bool(cond)


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


# ── 假 bridge：慢返回 + 阶段变化 + done 后产物齐 ────────────────────────
class SlowLapianBridge:
    """只实现拆解要用的三个面：lapian/run、job 轮询（慢返回）、tasks（产物标志）。

    产物标志**故意**在 done 之前为假：run_discuss 起手会先查产物（产物优先），
    若一开始就齐，它会跳过整个拆解，根本走不到等待循环→心跳就无从验证。
    """

    def __init__(self, running_polls: int = 12, stage_every: int = 4, final: str = "done") -> None:
        self.running_polls = running_polls
        self.stage_every = stage_every
        self.final = final
        self.polls = 0
        self._lock = threading.Lock()
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
                if n:
                    self.rfile.read(n)
                with outer._lock:
                    outer.polls = 0
                self._send(200, {"job": "beefbeef"})

            def do_GET(self) -> None:         # noqa: N802
                if self.path == "/api/tasks":
                    with outer._lock:
                        ready = outer.polls >= outer.running_polls
                    self._send(200, {"tasks": [{"id": BRIDGE_ID,
                                                "has_disc": ready, "has_check": ready,
                                                "has_prompt": ready}]})
                    return
                if self.path.startswith("/api/job/"):
                    with outer._lock:
                        outer.polls += 1
                        n = outer.polls
                    if n >= outer.running_polls:
                        if outer.final == "error":
                            self._send(200, {"status": "error", "error": "探针假错误：桥侧拆解失败"})
                        else:
                            self._send(200, {"status": "done", "elapsed": round(n * 0.2, 1),
                                             "result": "probe"})
                        return
                    self._send(200, {"status": "running",
                                     "stage": f"阶段{1 + (n - 1) // outer.stage_every}",
                                     "elapsed": round(n * 0.2, 1)})
                    return
                self._send(404, {"error": f"stub bridge 不认 {self.path}"})

        self.port = probe_guard.free_port()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def reset(self) -> None:
        """把轮询计数归零（产物标志随之回假）：run_discuss 起手先查产物，产物假才真走等待循环。"""
        with self._lock:
            self.polls = 0

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


# ── 夹具 ──────────────────────────────────────────────────────────────
def make_task(local_id: str, bridge_id: str) -> Path:
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


def wait_job(job_id: str, timeout: float = 40.0) -> dict:
    import hub_api  # noqa: E402

    deadline = time.time() + timeout
    while time.time() < deadline:
        rec = hub_api.HUB_JOBS.get(job_id) or {}
        if rec.get("status") != "running":
            return rec
        time.sleep(0.05)
    return hub_api.HUB_JOBS.get(job_id) or {}


def main() -> int:
    real_root = Path(config.PROJECTS_DIR)
    real_before = probe_guard.projects_snapshot(real_root)
    iso_root = Path(tempfile.mkdtemp(prefix="wanprobe-at16b4b-",
                                    dir=os.environ.get("TMPDIR") or tempfile.gettempdir()))
    iso_projects = iso_root / "projects"
    iso_projects.mkdir(parents=True, exist_ok=True)
    stub = SlowLapianBridge()
    port = probe_guard.free_port()
    base = f"http://127.0.0.1:{port}"

    # 读写同源：先补丁 config 再懒加载（hub_api.TASKS_DIR = config.PROJECTS_DIR，import 期求值）
    config.PROJECTS_DIR = iso_projects
    old_bridge_base = config.BRIDGE_BASE
    config.BRIDGE_BASE = stub.base
    import hub_api  # noqa: E402
    from automation import bridge as abridge, discuss  # noqa: E402

    proc = None
    keep = (discuss.POLL_INTERVAL, discuss.HEARTBEAT_INTERVAL)
    real_run = discuss.run_discuss
    real_bridge_get, real_bridge_post = abridge.get, abridge.post_json
    try:
        stub.thread.start()
        env = dict(os.environ, WANREPRO_AUTO_DIR=str(iso_projects), WANREPRO_BRIDGE=stub.base)
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
            print("[probe-at16-b4b] 拒跑：数据根不同源，后续写入/清理一律不执行。")
            return summary()

        # ── [5] 默认间隔 60s（先验常量与默认行为，后面才调小加速）──────────
        print("5) 默认间隔：HEARTBEAT_INTERVAL=60s，不传参时短跑不出心跳（不刷屏）")
        check("[5] HEARTBEAT_INTERVAL == 60（等待期 60s 一行）",
              discuss.HEARTBEAT_INTERVAL == 60, str(discuss.HEARTBEAT_INTERVAL))
        tdir_unit = make_task(TASK_UNIT, "probe-b4b-unit")
        unit_stage, unit_beat = [], []
        keep_poll = discuss.POLL_INTERVAL
        discuss.POLL_INTERVAL = 0.2
        try:
            discuss.start_and_wait({"bridge_id": "probe-b4b-unit", "intent": ""},
                                   on_stage=unit_stage.append, on_heartbeat=unit_beat.append)
        except Exception as err:  # noqa: BLE001 —— 不传 heartbeat_interval：默认 60s，跑完一轮 ≈1.2s
            check("[5] 默认间隔下短跑（不传 heartbeat_interval）", False, f"{type(err).__name__}: {err}")
        discuss.POLL_INTERVAL = keep_poll
        check("[5] 默认 60s 下 ≈1.2s 的等待**不许**出心跳（心跳不是每次轮询都刷）",
              unit_beat == [], f"{len(unit_beat)} 行: {unit_beat[:2]}")

        # ── [6] 回调分离（单元级，直接对 start_and_wait）──────────────────
        print("6) 回调分离：on_stage 只收阶段行、on_heartbeat 只收心跳行，互不串门")
        unit_stage.clear()
        unit_beat.clear()
        discuss.POLL_INTERVAL, discuss.HEARTBEAT_INTERVAL = 0.2, 0.4
        try:
            discuss.start_and_wait({"bridge_id": "probe-b4b-unit", "intent": ""},
                                   on_stage=unit_stage.append, on_heartbeat=unit_beat.append)
        finally:
            discuss.POLL_INTERVAL, discuss.HEARTBEAT_INTERVAL = keep
        check("[6] on_heartbeat 收到的每行都是心跳文案（没有阶段行借道）",
              bool(unit_beat) and all(HEARTBEAT_MARK in m for m in unit_beat),
              f"{len(unit_beat)} 行: {unit_beat[:3]}")
        check("[6] on_stage 收到的每行都不是心跳文案（心跳没走 _stage_recorder 那条链）",
              bool(unit_stage) and not any(HEARTBEAT_MARK in m for m in unit_stage),
              f"{len(unit_stage)} 行: {unit_stage[:3]}")
        check("[6] 阶段行里没有「总耗时」口径（阶段行只报桥给的单阶段耗时）",
              not any("总耗时" in m for m in unit_stage),
              f"{unit_stage[:3]}")

        # ── [1]~[4] 端到端：hub 起拆解，等待期采样 ────────────────────────
        print("1) 端到端：hub 起拆解（慢 bridge），等待期日志追加心跳且总耗时递增")
        tdir = make_task(TASK_LIVE, BRIDGE_ID)
        # 心跳总耗时是**整秒**口径（生产 60s 一行，整秒必然递增）；加速度下若把间隔压到
        # 亚秒，同秒内会出多行同值，测不出「递增」这条性质。故这里把等待拉长到 ≈5s、
        # 心跳间隔设 1s：4 行心跳、总耗时 1→2→3→4 秒，性质可证。
        discuss.POLL_INTERVAL, discuss.HEARTBEAT_INTERVAL = 0.2, 1.0
        stub.running_polls = 25
        stub.reset()      # 上游单元跑过同一假 bridge：产物标志必须先回假，否则 run_discuss 走「产物优先」跳过等待
        started = hub_api.start_discuss(TASK_LIVE)
        samples: list[tuple[list[str], dict]] = []
        deadline = time.time() + 30
        while time.time() < deadline:
            log_now = read_log(tdir)
            task_now = read_task(tdir)
            samples.append((log_now, task_now))
            if str(task_now.get("lapian_stage") or "").startswith("终态"):
                break
            time.sleep(0.03)
        rec = wait_job(started["job_id"])
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
        time.sleep(1.2)                    # > 3 个心跳周期（0.35s × 3）
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
              bool(beat_lines) and all(hb_seconds(l) is not None and "5–15 分钟" in l for l in beat_lines),
              f"{beat_lines[:2]}")
        check("[7] 心跳行不含百分比 / 远程进度暗示（% / 进度 / 已完成 / 剩余）",
              bool(beat_lines) and not any(("%" in l or "进度" in l or "已完成" in l or "剩余" in l)
                                           for l in beat_lines),
              f"{[l for l in beat_lines if '%' in l or '进度' in l][:2]}")

        # ── [9] 生产默认 60s 实测（慢跑，env 开关；默认跑法跳过）────────────
        if os.environ.get("WANPROBE_B4B_SLOW") == "1":
            print("9) 生产默认间隔实测（WANPROBE_B4B_SLOW=1）：**不动**心跳常量，等 66s 看真心跳")
            tdir_slow = make_task(TASK_SLOW, BRIDGE_ID)
            discuss.POLL_INTERVAL, discuss.HEARTBEAT_INTERVAL = 0.2, 60
            stub.running_polls = 330      # 0.2s × 330 = 66s 等待
            stub.reset()
            slow_job = hub_api.start_discuss(TASK_SLOW)
            slow_rec = wait_job(slow_job["job_id"], timeout=180)
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

        # ── 真实任务零写入 ───────────────────────────────────────────────
        real_after = probe_guard.projects_snapshot(real_root)
        try:
            probe_guard.assert_no_loss(real_before, real_after)
            check("[8] 真实任务零损失/零新增（真实数据根快照对账）", True)
        except probe_guard.GuardError as err:
            check("[8] 真实任务零损失/零新增（真实数据根快照对账）", False, str(err))
        return summary()
    finally:
        discuss.POLL_INTERVAL, discuss.HEARTBEAT_INTERVAL = keep
        discuss.run_discuss = real_run
        abridge.get, abridge.post_json = real_bridge_get, real_bridge_post
        config.BRIDGE_BASE = old_bridge_base
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
