# -*- coding: utf-8 -*-
"""AT-3 拆解自动执行器——ingested → discussing → awaiting_assets。

桥合同（server.py 实读 2026-11-08）：
  POST /api/lapian/run {id, intent} → {job}；busy=HTTP 409（排队语义，不是失败）
  GET  /api/job/<hex> → {status: running|done|error}；done 带 result；
       error 带 error 原文全量；404=服务重启过（JOBS 内存态丢失）

审计修正（路2 B4）：产物优先——执行前先按 bridge_id 查 has_* 三标志，
全真则判「已完成」直接迁移，不重跑（kill/重启后盲重发会新建任务的坑在此消解）。
"""
import argparse
import json
import sys
import time
from pathlib import Path

from . import bridge, config, state

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

POLL_INTERVAL = 3        # 秒
POLL_MAX_SECONDS = 1800  # 与桥端 analyze 子进程上限一致：长片大拆解实测可超 10 分钟
BUSY_RETRY_MAX = 60      # 409 排队：最多等 60 次 × 5s = 5 分钟
BUSY_RETRY_INTERVAL = 5


class DiscussError(RuntimeError):
    pass


def _products_ready(bridge_id: str) -> bool:
    p = state.probe_bridge_products(bridge_id)
    return bool(p and p["has_disc"] and p["has_check"] and p["has_prompt"])


def start_and_wait(task: dict, on_stage=None) -> str:
    """POST lapian/run（409 排队重试）→ 轮询 job 至 done/error。返回 job id。

    on_stage(msg)：阶段回调（排队/提交/每次 stage 变化）——hub 工作台用它落
    拆解日志.txt 并回写任务.json 的 lapian_stage；CLI 模式不传。
    """
    def _emit(msg: str) -> None:
        if on_stage:
            try:
                on_stage(msg)
            except Exception:  # noqa: BLE001 —— 进度回调失败不拖垮拆解主链
                pass

    for attempt in range(BUSY_RETRY_MAX):
        try:
            out = bridge.post_json("/api/lapian/run",
                                   {"id": task["bridge_id"], "intent": task.get("intent", "")})
            job = out.get("job")
            if not job:
                raise DiscussError(f"lapian/run 未回 job: {json.dumps(out, ensure_ascii=False)[:200]}")
            break
        except bridge.BridgeError as e:
            if "HTTP 409" in str(e):
                _emit(f"拆解槽忙，排队重试（{attempt + 1}/{BUSY_RETRY_MAX}，{BUSY_RETRY_INTERVAL}s 后）")
                print(f"  [排队] 桥忙（{BUSY_RETRY_INTERVAL}s 后重试 {attempt + 1}/{BUSY_RETRY_MAX}）")
                time.sleep(BUSY_RETRY_INTERVAL)
                continue
            raise
    else:
        raise DiscussError(f"桥持续忙 {BUSY_RETRY_MAX} 次，放弃（任务未失败，重跑本命令续）")
    _emit(f"已提交拆解（job={job}），通常 1–3 分钟，远程模型慢时会更久")

    waited = 0
    last_stage = ""
    while waited < POLL_MAX_SECONDS:
        try:
            rec = bridge.get(f"/api/job/{job}")
        except bridge.BridgeError as e:
            if "HTTP 404" in str(e):
                # 服务重启过（JOBS 丢）——产物优先：产物在就判完成
                if _products_ready(task["bridge_id"]):
                    print("  [恢复] job 404 但产物齐——判已完成")
                    return job
                raise DiscussError(f"job 丢失（桥重启）且产物未齐: {e}") from None
            raise
        st = rec.get("status")
        if st == "done":
            _emit(f"拆解完成（耗时 {rec.get('elapsed', '?')}s）")
            return job
        if st == "error":
            _emit(f"失败：{rec.get('error')}")
            raise DiscussError(f"桥侧拆解失败（原文）: {rec.get('error')}")
        stage = str(rec.get("stage") or "")
        if stage and stage != last_stage:
            last_stage = stage
            _emit(f"{stage}（已 {rec.get('elapsed', waited)}s）")
        time.sleep(POLL_INTERVAL)
        waited += POLL_INTERVAL
    raise DiscussError(f"拆解超 {POLL_MAX_SECONDS}s 未完成（job={job}）——不判失败，可重跑续查")


