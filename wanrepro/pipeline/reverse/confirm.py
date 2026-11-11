#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""S2.1/S2.2 待确认置换清单：十层拆解 → 四类实体 → 全预填的 待确认清单.md

为什么要有这份清单：多模态只看得见形态，看不见名字。实测同一件遮挡物被写成
「木制折叠尺」「木臂」，而它其实是别的东西。所以凡带身份的东西都不让模型命名：
模型只按形态预填「模型看到什么」，人只补「命名」「资产图」「用我的描述」三列，或者什么都不做。

四类实体与抽取来源（缺 一/二/三/七 任一层即退出 2）：
  主体  二 产品                      每个不同款式算一个实体（颜色/字样不同即不同）
  道具  三 运动                      除主体外运动中的物体（遮挡物、工具、机关）
  人物  一 场景 / 七 分镜表           出镜的人（数量 + 外形特征；细节回退到九 资产）
  场景  一 场景                      环境 / 背景 / 陈设

「模型看到什么」只写形态（颜色/形状/材质/轨迹），写盘前过一遍身份词清洗表；
「锁什么」由类别给出中性条目；「建议」由意图建议按清单行号回填；「命名」「资产图」「用我的描述」由人工确认。

「出现时段」不放表格列（人只看三列，机器要结构化）：生成清单时同步解析每行源资产的
镜号 → 分镜表起止，落成 md 旁边的 `待确认清单.json`（n = 表格物理行序），并标注同实体疑似
（AT-8 置换台的取帧依据）。解析不出时段的行留空数组，不抛错。

用法：
    py -3 pipeline/reverse/confirm.py analysis/reverse/<id>/拆解.md
    py -3 pipeline/reverse/confirm.py --verify analysis/reverse/<id>/待确认清单.md

退出码：
    0 成功；--verify 命中身份词只警告，不改退出码
    2 输入不存在，或缺 一/二/三/七 层
    3 一个实体都抽不到（不写清单）
    4 --verify：清单结构损坏，或类别不在四类内
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------- 常量

CHECKLIST_NAME = "待确认清单.md"
# 「出现时段」的结构化落点（AT-8 B1）：不占清单表格列，供置换台/取帧消费
CHECKLIST_SPANS_NAME = "待确认清单.json"
LAYER_SCENE = "一 场景"
LAYER_PRODUCT = "二 产品"
LAYER_MOTION = "三 运动"
LAYER_SHOTS = "七 分镜表"
LAYER_ASSETS = "九 资产"
LAYER_CLOSING = "十 图文与收束"
REQUIRED_LAYERS = (LAYER_SCENE, LAYER_PRODUCT, LAYER_MOTION, LAYER_SHOTS)

# 「品牌视觉（置换位）」（2026-11-04 路 A 审计 B4）：第十层品牌字/包装字的清单落点。
# 不是备图资产——参考片品牌图本来就不许照搬；值是客户品牌字样（待填）。
BRAND_VISUAL_CATEGORY = "品牌视觉（置换位）"

CATEGORIES = ("主体", "服装", "道具", "人物", "场景", BRAND_VISUAL_CATEGORY)

# 人工确认后能落到提示词里的类别，顺序即清单顺序
LOCK_BY_CATEGORY = {
    "主体": "外形比例、字标排版、顶部件形状",
    "道具": "颜色、支点位置、圆弧幅度、完全遮挡的那一帧",
    "人物": "发型与轮廓、服装颜色与版型、手持物位置",
    "服装": "颜色、版型轮廓、领口与袖长、鞋与配饰",
    "场景": "光影方向、纹理、相邻块面的边缘位置",
    BRAND_VISUAL_CATEGORY: "客户品牌字样（待填）",
}

# 「模型看到什么」里不许出现的身份词：模型看不见名字，这些是人的活。
BANNED_IDENTITY = (
    "吊扇", "风扇", "手机", "地毯", "坐垫", "桌子", "沙发", "瓶子", "杯子",
)

# 生成「模型看到什么」时的身份词清洗：正则 → 形态说法。
# 顺序有意义：带材质/类型前缀的「…瓶身」整体换掉，剩下的裸「瓶身」当表面。
IDENTITY_REWRITES = (
    (r"瓶盖", "盖体"),
    (r"瓶型", "外形"),
    (r"瓶口", "顶部开口"),
    (r"瓶体", "小物体"),
    (r"(?:玻璃|塑料|透明)瓶身", "小物体"),
    (r"(?:玻璃|塑料|透明)瓶", "小物体"),
    (r"瓶身", "表面"),
    (r"瓶子?", "小物体"),
    (r"喷雾盖", "盖体"),
    (r"便携", ""),
    (r"吊扇|风扇", "长条形旋转构件"),
    (r"地毯", "绒面平面"),
    (r"短绒毯面", "短绒面"),
    (r"毯面", "绒面"),
    (r"坐垫", "弧形软体块"),
    (r"沙发", "大面积软体块"),
    (r"桌子", "水平平面"),
    (r"手机", "扁平长方形物体"),
    (r"杯子", "带把圆柱体"),
    (r"女性演员", "女性"),
    (r"男性演员", "男性"),
    (r"演员", "出镜者"),
)

HEADING_RE = re.compile(r"^ {0,3}(#{1,6})[ \t]*(.+?)[ \t]*$")
BULLET_RE = re.compile(r"^[*\-+•]\s+")
NUMBERED_RE = re.compile(r"^\d+[.、)]\s*")
LABEL_RE = re.compile(
    r"^(?:\*\*(?P<bold>[^*]{1,60})\*\*|(?P<plain>[^：:]{1,14}))\s*[：:]\s*(?P<rest>.*)$"
)
SEPARATOR_CELL_RE = re.compile(r"^:?-{3,}:?$")
# 存在性空态行（T5，2026-11 路 A 审计 B1；T6 扩到第十层）：`* 人物：无（全片无人物出镜）`、
# `* 服装：无`、`* 人物：无完整人物（仅手部入画…）`、`* 图文：无（全片画面无持久性文字元素）`
# ——说明「这一类东西本片不存在」的行，不是资产条目。第十层的 `收束：…` 行是时间轴结构
# 不是实体，且四类实体的抽取来源（一/二/三/七/九 层）本来就不读第十层，天然不产实体；
# 这里把 `图文：无` 收进空态正则，是给「逐行扫全部层」的任何消费路径兜底。
# 正资产按契约必带资产名（`**服装A（出现于…）**：…`），不会以裸「类别：无…」开头，
# 因此「类别 + 冒号 + 无开头」三条件同时成立即判空态。空态行不进实体、不进待确认清单、
# 不产生备图行；正文原样保留在拆解.md（IR），由清单/投影各消费点过滤。
ABSENT_STATE_RE = re.compile(
    r"^[ \t]*[*\-+•]*[ \t]*\*{0,2}(?:人物|服装|动物|图文)\*{0,2}[ \t]*[：:][ \t]*无"
)
PERSON_RE = re.compile(r"女性|男性|女人|男人|女子|男子|女士|男士|人像|面部|半身")
MOVING_STILL_RE = re.compile(r"静止|不动|无运动|恒定不动")
THING_RE = re.compile(r"物体|构件|长条|长杆|支架|臂|杆|片|块|条|带|绳|板|环|球|件")

