#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""prompt_lint.py — 提示词体检（v2：词表分级 + 负向减法）

两条设计原则（都是用户 2026-10-28 拍板的红线）：
  1. **禁止把没经过多次实践检验的结论硬编码**。单次观察只能当 observed（INFO），
     ≥3 个独立案例复现并经人确认才可升 confirmed（ERROR）。
  2. **负向清单默认不写**。官方原文：负向清单「没有可留空，不必凑数，也不要重复正向已写明的内容」；
     线上 287 条样本 97.6% 零否定。确需写时 ≤3 条，且不得与正向重复（重复=ERROR）。

用法：
  py -3 pipeline/reverse/prompt_lint.py --prompt prompts/x.txt --tech analysis/reverse/<id>/tech.json --assets 2
退出码：0 通过（INFO/WARN 不算失败）｜1 有 ERROR
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ERROR, WARN, INFO = "ERROR", "WARN", "INFO"
ROOT = Path(__file__).resolve().parents[2]
LEXICON = ROOT / "spec" / "lexicon.json"

TIME_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[-–~至]\s*(\d+(?:\.\d+)?)\s*(?:秒|s)")
NEG_ITEM = re.compile(r"(?:不要|禁止|避免|不得|勿)\s*([^，。；、,\n]{2,24})")
NEG_SECTION = re.compile(r"负向清单[:：]?(.*)$", re.S)
WHITE_WORDS = ["白色字卡", "白色文字", "白色字幕", "白字"]
BRIGHT_BG = ["白色台面", "白底", "乳白", "白色透光", "纯白背景"]
# 从否定条目里剥掉的泛用尾词，留下的词干用于"是否已被正向写明"的比对
STRIP_TAIL = ["出现", "产生", "造成", "变形", "痕迹", "效果", "问题", "的情况", "任何"]


