#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""画幅归一 —— 像素尺寸 → 视频模型真正能选的画幅。

为什么只留五种（2026-11 调研，见 docs/archive/20261103/调研-画幅与分辨率-2026.md）
--------------------------------------------------------------------------
所有主流视频模型都出 16:9 与 9:16；多数还出 1:1、4:3、3:4（Seedance 2.5、
Runway Gen-4 Turbo、Wan 3.0、Kling 2.6 Pro、Hailuo H3、Creatify Boreal）；
21:9 只有少数（Hailuo H3、Runway Gen-4 Turbo）；4:5 没有任何视频模型支持。

所以：
- 提示词里的画幅只允许 16:9 / 9:16 / 1:1 / 4:3 / 3:4，源片比例不标准（例如
  1080×1888 = 135:236）就取**最接近**的一种，绝不把 135:236 这种写进提示词
  ——生成端根本没有这个选项。
- 分辨率、帧率、画质**不进提示词**：那是生成平台上的参数，由使用者自己选。
"""

from __future__ import annotations

from math import log

# 顺序即优先级无关，只按对数距离取最近
STANDARD_ASPECTS: list[tuple[tuple[int, int], str]] = [
    ((16, 9), "16:9 横屏"),
    ((9, 16), "9:16 竖屏"),
    ((1, 1), "1:1 方形"),
    ((4, 3), "4:3 横版"),
    ((3, 4), "3:4 竖版"),
]


def canonical_aspect(width: int, height: int) -> str:
    """像素尺寸 → 最接近的标准画幅名；拿不到尺寸就返回空串。"""
    try:
        width, height = int(width or 0), int(height or 0)
    except (TypeError, ValueError):
        return ""
    if width <= 0 or height <= 0:
        return ""
    actual = log(width / height)
    return min(STANDARD_ASPECTS, key=lambda kv: abs(actual - log(kv[0][0] / kv[0][1])))[1]


def canonical_ratio(width: int, height: int) -> str:
    """只要比例本身（`9:16`），不带「竖屏/横屏」。"""
    return canonical_aspect(width, height).split(" ")[0]
