#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AT-22 E4 探针：**误报豁免白名单**（行 × 词键）的全生命周期与防放大断言（写类，必须 --isolated）。

跑法（wanrepro/ 下）：`python automation/probe_at22_e4.py --isolated`，退出码 0 = 全绿。
**不带 `--isolated` 一律拒跑**：本探针会写任务/清单/提示词/自检，只在临时根内跑。

判据（卡面 AT22-B4 第 5 条 + 方案 §2–§5、§7，一条一条对）：

  [0] 隔离生效：任务根 / 清单根 / 拆解根都在临时根内，且渲染走**进程内假 bridge**（真 bridge 一次都不碰）。
  [1] 词键与闭包同源：`word_key` = NFKC+strip+casefold；`row_word_closure` 与 `render.residue_words`
      逐词相等（服务端校验不另写一份口径）；≥3 字口径生效（2 字组不进闭包）。
  [2] 生命周期（单行夹具）：FAIL → 加白 → **自检重跑后 PASS 且证据写明「已按你的裁量保留 1 个词」**、
      台账 生效 → 落 `任务.json`（v1 schema 字段齐全、唯一键=行×词键）→ 同 (行,词) 再加幂等 →
      撤销 → **同一个 FAIL 原样报回**（同行号/同词/同上下文原文）→ 任务文件里的白名单键被清掉。
  [3] 行级作用域（两行同词）：豁免只对「该行×该词键」生效——豁免 #6 后 #8 仍报；两行都豁免才 PASS；
      撤销全部后两条命中都回来；批量加白落**多条行级条目**（不是任务级词）。
  [4] 防放大：整句/任意词混不进来（V3 词键闭包，400 人话）、2 字词（V2）、不存在的行（V1）、
      没绑图的行（V4/409）、品牌位（409）；**豁免 core 不等于整行免检**（2–3 字组仍逐条判）。
  [5] 台账五态（读路径）：生效 / 失效-正文无此词 / 失效-行没绑图 / 失效-行不存在 / 未参与-不在词表；
      图号只是证据快照（写成 999 仍 生效）。
  [6] 状态守卫（V6）：只有 awaiting_approval 能改，其余八态 409 + 人话 + **任务文件零变化**。
  [7] 上限与撤销语义：条目 50 上限（V5 409）；撤销不存在的条目 → 404；空=清键不留空壳。
  [8] 不重渲染：全流程**零渲染请求**，正文 / 审批快照 / 状态一字不变（豁免不改任何渲染输入）。
  [9] HTTP 路由冒烟：真起隔离实例，POST / DELETE `/api/hub/task/<id>/selfcheck-whitelist`，
      `GET /api/hub/task/<id>` 透出 `selfcheck_whitelist`，错误码走线上。
  [10] 前端联动：`node scripts/probe-at22-e4.mjs` 全绿（起态门/接线/真渲染）。
  [11] 真实目录只读：真实 projects/analysis 跑前跑后零变化；临时根跑完删净。
"""
from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]                       # wanrepro/
WEB = ROOT.parent / "web"

if "--isolated" not in sys.argv:
    print("[probe-at22-e4] 拒跑：本探针会写任务/清单/提示词/自检，必须带 --isolated。\n"
          "  例：python automation/probe_at22_e4.py --isolated")
    raise SystemExit(2)

REAL_PROJECTS = ROOT / "automation" / "projects"
REAL_ANALYSIS = ROOT / "analysis" / "reverse"


def snapshot(root: Path) -> set[str]:
    try:
        return {p.name for p in root.iterdir()}
    except OSError:
        return set()


BEFORE = {"projects": snapshot(REAL_PROJECTS), "analysis": snapshot(REAL_ANALYSIS)}

TMP_ROOT = Path(tempfile.mkdtemp(prefix="probe-at22-e4-"))
TMP_PROJECTS = TMP_ROOT / "projects"
TMP_ANALYSIS = TMP_ROOT / "analysis"
TMP_PROJECTS.mkdir(parents=True, exist_ok=True)
TMP_ANALYSIS.mkdir(parents=True, exist_ok=True)
os.environ["WANREPRO_AUTO_DIR"] = str(TMP_PROJECTS)
os.environ["WANREPRO_ANALYSIS_ROOT"] = str(TMP_ANALYSIS)

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ui"))

# 假 bridge 必须在 `automation.config` 被 import 之前定下基址（config.BRIDGE_BASE 是 import 期常量）。
from automation.probe_guard import StubBridge, free_port, http_req  # noqa: E402


def _never_render(path: str) -> str:
    """白名单**不许**触发渲染：真被调到就是缺陷，探针按 0 次渲染断言。"""
    raise AssertionError(f"白名单流程里不该渲染，却收到渲染请求：{path}")


STUB = StubBridge(render_fn=_never_render)
os.environ["WANREPRO_BRIDGE"] = STUB.base

from automation import cards, config, frames, replace, selfcheck, state  # noqa: E402
import hub_api  # noqa: E402

cards.ANALYSIS = TMP_ANALYSIS
frames.ANALYSIS = TMP_ANALYSIS

LIST_NAME = replace.LIST_NAME
RESULTS: list[tuple[str, bool]] = []
TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6300010000050001a5f645400000000049454e44ae426082")

BRIDGE_ONE = "probe-e4-one"
BRIDGE_TWO = "probe-e4-two"
BRIDGE_CORE = "probe-e4-core"
BRIDGE_LIMIT = "probe-e4-limit"
BRIDGE_ROUTE = "probe-e4-route"
BRIDGE_GUARD = "probe-e4-guard"

TASK_ONE = "auto-9901"
TASK_TWO = "auto-9902"
TASK_CORE = "auto-9903"
TASK_LIMIT = "auto-9904"
TASK_ROUTE = "auto-9905"
TASK_GUARD = "auto-9906"

WORD = "香水瓶"                       # 3 字 core → 词表只有它一个（2 字组被 ≥3 口径滤掉）
CORE_LONG = "双耳奖杯造型香水小物体"     # 长 core：豁免它不等于整行免检
PROMPT_ONE = "# 提示词\n\n0-2秒，中景，桌上放着香水瓶，镜头缓慢推近。\n"
PROMPT_TWO = PROMPT_ONE
PROMPT_CORE = "# 提示词\n\n0-2秒，中景，奖杯造型静置桌面，镜头缓慢推近。\n"

CK_ONE = """# 待确认清单 · {bridge}

