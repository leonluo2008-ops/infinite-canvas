#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""提示词保真判据（J1 / J2b / J3 / J4）——机器判，红了不许合并。

跑法：`py -3 tests/pipeline_shape_check.py`        退出码 0 = 全过，1 = 有 FAIL

为什么单独一个脚本：这四个判据判的是**产物的形状**（句子结构 / 列名回显 / 图号位置 / 禁词），
和 `render_check.py`（渲染器本身的八条）、`template_regression.py`（基线字节）不是一回事。
夹具用 `tests/fixtures/prompt-fidelity/拆解.md`：13 列分镜、九层齐全、0 空列，
不依赖模型、不联网、不花钱。

四条判据（对应 `docs/archive/20261103/方案-提示词保真-20261101.md` §5）
------------------------------------------------------
J1  叙事模板产物里，站位/朝向/视线/景别/镜头运动的**内容**在叙述句里，不是 `站位：…` 列名回显；
    且每个时段句至少含一个位置词（左/右/中/前/后/画面/背对/侧身）。
J2b 旧三套模板 + 新 13 列拆解：新列进产物是预期，但**旧 8 列的内容与顺序一字不变**。
J3  四套产物全文不出现 画外手 / 似乎 / 大概 / 可能是 / 隐约；叙事模板产物里出现 `画面外` 直接红。
J4  仅叙事模板：`@图片N` 必须在句子内部（行首不许是图号，图号前至少一个非空白字符）。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RENDER = ROOT / "pipeline" / "reverse" / "render.py"
FIXTURE = ROOT / "tests" / "fixtures" / "prompt-fidelity" / "拆解.md"
NARRATIVE = "timeline-narrative"
OLD_TEMPLATES = ("lumi-4section", "employee-checklist", "seven-section")
ALL_TEMPLATES = OLD_TEMPLATES + (NARRATIVE,)

# 旧 8 列（方案 §3：13 列是在这 8 列之后追加，前 8 列顺序不动）
OLD_COLUMNS = ("镜号", "起秒-止秒", "运动主体", "运动形态", "速度", "画面内容", "光影变化", "转场方式")
FORBIDDEN = ("画外手", "似乎", "大概", "可能是", "隐约")
POSITION_WORDS = ("左", "右", "中", "前", "后", "画面", "背对", "侧身")
# 这些列名一旦以「列名：」形态出现在行首，就说明模板只是把表格原样倒出来（J1 红）
COLUMN_PREFIXES = ("站位", "朝向", "视线", "景别", "镜头运动")

results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> bool:
    results.append((ok, name, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"｜{detail}" if detail else ""))
    return ok


def run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(RENDER), *args],
                          cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8")


def fixture_rows() -> tuple[list[str], list[list[str]]]:
    """夹具里那张分镜表 → (表头, 数据行)。"""
    rows: list[list[str]] = []
    for raw in FIXTURE.read_text(encoding="utf-8").splitlines():
        s = raw.strip()
        if not s.startswith("|"):
            continue
        if re.fullmatch(r"\|[\s:|-]+\|", s):
            continue
        rows.append([c.strip() for c in s.strip("|").split("|")])
    return rows[0], rows[1:]


def body_lines(path: Path) -> list[str]:
    """产物正文行（砍掉「# 附注」那节：附注里允许出现模板自己的说明文字）。"""
    out: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("# 附注"):
            break
        out.append(line)
    return out


def render_all(work: Path) -> dict[str, Path]:
    pid = work / "fx"
    pid.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FIXTURE, pid / "拆解.md")
    prods: dict[str, Path] = {}
    for tpl in ALL_TEMPLATES:
        out = work / f"out-{tpl}"
        cp = run(["--id", str(pid), "--template", tpl, "--out-dir", str(out)])
        p = out / f"提示词-{tpl}.md"
        ok = cp.returncode == 0 and p.is_file()
        check(ok, f"渲染 {tpl}", f"exit={cp.returncode}" + ("" if ok else f"｜{cp.stderr.strip()[-160:]}"))
        if ok:
            prods[tpl] = p
    return prods


