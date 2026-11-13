# -*- coding: utf-8 -*-
"""AT-9 B5 渲染自检——渲染完成时自动跑，结论落盘 projects/<id>/自检.json。

数据合同 B0「自检结果」：生产者=渲染后自检函数；存储=projects/<id>/自检.json，
每条 {项,结论,证据}；消费者=详情页审批区（✓/⚠ 可见，FAIL 可 override 但必显）；
旧任务无文件 → 前端显「未检查」。

四项全部机械可判（AT-9.md §六层4 + AT-11 §3.3 第4项）：
  ① 次序对账：行号→图号→实际提交图片次序三方映射一致。发号用 render.assign_numbers
    同源函数重算（清单行号与 @图片N 是两套编号，禁行号==图号字面比较；组内 N 行共用
    1 图号——AT-11 B1），提交次序取确认时点快照 任务.json assets_confirmed（列表序=提交序，
    确认时已按组去重）。
  ② 绑图行残留：已传图行的原物核心词（该行实体名+特征词，词表与 strip_core_residue 同源：
    render.residue_words = 核心词去动作/方位/视角词后切 2-3 字组，≥3 字，AT-22 E1 R3）不得
    残留在提示词正文（附注/替换说明/实体自身标签行除外）。
  ③ 锁点对账：必换未换集合 == 确认时点『按原样』/『按原片』/empty_confirm 记录。独立从
    任务.json 快照核对（不自证）：assets_confirmed/empty_confirm/按原样确认/按原片确认
    都在确认动作落账，这里只读比对。
  ④ 组绑定一致性（AT-11）：确认组内行图号/URL 必须一致；同组两 URL→FAIL；同号不同
    URL→FAIL。
自检器自身异常 → 写一条 FAIL 记录如实落盘，绝不打断渲染主链（空响应不算成功的反面：
检查器挂了也算「未通过」，不许静默当 PASS）。
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
import traceback
import unicodedata
from bisect import bisect_right
from pathlib import Path

from . import replace, state

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
SELFCHECK_NAME = "自检.json"
PASS, FAIL = "PASS", "FAIL"
WARN = "WARN"                       # 拆解门禁条目三态（自检四项只有 PASS/FAIL）


# ---------------------------------------------------------------- 人话说明（AT-16 B2）
#
# 用户话（结论 + 原因 + 怎么处理）由自检生产者出，前端只渲染——红线 25：前端不得解析证据
# 文字重判。这里每条「说明」和「证据」都由**同一份检查器发现**装配：发现（finding）里带
# 行号/图号，文字既拼证据原文又喂话术，动态数字不靠前端再解析一遍。
#
# 处理指引只能指向**真实存在的入口**：正文里「…」包起来的具名，要么是详情页按钮/区域的
# 文案（task-detail-drawer.tsx 里出现），要么是清单列名（待确认清单.md 表头）。probe_at16_b2
# 把每条指引的具名逐个对账，编造的入口当场判红。

HOW_REBIND = ("点「退回置换」回到待置换区：对点到的行在上传区「重传（覆盖）」重新绑图"
              "（组内行在组卡上传，全组联动）；不要这张图就点「移除」按原片处理，再确认置换重渲染")
HOW_RECONFIRM = ("点「退回置换」后重新确认置换：发号按清单当前绑定重算，重算后才写进提示词"
                 "（重算后仍对不上，说明清单「资产图」列绑错了行，按上面点到的行「重传（覆盖）」或「移除」）")
HOW_ACK = ("点「退回置换」回到待置换区：给这些行传图，或逐行勾上「知道，按原样」（按原片沿用），再确认置换")
HOW_GROUP = ("在待置换区该组组卡上传区「重传（覆盖）」统一成同一张图（组内行全组联动）；"
             "同号不同 URL 就把多余那张点「清空」按原片沿用，再确认置换")
HOW_GATE = ("点详情底部「重试」重跑拆解与渲染（自动检查按重跑结果重算）；"
            "重跑也补不上 → 按上面点到的位置补素材或意图，再重跑一次")
# AT-22 E3 §4.6（出口替换 E-1）：② 残留的处理指引从「退回置换→改清单某一列」改成
# **本页行内闭环**——每条发现上就有「改这一行的描述」→「保存并重渲染」，写完自检自动重跑。
# 「退回置换」只作兜底（逐行改不动时）。文案里的「…」具名必须逐字存在于详情页源码
# （probe_at16_b2 ② 对账），且不得出现路径 / `.md` / 「磁盘」（E2/E3 双探针把关）。
HOW_DESC = ("在下面这条发现上点「改这一行的描述」，写完点「保存并重渲染」——"
            "提示词按新描述重出、自检自动重跑；逐行改不动就点「退回置换」回待置换区改（兜底）")

ORDER_KIND_WORD = {
    "发号": "图号跳号了",
    "正文号": "提示词里的 @图片N 和清单对不上",
    "提交条数": "提交的图数和该发的图数不一致",
    "组内多提交": "同一组多提交了一张",
    "次序错位": "提交的顺序和行对不上",
    "提交图不符": "提交的图不属于那一行",
}


# ---------------------------------------------------------------- finding v2（AT-22 E2）
#
# 病根（E2 §1.1）：同一条命中句在屏幕上出现 4 次（结论人话 → 原因 → 发现 → 证据），行号/图号
# 各重复 2-3 遍，且 4 次都是字段值（`#6`／`图1`／`词表`）而不是话。
#
# 做法：生产者把**给人看的那一份**结构化，前端只按槽位渲染（红线 25：不解析证据字符串）。
#   · `文字`／`证据` 内容一个字节都不改 —— 历史探针（probe_at9/at15_r2/at16_b1b）与排障锚点
#     都挂在它们上面；v2 只是**追加**字段。
#   · `原文` 与 `文字` 同值（检查器原句），是 v2 的显式键（新探针断言改查它，不再猜）。
#   · `说明.原因` v2 起不产出 —— `原因=_evidence(problems)` 等于 `发现` 的拼接，最大重复源。
#   · 无行号的整体问题（如「提交张数不对」）不带 `行号`，位置段由前端按「这次整体」变体渲染。
#
# 一个 finding 一个词（与 E4「保留这个词」逐词粒度对齐）；同一句的多条由**渲染层**按
# `行号+句序` 分组并成一张卡（分组键是结构化字段，前端不解析文字）。

# 项显示名（E2 §2）：契约键 `项` 保持不变（探针/前端识别用），另给一行人能读的名字。
ITEM_DISPLAY = {
    "次序对账": "图号与提交次序",
    "绑图行残留": "提示词里的老词",
    "锁点对账": "该换的没换",
    "组绑定一致性": "合并组的图是否一致",
    "拆解门禁": "拆解产物检查",
}

# 图号 → 「你传的××图」（E2 §2）：按该图号主行的类别取词；主行类别不在表里 → 「你传的图」。
KIND_WORD = {"主体": "产品图", "人物": "人物图", "道具": "道具图", "服装": "服装图",
             "场景": "场景图", "品牌视觉": "品牌图"}

# 透出给前端的 v2 字段（真值才带；None/空串/空列表/空对象不落盘，前端按缺省降级）。
SHOWN_V2_KEYS = ("行称呼", "类别", "组", "图称呼", "图引用", "词", "句序",
                 "上下文", "片段", "原文", "人话", "可修", "切片")


def _finding(kind: str, text: str, row: int | None = None, no: int | None = None, **v2) -> dict:
    """一条检查器发现：`文字`=证据原文（与旧格式逐字一致），其余是 v2 结构化槽位。

    `原文` 恒等于 `文字`（同一条检查器原句的显式键）；v2 槽位传进来是空值就不落键——
    前端只看「键在不在」，不猜空字符串是什么意思。
    """
    finding: dict = {"类型": kind, "文字": text, "行号": row, "图号": no}
    if text:
        finding["原文"] = text
    for key, value in v2.items():
        if key in SHOWN_V2_KEYS and value not in (None, "", [], {}):
            finding[key] = value
    return finding


def _evidence(findings: list[dict]) -> str:
    return "；".join(str(f["文字"]) for f in findings)


def _shown(findings: list[dict]) -> list[dict]:
    """透出给前端的结构化发现：行号/图号只带真值（None 不出现），v2 槽位真值才带。"""
    shown: list[dict] = []
    for f in findings:
        row: dict = {"文字": str(f["文字"])}
        if f.get("行号"):
            row["行号"] = int(f["行号"])
        if f.get("图号"):
            row["图号"] = int(f["图号"])
        for key in SHOWN_V2_KEYS:
            value = f.get(key)
            if value not in (None, "", [], {}):
                row[key] = value
        shown.append(row)
    return shown


def _say(结论人话: str, 处理: str = "", findings: list[dict] | None = None,
         统计: dict | None = None, 清单行: dict | None = None,
         原句: dict | None = None) -> dict:
    """「说明」对象：结论人话（一行）/怎么处理/结构化发现/折叠区数据——前端逐字渲染，不重判。

    v2 起**不写 `原因`**（= 发现的拼接，最大重复源，E2 §2）；`统计`／`清单行`／`原句` 只喂折叠区
    「展开看证据（内部明细）」，可见区一个数字统计都不许出现（E2 硬规则 3）。
    """
    said: dict = {"结论人话": 结论人话}
    if 处理:
        said["处理"] = 处理
    shown = _shown(findings or [])
    if shown:
        said["发现"] = shown
    if 统计:
        said["统计"] = 统计
    if 清单行:
        said["清单行"] = 清单行
    if 原句:
        said["原句"] = 原句
    return said


def _load_render():
    """按路径加载 pipeline/reverse/render.py（发号/词表同源；它不是包，同 cards._confirm 加载法）。"""
    path = ROOT / "pipeline" / "reverse" / "render.py"
    sys.path.insert(0, str(path.parent))          # render 内部 `from aspect import …` 同目录依赖
    spec = importlib.util.spec_from_file_location("wanrepro_reverse_render", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------- 位置段素材（v2 槽位数据源）


def _short_names(bridge_id: str, rows: list[dict]) -> dict[int, str]:
    """{清单行号: 人话短名}——与资产置换卡的 `短名` 一字不差（cards.short_names 同源）。

    短名起不出来（拆解.md 缺、侧车坏、旧任务）一律 {}：位置段降级「类别」→「第 N 行」，
    自检**不许**因为「起不出名字」而报错或漏报（E2 §11 风险表第一行）。
    """
    if not bridge_id or not rows:
        return {}
    try:
        from . import cards
        layers = cards._split(cards._summary_markdown(bridge_id))
        if not layers:
            return {}
        spans, _note = cards.load_spans(bridge_id)
        twins = {int(no): (payload.get("同实体疑似") or []) for no, payload in spans.items()}
        positions = {int(no): payload.get("画面位置") for no, payload in spans.items()}
        names = cards.short_names(rows, layers, twins, positions)
        return {int(no): str(name) for no, name in names.items() if name}
    except Exception:                             # noqa: BLE001 —— 短名是锦上添花，不许拖垮自检
        return {}


def _kind_word(kind: str) -> str:
    for prefix, word in KIND_WORD.items():
        if kind.startswith(prefix):
            return word
    return "图"


def _row_labels(rows: list[dict], ents, groups: list[list[int]] | None,
                names: dict[int, str]) -> dict[int, dict]:
    """行号 → v2 位置段素材：行称呼 / 类别 / 组 / 图号 / 图称呼 / 图引用。

    「图称呼」按该图号**主行**（同号行号最小者，与发号口径一致）的类别取词；
    主行类别不认识 → 「你传的图」。图引用 = 该行清单「资产图」首串（原样，local:… 或 https:…）。
    """
    cat_of: dict[int, str] = {}
    for row in rows or []:
        try:
            cat_of[int(row["no"])] = str(row.get("cat") or "")
        except (KeyError, TypeError, ValueError):
            continue
    no_of: dict[int, int] = {}
    ref_of: dict[int, str] = {}
    for ent in ents or []:
        try:
            row_no, no = int(getattr(ent, "row_no", 0) or 0), int(getattr(ent, "no", 0) or 0)
        except (TypeError, ValueError):
            continue
        if not row_no:
            continue
        if no:
            no_of[row_no] = no
        urls = [u.strip() for u in re.split(r"[,，]", str(getattr(ent, "image", "") or "")) if u.strip()]
        if urls:
            ref_of.setdefault(row_no, urls[0])
        if not cat_of.get(row_no):
            cat_of[row_no] = str(getattr(ent, "kind", "") or "")
    group_of: dict[int, list[int]] = {}
    for group in groups or []:
        members = sorted({int(n) for n in group})
        for n in members:
            group_of[n] = members
    rows_by_no: dict[int, list[int]] = {}
    for row_no, no in no_of.items():
        rows_by_no.setdefault(no, []).append(row_no)
    labels: dict[int, dict] = {}
    for row_no in sorted(set(cat_of) | set(no_of)):
        label: dict = {}
        name = names.get(row_no) or ""
        if name:
            label["行称呼"] = name
        kind = cat_of.get(row_no) or ""
        if kind:
            label["类别"] = kind
        members = group_of.get(row_no)
        if members and len(members) > 1:
            label["组"] = members
        no = no_of.get(row_no)
        if no:
            label["图号"] = no
            main_kind = cat_of.get(min(rows_by_no.get(no) or [row_no]), "")
            label["图称呼"] = "你传的" + _kind_word(main_kind)
        ref = ref_of.get(row_no)
        if ref:
            label["图引用"] = ref
        labels[row_no] = label
    return labels


def _v2_of(labels: dict[int, dict], row_no: int | None, **extra) -> dict:
    """取该行的 v2 槽位（图号另走 `no=` 参数，避免与 _finding 的位置参数撞名）。"""
    out = {k: v for k, v in (labels.get(row_no) or {}).items() if k != "图号"}
    out.update({k: v for k, v in extra.items() if v not in (None, "", [], {})})
    return out


def _row_phrase(row_no: int | None, labels: dict[int, dict]) -> str:
    """「第 6 行「道具B」」；行称呼/类别都缺 → 「第 6 行」；无行号 → 空串（调用方用整体变体）。"""
    if not row_no:
        return ""
    label = labels.get(int(row_no)) or {}
    name = label.get("行称呼") or label.get("类别") or ""
    return f"第 {int(row_no)} 行「{name}」" if name else f"第 {int(row_no)} 行"


def _rows_phrase(row_nos: list[int], labels: dict[int, dict]) -> str:
    """「第 1、5、6 行」；这些行短名一致 → 带上「道具B」（不再逐行重复同一个名字）。"""
    rows = sorted({int(n) for n in row_nos if n})
    if not rows:
        return "这次整体"
    names = {(labels.get(r) or {}).get("行称呼") or (labels.get(r) or {}).get("类别") or "" for r in rows}
    joined = f"第 {'、'.join(str(r) for r in rows)} 行"
    if len(names) == 1 and names.pop():
        return f"{joined}「{(labels.get(rows[0]) or {}).get('行称呼') or (labels.get(rows[0]) or {}).get('类别')}」"
    return joined


# ---------------------------------------------------------------- 切句（命中段/上下文/片段）


def _sentence_spans(body: str) -> list[tuple[int, int]]:
    """正文切句：`。！？；` 与换行都是边界；空白段不进表（句序只在有内容的句子上编号）。"""
    spans: list[tuple[int, int]] = []
    start = 0
    for index, ch in enumerate(body):
        if ch in "。！？；\n":
            if body[start:index].strip():
                spans.append((start, index + 1))
            start = index + 1
    if body[start:].strip():
        spans.append((start, len(body)))
    return spans


def _sentence_at(body: str, spans: list[tuple[int, int]], offset: int) -> tuple[int, str]:
    """命中偏移 → (句序 1 起, 命中句原文)。偏移落在句间空白 → (0, "")，调用方按无句处理。"""
    for index, (start, end) in enumerate(spans, 1):
        if start <= offset < end:
            return index, body[start:end].strip()
    return 0, ""


def _sentence_block(body: str, spans: list[tuple[int, int]], finding: dict) -> dict:
    """折叠区「提示词里的原句」：命中句所在整段（前后各带 1 句）+ 句序/行号；无句序 → {}。"""
    try:
        index = int(finding.get("句序") or 0)
    except (TypeError, ValueError):
        return {}
    if index <= 0 or index > len(spans):
        return {}
    lo, hi = max(0, index - 2), min(len(spans), index + 1)
    block = {"句序": index, "段": "".join(body[s:e] for s, e in spans[lo:hi]).strip()}
    if finding.get("行号"):
        block["行号"] = int(finding["行号"])
    return block


def _segments(sentence: str, words: list[str]) -> list[dict]:
    """同一句的分段高亮：每段带 `起`/`止`（在 `上下文` 里的字符偏移），前端只按偏移上色。

    重叠词（「下角入」/「角入画」互为子串且同区间）在**数据层不并词**（E4 要逐词保留），
    渲染层按偏移并集叠高亮即可——所以这里给出区间，而不是拼好的一整串。
    """
    ranges: list[tuple[int, int]] = []
    for word in words:
        for match in re.finditer(re.escape(word), sentence):
            ranges.append((match.start(), match.end()))
    ranges.sort()
    merged: list[tuple[int, int]] = []
    for start, end in ranges:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    out: list[dict] = []
    pos = 0
    for start, end in merged:
        if start > pos:
            out.append({"文": sentence[pos:start], "起": pos, "止": start, "高亮": False})
        out.append({"文": sentence[start:end], "起": start, "止": end, "高亮": True})
        pos = end
    if pos < len(sentence):
        out.append({"文": sentence[pos:], "起": pos, "止": len(sentence), "高亮": False})
    return out


def _checklist_lines(ck_path: Path) -> dict[int, str]:
    """清单行原文（折叠区「这一行的清单原文」块）：首格是行号的表格行，读不出 → {}。"""
    try:
        text = Path(ck_path).read_text(encoding="utf-8-sig")
    except OSError:
        return {}
    out: dict[int, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if cells and cells[0].isdigit():
            out[int(cells[0])] = line
    return out


def _lines_block(rows_wanted: list[int], lines: dict[int, str]) -> dict:
    """折叠区「这一行的清单原文」：{行号: 整行 md 原文}（只收真行；读不出就不给这块）。"""
    out = {str(int(n)): lines[int(n)] for n in sorted({int(n) for n in rows_wanted if n}) if int(n) in lines}
    return out


def run_selfcheck(task_dir: Path) -> list[dict]:
    """渲染完成后调用：三项检查 → 落盘 自检.json → 返回条目（get_task 原样透出）。"""
    try:
        items = _check_all(task_dir)
    except Exception as e:  # noqa: BLE001 —— 检查器异常也如实落盘（FAIL），不打断渲染主链
        detail = f"自检器异常（渲染主链不受影响）: {e}；{''.join(traceback.format_exc().splitlines()[-2:])}"
        items = [{"项": "自检执行", "结论": FAIL, "证据": detail,
                  "项显示名": "自检本身",
                  "说明": _say("自检没跑起来（检查器自己异常了）：这次的图没有机械核对过，别当已通过",
                              处理="点详情底部「重试」重跑拆解与渲染让自检重跑；重复出现就把这条证据发给开发")}]
    path = task_dir / SELFCHECK_NAME
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return items


def _fold_report_item(item: dict) -> dict:
    """读取层折叠兜底（AT-23 B1 返工）：**存量任务**的 自检.json 落盘早于折叠落地，读的时候收口。

    病根同上（`_fold_contained_words`）：折叠最初只挂在「新跑自检」的产出路径，存量任务
    （auto-0067/0068 的 自检.json 是折叠前落盘：同句 6 词并存、统计.词数=6、无展示词数留痕）
    经 GET /api/hub/task 原样透出 → 真机红字仍列 6 词、6 个「保留这个词」。重跑存量任务自检
    是禁止动作（真实任务只读），所以兜底只能落在**读取层**这一个出口（`load_selfcheck`，
    hub_api 三处「自检」组装全走它）。

    幂等判据：`统计.判定词数` 已是折叠后产出的留痕键——键在就说明这份载荷是新格式，
    原样透出（不重复折叠、不留第二次痕）。旧载荷里没有任何词被包含（没折掉一条）时也原样
    透出：折叠是展示收口，不是给干净数据添痕。

    改的只有展示侧：`发现` 折成完整原词 + `切片`、`统计` 补 `判定词数`（=判定集词数，
    一个不少）与折叠后的 `词数`、`结论人话` 里的「还留着 N 个」按展示集改口。`证据` 一字不动
    ——那是检查器原始输出（判定证据），只进折叠区「检查器原始输出」。
    """
    if item.get("项") != "绑图行残留" or item.get("结论") != FAIL:
        return item
    say = item.get("说明")
    if not isinstance(say, dict):
        return item
    findings = say.get("发现")
    if not isinstance(findings, list) or len(findings) < 2:
        return item
    stats = say.get("统计") if isinstance(say.get("统计"), dict) else {}
    if FOLD_STAT_KEY in stats:
        return item                                  # 新产出已折叠：幂等跳过
    if not all(isinstance(f, dict) and f.get("词") for f in findings):
        return item                                  # 有非词类发现：不猜，原样透出
    hits = [dict(f) for f in findings]
    folded = _fold_contained_words(hits)
    if len(folded) >= len(hits):
        return item                                  # 一句里没有包含关系：不用收口
    new_stats = dict(stats)
    new_stats["词数"] = len(folded)
    new_stats[FOLD_STAT_KEY] = len(hits)
    new_stats["句数"] = len({(f.get("行号"), f.get("句序")) for f in folded})
    new_stats[FOLD_SOURCE_KEY] = "读取层（存量任务的自检.json 早于折叠落地，未重跑自检）"
    new_say = dict(say)
    new_say["发现"] = folded
    new_say["统计"] = new_stats
    head = str(say.get("结论人话") or "")
    if head:
        new_say["结论人话"] = FOLD_HEAD_COUNT.sub(lambda m: f"{m.group(1)}{len(folded)}{m.group(2)}", head)
    out = dict(item)
    out["说明"] = new_say
    return out


def load_selfcheck(task_dir: Path) -> list[dict] | None:
    """读 自检.json（get_task 透出用）；无文件/坏文件/非列表 → None（前端显「未检查」）。

    AT-15 R2：追加一条「拆解门禁」合成条目（只读 analysis/reverse/<bridge>/门禁.json，
    不落盘、不改 B5 自检合同的原有四项）——门禁数据是渲染后门禁步落的另一份文件，
    前端自检面板只认 自检.json 这一束，所以在读路径汇总（白名单判据汇总位）。

    AT-23 B1 返工：透出前做一次**读取层折叠兜底**（`_fold_report_item`）——存量任务不重跑
    自检也能看到「一句话一个完整原词」，新格式载荷幂等直通。后端唯一出口，前端零改动。
    """
    items: list[dict] | None = None
    path = task_dir / SELFCHECK_NAME
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            data = None
        if isinstance(data, list):
            rows = [_fold_report_item(row) for row in data if isinstance(row, dict)]
            items = rows or None
    gate = load_gate_item(task_dir)
    if gate is not None:
        items = (items or []) + [gate]
    return items


# 门禁缺项/告警 → 人话（E2 §6 对照表）：只换**已知的两条**检查项说法，其余保留原文去掉代号前缀。
GATE_WORDS = (
    (r"^主列表行缺画面位置[：:]\s*第\s*(\d+)\s*行.*?判定\s*(\d+)\s*行\s*/\s*共\s*(\d+)\s*行",
     "第 {0} 行没标出画面位置（{2} 行里只标了 {1} 行）"),
    (r"^主列表行缺自身框[：:]\s*第\s*(\d+)\s*行.*", "第 {0} 行的定位框没生成（画面位置没指向代表帧或超出九宫格）"),
)
GATE_CODE_PREFIX = re.compile(r"^[A-Z]\d+\s+[^：:]{1,20}[：:]\s*")


def _gate_finding(text: str, kind: str) -> dict:
    """门禁一条缺项/告警 → 一条结构化发现：`人话` 给人看，`原文` 是检查器原句（折叠区用）。"""
    raw = str(text or "").strip()
    body = GATE_CODE_PREFIX.sub("", raw)
    human = ""
    for pattern, template in GATE_WORDS:
        match = re.search(pattern, body)
        if match:
            human = template.format(*[g or "" for g in match.groups()])
            break
    if not human:
        human = body
    return _finding(kind, f"{kind}：{raw}", row=None, no=None, 人话=human)


GATE_ITEM_NAME = "拆解门禁"          # 前端按 项==GATE_ITEM_NAME 识别并单独渲染红/绿条


def _gate_bridge_id(task_dir: Path) -> str:
    """任务号 → 桥 id（只读 任务.json 单键；state.load 的校验在读路径上太重）。"""
    try:
        data = json.loads((Path(task_dir) / "任务.json").read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return ""
    return str(data.get("bridge_id") or "").strip()


def load_gate_item(task_dir: Path) -> dict | None:
    """analysis/reverse/<bridge>/门禁.json → 自检面板一条 {项,结论,证据}；无文件 → None。

    结论三态沿用门禁自己的 verdict（PASS/WARN/FAIL）；证据第一行是摘要（状态/绿项数/
    重拆轮次），缺项与告警逐条排在后面——前端只渲染，不解释（红线 25：渲染承接既有自检位）。
    根走 `replace.analysis_root()`（认 WANREPRO_ANALYSIS_ROOT）：与清单同一真源，隔离探针
    才不会「读 A 写 B」。
    """
    bridge_id = _gate_bridge_id(task_dir)
    if not bridge_id:
        return None
    gate_path = replace.analysis_root() / bridge_id / "门禁.json"
    if not gate_path.is_file():
        return None
    try:
        gate = json.loads(gate_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        gate = None
    if not isinstance(gate, dict):
        return {"项": GATE_ITEM_NAME, "结论": FAIL, "项显示名": ITEM_DISPLAY[GATE_ITEM_NAME],
                "证据": "门禁.json 读不出（拆解产物门禁留痕损坏）——如实标红，重跑拆解可重建",
                "说明": _say("拆解产物的门禁留痕读不出来（文件损坏），这次拆解有没有过门禁无据可查",
                            处理=HOW_GATE)}
    result = gate.get("结果") if isinstance(gate.get("结果"), dict) else {}
    verdict = str(result.get("verdict") or "").strip().upper()
    checks = result.get("checks") if isinstance(result.get("checks"), list) else []
    total = len(checks)
    passed = sum(1 for item in checks if isinstance(item, dict) and item.get("verdict") == PASS)
    missing = [str(x) for x in (result.get("缺项") or []) if str(x).strip()]
    warns = [str(x) for x in (result.get("告警") or []) if str(x).strip()]
    status = str(gate.get("状态") or "").strip()
    rounds = gate.get("轮次") if isinstance(gate.get("轮次"), int) else 0
    limit = gate.get("上限") if isinstance(gate.get("上限"), int) else "?"
    if not verdict:
        verdict = FAIL if "标红" in status else (WARN if "告警" in status else PASS)
    lines = [f"状态 {status or '未知'}｜绿项 {passed}/{total}"
             f"｜缺项 {len(missing)}｜告警 {len(warns)}｜重拆 {rounds}/{limit}"]
    lines += [f"❌ {item}" for item in missing]
    lines += [f"⚠ {item}" for item in warns]
    # 人话（AT-16 B2 → AT-22 E2 重构）：结论人话只说「几项没过、下一步点哪」，统计进折叠区；
    # 缺项/告警逐条人话化（去 `D2 画面位置：` 这类代号开头的写法），检查器原句留在 `原文`。
    stats = {"通过": passed, "没通过": len(missing), "提醒": len(warns),
             "已重拆": rounds, "上限": limit, "共检查": total}
    found = [_gate_finding(item, "缺项") for item in missing] + [_gate_finding(item, "告警") for item in warns]
    if verdict == PASS:
        说明 = _say("拆解产物的检查都过了，没有要补的东西", 处理="无需处理", 统计=stats)
    else:
        if verdict == WARN:
            head = f"拆解只出了 {len(warns)} 条提醒，不挡后面的流程"
        else:
            head = f"拆解没通过 {len(missing)} 项自动检查，可以先点「重试」重跑拆解"
        说明 = _say(head, 处理=HOW_GATE, findings=found, 统计=stats)
    return {"项": GATE_ITEM_NAME, "结论": verdict, "证据": "\n".join(lines),
            "项显示名": ITEM_DISPLAY[GATE_ITEM_NAME], "说明": 说明}


# ---------------------------------------------------------------- 三项

def _check_all(task_dir: Path) -> list[dict]:
    task = state.load(task_dir)
    bridge_id = str(task.get("bridge_id") or "")
    # AT-21 B0：清单按任务作用域取——fork 任务读它自己的私有副本（源任务清单永不被 fork 碰）；
    # 其余任务仍是 `analysis/reverse/<bridge>/待确认清单.md`（同一真源，认 WANREPRO_ANALYSIS_ROOT）。
    scope = replace.private_scope(str(task.get("local_id") or task_dir.name), task)
    try:
        ck_path = replace.checklist_path(bridge_id, scope)
    except replace.ReplaceError as exc:
        raise FileNotFoundError(f"{exc}（自检以清单为行号真源）") from None
    checklist_md = ck_path.read_text(encoding="utf-8-sig")
    prompt_path = task_dir / "提示词.md"
    prompt = prompt_path.read_text(encoding="utf-8") if prompt_path.is_file() else ""

    rows = replace.parse_rows(checklist_md)
    must_rows = sorted({int(r["no"]) for r in rows if "必须换" in (r.get("advice") or "")})

    render = _load_render()
    ents = render.parse_checklist(ck_path)
    # AT-11 B1：发号同源重算带组/行决策覆盖（与 render 主入口同一 sidecar 数据源）
    groups = replace.load_groups(bridge_id, scope)
    decisions = replace.load_row_decisions(bridge_id, scope)
    render.assign_numbers(ents, groups, decisions)
    row_to_no = {int(e.row_no): int(e.no) for e in ents if e.no}

    submitted = [(int(x.get("no") or 0), str(x.get("url") or ""))
                 for x in (task.get("assets_confirmed") or []) if isinstance(x, dict)]
    ack_rows = {int(x) for x in ((task.get("按原样确认") or {}).get("rows") or [])}
    ko_rows = {int(x) for x in ((task.get("按原片确认") or {}).get("rows") or [])}   # AT-11 B2 确认时点快照
    empty_confirm = bool(task.get("empty_confirm"))

    body = _prompt_body(prompt)
    # v2 位置段素材（AT-22 E2）：短名/类别/组/图号/图称呼/图引用 + 清单行原文。
    # 起不出来一律降级为空（不阻断自检），只影响位置段详细程度。
    labels = _row_labels(rows, ents, groups, _short_names(bridge_id, rows))
    lines = _checklist_lines(ck_path)
    return [
        _check_order(body, ents, row_to_no, submitted, ck_path, groups, labels, lines),
        # AT-22 E4：白名单取**同一份任务快照**（上面 state.load 那一次），判定层不新增读盘——
        # 否则自检跑到一半任务文件被改，会得到「半新半旧」的豁免口径。
        _check_residue(body, ents, render, bridge_id, labels, lines,
                       whitelist=task.get(WHITELIST_FIELD)),
        _check_lock(rows, must_rows, submitted, ack_rows, empty_confirm, ko_rows, groups, labels, lines),
        _check_groups(ents, row_to_no, submitted, groups, labels, lines),
    ]


def _prompt_body(prompt: str) -> str:
    """正文 = 提示词全文挖掉「# 附注」（贴平台前要删的人读节）与「## 本片替换说明」（meta 注记）。"""
    lines: list[str] = []
    skip_heading: tuple[str, ...] = ()
    for raw in prompt.splitlines():
        head = raw.strip()
        if head.startswith("# 附注"):
            skip_heading = ("# ", )
            continue
        if head.startswith("## 本片替换说明"):
            skip_heading = ("## ", "- ")
            continue
        if skip_heading:
            if not head or head.startswith(skip_heading):
                continue
            skip_heading = ()          # 遇到别的内容 = 该节结束，恢复扫描
        lines.append(raw)
    return "\n".join(lines)


