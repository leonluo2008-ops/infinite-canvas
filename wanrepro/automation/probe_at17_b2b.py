#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AT-17 B2b 探针：批准前音频门禁（三态×提示词声音段一致性 + 失败原因透出 + 不自动回滚）。

跑法（`wanrepro/` 下，**纯离线零花钱**：不调模型、不跑 ffmpeg、不提交任何出片）：
  python -m automation.probe_at17_b2b
判据全对才 exit 0；任何一条 FAIL 如实打印并 exit 1（判 returncode，禁子串判通过）。

判据：
  [1] 纯口径（pipeline/reverse/audio_gate.evaluate，零模型零网络）：
      三类 PASS 正例（真渲染产物：沿用原片 / 按新主体调整 / 不要）+ 三类 FAIL 反例
      （不要却描述 BGM / 调整却沿用原片音效点原文 / 调整却点名旧主体实体名词 /
       沿用却声称无 BGM）+ 老任务缺三态 → 跳过（WARN 不 FAIL）+ 无拆解 → 降级 +
      空提示词 → 跳过。
  [2] approve 链执法（generate.do_approve / hub_api.do_approve，隔离任务目录）：
      不合格 → 批准被拒且原因文案在场；任务仍在 awaiting_approval；**提示词与自检字节不变**
      （附注 13：不自动回滚渲染）；门禁结果落任务文件 `audio_gate`，prompt_sha256 与文件一致；
      合格 → 放行到 approved 且 audio_gate=PASS；老任务缺三态 → 放行且 audio_gate=WARN。
  [3] 透出：hub_api.get_task 带 audio_gate（FAIL 时带原因原文）；隔离实例的
      GET /api/hub/task/<id> 与 POST /api/hub/approve/<id>（409 + 原因）同一份数据；
      批准失败后再 GET 仍 awaiting_approval（5 秒轮询看得到原因，不靠一次性 toast）。
  [4] 真实任务只读：auto-0006/0043/0052 的 任务.json SHA256 前后一致；
      探针自建的任务目录与分析目录跑完自删，真实目录集一格不多不少。
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]                  # wanrepro/
for extra in (ROOT, ROOT / "pipeline" / "reverse", ROOT / "ui"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from automation import config, generate, state              # noqa: E402
from automation.probe_guard import (GuardError, assert_no_loss, assert_same_root,  # noqa: E402
                                    free_port, http_req, projects_snapshot)
import hub_api                                              # noqa: E402

ANALYSIS = ROOT / "analysis" / "reverse"
PROJECTS = ROOT / "automation" / "projects"
FIXTURE = ROOT / "tests" / "fixtures" / "at15-r1"
REAL_TASKS = ("auto-0006", "auto-0043", "auto-0052")
PROBE_IDS = tuple(f"auto-99{n:02d}" for n in range(1, 13))
WORK = Path(tempfile.mkdtemp(prefix="probe-at17-b2b-"))
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f" —— {detail}" if detail and not cond else ""))
    return bool(cond)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""


def content_sha(path: Path) -> str:
    """文本内容哈希（与 audio_gate.prompt_sha 同口径：读文本 → utf-8 编码 → sha256）。

    注意与 sha256() 的区别：写盘走文本模式会在 Windows 上把 \\n 写成 \\r\\n，读回又统一成 \\n，
    所以「门禁记的提示词哈希」只可能等于**内容**哈希，不可能等于原始字节哈希。
    """
    return hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest() \
        if path.is_file() else ""


def task_of(data) -> dict:
    """服务端 GET /api/hub/task/<id> 的回包是 {task:{…}}（也兼容扁平对象）。"""
    if isinstance(data, dict) and isinstance(data.get("task"), dict):
        return data["task"]
    return data if isinstance(data, dict) else {}


