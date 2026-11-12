#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AT-21 B0 探针：fork 复用合同冻结 + 核心链（私有清单副本 / inbox 复制 / derived_from / 直写提示词）。

跑法（wanrepro/ 下）：`python automation/probe_at21_b0.py --isolated`，退出码 0 = 全绿。
**不带 `--isolated` 一律拒跑**（会写任务/清单/侧车，写类探针只许在隔离根内跑）。

锁的是卡面验收（`docs/审计收敛-AT21-AT22-双路裁决-20261112.md` 裁决 3 + 卡 t_cc8ea89b）：

  [0] 隔离生效：任务根 / 拆解根 / 配置根 都落在本次临时根内（伪隔离 = 写真实目录）；
  [1] 派生基本盘：done → fork 出新任务号；**源清单 SHA 不变、源任务.json 逐字节不变、
      源提示词.md 逐字节不变**；fork 目录里有私有 待确认清单.md/.json，`local:<源号>/` 已重写指自己；
      inbox 同 sha、指纹同抄、资产图字节复制；
  [2] 出生态与剥离：fork 起步 awaiting_assets、derived_from 三件齐全（来源号/bridge/时点）、
      审批/提交/生成痕迹/产物一律不在（零审批痕迹）；来源任务侧反查到派生版本（读侧反查）；
  [3] 双 fork 互不影响：fork A 改图 → A 变、B 不变、源清单 SHA 不变；
  [4] 渲染作用域（真调 render.py）：桥侧按 fork 的私有清单渲染 → 正文里是 fork 的命名/其图被
      `@图片1` 接上；共享 analysis 目录里**没有**多出提示词产物（源任务侧零污染）；
      非 fork 任务带 checklist_task → 400；跨桥 → 403；路径穿越 → 400；
  [5] 删源任务后 fork 仍可用：源视频从 fork 自己的 inbox 读得到（同 sha）、私有清单还在、
      拆解产物按 bridge 引用不断链（analysis 目录因 fork 仍引用而不删）；
  [6] 直写 提示词.md（D3-②）：正文落盘 + `prompt_edited_at` 留痕 + approval_snapshot 按新正文重算 +
      状态不变（仍需重新批准）；同文重复提交 = unchanged（不写不重算）；过短 → 400；
      非 awaiting_approval → 409 人话；
  [7] 前端联动：`node web/scripts/probe-at21-b0.mjs` 全绿（派生/手改起态门与后端状态表同序、
      归一化只搬真值、请求形状、详情页反查入口与「复用为新版本」/「手改正文」真渲染）；
  [8] 真实目录只读：真实 projects / analysis/reverse / uploads 一条都没少、夹具号没落进真实目录；
      临时根删净。

隔离手法：`--isolated` 下 `WANREPRO_AUTO_DIR` + `WANREPRO_ANALYSIS_ROOT` 指向临时根（在 import
hub_api / server / automation 之前设）；桥地址指向死端口（本探针不连桥）；`render.py` 用 `--id <绝对路径>`
与 `--out-dir/--checklist-dir <临时根>` 直调，因此产物全落临时根。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]                    # wanrepro/

if "--isolated" not in sys.argv:
    print("[probe-at21-b0] 拒跑：本探针会写任务/清单/侧车，必须带 --isolated。\n"
          "  例：python automation/probe_at21_b0.py --isolated")
    raise SystemExit(2)

REAL_PROJECTS = ROOT / "automation" / "projects"
REAL_ANALYSIS = ROOT / "analysis" / "reverse"
REAL_UPLOADS = ROOT / "uploads"


def snapshot(root: Path) -> set[str]:
    try:
        return {p.name for p in root.iterdir()}
    except OSError:
        return set()


BEFORE = {"projects": snapshot(REAL_PROJECTS), "analysis": snapshot(REAL_ANALYSIS),
          "uploads": snapshot(REAL_UPLOADS)}

