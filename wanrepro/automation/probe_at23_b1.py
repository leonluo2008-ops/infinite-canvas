#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AT-23 B1 探针：**切片词折叠成完整原词**（判定集不动、展示集取最大词；写类，必须 --isolated）。

跑法（wanrepro/ 下）：`python automation/probe_at23_b1.py --isolated`，退出码 0 = 全绿。
**不带 `--isolated` 一律拒跑**：本探针会写任务/清单/提示词/自检，只在临时根内跑。

判据（设计 §3.4 / §3.5，一条一条对）：

  [0] 隔离生效：任务根 / 清单根 / 拆解根都在临时根内，渲染只可能打进程内假 bridge。
  [1] 判定集一个不少：`residue_words()` 对「裹亮橙色标签款」仍出 6 个词（core + 5 个 3 字切片），
      selfcheck 的命中集合（展示词 ∪ 全部切片）与「residue_words ∩ 正文」逐词相等 ——
      折叠发生在**报告层**，判定口径零改动（`_check_residue` 仍走 residue_words）。
  [2] 展示集收口：同一「行×句」只出 **1 条** finding，`词` = 完整原词 7 字、`切片` = 5 个切片
      （按它在原词里的起始位置定序，逐字与 residue_words 一致）；`统计.判定词数=6 / 词数=1`。
  [3] 证据格式不变：`证据` 仍是旧格式（`#1 的「…」残留于正文` + `已传图行词表 #1→图1(6词)`），
      `原文 == 文字`（历史探针与排障锚点不破）。
  [4] 白名单语义零变化（E4）：点 1 次「保留这个词」写进 `selfcheck_whitelist` 的是**完整原词**
      → 自检重跑该项 PASS 且证据写明「已按你的裁量保留 1 个词」；撤销 → 同一个 FAIL 原样回来
      （同行号、同词、同 5 个切片）；切片词（「亮橙色」）照旧可写（服务端闭包走 residue_words，
      口径没动）。
  [5] 折叠边界：只有同一 行×句 才折叠（同词不同句各自成条）；同一句里互不包含的两个词
      各留一条、谁也不带切片；多保留词时切片归**包含它的最短保留词**。
  [6] 真实任务只读：auto-0068 / auto-0067 重算后都是 1 词（完整原词）+ 5 切片 + 判定词数 6，
      且真实 projects/analysis 跑前跑后零变化（只调 `_check_all`，不落盘）。
  [7] 临时根跑完删净。
  [8] **读取层折叠兜底**（返工）：折叠前落盘的旧格式 自检.json（同句 6 词并存、统计.词数=6、
      无 判定词数）经 `selfcheck.load_selfcheck` 读出 → 1 条完整原词 + 5 切片 + 判定词数 6 +
      「还留着 1 个」；处理/清单行/原句/证据一字不改、自检.json 字节不变（不落盘、不重跑自检）；
      已折叠载荷幂等直通（不重复折叠、不添第二次痕）；无包含关系的旧载荷原样透出；坏文件仍 → None；
      hub_api 三处「自检」组装全走 load_selfcheck（后端唯一出口）；真实存量 auto-0067/0068
      读路径即收口且 SHA256 前后不变。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]                       # wanrepro/

if "--isolated" not in sys.argv:
    print("[probe-at23-b1] 拒跑：本探针会写任务/清单/提示词/自检，必须带 --isolated。\n"
          "  例：python automation/probe_at23_b1.py --isolated")
    raise SystemExit(2)

REAL_PROJECTS = ROOT / "automation" / "projects"
REAL_ANALYSIS = ROOT / "analysis" / "reverse"


def snapshot(root: Path) -> set[str]:
    try:
        return {p.name for p in root.iterdir()}
    except OSError:
        return set()


BEFORE = {"projects": snapshot(REAL_PROJECTS), "analysis": snapshot(REAL_ANALYSIS)}

