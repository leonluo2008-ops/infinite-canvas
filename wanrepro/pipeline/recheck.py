#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""复检清单：把「机器拿不准」和「机器与模型对不上」的行摆到人面前，一条条定。

为什么要有它
------------
pop-3-b3 的首帧行里「背对镜头」与「视线看向镜头」两处打转；同一份拆解的分镜表第 1
镜又写「朝向：侧身偏右转正」。这类「模型自己前后不一致 / 模型判断与机器实测打架」
的地方，机器不许自己选，也不许自己编句子：清单只把原样摆出来，由人点「用建议 / 保
留原样 / 自己改」。方案见 docs/archive/20261103/方案-人工复检-20261102.md。

**本模块不主张任何写作规范**。「首帧该怎么写」不是这里能定的（样本量 1，范例里
也找不到依据）。所以 suggest 只从**同一份材料**里原样照搬，一个自己编的字都没有：

- 首帧朝向 → 优先用 `看帧.json` 里首帧窗口的「描述」（机器取原分辨率帧、每帧单独问一次
  读出来的画面描述，2026-11-02 起）；没有这个文件，退回分镜表第 1 镜的「朝向」「视线」
  两列原样拼接（行首「首帧：」是本行原本就有的键，不是新增内容；去掉它，另外三套模板
  就拿不到这一块了）。
- 一镜到底 → 按机器数出来的硬切次数把「是 / 否」换成答案。
- 其余（自陈不确定、剪辑列冲突）→ suggest 留空，只提示人自己看。

三个来源（都能机器判；判不出就不报，不假装全覆盖）
--------------------------------------------------
1. 首帧朝向：首帧行里「背对镜头」与「视线看着镜头」同时出现；或首帧行的朝向与分镜表第
   1 镜「朝向」列**真相反**（正对 ↔ 背对；侧对 / 侧背 / 背对 之间是相邻态，不报）；或与
   `看帧.json` 里首帧窗口的朝向/视线真相反。
2. 机器实测冲突：直接复用 pipeline/reverse/extract.py 的 check_facts()——「硬切几次」
   「首帧窗口里人朝哪边」这套事实只有那一份实现，不在这里重写一份。
3. 模型自陈不确定：拆解里自己写了「看不清 / 不确定 / 疑似」的行。

写法（都是硬约束）
------------------
- 只改「那一行」：首帧只换首帧行，一镜到底只换一镜到底行；改前不留 .bak，直接原子
  写（同目录临时文件 + os.replace）。
- 「保留原样」一个字节都不动，只把状态记 kept；「用建议」改完按新拆解重算一遍，并
  把这一条记成 replaced。两种都要把 analysis/reverse/<id>/复检清单.json 更新掉。
- 已定过态（kept / replaced）的条目不会再跳出来提醒：原文没变就沿用它原来的结论。
- 提示词正文里绝不出现「待复检 / 已复检 / 置信度」这类字眼：清单是给人看的文件，不
  进提示词（判据 R4）。清单本身也从不参与渲染。

用法
----
    py -3 pipeline/recheck.py --id <id>                  # 列清单（顺便落盘）
    py -3 pipeline/recheck.py --id <id> --use <项id>     # 用建议改写拆解.md 那一行
    py -3 pipeline/recheck.py --id <id> --use <项id> --text "自己写的一行"
    py -3 pipeline/recheck.py --id <id> --keep <项id>    # 保留原样，文件不动
    py -3 pipeline/recheck.py --id <id> --json           # 给界面读的一行 JSON

`--id` 既认任务 id（analysis/reverse/<id>），也认目录路径（测试夹具用）。
退出码：0 成功｜2 任务/文件/复检项问题（stderr 一句话）。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ANALYSIS = ROOT / "analysis" / "reverse"
REVERSE_DIR = ROOT / "pipeline" / "reverse"

DISSECTION = "拆解.md"
LIST_NAME = "复检清单.json"

# 层标题（`## 七 分镜表`）。只认十层里带中文序号的层，别的 `## x` 不算；
# 十要认——第七层的表格扫到第十层标题就该停，不能把「十 图文与收束」的行当分镜表读。
LAYER_RE = re.compile(r"^\s*#{1,6}\s*([一二三四五六七八九十])(?:\s|$)")
SEP_ROW_RE = re.compile(r"^\|[\s:|-]+\|$")

