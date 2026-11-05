#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""拆解进度文件（analysis/reverse/<id>/进度.json）：给轮询端看「现在跑到哪一步」。

为什么要有它：拆解一跑 10–20 分钟，轮询端只有计时器和 job 尾号，用户不知道任务
到底在不在正常执行（M6-T6F 假失败的次因）。这里只做一件事：在关键阶段边界把
当前阶段覆盖写进 进度.json，格式 {"stage": 中文阶段名, "ts": epoch}；失败时
{"stage": "失败", "error": 原因}。阶段序列（实际执行顺序）：

    抽帧 → 大拆解 → 汇总 → 专项判定 → 投影 → 完成

谁在写：
    ui/server.py            开跑即写「抽帧」（盖掉同任务上一轮的残留进度）；
    analyze.py              主流程各阶段边界（抽帧/汇总/专项判定/投影/完成/失败）；
    pipeline/reverse/extract.py  进大拆解主调用前标「大拆解」（拆解里唯一的长阶段）。

约定：
- 覆盖式原子写（临时文件 + os.replace），半截 JSON 不会被人当真。
- 同一任务同一时刻只有一个 analyze 在跑（ANALYZING / ANALYZE_BUSY 保证），无交错写。
- 写失败只留一行 stderr 痕迹，绝不往调用方抛——进度显示永远不值得让拆解本身失败。
- 读端（ui/server.py read_progress）读不到 / 坏 JSON 一律当「没有进度」，字段缺省。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

NAME = "进度.json"


def write_progress(outdir, stage: str, error: str | None = None) -> None:
    """覆盖式写当前阶段。任何异常都就地吞掉（打一行 stderr），不影响拆解主流程。

    outdir 接 Path 或可转字符串的任务目录；stage 是给人看的中文阶段名；
    error 只在 stage="失败" 时附带原因。
    """
    try:
        outdir = Path(outdir)
        outdir.mkdir(parents=True, exist_ok=True)
        data: dict = {"stage": str(stage), "ts": int(time.time())}
        if error:
            data["error"] = str(error)
        target = outdir / NAME
        temp = target.with_name(f".{NAME}.tmp{os.getpid()}")
        temp.write_text(json.dumps(data, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(temp, target)
    except OSError as exc:
        print(f"WARN 进度没写成（{stage}）：{exc}", file=sys.stderr)


def read_progress(task_dir) -> dict | None:
    """读任务的进度；不在 / 坏 JSON / 不是对象 一律返回 None（调用方按「没有」处理）。"""
    try:
        raw = (Path(task_dir) / NAME).read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None
