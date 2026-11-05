# -*- coding: utf-8 -*-
"""overlay_logo.py — 后期叠品牌 logo（通用，不含任何产品信息）

用法：
  py -3 pipeline/post/overlay_logo.py --video in.mp4 --logo logo.png \
      --start 9.6 --fade 0.6 --out out.mp4 [--width-pct 38] [--y-pct 48]

规则（来自实测）：
- logo 一律后期叠，不让模型生成（480P 下文字必糊、模型会重排丢字）
- 叠片用透明 PNG，宽度按画面宽度百分比，避免不同分辨率下比例漂移
- 淡入比硬切自然；尾段保持到片尾，不做黑底
"""
from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import subprocess
import sys


def probe(path: pathlib.Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format",
                          "-of", "json", str(path)], capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    d = json.loads(out.stdout or "{}")
    v = next((s for s in d.get("streams", []) if s.get("codec_type") == "video"), {})
    return {"w": int(v.get("width", 0)), "h": int(v.get("height", 0)),
            "dur": float(d.get("format", {}).get("duration", 0))}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--logo", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--start", type=float, default=0.0, help="logo 出现时刻（秒）")
    ap.add_argument("--fade", type=float, default=0.6)
    ap.add_argument("--width-pct", type=float, default=38.0, help="logo 占画面宽度百分比")
    ap.add_argument("--y-pct", type=float, default=50.0, help="logo 中心纵向位置百分比")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    video, logo, out = pathlib.Path(a.video), pathlib.Path(a.logo), pathlib.Path(a.out)
    for p, name in ((video, "视频"), (logo, "logo")):
        if not p.exists():
            print(f"❌ {name}不存在：{p}")
            return 2

    out.parent.mkdir(parents=True, exist_ok=True)
    info = probe(video)
    w, h, dur = info["w"], info["h"], info["dur"]
    if not w:
        print("❌ 读不到视频宽高")
        return 2
    lw = max(2, int(w * a.width_pct / 100) // 2 * 2)
    y = int(h * a.y_pct / 100)
    start = max(0.0, min(a.start, max(0.0, dur - 0.2)))
    fade = max(0.0, min(a.fade, max(0.0, dur - start)))

    # logo 缩放到目标宽度，透明底；y 以中心对齐
    vf = (f"[1:v]scale={lw}:-1[lg];"
          f"[0:v][lg]overlay=(W-w)/2:{y}-h/2:enable='gte(t,{start})':"
          f"format=auto,format=yuv420p[v]")
    # alpha 淡入：给 logo 加 fade（在 overlay 前对 alpha 生效）
    vf = (f"[1:v]scale={lw}:-1,format=rgba,fade=t=in:st={start}:d={fade}:alpha=1[lg];"
          f"[0:v][lg]overlay=(W-w)/2:{y}-h/2:enable='gte(t,{start})':format=auto,format=yuv420p[v]")
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(video), "-loop", "1", "-i", str(logo),
           "-filter_complex", vf, "-map", "[v]", "-map", "0:a?",
           "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-c:a", "copy",
           "-t", f"{dur:.3f}", "-shortest", str(out)]

    print(f"源 {w}x{h} {dur:.2f}s｜logo {lw}px（{a.width_pct}% 宽）｜y={y}｜{start:.2f}s 起淡入 {fade:.2f}s")
    if a.dry_run:
        print("dry-run：", " ".join(cmd))
        return 0
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        print("❌ ffmpeg 失败：", (r.stderr or "")[-400:])
        return 1
    print(f"✅ 输出 {out}｜{out.stat().st_size / 1024:.0f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
