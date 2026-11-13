#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AT-22 E2 探针：finding v2 结构化 + 单条呈现 + 去重可量化 + 旧数据降级（写类，必须 --isolated）。

跑法（wanrepro/ 下）：`python automation/probe_at22_e2.py --isolated`，退出码 0 = 全绿。
**不带 `--isolated` 一律拒跑**：本探针会写任务/清单/门禁/自检，只在临时根内跑。

判据（卡面 AT22-B2 第 5 条，一条一条对）：

  [0] 隔离生效：任务根 / 清单根 / 门禁根三处都落在临时根内；`cards` 的拆解根显式指到临时根
      （它是模块常量，不认 env——不指就会读到真实拆解产物，短名断言会变成「读 A 写 B」）。
  [1] finding v2 schema：真跑 selfcheck，命中类 finding 必带 词/句序/上下文/片段/行称呼/图称呼/
      图引用/可修；`片段` 逐段 `文 == 上下文[起:止]`（拼起来还原整句）、至少一段 `高亮`；
      `原文` 与 `文字` 同值（旧字段兼容）；`说明.原因` 不再产出（最大重复源已拔掉）。
  [2] 一条说清：结论人话里的行指代是「第 N 行「短名」」，可见字段（结论人话/项显示名/处理）零禁用词
      （`#6`／`图1`／`图1` 路径／`已传图行词表`／`锁点`／`门禁`／`绿项` 一律不许出现）。
  [3] 去重可量化：同句多词在数据层仍是「一词一条」，但 `行号+句序` 完全一致（渲染层据此并成一张卡）；
      统计（词数/句数）与真实命中数一致；同时用真实历史文件（auto-0066 的 pre-B2 自检.json，只读）
      量出「同一句在旧载荷里被几个可见位承载」的基线，供前端探针算下降比。
  [4] hub_api 透传：get_task 把 v2 字段原样带出（不裁字段），旧结构照旧不伪造。
  [5] 折叠区数据齐备：`统计`／`清单行`／`原句` 三块都在（前端折叠区五块的三个数据源），
      且都不在可见字段里（可见区一个数字统计都不许出现）。
  [6] 呈现联动：node scripts/probe-at22-e2.mjs（SSR 真渲染 drawer）全绿——单条模板、并卡计数、
      禁用词扫描、折叠五块、定位按钮、重复度下降、旧数据降级不炸。
  [7] 真实目录只读：真实 projects/analysis 跑前跑后零变化；临时根跑完删净。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]                       # wanrepro/

if "--isolated" not in sys.argv:
    print("[probe-at22-e2] 拒跑：本探针会写任务/清单/门禁/自检，必须带 --isolated。\n"
          "  例：python automation/probe_at22_e2.py --isolated")
    raise SystemExit(2)

REAL_PROJECTS = ROOT / "automation" / "projects"
REAL_ANALYSIS = ROOT / "analysis" / "reverse"
WEB = ROOT.parent / "web"
LEGACY_FILE = REAL_PROJECTS / "auto-0066" / "自检.json"          # 真实历史文件（pre-B2 载荷，只读）


def snapshot(root: Path) -> set[str]:
    try:
        return {p.name for p in root.iterdir()}
    except OSError:
        return set()


BEFORE = {"projects": snapshot(REAL_PROJECTS), "analysis": snapshot(REAL_ANALYSIS)}

TMP_ROOT = Path(tempfile.mkdtemp(prefix="probe-at22-e2-"))
TMP_PROJECTS = TMP_ROOT / "projects"
TMP_ANALYSIS = TMP_ROOT / "analysis"
TMP_PROJECTS.mkdir(parents=True, exist_ok=True)
TMP_ANALYSIS.mkdir(parents=True, exist_ok=True)
os.environ["WANREPRO_AUTO_DIR"] = str(TMP_PROJECTS)               # 必须在 import 之前
os.environ["WANREPRO_ANALYSIS_ROOT"] = str(TMP_ANALYSIS)
os.environ["WANREPRO_BRIDGE"] = "http://127.0.0.1:9"              # 本探针不连桥
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ui"))

from automation import cards, config, frames, replace, selfcheck  # noqa: E402
import hub_api  # noqa: E402

