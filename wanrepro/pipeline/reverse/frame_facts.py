#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""看帧判定：取一个窗口里的几帧、每帧各问一次，再按字段机械聚合。

为什么要有它
------------
拆解是一次调用同时要十层 + 5 镜 × 13 列 + 首帧行（extract.py 把整段 mp4 的 base64 一次
交给模型，模型自己按 ~1fps 抽帧并内部降分辨率）。同一条片子的 0 秒帧，人看一张原分辨率
静帧就能说清「她身体侧对镜头、头回转、眼睛看着镜头」，拆解却写成「背对镜头」——人物朝向
在几百个字段里只是一个从句，没有任何专项判定与校验。方案见
docs/archive/20261103/方案-看帧判定-20261102.md。

本模块做三件事，都归在「机器能测的事实」名下：

1. ffmpeg 按秒抽帧，**原分辨率、不缩放**（判据 F2：尺寸必须等于源片，不是缩略图）；
2. **窗口内多帧各问一次**：`--window 0:0.5 --n 3` = 0 / 0.25 / 0.5 三帧，每帧一次 VLM
   调用，一帧只问这一帧，产结构化字段 + 一句能直接进提示词的「描述」（描述不是字段拼接：
   它要写清在做什么、眼神落在哪、周围什么光景）；
3. **机械聚合**（见 aggregate）：朝向档 / 视线 / 人数 / 景别逐字段数。全部一致 → 高；
   有分歧 → 低、该字段写「歧」、各帧说法按秒列进 `分歧`——不合成、不平均、也不再用模型
   做一次「总结」。`描述` 取信息量最大的一帧，并在 `描述秒` 标明来自哪一秒。

为什么不是「同一帧反复问」
--------------------------
2026-11-02 实测：同一帧连问两次，答案在「侧对 / 侧背」之间摆，问得越多越不知道该信哪次；
换成 0.00 / 0.25 / 0.50 三帧各问一次，三帧都给「侧背」+「直视镜头」，一致，描述还更全
（多出左臂甩出、右手持手机在臀后）。所以置信度不再由「同一帧问了几次」决定，而是
「同一段窗口里不同时刻的说法一不一致」。`--votes` 只为兼容旧命令行保留，默认 1。

调用层复用 pipeline/reverse/extract.py 的 call_model()（直连、重试、空响应判失败、密钥
redact），不在这里另写一套 HTTP。

产物
----
    analysis/reverse/<id>/看帧/<秒>.png      每帧一张，原分辨率
    analysis/reverse/<id>/看帧.json          每个窗口一条：
                                             {"窗口":"0:0.5","帧":[{秒,文件,sha256,判定}],"聚合":{…}}
                                             外加扁平的「帧」数组，老读者照读（向后兼容）

用法
----
    py -3 pipeline/reverse/frame_facts.py --id <id>                       # 首帧窗口 0:0.5，3 帧
    py -3 pipeline/reverse/frame_facts.py --id <id> --window 0:0.5 --n 3  # 明写窗口（只跑这一个）
    py -3 pipeline/reverse/frame_facts.py --id <id> --times 0,2.5,6       # 老口径：这几个秒各判一帧
    py -3 pipeline/reverse/frame_facts.py --id <id> --json                # 一行 JSON 给界面读
    py -3 pipeline/reverse/frame_facts.py --id <任务目录>                  # 也认目录路径（测试夹具用）

退出码：0 写盘成功｜2 预检失败（任务 / 视频 / 密钥 / ffmpeg / 帧尺寸与源片不符）
        3 一个窗口都没判成功（全失败也已如实写进 看帧.json，退出码只是告警）
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from datetime import datetime

REPO_ROOT = Path(__file__).resolve().parents[2]
EXTRACT_PATH = Path(__file__).resolve().parent / "extract.py"

ANALYSIS = REPO_ROOT / "analysis" / "reverse"
FRAME_DIR = "看帧"
FACTS_NAME = "看帧.json"
DISSECTION_NAME = "拆解.md"
FINGERPRINT_NAME = ".源视频指纹.json"

DEFAULT_MAX_SHOTS = 8
DEFAULT_MAX_FRAMES = 12
DEFAULT_TIMEOUT = 180.0
DEFAULT_WINDOW = "0:0.5"              # 首帧窗口：0 秒起 0.5 秒（避开片头黑帧/淡入的那一瞬）
DEFAULT_N = 3                         # 窗口内均匀取几帧（含首尾）
DEFAULT_WINDOW_WIDTH = 0.5            # 多秒 / auto 模式：每个起点往后开多宽的窗口
DEFAULT_VOTES = 1                     # 单帧只问一次；同一帧反复问不再作为置信度依据
MAX_VOTES = 3                         # 只为兼容旧命令行保留
PARSE_ATTEMPTS = 2                     # 解析失败再问一次；两次都不行就记「低」
MODEL_LABEL = "gemini-3.8-flash"

# 判定 JSON 必须有的字段：缺一个就当这次没判成（重试一次，再失败记低）
REQUIRED_KEYS = ("people", "subject", "body_orientation", "gaze", "posture_action",
                 "shot_size", "confidence", "描述")
CONFIDENCE_LEVELS = ("高", "中", "低")

# 聚合时逐字段数的四样：都是「一眼能定」的事实，也是进提示词的那几个字段
AGG_FIELDS = ("facing_class", "gaze", "people", "shot_size")
FIELD_LABELS = {"facing_class": "朝向档", "gaze": "视线", "people": "人数", "shot_size": "景别"}
COUNT_WORDS = {1: "一", 2: "两", 3: "三", 4: "四", 5: "五", 6: "六", 7: "七", 8: "八"}

CLOCK_RE = re.compile(r"(\d{1,2}):(\d{2})(?::(\d{2}))?(?:\.(\d{1,3}))?")
WINDOW_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*[:：~\-–]\s*(\d+(?:\.\d+)?)\s*[sS]?\s*$")
FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)
LAYER_RE = re.compile(r"^\s*#{1,6}\s*([一二三四五六七八九十])(?:\s|$)")


class FrameFactsError(RuntimeError):
    """一句话能说清的错误：打到 stderr，退出码 2。"""


# ---------------------------------------------------------------- 调用层（复用 extract）


_EXTRACT = None


def extract_module():
    """按路径导入 extract.py：那里有唯一一份 VLM 调用层与 ffprobe 预检。"""
    global _EXTRACT
    if _EXTRACT is None:
        spec = importlib.util.spec_from_file_location("_frame_facts_extract", EXTRACT_PATH)
        if spec is None or spec.loader is None:
            raise FrameFactsError(f"导不进 {EXTRACT_PATH}：看帧判定没法调模型")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _EXTRACT = module
    return _EXTRACT


# ---------------------------------------------------------------- 判定提示词

