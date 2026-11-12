#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AT-17 B1 探针：拆解侧「八 声音」结构化落盘（任务书 v2.6 + extract + 门禁 D8 + 台词链）。

跑法：`wanrepro/` 下 `python -m automation.probe_at17_b1`（纯离线：无 HTTP、不调模型、
不跑 ffmpeg、零花钱；抽帧/时长换替身）。判据全对才 exit 0，任何一条 FAIL 如实打印并 exit 1
（判 returncode，禁子串判通过）。

判据：
  [1] 任务书 v2.6 合同（红线 24：十层标题一字不动，只改八 声音层行内指引）——
      frontmatter version=2.6；围栏正文里 `## ` 标题集合与 extract.LAYER_TITLES 逐字一致、
      无新增平级标题；八 声音层含 BGM 四字段锚点、音效点、说话人两类枚举（画外音/出镜者）、
      「无音轨／省字段不省层」、听感判优先（机器字段只作客观锚）；v2.5 那两处正例锚点仍在。
  [2] extract.parse_sound_layer（落盘与判据同一个函数）——结构化正例四字段/音效点/台词说话人
      全部读得出；缺字段进 bgm_missing；「无音轨」正文识别 no_audio；台词缺说话人如实报。
  [3] extract 无音轨片「省字段不省层」——八层正文空 → 补一行「无音轨」后十层照样齐；
      有音轨片正文空仍按缺层失败（行为不变）；连八层标题都漏的不替它造标题；
      补层不改十层标题数量。
  [4] dissect_gate D8——有音轨齐字段 PASS；缺 BGM.节奏 FAIL；台词缺说话人 FAIL；
      台词「无」片 PASS；音轨「无」片不触发 PASS；读不到音轨标记如实 WARN。
  [5] 门禁集成——D8 进 checks 清单（第 9 项）；破坏 BGM 字段后 verdict=FAIL 且诊断含 D8
      （重拆会把口径带给下一轮模型）。
  [6] 台词链四处联动——intent.parse_dialogue 按表头名定位说话人列（中间列不错位）、
      表头读不到回退表尾位置、三列旧格式历史兼容不崩；dialogue_markdown 出四列表；
      render.load_dialogue_rows 保留 speaker（不许只扩存储不消费）；意图建议.md 表头同步四列；
      rewrite_dialogue 用带说话人的行照样替换正文。
  [7] 真实任务只读（auto-0036/任务.json + 真实拆解.md SHA256 前后不变）+ 跑后零残留。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]          # wanrepro/
sys.path.insert(0, str(ROOT))
from automation import cards, frames, replace  # noqa: E402

PROMPT_PATH = ROOT / "prompts" / "视频拆解.md"
INTENT_PROMPT = ROOT / "prompts" / "意图建议.md"
FIXTURE_SRC = ROOT / "tests" / "fixtures" / "at15-r1"
FIX_NAMES = ("拆解.md", "待确认清单.md", "待确认清单.json", "意图建议.md")
REAL_TASK = ROOT / "automation" / "projects" / "auto-0036" / "任务.json"
REAL_DISC = ROOT / "analysis" / "reverse" / "1-105" / "拆解.md"
BRIDGE = "probe-at17-b1"
LOCAL = "auto-probe17"
DURATION = 6.04
PNG_A = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c626001000000ffff03000006000557bfabd4"
    "0000000049454e44ae426082")

