#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""S0.7 模板回归测试：改模板后能不能一眼看出产物变了。

做法：固定夹具 `analysis/reverse/test_a/`，把三套模板渲出的产物存进
`tests/fixtures/template_baseline/`，以后改模板跑一遍就能 diff。

跑法
----
    py -3 tests/template_regression.py            # 比对（基线缺失则写入）
    py -3 tests/template_regression.py --update   # 重建基线（改模板后确认无误再跑）

负向用例（必须 FAIL，FAIL 才是通过）
------------------------------------
① 删掉某段的 `- 写什么：` 字段 → 校验应 exit=3（四要素缺一项）
② 拆解里抽掉 `## 八 声音` 而模板还要它 → 应 exit=4（缺层）
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RENDER = ROOT / "pipeline" / "reverse" / "render.py"
FIXTURE_ID = "test_a"
BASELINE = ROOT / "tests" / "fixtures" / "template_baseline"
TEMPLATES = ("lumi-4section", "employee-checklist", "seven-section", "timeline-narrative",
             "seedance-flow")

results: list[tuple[bool, str, str]] = []


def flow_checks(text: str) -> list[str]:
    """单段流式正文三断言（ROADMAP C0.1）：无标题/无附注、≥3 时段、引用序==图序。

    返回错误清单；空清单 = 通过。断言只读产物文本，不看模板措辞。
    """
    errs: list[str] = []
    if re.search(r"^#{1,6}\s", text, re.M):
        errs.append("出现 Markdown 标题行")
    if "附注" in text:
        errs.append("出现附注")
    segs = re.findall(r"\d+\s*-\s*\d+\s*秒", text)
    if len(segs) < 3:
        errs.append(f"时段只有 {len(segs)} 段（<3）")
    order: list[int] = []
    for n in (int(x) for x in re.findall(r"@图片(\d+)", text)):
        if n not in order:
            order.append(n)
    if order != sorted(order):
        errs.append(f"引用序≠图序：{order}")
    return errs



def check(ok: bool, name: str, detail: str = "") -> bool:
    results.append((ok, name, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"｜{detail}" if detail else ""))
    return ok


def run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(RENDER), *args],
                          cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8")


def md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def same_text(a: Path, b: Path) -> bool:
    """比「内容」而不是比「字节」。

    实测（2026-10-30）：仓库 core.autocrlf=true，切分支时 Git 把基线的 LF 换成 CRLF，
    而渲染器新写出来的是 LF——字节不同、内容一字不差，回归却报 6 个 FAIL，
    报错还写着「行数不同：基线 52 行 / 本次 52 行」（zip 逐行比完全一致，
    最后落到长度那句兜底说明上），极易被误判成真回归。
    read_text 默认走 universal newlines，CRLF / CR / LF 一律归一成 LF，直接比字符串即可。
    """
    return a.read_text(encoding="utf-8") == b.read_text(encoding="utf-8")


def first_diff(a: Path, b: Path) -> str:
    la = a.read_text(encoding="utf-8").splitlines()
    lb = b.read_text(encoding="utf-8").splitlines()
    for i, (x, y) in enumerate(zip(la, lb), 1):
        if x != y:
            return f"第 {i} 行\n    基线：{x[:80]}\n    本次：{y[:80]}"
    return f"行数不同：基线 {len(la)} 行 / 本次 {len(lb)} 行"


def render_to(pid: str, tpl: str, out: Path) -> subprocess.CompletedProcess:
    return run(["--id", pid, "--template", tpl, "--out-dir", str(out)])