| # | 类别 | 模型看到什么 | 命名 | 资产图 | 锁什么 | 建议 | 用我的描述 |
|---|------|-------------|------|--------|--------|------|------------|
| 1 | 主体 | 香水瓶 |  | {i1} | 外形比例 | 必须换 |  |
"""

CK_TWO = """# 待确认清单 · {bridge}

| # | 类别 | 模型看到什么 | 命名 | 资产图 | 锁什么 | 建议 | 用我的描述 |
|---|------|-------------|------|--------|--------|------|------------|
| 6 | 道具 | 香水瓶 |  | {i6} | 颜色、支点位置 | 必须换 |  |
| 8 | 道具 | 香水瓶 |  | {i8} | 颜色、支点位置 | 必须换 |  |
"""

CK_CORE = """# 待确认清单 · {bridge}

| # | 类别 | 模型看到什么 | 命名 | 资产图 | 锁什么 | 建议 | 用我的描述 |
|---|------|-------------|------|--------|--------|------|------------|
| 2 | 主体 | 双耳奖杯造型香水小物体 |  | {i2} | 外形比例 | 必须换 |  |
| 7 | 道具 | 双耳奖杯造型香水小物体 |  |  | 外形比例 | 建议准备 |  |
| 12 | 品牌视觉（置换位） | 参考片品牌位 标语品牌字：「POP!」 |  | {i12} | 客户品牌字样（待填） | 必须换 |  |
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


def seed_bridge(bridge: str, ck_tpl: str = CK_ONE, local_id: str = TASK_ONE) -> None:
    bd = TMP_ANALYSIS / bridge
    bd.mkdir(parents=True, exist_ok=True)
    text = ck_tpl.format(bridge=bridge,
                         i1=ref(local_id, "row1.png"), i6=ref(local_id, "row6.png"),
                         i8=ref(local_id, "row8.png"), i2=ref(local_id, "row2.png"),
                         i12=ref(local_id, "row12.png"))
    (bd / LIST_NAME).write_text(text, encoding="utf-8")
    (bd / "拆解.md").write_text((ROOT / "tests" / "fixtures" / "at11-m1" / "拆解.md").read_text(
        encoding="utf-8-sig"), encoding="utf-8")
    (bd / "门禁.json").write_text(json.dumps(GATE_PASS, ensure_ascii=False, indent=2), encoding="utf-8")


def seed_task(local_id: str, st: str, bridge: str, *, prompt: str | None = None,
              whitelist=None) -> Path:
    d = TMP_PROJECTS / local_id
    d.mkdir(parents=True, exist_ok=True)
    task = {"local_id": local_id, "state": st, "bridge_id": bridge,
            "created_at": iso(-600), "intent": "AT-22 E4 探针夹具（隔离根内）",
            "history": [{"at": iso(-600), "from": "discussing", "to": st, "note": "夹具"}],
            "audio_mode": "保留原声"}
    if whitelist is not None:
        task[selfcheck.WHITELIST_FIELD] = whitelist
    (d / "任务.json").write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
    if prompt is not None:
        (d / "提示词.md").write_text(prompt, encoding="utf-8")
    up = d / "asset-uploads"
    up.mkdir(parents=True, exist_ok=True)
    for name in ("row1.png", "row2.png", "row6.png", "row8.png", "row12.png"):
        (up / name).write_bytes(TINY_PNG)
    return d


def task_json(d: Path) -> dict:
    return json.loads((d / "任务.json").read_text(encoding="utf-8"))


def residue_of(items: list[dict]) -> dict:
    return next((item for item in items if item.get("项") == "绑图行残留"), {})


def findings_of(item: dict) -> list[dict]:
    return list((item.get("说明") or {}).get("发现") or [])


def texts_of(item: dict) -> list[str]:
    return [str(f.get("文字")) for f in findings_of(item)]


def rows_of(item: dict) -> list[int]:
    return [int(f["行号"]) for f in findings_of(item) if f.get("行号")]


def words_of(item: dict) -> list[str]:
    return [str(f.get("词")) for f in findings_of(item) if f.get("词")]