def _check_order(body: str, ents, row_to_no: dict[int, int],
                 submitted: list[tuple[int, str]], ck_path: Path,
                 groups: list[list[int]] | None = None,
                 labels: dict[int, dict] | None = None,
                 lines: dict[int, str] | None = None) -> dict:
    """① 次序对账：行号→图号（assign_numbers 同源重算，组内 N 行共用 1 图号——AT-11 B1）
    → 提交次序（确认快照列表序=提交序，确认时已按组去重）。"""
    groups = groups or []
    labels = labels or {}
    lines = lines or {}
    refs = sorted({int(n) for n in re.findall(r"@图片(\d+)", body)})
    url_by_row = {int(e.row_no): [u.strip() for u in re.split(r"[,，]", e.image) if u.strip()]
                  for e in ents if e.image}
    stats = {"要发张数": len(set(row_to_no.values())), "提交张数": len(submitted),
             "正文图号": refs, "行→图号": {str(k): v for k, v in sorted(row_to_no.items())},
             "确认组": groups}
    if not row_to_no and not refs and not submitted:
        return {"项": "次序对账", "结论": PASS, "项显示名": ITEM_DISPLAY["次序对账"],
                "证据": "全沿用/无图：清单无绑图行、正文无 @图片N、提交快照为空——三方一致",
                "说明": _say("全沿用、没换图：清单没有绑图行、正文没有 @图片N、提交快照为空——三方一致",
                            处理="无需处理", 统计=stats)}
    problems: list[dict] = []                       # 同一份发现：文字→证据原文，行号/图号→人话
    nums = sorted(set(row_to_no.values()))          # 唯一图号集合（组内多行同号算一个）
    if nums != list(range(1, len(nums) + 1)):
        problems.append(_finding("发号", f"发号不连续: row→图号={row_to_no}",
                                 人话="图号没从 1 开始连着排（中间跳号了）"))
    if refs != nums:
        problems.append(_finding("正文号", f"正文图号 {refs} ≠ 应发号 {nums}",
                                 人话="提示词里写的 @图片N 和清单该发的图号对不上"))
    if len(submitted) != len(nums):
        problems.append(_finding("提交条数",
                                 f"提交快照 {len(submitted)} 条 ≠ 发图单位 {len(nums)} 个（组内多行应只提交一条）",
                                 人话=f"该发 {len(nums)} 张，实际提交了 {len(submitted)} 张"))
    else:
        seen_units: set[tuple] = set()
        for i, (no, url) in enumerate(submitted, 1):
            unit = replace.row_unit(no, groups)
            if unit in seen_units:
                problems.append(_finding("组内多提交",
                                         f"提交第 {i} 张来自行 {no}，但该行所在单位已有提交（组内多行多提交）",
                                         row=no, no=i,
                                         **_v2_of(labels, no, 人话="这一行所在的组已经交过一张了，这里又交了一张")))
            seen_units.add(unit)
            if row_to_no.get(no) != i:
                problems.append(_finding("次序错位",
                                         f"提交第 {i} 张来自行 {no}，但发号映射给该行的是图{row_to_no.get(no)}（应为图{i}）",
                                         row=no, no=row_to_no.get(no),
                                         **_v2_of(labels, no,
                                                  人话=f"这一行该排在第 {row_to_no.get(no)} 张提交，实际排在第 {i} 张")))
            if url and url not in url_by_row.get(no, []):
                problems.append(_finding("提交图不符",
                                         f"提交第 {i} 张 URL 不在清单第 {no} 行绑图里: {url[:60]}",
                                         row=no, no=i,
                                         **_v2_of(labels, no, 人话="提交的这张图不在这一行的清单绑图里")))
    ev = (f"行→图号={row_to_no}；正文@图片N={refs}；提交次序={[no for no, _ in submitted]}"
          f"（清单 {ck_path.parent.name}）")
    if not problems:
        return {"项": "次序对账", "结论": PASS, "项显示名": ITEM_DISPLAY["次序对账"],
                "证据": "三方映射一致：" + ev,
                "说明": _say("发号、提示词里的 @图片N、提交次序三方对得上，没有错位的图",
                            处理="无需处理", 统计=stats)}
    kinds = {p["类型"] for p in problems}
    words = list(dict.fromkeys(ORDER_KIND_WORD[k] for k in (p["类型"] for p in problems) if k in ORDER_KIND_WORD))
    how: list[str] = []
    if kinds & {"发号", "正文号"}:
        how.append(HOW_RECONFIRM)
    if kinds & {"提交条数", "组内多提交", "次序错位", "提交图不符"}:
        how.append(HOW_REBIND)
    if kinds == {"提交条数"}:
        # E2 §7 样例 3：一眼能指出「一张图都没提交」，而不是「对不上：1 处」
        head = f"你确认置换时提交了 {len(submitted)} 张图，这次要发 {len(nums)} 张"
        grouped = [n for n in nums if len([r for r, v in row_to_no.items() if v == n]) > 1]
        if grouped:
            members = [r for r in row_to_no if row_to_no[r] == grouped[0]]
            head += f"（{_rows_phrase(members, labels)}合并成一张）"
    else:
        head = f"发的图和提交的图没对上：{len(problems)} 处——" + "、".join(words or ["对不上号"])
    return {"项": "次序对账", "结论": FAIL, "项显示名": ITEM_DISPLAY["次序对账"],
            "证据": _evidence(problems) + "｜" + ev,
            "说明": _say(head, 处理="；".join(how) or HOW_RECONFIRM, findings=problems,
                        统计=stats, 清单行=_lines_block([p.get("行号") for p in problems], lines))}


