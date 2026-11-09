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


def snapshot_hash(prompt: str, params: dict) -> str:
    """审批快照：prompt + 生成参数 的内容 hash（绑定批准与提交内容）。"""
    blob = json.dumps({"prompt": prompt, "params": params},
                      ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


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
    for key in ("approval", "approval_snapshot", "gen_params"):
        task.pop(key, None)                  # 预演参数/审批随提示词一起作废
    task["state"] = "awaiting_assets"
    task.setdefault("history", []).append({"at": _now(), "from": "awaiting_approval",
                                           "to": "awaiting_assets", "note": note})
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
