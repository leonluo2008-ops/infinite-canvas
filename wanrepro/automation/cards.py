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

def _summary_markdown(bridge_id: str) -> str:
    """拆解.md 原文（读不出 → 空串）。代表帧要片长与分镜表，和层解析共用同一次读。"""
    path = ANALYSIS / bridge_id / SUMMARY_NAME
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return ""


def _split(markdown: str) -> dict:
    if not str(markdown or "").strip():
        return {}
    try:
        return _confirm().split_layers(markdown)
    except Exception:
        return {}


def _layers(bridge_id: str) -> dict:
    return _split(_summary_markdown(bridge_id))


# ---------------------------------------------------------------- 分镜表时段（AT-15 S1）

def _relaxed_bounds(shots_layer: str, total: float) -> dict[int, tuple[float, float]]:
    """分镜表宽松复算：严格闸门（confirm 消歧 v2）整表被拒时的第二路（AT-15 S1 止血）。

    严格闸门要求「末段右端 ≈ 片长（±0.5s）」——片尾带品牌定格/尾卡的片（pop-1：分镜表止
    00:05、片长 6.04s）会因此整表被打回，代表帧全落中段兜底（现场 auto-0036 八卡同帧）。
    宽松路只做四件事：读得出 ∧ 段内有序 ∧ 按镜号单调 ∧ 单镜不越界；读法固定 mm:ss
    （frames.clock_seconds 的文档口径，与拆解.md 分镜表同一套）；单镜越界即丢该镜、不丢整表，
    整表不足两镜则不认（单镜表无法自证）。
    """
    rows = _confirm()._clock_span_rows(shots_layer)   # 复用 confirm 的表行解析，不另造一套
    raw: dict[int, tuple[float, float]] = {}
    for shot, start_token, end_token in rows:
        start, end = frames.clock_seconds(start_token), frames.clock_seconds(end_token)
        if start is None or end is None or end <= start or start < 0:
            continue
        if total > 0 and end > total + 0.5:            # 越界镜：这个读法不可信，丢镜不丢表
            continue
        raw[int(shot)] = (start, end)
    if len(raw) < 2:
        return {}
    ordered = sorted(raw.items())
    for (_no1, (_start1, end1)), (_no2, (start2, _end2)) in zip(ordered, ordered[1:]):
        if start2 < end1 - 0.01:                      # 段间不单调 → 整表不认
            return {}
    return raw


def shot_bounds(shots_layer: str, total: float | None) -> dict[int, tuple[float, float]]:
    """七 分镜表 → {镜号: (起秒, 止秒)}：严格闸门优先，整表被拒才走宽松复算（AT-15 S1）。

    严格路 = confirm.parse_shot_times（歧义 token 全表联判）；它的结果非空就原样用，
    宽松路只在「一条都读不出」时兜底，老片（pop-4 等）行为零变化。
    """
    strict = _confirm().parse_shot_times(shots_layer, total or None) or {}
    out: dict[int, tuple[float, float]] = {}
    for shot, pair in strict.items():
        try:
            start, end = frames.clock_seconds(pair[0]), frames.clock_seconds(pair[1])
            number = int(shot)
        except (TypeError, ValueError, IndexError):
            continue
        if start is None or end is None or end <= start:
            continue
        out[number] = (start, end)
    return out or _relaxed_bounds(shots_layer, total or 0.0)


# ---------------------------------------------------------------- 意图建议（AT-10 C1）

INTENT_NAME = "意图建议.md"

_INTENT: list = []


