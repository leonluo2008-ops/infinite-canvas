# -*- coding: utf-8 -*-
"""AT-8 B2 视觉包——`待确认清单.md`（+ 出现时段侧车）→ `asset_cards[]`（数据合同 v3）。

真源分工（合同 v3）：
  · 行/类别/模型看到什么/命名/资产图/建议 ← 清单 **md**（人工改过的列以 md 为准）
  · 出现时段/同实体疑似 ← 侧车 `待确认清单.json`（B1 产出，schema=at8-asset-spans-v1）
  · 已传图 ← 清单「资产图」列（**不是** pending_replace：那是本次会话上传流水，回显不依赖它）
  · 帧 ← `automation/frames.py`（时段中点 → ±0.5s 缓存 → 现抽 → 中段/首帧兜底）

短名清洗（审计「字段缺：短名清洗规则」）：
  九 资产 / 二 产品 / 十 图文的条目名（服装A、道具B、核心产品、包装字A…）按描述同源匹配
  （复用 confirm.py 的 span_overlap/first_clause，不另造一套相似度），拼成「类别+字母」形态；
  同实体多行（一件东西既登记成主体又登记成道具）在短名后加对方类别括注，两张卡一眼分得开。
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
from pathlib import Path

from . import frames, replace

ROOT = Path(__file__).resolve().parents[1]
ANALYSIS = ROOT / "analysis" / "reverse"
SPANS_NAME = "待确认清单.json"
SUMMARY_NAME = "拆解.md"
SPANS_SCHEMA = "at8-asset-spans-v1"

SCENE_CATEGORY = "场景"
MAIN_CATEGORIES = ("主体", "品牌视觉")     # 前缀命中（含「品牌视觉（置换位）」）；是否核心还看建议，见 is_core
CORE_ADVICE = ("必须换", "建议准备")
MATCH_MIN = 0.40                           # 条目名匹配闸门（同源描述才认，只沾边的不要）

_CONFIRM: list = []


def _confirm():
    """复用 pipeline/reverse/confirm.py 的解析件（它不是包，按文件加载；模块无 import 副作用）。"""
    if not _CONFIRM:
        path = ROOT / "pipeline" / "reverse" / "confirm.py"
        spec = importlib.util.spec_from_file_location("wanrepro_reverse_confirm", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _CONFIRM.append(module)
    return _CONFIRM[0]


# ---------------------------------------------------------------- 侧车 / md

def load_spans(bridge_id: str) -> tuple[dict[int, dict], str]:
    """出现时段侧车 → ({n: {出现时段, 同实体疑似, 来源}}, 读不出时的说明)。

    没有侧车（B1 之前拆的旧任务）→ ({}, "")，由调用方标「无侧车（旧任务）」；
    侧车坏了不静默：说明带回，卡上标「侧车读不出（已走中段兜底）」。
    """
    path = ANALYSIS / bridge_id / SPANS_NAME
    if not path.is_file():
        return {}, ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        return {}, f"侧车读不出（已走中段兜底）：{exc}"
    if not isinstance(payload, dict):
        return {}, "侧车格式不对（已走中段兜底）：顶层不是对象"
    note = "" if payload.get("schema") == SPANS_SCHEMA else f"侧车 schema={payload.get('schema')!r}（按原样读）"
    out: dict[int, dict] = {}
    for row in payload.get("rows") or []:
        if not isinstance(row, dict):
            continue
        try:
            n = int(row.get("n"))
        except (TypeError, ValueError):
            continue
        pos = row.get("画面位置")
        out[n] = {
            "出现时段": [span for span in (row.get("出现时段") or []) if isinstance(span, dict)],
            # AT-9 B1 读侧透传：{"格":1-9,"镜次":N}，旧 sidecar / 缺值 → None（消费端标「未能标注」）
            "画面位置": pos if isinstance(pos, dict) else None,
            "同实体疑似": [int(x) for x in (row.get("同实体疑似") or [])],
            "来源": str(row.get("来源") or ""),
        }
    return out, note


def split_urls(cell: str) -> list[str]:
    """「资产图」列 → 列表（多图用英文逗号；人手填的中文逗号/分号也容；不按空格切，路径里可能有空格）。"""
    parts = re.split(r"[,，;；]+", str(cell or "").strip())
    return [p for p in (part.strip() for part in parts) if p]


# ---------------------------------------------------------------- 短名

def _clean_label(label: str) -> str:
    """条目名去掉括注：`包装字A（出现于：00:06-00:08）` → `包装字A`。"""
    name = re.sub(r"[（(]\s*出现于[:：]?[^）)]*[）)]", "", str(label or ""))
    name = re.sub(r"[（(]\s*\d{1,2}:\d{2}[^）)]*[）)]", "", name)
    return name.strip(" ：:*-｜|（()）")


def _candidates(layers: dict) -> list[dict]:
    """候选条目名：二 产品（主体）/ 九 资产（人物·服装·道具·场景）/ 十 图文（品牌视觉）。"""
    cf = _confirm()
    out: list[dict] = []
    for label, text in cf.bullets(layers.get("二 产品", "")):
        if "产品" in label or "商品" in label:
            out.append({"类别": "主体", "名字": _clean_label(label), "正文": text})
    for entry in cf.asset_entries(layers.get("九 资产", "")):
        out.append({"类别": entry["类别"], "名字": entry["名字"], "正文": entry["正文"]})
    for label, text in cf.bullets(layers.get("十 图文与收束", "")):
        name = _clean_label(label)
        if name and name not in ("收束",):
            out.append({"类别": "品牌视觉", "名字": name, "正文": text})
    return out


def _score(candidate: dict, text: str) -> float:
    cf = _confirm()
    body = f"{candidate['名字']} {candidate['正文']}"
    return (cf.span_overlap(body, text)
            + cf.span_overlap(cf.first_clause(body), cf.first_clause(text))) / 2


def _base_short_name(category: str, text: str, candidates: list[dict]) -> str:
    key = "品牌视觉" if str(category).startswith("品牌视觉") else category
    pool = [c for c in candidates if c["类别"] == key]
    scored = sorted(((_score(c, text), c["名字"]) for c in pool), key=lambda x: -x[0])
    matched = [name for score, name in scored if score >= MATCH_MIN and name]
    if key == "品牌视觉" and matched:
        # 品牌位一行多段（包装字A/B/C）：同前缀的收成「包装字A/B/C」
        if len(matched) > 1:
            stem = os.path.commonprefix(sorted(matched))
            if stem and all(1 <= len(name) - len(stem) <= 3 for name in matched):
                return stem + "/".join(name[len(stem):] for name in sorted(matched, key=lambda x: x[len(stem):]))
        return matched[0] if len(matched) == 1 else "、".join(sorted(matched))
    if matched:
        name = matched[0]
        if key == "主体" or not name or name == category or name.startswith(str(category)):
            return name or str(category)
        return f"{category}{name}"
    # 匹配不到：描述自带 `A：` 前缀时用「类别+字母」兜底，否则只有类别
    letter = re.match(r"^([A-Za-zＡ-Ｚ])[：:]", str(text or "").strip())
    return f"{category}{letter.group(1)}" if letter else str(category)


# ---------------------------------------------------------------- 短名映射表（AT-9 B2）
# 人话短名映射表初版（合同：人工维护、不加模型调用）：
#   · 核心产品 → 本品主体（所有者口径：产品在正文/卡片上只叫「本品」）
#   · 道具C 等  → 按实体名（九 资产 条目名，A/B/C 字母档位保留）
#   · 人物     → 按描述特征（波波头短发 / 马尾卷发…，同一人物两张登记一眼是同一个人）
#   · 全不中   → 退路「几何称呼+行号」（画面位置九宫格方位 + 清单行号）
# 旧『对方类别括注』逻辑（`核心产品（道具）`）废弃：跨类别同实体改用联名「·同件对方短名」，
# 不再产出任何括注；同类别疑似（#3↔#5 两人物）不加任何标注，靠特征词区分。

CORE_PRODUCT_SHORT = "本品主体"
CROSS_TWIN_MARK = "·同件{}"
GRID_NAMES = {1: "左上", 2: "上方", 3: "右上", 4: "左侧", 5: "中央",
              6: "右侧", 7: "左下", 8: "下方", 9: "右下"}
PERSON_FEATURES = ("波波头", "短发", "马尾", "卷发", "长发", "直发", "男性", "女性",
                   "青年", "中年", "胡茬", "光头")


def _is_core_product(category: str, text: str, candidates: list[dict]) -> bool:
    """主体行是否「核心产品」：匹配到的实体名带「核心」（如条目名就叫核心产品）。"""
    pool = [c for c in candidates if c["类别"] == "主体"]
    scored = sorted(((_score(c, text), c["名字"]) for c in pool), key=lambda x: -x[0])
    matched = [name for score, name in scored if score >= MATCH_MIN and name]
    return bool(matched and "核心" in matched[0])


def _person_short_name(text: str) -> str:
    """人物 → 按描述特征：描述里按出现顺序取前两个特征词拼「XX人物」。"""
    feats = [w for w in PERSON_FEATURES if w in str(text or "")][:2]
    return ("".join(feats) + "人物") if feats else "人物"


def _grid_fallback(no: int, pos) -> str:
    """退路：几何称呼+行号。画面位置（九宫格 1-9）在场 → 「左侧位·行12」；不在 → 「行12」。"""
    grid = pos.get("格") if isinstance(pos, dict) else None
    name = GRID_NAMES.get(grid) if isinstance(grid, int) else None
    return f"{name}位·行{no}" if name else f"行{no}"


def short_names(rows: list[dict], layers: dict, twins: dict[int, list[int]],
                positions: dict[int, dict] | None = None) -> dict[int, str]:
    """{清单行号: 人话短名}（AT-9 B2 映射表初版，无括注）。

    跨类别同实体（疑似面，只做展示提示；写联动只认确认组）联名「·同件对方短名」，
    两张卡一眼分得开——at8 的 #2/#7（主体↔道具）各带对方称呼。
    """
    candidates = _candidates(layers)
    positions = positions or {}
    cat_of = {row["no"]: row.get("cat") or "" for row in rows}
    out: dict[int, str] = {}
    for row in rows:
        no, cat, text = row["no"], row.get("cat") or "", row.get("desc") or ""
        if cat == "主体":
            base = CORE_PRODUCT_SHORT if _is_core_product(cat, text, candidates) \
                else _base_short_name(cat, text, candidates)
        elif cat == "人物":
            base = _person_short_name(text)
        else:
            base = _base_short_name(cat, text, candidates)
        if base == cat:                      # 映射全不中（裸类别）→ 退路：几何称呼+行号
            base = _grid_fallback(no, positions.get(no))
        out[no] = base
    base_only = dict(out)                    # 联名前快照：联名只引对方短名，不嵌对方的联名
    for row in rows:                         # 跨类别同实体联名（非括注形态）
        no = row["no"]
        marks = [base_only.get(t) or f"行{t}" for t in sorted({int(x) for x in (twins.get(no) or [])})
                 if cat_of.get(t) and cat_of.get(t) != cat_of[no]]
        if marks:
            out[no] = out[no] + "".join(CROSS_TWIN_MARK.format(m) for m in marks)
    return out


# ---------------------------------------------------------------- 核心过滤

def is_core(category: str, advice: str) -> bool:
    """主列表 = 类别非场景 且（建议∈{必须换,建议准备} 或（类别∈{主体,品牌视觉} 且 建议非「可沿用」且非空））。

    场景类永不进主列表（所有者裁定①：环境类排除）——建议写了「必须换」也一样。
    主体/品牌视觉若建议=「可沿用」（参考片自己的设备，员工无需操作）或建议留空 → 归次要，
    不再按类别恒为核心（2026-11-09 所有者口径：主列表只放要动手的行）。
    """
    cat, tip = str(category or ""), str(advice or "")
    if cat == SCENE_CATEGORY:
        return False
    if tip in CORE_ADVICE:
        return True
    return (cat in MAIN_CATEGORIES or cat.startswith("品牌视觉")) and tip and tip != "可沿用"


# ---------------------------------------------------------------- 组装

def _layers(bridge_id: str) -> dict:
    path = ANALYSIS / bridge_id / SUMMARY_NAME
    if not path.is_file():
        return {}
    try:
        return _confirm().split_layers(path.read_text(encoding="utf-8-sig", errors="replace"))
    except OSError:
        return {}


def build(bridge_id: str, local_id: str) -> list[dict]:
    """清单 + 侧车 + 帧 → asset_cards[]（按 n 升序；清单不存在 = 还没拆解，返回 []）。"""
    if not bridge_id:
        return []
    try:
        md_path = replace.checklist_path(bridge_id)
    except replace.ReplaceError:
        return []
    rows = replace.parse_rows(md_path.read_text(encoding="utf-8-sig", errors="replace"))
    if not rows:
        return []
    spans, spans_note = load_spans(bridge_id)
    layers = _layers(bridge_id)
    twins = {no: (payload.get("同实体疑似") or []) for no, payload in spans.items()}
    positions = {no: payload.get("画面位置") for no, payload in spans.items()}
    # AT-9 B2 确认组（sidecar 顶层『同实体确认』，写端点生产；疑似≠确认，疑似不联动）
    groups = replace.load_groups(bridge_id)
    names = short_names(rows, layers, twins, positions) if layers else {}
    cache = frames.load_cache(bridge_id)
    cards: list[dict] = []
    for row in sorted(rows, key=lambda r: r["no"]):
        no = row["no"]
        payload = spans.get(no) or {}
        appear = payload.get("出现时段") or []
        spans_now = [{"起": str(s.get("起") or ""), "止": str(s.get("止") or "")} for s in appear]
        shots = [frames.build_frame(bridge_id, local_id, no, span, cache)
                 for span in (spans_now or [None])]
        located = "精确"
        if any(frame.get("定位") == "未定位" for frame in shots):
            located = "未定位"
        elif any(frame.get("定位") == "未精确定位" for frame in shots):
            located = "未精确定位"
        cards.append({
            "n": no,
            "类别": row.get("cat") or "",
            "短名": names.get(no) or (row.get("cat") or f"#{no}"),
            "建议": row.get("advice") or "",
            "核心": is_core(row.get("cat") or "", row.get("advice") or ""),
            "出现时段": spans_now,
            "帧": shots,
            "已传图": split_urls(row.get("cur_assets") or ""),
            "描述": row.get("desc") or "",
            "画面位置": payload.get("画面位置"),
            "同实体疑似": payload.get("同实体疑似") or [],
            # AT-9 B2 确认组：该行所属组（升序）；无组 = []（合同：空数组行为同现状）
            "同实体确认": next((list(g) for g in groups if no in g), []),
            # 定位状态（合同 v3 审计「字段缺：定位状态」）：精确 / 未精确定位 / 未定位
            "定位": located,
            "时段来源": payload.get("来源") or spans_note or ("无侧车（旧任务）" if not spans else ""),
        })
    return cards