def module_by_path(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RENDER = module_by_path("probe_b2b_render", ROOT / "pipeline" / "reverse" / "render.py")
GATE = module_by_path("probe_b2b_gate", ROOT / "pipeline" / "reverse" / "audio_gate.py")

# 拆解夹具：结构化「八 声音」（含 BGM 四字段 / 音效点 / 台词说话人），来自 at15-r1 真拆解
LEGACY = FIXTURE / "拆解.md"
CHECKLIST = FIXTURE / "待确认清单.md"
SOUND_BGM = "\n".join((
    "- BGM.情绪：轻快",
    "- BGM.节奏：中速，末段渐弱",
    "- BGM.乐器/织体：木琴、轻柔钢琴、软合成音",
    "- BGM.强弱走向：轻起渐强收",
    "- 音效点：00:01-00:02 盖子拧开的一声「咔」",
    "- 人声语言：西班牙语（es-MX）",
    "- 台词：00:01-00:02 「Esta fragancia es increíble」 说话人：画外音",
    "- 台词：00:03-00:04 「Mírame」 说话人：出镜者",
))
ORIGINAL_POINT = "00:01-00:02 盖子拧开的一声「咔」"
KEEP_BGM_LINE = ("BGM：情绪轻快，节奏中速，末段渐弱，乐器/织体木琴、轻柔钢琴、软合成音，"
                 "强弱走向轻起渐强收")


# ---------------------------------------------------------------- 夹具构造

def with_sound_body(markdown: str, body: str, audio: str = "有") -> str:
    """换掉「八 声音」正文并同步头部音轨标记（与 probe_at17_b1/b2a/b2c 同款做法）。"""
    out: list[str] = []
    inside = False
    for line in markdown.splitlines():
        match = re.match(r"^#{1,6}\s*(.+?)\s*$", line)
        if match:
            inside = match.group(1).strip() == "八 声音"
            out.append(line)
            if inside and body:
                out.append(body)
            continue
        if inside:
            continue
        out.append(line)
    return re.sub(r"音轨:\s*(有|无)", f"音轨: {audio}", "\n".join(out) + "\n", count=1)


def disc_text() -> str:
    return with_sound_body(LEGACY.read_text(encoding="utf-8-sig"), SOUND_BGM)


DISC = disc_text()


def render_prompt(mode: str, template: str = "seedance-flow") -> str:
    """真渲染一条（种子模板 = hub 默认 seedance-flow），三态各一——正例不接受手写。"""
    layers, _order = RENDER.parse_layers(DISC)
    text = (ROOT / "templates" / f"{template}.md").read_text(encoding="utf-8-sig")
    fm, body = RENDER.split_frontmatter(text)
    secs, _opt = RENDER.parse_sections(body)
    ents = RENDER.parse_checklist(CHECKLIST)
    facts = RENDER.header_facts(DISC)
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        out = RENDER.render_prompt(fm, secs, layers, ents, facts, "probe-b2b", template, {},
                                   audio_mode=mode, audio_src="探针")
    return out


GOOD = {mode: render_prompt(mode) for mode in ("沿用原片", "按新主体调整", "不要")}
OLD_NOUN = RENDER.entity_noun_core(RENDER.parse_checklist(CHECKLIST)[0])

# 三类反例：在**真渲染产物**上把声音段偷回不该有的内容（模拟「提示词与意图不符」）
BAD_MUTE = GOOD["不要"].replace("BGM：无 BGM，仅保留环境声",
                                "BGM：情绪轻快，节奏中速，乐器木琴与轻柔钢琴")
BAD_ADJUST_POINT = GOOD["按新主体调整"].replace(
    "音效点：不沿用原片声源（原片音效点由旧主体的动作产生），按新主体在对应时刻的动作重新拟声",
    f"音效点：{ORIGINAL_POINT}")
BAD_ADJUST_NOUN = GOOD["按新主体调整"].replace(
    "BGM：沿用原片的情绪（轻快）", f"BGM：{OLD_NOUN}出场的情绪轻快")
BAD_KEEP = GOOD["沿用原片"].replace(KEEP_BGM_LINE,
                                    "BGM：无BGM（原片没有背景音乐），仅保留环境声")


def seed_task(projects: Path, local_id: str, bridge_id: str, prompt: str,
              audio_mode: str | None = None, *, snapshot: bool = True,
              selfcheck: bool = True) -> Path:
    """一条 awaiting_approval 夹具（自建自删）：提示词 + 审批快照 +（可选）自检.json。"""
    tdir = projects / local_id
    if tdir.is_dir():
        shutil.rmtree(tdir)
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / generate.PROMPT_FILE).write_text(prompt, encoding="utf-8")
    if selfcheck:
        (tdir / "自检.json").write_text(json.dumps(
            [{"项": "音频门禁探针夹具", "结论": "PASS", "证据": "b2b"}],
            ensure_ascii=False, indent=2), encoding="utf-8")
    params = {"duration": 5, "ratio": "9:16", "resolution": "720", "audio": True}
    task = {
        "local_id": local_id, "bridge_id": bridge_id,
        "created_at": "2026-01-01T00:00:00+08:00", "state": "awaiting_approval",
        "history": [{"at": "2026-01-01T00:00:00+08:00", "from": None, "to": "ingested",
                     "note": "probe-at17-b2b 夹具"},
                    {"at": "2026-01-01T00:00:01+08:00", "from": "ingested",
                     "to": "awaiting_approval", "note": "probe-at17-b2b 夹具"}],
        "intent": "AT-17 B2b 音频门禁探针夹具", "assets": [],
        "ingestion": {"zip_sha256": f"probe-{local_id}", "zip_name": "probe",
                      "video_name": "", "text_name": "", "ignored": []},
    }
    if audio_mode is not None:
        task["audio_mode"] = audio_mode
    if snapshot:
        task["gen_params"] = params
        task["approval_snapshot"] = state.snapshot_hash(prompt, params)
    (tdir / "任务.json").write_text(json.dumps(task, ensure_ascii=False, indent=2),
                                    encoding="utf-8")
    return tdir