def check_j1(narr: Path) -> None:
    """叙事模板：列名不回显 + 每个时段句有位置词。"""
    lines = [ln.strip() for ln in body_lines(narr) if ln.strip()]
    echoed = [ln[:40] for ln in lines
              if any(re.match(rf"^{p}\s*[：:]", ln) for p in COLUMN_PREFIXES)]
    check(not echoed, "J1a 叙事模板无列名回显（站位：/朝向：/视线：/景别：/镜头运动：）",
          "；".join(echoed[:3]) if echoed else "0 处列名前缀")

    # 只看「时段叙事」那一节的时间码句（段 1 的影调行也以时间码开头，不归 J1 管）
    sec: list[str] = []
    inside = False
    for ln in lines:
        if ln.startswith("## "):
            inside = "时段叙事" in ln
            continue
        if inside:
            sec.append(ln)
    shots = [ln for ln in sec if re.match(r"^\d{1,2}:\d{2}(?:\.\d+)?\s*[-–—~]\s*\d{1,2}:\d{2}", ln)]
    bad = [ln[:36] for ln in shots if not any(w in ln for w in POSITION_WORDS)]
    _, rows = fixture_rows()
    check(len(shots) == len(rows) and not bad,
          "J1b 每个时段句都带位置词",
          f"{len(shots)} 句时段行 / 夹具 {len(rows)} 镜"
          + ("；缺位置词：" + "；".join(bad[:3]) if bad else "；每句都有（左/右/画面/侧背…）"))

    # 段落空掉是静默故障：模板某段的 `写什么` 误写了维度词，渲染器按维度过滤子块，
    # 那一段就只剩一个「无」。这里逐段验有正文。
    sections: dict[str, list[str]] = {}
    cur = None
    for ln in body_lines(narr):
        if ln.startswith("## "):
            cur = ln[3:].strip()
            sections[cur] = []
        elif cur and ln.strip():
            sections[cur].append(ln.strip())
    empty = [t for t, ls in sections.items() if not ls or ls == ["无"]]
    check(len(sections) == 4 and not empty, "J1c 四段都不空（无静默的「无」段）",
          "空段：" + "；".join(empty) if empty else f"{len(sections)} 段都有正文")


def check_j2b(prods: dict[str, Path]) -> None:
    """旧三套模板：13 列拆解下，旧 8 列的内容与顺序不能变。"""
    header, rows = fixture_rows()
    for tpl in OLD_TEMPLATES:
        p = prods.get(tpl)
        if p is None:
            check(False, f"J2b {tpl} 旧 8 列不变（内容与顺序）", "产物缺失")
            continue
        lines = [ln.strip() for ln in body_lines(p)]
        shot_lines = [ln for ln in lines if re.match(r"^(?:@图片\d+\s+)*镜号\s*\d+｜", ln)]
        problems: list[str] = []
        if len(shot_lines) != len(rows):
            problems.append(f"分镜行 {len(shot_lines)} 行，夹具 {len(rows)} 行")
        for ln, cells in zip(shot_lines, rows):
            body = re.sub(r"^(?:@图片\d+\s+)+", "", ln)
            pos = -1
            for i, col in enumerate(OLD_COLUMNS):
                want = cells[header.index(col)]
                if not want:
                    continue
                # 行首镜号写成「镜号 1」，其余列写成「列名：值」（第 2 列裸值）
                probe = want if col in ("镜号", "起秒-止秒") else want
                at = body.find(probe, pos + 1)
                if at < 0:
                    problems.append(f"第 {cells[0]} 镜缺「{col}」的值")
                    break
                pos = at
        check(not problems, f"J2b {tpl} 旧 8 列不变（内容与顺序）",
              "；".join(problems[:3]) if problems else f"{len(shot_lines)} 个分镜行，8 列逐列在原位")


def check_j3(prods: dict[str, Path]) -> None:
    for tpl, p in prods.items():
        text = p.read_text(encoding="utf-8")
        hits = [w for w in FORBIDDEN if w in text]
        if tpl == NARRATIVE and "画面外" in text:
            hits.append("画面外")
        check(not hits, f"J3 禁词 {tpl}", "命中：" + "、".join(hits) if hits else "0 命中")


def check_j4(narr: Path) -> None:
    lines = body_lines(narr)
    head_shot = [ln[:24] for ln in lines if re.match(r"^\s*@图片\d", ln)]
    dangling = [ln[:24] for ln in lines
                for m in re.finditer(r"@图片\d+", ln)
                if not ln[:m.start()].strip()]
    check(not head_shot, "J4a 行首不是图号", "；".join(head_shot[:3]) if head_shot else "0 行以 @图片 开头")
    check(not dangling, "J4b 图号前至少一个非空白字符",
          "；".join(dict.fromkeys(dangling)) if dangling else "每处 @图片N 都在句内")


