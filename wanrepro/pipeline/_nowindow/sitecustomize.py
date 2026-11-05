# -*- coding: utf-8 -*-
"""Windows 无窗口补丁：链上每个 Python 进程 import 时自动执行。

背景（2026-11-05 实测）：bridge 被画布 vite 以无控制台方式拉起（autoStartBridge）后，
它再 spawn 的每个子进程（ffmpeg 抽帧 / python 分析脚本）都会各弹一个终端窗口——
打开画布一次批量弹十几个。老 web 时代 bridge 从终端手动起（有控制台），孙进程继承
控制台不弹，所以问题只在画布自动拉起形态暴露。

机制：monkeypatch subprocess.run / Popen，Windows 上默认附加 CREATE_NO_WINDOW
（进程有控制台但不可见，孙进程继承它，不再新建窗口）。调用方显式传了其它
creationflags 时做按位或合并，不覆盖。

分两路生效：
- ui/server.py 顶部直接 from nowindow import install_nowindow; install_nowindow()
- 子进程链：server.py 的 run_py 给 child_env 注 PYTHONPATH=本目录，Python 启动时
  自动 import sitecustomize → 整链（analyze/pipeline/ffmpeg）全覆盖。
"""
from __future__ import annotations

import os
import subprocess

_FLAG = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
_installed = False


def install_nowindow() -> None:
    """幂等安装：默认附加 CREATE_NO_WINDOW 到本进程的全部 subprocess 调用。"""
    global _installed
    if not _FLAG or _installed:
        return
    _installed = True

    orig_run = subprocess.run
    orig_popen = subprocess.Popen

    def _run(*args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | _FLAG
        return orig_run(*args, **kwargs)

    def _popen(*args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | _FLAG
        return orig_popen(*args, **kwargs)

    subprocess.run = _run  # type: ignore[assignment]
    subprocess.Popen = _popen  # type: ignore[assignment]


install_nowindow()
