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
import time
from contextlib import contextmanager
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

# ---- AT-19 契约文件（方案 v2 §1.3）------------------------------------------------
EXEC_EXIT_FILE = "执行体退出码.json"      # 执行体收尾写；spawn 失败时父进程代写（outcome=spawn_failed）
CHILD_LOG_FILE = "拆解子进程日志.txt"     # 子进程 stdout/stderr 合流（追加式）
TASK_LOCK_FILE = ".任务.lock"             # 进程间文件锁载体（X1）
EXEC_GRACE_SECONDS = 60                   # 只保护「执行体还没接管」（E3）；E1/E2 不看宽限
EXEC_PID_REUSE_TOLERANCE = 2              # PID 复用判据：OS creation time 差 > 2s 视为复用
EXEC_RUN_ID_KEY = "exec_run_id"           # 任务.json 上的 run 令牌（X2 所有权栅栏）

_ARTIFACT_EXCLUDE = (_TASK_FILE, HEARTBEAT_FILE, "拆解日志.txt",          # §1.3：digest 不许自证自变
                     EXEC_EXIT_FILE, EXEC_EXIT_FILE + ".tmp",
                     HEARTBEAT_FILE + ".tmp", CHILD_LOG_FILE, TASK_LOCK_FILE)
# 活动证据六源：心跳 / 状态迁移 / 阶段锚 / 任务目录产物 / 拆解产物 / 起跑锚。
# 退出码文件是「死亡证据」不是活动；锁文件每次读写都改 mtime；子进程日志逐行增长——
# 三者都不许顶替 last_activity_at（否则死证被自己的残留文件盖掉）。
_ACTIVITY_EXCLUDE = (_TASK_FILE, HEARTBEAT_FILE, EXEC_EXIT_FILE, EXEC_EXIT_FILE + ".tmp",
                     HEARTBEAT_FILE + ".tmp", CHILD_LOG_FILE, TASK_LOCK_FILE)
_ANALYSIS_REVERSE = (Path(os.environ["WANREPRO_ANALYSIS_ROOT"]).expanduser()
                     if (os.environ.get("WANREPRO_ANALYSIS_ROOT") or "").strip()
                     else Path(__file__).resolve().parents[1] / "analysis" / "reverse")

_ORPHAN_LOCK = threading.Lock()    # 只保护 fail_orphan_task 的判定-写盘临界区；transition() 不变


def fake_run_reason(state_name: str) -> str:
    """该状态的失联文案（只有 discussing/rendering 有专属文案，其余给通用文案）。"""
    return FAKE_RUN_REASONS.get(str(state_name or ""), FAKE_RUN_REASON_FALLBACK)


# ---------------------------------------------------------------- 心跳侧车读写（锚）

def heartbeat_path(task_dir: Path) -> Path:
    return Path(task_dir) / HEARTBEAT_FILE


def _heartbeat_blank() -> dict:
    """心跳全空结构（缺文件 / 坏 JSON / 从未跑起 的唯一可识别形态）。

    AT-19 扩 4 键：exec（child|thread|""）/ pid / pid_created_at（OS creation time）/ exit_code。
    读侧是**逐键白名单拷贝**，新增字段不进这里就等于没写（审计 X8：静默失效）。
    """
    return {"schema": _HEARTBEAT_SCHEMA, "run_id": "", "started_at": "", "last_beat_at": "",
            "beat_count": 0, "phase": "", "artifact_count": 0, "artifact_digest": "",
            "ended_at": "", "outcome": "",
            "exec": "", "pid": 0, "pid_created_at": "", "exit_code": None}


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
                "ended_at", "outcome", "exec", "pid_created_at"):
        value = raw.get(key)
        out[key] = "" if value is None else str(value)
    for key in ("schema", "beat_count", "artifact_count", "pid"):
        try:
            out[key] = int(raw.get(key) or 0)
        except (TypeError, ValueError):
            out[key] = 0
    code = raw.get("exit_code")
    try:
        out["exit_code"] = None if code is None else int(code)
    except (TypeError, ValueError):
        out["exit_code"] = None
    return out