def ledger_of(item: dict) -> list[dict]:
    return list(item.get("豁免") or [])


def add(local_id: str, rows, word: str, **kw) -> dict:
    return hub_api.set_selfcheck_whitelist(local_id, [rows] if isinstance(rows, int) else list(rows),
                                           word=word, **kw)


def drop(local_id: str, rows=None, word: str = "", remove_all: bool = False) -> dict:
    rows = [] if rows is None else ([rows] if isinstance(rows, int) else list(rows))
    return hub_api.set_selfcheck_whitelist(local_id, rows, word=word, remove=True,
                                           remove_all=remove_all)


def call_err(fn) -> tuple[int | None, str]:
    try:
        fn()
    except hub_api.HubError as err:
        return getattr(err, "status", None), str(err)
    except Exception as err:                      # noqa: BLE001 —— 非 HubError 也要如实报
        return None, f"{type(err).__name__}: {err}"
    return None, ""


# ---------------------------------------------------------------- [0] 隔离

def isolation_phase() -> bool:
    print("[0] 隔离生效：任务/清单/拆解根都在临时根内，渲染只可能打进程内假 bridge")
    ok = True
    ok &= check("清单根在临时根内", under_tmp(replace.analysis_root()), str(replace.analysis_root()))
    ok &= check("hub 链任务根在临时根内", under_tmp(hub_api.TASKS_DIR), str(hub_api.TASKS_DIR))
    ok &= check("配置任务根在临时根内", under_tmp(config.PROJECTS_DIR), str(config.PROJECTS_DIR))
    ok &= check("拆解根（cards.ANALYSIS）在临时根内", under_tmp(cards.ANALYSIS), str(cards.ANALYSIS))
    ok &= check("bridge 基址是本进程内的假 bridge（不是 8776/8779 真桥）",
                config.BRIDGE_BASE == STUB.base and str(STUB.port) not in ("8776", "8779"),
                f"{config.BRIDGE_BASE} vs stub {STUB.base}")
    return ok


# ---------------------------------------------------------------- [1] 词键 + 闭包同源

def wordkey_phase() -> bool:
    print("[1] 词键 = NFKC+strip+casefold；闭包与 render.residue_words 同源；≥3 字口径")
    seed_bridge(BRIDGE_ONE, CK_ONE, TASK_ONE)
    seed_bridge(BRIDGE_CORE, CK_CORE, TASK_CORE)
    ok = True
    ok &= check("[1] 词键：前后空白被 strip", selfcheck.word_key("　 香水瓶 \t") == "香水瓶",
                repr(selfcheck.word_key("　 香水瓶 \t")))
    ok &= check("[1] 词键：全角 NFKC + casefold", selfcheck.word_key("ＡＢＣ") == "abc",
                repr(selfcheck.word_key("ＡＢＣ")))
    ok &= check("[1] 词键：大小写不敏感（AbC == abc）", selfcheck.word_key("AbC") == selfcheck.word_key("abc"))
    closure = selfcheck.row_word_closure(BRIDGE_ONE)
    ok &= check("[1] 闭包含这一行（行号=清单 #列）", sorted(closure) == [1], str(sorted(closure)))
    render = selfcheck._load_render()
    ents = render.parse_checklist(TMP_ANALYSIS / BRIDGE_ONE / LIST_NAME)
    render.assign_numbers(ents, [], {})
    same = all(set(closure[int(e.row_no)]["词"]) == {selfcheck.word_key(w) for w in render.residue_words(e)}
               for e in ents)
    ok &= check("[1] 闭包词集 == render.residue_words（同一取词点，不另写一份口径）", same,
                json.dumps({int(e.row_no): render.residue_words(e) for e in ents}, ensure_ascii=False))
    ok &= check("[1] ≥3 字口径：2 字组（香水/水瓶）不进闭包", "香水" not in closure[1]["词"],
                str(sorted(closure[1]["词"])))
    ok &= check("[1] 来源标注：3 字 core → core", closure[1]["来源"].get(WORD) == "core",
                json.dumps(closure[1]["来源"], ensure_ascii=False))
    core_closure = selfcheck.row_word_closure(BRIDGE_CORE)
    ok &= check("[1] 未绑图行 已传图=False（V4 判据）", core_closure[7]["已传图"] is False,
                json.dumps(core_closure.get(7), ensure_ascii=False))
    ok &= check("[1] 品牌位标记 不查=True（不参加残留检查）", core_closure[12]["不查"] is True,
                json.dumps(core_closure.get(12), ensure_ascii=False))
    return ok


# ---------------------------------------------------------------- [2] 生命周期