TMP_ROOT = Path(tempfile.mkdtemp(prefix="probe-at23-b1-"))
TMP_PROJECTS = TMP_ROOT / "projects"
TMP_ANALYSIS = TMP_ROOT / "analysis"
TMP_PROJECTS.mkdir(parents=True, exist_ok=True)
TMP_ANALYSIS.mkdir(parents=True, exist_ok=True)
os.environ["WANREPRO_AUTO_DIR"] = str(TMP_PROJECTS)
os.environ["WANREPRO_ANALYSIS_ROOT"] = str(TMP_ANALYSIS)

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ui"))

# 假 bridge 必须在 `automation.config` 被 import 之前定下基址（config.BRIDGE_BASE 是 import 期常量）。
from automation.probe_guard import StubBridge  # noqa: E402


def _never_render(path: str) -> str:
    """本批只动展示层/白名单，一步都不该渲染：真被调到就是缺陷。"""
    raise AssertionError(f"B1 折叠流程里不该渲染，却收到渲染请求：{path}")


STUB = StubBridge(render_fn=_never_render)
os.environ["WANREPRO_BRIDGE"] = STUB.base

from automation import cards, config, frames, replace, selfcheck  # noqa: E402
import hub_api  # noqa: E402

cards.ANALYSIS = TMP_ANALYSIS
frames.ANALYSIS = TMP_ANALYSIS

LIST_NAME = replace.LIST_NAME
RESULTS: list[tuple[str, bool]] = []
TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6300010000050001a5f645400000000049454e44ae426082")

BRIDGE = "probe-b1-one"
TASK = "auto-9951"
WORD = "裹亮橙色标签款"                     # 真实任务 auto-0068 的原物核心词（7 字）
SLICES = ["裹亮橙", "亮橙色", "橙色标", "色标签", "标签款"]   # 5 个 3 字滑窗（按起始位置）
PROMPT = "# 提示词\n\n全程使用参考图中的@图片1 裹亮橙色标签款。0-3秒，中景，人物与@图片1，微幅晃动。\n"

CK = """# 待确认清单 · {bridge}

| # | 类别 | 模型看到什么 | 命名 | 资产图 | 锁什么 | 建议 | 用我的描述 |
|---|------|-------------|------|--------|--------|------|------------|
| 1 | 主体 | 裹亮橙色标签款 |  | {i1} | 外形比例 | 必须换 |  |
"""

GATE_PASS = {
    "状态": "通过", "轮次": 1, "上限": 2,
    "结果": {"verdict": "PASS", "checks": [{"id": "D1", "verdict": "PASS"}, {"id": "D2", "verdict": "PASS"},
                                         {"id": "D3", "verdict": "PASS"}, {"id": "D4", "verdict": "PASS"}],
             "缺项": [], "告警": []},
}


def check(name: str, cond, detail: str = "") -> bool:
    ok = bool(cond)
    RESULTS.append((name, ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail and not ok else ""))
    return ok


def iso(delta: int = 0) -> str:
    return (datetime.now(timezone.utc).astimezone() + timedelta(seconds=delta)).isoformat(timespec="seconds")


def under_tmp(path) -> bool:
    try:
        return Path(path).resolve().is_relative_to(TMP_ROOT.resolve())
    except (OSError, AttributeError):
        return False


def ref(local_id: str, name: str) -> str:
    return f"local:{local_id}/asset-uploads/{name}"


def seed() -> Path:
    bd = TMP_ANALYSIS / BRIDGE
    bd.mkdir(parents=True, exist_ok=True)
    (bd / LIST_NAME).write_text(CK.format(bridge=BRIDGE, i1=ref(TASK, "row1.png")), encoding="utf-8")
    (bd / "拆解.md").write_text((ROOT / "tests" / "fixtures" / "at11-m1" / "拆解.md").read_text(
        encoding="utf-8-sig"), encoding="utf-8")
    (bd / "门禁.json").write_text(json.dumps(GATE_PASS, ensure_ascii=False, indent=2), encoding="utf-8")
    d = TMP_PROJECTS / TASK
    d.mkdir(parents=True, exist_ok=True)
    task = {"local_id": TASK, "state": "awaiting_approval", "bridge_id": BRIDGE,
            "created_at": iso(-600), "intent": "AT-23 B1 探针夹具（隔离根内）",
            "history": [{"at": iso(-600), "from": "rendering", "to": "awaiting_approval", "note": "夹具"}],
            "assets_confirmed": [{"no": 1, "url": ref(TASK, "row1.png")}],
            "audio_mode": "保留原声"}
    (d / "任务.json").write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
    (d / "提示词.md").write_text(PROMPT, encoding="utf-8")
    up = d / "asset-uploads"
    up.mkdir(parents=True, exist_ok=True)
    (up / "row1.png").write_bytes(TINY_PNG)
    return d


