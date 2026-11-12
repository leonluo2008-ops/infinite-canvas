#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模板驱动渲染器 —— 十层 IR + 模板 → 提示词 + 资产清单。

纯字符串拼接：不调模型、不联网、不读环境变量、不写时间戳。
同一输入两次跑，字节必须相同（tests/render_check.py 会验）。

取数纪律（防「同一内容在多段重复倾倒」）
----------------------------------------
1. 每一层正文先切成**子块**（`标签：内容` / `标签`+子项 / 表格一行一块），子块身份全局唯一。
2. 按模板段序 1→2→…依次消费：某块被一段吃掉即标记已用，后面的段拿不到。
3. 段吃不吃某块，两条同时成立：①该块所在的层被本段「数据来源」声明；
   ②块标签的维度 ∈ 本段维度集（维度由段标题 + 「写什么」文本推出）。
   某维度被某段标题点名独占时（如「声音」被「声音与收束」独占），别的段不吃。
4. 标签推不出维度、或维度对不上任何段的块 → **丢弃**；倒进随便一段算缺陷。
   丢弃清单写进产物末尾「附注」，可核。
5. **分镜行必带图号**：分镜维度（`镜号` / `分镜`）的行，开头按 `@图片N ` 挂上本镜头涉及的
   资产——实体核心名词短语的**后缀**命中即挂（长描述实体名 vs 短主体词的唯一桥），
   一行最多 3 个，已带图号的行不动（幂等）。命中不了就不挂，宁缺勿错。

输入
----
- `analysis/reverse/<id>/拆解.md`        必需，十层固定标题（`## 一 场景` … `## 十 图文与收束`）
- `analysis/reverse/<id>/待确认清单.md`  可选，`| # | 类别 | 模型看到什么 | 命名 | 资产图 | 锁什么 |`
- `templates/<name>.md`                 必需，格式见 `templates/README.md` §3/§4

输出
----
- `analysis/reverse/<id>/提示词-<template>.md`
- `analysis/reverse/<id>/资产清单.md`

退出码
------
0 成功｜2 输入缺（拆解 / 模板）｜3 模板不合规（templates/README.md §5）｜4 拆解缺层
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from confirm import BRAND_VISUAL_CATEGORY  # noqa: E402  「品牌视觉（置换位）」唯一真源在 confirm.py（render_lapian 同例）

EXIT_OK = 0
EXIT_INPUT = 2
EXIT_TEMPLATE = 3
EXIT_LAYER = 4

ROOT = Path(__file__).resolve().parents[2]

# 十层清单（templates/README.md §5 唯一认可的层名）
LAYERS: list[tuple[str, str]] = [
    ("一", "场景"), ("二", "产品"), ("三", "运动"), ("四", "光影"), ("五", "镜头"),
    ("六", "意境"), ("七", "分镜表"), ("八", "声音"), ("九", "资产"), ("十", "图文与收束"),
]
LAYER_BY_NAME = {name: cn for cn, name in LAYERS}
LAYER_BY_CN = {cn: name for cn, name in LAYERS}

# 子块维度表：判断「这块该进哪一段」。
#   label_keys   —— 出现在子块标签里，说明这块属于哪个维度
#   section_keys —— 出现在段标题 / 「写什么」里，说明这一段要哪个维度
# 匹配：取最长命中；等长时按本表顺序（越靠前越优先）。
DIMENSIONS: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = [
    ("分镜", ("镜号", "分镜"),
     ("分镜", "镜号", "镜头数", "场景数")),
    ("声音", ("声音", "台词", "对白", "人声", "旁白", "BGM", "音乐", "音效", "音画",
              "歌声", "唱词", "环境声", "收音", "口播", "配音"),
     ("声音", "台词", "对白", "音乐", "音效", "BGM", "口播")),
    ("镜头", ("机位", "镜头", "景别", "焦段", "焦点", "景深", "构图", "角度", "运镜"),
     ("机位", "镜头", "景别", "焦段", "景深", "构图")),
    ("运动", ("运动主体", "运动轨迹", "轨迹", "方向", "周期", "速度", "出入画", "入画",
              "出画", "模糊", "主体", "动作", "变化", "替换", "更替", "转场"),
     ("运动", "动作", "轨迹", "转场", "变化", "速度")),
    ("产品", ("款式及替换节点", "替换节点", "更换时机", "替换时间", "数量", "规格",
              "款式", "外形", "外观", "标签", "包装", "瓶身"),
     ("产品", "物体", "款式", "数量", "替换")),
    ("场景", ("位置", "底衬", "空间", "背景", "环境", "陈设", "材质", "地面", "墙面", "色调"),
     ("场景", "环境", "陈设", "背景", "位置")),
    ("资产", ("演员", "人物", "角色"),
     ("人物", "资产", "出镜", "演员")),
    ("光", ("亮度变化", "光源", "光线", "光斑", "光影", "阴影", "投影", "亮度", "照度",
            "明暗", "布光", "光"),
     ("光", "明暗", "冷暖", "光影")),
    ("色彩", ("色彩", "颜色", "调色", "配色"),
     ("色彩", "色调", "颜色")),
    ("氛围", ("调性", "氛围", "情绪", "气质", "风格", "节奏感", "参照", "具体手段", "视觉手法"),
     ("氛围", "调性", "情绪", "节奏")),
    ("质感", ("质感", "纹理", "工艺", "清晰度", "写实", "画质"),
     ("质感", "材质", "纹理", "外形")),
    # 第十层「图文与收束」：持久性文字/图形标识 + 片尾收束类型/版式/动效时序。
    # 标签键刻意避开裸「文字」「字幕」——别的层的行文里太常见（产品层「标签文字」会误归）；
    # 只认图文条目的名字（品牌字/水印/角标/图文/收束）。段键只给 图文/品牌/片尾——
    # 「收束/收尾」不能进段键：既有段标题里有「声音与收束」「全程一致性收尾」，会把这个
    # 维度抢走，第十层的可选段就永远排不上（dim 独占按标题先到先得）。
    ("收束", ("图文", "收束", "品牌字", "水印", "角标", "片尾字"),
     ("图文", "品牌", "片尾")),
]

# 清单「类别」→ 资产清单「类型」
KIND_TO_ASSET_TYPE = {
    "主体": "产品图",
    "服装": "服装参考图",
    "人物": "人物参考图",
    "场景": "场景参考图",
    "道具": "道具参考图",
}
# 清单「类别」→ 该实体在拆解里主要落在哪一层（仅用于「命名兜底落位」）
KIND_TO_LAYER_CN = {"主体": "二", "服装": "九", "人物": "九", "场景": "一", "道具": "三"}

# 甲方硬约束：「九 资产」层里出现「手握着产品」才补「手握比例图」。
# 只扫「九 资产」层，不扫五（镜头）——「手持机位」是手持摄影，不是手握产品，扫五层会误触发。
HAND_KEYS = ("手持", "手握", "手拿", "握住", "握着", "举着", "持瓶", "手中", "单手", "双手")

# ---- 分镜行挂图号（资产 ↔ 提示词对齐）--------------------------------------
# 图号是资产和提示词之间唯一的纽带：分镜行不写 `@图片N`，上传时人对不上「这行说的是哪张图」。
# 但实体名是长描述（`熟制海鲜托盘拼盘：必须锁住浓稠红亮酱料包裹度…`），分镜行里只有短词
# （`海鲜拼盘`），整串匹配一条也挂不上。这里把实体名压成「核心名词短语」，再用**从长到短
# 的后缀**去命中分镜行文本：
#     熟制海鲜托盘拼盘 → 海鲜托盘拼盘 → 托盘拼盘 → 盘拼盘 → 拼盘 ✓（命中「海鲜拼盘」）
# 纯字符串，不调模型：命中不了就是命中不了，宁缺勿错——手套全程戴着、布帘是背景，
# 分镜行没提到它们，就不该挂上去当噪声。
CORE_TAIL_RE = re.compile(r"必须锁住.*$")      # 「必须锁住…」是要求，不是名字
PAREN_RE = re.compile(r"[（(][^）)]*[）)]")      # 括号里是补充说明
CORE_SPLIT_RE = re.compile(r"[；;。，,、：:]")    # 核心短语只取第一个分句
# 颜色/材质修饰词（AT-9 B2 残留清扫用；与 replace_uploaded_asset_refs 的 mod 同词表）
COLOR_MATERIAL_RE = re.compile(
    r"(?:[红橙黄绿青蓝紫黑白灰金银粉棕][色]?|荧光|金属|塑料|玻璃|磨砂|亮面|哑光|透明|反光|细长|圆柱)")
UPLOADED_REF_SPLIT_RE = re.compile(r"(@图片\d+)")   # 捕获组：文本段 / 图号 交替
CJK_TAIL_RE = re.compile(r"[一-龥]+$")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
MIN_SUFFIX_CJK = 2                             # 后缀至少 2 个汉字，再短就只剩通用词
MAX_TAGS_PER_LINE = 3                          # 一行最多挂 3 个图号，多了是噪声不是信息
GENERIC_SUFFIXES = frozenset({
    "人物", "东西", "主体", "产品", "背景", "画面", "物体", "物品", "场景",
    "道具", "颜色", "材质", "轮廓", "表面", "形状", "比例", "位置", "部分", "区域",
})

# 分镜「运动主体」列的值 → 资产。短词（`人物`/`手机`/`包装`）命中不了长资产名
# （`细长圆柱形瓶身配银色圆柱盖` 这类描述），只靠后缀匹配会一行图号都挂不上；兜底必须认这张表。
# 每条形如 (主体值里出现的词, 认资产的方式, 参数)：
#     ("kind", "人物")  资产类别是「人物」
#     ("name", "手机")  资产名（或核心短语）里含「手机」
# 只写**类别词**，不写具体产品名/品牌：红线 10「pipeline/ 不得出现产品名」由
# tests/generic_smoke.py 守着。核心产品按类别（`产品`/`瓶身` → 主体）认，换项目不改代码。
# 场景类（地铁 / 白棚 / 通道）故意不登记：分镜写「地铁」不等于这一镜要挂场景参考图，
# 场景图是给生成端定环境的，挂到分镜行上只会让人误以为这镜要用它。
SUBJECT_ALIASES: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (("人物", "模特", "演员", "女孩", "女性", "男生", "男人", "女人"), "kind", "人物"),
    (("手机",), "name", "手机"),
    (("产品", "瓶身"), "kind", "主体"),
    (("包装", "纸盒", "盒"), "name", "盒"),
)
# `运动主体：人物` → 只留「人物」；列名本身带「主体」二字，不剥掉就会被兜底误命中
SUBJECT_LABEL_RE = re.compile(r"^\s*(?:运动主体|主体)\s*[：:]\s*")

# 「九层 六（意境）」——本项目唯一认的取数语法；`十层 十（图文与收束）` 等价（两者都认）
LAYER_REF_RE = re.compile(r"(?:九层|十层)\s*([一二三四五六七八九十])\s*[（(]\s*([^）)]+?)\s*[）)]")
# `### 第 1 段：整体风格` / 清单式 `### 第 1 行：✅ 片长`
SEC_RE = re.compile(r"^###\s+第\s*(\d+)\s*(段|行)\s*[：:]\s*(.+?)\s*$")
# `### 可选段 A：前置总述（2/17）`
OPT_RE = re.compile(r"^###\s+可选段\s*([A-Za-z0-9]+)\s*[：:]\s*(.+?)\s*$")
PART_RE = re.compile(r"\*\*Part\.(\d+)\*\*\s*[：:]\s*「(.+?)」")
FIELD_RE = re.compile(
    r"^-\s*(写什么|数据来源|口吻|范例证据|原文依据|命名变体|渲染建议|反面证据|分镜列"
    r"|实体指认|时段格式|连写|字样置换|原句输出|声音段|台词渲染)\s*[：:]\s*(.*)$"
)
LAYER_HEAD_RE = re.compile(r"^##\s*([一二三四五六七八九十])\s+(\S.*?)\s*$")
DUP_DECIMAL_RE = re.compile(r"(?<![\d.])(\d+\.\d{2,})(?![\d.])")


class Fail(Exception):
    """带退出码的失败。"""

    def __init__(self, code: int, msg: str) -> None:
        super().__init__(msg)
        self.code = code
        self.msg = msg


# ---------------------------------------------------------------- 基础 IO


def die(err: Fail) -> "None":
    print(f"[FAIL exit={err.code}] {err.msg}", file=sys.stderr)
    raise SystemExit(err.code)


def read_text(path: Path, code: int, what: str) -> str:
    if not path.is_file():
        raise Fail(code, f"{what} 不存在：{path.name}")
    return path.read_text(encoding="utf-8-sig")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


# ---------------------------------------------------------------- 模板解析


def split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---"):
        raise Fail(EXIT_TEMPLATE, "模板缺 frontmatter（首行应为 ---）")
    end = text.find("\n---", 3)
    if end < 0:
        raise Fail(EXIT_TEMPLATE, "frontmatter 未闭合")
    head = text[3:end].strip("\n")
    body = text[end + 4:]
    fm: dict[str, str] = {}
    for line in head.splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        fm[k.strip()] = v.strip()
    return fm, body


class Section:
    def __init__(self, n: int, unit: str, title: str, optional: bool) -> None:
        self.n = n
        self.unit = unit          # 段 / 行 / 可选段
        self.title = title        # 逐字保留，渲染时直接做标题
        self.optional = optional
        self.fields: dict[str, str] = {}
        self.parts: list[tuple[str, str]] = []   # 范例证据：(Part.N, 引文)

    def field(self, key: str) -> str:
        return (self.fields.get(key) or "").strip()

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<Section {self.n}{self.unit} {self.title}>"


def parse_sections(body: str) -> tuple[list[Section], list[Section]]:
    """按 README §4 切段：只认 `### 第 N 段：`/`### 第 N 行：`/`### 可选段 `，不依赖 ## 层级。"""
    main: list[Section] = []
    opt: list[Section] = []
    cur: Section | None = None
    cur_field: str | None = None
    for raw in body.splitlines():
        m = SEC_RE.match(raw)
        if m:
            cur = Section(int(m.group(1)), m.group(2), m.group(3), optional=False)
            main.append(cur)
            cur_field = None
            continue
        m = OPT_RE.match(raw)
        if m:
            cur = Section(-1, "可选段", m.group(2), optional=True)
            cur.fields["_tag"] = m.group(1)
            opt.append(cur)
            cur_field = None
            continue
        if raw.startswith("### ") or raw.startswith("## "):
            cur, cur_field = None, None
            continue
        if cur is None:
            continue
        m = FIELD_RE.match(raw)
        if m:
            cur_field = m.group(1)
            cur.fields[cur_field] = m.group(2).strip()
            continue
        if cur_field and (raw.startswith("  ") or raw.startswith("\t")):
            extra = raw.strip()
            if cur_field in ("数据来源", "口吻") and (extra.startswith("- ") or extra.startswith("⚠")):
                continue          # 字段下的补充注解，不进产物附注
            if extra:
                sep = "\n" if cur_field in ("范例证据", "原文依据") else " "
                cur.fields[cur_field] = (cur.fields[cur_field] + sep + extra).strip()
            continue
        if not raw.strip():
            continue
    for sec in main + opt:
        for part_no, quote in PART_RE.findall(sec.field("范例证据")):
            sec.parts.append((part_no, quote))
    return main, opt


def layer_refs(sec: Section) -> list[tuple[str, str]]:
    """从「数据来源」抽 `九层 X（名）`，按声明顺序返回 [(中文数字, 层名)]。"""
    src = sec.field("数据来源").replace("`", "")
    refs: list[tuple[str, str]] = []
    for cn, name in LAYER_REF_RE.findall(src):
        if LAYER_BY_CN.get(cn) != name:
            raise Fail(
                EXIT_TEMPLATE,
                f"段 {sec.n}「{sec.title}」数据来源写的是「九层 {cn}（{name}）」，"
                f"但层清单里 {cn} 是「{LAYER_BY_CN.get(cn)}」——层号与层名对不上",
            )
        if (cn, name) not in refs:
            refs.append((cn, name))
    if not refs:
        raise Fail(
            EXIT_TEMPLATE,
            f"段 {sec.n}「{sec.title}」数据来源里没有 `九层 <中文数字>（<层名>）` 引用；"
            "渲染器只认这一种取数语法",
        )
    return refs


# ---------------------------------------------------------------- §5 校验

def norm_for_grep(s: str) -> str:
    """去 Markdown 修饰与全部空白后比较——模板原文里有 `**摄像机**：`、`： ` 这类写法。"""
    s = s.replace("**", "").replace("`", "").replace("*", "")
    return re.sub(r"\s+", "", s)


class ExampleCorpus:
    def __init__(self, root: Path) -> None:
        self.text = ""
        ex = root / "library" / "examples"
        if ex.is_dir():
            for p in sorted(ex.glob("*.md")):
                self.text += p.read_text(encoding="utf-8", errors="replace") + "\n"
        self.norm = norm_for_grep(self.text)

    def hit(self, quote: str) -> bool:
        return norm_for_grep(quote) in self.norm


