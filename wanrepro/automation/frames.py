# -*- coding: utf-8 -*-
"""AT-8 B2 取帧映射——出现时段 → 帧（±0.5s 缓存 → ffmpeg 现抽 → 中段/首帧兜底）。

为什么单独成模块，而不是直接扩 `ui/server.py:build_asset_frame`（2026-11-09）：
  · build_asset_frame(任务, 资产名) 靠「资产名在拆解.md 的哪行出现于」定时刻，同实体登记成两行
    （一件东西既算主体又算道具）时两行会落到同一帧；置换台要的是「这一行」的帧，
    时刻由侧车 `待确认清单.json` 的出现时段给出，与资产名无关。
  · 它不认 看帧/ 的缓存密度（±0.5s 就近取），也没有「时段空 → 整片中段」与「现抽失败 → 原片首帧」
    这两级兜底（数据合同 v3 的兜底链）。
  · 它在 ui/ 层：server → hub_api → automation 是单向依赖，automation 不能反向 import 它。
  取帧命令（`ffmpeg -ss … -frames:v 1 -vf scale=w=min(960,iw):h=-2`、最大宽 960）与它保持同一口径，
  免得同一任务出现两种尺寸的帧。

帧文件落点：`analysis/reverse/<bridge_id>/看帧/at8-<n>-<秒>.png`（与看帧缓存同目录，前端同一条路由取图）。
缓存池只认「拆解阶段的看帧」（看帧.json + 目录里的 <秒>.png），at8-* 派生帧不进缓存池——
否则第二次调用同一行会被标成「缓存」，来源就随调用次数漂移了。
"""
from __future__ import annotations

import json
import re
import subprocess
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]   # wanrepro/
ANALYSIS = ROOT / "analysis" / "reverse"

CACHE_TOL = 0.5        # 数据合同 v3：±0.5s 内有缓存帧就用缓存
MAX_WIDTH = 960        # 最大宽，与 build_asset_frame 同口径
DERIVED_PREFIX = "at8-"
FRAME_SUFFIX = ".png"
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.(?:png|jpg|jpeg)$", re.IGNORECASE)
CLOCK_RE = re.compile(r"^(\d{1,4}):(\d{1,2})(?:\.(\d{1,3}))?$")


class FrameError(RuntimeError):
    """取帧路径不合法 / 帧文件不存在——server 层转 4xx。"""


# ---------------------------------------------------------------- 时间码

def clock_seconds(text) -> float | None:
    """`MM:SS` / `MM:SS.SS` / 纯秒 → 秒；读不出返回 None（不猜）。

    与拆解.md 分镜表同口径：`00:12` 是 12 秒，不是 12 分。
    """
    raw = str(text if text is not None else "").strip()
    if not raw:
        return None
    match = CLOCK_RE.match(raw)
    if match:
        seconds = int(match.group(1)) * 60 + int(match.group(2))
        if match.group(3):
            seconds += float("0." + match.group(3))
        return round(float(seconds), 3)
    try:
        return round(float(raw), 3)
    except ValueError:
        return None


def span_bounds(span: dict | None) -> tuple[float, float] | None:
    """`{"起":"00:10","止":"00:12"}` → (10.0, 12.0)；起止读不出或止≤起 → None。"""
    if not isinstance(span, dict):
        return None
    start = clock_seconds(span.get("起"))
    end = clock_seconds(span.get("止"))
    if start is None or end is None or end < start:
        return None
    return start, end


def span_mid(span: dict | None) -> float | None:
    """时段中点（取帧点）。"""
    bounds = span_bounds(span)
    if bounds is None:
        return None
    return round((bounds[0] + bounds[1]) / 2, 2)


# ---------------------------------------------------------------- 源视频 / 时长

_DURATION: dict[str, float] = {}


def video_path(bridge_id: str) -> Path | None:
    """`.源视频指纹.json` 的「路径」（相对 wanrepro/）→ 源视频；没有就 None（现抽只能落首帧兜底）。"""
    if not bridge_id:
        return None
    fp = ANALYSIS / bridge_id / ".源视频指纹.json"
    try:
        meta = json.loads(fp.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    raw = str((meta or {}).get("路径") or "").strip()
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = ROOT / raw
    return path if path.is_file() else None


def duration(video: Path | None) -> float:
    """ffprobe 读时长（按路径缓存）；读不到 0.0——只影响中段兜底与越界保护。"""
    if video is None:
        return 0.0
    key = str(video)
    if key in _DURATION:
        return _DURATION[key]
    value = 0.0
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(video)],
            capture_output=True, timeout=30,
        )
        value = float(proc.stdout.decode("utf-8", "replace").strip().splitlines()[0])
    except (OSError, subprocess.TimeoutExpired, IndexError, ValueError):
        value = 0.0
    _DURATION[key] = value
    return value


# ---------------------------------------------------------------- 看帧缓存

