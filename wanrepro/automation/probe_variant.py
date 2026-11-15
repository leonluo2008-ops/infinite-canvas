#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""卡C路A 探针：变体任务直建（引擎入口）合同冻结 + 核心链。

跑法（wanrepro/ 下）：`python automation/probe_variant.py --isolated`，退出码 0 = 全绿。
**不带 `--isolated` 一律拒跑**（会写任务/清单，写类探针只许在隔离根内跑）。

锁的任务书验收（卡C路A 2026-11-15）：

  [0] 隔离生效：任务根 / 拆解根都落在本次临时根内（伪隔离 = 写真实目录）；
  [1] 双变体同 bridge_id 各得独立任务号；都 awaiting_assets 出生态；ingestion **无 zip_sha256**
      （幂等键刻意绕过）；intent / variant_of / checklist_private 落盘；
  [2] 拆解产物共享引用：两变体 bridge_id 一致；私有清单/侧车副本在场且与共享份同字节；
      共享清单/侧车 SHA 不变（零写穿）；
  [3] 源/其他任务零接触：源任务.json 逐字节不变、inbox 不变、无关任务不变；
      uploads 解析路径（monkeypatch UPLOADS_ROOT）不碰真实 uploads；
  [4] 推进：confirm（真调 replace.run_replace，empty_confirm）→ rendering；
      渲染完成段在隔离根内走受控边 state.transition（真桥渲染链由真链验收覆盖——本探针不连桥）；
      非法 bridge_id / 空意图 → 如实报错不落半套；
  [5] 真实目录只读：真实 projects / analysis/reverse / uploads 一条都没少；临时根删净。

隔离手法：`--isolated` 下 `WANREPRO_AUTO_DIR` + `WANREPRO_ANALYSIS_ROOT` 在 import automation
之前设；桥地址指向死端口（本探针不连桥）。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]                    # wanrepro/

if "--isolated" not in sys.argv:
    print("[probe-variant] 拒跑：本探针会写任务/清单，必须带 --isolated。\n"
          "  例：python automation/probe_variant.py --isolated")
    raise SystemExit(2)

REAL_PROJECTS = ROOT / "automation" / "projects"
REAL_ANALYSIS = ROOT / "analysis" / "reverse"
REAL_UPLOADS = ROOT / "uploads"

TMP_ROOT = Path(tempfile.mkdtemp(prefix="probe-variant-"))
TMP_PROJECTS = TMP_ROOT / "projects"
TMP_ANALYSIS = TMP_ROOT / "analysis" / "reverse"
os.environ["WANREPRO_AUTO_DIR"] = str(TMP_PROJECTS)
os.environ["WANREPRO_ANALYSIS_ROOT"] = str(TMP_ANALYSIS)
os.environ["WANREPRO_BRIDGE"] = "http://127.0.0.1:1"          # 死端口：本探针不连桥
TMP_PROJECTS.mkdir(parents=True)
TMP_ANALYSIS.mkdir(parents=True)

sys.path.insert(0, str(ROOT))

from automation import config, replace, state, variant            # noqa: E402

BRIDGE = "br-variant-a"
INTENT = "变体A：换成粉色包装"
RESULTS: list[tuple[str, bool]] = []
FIXTURE_VIDEOS: set[str] = set()


def check(label: str, good: bool, detail: str = "") -> bool:
    RESULTS.append((label, bool(good)))
    print(f"  [{'PASS' if good else 'FAIL'}] {label}" + (f" —— {detail}" if detail and not good else ""))
    return bool(good)


def sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def snapshot(root: Path) -> set[str]:
    if not root.is_dir():
        return set()
    return {str(p.relative_to(root)) for p in root.rglob("*")}


LIST_NAME = replace.LIST_NAME
SIDE_NAME = replace.SIDECAR_NAME