WORK = Path(tempfile.mkdtemp(prefix="probe-at17-b1-"))
FIX = WORK / BRIDGE
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f" —— {detail}" if detail and not cond else ""))
    return bool(cond)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def module_by_path(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


EXTRACT = module_by_path("probe_at17_extract", ROOT / "pipeline" / "reverse" / "extract.py")
GATE = module_by_path("probe_at17_gate", ROOT / "pipeline" / "reverse" / "dissect_gate.py")
INTENT = module_by_path("probe_at17_intent", ROOT / "pipeline" / "reverse" / "intent.py")
# render.py 顶层 `from confirm import …`（同目录模块）：按路径加载前要把该目录放进 sys.path
sys.path.insert(0, str(ROOT / "pipeline" / "reverse"))
RENDER = module_by_path("probe_at17_render", ROOT / "pipeline" / "reverse" / "render.py")


# ---------------------------------------------------------------- 夹具与替身

def stub_ensure(video, at, out):
    """抽帧替身：不跑 ffmpeg，落 1×1 PNG。"""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(PNG_A)
    return True, ""


def _fix_file(bridge_id: str, name: str) -> Path:
    path = WORK / str(bridge_id) / name
    if not path.is_file():
        raise replace.ReplaceError(f"夹具缺文件 {path}")
    return path


class Patches:
    """隔离补丁组：ANALYSIS 双根 + 清单解析路径 + 抽帧/时长替身。"""

    def __enter__(self):
        self.ps = [
            patch.object(cards, "ANALYSIS", WORK),
            patch.object(frames, "ANALYSIS", WORK),
            patch.object(replace, "checklist_path", lambda bid: _fix_file(bid, "待确认清单.md")),
            patch.object(replace, "sidecar_path", lambda bid: _fix_file(bid, "待确认清单.json")),
            patch.object(frames, "_ensure", stub_ensure),
            patch.object(frames, "duration", lambda video=None: DURATION),
        ]
        for p in self.ps:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self.ps):
            p.stop()
        return False


def base_markdown() -> str:
    return (FIXTURE_SRC / "拆解.md").read_text(encoding="utf-8-sig")


def reset_fixture(markdown: str | None = None) -> None:
    FIX.mkdir(parents=True, exist_ok=True)
    for name in FIX_NAMES:
        shutil.copy2(FIXTURE_SRC / name, FIX / name)
    (FIX / "门禁.json").unlink(missing_ok=True)
    for leftover in FIX.glob("门禁诊断-*"):
        leftover.unlink()
    if markdown is not None:
        (FIX / "拆解.md").write_text(markdown, encoding="utf-8")


def with_sound_body(markdown: str, body: str, audio: str = "有") -> str:
    """把「八 声音」正文整体换掉（body 为空 = 正文空），并同步头部音轨标记。"""
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
    text = "\n".join(out) + "\n"
    return re.sub(r"音轨:\s*(有|无)", f"音轨: {audio}", text, count=1)


SOUND_OK = "\n".join((
    "- BGM.情绪：轻快",
    "- BGM.节奏：中速，末段渐弱",
    "- BGM.乐器/织体：木琴、轻柔钢琴、软合成音",
    "- BGM.强弱走向：轻起渐强收",
    "- 音效点：00:01-00:02 盖子拧开的一声「咔」",
    "- 音效点：00:05-00:06 收尾一声轻响",
    "- 人声语言：西班牙语",
    "- 台词：00:01-00:03｜「Hola」｜说话人：画外音",
    "- 台词：00:03-00:05｜「Mira esto」｜说话人：出镜者",
    "- 对位：鼓点重拍与剪辑点对齐",
))


# ---------------------------------------------------------------- [1] 任务书合同

def prompt_body() -> str:
    raw = PROMPT_PATH.read_text(encoding="utf-8")
    match = re.search(r"```[ \t]*\r?\n(.*?)\r?\n```", raw, re.DOTALL)
    return match.group(1) if match else raw


def layer_body(body: str, title: str) -> str:
    match = re.search(r"^##\s*" + re.escape(title) + r"\s*$(.*?)(?=^##\s|\Z)", body, re.M | re.S)
    return match.group(1) if match else ""


