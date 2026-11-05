#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""看帧判定 · 接线检查（B1 自动跑 + B2 指纹 + B3 回归）。

跑法：`py -3 tests/frame_facts_wiring_check.py`      退出码 0 = 全过，1 = 有 FAIL

为什么要有它
------------
`pipeline/reverse/frame_facts.py` 本身（判据 F1–F5）在 `frame_facts_check.py` 里已经验过。
但只做出判定、没人自动跑，等于没有：拆解照样「让模型顺便看」，旧的错原样重演。这个文件
盯的是**接线**——拆解第一步会不会自己跑、跑不起会不会降级、已有判定会不会重复花钱、
判定变了会不会触发重拆。

覆盖
----
① 空目录 + 假视频：run_extract 自己去跑首帧窗口（0:0.5、3 帧）＋尾帧窗（末 1.5 秒、2 帧；T6 起）
   ①-a 判定成功 → 建 `看帧.json`（窗口 + 聚合）、日志露出「[看帧] 0:0.5 窗口 · 0 秒 → …（1 次调用）」、
        提示词里注入这一块
   ①-b 网络被阻断 → 调用确实发出去了；失败后**不注入**、`看帧.json` 不落盘（下次还能重试）、
        stderr 一行警告、产物头部一行「本份拆解没有看帧判定（原因…）」、拆解照跑（exit 0）
② 已有可用判定 → 一次 VLM 调用都不多发（抽帧都不许再抽）
③ `--frames off` → 一次判定也不调，回到旧行为
④ 指纹含「看帧判定」摘要：改了判定 → 摘要变（触发重拆）；只改时间戳 → 摘要不变；
   写完指纹后下一次算出来的一样（不会反复重拆）；老指纹没有这个字段 → 当成当时没有判定
⑤ 回归：frame_facts_check / recheck_check / render_check / template_regression /
   gen_invoke_check 全绿

纪律：真实任务目录只读；所有写盘都在 tempfile 造的假任务里；真实 0 秒那一帧不在这里跑
（会花钱），这里用的是替身调用层。
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRAME_FACTS = ROOT / "pipeline" / "reverse" / "frame_facts.py"
EXTRACT = ROOT / "pipeline" / "reverse" / "extract.py"
ANALYZE = ROOT / "analyze.py"

FRAME_SIZE = (1080, 1888)
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")

JUDGMENT = {
    "frame": 0,
    "people": 1,
    "subject": "年轻女性",
    "position": {"where": "画面中央偏右", "height_ratio": "占画面高度约3/4"},
    "body_orientation": {"facing": "侧对镜头", "back_toward": "画面右侧"},
    "head_orientation": "向左回转，面向镜头",
    "gaze": "直视镜头",
    "posture_action": "右手抬到胸前握拳，左臂垂在体侧",
    "shot_size": "中景",
    "objects": ["橙色座椅"],
    "confidence": "高",
    "描述": "车厢里一个年轻女人侧身站着又回头看向镜头，右手抬到胸前，顶灯从上方斜下来。",
}

results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> bool:
    results.append((ok, name, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"｜{detail}" if detail else ""))
    return ok


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"导不进 {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_probe(path, timeout=60.0) -> dict:
    """ffprobe 的替身：够 run_extract 与抽帧尺寸对账用，不碰真文件。"""
    return {"duration": 15.0, "has_video": True, "has_audio": True,
            "width": FRAME_SIZE[0], "height": FRAME_SIZE[1], "fps": 30.0,
            "aspect": "9:16", "codec_types": ["video", "audio"], "stderr": "", "hard_cuts": []}


def fake_grab(video: Path, seconds: float, out_png: Path, source_size) -> dict:
    """ffmpeg 的替身：写一个真文件，尺寸按源片报。"""
    out_png.parent.mkdir(parents=True, exist_ok=True)
    out_png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 128)
    return {"文件": str(out_png), "宽": source_size[0], "高": source_size[1],
            "sha256": hashlib.sha256(out_png.read_bytes()).hexdigest(),
            "字节": out_png.stat().st_size}


def dissection_text(layer_titles) -> str:
    """拆解那一份的替身正文：九层标题齐全，正文非空。"""
    return "\n\n".join(f"## {title}\n（占位正文）" for title in layer_titles) + "\n"


