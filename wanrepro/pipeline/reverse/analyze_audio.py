#!/usr/bin/env python3
"""Measure video audio and transcribe speech with faster-whisper; failures stay unknown."""
from __future__ import annotations

import argparse
import gc
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


def unknown_audio(reason: str | None = None) -> dict:
    return {
        "duration_s": None, "mean_db": None, "max_db": None,
        "has_voice": None, "language": None, "language_prob": None,
        "speech_ratio": None, "dialogue": [], "music_likely": None,
        "notes": [reason] if reason else [],
    }


def run(command: list[str]) -> subprocess.CompletedProcess:
    result = subprocess.run(command, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=300)
    if result.returncode:
        raise RuntimeError(f"{command[0]} exit={result.returncode}: {result.stderr.strip()[-1500:]}")
    return result


def transcribe(wav: Path, model_name: str, device: str, notes: list[str]):
    from faster_whisper import WhisperModel
    from faster_whisper.utils import download_model

    # Prefer the cache without a network metadata check on every video.
    if Path(model_name).is_dir():
        model_path = model_name
    else:
        try:
            model_path = download_model(model_name, local_files_only=True)
        except FileNotFoundError:
            model_path = download_model(model_name)
    if device == "auto":
        import ctranslate2
        try:
            device = "cuda" if ctranslate2.get_cuda_device_count() else "cpu"
        except RuntimeError as exc:
            notes.append(f"CUDA 探测失败，改用 CPU int8：{exc}")
            device = "cpu"
    devices = [device, "cpu"] if device == "cuda" else [device]
    for current in devices:
        model = None
        try:
            model = WhisperModel(model_path, device=current,
                                 compute_type="float16" if current == "cuda" else "int8")
            segments, info = model.transcribe(str(wav), vad_filter=True, beam_size=5)
            # Transcription is lazy: iterate here so CUDA runtime failures also retry on CPU.
            return list(segments), info
        except Exception as exc:
            if current != "cuda":
                raise
            notes.append(f"CUDA 转写失败，改用 CPU int8：{type(exc).__name__}: {exc}")
        finally:
            del model
            gc.collect()


def analyze_audio(video: Path, model: str = "large-v3", device: str = "auto") -> dict:
    audio = unknown_audio()
    try:
        probe = json.loads(run([
            "ffprobe", "-v", "error", "-show_format", "-show_streams",
            "-of", "json", str(video),
        ]).stdout)
        duration = float(probe["format"]["duration"])
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("video duration must be finite and positive")
        audio["duration_s"] = duration
        if not any(stream.get("codec_type") == "audio" for stream in probe.get("streams", [])):
            audio.update(has_voice=False, speech_ratio=0.0, music_likely=False)
            audio["notes"].append("视频无音轨，音频实测无人声")
            return audio
        volume = run([
            "ffmpeg", "-hide_banner", "-nostdin", "-i", str(video),
            "-map", "0:a:0", "-vn", "-af", "volumedetect", "-f", "null", "-",
        ]).stderr
        for key, label in (("mean_db", "mean_volume"), ("max_db", "max_volume")):
            match = re.search(rf"{label}:\s*([-+\d.inf]+)\s*dB", volume)
            if not match or not math.isfinite(float(match.group(1))):
                raise ValueError(f"ffmpeg did not return finite {label}")
            audio[key] = float(match.group(1))
        # TMPDIR is supplied by the caller; never put decoded audio in the repository.
        temp_root = os.environ.get("TMPDIR") or str(
            Path.home() / "AppData/Local/hermes/cache/scratch" if os.name == "nt"
            else Path(tempfile.gettempdir()))
        Path(temp_root).mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="reverse_audio_", dir=temp_root) as folder:
            wav = Path(folder) / "audio.wav"
            run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(video),
                 "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000",
                 "-c:a", "pcm_s16le", "-y", str(wav)])
            segments, info = transcribe(wav, model, device, audio["notes"])
        dialogue = []
        for segment in segments:
            start, end = max(0.0, float(segment.start)), min(duration, float(segment.end))
            text = segment.text.strip()
            if text and end > start:
                dialogue.append({"t_from_s": start, "t_to_s": end, "text": text})
        ratio = sum(line["t_to_s"] - line["t_from_s"] for line in dialogue) / duration
        has_voice = bool(dialogue) and ratio >= 0.05
        audio.update(has_voice=has_voice, language=info.language if dialogue else None,
                     language_prob=float(info.language_probability) if dialogue else None,
                     speech_ratio=ratio, dialogue=dialogue)
        if has_voice:
            audio["notes"].append("人声与音乐未分离")
        elif audio["mean_db"] > -40:
            audio["music_likely"] = True
            audio["notes"].append("有音频但无人声，可能是音乐/环境声（未做曲式与 BPM 识别）")
        else:
            audio["music_likely"] = False
        if dialogue and not has_voice:
            audio["notes"].append("检出短语音段，但语音占比低于 0.05，未达到有人声判定阈值；台词保留供人工核对")
    except Exception as exc:
        audio.update(has_voice=None, language=None, language_prob=None,
                     speech_ratio=None, dialogue=[], music_likely=None)
        audio["notes"].append(f"音频分析失败：{type(exc).__name__}: {exc}")
    return audio


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--model", default="large-v3")
    parser.add_argument("--device", choices=("auto", "cuda", "cpu"), default="auto")
    args = parser.parse_args(argv)
    video, out = args.video.expanduser().resolve(), args.out.expanduser().resolve()
    if out == video:
        parser.error("output must differ from video")
    audio = analyze_audio(video, args.model, args.device)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(audio, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                   encoding="utf-8")
    status = "WARN" if audio["has_voice"] is None else "OK"
    print(f"{status} {out} has_voice={audio['has_voice']} language={audio['language']} "
          f"segments={len(audio['dialogue'])}")
    if audio["has_voice"] is None:
        print("WARN " + "；".join(audio["notes"]), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