def probe_prompt_contract() -> None:
    print("[1] 任务书 v2.6 合同（十层标题不动，只改八 声音层）")
    raw = PROMPT_PATH.read_text(encoding="utf-8")
    check("frontmatter version=2.6",
          bool(re.search(r"^version:\s*2\.6\s*$", raw, re.M)),
          "frontmatter 没有 version: 2.6")
    body = prompt_body()
    found = [line.strip() for line in body.splitlines() if line.strip().startswith("## ")]
    titles = [f"## {title}" for title in EXTRACT.LAYER_TITLES]
    check("围栏正文十层标题逐字一致（顺序也不动）", found[:len(titles)] == titles,
          f"{found[:len(titles)]}")
    meta_tail = ["## 赛马实测记录（2026-10-29）", "## 接口纪律（踩过的坑）",
                 "## 下游怎么消费（渲染器不要碰 JSON 键名）"]
    check("没有新增平级标题（十层之外只有三个既有定版段）",
          found[len(titles):] == meta_tail, f"{found[len(titles):]}")
    check("{{镜头语言契约}} 占位原样保留",
          "{{镜头语言契约}}" in body)

    sound = layer_body(body, "八 声音")
    anchors = {
        "BGM.情绪": "情绪字段", "BGM.节奏": "节奏字段",
        "BGM.乐器/织体": "乐器织体字段", "BGM.强弱走向": "强弱走向字段",
        "音效点": "音效点数组", "台词：": "台词逐句行", "说话人": "说话人列",
        "画外音": "枚举·画外音", "出镜者": "枚举·出镜者",
        "无BGM": "无 BGM 缺省", "无音轨": "无音轨正文", "省字段不省层": "省字段不省层",
        "听感": "听感判优先", "客观锚": "机器字段只作客观锚",
    }
    missing = [label for key, label in anchors.items() if key not in sound]
    check(f"八 声音层 {len(anchors)} 个结构化锚点全部在场", not missing, f"缺 {missing}")
    check("八 声音层写明了说话人枚举只有两类",
          "只许这两个词" in sound and "画外音" in sound and "出镜者" in sound)
    check("八 声音层没引入新标题（正文里没有 `## `）",
          not any(line.strip().startswith("## ") for line in sound.splitlines()))

    # v2.5 那两处正例一字未动（红线 24 的另一种说法：别顺手改别的层）
    shots = layer_body(body, "七 分镜表")
    assets = layer_body(body, "九 资产")
    check("v2.5 七 分镜表末镜正例仍在（14 列一列不少）",
          "05 | 00:05-00:06 | 品牌字与产品" in shots)
    check("v2.5 九 资产画面位置锚仍在",
          "画面位置：九宫格N·镜M" in assets and "§9·画面位置" in assets)

    intent_raw = INTENT_PROMPT.read_text(encoding="utf-8")
    check("意图建议.md 台词改写表头同步四列（说话人列）",
          "| 起-止 | 原文 | 中文 | 说话人 |" in intent_raw)


# ---------------------------------------------------------------- [2] 八声音解析

