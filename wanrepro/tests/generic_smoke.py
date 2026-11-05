# -*- coding: utf-8 -*-
"""generic_smoke.py — 通用性冒烟测试：证明工具不认识任何具体产品

仍在执行的检查：工具目录（`pipeline/` `wanrepro/` `spec/`）不得出现真产品词。
摘除的检查：原「假项目跑 check_basis」两条（0 ERROR 通用通过 / 改比例后被规则挡下）——
  旧规则库已于 2026-10-29 删除（所有者裁定），重建后另立版本；本检查暂不可用。
  **不报 PASS，也不伪装成 SKIP**。
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]


def main() -> int:
    # 工具目录不含真产品词
    # 用词边界，避免 K-POP 这类误报；b-* 是项目案例产物（gitignored），不算工具层
    pats = [r"\bPOP!?\b", r"香水", r"和果子", r"VANILLE", r"C94E0A"]
    hits = []
    for d in ["pipeline", "wanrepro", "spec"]:
        for f in (ROOT / d).rglob("*"):
            if f.is_file() and f.suffix in (".py", ".json", ".md"):
                t = f.read_text(encoding="utf-8", errors="ignore")
                for p in pats:
                    if re.search(p, t):
                        hits.append(f"{f.relative_to(ROOT)}:{p}")
    print(f"[1] 工具层产品词：{hits or '0 命中'}")
    print("[2] 已摘除：旧规则库已于 2026-10-29 删除（所有者裁定），重建后另立版本；"
          "check_basis 通用性/项目参数两条检查暂不可用")

    verdict = "PASS" if not hits else "FAIL"
    print(f"\n结论：{verdict}（1 无产品硬编码={not hits}｜2 暂不可用）")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