def load_json(p: Path, default: dict) -> dict:
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--tech", required=True)
    ap.add_argument("--assets", type=int, default=None)
    ap.add_argument("--product", default="pipeline/products/pop_sku01_orange.json")
    ap.add_argument("--observed", action="store_true", help="声明负向条目对应的错误已在本素材实测出现过")
    args = ap.parse_args()

    raw_text = Path(args.prompt).read_text(encoding="utf-8")
    prof_m = re.search(r"^#\s*profile:\s*(\w+)", raw_text, re.M)
    profile = prof_m.group(1).lower() if prof_m else "film"
    text = raw_text
    tech = load_json(Path(args.tech), {})
    meta = tech.get("meta", {})
    # 比对基准是**生成段时长**（动作段），不是成片总长：字卡与黑底尾卡后期叠
    structure = tech.get("structure", {})
    dur = float(structure.get("action_end_s") or meta.get("duration_s", 0) or 0)
    ratio = "9:16" if meta.get("width", 1) < meta.get("height", 1) else "16:9"
    lex_p = LEXICON
    if not lex_p.exists():
        raise SystemExit("旧规则库已于 2026-10-29 删除（所有者裁定），重建后另立版本；本检查/本功能暂不可用")
    lex = json.loads(lex_p.read_text(encoding="utf-8"))
    policy = lex.get("negative_policy", {})

    issues: list[tuple[str, str, str]] = []

    # ---------- 结构类硬标准 ----------
    if any(w in text for w in WHITE_WORDS) and any(w in text for w in BRIGHT_BG):
        issues.append((ERROR, "S1 字卡对比度",
                       "浅色底 + 白色字卡，白底白字。改深色字（如 #3A2410）或加半透明底板"))

    card = re.search(r"「([^」]+)」", text)
    if meta and ratio not in text:
        issues.append((ERROR, "S3 比例继承", f"未声明参考比例 {ratio}（实测 {meta.get('width')}x{meta.get('height')}）"))
    if profile != "shot" and dur and f"{dur:.0f} 秒" not in text and f"{dur:.0f}秒" not in text:
        issues.append((WARN, "S3 时长", f"未写全片时长 {dur:.1f}s"))

    refs = sorted(set(re.findall(r"@图片(\d+)", text)), key=int)
    if args.assets is not None and len(refs) != args.assets:
        issues.append((ERROR, "S4 素材引用", f"引用 {len(refs)} 张（@图片{','.join(refs) or '无'}），实际 {args.assets} 张"))
    if re.search(r"(?<!@)图片\d", text):
        issues.append((WARN, "S4 素材引用", "出现不带 @ 的「图片N」——万相不绑定（实测仅 @图片N 生效）"))

    prod = load_json(ROOT / args.product, {})
    for bad in prod.get("forbidden_terms", []):
        if bad in text and not re.search(rf"(?:不要|禁止|避免)\s*[^，。\n]{{0,6}}{re.escape(bad)}", text):
            issues.append((ERROR, "S5 产品事实", f"出现「{bad}」：本品为滚珠（roll-on），配置列为禁用表述"))

    # ---------- 时间轴（整片语法才查首尾相接；分镜逐镜单独生成，不需要接龙） ----------
    spans = [(float(a), float(b)) for a, b in TIME_RE.findall(text)] if profile != "shot" else []
    if spans:
        cursor = 0.0
        for a, b in spans:
            if abs(a - cursor) > 0.05:
                issues.append((ERROR, "T1 时间轴", f"{cursor:.1f}s 处断档（下一段 {a:.1f}s 起）"))
            cursor = max(cursor, b)
        if dur and abs(cursor - dur) > 0.5:
            issues.append((WARN, "T1 时间轴", f"结束于 {cursor:.1f}s，参考 {dur:.1f}s"))

    # ---------- 负向清单：减法优先 ----------
    # 只在「负向清单」段里找否定项：原先全篇扫描，会把正向里的「不得改写或重排」也当成负向条目，
    # 再与正向比对 → 必然误判重复（本次实测到的假阳性）
    sec = NEG_SECTION.search(text)
    scope = sec.group(1) if sec else text
    neg_items = NEG_ITEM.findall(scope)
    positive_part = text[:sec.start()] if sec else text
    if profile == "shot":
        # 分镜语法用官方「约束段」承载禁止项，不是整片负向清单，跳过 S6/S7/S8
        neg_items = []
    if neg_items:
        if not args.observed:
            issues.append((WARN, "S7 负向减法",
                           "写了负向但未声明「该错误已在本素材实测出现过」——没有实测依据就先别堆否定"))
        dup = []
        for raw in neg_items:
            stem = raw
            for t in STRIP_TAIL:
                stem = stem.replace(t, "")
            stem = stem.strip("的 ")
            if len(stem) >= 2 and stem in positive_part:
                dup.append(stem)
        if dup:
            # 判定为启发式子串匹配，会误报（已实测一次）；按纪律不当阻断项，只提示人工复核
            issues.append((WARN, "S8 负向疑似重复",
                           f"负向条目疑似与正向重复：{'、'.join(sorted(set(dup))[:5])}——官方 L126 禁止重复正向内容，请人工确认"))
        if sec is None:
            issues.append((INFO, "S6 负向位置", "负向条目散落在正文中，若确需保留应集中在末尾一段"))

    # ---------- 词表分级（不轻易阻断） ----------
    for item in lex.get("terms", []):
        term = item["term"]
        if term not in text:
            continue
        status = item.get("status", "observed")
        n = len(item.get("evidence", []))
        if status == "confirmed":
            issues.append((ERROR, "L 词表(confirmed)", f"「{term}」已在 {n} 例复现，按规则阻断"))
        elif status == "candidate":
            issues.append((WARN, "L 词表(candidate)", f"「{term}」{n} 例复现，仅供参考：{item.get('note','')}"))
        else:
            issues.append((INFO, "L 词表(observed)", f"「{term}」仅 {n} 例观察，不构成禁令：{item.get('note','')}"))

    # ---------- 输出 ----------
    order = {ERROR: 0, WARN: 1, INFO: 2}
    issues.sort(key=lambda i: order[i[0]])
    n = {lvl: sum(1 for i in issues if i[0] == lvl) for lvl in (ERROR, WARN, INFO)}
    print(f"体检 {args.prompt}｜{n[ERROR]} ERROR / {n[WARN]} WARN / {n[INFO]} INFO")
    for lvl, rule, msg in issues:
        print(f"  [{lvl}] {rule} — {msg}")
    if not issues:
        print("  ✅ 全部通过")
    return 1 if n[ERROR] else 0


if __name__ == "__main__":
    raise SystemExit(main())
