#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""start_retry 活跃 job 守卫探针（方案 B）：带豁免的统一存活判定 + 可解释拒绝文案。

跑法（wanrepro/ 下）：`python automation/probe_retry_guard.py`，退出码 0 = 全绿。

锁的是卡面三条验收（不是函数返回值好看）：

  [1] 残留记录不再挡重试：`status=force_finished`（方案 A 写下的人工已结束）、
      `status=failed` / `done` 残留、心跳超 900s 的僵尸 `running` —— 四种形态都放行，
      且响应 `ignored_stale_jobs` 逐条说明「哪个 job、什么状态、为什么没阻断」；
  [2] 真活跃守卫仍在：启动不足 60s 的 running、心跳新鲜的 running 依旧 409，
      文案含 job_id / status / started（及距今时长）/ 命中依据；
  [3] 口径唯一：守卫判据与看门狗/删除合同共用 `_job_blocks_retry`（`_job_alive` 是其布尔
      投影），对同一批夹具两处结论必须一致——「看门狗认死、重试认活」的分叉不许复活；
  [4] 正常路径零回归：无任何 job 记录时照旧重试成功（state=discussing、心跳重置、
      previous_reason 回显），非 failed 任务照旧被状态文案拦住，守卫顺序仍是「先 job 后状态」。

隔离手法：`WANREPRO_AUTO_DIR` → 本次临时根（任务根唯一来源 automation.config.PROJECTS_DIR），
夹具一律 auto-96xx；`_spawn_discuss` 在**守卫之后**的派发边界被替身接住（不起子进程、不连桥），
本探针验证的就是守卫判定本身；跑完整个临时根删除，真实 projects/analysis/uploads 顶层集合
跑前跑后逐字比对。
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
TMP_ROOT = Path(tempfile.mkdtemp(prefix="probe-retry-guard-"))
REAL_PROJECTS = ROOT / "automation" / "projects"
REAL_ANALYSIS = ROOT / "analysis" / "reverse"
REAL_UPLOADS = ROOT / "uploads"

os.environ["WANREPRO_AUTO_DIR"] = str(TMP_ROOT)               # 必须在 import hub_api 之前
os.environ["WANREPRO_BRIDGE"] = "http://127.0.0.1:9"           # 本探针不连桥（派发被替身接住）
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ui"))

import hub_api  # noqa: E402

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