def seed_bridge(bridge_id: str, *, rows: int = 4) -> dict:
    """隔离根里造一份拆解产物（共享清单 + 侧车 + 指纹）+ 一条同 bridge 的既有任务（inbox 源片）。"""
    bd = TMP_ANALYSIS / bridge_id
    bd.mkdir(parents=True, exist_ok=True)
    lines = [f"# 待确认清单 · {bridge_id}", "",
             "> 每行已预填模型的描述。", "",
             "| # | 类别 | 模型看到什么 | 命名 | 资产图 | 锁什么 | 建议 | 用我的描述 |",
             "|---|------|-------------|------|--------|--------|------|------------|"]
    for n in range(1, rows + 1):
        lines.append(f"| {n} | 主体 | 描述{n} | 命名{n} |  | 外形 | 可沿用 |  |")
    (bd / LIST_NAME).write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
    (bd / SIDE_NAME).write_text(json.dumps(
        {"同实体确认": [[3, 4]], "行决策": {"2": "keep_origin"}}, ensure_ascii=False, indent=2),
        encoding="utf-8")

    src_dir = TMP_PROJECTS / "auto-7001"
    inbox = src_dir / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    video = inbox / "源片.mp4"
    video.write_bytes(b"VARIANTPROBESRC" * 2048)
    task = {"local_id": "auto-7001", "bridge_id": bridge_id, "created_at": "2026-11-15T00:00:00",
            "state": "done", "intent": "探针源任务（隔离根内）",
            "ingestion": {"video_name": "源片.mp4", "source_name": "源片.mp4", "zip_sha256": "f" * 64},
            "history": [{"at": "2026-11-15T00:00:00", "from": None, "to": "done", "note": "夹具"}]}
    (src_dir / "任务.json").write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
    (bd / variant.FP_NAME).write_text(json.dumps(
        {"路径": f"uploads/{video.name}", "大小": video.stat().st_size,
         "前1MB_sha256": variant._sha_first_mb(video), "记录时间": "2026-11-15 00:00:00"},
        ensure_ascii=False, indent=2), encoding="utf-8")
    FIXTURE_VIDEOS.add(video.name)
    return {"dir": src_dir, "bridge": bd, "video": video, "task_file": src_dir / "任务.json"}


# ── [0] 隔离生效 ─────────────────────────────────────────────────────────
def isolation_phase() -> bool:
    print("[0] 隔离生效：任务根/拆解根都在临时根内")
    ok = check("[0] 任务根=临时根", Path(variant.config.PROJECTS_DIR) == TMP_PROJECTS,
               str(variant.config.PROJECTS_DIR))
    ok &= check("[0] 拆解根=临时根", replace.analysis_root() == TMP_ANALYSIS,
                str(replace.analysis_root()))
    return ok