WHITELIST_FIELD = "selfcheck_whitelist"
WHITELIST_VERSION = 1
WHITELIST_MAX = 50                                  # 单任务上限（触顶 = 词表口径有病的信号）
WHITELIST_WORD_MIN, WHITELIST_WORD_MAX = 3, 20      # V2 同值（残留词表只收 ≥3 字）


def word_key(word: str) -> str:
    """词键（唯一判定口径）：NFKC + strip + casefold。

    现状词表全是汉字（`entity_noun_core` 已 strip 非汉字），词键与原文相同；存下来是为了 E1 将来
    放宽到含字母/全角时不用改数据，也让「条目为什么失效」可 diff、可排障。
    """
    return unicodedata.normalize("NFKC", str(word or "")).strip().casefold()


def whitelist_entries(container) -> list[dict]:
    """`任务.json.selfcheck_whitelist` → 条目列表（畸形/缺字段一律丢弃，不猜、不补默认行）。

    `空列表 = 没豁免`；老任务没有这个键 → `[]`。读侧只认 (`行`, `词键`) 两列真值，别的字段
    （来源/图号/上下文/加入于）都是证据，缺失不影响判定。
    """
    if not isinstance(container, dict):
        return []
    raw = container.get("条目")
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for it in raw:
        if not isinstance(it, dict):
            continue
        try:
            row = int(it.get("行") or 0)
        except (TypeError, ValueError):
            continue
        key = word_key(it.get("词键") or it.get("词") or "")
        if row < 1 or not key:
            continue
        out.append({**it, "行": row, "词键": key})
    return out