def _intent():
    """复用 pipeline/reverse/intent.py 的建议解析件（它不是包，按文件加载；顶层无网络调用）。"""
    if not _INTENT:
        path = ROOT / "pipeline" / "reverse" / "intent.py"
        spec = importlib.util.spec_from_file_location("wanrepro_reverse_intent", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _INTENT.append(module)
    return _INTENT[0]


def load_suggested_shots(bridge_id: str) -> dict[int, list[int]]:
    """`意图建议.md` → {清单行号: [镜次…]}（同 confirm 的意图建议读写口径）。

    只读建议文本，不调模型。三节全读（必须换 → 建议准备 → 可沿用），行号按 ITEM_RE 取，
    镜次复用 confirm.shot_numbers（`镜 1、3` / `镜 1-3` 都能展开）。无文件/无匹配 → {}。
    """
    if not bridge_id:
        return {}
    path = ANALYSIS / bridge_id / INTENT_NAME
    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return {}
    intent, cf = _intent(), _confirm()
    out: dict[int, list[int]] = {}
    for name in intent.SECTION_NAMES:
        for line in intent._section_bodies(text).get(name) or []:
            match = intent.ITEM_RE.match(line)
            if not match:
                continue
            try:
                row = int(match.group("row"))
            except (TypeError, ValueError):
                continue
            for shot in cf.shot_numbers(f"镜{match.group('shots')}"):
                number = int(shot)
                if number not in out.setdefault(row, []):
                    out[row].append(number)
    return out


# ---------------------------------------------------------------- 帧内框（AT-10 C2）

GRID_CELL_SIZE = round(100 / 3, 2)   # 九宫格单格宽高（内容盒百分比 33.33）


def grid_rect(cell) -> dict | None:
    """九宫格格号 → 内容盒百分比 {x%,y%,w%,h%}（AT-10 矩阵：1=左上…9=右下，行主序）。

    col=(N-1)%3、row=(N-1)//3，x/y=格左上角，w/h=33.33；坐标=图片 contain 后内容盒 0~100%。
    非 1-9 整数（含 bool、浮点、字符串）→ None：越界不猜、不默认格 5（越界降级）。
    """
    if isinstance(cell, bool) or not isinstance(cell, int) or not 1 <= cell <= 9:
        return None
    col, row = (cell - 1) % 3, (cell - 1) // 3
    return {"x%": round(col * 100 / 3, 2), "y%": round(row * 100 / 3, 2),
            "w%": GRID_CELL_SIZE, "h%": GRID_CELL_SIZE}


def frame_boxes(rows: list[dict], spans: dict[int, dict], names: dict[int, str],
                shot: int) -> list[dict]:
    """镜 shot 的帧内框集合（纯函数）：位置键镜次==shot 且格合法的行，行号升序。

    框集合按镜身份统一（AT-10 §三）：同帧所有卡拿同一集合，「本卡」标记由调用方逐卡打，
    纯函数里恒 False。角标=该行「建议」列原文（与 AT-8 卡片角标同源，禁新增识别调用）；
    短名兜底与卡片短名一致（names 缺 → 类别 → #行号）。格非法的行不出框。
    """
    out: list[dict] = []
    for row in sorted(rows, key=lambda r: r["no"]):
        no = row["no"]
        pos = (spans.get(no) or {}).get("画面位置")
        key_shot = pos.get("镜次") if isinstance(pos, dict) else None
        if isinstance(key_shot, bool) or not isinstance(key_shot, int) or key_shot != shot:
            continue
        rect = grid_rect(pos.get("格"))
        if rect is None:
            continue
        out.append({
            "行": no,
            "短名": names.get(no) or (row.get("cat") or f"#{no}"),
            "角标": row.get("advice") or "",
            "格": pos["格"],
            "x%": rect["x%"],
            "y%": rect["y%"],
            "w%": rect["w%"],
            "h%": rect["h%"],
            "本卡": False,
        })
    return out


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
    markdown = _summary_markdown(bridge_id)
    layers = _split(markdown)
    twins = {no: (payload.get("同实体疑似") or []) for no, payload in spans.items()}
    positions = {no: payload.get("画面位置") for no, payload in spans.items()}
    # AT-9 B2 确认组（sidecar 顶层『同实体确认』，写端点生产；疑似≠确认，疑似不联动）
    groups = replace.load_groups(bridge_id)
    names = short_names(rows, layers, twins, positions) if layers else {}
    cache = frames.load_cache(bridge_id)
    # ---- AT-10 C1 代表帧：分镜表时段 → 代表镜（位置键典型镜 → 意图建议镜）→ 同镜共享帧
    video = frames.video_path(bridge_id)
    total = frames.duration(video) or (_confirm().probe_duration(markdown) or 0.0)
    bounds: dict[int, tuple[float, float]] = {}
    shots_layer = layers.get("七 分镜表", "")
    if shots_layer:
        bounds = shot_bounds(shots_layer, total)
    suggested = load_suggested_shots(bridge_id)
    shared: dict[tuple, dict] = {}      # 组装级去重：("shot", 镜号) / ("frame", url) / ("fallback", 中段秒)
    box_cache: dict[int, list] = {}     # AT-10 C2：帧内框集合按镜缓存（同一镜只组装一次，同帧多卡复用）

    def representative(no: int, payload: dict, row_frames: list[dict]) -> dict:
        """该行代表帧（AT-15 S1 四级回退链）：

        ① 画面位置键所指镜（镜次直取——pop-1 式拆解只有「画面位置：九宫格5·镜2」，没有「出现于」）
        ② 意图建议.md 里第一个可用镜
        ③ 本行「帧」列表里首个 定位=精确 的时段帧（镜次未知 → 不标镜次、不画框）
        ④ 整片中段兜底（诚实兜底，最后一路）
        镜次越界/查不到（分镜表没这镜）→ 顺延下一路，不炸、不拿他镜帧冒充。
        """
        pos = payload.get("画面位置")
        shot = pos.get("镜次") if isinstance(pos, dict) else None
        if isinstance(shot, bool) or not isinstance(shot, int) or shot not in bounds:
            shot = next((cand for cand in suggested.get(no) or [] if cand in bounds), None)
        if shot is not None:
            key: tuple = ("shot", shot)
            if key not in shared:
                start, end = bounds[shot]
                shared[key] = frames.build_shot_frame(bridge_id, local_id, shot, start, end, cache, video)
            return shared[key]
        exact = next((frame for frame in row_frames
                      if frame.get("定位") == "精确" and frame.get("url")), None)
        if exact is not None:
            key = ("frame", str(exact.get("url")))
            if key not in shared:
                shared[key] = {"url": exact.get("url") or "", "秒": exact.get("秒"),
                               "镜次": None, "定位": "精确", "来源": "时段帧"}
            return shared[key]
        key = ("fallback", round((total or 0.0) / 2, 2))
        if key not in shared:
            shared[key] = frames.build_fallback_frame(bridge_id, local_id, video)
        return shared[key]

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
        rep_frame = representative(no, payload, shots)
        # AT-10 C2 帧内框：代表帧精确定位且镜次是 int → 该镜框集合（按镜缓存），逐卡 copy 打「本卡」；
        # 未定位 / 镜次非 int → []（不画框不标镜次）；本行无九宫格键 → 不产生自己的框但保留同镜他人的框
        rep_boxed = rep_frame.get("定位") == "精确" and isinstance(rep_frame.get("镜次"), int) \
            and not isinstance(rep_frame.get("镜次"), bool)
        boxes: list[dict] = []
        if rep_boxed:
            rep_shot = int(rep_frame["镜次"])
            if rep_shot not in box_cache:
                box_cache[rep_shot] = frame_boxes(rows, spans, names, rep_shot)
            boxes = [dict(box, 本卡=(box["行"] == no)) for box in box_cache[rep_shot]]
        # AT-10 C1 自身框标志：本行画面位置键正指代表帧所属镜（键的典型镜 = 代表镜）
        # ∧ 本行框确实生成（帧内框里存在 本卡=True 的框，C2 收紧：格非法/无键都不出框）→ 恒等于「帧内框有本卡框」
        # 键镜秒不可用回退到意图镜 / 无键 / 未定位 → False（回退镜没有该资产位置证据，只给帧不画框）
        pos = payload.get("画面位置")
        own_box = bool(isinstance(pos, dict)
                       and isinstance(pos.get("镜次"), int)
                       and not isinstance(pos.get("镜次"), bool)
                       and rep_boxed
                       and int(pos["镜次"]) == int(rep_frame["镜次"])
                       and any(box["本卡"] for box in boxes))
        cards.append({
            "n": no,
            "类别": row.get("cat") or "",
            "短名": names.get(no) or (row.get("cat") or f"#{no}"),
            "建议": row.get("advice") or "",
            "核心": is_core(row.get("cat") or "", row.get("advice") or ""),
            "出现时段": spans_now,
            "帧": shots,
            # AT-10 C1 代表帧（数据合同：{url, 秒, 镜次, 定位}；未定位时镜次 null、不画框不标镜次）
            # 自身框：见上方 own_box 注释——恒等于「帧内框里存在本卡框」，探针断言 #7=False
            "代表帧": rep_frame,
            "自身框": own_box,
            # AT-10 C2 帧内框集合（{行,短名,角标,格,x%,y%,w%,h%,本卡}）；同帧多卡共享同一 base 仅本卡标记不同
            "帧内框": boxes,
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

    # ---- AT-11 M1 组卡投影（§3.1.2 单帧单框，B4 闭合）：成员卡挂同一『组』投影对象 ----
    # 主行=『组元数据』落盘值（无 → 成员最小行，M2 确认窗可改）；代表帧与唯一框**同源自主行**
    # ——组卡只投影主行代表帧+主行自身框；主行无自身框 → 单框=None+单框降级=True（诚实降级，
    # 绝不搬组内其他行的框充数）。镜次并集=成员位置键镜次 ∪ 意图建议镜次（evidence 级）。
    decisions = replace.load_row_decisions(bridge_id)
    meta = replace.load_group_meta(bridge_id)
    by_no_cards = {card["n"]: card for card in cards}
    for card in cards:
        card["组"] = None
        card["决策"] = decisions.get(card["n"], "")
    for group in groups:
        key = replace.group_key(group)
        gm = meta.get(key) or {}
        primary = gm.get("主行")
        if not (isinstance(primary, int) and primary in group and primary in by_no_cards):
            primary = min(n for n in group if n in by_no_cards) if any(n in by_no_cards for n in group) else None
        if primary is None:
            continue                        # 成员都不在清单（坏组）：不投影，不猜
        pcard = by_no_cards[primary]
        own_box = next((dict(b) for b in (pcard.get("帧内框") or []) if b.get("本卡")), None)
        shot_union: set[int] = set()
        for no in group:
            pos_no = positions.get(no)
            if isinstance(pos_no, dict) and isinstance(pos_no.get("镜次"), int) \
                    and not isinstance(pos_no.get("镜次"), bool):
                shot_union.add(int(pos_no["镜次"]))
            shot_union |= {int(s) for s in (suggested.get(no) or []) if isinstance(s, int)}
        proj = {
            "键": key,
            "成员": list(group),
            "主行": primary,
            "组名": str(gm.get("组名") or ""),
            "代表帧": pcard.get("代表帧"),
            "单框": own_box,
            "单框降级": own_box is None,
            "镜次并集": sorted(shot_union),
        }
        for no in group:
            if no in by_no_cards:
                by_no_cards[no]["组"] = dict(proj)
    return cards