JUDGE_PROMPT = """\
这是同一条参考视频里 **第 {t} 秒** 的那一帧，原分辨率 {w}×{h}，你手上只有这一张图。
你只回答这一帧里看得见的东西。

## 一、先看人
- 有几个人；站在画面的哪个位置（左/中/右，前/中/后景），占画面多大。
- 身体怎么摆，分两层写：
  ① `body_orientation.facing`：用你自己的话把这个角度说清，别硬塞进三个词里。
     「正对镜头」「侧对镜头」「3/4 侧面、略背向」「侧背对镜头」「背对镜头」都可以——
     写成你眼睛看见的那个姿势。
  ② `body_orientation.facing_class`：再从下面五个里挑一个，只写这一个词：
     `正对` / `侧对` / `侧背` / `背对` / `不确定`。挑的时候数三样东西：
     - 正对：胸口整片可见，两侧肩头都在，看不见背脊；
     - 侧对：一侧肩线与腰侧轮廓清楚，胸口只露一半，背脊只在边缘一闪；
     - 侧背：主要看见后背与肩胛，背脊线清楚，胸口只剩一条边；
     - 背对：整片后背对着镜头，胸口完全看不见；
     - 不确定：这三样里有两样看不清，或者角度正好卡在两条之间。
     拿不准就写「不确定」——写不确定不扣分，硬猜一个才让人没法用。
- 背朝画面的哪一侧：侧对、侧背或背对时，写清后背朝着画面的左边还是右边，填进
  `body_orientation.back_toward`（写「画面左侧」或「画面右侧」）。
- 头：朝哪边，有没有回转，回转后脸朝哪里。
- 眼睛落在哪里：直直看进镜头，还是看向画面里的某个方向、画外、低头、闭眼、看不清。
- 手：两只手各在做什么，动作停在哪一拍（正发力 / 抬到一半 / 已经放下）。
- 景别：远景 / 全景 / 中景 / 近景 / 特写 / 极特写；机位是平视、仰拍还是俯拍。
- 画面里还有什么：显眼的物件、背景、颜色。
看不清就写「看不清」，不要替我补一个合理的样子；道具和材质不要猜名字，写形状、颜色、位置就好。

## 二、再写一句「描述」
把上面看到的东西写成 **一句能直接放进视频生成提示词的话**，按这个顺序一口气说完：
1. 这是哪儿、光从哪来——硬还是软、什么颜色、落在哪面墙上；
2. 这个人在画面的什么位置、身体怎么摆；
3. 手和肩在做什么：是抬起、握拳、伸出去、扶着还是垂着，动作停在哪一拍；
4. 眼睛落在哪；
5. 这一帧给人什么感觉。
写成一个连贯的句子，像在跟摄影师讲这一帧该怎么拍；不要写成「朝向：…；视线：…」这样的
字段罗列。这句话是给生成模型看的，它照着这句话要能看见这一帧——人、动作、眼神、光与
氛围，一样都不能少。

## 三、画面里的文字（第十层「图文与收束」要用）
- 只抄这一帧里**看得见的持久性文字/图形标识**（品牌字样、标语、字幕、角标、水印、
  包装上的字），填进 `text_elements` 数组，每条四个字段：
  `{"文字": "照抄画面原字（含大小写、标点；读不清写「部分可辨：<能读出的部分>」）",
    "位置": "画面哪里（顶部居中／瓶身右侧纵向…）",
    "形态": "衬线与否、粗细、颜色、与画面的相对大小",
    "动效": "单帧看不出动效就写 无"}`
- 这一帧没有任何持久性文字就写空数组 `[]`。
- 只照抄看得见的字，**不判断这是什么牌子**、不写品牌含义来历；只在这一帧一闪而过、
  不停留的文字不登记。

## 四、输出格式
只输出一个 JSON 对象（不要 ``` 围栏，不要解释）。下面每个值都只是**格式示范**，
一律按这一帧的真实内容填，尤其「描述」不许照抄示范句：
{
  "frame": {t},
  "people": 1,
  "subject": "年轻女性",
  "position": {"where": "画面中央偏右", "height_ratio": "占画面高度约3/4"},
  "body_orientation": {"facing": "侧背对镜头", "facing_class": "侧背", "back_toward": "画面右侧"},
  "head_orientation": "向左回转，面向镜头",
  "gaze": "直视镜头",
  "posture_action": "右手抬到胸前握拳，左臂垂在体侧，双脚一前一后",
  "shot_size": "中景",
  "objects": ["橙色座椅", "车厢门"],
  "text_elements": [{"文字": "照抄画面原字", "位置": "顶部居中", "形态": "无衬线粗体大字", "动效": "无"}],
  "confidence": "高",
  "描述": "午后窗边的逆光里，她侧身站着又回头看向镜头，右手把杯子举到胸口，桌面落着一层暖黄的光。"
}
没有人物时 people 写 0、subject 写「无人物」，body_orientation / gaze / posture_action 写「—」；
画面里没有持久性文字时 text_elements 写 []。
confidence 只写 高 / 中 / 低——看不准就写低，别硬编。"""


def judge_prompt(seconds: float, width: int, height: int) -> str:
    """一帧的提示词：只问这一帧，只问看到什么，另要一句能进提示词的描述。"""
    return (JUDGE_PROMPT
            .replace("{t}", f"{seconds:g}")
            .replace("{w}", str(int(width or 0)))
            .replace("{h}", str(int(height or 0))))


# ---------------------------------------------------------------- 解析