def row_word_closure(bridge_id: str, list_task: str | None = None) -> dict[int, dict]:
    """每行的残留词表闭包（服务端校验白名单的**同一口径**：render.residue_words，不另写一份）。

    返回 {行号: {"类别", "已传图", "不查", "图号", "词": {词键: 词}, "来源": {词键: core|gram}}}。
    写路径的 V1/V3/V4 全查这张表——「词必须在这一行本次的词表里」是防整句/任意词混进来的硬闸。
    `list_task`（AT-21 B0）：fork 任务传自己（读私有清单副本），其余传 None（读共享清单）。
    """
    out: dict[int, dict] = {}
    if not bridge_id:
        return out
    render = _load_render()
    try:
        ck_path = replace.checklist_path(bridge_id, list_task)
    except replace.ReplaceError:
        return out
    if not ck_path.is_file():
        return out
    ents = render.parse_checklist(ck_path)
    render.assign_numbers(ents, replace.load_groups(bridge_id, list_task),
                          replace.load_row_decisions(bridge_id, list_task))
    for e in ents:
        core = render.residue_core(e)
        words = render.residue_words(e)
        out[int(e.row_no)] = {
            "类别": str(e.kind),
            "已传图": bool(e.no and e.has_asset_image),
            "不查": e.kind.startswith("品牌视觉"),
            "图号": int(e.no or 0),
            "词": {word_key(w): w for w in words},
            "来源": {word_key(w): ("core" if w == core else "gram") for w in words},
        }
    return out