def line_with(text: str, needle: str) -> str:
    """从 stdout/stderr 里捞出含关键字的整行（当证据贴给人看）。"""
    return next((line.strip() for line in (text or "").splitlines() if needle in line), "")


def make_entry(seconds: float = 0.0, description: str = None, facing: str = None) -> dict:
    """看帧.json 里的一条记录（判定可改，用来验指纹变不变）。"""
    judgment = json.loads(json.dumps(JUDGMENT, ensure_ascii=False))
    if description is not None:
        judgment["描述"] = description
    if facing is not None:
        judgment["body_orientation"]["facing"] = facing
    return {"秒": seconds, "文件": f"analysis/reverse/fake/看帧/{seconds:g}.png",
            "sha256": "f" * 64, "宽": FRAME_SIZE[0], "高": FRAME_SIZE[1],
            "判定": judgment, "模型": "gemini-3.8-flash", "时间": "2026-11-02 10:00:00"}


def make_payload(entries: list[dict]) -> dict:
    return {"id": "fake", "源视频": "fake.mp4",
            "源片尺寸": {"宽": FRAME_SIZE[0], "高": FRAME_SIZE[1]}, "帧": entries}


def run_extract_with(extract, outdir: Path, frames: str = "", judge=None, grab=None,
                     dissect=None):
    """跑一次 run_extract（模型调用与抽帧都换替身，不联网、不落真产物）。

    `judge` 只管单帧判定那一类调用（附件是 .png），`dissect` 只管拆解那一类。
    返回 (退出码, stdout, stderr, 拼给模型的提示词)。
    """
    captured: dict = {"prompt": ""}

    def default_judge(prompt, path):
        return json.dumps(JUDGMENT, ensure_ascii=False), {}, 0.1

    def default_dissect(prompt, path):
        return dissection_text(extract.LAYER_TITLES), {}, 0.1

    real = (extract.probe_video, extract.call_model, extract.load_api_key)

    def call_model(prompt, path, key, timeout=300.0):
        captured["prompt"] = prompt
        if str(path).lower().endswith(IMAGE_SUFFIXES):
            return (judge or default_judge)(prompt, path)
        return (dissect or default_dissect)(prompt, path)

    extract.probe_video = fake_probe
    extract.call_model = call_model
    extract.load_api_key = lambda: "test-key"
    module = extract.frame_facts_module()
    real_grab = module.grab_frame
    if grab is not None:
        module.grab_frame = grab
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = extract.run_extract(Path("fake.mp4"), outdir.name, outdir, 5.0, "", frames)
    finally:
        extract.probe_video, extract.call_model, extract.load_api_key = real
        module.grab_frame = real_grab
    return code, out.getvalue(), err.getvalue(), captured["prompt"]


# ------------------------------------------------------------------ ①-a 自动跑（成功）


def auto_run_ok(extract, tmp: Path) -> None:
    """空目录 + 假视频：拆解第一步自己去抽 0 秒那一帧、判一次、写 看帧.json。"""
    outdir = tmp / "auto-ok"
    outdir.mkdir(parents=True, exist_ok=True)
    judged: list[str] = []

    def judge(prompt, path):
        judged.append(str(path))
        return json.dumps(JUDGMENT, ensure_ascii=False), {}, 0.1

    code, out, err, prompt = run_extract_with(extract, outdir, judge=judge, grab=fake_grab)
    facts_path = outdir / "看帧.json"
    check(code == 0 and facts_path.is_file(),
          "①-a 空目录会自动跑 0 秒判定并写出 看帧.json", f"exit={code}｜{facts_path.name}")

    facts = json.loads(facts_path.read_text(encoding="utf-8")) if facts_path.is_file() else {}
    entry = extract._frame_entry(facts, 0.0) or {}
    judgment = entry.get("判定") if isinstance(entry.get("判定"), dict) else {}
    check(entry.get("秒") == 0.0 and bool(judgment.get("描述")),
          "①-a 写的是 0 秒那一条（带描述）", f"帧秒={[f.get('秒') for f in facts.get('帧') or []]}")
    check((entry.get("宽"), entry.get("高")) == FRAME_SIZE,
          "①-a 记录的帧尺寸 == 源片尺寸（原分辨率口径没丢）", f"{entry.get('宽')}×{entry.get('高')}")
    check(len(judged) == 5,
          "①-a 默认跑首帧窗口 0:0.5 的 3 帧 ＋ 尾帧窗（末 1.5 秒、2 帧），每帧各问一次", f"{len(judged)} 次")
    check("[看帧] 0:0.5 窗口 · 0 秒 → 侧对镜头（侧对）/ 直视镜头（1 次调用）" in out,
          "①-a 日志能看出「哪个窗口的哪一秒 → 判成什么、什么档、几次调用」",
          line_with(out, "[看帧] 0:0.5 窗口"))
    check("[看帧] 13.5:15 窗口 · 13.5 秒" in out,
          "①-a 尾帧窗也真跑了（末 1.5 秒、2 帧，给第十层当锚点）", line_with(out, "13.5:15"))
    check("## 看帧判定" in prompt and "class=" in prompt,
          "①-a 同一次拆解的提示词里注入了这一块（默认 fields 档，不是白跑）")


