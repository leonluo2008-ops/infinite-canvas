#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地 Web UI：选视频 → 看拆解 → 改命名/传图（自动识别）→ 选模板 → 拿提示词和有序图片包。

它解决的唯一问题：**提示词里的 @图片N 和实际上传的图片必须一一对应**。
所以这里的每个动作都围绕「序号」：清单的 # 列 = 资产清单的 图片N = 提示词里的 @图片N
= assets/图片N.<ext> = 图片包里的 图片N.<ext>。

用法
----
    py -3 ui/server.py                       # 127.0.0.1:8776，自动开浏览器
    py -3 ui/server.py --port 9000 --no-browser

只用标准库：http.server / json / base64 / zipfile / subprocess / pathlib。
整合包形态：ROOT/web-dist/ 存在时 serve 其静态产物（SPA，/api 口径不变），并优先用
包内 runtime/ffmpeg、包根 config/token 口令、app/config/channel-config.json 预置；
开发机没有这些目录，行为完全不变。
不改 pipeline/reverse/{extract,confirm,render}.py，不改 templates/，不改拆解.md；
对待确认清单.md 只改「命名」列。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from datetime import datetime
import traceback
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Windows 无窗口补丁（2026-11-05）：画布 vite 自动拉起 bridge 后，孙进程（ffmpeg/python）
# 会各弹一个终端窗口。本进程直接装补丁；子进程链经 PYTHONPATH 注入 pipeline/_nowindow
# 的 sitecustomize（Python 启动自动执行）实现全链覆盖。
_NOWINDOW_DIR = str(ROOT / "pipeline" / "_nowindow")
if _NOWINDOW_DIR not in sys.path:
    sys.path.insert(0, _NOWINDOW_DIR)

import hub_api  # noqa: E402 —— 自动化工作台业务层（内部自行把 wanrepro/ 加进 sys.path）
os.environ["PYTHONPATH"] = _NOWINDOW_DIR + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else "")
import sitecustomize  # noqa: E402  （_nowindow 目录；import 即安装 subprocess 无窗口补丁）
from automation import config as auto_config, local_assets  # noqa: E402  AT-14 B3 本地资产（config.PROJECTS_DIR 与 hub TASKS_DIR 同源）
from automation import replace as auto_replace          # noqa: E402  AT-22 B0 清单写锁（两链同一把）

# 拆解产物根：与 hub_api._analysis_reverse / replace.analysis_root / state 同一真源
# （`WANREPRO_ANALYSIS_ROOT` 优先）。为什么画布链也要认它：清单是本模块与 hub 链共写的
# 同一份文件，根不一致 = 读 A 写 B 的伪隔离（隔离探针会打到真实 analysis/reverse）。
_ANALYSIS_ENV = (os.environ.get("WANREPRO_ANALYSIS_ROOT") or "").strip()
ANALYSIS = Path(_ANALYSIS_ENV).expanduser() if _ANALYSIS_ENV else ROOT / "analysis" / "reverse"
RAW = ROOT / "原始资料"
UPLOADS = ROOT / "uploads"                 # 界面上传的新视频落这里；原始资料/ 一个字节不动
TEMPLATES = ROOT / "templates"
UI_DIR = Path(__file__).resolve().parent
INDEX: Path | None = None  # 老 web 工具页已删（M6-T8）；留空占位防旧引用，随后续清理移除
# 整合包形态：app/web-dist/ 放面布前端构建产物；存在 = 整合包，serve 它（SPA）。
# 开发机没有这个目录，相关分支全部退回现状。
WEB_DIST = ROOT / "web-dist"
WEB_DIST_ROOT: Path | None = WEB_DIST.resolve() if WEB_DIST.is_dir() else None
PY = sys.executable


def ui_index() -> Path:
    """首页入口：只有整合包形态的 web-dist/index.html（画布 SPA 构建产物）。
    2026-11-05 老 web 工具页（ui/index.html）已按 M6-T8 删除——开发机上画布走
    vite 开发服 :3000，:8779 根路径不提供页面。"""
    if WEB_DIST_ROOT is None:
        raise ApiError(503, "老 web 工具页已删除（M6-T8）。画布入口：开发机 vite :3000，整合包构建 web-dist 后自动提供。")
    return WEB_DIST_ROOT / "index.html"

IMAGE_EXT = {"png", "jpg", "jpeg", "webp"}
VIDEO_EXT = {"mp4", "mov", "webm"}
MAX_UPLOAD = int(os.environ.get("WAN_MAX_UPLOAD_MB", "500")) * 1024 * 1024   # 单个视频上限；公网演示时用环境变量收到 120
# 公网模式：设了 WAN_TOKEN 就要求带口令；本机开发不设，行为完全不变。
TOKEN = os.environ.get("WAN_TOKEN", "").strip()
if not TOKEN:
    # 整合包兼容：包根 config/token 首行当口令；WAN_TOKEN 未设才读，环境变量优先。
    _token_file = ROOT.parent / "config" / "token"
    if _token_file.is_file():
        try:
            TOKEN = _token_file.read_text(encoding="utf-8-sig", errors="replace").splitlines()[0].strip()
        except (OSError, IndexError):
            TOKEN = ""
FP_NAME = ".源视频指纹.json"
# 万相 omni2video 的真参数域（别自己造：不在这些值里的会直接 400）
RATIOS = ("16:9", "9:16", "1:1", "4:3", "3:4")
RESOLUTIONS = ("480P", "720P", "1080P")
TOKEN_RE = re.compile(r"[0-9a-f]{16}")     # 预演令牌：sha256 前 16 位十六进制
GEN_STALE = 60 * 60          # 记录写着 running 但一小时没动静 → 当它挂了，允许重开
MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpg"),
)
MIME = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}
VIDEO_MIME = {"mp4": "video/mp4", "m4v": "video/x-m4v", "mov": "video/quicktime", "webm": "video/webm"}
# 整合包静态 serve（web-dist）：按扩展名给 Content-Type，其余回 octet-stream。
STATIC_MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".json": "application/json",
    ".map": "application/json",
    ".webmanifest": "application/manifest+json",
    ".txt": "text/plain; charset=utf-8",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}

# Range: bytes=0-1023 / bytes=1024- / bytes=-2048（后缀）
RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")
CHUNK = 64 * 1024

ID_RE = re.compile(r"^[\w\u4e00-\u9fa5.\-]{1,64}$")
ASSET_RE = re.compile(r"^图片(\d+)\.(png|jpg|jpeg|webp)$", re.IGNORECASE)
HEAD_RE = re.compile(r"^##\s*([一二三四五六七八九十])\s+(.+?)\s*$")

# 上传文件名里的隐形字符：零宽空格/连接符、方向控制符、BOM、软连字符。
# 仓里已有先例（`需求录屏.mp4` 带 U+200B），不清掉的话路径看起来一样却打不开。
INVISIBLE_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff\u00ad]")
UNSAFE_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')

WRITE_LOCK = threading.Lock()
ANALYZING: set[str] = set()      # 正在跑的拆解（一次付费调用，别让双击变两次）
# 公网演示：/api/analyze 改成「提交 → 轮询」，否则隧道/边缘在 100 秒就会断（524），
# 而一次拆解实测 60–150 秒。JOBS 存进度，BUSY 保证全局同时只跑一个（一次付费调用）。
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
ANALYZE_BUSY = threading.Lock()
JOB_TTL = 3600                   # 一小时前的任务记录随手清掉

# 资产图「识别命名」的后台状态：(task_id, no) → {state, name, warning, started, elapsed}
# 命名要调多模态模型，实测 10–50 秒；挂在 /api/upload 的响应里就等于让界面假死，
# 所以上传只负责落盘，命名另开线程跑，前端轮询 /api/naming/<id>/<no> 拿结果。
NAMING: dict[tuple[str, int], dict] = {}
NAMING_LOCK = threading.Lock()
NAMING_KEEP = 900                # 跑完的状态保留 15 分钟（供刷新后的轮询），过期清掉


class ApiError(Exception):
    def __init__(self, status: int, msg: str, *, stage: str = "",
                 extra: dict | None = None) -> None:
        super().__init__(msg)
        self.status = status
        self.msg = msg
        # stage：机器可读的失败阶段（如 host_images）。调用方**只按它分类**，不猜文案——
        # 「图床不可用」必须能被 automation 认成前置拒绝，而不是「提交结果未知」。
        self.stage = stage
        self.extra = dict(extra or {})   # 附加自述（逐图源结果等），随错误体一起回给调用方


# ---------------------------------------------------------------- 基础读写


