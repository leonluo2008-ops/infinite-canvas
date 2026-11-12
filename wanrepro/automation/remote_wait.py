# -*- coding: utf-8 -*-
"""远程拆解等待语义的**唯一文案源**（「无法取消 / 只能等返回 / 预计还需多久」）。

为什么单独成模块：同一件事实有四处出口——桥端 409 自述、discuss 排队日志、hub 重试拒绝、
强制结束回执/详情页。各写一套必然漂移（一次现场四种说法），所以「事实」与「人话」分开：
  · 事实（拆解槽被谁占、已跑多久、当前阶段）由桥端 `GET /api/lapian/slot` 与 任务.json 提供；
  · 人话（怎么解释、预计还需多久、能不能取消）只在本模块生成，谁都不许另抄一份。

口径写死的三条产品语义（所有者 2026-11-12 裁定）：
  1. 已提交到远程（聚鑫）的拆解**无法取消**，只能等它返回——任何「终止」动作都不能谎称已经停掉远程；
  2. 要再提交同一条任务，只能等上一个结果返回、拆解槽释放之后；
  3. 已终止任务晚到的远程结果按作废丢弃（作废栅栏），不覆盖 failed 状态——这句也要给人看见。

预计剩余时长是**启发式**，不是远程进度：桥端只有 status/stage，没有百分比，编一个就是假进度。
这里只按「远程通常 5–15 分钟」的既有口径给中位数推算，并把口径写在句子里，让用户知道这是估计。
"""
from __future__ import annotations

# 远程拆解典型耗时（秒）：文案既有口径「远程通常 5–15 分钟」的中位，用于推算「预计还需」。
REMOTE_TYPICAL_SECONDS = 720
# 远程等待的硬上限（与 discuss.POLL_MAX_SECONDS / 桥端 analyze 子进程 1800s 同值）。
REMOTE_MAX_SECONDS = 1800
# 预计剩余的下限：过了典型时长也不说「马上好」（那是编），只说「还需约 1 分钟」。
MIN_ETA_SECONDS = 60
# 唯一口径句：所有「预计还需」后面都跟它，避免把估计说成进度。
TYPICAL_HINT = "远程通常 5–15 分钟，慢时更久"


# ---------------------------------------------------------------- 时长格式化

def format_seconds(seconds) -> str:
    """秒 → 人话时长（45 秒 / 3 分 12 秒 / 1 小时 05 分）。非数值 → 「时长未知」。"""
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return "时长未知"
    if total < 0:
        return "时长未知"
    if total < 60:
        return f"{total} 秒"
    if total < 3600:
        return f"{total // 60} 分 {total % 60:02d} 秒"
    return f"{total // 3600} 小时 {(total % 3600) // 60:02d} 分"


