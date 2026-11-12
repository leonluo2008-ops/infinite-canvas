# -*- coding: utf-8 -*-
"""audio_gate.py — 批准前的音频一致性执法（AT-17 B2b，方案 §4 B2b + §7 附注 13）

门禁只回答一件事：**这条提示词的声音段，跟它声明的音频三态是不是同一件事？**
答否就拒绝「批准」这个动作（generate.do_approve 前置）——**不动已渲染的产物**
（附注 13：gate 失败只拒绝 approve、不自动回滚渲染），产物保持原状，等人改。

纯字符串/结构化判定，零模型调用、零网络（附注 10 同一口径）。

三条判定（口径写死在下面，交付说明逐条对齐）：

  A1 `不要` × BGM 断言
     提示词里出现**未被否定的** BGM / 背景音乐 / 背景乐 / 配乐 断言 → FAIL。
     「否定」只认「无 BGM / 没有背景音乐 / 不要 / 不加 / 仅保留环境声」这类**不存在**的说法；
     「不沿用 / 不重复原片旋律」不算否定——它们是在讲「有音乐、但换掉」，属「不要」模式下的错。

  A2 `按新主体调整` × 旧主体声源沿用（B2a 剥离逻辑的反向校验）
     B2a 在「调整」模式下做了两件事：① 音效点整类不沿用；② BGM 值里的实体核心词
     （`render.entity_noun_core`，取自待确认清单）整串剥掉。反向校验就是：把这两类东西
     拿去**声音段行**（BGM / 音效点 / 背景音乐那几行）里找——
       · 原片音效点原文（去空白后 ≥6 字）逐字出现 → FAIL；
       · 待确认清单实体核心词（≥2 字）出现 → FAIL。
     只看声音段行：其它层（素材/分镜/台词）出现产品名是正常的，不拿它判。

  A3 `沿用原片` × 「无 BGM」声明
     提示词声称无 BGM（「无BGM / 无 BGM / 没有背景音乐 / 无配乐」），但拆解的结构化 BGM
     确实存在（`BGM.*` 四字段任一非空且不是「无BGM」）→ FAIL。
     口径：只看 BGM 四字段，不拿音效点凑数——有音效点、无 BGM 的片子，沿用模式下写
     「BGM：无BGM（原片没有背景音乐）」是**对的**，不算矛盾。

不误伤（老任务）：任务上没有 `audio_mode` 字段、任务目录也没有 `audio_mode.json` →
整项记 WARN「跳过音频执法」，不 FAIL（缺三态不等于三态不符）。拆解读不到时，
依赖拆解的那条记 SKIP/WARN，不阻断批准，但如实写进结果里。
"""
from __future__ import annotations

import hashlib
import importlib.util
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

PASS, FAIL, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"
MODES = ("沿用原片", "按新主体调整", "不要")

# 声音段的识别词：这几行才是「声音」说的话，其它层不动
SOUND_MARKS = ("BGM", "背景音乐", "背景乐", "配乐", "音效点")
# 「无 BGM」的声明（A3 用）
NO_BGM_MARKS = ("无BGM", "无bgm", "没有背景音乐", "无背景音乐", "没有配乐", "无配乐")
# 「不存在」语义的否定词（A1 用）。刻意不含「不沿用 / 不重复」——那是「有音乐但换掉」
NEG_TERMS = ("无BGM", "没有背景音乐", "无背景音乐", "没有配乐", "无配乐", "不要", "不用", "不加",
             "不写", "不设", "不配", "不含", "不需要", "禁止", "去掉", "删掉", "删除", "移除",
             "仅保留", "只保留", "零BGM", "不使用", "无任何")
LABEL_RE = re.compile(r"^\s*(?:[-*+•]|\d+[.、)])?\s*(?:BGM|背景音乐|背景乐|配乐)\s*[：:]\s*")
_SOUND_POINT_MIN = 6                      # 音效点原文逐字比对的最小长度（更短的会误伤）

_SIBLING_MODULES: dict[str, object] = {}