# 主体：款式的数量与枚举
STYLE_PREFIX_RE = re.compile(r"^共\s*\d{1,3}\s*款[，,、]?\s*(?:均为)?(?:同规格)?\s*")
PLAIN_COUNT_RE = re.compile(r"^[0-9]{1,3}\s*(?:个|款|支|只|瓶)[。.]?$")
# 「1件核心产品」「共4款，均为同规格」这类纯计数前缀不属于形态，摘掉
COUNT_PREFIX_RE = re.compile(r"^[0-9]{1,3}\s*(?:件|个|款|支|只)\s*(?:核心)?(?:产品|主体|款|物品)?[。，,]?\s*")
ARC_RE = re.compile(r"圆弧|弧线|圆心|旋转|支点")
COLOR_HEAD_RE = re.compile(r"^([\u4e00-\u9fa5]{1,4}色)")
COLOR_TAG_RE = re.compile(r"([\u4e00-\u9fa5]{1,3}色)[^，,；;。]{0,6}标签")
TIME_RANGE_RE = re.compile(r"[（(]\s*\d{1,2}:\d{2}\s*[–\-—~至]\s*\d{1,2}:\d{2}\s*[）)]")
LEAD_SUBJECT_RE = re.compile(r"^(?:背景|前景)?(?:为|是|中)?(?:一件|一个|一条|一支|一只|一把)?\s*")
TRAJ_LEAD_RE = re.compile(r"^(?:该|此)?(?:长条|前景)?(?:物体|构件|主体)\s*")


# ---------------------------------------------------------------- 十层切分