def _fence_ok(data: dict, expected_run_id: str) -> bool:
    """逐写栅栏（方案 v2 §1.4 第二道）：给了期望 run 就必须逐字节匹配。

    不匹配 = 本次 run 已被接管（force_finish + retry / 人工重试），本次写入一律放弃。
    """
    return not expected_run_id or str((data or {}).get("run_id") or "") == str(expected_run_id)



def _heartbeat_write(task_dir: Path, data: dict) -> dict:
    target = heartbeat_path(task_dir)
    tmp = target.with_name(HEARTBEAT_FILE + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(target)            # 原子替换：中途 kill 不留半截心跳
    return data


def heartbeat_begin(task_dir: Path, *, run_id: str, phase: str = "", exec: str = "") -> dict:
    """一次 run 起手：整体覆盖写心跳（旧 run_id 作废）。**必须在 Popen 之前调用**。

    这样「执行体根本没起来」（启动失败 / 进程在起手后崩 / 桥连不上就退出）
    也有一份带 started_at 的心跳，判定窗口从发起时刻起算，而不是等执行体内部第一次写。
    last_beat_at 保持空串 = 本次 run 从未收到心跳（初值可被识别，不许伪造一次心跳）。
    exec（AT-19）：child=独立子进程，thread=旧进程内线程，""=旧任务/未知。
    """
    task_dir = Path(task_dir)
    data = _heartbeat_blank()
    data["run_id"] = str(run_id or "")
    data["started_at"] = _now()
    data["phase"] = str(phase or "")
    data["exec"] = str(exec or "")
    data["artifact_count"], data["artifact_digest"] = _artifact_fingerprint(task_dir)
    return _heartbeat_write(task_dir, data)


def heartbeat_attach(task_dir: Path, *, run_id: str, pid: int,
                     pid_created_at: str = "") -> dict:
    """父进程在 Popen 成功后写 pid + OS creation time（T0+，父的最后一笔心跳写入）。

    单写者合同：父只写 T0（begin）与 T0+（attach），此后 beat/finish 全归子进程。
    """
    task_dir = Path(task_dir)
    data = heartbeat_read(task_dir)
    if not _fence_ok(data, run_id):
        return {}                      # 已被接管：不写
    data["schema"] = _HEARTBEAT_SCHEMA
    data["pid"] = int(pid or 0)
    data["pid_created_at"] = str(pid_created_at or "")
    data["exec"] = "child"
    return _heartbeat_write(task_dir, data)


def heartbeat_beat(task_dir: Path, *, phase: str | None = None,
                   expected_run_id: str = "", pid: int | None = None) -> dict:
    """一次心跳：只写心跳文件的 last_beat_at/beat_count/artifact_*（+ phase 变化时同步）。

    **绝不碰 任务.json**（AT-16 B4b 合同：等待期心跳借道阶段回调会把阶段计时清零）。
    expected_run_id（AT-19 X2）：给了就逐写校验；不匹配 → 放弃写入并返回 {}（不抛，
    心跳失败不拖垮拆解）。
    """
    task_dir = Path(task_dir)
    data = heartbeat_read(task_dir)
    if not _fence_ok(data, expected_run_id):
        return {}
    data["schema"] = _HEARTBEAT_SCHEMA
    if not data["started_at"]:
        data["started_at"] = _now()          # 没有起手写（旧任务/异常路径）也不许伪造历史
    data["last_beat_at"] = _now()
    data["beat_count"] = int(data.get("beat_count") or 0) + 1
    if pid is not None:
        data["pid"] = int(pid or 0)
    if phase is not None and str(phase) != "":
        data["phase"] = str(phase)
    data["artifact_count"], data["artifact_digest"] = _artifact_fingerprint(task_dir)
    return _heartbeat_write(task_dir, data)


def heartbeat_finish(task_dir: Path, *, outcome: str, expected_run_id: str = "",
                     exit_code: int | None = None) -> dict:
    """一次 run 收尾：写 ended_at + outcome + exit_code（诊断位，不参与判定）。

    expected_run_id 不匹配 → 放弃（旧执行体不许给新 run 的心跳盖终态）。
    """
    data = heartbeat_read(task_dir)
    if not _fence_ok(data, expected_run_id):
        return {}
    data["ended_at"] = _now()
    data["outcome"] = str(outcome or "")
    if exit_code is not None:
        data["exit_code"] = int(exit_code)
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
                     trigger: str = "auto_scan", run_id: str = "",
                     exec_view: dict | None = None) -> dict:
    """运行态 → failed 的唯一受控入口（方案 §3.3；T1/T2 两条显式边 + AT-19 exec_dead 边）。

    返回 {"changed": bool, "state": "failed", "state_was": str, "reason": str, "at": iso}
    · 已是 failed → changed=False / already=True，**不写 history、不刷新 failed_at**（幂等）；
    · 状态不在运行态 → StateError("任务当前不是运行态（<state>）")；
    · trigger="auto_scan"：写盘前**重新过一遍守卫**（G1 运行态 / G2 静默 ≥900s /
      G3 无在途证据 / G4 调用方传入的 held_job），未过即拒降级——守卫不只在扫描器里；
    · trigger="exec_dead"（AT-19）：写盘前重过 `judge_exec_dead`（E1/E2/E2b/E3 死证），
      未握死证即拒降级——这是「执行体不存在→立即降级」的入口，与 900s 静默判定分离；
    · 只写 5 个键：state / history(追加一条) / failed_reason / failed_at / failed_evidence；
    · **绝不**：删文件、清 pending_replace、清 approval、动产物、动 lapian_*。
    """
    task_dir = Path(task_dir)
    reason_code = ""
    with _ORPHAN_LOCK:
        task = load(task_dir)
        state_was = str(task["state"])
        if state_was == "failed":
            return {"changed": False, "already": True, "state": "failed",
                    "state_was": "failed",
                    "reason": str(task.get("failed_reason") or ""),
                    "at": str(task.get("failed_at") or "")}
        if state_was not in RUNNING_STATES_MACHINE:
            raise StateError(f"任务当前不是运行态（{state_was}）")
        reason = str(reason or "").strip() or fake_run_reason(state_was)
        probe = dict(probe) if isinstance(probe, dict) else {}
        if trigger == "auto_scan":
            snapshot = probe or activity_snapshot(task_dir, task)
            lost, code = judge_lost_run(task, snapshot,
                                        held_job=bool(snapshot.get("held_job")))
            if not lost:
                raise StateError(f"未达假运行判定（{code or 'unknown'}），拒绝降级")
            probe = snapshot
        elif trigger == "exec_dead":
            view = exec_view if isinstance(exec_view, dict) else exec_snapshot(task_dir, task)
            dead, code = judge_exec_dead(task, view)
            if not dead:
                raise StateError(f"未达执行体死证（{code or 'unknown'}），拒绝降级")
            reason_code = str(code or "")
            probe = {**probe, "exec": view}
        return _write_failed(task_dir, reason=reason, probe=probe, trigger=trigger,
                             run_id=run_id, expected_from=state_was,
                             reason_code=reason_code)