def probe_parse_sound() -> None:
    print("[2] extract.parse_sound_layer：结构化字段读得出、缺项如实报")
    sound = EXTRACT.parse_sound_layer(SOUND_OK)
    check("BGM 四字段齐且值正确",
          not sound["bgm_missing"]
          and sound["bgm"]["BGM.情绪"] == "轻快"
          and sound["bgm"]["BGM.乐器/织体"] == "木琴、轻柔钢琴、软合成音",
          f"{sound['bgm']}")
    check("音效点 2 条（时段 + 一句声源）", len(sound["sound_points"]) == 2,
          f"{sound['sound_points']}")
    check("台词 2 句且说话人分别是 画外音/出镜者",
          [row["speaker"] for row in sound["dialogue"]] == ["画外音", "出镜者"],
          f"{sound['dialogue']}")
    check("语种读出（西班牙语）", sound["spoken_language"] == "西班牙语")

    no_bgm = EXTRACT.parse_sound_layer(SOUND_OK.replace("- BGM.节奏：中速，末段渐弱\n", ""))
    check("缺 BGM.节奏 → bgm_missing 精确点名",
          no_bgm["bgm_missing"] == ["BGM.节奏"], f"{no_bgm['bgm_missing']}")

    no_speaker = EXTRACT.parse_sound_layer(
        SOUND_OK.replace("｜说话人：出镜者", ""))
    check("台词缺说话人 → dialogue_missing_speaker 如实报",
          no_speaker["dialogue_missing_speaker"] == ["Mira esto"],
          f"{no_speaker['dialogue_missing_speaker']}")

    all_none = EXTRACT.parse_sound_layer("\n".join((
        "- BGM.情绪：无BGM", "- BGM.节奏：无BGM",
        "- BGM.乐器/织体：无BGM", "- BGM.强弱走向：无BGM",
        "- 音效点：无", "- 人声语言：无", "- 台词：无")))
    check("无 BGM 片：四字段写 无BGM 即算齐（不逼模型留空）",
          not all_none["bgm_missing"] and all_none["no_dialogue"])
    check("无音轨正文 → no_audio（不是缺字段）",
          EXTRACT.parse_sound_layer("无音轨")["no_audio"] is True)

    # 散文里的音效点（时段 + 「拟声词」）不许被台词链当成台词（intent 侧口径）
    disc = "## 八 声音\n" + SOUND_OK + "\n"
    rows = INTENT.dialogue_rows(disc)
    check("台词链只认 `台词：` 行：音效点里的拟声词不进台词表",
          [row["original"] for row in rows] == ["Hola", "Mira esto"], f"{rows}")
    check("台词链带出说话人（画外音/出镜者）",
          [row["speaker"] for row in rows] == ["画外音", "出镜者"], f"{rows}")


# ---------------------------------------------------------------- [3] 无音轨补层

def probe_no_audio_layer() -> None:
    print("[3] 无音轨片：省字段不省层（十层标题与正文保留）")
    base = base_markdown()
    titles_before = [line.strip() for line in base.splitlines()
                     if line.strip().startswith("## ")]

    empty_no_audio = with_sound_body(base, "", audio="无")
    filled, action = EXTRACT.ensure_sound_layer(empty_no_audio, False)
    check("无音轨 + 八层正文空 → 动作 filled", action == "filled", action)
    check("补进去的就是「无音轨」正文",
          EXTRACT.sound_body(filled).strip() == EXTRACT.NO_AUDIO_TEXT,
          f"{EXTRACT.sound_body(filled)!r}")
    try:
        EXTRACT.require_layers(filled)
    except EXTRACT.LayerError as exc:              # noqa: BLE001 —— 真缺层要看清是哪层
        ok = False
        detail = f"{exc.missing}"
    else:
        ok, detail = True, ""
    check("补层后十层齐全（require_layers 通过）", ok, detail)
    titles_after = [line.strip() for line in filled.splitlines()
                    if line.strip().startswith("## ")]
    check("补层不改十层标题数量/顺序", titles_after == titles_before,
          f"{titles_before} → {titles_after}")

    # 无音轨片模型照样写了声音内容（实测会编）→ 改写成「无音轨」，不静默采信
    guessed = with_sound_body(base, SOUND_OK, audio="无")
    replaced, action2 = EXTRACT.ensure_sound_layer(guessed, False)
    check("无音轨 + 模型编了声音内容 → 动作 replaced",
          action2 == "replaced", action2)
    check("改写后正文只剩「无音轨」（不含编出来的 BGM/台词）",
          EXTRACT.sound_body(replaced).strip() == EXTRACT.NO_AUDIO_TEXT
          and "BGM.情绪" not in replaced and "「Hola」" not in replaced,
          f"{EXTRACT.sound_body(replaced)!r}")
    check("改写后十层标题与正文层数不变（不删层）",
          [line.strip() for line in replaced.splitlines()
           if line.strip().startswith("## ")] == titles_before)

    empty_with_audio = with_sound_body(base, "", audio="有")
    unchanged, action3 = EXTRACT.ensure_sound_layer(empty_with_audio, True)
    check("有音轨片正文空 → 一个字不改（不替模型编声音）",
          action3 == "" and unchanged == empty_with_audio)
    try:
        EXTRACT.require_layers(unchanged)
    except EXTRACT.LayerError:
        ok2 = True
    else:
        ok2 = False
    check("有音轨片正文空仍按缺层失败（缺层警告行为不变）", ok2)

    no_heading = "\n".join(line for line in empty_no_audio.splitlines()
                           if not re.match(r"^#{1,6}\s*八 声音\s*$", line)) + "\n"
    kept, action4 = EXTRACT.ensure_sound_layer(no_heading, False)
    check("连八层标题都漏的 → 不补（不替它造标题）",
          action4 == "" and kept == no_heading)
    try:
        EXTRACT.require_layers(kept)
    except EXTRACT.LayerError:
        ok3 = True
    else:
        ok3 = False
    check("缺标题仍如实判缺层（不静默放行）", ok3)

    already = with_sound_body(base, "无音轨", audio="无")
    same, action5 = EXTRACT.ensure_sound_layer(already, False)
    check("正文已写「无音轨」→ 原样不动", action5 == "" and same == already)


