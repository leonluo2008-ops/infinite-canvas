#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""JOB 生命周期审计探针（t_408a0867）：终态唯一出口 + 统一存活谓词。

跑法（wanrepro/ 下）：`python -m automation.probe_job_lifecycle`；退出码 0 = 全绿。

锁的口径（审计表里的两条合同，逐条机器判定）：

  [1] 写入点收敛：创建只许 `_spawn` / `_spawn_process` 两处写 status="running"；终态只许
      `_finalize_job` 写（源码里不再出现任何 `"status": "done"/"failed"` 直写）；活跃判定
      只许 `_job_blocks_retry`/`_job_alive`（`_active_submit` 付费闸同源）。
  [2] `_finalize_job` 合同：拒绝 running；就地 update（同一 dict 对象）；首个终态权威
      （force_finished 不被晚到的 done 翻回）；记录不存在且无 local_id 时不凭空造记录。
  [3] `_spawn` 三出口：正常 → done；普通异常 → failed（带脱敏 stderr）；BaseException
      （SystemExit）→ failed；线程起不来 → 抛 HubError 且记录当场 failed（不留 running）。
  [4] 周期心跳：线程 job 在目标体无回调的长跑里仍每 HEARTBEAT_SECONDS 续打（付费闸与
      held_job 判活的证据源）。
  [5] `_reap` 三出口：exit 0 → done；exit N → failed + 退出码文案；wait() 抛错 → failed。
  [6] `_spawn_process` 派发失败（命令行构造失败）→ 记录 failed，无 running 残留。
  [7] `_active_submit`：启动 <60s 的真 running / 心跳新鲜 → 活跃；心跳超 900s 的僵尸
      running / force_finished / failed / done → 不活跃。
  [8] `_finish_running_jobs`：running → force_finished（就地），且立刻不再算活跃。

