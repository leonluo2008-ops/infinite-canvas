# -*- coding: utf-8 -*-
"""AT-2 状态机引擎——任务.json 的状态字段、单向迁移、重启恢复。

状态链（ROADMAP 功能节点 v2）：
  ingested → discussing → awaiting_assets → rendering → awaiting_approval
           → approved → generating → done
  任意状态 → failed（带原文错误，禁静默降级）

审计修正（docs/审计-任务自动化-路2.md）：
  · 「单向」不等于「可恢复」：恢复动作 = 先按 bridge_id 查产物（桥侧任务
    has_* 三个标志/成片文件），再查 job——bridge JOBS 是内存态，重启后 job
    查询 404 但产物仍在，此时判「已完成」不重跑（产物优先）。
  · 审批绑定提交快照：approved 迁移必须带 snapshot_sha256（prompt+参数 hash）；
    之后任何输入变化 → 批准自动作废（回 awaiting_approval）。
  · busy 返回 = 排队，不是 failed（AT-6 调度职责，此处只提供状态语义）。

迁移守卫（不可绕过的停点）：
  · rendering 只能从 awaiting_assets 进（置换确认或全沿用显式确认后）；
  · approved 只能从 awaiting_approval 进且必须带审批快照；
  · generating 只能从 approved 进。

AT-9 增量（数据合同 B0，仅此两条）：
  · 受控回退边 awaiting_approval → awaiting_assets（rollback_to_assets，退回置换）；
  · 全沿用：awaiting_assets → rendering 允许空置换，前提 empty_confirm:true。
"""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

STATES = ["ingested", "discussing", "awaiting_assets", "rendering",
          "awaiting_approval", "approved", "generating", "done", "failed"]
_ORDER = {s: i for i, s in enumerate(STATES[:-1])}  # failed 特殊：任意态可进
_TASK_FILE = "任务.json"