# ── [1][2][3] 直建基本盘 / 共享引用 / 零接触 ─────────────────────────────
def create_phase(src: dict) -> dict:
    print("[1][2][3] 双变体直建：独立任务号 / awaiting_assets / 共享拆解产物 / 源零接触")
    src_task_sha = sha(src["task_file"])
    src_video_sha = sha(src["video"])
    shared_md_sha = sha(src["bridge"] / LIST_NAME)
    shared_side_sha = sha(src["bridge"] / SIDE_NAME)
    shared_listing = sorted(p.name for p in src["bridge"].iterdir())

    a = variant.create_variant_task(BRIDGE, INTENT)
    b = variant.create_variant_task(BRIDGE, "变体B：换成蓝色包装")

    ok = check("[1] 两个变体各得独立任务号（≠源、互不等）",
               a["local_id"] != b["local_id"] and {a["local_id"], b["local_id"]} != {"auto-7001"},
               f"{a['local_id']} vs {b['local_id']}")
    ta = json.loads((TMP_PROJECTS / a["local_id"] / "任务.json").read_text(encoding="utf-8"))
    tb = json.loads((TMP_PROJECTS / b["local_id"] / "任务.json").read_text(encoding="utf-8"))
    ok &= check("[1] 两变体都 awaiting_assets 出生态",
                ta["state"] == "awaiting_assets" and tb["state"] == "awaiting_assets",
                f"{ta['state']}/{tb['state']}")
    ok &= check("[1] ingestion 无 zip_sha256（幂等键刻意绕过）",
                "zip_sha256" not in ta.get("ingestion", {}) and "zip_sha256" not in tb.get("ingestion", {}),
                json.dumps(ta.get("ingestion", {}), ensure_ascii=False)[:120])
    ok &= check("[1] 意图各自落盘", ta.get("intent") == INTENT and tb.get("intent") == "变体B：换成蓝色包装",
                f"{ta.get('intent')!r}/{tb.get('intent')!r}")
    ok &= check("[1] variant_of/checklist_private/私有清单副本落盘",
                ta.get("variant_of", {}).get("bridge_id") == BRIDGE
                and ta.get("checklist_private") is True
                and (TMP_PROJECTS / a["local_id"] / LIST_NAME).is_file()
                and (TMP_PROJECTS / b["local_id"] / LIST_NAME).is_file())
    ok &= check("[1] 源视频字节进各自 inbox（同 sha）",
                sha(TMP_PROJECTS / a["local_id"] / "inbox" / a["video"]) == src_video_sha
                and sha(TMP_PROJECTS / b["local_id"] / "inbox" / b["video"]) == src_video_sha)

    ok &= check("[2] 两变体 bridge_id 一致（共享同一份拆解产物）",
                ta["bridge_id"] == tb["bridge_id"] == BRIDGE)
    ok &= check("[2] 私有清单/侧车与共享份同字节",
                sha(TMP_PROJECTS / a["local_id"] / LIST_NAME) == shared_md_sha
                and sha(TMP_PROJECTS / b["local_id"] / LIST_NAME) == shared_md_sha
                and (not (TMP_PROJECTS / a["local_id"] / SIDE_NAME).exists()
                     or sha(TMP_PROJECTS / a["local_id"] / SIDE_NAME) == shared_side_sha))
    ok &= check("[2] 共享清单/侧车 SHA 不变（零写穿）",
                sha(src["bridge"] / LIST_NAME) == shared_md_sha
                and sha(src["bridge"] / SIDE_NAME) == shared_side_sha)
    ok &= check("[2] 共享拆解产物目录零新增文件",
                sorted(p.name for p in src["bridge"].iterdir()) == shared_listing,
                f"多出 {sorted(set(p.name for p in src['bridge'].iterdir()) - set(shared_listing))}")

    ok &= check("[3] 源任务.json 逐字节不变", sha(src["task_file"]) == src_task_sha)
    ok &= check("[3] 源任务 inbox 不变", sha(src["video"]) == src_video_sha)
    return {"a": a, "b": b, "ta": ta, "tb": tb, "ok": ok}


# ── [3续] uploads 解析路径（monkeypatch，不碰真实 uploads）───────────────
def uploads_resolve_phase() -> bool:
    print("[3续] uploads 落地副本解析路径（monkeypatch UPLOADS_ROOT）")
    bridge2 = "br-variant-up"
    bd = TMP_ANALYSIS / bridge2
    bd.mkdir(parents=True, exist_ok=True)
    (bd / LIST_NAME).write_text("# 待确认清单 · " + bridge2 + "\r\n", encoding="utf-8")
    up_video = TMP_ROOT / "uploads-fixture" / "落地片.mp4"
    up_video.parent.mkdir(parents=True)
    up_video.write_bytes(b"UPLOADSCOPY" * 4096)
    (bd / variant.FP_NAME).write_text(json.dumps(
        {"路径": f"uploads/{up_video.name}", "大小": up_video.stat().st_size}, ensure_ascii=False),
        encoding="utf-8")
    real_root = variant.UPLOADS_ROOT
    variant.UPLOADS_ROOT = up_video.parent
    try:
        got = variant.resolve_source_video(bridge2)
        ok = check("[3续] 指纹路径 → uploads 副本命中", got == up_video, str(got))
    finally:
        variant.UPLOADS_ROOT = real_root
    # 路径穿越防护：指纹改写为逃逸路径 → 只取落地名，不逃出 UPLOADS_ROOT
    decoy = up_video.parent / "真实机密.mp4"
    decoy.write_bytes(b"DECOY" * 1024)
    (bd / variant.FP_NAME).write_text(json.dumps(
        {"路径": "uploads/../../真实机密.mp4"}, ensure_ascii=False), encoding="utf-8")
    variant.UPLOADS_ROOT = up_video.parent
    try:
        got2 = variant.resolve_source_video(bridge2)
        ok &= check("[3续] 路径穿越被落地名截断", got2 == up_video.parent / "真实机密.mp4", str(got2))
    finally:
        variant.UPLOADS_ROOT = real_root
    return ok