def _split_exempt(row_no, words: list[str], exempt: set[tuple[int, str]]) -> tuple[list[str], list[str]]:
    """一行原物词表 → (保留待查的, 被豁免的)。**唯一过滤点**：词表已成型、还没与正文比对。"""
    kept: list[str] = []
    dropped: list[str] = []
    for word in words:
        (dropped if (int(row_no), word_key(word)) in exempt else kept).append(word)
    return kept, dropped


def _exempt_ledger(entries: list[dict], ents, render, bound_rows: set[int],
                   body: str) -> list[dict]:
    """豁免台账（落 `自检.json` item② 的 `豁免`）：每条白名单这次到底管没管用，逐条说清。

    失效条目**一律不自动删**（方案 §3.3）：自动删会在用户改回去时静默丢掉裁量；灰显 + 可撤销
    把决定权留在人手里。五种状态与 UI 表现同表：
      生效 / 失效-正文无此词 / 失效-行没绑图 / 失效-行不存在 / 未参与-不在词表
    """
    by_row: dict[int, list] = {}
    for e in ents:
        by_row.setdefault(int(e.row_no), []).append(e)
    ledger: list[dict] = []
    for it in entries:
        row, key = int(it["行"]), str(it["词键"])
        shown = str(it.get("词") or key)
        item: dict = {"行": row, "词": shown, "词键": key}
        if it.get("来源"):
            item["来源"] = str(it["来源"])
        if it.get("图号"):
            item["图号"] = int(it["图号"])
        if it.get("上下文"):
            item["上下文"] = str(it["上下文"])[:40]
        if row not in by_row:
            item.update({"状态": "失效-行不存在", "原因": "清单里没有这一行了（清单重生成或删改过）"})
        elif row not in bound_rows:
            item.update({"状态": "失效-行没绑图", "原因": "这一行现在没有绑图，没有残留可豁免"})
        else:
            words = [w for e in by_row[row] for w in render.residue_words(e)]
            hit = next((w for w in words if word_key(w) == key), "")
            if not hit:
                item.update({"状态": "未参与-不在词表", "原因": "词表口径已不含这个词，条目无害"})
            elif not re.search(re.escape(hit), body):
                item.update({"状态": "失效-正文无此词", "原因": "正文里已经没有这个词（改过描述后会失效）"})
            else:
                item.update({"状态": "生效", "词": hit, "原因": ""})
        ledger.append(item)
    return ledger


