#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""delogo_local.py — 本地擦除画面固定位置的平台水印（TikTok 角标）

参考视频来自 TikTok，抽帧后角落带 TikTok 水印。合成图沿用该帧，水印必须去掉：
既不能出现在交付物里，也会被模型当成"画面内容"复刻。

用法：
  py -3 pipeline/reverse/delogo_local.py --glob "<项目目录>/analysis/assets/*.png" \
      --box 0,1190,175,1475 --suffix _clean
"""
from __future__ import annotations

import argparse
import glob as g
from pathlib import Path

import cv2
import numpy as np


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--glob", required=True)
    ap.add_argument("--box", required=True, help="x0,y0,x1,y1 像素（基于原图）")
    ap.add_argument("--suffix", default="_clean")
    ap.add_argument("--radius", type=int, default=9)
    args = ap.parse_args()

    x0, y0, x1, y1 = (int(v) for v in args.box.split(","))
    files = sorted(g.glob(args.glob))
    if not files:
        raise SystemExit(f"没有匹配文件: {args.glob}")

    for f in files:
        img = cv2.imdecode(np.fromfile(f, dtype=np.uint8), cv2.IMREAD_COLOR)
        h, w = img.shape[:2]
        mask = np.zeros((h, w), np.uint8)
        mask[max(0, y0):min(h, y1), max(0, x0):min(w, x1)] = 255
        out = cv2.inpaint(img, mask, args.radius, cv2.INPAINT_TELEA)
        p = Path(f)
        dst = p.with_name(p.stem + args.suffix + p.suffix)
        ok, buf = cv2.imencode(".png", out)
        buf.tofile(str(dst))
        print(f"✅ {dst.name}  擦除框=({x0},{y0})-({x1},{y1})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
