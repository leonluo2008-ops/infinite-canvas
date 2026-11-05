#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Detect scene changes by comparing local HSV histograms frame by frame."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import cv2

try:
    from .verify_cuts import GRID_SIZE, LOCAL_HIST_CORR, RESIZED_HEIGHT
except ImportError:
    from verify_cuts import GRID_SIZE, LOCAL_HIST_CORR, RESIZED_HEIGHT

HARD_CUT_MIN_SCORE = 25
HARD_CUT_PREV_MAX = 12
CANDIDATE_MIN_SCORE = 12
HARD_CUT_MERGE_S = 0.15
CANDIDATE_CLUSTER_S = 0.5
OPENING_IGNORE_S = 0.2


def _frame_histograms(frame):
    height = RESIZED_HEIGHT
    width = max(GRID_SIZE, round(frame.shape[1] * height / frame.shape[0]))
    small = cv2.resize(frame, (width, height), interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    histograms = []
    for row in range(GRID_SIZE):
        y0 = row * height // GRID_SIZE
        y1 = (row + 1) * height // GRID_SIZE
        for col in range(GRID_SIZE):
            x0 = col * width // GRID_SIZE
            x1 = (col + 1) * width // GRID_SIZE
            hist = cv2.calcHist([hsv[y0:y1, x0:x1]], [0, 1], None,
                                [16, 16], [0, 180, 0, 256])
            cv2.normalize(hist, hist)
            histograms.append(hist)
    return histograms


def _changed_cells(before, after) -> int:
    return sum(
        float(cv2.compareHist(hist_before, hist_after, cv2.HISTCMP_CORREL)) < LOCAL_HIST_CORR
        for hist_before, hist_after in zip(before, after)
    )



def _cluster_candidates(candidates: list[dict]) -> list[dict]:
    clusters = []
    for candidate in candidates:
        if not clusters or candidate["t"] - clusters[-1][-1]["t"] > CANDIDATE_CLUSTER_S:
            clusters.append([candidate])
        else:
            clusters[-1].append(candidate)
    return [max(cluster, key=lambda item: item["score"]) for cluster in clusters]


def detect_scenes(video: Path, fps_fallback: float = 30.0) -> dict:
    """Return isolated hard cuts and clustered motion/soft-transition candidates."""
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        cap.release()
        raise RuntimeError(f"cannot open video: {video}")

    fps = cap.get(cv2.CAP_PROP_FPS)
    if not fps or fps <= 0:
        fps = fps_fallback
    if not fps or fps <= 0:
        cap.release()
        raise RuntimeError(f"invalid video FPS and fallback: {fps!r}")

    scores = []
    previous_histograms = None
    previous_scores = []
    frame_index = 0
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            current_histograms = _frame_histograms(frame)
            if previous_histograms is not None:
                time_s = frame_index / fps
                score = _changed_cells(previous_histograms, current_histograms)
                prev = max(previous_scores[-2:], default=0)
                scores.append({"t": time_s, "score": score, "prev": prev})
                previous_scores.append(score)
            previous_histograms = current_histograms
            frame_index += 1
    finally:
        cap.release()

    eligible = [item for item in scores if item["t"] > OPENING_IGNORE_S]
    raw_hard = [item for item in eligible
                if item["score"] >= HARD_CUT_MIN_SCORE and item["prev"] < HARD_CUT_PREV_MAX]
    hard_cuts = []
    for item in raw_hard:
        if not hard_cuts or item["t"] - hard_cuts[-1]["t"] > HARD_CUT_MERGE_S:
            hard_cuts.append(item)

    hard_times = {item["t"] for item in hard_cuts}
    candidates = [item for item in eligible
                  if item["score"] >= CANDIDATE_MIN_SCORE and item["t"] not in hard_times]
    motion_or_soft = _cluster_candidates(candidates)
    return {
        "events": eligible,
        "stable_cuts": hard_cuts,
        "motion_or_soft": motion_or_soft,
    }


def _rounded_results(results: dict) -> dict:
    def rounded(items: list[dict]) -> list[dict]:
        return [{**item, "t": round(item["t"], 3)} for item in items]

    events = rounded(results["events"])
    stable_cuts = rounded(results["stable_cuts"])
    motion_or_soft = rounded(results["motion_or_soft"])
    return {
        "hits": events,
        "cuts": [item["t"] for item in stable_cuts],
        "stable_cuts": stable_cuts,
        "motion_or_soft": motion_or_soft,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video")
    parser.add_argument("--fps", type=float, default=30.0,
                        help="fallback FPS only when video metadata has no valid frame rate")
    parser.add_argument("--json", action="store_true", help="print cut lists as JSON")
    args = parser.parse_args()

    video = Path(args.video).expanduser().resolve()
    if not video.exists():
        print(f"视频不存在: {video}", file=sys.stderr)
        return 2
    try:
        results = _rounded_results(detect_scenes(video, fps_fallback=args.fps))
    except (OSError, RuntimeError, ValueError, cv2.error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(results, ensure_ascii=False))
        return 0

    hard_times = {item["t"] for item in results["stable_cuts"]}
    candidate_times = {item["t"] for item in results["motion_or_soft"]}
    for event in results["hits"]:
        if event["t"] in hard_times:
            verdict = "CUT"
        elif event["t"] in candidate_times:
            verdict = "CANDIDATE"
        elif event["score"] >= CANDIDATE_MIN_SCORE:
            verdict = "CANDIDATE"
        else:
            continue
        print(f"t={event['t']:.3f} score={event['score']} prev={event['prev']} {verdict}")
    print(f"CUTS {results['cuts']}")
    print(f"MOTION_OR_SOFT {[item['t'] for item in results['motion_or_soft']]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
