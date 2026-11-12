#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AT-17 B3 探针：音频三态改选 → 落盘 → 重渲染 → 门禁联动（hub 链 + 画布链）。

跑法（`wanrepro/` 下，**纯离线零花钱**：不调模型、不跑 ffmpeg、不生成视频）：
  python -m automation.probe_at17_b3
判据全对才 exit 0；任何一条 FAIL 如实打印并 exit 1（判 returncode，禁子串判通过）。

判据：
  [1] 写实现只有一份：`intent.save_audio_mode` / `server.set_task_audio_mode`
      ——三个中文词可写、枚举外 ValueError→400 原文、写后 `待核对` 归 False（人已亲手选过）。
  [2] 重渲染的前置守卫（零网络）：非 awaiting_approval → GenError；任务没有三态 → GenError。
      （这两条必须在碰 bridge 之前就拒，否则「改三态」会替已批准的任务偷偷换正文。）
  [3] hub 链端到端（隔离实例，探针自建自删；**真 render.py + 真 audio_gate**，bridge 指回自己）：
      · 三态=不要 × 提示词声音段在讲 BGM → GET 里 audio_gate=FAIL（红条依据）
      · POST /api/hub/task/<id>/audio-mode「沿用原片」→ 200：任务.json 落值 + 旁路同值
        + 提示词按新三态重渲染 + audio_gate 重判为 PASS（红条消失，不是一次性 toast）
      · 再改「按新主体调整」→ 提示词声音段文本**真的变了**（BGM 行改写、音效点整类不沿用）
      · 枚举外 → 400；已批准任务 → 409（只能用既有「撤销批准」回退，不为改三态放宽状态机）
  [4] 画布链端到端（同一隔离实例）：POST /api/audio-mode/<task> 落任务目录 audio_mode.json
      → GET /api/task 透出 → 随后 GET /api/render/<task>/<tpl>（不带 query）产物的声音段
      就是新三态的产物（落盘即生效，画布链没有 approve）。
  [5] 真实任务只读（auto-0006 任务.json SHA256 前后一致）+ 探针自建任务/分析目录自删，
      跑后真实目录集一格不多不少。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
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
PROJECTS = config.PROJECTS_DIR
FIXTURE = ROOT / "tests" / "fixtures" / "at15-r1"
REAL_TASKS = tuple(t for t in ("auto-0006", "auto-0043", "auto-0052")
                   if (PROJECTS / t / "任务.json").is_file())
PROBE_TAG = f"probe-at17b3-{os.getpid()}"
PROBE_IDS = ("auto-9913", "auto-9914", "auto-9915")
WORK = Path(tempfile.mkdtemp(prefix="probe-at17-b3-"))
RESULTS: list[tuple[str, bool, str]] = []

PROMPTS = ("沿用原片", "按新主体调整", "不要")


def check(name: str, cond: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f" —— {detail}" if detail and not cond else ""))
    return bool(cond)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""