FIRST_FRAME_RE = re.compile(r"^\s*(?:[-*+]\s+)?首帧\s*[：:]")
# 模型自陈没把握；「部分可辨」是第十层图文的新表达（读不清照实写能读出的部分），
# 同样是模型自陈没把握、要人工核对的行（2026-11-04 路 A 审计 S3）
UNCERTAIN_WORDS = ("看不清", "不确定", "疑似", "部分可辨")
# 行首列表符号：写回时保留它（首帧行带 `- `，去掉的话 render 会把它并进上一块）
PREFIX_RE = re.compile(r"^([ \t]*(?:[-*+][ \t]+)?)(.*)$")
ONE_TAKE_RE = re.compile(r"一镜到底\s*[：:]")
FRAME_FACTS_NAME = "看帧.json"


class RecheckError(RuntimeError):
    """一句话能说清的错误：命令行打到 stderr，界面回给页面。"""


# ---------------------------------------------------------------- 文件读写


def resolve(source: str) -> Path:
    """任务 id 或目录路径 → 分析目录。"""
    raw = str(source or "").strip()
    if not raw:
        raise RecheckError("要一个任务 id（analysis/reverse/<id>）")
    path = Path(raw)
    if path.is_absolute() or path.is_dir():
        if not path.is_dir():
            raise RecheckError(f"任务目录不存在：{raw}")
        return path
    folder = ANALYSIS / raw
    if not folder.is_dir():
        raise RecheckError(f"任务不存在：{raw}（analysis/reverse/ 下没有这个目录）")
    return folder


def dissection_of(folder: Path) -> Path:
    path = folder / DISSECTION
    if not path.is_file():
        raise RecheckError(f"{folder.name} 还没有 {DISSECTION}，先跑一次拆解")
    return path


def _read(path: Path) -> tuple[list[str], str, bytes]:
    """→ (按行拆开的文本, 换行符, BOM)。读之前先记下这两样，写回时原样还回去。"""
    data = path.read_bytes()
    bom = b"\xef\xbb\xbf" if data.startswith(b"\xef\xbb\xbf") else b""
    body = data[len(bom):].decode("utf-8", errors="replace")
    newline = "\r\n" if "\r\n" in body else "\n"
    return body.splitlines(), newline, bom