# ---------------------------------------------------------------- [4] D8 判定

def probe_gate_d8() -> None:
    print("[4] dissect_gate D8：有音轨判四字段/说话人，无音轨不触发")
    full = with_sound_body(base_markdown(), SOUND_OK, audio="有")
    verdict, msg = GATE.check_sound(full, EXTRACT)
    check("有音轨 + 齐字段 → PASS", verdict == GATE.P, f"{verdict}｜{msg}")

    verdict, msg = GATE.check_sound(
        with_sound_body(base_markdown(), SOUND_OK.replace("- BGM.节奏：中速，末段渐弱\n", ""),
                        audio="有"), EXTRACT)
    check("有音轨 + 缺 BGM.节奏 → FAIL 并点名字段",
          verdict == GATE.F and "BGM.节奏" in msg, f"{verdict}｜{msg}")

    verdict, msg = GATE.check_sound(
        with_sound_body(base_markdown(), SOUND_OK.replace("｜说话人：出镜者", ""), audio="有"),
        EXTRACT)
    check("有音轨 + 台词缺说话人 → FAIL", verdict == GATE.F and "说话人" in msg,
          f"{verdict}｜{msg}")

    verdict, msg = GATE.check_sound(
        with_sound_body(base_markdown(), "\n".join((
            "- BGM.情绪：轻快", "- BGM.节奏：中速", "- BGM.乐器/织体：木琴",
            "- BGM.强弱走向：轻起渐强收", "- 音效点：无",
            "- 人声语言：无", "- 台词：无")), audio="有"), EXTRACT)
    check("有音轨 + 无台词片（台词：无）→ PASS（B0：无台词→无列）",
          verdict == GATE.P, f"{verdict}｜{msg}")

    verdict, msg = GATE.check_sound(with_sound_body(base_markdown(), "无音轨", audio="无"), EXTRACT)
    check("无音轨片 → PASS（本项不触发）", verdict == GATE.P and "不触发" in msg,
          f"{verdict}｜{msg}")

    no_header = re.sub(r"<!--.*?-->\n", "", with_sound_body(base_markdown(), SOUND_OK))
    verdict, msg = GATE.check_sound(no_header, EXTRACT)
    check("读不到音轨标记 → WARN 如实告警（不假装通过）", verdict == GATE.W,
          f"{verdict}｜{msg}")


# ---------------------------------------------------------------- [5] 门禁集成

