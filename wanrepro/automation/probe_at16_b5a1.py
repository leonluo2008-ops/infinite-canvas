#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AT-16 B5a-1 探针（后端侧）：提交结果**四类形态 + 人话说明 + 摘要生产链**。

跑法：wanrepro/ 下 `python -m automation.probe_at16_b5a1`。

缺陷 D6（auto-0043 实证）：批准即自动提交 → 渠道 403 落盘 gen_error，但
①列表/详情状态标签仍按 state 说「已批准·待提交」②错误只有一次性 toast（列表零信息）
③原文英文 JSON 堆叠无人话。根因之一（审计 A2）：`ui/hub_api._summary` 根本没透出
gen_error/dispatch/submit_unknown/active_submit，列表 5 秒轮询整表替换后什么都留不下。

本探针锁后端这一半（前端渲染与接线由 web/scripts/probe-at16-b5a.mjs 锁）：

  [0] 隔离：TASKS_DIR 必须落在本次临时根内（否则拒跑，真实任务零写入）。
  [1] 四类形态判定（唯一判定源 automation/submit_view.build）：
      unknown（submit_unknown / dispatch 未完结）/ rejected（前置拒绝回退态）/
      inflight（active 或 generating+超时）/ failed（明确渠道失败）/ none。
  [2] 摘要生产链：`_summary` 透出最小提交诊断（has_gen_error / dispatch_phase /
      submit_unknown / active_submit / error_code）+ 人话（conclusion/causes/guidance/raw）。
  [3] 动作许可矩阵：unknown/inflight → blocks_retry=true（禁重提）；rejected/failed → false。
  [4] 人话纪律：可能原因**并列**（≥2 条且带「可能」），不把 403 翻成唯一原因；脱敏原文保留。
  [5] 历史兼容：旧任务 gen_error 无分类也出人话壳 + 原文（只读，不改历史文件）。
  [6] 创建次数 = 0：unknown 夹具上 start_submit 被既有阻断守卫拒掉，HUB_JOBS 零新增。
  [7] 轮询稳定：连续两次摘要（模拟 5 秒轮询）form/人话不退化成空。
  [8] 同源：详情 get_task 的 submit 与列表摘要逐字段一致（同一生产函数）。
  [9] 静态守卫：submit 只在 _summary 产生一处；get_task 复用 _summary；无新端点；
      submit_view 不写盘（只读事实装配）。

夹具任务只用 auto-9801..9808（临时根内，跑完连根删）。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]          # wanrepro/
TMP_ROOT = Path(tempfile.mkdtemp(prefix="probe-b5a1-"))
os.environ["WANREPRO_AUTO_DIR"] = str(TMP_ROOT)     # 必须在 import hub_api 之前
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ui"))

from automation import submit_view  # noqa: E402
import hub_api  # noqa: E402

RESULTS: list[tuple[str, bool]] = []
GEN_403 = "video_request_failed: HTTP 403 Forbidden（渠道拒绝）"


def check(name: str, cond: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" —— {detail}" if detail and not cond else ""))
    return bool(cond)


def summary() -> int:
    fails = [n for n, ok in RESULTS if not ok]
    print(f"\n[probe-at16-b5a1] {len(RESULTS) - len(fails)}/{len(RESULTS)} PASS")
    for n in fails:
        print(f"  FAIL {n}")
    return 1 if fails else 0


def seed(local_id: str, **fields) -> Path:
    """种一个隔离夹具任务（只写本次临时根）。"""
    d = TMP_ROOT / local_id
    d.mkdir(parents=True, exist_ok=True)
    task = {"local_id": local_id, "state": "approved", "history": []}
    task.update(fields)
    (d / "任务.json").write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
    return d


# ════════════════════════════════════════════════════════════ [0] 隔离
print("0) 隔离：TASKS_DIR 落在本次临时根内")
print(f"   temp root = {TMP_ROOT}")
check("[0] TASKS_DIR == 临时根（不一致必须拒跑，真实任务零写入）",
      Path(hub_api.TASKS_DIR).resolve() == TMP_ROOT.resolve(),
      f"{hub_api.TASKS_DIR} != {TMP_ROOT}")

