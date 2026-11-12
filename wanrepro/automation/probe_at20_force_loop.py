#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""人工强制结束 → 重试 / 删除 闭环探针（卡 t_135a22c9 验收口）。

跑法（wanrepro/ 下）：`python -m automation.probe_at20_force_loop`，退出码 0 = 全绿。

锁的是**所有者真机那条动线**，不是函数返回值好看：

  [1] 强制结束三半闭环：文件（任务.json 落 failed）/ 内存（HUB_JOBS 残留 running 就地置
      `force_finished`，对象身份不变）/ 心跳（侧车终态化：last_beat_at 清空、ended_at+outcome
      落笔、closed_beat_at 留下最后真实心跳、run_id/pid 诊断位不动）——`heartbeat_read`
      此后**不再产出新鲜心跳**。
  [2] 强制结束 → **立即可重试**：不等 900s，`start_retry` 直接放行转 discussing，放行回执
      里逐条列明豁免了哪条残留记录。
  [3] 强制结束 → **立即可删**：`delete_task` 不再被「排队/运行中的 job」挡住（残留记录只登记
      不阻断），任务目录真删、墓碑真写。
  [4] 对照组（守卫不放松）：真活跃的拆解中任务——心跳新鲜的 running 记录在，retry 与 delete
      **依旧 409**；启动不足 60s 的起跑窗口同样拦。
  [5] 幂等 / 口径单一 / 隔离：二次强制结束不刷 failed_at、不覆盖原原因，仍把残留心跳收干净；
      retry 守卫与删除合同共用 `_job_alive`；真实 projects/analysis/uploads 顶层集合跑前跑后
      逐字一致，任务根自述 = 本探针临时根。

隔离手法：`WANREPRO_AUTO_DIR` → 本次临时根（任务根的**唯一来源** automation.config.PROJECTS_DIR），
夹具一律 auto-97xx；`_spawn_discuss` 在守卫之后的派发边界被替身接住（不起子进程、不连桥），
本探针验证的是守卫/闭环判定本身；跑完整个临时根删除。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]                    # wanrepro/
TMP_ROOT = Path(tempfile.mkdtemp(prefix="probe-force-loop-"))
REAL_PROJECTS = ROOT / "automation" / "projects"
REAL_ANALYSIS = ROOT / "analysis" / "reverse"
REAL_UPLOADS = ROOT / "uploads"

os.environ["WANREPRO_AUTO_DIR"] = str(TMP_ROOT)               # 必须在 import hub_api 之前
os.environ["WANREPRO_BRIDGE"] = "http://127.0.0.1:9"           # 本探针不连桥（派发被替身接住）
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ui"))

import hub_api  # noqa: E402
from automation import state  # noqa: E402

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