def check_j4_with_tags(work: Path) -> None:
    """补一份待确认清单，让图号真的挂上：验「图号紧跟主体值、在句内」。"""
    pid = work / "fx-tags"
    pid.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FIXTURE, pid / "拆解.md")
    rows = [
        ("人物", "亚洲外观女性，约 25 岁，黑色齐肩直发", "人物A"),
        ("服装", "米白色宽松长袖衬衫，深灰色直筒长裤", "服装A"),
        ("道具", "深灰色直板手机，黑色圆角矩形屏幕", "智能手机"),
        ("道具", "透明细长圆柱玻璃瓶，浅金色液体", "香水瓶"),
        ("道具", "米白色长方体细长纸盒", "香水纸盒"),
    ]
    lines = ["| # | 类别 | 模型看到什么 | 命名 | 资产图 | 锁什么 | 建议 | 用户描述 |",
             "|---|---|---|---|---|---|---|---|"]
    for i, (kind, seen, name) in enumerate(rows, 1):
        lines.append(f"| {i} | {kind} | {seen} | {name} | tests/fixtures/占位.png | 形态不变 | | |")
    (pid / "待确认清单.md").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

    out = work / "out-tags"
    cp = run(["--id", str(pid), "--template", NARRATIVE, "--out-dir", str(out)])
    p = out / f"提示词-{NARRATIVE}.md"
    if cp.returncode != 0 or not p.is_file():
        check(False, "J4c 挂图号后仍在句内", f"exit={cp.returncode}｜{cp.stderr.strip()[-160:]}")
        return
    body = [ln.strip() for ln in body_lines(p) if ln.strip()]
    tagged = [ln for ln in body if "@图片" in ln and re.match(r"^\d{1,2}:\d{2}", ln)]
    problems: list[str] = []
    for ln in body:
        if re.match(r"^\s*@图片\d", ln):
            problems.append(f"行首图号：{ln[:20]}")
    # 镜 2 的主体是「手机」：图号必须紧跟在「手机」后面，而且不能在行首
    shot2 = next((ln for ln in tagged if ln.startswith("00:02-00:04")), "")
    m = re.search(r"手机(\s*@图片\d+)?", shot2)
    if not shot2:
        problems.append("镜 2 时段行缺失")
    elif not m or not m.group(1):
        problems.append("镜 2 的「手机」后面没挂图号")
    check(len(tagged) >= 4 and not problems, "J4c 挂图号后仍在句内（紧跟主体值）",
          "；".join(problems) if problems else f"{len(tagged)} 句带图号；镜 2「手机 @图片3」紧跟主体")


def check_missing_column(work: Path) -> None:
    """负向：模板声明的列在表头不存在 → 必须 exit 3，不许静默跳过。"""
    src = (ROOT / "templates" / f"{NARRATIVE}.md").read_text(encoding="utf-8")
    lines = src.splitlines()
    idx = next((i for i, ln in enumerate(lines) if ln.startswith("- 分镜列：")), None)
    if idx is None:
        check(False, "负向 缺列 → exit 3", "模板里找不到 `分镜列` 那一行，判据失效")
        return
    lines[idx] = "- 分镜列：时段,景别,不存在的列"
    broken = "\n".join(lines)
    if broken == src:
        check(False, "负向 缺列 → exit 3", "模板里找不到 `分镜列` 那一行，判据失效")
        return
    # 文件名必须与 frontmatter name 一致，否则会先被「name 与文件名不一致」挡下来，
    # 测不到缺列这条分支
    tpl = work / f"{NARRATIVE}.md"
    tpl.write_text(broken, encoding="utf-8", newline="\n")
    pid = work / "fx"
    cp = run(["--id", str(pid), "--template", str(tpl), "--out-dir", str(work / "neg-col")])
    ok = cp.returncode == 3 and "不存在的列" in cp.stderr and "分镜表头里没有" in cp.stderr
    check(ok, "负向 模板要的列拆解里没有 → exit 3",
          f"exit={cp.returncode}｜{cp.stderr.strip().splitlines()[-1][:100] if cp.stderr.strip() else ''}")
    check(not (work / "neg-col" / f"提示词-{tpl.stem}.md").is_file(), "负向 缺列时不落产物")


def check_no_table(work: Path) -> None:
    """负向：写了 `分镜列`，但拆解「七 分镜表」里没有表格 → exit 3。"""
    pid = work / "fx-notable"
    pid.mkdir(parents=True, exist_ok=True)
    text = "\n".join(ln for ln in FIXTURE.read_text(encoding="utf-8").splitlines()
                     if not ln.strip().startswith("|"))
    (pid / "拆解.md").write_text(text + "\n", encoding="utf-8", newline="\n")
    cp = run(["--id", str(pid), "--template", NARRATIVE, "--out-dir", str(work / "neg-table")])
    ok = cp.returncode == 3 and "没有表格" in cp.stderr
    check(ok, "负向 拆解无分镜表 → exit 3",
          f"exit={cp.returncode}｜{cp.stderr.strip().splitlines()[-1][:100] if cp.stderr.strip() else ''}")


def main() -> int:
    print("=" * 78)
    print("pipeline_shape_check · J1 / J2b / J3 / J4")
    print("=" * 78)
    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        prods = render_all(work)
        narr = prods.get(NARRATIVE)
        if narr is None:
            check(False, "J1/J4 需要叙事模板产物", "产物缺失")
        else:
            check_j1(narr)
            check_j4(narr)
        check_j2b(prods)
        check_j3(prods)
        check_j4_with_tags(work)
        check_missing_column(work)
        check_no_table(work)

    n_fail = sum(1 for ok, _, _ in results if not ok)
    print("-" * 78)
    print(f"{len(results) - n_fail} PASS / {n_fail} FAIL")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
