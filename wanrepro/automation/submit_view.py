#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""提交结果说明的**唯一生产者**（AT-16 B5a-1 · D6 展示面）。

为什么要有这个模块
------------------
auto-0043 实证：批准即自动提交 → 渠道 403 落盘 ``gen_error``，但①状态标签仍按 ``state``
显示「已批准·待提交」②错误只弹一次性 toast（列表零信息）③原文是英文 JSON 堆叠④重提入口
藏在抽屉底部。用户既不知道「现在到底提交到哪一步」，也不知道「下一步该干什么」。

本模块只做一件事：把**既有持久事实**装配成「四类形态 + 人话」。
判定源（不新造状态机、不写盘、不迁移状态）：

    active_submit            本进程是否有提交线程在跑（HUB_JOBS）
    dispatch.phase           prepared / dispatched / confirmed / cleared（未完结=结果未知）
    submit_unknown           未知收单持久阻断标记
    gen_error                脱敏后原文（有无 + 短错误码）
    submit_unknown.reason    旧阻断原文（自愈判定要读它——gen_error 可能被后续消息覆盖）
    state                    后端状态（generating / awaiting_approval / failed / …）
    cloud_task_id / gen_task_id  云端任务号（续查只认它）
    gen_job                  本地提交 job（有它 = 已在生成链上，不是创建被拒）

四类形态（与 docs/nodes/AT-16.md §B5a-1 一一对应）：

    ① unknown   结果未知：可能已收单，先核对 → **禁重提**，给续查/人工解除
    ② rejected  已拒绝：可改后重提（渠道**创建端点** 4xx 明确拒绝 → 任务没建成：
                400/403/409/422…；含旧版把 403 fail_to_fetch_task 误标成「结果未知」的那些）
    ③ inflight  云端处理中 / 等待超时：按 taskId 续查 → 禁重提
    ④ failed    明确渠道失败且无未知阻断：可重试

人话纪律（审计 A6）：渠道 403 只能证明「HTTP 403 + 响应正文」，**证不了** key 失效 /
额度尽 / 渠道故障中的哪一个——所以可能原因一律并列，不翻译成唯一原因，脱敏原文保留可展开。

