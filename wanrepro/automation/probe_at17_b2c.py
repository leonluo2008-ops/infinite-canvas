#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AT-17 B2c 探针：意图层音频三态落盘（audio_mode 旁路 ITEM_RE / 空意图也写默认值 / hub 任务.json）。

跑法（`wanrepro/` 下，**纯离线零花钱**：不调模型、不跑 ffmpeg、不打真实 bridge）：
  python -m automation.probe_at17_b2c              # 全部判据
  python -m automation.probe_at17_b2c --seed-demo  # 只重种三态演示任务 analysis/reverse/at17-demo-*
判据全对才 exit 0；任何一条 FAIL 如实打印并 exit 1（判 returncode，禁子串判通过）。

判据：
  [1] 三态默认推导（B0②）：有 BGM → 按新主体调整；无 BGM → 沿用原片；无音轨 → 不要；
      老散文产物（无结构化字段）→ 沿用原片 + 待核对（不猜成无BGM）；audio.json music_likely 兜底。
  [2] 枚举/字段全链一套：pipeline/reverse/intent.py、ui/server.py、ui/hub_api.py 三处字面一致。
  [3] 空意图也落默认值：intent.build 空分支、analyze.stage_intent 空分支都落 audio_mode.json；
      删旧建议文件（意图建议.md）≠ 删 audio_mode.json。
  [4] ITEM_RE 合同不破：parse_suggestions 归一化产物逐字不变；写进三节正文的 audio_mode 行会被
      归一化丢弃（这就是走旁路的原因）；apply_suggestions 回填第 7 列不受影响。
  [5] hub 链落盘：隔离任务（自建自删）走真 discuss 流程 → 任务.json 出现 audio_mode 且值正确；
      已有值不被覆盖；hub_api.get_task 透出 audio_mode 四键。
  [6] 画布链读面：server.task_audio_mode 读 audio_mode.json；坏文件/枚举外值 → None（不猜）。
  [7] 演示任务留档：analysis/reverse/at17-demo-*（三态各一条，--seed-demo 可重种）值正确。
  [8] 真实任务只读（auto-0006/0043 任务.json SHA256 不变）+ 跑后零残留。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import shutil
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]          # wanrepro/
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ui"))                 # hub_api / server 在 ui/

from automation import cards, config, discuss, frames, replace, state  # noqa: E402

import hub_api   # noqa: E402
import server    # noqa: E402 —— 画布链读面（task_audio_mode / 常量）

ANALYSIS = ROOT / "analysis" / "reverse"
FIXTURE = ROOT / "tests" / "fixtures" / "at15-r1"
REAL_DISC = ANALYSIS / "1-34-2" / "拆解.md"          # 真 v2.6 结构化拆解（有 BGM，夹具底本）
LEGACY_DISC = ANALYSIS / "1" / "拆解.md"            # 真 v2.5 自由散文八声音（老产物只读样本）
DEMO_PREFIX = "at17-demo-"
REAL_TASKS = (ROOT / "automation" / "projects" / "auto-0006" / "任务.json",
              ROOT / "automation" / "projects" / "auto-0043" / "任务.json")

WORK = Path(tempfile.mkdtemp(prefix="probe-at17-b2c-"))
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f" —— {detail}" if detail and not cond else ""))
    return bool(cond)


