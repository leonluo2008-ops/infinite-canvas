#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""guide.py — 提示词编写前的「必读」入口（对应 MCP 工具 repro_guide）

硬性要求（用户 2026-10-28）：
  **写提示词时必须每次现读案例库，不许凭模型记忆。**
所以本工具做三件事：
  1. 输出当前规范（八段式 + 已拍板标准 + 反例）与按任务类型筛出的必读案例全文
  2. 给出版本号与案例 id，要求写进提示词头部两行
  3. 落一份读取凭证 .wanrepro/guides/<stamp>.json（含库版本、案例 id、时间）
     —— 门禁 gate.py 会核对提示词头声明的版本与凭证是否一致，不一致即拒绝提交

用法：
  py -3 pipeline/reverse/guide.py --task render --assets 2 --limit 3
  py -3 pipeline/reverse/guide.py --task render --show c-online-03   # 看全文
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / "library"
STATE = ROOT / ".wanrepro" / "guides"


def load_manifest() -> dict:
    p = LIB / "manifest.json"
    if not p.exists():
        raise SystemExit("旧规则库已于 2026-10-29 删除（所有者裁定），重建后另立版本；本检查/本功能暂不可用")
    return json.loads(p.read_text(encoding="utf-8"))


def load_cases() -> list[dict]:
    out = []
    for f in sorted((LIB / "cases").glob("*.json")):
        out.append(json.loads(f.read_text(encoding="utf-8")))
    return out


def pick(cases: list[dict], limit: int) -> list[dict]:
    rank = {"A": 0, "B": 1, "C": 2}
    cases.sort(key=lambda c: (rank.get(c["evidence_level"], 9), c["id"]))
    return cases[:limit]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", default="render", choices=["render", "compose", "lint", "all"])
    ap.add_argument("--limit", type=int, default=3)
    ap.add_argument("--show", default=None, help="打印某条案例全文")
    ap.add_argument("--assets", type=int, default=None)
    args = ap.parse_args()

    man = load_manifest()
    cases = load_cases()

    if args.show:
        hit = [c for c in cases if c["id"] == args.show]
        if not hit:
            raise SystemExit(f"没有这条案例：{args.show}")
        print(hit[0]["prompt"])
        return 0

    chosen = pick(cases, args.limit)
    lex_p = ROOT / "spec" / "lexicon.json"
    if not lex_p.exists():
        raise SystemExit("旧规则库已于 2026-10-29 删除（所有者裁定），重建后另立版本；本检查/本功能暂不可用")
    lex = json.loads(lex_p.read_text(encoding="utf-8"))
    standards = [s for s in lex.get("standards", []) if s.get("level") == "gate"]

    print(f"# 提示词编写必读 ｜ 案例库版本 {man['version']}")
    print(f"# 构建时间 {man['built_at']}｜案例 {man['counts']['cases']} 条 {man['counts']['by_level']}"
          f"｜反例 {man['counts']['antipatterns']} 条\n")

    print("## 一、结构（七段必填 + 负向可选；官方原文：负向「没有可留空，不必凑数」）")
    print("总体描述 → 素材引用（@图片N） → 分镜（起-止秒） → 台词 → 音效/BGM → 风格情绪 → 一致性")
    print("负向清单：默认不写；仅在①同素材实测出现过该错误 ②正向无法表达 ③不重复正向 ④≤3 条 时才加\n")

    print("## 二、已拍板的硬标准（可阻断）")
    for s in standards:
        print(f"- [{s['id']}] {s['rule']}   ← {s['source']}")
    print()

    print("## 三、反例（照着这些形状写就是废稿）")
    for f in sorted((LIB / "antipatterns").glob("*.json")):
        a = json.loads(f.read_text(encoding="utf-8"))
        print(f"- {a['id']}｜{a['title']}｜症状：{a['symptom']}｜根因：{a['root_cause']}｜规则：{a['rule']}")
    print()

    print(f"## 四、本次必读案例（{len(chosen)} 条，全文见下）")
    for c in chosen:
        print(f"- {c['id']}（证据 {c['evidence_level']}）{c.get('why_good','')}")
    print()

    for c in chosen:
        print(f"===== {c['id']} ｜证据 {c['evidence_level']}｜参数 {json.dumps(c.get('params', {}), ensure_ascii=False)} =====")
        print(c["prompt"].strip()[:2600])
        print()

    # 读取凭证
    STATE.mkdir(parents=True, exist_ok=True)
    stamp_payload = {
        "library_version": man["version"],
        "cases": [c["id"] for c in chosen],
        "task": args.task,
        "assets": args.assets,
        "read_at": datetime.now().isoformat(timespec="seconds"),
    }
    stamp = "g-" + hashlib.sha256(json.dumps(stamp_payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:10]
    (STATE / f"{stamp}.json").write_text(json.dumps({"stamp": stamp, **stamp_payload}, ensure_ascii=False, indent=1),
                                         encoding="utf-8")

    print("## 五、写进提示词头部的两行（门禁会核对）")
    print(f"# library: {man['version']}")
    print(f"# cases: {', '.join(c['id'] for c in chosen)}")
    print(f"\n读取凭证：{stamp}（.wanrepro/guides/{stamp}.json）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
