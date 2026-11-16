#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""拆解耗时分解（S2.7）——server 侧包装层，**引擎零改动**。

为什么要有它：服务器拉片比 PC 慢，但产物只有一句「总耗时」和几个心跳阶段，看不出时间花在
抽帧还是模型调用上。本模块不碰引擎的写点：子进程起止时间戳由 bridge（ui/server.py）给，
其余信号全部来自引擎**已经**落盘的产物：

  * `进度.json`（pipeline/reverse/progress.py，覆盖式心跳 `{stage, ts}`）——
    它是**覆写**而不是追加，没有历史序列，所以这里在 job 生命周期内按 1Hz 采样，
    把「观测时刻 → 当前阶段」的序列攒起来还原各阶段窗口；
  * `frames/*.jpg` 的 mtime —— 抽帧段的起止（首帧出现 → 末帧落盘）；
  * `门禁诊断-第N轮.md` 的个数 —— 门禁重拆轮次（`门禁.json` 的「轮次」作为旁证）。

落盘产物：`analysis/reverse/<id>/耗时.json`（原子写，覆盖上一轮）。字段：

    总耗时        子进程墙钟（server 侧 job 起止，秒）
    抽帧耗时      首帧 → 末帧（frames 目录 mtime 极差，秒；不足 2 帧为 null）
    大拆解等待    「大拆解」阶段窗口宽度（拆解里唯一的长模型调用；未出现为 null）
    门禁重拆轮次  门禁诊断-第N轮.md 的个数
    阶段          各阶段 [{阶段, 开始, 相对开始, 时长}]（1Hz 采样的边界，±1s）
    归属          多账户模式下的任务归属用户名（单人模式为 ""）

零兜底（与 progress.py 同一条原则）：采集/写盘任何异常都就地吞掉，只留一行 stderr，
**绝不**影响拆解主链；文件写不出就当没有，读端按 available:false 处理。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

TIMING_NAME = "耗时.json"
OWNER_NAME = ".归属.json"          # 任务归属侧车（仅多账户模式下首占写一次）
SCHEMA = "lapian-timing/1"
POLL_SECONDS = 1.0                 # 进度.json 采样间隔：阶段边界精度 ±1s，够看「花在哪」
FRAMES_DIR = "frames"
FRAMES_FALLBACK = ("_work", "vlm_frames")
ROUND_PREFIX = "门禁诊断-第"
GATE_NAME = "门禁.json"
REQUIRED_FIELDS = ("总耗时", "抽帧耗时", "大拆解等待", "门禁重拆轮次", "阶段")

# 拆解里会调模型的阶段（用于把「大拆解等待」之外的模型等待也标出来，纯展示字段）
MODEL_STAGES = ("大拆解", "汇总", "专项判定")


# ---------------------------------------------------------------- 小工具


def iso(ts: float | None) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return ""


def write_json_atomic(path: Path, data: dict) -> bool:
    """临时文件 + os.replace：半截 JSON 不会被读端当真。失败只留一行 stderr。"""
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.tmp{os.getpid()}")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, path)
        return True
    except (OSError, ValueError, TypeError) as exc:
        print(f"WARN 耗时没写成（{getattr(path, 'name', path)}）：{exc}", file=sys.stderr)
        return False


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------- 归属侧车（多账户）


def write_owner(outdir, username: str) -> bool:
    """首占记录任务归属：只在多账户模式、且该任务还没有归属记录时写一次。

    为什么需要它：耗时数据按登录用户隔离（他人任务 404 同形），而拆解产物目录本身
    没有任何归属信息。首占不覆盖——第二个用户对同一 <id> 发起拆解也不会改写归属
    （与「他人 404 同形」配套：不泄露、也不互相篡改）。单人模式不写（零改动）。
    """
    name = str(username or "").strip()
    if not name:
        return False
    try:
        target = Path(outdir) / OWNER_NAME
        if target.is_file():
            return False
        return write_json_atomic(target, {"用户名": name, "时间": iso(time.time())})
    except OSError:
        return False


def read_owner(outdir) -> str:
    """读任务归属用户名；没有/坏文件 → 空串（读端按「不是你的」处理）。"""
    data = _read_json(Path(outdir) / OWNER_NAME)
    return str((data or {}).get("用户名") or "").strip()