def _sibling(name: str):
    """按路径加载同目录模块（render / extract 都是「按脚本跑」的单文件，不是包）。

    render.py 顶部 `from confirm import ...`，所以加载期间同目录必须在 sys.path 上；
    extract.py 无本地依赖。加载结果缓存：一次进程只导一次（与 render 主链同一份常量）。
    """
    if name in _SIBLING_MODULES:
        return _SIBLING_MODULES[name]
    here = Path(__file__).resolve().parent
    added = str(here) not in sys.path
    if added:
        sys.path.insert(0, str(here))
    try:
        spec = importlib.util.spec_from_file_location(f"wanrepro_reverse_{name}_for_gate",
                                                      here / f"{name}.py")
        if spec is None or spec.loader is None:
            raise ImportError(f"导不进 {here / f'{name}.py'}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        if added and str(here) in sys.path:
            sys.path.remove(str(here))
    _SIBLING_MODULES[name] = module
    return module


# ---------------------------------------------------------------- 文本小工具

def _flat(value: str) -> str:
    """去空白 + 去 markdown 粗体/项目符号：结构比对统一口径（与 render._flat 同法）。"""
    return re.sub(r"\s+", "", (value or "").replace("**", ""))


def _negated(text: str) -> bool:
    flat = _flat(text).lower()
    return any(_flat(term).lower() in flat for term in NEG_TERMS)


def music_assertion(line: str) -> str:
    """该行是否在**断言有音乐** → 命中的那句原文（空串=没有断言）。

    `BGM：…` 这类标签行：剥掉标签后剩正文，正文是否定句式就不算断言。
    句中提 BGM 的行：逐分句看，只要有一个含音乐词且没被否定的分句就算断言。
    """
    text = (line or "").replace("**", "").strip()
    if not text:
        return ""
    match = LABEL_RE.match(text)
    if match:
        body = text[match.end():].strip()
        return "" if (not body or _negated(body)) else text
    if not any(term in text for term in SOUND_MARKS[:4]):     # 只认 BGM/背景音乐/背景乐/配乐
        return ""
    for clause in re.split(r"[，。；、,;：:]", text):
        if any(term in clause for term in SOUND_MARKS[:4]) and not _negated(clause):
            return text
    return ""


def _sentences(text: str) -> list[str]:
    """句子级切分（。！？换行）——流式模板整段是一行，按行判会把全片当成一句。"""
    out = []
    for chunk in re.split(r"[。！？!?\n]", text or ""):
        chunk = chunk.strip()
        if chunk:
            out.append(chunk)
    return out


def music_assertion(text: str) -> str:
    """是否在**断言有音乐** → 命中的那句原文（空串=没有断言）。

    逐句判（流式模板整段一行，不能按行）：
      · 句首是 `BGM：…` 这类标签 → 剥掉标签看正文，否定句式（无 BGM／不要／仅保留环境声）不算断言，
        其余都算——标签本身就是「这一段在讲 BGM」；
      · 句中提 BGM／背景音乐：逐分句看，含音乐词且没被否定的分句就算断言。
    「不沿用 / 不重复原片旋律」**不算**否定：它表述的是「有音乐、但换掉」，那是「不要」模式下的错。
    """
    for sent in _sentences(text):
        sent = sent.replace("**", "").strip()
        if not sent:
            continue
        match = LABEL_RE.match(sent)
        if match:
            body = sent[match.end():].strip()
            if body and not _negated(body):
                return sent
            continue
        if not any(term in sent for term in SOUND_MARKS[:4]):     # 只认 BGM/背景音乐/背景乐/配乐
            continue
        for clause in re.split(r"[，、,;]", sent):
            if any(term in clause for term in SOUND_MARKS[:4]) and not _negated(clause):
                return sent
    return ""


def claims_no_bgm(text: str) -> list[str]:
    """声称「无 BGM」的句子原文（空列表=没有该声明）。"""
    out = []
    for sent in _sentences(text):
        flat = _flat(sent).lower()
        if any(mark.lower() in flat for mark in NO_BGM_MARKS):
            out.append(sent)
    return out


def sound_clauses(text: str) -> list[str]:
    """声音子句：句子里含 BGM/背景音乐/背景乐/配乐/音效点的那几句（台词/素材/分镜其余句不算）。

    流式模板把声音短句嵌在分镜句末，句子级切分后仍是独立一句（`BGM：…` / `音效点：…`），
    所以这里按「句子」而不是「行」取范围——按行取会把整段分镜都算成声音段。
    """
    out = []
    for sent in _sentences(text):
        flat = _flat(sent)
        if any(term in flat for term in SOUND_MARKS):
            out.append(sent)
    return out


def is_sound_line(line: str) -> bool:
    """声音段行（探针/排障用）：BGM 标签行，或含音效点/音乐词的行。"""
    flat = _flat(line)
    return bool(LABEL_RE.match(line.replace("**", "").strip())) or "音效点" in flat \
        or any(term in flat for term in SOUND_MARKS[:4])


def prompt_sha(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 拆解 / 清单

def sound_info(disc_text: str) -> dict | None:
    """拆解正文 → 「八 声音」结构化（与 B1/B2a 同一个 parse_sound_layer）；读不到 → None。"""
    if not (disc_text or "").strip():
        return None
    try:
        render = _sibling("render")
        extract = _sibling("extract")
        layers, _order = render.parse_layers(disc_text)
        body = layers.get("八", "")
        if not body.strip():
            return None
        return extract.parse_sound_layer(body)
    except Exception:                       # noqa: BLE001 —— 判不了就不判，下面如实记 WARN
        return None


def structured_bgm(info: dict) -> list[str]:
    """拆解里**确实有**的 BGM 值（非空且不是「无BGM」）——A3 的唯一依据。"""
    render = _sibling("render")
    no_bgm = getattr(render, "_no_bgm_value", None)
    out = []
    for value in (info.get("bgm") or {}).values():
        text = str(value or "").strip()
        if not text:
            continue
        if (no_bgm(text) if callable(no_bgm) else _flat(text).lower().startswith("无bgm")):
            continue
        out.append(text)
    return out


def subject_nouns(checklist_path: Path | str | None) -> list[tuple[str, str]]:
    """待确认清单 → [(实体核心词, 类别)]：与 B2a 剥离 BGM 值时用的**同一个**函数取词。"""
    path = Path(checklist_path) if checklist_path else None
    if path is None or not path.is_file():
        return []
    try:
        render = _sibling("render")
        out, seen = [], set()
        for ent in render.parse_checklist(path):
            noun = render.entity_noun_core(ent)
            if len(noun) < 2 or noun in seen:
                continue
            seen.add(noun)
            out.append((noun, str(getattr(ent, "kind", ""))))
        return out
    except Exception:                       # noqa: BLE001
        return []


# ---------------------------------------------------------------- 主判定

def evaluate(prompt: str, audio_mode: str, *, disc_text: str = "",
             checklist_path: Path | str | None = None) -> dict:
    """三态 × 提示词声音段一致性判定 → 门禁结果（纯函数：不写盘、不调模型、不联网）。

    返回 {verdict, audio_mode, checks[{id,verdict,msg}], 发现[], 处理, at,
          prompt_sha256, 口径}。verdict ∈ PASS / FAIL / WARN / SKIP。
    """
    mode = (audio_mode or "").strip()
    checks: list[dict] = []

    def add(cid: str, verdict: str, msg: str) -> None:
        checks.append({"id": cid, "verdict": verdict, "msg": msg})

    lines = [ln for ln in (prompt or "").splitlines() if ln.strip()]
    if not lines:
        add("A9 提示词", WARN, "提示词为空（还没渲染 / 被清掉）：音频门禁无内容可判，跳过")
        return _result(WARN, mode, checks, {}, prompt)
    if mode not in MODES:
        add("A0 音频三态", WARN,
            "任务没有 audio_mode（老任务/三态未落盘）：跳过音频执法，不阻断批准")
        return _result(WARN, mode, checks, {}, prompt)

    info = sound_info(disc_text) if mode in ("按新主体调整", "沿用原片") else None

    if mode == "不要":
        hit = music_assertion(prompt)
        if hit:
            add("A1 不要×BGM 断言", FAIL,
                f"三态=不要，但提示词仍在描述 BGM／背景音乐：「{_quote(hit)}」")
        else:
            add("A1 不要×BGM 断言", PASS,
                "三态=不要：提示词没有 BGM 描述（只有「无 BGM／仅保留环境声」这类否定句式）")

    elif mode == "按新主体调整":
        if info is None:
            add("A2 调整×旧声源沿用", WARN,
                "拆解「八 声音」读不到（无拆解.md / 无八层）：旧主体声源是否沿用判不了，跳过")
        else:
            sound_text = _flat("。".join(sound_clauses(prompt)))
            leaked_points = [str(p).strip() for p in (info.get("sound_points") or [])
                             if len(_flat(str(p))) >= _SOUND_POINT_MIN
                             and _flat(str(p)) in sound_text]
            leaked_nouns = [noun for noun, _kind in subject_nouns(checklist_path)
                            if noun and noun in sound_text]
            if leaked_points or leaked_nouns:
                bits = []
                if leaked_points:
                    bits.append(f"原片音效点原文「{_quote(leaked_points[0])}」")
                if leaked_nouns:
                    bits.append(f"旧主体声源名词「{'、'.join(leaked_nouns[:3])}」")
                add("A2 调整×旧声源沿用", FAIL,
                    f"三态=按新主体调整，但提示词声音段沿用了{'；'.join(bits)}"
                    f"（调整模式要求剥离旧主体声源、音效点整类不沿用）")
            else:
                add("A2 调整×旧声源沿用", PASS,
                    "三态=按新主体调整：声音段没有原片音效点原文，也没有待确认清单里的实体名词")

    else:                                                # 沿用原片
        claims = claims_no_bgm(prompt)
        if info is None:
            add("A3 沿用×无BGM 声明", WARN,
                "拆解「八 声音」读不到（无拆解.md / 无八层）：拆解有没有 BGM 判不了，跳过")
        else:
            bgm = structured_bgm(info)
            if claims and bgm:
                add("A3 沿用×无BGM 声明", FAIL,
                    f"三态=沿用原片，但提示词声称无 BGM（「{_quote(claims[0])}」），"
                    f"而拆解的 BGM 字段有值（{'；'.join(bgm[:2])}）")
            elif claims:
                add("A3 沿用×无BGM 声明", PASS,
                    "三态=沿用原片：提示词声称无 BGM 与拆解一致（拆解 BGM 四字段为「无BGM」）")
            else:
                add("A3 沿用×无BGM 声明", PASS,
                    "三态=沿用原片：提示词没有「无 BGM」声明（沿用模式原样保留原片 BGM）")

    verdict = FAIL if any(c["verdict"] == FAIL for c in checks) else (
        WARN if any(c["verdict"] in (WARN, SKIP) for c in checks) else PASS)
    return _result(verdict, mode, checks, info or {}, prompt)


def _quote(text: str, limit: int = 48) -> str:
    flat = re.sub(r"\s+", " ", (text or "").replace("**", "")).strip()
    return flat if len(flat) <= limit else flat[:limit] + "…"


def _result(verdict: str, mode: str, checks: list[dict], info: dict, prompt: str) -> dict:
    fails = [c["msg"] for c in checks if c["verdict"] == FAIL]
    notes = [c["msg"] for c in checks if c["verdict"] in (WARN, SKIP)]
    return {
        "verdict": verdict,
        "audio_mode": mode,
        "checks": checks,
        "发现": fails or notes,
        "处理": _advice(mode, bool(fails)),
        "at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "prompt_sha256": prompt_sha(prompt),
        "口径": "A1 不要×BGM 断言 / A2 调整×旧声源（B2a 剥离反向校验）/ A3 沿用×无BGM 声明；"
                "缺三态跳过；纯字符串，零模型调用",
    }


def _advice(mode: str, failed: bool) -> str:
    if not failed:
        return "" if mode else "任务没有音频三态：老任务跳过执法，如需执法先重跑拆解落三态"
    return ("门禁只挡「批准」，已渲染的产物保持原状（不自动回滚）——"
            "按新三态重跑渲染（render）让声音段与三态一致，再批准；或改回与提示词一致的三态。")