def resolve_doc_ref(ref: str, root: Path) -> tuple[Path | None, str]:
    """从一句话里找 `docs/xxx.md` + `§N`（章节号）。找不到返回 (None, "")。"""
    m = re.search(r"docs/[^\s（）()，,；;「」]+?\.md", ref)
    if not m:
        return None, ""
    path = root / m.group(0)
    tail = ref[m.end():]
    sec = re.search(r"§\s*(\d+)", tail) or re.search(r"§\s*(\d+)", ref)
    return path, (sec.group(1) if sec else "")


def validate_template(fm: dict[str, str], main: list[Section], opt: list[Section],
                      path: Path, root: Path, corpus: ExampleCorpus,
                      wanted_optional: list[str]) -> None:
    """templates/README.md §5：逐条检查，失败即失败（exit 3），不静默补空。"""
    for key in ("name", "来源", "段落数"):
        if not fm.get(key):
            raise Fail(EXIT_TEMPLATE, f"模板 frontmatter 缺必填字段 `{key}`")
    if fm["name"] != path.stem:
        raise Fail(EXIT_TEMPLATE, f"frontmatter name=「{fm['name']}」与文件名「{path.stem}」不一致")
    if fm.get("废弃", "").lower() == "true":
        raise Fail(EXIT_TEMPLATE, "模板标了 `废弃: true`，渲染器拒绝选中")

    try:
        declared = int(fm[f"段落数"])
    except ValueError:
        raise Fail(EXIT_TEMPLATE, f"`段落数` 不是整数：{fm['段落数']}") from None
    if declared != len(main):
        raise Fail(EXIT_TEMPLATE,
                   f"frontmatter 段落数={declared}，正文实际 {len(main)} 段——对不上就是改了正文没改头")

    # 段号：默认从 1 连续；`首段编号: 0` 时才允许 0 起
    start = 0 if fm.get("首段编号", "").strip() == "0" else 1
    got = [s.n for s in main]
    if got != list(range(start, start + len(main))):
        raise Fail(EXIT_TEMPLATE,
                   f"段号必须从 {start} 起连续递增，实际是 {got}")
    units = {s.unit for s in main}
    if len(units) > 1:
        raise Fail(EXIT_TEMPLATE, f"同一模板混用「段」与「行」两种编号：{sorted(units)}")

    evidence_free = fm.get("可信度", "").startswith("无")

    for sec in main + opt:
        tag = f"{sec.n}{sec.unit}「{sec.title}」"
        if sec.optional and sec.fields.get("_tag", "") not in wanted_optional:
            continue  # 没打开的可选段不校验——它不参与本次渲染
        for key in ("写什么", "数据来源", "口吻"):
            if not sec.field(key):
                raise Fail(EXIT_TEMPLATE, f"{tag} 缺「{key}」——四要素必须齐全，不静默补空")
        layer_refs(sec)  # 层号/层名对不上的，这里就炸

        if sec.parts:
            if len(sec.parts) < 3:
                raise Fail(EXIT_TEMPLATE, f"{tag} 范例证据只有 {len(sec.parts)} 条，规范要求 ≥ 3 条")
            for part_no, quote in sec.parts:
                if not corpus.hit(quote):
                    raise Fail(EXIT_TEMPLATE,
                               f"{tag} 范例证据 Part.{part_no} 在 library/examples/ 里 0 命中："
                               f"「{quote[:40]}」——引文对不上就是引用造假")
            continue

        orig = sec.field("原文依据")
        if orig and orig.strip() not in ("无", "无。"):
            doc, chapter = resolve_doc_ref(orig, root)
            if doc is None:
                doc, chapter = resolve_doc_ref(fm.get("来源", ""), root)
            if doc is None:
                raise Fail(EXIT_TEMPLATE, f"{tag} 原文依据没写明 docs/ 抄录件与章节")
            if not doc.is_file():
                raise Fail(EXIT_TEMPLATE, f"{tag} 原文依据指向的 {doc.name} 不存在")
            if chapter:
                text = doc.read_text(encoding="utf-8", errors="replace")
                if not re.search(rf"^#+\s*{chapter}[\.、\s]", text, re.M):
                    raise Fail(EXIT_TEMPLATE, f"{tag} 原文依据指向的 {doc.name} §{chapter} 定位不到")
            continue

        if evidence_free:
            # 模板明标「可信度：无…」：承认自己没有甲方依据，不伪造。
            # 这类模板默认不被选中，只有 --template 显式点名才走到这里。
            continue
        raise Fail(EXIT_TEMPLATE,
                   f"{tag} 既没有 ≥3 条范例证据，也没有可定位的原文依据——规范里这叫「视同造假」")


# ---------------------------------------------------------------- 拆解解析


def parse_layers(text: str) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """按 `## 一 场景` 切十层。返回 ({中文数字: 正文}, [(数字, 层名)])。"""
    layers: dict[str, str] = {}
    order: list[tuple[str, str]] = []
    cur: str | None = None
    buf: list[str] = []
    for raw in text.splitlines():
        m = LAYER_HEAD_RE.match(raw)
        if m:
            if cur is not None:
                layers[cur] = "\n".join(buf).strip()
            cn, name = m.group(1), m.group(2)
            if LAYER_BY_CN.get(cn) != name:
                raise Fail(EXIT_LAYER, f"拆解里「## {cn} {name}」的层名与十层清单不符")
            cur = cn
            buf = []
            order.append((cn, name))
            continue
        if raw.startswith("## "):   # 非十层标题，层正文到此为止
            if cur is not None:
                layers[cur] = "\n".join(buf).strip()
                cur, buf = None, []
            continue
        if cur is not None:
            buf.append(raw)
    if cur is not None:
        layers[cur] = "\n".join(buf).strip()
    return layers, order


def _is_table_row(s: str) -> bool:
    """表格行的判定（含不带前置竖线的写法）。

    只认两种：行首竖线；或者「纯数字开头 + 竖线」的编号行 / 纯分隔行。
    **不许**用「行里出现『镜号』二字」当判据——夹具里 `* 镜号01：…` 这种项目符号
    会被当成表头，把整张表拼坏（2026-11-01 实测踩过）。
    """
    if s.startswith("|"):
        return True
    return bool(re.match(r"^\s*(\d+|:?-{2,})\s*\|", s))


def _is_table_head(s: str) -> bool:
    """判断一行是不是表格的开头。

    2026-11-01 实测：视觉模型有时把分镜表写成 `1 | 00:00-00:02 | 人物 | …`（不带前置竖线），
    旧判定只认行首 `|`，整张表被当成散文丢掉——分镜段直接没了。所以放宽成：
    行首竖线，或者「≥3 个竖线 + 带表头词 + 不是列表项/标题行」。
    """
    if s.startswith("|"):
        return True
    if re.match(r"^\s*[*+\-]\s|^\s*#", s):
        return False
    if re.match(r"^\s*\d{1,2}\s*\|", s):
        return True                       # 模型省掉表头，直接从「1 | 00:00-…」开始
    if s.count("|") < 3:
        return False
    return "镜号" in s


DEFAULT_SHOT_COLUMNS = (
    "镜号", "起秒-止秒", "运动主体", "运动形态", "速度", "画面内容",
    "光影变化", "转场方式", "站位", "朝向", "视线", "镜头运动", "景别",
)


def _looks_like_data_row(cells: list[str]) -> bool:
    """第一行是不是数据行（镜号是纯数字 / 时间码）。

    2026-11-01 赛马实测：同一条模型对 25 秒的片子省掉了表头，直接写
    `1 | 00:00-01.97 | 海鲜拼盘 | …`。旧逻辑把第一行当表头，结果是第一镜被吃掉、
    后面每一镜的列名全错。这里改判：第一行像数据，就按契约的 13 个列名补一个表头。
    """
    if not cells:
        return False
    first = cells[0].strip()
    if re.fullmatch(r"\d{1,2}", first):
        return True
    if re.fullmatch(r"镜\s*号\s*\d{1,3}", first):
        return True                     # `镜号01 | 00:00–00:04 | …` 这种带镜号前缀的项目符号行
    if re.search(r"\d{1,2}:\d{2}", first):
        return True
    # 第二格就是时间码/时间区间：表头不会是这样（表头第二格写的是「起秒-止秒」）
    return any(re.search(r"\d{1,2}:\d{2}", c) for c in cells[1:2])


def _with_header(rows: list[list[str]]) -> list[list[str]]:
    if rows and _looks_like_data_row(rows[0]):
        width = max(len(r) for r in rows)
        header = list(DEFAULT_SHOT_COLUMNS[:width])
        header += [""] * (width - len(header))
        return [header] + rows
    return rows


def render_table(block: list[str]) -> list[str]:
    """Markdown 表格 → 一行一个镜头（范例库 17 条里 0 处表格，提示词里也不该有）。"""
    rows: list[list[str]] = []
    for ln in block:
        s = ln.strip()
        if re.fullmatch(r"\|[\s:|-]+\|", s):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if cells and all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
            continue                            # 分隔行（`| :--- | :--- |` 或 `--- | ---`）
        rows.append(cells)
    if not rows:
        return []
    rows = _with_header(rows)
    header = rows[0]
    out: list[str] = []
    for cells in rows[1:]:
        bits: list[str] = []
        for i, cell in enumerate(cells):
            head = header[i] if i < len(header) else ""
            if head in ("镜号", "#", "序号"):
                bits.append(f"镜号 {cell}")
            elif i == 1 or head in ("起秒-止秒", "时间", "时间码"):
                bits.append(cell)
            else:
                bits.append(f"{head}：{cell}" if head else cell)
        out.append("｜".join(b for b in bits if b))
    return out


# ---- `分镜列` 指令（叙事体模板唯一可行的路径）--------------------------------
# 表格行是「镜号 1｜00:00-00:02｜运动主体：人物｜…」，叙事体要的是「00:00-00:02，全景，人物 @图片1，…」——
# 一句一条、列名不回显、图号落在句内。模板某段写 `- 分镜列：时段,景别,运动主体,…` 时走这条路；
# 没写这条指令的模板完全走原路径（`render_table` 逐列倒出），旧三套模板行为不变。
# 列名必须能在拆解的表头里找到：找不到就 exit 3 报「模板要的列 X 拆解里没有」，不静默跳过。
SHOT_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "镜号": ("镜号", "#", "序号"),
    "时段": ("起秒-止秒", "时间码", "时间"),
}
# 这几列在某些镜头里没有内容（没有人物的镜头，站位/朝向/视线写「—（无人物）」）。
# 这类值不进句子——「—（无人物）」念出来不是话。
SHOT_SKIP_VALUES = ("—", "-", "——", "无人物", "无", "N/A", "n/a")


def shot_table(layer_body: str) -> tuple[list[str], list[list[str]]]:
    """取一层正文里的第一张表 → (表头, 数据行)。没有表返回 ([], [])。

    与块解析共用 `_is_table_head`：模型偶尔不写前置竖线（`1 | 00:00-00:02 | 人物 | …`），
    只认行首 `|` 会把整张表判成没有表。
    """
    rows: list[list[str]] = []
    for raw in layer_body.splitlines():
        s = raw.strip()
        # 列表项写法（`* 镜号01 | 00:00–00:04 | …`）：模型实测常把分镜表写成项目符号行。
        # 不剥符号的话整张表被判成「没有表格」，叙事体模板直接 exit 3（2026-11-03 实测）。
        s = re.sub(r"^(?:[*+•·]|[-–—](?=\s)|\d{1,3}[.)](?=\s))\s*", "", s)
        if not s:
            continue
        if not _is_table_head(s) and not (rows and s.count("|") >= 2):
            if rows:
                break                       # 表格结束
            continue
        if re.fullmatch(r"\|[\s:|-]+\|", s):
            continue                        # 分隔行
        cells = [c.strip() for c in s.strip("|").split("|")]
        if cells and all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
            continue                        # 分隔行（无前置竖线写法）
        rows.append(cells)
    if not rows:
        return [], []
    rows = _with_header(rows)
    return rows[0], rows[1:]


def shot_columns(sec: Section) -> list[str]:
    """段里写了 `- 分镜列：…` 就返回按声明顺序的列名；没写返回 []（= 走原路径）。"""
    raw = sec.field("分镜列").strip().strip("`")
    if not raw:
        return []
    return [c.strip().strip("`") for c in re.split(r"[,，、]", raw) if c.strip()]


def resolve_shot_column(name: str, header: list[str], sec: Section) -> int | None:
    """列名 → 表头下标。先认全名，再认别名（时段→起秒-止秒）；都不认就 exit 3。

    列名末尾带 `?` 表示**可选列**（如 `情绪/表演?`）：旧拆解没有这一列时跳过，不判缺列；
    新拆解有就照常取。没带 `?` 的列仍然缺一列都不行。
    """
    optional = name.endswith("?")
    bare = name.rstrip("?") if optional else name
    for cand in (bare,) + SHOT_COLUMN_ALIASES.get(bare, ()):
        if cand in header:
            return header.index(cand)
    if optional:
        return None
    raise Fail(
        EXIT_TEMPLATE,
        f"段 {sec.n}「{sec.title}」的 `分镜列` 要「{bare}」这一列，拆解的分镜表头里没有"
        f"（现有列：{'、'.join(header) or '无'}）——缺列不许静默跳过",
    )


def assemble_shot_line(cells: list[str], header: list[str], cols: list[str],
                       sec: Section, ents: list["Entity"]) -> str:
    """一行表格 → 叙事体的一句：按 `分镜列` 声明的顺序取值，用「，」连接。

    - 空值 / `—` / `无人物` 的列直接跳过（没有人物的镜头不硬凑「—（无人物）」）；
    - `运动主体` 那一列取到值时，把该镜的 `@图片N` 紧跟在主体值后面（不是行首前缀）；
    - 一列都取不到就返回空串，调用方不输出空行。
    """
    speed = ""
    if "速度" in header:
        sv = cells[header.index("速度")].strip() if header.index("速度") < len(cells) else ""
        if sv and sv not in SHOT_SKIP_VALUES and not sv.startswith("—"):
            speed = sanitize(strip_core_residue(
                replace_uploaded_asset_refs(apply_naming(sv, ents), ents), ents))
    bits: list[str] = []
    for name in cols:
        idx = resolve_shot_column(name, header, sec)
        if idx is None:
            continue
        bare = name.rstrip("?")
        value = cells[idx].strip() if idx < len(cells) else ""
        if not value or value in SHOT_SKIP_VALUES or value.startswith("—"):
            continue
        if bare == "情绪/表演" and value.startswith("无可见表情"):
            continue                      # 备注，不是画面内容
        text = sanitize(strip_core_residue(
            replace_uploaded_asset_refs(apply_naming(value, ents), ents), ents))
        if not text:
            continue
        if bare in ("运动主体", "主体"):
            have = set(re.findall(r"@图片(\d+)", text))
            text += "".join(f" @图片{no}" for no in subject_tag_numbers(value, ents)
                            if str(no) not in have)
        if bare == "运动形态" and speed:
            text += "，" + speed          # 速度是这一镜动作的收束，不单独占一个槽位
        bits.append(text)
    return "，".join(bits)


def _tc_to_sec(tok: str) -> float:
    """`00:04` → 4.0；`2.0` → 2.0。冒号按 分:秒（分镜表不带小时）。"""
    tok = tok.strip()
    if ":" in tok:
        mm, ss = tok.split(":", 1)
        return int(mm) * 60 + float(ss or 0)
    return float(tok or 0)


def _fmt_sec(v: float) -> str:
    s = f"{v:.1f}".rstrip("0").rstrip(".")
    return s if s else "0"


def seconds_timecodes(text: str) -> str:
    """行首时间码区间 → 生成平台惯用的「N-M秒」。

    认三种写法（都在实测拆解里出现过）：`00:00–00:04`、`00:00-2.0`、`2.0-4.1`。
    只动行首那一组区间；正文里其他数字（比例、数量）不碰。
    """
    m = re.match(r"^\s*([\d]{1,2}(?::\d{1,2})?(?:\.\d+)?)\s*[–—\-~至]\s*"
                 r"(\d{1,2}(?::\d{1,2})?(?:\.\d+)?)\s*(?=[，,、\s]|$)", text)
    if not m:
        return text
    a, b = _tc_to_sec(m.group(1)), _tc_to_sec(m.group(2))
    return f"{_fmt_sec(a)}-{_fmt_sec(b)}秒" + text[m.end():]


KIND_SHORT_LABEL = {"主体": "产品", "人物": "人物", "场景": "场景", "道具": "道具", "服装": "服装"}