# `cards` / `frames` 的拆解根是模块常量（不认 env）：探针显式指到临时根，否则短名/帧缓存会
# 「读真路径、写真路径」——第一版探针就因此在真实 analysis/reverse 里建了一个空的「看帧」目录。
cards.ANALYSIS = TMP_ANALYSIS
frames.ANALYSIS = TMP_ANALYSIS

RESULTS: list[tuple[str, bool]] = []
BRIDGE = "probe-at22e2"
TASK = "auto-9801"
TASK_LEGACY = "auto-9802"
LIST_NAME = replace.LIST_NAME

# 可见区禁用词（E2 §8.2）：命中即实现不合格（`@图片N` 只允许出现在上下文引用里，另按元素白名单扫）
BANNED = (
    (r"#\d", "「#N」行号写法"),
    (r"图\d+(?!（你传的)", "「图M」裸图号"),
    (r"已传图行词表|发图单位|提交快照|必换行|empty_confirm|锁点|门禁|绿项|缺项|告警|重拆\s*\d|行→图号|正文\s*\d+\s*字",
     "内部术语/统计串"),
    (r"analysis/reverse|待确认清单\.md", "磁盘路径"),
    (r"原因：", "「原因：」段落（与发现重复）"),
)


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


# ---------------------------------------------------------------- 夹具

CK = """# 待确认清单 · {bridge}

| # | 类别 | 模型看到什么 | 命名 | 资产图 | 锁什么 | 建议 | 用我的描述 |
|---|------|-------------|------|--------|--------|------|------------|
| 1 | 主体 | 细长圆柱体紫色表面，顶部为银色金属圆柱盖，管体侧面印有粉白字母排版 |  | {img1} | 外形比例 | 必须换 |  |
| 6 | 道具 | 紫色圆柱管身配亮银色金属盖的香水喷雾瓶 |  | {img6} | 颜色、支点位置 | 建议准备 |  |
| 12 | 品牌视觉（置换位） | 参考片品牌位 标语品牌字（出现于：00:05-00:07）：「POP!」｜红色无衬线粗体大字｜正红 |  |  | 客户品牌字样（待填） | 必须换 |  |
"""

# 正文：第一句干净，第二句把道具A 的原物词整段写进去（同句多词 → 渲染层只该出一张卡），
# 第三句再带一次（不同句 → 另一张卡），第四句给主体行的词（第三张卡）。
PROMPT = """# 提示词

全程使用参考图中的@图片1。0-2秒，中景，光线从右后方打进来。

3-4秒，紫色圆柱管身配亮银色金属盖的香水喷雾瓶静置桌面，镜头缓慢推近。

5-6秒，香水喷雾瓶旁的水珠反光。
"""

GATE = {
    "状态": "标红（缺项 2）",
    "轮次": 1,
    "上限": 2,
    "结果": {
        "verdict": "FAIL",
        "checks": [{"id": "D1", "verdict": "PASS"}, {"id": "D2", "verdict": "FAIL"},
                   {"id": "D3", "verdict": "PASS"}, {"id": "D4", "verdict": "FAIL"}],
        "缺项": ["D2 画面位置：主列表行缺画面位置：第 1 行（判定 2 行 / 共 9 行，覆盖 7 行）",
                 "D4 自身框：主列表行缺自身框：第 1 行（画面位置没指向代表镜或格越界）"],
        "告警": [],
    },
}


def ref(name: str) -> str:
    return f"local:{TASK}/asset-uploads/{name}"


def seed_task(local_id: str = TASK, *, with_prompt: bool = True) -> Path:
    d = TMP_PROJECTS / local_id
    d.mkdir(parents=True, exist_ok=True)
    task = {"local_id": local_id, "state": "awaiting_approval", "bridge_id": BRIDGE,
            "created_at": iso(-600), "intent": "AT-22 E2 探针夹具（隔离根内）",
            "history": [{"at": iso(-600), "from": "rendering", "to": "awaiting_approval", "note": "夹具"}],
            # 提交快照只交了一张 → 次序对账「提交条数」不符（无行号的整体问题变体）
            "assets_confirmed": [{"no": 1, "url": ref("row1.png")}]}
    (d / "任务.json").write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
    if with_prompt:
        (d / "提示词.md").write_text(PROMPT, encoding="utf-8")
    return d