# ── [4] 推进 + 非法输入 ─────────────────────────────────────────────────
def advance_phase(ctx: dict) -> bool:
    print("[4] 推进：confirm→rendering→awaiting_approval（受控边）+ 非法输入如实报错")
    from automation import replace as rep
    a_id = ctx["a"]["local_id"]
    tdir = TMP_PROJECTS / a_id
    ok = True
    try:
        st = rep.run_replace(tdir, assets=None, map_spec=None, name_spec=None,
                             confirm=True, verbose=False, empty_confirm=True, ack_rows=[])
        ok &= check("[4] 确认置换（全沿用）→ rendering", st == "rendering", str(st))
        # 渲染完成段：真桥渲染链（bridge GET /api/render）由真链验收覆盖；本探针不连桥，
        # 在隔离根内走引擎受控迁移边（generate.do_render 成功段同一函数，禁手写 state 文件）。
        state.transition(tdir, "awaiting_approval", note="探针：渲染完成段受控迁移（真链另验）")
        ok &= check("[4] 推进到 awaiting_approval", state.load(tdir)["state"] == "awaiting_approval")
    except Exception as exc:  # noqa: BLE001
        ok &= check("[4] 推进链无异常", False, repr(exc))

    for label, fn, needle in (
        ("bridge_id 不存在", lambda: variant.create_variant_task("br-不存在", INTENT), "拆解产物不在"),
        ("空意图", lambda: variant.create_variant_task(BRIDGE, "   "), "意图"),
        ("bridge_id 空串", lambda: variant.create_variant_task("", INTENT), "bridge_id"),
    ):
        try:
            fn()
            ok &= check(f"[4] {label} → 报错", False, "没有报错")
        except variant.VariantError as exc:
            ok &= check(f"[4] {label} → 报错含「{needle}」", needle in str(exc), str(exc)[:120])
    return ok


# ── 入口 ────────────────────────────────────────────────────────────────
def main() -> int:
    print(f"[probe-variant] 隔离根 {TMP_ROOT}")
    before = {"projects": snapshot(REAL_PROJECTS), "analysis": snapshot(REAL_ANALYSIS),
              "uploads": snapshot(REAL_UPLOADS)}
    ok = True
    try:
        ok &= isolation_phase()
        ctx: dict = {}
        if ok:
            src = seed_bridge(BRIDGE)
            ctx = create_phase(src)
            ok &= ctx.get("ok", False)
            if ok:
                ok &= uploads_resolve_phase()
                ok &= advance_phase(ctx)
    finally:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)

    after = {"projects": snapshot(REAL_PROJECTS), "analysis": snapshot(REAL_ANALYSIS),
             "uploads": snapshot(REAL_UPLOADS)}
    for name in ("projects", "analysis", "uploads"):
        lost = sorted(before[name] - after[name])
        ok &= check(f"[5] 真实 {name} 没有条目消失（不删真实数据）", not lost, f"丢了 {lost}")
        gained = sorted(set(after[name] - before[name]) & {f"auto-{n}" for n in range(9000, 9100)}
                        | (set(after[name] - before[name]) & {f"br-variant{c}" for c in ("", "-up")}))
        ok &= check(f"[5] 夹具没落进真实 {name}", not gained, f"落进真实目录：{gained}")
    ok &= check("[5] 临时根已删净", not TMP_ROOT.exists(), str(TMP_ROOT))

    passed = sum(1 for _, good in RESULTS if good)
    total = len(RESULTS)
    print(f"\nSUMMARY: {passed}/{total} PASS")
    for label, good in RESULTS:
        if not good:
            print(f"  FAIL: {label}")
    return 0 if (ok and passed == total) else 1


if __name__ == "__main__":
    raise SystemExit(main())