def residue_of(items: list[dict]) -> dict:
    return next((item for item in items if item.get("项") == "绑图行残留"), {})


def findings_of(item: dict) -> list[dict]:
    return list((item.get("说明") or {}).get("发现") or [])


def add(local_id: str, rows: int, word: str) -> dict:
    return hub_api.set_selfcheck_whitelist(local_id, [rows], word=word)


def drop(local_id: str, rows: int, word: str) -> dict:
    return hub_api.set_selfcheck_whitelist(local_id, [rows], word=word, remove=True)


# ---------------------------------------------------------------- [0] 隔离

def isolation_phase() -> bool:
    print("[0] 隔离生效：任务/清单/拆解根都在临时根内，渲染只可能打进程内假 bridge")
    ok = True
    ok &= check("清单根在临时根内", under_tmp(replace.analysis_root()), str(replace.analysis_root()))
    ok &= check("hub 链任务根在临时根内", under_tmp(hub_api.TASKS_DIR), str(hub_api.TASKS_DIR))
    ok &= check("配置任务根在临时根内", under_tmp(config.PROJECTS_DIR), str(config.PROJECTS_DIR))
    ok &= check("拆解根（cards.ANALYSIS / frames.ANALYSIS）在临时根内",
                under_tmp(cards.ANALYSIS) and under_tmp(frames.ANALYSIS),
                f"{cards.ANALYSIS} / {frames.ANALYSIS}")
    ok &= check("bridge 基址是本进程内的假 bridge（不是 8776/8779 真桥）",
                config.BRIDGE_BASE == STUB.base and str(STUB.port) not in ("8776", "8779"),
                f"{config.BRIDGE_BASE} vs stub {STUB.base}")
    return ok


# ---------------------------------------------------------------- [1] 判定集不动

def judgment_phase(d: Path) -> bool:
    print("[1] 判定集一个不少：residue_words 仍 6 个词，折叠只发生在报告层")
    ok = True
    render = selfcheck._load_render()
    ents = render.parse_checklist(replace.checklist_path(BRIDGE))
    render.assign_numbers(ents, replace.load_groups(BRIDGE), replace.load_row_decisions(BRIDGE))
    ent = next(e for e in ents if int(e.row_no) == 1)
    words = render.residue_words(ent)
    ok &= check("[1] residue_words(「裹亮橙色标签款」) = 6 个词（core + 5 个 3 字切片）",
                len(words) == 6 and WORD in words and set(SLICES) <= set(words),
                json.dumps(words, ensure_ascii=False))
    ok &= check("[1] ≥3 字口径不变（2 字组不进表）", all(len(w) >= 3 for w in words),
                json.dumps([w for w in words if len(w) < 3], ensure_ascii=False))
    item = residue_of(selfcheck._check_all(d))
    found = findings_of(item)
    covered = {str(f.get("词") or "") for f in found} | {str(s) for f in found for s in (f.get("切片") or [])}
    hit_words = {w for w in words if w in PROMPT}
    ok &= check("[1] 展示词 ∪ 全部切片 == residue_words ∩ 正文（判定命中一个不少、一个不多）",
                covered == hit_words and len(hit_words) == 6,
                f"covered={sorted(covered)} hit={sorted(hit_words)}")
    ok &= check("[1] 判定仍走 residue_words（单一取词点不另写一份）",
                "residue_words(" in __import__("inspect").getsource(selfcheck._check_residue))
    return ok


# ---------------------------------------------------------------- [2] 展示集收口