def _write_lines(path: Path, lines: list[str], newline: str, bom: bytes) -> None:
    """原子写：同目录临时文件 → fsync → os.replace；中途失败不留半截文件。"""
    text = newline.join(lines) + newline
    tmp = path.with_name(f".{path.name}.tmp{os.getpid()}")
    try:
        with open(tmp, "wb") as handle:
            handle.write(bom + text.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------- 小工具


def split_prefix(line: str) -> tuple[str, str]:
    """一行 → (行首缩进/列表符号, 正文)。改的时候只换正文，符号原样留着。"""
    match = PREFIX_RE.match(line)
    return (match.group(1), match.group(2)) if match else ("", line)


def _now() -> str:
    """人定这一条的时刻（本地时区，秒级）。清单是给人看的，时间要能对上手表。"""
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _cells(line: str) -> list[str] | None:
    """竖线行 → 单元格。**不假设行首有 `|`**：拆解里的分镜表写成 `1 | 00:00-00:02 | …`，
    表格工具导出的才带首尾竖线，两种都出现过（解析不得假设模型格式）。"""
    s = line.strip()
    if s.count("|") < 2 or SEP_ROW_RE.fullmatch(s):
        return None
    return [cell.strip() for cell in s.strip("|").split("|")]


def _facing(text: str, field: bool = False) -> str:
    """朝向 → 正对 / 侧对 / 侧背 / 背对 / 空（说不清就不判）。

    判据只有一份：pipeline/reverse/extract.py 的 facing_class()。
    2026-11-02 实测两件事：旧写法把「背朝画面右侧」读成「背对镜头」，把一句正确的话
    报成矛盾；硬三选一又把「侧背」读成「背对」。所以这里是四档 + 不确定，而「算不算
    矛盾」只用 facing_conflict()——只有 正对 ↔ 背对 是真相反，相邻两档不报。
    """
    face = _facts().facing_class(text, field=field)
    return "" if face == "不确定" else face


def _layer_start(lines: list[str], numeral: str) -> int | None:
    for index, line in enumerate(lines):
        match = LAYER_RE.match(line)
        if match and match.group(1) == numeral:
            return index
    return None


def _shot1(lines: list[str]) -> dict | None:
    """分镜表第 1 镜的「朝向」「视线」两格 + 它所在的 0 基行号；表或列不在 → None。"""
    start = _layer_start(lines, "七")
    if start is None:
        return None
    header: list[str] | None = None
    for index in range(start + 1, len(lines)):
        if LAYER_RE.match(lines[index]):
            break                                   # 出了第七层
        cells = _cells(lines[index])
        if cells is None:
            continue
        if header is None:
            if any("镜号" in cell for cell in cells):
                header = cells
            continue
        if not re.fullmatch(r"\d+", cells[0]):
            continue

        def pick(name: str) -> str:
            column = next((i for i, cell in enumerate(header or []) if cell == name), None)
            return cells[column] if column is not None and column < len(cells) else ""

        return {"朝向": pick("朝向"), "视线": pick("视线"), "line": index}
    return None


def suggest_from_shot1(cells: dict) -> str:
    """建议值 = 第 1 镜「朝向」「视线」两列原样拼接，一个自己编的字都不加。

    两列都是模型自己写的（同一份拆解里），人挑哪个都对；机器只负责把话搬过来。
    「—」「无」这类占位不算内容，跳过。
    """
    parts = [cells.get(key, "") for key in ("朝向", "视线")]
    return "，".join(part for part in parts if part and part not in ("—", "-", "无", "/"))


def machine_cuts(text: str) -> list[float] | None:
    """从拆解.md 的规格行读回机器实测的硬切次数；读不到 → None（不猜、不重测）。

    为什么不在这里再跑一次 ffprobe：硬切次数在拆解落盘时已经量过并写进「> 规格」行，
    那是 check_facts() 判冲突用的同一份事实。重测一遍既慢又可能读到另一份片子；
    这里只借长度，判据本身只数次数。
    """
    match = re.search(r"硬切\s*(\d+)\s*次", text)
    if not match:
        return None
    return [0.0] * int(match.group(1))


_FACTS = None


def _facts():
    """按路径导 extract.py——机器事实只有那一份实现，不许在这里抄第二份。

    （check_facts / camera_relation / gaze_relation / frame_facts_conflicts 都在里面。）
    """
    global _FACTS
    if _FACTS is None:
        path = REVERSE_DIR / "extract.py"
        spec = importlib.util.spec_from_file_location("_recheck_extract", path)
        if spec is None or spec.loader is None:
            raise RecheckError(f"导不进 {path}：机器实测冲突没法判")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _FACTS = module
    return _FACTS


_FRAMES = None


def _frames():
    """按路径导 frame_facts.py——读 看帧.json 只有那一份实现。"""
    global _FRAMES
    if _FRAMES is None:
        path = REVERSE_DIR / "frame_facts.py"
        spec = importlib.util.spec_from_file_location("_recheck_frame_facts", path)
        if spec is None or spec.loader is None:
            raise RecheckError(f"导不进 {path}：看帧判定读不出来")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _FRAMES = module
    return _FRAMES


def frame_facts_of(folder: Path) -> dict | None:
    """任务目录里的 看帧.json；没有 / 读不了 → None（退回旧逻辑，不编）。"""
    try:
        return _frames().load_facts(folder)
    except Exception:                                  # noqa: BLE001 —— 判定缺失不该打垮清单
        return None


# ---------------------------------------------------------------- 三个来源


def _first_frame_items(lines: list[str], frame_facts: dict | None = None) -> list[dict]:
    index = next((i for i, line in enumerate(lines) if FIRST_FRAME_RE.match(line)), None)
    if index is None:
        return []
    _prefix, body = split_prefix(lines[index])
    facts = _facts()
    back_hit = facts.BACK_ON_CAMERA_RE.search(body)      # 「背对镜头」这类，不是「背朝画面右侧」
    gaze_hit = facts.gaze_clause(body)
    shot = _shot1(lines)
    own = _facing(body)
    table = _facing(shot["朝向"], field=True) if shot else ""

    # 同一句里「背对镜头」又说「视线看着镜头」——两件事打架
    in_line = bool(back_hit and gaze_hit and facts.gaze_relation(gaze_hit) == "camera")
    # 与分镜表第 1 镜对不上：只有真相反（正对 ↔ 背对）才单独立一条；
    # 相邻两档（侧对 / 侧背 / 背对）只当上下文——同一张图两次判定就会在这几档之间摆。
    with_table = bool(own and table and facts.facing_conflict(own, table))
    table_differs = bool(own and table and own != table)
    # 与「看帧判定」首帧窗口对不上（机器按窗口取原分辨率帧、每帧单独问出来的）
    against_frames = facts.frame_facts_conflicts(body, frame_facts) if frame_facts else []
    if not (in_line or with_table or against_frames):
        return []

    # why 只陈述「哪两处对不上」这个事实，不写「应该怎么怎么写」——怎么改是人的事。
    reasons: list[str] = []
    if in_line:
        reasons.append(f"这句话里「{back_hit.group(0)}」与「{gaze_hit}」两处打转")
    if shot is not None and (with_table or (table_differs and (in_line or against_frames))):
        reasons.append(f"同一份拆解的分镜表第 1 镜写的是「朝向：{shot['朝向']}」"
                       + (f"「视线：{shot['视线']}」" if shot["视线"] else "")
                       + ("，与首帧这句对不上" if in_line else "，首帧这句是另一个方向"))
    reasons.extend(against_frames)
    why = "；".join(reasons) + "。"
    if against_frames:
        why += "看帧判定是机器按窗口取原分辨率帧、每帧单独问出来的。"
    why += "两处都是模型自己写的，请你挑一个或自己改。"

    description = _frames().description_of(frame_facts, 0.0) if frame_facts else ""
    cells = suggest_from_shot1(shot) if shot else ""
    if description:
        suggest = f"首帧：{description}"
        why += "建议值取自看帧判定里首帧窗口的画面描述，原样照搬，没有加字。"
    elif cells:
        suggest = f"首帧：{cells}"
        why += "建议值取自本拆解的分镜表第 1 镜的「朝向 / 视线」两列，原样照搬，没有加字。"
    else:
        suggest = ""
        why += "机器没有可照搬的原话，建议值留空。"

    # 行首「首帧：」是本行原本就有的键：去掉它，另外三套模板就拿不到这一块了。
    if suggest and suggest == body:
        suggest = ""
    return [{
        "id": "firstframe-orientation",
        "kind": "首帧朝向",
        "where": f"{DISSECTION} 七 分镜表 · 首帧行（第 {index + 1} 行）",
        "original": body,
        "suggest": suggest,
        "why": why,
        "status": "pending",
        "_line": index,
    }]


def _machine_fact_items(text: str, lines: list[str]) -> list[dict]:
    cuts = machine_cuts(text)
    if cuts is None:
        return []                                      # 没有规格行 → 判不了，不报
    out: list[dict] = []
    for conflict in _facts().check_facts(text, cuts):
        if "看帧" in conflict:
            # 首帧行与看帧判定对不上，已经由 firstframe-orientation 那条承载，
            # 同一条行不重复开两条（判定本身仍是 check_facts 里那一份实现）。
            continue
        one_take = next((i for i, line in enumerate(lines) if ONE_TAKE_RE.search(line)), None)
        if "一镜到底" in conflict and one_take is not None:
            _prefix, body = split_prefix(lines[one_take])
            answer = "否" if cuts else "是"
            fixed = re.sub(r"(一镜到底\s*[：:]\s*)\S+", lambda m: m.group(1) + answer, body)
            out.append({
                "id": "machine-fact-onetake",
                "kind": "机器实测冲突",
                "where": f"{DISSECTION} 七 分镜表 · 一镜到底行（第 {one_take + 1} 行）",
                "original": body,
                "suggest": fixed if fixed != body else "",
                "why": f"{conflict}。硬切次数是 ffmpeg 数出来的、可复算，这一行按机器结论"
                       f"应当是「一镜到底：{answer}」。",
                "status": "pending",
                "_line": one_take,
            })
            continue
        clip = next((i for i, line in enumerate(lines)
                     if (cells := _cells(line)) and any("剪辑" in cell for cell in cells)), None)
        out.append({
            "id": "machine-fact-clip",
            "kind": "机器实测冲突",
            "where": f"{DISSECTION} 七 分镜表 · 转场方式列",
            "original": conflict,
            "suggest": "",
            "why": f"{conflict}。转场方式散在整张表里，机器不改表：标了「剪辑」的镜头"
                   f"请你逐行看一眼。",
            "status": "pending",
            "_line": clip if clip is not None else (_layer_start(lines, "七") or 0),
        })
    return out


def _uncertain_items(lines: list[str]) -> list[dict]:
    out: list[dict] = []
    for index, raw in enumerate(lines):
        stripped = raw.strip()
        if not stripped or stripped.startswith("<!--"):
            continue
        hit = next((word for word in UNCERTAIN_WORDS if word in stripped), "")
        if not hit:
            continue
        _prefix, body = split_prefix(raw)
        out.append({
            "id": f"uncertain-{len(out) + 1}",
            "kind": "模型自陈不确定",
            "where": f"{DISSECTION} 第 {index + 1} 行",
            "original": body,
            "suggest": "",
            "why": f"模型自己在这一行写了「{hit}」——它对这个细节没把握。机器看不见原片里"
                   f"的实物，判不了这条是真看到的还是猜的。",
            "status": "pending",
            "_line": index,
        })
    return out


def detect(source: str) -> list[dict]:
    """现算一遍复检项（只读拆解.md 与 看帧.json，不读旧清单）。条目按文件顺序排。"""
    folder = resolve(source)
    lines, _newline, _bom = _read(dissection_of(folder))
    text = "\n".join(lines)
    facts = frame_facts_of(folder)
    items = (_first_frame_items(lines, facts) + _machine_fact_items(text, lines)
             + _uncertain_items(lines))
    items.sort(key=lambda item: (item["_line"], item["id"]))
    return items


# ---------------------------------------------------------------- 清单文件


def public(item: dict) -> dict:
    """去掉内部锚点（_line）：清单是给人看的，行号已经在 where 里。"""
    return {key: value for key, value in item.items() if not key.startswith("_")}


def load(source: str) -> list[dict]:
    path = resolve(source) / LIST_NAME
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []                                       # 清单坏了不该把任务页打成 500
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def save(source: str, items: list[dict]) -> None:
    path = resolve(source) / LIST_NAME
    text = json.dumps([public(item) for item in items], ensure_ascii=False, indent=2) + "\n"
    tmp = path.with_name(f".{path.name}.tmp{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def merge(old: list[dict], fresh: list[dict]) -> list[dict]:
    """旧清单 + 现算结果 → 新清单。

    人已经定过态（kept / replaced）的条目：原文没变就沿用结论，不再重复提醒——否则
    「保留原样」点完下一刷又跳出来，等于逼人改。原文变了就回到 pending 重新问。
    """
    previous = {item.get("id"): item for item in old if item.get("id")}
    out: list[dict] = []
    for item in fresh:
        prev = previous.get(item["id"])
        if prev:
            status = prev.get("status")
            if status == "kept" and prev.get("original") == item["original"]:
                item["status"] = "kept"
                item["decidedAt"] = prev.get("decidedAt", "")
            elif status == "replaced" and (prev.get("applied") or prev.get("text")) in (
                    item["original"], item.get("suggest")):
                item["status"] = "replaced"
                item["original"] = prev.get("original", item["original"])
                item["applied"] = prev.get("applied") or prev.get("text")
                item["decidedAt"] = prev.get("decidedAt", "")
        out.append(item)
    seen = {item["id"] for item in out}
    for prev in old:                                    # 已处理的记录留着：事后能查谁定了什么
        if prev.get("id") in seen or prev.get("status") not in ("kept", "replaced"):
            continue
        out.append(prev)
    return out


def pending_count(items: list[dict]) -> int:
    return sum(1 for item in items if item.get("status") == "pending")


def refresh(source: str) -> list[dict]:
    """现算 + 并进旧清单 + 落盘（内容没变就不动文件）。"""
    old = load(source)
    items = merge(old, detect(source))
    if json.dumps([public(i) for i in items], ensure_ascii=False) != \
            json.dumps(old, ensure_ascii=False):
        save(source, items)
    return items


# ---------------------------------------------------------------- 写回


def _clean_line(text: str) -> str:
    """人能写进来的那一行：只许一行，行首列表符号剥掉（写回时用原行的符号）。"""
    value = str(text or "").replace("\r", "\n")
    if "\n" in value.strip():
        raise RecheckError("只能改一行：把这段话里的换行删掉再提交")
    return PREFIX_RE.match(value.strip()).group(2).strip() if value.strip() else ""


def apply(source: str, item_id: str, action: str, text: str | None = None) -> list[dict]:
    """人定了一条：use 改那一行并记 replaced｜keep 只记 kept。返回更新后的清单。"""
    if action not in ("use", "keep"):
        raise RecheckError(f"action 只支持 use / keep，收到 {action!r}")
    items = refresh(source)                             # 先把清单对齐到当前拆解
    target = next((item for item in items if item.get("id") == item_id), None)
    if target is None:
        raise RecheckError(f"没有这一条复检项：{item_id}（跑一次 --id 看最新清单）")

    if action == "keep":
        target["status"] = "kept"
        target["decidedAt"] = _now()
        target.pop("text", None)
        target.pop("applied", None)
        save(source, items)
        return items

    fresh = next((item for item in detect(source) if item["id"] == item_id), None)
    if fresh is None:
        raise RecheckError("这一条在当前拆解里已经没有对应行了（可能刚被改过），刷新看看")
    new_text = _clean_line(text) if text and str(text).strip() else _clean_line(fresh.get("suggest") or "")
    if not new_text:
        raise RecheckError("这一条没有机器建议，只能「保留原样」或自己写一行")

    path = dissection_of(resolve(source))
    lines, newline, bom = _read(path)
    index = fresh["_line"]
    prefix, body = split_prefix(lines[index])
    if body != fresh["original"]:
        raise RecheckError("拆解这一行刚被人改过，刷新后再定")
    before_text = body                                  # 改前原句，留痕要用它
    decided_at = _now()
    lines[index] = prefix + new_text
    _write_lines(path, lines, newline, bom)

    after = merge(items, detect(source))
    hit = next((item for item in after if item["id"] == item_id), None)
    if hit is None:                                     # 冲突消失 → 记一条历史，别让它没影
        hit = {key: target[key] for key in ("id", "kind", "where", "original", "suggest", "why")
               if key in target}
        after.append(hit)
    # 2026-11-02 实测的留痕 bug：`use` 之后 original 被现算出来的新句覆盖，事后看不出改前
    # 是哪句。改前原句（original）、改后文案（applied）、定的时候（decidedAt）三样都要留。
    hit["status"] = "replaced"
    hit["original"] = before_text
    hit["applied"] = new_text
    hit["decidedAt"] = decided_at
    hit.pop("text", None)
    save(source, after)
    return after


# ---------------------------------------------------------------- 命令行


def render_listing(task: Path, items: list[dict]) -> str:
    pending = pending_count(items)
    head = f"任务 {task.name} · 待复检 {pending} 项（共 {len(items)} 条记录）"
    if not items:
        return head + "\n（没有机器拿不准的行）"
    blocks = [head]
    for number, item in enumerate(items, start=1):
        blocks.append(f"\n[{number}] {item.get('status', 'pending')}  {item.get('id')}  "
                      f"{item.get('kind', '')}")
        blocks.append(f"    位置：{item.get('where', '')}")
        blocks.append(f"    原文：{item.get('original', '')}")
        blocks.append(f"    建议：{item.get('suggest') or '（机器没有建议，请自己写一行）'}")
        if item.get("applied"):
            blocks.append(f"    已改为：{item.get('applied')}"
                          + (f"（{item.get('decidedAt')}）" if item.get("decidedAt") else ""))
        blocks.append(f"    为什么：{item.get('why', '')}")
    return "\n".join(blocks)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="复检清单：机器拿不准 / 与机器实测冲突的行，交给人定")
    parser.add_argument("--id", required=True, help="任务 id（analysis/reverse/<id>）或目录路径")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--use", metavar="项id", default="", help="用这一条的（建议或 --text）改写拆解.md 那一行")
    action.add_argument("--keep", metavar="项id", default="", help="保留原样：只记 kept，文件不动")
    parser.add_argument("--text", default="", help="配合 --use：不用建议，改写成你写的这一行")
    parser.add_argument("--json", action="store_true", help="给界面读：一行 JSON")
    args = parser.parse_args(argv)

    try:
        task = resolve(args.id)
        wrote: dict | None = None
        if args.use:
            items = apply(task, args.use, "use", args.text or None)
            wrote = {"id": args.use, "action": "use",
                     "applied": next((i.get("applied") for i in items
                                      if i.get("id") == args.use), "")}
        elif args.keep:
            items = apply(task, args.keep, "keep")
            wrote = {"id": args.keep, "action": "keep"}
        else:
            items = refresh(task)
    except RecheckError as err:
        print(str(err), file=sys.stderr)
        return 2

    if args.json:
        payload: dict = {"task": task.name, "items": [public(i) for i in items],
                         "pending": pending_count(items)}
        if wrote:
            payload["wrote"] = wrote
        print(json.dumps(payload, ensure_ascii=False))
        return 0

    print(render_listing(task, items))
    if wrote:
        print(f"\n已{wrote['action']}：{wrote['id']}")
        if wrote["action"] == "use":
            print(f"拆解.md 那一行现在是：{wrote.get('applied', '')}")
            print("提示词还没重渲染：界面里点「用建议」会自动重渲染；命令行请自己跑 "
                  "py -3 pipeline/reverse/render.py --id <id> --template <tpl>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