def lifecycle_phase() -> bool:
    print("[2] 生命周期：FAIL → 加白 → PASS（带豁免标记）→ 幂等 → 撤销 → 原样报回")
    ok = True
    d = seed_task(TASK_ONE, "awaiting_approval", BRIDGE_ONE, prompt=PROMPT_ONE)
    items0 = selfcheck.run_selfcheck(d)
    item0 = residue_of(items0)
    ok &= check("[2] 基线 FAIL（正文里确实有原物词）", item0.get("结论") == "FAIL", item0.get("结论"))
    ok &= check("[2] 基线命中词 = 香水瓶（词表只有它一个）", words_of(item0) == [WORD], str(words_of(item0)))
    before_texts = texts_of(item0)

    echo = add(TASK_ONE, 1, WORD)
    item1 = residue_of(echo.get("自检") or [])
    ok &= check("[2] 加白回显这一代自检（前端不用二次拉详情）",
                isinstance(echo.get("自检"), list) and item1.get("结论") == "PASS",
                json.dumps(item1.get("结论"), ensure_ascii=False))
    ok &= check("[2] PASS 证据写明「已按你的裁量保留 1 个词」",
                "已按你的裁量保留 1 个词" in str(item1.get("证据")) and WORD in str(item1.get("证据")),
                str(item1.get("证据"))[:200])
    ok &= check("[2] PASS 结论人话带「其中 1 个词按你的裁量保留」（豁免是可见状态，不是隐形通过）",
                "1 个词按你的裁量保留" in str((item1.get("说明") or {}).get("结论人话")),
                str((item1.get("说明") or {}).get("结论人话")))
    ledger1 = ledger_of(item1)
    ok &= check("[2] 台账 1 条且状态=生效", len(ledger1) == 1 and ledger1[0].get("状态") == "生效",
                json.dumps(ledger1, ensure_ascii=False))
    ok &= check("[2] 台账带行号/词/词键（判定只认这两列）",
                ledger1 and ledger1[0].get("行") == 1 and ledger1[0].get("词键") == WORD,
                json.dumps(ledger1, ensure_ascii=False))
    ok &= check("[2] 自检.json 落盘就是这一代（不是只改响应体）",
                residue_of(selfcheck.load_selfcheck(d) or []).get("结论") == "PASS")

    task = task_json(d)
    wl = task.get(selfcheck.WHITELIST_FIELD) or {}
    entry = (wl.get("条目") or [{}])[0]
    ok &= check("[2] 任务.json 落 selfcheck_whitelist v1（版本/更新于/条目）",
                wl.get("版本") == 1 and bool(wl.get("更新于")) and len(wl.get("条目") or []) == 1,
                json.dumps(wl, ensure_ascii=False)[:240])
    ok &= check("[2] 条目字段齐全（行/词/词键/来源/图号/加入于/操作人）",
                all(k in entry for k in ("行", "词", "词键", "来源", "图号", "加入于", "操作人")),
                json.dumps(entry, ensure_ascii=False))
    ok &= check("[2] 来源=core（豁免的是整段实体名，一眼看出粒度）", entry.get("来源") == "core",
                str(entry.get("来源")))
    ok &= check("[2] 操作人=owner-hub（不记 IP/账号）", entry.get("操作人") == "owner-hub")

    again = add(TASK_ONE, 1, WORD)
    ok &= check("[2] 同 (行,词) 再加 → 幂等（已存在=1、新增=0、条目仍 1 条）",
                again.get("已存在") == 1 and again.get("新增") == 0
                and len((task_json(d).get(selfcheck.WHITELIST_FIELD) or {}).get("条目") or []) == 1,
                json.dumps({k: again.get(k) for k in ("新增", "已存在")}, ensure_ascii=False))

    back = drop(TASK_ONE, 1, WORD)
    item2 = residue_of(back.get("自检") or [])
    ok &= check("[2] 撤销回显：这一项从 PASS 回到 FAIL", item2.get("结论") == "FAIL", item2.get("结论"))
    ok &= check("[2] 撤销后同一个 FAIL 原样复现（文字逐字相同、同行号同词）",
                texts_of(item2) == before_texts, f"{texts_of(item2)} vs {before_texts}")
    ok &= check("[2] 撤销计数=1", back.get("移除") == 1, str(back.get("移除")))
    ok &= check("[2] 任务文件里白名单键被清掉（空=没豁免，不留空壳）",
                selfcheck.WHITELIST_FIELD not in task_json(d),
                json.dumps(task_json(d).get(selfcheck.WHITELIST_FIELD), ensure_ascii=False))
    ok &= check("[2] 撤销后 豁免 台账整块消失（没白名单就不落这个键）",
                "豁免" not in item2 and "豁免" not in residue_of(selfcheck.load_selfcheck(d) or []))
    return ok


# ---------------------------------------------------------------- [3] 行级作用域

