#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AT-22 E3 探针：**自检异常 UI 内闭环**的全编排链断言（写类，必须 --isolated）。

跑法（wanrepro/ 下）：`python automation/probe_at22_e3.py --isolated`，退出码 0 = 全绿。
**不带 `--isolated` 一律拒跑**：本探针会写任务/清单/提示词/自检/门禁，只在临时根内跑。

判据（卡面 AT22-B3 第 4 条 + 方案 §3/§5，一条一条对）：

  [0] 隔离生效：任务根 / 清单根 / 门禁根 / 拆解根四处都在临时根内，且渲染打的是**进程内假 bridge**
      （真 bridge 端口一次都不许碰——碰了就会读到真实任务）。
  [1] 待置换区（awaiting_assets）只写清单：第 8 列变了、状态不变、**零渲染请求**，
      回显 `重渲染.做了=false` + 一句「确认置换时按新描述重渲染」。
  [2] 待审批（awaiting_approval）一次请求跑完全编排链，五个锚点各断言一次：
      ① 清单第 8 列 = 新描述（写进盘）；
      ② 提示词重出：假渲染器**在被调用的那一刻**读到的清单里已经有新描述（次序证明），
         且落盘正文含新描述、不含该行原物核心词；
      ③ 审批快照重算：新快照 ≠ 旧快照，且 == `state.snapshot_for(新任务, 新正文)`（不自证，同源函数复算）；
      ④ 自检重跑：毒标记（预置的伪造条目）被整份覆盖，且「绑图行残留」由 FAIL → PASS
         （= 这一行的异常真被这次编辑解掉了，不是只把文件时间戳动了一下）；
      ⑤ 音频门禁重跑：毒化的旧 `audio_gate` 被新结果替换（不是沿用旧判定）。
  [3] 幂等与乐观锁：同值再写 → `unchanged=true`、零渲染、清单零变化；
      `expect_desc` 与盘上现值不符 → 409 人话，且清单零变化。
  [4] 状态守卫矩阵：done/approved/generating/rendering/ingested 一律 409 + 人话，
      且**清单与提示词零变化**（拒绝不带副作用）；文案点名的出口必须真实存在（前端源码对账）。
  [5] 旧数据兼容 + 组联动：7 列老清单写描述 → 补出第 8 列表头、行可读回；
      确认组（同实体确认 [[1,3]]）改一行 → 整组同一个描述（不出现「一张图两个描述」）。
  [6] 并发写零丢失（承接 B0 探针模式）：描述写者与图片写者交错 → 行数不变、每行可解析、
      两边的写都在盘上；无锁对照必须丢（证明本断言有灵敏度）。
  [7] 前端联动：`node scripts/probe-at22-e3.mjs` 全绿——起态门表覆盖全部状态、按钮与文案接线、
      真渲染（PromptSection 行内入口开/关、HubTextEditor 控件与字数），且新增文案零禁用词。
  [8] 真实目录只读：真实 projects/analysis 跑前跑后零变化；临时根跑完删净。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]                       # wanrepro/
WEB = ROOT.parent / "web"

if "--isolated" not in sys.argv:
    print("[probe-at22-e3] 拒跑：本探针会写任务/清单/提示词/自检/门禁，必须带 --isolated。\n"
          "  例：python automation/probe_at22_e3.py --isolated")
    raise SystemExit(2)

REAL_PROJECTS = ROOT / "automation" / "projects"
REAL_ANALYSIS = ROOT / "analysis" / "reverse"


def snapshot(root: Path) -> set[str]:
    try:
        return {p.name for p in root.iterdir()}
    except OSError:
        return set()


BEFORE = {"projects": snapshot(REAL_PROJECTS), "analysis": snapshot(REAL_ANALYSIS)}

TMP_ROOT = Path(tempfile.mkdtemp(prefix="probe-at22-e3-"))
TMP_PROJECTS = TMP_ROOT / "projects"
TMP_ANALYSIS = TMP_ROOT / "analysis"
TMP_PROJECTS.mkdir(parents=True, exist_ok=True)
TMP_ANALYSIS.mkdir(parents=True, exist_ok=True)
os.environ["WANREPRO_AUTO_DIR"] = str(TMP_PROJECTS)
os.environ["WANREPRO_ANALYSIS_ROOT"] = str(TMP_ANALYSIS)

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ui"))