def entity_roll_call(ents: list["Entity"]) -> str:
    """实体指认句：`全程使用参考图中的 @图片1 产品、@图片2 人物。`

    每个实体只给一个 2–6 字的典型说明（命名优先，没有就退到类别词），
    外形 / 颜色 / 材质一律不写——细节交给图（`.hermes.md` 第 17 条）。

    AT-11 M5（合并组=纯图号引用）：同一图号被 ≥2 个实体认领（判据与
    strip_core_residue 的 claimed 口径一致）= 这些行是同一件东西，已被同一张图
    整体替换——组内图号只出一个 bit，且说明词清空成纯 `@图片N`。组内任何成员的
    命名都是旧物残留（方案 §六.2「零旧描述残留」）：auto-0024 里 #1 命名
    「有粉色标签款」是参考片旧描述，若写进 roll call 就会绕过 strip_core_residue
    的清扫、自检②绑图行残留直接 FAIL。非组行行为完全不变（命名优先、退类别词）。
    """
    claimed: dict[int, int] = {}
    for e in ents:
        if e.no:
            claimed[e.no] = claimed.get(e.no, 0) + 1
    bits: list[str] = []
    seen_nos: set[int] = set()
    for ent in sorted((e for e in ents if e.no), key=lambda e: e.no):
        if ent.no in seen_nos:      # 组内成员共用同一图号：只出一个 bit
            continue
        seen_nos.add(ent.no)
        if claimed.get(ent.no, 0) >= 2:
            bits.append(f"@图片{ent.no}")
            continue
        desc = (ent.name or KIND_SHORT_LABEL.get(ent.kind, "")).strip()
        bits.append(f"@图片{ent.no}" + (f" {desc}" if desc else ""))
    if not bits:
        return ""
    return "全程使用参考图中的" + "、".join(bits) + "。"


def split_label(item: str) -> tuple[str, str]:
    """`数量与规格：共4款…` → ("数量与规格", "数量与规格：共4款…")；无冒号标签为空。"""
    m = re.match(r"^([^：:｜]{1,14})[：:]\s*(.*)$", item)
    if not m:
        return "", item
    head, tail = m.group(1), m.group(2)
    if head and head[-1].isdigit() and tail[:1].isdigit():
        return "", item          # `00:00` 这种时间码，不是标签
    label = head.strip()
    return label, f"{label}：{tail.strip()}"


def _norm_pipes(s: str) -> str:
    """半角竖线统一成全角——只归一**结构分隔**，引号里的原字照抄不动。

    模型输出的分镜行有两种写法，必须都认：
      `* 01｜00:00–00:04｜…`（全角，历史格式）
      `1 | 00:00-00:01 | 海鲜拼盘 | …`（半角加空格）
    不归一的话第二种的 `label_of()` 取不到标签，整层分镜会被丢光。
    但引号里是 OCR 原字（第十层图文会有「部分可辨：EAU | DE …」这种半角竖线原样），
    原字照抄含标点是铁律——改写它就是替模型改供词（2026-11-04 路 A 审计 S3），
    与 render_lapian 的原字保留同口径。引号不配对时整行照旧归一（宁可保守不猜）。
    """
    parts = re.split(r"(「[^」]*」|『[^』]*』)", s)
    return "".join(part if index % 2 else re.sub(r"\s*\|\s*", "｜", part)
                   for index, part in enumerate(parts))


def label_of(item: str) -> tuple[str, str]:
    """先按冒号取标签；分镜行（`镜号01｜00:00–00:04｜…`）按竖线取首段。"""
    label, line = split_label(item)
    if not label and "｜" in item:
        label = item.split("｜")[0].strip()
    # `* 01 | 00:00-00:03 | …` 这类没有「镜号」前缀的分镜行：纯数字标签补上「镜号」，
    # 否则 dim_of_label 认不出它是分镜维度，整层会被当「对不上任何段」丢掉。
    if re.fullmatch(r"\d{1,3}", label.strip()):
        label = "镜号" + label.strip()
    return label, line


def _rescue_label(label: str, line: str) -> str:
    """标签里认不出维度时，拿正文开头再认一次。

    2026-10-30 实测：模型有时把镜头层写成 `- 00:00：微俯拍，固定特写，景深极浅…`，
    标签变成了时间码，dim_of_label 认不出，整层被判成「对不上任何段」而丢掉，
    提示词里的「镜头与摄影」直接变成「无」。正文里其实写着景深、焦点，用正文开头再认一次就能救回来。
    label 只用于归维度与诊断，不印进提示词正文，所以替换它不影响产物措辞。
    """
    if dim_of_label(label):
        return label
    probe = re.sub(r"\s+", "", line)[:24]
    if probe and dim_of_label(probe):
        return probe
    return label


def dim_of_label(label: str) -> str:
    """子块标签 → 维度。取最长命中；等长按 DIMENSIONS 顺序。"""
    best, best_len = "", 0
    for dim, keys, _sec_keys in DIMENSIONS:
        for k in keys:
            if k in label and len(k) > best_len:
                best, best_len = dim, len(k)
    return best


def dims_of_section(sec: Section) -> tuple[set[str], set[str]]:
    """段要哪些维度：返回 (全部维度, 由标题独占的维度)。"""
    title, what = sec.title, sec.field("写什么")
    want: set[str] = set()
    strong: set[str] = set()
    for dim, _keys, sec_keys in DIMENSIONS:
        for k in sec_keys:
            if k in title:
                strong.add(dim)
                want.add(dim)
                break
        for k in sec_keys:
            if k in what:
                want.add(dim)
                break
    return want, strong


def _looks_like_sentence(label: str) -> bool:
    """标签是不是一整句话。

    `_rescue_label` 会把长标签截到 24 字；「首帧：中东或拉丁外观年轻女性…」截断后仍含
    「外观」，于是被 dim_of_label 判成「产品」，整行被换成资产名（首帧只剩 @图片1 + 资产名）。
    判据：冒号后面是一段带逗号的长文本 → 它是句子，不是字段名。
    """
    if not re.search(r"[：:]", label):
        return False
    head, tail = re.split(r"[：:]", label, maxsplit=1)
    if head.strip() not in ("首帧", "开场", "起始帧", "第0秒", "结尾", "末帧"):
        return False
    return len(tail) > 12 or "，" in tail


LAYER_DIM = {
    "场景": "场景", "产品": "产品", "运动": "运动", "光影": "光",
    "镜头": "镜头", "意境": "氛围", "分镜表": "分镜", "声音": "声音", "资产": "资产",
    "图文与收束": "收束",
}


class Block:
    """一层正文里的一个子块：一个标签 + 若干行（标签行 + 子项）。

    `layer` 是块的**唯一来源标识**，一律存中文层号（一…十）：调用方给层号或层名都行，
    这里归一成层号。2026-11-04 路 A 审计 B2 的根因就是这里曾存层名「图文与收束」，
    下游置换门却比较层号「十」，永假——包装原字整个漏换。
    """

    __slots__ = ("layer", "label", "lines", "dim")

    def __init__(self, layer: str, label: str, first: str) -> None:
        if layer not in LAYER_BY_CN and layer in LAYER_BY_NAME:
            layer = LAYER_BY_NAME[layer]      # 层名（图文与收束）→ 层号（十），统一标识
        self.layer = layer
        self.label = label
        self.lines = [first]
        self.dim = "" if _looks_like_sentence(label) else dim_of_label(label)
        if layer == "十":
            # B1（2026-11-04 路 A 审计）：第十层按**来源归属**路由——这一层的条目全是
            # 图文，维度就是收束，不靠条目名字里少数字词猜。猜名字的实测后果：
            # 字幕/标语/角标猜不中被维度过滤整条丢弃；「包装字」被「包装」误归产品
            # 维度，跑进产品段还躲过字样置换门，包装原字直接进正文。
            self.dim = "收束"
        elif not self.dim and label in (layer, LAYER_BY_CN.get(layer, "")):
            # 整层写成一段散文（没有 `标签：` 的小标题）时，块名就是层名/层号；
            # 它属于这一层的维度，不能因为没标签就被 want 过滤丢掉。
            self.dim = LAYER_DIM.get(LAYER_BY_CN.get(layer, layer), "")

    def add(self, line: str) -> None:
        self.lines.append(line)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<Block {self.layer}·{self.label or '(无标签)'}>"


def parse_blocks(body: str, layer: str) -> list[Block]:
    """把一层正文切成子块：顶层项目符号一条一块，表格一行一块，子项并进上一块。

    `layer` 传层号（十）或层名（图文与收束）都认，Block 里统一存层号。
    """
    blocks: list[Block] = []
    lines = body.splitlines()
    i = 0
    cur: Block | None = None
    while i < len(lines):
        raw = lines[i]
        s = raw.strip()
        i += 1
        if not s or s.startswith("<!--"):
            continue
        indent = len(raw) - len(raw.lstrip())

        if _is_table_head(s):                      # 表格：一行一块
            block = [s] + [x.strip() for x in lines[i:] if _is_table_row(x.strip())]
            i += len(block) - 1
            for row in render_table(block):
                label = row.split("｜")[0] if row.startswith("镜号") else row.split("：")[0]
                blocks.append(Block(layer, label, row))
            cur = None
            continue

        m = re.match(r"^[*-]\s+(.*)$", s)
        if m and indent == 0:                     # 顶层项目符号 = 新子块
            item = re.sub(r"^\*\*(.+?)\*\*\s*[：:]\s*", r"\1：", m.group(1)).replace("**", "")
            label, line = label_of(_norm_pipes(item))
            label = _rescue_label(label, line)
            cur = Block(layer, label, line)
            blocks.append(cur)
            continue

        m = re.match(r"^(\d+)\.\s+(.*)$", s)
        if m and indent == 0:                     # 顶层编号 = 新子块
            label, line = label_of(_norm_pipes(m.group(2).replace("**", "")))
            cur = Block(layer, label, f"{m.group(1)}. {line}")
            blocks.append(cur)
            continue

        # `1 | 00:00-00:01 | 海鲜拼盘 | …`：不带项目符号、不带点的纯数字竖线行，也是分镜
        m = re.match(r"^(\d{1,3})\s*\|\s*(.+)$", s)
        if m and indent == 0:
            label, line = label_of(_norm_pipes(s))
            cur = Block(layer, label, line)
            blocks.append(cur)
            continue

        # 层首无标签的散文行（模型有时不用项目符号）：按层名归维度，否则整层丢掉
        if cur is None:
            cur = Block(layer, layer, s)
            blocks.append(cur)
            continue

        if cur is not None:                       # 子项 / 续行，并进上一块
            m = re.match(r"^(\d+)\.\s+(.*)$", s)
            if m:
                cur.add(f"  {m.group(1)}. {m.group(2).replace('**', '')}")
                continue
            m = re.match(r"^[*-]\s+(.*)$", s)
            if m:
                cur.add("  · " + m.group(1).replace("**", ""))
                continue
            cur.add("  " + s.replace("**", ""))
            continue

        label, line = label_of(s.replace("**", ""))   # 无项目符号的散行
        blocks.append(Block(layer, label, line))
    return blocks


def sanitize(line: str) -> str:
    """产物卫生：去加粗标记、多位小数只留一位、收拾多余空格。

    2026-11-02 所有者裁定：「高级感」「氛围感」这类词是可以出现的，而且能有效引导模型。
    原先这里会静默删掉它们，等于替甲方改稿——已拆掉。
    """
    line = line.replace("**", "")
    line = DUP_DECIMAL_RE.sub(lambda m: f"{float(m.group(1)):.1f}", line)
    line = re.sub(r"的{2,}", "的", line)
    line = re.sub(r"[ \t]{2,}", " ", line)
    return line.rstrip()


# 第十层「图文与收束」照抄的参考片字样 → 客户占位（借创意不复刻品牌；模板段 `- 字样置换：是` 启用）。
# 置换两态（2026-11-04 路 A 审计 B3）：
#   品牌位（品牌字/包装字）→「客户品牌字样」；
#   文案位（字幕/标语/角标/水印/促销文案）→「客户文案（定稿后填入）」——参考片文案
#   不照搬，也不伪装成品牌字。「部分可辨：」是模型的不确定性标记，两个占位都不吃它，
#   原样保留（进人工复检）。引号内不设字数上限：61 个字的「原字」原样外泄也是泄漏（B2）。
BRAND_PLACEHOLDER = "「客户品牌字样」"
COPY_PLACEHOLDER = "「客户文案（定稿后填入）」"
BRAND_GLYPH_RE = re.compile(r"「[^」\n]+」")
UNCERTAIN_GLYPH_MARK = "部分可辨"
# 字样身份表：引号前面紧挨着的词（行内）或整块标签（块级）决定这条字样是品牌位还是文案位。
# 先 brand 后 copy：`品牌字` 得先于 `品牌` 命中，不被更短的键抢先。
GLYPH_KIND_KEYS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("brand", ("品牌字", "包装字", "品牌", "商标", "标识字", "logo")),
    ("copy", ("字幕", "标语", "角标", "水印", "促销", "花字", "贴纸", "文案", "口播字")),
)


def glyph_kind_of_label(label: str) -> str:
    """块级身份：`包装字（出现于…` → brand；`字幕（出现于…` → copy；认不出 → 空。

    认不出就不猜——置换是受控行为，身份不明的引号留给行内上下文与人工复核，
    不无差别套品牌占位（B3 的教训就是把所有引号都当品牌）。
    """
    probe = (label or "").lower()
    for kind, keys in GLYPH_KIND_KEYS:
        if any(k in probe for k in keys):
            return kind
    return ""


def _quote_context_kind(line: str, pos: int) -> str:
    """行内身份：引号前紧挨着的词。`字幕「全场 5 折」` → copy；`品牌「SPARK!」` → brand。

    收束段一句话里同时写「字幕在下、品牌在上」时，靠它把同一段里的两种位分开换。
    """
    before = re.sub(r"[：:＝=、，,（(）)\s——\\-]+$", "", line[:pos])
    for kind, keys in GLYPH_KIND_KEYS:
        if any(before.endswith(k) for k in keys):
            return kind
    return ""


def _replace_glyph_pass(line: str, block_kind: str, pass_kind: str, placeholder: str) -> str:
    """只换身份命中的引号字样；其他身份、「部分可辨」与占位本身原样保留。

    单一身份一遍过（品牌函数不吃文案引号，文案函数不吃品牌引号），行内上下文优先
    于块级身份；身份认不出的引号不动。幂等：占位字面再过一遍不变形；
    连排占位收成一个，不让一句话里出现五遍占位。
    """
    changed = False

    def _sub(match: re.Match) -> str:
        nonlocal changed
        text = match.group(0)
        if UNCERTAIN_GLYPH_MARK in text:
            return text                      # 不确定性标记保留，不许抹
        kind = _quote_context_kind(line, match.start()) or block_kind
        if kind != pass_kind:
            return text
        changed = True
        return placeholder

    out = BRAND_GLYPH_RE.sub(_sub, line)
    if not changed:
        return line
    for ph in (BRAND_PLACEHOLDER, COPY_PLACEHOLDER):
        out = re.sub(rf"({re.escape(ph)})[与和、,，]\s*\1", r"\1", out)
        out = re.sub(rf"({re.escape(ph)})(?:\s*\1)+", r"\1", out)
    return out


def _replace_brand_glyphs(line: str, block_kind: str = "") -> str:
    """品牌位置换：brand 身份的引号字样 → 「客户品牌字样」，其余一律不动。"""
    return _replace_glyph_pass(line, block_kind, "brand", BRAND_PLACEHOLDER)


def _replace_copy_glyphs(line: str, block_kind: str = "") -> str:
    """文案位置换：copy 身份的引号字样 → 「客户文案（定稿后填入）」，其余一律不动。"""
    return _replace_glyph_pass(line, block_kind, "copy", COPY_PLACEHOLDER)


def glyph_dictionary(blocks_by_layer: dict[str, list["Block"]]) -> dict[str, str]:
    """第十层**实际登记**的字样 → 客户占位的置换字典（跨层兜底用）。

    字段级置换只开在第十层/收束段（引号内、身份命中才换），真机验收暴露两个它
    管不到的边界：① 图文条目「位置」等非「文字内容」字段里裸嵌的原品牌字
    （`位置=顶部ZING!正下方`）；② 产品/场景等其他层描述里照抄的已登记字样
    （`印有…大字「ZING!」`），置换门不开，原字照进正文，生成端会照抄。
    这里把第十层登记的字样提成字典，对渲染正文**全部段**做字典替换：
    品牌/包装类 → 品牌占位，字幕/标语等文案类 → 文案占位。字典只含登记过的
    字样，不做全局猜测，误伤面可控；「部分可辨」条目是模型的不确定性标记，
    不进字典（原样保留进人工复检，与字段级同口径）；身份认不出的条目也不进
    （认不出就不猜）。同字样重复登记取首次身份。
    """
    dictionary: dict[str, str] = {}
    for block in blocks_by_layer.get("十", []):
        kind = glyph_kind_of_label(block.label)
        if not kind:
            continue
        placeholder = BRAND_PLACEHOLDER if kind == "brand" else COPY_PLACEHOLDER
        for line in block.lines:
            for match in BRAND_GLYPH_RE.finditer(line):
                text = match.group(0)
                if UNCERTAIN_GLYPH_MARK in text:
                    continue
                word = text[1:-1].strip()          # 去掉引号取原字
                if word:
                    dictionary.setdefault(word, placeholder)
    # 长字样先换，防止短键抢先吃掉长键的一部分
    return dict(sorted(dictionary.items(), key=lambda kv: len(kv[0]), reverse=True))


