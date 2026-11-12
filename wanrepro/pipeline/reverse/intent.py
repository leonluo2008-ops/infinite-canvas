#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""根据九层拆解、清单行号和用户意图生成三档资产建议。

这是第二次、纯文本模型调用：不上传视频。调用方式复用 extract.py 的密钥读取、
直连 opener、响应解析和空响应重试策略。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import ssl
import sys
import time
import urllib.error
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT_PATH = REPO_ROOT / "prompts" / "意图建议.md"
SUGGESTION_NAME = "意图建议.md"
DIALOGUE_NAME = "台词改写.md"
DEFAULT_TIMEOUT = 300.0

SECTION_NAMES = ("必须换", "建议准备", "可沿用")
DIALOGUE_HEADING = "台词改写"
NO_DIALOGUE_MARK = "无台词"
NOT_APPLICABLE_MARK = "不适用"
# AT-17 B1：八 声音 v2.6 起逐句带说话人（枚举仅两类），台词改写.md 表尾追加「说话人」列。
SPEAKER_COLUMN = "说话人"
SPEAKER_LABELS = ("画外音", "出镜者")
SPEAKER_RE = re.compile(r"说话人\s*[：:]\s*([^｜|\s，,；;。]+)")
DIALOGUE_LINE_MARK = "台词："
NEW_SCHEMA_MARKS = ("BGM.情绪", "BGM.节奏", "音效点：", DIALOGUE_LINE_MARK)
# 「没人说话」的写法：不是语种（v2.6 起拆解会明写 `人声语言：无`）
NO_SPEECH_VALUES = ("无", "无台词", "无对白", "不适用")
# AT-17 B2c：音频三态（B0② 合同）。字段名/枚举值全链一套，禁止混用（B0 附注 4）——
# 字段恒为 `audio_mode`，值只许这三个中文词；落盘走 `audio_mode.json` 旁路文件，
# 严禁写进三档建议正文（ITEM_RE 只认 `行号|理由|镜次` 行，归一化会丢弃别的行）。
AUDIO_MODE_FIELD = "audio_mode"
AUDIO_MODE_FILE = "audio_mode.json"
AUDIO_MODE_KEEP = "沿用原片"
AUDIO_MODE_ADJUST = "按新主体调整"
AUDIO_MODE_MUTE = "不要"
AUDIO_MODE_VALUES = (AUDIO_MODE_KEEP, AUDIO_MODE_ADJUST, AUDIO_MODE_MUTE)
NO_BGM_MARKS = ("无bgm", "没有bgm")       # 归一后小写比对：`无BGM` 系列 / `没有BGM`
AUDIO_JSON_NAME = "audio.json"
ITEM_RE = re.compile(
    r"^\s*[-*+•]?\s*(?P<row>\d{1,4})\s*[|｜]\s*理由\s*[：:]\s*"
    r"(?P<reason>.*?)\s*[|｜]\s*镜次\s*[：:]\s*(?P<shots>.*?)\s*$"
)
SHOT_RE = re.compile(r"^\s*(?:[*+\-]\s*)?\|?\s*(\d{1,3})\s*(?:[|｜]|$)")
HEADING_RE = re.compile(r"^\s*#{1,6}\s*(.+?)\s*$")
# 时间码：拆解里是 `00:00-00:03`，模型可能写成 `00:00–00:03` / `00:00~00:03`
TIME_CODE_RE = re.compile(r"\d{1,2}:\d{2}(?:\.\d+)?\s*[-–—~～]\s*\d{1,2}:\d{2}(?:\.\d+)?")
QUOTE_CHARS = "\"'“”‘’「」『』"


