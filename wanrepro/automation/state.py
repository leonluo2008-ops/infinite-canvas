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
import os
import threading
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
                         "gen_channel", "snapshot_version", "audio_gate") + SUBMIT_RUNTIME_KEYS


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


# ============================================================================
# AT-18：假运行态（进程死、状态活）的「锚 + 判定 + 受控降级」
#
# 方案冻结稿 v2（docs/方案-假运行态判定与出清-20261112.md，卡 t_daef20d2）§1–§3。
# 本段只做三件事，全部落在状态机模块内，其它模块只调用、不重写：
#   · 锚：心跳侧车 `<任务目录>/心跳.json` 的读写原语（单写者、原子替换）。
#         为什么另起文件而不写进 任务.json：transition() 是 load→改→save、**全程不持
#         task_lock**（跨进程 CLI 也调它，线程锁根治不了），60s 心跳若也写同一份
#         任务.json，两边必然互相覆盖——最坏情况心跳把已推进到 awaiting_assets 的
#         状态覆盖回 discussing（状态倒退）。两个写族物理分离，读时在调用方合并。
#   · 判定：activity_snapshot()（六源取 max）+ judge_lost_run()（纯函数，不写盘）。
#   · 降级：fail_orphan_task()（唯一写入口；幂等；只写 5 个键，绝不删文件/产物）。
#
# 与 render.py 提示词链**零交集**：本段不 import render，不读不写任何渲染字段。
# ============================================================================

# 状态机里的真实运行态（与 ui/hub_api.py 的 RUNNING_STATES 同集合，但只含状态机
# 有的三个；「ingesting」不是状态机状态，本段不认它）。
RUNNING_STATES_MACHINE = ("discussing", "rendering", "generating")

# 冻结值（方案 §2.1）：改动必须回方案评审，并重跑 tests/test_state_lostrun.py 的反例夹具。
FAKE_RUN_SILENCE_SECONDS = 900     # 连续静默 ≥ 该值即判「假运行」（= 183s 最长合法空档的 4.9 倍）
HEARTBEAT_SECONDS = 60             # 心跳间隔（复用 discuss.HEARTBEAT_INTERVAL=60，不新造数值）
LOST_RUN_WATCHDOG_SECONDS = 300    # 后台对账扫描周期
STALE_JOB_SECONDS = 900            # 僵尸 JOB 记录判定阈值（与主阈值同值，排障只记一个数）

# 自动降级只覆盖这两个态（generating 永不自动降级，见 §3.2 G3）。
FAKE_RUN_DEGRADABLE_STATES = ("discussing", "rendering")

# 唯一给用户看的失败文案源（前端不得自造文案）。
FAKE_RUN_REASONS = {
    "discussing": "拆解进程失联，可重试或删除",
    "rendering": "渲染进程失联，可重试或删除",
}
FAKE_RUN_REASON_FALLBACK = "进程失联，可重试或删除"

# 活动证据六源（冻结枚举：写入 activity.source / failed_evidence.last_activity_source）。
ACTIVITY_SOURCES = ("心跳", "状态迁移", "阶段锚", "任务目录产物", "拆解产物", "起跑锚")

HEARTBEAT_FILE = "心跳.json"
_HEARTBEAT_SCHEMA = 1
_ARTIFACT_DEPTH = 2                # 产物清单浅扫：不递归超过 2 层
_ARTIFACT_EXCLUDE = (_TASK_FILE, HEARTBEAT_FILE, "拆解日志.txt")   # §1.3：digest 不许自证自变
_ACTIVITY_EXCLUDE = (_TASK_FILE, HEARTBEAT_FILE)                   # 活动证据：日志 mtime 仍算
_ANALYSIS_REVERSE = Path(__file__).resolve().parents[1] / "analysis" / "reverse"

_ORPHAN_LOCK = threading.Lock()    # 只保护 fail_orphan_task 的判定-写盘临界区；transition() 不变


def fake_run_reason(state_name: str) -> str:
    """该状态的失联文案（只有 discussing/rendering 有专属文案，其余给通用文案）。"""
    return FAKE_RUN_REASONS.get(str(state_name or ""), FAKE_RUN_REASON_FALLBACK)


# ---------------------------------------------------------------- 心跳侧车读写（锚）

