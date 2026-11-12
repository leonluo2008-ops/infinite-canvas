#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一条命令串五步：拆解 → 待确认清单 → 意图建议回填 → 渲染提示词 → 拆解产物门禁（两阶段 + 断点续跑；门禁见 AT-15 R1）。

为什么要有它：下游命令各自都能跑，但「跑完拆解 → 清单 → 意图建议回填 → 人改清单 → 再渲染」这个来回，
每换一条片子都要手工敲多遍 `--id` 和路径，敲错一个字符就落到另一个目录。
这里只做一件事：把四步按顺序串起来，并且**在正确的位置停下来**。

两个阶段
--------
不带 `--yes`：跑 拆解 + 待确认清单 + 意图建议回填，然后停。这是给人改清单的那一步，退出码 0。
带   `--yes`：拆解（已在就跳）→ 清单（已在就跳）→ 意图建议回填 → 渲染，一次跑到底，退出码 0。

断点续跑
--------
`拆解.md` 存在、且源视频指纹（大小 + mtime + 前 1MB 的 sha256）与上次记下的一致
→ 不重调模型，直接复用；指纹变了（换了片子 / 片子被重下过）→ 重新拆解。
指纹里还记两份内容摘要：拼好之后的提示词（模板 / 注入文案 / 意图变就重拆）与看帧判定
（`看帧.json` 的「秒 + 判定」，判定变了就重拆；只动了时间戳不算变化）。
指纹记在 `analysis/reverse/<id>/.源视频指纹.json`，只在真跑过一次拆解之后才写。

拆解进度
--------
关键阶段边界会往 `analysis/reverse/<id>/进度.json` 覆写当前阶段
（`{"stage": 中文阶段名, "ts": epoch}`，失败时附 `error`），给轮询端透出
「现在跑到哪一步」（M6-T6F）：抽帧 → 大拆解 → 汇总 → 专项判定 → 投影 → 完成。
读写口径归 `pipeline/reverse/progress.py`；写不了不影响任何主流程。

清单的重建条件比「文件在」严一点：`待确认清单.md` 必须**不早于** `拆解.md`。
拆解重跑过而清单还是旧的，说明清单对不上新拆解，这时重建（并警告人工列会被覆盖）；
人工改过清单（mtime 更新）就一律不碰。

用法
----
    py -3 analyze.py <视频> [--id <id>] [--template <name>] [--yes]
                      [--model <名>] [--base-url <URL>] [--api-key <KEY>]

    py -3 analyze.py "原始资料/…/xxx.mp4"                 # 停在待确认清单
    py -3 analyze.py "原始资料/…/xxx.mp4" --yes           # 一路跑到底
    py -3 analyze.py "原始资料/…/xxx.mp4" --id demo1      # 指定产物目录名
    py -3 analyze.py "原始资料/…/xxx.mp4" --template seven-section

退出码（沿用下游）
------------------
0 成功（不带 --yes 时表示「已停在待确认清单」）
2 预检/输入失败：视频不在、目录建不了、模板不存在、下游报输入错
3 API 失败 / 模板不合规
4 缺层：拆解十层不齐
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------- 常量

REPO_ROOT = Path(__file__).resolve().parent
REVERSE_DIR = REPO_ROOT / "pipeline" / "reverse"
ANALYSIS_ROOT = REPO_ROOT / "analysis" / "reverse"
TEMPLATES_DIR = REPO_ROOT / "templates"

EXTRACT_REL = "pipeline/reverse/extract.py"
CONFIRM_REL = "pipeline/reverse/confirm.py"
INTENT_REL = "pipeline/reverse/intent.py"
RENDER_REL = "pipeline/reverse/render.py"

DISC_NAME = "拆解.md"
CHECK_NAME = "待确认清单.md"
FP_NAME = ".源视频指纹.json"
INTENT_NAME = "意图.txt"
FRAME_FACTS_NAME = "看帧.json"

HEAD_BYTES = 1024 * 1024          # 指纹只看前 1MB：大文件也不慢
DEFAULT_TEMPLATE = "lumi-4section"
MTIME_TOL = 1e-6                  # 1 微秒内算「同时刻」，避免文件系统精度抖动误判

EXIT_OK = 0
EXIT_INPUT = 2
EXIT_API = 3
EXIT_LAYER = 4

STOP_AT_CHECKLIST = "已生成 待确认清单.md，请改完/传完图后重新运行并加 --yes"


# ---------------------------------------------------------------- 输出小工具

def display_path(path: Path) -> str:
    """仓库内路径显示成相对路径，方便对账。"""
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except (ValueError, OSError):
        return str(path)


def quote_arg(text: str) -> str:
    """打印命令时给带空格/引号的参数加引号，保证复制出去能直接跑。"""
    return f'"{text}"' if any(ch in text for ch in ' "&()') else text