def main(argv: list[str]) -> int:
    update = "--update" in argv
    print("=" * 78)
    print("template_regression · 固定夹具 analysis/reverse/test_a/")
    print("=" * 78)

    BASELINE.mkdir(parents=True, exist_ok=True)

    # ① 三套模板各渲一次，与基线比字节
    with tempfile.TemporaryDirectory() as td:
        for tpl in TEMPLATES:
            out = Path(td) / tpl
            cp = render_to(FIXTURE_ID, tpl, out)
            if cp.returncode != 0:
                check(False, f"渲染 {tpl}", f"exit={cp.returncode}｜{cp.stderr.strip()[-200:]}")
                continue
            check(True, f"渲染 {tpl}", f"exit=0，产物 {len(list(out.iterdir()))} 个")
            if tpl == "seedance-flow":
                errs = flow_checks((out / f"提示词-{tpl}.md").read_text(encoding="utf-8"))
                check(not errs, "流式正文三断言（无标题附注 / ≥3 时段 / 引用序==图序）",
                      "；".join(errs) if errs else "3/3 通过")
            for src, name in ((out / f"提示词-{tpl}.md", f"{tpl}.提示词.md"),
                              (out / "资产清单.md", f"{tpl}.资产清单.md")):
                base = BASELINE / name
                if update:
                    shutil.copyfile(src, base)
                    check(True, f"写入基线 {name}", f"md5={md5(base)[:12]}…")
                    continue
                if not base.is_file():
                    # 基线缺失即 FAIL（ROADMAP C0.1）：没有基线比不了，不能静默当通过。
                    check(False, f"基线缺失 {name}", "跑 --update 重建基线后才算通过")
                    continue
                ok = same_text(base, src)
                check(ok, f"基线一致 {name}",
                      f"md5={md5(src)[:12]}…" if ok else first_diff(base, src))

        # ② 负向用例①：删掉一段的「写什么」→ 必须 exit=3
        broken = Path(td) / "lumi-4section.md"
        text = (ROOT / "templates" / "lumi-4section.md").read_text(encoding="utf-8")
        lines = text.splitlines()
        out_lines: list[str] = []
        removed = 0
        in_sec2 = False
        for ln in lines:
            if re.match(r"^### 第 2 段：", ln):
                in_sec2 = True
            elif ln.startswith("### ") or ln.startswith("## "):
                in_sec2 = False
            if in_sec2 and ln.startswith("- 写什么：") and removed == 0:
                removed += 1
                continue
            out_lines.append(ln)
        broken.write_text("\n".join(out_lines) + "\n", encoding="utf-8", newline="\n")
        cp = run(["--id", FIXTURE_ID, "--template", str(broken), "--out-dir", str(Path(td) / "neg1")])
        ok = cp.returncode == 3 and "写什么" in cp.stderr
        check(ok, "负向① 删掉「写什么」→ exit=3",
              f"删了 {removed} 行；exit={cp.returncode}｜{cp.stderr.strip().splitlines()[-1][:90] if cp.stderr.strip() else ''}")

        # ③ 负向用例②：拆解缺「八 声音」→ 必须 exit=4
        disc = (ROOT / "analysis" / "reverse" / FIXTURE_ID / "拆解.md").read_text(encoding="utf-8")
        cut = re.split(r"^## 八 声音\s*$", disc, flags=re.M)[0]
        neg2 = Path(td) / "neg2_id"
        neg2.mkdir()
        (neg2 / "拆解.md").write_text(cut, encoding="utf-8", newline="\n")
        shutil.copyfile(ROOT / "analysis" / "reverse" / FIXTURE_ID / "待确认清单.md",
                        neg2 / "待确认清单.md")
        cp = run(["--id", str(neg2), "--template", "lumi-4section", "--out-dir", str(Path(td) / "neg2")])
        ok = cp.returncode == 4 and "在拆解里没有" in cp.stderr
        check(ok, "负向② 拆解缺「八 声音」→ exit=4",
              f"exit={cp.returncode}｜{cp.stderr.strip().splitlines()[-1][:90] if cp.stderr.strip() else ''}")
        check(not (Path(td) / "neg2" / "提示词-lumi-4section.md").is_file(),
              "负向② 失败时不落产物")

        # ④ 负向用例③：把流式正文里的图号对调（错图序）→ 三断言必须报错
        flow_base = BASELINE / "seedance-flow.提示词.md"
        if flow_base.is_file():
            bad = (flow_base.read_text(encoding="utf-8")
                   .replace("@图片1", "@图片X").replace("@图片2", "@图片1").replace("@图片X", "@图片2"))
            errs = flow_checks(bad)
            check(bool(errs) and any("引用序" in e for e in errs),
                  "负向③ 错图序 → 断言 FAIL", "；".join(errs)[:120] or "没报错，断言失效")
        else:
            check(False, "负向③ 错图序 → 断言 FAIL", "缺基线 seedance-flow.提示词.md")

    n_fail = sum(1 for ok, _, _ in results if not ok)
    print("-" * 78)
    print(f"{len(results) - n_fail} PASS / {n_fail} FAIL")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
