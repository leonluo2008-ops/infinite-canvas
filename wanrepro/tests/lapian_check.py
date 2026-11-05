"""M1 验收：拉片投影断言（docs/方案-画布MVP-20261103.md §4）。

FAIL 如实记，不改判据迁就结果。断言组：
  A  DNA 9 块名全出现
  B  ≥3 时段子块且每块四子块标签在位
  C  起/终衔接块在位：有正文或有〔未取证〕标注；裸空 = FAIL
  D  无裸空块；缺项行必带三态标注之一
  E  图号不丢：待确认清单已备图实体的 @图片N 集合 ⊆ 产物图号集合
  F  剥层夹具（删 六/八 层）不崩，相关块带〔未取证〕
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RENDER = ROOT / "pipeline" / "reverse" / "render_lapian.py"
TASKS = ["test_a", "pop-4-emo"]
DNA_NAMES = ["画幅载体", "类型媒介", "核心主体资产", "场景与世界规则", "全片色调与光影弧线",
             "影像质感与镜头语法", "剪辑与节奏", "声音基调", "跨拉片段连续规则"]
SUB_LABELS = ["画面主体与表演", "镜头调度", "影像渲染", "声音叙事"]
STATE = re.compile(r"〔(未取证|不适用|缺字段)〕")
IMG_NO = re.compile(r"@图片(\d+)")


def run(task: str) -> tuple[int, str, str]:
    r = subprocess.run([sys.executable, str(RENDER), "--id", task, "--spec", "jimeng-lapian"],
                       capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT))
    return r.returncode, r.stdout, r.stderr


def read_lapian(task: str) -> str:
    return (ROOT / "analysis" / "reverse" / task / "拉片-jimeng-lapian.md").read_text(encoding="utf-8")


def check(task: str, md: str) -> list[str]:
    errs: list[str] = []
    # A DNA 9 块名
    for name in DNA_NAMES:
        if f"- {name}：" not in md:
            errs.append(f"[A] DNA 块名缺失：{name}")
    # B 时段子块
    heads = re.findall(r"^([\d.]+-[\d.]+秒)｜原片镜(\d+)$", md, re.M)
    if len(heads) < 3:
        errs.append(f"[B] 时段子块不足 3（实际 {len(heads)}）")
    for tc, no in heads:
        seg = md.split(f"{tc}｜原片镜{no}", 1)[1].split("【", 1)[0]
        for lab in SUB_LABELS:
            if f"- {lab}：" not in seg:
                errs.append(f"[B] 时段 {tc} 缺子块标签：{lab}")
    # C 起/终衔接块
    for block, tag in (("起始衔接状态", "起"), ("结束交接状态", "终")):
        m = re.search(rf"【{block}】\n(.*?)(?=\n【|\Z)", md, re.S)
        if not m:
            errs.append(f"[C] 块缺失：【{block}】")
            continue
        body = m.group(1).strip()
        if not body:
            errs.append(f"[C] 【{block}】裸空（无正文且无态标注）")
        elif not STATE.search(body) and len(body) < 8:
            errs.append(f"[C] 【{block}】正文过短且无态标注：{body!r}")
    # D 无裸空块；缺项带态
    for m in re.finditer(r"【([^】]+)】\n(.*?)(?=\n【|\Z)", md, re.S):
        title, body = m.group(1), m.group(2).strip()
        if "拉片段" in title and "｜原片" in title:
            continue                      # 段头块是单行标题块，无块体是结构本身
        if not body:
            errs.append(f"[D] 裸空块：【{title}】")
        elif STATE.search(body) is None and title != "拉片段 01" and "｜原片" not in title:
            # 有正文的块顺带含态标注不强制；只查「看起来是占位」的行
            for line in body.splitlines():
                if re.fullmatch(r"- [^：]+：\s*", line):
                    errs.append(f"[D] 【{title}】占位空值行：{line!r}")
    # E 图号不丢
    src = ROOT / "analysis" / "reverse" / task / "待确认清单.md"
    if src.is_file():
        sys.path.insert(0, str(ROOT / "pipeline" / "reverse"))
        from render import assign_numbers, parse_checklist
        ents = assign_numbers(parse_checklist(src))
        want = {str(e.no) for e in ents if e.no}
        got = set(IMG_NO.findall(md))
        miss = want - got
        if miss:
            errs.append(f"[E] 已备图实体图号未出现在拉片：@图片{','.join(sorted(miss))}")
    return errs


def stripped_fixture() -> tuple[str, Path]:
    """剥层夹具：拷 test_a 拆解，删 六 意境 与 八 声音 两层。"""
    tmp = Path(tempfile.mkdtemp(prefix="lapian-fx-"))
    d = tmp / "fx-stripped"
    shutil.copytree(ROOT / "analysis" / "reverse" / "test_a", d)
    txt = (d / "拆解.md").read_text(encoding="utf-8")
    out, skip = [], False
    for line in txt.splitlines():
        if re.match(r"^## [六八] ", line):
            skip = True
            continue
        if re.match(r"^## ", line):
            skip = False
        if not skip:
            out.append(line)
    (d / "拆解.md").write_text("\n".join(out), encoding="utf-8")
    return str(d), d


def main() -> int:
    fails: list[str] = []
    for task in TASKS:
        code, out, err = run(task)
        if code != 0:
            fails.append(f"[RUN] {task} 渲染退出码 {code}：{err.strip()[:200]}")
            continue
        fails.extend(check(task, read_lapian(task)))
    # F 剥层夹具
    fxd, fxp = stripped_fixture()
    r = subprocess.run([sys.executable, str(RENDER), "--id", fxd, "--spec", "jimeng-lapian"],
                       capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT))
    if r.returncode != 0:
        fails.append(f"[F] 剥层夹具崩溃 exit={r.returncode}：{r.stderr.strip()[:200]}")
    else:
        fmd = (fxp / "拉片-jimeng-lapian.md").read_text(encoding="utf-8")
        for name, key in (("跨拉片段连续规则", "〔未取证〕"),
                          ("声音基调", "〔未取证〕"), ("类型媒介", "〔未取证〕")):
            m = re.search(rf"(?:[-【]\s*){name}[：:】](.*?)(?=\n[-【]|\Z)", fmd, re.S)
            if not m or key not in m.group(1):
                fails.append(f"[F] 剥层后「{name}」应带「{key}」")
        for line in fmd.splitlines():
            if re.fullmatch(r"- [^：]+：\s*", line):
                fails.append(f"[F] 剥层夹具出现占位空值行：{line!r}")
    shutil.rmtree(fxp.parent, ignore_errors=True)

    total = len(TASKS) + 1
    if fails:
        print(f"{len(fails)} FAIL / M1 验收未过")
        for f in fails:
            print(" -", f)
        return 1
    print(f"M1 验收：{total} 组夹具全过（A DNA9 / B 时段子块 / C 衔接块 / D 无裸空 / E 图号 / F 剥层）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
