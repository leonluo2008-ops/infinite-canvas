"""T5 存在性空态行隔离（路 A 审计 B1+S3 回归门）。

拉片提示词新增空态行：`* 人物：无（全片无人物出镜）`、`* 服装：无`、
`* 人物：无完整人物（仅手部入画…）`。修复前下游把 `*` 开头行一律当资产条目：
空态被 confirm 抽成人物/服装实体，render_assets 产出
「| — | 人物参考图 | 清单第 3 行 · 人物：无完整人物… |」——要求给不存在的人备图；
拉片投影「核心主体资产」也摘要出「人物；服装」，与固有资产块里的「无」自相矛盾。
本组守住四件事：
  A  空态行逐项隔离：不产实体（extract_assets 零行）；判定口径对真实条目不误伤
  B  样例二式组合链路：空态不进清单、不产备图行；真实手部道具（道具B）照常在
  C  拉片投影：核心主体资产不含空态名；固有资产块保留否定全文；
     九 层只剩空态时按未取证标注，不冒充有资产
  R  既有 asset_rows_check / lapian_asset_head_check 不回归
"""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RENDER = ROOT / "pipeline" / "reverse" / "render_lapian.py"
SPEC_LINE = "> 规格：1080×1920 · 9:16 竖屏 · 30 fps · 全片 5 秒 · 硬切 0 次"

sys.path.insert(0, str(ROOT / "pipeline" / "reverse"))
import confirm as C  # noqa: E402
import render as R  # noqa: E402

results: list[tuple[bool, str]] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    results.append((ok, name))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"｜{detail}" if detail else ""))


# ---- A：空态行逐项隔离（审计同款探针）----------------------------------------
ABSENT_LINES = [
    "* 人物：无（全片无人物出镜）",
    "* 服装：无",
    "* 人物：无完整人物（仅手部入画，手的外形写在道具B里）",
]

REAL_LINES_NOT_ABSENT = [
    # 正资产必带资产名（契约），描述以「无」开头也不能误伤（如「无袖连衣裙」）
    "**人物（出现于：镜 1、4、7）**：20-25 岁女性，脸型、肤色、发型、身高体型",
    "**服装A（出现于：镜 1、7）**：无袖黑色连衣裙",
    "人物：浅肤色，手部修长",
    "道具A（出现于：00:01-00:05）：台面中央的扁平圆柱容器",
]


def section_a() -> None:
    for line in ABSENT_LINES + ["* **人物**：无（全片无人物出镜）", "- 动物：无",
                                 "人物: 无"]:   # 加粗/动物防御/半角冒号变体
        rows = C.extract_assets(line, [])
        check(rows == [], f"A1 空态不产实体：{line.strip()}", f"实际 {rows}")
        check(C.is_absent_state(line), f"A2 判定命中：{line.strip()}")
    for line in REAL_LINES_NOT_ABSENT:
        rows = C.extract_assets(f"* **{line}**" if not line.startswith("**") else f"* {line}", [])
        check(rows != [], f"A3 真实条目不误伤：{line[:24]}…", f"被过滤成 {rows}")
        check(not C.is_absent_state(line), f"A4 判定不命中：{line[:24]}…")


# ---- B：样例二式组合链路（build_rows → 待确认清单 → render_assets）-----------
NINE_BODY = "\n".join([
    "* 人物：无完整人物（仅手部入画，手的外形写在道具B里）",
    "* 服装：无",
    "* **道具A（出现于：00:01-00:05）**：台面中央陈列的扁平圆柱容器，哑光质感",
    "* **道具B（出现于：00:02-00:04）**：入画取物的手，浅肤色、骨节匀称、只到手腕",
    "* **场景A（出现于：全片）**：米白哑光台面，均匀柔光",
])
LAYERS = {
    "一 场景": "* 米白哑光台面，均匀柔光，四周留白干净",
    "二 产品": "* 台面中央陈列的扁平圆柱容器，哑光质感（00:01-00:05）",
    "三 运动": "* 运动主体：一只手从画面下方入画，取走台面中央的容器\n"
               "* 轨迹与方向：自台面中央向画面下方匀速移出",
    "七 分镜表": "| 镜号 | 景别 | 画面内容 | 运动形态 |\n|---|---|---|---|\n"
                 "| 01 | 特写 | 台面中央的容器 | 静止 |\n"
                 "| 02 | 特写 | 手部入画取走容器 | 手部入画取物 |",
    "九 资产": NINE_BODY,
}