def seed_analysis(bridge_id: str, *, audio_mode_json: str | None = None) -> Path:
    outdir = ANALYSIS / bridge_id
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "拆解.md").write_text(DISC, encoding="utf-8")
    shutil.copy2(CHECKLIST, outdir / "待确认清单.md")
    if audio_mode_json is not None:
        (outdir / "audio_mode.json").write_text(json.dumps(
            {"audio_mode": audio_mode_json, "依据": "探针夹具", "待核对": False},
            ensure_ascii=False, indent=2), encoding="utf-8")
    return outdir


def try_approve(tdir: Path) -> tuple[bool, str]:
    try:
        generate.do_approve(tdir, verbose=False)
        return True, ""
    except generate.GenError as err:
        return False, str(err)
    except Exception as err:                       # noqa: BLE001 —— 别的异常也要如实报
        return False, f"{type(err).__name__}: {err}"


# ---------------------------------------------------------------- [1] 纯口径

def probe_pure() -> None:
    print("[1] 纯口径：三类 PASS 正例（真渲染）+ 三类 FAIL 反例 + 跳过/降级")

    for mode, prompt in GOOD.items():
        res = GATE.evaluate(prompt, mode, disc_text=DISC, checklist_path=CHECKLIST)
        check(f"正例 {mode}：真渲染产物 → PASS（不误伤自己的产物）",
              res["verdict"] == "PASS", json.dumps(res["checks"], ensure_ascii=False)[:220])

    cases = (
        ("不要 却描述 BGM", BAD_MUTE, "不要", "BGM"),
        ("调整 却沿用原片音效点原文", BAD_ADJUST_POINT, "按新主体调整", "音效点"),
        ("调整 却点名旧主体实体名词", BAD_ADJUST_NOUN, "按新主体调整", OLD_NOUN),
        ("沿用 却声称无 BGM", BAD_KEEP, "沿用原片", "无 BGM"),
    )
    for label, prompt, mode, must in cases:
        res = GATE.evaluate(prompt, mode, disc_text=DISC, checklist_path=CHECKLIST)
        text = "；".join(res.get("发现") or [])
        check(f"反例 {label} → FAIL", res["verdict"] == "FAIL",
              json.dumps(res["checks"], ensure_ascii=False)[:220])
        check(f"反例 {label}：原因文案里点到「{must}」", must in text, text[:220])
        check(f"反例 {label}：结果自带怎么处理", bool((res.get("处理") or "").strip()),
              str(res.get("处理"))[:120])

    legacy = GATE.evaluate(GOOD["不要"], "", disc_text=DISC, checklist_path=CHECKLIST)
    check("老任务缺 audio_mode → WARN 跳过（不 FAIL、不误伤）",
          legacy["verdict"] == "WARN" and "跳过" in "".join(legacy["发现"]),
          json.dumps(legacy["checks"], ensure_ascii=False)[:220])

    no_disc = GATE.evaluate(GOOD["按新主体调整"], "按新主体调整", disc_text="",
                            checklist_path=CHECKLIST)
    check("拆解读不到 → 降级 WARN（判不了就不判，不阻断批准）",
          no_disc["verdict"] == "WARN" and no_disc["checks"][0]["id"].startswith("A2"),
          json.dumps(no_disc["checks"], ensure_ascii=False)[:220])

    empty = GATE.evaluate("", "不要", disc_text=DISC, checklist_path=CHECKLIST)
    check("空提示词 → WARN 跳过（没有内容可判）",
          empty["verdict"] == "WARN" and empty["checks"][0]["id"].startswith("A9"),
          json.dumps(empty["checks"], ensure_ascii=False)[:220])

    # 口径防抖：真渲染产物里「无 BGM，仅保留环境声」不能被 A1 当成 BGM 断言
    mute_lines = [ln for ln in GOOD["不要"].splitlines() if "BGM" in ln]
    check("「无 BGM，仅保留环境声」是声音段真话，不算 BGM 断言",
          bool(mute_lines) and GATE.music_assertion(GOOD["不要"]) == "",
          str(mute_lines)[:200])

    # 口径防抖：调整模式的「不重复原片旋律」不是否定「有音乐」，不能被 A1 放过
    check("调整模式的「不重复原片旋律」不算「无 BGM」否定（换模式后仍会被 A1 抓到）",
          GATE.music_assertion(GOOD["按新主体调整"]) != "",
          GATE.music_assertion(GOOD["按新主体调整"])[:160])

    # 四字段解析同源：拆解里确有 BGM（A3 的判据不是拿音效点凑数）
    info = GATE.sound_info(DISC)
    check("拆解 BGM 四字段解析同源（extract.parse_sound_layer）→ 情绪=轻快",
          bool(info) and str((info.get("bgm") or {}).get("BGM.情绪", "")).strip() == "轻快",
          json.dumps(info.get("bgm") if info else {}, ensure_ascii=False)[:200])
    check("A3 判据只认 BGM 四字段（音效点单独存在 ≠ 有 BGM）",
          GATE.structured_bgm({"bgm": {"BGM.情绪": "无BGM"}, "sound_points": ["x"]}) == [],
          "structured_bgm 把「无BGM」排除")