def banner(text: str) -> None:
    print("")
    print("-" * 78)
    print(text)
    print("-" * 78)


def use_line_buffering() -> None:
    """重定向到管道/文件时也按行flush：否则本文件的 stdout 会攒到最后一次性吐出，
    而 stderr 是即时的，两者在日志里会错位（子进程输出被夹在警告中间）。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)
        except (AttributeError, ValueError):
            pass


# ---------------------------------------------------------------- 源视频指纹

def head_sha256(path: Path, limit: int = HEAD_BYTES) -> str:
    """读文件头 limit 字节算 sha256；读不到就抛 OSError（由调用方判失败）。"""
    digest = hashlib.sha256()
    remaining = limit
    with open(path, "rb") as handle:
        while remaining > 0:
            chunk = handle.read(min(HEAD_BYTES, remaining))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
    return digest.hexdigest()


def make_fingerprint(video: Path) -> dict:
    """源视频指纹：大小 + mtime + 前 1MB 的 sha256。三项一起变才算换了片子。"""
    stat = video.stat()
    return {
        "路径": display_path(video),
        "大小": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "前1MB_sha256": head_sha256(video),
        "记录时间": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def read_fingerprint(path: Path) -> dict | None:
    """读上次记的指纹；文件不在或不是合法 JSON 都当「没有指纹」。"""
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def read_intent(outdir: Path) -> str:
    """只读任务目录当前的意图；文件不存在等同于空意图。"""
    path = outdir / INTENT_NAME
    try:
        return path.read_bytes().decode("utf-8") if path.is_file() else ""
    except OSError as exc:
        raise OSError(f"读不了当前 {INTENT_NAME}：{exc}") from exc


def intent_hash(intent: str) -> str:
    """意图原文 UTF-8 的 sha256；空意图也有稳定的空哈希。"""
    return hashlib.sha256(intent.encode("utf-8")).hexdigest()


def write_intent(outdir: Path, intent: str) -> None:
    """原子写当前意图；不清洗用户原文。"""
    outdir.mkdir(parents=True, exist_ok=True)
    target = outdir / INTENT_NAME
    temp = target.with_name(f".{target.name}.tmp{os.getpid()}")
    try:
        temp.write_text(intent, encoding="utf-8", newline="\n")
        os.replace(temp, target)
    except OSError:
        try:
            temp.unlink()
        except OSError:
            pass
        raise


def write_fingerprint(path: Path, data: dict) -> None:
    """先写同目录临时文件再 rename：半截 JSON 不该被下次当真。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp{os.getpid()}")
    try:
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, path)
    except OSError:
        try:
            temp.unlink()
        except OSError:
            pass
        raise


def compare_fingerprint(stored: dict, current: dict) -> tuple[bool, str]:
    """比三项。返回 (是否一致, 不一致的原因)。"""
    if stored.get("大小") != current["大小"]:
        return False, f"大小 {stored.get('大小')} → {current['大小']} 字节"
    if stored.get("mtime_ns") != current["mtime_ns"]:
        old = stored.get("mtime_ns")
        try:
            old_text = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(old) / 1e9))
        except (TypeError, ValueError, OSError):
            old_text = str(old)
        new_text = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(current["mtime_ns"] / 1e9))
        return False, f"mtime {old_text} → {new_text}"
    if stored.get("前1MB_sha256") != current["前1MB_sha256"]:
        return False, (f"前 1MB sha256 {str(stored.get('前1MB_sha256'))[:12]}… → "
                       f"{current['前1MB_sha256'][:12]}…")
    return True, ""


def describe_fingerprint(data: dict) -> str:
    """一行摘要（给人看的，不打印完整 hash）。"""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(data["mtime_ns"] / 1e9))
    return f"大小 {data['大小']} 字节｜mtime {stamp}｜首 1MB sha256 {data['前1MB_sha256'][:12]}…"


# ---------------------------------------------------------------- 看帧判定摘要（进指纹）

_FRAME_FACTS_MODULE = None


def frame_facts_module():
    """按路径导入 frame_facts.py：`看帧.json` 的格式归它管，摘要算法不在别处再写一份。"""
    global _FRAME_FACTS_MODULE
    if _FRAME_FACTS_MODULE is None:
        _FRAME_FACTS_MODULE = load_module("_reverse_frame_facts", REVERSE_DIR / "frame_facts.py")
    return _FRAME_FACTS_MODULE


def frame_facts_digest(outdir: Path) -> str:
    """看帧.json 里「会进提示词」那部分（秒 + 判定）的摘要；没有文件 / 没有可用判定 → 空串。

    不含时间戳与文件路径：同一条判定原样重跑一遍不算变化，不该因此把拆解重拆一遍。
    """
    path = Path(outdir) / FRAME_FACTS_NAME
    if not path.is_file():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    try:
        return frame_facts_module().content_digest(data if isinstance(data, dict) else None)
    except (ImportError, OSError, AttributeError, SyntaxError):
        return ""