def format_eta(seconds) -> str:
    """预计剩余 → 人话（不足 1 分钟按 1 分钟；取整到分钟，不假装精确到秒）。"""
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return ""
    total = max(MIN_ETA_SECONDS, total)
    minutes = max(1, int((total + 59) // 60))
    return f"约 {minutes} 分钟"


def eta_seconds(elapsed) -> int | None:
    """按「已跑多久」推算预计还需（秒）。elapsed 非数值 → None（不编）。"""
    try:
        e = int(float(elapsed))
    except (TypeError, ValueError):
        return None
    if e < 0:
        return None
    remain = REMOTE_TYPICAL_SECONDS - e
    if remain > 0:
        return int(min(remain, max(MIN_ETA_SECONDS, REMOTE_MAX_SECONDS - e)))
    return int(MIN_ETA_SECONDS)          # 已过典型时长：给下限，不谎称「马上好」


# ---------------------------------------------------------------- 槽真相归一

def holder_of(slot: dict | None) -> dict:
    """把桥端 slot 自述归一成 {task_id, job, elapsed_seconds, stage, started_at}；缺的一律空。

    桥端可能只报「忙」而报不出持有者（acquire 了锁但 job 记录还没落、或记录被 TTL 清掉）——
    这种「不知道」必须原样保留为 holder={}，绝不用猜一个任务号填上。
    """
    holder = (slot or {}).get("holder")
    if not isinstance(holder, dict):
        return {}
    out = {
        "task_id": str(holder.get("task_id") or "").strip(),
        "job": str(holder.get("job") or "").strip(),
        "stage": str(holder.get("stage") or "").strip(),
        "started_at": str(holder.get("started_at") or "").strip(),
        "elapsed_seconds": None,
    }
    try:
        elapsed = holder.get("elapsed_seconds")
        out["elapsed_seconds"] = None if elapsed is None else int(float(elapsed))
    except (TypeError, ValueError):
        out["elapsed_seconds"] = None
    return out


def is_same_task(slot: dict | None, bridge_id: str, *, task_busy: str = "") -> bool:
    """占据拆解槽的是不是「本任务的上一轮提交」——只认逐字相等的任务号，不猜。"""
    target = str(bridge_id or "").strip()
    if not target:
        return False
    if str(task_busy or "").strip() == target:
        return True
    return holder_of(slot).get("task_id") == target


# ---------------------------------------------------------------- 排队中（discuss 侧）

def busy_line(slot: dict | None, *, bridge_id: str = "", waited=None,
              task_busy: str = "") -> str:
    """拆解槽忙时给人看的一句话：在等谁、等了多久、预计还需多久、为什么只能等。"""
    holder = holder_of(slot)
    elapsed = holder.get("elapsed_seconds")
    eta = eta_seconds(elapsed)
    wait_text = format_seconds(waited) if waited is not None else ""
    same = is_same_task(slot, bridge_id, task_busy=task_busy)
    if same:
        progress = f"已跑 {format_seconds(elapsed)}，预计还需{format_eta(eta)}" if elapsed is not None \
            else f"跑了多久暂时读不到（{TYPICAL_HINT}）"
        return ("上一轮远程拆解仍在跑（" + progress + "）——已提交到聚鑫的拆解无法取消，"
                "只能等它返回；返回后拆解槽自动释放，届时再点重试即可"
                "（那一轮结果会按作废丢弃，不会覆盖失败状态）")
    if holder:
        who = holder.get("task_id") or "另一个任务"
        progress = f"已跑 {format_seconds(elapsed)}，预计还需{format_eta(eta)}" if elapsed is not None \
            else f"跑了多久暂时读不到（{TYPICAL_HINT}）"
        waited_text = f"，本任务已排队 {wait_text}" if wait_text else ""
        return (f"拆解槽被 {who} 的远程拆解占用（{progress}{waited_text}）——"
                "同一时刻只允许一个拆解，等它返回后这里自动续上")
    waited_text = f"已等 {wait_text}，" if wait_text else ""
    return (f"拆解槽被占用，但暂时读不到是哪个任务在跑（{waited_text}{TYPICAL_HINT}）——"
            "等它释放后这里自动续上")


def busy_exhausted_reason(slot: dict | None, *, bridge_id: str = "", waited=None,
                          task_busy: str = "") -> str:
    """排队等到上限仍未拿到槽的**落盘原因**（写进 failed_reason，界面直接展示这一句）。"""
    holder = holder_of(slot)
    waited_text = format_seconds(waited) if waited is not None else "一段时间"
    if is_same_task(slot, bridge_id, task_busy=task_busy):
        head = "本次未提交：上一轮远程拆解还没返回"
    elif holder:
        head = f"本次未提交：拆解槽被 {holder.get('task_id') or '另一个任务'} 的远程拆解占着"
    else:
        head = "本次未提交：拆解槽一直被占着（读不到持有者）"
    elapsed = holder.get("elapsed_seconds")
    ran = f"，对方已跑 {format_seconds(elapsed)}" if elapsed is not None else ""
    return (f"{head}——已等 {waited_text}{ran}（{TYPICAL_HINT}）。"
            "已提交到远程的拆解无法取消；等它返回、拆解槽释放后再点重试即可，本任务没有被远程判失败")


# ---------------------------------------------------------------- 强制结束回执（hub 侧）

def force_finish_remote(slot: dict | None, *, bridge_id: str, submitted: bool,
                        bridge_error: str = "") -> dict:
    """强制结束回执里的「远程半边」：本地已终止 ≠ 远程已停。

    返回 {running, cancellable, submitted, elapsed_seconds, eta_seconds, task_id,
          checked_at, source, message}——message 是给人读的三分句：
    本地已终止 / 远程无法取消仍在跑（有预计） / 结果返回后按作废丢弃。
    """
    holder = holder_of(slot)
    busy = bool((slot or {}).get("busy"))
    same = bool(busy and is_same_task(slot, bridge_id))
    elapsed = holder.get("elapsed_seconds") if same else None
    eta = eta_seconds(elapsed) if same else None
    if bridge_error:
        source = "unknown"
        message = ("本地已终止（状态=失败，产物全留）；远程拆解状态未知（桥连不上："
                   f"{bridge_error}）——若远程仍在跑，它的结果返回后会按作废丢弃，"
                   "不会覆盖这次失败；要再提交同一条任务，请确认拆解槽空闲后再点重试")
    elif same:
        source = "bridge_live"
        progress = (f"已跑 {format_seconds(elapsed)}，预计还需{format_eta(eta)}"
                    if elapsed is not None else f"跑了多久暂时读不到（{TYPICAL_HINT}）")
        message = ("本地已终止（状态=失败，产物全留）；已提交到远程的拆解无法取消，仍在跑（"
                   + progress + "）；结果返回后会按作废丢弃，不会覆盖这次失败状态。"
                   "要再提交同一条任务，等它返回、拆解槽释放后再点重试")
    elif busy:
        source = "bridge_live"
        who = holder.get("task_id") or "另一个任务"
        message = ("本地已终止（状态=失败，产物全留）；拆解槽当前被 "
                   f"{who} 的远程拆解占用（不是本任务）——结果返回后槽自动释放，"
                   "要再提交这条任务，等槽空闲后再点重试")
    elif submitted:
        source = "bridge_live"
        message = ("本地已终止（状态=失败，产物全留）；桥端拆解槽已空闲——本任务上一次提交的远程拆解"
                   "如果已经返回，其结果会按作废丢弃，不会覆盖这次失败状态；现在可以点重试")
    else:
        source = "bridge_live"
        message = ("本地已终止（状态=失败，产物全留）；桥端拆解槽空闲，也没有本任务的远程拆解在跑"
                   "（这一轮可能还没提交到远程）")
    return {
        "submitted": bool(submitted or same),
        "running": bool(same),
        "cancellable": False,                 # 平台事实：聚鑫侧提交后无法取消
        "task_id": str(holder.get("task_id") or "") if same else str(bridge_id or ""),
        "job": str(holder.get("job") or "") if same else "",
        "elapsed_seconds": elapsed,
        "eta_seconds": eta,
        "source": source,
        "message": message,
    }


def remote_idle_message(*, submitted: bool) -> str:
    """作废结果的可见性：远程已不在跑时，界面该说的那一句（不谎称「已取消」）。"""
    if submitted:
        return ("远程拆解已不在跑（拆解槽已释放）——上一轮提交的结果如果已经返回，"
                "会按作废丢弃，不覆盖当前失败状态")
    return "远程拆解已不在跑（拆解槽已释放）"