def display_phase(d: Path) -> bool:
    print("[2] 展示集收口：同一 行×句 只出 1 条完整原词，5 个切片进 `切片`（只进折叠区）")
    ok = True
    item = residue_of(selfcheck._check_all(d))
    found = findings_of(item)
    stats = (item.get("说明") or {}).get("统计") or {}
    ok &= check("[2] 同一句只出 1 条 finding（6 条 → 1 条）", len(found) == 1,
                json.dumps([f.get("词") for f in found], ensure_ascii=False))
    card = found[0] if found else {}
    ok &= check("[2] 展示词 = 完整原词「裹亮橙色标签款」（不是任何切片）",
                card.get("词") == WORD and len(str(card.get("词"))) == 7, str(card.get("词")))
    ok &= check("[2] `切片` 逐字与 residue_words 的切片集合一致（5 个，按原词内起始位置定序）",
                list(card.get("切片") or []) == SLICES,
                json.dumps(card.get("切片"), ensure_ascii=False))
    ok &= check("[2] 统计：判定词数=6（判定集留痕）/ 词数=1（展示集）",
                stats.get("判定词数") == 6 and stats.get("词数") == 1 and stats.get("句数") == 1,
                json.dumps(stats, ensure_ascii=False))
    ok &= check("[2] 结论人话按展示集报数（1 个词，不再说 6 个）",
                "1 个" in str((item.get("说明") or {}).get("结论人话") or "")
                and "6 个" not in str((item.get("说明") or {}).get("结论人话") or ""),
                str((item.get("说明") or {}).get("结论人话")))
    return ok


# ---------------------------------------------------------------- [3] 证据格式不变

def evidence_phase(d: Path) -> bool:
    print("[3] 证据格式不变：旧字段一字不改，切片只作新增结构化槽位")
    ok = True
    item = residue_of(selfcheck._check_all(d))
    found = findings_of(item)
    evidence = str(item.get("证据") or "")
    ok &= check("[3] `证据` 仍是旧格式（#1 的「…」残留于正文 + 已传图行词表）",
                "#1 的「" in evidence and "已传图行词表" in evidence and "#1→图1(6词)" in evidence,
                evidence[:200])
    ok &= check("[3] `原文` 与 `文字` 同值（历史探针/排障锚点不破）",
                all(f.get("原文") == f.get("文字") for f in found))
    ok &= check("[3] 旧字段 `文字` 里只提完整原词（不列切片）",
                all(str(f.get("文字")).count("「") == 1 and WORD in str(f.get("文字")) for f in found),
                json.dumps([f.get("文字") for f in found], ensure_ascii=False))
    return ok


# ---------------------------------------------------------------- [4] 白名单语义零变化

