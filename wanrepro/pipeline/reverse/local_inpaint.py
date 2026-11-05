#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""local_inpaint.py — 本地免费擦除（cv2.inpaint），用于在花钱调万相图像编辑之前先看个大概

只擦 spec 里标记的产品区域（+ 可选外扩），不擦手（手要擦得靠万相 image2image）。
产出：图片2_场景板_本地修补.jpg，以及 mask 预览图。

用法：
  py -3 pipeline/reverse/local_inpaint.py --spec analysis/reverse/<id>/spec.json \
      --frame analysis/reverse/<id>/assets/场景板_待清理.jpg \
      -o analysis/reverse/<id>/assets
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", required=True)
    ap.add_argument("--frame", required=True)
    ap.add_argument("-o", "--outdir", required=True)
    ap.add_argument("--pad", type=int, default=6, help="bbox 外扩像素，覆盖阴影与边缘")
    args = ap.parse_args()

    spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    img = cv2.imdecode(np.fromfile(args.frame, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise SystemExit(f"读不到图片: {args.frame}")
    h, w = img.shape[:2]

    mask = np.zeros((h, w), np.uint8)
    boxes = []
    for s in spec["shots"]:
        pp = s.get("product_presence", {})
        if not pp.get("present"):
            continue
        b = pp.get("bbox_rel", {})
        x0 = int(b.get("x", 0) * w) - args.pad
        y0 = int(b.get("y", 0) * h) - args.pad
        x1 = int((b.get("x", 0) + b.get("w", 0)) * w) + args.pad
        y1 = int((b.get("y", 0) + b.get("h", 0)) * h) + args.pad
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(w, x1), min(h, y1)
        mask[y0:y1, x0:x1] = 255
        boxes.append([x0, y0, x1, y1])
    if not boxes:
        raise SystemExit("spec 里没有产品 bbox")

    out_dir = Path(args.outdir)
    out_dir.mkdir(parents=True, exist_ok=True)
    fixed = cv2.inpaint(img, mask, 7, cv2.INPAINT_TELEA)

    p_img = out_dir / "图片2_场景板_本地修补.jpg"
    p_mask = out_dir / "mask_产品区域.png"
    cv2.imencode(".jpg", fixed, [int(cv2.IMWRITE_JPEG_QUALITY), 95])[1].tofile(str(p_img))
    cv2.imencode(".png", mask)[1].tofile(str(p_mask))
    print(f"✅ {p_img}")
    print(f"   蒙版 {p_mask}  bbox={boxes}  原图 {w}x{h}")
    print("   注意：本地 inpaint 只补平面色块，瓶子留下的拖影会糊成一片；")
    print("   要真正补出麻薯/纸托的结构，得用 wan image2image（计费）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