class StateError(RuntimeError):
    """非法迁移/坏任务文件——如实抛出。"""


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def load(task_dir: Path) -> dict:
    mf = task_dir / _TASK_FILE
    try:
        d = json.loads(mf.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise StateError(f"任务文件读不了（{mf}）: {e}") from None
    if d.get("state") not in STATES:
        raise StateError(f"任务 {task_dir.name} 状态非法: {d.get('state')!r}")
    return d


def save(task_dir: Path, task: dict) -> None:
    mf = task_dir / _TASK_FILE
    tmp = mf.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(mf)  # 原子替换：中途 kill 不留半截任务文件


SNAPSHOT_VERSION = 2                     # AT-13 §2.5：当前审批快照版本（新任务）
CHANNEL_IDENTITY_KEYS = ("baseUrl", "apiFormat", "渠道名")   # 渠道身份三键（进 v2 快照，key 不进）
# 提交链运行时键（AT-13 B2）：一次提交尝试的"在途/未知/回执"痕迹。
# 作废/回退/受控恢复一律连根清掉——留着会让「有 dispatch、无 confirmed」的守卫挡住下一次正经提交。
SUBMIT_RUNTIME_KEYS = ("gen_job", "gen_task_id", "cloud_task_id", "submit_unknown",
                       "dispatch", "gen_error", "gen_progress")
# 退回置换时一并作废的键（AT-13 §2.5 清理键表扩展 + B2 提交链键）
ROLLBACK_CLEARED_KEYS = ("approval", "approval_snapshot", "gen_params",
                         "gen_channel", "snapshot_version") + SUBMIT_RUNTIME_KEYS


def _blob_hash(blob: dict) -> str:
    return hashlib.sha256(json.dumps(blob, ensure_ascii=False,
                                     sort_keys=True).encode("utf-8")).hexdigest()


def snapshot_hash(prompt: str, params: dict) -> str:
    """审批快照 v1：prompt + 生成参数 的内容 hash（绑定批准与提交内容）。

    AT-13 §2.5：这是**旧算法，逐字节保持不动**——老任务（任务.json 无 snapshot_version）
    一律按此比对；给 v1 输入 append 空 model 会让全部老任务 instantly stale（R2 实证）。
    """
    return _blob_hash({"prompt": prompt, "params": params})


def channel_identity(gen_channel: dict) -> dict:
    """渠道身份三键（缺键补空串，保证哈希输入结构稳定）。"""
    ch = gen_channel if isinstance(gen_channel, dict) else {}
    return {k: ("" if ch.get(k) is None else str(ch.get(k))) for k in CHANNEL_IDENTITY_KEYS}


def snapshot_hash_v2(prompt: str, params: dict, gen_channel: dict,
                     model: str | None = None, mode: str | None = None) -> str:
    """审批快照 v2（AT-13 §2.5）：prompt + params + 渠道身份 + model + mode。

    - 渠道身份=baseUrl+apiFormat+渠道名：同名模型换渠道 → 快照失效（§2.2）；
    - **key 不进快照**：只轮换 key 不作废批准；
    - model/mode 缺省从 gen_channel 取，显式传参可覆盖（探针/CLI 用）；None/空 → 空串，
      不省略字段（结构恒定，便于跨版本排障）。
    """
    ch = gen_channel if isinstance(gen_channel, dict) else {}
    model_value = ch.get("model") if model is None else model
    mode_value = ch.get("mode") if mode is None else mode
    return _blob_hash({
        "snapshot_version": SNAPSHOT_VERSION,
        "prompt": prompt,
        "params": params,
        "channel": channel_identity(ch),
        "model": "" if model_value is None else str(model_value),
        "mode": "" if mode_value is None else str(mode_value),
    })


def snapshot_version(task: dict) -> int:
    """任务自身的快照版本（无字段/坏值 = v1，老任务语义）。"""
    try:
        return int((task or {}).get("snapshot_version") or 1)
    except (TypeError, ValueError):
        return 1


def snapshot_for(task: dict, prompt: str) -> str:
    """按任务自身版本算「当前输入」的快照（AT-13 §2.5 版本分流唯一入口）。

    v1（无 snapshot_version）= prompt+params 旧算法；v2 = 再并入渠道身份/model/mode。
    approval_stale 仍是字符串比对——两侧必须同版本算出，故调用方一律走这里，不许自己拼。
    """
    task = task or {}
    params = task.get("gen_params") or {}
    if snapshot_version(task) >= SNAPSHOT_VERSION:
        return snapshot_hash_v2(prompt, params, task.get("gen_channel") or {})
    return snapshot_hash(prompt, params)


# 停点内建守卫：引擎层强制，不靠调用方自觉（审计路1 B2「不可绕过」）
#   rendering ← awaiting_assets：必须带置换确认（assets_confirmed）**或**全沿用显式确认
#   （empty_confirm，AT-9 数据合同状态机增量 2：空置换显式确认也放行）；
#   approved ← awaiting_approval：必须带审批快照（approval.snapshot_sha256）
_BUILTIN_GUARDS: dict[str, list[tuple[str, str]]] = {
    "approved": [("approval", "审批快照（approval.snapshot_sha256）")],
}


def transition(task_dir: Path, to_: str, *, note: str = "",
               require: dict | None = None, extra: dict | None = None) -> dict:
    """迁移状态（单向守卫）。require: 该迁移必带的键值（如 approved 必带 approval）。

    停点守卫由引擎内建（_BUILTIN_GUARDS）：approved 必须带 approval.snapshot_sha256、
    rendering 必须带 assets_confirmed——调用方不传就是非法迁移，无法绕过。
    """
    task = load(task_dir)
    cur = task["state"]
    if to_ == "failed":
        pass  # 任意态可进 failed
    elif to_ == "done":
        if cur != "generating":
            raise StateError(f"done 只能从 generating 进，当前 {cur}")
    elif cur == "failed" and to_ == "discussing":
        pass  # 人工重试拆解（工作台重试按钮 / CLI 重跑，2026-11-09）——failed 唯一合法出口
    else:
        if cur not in _ORDER or to_ not in _ORDER:
            raise StateError(f"未知状态: {cur} → {to_}")
        if _ORDER[to_] != _ORDER[cur] + 1:
            raise StateError(
                f"非法迁移 {cur} → {to_}（只能逐级前进；停点状态必须走确认流程）")
    for k, v in (require or {}).items():
        if not v:
            raise StateError(f"{cur} → {to_} 必须带 {k}（停点守卫，不可绕过）")
        _deep_set(task, k, v)
    # rendering 停点守卫（引擎内建，二选一）：正常置换确认 或 全沿用显式确认（AT-9 增量 2）
    if to_ == "rendering" and not (_dig(task, "assets_confirmed") or _dig(task, "empty_confirm")):
        raise StateError(
            f"{cur} → rendering 缺置换确认（assets_confirmed）或全沿用确认（empty_confirm）"
            "——停点守卫（引擎内建，不可绕过）")
    for key, why in _BUILTIN_GUARDS.get(to_, []):
        if not _dig(task, key):
            raise StateError(f"{cur} → {to_} 缺{why}——停点守卫（引擎内建，不可绕过）")
    task["state"] = to_
    task.setdefault("history", []).append({"at": _now(), "from": cur, "to": to_,
                                           "note": note})
    task.update(extra or {})
    save(task_dir, task)
    return task


def _dig(d: dict, dotted: str):
    cur: object = d
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _deep_set(d: dict, dotted: str, value) -> None:
    parts = dotted.split(".")
    cur = d
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = value


def rollback_to_assets(task_dir: Path, *, note: str = "退回置换") -> dict:
    """受控回退边（AT-9 数据合同状态机增量 1）：awaiting_approval → awaiting_assets。

    动作「退回置换」：丢弃当前提示词（提示词.md 删除，审批/预演参数一并作废），
    note 记进 history。这是唯一一条非 forward 的合法迁移——不走 transition（那里只有
    逐级前进），守卫在函数内：只有 awaiting_approval 能退。
    """
    task = load(task_dir)
    if task["state"] != "awaiting_approval":
        raise StateError(f"退回置换需 awaiting_approval，当前 {task['state']}")
    prompt_file = task_dir / "提示词.md"
    if prompt_file.is_file():
        prompt_file.unlink()                 # 丢弃当前提示词（合同：回退=丢弃）
    selfcheck_file = task_dir / "自检.json"
    if selfcheck_file.is_file():
        selfcheck_file.unlink()             # 自检结论属于被丢弃的那次渲染，一并作废（B5）
    for key in ROLLBACK_CLEARED_KEYS:
        task.pop(key, None)                  # 预演参数/审批/渠道身份/未知标记随提示词一起作废
    task["state"] = "awaiting_assets"
    task.setdefault("history", []).append({"at": _now(), "from": "awaiting_approval",
                                           "to": "awaiting_assets", "note": note})
    save(task_dir, task)
    return task


# ---------------------------------------------------------------- AT-13 B2：提交链受控迁移

def dispatch_phase(task: dict) -> str:
    """提交意图阶段（§2.3）：prepared → dispatched → confirmed｜cleared（无标记 = 空串）。

    结构：{"phase": str, "at": iso, "note": str, "job": bridge job, "carrier": wan 本地承载目录}。
    兼容早期扁平字符串写法（只读，不再写扁平值）。
    """
    raw = (task or {}).get("dispatch")
    if isinstance(raw, dict):
        return str(raw.get("phase") or "")
    return str(raw or "")


def dispatch_blocks_create(task: dict) -> bool:
    """「有 dispatch、无 confirmed 回执 → 禁建」的唯一判定（§2.3 R2 N-01）。

    prepared（已落意图、未发出）/dispatched（已发出、无回执）= 结果未知：所有发起方
    （hub HTTP / runner / CLI）都必须先人工核对渠道无单再解除，不许再创建。
    """
    return dispatch_phase(task) in ("prepared", "dispatched")


def reject_back_to_approval(task_dir: Path, *, note: str = "前置拒绝：受控回退重批",
                            error: str | None = None, extra: dict | None = None) -> dict:
    """受控回退边（AT-13 §2.4）：approved → awaiting_approval，**仅前置拒绝路径**。

    清 approval/approval_snapshot（批准作废，必须重批）+ 提交链运行时键；
    保留 assets_confirmed / empty_confirm / 提示词 / gen_params / gen_channel（草稿）。
    不走 rollback_to_assets（那条会丢提示词，属"退回置换"语义）。
    extra：随**同一次 save** 落盘的附加键（AT-16 B12 的图源自述 asset_host）——不另开写盘窗口。
    """
    task = load(task_dir)
    if task["state"] != "approved":
        raise StateError(f"前置拒绝回退需 approved，当前 {task['state']}")
    for key in ("approval", "approval_snapshot") + SUBMIT_RUNTIME_KEYS:
        task.pop(key, None)
    if error:
        task["gen_error"] = error
    if extra:
        task.update(extra)
    task["state"] = "awaiting_approval"
    task.setdefault("history", []).append({"at": _now(), "from": "approved",
                                           "to": "awaiting_approval", "note": note})
    save(task_dir, task)
    return task


def recover_orphan_generating(task_dir: Path, *, active_submit: bool = False,
                              cloud_in_flight: bool = False, cloud_task_id: str = "",
                              note: str = "generating 孤儿受控恢复") -> dict:
    """generating 孤儿受控恢复（AT-13 §2.4 R2 B-04 尾巴）：generating → awaiting_approval。

    四个守卫全过才动（任一不过如实抛，不硬迁）：
      ① 无活跃提交占用（active_submit=False）——提交线程还在跑=不是孤儿；
      ② 无云端在途证据（cloud_in_flight=False）——有 taskId 就续查，不恢复；
      ③ 无成片（成片.mp4 存在 → 走 done/续查，不许把已成片的任务退回）；
      ④ 提交意图已核对解除（dispatch 不是 prepared/dispatched、且无 submit_unknown 阻断）。
    动作：清 approval/approval_snapshot + 提交链运行时键，保留资产/提示词/gen_params/gen_channel，
    全程 history。不走 approved 借道。
    """
    task = load(task_dir)
    if task["state"] != "generating":
        raise StateError(f"孤儿恢复起态必须是 generating，当前 {task['state']}")
    if active_submit:
        raise StateError("任务还有活跃提交占用（提交进行中）——不是孤儿，禁恢复")
    if cloud_in_flight:
        raise StateError(f"有云端在途证据（taskId={cloud_task_id or '?'}）——先续查该任务，禁恢复")
    video = task_dir / "成片.mp4"
    if video.is_file() and video.stat().st_size > 0:
        raise StateError(f"成片已在（{video.stat().st_size} 字节）——走续查/完成路径，禁恢复")
    phase = dispatch_phase(task)
    if phase in ("prepared", "dispatched"):
        raise StateError(f"提交意图未核对解除（dispatch={phase}）——先到渠道控制台核对无单并解除阻断")
    if task.get("submit_unknown"):
        raise StateError("仍有「未知收单」阻断（submit_unknown）——先人工核对渠道无单并解除阻断")
    for key in ("approval", "approval_snapshot") + SUBMIT_RUNTIME_KEYS:
        task.pop(key, None)
    task["state"] = "awaiting_approval"
    task.setdefault("history", []).append({"at": _now(), "from": "generating",
                                           "to": "awaiting_approval", "note": note})
    save(task_dir, task)
    return task


def approval_stale(task_dir: Path, current_snapshot: str) -> bool:
    """输入变化是否已作废批准：approved/generating 态下快照不一致 → 作废。"""
    task = load(task_dir)
    if task["state"] in ("approved", "generating"):
        approved_sha = task.get("approval", {}).get("snapshot_sha256", "")
        return approved_sha != current_snapshot
    return False


def probe_bridge_products(bridge_id: str) -> dict | None:
    """恢复辅助：按 bridge_id 查桥任务产物标志（产物优先于 job——JOBS 是内存态）。

    返回 {has_disc, has_check, has_prompt} 或 None（任务不存在）。
    """
    from . import bridge  # 延迟导入，state 可脱离网络单测
    for t in bridge.tasks():
        if t.get("id") == bridge_id:
            return {"has_disc": bool(t.get("has_disc")),
                    "has_check": bool(t.get("has_check")),
                    "has_prompt": bool(t.get("has_prompt"))}
    return None