def parse_judgment(text: str) -> dict:
    """模型正文 → 判定 dict。缺字段 / 不是 JSON 一律抛 ValueError（不许当结果存下）。"""
    raw = (text or "").strip()
    fence = FENCE_RE.search(raw)
    if fence:
        raw = fence.group(1).strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("正文里没有 JSON 对象")
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON 解析失败：{exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("JSON 顶层不是对象")

    missing = [key for key in REQUIRED_KEYS if key not in data]
    if missing:
        raise ValueError("缺字段：" + "、".join(missing))

    body = data.get("body_orientation")
    if isinstance(body, str):
        body = {"facing": body, "back_toward": ""}
    elif not isinstance(body, dict):
        body = {}
    reader = extract_module()
    facing = str(body.get("facing") or "").strip()
    stated = str(body.get("facing_class") or "").strip()
    data["body_orientation"] = {
        "facing": facing,
        "facing_class": stated if stated in reader.FACING_CLASSES
        else reader.facing_class(facing, field=True),
        "back_toward": str(body.get("back_toward") or "").strip(),
    }

    objects = data.get("objects")
    if isinstance(objects, str):
        data["objects"] = [objects]
    elif not isinstance(objects, list):
        data["objects"] = []

    people = data.get("people")
    if isinstance(people, str) and people.strip().isdigit():
        data["people"] = int(people.strip())
    elif not isinstance(people, (int, float)):
        data["people"] = None

    confidence = str(data.get("confidence") or "").strip()
    data["confidence"] = confidence if confidence in CONFIDENCE_LEVELS else "中"
    data["描述"] = str(data.get("描述") or "").strip()
    if not data["描述"]:
        raise ValueError("描述为空（这一句是给生成模型看的，不许空着交）")

    # 第十层「图文与收束」的判定字段，**两态**（2026-11-04 路 A 审计 B6）：
    #   模型显式给了数组（含空数组）＝已判——空数组就是「看了，真没字」，照旧落键；
    #   **漏答键或结构非法（字符串/数字/null）＝未判——不落这个键**，聚合端据此不设
    #   「图文已判」，注入端就造不出「机器逐帧看过、没有文字」的伪事实。
    raw_texts = data.get("text_elements")
    if isinstance(raw_texts, dict):
        raw_texts = [raw_texts]
    if isinstance(raw_texts, list):
        elements = []
        for item in raw_texts:
            if isinstance(item, str):
                item = {"文字": item}
            if not isinstance(item, dict):
                continue
            text = str(item.get("文字") or "").strip()
            if not text:
                continue
            elements.append({
                "文字": text,
                "位置": str(item.get("位置") or "").strip(),
                "形态": str(item.get("形态") or "").strip(),
                "动效": str(item.get("动效") or "").strip(),
            })
        data["text_elements"] = elements
    elif "text_elements" in data:
        del data["text_elements"]     # 结构非法＝未判：摘掉残值，不能让聚合误认作答
    return data


def failed_judgment(reason: str, raw: str = "") -> dict:
    """判不成的那一帧也要留痕：confidence=低 + 原因 + 原文摘要，绝不静默写空。"""
    judgment = {
        "people": None,
        "subject": "",
        "body_orientation": {"facing": "", "facing_class": "不确定", "back_toward": ""},
        "head_orientation": "",
        "gaze": "",
        "posture_action": "",
        "shot_size": "",
        "objects": [],
        "text_elements": [],
        "confidence": "低",
        "描述": "",
        "错误": reason,
    }
    if raw:
        judgment["原文摘要"] = raw[:600]
    return judgment


# ---------------------------------------------------------------- 窗口：区间 → 要抽的秒


def parse_window(text: str) -> tuple[float, float]:
    """`0:0.5` / `0-0.5` / `0:0.5s` → (0.0, 0.5)；认不出 / 止<起 一律报错（不猜）。"""
    raw = str(text or "").strip()
    match = WINDOW_RE.match(raw)
    if not match:
        raise FrameFactsError(f"--window 认不出 {text!r}：写成 起:止（秒），如 0:0.5")
    start, end = float(match.group(1)), float(match.group(2))
    if end < start:
        raise FrameFactsError(f"--window 的止秒不能小于起秒：{text!r}")
    return round(start, 3), round(end, 3)


def window_label(start: float, end: float) -> str:
    """窗口的名字就用它的起止秒：0:0.5（写进 看帧.json，也是复用时的键）。"""
    return f"{round(float(start), 3):g}:{round(float(end), 3):g}"


def window_times(start: float, end: float, n: int = DEFAULT_N,
                 duration: float | None = None) -> list[float]:
    """窗口内均匀取 n 帧，**含首尾**：0:0.5 + 3 → [0, 0.25, 0.5]。

    n<=1 或起止相同 → 只有起点（老口径的单帧判定就是这条）。
    给了片长就把秒数收到最后一帧能抽的位置（与 choose_times 同口径，不炸整次）。
    """
    start, end = float(start), float(end)
    count = max(1, int(n or 1))
    if count == 1 or end <= start:
        picks = [start]
    else:
        step = (end - start) / (count - 1)
        picks = [start + step * index for index in range(count)]

    limit = max(0.0, float(duration) - 0.05) if duration else None
    out: list[float] = []
    for value in picks:
        seconds = round(max(0.0, float(value)), 3)
        if limit is not None:
            seconds = min(seconds, round(limit, 3))
        if seconds not in out:
            out.append(seconds)
    return out


def make_window(start: float, end: float, n: int = DEFAULT_N,
                duration: float | None = None) -> dict:
    """一处窗口 = 起止秒 + 要抽几帧 + 实际要抽的那几个秒。"""
    return {
        "窗口": window_label(start, end),
        "起": round(float(start), 3),
        "止": round(float(end), 3),
        "n": max(1, int(n or 1)),
        "帧秒": window_times(start, end, n, duration),
    }


def normalize_windows(items, duration: float | None = None) -> list[dict]:
    """要判的东西 → 统一的窗口记录表。

    认 dict（已经是窗口）与裸秒数（老的 `--times` 口径：那一刻单帧判一次）。
    同一窗口只跑一次，按起秒升序。
    """
    out: list[dict] = []
    seen: set[str] = set()
    for item in items or []:
        if isinstance(item, dict):
            try:
                start = float(item.get("起", 0.0))
                end = float(item.get("止", start))
            except (TypeError, ValueError):
                raise FrameFactsError(f"窗口记录认不出起止秒：{item!r}")
            n = item.get("n")
            if not isinstance(n, int) or n < 1:
                n = len(item.get("帧秒") or []) or 1
            window = make_window(start, end, n, duration)
        else:
            try:
                seconds = float(item)
            except (TypeError, ValueError):
                raise FrameFactsError(f"要判的秒数认不出：{item!r}")
            window = make_window(seconds, seconds, 1, duration)
        if window["窗口"] in seen:
            continue
        seen.add(window["窗口"])
        out.append(window)
    return sorted(out, key=lambda window: window["起"])


# ---------------------------------------------------------------- 机械聚合

ACTION_WORDS = ("抬", "举", "握", "伸", "推", "扶", "抱", "转", "走", "站", "坐", "靠",
                "回", "望", "垂", "叉", "撑", "抓", "拉", "摆", "弯")


def clamp_votes(value) -> int:
    """`--votes` → 1..MAX_VOTES 的整数；认不出就用默认值（默认 1）。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return DEFAULT_VOTES
    return max(1, min(MAX_VOTES, number))


def richness(judgment: dict) -> int:
    """一段判定的信息量：字数打底，写了动作 / 眼神的再加分。

    聚合时描述取这一段——不是取「多数」，而是取讲得更细的那一段：
    细的那段才看得出人在做什么、眼睛落在哪，这句话才是给生成模型看的。
    """
    description = str((judgment or {}).get("描述") or "")
    score = len(description)
    if any(word in description for word in ACTION_WORDS):
        score += 20
    if str((judgment or {}).get("gaze") or "").strip():
        score += 10
    if str((judgment or {}).get("posture_action") or "").strip():
        score += 10
    return score


def _field_raw(field: str, judgment: dict) -> str:
    """这一帧在某个字段上的「原话」（写进 分歧，也是聚合取值的来源）。"""
    body = judgment.get("body_orientation")
    body = body if isinstance(body, dict) else {}
    if field == "facing_class":
        stated = str(body.get("facing_class") or "").strip()
        reader = extract_module()
        if stated not in reader.FACING_CLASSES:
            stated = reader.facing_class(str(body.get("facing") or ""), field=True)
        return stated
    if field == "gaze":
        return str(judgment.get("gaze") or "").strip()
    if field == "people":
        value = judgment.get("people")
        return "" if value in (None, "") else str(value)
    return str(judgment.get("shot_size") or "").strip()


def _field_key(field: str, judgment: dict) -> str:
    """比较用的归一值：只把同义说法并到一起，不做语义推断。

    - 视线：`直视镜头` / `看着镜头` 都是「看镜头」，用 extract.gaze_relation 归成 camera/away；
      归不出来（看不清、没写）就按原话比。
    - 其余字段原话就是键。
    """
    raw = _field_raw(field, judgment)
    if field == "gaze":
        return extract_module().gaze_relation(raw) or raw
    return raw


def aggregate(judgments) -> dict:
    """窗口内多帧判定 → 一条（机械计数，不合成、不平均、不再让模型总结）。

    入参：`[(秒, 判定), …]`，也认带「秒」的判定 dict。
    规则：
      · 四个字段逐字段数：全部一致 → 取该值、confidence=高；
      · 有任何字段不一致 → 该字段写「歧」、各帧说法按秒列进 `分歧`、confidence=低；
      · 有帧没判成 → 同样低置信，并在 `分歧` 里写明哪一秒没判成（不假装那一帧没问过）；
      · `描述` 取信息量最大的一帧（richness），`描述秒` 写明来自哪一秒。
    单帧窗口（老格式折出来的）没有可比的第二帧：`finalize()` 会把置信度换回它自报的那一档。
    """
    rows: list[tuple[float, dict]] = []
    for index, item in enumerate(judgments or []):
        if isinstance(item, (tuple, list)) and len(item) == 2:
            seconds, judgment = item
        else:
            judgment = item
            seconds = judgment.get("秒") if isinstance(judgment, dict) else None
            if seconds is None and isinstance(judgment, dict):
                seconds = judgment.get("frame")
        try:
            seconds = round(float(seconds), 3)
        except (TypeError, ValueError):
            seconds = round(0.25 * index, 3)
        if isinstance(judgment, dict):
            rows.append((seconds, judgment))

    usable = [(seconds, judgment) for seconds, judgment in rows
              if not judgment.get("错误") and str(judgment.get("描述") or "").strip()]
    if not usable:
        failed = failed_judgment("窗口里一帧都没判成")
        failed.update({
            "帧数": len(rows), "判成": 0, "描述秒": None,
            "分歧": [f"{seconds:g} 秒这一帧没判成：{judgment.get('错误') or '没有判定'}"
                     for seconds, judgment in rows],
        })
        return failed

    chosen_seconds, chosen = max(usable, key=lambda pair: (richness(pair[1]), -pair[0]))
    disagreements: list[str] = []
    disagree_values: dict[str, list[str]] = {}
    agreed: dict[str, str] = {}

    for field in AGG_FIELDS:
        readings = [(seconds, _field_raw(field, judgment), _field_key(field, judgment))
                    for seconds, judgment in usable]
        keys = {key for _seconds, _raw, key in readings}
        if len(keys) == 1:
            key = next(iter(keys))
            # 同一档的不同说法不替模型挑：取「描述那一帧」的原话
            agreed[field] = next((raw for seconds, raw, k in readings
                                  if seconds == chosen_seconds and k == key), readings[0][1])
        else:
            agreed[field] = "歧"
            values: list[str] = []
            for _seconds, raw, _key in readings:
                if raw not in values:
                    values.append(raw)
            disagree_values[field] = values
            disagreements.append(
                f"{FIELD_LABELS[field]}：" + "／".join(
                    f"{seconds:g} 秒「{raw}」" for seconds, raw, _key in readings))

    usable_seconds = {seconds for seconds, _judgment in usable}
    for seconds, judgment in rows:
        if seconds in usable_seconds:
            continue
        disagreements.append(f"{seconds:g} 秒这一帧没判成：{judgment.get('错误') or '没有描述'}")

    # 第十层「图文与收束」：机器逐帧抄到的持久性文字，按「文字+位置」去重（同一处同一个字
    # 两帧都看到只登记一次，秒取首见那帧）；「图文已判」标记这几帧**全都**真的被问过——
    # 解析端只对显式作答的帧落 text_elements 键（漏答＝未判，2026-11-04 路 A 审计 B6），
    # 窗口里只要有一帧没答就不设图文已判，注入端就造不出「看过无字」的伪事实；
    # 旧记录没有这个键，同样落在未判这一态（注入端一个字不加）。
    asked_about_text = all(isinstance(judgment, dict) and "text_elements" in judgment
                           for _seconds, judgment in usable)
    text_rows: list[dict] = []
    seen_texts: set[tuple[str, str]] = set()
    for seconds, judgment in usable:
        for element in judgment.get("text_elements") or []:
            if not isinstance(element, dict):
                continue
            text = str(element.get("文字") or "").strip()
            if not text:
                continue
            key = (text, str(element.get("位置") or "").strip())
            if key in seen_texts:
                continue
            seen_texts.add(key)
            text_rows.append({
                "秒": seconds,
                "文字": text,
                "位置": str(element.get("位置") or "").strip(),
                "形态": str(element.get("形态") or "").strip(),
                "动效": str(element.get("动效") or "").strip(),
            })

    people_value = agreed.pop("people")
    people = int(people_value) if str(people_value).strip().isdigit() else people_value

    body = chosen.get("body_orientation")
    body = body if isinstance(body, dict) else {}
    facing_class = agreed.pop("facing_class")
    if facing_class == "歧":
        facing, back_toward = "歧", ""
    else:
        facing = str(body.get("facing") or "").strip()
        back_toward = str(body.get("back_toward") or "").strip()

    return {
        "people": people,
        "gaze": agreed.pop("gaze"),
        "shot_size": agreed.pop("shot_size"),
        "body_orientation": {"facing": facing, "facing_class": facing_class,
                             "back_toward": back_toward},
        "confidence": "低" if disagreements else "高",
        "描述": str(chosen.get("描述") or "").strip(),
        "描述秒": chosen_seconds,
        "图文": text_rows,
        "图文已判": asked_about_text,
        "分歧": disagreements,
        "分歧值": disagree_values,
        "帧数": len(rows),
        "判成": len(usable),
        "_单帧自报": str(chosen.get("confidence") or "") if len(usable) == 1 else "",
    }


def finalize(aggregated: dict) -> dict:
    """单帧窗口的置信度：没有第二帧可比，就沿用它自报的那一档（不硬凑「高」）。"""
    if aggregated.get("_单帧自报") and not aggregated.get("分歧"):
        aggregated["confidence"] = aggregated["_单帧自报"]
    aggregated.pop("_单帧自报", None)
    return aggregated


def window_note(aggregated: dict | None) -> str:
    """低置信窗口的一句实话：（这一窗口三帧里 侧对／侧背 不一，以描述为准）。

    高置信 / 没有分歧 → 空串（不写没发生的事）。
    """
    agg = aggregated or {}
    if str(agg.get("confidence") or "") != "低":
        return ""
    count = COUNT_WORDS.get(int(agg.get("帧数") or 0), str(agg.get("帧数") or ""))
    values = agg.get("分歧值") or {}
    for field in AGG_FIELDS:
        if values.get(field):
            return f"（这一窗口{count}帧里 {'／'.join(values[field])} 不一，以描述为准）"
    return f"（这一窗口{count}帧里有帧没判成，以描述为准）"


def readable_frames(facts: dict | None) -> list[dict]:
    """payload → 窗口记录表。新格式直接用「窗口」；老格式（只有「帧」）把每帧折成单帧窗口。

    折出来的单帧窗口同样带「聚合」：下游只认一种形状，不用到处写 `if 窗口 … else 帧`。
    """
    windows = [window for window in (facts or {}).get("窗口") or []
               if isinstance(window, dict) and window.get("帧")]
    if windows:
        return sorted(windows, key=lambda window: float(window.get("起") or 0.0))
    out: list[dict] = []
    for frame in (facts or {}).get("帧") or []:
        if not isinstance(frame, dict):
            continue
        judgment = frame.get("判定")
        if not isinstance(judgment, dict):
            continue
        try:
            seconds = round(float(frame.get("秒")), 3)
        except (TypeError, ValueError):
            continue
        aggregated = finalize(aggregate([(seconds, judgment)]))
        if judgment.get("votes"):
            # 老记录里的多次判定留痕要带上：低置信判定（冲突不参与）与旧的「判定不一」
            # 提示都读这个键；新窗口的聚合不写 votes。
            aggregated["votes"] = judgment["votes"]
        out.append({"窗口": f"{seconds:g}", "起": seconds, "止": seconds, "帧": [frame],
                    "聚合": aggregated})
    return sorted(out, key=lambda window: window.get("起", 0.0))


def seconds_entry(data: dict | None, seconds: float = 0.0) -> dict | None:
    """看帧.json → 某一秒起的那一条记录（判定是这一窗口的聚合）；没有返回 None。

    老格式里没有窗口、只有散帧：`readable_frames` 已把每帧折成单帧窗口，这里照同样口径找。
    """
    for window in readable_frames(data):
        try:
            start = float(window.get("起", -1e9))
        except (TypeError, ValueError):
            continue
        if abs(start - float(seconds)) >= 1e-6:
            continue
        frames = window.get("帧") or []
        entry = dict(frames[0]) if frames and isinstance(frames[0], dict) else {}
        entry.update({"秒": round(start, 3), "窗口": window.get("窗口"),
                      "帧": frames, "判定": window.get("聚合") or {}})
        return entry
    return None


def description_of(data: dict | None, seconds: float = 0.0) -> str:
    """看帧.json 里某一窗口的「描述」原句（聚合取的那一帧）；没有就空串。"""
    entry = seconds_entry(data, seconds)
    judgment = (entry or {}).get("判定")
    return str((judgment or {}).get("描述") or "").strip() if isinstance(judgment, dict) else ""


def usable_entry(entry: dict | None) -> bool:
    """这条记录算不算「已经有判定」：判定是 dict 且描述非空。

    判失败的那一条也写进 看帧.json（低置信 + 错误原因），但它没有描述——不拿它当
    「判过了」，否则一次网络抖动就会让这个任务永远不再重试（钱白花，事实永远缺）。
    """
    judgment = (entry or {}).get("判定")
    return isinstance(judgment, dict) and bool(str(judgment.get("描述") or "").strip())


def existing_window(done: dict, wanted: dict) -> dict | None:
    """已有判定里对得上这个窗口的那一条：先按标签，再按起点找老记录（标签是裸秒数）。

    老 看帧.json 只有散帧、没有区间标签；同一个起点就该算问过了——不为凑够 3 帧再花 3 次调用。
    """
    window = (done or {}).get(str(wanted.get("窗口")))
    if isinstance(window, dict):
        return window
    for candidate in (done or {}).values():
        if not isinstance(candidate, dict) or ":" in str(candidate.get("窗口") or ""):
            continue
        try:
            if abs(float(candidate.get("起", -1e9)) - float(wanted.get("起", 0.0))) < 1e-6:
                return candidate
        except (TypeError, ValueError):
            continue
    return None


def window_reusable(window: dict | None, wanted: dict) -> bool:
    """已有窗口记录能不能直接复用：聚合有描述、帧数与这次要的一致。

    `usable_entry` 只管「有没有判成」；这里还要管「问的还是不是同一个窗口」——
    n 从 3 改成 2 就该重跑，不能拿三帧的聚合冒充两帧的。
    """
    if not isinstance(window, dict):
        return False
    agg = window.get("聚合")
    if not isinstance(agg, dict) or not str(agg.get("描述") or "").strip():
        return False
    frames = [frame for frame in window.get("帧") or [] if isinstance(frame, dict)]
    if len(frames) != int(wanted.get("n") or 1):
        # 2026-11-02 实测教训：这里原先允许「旧单帧记录」复用成 3 帧窗口，
        # 结果今天之前拆过的任务（pop-3-b3 就是）永远走不到新窗口路径——
        # ensure_facts 报 calls=0、reused=1，一次判定都不跑，验证等于白做。
        # 单帧就是单帧，不能冒充窗口：帧数不一致一律重跑。
        return False
    wanted_seconds = [round(float(s), 3) for s in (wanted.get("帧秒") or [])]
    if wanted_seconds:
        got = [round(float(frame.get("秒") or 0), 3) for frame in frames]
        if got != wanted_seconds:
            return False
    return True


# ---------------------------------------------------------------- 兼容：老记录的 votes

def vote_classes(judgment: dict | None) -> list[str]:
    """老记录里每次投票的朝向档位（按判的顺序）；没有 votes → 空表。"""
    classes: list[str] = []
    for vote in (judgment or {}).get("votes") or []:
        if not isinstance(vote, dict):
            continue
        face = str(vote.get("facing_class") or "").strip()
        if face not in extract_module().FACING_CLASSES:
            face = extract_module().facing_class(str(vote.get("facing") or ""), field=True)
        classes.append(face)
    return classes


def votes_disagree(judgment: dict | None) -> bool:
    """老记录里同一帧多次判定对朝向档位不一致 → 那一帧的朝向本身没定论。

    新判定不再写 votes（同一帧反复问不作为置信度依据），这条只为读老 看帧.json 留着。
    """
    return len(set(vote_classes(judgment))) > 1


def vote_note(judgment: dict | None) -> str:
    """老记录的一句实话：（这一帧的朝向两次判定不一：侧对／背对，以描述为准）。"""
    classes = vote_classes(judgment)
    if len(set(classes)) <= 1:
        return ""
    count = {2: "两", 3: "三"}.get(len(classes), str(len(classes)))
    return f"（这一帧的朝向{count}次判定不一：{'／'.join(classes)}，以描述为准）"


def merge_readings(judgments: list[dict]) -> dict:
    """同一帧问了多次（旧口径，默认不再走）：取信息最多那段当正文，各次读数留痕。

    2026-11-02 起**不用分歧决定置信度**：多次问同一帧本来就会摆，摆了不等于画面没定论。
    置信度只取各次自报里最保守的那一档；要判稳不稳，看窗口里不同时刻的帧（aggregate）。
    """
    if not judgments:
        return failed_judgment("一次判定都没有")
    chosen = max(enumerate(judgments), key=lambda pair: (richness(pair[1]), -pair[0]))[1]
    merged = json.loads(json.dumps(chosen, ensure_ascii=False))
    body = merged.get("body_orientation")
    merged["body_orientation"] = body if isinstance(body, dict) else {}

    votes = []
    for judgment in judgments:
        item = judgment.get("body_orientation") or {}
        votes.append({
            "facing": str(item.get("facing") or ""),
            "facing_class": str(item.get("facing_class") or ""),
            "confidence": str(judgment.get("confidence") or ""),
            "描述": str(judgment.get("描述") or ""),
        })
    merged["votes"] = votes

    levels = list(CONFIDENCE_LEVELS)
    merged["confidence"] = max(
        (vote["confidence"] for vote in votes),
        key=lambda level: levels.index(level) if level in levels else 1)
    return merged


# ---------------------------------------------------------------- 取帧


def _display(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except (ValueError, OSError):
        return str(path)


def probe_size(path: Path) -> tuple[int, int]:
    """量一张图/一段片的像素尺寸。量不到就抛 FrameFactsError（不许猜）。"""
    cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0",
           "-show_entries", "stream=width,height", "-of", "json", str(path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=60)
    except FileNotFoundError as exc:
        raise FrameFactsError("找不到 ffprobe，请装 ffmpeg 并把 bin 目录加进 PATH") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise FrameFactsError(f"ffprobe 跑不起来：{exc}") from exc
    if proc.returncode != 0:
        raise FrameFactsError(f"量不出尺寸（ffprobe 退出码 {proc.returncode}）："
                              f"{(proc.stderr or '').strip()[:200]}")
    try:
        streams = (json.loads(proc.stdout or "{}").get("streams") or [])
        width = int(streams[0].get("width") or 0)
        height = int(streams[0].get("height") or 0)
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise FrameFactsError(f"量不出尺寸：{exc}") from exc
    if not (width and height):
        raise FrameFactsError(f"量不出尺寸：{path.name}")
    return width, height


def grab_frame(video: Path, seconds: float, out_png: Path,
               source_size: tuple[int, int]) -> dict:
    """抽一帧落盘：**不加任何 -vf，不缩放**。抽完当场量尺寸，和源片不一致就报错。"""
    out_png.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error",
           "-ss", f"{seconds:.3f}", "-i", str(video),
           "-frames:v", "1", "-y", str(out_png)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=120)
    except FileNotFoundError as exc:
        raise FrameFactsError("找不到 ffmpeg，请装 ffmpeg 并把 bin 目录加进 PATH") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise FrameFactsError(f"ffmpeg 跑不起来：{exc}") from exc
    if proc.returncode != 0 or not out_png.is_file() or out_png.stat().st_size == 0:
        detail = (proc.stderr or "").strip()[:300] or "没有产出文件"
        raise FrameFactsError(f"抽第 {seconds:g} 秒失败：{detail}")

    width, height = probe_size(out_png)
    if (width, height) != tuple(source_size):
        raise FrameFactsError(
            f"抽出的帧 {width}×{height} 与源片 {source_size[0]}×{source_size[1]} 不一致"
            f"——不是原分辨率，判定会看错（判据 F2）")
    return {
        "文件": _display(out_png),
        "宽": width,
        "高": height,
        "sha256": hashlib.sha256(out_png.read_bytes()).hexdigest(),
        "字节": out_png.stat().st_size,
    }


def parse_clock(cell: str) -> float | None:
    """`00:02` / `00:02.5` / `1:02:03` / `12.5` / `12.5s` → 秒；认不出返回 None。"""
    text = (cell or "").strip()
    if not text:
        return None
    match = CLOCK_RE.search(text)
    if match:
        first, second, third, frac = match.groups()
        if third is None:
            minutes, seconds_part = int(first), int(second)
            value = minutes * 60 + seconds_part
        else:
            value = int(first) * 3600 + int(second) * 60 + int(third)
        if frac:
            value += float("0." + frac)
        return round(float(value), 3)
    digits = re.search(r"\d+(?:\.\d+)?", text)
    return round(float(digits.group(0)), 3) if digits else None


def shot_starts(folder: Path) -> list[float]:
    """从 拆解.md 的「七 分镜表」里读每镜起秒；表不在就返回空表（不猜）。"""
    path = folder / DISSECTION_NAME
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []

    start_index = next((i for i, line in enumerate(lines)
                        if (m := LAYER_RE.match(line)) and m.group(1) == "七"), None)
    if start_index is None:
        return []

    header: list[str] | None = None
    column = 1
    out: list[float] = []
    for line in lines[start_index + 1:]:
        if LAYER_RE.match(line):
            break
        text = line.strip()
        if text.count("|") < 2:
            continue
        cells = [cell.strip() for cell in text.strip("|").split("|")]
        if header is None:
            if any("镜号" in cell for cell in cells):
                header = cells
                column = next((i for i, cell in enumerate(cells) if "起秒" in cell), 1)
            continue
        if not re.fullmatch(r"\d+", cells[0]):
            continue
        if column < len(cells):
            value = parse_clock(cells[column].split("-")[0])
            if value is not None:
                out.append(value)
    return out


def choose_times(folder: Path, duration: float, cuts: list[float],
                 explicit: list[float] | None = None,
                 max_shots: int = DEFAULT_MAX_SHOTS,
                 max_frames: int = DEFAULT_MAX_FRAMES) -> list[float]:
    """要抽哪几秒（升序）。t=0 永远在；显式给了 --times 就只用它（外加 0）。

    这是「一秒一个窗口」的取点规则：每点开多宽的窗口由调用方定
    （extract 的 --frames auto 就用它给每镜起点开窗口）。
    超出片长的秒数收到最后一帧上——不是替人猜，而是让「手抖多写了个 60」不至于废掉整次。
    """
    picked: list[float] = [0.0]
    if explicit:
        picked.extend(explicit)
    else:
        starts = [t for t in shot_starts(folder) if 0 < t < max(duration, 0.001)]
        if starts:
            picked.extend(starts[:max_shots])
        else:
            picked.extend(t for t in cuts if 0 < t < duration)
            picked.extend(duration * ratio for ratio in (0.25, 0.5, 0.75))
            picked = picked[:max_frames]
    limit = max(0.0, float(duration) - 0.05)
    seen: list[float] = []
    for value in picked:
        seconds = round(min(max(0.0, float(value)), limit), 3)
        if seconds not in seen:
            seen.append(seconds)
    return sorted(seen)


# ---------------------------------------------------------------- 判定一帧


def judge_frame(call, prompt: str, png: Path,
                votes: int = DEFAULT_VOTES) -> tuple[dict, str]:
    """判这一帧：**一次为准**（`votes` 默认 1）。

    2026-11-02 实测：同一帧连问两次，答案会在「侧对 / 侧背 / 背对」之间摆，问得越多越不
    知道该信哪次；同一帧只问一次、换窗口里不同时刻的几帧各问一次，三帧反而一致且更全。
    所以置信度不再由「同一帧问了几次」决定，改由 aggregate() 逐字段数不同帧的说法。
    `votes>1` 只为兼容旧命令行保留：多问的那几次留痕在 votes 里，不参与置信度判定。

    `call(prompt, path)` 返回 (正文, usage, 耗时)；抽出来是为了测试能塞替身。
    """
    votes = clamp_votes(votes)
    collected: list[dict] = []
    last_error = ""
    raw = ""
    for index in range(1, votes + 1):
        for attempt in range(1, PARSE_ATTEMPTS + 1):
            try:
                text, _usage, _elapsed = call(prompt, png)
            except Exception as exc:                   # 调用层已重试过；这里只留痕不重来
                return (failed_judgment(f"调用失败：{type(exc).__name__}：{exc}"),
                        f"调用失败：{exc}")
            raw = text
            try:
                collected.append(parse_judgment(text))
                break
            except ValueError as exc:
                last_error = str(exc)
                if attempt < PARSE_ATTEMPTS:
                    print(f"[重问] 第 {png.stem} 秒第 {index} 次判定解析失败（{exc}），再问一次",
                          file=sys.stderr, flush=True)
        else:
            return (failed_judgment(
                f"第 {index} 次判定解析失败（重试 {PARSE_ATTEMPTS - 1} 次）：{last_error}", raw),
                last_error)
    if votes == 1:
        return collected[0], ""
    return merge_readings(collected), ""


# ---------------------------------------------------------------- 读写


def _atomic_write(path: Path, text: str) -> int:
    """先写同目录临时文件再 rename；失败不留半截文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp{os.getpid()}")
    data = text.encode("utf-8")
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return len(data)


def load_facts(folder: Path) -> dict | None:
    """读 看帧.json；没有 / 坏了 → None（调用方自己决定退路，不在这里编）。

    新格式（只有「窗口」）与老格式（只有「帧」）都认。
    """
    path = folder / FACTS_NAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data if (data.get("窗口") or data.get("帧")) else None


def flatten_frames(windows: list[dict]) -> list[dict]:
    """窗口表 → 扁平的「帧」数组（老读者只认这个键，写一份让它们照读）。"""
    rows: list[dict] = []
    for window in windows:
        for frame in window.get("帧") or []:
            if not isinstance(frame, dict):
                continue
            rows.append({
                "秒": frame.get("秒"),
                "文件": frame.get("文件"),
                "sha256": frame.get("sha256"),
                "宽": frame.get("宽"),
                "高": frame.get("高"),
                "判定": frame.get("判定"),
                "模型": window.get("模型"),
                "时间": window.get("时间"),
                "窗口": window.get("窗口"),
            })
    return sorted(rows, key=lambda row: float(row.get("秒") or 0.0))


def build_payload(folder: Path, display_video: str, source_size: tuple[int, int],
                  windows: list[dict]) -> dict:
    """整份 看帧.json：窗口是正文（带聚合），「帧」是给老读者的扁平视图。"""
    ordered = sorted((window for window in windows if isinstance(window, dict)),
                     key=lambda window: float(window.get("起") or 0.0))
    return {
        "id": folder.name,
        "源视频": display_video,
        "源片尺寸": {"宽": source_size[0], "高": source_size[1]},
        "窗口": ordered,
        "帧": flatten_frames(ordered),
    }


def content_digest(data: dict | None) -> str:
    """看帧.json 里**会进提示词**的那部分的 sha256；没有可用判定 → 空串。

    新格式只摘要「窗口 + 聚合」：提示词里写的就是聚合结果；某帧原话改了但聚合没变，
    不该因此把整份拆解重拆。
    老格式（只有「帧」）照旧摘要「秒 + 判定」——算法一行不动，老指纹不会因为我们改了
    读法就集体失效。不含「时间」「文件」「模型」：判定原样重跑一遍只是时间戳变。
    """
    rows = []
    if (data or {}).get("窗口"):
        for window in (data or {}).get("窗口") or []:
            if not isinstance(window, dict):
                continue
            agg = window.get("聚合")
            if not isinstance(agg, dict) or not str(agg.get("描述") or "").strip():
                continue
            rows.append({"窗口": str(window.get("窗口") or ""), "聚合": agg})
        if not rows:
            return ""
        rows.sort(key=lambda row: row["窗口"])
    else:
        for frame in (data or {}).get("帧") or []:
            if not usable_entry(frame):
                continue
            try:
                seconds = round(float(frame.get("秒")), 3)
            except (TypeError, ValueError):
                continue
            rows.append({"秒": seconds, "判定": frame.get("判定")})
        if not rows:
            return ""
        rows.sort(key=lambda row: row["秒"])
    blob = json.dumps(rows, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def resolve_video(folder: Path, explicit: str | None) -> Path:
    """找源片：显式 --video → 任务目录里的 .源视频指纹.json；都不行就报错。"""
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise FrameFactsError(f"视频不存在：{explicit}")
        return path

    fingerprint = folder / FINGERPRINT_NAME
    if fingerprint.is_file():
        try:
            recorded = json.loads(fingerprint.read_text(encoding="utf-8"))
            raw = str(recorded.get("路径") or "").strip()
        except (OSError, json.JSONDecodeError):
            raw = ""
        if raw:
            candidate = Path(raw)
            if not candidate.is_absolute():
                candidate = REPO_ROOT / raw
            if candidate.is_file():
                return candidate
            raise FrameFactsError(f"指纹里记的源片不在了：{raw}")
    raise FrameFactsError(f"{folder.name} 没有 .源视频指纹.json，用 --video 指一条片子")


def _stamp() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _same_source(previous: dict, display_video: str, source_size: tuple[int, int]) -> bool:
    """这份 看帧.json 判的是不是同一条片子：路径与尺寸都得对上，对不上就整份作废。"""
    if not previous:
        return False
    if previous.get("源视频") != display_video:
        return False
    size = previous.get("源片尺寸") or {}
    return size.get("宽") == source_size[0] and size.get("高") == source_size[1]


def ensure_facts(folder: Path, video: Path, probe: dict, windows, call,
                 force: bool = False,
                 votes: int = DEFAULT_VOTES) -> tuple[dict | None, dict]:
    """拆解前把要的那几个窗口判定补齐：缺才跑，跑成功才写。

    这是 extract.py 自动接线用的入口（命令行 `--frames` 走 run()）。`windows` 认窗口记录
    与裸秒数（裸秒数 = 那一刻单帧判一次，老口径）。`call(prompt, png)` 返回
    (正文, usage, 耗时)，与 judge_frame 同款，测试可塞替身。

    返回 (facts, summary)：
      facts   整份 payload，给注入用；None = 没有可用判定（调用方不要注入）
      summary {"ok", "calls", "missing", "reused", "reason", "created"}

    纪律：任何一步失败都只写进 summary["reason"]，不抛异常把拆解拖死；失败的窗口不落盘，
    下次还会重试（见 usable_entry / window_reusable）。
    """
    folder = Path(folder)
    summary = {"ok": True, "calls": 0, "missing": [], "reused": 0,
               "reason": "", "created": False}
    previous = load_facts(folder) or {}
    display_video = _display(Path(video))
    source_size = (int(probe.get("width") or 0), int(probe.get("height") or 0))

    try:
        wanted = normalize_windows(windows, probe.get("duration"))
    except FrameFactsError as exc:
        summary.update(ok=False, reason=str(exc))
        return previous or None, summary

    reuse_ok = _same_source(previous, display_video, source_size)
    done = {str(window.get("窗口")): window
            for window in readable_frames(previous)} if reuse_ok else {}

    missing = [window for window in wanted
               if force or not window_reusable(existing_window(done, window), window)]
    summary["missing"] = [window["窗口"] for window in missing]
    summary["reused"] = len(wanted) - len(missing)
    if not missing:
        return previous or None, summary
    if not all(source_size):
        summary.update(ok=False, reason="ffprobe 没量到源片尺寸，抽出来的帧没法对（判据 F2）")
        return previous or None, summary

    fresh: list[dict] = []
    judged_frames: dict[float, dict] = {}   # 本轮已判过的帧，键=取整秒（2026-11-04 路 A 审计 S1）
    for window in missing:
        frames: list[dict] = []
        spent = {"n": 0}

        def _call(text: str, path: Path, _spent=spent):
            _spent["n"] += 1
            summary["calls"] += 1
            return call(text, path)

        failure = ""
        for seconds in window["帧秒"]:
            shared = judged_frames.get(round(float(seconds), 3))
            if shared is not None:
                # 首/尾窗在短片上会重合到同一秒：判定是已取证事实，同秒问两遍只会重复
                # 计费、还可能给同帧攒出两份不同答案——跨窗共享这一份判定（S1），
                # 两份窗口聚合照旧各算各的，尾锚点语义不变。
                frames.append(shared)
                print(f"[看帧] {window['窗口']} 窗口 · {seconds:g} 秒 → 同秒已判过，复用前窗判定",
                      flush=True)
                continue
            png = folder / FRAME_DIR / f"{seconds:g}.png"
            try:
                grabbed = grab_frame(Path(video), seconds, png, source_size)
            except FrameFactsError as exc:
                failure = str(exc)
                break
            judgment, error = judge_frame(_call, judge_prompt(seconds, grabbed["宽"], grabbed["高"]),
                                          png, votes=votes)
            if error:
                failure = f"{window['窗口']} 窗口第 {seconds:g} 秒判定没判成：{error}"
                break
            body = judgment.get("body_orientation") if isinstance(judgment, dict) else None
            body = body if isinstance(body, dict) else {}
            print(f"[看帧] {window['窗口']} 窗口 · {seconds:g} 秒 → {body.get('facing') or '—'}"
                  f"（{body.get('facing_class') or '—'}）/ {judgment.get('gaze') or '—'}"
                  f"（{spent['n']} 次调用）", flush=True)
            record = {
                "秒": seconds,
                "文件": grabbed["文件"],
                "sha256": grabbed["sha256"],
                "宽": grabbed["宽"],
                "高": grabbed["高"],
                "判定": judgment,
            }
            frames.append(record)
            judged_frames[round(float(seconds), 3)] = record

        if failure:
            summary.update(ok=False, reason=failure)
            break
        if not frames:
            summary.update(ok=False, reason=f"{window['窗口']} 窗口一帧都没抽到")
            break

        aggregated = finalize(aggregate([(frame["秒"], frame["判定"]) for frame in frames]))
        body = aggregated.get("body_orientation") or {}
        print(f"[聚合] {window['窗口']} 窗口（{aggregated.get('帧数')} 帧）"
              f" confidence={aggregated.get('confidence')}"
              f" · facing_class={body.get('facing_class')} · gaze={aggregated.get('gaze')}"
              f" · people={aggregated.get('people')} · 景别={aggregated.get('shot_size')}",
              flush=True)
        fresh.append({
            "窗口": window["窗口"],
            "起": window["起"],
            "止": window["止"],
            "帧": frames,
            "聚合": aggregated,
            "模型": MODEL_LABEL,
            "时间": _stamp(),
        })

    if not fresh:
        return previous or None, summary

    taken = {str(window.get("窗口")) for window in fresh}
    taken_starts = {round(float(window.get("起") or 0.0), 3) for window in fresh}
    entries = list(fresh)
    if reuse_ok:
        # 同一个起点只留一条：老记录标签是裸秒数（0），新窗口是 0:0.5，不能两条并排
        entries.extend(window for window in readable_frames(previous)
                       if str(window.get("窗口")) not in taken
                       and round(float(window.get("起") or 0.0), 3) not in taken_starts)
    payload = build_payload(folder, display_video, source_size, entries)
    try:
        _atomic_write(folder / FACTS_NAME, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    except OSError as exc:
        summary.update(ok=False, reason=f"写 {FACTS_NAME} 失败：{exc}")
        return previous or None, summary
    summary["created"] = True
    return payload, summary


# ---------------------------------------------------------------- 主流程


def run(folder: Path, times: list[float] | None, as_json: bool, video_arg: str | None,
        timeout: float, force: bool, votes: int = DEFAULT_VOTES,
        window: str = DEFAULT_WINDOW, n: int = DEFAULT_N) -> int:
    if not folder.is_dir():
        print(f"ERROR 任务目录不存在：{folder}", file=sys.stderr)
        return 2

    extract = extract_module()
    try:
        video = resolve_video(folder, video_arg)
        probe = extract.probe_video(video)
        api_key = extract.load_api_key()
        if not api_key:
            raise FrameFactsError(f"缺密钥：设置环境变量 {extract.KEY_ENV} 或写进 HERMES_HOME/.env")
        if times:
            wanted = normalize_windows(times, probe["duration"])
            rule = "老口径：这几个秒各判一帧"
        else:
            start, end = parse_window(window)
            wanted = normalize_windows([make_window(start, end, n, probe["duration"])])
            rule = f"窗口 {wanted[0]['窗口']}（{wanted[0]['n']} 帧，均匀取、含首尾）"
    except (FrameFactsError, extract.PrecheckError) as exc:
        print(f"ERROR 预检失败：{exc}", file=sys.stderr)
        return 2

    source_size = (int(probe.get("width") or 0), int(probe.get("height") or 0))
    if not all(source_size):
        print("ERROR 预检失败：ffprobe 没量到源片尺寸，抽出来的帧没法对（判据 F2）",
              file=sys.stderr)
        return 2

    print(f"[预检] {_display(video)} · {source_size[0]}×{source_size[1]} · "
          f"{probe['duration']:.2f} 秒")
    print(f"[取帧] {len(wanted)} 个窗口 · {rule}："
          + "；".join(f"{w['窗口']}→{'、'.join(f'{t:g}' for t in w['帧秒'])} 秒" for w in wanted))

    previous = load_facts(folder) or {}
    display_video = _display(video)
    keep_rest = _same_source(previous, display_video, source_size)
    done = {str(window.get("窗口")): window
            for window in readable_frames(previous)} if keep_rest else {}

    entries: list[dict] = []
    ok_count = 0
    for window_spec in wanted:
        grabbed_frames: list[tuple[float, Path, dict]] = []
        try:
            for seconds in window_spec["帧秒"]:
                png = folder / FRAME_DIR / f"{seconds:g}.png"
                grabbed = grab_frame(video, seconds, png, source_size)
                grabbed_frames.append((seconds, png, grabbed))
                print(f"[抽帧] {window_spec['窗口']} 窗口 · {seconds:g} 秒 → {grabbed['文件']}"
                      f"（{grabbed['宽']}×{grabbed['高']}）")
        except FrameFactsError as exc:
            print(f"ERROR 取帧失败：{exc}", file=sys.stderr)
            return 2

        old = existing_window(done, window_spec)
        old_shas = [frame.get("sha256") for frame in (old or {}).get("帧") or []]
        new_shas = [entry["sha256"] for _seconds, _png, entry in grabbed_frames]
        # 老记录只有 1 帧：拿它的 sha 对这次抽的第一帧，对上就是同一张图（不重复花钱）
        same_frames = bool(old) and bool(old_shas) and new_shas[:len(old_shas)] == old_shas
        if (not force and same_frames and window_reusable(old, window_spec)
                and str((old or {}).get("聚合", {}).get("confidence")) != "低"):
            record = old
            ok_count += 1
            print(f"[复用] {record.get('窗口')} 判定（帧 sha256 未变，不重复花钱）")
        else:
            frames: list[dict] = []
            failure = ""
            spent = {"n": 0}
            for seconds, png, grabbed in grabbed_frames:
                spent["n"] += 1
                judgment, error = judge_frame(
                    lambda text, path: extract.call_model(text, path, api_key, timeout),
                    judge_prompt(seconds, grabbed["宽"], grabbed["高"]), png, votes=votes)
                if error:
                    failure = f"{seconds:g} 秒判定没判成：{error}"
                    break
                body = judgment.get("body_orientation") if isinstance(judgment, dict) else {}
                body = body if isinstance(body, dict) else {}
                print(f"[看帧] {window_spec['窗口']} 窗口 · {seconds:g} 秒："
                      f"people={judgment.get('people')}"
                      f" facing={body.get('facing', '')!r} class={body.get('facing_class', '')!r}"
                      f" gaze={judgment.get('gaze', '')!r} confidence={judgment.get('confidence')}"
                      f"（{spent['n']} 次调用）")
                frames.append({
                    "秒": round(float(seconds), 3),
                    "文件": grabbed["文件"],
                    "sha256": grabbed["sha256"],
                    "宽": grabbed["宽"],
                    "高": grabbed["高"],
                    "判定": judgment,
                })
            if failure:
                print(f"[看帧] {window_spec['窗口']} 窗口判定没判成：{failure}",
                      file=sys.stderr, flush=True)
                continue
            aggregated = finalize(aggregate([(frame["秒"], frame["判定"]) for frame in frames]))
            body = aggregated.get("body_orientation") or {}
            note = window_note(aggregated).strip("（）")
            print(f"[聚合] {window_spec['窗口']} 窗口（{aggregated.get('帧数')} 帧）"
                  f" confidence={aggregated.get('confidence')}"
                  f" · facing_class={body.get('facing_class')} · gaze={aggregated.get('gaze')}"
                  f" · people={aggregated.get('people')} · 景别={aggregated.get('shot_size')}"
                  + (f"｜{note}" if note else ""))
            record = {
                "窗口": window_spec["窗口"],
                "起": window_spec["起"],
                "止": window_spec["止"],
                "帧": frames,
                "聚合": aggregated,
                "模型": MODEL_LABEL,
                "时间": _stamp(),
            }
            ok_count += 1
        entries.append(record)

    if keep_rest:
        taken = {str(entry.get("窗口")) for entry in entries}
        taken_starts = {round(float(entry.get("起") or 0.0), 3) for entry in entries}
        entries.extend(window for window in readable_frames(previous)
                       if str(window.get("窗口")) not in taken
                       and round(float(window.get("起") or 0.0), 3) not in taken_starts)
    payload = build_payload(folder, display_video, source_size, entries)
    try:
        written = _atomic_write(folder / FACTS_NAME,
                                json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    except OSError as exc:
        print(f"ERROR 写盘失败：{exc}", file=sys.stderr)
        return 2

    if as_json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(f"[写出] {_display(folder / FACTS_NAME)}"
              f"（{written} 字节，{len(payload['窗口'])} 个窗口 / {len(payload['帧'])} 帧）")
        for entry in payload["窗口"]:
            aggregated = entry.get("聚合") or {}
            body = aggregated.get("body_orientation") or {}
            print(f"\n  窗口 {entry.get('窗口')}（{len(entry.get('帧') or [])} 帧）"
                  f" · 置信={aggregated.get('confidence')}")
            if aggregated.get("描述"):
                print(f"    {aggregated['描述']}　← 来自 {aggregated.get('描述秒')} 秒那一帧")
            else:
                print("    （没有描述：这个窗口没判成，见 聚合.错误）")
            print(f"    字段：body={body.get('facing', '')}（{body.get('facing_class', '')}）"
                  f" / back_toward={body.get('back_toward', '')}"
                  f" · gaze={aggregated.get('gaze', '')}"
                  f" · people={aggregated.get('people')}"
                  f" · 景别={aggregated.get('shot_size', '')}")
            for line in aggregated.get("分歧") or []:
                print(f"    分歧：{line}")
            for frame in entry.get("帧") or []:
                judgment = frame.get("判定") or {}
                print(f"    {frame.get('秒'):g} 秒：{str(judgment.get('描述') or '')[:48]}"
                      f"（置信={judgment.get('confidence')}）")

    if ok_count == 0:
        print("ERROR 一个窗口都没判成功（看帧.json 已如实写下低置信度与原因，下游不会据此改动拆解）",
              file=sys.stderr)
        return 3
    return 0


def resolve(source: str) -> Path:
    """任务 id 或目录路径 → 任务目录（与 pipeline/recheck.py 同口径）。"""
    raw = str(source or "").strip()
    if not raw:
        raise FrameFactsError("要一个任务 id（analysis/reverse/<id>）")
    path = Path(raw)
    if path.is_absolute() or path.is_dir():
        if not path.is_dir():
            raise FrameFactsError(f"任务目录不存在：{raw}")
        return path
    folder = ANALYSIS / raw
    if not folder.is_dir():
        raise FrameFactsError(f"任务不存在：{raw}（analysis/reverse/ 下没有这个目录）")
    return folder


def parse_times(raw: str) -> list[float]:
    """`0,2.5,6` → [0.0, 2.5, 6.0]；写错就报错，不猜。"""
    out = [0.0]
    for piece in (raw or "").split(","):
        text = piece.strip()
        if not text:
            continue
        try:
            value = float(text)
        except ValueError as exc:
            raise FrameFactsError(f"--times 里有认不出的秒数：{text!r}") from exc
        if value < 0:
            raise FrameFactsError(f"--times 不许是负数：{text!r}")
        if value not in out:
            out.append(value)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="看帧判定：机器取一个窗口里的几帧、每帧各问一次，逐字段机械聚合")
    parser.add_argument("--id", required=True, help="任务 id（analysis/reverse/<id>）或目录路径")
    parser.add_argument("--window", default=DEFAULT_WINDOW, metavar="起:止",
                        help=f"秒区间（默认 {DEFAULT_WINDOW}）；不给 --times 就只跑这一个窗口")
    parser.add_argument("--n", type=int, default=DEFAULT_N,
                        help=f"窗口内均匀取几帧、含首尾（默认 {DEFAULT_N}）")
    parser.add_argument("--times", default="",
                        help="老口径：指定秒数，逗号分隔，每个秒数单帧判一次（0 秒必抽）")
    parser.add_argument("--video", default="", help="源片路径；默认读 .源视频指纹.json")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help=f"单次请求超时秒数（默认 {DEFAULT_TIMEOUT:g}）")
    parser.add_argument("--force", action="store_true", help="不复用同 sha256 的旧判定，重判一遍")
    parser.add_argument("--votes", type=int, default=DEFAULT_VOTES,
                        help=f"同一帧问几次（默认 {DEFAULT_VOTES}，只为兼容旧命令行；"
                             "同一帧反复问不再作为置信度依据——判稳不稳看窗口多帧聚合）")
    parser.add_argument("--json", action="store_true", help="给界面读：一行 JSON")
    args = parser.parse_args(argv)

    try:
        folder = resolve(args.id)
        times = parse_times(args.times) if args.times else None
        parse_window(args.window)
    except FrameFactsError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    votes = clamp_votes(args.votes)
    if votes != args.votes:
        print(f"[判定] --votes {args.votes} 超出 1–{MAX_VOTES}，按 {votes} 次跑", flush=True)
    n = int(args.n) if int(args.n or 0) >= 1 else DEFAULT_N
    if n != args.n:
        print(f"[判定] --n {args.n} 认不出，按 {n} 帧跑", flush=True)
    return run(folder, times, args.json, args.video or None, args.timeout, args.force, votes,
               args.window, n)


if __name__ == "__main__":
    raise SystemExit(main())
