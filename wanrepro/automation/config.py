# -*- coding: utf-8 -*-
"""automation 配置——环境变量覆盖，默认值本机可跑。

环境变量（全部可选）：
  WANREPRO_BRIDGE   bridge 基地址（默认 http://127.0.0.1:8776=直跑 ui/server.py；
                    vite autoStartBridge 拉起时是 8779——web/vite.config.ts:45。
                    端口漂移在此配置化，不散落代码）
  WANREPRO_AUTO_DIR 任务目录根（默认本包下 projects/，运行时产物不进 git）
"""
import os
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parent

BRIDGE_BASE = os.environ.get("WANREPRO_BRIDGE", "http://127.0.0.1:8776").rstrip("/")
PROJECTS_DIR = Path(os.environ.get("WANREPRO_AUTO_DIR", str(_PKG_DIR / "projects")))

# zip 内容约定（宽松）：视频恰好 1 个；资产图 0..N；意图文本 0..1（取第一个）
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".avi", ".mkv"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
TEXT_EXTS = {".txt", ".md"}


def ensure_dirs() -> None:
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