def fingerprint_for(video: Path, outdir: Path, model_identity: str = "") -> dict:
    """拆解指纹 = 源视频三项 + 「看帧判定」内容摘要 + 拆解模型。

    看帧判定现在是拆解的第一步（extract.py 自动跑），它改了拆解就该重写——否则后补的
    判定对已有拆解永远不生效。正常情况下由 B1 在拆解**之前**生成，写完指纹就是新值，
    下一次算出来的还是同一个值 → 不会反复重拆。
    拆解模型（M6-T7-C）：选定渠道模型等于换了一条产出线，换了就该重拆——老指纹没有
    这个字段，读出来是空串；不带 --model 时当前值也是空串 → 旧任务零回归。
    """
    data = make_fingerprint(video)
    data["看帧判定哈希"] = frame_facts_digest(outdir)
    data["拆解模型"] = model_identity
    return data


def compare_frame_facts(stored: dict, current: dict) -> tuple[bool, str]:
    """比「看帧判定」摘要。老指纹没有这个字段 → 当成当时没有判定（后补的会触发重拆）。"""
    old = str(stored.get("看帧判定哈希") or "")
    new = str(current.get("看帧判定哈希") or "")
    if old == new:
        return True, ""
    if not old:
        return False, "上次拆解时没有看帧判定，现在有了"
    if not new:
        return False, f"看帧判定没了（{old[:12]}… → 无）"
    return False, f"看帧判定变了（{old[:12]}… → {new[:12]}…）"


def compare_model(stored: dict, current: dict) -> tuple[bool, str]:
    """比「拆解模型」（M6-T7-C）。老指纹没有这个字段 → 当成默认模型。

    不带 --model 时当前值是空串，与老指纹（缺字段读空）一致 → 不触发重拆（零回归）；
    指定了模型（或从指定退回默认）→ 产出线变了，重拆。
    """
    old = str(stored.get("拆解模型") or "")
    new = str(current.get("拆解模型") or "")
    if old == new:
        return True, ""
    if not old:
        return False, f"上次拆解没记拆解模型，现在指定了 {new}"
    if not new:
        return False, f"拆解模型变了（{old} → 默认）"
    return False, f"拆解模型变了（{old} → {new}）"


def model_identity(model: str | None, base_url: str | None) -> str:
    """拆解模型指纹串：命令行透传值原样拼 `模型@baseUrl`，都不给 = 空串（与老指纹同口径）。"""
    return f"{(model or '').strip()}@{(base_url or '').strip()}"


# ---------------------------------------------------------------- 借下游的解析器

def load_module(name: str, path: Path):
    """按路径导入 pipeline/reverse 下的脚本（那个目录不是包，没有 __init__.py）。"""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"导不进 {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------- 拆解进度文件

_PROGRESS_MODULE = None


def progress_module():
    """按路径导入 progress.py（进度.json 的读写口径归它管）；导不进返回 None。

    与 frame_facts_module 不同，这里失败**不抛**：进度透出是给人看的锦上添花，
    绝不能因为它把拆解拖死——静默退化为「不写进度」。
    """
    global _PROGRESS_MODULE
    if _PROGRESS_MODULE is None:
        try:
            _PROGRESS_MODULE = load_module("_reverse_progress", REVERSE_DIR / "progress.py")
        except (ImportError, OSError, AttributeError, SyntaxError):
            _PROGRESS_MODULE = False          # False = 试过且失败，不再重试
    return _PROGRESS_MODULE or None


def mark_stage(outdir: Path, stage: str, error: str | None = None) -> None:
    """往 analysis/reverse/<id>/进度.json 覆写当前阶段（M6-T6F 进度透出）。

    阶段序列（实际执行顺序）：抽帧 → 大拆解（extract.py 自己标）→ 汇总 →
    专项判定 → 投影 → 完成；失败写 {"stage": "失败", "error": 原因}。
    写不了就算了——显示用途，不值得让拆解失败。
    """
    mod = progress_module()
    if mod is None:
        return
    try:
        mod.write_progress(outdir, stage, error)
    except Exception:                        # noqa: BLE001 —— 显示用途，吞掉
        pass


def mark_failure(outdir: Path, step: str, code: int, detail: str = "") -> None:
    """桥侧失败的**唯一**进度写法：进度.json = {stage:"失败", error:"<步骤>失败（退出码 N[；细节]）"}。

    职责边界（与 automation/discuss.py 的失败回写配对）：
      · 本文件（桥侧）只写 analysis/reverse/<id>/进度.json，**不写** 任务.json；
      · 错误原文的主通道是 stderr——由 bridge（ui/server.py::analyze_video）采集尾部
        并原样放进 /api/job 的 error 字段；这里补的是**机器可判定**的「哪一步 + 退出码」，
        供轮询端在 job 记录丢失（服务重启）时仍能判失败并用同一套文案呈现；
      · 同一句写进 history 由 discuss.py 负责，两边同源不同盘，不互相覆盖。
    """
    tail = f"；{detail}" if detail else ""
    mark_stage(outdir, "失败", f"{step}失败（退出码 {code}{tail}）")