def heartbeat_path(task_dir: Path) -> Path:
    return Path(task_dir) / HEARTBEAT_FILE


def _heartbeat_blank() -> dict:
    return {"schema": _HEARTBEAT_SCHEMA, "run_id": "", "started_at": "", "last_beat_at": "",
            "beat_count": 0, "phase": "", "artifact_count": 0, "artifact_digest": "",
            "ended_at": "", "outcome": ""}


def heartbeat_read(task_dir: Path) -> dict:
    """读心跳侧车（纯读，**不抛**）：缺文件/坏 JSON → 全空结构（=「进程从未跑起」可识别）。

    全空结构 + 判定层只看 `last_beat_at`/`started_at` 是否为空，就足以把「从未起跑」
    与「跑过但静默」分开——这正是 auto-0052 的形态所要求的。
    """
    out = _heartbeat_blank()
    try:
        raw = json.loads(heartbeat_path(task_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return out
    if not isinstance(raw, dict):
        return out
    for key in ("run_id", "started_at", "last_beat_at", "phase", "artifact_digest",
                "ended_at", "outcome"):
        value = raw.get(key)
        out[key] = "" if value is None else str(value)
    for key in ("schema", "beat_count", "artifact_count"):
        try:
            out[key] = int(raw.get(key) or 0)
        except (TypeError, ValueError):
            out[key] = 0
    return out


def _heartbeat_write(task_dir: Path, data: dict) -> dict:
    target = heartbeat_path(task_dir)
    tmp = target.with_name(HEARTBEAT_FILE + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(target)            # 原子替换：中途 kill 不留半截心跳
    return data


def heartbeat_begin(task_dir: Path, *, run_id: str, phase: str = "") -> dict:
    """一次 run 起手：整体覆盖写心跳（旧 run_id 作废）。**必须在起线程之前调用**。

    这样「线程根本没起来」（Thread.start() 失败 / 进程在起手后崩 / 桥连不上就退出）
    也有一份带 started_at 的心跳，判定窗口从发起时刻起算，而不是等线程内部第一次写。
    last_beat_at 保持空串 = 本次 run 从未收到心跳（初值可被识别，不许伪造一次心跳）。
    """
    task_dir = Path(task_dir)
    data = _heartbeat_blank()
    data["run_id"] = str(run_id or "")
    data["started_at"] = _now()
    data["phase"] = str(phase or "")
    data["artifact_count"], data["artifact_digest"] = _artifact_fingerprint(task_dir)
    return _heartbeat_write(task_dir, data)


def heartbeat_beat(task_dir: Path, *, phase: str | None = None) -> dict:
    """一次心跳：只写心跳文件的 last_beat_at/beat_count/artifact_*（+ phase 变化时同步）。

    **绝不碰 任务.json**（AT-16 B4b 合同：等待期心跳借道阶段回调会把阶段计时清零）。
    """
    task_dir = Path(task_dir)
    data = heartbeat_read(task_dir)
    data["schema"] = _HEARTBEAT_SCHEMA
    if not data["started_at"]:
        data["started_at"] = _now()          # 没有起手写（旧任务/异常路径）也不许伪造历史
    data["last_beat_at"] = _now()
    data["beat_count"] = int(data.get("beat_count") or 0) + 1
    if phase is not None and str(phase) != "":
        data["phase"] = str(phase)
    data["artifact_count"], data["artifact_digest"] = _artifact_fingerprint(task_dir)
    return _heartbeat_write(task_dir, data)


def heartbeat_finish(task_dir: Path, *, outcome: str) -> dict:
    """一次 run 收尾：写 ended_at + outcome（仅诊断，不参与判定）。"""
    data = heartbeat_read(task_dir)
    data["ended_at"] = _now()
    data["outcome"] = str(outcome or "")
    return _heartbeat_write(Path(task_dir), data)


# ---------------------------------------------------------------- 产物清单与指纹（锚）

def _iter_files(base: Path, *, depth: int = _ARTIFACT_DEPTH, exclude_top=()):
    """浅层文件清单：不超 depth 层、不跟随符号链接、单条 OSError 跳过（并发删除/无权限/路径过长）。

    产量 (相对路径 posix, size, mtime_ns)。目录本身不产条目——「进程从未跑起」时
    只有空 inbox/ 目录，不该被当成产物活动。
    """
    base = Path(base)
    exclude_top = frozenset(exclude_top)
    stack = [(base, 0)]
    while stack:
        directory, level = stack.pop()
        try:
            with os.scandir(directory) as scan:
                entries = list(scan)
        except OSError:
            continue                      # 目录读不了：整目录跳过，不中断判定
        for entry in entries:
            try:
                if entry.is_symlink():     # 不跟随符号链接（防环、防越界）
                    continue
                if entry.is_dir():
                    if level + 1 < depth:
                        stack.append((Path(entry.path), level + 1))
                    continue
                if not entry.is_file():
                    continue
                if level == 0 and entry.name in exclude_top:
                    continue
                stat = entry.stat()
            except OSError:
                continue
            rel = Path(entry.path).relative_to(base).as_posix()
            yield rel, stat.st_size, stat.st_mtime_ns


def artifact_manifest(task_dir: Path, task: dict | None = None) -> list[dict]:
    """产物清单（口径冻结 §1.3）：任务目录（除 任务.json/心跳.json/拆解日志.txt）
    + analysis/reverse/<bridge_id>/ 的浅层清单，元素 (相对路径, size, mtime_ns)，按路径排序。

    两条排除都是硬要求：任务.json/心跳.json 会让锚自证自变；拆解日志.txt 每 60s 被心跳
    追加，算进去则「心跳活着但产物不动」永远检不出来（日志 mtime 仍是活动证据，两套口径）。
    """
    task_dir = Path(task_dir)
    items = [{"path": rel, "size": size, "mtime_ns": mtime_ns}
             for rel, size, mtime_ns in _iter_files(task_dir, exclude_top=_ARTIFACT_EXCLUDE)]
    bridge_id = str((task or {}).get("bridge_id") or "").strip()
    if bridge_id:
        base = _ANALYSIS_REVERSE / bridge_id
        items.extend({"path": f"analysis/reverse/{bridge_id}/{rel}", "size": size,
                      "mtime_ns": mtime_ns}
                     for rel, size, mtime_ns in _iter_files(base))
    items.sort(key=lambda item: item["path"])
    return items


def artifact_digest(task_dir: Path, task: dict | None = None) -> str:
    """产物指纹（`sha256:` + 清单 sha256）。用途只有两条：留痕、诊断「心跳在动但产物不动」。"""
    blob = json.dumps([[item["path"], item["size"], item["mtime_ns"]]
                       for item in artifact_manifest(task_dir, task)],
                      ensure_ascii=False, sort_keys=True)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _artifact_fingerprint(task_dir: Path) -> tuple[int, str]:
    try:
        task = _task_or_load(task_dir, None)
    except StateError:
        task = {}                      # 坏任务也要能写心跳：指纹退化成「任务目录半边」
    items = artifact_manifest(task_dir, task)
    blob = json.dumps([[item["path"], item["size"], item["mtime_ns"]] for item in items],
                      ensure_ascii=False, sort_keys=True)
    return len(items), "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _max_mtime_ns(base: Path, *, exclude_top=()) -> int | None:
    best: int | None = None
    for _rel, _size, mtime_ns in _iter_files(base, exclude_top=exclude_top):
        if best is None or mtime_ns > best:
            best = mtime_ns
    return best


# ---------------------------------------------------------------- 活动快照与判定

def _parse_iso(value) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone() if parsed.tzinfo is None else parsed


def _iso_from_ns(mtime_ns: int) -> str:
    return (datetime.fromtimestamp(mtime_ns / 1e9, tz=timezone.utc)
            .astimezone().isoformat(timespec="seconds"))


def _task_or_load(task_dir: Path, task: dict | None) -> dict:
    return task if isinstance(task, dict) else load(Path(task_dir))


def activity_snapshot(task_dir: Path, task: dict | None = None, *, now=None) -> dict:
    """活动锚快照（纯读，不写盘）。六源取 max —— 「心跳与产物双零变化」的可判定写法。

    判定是**时间戳证据的函数**，不是「前后两次观测的差分」：文件 mtime / 心跳时刻 /
    history 时刻都是持久事实，所以进程或服务重启后存量任务立刻可判（不需要扫两轮）。
    """
    task_dir = Path(task_dir)
    task = _task_or_load(task_dir, task)
    now_dt = _parse_iso(now) or datetime.now(timezone.utc).astimezone()
    scan_error = ""
    collected: dict[str, list[str]] = {}

    def _add(source: str, value) -> None:
        if isinstance(value, str) and value.strip() and _parse_iso(value):
            collected.setdefault(source, []).append(value.strip())

    heartbeat = heartbeat_read(task_dir)
    _add("心跳", heartbeat["last_beat_at"])
    _add("起跑锚", heartbeat["started_at"])
    history = task.get("history") or []
    if isinstance(history, list) and history and isinstance(history[-1], dict):
        _add("状态迁移", history[-1].get("at"))
    for key in ("lapian_started_at", "lapian_stage_at"):
        _add("阶段锚", task.get(key))
    try:
        latest = _max_mtime_ns(task_dir, exclude_top=_ACTIVITY_EXCLUDE)
        if latest is not None:
            _add("任务目录产物", _iso_from_ns(latest))
        bridge_id = str(task.get("bridge_id") or "").strip()
        if bridge_id:
            latest = _max_mtime_ns(_ANALYSIS_REVERSE / bridge_id)
            if latest is not None:
                _add("拆解产物", _iso_from_ns(latest))
    except OSError as exc:                      # 兜不住的单条异常：只记一条 scan_error
        scan_error = f"{type(exc).__name__}: {exc}"

    sources = {source: max(values, key=lambda v: _parse_iso(v))
               for source, values in collected.items()}
    if sources:
        source = max(sources, key=lambda s: (_parse_iso(sources[s]),
                                            -ACTIVITY_SOURCES.index(s)))
        last_activity_at = sources[source]
        silence_seconds = int((now_dt - _parse_iso(last_activity_at)).total_seconds())
    else:
        source, last_activity_at, silence_seconds = "", "", 0   # 无证据不判死（见 judge）

    heartbeat_age = None
    if _parse_iso(heartbeat["last_beat_at"]):
        heartbeat_age = int((now_dt - _parse_iso(heartbeat["last_beat_at"])).total_seconds())

    probe = {
        "last_activity_at": last_activity_at,
        "source": source,
        "silence_seconds": silence_seconds,
        "threshold_seconds": FAKE_RUN_SILENCE_SECONDS,
        "heartbeat_at": heartbeat["last_beat_at"],
        "heartbeat_age_seconds": heartbeat_age,
        "heartbeat_run_id": heartbeat["run_id"],
        "artifact_count": heartbeat["artifact_count"],
        "artifact_digest": heartbeat["artifact_digest"],
        "artifact_files": len(artifact_manifest(task_dir, task)),
        "sources": sources,
        "scanned_at": now_dt.isoformat(timespec="seconds"),
    }
    if scan_error:
        probe["scan_error"] = scan_error
    return probe


def cloud_in_flight(task: dict) -> bool:
    """`generating` 的云端在途证据（§3.2 G3 真值表）：有 cloud_task_id / 提交意未核销 / 结果未知。

    有在途证据必须续查，判死可能导致重复付费提交（AT-13 合同）。
    """
    task = task or {}
    if str(task.get("cloud_task_id") or "").strip():
        return True
    if dispatch_phase(task) in ("prepared", "dispatched"):
        return True
    return bool(task.get("submit_unknown"))


def judge_lost_run(task: dict, probe: dict, *, now=None,
                   held_job: bool = False) -> tuple[bool, str]:
    """判定是否「假运行」。返回 (是否假运行, 原因码)。

    原因码（冻结）：`""` 判定为假运行（可降级）／`not_running` 状态不在运行态／
    `silence_below_threshold` 静默未满阈值／`in_flight_cloud` generating 有云端在途证据／
    `clock_back` now < last_activity_at（时钟回拨，本轮跳过）／`held_job` 本进程内该任务的
    job 仍活着且心跳新鲜／`manual_only_state` generating 无在途证据但自动扫描也不许降级
    （只能走人工强制结束）。

    **纯函数**：不写盘、不读 HUB_JOBS（内存态可缺失；`held_job` 由调用方作为入参传入）。
    """
    task = task or {}
    probe = probe or {}
    state_name = str(task.get("state") or "")
    if state_name not in RUNNING_STATES_MACHINE:
        return False, "not_running"
    if state_name == "generating":
        if cloud_in_flight(task):
            return False, "in_flight_cloud"
        return False, "manual_only_state"
    if held_job or probe.get("held_job"):
        return False, "held_job"
    now_dt = _parse_iso(now) or datetime.now(timezone.utc).astimezone()
    last_dt = _parse_iso(probe.get("last_activity_at"))
    if last_dt is not None:
        silence_seconds = int((now_dt - last_dt).total_seconds())
        if silence_seconds < 0:
            return False, "clock_back"
    else:
        silence_seconds = int(probe.get("silence_seconds") or 0)
    if silence_seconds < FAKE_RUN_SILENCE_SECONDS:
        return False, "silence_below_threshold"
    return True, ""


# ---------------------------------------------------------------- 受控降级（唯一写入口）

def fail_orphan_task(task_dir: Path, *, reason: str, probe: dict | None = None,
                     trigger: str = "auto_scan") -> dict:
    """运行态 → failed 的唯一受控入口（方案 §3.3；T1/T2 两条显式边）。

    返回 {"changed": bool, "state": "failed", "state_was": str, "reason": str, "at": iso}
    · 已是 failed → changed=False / already=True，**不写 history、不刷新 failed_at**（幂等）；
    · 状态不在运行态 → StateError("任务当前不是运行态（<state>）")；
    · trigger="auto_scan" 时在写盘前**重新过一遍守卫**（G1 运行态 / G2 静默 ≥900s /
      G3 无在途证据 / G4 调用方传入的 held_job），未过即拒降级——守卫不只在扫描器里；
    · 只写 5 个键：state / history(追加一条) / failed_reason / failed_at / failed_evidence；
    · **绝不**：删文件、清 pending_replace、清 approval、动产物、动 lapian_*。

    为什么不用 transition()：它的 failed 边是「任意态可进」，会把判定窗口内**并发推进**
    到 awaiting_assets 的任务也判死。这里写盘形态与 transition() 完全一致（history 同形状
    + 一次 save），但在写盘前重读校验起态未变；起态变了 → 放弃本轮（changed=False）。
    """
    task_dir = Path(task_dir)
    with _ORPHAN_LOCK:
        task = load(task_dir)
        state_was = task["state"]
        if state_was == "failed":
            return {"changed": False, "already": True, "state": "failed",
                    "state_was": "failed",
                    "reason": str(task.get("failed_reason") or ""),
                    "at": str(task.get("failed_at") or "")}
        if state_was not in RUNNING_STATES_MACHINE:
            raise StateError(f"任务当前不是运行态（{state_was}）")
        reason = str(reason or "").strip() or fake_run_reason(state_was)
        if trigger == "auto_scan":
            snapshot = probe if isinstance(probe, dict) else activity_snapshot(task_dir, task)
            lost, reason_code = judge_lost_run(task, snapshot,
                                               held_job=bool(snapshot.get("held_job")))
            if not lost:
                raise StateError(f"未达假运行判定（{reason_code or 'unknown'}），拒绝降级")
            probe = snapshot
        probe = probe or {}
        at = _now()
        evidence = {
            "trigger": str(trigger or "auto_scan"),
            "silence_seconds": int(probe.get("silence_seconds") or 0),
            "last_activity_at": str(probe.get("last_activity_at") or ""),
            "last_activity_source": str(probe.get("source") or ""),
            "heartbeat_at": str(probe.get("heartbeat_at") or ""),
            "threshold_seconds": FAKE_RUN_SILENCE_SECONDS,
            "run_id": str(probe.get("heartbeat_run_id") or ""),
            "decided_at": at,
        }
        fresh = load(task_dir)
        if fresh.get("state") != state_was:
            return {"changed": False, "already": fresh.get("state") == "failed",
                    "state": str(fresh.get("state") or ""), "state_was": state_was,
                    "reason": str(reason or ""), "at": at}
        task["state"] = "failed"
        task.setdefault("history", []).append({"at": at, "from": state_was,
                                              "to": "failed", "note": str(reason)})
        task["failed_reason"] = str(reason)
        task["failed_at"] = at
        task["failed_evidence"] = evidence
        save(task_dir, task)
        return {"changed": True, "already": False, "state": "failed",
                "state_was": state_was, "reason": str(reason), "at": at,
                "evidence": evidence}

