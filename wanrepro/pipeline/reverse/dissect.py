#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dissect.py — 参考视频逆向拆解的技术层（确定性数据，不依赖模型判断）

产出 tech.json：
  - 真实元数据（ffprobe）：时长/帧率/分辨率/音轨
  - 剪辑点（多阈值 scene detect，取稳定值）
  - 逐秒帧间差异（判断运动烈度与是否有硬切）
  - 音频逐秒 RMS（判断有无 BGM / 有无静音段）
  - 关键帧落盘（剪辑点前后 + 每秒一帧）

用法：
  py -3 pipeline/reverse/dissect.py "<video>" -o analysis/reverse/<id>

设计原则（重要）：
  时间轴、帧率、剪辑点、音量一律由 ffmpeg 测出并写进 tech.json；
  视觉模型只负责"画面里是什么、怎么动"，不得改写这些数字。
  —— 实测过：让模型自己报时长/剪辑点，它会把 13.35s 报成 12.0s、把 23.976fps 报成 30fps。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

try:
    from .detect_scenes import detect_scenes
except ImportError:
    from detect_scenes import detect_scenes


def run(cmd: list[str]) -> str:
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return (p.stdout or "") + (p.stderr or "")


def ffprobe_meta(video: Path) -> dict:
    out = run([
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(video),
    ])
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return {"error": out[:500]}

    v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    a = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    num, _, den = (v.get("r_frame_rate") or "0/1").partition("/")
    fps = round(int(num) / int(den), 3) if den and int(den) else None
    w, h = v.get("width"), v.get("height")
    return {
        "duration_s": round(float(data.get("format", {}).get("duration", 0)), 3),
        "width": w,
        "height": h,
        "aspect": f"{w}:{h}" if w and h else None,
        "aspect_note": ("9:16 精确" if w and h and abs(w / h - 9 / 16) < 0.005
                        else f"非精确 9:16（{round(w / h, 4)}）" if w and h else None),
        "fps": fps,
        "frames": v.get("nb_frames"),
        "video_codec": v.get("codec_name"),
        "has_audio": bool(a),
        "audio_codec": (a or {}).get("codec_name"),
        "audio_channels": (a or {}).get("channels"),
        "audio_sample_rate": (a or {}).get("sample_rate"),
        "bit_rate": data.get("format", {}).get("bit_rate"),
    }


def detect_cuts(video: Path, thresholds=(0.15, 0.25, 0.35, 0.5)) -> dict:
    """Return hard cuts and separately clustered motion/soft candidates."""
    detected = detect_scenes(video)
    hard_cuts = [round(hit["t"], 3) for hit in detected["stable_cuts"]]
    motion_or_soft = [round(hit["t"], 3) for hit in detected["motion_or_soft"]]
    all_pts = sorted(set(hard_cuts + motion_or_soft))
    res = {str(th): list(all_pts) for th in thresholds}
    return {
        "by_threshold": res,
        "all_points": all_pts,
        "stable_cuts": hard_cuts,
        "motion_or_soft": motion_or_soft,
    }


def per_second_framediff(video: Path, duration: float) -> list:
    """逐秒帧间差异（0-1，越大变化越剧烈）。用于判断运动烈度，不做剪辑判断。"""
    out = run([
        "ffmpeg", "-hide_banner", "-i", str(video),
        "-vf", "select='gte(scene,0)',metadata=print", "-an", "-f", "null", "-",
    ])
    vals = [float(x) for x in re.findall(r"scene_score=([0-9.]+)", out)]
    if not vals:
        return []
    buckets = {}
    step = max(1, len(vals) // max(1, int(duration)))
    for i, v in enumerate(vals):
        buckets.setdefault(i // step, []).append(v)
    return [{"sec": k, "mean_score": round(sum(v) / len(v), 4), "max_score": round(max(v), 4)}
            for k, v in sorted(buckets.items())]


def audio_rms_per_second(video: Path, sample_rate: int | None = None) -> list:
    out = run([
        "ffmpeg", "-hide_banner", "-i", str(video), "-vn",
        "-af", "astats=metadata=1:reset=1,ametadata=print:key=lavfi.astats.Overall.RMS_level",
        "-f", "null", "-",
    ])
    raw = re.findall(r"RMS_level=([-0-9.inf]+)", out)
    vals = [float(x) for x in raw if x not in ("-inf", "inf", "") and "inf" not in x]
    if not vals:
        return []
    # astats reset=1 是"每 1024 采样一块"，不是每秒一块 → 按块长折回秒
    blocks_per_sec = max(1, round((sample_rate or 44100) / 1024))
    buckets: dict[int, list[float]] = {}
    for i, v in enumerate(vals):
        buckets.setdefault(i // blocks_per_sec, []).append(v)
    return [{"sec": k, "rms_db": round(sum(v) / len(v), 2)} for k, v in sorted(buckets.items())]


def extract_frames(video: Path, outdir: Path, duration: float, cuts: list) -> list:
    outdir.mkdir(parents=True, exist_ok=True)
    times = {round(i * 1.0, 1) for i in range(int(duration) + 1)}
    for c in cuts:
        times.add(round(max(0.0, c - 0.2), 1))
        times.add(round(c, 3))
    files = []
    for t in sorted(times):
        if t > duration:
            continue
        name = f"t{t:06.2f}.jpg".replace(".", "_", 2).replace("_jpg", ".jpg")
        name = f"t{str(t).replace('.', '_')}s.jpg"
        path = outdir / name
        run(["ffmpeg", "-v", "error", "-ss", str(t), "-i", str(video),
             "-frames:v", "1", "-y", str(path)])
        if path.exists():
            files.append({"t_s": t, "file": str(path)})
    return files


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("-o", "--outdir", required=True)
    ap.add_argument("--no-frames", action="store_true")
    args = ap.parse_args()

    video = Path(args.video).expanduser().resolve()
    outdir = Path(args.outdir).expanduser().resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    if not video.exists():
        print(f"视频不存在: {video}", file=sys.stderr)
        return 2

    meta = ffprobe_meta(video)
    cuts = detect_cuts(video)
    duration = meta.get("duration_s") or 0
    tech = {
        "source": str(video),
        "meta": meta,
        "cuts": cuts,
        "framediff_per_second": per_second_framediff(video, duration),
        "audio_rms_per_second": audio_rms_per_second(
            video, int(meta.get("audio_sample_rate") or 44100)),
        "shot_boundaries_guess": [0.0] + cuts["stable_cuts"] + [duration],
    }
    if not args.no_frames:
        tech["frames"] = extract_frames(video, outdir / "frames", duration, cuts["stable_cuts"])

    out = outdir / "tech.json"
    out.write_text(json.dumps(tech, ensure_ascii=False, indent=2), "utf-8")
    print(f"✅ {out}")
    print(f"   时长 {meta.get('duration_s')}s / {meta.get('width')}x{meta.get('height')} / "
          f"{meta.get('fps')}fps / 音轨 {meta.get('has_audio')}")
    print(f"   稳定剪辑点: {cuts['stable_cuts'] or '无（单镜到底）'}")
    print(f"   关键帧: {len(tech.get('frames', []))} 张")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