def local_layer_check(disc: Path, extract_mod) -> tuple[bool, str]:
    """不调模型，只按 extract.py 的规矩复核十层。复用它的正则，不另写一套。"""
    try:
        markdown = disc.read_text(encoding="utf-8")
    except OSError as exc:
        return False, f"读不到：{exc}"
    bodies, missing = extract_mod.split_layers(markdown)
    if missing:
        return False, "缺 " + "、".join(title for _index, title, _reason in missing)
    total = sum(len(body) for body in bodies.values())
    return True, f"十层齐全，正文合计 {total} 字"


# ---------------------------------------------------------------- 跑下游

def run_child(script_rel: str, args: list[str]) -> int:
    """跑一个下游脚本，子进程直接继承 stdout/stderr（用户看到的就是原始输出）。"""
    script = REPO_ROOT / script_rel
    printable = " ".join(["py -3", script_rel] + [
        "<api-key 已隐藏>" if index and args[index - 1] == "--api-key" else quote_arg(arg)
        for index, arg in enumerate(args)])
    print(f"[命令] {printable}", flush=True)
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"          # 子进程也按行吐，日志里才排得上队
    try:
        proc = subprocess.run([sys.executable, str(script), *args], cwd=str(REPO_ROOT), env=env)
    except OSError as exc:
        print(f"ERROR 起不了子进程：{exc}", file=sys.stderr)
        return EXIT_INPUT
    return proc.returncode


# ---------------------------------------------------------------- 阶段 1：拆解