def module_by_path(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


INTENT = module_by_path("probe_b2c_intent", ROOT / "pipeline" / "reverse" / "intent.py")
ANALYZE = module_by_path("probe_b2c_analyze", ROOT / "analyze.py")


# ---------------------------------------------------------------- 夹具（B1 形态）

def base_markdown() -> str:
    return (ROOT / "tests" / "fixtures" / "at15-r1" / "拆解.md").read_text(encoding="utf-8-sig")


def with_sound_body(markdown: str, body: str, audio: str = "有") -> str:
    """把「八 声音」正文整体换掉，并同步头部音轨标记（与 probe_at17_b1 同款）。"""
    out: list[str] = []
    inside = False
    for line in markdown.splitlines():
        match = re.match(r"^#{1,6}\s*(.+?)\s*$", line)
        if match:
            inside = match.group(1).strip() == "八 声音"
            out.append(line)
            if inside and body:
                out.append(body)
            continue
        if inside:
            continue
        out.append(line)
    text = "\n".join(out) + "\n"
    return re.sub(r"音轨:\s*(有|无)", f"音轨: {audio}", text, count=1)


SOUND_BGM = "\n".join((
    "- BGM.情绪：轻快",
    "- BGM.节奏：中速，末段渐弱",
    "- BGM.乐器/织体：木琴、轻柔钢琴、软合成音",
    "- BGM.强弱走向：轻起渐强收",
    "- 音效点：00:01-00:02 盖子拧开的一声「咔」",
    "- 人声语言：无",
    "- 台词：无",
))
SOUND_NO_BGM = "\n".join((
    "- BGM.情绪：无BGM",
    "- BGM.节奏：无BGM",
    "- BGM.乐器/织体：无BGM",
    "- BGM.强弱走向：无BGM",
    "- 音效点：00:02-00:03 布料摩擦声",
    "- 人声语言：无",
    "- 台词：无",
))


def disc_bgm() -> str:
    return with_sound_body(base_markdown(), SOUND_BGM, audio="有")


def disc_no_bgm() -> str:
    return with_sound_body(base_markdown(), SOUND_NO_BGM, audio="有")


def disc_no_audio() -> str:
    return with_sound_body(base_markdown(), "无音轨", audio="无")


# ---------------------------------------------------------------- [1] 三态推导

def probe_derive() -> None:
    print("[1] 三态默认推导（有BGM / 无BGM / 无音轨 / 老产物 / music_likely 兜底）")
    info = INTENT.audio_mode(disc_bgm())
    check("有 BGM 片 → 按新主体调整（依据带 BGM.情绪 实值）",
          info["audio_mode"] == "按新主体调整" and "BGM.情绪=轻快" in info["依据"]
          and info["待核对"] is False, f"{info}")

    info = INTENT.audio_mode(disc_no_bgm())
    check("无 BGM 片（四字段写 无BGM）→ 沿用原片",
          info["audio_mode"] == "沿用原片" and info["待核对"] is False, f"{info}")

    info = INTENT.audio_mode(disc_no_audio())
    check("无音轨片（头部 音轨: 无 + 正文 无音轨）→ 不要",
          info["audio_mode"] == "不要" and info["待核对"] is False, f"{info}")

    info = INTENT.audio_mode(with_sound_body(base_markdown(), "无音轨", audio="有"))
    check("正文只剩「无音轨」但头部写有音轨 → 仍判 不要（正文是机器可读形态）",
          info["audio_mode"] == "不要", f"{info}")

    info = INTENT.audio_mode(with_sound_body(base_markdown(), SOUND_BGM, audio="无"))
    check("头部 音轨: 无（模型仍写了 BGM）→ 不要（音轨标记优先，不采信编出来的 BGM）",
          info["audio_mode"] == "不要", f"{info}")

    legacy = LEGACY_DISC.read_text(encoding="utf-8-sig")   # 真 v2.5 自由散文产物（只读）
    check("老散文产物（无结构化 BGM 字段）确实没有 BGM.情绪 锚点",
          "BGM.情绪" not in legacy, "该样本已成为结构化产物，换样本")
    info = INTENT.audio_mode(legacy)
    check("老产物 → 沿用原片 + 待核对（不把「不知道」写成 无BGM）",
          info["audio_mode"] == "沿用原片" and info["待核对"] is True, f"{info}")

    info = INTENT.audio_mode(legacy, {"music_likely": True})
    check("结构化缺失 + audio.json music_likely=true → 按新主体调整",
          info["audio_mode"] == "按新主体调整" and info["待核对"] is False, f"{info}")
    info = INTENT.audio_mode(legacy, {"music_likely": False})
    check("结构化缺失 + music_likely=false → 沿用原片（不当成 无BGM 声明）",
          info["audio_mode"] == "沿用原片" and "music_likely=false" in info["依据"], f"{info}")

    values = {INTENT.audio_mode(text)["audio_mode"]
              for text in (disc_bgm(), disc_no_bgm(), disc_no_audio())}
    check("三态枚举值就是 沿用原片 / 按新主体调整 / 不要（无第四种）",
          values == {"沿用原片", "按新主体调整", "不要"}, f"{values}")


# ---------------------------------------------------------------- [2] 命名一致

def probe_naming() -> None:
    print("[2] 字段名/枚举值/文件名全链一套（B0 附注 4）")
    check("intent 侧枚举 == server 侧枚举",
          tuple(INTENT.AUDIO_MODE_VALUES) == tuple(server.AUDIO_MODE_VALUES)
          == ("沿用原片", "按新主体调整", "不要"),
          f"{INTENT.AUDIO_MODE_VALUES} / {server.AUDIO_MODE_VALUES}")
    check("三处字段名恒为 audio_mode",
          INTENT.AUDIO_MODE_FIELD == server.AUDIO_MODE_FIELD == hub_api.AUDIO_MODE_FIELD
          == "audio_mode")
    check("落盘文件名恒为 audio_mode.json",
          INTENT.AUDIO_MODE_FILE == server.AUDIO_MODE_NAME == "audio_mode.json")
    check("没有另起别名（磁盘/任务.json 里不出现 音频三态 作为机器字段名）",
          not any("音频三态" in name for name in (INTENT.AUDIO_MODE_FILE, server.AUDIO_MODE_NAME)))


# ---------------------------------------------------------------- [3] 空意图落盘

def probe_empty_intent() -> None:
    print("[3] 空意图也落默认值；删旧建议文件 ≠ 删 audio_mode")
    outdir = WORK / "empty-intent"
    outdir.mkdir(parents=True, exist_ok=True)
    disc_path = outdir / "拆解.md"
    disc_path.write_text(disc_bgm(), encoding="utf-8")
    checklist_src = FIXTURE / "待确认清单.md"
    checklist = outdir / "待确认清单.md"
    shutil.copy2(checklist_src, checklist)
    (outdir / "意图建议.md").write_text("# 上一轮的旧建议\n\n## 必须换\n- 1｜理由：旧｜镜次：1\n",
                                        encoding="utf-8")

    text = INTENT.build(disc_path.read_text(encoding="utf-8"),
                        checklist.read_text(encoding="utf-8"), "", outdir=outdir)
    mode = INTENT.read_audio_mode(outdir)
    check("intent.build 空意图分支返回空建议文本（不调模型）", text == "", repr(text[:40]))
    check("intent.build 空意图分支落 audio_mode.json 且值正确",
          mode.get("audio_mode") == "按新主体调整", f"{mode}")

    code, action = ANALYZE.stage_intent(disc_path, checklist, outdir, "")
    check("analyze.stage_intent 空意图分支返回 清理/成功", code == 0 and action == "清理",
          f"{code}｜{action}")
    check("空意图把旧建议文件删掉（意图建议.md 不在）",
          not (outdir / "意图建议.md").is_file())
    mode = INTENT.read_audio_mode(outdir)
    check("删建议文件不影响 audio_mode.json（仍在且值仍是 按新主体调整）",
          mode.get("audio_mode") == "按新主体调整", f"{mode}")

    checklist_after = checklist.read_text(encoding="utf-8")
    filled = [line for line in checklist_after.splitlines()
              if line.startswith("|") and not line.startswith("| #") and not line.startswith("|---")]
    check("空意图把清单建议列清空（机器拥有列不留上一轮值）",
          all(cells[6].strip() == ""
              for cells in ([c.strip() for c in line.strip().strip("|").split("|")]
                            for line in filled) if len(cells) >= 8 and cells[0].isdigit()),
          "建议列仍有残留")

    # 无音轨片走同一条空意图路径 → 不要
    outdir2 = WORK / "empty-intent-noaudio"
    outdir2.mkdir(parents=True, exist_ok=True)
    disc2 = outdir2 / "拆解.md"
    disc2.write_text(disc_no_audio(), encoding="utf-8")
    shutil.copy2(checklist_src, outdir2 / "待确认清单.md")
    ANALYZE.stage_intent(disc2, outdir2 / "待确认清单.md", outdir2, "")
    check("无音轨片空意图 → audio_mode=不要",
          INTENT.read_audio_mode(outdir2).get("audio_mode") == "不要",
          f"{INTENT.read_audio_mode(outdir2)}")


# ---------------------------------------------------------------- [4] ITEM_RE 合同

def probe_item_contract() -> None:
    print("[4] ITEM_RE 合同不破：归一化逐字不变；写进正文的 audio_mode 行必被丢弃")
    disc = disc_bgm()
    checklist = (FIXTURE / "待确认清单.md").read_text(encoding="utf-8-sig")
    rows = [row["row"] for row in INTENT.checklist_rows(checklist)]
    shots = sorted(INTENT.valid_shot_numbers(disc))
    check("夹具清单有行号、拆解能读出镜号（回归断言的前提）",
          len(rows) >= 1 and len(shots) >= 2, f"rows={rows[:5]} shots={shots[:5]}")
    row, shot_a, shot_b = rows[0], shots[0], shots[1]

    raw = (f"## 必须换\n- {row}｜理由：探针理由｜镜次：{shot_a}, {shot_b}\n\n"
           "## 建议准备\n\n## 可沿用\n")
    golden = (f"## 必须换\n- {row}｜理由：探针理由｜镜次：{shot_a}, {shot_b}\n\n"
              "## 建议准备\n\n## 可沿用\n")
    normalized, inv, dup, bad = INTENT.parse_suggestions(raw, disc, checklist)
    check("既有三节建议归一化产物逐字不变（ITEM_RE 合同未动）", normalized == golden,
          repr(normalized))
    check("无效行号/重复行号/无效镜次计数仍为 0", (inv, dup, bad) == (0, 0, 0),
          f"{inv}/{dup}/{bad}")

    dirty = raw.replace("## 必须换\n", f"## 必须换\naudio_mode：按新主体调整\n")
    normalized2, _i, _d, _b = INTENT.parse_suggestions(dirty, disc, checklist)
    check("把 audio_mode 写进三节正文 → 归一化后彻底消失（这就是必须走旁路的原因）",
          "audio_mode" not in normalized2 and "按新主体调整" not in normalized2,
          repr(normalized2[:80]))
    check("插了脏行也不影响正行（行号/理由/镜次照旧）", normalized2 == golden,
          repr(normalized2))

    work_check = WORK / "item-checklist.md"
    shutil.copy2(FIXTURE / "待确认清单.md", work_check)
    filled = INTENT.apply_suggestions(work_check, normalized)
    line = next(line for line in work_check.read_text(encoding="utf-8").splitlines()
                if line.startswith(f"| {row} |"))
    check("apply_suggestions 仍按行号回填第 7 列", filled >= 1
          and [c.strip() for c in line.strip().strip("|").split("|")][6] == "必须换",
          f"{filled}｜{line[:80]}")

    items = hub_api._suggestions(normalized, checklist, disc)
    hit = next((item for item in items if item["n"] == row), None)
    check("hub 建议透出（AT-9 B1 合同）逐字段不变",
          hit is not None and hit["档位"] == "必须换" and hit["理由"] == "探针理由"
          and hit["镜次"] == [shot_a, shot_b] and hit["待核对"] is False, f"{hit}")
    check("建议透出里没有音频三态字段（三态走独立通道，不混进建议行）",
          all("audio_mode" not in json.dumps(item, ensure_ascii=False) for item in items))


# ---------------------------------------------------------------- [5] hub 链落盘

class BridgeStub:
    """进程内假 bridge：拆解接口三面（run / job / tasks）。

    /api/tasks 的 has_* 只在「已提交过 lapian/run」之后才报 True——这样 run_discuss 会走
    「真提交 → 轮询 done → 核产物」这条路，而不是一上来就判产物已齐。
    """

    JOB = "abcdef01"

    def __init__(self) -> None:
        self.started = False
        self.calls: list[str] = []
        self.lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                return

            def _send(self, code: int, payload: dict) -> None:
                raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):        # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                with outer.lock:
                    outer.calls.append(f"POST {self.path}")
                if self.path == "/api/lapian/run":
                    with outer.lock:
                        outer.started = True
                    self._send(200, {"job": outer.JOB})
                    return
                self._send(404, {"error": "stub bridge 只认 /api/lapian/run"})

            def do_GET(self):         # noqa: N802
                with outer.lock:
                    outer.calls.append(f"GET {self.path}")
                if self.path == "/api/tasks":
                    with outer.lock:
                        ready = outer.started
                    self._send(200, {"tasks": [{"id": outer.bridge_id, "has_disc": ready,
                                                "has_check": ready, "has_prompt": ready}]})
                    return
                if self.path == f"/api/job/{outer.JOB}":
                    self._send(200, {"status": "done", "elapsed": 1.2})
                    return
                self._send(404, {"error": "stub bridge 不认这个路径"})

        self.port = _free_port()
        self.bridge_id = ""
        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def _free_port() -> int:
    import socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def seed_hub_task(projects: Path, analysis: Path, bridge_id: str, local_id: str,
                  disc_text: str) -> Path:
    """在隔离根里种一条 ingested 任务 + 拆解产物（不碰真实 projects/analysis）。"""
    task_dir = projects / local_id
    (task_dir / "inbox").mkdir(parents=True, exist_ok=True)
    task = {
        "local_id": local_id, "bridge_id": bridge_id,
        "created_at": "2026-01-01T00:00:00+08:00", "state": "ingested",
        "history": [{"at": "2026-01-01T00:00:00+08:00", "from": None, "to": "ingested",
                     "note": "probe-at17-b2c 夹具"}],
        "intent": "AT-17 B2c 音频三态探针夹具", "assets": [],
        "ingestion": {"zip_sha256": f"probe-{local_id}", "zip_name": "probe",
                      "video_name": "", "text_name": "", "ignored": []},
    }
    (task_dir / "任务.json").write_text(json.dumps(task, ensure_ascii=False, indent=2),
                                        encoding="utf-8")
    bridge_dir = analysis / bridge_id
    bridge_dir.mkdir(parents=True, exist_ok=True)
    (bridge_dir / "拆解.md").write_text(disc_text, encoding="utf-8")
    shutil.copy2(FIXTURE / "待确认清单.md", bridge_dir / "待确认清单.md")
    (bridge_dir / "提示词-lumi-4section.md").write_text("# 提示词\n探针占位\n", encoding="utf-8")
    return task_dir