def probe_gate_integration() -> None:
    print("[5] 门禁集成：D8 进 checks 清单、破坏后 verdict=FAIL 且诊断带口径")
    with Patches():
        reset_fixture()
        result = GATE.evaluate(FIX, bridge_id=BRIDGE, local_id=LOCAL)
        ids = [item["id"] for item in result["checks"]]
        d8 = next((item for item in result["checks"] if item["id"] == "D8 八声音"), None)
        check("D8 进 checks 清单（第 9 项）", len(ids) == 9 and ids[-1] == "D8 八声音",
              f"{ids}")
        check("好产物 D8=PASS 且整体 verdict=PASS",
              d8 is not None and d8["verdict"] == GATE.P and result["verdict"] == GATE.P,
              f"{d8}｜{result['verdict']}｜{result['缺项']}")

        broken = with_sound_body(base_markdown(),
                                 SOUND_OK.replace("- BGM.强弱走向：轻起渐强收\n", ""), audio="有")
        reset_fixture(broken)
        result = GATE.evaluate(FIX, bridge_id=BRIDGE, local_id=LOCAL)
        fails = {item["id"].split()[0] for item in result["checks"] if item["verdict"] == GATE.F}
        check("缺 BGM.强弱走向 → verdict=FAIL 且缺项含 D8",
              result["verdict"] == GATE.F and "D8" in fails, f"{result['缺项']}")
        diagnosis = GATE.build_diagnosis(result)
        check("重拆诊断带 D8 口径（下一轮模型知道怎么改）",
              "D8" in diagnosis and "BGM.强弱走向" in diagnosis, diagnosis[:200])

        legacy = with_sound_body(
            base_markdown(),
            "全片无真人台词；背景音乐为中速轻快的流行 R&B，伴随鼓点律动。", audio="有")
        reset_fixture(legacy)
        result = GATE.evaluate(FIX, bridge_id=BRIDGE, local_id=LOCAL)
        fails = {item["id"].split()[0] for item in result["checks"] if item["verdict"] == GATE.F}
        check("v2.5 自由散文老产物 → D8 判 FAIL（新合同对旧正文不留后门）",
              "D8" in fails, f"{result['缺项']}")


# ---------------------------------------------------------------- [6] 台词链