def module_by_path(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


INTENT = module_by_path("probe_b3_intent", ROOT / "pipeline" / "reverse" / "intent.py")
RENDER = module_by_path("probe_b3_render", ROOT / "pipeline" / "reverse" / "render.py")


# ---------------------------------------------------------------- 夹具

def seed_analysis(bridge_id: str) -> Path:
    """拆解/清单夹具（structured v2.6，真产物形态）——探针自建自删。"""
    outdir = ANALYSIS / bridge_id
    shutil.rmtree(outdir, ignore_errors=True)
    outdir.mkdir(parents=True, exist_ok=True)
    for name in ("拆解.md", "待确认清单.md", "待确认清单.json"):
        src = FIXTURE / name
        if src.is_file():
            shutil.copy2(src, outdir / name)
    return outdir


def render_prompt(bridge_id: str, mode: str) -> str:
    """用**真 render.py** 渲染一版提示词（与 bridge 的 GET /api/render 同一实现）。"""
    out = WORK / f"render-{mode}"
    out.mkdir(parents=True, exist_ok=True)
    code = RENDER.main(["--id", str(ANALYSIS / bridge_id), "--template", "seedance-flow",
                        "--audio-mode", mode, "--out-dir", str(out)])
    if code != 0:
        raise RuntimeError(f"render.py 退出 {code}（mode={mode}）")
    return (out / "提示词-seedance-flow.md").read_text(encoding="utf-8")


def seed_task(local_id: str, bridge_id: str, prompt: str, audio_mode: str | None,
              projects: Path | None = None) -> Path:
    """一条 awaiting_approval 夹具（自建自删）：提示词 + 审批快照 + 自检占位。"""
    tdir = (projects or PROJECTS) / local_id
    shutil.rmtree(tdir, ignore_errors=True)
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / generate.PROMPT_FILE).write_text(prompt, encoding="utf-8")
    (tdir / "自检.json").write_text(json.dumps(
        [{"项": "B3 探针夹具", "结论": "PASS", "证据": "probe_at17_b3"}],
        ensure_ascii=False, indent=2), encoding="utf-8")
    params = {"duration": 5, "ratio": "9:16", "resolution": "720", "audio": True}
    task = {
        "local_id": local_id, "bridge_id": bridge_id,
        "created_at": "2026-01-01T00:00:00+08:00", "state": "awaiting_approval",
        "history": [{"at": "2026-01-01T00:00:00+08:00", "from": None, "to": "ingested",
                     "note": "probe_at17_b3 夹具"},
                    {"at": "2026-01-01T00:00:01+08:00", "from": "ingested",
                     "to": "awaiting_approval", "note": "probe_at17_b3 夹具"}],
        "intent": "AT-17 B3 三态改选探针夹具", "assets": [],
        "ingestion": {"zip_sha256": f"probe-{local_id}", "zip_name": "probe",
                      "video_name": "", "text_name": "", "ignored": []},
        "gen_params": params,
        "approval_snapshot": state.snapshot_hash(prompt, params),
    }
    if audio_mode is not None:
        task["audio_mode"] = audio_mode
    (tdir / "任务.json").write_text(json.dumps(task, ensure_ascii=False, indent=2),
                                    encoding="utf-8")
    return tdir


def sound_text(prompt: str) -> str:
    """提示词里的「声音段」原文（BGM / 背景音乐 / 配乐 / 音效点 那几句）。"""
    import re
    out = [s.strip() for s in re.split(r"[。！？\n]", prompt or "")
           if any(k in s for k in ("BGM", "背景音乐", "背景乐", "配乐", "音效点"))]
    return "。".join(out)


# ---------------------------------------------------------------- [1] 写实现

def probe_writer() -> None:
    print("[1] 三态写实现（intent.save_audio_mode / server.set_task_audio_mode）")
    outdir = WORK / "write"
    outdir.mkdir(parents=True, exist_ok=True)
    for mode in ("沿用原片", "按新主体调整", "不要"):
        info = INTENT.save_audio_mode(outdir, mode)
        saved = json.loads((outdir / "audio_mode.json").read_text(encoding="utf-8"))
        check(f"写「{mode}」→ audio_mode.json 值逐字落盘、待核对=False",
              info["audio_mode"] == mode and saved["audio_mode"] == mode
              and saved["待核对"] is False and saved["依据"],
              json.dumps(saved, ensure_ascii=False))
    bad_target = outdir / "audio_mode.json"
    before = bad_target.read_text(encoding="utf-8")
    raised = ""
    try:
        INTENT.save_audio_mode(outdir, "静音")
    except ValueError as exc:
        raised = str(exc)
    check("枚举外的值 → ValueError 且原文点名合法值（不静默归一）",
          "只认" in raised and "静音" in raised, raised)
    check("枚举外的值不落盘（文件字节不变）",
          bad_target.read_text(encoding="utf-8") == before)

    # server 侧：画布链入口把 ValueError 翻成 400 语义（ApiError），值域外不写盘
    import server
    bridge = f"{PROBE_TAG}-writer"
    outdir2 = seed_analysis(bridge)
    res = server.set_task_audio_mode(bridge, "按新主体调整", "探针")
    saved2 = json.loads((outdir2 / "audio_mode.json").read_text(encoding="utf-8"))
    check("server.set_task_audio_mode 落盘并回显（ok + 值 + 来源）",
          res.get("ok") is True and res.get("audio_mode") == "按新主体调整"
          and saved2["audio_mode"] == "按新主体调整", json.dumps(res, ensure_ascii=False))
    status = 0
    msg = ""
    try:
        server.set_task_audio_mode(bridge, "随便")
    except server.ApiError as exc:
        status, msg = exc.status, exc.msg
    check("值域外 → ApiError 400（HTTP 层就是 400，不是 500/静默）",
          status == 400 and "只认" in msg, f"{status} {msg}")
    shutil.rmtree(outdir2, ignore_errors=True)


