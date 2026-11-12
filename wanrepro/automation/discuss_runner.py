# -*- coding: utf-8 -*-
"""AT-19 拆解执行体入口——**独立子进程**跑 discuss.run_discuss（父进程不持有执行体）。

为什么要有这个模块：AT-18 之前执行体是 server 进程内的 daemon 线程，server 重启/停服
执行体随进程消失，状态机只剩 900s 静默一条兜底（实测滞后 1420s）。AT-19 把它拆成独立
子进程，server 只负责派发与读取；子进程自己写心跳、自己写退出码，重启后的启动对账只认
磁盘证据（心跳/PID/退出码）。

调用形态（由 hub_api._spawn_process 拉起，不是给人用的 CLI）：
    python -m automation.discuss_runner <local_id> --run-id <run_id>

退出码（写进 执行体退出码.json 的 exit_code 同此值）：
    0  拆解完成（→ awaiting_assets）
    3  任务级失败（discuss 已把 failed 写进 任务.json；本进程只回退出码）
    20 环境级故障（桥连不上/超时）——状态保留 discussing，重跑续（不当死证）
    9  本次 run 已被接管（心跳 run 或 任务.json.exec_run_id 不是本 run）→ 不写任何状态
    2  隔离守卫失败（任务目录不在 config.PROJECTS_DIR 下 / 任务不存在）
"""
from __future__ import annotations