def probe_dialogue_chain() -> None:
    print("[6] 台词链四处：表头定位 / 表尾回退 / 三列兼容 / 渲染消费")
    disc = "## 八 声音\n" + SOUND_OK + "\n"
    legacy_disc = ("## 八 声音\n全片无口播。\n- 00:01-00:03 「Hola」\n- 00:03-00:05 「Mira esto」\n")

    head = "## 必须换\n## 建议准备\n## 可沿用\n## 台词改写\n"
    raw4 = head + "\n".join((
        "| 起-止 | 原文 | 中文 | 说话人 |", "|---|---|---|---|",
        "| 00:01-00:03 | Hola | 你好 | 画外音 |",
        "| 00:03-00:05 | Mira esto | 看这个 | 出镜者 |"))
    rows, stats = INTENT.parse_dialogue(raw4, disc)
    check("四列表：两句都留下且说话人落进行对象",
          [row["speaker"] for row in rows] == ["画外音", "出镜者"], f"{rows}")
    check("统计里数得出带说话人的句数", stats["speakers"] == 2, f"{stats}")

    raw_mid = head + "\n".join((
        "| 起-止 | 原文 | 说话人 | 中文 |", "|---|---|---|---|",
        "| 00:01-00:03 | Hola | 出镜者 | 你好 |",
        "| 00:03-00:05 | Mira esto | 画外音 | 看这个 |"))
    rows, _stats = INTENT.parse_dialogue(raw_mid, disc)
    check("说话人列在中间：按表头名定位，不错位不吃掉中文",
          [row["speaker"] for row in rows] == ["出镜者", "画外音"]
          and [row["chinese"] for row in rows] == ["你好", "看这个"], f"{rows}")

    raw_nohead = head + "\n".join((
        "| 00:01-00:03 | Hola | 你好 | 画外音 |",
        "| 00:03-00:05 | Mira esto | 看这个 | 出镜者 |"))
    rows, _stats = INTENT.parse_dialogue(raw_nohead, disc)
    check("表头读不到 → 回退表尾位置读说话人（第四列）",
          [row["speaker"] for row in rows] == ["画外音", "出镜者"], f"{rows}")

    raw3 = head + "\n".join((
        "| 起-止 | 原文 | 中文 |", "|---|---|---|",
        "| 00:01-00:03 | Hola | 你好 |",
        "| 00:03-00:05 | Mira esto | 看这个 |"))
    rows, stats = INTENT.parse_dialogue(raw3, legacy_disc)
    check("三列旧格式（无说话人列）历史兼容：两句照收、speaker 为空",
          len(rows) == 2 and all(row["speaker"] == "" for row in rows), f"{rows}")
    check("旧格式不再多出来缺失句（missing 为空）", stats["missing"] == [], f"{stats['missing']}")

    md = INTENT.dialogue_markdown(rows)
    check("台词改写.md 表头是四列（说话人列在表尾）", "| 起-止 | 原文 | 中文 | 说话人 |" in md,
          md.splitlines()[:4])

    path = WORK / "dialogue-4.md"
    INTENT.write_dialogue(WORK, INTENT.parse_dialogue(raw4, disc)[0])
    path = WORK / INTENT.DIALOGUE_NAME
    loaded = RENDER.load_dialogue_rows(path)
    check("render.load_dialogue_rows 保留 speaker（不只扩存储）",
          [row["speaker"] for row in loaded] == ["画外音", "出镜者"], f"{loaded}")

    legacy_path = WORK / "dialogue-legacy.md"
    legacy_path.write_text(
        "# 台词改写\n\n| 起-止 | 原文 | 中文 |\n|---|---|---|\n"
        "| 00:01-00:03 | Hola | 你好 |\n| 00:03-00:05 | Mira esto | 看这个 |\n",
        encoding="utf-8")
    legacy_rows = RENDER.load_dialogue_rows(legacy_path)
    check("三列旧文件照样解析（speaker 为空、不崩）",
          len(legacy_rows) == 2 and all(row["speaker"] == "" for row in legacy_rows),
          f"{legacy_rows}")

    prompt = "正文一句 Hola，又一句 Mira esto。"
    new_text, hits, misses = INTENT.rewrite_dialogue(prompt, loaded)
    check("render 侧替换照旧生效（带说话人的行不挡替换）",
          hits == 2 and not misses and "你好" in new_text and "看这个" in new_text,
          f"{hits}｜{misses}｜{new_text}")
    lang = RENDER.spoken_language(EXTRACT.sound_body(disc))
    check("渲染侧读得出语种（西班牙语）", lang == "西班牙语", lang)
    check("`人声语言：无` 不当语种（无台词片不会被改成「中文」）",
          RENDER.spoken_language("- 人声语言：无") == ""
          and INTENT.source_language("## 八 声音\n- 人声语言：无\n") == ""
          and RENDER.rewrite_spoken_language("人声语言：无", "") == ("人声语言：无", False))


# ---------------------------------------------------------------- [7] 只读与残留

def probe_readonly(before: dict[str, str]) -> None:
    print("[7] 真实数据只读 + 零残留")
    after = {str(path): sha256(path) for path in (REAL_TASK, REAL_DISC) if path.is_file()}
    changed = [path for path, digest in before.items() if after.get(path) != digest]
    check("真数据只读（auto-0036/任务.json + 真实拆解.md SHA256 不变）", not changed,
          f"变了：{changed}")


def main() -> int:
    print("probe_at17_b1 · 拆解侧八声音结构化（离线零花钱）")
    watch = [path for path in (REAL_TASK, REAL_DISC) if path.is_file()]
    before = {str(path): sha256(path) for path in watch}
    try:
        probe_prompt_contract()
        probe_parse_sound()
        probe_no_audio_layer()
        probe_gate_d8()
        probe_gate_integration()
        probe_dialogue_chain()
        probe_readonly(before)
    finally:
        shutil.rmtree(WORK, ignore_errors=True)
        check("跑后零残留（临时夹具根已删）", not WORK.exists(), str(WORK))
    passed = sum(1 for _name, ok, _detail in RESULTS if ok)
    fails = [name for name, ok, _detail in RESULTS if not ok]
    print(f"\nprobe_at17_b1: {passed}/{len(RESULTS)} PASS"
          + (f"｜FAIL：{'；'.join(fails)}" if fails else ""))
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