def replace_registered_glyphs(line: str, dictionary: dict[str, str]) -> str:
    """字典式置换：正文行里裸现或引号内的已登记字样 → 登记身份对应的占位。

    泄漏不挑字段（位置/字体/版式里裸嵌的原字一样换），也不挑层；唯一豁免是
    「部分可辨」引号——先整体摘下、换完再放回，不确定性标记不被占位抹掉。
    幂等：占位不含任何原字，重复过不变形。
    """
    if not dictionary:
        return line
    stashed: list[str] = []

    def _stash(match: re.Match) -> str:
        text = match.group(0)
        if UNCERTAIN_GLYPH_MARK not in text:
            return text
        stashed.append(text)
        return f"\x00{len(stashed) - 1}\x00"

    out = BRAND_GLYPH_RE.sub(_stash, line)

    def _whole(match: re.Match) -> str:
        # 引号里恰好就是一个登记字样 → 连引号一起换成占位，
        # 不然内层替换会留下「「客户品牌字样」」这种叠引号怪相。
        return dictionary.get(match.group(0)[1:-1].strip(), match.group(0))

    out = BRAND_GLYPH_RE.sub(_whole, out)
    for word, placeholder in dictionary.items():
        if word in out:
            out = out.replace(word, placeholder)
    for index, text in enumerate(stashed):
        out = out.replace(f"\x00{index}\x00", text)
    return out


def decode_user_desc(value: str) -> str:
    """清单第 8 列的安全编码；只还原本功能写入的控制字符。"""
    return (value.replace("&#124;", "|").replace("&#10;", chr(10))
            .replace("&#13;", chr(13)).replace("&#38;", "&"))


def sanitize_with_user_desc(line: str, ents: list[Entity]) -> str:
    """清洗参考片文本，但把用户手填描述整段保护起来。"""
    protected: list[tuple[str, str]] = []
    for index, ent in enumerate(ents):
        if not ent.my_desc:
            continue
        label = ent.label()
        if label not in line:
            continue
        token = f"__USER_DESC_{index}__"
        line = line.replace(label, token)
        protected.append((token, label))
    line = sanitize(line)
    for token, value in protected:
        line = line.replace(token, value)
    return line


ASSET_REFERENCE_TERMS = (
    "长方柱体扁瓶", "瓶身", "瓶体", "瓶盖", "小物体", "产品",
)


def item_payload(text: str) -> str:
    """行的正文部分（去掉「标签：」前缀），用来比相似度。"""
    _head, sep, tail = text.partition("：")
    return (tail if sep else text).strip()


def find_duplicate_asset(item: dict, pool: list[dict]) -> dict | None:
    """在一段里找「同一件东西被登记了两次」的另一行。

    只跨层合并：`二 产品·附件` 与 `九 资产·实体道具二` 说的都是那个束口袋，属于重复登记；
    同一层里两条长得像的行（两个不同产品都叫「粉色小瓶」）不合并——宁多勿删。
    跨层阈值 0.45：实测同一束口袋的两行相似度 0.51，而它和「人物」那一行只有 0.11。
    """
    import difflib
    body = item_payload(item["text"])
    if not body:
        return None
    for other in pool:
        if other["drop"] or other["dim"] not in ("产品", "资产"):
            continue
        twin = item_payload(other["text"])
        if not twin:
            continue
        if body == twin:
            return other
        cross_layer = other["layer"] != item["layer"]
        threshold = 0.45 if cross_layer else 0.92
        if body in twin or twin in body:
            if cross_layer or len(body) / max(len(twin), 1) > 0.8:
                return other
            continue
        if difflib.SequenceMatcher(None, body, twin).ratio() >= threshold:
            return other
    return None


def replace_uploaded_asset_refs(line: str, ents: list[Entity], dimension: str = "",
                                layer: str = "") -> str:
    """用户传图/填描述后，清掉参考片对同一主体的产品称呼与形态描述。

    2026-10-31 所有者拍板：正文里不许再写产品名，一律用图片资产（`@图片N`）指代。
    原来这里把每个「产品」替换成 11 个字的全名，一句话里出现两次就炸开
    （「焦点始终跟随某产品滚珠接触面或某产品标识」，一句话里同一个名字出现两次），
    而且「小滚珠瓶」里的「滚珠」也被当成锚点，句子直接读不通。
    名字只在「产品参考」那一段定义一次，正文用图号 + 最多一个短说明。
    """
    products = [e for e in ents if e.kind == "主体" and (e.has_asset_image or e.my_desc.strip())]
    if not products:
        return line
    protected: list[tuple[str, str]] = []
    for index, ent in enumerate(products):
        label = ent.label()
        if label in line:
            token = f"__ASSET_REF_{index}__"
            line = line.replace(label, token)
            protected.append((token, label))
    # 产品层本身就是参考片完整产品外观，整行改为绑定资产标签，避免残留参考颜色/材质。
    # 但只有真的在讲这件产品的行才改：附件（束口袋）、时间节点这些行不许被产品描述顶掉，
    # 否则同一张图会在这一节里重复出现两三次（2026-10-31 所有者截图里的那两行）。
    # layer 传层号（Block 统一标识后的「二」），兼容旧调用方直接传层名「产品」。
    if dimension == "产品" and layer in ("二", "产品"):
        speaks_of_product = "产品" in line or any(pat and pat in line for pat, _rep in products[0].patterns())
        if speaks_of_product:
            prefix, sep, _tail = re.split(r"([：:])", line, maxsplit=1) if re.search(r"[：:]", line) else ("", "", line)
            if sep:
                line = prefix + sep + "；".join(e.label() for e in products)
    else:
        # 正文用图号指代，允许一个短说明（品类/角色）；详细外观不写，模型照着图生成。
        # 替换要连同紧邻的颜色/材质形容词一起吃掉——只换「瓶身」会留下「紫色@图片1」，
        # 参考片的紫色/银色会和上传图打架（2026-11-02 所有者实测）。
        replacement = f"@图片{products[0].no}" if products[0].no else (products[0].name or "产品")
        # 「颜色/材质 + 瓶(身/体/盖)」整段换成图号。只吃修饰词，不吃动词——
        # 「快速掠过瓶身」必须保留「快速掠过」。
        mod = (r"(?:(?:[红橙黄绿青蓝紫黑白灰金银粉棕][色]?|荧光|金属|塑料|玻璃|磨砂|亮面|哑光|透明|反光|"
               r"细长|圆柱))*")
        line = re.sub(mod + r"瓶(?!子)(?:身|体|盖)?", replacement, line)
        line = re.sub("|".join(re.escape(term) for term in ASSET_REFERENCE_TERMS),
                      replacement, line)
        # 产品外观词（颜色/材质）是参考片的，不是上传图的：图号前面不允许再挂这些词。
        line = re.sub(r"(?:[红橙黄绿青蓝紫黑白灰金银粉棕][色]?|金属|塑料|玻璃|磨砂|亮面|哑光|透明|反光)+"
                      r"[一-龥]{0,2}(@图片\d+)", lambda m: m.group(1), line)
        # 人物：有图就把「年轻混血女性 / 女主角 / 模特」整段换成图号，不留下参考片的国籍与发色
        for ent in ents:
            if ent.kind == "人物" and ent.no:
                # 图号 + 最短说明：让模型一眼知道这张图是人物；详细长相/服装不写
                rep = f"人物@图片{ent.no}"
                line = re.sub(r"(?<!无)(?:(?:年轻|混血|拉丁|中东|外国|中国|/))*"
                              r"(?:女性|女主角|女主|模特|人物)", rep, line)
        # 同号连写收成一个：「@图片1与@图片1」→「@图片1」
        line = re.sub(rf"({re.escape(replacement)})\s*[与和、,，]\s*{re.escape(replacement)}",
                      replacement, line)
        line = re.sub(rf"({re.escape(replacement)})(?:\s+{re.escape(replacement)})+",
                      replacement, line)
        line = re.sub(r"(?:用户上传资产)(?:[^，。；|]*用户上传资产)+", "用户上传资产", line)
    for token, value in protected:
        line = line.replace(token, value)
    return line


def entity_noun_core(ent: "Entity") -> str:
    """实体名（AT-9 B2 残留修复）：命名优先；否则「模型看到什么」去字母/数字前缀后取第一个分句，只留汉字。

    #1 `双耳奖杯造型香水小物体。表面为…` → `双耳奖杯造型香水小物体`
    #11 `C：双耳奖杯形态玻璃香水小物体，…` → `双耳奖杯形态玻璃香水小物体`（去 `C：` 前缀）
    """
    base = ent.name or ent.seen
    base = re.sub(r"^[A-Za-zＡ-Ｚ0-9]{1,3}\s*[：:]\s*", "", base)
    base = CORE_TAIL_RE.sub("", base)
    base = PAREN_RE.sub("", base)
    noun = CORE_SPLIT_RE.split(base)[0]
    return re.sub(r"[^一-龥]", "", noun)


# ---------------------------------------------------------------- 残留词表收词口径（AT-22 E1 R3）
#
# docs/方案-AT22-E1-残留词表收词规则-20261112.md §六 / docs/审计收敛-AT21-AT22-双路裁决-20261112.md。
# 误报根因只有一条：**取词**把动作/方位/视角词当成了原物特征词——「从右下角入画的第一人称右手」
# 被切成「右下角/角入画/下角入/第一人」，于是用户合法保留的构图描述被判成旧物残留（66 任务实测
# 命中 232 条里 19 条误报，全落五类源结构）。
#
# R3 在收词侧三步收窄，一律**删词不跳行**（混合名里的实物部分照旧活着，纯动作名自然空表）：
#   ① 核心词先删动作/方位/视角词，再切 2-3 字组；
#   ② 通用占位名（我们的产品…）不建表——它对正文任何「…的产品」句都成立，没有判定价值；
#   ③ 匹配侧跳过实体自身标签行（`<命名>（<形态>）`），落在 selfcheck._check_residue。
# 黑名单只增不减：新增一条词必须跑一次 66 任务语料回归（automation/probe_e1_residue.py --assert）。
ACTION_VIEW_WORDS = (
    "入画", "出画", "视角", "第一人称", "第二人称", "人称", "视点", "镜头", "运镜", "机位",
    "前景", "背景", "右手", "左手", "双手", "手部", "手持", "握持", "特写", "近景",
    "中景", "远景", "全景", "俯视", "仰视", "平视", "右下角", "左下角", "右上角",
    "左上角", "右下", "左下", "右上", "左上", "画面", "边缘", "转身", "托举", "推近",
    "拉远", "平移", "跟拍", "环绕", "俯拍", "仰拍", "空镜",
)
GENERIC_ENTITY_NAMES = frozenset({
    "我们的产品", "我们的道具", "我的产品", "产品", "道具", "人物", "场景",
    "你上传的素材", "用户上传资产", "你的产品", "你的道具", "你的角色", "你的场景",
})
# 长词先删：「第一人称」先于「人称」，否则先删「人称」会留下「第一」这种无意义残片。
_ACTION_VIEW_LONG_FIRST = tuple(sorted(ACTION_VIEW_WORDS, key=len, reverse=True))


def strip_action_words(core: str) -> str:
    """删核心词里的动作/方位/视角词（长词优先）。删词不是跳行：删完剩下的实物词仍参与判定。"""
    for word in _ACTION_VIEW_LONG_FIRST:
        core = core.replace(word, "")
    return core


def residue_core(ent: "Entity") -> str:
    """绑图行残留的**唯一取词口径**（判定与清扫同源）：核心词去动作视角词；通用占位名 → 空串（不建表）。"""
    core = strip_action_words(entity_noun_core(ent))
    return "" if core in GENERIC_ENTITY_NAMES else core


def residue_words(ent: "Entity") -> list[str]:
    """残留词表：core ∪ 2-3 字组，只收 ≥3 字（2 字组是同品类/同色词，必误红——见 AT-9 B2）。"""
    core = residue_core(ent)
    if not core:
        return []
    words = sorted({core} | set(_noun_grams(core)), key=len, reverse=True)
    return [w for w in words if len(w) >= 3]


def _noun_grams(noun: str) -> frozenset:
    """实体名的 2-3 字组（特征词组）：残留清扫的可匹配词集。"""
    out: set[str] = set()
    for n in (2, 3):
        out.update(noun[i:i + n] for i in range(len(noun) - n + 1))
    return frozenset(out)


def _strip_modifier_run(run: str, grams: frozenset) -> str:
    """从右往左吃掉紧邻图号的「修饰词+实体名」链：颜色材质词或实体名 2-3 字组才吃，别的（动词/别实体词）即停。

    `取出黑色奖杯香水@图片1` 的 run=`取出黑色奖杯香水`：香水✓→奖杯✓→黑色(颜色)✓→取出✗停
    → 保留 `取出`，吃掉 `黑色奖杯香水`（L1 P129 残留的确切形态）。
    """
    i = len(run)
    while i > 0:
        for n in (3, 2):
            if i - n < 0:
                continue
            tok = run[i - n:i]
            if tok in grams or COLOR_MATERIAL_RE.fullmatch(tok):
                i -= n
                break
        else:
            break
    return run[:i]


def strip_core_residue(line: str, ents: list["Entity"]) -> str:
    """绑图行残留清扫（AT-9 B2，判据=L1 P129 场景修复后零残留）。

    已传图行(@图片N)左侧**紧邻**的原物核心词——「黑色奖杯香水」挂在 @图片1 前——按
    实体名 2-3 字组 + 颜色材质词从右往左吃掉；未传图的行一字不动（借创意参考描述本就保留）。
    只认实体名词组与颜色词，不加模型调用；图号自身与「素材：@图片N」这类行首/标点邻接不受影响。

    AT-11 合并组扩展（§六.2 零旧描述残留）：同一图号被**多个实体**共用（组内 N 行一图号）
    = 这些行是同一件东西——成员实体的核心词组在本行**任何位置**都是旧物残留（既有左邻
    清扫吃不到分镜行中段的细节词，如「…慢动作，银色金属圆柱盖特写，…」）。仅对「同号
    多实体」的行启用整段清扫；单行绑图行为一字不变（零回归面）。
    """
    uploaded: dict[int, frozenset] = {}
    claimed: dict[int, int] = {}
    for e in ents:
        if e.kind.startswith("品牌视觉"):
            # AT-16 B1b：品牌位有图号后其「命名」是客户字样（品牌文字/客户品牌字样），
            # 不是原物核心词——按它取词组会把正文里的中性词当旧物残留扫掉。
            continue
        if e.no and e.has_asset_image:
            # AT-22 E1 R3：与自检②共用 residue_core（动作/方位/视角词不进表）——否则清扫器会把
            # 「自右下角入画」这类合法构图词削掉（§1.2 实测）。claimed 计数照旧含每行，组语义不变。
            uploaded[e.no] = (uploaded.get(e.no, frozenset())
                              | _noun_grams(residue_core(e)))
            claimed[e.no] = claimed.get(e.no, 0) + 1
    if not uploaded:
        return line
    # AT-11：同号被 ≥2 个实体认领 = 合并组；整段清扫只认这批图号的词组（不限行内有无
    # 图号——分镜单元格组装层先于挂图号，成员旧物细节词就落在无 token 的文本里）。
    shared = {no: grams for no, grams in uploaded.items() if claimed[no] >= 2}
    segs = UPLOADED_REF_SPLIT_RE.split(line)
    out: list[str] = []
    for i, seg in enumerate(segs):
        if i % 2 == 1:                          # 捕获组 = 「@图片N」整段
            grams = uploaded.get(int(seg[3:]))
            if grams and out:
                m = CJK_TAIL_RE.search(out[-1])
                if m:
                    out[-1] = out[-1][:m.start()] + _strip_modifier_run(m.group(0), grams)
            out.append(seg)
        elif shared and seg:
            out.append(_drop_shared_runs(seg, shared))
        else:
            out.append(seg)
    return "".join(out)


def _drop_shared_runs(text: str, shared: dict[int, frozenset]) -> str:
    """合并组整段清扫（AT-11）：CJK 串**以合并成员词组开头** → 该串是旧物细节短语
    （「银色金属圆柱盖特写」这类逗号界定的细节描述），整段去掉并顺手吃掉紧邻的一个逗号。
    只认「串首命中 ≥3 字词组」且串长 ≤30（细节短语形态），不做全文子串删除（不过度清扫）。"""
    grams = frozenset().union(*shared.values()) if shared else frozenset()
    ordered = sorted((g for g in grams if len(g) >= 3), key=len, reverse=True)
    parts = re.split(r"([一-龥]+)", text)
    for i in range(1, len(parts), 2):           # 捕获组 = CJK 串
        run = parts[i]
        if len(run) <= 30 and any(run.startswith(g) for g in ordered):
            parts[i] = ""
            if i + 1 < len(parts):
                parts[i + 1] = re.sub(r"^，", "", parts[i + 1], count=1)
            else:
                parts[i - 1] = re.sub(r"，$", "", parts[i - 1], count=1)
    return "".join(parts)