def frames_dir(bridge_id: str, create: bool = False) -> Path:
    d = ANALYSIS / bridge_id / "看帧"
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def load_cache(bridge_id: str) -> list[tuple[float, Path]]:
    """看帧缓存索引 [(秒, 路径)]：看帧.json 的窗口→帧映射优先，目录里 `<秒>.png` 补齐。

    派生帧（at8-*）不算缓存——它们的时刻是为某一行算出来的，不是拆解阶段的均匀看帧。
    """
    d = frames_dir(bridge_id)
    index: dict[float, Path] = {}
    meta = ANALYSIS / bridge_id / "看帧.json"
    try:
        payload = json.loads(meta.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        payload = None
    if isinstance(payload, dict):
        for window in payload.get("窗口") or []:
            for item in (window or {}).get("帧") or []:
                if not isinstance(item, dict):
                    continue
                sec = clock_seconds(item.get("秒"))
                raw = str(item.get("文件") or "").strip()
                if sec is None or not raw:
                    continue
                path = Path(raw)
                if not path.is_absolute():
                    path = ROOT / raw
                if path.is_file():
                    index.setdefault(sec, path)
    if d.is_dir():
        for path in d.iterdir():
            if path.suffix.lower() != FRAME_SUFFIX or path.name.startswith(DERIVED_PREFIX):
                continue
            try:
                sec = clock_seconds(path.stem)
            except ValueError:
                continue
            if sec is not None and path.is_file():
                index.setdefault(sec, path)
    return sorted(index.items())


def nearest(cache: list[tuple[float, Path]], at: float) -> tuple[float, Path] | None:
    """±CACHE_TOL 内最近的一帧；没有返回 None（→ 现抽）。"""
    best, best_gap = None, CACHE_TOL + 1e-9
    for sec, path in cache:
        gap = abs(sec - at)
        if gap <= best_gap and (best is None or gap < best_gap):
            best, best_gap = (sec, path), gap
    return best


# ---------------------------------------------------------------- 抽帧

def _ffmpeg_frame(video: Path | None, at: float, out: Path) -> tuple[bool, str]:
    """从源视频现抽 1 帧到 out（最大宽 960）。返回 (成功, 失败原文)。"""
    if video is None:
        return False, "源视频定位不到（.源视频指纹.json 缺失或路径不在）"
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-v", "error", "-nostdin", "-y",
        "-ss", f"{max(0.0, at):.2f}", "-i", str(video), "-frames:v", "1",
        "-vf", f"scale=w=min({MAX_WIDTH}\\,iw):h=-2",
        str(out),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"截帧跑不起来：{exc}"
    if proc.returncode != 0 or not out.is_file() or out.stat().st_size <= 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        return False, (err[-300:] if err else f"ffmpeg 退出码 {proc.returncode}")
    return True, ""


def _ensure(video: Path | None, at: float, out: Path) -> tuple[bool, str]:
    """落盘即缓存：同一 (行, 时刻) 的文件在就不再跑 ffmpeg。"""
    if out.is_file() and out.stat().st_size > 0:
        return True, ""
    return _ffmpeg_frame(video, at, out)


def frame_url(local_id: str, filename: str) -> str:
    return f"/api/hub/frame/{urllib.parse.quote(str(local_id))}/{urllib.parse.quote(filename)}"


def build_frame(bridge_id: str, local_id: str, n: int, span: dict | None,
                cache: list[tuple[float, Path]] | None = None) -> dict:
    """一行 → 一个帧对象 {url, 秒, 来源, 定位}。

    兜底链（数据合同 v3）：中点 ±0.5s 缓存 → ffmpeg 现抽 → 现抽失败取原片首帧（「未定位」）。
    span 为空（时段解析不出）→ 整片中段帧（「未精确定位」）。
    """
    if cache is None:
        cache = load_cache(bridge_id)
    video = video_path(bridge_id)
    bounds = span_bounds(span)
    mid = span_mid(span)
    if mid is None:
        # 时段空 → 整片中段（合同 v3 兜底链第三跳）
        at = round((duration(video) or 0.0) / 2, 2)
        return _derive(bridge_id, local_id, n, video, at, False,
                       source="中段兜底", located="未精确定位")
    # 中点在时段内 ±0.5s 找到看帧缓存 → 直接用（缓存帧自身秒要落在时段内，免得取到邻镜画面）
    hit = nearest(cache, mid)
    if hit is not None:
        sec, path = hit
        if bounds is None or (bounds[0] - 1e-9 <= sec <= bounds[1] + 1e-9):
            return {"url": frame_url(local_id, path.name), "秒": sec,
                    "来源": "缓存", "定位": "精确"}
    return _derive(bridge_id, local_id, n, video, mid, True,
                   source="现抽", located="精确")


def _derive(bridge_id: str, local_id: str, n: int, video: Path | None,
            at: float, allow_fallback: bool, *, source: str, located: str) -> dict:
    """现抽/中段兜底：落 at8-<n>-<秒>.png；现抽失败退原片首帧（「未定位」）。"""
    out_dir = frames_dir(bridge_id, create=True)
    out = out_dir / f"{DERIVED_PREFIX}{n}-{at:.2f}{FRAME_SUFFIX}"
    ok, err = _ensure(video, at, out)
    if ok:
        return {"url": frame_url(local_id, out.name), "秒": at,
                "来源": source, "定位": located}
    if allow_fallback:
        first = out_dir / f"{DERIVED_PREFIX}{n}-0.00{FRAME_SUFFIX}"
        ok2, err2 = _ensure(video, 0.0, first)
        if ok2:
            return {"url": frame_url(local_id, first.name), "秒": 0.0,
                    "来源": "首帧兜底", "定位": "未定位", "错误": err[:200]}
        err = f"{err}；首帧兜底也失败：{err2}"
    # 一张图都拿不到：url 留空，前端 img onerror 出「未定位（点看原片）」
    return {"url": "", "秒": at, "来源": source if not allow_fallback else "首帧兜底",
            "定位": "未定位", "错误": err[:300]}


# ---------------------------------------------------------------- 取图（HTTP 面）

def resolve(bridge_id: str, filename: str) -> Path:
    """帧文件名 → 磁盘路径（只认 看帧/ 下的 png/jpg，防路径穿越）。"""
    name = str(filename or "").strip()
    if not SAFE_NAME_RE.match(name):
        raise FrameError(f"帧文件名不合法：{filename!r}")
    base = frames_dir(bridge_id).resolve()
    target = (base / name).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        raise FrameError(f"帧文件名不合法：{filename!r}") from None
    if not target.is_file():
        raise FrameError(f"帧不存在：{name}")
    return target