def row_scope_phase() -> bool:
    print("[3] 行级作用域：豁免只对「该行 × 该词键」生效（不是任务级词）")
    seed_bridge(BRIDGE_TWO, CK_TWO, TASK_TWO)
    d = seed_task(TASK_TWO, "awaiting_approval", BRIDGE_TWO, prompt=PROMPT_TWO)
    ok = True
    item0 = residue_of(selfcheck.run_selfcheck(d))
    ok &= check("[3] 基线：两行都命中（#6、#8）", item0.get("结论") == "FAIL" and rows_of(item0) == [6, 8],
                f"{item0.get('结论')} {rows_of(item0)}")

    item1 = residue_of(add(TASK_TWO, 6, WORD).get("自检") or [])
    ok &= check("[3] 只豁免 #6 → 该项仍 FAIL（#8 还留着）", item1.get("结论") == "FAIL", item1.get("结论"))
    ok &= check("[3] 只豁免 #6 → 命中行只剩 [8]（作用域是行级）", rows_of(item1) == [8], str(rows_of(item1)))
    ok &= check("[3] FAIL 文案写明「另有 1 个词按你的裁量保留」（豁免与未豁免分开说）",
                "另有 1 个词按你的裁量保留" in str((item1.get("说明") or {}).get("结论人话")),
                str((item1.get("说明") or {}).get("结论人话")))
    ok &= check("[3] 证据里写清 #{行}「词」而不是只说「1 个词」",
                "#6「香水瓶」" in str(item1.get("证据")), str(item1.get("证据"))[:200])

    item2 = residue_of(add(TASK_TWO, [6, 8], WORD).get("自检") or [])
    entries = (task_json(d).get(selfcheck.WHITELIST_FIELD) or {}).get("条目") or []
    ok &= check("[3] 两行一起保留 → PASS（批量落两条行级条目）",
                item2.get("结论") == "PASS" and len(entries) == 2
                and sorted(e["行"] for e in entries) == [6, 8],
                json.dumps(entries, ensure_ascii=False))
    ok &= check("[3] 台账两条都 生效", [e.get("状态") for e in ledger_of(item2)] == ["生效", "生效"],
                json.dumps(ledger_of(item2), ensure_ascii=False))

    item3 = residue_of(drop(TASK_TWO, remove_all=True).get("自检") or [])
    ok &= check("[3] 全部撤销 → 两条命中都回来", item3.get("结论") == "FAIL" and rows_of(item3) == [6, 8],
                f"{item3.get('结论')} {rows_of(item3)}")
    return ok


# ---------------------------------------------------------------- [4] 防放大（写路径校验）

def guard_write_phase() -> bool:
    print("[4] 防放大：整句/2 字词/不存在的行/没绑图/品牌位一律拒绝，且零副作用")
    ok = True
    seed_bridge(BRIDGE_ONE, CK_ONE, TASK_ONE)
    seed_bridge(BRIDGE_CORE, CK_CORE, TASK_CORE)
    d1 = seed_task(TASK_ONE, "awaiting_approval", BRIDGE_ONE, prompt=PROMPT_ONE)
    d3 = seed_task(TASK_CORE, "awaiting_approval", BRIDGE_CORE, prompt=PROMPT_CORE)
    base1 = (d1 / "任务.json").read_text(encoding="utf-8")
    base3 = (d3 / "任务.json").read_text(encoding="utf-8")

    sentence = "中景桌上放着香水瓶镜头缓慢推近不要字幕"
    status, msg = call_err(lambda: add(TASK_ONE, 1, sentence))
    ok &= check("[4] 整句混进来 → 400 人话（V3 词键闭包）", status == 400 and "残留词表" in msg,
                f"{status} {msg}")
    status, msg = call_err(lambda: add(TASK_ONE, 1, "香水"))
    ok &= check("[4] 2 字词 → 400 人话（V2 只收 ≥3 字）", status == 400 and "3" in msg, f"{status} {msg}")
    status, msg = call_err(lambda: add(TASK_ONE, 99, WORD))
    ok &= check("[4] 不存在的行 → 400 人话（V1）", status == 400 and "99" in msg, f"{status} {msg}")
    status, msg = call_err(lambda: add(TASK_CORE, 7, CORE_LONG))
    ok &= check("[4] 没绑图的行 → 409 人话（V4）", status == 409 and "绑图" in msg, f"{status} {msg}")
    status, msg = call_err(lambda: add(TASK_CORE, 12, WORD))
    ok &= check("[4] 品牌位 → 409 人话（品牌位不参加残留检查）", status == 409 and "品牌" in msg,
                f"{status} {msg}")
    status, msg = call_err(lambda: add(TASK_ONE, 1, ""))
    ok &= check("[4] 空词 → 400 人话", status == 400 and "词" in msg, f"{status} {msg}")
    ok &= check("[4] 全部拒绝之后任务文件零变化（拒绝不带副作用）",
                (d1 / "任务.json").read_text(encoding="utf-8") == base1
                and (d3 / "任务.json").read_text(encoding="utf-8") == base3)

    # 豁免 core ≠ 整行免检：core 在闭包里（V3 放行），但 2–3 字组仍逐条判
    item = residue_of(add(TASK_CORE, 2, CORE_LONG).get("自检") or [])
    ok &= check("[4] 豁免 core 后该项仍 FAIL（2–3 字组仍逐条判，不是整行免检）",
                item.get("结论") == "FAIL", item.get("结论"))
    ok &= check("[4] 命中里不再有被豁免的 core，其它组词照报",
                CORE_LONG not in words_of(item) and bool(words_of(item)), str(words_of(item)))
    ok &= check("[4] 被豁免的 core 记为「失效-正文无此词」（正文没这句，如实说）",
                [e.get("状态") for e in ledger_of(item)] == ["失效-正文无此词"],
                json.dumps(ledger_of(item), ensure_ascii=False))
    drop(TASK_CORE, remove_all=True)
    return ok


# ---------------------------------------------------------------- [5] 台账五态（读路径）