def header_facts(disc: str) -> dict[str, str]:
    """从拆解头注释读全片参数（时长）；拆解层里没有的字段不编。"""
    facts: dict[str, str] = {}
    m = re.search(r"时长[:：]\s*([\d.]+)\s*s", disc)
    if m:
        v = float(m.group(1))
        facts["时长"] = str(round(v)) if abs(v - round(v)) <= 0.5 else f"{v:.1f}"
    m = re.search(r"音轨[:：]\s*(\S+)", disc)
    if m:
        facts["音轨"] = m.group(1)
    # 规格行（ffprobe 量的，不是模型编的）：`> 规格：1080×1920 · 9:16 竖屏 · 24.00 秒 · 30.0 fps · 有音轨`
    m = re.search(r"^>\s*规格[:：]\s*(.+)$", disc, re.MULTILINE)
    if m:
        parts = [p.strip() for p in re.split(r"[·|]", m.group(1)) if p.strip()]
        for part in parts:
            if re.fullmatch(r"\d+\s*[×x]\s*\d+", part):
                facts["尺寸"] = part.replace(" ", "")
            elif re.fullmatch(r"\d+:(\d+)(\s+\S+)?", part) or re.fullmatch(r"\d+:\d+(\s+\S+)?", part):
                facts["画幅"] = part
            elif part.endswith("fps"):
                facts["帧率"] = part
    # 尺寸是机器量的真值，画幅一律由它重算：老拆解里可能写着「135:236」这种
    # 生成平台上没有的比例（1080×1888 的像素比），不能照抄进提示词。
    if facts.get("尺寸"):
        dim = re.fullmatch(r"(\d+)\s*[×x]\s*(\d+)", facts["尺寸"])
        if dim:
            from aspect import canonical_aspect

            facts["画幅"] = canonical_aspect(int(dim.group(1)), int(dim.group(2)))
    # 老任务（拆解生成于「规格行」上线之前）没有这一行：拿头部注释里的视频路径现量一次。
    # 不调模型、不改拆解，只补渲染要用的画幅数字。
    if not facts.get("画幅"):
        m = re.search(r"视频[:：]\s*([^|]+?)\s*(?:\||$)", disc, re.MULTILINE)
        if m:
            facts.update(_probe_aspect(m.group(1).strip()))
    return facts


def _probe_aspect(video_ref: str) -> dict[str, str]:
    """用 ffprobe 现量一个视频的尺寸/画幅/帧率；失败就返回空字典（不打断渲染）。"""
    import subprocess

    path = Path(video_ref)
    if not path.is_absolute():
        path = ROOT / path
    if not path.is_file():
        return {}
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,avg_frame_rate,r_frame_rate",
             "-of", "json", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
        )
        if proc.returncode != 0:
            return {}
        streams = (json.loads(proc.stdout or "{}").get("streams") or [])
        if not streams:
            return {}
        width = int(streams[0].get("width") or 0)
        height = int(streams[0].get("height") or 0)
        fps = 0.0
        for key in ("avg_frame_rate", "r_frame_rate"):
            raw = str(streams[0].get(key) or "")
            if "/" in raw:
                num, _, den = raw.partition("/")
                try:
                    if float(den or 0):
                        fps = round(float(num) / float(den), 2)
                        break
                except (TypeError, ValueError):
                    pass
        out: dict[str, str] = {}
        if width and height:
            from aspect import canonical_aspect

            out["尺寸"] = f"{width}×{height}"
            # 只给视频模型真能选的画幅（16:9 / 9:16 / 1:1 / 4:3 / 3:4），取最近的一种。
            # 1080×1888 这种非标准比例以前会写成「135:236」，生成端没有这个选项。
            out["画幅"] = canonical_aspect(width, height)
        if fps:
            out["帧率"] = f"{fps} fps"
        return out
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}


def fill_title(title: str, n_shots: int, span: str) -> str:
    """段标题里模板自带的示例数字要换成这条片子的真值。

    2026-10-31 所有者实测：模板 `employee-checklist.md` 的标题写的是「✅ 分镜（N个镜头，0–10 秒）」，
    渲染器「标题逐字取自模板」原样输出，于是 23 秒、8 个镜头的片子被标成「0–10 秒」。
    这里把「N个镜头 / □个镜头」换成真实镜头数，把「0–10 秒」这类示例区间换成真实跨度。
    """
    out = title
    if n_shots:
        out = re.sub(r"[Nn□Xx]\s*个镜头", f"{n_shots}个镜头", out)
    if span:
        out = re.sub(r"\d{1,2}:\d{2}\s*[–\-—~]\s*\d{1,2}:\d{2}", span, out)
        out = re.sub(r"\d+\s*[–\-—~]\s*\d+\s*秒", span, out)
    return out


def shot_range(blocks: list[Block]) -> tuple[int, str]:
    """从「七 分镜表」的块里数镜头数与时间区间。"""
    shots = [b for b in blocks if b.dim == "分镜"]
    codes: list[str] = []
    for b in shots:
        codes.extend(re.findall(r"\d{2}:\d{2}(?:\.\d+)?", b.lines[0]))
    if not codes:
        return 0, ""
    return len(shots), f"{codes[0]}–{codes[-1]}"


# ---------------------------------------------------------------- 待确认清单


class Entity:
    def __init__(self, kind: str, seen: str, name: str, image: str, lock: str,
                 no: int = 0, suggest: str = "", my_desc: str = "", row_no: int = 0) -> None:
        self.kind = kind
        # 清单里「模型看到什么」常以 `；` 起头（模型分行吐描述留下的分隔符）。
        # 不剥掉，命名空时就会渲染成 `@图片1 ；熟制海鲜大餐`——一个孤零零的分号。
        self.seen = re.sub(r"^[；;，,、。：:\s]+", "", seen).strip()
        self.name = name.strip()
        self.image = image.strip()
        self.lock = lock.strip()
        self.suggest = suggest.strip()
        self.my_desc = my_desc
        self.row_no = row_no or no    # 待确认清单的「#」列（人工看的行号，稳定不变）
        self.no = no                  # 提示词里的 @图片N：只给已备图的资产按顺序发号 的 N
        self.hits = 0

    @property
    def has_asset_image(self) -> bool:
        """资产图列非空即表示用户已经绑定图；实际落盘图由 UI 写回同一列。"""
        return bool(self.image)

    @property
    def shape(self) -> str:
        """按描述优先级取括号内容；用户描述必须整段保留。"""
        if self.my_desc.strip():
            return self.my_desc
        if self.has_asset_image:
            return "形态以附图为准"
        head = self.seen.split("；")[0].strip()
        if head == self.seen.strip():
            head = self.seen.split("，")[0].strip()
        return head.rstrip("。；")

    def label(self) -> str:
        """`@图片N <命名>（<描述>）`；上传图但无命名时也不再泄漏参考片描述。"""
        head = f"@图片{self.no} " if self.no else ""
        if not self.name and not (self.has_asset_image or self.my_desc.strip()):
            return head + self.seen if head else self.seen
        head = f"@图片{self.no} " if self.no else ""
        # 上传了图但没命名：用类别说话（你的产品/你的道具/你的角色/你的场景），
        # 别让「用户上传资产」这个名字和图注里的「形态以附图为准」重复两遍。
        name = self.name or {
            "主体": "你的产品", "道具": "你的道具", "人物": "你的角色", "场景": "你的场景",
        }.get(self.kind, "你上传的素材")
        return f"{head}{name}（{self.shape}）"

    def patterns(self) -> list[tuple[str, str]]:
        if not self.name and not (self.has_asset_image or self.my_desc.strip()):
            return []
        head = f"@图片{self.no} " if self.no else ""
        pats = [(self.seen, self.label())]
        segs = [s.strip() for s in self.seen.split("；") if s.strip()]
        if len(segs) > 1:
            pats.append((segs[-1], f"{head}{self.name or '用户上传资产'}"))
        return pats

    def core_phrase(self) -> str:
        """实体名的「核心名词短语」——挂分镜行用的锚点。

        命名非空时取命名；命名空时取「模型看到什么」，砍掉 `：` 之后的锁图要求和括号备注，
        只留第一个分句：
            熟制海鲜托盘拼盘：必须锁住浓稠红亮酱料包裹度… → 熟制海鲜托盘拼盘
            黑色柄厨房剪刀：必须锁住黑色环形握柄…          → 黑色柄厨房剪刀
        """
        base = self.name or self.seen
        base = CORE_TAIL_RE.sub("", base)
        base = PAREN_RE.sub("", base)
        base = CORE_SPLIT_RE.split(base)[0]
        return re.sub(r"\s+", "", base).strip("、…·-")

    def tag_suffixes(self) -> list[str]:
        """核心短语的全部后缀，从长到短（含全名）。短于 2 个汉字、或本身就是通用词的丢掉。"""
        core = self.core_phrase()
        out: list[str] = []
        for i in range(len(core)):
            suf = core[i:]
            if len(CJK_RE.findall(suf)) < MIN_SUFFIX_CJK:
                continue          # `款` 这种通用后缀，挂上去只会把整段分镜糊满图号
            if suf in GENERIC_SUFFIXES:
                continue          # `人物` `产品` `背景`：每行都有，挂了等于没挂
            out.append(suf)
        return out


def image_urls(image: str) -> list[str]:
    """「资产图」列 → URL 列表（多图英文/中文逗号分隔，顺序即绑定顺序）。"""
    return [u.strip() for u in re.split(r"[,，]", image or "") if u.strip()]


def parse_checklist(path: Path) -> list[Entity]:
    if not path.is_file():
        return []
    rows: list[Entity] = []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        s = raw.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) < 6 or not re.fullmatch(r"\d+", cells[0]):
            continue
        if cells[1].startswith("品牌视觉"):
            # 「品牌视觉（置换位）」（B4/T6-G）：收进实体列表、**免「建议」条件**直接进资产表
            # ——真机对照（pop2-source-2）里该行「建议」列被意图回填清空过，按建议值过滤
            # 会把置换位整行丢掉（t6r-pop1 建议=必须换收得进、pop2 建议=空收不进）。
            # AT-16 B1b：资产图列**不再置空**——品牌图走「有效参考」进正文引用/发号/提交清单/
            # 资产清单（五点同源，见 effective_units）；未绑图时 no=0，照旧不占号不猜图。
            # 「命名/用我的描述」照读；「建议」也照读（有图号后与普通行同口径）。
            rows.append(Entity(cells[1], cells[2], cells[3], cells[4], cells[5],
                               no=0,
                               suggest=cells[6] if len(cells) > 6 else "",
                               my_desc=decode_user_desc(cells[7]) if len(cells) > 7 else "",
                               row_no=int(cells[0])))
            continue
        rows.append(Entity(cells[1], cells[2], cells[3], cells[4], cells[5],
                           no=0,
                           suggest=cells[6] if len(cells) > 6 else "",
                           my_desc=decode_user_desc(cells[7]) if len(cells) > 7 else "",
                           row_no=int(cells[0])))
    return rows


# AT-12 N1：「按原片行」值白名单（与 automation.replace.DECISION_VALUES 同源；pipeline 不 import automation）
ORIGIN_DECISIONS = frozenset({"keep_origin", "dismissed"})

def assign_numbers(ents: list[Entity], groups: list[list[int]] | None = None,
                   decisions: dict[int, str] | None = None) -> list[Entity]:
    """给「备了图」的资产发 @图片N——AT-11 B1 起**组感知**：行号→组/单体→唯一图号。

    · 组内行共用一个图号（组=一个发号单位）；组外不同行共用 URL 保持**独立图号**
      （禁全局 URL 去重偷改语义）。
    · decisions（『行决策覆盖』）里「按原片行」（keep_origin / AT-12 dismissed）在发号**之前**胜出
      （§3.4）：已有图/命名/自填描述的**有效视图**被屏蔽（该行按原片描述渲染、无 @图片N），原值留在
      清单/覆盖表里供 restore——这里只改内存副本，不动盘面。
    · **AT-16 B1b**：品牌视觉行不再豁免——绑了图就与普通行同口径占号（唯一口径见 effective_units）；
      未绑图 → no=0 不占号。
    · 不传 groups/decisions 时行为与旧版逐行发号完全一致（旧调用方零破坏）。
    """
    units = [sorted({int(x) for x in g}) for g in (groups or [])]
    ko_rows = {int(n) for n, d in (decisions or {}).items() if d in ORIGIN_DECISIONS}
    seq = 0
    seen: dict[tuple, int] = {}
    for ent in ents:
        ent.no = 0
        if ent.row_no in ko_rows:
            ent.name = ""       # 「按原片」屏蔽命名的有效视图（照抄原描述文字）
            ent.my_desc = ""    # 自填描述同屏蔽（§3.4-2）；盘面原值不动
            continue
        if not ent.image:
            continue
        unit = next((("g", gi) for gi, g in enumerate(units) if ent.row_no in g),
                    ("s", ent.row_no))
        if unit not in seen:
            seq += 1
            seen[unit] = seq
        ent.no = seen[unit]
    return ents


def effective_units(ents: list[Entity], groups: list[list[int]] | None = None,
                    decisions: dict[int, str] | None = None) -> list[dict]:
    """**五点同源装配**（AT-16 B1b）：行→图号→提交清单→确认快照→资产清单的唯一口径。

    同一份有效映射（清单当前「资产图」绑定 + 组 + 行决策）装配成按图号升序的发号单位：
        {"no": 图号, "row_no": 主行号, "rows": [行号…], "urls": [...], "url": 末张绑定,
         "entity": Entity}
    渲染侧读 ents.no（render_assets / entity_roll_call），提交侧读本函数
    （automation.replace.effective_entries）——两端不再各有一套合同。
    「不静默丢图」=保护**当前有效绑定**：清空/覆盖/dismiss 后旧 URL 已不在清单 → 自动出局；
    restore 后重新入列（不是无差别保留历史图）。
    """
    ents = assign_numbers(ents, groups, decisions)
    out: list[dict] = []
    index: dict[int, dict] = {}
    for ent in ents:
        if not ent.no:
            continue
        item = index.get(ent.no)
        if item is None:
            item = {"no": ent.no, "row_no": ent.row_no, "rows": [ent.row_no],
                    "urls": image_urls(ent.image), "entity": ent}
            index[ent.no] = item
            out.append(item)
        else:
            item["rows"].append(ent.row_no)
            item["urls"] += image_urls(ent.image)
    out.sort(key=lambda x: x["no"])
    for item in out:
        item["url"] = item["urls"][-1] if item["urls"] else ""   # 末张=最新绑定（覆盖/重传后为准）
    return out