# ---------------------------------------------------------------- [2] 重渲染守卫

def probe_guards() -> None:
    print("[2] 重渲染前置守卫（零网络：守卫在碰 bridge 之前就要拒）")
    bridge = f"{PROBE_TAG}-guard"
    seed_analysis(bridge)
    # 守卫用例也在临时根里跑（真 projects 目录一个字节不碰）
    tmp_projects = WORK / "guard-projects"
    tmp_projects.mkdir(parents=True, exist_ok=True)
    approved = seed_task("auto-9915", bridge, "占位提示词" * 40, "不要", tmp_projects)
    task = state.load(approved)
    task["state"] = "approved"
    state.save(approved, task)
    msg = ""
    try:
        generate.rerender_for_audio_mode(approved)
    except generate.GenError as exc:
        msg = str(exc)
    check("已批准（approved）改三态 → GenError 并点名状态（不放宽状态机）",
          "awaiting_approval" in msg, msg[:200])

    no_mode = seed_task("auto-9915", bridge, "占位提示词" * 40, None, tmp_projects)
    msg2 = ""
    try:
        generate.rerender_for_audio_mode(no_mode)
    except generate.GenError as exc:
        msg2 = str(exc)
    check("任务没有三态 → GenError 明说「先把三态落盘」", "audio_mode" in msg2, msg2[:200])
    shutil.rmtree(tmp_projects, ignore_errors=True)
    shutil.rmtree(ANALYSIS / bridge, ignore_errors=True)


# ---------------------------------------------------------------- [3][4] HTTP 端到端

def _wait_ready(base: str, proc: subprocess.Popen, timeout: float = 90.0) -> dict:
    deadline = time.time() + timeout
    last = "无响应"
    while time.time() < deadline:
        if proc.poll() is not None:
            out = ""
            try:
                out = (proc.stdout.read() or "")[-1500:] if proc.stdout else ""
            except Exception:                    # noqa: BLE001
                out = "（读子进程输出失败）"
            raise RuntimeError(f"隔离实例提前退出 rc={proc.returncode}｜{out or '（无输出）'}")
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


def task_of(data) -> dict:
    if isinstance(data, dict) and isinstance(data.get("task"), dict):
        return data["task"]
    return data if isinstance(data, dict) else {}