# ---------------------------------------------------------------- [2] approve 链

FAIL_SPECS = (
    ("auto-9901", "不要", BAD_MUTE, "BGM"),
    ("auto-9902", "按新主体调整", BAD_ADJUST_POINT, "音效点"),
    ("auto-9903", "按新主体调整", BAD_ADJUST_NOUN, OLD_NOUN),
    ("auto-9904", "沿用原片", BAD_KEEP, "无 BGM"),
)


def probe_chain() -> None:
    print("[2] approve 链：不合格拒批（产物不动）+ 合格放行 + 老任务跳过")
    before = projects_snapshot(PROJECTS)
    for local_id, mode, prompt, must in FAIL_SPECS:
        bridge = f"probe-at17b2b-{os.getpid()}-{local_id}"
        seed_analysis(bridge)
        tdir = seed_task(PROJECTS, local_id, bridge, prompt, mode)
        prompt_sha = sha256(tdir / generate.PROMPT_FILE)
        selfcheck_sha = sha256(tdir / "自检.json")
        ok, msg = try_approve(tdir)
        check(f"{local_id}（{mode}）不合格 → 批准被拒", not ok, msg[:200])
        check(f"{local_id}：拒批文案带「音频门禁拒绝批准」+ 具体原因",
              "音频门禁拒绝批准" in msg and must in msg, msg[:220])
        check(f"{local_id}：状态仍 awaiting_approval（没被偷偷批准）",
              state.load(tdir)["state"] == "awaiting_approval", state.load(tdir)["state"])
        check(f"{local_id}：提示词字节不变（附注 13 不自动回滚渲染）",
              sha256(tdir / generate.PROMPT_FILE) == prompt_sha,
              f"{sha256(tdir / generate.PROMPT_FILE)[:12]} vs {prompt_sha[:12]}")
        check(f"{local_id}：自检.json 字节不变（渲染产物整包不动）",
              sha256(tdir / "自检.json") == selfcheck_sha, "自检.json 被改过")
        task = state.load(tdir)
        gate = task.get("audio_gate") or {}
        check(f"{local_id}：门禁结果落任务文件（verdict=FAIL/checks/发现/处理）",
              gate.get("verdict") == "FAIL" and bool(gate.get("checks"))
              and bool(gate.get("发现")) and bool(gate.get("处理")),
              json.dumps(gate, ensure_ascii=False)[:220])
        check(f"{local_id}：落盘的 prompt_sha256 == 提示词内容哈希",
              str(gate.get("prompt_sha256")) == content_sha(tdir / generate.PROMPT_FILE),
              f"{str(gate.get('prompt_sha256'))[:12]} vs "
              f"{content_sha(tdir / generate.PROMPT_FILE)[:12]}")
        detail = hub_api.get_task(local_id)
        check(f"{local_id}：get_task 透出 audio_gate（原因文案与落盘同一份）",
              (detail.get("audio_gate") or {}).get("verdict") == "FAIL"
              and (detail.get("audio_gate") or {}).get("发现") == gate.get("发现"),
              json.dumps(detail.get("audio_gate"), ensure_ascii=False)[:220])
        # hub 层：同一条链抛 HubError（HTTP 侧转 409），文案不变
        try:
            hub_api.do_approve(local_id)
            check(f"{local_id}：hub_api.do_approve 也拒（HubError）", False, "居然成功了")
        except hub_api.HubError as err:
            check(f"{local_id}：hub_api.do_approve 拒批 409 且文案同上",
                  getattr(err, "status", 409) == 409 and "音频门禁拒绝批准" in str(err),
                  str(err)[:200])
        shutil.rmtree(tdir, ignore_errors=True)

    print("  —— 合格：三类一致任务放行到 approved")
    for local_id, mode in (("auto-9905", "沿用原片"), ("auto-9906", "按新主体调整"),
                           ("auto-9907", "不要")):
        bridge = f"probe-at17b2b-{os.getpid()}-{local_id}"
        seed_analysis(bridge)
        tdir = seed_task(PROJECTS, local_id, bridge, GOOD[mode], mode)
        ok, msg = try_approve(tdir)
        task = state.load(tdir)
        check(f"{local_id}（{mode}）一致 → 批准放行", ok and task["state"] == "approved",
              msg or task["state"])
        check(f"{local_id}：audio_gate 落 PASS（门禁跑过、不是没跑）",
              (task.get("audio_gate") or {}).get("verdict") == "PASS",
              json.dumps(task.get("audio_gate"), ensure_ascii=False)[:200])
        check(f"{local_id}：get_task 透出 audio_gate=PASS",
              (hub_api.get_task(local_id).get("audio_gate") or {}).get("verdict") == "PASS",
              "get_task 没透出")
        shutil.rmtree(tdir, ignore_errors=True)

    print("  —— 老任务（无 audio_mode 字段）不误伤")
    bridge = f"probe-at17b2b-{os.getpid()}-auto-9908"
    seed_analysis(bridge)
    tdir = seed_task(PROJECTS, "auto-9908", bridge, BAD_MUTE)      # 内容本身不合格
    ok, msg = try_approve(tdir)
    task = state.load(tdir) if tdir.is_dir() else {}
    check("老任务缺 audio_mode → 照样放行（缺字段 ≠ 三态不符）",
          ok and task.get("state") == "approved", msg or task.get("state"))
    check("老任务：audio_gate 记 WARN「跳过音频执法」",
          (task.get("audio_gate") or {}).get("verdict") == "WARN"
          and "跳过" in "".join((task.get("audio_gate") or {}).get("发现") or []),
          json.dumps(task.get("audio_gate"), ensure_ascii=False)[:220])
    shutil.rmtree(tdir, ignore_errors=True)

    print("  —— 画布链旁路：任务.json 无三态、analysis/audio_mode.json 有 → 按它执法")
    bridge = f"probe-at17b2b-{os.getpid()}-auto-9909"
    seed_analysis(bridge, audio_mode_json="沿用原片")
    tdir = seed_task(PROJECTS, "auto-9909", bridge, BAD_KEEP)      # 声称无 BGM，与「沿用原片」矛盾
    ok, msg = try_approve(tdir)
    check("analysis/audio_mode.json=沿用原片 × 提示词声称无 BGM → 拒批",
          (not ok) and "音频门禁拒绝批准" in msg, msg[:200])
    check("旁路三态：结果里 audio_mode 记的是「沿用原片」",
          (state.load(tdir).get("audio_gate") or {}).get("audio_mode") == "沿用原片",
          json.dumps(state.load(tdir).get("audio_gate"), ensure_ascii=False)[:200])
    shutil.rmtree(tdir, ignore_errors=True)

    after = projects_snapshot(PROJECTS)
    try:
        assert_no_loss(before, after)
        check("探针自建任务目录已自删（真实任务集一格不少）", True)
    except GuardError as err:
        check("探针自建任务目录已自删（真实任务集一格不少）", False, str(err))