# ------------------------------------------------------------------ ①-b 自动跑失败


def auto_run_fail(extract, tmp: Path) -> None:
    """网络被阻断：调用确实发出去了，失败后不注入、不落盘、两处各留一行警告，拆解照跑。"""
    outdir = tmp / "auto-fail"
    outdir.mkdir(parents=True, exist_ok=True)
    attempted: list[str] = []

    def blocked(prompt, path):
        attempted.append(str(path))
        raise RuntimeError("网络被测试阻断")

    code, out, err, prompt = run_extract_with(extract, outdir, judge=blocked, grab=fake_grab)
    check(len(attempted) == 1, "①-b 自动判定确实发出去了（不是压根没跑）", f"{attempted}")
    check(not (outdir / "看帧.json").is_file(),
          "①-b 判失败不落 看帧.json（下次还能重试，不会被一次抖动钉死）")
    check("## 看帧判定" not in prompt, "①-b 失败后不注入（不拿半份判定改写拆解）")
    warn = [line for line in err.splitlines() if "看帧判定没跑成" in line]
    check(len(warn) == 1, "①-b stderr 正好一行警告", (warn or [""])[0][:90])
    check(code == 0 and (outdir / "拆解.md").is_file(),
          "①-b 拆解照跑（可选步骤失败不拖垮主流程）", f"exit={code}")
    document = (outdir / "拆解.md").read_text(encoding="utf-8") if (outdir / "拆解.md").is_file() else ""
    head = [line for line in document.splitlines()
            if line.startswith("> 看帧判定：") and "本份拆解没有看帧判定" in line]
    check(len(head) == 1, "①-b 产物头部留一行「本份拆解没有看帧判定（原因）」",
          (head or [""])[0][:90])


# ------------------------------------------------------------------ ② 已有判定不重复花钱