def run_discuss(task_dir: Path, *, verbose: bool = True, on_stage=None) -> str:
    """执行拆解到 awaiting_assets。返回终态。

    on_stage(msg)：阶段回调透传给 start_and_wait（hub 工作台落日志/回写状态）。

    失败语义分级（审计 WARN「busy=排队不记 failed」同类）：
      · 任务级失败（桥报 error / done 但产物缺 / job 丢失且产物未齐）
        → failed 终态，history 记原文；
      · 环境级故障（桥连不上 / 超时放弃）→ 状态不迁移（留 ingested/discussing），
        重跑本命令即续——环境恢复不该把任务判死。
    """
    task = state.load(task_dir)
    cur = task["state"]
    if cur == "ingested":
        pass
    elif cur == "failed":
        # 人工重跑（CLI 重跑/工作台重试按钮都是人工动作）：failed 不判死，重跑即续
        state.transition(task_dir, "discussing", note="人工重试拆解")
        if verbose:
            print(f"[重试] {task['local_id']}: failed → discussing（人工重试）")
    elif cur == "discussing":
        if verbose:
            print(f"[续跑] {task['local_id']}: 上次拆解中断（discussing），重查产物后续跑")
    elif cur == "awaiting_assets" and _products_ready(task["bridge_id"]):
        if verbose:
            print(f"[SKIP] {task['local_id']}: 拆解产物已齐（产物优先，不重跑）")
        return "awaiting_assets"
    else:
        raise DiscussError(f"状态 {cur} 不可启动拆解（应 ingested/discussing 或已完成）")

    # 产物优先：kill/重启恢复时可能桥里已完成但本地状态还停在 ingested/discussing
    if _products_ready(task["bridge_id"]):
        if cur == "ingested":
            state.transition(task_dir, "discussing", note="产物已齐，直接迁移")
        state.transition(task_dir, "awaiting_assets", note="桥侧产物优先判完成")
        if verbose:
            print(f"[SKIP] {task['local_id']}: 桥侧产物已齐（恢复路径）")
        return "awaiting_assets"

    if cur == "ingested":
        state.transition(task_dir, "discussing", note="拆解启动")
    try:
        job = start_and_wait(task, on_stage=on_stage)
        # done 后核产物（失败即失败：桥说 done 但产物缺 → 如实报）
        if not _products_ready(task["bridge_id"]):
            raise DiscussError("桥报 done 但 has_disc/has_check/has_prompt 不全——如实记失败")
        state.transition(task_dir, "awaiting_assets",
                         note=f"拆解完成 job={job}", extra={"last_job": job})
        if verbose:
            print(f"[OK] {task['local_id']}: 拆解完成 → awaiting_assets（job={job}）")
        return "awaiting_assets"
    except bridge.BridgeError as e:
        # 环境级：状态留在 discussing，重跑即续（不判 failed）
        if verbose:
            print(f"[环境故障] {task['local_id']}: {e} —— 状态保留 discussing，重跑续")
        raise
    except DiscussError as e:
        state.transition(task_dir, "failed", note=str(e))
        raise


def _resolve(local: str) -> Path:
    d = config.PROJECTS_DIR / local
    if not (d / "任务.json").is_file():
        cand = list(config.PROJECTS_DIR.glob(f"auto-*"))
        raise SystemExit(f"找不到任务 {local}（projects 根：{config.PROJECTS_DIR}；现有: "
                         f"{[p.name for p in cand] if cand else '无'}）")
    return d


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="AT-3 拆解自动执行器")
    ap.add_argument("local_id", help="任务号（auto-0001 形态）")
    ap.add_argument("--bridge", help="bridge 基地址")
    args = ap.parse_args(argv)
    if args.bridge:
        config.BRIDGE_BASE = args.bridge.rstrip("/")
    config.ensure_dirs()
    try:
        run_discuss(_resolve(args.local_id))
        return 0
    except (DiscussError, bridge.BridgeError, state.StateError) as e:
        print(f"[FAIL] {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