def probe_http() -> None:
    print("[3] hub 链端到端：改三态 → 重渲染 → 门禁重判（隔离实例 + 真 render.py）")
    iso = WORK / "hub"
    iso_projects = iso / "projects"
    iso_projects.mkdir(parents=True, exist_ok=True)
    real_before = {p.name for p in ANALYSIS.iterdir() if p.is_dir()}
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env = dict(os.environ, WANREPRO_AUTO_DIR=str(iso_projects), WANREPRO_BRIDGE=base)
    proc: subprocess.Popen | None = None
    log_fh = None
    bridge = f"{PROBE_TAG}-http"
    try:
        seed_analysis(bridge)
        legacy_prompt = render_prompt(bridge, "沿用原片")          # 声音段在讲 BGM
        adjust_prompt = render_prompt(bridge, "按新主体调整")       # 骨架保留、旧声源剥离
        # 夹具：提示词是「沿用」产物，任务却声明「不要」→ 门禁必然 FAIL（A1 不要×BGM 断言）
        bad = seed_task("auto-9913", bridge, legacy_prompt, "不要", iso_projects)
        good = seed_task("auto-9914", bridge, legacy_prompt, "沿用原片", iso_projects)
        for tdir in (bad, good):
            generate.run_audio_gate(tdir, state.load(tdir))        # 渲染完成时产品自己会跑这一遍

        log_fh = open(iso / "server.log", "w", encoding="utf-8")    # noqa: SIM115
        proc = subprocess.Popen(
            [sys.executable, str(ROOT / "ui" / "server.py"), "--port", str(port), "--no-browser"],
            cwd=str(ROOT), env=env, stdout=log_fh, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        )
        where = _wait_ready(base, proc)
        assert_same_root(where, iso_projects)
        check("隔离实例自述 tasks_dir == 临时根（读写同源）", True)

        st, data = http_req(base, "/api/hub/task/auto-9913")
        gate = task_of(data).get("audio_gate") or {}
        check("改选前：GET 里 audio_gate=FAIL（红条有依据）",
              st == 200 and gate.get("verdict") == "FAIL",
              f"{st} {json.dumps(gate, ensure_ascii=False)[:200]}")
        check("改选前：红条文案点名「三态=不要 却在讲 BGM」",
              any("不要" in str(x) and "BGM" in str(x) for x in (gate.get("发现") or [])),
              json.dumps(gate.get("发现"), ensure_ascii=False)[:220])

        st, data = http_req(base, "/api/hub/task/auto-9913/audio-mode", method="POST",
                            data=json.dumps({"audio_mode": "沿用原片"}).encode("utf-8"),
                            ctype="application/json")
        check("POST audio-mode「沿用原片」→ 200", st == 200, f"{st} {str(data)[:200]}")
        body = data if isinstance(data, dict) else {}
        new_gate = body.get("audio_gate") or {}
        check("响应里直接带新门禁结果（PASS）与状态仍 awaiting_approval",
              body.get("state") == "awaiting_approval" and new_gate.get("verdict") == "PASS",
              json.dumps({k: body.get(k) for k in ("state", "audio_mode")}, ensure_ascii=False)
              + f"｜gate={new_gate.get('verdict')}")
        check("响应里提示词正文已按新三态重渲染（声音段含 BGM 原值）",
              "BGM" in str(body.get("prompt_md") or ""), str(body.get("prompt_md"))[:160])

        st, data = http_req(base, "/api/hub/task/auto-9913")
        detail = task_of(data)
        check("再 GET（5 秒轮询同一条路）：audio_mode=沿用原片、待核对=false、依据=界面改选",
              detail.get("audio_mode") == "沿用原片"
              and detail.get("audio_mode_待核对") is False
              and "改选" in str(detail.get("audio_mode_依据") or ""),
              json.dumps({k: detail.get(k) for k in
                          ("audio_mode", "audio_mode_待核对", "audio_mode_依据", "audio_mode_来源")},
                         ensure_ascii=False))
        check("再 GET：红条依据（audio_gate）已重判为 PASS（不是旧三态的 FAIL）",
              (detail.get("audio_gate") or {}).get("verdict") == "PASS",
              json.dumps(detail.get("audio_gate"), ensure_ascii=False)[:200])
        disk = json.loads((bad / "任务.json").read_text(encoding="utf-8"))
        check("任务.json 落值 + 重渲染留痕（audio_mode_history 一条）",
              disk.get("audio_mode") == "沿用原片"
              and isinstance(disk.get("audio_mode_history"), list)
              and len(disk["audio_mode_history"]) == 1,
              json.dumps(disk.get("audio_mode_history"), ensure_ascii=False)[:200])
        side = json.loads((ANALYSIS / bridge / "audio_mode.json").read_text(encoding="utf-8"))
        check("画布旁路同步同值（两链不打架）", side.get("audio_mode") == "沿用原片",
              json.dumps(side, ensure_ascii=False))
        snap = state.snapshot_for(state.load(bad), (bad / generate.PROMPT_FILE).read_text(encoding="utf-8"))
        check("审批快照按新正文重算（否则之后 approve 永远对不上）",
              state.load(bad).get("approval_snapshot") == snap,
              str(state.load(bad).get("approval_snapshot"))[:32])

        # 再改一次：提示词声音段文本必须真的随三态变（沿用 → 调整）
        prompt_before = (bad / generate.PROMPT_FILE).read_text(encoding="utf-8")
        st, data = http_req(base, "/api/hub/task/auto-9913/audio-mode", method="POST",
                            data=json.dumps({"audio_mode": "按新主体调整"}).encode("utf-8"),
                            ctype="application/json")
        prompt_after = (bad / generate.PROMPT_FILE).read_text(encoding="utf-8")
        check("再改「按新主体调整」→ 200 且提示词正文被重写", st == 200 and prompt_after != prompt_before,
              f"{st}｜{(data or {}).get('error', '') if isinstance(data, dict) else ''}")
        check("声音段文本随三态变（沿用→调整：BGM 行改写、音效点整类不沿用）",
              "不重复原片旋律" in sound_text(prompt_after)
              and "POP" not in sound_text(prompt_after)
              and "POP" in sound_text(prompt_before),
              f"before={sound_text(prompt_before)[:120]}｜after={sound_text(prompt_after)[:120]}")
        st, data = http_req(base, "/api/hub/task/auto-9913")
        check("第二次改选后门禁仍 PASS、三态=按新主体调整",
              task_of(data).get("audio_mode") == "按新主体调整"
              and (task_of(data).get("audio_gate") or {}).get("verdict") == "PASS",
              json.dumps({k: task_of(data).get(k) for k in ("audio_mode",)},
                         ensure_ascii=False))

        st, data = http_req(base, "/api/hub/task/auto-9913/audio-mode", method="POST",
                            data=json.dumps({"audio_mode": "静音"}).encode("utf-8"),
                            ctype="application/json")
        err = str((data or {}).get("error") or "") if isinstance(data, dict) else str(data)
        check("枚举外的值 → 400 + 点名合法值", st == 400 and "只认" in err, f"{st} {err[:160]}")

        st, data = http_req(base, "/api/hub/task/auto-9913/audio-mode", method="POST",
                            data=b"{}", ctype="application/json")
        check("不带 audio_mode → 400（空值也是值域外，不当作「保持不变」）",
              st == 400, f"{st} {str(data)[:120]}")

        approved = seed_task("auto-9914", bridge, legacy_prompt, "沿用原片", iso_projects)
        task = state.load(approved)
        task["state"] = "approved"
        task["approval"] = {"snapshot_sha256": task["approval_snapshot"], "at": "2026-01-01T00:00:09+08:00",
                            "by": "probe"}
        state.save(approved, task)
        st, data = http_req(base, "/api/hub/task/auto-9914/audio-mode", method="POST",
                            data=json.dumps({"audio_mode": "不要"}).encode("utf-8"),
                            ctype="application/json")
        err = str((data or {}).get("error") or "") if isinstance(data, dict) else str(data)
        check("已批准任务改三态 → 409 + 指引「撤销批准」回退", st == 409 and "撤销批准" in err,
              f"{st} {err[:200]}")

        # ---------------- [4] 画布链
        print("[4] 画布链端到端：POST /api/audio-mode 落盘即生效（画布链没有 approve）")
        st, data = http_req(base, f"/api/audio-mode/{bridge}", method="POST",
                            data=json.dumps({"audio_mode": "不要"}).encode("utf-8"),
                            ctype="application/json")
        check("POST /api/audio-mode → 200 且回显 audio_mode=不要", st == 200
              and (data or {}).get("audio_mode") == "不要", f"{st} {str(data)[:160]}")
        saved = json.loads((ANALYSIS / bridge / "audio_mode.json").read_text(encoding="utf-8"))
        check("任务目录 audio_mode.json 值逐字落盘、依据=界面改选", saved.get("audio_mode") == "不要"
              and saved.get("待核对") is False, json.dumps(saved, ensure_ascii=False))
        st, data = http_req(base, f"/api/task/{bridge}")
        check("GET /api/task 透出新三态（画布链读回的唯一出口）",
              st == 200 and (data or {}).get("audio_mode", {}).get("audio_mode") == "不要",
              str((data or {}).get("audio_mode"))[:160])
        st, data = http_req(base, f"/api/render/{bridge}/seedance-flow")
        text_now = str((data or {}).get("text") or "")
        check("随后渲染（不带 query）声音段 = 新三态产物（落盘即生效）",
              st == 200 and "仅保留环境声" in sound_text(text_now)
              and "电贝斯" not in sound_text(text_now),
              f"{st}｜{sound_text(text_now)[:140]}")
        st, data = http_req(base, f"/api/render/{bridge}/seedance-flow?audio_mode="
                                  + urllib.parse.quote("沿用原片"))
        text_keep = str((data or {}).get("text") or "")
        check("显式 ?audio_mode=沿用原片 → 声音段换回原片 BGM（三态真的进渲染）",
              st == 200 and "电贝斯" in sound_text(text_keep) and text_keep != text_now,
              f"{st}｜{sound_text(text_keep)[:140]}")
        st, data = http_req(base, f"/api/audio-mode/{bridge}", method="POST",
                            data=json.dumps({"audio_mode": "静音"}).encode("utf-8"),
                            ctype="application/json")
        check("画布链枚举外的值 → 400", st == 400, f"{st} {str(data)[:140]}")
    except Exception as err:                     # noqa: BLE001 —— 探针自己崩了也要如实报
        check(f"HTTP 端到端跑通（异常：{err}）", False, str(err)[:300])
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
    for path in ANALYSIS.glob("probe-at17b3-*"):
        shutil.rmtree(path, ignore_errors=True)
    shutil.rmtree(WORK, ignore_errors=True)


