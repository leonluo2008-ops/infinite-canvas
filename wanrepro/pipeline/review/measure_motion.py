# -*- coding: utf-8 -*-
"""measure_motion.py — 量化成片的「有效内容时长」与「静止尾巴」

用途：复盘「生成了 N 秒但后面空着」这类问题，用数据而不是肉眼。
输出：最后一次显著运动时刻、其后静止时长、0.5s 粒度帧差。
"""
from __future__ import annotations

import argparse
import collections
import pathlib

import cv2
import numpy as np


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--thr", type=float, default=0.5, help="判为『有运动』的帧差阈值")
    a = ap.parse_args()
    cap = cv2.VideoCapture(a.video)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    prev, diffs = None, []
    while True:
        ok, f = cap.read()
        if not ok:
            break
        g = cv2.cvtColor(cv2.resize(f, (120, 214)), cv2.COLOR_BGR2GRAY).astype(np.float32)
        if prev is not None:
            diffs.append(float(np.abs(g - prev).mean()))
        prev = g
    total = len(diffs) / fps
    ts = [i / fps for i in range(len(diffs))]
    last = max((t for t, d in zip(ts, diffs) if d > a.thr), default=0.0)
    print(f"视频 {pathlib.Path(a.video).name}｜{total:.2f}s｜{len(diffs)} 帧｜{fps:.0f}fps")
    print(f"最后一次显著运动（帧差>{a.thr}）：{last:.2f}s  → 其后静止 {total - last:.2f}s")
    by = collections.defaultdict(list)
    for t, d in zip(ts, diffs):
        by[round(t * 2) / 2].append(d)
    print("0.5s 粒度：" + " ".join(f"{k:.1f}s:{np.mean(v):.2f}" for k, v in sorted(by.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
