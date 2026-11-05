#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Verify cut candidates by the spatial distribution of frame changes."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2


GRID_SIZE = 6
LOCAL_HIST_CORR = 0.6
MIN_CHANGED_CELLS = 25
OFFSET_S = 0.12
RESIZED_HEIGHT = 240
# 判定主信号：6×6 网格里「局部颜色分布明显变了」的格子数（共 36 格）。
# 实测依据（2026-10-28，两段片共 19 个候选点，逐点看过前后帧）：
#   真切点（换人/换景/进黑场）→ 29/36、31、33、34、34、36
#   字幕换词、产品小窗进出、镜头内运动 → 3、9、9、11、13、13、14、18、19、20、20、21
# 两侧最近距离 29 vs 21，判据取 25。格子数阈值 25 不是拍的，是按这个间隔定的；
# 再改判据必须先重跑那批对照点。切点验收已归入 docs/ROADMAP.md 批次 B3
# （产物单一真源与旧中间件隔离，覆盖体检 B2「切点/台词双口径」问题），
# 按批次要求先建基准夹具（tests/fixtures/）后才能重跑。
HIST_CORR_THRESHOLD = 0.6  # 仅用于日志里打印全局直方图相关（同色系切换会失灵，不作判定）


def _read_target_frames(video: Path, times: list[float]) -> dict[float, object]:
    """Read the requested timestamps by walking VideoCapture frames in order."""
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        cap.release()
        raise RuntimeError(f"cannot open video: {video}")

    fps = cap.get(cv2.CAP_PROP_FPS)
    if not fps or fps <= 0:
        cap.release()
        raise RuntimeError(f"invalid video FPS: {fps!r}")

    target_indices = {
        time: max(0, int(round(time * fps)))
        for time in times
    }
    wanted = {}
    for time, index in target_indices.items():
        wanted.setdefault(index, []).append(time)

    frames = {}
    frame_index = 0
    try:
        while wanted:
            ok, frame = cap.read()
            if not ok:
                break
            for time in wanted.pop(frame_index, []):
                frames[time] = frame.copy()
            frame_index += 1
    finally:
        cap.release()
    return frames


def changed_cell_count(before, after) -> int:
    """6×6 网格里局部 HSV 直方图相关性 < 0.6 的格子数（0-36）。

    只看亮度差会被「同色系换景」骗过（官号参考1 手持→俯拍，全局直方图相关 0.99），
    只看整幅直方图会被「全屏字幕」骗过（ai视频参考1 字幕换词，全局直方图相关 0.99）。
    逐格看颜色分布的搬动，两者都能分开。
    """
    height = RESIZED_HEIGHT
    width = max(GRID_SIZE, round(before.shape[1] * height / before.shape[0]))
    before_small = cv2.resize(before, (width, height), interpolation=cv2.INTER_AREA)
    after_small = cv2.resize(after, (width, height), interpolation=cv2.INTER_AREA)

    changed = 0
    for row in range(GRID_SIZE):
        y0 = row * height // GRID_SIZE
        y1 = (row + 1) * height // GRID_SIZE
        for col in range(GRID_SIZE):
            x0 = col * width // GRID_SIZE
            x1 = (col + 1) * width // GRID_SIZE
            cell_before = cv2.calcHist([cv2.cvtColor(before_small[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)],
                                       [0, 1], None, [16, 16], [0, 180, 0, 256])
            cell_after = cv2.calcHist([cv2.cvtColor(after_small[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)],
                                      [0, 1], None, [16, 16], [0, 180, 0, 256])
            cv2.normalize(cell_before, cell_before)
            cv2.normalize(cell_after, cell_after)
            if float(cv2.compareHist(cell_before, cell_after, cv2.HISTCMP_CORREL)) < LOCAL_HIST_CORR:
                changed += 1
    return changed


def hist_correlation(before, after) -> float:
    """HSV 二维直方图相关性：1=颜色分布相同，越低越可能是换了画面。"""
    hist_before = cv2.calcHist([cv2.cvtColor(before, cv2.COLOR_BGR2HSV)], [0, 1], None,
                               [30, 32], [0, 180, 0, 256])
    hist_after = cv2.calcHist([cv2.cvtColor(after, cv2.COLOR_BGR2HSV)], [0, 1], None,
                              [30, 32], [0, 180, 0, 256])
    cv2.normalize(hist_before, hist_before)
    cv2.normalize(hist_after, hist_after)
    return float(cv2.compareHist(hist_before, hist_after, cv2.HISTCMP_CORREL))


def verify_cuts(video: Path, cuts: list[float]) -> list[tuple[float, int, bool]]:
    """Verify candidates using frames 0.12s before and after each candidate."""
    times = []
    for cut in cuts:
        times.extend((cut - OFFSET_S, cut + OFFSET_S))
    frames = _read_target_frames(video, times)

    results = []
    for cut in cuts:
        before = frames.get(cut - OFFSET_S)
        after = frames.get(cut + OFFSET_S)
        if before is None or after is None:
            results.append((cut, (0, 1.0), False))
            continue
        cells = changed_cell_count(before, after)
        corr = hist_correlation(before, after)
        results.append((cut, (cells, corr), cells >= MIN_CHANGED_CELLS))
    return results


def filter_cuts(video: Path, cuts: list[float]) -> list[float]:
    """Keep only candidates whose local colour layout changed across most cells."""
    return [cut for cut, _cells, keep in verify_cuts(video, cuts) if keep]


def parse_cuts(raw: str) -> list[float]:
    if not raw.strip():
        return []
    try:
        return [float(value.strip()) for value in raw.split(",") if value.strip()]
    except ValueError as exc:
        raise ValueError(f"invalid --cuts value: {raw!r}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video")
    parser.add_argument("--cuts", required=True, help="comma-separated candidate timestamps")
    args = parser.parse_args()

    video = Path(args.video).expanduser().resolve()
    if not video.exists():
        print(f"视频不存在: {video}", file=sys.stderr)
        return 2
    try:
        cuts = parse_cuts(args.cuts)
        results = verify_cuts(video, cuts)
    except (OSError, RuntimeError, ValueError, cv2.error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    for cut, (cells, corr), keep in results:
        verdict = "KEEP" if keep else "DROP"
        print(f"t={cut:g} cells={cells}/36 hist={corr:.3f} {verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