def main() -> int:
    print(f"AT-17 B3 探针：音频三态改选（落盘 + 重渲染 + 门禁联动）｜临时根 {WORK}")
    real_before = real_digests()
    analysis_before = {p.name for p in ANALYSIS.iterdir() if p.is_dir()}
    projects_before = projects_snapshot(PROJECTS)
    try:
        probe_writer()
        probe_guards()
        probe_http()
    finally:
        after = real_digests()
        check("真实任务只读（auto-0006 等 任务.json SHA256 前后一致）",
              after == real_before, f"变了：{[k for k in real_before if after.get(k) != real_before[k]]}")
        try:
            assert_no_loss(projects_before, projects_snapshot(PROJECTS))
            check("跑后真实任务集一格不少（有损即红）", True)
        except GuardError as err:
            check("跑后真实任务集一格不少（有损即红）", False, str(err))
        cleanup()
        analysis_after = {p.name for p in ANALYSIS.iterdir() if p.is_dir()}
        check("探针零残留：analysis/reverse 与跑前一致",
              analysis_after == analysis_before,
              f"多了：{sorted(analysis_after - analysis_before)}｜少了：{sorted(analysis_before - analysis_after)}")
        check("探针零残留：automation/projects 无 probe 夹具",
              not any((PROJECTS / tid).exists() for tid in PROBE_IDS))

    total = len(RESULTS)
    passed = sum(1 for _n, ok, _d in RESULTS if ok)
    print(f"\nAT-17 B3 探针结果：{passed}/{total} 通过")
    if passed != total:
        print("未通过：")
        for name, ok, detail in RESULTS:
            if not ok:
                print(f"  - {name}｜{detail[:300]}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