def whitelist_phase(d: Path) -> bool:
    print("[4] 白名单语义零变化：写完整原词 → 重跑 PASS；撤销 → 同一个 FAIL 原样回来")
    ok = True
    base = residue_of(selfcheck.run_selfcheck(d))
    base_found = findings_of(base)
    ok &= check("[4] 基线 FAIL、命中词=完整原词", base.get("结论") == "FAIL"
                and [f.get("词") for f in base_found] == [WORD],
                f"{base.get('结论')} {[f.get('词') for f in base_found]}")

    echo = add(TASK, 1, WORD)
    item1 = residue_of(echo.get("自检") or [])
    entry = ((json.loads((d / "任务.json").read_text(encoding="utf-8")).get(selfcheck.WHITELIST_FIELD) or {})
             .get("条目") or [{}])[0]
    ok &= check("[4] 点 1 次「保留这个词」→ 落 `行1 × 裹亮橙色标签款`（词键=完整原词）",
                entry.get("行") == 1 and entry.get("词") == WORD and entry.get("词键") == WORD,
                json.dumps(entry, ensure_ascii=False))
    ok &= check("[4] 自检重跑：完整原词已摘出判定（finding 里不再出现 7 字原词）",
                all(f.get("词") != WORD for f in findings_of(item1)),
                json.dumps([f.get("词") for f in findings_of(item1)], ensure_ascii=False))
    ok &= check("[4] 证据写明「已按你的裁量保留 1 个词」（豁免是可见状态，不是隐形通过）",
                "已按你的裁量保留 1 个词" in str(item1.get("证据")),
                str(item1.get("证据"))[:200])
    ledger = item1.get("豁免") or []
    ok &= check("[4] 豁免台账 1 条且 生效",
                len(ledger) == 1 and ledger[0].get("状态") == "生效",
                json.dumps(ledger, ensure_ascii=False))
    # 设计 §3.5-4「该项 PASS **或按真实结论显示**」：只豁免完整原词时，另外 5 个切片按 E4 的
    # 行×词键粒度仍在判定表里、正文里也确实还留着它们（判定口径一个词没少）——所以这一项仍
    # 如实 FAIL。这不是缺陷：判定的词粒度没被折叠改过，探针就照实锁「仍 FAIL 且不提原词」。
    ok &= check("[4] 结论按真实结论显示（切片仍在判定表 → 仍 FAIL，不静默判过）",
                item1.get("结论") == "FAIL",
                f"{item1.get('结论')} {[f.get('词') for f in findings_of(item1)][:3]}")

    back = drop(TASK, 1, WORD)
    item2 = residue_of(back.get("自检") or [])
    ok &= check("[4] 撤销 → 同一个 FAIL 原样回来（同行号、同词、同 5 个切片）",
                item2.get("结论") == "FAIL"
                and [f.get("词") for f in findings_of(item2)] == [WORD]
                and list(findings_of(item2)[0].get("切片") or []) == SLICES,
                json.dumps(findings_of(item2), ensure_ascii=False)[:300])

    # 切片的词键照旧在闭包里（服务端校验走 residue_words，折叠没动可写集合）——E4 语义零变化。
    closure = selfcheck.row_word_closure(BRIDGE).get(1) or {}
    keys = set((closure.get("词") or {}))
    ok &= check("[4] 闭包词集仍含完整原词 + 5 个切片（服务端能写的词一个没少）",
                {selfcheck.word_key(w) for w in [WORD, *SLICES]} <= keys and len(keys) == 6,
                json.dumps(sorted(keys), ensure_ascii=False))
    echo2 = add(TASK, 1, SLICES[1])
    ok &= check("[4] 切片词（亮橙色）照旧可写 → 新增 1 条、无 400（闭包口径未被折叠动过）",
                echo2.get("新增") == 1, json.dumps(echo2, ensure_ascii=False)[:200])
    drop(TASK, 1, SLICES[1])
    return ok


# ---------------------------------------------------------------- [5] 折叠边界

def boundary_phase() -> bool:
    print("[5] 折叠边界：只有同一 行×句 才折叠；同句互不包含各自成条；切片归最短保留词")
    ok = True

    def hit(row: int, order: int, word: str) -> dict:
        return {"行号": row, "句序": order, "词": word, "文字": f"#{row} 的「{word}」"}

    same = selfcheck._fold_contained_words([hit(1, 1, WORD), hit(1, 1, "亮橙色")])
    ok &= check("[5] 同一 行×句 的包含关系 → 折成 1 条（保留 7 字原词，3 字切片进 `切片`）",
                len(same) == 1 and same[0]["词"] == WORD and same[0].get("切片") == ["亮橙色"],
                json.dumps(same, ensure_ascii=False))

    other = selfcheck._fold_contained_words([hit(1, 1, WORD), hit(1, 2, "亮橙色")])
    ok &= check("[5] 同词不同句 → 各自成条，不跨句折叠（句是折叠的作用域）",
                len(other) == 2 and all("切片" not in entry for entry in other),
                json.dumps(other, ensure_ascii=False))

    disjoint = selfcheck._fold_contained_words([hit(1, 1, WORD), hit(1, 1, "香水喷雾瓶")])
    ok &= check("[5] 同句互不包含的两个原物词 → 各留一条、谁也不带 `切片`",
                len(disjoint) == 2 and all("切片" not in entry for entry in disjoint),
                json.dumps(disjoint, ensure_ascii=False))

    nested = selfcheck._fold_contained_words(
        [hit(1, 1, "裹亮橙色标签款"), hit(1, 1, "亮橙色标签款特写"), hit(1, 1, "亮橙色")])
    ok &= check("[5] 多保留词时切片归**包含它的最短保留词**（亮橙色 → 7 字那条，不跑到 8 字那条）",
                len(nested) == 2
                and next(e for e in nested if e["词"] == WORD).get("切片") == ["亮橙色"]
                and "切片" not in next(e for e in nested if e["词"] == "亮橙色标签款特写"),
                json.dumps(nested, ensure_ascii=False))
    return ok