def stage_extract(video: Path, video_id: str, outdir: Path, extract_mod, intent: str = "",
                  model: str | None = None, base_url: str | None = None,
                  api_key: str | None = None, diagnosis: Path | None = None,
                  force: bool = False) -> tuple[int, str]:
    """视频指纹与当前意图都未变且十层合格 → 跳过；否则真跑一次拆解。

    AT-15 R1 自动重拆：diagnosis=门禁诊断文件（非标题形态追加任务书末尾）、force=True
    绕过「指纹没变就跳过」——门禁判它不合格，这一版必须重跑，不能用缓存交差。
    """
    disc = outdir / DISC_NAME
    fp_path = outdir / FP_NAME

    try:
        intent = read_intent(outdir)
        current_intent_hash = intent_hash(intent)
        # 2026-10-30 实测：只算意图哈希时，改了 prompts/视频拆解.md 或改了意图注入那段文案，
        # 缓存照样命中——修好的 prompt 对已有任务永远不生效。所以改算**拼好之后的整段提示词**的哈希：
        # 提示词变（模板变 / 注入文案变 / 意图变）就重拆，一次覆盖三种情况。
        try:
            assembled = extract_mod.build_prompt(extract_mod.read_prompt(), intent)
        except Exception:                      # noqa: BLE001 —— 读不到 prompt 时退回只比意图
            assembled = None
        current_prompt_hash = (
            hashlib.sha256(assembled.encode("utf-8")).hexdigest() if assembled is not None
            else current_intent_hash
        )
        current = fingerprint_for(video, outdir, model_identity(model, base_url))
    except (OSError, UnicodeError) as exc:
        print(f"ERROR 读不了拆解输入（算不出视频或意图指纹）：{exc}", file=sys.stderr)
        return EXIT_INPUT, "失败"

    if disc.is_file() and not force:
        stored = read_fingerprint(fp_path)
        if stored is None:
            print(f"[重跑] {DISC_NAME} 在，但没有源视频指纹（{FP_NAME}）——"
                  f"证明不了视频没换过，重新拆解")
        else:
            stored_prompt_hash = stored.get("提示词哈希") or stored.get("意图哈希") or intent_hash("")
            same_intent = stored_prompt_hash == current_prompt_hash
            if not same_intent:
                if stored.get("提示词哈希"):
                    print("[重跑] 拆解提示词或意图变了 → 重新拆解")
                else:
                    print("[重跑] 意图变了 → 重新拆解")
            same_video, why = compare_fingerprint(stored, current)
            same_frames, why_frames = compare_frame_facts(stored, current)
            same_model, why_model = compare_model(stored, current)
            if same_intent and not same_video:
                print(f"[重跑] 源视频指纹变了（{why}）→ {DISC_NAME} 是基于旧视频的，重新拆解")
            elif same_intent and not same_frames:
                print(f"[重跑] {why_frames} → {DISC_NAME} 要按新判定重写")
            elif same_intent and not same_model:
                print(f"[重跑] {why_model} → {DISC_NAME} 要按选定模型重拆")
            elif same_intent and same_video:
                ok, detail = local_layer_check(disc, extract_mod)
                if ok:
                    frames_note = (f"｜看帧判定 {current['看帧判定哈希'][:12]}…"
                                   if current.get("看帧判定哈希") else "｜看帧判定 无")
                    if intent:
                        print(f"[跳过] {DISC_NAME} 已存在，源视频指纹与意图未变（{describe_fingerprint(current)}"
                              f"{frames_note}），不调模型")
                    else:
                        print(f"[跳过] {DISC_NAME} 已存在，源视频指纹未变（{describe_fingerprint(current)}"
                              f"{frames_note}），不调模型")
                    print(f"[跳过] 十层本地复核通过：{detail}")
                    return EXIT_OK, "跳过"
                if intent:
                    print(f"[重跑] 指纹与意图没变但十层不齐（{detail}）→ 重新拆解")
                else:
                    print(f"[重跑] 指纹没变但十层不齐（{detail}）→ 重新拆解")
    else:
        if force:
            print(f"[重拆] 门禁判不合格 → 按诊断重跑拆解（{DISC_NAME} 覆盖写）")
        else:
            print(f"[拆解] {outdir.name}/ 下没有 {DISC_NAME}，开始拆解")

    child_args = [display_path(video), "--id", video_id]
    if intent:
        child_args.extend(["--intent", intent])
    if diagnosis is not None:
        child_args.extend(["--diagnosis", str(diagnosis)])
    if (model or "").strip():
        child_args.extend(["--model", model.strip()])
    if (base_url or "").strip():
        child_args.extend(["--base-url", base_url.strip()])
    if (api_key or "").strip():
        child_args.extend(["--api-key", api_key.strip()])
    code = run_child(EXTRACT_REL, child_args)
    if code != EXIT_OK:
        print(f"ERROR 拆解失败（下游退出码 {code}），后面两步不再执行", file=sys.stderr)
        return code, "失败"

    if not disc.is_file():
        print(f"ERROR 拆解报成功，但 {display_path(disc)} 不在——下游说成功却没有产物",
              file=sys.stderr)
        return EXIT_LAYER, "失败"

    try:
        updated = dict(current)
        # 拆解子进程里可能刚生成了 看帧.json（B1 自动跑的那一帧）：指纹记**跑完之后**的值，
        # 否则下一次会算出不同的摘要，把刚拆完的这份又重拆一遍。
        updated["看帧判定哈希"] = frame_facts_digest(outdir)
        updated["提示词哈希"] = current_prompt_hash
        updated["意图哈希"] = current_intent_hash
        updated["拆解模型"] = current["拆解模型"]
        write_fingerprint(fp_path, updated)
    except OSError as exc:
        print(f"WARN 指纹没写成（{exc}）；不影响本次产物，但下次会重跑拆解", file=sys.stderr)
    else:
        if intent:
            print(f"[指纹] 记下 {FP_NAME}（视频与意图均未变时下次直接复用，不再调模型）")
        else:
            print(f"[指纹] 记下 {FP_NAME}（同一条视频下次直接复用，不再调模型）")
    return EXIT_OK, "重跑"


def stage_intent(disc: Path, checklist: Path, outdir: Path, intent: str) -> tuple[int, str]:
    """清单生成后，按清单行号生成建议并回填第 7 列；空意图则清理两者。"""
    suggestion = outdir / "意图建议.md"
    try:
        intent_mod = load_module("_reverse_intent", REPO_ROOT / INTENT_REL)
        if not intent:
            suggestion.unlink(missing_ok=True)
            intent_mod.write_dialogue(outdir, [])
            filled = intent_mod.apply_suggestions(checklist, "")
            # AT-17 B2c：空意图也落三态默认值（删建议文件 ≠ 删 audio_mode）
            mode_path, mode_info = intent_mod.write_audio_mode(
                outdir, disc.read_text(encoding="utf-8"))
            print(f"[意图建议] 意图为空，已删除旧建议文件并清空建议列（{filled} 行）")
            print(f"[音频三态] {mode_path}（{mode_info['audio_mode']}｜{mode_info['依据']}）")
            return EXIT_OK, "清理"
        disc_text = disc.read_text(encoding="utf-8")
        checklist_text = checklist.read_text(encoding="utf-8")
        suggestion.unlink(missing_ok=True)
        # 建议列是机器拥有列；调用失败也不能让上一轮建议伪装成当前结果。
        intent_mod.apply_suggestions(checklist, "")
        # 台词对照表复用这一次纯文本调用，同时落 `台词改写.md`（render 阶段据此替换正文台词）
        normalized = intent_mod.build(disc_text, checklist_text, intent, outdir=outdir)
        intent_mod._write_utf8(suggestion, normalized)
        filled = intent_mod.apply_suggestions(checklist, normalized)
        print(f"[写出] {suggestion}（{len(normalized.encode('utf-8'))} 字节）；回填建议列 {filled} 行")
    except (OSError, UnicodeError, ImportError, RuntimeError) as exc:
        print(f"ERROR 意图建议失败：{exc}", file=sys.stderr)
        return EXIT_API, "失败"
    return EXIT_OK, "生成"