def ledger_phase() -> bool:
    print("[5] 台账五态 + 图号不参与判定（图号只是证据快照）")
    seed_bridge(BRIDGE_CORE, CK_CORE, TASK_CORE)
    wl = {"版本": 1, "更新于": iso(), "条目": [
        {"行": 2, "词": "奖杯造", "词键": "奖杯造", "来源": "gram", "图号": 999, "操作人": "owner-hub"},
        {"行": 2, "词": CORE_LONG, "词键": CORE_LONG, "来源": "core", "操作人": "owner-hub"},
        {"行": 7, "词": CORE_LONG, "词键": CORE_LONG, "来源": "core", "操作人": "owner-hub"},
        {"行": 99, "词": WORD, "词键": WORD, "来源": "core", "操作人": "owner-hub"},
        {"行": 2, "词": "香水", "词键": "香水", "来源": "gram", "操作人": "owner-hub"},
    ]}
    d = seed_task(TASK_CORE, "awaiting_approval", BRIDGE_CORE, prompt=PROMPT_CORE, whitelist=wl)
    item = residue_of(selfcheck.run_selfcheck(d))
    ledger = {(e["行"], e.get("词键")): e for e in ledger_of(item)}
    ok = True
    ok &= check("[5] 台账 5 条全在（不静默丢条目）", len(ledger_of(item)) == 5, str(len(ledger_of(item))))
    ok &= check("[5] 图号 999 的旧条目仍 生效（图号不参与判定）",
                ledger.get((2, "奖杯造"), {}).get("状态") == "生效", json.dumps(ledger.get((2, "奖杯造")),
                                                                           ensure_ascii=False))
    states = [e.get("状态") for e in ledger_of(item)]
    for want in ("生效", "失效-正文无此词", "失效-行没绑图", "失效-行不存在", "未参与-不在词表"):
        ok &= check(f"[5] 台账状态覆盖：{want}", want in states, str(states))
    ok &= check("[5] 失效条目一律不自动删（任务文件还是 5 条）",
                len((task_json(d).get(selfcheck.WHITELIST_FIELD) or {}).get("条目") or []) == 5)
    ok &= check("[5] 失效条目在证据里也写明（不许静默）", "个豁免条目已失效" in str(item.get("证据")),
                str(item.get("证据"))[:240])
    ok &= check("[5] 未参与-不在词表的条目说清「无害」",
                "无害" in next((e.get("原因") or "") for e in ledger_of(item)
                              if e.get("状态") == "未参与-不在词表"), "")
    ok &= check("[5] 豁免一条 gram 后同一句的其它词照报（子串不传播，B11）",
                len(words_of(item)) >= 1 and all(w != "奖杯造" for w in words_of(item)),
                str(words_of(item)))
    drop(TASK_CORE, remove_all=True)
    return ok


# ---------------------------------------------------------------- [6] 状态守卫

GUARD_STATES = ("ingested", "discussing", "awaiting_assets", "rendering", "approved", "generating", "done", "failed")


def state_guard_phase() -> bool:
    print("[6] 状态守卫：只有 awaiting_approval 能改，其余八态 409 + 人话 + 零副作用")
    seed_bridge(BRIDGE_GUARD, CK_ONE, TASK_GUARD)
    ok = True
    drawer = (WEB / "src/pages/hub/task-detail-drawer.tsx").read_text(encoding="utf-8")
    for st in GUARD_STATES:
        lid = f"{TASK_GUARD}-{st}"
        d = seed_task(lid, st, BRIDGE_GUARD, prompt=PROMPT_ONE)
        before = (d / "任务.json").read_text(encoding="utf-8")
        status, msg = call_err(lambda lid=lid: add(lid, 1, WORD))
        ok &= check(f"[6] state={st} → 409 + 人话", status == 409 and bool(msg), f"{status} {msg}")
        ok &= check(f"[6] state={st} 拒绝后任务文件零变化（拒绝不带副作用）",
                    (d / "任务.json").read_text(encoding="utf-8") == before)
        named = re.findall(r"「([^」]+)」", msg)
        missing = [w for w in named if w not in drawer]
        ok &= check(f"[6] state={st} 文案点名的出口真存在：{named}", not missing, f"缺 {missing}")
    return ok


# ---------------------------------------------------------------- [7] 上限 / 撤销语义

def limit_phase() -> bool:
    print("[7] 单任务 50 条上限（V5）+ 撤销不存在的条目 → 404")
    seed_bridge(BRIDGE_LIMIT, CK_ONE, TASK_LIMIT)
    entries = [{"行": 1, "词": f"占位词{i:02d}", "词键": f"占位词{i:02d}", "来源": "gram",
                "操作人": "owner-hub"} for i in range(selfcheck.WHITELIST_MAX)]
    wl = {"版本": 1, "更新于": iso(), "条目": entries}
    d = seed_task(TASK_LIMIT, "awaiting_approval", BRIDGE_LIMIT, prompt=PROMPT_ONE, whitelist=wl)
    ok = True
    status, msg = call_err(lambda: add(TASK_LIMIT, 1, WORD))
    ok &= check(f"[7] 已 {selfcheck.WHITELIST_MAX} 条时再加 → 409 人话（触顶=口径有病的信号）",
                status == 409 and "上限" in msg and "别用白名单兜底" in msg, f"{status} {msg}")
    ok &= check("[7] 触顶后条目数不变（没写半条）",
                len((task_json(d).get(selfcheck.WHITELIST_FIELD) or {}).get("条目") or [])
                == selfcheck.WHITELIST_MAX)

    seed_bridge(BRIDGE_ONE, CK_ONE, TASK_ONE)
    d1 = seed_task(TASK_ONE, "awaiting_approval", BRIDGE_ONE, prompt=PROMPT_ONE)
    status, msg = call_err(lambda: drop(TASK_ONE, 1, WORD))
    ok &= check("[7] 撤销本来不存在的条目 → 404 人话（不静默成功）", status == 404 and "本来就不在" in msg,
                f"{status} {msg}")
    ok &= check("[7] 撤销失败后任务文件零变化",
                selfcheck.WHITELIST_FIELD not in task_json(d1))
    return ok