# ---------------------------------------------------------------- [6] 真实任务（只读）
#
# 真实任务 auto-0068 / auto-0067 是**私有清单副本**任务（AT-21 B0），清单在任务目录里、按
# `config.PROJECTS_DIR` 解析——本进程已把任务根指到临时根，在这里读真任务必然 ReplaceError。
# 所以真机那一条（验收 1/2/5）交给 ego 探针 `web/scripts/ego-at23-a1.mjs` [E] 段（打真实 API +
# 真 DOM），本探针只负责隔离根内的判定口径与白名单语义。


# ---------------------------------------------------------------- [8] 读取层折叠兜底
#
# 前卡（t_b27d8d63）验收 FAIL 的根因：折叠只挂在「新跑自检」的产出路径，**存量任务**的
# 自检.json 仍是折叠前落盘（同句 6 词并存），GET /api/hub/task 原样透出 → 真机仍列 6 词。
# 重跑存量任务自检是禁止动作，所以兜底落在读取层唯一出口 `selfcheck.load_selfcheck`。


def legacy_payload(words: list[str]) -> list[dict]:
    """折叠前落盘的 自检.json（= auto-0067/0068 的真实形态：同句多词并存、统计.词数=命中数）。"""
    findings = [{
        "文字": f"#1 的「{word}」残留于正文: …参考图中的@图片1 {WORD}。0-3秒…",
        "行号": 1, "图号": 1, "行称呼": "本品主体", "类别": "主体",
        "图称呼": "你传的产品图", "图引用": ref(TASK, "row1.png"), "词": word, "句序": 1,
        "上下文": f"全程使用参考图中的@图片1 {WORD}。",
        "原文": f"#1 的「{word}」残留于正文: …参考图中的@图片1 {WORD}。0-3秒…",
        "可修": {"类型": "改描述"},
    } for word in words]
    return [
        {
            "项": "绑图行残留", "结论": "FAIL", "项显示名": "提示词里的老词",
            "证据": "；".join(f["文字"] for f in findings[:5]) + ("…" if len(findings) > 5 else "")
                    + "｜已传图行词表 1 行（#1→图1(6词)），正文 448 字",
            "说明": {
                "结论人话": f"提示词里还留着 {len(findings)} 个要换掉的原物词，都在第 1 行「本品主体」里",
                "处理": "在下面这条发现上点「改这一行的描述」，写完点「保存并重渲染」——提示词按新描述重出",
                "发现": findings,
                "统计": {"词表行数": 1, "正文字数": 448, "行→图号": {"1": 1},
                         "词数": len(findings), "句数": 1},
                "清单行": {"1": WORD},
                "原句": {"句序": 1, "段": f"全程使用参考图中的@图片1 {WORD}。", "行号": 1},
            },
        },
        {"项": "次序对账", "结论": "PASS", "项显示名": "图号与提交次序", "证据": "图号与提交次序一致"},
    ]


