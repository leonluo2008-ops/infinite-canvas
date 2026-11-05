"""画幅归一 + 提示词里不写尺寸/帧率/画质。

背景（2026-11 所有者反馈）：生成端提示词里出现了「画幅 135:236 · 尺寸 1080×1888」。
- 视频模型只认 16:9 / 9:16 / 1:1 / 4:3 / 3:4（21:9 只有少数，4:5 谁都不支持），
  135:236 这种像素比在生成平台上是没有这个选项的；
- 分辨率、帧率、画质都是生成平台上的参数，使用者自己选，不该进提示词。

判据：
① canonical_aspect 把非标准比例归到最近的标准画幅；
② 老拆解里写着 135:236，渲染出来的提示词也必须是 9:16 竖屏；
③ 提示词里不得出现「尺寸」「fps」「1080×1888」。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REVERSE = ROOT / "pipeline" / "reverse"
sys.path.insert(0, str(REVERSE))

from aspect import canonical_aspect  # noqa: E402

RENDER = REVERSE / "render.py"
FIXTURE = ROOT / "tests" / "fixtures" / "shot_tags"

results: list[tuple[bool, str]] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    results.append((ok, name))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"｜{detail}" if detail else ""))


def main() -> int:
    cases = [
        ((1080, 1888), "9:16 竖屏"),
        ((1920, 1080), "16:9 横屏"),
        ((1080, 1080), "1:1 方形"),
        ((1440, 1080), "4:3 横版"),
        ((1080, 1440), "3:4 竖版"),
        ((2160, 1080), "16:9 横屏"),
        ((3840, 2160), "16:9 横屏"),
        ((0, 0), ""),
    ]
    bad = [(wh, got, want) for wh, want in cases if (got := canonical_aspect(*wh)) != want]
    check(not bad, "① 像素尺寸归到最近的标准画幅", f"不符 {bad}" if bad else "8/8")

    with tempfile.TemporaryDirectory() as td:
        work = Path(td) / "task"
        out = Path(td) / "out"
        work.mkdir()
        shutil.copy(FIXTURE / "拆解.md", work / "拆解.md")
        src = (work / "拆解.md").read_text(encoding="utf-8")
        # 换成「非标准比例 + 尺寸 + 帧率」的老式规格行
        lines = []
        replaced = False
        for line in src.splitlines():
            if line.startswith("> 规格："):
                lines.append("> 规格：1080×1888 · 135:236 · 15.00 秒 · 30.0 fps · 有音轨")
                replaced = True
            else:
                lines.append(line)
        if not replaced:  # 夹具没有规格行就补一行到头注释后
            lines.insert(1, "> 规格：1080×1888 · 135:236 · 15.00 秒 · 30.0 fps · 有音轨")
        (work / "拆解.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

        cp = subprocess.run(
            [sys.executable, str(RENDER), "--id", str(work),
             "--template", "lumi-4section", "--out-dir", str(out)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
        )
        check(cp.returncode == 0, "② 夹具能渲染成功", f"exit={cp.returncode} {cp.stderr.strip()[:120]}")
        produced = out / "提示词-lumi-4section.md"
        check(produced.is_file(), "③ 产物落盘", str(produced))
        if produced.is_file():
            text = produced.read_text(encoding="utf-8")
            spec = next((ln for ln in text.splitlines() if ln.startswith("**全片规格**")), "")
            check("画幅 9:16 竖屏" in spec, "④ 非标准比例归到 9:16 竖屏", spec or "（没有全片规格行）")
            check("135:236" not in text, "⑤ 135:236 不再出现在提示词里")
            check("尺寸" not in text and "1080×1888" not in text, "⑥ 提示词不写尺寸")
            check("fps" not in spec, "⑦ 全片规格不写帧率", spec)

    passed = sum(1 for ok, _ in results if ok)
    print(f"\n{passed} PASS / {len(results) - passed} FAIL")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