def read_text(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8-sig", errors="replace")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def task_dir(task_id: str) -> Path:
    if not task_id or ".." in task_id or not ID_RE.match(task_id):
        raise ApiError(400, f"任务 id 不合法：{task_id!r}")
    d = ANALYSIS / task_id
    if not d.is_dir():
        raise ApiError(404, f"任务不存在：{task_id}")
    return d


def assets_dir(task_id: str) -> Path:
    d = task_dir(task_id) / "assets"
    d.mkdir(parents=True, exist_ok=True)
    return d


def video_file(task_id: str, strict: bool = True) -> Path | None:
    """从 .源视频指纹.json 的「路径」读回源视频。

    指纹里存的是**相对仓库根**的路径（如 `原始资料/…/x.mp4`，文件名里可能带零宽
    字符），所以只在不是绝对路径时才拼 ROOT，其余原样保留——不要 normalize 名字。
    strict=False 供列表用：任何问题都返回 None，不抛。
    """
    fp = ANALYSIS / task_id / ".源视频指纹.json"
    if not fp.is_file():
        if not strict:
            return None
        raise ApiError(404, f"{task_id} 没有 .源视频指纹.json，无法定位源视频")
    try:
        meta = json.loads(read_text(fp) or "{}")
    except json.JSONDecodeError:
        if not strict:
            return None
        raise ApiError(500, f"{task_id} 的 .源视频指纹.json 不是合法 JSON") from None
    raw = str((meta or {}).get("路径") or "").strip()
    if not raw:
        if not strict:
            return None
        raise ApiError(404, f"{task_id} 的 .源视频指纹.json 里没有「路径」")
    path = Path(raw)
    if not path.is_absolute():
        path = ROOT / raw
    try:
        path = path.resolve()
    except OSError:
        if not strict:
            return None
        raise ApiError(400, f"源视频路径不合法：{raw}") from None
    if not path.is_file():
        if not strict:
            return None
        raise ApiError(404, f"源视频文件不在了：{raw}")
    return path


# ---------------------------------------------------------------- 拆解 / 清单 / 产物


def parse_dissection(task_id: str) -> list[dict]:
    """拆解.md → [{title, body}]，十层按原文顺序。"""
    text = read_text(task_dir(task_id) / "拆解.md")
    layers: list[dict] = []
    cur: dict | None = None
    buf: list[str] = []
    for raw in text.splitlines():
        m = HEAD_RE.match(raw)
        if m:
            if cur is not None:
                cur["body"] = "\n".join(buf).strip()
            cur = {"title": f"{m.group(1)} {m.group(2)}", "body": ""}
            layers.append(cur)
            buf = []
            continue
        if raw.startswith("## "):
            if cur is not None:
                cur["body"] = "\n".join(buf).strip()
                cur, buf = None, []
            continue
        if cur is not None:
            buf.append(raw)
    if cur is not None:
        cur["body"] = "\n".join(buf).strip()
    # 去掉开头的 HTML 注释（耗时/token 那行不进 UI）
    for layer in layers:
        layer["body"] = re.sub(r"^\s*<!--.*?-->\s*", "", layer["body"], flags=re.S).strip()
    return layers


def table_cells(line: str) -> list[str] | None:
    s = line.strip()
    if not s.startswith("|"):
        return None
    if re.fullmatch(r"\|[\s:|-]+\|", s):
        return None
    return [c.strip() for c in s.strip("|").split("|")]


def decode_desc_cell(value: str) -> str:
    """解码描述列自己的安全编码，不改变用户文本中的其他 HTML 实体。"""
    return (value.replace("&#124;", "|").replace("&#10;", chr(10))
            .replace("&#13;", chr(13)).replace("&#38;", "&"))


def encode_desc_cell(value: str) -> str:
    """把描述中的表格控制字符编码，读回后恢复为原文。"""
    return (str(value).replace("&", "&#38;").replace("|", "&#124;")
            .replace(chr(13), "&#13;").replace(chr(10), "&#10;"))


def parse_checklist(task_id: str) -> list[dict]:
    rows: list[dict] = []
    for raw in read_text(task_dir(task_id) / "待确认清单.md").splitlines():
        cells = table_cells(raw)
        if not cells or len(cells) < 6 or not re.fullmatch(r"\d+", cells[0]):
            continue
        rows.append({
            "no": int(cells[0]),
            "kind": cells[1],
            "seen": cells[2],
            "name": cells[3],
            "image": cells[4],
            "lock": cells[5],
            "suggest": cells[6] if len(cells) > 6 else "",
            "my_desc": decode_desc_cell(cells[7]) if len(cells) > 7 else "",
        })
    return rows


def task_intent(task_id: str) -> str:
    """读取任务当前意图；缺文件等同空字符串。"""
    path = task_dir(task_id) / "意图.txt"
    return path.read_bytes().decode("utf-8") if path.is_file() else ""


def intent_hash(intent: str) -> str:
    return hashlib.sha256(intent.encode("utf-8")).hexdigest()


def task_intent_changed(task_id: str, intent: str | None = None) -> bool:
    """当前意图哈希与拆解成功时记录的哈希不同；老指纹缺字段按空意图兼容。"""
    d = task_dir(task_id)
    current = task_intent(task_id) if intent is None else intent
    try:
        meta = json.loads(read_text(d / FP_NAME) or "{}")
    except json.JSONDecodeError:
        return bool(current)
    stored = meta.get("意图哈希", intent_hash("")) if isinstance(meta, dict) else intent_hash("")
    return stored != intent_hash(current)


def ensure_checklist_width(cells: list[str]) -> None:
    while len(cells) < 8:
        cells.append("")


def _update_checklist_cell(task_id: str, no: int, index: int, value: str) -> bool:
    """改清单第 no 行的第 index 列（画布链唯一写口）。

    AT-22 B0（双路审计 M-3）：**读-改-整文件重写全程持 `replace.CHECKLIST_LOCK`**——hub 链
    `set_asset → replace.set_row/set_group_write` 写的是同一份 `待确认清单.md`，从前这里用的是
    本模块自己的 WRITE_LOCK，两把锁互不相识，并发即后写者覆盖先写者。WRITE_LOCK 从此只罩
    资产图/视频等文件写，不再罩清单（锁序单向：task_lock → CHECKLIST_LOCK，画布链不持后者反要前者）。
    """
    path = task_dir(task_id) / "待确认清单.md"
    if not path.is_file():
        raise ApiError(404, "待确认清单.md 不存在，先跑一次拆解")
    with auto_replace.CHECKLIST_LOCK:
        lines = read_text(path).splitlines()
        for i, raw in enumerate(lines):
            cells = table_cells(raw)
            if not cells:
                continue
            if "类别" in cells and len(cells) < 8:
                ensure_checklist_width(cells)
                cells[6] = "建议"
                cells[7] = "用我的描述"
                lines[i] = "| " + " | ".join(cells) + " |"
                continue
            if len(cells) < 6 or not re.fullmatch(r"\d+", cells[0]):
                continue
            if int(cells[0]) != no:
                continue
            ensure_checklist_width(cells)
            cells[index] = value
            lines[i] = "| " + " | ".join(cells) + " |"
            write_text(path, "\n".join(lines) + "\n")
            return True
    raise ApiError(404, f"待确认清单里没有第 {no} 行")


def set_checklist_image(task_id: str, no: int, filename: str) -> bool:
    """把已落盘图片写回资产图列；这是渲染器判断「有资产图」的绑定真源。"""
    return _update_checklist_cell(task_id, no, 4, filename)


def set_checklist_desc(task_id: str, no: int, text: str) -> bool:
    """保存第 8 列描述；空字符串也要能清空人工覆盖。"""
    return _update_checklist_cell(task_id, no, 7, encode_desc_cell(text))


def set_checklist_name(task_id: str, no: int, name: str) -> bool:
    """只改「命名」列（第 4 列）；其余字节原样保留。"""
    clean = re.sub(r"\s+", " ", name).replace("|", "｜").strip()
    return _update_checklist_cell(task_id, no, 3, clean)


def parse_asset_sheet(task_id: str) -> list[dict]:
    rows: list[dict] = []
    for raw in read_text(task_dir(task_id) / "资产清单.md").splitlines():
        cells = table_cells(raw)
        if not cells or len(cells) < 4:
            continue
        m = re.fullmatch(r"图片(\d+)", cells[0])
        pending = cells[0] in ("—", "-", "–", "")
        if not m and not pending:
            continue
        what = cells[2]
        row_no = 0
        mm = re.match(r"清单第\s*(\d+)\s*行\s*·\s*", what)
        if mm:
            row_no = int(mm.group(1))
            what = what[mm.end():]
        rows.append({
            "no": int(m.group(1)) if m else 0,     # 提示词图号；0 = 还没备图，不进正文
            "row": row_no,                          # 清单行号：上传与写回都按它对齐
            "type": cells[1],
            "what": what,
            "lock": cells[3],
        })
    return rows


def asset_files(task_id: str) -> dict[int, Path]:
    found: dict[int, Path] = {}
    d = task_dir(task_id) / "assets"
    if not d.is_dir():
        return found
    for p in sorted(d.iterdir()):
        m = ASSET_RE.match(p.name)
        if m and p.is_file():
            found[int(m.group(1))] = p
    return found


def latest_prompt(task_id: str) -> tuple[str, str]:
    """→ (正文, 模板名)；取 mtime 最新的 提示词-<tpl>.md。"""
    files = sorted(task_dir(task_id).glob("提示词-*.md"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return "", ""
    path = files[0]
    return read_text(path), path.stem[len("提示词-"):]


# ---------------------------------------------------------------- 人工复检（B1/B2）

RECHECK_TIMEOUT = 60


def recheck_call(task_id: str, extra: list[str] | None = None) -> dict:
    """跑 `pipeline/recheck.py --json`：检测与写回只有那一份实现，这里不另写一套判断。

    命令行出错是一行中文（stderr），直接转给页面——「复检项不存在」「只能改一行」
    这些都要人看得懂，不该变成 500 加 traceback。
    """
    args = ["pipeline/recheck.py", "--id", task_id, "--json"] + list(extra or [])
    code, out, err = run_py(args, timeout=RECHECK_TIMEOUT)
    if code != 0:
        raise ApiError(400, (err.strip().splitlines() or [f"复检失败（退出 {code}）"])[-1])
    try:
        data = json.loads(out.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        raise ApiError(500, "复检清单输出看不懂：" + tail(out)) from None
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ApiError(500, "复检清单输出不是一个对象：" + tail(out))
    data.setdefault("pending", 0)
    return data


def render_task(task_id: str, stem: str, optional: str = "", audio_mode: str = "") -> str:
    """调 render.py 重渲染一版模板，返回落盘的模板名。模板不存在 → 400。

    `optional` 是要打开的可选段字母（如 `C`，逗号分隔可多个）：render.py 默认全关，
    不透传的话界面选了「品牌收束」也不会出现在正文里（2026-11-04 路 A 审计 B5——
    UI 是唯一入口，这里传了才算数）。

    `audio_mode`：音频三态（AT-17 B2a）。界面改选后要传下来才生效；不传时渲染器自己读
    任务目录 `audio_mode.json`（意图阶段落的默认值），所以老调用方不改也照常跑。
    """
    if not re.fullmatch(r"[0-9A-Za-z._-]{1,64}", stem) or not (TEMPLATES / f"{stem}.md").is_file():
        raise ApiError(400, f"模板不存在：templates/{stem}.md")
    argv = ["pipeline/reverse/render.py", "--id", task_id, "--template", stem]
    if str(optional or "").strip():
        argv += ["--optional", str(optional).strip()]
    mode = str(audio_mode or "").strip()
    if mode:
        if mode not in AUDIO_MODE_VALUES:
            raise ApiError(400, f"audio_mode 只认 {'/'.join(AUDIO_MODE_VALUES)}，收到「{mode}」")
        argv += ["--audio-mode", mode]
    code, out, err = run_py(argv, timeout=120)
    if code != 0:
        raise ApiError(500, f"render.py 退出 {code}：{tail(err) or tail(out)}")
    return latest_prompt(task_id)[1] or stem


def build_assets(task_id: str) -> list[dict]:
    """资产清单表 + 实际落盘图片 → 一份带 url 的对齐表。"""
    files = asset_files(task_id)
    out: list[dict] = []
    for row in parse_asset_sheet(task_id):
        f = files.get(row.get("row") or row["no"]) or files.get(row["no"])
        row["file"] = f.name if f else ""
        row["url"] = f"/assets/{urllib.parse.quote(task_id)}/{urllib.parse.quote(f.name)}" if f else ""
        out.append(row)
    # 清单里没有、但盘上有的（手放进去的图）也列出来，免得「传了图却没显示」。
    # 判重要按**清单行号**：图号只发给已备图的资产，没备图的行图号是 0，
    # 拿图号判重会把「第 2 行已传图（图号1）」和「盘上 图片2.png」当成两件东西，
    # 页面就会同一个资产出现两行（实测 10 个资产显示成 15 行）。
    known = {int(r.get("row") or r["no"]) for r in out}
    for no, f in sorted(files.items()):
        if no in known:
            continue
        out.append({"no": no, "row": no, "type": "未在清单内", "what": f.name, "lock": "",
                    "file": f.name,
                    "url": f"/assets/{urllib.parse.quote(task_id)}/{urllib.parse.quote(f.name)}"})
    out.sort(key=lambda r: (r.get("row") or 10 ** 6, r["no"]))   # 按清单行号排，没行号的垫底
    return out


DIALOGUE_NAME = "台词改写.md"
RECEIPT_NAME = "渲染回执.json"
# AT-17 B2c 音频三态：文件名 / 字段名 / 枚举值（与 pipeline/reverse/intent.py 一套，
# 改一处必须同改——探针 probe_at17_b2c 交叉断言两边字面一致）
AUDIO_MODE_NAME = "audio_mode.json"
AUDIO_MODE_FIELD = "audio_mode"
AUDIO_MODE_VALUES = ("沿用原片", "按新主体调整", "不要")
TIME_CODE_RE = re.compile(r"\d{1,2}:\d{2}(?:\.\d+)?\s*[-–—~～]\s*\d{1,2}:\d{2}(?:\.\d+)?")
QUOTE_CHARS = "\"'“”‘’「」『』"


def dialogue_map(task_id: str) -> list[dict] | None:
    """`台词改写.md` → [{start, source, zh}]；文件不在 → None（页面据此整块不显示）。

    解析口径与生产端 `render.py:load_dialogue_rows` 一致：全角/半角竖线、有无项目符号都先
    归一，再取「时间码 / 原文 / 中文」三列；标题行与分隔行按内容剔除，不假设模型格式。
    """
    path = task_dir(task_id) / DIALOGUE_NAME
    if not path.is_file():
        return None
    rows: list[dict] = []
    for raw in read_text(path).splitlines():
        line = re.sub(r"\s*[|｜]\s*", "｜", raw.strip().lstrip("-*+• ").strip())
        if "｜" not in line:
            continue
        cells = [c.strip() for c in line.strip("｜").split("｜")]
        if len(cells) < 3 or all(re.fullmatch(r"[-: ]*", c) for c in cells):
            continue
        if any(key in c for c in cells for key in ("起-止", "原文", "中文")):
            continue
        code = TIME_CODE_RE.search(cells[0])
        source = cells[1].strip().strip(QUOTE_CHARS).strip()
        zh = cells[2].strip()
        if not code or not source or not zh:
            continue
        rows.append({"start": code.group(0), "source": source, "zh": zh})
    return rows


def intent_receipt(task_id: str) -> dict | None:
    """`渲染回执.json` 原样读回（dialogue_total / hits / misses / residual / language_line）。

    缺失或坏了 → None：一个回执文件不该把整个任务页打成 500。
    """
    path = task_dir(task_id) / RECEIPT_NAME
    if not path.is_file():
        return None
    try:
        data = json.loads(read_text(path) or "{}")
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def task_audio_mode(task_id: str) -> dict | None:
    """`audio_mode.json` → {audio_mode, 依据, 待核对}；文件不在/坏了/值不在枚举内 → None。

    AT-17 B2c：三态默认值由意图阶段（analyze.stage_intent / intent.build）按拆解八 声音
    结构化字段推导后落盘，这里只读，不推导、不补值。字段名与枚举值全链一套
    （audio_mode / 沿用原片·按新主体调整·不要）。
    """
    path = task_dir(task_id) / AUDIO_MODE_NAME
    if not path.is_file():
        return None
    try:
        data = json.loads(read_text(path) or "{}")
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    value = str(data.get(AUDIO_MODE_FIELD) or "").strip()
    if value not in AUDIO_MODE_VALUES:
        return None
    return {"audio_mode": value, "依据": str(data.get("依据") or ""),
            "待核对": bool(data.get("待核对"))}


_INTENT_MODULE_CACHE: list = []


def _intent_module():
    """按路径加载 pipeline/reverse/intent.py（与 hub_api._intent_mod 同一做法，进程内缓存一次）。"""
    if _INTENT_MODULE_CACHE:
        return _INTENT_MODULE_CACHE[0]
    import importlib.util
    path = ROOT / "pipeline" / "reverse" / "intent.py"
    spec = importlib.util.spec_from_file_location("wanrepro_reverse_intent_for_server", path)
    if spec is None or spec.loader is None:
        raise ApiError(500, f"导不进 {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _INTENT_MODULE_CACHE.append(module)
    return module


def set_task_audio_mode(task_id: str, mode: str, basis: str = "") -> dict:
    """画布链：界面改选三态 → 落任务目录 `audio_mode.json`（画布链没有 approve，落盘即生效）。

    写实现只有 intent.save_audio_mode 一份（枚举校验也在那里，值域外 → 400 原文给用户）。
    画布链的提示词是按需渲染的（拉片 → 提示词 / 重渲染），渲染器自己读这份文件，
    所以这里只落盘、不预生成产物（方案 §4 B3：画布链落盘即可）。
    """
    dir_ = task_dir(task_id)                     # 校验 id 与任务存在（404 语义沿用）
    try:
        info = _intent_module().save_audio_mode(dir_, mode, basis or "画布拉片弹层改选")
    except ValueError as exc:
        raise ApiError(400, str(exc)) from None
    return {"ok": True, "id": task_id, **info, "来源": AUDIO_MODE_NAME}


def task_payload(task_id: str) -> dict:
    prompt_text, template = latest_prompt(task_id)
    src = video_file(task_id, strict=False)
    source: dict = {}
    if src is not None:
        try:
            abs_path = str(src)
        except OSError:
            abs_path = ""
        source = {"name": src.name, "rel": rel_path(src), "abs": abs_path}
    return {
        "id": task_id,
        "disc": {"layers": parse_dissection(task_id)},
        "checklist": parse_checklist(task_id),
        "prompt": {"text": prompt_text, "template": template},
        "assets": build_assets(task_id),
        "source": source,
        "intent": task_intent(task_id),
        "intent_changed": task_intent_changed(task_id),
        # 意图回执（C-3）：页面看「原句 → 中文」，复制正文只给成品。文件不在就是 null。
        "dialogueMap": dialogue_map(task_id),
        "intentReceipt": intent_receipt(task_id),
        # AT-17 B2c：音频三态（画布链节点状态只读面）——B3 界面接入前先透出，字段名同 hub 侧。
        "audio_mode": task_audio_mode(task_id),
    }


# ---------------------------------------------------------------- 生成成片（万相 CLI）


GEN_DIR = "生成"
GEN_RECORD = "记录.json"


def gen_dir(task_id: str) -> Path:
    return task_dir(task_id) / GEN_DIR


def gen_record(task_id: str) -> dict:
    """`生成/记录.json` 原样读回；没有/坏了 → {}（别让一个记录文件把整页打成 500）。"""
    path = gen_dir(task_id) / GEN_RECORD
    if not path.is_file():
        return {}
    try:
        data = json.loads(read_text(path) or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def gen_state(task_id: str) -> dict:
    """页面轮询用：记录 + 成片是否真在盘上 + 带版本号的 url（换了片子能刷掉浏览器缓存）。"""
    rec = gen_record(task_id)
    if not rec:
        return {"status": "idle", "template": latest_prompt(task_id)[1], "available": False,
                "preflight": {}}
    state = dict(rec)
    name = str(rec.get("video") or "")
    path = gen_dir(task_id) / name if name else None
    state["available"] = bool(path and path.is_file() and path.stat().st_size > 0)
    state["url"] = (f"/genvideo/{urllib.parse.quote(task_id)}?v={int(path.stat().st_mtime)}"
                    if state["available"] else "")
    state["bytes"] = path.stat().st_size if state["available"] else 0
    # 提交前核对的结果要能透到页面：出片之后才看得见「当时核过什么」
    state["preflight"] = rec.get("preflight") or {}
    if state.get("status") == "running":
        age = time.time() - float(rec.get("started") or 0)
        state["elapsed"] = round(age, 1)
        state["stale"] = age > GEN_STALE
    return state


GEN_PLAN_TIMEOUT = 240       # 预演要启 node、查余额、前后各拉一次任务列表；给足但不无限等


def gen_plan(task_id: str, template: str, duration: int, ratio: str, resolution: str,
             audio: bool = True) -> dict:
    """跑一次 `wan_video.plan`（**里面已经带了预演核对**）→ 计划字典。

    这是唯一的核对入口：弹层打开时、任一参数变化时、点「确认生成」时，都走这一条，
    所以页面上看到的核对行和服务端提交前判的东西是**同一个函数**算出来的。
    """
    args = ["pipeline/gen/wan_video.py", "--id", task_id,
            "--duration", str(duration), "--ratio", ratio, "--resolution", resolution,
            "--audio-output", "true" if audio else "false"]
    if template:
        args += ["--template", template]
    code, out, err = run_py(args, timeout=GEN_PLAN_TIMEOUT)
    if code != 0:
        raise ApiError(400, tail(err) or tail(out) or f"计划失败（退出 {code}）")
    try:
        info = json.loads(out.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        raise ApiError(500, "计划输出看不懂：" + tail(out)) from None
    if not isinstance(info, dict):
        raise ApiError(500, "计划输出不是一个对象：" + tail(out)) from None
    return info


# ------------------------------------------------- 万相视频生成 bridge（canvas 任务）
# 画布端一手件：POST /api/wan/video/run 建任务 → 后台子进程跑 wan_video.py →
# GET /api/wan/video/job/<jobId> 轮询（状态映射以 生成/记录.json 为唯一状态源，引擎契约）→
# GET /api/wan/video/file/<taskId> 拿成片。并发：万相 CLI 同时只认一条，第二条 409。

WAN_TEMPLATE = "canvas"                     # bridge 专用模板：提示词-canvas.md → 成片-canvas.mp4
WAN_RATIOS = ("16:9", "9:16", "1:1")        # 画布端只暴露这三个画幅
WAN_RESOLUTIONS = {"480": "480P", "720": "720P", "1080": "1080P"}   # 画布端给数字；引擎 argparse 要 P 后缀（RESOLUTIONS=480P/720P/1080P，见 wan_submit.py:31）
WAN_JOB_TIMEOUT = 3600                      # 引擎内部预演+提交+等待合计可到 ~45 分钟，这里兜底
WAN_BUSY = threading.Lock()                 # 同时最多 1 条万相 job（CLI 限制；并发会重复扣费）
DATAURL_RE = re.compile(r"^data:image/([\w.+-]+);base64,(.*)$", re.S)
DATAURL_EXT = {"png": "png", "jpeg": "jpg", "jpg": "jpg", "webp": "webp"}


def decode_data_url(raw: str, name: str) -> tuple[bytes, str]:
    """`data:image/<mime>;base64,…` → (字节, 扩展名)。mime 决定落盘扩展名，文件名只做展示。"""
    m = DATAURL_RE.match(str(raw or "").strip())
    if not m:
        raise ApiError(400, f"图片 {name} 的 dataUrl 不是 data:image/…;base64, 形式")
    ext = DATAURL_EXT.get(m.group(1).lower())
    if not ext:
        raise ApiError(400, f"图片 {name} 的格式 image/{m.group(1)} 不支持（只收 png/jpg/webp）")
    try:
        return base64.b64decode(re.sub(r"\s+", "", m.group(2)), validate=True), ext
    except ValueError:
        raise ApiError(400, f"图片 {name} 的 base64 解不出来") from None


# ------------------------------------------- 参考图图床（asset/upload 与 sdgf 共用）
# 渠道只收公网 HTTPS 直链：本地字节先留一份备份，再转传 uguu；uguu 不可用而本机又配了
# 腾讯云 videocache 凭证时走第二顺位（出站隧道降级已废弃移除，AT-16 B12f）。
# **拿到 URL 必须当场验活**（AT-16 B12）：只查「是不是 https」挡不住死链——格式对但拉不到
# 的 URL 发给渠道，渠道拉图失败 → 403 拒创建（auto-0043 实测）。

IMAGE_VERIFY_TIMEOUT = 8.0                            # 直链验活超时（秒）——验不过就当该图源不可用
# CF 边缘对 Python-urllib 默认 UA 有内置拦截（error code 1010），验活必须带浏览器 UA
IMAGE_VERIFY_UA = "Mozilla/5.0 (wanrepro-bridge)"
# 直连优先、再环境代理（与 upload_uguu_bytes 的双路径同法）：本机出站与渠道拉图不是同一条链路
_VERIFY_OPENERS = (urllib.request.build_opener(urllib.request.ProxyHandler({})), None)

# 第三图源：腾讯云 videocache（nginx + WebDAV PUT）。凭证是机器级 env，没配就**如实跳过**（不静默）。
VC_TOKEN_ENV = "TENCENT_VC_TOKEN"
VC_UPLOAD_BASE = os.environ.get("WANREPRO_VC_UPLOAD_BASE", "http://1.14.74.186/upload/")
VC_UPLOAD_TIMEOUT = 60
VC_PUBLIC_BASE = os.environ.get("WANREPRO_VC_PUBLIC_BASE", "https://vc.aistar.work/vc/")   # 渠道只收 https

# 上传渠道计数（AT-14 B3 A2，/api/hub/where 透出）：local=本地引用（零图床），
# uguu=转传成功，tencent=腾讯云图床。只增不减，进程内诊断面用。
ASSET_COUNTERS = {"local": 0, "uguu": 0, "tencent": 0}
_ASSET_COUNTERS_LOCK = threading.Lock()
_UGUU_LAST_ERROR = ""       # 最近一次 uguu 上传失败的短原因（人话自述用，进程内诊断面）


def _bump_asset_counter(source: str) -> None:
    key = {"tencent-videocache": "tencent"}.get(source, source)
    if key in ASSET_COUNTERS:
        with _ASSET_COUNTERS_LOCK:
            ASSET_COUNTERS[key] += 1


def upload_uguu_bytes(data: bytes, name: str) -> str:
    """转传 uguu（Pomf API）返回直链；任何异常返回空串由调用方降级。

    直传与系统代理各试一次（uguu 是境外站，两种路径通哪个用哪个）。
    失败原因落 _UGUU_LAST_ERROR（HTTP 码 / 连不上），供前置拒绝的人话自述用。
    """
    global _UGUU_LAST_ERROR
    CRLF = chr(13) + chr(10)          # multipart 边界串用字符拼，避免源码里的转义被工具链吃掉
    boundary = "----wanrepro" + secrets.token_hex(8)
    body = io.BytesIO()
    body.write(f'--{boundary}{CRLF}Content-Disposition: form-data; name="files[]"; '
               f'filename="{name}"{CRLF}Content-Type: application/octet-stream{CRLF}{CRLF}'.encode())
    body.write(data)
    body.write(f"{CRLF}--{boundary}--{CRLF}".encode())
    payload = body.getvalue()
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}",
               "User-Agent": "Mozilla/5.0 (wanrepro-bridge)"}
    reasons: list[str] = []
    for handlers in ([urllib.request.ProxyHandler({})], None):  # 先直连，后环境代理
        try:
            opener = urllib.request.build_opener(*(handlers or []))
            req = urllib.request.Request("https://uguu.se/upload?output=json",
                                         data=payload, headers=headers, method="POST")
            resp = json.loads(opener.open(req, timeout=60).read().decode())
            url = str((resp.get("files") or [{}])[0].get("url") or "")
            if url.startswith("https://"):
                _UGUU_LAST_ERROR = ""
                return url
            reasons.append("上传返回里没有 https 直链")
        except urllib.error.HTTPError as err:
            reasons.append(f"HTTP {err.code}")
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            reasons.append(f"连不上（{getattr(err, 'reason', err)}）")
        except (json.JSONDecodeError, ValueError, KeyError, IndexError, TypeError):
            reasons.append("上传响应看不懂")
    _UGUU_LAST_ERROR = " / ".join(dict.fromkeys(reasons)) or "上传失败"
    return ""


class ImageHostError(ApiError):
    """参考图没有公网地址：图源全不可用，或直链验活不过（**本地前置拒绝**，零渠道请求）。

    stage=host_images 是机器可读标记：automation 据此回到 awaiting_approval（可改后重提），
    而不是记成「提交结果未知」——后者会把人引导去渠道核单，而这次根本没发请求。
    """

    def __init__(self, msg: str, *, sources: list[dict] | None = None,
                 skipped: list[dict] | None = None) -> None:
        super().__init__(502, msg, stage="host_images",
                         extra={"sources": sources or [], "skipped": skipped or []})
        self.sources = sources or []
        self.skipped = skipped or []


def _verify_once(url: str, opener, timeout: float) -> tuple[bool, str]:
    """HEAD 拿不到就 GET 首字节（有的站不支持 HEAD）；200/206 且能读到字节才算活。"""
    for method in ("HEAD", "GET"):
        req = urllib.request.Request(url, method=method, headers={"User-Agent": IMAGE_VERIFY_UA})
        try:
            resp = (opener.open(req, timeout=timeout) if opener
                    else urllib.request.urlopen(req, timeout=timeout))
            with resp:
                code = int(getattr(resp, "status", 0) or 0)
                if code not in (200, 206):
                    return False, f"HTTP {code}"
                if method == "GET":
                    if not resp.read(1):
                        return False, "0 字节（文件不在）"
                    return True, f"HTTP {code}"
                declared = (resp.headers.get("Content-Length") or "").strip()
                if declared == "0":
                    return False, "0 字节（文件不在）"      # 头里就自报空文件：不必再 GET
                if not declared:
                    continue                              # 没给长度（分块）：改用 GET 真取首字节
                return True, f"HTTP {code}"
        except urllib.error.HTTPError as err:
            if method == "HEAD" and err.code not in (403, 404, 410):
                continue                       # 头方法不支持（405/501）或 5xx：改用 GET 真取一次
            return False, f"HTTP {err.code}"
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            return False, f"连不上（{getattr(err, 'reason', err)}）"
    return False, "HEAD/GET 都拿不到"

def image_url_alive(url: str, timeout: float = IMAGE_VERIFY_TIMEOUT) -> tuple[bool, str]:
    """公网直链验活 → (可达?, 原因/状态)。**只信「真能取到字节」这一个事实。**

    直连优先、再环境代理；403/404/410 是「文件不在」，换出口也一样，不再重试。
    """
    reasons: list[str] = []
    for opener in _VERIFY_OPENERS:
        ok, why = _verify_once(url, opener, timeout)
        if ok:
            return True, why
        reasons.append(why)
        if why in ("HTTP 403", "HTTP 404", "HTTP 410"):
            break
    return False, " / ".join(dict.fromkeys(reasons)) or "不可达"


def upload_tencent_vc(data: bytes, name: str, key: str, token: str) -> tuple[str, str]:
    """第三图源：腾讯云 videocache（WebDAV PUT）→ (公网 https 直链, 失败原因)。

    上传走明文直连（快）；对外直链必须是 https（渠道只收 https），前缀可用
    WANREPRO_VC_PUBLIC_BASE 换。云服 6 小时定时清空 + 5GB 配额：只当提交瞬间的中转。
    """
    path = urllib.parse.quote(key, safe="/")
    try:
        req = urllib.request.Request(
            f"{VC_UPLOAD_BASE.rstrip('/')}/{path}", data=data, method="PUT",
            headers={"X-Token": token, "Content-Type": "application/octet-stream",
                     "User-Agent": IMAGE_VERIFY_UA})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=VC_UPLOAD_TIMEOUT) as resp:
            code = int(getattr(resp, "status", 0) or 0)
            if code not in (200, 201, 204):
                return "", f"上传 HTTP {code}"
    except urllib.error.HTTPError as err:
        return "", f"上传 HTTP {err.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        return "", f"上传连不上（{getattr(err, 'reason', err)}）"
    return f"{VC_PUBLIC_BASE.rstrip('/')}/{path}", ""


def _host_failure_message(tried: list[dict], skipped: list[dict]) -> str:
    """图床全不可用的人话指引：逐图源自述 + 明说「没发渠道请求、不扣费」。"""
    parts = [f"{row.get('label') or row.get('source')} {row.get('reason') or '不可用'}"
             for row in tried]
    parts += [f"{row.get('label') or row.get('source')} {row.get('reason')}" for row in skipped]
    detail = "；".join(parts) or "所有图源都不可用"
    return (f"图床不可用（{detail}）——参考图没有公网地址，渠道拉不到图会拒创建。"
            "本地已拒绝提交：这次没有向渠道发任何请求、不会扣费；图床修好后直接重试即可。")


def host_image_detailed(data: bytes, raw_name: str) -> dict:
    """参考图字节 → 公网 https 直链，**每个候选直链逐一验活** → {url, source, tried, skipped}。

    图源顺序 uguu → 腾讯云 videocache（没配 TENCENT_VC_TOKEN 就如实跳过）；
    出站隧道降级已废弃移除（AT-16 B12f）。
    本地一律留一份备份（uploads/vc/）——M4 直链路由靠它出图。
    全部不可用抛 ImageHostError：本地前置拒绝，参考图没有公网地址就不发任何渠道请求。
    """
    safe_name = re.sub(r"[^\w.\-]", "_", raw_name or "ref.png")
    # 公网直链必须是 **ASCII 路径**：中文名的 URL 发出去要 percent-encode，渠道/隧道各按各自
    # 规矩解，实测 urllib 直接在 putrequest 上炸 UnicodeEncodeError（B12 探针撞出来的）。
    # 本地备份也用同一个 key，于是 /vc/ 路由与备份文件一一对得上。
    stem = re.sub(r"[^0-9A-Za-z._-]", "", Path(safe_name).stem)[:40] or "ref"
    ext = Path(safe_name).suffix.lower() or ".png"
    ascii_name = f"{stem}{ext}"
    key = f"m4/{secrets.token_hex(4)}/{ascii_name}"
    dest = UPLOADS / "vc" / key
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)

    tried: list[dict] = []
    skipped: list[dict] = []

    def accept(source: str, label: str, url: str) -> dict | None:
        ok, why = image_url_alive(url)
        if ok:
            _bump_asset_counter(source)          # 计数只旁路观察，不改返回形状
            return {"url": url, "source": source, "tried": tried, "skipped": skipped}
        tried.append({"source": source, "label": label, "url": url,
                      "reason": f"直链验活失败（{why}）"})
        return None

    uguu_url = upload_uguu_bytes(data, ascii_name)          # ① uguu
    if uguu_url:
        got = accept("uguu", "uguu", uguu_url)
        if got:
            return got
    else:
        tried.append({"source": "uguu", "label": "uguu",
                      "reason": f"上传失败（{_UGUU_LAST_ERROR or '服务不可用'}）"})

    token = os.environ.get(VC_TOKEN_ENV, "").strip()        # ② 腾讯云 videocache
    if not token:
        skipped.append({"source": "tencent-videocache", "label": "腾讯云图床",
                        "reason": f"未配置 {VC_TOKEN_ENV}，已跳过"})
    else:
        vc_url, vc_why = upload_tencent_vc(data, ascii_name, key, token)
        if vc_url:
            got = accept("tencent-videocache", "腾讯云图床", vc_url)
            if got:
                return got
        else:
            tried.append({"source": "tencent-videocache", "label": "腾讯云图床", "reason": vc_why})

    raise ImageHostError(_host_failure_message(tried, skipped), sources=tried, skipped=skipped)


def store_public_image(data: bytes, raw_name: str) -> tuple[str, str]:
    """参考图字节 → (公网 https 直链, 来源)。本地落 uploads/vc/ 作备份。

    拿到的每条 URL 都验活；全不可用抛 ImageHostError（502，可修复后重试），
    **不再把格式对但拉不到的死链交给调用方**。
    """
    got = host_image_detailed(data, raw_name)
    return str(got["url"]), str(got["source"])


def wan_prepare_task(prompt: str, images: list) -> str:
    """建 `wan-<unixts>` 任务目录并落盘输入：提示词-canvas.md + assets/图片N.<ext>（按序）。

    目录建在 analysis/reverse/ 下——wan_video.py 的 task_dir() 只认那里
    （`ANALYSIS = ROOT/"analysis"/"reverse"`，引擎不改，bridge 就按引擎的输入约定走）。
    """
    with WRITE_LOCK:
        ts = int(time.time())
        while (ANALYSIS / f"wan-{ts}").exists():        # 同秒并发 → 顺延到下一个空闲时间戳
            ts += 1
        task_id = f"wan-{ts}"
        d = ANALYSIS / task_id
        (d / "assets").mkdir(parents=True, exist_ok=True)
        write_text(d / f"提示词-{WAN_TEMPLATE}.md", prompt)
        for no, item in enumerate(images, start=1):     # 列表顺序 = 图号顺序，@图片N 按此对齐
            name = str(item.get("name") or f"图片{no}")
            blob, ext = decode_data_url(item.get("dataUrl"), name)
            if not blob:
                raise ApiError(400, f"图片 {name} 是空的（0 字节）")
            (d / "assets" / f"图片{no}.{ext}").write_bytes(blob)
        return task_id


def wan_argv(task_id: str, duration: int, ratio: str, resolution: str,
             audio: bool, confirm: bool) -> list[str]:
    """wan_video.py 子进程命令行（唯一拼装点；参数名与其 argparse 逐一对过）。

    不带 `--confirm` = 引擎只打计划不提交（引擎契约：花钱动作必须显式确认）。
    """
    argv = ["pipeline/gen/wan_video.py", "--id", task_id, "--template", WAN_TEMPLATE,
            "--duration", str(duration), "--ratio", ratio,
            "--resolution", WAN_RESOLUTIONS[resolution],
            "--audio-output", "true" if audio else "false"]
    if confirm:
        argv.append("--confirm")
    return argv


def wan_state_by_task(task_id: str) -> dict:
    """/api/wan/video/state/<taskId>：按承载目录读 `生成/记录.json`（**重启后 JOBS 丢了也能查**）。

    AT-13 §2.3/§5：万相续查/找回只认这条读路径；cloudTaskId=记录.json.taskId（云端 id），
    taskId=本地承载目录号（wan-<ts>，不是云端 id）。
    """
    if not task_id or ".." in task_id or not re.fullmatch(r"[0-9A-Za-z._-]{1,64}", task_id):
        raise ApiError(400, f"任务号不合法：{task_id!r}")
    d = ANALYSIS / task_id
    if not d.is_dir():
        raise ApiError(404, f"没有这个万相承载任务：{task_id}")
    rec = gen_record(task_id)
    if not rec:
        return {"status": "idle", "phase": "", "cloudTaskId": "", "stage": "",
                "taskId": task_id, "hasVideo": False}
    status = str(rec.get("status") or "")
    out: dict = {"status": status or "running", "phase": str(rec.get("phase") or ""),
                 "cloudTaskId": str(rec.get("taskId") or ""), "taskId": task_id,
                 "stage": str(rec.get("stage") or "")}
    if status == "failed":
        out["error"] = str(rec.get("error") or "生成失败（原因见任务记录）")
    final = d / GEN_DIR / f"成片-{WAN_TEMPLATE}.mp4"
    out["hasVideo"] = bool(final.is_file() and final.stat().st_size > 0)
    return out


def wan_video_job_state(job_id: str) -> dict:
    """/api/wan/video/job/<jobId>：JOBS 记录 + 生成/记录.json → 轮询状态。

    记录.json 是唯一状态源；子进程退出码只补「进程级失败」：
    exit != 0 → failed + stderr 尾部一句（引擎把原因打在 stderr 最后一行）。
    """
    with JOBS_LOCK:
        rec = JOBS.get(job_id)
        rec = dict(rec) if rec else None
    if rec is None or rec.get("kind") != "wan-video":
        raise ApiError(404, "没有这个万相视频任务号（可能服务重启过，重新发起一次生成即可）")
    task_id = str(rec.get("task_id") or "")
    try:
        gen_rec = gen_record(task_id)
        gen_path = gen_dir(task_id)
    except ApiError:
        gen_rec, gen_path = {}, None

    out: dict = {"status": "running", "stage": "计划",
                 "elapsed": round(time.time() - float(rec.get("started") or time.time()), 1),
                 "taskId": task_id,
                 # AT-13 §2.3 R2 N-03：结构化阶段（pre_rejected/submitting/submit_unknown/
                 # submitted/wait_timeout/cloud_failed/done）+ 云端 id（记录.json.taskId）
                 "phase": str(gen_rec.get("phase") or ""),
                 "cloudTaskId": str(gen_rec.get("taskId") or "")}
    final_name = f"成片-{WAN_TEMPLATE}.mp4"
    jstatus = str(gen_rec.get("status") or "")
    if jstatus == "done":
        out.update(status="done", stage="完成")
        return out
    if jstatus == "failed":
        out.update(status="failed", stage="失败",
                   error=str(gen_rec.get("error") or "生成失败（原因见任务记录）"))
        return out
    if jstatus == "running":
        if not gen_rec.get("taskId"):
            out["stage"] = "已提交"               # 记录已写、taskId 还没回填 = CLI 提交中
        elif gen_path is not None and (
                (gen_path / final_name).is_file()
                or any(p.name != final_name for p in gen_path.glob("*.mp4"))):
            out["stage"] = "下载中"               # CLI 已往 生成/ 落 mp4（还没改名成成片）
        else:
            out["stage"] = "生成中"

    job_status = str(rec.get("status") or "running")
    if job_status == "error":                    # bridge 自己起子进程/记日志就挂了
        out.update(status="failed", error=str(rec.get("error") or "bridge 内部异常"))
    elif job_status == "done" and int(rec.get("exit") or 0) != 0:
        lines = [ln for ln in str(rec.get("stderr") or "").strip().splitlines() if ln.strip()]
        out.update(status="failed",
                   error=lines[-1] if lines else f"wan_video.py 退出码 {rec.get('exit')}")
    elif job_status == "done" and jstatus != "running":
        out.update(status="done", stage="完成")   # dry 计划：引擎退出 0 且没有记录.json
    return out


# --------------------------------------------- SDGF 视频生成 bridge（服务端合同）
# 画布前端的提交/轮询/下载三件事（web/src/services/api/video.ts 的 sdgf 一族 +
# sdgf-payload.ts 的两族方言）在这里复刻成服务端端点，automation 只带渠道配置直调，
# 不用开浏览器：
#   POST /api/sdgf/video/run   body {prompt, images?, duration, ratio, resolution, audio,
#                                    mode?, confirm, channel{baseUrl,apiKey,model}}
#        dry（confirm 不是布尔 true）→ {dry:true, preview, preflight}
#        confirm=true              → {job, taskId, dry:false}，后台轮询并代下成片
#   GET  /api/sdgf/video/job/<job>     → {status: running|done|failed, stage, elapsed, progress?, taskId, error?}
#   GET  /api/sdgf/video/file/<taskId> → video/mp4（Range 流；成片落在 uploads/sdgf/<taskId>/）
# 渠道合同（与前端逐字对齐，改一处必须同步另一处）：
#   POST {base}/v1/videos（JSON，鉴权头带渠道 key）→ {id}
#   GET  {base}/v1/videos/{id} → {status: queued|in_progress|completed|failed|cancelled,
#                                progress: 0-100 数字, metadata.url}
#   GET  {base}/v1/videos/{id}/content → 字节（metadata.url 不是完整 http(s) 时走这条）
# 凭证只在请求内存里：不落盘、不进日志、任何响应都不回显（渠道错误文本里出现也抹成 ***）。

SDGF_RATIOS = ("16:9", "9:16", "1:1", "4:3", "3:4", "adaptive")
SDGF_METADATA_RATIOS = ("16:9", "9:16", "1:1")   # 站方文档枚举：别名族只认这三个进 metadata.ratio
SDGF_RESOLUTIONS = ("480", "720", "1080")        # 只做参数核对；渠道按模型名定档，不发这个字段
SDGF_LEGACY_MAX_SECONDS = 15                     # SD2.0 旧合同上限；SD2.5 全系 4–30 原样
SDGF_LEGACY_MODEL_RE = re.compile(r"^sd2(\.0)?-", re.IGNORECASE)
SDGF_FULL_MODEL_RE = re.compile(r"^SD2\.5-满血-GF-(480P|720P|1080P)$", re.IGNORECASE)
SDGF_HTTP_TIMEOUT = 90                           # 创建/查询：与前端 axios 的 90s 一致
SDGF_DOWNLOAD_TIMEOUT = 600                      # 成片下载：几分钟的 mp4 不能按 90s 掐
SDGF_POLL_INTERVAL = 2.5                         # 与前端等待单点一致（360×2.5s=15 分钟）
SDGF_POLL_MAX = 360
SDGF_FAILED_RECHECK = 3                          # 站方存在瞬态 failed 自愈：连败 3 次才判死
SDGF_FAILED_RECHECK_DELAY = 8
SDGF_JOBS = threading.BoundedSemaphore(2)        # 并发上限 2（项目红线），第三条 409
SDGF_RECORD = "任务.json"
SDGF_FINAL = "成片.mp4"
SDGF_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 渠道直连，不走系统代理


def sdgf_api_base(raw: str) -> str:
    """渠道 baseUrl → `…/v1`（与前端 buildApiUrl 同一条归一：已带 /v1 就不重复拼）。"""
    base = str(raw or "").strip().rstrip("/")
    if not base:
        return ""
    return base if base.lower().endswith("/v1") else base + "/v1"


def sdgf_url_ok(url: str) -> bool:
    """baseUrl 形态：https 一律放行；http 只放行回环（本地 mock）——凭证不走明文。"""
    if not re.match(r"^https?://", url or "", re.IGNORECASE):
        return False
    if url.lower().startswith("https://"):
        return True
    return (urllib.parse.urlparse(url).hostname or "").lower() in ("127.0.0.1", "localhost", "::1")


def mask_secret(text: str, secret: str) -> str:
    """把渠道凭证从任何要回给调用方/写日志的文本里抹掉（渠道错误体可能原样回显 key）。"""
    out = str(text or "")
    return out.replace(secret, "***") if secret else out


def sdgf_full_model(model: str) -> bool:
    return bool(SDGF_FULL_MODEL_RE.match((model or "").strip()))


def sdgf_ratio_fields(model: str, ratio: str) -> dict:
    """比例字段：满血名只发顶层 aspect_ratio；别名族双写 metadata.ratio（网关只认后者）。"""
    if sdgf_full_model(model):
        return {"aspect_ratio": ratio}
    return ({"aspect_ratio": ratio, "metadata": {"ratio": ratio}}
            if ratio in SDGF_METADATA_RATIOS else {"aspect_ratio": ratio})


def sdgf_sound_fields(model: str, audio: bool) -> dict:
    """声音开关：满血名生效位在顶层 enableSound；别名族在 metadata.enableSound。"""
    if not audio:
        return {}
    return {"enableSound": "on"} if sdgf_full_model(model) else {"metadata": {"enableSound": "on"}}


def sdgf_clamp_duration(model: str, seconds: int) -> int:
    """时长按代际：SD2.0 系旧合同上限 15（截断），SD2.5 全系 4–30 原样。"""
    return min(seconds, SDGF_LEGACY_MAX_SECONDS) if SDGF_LEGACY_MODEL_RE.match((model or "").strip()) else seconds


def sdgf_mode_name(mode: str, image_count: int) -> str:
    """站方中文 mode 枚举（与前端 resolveVideoMode + sdgfMode 两张表逐条对齐）。"""
    if mode == "reference":
        return "参考生视频"
    if image_count == 0:
        return "文生视频"
    if image_count == 1:
        return "首帧生视频"
    if image_count == 2:
        return "首尾帧"
    return "参考生视频"


def sdgf_plan(payload: dict) -> dict:
    """请求体 → 预演 + 结构化核对 + 真提交用的渠道请求体（**纯本地计算，不发网络请求**）。

    核对项与前端对齐：时长 4–30（SD2.0 旧合同截断到 15）、画幅白名单、两个模型族的
    比例/声音生效位、mode 按图数、参考图只收公网 HTTPS（dataUrl 到真提交前才换 URL）。
    `preflight.reasons` 为空才算通过；ok=false 时 confirm=true 也不提交（不花钱）。
    """
    reasons: list[str] = []
    notes: list[str] = []
    channel = payload.get("channel") if isinstance(payload.get("channel"), dict) else {}
    api_base = sdgf_api_base(str(channel.get("baseUrl") or ""))
    model = str(channel.get("model") or "").strip()
    key = str(channel.get("apiKey") or "").strip()
    if not api_base:
        reasons.append("channel.baseUrl 不能为空")
    elif not sdgf_url_ok(api_base):
        reasons.append("channel.baseUrl 必须是 https（http 只放行 127.0.0.1/localhost 的本地 mock）：凭证不走明文")
    if not model:
        reasons.append("channel.model 不能为空（模型名决定比例/声音方言与分辨率档位）")
    if not key:
        reasons.append("channel.apiKey 不能为空")

    prompt = str(payload.get("prompt") or "")
    if not prompt.strip():
        reasons.append("prompt 不能为空")

    duration = None
    raw_duration = payload.get("duration")
    try:
        duration = int(round(float(raw_duration)))
    except (TypeError, ValueError):
        reasons.append(f"duration 必须是秒数（4–30，收到 {raw_duration!r}）")
    else:
        if not 4 <= duration <= 30:
            reasons.append(f"duration 只支持 4–30 秒（收到 {duration}）")
            duration = None
        elif model:
            clamped = sdgf_clamp_duration(model, duration)
            if clamped != duration:
                notes.append(f"{model} 属旧合同上限 15 秒：按 {clamped} 秒预演（请求 {duration} 秒）")
                duration = clamped

    ratio = str(payload.get("ratio") or "").strip()
    if ratio not in SDGF_RATIOS:
        reasons.append(f"ratio 只支持 {'/'.join(SDGF_RATIOS)}（收到 {ratio!r}）")

    resolution = str(payload.get("resolution") or "").strip()
    if resolution:
        digits = resolution.lower()
        digits = digits[:-1] if digits.endswith("p") else digits
        if digits not in SDGF_RESOLUTIONS:
            reasons.append(f"resolution 只支持 480/720/1080（收到 {resolution!r}）")
        else:
            model_res = re.search(r"(480|720|1080)P", model, re.IGNORECASE)
            if model_res and model_res.group(1) != digits:
                reasons.append(f"resolution={digits} 与模型名档位 {model_res.group(1).upper()}P 不一致"
                               "（渠道按模型名定档，请换模型或改 resolution）")
        notes.append("渠道按模型名定档：本端点不发 resolution/size 字段")

    audio_raw = payload.get("audio", True)
    if not isinstance(audio_raw, bool):
        reasons.append("audio 必须是布尔（true/false）")
    audio = audio_raw if isinstance(audio_raw, bool) else True

    mode_raw = payload.get("mode")
    if mode_raw not in (None, "", "frames", "reference"):
        reasons.append(f"mode 只支持 reference/frames（收到 {mode_raw!r}）")
    mode = "reference" if mode_raw == "reference" else "frames"

    raw_images = payload.get("images")
    raw_images = raw_images if isinstance(raw_images, list) else []
    images: list[dict] = []
    for no, item in enumerate(raw_images, start=1):
        name, url, blob = f"ref-{no}.png", "", None
        if isinstance(item, str):
            url = item.strip()
        elif isinstance(item, dict):
            name = str(item.get("name") or name)
            url = str(item.get("url") or "").strip()
            if not url and item.get("dataUrl"):
                try:
                    blob, ext = decode_data_url(str(item.get("dataUrl")), name)
                except ApiError as err:
                    reasons.append(err.msg)
                    continue
                if not blob:
                    reasons.append(f"参考图 {name} 是空的（0 字节）")
                    continue
                if len(blob) > 20 * 1024 * 1024:
                    reasons.append(f"参考图 {name} 超过 20 MB（{len(blob) / 1048576:.1f} MB）")
                    continue
                name = f"{Path(name).stem or f'ref-{no}'}.{ext}"
        else:
            reasons.append(f"images[{no}] 必须是公网 URL 字符串或 {{name, dataUrl}}")
            continue
        if url and not url.lower().startswith("https://"):
            reasons.append(f"参考图 {no} 的 URL 必须是公网 https 直链（收到 {url[:60]!r}）")
            continue
        if not url and blob is None:
            reasons.append(f"参考图 {no} 既没有 url 也没有 dataUrl")
            continue
        images.append({"no": no, "name": name, "url": url, "data": blob})
    if mode == "reference" and not images:
        reasons.append("mode=reference 至少需要 1 张参考图（渠道没有 0 图参考模式）")
    if any(not img["url"] for img in images):
        notes.append("dataUrl 参考图在 confirm=true 时先换公网 URL（dry 只做本地校验，不上传）")

    body: dict = {"prompt": prompt}
    if model:
        body["model"] = model
    if duration:
        body["duration"] = duration
    ratio_fields = sdgf_ratio_fields(model, ratio) if (model and ratio in SDGF_RATIOS) else {}
    sound_fields = sdgf_sound_fields(model, audio) if model else {}
    if ratio_fields:
        body["aspect_ratio"] = ratio_fields["aspect_ratio"]
    metadata = {**(ratio_fields.get("metadata") or {}), **(sound_fields.get("metadata") or {})}
    if metadata:
        body["metadata"] = metadata
    if sound_fields.get("enableSound"):
        body["enableSound"] = sound_fields["enableSound"]
    body["mode"] = sdgf_mode_name(mode, len(images))
    if images:
        body["images"] = [img["url"] or f"<dataUrl:{img['name']} 待传图床>" for img in images]

    preview = {
        "method": "POST",
        "url": f"{api_base}/videos" if api_base else "",
        "headers": {"Authorization": "Bearer ***", "Content-Type": "application/json"},
        "channel": {"baseUrl": api_base, "model": model},
        "body": body,
        "images": [{"no": img["no"], "name": img["name"],
                    "source": "url" if img["url"] else "dataUrl",
                    "url": img["url"], "bytes": len(img["data"]) if img["data"] else None} for img in images],
        "notes": notes,
    }
    return {
        "preview": preview,
        "preflight": {"ok": not reasons, "reasons": reasons},
        "body": body,
        "images": images,
        "channel": {"baseUrl": api_base, "model": model},
        "key": key,          # 内部字段：只给提交/轮询用，绝不进响应
        "request": {"promptChars": len(prompt), "promptSha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                    "model": model, "duration": duration, "ratio": ratio,
                    "mode": body.get("mode"), "images": len(images)},
    }


def sdgf_http(url: str, key: str, *, method: str = "GET", body: dict | None = None,
              timeout: int = SDGF_HTTP_TIMEOUT, passthrough_4xx: bool = False) -> bytes:
    """渠道 HTTP（鉴权头与 JSON 序列化的唯一装配点；直连不走系统代理）。

    passthrough_4xx：**只给创建链**（sdgf_create）用——AT-16 B11：渠道在 POST /videos 回 4xx
    且带错误体，等于明确说「任务没建成」（auto-0043 现场：403 fail_to_fetch_task /
    video_request_failed），必须把真实状态码+原文透出去，让上层判「已拒绝·可改后重提」；
    一律包成 502 会把明确拒绝抹成「结果未知」，逼所有者去渠道控制台核对 + 走解除阻断绕路。
    查询/下载链**不开**（那些阶段的 4xx/断连/超时确实是状态不明 → 仍 502/504）。
    """
    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with SDGF_DIRECT.open(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as err:
        # AT-13 §2.8：**先脱敏后截断**——先截 600 字节会把 key 切半，剩下的前缀照样泄出去（R1 实证）
        raw = err.read(65536).decode("utf-8", errors="replace").strip()
        text = mask_secret(raw, key)[:600]
        if passthrough_4xx and 400 <= err.code < 500:
            # 创建阶段 4xx = 明确拒绝（任务没建成）：原码 + 原文一起透传，不抹成 502。
            raise ApiError(err.code, f"渠道创建端点明确拒绝 HTTP {err.code}：{text or err.reason}") from None
        raise ApiError(502, f"渠道响应 HTTP {err.code}：{text or err.reason}") from None
    except urllib.error.URLError as err:
        raise ApiError(502, mask_secret(
            f"渠道连不上（{err.reason}）——提交状态不明时勿重复提交，先到渠道控制台核对", key)) from None
    except TimeoutError:
        raise ApiError(504, "渠道请求超时——提交状态不明时勿重复提交，先到渠道控制台核对") from None


def sdgf_create(api_base: str, key: str, body: dict) -> str:
    """提交生成任务，返回渠道任务 id。

    渠道 4xx（带错误体）= 明确拒绝、任务没建成 → 原码透传（AT-16 B11：上层按「已拒绝·可改后
    重提」处理，不用去控制台核对、也不用解除阻断）。
    其余「不确定有没有建成」（5xx / 断连 / 超时 / 2xx 却没有任务 id）仍按 502 报并要求核对，
    不自动重试——那几种情况下云端可能已经收单。
    """
    raw = sdgf_http(f"{api_base}/videos", key, method="POST", body=body, passthrough_4xx=True)
    try:
        obj = json.loads(raw.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ApiError(502, f"创建响应不是 JSON（{len(raw)} 字节）——状态不明，先到渠道控制台核对再决定是否重提") from None
    rec = obj if isinstance(obj, dict) else {}
    task_id = str(rec.get("id") or rec.get("task_id") or "").strip()
    if not task_id:
        raise ApiError(502, "创建响应里没有任务 id——提交状态不明，先到渠道控制台核对，勿重复提交")
    return task_id


def sdgf_query(api_base: str, key: str, task_id: str) -> dict:
    """查一次任务。progress 只透传 0–100 的数字（字符串/越界一律当没有，别让进度条说谎）。"""
    raw = sdgf_http(f"{api_base}/videos/{urllib.parse.quote(task_id, safe='')}", key)
    try:
        obj = json.loads(raw.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ApiError(502, f"查询响应不是 JSON（{len(raw)} 字节）") from None
    rec = obj if isinstance(obj, dict) else {}
    meta = rec.get("metadata") if isinstance(rec.get("metadata"), dict) else {}
    url = str(meta.get("url") or "")
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = ""                                    # 相对路径/空 → 走鉴权 content 端点
    prog = rec.get("progress")
    progress = prog if isinstance(prog, (int, float)) and not isinstance(prog, bool) and 0 <= prog <= 100 else None
    err = rec.get("error") if isinstance(rec.get("error"), dict) else {}
    return {"status": str(rec.get("status") or ""), "progress": progress, "url": url,
            "error": str(err.get("message") or "")}


def sdgf_download(api_base: str, key: str, task_id: str, direct_url: str, dest: Path) -> int:
    """成片落盘，返回字节数。

    `metadata.url` 是完整 http(s) 就直取——**不带鉴权头**（可能落在第三方 CDN 上，别把 key 送出去）；
    相对路径/空 → 走渠道 content 端点（带鉴权）。JSON 错误体与 0 字节都按失败，绝不当成片。
    """
    url, headers = direct_url, {"Accept": "video/mp4,*/*"}
    if not url:
        url = f"{api_base}/videos/{urllib.parse.quote(task_id, safe='')}/content"
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp = dest.with_suffix(".part")
    try:
        with SDGF_DIRECT.open(req, timeout=SDGF_DOWNLOAD_TIMEOUT) as resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if "json" in ctype or "text/" in ctype:
                # 先脱敏后截断（AT-13 §2.8）：顺序反了会漏出被切断的 key 前缀
                text = mask_secret(resp.read(65536).decode("utf-8", errors="replace").strip(), key)[:400]
                raise ApiError(502, f"成片下载拿到的是错误响应：{text}")
            total = 0
            with open(temp, "wb") as fh:
                while True:
                    chunk = resp.read(CHUNK)
                    if not chunk:
                        break
                    fh.write(chunk)
                    total += len(chunk)
        if total <= 0:
            raise ApiError(502, "成片是 0 字节（渠道返回空响应）")
        temp.replace(dest)
        return total
    except urllib.error.HTTPError as err:
        raw = err.read(65536).decode("utf-8", errors="replace").strip()
        text = mask_secret(raw, key)[:400]
        raise ApiError(502, f"成片下载 HTTP {err.code}：{text or err.reason}") from None
    except urllib.error.URLError as err:
        raise ApiError(502, f"成片下载连不上（{err.reason}）") from None
    finally:
        temp.unlink(missing_ok=True)


def sdgf_safe_id(task_id: str) -> str:
    """渠道任务 id → 本地目录名。合法 id 原样用（服务重启后 /file 仍能按 id 找到成片），否则取 hash。"""
    tid = str(task_id or "").strip()
    if tid and ".." not in tid and ID_RE.match(tid):
        return tid
    return "h-" + hashlib.sha1(tid.encode("utf-8")).hexdigest()[:16]


def sdgf_task_dir(task_id: str) -> Path:
    return UPLOADS / "sdgf" / sdgf_safe_id(task_id)


def sdgf_write_record(task_id: str, data: dict) -> None:
    """任务记录落盘（诊断用；不含任何凭证，prompt 只留长度与 sha256）。"""
    try:
        path = sdgf_task_dir(task_id) / SDGF_RECORD
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text(path, json.dumps({**data, "updated": int(time.time())}, ensure_ascii=False, indent=2))
    except OSError:
        pass                                          # 记录写不进去不影响生成本身


def sdgf_host_images(plan: dict) -> tuple[dict, list[str]]:
    """把预演体里的图片占位换成真 URL：URL 直接用；dataUrl 字节先换公网直链（**换完验活**）。

    换不到就报错——宁可失败也不发一条没有参考图（或图拉不到）的请求
    （那会出一版跟提示词无关的片子，渠道还会拒创建并可能扣费）。
    AT-14 B3 A5：同时返回每张图的**实际来源**（(body, sources)，序=images 序）——
    来源 ∈ uguu / tencent-videocache / url，调用点把它当 imageSources 落账。
    AT-16 B12：图源全不可用时这里抛 ImageHostError（502 + stage=host_images），
    automation 认它做**前置拒绝**（零渠道请求、回 awaiting_approval），不记成「结果未知」。
    """
    body = dict(plan["body"])
    urls: list[str] = []
    sources: list[str] = []
    for img in plan["images"]:
        url = img["url"]
        if not url:
            url, source = store_public_image(img["data"], img["name"])
            sources.append(source)
        else:
            sources.append("url")             # 调用方自带的直链：如实标注，不算图床上传
        if not url.lower().startswith("https://"):
            raise ImageHostError(f"参考图 {img['name']} 换公网 URL 失败（图床不可用）",
                                 sources=[{"source": sources[-1], "label": sources[-1],
                                           "reason": "换出来的不是 https 直链"}])
        urls.append(url)
    if urls:
        body["images"] = urls
    else:
        body.pop("images", None)
    return body, sources


def sdgf_job_update(job_id: str, **fields) -> None:
    with JOBS_LOCK:
        rec = JOBS.get(job_id)
        if rec is not None:
            rec.update(fields)


def sdgf_run_job(job_id: str, task_id: str, api_base: str, key: str, meta: dict) -> None:
    """后台线程：轮询渠道 → 下载成片 → 落记录。凭证只在内存里，线程结束即随栈释放。"""
    try:
        sdgf_write_record(task_id, {"job": job_id, "taskId": task_id, "status": "running",
                                    "stage": "生成中", **meta})
        failed_streak = 0
        for _attempt in range(SDGF_POLL_MAX):
            try:
                state = sdgf_query(api_base, key, task_id)
            except ApiError:
                time.sleep(SDGF_POLL_INTERVAL)        # 查询失败不判死：同一任务 id 继续重查
                continue
            status = state["status"]
            if status == "completed":
                sdgf_job_update(job_id, stage="下载中")
                size = sdgf_download(api_base, key, task_id, state["url"],
                                     sdgf_task_dir(task_id) / SDGF_FINAL)
                sdgf_job_update(job_id, status="done", stage="完成", progress=None, bytes=size)
                sdgf_write_record(task_id, {"job": job_id, "taskId": task_id, "status": "done",
                                            "stage": "完成", "bytes": size, **meta})
                return
            if status in ("failed", "cancelled"):
                failed_streak += 1
                if failed_streak >= SDGF_FAILED_RECHECK:
                    raise ApiError(502, f"渠道判失败：{state['error'] or status}（已复核 {failed_streak} 次）")
                time.sleep(SDGF_FAILED_RECHECK_DELAY)   # 站方瞬态 failed 会自愈：隔 8s 再核
                continue
            failed_streak = 0
            if state["progress"] is not None:
                sdgf_job_update(job_id, progress=state["progress"], stage="生成中")
            time.sleep(SDGF_POLL_INTERVAL)
        raise ApiError(504, f"等了 {round(SDGF_POLL_MAX * SDGF_POLL_INTERVAL / 60)} 分钟还没完成；"
                            f"渠道任务 {task_id} 可能仍在跑——本端点没有续查入口，重提会重复计费，先到渠道控制台核对")
    except ApiError as err:
        message = mask_secret(err.msg, key)
        sdgf_job_update(job_id, status="failed", stage="失败", progress=None, error=message)
        sdgf_write_record(task_id, {"job": job_id, "taskId": task_id, "status": "failed",
                                    "stage": "失败", "error": message, **meta})
    except Exception as exc:                          # noqa: BLE001 - 后台线程不能静默死掉
        traceback.print_exc()
        message = mask_secret(f"bridge 内部异常：{exc}", key)
        sdgf_job_update(job_id, status="failed", stage="失败", progress=None, error=message)
        sdgf_write_record(task_id, {"job": job_id, "taskId": task_id, "status": "failed",
                                    "stage": "失败", "error": message, **meta})
    finally:
        SDGF_JOBS.release()


def sdgf_job_state(job_id: str) -> dict:
    """/api/sdgf/video/job/<job>：JOBS 记录 → 轮询状态（progress 只在 running 且是 0–100 数字时透出）。"""
    with JOBS_LOCK:
        rec = JOBS.get(job_id)
        rec = dict(rec) if rec else None
    if rec is None or rec.get("kind") != "sdgf-video":
        raise ApiError(404, "没有这个 SDGF 视频任务号（可能服务重启过，重新发起一次生成即可）")
    status = str(rec.get("status") or "running")
    out: dict = {"status": status, "stage": str(rec.get("stage") or ""),
                 "elapsed": round(time.time() - float(rec.get("started") or time.time()), 1),
                 "taskId": str(rec.get("task_id") or "")}
    prog = rec.get("progress")
    if status == "running" and isinstance(prog, (int, float)) and not isinstance(prog, bool) and 0 <= prog <= 100:
        out["progress"] = prog
    if status == "failed":
        out["error"] = str(rec.get("error") or "生成失败")
    if status == "done" and rec.get("bytes"):
        out["bytes"] = rec["bytes"]
    return out


# ---------------------------------------------------------------- 子进程调用


def run_py(args: list[str], timeout: int, extra_env: dict | None = None) -> tuple[int, str, str]:
    child_env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    if extra_env:
        child_env.update(extra_env)
    proc = subprocess.run(
        [PY, *args],
        cwd=str(ROOT),
        capture_output=True,
        timeout=timeout,
        env=child_env,
    )
    out = proc.stdout.decode("utf-8", errors="replace")
    err = proc.stderr.decode("utf-8", errors="replace")
    return proc.returncode, out, err


def tail(text: str, limit: int = 1200) -> str:
    text = (text or "").strip()
    return text[-limit:]


def slug(text: str) -> str:
    s = re.sub(r"[^0-9A-Za-z_-]+", "-", text).strip("-")
    return (s[:32] or "task").lower()


def new_task_id(video: Path) -> str:
    base = slug(video.stem)
    candidate, n = base, 1
    while (ANALYSIS / candidate).exists():
        n += 1
        candidate = f"{base}-{n}"
    return candidate


# ---------------------------------------------------------------- 视频上传 / 建任务


def head_sha256(path: Path, limit: int = 1024 * 1024) -> str:
    """前 1MB 的 sha256——与 analyze.py 的指纹算法逐字节一致（同一条片子才认得出）。"""
    digest = hashlib.sha256()
    remaining = limit
    with open(path, "rb") as fh:
        while remaining > 0:
            chunk = fh.read(min(CHUNK, remaining))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
    return digest.hexdigest()


def rel_path(path: Path) -> str:
    """仓库内路径记相对（与 analyze.py 的 display_path 同款），指纹才能跨机器对账。"""
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except (ValueError, OSError):
        return str(path)


def clean_video_name(raw: str) -> tuple[str, str]:
    """→ (stem, ext)。清隐形字符、剥目录、挡掉非法字符；只认 mp4/mov/webm。"""
    name = INVISIBLE_RE.sub("", unicodedata.normalize("NFC", str(raw or "")))
    name = name.replace("\\", "/").rsplit("/", 1)[-1]      # 去掉任何目录成分
    name = UNSAFE_RE.sub("-", name).strip(" .")
    stem, dot, ext = name.rpartition(".")
    if not dot:
        raise ApiError(400, f"认不出文件格式：{raw or '(无文件名)'}（只收 MP4 / MOV / WEBM）")
    ext = ext.lower()
    if ext not in VIDEO_EXT:
        raise ApiError(400, f"只收 MP4 / MOV / WEBM，收到 .{ext}")
    stem = re.sub(r"\s+", " ", stem).strip(" .")[:60] or "video"
    return stem, ext


def sniff_video(head: bytes, ext: str) -> bool:
    """看一眼文件头：扩展名对不上内容就早点拒——否则 /video/<id> 会 200 但播放器放不出来。"""
    if ext == "webm":
        return head[:4] == b"\x1aE\xdf\xa3"               # EBML
    if len(head) >= 8 and head[4:8] == b"ftyp":           # mp4 / mov 标准起手
        return True
    return head[4:8] in (b"moov", b"mdat", b"wide", b"free", b"skip", b"pnot", b"uuid")


def task_id_base(stem: str) -> str:
    base = slug(stem)
    if base == "task":        # slug() 把非 ASCII 全吃掉；纯中文名退化成这个，改用短哈希
        base = "video-" + hashlib.sha1(stem.encode("utf-8")).hexdigest()[:8]
    return base[:48]


def unique_task_id(base: str) -> str:
    candidate, n = base, 1
    while (ANALYSIS / candidate).exists():
        n += 1
        candidate = f"{base}-{n}"
    return candidate


def write_fingerprint(task_id: str, video: Path) -> dict:
    """照 analyze.py 的字段名写 .源视频指纹.json（/video/<id> 靠「路径」定位源视频）。"""
    stat = video.stat()
    meta = {
        "路径": rel_path(video),
        "大小": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "前1MB_sha256": head_sha256(video),
        "记录时间": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    path = ANALYSIS / task_id / FP_NAME
    with WRITE_LOCK:
        write_text(path, json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
    return meta


def place_upload(temp: Path, stem: str, ext: str) -> Path:
    """把临时文件挪进 uploads/；重名不覆盖（x.mp4 → x-2.mp4）。O_EXCL 占位防同名并发。"""
    UPLOADS.mkdir(parents=True, exist_ok=True)
    n = 0
    while True:
        n += 1
        cand = UPLOADS / (f"{stem}.{ext}" if n == 1 else f"{stem}-{n}.{ext}")
        try:
            os.close(os.open(cand, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        except FileExistsError:
            continue
        try:
            os.replace(temp, cand)
        except OSError:
            cand.unlink(missing_ok=True)
            raise
        return cand


def create_task_from_upload(video: Path) -> str:
    """给刚上传的视频建 `analysis/reverse/<id>/` + 指纹。不写别的产物（拆解要花钱，得人点头）。"""
    task_id = unique_task_id(task_id_base(video.stem))
    (ANALYSIS / task_id).mkdir(parents=True, exist_ok=True)
    write_fingerprint(task_id, video)
    return task_id


def pending_task_for(video: Path) -> str | None:
    """同一条源视频、已由上传建了目录但还没有 拆解.md → 复用那个 id。

    不复用的话 /api/analyze 会另建 `<id>-2`，留下一个只有指纹的空壳目录。
    """
    if not ANALYSIS.is_dir():
        return None
    for d in sorted(ANALYSIS.iterdir()):
        if not d.is_dir() or (d / "拆解.md").is_file():
            continue
        src = video_file(d.name, strict=False)
        if src is None:
            continue
        try:
            if src.resolve() == video:
                return d.name
        except OSError:
            continue
    return None


# ---------------------------------------------------------------- 图片命名


def import_asset_namer():
    sys.path.insert(0, str(ROOT / "pipeline" / "reverse"))
    import asset_name  # noqa: PLC0415 - 延迟导入，避免服务启动时依赖 API
    return asset_name


def detect_ext(filename: str, blob: bytes) -> str:
    suffix = Path(filename).suffix.lower().lstrip(".")
    if suffix in IMAGE_EXT:
        return "jpg" if suffix == "jpeg" else suffix
    for magic, ext in MAGIC:
        if blob.startswith(magic):
            return ext
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "webp"
    raise ApiError(400, f"认不出图片格式：{filename or '(无文件名)'}")


def save_asset(task_id: str, no: int, filename: str, blob: bytes) -> Path:
    if no < 1:
        raise ApiError(400, "no 必须是 ≥1 的整数")
    ext = detect_ext(filename, blob)
    d = assets_dir(task_id)
    for old in d.glob(f"图片{no}.*"):          # 同序号重传 = 覆盖
        if old.is_file() and old.name != f"图片{no}.{ext}":
            old.unlink()
    path = d / f"图片{no}.{ext}"
    with WRITE_LOCK:
        path.write_bytes(blob)
    return path


def name_and_store(task_id: str, no: int, path: Path) -> tuple[str, str, str]:
    """调 asset_name 识别 → 写回清单命名列和描述列。→ (名字, 描述, 失败原因)"""
    kind = "主体"
    for row in parse_checklist(task_id):
        if row["no"] == no:
            kind = row["kind"] if row["kind"] in ("主体", "服装", "人物", "场景", "道具") else "主体"
            break
    try:
        mod = import_asset_namer()
        name, desc = mod.identify(path, kind)
    except Exception as err:                   # noqa: BLE001 - 命名失败不能拖垮上传
        return "", "", f"自动命名失败：{getattr(err, 'msg', None) or err}"
    if name:
        set_checklist_name(task_id, no, name)
    # 只有已有人工描述时才保护它；自动识别描述仅负责预填空列。
    current = next((r for r in parse_checklist(task_id) if r["no"] == no), None)
    if desc and not (current and current.get("my_desc", "").strip()):
        set_checklist_desc(task_id, no, desc)
    return name, desc, ""


def naming_state(task_id: str, no: int) -> dict:
    """→ {state: running|done|failed|unknown, name, warning, elapsed}。"""
    with NAMING_LOCK:
        st = NAMING.get((task_id, no))
        if st is None:
            return {"no": no, "state": "unknown", "name": "", "desc": "", "warning": "", "elapsed": 0.0}
        out = dict(st)
    if out.get("state") == "running":
        out["elapsed"] = round(time.time() - float(out.get("started") or time.time()), 1)
    return out


def start_naming(task_id: str, no: int, path: Path) -> None:
    """后台跑识别命名，立刻返回。落盘与命名拆开：用户先看到图，再看到名字。"""
    with NAMING_LOCK:
        NAMING[(task_id, no)] = {"state": "running", "name": "", "desc": "", "warning": "",
                                 "started": time.time(), "elapsed": 0.0}

    def work() -> None:
        try:
            name, desc, warning = name_and_store(task_id, no, path)
        except Exception as err:               # noqa: BLE001 - 后台线程不能静默死掉
            traceback.print_exc()
            name, desc, warning = "", "", f"自动命名失败：{getattr(err, 'msg', None) or err}"
        with NAMING_LOCK:
            started = float((NAMING.get((task_id, no)) or {}).get("started") or time.time())
            NAMING[(task_id, no)] = {
                "state": "failed" if warning else "done",
                "name": name, "desc": desc, "warning": warning,
                "started": started, "elapsed": round(time.time() - started, 1),
            }
            # 清掉过期状态，别让常开着的服务只涨不落
            cutoff = time.time() - NAMING_KEEP
            for key, val in list(NAMING.items()):
                if val.get("state") != "running" and float(val.get("started") or 0) < cutoff:
                    NAMING.pop(key, None)

    threading.Thread(target=work, name=f"asset-name-{task_id}-{no}", daemon=True).start()


# ---------------------------------------------------------------- multipart


def parse_multipart(body: bytes, boundary: str) -> tuple[dict[str, str], dict[str, tuple[str, bytes]]]:
    fields: dict[str, str] = {}
    files: dict[str, tuple[str, bytes]] = {}
    sep = b"--" + boundary.encode("latin-1")
    for chunk in body.split(sep):
        if not chunk or chunk in (b"--\r\n", b"--"):
            continue
        chunk = chunk[2:] if chunk.startswith(b"\r\n") else chunk
        if chunk.endswith(b"\r\n"):
            chunk = chunk[:-2]
        head, _, data = chunk.partition(b"\r\n\r\n")
        if not _:
            continue
        text = head.decode("utf-8", errors="replace")
        m = re.search(r'name="([^"]*)"', text)
        if not m:
            continue
        field = m.group(1)
        fm = re.search(r'filename="([^"]*)"', text)
        if fm is not None:
            files[field] = (fm.group(1), data)
        else:
            fields[field] = data.decode("utf-8", errors="replace")
    return fields, files


# ---------------------------------------------------------------- 资产位截帧（T3-J2）

# 资产位节点要「原片截图」。截帧在 bridge 侧做，理由同拉片/渲染：ffmpeg 不进浏览器。
# 入参只有 task_id + 资产名——源视频路径由 .源视频指纹.json 查回，时间区间由 拆解.md 的
# 「出现于：00:00-02.03、04.07-06.00」解析，取第一段的中点。前端不传视频、不传时间。
FRAME_DIR = "frames"
FRAME_MAX_WIDTH = 960
SPAN_TEXT_RE = re.compile(r"出现于[：:]\s*([^）)\n]+)")
SPAN_RANGE_RE = re.compile(r"(\d{1,2}(?::\d{1,2})?(?:\.\d{1,2})?)\s*[-–—~]\s*(\d{1,2}(?::\d{1,2})?(?:\.\d{1,2})?)")


def parse_timecode(token: str, duration: float = 0.0) -> float | None:
    """IR 的时间码混用：`00:00`（分:秒）与 `02.03`（秒.百分秒）。

    `06:00` 在 6.85 秒的片子里是 6 秒而不是 6 分——按分:秒读出来超过时长就退回按秒读。
    """
    raw = str(token or "").strip().replace("：", ":")
    if not raw:
        return None
    if ":" in raw:
        head, _, tail = raw.partition(":")
        try:
            value = float(head) * 60 + float(tail)
        except ValueError:
            return None
        if duration > 0 and value > duration:
            try:
                return float(raw.replace(":", "."))
            except ValueError:
                return None
        return value
    try:
        return float(raw)
    except ValueError:
        return None


def video_duration(video: Path) -> float:
    """ffprobe 读时长；读不到就 0（后面只影响越界保护，不影响截帧）。"""
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(video)],
            capture_output=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 0.0
    try:
        return float(proc.stdout.decode("utf-8", "replace").strip().splitlines()[0])
    except (IndexError, ValueError):
        return 0.0


def asset_span(task_id: str, name: str, duration: float = 0.0) -> tuple[float, float] | None:
    """拆解.md 里这条资产的「出现于」第一段区间（秒）。找不到 → None（截帧退到首帧附近）。"""
    path = ANALYSIS / task_id / "拆解.md"
    if not path.is_file():
        return None
    for line in read_text(path).splitlines():
        if name not in line:
            continue
        span = SPAN_TEXT_RE.search(line)
        if not span:
            continue
        hit = SPAN_RANGE_RE.search(span.group(1))
        if not hit:
            continue
        start = parse_timecode(hit.group(1), duration)
        end = parse_timecode(hit.group(2), duration)
        if start is None or end is None or end <= start:
            continue
        return start, end
    return None


def jpeg_size(path: Path) -> tuple[int, int]:
    """JPEG 的 SOF 段读宽高（只为落节点 naturalWidth/Height，不值得再起一次 ffprobe）。"""
    data = path.read_bytes()
    index = 2
    while index + 9 < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3):
            height = (data[index + 5] << 8) | data[index + 6]
            width = (data[index + 7] << 8) | data[index + 8]
            return width, height
        index += 2 + ((data[index + 2] << 8) | data[index + 3])
    return 0, 0


def build_asset_frame(task_id: str, name: str) -> dict:
    """资产位截帧：返回帧图路径 + 时刻 + 尺寸；同一 (任务, 资产, 时刻) 命中缓存不重跑 ffmpeg。"""
    video = video_file(task_id)
    duration = video_duration(video)
    span = asset_span(task_id, name, duration)
    if span:
        at = round((span[0] + span[1]) / 2, 2)
    else:
        # 没有出现区间：退到片头附近的一帧。宁可给一张「不太准的原片」，也不要一张白卡（所有者原话）。
        at = round(max(0.0, min(0.4, (duration or 0.8) / 10)), 2)
    out_dir = task_dir(task_id) / "assets" / FRAME_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    # 文件名带资产名哈希：slug() 只留 ASCII，「人物A / 服装A」都会塌成 a，同刻就撞同一张缓存。
    tag = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
    path = out_dir / f"{slug(name)}-{tag}-{at:.2f}.jpg"
    if not path.is_file() or path.stat().st_size <= 0:
        cmd = [
            "ffmpeg", "-v", "error", "-nostdin", "-y",
            "-ss", f"{at:.2f}", "-i", str(video), "-frames:v", "1",
            "-vf", "scale=w=min(960\\,iw):h=-2",
            "-q:v", "3", str(path),
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ApiError(500, f"截帧跑不起来：{exc}") from None
        if proc.returncode != 0 or not path.is_file() or path.stat().st_size <= 0:
            raise ApiError(500, "截帧失败：" + (tail(proc.stderr.decode("utf-8", "replace")) or f"退出码 {proc.returncode}"))
    width, height = jpeg_size(path)
    url = f"/api/asset-frame/{urllib.parse.quote(task_id)}?name={urllib.parse.quote(name)}&raw=1"
    return {"ok": True, "t": at, "approx": span is None, "span": list(span) if span else None,
            "width": width, "height": height, "url": url, "path": str(path)}


# ---------------------------------------------------------------- HTTP


class Handler(BaseHTTPRequestHandler):
    server_version = "WanUI/1.0"
    protocol_version = "HTTP/1.1"

    # -- 输出 ------------------------------------------------------
    def send_body(self, data: bytes, status: int = 200, ctype: str = "application/octet-stream",
                  extra: dict[str, str] | None = None) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            origin = self.headers.get("Origin") or ""
            if origin in CANVAS_ORIGINS:          # 画布 dev（:3000）fetch 白名单
                self.send_header("Access-Control-Allow-Origin", origin)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            # 客户端已经断开（刷新/关标签/取消上传）。socket 死了，没什么可写的，
            # 安静收工即可——不能让它冒到 dispatch 变成一条假的 500。
            self.close_connection = True

    def send_json(self, obj, status: int = 200) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_body(data, status, "application/json; charset=utf-8")

    def query_param(self, key: str) -> str:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        return (q.get(key) or [""])[0]

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write(f"[ui] {self.address_string()} {fmt % args}\n")

    def handle_one_request(self) -> None:
        """浏览器关 keep-alive 连接会打断 readline，抛 ConnectionResetError。

        这不是错误：请求早已正常返回 200，只是客户端不打招呼就断开了
        （刷新页面、关标签、headless 截图退出都会这样）。默认实现会把整段
        traceback 打到 stderr，双击 启动UI.bat 的人看到满屏红字会以为坏了。
        """
        try:
            super().handle_one_request()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            self.close_connection = True

    # -- 入参 ------------------------------------------------------
    def read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def read_json(self) -> dict:
        body = self.read_body()
        if not body:
            return {}
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ApiError(400, "请求体不是合法 JSON") from None
        if not isinstance(payload, dict):
            raise ApiError(400, "请求体必须是 JSON 对象")
        return payload

    def _rows_param(self, payload: dict) -> list[int]:
        """确认组端点的 rows 参数：非空整数数组（清单行号）。"""
        raw = (payload or {}).get("rows")
        if not isinstance(raw, list) or not raw:
            raise ApiError(400, "rows 必须是非空数组（待确认清单的行号）")
        try:
            return sorted({int(x) for x in raw})
        except (TypeError, ValueError):
            raise ApiError(400, "rows 必须是整数数组") from None

    # -- 路由 ------------------------------------------------------
    def do_GET(self) -> None:
        self.dispatch("GET")

    def do_DELETE(self) -> None:
        # AT-22 E4：撤销豁免走 DELETE（body 同 POST 的 {行, 词}，或 {"全部": true}）。
        # dispatch 把非 GET 一律交给 route_post，路由内按 self.command 分流。
        self.dispatch("DELETE")

    def do_HEAD(self) -> None:
        self.dispatch("GET")

    def gate(self) -> bool:
        """公网口径：设了 WAN_TOKEN 就要求口令，没设就直通（本机开发行为不变）。

        `?token=…` 校验通过后种 HttpOnly Cookie 并 302 到不带 token 的地址，
        这样刷新、点链接、下载图片都不会再把口令写进历史记录。
        """
        if not TOKEN:
            return True
        # 本机直连（Host 是 127.0.0.1/localhost）不用口令；隧道过来的请求 Host 是公网域名，
        # 客户端地址同样是 127.0.0.1，所以必须靠 Host 头区分，不能只看 client_address。
        host = (self.headers.get("Host") or "").split(":")[0].strip("[]").lower()
        # v1.0.0 公网：vite proxy 会把 Host 改写成 127.0.0.1:8776，Host 判定失效——
        # 以 X-Forwarded-Forwarded 链为准：CF/隧道链路必带真实客户端 IP，出现非私网 IP 一律走口令。
        xff = (self.headers.get("X-Forwarded-For") or "")
        ips = [ip.strip() for ip in xff.split(",") if ip.strip()]
        def _private(ip: str) -> bool:
            return ip.startswith(("127.", "10.", "192.168.", "172.", "::1", "localhost")) or ip == ""
        if any(not _private(ip) for ip in ips):
            pass  # 有公网来源 IP：落到下方口令校验，Host 是什么都不豁免
        elif host in ("127.0.0.1", "localhost", "::1"):
            return True
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        given = (query.get("token") or [""])[0].strip()
        if given and secrets.compare_digest(given, TOKEN):
            rest = [(k, v) for k, vs in query.items() if k != "token" for v in vs]
            # v1.0.0 画布公网（隧道→vite:3000，bridge 走 /api 代理）：token 入口直接落到画布首页，
            # cookie 已种（同域），甲方再访问任意 /api 均通过。
            target = "/"
            self.send_response(302)
            self.send_header("Location", target)
            self.send_header("Set-Cookie", f"wan_auth={TOKEN}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False
        cookie = self.headers.get("Cookie") or ""
        m = re.search(r"(?:^|;\s*)wan_auth=([^;]+)", cookie)
        if m and secrets.compare_digest(m.group(1).strip(), TOKEN):
            return True
        self.send_body(
            "需要访问口令。\n用法：在地址后面加 ?token=<口令>，例如 https://<域名>/?token=…\n"
            "（这个站点会调用付费模型，所以默认不对外开放。）".encode("utf-8"),
            401, "text/plain; charset=utf-8",
        )
        return False

    def do_POST(self) -> None:
        self.dispatch("POST")

    def do_OPTIONS(self) -> None:
        origin = self.headers.get("Origin") or ""
        rmethod = (self.headers.get("Access-Control-Request-Method") or "").upper()
        if origin in CANVAS_ORIGINS and rmethod:
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Filename")
            self.send_header("Access-Control-Max-Age", "86400")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_body(b"{}", 404, "application/json")

    def dispatch(self, method: str) -> None:
        if method == "OPTIONS":
            self.do_OPTIONS()
            return
        try:
            if not self.gate():
                return
            path = urllib.parse.urlparse(self.path).path
            if method == "GET":
                self.route_get(path)
            else:
                self.route_post(path)
        except ApiError as err:
            self.send_json({"error": err.msg, **({"stage": err.stage} if err.stage else {}), **err.extra},
                           err.status)
        except hub_api.HubError as e:      # 工作台业务错（状态不对/文件缺失）→ 4xx，原文给员工看
            # AT-13 §2.2：配置不合法（HubInputError）→ 400，不是状态冲突 409
            extra = getattr(e, "extra", None)
            self.send_json({"error": str(e), **(extra if isinstance(extra, dict) else {})},
                           getattr(e, "status", 409))
        except subprocess.TimeoutExpired:
            self.send_json({"error": "子进程超时"}, 500)
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            # 客户端在写回途中跑了（刷新、关标签、上传到一半取消）。
            # 这不是服务故障：既不该记 500，也不该再往一个已死的 socket 写错误响应。
            self.close_connection = True
        except Exception:                       # noqa: BLE001 - 兜底：绝不把异常写成 0/空
            traceback.print_exc()
            try:
                self.send_json({"error": "服务内部异常，见终端 traceback"}, 500)
            except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
                self.close_connection = True

    def route_get(self, path: str) -> None:
        # ---- 自动化工作台（hub）：任务列表/详情/成片/job 轮询 ----
        if path == "/api/hub/tasks":
            self.send_json({"tasks": hub_api.list_tasks()})
            return
        if path == "/api/hub/where":
            # 数据根自述（诊断面）：探针跑前据此断言「服务端写入目录 == 探针读取目录」，
            # 读/写分家的伪隔离直接拒跑（AT-14 B2 P0：真实任务 auto-0001 被误删）。
            # AT-14 B3 A2：附上传渠道计数（local/uguu/tencent），装配链自述可对账。
            self.send_json({**hub_api.where(), "asset_counters": dict(ASSET_COUNTERS)})
            return
        m = re.fullmatch(r"/api/hub/asset-file/([^/]+)/([^/]+)", path)
        if m:
            # AT-14 B3 A：本地引用取图（local:任务号/asset-uploads/文件名 的读面）。
            # 为什么不能复用现有 /assets/ 路由：那条按 bridge_id 取 bridge 资产目录
            # （analysis/reverse/<bridge_id>/assets）、且只认 图片N 扩展名；本地引用按
            # automation local_id 分任务目录、内容寻址命名（16位hex），且在上传时就
            # 要能取（拆解/桥任务存在之前）——两套命名两套根，塞不进同一个 handler。
            local_id = urllib.parse.unquote(m.group(1))
            name = urllib.parse.unquote(m.group(2))
            try:
                blob = local_assets.read(f"local:{local_id}/asset-uploads/{name}")
            except local_assets.LocalAssetError as e:
                raise ApiError(404, str(e)) from None
            mime = {".png": "image/png", ".jpg": "image/jpeg",
                    ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(
                        Path(name).suffix.lower(), "application/octet-stream")
            self.send_body(blob, 200, mime)
            return
        m = re.fullmatch(r"/api/hub/task/([^/]+)", path)
        if m:
            self.send_json({"task": hub_api.get_task(urllib.parse.unquote(m.group(1)))})
            return
        if path == "/api/lapian/slot":
            # 拆解槽真相（只读）：hub 侧用它把「槽忙」翻成人话（在等谁的远程返回、预计还需多久）
            self.send_json(lapian_slot_view())
            return
        m = re.fullmatch(r"/api/hub/file/([^/]+)", path)
        if m:
            f = hub_api.output_file(urllib.parse.unquote(m.group(1)))
            self.send_body_bytes_or_file(f)
            return
        m = re.fullmatch(r"/api/hub/job/([^/]+)", path)
        if m:
            self.send_json(hub_api.job_status(urllib.parse.unquote(m.group(1))))
            return
        m = re.fullmatch(r"/api/hub/frame/([^/]+)/([^/]+)", path)
        if m:
            # AT-8 B2：置换台帧图（看帧缓存 / at8 派生帧同目录；只认 png/jpg，不认目录穿越）
            f = hub_api.frame_file(urllib.parse.unquote(m.group(1)),
                                   urllib.parse.unquote(m.group(2)))
            self.send_body(f.read_bytes(), 200,
                           MIME.get(f.suffix.lower().lstrip("."), "image/png"))
            return
        m = re.fullmatch(r"/api/hub/render/([^/]+)", path)
        if m:
            self.send_json(hub_api.do_render(urllib.parse.unquote(m.group(1))))
            return
        # ---- 图床直链（M4）：https://vc-t.aistar.work/vc/<key> → uploads/vc/<key> ----
        if path.startswith("/vc/"):
            rel = path[len("/vc/"):]
            target = (UPLOADS / "vc" / rel).resolve()
            if not str(target).startswith(str((UPLOADS / "vc").resolve())) or not target.is_file():
                raise ApiError(404, "图床里没有这个文件")
            mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(target.suffix.lower(), "application/octet-stream")
            self.send_body(target.read_bytes(), 200, mime)
            return
        if path in ("/", "/index.html"):
            index = ui_index()
            if not index.is_file():
                raise ApiError(500, "ui/index.html 不见了" if WEB_DIST_ROOT is None
                               else "web-dist/index.html 不见了")
            self.send_body(index.read_bytes(), 200, "text/html; charset=utf-8")
            return
        if path == "/api/videos":
            self.send_json({"videos": list_videos()})
            return
        if path == "/api/tasks":
            self.send_json({"tasks": list_tasks()})
            return
        # ---- 整合包：面布前端首启导入渠道预置（app/config/channel-config.json）----
        if path == "/api/bootstrap-config":
            preset = ROOT / "config" / "channel-config.json"
            if not preset.is_file():
                raise ApiError(404, "no preset")
            self.send_body(preset.read_bytes(), 200, "application/json")
            return
        # ---- 画布 bridge（M3）：三个只读 API，供画布前端（:3000）fetch -------
        if path == "/api/specs":
            sys.path.insert(0, str(ROOT / "pipeline" / "reverse"))
            from spec_registry import load_registry
            self.send_json({"specs": load_registry()})
            return
        m = re.fullmatch(r"/api/lapian/([^/]+)", path)
        if m:
            self.send_json(lapian_payload(urllib.parse.unquote(m.group(1)),
                                          self.query_param("spec") or "jimeng-lapian"))
            return
        m = re.fullmatch(r"/api/render/([^/]+)/([^/]+)", path)
        if m:
            self.send_json(render_payload(urllib.parse.unquote(m.group(1)),
                                          urllib.parse.unquote(m.group(2)),
                                          self.query_param("optional"),
                                          self.query_param("audio_mode"),
                                          self.query_param("checklist_task")))
            return
        m = re.fullmatch(r"/api/job/([0-9a-f]+)", path)
        if m:
            with JOBS_LOCK:
                rec = JOBS.get(m.group(1))
                rec = dict(rec) if rec else None
            if rec is None:
                raise ApiError(404, "没有这个任务号（可能服务重启过，重开一次拆解即可）")
            payload = {"status": rec.get("status")}
            if rec.get("status") == "done":
                payload["result"] = rec.get("result")
                payload["elapsed"] = rec.get("elapsed")
            elif rec.get("status") == "error":
                # error 文案原样全量透传（前端 error 态要展示全文），缺理由时给兜底话
                payload["error"] = str(rec.get("error") or "拆解失败（bridge 没记下原因，重查一次或看 bridge.log）")
            else:
                payload["elapsed"] = round(time.time() - float(rec.get("started") or time.time()), 1)
                # running 态顺带读该任务 进度.json 透出当前阶段（M6-T6F）；读不到就缺省，不影响原响应
                stage_rec = read_progress(rec.get("task_id"))
                stage_name = str(stage_rec.get("stage") or "") if stage_rec else ""
                if stage_name:
                    payload["stage"] = stage_name
                    if stage_rec.get("ts"):
                        payload["stage_ts"] = stage_rec.get("ts")
            self.send_json(payload)
            return
        m = re.fullmatch(r"/api/wan/video/job/([^/]+)", path)
        if m:
            self.send_json(wan_video_job_state(urllib.parse.unquote(m.group(1))))
            return
        m = re.fullmatch(r"/api/wan/video/state/([^/]+)", path)
        if m:
            # AT-13 §2.3/§5：按承载目录读记录（重启后 JOBS 丢失的续查路径）
            self.send_json(wan_state_by_task(urllib.parse.unquote(m.group(1))))
            return
        m = re.fullmatch(r"/api/wan/video/file/([^/]+)", path)
        if m:
            self.serve_wan_video(urllib.parse.unquote(m.group(1)))
            return
        m = re.fullmatch(r"/api/sdgf/video/job/([^/]+)", path)
        if m:
            self.send_json(sdgf_job_state(urllib.parse.unquote(m.group(1))))
            return
        m = re.fullmatch(r"/api/sdgf/video/file/([^/]+)", path)
        if m:
            self.serve_sdgf_video(urllib.parse.unquote(m.group(1)))
            return
        m = re.fullmatch(r"/api/generate/([^/]+)", path)
        if m:
            self.send_json(gen_state(urllib.parse.unquote(m.group(1))))
            return
        m = re.fullmatch(r"/genvideo/([^/]+)", path)
        if m:
            self.serve_generated(urllib.parse.unquote(m.group(1)))
            return
        m = re.fullmatch(r"/api/task/([^/]+)", path)
        if m:
            self.send_json(task_payload(urllib.parse.unquote(m.group(1))))
            return
        m = re.fullmatch(r"/api/recheck/([^/]+)", path)
        if m:
            tid = urllib.parse.unquote(m.group(1))
            task_dir(tid)
            self.send_json(recheck_call(tid))
            return
        m = re.fullmatch(r"/api/naming/([^/]+)/(\d+)", path)
        if m:
            tid = urllib.parse.unquote(m.group(1))
            task_dir(tid)
            self.send_json(naming_state(tid, int(m.group(2))))
            return
        m = re.fullmatch(r"/api/zip/([^/]+)", path)
        if m:
            self.download_zip(urllib.parse.unquote(m.group(1)))
            return
        m = re.fullmatch(r"/api/asset-frame/([^/]+)", path)
        if m:
            # T3-J2：资产位截帧。?meta 默认返回 JSON（含 url），?raw=1 直接回 JPEG 给 <img>。
            tid = urllib.parse.unquote(m.group(1))
            name = (self.query_param("name") or "").strip()
            if not name:
                raise ApiError(400, "要带 name=<资产名>（拆解.md 里资产行的名字）")
            task_dir(tid)                        # 校验任务 id（不合法/不存在 → 400/404）
            info = build_asset_frame(tid, name)
            if self.query_param("raw") == "1":
                self.send_body(Path(info["path"]).read_bytes(), 200, "image/jpeg")
                return
            info.pop("path", None)
            self.send_json(info)
            return
        m = re.fullmatch(r"/assets/([^/]+)/([^/]+)", path)
        if m:
            # 整合包形态：Vite 构建产物（/assets/index-*.js 等）与任务资产图共用 /assets/ 前缀，
            # web-dist 里真有这个文件就先按静态给；没有才走任务资产路由（行为与开发机一致）。
            if self.try_static(path):
                return
            self.serve_asset(urllib.parse.unquote(m.group(1)), urllib.parse.unquote(m.group(2)))
            return
        m = re.fullmatch(r"/video/([^/]+)", path)
        if m:
            self.serve_video(urllib.parse.unquote(m.group(1)))
            return
        # ---- 整合包形态兜底：现有路由都没命中且非 /api/* → web-dist 静态文件 ----
        # 目录/不存在回退 index.html（SPA path 路由 /canvas/<id>）；/api/* 照旧 404 JSON。
        if WEB_DIST_ROOT is not None and not path.startswith("/api/"):
            self.serve_static(path)
            return
        raise ApiError(404, f"没有这个地址：{path}")

    def route_post(self, path: str) -> None:
        # ---- 自动化工作台（hub）：建任务/拆解/置换/确认/渲染/批准/提交 ----
        if path == "/api/hub/maintenance/scan-lost-runs":
            # AT-18 §4.1：假运行态对账扫描（手动/定时同一入口）。默认 dry-run 只返回清单；
            # apply=true 才真的降级（幂等：已 failed 的下一轮进 skipped{not_running}）。
            payload = self.read_json()
            self.send_json(hub_api.scan_lost_runs(apply=payload.get("apply") is True))
            return
        if path == "/api/hub/maintenance/sweep-residue":
            # t_4f779245：删除通道残留清扫（隔离箱 `.trash/` + 无 任务.json 的孤儿目录）。
            # 同样默认 dry-run 只报清单；apply=true 才真删（启动对账/watchdog 走 apply=True）。
            payload = self.read_json()
            self.send_json(hub_api.sweep_residue(apply=payload.get("apply") is True))
            return
        if path == "/api/hub/ingest":
            self.do_hub_ingest()
            return
        m = re.fullmatch(r"/api/hub/asset-probe/([^/]+)", path)
        if m:
            # AT-14 B3 A3：该任务全部资产引用健康态（local/ok/expired/unknown + 显示URL）
            self.read_json()                    # 无字段，读掉请求体保持 keep-alive 干净
            self.send_json(hub_api.probe_assets(urllib.parse.unquote(m.group(1))))
            return
        m = re.fullmatch(r"/api/hub/discuss/([^/]+)", path)
        if m:
            self.send_json(hub_api.start_discuss(urllib.parse.unquote(m.group(1))))
            return
        m = re.fullmatch(r"/api/hub/assets/([^/]+)", path)
        if m:
            self.do_hub_assets(urllib.parse.unquote(m.group(1)))
            return
        m = re.fullmatch(r"/api/hub/confirm/([^/]+)", path)
        if m:
            # AT-9 B2：body 可带 empty_confirm:true = 全沿用（空置换显式确认 → rendering）
            # AT-9 B5：body 可带 按原样:[行号] = 确认时点「知道，按原样」勾选（自检③锁点对账数据源）
            payload = self.read_json()
            raw_acks = payload.get("按原样")
            try:
                ack_rows = sorted({int(x) for x in raw_acks}) if raw_acks else []
            except (TypeError, ValueError):
                raise ApiError(400, "按原样 必须是行号数组") from None
            self.send_json(hub_api.do_confirm(urllib.parse.unquote(m.group(1)),
                                              empty_confirm=bool(payload.get("empty_confirm")),
                                              ack_rows=ack_rows))
            return
        m = re.fullmatch(r"/api/hub/render/([^/]+)", path)
        if m:
            self.send_json(hub_api.do_render(urllib.parse.unquote(m.group(1))))
            return
        m = re.fullmatch(r"/api/hub/approve/([^/]+)", path)
        if m:
            # AT-13 §2.2/§三：body 可带 config（画布生成配置：渠道身份/model/mode/params，**无 key**）
            # → 服务端白名单取键（hub_api._normalize_config 校验+构造）→ 固化+算 v2 快照 → approved。
            # 不带 config = 旧语义（v1 快照，老任务/CLI），行为零变化。
            payload = self.read_json()
            config = payload.get("config")
            if config is not None and not isinstance(config, dict):
                raise ApiError(400, "config 必须是 JSON 对象（{渠道身份,model,mode,params}）")
            self.send_json(hub_api.do_approve(urllib.parse.unquote(m.group(1)), config=config))
            return
        m = re.fullmatch(r"/api/hub/submit/([^/]+)", path)
        if m:
            # AT-13 §三：body 可带 key（一次性，不落盘、不进 argv/URL/日志）→ start_submit 透传
            payload = self.read_json()
            key = payload.get("key")
            if key is not None and not isinstance(key, str):
                raise ApiError(400, "key 必须是字符串（渠道密钥，提交时一次性透传）")
            self.send_json(hub_api.start_submit(urllib.parse.unquote(m.group(1)),
                                                key=(key or "").strip() or None))
            return
        m = re.fullmatch(r"/api/hub/task/([^/]+)/release", path)
        if m:
            # AT-13 §2.3：人工解除「未知收单」阻断（核对渠道无单之后，身体带 confirm:true）
            payload = self.read_json()
            self.send_json(hub_api.release_submit_unknown(
                urllib.parse.unquote(m.group(1)),
                confirm=payload.get("confirm") is True,
                note=str(payload.get("note") or "")))
            return
        m = re.fullmatch(r"/api/hub/task/([^/]+)/recover", path)
        if m:
            # AT-13 §2.4：generating 孤儿受控恢复（四守卫，confirm:true 才动）
            payload = self.read_json()
            self.send_json(hub_api.recover_orphan(urllib.parse.unquote(m.group(1)),
                                                  confirm=payload.get("confirm") is True))
            return
        m = re.fullmatch(r"/api/hub/task/([^/]+)/audio-mode", path)
        if m:
            # AT-17 B3：确认流改选音频三态 → 落盘 + 按新三态重渲染 + 门禁重判（hub_api 一处收口）。
            # body: {"audio_mode": "沿用原片|按新主体调整|不要"}；枚举外 → 400，状态不对 → 409。
            payload = self.read_json()
            self.send_json(hub_api.set_audio_mode(urllib.parse.unquote(m.group(1)),
                                                  str(payload.get("audio_mode") or "")))
            return
        m = re.fullmatch(r"/api/hub/task/([^/]+)/selfcheck-whitelist", path)
        if m:
            # AT-22 E4：误报豁免白名单（行×词键）。
            #   POST   {"行": 6|[5,6], "词": "右下角"} → 加白 + 自检重跑回显
            #   DELETE 同上（撤销这几行的这个词）或 {"全部": true}（全撤）
            payload = self.read_json()
            local_id = urllib.parse.unquote(m.group(1))
            if str(getattr(self, "command", "POST") or "POST").upper() == "DELETE":
                self.send_json(hub_api.set_selfcheck_whitelist(
                    local_id, hub_api.whitelist_rows(payload),
                    word=str(payload.get("词") or ""), remove=True,
                    remove_all=payload.get("全部") is True))
                return
            self.send_json(hub_api.set_selfcheck_whitelist(
                local_id, hub_api.whitelist_rows(payload),
                word=str(payload.get("词") or "")))
            return
        m = re.fullmatch(r"/api/hub/task/([^/]+)/revoke", path)
        if m:
            # AT-13 §2.2/§2.4：撤销批准（受控回退 approved→awaiting_approval，不自动重批）
            payload = self.read_json()
            self.send_json(hub_api.revoke_approval(
                urllib.parse.unquote(m.group(1)),
                note=str(payload.get("note") or "撤销批准（改配置后重批）")))
            return
        # ---- AT-9 B2：任务级 REST（确认组 / 回退 / 删除，路径参数非模板变量） ----
        m = re.fullmatch(r"/api/hub/task/([^/]+)/force-finish", path)
        if m:
            # AT-18 §4.2：所有者人工兜底口（覆盖阈值误判）。二次确认参数缺/类型不对 → 400；
            # 状态不是运行态 → 409；已 failed → 200 幂等（不二次写 history）。
            payload = self.read_json()
            self.send_json(hub_api.force_finish(
                urllib.parse.unquote(m.group(1)),
                confirm=payload.get("confirm"),
                confirm_name=payload.get("confirm_name"),
                reason=str(payload.get("reason") or "")))
            return
        m = re.fullmatch(r"/api/hub/task/([^/]+)/delete", path)
        if m:
            payload = self.read_json()
            confirm_name = payload.get("confirm_name")
            self.send_json(hub_api.delete_task(urllib.parse.unquote(m.group(1)),
                                               confirm_name=None if confirm_name is None
                                               else str(confirm_name)))
            return
        m = re.fullmatch(r"/api/hub/task/([^/]+)/fork", path)
        if m:
            # AT-21 B0：复用为新版本 → 派生**新任务**（源任务只读，产物按 bridge 共享，清单/资产图各一份）。
            # 只接受无体（或空体）请求：派生不携带任何「沿用上次配置」参数——新版本必须重新批准。
            self.send_json(hub_api.fork_task(urllib.parse.unquote(m.group(1))))
            return
        m = re.fullmatch(r"/api/hub/task/([^/]+)/prompt", path)
        if m:
            # AT-21 B0 D3-②：手改正文 → 直写 提示词.md + 重算审批快照 + 自检/音频门禁重跑。
            # body: {"text": "<全文>", "expect_text": "<打开编辑器时的正文，可选乐观锁>"}
            payload = self.read_json()
            self.send_json(hub_api.save_prompt_text(
                urllib.parse.unquote(m.group(1)),
                str(payload.get("text") if payload.get("text") is not None else ""),
                expect_text=None if payload.get("expect_text") is None
                else str(payload.get("expect_text"))))
            return
        m = re.fullmatch(r"/api/hub/task/([^/]+)/group", path)
        if m:
            # AT-11 M2 确认小窗：可选 主行/组名 → 『组元数据』落盘（缺省仍=最小行）。
            payload = self.read_json()
            primary = payload.get("主行")
            self.send_json(hub_api.group_rows(urllib.parse.unquote(m.group(1)),
                                              self._rows_param(payload),
                                              primary=payload.get("主行"),
                                              name=payload.get("组名")))
            return
        m = re.fullmatch(r"/api/hub/task/([^/]+)/ungroup", path)
        if m:
            self.send_json(hub_api.ungroup_rows(urllib.parse.unquote(m.group(1)),
                                                self._rows_param(self.read_json())))
            return
        m = re.fullmatch(r"/api/hub/task/([^/]+)/rollback", path)
        if m:
            self.read_json()                    # 无字段，读掉请求体保持 keep-alive 干净
            self.send_json(hub_api.do_rollback(urllib.parse.unquote(m.group(1))))
            return
        m = re.fullmatch(r"/api/hub/task/([^/]+)/retry", path)
        if m:
            # AT-9 B5：failed 态续跑入口（重跑即续，产物优先）；非 failed → 409
            self.read_json()                    # 无字段，读掉请求体保持 keep-alive 干净
            self.send_json(hub_api.start_retry(urllib.parse.unquote(m.group(1))))
            return
        if path == "/api/hub/asset":
            # AT-8 B2 写路径（数据合同 v3）：{task_id,n,image_url} 覆盖 / {…,clear:true} 清空 /
            # {…,text} 写命名；AT-11/AT-12 扩展：{…,decision} 行决策（keep_origin/dismissed/restore，与上互斥）；
            # AT-22 E3 扩展：{…,desc,expect_desc?,rerender?} 写「用我的描述」列（awaiting_approval 时
            # 同一请求内重渲染 + 自检/门禁重跑），与上面各支互斥。
            payload = self.read_json()
            try:
                n = int(payload.get("n"))
            except (TypeError, ValueError):
                raise ApiError(400, "n 必须是整数（待确认清单的行号）") from None
            text = payload.get("text")
            decision = payload.get("decision")
            desc = payload.get("desc")
            expect_desc = payload.get("expect_desc")
            self.send_json(hub_api.set_asset(
                str(payload.get("task_id") or ""), n,
                image_url=(str(payload.get("image_url") or "").strip() or None),
                clear=bool(payload.get("clear")),
                text=None if text is None else str(text),
                decision=str(decision) if decision else None,
                desc=None if desc is None else str(desc),
                expect_desc=None if expect_desc is None else str(expect_desc),
                rerender=bool(payload.get("rerender")),
            ))
            return
        if path == "/api/asset/upload":
            self.do_asset_upload()
            return
        if path == "/api/video/upload":
            self.do_video_upload()
            return
        if path == "/api/lapian/run":
            payload = self.read_json()
            tid = str(payload.get("id") or "")
            task_dir(tid)                     # 校验任务存在（404 语义一致）
            vf = video_file(tid)              # 指纹解析源视频路径（404/500 语义沿用）
            rel = vf.resolve().relative_to(ROOT) if str(vf.resolve()).startswith(str(ROOT)) else vf
            # M6-T7-C：可选 分析模型/渠道 透传（不传 → 与旧版完全一致）
            extra_args, extra_env = lapian_channel_passthrough(payload)
            self.send_json(start_analyze_job(str(rel), str(payload.get("intent") or ""), tid,
                                             extra_args=extra_args, extra_env=extra_env))
            return
        if path == "/api/wan/video/run":
            self.do_wan_video_run()
            return
        if path == "/api/sdgf/video/run":
            self.do_sdgf_video_run()
            return
        if path == "/api/analyze":
            payload = self.read_json()
            intent = str(payload.get("intent") or "")
            task_id = str(payload.get("id") or "")
            # 公网口径：立刻回 job id，前端轮询 /api/job/<id>；同步跑会被边缘 100s 掐断
            self.send_json(start_analyze_job(str(payload.get("video") or ""), intent, task_id))
            return
        m = re.fullmatch(r"/api/name/([^/]+)", path)
        if m:
            tid = urllib.parse.unquote(m.group(1))
            payload = self.read_json()
            try:
                no = int(payload.get("no"))
            except (TypeError, ValueError):
                raise ApiError(400, "no 必须是整数") from None
            set_checklist_name(tid, no, str(payload.get("name") or ""))
            self.send_json({"ok": True, "no": no})
            return
        m = re.fullmatch(r"/api/desc/([^/]+)", path)
        if m:
            tid = urllib.parse.unquote(m.group(1))
            payload = self.read_json()
            try:
                no = int(payload.get("row"))
            except (TypeError, ValueError):
                raise ApiError(400, "row 必须是整数") from None
            text = str(payload.get("text") or "")
            set_checklist_desc(tid, no, text)
            self.send_json({"ok": True, "row": no, "text": text})
            return
        m = re.fullmatch(r"/api/upload/([^/]+)", path)
        if m:
            self.do_upload(urllib.parse.unquote(m.group(1)))
            return
        m = re.fullmatch(r"/api/audio-mode/([^/]+)", path)
        if m:
            # AT-17 B3（画布链）：拉片弹层改选三态 → 落任务目录 audio_mode.json（画布链无 approve，
            # 落盘即生效；渲染时 render.py 自己读这份）。body: {"audio_mode": "…"}，枚举外 → 400。
            payload = self.read_json()
            self.send_json(set_task_audio_mode(urllib.parse.unquote(m.group(1)),
                                               str(payload.get("audio_mode") or ""),
                                               str(payload.get("依据") or "")))
            return
        m = re.fullmatch(r"/api/render/([^/]+)", path)
        if m:
            self.do_render(urllib.parse.unquote(m.group(1)))
            return
        m = re.fullmatch(r"/api/recheck/([^/]+)", path)
        if m:
            self.do_recheck(urllib.parse.unquote(m.group(1)))
            return
        m = re.fullmatch(r"/api/generate/([^/]+)", path)
        if m:
            self.do_generate(urllib.parse.unquote(m.group(1)))
            return
        m = re.fullmatch(r"/api/confirm/([^/]+)", path)
        if m:
            self.do_confirm(urllib.parse.unquote(m.group(1)))
            return
        m = re.fullmatch(r"/api/asset/delete/([^/]+)", path)
        if m:
            self.do_delete_asset(urllib.parse.unquote(m.group(1)))
            return
        self.read_body()                       # 排掉未读的请求体，别把 keep-alive 弄乱
        raise ApiError(404, f"没有这个地址：{path}")

    # -- 具体动作 --------------------------------------------------
    def upload_filename(self) -> str:
        """文件名从 X-Filename 头（percent-encoded）或 ?name= 取。"""
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        header = self.headers.get("X-Filename") or ""
        # 头是按 latin-1 解出来的；客户端若直接塞了 UTF-8 字节，这里还原成真 Unicode
        if any("\x80" <= ch <= "\xff" for ch in header):
            try:
                header = header.encode("latin-1").decode("utf-8")
            except (UnicodeEncodeError, UnicodeDecodeError):
                pass
        for raw in (urllib.parse.unquote(header), (query.get("name") or [""])[0]):
            if raw.strip():
                return raw.strip()
        raise ApiError(400, "缺文件名：把文件名 percent-encode 后放 X-Filename 头（或 ?name=）")

    def stream_to(self, temp: Path, length: int) -> int:
        """把请求体按块写进 temp（不整个读进内存），返回实际字节数。"""
        written = 0
        left = length
        with open(temp, "wb") as fh:
            while left > 0:
                chunk = self.rfile.read(min(CHUNK, left))
                if not chunk:
                    break
                fh.write(chunk)
                written += len(chunk)
                left -= len(chunk)
        return written

    def do_hub_ingest(self) -> None:
        """POST /api/hub/ingest —— multipart(video=<文件>, intent=<文本>) → 建任务。

        收完落临时文件转 automation.ingest（幂等键=视频 sha，同片重传命中同任务）。
        """
        length = int(self.headers.get("Content-Length") or 0)
        ctype = (self.headers.get("Content-Type") or "").lower()
        if not ctype.startswith("multipart/") or length <= 0:
            raise ApiError(400, "hub/ingest 要 multipart：video=<文件> intent=<文本>")
        if length > 64 * 1024 * 1024:
            raise ApiError(413, f"浏览器直传上限 64 MB（收到 {length / 1048576:.1f} MB）")
        bm = re.search(r"boundary=([^;]+)", self.headers.get("Content-Type") or "", re.IGNORECASE)
        if not bm:
            raise ApiError(400, "multipart 缺 boundary")
        fields, files = parse_multipart(self.read_body(), bm.group(1).strip().strip('"'))
        if "video" not in files:
            raise ApiError(400, "multipart 里没有 video 字段（文件放 video 字段）")
        name, blob = files["video"]
        UPLOADS.mkdir(parents=True, exist_ok=True)
        stem, ext = clean_video_name(name)
        suffix = f".{ext}".lower() if ext else Path(name).suffix.lower()
        if suffix not in {".mp4", ".mov", ".webm"}:
            raise ApiError(400, f"参考视频只收 mp4/mov/webm（收到 {suffix or '无扩展名'}）")
        # 两段式：临时目录+原始名——video_name/桥任务 id 都来自文件名，不能让 .hub-incoming 泄进去
        tdir = UPLOADS / f".hub-incoming-{os.getpid()}-{int(time.time() * 1000)}"
        tdir.mkdir(parents=True, exist_ok=True)
        temp = tdir / f"{stem}{suffix}"
        try:
            temp.write_bytes(blob)
            if temp.stat().st_size <= 0:
                raise ApiError(400, "视频是空的（0 字节）")
            res = hub_api.do_ingest(temp, fields.get("intent", ""))
        finally:
            for p in tdir.iterdir():
                p.unlink(missing_ok=True)
            if tdir.is_dir():
                tdir.rmdir()
        self.send_json(res)

    def do_hub_assets(self, local_id: str) -> None:
        """POST /api/hub/assets/<local_id> —— multipart 多图（file / file0 / file1 …）。

        parse_multipart 同名字段会覆盖，所以多图用 file、file0、file1… 任意组合。
        """
        length = int(self.headers.get("Content-Length") or 0)
        ctype = (self.headers.get("Content-Type") or "").lower()
        if not ctype.startswith("multipart/") or length <= 0:
            raise ApiError(400, "hub/assets 要 multipart：file=<图片>（多图用 file0/file1…）")
        if length > 20 * 1024 * 1024:
            raise ApiError(413, f"图太大（{length / 1048576:.1f} MB），单次上限 20 MB")
        bm = re.search(r"boundary=([^;]+)", self.headers.get("Content-Type") or "", re.IGNORECASE)
        if not bm:
            raise ApiError(400, "multipart 缺 boundary")
        _fields, files = parse_multipart(self.read_body(), bm.group(1).strip().strip('"'))
        items = sorted((k, v) for k, v in files.items() if k == "file" or k.startswith("file"))
        if not items:
            raise ApiError(400, "multipart 里没有图（文件放 file 字段，多图 file0/file1…）")
        paths: list[Path] = []
        try:
            for i, (_k, (name, blob)) in enumerate(items):
                suffix = Path(name).suffix.lower()
                if suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
                    raise ApiError(400, f"替换图只收 png/jpg/webp（{name} 是 {suffix or '无扩展名'}）")
                p = UPLOADS / f".hub-asset-{local_id}-{int(time.time() * 1000)}-{i}{suffix}"
                p.write_bytes(blob)
                paths.append(p)
            res = hub_api.do_assets(local_id, paths)
        finally:
            for p in paths:
                p.unlink(missing_ok=True)
        self.send_json(res)

    def send_body_bytes_or_file(self, f: Path) -> None:
        """成片发送：小文件整读；带 Range 支持的流式留给后续（当前成片 ≤ 数 MB）。"""
        self.send_body(f.read_bytes(), 200, "video/mp4")

    def do_asset_upload(self) -> None:
        """POST /api/asset/upload —— 参考图上传。

        AT-14 B3 A 两条路（按可选头 X-Task-Id 分流，percent-encoded）：
          · 带任务号：只落本地引用（任务目录 asset-uploads/，内容寻址），**不碰任何图床、
            不写 uploads/vc**——提交时才由 generate._assemble_images 装配成渠道要的形态；
          · 不带：旧行为一字不改（本地备份 uploads/vc + 转传 uguu，全挂本地前置拒绝）。
        2026-11-03 实测：器灵生成端拉 Cloudflare Tunnel（vc-t.aistar.work）图不稳定
        （网关过、生成挂），同图换 uguu 源即通 → 旧路全面切 uguu
        （docs/技术文档-SDGF带图生成排查与图床切换uguu-20261103.md）。
        """
        import urllib.parse as _up
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise ApiError(400, "请求体是空的：图片字节要放进 body")
        if length > 20 * 1024 * 1024:
            raise ApiError(413, f"图片太大（{length / 1048576:.1f} MB），上限 20 MB")
        raw_name = _up.unquote(self.headers.get("X-Filename") or "ref.png")
        data = self.rfile.read(length)
        task_id = _up.unquote(self.headers.get("X-Task-Id") or "").strip()
        if task_id:
            # 新路：本地引用。任务不存在=409（不是 400：请求形态对、目标不在）。
            if not local_assets.local_id_ok(task_id) \
                    or not (auto_config.PROJECTS_DIR / task_id).is_dir():
                raise ApiError(409, f"任务不存在: {task_id}")
            try:
                ref = local_assets.store(task_id, data, raw_name)
            except local_assets.LocalAssetError as e:
                raise ApiError(400, str(e)) from None
            with _ASSET_COUNTERS_LOCK:
                ASSET_COUNTERS["local"] += 1
            self.send_json({"url": local_assets.display_url(ref), "local": ref,
                            "bytes": length, "source": "local"})
            return
        # 旧路（无 X-Task-Id）：uguu + 隧道降级 + uploads/vc 备份，一字不改
        suffix = Path(raw_name).suffix.lower()
        if suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
            raise ApiError(400, f"参考图只收 png/jpg/webp（收到 {suffix or '无扩展名'}）")
        url, source = store_public_image(data, raw_name)
        self.send_json({"url": url, "bytes": length, "source": source})

    def do_video_upload(self) -> None:
        """POST /api/video/upload —— 收视频字节 → uploads/ → 建任务目录 + 指纹。

        两种投递都收：
          · 原始字节（前端走这条）：Content-Type: application/octet-stream，
            文件名放 X-Filename（percent-encoded UTF-8）或 ?name=
          · multipart/form-data（curl -F 顺手用）：只在小文件时走内存
        """
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise ApiError(400, "请求体是空的：视频字节要放进 body")
        if length > MAX_UPLOAD:
            raise ApiError(413, f"视频太大（{length / 1048576:.1f} MB），上限 500 MB")
        started = time.time()

        ctype = (self.headers.get("Content-Type") or "").lower()
        multipart = ctype.startswith("multipart/")
        if multipart and length > 64 * 1024 * 1024:
            raise ApiError(413, "multipart 上传上限 64 MB；大文件请用原始字节（curl --data-binary）")

        UPLOADS.mkdir(parents=True, exist_ok=True)
        temp = UPLOADS / f".incoming-{os.getpid()}-{int(time.time() * 1000)}.part"
        try:
            if multipart:
                boundary = re.search(r"boundary=([^;]+)", self.headers.get("Content-Type") or "",
                                     re.IGNORECASE)
                if not boundary:
                    raise ApiError(400, "multipart 缺 boundary")
                _fields, files = parse_multipart(self.read_body(), boundary.group(1).strip().strip('"'))
                if "file" not in files:
                    raise ApiError(400, "multipart 里没有 file 字段")
                filename, blob = files["file"]
                temp.write_bytes(blob)
            else:
                filename = self.upload_filename()
                self.stream_to(temp, length)

            size = temp.stat().st_size
            if size <= 0:
                raise ApiError(400, "视频是空的（0 字节）")
            stem, ext = clean_video_name(filename)
            with open(temp, "rb") as fh:
                head = fh.read(16)
            if not sniff_video(head, ext):
                raise ApiError(400, f"「{filename}」看着不是 {ext.upper()} 视频（文件头对不上），换个文件试试")

            target = place_upload(temp, stem, ext)
            task_id = create_task_from_upload(target)
        finally:
            temp.unlink(missing_ok=True)

        self.send_json({
            "ok": True,
            "id": task_id,
            "name": target.name,
            "size": size,
            "elapsed": round(time.time() - started, 1),
        })

    def do_upload(self, task_id: str) -> None:
        task_dir(task_id)
        ctype_raw = self.headers.get("Content-Type") or ""
        raw = self.read_body()
        if ctype_raw.lower().startswith("multipart/form-data"):
            # boundary 大小写敏感（curl 会混大小写），不能对整条头做 lower()
            m = re.search(r"boundary=([^;]+)", ctype_raw, re.IGNORECASE)
            if not m:
                raise ApiError(400, "multipart 缺 boundary")
            fields, files = parse_multipart(raw, m.group(1).strip().strip('"'))
            no_raw = fields.get("no", "")
            if "file" not in files:
                raise ApiError(400, "multipart 里没有 file 字段")
            filename, blob = files["file"]
        else:
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ApiError(400, "请求体不是合法 JSON") from None
            if not isinstance(payload, dict):
                raise ApiError(400, "请求体必须是 JSON 对象")
            no_raw = payload.get("no")
            filename = str(payload.get("filename") or "upload.png")
            try:
                blob = base64.b64decode(str(payload.get("data_b64") or ""), validate=True)
            except Exception:                   # noqa: BLE001
                raise ApiError(400, "data_b64 解不出来") from None
        # 上传按「清单行号」对齐；老调用只传 no，那时 no 就是清单行号，行为不变
        try:
            no = int(str(fields.get("row") or no_raw).strip()) if ctype_raw.lower().startswith("multipart/form-data")                 else int(str((payload.get("row") if isinstance(payload, dict) else None) or no_raw).strip())
        except (TypeError, ValueError):
            raise ApiError(400, "no/row 必须是整数") from None
        if not blob:
            raise ApiError(400, "图是空的（0 字节）")

        path = save_asset(task_id, no, filename, blob)
        set_checklist_image(task_id, no, path.name)
        # 默认两步：先落盘、立刻回响应，命名后台跑（10–50 秒），前端轮询 /api/naming。
        # 老的非浏览器调用（JSON + base64，脚本用）保持同步，行为不变。
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        sync = not ctype_raw.lower().startswith("multipart/form-data")
        flag = (query.get("sync") or [""])[0].strip().lower()
        if flag:
            sync = flag in ("1", "true", "yes", "on")

        out = {
            "ok": True,
            "no": no,
            "file": path.name,
            "url": f"/assets/{urllib.parse.quote(task_id)}/{urllib.parse.quote(path.name)}",
            "bytes": len(blob),
        }
        if sync:
            name, desc, warning = name_and_store(task_id, no, path)
            with NAMING_LOCK:
                NAMING[(task_id, no)] = {
                    "state": "failed" if warning else "done",
                    "name": name, "desc": desc, "warning": warning,
                    "started": time.time(), "elapsed": 0.0,
                }
            out["naming"] = "done"
            out["name"] = name
            out["desc"] = desc
            if warning:
                out["warning"] = warning
        else:
            start_naming(task_id, no, path)
            out["naming"] = "running"
        self.send_json(out)

    def do_render(self, task_id: str) -> None:
        task_dir(task_id)
        payload = self.read_json()
        template = str(payload.get("template") or "").strip()
        stem = template[:-3] if template.endswith(".md") else template
        # 可选段字母（如 C＝品牌收束与图文）：显式请求就要真进渲染 argv，不透传等于白选（B5）
        # 音频三态（AT-17 B2a）：请求里带就用请求值；没带就用本任务 audio_mode.json 的落盘值
        # （B3 界面改选走这里；两个都没有时渲染器自己推导，行为与老版本一致）。
        mode = str(payload.get("audio_mode") or "").strip()
        if not mode:
            mode = str((task_audio_mode(task_id) or {}).get("audio_mode") or "")
        render_task(task_id, stem, str(payload.get("optional") or ""), mode)

        text, tpl = latest_prompt(task_id)
        self.send_json({"ok": True, "template": tpl or stem,
                        "prompt": {"text": text, "template": tpl or stem},
                        "assets": build_assets(task_id),
                        # 重渲染会重写 渲染回执.json：一起返，免得页面上的回执停在上一次
                        "dialogueMap": dialogue_map(task_id),
                        "intentReceipt": intent_receipt(task_id)})

    def do_recheck(self, task_id: str) -> None:
        """人工复检的一条：use 改写拆解.md 那一行并重渲染；keep 只记 kept（文件不动）。

        写回与检测都在 pipeline/recheck.py 里（那里是唯一实现）；这里只负责调它、
        然后把提示词重新渲染一遍。**重渲染不是可选项**：preflight 与生成读的是盘上的
        `提示词-<模板>.md`，不重渲染，人刚改的这一行永远到不了生成请求（核对行和
        预演令牌也会停在旧正文上）。
        """
        task_dir(task_id)
        payload = self.read_json()
        action = str(payload.get("action") or "").strip()
        if action not in ("use", "keep"):
            raise ApiError(400, "action 只支持 use / keep")
        item = str(payload.get("id") or "").strip()
        if not item:
            raise ApiError(400, "缺少复检项 id")

        args = ["--use" if action == "use" else "--keep", item]
        text = str(payload.get("text") or "")
        if action == "use" and text.strip():
            args += ["--text", text]
        data = recheck_call(task_id, args)          # 原子写，不留 .bak

        template = str(payload.get("template") or "").strip()
        stem = template[:-3] if template.endswith(".md") else template
        if not stem:
            stem = latest_prompt(task_id)[1]
        if stem:
            try:
                # 可选段跟渲染请求同口径透传：不带上，人刚看过的收束段就会在这次
                # 重渲染里被静默关掉（2026-11-04 路 A 审计 B5 同源问题）
                render_task(task_id, stem, str(payload.get("optional") or ""))
            except ApiError as err:
                raise ApiError(err.status, f"拆解已改并落盘，但重渲染失败：{err.msg}") from None

        text_now, tpl = latest_prompt(task_id)
        data["prompt"] = {"text": text_now, "template": tpl or stem}
        self.send_json(data)

    def do_generate(self, task_id: str) -> None:
        """两段式：不带 confirm 只回计划（查余额、数图、跑一次预演，全免费）；带 confirm 才真提交。

        花钱这件事必须由人点两次：第一次看参数、核对行和余额，第二次才是「扣灵感值」。
        带 confirm 的请求**服务端自己再跑一遍预演**，并要求 preflightToken 与本次算出的
        令牌相等且核对通过，否则 409 直接返回、绝不提交——页面上的按钮禁用只是体验，
        真正拦在这里（提示词重渲染过、参数被改过，令牌就对不上）。
        生成要跑几分钟，放后台线程里跑，页面轮询 /api/generate/<id> 看 `生成/记录.json`。
        """
        task_dir(task_id)
        payload = self.read_json()
        template = str(payload.get("template") or "").strip()
        try:
            duration = int(payload.get("duration") or 15)
        except (TypeError, ValueError):
            raise ApiError(400, "duration 必须是整数") from None
        if not 2 <= duration <= 30:
            raise ApiError(400, "时长只支持 2–30 秒（万相的上限）")
        ratio = str(payload.get("ratio") or "9:16")
        if ratio not in RATIOS:
            raise ApiError(400, f"画幅只支持 {'/'.join(RATIOS)}")
        resolution = str(payload.get("resolution") or "1080P")
        if resolution not in RESOLUTIONS:
            raise ApiError(400, f"分辨率只支持 {'/'.join(RESOLUTIONS)}")
        audio = bool(payload.get("audio", True))

        plan = gen_plan(task_id, template, duration, ratio, resolution, audio)

        if not payload.get("confirm"):
            self.send_json({"plan": plan})
            return

        # —— 复核闸门：不接受页面报上来的结果，自己重算一次 ——
        pf = plan.get("preflight") if isinstance(plan.get("preflight"), dict) else {}
        token = str(payload.get("preflightToken") or "").strip()
        # 先按格式收口（16 位十六进制）：compare_digest 只吃 ASCII，页面塞中文进来会抛 TypeError
        if (not pf.get("ok") or not TOKEN_RE.fullmatch(token)
                or not secrets.compare_digest(token, str(pf.get("token") or ""))):
            self.send_json({"error": "核对未通过或参数已变，未提交", "preflight": pf}, 409)
            return

        state = gen_state(task_id)
        if state.get("status") == "running" and not state.get("stale"):
            raise ApiError(409, "已经有一条在跑了，等它出来再点（同时跑两条等于扣两次钱）")
        args = ["pipeline/gen/wan_video.py", "--id", task_id]
        if template:
            args += ["--template", template]
        args += ["--duration", str(duration), "--ratio", ratio, "--resolution", resolution,
                 "--audio-output", "true" if audio else "false", "--confirm"]
        threading.Thread(target=self._run_generate, args=(task_id, args),
                         daemon=True, name=f"gen-{task_id}").start()
        time.sleep(0.3)                                     # 让线程先把 running 写进记录
        self.send_json({"started": True, "state": gen_state(task_id)})


    def _run_generate(self, task_id: str, args: list[str]) -> None:
        code, out, err = run_py(args, timeout=1900)
        gen_dir(task_id).mkdir(parents=True, exist_ok=True)
        (gen_dir(task_id) / "日志.txt").write_text(
            f"退出码 {code}\n--- stdout ---\n{out}\n--- stderr ---\n{err}\n", encoding="utf-8")

    # -- 万相视频生成 bridge（canvas 任务）--------------------------

    def do_wan_video_run(self) -> None:
        """POST /api/wan/video/run —— 建任务 + 起后台生成，立即返回 {job, taskId}。

        与 /api/analyze 同款「提交 → 轮询」：生成要跑几分钟，不能同步等。
        confirm=false 只打计划不花钱（引擎契约：花钱动作必须显式确认）。
        """
        payload = self.read_json()
        prompt = str(payload.get("prompt") or "")
        if not prompt.strip():
            raise ApiError(400, "prompt 不能为空")
        try:
            duration = int(payload.get("duration"))
        except (TypeError, ValueError):
            raise ApiError(400, "duration 必须是整数（2–30 秒）") from None
        if not 2 <= duration <= 30:
            raise ApiError(400, "时长只支持 2–30 秒（万相的上限）")
        ratio = str(payload.get("ratio") or "")
        if ratio not in WAN_RATIOS:
            raise ApiError(400, f"画幅只支持 {'/'.join(WAN_RATIOS)}")
        resolution = str(payload.get("resolution") or "")
        if resolution not in WAN_RESOLUTIONS:
            raise ApiError(400, "分辨率只支持 480 / 720 / 1080")
        audio = bool(payload.get("audio", True))
        confirm = bool(payload.get("confirm"))
        images = payload.get("images")
        images = images if isinstance(images, list) else []
        for item in images:
            if not isinstance(item, dict):
                raise ApiError(400, "images 必须是 [{name, dataUrl}] 列表")

        if not WAN_BUSY.acquire(blocking=False):      # CLI 限制：同时只认一条，第二条直接挡
            raise ApiError(409, "已经有一个万相生成在跑（CLI 同时只认一条），稍后再试")
        now = time.time()
        job_id = secrets.token_hex(8)
        try:
            task_id = wan_prepare_task(prompt, images)   # 落盘失败则释放并发位，不留占坑
        except BaseException:
            WAN_BUSY.release()
            raise
        with JOBS_LOCK:
            for stale, old in list(JOBS.items()):        # 过期任务记录随手清（与拆解同款）
                if now - float(old.get("started") or now) > JOB_TTL:
                    JOBS.pop(stale, None)
            JOBS[job_id] = {"status": "running", "started": now,
                            "kind": "wan-video", "task_id": task_id}
        argv = wan_argv(task_id, duration, ratio, resolution, audio, confirm)
        threading.Thread(target=self._run_wan_video, args=(job_id, task_id, argv),
                         daemon=True, name=f"wan-video-{task_id}").start()
        self.send_json({"job": job_id, "taskId": task_id})

    def _run_wan_video(self, job_id: str, task_id: str, argv: list[str]) -> None:
        """后台线程：跑 wan_video.py（提交与否由 argv 里的 --confirm 决定）。

        结束后把退出码/stderr 尾部记进 JOBS，日志落 生成/bridge日志.txt，释放并发位。
        """
        code, out, err, error = -1, "", "", ""
        try:
            code, out, err = run_py(argv, timeout=WAN_JOB_TIMEOUT)
        except subprocess.TimeoutExpired:
            code, err = 124, f"wan_video.py 超过 {WAN_JOB_TIMEOUT}s 没跑完，按失败处理"
        except Exception as exc:                     # noqa: BLE001 - 后台线程不能静默死掉
            traceback.print_exc()
            error = f"bridge 起子进程失败：{exc}"
        try:
            gen = ANALYSIS / task_id / GEN_DIR
            gen.mkdir(parents=True, exist_ok=True)
            (gen / "bridge日志.txt").write_text(
                f"命令行 {' '.join(argv)}\n退出码 {code}\n--- stdout ---\n{out}\n--- stderr ---\n{err}\n",
                encoding="utf-8")
        except OSError:
            pass
        finally:
            WAN_BUSY.release()
        with JOBS_LOCK:
            rec = JOBS.get(job_id)
            if rec is not None:
                rec.update(status="error" if error else "done", exit=code,
                           stderr=tail(err, 400), stdout=tail(out, 400),
                           error=error,
                           elapsed=round(time.time() - float(rec.get("started") or 0), 1))

    # -- SDGF 视频生成 bridge（服务端合同）--------------------------

    def do_sdgf_video_run(self) -> None:
        """POST /api/sdgf/video/run —— 与 /api/wan/video/run 同形：立即回 {job, taskId}。

        confirm 必须**是布尔 true** 才真提交（字符串 "false"/"1" 一律当 dry——AT-5 判据）；
        dry 只做本地预演与结构化核对，不发任何请求、不花钱。
        """
        payload = self.read_json()
        prompt = str(payload.get("prompt") or "")
        if not prompt.strip():
            raise ApiError(400, "prompt 不能为空")
        if payload.get("images") is not None and not isinstance(payload.get("images"), list):
            raise ApiError(400, "images 必须是列表（公网 https URL 字符串或 {name, dataUrl}）")
        plan = sdgf_plan(payload)
        if payload.get("confirm") is not True:
            self.send_json({"dry": True, "preview": plan["preview"], "preflight": plan["preflight"]})
            return
        if not plan["preflight"]["ok"]:
            self.send_json({"error": "预演未通过，未提交（不花钱）", "preflight": plan["preflight"]}, 400)
            return
        if not SDGF_JOBS.acquire(blocking=False):
            raise ApiError(409, "同时最多 2 条 SDGF 生成在跑（并发上限），稍后再试")
        try:
            body, sources = sdgf_host_images(plan)    # dataUrl → 公网 URL（dry 不走到这里）
            task_id = sdgf_create(plan["channel"]["baseUrl"], plan["key"], body)
        except BaseException:
            SDGF_JOBS.release()
            raise
        job_id = secrets.token_hex(8)
        now = time.time()
        with JOBS_LOCK:
            for stale, old in list(JOBS.items()):         # 过期任务记录随手清（与万相同款）
                if now - float(old.get("started") or now) > JOB_TTL:
                    JOBS.pop(stale, None)
            JOBS[job_id] = {"status": "running", "started": now, "kind": "sdgf-video",
                            "task_id": task_id, "stage": "已提交", "progress": None}
        threading.Thread(target=sdgf_run_job,
                         args=(job_id, task_id, plan["channel"]["baseUrl"], plan["key"], plan["request"]),
                         daemon=True, name=f"sdgf-video-{sdgf_safe_id(task_id)}").start()
        self.send_json({"job": job_id, "taskId": task_id, "dry": False,
                        "imageSources": sources})   # AT-14 B3 A5：装配来源如实透传（dry 分支不带）

    def serve_sdgf_video(self, task_id: str) -> None:
        """GET /api/sdgf/video/file/<taskId> —— 本地成片（bridge 代下，凭证不出服务端）。

        目录名就是渠道任务 id：服务重启后仍能按 id 找到已下完的成片（AT-6 归档依赖这一点）。
        """
        if not task_id or ".." in task_id or not ID_RE.match(task_id):
            raise ApiError(400, f"任务 id 不合法：{task_id!r}")
        final = sdgf_task_dir(task_id) / SDGF_FINAL
        if not final.is_file():
            raise ApiError(404, f"{task_id} 还没有成片（渠道没跑完，或没提交成功）")
        self.send_mp4(final)

    def do_confirm(self, task_id: str) -> None:
        """在已有「拆解.md」上补生成「待确认清单.md」。

        为什么不复用 /api/analyze：那条会重跑视频拆解，一次模型调用要几十秒还花钱。
        用户只是漏了清单（或想按新拆解重建），本地抽实体是零成本动作。
        """
        task_dir(task_id)
        self.read_body()
        disc = ANALYSIS / task_id / "拆解.md"
        if not disc.is_file():
            raise ApiError(400, f"{task_id} 还没有拆解.md，先跑一次拆解")

        code, out, err = run_py(["pipeline/reverse/confirm.py", str(disc)], timeout=120)
        if code != 0:
            raise ApiError(500, f"confirm.py 退出 {code}：{tail(err) or tail(out)}")
        self.send_json({"ok": True, "checklist": parse_checklist(task_id)})

    # -- 整合包静态 serve（web-dist）--------------------------------

    def try_static(self, url_path: str) -> bool:
        """web-dist 里有 url_path 对应的**文件**就 serve 并返回 True；否则 False（继续原路由）。

        只在整合包形态（WEB_DIST_ROOT 非空）生效；路径穿越企图一律 False，绝不 serve。
        """
        root = WEB_DIST_ROOT
        if root is None:
            return False
        rel = urllib.parse.unquote(url_path).lstrip("/")
        if not rel:
            return False
        try:
            target = (root / rel).resolve()
            target.relative_to(root)               # 防路径穿越：必须仍在 web-dist 内
        except (ValueError, OSError):
            return False
        if target.is_file():
            self.send_body(target.read_bytes(), 200,
                           STATIC_MIME.get(target.suffix.lower(), "application/octet-stream"))
            return True
        return False

    def serve_static(self, url_path: str) -> None:
        """整合包形态兜底：web-dist 静态文件；是目录或不存在 → 回退 index.html（SPA path 路由）。"""
        root = WEB_DIST_ROOT
        if root is None:
            raise ApiError(404, f"没有这个地址：{url_path}")
        rel = urllib.parse.unquote(url_path).lstrip("/")
        try:
            target = ((root / rel) if rel else root).resolve()
            target.relative_to(root)               # 防路径穿越：必须仍在 web-dist 内
        except (ValueError, OSError):
            raise ApiError(404, f"没有这个地址：{url_path}") from None
        if target.is_dir() or not target.is_file():
            target = root / "index.html"
        if not target.is_file():
            raise ApiError(404, f"没有这个地址：{url_path}")
        self.send_body(target.read_bytes(), 200,
                       STATIC_MIME.get(target.suffix.lower(), "application/octet-stream"))

    def serve_asset(self, task_id: str, filename: str) -> None:
        if not ASSET_RE.match(filename):
            raise ApiError(404, "只认 assets/图片N.<ext>")
        path = (task_dir(task_id) / "assets" / filename)
        if not path.is_file():
            raise ApiError(404, f"图不存在：{filename}")
        ext = path.suffix.lower().lstrip(".")
        self.send_body(path.read_bytes(), 200, MIME.get(ext, "application/octet-stream"))

    def serve_video(self, task_id: str) -> None:
        """流式返回源视频，支持 Range（206）——不支持的话 <video> 拖不动进度条。"""
        task_dir(task_id)                      # 先校验 id（不合法/不存在 → 400/404）
        self.send_mp4(video_file(task_id))

    def serve_generated(self, task_id: str) -> None:
        """返回万相生成的成片（同样走 Range 流）。没有就 404，绝不拿源视频顶替。"""
        state = gen_state(task_id)
        if not state.get("available"):
            raise ApiError(404, "还没有生成成片")
        self.send_mp4(gen_dir(task_id) / state["video"])

    def serve_wan_video(self, task_id: str) -> None:
        """GET /api/wan/video/file/<taskId> —— wan-<ts> 任务的成片 成片-canvas.mp4。

        画布端按 taskId 直取成片；不校验任务目录存在（成片在不在本身就是要回答的问题）。
        """
        if not task_id or ".." in task_id or not ID_RE.match(task_id):
            raise ApiError(400, f"任务 id 不合法：{task_id!r}")
        final = ANALYSIS / task_id / GEN_DIR / f"成片-{WAN_TEMPLATE}.mp4"
        if not final.is_file():
            raise ApiError(404, f"{task_id} 还没有生成成片（{final.name}）")
        self.send_mp4(final)

    def send_mp4(self, path: Path) -> None:
        try:
            total = path.stat().st_size
        except OSError as err:
            raise ApiError(500, f"读不到源视频大小：{err}") from None
        if total <= 0:
            raise ApiError(500, "源视频是 0 字节")

        ext = path.suffix.lower().lstrip(".")
        ctype = VIDEO_MIME.get(ext, "application/octet-stream")
        status, start, end = 200, 0, total - 1
        raw_range = (self.headers.get("Range") or "").strip()

        if raw_range:
            m = RANGE_RE.match(raw_range)
            if not m or (m.group(1) == "" and m.group(2) == ""):
                self.send_range_error(total, raw_range)
                return
            first, last = m.group(1), m.group(2)
            if first == "":                    # 后缀区间：bytes=-2048
                n = int(last)
                if n <= 0:
                    self.send_range_error(total, raw_range)
                    return
                start, end = max(0, total - n), total - 1
            else:
                start = int(first)
                end = total - 1 if last == "" else min(int(last), total - 1)
            if start >= total or start > end:
                self.send_range_error(total, raw_range)
                return
            status = 206

        length = end - start + 1
        try:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
            self.end_headers()
            if self.command == "HEAD":
                return
            with open(path, "rb") as fh:
                fh.seek(start)
                left = length
                while left > 0:
                    chunk = fh.read(min(CHUNK, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            # 播放器拖动进度条会主动断开上一个 Range 连接，这是正常行为。
            self.close_connection = True

    def send_range_error(self, total: int, raw_range: str) -> None:
        data = json.dumps({"error": f"Range 不满足：{raw_range}"}, ensure_ascii=False).encode("utf-8")
        self.send_response(416)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Range", f"bytes */{total}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def do_delete_asset(self, task_id: str) -> None:
        """删掉某个序号的资产图（同序号的所有扩展名）。只动 assets/图片N.*。"""
        assets_dir(task_id)
        payload = self.read_json()
        try:
            no = int(payload.get("no"))
        except (TypeError, ValueError):
            raise ApiError(400, "no 必须是整数") from None
        removed: list[str] = []
        with WRITE_LOCK:
            for old in assets_dir(task_id).glob(f"图片{no}.*"):
                if old.is_file():
                    old.unlink()
                    removed.append(old.name)
        set_checklist_image(task_id, no, "")
        self.send_json({"ok": True, "no": no, "removed": removed})

    def download_zip(self, task_id: str) -> None:
        files = asset_files(task_id)
        if not files:
            raise ApiError(404, "这个任务还没有任何资产图，先传图")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for seq, no in enumerate(sorted(files), start=1):   # 按清单行号排序，重排成 1..K 与 @图片N 对齐
                zf.write(files[no], arcname=f"图片{seq}{files[no].suffix}")
        name = f"图片包-{task_id}.zip"
        quoted = urllib.parse.quote(name)
        # filename* 给浏览器（Chrome 存成「图片包-<id>.zip」）；
        # filename= 是给 curl 这类只认 ASCII 的客户端的回退。
        self.send_body(
            buf.getvalue(), 200, "application/zip",
            {"Content-Disposition": f"attachment; filename=\"assets-{task_id}.zip\"; "
                                    f"filename*=UTF-8''{quoted}"},
        )


# ---------------------------------------------------------------- 目录扫描 / 拆解


def list_videos() -> list[dict]:
    out: list[dict] = []
    if not RAW.is_dir():
        return out
    for p in sorted(RAW.rglob("*")):
        if not p.is_file() or p.suffix.lower() != ".mp4":
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        out.append({
            "path": p.relative_to(ROOT).as_posix(),
            "name": p.name,
            "size_mb": round(size / 1048576, 1),
        })
    out.sort(key=lambda v: v["path"])
    return out


CANVAS_ORIGINS = {"http://127.0.0.1:3000", "http://localhost:3000"}


def lapian_payload(tid: str, spec: str) -> dict:
    """M3 bridge：IR → 拉片 blocks + markdown（只读投影，不落盘）。"""
    sys.path.insert(0, str(ROOT / "pipeline" / "reverse"))
    import render_lapian
    from render import Fail
    d = ROOT / "analysis" / "reverse" / tid
    if not d.is_dir():
        raise ApiError(404, f"任务不存在：{tid}")
    try:
        fm, rows = render_lapian.load_spec(spec)
        blocks = render_lapian.project(render_lapian.IR(d), rows)
    except Fail as e:
        raise ApiError(400, f"拉片投影失败：{e}")
    return {"task": tid, "spec": spec, "blocks": blocks,
            "markdown": render_lapian.render_markdown(tid, spec, blocks)}


def render_payload(tid: str, spec_name: str, optional: str = "", audio_mode: str = "",
                   checklist_task: str = "") -> dict:
    """M3 bridge：生成规范 → 提示词正文（走 render.py，产物照常落盘可回看）。

    AT-17 B2a：`audio_mode` 是音频三态（沿用原片 / 按新主体调整 / 不要）。GET 没有 body，
    所以走 query 透传；不传就让渲染器自己读任务目录 `audio_mode.json`，老调用方不受影响。
    枚举外的值直接 400——静默丢弃会让调用方以为参数生效了。

    AT-21 B0：`checklist_task` 是**清单作用域**（派生任务号）。非空 = 这次渲染读那个任务自己的
    私有清单副本（`任务目录/待确认清单.md/.json`），产物也落它自己的任务目录——共享 `analysis/reverse/<id>`
    里的清单/提示词一概不碰（否则 fork 一渲染就把源任务侧的共享提示词覆盖了）。
    形状/归属/存在三重校验在 `hub_api.private_list_dir`，这里只转发。
    """
    sys.path.insert(0, str(ROOT / "pipeline" / "reverse"))
    from spec_registry import load_registry
    from render import Fail, main as render_main
    specs = {s["file"].removesuffix(".md"): s for s in load_registry()}
    meta = specs.get(spec_name)
    if meta is None:
        raise ApiError(404, f"规范不存在：{spec_name}")
    if meta["类型"] != "生成":
        raise ApiError(400, f"{spec_name} 是{meta['类型']}规范，不是生成规范")
    task_dir(tid)                                    # 校验任务存在（404 语义一致）
    scoped = hub_api.private_list_dir(tid, checklist_task)   # None = 共享清单（既有行为）
    tpl_stem = Path(meta["正文模板"]).stem
    argv = ["--id", tid, "--template", tpl_stem]
    if scoped is not None:
        argv += ["--checklist-dir", str(scoped), "--out-dir", str(scoped)]
    if str(optional or "").strip():                  # bridge 与 handler 同口径：可选段要透传（B5）
        argv += ["--optional", str(optional).strip()]
    mode = str(audio_mode or "").strip()
    if mode:
        if mode not in AUDIO_MODE_VALUES:
            raise ApiError(400, f"audio_mode 只认 {'/'.join(AUDIO_MODE_VALUES)}，收到「{mode}」")
        argv += ["--audio-mode", mode]
    try:
        code = render_main(argv)
    except Fail as e:
        raise ApiError(400, f"渲染失败：{e}")
    except SystemExit as e:                     # render.py 失败路径 raise SystemExit(code)（实测 2026-11-08：
        # 模板缺原文依据文件时 exit 3 穿透 handler → 连接直接断、客户端拿到 RemoteDisconnected
        # 而非 5xx JSON）——此处拦截，code 与 stderr 原文如实回给调用方（失败即失败，但不当连接黑洞）
        detail = str(getattr(e, "code", "") or "")
        raise ApiError(500, f"渲染异常退出（exit={detail or '?'}）——看 bridge 控制台 stderr 原文") from None
    if code != 0:
        raise ApiError(500, f"渲染退出码 {code}")
    out_dir = scoped if scoped is not None else (ROOT / "analysis" / "reverse" / tid)
    text = (out_dir / f"提示词-{tpl_stem}.md").read_text(encoding="utf-8")
    return {"task": tid, "spec": spec_name, "template": tpl_stem, "text": text}


def list_tasks() -> list[dict]:
    out: list[dict] = []
    if not ANALYSIS.is_dir():
        return out
    for d in ANALYSIS.iterdir():
        if not d.is_dir():
            continue
        prompts = list(d.glob("提示词-*.md"))
        src = video_file(d.name, strict=False)
        out.append({
            "id": d.name,
            "has_disc": (d / "拆解.md").is_file(),
            "has_check": (d / "待确认清单.md").is_file(),
            "has_prompt": bool(prompts),
            "video": src.name if src else "",
            "intent": task_intent(d.name),
            "intent_changed": task_intent_changed(d.name),
            "mtime": int(max([p.stat().st_mtime for p in d.iterdir() if p.is_file()] or
                             [d.stat().st_mtime])),
        })
    out.sort(key=lambda t: t["mtime"], reverse=True)
    return out


def read_progress(task_id) -> dict | None:
    """读任务的拆解进度文件（analyze.py 阶段透出用，M6-T6F）。

    读不到 / 坏 JSON / id 不合法 一律返回 None——进度只是锦上添花，
    绝不影响 /api/job 原有响应。格式归 pipeline/reverse/progress.py。
    """
    tid = str(task_id or "")
    if not tid or "/" in tid or "\\" in tid or tid in (".", ".."):
        return None
    try:
        sys.path.insert(0, str(ROOT / "pipeline" / "reverse"))
        from progress import read_progress as _read_progress
        return _read_progress(ANALYSIS / tid)
    except Exception:                        # noqa: BLE001 —— 读不到就当没有
        return None


# ------------------------------------------------------- 拉片渠道透传（M6-T7-C）

_VERSION_SUFFIX_RE = re.compile(r"/v\d+[a-z]*$")


def normalize_gemini_base(url: str) -> str:
    """渠道 baseUrl 归一（拆解走 Gemini 原生 generateContent 端点）。

    渠道配置常填裸域名（不带版本段）——自动补默认版本 /v1beta；
    显式带 /v1、/v1beta 等版本段则尊重原值。空值返回空串（不透传 base-url）。
    """
    base = (url or "").strip().rstrip("/")
    if not base:
        return ""
    if _VERSION_SUFFIX_RE.search(base):
        return base
    return f"{base}/v1beta"


def lapian_channel_passthrough(payload: dict) -> tuple[list[str], dict]:
    """从 /api/lapian/run 请求体提取可选「分析模型 / 渠道」→（analyze.py 附加参数, 子进程附加环境）。

    channel 形如 {baseUrl, key, model}（与生成节点随请求带渠道的做法同款）；顶层 model 是
    只选模型名的简写，channel.model 优先。什么都不传 → ([], {})：命令行与环境与旧版完全
    一致（旧任务零回归）。key 不上命令行——子进程会把命令行原样打进日志，密钥走环境变量
    JXIN_API_KEY 传（extract.load_api_key 本就环境变量优先，读法不变）。
    """
    channel = payload.get("channel")
    channel = channel if isinstance(channel, dict) else {}

    def clean(value) -> str:
        return str(value or "").strip()

    model = clean(channel.get("model")) or clean(payload.get("model"))
    base = normalize_gemini_base(clean(channel.get("baseUrl")))
    key = clean(channel.get("key"))
    argv: list[str] = []
    if model:
        argv += ["--model", model]
    if base:
        argv += ["--base-url", base]
    env = {"JXIN_API_KEY": key} if key else {}
    return argv, env


def analyze_argv(rel: str, task_id: str, intent: str,
                 extra_args: list[str] | None = None) -> list[str]:
    """拆解子进程命令行（唯一拼装点）；不透传时与历史版本逐字节一致。"""
    return ["analyze.py", rel, "--id", task_id, "--yes", "--intent", intent] + list(extra_args or [])


def lapian_slot_view() -> dict:
    """拆解槽真相（只读诊断面）：谁在占、占了多久、跑到哪个阶段。

    为什么必须把这个面暴露出来：拆解槽（ANALYZE_BUSY）是**桥进程内的锁**，hub 那侧看不见
    「槽被谁占着」。强制结束一个已提交远程拆解的任务后，重试只能反复撞 409，界面给用户刷
    「第 N 次失败」——事实（上一个 run 的远程拆解还在跑、还剩多久）本来就查得到，只是没出口。
    本面只读、零副作用；锁被占但查不到 job 记录（acquire 与记录落盘之间、或记录被 TTL 清掉）
    时如实报 busy=true + holder=None，绝不猜一个任务号填上。
    """
    busy = bool(ANALYZE_BUSY.locked())
    holder = None
    with JOBS_LOCK:
        running = [(jid, dict(rec)) for jid, rec in JOBS.items()
                   if str(rec.get("status") or "") == "running"]
    if busy and running:
        # 同一时刻只有一个 job 能持有 ANALYZE_BUSY → 最早启动的那条就是持有者
        running.sort(key=lambda kv: float(kv[1].get("started") or 0))
        jid, rec = running[0]
        started = float(rec.get("started") or 0)
        tid = str(rec.get("task_id") or "")
        stage_rec = read_progress(tid) or {}
        holder = {
            "job": jid,
            "task_id": tid,
            "started_at": datetime.fromtimestamp(started).astimezone().isoformat(timespec="seconds")
                          if started else "",
            "elapsed_seconds": round(time.time() - started, 1) if started else None,
            "stage": str(stage_rec.get("stage") or ""),
        }
    return {
        "busy": busy,
        "holder": holder,
        # 诊断用：哪些任务 id 记在 ANALYZING（同一任务重拆也会 409，文案要指向它）
        "analyzing": sorted(ANALYZING),
        "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def start_analyze_job(video: str, intent: str = "", requested_task_id: str = "",
                      extra_args: list[str] | None = None,
                      extra_env: dict | None = None) -> dict:
    """提交拆解任务，立刻返回 job id；真正的 analyze.py 在后台线程里跑。"""
    if not ANALYZE_BUSY.acquire(blocking=False):
        # 409 语义写死：**排队**，不是失败。extra.slot 把「谁在占、跑了多久」交给调用方，
        # 调用方（automation/discuss.py 的排队循环 / hub 重试入口）据此说人话，而不是刷计数。
        raise ApiError(409, "已经有一个拆解在跑，等它跑完（约 1–3 分钟）再点",
                       extra={"slot": lapian_slot_view()})
    now = time.time()
    job_id = secrets.token_hex(8)
    with JOBS_LOCK:
        for stale, rec in list(JOBS.items()):
            if now - float(rec.get("started") or now) > JOB_TTL:
                JOBS.pop(stale, None)
        JOBS[job_id] = {"status": "running", "started": now}

    def run() -> None:
        def on_task(task_id: str) -> None:
            """job→task 映射（M6-T6F）：analyze_video 把任务 id 定下来就回填，
            /api/job 轮询端据此读该任务的 进度.json 透出当前阶段。"""
            with JOBS_LOCK:
                rec = JOBS.get(job_id)
                if rec is not None:
                    rec["task_id"] = task_id

        try:
            result = analyze_video(video, intent, requested_task_id, on_task,
                                   extra_args=extra_args, extra_env=extra_env)
            rec = {"status": "done", "result": result, "started": now,
                   "elapsed": round(time.time() - now, 1)}
        except ApiError as err:
            rec = {"status": "error", "error": err.msg, "started": now,
                   "elapsed": round(time.time() - now, 1)}
        except Exception as exc:                    # noqa: BLE001
            traceback.print_exc()
            rec = {"status": "error", "error": f"服务内部异常：{exc}", "started": now,
                   "elapsed": round(time.time() - now, 1)}
        finally:
            ANALYZE_BUSY.release()
        with JOBS_LOCK:
            JOBS[job_id] = rec

    threading.Thread(target=run, daemon=True, name=f"analyze-{job_id}").start()
    return {"job": job_id}


def analyze_video(video: str, intent: str = "", requested_task_id: str = "",
                  on_task=None, extra_args: list[str] | None = None,
                  extra_env: dict | None = None) -> dict:
    """跑一次拆解。on_task：任务 id 定下来时的回调（job→task 映射，可空）。"""
    if not video:
        raise ApiError(400, "缺 video")
    path = Path(video)
    if not path.is_absolute():
        path = ROOT / path
    try:
        path = path.resolve()
    except OSError:
        raise ApiError(400, f"路径不合法：{video}") from None
    if not path.is_file() or path.suffix.lower() != ".mp4":
        raise ApiError(400, f"视频不存在或不是 mp4：{video}")
    try:
        rel = path.relative_to(ROOT).as_posix()
    except ValueError:
        raise ApiError(400, "视频必须在仓库目录内（原始资料/ 或 uploads/）") from None

    # 已有任务重拆沿用原 id；新任务仍按源视频查找/生成。
    if requested_task_id:
        task_id = task_dir(requested_task_id).name
        recorded = video_file(task_id, strict=True)
        if recorded.resolve() != path:
            raise ApiError(400, "任务 id 与 video 不是同一条源视频")
    else:
        task_id = pending_task_for(path) or new_task_id(path)
    task_path = ANALYSIS / task_id
    task_path.mkdir(parents=True, exist_ok=True)
    with WRITE_LOCK:
        if task_id in ANALYZING:
            raise ApiError(409, f"{task_id} 正在拆解，等它跑完（约 30–60 秒）",
                           extra={"slot": lapian_slot_view(), "task_id": task_id})
        write_text(task_path / "意图.txt", intent)
        ANALYZING.add(task_id)
    started = time.time()

    # 进度透出（M6-T6F）：先把 job→task 映射记下，再立刻写「抽帧」——
    # 盖掉同任务上一轮的残留进度（旧文件可能还写着「完成」），轮询端马上有阶段可看。
    if on_task is not None:
        try:
            on_task(task_id)
        except Exception:                    # noqa: BLE001 —— 显示用途，不影响拆解
            pass
    try:
        sys.path.insert(0, str(ROOT / "pipeline" / "reverse"))
        from progress import write_progress
        write_progress(task_path, "抽帧")
    except Exception:                        # noqa: BLE001 —— 同上
        pass

    try:
        # 1800s：输入不限长度（本期范围），长片大拆解实测可超 10 分钟；提前完成自然提前返回
        code, out, err = run_py(analyze_argv(rel, task_id, intent, extra_args),
                                timeout=1800, extra_env=extra_env)
    except subprocess.TimeoutExpired:
        raise ApiError(500, "analyze.py 超过 1800s 没跑完（长片超引擎上限，需拆段输入）") from None
    finally:
        with WRITE_LOCK:
            ANALYZING.discard(task_id)
    if code != 0:
        raise ApiError(500, f"analyze.py 退出 {code}：{tail(err) or tail(out)}")
    return {"id": task_id, "elapsed": round(time.time() - started, 1),
            "log": tail(out)}


# ---------------------------------------------------------------- 入口


def bundle_ffmpeg_source() -> str:
    """整合包 runtime：ROOT/../runtime/ffmpeg/ 里有 ffmpeg.exe → 该目录前插进 PATH。

    子进程（pipeline 调 ffprobe/ffmpeg）自动继承这份 PATH；目录不存在（开发机）不动 PATH。
    返回来源名（包内 / PATH）给启动横幅。
    """
    bundled = (ROOT.parent / "runtime" / "ffmpeg").resolve()
    if (bundled / "ffmpeg.exe").is_file():
        os.environ["PATH"] = str(bundled) + os.pathsep + os.environ.get("PATH", "")
        return "包内"
    return "PATH"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="本地 Web UI（拆解 → 命名/传图 → 提示词 + 图片包）")
    ap.add_argument("--port", type=int, default=8776)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true", help="不自动开浏览器")
    ap.add_argument("--no-watchdog", action="store_true",
                    help="不启后台假运行态对账扫描（AT-18；等价 WANREPRO_WATCHDOG=0，探针/调试用）")
    args = ap.parse_args(argv)

    ffmpeg_src = bundle_ffmpeg_source()

    # M6-T8 后：开发机不再要求本地页面文件——画布在 vite :3000；整合包有 web-dist。
    if WEB_DIST_ROOT is not None and not (WEB_DIST_ROOT / "index.html").is_file():
        print(f"[FAIL exit=2] 缺页面：{WEB_DIST_ROOT / 'index.html'}", file=sys.stderr)
        return 2

    try:
        httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as err:
        print(f"[FAIL exit=2] 端口 {args.port} 起不来：{err}", file=sys.stderr)
        return 2

    url = f"http://{args.host}:{args.port}/"
    print(f"[OK] {url}  （Ctrl+C 停）", flush=True)
    print(f"     仓库：{ROOT}", flush=True)
    print(f"     ffmpeg: {ffmpeg_src}", flush=True)
    # AT-18 §5.2：后台对账看门狗（首个 30s 后首跑，之后每 300s）。可关：--no-watchdog 或
    # WANREPRO_WATCHDOG=0（隔离探针必须带上，免自动降级干扰夹具）。
    watchdog_on = (not args.no_watchdog
                   and (os.environ.get("WANREPRO_WATCHDOG", "1") or "").strip() != "0")
    watchdog_started = hub_api.start_lost_run_watchdog() if watchdog_on else False
    print(f"     假运行态对账：{'每 300s 自动扫描' if watchdog_started else '已关闭（手动 scan 入口仍可用）'}",
          flush=True)
    # AT-19 §3：启动即对账——bind + [OK] 之后立刻后台跑**一次** apply 扫描（不等 watchdog 首跑
    # 的 30s）。可关：WANREPRO_BOOT_RECONCILE=0（只影响这一条，watchdog 另有开关）。
    boot_on = (os.environ.get("WANREPRO_BOOT_RECONCILE", "1") or "").strip() != "0"
    if boot_on:
        hub_api.startup_reconcile_async()
        print("     启动即对账：已触发（结果行随扫描完成打印）", flush=True)
    else:
        print("     启动即对账：已关闭（WANREPRO_BOOT_RECONCILE=0）", flush=True)
    if not args.no_browser and WEB_DIST_ROOT is not None:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()  # 整合包才开页；开发机画布在 vite :3000
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[ui] 已停", flush=True)
    finally:
        hub_api.stop_lost_run_watchdog()
        # AT-19 §1.5 停服语义：Ctrl+C **不杀执行体**（执行体与 server 解耦正是目标本身），
        # 但必须可见——重启后启动对账会按磁盘证据接管其状态。
        alive = [rec for rec in hub_api.HUB_JOBS.values()
                 if rec.get("mode") == "process" and rec.get("status") == "running"
                 and rec.get("pid")]
        for rec in alive[:5]:
            print(f"[ui] 拆解执行体仍在后台运行（pid={rec.get('pid')} kind={rec.get('kind')} "
                  f"job={rec.get('local_id')}），重启后启动对账会接管其状态", flush=True)
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
