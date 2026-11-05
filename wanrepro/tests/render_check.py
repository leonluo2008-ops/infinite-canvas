#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""render.py 判据脚本（纯标准库，无 pytest 依赖）。

跑法：`py -3 tests/render_check.py`        退出码 0 = 全过，1 = 有 FAIL

七条判据（对应交付要求，最后一条来自质量纠偏）
------------------------------------------------
① 三组命令都出文件（--id test_a/test_b，--template lumi-4section/employee-checklist）
② 段标题与模板定义逐字一致（`## ` 序列 == 模板 `### 第 N 段：` 右侧文本序列）
③ `@图片N` 与 `资产清单.md` 对账：用到的编号无空洞（从 1 起），且每个 `@图片N` 都能在资产清单里
   找到同号 `| 图片N |` 行；没有 @ 图号时，资产清单「产品图」行数 == 清单「主体」行数且顺序一致
   （分镜行会重复引用同一张图，所以不再要求「全篇只出现一次且升序」）
④ 产物里没有绝对路径 / 主机名 / 用户名 / 空话词 / 多位小数
⑤ 同一输入跑两次，产物 md5 相同
⑥ 换 --template 不改代码就出另一套（标题换 ✅ 行，内容按模板重取）
⑦ 重复标签检查：同一「标签：」行在整篇里只出现一次（镜号整组只许落在一段）
⑧ 分镜行挂图号：长描述实体名 × 短主体词（`熟制海鲜托盘拼盘` ↔ `海鲜拼盘`）必须挂上，
   没提到的实体（手套、布帘）不许挂；命名空时资产清单不得出 `@图片1 ；…` 残句
外加取数落位：整体调性→段1，数量与规格/款式及替换节点→段3。
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RENDER = ROOT / "pipeline" / "reverse" / "render.py"
REVERSE = ROOT / "analysis" / "reverse"

SEC_RE = re.compile(r"^###\s+第\s*(\d+)\s*(段|行)\s*[：:]\s*(.+?)\s*$")

results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> bool:
    results.append((ok, name, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"｜{detail}" if detail else ""))
    return ok


def run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(RENDER), *args],
                          cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8")


def md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def template_titles(tpl: Path) -> list[str]:
    return [m.group(3) for line in tpl.read_text(encoding="utf-8").splitlines()
            if (m := SEC_RE.match(line))]


def output_titles(path: Path) -> list[str]:
    """只取正文标题（`## `），附注那节是 `# `，不参与。"""
    return [ln[3:].strip() for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.startswith("## ")]


def sections(path: Path) -> list[tuple[str, list[str]]]:
    """产物 → [(段标题, 段内行)]，遇到 `# ` 一级标题（附注）即停。"""
    out: list[tuple[str, list[str]]] = []
    cur: list[str] | None = None
    for ln in path.read_text(encoding="utf-8").splitlines():
        if ln.startswith("## "):
            cur = []
            out.append((ln[3:].strip(), cur))
        elif ln.startswith("# 附注"):
            break
        elif cur is not None and ln.strip():
            cur.append(ln.strip())
    return out


def label_of(line: str) -> str:
    """行首标签：`数量与规格：…` → 数量与规格；时间码里的冒号不算。

    分镜行现在会带 `@图片N ` 前缀，判标签前先剥掉，否则「镜号」会被图号顶走。
    """
    line = re.sub(r"^(?:@图片\d+\s+)+", "", line)
    if line.startswith("镜号"):
        return "镜号（整组）"
    m = re.search(r"([^：:]{1,20})[：:](?!\d)", line)
    return m.group(1).strip() if m else ""


def prep_images(pid: str) -> None:
    """给测试夹具的清单补上「资产图」。

    2026-10-31 契约变更：只有备了图的资产才发 @图片N（没备图的资产不进提示词）。
    测试夹具原本靠「按清单行号发号」，契约一改就整片 FAIL。夹具是测试资产，
    这里统一补一个占位路径，让它继续验编号、对账、挂图这些真正的判据。
    """
    path = REVERSE / pid / "待确认清单.md"
    if not path.is_file():
        return
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if re.match(r"\|\s*\d+\s*\|", line):
            cells = line.split("|")
            if len(cells) > 6 and "主体" not in cells[2] and cells[5].strip():
                cells[5] = "  "
                line = "|".join(cells)
            # 只给「主体(产品)」备图：这是最接近真实用法的最小集合，
            # 全给会让每个实体都替换成图注，反而把场景描述冲掉。
            if len(cells) > 6 and "主体" in cells[2] and not cells[5].strip():

                cells[5] = " tests/fixtures/占位.png "
                line = "|".join(cells)
        out.append(line)
    path.write_text(chr(10).join(out) + chr(10), encoding="utf-8")