# 假 bridge 必须在 `automation.config` 被 import 之前定下基址（config.BRIDGE_BASE 是 import 期常量）。
# probe_guard 只依赖标准库，先单独 import 它，再拿它选到的端口写 env。
from automation.probe_guard import StubBridge, free_port, http_req  # noqa: E402

RENDER_SEEN: list[dict] = []        # 每次被调用的那一刻：读了哪份清单、清单里这一行的描述是什么


def _stub_render(path: str) -> str:
    """进程内假渲染器：**真的去读那一刻的清单**，把第 8 列（空则用「模型看到什么」）写进正文。

    这不是在模拟真渲染器的模板（红线 14：不假设模板格式），只用来证明**次序**：
    渲染请求发生在清单写之后，且正文里出现的是新描述、不含该行原物核心词。
    """
    bridge = path.split("/api/render/", 1)[-1].split("/")[0].split("?")[0]
    md_path = TMP_ANALYSIS / bridge / LIST_NAME_GUESS
    text = md_path.read_text(encoding="utf-8-sig") if md_path.is_file() else ""
    rows = _rows_of(text)
    RENDER_SEEN.append({
        "path": path,
        "rows": {r["no"]: (r.get("my_desc") or "") for r in rows},
        "has_new": None,                     # 由断言侧按各自期望值填（见 phase 2）
    })
    lines = ["# 提示词（探针假渲染器：按清单第 8 列出正文）", ""]
    for row in rows:
        desc = (row.get("my_desc") or row.get("desc") or "").strip()
        if not desc:
            continue
        lines.append(f"0-2秒，中景，{desc}静置桌面，镜头缓慢推近。")
        lines.append("")
    lines.append("全程使用参考图中的@图片1，光线从右后方打进来，画面保持通透，"
                 "镜头运动平稳，不要快速摇移，不要字幕，不要水印，不要额外文字。")
    lines.append("")
    lines.append("结尾三秒定格在产品正面，浅灰背景，柔和高光，产品细节清晰可辨，"
                 "不要出现原片中的品牌字样，不要出现原片的包装配色。")
    return "\n".join(lines)


STUB = StubBridge(render_fn=_stub_render)
os.environ["WANREPRO_BRIDGE"] = STUB.base

from automation import cards, config, frames, generate, replace, selfcheck, state  # noqa: E402
import hub_api  # noqa: E402

cards.ANALYSIS = TMP_ANALYSIS
frames.ANALYSIS = TMP_ANALYSIS

LIST_NAME_GUESS = replace.LIST_NAME
RESULTS: list[tuple[str, bool]] = []
TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6300010000050001a5f645400000000049454e44ae426082")
BRIDGE = "probe-e3"
TASK = "auto-9801"
TASK_ASSETS = "auto-9802"
TASK_LEGACY = "auto-9803"
TASK_GROUP = "auto-9804"
TASK_GUARD = "auto-9805"

ORIGIN_ROW1 = "细长圆柱体紫色表面，顶部为银色金属圆柱盖"
ORIGIN_ROW1_CORE = "细长圆柱体紫色表面"
NEW_DESC = "客户款 双层玻璃香水瓶，磨砂瓶身"
GROUP_DESC = "客户款 玻璃瓶（整组同款）"

CK = """# 待确认清单 · {bridge}

| # | 类别 | 模型看到什么 | 命名 | 资产图 | 锁什么 | 建议 | 用我的描述 |
|---|------|-------------|------|--------|--------|------|------------|
| 1 | 主体 | {o1} |  | {img1} | 外形比例 | 必须换 |  |
| 3 | 主体 | 另一角度的磨砂瓶身，浅灰背景 |  |  | 外形比例 | 必须换 |  |
| 6 | 道具 | 紫色圆柱管身配亮银色金属盖的香水喷雾瓶 |  |  | 颜色、支点位置 | 建议准备 |  |
| 12 | 品牌视觉（置换位） | 参考片品牌位 标语品牌字：「POP!」｜红色无衬线粗体大字 |  |  | 客户品牌字样（待填） | 必须换 |  |
"""

