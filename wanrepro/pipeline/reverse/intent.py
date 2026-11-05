#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""根据九层拆解、清单行号和用户意图生成三档资产建议。

这是第二次、纯文本模型调用：不上传视频。调用方式复用 extract.py 的密钥读取、
直连 opener、响应解析和空响应重试策略。
"""

from __future__ import annotations

import argparse
import importlib.util
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
    """拆解「八 声音」里写的语种（`人声语言：西班牙语（…）` → 西班牙语）；没有就空。"""
    probe = dialogue_layer(disc_text).replace("**", "").replace("`", "")
    match = re.search(r"人声语言\s*[：:]\s*([^\s（(（；;，,。]+)", probe)
    return match.group(1).strip() if match else ""


def dialogue_lines(disc_text: str) -> list[tuple[str, str]]:
    """拆解「八 声音」里逐句台词：[(归一时间码, 原文)]，只认「时间码 + 引号」的行。"""
    found: list[tuple[str, str]] = []
    for raw in dialogue_layer(disc_text).splitlines():
        code = TIME_CODE_RE.search(raw)
        if not code:
            continue
        quoted = re.search(r"[\"“「『](.+?)[\"”」』]", raw[code.end():])
        if not quoted:
            continue
        text = quoted.group(1).strip()
        if not text:
            continue
        found.append((_norm_time(code.group(0)), text))
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
    返回 (行列表, 统计)；行字段 `start_end` / `original` / `chinese`。
    """
    stats = {
        "total": 0, "dropped": 0, "duplicates": 0, "repaired": 0,
        "fabricated": [], "missing": [], "mark": "",
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

    known = dialogue_lines(disc_text)
    known_by_text = {_norm_ws(text): code for code, text in known}
    order = {_norm_ws(text): index for index, (_code, text) in enumerate(known)}
    rows: list[dict] = []
    seen: set[str] = set()

    for raw_line in section:
        cells = _table_cells(raw_line)
        if not cells or len(cells) < 3:
            continue
        if any(key in "".join(cells[:1] + cells[2:3]) for key in ("原文", "中文", "起-止")):
            continue                      # 表头
        code_match = TIME_CODE_RE.search(cells[0])
        original = cells[1].strip().strip(QUOTE_CHARS).strip()
        chinese = cells[2].strip()
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
        rows.append({"start_end": start_end, "original": original, "chinese": chinese})

    rows.sort(key=lambda row: order.get(_norm_ws(row["original"]), 999))
    stats["missing"] = [text for _code, text in known if _norm_ws(text) not in seen]
    stats["total"] = len(rows)
    return rows, stats


def dialogue_markdown(rows: list[dict], mark: str = "") -> str:
    """`台词改写.md` 的规范形态：三列表格；没有台词时写一行标记。"""
    lines = ["# 台词改写", ""]
    if not rows:
        lines.append(mark or NO_DIALOGUE_MARK)
        return "\n".join(lines) + "\n"
    lines += ["| 起-止 | 原文 | 中文 |", "|---|---|---|"]
    for row in rows:
        lines.append(f"| {row['start_end']} | {row['original']} | {row['chinese']} |")
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


def build(disc_text: str, checklist_text: str, intent_text: str,
          outdir: Path | str | None = None) -> str:
    """根据九层、清单行索引和用户意图生成规范化三档建议文本。

    `outdir` 给了就同时把台词对照表落成 `<outdir>/台词改写.md`（复用这一次模型调用，
    不再多调一次）；没给就只返回建议文本，行为与以前逐字一致。
    """
    if not intent_text.strip():
        if outdir is not None:
            write_dialogue(outdir, [])
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
            print(f"[意图建议] 意图为空，已删除旧建议文件：{target}；清空建议列 {filled} 行", file=sys.stderr)
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