# ============================================================================
# AT-19：执行体进程化 —— 进度与死证层（退出码回写 / PID 判死 / 启动即对账的证据面）
#
# 方案 v2（kanban 附件 方案v2-AT-19-…-20261112.md）§1.3 / §2 / §4.3。本段只做三件事：
#   · 证据：`执行体退出码.json`（子进程收尾唯一写点，原子替换）+ PID 存活/creation time
#            （Windows GetProcessTimes / POSIX /proc/<pid>/stat 第 22 字段）——死证只认磁盘。
#   · 判定：`judge_exec_dead(task, exec_view)` 纯函数，E1/E2/E2b/E3 四条死证，900s 静默
#            判定（judge_lost_run）**零改动**、仍作兜底。
#   · 写盘：`_write_failed` 是 discuss 链 failed 5 键的**唯一构造点**，进程间文件锁 +
#            run_id 栅栏 + 起态准入；`fail_orphan_task` 只是它前面的守卫壳。
#
# 冻结值一个不动：900/60/300/900/30、FAKE_RUN_REASONS、墓碑合同、generating 不自动降级。
# ============================================================================

_LOCK_RETRY_SECONDS = 0.05
_LOCK_TIMEOUT_SECONDS = 5.0


@contextmanager
def _file_lock(task_dir: Path):
    """任务级**进程间**互斥（X1）：`<任务根>/.locks/<任务号>.lock` 上一个字节的独占锁。

    为什么必须跨进程：AT-19 把执行体搬出 server 进程后，「子进程整份 save」与「server
    对账写 failed」是真并发。进程内 threading.Lock 挡不住，`_write_task` 的「写盘前重读」
    也只能把窗口缩到 load→save 的毫秒级。锁只在 read-modify-write 区间持有（毫秒级），
    不做长事务、不覆盖 transition()。

    锁文件放在**任务目录之外**（同级 .locks/）：任务目录要被「只写 5 键、文件集合不变」的
    冻结合同与产物指纹/活动六源检视，锁文件在里面会同时污染这三处。
    """
    task_dir = Path(task_dir)
    lock_dir = task_dir.parent / ".locks"
    lock_path = lock_dir / f"{task_dir.name}.lock"
    fd = None
    deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
    try:
        try:
            lock_dir.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
        except OSError:
            yield                          # 锁文件都建不了（目录被删/无权限）：退化为无锁，绝不阻塞调用方
            return
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise StateError(f"任务文件锁等待超时（{_LOCK_TIMEOUT_SECONDS}s）：{lock_path}")
                time.sleep(_LOCK_RETRY_SECONDS)
        yield
    finally:
        if fd is not None:
            try:
                if os.name == "nt":
                    import msvcrt
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass


# ---------------------------------------------------------------- 退出码契约文件

def exec_path(task_dir: Path) -> Path:
    return Path(task_dir) / EXEC_EXIT_FILE


def _exec_blank() -> dict:
    return {"schema": 1, "run_id": "", "pid": 0, "exit_code": None, "outcome": "",
            "error_tail": "", "reason_source": "", "at": "", "exists": False}


def exec_read(task_dir: Path) -> dict:
    """读 执行体退出码.json（纯读，不抛）。exists=False = 本次 run 没写退出码（被强杀/还在跑）。"""
    out = _exec_blank()
    try:
        raw = json.loads(exec_path(task_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return out
    if not isinstance(raw, dict):
        return out
    out["exists"] = True
    for key in ("run_id", "outcome", "error_tail", "reason_source", "at"):
        value = raw.get(key)
        out[key] = "" if value is None else str(value)
    for key in ("pid",):
        try:
            out[key] = int(raw.get(key) or 0)
        except (TypeError, ValueError):
            out[key] = 0
    code = raw.get("exit_code")
    try:
        out["exit_code"] = None if code is None else int(code)
    except (TypeError, ValueError):
        out["exit_code"] = None
    return out


def exec_write(task_dir: Path, *, run_id: str, pid: int = 0, exit_code: int | None = None,
               outcome: str = "", error_tail: str = "", reason_source: str = "") -> dict:
    """执行体退出前**一次**原子写（tmp + replace）。

    栅栏：已有退出码且 run_id 不是本次 run → 不覆盖（旧执行体不许盖新 run 的死证）。
    唯一由父代写的例外：spawn 失败（outcome="spawn_failed"，父用同一 run_id 写）。
    """
    task_dir = Path(task_dir)
    current = exec_read(task_dir)
    if current.get("exists") and str(current.get("run_id") or "") not in ("", str(run_id)):
        return current
    payload = {"schema": 1, "run_id": str(run_id or ""), "pid": int(pid or 0),
               "exit_code": None if exit_code is None else int(exit_code),
               "outcome": str(outcome or ""),
               "error_tail": str(error_tail or "")[:2000],
               "reason_source": str(reason_source or ""), "at": _now()}
    target = exec_path(task_dir)
    tmp = target.with_name(EXEC_EXIT_FILE + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(target)
    payload["exists"] = True
    return payload


def stamp_exec_run(task_dir: Path, run_id: str) -> dict:
    """把本次 run 令牌写进 任务.json（X2 所有权栅栏的锚）。持进程间锁，读-改-写。"""
    task_dir = Path(task_dir)
    with _file_lock(task_dir):
        task = load(task_dir)
        task[EXEC_RUN_ID_KEY] = str(run_id or "")
        save(task_dir, task)
        return task


# ---------------------------------------------------------------- PID 探测（活/creation time）

def _win_open(pid: int):
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    # QUERY_LIMITED_INFORMATION(0x1000) | SYNCHRONIZE(0x00100000)
    return k32, k32.OpenProcess(0x1000 | 0x00100000, False, int(pid))


def pid_alive(pid: int) -> bool | None:
    """PID 是否活着。None = 探测失败（权限/平台不支持）——判定层按保守处理，不许当死证。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        try:
            k32, handle = _win_open(pid)
        except OSError:
            return None
        if not handle:
            err = ctypes.get_last_error()
            return None if err == 5 else False      # 5=拒绝访问 → 存在但探不动
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return None
            return code.value == 259                # STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)                              # POSIX：信号 0 = 只探存在
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def pid_created_at(pid: int) -> str:
    """进程 OS creation time（ISO，毫秒精度）。取不到 → ""（判定层退化为 exec_unknown）。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return ""
    if pid <= 0:
        return ""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        try:
            k32, handle = _win_open(pid)
        except OSError:
            return ""
        if not handle:
            return ""
        try:
            created = wintypes.FILETIME()
            exited = wintypes.FILETIME()
            kernel = wintypes.FILETIME()
            user = wintypes.FILETIME()
            if not k32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                       ctypes.byref(kernel), ctypes.byref(user)):
                return ""
            ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
            epoch = ticks / 10_000_000 - 11644473600      # FILETIME(1601) → Unix epoch
            return (datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone()
                    .isoformat(timespec="milliseconds"))
        finally:
            k32.CloseHandle(handle)
    # POSIX：/proc/<pid>/stat 第 22 字段 starttime（clock tick）→ 秒
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        fields = stat[stat.rfind(")") + 2:].split()
        start_ticks = int(fields[19])
        btime = 0
        for line in Path("/proc/stat").read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("btime "):
                btime = int(line.split()[1])
                break
        if not btime:
            return ""
        epoch = btime + start_ticks / os.sysconf("SC_CLK_TCK")
        return (datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone()
                .isoformat(timespec="milliseconds"))
    except (OSError, ValueError, IndexError):
        return ""


# ---------------------------------------------------------------- 执行体快照 + 死证判定

def exec_snapshot(task_dir: Path, task: dict | None = None) -> dict:
    """执行体证据快照（纯读，不写盘）：心跳 4 新键 + 退出码文件 + PID 实时探活。

    判定只吃这份快照——**不读 HUB_JOBS**（server 重启后内存态为空，进程化后内存态
    更是另一个进程的，永远不可信）。
    """
    task_dir = Path(task_dir)
    task = _task_or_load(task_dir, task)
    hb = heartbeat_read(task_dir)
    ex = exec_read(task_dir)
    pid = int(hb.get("pid") or 0)
    alive = pid_alive(pid) if pid > 0 else None
    actual_created = pid_created_at(pid) if (pid > 0 and alive) else ""
    return {
        "heartbeat_run_id": str(hb.get("run_id") or ""),
        "heartbeat_at": str(hb.get("last_beat_at") or ""),
        "started_at": str(hb.get("started_at") or ""),
        "beat_count": int(hb.get("beat_count") or 0),
        "exec": str(hb.get("exec") or ""),
        "pid": pid,
        "pid_created_at": str(hb.get("pid_created_at") or ""),
        "pid_alive": alive,
        "pid_actual_created_at": actual_created,
        "exit_exists": bool(ex.get("exists")),
        "exit_run_id": str(ex.get("run_id") or ""),
        "exit_code": ex.get("exit_code"),
        "exit_outcome": str(ex.get("outcome") or ""),
        "exit_error_tail": str(ex.get("error_tail") or ""),
        "exit_at": str(ex.get("at") or ""),
        "task_exec_run_id": str((task or {}).get(EXEC_RUN_ID_KEY) or ""),
        "scanned_at": _now(),
    }


def judge_exec_dead(task: dict, view: dict, *, now=None) -> tuple[bool, str]:
    """执行体死证判定（纯函数）。返回 (是否已死, 原因码)。

    判定顺序（方案 v2 §2，冻结）：
      1 not_running            状态不在运行态
      2 manual_only_state      generating 永不自动降级（冻结）
      3 exec_no_evidence       心跳缺失 / exec 非 child（旧任务）→ 退回 900s 静默判定
      4 exec_stale_run         心跳 run ≠ 任务 exec_run_id（旧执行体，已被接管）
      5 exec_exit_written      E1：退出码回写且非 0（outcome=env_error 除外，环境级留 discussing）
      6 exec_pid_gone          E2：pid>0 且已死
      7 exec_pid_reused        E2b：pid 活着但 OS creation time 与登记的差 > 2s（PID 复用）
      8 exec_grace / exec_never_attached   E3：child 但从未接管；60s 宽限**只在这里**
      9/10/11 exec_unknown / exec_alive   其余保守（退回 900s）

    宽限口径（X3）：E1/E2/E2b 不看 60s——执行体已握死证就立即降级。
    心跳超时**不作**独立死证（Q1 默认裁定）：心跳只让扫描走到 E2/E3。
    """
    task = task or {}
    view = view or {}
    state_name = str(task.get("state") or "")
    if state_name not in RUNNING_STATES_MACHINE:
        return False, "not_running"
    if state_name == "generating":
        return False, "manual_only_state"
    run_id = str(view.get("heartbeat_run_id") or "")
    if not run_id or str(view.get("exec") or "") != "child":
        return False, "exec_no_evidence"
    owner = str(view.get("task_exec_run_id") or "")
    if owner and owner != run_id:
        return False, "exec_stale_run"
    exit_code = view.get("exit_code")
    exit_run = str(view.get("exit_run_id") or "")
    outcome = str(view.get("exit_outcome") or "")
    if view.get("exit_exists") and exit_run == run_id and isinstance(exit_code, int):
        if outcome == "env_error":
            return False, "exec_env_error"        # 环境级故障：状态留 discussing，重跑即续
        if exit_code != 0:
            return True, "exec_exit_written"      # E1：正常退出但非 0（或 spawn 失败）
    pid = int(view.get("pid") or 0)
    alive = view.get("pid_alive")
    if pid > 0 and alive is False:
        return True, "exec_pid_gone"              # E2
    if pid > 0 and alive is True:
        registered = _parse_iso(view.get("pid_created_at"))
        actual = _parse_iso(view.get("pid_actual_created_at"))
        if registered and actual:
            if abs((actual - registered).total_seconds()) > EXEC_PID_REUSE_TOLERANCE:
                return True, "exec_pid_reused"    # E2b
        return False, "exec_alive"                # 在跑但不动 → 900s
    if pid == 0 and int(view.get("beat_count") or 0) == 0:
        started = _parse_iso(view.get("started_at"))
        now_dt = _parse_iso(now) or datetime.now(timezone.utc).astimezone()
        if started is not None and (now_dt - started).total_seconds() < EXEC_GRACE_SECONDS:
            return False, "exec_grace"            # E3 宽限：只保护「还没来得及接管」
        return True, "exec_never_attached"        # E3
    if pid == 0 and int(view.get("beat_count") or 0) > 0:
        return False, "exec_unknown"              # 自相矛盾：不动手，退回 900s
    return False, "exec_unknown"                  # 探测失败：保守


# ---------------------------------------------------------------- 唯一写盘核

def _write_failed(task_dir: Path, *, reason: str, probe: dict, trigger: str,
                  run_id: str = "", expected_from: str = "",
                  reason_code: str = "") -> dict:
    """discuss 链 failed 5 键的**唯一构造点**（X1 锁 + X2 栅栏 + X4 准入，全在这里收口）。

    · 进程间文件锁包住 load→判定→save；
    · 起态准入：`expected_from` 已给且重读后变了 → changed=False（并发推进不误杀）；
    · run 栅栏：给了 run_id 且任务 exec_run_id 不匹配 → 放弃（旧执行体不许写盘）；
    · 已 failed：只允许「兜底句 → 原文」单向升级（不写 history、不刷 failed_at）。
    """
    task_dir = Path(task_dir)
    at = _now()
    with _file_lock(task_dir):
        task = load(task_dir)
        cur = str(task.get("state") or "")
        if cur == "failed":
            old = str(task.get("failed_reason") or "")
            if old == str(reason or ""):
                return {"changed": False, "already": True, "state": "failed",
                        "state_was": "failed", "reason": old, "at": str(task.get("failed_at") or "")}
            if _is_fallback_text(old) and not _is_fallback_text(reason):
                evidence = task.get("failed_evidence")
                evidence = dict(evidence) if isinstance(evidence, dict) else {}
                evidence["reason_upgraded_from"] = old
                evidence["reason_source"] = str((probe or {}).get("reason_source") or "")
                for extra_key in ("exit_code", "cloud_error", "cloud_raw"):
                    if extra_key in (probe or {}):
                        evidence[extra_key] = probe[extra_key]
                task["failed_reason"] = str(reason)
                task["failed_evidence"] = evidence
                save(task_dir, task)
                return {"changed": True, "upgraded": True, "state": "failed",
                        "state_was": "failed", "reason": str(reason), "was": old, "at": at}
            return {"changed": False, "already": True, "state": "failed",
                    "state_was": "failed", "reason": old,
                    "at": str(task.get("failed_at") or "")}
        if expected_from and cur != expected_from:
            return {"changed": False, "already": False, "state": cur,
                    "state_was": expected_from, "reason": str(reason), "at": at}
        if cur not in RUNNING_STATES_MACHINE:
            raise StateError(f"起态 {cur} 不在允许集合，拒绝改判 failed")
        if run_id and str(task.get(EXEC_RUN_ID_KEY) or "") != str(run_id):
            raise StateError(f"run 已作废（{run_id}），拒绝写盘")
        probe = probe or {}
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
        if reason_code:
            evidence["reason_code"] = str(reason_code)
        for extra_key in ("exit_code", "fallback_text", "reason_source",
                          # 云端原文（extract.py 落盘的 拆解错误.json）：冻结 schema 原样透传，
                          # 前端直接取 cloud_error.message 渲染，不需要解析 failed_reason 文案；
                          # cloud_raw=on_disk/missing 区分「有原文」与「未落盘」，见 discuss._failure_detail。
                          "cloud_error", "cloud_raw"):
            if extra_key in probe:
                evidence[extra_key] = probe[extra_key]
        view = probe.get("exec")
        if isinstance(view, dict):
            # 可判定证据（卡面要求）：PID / 心跳 / 退出码三件一并落进 failed_evidence
            evidence["exec"] = {
                "pid": int(view.get("pid") or 0),
                "pid_alive": view.get("pid_alive"),
                "pid_created_at": str(view.get("pid_created_at") or ""),
                "beat_count": int(view.get("beat_count") or 0),
                "heartbeat_at": str(view.get("heartbeat_at") or ""),
                "exit_code": view.get("exit_code"),
                "exit_outcome": str(view.get("exit_outcome") or ""),
                "exit_at": str(view.get("exit_at") or ""),
            }
        task["state"] = "failed"
        task.setdefault("history", []).append({"at": at, "from": cur,
                                              "to": "failed", "note": str(reason)})
        task["failed_reason"] = str(reason)
        task["failed_at"] = at
        task["failed_evidence"] = evidence
        save(task_dir, task)
        return {"changed": True, "already": False, "state": "failed", "state_was": cur,
                "reason": str(reason), "at": at, "evidence": evidence}


def _is_fallback_text(text: str) -> bool:
    """是否 AT-18 冻结兜底文案（与 discuss._is_fallback_reason 同一口径，唯一来源在此）。"""
    t = str(text or "").strip()
    return bool(t) and (t == FAKE_RUN_REASON_FALLBACK or t in set(FAKE_RUN_REASONS.values()))


def write_failed(task_dir: Path, *, reason: str, source: str = "discuss",
                 exit_code: int | None = None, trigger: str = "",
                 run_id: str = "", probe: dict | None = None) -> dict:
    """discuss 执行体报告任务级失败时的公共入口（`_write_failed` 的薄包装）。

    与 `fail_orphan_task` 共用同一个写盘核：这就是「同一次失败只允许一次状态迁移」的实现
    形态——谁先拿到文件锁谁写，后来者只会走「已 failed」分支（幂等或兜底句→原文升级）。
    """
    probe = dict(probe or {})
    probe.setdefault("reason_source", str(source or ""))
    if exit_code is not None:
        probe.setdefault("exit_code", exit_code)
    return _write_failed(task_dir, reason=reason, probe=probe,
                         trigger=str(trigger or source or "discuss"), run_id=run_id)