def probe_hub_chain() -> None:
    print("[5] hub 链落盘：隔离任务走真 discuss 流程 → 任务.json 有 audio_mode；get_task 透出")
    real_dirs_before = {p.name for p in ANALYSIS.iterdir() if p.is_dir()}
    iso = WORK / "hub"
    projects, analysis = iso / "projects", iso / "analysis" / "reverse"
    projects.mkdir(parents=True, exist_ok=True)
    analysis.mkdir(parents=True, exist_ok=True)
    bridge_id, local_id = "probe-b2c-bridge", "auto-probe17c"
    stub = BridgeStub()
    stub.bridge_id = bridge_id
    task_dir = seed_hub_task(projects, analysis, bridge_id, local_id, disc_bgm())

    patches = [
        patch.object(config, "BRIDGE_BASE", stub.base),
        patch.object(discuss, "_ANALYSIS_ROOT", analysis),
        patch.object(hub_api, "TASKS_DIR", projects),
        patch.object(hub_api, "ROOT", iso),
        patch.object(cards, "ANALYSIS", analysis),
        # frames 自己有一份 ANALYSIS 常量：cards.build 会顺手 ensure 看帧目录——不换根就会
        # 在真实 analysis/reverse 下留一个空壳目录（实测过，AT-14 B2 同类坑）
        patch.object(frames, "ANALYSIS", analysis),
        patch.object(replace, "checklist_path",
                     lambda bid: analysis / bid / "待确认清单.md"),
        patch.object(replace, "sidecar_path", lambda bid: analysis / bid / "待确认清单.json"),
        patch.object(server, "ANALYSIS", analysis),
    ]
    started = [p.start() for p in patches]
    try:
        stub.thread.start()
        job = hub_api.start_discuss(local_id)
        check("hub 拆解任务真的起了后台 job", bool(job.get("job_id")), f"{job}")
        state_name, waited = "", 0.0
        while waited < 60:
            with hub_api._TASK_LOCKS_GUARD:
                pass
            records = list(hub_api.HUB_JOBS.values())
            rec = next((r for r in records if r.get("local_id") == local_id), None)
            if rec and rec.get("status") != "running":
                break
            time.sleep(0.3)
            waited += 0.3
        task = state.load(task_dir)
        state_name = task["state"]
        check("拆解走完（ingested → awaiting_assets）", state_name == "awaiting_assets",
              f"{state_name}（等 {waited:.1f}s）")
        check("任务.json 落出 audio_mode 且值正确（有 BGM → 按新主体调整）",
              task.get("audio_mode") == "按新主体调整", f"{task.get('audio_mode')!r}")
        log = (task_dir / "拆解日志.txt").read_text(encoding="utf-8") \
            if (task_dir / "拆解日志.txt").is_file() else ""
        check("拆解日志留了一行三态（人能看见怎么来的）",
              "音频三态：按新主体调整" in log, log[-200:])
        side = INTENT.read_audio_mode(analysis / bridge_id)
        check("hub 链同一份推导也落了画布链旁路（audio_mode.json 在桥任务目录）",
              side.get("audio_mode") == "按新主体调整" and "BGM.情绪" in side.get("依据", ""),
              f"{side}")

        detail = hub_api.get_task(local_id)
        check("hub_api.get_task 透出 audio_mode", detail.get("audio_mode") == "按新主体调整",
              f"{detail.get('audio_mode')!r}")
        check("透出 audio_mode_来源 = 任务.json（值来自落盘字段）",
              detail.get("audio_mode_来源") == "任务.json", f"{detail.get('audio_mode_来源')!r}")
        check("透出 audio_mode_待核对=false、依据带 BGM.情绪",
              detail.get("audio_mode_待核对") is False
              and "BGM.情绪" in str(detail.get("audio_mode_依据") or ""),
              f"{detail.get('audio_mode_依据')!r}")
        check("四个透出键都在（值/依据/待核对/来源）",
              {"audio_mode", "audio_mode_依据", "audio_mode_待核对", "audio_mode_来源"}
              <= set(detail))

        # 已有值不覆盖（B3 界面改选留得住）
        task["audio_mode"] = "不要"
        state.save(task_dir, task)
        again = hub_api._persist_audio_mode(task_dir)
        check("已有 audio_mode 不被重跑覆盖（用户改选留得住）",
              "未覆盖" in str(again.get("why") or "")
              and state.load(task_dir)["audio_mode"] == "不要",
              f"{again}｜{state.load(task_dir)['audio_mode']!r}")

        # 无音轨任务的同一路：值应为 不要（另一条隔离任务，自建自删）
        task_dir2 = seed_hub_task(projects, analysis, "probe-b2c-bridge2", "auto-probe17d",
                                  disc_no_audio())
        info = discuss.persist_audio_mode(task_dir2)
        check("无音轨片落盘 = 不要（同一份推导、hub 侧同样生效）",
              info.get("audio_mode") == "不要", f"{info}")

        # 读不到拆解时：不写、不猜值（如实报）
        task_dir3 = seed_hub_task(projects, analysis, "probe-b2c-bridge3", "auto-probe17e",
                                  disc_bgm())
        (analysis / "probe-b2c-bridge3" / "拆解.md").unlink()
        info = discuss.persist_audio_mode(task_dir3)
        check("读不到拆解 → 不落值、如实给原因（禁编值）",
              info.get("ok") is False and "audio_mode" not in info, f"{info}")
        check("此时任务.json 里没有 audio_mode 字段",
              "audio_mode" not in state.load(task_dir3), f"{state.load(task_dir3).keys()}")
    finally:
        stub.httpd.shutdown()
        stub.httpd.server_close()
        for p in reversed(patches):
            p.stop()
        real_dirs_after = {p.name for p in ANALYSIS.iterdir() if p.is_dir()}
        check("隔离跑不动真实 analysis 根（目录集一格不多不少）",
              real_dirs_after == real_dirs_before,
              f"多了：{sorted(real_dirs_after - real_dirs_before)}｜少了：{sorted(real_dirs_before - real_dirs_after)}")