def _load_extract_module():
    """按路径导入同目录的 extract.py，避免依赖目录是否是 Python package。"""
    path = REPO_ROOT / "pipeline" / "reverse" / "extract.py"
    spec = importlib.util.spec_from_file_location("_reverse_extract_for_intent", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"导不进 {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# 延迟导入：本模块要能被 render.py 当纯字符串工具导入（只要 rewrite_dialogue）。
# 顶层就 exec extract.py 会把 ssl/urllib 那一整套带进渲染进程，也没必要。
_extract = None


def _extract_mod():
    global _extract
    if _extract is None:
        _extract = _load_extract_module()
    return _extract


class IntentError(RuntimeError):
    """意图建议调用或输出校验失败。"""


def _delete_old(target: Path) -> None:
    try:
        target.unlink(missing_ok=True)
    except OSError as exc:
        raise IntentError(f"删除旧 {target} 失败：{exc}") from exc


def _read_prompt() -> str:
    try:
        raw = PROMPT_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise IntentError(f"读不到提示词源头 {PROMPT_PATH}：{exc}") from exc
    if not raw.strip():
        raise IntentError(f"提示词源头为空：{PROMPT_PATH}")
    return raw.strip()


def _build_text_payload(prompt: str) -> dict:
    return {"contents": [{"parts": [{"text": prompt}]}]}


def call_model(prompt: str, api_key: str, timeout: float = DEFAULT_TIMEOUT):
    """纯文本调用；复用 extract.py 的直连、响应解析和退避参数。"""
    ext = _extract_mod()
    opener = ext._build_opener()
    payload = _build_text_payload(prompt)
    attempts = 1 + len(ext.BACKOFF_SECONDS)
    started = time.monotonic()
    last_error = "未知失败"

    for attempt in range(1, attempts + 1):
        try:
            print(f"[调用] 第 {attempt}/{attempts} 次 直连 {ext.MODEL_LABEL}（纯文本）", flush=True)
            raw = ext._post_generate_content(opener, api_key, payload, timeout)
            text, usage = ext._parse_response(raw)
            elapsed = time.monotonic() - started
            print(f"[成功] 纯文本建议耗时 {elapsed:.1f}s，正文 {len(text)} 字", flush=True)
            return text, usage, elapsed
        except ext.EmptyResponseError as exc:
            last_error = f"空响应：{ext.redact(str(exc), api_key)}"
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                body = ""
            last_error = f"HTTP {exc.code} {exc.reason}" + (f" / {body}" if body else "")
            last_error = ext.redact(last_error, api_key)
        except urllib.error.URLError as exc:
            last_error = ext.redact(f"URLError：{exc.reason}", api_key)
        except (ssl.SSLError, OSError) as exc:
            last_error = ext.redact(f"{type(exc).__name__}：{exc}", api_key)
        except ext.ApiError as exc:
            last_error = ext.redact(str(exc), api_key)
        except Exception as exc:
            last_error = ext.redact(f"{type(exc).__name__}：{exc}", api_key)

        print(f"[失败] 纯文本第 {attempt} 次：{last_error}", file=sys.stderr, flush=True)
        if attempt <= len(ext.BACKOFF_SECONDS):
            delay = ext.BACKOFF_SECONDS[attempt - 1]
            print(f"[退避] {delay}s 后重试", file=sys.stderr, flush=True)
            time.sleep(delay)

    raise IntentError(f"{attempts} 次纯文本尝试全部失败，最后一次：{last_error}")


def _section_bodies(text: str) -> dict[str, list[str]]:
    """读取三节，兼容模型多写一层 markdown # 的情况。"""
    result = {name: [] for name in SECTION_NAMES}
    current = None
    for raw_line in text.splitlines():
        heading = HEADING_RE.match(raw_line)
        if heading:
            title = heading.group(1).strip().rstrip("：:")
            current = title if title in result else None
            continue
        if current is not None:
            result[current].append(raw_line)
    return result


def valid_shot_numbers(disc_text: str) -> set[int]:
    """只从「七 分镜表」正文首列取镜号，不采信九层其他数字。"""
    bodies, _missing = _extract_mod().split_layers(disc_text)
    body = bodies.get("七 分镜表", "")
    found: set[int] = set()
    for line in body.splitlines():
        match = SHOT_RE.match(line)
        if match:
            found.add(int(match.group(1)))
    return found


def checklist_rows(checklist_text: str) -> list[dict]:
    """解析清单表，供 prompt 生成与行号校验；只取数据行。"""
    rows = []
    for line in checklist_text.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 3 or not cells[0].isdigit():
            continue
        rows.append({"row": int(cells[0]), "category": cells[1], "description": cells[2]})
    return rows


def checklist_prompt(checklist_text: str) -> str:
    """把清单压成只含行号、类别、描述前 60 字的模型输入。"""
    lines = [
        f"{item['row']}｜{item['category']}｜{item['description'][:60]}"
        for item in checklist_rows(checklist_text)
    ]
    return "\n".join(lines)


def parse_suggestions(raw: str, disc_text: str, checklist_text: str) -> tuple[str, int, int, int]:
    """过滤模型输出，返回规范三节文本、无效行号数、重复行号数、无效镜次数。"""
    sections = _section_bodies(raw)
    present = {
        match.group(1).strip().rstrip("：:")
        for match in (HEADING_RE.match(line) for line in raw.splitlines())
        if match and match.group(1).strip().rstrip("：:") in SECTION_NAMES
    }
    if present != set(SECTION_NAMES):
        missing = "、".join(name for name in SECTION_NAMES if name not in present)
        raise IntentError(f"模型输出缺少固定节：{missing}")
    valid_rows = {item["row"] for item in checklist_rows(checklist_text)}
    shot_numbers = valid_shot_numbers(disc_text)
    invalid_rows = 0
    duplicate_rows = 0
    invalid_shots = 0
    seen_rows: set[int] = set()
    output = []

    for section in SECTION_NAMES:
        output.append(f"## {section}")
        for raw_line in sections[section]:
            match = ITEM_RE.match(raw_line)
            if not match:
                continue
            row_number = int(match.group("row"))
            if row_number not in valid_rows:
                invalid_rows += 1
                continue
            if row_number in seen_rows:
                duplicate_rows += 1
                continue
            seen_rows.add(row_number)
            reason = re.sub(r"\s+", " ", match.group("reason").strip())

            kept_shots = []
            for token in re.split(r"[,，、\s]+", match.group("shots").strip()):
                if not token:
                    continue
                number_match = re.fullmatch(r"0*(\d{1,3})", token)
                if not number_match:
                    invalid_shots += 1
                    continue
                number = int(number_match.group(1))
                if number in shot_numbers:
                    kept_shots.append(str(number))
                else:
                    invalid_shots += 1
            output.append(
                f"- {row_number}｜理由：{reason}｜镜次：{', '.join(kept_shots)}"
            )
        if section != SECTION_NAMES[-1]:
            output.append("")
    return "\n".join(output).rstrip() + "\n", invalid_rows, duplicate_rows, invalid_shots


def _write_utf8(target: Path, text: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.tmp{os.getpid()}")
    try:
        temp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(temp, target)
    except OSError:
        try:
            temp.unlink()
        except OSError:
            pass
        raise


# ---------------------------------------------------------------- 台词改写（C-1）
# 模型那一次纯文本调用同时产出两样东西：三档建议 + 台词对照表。
# 对照表单独落 `台词改写.md`，由 render.py 在正文里逐句替换；拆解.md 一个字不动。


def _norm_time(code: str) -> str:
    """时间码归一：去掉空格、把各种破折号统一成半角 `-`。拆解原件不做改动。"""
    return re.sub(r"\s*[-–—~～]\s*", "-", code.strip())


def _norm_ws(text: str) -> str:
    """比对用：去掉全部空白与首尾引号。"""
    return re.sub(r"\s+", "", text.strip().strip(QUOTE_CHARS))


def _all_sections(text: str) -> dict[str, list[str]]:
    """按标题切节（不预设标题名），供台词改写节取正文。"""
    result: dict[str, list[str]] = {}
    current: str | None = None
    for raw_line in text.splitlines():
        heading = HEADING_RE.match(raw_line)
        if heading:
            current = heading.group(1).strip().rstrip("：:")
            result.setdefault(current, [])
            continue
        if current is not None:
            result[current].append(raw_line)
    return result


def dialogue_layer(disc_text: str) -> str:
    """九层「八 声音」正文；拆解没有这一层时返回空串。"""
    bodies, _missing = _extract_mod().split_layers(disc_text)
    return bodies.get("八 声音", "")


def source_language(disc_text: str) -> str:
    """拆解「八 声音」里写的语种（`人声语言：西班牙语（…）` → 西班牙语）；没有就空。

    v2.6 起没人说话的片子会明写 `人声语言：无`——那不是语种，按「没有」处理。
    """
    probe = dialogue_layer(disc_text).replace("**", "").replace("`", "")
    match = re.search(r"人声语言\s*[：:]\s*([^\s（(（；;，,。]+)", probe)
    if not match:
        return ""
    lang = match.group(1).strip()
    return "" if lang in NO_SPEECH_VALUES else lang


def dialogue_lines(disc_text: str) -> list[tuple[str, str]]:
    """拆解「八 声音」里逐句台词：[(归一时间码, 原文)]，只认「时间码 + 引号」的行。"""
    return [(row["start_end"], row["original"]) for row in dialogue_rows(disc_text)]


def dialogue_rows(disc_text: str) -> list[dict]:
    """拆解「八 声音」逐句台词 + 说话人：[{start_end, original, speaker}]。

    v2.6 形态是 `- 台词：00:01-00:03｜「原文」｜说话人：画外音`（逐句带说话人）；
    v2.5 及更早是自由散文，靠「时间码 + 引号」抽句、speaker 为空（历史兼容，不炸也不猜）。
    新形态只认带 `台词：` 标记的行——散文里夹着的音效点（时段 + 「拟声词」）不会被当成台词。
    """
    body = dialogue_layer(disc_text)
    lines = body.splitlines()
    if any(mark in body for mark in NEW_SCHEMA_MARKS):
        lines = [line for line in lines if DIALOGUE_LINE_MARK in line]
    found: list[dict] = []
    for raw in lines:
        code = TIME_CODE_RE.search(raw)
        if not code:
            continue
        quoted = re.search(r"[\"“「『](.+?)[\"”」』]", raw[code.end():])
        if not quoted:
            continue
        text = quoted.group(1).strip()
        if not text:
            continue
        speaker = ""
        match = SPEAKER_RE.search(raw)
        if match:
            speaker = next((label for label in SPEAKER_LABELS if label in match.group(1)), "")
        found.append({"start_end": _norm_time(code.group(0)), "original": text,
                      "speaker": speaker})
    return found


def _table_cells(raw: str) -> list[str] | None:
    """把一行 Markdown 表格拆成单元格；不是表格行返回 None（全角/半角竖线都认）。"""
    line = re.sub(r"\s*\|\s*", "｜", raw.strip())
    line = re.sub(r"^[-*+]\s*", "", line)
    if "｜" not in line:
        return None
    cells = [cell.strip().strip("`*_ ") for cell in line.strip().strip("｜").split("｜")]
    if all(re.fullmatch(r"[-: ]*", cell) for cell in cells):
        return None                       # 分隔行 |---|
    return cells


def parse_dialogue(raw: str, disc_text: str) -> tuple[list[dict], dict]:
    """从模型输出里解析「台词改写」节，并用拆解「八 声音」逐句核对。

    只做两件事：时间码对不对得上拆解、原文在不在拆解里。对不上就丢，不猜。
    返回 (行列表, 统计)；行字段 `start_end` / `original` / `chinese` / `speaker`。

    AT-17 B1 附注 6：说话人列**按表头名定位**（表头写了「说话人」就取那一列，中间多列也不会错位）；
    表头读不到才回退表尾位置读取（第 4 列起）；模型没给（或给的不是两类枚举）时，
    用拆解该句自己写的说话人落定——拆解是原文真源，与「时间码以拆解为准」同一口径。
    三列旧格式（无说话人列）照旧解析，speaker 为空，不崩。
    """
    stats = {
        "total": 0, "dropped": 0, "duplicates": 0, "repaired": 0,
        "fabricated": [], "missing": [], "mark": "", "speakers": 0,
    }
    section = next((lines for title, lines in _all_sections(raw).items()
                    if title.startswith(DIALOGUE_HEADING)), None)
    if section is None:
        stats["mark"] = "缺节"
        return [], stats
    flat = "\n".join(section)
    if NO_DIALOGUE_MARK in flat:
        stats["mark"] = NO_DIALOGUE_MARK
        return [], stats
    if NOT_APPLICABLE_MARK in flat:
        stats["mark"] = NOT_APPLICABLE_MARK
        return [], stats

    known_rows = dialogue_rows(disc_text)
    known_by_text = {_norm_ws(row["original"]): row["start_end"] for row in known_rows}
    known_speaker = {_norm_ws(row["original"]): row["speaker"] for row in known_rows}
    order = {_norm_ws(row["original"]): index for index, row in enumerate(known_rows)}
    rows: list[dict] = []
    seen: set[str] = set()
    speaker_index: int | None = None
    col_original, col_chinese = 1, 2

    for raw_line in section:
        cells = _table_cells(raw_line)
        if not cells or len(cells) < 3:
            continue
        if any(key in "".join(cells[:1] + cells[2:3]) for key in ("原文", "中文", "起-止")):
            # 表头行：按列名定位（说话人列找不到列名就留 None，行内回退表尾位置）
            speaker_index = next((index for index, cell in enumerate(cells)
                                  if SPEAKER_COLUMN in cell), None)
            col_original = next((index for index, cell in enumerate(cells)
                                 if "原文" in cell), 1)
            col_chinese = next((index for index, cell in enumerate(cells)
                                if "中文" in cell), 2)
            continue
        if col_original >= len(cells) or col_chinese >= len(cells):
            continue                      # 列名读出来但行比表头短：这一行不可用，跳过不猜
        code_match = TIME_CODE_RE.search(cells[0])
        original = cells[col_original].strip().strip(QUOTE_CHARS).strip()
        chinese = cells[col_chinese].strip()
        if not original or not chinese:
            stats["dropped"] += 1
            continue
        key = _norm_ws(original)
        if key not in known_by_text:
            stats["dropped"] += 1
            stats["fabricated"].append(original)
            continue                      # 拆解里没有这句 = 编造，丢
        if key in seen:
            stats["duplicates"] += 1
            continue
        seen.add(key)
        start_end = _norm_time(code_match.group(0)) if code_match else ""
        if start_end != known_by_text[key]:
            start_end = known_by_text[key]
            stats["repaired"] += 1        # 时间码没照抄拆解：以拆解为准
        speaker = ""
        if speaker_index is not None and speaker_index < len(cells):
            speaker = cells[speaker_index]
        elif speaker_index is None and len(cells) >= 4:
            speaker = cells[-1]           # 表头读不到：回退表尾位置读（防中间列错位）
        speaker = next((label for label in SPEAKER_LABELS if label in speaker), "")
        speakers = speaker or known_speaker.get(key, "")
        if speakers:
            stats["speakers"] += 1
        rows.append({"start_end": start_end, "original": original, "chinese": chinese,
                     "speaker": speakers})

    rows.sort(key=lambda row: order.get(_norm_ws(row["original"]), 999))
    stats["missing"] = [row["original"] for row in known_rows
                        if _norm_ws(row["original"]) not in seen]
    stats["total"] = len(rows)
    return rows, stats


def dialogue_markdown(rows: list[dict], mark: str = "") -> str:
    """`台词改写.md` 的规范形态：四列表格（说话人列在表尾）；没有台词时写一行标记。"""
    lines = ["# 台词改写", ""]
    if not rows:
        lines.append(mark or NO_DIALOGUE_MARK)
        return "\n".join(lines) + "\n"
    lines += [f"| 起-止 | 原文 | 中文 | {SPEAKER_COLUMN} |", "|---|---|---|---|"]
    for row in rows:
        speaker = str(row.get("speaker") or "").strip() if isinstance(row, dict) else ""
        lines.append(f"| {row['start_end']} | {row['original']} | {row['chinese']} | {speaker} |")
    return "\n".join(lines) + "\n"


def write_dialogue(out_dir: Path | str, rows: list[dict], mark: str = "") -> Path:
    """落 `台词改写.md`；没有台词时删掉旧文件，避免上一轮的表伪装成这一轮。"""
    target = Path(out_dir) / DIALOGUE_NAME
    if not rows:
        target.unlink(missing_ok=True)
        return target
    _write_utf8(target, dialogue_markdown(rows, mark))
    return target


def _row_pair(row) -> tuple[str, str]:
    """兼容 (起-止, 原文, 中文) 三元组、二元组与 dict。"""
    if isinstance(row, dict):
        return str(row.get("original", "")), str(row.get("chinese", ""))
    items = list(row)
    if len(items) >= 3:
        return str(items[1]), str(items[2])
    if len(items) == 2:
        return str(items[0]), str(items[1])
    return "", ""


def rewrite_dialogue(text: str, table) -> tuple[str, int, list[str]]:
    """把正文里的每句原文精确替换成中文。纯字符串，不调模型。

    返回 (新正文, 命中句数, 未命中原文列表)。命中按「句」算：同一句在正文里出现多次也一起替换、
    只记 1 句；正文里找不到的句子进未命中列表，由调用方写进回执，不许静默。
    """
    new_text = text
    hits = 0
    misses: list[str] = []
    for row in table or []:
        original, chinese = _row_pair(row)
        original = original.strip().strip(QUOTE_CHARS).strip()
        chinese = chinese.strip()
        if not original or not chinese:
            continue
        if original in new_text:
            new_text = new_text.replace(original, chinese)
            hits += 1
        else:
            misses.append(original)
    return new_text, hits, misses


# ---------------------------------------------------------------- 音频三态（AT-17 B2c）
# 三态作用域=BGM+音效点（B0②；台词走既有改写链）。默认值不是模型给的，而是**按 B1
# 已落盘的结构化字段推导**（BGM.情绪 != 无BGM 优先，B0 附注 2）——纯字符串，零模型调用。
#
# 落盘形态（双链同一份推导、各落各的盘）：
#   · 画布链：任务目录 `audio_mode.json` 旁路文件（不进三档建议正文，ITEM_RE 归一化丢不掉它）；
#   · hub 链：任务.json 字段 `audio_mode`（discuss 拆解完成时写；见 automation/discuss.py）。
# 删除建议文件（空意图/重跑）≠ 删这一份：两者是各自独立的文件/字段。


def _no_bgm(value: str) -> bool:
    """`无BGM` 系列的写法归一（`无 BGM` / `无BGM（仅环境音）` 都算）；只认字面，不猜语义。"""
    flat = re.sub(r"\s+", "", value).lower()
    return flat.startswith(NO_BGM_MARKS[0]) or flat == NO_BGM_MARKS[1]


def audio_json(outdir: Path | str) -> dict:
    """任务目录下的 `audio.json`（老链 analyze_audio 产物）；没有/坏了 → {}，不猜。"""
    path = Path(outdir) / AUDIO_JSON_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def audio_mode(disc_text: str, audio_meta: dict | None = None) -> dict:
    """按 B0② 默认规则推导音频三态。返回 {audio_mode, 依据, 待核对}。

    规则（优先级写死，B0 附注 2）：
      1. 拆解标了无音轨（头部 `音轨: 无` 或八层正文只剩「无音轨」）→ `不要`；
      2. 结构化 `BGM.情绪` 有值且不是 `无BGM` → `按新主体调整`（有 BGM 片）；
      3. 结构化 `BGM.情绪` = `无BGM` → `沿用原片`（无 BGM 片）；
      4. 结构化字段缺失（v2.5 老产物/模型漏写）→ 用 `audio.json` 的 `music_likely` 兜底；
      5. `music_likely` 也读不到 → `沿用原片` + `待核对=True`：**不把「不知道」写成「无BGM」**
         （附注 2：不得猜成无 BGM，留给人核对）。沿用原片=不动原片音频，是唯一零风险的缺省。
    """
    ext = _extract_mod()
    body = ext.sound_body(disc_text)
    plain = body.replace("**", "").strip()
    info = {"audio_mode": AUDIO_MODE_KEEP, "依据": "", "待核对": False}
    if ext.audio_track_flag(disc_text) is False or plain == ext.NO_AUDIO_TEXT:
        info.update({"audio_mode": AUDIO_MODE_MUTE, "依据": "拆解标记无音轨"})
        return info
    parsed = ext.parse_sound_layer(body)
    mood = str(parsed["bgm"].get("BGM.情绪") or "").strip()
    if mood and not _no_bgm(mood):
        info.update({"audio_mode": AUDIO_MODE_ADJUST, "依据": f"BGM.情绪={mood}"})
        return info
    if mood:
        info.update({"audio_mode": AUDIO_MODE_KEEP, "依据": "BGM.情绪=无BGM"})
        return info
    music = (audio_meta or {}).get("music_likely") if isinstance(audio_meta, dict) else None
    if music is True:
        info.update({"audio_mode": AUDIO_MODE_ADJUST,
                     "依据": "八 声音无结构化 BGM 字段，按 audio.json music_likely=true 兜底"})
        return info
    if music is False:
        info.update({"audio_mode": AUDIO_MODE_KEEP,
                     "依据": "八 声音无结构化 BGM 字段，按 audio.json music_likely=false 兜底"})
        return info
    info.update({"依据": "八 声音无结构化 BGM 字段、audio.json 也读不到——不猜无BGM，请人工核对",
                 "待核对": True})
    return info


def read_audio_mode(outdir: Path | str) -> dict:
    """读回 `audio_mode.json`；缺失/坏了/值不在枚举内 → {}（读侧不猜、不补）。"""
    path = Path(outdir) / AUDIO_MODE_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    value = str(data.get(AUDIO_MODE_FIELD) or "").strip()
    if value not in AUDIO_MODE_VALUES:
        return {}
    return {"audio_mode": value, "依据": str(data.get("依据") or ""),
            "待核对": bool(data.get("待核对"))}


def write_audio_mode(outdir: Path | str, disc_text: str):
    """推导并落 `audio_mode.json`。返回 (路径, {audio_mode, 依据, 待核对})。"""
    info = audio_mode(disc_text, audio_json(outdir))
    target = Path(outdir) / AUDIO_MODE_FILE
    _write_utf8(target, json.dumps(info, ensure_ascii=False, indent=2) + "\n")
    return target, info


def save_audio_mode(outdir: Path | str, value: str, 依据: str = "") -> dict:
    """B3：把界面改选的三态**显式**落到 `audio_mode.json`（唯一写实现，推导仍只走 audio_mode()）。

    和 `write_audio_mode()` 的分工：那个按拆解推导（拆解完成时用），这个照人选的值写。
    值只认 B0② 三个中文词（枚举外直接 ValueError，不静默归一）；`待核对` 归 False——
    人已亲手选过，不该再挂「请核对」。返回 {audio_mode, 依据, 待核对}。
    """
    mode = str(value or "").strip()
    if mode not in AUDIO_MODE_VALUES:
        raise ValueError(f"audio_mode 只认 {'/'.join(AUDIO_MODE_VALUES)}，收到「{mode}」")
    info = {"audio_mode": mode, "依据": str(依据 or "界面改选"), "待核对": False}
    _write_utf8(Path(outdir) / AUDIO_MODE_FILE,
                json.dumps(info, ensure_ascii=False, indent=2) + "\n")
    return info


def build(disc_text: str, checklist_text: str, intent_text: str,
          outdir: Path | str | None = None) -> str:
    """根据九层、清单行索引和用户意图生成规范化三档建议文本。

    `outdir` 给了就同时把台词对照表落成 `<outdir>/台词改写.md`（复用这一次模型调用，
    不再多调一次）；没给就只返回建议文本，行为与以前逐字一致。
    """
    if not intent_text.strip():
        if outdir is not None:
            write_dialogue(outdir, [])
            # AT-17 B2c：空意图也要落 audio_mode 默认值（无建议 ≠ 无三态）
            write_audio_mode(outdir, disc_text)
        return ""
    base_prompt = _read_prompt()
    prompt = (
        base_prompt
        + "\n\n【九层拆解全文】\n"
        + disc_text
        + "\n\n【清单行列表】\n"
        + checklist_prompt(checklist_text)
        + "\n\n【用户意图】\n"
        + intent_text
    )
    api_key = _extract_mod().load_api_key()
    if not api_key:
        raise IntentError(f"缺密钥：设置环境变量 {_extract_mod().KEY_ENV} 或写进 HERMES_HOME/.env")
    raw, _usage, _elapsed = call_model(prompt, api_key, DEFAULT_TIMEOUT)
    if not raw.strip():
        raise IntentError("模型返回空正文，不写入建议文件")
    normalized, invalid_rows, duplicate_rows, invalid_shots = parse_suggestions(
        raw, disc_text, checklist_text
    )
    if invalid_rows:
        print(f"WARN 意图建议：丢弃 {invalid_rows} 个不存在的清单行号", file=sys.stderr)
    if duplicate_rows:
        print(f"WARN 意图建议：丢弃 {duplicate_rows} 个重复清单行号", file=sys.stderr)
    if invalid_shots:
        print(f"WARN 意图建议：删除 {invalid_shots} 个不存在的镜次", file=sys.stderr)
    dialogue_rows, dialogue_stats = parse_dialogue(raw, disc_text)
    if dialogue_stats["fabricated"]:
        print(f"WARN 台词改写：丢弃 {len(dialogue_stats['fabricated'])} 句拆解里没有的台词",
              file=sys.stderr)
    if dialogue_stats["duplicates"]:
        print(f"WARN 台词改写：丢弃 {dialogue_stats['duplicates']} 句重复台词", file=sys.stderr)
    if dialogue_stats["repaired"]:
        print(f"WARN 台词改写：{dialogue_stats['repaired']} 句时间码没照抄拆解，已按拆解纠正",
              file=sys.stderr)
    if dialogue_stats["missing"]:
        print(f"WARN 台词改写：拆解有 {len(dialogue_stats['missing'])} 句台词没进对照表",
              file=sys.stderr)
    if outdir is not None:
        target = Path(outdir) / DIALOGUE_NAME
        try:
            write_dialogue(outdir, dialogue_rows, dialogue_stats.get("mark", ""))
        except OSError as exc:
            print(f"WARN 台词改写：{target} 没写成（{exc}）", file=sys.stderr)
        else:
            if dialogue_rows:
                print(f"[写出] {target}（{len(dialogue_rows)} 句台词）", flush=True)
            else:
                print(f"[台词改写] 本节标记「{dialogue_stats.get('mark', '')}」，不落文件",
                      flush=True)
        # AT-17 B2c：三态默认值同一轮落盘（与建议/台词各写各的文件，互不覆盖）
        try:
            mode_path, mode_info = write_audio_mode(outdir, disc_text)
        except OSError as exc:
            print(f"WARN 音频三态：{Path(outdir) / AUDIO_MODE_FILE} 没写成（{exc}）",
                  file=sys.stderr)
        else:
            print(f"[写出] {mode_path}（{mode_info['audio_mode']}｜{mode_info['依据']}）",
                  flush=True)
    return normalized


def apply_suggestions(checklist_path: Path | str, suggestions_text: str) -> int:
    """按清单行号回填第 7 列「建议」，并清掉本轮没有的旧值。"""
    path = Path(checklist_path)
    text = path.read_text(encoding="utf-8")
    sections = _section_bodies(suggestions_text)
    requested: dict[int, str] = {}
    for section in SECTION_NAMES:
        for raw_line in sections[section]:
            match = ITEM_RE.match(raw_line)
            if match:
                requested.setdefault(int(match.group("row")), section)

    filled = 0
    output = []
    for line in text.splitlines():
        if not line.lstrip().startswith("|"):
            output.append(line)
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 8 or not cells[0].isdigit():
            output.append(line)
            continue
        cells[6] = requested.get(int(cells[0]), "")
        if cells[6]:
            filled += 1
        output.append("| " + " | ".join(cells) + " |")
    _write_utf8(path, "\n".join(output).rstrip("\n") + "\n")
    return filled


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="九层拆解 + 清单行列表 + 用户意图 → 三档资产建议")
    parser.add_argument("disc", help="拆解.md")
    parser.add_argument("checklist", help="待确认清单.md（提供行号、类别和描述）")
    parser.add_argument("intent", help="用户意图.txt")
    parser.add_argument("--out", help="建议文件路径，默认仓库根目录/意图建议.md")
    args = parser.parse_args(argv)
    target = Path(args.out) if args.out else REPO_ROOT / SUGGESTION_NAME
    try:
        _delete_old(target)
        disc = Path(args.disc).read_text(encoding="utf-8")
        checklist = Path(args.checklist).read_text(encoding="utf-8")
        intent_path = Path(args.intent)
        intent = intent_path.read_text(encoding="utf-8") if intent_path.is_file() else ""
        if not intent.strip():
            filled = apply_suggestions(Path(args.checklist), "")
            write_dialogue(target.parent, [])
            # AT-17 B2c：空意图也落三态默认值（删建议文件 ≠ 删 audio_mode）
            mode_path, mode_info = write_audio_mode(target.parent, disc)
            print(f"[意图建议] 意图为空，已删除旧建议文件：{target}；清空建议列 {filled} 行",
                  file=sys.stderr)
            print(f"[音频三态] {mode_path}（{mode_info['audio_mode']}）", file=sys.stderr)
            return 0
        normalized = build(disc, checklist, intent, outdir=target.parent)
        _write_utf8(target, normalized)
        filled = apply_suggestions(Path(args.checklist), normalized)
        print(f"[写出] {target}（{len(normalized.encode('utf-8'))} 字节）；回填建议列 {filled} 行")
    except (OSError, UnicodeError, IntentError) as exc:
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
        print(f"ERROR 意图建议失败：{exc}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