TMP_ROOT = Path(tempfile.mkdtemp(prefix="probe-at21-b0-"))
TMP_PROJECTS = TMP_ROOT / "projects"
TMP_ANALYSIS = TMP_ROOT / "_analysis"
TMP_PROJECTS.mkdir(parents=True, exist_ok=True)
TMP_ANALYSIS.mkdir(parents=True, exist_ok=True)
os.environ["WANREPRO_AUTO_DIR"] = str(TMP_PROJECTS)           # 必须在 import 之前
os.environ["WANREPRO_ANALYSIS_ROOT"] = str(TMP_ANALYSIS)
os.environ["WANREPRO_BRIDGE"] = "http://127.0.0.1:9"          # 本探针不连桥
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ui"))

import hub_api  # noqa: E402
from automation import cards, fork, frames, generate, replace, state  # noqa: E402

RESULTS: list[tuple[str, bool]] = []
LIST_NAME = replace.LIST_NAME
SIDECAR = replace.SIDECAR_NAME
FIXTURES = ("auto-7001", "auto-7002", "auto-7003", "auto-7004", "auto-7005", "auto-7006")
HEADER = ("| # | 类别 | 模型看到什么 | 命名 | 资产图 | 锁什么 | 建议 | 用我的描述 |",
          "| --- | --- | --- | --- | --- | --- | --- | --- |")
# 真拆解正文（读一份现网产物当夹具；只读不写，找不到就让渲染相位降级为「如实未覆盖」）
DISC_SRC = REAL_ANALYSIS / "3" / "拆解.md"
# 本地引用串只认「local:任务号/asset-uploads/16位hex.png」（hub 侧格式校验），夹具就用合法名
ASSET_A = "00000000000000a1.png"
ASSET_B = "00000000000000b2.png"


def check(name: str, cond, detail: str = "") -> bool:
    ok = bool(cond)
    RESULTS.append((name, ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail and not ok else ""))
    return ok


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ── 夹具 ────────────────────────────────────────────────────────────────
def seed_source(lid: str, *, bridge: str, rows: int = 4, video_name: str = "") -> dict:
    """隔离根里造一条**已出片**任务（done）：inbox 源片 + uploads 指纹 + 共享清单 + 侧车 + 产物。"""
    video_name = video_name or f"{lid}-源片.mp4"
    d = TMP_PROJECTS / lid
    (d / "inbox").mkdir(parents=True, exist_ok=True)
    (d / "asset-uploads").mkdir(parents=True, exist_ok=True)
    video = d / "inbox" / video_name
    video.write_bytes(b"AT21B0FAKEVIDEO" * 2048)             # 内容只求同 sha，不求能解码
    (d / "asset-uploads" / ASSET_A).write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 64)
    (d / "提示词.md").write_text(f"# 源任务 {lid} 的提示词正文\n\n" + "源正文。" * 120, encoding="utf-8")
    (d / "成片.mp4").write_bytes(b"FINAL-OUTPUT" * 100)

    bd = TMP_ANALYSIS / bridge
    bd.mkdir(parents=True, exist_ok=True)
    if DISC_SRC.is_file():
        shutil.copy2(DISC_SRC, bd / "拆解.md")
    ref = f"local:{lid}/asset-uploads/{ASSET_A}"
    lines = [f"# 待确认清单 · {bridge}", "", *HEADER]
    for n in range(1, rows + 1):
        asset = ref if n in (1, 3) else ""
        advice = "必须换" if n == 1 else "可沿用"
        lines.append(f"| {n} | 主体 | 描述{n}·源任务版本 | 源命名{n} | {asset} | 光影 | {advice} |  |")
    (bd / LIST_NAME).write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
    (bd / SIDECAR).write_text(json.dumps(
        {"同实体确认": [[3, 4]], "行决策": {"2": "keep_origin"},
         "组元数据": {"3,4": {"主行": 3, "组名": "组·源"}}},
        ensure_ascii=False, indent=2), encoding="utf-8")
    (bd / ".源视频指纹.json").write_text(json.dumps(
        {"路径": str(video), "大小": video.stat().st_size, "前1MB_sha256": _first_mb(video),
         "记录时间": "2026-11-13 00:00:00"}, ensure_ascii=False, indent=2), encoding="utf-8")

    task = {"local_id": lid, "bridge_id": bridge, "created_at": "2026-11-13T00:00:00+08:00",
            "state": "done", "intent": "AT-21 B0 探针源任务（隔离根内）",
            "audio_mode": "沿用原片", "audio_mode_依据": "夹具",
            "gen_params": {"时长": 5}, "size_input": "16:9",
            "assets": [f"{lid[-4:]}aaaa.png"],
            "ingestion": {"video_name": video_name, "source_name": video_name,
                          "zip_sha256": "f" * 64},
            "pending_replace": [{"no": 1, "img": "a.png", "url": ref, "name": "源命名1", "desc": "描述1"}],
            "approval_snapshot": "deadbeef" * 8, "approved_at": "2026-11-13T01:00:00+08:00",
            "gen_channel": {"渠道": "夹具渠道"}, "gen": {"task_id": "cloud-123"},
            "submit_unknown": None,
            "history": [{"at": "2026-11-13T00:00:00+08:00", "from": None, "to": "done",
                         "note": "夹具：已完成"}]}
    (d / "任务.json").write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"dir": d, "bridge": bd, "video": video, "task": task}