# ---------------------------------------------------------------- [3] HTTP 透出

def _wait_where(base: str, proc: subprocess.Popen, timeout: float = 90.0) -> dict:
    deadline = time.time() + timeout
    last = f"{base} 无响应"
    while time.time() < deadline:
        if proc.poll() is not None:
            out = ""
            try:
                out = (proc.stdout.read() or "")[-1200:] if proc.stdout else ""
            except Exception:                    # noqa: BLE001
                out = "（读子进程输出失败）"
            raise RuntimeError(f"隔离实例提前退出 rc={proc.returncode}｜输出：{out or '（无输出）'}")
        try:
            status, data = http_req(base, "/api/hub/where", timeout=5)
        except OSError as err:
            last = f"连接未就绪：{err}"
            time.sleep(0.7)
            continue
        if status == 200 and isinstance(data, dict):
            return data
        last = f"{status} {str(data)[:120]}"
        time.sleep(0.7)
    raise RuntimeError(f"隔离实例 {timeout:.0f}s 内没就绪：{last}")


def probe_http() -> None:
    print("[3] HTTP 透出：GET /api/hub/task 带 audio_gate；POST approve 409 + 原因")
    iso = WORK / "hub"
    iso_projects = iso / "projects"
    iso_projects.mkdir(parents=True, exist_ok=True)
    real_before = {p.name for p in ANALYSIS.iterdir() if p.is_dir()}
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env = dict(os.environ, WANREPRO_AUTO_DIR=str(iso_projects))
    proc: subprocess.Popen | None = None
    log_fh = None
    bridge = f"probe-at17b2b-{os.getpid()}-http"
    try:
        seed_analysis(bridge)
        bad = seed_task(iso_projects, "auto-9910", bridge, BAD_MUTE, "不要")
        good = seed_task(iso_projects, "auto-9911", bridge, GOOD["不要"], "不要")
        bad_prompt_sha = sha256(bad / generate.PROMPT_FILE)
        # 渲染完成时产品自己就会跑一次门禁（do_render 尾部）——这里照同一条生产函数先跑，
        # 让 GET 能证明「批准之前就看得见原因」，而不是只靠点一下的 toast。
        for tdir in (bad, good):
            generate.run_audio_gate(tdir, state.load(tdir))

        log_path = iso / "server.log"
        log_fh = open(log_path, "w", encoding="utf-8")           # noqa: SIM115
        proc = subprocess.Popen(
            [sys.executable, str(ROOT / "ui" / "server.py"), "--port", str(port), "--no-browser"],
            cwd=str(ROOT), env=env, stdout=log_fh, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        )
        where = _wait_where(base, proc)
        assert_same_root(where, iso_projects)
        check("隔离实例自述 tasks_dir == 临时根（读写同源）", True)

        st, data = http_req(base, "/api/hub/task/auto-9910")
        gate = task_of(data).get("audio_gate")
        check("GET /api/hub/task/<不合格> → audio_gate.verdict=FAIL",
              st == 200 and isinstance(gate, dict) and gate.get("verdict") == "FAIL",
              f"{st} {str(data)[:200]}")
        check("GET …：失败原因原文经 HTTP 透出（用户看得到「为什么批不了」）",
              isinstance(gate, dict) and any("BGM" in str(x) for x in (gate.get("发现") or []))
              and bool(gate.get("处理")),
              json.dumps(gate, ensure_ascii=False)[:220])

        st, data = http_req(base, "/api/hub/approve/auto-9910", method="POST",
                            data=b"{}", ctype="application/json")
        err = str((data or {}).get("error") or "") if isinstance(data, dict) else str(data)
        check("POST /api/hub/approve/<不合格> → 409", st == 409, f"{st} {err[:160]}")
        check("approve 响应带拒批原因（同一条文案）",
              "音频门禁拒绝批准" in err and "BGM" in err, err[:220])

        st, data = http_req(base, "/api/hub/task/auto-9910")
        after_fail = task_of(data)
        gate2 = after_fail.get("audio_gate") or {}
        check("拒批后 GET：状态仍 awaiting_approval（轮询看得到原因）",
              after_fail.get("state") == "awaiting_approval",
              str(after_fail.get("state")))
        check("拒批后 GET：audio_gate 仍在（不是一次性 toast）",
              (gate2 or {}).get("verdict") == "FAIL", json.dumps(gate2, ensure_ascii=False)[:200])
        check("拒批后提示词字节不变（不自动回滚渲染）",
              sha256(bad / generate.PROMPT_FILE) == bad_prompt_sha, "提示词被改/删了")

        st, data = http_req(base, "/api/hub/approve/auto-9911", method="POST",
                            data=b"{}", ctype="application/json")
        check("POST approve/<一致> → 200 approved（门禁不挡合格任务）",
              st == 200 and isinstance(data, dict) and data.get("state") == "approved",
              f"{st} {str(data)[:160]}")
        st, data = http_req(base, "/api/hub/task/auto-9911")
        check("GET /api/hub/task/<一致> → audio_gate.verdict=PASS",
              st == 200 and (task_of(data).get("audio_gate") or {}).get("verdict") == "PASS",
              f"{st} {str(data)[:160]}")
    except Exception as err:                     # noqa: BLE001 —— 探针自己崩了也要如实报
        check(f"HTTP 透出端到端跑通（异常：{err}）", False, str(err)[:300])
    finally:
        try:
            if log_fh is not None:
                log_fh.close()
        except Exception:                        # noqa: BLE001
            pass
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        shutil.rmtree(iso, ignore_errors=True)
        shutil.rmtree(ANALYSIS / bridge, ignore_errors=True)
        real_after = {p.name for p in ANALYSIS.iterdir() if p.is_dir()}
        check("探针自建分析目录已自删（真实 analysis 目录集一格不多不少）",
              real_after == real_before,
              f"多了：{sorted(real_after - real_before)}｜少了：{sorted(real_before - real_after)}")


