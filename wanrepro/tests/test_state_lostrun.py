#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AT-18 单测：假运行态判定 + 受控降级（方案冻结稿 v2 §1–§3 / §7）。

跑法：wanrepro/ 下 `python tests/test_state_lostrun.py`（无需 pytest；也兼容 pytest 收集）。

锁什么（卡 t_0c2de297 的 (a)–(d) + 方案 §7 的 A0/A1–A5/A7/A11/A12/A16/A20）：
  [0] 冻结常量：阈值 900 / 文案逐字 / 状态枚举 / 六源枚举——偷改即红。
  [1] (a) 正常心跳任务不被误判（心跳新鲜 → silence_below_threshold，扫描后状态不动）。
  [2] (a) 心跳死但产物在动 → 同样不判死（六源取 max，不是"只看心跳"）。
  [3] (b) 零心跳零产物超阈值 → 降级为 failed，文案逐字，产物与目录一个不少。
  [4] (b) auto-0052 形态（只有 任务.json + 空 inbox/，无 心跳.json）→ 判死且可降级。
  [5] (c) 同一任务连扫两次只降级一次：第二次进 skipped(not_running)，history 只 +1。
  [6] (d) 已降级任务可被删除接口接受（墓碑生效）；discussing 仍拒删（A12 回归）。
  [7] 时钟回拨 → clock_back 跳过，任务文件字节不变。
  [8] generating 永不自动降级（A6）：有云端在途 → in_flight_cloud；无在途也只给人工口。
  [9] 心跳侧车原语：起手写 started_at 且 last_beat_at 留空（"从未起跑"可识别）；
      beat 只动心跳文件；产物指纹排除 拆解日志.txt，但日志 mtime 仍算活动证据。
  [10] 起跑锚兜底：进程从未跑起（只有 started_at）→ 仍可判死并降级。
  [11] 只读边界：非运行态任务零改动；降级入口对非运行态只抛不含尾巴的原文。
  [12] 扫描容错（A20）：一条坏 任务.json 不打断其它任务判定。
  [13] 零交集：state.py 不 import render（提示词链）。

夹具全部内联建在临时目录；判定链的 analysis 半边用 state._ANALYSIS_REVERSE 重定向到临时根，
真实 projects/ analysis/ uploads/ 零读写。
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

ROOT = Path(__file__).resolve().parents[1]                 # wanrepro/
TMP_ROOT = Path(tempfile.mkdtemp(prefix="test-at18-lostrun-"))
os.environ["WANREPRO_AUTO_DIR"] = str(TMP_ROOT)            # 必须在 import hub_api 之前
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ui"))

from automation import state                               # noqa: E402


def hub_module():
    """按需加载 ui/hub_api（删除接口回归用）：先工作区，失败再退 HEAD 已提交版本。"""
    try:
        import hub_api
        print("[..] [6] 删除接口回归跑的是：工作区 ui/hub_api.py")
        return hub_api
    except Exception as exc:                       # noqa: BLE001 - 半编辑态什么都可能抛
        print(f"[..] 工作区 hub_api 不可导入（{type(exc).__name__}: {exc}）")
    return hub_from_head()


