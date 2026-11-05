"""资产表判重：清单行号 vs 提示词图号。

背景（2026-10-31 实测）：图号只发给「已备图」的资产，没备图的行图号是 0。
`build_assets` 原来拿图号判重，于是「第 2 行已传图（图号 1）」与「盘上 图片2.png」
被当成两件东西 —— 10 个资产在页面上显示成 15 行。

这个用例守住三件事：
1. 清单 9 行 + 3 张盘上图片，输出必须恰好 9 行（不重复、不丢）。
2. 每行都带 row（清单行号），上传/写回才不会绑错。
3. 盘上有、清单里没有的图仍然要列出来（不能因为判重把真·孤儿图吞掉）。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ui"))
import server  # noqa: E402

SHEET = """# 资产清单 · 判重夹具

| 序号 | 类型 | 要准备什么 | 锁什么 |
|---|---|---|---|
| 图片1 | 产品图 | 清单第 2 行 · 细长圆柱瓶身 | 外形比例 |
| — | 产品图 | 清单第 1 行 · 扁平黑框屏幕 | 边框 |
| — | 人物参考图 | 清单第 3 行 · 二十岁女性 | 发型 |
| — | 服装参考图 | 清单第 4 行 · 黑色背心 | 版型 |
| — | 服装参考图 | 清单第 5 行 · 橙色连衣裙 | 长度 |
| — | 道具参考图 | 清单第 6 行 · 橙色纸盒 | 色彩 |
| — | 场景参考图 | 清单第 7 行 · 地铁车厢 | 结构 |
| — | 场景参考图 | 清单第 8 行 · 纯白影棚 | 干净 |
| — | 场景参考图 | 清单第 9 行 · 多边形通道 | 纵深 |
"""

results: list[tuple[bool, str]] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    results.append((ok, name))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"｜{detail}" if detail else ""))


def make_task(root: Path, name: str, images: list[int], sheet: str = SHEET) -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "资产清单.md").write_text(sheet, encoding="utf-8")
    (d / "待确认清单.md").write_text("# 空\n", encoding="utf-8")
    assets = d / "assets"
    assets.mkdir()
    for no in images:
        (assets / f"图片{no}.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    return d


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        # 清单 9 行；盘上有 图片2/3/4 —— 第 2 行（图号1）已备图，其余是待备图的占位文件
        make_task(root, "mixed", [2, 3, 4])
        old_analysis = server.ANALYSIS
        server.ANALYSIS = root
        try:
            rows = server.build_assets("mixed")
        finally:
            server.ANALYSIS = old_analysis

        nos = [r.get("row") for r in rows]
        check(len(rows) == 9, "① 9 行资产输出 9 行", f"实际 {len(rows)} 行：rows={nos}")
        check(len(set(nos)) == len(nos), "② 清单行号不重复", f"rows={nos}")
        check(all(isinstance(r.get("row"), int) and (r.get("row") or 0) > 0 for r in rows),
              "③ 每行都带清单行号", f"rows={nos}")
        rows_nos = [r.get("row") or 0 for r in rows]
        check(rows_nos == sorted(rows_nos),
              "④ 按清单行号排序", f"rows={nos}")

        # 盘上有、清单里没有的图：仍要列出来
        make_task(root, "orphan", [2])
        (root / "orphan" / "assets" / "图片99.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        old_analysis = server.ANALYSIS
        server.ANALYSIS = root
        try:
            rows2 = server.build_assets("orphan")
        finally:
            server.ANALYSIS = old_analysis
        check(any(r.get("type") == "未在清单内" for r in rows2),
              "⑤ 清单外的孤儿图仍会列出", f"类型={[r.get('type') for r in rows2]}")

    passed = sum(1 for ok, _ in results if ok)
    print(f"\n{passed} PASS / {len(results) - passed} FAIL")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
