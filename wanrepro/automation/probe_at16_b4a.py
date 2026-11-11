#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AT-16 B4a 探针：拆解进度**双锚持久化**（写侧 + 读侧，纯离线自起隔离实例）。

跑法：wanrepro/ 下 `python -m automation.probe_at16_b4a`。

背景缺陷：拆解远程等待段进度假冻结——`lapian_stage` 是静态阶段字符串，UI 上的「已 Xs」
是日志行里的 job 总耗时快照，10 分钟不动。本探针锁**锚**，不锁像素（前端走表由
web/scripts/probe-at16-b4a.mjs 用可控时钟验）：

  [0] 隔离与同源：自起 ui/server.py（临时 WANREPRO_AUTO_DIR + 假 bridge），/api/hub/where
      自述 TASKS_DIR == 探针根，不一致拒跑（防误写/误删真实任务）。
  [1] 首拆双锚：start_discuss 起手落 lapian_started_at / lapian_stage_at，且 **ended_at 为空**
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

夹具任务只用 auto-9701..auto-9704（隔离根内，跑完连根删）；真实任务零写入，跑前跑后对账。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]          # wanrepro/
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ui"))                 # hub_api.py 在 ui/ 下（与 probe_hub 同法）

from automation import config, probe_guard  # noqa: E402

RESULTS: list[tuple[str, bool]] = []
TASK_RUN = "auto-9701"      # 首拆 → 阶段 → 终态 → 重拆
TASK_LEGACY = "auto-9702"   # 旧任务：无锚字段
TASK_ODD = "auto-9703"      # 异构 lapian_stage（对象）
TASK_FAIL = "auto-9704"     # 失败终态
DISCUSS_LOG = "拆解日志.txt"


def check(name: str, cond: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" —— {detail}" if detail and not cond else ""))
    return bool(cond)


def summary() -> int:
    fails = [n for n, ok in RESULTS if not ok]
    print(f"\n[probe-at16-b4a] {len(RESULTS) - len(fails)}/{len(RESULTS)} PASS")
    for n in fails:
        print(f"  FAIL {n}")
    return 1 if fails else 0


def make_task(local_id: str, state_name: str, bridge_id: str, **extra) -> Path:
    """隔离根内的任务夹具（与其它探针同形，跑完连根删）。"""
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
    return json.loads((tdir / "任务.json").read_text(encoding="utf-8"))


