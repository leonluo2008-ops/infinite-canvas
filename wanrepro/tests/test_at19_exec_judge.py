# -*- coding: utf-8 -*-
"""AT-19 判据单测：judge_exec_dead 真值表 / 契约文件 / 唯一写盘核幂等（不联网、不写真实目录）。

跑法（wanrepro/ 下）：
    python tests/test_at19_exec_judge.py
退出码 0 = 全绿。夹具全在 tempfile 临时根，跑完删除。
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from automation import state  # noqa: E402

RESULTS: list[tuple[str, bool]] = []


def check(name: str, cond, detail: str = "") -> None:
    ok = bool(cond)
    RESULTS.append((name, ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail and not ok else ""))


TMP = Path(tempfile.mkdtemp(prefix="at19-judge-"))
RUN = "run-aaaaaaaaaaaa"


def task_dir(name: str, st: str = "discussing") -> Path:
    d = TMP / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "任务.json").write_text(json.dumps({
        "local_id": name, "state": st, "history": [], "bridge_id": f"probe-{name}",
        "exec_run_id": RUN, "ingestion": {"video_name": ""},
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return d


def view(**over) -> dict:
    base = {"heartbeat_run_id": RUN, "heartbeat_at": "2026-11-12T16:00:00+08:00",
            "started_at": "2026-11-12T15:59:00+08:00", "beat_count": 3, "exec": "child",
            "pid": 0, "pid_created_at": "", "pid_alive": None,
            "pid_actual_created_at": "", "exit_exists": False, "exit_run_id": "",
            "exit_code": None, "exit_outcome": "", "exit_error_tail": "", "exit_at": "",
            "task_exec_run_id": RUN}
    base.update(over)
    return base


print("\n[1] judge_exec_dead 真值表（11 条分支）")
dead, code = state.judge_exec_dead({"state": "failed"}, view())
check("[1] 非运行态 → not_running", (not dead) and code == "not_running", code)
dead, code = state.judge_exec_dead({"state": "generating"}, view(exit_exists=True,
                                                               exit_run_id=RUN, exit_code=3))
check("[1] generating 有退出码也不降级 → manual_only_state",
      (not dead) and code == "manual_only_state", code)
dead, code = state.judge_exec_dead({"state": "discussing"}, view(exec="", heartbeat_run_id=""))
check("[1] 旧任务无 exec 证据 → exec_no_evidence", (not dead) and code == "exec_no_evidence", code)
dead, code = state.judge_exec_dead({"state": "discussing"}, view(task_exec_run_id="run-new"))
check("[1] 心跳 run ≠ 任务 exec_run_id → exec_stale_run",
      (not dead) and code == "exec_stale_run", code)
dead, code = state.judge_exec_dead({"state": "discussing"},
                                  view(exit_exists=True, exit_run_id=RUN, exit_code=3))
check("[1] E1 退出码非 0 → exec_exit_written（不等 60s 宽限）",
      dead and code == "exec_exit_written", code)
dead, code = state.judge_exec_dead({"state": "discussing"},
                                  view(exit_exists=True, exit_run_id=RUN,
                                       exit_code=20, exit_outcome="env_error"))
check("[1] E1 但 outcome=env_error → 环境级不判死", (not dead) and code == "exec_env_error", code)
dead, code = state.judge_exec_dead({"state": "discussing"},
                                  view(exit_exists=True, exit_run_id=RUN, exit_code=0,
                                       exit_outcome="done"))
check("[1] 退出码 0（正常完成）不是死证", not dead, code)
dead, code = state.judge_exec_dead({"state": "discussing"}, view(pid=41888, pid_alive=False))
check("[1] E2 PID 已死 → exec_pid_gone（不看宽限）", dead and code == "exec_pid_gone", code)
dead, code = state.judge_exec_dead({"state": "discussing"},
                                  view(pid=41888, pid_alive=True,
                                       pid_created_at="2026-11-12T16:00:00.000+08:00",
                                       pid_actual_created_at="2026-11-12T18:00:00.000+08:00"))
check("[1] E2b creation time 差 > 2s → exec_pid_reused", dead and code == "exec_pid_reused", code)
dead, code = state.judge_exec_dead({"state": "discussing"},
                                  view(pid=41888, pid_alive=True,
                                       pid_created_at="2026-11-12T16:00:00.000+08:00",
                                       pid_actual_created_at="2026-11-12T16:00:00.500+08:00"))
check("[1] E2b 同一进程（差 ≤2s）→ exec_alive", (not dead) and code == "exec_alive", code)
dead, code = state.judge_exec_dead({"state": "discussing"}, view(pid=0, beat_count=0),
                                  now="2026-11-12T15:59:30+08:00")
check("[1] E3 未接管不足 60s → exec_grace", (not dead) and code == "exec_grace", code)
dead, code = state.judge_exec_dead({"state": "discussing"}, view(pid=0, beat_count=0),
                                  now="2026-11-12T16:05:00+08:00")
check("[1] E3 未接管过 60s → exec_never_attached", dead and code == "exec_never_attached", code)
dead, code = state.judge_exec_dead({"state": "discussing"}, view(pid=0, beat_count=5))
check("[1] pid=0 但有心跳（自相矛盾）→ exec_unknown 保守", (not dead) and code == "exec_unknown", code)
dead, code = state.judge_exec_dead({"state": "discussing"}, view(pid=41888, pid_alive=None))
check("[1] 探测失败 → exec_unknown", (not dead) and code == "exec_unknown", code)

print("\n[2] 契约文件：心跳 4 新键 round-trip / 退出码原子写 + run 栅栏")
D = task_dir("auto-9901")
state.heartbeat_begin(D, run_id=RUN, phase="hub-discuss", exec="child")
state.heartbeat_attach(D, run_id=RUN, pid=4242, pid_created_at="2026-11-12T17:00:00.000+08:00")
hb = state.heartbeat_read(D)
check("[2] heartbeat_read 读得到 exec/pid/pid_created_at/exit_code（X8 白名单已扩）",
      hb.get("exec") == "child" and hb.get("pid") == 4242
      and hb.get("pid_created_at", "").startswith("2026-11-12T17:00")
      and hb.get("exit_code") is None, str(hb))
check("[2] beat 带 wrong run_id 被栅栏拒绝（不写）",
      state.heartbeat_beat(D, expected_run_id="run-other") == {}
      and state.heartbeat_read(D)["beat_count"] == 0, str(state.heartbeat_read(D)))
check("[2] beat 带本 run → beat_count+1",
      state.heartbeat_beat(D, expected_run_id=RUN, pid=4242).get("beat_count") == 1)
state.exec_write(D, run_id=RUN, pid=4242, exit_code=3, outcome="failed", error_tail="boom")
ex = state.exec_read(D)
check("[2] 退出码文件字段齐全", ex["exists"] and ex["run_id"] == RUN and ex["exit_code"] == 3
      and ex["outcome"] == "failed" and ex["error_tail"] == "boom", str(ex))
state.exec_write(D, run_id="run-older", pid=1, exit_code=9, outcome="failed")
check("[2] 旧 run 不许覆盖新 run 的退出码", state.exec_read(D)["exit_code"] == 3,
      str(state.exec_read(D)))

print("\n[3] 产物/活动口径：退出码、锁、子进程日志不许顶替活动源")
D3 = task_dir("auto-9902")
base_digest = state.artifact_digest(D3)
(D3 / state.EXEC_EXIT_FILE).write_text('{"run_id":"x","exit_code":3}', encoding="utf-8")
(D3 / state.TASK_LOCK_FILE).write_text("", encoding="utf-8")
(D3 / state.CHILD_LOG_FILE).write_text("log line\n", encoding="utf-8")
check("[3] 三个新文件不进产物指纹", state.artifact_digest(D3) == base_digest)
probe = state.activity_snapshot(D3)
check("[3] 三个新文件不顶替 last_activity_at", probe["source"] != "任务目录产物",
      str(probe.get("sources")))

print("\n[4] 唯一写盘核：一次迁移 / 幂等 / 兜底句→原文升级 / run 栅栏")
D4 = task_dir("auto-9903", st="discussing")
view4 = view(pid=41888, pid_alive=False, heartbeat_at="2026-11-12T16:00:00+08:00")
res1 = state.fail_orphan_task(D4, reason="拆解进程失联，可重试或删除", probe={"exec": view4},
                              trigger="exec_dead", exec_view=view4)
t1 = json.loads((D4 / "任务.json").read_text(encoding="utf-8"))
check("[4] exec_dead 迁移成功", res1.get("changed") and t1["state"] == "failed", str(res1))
check("[4] 证据含 reason_code / PID / 心跳 / 退出码",
      t1["failed_evidence"].get("reason_code") == "exec_pid_gone"
      and t1["failed_evidence"]["exec"]["pid"] == 41888
      and t1["failed_evidence"]["exec"]["heartbeat_at"].startswith("2026-11-12T16:00"),
      str(t1["failed_evidence"]))
hist1 = len(t1["history"])
res2 = state.fail_orphan_task(D4, reason="拆解进程失联，可重试或删除", probe={"exec": view4},
                              trigger="exec_dead", exec_view=view4)
t2 = json.loads((D4 / "任务.json").read_text(encoding="utf-8"))
check("[4] 重复命中 → changed=False 且 history 不增",
      (not res2.get("changed")) and len(t2["history"]) == hist1, str(res2))
check("[4] failed_at 不被刷新", t2["failed_at"] == t1["failed_at"])
up = state.write_failed(D4, reason="analyze.py 退出 3：真实原文", source="bridge_exit",
                        exit_code=3, probe={"fallback_text": False})
t3 = json.loads((D4 / "任务.json").read_text(encoding="utf-8"))
check("[4] 兜底句 → 原文只升级原因（不写 history、不刷 failed_at）",
      up.get("upgraded") and t3["failed_reason"].startswith("analyze.py 退出 3")
      and len(t3["history"]) == hist1 and t3["failed_at"] == t1["failed_at"], str(up))

D5 = task_dir("auto-9904", st="discussing")
try:
    state.write_failed(D5, reason="旧执行体的失败", run_id="run-someone-else")
    fenced = False
except state.StateError:
    fenced = True
t5 = json.loads((D5 / "任务.json").read_text(encoding="utf-8"))
check("[4] run 栅栏：旧 run 写盘被拒（StateError + 状态不变）", fenced and t5["state"] == "discussing")

D6 = task_dir("auto-9905", st="generating")
try:
    state.fail_orphan_task(D6, reason="x", probe={"exec": view(exit_exists=True,
                                                              exit_run_id=RUN, exit_code=3)},
                           trigger="exec_dead")
    gen_blocked = False
except state.StateError:
    gen_blocked = True
t6 = json.loads((D6 / "任务.json").read_text(encoding="utf-8"))
check("[4] generating 即使有死证也拒绝自动降级（冻结口径）",
      gen_blocked and t6["state"] == "generating")

print("\n[5] 冻结常量未被改写")
check("[5] 900/60/300/900/30 逐值不变",
      (state.FAKE_RUN_SILENCE_SECONDS, state.HEARTBEAT_SECONDS,
       state.LOST_RUN_WATCHDOG_SECONDS, state.STALE_JOB_SECONDS) == (900, 60, 300, 900),
      "见 state.py 常量段")

print("\n[6] 重试凭证轮转：新 run 写得进 / 旧 run 迟到写仍被拒")
RUN1, RUN2 = "run-111111111111", "run-222222222222"
D7 = task_dir("auto-9906", st="discussing")
state.stamp_exec_run(D7, RUN1)
w1 = state.exec_write(D7, run_id=RUN1, pid=111, exit_code=3, outcome="failed", error_tail="round-1")
check("[6] 第一轮：本轮所有者 run 写得进（run_id=RUN1 / exit 3）",
      w1["run_id"] == RUN1 and w1["exit_code"] == 3, str(w1))
state.stamp_exec_run(D7, RUN2)                 # 重试：T0 换所有者
state.exec_rotate(D7)                          # 重试：T0 作废上一轮死证
rot = state.exec_read(D7)
check("[6] 重试轮转：上一轮死证被作废（exists=False，不再挂 exit=3）",
      (not rot["exists"]) and rot["exit_code"] is None, str(rot))
late1 = state.exec_write(D7, run_id=RUN1, pid=111, exit_code=3, outcome="failed",
                         error_tail="late-from-old-run")
after_late1 = state.exec_read(D7)
check("[6] 旧 run 迟到写被拒：文件仍不存在（不把旧死证补写回来）",
      (not after_late1["exists"]) and late1.get("exists") is False, str(after_late1))
w2 = state.exec_write(D7, run_id=RUN2, pid=222, exit_code=3, outcome="failed",
                      error_tail="round-2")
t7 = json.loads((D7 / "任务.json").read_text(encoding="utf-8"))
check("[6] 新 run 退出码落盘：run_id == 任务.json.exec_run_id 且 exit_code 一致",
      w2["run_id"] == RUN2 and t7["exec_run_id"] == RUN2
      and state.exec_read(D7)["exit_code"] == 3, f"{w2} / owner={t7.get('exec_run_id')}")
state.exec_write(D7, run_id=RUN1, pid=111, exit_code=0, outcome="done",
                 error_tail="late-after-new-cert")
fin = state.exec_read(D7)
check("[6] 新死证落盘后，旧 run 迟到写仍不得覆盖",
      fin["run_id"] == RUN2 and fin["exit_code"] == 3
      and fin["error_tail"] == "round-2", str(fin))

D8 = TMP / "auto-9907"
D8.mkdir(parents=True, exist_ok=True)
(D8 / "任务.json").write_text(json.dumps({"local_id": "auto-9907", "state": "discussing",
                                          "history": []}, ensure_ascii=False),
                              encoding="utf-8")
state.exec_write(D8, run_id="run-legacy-a", exit_code=3, outcome="failed")
state.exec_write(D8, run_id="run-legacy-b", exit_code=9, outcome="failed")
leg = state.exec_read(D8)
check("[6] 无 exec_run_id 的旧任务：退回文件级保守栅栏（别的 run 不许覆盖）",
      leg["run_id"] == "run-legacy-a" and leg["exit_code"] == 3, str(leg))

total = len(RESULTS)
passed = sum(1 for _, ok in RESULTS if ok)
print(f"\n共 {total} 项断言：PASS {passed} / FAIL {total - passed}")
shutil.rmtree(TMP, ignore_errors=True)
raise SystemExit(0 if passed == total else 1)