def seed(local_id: str, state_name: str = "failed", *, heartbeat_at: str | None = None,
         reason: str = "拆解失败（夹具）") -> Path:
    """建一条隔离根内的任务；heartbeat_at 非 None 时写心跳侧车（守卫的第二个证据源）。"""
    d = TMP_ROOT / local_id
    d.mkdir(parents=True, exist_ok=True)
    task = {"local_id": local_id, "state": state_name, "created_at": iso(-5000),
            "intent": "retry 守卫探针夹具（隔离根内）",
            "history": [{"at": iso(-600), "from": "ingested", "to": state_name, "note": "夹具"}]}
    if state_name == "failed":
        task["failed_reason"] = reason
    (d / "任务.json").write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
    if heartbeat_at is not None:
        (d / "心跳.json").write_text(json.dumps({
            "schema": 1, "run_id": "probe-run", "started_at": iso(-1200),
            "last_beat_at": heartbeat_at, "beat_count": 3, "phase": "夹具",
            "artifact_count": 0, "artifact_digest": "", "ended_at": "", "outcome": "",
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    return d


SPAWNED: list[tuple[str, str]] = []


def fake_spawn(task_dir: Path, first_line: str) -> dict:
    """派发边界替身：只登记「守卫放行了」，不起子进程（守卫之后才被调用）。"""
    SPAWNED.append((Path(task_dir).name, first_line))
    return {"job_id": f"probe-spawn-{len(SPAWNED)}"}


hub_api._spawn_discuss = fake_spawn

JOBS: dict[str, dict] = {}


def job(job_id: str, local_id: str, status: str, *, age: int, kind: str = "hub-discuss") -> dict:
    rec = {"status": status, "kind": kind, "started": iso(-age), "local_id": local_id}
    JOBS[job_id] = rec
    return rec


def reset_jobs(*recs: dict) -> None:
    hub_api.HUB_JOBS.clear()
    for rec in recs:
        hub_api.HUB_JOBS[f"j{len(hub_api.HUB_JOBS) + 1}"] = rec


def retry(local_id: str) -> tuple[str, dict]:
    """调一次 start_retry：(放行返回的 dict 或空, {'error': 文案})。"""
    try:
        return "ok", hub_api.start_retry(local_id)
    except hub_api.HubError as exc:
        return "err", {"error": str(exc)}


def main() -> int:
    print("=== [0] 修复前必红：同一批夹具在旧口径（纯 status==running）下会被误拒 ===")
    # 旧实现 = `_job_running_records`（:280）纯 status 计数，无任何豁免。用同一组夹具跑旧口径，
    # 得到「旧实现会命中阻断」的条数——这就是修复前的红；修复后走的是豁免判据（绿）。
    OLD_FIXTURES = [("force_finished", 30), ("failed", 90), ("done", 120), ("running", 2100)]
    old_blocked = sum(1 for st, age in OLD_FIXTURES if st == "running")
    new_blocked = sum(1 for st, age in OLD_FIXTURES
                      if hub_api._job_blocks_retry({"status": st, "started": iso(-age),
                                                    "kind": "hub-discuss"}, heartbeat_at=""))
    check("[0] 旧口径对残留记录判 1 条阻断（修复前必被拒）", old_blocked == 1, f"{old_blocked}")
    check("[0] 新判据对同一批记录判 0 条阻断（修复后放行）", new_blocked == 0, f"{new_blocked}")

    print("=== [1] 残留记录不再挡重试（带豁免的统一存活判定）===")
    # ① force_finished（方案 A 的人工已结束）：心跳侧车还新鲜也照样放行
    local = "auto-9601"
    seed(local, "failed", heartbeat_at=iso(-5))
    rec = job("ff-1", local, "force_finished", age=30)
    reset_jobs(rec)
    verdict, resp = retry(local)
    check("[1a] force_finished 残留 → 重试放行", verdict == "ok", f"{verdict} {resp}")
    check("[1a] 放行即真派发（守卫之后的 _spawn_discuss 被调用）",
          SPAWNED and SPAWNED[-1][0] == local, f"{SPAWNED[-3:]}")
    check("[1a] 响应回显被豁免记录（status 可见，可追溯）",
          verdict == "ok" and any("force_finished" in s for s in resp.get("ignored_stale_jobs") or []),
          f"{resp.get('ignored_stale_jobs')}")
    check("[1a] 心跳新鲜但 status != running 时不阻断（口径只看存活，不看侧车）",
          hub_api._job_alive(rec, heartbeat_at=iso(-5)) is False)

    # ② failed / done 残留
    local = "auto-9602"
    seed(local, "failed", heartbeat_at=iso(-5))
    reset_jobs(job("f-1", local, "failed", age=90), job("d-1", local, "done", age=120))
    verdict, resp = retry(local)
    check("[1b] failed/done 残留 → 重试放行", verdict == "ok", f"{verdict} {resp}")
    check("[1b] 两条残留都进了 ignored_stale_jobs",
          len(resp.get("ignored_stale_jobs") or []) == 2, f"{resp.get('ignored_stale_jobs')}")

    # ③ 心跳超 900s 的僵尸 running（进程早死或强杀后没回写）
    local = "auto-9603"
    seed(local, "failed", heartbeat_at=iso(-2000))
    rec = job("z-1", local, "running", age=2100)
    reset_jobs(rec)
    verdict, resp = retry(local)
    check("[1c] 心跳超 900s 的僵尸 running → 重试放行", verdict == "ok", f"{verdict} {resp}")
    check("[1c] 放行的是僵尸记录（ignored 里写清 status=running）",
          any("running" in s for s in resp.get("ignored_stale_jobs") or []),
          f"{resp.get('ignored_stale_jobs')}")

    # ④ 无心跳侧车的 running（Thread.start() 失败的孤儿 / 老任务）
    local = "auto-9604"
    seed(local, "failed")                       # 没有 心跳.json
    reset_jobs(job("o-1", local, "running", age=1500))
    verdict, resp = retry(local)
    check("[1d] 无心跳且启动久远的 running → 重试放行", verdict == "ok", f"{verdict} {resp}")

    print("=== [2] 真活跃守卫仍在（心跳新鲜的真 running 依旧拒绝）===")
    local = "auto-9611"
    seed(local, "failed", heartbeat_at=iso(-10))
    job("live-1", local, "running", age=400)   # 启动 400s（超 60s 宽限），靠心跳活着
    reset_jobs(JOBS["live-1"])
    n_before = len(SPAWNED)
    verdict, resp = retry(local)
    err = str(resp.get("error") or "")
    check("[2a] 心跳 10s 的 running → 409 拒绝", verdict == "err" and "重试拒绝" in err, f"{verdict} {err}")
    check("[2a] 拒绝后没有派发新 job", len(SPAWNED) == n_before, f"{SPAWNED[n_before:]}")
    check("[2a] 文案含 job_id / status=running", "job=j1" in err and "status=running" in err, err)
    check("[2a] 文案含 started 时刻与距今时长",
          "started=" in err and "已运行" in err and "s" in err.split("已运行", 1)[1], err)
    check("[2a] 文案含命中依据（心跳 ≤900s）", "心跳距今" in err and "900s" in err, err)

    # 启动不足 60s、心跳侧车仍是旧值的真正起跑窗口
    local = "auto-9612"
    seed(local, "failed", heartbeat_at=iso(-4000))
    reset_jobs(job("fresh-1", local, "running", age=8))
    verdict, resp = retry(local)
    err = str(resp.get("error") or "")
    check("[2b] 启动 8s 的 running（60s 宽限期内）→ 409 拒绝", verdict == "err", f"{verdict} {err}")
    check("[2b] 命中依据写明启动宽限期", "启动至今" in err and "60s" in err, err)

    print("=== [3] 口径唯一：守卫判据 == 看门狗/删除合同判据 ===")
    parity_ok, parity_bad = True, []
    for rec in list(JOBS.values()):
        for beat in (iso(-5), iso(-2000)):
            a = hub_api._job_alive(rec, heartbeat_at=beat)
            b = bool(hub_api._job_blocks_retry(rec, heartbeat_at=beat))
            if a is not b:
                parity_ok = False
                parity_bad.append(f"{rec.get('status')}/{rec.get('started')}/{beat}")
    check("[3a] _job_alive 与 _job_blocks_retry 对全部夹具逐一一致", parity_ok, str(parity_bad))
    check("[3a] _job_alive 是 _job_blocks_retry 的布尔投影（单一判据源）",
          "bool(_job_blocks_retry" in (Path(hub_api.__file__).read_text(encoding="utf-8")))
    check("[3b] start_retry 不再按 status==running 裸判",
          "_job_running_records(local_id)" not in
          Path(hub_api.__file__).read_text(encoding="utf-8").split("def start_retry", 1)[1]
          .split("def do_assets", 1)[0])

    print("=== [4] 正常路径零回归 ===")
    local = "auto-9621"
    seed(local, "failed", heartbeat_at=iso(-60), reason="云端 429（夹具原文）")
    reset_jobs()
    verdict, resp = retry(local)
    check("[4a] 无 job 记录 → 重试成功（正常路径）", verdict == "ok" and resp.get("state") == "discussing",
          f"{verdict} {resp}")
    check("[4a] 心跳重置 + 旧失败原因回显",
          resp.get("heartbeat_reset") is True and resp.get("previous_reason") == "云端 429（夹具原文）",
          f"{resp}")
    check("[4a] 无豁免记录时 ignored_stale_jobs 为空", resp.get("ignored_stale_jobs") == [],
          f"{resp.get('ignored_stale_jobs')}")

    local = "auto-9622"
    seed(local, "discussing", heartbeat_at=iso(-10))
    reset_jobs(job("live-2", local, "running", age=200))
    verdict, resp = retry(local)
    err = str(resp.get("error") or "")
    check("[4b] 守卫顺序仍是「先 job 后状态」：discussing + 活 job → 报 job",
          verdict == "err" and "拆解任务在跑" in err and "重试需 failed" not in err, err)

    local = "auto-9623"
    seed(local, "discussing", heartbeat_at=iso(-10))
    reset_jobs(job("ff-2", local, "force_finished", age=30))
    verdict, resp = retry(local)
    err = str(resp.get("error") or "")
    check("[4c] 非 failed 且残留已豁免 → 落到状态文案（豁免不放宽状态机）",
          verdict == "err" and "重试需 failed 状态" in err, err)

    print("=== [5] 与方案 A 合体：真调 force_finish 清退内存记录 → 重试立刻放行 ===")
    local = "auto-9631"
    seed(local, "discussing", heartbeat_at=iso(-5))
    reset_jobs(job("ff-real", local, "running", age=120))
    fin = hub_api.force_finish(local, confirm=True, confirm_name=local, reason="探针：人工强制结束")
    check("[5a] force_finish 把任务置 failed（changed=true）",
          fin.get("state") == "failed" and fin.get("changed") is True, f"{fin}")
    check("[5a] 内存 running 记录被置终态 force_finished",
          (hub_api.HUB_JOBS.get("j1") or {}).get("status") == "force_finished",
          f"{hub_api.HUB_JOBS}")
    verdict, resp = retry(local)
    check("[5b] 强制结束后立刻重试 → 放行（A 清退 + B 豁免协同）", verdict == "ok", f"{verdict} {resp}")
    check("[5b] 放行回执里能看到被豁免的那条 force_finished",
          any("force_finished" in s for s in resp.get("ignored_stale_jobs") or []),
          f"{resp.get('ignored_stale_jobs')}")

    # 幂等：连点两次强制结束后再重试，仍须走通（第二次是 already=true，不许把记录复辟成 running）
    again = hub_api.force_finish(local, confirm=True, confirm_name=local, reason="探针：再点一次")
    check("[5c] 第二次 force_finish 幂等（already=true / changed=false）",
          again.get("already") is True and again.get("changed") is False, f"{again}")
    check("[5c] 幂等重入后记录仍是 force_finished（没有被复辟成 running）",
          (hub_api.HUB_JOBS.get("j1") or {}).get("status") == "force_finished",
          f"{hub_api.HUB_JOBS.get('j1')}")
    verdict, resp = retry(local)
    check("[5c] 连续两次强制结束后再重试 → 仍成功", verdict == "ok", f"{verdict} {resp}")

    print("=== [6] 隔离与自清理 ===")
    after = {"projects": snapshot(REAL_PROJECTS), "analysis": snapshot(REAL_ANALYSIS),
             "uploads": snapshot(REAL_UPLOADS)}
    for key in BEFORE:
        check(f"[6] 真实 {key}/ 顶层集合跑前跑后一致", BEFORE[key] == after[key],
              f"-{sorted(BEFORE[key] - after[key])} +{sorted(after[key] - BEFORE[key])}")
    check("[6] 任务根自述 = 本探针隔离根（没有写进真实 projects/）",
          str(hub_api.TASKS_DIR) == str(TMP_ROOT), f"{hub_api.TASKS_DIR} vs {TMP_ROOT}")

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