def _exempt_note(active: list[dict], stale: list[dict]) -> str:
    """豁免台账 → 证据里那一句。**永远可见**（不许静默通过），且只列已生效的，失效另起一句。"""
    parts: list[str] = []
    if active:
        shown = "、".join(f"#{it['行']}「{it['词']}」" for it in active[:5])
        parts.append(f"已按你的裁量保留 {len(active)} 个词：{shown}"
                     + (f" 等 {len(active)} 个" if len(active) > 5 else ""))
    if stale:
        kinds = "、".join(dict.fromkeys(str(it["状态"]) for it in stale))
        parts.append(f"{len(stale)} 个豁免条目已失效（{kinds}）")
    return "；".join(parts)


def _own_label_line(line: str, names: set[str]) -> bool:
    """实体自身标签行（AT-22 E1 R3）：`<命名>（<形态>）` 的孤立定义行。

    旧模板 lumi-4section 把实体定义行整行写进正文，词表在这行上是**自指**（「粉红色标签款（透明圆柱形
    喷雾小小物体）」命中的就是它自己的命名与形态词），按设计该出现，不构成残留证据。
    """
    s = line.strip()
    for name in names:
        if not name or not s.startswith(name):
            continue
        rest = s[len(name):].strip()
        if rest.startswith("（") and rest.endswith("）") and len(rest) <= 30:
            return True
    return False


FOLD_STAT_KEY = "判定词数"          # 判定集词数留痕（折叠后产出必带；读取层兜底也以它为幂等判据）
FOLD_SOURCE_KEY = "折叠来源"        # 只在读取层兜底时落（存量载荷被收口时才出现）
FOLD_HEAD_COUNT = re.compile(r"(还留着\s*)\d+(\s*个要换掉的原物词)")


def _fold_contained_words(hits: list[dict]) -> list[dict]:
    """把同一「行×句」的命中词按**包含关系折叠**成展示集（AT-23 B1：判定集不动，展示集取最大词）。

    病根：`residue_words()` 为了**匹配**把原词展开成 core ∪ 2/3 字组（只留 ≥3 字）——7 字原词
    必然产出 5 个 3 字切片（「裹亮橙色标签款」→ 亮橙色/橙色标/裹亮橙/标签款/色标签）。判定用完整
    集合是对的（AT-22 B1 基线：命中 214 / 真残留 213 / 误报 ≤1），错在把它原样当**展示集**透出：
    一句话里冒出 6 个『』、6 个「保留这个词」，其中 5 个是同一个残留的另外 5 种切法。

    做法：只在**报告层**折叠——同一 行×句 内保留「不被其他命中词包含」的词（=完整原词），
    被折叠的切片按「包含它的最短保留词」归属，写进该条 finding 的 `切片`（只进折叠区
    「同一个词的 N 种切法」）。判定侧一行不动（`residue_words` 一个词不少），命中/漏报与
    AT-22 B1 基线逐字一致；`selfcheck_whitelist` 的键仍是 行×词键，合并后写的是完整原词
    （服务端闭包校验走 `residue_words`，完整原词∈集合 → E4 语义零变化）。

    切片的排列按它在完整原词里的**起始位置**定序——`residue_words` 里等长词的先后来自 set 迭代，
    展示口径不能跟着 Python 哈希漂；按起始位置排也正是人读「滑窗切法」时的顺序。
    """
    groups: dict[tuple[int, int], list[tuple[int, dict]]] = {}
    for index, hit in enumerate(hits):
        key = (int(hit.get("行号") or 0), int(hit.get("句序") or 0))
        groups.setdefault(key, []).append((index, hit))
    folded: dict[int, list[str]] = {}          # 保留词的 hits 下标 → 被折叠的切片
    dropped: set[int] = set()                  # 被折叠掉的 hits 下标
    for group in groups.values():
        words = [str(hit.get("词") or "") for _, hit in group]
        keep = [pos for pos, word in enumerate(words)
                if not any(other != word and word in other for other in words)]
        kept = set(keep)
        hosts: dict[int, list[str]] = {}
        for pos, word in enumerate(words):
            if pos in kept or not word:
                continue
            candidates = [at for at in keep if word in words[at]]
            if not candidates:
                continue                        # 理论上不会（包含链的终点必是保留词），防御性跳过
            hosts.setdefault(min(candidates, key=lambda at: (len(words[at]), at)), []).append(word)
        for at, slices in hosts.items():
            folded[group[at][0]] = sorted(set(slices), key=lambda w: (words[at].find(w), w))
        dropped.update(group[pos][0] for pos in range(len(group)) if pos not in kept)
    out: list[dict] = []
    for index, hit in enumerate(hits):
        if index in dropped:
            continue
        item = dict(hit)
        if folded.get(index):
            item["切片"] = folded[index]
        out.append(item)
    return out