def read_layer_phase() -> bool:
    print("[8] 读取层折叠兜底：旧格式 6 词载荷 → 1 个完整原词（存量任务不重跑自检也收口）")
    ok = True
    d = TMP_PROJECTS / "auto-9952"
    d.mkdir(parents=True, exist_ok=True)
    path = d / selfcheck.SELFCHECK_NAME
    legacy = legacy_payload([WORD, "亮橙色", "橙色标", "裹亮橙", "标签款", "色标签"])
    path.write_text(json.dumps(legacy, ensure_ascii=False, indent=2), encoding="utf-8")
    raw_before = path.read_bytes()

    items = selfcheck.load_selfcheck(d) or []
    item = residue_of(items)
    found = findings_of(item)
    say = item.get("说明") or {}
    stats = say.get("统计") or {}

    ok &= check("[8] 数据层：旧格式 6 词并存 → 读出 1 条 finding，词=7 字完整原词",
                len(found) == 1 and found[0].get("词") == WORD,
                json.dumps([f.get("词") for f in found], ensure_ascii=False))
    ok &= check("[8] 切片 5 个逐字一致（按原词内起始位置定序）",
                bool(found) and list(found[0].get("切片") or []) == SLICES,
                json.dumps((found[0].get("切片") if found else None), ensure_ascii=False))
    ok &= check("[8] 统计留痕：判定词数=6 / 展示 词数=1 / 句数=1",
                stats.get("判定词数") == 6 and stats.get("词数") == 1 and stats.get("句数") == 1,
                json.dumps(stats, ensure_ascii=False))
    ok &= check("[8] 折叠来源如实标注（读取层兜底，不是重跑自检）",
                "读取层" in str(stats.get("折叠来源")), json.dumps(stats.get("折叠来源"), ensure_ascii=False))
    ok &= check("[8] 判定侧统计原样（词表行数/正文字数/行→图号 一个不改）",
                stats.get("词表行数") == 1 and stats.get("正文字数") == 448
                and stats.get("行→图号") == {"1": 1}, json.dumps(stats, ensure_ascii=False))
    ok &= check("[8] 结论人话按展示集改口（「还留着 1 个」；不再嘴上说 6 个）",
                "还留着 1 个要换掉的原物词" in str(say.get("结论人话")), str(say.get("结论人话")))
    ok &= check("[8] 处理/清单行/原句 一字不改（判定侧事实不重写）",
                say.get("处理") == legacy[0]["说明"]["处理"]
                and say.get("清单行") == legacy[0]["说明"]["清单行"]
                and say.get("原句") == legacy[0]["说明"]["原句"])
    ok &= check("[8] 证据一字不改（检查器原始输出只进折叠区，不当判定改写）",
                item.get("证据") == legacy[0]["证据"], str(item.get("证据"))[:160])
    ok &= check("[8] 只读：自检.json 字节不变（折叠只在读路径，不落盘）",
                path.read_bytes() == raw_before)
    ok &= check("[8] 非残留项原样透出（次序对账 PASS 一字不动）",
                len(items) == 2 and items[1] == legacy[1])

    # 幂等：把折叠后的载荷（= 新格式落盘）再读一遍，必须逐字相同。
    path.write_text(json.dumps([item, legacy[1]], ensure_ascii=False, indent=2), encoding="utf-8")
    again = residue_of(selfcheck.load_selfcheck(d) or [])
    ok &= check("[8] 幂等：已折叠载荷再读逐字相同（不重复折叠、不添第二次痕）", again == item)
    ok &= check("[8] 幂等：判定词数仍 6、切片仍 5 个、折叠来源不被改写",
                ((again.get("说明") or {}).get("统计") or {}).get("判定词数") == 6
                and list(findings_of(again)[0].get("切片") or []) == SLICES
                and ((again.get("说明") or {}).get("统计") or {}).get("折叠来源")
                == stats.get("折叠来源"))

    # 同句互不包含的两个词：没有可折叠的，原样透出（不给干净数据添痕）。
    plain = TMP_PROJECTS / "auto-9953"
    plain.mkdir(parents=True, exist_ok=True)
    plain_path = plain / selfcheck.SELFCHECK_NAME
    plain_items = legacy_payload([WORD, "香水喷雾瓶"])
    plain_path.write_text(json.dumps(plain_items, ensure_ascii=False, indent=2), encoding="utf-8")
    read_plain = selfcheck.load_selfcheck(plain) or []
    ok &= check("[8] 无包含关系的老载荷 → 原样透出（不落 判定词数、不改任何字段）",
                residue_of(read_plain) == plain_items[0],
                json.dumps(((residue_of(read_plain).get("说明") or {}).get("统计")), ensure_ascii=False))

    # 坏文件/空文件语义不变（前端「未检查」）。
    bad = TMP_PROJECTS / "auto-9954"
    bad.mkdir(parents=True, exist_ok=True)
    (bad / selfcheck.SELFCHECK_NAME).write_text("{坏 json", encoding="utf-8")
    ok &= check("[8] 坏 自检.json 语义不变 → None（不因兜底把坏文件当空自检）",
                selfcheck.load_selfcheck(bad) is None)

    # 结构性守卫：hub_api 三处「自检」组装全走 load_selfcheck，别的路径不许直接读 自检.json。
    src = (ROOT / "ui" / "hub_api.py").read_text(encoding="utf-8")
    ok &= check("[8] 后端唯一出口：hub_api 的 3 处「自检」组装全走 selfcheck.load_selfcheck",
                src.count('"自检": selfcheck.load_selfcheck(') == 3
                and "SELFCHECK_NAME" not in src,
                f"hit={src.count(chr(34) + '自检' + chr(34) + ': selfcheck.load_selfcheck(')}"
                f" direct={src.count('SELFCHECK_NAME')}")

    # 真实存量任务只读：读路径也收口，且 自检.json 字节零变化。
    for real_id in ("auto-0067", "auto-0068"):
        real_path = REAL_PROJECTS / real_id / selfcheck.SELFCHECK_NAME
        if not real_path.is_file():
            continue
        before = hashlib.sha256(real_path.read_bytes()).hexdigest()
        real_found = findings_of(residue_of(selfcheck.load_selfcheck(REAL_PROJECTS / real_id) or []))
        after = hashlib.sha256(real_path.read_bytes()).hexdigest()
        ok &= check(f"[8] 真实存量 {real_id}：读路径 1 词完整原词 + 5 切片 + 判定词数 6",
                    len(real_found) == 1 and real_found[0].get("词") == WORD
                    and list(real_found[0].get("切片") or []) == SLICES
                    and ((next((i for i in (selfcheck.load_selfcheck(REAL_PROJECTS / real_id) or [])
                                if i.get("项") == "绑图行残留"), {}).get("说明") or {})
                         .get("统计") or {}).get("判定词数") == 6,
                    json.dumps([f.get("词") for f in real_found], ensure_ascii=False))
        ok &= check(f"[8] 真实存量 {real_id}：自检.json SHA256 前后不变（只读，不重跑自检）",
                    before == after)
    return ok