# ---------------------------------------------------------------- 阶段 2：清单

def stage_confirm(disc: Path, outdir: Path, intent: str = "", refresh: bool = False) -> tuple[int, str]:
    """先生成清单，再由意图阶段按行号回填建议。"""
    check = outdir / CHECK_NAME

    if check.is_file():
        try:
            disc_mtime = disc.stat().st_mtime
            check_mtime = check.stat().st_mtime
            check_text = check.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            print(f"ERROR 读不到清单时间戳或内容：{exc}", file=sys.stderr)
            return EXIT_INPUT, "失败"
        has_b2_columns = "| 建议 |" in check_text and "| 用我的描述 |" in check_text
        if (disc_mtime <= check_mtime + MTIME_TOL
                and has_b2_columns
                and not refresh):
            print(f"[跳过] {CHECK_NAME} 已存在，且不早于 {DISC_NAME}"
                  f"（这份清单就是按这份拆解生成的），不重建")
            return EXIT_OK, "跳过"
        if not has_b2_columns:
            print(f"[重建] {CHECK_NAME} 缺少 B-2 尾列 → 追加「建议 / 用我的描述」")
        else:
            print(f"[重建] {DISC_NAME} 比 {CHECK_NAME} 新或本次需刷新 → 按新拆解重建清单")
        print("       注意：原清单里人工填过的「命名 / 资产图 / 用我的描述」会保留，重建后请重新核对",
              file=sys.stderr)
    else:
        print(f"[清单] {outdir.name}/ 下没有 {CHECK_NAME}，按拆解生成")

    code = run_child(CONFIRM_REL, [display_path(disc)])
    if code != EXIT_OK:
        print(f"ERROR 生成清单失败（下游退出码 {code}）", file=sys.stderr)
        return code, "失败"
    if not check.is_file():
        print(f"ERROR 清单报成功，但 {display_path(check)} 不在", file=sys.stderr)
        return EXIT_LAYER, "失败"
    return EXIT_OK, "生成"


# ---------------------------------------------------------------- 阶段 4：渲染

def stage_render(video_id: str, template: str) -> int:
    """渲染提示词 + 资产清单；模板不存在按「输入失败」返回 2。"""
    tpl_name = template[:-3] if template.endswith(".md") else template
    tpl_path = TEMPLATES_DIR / f"{tpl_name}.md"
    if not tpl_path.is_file():
        print(f"ERROR 模板不存在：{display_path(tpl_path)}；"
              f"现有 {sorted(p.stem for p in TEMPLATES_DIR.glob('*.md'))}", file=sys.stderr)
        return EXIT_INPUT
    return run_child(RENDER_REL, ["--id", video_id, "--template", tpl_name])


# ---------------------------------------------------------------- AT-15 R1 拆解产物门禁