# ════════════════════════════════════════════════════════════ 夹具
seed("auto-9801", state="approved", gen_error=GEN_403,
     submit_unknown={"at": "2026-11-11T22:00:00+08:00", "reason": "提交结果未知：断连", "dispatch": "dispatched"},
     dispatch={"phase": "dispatched", "at": "2026-11-11T22:00:00+08:00"})
seed("auto-9802", state="approved", dispatch={"phase": "prepared", "at": "2026-11-11T22:01:00+08:00"})
seed("auto-9803", state="awaiting_approval", gen_error="渠道前置拒绝（HTTP 409）：并发闸满")
seed("auto-9804", state="generating", gen_error="等待超时（云端未回执）", cloud_task_id="task-abc123")
seed("auto-9805", state="failed", gen_error="云端确认失败：内容被拒")
seed("auto-9806", state="approved")
seed("auto-9807", state="failed", gen_error="请求失败")          # 旧任务：无任何提交链字段
seed("auto-9808", state="generating", cloud_task_id="task-live9")
seed("auto-9809", state="approved",
     submit_unknown={"at": "2026-11-11T22:05:00+08:00", "reason": "提交结果未知：断连", "dispatch": ""})

rows = {row["local_id"]: row for row in hub_api.list_tasks()}
# active 夹具：HUB_JOBS 里挂一个 running 记录（等价「本进程提交在跑」）
hub_api.HUB_JOBS["probe-job-1"] = {"local_id": "auto-9808", "status": "running", "kind": "hub-submit"}
rows = {row["local_id"]: row for row in hub_api.list_tasks()}

# ════════════════════════════════════════════════════════════ [1] 四类形态
print("1) 四类形态判定（唯一判定源 submit_view.build）")
expect = {
    "auto-9801": "unknown",     # submit_unknown
    "auto-9802": "unknown",     # dispatch 未完结（可能已发出）
    "auto-9803": "rejected",    # 前置拒绝回退态
    "auto-9804": "inflight",    # generating + 超时 + 有 taskId
    "auto-9805": "failed",      # 明确渠道失败
    "auto-9806": "none",        # 正常待提交，不编造结果
    "auto-9807": "failed",      # 旧任务（无分类字段）也归到明确失败
    "auto-9808": "inflight",    # 活跃提交占用
}
for local_id, form in expect.items():
    got = (rows.get(local_id) or {}).get("submit", {}).get("form")
    check(f"[1] {local_id} → form={form}", got == form, f"得到 {got!r}")

check("[1] 形态取值只在四类 + none 之内（不发明第五种）",
      all((row.get("submit") or {}).get("form") in submit_view.FORMS for row in rows.values()),
      str({k: (v.get("submit") or {}).get("form") for k, v in rows.items()}))

# ════════════════════════════════════════════════════════════ [2] 摘要生产链
print("2) 摘要最小诊断字段（列表不拉详情就能说实话）")
diag = rows["auto-9801"]["submit"]
for field in ("has_gen_error", "dispatch_phase", "submit_unknown", "active_submit",
              "error_code", "raw", "cloud_task_id"):
    check(f"[2] unknown 摘要含 {field}", field in diag, str(list(diag)))
check("[2] has_gen_error=True 且原样脱敏原文（不裁不改）",
      diag["has_gen_error"] is True and diag["raw"] == GEN_403, repr(diag.get("raw")))
check("[2] dispatch_phase=dispatched、submit_unknown=True",
      diag["dispatch_phase"] == "dispatched" and diag["submit_unknown"] is True, str(diag))
check("[2] 短错误码从原文抠出 HTTP 403（不推断语义）", diag["error_code"] == "HTTP 403", repr(diag.get("error_code")))
check("[2] active 夹具摘要 active_submit=True", rows["auto-9808"]["submit"]["active_submit"] is True,
      str(rows["auto-9808"]["submit"].get("active_submit")))
check("[2] 无错误任务（auto-9806）四个诊断位全空/False，且无结论",
      rows["auto-9806"]["submit"]["has_gen_error"] is False
      and rows["auto-9806"]["submit"]["dispatch_phase"] == ""
      and rows["auto-9806"]["submit"]["conclusion"] == "")