# ---------------------------------------------------------------- [8] 不重渲染

def no_render_phase() -> bool:
    print("[8] 不重渲染：全流程零渲染请求，正文/审批快照/状态一字不变")
    seed_bridge(BRIDGE_ONE, CK_ONE, TASK_ONE)
    d = seed_task(TASK_ONE, "awaiting_approval", BRIDGE_ONE, prompt=PROMPT_ONE)
    task = state.load(d)
    task["approval_snapshot"] = "快照-毒"
    state.save(d, task)
    ok = True
    renders_before = len(STUB.renders)
    prompt_before = (d / "提示词.md").read_text(encoding="utf-8")
    add(TASK_ONE, 1, WORD)
    drop(TASK_ONE, remove_all=True)
    add(TASK_ONE, 1, WORD)
    ok &= check("[8] 加白/撤销全程零渲染请求（豁免不改任何渲染输入）",
                len(STUB.renders) == renders_before, f"新增 {len(STUB.renders) - renders_before} 次")
    ok &= check("[8] 提示词正文一字不变", (d / "提示词.md").read_text(encoding="utf-8") == prompt_before)
    task2 = state.load(d)
    ok &= check("[8] 审批快照一字不变", task2.get("approval_snapshot") == "快照-毒",
                str(task2.get("approval_snapshot")))
    ok &= check("[8] 状态仍是 awaiting_approval（不迁移）", task2["state"] == "awaiting_approval")
    drop(TASK_ONE, remove_all=True)
    return ok


# ---------------------------------------------------------------- [9] HTTP 路由