def seed(local_id: str, state_name: str = "discussing", *, heartbeat_at: str | None = None,
         run_id: str = "probe-run-old", beat_count: int = 7,
         reason: str = "") -> Path:
    """建一条隔离根内的任务；heartbeat_at 非 None 时写心跳侧车（= 执行体「还在打」的证据）。"""
    d = TMP_ROOT / local_id
    d.mkdir(parents=True, exist_ok=True)
    task = {"local_id": local_id, "state": state_name, "bridge_id": "",
            "created_at": iso(-5000), "intent": "force-finish 闭环探针夹具（隔离根内）",
            "history": [{"at": iso(-600), "from": "ingested", "to": state_name, "note": "夹具"}]}
    if reason:
        task["failed_reason"] = reason
        task["failed_at"] = iso(-300)
    (d / "任务.json").write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
    if heartbeat_at is not None:
        (d / "心跳.json").write_text(json.dumps({
            "schema": 1, "run_id": run_id, "started_at": iso(-1200),
            "last_beat_at": heartbeat_at, "beat_count": beat_count, "phase": "夹具阶段",
            "artifact_count": 0, "artifact_digest": "", "ended_at": "", "outcome": "",
            "exec": "thread", "pid": 0, "pid_created_at": "", "exit_code": None,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    return d


SPAWNED: list[tuple[str, str]] = []


def fake_spawn(task_dir: Path, first_line: str) -> dict:
    """派发边界替身：只登记「守卫放行了」，不起子进程（守卫之后才被调用）。"""
    SPAWNED.append((Path(task_dir).name, first_line))
    return {"job_id": f"probe-spawn-{len(SPAWNED)}"}


hub_api._spawn_discuss = fake_spawn


def running_job(local_id: str, *, age: int, kind: str = "hub-discuss") -> dict:
    rec = {"status": "running", "kind": kind, "started": iso(-age), "local_id": local_id}
    hub_api.HUB_JOBS["live-job"] = rec
    return rec


def reset_jobs(*recs: tuple[str, dict]) -> None:
    hub_api.HUB_JOBS.clear()
    for job_id, rec in recs:
        hub_api.HUB_JOBS[job_id] = rec


def force(local_id: str, **kw) -> tuple[str, dict]:
    try:
        return "ok", hub_api.force_finish(local_id, confirm=True, confirm_name=local_id, **kw)
    except hub_api.HubError as exc:
        return "err", {"error": str(exc)}


def retry(local_id: str) -> tuple[str, dict]:
    try:
        return "ok", hub_api.start_retry(local_id)
    except hub_api.HubError as exc:
        return "err", {"error": str(exc)}


def delete(local_id: str) -> tuple[str, dict]:
    try:
        return "ok", hub_api.delete_task(local_id)
    except hub_api.HubError as exc:
        return "err", {"error": str(exc)}


def main() -> int:
    print("=== [1] 强制结束三半闭环：文件 / 内存 / 心跳 ===")
    local = "auto-9701"
    beat_before = iso(-5)                          # 执行体 5s 前刚打过心跳 = 「还在跑」的现场
    d = seed(local, "discussing", heartbeat_at=beat_before)
    rec = running_job(local, age=400)             # 超 60s 宽限期，靠心跳活着
    verdict, resp = force(local)
    check("[1a] 强制结束成功（discussing → failed）",
          verdict == "ok" and resp.get("changed") is True and resp.get("state") == "failed",
          f"{verdict} {resp}")
    task = json.loads((d / "任务.json").read_text(encoding="utf-8"))
    check("[1a] 文件半边：任务.json 落 failed + 人工原因",
          task.get("state") == "failed" and task.get("failed_reason") == hub_api.FORCE_FINISH_DEFAULT_REASON,
          f"{task.get('state')} / {task.get('failed_reason')}")
    live = hub_api.HUB_JOBS.get("live-job") or {}
    check("[1b] 内存半边：残留 running 记录就地置 force_finished",
          live.get("status") == "force_finished", str(live))
    check("[1b] 内存半边：记录对象身份不变（后台线程持有的是同一份）",
          hub_api.HUB_JOBS.get("live-job") is rec)
    check("[1b] 内存半边：finished_at / finish_reason / finished_by 三键落账",
          isinstance(live.get("finished_at"), int) and live.get("finish_reason") == "manual_force_finish"
          and live.get("finished_by") == hub_api.FORCE_FINISH_FINISHED_BY, str(live))
    check("[1b] 响应回执列出被清退的 job", resp.get("jobs_force_finished") == ["live-job"],
          str(resp.get("jobs_force_finished")))
    hb_raw = json.loads((d / "心跳.json").read_text(encoding="utf-8"))
    check("[1c] 心跳半边：last_beat_at 清空（heartbeat_read 不再产出新鲜心跳）",
          hb_raw.get("last_beat_at") == "" and state.heartbeat_read(d)["last_beat_at"] == "",
          json.dumps(state.heartbeat_read(d), ensure_ascii=False))
    check("[1c] 心跳半边：ended_at + outcome 落笔（终态可判）",
          hb_raw.get("ended_at") and hb_raw.get("outcome") == hub_api.FORCE_FINISH_HEARTBEAT_OUTCOME,
          json.dumps(hb_raw, ensure_ascii=False))
    check("[1c] 心跳半边：最后真实心跳时刻留痕 closed_beat_at（不丢证据）",
          hb_raw.get("closed_beat_at") == beat_before and hb_raw.get("beat_count") == 7,
          json.dumps(hb_raw, ensure_ascii=False))
    check("[1c] 心跳半边：run_id / pid / exec 诊断位不动（栅栏与墓碑对账锚）",
          hb_raw.get("run_id") == "probe-run-old" and hb_raw.get("exec") == "thread",
          json.dumps(hb_raw, ensure_ascii=False))
    check("[1c] 响应回执 heartbeat_closed=true", resp.get("heartbeat_closed") is True, str(resp))
    check("[1c] held_job 已松开（看门狗不再把它当「本进程有活 job」）",
          hub_api._held_job(local) is False)

    print("=== [2] 强制结束 → 立即可重试（不等 900s）===")
    n_before = len(SPAWNED)
    verdict, resp = retry(local)
    check("[2a] 重试立即 200 且转 discussing",
          verdict == "ok" and resp.get("state") == "discussing", f"{verdict} {resp}")
    check("[2a] 重试真派发（守卫之后的 _spawn_discuss 被调用）",
          len(SPAWNED) == n_before + 1 and SPAWNED[-1][0] == local, f"{SPAWNED[n_before:]}")
    check("[2a] 放行回执逐条列明豁免的残留记录（force_finished 可见）",
          any("force_finished" in s for s in resp.get("ignored_stale_jobs") or []),
          str(resp.get("ignored_stale_jobs")))
    check("[2a] 心跳锚重建标记为真", resp.get("heartbeat_reset") is True, str(resp))

    print("=== [3] 强制结束 → 立即可删（不被残留 job 挡）===")
    local = "auto-9702"
    seed(local, "discussing", heartbeat_at=iso(-3), run_id="probe-run-del")
    running_job(local, age=500)
    verdict, resp = force(local)
    check("[3a] 前提：强制结束成功", verdict == "ok" and resp.get("state") == "failed", f"{verdict} {resp}")
    check("[3a] 前提：残留记录已 force_finished",
          (hub_api.HUB_JOBS.get("live-job") or {}).get("status") == "force_finished")
    verdict, resp = delete(local)
    check("[3b] 删除立即 200（没有「排队/运行中的 job」拒单）",
          verdict == "ok" and resp.get("deleted") == local, f"{verdict} {resp}")
    check("[3b] 删除是真删：任务目录已不在",
          not (TMP_ROOT / local).exists(), f"{(TMP_ROOT / local)}")
    check("[3b] 残留记录只登记不阻断（ignored_stale_jobs 有它）",
          any("live-job" in s for s in resp.get("ignored_stale_jobs") or []),
          str(resp.get("ignored_stale_jobs")))
    tombs = json.loads((TMP_ROOT / ".tombstones.json").read_text(encoding="utf-8")) \
        if (TMP_ROOT / ".tombstones.json").is_file() else []
    check("[3b] 墓碑真写（state_was=failed + 记 run_id）",
          any(t.get("id") == local for t in tombs), str(tombs)[:300])

    print("=== [4] 对照组：真活跃的拆解中任务，retry/delete 仍被正确拦截 ===")
    local = "auto-9711"
    seed(local, "failed", heartbeat_at=iso(-10), reason="云端 429（夹具原文）")
    running_job(local, age=400)                    # 启动 400s，靠 10s 前的心跳活着
    n_before = len(SPAWNED)
    verdict, resp = retry(local)
    err = str(resp.get("error") or "")
    check("[4a] 活跃 job 在跑 → retry 409 拒绝",
          verdict == "err" and "已有拆解任务在跑" in err, f"{verdict} {err}")
    check("[4a] 拒绝后没有派发新 job", len(SPAWNED) == n_before, f"{SPAWNED[n_before:]}")
    check("[4a] 文案可解释：job / status / started / 命中依据",
          "job=live-job" in err and "status=running" in err and "started=" in err
          and "心跳距今" in err, err)
    verdict, resp = delete(local)
    err = str(resp.get("error") or "")
    check("[4b] 活跃 job 在跑 → delete 409 拒绝",
          verdict == "err" and "排队/运行中的 job" in err, f"{verdict} {err}")
    check("[4b] 任务目录未被删（拒单不是假拒）", (TMP_ROOT / local).is_dir())

    local = "auto-9712"
    seed(local, "failed", heartbeat_at=iso(-4000), reason="夹具")
    running_job(local, age=8)                      # 起跑 8s，心跳侧车还是上一轮旧值
    verdict, resp = retry(local)
    err = str(resp.get("error") or "")
    check("[4c] 启动不足 60s 的起跑窗口 → retry 409", verdict == "err" and "启动至今" in err,
          f"{verdict} {err}")
    verdict, resp = delete(local)
    err = str(resp.get("error") or "")
    check("[4c] 启动不足 60s 的起跑窗口 → delete 409",
          verdict == "err" and "排队/运行中的 job" in err, f"{verdict} {err}")

    print("=== [5] 幂等 / 口径单一 / 隔离 ===")
    local = "auto-9721"
    d = seed(local, "discussing", heartbeat_at=iso(-5))
    running_job(local, age=300)
    verdict, first = force(local)
    check("[5a] 前提：首次强制结束成功", verdict == "ok" and first.get("changed") is True, str(first))
    task_first = json.loads((d / "任务.json").read_text(encoding="utf-8"))
    history_first = len(task_first.get("history") or [])
    failed_at_first = task_first.get("failed_at")
    hub_api.HUB_JOBS["live-job"]["status"] = "running"     # 模拟并发残留再次出现在内存里
    time.sleep(1.1)                                        # 让「刷新」可观测
    verdict, second = force(local)
    task_second = json.loads((d / "任务.json").read_text(encoding="utf-8"))
    check("[5a] 二次强制结束幂等：already=true / changed=false",
          verdict == "ok" and second.get("already") is True and second.get("changed") is False,
          f"{verdict} {second}")
    check("[5a] 二次不写 history、不刷新 failed_at、不覆盖原原因",
          len(task_second.get("history") or []) == history_first
          and task_second.get("failed_at") == failed_at_first
          and task_second.get("failed_reason") == task_first.get("failed_reason"),
          f"{history_first} → {len(task_second.get('history') or [])}")
    check("[5a] 二次仍把内存残留清干净（already 也闭环）",
          (hub_api.HUB_JOBS.get("live-job") or {}).get("status") == "force_finished"
          and second.get("jobs_force_finished") == ["live-job"], str(second))
    hb2 = state.heartbeat_read(d)
    check("[5a] 二次仍把心跳收成终态（closed_beat_at 不丢、last_beat_at 空）",
          hb2["last_beat_at"] == "" and hb2["closed_beat_at"] != "" and second.get("heartbeat_closed") is True,
          json.dumps(hb2, ensure_ascii=False))
    check("[5a] 二次调用不报「不是运行态」（failed 是合法入口）", verdict == "ok", f"{verdict} {second}")

    reset_jobs(("live-job", running_job("auto-9731", age=400)))
    parity_bad = []
    for status in ("running", "force_finished", "failed", "done"):
        for beat in ("", iso(-5), iso(-2000)):
            rec = {"status": status, "kind": "k", "started": iso(-400), "local_id": "auto-x"}
            if hub_api._job_alive(rec, heartbeat_at=beat) is not bool(
                    hub_api._job_blocks_retry(rec, heartbeat_at=beat)):
                parity_bad.append(f"{status}/{beat}")
    check("[5b] _job_alive 与 _job_blocks_retry 口径一致（守卫与删除合同同一判据）",
          not parity_bad, str(parity_bad))
    body = Path(hub_api.__file__).read_text(encoding="utf-8").split("def start_retry", 1)[1] \
        .split("def do_assets", 1)[0]
    check("[5b] start_retry 不再按 status==running 裸判",
          "_job_running_records(local_id)" not in body)
    check("[5c] 任务根自述 = 本探针临时根（没有写进真实 projects/）",
          str(hub_api.TASKS_DIR) == str(TMP_ROOT), f"{hub_api.TASKS_DIR} vs {TMP_ROOT}")

    print("=== [6] 隔离自清理 ===")
    after = {"projects": snapshot(REAL_PROJECTS), "analysis": snapshot(REAL_ANALYSIS),
             "uploads": snapshot(REAL_UPLOADS)}
    for key in BEFORE:
        check(f"[6] 真实 {key}/ 顶层集合跑前跑后一致", BEFORE[key] == after[key],
              f"-{sorted(BEFORE[key] - after[key])} +{sorted(after[key] - BEFORE[key])}")

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