def section_b() -> None:
    rows = C.build_rows(LAYERS)
    bad = [r for r in rows if r["类别"] in ("人物", "服装")]
    check(not bad, "B1 空态不进待确认清单（无人物/服装行）",
          f"类别={[r['类别'] for r in rows]}")
    leaked = [r for r in rows if "无完整人物" in str(r.get("看到什么")) or "全片无人物出镜" in str(r.get("看到什么"))]
    check(not leaked, "B2 空态正文不进任何清单行", f"{[r['看到什么'] for r in leaked]}")
    hand = [r for r in rows if r["类别"] == "道具" and "手" in str(r.get("看到什么"))]
    check(bool(hand), "B3 真实手部道具仍在（道具B 不被误伤）",
          f"道具行={[r['看到什么'][:18] for r in rows if r['类别'] == '道具']}")

    checklist = C.render_checklist("absent-fx", rows)
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "待确认清单.md"
        path.write_text(checklist, encoding="utf-8")
        ents = R.assign_numbers(R.parse_checklist(path))
    assets_md = R.render_assets(ents, {"九": NINE_BODY}, "absent-fx")
    data_lines = [ln for ln in assets_md.splitlines()
                  if re.match(r"^\| (图片\d+|—) \| ", ln)]   # 只看数据行；图例行含「人物参考图」字样
    check(not any("人物参考图" in ln for ln in data_lines), "B4 资产表无人物参考图备图行",
          f"数据行={data_lines}")
    check(not any("服装参考图" in ln for ln in data_lines), "B5 资产表无服装参考图备图行",
          f"数据行={data_lines}")
    check("无完整人物" not in assets_md, "B6 空态正文不进资产表")
    check(any("道具参考图" in ln for ln in data_lines), "B7 手部道具备图位正常",
          f"数据行={data_lines}")


# ---- C：拉片投影（核心主体资产 vs 固有资产块）---------------------------------
def run_lapian(d: Path) -> str:
    r = subprocess.run([sys.executable, str(RENDER), "--id", str(d),
                        "--spec", "jimeng-lapian"],
                       capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT))
    if r.returncode != 0:
        raise AssertionError(f"render_lapian 退出码 {r.returncode}：{r.stderr.strip()[:300]}")
    return (d / "拉片-jimeng-lapian.md").read_text(encoding="utf-8")


def make_fixture(base: Path, name: str, nine_lines: list[str]) -> Path:
    d = base / name
    d.mkdir(parents=True)
    body = [f"# 拆解 · {name}", "", SPEC_LINE, "", "## 九 资产", *nine_lines, ""]
    (d / "拆解.md").write_text("\n".join(body), encoding="utf-8")
    return d


def section_c() -> None:
    base = Path(tempfile.mkdtemp(prefix="absent-state-"))
    try:
        # C1 混合层：空态被跳过、真实资产名保留、否定全文留在固有资产块
        d = make_fixture(base, "fx-mixed", NINE_BODY.splitlines())
        md = run_lapian(d)
        m = re.search(r"- 核心主体资产：(.+)", md)
        check(bool(m), "C1 投影有核心主体资产行")
        if m:
            head = m.group(1)
            check("人物" not in head and "服装" not in head,
                  "C2 摘要不含空态名（不再是「人物；服装」）", head)
            check("道具A（出现于：00:01-00:05）" in head and "场景A（出现于：全片）" in head,
                  "C3 真实资产名完整保留", head)
        check("人物：无完整人物（仅手部入画，手的外形写在道具B里）" in md
              and "- 服装：无" in md, "C4 固有资产块保留否定全文")

        # C2 九 层只剩空态：按未取证标注，不冒充有资产
        d2 = make_fixture(base, "fx-only-null", ABSENT_LINES)
        md2 = run_lapian(d2)
        m2 = re.search(r"- 核心主体资产：(.+)", md2)
        check(bool(m2), "C5 纯空态投影有核心主体资产行")
        if m2:
            check("未取证" in m2.group(1) and "人物" not in m2.group(1)
                  and "服装" not in m2.group(1).split("（")[0],
                  "C6 纯空态摘要走未取证，不摘要出人物/服装", m2.group(1))
    finally:
        import shutil
        shutil.rmtree(base, ignore_errors=True)


# ---- R：既有回归不破 ----------------------------------------------------------
def section_r() -> int:
    fails = 0
    for name in ("asset_rows_check.py", "lapian_asset_head_check.py"):
        r = subprocess.run([sys.executable, str(ROOT / "tests" / name)],
                           capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT))
        ok = r.returncode == 0
        check(ok, f"R1 既有回归 {name}", "" if ok else r.stdout[-500:] + r.stderr[-300:])
        fails += 0 if ok else 1
    return fails


def main() -> int:
    section_a()
    section_b()
    section_c()
    section_r()
    passed = sum(1 for ok, _ in results if ok)
    print(f"\n{passed} PASS / {len(results) - passed} FAIL")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