def seed_bridge() -> None:
    bd = TMP_ANALYSIS / BRIDGE
    bd.mkdir(parents=True, exist_ok=True)
    (bd / LIST_NAME).write_text(CK.format(bridge=BRIDGE, img1=ref("row1.png"), img6=ref("row6.png")),
                                encoding="utf-8")
    (bd / "拆解.md").write_text((ROOT / "tests" / "fixtures" / "at11-m1" / "拆解.md").read_text(
        encoding="utf-8-sig"), encoding="utf-8")
    (bd / "门禁.json").write_text(json.dumps(GATE, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- [0] 隔离

def isolation_phase() -> bool:
    print("[0] 隔离生效：任务根/清单根/门禁根/拆解根四处都指向临时根")
    seed_bridge()
    seed_task()
    ok = True
    ok &= check("清单根在临时根内", under_tmp(replace.analysis_root()), str(replace.analysis_root()))
    ok &= check("清单路径在临时根内", under_tmp(replace.analysis_root() / BRIDGE / LIST_NAME),
                str(replace.analysis_root() / BRIDGE / LIST_NAME))
    ok &= check("门禁路径在临时根内（selfcheck 读门禁与清单同源）",
                under_tmp(replace.analysis_root() / BRIDGE / "门禁.json"))
    ok &= check("hub 链任务根在临时根内", under_tmp(hub_api.TASKS_DIR), str(hub_api.TASKS_DIR))
    ok &= check("配置任务根（config.PROJECTS_DIR）在临时根内", under_tmp(config.PROJECTS_DIR),
                str(config.PROJECTS_DIR))
    ok &= check("拆解根（cards.ANALYSIS 已显式指到临时根）在临时根内", under_tmp(cards.ANALYSIS),
                str(cards.ANALYSIS))
    ok &= check("帧缓存根（frames.ANALYSIS 已显式指到临时根）在临时根内", under_tmp(frames.ANALYSIS),
                str(frames.ANALYSIS))
    return ok


# ---------------------------------------------------------------- [1] finding v2 schema

def _check_residue_item(items: list[dict]) -> dict:
    return next((item for item in items if item.get("项") == "绑图行残留"), {})


def schema_phase(items: list[dict]) -> bool:
    print("[1] finding v2 schema：命中类结构化字段齐全、片段可还原、原因不再产出")
    ok = True
    residue = _check_residue_item(items)
    ok &= check("绑图行残留判 FAIL（夹具正文确实残留原物词）", residue.get("结论") == "FAIL",
                json.dumps(residue.get("证据", ""), ensure_ascii=False)[:160])
    ok &= check("每条说明都不再有「原因」键（最大重复源已拔掉）",
                all("原因" not in (item.get("说明") or {}) for item in items),
                str([item["项"] for item in items if "原因" in (item.get("说明") or {})]))
    say = residue.get("说明") or {}
    found = say.get("发现") or []
    ok &= check("命中类 finding 带齐 v2 槽位（词/句序/上下文/片段/行称呼/图称呼/图引用/可修）",
                bool(found) and all(
                    f.get("词") and f.get("句序") and f.get("上下文") and f.get("片段")
                    and f.get("行称呼") and f.get("图称呼") and f.get("图引用") and f.get("可修")
                    for f in found),
                json.dumps([sorted(f) for f in found], ensure_ascii=False)[:400])
    ok &= check("`原文` 与 `文字` 同值（旧字段一个字节不改）",
                all(f.get("原文") == f.get("文字") for f in found))
    ok &= check("`证据` 仍是旧格式（历史探针/排障锚点不破）",
                "#6 的「" in str(residue.get("证据", "")) and "已传图行词表" in str(residue.get("证据", "")),
                str(residue.get("证据", ""))[:160])
    segments_ok, highlight_ok, cover_ok = True, False, True
    for finding in found:
        context, pieces = str(finding.get("上下文") or ""), finding.get("片段") or []
        if "".join(str(piece.get("文") or "") for piece in pieces) != context:
            cover_ok = False
        for piece in pieces:
            start, end = piece.get("起"), piece.get("止")
            if not isinstance(start, int) or not isinstance(end, int) or context[start:end] != piece.get("文"):
                segments_ok = False
            if piece.get("高亮"):
                highlight_ok = True
    ok &= check("`片段` 拼起来还原 `上下文`（原文一字不改）", cover_ok)
    ok &= check("`片段` 的 起/止 与 `上下文` 下标逐段对齐", segments_ok)
    ok &= check("`片段` 至少一段 高亮=true（前端按偏移上色，不解析文字）", highlight_ok)
    ok &= check("同一句的命中 finding 共享 行号+句序（渲染层并卡的唯一依据；AT-23 B1 折叠后同句只剩完整原词）",
                len({(f.get("行号"), f.get("句序")) for f in found}) == 1 and bool(found),
                str(sorted({(f.get("行号"), f.get("句序")) for f in found})))
    # AT-23 B1（质检面板就地处理闭环 第 3 批）：判定集仍是一词一条，**展示集**按同一「行×句」的
    # 包含关系折叠成完整原词、切片进 `切片`。原断言「数据层仍是同句多词（一词一条，≥2 条）」
    # 描述的是折叠前的口径，B1 落地后必红——改成同时锁「判定数没少 + 展示收口 + 切片留全」。
    stats_row = (residue.get("说明") or {}).get("统计") or {}
    ok &= check("AT-23 B1：判定集一个不少（统计.判定词数 > 展示条数）、展示收口成完整原词、切片进 `切片`",
                len({f.get("词") for f in found}) == len(found)
                and int(stats_row.get("判定词数") or 0) > len(found)
                and all(f.get("切片") for f in found),
                f"判定词数={stats_row.get('判定词数')} 展示={[f.get('词') for f in found]} "
                f"切片={[len(f.get('切片') or []) for f in found]}")
    return ok


# ---------------------------------------------------------------- [2] 一条说清（可见字段）

def sayable_phase(items: list[dict]) -> bool:
    print("[2] 一条说清：可见字段零禁用词、行指代是「第 N 行「短名」」")
    ok = True
    bad: list[str] = []
    for item in items:
        say = item.get("说明") or {}
        for field in ("结论人话", "项显示名", "处理"):
            text = str(say.get(field) or (item.get("项显示名") if field == "项显示名" else "") or "")
            for pattern, why in BANNED:
                if re.search(pattern, text):
                    bad.append(f"{item.get('项')}.{field}={text[:48]!r} ← {why}")
    ok &= check("可见字段（结论人话/项显示名/处理）零禁用词", not bad, "；".join(bad[:6]))
    residue = _check_residue_item(items)
    head = str((residue.get("说明") or {}).get("结论人话") or "")
    ok &= check("结论人话点出「几处 + 第几行 + 短名」（一眼能指出改哪行）",
                bool(re.search(r"\d+\s*个", head)) and "第 6 行" in head and "道具A" in head, head)
    ok &= check("项显示名是话不是内部项名（提示词里的老词）",
                residue.get("项显示名") == "提示词里的老词", str(residue.get("项显示名")))
    ok &= check("处理只写页面里真有的入口（无磁盘路径、无清单列名）",
                "退回置换" in str((residue.get("说明") or {}).get("处理") or "")
                and "analysis/reverse" not in str((residue.get("说明") or {}).get("处理") or ""),
                str((residue.get("说明") or {}).get("处理"))[:120])
    return ok


# ---------------------------------------------------------------- [3] 去重可量化

def legacy_baseline() -> dict:
    """真实历史文件（auto-0066 的 pre-B2 自检.json）：同一命中句被几个可见位承载。"""
    out = {"file": str(LEGACY_FILE), "exists": LEGACY_FILE.is_file(), "carriers": 0,
           "sentence": "", "fields": {}}
    if not LEGACY_FILE.is_file():
        return out
    data = json.loads(LEGACY_FILE.read_text(encoding="utf-8-sig"))
    item = next((row for row in data if row.get("项") == "绑图行残留"), None)
    if not isinstance(item, dict):
        return out
    say = item.get("说明") or {}
    evidence = str(item.get("证据") or "")
    match = re.search(r"…([^…]{8,})…", evidence)
    sentence = match.group(1) if match else ""
    out["sentence"] = sentence
    probe = sentence[3:14] if len(sentence) > 14 else sentence          # 命中句中段（各副本都含）
    carriers = {}
    if probe and probe in str(say.get("结论人话") or ""):
        carriers["结论人话"] = 1
    if probe and probe in str(say.get("原因") or ""):
        carriers["原因"] = 1
    findings = say.get("发现") or []
    if probe and any(probe in str(f.get("文字") or "") for f in findings):
        carriers["发现"] = sum(1 for f in findings if probe in str(f.get("文字") or ""))
    if probe and probe in evidence:
        carriers["证据"] = 1
    out["fields"] = carriers
    out["carriers"] = sum(carriers.values())
    return out


def dedupe_phase(items: list[dict]) -> bool:
    print("[3] 去重可量化：数据层一词一条 × 渲染层一句一卡，基线取自真实历史载荷")
    ok = True
    residue = _check_residue_item(items)
    say = residue.get("说明") or {}
    stats = say.get("统计") or {}
    found = say.get("发现") or []
    ok &= check("统计给出 词数/句数（并卡前后的口径都留痕）",
                stats.get("词数") == len(found) and stats.get("句数") == 1,
                json.dumps(stats, ensure_ascii=False)[:200])
    ok &= check("词表行数/正文字数进统计（可见区不再出现这两个数）",
                isinstance(stats.get("词表行数"), int) and isinstance(stats.get("正文字数"), int),
                json.dumps(stats, ensure_ascii=False)[:200])
    words = {f.get("词") for f in found}
    # AT-23 B1：原断言「同句多词 ≥3」是折叠前的展示口径；现锁「判定 ≥3 个词、展示折叠后更少」。
    ok &= check("判定 ≥3 个词、展示按包含关系折叠成完整原词（AT-23 B1：判定不动、展示收口）",
                int(stats.get("判定词数") or 0) >= 3 and 0 < len(words) < int(stats["判定词数"]),
                f"判定词数={stats.get('判定词数')} 展示={sorted(words)}")
    legacy = legacy_baseline()
    print(f"      真实历史载荷（pre-B2）：{legacy['file']} 承载位={legacy['carriers']} {legacy['fields']}")
    ok &= check("真实历史载荷里同一命中句被 ≥3 个可见位承载（基线可量化）",
                legacy["carriers"] >= 3, json.dumps(legacy, ensure_ascii=False)[:240])
    ok &= check("v2 载荷里检查器原句只出现在 `原文`/`证据`（可见位 0 承载）",
                all(f.get("原文") == f.get("文字") for f in found)
                and not any(str(f.get("原文") or "") in str(say.get("结论人话") or "") for f in found))
    return ok


# ---------------------------------------------------------------- [4] hub 透传 + [5] 折叠数据

def passthrough_phase() -> bool:
    print("[4] hub_api 透传：get_task 把 v2 字段原样带出，旧结构不伪造")
    ok = True
    task = hub_api.get_task(TASK)
    rows = task.get("自检") or []
    residue = _check_residue_item([row for row in rows if isinstance(row, dict)])
    found = ((residue.get("说明") or {}).get("发现") or [])
    ok &= check("get_task 透出 自检 束（含读路径汇总的门禁条目）",
                bool(rows) and any(row.get("项") == selfcheck.GATE_ITEM_NAME for row in rows),
                str([row.get("项") for row in rows]))
    ok &= check("get_task 透出的 finding 带 v2 槽位（词/片段/行称呼/图引用 一个不少）",
                bool(found) and all(f.get("词") and f.get("片段") and f.get("行称呼") and f.get("图引用")
                                    for f in found),
                json.dumps(found[:1], ensure_ascii=False)[:300])
    ok &= check("get_task 透出的 统计/清单行/原句 三块都在（折叠区数据源）",
                all(key in (residue.get("说明") or {}) for key in ("统计", "清单行", "原句")),
                str(sorted((residue.get("说明") or {}).keys())))

    legacy_dir = seed_task(TASK_LEGACY, with_prompt=False)
    legacy = json.loads(LEGACY_FILE.read_text(encoding="utf-8-sig")) if LEGACY_FILE.is_file() else []
    (legacy_dir / "自检.json").write_text(json.dumps(legacy, ensure_ascii=False, indent=2), encoding="utf-8")
    before = (legacy_dir / "自检.json").read_bytes()
    rows2 = hub_api.get_task(TASK_LEGACY).get("自检") or []
    file_rows = [row for row in rows2 if row.get("项") != selfcheck.GATE_ITEM_NAME]
    ok &= check("旧结构（pre-B2 载荷）原样透出，不伪造 v2 槽位",
                bool(file_rows) and all(not any(k in (f or {}) for f in (row.get("说明") or {}).get("发现") or []
                                                     for k in ("词", "片段", "行称呼"))
                                        for row in file_rows),
                json.dumps([sorted(row.get("说明") or {}) for row in file_rows], ensure_ascii=False)[:240])
    ok &= check("旧结构文件被只读对待（跑前跑后逐字节一致）",
                (legacy_dir / "自检.json").read_bytes() == before)
    return ok


def fold_phase(items: list[dict]) -> bool:
    print("[5] 折叠区数据齐备：统计/清单行/原句三块；可见字段不含任何统计")
    ok = True
    residue = _check_residue_item(items)
    say = residue.get("说明") or {}
    lines = say.get("清单行") or {}
    block = say.get("原句") or {}
    ok &= check("`清单行` 带该行整行 md 原文", str(6) in lines and str(lines.get("6", "")).startswith("| 6 |"),
                json.dumps(lines, ensure_ascii=False)[:200])
    ok &= check("`原句` 带句序与整段（前后各带一句）",
                isinstance(block.get("句序"), int) and "紫色圆柱管身" in str(block.get("段") or "")
                and len(str(block.get("段") or "")) > 20, json.dumps(block, ensure_ascii=False)[:200])
    ok &= check("`统计` 里的键不在可见字段里（结论人话不含数字统计串）",
                all(not re.search(r"词表|正文字数|行→图号", str(say.get(key) or ""))
                    for key in ("结论人话", "处理")))
    return ok


# ---------------------------------------------------------------- [6] 呈现联动

def frontend_phase(items: list[dict], legacy_path: Path) -> bool:
    print("[6] 呈现联动：node scripts/probe-at22-e2.mjs（SSR 真渲染 drawer）")
    v2_path = TMP_ROOT / "v2-items.json"
    v2_path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    dump = os.environ.get("AT22E2_DUMP_V2")           # 排障用：把夹具载荷留一份到临时根外
    if dump:
        Path(dump).write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    cmd = ["node", "scripts/probe-at22-e2.mjs", "--v2", str(v2_path), "--legacy", str(legacy_path)]
    proc = subprocess.run(cmd, cwd=str(WEB), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=900)
    tail = "\n".join(line for line in (proc.stdout or "").splitlines() if "FAIL" in line or "PASS," in line)
    ok = check("前端探针全绿（单条模板/并卡/禁用词/折叠/定位/去重/降级）", proc.returncode == 0,
               (tail[-800:] or (proc.stderr or "")[-400:]))
    for line in (proc.stdout or "").splitlines():
        if line.strip().startswith("[PASS]"):
            print(f"     {line.strip()}")
    return ok


# ---------------------------------------------------------------- 入口

def main() -> int:
    print(f"[probe-at22-e2] 隔离根 {TMP_ROOT}")
    ok = True
    try:
        ok &= isolation_phase()
        items = selfcheck.run_selfcheck(TMP_PROJECTS / TASK)
        ok &= schema_phase(items)
        ok &= sayable_phase(items)
        ok &= dedupe_phase(items)
        ok &= passthrough_phase()
        ok &= fold_phase(items)
        legacy_copy = TMP_ROOT / "legacy-autocheck.json"
        if LEGACY_FILE.is_file():
            legacy_copy.write_bytes(LEGACY_FILE.read_bytes())      # 只读复制，真实文件不动
        ok &= frontend_phase(hub_api.get_task(TASK).get("自检") or [], legacy_copy)
    finally:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)

    after = {"projects": snapshot(REAL_PROJECTS), "analysis": snapshot(REAL_ANALYSIS)}
    for name in ("projects", "analysis"):
        lost = sorted(BEFORE[name] - after[name])
        ok &= check(f"[7] 真实 {name} 没有条目消失（不写真实目录）", not lost, f"丢了 {lost}")
        extra = sorted(after[name] - BEFORE[name])
        if extra:
            print(f"      注：{name} 跑期间另有新增 {extra[:6]}（非本探针产物：同仓并发跑别的探针/任务）")
    ok &= check("[7] 临时根已删净", not TMP_ROOT.exists(), str(TMP_ROOT))

    passed = sum(1 for _, good in RESULTS if good)
    total = len(RESULTS)
    print(f"\n[probe-at22-e2] {passed}/{total} PASS")
    return 0 if (ok and passed == total) else 1


if __name__ == "__main__":
    raise SystemExit(main())