def _first_mb(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        h.update(fh.read(1024 * 1024))
    return h.hexdigest()


def set_state(lid: str, state_name: str) -> None:
    f = TMP_PROJECTS / lid / "任务.json"
    task = json.loads(f.read_text(encoding="utf-8"))
    task["state"] = state_name
    f.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")


def under_tmp(p) -> bool:
    try:
        return Path(p).resolve().is_relative_to(TMP_ROOT.resolve())
    except (OSError, AttributeError):
        return False


# ── [0] 隔离 ────────────────────────────────────────────────────────────
def isolation_phase() -> bool:
    print("[0] 隔离生效：任务根/拆解根/配置根都在临时根内")
    ok = True
    ok &= check("hub 任务根在临时根内", under_tmp(hub_api.TASKS_DIR), str(hub_api.TASKS_DIR))
    ok &= check("拆解产物根在临时根内（replace.analysis_root）", under_tmp(replace.analysis_root()),
                str(replace.analysis_root()))
    ok &= check("配置任务根在临时根内", under_tmp(fork.config.PROJECTS_DIR),
                str(fork.config.PROJECTS_DIR))
    ok &= check("hub 拆解根与 replace 同源", under_tmp(hub_api._analysis_reverse()),
                str(hub_api._analysis_reverse()))
    return ok


# ── [1][2] 派生基本盘 + 出生态/剥离 ───────────────────────────────────────
def fork_phase() -> dict:
    print("[1] 派生基本盘：源任务只读 + 私有副本齐全 + 引用重写 + inbox/指纹/资产图")
    src = seed_source("auto-7001", bridge="br-21a", rows=4)
    lid = "auto-7001"
    before = {"md": sha(src["bridge"] / LIST_NAME), "side": sha(src["bridge"] / SIDECAR),
              "task": sha(src["dir"] / "任务.json"), "prompt": sha(src["dir"] / "提示词.md"),
              "video": sha(src["video"]), "bridge_files": sorted(p.name for p in src["bridge"].iterdir())}
    out = hub_api.fork_task(lid)
    new_id = str(out.get("local_id") or "")
    nd = TMP_PROJECTS / new_id
    ok = True
    ok &= check("[1] 派生返回新任务号且不是源任务", new_id.startswith("auto-") and new_id != lid, new_id)
    ok &= check("[1] 源清单 SHA 不变", sha(src["bridge"] / LIST_NAME) == before["md"], "源清单被改了")
    ok &= check("[1] 源侧车 SHA 不变", sha(src["bridge"] / SIDECAR) == before["side"], "源侧车被改了")
    ok &= check("[1] 源任务.json 逐字节不变", sha(src["dir"] / "任务.json") == before["task"],
                "源任务文件被改了")
    ok &= check("[1] 源提示词.md 逐字节不变", sha(src["dir"] / "提示词.md") == before["prompt"],
                "源提示词被改了")
    ok &= check("[1] 源视频 SHA 不变", sha(src["video"]) == before["video"], "源片被改了")
    ok &= check("[1] 源拆解目录文件集合不变",
                sorted(p.name for p in src["bridge"].iterdir()) == before["bridge_files"],
                "源拆解目录多了/少了文件（渲染产物泄漏到共享目录）")

    priv_md, priv_side = nd / LIST_NAME, nd / SIDECAR
    ok &= check("[1] fork 私有清单在", priv_md.is_file(), str(priv_md))
    ok &= check("[1] fork 私有侧车在", priv_side.is_file(), str(priv_side))
    ptxt = priv_md.read_text(encoding="utf-8") if priv_md.is_file() else ""
    ok &= check("[1] 私有副本里没有源任务号引用", f"local:{lid}/" not in ptxt, "还指着源任务的资产图")
    ok &= check("[1] 私有副本引用已重写指自己", f"local:{new_id}/" in ptxt, "没重写")
    inbox = list((nd / "inbox").iterdir()) if (nd / "inbox").is_dir() else []
    ok &= check("[1] inbox 有一份同 sha 源片",
                len(inbox) == 1 and sha(inbox[0]) == before["video"], [p.name for p in inbox])
    ok &= check("[1] 指纹副本同抄",
                (nd / ".源视频指纹.json").is_file()
                and sha(nd / ".源视频指纹.json") == sha(src["bridge"] / ".源视频指纹.json"),
                "指纹副本不在或内容不同")
    up = list((nd / "asset-uploads").iterdir()) if (nd / "asset-uploads").is_dir() else []
    ok &= check("[1] 资产图字节已复制",
                up and sha(up[0]) == sha(next((src["dir"] / "asset-uploads").iterdir())),
                [p.name for p in up])

    print("[2] 出生态与剥离：awaiting_assets + derived_from + 零审批痕迹 + 来源侧反查")
    task = json.loads((nd / "任务.json").read_text(encoding="utf-8"))
    ok &= check("[2] 起步态=awaiting_assets", task.get("state") == "awaiting_assets", task.get("state"))
    ok &= check("[2] 私有清单声明在场", task.get(replace.PRIVATE_FIELD) is True, task.get(replace.PRIVATE_FIELD))
    df = task.get("derived_from") or {}
    ok &= check("[2] derived_from 记来源任务号", df.get("local_id") == lid, str(df))
    ok &= check("[2] derived_from 记 bridge 与时点",
                df.get("bridge_id") == "br-21a" and bool(df.get("at")), str(df))
    ok &= check("[2] history 从派生起点如实记一条",
                len(task.get("history") or []) == 1
                and (task["history"][0].get("from") is None)
                and str(task["history"][0].get("to")) == "awaiting_assets",
                str(task.get("history"))[:120])
    leaked = [k for k in ("approval_snapshot", "approved_at", "gen", "gen_channel",
                          "submit_unknown", "assets_confirmed", "selfcheck", "audio_gate",
                          "lost_run", "heartbeat") if task.get(k)]
    ok &= check("[2] 审批/提交/生成痕迹一概不继承", not leaked, f"漏继承：{leaked}")
    ok &= check("[2] pending_replace 清空（不继承上次上传流水）",
                task.get("pending_replace") == [], str(task.get("pending_replace"))[:80])
    ok &= check("[2] 产物不继承（无 成片.mp4）", not (nd / "成片.mp4").is_file(), "fork 里出现了成片")
    ok &= check("[2] 标量抄值（意图/三态/参数）",
                task.get("intent") == src["task"]["intent"]
                and task.get("audio_mode") == "沿用原片"
                and (task.get("gen_params") or {}).get("时长") == 5, str(task.get("gen_params")))
    ok &= check("[2] ingestion 不抄 zip_sha256（否则同片重传会撞到派生版）",
                "zip_sha256" not in (task.get("ingestion") or {}), str(task.get("ingestion")))

    src_view = hub_api.get_task(lid)
    ok &= check("[2] 来源任务侧反查到派生版本",
                [v.get("local_id") for v in (src_view.get("derived_versions") or [])] == [new_id],
                str(src_view.get("derived_versions"))[:160])
    sum_view = [t for t in hub_api.list_tasks() if t.get("local_id") == lid]
    ok &= check("[2] 列表卡片带派生版本数",
                bool(sum_view) and int(sum_view[0].get("derived_count") or 0) == 1,
                str(sum_view[:1])[:160])
    new_view = hub_api.get_task(new_id)
    ok &= check("[2] 派生任务详情带 derived_from 与私有作用域自述",
                (new_view.get("derived_from") or {}).get("local_id") == lid
                and new_view.get("checklist_private") is True, str(new_view.get("derived_from")))
    return {"src": src, "new_id": new_id}


# ── [3] 双 fork 互不影响 ─────────────────────────────────────────────────
def dual_fork_phase(ctx: dict) -> bool:
    print("[3] 双 fork 互不影响：A 改图 → A 变、B 不变、源清单 SHA 不变")
    src, a_id = ctx["src"], ctx["new_id"]
    b_id = str(hub_api.fork_task("auto-7001").get("local_id") or "")
    ok = True
    ok &= check("[3] 第二次派生拿到另一个新号", b_id and b_id not in (a_id, "auto-7001"), b_id)
    src_md = sha(src["bridge"] / LIST_NAME)
    b_md = sha(TMP_PROJECTS / b_id / LIST_NAME)
    ref = f"local:{a_id}/asset-uploads/{ASSET_B}"
    (TMP_PROJECTS / a_id / "asset-uploads" / ASSET_B).write_bytes(b"\x89PNG\r\n\x1a\nA")
    hub_api.set_asset(a_id, 1, image_url=ref)
    ok &= check("[3] A 的清单出现自己的新引用", ref in (TMP_PROJECTS / a_id / LIST_NAME).read_text(encoding="utf-8"),
                "A 没写上")
    ok &= check("[3] B 的清单 SHA 不变（互不影响）", sha(TMP_PROJECTS / b_id / LIST_NAME) == b_md, "B 被改了")
    ok &= check("[3] 源任务清单 SHA 不变（A 改图没污染源）",
                sha(src["bridge"] / LIST_NAME) == src_md, "源清单被 fork 的写路径改了")
    # 描述改走同一作用域（A 改第 2 行描述；B/源都不动）
    b_md2 = sha(TMP_PROJECTS / b_id / LIST_NAME)
    hub_api.set_asset(a_id, 3, desc="A 改过的描述")
    ok &= check("[3] A 改描述后 B 不变", sha(TMP_PROJECTS / b_id / LIST_NAME) == b_md2, "B 被连带改了")
    ok &= check("[3] A 改描述后源清单不变", sha(src["bridge"] / LIST_NAME) == src_md, "源清单被连带改了")
    cards_a = cards.build("br-21a", a_id)
    ok &= check("[3] A 的资产卡读的是私有副本（已换数按自己算）",
                all(c.get("n") for c in cards_a), str(cards_a)[:120])
    return ok


# ── [4] 渲染作用域（真调 render.py）────────────────────────────────────────
def render_scope_phase(ctx: dict) -> bool:
    print("[4] 渲染作用域：桥侧按 fork 私有清单渲染，共享目录零污染，非法作用域被挡")
    if not DISC_SRC.is_file():
        print("      注：本工作副本没有 analysis/reverse/3/拆解.md，渲染相位**未覆盖**（如实记，不算 PASS）")
        RESULTS.append(("[4] 渲染相位（缺夹具未覆盖）", False))
        return False
    src, a_id = ctx["src"], ctx["new_id"]
    bridge = src["bridge"]
    sys.path.insert(0, str(ROOT / "pipeline" / "reverse"))
    import render  # noqa: E402

    ok = True
    # 4a. private_list_dir 三重校验
    got = hub_api.private_list_dir("br-21a", a_id)
    ok &= check("[4] 作用域解析到 fork 任务目录", got == TMP_PROJECTS / a_id, str(got))
    ok &= check("[4] 空作用域 → 共享清单（既有行为）", hub_api.private_list_dir("br-21a", "") is None)
    # 跨桥夹具：另造一条别桥任务（auto-7006 在 br-21x 下），它的任务号不属于 br-21a
    seed_source("auto-7006", bridge="br-21x", rows=2)
    for label, bridge_arg, task_arg, want in (
        ("非 fork 任务带作用域", "br-21a", "auto-7001", 400),
        ("跨桥引用", "br-21a", "auto-7006", 403),
        ("任务不存在", "br-21a", "auto-7099", 409),
        ("路径穿越", "br-21a", "../auto-7001", 400),
        ("绝对路径", "br-21a", str(TMP_PROJECTS / "auto-7006"), 400),
    ):
        status = None
        try:
            hub_api.private_list_dir(bridge_arg, task_arg)
        except hub_api.HubError as exc:
            status = getattr(exc, "status", None)
        ok &= check(f"[4] {label} → {want}", status == want, f"实际 {status}")

    # 4b. 真渲染：fork 私有清单（改了命名）→ 正文用 fork 的命名；共享目录不落产物
    before_bridge = sorted(p.name for p in bridge.iterdir())
    priv = TMP_PROJECTS / a_id / LIST_NAME
    txt = priv.read_text(encoding="utf-8").replace("源命名1", "派生版命名甲")
    priv.write_text(txt, encoding="utf-8")
    code = render.main(["--id", str(bridge), "--template", "lumi-4section",
                        "--checklist-dir", str(priv.parent), "--out-dir", str(priv.parent)])
    ok &= check("[4] render.py 带作用域跑通（exit 0）", code == 0, f"exit={code}")
    fork_prompt = (priv.parent / "提示词-lumi-4section.md")
    ok &= check("[4] 产物落在 fork 任务目录",
                fork_prompt.is_file() and (priv.parent / "资产清单.md").is_file(), "产物没落 fork 目录")
    body = fork_prompt.read_text(encoding="utf-8") if fork_prompt.is_file() else ""
    ok &= check("[4] 正文用的是 fork 的命名", "派生版命名甲" in body, "正文里没有 fork 的命名")
    ok &= check("[4] fork 的资产图被接成 @图片N", "@图片1" in body, "图没接上")
    ok &= check("[4] 共享 analysis 目录零新增产物",
                sorted(p.name for p in bridge.iterdir()) == before_bridge,
                f"多出 {sorted(set(p.name for p in bridge.iterdir()) - set(before_bridge))}")

    # 4c. 反向对照：不带作用域 → 正文用源命名（证明 4b 的差异来自作用域而不是别的东西）
    out2 = TMP_ROOT / "render-base"
    out2.mkdir(exist_ok=True)
    render.main(["--id", str(bridge), "--template", "lumi-4section", "--out-dir", str(out2)])
    base = (out2 / "提示词-lumi-4section.md").read_text(encoding="utf-8")
    ok &= check("[4] 反向对照：共享清单渲染出源命名", "源命名1" in base and "派生版命名甲" not in base,
                "共享渲染没有用源命名")

    # 4d. hub 侧 fetch_prompt 会带 checklist_task（假桥只验 URL 形状）
    captured: dict = {}
    old_get = generate.bridge.get
    generate.bridge.get = lambda url: (captured.update(url=url), {"text": "x"})[1]
    try:
        generate.fetch_prompt("br-21a", "沿用原片", a_id)
    finally:
        generate.bridge.get = old_get
    ok &= check("[4] hub 链渲染请求带上 checklist_task",
                f"checklist_task={a_id}" in str(captured.get("url")), str(captured.get("url")))
    return ok


# ── [5] 删源任务后 fork 仍可用 ───────────────────────────────────────────
def delete_source_phase() -> bool:
    print("[5] 删源任务后 fork 仍可用：源片可读（inbox 同 sha）、私有清单在、拆解产物不断链")
    src = seed_source("auto-7004", bridge="br-21b", rows=3)
    src_id = "auto-7004"
    fork_id = str(hub_api.fork_task(src_id).get("local_id") or "")
    ok = True
    ok &= check("[5] 派生成功", fork_id.startswith("auto-") and fork_id != src_id, fork_id)
    src_video_sha = sha(src["video"])
    out = hub_api.delete_task(src_id)
    ok &= check("[5] 源任务已删", out.get("ok") is True and not (TMP_PROJECTS / src_id).exists(), str(out)[:100])
    ok &= check("[5] 拆解产物因 fork 仍引用而保留", (TMP_ANALYSIS / "br-21b").is_dir(),
                f"analysis_skip={out.get('analysis_skip')}")
    nd = TMP_PROJECTS / fork_id
    inbox = [p for p in (nd / "inbox").iterdir() if p.is_file()]
    ok &= check("[5] fork 自己的 inbox 源片仍在且同 sha",
                len(inbox) == 1 and sha(inbox[0]) == src_video_sha, [p.name for p in inbox])
    ok &= check("[5] fork 私有清单仍在", (nd / LIST_NAME).is_file(), "私有清单没了")
    got = frames.video_path("br-21b", fork_id)
    ok &= check("[5] 抽帧能定位到 fork 自己的源片（删源后）",
                got is not None and under_tmp(got) and got == inbox[0], str(got))
    view = hub_api.get_task(fork_id)
    ok &= check("[5] 派生任务的来源号如实留着（即使源任务已删）",
                (view.get("derived_from") or {}).get("local_id") == src_id, str(view.get("derived_from")))
    ok &= check("[5] 删源后 fork 仍能读清单做资产卡", isinstance(cards.build("br-21b", fork_id), list))
    return ok


# ── [6] 直写 提示词.md ──────────────────────────────────────────────────
def prompt_write_phase() -> bool:
    print("[6] 直写 提示词.md：落盘 + 留痕 + 快照重算 + 状态不变；幂等/过短/错状态各按其位")
    src = seed_source("auto-7005", bridge="br-21c", rows=3)
    lid = "auto-7005"
    d = src["dir"]
    set_state(lid, "awaiting_approval")
    old_prompt = (d / "提示词.md").read_text(encoding="utf-8")
    old_snap = json.loads((d / "任务.json").read_text(encoding="utf-8")).get("approval_snapshot")
    ok = True

    new_text = old_prompt.replace("源正文。", "手改后的正文。", 1) + "\n\n（手改追加：镜头要更慢）\n"
    out = hub_api.save_prompt_text(lid, new_text)
    ok &= check("[6] 正文已落盘（磁盘逐字等于提交内容）",
                (d / "提示词.md").read_text(encoding="utf-8") == new_text, "盘上正文不对")
    ok &= check("[6] 返回体回显新正文", out.get("prompt_md") == new_text, str(out)[:100])
    task = json.loads((d / "任务.json").read_text(encoding="utf-8"))
    ok &= check("[6] prompt_edited_at 留痕", bool(task.get("prompt_edited_at")), str(task.get("prompt_edited_at")))
    ok &= check("[6] 审批快照按新正文重算（与旧值不同）",
                task.get("approval_snapshot") and task["approval_snapshot"] != old_snap,
                "快照没变 = 批的还是旧正文")
    ok &= check("[6] 状态不变（仍是 awaiting_approval）", task.get("state") == "awaiting_approval",
                task.get("state"))
    ok &= check("[6] 自检随正文一起重跑（有结果束）", isinstance(out.get("自检"), list), str(out.get("自检"))[:100])

    # 幂等：同一正文再提交一次 → unchanged，不写不重算
    snap2 = task["approval_snapshot"]
    out2 = hub_api.save_prompt_text(lid, new_text)
    task2 = json.loads((d / "任务.json").read_text(encoding="utf-8"))
    ok &= check("[6] 同文重复提交 = unchanged", out2.get("unchanged") is True, str(out2)[:100])
    ok &= check("[6] unchanged 不改快照", task2.get("approval_snapshot") == snap2, "快照被重算了")

    # 过短 → 400
    status = None
    try:
        hub_api.save_prompt_text(lid, "太短了")
    except hub_api.HubError as exc:
        status = getattr(exc, "status", None)
    ok &= check("[6] 过短正文 → 400", status == 400, f"实际 {status}")
    ok &= check("[6] 过短被拒后正文未变", (d / "提示词.md").read_text(encoding="utf-8") == new_text,
                "被拒却写了盘")

    # 错状态 → 409 人话
    for st_name in ("done", "approved", "generating", "awaiting_assets"):
        set_state(lid, st_name)
        status, msg = None, ""
        try:
            hub_api.save_prompt_text(lid, new_text + "x")
        except hub_api.HubError as exc:
            status, msg = getattr(exc, "status", None), str(exc)
        ok &= check(f"[6] {st_name} 态拒改正文 → 409 且说清出口",
                    status == 409 and ("批准" in msg or "复用为新版本" in msg or "置换" in msg
                                       or "重试" in msg or "拆解" in msg), f"{status} / {msg}")
    set_state(lid, "awaiting_approval")

    # 乐观锁：expect_text 与盘上不一致 → 409 不写
    status = None
    try:
        hub_api.save_prompt_text(lid, new_text + "\n（并发写）", expect_text="别人看到的旧正文")
    except hub_api.HubError as exc:
        status = getattr(exc, "status", None)
    ok &= check("[6] expect_text 不符 → 409（不覆盖别处的写入）", status == 409, f"实际 {status}")
    ok &= check("[6] 乐观锁拒绝后正文未变", (d / "提示词.md").read_text(encoding="utf-8") == new_text,
                "被拒却写了盘")
    return ok


# ── [7] 前端联动（起态门/归一化/请求形状/真渲染）───────────────────────────
WEB = ROOT.parent / "web"


def frontend_phase() -> bool:
    """前端探针（`web/scripts/probe-at21-b0.mjs`）：起态门与详情页反查入口的接线 + 真渲染。"""
    print("[7] 前端联动：node scripts/probe-at21-b0.mjs（起态门/归一化/请求形状/真渲染）")
    script = WEB / "scripts" / "probe-at21-b0.mjs"
    if not script.is_file():
        print("      注：本工作副本没有 web/scripts/probe-at21-b0.mjs——前端相位**未覆盖**（如实记，不算 PASS）")
        RESULTS.append(("[7] 前端相位（缺 web 脚本未覆盖）", False))
        return False
    states_path = TMP_ROOT / "states.json"
    states_path.write_text(json.dumps(sorted(state.STATES), ensure_ascii=False), encoding="utf-8")
    proc = subprocess.run(["node", "scripts/probe-at21-b0.mjs", "--states", str(states_path)],
                          cwd=str(WEB), capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=900)
    fails = "\n".join(line for line in (proc.stdout or "").splitlines() if "FAIL" in line)
    ok = check("[7] 前端探针全绿（起态门/归一化/请求形状/真渲染）", proc.returncode == 0,
               fails[-800:] or (proc.stderr or "")[-400:])
    for line in (proc.stdout or "").splitlines():
        if line.strip().startswith("[PASS]"):
            print(f"     {line.strip()}")
    return ok


# ── 入口 ────────────────────────────────────────────────────────────────
def main() -> int:
    print(f"[probe-at21-b0] 隔离根 {TMP_ROOT}")
    ok = True
    try:
        ok &= isolation_phase()
        ctx = fork_phase()
        ok &= all(good for _, good in RESULTS)          # 派生基本盘不过就别往下跑
        if ok:
            ok &= dual_fork_phase(ctx)
            ok &= render_scope_phase(ctx)
            ok &= delete_source_phase()
            ok &= prompt_write_phase()
            ok &= frontend_phase()
    finally:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)

    after = {"projects": snapshot(REAL_PROJECTS), "analysis": snapshot(REAL_ANALYSIS),
             "uploads": snapshot(REAL_UPLOADS)}
    for name in ("projects", "analysis", "uploads"):
        lost = sorted(BEFORE[name] - after[name])
        ok &= check(f"[8] 真实 {name} 没有条目消失（不删真实数据）", not lost, f"丢了 {lost}")
        mine = sorted(x for x in (after[name] - BEFORE[name]) if x in FIXTURES)
        ok &= check(f"[8] 夹具号没落进真实 {name}", not mine, f"落进真实目录：{mine}")
        extra = sorted((after[name] - BEFORE[name]) - set(mine))
        if extra:
            print(f"      注：{name} 跑期间另有新增 {extra[:6]}（非本探针产物：同仓并发痕迹）")
    ok &= check("[8] 临时根已删净", not TMP_ROOT.exists(), str(TMP_ROOT))

    passed = sum(1 for _, good in RESULTS if good)
    total = len(RESULTS)
    print(f"\n[probe-at21-b0] {passed}/{total} PASS")
    return 0 if (ok and passed == total) else 1


if __name__ == "__main__":
    raise SystemExit(main())
