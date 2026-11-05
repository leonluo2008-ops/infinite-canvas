"""拉片投影渲染器 —— 十层 IR + specs/<name>.md 块表 → 即梦结构拉片。

与 render.py 分工：render.py 管生成正文（templates/），本件管**拉片投影**（specs/）。
只读投影：不改 IR、不写回拆解。缺项不编造——三态标注（specs/jimeng-lapian.md 契约）：
    〔未取证〕 IR 没这块    〔不适用〕 该片类型没有（须有据）    〔缺字段〕 旧格式缺层/列
裸空块 = FAIL（tests/lapian_check.py 守着）。

用法：
    py -3 pipeline/reverse/render_lapian.py --id pop-4-emo --spec jimeng-lapian
    产物：analysis/reverse/<id>/拉片-<spec>.md；--json 打 blocks 结构到 stdout。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from render import (  # noqa: E402
    EXIT_INPUT, EXIT_LAYER, EXIT_OK, EXIT_TEMPLATE, Fail, ROOT,
    apply_naming, assign_numbers, parse_checklist, parse_layers, read_text,
    sanitize, seconds_timecodes, shot_table, split_frontmatter, write_text,
    SHOT_SKIP_VALUES,
)
from confirm import is_absent_state  # noqa: E402  存在性空态行判定（唯一真源在 confirm.py）

SPEC_DIR = ROOT / "specs"
STATE_RE = re.compile(r"〔(未取证|不适用|缺字段)〕")
SPEC_LINE_RE = re.compile(r"^>\s*规格[：:]\s*(.+)$", re.M)
COMMENT_RE = re.compile(r"^<!--\s*(.*?)\s*-->", re.S)
DUR_RE = re.compile(r"时长[：:]\s*([\d.]+)\s*s")
TRACK_RE = re.compile(r"音轨[：:]\s*(有|无)")
RATIO_RE = re.compile(r"(\d{2,4}[×x]\d{2,4})\s*·\s*(\d+:\d+)\s*(横屏|竖屏|方画幅)?")
FPS_RE = re.compile(r"([\d.]+)\s*fps")
CUT_RE = re.compile(r"硬切\s*(\d+)\s*次")
ONE_TAKE_RE = re.compile(r"一镜到底[：:]\s*(是|否)")
FIRST_FRAME_RE = re.compile(r"首帧[：:]\s*(.+)")


class IR:
    """一份拆解的只读视图（十层 + 清单 + 分镜表）。"""

    def __init__(self, task_dir: Path) -> None:
        self.dir = task_dir
        raw = read_text(task_dir / "拆解.md", EXIT_LAYER, "拆解")
        self.raw = raw
        self.layers, self.order = parse_layers(raw)
        self.ents = assign_numbers(parse_checklist(task_dir / "待确认清单.md"))
        m = SPEC_LINE_RE.search(raw)
        self.spec_line = m.group(1).strip() if m else ""
        cm = COMMENT_RE.search(raw)
        self.comment = cm.group(1) if cm else ""
        self.shot_header: list[str] = []
        self.shot_rows: list[list[str]] = []
        if "七" in self.layers:
            self.shot_header, self.shot_rows = shot_table(self.layers["七"])

    # ---- 基础取数 -----------------------------------------------------------
    def layer(self, cn: str) -> str:
        return sanitize(apply_naming(self.layers.get(cn, ""), self.ents)).strip()

    def layer_lines(self, cn: str, bullet_only: bool = False) -> list[str]:
        body = self.layers.get(cn, "")
        if not body:
            return []
        out = []
        for raw in body.splitlines():
            s = sanitize(apply_naming(raw.strip(), self.ents))
            if not s:
                continue
            if bullet_only and not re.match(r"^[*+•·\-–—]", raw.strip()):
                continue
            s = re.sub(r"^[*+•·\-–—]\s*", "", s)
            out.append(s)
        return out

    def field_in(self, cn: str, key: str) -> str:
        """层内 `**key**：value` 或 `key：value` 行的 value。"""
        pat = re.compile(rf"\*{{0,2}}{re.escape(key)}\*{{0,2}}\s*[：:]\s*(.+)")
        for line in self.layer_lines(cn):
            m = pat.match(line)
            if m:
                return m.group(1).strip()
        return ""

    def shot_col(self, name: str) -> int | None:
        if name in self.shot_header:
            return self.shot_header.index(name)
        for alias in (f"{name}",):
            if alias in self.shot_header:
                return self.shot_header.index(alias)
        return None

    def cell(self, row: list[str], name: str) -> str:
        i = self.shot_col(name)
        return row[i].strip() if i is not None and i < len(row) else ""


# ---- 三态标注小件 ----------------------------------------------------------
def unproven(why: str) -> list[str]:
    return [f"〔未取证〕（{why}）"]


def missing_field(why: str) -> list[str]:
    return [f"〔缺字段〕（{why}）"]


# ---- 投影函数（来源键 → 块）-----------------------------------------------
# 返回 (title_lines, body_lines)；title_lines 为空表示该块直接以【标题】呈现。
def _p_spec_line(ir: IR) -> tuple[str, list[str]]:
    if ir.spec_line:
        parts = []
        m = RATIO_RE.search(ir.spec_line)
        if m:
            orient = m.group(3) or ""
            parts.append(f"{orient}{m.group(2)}（{m.group(1)}）".lstrip())
        m = FPS_RE.search(ir.spec_line)
        if m:
            parts.append(f"{m.group(1)} fps")
        m = re.search(r"([\d.]+)\s*秒", ir.spec_line)
        if m:
            parts.append(f"全片 {m.group(1)} 秒")
        m = TRACK_RE.search(ir.spec_line)
        if m:
            parts.append(f"{'有' if m.group(1) == '有' else '无'}音轨")
        if not parts:                      # 规格行在但没解析出任何字段
            return missing_field("规格行存在但无画幅/时长字段")
        return "，".join(parts)
    if ir.comment:                         # 旧格式兜底：HTML 注释里的时长
        m = DUR_RE.search(ir.comment)
        t = TRACK_RE.search(ir.comment)
        if m:
            track = f" · {'有' if t and t.group(1) == '有' else '无'}音轨" if t else ""
            return f"全片 {m.group(1)} 秒{track}（画幅与帧率〔缺字段〕——旧拆解无规格行）"
        return missing_field("既无规格行，注释里也无时长")
    return missing_field("既无规格行也无注释元数据")


def _p_genre(ir: IR) -> str:
    tone = ir.field_in("六", "整体调性")
    if tone:
        return f"调性线索：{tone}；类型判定〔未取证〕（IR 无类型层，不编「广告片/短片」类结论）"
    return "／".join(unproven("六 意境无「整体调性」行，且 IR 无类型判定层"))


def asset_head(ln: str) -> str:
    """资产行 → 主体名：在「名/描述」分隔冒号处切断，保留名内的括号时间区间。

    分隔冒号 = 不在任何括号内、且不夹在两个数字之间的全/半角冒号。
    这样「人物（出现于：00:00-00:04）：描述」切在括号后的冒号上，
    名内的「出现于：00:00-00:04」不误切（括号保护 + 数字夹住的半角冒号是时刻不是分隔）；
    行被截断（无闭合括号/无分隔冒号）时原样保留，宁全勿丢。
    """
    s = ln.strip().strip("*").strip()
    depth = 0
    for i, ch in enumerate(s):
        if ch in "（(":
            depth += 1
        elif ch in "）)":
            depth = max(0, depth - 1)
        elif ch in "：:" and depth == 0 and not (
                i > 0 and s[i - 1].isdigit() and i + 1 < len(s) and s[i + 1].isdigit()):
            return s[:i].strip().strip("*").strip()
    return s


def _p_core_assets(ir: IR) -> str:
    with_img = [e for e in ir.ents if e.no]
    if with_img:
        return "、".join(e.label() for e in with_img)
    if "九" in ir.layers and ir.layers["九"].strip():
        lines = ir.layer_lines("九", bullet_only=True)
        # 存在性空态行（`人物：无…` / `服装：无`）不是资产名：跳过，不摘要成「人物；服装」
        # （2026-11 路 A 审计 S3：否则同一投影前段看似有角色、固有资产块却写「无」）。
        # 否定全文保留在固有资产块（_p_assets_in_seg 原样 quoting，不在这里动）。
        names = [asset_head(ln) for ln in lines if not is_absent_state(ln)]
        names = [n for n in names if n]
        if names:
            return "；".join(names) + "（图号绑定〔未取证〕——无已备图实体，未发 @图片N）"
        if lines:
            return "／".join(unproven("九 资产层只有存在性空态说明，没有实际资产条目"))
    return "／".join(unproven("九 资产层为空且无待确认清单"))


def _one_layer(ir: IR, cn: str, what: str) -> str:
    body = ir.layer(cn)
    return body if body else "／".join(unproven(f"{what}（{cn} 层缺失或为空）"))


def _p_rhythm(ir: IR) -> str:
    parts = []
    m = ONE_TAKE_RE.search(ir.layers.get("七", ""))
    if m:
        parts.append(f"一镜到底：{m.group(1)}")
    m = CUT_RE.search(ir.spec_line)
    if m:
        parts.append(f"硬切 {m.group(1)} 次")
    rhythm = ir.field_in("六", "节奏感")
    if rhythm:
        parts.append(f"节奏：{rhythm}")
    if not parts:
        return "／".join(unproven("无硬切信息、无一镜到底行、六 意境无「节奏感」行"))
    return "；".join(parts)


def _p_sound_base(ir: IR) -> str:
    lines = ir.layer_lines("八")
    if not lines:
        return "／".join(unproven("八 声音层缺失或为空"))
    return "；".join(lines)


def _p_continuity(ir: IR) -> str:
    tech = ir.field_in("六", "手法")
    if tech:
        return f"全片手法：{tech}；跨段连续契约〔未取证〕（IR 无独立跨段规则层，此为全片手法非段间契约）"
    return "／".join(unproven("六 意境无「手法」行，且 IR 无独立跨段规则层"))


def _duration(ir: IR) -> float | None:
    m = re.search(r"([\d.]+)\s*秒", ir.spec_line) if ir.spec_line else None
    if m:
        return float(m.group(1))
    m = DUR_RE.search(ir.comment) if ir.comment else None
    return float(m.group(1)) if m else None


def _p_seg_head(ir: IR) -> str:
    d = _duration(ir)
    if d is None:
        return "拉片段 01｜原片时长〔缺字段〕｜模型时长〔缺字段〕"
    model = round(d)
    return f"拉片段 01｜原片 0-{d:g} 秒｜模型时长 {model} 秒"


def _p_assets_in_seg(ir: IR) -> str:
    lines = ir.layer_lines("九", bullet_only=True)
    if lines:
        return "\n".join(f"- {ln}" for ln in lines)
    body = ir.layer("九")
    if body:
        return body
    return "／".join(unproven("九 资产层缺失或为空"))


def _p_anchors(ir: IR) -> str:
    parts = []
    prod = ir.layer_lines("二")
    if prod:
        parts.append(f"产品锚：{prod[0]}")
    scene = ir.layer_lines("一")
    if scene:
        parts.append(f"场景锚：{scene[0]}")
    if not parts:
        return "／".join(unproven("二 产品与 一 场景均缺失"))
    return "\n".join(parts)


def _p_start_state(ir: IR) -> str:
    m = FIRST_FRAME_RE.search(ir.layers.get("七", ""))
    if m:
        return sanitize(apply_naming(f"首帧：{m.group(1).strip()}", ir.ents))
    return "／".join(unproven("七 分镜层无「首帧」行（旧拆解常见）"))


def _shot_four_subblocks(ir: IR, row: list[str], idx: int) -> list[str]:
    """一个分镜行 → 四子块（画面主体与表演/镜头调度/影像渲染/声音叙事）。"""
    # 画面主体与表演
    perf_bits = []
    for col in ("运动主体", "画面内容", "情绪/表演", "站位", "朝向", "视线"):
        v = ir.cell(row, col)
        if v and v not in SHOT_SKIP_VALUES:
            perf_bits.append(v)
    perf = "；".join(perf_bits) if perf_bits else ""
    perf_line = (f"- 画面主体与表演：{perf}" if perf
                 else "- 画面主体与表演：〔缺字段〕（分镜表无主体/画面列或该行全空）")

    # 镜头调度
    lens_bits = []
    for col in ("镜头运动", "景别"):
        v = ir.cell(row, col)
        if v and v not in SHOT_SKIP_VALUES:
            lens_bits.append(v)
    if lens_bits:
        lens_line = f"- 镜头调度：{'，'.join(lens_bits)}"
    else:
        # 旧格式缺列：五 镜头 层按**关键词定位**兜底，不按序号瞎取。
        # 2026-11-06 修：原 five[idx] 会把镜 2 对到「焦段与景深」行（层行数 ≠ 分镜行数，
        # 旧拆解五层通常只有 2 行：机位与运镜 / 焦段与景深）。
        five = ir.layer_lines("五")
        pick = next((ln for ln in five if "机位与运镜" in ln), "")
        if not pick:
            pick = next((ln for ln in five if "运镜" in ln), "")
        if pick:
            # 去掉行首的 **标签**： 前缀，只留值
            pick = re.sub(r"^\*{0,2}[^*：:]{0,6}运镜\*{0,2}\s*[：:]\s*", "", pick).strip()
        lens_line = (f"- 镜头调度：〔缺字段〕（表无镜头列；五 镜头 层「机位与运镜」兜底：{pick}）" if pick
                     else "- 镜头调度：〔旧格式无运镜列〕（建议重新拆解补齐分镜表）")

    # 影像渲染
    render_bits = []
    for col in ("光影变化", "速度"):
        v = ir.cell(row, col)
        if v and v not in SHOT_SKIP_VALUES:
            render_bits.append(v)
    render_line = (f"- 影像渲染：{'，'.join(render_bits)}" if render_bits
                   else "- 影像渲染：〔缺字段〕（表无光影变化/速度列或该行全空）")

    # 声音叙事（全片基调挂段；台词「无」→ 不适用）
    sound_lines = ir.layer_lines("八")
    dialogue = next((ln for ln in sound_lines if ln.startswith("台词")), "")
    if dialogue and re.search(r"台词[：:]\s*无", dialogue):
        dial = "台词：〔不适用〕（拆解判明无台词）"
    elif dialogue:
        dial = f"{dialogue}（时段归属〔未取证〕——台词层无时间戳）"
    else:
        dial = "台词：〔未取证〕（八 声音层无台词行）"
    base = "；".join(ln for ln in sound_lines if not ln.startswith("台词"))
    sound_line = (f"- 声音叙事：{dial}；全片基调：{base}（时段级〔未取证〕）" if base
                  else f"- 声音叙事：{dial}；全片基调〔未取证〕（八 声音层缺失）")
    return [perf_line, lens_line, render_line, sound_line]


def _p_time_segments(ir: IR) -> list[str]:
    if not ir.shot_rows:
        return unproven("七 分镜层没有解析出分镜表")
    out: list[str] = []
    for idx, row in enumerate(ir.shot_rows):
        tc = ir.cell(row, "起秒-止秒") or (row[1].strip() if len(row) > 1 else "")
        no = ir.cell(row, "镜号") or str(idx + 1)
        no = re.sub(r"\D", "", str(no)) or str(idx + 1)
        tc = seconds_timecodes(tc) if tc else tc
        head = f"{tc}｜原片镜{no}" if tc else f"〔缺字段〕时段（表头无时段列）｜原片镜{no}"
        out.append(head)
        out.extend(_shot_four_subblocks(ir, row, idx))
    return out


def _p_closing(ir: IR) -> str:
    """十 图文与收束 → DNA 收束段：收束类型/版式/动效时序 + 图文元素逐条。

    空态照实投影：`图文：无（全片画面无持久性文字元素）` 与
    `收束：类型=无品牌露出，画面直接结束` 是**真事实**（这一片确实没有文字/没有品牌露出），
    原样进块，不按「无资产」过滤——生成端要靠它们知道「别自己加字」。整层缺失
    （旧格式九层拆解）→ 〔未取证〕，不编。
    """
    body = ir.layers.get("十", "")
    lines = ir.layer_lines("十", bullet_only=True) if body.strip() else []
    if not lines:
        body = ir.layer("十")
        if body:
            return body
        return "／".join(unproven("十 图文与收束层缺失（旧格式拆解无这一层）"))
    closing = [ln for ln in lines if ln.startswith("收束")]
    elements = [ln for ln in lines if not ln.startswith("收束")]
    out = closing + elements
    out = [ln for ln in out if ln.strip()]
    if not out:
        return "／".join(unproven("十 图文与收束层只有空行，没有可投影的内容"))
    return "\n".join(f"- {ln}" for ln in out)


def _p_end_state(ir: IR) -> str:
    if ir.shot_rows:
        last = ir.shot_rows[-1]
        v = ir.cell(last, "画面内容")
        if v:
            return (f"尾镜画面：{v}\n独立交接契约〔未取证〕（IR 无独立结束状态层，"
                    f"此为末镜画面内容，非生成端交接约束）")
    return "／".join(unproven("分镜表为空，无末镜画面可引；IR 亦无独立结束状态层"))


def _p_constraints(ir: IR) -> str:
    parts = []
    m = CUT_RE.search(ir.spec_line)
    if m:
        parts.append(f"全片硬切 {m.group(1)} 次（转场按分镜表「转场方式」列）")
    m = ONE_TAKE_RE.search(ir.layers.get("七", ""))
    if m:
        parts.append(f"一镜到底：{m.group(1)}")
    tech = ir.field_in("六", "手法")
    if tech:
        parts.append(f"手法：{tech}")
    if not parts:
        return "／".join(unproven("无硬切/一镜到底/手法信息可引"))
    return "\n".join(f"- {p}" for p in parts)


PROJECTIONS = {
    "spec_line": _p_spec_line, "genre": _p_genre, "core_assets": _p_core_assets,
    "scene": lambda ir: _one_layer(ir, "一", "场景层"), 
    "tone_light": lambda ir: _one_layer(ir, "四", "光影层"),
    "lens": lambda ir: ("\n".join(f"- {ln}" for ln in ir.layer_lines("五"))
                        if ir.layer_lines("五") else "／".join(unproven("五 镜头层缺失或为空"))),
    "rhythm": _p_rhythm, "sound_base": _p_sound_base, "continuity": _p_continuity,
    "seg_head": _p_seg_head, "assets_in_seg": _p_assets_in_seg, "anchors": _p_anchors,
    "closing": _p_closing,
    "start_state": _p_start_state, "time_segments": _p_time_segments,
    "end_state": _p_end_state, "constraints": _p_constraints,
}

SPEC_TABLE_RE = re.compile(r"^\|\s*(\d+)\s*\|(.+)\|\s*(\w+)\s*\|", re.M)


def load_spec(spec_name: str) -> tuple[dict[str, str], list[dict[str, str]]]:
    """读 specs/<name>.md：frontmatter + 块定义表行（序/块名/来源键/态/说明）。"""
    path = SPEC_DIR / f"{spec_name}.md"
    if not path.is_file():
        raise Fail(EXIT_INPUT, f"拉片规范不存在：specs/{spec_name}.md")
    fm, body = split_frontmatter(read_text(path, EXIT_TEMPLATE, "拉片规范"))
    rows: list[dict[str, str]] = []
    for raw in body.splitlines():
        s = raw.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) < 3 or not re.fullmatch(r"\d+", cells[0]):
            continue
        rows.append({"序": cells[0], "块名": cells[1], "来源键": cells[2],
                     "态": cells[3] if len(cells) > 3 else "",
                     "说明": cells[4] if len(cells) > 4 else ""})
    if not rows:
        raise Fail(EXIT_TEMPLATE, f"拉片规范 specs/{spec_name}.md 里没有块定义表")
    for r in rows:
        if r["来源键"] not in PROJECTIONS:
            raise Fail(EXIT_TEMPLATE,
                       f"规范「{spec_name}」第 {r['序']} 块「{r['块名']}」的来源键"
                       f"「{r['来源键']}」未在渲染器注册（先注册投影函数，再引用）")
    return fm, rows


def project(ir: IR, spec_rows: list[dict[str, str]]) -> list[dict]:
    """按块表投影 → blocks[]：{title, lines[], dna}。dna=True → `- 块名：…` 行。"""
    blocks: list[dict] = []
    for r in spec_rows:
        seq = int(r["序"])
        val = PROJECTIONS[r["来源键"]](ir)
        lines = val if isinstance(val, list) else ([val] if val else [])
        if not lines:                       # 投影函数不许返回空（缺项必须带态）
            raise Fail(EXIT_TEMPLATE, f"块「{r['块名']}」投影结果为空且无态标注——裸空块")
        blocks.append({"title": r["块名"], "lines": lines,
                       "dna": seq <= 10, "seg_head": r["来源键"] == "seg_head"})
    return blocks


def render_markdown(task_id: str, spec_name: str, blocks: list[dict]) -> str:
    out = [f"# 拉片 · {task_id}（规范：{spec_name}）", ""]
    dna = [b for b in blocks if b["dna"]]
    seg = [b for b in blocks if not b["dna"]]
    if dna:
        out.append("【全局视听DNA】")
        for b in dna:
            out.append(f"- {b['title']}：{b['lines'][0]}")
            out.extend(f"  {ln}" for ln in b["lines"][1:])
        out.append("")
    for b in seg:
        if b["seg_head"]:
            out.append(f"【{b['lines'][0]}】")
        else:
            out.append(f"【{b['title']}】")
            out.extend(b["lines"])
        out.append("")
    return "\n".join(out).strip() + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="十层 IR + 拉片规范 → 即梦结构拉片（只读投影）")
    ap.add_argument("--id", required=True, help="analysis/reverse/<id> 目录名或路径")
    ap.add_argument("--spec", default="jimeng-lapian", help="specs/<name>.md 的 name")
    ap.add_argument("--json", action="store_true", help="stdout 打 blocks JSON（bridge 用）")
    args = ap.parse_args(argv)

    id_path = Path(args.id)
    if not id_path.is_absolute():
        cand = ROOT / "analysis" / "reverse" / args.id
        id_path = cand if cand.is_dir() else (ROOT / args.id)
    if not id_path.is_dir():
        raise Fail(EXIT_INPUT, f"分析目录不存在：{args.id}")

    fm, spec_rows = load_spec(args.spec)
    ir = IR(id_path)
    blocks = project(ir, spec_rows)
    md = render_markdown(id_path.name, args.spec, blocks)
    out_path = id_path / f"拉片-{args.spec}.md"
    write_text(out_path, md)
    if args.json:
        print(json.dumps({"task": id_path.name, "spec": args.spec, "spec_fm": fm,
                          "blocks": blocks}, ensure_ascii=False, indent=1))
    else:
        texts = [" ".join(b["lines"]) for b in blocks]
        marked = sum(1 for tx in texts if STATE_RE.search(tx))
        print(f"OK 拉片已写入 {out_path}（{len(blocks)} 块，态标注 {marked} 块）")
    return EXIT_OK


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Fail as e:
        msg = str(e)
        code = int(re.match(r"\[(\d+)\]", msg).group(1)) if re.match(r"\[(\d+)\]", msg) else 1
        print(f"FAIL {msg}", file=sys.stderr)
        sys.exit(code)