def _check_residue(body: str, ents, render, bridge_id: str = "",
                   labels: dict[int, dict] | None = None,
                   lines: dict[int, str] | None = None,
                   whitelist: dict | None = None) -> dict:
    """② 绑图行残留：已传图行的原物核心词（实体名+特征词）零残留（词表=strip_core_residue 同源）。

    词表只收 ≥3 字的组：2 字组（香水/黑色这类品类与颜色词）不是特征词——我们的产品
    常与原物同品类（香水换香水），别的行描述也常带同色词，2 字组必误红（实测见探针正例）。

    AT-22 E1 R3（判定精度）：取词走 `render.residue_words`（动作/方位/视角词不进表、通用占位名
    不建表），匹配时跳过实体自身标签行——五类源结构的误报由此清零（66 任务实测 19→1，
    剩余 1 条「品类泛化词撞拍摄手法句」靠 E4 用户白名单兜底）。**删词不跳行**：混合名里的
    实物词照旧参与判定，「入画式香水瓶」仍拦。

    AT-22 E2（呈现）：每条命中带 v2 槽位——词/句序/上下文/片段（同句多词在渲染层并成一张卡）、
    行称呼/图称呼/图引用（位置段与缩略图）、可修（E3 的动作声明）；`文字` 仍是检查器原句，`证据` 不变。

    AT-22 E4（豁免）：`whitelist` = 任务.json 的 `selfcheck_whitelist`（调用方已在同一份任务快照里
    读过，判定层**不新增读盘**）。过滤点唯一：词表成型后、与正文比对前由 `_split_exempt` 摘词；
    摘掉的词按台账记进 `豁免`（生效/失效/未参与），命中项转 PASS 时必须把豁免写进证据——豁免是
    **可见状态**，不是隐形通过。判定只认 (行, 词键)，图号不参与。
    """
    labels = labels or {}
    lines = lines or {}
    entries = whitelist_entries(whitelist)
    exempt = {(int(it["行"]), str(it["词键"])) for it in entries}
    bound = [e for e in ents
             if not e.kind.startswith("品牌视觉") and e.no and e.has_asset_image]
    names = {e.name for e in bound if e.name}
    starts = [0] + [m.end() for m in re.finditer(r"\n", body)]
    sent_spans = _sentence_spans(body)

    def line_at(offset: int) -> str:
        idx = bisect_right(starts, offset) - 1
        end = body.find("\n", offset)
        return body[starts[max(idx, 0)]:end if end != -1 else len(body)]

    scanned: list[str] = []
    hits: list[dict] = []
    for e in bound:
        kept, _dropped = _split_exempt(e.row_no, render.residue_words(e), exempt)
        scanned.append(f"#{e.row_no}→图{e.no}({len(kept)}词)")
        for w in kept:
            for m in re.finditer(re.escape(w), body):
                if _own_label_line(line_at(m.start()), names):
                    continue                    # 自指标签行不算残留证据，继续找后续真正文位置
                left = max(0, m.start() - 10)
                order_no, sentence = _sentence_at(body, sent_spans, m.start())
                hits.append(_finding("残留",
                                     f"#{e.row_no} 的「{w}」残留于正文: …{body[left:m.end() + 10]}…",
                                     row=e.row_no, no=e.no,
                                     **_v2_of(labels, e.row_no, 词=w, 句序=order_no or None,
                                              上下文=sentence or None,
                                              片段=_segments(sentence, [w]) if sentence else None,
                                              可修={"类型": "改描述"})))
                break
    ledger = _exempt_ledger(entries, ents, render, {int(e.row_no) for e in bound}, body)
    # AT-23 B1（质检面板就地处理闭环 第 3 批）：判定集 → 展示集只在**这里**收口。
    # 上面 `hits` 是按完整 `residue_words()` 逐词命中（判定口径，一个词不少）；这里按同一
    # 「行×句」的包含关系折叠成完整原词 + `切片`（判定数留进统计，证据可复核）。
    judgment_words = len(hits)
    hits = _fold_contained_words(hits)
    active = [it for it in ledger if it["状态"] == "生效"]
    stale = [it for it in ledger if it["状态"] != "生效"]
    note = _exempt_note(active, stale)
    ev = f"已传图行词表 {len(scanned)} 行（{'、'.join(scanned) or '无'}），正文 {len(body)} 字"
    if note:
        ev += "；" + note
    stats = {"词表行数": len(scanned), "正文字数": len(body),
             "行→图号": {str(e.row_no): int(e.no) for e in bound if e.no}}
    extra = {"豁免": ledger} if ledger else {}
    if not hits:
        say = "已传图的每一行，原物的名字和特征词都没残留在提示词正文里"
        if active:
            say += f"（其中 {len(active)} 个词按你的裁量保留）"
        if stale:
            say += f"；{len(stale)} 个豁免条目已失效，见「展开看证据」"
        return {"项": "绑图行残留", "结论": PASS, "项显示名": ITEM_DISPLAY["绑图行残留"],
                "证据": "零残留｜" + ev, **extra,
                "说明": _say(say, 处理="无需处理", 统计=stats)}
    shown = "；".join(str(h["文字"]) for h in hits[:5])
    rows = list(dict.fromkeys(h["行号"] for h in hits if h.get("行号")))
    stats["词数"] = len(hits)
    stats[FOLD_STAT_KEY] = judgment_words
    stats["句数"] = len({(h.get("行号"), h.get("句序")) for h in hits})
    head = (f"提示词里还留着 {len(hits)} 个要换掉的原物词，都在{_row_phrase(rows[0], labels)}里"
            if len(rows) == 1 else
            f"提示词里还留着 {len(hits)} 个要换掉的原物词，分别在{_rows_phrase(rows, labels)}")
    if active:
        head += f"；另有 {len(active)} 个词按你的裁量保留"
    return {"项": "绑图行残留", "结论": FAIL, "项显示名": ITEM_DISPLAY["绑图行残留"],
            "证据": shown + ("…" if len(hits) > 5 else "") + "｜" + ev, **extra,
            "说明": _say(head, 处理=HOW_DESC, findings=hits, 统计=stats,
                        清单行=_lines_block(rows, lines),
                        原句=_sentence_block(body, sent_spans, hits[0]))}