# ---------------------------------------------------------------- [6] 画布链读面

def probe_canvas_read() -> None:
    print("[6] 画布链读面 server.task_audio_mode：只读、坏文件/枚举外值不猜")
    outdir = ANALYSIS / f"{DEMO_PREFIX}b2c-read"       # 探针自建自删（真根下，走 task_dir 合同）
    try:
        with patch.object(server, "ANALYSIS", WORK / "canvas"):
            target = WORK / "canvas" / "probe-b2c-read"
            target.mkdir(parents=True, exist_ok=True)
            check("没有 audio_mode.json → None（页面据此不显示）",
                  server.task_audio_mode("probe-b2c-read") is None)
            (target / "audio_mode.json").write_text(json.dumps(
                {"audio_mode": "不要", "依据": "拆解标记无音轨", "待核对": False},
                ensure_ascii=False), encoding="utf-8")
            got = server.task_audio_mode("probe-b2c-read")
            check("正常文件读回 {audio_mode, 依据, 待核对}",
                  got == {"audio_mode": "不要", "依据": "拆解标记无音轨", "待核对": False},
                  f"{got}")
            (target / "audio_mode.json").write_text(
                json.dumps({"audio_mode": "混合"}, ensure_ascii=False), encoding="utf-8")
            check("枚举外的值 → None（不把脏值透给界面）",
                  server.task_audio_mode("probe-b2c-read") is None)
            (target / "audio_mode.json").write_text("{坏 json", encoding="utf-8")
            check("坏文件 → None（一个坏文件不该把任务页打成 500）",
                  server.task_audio_mode("probe-b2c-read") is None)
    finally:
        shutil.rmtree(outdir, ignore_errors=True)


