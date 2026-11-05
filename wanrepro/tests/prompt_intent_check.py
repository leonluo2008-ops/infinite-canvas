#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B-1 prompt intent checks. Run with: py -3 tests/prompt_intent_check.py"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXTRACT = ROOT / "pipeline" / "reverse" / "extract.py"


def load_extract():
    spec = importlib.util.spec_from_file_location("prompt_intent_extract", EXTRACT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法导入 {EXTRACT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    mod = load_extract()
    total = 0
    failed = 0

    def check(name: str, condition: bool) -> None:
        nonlocal total, failed
        total += 1
        if condition:
            print(f"[PASS] {name}")
        else:
            failed += 1
            print(f"[FAIL] {name}")

    base = "固定提示词\n## 一 场景\n正文"
    check("空意图逐字返回 base", mod.build_prompt(base, "") == base)

    intent = "把香水换成我的产品"
    with_intent = mod.build_prompt(base, intent)
    check("带意图包含原文", intent in with_intent)
    check("带意图包含不许加节类约束", "不许加节" in with_intent)

    print(f"{total - failed} PASS / {failed} FAIL")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