隔离手法：`WANREPRO_AUTO_DIR` / `WANREPRO_ANALYSIS_ROOT` 指向本次临时根（import hub_api
之前设），不起子进程、不连桥；真实 projects/analysis/uploads 顶层集合跑前跑后逐字比对。
"""
from __future__ import annotations

import inspect
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]                    # wanrepro/
TMP_ROOT = Path(tempfile.mkdtemp(prefix="probe-job-lifecycle-"))
REAL_PROJECTS = ROOT / "automation" / "projects"
REAL_ANALYSIS = ROOT / "analysis" / "reverse"
REAL_UPLOADS = ROOT / "uploads"

os.environ["WANREPRO_AUTO_DIR"] = str(TMP_ROOT)                # 必须在 import hub_api 之前
os.environ["WANREPRO_ANALYSIS_ROOT"] = str(TMP_ROOT / "analysis")
os.environ["WANREPRO_BRIDGE"] = "http://127.0.0.1:9"            # 本探针不连桥
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ui"))

import hub_api  # noqa: E402

SOURCE = Path(hub_api.__file__).read_text(encoding="utf-8")
RESULTS: list[tuple[str, bool]] = []


def check(name: str, cond, detail: str = "") -> bool:
    ok = bool(cond)
    RESULTS.append((name, ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail and not ok else ""))
    return ok


def iso(delta: int = 0) -> str:
    return (datetime.now(timezone.utc).astimezone()
            + timedelta(seconds=delta)).isoformat(timespec="seconds")


def snapshot(root: Path) -> set[str]:
    try:
        return {p.name for p in root.iterdir()}
    except OSError:
        return set()


BEFORE = {"projects": snapshot(REAL_PROJECTS), "analysis": snapshot(REAL_ANALYSIS),
          "uploads": snapshot(REAL_UPLOADS)}


def wait_done(job_id: str, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        rec = hub_api.HUB_JOBS.get(job_id) or {}
        if rec.get("status") in hub_api.JOB_TERMINAL_STATUSES:
            return rec
        time.sleep(0.02)
    return hub_api.HUB_JOBS.get(job_id) or {}


def new_record(job_id: str, local_id: str = "auto-9700", kind: str = "probe-kind",
               status: str = "running", started: str | None = None) -> dict:
    rec = {"status": status, "kind": kind, "local_id": local_id,
           "started": started if started is not None else iso()}
    hub_api.HUB_JOBS[job_id] = rec
    return rec


def main() -> int:                                          # noqa: C901 —— 探针按组平铺
    print("=== [1] 写入点收敛（静态合同） ===")
    running_hits = SOURCE.count('"status": "running"')
    check("[1a] 创建态 status=running 只出现在 _spawn / _spawn_process 两处",
          running_hits == 2,
          f"命中 {running_hits} 处")
    done_hits = SOURCE.count('"status": "done"')
    failed_hits = SOURCE.count('"status": "failed"')
    check("[1b] 源码里没有任何 status=done/failed 直写（终态只经 _finalize_job）",
          done_hits == 0 and failed_hits == 0,
          f"done={done_hits} failed={failed_hits}")
    check("[1c] HUB_JOBS 记录创建点 = 2（_spawn / _spawn_process）",
          SOURCE.count("HUB_JOBS[job_id] = {") == 2,
          f"命中 {SOURCE.count('HUB_JOBS[job_id] = {')} 处")
    check("[1d] setdefault 补记录只在 _finalize_job 里",
          SOURCE.count("HUB_JOBS.setdefault(") == 1
          and "HUB_JOBS.setdefault(" in inspect.getsource(hub_api._finalize_job),
          f"命中 {SOURCE.count('HUB_JOBS.setdefault(')} 处")
    check("[1e] _active_submit 走统一存活谓词（不再自看 status==running）",
          "_job_alive(" in inspect.getsource(hub_api._active_submit)
          and '"running"' not in inspect.getsource(hub_api._active_submit),
          inspect.getsource(hub_api._active_submit))
    check("[1f] _finish_running_jobs 经 _finalize_job 落 force_finished",
          "_finalize_job(" in inspect.getsource(hub_api._finish_running_jobs),
          "未走统一出口")
    for name, fn in (("_retry_guard", hub_api._retry_guard), ("delete_task", hub_api.delete_task),
                     ("_held_job", hub_api._held_job)):
        src = inspect.getsource(fn)
        check(f"[1g] {name} 的存活判定同源（_job_blocks_retry/_job_alive）",
              "_job_blocks_retry(" in src or "_job_alive(" in src, src[:160])

    print("=== [2] _finalize_job 合同 ===")
    try:
        hub_api._finalize_job("probe-not-exist", "running")
        rejected = False
    except ValueError:
        rejected = True
    check("[2a] 拒绝把人写成终态 running（ValueError）", rejected, "未抛 ValueError")
    rec = new_record("j-final-1")
    ident = id(rec)
    hub_api._finalize_job("j-final-1", "done", result={"ok": True})
    check("[2b] 就地 update：同一 dict 对象（后台线程手持的引用不作废）",
          hub_api.HUB_JOBS["j-final-1"] is rec and id(hub_api.HUB_JOBS["j-final-1"]) == ident, "")
    check("[2c] 终态落 status/ended/result", rec.get("status") == "done"
          and bool(rec.get("ended")) and rec.get("result") == {"ok": True}, json.dumps(rec)[:160])
    fin = new_record("j-final-2", status="force_finished")
    fin.update({"finished_at": 1, "finish_reason": "manual_force_finish"})
    hub_api._finalize_job("j-final-2", "done", result="late")
    check("[2d] 首个终态权威：force_finished 不被晚到的 done 翻回",
          fin.get("status") == "force_finished" and fin.get("terminal_conflict") == "force_finished→done",
          json.dumps(fin, ensure_ascii=False)[:200])
    before = set(hub_api.HUB_JOBS)
    none_rec = hub_api._finalize_job("probe-absent", "failed")
    check("[2e] 记录不存在且无 local_id/kind → 不凭空造记录",
          none_rec is None and set(hub_api.HUB_JOBS) == before, f"{none_rec}")

    print("=== [3] _spawn 三出口：绝不留 running ===")
    job = hub_api._spawn("probe-kind", lambda: {"ok": 1})
    rec = wait_done(job["job_id"])
    check("[3a] 正常返回 → done + result", rec.get("status") == "done"
          and rec.get("result") == {"ok": 1}, json.dumps(rec)[:160])

    def boom() -> None:
        raise RuntimeError("probe 目标异常")

    job = hub_api._spawn("probe-kind", boom)
    rec = wait_done(job["job_id"])
    check("[3b] 普通异常 → failed + error/stderr（脱敏原文进落账面）",
          rec.get("status") == "failed" and "probe 目标异常" in str(rec.get("error"))
          and bool(rec.get("stderr")), json.dumps(rec, ensure_ascii=False)[:200])

    orig_hook = threading.excepthook
    threading.excepthook = lambda args: None                # SystemExit 冒泡不刷屏
    try:
        def exit_now() -> None:
            raise SystemExit(3)

        job = hub_api._spawn("probe-kind", exit_now)
        rec = wait_done(job["job_id"])
    finally:
        threading.excepthook = orig_hook
    check("[3c] BaseException（SystemExit）→ failed，不吞不悬", rec.get("status") == "failed",
          json.dumps(rec, ensure_ascii=False)[:200])

    orig_start = threading.Thread.start

    def no_thread(self):                                     # noqa: ANN001
        raise RuntimeError("can't start new thread (probe)")

    threading.Thread.start = no_thread
    try:
        raised = ""
        try:
            hub_api._spawn("probe-kind", lambda: None)
        except hub_api.HubError as exc:
            raised = str(exc)
    finally:
        threading.Thread.start = orig_start
    spawn_recs = [r for r in hub_api.HUB_JOBS.values() if r.get("kind") == "probe-kind"]
    last = spawn_recs[-1] if spawn_recs else {}
    check("[3d] 线程起不来 → 抛 HubError（一次都没跑不说成功）",
          "后台线程起不来" in raised, raised or "未抛")
    check("[3d] 线程起不来 → 记录当场 failed，没有 running 残留",
          last.get("status") == "failed", json.dumps(last, ensure_ascii=False)[:200])

    print("=== [4] 周期心跳（长跑里的付费闸/held_job 证据） ===")
    beat_dir = TMP_ROOT / "auto-9701"
    beat_dir.mkdir(parents=True, exist_ok=True)
    (beat_dir / "任务.json").write_text(json.dumps(
        {"local_id": "auto-9701", "state": "approved", "created_at": iso(-60)},
        ensure_ascii=False), encoding="utf-8")
    orig_beat = hub_api.HEARTBEAT_SECONDS
    hub_api.HEARTBEAT_SECONDS = 0.15
    try:
        job = hub_api._spawn("probe-kind", lambda: time.sleep(0.6), heartbeat_dir=beat_dir)
        rec = wait_done(job["job_id"], timeout=6)
    finally:
        hub_api.HEARTBEAT_SECONDS = orig_beat
    data = hub_api.state.heartbeat_read(beat_dir)
    check("[4a] 目标体无任何回调也照样续打心跳（beat_count ≥ 2）",
          int(data.get("beat_count") or 0) >= 2 and bool(data.get("last_beat_at")),
          json.dumps(data, ensure_ascii=False)[:200])
    check("[4b] 心跳 run_id 恒等于本次 job（栅栏不被别的 run 顶掉）",
          str(data.get("run_id") or "") == job["job_id"], f"{data.get('run_id')} vs {job['job_id']}")
    check("[4c] 收尾后记录终态 done", rec.get("status") == "done", json.dumps(rec)[:160])

    print("=== [5] _reap 三出口 ===")

    class FakeProc:
        def __init__(self, code=None, exc: Exception | None = None, pid: int = 4321):
            self._code, self._exc, self.pid = code, exc, pid

        def wait(self):
            if self._exc is not None:
                raise self._exc
            return self._code

    new_record("j-reap-0", local_id="auto-9702", kind="hub-discuss", status="running")
    hub_api._reap(FakeProc(0), "j-reap-0", "auto-9702")
    rec = hub_api.HUB_JOBS["j-reap-0"]
    check("[5a] exit 0 → done + exit_code/pid/mode 落账", rec.get("status") == "done"
          and rec.get("exit_code") == 0 and rec.get("pid") == 4321 and rec.get("mode") == "process",
          json.dumps(rec)[:200])
    new_record("j-reap-3", local_id="auto-9702", kind="hub-discuss", status="running")
    hub_api._reap(FakeProc(3), "j-reap-3", "auto-9702")
    rec = hub_api.HUB_JOBS["j-reap-3"]
    check("[5b] exit 3 → failed + 点名退出码（可诊断）",
          rec.get("status") == "failed" and "退出码 3" in str(rec.get("error")),
          json.dumps(rec, ensure_ascii=False)[:200])
    new_record("j-reap-x", local_id="auto-9702", kind="hub-discuss", status="running")
    hub_api._reap(FakeProc(exc=OSError("probe wait 崩")), "j-reap-x", "auto-9702")
    rec = hub_api.HUB_JOBS["j-reap-x"]
    check("[5c] wait() 抛错 → 仍落 failed，不停在 running",
          rec.get("status") == "failed" and "等待执行体失败" in str(rec.get("error")),
          json.dumps(rec, ensure_ascii=False)[:200])

    print("=== [6] _spawn_process 派发失败落终态 ===")
    proc_dir = TMP_ROOT / "auto-9703"
    proc_dir.mkdir(parents=True, exist_ok=True)
    (proc_dir / "任务.json").write_text(json.dumps(
        {"local_id": "auto-9703", "state": "discussing", "created_at": iso(-30)},
        ensure_ascii=False), encoding="utf-8")
    out = hub_api._spawn_process(proc_dir, kind="hub-discuss",
                                 argv_builder=lambda _rid: (_ for _ in ()).throw(
                                     ValueError("probe argv 构造失败")))
    rec = hub_api.HUB_JOBS.get(out.get("job_id") or "") or {}
    check("[6a] 命令行构造失败 → 记录 failed（派发失败即终态）",
          rec.get("status") == "failed", json.dumps(rec, ensure_ascii=False)[:200])
    check("[6b] 回执如实带 error，且没有 pid（没起进程不说起过）",
          "命令行构造失败" in str(out.get("error")) and out.get("pid") is None, f"{out}")
    check("[6c] 该任务名下没有 running 残留",
          not [r for r in hub_api.HUB_JOBS.values()
               if r.get("local_id") == "auto-9703" and r.get("status") == "running"],
          json.dumps(hub_api.HUB_JOBS, ensure_ascii=False)[:300])

    print("=== [7] _active_submit：统一谓词（付费闸不被僵尸记录锁死） ===")
    hub_api.HUB_JOBS.clear()
    new_record("j-fresh", local_id="auto-9704", kind="hub-submit", started=iso())
    check("[7a] 启动不足 60s 的真 running → 活跃（付费闸保留）",
          hub_api._active_submit("auto-9704") is True, "")
    new_record("j-stale", local_id="auto-9705", kind="hub-submit", started=iso(-5000))
    check("[7b] 心跳超 900s 的僵尸 running → 不活跃（不再永久挡提交）",
          hub_api._active_submit("auto-9705") is False, "")
    beat2 = TMP_ROOT / "auto-9706"
    beat2.mkdir(parents=True, exist_ok=True)
    (beat2 / "心跳.json").write_text(json.dumps(
        {"schema": 1, "run_id": "j-beat", "started_at": iso(-3000),
         "last_beat_at": iso(-5), "beat_count": 9}, ensure_ascii=False), encoding="utf-8")
    new_record("j-beat", local_id="auto-9706", kind="hub-submit", started=iso(-3000))
    check("[7c] 启动早但心跳新鲜（真在跑）→ 活跃（不放宽）",
          hub_api._active_submit("auto-9706") is True, "")
    new_record("j-ff", local_id="auto-9707", kind="hub-submit", status="force_finished")
    new_record("j-done", local_id="auto-9708", kind="hub-submit", status="done")
    check("[7d] force_finished / done 不算活跃",
          hub_api._active_submit("auto-9707") is False
          and hub_api._active_submit("auto-9708") is False, "")

    print("=== [8] _finish_running_jobs（人工强制结束的内存半边） ===")
    run_rec = new_record("j-ff-1", local_id="auto-9709", kind="hub-discuss")
    done_rec = new_record("j-ff-2", local_id="auto-9709", kind="hub-render", status="done")
    finished = hub_api._finish_running_jobs("auto-9709", note="probe 人工强制结束")
    check("[8a] 只清 running：返回命中的 job_id，已终态记录原样",
          finished == ["j-ff-1"] and done_rec.get("status") == "done", f"{finished}")
    check("[8b] 就地置 force_finished + finished_at/finish_reason/finished_by/ended",
          hub_api.HUB_JOBS["j-ff-1"] is run_rec
          and run_rec.get("status") == "force_finished"
          and isinstance(run_rec.get("finished_at"), int)
          and run_rec.get("finish_reason") == "manual_force_finish"
          and run_rec.get("finished_by") == hub_api.FORCE_FINISH_FINISHED_BY
          and bool(run_rec.get("ended")), json.dumps(run_rec, ensure_ascii=False)[:240])
    check("[8c] 强制结束后该任务立刻不算活跃（重试/删除/提交三闸同时解锁）",
          hub_api._active_submit("auto-9709") is False, "")
    again = hub_api._finish_running_jobs("auto-9709", note="probe 再调一次")
    check("[8d] 重复调用幂等：无可改、不抛异常、不重复计入",
          again == [] and run_rec.get("status") == "force_finished", f"{again}")

    # [7] 组故意留置的「活跃」夹具（j-fresh/j-stale/j-beat）是判据输入、不是被测代码留下的残留：
    # 清掉它们，[9] 的「零 running 残留」才只考核真正走创建/收尾路径的记录。
    for planted in ("j-fresh", "j-stale", "j-beat"):
        hub_api.HUB_JOBS.pop(planted, None)

    print("=== [9] 隔离与自清理 ===")
    after = {"projects": snapshot(REAL_PROJECTS), "analysis": snapshot(REAL_ANALYSIS),
             "uploads": snapshot(REAL_UPLOADS)}
    for key in BEFORE:
        check(f"[9] 真实 {key}/ 顶层集合跑前跑后一致", BEFORE[key] == after[key],
              f"-{sorted(BEFORE[key] - after[key])} +{sorted(after[key] - BEFORE[key])}")
    check("[9] 任务根自述 = 本探针隔离根（没有写进真实 projects/）",
          str(hub_api.TASKS_DIR) == str(TMP_ROOT), f"{hub_api.TASKS_DIR} vs {TMP_ROOT}")
    leftover = [jid for jid, r in hub_api.HUB_JOBS.items() if r.get("status") == "running"]
    check("[9] 全流程结束后 HUB_JOBS 里没有任何 running 残留", not leftover, f"{leftover}")

    passed = sum(1 for _, ok in RESULTS if ok)
    print(f"\n{'ALL PASS' if passed == len(RESULTS) else 'FAILED'} —— {passed}/{len(RESULTS)}")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    try:
        code = main()
    finally:
        hub_api.HUB_JOBS.clear()
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
    sys.exit(code)