# ---------------------------------------------------------------- [7] 演示留档

DEMOS = (
    (f"{DEMO_PREFIX}b2c-bgm", "有BGM→按新主体调整", "按新主体调整"),
    (f"{DEMO_PREFIX}b2c-nobgm", "无BGM→沿用原片", "沿用原片"),
    (f"{DEMO_PREFIX}b2c-noaudio", "无音轨→不要", "不要"),
)


def seat_demos() -> list[str]:
    """种/重种三态演示任务（真意图阶段空分支，零模型调用；产物留档不删，供所有者界面查验）。"""
    base = REAL_DISC.read_text(encoding="utf-8-sig")
    bodies = {"bgm": (SOUND_BGM, "有"), "nobgm": (SOUND_NO_BGM, "有"),
              "noaudio": ("无音轨", "无")}
    seeded = []
    for name, _label, _value in DEMOS:
        key = name.rsplit("-", 1)[-1]
        body, audio = bodies[key]
        outdir = ANALYSIS / name
        outdir.mkdir(parents=True, exist_ok=True)
        disc = outdir / "拆解.md"
        disc.write_text(with_sound_body(base, body, audio=audio), encoding="utf-8")
        checklist = outdir / "待确认清单.md"
        shutil.copy2(ANALYSIS / "1-34-2" / "待确认清单.md", checklist)
        (outdir / "意图.txt").write_text("", encoding="utf-8")
        code, action = ANALYZE.stage_intent(disc, checklist, outdir, "")
        print(f"    [种] {name}：stage_intent={code}/{action} → "
              f"{INTENT.read_audio_mode(outdir).get('audio_mode')}")
        seeded.append(name)
    return seeded