# ════════════════════════════════════════════════════════════ [3] 动作许可矩阵
print("3) 动作许可：禁重提 vs 可重提")
blocked = {"auto-9801": True, "auto-9802": True, "auto-9804": True, "auto-9808": True}
allowed = {"auto-9803": False, "auto-9805": False, "auto-9806": False}
for local_id, want in {**blocked, **allowed}.items():
    got = rows[local_id]["submit"]["blocks_retry"]
    check(f"[3] {local_id} blocks_retry={want}", got is want, f"得到 {got!r}")
check("[3] 在途有 taskId → can_resume=True（只查不重提）", rows["auto-9804"]["submit"]["can_resume"] is True,
      str(rows["auto-9804"]["submit"].get("can_resume")))
check("[3] unknown 无 taskId → can_resume=False（不假装能续查）",
      rows["auto-9801"]["submit"]["can_resume"] is False)

# ════════════════════════════════════════════════════════════ [4] 人话纪律
print("4) 人话：四类都有结论，可能原因并列，原文保留")
for local_id in ("auto-9801", "auto-9803", "auto-9804", "auto-9805"):
    sv = rows[local_id]["submit"]
    check(f"[4] {local_id} 有结论（{sv['form']}）", bool(sv["conclusion"]), repr(sv.get("conclusion")))
    check(f"[4] {local_id} 有处理指引（指向真实入口，不是空话）", len(sv["guidance"]) >= 1, str(sv.get("guidance")))
unknown_causes = rows["auto-9801"]["submit"]["causes"]
check("[4] unknown 可能原因 ≥2 条并列（不锁唯一原因）", len(unknown_causes) >= 2, str(unknown_causes))
check("[4] unknown 每条原因都带「可能」（并列口径，不翻译成确定结论）",
      all("可能" in cause for cause in unknown_causes), str(unknown_causes))
failed_causes = " ".join(rows["auto-9805"]["submit"]["causes"])
check("[4] failed 原因以渠道原文为准（不把错误码翻成唯一原因）", "以渠道原文为准" in failed_causes, failed_causes)
check("[4] 四类形态都没有「已收单」式断言冒充事实（只在 unknown 里说可能已收单）",
      "可能已收单" in rows["auto-9801"]["submit"]["conclusion"]
      and "可能已收单" not in rows["auto-9805"]["submit"]["conclusion"])
check("[4] 前置拒绝结论写明可改后重提", "可改后重提" in rows["auto-9803"]["submit"]["conclusion"],
      rows["auto-9803"]["submit"]["conclusion"])
check("[4] 明确失败结论写明可重试", "可重试" in rows["auto-9805"]["submit"]["conclusion"],
      rows["auto-9805"]["submit"]["conclusion"])

# ════════════════════════════════════════════════════════════ [5] 历史兼容
print("5) 历史 gen_error 只读兼容（旧任务无分类字段）")
legacy = rows["auto-9807"]["submit"]
check("[5] 旧任务也出人话壳（form/结论/指引齐）",
      legacy["form"] == "failed" and bool(legacy["conclusion"]) and len(legacy["guidance"]) >= 1,
      json.dumps(legacy, ensure_ascii=False)[:200])
check("[5] 旧任务原文保留（不因无分类就丢原文）", legacy["raw"] == "请求失败", repr(legacy.get("raw")))
check("[5] 历史文件未被改写（任务.json 字节不含 submit 键）",
      "submit" not in json.loads((TMP_ROOT / "auto-9807" / "任务.json").read_text(encoding="utf-8")),
      str(list(json.loads((TMP_ROOT / "auto-9807" / "任务.json").read_text(encoding="utf-8")))))

# ════════════════════════════════════════════════════════════ [6] 创建次数=0
print("6) unknown 夹具：创建次数 = 0（后端既有守卫，不新造）")
before = len(hub_api.HUB_JOBS)
raised = ""
try:
    hub_api.start_submit("auto-9801")
except Exception as e:  # noqa: BLE001 —— 探针要的是「被拒」，具体类型不断言死
    raised = str(e)