def route_phase() -> bool:
    """真起隔离实例，走**线上那条路由**（server.py 的载荷解析 + 方法分流）。"""
    print("[9] HTTP 路由冒烟：POST / DELETE /api/hub/task/<id>/selfcheck-whitelist")
    seed_bridge(BRIDGE_ROUTE, CK_TWO, TASK_ROUTE)
    d = seed_task(TASK_ROUTE, "awaiting_approval", BRIDGE_ROUTE, prompt=PROMPT_TWO)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env = dict(os.environ, WANREPRO_AUTO_DIR=str(TMP_PROJECTS),
               WANREPRO_ANALYSIS_ROOT=str(TMP_ANALYSIS), WANREPRO_BRIDGE=STUB.base)
    proc = subprocess.Popen([sys.executable, str(ROOT / "ui" / "server.py"), "--port", str(port),
                             "--no-browser"], cwd=str(ROOT), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace")
    ok = True
    try:
        where: dict = {}
        deadline = time.time() + 90
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            try:
                s, payload = http_req(base, "/api/hub/where", timeout=5)
            except OSError:
                time.sleep(0.7)
                continue
            if s == 200 and isinstance(payload, dict):
                where = payload
                break
            time.sleep(0.7)
        ok &= check("[9] 隔离实例起来了并自述数据根",
                    bool(where) and Path(str(where.get("tasks_dir"))).resolve() == TMP_PROJECTS.resolve(),
                    json.dumps(where, ensure_ascii=False)[:200])
        if not where:
            return False

        body = json.dumps({"行": [6, 8], "词": WORD}, ensure_ascii=False).encode("utf-8")
        s1, d1 = http_req(base, f"/api/hub/task/{TASK_ROUTE}/selfcheck-whitelist", "POST", body,
                          "application/json")
        item = residue_of((d1 or {}).get("自检") or []) if isinstance(d1, dict) else {}
        ok &= check("[9] POST 加白走线上路由（200 + 回显自检已 PASS）", s1 == 200 and item.get("结论") == "PASS",
                    f"{s1} {str(d1)[:240]}")
        ok &= check("[9] POST 回显带 selfcheck_whitelist（条目 2 条、行 6/8）",
                    isinstance(d1, dict) and len(((d1.get("selfcheck_whitelist") or {}).get("条目") or [])) == 2
                    and sorted(e.get("行") for e in d1["selfcheck_whitelist"]["条目"]) == [6, 8],
                    str(((d1 or {}).get("selfcheck_whitelist") if isinstance(d1, dict) else ""))[:200])

        s2, d2 = http_req(base, f"/api/hub/task/{TASK_ROUTE}")
        detail = (d2 or {}).get("task") if isinstance(d2, dict) else None
        ok &= check("[9] GET 详情透出 selfcheck_whitelist（面板不依赖自检.json 是否最新）",
                    s2 == 200 and isinstance(detail, dict)
                    and isinstance(detail.get("selfcheck_whitelist"), dict)
                    and len(detail["selfcheck_whitelist"].get("条目") or []) == 2,
                    str((detail or {}).get("selfcheck_whitelist"))[:200])

        s3, d3 = http_req(base, f"/api/hub/task/{TASK_ROUTE}/selfcheck-whitelist", "DELETE", body,
                          "application/json")
        ok &= check("[9] DELETE 撤销（200 + 移除=2 + 自检回到 FAIL）",
                    s3 == 200 and isinstance(d3, dict) and d3.get("移除") == 2
                    and residue_of(d3.get("自检") or []).get("结论") == "FAIL",
                    f"{s3} {str(d3)[:240]}")
        s4, d4 = http_req(base, f"/api/hub/task/{TASK_ROUTE}")
        detail4 = (d4 or {}).get("task") if isinstance(d4, dict) else None
        ok &= check("[9] 撤销后详情 selfcheck_whitelist 为空",
                    s4 == 200 and isinstance(detail4, dict) and not detail4.get("selfcheck_whitelist"),
                    str((detail4 or {}).get("selfcheck_whitelist"))[:120])

        bad = json.dumps({"行": 6, "词": "香水"}, ensure_ascii=False).encode("utf-8")
        s5, d5 = http_req(base, f"/api/hub/task/{TASK_ROUTE}/selfcheck-whitelist", "POST", bad,
                          "application/json")
        ok &= check("[9] 2 字词走线上 → 400 人话（路由没绕过校验）",
                    s5 == 400 and "3" in json.dumps(d5, ensure_ascii=False), f"{s5} {str(d5)[:160]}")
        s6, d6 = http_req(base, f"/api/hub/task/{TASK_ROUTE}/selfcheck-whitelist", "POST",
                          json.dumps({"词": WORD}, ensure_ascii=False).encode("utf-8"), "application/json")
        ok &= check("[9] 缺「行」→ 400 人话", s6 == 400 and "行" in json.dumps(d6, ensure_ascii=False),
                    f"{s6} {str(d6)[:160]}")
        s7, d7 = http_req(base, f"/api/hub/task/{TASK_ROUTE}/selfcheck-whitelist", "DELETE",
                          json.dumps({"全部": True}, ensure_ascii=False).encode("utf-8"),
                          "application/json")
        ok &= check("[9] DELETE {全部:true} 空清单也不炸（移除=0、200）",
                    s7 == 200 and isinstance(d7, dict) and d7.get("移除") == 0, f"{s7} {str(d7)[:160]}")
        ok &= check("[9] 线上路由全程零渲染", not STUB.renders, str(STUB.renders[:2]))
    finally:
        proc.terminate()
        try:
            proc.wait(15)
        except subprocess.TimeoutExpired:
            proc.kill()
    return ok


# ---------------------------------------------------------------- [10] 前端

def frontend_phase() -> bool:
    print("[10] 前端联动：node scripts/probe-at22-e4.mjs")
    states_path = TMP_ROOT / "states.json"
    states_path.write_text(json.dumps(sorted(state.STATES), ensure_ascii=False), encoding="utf-8")
    cmd = ["node", "scripts/probe-at22-e4.mjs", "--states", str(states_path)]
    proc = subprocess.run(cmd, cwd=str(WEB), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=900)
    tail = "\n".join(line for line in (proc.stdout or "").splitlines() if "FAIL" in line)
    ok = check("[10] 前端探针全绿（起态门/接线/真渲染）", proc.returncode == 0,
               tail[-800:] or (proc.stderr or "")[-400:])
    for line in (proc.stdout or "").splitlines():
        if line.strip().startswith("[PASS]"):
            print(f"     {line.strip()}")
    return ok


def main() -> int:
    print(f"[probe-at22-e4] 隔离根 {TMP_ROOT}")
    print(f"[probe-at22-e4] 假 bridge {STUB.base}")
    ok = True
    STUB.__enter__()
    try:
        ok &= isolation_phase()
        ok &= wordkey_phase()
        ok &= lifecycle_phase()
        ok &= row_scope_phase()
        ok &= guard_write_phase()
        ok &= ledger_phase()
        ok &= state_guard_phase()
        ok &= limit_phase()
        ok &= no_render_phase()
        ok &= route_phase()
        ok &= frontend_phase()
    finally:
        STUB.__exit__(None, None, None)
        shutil.rmtree(TMP_ROOT, ignore_errors=True)

    print("[11] 真实目录只读 + 临时根删净")
    after = {"projects": snapshot(REAL_PROJECTS), "analysis": snapshot(REAL_ANALYSIS)}
    for name in ("projects", "analysis"):
        lost = sorted(BEFORE[name] - after[name])
        ok &= check(f"[11] 真实 {name} 没有条目消失（不写真实目录）", not lost, f"丢了 {lost}")
    ok &= check("[11] 临时根已删净", not TMP_ROOT.exists(), str(TMP_ROOT))

    passed = sum(1 for _, good in RESULTS if good)
    total = len(RESULTS)
    print(f"\n[probe-at22-e4] {passed}/{total} PASS")
    return 0 if (ok and passed == total) else 1


if __name__ == "__main__":
    raise SystemExit(main())