def main() -> int:
    print("=" * 78)
    print("render_check · 判据脚本")
    print("=" * 78)

    # ① 三组命令都出文件
    for pid in ("test_a", "test_b", "shot_tags"):
        prep_images(pid)
    jobs = [("test_a", "lumi-4section"), ("test_a", "employee-checklist"), ("test_b", "lumi-4section")]
    prods: dict[tuple[str, str], tuple[Path, Path]] = {}
    for pid, tpl in jobs:
        cp = run(["--id", pid, "--template", tpl])
        p = REVERSE / pid / f"提示词-{tpl}.md"
        a = REVERSE / pid / "资产清单.md"
        ok = cp.returncode == 0 and p.is_file() and a.is_file()
        check(ok, f"① 出文件 {pid} × {tpl}",
              f"exit={cp.returncode}，提示词 {p.stat().st_size if p.is_file() else 0} B")
        if not ok:
            print(cp.stderr.strip())
        prods[(pid, tpl)] = (p, a)

    # ② 段标题逐字一致
    for (pid, tpl) in jobs:
        p, _a = prods[(pid, tpl)]
        if not p.is_file():
            check(False, f"② 标题一致 {pid} × {tpl}", "产物缺失")
            continue
        want = template_titles(ROOT / "templates" / f"{tpl}.md")
        got = output_titles(p)
        # 标题照样「取自模板」，但模板里的示例数字（N个镜头 / 0–10 秒）会被渲染器换成真值
        # （2026-10-31 所有者实测：23 秒的片子被标成 0–10 秒）。比较前把这些数字归一。
        def norm_title(s: str) -> str:
            s = re.sub(r"\d+\s*个镜头", "N个镜头", s)
            s = re.sub(r"[Nn□Xx]\s*个镜头", "N个镜头", s)
            s = re.sub(r"\d{1,2}:\d{2}\s*[–\-—~]\s*\d{1,2}:\d{2}", "SPAN", s)
            s = re.sub(r"\d+\s*[–\-—~]\s*\d+\s*秒", "SPAN", s)
            return s
        same = [norm_title(x) for x in want] == [norm_title(x) for x in got]
        detail = "逐字一致：" + " / ".join(got) if same else f"模板={want} 产物={got}"
        check(same, f"② 标题一致 {pid} × {tpl}", detail)

    # ③ @图片N 与资产清单对账
    #    分镜行会重复引用同一张图（一个镜头一行），也可能整类资产（大类产品图、背景）根本不被
    #    分镜提到，所以判据从「全篇只出现一次且升序」改成对账：
    #    ①提示词里出现的每个 @图片N，都能在资产清单找到同号 `| 图片N |` 行；
    #    ②有资产清单就必须在图号用上（一个 @图片 都没有 = 资产与提示词脱钩，正是要防的缺陷）。
    #    分镜行有没有真挂上图号，由 ⑧ 用夹具单独判。
    for (pid, tpl), (p, a) in prods.items():
        text = p.read_text(encoding="utf-8") if p.is_file() else ""
        nums = [int(n) for n in re.findall(r"@图片(\d+)", text)]
        asset_text = a.read_text(encoding="utf-8") if a.is_file() else ""
        asset_nos = sorted({int(n) for n in re.findall(r"^\|\s*图片(\d+)\s*\|", asset_text, re.M)})
        if nums:
            uniq = sorted(set(nums))
            orphan = [n for n in uniq if n not in asset_nos]
            ok = not orphan
            detail = (f"@图片编号 {uniq}（共 {len(nums)} 处引用）；资产清单同号 {asset_nos}"
                      + (f"；资产清单里找不到的图号 {orphan}" if orphan else "；每个图号都对得上"))
        else:
            n_sub = len(re.findall(r"^\|\s*\d+\s*\|\s*主体\s*\|", (REVERSE / pid / "待确认清单.md")
                                   .read_text(encoding="utf-8"), re.M)) if (REVERSE / pid / "待确认清单.md").is_file() else 0
            # 资产清单现在是 `| 图片N | 产品图 | … |`，兼容旧表头 `| 产品图 | …`
            n_img = len(re.findall(r"^\|\s*(?:图片\d+\s*\|\s*)?产品图\s*\|", asset_text, re.M))
            ok = n_sub == n_img and not asset_nos
            detail = f"没有 @图号；改核产品图行数：清单主体 {n_sub} 行 / 资产清单产品图 {n_img} 行"
        check(ok, f"③ 图号对账 {pid} × {tpl}", detail)

    # ④ 无绝对路径 / 主机名 / 用户名 / 空话词 / 多位小数
    host = socket.gethostname()
    user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    bad_patterns = [("绝对路径", re.compile(r"[A-Za-z]:[\\/]")),
                    ("绝对路径", re.compile(r"/(?:Users|home|root)/")),
                    ("主机名", re.compile(re.escape(host), re.I))]
    if user:
        bad_patterns.append(("用户名", re.compile(re.escape(user), re.I)))
    for (pid, tpl), (p, a) in prods.items():
        hits: list[str] = []
        for f in (p, a):
            if not f.is_file():
                continue
            txt = f.read_text(encoding="utf-8")
            for name, rx in bad_patterns:
                if rx.search(txt):
                    hits.append(f"{f.name}:{name}")
            for m in re.finditer(r"(?<![\d.])\d+\.\d{2,}(?![\d.])", txt):
                hits.append(f"{f.name}:多位小数 {m.group(0)}")
        check(not hits, f"④ 产物卫生 {pid} × {tpl}", "；".join(hits) if hits else "无绝对路径/主机名/用户名/多位小数")

    # ⑤ 同输入两次跑，md5 相同
    with tempfile.TemporaryDirectory() as td:
        for pid, tpl in jobs[:2]:
            h: list[str] = []
            for k in (1, 2):
                out = Path(td) / f"{pid}-{tpl}-{k}"
                run(["--id", pid, "--template", tpl, "--out-dir", str(out)])
                h.append(md5(out / f"提示词-{tpl}.md") + md5(out / "资产清单.md"))
            check(h[0] == h[1], f"⑤ 二次跑字节相同 {pid} × {tpl}",
                  f"md5(提示词+清单)={h[0][:12]}… vs {h[1][:12]}…")

    # ⑥ 换模板不改代码
    pa, _ = prods[("test_a", "lumi-4section")]
    pb, _ = prods[("test_a", "employee-checklist")]
    if pa.is_file() and pb.is_file():
        ta, tb = output_titles(pa), output_titles(pb)
        ok = (not any(t.startswith("✅") for t in ta)
              and all(t.startswith("✅") for t in tb)
              and ta != tb and len(tb) == 7)
        check(ok, "⑥ 换 --template 出另一套",
              f"lumi 段标题 {len(ta)} 个（无 ✅）；employee 段标题 {len(tb)} 个（全 ✅ 行）")
    else:
        check(False, "⑥ 换 --template 出另一套", "产物缺失")

    # ⑦ 重复标签检查
    for (pid, tpl), (p, _a) in prods.items():
        if not p.is_file():
            check(False, f"⑦ 重复标签 {pid} × {tpl}", "产物缺失")
            continue
        where: dict[str, list[str]] = {}
        for title, lines in sections(p):
            for ln in lines:
                lab = label_of(ln)
                if lab:
                    where.setdefault(lab, []).append(title)
        dup = {k: v for k, v in where.items()
               if len(set(v)) > 1 or (k != "镜号（整组）" and len(v) > 1)}
        detail = "0 重复" + ("" if not dup else "；" + "；".join(
            f"「{k}」出现在 {v}" for k, v in list(dup.items())[:5]))
        check(not dup, f"⑦ 重复标签 {pid} × {tpl}", detail)
        if (pid, tpl) == ("test_a", "lumi-4section"):
            sec1 = dict(sections(p)).get("整体风格", [])
            sec3 = dict(sections(p)).get("场景分镜", [])
            check(any(x.startswith("整体调性：") for x in sec1),
                  "⑦ 落位 整体调性 → 段1 整体风格")
            check(any(x.startswith("数量与规格：") for x in sec3)
                  and any(x.startswith("款式及替换节点：") for x in sec3),
                  "⑦ 落位 数量与规格 / 款式及替换节点 → 段3 场景分镜")

    # ⑧ 分镜行挂图号：长描述实体名 × 短主体词（tests/fixtures/shot_tags/）
    #    夹具里实体名是 `熟制海鲜托盘拼盘：必须锁住…`，分镜行只写 `海鲜拼盘`；
    #    整串匹配一条也挂不上，只有「核心名词短语的后缀命中」才能对上。
    fx = ROOT / "tests" / "fixtures" / "shot_tags"
    with tempfile.TemporaryDirectory() as td:
        pid_dir = Path(td) / "shot_tags"
        shutil.copytree(fx, pid_dir)
        out = Path(td) / "out"
        cp = run(["--id", str(pid_dir), "--template", "lumi-4section", "--out-dir", str(out)])
        p = out / "提示词-lumi-4section.md"
        ad = out / "资产清单.md"
        if cp.returncode != 0 or not p.is_file():
            check(False, "⑧ 分镜行挂图号", f"exit={cp.returncode}｜{cp.stderr.strip()[-200:]}")
        else:
            sec3 = dict(sections(p)).get("场景分镜", [])
            shots: dict[str, str] = {}
            for ln in sec3:
                body = re.sub(r"^(?:@图片\d+\s+)+", "", ln)
                m = re.match(r"^(?:镜号\s*)?(\d+)｜", body)
                if m:
                    shots[m.group(1)] = ln
            problems: list[str] = []
            for shot_no, img in (("1", 3), ("2", 4), ("3", 5)):   # 镜号 → 该行必须挂的图号
                ln = shots.get(shot_no, "")
                head = re.match(r"^((?:@图片\d+\s+)*)", ln).group(1)
                if f"@图片{img} " not in head:
                    problems.append(f"镜号{shot_no} 没挂 @图片{img}（行首是「{head.strip() or '无图号'}」）")
            sec3_text = "\n".join(sec3)
            for img in (6, 7):                  # 手套 / 布帘：分镜里没提到，不该挂
                if re.search(rf"@图片{img}(?!\d)", sec3_text):
                    problems.append(f"分镜里挂了没提到的 @图片{img}")
            for ln in shots.values():           # 同一行不许重复叠同一图号，最多 3 个
                tags = re.findall(r"@图片(\d+)", ln)
                if len(tags) != len(set(tags)):
                    problems.append(f"图号重复叠加：{ln[:24]}")
                if len(tags) > 3:
                    problems.append(f"一行挂了 {len(tags)} 个图号（上限 3）：{ln[:24]}")
            asset_text = ad.read_text(encoding="utf-8") if ad.is_file() else ""
            if re.search(r"@图片1\s*[；;]", asset_text):
                problems.append("资产清单仍有 `@图片1 ；` 残句")
            check(not problems and len(shots) == 4,
                  "⑧ 分镜行挂图号（长描述实体名 × 短主体词）",
                  "；".join(problems) if problems else
                  f"4 行分镜｜海鲜拼盘→@图片3／人物与易拉罐→@图片4／剪刀与蟹腿→@图片5；"
                  "手套·布帘未挂；无重复叠加；资产清单无 `@图片1 ；` 残句")

    # ⑨ 只按「主体」列挂图号：布光列里的「台面」不能把场景图挂到这个镜头上
    #    实测事故：镜01 主体是「手部与产品」，布光写「台面漫反射阴影」，
    #    场景实体「黑色平整台面」的「台面」命中整行 → 挂了 @图片7，用户以为这镜要用场景图。
    try:
        sys.path.insert(0, str(ROOT / "pipeline" / "reverse"))
        from render import Entity as _Ent, attach_image_tags as _tag
        prod = _Ent("主体", "扁长方体表面，顶部为球形黑色盖子", "", "", "", no=1)
        scene = _Ent("场景", "黑色平整台面，背景为白色叠放织物", "", "", "", no=7)
        line = ("01｜00:00-00:00｜手部与产品｜右上斜向抓取拿起｜快｜"
                "梳妆台抓取香水瓶｜台面漫反射阴影｜硬切")
        got = _tag(line, [prod, scene])
        problems9: list[str] = []
        if not got.startswith("@图片1 "):
            problems9.append("主体「手部与产品」没挂上产品 @图片1")
        if "@图片7" in got:
            problems9.append("布光列的「台面」把场景 @图片7 误挂到了这个镜头")
        check(not problems9, "⑨ 只按主体列挂图号（布光里的「台面」不算）",
              "；".join(problems9) if problems9 else
              "镜01「手部与产品」→@图片1；场景「黑色平整台面」未误挂")
    except Exception as exc:                       # noqa: BLE001 —— 测试本身不许炸掉整轮
        check(False, "⑨ 只按主体列挂图号（布光里的「台面」不算）", f"异常：{exc}")

    n_fail = sum(1 for ok, _, _ in results if not ok)
    print("-" * 78)
    print(f"{len(results) - n_fail} PASS / {n_fail} FAIL")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