import argparse
import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]          # wanrepro/
for _p in (str(ROOT), str(ROOT / "ui")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from automation import config, discuss, state  # noqa: E402

EXIT_OK = 0
EXIT_TASK_FAILED = 3
EXIT_ENV = 20
EXIT_SUPERSEDED = 9
EXIT_ISOLATION = 2


def _emit_exit(task_dir: Path, run_id: str, code: int, outcome: str,
               tail: str = "", source: str = "") -> None:
    """收尾唯一两写：退出码文件（死证）+ 心跳终态（诊断）。都带 run_id 栅栏。"""
    try:
        state.exec_write(task_dir, run_id=run_id, pid=os.getpid(), exit_code=code,
                         outcome=outcome, error_tail=tail, reason_source=source)
    except OSError as exc:
        print(f"[runner] 退出码文件写失败：{exc}", file=sys.stderr)
    try:
        state.heartbeat_finish(task_dir, outcome=outcome, expected_run_id=run_id,
                               exit_code=code)
    except OSError:
        pass


def run(local_id: str, run_id: str) -> int:
    task_dir = Path(config.PROJECTS_DIR) / local_id
    # 隔离守卫（R9）：任务目录必须正好在 config.PROJECTS_DIR 下——防 WANREPRO_AUTO_DIR
    # 配错时子进程去写真实任务目录。
    try:
        if task_dir.parent.resolve() != Path(config.PROJECTS_DIR).resolve():
            print(f"[runner] 隔离守卫拒绝：{task_dir}", file=sys.stderr)
            return EXIT_ISOLATION
    except OSError:
        return EXIT_ISOLATION
    if not (task_dir / "任务.json").is_file():
        print(f"[runner] 任务不存在：{task_dir}", file=sys.stderr)
        return EXIT_ISOLATION

    # 令牌第一道（X2）：心跳里的 run 不是本 run → 已被接管，不写任何状态直接退出。
    heartbeat = state.heartbeat_read(task_dir)
    if str(heartbeat.get("run_id") or "") != str(run_id):
        print(f"[runner] run 已被接管（心跳 {heartbeat.get('run_id')!r} ≠ {run_id!r}）",
              file=sys.stderr)
        return EXIT_SUPERSEDED
    try:
        task = state.load(task_dir)
    except state.StateError as exc:
        print(f"[runner] 任务文件坏了：{exc}", file=sys.stderr)
        return EXIT_ISOLATION
    owner = str(task.get(state.EXEC_RUN_ID_KEY) or "")
    if owner and owner != str(run_id):
        print(f"[runner] 任务已被新 run 接管（{owner} ≠ {run_id}）", file=sys.stderr)
        return EXIT_SUPERSEDED

    import hub_api                                   # 回调工厂的唯一实现（零复制）
    emit = hub_api._stage_recorder(task_dir, expected_run_id=run_id)
    log_line = hub_api._log_only(task_dir)
    beat = hub_api._heart(task_dir, expected_run_id=run_id)

    def stage(msg: str, *, terminal: bool = False) -> None:
        emit(msg, terminal=terminal)
        beat(msg)

    def on_beat(msg: str) -> None:
        log_line(msg)                                 # AT-16 B4b：等待心跳只进日志
        beat(msg)                                     # AT-19：同时刷新侧车（含产物锚）

    outcome = "failed"
    code = EXIT_TASK_FAILED
    tail = ""
    source = "exception"
    try:
        result = discuss.run_discuss(task_dir, verbose=False, on_stage=stage,
                                     on_heartbeat=on_beat)
        # AT-17 B2c：拆解完成即落音频三态（默认值；已有值不覆盖）
        mode = hub_api._persist_audio_mode(task_dir)
        if mode.get("audio_mode"):
            emit(f"音频三态：{mode['audio_mode']}（{mode.get('依据') or '已有值'}）")
        else:
            emit(f"音频三态：未落盘（{mode.get('why') or '读不到拆解'}）")
        emit("终态：awaiting_assets（等置换）", terminal=True)
        beat("终态：awaiting_assets（等置换）")
        outcome, code = "done", EXIT_OK
        return EXIT_OK
    except discuss.SlotBusy as exc:
        # 槽占用等到上限 = **本轮没提交上去**（环境级，不是远程判失败）：状态保留 discussing，
        # 稍后再点重试即续。文案已在 discuss 侧按 remote_wait 唯一源生成（在等谁的远程返回、
        # 预计还需多久），这里只负责如实退场，不许把它写成 failed。
        message = str(exc) or "拆解槽一直被占用，本次未提交"
        emit(f"终态：本轮未提交（{message}）——状态保留，稍后再点重试", terminal=True)
        beat(f"终态：本轮未提交（{message}）")
        tail, outcome, code, source = message, "env_error", EXIT_ENV, "slot_busy"
        return EXIT_ENV
    except discuss.DiscussError as exc:
        # 任务级失败：discuss 已把 failed 写进任务.json（唯一写盘核 state.write_failed）。
        emit(f"终态：失败（{exc}）", terminal=True)
        beat(f"终态：失败（{exc}）")
        tail, outcome, code = str(exc), "failed", EXIT_TASK_FAILED
        return EXIT_TASK_FAILED
    except state.StateError as exc:
        # 状态被并发推进（例如已被降级/接管）：不再写盘，按「本 run 作废」退场。
        emit(f"终态：本 run 作废（{exc}）", terminal=True)
        tail, outcome, code = str(exc), "superseded", EXIT_SUPERSEDED
        return EXIT_SUPERSEDED
    except Exception as exc:                          # noqa: BLE001
        # 环境级故障（桥连不上/超时）：状态保留 discussing，重跑续——不当死证。
        emit(f"终态：环境故障（{exc}）——状态保留，重跑续", terminal=True)
        beat(f"终态：环境故障（{exc}）——状态保留，重跑续")
        tail = f"{type(exc).__name__}: {exc}"
        outcome, code, source = "env_error", EXIT_ENV, "exception"
        return EXIT_ENV
    finally:
        _emit_exit(task_dir, run_id, code, outcome, tail, source)
        if tail:
            print(f"[runner] {local_id} 终态={outcome} exit={code}：{tail}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="AT-19 拆解执行体（子进程入口）")
    ap.add_argument("local_id")
    ap.add_argument("--run-id", required=True)
    args = ap.parse_args(argv)
    run_id = str(args.run_id or os.environ.get("WANREPRO_EXEC_RUN_ID") or "")
    if not run_id:
        print("[runner] 缺 run_id（--run-id 或 WANREPRO_EXEC_RUN_ID）", file=sys.stderr)
        return EXIT_ISOLATION
    try:
        return run(args.local_id, run_id)
    except Exception:                                 # noqa: BLE001 —— 兜底：崩溃也要留退出码
        print(f"[runner] 未捕获异常：\n{traceback.format_exc()}", file=sys.stderr)
        task_dir = Path(config.PROJECTS_DIR) / args.local_id
        if (task_dir / "任务.json").is_file():
            _emit_exit(task_dir, run_id, EXIT_ENV, "crash",
                       traceback.format_exc()[-2000:], "exception")
        return EXIT_ENV


if __name__ == "__main__":
    raise SystemExit(main())