check("[6] unknown（submit_unknown+dispatch）上提交被拒，且报的是「可能已收单/先核对」口径",
      ("未知收单" in raised or "没拿到确定回执" in raised or "先核对" in raised), raised)
pure_unknown = ""
try:
    hub_api.start_submit("auto-9809")
except Exception as e:  # noqa: BLE001
    pure_unknown = str(e)
check("[6] 只有 submit_unknown（无 dispatch）也拒，文案点名「未知收单」",
      "未知收单" in pure_unknown, pure_unknown)
check("[6] 创建次数 = 0（HUB_JOBS 零新增，一次创建都没发）", len(hub_api.HUB_JOBS) == before,
      f"{before} → {len(hub_api.HUB_JOBS)}")
rejected_err = ""
try:
    hub_api.start_submit("auto-9803")
except Exception as e:  # noqa: BLE001
    rejected_err = str(e)
check("[6] 前置拒绝夹具不是被「未知收单」挡住（可走改配置重批这条正路）",
      "未知收单" not in rejected_err and "没拿到确定回执" not in rejected_err, rejected_err)

# ════════════════════════════════════════════════════════════ [7] 轮询稳定
print("7) 5 秒轮询整表替换：错误不消失")
first = {row["local_id"]: row["submit"] for row in hub_api.list_tasks()}
second = {row["local_id"]: row["submit"] for row in hub_api.list_tasks()}
check("[7] 两轮摘要：四条形态逐帧一致（不被下一轮抹掉）",
      all(first[k]["form"] == second[k]["form"] for k in first) and len(first) == 9, str(len(first)))
check("[7] 两轮摘要：unknown 的结论与原文都还在",
      second["auto-9801"]["conclusion"] == first["auto-9801"]["conclusion"]
      and second["auto-9801"]["raw"] == GEN_403)
check("[7] 两轮摘要：failed 的原文都还在", second["auto-9805"]["raw"] == "云端确认失败：内容被拒")

# ════════════════════════════════════════════════════════════ [8] 详情同源
print("8) 详情 get_task 与列表摘要同源（同一生产函数）")
for local_id in ("auto-9801", "auto-9804", "auto-9805"):
    detail = hub_api.get_task(local_id)
    check(f"[8] {local_id} 详情 submit 与摘要逐字段一致",
          detail.get("submit") == rows[local_id]["submit"],
          json.dumps(detail.get("submit"), ensure_ascii=False)[:160])

# ════════════════════════════════════════════════════════════ [9] 静态守卫
print("9) 静态守卫：一处生产、无新端点、不写盘")
hub_py = (ROOT / "ui" / "hub_api.py").read_text(encoding="utf-8")
server_py = (ROOT / "ui" / "server.py").read_text(encoding="utf-8")
view_py = (ROOT / "automation" / "submit_view.py").read_text(encoding="utf-8")
check("[9] 摘要里 submit 只产出一处（`\"submit\": submit_view.build` 恰好 1 次）",
      hub_py.count('"submit": submit_view.build') == 1, str(hub_py.count('"submit": submit_view.build')))
check("[9] 详情复用 _summary（不另算一份判定）",
      "d = _summary(task, task_dir)" in hub_py)
check("[9] 没有为提交结果另造端点（server.py 无 submit-result / submit-view 路由）",
      "submit-result" not in server_py and "submit_view" not in server_py)
check("[9] 判定源不写盘（submit_view 无 save/write/mkdir/rmtree）",
      not any(token in view_py for token in ("state.save(", "write_text(", "mkdir(", "rmtree(")))
check("[9] 判定顺序有据可查（注释里写明 active→unknown→dispatch→generating→rejected→failed）",
      "active_submit" in view_py and "dispatch prepared/dispatched" in view_py)

# ════════════════════════════════════════════════════════════ 收尾
hub_api.HUB_JOBS.clear()
shutil.rmtree(TMP_ROOT, ignore_errors=True)
print(f"\n清理：临时根已删（{TMP_ROOT} 存在={TMP_ROOT.exists()}）")
sys.exit(summary())