def load_merge_overlay(sidecar_path) -> tuple[list[list[int]], dict[int, str]]:
    """读 sidecar『同实体确认』+『行决策覆盖』（AT-11）。

    pipeline 不 import automation（render 保持独立可运行），按同一 JSON 合同容错直读；
    无文件/坏 JSON/坏值 → ([], {})（行为同无组无覆盖，逐行发号）。行号→图号必须与
    automation.replace.load_groups / load_row_decisions 读出的完全一致（同一数据源）。
    AT-12 N1：值白名单与 automation.replace.DECISION_VALUES 对齐（keep_origin / dismissed）。
    """
    try:
        payload = json.loads(Path(sidecar_path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return [], {}
    if not isinstance(payload, dict):
        return [], {}
    groups: list[list[int]] = []
    for group in payload.get("同实体确认") or []:
        if isinstance(group, list) and len(group) >= 2 \
                and all(isinstance(x, int) and x >= 1 for x in group):
            groups.append(sorted(set(group)))
    decisions: dict[int, str] = {}
    raw = payload.get("行决策覆盖")
    if isinstance(raw, dict):
        for key, value in raw.items():
            if str(key).isdigit() and value in ORIGIN_DECISIONS:
                decisions[int(key)] = str(value)
    return groups, decisions


def apply_naming(line: str, ents: list[Entity]) -> str:
    """清单填了命名的实体：把「模型看到什么」换成「命名（保留形态特征）」。"""
    pats: list[tuple[str, str, Entity]] = []
    for e in ents:
        for pat, rep in e.patterns():
            if pat and pat in line:
                pats.append((pat, rep, e))
    pats.sort(key=lambda x: len(x[0]), reverse=True)
    for pat, rep, ent in pats:
        if pat in line:
            line = line.replace(pat, rep)
            ent.hits += 1
    return line


def _subject_hits(scope: str, subject_value: str, ents: list[Entity]) -> dict[int, int]:
    """主体文本 → {图号: 最强命中长度}。

    两条判定，与 `attach_image_tags` 的语义完全一致：
    1. 实体核心名词的后缀在 `scope` 里命中（长描述实体名 ↔ 短主体词的唯一桥）；
    2. `subject_value` 里的短词（`人物`/`手机`/`产品`/`包装`）走 `SUBJECT_ALIASES` 别名表。
    """
    norm = re.sub(r"\s+", "", scope)
    best: dict[int, int] = {}
    for e in ents:
        if not e.no:
            continue
        for suf in e.tag_suffixes():
            if suf in norm:
                best[e.no] = max(best.get(e.no, 0), len(suf))
                break

    # 主体值别名：短词 → 资产类别 / 资产名片段。只在主体列的值里匹配，绝不扫整行。
    if subject_value:
        for words, mode, arg in SUBJECT_ALIASES:
            if not any(w in subject_value for w in words):
                continue
            for e in ents:
                if not e.no:
                    continue
                name = e.name or e.core_phrase()
                if (mode == "kind" and e.kind == arg) or (mode == "name" and arg in name):
                    best[e.no] = max(best.get(e.no, 0), len(arg))
                    break
    return best


def subject_tag_numbers(value: str, ents: list[Entity]) -> list[int]:
    """只按「运动主体」列的值取图号：升序、最多 MAX_TAGS_PER_LINE 个。

    `分镜列` 用它把图号塞进句子（紧跟主体值），不带行首前缀。
    """
    if not value.strip():
        return []
    best = _subject_hits(value, value, ents)
    ranked = sorted(best.items(), key=lambda kv: (-kv[1], kv[0]))
    return sorted(no for no, _n in ranked[:MAX_TAGS_PER_LINE])


def attach_image_tags(line: str, ents: list[Entity]) -> str:
    """给分镜行挂 `@图片N`：一行最多 3 个，已有图号不重复挂（幂等）。

    只认「主体」那一列，不看整行（2026-10-30 实测的坑）：
    原来拿整行做匹配，某镜头主体是「手部与产品」，但布光列写了「台面漫反射阴影」，
    于是场景实体「黑色平整台面」里的「台面」命中了，把 @图片7 挂到了这个镜头上——
    用户看到的是「这个镜头要用场景图」，完全是误导。

    命中规则：
    1. 先把行切成 `镜号｜起-止｜主体｜动作｜…`，只在**主体**列里找；
       切不出这个结构（别的模板）才退回整行。
    2. 实体核心名词从长到短取后缀，第一个能命中主体列的就认。
    3. 主体值里的短词（`人物`/`手机`/`产品`/`包装`）走 SUBJECT_ALIASES 别名表，
       按「资产类别」或「资产名片段」映射，避免删掉兜底后一行图号都不剩。
       兜底只匹配主体列的**值**，不扫整行——2026-11-01 的 bug 是整行搜 `产品|主体`
       撞上了「运动主体：」这个列名，于是每一镜都挂了产品图号（`运动主体：手机` 也挂）。
    4. 多个实体同时命中时按命中后缀长度取最长的前 3，输出按图号升序；
       正文里已经用 `@图片N` 指代过的产品不重复挂，只补齐主体缺的那几个。
    5. 只有表格行（第 2 列是「起-止」时间码）才允许补挂；句子型产物（叙事模板把图号
       写在句内）原样返回——在行首再加图号会破坏「图号必须在句内」。
    """
    cells = line.split("｜")                    # 镜号｜起-止｜主体｜…
    if len(cells) < 3 or not cells[1].strip():
        cells = line.split("|")
    shot_row = len(cells) >= 3 and bool(re.fullmatch(     # 第 2 列是「起-止」时间码 = 表格行
        r"\d{1,2}:\d{2}(?:\.\d+)?\s*[-–—~～]\s*\d{1,2}:\d{2}(?:\.\d+)?", cells[1].strip()))
    if "@图片" in line and not shot_row:
        return line                             # 句子型产物不许在行首补图号
    if shot_row:
        value = SUBJECT_LABEL_RE.sub("", cells[2]).strip()
        scope = value
    else:
        value = ""
        scope = line                            # 结构不认识，退回整行（保守）
    best_hits = _subject_hits(scope, value, ents)
    if not best_hits:
        return line
    ranked = sorted(best_hits.items(), key=lambda kv: (-kv[1], kv[0]))   # (图号, 命中长度)
    existing = {int(n) for n in re.findall(r"@图片(\d+)", line)}
    room = max(0, MAX_TAGS_PER_LINE - len(existing))                # 已有图号也算名额
    picked = [no for no, _n in ranked if no not in existing][:room]
    nos = sorted(picked)
    if not nos:
        return line
    return "".join(f"@图片{no} " for no in nos) + line


# ---------------------------------------------------------------- 渲染


def suggestion_reasons(path: Path) -> dict[int, str]:
    """读取意图建议中的理由；没有建议文件时返回空映射。"""
    if not path.is_file():
        return {}
    reasons: dict[int, str] = {}
    item_re = re.compile(r"^\s*[-*+•]?\s*(\d+)\s*[|｜]\s*理由\s*[：:]\s*(.*?)\s*[|｜]\s*镜次\s*[：:]")
    for raw in path.read_text(encoding="utf-8").splitlines():
        match = item_re.match(raw)
        if match:
            reasons[int(match.group(1))] = match.group(2).strip()
    return reasons


# ---------------------------------------------------------------- 台词改写（C-2）
# 意图要求「台词换成中文」时，intent.py 落了一张 `台词改写.md`（起-止｜原文｜中文）。
# 这里在渲染完的正文里**逐句精确替换**：不依赖模板段标题（lumi 的台词嵌在场景里、
# 甲方清单的台词在分镜里，段结构完全不同），只要原文出现在正文就换掉。
# 替换结果写进同目录 `渲染回执.json`，不写进提示词正文（复制出去的就是能直接用的成品）。
DIALOGUE_NAME = "台词改写.md"
# 「没人说话」的写法：不是语种，读语种时一律当空（v2.6 起拆解会明写 `人声语言：无`）
NO_SPEECH_VALUES = ("无", "无台词", "无对白", "不适用")
RECEIPT_NAME = "渲染回执.json"
TIME_CODE_RE = re.compile(r"\d{1,2}:\d{2}(?:\.\d+)?\s*[-–—~～]\s*\d{1,2}:\d{2}(?:\.\d+)?")
QUOTE_CHARS = "\"'“”‘’「」『』"
QUOTE_PAIRS = (
    re.compile(r'"([^"\n]{1,400})"'),
    re.compile(r"“([^”\n]{1,400})”"),
    re.compile(r"「([^」\n]{1,400})」"),
    re.compile(r"『([^』\n]{1,400})』"),
)
LATIN_RE = re.compile(r"[A-Za-z]{2,}")


def _load_intent_module():
    """按路径导入 intent.py：渲染器只借它的纯字符串 `rewrite_dialogue`，不调模型。"""
    import importlib.util
    path = ROOT / "pipeline" / "reverse" / "intent.py"
    spec = importlib.util.spec_from_file_location("_reverse_intent_for_render", path)
    if spec is None or spec.loader is None:
        raise Fail(EXIT_INPUT, f"导不进 {path.name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_dialogue_rows(path: Path) -> list[dict]:
    """读 `台词改写.md`：只认「时间码 + 原文 + 中文」三列，表尾有「说话人」列就一起带上。

    文件不存在 / 写的是「无台词」「不适用」→ 返回空列表，渲染行为与没有意图时逐字一致。
    AT-17 B1 附注 7：说话人**保留在返回行里**（不许只扩存储不消费）——本批渲染不消费，
    B2a 分角色落位时才用；三列旧格式没有这一列 → speaker 为空，解析行为不变。
    """
    if not path.is_file():
        return []
    rows: list[dict] = []
    speaker_index: int | None = None
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = re.sub(r"\s*\|\s*", "｜", raw.strip())
        if "｜" not in line:
            continue
        cells = [c.strip() for c in line.strip().strip("｜").split("｜")]
        if len(cells) < 3 or all(re.fullmatch(r"[-: ]*", c) for c in cells):
            continue
        if any(key in c for c in cells for key in ("起-止", "原文", "中文")):
            speaker_index = next((index for index, cell in enumerate(cells)
                                  if "说话人" in cell), None)
            continue
        code = TIME_CODE_RE.search(cells[0])
        original = cells[1].strip().strip(QUOTE_CHARS).strip()
        chinese = cells[2].strip()
        if not code or not original or not chinese:
            continue
        speaker = ""
        if speaker_index is not None and speaker_index < len(cells):
            speaker = cells[speaker_index]
        elif speaker_index is None and len(cells) >= 4:
            speaker = cells[-1]
        rows.append({"start_end": code.group(0), "original": original, "chinese": chinese,
                     "speaker": speaker})
    return rows


def spoken_language(sound_layer: str) -> str:
    """拆解「八 声音」写的语种（`人声语言：西班牙语（…）` → 西班牙语）；读不到就空。

    v2.6 起没人说话的片子会明写 `人声语言：无`——那不是语种，按「读不到」处理，
    否则下游会把「无」当成一种外语再去替换。
    """
    probe = sound_layer.replace("**", "").replace("`", "")
    match = re.search(r"人声语言\s*[：:]\s*([^\s（(（；;，,。]+)", probe)
    if not match:
        return ""
    lang = match.group(1).strip()
    return "" if lang in NO_SPEECH_VALUES else lang


def rewrite_spoken_language(text: str, source_lang: str) -> tuple[str, bool]:
    """把「人声语言：<原语种>」改成中文。

    只换语种名，括号里的口吻说明原样保留。不换这一行的话，正文会自相矛盾：
    台词已经全是中文，语种却还写着原片语言——生成平台照这行理解，就会照旧说外语。
    """
    if not source_lang or source_lang in ("中文", "汉语", "普通话"):
        return text, False
    pattern = re.compile(r"(人声语言\s*[：:]\s*)" + re.escape(source_lang))
    new_text, count = pattern.subn(r"\g<1>中文", text)
    return new_text, count > 0


def residual_dialogue(text: str) -> list[str]:
    """残留扫描：正文（附注之前）引号里还有拉丁字母的句子，逐条列出。

    逐行、同种引号配对扫，跨行串成一条就说明引号本身不配对，那种情况也不该被当成台词。
    """
    body = text.split("\n# 附注", 1)[0]
    found: list[str] = []
    for line in body.splitlines():
        for pattern in QUOTE_PAIRS:
            for match in pattern.finditer(line):
                inner = match.group(1).strip()
                if LATIN_RE.search(inner) and inner not in found:
                    found.append(inner)
    return found


# ---------------------------------------------------------------- 音频三态声音段（AT-17 B2a）
#
# 作用域 = BGM + 音效点（B0②）；台词走既有改写链（intent.rewrite_dialogue），这里只负责
# 把「谁说哪句」渲染成行，不改写译文。
#
# 三态是**纯字符串规则**，零模型调用（附注 10：新增模型调用要单独付费口径批准）：
#   · `沿用原片`   = 八层原值原样进声音段（BGM 四字段原词、音效点逐条、台词逐句带说话人）；
#   · `按新主体调整` = 只留**主体无关的骨架**（情绪 / 节奏 / 织体 / 强弱走向），
#                    ① 音效点整类不沿用——它们由原片主体的动作产生（「瓶盖摩擦声」这类
#                       物 specific 描述换主体后就是错的），留着等于沿用；
#                    ② BGM 值里点名旧实体的名词（待确认清单实体核心词）一并剥掉，
#                       剩下的就是换谁都能用的骨架；
#   · `不要`       = 官方三档式「无 BGM，仅保留环境声」，原片 BGM 词一个不带。
# 无音轨片：不写 BGM / 音效点 / 台词，如实写「无音轨」（省内容不省段，B0 附注 1）。
#
# 未指定三态（老调用方 / 老任务）→ kind="none"，整条链路走原路径，产物一字不变。

AUDIO_MODE_VALUES = ("沿用原片", "按新主体调整", "不要")
AUDIO_MODE_KEEP, AUDIO_MODE_ADJUST, AUDIO_MODE_MUTE = AUDIO_MODE_VALUES
SOUND_LAYER = "八"
NO_AUDIO_LINE = "无音轨：本条片子没有音轨，不写 BGM、音效点与台词；生成端音频开关随渠道默认。"
MUTE_LINES = ["BGM：无 BGM，仅保留环境声。", "音效点：无（只保留环境声，不设计音效点）。"]
ADJUST_SOUND_LINE = "音效点：不沿用原片声源（原片音效点由旧主体的动作产生），按新主体在对应时刻的动作重新拟声。"
NO_BGM_LITERAL = "无BGM"
# 键名与 extract.BGM_FIELDS 逐字一致（`BGM.情绪`…）；左边是解析键，右边是进提示词的标签。
BGM_LABELS = (("BGM.情绪", "情绪"), ("BGM.节奏", "节奏"),
              ("BGM.乐器/织体", "乐器/织体"), ("BGM.强弱走向", "强弱走向"))


def _sound_parser():
    """八 声音结构化解析与 B1/extract 同源（判据与消费同一个函数，不另写一套正则）。"""
    from extract import parse_sound_layer       # 同目录；render.py 运行时 sys.path 已含此目录
    return parse_sound_layer


def _flat(value: str) -> str:
    return re.sub(r"\s+", "", value or "")


def _no_bgm_value(value: str) -> bool:
    return _flat(value).lower().startswith(NO_BGM_LITERAL.lower())


def _strip_subject_nouns(value: str, ents: list["Entity"]) -> str:
    """BGM 值里点名旧实体的名词整串剥掉（实体核心名词短语，≥2 字；短于 2 字会误伤）。"""
    out = value
    for ent in ents:
        core = entity_noun_core(ent)
        if len(core) >= 2:
            out = out.replace(core, "")
    return re.sub(r"[，、；]{2,}", "，", out).strip("，、； ")


def _dialogue_lines(info: dict, *, checklist: bool = False) -> list[str]:
    """台词 → 行；说话人只认拆解侧那两类（画外音 / 出镜者），没标就如实写「未标」。"""
    out: list[str] = []
    lang = str(info.get("spoken_language") or "").strip()
    if lang:
        out.append(f"人声语言：{lang}。")
    rows = info.get("dialogue") or []
    if not rows:
        if info.get("no_dialogue"):
            out.append("台词：无。")
        return out
    for row in rows:
        text = str(row.get("original") or "").strip()
        if not text:
            continue
        speaker = str(row.get("speaker") or "").strip()
        if checklist:                              # 甲方清单式：`[画外音] | 画外音：原文`
            tag = speaker or "（说话人未标）"
            out.append(f"[{tag}] | {tag}：{text}")
        elif speaker:
            out.append(f"台词：{speaker}「{text}」。")
        else:
            out.append(f"台词：「{text}」（说话人未标）。")
    return out


def sound_plan(layers: dict[str, str], ents: list["Entity"], audio_mode: str,
               has_audio: bool | None = None) -> dict:
    """音频三态 → 声音段骨架。返回 {kind, mode, bgm[], dialogue[], note}。

    kind = `none`（没给三态，走原路径）/ `keep` / `adjust` / `mute`。
    `bgm` 是 BGM+音效点那几行，`dialogue` 是台词行；两拨分开，是为了清单式模板能把
    BGM 塞进行内（口播行）、台词另起 `[说话人] | 说话人：原文` 行。
    """
    mode = (audio_mode or "").strip()
    if mode not in AUDIO_MODE_VALUES:
        return {"kind": "none", "mode": "", "bgm": [], "dialogue": [], "dialogue_check": [],
                "note": ""}

    body = layers.get(SOUND_LAYER, "")
    info = _sound_parser()(body)
    dialogue = _dialogue_lines(info)
    dialogue_check = _dialogue_lines(info, checklist=True)
    no_audio = bool(info.get("no_audio")) or has_audio is False
    if no_audio:
        return {"kind": "mute", "mode": mode, "bgm": [NO_AUDIO_LINE], "dialogue": [],
                "dialogue_check": [],
                "note": "八 声音 标记无音轨：按「不要」渲染，不写 BGM / 音效点 / 台词"}

    bgm = info.get("bgm") or {}
    present = [(label, str(bgm.get(key) or "").strip()) for key, label in BGM_LABELS]
    present = [(label, value) for label, value in present if value and not _no_bgm_value(value)]
    points = [str(p).strip() for p in (info.get("sound_points") or []) if str(p).strip()]
    has_bgm = bool(present)
    structured = has_bgm or bool(points) or any(str(v or "").strip() for v in bgm.values())

    if mode == AUDIO_MODE_MUTE:
        return {"kind": "mute", "mode": mode, "bgm": list(MUTE_LINES), "dialogue": dialogue,
                "dialogue_check": dialogue_check, "note": ""}

    if mode == AUDIO_MODE_KEEP:
        if not structured:                         # v2.5 自由散文：原样保留（逐行照抄）
            raw = [line for line in _sound_raw_lines(body) if line]
            return {"kind": "keep", "mode": mode, "bgm": raw, "dialogue": [],
                    "dialogue_check": dialogue_check,
                    "note": "" if raw else "八 声音 正文为空"}
        lines: list[str] = []
        if has_bgm:
            bits = "，".join(f"{label}{value}" for label, value in present)
            lines.append(f"BGM：{bits}。")
        elif any(str(v or "").strip() for v in bgm.values()):
            lines.append("BGM：无BGM（原片没有背景音乐）。")
        lines += [f"音效点：{p}" if p.endswith(("。", "！", "？")) else f"音效点：{p}。"
                  for p in points]
        return {"kind": "keep", "mode": mode, "bgm": lines, "dialogue": dialogue,
                "dialogue_check": dialogue_check, "note": ""}

    # 按新主体调整
    if has_bgm:
        bits = "、".join(f"{label}（{_strip_subject_nouns(value, ents)}）"
                         for label, value in present)
        lines = [f"BGM：沿用原片的{bits}骨架，按新主体的气质重新配器，不重复原片旋律。",
                 ADJUST_SOUND_LINE]
        return {"kind": "adjust", "mode": mode, "bgm": lines, "dialogue": dialogue,
                "dialogue_check": dialogue_check, "note": ""}
    if structured:                                 # 原片无 BGM：保留骨架说法，不搬原曲目
        return {"kind": "adjust", "mode": mode,
                "bgm": ["BGM：原片没有背景音乐，按新主体重新配器。", ADJUST_SOUND_LINE],
                "dialogue": dialogue, "dialogue_check": dialogue_check, "note": ""}
    # 自由散文：拆不出四字段，只能给骨架指令，绝不逐字沿用（否则就是沿用）
    return {"kind": "adjust", "mode": mode,
            "bgm": ["BGM：沿用原片的情绪、节奏与强弱走向骨架，按新主体重新配器，不重复原片旋律。",
                    ADJUST_SOUND_LINE],
            "dialogue": dialogue, "dialogue_check": dialogue_check,
            "note": "八 声音 是自由散文、没有 BGM 四字段：只给骨架指令，不逐字沿用（待人工核对）"}


def _sound_raw_lines(body: str) -> list[str]:
    """八层正文 → 逐行文本（去项目符号/加粗，保留原句一个字不改）。"""
    out: list[str] = []
    for raw in body.splitlines():
        line = raw.replace("**", "").strip()
        line = re.sub(r"^(?:[-*+•]|\d+\.)\s*", "", line)
        if line:
            out.append(sanitize(line))
    return out


def _sound_dialogue_claim(sec: "Section") -> bool:
    """本段是否要渲染台词行（`台词渲染：说话人`）。"""
    return bool(sec.field("台词渲染").strip())


def _sound_section_style(sec: "Section") -> str:
    """本段的声音段形态：`是`=多行块；`行内`=压成一行（清单式口播行）。空=本段不渲染声音段。"""
    value = sec.field("声音段").strip()
    if not value or value in ("否", "false"):
        return ""
    return "行内" if "行内" in value else "是"


def resolve_audio_mode(explicit: str, task_dir: Path, disc: str) -> tuple[str, str]:
    """三态取值：显式入参 > 任务目录 `audio_mode.json` > 按拆解推导。返回 (值, 来源)。

    来源如实标出来，产物附注里能查：`请求参数`（hub/画布透传）/`audio_mode.json`（意图阶段落盘）
    /`按拆解推导`（老任务、没有落盘文件）。推导只有一份实现（intent.audio_mode，B2c 同源）。
    """
    want = (explicit or "").strip()
    if want:
        if want not in AUDIO_MODE_VALUES:
            raise Fail(EXIT_INPUT, f"audio_mode 只认 {'/'.join(AUDIO_MODE_VALUES)}，收到「{want}」")
        return want, "请求参数"
    try:
        intent = _load_intent_module()
        value = str((intent.read_audio_mode(task_dir) or {}).get("audio_mode") or "").strip()
        if value in AUDIO_MODE_VALUES:
            return value, "audio_mode.json"
        value = str((intent.audio_mode(disc) or {}).get("audio_mode") or "").strip()
        if value in AUDIO_MODE_VALUES:
            return value, "按拆解推导"
    except Exception as err:              # noqa: BLE001 —— 推不出来就退回原路径，但必须出声
        print(f"[WARN] 音频三态推导失败（{err}）：八 声音 按原路径渲染", file=sys.stderr)
    return "", ""


def render_prompt(fm: dict[str, str], secs: list[Section], layers: dict[str, str],
                  ents: list[Entity], facts: dict[str, str], pid: str, tpl_stem: str,
                  reasons: dict[int, str] | None = None,
                  body_mode: str = "", audio_mode: str = "",
                  audio_src: str = "", has_audio: bool | None = None) -> str:
    """body_mode：`流式` 时只输出一段可直接贴进生成平台的正文——无标题、无规格行、无附注。

    出处分两档：模板 frontmatter `正文模式:`，命令行 `--body-mode` 覆盖。
    audio_mode：`沿用原片` / `按新主体调整` / `不要`（AT-17 B2a）；空=未指定，八 声音 走原路径
    （老调用方产物一字不变）。取值来源见 `resolve_audio_mode()`。
    """
    mode = (body_mode or fm.get("正文模式", "")).strip()
    flow = mode in ("流式", "plain")

    blocks: dict[str, list[Block]] = {cn: parse_blocks(body, cn)
                                      for cn, body in layers.items()}
    # 字典只含第十层登记过的字样；没有第十层（旧九层拆解）时为空，全部行为不变。
    glyph_dict = glyph_dictionary(blocks)
    n_shots, span = shot_range(blocks.get("七", []))

    # 维度独占：某维度被某个段的标题点名，就只准那个段吃（先出现的段为准）
    dims = [dims_of_section(s) for s in secs]
    owner: dict[str, int] = {}
    for idx, (_want, strong) in enumerate(dims):
        for d in strong:
            owner.setdefault(d, idx)

    used: set[int] = set()
    took: dict[str, int] = {}          # 层 → 真正吃掉该层子块的第一段
    bodies: list[list[str]] = []
    # ---- 音频三态（AT-17 B2a）：八 声音 由「逐块倒出」升级为「按三态渲染的声音段」 ----
    plan = sound_plan(layers, ents, audio_mode, has_audio)
    sound_styles = [_sound_section_style(s) for s in secs]
    sound_sections = {i for i, style in enumerate(sound_styles) if style}
    dialogue_sections = {i for i, s in enumerate(secs) if _sound_dialogue_claim(s)}
    # 模板没声明声音段落位、但三态要求改写声音时，退回「谁独占声音维度谁吃」——
    # 不标注就等于留着原片 BGM 词，那是漏，不是省。
    if plan["kind"] in ("adjust", "mute") and not sound_sections and "声音" in owner:
        sound_sections.add(owner["声音"])
        sound_styles[owner["声音"]] = "是"
    sound_claimed = bool(sound_sections or dialogue_sections)
    # 结构化四字段之外的块（`对位：` 这类）只在**沿用**模式下原样跟着声音段走，不静默丢。
    # 「调整」不带：这些旁支句里同样可能点名旧主体，剥不干净就是沿用；「不要」与无音轨更不带。
    sound_extra: list[str] = []
    if sound_claimed and plan["kind"] == "keep":
        known = {"BGM", "音效点", "人声语言", "台词"}
        for b in blocks.get(SOUND_LAYER, []):
            if b.label.split(".")[0].split("（")[0] in known:
                continue
            sound_extra += [x for x in (sanitize_with_user_desc(strip_core_residue(
                replace_uploaded_asset_refs(apply_naming(x, ents), ents, b.dim, b.layer), ents
            ), ents) for x in b.lines) if x]
    for idx, sec in enumerate(secs):
        want = dims[idx][0]
        body_items: list[dict] = []
        dropped_count = [0]
        body: list[str] = []
        sound_style = sound_styles[idx]
        dialogue_here = idx in dialogue_sections
        if plan["kind"] == "none":
            # 没给三态（老调用方 / 推导失败）：两个落位都不生效，八 声音 走原路径——
            # 老调用方的产物必须一字不变（probe_at9/at11/at16 都直接调 render_prompt）。
            sound_style = ""
            dialogue_here = False
        if (plan["kind"] == "none" and flow and sound_styles[idx]
                and {cn for cn, _name in layer_refs(sec)} == {SOUND_LAYER}):
            # 流式模板里新加的「纯声音段」在老路径下无处落位（它本来就是为三态新建的）：
            # 没有三态就整段不出，流式正文与改模板前一字不差。
            bodies.append([])
            continue
        # `分镜列`：这段把分镜表按声明的列拼成「一句一条」，不逐列倒出（叙事体唯一可行的路径）。
        ncols = shot_columns(sec)
        # 自带句子的段（实体指认 / 原句输出）不消费拆解子块——它们的正文就是那一句，
        # 再往里灌资产行 / 镜头行只会把单段流式正文冲散。
        self_contained = (sec.field("实体指认").strip() not in ("", "否", "false")
                          or bool(sec.field("原句输出").strip()))
        narr: list[str] = []
        if ncols:
            header, rows = shot_table(layers.get("七", ""))
            if not header:
                raise Fail(EXIT_TEMPLATE,
                           f"段 {sec.n}「{sec.title}」写了 `分镜列`，但拆解「七 分镜表」里没有表格")
            # 分镜行已经被前面的段吃掉的话，这里不再重复输出同一张表（否则同一条片子会被写两遍）
            if any(b.dim == "分镜" and id(b) not in used for b in blocks.get("七", [])):
                narr = [ln for ln in (assemble_shot_line(c, header, ncols, sec, ents)
                                      for c in rows) if ln]
                # 可选列（带 `?`）在旧拆解里可能整列没有：跳过，但要说一声——
                # 「不静默跳过」的意思是不悄悄少东西，不是缺一列就炸掉整片。
                missing_opt = [c.rstrip("?") for c in ncols
                               if c.endswith("?") and resolve_shot_column(c, header, sec) is None]
                if missing_opt:
                    print(f"  [WARN] 段 {sec.n}「{sec.title}」可选列在拆解里没有："
                          f"{'、'.join(missing_opt)}（这两项不进句子）", file=sys.stderr)

        # 数值段（写什么要「秒数 / 总时长」）：给数值，不铺该层全文，也不消费子块
        if re.search(r"秒数|总时长", sec.field("写什么")):
            aspect = facts.get("画幅") or ""
            size = facts.get("尺寸") or ""
            frame = f"{aspect} {size}".strip()
            head = f"片长：{facts.get('时长', '待填')} 秒（{frame or '画幅待填'}）"
            if n_shots:
                head += f"；分镜 {n_shots} 个镜头，{span}"
            bodies.append([sanitize(head)])
            continue

        for cn, _name in (() if self_contained else layer_refs(sec)):
            for b in blocks.get(cn, []):
                if id(b) in used:
                    continue
                if cn == SOUND_LAYER and sound_claimed and plan["kind"] != "none":
                    # 八 声音 由三态渲染（声音段/台词行），不再逐块倒出——倒出来就是字段名回显，
                    # 而且会把「沿用/调整/不要」三态的差异冲平（附注 9/10 的同一个根因）。
                    used.add(id(b))
                    took.setdefault(cn, idx)
                    continue
                if ncols:
                    if b.dim != "分镜":
                        continue              # 本段只组装分镜行，同层别的块留给后面的段
                    used.add(id(b))
                    took.setdefault(cn, idx)
                    continue                  # 分镜行由 `分镜列` 统一组装，不再逐列倒出
                if b.dim and owner.get(b.dim, idx) != idx:
                    continue                      # 这一维度被别的段独占
                if want and b.dim not in want:
                    continue                      # 维度对不上本段
                if not want and not b.label:
                    continue
                lines = [sanitize_with_user_desc(strip_core_residue(
                    replace_uploaded_asset_refs(apply_naming(x, ents), ents, b.dim, b.layer), ents
                ), ents) for x in b.lines]
                if (sec.field("字样置换").strip() not in ("", "否", "false")
                        and (b.layer == "十" or b.dim == "收束")):
                    # 第十层照抄的参考片字样 → 客户占位（借创意不复刻品牌）：
                    # 版式/动效/时段原样保留，只有原字换掉；模板段不写 `字样置换：是` 就不生效。
                    # Block.layer 已统一为层号，`== "十"` 这道门现在真的命中第十层的块
                    # （B2：曾存层名「图文与收束」，比较层号永假，包装原字整个漏换）。
                    # 置换两态（B3）：品牌位换「客户品牌字样」，文案位（字幕/标语/角标/
                    # 水印/促销文案）换「客户文案（定稿后填入）」，「部分可辨」原样保留。
                    kind = glyph_kind_of_label(b.label)
                    lines = [_replace_brand_glyphs(x, kind) for x in lines]
                    lines = [_replace_copy_glyphs(x, kind) for x in lines]
                if "一句话" in sec.field("写什么"):
                    # 模板要的是「一句话」；整块倒出会把后面所有分句（含手法术语）一起带进总述
                    lines = [x.split("。")[0] + "。" if "。" in x else x for x in lines]
                if b.dim == "分镜":
                    # 分镜行挂图号：资产与提示词在这里对齐，上传时才能逐行核对
                    lines = [attach_image_tags(x, ents) for x in lines]
                for x in (y for y in lines if y):
                    item = {"text": x, "dim": b.dim, "layer": cn, "drop": False}
                    if b.dim in ("产品", "资产"):
                        twin = find_duplicate_asset(item, body_items)
                        if twin is not None:
                            if len(item_payload(item["text"])) > len(item_payload(twin["text"])):
                                twin["drop"] = True
                                dropped_count[0] += 1
                            else:
                                item["drop"] = True
                                dropped_count[0] += 1
                    body_items.append(item)
                used.add(id(b))
                took.setdefault(cn, idx)

        # 只合并「产品 / 资产」这两类行，且只跨层合并（二 产品·附件 与 九 资产·实体道具二 是同一件东西）。
        # 同层的高相似行（比如两个不同产品都叫「粉色小瓶」）不合并——宁多勿删。
        body = narr if ncols else [it["text"] for it in body_items if not it["drop"]]
        if dropped_count[0]:
            print(f"  [去重] 「{sec.title}」合并 {dropped_count[0]} 条重复资产行", file=sys.stderr)
            dropped_count[0] = 0

        # —— 音频三态声音段（AT-17 B2a）——
        # 本段声明 `声音段：是|行内` 就把三态渲染的声音行接进本段正文；`台词渲染：说话人`
        # 另起台词行（清单式 `[画外音] | 画外音：原文`）。两句可并存：口播行只要 BGM，
        # 分镜行只要台词——甲方清单式就是这样拆的，声音不独立成段。
        if plan["kind"] != "none":
            if sound_style:
                sound_lines = list(plan["bgm"]) + sound_extra
                if sound_style == "行内":
                    # 行内：压成一条流水句（标点各留一个句号，不出现「。；」）
                    joined = "；".join(x.rstrip("。；") for x in sound_lines if x)
                    if joined:
                        body = body + [sanitize(joined + "。")]
                else:
                    if not dialogue_sections:          # 台词没人另起行时，并进声音段
                        sound_lines += list(plan["dialogue"])
                    body = body + [sanitize(x) for x in sound_lines if x]
            if dialogue_here:
                body = body + [sanitize(x) for x in plan["dialogue_check"] if x]

        # —— 单段流式模板的段指令（templates/seedance-flow.md）——
        # `实体指认`：段首一句「全程使用参考图中的 @图片N 说明、…」，只给图号和短说明。
        if sec.field("实体指认").strip() not in ("", "否", "false"):
            roll = entity_roll_call(ents)
            if roll:
                body = [roll] + body
        # `原句输出`：模板写死的收尾句（出处见该段 `原文依据`），原样进正文。
        literal = sec.field("原句输出").strip()
        if literal:
            body = body + [literal if literal.endswith(("。", "！", "？")) else literal + "。"]
        # `时段格式：秒`：`00:00–00:04` → `0-4秒`。
        if sec.field("时段格式").strip().strip("`") == "秒":
            body = [seconds_timecodes(x) for x in body]
        # `连写：是`：本段多行合成一段（分镜行 → 连续叙事），行尾补句号。
        if sec.field("连写").strip() not in ("", "否", "false"):
            joined = "".join(x if x.endswith(("。", "！", "？", "，")) else x + "。" for x in body)
            body = [joined] if joined else []

        # 首帧类段：拆解里没有「第 0 秒状态」这个独立块，它就是分镜第一条。
        # 这类段允许**只读复用**分镜首块（不解除前面的消费、不整块复制），
        # 否则去重逻辑会把首帧段饿成「无」。
        if not body and re.search(r"首帧|第\s*0\s*秒|起始帧|开场帧", sec.field("写什么")):
            first_shot = next((b for b in blocks.get("七", []) if b.dim == "分镜"), None)
            if first_shot is not None and first_shot.lines:
                body = [sanitize_with_user_desc("首帧：" + strip_core_residue(
                    replace_uploaded_asset_refs(apply_naming(first_shot.lines[0], ents),
                                                ents, first_shot.dim, first_shot.layer), ents), ents)]

        bodies.append(body)

    dropped = [f"{cn} {LAYER_BY_CN[cn]}·{b.label or b.lines[0][:12]}"
               for cn, bl in blocks.items() for b in bl if id(b) not in used]

    # 命名兜底：拆解原文里找不到逐字对应的实体，落到它所属层第一次出现的那一段（合并成一行）
    loose: dict[int, list[str]] = {}
    cited = {m for body in bodies for line in body for m in re.findall(r"@图片(\d+)", line)}
    for ent in ents:
        if not (ent.name or ent.has_asset_image or ent.my_desc.strip()) or ent.hits or not secs:
            continue
        if ent.no and str(ent.no) in cited:
            continue                      # 正文已经用 @图片N 指过它，不再另起「素材：」行
        cn = KIND_TO_LAYER_CN.get(ent.kind, "")
        target = took.get(cn)
        if target is None:
            target = next((i for i, s in enumerate(secs)
                           if cn and cn in [c for c, _ in layer_refs(s)]), 0)
        loose.setdefault(target, []).append(ent.label())
    # 叙事体（模板里有 `分镜列`）的正文不许以图号开头（判据 J4：图号必须在句内），
    # 兜底行加一个引导词；旧三套模板的行首图号是既有契约，不加（J2b）。
    loose_head = "素材：" if any(shot_columns(s) for s in secs) else ""
    narrative = bool(loose_head)
    # 流式模板已经有「实体指认」句时，不再往末段塞「素材：@图片N」——同一批图号写两遍会把正文冲散。
    skip_loose = flow and any(s.field("实体指认").strip() not in ("", "否", "false") for s in secs)
    for target, labels in ({} if skip_loose else loose).items():
        # 一个实体一行。原来用 `；` 把四个产品挤成一行「命名：@图片1 …；@图片2 …」，
        # 上传时要逐张核对，挤在一起根本没法对照；拆开后每行的标签就是它的图号。
        for label in labels:
            text = label
            if narrative:
                # 叙事体正文只放「@图片N 短名」：整段资产百科（几十字的外形描述）灌进
                # 正文会把叙事冲散，所有者早前也明确要求产品在正文里只用图号。
                text = re.split(r"[（(]", text, maxsplit=1)[0].strip()
                target = len(bodies) - 1        # 素材对照放最后一段，不塞进开场总述
            bodies[target].append(sanitize_with_user_desc(loose_head + text, ents))

    # 字典式置换收口（先字段级、后字典级，两遍幂等）：字段级置换只开在第十层/
    # 收束段，这里对**全部段**的正文行做字典替换兜底——图文条目「位置」等字段
    # 里裸嵌的原字与产品/场景等其他层描述里照抄的已登记字样都在此换掉
    # （T6 真机验收发现的两处泄漏边界）。「部分可辨」引号在函数内整体豁免。
    if glyph_dict:
        bodies = [[replace_registered_glyphs(x, glyph_dict) for x in body]
                  for body in bodies]

    if flow:
        # 流式正文：只有一段。段与段之间也直接相接（0 x 空格），段内行尾补句号。
        parts = [x for body in bodies for x in body if x]
        para = "".join(p if p.endswith(("。", "！", "？")) else p + "。" for p in parts)
        if dropped:
            print("[WARN] 流式正文未落入任何段的子块：" + "；".join(dropped), file=sys.stderr)
        return para.rstrip() + "\n"

    out: list[str] = [
        f"# 提示词 · {pid} · {tpl_stem}",
        "",
        f"> 由 `拆解.md` 十层 IR 按 `templates/{tpl_stem}.md` 逐段拼接；纯字符串渲染，未经模型加工。",
        "> 段标题逐字取自模板；每个子块只进一段（先引用的段拿走，后段不再重复）。",
        "",
    ]
    # 模板里没有「总述」这类数值段时，画幅就没有落点，生成端会自己猜比例。
    # 2026-10-31 所有者反馈：比例必须出现在提示词里。这里补一行机器量出来的全片规格。
    has_numeric_section = any(re.search(r"秒数|总时长", s.field("写什么")) for s in secs)
    # 只有量到了画幅才补这一行：老任务（拆解里没有规格行）保持原样，不给半截空行。
    if not has_numeric_section and facts.get("画幅"):
        # 只写生成端真有的东西：片长、画幅（五种之一）、有没有音轨。
        # 尺寸 / 帧率 / 画质一律不进提示词——那是生成平台上的参数，由使用者自己选
        # （2026-11 所有者反馈）。
        spec_bits = [f"片长 {facts.get('时长', '待填')} 秒"]
        if facts.get("画幅"):
            spec_bits.append(f"画幅 {facts['画幅']}")
        if facts.get("音轨"):
            spec_bits.append(f"音轨{'有' if str(facts['音轨']).startswith('有') else '无'}")
        out.append("**全片规格**：" + " · ".join(spec_bits))
        out.append("")
    for sec, body in zip(secs, bodies):
        out.append(f"## {fill_title(sec.title, n_shots, span)}")
        out.extend(body if body else ["无"])
        out.append("")

    reasons = reasons or {}
    replacement_ents = [ent for ent in ents if ent.suggest == "必须换"]
    if replacement_ents or any(ent.suggest for ent in ents):
        out.append("## 本片替换说明")
        out.append("")
        for ent in replacement_ents:
            reason = reasons.get(ent.no, "该资产被标记为必须换")
            out.append(f"- 图片{ent.no}：{reason}")
        out.append("- 其余沿用参考片")
        out.append("")

    out.append("# 附注（贴进生成平台时删掉本节）")
    out.append("")
    out.append(f"- 模板：{fm['name']}｜来源：{fm.get('来源', '')}")
    if fm.get("可信度"):
        out.append(f"- 可信度：{fm['可信度']}")
    # 音频三态（AT-17 B2a）：产物自己说话——声音段为什么长这样，附注里能查。
    if plan["mode"]:
        src = f"，来源：{audio_src}" if audio_src else ""
        out.append(f"- 音频三态：{plan['mode']}（声音段按该态渲染{src}）")
    if plan["note"]:
        out.append(f"- 音频三态提示：{plan['note']}")
    for sec in secs:
        src = sec.field("数据来源").replace("`", "").strip()
        out.append(sanitize(f"- 段 {sec.n} {sec.title}｜口吻：{sec.field('口吻')}｜数据来源：{src}"))
    if dropped:
        out.append(sanitize("- 未落入任何段、按规范丢弃的子块：" + "；".join(dropped)))
    return "\n".join(out).rstrip() + "\n"


def render_assets(ents: list[Entity], layers: dict[str, str], pid: str) -> str:
    # 第十层登记字样 → 客户占位的字典（与正文同一套 glyph_dictionary，T6-G）：
    # 资产清单是客户视角的生产素材，描述里照抄的原字（道具行「表面有「ZING!」字样」）
    # 与提示词正文一样要置换——品牌/包装类→「客户品牌字样」，字幕/标语类→「客户文案
    # （定稿后填入）」。拆解.md / 待确认清单.md 是 IR 事实层，照实不动，只置换本清单。
    blocks: dict[str, list[Block]] = {cn: parse_blocks(body, cn)
                                      for cn, body in layers.items()}
    glyph_dict = glyph_dictionary(blocks)
    out: list[str] = [
        f"# 资产清单 · {pid}",
        "",
        "> 由 `拆解.md`「九 资产」+ `待确认清单.md` 生成；**不生图**，只列要准备什么。",
        "> 类型映射：主体→产品图｜人物→人物参考图｜场景→场景参考图｜道具→道具参考图。",
        "",
        "| 序号 | 类型 | 要准备什么 | 锁什么 |",
        "|---|---|---|---|",
    ]
    next_no = 0
    pending: list[str] = []
    unbound_brand: list[Entity] = []
    for ent in ents:
        if ent.kind.startswith("品牌视觉"):
            # AT-16 B1b 第五点：品牌行绑了图 → 与备图行同表同号（不再另列无编号占位）；
            # 未绑图 → 走文末置换位占位行（不占号、不猜图）。
            if not ent.no:
                unbound_brand.append(ent)
                continue
            next_no = max(next_no, ent.no)
            nimg = len(image_urls(ent.image))
            what = ent.label().replace("|", "／") + f"（已备图 {nimg} 张）"
            out.append(f"| 图片{ent.no} | {BRAND_VISUAL_CATEGORY} | 清单第 {ent.row_no} 行 · {what}"
                       f" | {ent.lock or '客户品牌字样（待填）'} |")
            continue
        kind = KIND_TO_ASSET_TYPE.get(ent.kind)
        if not kind:
            raise Fail(EXIT_INPUT, f"待确认清单里有未知类别「{ent.kind}」（只认 主体/人物/场景/道具）")
        if ent.no:                       # 已备图：进提示词，占 @图片N
            next_no = max(next_no, ent.no)
        else:                            # 未备图：只列要准备什么，不占号
            pass
        what = ent.label().replace("|", "／")
        if ent.image:
            nimg = len([x for x in re.split(r"[,，]", ent.image) if x.strip()])
            what += f"（已备图 {nimg} 张）"
        if ent.no:
            out.append(f"| 图片{ent.no} | {kind} | 清单第 {ent.row_no} 行 · {what} | {ent.lock or '无'} |")
        else:
            pending.append(f"| — | {kind} | 清单第 {ent.row_no} 行 · {what} | {ent.lock or '无'} |")

    out.extend(pending)

    # 第十层品牌字/包装字 → 「品牌视觉（置换位）」（2026-11-04 路 A 审计 B4；T6-G 双保险）：
    # 首选收待确认清单里的品牌视觉行（免「建议」条件——意图回填会把该列清空，真机对照
    # pop2-source-2 因此丢过整行；人工填的命名/描述也随行带上）。AT-16 B1b：绑了图的品牌行
    # 已在上面与备图行同表同号，这里只列**未绑图**的品牌行；清单里根本没有品牌行时才退回
    # 「十」层探测，置换位仍在。字幕/标语/角标/水印是文案位，生成端占位即可，
    # 没有可备的图，不进资产表。
    if unbound_brand:
        for e in unbound_brand:
            head = f"{e.name}：" if e.name else ""
            desc = (head + (e.my_desc.strip() or e.seen)).replace("|", "／")
            out.append(f"| — | {BRAND_VISUAL_CATEGORY} | 清单第 {e.row_no} 行 · {desc}"
                       f" | {e.lock or '客户品牌字样（待填）'} |")
    elif not any(e.kind.startswith("品牌视觉") for e in ents) \
            and re.search(r"品牌字|包装字", layers.get("十", "")):
        out.append("| — | 品牌视觉（置换位） | 不要求备图：参考片品牌字/包装字原字已置换，"
                   "生成端按「客户品牌字样」占位渲染；客户定稿后把实际字样填进本行"
                   " | 客户品牌字样（待填） |")

    # 引用图数门禁：万相「图1…图N」是 media 数组顺序，超过 5 张挂不下。
    if next_no > 5:
        out.append("")
        out.append(f"> ⛔ 本次引用 {next_no} 张图，超过万相单次上限（5 张）。这份提示词不能直接提交：请减少要换的资产，或按下面分组分段生成。")
        numbered = [e for e in ents if e.no]
        for gi in range(0, len(numbered), 5):
            group = numbered[gi:gi + 5]
            names = "、".join(f"图片{e.no}（{e.name or e.kind}）" for e in group)
            out.append(f"> · 第 {gi // 5 + 1} 段：{names}")
    elif next_no:
        out.append("")
        out.append(f"> 本次引用 {next_no} 张图（≤5），可直接提交。")
    else:
        out.append("")
        out.append("> 还没有备图：提示词里不会出现 @图片N，全部按文字描述生成。")

    assets = layers.get("九", "")
    if any(k in assets for k in HAND_KEYS):
        out.append(
            f"| — | 手握比例图 | 同一产品 + 人手握持的中近景参考"
            "（以手掌宽度与瓶身长度的真实比例为准，标签不被手指完全遮住） "
            "| 手握时瓶体占手掌的比例、指缝间露出瓶身的长度、标签可见面积 |"
        )
        note = ("> 「九 资产」层提到手持/握住/举着 → 按甲方硬约束补「手握比例图」；"
                "缺这张图，出片时瓶体比例会被放大。")
    else:
        note = "> 「九 资产」层未提到手持/握住/举着 → 不出「手握比例图」。" 
    out.append("")
    out.append(note)
    # 原字置换只开在资产清单（客户视角生产素材）；幂等，占位字面再过一遍不变形。
    return "\n".join(replace_registered_glyphs(sanitize_with_user_desc(x, ents), glyph_dict)
                     for x in out) + "\n"


# ---------------------------------------------------------------- 入口


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="十层 IR + 模板 → 提示词 + 资产清单（纯字符串渲染）")
    ap.add_argument("--id", required=True, help="分析目录名（analysis/reverse/<id>）或直接给目录路径")
    ap.add_argument("--template", required=True, help="templates/<name>.md 的 name，或 .md 文件路径")
    ap.add_argument("--optional", default="", help="要打开的可选段字母，逗号分隔，如 A,B（默认全关）")
    ap.add_argument("--out-dir", default="", help="产物目录（默认写回 --id 目录）")
    ap.add_argument("--body-mode", default="",
                    help="正文模式：留空=模板 frontmatter `正文模式:`（缺省文档模式）；`流式`=只出一段可直贴正文（无标题/无附注）")
    ap.add_argument("--audio-mode", default="",
                    help="音频三态：沿用原片 / 按新主体调整 / 不要（AT-17 B2a）；"
                         "留空=任务目录 audio_mode.json → 按拆解推导（都没有则八 声音 走原路径）")
    args = ap.parse_args(argv)

    try:
        corpus = ExampleCorpus(ROOT)

        # 1) 目录
        id_path = Path(args.id)
        if not id_path.is_absolute():
            candidate = ROOT / "analysis" / "reverse" / args.id
            id_path = candidate if candidate.is_dir() else (ROOT / args.id)
        if not id_path.is_dir():
            raise Fail(EXIT_INPUT, f"分析目录不存在：{id_path.name}")
        pid = id_path.name

        # 2) 模板
        tpl_arg = args.template
        tpl_path = Path(tpl_arg)
        if not tpl_path.is_file():
            stem = tpl_arg[:-3] if tpl_arg.endswith(".md") else tpl_arg
            tpl_path = ROOT / "templates" / f"{stem}.md"
        if tpl_path.is_file():
            tpl_path = tpl_path.resolve()
        if not tpl_path.is_file():
            raise Fail(EXIT_INPUT, f"模板不存在：templates/{args.template}.md")

        text = read_text(tpl_path, EXIT_TEMPLATE, "模板")
        fm, body = split_frontmatter(text)
        main_secs, opt_secs = parse_sections(body)
        wanted = [x.strip().upper() for x in re.split(r"[,，]", args.optional) if x.strip()]
        secs = main_secs + [s for s in opt_secs if s.fields.get("_tag", "") in wanted]
        if wanted:
            have = {s.fields.get("_tag") for s in opt_secs}
            miss = [w for w in wanted if w not in have]
            if miss:
                raise Fail(EXIT_TEMPLATE, f"模板里没有可选段 {','.join(miss)}；现有 {sorted(have)}")
        validate_template(fm, main_secs, opt_secs, tpl_path, ROOT, corpus, wanted)

        # 3) 拆解十层
        disc = read_text(id_path / "拆解.md", EXIT_INPUT, "拆解")
        layers, order = parse_layers(disc)
        missing = sorted({name for s in secs for _cn, name in layer_refs(s)
                          if LAYER_BY_NAME[name] not in layers},
                         key=lambda n: LAYER_BY_NAME[n])
        if missing:
            raise Fail(EXIT_LAYER,
                       f"模板段要的层在拆解里没有：{'、'.join(f'{m} {LAYER_BY_NAME[m]}' for m in missing)}；"
                       f"拆解只有 {len(order)} 层")
        if len(order) != 10:
            print(f"[WARN] 拆解只解析出 {len(order)} 层，模板用到的层都在，继续。", file=sys.stderr)

        # 4) 待确认清单（可选）——发号读组/行决策覆盖（AT-11 §3.3/§3.4，同一 sidecar 数据源）
        checklist_md_path = id_path / "待确认清单.md"
        groups, ko_decisions = load_merge_overlay(id_path / "待确认清单.json")
        ents = assign_numbers(parse_checklist(checklist_md_path), groups, ko_decisions)
        if not ents:
            print("[WARN] 没有可用的待确认清单，命名替换跳过（原样用模型看到的描述）。", file=sys.stderr)

        out_dir = Path(args.out_dir) if args.out_dir else id_path
        # 音频三态（AT-17 B2a）：命令行请求参数 > 任务目录 audio_mode.json > 按拆解推导
        audio_mode, audio_src = resolve_audio_mode(args.audio_mode, id_path, disc)
        facts = header_facts(disc)
        has_audio = str(facts.get("音轨", "")).startswith("有") if facts.get("音轨") else None
        prompt = render_prompt(fm, secs, layers, ents, facts, pid, tpl_path.stem,
                               suggestion_reasons(id_path / "意图建议.md"),
                               body_mode=args.body_mode, audio_mode=audio_mode,
                               audio_src=audio_src, has_audio=has_audio)
        assets = render_assets(ents, layers, pid)
        # 台词改写：只有 intent.py 落了对照表才动正文；没有表 → 整段跳过，
        # 产物与「没有意图」时逐字一致（判据 B6）。
        dialogue_rows = load_dialogue_rows(id_path / DIALOGUE_NAME)
        receipt: dict | None = None
        if dialogue_rows:
            intent_mod = _load_intent_module()
            source_lang = spoken_language(layers.get("八", ""))
            prompt, hits, misses = intent_mod.rewrite_dialogue(prompt, dialogue_rows)
            prompt, lang_changed = rewrite_spoken_language(prompt, source_lang)
            receipt = {
                "dialogue_total": len(dialogue_rows),
                "hits": hits,
                "misses": misses,
                "source_lang": source_lang,
                "residual": residual_dialogue(prompt),
                "language_line": "已改为中文" if lang_changed else "未改（拆解没写语种或已是中文）",
                "template": tpl_path.stem,
            }

        write_text(out_dir / f"提示词-{tpl_path.stem}.md", prompt)
        write_text(out_dir / "资产清单.md", assets)
        if receipt is not None:
            write_text(out_dir / RECEIPT_NAME,
                       json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")

        print(f"[OK] 提示词-{tpl_path.stem}.md（{len(prompt.splitlines())} 行）"
              f" ＋ 资产清单.md（{len(ents) + (1 if '| 手握比例图 |' in assets else 0)} 行资产）"
              + (f" ＋ 渲染回执.json（台词 {receipt['hits']}/{receipt['dialogue_total']} 句已中文化"
                 + (f"，正文仍有 {len(receipt['residual'])} 句原文" if receipt.get("residual") else "")
                 + "）" if receipt else ""))
        return EXIT_OK
    except Fail as err:
        die(err)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
