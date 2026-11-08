# -*- coding: utf-8 -*-
"""AT-6 批量推进器（runner）——扫全部任务，能自动推的推，需要人的亮灯。

状态分桶与动作：
  ingested        → 自动：run_discuss（拆解已授权）
  discussing      → 自动：run_discuss 续跑（产物优先）
  awaiting_assets → 亮灯：待置换（人工给图 → automation.replace）
  rendering       → 自动：do_render（渲染+预演）
  awaiting_approval → 亮灯：待审批（人工 approve）
  approved        → 自动：do_submit（已批准的提交属已授权动作）
  generating      → 自动：do_submit 续查（产物优先）
  done / failed   → 无动作（failed 亮灯供人工检查）

并发（红线：拆解/生成各单槽，busy=排队）：
  主循环串行推进；拆解靠桥 409 排队语义（discuss 自带重试），生成同槽串行。
  --loop 常驻：每轮间隔默认 30s；--once 单轮（cron/手动驱动）。

用法：
  py -3 -m automation.runner --once
  py -3 -m automation.runner --loop 30
"""
import argparse
import sys
import time
import traceback
from pathlib import Path

from . import config, state
from .discuss import run_discuss
from .generate import do_render, do_submit

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

LIGHT = {"awaiting_assets": "待置换（automation.replace 回填+确认）",
         "awaiting_approval": "待审批（automation.generate approve）",
         "failed": "失败待检查（任务.json history 有原文）"}


def scan() -> list[tuple[Path, dict]]:
    out = []
    if config.PROJECTS_DIR.is_dir():
        for mf in sorted(config.PROJECTS_DIR.glob("auto-*/任务.json")):
            try:
                out.append((mf.parent, state.load(mf.parent)))
            except state.StateError:
                continue
    return out


def advance(task_dir: Path, st: str) -> str:
    """推进一个任务一步。返回动作描述。"""
    if st in ("ingested", "discussing"):
        run_discuss(task_dir, verbose=False)
        return "拆解→awaiting_assets"
    if st == "rendering":
        do_render(task_dir, "", verbose=False)
        return "渲染→awaiting_approval"
    if st in ("approved", "generating"):
        do_submit(task_dir, verbose=False)
        return "提交/续查→done"
    return ""


def run_once(verbose: bool = True) -> dict:
    """单轮：推进所有可自动任务；返回状态板。失败任务如实记数不中断其他。"""
    board: dict[str, int] = {}
    acted, errs = [], []
    for tdir, task in scan():
        st = task["state"]
        board[st] = board.get(st, 0) + 1
        if st in LIGHT or st in ("done",):
            continue
        try:
            note = advance(tdir, st)
            if note:
                acted.append(f"{task['local_id']}: {note}")
        except Exception as e:  # noqa: BLE001 —— 单任务失败不拖垮整轮
            errs.append(f"{task['local_id']}: {type(e).__name__} {str(e)[:120]}")
            traceback.print_exc(file=sys.stderr)
    if verbose:
        print(f"[状态板] {board}")
        for a in acted:
            print(f"  [推进] {a}")
        for tdir2, task2 in scan():
            if task2["state"] in LIGHT:
                print(f"  [亮灯] {task2['local_id']}: {LIGHT[task2['state']]}")
        for e in errs:
            print(f"  [错误] {e}", file=sys.stderr)
    return {"board": board, "acted": len(acted), "errors": len(errs)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="AT-6 批量推进器")
    ap.add_argument("--loop", type=int, metavar="SEC", help="常驻模式，轮间隔秒")
    ap.add_argument("--once", action="store_true", help="单轮")
    ap.add_argument("--bridge")
    args = ap.parse_args(argv)
    if args.bridge:
        config.BRIDGE_BASE = args.bridge.rstrip("/")
    config.ensure_dirs()
    if args.loop:
        while True:
            run_once()
            time.sleep(args.loop)
    run_once()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