# ---------------------------------------------------------------- 主流程

def real_digests() -> dict[str, str]:
    return {tid: sha256(PROJECTS / tid / "任务.json") for tid in REAL_TASKS}


def cleanup() -> None:
    for tid in PROBE_IDS:
        shutil.rmtree(PROJECTS / tid, ignore_errors=True)
    for path in ANALYSIS.glob("probe-at17b2b-*"):
        shutil.rmtree(path, ignore_errors=True)
    shutil.rmtree(WORK, ignore_errors=True)


def main() -> int:
    print(f"AT-17 B2b 探针：批准前音频门禁（零付费、纯离线）｜临时根 {WORK}")
    real_before = real_digests()
    analysis_before = {p.name for p in ANALYSIS.iterdir() if p.is_dir()}
    projects_before = projects_snapshot(PROJECTS)
    try:
        probe_pure()
        probe_chain()
        probe_http()
    finally:
        for tid in PROBE_IDS:
            shutil.rmtree(PROJECTS / tid, ignore_errors=True)
        for path in ANALYSIS.glob("probe-at17b2b-*"):
            shutil.rmtree(path, ignore_errors=True)

    print("[4] 真实任务只读 + 零残留")
    after = real_digests()
    present = {tid: digest for tid, digest in real_before.items() if digest}
    absent = [tid for tid, digest in real_before.items() if not digest]
    check(f"真实任务只读：在盘的 {sorted(present)} 任务.json SHA256 前后一致",
          all(present.get(tid) == after.get(tid) for tid in present),
          f"{present} → {after}")
    if absent:
        # 如实记录，不当成「通过」也不冤枉本卡：任务目录已被删除（删单是产品行为，有 .tombstones 记录）
        print(f"     如实记录：{'、'.join(absent)} 的 任务.json 不在盘（已被删除，"
              f"删单记录见 automation/projects/.tombstones.json），不参与 SHA 对账")
    analysis_after = {p.name for p in ANALYSIS.iterdir() if p.is_dir()}
    check("analysis/reverse 目录集不变（探针产物零残留）",
          analysis_after == analysis_before,
          f"多了：{sorted(analysis_after - analysis_before)}")
    try:
        assert_no_loss(projects_before, projects_snapshot(PROJECTS))
        check("automation/projects 真实任务集一格不少", True)
    except GuardError as err:
        check("automation/projects 真实任务集一格不少", False, str(err))
    shutil.rmtree(WORK, ignore_errors=True)
    check("探针临时根已清（零残留）", not WORK.exists(), str(WORK))

    total, passed = len(RESULTS), sum(1 for _n, ok, _d in RESULTS if ok)
    print(f"\nAT-17 B2b 探针：{passed}/{total} 项通过")
    bad = [(n, d) for n, ok, d in RESULTS if not ok]
    for name, detail in bad:
        print(f"  ✗ {name}｜{detail}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