def probe_demos() -> None:
    print("[7] 演示任务留档（at17-demo-*，三态各一条，供所有者界面查验）")
    for name, label, expect in DEMOS:
        outdir = ANALYSIS / name
        info = INTENT.read_audio_mode(outdir)
        check(f"演示任务 {name} 在盘且有 audio_mode（{label}）",
              outdir.is_dir() and info.get("audio_mode") == expect,
              f"{info}｜缺则跑 --seed-demo 重种")
        check(f"演示任务 {name} 的 audio_mode.json 与 拆解.md 同目录（画布链节点状态）",
              (outdir / "拆解.md").is_file())
    demos = sorted(p.name for p in ANALYSIS.glob(f"{DEMO_PREFIX}*"))
    print(f"    留档演示任务：{demos}")


# ---------------------------------------------------------------- [8] 只读与残留

def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def probe_readonly(before: dict[str, str]) -> None:
    print("[8] 真实任务只读 + 零残留")
    after = {str(path): sha256(path) for path in REAL_TASKS if path.is_file()}
    changed = [path for path, digest in before.items() if after.get(path) != digest]
    check("真实任务只读（auto-0006/0043 任务.json SHA256 不变）", not changed, f"变了：{changed}")


def main() -> int:
    print("probe_at17_b2c · 意图层音频三态落盘（离线零花钱）")
    before = {str(path): sha256(path) for path in REAL_TASKS if path.is_file()}
    try:
        probe_derive()
        probe_naming()
        probe_empty_intent()
        probe_item_contract()
        probe_hub_chain()
        probe_canvas_read()
        probe_demos()
        probe_readonly(before)
    finally:
        shutil.rmtree(WORK, ignore_errors=True)
        check("跑后零残留（临时夹具根已删）", not WORK.exists(), str(WORK))
    passed = sum(1 for _n, ok, _d in RESULTS if ok)
    fails = [n for n, ok, _d in RESULTS if not ok]
    print(f"\nprobe_at17_b2c: {passed}/{len(RESULTS)} PASS"
          + (f"｜FAIL：{'；'.join(fails)}" if fails else ""))
    return 0 if not fails else 1


if __name__ == "__main__":
    if "--seed-demo" in sys.argv:
        print("probe_at17_b2c · 重种三态演示任务（留档不删）")
        for name in seat_demos():
            print(f"    {name} → {INTENT.read_audio_mode(ANALYSIS / name)}")
        raise SystemExit(0)
    raise SystemExit(main())