CK_LEGACY = """# 待确认清单 · {bridge}

| # | 类别 | 模型看到什么 | 命名 | 资产图 | 锁什么 | 建议 |
|---|------|-------------|------|--------|--------|------|
| 1 | 主体 | {o1} |  |  | 外形比例 | 必须换 |
| 6 | 道具 | 紫色圆柱管身配亮银色金属盖的香水喷雾瓶 |  |  | 颜色、支点位置 | 建议准备 |
"""

GATE_PASS = {
    "状态": "通过",
    "轮次": 1,
    "上限": 2,
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


def _rows_of(md_text: str) -> list[dict]:
    return replace.parse_rows(md_text)


def ref(local_id: str, name: str) -> str:
    return f"local:{local_id}/asset-uploads/{name}"


def _row_cells(line: str) -> list[str]:
    return replace.row_cells(line)


def ck_text(bridge: str, *, legacy: bool = False) -> str:
    tpl = CK_LEGACY if legacy else CK
    return tpl.format(bridge=bridge, o1=ORIGIN_ROW1, img1=ref(TASK, "row1.png"))


def seed_bridge(bridge: str = BRIDGE, *, legacy: bool = False, groups: list[list[int]] | None = None) -> None:
    bd = TMP_ANALYSIS / bridge
    bd.mkdir(parents=True, exist_ok=True)
    (bd / LIST_NAME_GUESS).write_text(ck_text(bridge, legacy=legacy), encoding="utf-8")
    (bd / "拆解.md").write_text((ROOT / "tests" / "fixtures" / "at11-m1" / "拆解.md").read_text(
        encoding="utf-8-sig"), encoding="utf-8")
    (bd / "门禁.json").write_text(json.dumps(GATE_PASS, ensure_ascii=False, indent=2), encoding="utf-8")
    if groups:
        (bd / "待确认清单.json").write_text(
            json.dumps({"同实体确认": groups}, ensure_ascii=False, indent=2), encoding="utf-8")


def seed_task(local_id: str, st: str, bridge: str = BRIDGE, *, prompt: str | None = None) -> Path:
    d = TMP_PROJECTS / local_id
    d.mkdir(parents=True, exist_ok=True)
    task = {"local_id": local_id, "state": st, "bridge_id": bridge,
            "created_at": iso(-600), "intent": "AT-22 E3 探针夹具（隔离根内）",
            "history": [{"at": iso(-600), "from": "discussing", "to": st, "note": "夹具"}],
            "audio_mode": "保留原声",
            "assets_confirmed": [{"no": 1, "url": ref(local_id, "row1.png")}]}
    (d / "任务.json").write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
    if prompt is not None:
        (d / "提示词.md").write_text(prompt, encoding="utf-8")
    # 清单「资产图」列引用的本地图必须真在盘上（确认链会按引用取图；假图 = 假绿）
    up = d / "asset-uploads"
    up.mkdir(parents=True, exist_ok=True)
    for name in ("row1.png", "p0.png", "p1.png", "p2.png", "p3.png", "p4.png", "p5.png"):
        (up / name).write_bytes(TINY_PNG)
    return d


def md_of(bridge: str = BRIDGE) -> str:
    return (TMP_ANALYSIS / bridge / LIST_NAME_GUESS).read_text(encoding="utf-8")


def set_desc(local_id: str, n: int, desc: str, **kw) -> dict:
    return hub_api.set_asset(local_id, n, desc=desc, **kw)


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
    print("[0] 隔离生效：任务/清单/门禁/拆解根都在临时根内，渲染只打进程内假 bridge")
    ok = True
    ok &= check("清单根在临时根内", under_tmp(replace.analysis_root()), str(replace.analysis_root()))
    ok &= check("hub 链任务根在临时根内", under_tmp(hub_api.TASKS_DIR), str(hub_api.TASKS_DIR))
    ok &= check("配置任务根在临时根内", under_tmp(config.PROJECTS_DIR), str(config.PROJECTS_DIR))
    ok &= check("拆解根（cards.ANALYSIS）在临时根内", under_tmp(cards.ANALYSIS), str(cards.ANALYSIS))
    ok &= check("bridge 基址是本进程内的假 bridge（不是 8776/8779 真桥）",
                config.BRIDGE_BASE == STUB.base and str(STUB.port) not in ("8776", "8779"),
                f"{config.BRIDGE_BASE} vs stub {STUB.base}")
    return ok


# ---------------------------------------------------------------- [1] 待置换区只写清单

def assets_state_phase() -> bool:
    print("[1] awaiting_assets：只写清单第 8 列，零渲染")
    seed_bridge()
    seed_task(TASK_ASSETS, "awaiting_assets")
    ok = True
    before_render = len(STUB.renders)
    before_md = md_of()
    echo = set_desc(TASK_ASSETS, 1, NEW_DESC)
    after_md = md_of()
    row = next(r for r in _rows_of(after_md) if r["no"] == 1)
    ok &= check("第 8 列写进盘（读回与输入逐字一致）", row["my_desc"] == NEW_DESC, row["my_desc"])
    ok &= check("其它列一格不动（第 3/6/12 行原文不变）",
                all(b == a for b, a in zip(
                    [ln for ln in before_md.splitlines() if ln.startswith("| 6") or ln.startswith("| 12")],
                    [ln for ln in after_md.splitlines() if ln.startswith("| 6") or ln.startswith("| 12")])))
    ok &= check("状态不变（仍 awaiting_assets）", state.load(TMP_PROJECTS / TASK_ASSETS)["state"] == "awaiting_assets")
    ok &= check("零渲染请求（这一态不白跑一次渲染）", len(STUB.renders) == before_render,
                f"新增 {len(STUB.renders) - before_render} 次")
    ok &= check("回显 重渲染.做了=false + 一句「确认置换时按新描述重渲染」",
                echo.get("重渲染", {}).get("做了") is False and "确认置换" in str(echo.get("说明") or ""),
                json.dumps({k: echo.get(k) for k in ("重渲染", "说明")}, ensure_ascii=False))
    ok &= check("回显 rows 带该行新描述与已传图", any(r.get("我的描述") == NEW_DESC for r in echo.get("rows") or []),
                json.dumps(echo.get("rows"), ensure_ascii=False)[:200])
    return ok


# ---------------------------------------------------------------- [2] 全编排链

def approval_chain_phase() -> bool:
    print("[2] awaiting_approval：一次请求跑完「写清单→重出提示词→重算快照→自检重跑→门禁重跑」")
    seed_bridge()
    old_prompt = _stub_render(f"/api/render/{BRIDGE}/lumi")
    d = seed_task(TASK, "awaiting_approval", prompt=old_prompt)
    task0 = state.load(d)
    task0["approval_snapshot"] = "旧快照-毒"
    task0["audio_gate"] = {"verdict": "毒-旧一代", "发现": ["旧一代门禁结果"]}
    state.save(d, task0)
    (d / selfcheck.SELFCHECK_NAME).write_text(json.dumps(
        [{"项": "探针毒标记", "结论": "FAIL", "证据": "这一条是探针预置的伪造条目"}], ensure_ascii=False),
        encoding="utf-8")

    ok = True
    before_render = len(STUB.renders)
    RENDER_SEEN.clear()
    echo = set_desc(TASK, 1, NEW_DESC)
    task1 = state.load(d)
    md1 = md_of()
    prompt1 = (d / "提示词.md").read_text(encoding="utf-8")
    items1 = selfcheck.load_selfcheck(d)

    # ① 清单
    ok &= check("[2①] 清单第 8 列 = 新描述", next(r for r in _rows_of(md1) if r["no"] == 1)["my_desc"] == NEW_DESC)
    # ② 提示词重出（次序 + 内容）
    ok &= check("[2②] 恰好一次渲染请求（一次动作一代正文，不重渲两遍）", len(STUB.renders) - before_render == 1,
                f"新增 {len(STUB.renders) - before_render} 次")
    ok &= check("[2②] 假渲染器被调用时清单里**已有**新描述（写清单在重出提示词之前）",
                bool(RENDER_SEEN) and RENDER_SEEN[0]["rows"].get(1) == NEW_DESC,
                json.dumps(RENDER_SEEN[0]["rows"], ensure_ascii=False) if RENDER_SEEN else "无渲染记录")
    ok &= check("[2②] 落盘正文含新描述", NEW_DESC in prompt1, prompt1[:160])
    ok &= check("[2②] 落盘正文不含该行原物核心词", ORIGIN_ROW1_CORE not in prompt1,
                next((ln for ln in prompt1.splitlines() if ORIGIN_ROW1_CORE in ln), ""))
    ok &= check("[2②] 正文不是半截（>200 字，os.replace 落盘）",
                len(prompt1.strip()) > 200 and not (d / "提示词.md.tmp").exists())
    # ③ 快照
    old_snap = "旧快照-毒"
    ok &= check("[2③] 快照既不是旧毒值，也 == 同源函数对「新任务+新正文」的复算",
                task1.get("approval_snapshot") != old_snap
                and task1.get("approval_snapshot") == state.snapshot_for(task1, prompt1),
                f"{task1.get('approval_snapshot')!r} vs {state.snapshot_for(task1, prompt1)!r}")
    # ④ 自检
    names = [i.get("项") for i in items1]
    residue = next((i for i in items1 if i.get("项") == "绑图行残留"), None)
    ok &= check("[2④] 自检整份重跑（探针毒标记被覆盖，不残留）", "探针毒标记" not in names, str(names))
    ok &= check("[2④] 这一行的残留由 FAIL → PASS（异常真被这次编辑解掉）",
                residue is not None and residue.get("结论") == "PASS",
                json.dumps(residue, ensure_ascii=False)[:300] if residue else "缺该项")
    ok &= check("[2④] 回显里带这一代自检（前端就地刷新用，不另开请求再判）",
                isinstance(echo.get("自检"), list) and any(i.get("项") == "绑图行残留" for i in echo["自检"]))
    # ⑤ 音频门禁
    ok &= check("[2⑤] 音频门禁重跑（旧毒判定被替换）",
                isinstance(task1.get("audio_gate"), dict) and task1["audio_gate"].get("verdict") != "毒-旧一代",
                json.dumps(task1.get("audio_gate"), ensure_ascii=False)[:200])
    ok &= check("[2⑤] 回显带门禁与正文（面板不刷新也能立刻显示这一代）",
                isinstance(echo.get("audio_gate"), dict) and NEW_DESC in str(echo.get("prompt_md") or ""))
    # 状态与留痕
    ok &= check("[2] 回到 awaiting_approval（受控回退→确认→渲染链走完，不吊死在 rendering）",
                task1["state"] == "awaiting_approval", task1["state"])
    ok &= check("[2] 留痕 prompt_edited_at 与原因（人为改过正文这件事可查）",
                bool(task1.get("prompt_edited_at")) and "改描述" in str(task1.get("prompt_edit_reason") or ""),
                f"{task1.get('prompt_edited_at')!r} / {task1.get('prompt_edit_reason')!r}")
    ok &= check("[2] 回显里写明这次真跑了哪些阶段",
                isinstance(echo.get("重渲染", {}).get("阶段"), list)
                and len(echo["重渲染"]["阶段"]) >= 5, json.dumps(echo.get("重渲染"), ensure_ascii=False))
    return ok


# ---------------------------------------------------------------- [3] 幂等 / 乐观锁

def idempotent_phase() -> bool:
    print("[3] 幂等与乐观锁")
    ok = True
    before_render = len(STUB.renders)
    md_before = md_of()
    echo = set_desc(TASK, 1, NEW_DESC)
    ok &= check("[3] 同值再写 → unchanged=true（连点两次只产生一代正文）",
                echo.get("unchanged") is True and echo.get("重渲染", {}).get("做了") is False,
                json.dumps(echo, ensure_ascii=False)[:200])
    ok &= check("[3] 同值再写 → 零渲染、清单零变化",
                len(STUB.renders) == before_render and md_of() == md_before)
    status, msg = call_err(lambda: set_desc(TASK, 1, "另一版描述", expect_desc="盘上没有的旧值"))
    ok &= check("[3] expect_desc 与盘上不符 → 409 人话（别处改过这一行）",
                status == 409 and "改过" in msg, f"{status} {msg}")
    ok &= check("[3] 409 之后清单零变化", md_of() == md_before)
    status2, msg2 = call_err(lambda: set_desc(TASK, 1, "长" * 501))
    ok &= check("[3] 超长描述 → 400 人话（上限 500 字）", status2 == 400 and "500" in msg2, f"{status2} {msg2}")
    status3, msg3 = call_err(lambda: set_desc(TASK, 99, "不存在的行"))
    ok &= check("[3] 不存在的行号 → 404 人话", status3 == 404 and "99" in msg3, f"{status3} {msg3}")
    status4, msg4 = call_err(lambda: set_desc(TASK, 1, NEW_DESC, image_url="http://x/y.png"))
    ok &= check("[3] 与图片/命名/决策互斥 → 拒绝且不自作主张", status4 is not None and "互斥" in msg4,
                f"{status4} {msg4}")
    return ok


# ---------------------------------------------------------------- [4] 状态守卫

GUARD_STATES = ("done", "approved", "generating", "rendering", "ingested")


def guard_phase() -> bool:
    print("[4] 状态守卫：不给编辑入口的五态一律 409，且拒绝无副作用")
    seed_bridge(bridge="probe-e3g")
    ok = True
    for st in GUARD_STATES:
        lid = f"{TASK_GUARD}-{st}"
        seed_task(lid, st, bridge="probe-e3g")
        md_before = md_of("probe-e3g")
        prompt_before = (TMP_PROJECTS / lid / "提示词.md").read_text(encoding="utf-8") \
            if (TMP_PROJECTS / lid / "提示词.md").is_file() else None
        renders_before = len(STUB.renders)
        status, msg = call_err(lambda lid=lid: set_desc(lid, 1, NEW_DESC))
        ok &= check(f"[4] state={st} → 409 + 人话", status == 409 and bool(msg), f"{status} {msg}")
        ok &= check(f"[4] state={st} 拒绝后清单/提示词/渲染零变化",
                    md_of("probe-e3g") == md_before and len(STUB.renders) == renders_before
                    and ((TMP_PROJECTS / lid / "提示词.md").read_text(encoding="utf-8")
                         if (TMP_PROJECTS / lid / "提示词.md").is_file() else None) == prompt_before)
        # 文案里点名的具名出口必须真实存在（不编造按钮）
        named = re.findall(r"「([^」]+)」", msg)
        if named:
            drawer = (WEB / "src/pages/hub/task-detail-drawer.tsx").read_text(encoding="utf-8")
            missing = [w for w in named if w not in drawer]
            ok &= check(f"[4] state={st} 文案点名的出口真存在：{named}", not missing, f"缺 {missing}")
    return ok


# ---------------------------------------------------------------- [5] 旧数据 + 组联动

def legacy_and_group_phase() -> bool:
    print("[5] 旧数据兼容（7 列清单）+ 确认组整组联动")
    ok = True
    seed_bridge(bridge="probe-e3w", legacy=True)
    seed_task(TASK_LEGACY, "awaiting_assets", bridge="probe-e3w")
    set_desc(TASK_LEGACY, 1, NEW_DESC)
    md = md_of("probe-e3w")
    rows = _rows_of(md)
    ok &= check("[5] 老清单（7 列）写描述不炸，行可读回", next(r for r in rows if r["no"] == 1)["my_desc"] == NEW_DESC)
    header = next((ln for ln in md.splitlines() if "类别" in ln), "")
    ok &= check("[5] 表头补出「用我的描述」列（用户能看到列名）", "用我的描述" in header, header)
    ok &= check("[5] 没写过的老行原样不动（行 6 仍是 7 格，解析不报错）",
                len([r for r in rows if r["no"] == 6]) == 1 and next(r for r in rows if r["no"] == 6)["my_desc"] == "")

    seed_bridge(bridge="probe-e3k", groups=[[1, 3]])
    seed_task(TASK_GROUP, "awaiting_assets", bridge="probe-e3k")
    echo = set_desc(TASK_GROUP, 1, GROUP_DESC)
    gmd = md_of("probe-e3k")
    grows = {r["no"]: r["my_desc"] for r in _rows_of(gmd)}
    ok &= check("[5] 组内两行同一个描述（不出现一张图两个描述）",
                grows.get(1) == GROUP_DESC and grows.get(3) == GROUP_DESC, json.dumps(grows, ensure_ascii=False))
    ok &= check("[5] 回显带组与全组行", echo.get("组") == [1, 3] and len(echo.get("rows") or []) == 2,
                json.dumps({k: echo.get(k) for k in ("组", "rows")}, ensure_ascii=False)[:200])
    return ok


# ---------------------------------------------------------------- [6] 并发零丢失

def concurrency_phase() -> bool:
    print("[6] 并发写零丢失（描述写者 × 图片写者交错）+ 无锁对照必丢")
    seed_bridge(bridge="probe-e3c")
    seed_task(TASK_GUARD + "-c", "awaiting_assets", bridge="probe-e3c")
    lid = TASK_GUARD + "-c"
    ok = True
    errs: list[str] = []

    def desc_writer(i: int) -> None:
        try:
            hub_api.set_asset(lid, 1, desc=f"并发描述-{i}")
        except Exception as exc:              # noqa: BLE001
            errs.append(f"desc{i}: {exc}")

    def img_writer(i: int) -> None:
        try:
            replace.set_row("probe-e3c", 6, image_urls=[ref(lid, f"p{i}.png")], overwrite=True)
        except Exception as exc:              # noqa: BLE001
            errs.append(f"img{i}: {exc}")

    threads = [threading.Thread(target=desc_writer, args=(i,)) for i in range(6)]
    threads += [threading.Thread(target=img_writer, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    rows = _rows_of(md_of("probe-e3c"))
    by_no = {r["no"]: r for r in rows}
    ok &= check("[6] 12 个写者零异常", not errs, "; ".join(errs[:3]))
    ok &= check("[6] 行数不变（一条没丢）", sorted(by_no) == [1, 3, 6, 12], str(sorted(by_no)))
    ok &= check("[6] 每一行仍可解析（清单没被写坏）",
                len(rows) == 4 and all(r["cat"] for r in rows))
    ok &= check("[6] 描述写者的写落在盘上（≠空）", by_no[1]["my_desc"].startswith("并发描述-"), by_no[1]["my_desc"])
    ok &= check("[6] 图片写者的写也落在盘上（行 6 有资产图）", "p" in by_no[6]["cur_assets"], by_no[6]["cur_assets"])

    # 无锁对照：同样的读-改-写但**不持锁**，必须丢更新（证明上面的断言有灵敏度）
    md_path = TMP_ANALYSIS / "probe-e3c" / LIST_NAME_GUESS
    base = md_path.read_text(encoding="utf-8")
    lost: list[str] = []

    def racer(tag: str) -> None:
        text = md_path.read_text(encoding="utf-8")
        time.sleep(0.05)
        md_path.write_text(replace.fill_row(text, 1, [], None, desc=f"无锁-{tag}"), encoding="utf-8")

    ts = [threading.Thread(target=racer, args=(t,)) for t in ("A", "B")]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)
    final = next(r for r in _rows_of(md_path.read_text(encoding="utf-8")) if r["no"] == 1)["my_desc"]
    lost.append(final)
    ok &= check("[6] 无锁对照确实丢了一次写（断言有灵敏度，不是假绿）",
                final.count("无锁-") == 1 or final.startswith("无锁-"), f"final={final!r}（两个写者应只留一个）")
    md_path.write_text(base, encoding="utf-8")          # 复原，后续断言不吃污染
    return ok


# ---------------------------------------------------------------- [7] 前端联动

def frontend_phase() -> bool:
    print("[7] 前端联动：node scripts/probe-at22-e3.mjs")
    states_path = TMP_ROOT / "states.json"
    states_path.write_text(json.dumps(sorted(state.STATES), ensure_ascii=False), encoding="utf-8")
    cmd = ["node", "scripts/probe-at22-e3.mjs", "--states", str(states_path)]
    proc = subprocess.run(cmd, cwd=str(WEB), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=900)
    tail = "\n".join(line for line in (proc.stdout or "").splitlines() if "FAIL" in line)
    ok = check("前端探针全绿（起态门/接线/真渲染/禁用词）", proc.returncode == 0,
               tail[-800:] or (proc.stderr or "")[-400:])
    for line in (proc.stdout or "").splitlines():
        if line.strip().startswith("[PASS]"):
            print(f"     {line.strip()}")
    return ok


# ---------------------------------------------------------------- [7] HTTP 路由冒烟

def route_phase() -> bool:
    """真起一个隔离实例，走一遍 **线上那条路由**（server.py 的 /api/hub/asset 载荷解析）。

    进程内探针测不到 `server.py` 里 desc/expect_desc/rerender 的取值（键名写错时 UI 会静默失联，
    而 Python 侧与 tsc 都不会报），所以这一小段必须真打 HTTP。
    """
    print("[7] HTTP 路由冒烟：真起隔离实例，POST /api/hub/asset {desc, expect_desc}")
    seed_bridge(bridge="probe-e3r")
    lid = TASK_GUARD + "-r"
    seed_task(lid, "awaiting_assets", bridge="probe-e3r")
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
                s, d = http_req(base, "/api/hub/where", timeout=5)
            except OSError:
                time.sleep(0.7)
                continue
            if s == 200 and isinstance(d, dict):
                where = d
                break
            time.sleep(0.7)
        ok &= check("[7] 隔离实例起来了并自述数据根", bool(where) and Path(str(where.get("tasks_dir"))).resolve()
                    == TMP_PROJECTS.resolve(), json.dumps(where, ensure_ascii=False)[:200])
        if not where:
            return False
        body = json.dumps({"task_id": lid, "n": 1, "desc": "HTTP 路由描述",
                           "expect_desc": ""}, ensure_ascii=False).encode("utf-8")
        s1, d1 = http_req(base, "/api/hub/asset", "POST", body, "application/json")
        ok &= check("[7] desc 走线上路由写进清单（200 + 回显新值）",
                    s1 == 200 and isinstance(d1, dict) and d1.get("我的描述") == "HTTP 路由描述",
                    f"{s1} {str(d1)[:200]}")
        body2 = json.dumps({"task_id": lid, "n": 1, "desc": "另一版", "expect_desc": "盘上没有"},
                           ensure_ascii=False).encode("utf-8")
        s2, d2 = http_req(base, "/api/hub/asset", "POST", body2, "application/json")
        ok &= check("[7] expect_desc 不符 → 409 且错误体带人话",
                    s2 == 409 and "改过" in json.dumps(d2, ensure_ascii=False),
                    f"{s2} {str(d2)[:200]}")
        s3, d3 = http_req(base, "/api/hub/asset", "POST",
                          json.dumps({"task_id": lid, "desc": "x"}).encode("utf-8"), "application/json")
        ok &= check("[7] 缺 n → 400 人话（路由校验没被 desc 支绕过）",
                    s3 == 400 and "n" in json.dumps(d3, ensure_ascii=False), f"{s3} {str(d3)[:160]}")
    finally:
        proc.terminate()
        try:
            proc.wait(15)
        except subprocess.TimeoutExpired:
            proc.kill()
    return ok


# ---------------------------------------------------------------- [8] 真实目录只读

def main() -> int:
    print(f"[probe-at22-e3] 隔离根 {TMP_ROOT}")
    print(f"[probe-at22-e3] 假 bridge {STUB.base}")
    ok = True
    STUB.__enter__()
    try:
        ok &= isolation_phase()
        ok &= assets_state_phase()
        ok &= approval_chain_phase()
        ok &= idempotent_phase()
        ok &= guard_phase()
        ok &= legacy_and_group_phase()
        ok &= concurrency_phase()
        ok &= route_phase()
        ok &= frontend_phase()
    finally:
        STUB.__exit__(None, None, None)
        shutil.rmtree(TMP_ROOT, ignore_errors=True)

    after = {"projects": snapshot(REAL_PROJECTS), "analysis": snapshot(REAL_ANALYSIS)}
    for name in ("projects", "analysis"):
        lost = sorted(BEFORE[name] - after[name])
        ok &= check(f"[8] 真实 {name} 没有条目消失（不写真实目录）", not lost, f"丢了 {lost}")
    ok &= check("[8] 临时根已删净", not TMP_ROOT.exists(), str(TMP_ROOT))

    passed = sum(1 for _, good in RESULTS if good)
    total = len(RESULTS)
    print(f"\n[probe-at22-e3] {passed}/{total} PASS")
    return 0 if (ok and passed == total) else 1


if __name__ == "__main__":
    raise SystemExit(main())