历史兼容：旧任务的 gen_error 没有分类也照样出「人话壳 + 原文」，不假装有结论（只读）。
"""

from __future__ import annotations

import re

from . import state

# ---------------------------------------------------------------- 形态常量（前后端同字面量）

FORM_NONE = "none"          # 无提交结果可讲（未提交 / 已交付 / 正常待批准）
FORM_UNKNOWN = "unknown"    # ① 结果未知（submit_unknown / dispatch 未完结）
FORM_REJECTED = "rejected"  # ② 前置拒绝（渠道 400/409 或本地预检）→ 可改后重提
FORM_INFLIGHT = "inflight"  # ③ 在途 / 等待超时 → 按 taskId 续查
FORM_FAILED = "failed"      # ④ 明确渠道失败（无未知阻断）→ 可重试

FORMS = (FORM_NONE, FORM_UNKNOWN, FORM_REJECTED, FORM_INFLIGHT, FORM_FAILED)

# 短错误码只做「可检索的标记」，不做因果翻译。
_HTTP_RE = re.compile(r"HTTP\s*(\d{3})")
_TOKEN_RE = re.compile(r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b")
# 创建阶段渠道 4xx 的**原文标记**（两种形态都认，都是渠道原文里本来就有的字）：
#   · B11 起：bridge 把创建链的 4xx 原码透传，原文「渠道创建端点明确拒绝 HTTP 4xx」
#   · B11 前：一律包成 502，原文只剩「渠道响应 HTTP 4xx」（auto-0043 现场）
_CREATE_4XX_RE = re.compile(r"(?:渠道创建端点[^\n]{0,24}?|渠道响应\s*)HTTP\s*(4\d{2})")


def error_code(text: str) -> str:
    """从脱敏原文里抠一个**短错误码**（HTTP 403 / video_request_failed / 空）。

    只抽「原文里本来就有的标记」——不推断语义、不把 403 翻译成「key 失效」。
    """
    raw = str(text or "").strip()
    if not raw:
        return ""
    hit = _HTTP_RE.search(raw)
    if hit:
        return f"HTTP {hit.group(1)}"
    token = _TOKEN_RE.search(raw)
    return token.group(1)[:48] if token else ""


def create_reject_code(text: str) -> str:
    """创建阶段渠道 4xx 标记 → 状态码串（'' = 不是「创建端点明确拒绝」）。

    只认原文标记，不推断「key 失效 / 额度尽」之类语义（人话纪律）。查询/下载阶段的
    4xx 也带 HTTP 码，但**不是**创建拒绝——调用方必须同时要求「无云端 task_id / 无 gen_job」。
    """
    hit = _CREATE_4XX_RE.search(str(text or ""))
    return hit.group(1) if hit else ""


def create_reject_evidence(task: dict) -> str:
    """「旧版把创建阶段 4xx 误标成未知收单」的判定证据（'' = 无）。

    历史误标的三件套同时在才算：①原文里有创建端点 4xx 标记 ②没拿到云端 task_id / 本地
    job（= 任务确实没建成）③任务上还压着阻断标记（submit_unknown 或 dispatch 未完结）。
    命中即说明那条阻断的**唯一前提**（防重复扣费）不成立 —— 供写路径自愈，只读不改盘。
    """
    f = _facts(task)
    if f["cloud"] or f["job"]:
        return ""
    if not (f["unknown"] or f["phase"] in ("prepared", "dispatched")):
        return ""
    return create_reject_code(f["raw"] or f["unknown_reason"])


def _facts(task: dict) -> dict:
    task = task if isinstance(task, dict) else {}
    raw = str(task.get("gen_error") or "").strip()
    blocked = task.get("submit_unknown")
    if isinstance(blocked, dict):
        reason = str(blocked.get("reason") or "").strip()
    else:
        reason = str(blocked or "").strip()
    return {
        "state": str(task.get("state") or ""),
        "raw": raw,
        "unknown_reason": reason,
        "phase": state.dispatch_phase(task),
        "unknown": bool(task.get("submit_unknown")),
        "cloud": str(task.get("cloud_task_id") or task.get("gen_task_id") or "").strip(),
        "job": str(task.get("gen_job") or "").strip(),
    }


def _form(f: dict, active_submit: bool) -> str:
    """四类形态判定（**唯一判定源**；顺序固定，前端不得再按 state+gen_error 重判）。

    顺序 = 先阻断事实、再在途、再回退态、最后终态失败：
      1. active_submit            → inflight（本地线程还在跑，结果未定）
      2. 创建阶段渠道 4xx          → rejected（B11：渠道说「任务没建成」，不是「无法证明未收单」。
                                    要求无云端 task_id / 无 gen_job，且 state 不是 generating
                                    ——生成态只可能来自已有 taskId 的那条链，绝不当成创建被拒）
      3. submit_unknown           → unknown（持久阻断，未解除前一切重提都会被后端拒）
      4. dispatch prepared/dispatched → unknown（请求可能已发出，云端可能已收单）
      5. state=generating         → inflight（有 taskId 走续查；无 taskId 见人话）
      6. awaiting_approval+gen_error → rejected（_pre_reject 受控回退留下的原文）
      7. state=failed             → failed（云端确认终态失败）
      8. 其余带 gen_error 的组合   → unknown（无法证明「没被收单」就按未知，禁盲目重提）
    """
    if active_submit:
        return FORM_INFLIGHT
    if create_reject_code(f["raw"] or f["unknown_reason"]) and not f["cloud"] and not f["job"] \
            and f["state"] != "generating":
        return FORM_REJECTED
    if f["unknown"]:
        return FORM_UNKNOWN
    if f["phase"] in ("prepared", "dispatched"):
        return FORM_UNKNOWN
    if f["state"] == "generating":
        return FORM_INFLIGHT
    if f["state"] == "awaiting_approval" and f["raw"]:
        return FORM_REJECTED
    if f["state"] == "failed":
        return FORM_FAILED
    if f["raw"]:
        return FORM_UNKNOWN
    return FORM_NONE


def _unknown_text(f: dict) -> dict:
    cloud = f["cloud"]
    causes = [
        "可能：请求已经发出、渠道也收了单，只是回执丢了或不是可解析的 JSON。",
        "可能：请求根本没到渠道（断连 / 超时 / 并发闸挡住），本地无从证伪。",
    ]
    if f["phase"]:
        causes.append(f"可能：本地只记到 dispatch={f['phase']}——已落提交意图但缺确定回执，"
                      "仍无法证明「发出去了」还是「没发出去」。")
    guidance = [
        "先到渠道控制台按时间 / 提示词核对有没有这一单。",
        cloud and f"核对到单 → 按云端 taskId {cloud} 续查（只查不重提）。"
        or "核对到单 → 拿到云端 taskId 后按该 id 续查（只查不重提）。",
        "确认渠道确实没有这一单 → 走「解除阻断」后再提交（人工动作，会记任务历史）。",
    ]
    return {
        "label": "结果未知·先核对",
        "conclusion": "结果未知：可能已收单，先核对",
        "causes": causes,
        "guidance": guidance,
        "blocks_retry": True,
        "can_resume": bool(cloud),
    }


def _rejected_text(f: dict) -> dict:
    code = create_reject_code(f["raw"] or f["unknown_reason"])
    causes = [
        f"渠道在创建端点明确拒绝（原文标记 HTTP {code}）：任务没建成——参数 / 模型 / 额度 / "
        f"鉴权哪一项不符以原文为准。"
        if code else
        "可能：渠道前置校验挡下（HTTP 400/409——参数不被接受 / 并发闸满 / 状态不符）。",
        "可能：本地预检没过（缺配置、上传图读到 / 转 dataUrl 失败、批准快照失效）。",
        "本次没有拿到云端 taskId，属「没发出去」的回退态，不是未知收单。",
    ]
    guidance = [
        "按原文改配置（模型 / 渠道 / 画幅 / 时长 / key）。",
        "改完重新批准后直接重提（本次渠道没建单，不产生付费单；无需核对、也无需解除阻断）。",
        f"原文给了 Request ID：按渠道要求凭它联系渠道支持（HTTP {code} 原文见下）。"
        if code else
        "原文里若给出 Request ID，按渠道要求凭它联系渠道支持。",
    ]
    return {
        "label": "已拒绝·可改后重提",
        "conclusion": "已拒绝：可改后重提",
        "causes": causes,
        "guidance": guidance,
        "blocks_retry": False,
        "can_resume": False,
    }


def _inflight_text(f: dict, active_submit: bool) -> dict:
    cloud = f["cloud"]
    if active_submit:
        causes = [
            "本地提交线程仍在跑（排队 / 已发出等云端回执），结果还没定。",
            "重复点提交会重复扣费，后端会直接拒。",
        ]
        conclusion = "提交处理中：等回执，别重复提交"
        label = "提交处理中·勿重复"
        guidance = [
            "进度 5 秒自动刷新；结果出来会变成成片或失败红条。",
            "这期间「提交生成」与重试都不可用。",
        ]
    elif cloud:
        causes = [
            "云端任务仍在跑：远程模型通常 5–15 分钟，慢时更久。",
            "本地等待超时 ≠ 云端失败（超时只说明本地没等到回执）。",
        ]
        conclusion = "云端处理中：按 taskId 续查"
        label = "等待超时·续查" if f["raw"] else "云端处理中·续查"
        guidance = [
            f"按云端 taskId {cloud} 续查（只查不重提）。",
            "别重新提交——云端那份可能已在跑，重提会重复扣费。",
        ]
    else:
        causes = [
            "可能：等待超时 / 回执丢了，本地没有云端 taskId 可续查。",
            "可能：云端确实没收单（这时才需要人工核对后受控恢复）。",
        ]
        conclusion = "在途状态待核对：没有云端 taskId"
        label = "在途待核对·无 taskId"
        guidance = [
            "先到渠道控制台核对有没有这一单（只读核对，别急着提交）。",
            "详情里走「受控恢复 / 续查」入口；不许直接重提。",
        ]
    return {
        "label": label,
        "conclusion": conclusion,
        "causes": causes,
        "guidance": guidance,
        "blocks_retry": True,
        "can_resume": bool(cloud),
    }


def _failed_text(f: dict) -> dict:
    cloud = f["cloud"]
    return {
        "label": "失败·可重试",
        "conclusion": "失败：可重试",
        "causes": [
            "云端给出明确终态失败（不是超时）——可能：key 失效 / 额度用尽 / 渠道故障 / "
            "内容被拒，具体以渠道原文为准。",
        ],
        "guidance": [
            "先按原文修配置（key / 额度 / 模型 / 内容）再重新提交。",
            cloud and f"重提前若云端还留 taskId {cloud}，先续查确认没有成片再提交。"
            or "重提前确认渠道没有残留任务（有就按 taskId 续查，别重复扣费）。",
        ],
        "blocks_retry": False,
        "can_resume": False,
    }


def build(task: dict, *, active_submit: bool = False) -> dict:
    """任务事实 → 提交诊断（摘要/详情同源消费；纯读，不写盘）。"""
    f = _facts(task)
    form = _form(f, bool(active_submit))
    code4 = create_reject_code(f["raw"] or f["unknown_reason"])
    out = {
        "form": form,
        "label": "",
        "conclusion": "",
        "causes": [],
        "guidance": [],
        # ---- 最小提交诊断（摘要也要透出，列表不拉详情就能说实话）----
        # 创建阶段 4xx 的短码取**渠道那一手**（HTTP 403），不取包在外面的 502 壳：
        # 列表上「HTTP 502」会把人引向「网关挂了」，而原文说的是「任务没建成」（B11）。
        "error_code": f"HTTP {code4}" if code4 else error_code(f["raw"]),
        "raw": f["raw"],
        "dispatch_phase": f["phase"],
        "submit_unknown": f["unknown"],
        "active_submit": bool(active_submit),
        "has_gen_error": bool(f["raw"]),
        "cloud_task_id": f["cloud"],
        "blocks_retry": False,
        "can_resume": False,
    }
    if form == FORM_UNKNOWN:
        out.update(_unknown_text(f))
    elif form == FORM_REJECTED:
        out.update(_rejected_text(f))
    elif form == FORM_INFLIGHT:
        out.update(_inflight_text(f, bool(active_submit)))
    elif form == FORM_FAILED:
        out.update(_failed_text(f))
    return out