def window_payload(seconds=(0.0, 0.25, 0.5), facing="侧背") -> dict:
    """看帧.json 的一条完整记录：首帧窗口 0:0.5 × 3 帧 ＋ 尾帧窗 13.5:15 × 2 帧（默认要的两窗）。"""
    entries = [make_entry(s, facing=facing) for s in seconds]
    tail_entries = [make_entry(s, facing=facing) for s in (13.5, 14.95)]
    agg = {
        "描述": JUDGMENT["描述"], "描述秒": seconds[len(seconds) // 2],
        "confidence": "高", "分歧": [],
        "body_orientation": {"facing": facing, "facing_class": "侧背"},
        "gaze": JUDGMENT["gaze"], "people": JUDGMENT["people"],
        "shot_size": JUDGMENT["shot_size"],
    }
    tail_agg = dict(agg)
    tail_agg["描述秒"] = 14.95
    return {"id": "fake", "源视频": "fake.mp4",
            "源片尺寸": {"宽": FRAME_SIZE[0], "高": FRAME_SIZE[1]},
            "窗口": [{"窗口": "0:0.5", "起": 0.0, "止": 0.5, "n": len(seconds),
                      "帧": entries, "聚合": agg, "模型": "gemini-3.8-flash",
                      "时间": "2026-11-02 10:00:00"},
                     {"窗口": "13.5:15", "起": 13.5, "止": 15.0, "n": 2,
                      "帧": tail_entries, "聚合": tail_agg, "模型": "gemini-3.8-flash",
                      "时间": "2026-11-02 10:00:00"}],
            "帧": entries + tail_entries}


def no_repeat(extract, tmp: Path) -> None:
    """看帧.json 已经有**同一个窗口**的可用判定 → 一次都不许再调模型，连帧都不许再抽。"""
    outdir = tmp / "no-repeat"
    outdir.mkdir(parents=True, exist_ok=True)
    payload = window_payload()
    (outdir / "看帧.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                     encoding="utf-8", newline="\n")

    judged: list[str] = []

    def no_grab(*args, **kwargs):
        raise AssertionError("已经有判定了，不该再抽帧")

    def no_judge(prompt, path):
        judged.append(str(path))
        raise AssertionError("已经有判定了，不该再调模型判帧")

    code, out, err, prompt = run_extract_with(extract, outdir, judge=no_judge, grab=no_grab)
    check(code == 0, "② 已有 看帧.json 时拆解正常跑完", f"exit={code}")
    check(not judged, "② 没有额外的判定调用（不重复花钱）", f"判定调用 {judged}")
    check("[看帧] 已有 2 个窗口可用判定，不重复调用" in out,
          "② 日志说明「首帧窗＋尾帧窗都复用了已有判定」", line_with(out, "已有 2 个窗口可用判定"))
    check("## 看帧判定" in prompt and "gaze=" in prompt,
          "② 但既有判定照样注入提示词（复用的是事实，不是省事）")
    document = (outdir / "拆解.md").read_text(encoding="utf-8") if (outdir / "拆解.md").is_file() else ""
    head = [line for line in document.splitlines() if line.startswith("> 看帧判定：")]
    check(not head, "② 成功路径不在产物头部留警告", str(head))


def old_single_frame_is_not_reused(extract, tmp: Path) -> None:
    """旧单帧记录**不能**冒充 3 帧窗口：必须重新跑窗口（今天的真实坑）。

    2026-11-02 实测：`window_reusable` 原先允许「旧单帧落在同一起点」复用，结果
    pop-3-b3 这类今天之前拆过的任务 `ensure_facts` 报 calls=0 / reused=1，
    新窗口路径一次都没跑过，验证等于白做。
    """
    outdir = tmp / "old-single"
    outdir.mkdir(parents=True, exist_ok=True)
    payload = make_payload([make_entry(0.0)])          # 旧格式：扁平帧、无窗口、1 帧
    (outdir / "看帧.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                     encoding="utf-8", newline="\n")

    grabbed: list[str] = []
    calls = []

    def judge(prompt, path):
        calls.append(str(path))
        return json.dumps(JUDGMENT, ensure_ascii=False), {}, 0.1

    code, out, err, prompt = run_extract_with(extract, outdir, judge=judge, grab=fake_grab)
    check(code == 0, "②-b 旧单帧记录下拆解照跑", f"exit={code}")
    check(len(calls) == 5, "②-b 旧单帧不冒充窗口：重跑首帧窗 3 帧 ＋ 尾帧窗 2 帧", f"判定调用 {len(calls)}")
    check("[看帧] 已有 1 个窗口可用判定" not in out,
          "②-b 不再声称「已有可用判定」")


# ------------------------------------------------------------------ ③ --frames off


def frames_off(extract, tmp: Path) -> None:
    """`--frames off` = 回到旧行为：不自动跑，没有文件就不注入，也不报错。"""
    outdir = tmp / "frames-off"
    outdir.mkdir(parents=True, exist_ok=True)
    judged: list[str] = []

    def no_grab(*args, **kwargs):
        raise AssertionError("--frames off 时不许抽帧")

    def no_judge(prompt, path):
        judged.append(str(path))
        raise AssertionError("--frames off 时不许调模型判帧")

    code, out, err, prompt = run_extract_with(extract, outdir, frames="off", judge=no_judge, grab=no_grab)
    check(code == 0, "③ --frames off 时拆解正常跑完", f"exit={code}")
    check(not judged, "③ 一次判定也不调", f"判定调用 {judged}")
    check(not (outdir / "看帧.json").is_file(), "③ 不自动生成 看帧.json")
    check("## 看帧判定" not in prompt, "③ 提示词与旧版同款（没有注入块）")
    document = (outdir / "拆解.md").read_text(encoding="utf-8") if (outdir / "拆解.md").is_file() else ""
    head = [line for line in document.splitlines() if line.startswith("> 看帧判定：")]
    check(not head, "③ 用户主动关掉 → 不写成「失败」留痕", str(head))
    check("本次不自动跑" in out, "③ 日志写明「本次不自动跑」", line_with(out, "取帧规则"))


# ------------------------------------------------------------------ ④ 指纹


def fingerprint(analyze, tmp: Path) -> None:
    """看帧判定进指纹：改判定 → 变；只改时间戳 → 不变；写完指纹后下一次一样 → 不反复重拆。"""
    video = tmp / "fake-video.mp4"
    video.write_bytes(b"fake video bytes for fingerprint")
    outdir = tmp / "fp-task"
    outdir.mkdir(parents=True, exist_ok=True)
    facts_path = outdir / "看帧.json"

    empty = analyze.fingerprint_for(video, outdir)
    check(empty.get("看帧判定哈希") == "", "④ 没有 看帧.json 时摘要为空串", repr(empty.get("看帧判定哈希")))

    facts_path.write_text(json.dumps(make_payload([make_entry(0.0)]), ensure_ascii=False),
                          encoding="utf-8")
    first = analyze.fingerprint_for(video, outdir)
    digest = first.get("看帧判定哈希") or ""
    check(len(digest) == 64 and digest == analyze.frame_facts_digest(outdir),
          "④ 指纹里有 看帧.json 的内容摘要（sha256）", digest[:12] + "…")
    check(analyze.compare_frame_facts({}, first) == (False, "上次拆解时没有看帧判定，现在有了"),
          "④ 老指纹没有这个字段 → 后补的判定触发重拆")

    same = make_payload([make_entry(0.0)])
    same["帧"][0]["时间"] = "2026-11-03 09:30:00"          # 判定一字没改，只是重跑了一遍
    same["帧"][0]["文件"] = "analysis/reverse/fake/看帧/re-0.png"
    facts_path.write_text(json.dumps(same, ensure_ascii=False), encoding="utf-8")
    check(analyze.frame_facts_digest(outdir) == digest,
          "④ 判定一样、只有时间/路径变 → 摘要不变（不算变化）")
    check(analyze.compare_frame_facts(first, analyze.fingerprint_for(video, outdir))[0],
          "④ 因此不会因为「重跑了一遍同样的判定」而重拆")

    changed = make_payload([make_entry(0.0, facing="背对镜头")])
    facts_path.write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")
    moved = analyze.fingerprint_for(video, outdir)
    ok, why = analyze.compare_frame_facts(first, moved)
    check(moved.get("看帧判定哈希") != digest and not ok,
          "④ 改了判定（侧对 → 背对）→ 指纹变、触发重拆", why)

    facts_path.unlink()
    gone = analyze.compare_frame_facts(first, analyze.fingerprint_for(video, outdir))
    check(gone[0] is False and "没了" in gone[1], "④ 判定被删了 → 也算变化", gone[1])

    # 正常路径：B1 先生成，指纹记的是**跑完之后**的值 → 下一次算出来一样
    facts_path.write_text(json.dumps(make_payload([make_entry(0.0)]), ensure_ascii=False),
                          encoding="utf-8")
    written = dict(empty)                                        # 拆解前算的指纹（那时还没有文件）
    written["看帧判定哈希"] = analyze.frame_facts_digest(outdir)    # 跑完之后回写的值
    nxt = analyze.fingerprint_for(video, outdir)                  # 下一次开跑前算的值
    check(analyze.compare_frame_facts(written, nxt) == (True, ""),
          "④ 写完指纹后下一次算出来的一样（不反复重拆）")


# ------------------------------------------------------------------ ⑥ --frames 取值规则


def frames_rule(extract, tmp: Path) -> None:
    """`--frames` 的四个档位：off / 默认 / auto / 秒列表；认不出就当场退出（不去花错的钱）。"""
    empty = tmp / "rule-empty"
    empty.mkdir(parents=True, exist_ok=True)
    probe = fake_probe(None)

    check(extract.frames_of("off", empty, probe) == (None, "off"),
          "⑥ off → 不自动跑（None）")
    default_windows, default_note = extract.frames_of("", empty, probe)
    check([(window["窗口"], window["n"], window["帧秒"]) for window in default_windows]
          == [("0:0.5", 3, [0.0, 0.25, 0.5]), ("13.5:15", 2, [13.5, 14.95])],
          "⑥ 默认 → 首帧窗口 0:0.5、3 帧 ＋ 尾帧窗（末 1.5 秒、2 帧）", f"{default_note}")
    check(extract.frames_of("0,2.5,6", empty, probe)[0] == [0.0, 2.5, 6.0],
          "⑥ 秒列表 → 就这几秒单帧判一次，0 秒照样在")
    check(extract.frames_of("0,60", empty, probe)[0] == [0.0, 14.95],
          "⑥ 超出片长的秒数收到最后一帧（与 choose_times 同口径，不炸整次）")
    auto, auto_note = extract.frames_of("auto", empty, dict(fake_probe(None), hard_cuts=[3.0]))
    auto = auto or []
    check(auto[:1] and auto[0]["窗口"] == "0:0.5" and auto[0]["n"] == 2
          and any(window["起"] == 3.0 for window in auto) and len(auto) <= 12,
          "⑥ auto → 每个起点一个窗口、各 2 帧（≤12 个）", f"{auto_note}｜{[w['窗口'] for w in auto]}")
    try:
        extract.frames_of("abc", empty, probe)
        check(False, "⑥ 认不出的 --frames 必须报预检错")
    except extract.PrecheckError as exc:
        check("off" in str(exc) or "--frames" in str(exc),
              "⑥ 认不出的 --frames 报预检错（不当成默认值偷偷跑）", str(exc)[:70])

    real_probe = extract.probe_video
    extract.probe_video = fake_probe
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            code = extract.run_extract(Path("fake.mp4"), "bad-frames", empty, 5.0, "", "abc")
    finally:
        extract.probe_video = real_probe
    check(code == 2, "⑥ 坏参数走真入口 → 退出码 2，不调模型", f"exit={code}")


# ------------------------------------------------------------------ ⑤ 回归


def regression() -> None:
    suites = [("frame_facts_check", 107), ("recheck_check", 50), ("render_check", 22),
              ("template_regression", 20), ("gen_invoke_check", 22)]
    pattern = re.compile(r"(\d+)\s*PASS\s*/\s*(\d+)\s*FAIL")
    for name, expected in suites:
        proc = subprocess.run([sys.executable, str(ROOT / "tests" / f"{name}.py")],
                              cwd=str(ROOT), capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        text = (proc.stdout or "") + (proc.stderr or "")
        passed = failed = -1
        for line in reversed(text.splitlines()):
            found = pattern.search(line)
            if found:
                passed, failed = int(found.group(1)), int(found.group(2))
                break
        check(proc.returncode == 0 and failed == 0 and passed == expected,
              f"⑤ 回归 {name}：{expected} PASS / 0 FAIL",
              f"exit={proc.returncode}｜{passed} PASS / {failed} FAIL")


# ------------------------------------------------------------------ 主流程


def main() -> int:
    print("=" * 78)
    print("看帧判定 · 接线检查（B1 自动跑 / B2 指纹 / B3 回归；真实任务只读）")
    print(f"仓库：{ROOT}")
    print("=" * 78)

    extract = load_module("extract", EXTRACT)
    analyze = load_module("analyze", ANALYZE)

    tmp = Path(tempfile.mkdtemp(prefix="frame-facts-wiring-"))
    try:
        auto_run_ok(extract, tmp)
        auto_run_fail(extract, tmp)
        no_repeat(extract, tmp)
        old_single_frame_is_not_reused(extract, tmp)
        frames_off(extract, tmp)
        frames_rule(extract, tmp)
        fingerprint(analyze, tmp)
        regression()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    failed = [name for ok, name, _ in results if not ok]
    print("-" * 78)
    print(f"合计 {len(results)} 项：{len(results) - len(failed)} PASS / {len(failed)} FAIL")
    if failed:
        for name in failed:
            print(f"  FAIL：{name}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