def hub_from_head():
    """加载 **HEAD 已提交版本** 的 hub_api（= 当时真正在跑的那份）。

    回退口径：并行卡（t_2e9bbad9 在改 hub_api.py）落盘前后一刻，工作区可能处半编辑态；
    本卡结论不许被别人的在途编辑污染，也不许假装验过工作区——所以这里显式降级到 HEAD
    版本跑同一条回归，并在输出里点名「需在对方落库后重跑」。
    """
    import importlib.util
    import subprocess
    try:
        src = subprocess.run(["git", "-C", str(ROOT.parent), "show", "HEAD:wanrepro/ui/hub_api.py"],
                             capture_output=True, text=True, encoding="utf-8", check=True).stdout
    except Exception as exc:                       # noqa: BLE001
        print(f"[..] 取 HEAD 版本也失败：{type(exc).__name__}: {exc}")
        return None
    fallback = TMP_ROOT / "_head_hub"
    fallback.mkdir(parents=True, exist_ok=True)
    target = fallback / "hub_api.py"
    target.write_text(src, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("hub_api_head", target)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:                       # noqa: BLE001
        print(f"[..] HEAD 版本也不可导入：{type(exc).__name__}: {exc}")
        return None
    print("[..] [6] 删除接口回归跑的是：HEAD 已提交版本（工作区半编辑态，需在对方落库后重跑）")
    return module

# 判定链的 analysis 半边重定向到临时根：单测不碰真实 analysis/reverse/。
ANALYSIS_TMP = TMP_ROOT / "_analysis_reverse"
ANALYSIS_TMP.mkdir(parents=True, exist_ok=True)
state._ANALYSIS_REVERSE = ANALYSIS_TMP

REASON_DISCUSS = state.FAKE_RUN_REASONS["discussing"]
FAILS: list[tuple[str, str]] = []


def check(name: str, cond: object, detail: str = "") -> None:
    ok = bool(cond)
    if not ok:
        FAILS.append((name, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ← {detail}" if not ok and detail else ""))


def iso(offset_seconds: int = 0) -> str:
    return ((datetime.now(timezone.utc).astimezone() + timedelta(seconds=offset_seconds))
            .isoformat(timespec="seconds"))


def sha(path: Path) -> str:
    import hashlib
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def case_root(name: str) -> Path:
    root = TMP_ROOT / name
    (root / "tasks").mkdir(parents=True, exist_ok=True)
    return root


def make_task(root: Path, local_id: str, *, state_name: str = "discussing",
              silence: int = 1000, extra: dict | None = None) -> Path:
    """建一个最小任务目录：任务.json（history[-1].at 在 silence 秒前）+ 空 inbox/。"""
    task_dir = root / "tasks" / local_id
    (task_dir / "inbox").mkdir(parents=True, exist_ok=True)
    task = {"id": local_id, "state": state_name,
            "history": [{"at": iso(-silence), "from": "ingested", "to": state_name,
                         "note": "夹具"}],
            "ingestion": {"video_name": ""}}
    task.update(extra or {})
    (task_dir / "任务.json").write_text(json.dumps(task, ensure_ascii=False, indent=2),
                                        encoding="utf-8")
    return task_dir


def put_heartbeat(task_dir: Path, *, beat_age: int | None, started_age: int | None = None,
                  run_id: str = "run-fixture", beat_count: int = 7) -> None:
    """直接落一份心跳侧车（夹具，不经原语——原语另有 [9] 专门验）。"""
    payload = state._heartbeat_blank()
    payload["run_id"] = run_id
    payload["started_at"] = iso(-(started_age if started_age is not None else (beat_age or 0)))
    payload["last_beat_at"] = "" if beat_age is None else iso(-beat_age)
    payload["beat_count"] = beat_count
    (task_dir / state.HEARTBEAT_FILE).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def touch(path: Path, *, age: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")
    stamp = time.time() - age
    os.utime(path, (stamp, stamp))
    return path


def scan(root: Path, *, apply: bool = False) -> dict:
    """对账扫描的**测试替身**（§4.1 集合语义）：运行态扫描 / 假运行入 lost / 其余入 skipped。

    真实实现在 ui/hub_api.py（另一张卡）；本替身只用于证明 state 层判定 + 降级的幂等，
    所以刻意不碰 HUB_JOBS，held_job 一律按 False。
    """
    out = {"scanned": 0, "lost": [], "skipped": [], "errors": []}
    for mf in sorted((root / "tasks").glob("auto-*/任务.json")):
        local_id = mf.parent.name
        try:
            task = state.load(mf.parent)
        except state.StateError as exc:                    # A20：坏任务不打断扫描
            out["errors"].append({"local_id": local_id, "error": str(exc)})
            continue
        if task["state"] not in state.RUNNING_STATES_MACHINE:
            out["skipped"].append({"local_id": local_id, "state": task["state"],
                                   "reason_code": "not_running"})
            continue
        out["scanned"] += 1
        probe = state.activity_snapshot(mf.parent, task)
        lost, reason_code = state.judge_lost_run(task, probe)
        if not lost:
            out["skipped"].append({"local_id": local_id, "state": task["state"],
                                   "reason_code": reason_code})
            continue
        row = {"local_id": local_id, "state": task["state"],
               "silence_seconds": probe["silence_seconds"],
               "last_activity_at": probe["last_activity_at"],
               "last_activity_source": probe["source"],
               "heartbeat_at": probe["heartbeat_at"],
               "reason": state.fake_run_reason(task["state"]), "changed": False}
        if apply:
            result = state.fail_orphan_task(mf.parent, reason=row["reason"], probe=probe)
            row["changed"] = result["changed"]
        out["lost"].append(row)
    return out


def read_task(task_dir: Path) -> dict:
    return json.loads((task_dir / "任务.json").read_text(encoding="utf-8"))


# ------------------------------------------------------------------ [0] 冻结常量
check("[0] 静默阈值冻结为 900s（A0）", state.FAKE_RUN_SILENCE_SECONDS == 900,
      f"实际 {state.FAKE_RUN_SILENCE_SECONDS}")
check("[0] 心跳间隔沿用 60s，不新造数值", state.HEARTBEAT_SECONDS == 60,
      f"实际 {state.HEARTBEAT_SECONDS}")
check("[0] 失联文案逐字冻结", REASON_DISCUSS == "拆解进程失联，可重试或删除", REASON_DISCUSS)
check("[0] 自动降级只覆盖 discussing/rendering",
      state.FAKE_RUN_DEGRADABLE_STATES == ("discussing", "rendering"),
      str(state.FAKE_RUN_DEGRADABLE_STATES))
check("[0] 运行态集合 = discussing/rendering/generating",
      state.RUNNING_STATES_MACHINE == ("discussing", "rendering", "generating"),
      str(state.RUNNING_STATES_MACHINE))
check("[0] 活动证据六源枚举冻结",
      state.ACTIVITY_SOURCES == ("心跳", "状态迁移", "阶段锚", "任务目录产物", "拆解产物", "起跑锚"),
      str(state.ACTIVITY_SOURCES))
check("[0] 文案与降级状态一致（每个自动降级态都有专属文案）",
      all(s in state.FAKE_RUN_REASONS for s in state.FAKE_RUN_DEGRADABLE_STATES),
      str(sorted(state.FAKE_RUN_REASONS)))

# ------------------------------------------------------------------ [1] (a) 正常心跳不误判
root = case_root("a-live")
live = make_task(root, "auto-9701", silence=2400)
put_heartbeat(live, beat_age=10, started_age=2400)
before = sha(live / "任务.json")
probe = state.activity_snapshot(live)
lost, code = state.judge_lost_run(read_task(live), probe)
check("[1] 心跳 10s 前 → 不判假运行（a）", lost is False and code == "silence_below_threshold",
      f"{lost}/{code} silence={probe['silence_seconds']}")
check("[1] 活动源落在「心跳」", probe["source"] == "心跳", str(probe["sources"]))
check("[1] 心跳龄期 ≤ 60s", (probe["heartbeat_age_seconds"] or 999) <= 60,
      str(probe["heartbeat_age_seconds"]))
report = scan(root, apply=True)
check("[1] 扫描后活任务状态仍 discussing、字节不变",
      read_task(live)["state"] == "discussing" and sha(live / "任务.json") == before,
      json.dumps(report, ensure_ascii=False))
check("[1] 活任务进 skipped 而不是 lost",
      not report["lost"] and report["skipped"] and
      report["skipped"][0]["reason_code"] == "silence_below_threshold",
      json.dumps(report, ensure_ascii=False))

# ------------------------------------------------------------------ [2] 心跳死但产物在动
root = case_root("a-artifact-moving")
moving = make_task(root, "auto-9702", silence=2400,
                   extra={"bridge_id": "probe-at18-unit-move"})
put_heartbeat(moving, beat_age=901, started_age=2400)
touch(ANALYSIS_TMP / "probe-at18-unit-move" / "拆解.md", age=30)
probe = state.activity_snapshot(moving)
lost, code = state.judge_lost_run(read_task(moving), probe)
check("[2] 心跳停 901s 但拆解产物 30s 内动 → 不判死",
      lost is False and code == "silence_below_threshold",
      f"{lost}/{code} src={probe['source']} silence={probe['silence_seconds']}")
check("[2] 活动源落在「拆解产物」", probe["source"] == "拆解产物", str(probe["sources"]))
check("[2] 产物指纹算进 analysis 半边",
      any(item["path"].startswith("analysis/reverse/probe-at18-unit-move/")
          for item in state.artifact_manifest(moving, read_task(moving))),
      str([i["path"] for i in state.artifact_manifest(moving, read_task(moving))]))

# ------------------------------------------------------------------ [3] (b) 零心跳零产物超阈值
root = case_root("b-dead")
dead = make_task(root, "auto-9703", silence=1200)
probe = state.activity_snapshot(dead)
lost, code = state.judge_lost_run(read_task(dead), probe)
check("[3] 零心跳零产物 + 静默 1200s → 判假运行（b）", lost is True and code == "",
      f"{lost}/{code} src={probe['source']} silence={probe['silence_seconds']}")
result = state.fail_orphan_task(dead, reason=REASON_DISCUSS, probe=probe)
after = read_task(dead)
check("[3] 降级返回 changed=True / state=failed",
      result["changed"] is True and result["state"] == "failed" and result["already"] is False,
      json.dumps(result, ensure_ascii=False))
check("[3] 原因文案逐字落盘（failed_reason）", after.get("failed_reason") == REASON_DISCUSS,
      str(after.get("failed_reason")))
check("[3] history 追加一条 from=discussing/to=failed，note 同文案",
      after["history"][-1]["from"] == "discussing" and after["history"][-1]["to"] == "failed"
      and after["history"][-1]["note"] == REASON_DISCUSS,
      json.dumps(after["history"][-1], ensure_ascii=False))
check("[3] failed_at 非空且等于 history 时刻",
      bool(after.get("failed_at")) and after["failed_at"] == after["history"][-1]["at"],
      f"{after.get('failed_at')} / {after['history'][-1]['at']}")
ev = after.get("failed_evidence") or {}
check("[3] failed_evidence 字段齐全（trigger/silence/last_activity/source/heartbeat/threshold/run_id/decided_at）",
      ev.get("trigger") == "auto_scan" and ev.get("threshold_seconds") == 900
      and ev.get("last_activity_source") == "状态迁移" and ev.get("silence_seconds") >= 1200
      and ev.get("last_activity_at") and ev.get("heartbeat_at") == ""
      and ev.get("run_id") == "" and ev.get("decided_at") == after["failed_at"],
      json.dumps(ev, ensure_ascii=False))
check("[3] 不静默清除：任务目录与 inbox 仍在、任务文件仍可读",
      (dead / "任务.json").is_file() and (dead / "inbox").is_dir(), str(dead))
kept_keys = set(after) - {"state", "history", "failed_reason", "failed_at", "failed_evidence"}
check("[3] 除 5 个键外未新增/未删任何字段",
      kept_keys == {"id", "ingestion"}, str(sorted(kept_keys)))

# ------------------------------------------------------------------ [3b] T2：rendering 同样可降级
root = case_root("b-rendering")
render_task = make_task(root, "auto-9716", state_name="rendering", silence=1200,
                        extra={"history": [{"at": iso(-1200), "from": "awaiting_assets",
                                            "to": "rendering", "note": "夹具"}]})
probe = state.activity_snapshot(render_task)
lost, code = state.judge_lost_run(read_task(render_task), probe)
check("[3b] rendering 静默 1200s → 判假运行（T2）", lost is True and code == "",
      f"{lost}/{code}")
result = state.fail_orphan_task(render_task, reason=state.fake_run_reason("rendering"),
                                probe=probe)
check("[3b] rendering 降级文案走专属一句",
      result["changed"] is True
      and read_task(render_task)["failed_reason"] == "渲染进程失联，可重试或删除",
      str(read_task(render_task).get("failed_reason")))

# ------------------------------------------------------------------ [4] auto-0052 形态
root = case_root("b-never-ran")
stalled = make_task(root, "auto-9704", silence=1000)
check("[4] 从未起跑：无 心跳.json", not (stalled / state.HEARTBEAT_FILE).exists())
report = scan(root, apply=True)
row = (report["lost"] or [{}])[0]
check("[4] 扫描判死并降级（auto-0052 形态）",
      len(report["lost"]) == 1 and row.get("changed") is True
      and row.get("last_activity_source") in ("状态迁移", "任务目录产物"),
      json.dumps(report, ensure_ascii=False))
check("[4] 降级后状态为 failed、文案同一句",
      read_task(stalled)["state"] == "failed"
      and read_task(stalled)["failed_reason"] == REASON_DISCUSS,
      json.dumps(read_task(stalled).get("failed_evidence"), ensure_ascii=False))

# ------------------------------------------------------------------ [5] (c) 幂等：连扫两次只降一次
root = case_root("c-idempotent")
once = make_task(root, "auto-9705", silence=1500)
first = scan(root, apply=True)
snap1 = read_task(once)
bytes1 = (once / "任务.json").read_bytes()
check("[5] 第一轮：lost 含该任务且 changed=True",
      len(first["lost"]) == 1 and first["lost"][0]["changed"] is True,
      json.dumps(first, ensure_ascii=False))
second = scan(root, apply=True)
snap2 = read_task(once)
check("[5] 第二轮：lost 为空（不再二次降级）", second["lost"] == [],
      json.dumps(second, ensure_ascii=False))
check("[5] 第二轮：进 skipped 且 reason_code=not_running",
      any(s["local_id"] == "auto-9705" and s["reason_code"] == "not_running"
          for s in second["skipped"]), json.dumps(second["skipped"], ensure_ascii=False))
check("[5] history 长度只 +1（1 条夹具 + 1 条降级）", len(snap2["history"]) == 2,
      str(len(snap2["history"])))
check("[5] failed_at 与文件字节在两轮之间不变",
      snap1["failed_at"] == snap2["failed_at"] and (once / "任务.json").read_bytes() == bytes1,
      f"{snap1['failed_at']} / {snap2['failed_at']}")
again = state.fail_orphan_task(once, reason=REASON_DISCUSS, trigger="auto_scan")
check("[5] 直接再调一次降级入口：changed=False / already=True，不写 history",
      again["changed"] is False and again.get("already") is True
      and again["reason"] == REASON_DISCUSS and len(read_task(once)["history"]) == 2,
      json.dumps(again, ensure_ascii=False))

# ------------------------------------------------------------------ [6] (d) 无「运行中不可删」死锁
def delete_regressions(hub, *, failed_id: str, running_id: str):
    """A11（已降级可删，墓碑生效）+ A12（discussing 仍拒删）。返回 (断言列表, 半编辑态异常)。"""
    checks: list[tuple[str, bool, str]] = []
    checks.append(("[6] failed 属 DELETABLE_STATES、不在 RUNNING_STATES",
                   "failed" not in hub.RUNNING_STATES and "failed" in hub.DELETABLE_STATES,
                   f"running={hub.RUNNING_STATES} deletable={hub.DELETABLE_STATES}"))
    d = TMP_ROOT / failed_id                       # 任务须落在 hub 的 TASKS_DIR（= TMP_ROOT）
    (d / "inbox").mkdir(parents=True, exist_ok=True)
    (d / "任务.json").write_text(json.dumps(
        {"id": failed_id, "state": "failed",
         "history": [{"at": iso(-100), "from": "ingested", "to": "discussing", "note": "夹具"},
                     {"at": iso(-60), "from": "discussing", "to": "failed",
                      "note": REASON_DISCUSS}],
         "failed_reason": REASON_DISCUSS, "failed_at": iso(-60),
         "failed_evidence": {"trigger": "auto_scan", "threshold_seconds": 900}},
        ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        deleted = hub.delete_task(failed_id)
    except hub.HubError as exc:
        checks.append(("[6] 删除接口接受已降级任务（墓碑生效）", False, f"HubError: {exc}"))
        return checks, None
    except Exception as exc:                      # noqa: BLE001 - 半编辑态（NameError 之类）
        checks.append(("[6] 删除接口可用（半编辑态）", False, f"{type(exc).__name__}: {exc}"))
        return checks, exc
    checks.append(("[6] 删除接口接受已降级任务（墓碑生效）",
                   deleted.get("ok") is True and deleted.get("tombstone") is True
                   and deleted.get("state_was") == "failed",
                   json.dumps(deleted, ensure_ascii=False)))
    tombstones = json.loads((hub.TOMBSTONES).read_text(encoding="utf-8"))
    checks.append(("[6] 墓碑留下该任务号", any(i.get("id") == failed_id for i in tombstones),
                   str([i.get("id") for i in tombstones])))
    checks.append(("[6] 目录已随删除消失", not d.exists(), str(d)))

    r = TMP_ROOT / running_id                     # A12 回归：运行态仍拒删（本批不许放宽）
    (r / "inbox").mkdir(parents=True, exist_ok=True)
    (r / "任务.json").write_text(json.dumps(
        {"id": running_id, "state": "discussing",
         "history": [{"at": iso(-5), "from": "ingested", "to": "discussing", "note": "夹具"}]},
        ensure_ascii=False, indent=2), encoding="utf-8")
    put_heartbeat(r, beat_age=5, started_age=200)
    try:
        hub.delete_task(running_id)
        checks.append(("[6] discussing 仍拒删（A12 回归）", False, "竟然删成功了"))
    except hub.HubError as exc:
        checks.append(("[6] discussing 仍拒删（A12 回归）",
                       "任务运行中（discussing），删除拒绝" in str(exc), str(exc)))
    except Exception as exc:                      # noqa: BLE001
        checks.append(("[6] discussing 仍拒删（A12 回归）", False, f"{type(exc).__name__}: {exc}"))
    return checks, None


hub = hub_module()
if hub is None:
    check("[6] 删除接口回归（A11 已降级可删 / A12 discussing 仍拒删）", False,
          "ui/hub_api.py 工作区与 HEAD 版本均不可加载")
else:
    checks, broken = delete_regressions(hub, failed_id="auto-9706", running_id="auto-9707")
    if broken is not None:
        print(f"[..] 工作区 delete_task 处于半编辑态（{type(broken).__name__}），"
              "改用 HEAD 版本重跑 A11/A12")
        head = hub_from_head()
        if head is not None:
            checks, _ = delete_regressions(head, failed_id="auto-9726", running_id="auto-9727")
            hub = head
    for name, ok, detail in checks:
        check(name, ok, detail)

# ------------------------------------------------------------------ [7] 时钟回拨
root = case_root("clock-back")
future = make_task(root, "auto-9708", silence=0,
                   extra={"history": [{"at": iso(600), "from": "ingested",
                                       "to": "discussing", "note": "夹具"}]})
probe = state.activity_snapshot(future)
lost, code = state.judge_lost_run(read_task(future), probe)
before = sha(future / "任务.json")
check("[7] last_activity 在未来 → clock_back 跳过", lost is False and code == "clock_back",
      f"{lost}/{code} silence={probe['silence_seconds']}")
try:
    state.fail_orphan_task(future, reason=REASON_DISCUSS, probe=probe)
    check("[7] clock_back 时降级入口拒写", False, "竟然降级了")
except state.StateError as exc:
    check("[7] clock_back 时降级入口拒写（未达假运行判定）",
          "未达假运行判定（clock_back）" in str(exc), str(exc))
check("[7] 任务文件字节不变", sha(future / "任务.json") == before)

# ------------------------------------------------------------------ [8] generating 不自动降级
root = case_root("generating")
gen_cloud = make_task(root, "auto-9709", state_name="generating", silence=2000,
                      extra={"cloud_task_id": "t_123", "history": [
                          {"at": iso(-2000), "from": "approved", "to": "generating",
                           "note": "夹具"}]})
probe = state.activity_snapshot(gen_cloud)
check("[8] generating + 云端 id → in_flight_cloud（A6）",
      state.judge_lost_run(read_task(gen_cloud), probe) == (False, "in_flight_cloud"),
      str(state.judge_lost_run(read_task(gen_cloud), probe)))
check("[8] generating 有 dispatch=prepared → in_flight_cloud",
      state.judge_lost_run({**read_task(gen_cloud), "cloud_task_id": "",
                            "dispatch": {"phase": "prepared"}}, probe)
      == (False, "in_flight_cloud"))
check("[8] generating 有 submit_unknown → in_flight_cloud",
      state.judge_lost_run({**read_task(gen_cloud), "cloud_task_id": "",
                            "submit_unknown": True}, probe) == (False, "in_flight_cloud"))
check("[8] generating 无在途证据也只给人工口（manual_only_state）",
      state.judge_lost_run({**read_task(gen_cloud), "cloud_task_id": ""}, probe)
      == (False, "manual_only_state"))
report = scan(root, apply=True)
check("[8] 扫描不降级 generating，进 skipped",
      not report["lost"] and report["skipped"]
      and report["skipped"][0]["reason_code"] == "in_flight_cloud"
      and read_task(gen_cloud)["state"] == "generating",
      json.dumps(report, ensure_ascii=False))

# ------------------------------------------------------------------ [9] 心跳侧车原语
root = case_root("heartbeat-primitives")
beat_dir = make_task(root, "auto-9710", silence=100)
payload = state.heartbeat_begin(beat_dir, run_id="run-abc", phase="起手")
check("[9] 起手写 started_at、last_beat_at 留空（从未收到心跳可识别）",
      payload["started_at"] and payload["last_beat_at"] == "" and payload["beat_count"] == 0
      and payload["run_id"] == "run-abc" and payload["artifact_count"] == 0,
      json.dumps(payload, ensure_ascii=False))
check("[9] 起手写不碰 任务.json 的 lapian_*/状态",
      read_task(beat_dir)["state"] == "discussing"
      and "lapian_stage_at" not in read_task(beat_dir))
p1 = state.heartbeat_beat(beat_dir, phase="等待中：本地等待循环仍在运行")
p2 = state.heartbeat_beat(beat_dir)
check("[9] beat 递增 beat_count 并写 last_beat_at / phase",
      p1["beat_count"] == 1 and p2["beat_count"] == 2 and p2["last_beat_at"]
      and p2["phase"] == "等待中：本地等待循环仍在运行",
      json.dumps(p2, ensure_ascii=False))
check("[9] beat 不写 任务.json（AT-16 B4b 合同）",
      read_task(beat_dir)["state"] == "discussing"
      and "lapian_stage_at" not in read_task(beat_dir))
digest_before = p2["artifact_digest"]
state.heartbeat_beat(beat_dir)                     # 再打一次心跳：只动心跳文件本身
digest_after = state.heartbeat_read(beat_dir)["artifact_digest"]
check("[9] 心跳自证不自变：连打两次心跳产物指纹不变", digest_before == digest_after,
      f"{digest_before} → {digest_after}")
(beat_dir / "拆解日志.txt").write_text("还在等\n", encoding="utf-8")
check("[9] 产物指纹排除 拆解日志.txt（否则「活着但不动」永不可检）",
      state.heartbeat_read(beat_dir)["artifact_digest"] == digest_before,
      f"{digest_before} → {state.heartbeat_read(beat_dir)['artifact_digest']}")
log_only = make_task(root, "auto-9711", silence=5000)
touch(log_only / "拆解日志.txt", age=20)
probe_log = state.activity_snapshot(log_only)
check("[9] 日志 mtime 仍算活动证据（口径不同，不许混）",
      probe_log["source"] == "任务目录产物" and probe_log["silence_seconds"] <= 60,
      f"{probe_log['source']} {probe_log['silence_seconds']}")
state.heartbeat_finish(beat_dir, outcome="env_error")
check("[9] 收尾写 ended_at/outcome（仅诊断）",
      state.heartbeat_read(beat_dir)["ended_at"]
      and state.heartbeat_read(beat_dir)["outcome"] == "env_error",
      json.dumps(state.heartbeat_read(beat_dir), ensure_ascii=False))

# ------------------------------------------------------------------ [10] 起跑锚兜底（从未真正跑起）
root = case_root("start-anchor")
never = make_task(root, "auto-9712", silence=3000,
                  extra={"history": [{"at": iso(-3000), "from": "ingested",
                                      "to": "discussing", "note": "夹具"}]})
state.heartbeat_begin(never, run_id="run-never", phase="拆解启动")
hb = state.heartbeat_read(never)
hb["started_at"] = iso(-1000)          # 起手 1000s 前就断了，一次心跳都没来
(never / state.HEARTBEAT_FILE).write_text(
    json.dumps(hb, ensure_ascii=False, indent=2), encoding="utf-8")
probe = state.activity_snapshot(never)
lost, code = state.judge_lost_run(read_task(never), probe)
check("[10] 只有起跑锚、心跳全空 → 仍判死（source=起跑锚）",
      lost is True and probe["source"] == "起跑锚",
      f"{lost}/{code} src={probe['source']} silence={probe['silence_seconds']}")
result = state.fail_orphan_task(never, reason=REASON_DISCUSS, probe=probe)
check("[10] 降级证据带 run_id、heartbeat_at 为空",
      result["changed"] is True
      and read_task(never)["failed_evidence"]["run_id"] == "run-never"
      and read_task(never)["failed_evidence"]["heartbeat_at"] == "",
      json.dumps(read_task(never)["failed_evidence"], ensure_ascii=False))

# ------------------------------------------------------------------ [11] 只读边界
root = case_root("readonly")
idle = make_task(root, "auto-9713", state_name="awaiting_approval", silence=5000)
before = sha(idle / "任务.json")
probe = state.activity_snapshot(idle)
check("[11] 非运行态 → not_running（不进 lost）",
      state.judge_lost_run(read_task(idle), probe) == (False, "not_running"))
report = scan(root, apply=True)
check("[11] 扫描对非运行态零写入",
      not report["lost"] and sha(idle / "任务.json") == before,
      json.dumps(report, ensure_ascii=False))
try:
    state.fail_orphan_task(idle, reason=REASON_DISCUSS, probe=probe)
    check("[11] 降级入口对非运行态抛错", False, "竟然改成功了")
except state.StateError as exc:
    check("[11] 降级入口抛原文（不带「无需…」尾巴，包装在 hub 层）",
          str(exc) == "任务当前不是运行态（awaiting_approval）", str(exc))
check("[11] 只读边界：任务文件字节不变", sha(idle / "任务.json") == before)

# ------------------------------------------------------------------ [12] 扫描容错
root = case_root("scan-tolerant")
bad = root / "tasks" / "auto-9714"
bad.mkdir(parents=True, exist_ok=True)
(bad / "任务.json").write_text("{ 这不是 JSON", encoding="utf-8")
good = make_task(root, "auto-9715", silence=1200)
report = scan(root, apply=True)
check("[12] 坏任务进 errors[]，不打断扫描（A20）",
      len(report["errors"]) == 1 and report["errors"][0]["local_id"] == "auto-9714",
      json.dumps(report["errors"], ensure_ascii=False))
check("[12] 同轮正常任务照判照降",
      len(report["lost"]) == 1 and report["lost"][0]["local_id"] == "auto-9715"
      and read_task(good)["state"] == "failed",
      json.dumps(report["lost"], ensure_ascii=False))

# ------------------------------------------------------------------ [13] 零交集
import ast

src = (ROOT / "automation" / "state.py").read_text(encoding="utf-8")
imported: set[str] = set()
for node in ast.walk(ast.parse(src)):
    if isinstance(node, ast.Import):
        imported.update(alias.name for alias in node.names)
    elif isinstance(node, ast.ImportFrom):
        imported.add(node.module or "")
        imported.update(f"{node.module}.{alias.name}" for alias in node.names)
check("[13] state.py 不 import render（提示词链零交集）",
      not any("render" in name for name in imported), str(sorted(imported)))
check("[13] 新增段不写 render 字段（提示词/声音段/三态）",
      "提示词.md" not in src.split("AT-18：假运行态")[1]
      and "声音段" not in src.split("AT-18：假运行态")[1])

# ------------------------------------------------------------------ 收尾
if hub is not None:
    hub.HUB_JOBS.clear()
shutil.rmtree(TMP_ROOT, ignore_errors=True)
print()
if FAILS:
    print(f"FAILED {len(FAILS)} 项：")
    for name, detail in FAILS:
        print(f"  · {name}" + (f"  ← {detail}" if detail else ""))
    sys.exit(1)
print("ALL PASS")
