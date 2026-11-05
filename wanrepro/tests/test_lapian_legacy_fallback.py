#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B5 单测：旧格式拆解的「镜头调度」兜底，不许按序号瞎取。

背景（docs/需求与方案-运镜传递保障-20261106 §B5）：旧拆解的分镜表没有「镜头运动」列，
render_lapian 的兜底原用 `five[idx]` 按分镜序号取五层行——五层只有 2 行
（机位与运镜 / 焦段与景深），镜 2 就拿到了「焦段与景深」，运镜信息当场丢。

三例：
① 旧格式：镜 1 **和** 镜 2 的「镜头调度」都取到「机位与运镜」行的内容，不是焦段行；
② 旧格式但五层没有运镜行：显式标〔旧格式无运镜列〕，不拿别的行充数；
③ 新格式（表里有镜头运动 + 景别列）：走列值，不进兜底。

跑法：py -3 tests/test_lapian_legacy_fallback.py   （无需 pytest；也兼容 pytest 收集）
夹具全部内联建在临时目录，不读 analysis/。
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "pipeline" / "reverse" / "render_lapian.py"

_spec = importlib.util.spec_from_file_location("render_lapian", MODULE_PATH)
render_lapian = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(render_lapian)

MISSING_LINE = "- 镜头调度：〔旧格式无运镜列〕（建议重新拆解补齐分镜表）"

# 旧格式：分镜表没有「镜头运动 / 景别」列（2026-11-06 pop3test 实况）
SEVEN_OLD = """## 七 分镜表
| 镜号 | 起秒-止秒 | 运动主体 | 运动形态 | 速度 | 画面内容 | 光影变化 | 转场方式 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| 1 | 00:00-00:02 | 女性人物 | 转体向前跨步抛掷 | 快 | 人物在暖黄地铁车厢内向镜头扔出手机 | 车厢常规格顶灯 | 组内剪辑 |
| 2 | 00:02-00:04 | 手机 | 空中自旋并悬停 | 慢 | 手机直逼镜头中央 | 背景灯光熄灭变暗 | 动势连接 |
"""

FIVE_WITH_CAMERA = """## 五 镜头
* **机位与运镜**：全片严格保持正面对称中心机位，略微仰角。00:00-00:02固定机位；00:03-00:04微推向悬浮手机。
* **焦段与景深**：标准至中焦段（约35mm-50mm）。景深极浅，背景人物彻底虚化。
"""

FIVE_WITHOUT_CAMERA = """## 五 镜头
* **焦段与景深**：标准至中焦段（约35mm-50mm）。景深极浅，背景人物彻底虚化。
"""

SEVEN_NEW = """## 七 分镜表
| 镜号 | 起秒-止秒 | 运动主体 | 运动形态 | 速度 | 画面内容 | 光影变化 | 转场方式 | 镜头运动 | 景别 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| 1 | 00:00-00:02 | 女性人物 | 转体向前跨步抛掷 | 快 | 人物在暖黄地铁车厢内向镜头扔出手机 | 车厢常规格顶灯 | 组内剪辑 | 固定微震 | 中景 |
| 2 | 00:02-00:04 | 手机 | 空中自旋并悬停 | 慢 | 手机直逼镜头中央 | 背景灯光熄灭变暗 | 动势连接 | 快速向前推进 | 特写 |
"""


def _make_ir(five: str, seven: str):
    """临时目录里拼一份最小拆解 → IR（不读 analysis/）。"""
    tmp = tempfile.TemporaryDirectory()
    d = Path(tmp.name)
    (d / "拆解.md").write_text(f"# 拆解\n\n{five}\n{seven}", encoding="utf-8")
    return tmp, render_lapian.IR(d)


def _lens_line(ir, idx: int) -> str:
    row = ir.shot_rows[idx]
    lines = render_lapian._shot_four_subblocks(ir, row, idx)
    hits = [ln for ln in lines if ln.startswith("- 镜头调度：")]
    assert len(hits) == 1, f"镜头调度行应恰好 1 行，实际 {hits}"
    return hits[0]


def test_old_format_uses_camera_row_not_lens_row():
    tmp, ir = _make_ir(FIVE_WITH_CAMERA, SEVEN_OLD)
    with tmp:
        assert len(ir.shot_rows) == 2, ir.shot_rows
        for idx in (0, 1):                     # 镜 1 + 镜 2（原 bug 就出在镜 2）
            line = _lens_line(ir, idx)
            assert "全片严格保持正面对称中心机位" in line, line
            assert "焦段" not in line, line
            assert "标准至中焦段" not in line, line
            assert "〔缺字段〕" in line, line   # 旧格式照实标缺列，补注五层运镜行
    print("[PASS] ① 旧格式镜 1/镜 2 都取「机位与运镜」行，不串焦段行")


def test_old_format_without_camera_row_is_explicit():
    tmp, ir = _make_ir(FIVE_WITHOUT_CAMERA, SEVEN_OLD)
    with tmp:
        line = _lens_line(ir, 0)
        assert line == MISSING_LINE, line
    print("[PASS] ② 五层无运镜行 → 显式〔旧格式无运镜列〕")


def test_new_format_does_not_fall_back():
    tmp, ir = _make_ir(FIVE_WITH_CAMERA, SEVEN_NEW)
    with tmp:
        first = _lens_line(ir, 0)
        second = _lens_line(ir, 1)
        assert first == "- 镜头调度：固定微震，中景", first
        assert second == "- 镜头调度：快速向前推进，特写", second
        for line in (first, second):
            assert "兜底" not in line and "旧格式" not in line, line
    print("[PASS] ③ 新格式走列值，不进兜底")


def main() -> int:
    cases = (test_old_format_uses_camera_row_not_lens_row,
             test_old_format_without_camera_row_is_explicit,
             test_new_format_does_not_fall_back)
    failed = 0
    for case in cases:
        try:
            case()
        except AssertionError as exc:
            failed += 1
            print(f"[FAIL] {case.__name__}｜{exc}")
    print(f"SUMMARY {len(cases) - failed}/{len(cases)} PASS")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