def wait_job(job_id: str, timeout: float = 30.0) -> dict:
    """等 HUB_JOBS 里这条 job 落终态（探针进程内的 hub_api 实例）。"""
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
    iso_root = Path(tempfile.mkdtemp(prefix="wanprobe-at16b4a-",
                                     dir=os.environ.get("TMPDIR") or tempfile.gettempdir()))
    iso_projects = iso_root / "projects"
    iso_projects.mkdir(parents=True, exist_ok=True)
    stub = probe_guard.StubBridge()
    port = probe_guard.free_port()
    base = f"http://127.0.0.1:{port}"

    # 读写同源：先补丁 config 再懒加载（hub_api.TASKS_DIR = config.PROJECTS_DIR，import 期求值）
    config.PROJECTS_DIR = iso_projects
    import hub_api  # noqa: E402
    from automation import discuss, state  # noqa: E402

    proc = None
    real_discuss = discuss.run_discuss
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
            print("[probe-at16-b4a] 拒跑：数据根不同源，后续写入/清理一律不执行。")
            return summary()

        # ── [1] 首拆双锚 + 阶段重置 + 运行中不停表 ─────────────
        print("1) 首拆双锚：起手两锚落盘；阶段变化只重置阶段锚，启动锚不动；运行中无终态锚")
        tdir_run = make_task(TASK_RUN, "ingested", "probe-b4a-run")
        stage_marks: dict[str, dict] = {}

        def fake_run(td: Path, *, verbose: bool = True, on_stage=None) -> str:
            """假拆解：两阶段 + 每段留 1.3s 等待窗（探针在窗内读真实任务文件）。"""
            state.transition(td, "discussing", note="probe-at16-b4a 假拆解")
            on_stage("阶段A：提交拆解（已 3s）")
            time.sleep(1.3)
            on_stage("阶段B：远程等待（已 45s）")
            time.sleep(1.3)
            return "probe-job"

        hub_api.discuss.run_discuss = fake_run
        started = hub_api.start_discuss(TASK_RUN)
        # 轮询任务文件，抓住两个阶段的快照 + 阶段B 等待窗内的运行态
        mid_window = None
        deadline = time.time() + 20
        while time.time() < deadline:
            task = read_task(tdir_run)
            stage_now = task.get("lapian_stage") or ""
            if stage_now.startswith("阶段A") and "A" not in stage_marks:
                stage_marks["A"] = dict(task)
            if stage_now.startswith("阶段B"):
                if "B" not in stage_marks:
                    stage_marks["B"] = dict(task)
                elif task.get("lapian_ended_at") == "":
                    mid_window = dict(task)
            if stage_now.startswith("终态"):
                break
            time.sleep(0.05)
        rec = wait_job(started["job_id"])
        task_after = read_task(tdir_run)

        check("[1] 首拆起手即落启动锚（lapian_started_at 非空）",
              bool((stage_marks.get("A") or {}).get("lapian_started_at")),
              json.dumps(stage_marks.get("A", {}), ensure_ascii=False)[:200])
        check("[1] 首拆起手即落阶段锚（lapian_stage_at == 启动锚，同一时刻起步）",
              (stage_marks.get("A") or {}).get("lapian_stage_at")
              == (stage_marks.get("A") or {}).get("lapian_started_at"),
              json.dumps(stage_marks.get("A", {}), ensure_ascii=False)[:200])
        check("[1] 阶段变化重置阶段锚（stage_at 前进）",
              bool(stage_marks.get("A")) and bool(stage_marks.get("B"))
              and stage_marks["B"]["lapian_stage_at"] > stage_marks["A"]["lapian_stage_at"],
              f"A={stage_marks.get('A', {}).get('lapian_stage_at')} B={stage_marks.get('B', {}).get('lapian_stage_at')}")
        check("[1] 阶段变化**不**动启动锚（总耗时不随阶段归零）",
              bool(stage_marks.get("B"))
              and stage_marks["B"]["lapian_started_at"] == stage_marks["A"]["lapian_started_at"],
              f"A={stage_marks.get('A', {}).get('lapian_started_at')} B={stage_marks.get('B', {}).get('lapian_started_at')}")
        check("[1] 运行中（阶段B 等待窗）终态锚恒空 —— 不停表",
              mid_window is not None and mid_window.get("lapian_ended_at") == "",
              json.dumps(mid_window or {}, ensure_ascii=False)[:200])

        # ── [2] 终态停表依据 ──────────────────────────────────
        print("2) 终态停表依据：成功/失败分别落 lapian_ended_at")
        check("[2] 成功终态：ended_at 落值且 ≥ 阶段锚（停表依据可算）",
              bool(task_after.get("lapian_ended_at"))
              and task_after["lapian_ended_at"] >= task_after["lapian_stage_at"],
              f"stage_at={task_after.get('lapian_stage_at')} ended_at={task_after.get('lapian_ended_at')}")
        check("[2] 成功终态：阶段文案仍是纯字符串且为终态行",
              isinstance(task_after.get("lapian_stage"), str)
              and task_after["lapian_stage"].startswith("终态：awaiting_assets"),
              str(task_after.get("lapian_stage"))[:120])

        tdir_fail = make_task(TASK_FAIL, "ingested", "probe-b4a-fail")

        def fake_fail(td: Path, *, verbose: bool = True, on_stage=None) -> str:
            state.transition(td, "discussing", note="probe-at16-b4a 假失败")
            on_stage("阶段A：提交拆解（已 3s）")
            time.sleep(1.1)
            raise discuss.DiscussError("桥侧拆解失败（探针假错误）")

        hub_api.discuss.run_discuss = fake_fail
        fail_job = hub_api.start_discuss(TASK_FAIL)
        wait_job(fail_job["job_id"])
        task_fail = read_task(tdir_fail)
        check("[2] 失败终态：ended_at 落值（失败也停表，不继续走）",
              bool(task_fail.get("lapian_ended_at")),
              json.dumps({k: task_fail.get(k) for k in ("lapian_stage", "lapian_ended_at")}, ensure_ascii=False))
        check("[2] 失败终态：阶段文案以「终态：失败」开头（人话可读）",
              str(task_fail.get("lapian_stage") or "").startswith("终态：失败"),
              str(task_fail.get("lapian_stage"))[:120])

        # ── [3] 读侧透出：get_task / 列表 / 详情 ──────────────
        print("3) 读侧透出：get_task、列表、详情三处 lapian 与任务文件同值")
        detail = hub_api.get_task(TASK_RUN)
        want = {k: task_after.get(k) or "" for k in ("lapian_started_at", "lapian_stage_at", "lapian_ended_at")}
        got = detail.get("lapian") or {}
        check("[3] get_task.lapian 三值 == 任务.json（读层不加工时间戳）",
              got == {"started_at": want["lapian_started_at"], "stage_at": want["lapian_stage_at"],
                      "ended_at": want["lapian_ended_at"]},
              f"got={got} want={want}")
        code, listing = probe_guard.http_req(base, "/api/hub/tasks", timeout=10)
        rows = [r for r in ((listing or {}).get("tasks") or []) if r.get("local_id") == TASK_RUN]
        check("[3] 列表摘要带 lapian（列表页不拉详情也能走表）",
              code == 200 and bool(rows) and (rows[0].get("lapian") or {}).get("started_at") == want["lapian_started_at"],
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
        hub_api.discuss.run_discuss = fake_run
        retry = hub_api.start_retry(TASK_RUN)
        retry_first = None
        deadline = time.time() + 20
        while time.time() < deadline:
            task = read_task(tdir_run)
            if retry_first is None and (task.get("lapian_stage") or "").startswith("人工重试"):
                retry_first = dict(task)
            if (task.get("lapian_stage") or "").startswith("终态"):
                break
            time.sleep(0.05)
        wait_job(retry["job_id"])
        new = read_task(tdir_run)
        check("[4] 重拆起手：启动锚按新 job 重置（新 > 旧）",
              retry_first is not None and retry_first["lapian_started_at"] > old["lapian_started_at"],
              f"old={old.get('lapian_started_at')} new={(retry_first or {}).get('lapian_started_at')}")
        check("[4] 重拆起手：上一轮 ended_at 作废（新 job 一开不停表）",
              retry_first is not None and retry_first.get("lapian_ended_at") == "",
              json.dumps(retry_first or {}, ensure_ascii=False)[:200])
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
        tdir_odd = make_task(TASK_ODD, "discussing", "probe-b4a-odd",
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

        # ── [7] 真实任务零写入 ────────────────────────────────
        real_after = probe_guard.projects_snapshot(real_root)
        try:
            probe_guard.assert_no_loss(real_before, real_after)
            check("[7] 真实任务零损失/零新增（真实数据根快照对账）", True)
        except probe_guard.GuardError as err:
            check("[7] 真实任务零损失/零新增（真实数据根快照对账）", False, str(err))
        return summary()
    finally:
        hub_api.discuss.run_discuss = real_discuss
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        stub.httpd.shutdown()
        stub.httpd.server_close()
        shutil.rmtree(iso_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
