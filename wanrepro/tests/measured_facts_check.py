"""机器事实必须在产物里看得见：硬切次数、冲突留痕。

2026-11-01 真 bug：探针能测到 `微信视频…484` 在 9.34s 有 1 次硬切，
但拼文档时 `meta` 漏了 `hard_cuts`，规格行永远写「硬切 0 次」——
产物里的机器事实是错的，比不写更糟（下游会以为这真是一部一镜到底的片子）。

跑法：py -3 tests/measured_facts_check.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline" / "reverse"))

import extract  # noqa: E402

BASE_META = {"duration": 13.35, "has_audio": True, "width": 576, "height": 1024,
             "fps": 23.98, "aspect": "9:16 竖屏", "elapsed": 54.1,
             "total_tokens": 6907, "attempts_note": "1"}


def main() -> int:
    fails: list[str] = []

    # ① 有硬切：规格行必须写「硬切 1 次」
    doc = extract.build_document("v.mp4", {**BASE_META, "hard_cuts": [9.343]}, "## 一 场景\n白台。\n")
    if "硬切 1 次" not in doc:
        fails.append(f"有 1 次硬切时规格行没写对：{[l for l in doc.splitlines() if '规格' in l]}")

    # ② 无硬切：写「硬切 0 次」
    doc0 = extract.build_document("v.mp4", {**BASE_META, "hard_cuts": []}, "## 一 场景\n白台。\n")
    if "硬切 0 次" not in doc0:
        fails.append("0 次硬切时规格行没写对")

    # ③ 冲突要留在产物里，不静默
    doc_c = extract.build_document("v.mp4", {**BASE_META, "hard_cuts": []},
                                   "- 一镜到底：否\n", ["机器实测 0 次硬切，拆解却答「一镜到底：否」"])
    if "与机器实测冲突" not in doc_c:
        fails.append("冲突没有写进产物头部")

    # ④ check_facts 的方向性
    m0 = extract.check_facts("- 一镜到底：否\n", [])
    m1 = extract.check_facts("- 一镜到底：否\n", [9.343])
    m2 = extract.check_facts("- 一镜到底：是\n", [9.343])
    if len(m0) != 1:
        fails.append(f"0 硬切 + 答「否」应记 1 条冲突，实得 {len(m0)}")
    if m1:
        fails.append(f"1 硬切 + 答「否」不该记冲突，实得 {m1}")
    if not m2:
        fails.append("1 硬切 + 答「是」应记冲突")

    # ⑤ 探针 → 产物 的接线：meta 必须把 hard_cuts 带上（这条才是抓真 bug 的那条）
    meta = extract.build_meta({"duration": 13.35, "has_audio": True, "hard_cuts": [9.343]}, 1.0, 2)
    if meta.get("hard_cuts") != [9.343]:
        fails.append("build_meta 丢了 hard_cuts——规格行会永远写「硬切 0 次」")
    if "硬切 1 次" not in extract.build_document("v.mp4", meta, "- 一镜到底：否\n"):
        fails.append("接线断了：meta 有硬切但产物规格行没体现")

    if fails:
        for f in fails:
            print(f"[FAIL] {f}")
        print(f"{len(fails)} FAIL")
        return 1
    print("5 PASS / 0 FAIL")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