def main() -> int:
    print(f"[probe-at23-b1] 隔离根 {TMP_ROOT}")
    print(f"[probe-at23-b1] 假 bridge {STUB.base}")
    ok = True
    STUB.__enter__()
    d = seed()
    try:
        ok &= isolation_phase()
        ok &= judgment_phase(d)
        ok &= display_phase(d)
        ok &= evidence_phase(d)
        ok &= whitelist_phase(d)
        ok &= boundary_phase()
        ok &= read_layer_phase()
    finally:
        STUB.__exit__(None, None, None)
        shutil.rmtree(TMP_ROOT, ignore_errors=True)

    print("[7] 真实目录只读 + 临时根删净")
    after = {"projects": snapshot(REAL_PROJECTS), "analysis": snapshot(REAL_ANALYSIS)}
    for name in ("projects", "analysis"):
        lost = sorted(BEFORE[name] - after[name])
        ok &= check(f"[7] 真实 {name} 没有条目消失（不写真实目录）", not lost, f"丢了 {lost}")
    ok &= check("[7] 临时根已删净", not TMP_ROOT.exists(), str(TMP_ROOT))

    total = len(RESULTS)
    passed = sum(1 for _n, flag in RESULTS if flag)
    print("")
    print(f"[probe-at23-b1] {passed}/{total} PASS")
    if not ok:
        print("FAIL: " + " | ".join(name for name, flag in RESULTS if not flag))
        return 1
    print("全绿")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