def normalize_heading(raw: str) -> str:
    """标题归一：全角空格当空格、压缩连续空白、去掉尾部标点。只做容错，不改字。"""
    text = raw.replace("\u3000", " ").replace("\u00a0", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text.rstrip("：:。.").strip()


def _norm_pipes(line: str) -> str:
    """半角竖线统一成全角，但**不碰真正的 Markdown 表格行**（以 `|` 开头的那些）。

    模型输出的分镜/清单有两种写法，必须都认：
      `* 01｜00:00–00:04｜…`（全角，历史格式）
      `1 | 00:00-00:01 | 海鲜拼盘 | …`（半角加空格）
    不归一的话第二种的 `split_shots()` 一行都取不到，人/道具/分镜全会丢。
    """
    if line.lstrip().startswith("|"):
        return line
    return re.sub(r"\s*\|\s*", "｜", line)


def split_layers(markdown: str) -> dict:
    """按 `^#{1,6}` 切段，返回 {层标题: 正文}。同标题多次出现时合并正文。"""
    sections: dict = {}
    order = []
    current = None
    buffer: list = []

    def flush():
        if current is not None:
            sections.setdefault(current, []).extend(buffer)

    for line in markdown.splitlines():
        match = HEADING_RE.match(line)
        if match:
            flush()
            current = normalize_heading(match.group(2))
            buffer = []
            order.append(current)
        elif current is not None:
            buffer.append(_norm_pipes(line))
    flush()
    return {title: "\n".join(lines).strip() for title, lines in sections.items()}


# ---------------------------------------------------------------- 行/单元清洗

def clean_cell(text: str) -> str:
    """表格单元安全化：去竖线、压空白；竖线会切断下游按 `|` 切列。"""
    text = text.replace("|", "／").replace("\r", " ").replace("\n", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def sanitize(text: str) -> str:
    """身份词 → 形态说法。模型只写形态，这张表保证落盘的文字里没有名字。"""
    for pattern, shape in IDENTITY_REWRITES:
        text = re.sub(pattern, shape, text)
    return clean_cell(text)


def clean_bullet(raw: str):
    """一行 → (标签, 正文)。去 `* `/`- `/`1. ` 前缀与 `**标签**：` 前缀。"""
    text = raw.strip()
    if not text:
        return "", ""
    text = BULLET_RE.sub("", text).strip()
    text = NUMBERED_RE.sub("", text).strip()
    match = LABEL_RE.match(text)
    if match:
        label = (match.group("bold") or match.group("plain") or "").strip()
        return label, match.group("rest").strip()
    return "", text


def is_absent_state(line: str) -> bool:
    """一行是否是存在性空态说明行（`人物：无…` / `服装：无` / `动物：无…` / `图文：无…`）。

    接受原样行（含项目符号/加粗）或 bullets() 拆出的 `标签：正文` 重建行。
    第十层的 `收束：…` 行不是空态（它是真事实：收束类型/版式/动效时序），
    也不该当实体——四类实体的抽取来源里没有第十层，天然不会读它。
    """
    return bool(ABSENT_STATE_RE.match(line or ""))


def bullets(body: str):
    """正文 → [(标签, 正文)]；只保留有正文的行。"""
    out = []
    for line in body.splitlines():
        if not line.strip():
            continue
        label, text = clean_bullet(line)
        text = text.rstrip("；;。，, ")
        if text:
            out.append((label, text))
    return out


def first_by_label(items, names) -> str:
    """按标签取第一条正文；标签含关键词即命中（模型每层的小标题写法不齐）。"""
    for label, text in items:
        for name in names:
            if name in label:
                return text
    return ""


def split_shots(body: str):
    """分镜表 → (表头 {列名: 下标}, 行 [cells])；兼容管道表格与 `* 镜号01 | …` 列表。

    全半角都认（AT-10 C1）：`split_layers()` 的 `_norm_pipes()` 会把**不以 `|` 开头**的行里的
    半角竖线转成全角 `｜`（其他下游依赖这个归一），而 pop-4 的分镜表正是「行首无 `|`、列间半角」，
    于是整表一行都进不来。这里读之前把 `｜` 换回 `|`，只影响分镜表这一处读取口径。
    """
    header = {}
    rows = []
    for line in body.splitlines():
        text = line.strip().lstrip("*").strip()
        text = text.replace("｜", "|")
        if "|" not in text:
            continue
        cells = [cell.strip() for cell in text.strip("|").split("|")]
        if not cells or all(SEPARATOR_CELL_RE.match(cell) for cell in cells if cell):
            continue
        if cells[0] in ("镜号", "#") or "运动形态" in cells:
            header = {name: index for index, name in enumerate(cells)}
            continue
        rows.append(cells)
    return header, rows


# ---------------------------------------------------------------- 四类实体

def extract_subjects(layer: str) -> list:
    """二 产品 → 每个不同款式一个主体实体。"""
    items = bullets(layer)

    # 先取「条目」：`1. xxx` 这类编号行才是各个款式
    styles = []
    for line in layer.splitlines():
        if not NUMBERED_RE.match(line.strip()):
            continue
        text = NUMBERED_RE.sub("", line.strip()).strip().rstrip("；;。，, ")
        if text:
            styles.append(text)

    # 2026-10-31：模型有时写成 `* **媒介产品（00:01-00:05）**：…` / `* **核心产品（00:04-00:14）**：…`，
    # 没有编号。旧逻辑会把第一条当「共同外形」、其余直接丢掉（pop-3 实测：只留下第一个，第二个被丢掉）。
    # 两个以上带「产品」字样的条目，就按条目逐个成行。
    product_items = [
        (label, text) for label, text in items
        if text and ("产品" in label or "商品" in label) and not PLAIN_COUNT_RE.match(text)
    ]
    if not styles and len(product_items) >= 2:
        rows = []
        for label, text in product_items:
            # 时段可能不止一段（`（00:00-00:01，00:01-00:07）`）：全部收集交给出现时段解析
            ranges = re.findall(
                r"[0-9]{1,2}:[0-9]{2}\s*[-–—~～至]\s*[0-9]{1,2}:[0-9]{2}", label
            )
            rows.append({
                "类别": "主体",
                "看到什么": sanitize(text),
                "命名": "",
                "出现于": "，".join(ranges),
            })
        return rows

    shape = first_by_label(
        items, ("数量与规格", "外形特征", "外形", "外观特征", "外观", "规格", "材质")
    )
    if not shape or PLAIN_COUNT_RE.match(shape):
        # 没有显式「外形特征」标签时的兜底：拿第一条**非条目**的正文当共同外形。
        # 不能直接拿 items[0]——若它本身就是某条款式，会被拼到每一条前面，
        # 导致第 1 条自己和自己重复、第 2 条被污染成「A；B」。
        joined = [
            text
            for _, text in items
            if not PLAIN_COUNT_RE.match(text)
            and not any(text.strip().startswith(s[:8]) for s in styles)
            # 「整盘 -> 蟹腿 -> 龙虾钳」这类是进食顺序，不是共同外形
            and "->" not in text
            and "→" not in text
            and "依次" not in text
        ]
        shape = "；".join(joined[:1]) if joined else ("" if styles else shape)
    shape = STYLE_PREFIX_RE.sub("", shape)
    # 摘掉计数前缀后可能留下开头的「。均为…」这种残句，句首标点一并清掉
    shape = COUNT_PREFIX_RE.sub("", shape).strip().lstrip("。，,；;、· ").rstrip("；;。")

    if styles:
        rows = []
        for style in styles:
            ranged = TIME_RANGE_RE.search(style)
            style = TIME_RANGE_RE.sub("", style).strip().rstrip("；;。，, ")
            name = COLOR_HEAD_RE.match(style)
            rows.append({
                "类别": "主体",
                # shape 可能为空（只抓到款式），此时 f"{shape}；{style}" 会拼出「；熟制…」这种空壳
                "看到什么": sanitize("；".join(x for x in (shape, style) if x)),
                "命名": (name.group(1) + "款") if name else "",
                # 编号款式括号里的时段（`1. 媒介产品（00:01-00:05）：…`）只留给「出现时段」解析，
                # 不写进「出现于」——那条键参与九 资产 判重，改它的口径会动到既有行数
                "源时间码": ranged.group(0).strip("（）() ") if ranged else "",
            })
        return rows

    name_match = COLOR_TAG_RE.search(shape)
    label_name = ""
    if name_match:
        # 「贴有亮橙色矩形标签」抓到的可能是「有亮橙色」，去掉动词字
        color = re.sub(r"^[有带贴印]", "", name_match.group(1))
        label_name = color + "标签款"
    return [{
        "类别": "主体",
        "看到什么": sanitize(shape),
        "命名": label_name,
    }]


def extract_props(motion_layer: str) -> list:
    """三 运动 → 除主体之外、真正在动的东西（遮挡物/工具/机关）。"""
    items = bullets(motion_layer)
    subject = first_by_label(items, ("运动主体",))
    if not subject:
        return []

    props = []
    for segment in re.split(r"[；;]", subject):
        segment = LEAD_SUBJECT_RE.sub("", segment.strip()).strip()
        if not segment or MOVING_STILL_RE.search(segment):
            continue
        if PERSON_RE.search(segment):          # 动的是人，算人物不算道具
            continue
        if not THING_RE.search(segment):       # 不是实体，跳过
            continue
        props.append(segment)
    if not props:
        return []

    trajectory = first_by_label(items, ("轨迹与方向", "轨迹"))
    trajectory = TRAJ_LEAD_RE.sub("", trajectory).strip().rstrip("；;。")
    period = first_by_label(items, ("周期与节奏", "周期性/重复", "周期性", "周期"))
    if trajectory:
        # 轨迹已经写了怎么扫，周期行开头的「周期性划过，」是同一件事
        period = re.sub(r"^(?:周期性|重复性)[^，,]{0,8}[，,]\s*", "", period)
    period = period.rstrip("；;。")

    # 转的锁支点与圆弧，直线进出的锁路径与速度——同一个字段不能两种口径
    arc = ARC_RE.search(trajectory + period)
    lock = LOCK_BY_CATEGORY["道具"] if arc else "颜色、形状比例、运动路径与速度、遮挡的那一帧"

    rows = []
    for prop in props:
        parts = [prop.rstrip("；;。")]
        if trajectory:
            parts.append(trajectory)
        if period:
            parts.append(period)
        rows.append({
            "类别": "道具",
            "看到什么": sanitize("；".join(parts)),
            "命名": "",
            "锁什么": lock,
        })
    return rows


def extract_people(scene_layer: str, shots_layer: str, assets_layer: str) -> list:
    """一 场景 / 七 分镜表 → 出镜的人（数量 + 外形特征）；细节回退到九 资产。"""
    header, shot_rows = split_shots(shots_layer)
    motion_index = header.get("运动形态", 3)

    gender = ""
    motions = []
    person_seen = False
    for cells in shot_rows:
        subject = cells[2] if len(cells) > 2 else ""
        if PERSON_RE.search(subject):
            person_seen = True
            if not gender:
                match = re.search(r"女性|男性|女人|男人|女子|男子|女士|男士", subject)
                gender = match.group(0) if match else ""
        if len(cells) > motion_index:
            motion = cells[motion_index].strip()
            if motion and motion not in motions:
                motions.append(motion)
    for _, text in bullets(scene_layer):
        if PERSON_RE.search(text):
            person_seen = True
            if not gender:
                match = re.search(r"女性|男性|女人|男人|女子|男子|女士|男士", text)
                gender = match.group(0) if match else ""
    if not person_seen:
        return []

    count = 1
    for _, text in bullets(scene_layer):
        match = re.search(r"([两二三四五六七八九十\d]+)\s*(?:位|名|人)", text)
        if match:
            count = match.group(1)
            break

    appearance = ""
    for label, text in bullets(assets_layer):
        if is_absent_state(f"{label}：{text}"):
            continue                   # 空态行不当外形来源（如「无完整人物（仅半身剪影…）」）
        if not PERSON_RE.search(text):
            continue
        head = re.split(r"必须锁住|必须锁|要锁住", text)[0].strip().rstrip("。；;，,")
        head = re.sub(r"^[^，,]{0,8}资产[：:，,]?\s*", "", head)
        appearance = sanitize(head)
        break

    head = f"{count} 位{gender}" if gender else f"{count} 位出镜者"
    parts = [head + "，面部正对镜头"]
    if appearance:
        # 外形里自带「女性」，前面已经说了人数与性别，去掉重复的那个词
        if gender:
            appearance = appearance.replace(gender, "", 1).lstrip("，, ")
        parts.append(appearance)
    if motions:
        parts.append("动作：" + "；".join(m.rstrip("。；;") for m in motions[:3]))
    return [{"类别": "人物", "看到什么": sanitize("；".join(parts)), "命名": ""}]


def extract_scene(scene_layer: str) -> list:
    """一 场景 → 环境/背景/陈设一条（四类里唯一允许合并成一行的）。"""
    texts = [text for _, text in bullets(scene_layer)]
    if not texts:
        return []
    joined = "；".join(text.rstrip("。；;") for text in texts[:4])
    joined = re.sub(r"[。；;]{2,}", "；", joined)
    if len(joined) > 220:
        joined = joined[:219] + "…"
    return [{"类别": "场景", "看到什么": sanitize(joined), "命名": ""}]


ASSET_KIND_BY_PREFIX = (
    ("服装", "服装"),
    ("穿搭", "服装"),
    ("配饰", "服装"),
    ("人物", "人物"),
    ("角色", "人物"),
    ("模特", "人物"),
    ("场景", "场景"),
    ("背景", "场景"),
    ("环境", "场景"),
    ("道具", "道具"),
    ("产品", "主体"),
    ("商品", "主体"),
)


ASSET_TYPE_PREFIXES = ("人物", "服装", "道具", "场景")


def parse_asset_entry(label: str, body: str = "") -> tuple[str, str, str]:
    """`人物 人物A（出现于：00:00-00:09、00:11-00:14）` → ("人物", "人物A", "00:00-00:09、00:11-00:14")。

    2026-10-31 新契约上线后模型这样写条目，旧解析把整串当正文，类别一律落到「道具」。
    """
    text = (label or "").strip().strip("*").strip()
    if not text:                      # 没有标签时，从正文里找类型前缀
        text = (body or "").strip()[:20]
    kind = "道具"
    for prefix in ASSET_TYPE_PREFIXES:
        if text.startswith(prefix):
            kind = prefix
            text = text[len(prefix):].strip(" ：:*-")
            break
    appear = ""
    match = re.search(r"[（(]\s*出现于[:：]?\s*([^）)]*)[）)]", text)
    if match:
        appear = match.group(1).strip()
        text = (text[:match.start()] + text[match.end():]).strip()
    name = text.strip(" ：:*-")
    return kind, name, appear


def asset_kind(label: str) -> str:
    """「九 资产」条目的名字 → 清单类别。

    2026-10-31：原来所有条目硬编码成「道具」，服装无处可去；可灵的元素库把
    「服装配饰」和「人物」并列成两类资产，这里照同一口径分。
    """
    return parse_asset_entry(label)[0]


def extract_assets(assets_layer: str, existing: list) -> list:
    """「九 资产」→ 补充行（常驻调用，不再只是没道具时的兜底）。

    2026-10-31 实测（pop-3）：九层九写了 8 条，清单只出 3 行——第二个产品、两套服装、第二个场景
    全丢。根因是这里自己写了一个只认 `1. xxx` 编号的正则，而模型这次用的是
    `* **服装A**：…` 项目符号；上面的 bullets() 本来就能解析（实测 8 条全出），
    这段重复实现把已经解析出来的东西又扔了一遍。现在直接复用 bullets()。

    去重：与已有的主体/人物/场景行按文字比对，同一件东西不重复登记。
    """
    import difflib

    def norm(text: str) -> str:
        return re.sub(r"[\s，。；、,.;:：（）()「」\"'`]", "", text)

    def twin(a: str, b: str, thr: float) -> bool:
        if not a or not b:
            return False
        if a in b or b in a:
            return True
        return difflib.SequenceMatcher(None, a, b).ratio() >= thr

    entries = [(label, text.strip().rstrip("；;。，, ")) for label, text in bullets(assets_layer)]
    entries = [(label, text) for label, text in entries if text]
    # 存在性空态行不是资产条目（2026-11 路 A 审计 B1 实测：`人物：无完整人物…` 被抽成人物
    # 实体后 render_assets 要求给不存在的人备人物参考图）。这里直接跳过；正文留在拆解.md，
    # 由拉片投影等消费点过滤。
    entries = [(label, text) for label, text in entries
               if not is_absent_state(f"{label}：{text}")]

    existing_text = " ".join(norm(str(r.get("看到什么") or "")) for r in existing)
    person_labels = [label for label, _ in entries if asset_kind(label) == "人物"]
    scene_count = sum(1 for label, _ in entries if asset_kind(label) == "场景")
    has_person_row = any(r.get("类别") == "人物" for r in existing)
    has_scene_row = any(r.get("类别") == "场景" for r in existing)

    def entity_hint(label: str) -> str:
        """`道具B（核心产品）` → `核心产品`：括号里那个词才是实体名。"""
        match = re.search(r"[（(]([^）)]+)[）)]", label)
        if not match:
            return ""
        hint = match.group(1).strip()
        hint = re.sub(r"^(?:核心|主要|次要|主|次)", "", hint)
        return norm(hint)

    subject_texts = [norm(str(r.get("看到什么") or "")) for r in existing if r.get("类别") == "主体"]
    subject_appear = {
        re.sub(r"\s", "", str(r.get("出现于") or "")): norm(str(r.get("看到什么") or ""))
        for r in existing
        if r.get("类别") == "主体" and r.get("出现于")
    }

    rows = []
    for label, body in entries:
        kind, name, appear = parse_asset_entry(label, body)
        core = norm(body)
        # 同一个东西已经作为「主体(产品)」登记过：九 资产 里又写一遍「道具  XX」
        if kind == "道具":
            if any(twin(core, s, 0.45) for s in subject_texts):
                continue
            same_range = subject_appear.get(re.sub(r"\s", "", appear), "")
            if same_range and name[:2] and name[:2] in same_range:
                continue
        # 条目名里的实体词（sanitize 过，和正文同口径）已经出现在主体行里 → 同一件东西
        hint = norm(sanitize(name)) if name else ""
        # 人物：只有一个人物条目、且已有出镜人物行 → 不重复登记
        if kind == "人物" and has_person_row and len(person_labels) == 1:
            continue
        # 括号里点了名，而这个实体已经在别的行里出现过（同一个东西同时出现在二 产品 与九 资产）
        if len(hint) >= 2 and hint in existing_text and not (kind == "场景" and scene_count >= 2):
            continue
        # 其余：整条正文已在别处出现 → 跳过
        if core and any(twin(core, norm(str(r.get("看到什么") or "")), 0.75) for r in existing):
            continue
        if core and any(twin(core, norm(str(r.get("看到什么") or "")), 0.75) for r in rows):
            continue
        shown = name or label
        rows.append({
            "类别": kind,
            "看到什么": sanitize(f"{shown}：{body}"),
            "命名": "",
            "出现于": appear,
        })
    return rows


def extract_brand_visuals(closing_layer: str) -> list:
    """「十 图文与收束」的品牌字/包装字 → 一行「品牌视觉（置换位）」（B4）。

    此前 confirm 不消费第十层：资产表只有产品/道具/场景，客户品牌值没有落点，
    品牌字只剩正文里的占位文本。这里补一个可编辑的置换位——不要求备图（参考片
    品牌图本来就不许照搬），值是「客户品牌字样（待填）」。字幕/标语/角标/水印是
    文案位，生成端占位（客户文案（定稿后填入））即可，不进资产表。
    「建议」列直接写「必须换」（T6-G）：品牌视觉置换位天然必换，与类别语义一致；
    真机对照（pop2-source-2）里该列曾为空，下游一度按建议值过滤把整行丢掉。
    注意意图建议回填仍会按行号覆盖此列——消费端不得依赖它（render 侧免建议条件收行）。
    """
    hits = [f"{label}：{text}" for label, text in bullets(closing_layer)
            if re.search(r"品牌字|包装字", label)
            and not is_absent_state(f"{label}：{text}")]
    if not hits:
        return []
    return [{
        "类别": BRAND_VISUAL_CATEGORY,
        "看到什么": sanitize(f"参考片品牌位 {'；'.join(hits)}（原字已置换，不要求备参考片品牌图）"),
        "命名": "",
        "锁什么": LOCK_BY_CATEGORY[BRAND_VISUAL_CATEGORY],
        "建议": "必须换",
    }]


# ---------------------------------------------------------------- 出现时段（AT-8 B1）

# 计时口径：拆解.md 的分镜表与第十层都写成 `00:06-00:08` 这种**两段**计时，15 秒的片里
# `00:12-00:15` 就是 12 秒到 15 秒（不是 12 分）。所以「出现时段」照抄同一口径输出 MM:SS，
# 不做时/分换算：B2 取帧把「起」「止」直接按秒读，与数据合同里 `帧.秒` 同源。
# AT-10 C1 认全格式族：`2.03`（纯秒小数）/`2.03s`/`2.03秒`/`00:02.03`（MM:SS.SS）/
# `00:00:02.03`（H:MM:SS.SS）/`00:12`（分:秒）/`02:03`（两段整数，**单 token 层按分:秒**，
# 歧义只在整张分镜表层联合判断，见 parse_shot_times）。
CLOCK_RE = re.compile(
    r"^(?P<first>\d{1,3})"
    r"(?:\s*[:：]\s*(?P<second>\d{1,2}(?:\.\d+)?)"
    r"(?:\s*[:：]\s*(?P<third>\d{1,2}(?:\.\d+)?))?"
    r"|\.(?P<frac>\d+))?$"
)
_CLOCK_UNIT_RE = re.compile(r"\s*(?:s|S|秒)\s*$")
# 一个时间码 token：两段/三段冒号（末段可带小数）或纯秒小数，两端都认；尾部单位另容
_CLOCK_TOKEN = (
    r"\d{1,3}(?:\s*[:：]\s*\d{1,2}(?:\.\d+)?"
    r"(?:\s*[:：]\s*\d{1,2}(?:\.\d+)?)?|\.\d+)?"
)
_CLOCK_UNIT = r"(?:\s*(?:s|S|秒))?"
CLOCK_SPAN_RE = re.compile(
    r"(" + _CLOCK_TOKEN + r")" + _CLOCK_UNIT
    + r"\s*[-–—~～至]\s*"
    + r"(" + _CLOCK_TOKEN + r")" + _CLOCK_UNIT
)
# 歧义两段整数 `NN:NN`：mm:ss = M*60+S，ss:cs = M + S/100（S≤99）
AMBIGUOUS_CLOCK_RE = re.compile(r"^(\d{1,3})\s*[:：]\s*(\d{1,2})$")
SHOT_LIST_RE = re.compile(
    r"(?:镜次|镜号|镜)\s*[:：]?\s*((?:\d{1,3}(?:\s*[-–—~～至]\s*\d{1,3})?\s*[、,，;；/]?\s*)+)"
)
SPAN_SEP_RE = re.compile(r"[、,，;；/]")
SHOT_NUMBER_SPAN_RE = re.compile(r"^(\d{1,3})\s*[-–—~～至]\s*(\d{1,3})$")
# 同实体疑似阈值：首句（形态主语）覆盖率与整段覆盖率都过才认，只过一个说明是「顺带提到」
TWIN_FIRST_MIN = 0.45
TWIN_FULL_MIN = 0.30
# 「画面位置：九宫格N·镜M」（AT-9 B1 注入的九 资产 条目尾段 → sidecar 新键，数据合同 v3）
# 分隔符宽容：模型可能写 `九宫格5·镜2` / `九宫格 5，镜 2` / `九宫格5:镜2`，锚点（画面位置/九宫格/镜）必须齐
POSITION_RE = re.compile(
    r"画面位置\s*[：:]\s*九宫格\s*(\d{1,2})\s*[·•・.。,，;；:：\s]*镜\s*(\d{1,3})"
)


def strip_unit(text: str) -> str:
    """时间码去掉尾部单位（`2.03s` / `2.03秒` → `2.03`）。"""
    return _CLOCK_UNIT_RE.sub("", str(text if text is not None else "").strip()).strip()


def clock_to_seconds(text: str):
    """单个时间码 → 秒；读不出返回 None（不猜）。

    认的格式族（AT-10 C1）：
      `2.03` 纯秒小数 → 2.03；`2.03s` / `2.03秒` → 2.03；
      `00:02.03` / `0:02.03`（MM:SS.SS）→ 2.03；`00:00:02.03`（H:MM:SS.SS）→ 2.03；
      `00:12` → 12.0（分:秒口径）；`02:03` → 123.0（两段整数仍按分:秒，**不在这里一律按 ss:cs**）。

    例外：模型有把 1 秒内的镜次写成 `00:87`（= 0.87 秒）的实测
    （analysis/reverse/1：全片 7.50 秒，分镜写成 00:00-00:87、00:87-01:97、05:90-07:45），
    秒位 ≥ 60 只有这种写法解释得通，按「分 + 秒位/100」读。

    两段 `NN:NN` 的 mm:ss / ss:cs 歧义不在这里裁决——整张分镜表联合判断见 parse_shot_times。
    """
    raw = strip_unit(text)
    if not raw:
        return None
    match = CLOCK_RE.match(raw)
    if not match:
        return None
    first, second, third, frac = (
        int(match.group("first")), match.group("second"),
        match.group("third"), match.group("frac"),
    )
    if frac is not None:
        return round(first + float("0." + frac), 3)
    if third is not None:
        return round(first * 3600 + float(second) * 60 + float(third), 3)
    if second is not None:
        tail = float(second)
        if tail >= 60:
            return round(first + tail / 100.0, 3)
        return round(first * 60 + tail, 3)
    return float(first)


def format_clock(seconds) -> str:
    """秒 → `MM:SS`（整秒）/ `MM:SS.ss`（有小数；保精度，AT-10 C1）。

    `format_clock(5) == "00:05"`、`format_clock(2.03) == "00:02.03"`、`format_clock(6.8) == "00:06.80"`。
    整秒输出不带小数，旧口径（清单/侧车的 `MM:SS`）与旧断言都不破。
    """
    cents = int(round(max(0.0, float(seconds)) * 100))
    whole, fraction = divmod(cents, 100)
    minutes, sec = divmod(whole, 60)
    if fraction:
        return f"{minutes:02d}:{sec:02d}.{fraction:02d}"
    return f"{minutes:02d}:{sec:02d}"


def _clock_span_rows(shots_layer: str) -> list[tuple[int, str, str]]:
    """分镜表 → [(镜号, 起 token, 止 token)]（表序；读不出时间码的行跳过）。"""
    header, rows = split_shots(shots_layer)
    time_index = next(
        (header[name] for name in ("起秒-止秒", "起秒", "时间码", "时间") if name in header), 1
    )
    out: list[tuple[int, str, str]] = []
    for cells in rows:
        if len(cells) <= time_index:
            continue
        digits = re.sub(r"[^0-9]", "", cells[0])
        if not digits:
            continue
        match = CLOCK_SPAN_RE.search(cells[time_index])
        if not match:
            continue
        out.append((int(digits), strip_unit(match.group(1)), strip_unit(match.group(2))))
    return out


def ambiguous_clock(token: str) -> bool:
    """两段整数 `NN:NN` = 歧义 token（mm:ss 与 ss:cs 都解释得通）。"""
    return bool(AMBIGUOUS_CLOCK_RE.match(strip_unit(token)))


def _segment_seconds(token: str, mode: str):
    """按指定全局模式解一个 token；读不出返回 None。

    mode=`mm:ss` 两段整数读 M*60+S；mode=`ss:cs` 读 M + S/100（S≤99）。明确小数/三段不受模式影响。
    """
    token = strip_unit(token)
    match = AMBIGUOUS_CLOCK_RE.match(token)
    if match:
        first, second = int(match.group(1)), int(match.group(2))
        if mode == "ss:cs":
            return round(first + second / 100.0, 3) if second <= 99 else None
        return float(first * 60 + second)
    return clock_to_seconds(token)


def _spans_plausible(pairs: list[tuple[int, float | None, float | None]],
                     duration: float | None) -> bool:
    """整表可行性：段起止有序 + 按镜号单调 + 有片长时末段右端≈片长（±0.5s）。"""
    if not pairs:
        return False
    for _, start, end in pairs:
        if start is None or end is None:
            return False
        if start < 0 or end < start:
            return False
        if duration and end > duration + 0.5:
            return False
    ordered = sorted(pairs, key=lambda item: item[0])
    for prev, nxt in zip(ordered, ordered[1:]):
        if nxt[1] < prev[2] - 0.01:
            return False
    if duration:
        if abs(ordered[-1][2] - duration) > 0.5:
            return False
    return True


def parse_shot_times(shots_layer: str, duration: float | None = None) -> dict:
    """七 分镜表 → {镜号: (起, 止)}；值按 format_clock（整秒 MM:SS，有小数带两位）。

    时间码消歧 v2（数据合同；AT-10 C1）：明确小数（`02.03`）/ 三段 / 带单位的不歧义；
    两段整数 `NN:NN` 是歧义 token，只有 mm:ss 与 ss:cs 两种解释。**不逐 token 猜**：
      · 全表按 mm:ss 求一遍、按 ss:cs 求一遍，各自校验（段内 end≥start、按镜号段间单调、
        有片长时末段右端与片长差 ≤0.5s 且不越界）；
      · 只有一种过 → 用它；两种都过 → 用 mm:ss（兼容旧片/sidecar 口径，第十层 `00:05-00:07` 即 5~7s）；
      · 都不过 → 只留不含歧义 token 的镜，歧义镜丢弃（消费端 → 未定位），不猜。
    """
    raw = _clock_span_rows(shots_layer)
    if not raw:
        return {}
    evaluated = {}
    for mode in ("mm:ss", "ss:cs"):
        evaluated[mode] = [
            (shot, _segment_seconds(start, mode), _segment_seconds(end, mode))
            for shot, start, end in raw
        ]
    for mode in ("mm:ss", "ss:cs"):
        if _spans_plausible(evaluated[mode], duration):
            return {str(shot): (format_clock(start), format_clock(end))
                    for shot, start, end in evaluated[mode]}
    times = {}
    for shot, start_token, end_token in raw:
        if ambiguous_clock(start_token) or ambiguous_clock(end_token):
            continue
        start, end = clock_to_seconds(start_token), clock_to_seconds(end_token)
        if start is None or end is None or end < start:
            continue
        times[str(shot)] = (format_clock(start), format_clock(end))
    return times


DURATION_HEAD_RE = re.compile(r"时长\s*[：:]\s*([0-9]+(?:\.[0-9]+)?)\s*(?:s|秒)?")
DURATION_TAIL_RE = re.compile(r"·\s*([0-9]+(?:\.[0-9]+)?)\s*秒")


def probe_duration(markdown: str) -> float | None:
    """拆解头部取片长（`时长: 6.85s` / `· 6.85 秒`）；取不到 None（消歧时退化为不加片长校验）。"""
    text = str(markdown or "")
    for pattern in (DURATION_HEAD_RE, DURATION_TAIL_RE):
        match = pattern.search(text)
        if not match:
            continue
        try:
            return round(float(match.group(1)), 3)
        except ValueError:
            continue
    return None


def whole_span(shot_times: dict) -> dict:
    """全片时段 = 首镜起 → 末镜止（「出现于：全片」用）。"""
    if not shot_times:
        return {}
    return {
        "起": min(value[0] for value in shot_times.values()),
        "止": max(value[1] for value in shot_times.values()),
    }


def shot_numbers(appear: str) -> list:
    """`镜 1、3-8` → ['1','3','4',…,'8']（去重保序）；不是镜号写法返回 []。"""
    match = SHOT_LIST_RE.search((appear or "").strip())
    if not match:
        return []
    numbers = []
    for part in SPAN_SEP_RE.split(match.group(1)):
        part = part.strip()
        if not part:
            continue
        between = SHOT_NUMBER_SPAN_RE.match(part)
        if between:
            first, last = sorted((int(between.group(1)), int(between.group(2))))
            numbers.extend(str(index) for index in range(first, last + 1))
        elif part.isdigit():
            numbers.append(str(int(part)))
    out = []
    for number in numbers:
        if number not in out:
            out.append(number)
    return out


def merge_spans(pairs) -> list:
    """[(起, 止)] → [{'起','止'}]：首尾相接或重叠的相邻段合成一段，其余按出现顺序保留。"""
    merged = []
    for start, end in pairs:
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return [{"起": start, "止": end} for start, end in merged]


def spans_from_text(text: str) -> list:
    """正文里现成的时间码 → 时段数组（第十层品牌位一行多段、二 产品括号里的时段）。

    AT-10 C1：span 正则现在也认纯数字 token（`2.03`），散文里的「20-25 岁」会被它当成时段。
    所以这里要求**起止 token 至少一个带 `:`/`：`/`.`** 才接受——时间是「有刻度」的写法，
    裸整数区间是数量/年龄区间。
    """
    pairs = []
    for match in CLOCK_SPAN_RE.finditer(text or ""):
        start_token, end_token = match.group(1), match.group(2)
        if not any(ch in start_token + end_token for ch in (":", "：", ".")):
            continue
        start, end = clock_to_seconds(start_token), clock_to_seconds(end_token)
        if start is None or end is None or end < start:
            continue
        pairs.append((format_clock(start), format_clock(end)))
    return merge_spans(pairs)


def spans_from_appearance(appear: str, shot_times: dict) -> list:
    """「出现于」原文 → 时段数组（AT-15 R1：复合写法两路都认）。

    认三种写法：
      · `00:00-00:03 / 镜1-3`（复合：**时段与镜号都给**）→ 取显式时间码。时间码是这一条
        自己的刻度，比分镜表更直接，也不怕分镜表整表被拒（pop-1 现场就是表被拒 → 全兜底）；
        旧实现先认镜号，表读不出时这段就成了「无时段」，白丢一条证据。
      · `镜 1、7` / `镜 3-6` → 按分镜表换算（表里没有这镜 → []，交给下一跳兜底）。
      · `全片` → 首镜起 → 末镜止。
    时段解析不猜：时间码按 confirm.clock_to_seconds 的既有口径，读不出就不认。
    """
    explicit = spans_from_text(appear)
    if explicit:
        return explicit
    numbers = shot_numbers(appear)
    if numbers:
        pairs = [shot_times[number] for number in numbers if number in shot_times]
        return merge_spans(pairs)          # 镜号在分镜表没有行 → []，交给下一跳兜底
    if "全片" in (appear or ""):
        whole = whole_span(shot_times)
        return [whole] if whole else []
    return []


def entry_position(label: str, body: str = "") -> dict | None:
    """条目里的「画面位置：九宫格N·镜M」→ {"格": N, "镜次": M}；没写/格不在 1–9 → None。

    AT-9 B1：生产者=拆解任务书【§9·画面位置】注入，模型在每条九 资产 条目末尾带这一段；
    旧拆解（没有这一段）自然落 None，消费端按「未能标注」兜底——合同不要求补写。
    """
    match = POSITION_RE.search(f"{label or ''}：{body or ''}")
    if not match:
        return None
    cell, shot = int(match.group(1)), int(match.group(2))
    if not 1 <= cell <= 9 or shot <= 0:
        return None
    return {"格": cell, "镜次": shot}


def asset_entries(assets_layer: str) -> list:
    """九 资产 → [{类别, 名字, 出现于, 画面位置, 正文}]（空态行跳过）——时段解析的源资产表。"""
    entries = []
    for label, text in bullets(assets_layer):
        if is_absent_state(f"{label}：{text}"):
            continue
        kind, name, appear = parse_asset_entry(label, text)
        if not appear:
            # 出现时段也可能落在正文侧（`**道具B**（出现于：镜 3-6）：…` 被拆成标签+正文）
            found = re.search(r"[（(]\s*出现于[:：]?\s*([^）)]*)[）)]", f"{label}：{text}")
            appear = found.group(1).strip() if found else ""
        entries.append({"类别": kind, "名字": name, "出现于": appear,
                        "画面位置": entry_position(label, text), "正文": text})
    return entries


def span_key(text: str) -> str:
    """比对用归一：去标点空白，去行首 `A：` 这类序号标记。"""
    key = re.sub(r"[\s，。；、,.;:：（）()「」『』\"'`｜|*\-—–→…]", "", str(text or ""))
    return re.sub(r"^[A-Za-zＡ-Ｚａ-ｚ]{1,2}(?=[\u4e00-\u9fa5])", "", key)


def span_bigrams(text: str) -> set:
    key = span_key(text)
    if not key:
        return set()
    return {key[index:index + 2] for index in range(len(key) - 1)} or {key}


def span_overlap(left: str, right: str) -> float:
    """交集占较短一方的比例：同源描述即使长短悬殊（长行带细部、短行只留形态）也认得出。"""
    left_grams, right_grams = span_bigrams(left), span_bigrams(right)
    if not left_grams or not right_grams:
        return 0.0
    return len(left_grams & right_grams) / min(len(left_grams), len(right_grams))


def first_clause(text: str) -> str:
    """描述首句 = 形态主语所在（`细长圆柱形…，亮面…` → 前一句）。"""
    return re.split(r"[，,；;。｜|]", str(text or ""), maxsplit=1)[0]


def twin_candidates(left: str, right: str) -> bool:
    """同实体只在「角色可换」的类别之间判：主体↔道具，或同一类别（主体↔主体除外）。"""
    if left == right:
        return left != "主体"          # 主体与主体是不同款式，按契约就是不同实体
    return {left, right} == {"主体", "道具"}


def mark_twin_entities(rows: list) -> list:
    """同实体疑似：同一件东西被登记成两行（例：主体行与道具行写的是同一件产品）。

    判据（两条都要过）：
      ① 角色可换：见 twin_candidates；
      ② 描述同源：首句覆盖率 ≥ TWIN_FIRST_MIN 且 整段覆盖率 ≥ TWIN_FULL_MIN。
         整段闸门挡住「顺带提到」——一行末尾顺带描述的另一件东西不是同实体。
    结果对称写进每行「同实体疑似」（对方行号 = 清单物理行序 n，升序）。
    """
    for row in rows:
        row.setdefault("同实体疑似", [])
    for index, row in enumerate(rows):
        for other in rows[index + 1:]:
            if not twin_candidates(row["类别"], other["类别"]):
                continue
            left, right = str(row.get("看到什么") or ""), str(other.get("看到什么") or "")
            if span_overlap(first_clause(left), first_clause(right)) < TWIN_FIRST_MIN:
                continue
            if span_overlap(left, right) < TWIN_FULL_MIN:
                continue
            row["同实体疑似"].append(other["idx"])
            other["同实体疑似"].append(row["idx"])
    for row in rows:
        row["同实体疑似"] = sorted(set(row["同实体疑似"]))
    return rows


def twin_asset_entry(row: dict, entries: list):
    """清单行 → 九 资产 里同一件东西的条目；挑不出返回 None。"""
    if not entries:
        return None
    category = row.get("类别")
    pool = [entry for entry in entries if entry["类别"] == category]
    if not pool:
        pool = [entry for entry in entries
                if entry["类别"] in ("主体", "道具") and category in ("主体", "道具")]
    if not pool:
        return entries[0] if len(entries) == 1 else None
    if len(pool) == 1:
        return pool[0]
    text = str(row.get("看到什么") or "")
    best, best_score = None, 0.0
    for entry in pool:
        body = f"{entry['名字']} {entry['正文']}"
        score = (span_overlap(body, text)
                 + span_overlap(first_clause(body), first_clause(text))) / 2
        if score > best_score:
            best, best_score = entry, score
    return best if best_score >= TWIN_FULL_MIN else None


def attach_appear_spans(rows: list, layers: dict, duration: float | None = None) -> list:
    """补齐每行「出现时段」+「同实体疑似」（不写进 md 表格）。

    数据流：九 资产「出现于：镜 N-M」→ 七 分镜表「镜号 → 起秒-止秒」→ MM:SS 时段数组，
    多时段全收集（首尾相接的合一段）。逐级兜底（数据合同 v3 兜底链前两跳）：
      ① 行自带时段：镜号走分镜表；`00:04-00:15` 直接用；`全片` 取整片
      ② 绑到九 资产 里同一件东西，用它的「出现于」
      ③ 正文自带时间码（第十层品牌位一行多段）
      ④ 都解析不出 → 空数组（B2 走中段帧兜底），这里不抛错
    """
    shot_times = parse_shot_times(layers.get(LAYER_SHOTS, ""), duration)
    entries = asset_entries(layers.get(LAYER_ASSETS, ""))
    for row in rows:
        raw = str(row.get("源时间码") or row.get("出现于") or "")
        spans = spans_from_appearance(raw, shot_times)
        source = f"出现于 {raw}" if spans else (f"未解析 出现于 {raw}" if raw else "")
        if not spans:
            entry = twin_asset_entry(row, entries)
            if entry:
                spans = spans_from_appearance(entry["出现于"], shot_times)
                if spans:
                    source = f"九 资产「{entry['名字'] or entry['类别']}」出现于 {entry['出现于']}"
        if not spans:
            spans = spans_from_text(row.get("看到什么") or "")
            if spans:
                source = "正文时间码"
        row["出现时段"] = spans
        row["时段来源"] = source
    # AT-9 B1：画面位置（九宫格 1–9 + 所属镜次）——九 资产 条目带的尾段随同实体匹配带到清单行；
    # 匹配不到条目的行保持 None（合同「旧任务缺值 → 前端描述首句常驻+未能标注」同款兜底）。
    for row in rows:
        if row.get("画面位置"):
            continue
        entry = twin_asset_entry(row, entries)
        if entry and entry.get("画面位置"):
            row["画面位置"] = entry["画面位置"]
    return mark_twin_entities(rows)


def span_payload(video_id: str, rows: list) -> dict:
    """清单行 → 结构化时段（AT-8 数据合同 v3 的 asset_cards 前置字段）。"""
    return {
        "schema": "at8-asset-spans-v1",
        "video_id": video_id,
        "source": "拆解.md",
        "计时": "MM:SS（与拆解.md 分镜表同口径，B2 取帧按秒读）",
        "rows": [
            {
                "n": row["idx"],
                "类别": row["类别"],
                "出现时段": list(row.get("出现时段") or []),
                "画面位置": row.get("画面位置"),          # AT-9 B1 新键：{"格":1-9,"镜次":N} 或 None
                "同实体疑似": list(row.get("同实体疑似") or []),
                "来源": row.get("时段来源") or "",
            }
            for row in rows
        ],
    }


def build_rows(layers: dict, duration: float | None = None) -> list:
    subjects = extract_subjects(layers.get(LAYER_PRODUCT, ""))
    props = extract_props(layers.get(LAYER_MOTION, ""))
    rows = []
    rows += subjects
    rows += props
    rows += extract_people(
        layers.get(LAYER_SCENE, ""),
        layers.get(LAYER_SHOTS, ""),
        layers.get(LAYER_ASSETS, ""),
    )
    scene_entries = [1 for label, _ in bullets(layers.get(LAYER_ASSETS, ""))
                     if parse_asset_entry(label)[0] == "场景"]
    if len(scene_entries) < 2:                 # 九 资产 列了多个场景时以它为准，不用一 场景 的合并行
        rows += extract_scene(layers.get(LAYER_SCENE, ""))
    # 九 资产 常驻合并：原来只在「三 运动」没给出道具时才读它，于是模型在九层写清楚的
    # 服装/第二个产品/第二个场景全被丢掉（2026-10-31 pop-3 实测：8 条 → 清单 3 行）。
    rows += extract_assets(layers.get(LAYER_ASSETS, ""), rows)
    # 第十层品牌位 → 「品牌视觉（置换位）」（B4）：confirm 开始消费第十层的品牌字/包装字。
    rows += extract_brand_visuals(layers.get(LAYER_CLOSING, ""))
    for index, row in enumerate(rows, start=1):
        row.setdefault("资产图", "")
        row.setdefault("建议", "")
        row.setdefault("用我的描述", "")
        row["idx"] = index
        row["锁什么"] = row.get("锁什么") or LOCK_BY_CATEGORY[row["类别"]]
    # AT-8 B1：出现时段 + 同实体疑似（清单行生成之后立刻解析，n 就是这里的物理行序）
    # AT-10 C1：片长透传下去，给分镜表时间码做整表联合消歧（mm:ss vs ss:cs）
    attach_appear_spans(rows, layers, duration)
    return rows


# ---------------------------------------------------------------- 落盘

def render_checklist(video_id: str, rows: list) -> str:
    lines = [
        f"# 待确认清单 · {video_id}",
        "",
        "> 每行已预填模型的描述。你只需要做三件事之一：",
        "> ① 改「命名」列　② 在「资产图」列填图片路径（多张用英文逗号）　③ 什么都不做 = 按原样使用",
        f"> 改完保存，然后运行：`py -3 pipeline/reverse/render.py --id {video_id} --template lumi-4section`",
        "",
        "| # | 类别 | 模型看到什么 | 命名 | 资产图 | 锁什么 | 建议 | 用我的描述 |",
        "|---|------|-------------|------|--------|--------|------|------------|",
    ]
    for row in rows:
        cells = [
            str(row["idx"]),
            row["类别"],
            row["看到什么"],
            row["命名"],
            row["资产图"],
            row["锁什么"],
            row.get("建议", ""),
            row.get("用我的描述", ""),
        ]
        lines.append("| " + " | ".join(clean_cell(cell) for cell in cells) + " |")
    return "\n".join(lines) + "\n"


def write_text(target: Path, text: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".tmp")
    with open(temp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    os.replace(temp, target)


# ---------------------------------------------------------------- 解析器

def parse_checklist(path) -> list:
    """待确认清单.md → [{idx, 类别, 看到什么, 命名, 资产图, 锁什么}]。

    按 `|` 切列，跳过表头与 `|---|` 分隔行；类别不在四类内抛 ValueError。
    """
    text = Path(path).read_text(encoding="utf-8")
    header = None
    out = []
    ordinal = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if not cells or all(SEPARATOR_CELL_RE.match(cell) for cell in cells if cell):
            continue
        if cells[0].lstrip("#").strip() == "" and "类别" in cells:
            header = {name: index for index, name in enumerate(cells)}
            continue
        if header is None and "类别" in cells and "模型看到什么" in cells:
            header = {name: index for index, name in enumerate(cells)}
            continue
        if len(cells) < 6:
            raise ValueError(f"列数不足 6 列：{stripped}")

        def cell(name, fallback):
            if header and name in header and header[name] < len(cells):
                return cells[header[name]]
            if fallback < len(cells):
                return cells[fallback]
            return ""

        category = cell("类别", 1)
        if category not in CATEGORIES:
            raise ValueError(f"类别「{category}」不在 {CATEGORIES} 之内：{stripped}")
        ordinal += 1
        raw_idx = cell("#", 0)
        try:
            idx = int(re.sub(r"[^0-9]", "", raw_idx) or ordinal)
        except ValueError:
            idx = ordinal
        out.append({
            "idx": idx,
            "类别": category,
            "看到什么": cell("模型看到什么", 2),
            "命名": cell("命名", 3),
            "资产图": cell("资产图", 4),
            "锁什么": cell("锁什么", 5),
            "建议": cell("建议", 6),
            "用我的描述": cell("用我的描述", 7),
        })
    return out


def verify(path: Path) -> int:
    try:
        rows = parse_checklist(path)
    except FileNotFoundError:
        print(f"ERROR 清单不存在：{path}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"ERROR 清单解析失败：{exc}", file=sys.stderr)
        return 4
    if not rows:
        print(f"ERROR 清单里没有数据行：{path}", file=sys.stderr)
        return 4

    counts = {name: 0 for name in CATEGORIES}
    for row in rows:
        counts[row["类别"]] += 1
    print(f"清单：{path}")
    print("计数：" + " / ".join(f"{name} {counts[name]}" for name in CATEGORIES) + f"（合计 {len(rows)} 行）")
    print("")

    hits = 0
    blanks = 0
    for row in rows:
        print(f"[{row['idx']}] {row['类别']}")
        print(f"     看到什么：{row['看到什么']}")
        print(f"     命名：{row['命名'] or '（空，等人工填）'}")
        print(f"     资产图：{row['资产图'] or '（空，等人工填）'}")
        print(f"     建议：{row.get('建议') or '（空）'}")
        print(f"     用我的描述：{row.get('用我的描述') or '（空）'}")
        print(f"     锁什么：{row['锁什么'] or '（空）'}")
        found = [word for word in BANNED_IDENTITY if word in row["看到什么"]]
        if found:
            hits += 1
            print(f"     WARN 身份词：「{'、'.join(found)}」——只写形态，名字交给人工")
        if not row["锁什么"].strip():
            blanks += 1
            print("     WARN 「锁什么」为空")
        if not row["看到什么"].strip():
            blanks += 1
            print("     WARN 「模型看到什么」为空")
    print("")
    print(f"身份词扫描：{hits} 行命中（黑名单 {len(BANNED_IDENTITY)} 词：{'、'.join(BANNED_IDENTITY)}）")
    print(f"空列扫描：{blanks} 处")
    print(f"OK 解析 {len(rows)} 行，类别全部合法")
    return 0


# ---------------------------------------------------------------- 人工改动保全

def merge_human_edits(rows: list, old_path: Path) -> list:
    """重跑时保住人工改过的「命名」「资产图」「用我的描述」三列。

    旧文件里这三列非空就以旧值为准——它们本来就是留给人填的列，
    模型只负责预填；「模型看到什么」「锁什么」「建议」仍按本次拆解重算。

    配对顺序：先按（类别, 模型看到什么）配；配不上再按同类别出现顺序兜底，
    这样即便描述文本被修过（例如清掉句首空分号），人工填写也能接回来。
    """
    if not old_path.is_file():
        return rows
    try:
        old = parse_checklist(old_path)
    except Exception:
        return rows                  # 旧文件坏了就当没有，不能因此写不出清单
    if not old:
        return rows

    def norm(text) -> str:
        return str(text or "").strip().lstrip("；;，,、。· ")

    by_text, by_cat = {}, {}
    for item in old:
        by_text.setdefault((item.get("类别", ""), norm(item.get("看到什么"))), item)
        by_cat.setdefault(item.get("类别", ""), []).append(item)

    used = set()
    for row in rows:
        src = by_text.get((row["类别"], norm(row.get("看到什么"))))
        if src is None:
            for cand in by_cat.get(row["类别"], []):
                if id(cand) not in used:
                    src = cand
                    break
        if src is None:
            continue
        used.add(id(src))
        for column in ("命名", "资产图", "用我的描述"):
            if str(src.get(column) or "").strip():
                row[column] = src[column]
    return rows


# ---------------------------------------------------------------- 入口

def run(source: Path) -> int:
    if not source.is_file():
        print(f"ERROR 拆解文件不存在：{source}", file=sys.stderr)
        return 2
    markdown = source.read_text(encoding="utf-8")
    layers = split_layers(markdown)
    missing = [title for title in REQUIRED_LAYERS if not layers.get(title, "").strip()]
    if missing:
        print(f"ERROR 缺层：{'、'.join(missing)}（清单要 一/二/三/七 四层）", file=sys.stderr)
        return 2

    rows = build_rows(layers, probe_duration(markdown))
    if not rows:
        print("ERROR 一个实体都抽不到，未写清单", file=sys.stderr)
        return 3

    video_id = source.parent.name
    target = source.parent / CHECKLIST_NAME
    rows = merge_human_edits(rows, target)       # 重跑不许覆盖人工填的命名、资产图与描述
    text = render_checklist(video_id, rows)
    leaked = [word for word in BANNED_IDENTITY if word in text]
    write_text(target, text)
    print(f"写入 {target}（{len(rows)} 行）")
    print("计数：" + " / ".join(
        f"{name} {sum(1 for row in rows if row['类别'] == name)}" for name in CATEGORIES
    ))
    spans_path = target.with_name(CHECKLIST_SPANS_NAME)
    write_text(spans_path, json.dumps(span_payload(video_id, rows),
                                     ensure_ascii=False, indent=2) + "\n")
    located = sum(1 for row in rows if row.get("出现时段"))
    twins = "、".join(f"#{row['idx']}↔{'/'.join(f'#{other}' for other in row['同实体疑似'])}"
                     for row in rows
                     if row.get("同实体疑似") and row["idx"] < min(row["同实体疑似"]))
    print(f"写入 {spans_path}（出现时段 {located}/{len(rows)} 行；同实体疑似 {twins or '无'}）")
    if leaked:
        print(f"WARN 身份词漏网：{'、'.join(leaked)}", file=sys.stderr)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="十层拆解 → 待确认置换清单（全预填）+ 清单解析器")
    parser.add_argument("source", nargs="?", help="analysis/reverse/<id>/拆解.md")
    parser.add_argument("--verify", metavar="清单", help="解析 待确认清单.md 并扫身份词")
    args = parser.parse_args(argv)

    if args.verify:
        return verify(Path(args.verify))
    if not args.source:
        parser.print_usage(sys.stderr)
        return 2
    return run(Path(args.source))


if __name__ == "__main__":
    sys.exit(main())