def load_dissect_gate():
    """按路径加载 pipeline/reverse/dissect_gate.py（不新增包；顶层无重依赖）。"""
    spec = importlib.util.spec_from_file_location("_reverse_dissect_gate",
                                                  REVERSE_DIR / "dissect_gate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def gate_session_key(video: Path, intent: str, model: str | None) -> str:
    """门禁计数会话键：同片 + 同意图 + 同模型 = 同一会话（重拆轮次续算，断点续跑不重置）。

    换了片子/意图/模型就是另一件事，轮次从 0 起——不然上一部片用掉的两次重拆
    会把新片子的重拆额度吃掉。
    """
    try:
        stat = video.stat()
        basis = (f"{video.resolve()}|{stat.st_size}|{stat.st_mtime_ns}|"
                 f"{intent}|{(model or '').strip()}")
    except OSError:
        basis = f"{video}|0|0|{intent}|{(model or '').strip()}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- 主流程

def main(argv: list[str] | None = None) -> int:
    use_line_buffering()
    parser = argparse.ArgumentParser(
        description="一条命令串五步：拆解 → 待确认清单 → 意图建议回填 → 渲染提示词 → 拆解产物门禁（两阶段 + 断点续跑；门禁见 AT-15 R1）")
    parser.add_argument("video", nargs="?", help="参考视频路径")
    parser.add_argument("--id", dest="video_id", help="产物目录名（默认由文件名推出）")
    parser.add_argument("--template", default=DEFAULT_TEMPLATE,
                        help=f"templates/<name>.md 的 name（默认 {DEFAULT_TEMPLATE}）")
    parser.add_argument("--intent", default=None, help="本次用户意图（可选；不传则读取任务目录当前 意图.txt）")
    parser.add_argument("--yes", action="store_true",
                        help="清单已确认，一路跑到底（拆解/清单已在则跳，最后渲染提示词）")
    parser.add_argument("--model", default=None,
                        help="拆解用多模态模型名（M6-T7-C；默认内置默认模型，行为不变）")
    parser.add_argument("--base-url", default=None,
                        help="拆解渠道 base URL：不带版本段自动补 /v1beta，显式带则尊重")
    parser.add_argument("--api-key", default=None,
                        help="拆解渠道密钥（默认读 JXIN_API_KEY / 包根 .env；日志打印时自动隐藏）")
    args = parser.parse_args(argv)

    if not args.video:
        parser.error("缺少视频路径")

    video = Path(args.video).expanduser()
    if not video.exists():
        print(f"ERROR 视频不存在：{args.video}", file=sys.stderr)
        return EXIT_INPUT
    if not video.is_file():
        print(f"ERROR 不是文件：{args.video}", file=sys.stderr)
        return EXIT_INPUT
    video = video.resolve()

    try:
        extract_mod = load_module("_reverse_extract", REVERSE_DIR / "extract.py")
    except (ImportError, OSError) as exc:
        print(f"ERROR 载入 extract.py 失败：{exc}", file=sys.stderr)
        return EXIT_INPUT

    video_id = args.video_id or extract_mod.make_video_id(video)
    outdir = ANALYSIS_ROOT / video_id
    try:
        outdir.mkdir(parents=True, exist_ok=True)
        if args.intent is not None:
            write_intent(outdir, args.intent)
        current_intent = read_intent(outdir)
    except OSError as exc:
        print(f"ERROR 读写当前意图失败：{exc}", file=sys.stderr)
        mark_stage(outdir, "失败", f"读写当前意图失败：{exc}")
        return EXIT_INPUT

    tpl_name = args.template[:-3] if args.template.endswith(".md") else args.template
    banner(f"analyze · {video_id}")
    print(f"视频：{display_path(video)}")
    print(f"产物：{display_path(outdir)}/")
    if not current_intent:
        print(f"模式：{'--yes 一路到底（拆解 → 清单 → 渲染）' if args.yes else '两阶段（跑完拆解 + 清单就停）'}"
              f"｜模板：{tpl_name}")
    else:
        print(f"模式：{'--yes 一路到底（拆解 → 清单 → 渲染）' if args.yes else '两阶段（跑完拆解 + 清单就停）'}"
              f"｜模板：{tpl_name}｜意图：有")

    # 1) 拆解
    banner("[1/5] 拆解：参考视频 → 十层 Markdown")
    mark_stage(outdir, "抽帧")           # 预检/抽帧/看帧判定先跑；「大拆解」由 extract.py 自己标
    code, state = stage_extract(video, video_id, outdir, extract_mod, current_intent,
                                model=args.model, base_url=args.base_url, api_key=args.api_key)
    if code != EXIT_OK:
        mark_failure(outdir, "拆解", code, "详见日志")
        return code
    disc = outdir / DISC_NAME

    # 2) 先生成清单，保证意图模型拿到稳定的行号索引。
    banner("[2/5] 待确认清单：十层 → 四类实体（全预填）")
    mark_stage(outdir, "汇总")           # 十层 Markdown 汇总成四类实体清单
    code, _check_state = stage_confirm(
        disc, outdir, current_intent, refresh=(state == "重跑")
    )
    if code != EXIT_OK:
        mark_failure(outdir, "清单生成", code)
        return code
    check = outdir / CHECK_NAME

    # 3) 再生成意图建议，并按行号回填清单第 7 列。
    banner("[3/5] 意图建议：清单行号 + 用户意图 → 三档建议")
    mark_stage(outdir, "专项判定")       # 按清单逐行判三档建议
    code, _intent_state = stage_intent(disc, check, outdir, current_intent)
    if code != EXIT_OK:
        mark_failure(outdir, "意图建议", code)
        return code

    # 两阶段：不带 --yes 就停在这里
    if not args.yes:
        banner("停在这里 · 该你动手了")
        print(STOP_AT_CHECKLIST)
        print("")
        print(f"  清单：{display_path(check)}")
        print("  要做的事（任选其一）：")
        print("    ① 改「命名」列：模型只写形态，名字由你来定")
        print("    ② 「资产图」列填图片路径（多张用英文逗号）")
        print("    ③ 什么都不做 = 按模型看到的原样使用")
        print("")
        next_cmd = (f'py -3 analyze.py {quote_arg(display_path(video))} '
                    f'--id {video_id} --template {tpl_name} --yes')
        if current_intent:
            next_cmd += f' --intent {quote_arg(current_intent)}'
        print(f"  下一步：{next_cmd}")
        return EXIT_OK

    # 4) 渲染
    banner(f"[4/5] 渲染：十层 + 清单 → 提示词-{tpl_name}.md ＋ 资产清单.md")
    mark_stage(outdir, "投影")           # 提示词 + 资产清单落盘（轮询端「拉投影」读的就是它）
    code = stage_render(video_id, tpl_name)
    if code != EXIT_OK:
        mark_failure(outdir, "渲染", code)
        return code

    prompt_path = outdir / f"提示词-{tpl_name}.md"
    assets_path = outdir / "资产清单.md"
    missing = [p.name for p in (check, prompt_path, assets_path) if not p.is_file()]
    if missing:
        print(f"ERROR 渲染报成功，但缺产物：{'、'.join(missing)}", file=sys.stderr)
        mark_failure(outdir, "渲染", EXIT_LAYER, f"报成功但缺产物：{'、'.join(missing)}")
        return EXIT_LAYER

    # 5) 拆解产物门禁（AT-15 R1）：判下游真正消费的东西；不合格 → 按诊断自动重拆（上限 2 次）
    banner("[5/5] 拆解产物门禁：D1–D8（不合格自动重拆，上限 2 次；到顶如实标红）")
    mark_stage(outdir, "门禁")

    def rework(diagnosis: str) -> tuple[bool, str]:
        """门禁 FAIL → 只重跑 视觉模型+提取+清单+意图+渲染（实测/抽帧不重跑）。"""
        existing = sorted(outdir.glob("门禁诊断-第*轮.md"))
        diag_path = outdir / f"门禁诊断-第{len(existing) + 1}轮.md"
        diag_path.write_text(diagnosis, encoding="utf-8")
        print(f"[重拆] 诊断落盘：{display_path(diag_path)}")
        code, _state = stage_extract(video, video_id, outdir, extract_mod, current_intent,
                                     model=args.model, base_url=args.base_url,
                                     api_key=args.api_key, diagnosis=diag_path, force=True)
        if code != EXIT_OK:
            return False, f"视觉模型/提取失败 exit={code}"
        code, _ = stage_confirm(disc, outdir, current_intent, refresh=True)
        if code != EXIT_OK:
            return False, f"清单重建失败 exit={code}"
        code, _ = stage_intent(disc, check, outdir, current_intent)
        if code != EXIT_OK:
            return False, f"意图建议失败 exit={code}"
        code = stage_render(video_id, tpl_name)
        if code != EXIT_OK:
            return False, f"渲染失败 exit={code}"
        return True, "重拆完成（视觉模型+提取+清单+意图+渲染）"

    gate = load_dissect_gate()
    try:
        gate_result = gate.run(
            outdir, bridge_id=video_id, local_id=video_id,
            session_key=gate_session_key(video, current_intent, args.model),
            layer_check=lambda path: local_layer_check(path, extract_mod),
            rework=rework, max_rounds=2,
            stage=lambda message: (mark_stage(outdir, str(message)),
                                   print(f"[门禁] {message}", flush=True)),
        )
    except Exception as exc:                        # noqa: BLE001 —— 门禁是质量信号，不是交付闸门
        # 产物已经落盘交付了：门禁自己炸掉要如实留痕，但不能把一次成功的拆解判成失败。
        print(f"WARN 门禁没跑成（{type(exc).__name__}: {exc}）；产物照常交付", file=sys.stderr)
        gate_result = {"verdict": "WARN", "状态": f"门禁环境故障（{type(exc).__name__}）",
                       "缺项": [], "告警": [f"{type(exc).__name__}: {exc}"], "轮次": 0,
                       "上限": 2, "checks": [], "历史": []}
    print(gate.render(gate_result))
    gate_failed = gate_result["verdict"] == "FAIL"
    gate_line = (f"{gate_result['状态']}｜{len(gate_result.get('缺项') or [])} FAIL / "
                 f"{len(gate_result.get('告警') or [])} WARN｜重拆 {gate_result['轮次']}/"
                 f"{gate_result['上限']} 次")

    banner("完成")
    mark_stage(outdir, f"完成·门禁{gate_line}" if gate_failed else "完成")
    print(f"拆解　　　：{display_path(disc)}（{disc.stat().st_size} 字节）")
    print(f"待确认清单：{display_path(check)}（{check.stat().st_size} 字节）")
    print(f"提示词　　：{display_path(prompt_path)}（{prompt_path.stat().st_size} 字节）")
    print(f"资产清单　：{display_path(assets_path)}（{assets_path.stat().st_size} 字节）")
    print(f"门禁　　　：{gate_line}（{display_path(outdir / '门禁.json')}）")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
