#!/usr/bin/env python3
"""Map VLM descriptions onto measured video segments; measured cuts own the timeline."""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

try:
    from .inject_measured import get_tech_measurements, inject
except ImportError:
    from inject_measured import get_tech_measurements, inject

ENDCARD_WORDS = ("tiktok", "搜索", "图标", "黑底")


def read_json(path: Path) -> dict:
    def reject_constant(value: str):
        raise ValueError(f"invalid JSON constant: {value}")

    data = json.loads(path.read_text(encoding="utf-8"), parse_constant=reject_constant)
    if not isinstance(data, dict):
        raise ValueError(f"expected JSON object: {path}")
    return data


def number(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def measured_boundaries(tech: dict) -> tuple[list[float], dict]:
    measured = get_tech_measurements(tech)
    duration = measured["duration_s"]
    cuts = measured["cut_points_s"]
    if duration <= 0 or any(not 0 < cut < duration for cut in cuts):
        raise ValueError("duration and cuts must be positive and inside the video")
    if cuts != sorted(set(cuts)):
        raise ValueError("stable cuts must be strictly increasing and unique")
    return [0.0, *cuts, duration], measured


def shot_intervals(shots: list, duration: float) -> list[tuple[float, float] | None]:
    """Use VLM time spans, or observed frame timestamps when spans are omitted."""
    intervals: list[tuple[float, float] | None] = [None] * len(shots)
    anchors: list[list[float]] = []
    for shot in shots:
        if not isinstance(shot, dict):
            raise ValueError("every VLM shot must be an object")
        start, end = shot.get("start_s"), shot.get("end_s")
        if number(start) and number(end) and 0 <= start < end:
            intervals[len(anchors)] = (float(start), min(float(end), duration))
        times = [step.get("t_s") for step in (shot.get("action_steps") or [])
                 if isinstance(step, dict)]
        times += [text.get(key) for text in (shot.get("onscreen_text") or [])
                  if isinstance(text, dict) for key in ("t_from_s", "t_to_s")]
        anchors.append(sorted(float(t) for t in times if number(t) and 0 <= t <= duration))

    # With no explicit span, only observed timestamps place a shot. The next
    # shot's first timestamp ends the current descriptive interval; never
    # extend a later description backward into an earlier measured segment.
    for i, observed in enumerate(anchors):
        if intervals[i] is not None or not observed:
            continue
        next_start = next((a[0] for a in anchors[i + 1:] if a), duration)
        start = observed[0] if i else 0.0
        end = max(observed[-1], next_start) if i + 1 < len(shots) else duration
        if end > start:
            intervals[i] = (start, min(end, duration))
    return intervals


def build_spec(vlm: dict, tech: dict, audio: dict | None = None) -> dict:
    bounds, measured = measured_boundaries(tech)
    source_shots = vlm.get("shots")
    if not isinstance(source_shots, list):
        raise ValueError("VLM shots must be an array")
    descriptions = shot_intervals(source_shots, measured["duration_s"])
    enriched = inject(dict(vlm), measured)
    spec = {key: enriched[key] for key in
            ("meta", "scene_env", "style", "lighting", "onscreen_text", "audio",
             "entities", "conflicts", "global", "story", "audio_summary") if key in enriched}
    if audio is not None:
        # Fresh ASR replaces the visual-only audio object, including its nulls.
        spec["audio"] = {key: audio.get(key) for key in (
            "has_voice", "language", "language_prob", "speech_ratio", "dialogue",
            "music_likely", "mean_db", "max_db", "notes")}
    spec.setdefault("conflicts", [])
    spec["shots"] = []
    spec["endcard"] = []
    for i, (start, end) in enumerate(zip(bounds, bounds[1:]), 1):
        overlaps = [max(0.0, min(end, span[1]) - max(start, span[0]))
                    if span is not None else 0.0 for span in descriptions]
        best = max(range(len(overlaps)), key=overlaps.__getitem__) if overlaps else None
        segment = {"shot_id": i, "start_s": start, "end_s": end}
        if best is None or overlaps[best] < (end - start) * 0.1:
            segment.update(empty=True, why="VLM 未描述此段")
        else:
            segment.update({k: v for k, v in source_shots[best].items()
                            if k not in ("shot_id", "start_s", "end_s", "derived_from_shot")})
            segment["derived_from_shot"] = best + 1  # VLM shots use one-based indices.
        subject = str(segment.get("subject_primary") or "").lower()
        actions = segment.get("action_steps") or []
        in_segment = [action for action in actions if isinstance(action, dict)
                      and number(action.get("t_s"))
                      and start <= action["t_s"] < end]
        black_only = (bool(in_segment) and all(
            any(word in str(action.get("physical_step") or "")
                for word in ("全黑", "黑屏", "纯黑", "渐隐变黑", "无画面"))
            for action in in_segment))
        if black_only or ("全黑" in subject and "无主体" in subject and not segment.get("contact")):
            segment = {"shot_id": i, "start_s": start, "end_s": end,
                       "empty": True, "why": "VLM 仅观察到纯黑画面，无可复刻主体",
                       "derived_from_shot": best + 1}
        elif in_segment:
            segment["action_steps"] = in_segment
        if (not segment.get("empty") and not segment.get("contact")
                and any(word in subject for word in ENDCARD_WORDS)):
            spec["endcard"].append(segment)
        else:
            spec["shots"].append(segment)
    return spec


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vlm", type=Path, required=True)
    parser.add_argument("--tech", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--audio", type=Path, help="fresh audio analysis overrides VLM audio")
    args = parser.parse_args()
    try:
        inputs = [args.vlm, args.tech] + ([args.audio] if args.audio else [])
        if args.out.resolve() in [path.resolve() for path in inputs]:
            raise ValueError("output must differ from all inputs")
        spec = build_spec(read_json(args.vlm), read_json(args.tech),
                          read_json(args.audio) if args.audio else None)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(spec, ensure_ascii=False, indent=2,
                                      sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        print(f"ERROR build_spec: {exc}", file=sys.stderr)
        return 2
    print(f"OK {args.out} segments={len(spec['shots']) + len(spec['endcard'])} "
          f"endcard={len(spec['endcard'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
