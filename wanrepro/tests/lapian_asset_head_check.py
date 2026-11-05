"""拉片投影「核心主体资产」资产行解析容错（回归：pop-2 出现于区间丢失）。

历史 bug：`re.sub(r"^\\*{0,2}|：.*$", "", ln)` 在第一个全角冒号处切断，
而资产行是「人物（出现于：00:00-00:04）：描述」——名内括号里的冒号被误当
分隔符，切出「人物（出现于」这种畸形名，时间区间整段丢失。
本组断言 asset_head 的容错口径：
  U  全角/半角/混用 括号与冒号都能在正确位置切断
  T  截断行（无闭合括号/无分隔冒号）不崩、不丢字
  E  端到端：全角/半角/截断三种夹具投影后 DNA 行主体名完整、时间区间在位
"""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RENDER = ROOT / "pipeline" / "reverse" / "render_lapian.py"

sys.path.insert(0, str(ROOT / "pipeline" / "reverse"))
from render_lapian import asset_head  # noqa: E402


# ---- U：asset_head 单元口径 -------------------------------------------------
UNIT_CASES = [
    # (输入行, 期望主体名, 说明)
    ("人物（出现于：00:00-00:04）：人种不明，浅肤色", "人物（出现于：00:00-00:04）", "全角括号+全角冒号"),
    ("人物(出现于:00:00-00:04):人种不明", "人物(出现于:00:00-00:04)", "半角括号+半角冒号"),
    ("道具（手持:摆位）:玻璃管", "道具（手持:摆位）", "全角括号内半角冒号（括号保护）"),
    ("服装A（出现于：00:00-01:53、03:63-04:50）：黑裙",
     "服装A（出现于：00:00-01:53、03:63-04:50）", "多时间段（conn-test-2 形制）"),
    ("人物：浅肤色，手部修长", "人物", "无括号普通行——旧口径保持"),
    ("人物 00:00-00:04：浅肤色", "人物 00:00-00:04", "无括号半角时刻不误切（数字夹住的冒号）"),
    ("人物（出现于：00:00-00:04", "人物（出现于：00:00-00:04", "截断：无闭合括号→原样保留"),
    ("人物（出现于", "人物（出现于", "截断：仅名前缀→原样保留"),
    ("人物", "人物", "截断：裸名→原样"),
    ("**人物（出现于：00:00）**：描述", "人物（出现于：00:00）", "加粗残留防御（sanitize 前调用也不怕）"),
    ("场景（出现于：00:00-00:05）", "场景（出现于：00:00-00:05）", "有名无描述行→整段保留"),
]


def unit_cases() -> list[str]:
    errs = []
    for line, want, why in UNIT_CASES:
        got = asset_head(line)
        if got != want:
            errs.append(f"[U] {why}：asset_head({line!r}) = {got!r}，期望 {want!r}")
    return errs


# ---- E：端到端投影夹具 -------------------------------------------------------
SPEC_LINE = "> 规格：1080×1920 · 9:16 竖屏 · 30 fps · 全片 5 秒 · 硬切 0 次"
ROWS_FULL = [  # 全角形制 = pop-2 实况
    "* **人物（出现于：00:00-00:04）**：人种不明，浅肤色。",
    "* **道具（手持，出现于：00:00-00:04）**：香水玻璃管。",
    "* **道具（摆件，出现于：00:00-00:05）**：红西柚、柑橘。",
    "* **场景（出现于：00:00-00:05）**：静物摄影台。",
]
ROWS_HALF = [  # 半角形制
    "* 人物(出现于:00:00-00:04):浅肤色。",
    "* 道具(手持,出现于:00:00-00:05):玻璃管。",
    "* 场景(出现于:00:00-00:05):摄影台。",
]
ROWS_TRUNC = [  # 截断形制
    "* 人物（出现于：00:00-00:04",
    "* 道具（手持",
    "* 场景",
]
WANT_HEADS = {
    "full": ["人物（出现于：00:00-00:04）", "道具（手持，出现于：00:00-00:04）",
             "道具（摆件，出现于：00:00-00:05）", "场景（出现于：00:00-00:05）"],
    "half": ["人物(出现于:00:00-00:04)", "道具(手持,出现于:00:00-00:05)",
             "场景(出现于:00:00-00:05)"],
    "trunc": ["人物（出现于：00:00-00:04", "道具（手持", "场景"],
}


def make_fixture(base: Path, name: str, rows: list[str]) -> Path:
    d = base / name
    d.mkdir(parents=True)
    body = [f"# 拆解 · {name}", "", SPEC_LINE, "", "## 九 资产", *rows, ""]
    (d / "拆解.md").write_text("\n".join(body), encoding="utf-8")
    return d


def e2e_cases() -> list[str]:
    errs = []
    base = Path(tempfile.mkdtemp(prefix="lapian-head-"))
    try:
        for name, rows in (("fx-full", ROWS_FULL), ("fx-half", ROWS_HALF), ("fx-trunc", ROWS_TRUNC)):
            d = make_fixture(base, name, rows)
            r = subprocess.run([sys.executable, str(RENDER), "--id", str(d),
                                "--spec", "jimeng-lapian"],
                               capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT))
            if r.returncode != 0:
                errs.append(f"[E] {name} 渲染退出码 {r.returncode}：{r.stderr.strip()[:200]}")
                continue
            md = (d / "拉片-jimeng-lapian.md").read_text(encoding="utf-8")
            m = re.search(r"- 核心主体资产：(.+)", md)
            if not m:
                errs.append(f"[E] {name} 无「核心主体资产」行")
                continue
            line = m.group(1)
            for head in WANT_HEADS[name.split("-")[1]]:
                if head not in line:
                    errs.append(f"[E] {name} 主体名不完整：缺「{head}」，整行：{line!r}")
            if "出现于；" in line or "出现于（" in line:
                errs.append(f"[E] {name} 出现于区间仍被切断：{line!r}")
            if name != "fx-trunc" and "（图号绑定〔未取证〕" not in line:
                errs.append(f"[E] {name} 丢了图号绑定态标注：{line!r}")
    finally:
        import shutil
        shutil.rmtree(base, ignore_errors=True)
    return errs


def main() -> int:
    fails = unit_cases() + e2e_cases()
    if fails:
        print(f"{len(fails)} FAIL / 资产头解析容错未过")
        for f in fails:
            print(" -", f)
        return 1
    print(f"资产头解析容错全过（U 单元 {len(UNIT_CASES)} 例 / E 端到端 全角+半角+截断 3 夹具）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