def _check_lock(rows: list[dict], must_rows: list[int],
                submitted: list[tuple[int, str]], ack_rows: set[int],
                empty_confirm: bool, ko_rows: set[int] | None = None,
                groups: list[list[int]] | None = None,
                labels: dict[int, dict] | None = None,
                lines: dict[int, str] | None = None) -> dict:
    """③ 锁点对账：必换未换集合 == 确认时点『按原样』/『按原片』/empty_confirm 记录（只读快照，不自证）。

    AT-11 §四锁点新规则：必换行 ∈ 组且组已传图（整组被那张图换掉）→ 不锁；
    行有按原片决策（keep_origin / AT-12 dismissed）→ 不锁——两者都不需要『按原样』勾选。
    """
    ko_rows = ko_rows or set()
    groups = groups or []
    labels = labels or {}
    lines = lines or {}
    swapped = {no for no, _ in submitted}
    for group in groups:                        # 组已传图 → 组内所有行视为已换
        if any(no in swapped for no in group):
            swapped |= set(group)
    unswapped = sorted(set(must_rows) - swapped)
    covered = (set(ack_rows) | ko_rows
               | ({int(r["no"]) for r in rows} if empty_confirm else set()))
    missed = sorted(set(unswapped) - covered)
    ghost = sorted((covered & set(must_rows)) - set(unswapped))   # 记录了按原样/按原片但快照里其实换了
    ev = (f"必换={must_rows}；确认快照已传={sorted(swapped)}；未换={unswapped}；"
          f"按原样记录={sorted(ack_rows)}；按原片记录={sorted(ko_rows)}；empty_confirm={empty_confirm}")
    stats = {"必换行": sorted(must_rows), "已传": sorted(swapped), "未换": unswapped,
             "按原样记录": sorted(ack_rows), "按原片记录": sorted(ko_rows)}
    problems: list[dict] = []
    if missed:
        problems.append(_finding("锁点缺口",
                                 f"必换行 {missed} 未换且无『按原样』/『按原片』/empty_confirm 记录（锁点缺口）"))
    if ghost:
        problems.append(_finding("记录矛盾",
                                 f"行 {ghost} 记了按原样/按原片但确认快照里已传图（记录与快照矛盾）"))
    if not problems:
        return {"项": "锁点对账", "结论": PASS, "项显示名": ITEM_DISPLAY["锁点对账"],
                "证据": "未换必换行都有确认时点记录｜" + ev,
                "说明": _say("标了必须换的每一行，要么真换了，要么有「按原样／按原片」的确认记录",
                            处理="无需处理", 统计=stats)}
    findings = ([_finding("锁点缺口", f"#{n} 标了必须换，但没传图、也没勾按原样", row=n,
                          **_v2_of(labels, n, 人话="这一行标了必须换，但没传图、也没勾「按原样」"))
                 for n in missed]
                + [_finding("记录矛盾", f"#{n} 记了按原片/按原样，但提交快照里其实已经传了图", row=n,
                            **_v2_of(labels, n, 人话="这一行记了按原样／按原片，但提交快照里其实已经传了图"))
                   for n in ghost])
    words = []
    if missed:
        words.append(f"必换行 {'、'.join(f'#{n}' for n in missed)} 没换也没说按原样")
    if ghost:
        words.append(f"行 {'、'.join(f'#{n}' for n in ghost)} 的按原样记录与已传图矛盾")
    how = []
    if missed:
        how.append(HOW_ACK)
    if ghost:
        how.append(HOW_RECONFIRM)
    if missed and not ghost:                       # E2 §7 样例 2：一行说清哪行、什么问题、下一步
        head = f"{_rows_phrase(missed, labels)}标了必须换，但没传图、也没勾「按原样」"
    elif ghost:
        head = f"{_rows_phrase(ghost, labels)}的按原样记录和已传图对不上"
    else:
        head = "锁点对不上：" + "；".join(words)
    return {"项": "锁点对账", "结论": FAIL, "项显示名": ITEM_DISPLAY["锁点对账"],
            "证据": _evidence(problems) + "｜" + ev,
            "说明": _say(head, 处理="；".join(how), findings=findings, 统计=stats,
                        清单行=_lines_block(missed + ghost, lines))}


def _check_groups(ents, row_to_no: dict[int, int],
                  submitted: list[tuple[int, str]],
                  groups: list[list[int]],
                  labels: dict[int, dict] | None = None,
                  lines: dict[int, str] | None = None) -> dict:
    """④ 组绑定一致性（AT-11 B1 新增）：组内行图号/URL 必须一致。

    同组两 URL → FAIL；同号不同 URL → FAIL。次序对账读新映射后，这里只管「同组绑定
    是否真的归一」：组内 N 行 1 图号 1 URL 才算合并成功。
    """
    labels = labels or {}
    lines = lines or {}
    url_by_row: dict[int, list[str]] = {}
    for e in ents:
        urls = [u.strip() for u in re.split(r"[,，]", e.image) if u.strip()]
        if urls:
            url_by_row[int(e.row_no)] = urls
    problems: list[dict] = []
    for group in groups:
        nums = sorted({row_to_no[n] for n in group if row_to_no.get(n)})
        if not nums:
            continue                            # 组内全无图：不占号不提交，合并仍成立（零图分支）
        members = sorted({int(n) for n in group})
        if len(nums) != 1:
            problems.append(_finding("组多号",
                                     f"组 {group} 图号不一致: {[{n: row_to_no[n]} for n in group if row_to_no.get(n)]}",
                                     row=members[0] if members else None, 组=members,
                                     人话="这一组几行的图号不一样（本该共用一张图）"))
        urls = sorted({u for n in group for u in url_by_row.get(n, [])})
        if len(urls) > 1:
            problems.append(_finding("组两URL",
                                     f"组 {group} 绑定了 {len(urls)} 个不同 URL（同组两 URL）: "
                                     + "、".join(u[:40] for u in urls[:3]),
                                     row=members[0] if members else None, 组=members,
                                     人话="这一组几行绑的不是同一张图（本该全组一张）"))
    num_urls: dict[int, set[str]] = {}
    for no, urls in url_by_row.items():
        num = row_to_no.get(no)
        if num:
            num_urls.setdefault(num, set()).update(urls)
    for num in sorted(num_urls):
        if len(num_urls[num]) > 1:
            rows_with = sorted(r for r, n in row_to_no.items() if n == num)
            problems.append(_finding("同号两URL",
                                     f"图{num} 对应多个 URL（同号不同 URL）: "
                                     + "、".join(sorted(u[:40] for u in num_urls[num])[:3]),
                                     no=num, row=rows_with[0] if rows_with else None,
                                     组=rows_with or None,
                                     人话="同一个图号挂了不同的图"))
    seen: dict[tuple, str] = {}
    for no, url in submitted:
        unit = replace.row_unit(no, groups)
        if unit in seen and seen[unit] != url:
            problems.append(_finding("提交同组两URL",
                                     f"提交快照里同组两 URL（行 {no} 与行 "
                                     f"{next(n for n, u in submitted if replace.row_unit(n, groups) == unit and u == seen[unit])}）",
                                     row=no,
                                     **_v2_of(labels, no, 人话="同一组交上来的两张图不一样")))
        seen.setdefault(unit, url)
    ev = (f"确认组={groups or '无'}；行→图号={row_to_no}；组内 URL 归一后提交 {len(submitted)} 条")
    stats = {"确认组": groups or [], "行→图号": {str(k): v for k, v in sorted(row_to_no.items())},
             "提交条数": len(submitted)}
    if not problems:
        return {"项": "组绑定一致性", "结论": PASS, "项显示名": ITEM_DISPLAY["组绑定一致性"],
                "证据": "组内行同图号同 URL（无组或全组归一）｜" + ev,
                "说明": _say("每个组内部都是同一张图、同一个图号（没有组时等于没这条约束）",
                            处理="无需处理", 统计=stats)}
    return {"项": "组绑定一致性", "结论": FAIL, "项显示名": ITEM_DISPLAY["组绑定一致性"],
            "证据": _evidence(problems) + "｜" + ev,
            "说明": _say(f"合并过的组里对不上：{len(problems)} 处——组内本该只有一张图、一个图号",
                        处理=HOW_GROUP, findings=problems, 统计=stats,
                        清单行=_lines_block([p.get("行号") for p in problems], lines))}