# ---------------------------------------------------------------- 采集


def _read_progress(outdir: Path) -> dict | None:
    """读 `进度.json`。格式归 pipeline/reverse/progress.py，这里只读不改（不 import 引擎模块）。"""
    data = _read_json(Path(outdir) / "进度.json")
    return data if isinstance(data, dict) else None


def frame_window(outdir: Path) -> dict | None:
    """抽帧段的 mtime 极差：{帧数, 首帧, 末帧, 开始, 结束, 耗时, 来源}。"""
    base = Path(outdir)
    dirs = [base / FRAMES_DIR, base.joinpath(*FRAMES_FALLBACK)]
    for d in dirs:
        try:
            if not d.is_dir():
                continue
            stamps = [p.stat().st_mtime for p in d.iterdir()
                      if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg", ".png")]
        except OSError:
            continue
        if not stamps:
            continue
        first, last = min(stamps), max(stamps)
        return {
            "帧数": len(stamps),
            "首帧": iso(first),
            "末帧": iso(last),
            "开始": iso(first),
            "结束": iso(last),
            "耗时": round(max(0.0, last - first), 1),
            "来源": d.name if d.parent == base else f"{d.parent.name}/{d.name}",
        }
    return None


def gate_rounds(outdir: Path) -> tuple[int, dict]:
    """门禁重拆轮次 = `门禁诊断-第N轮.md` 的个数；旁证取 `门禁.json` 的「轮次」。"""
    base = Path(outdir)
    rounds = 0
    try:
        for p in base.iterdir():
            name = p.name
            if p.is_file() and name.startswith(ROUND_PREFIX) and name.endswith("轮.md"):
                rounds += 1
    except OSError:
        pass
    gate = _read_json(base / GATE_NAME) or {}
    recorded = 0
    try:
        recorded = int(gate.get("轮次") or 0)
    except (TypeError, ValueError):
        recorded = 0
    return rounds, {"记录轮次": recorded, "记录状态": str(gate.get("状态") or "")}


def stage_windows(samples: list[tuple[float, str]], ended: float) -> list[dict]:
    """采样序列 → 阶段窗口（相邻同阶段合并；窗口右界 = 下一条采样的观测时刻，末段到 ended）。"""
    marks: list[tuple[float, str]] = []
    for ts, stage in samples:
        name = str(stage or "").strip()
        if not name:
            continue
        if marks and marks[-1][1] == name:
            continue
        marks.append((float(ts), name))
    windows: list[dict] = []
    for index, (ts, name) in enumerate(marks):
        end = marks[index + 1][0] if index + 1 < len(marks) else float(ended)
        windows.append({
            "阶段": name,
            "开始": iso(ts),
            "相对开始": round(max(0.0, ts - marks[0][0]), 1) if marks else 0.0,
            "时长": round(max(0.0, end - ts), 1),
            "起": round(max(0.0, ts - marks[0][0]), 1),
            "止": round(max(0.0, end - marks[0][0]), 1),
        })
    return windows


def _stage_seconds(windows: list[dict], name: str) -> float | None:
    total = [w["时长"] for w in windows if str(w["阶段"]) == name]
    return round(sum(total), 1) if total else None


class TimingRecorder:
    """一次拆解的耗时采集器：attach(任务目录) 起采样线程，finish() 收尾写盘。

    任何方法都不抛（除 attach 传错参数，那也吞掉）——监控绝不参与拆解主链的成败判定。
    """

    def __init__(self, started: float | None = None) -> None:
        self.started = float(started if started is not None else time.time())
        self.ended: float | None = None
        self.outdir: Path | None = None
        self.samples: list[tuple[float, str]] = []
        self.exit_code: int | None = None
        self.owner = ""
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    # -- 生命周期 --------------------------------------------------
    def attach(self, outdir, owner: str = "") -> None:
        try:
            self.outdir = Path(outdir)
            self.owner = str(owner or "")
            self._sample()
            thread = threading.Thread(target=self._loop, daemon=True, name="lapian-timing")
            self._thread = thread
            thread.start()
        except Exception:                      # noqa: BLE001 —— 采样起不来就只是没有分解
            self.outdir = None

    def _loop(self) -> None:
        while not self._stop.wait(POLL_SECONDS):
            try:
                self._sample()
            except Exception:                  # noqa: BLE001
                continue

    def _sample(self) -> None:
        if self.outdir is None:
            return
        rec = _read_progress(self.outdir)
        stage = str((rec or {}).get("stage") or "").strip()
        if not stage:
            return
        with self._lock:
            self.samples.append((time.time(), stage))

    def set_exit(self, code) -> None:
        try:
            self.exit_code = int(code)
        except (TypeError, ValueError):
            self.exit_code = None

    def stop(self) -> None:
        self._stop.set()

    # -- 汇总 ------------------------------------------------------
    def payload(self, error: str = "") -> dict:
        ended = float(self.ended or time.time())
        with self._lock:
            samples = list(self.samples)
        wall = round(max(0.0, ended - self.started), 1)
        windows = stage_windows(samples, ended)
        frames = frame_window(self.outdir) if self.outdir is not None else None
        rounds, gate = gate_rounds(self.outdir) if self.outdir is not None else (0, {})
        covered = round(sum(w["时长"] for w in windows), 1)
        payload: dict = {
            "schema": SCHEMA,
            "任务": self.outdir.name if self.outdir is not None else "",
            "归属": self.owner,
            "开始": iso(self.started),
            "结束": iso(ended),
            "总耗时": wall,
            "抽帧耗时": frames["耗时"] if frames else None,
            "大拆解等待": _stage_seconds(windows, "大拆解"),
            "模型等待": round(sum(_stage_seconds(windows, s) or 0.0 for s in MODEL_STAGES), 1) or None,
            "门禁重拆轮次": int(rounds),
            "门禁": gate,
            "抽帧": frames,
            "阶段": windows,
            "阶段外耗时": round(max(0.0, wall - covered), 1),
            "采集": {
                "方式": "server 侧包装（子进程起止 + 1Hz 采样 进度.json）",
                "间隔": POLL_SECONDS,
                "样本数": len(samples),
            },
            "退出码": self.exit_code,
        }
        if error:
            payload["错误"] = str(error)[:500]
        return payload

    def finish(self, error: str = "") -> dict | None:
        """收尾：停采样 → 汇总 → 写 `耗时.json`。返回 payload（写不出也返回，供 /api/job 用）。"""
        try:
            self.stop()
            self.ended = time.time()
            try:
                self._sample()
            except Exception:                  # noqa: BLE001
                pass
            payload = self.payload(error)
            if self.outdir is not None:
                write_json_atomic(self.outdir / TIMING_NAME, payload)
            return payload
        except Exception as exc:               # noqa: BLE001 —— 监控失败不算拆解失败
            print(f"WARN 耗时汇总没做成：{exc}", file=sys.stderr)
            return None


# ---------------------------------------------------------------- 读端


def load(outdir) -> tuple[dict | None, list[str]]:
    """读 `耗时.json` → （数据, 缺失/损坏字段）。没有文件 → (None, ["耗时.json"]）。"""
    base = Path(outdir)
    path = base / TIMING_NAME
    if not path.is_file():
        return None, [TIMING_NAME]
    data = _read_json(path)
    if data is None:
        return None, [f"{TIMING_NAME}（不是合法 JSON 对象）"]
    missing = [key for key in REQUIRED_FIELDS if key not in data]
    if missing:
        return data, missing
    # 形状校验：总耗时必须是数、阶段必须是列表（类型坏了同样算「不可用」，不当 500 抛）
    bad: list[str] = []
    if not isinstance(data.get("总耗时"), (int, float)):
        bad.append("总耗时")
    if not isinstance(data.get("阶段"), list):
        bad.append("阶段")
    if not isinstance(data.get("门禁重拆轮次"), int):
        bad.append("门禁重拆轮次")
    return data, bad


def view(outdir) -> dict:
    """端点用：可用 → {available:True, ...文件内容}；不可用 → {available:False, reason, missing}。"""
    data, missing = load(outdir)
    if missing:
        return {"available": False,
                "reason": "耗时.json 还没生成或字段不全（拆解可能没跑完 / 采集失败）",
                "missing": missing}
    out = dict(data or {})
    out["available"] = True
    return out
