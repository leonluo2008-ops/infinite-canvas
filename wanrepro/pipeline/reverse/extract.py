#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""S1.1 拆解：一条参考视频 → 十层结构化 Markdown（一次多模态调用）。

提示词单一真源是 prompts/视频拆解.md 里第一个 ``` 围栏内的正文；本文件不存任何副本。

用法：
    py -3 pipeline/reverse/extract.py <视频> [--id <id>] [--outdir <dir>] [--intent <意图>]
    py -3 pipeline/reverse/extract.py --check-only analysis/reverse/<id>/拆解.md

退出码：
    0 成功（--check-only 时表示十层齐全）
    2 预检失败：文件不存在 / ffprobe 不可用 / 无法解码 / 无时长 / 无视频流 / 缺密钥 / 提示词源头异常
    3 API 失败：重试 3 次（退避 5/10/20 秒）后仍无有效正文（云端原文落盘 analysis/<id>/拆解错误.json）
    4 缺层：十层标题缺任一层，或某层正文为空
"""

from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import os
from pathlib import Path
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# ---------------------------------------------------------------- 常量

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT_PATH = REPO_ROOT / "prompts" / "视频拆解.md"
ENDPOINT = "https://api.jxinapi.com/v1beta/models/gemini-3.8-flash:generateContent"
MODEL_LABEL = "gemini-3.8-flash"
KEY_ENV = "JXIN_API_KEY"
BACKOFF_SECONDS = (5, 10, 20)          # 3 次重试的退避；总尝试次数 = 1 + 3
DEFAULT_TIMEOUT = 300.0                # 单次请求超时（秒）
OUTPUT_NAME = "拆解.md"
FRAME_FACTS_NAME = "看帧.json"        # pipeline/reverse/frame_facts.py 的产物
ERROR_ARTIFACT_NAME = "拆解错误.json"  # ApiError 失败时的证据（键名已冻结，discuss.py 读取）
RAW_RESPONSE_LIMIT = 500               # 「拆解错误.json」里 raw_response 只留前 500 字
MAX_ID_LEN = 40


def resolve_endpoint(model: str | None, base_url: str | None) -> str:
    """外部渠道透传（M6-T7-C）→ Gemini 原生 generateContent 端点。

    base_url 不带版本段自动补 /v1beta（渠道配置常填裸域名）；显式带 /v1、/v1beta 等
    版本段则尊重原值。只给 model 不给 base_url 时落在既有默认渠道；两者都空返回
    既有 ENDPOINT（逐字节一致）。模型名/品牌一律来自调用方，不在本文件新增硬编码。
    """
    name = (model or "").strip()
    base = (base_url or "").strip().rstrip("/")
    if not base and not name:
        return ENDPOINT
    if not base:
        base = ENDPOINT.split("/models/", 1)[0]
    if not re.search(r"/v\d+[a-z]*$", base):
        base += "/v1beta"
    return f"{base}/models/{name or MODEL_LABEL}:generateContent"


# 十层标题：逐字匹配，顺序即层号（与 prompts/视频拆解.md 的标题逐字一致）
LAYER_TITLES = (
    "一 场景",
    "二 产品",
    "三 运动",
    "四 光影",
    "五 镜头",
    "六 意境",
    "七 分镜表",
    "八 声音",
    "九 资产",
    "十 图文与收束",
)

# 模型把模型下线通知当正文返回时的兜底识别（只在正文很短时判）
OFFLINE_MARKERS = ("已下线", "停止服务", "不再提供", "no longer available", "deprecated", "retired")

# 响应体里的媒体 mime 映射；默认按 mp4 处理（单帧判定走同一条调用层，传 .png 即可）
MIME_BY_SUFFIX = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".avi": "video/x-msvideo",
    ".mkv": "video/x-matroska",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}

FENCE_RE = re.compile(r"```[ \t]*\r?\n(.*?)\r?\n```", re.DOTALL)
HEADING_RE = re.compile(r"^ {0,3}(#{1,6})[ \t]*(.+?)[ \t]*$")


# ---------------------------------------------------------------- 异常

class PrecheckError(RuntimeError):
    """入口预检失败（退出码 2）。"""


class ApiError(RuntimeError):
    """调用失败或响应无效（重试用尽后退出码 3）。

    消息之外可携带云端原文（cloud_message / raw_response / finish_reason），主链路据此
    落盘「拆解错误.json」供事后追查；异常类型与 str() 语义不变（调用方照旧 except ApiError）。
    """

    def __init__(self, message: str, *, raw_response: str = "",
                 finish_reason=None, cloud_message: str | None = None):
        super().__init__(message)
        self.raw_response = raw_response
        self.finish_reason = finish_reason
        self.cloud_message = message if cloud_message is None else cloud_message


class EmptyResponseError(ApiError):
    """HTTP 200 但没有可用的正文（必须当失败，不得存盘）。"""


def _is_content_block(exc: BaseException) -> bool:
    """上游内容审核拦截（PROHIBITED_CONTENT / prompt_blocked / request_body_blocked）。

    2026-11-02 实测这种拦截对同一份请求是概率性的：同一部片子、同样的正文，
    换个时间又能过。所以要能认出来并降级重试，而不是当成普通 API 失败。
    """
    text = str(exc).upper()
    return any(marker in text for marker in
               ("PROHIBITED_CONTENT", "PROMPT_BLOCKED", "REQUEST_BODY_BLOCKED",
                "CONTENT POLICY", "CONTENTPOLICY"))



class LayerError(RuntimeError):
    """十层缺失（退出码 4）。"""

    def __init__(self, missing):
        self.missing = missing            # [(层号, 标题, 原因)]
        super().__init__("十层不完整")


# ---------------------------------------------------------------- 通用小工具

def redact(text: str, secret: str) -> str:
    """把密钥从任何要打印的文本里抹掉；密钥绝不落到 stdout/stderr/文件。"""
    if secret and secret in text:
        return text.replace(secret, "***")
    return text


def layer_number(title: str) -> int:
    """标题 → 层号（1..10）；非层标题返回 0。"""
    try:
        return LAYER_TITLES.index(title) + 1
    except ValueError:
        return 0


def normalize_heading(raw: str) -> str:
    """标题归一：全角空格当空格、压缩连续空白、去掉尾部标点。

    只做空白与尾标点的容错，不改字，缺一层仍然缺。
    """
    text = raw.replace("\u3000", " ").replace("\u00a0", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text.rstrip("：:。.").strip()


def load_api_key() -> str:
    """先读环境变量，再读 $HERMES_HOME/.env、~/.hermes/.env。只返回值，不打印。"""
    key = os.environ.get(KEY_ENV, "").strip()
    if key:
        return key

    candidates = []
    configured_home = os.environ.get("HERMES_HOME", "").strip()
    if configured_home:
        candidates.append(Path(configured_home) / ".env")
    candidates.append(Path.home() / ".hermes" / ".env")
    if os.name == "nt":
        candidates.append(Path.home() / "AppData" / "Local" / "hermes" / ".env")

    for env_path in candidates:
        try:
            lines = env_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            stripped = line.strip()
            if stripped.startswith(f"{KEY_ENV}="):
                value = stripped.split("=", 1)[1].strip().strip('"').strip("'")
                if value:
                    return value
    return ""


# ---------------------------------------------------------------- 提示词（单一真源）

def read_prompt(prompt_path: Path = PROMPT_PATH) -> str:
    """从定版提示词文件里取第一个 ``` 围栏内的正文，不硬编码副本。

    围栏正文里的 `{{镜头语言契约}}` 占位在返回前替换为 contracts/camera-contract.md
    的 §1+§2 全文——拆解与画布改写共用同一份契约（单一事实源），两侧不可能漂移。
    """
    try:
        raw = prompt_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PrecheckError(f"读不到提示词源头 {prompt_path}：{exc}") from exc

    match = FENCE_RE.search(raw)
    if not match:
        raise PrecheckError(f"提示词源头 {prompt_path} 里没有 ``` 围栏，无法取正文")
    # 原写法 strip(CRLF) + strip() 的等价简化（去围栏首尾空白）
    body = match.group(1).strip()

    # 源头必须自带十层标题，否则说明围栏取错了（例如取到「下游怎么消费」那段代码）
    # ⚠️ 不要对整篇 body 做 normalize_heading——它会把换行压成空格，标题行再也匹配不到
    missing = [title for title in LAYER_TITLES
               if not re.search(r"^#+[ \t]*" + re.escape(title), body, re.MULTILINE)]
    if missing:
        raise PrecheckError(
            "提示词源头第一个围栏疑似不是提示词正文，缺标题：" + "、".join(missing))

    body = _inject_camera_contract(body)
    return body


def _inject_camera_contract(body: str) -> str:
    """把 {{镜头语言契约}} 占位替换为契约 §1+§2 全文。

    占位符存在而契约文件缺失/无内容 → PrecheckError（失败即失败，不静默发半截任务书）；
    占位符不存在（旧版任务书）→ 原样返回，兼容未引用契约的历史正文。
    """
    placeholder = "{{镜头语言契约}}"
    if placeholder not in body:
        return body
    contract_path = REPO_ROOT / "contracts" / "camera-contract.md"
    try:
        contract = contract_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PrecheckError(f"任务书引用了镜头语言契约但读不到 {contract_path}：{exc}") from exc
    # §1 起到 §3 前 = 选词表 + 感受表述两节
    m = re.search(r"(## §1 .*?)(?=\n## §3 )", contract, re.S)
    if not m:
        raise PrecheckError(f"契约 {contract_path} 缺 §1/§2 段落结构，无法注入")
    section = m.group(1).strip()
    # 标题降级：契约的 ## §1/## §2 是给改写侧文档用的层级；注入拆解任务书时必须降成
    # 非标题文本——平级 ## 会把任务书「一~十」十层固定结构撕开，实测导致分镜表/资产
    # 层输出结构紊乱（角色错认、资产截图对位全错，2026-11-06 所有者真机反馈）。
    section = re.sub(r"^## (§\d+ [^\n]+)$", r"【\1】", section, flags=re.M)
    return body.replace(placeholder, section)


def build_diagnosis_block(diagnosis: str) -> str:
    """门禁诊断 → 追加到本轮任务书末尾（AT-15 R1 自动重拆用）。

    纪律（红线 24 / 20）：
      · **非标题形态**：整段只用【…】与「- 」列表，`#` 一律去掉——平级标题会把任务书的
        「一~十」十层结构撕开（2026-11-06 契约注入踩过：角色错认、资产截图对位全错）。
      · 写法指导式：告诉模型「该写成什么样」，不是把上一版骂一遍。
      · 不引入新的拆解目标：十层立场不变（只描述参考片里真实存在的东西）。
    文本由 pipeline/reverse/dissect_gate.py:build_diagnosis 组装（判据与下游同源）。
    """
    body = str(diagnosis or "").strip()
    if not body:
        return ""
    body = re.sub(r"(?m)^[ \t]*#{1,6}[ \t]*", "", body)
    return "\n\n" + body + "\n"


def build_prompt(base_text: str, intent: str, facts: dict | None = None,
                 frame_facts: dict | None = None, inject: str = "fields",
                 diagnosis: str = "") -> str:
    """把用户意图追加到视频拆解提示词；空意图必须保持原文逐字不变。

    2026-10-30 实测教训：初版写的是「围绕这条意图更有针对性地观察与拆解」，
    模型把「九 资产」层直接改写成「针对替换人物需求，需锁定并准备以下资产：」——
    中性 IR 被替换计划污染，下游资产清单跟着变成用户要准备的东西，而不是片子里有的东西。
    所以这里改成**只准改详略、不准改立场**：十层永远只描述参考片里真实存在的东西。

    2026-11-01 追加：机器能测的事实（硬切次数）必须先告诉模型，否则它会自己猜——
    实测它把 0 次硬切的片子答成「一镜到底：否」并标了一镜「剪辑」。

    2026-11-02 追加：有 看帧.json 时把每帧的单帧专项判定也告诉它（人物朝向/视线这类
    「一眼能定却写错」的事实）。没有这个文件就一个字都不加，旧流程照跑。
    """
    text = base_text
    if facts is not None:
        cuts = facts.get("hard_cuts") or []
        if cuts:
            where = "、".join(f"{t:.2f}" for t in cuts[:12])
            rule = ("- 因此「七 分镜表」里：一镜到底答「否」；"
                    "发生硬切的那几镜，转场方式标 `剪辑`，并在该镜的起秒-止秒里对上实测时间点。")
        else:
            where = "（无）"
            rule = ("- 因此「七 分镜表」里：一镜到底**必须答「是」**；"
                    "转场方式只许写 `无剪切` / `穿屏` / `遮挡`，**不许写 `剪辑`**。")
        text += (
            "\n\n【机器实测（这条是测量结果，不是让你判断的）】\n"
            + f"- 硬切（scene>0.3）：{len(cuts)} 次；时间点：{where}\n"
            + rule
            + "\n- 手或物体从屏幕、镜子、水面里钻出来时：写清穿过的是哪块面、从哪个位置穿出、"
            "穿出后往哪走，并把该镜转场标 `穿屏`。从画面边缘进来的，写清从哪一侧进来、"
            "先碰到什么。\n"
        )
    text += build_frame_facts_block(_pick_inject(frame_facts, inject),
                                    filler=(inject == "filler"),
                                    fields_only=(inject == "fields"))
    if inject not in ("off", "filler"):
        # 尾帧判定 → 第十层「图文与收束」锚点：有尾帧判定才注入，没有则一个字不加（含 off/filler）
        text += build_tail_brand_block(frame_facts,
                                       (facts or {}).get("duration") if facts else None)
    if not intent:
        return text + build_diagnosis_block(diagnosis)
    return (
        text
        + "\n\n【本次用户意图（仅供你判断哪些细节要多写两句，不是你这次的任务）】\n"
        + intent
        + "\n\n【执行约束】\n"
        + "1. 十层的立场永远不变：每一层都只描述**参考片里真实存在的东西**。\n"
        + "2. 十层标题、顺序、数量必须原样保持，不许加新标题、不许加节、不许合并或删节。\n"
        + "3. 用户的意图只影响**详略**：与他意图相关的元素，把形态、出现秒数、与其他元素的关系写细一点；"
        + "不相关的元素照常写。\n"
        + "4. 严禁把意图当成拆解目标。十层里不许出现「针对……需求」「需准备」「需锁定」「待替换」"
        + "「资产准备」这类为替换服务的措辞；第九层（资产）照旧只列片子里看得见的东西，"
        + "不许写用户要另外准备什么。\n"
        + "5. 不许把用户意图里的第一人称原句写进十层正文。"
    ) + build_diagnosis_block(diagnosis)


def write_intent(outdir: Path, intent: str) -> None:
    """把本次意图原文写入任务目录；空意图清掉旧值。"""
    target = outdir / "意图.txt"
    if not intent:
        try:
            target.unlink(missing_ok=True)
        except OSError as exc:
            raise OSError(f"清理旧意图失败：{exc}") from exc
        return
    _atomic_write(target, intent)


# ---------------------------------------------------------------- ffprobe 预检

def probe_video(video_path: Path, timeout: float = 60.0) -> dict:
    """ffprobe 预检：拿时长、流类型、有无音轨。不可解码即抛 PrecheckError。"""
    if not video_path.exists():
        raise PrecheckError(f"视频不存在：{video_path}")
    if not video_path.is_file():
        raise PrecheckError(f"不是文件：{video_path}")

    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-show_entries", "stream=codec_type,width,height,avg_frame_rate,r_frame_rate",
        "-of", "json",
        str(video_path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
    except FileNotFoundError as exc:
        raise PrecheckError("找不到 ffprobe，请装 ffmpeg 并把 bin 目录加进 PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise PrecheckError(f"ffprobe 超过 {timeout:.0f}s 未返回，文件可能损坏") from exc

    stderr_tail = (proc.stderr or "").strip().splitlines()[-3:]
    stderr_text = " / ".join(stderr_tail) if stderr_tail else "无 stderr 输出"

    if proc.returncode != 0:
        raise PrecheckError(f"ffprobe 判为不可解码（退出码 {proc.returncode}）：{stderr_text}")

    try:
        info = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise PrecheckError(f"ffprobe 输出不是 JSON：{stderr_text}") from exc

    raw_duration = (info.get("format") or {}).get("duration")
    try:
        duration = float(raw_duration)
    except (TypeError, ValueError) as exc:
        raise PrecheckError(
            f"拿不到时长（format.duration={raw_duration!r}），文件可能损坏或不是视频：{stderr_text}"
        ) from exc
    if not (duration > 0):
        raise PrecheckError(f"时长非法（{duration}），文件可能是空壳：{stderr_text}")

    codec_types = [(stream or {}).get("codec_type") for stream in (info.get("streams") or [])]
    has_video = "video" in codec_types
    has_audio = "audio" in codec_types
    if not has_video:
        raise PrecheckError("ffprobe 没找到视频流（只有音轨或空容器），无法拆解画面")

    # 画幅/尺寸/帧率由 ffprobe 直接量，不问模型（模型看不见像素尺寸，问它必编）。
    # 2026-10-31 所有者反馈：反推和提示词里都没有比例，生成时会出问题。
    width = height = 0
    fps = 0.0
    for stream in (info.get("streams") or []):
        if (stream or {}).get("codec_type") != "video":
            continue
        try:
            width = int(stream.get("width") or 0)
            height = int(stream.get("height") or 0)
        except (TypeError, ValueError):
            width = height = 0
        for key in ("avg_frame_rate", "r_frame_rate"):
            raw = str(stream.get(key) or "")
            if "/" in raw:
                num, _, den = raw.partition("/")
                try:
                    if float(den or 0):
                        fps = round(float(num) / float(den), 2)
                        break
                except (TypeError, ValueError):
                    pass
        break

    return {
        "duration": duration,
        "has_video": has_video,
        "has_audio": has_audio,
        "width": width,
        "height": height,
        "fps": fps,
        "aspect": aspect_label(width, height),
        "codec_types": [codec for codec in codec_types if codec],
        "stderr": stderr_text,
        "hard_cuts": detect_hard_cuts(video_path, timeout),
    }


SCENE_THRESHOLD = 0.3


def detect_hard_cuts(video_path: Path, timeout: float = 60.0) -> list[float]:
    """机器量硬切：返回硬切时间点（秒）。量不到就返回空表，绝不让模型去猜。

    2026-11-01 实测：`select='gt(scene,0.3)',showinfo` 在这条片上命中 0 帧，
    与甲方原提示词的「一镜到底，无剪切」一致；而视觉模型自己答了「一镜到底：否」
    并把某一镜标成「剪辑」。这件事机器能测，就不该交给模型判断。
    """
    cmd = [
        "ffmpeg", "-hide_banner", "-nostats",
        "-i", str(video_path),
        "-filter:v", f"select='gt(scene,{SCENE_THRESHOLD})',showinfo",
        "-f", "null", "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return []
    hits = []
    for line in (proc.stderr or "").splitlines():
        if "showinfo" not in line:
            continue
        m = re.search(r"\bn:\s*(\d+)\b.*?\bpts_time:\s*([0-9.]+)", line)
        if m:
            hits.append(round(float(m.group(2)), 3))
    return hits


# ---------------------------------------------------------------- 调模型

def make_video_id(video_path: Path) -> str:
    """id = 文件名去扩展名 → 去零宽字符（U+200B 等）→ 空格换 _ → 截断 40 字符。"""
    stem = video_path.stem
    for invisible in ("\u200b", "\u200c", "\u200d", "\ufeff"):
        stem = stem.replace(invisible, "")
    stem = re.sub(r"\s+", "_", stem.strip())
    stem = re.sub(r'[<>:"/\\|?*]', "_", stem)      # Windows 文件名非法字符
    stem = stem.strip("._") or "video"
    return stem[:MAX_ID_LEN]


def _build_opener():
    """聚鑫海外站必须直连：ProxyHandler({}) 清空代理，否则 SSLEOF 或空 candidates。"""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=context),
    )


def _post_generate_content(opener, api_key: str, payload: dict, timeout: float) -> str:
    """POST 一次；返回原始响应文本。密钥只进 URL 查询串，不出现在异常文本里。"""
    url = f"{ENDPOINT}?key={urllib.parse.quote(api_key, safe='')}"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with opener.open(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def _http_cloud_message(exc: urllib.error.HTTPError, body: str) -> str:
    """HTTP 错误的云端原文：优先取网关 JSON 里的 error.message（完整不截断）；
    取不到就把整个响应体当原文——绝不自己编原因。"""
    text = (body or "").strip()
    if text:
        try:
            envelope = json.loads(text)
        except json.JSONDecodeError:
            envelope = None
        if isinstance(envelope, dict):
            error = envelope.get("error")
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                return f"HTTP {exc.code} {exc.reason}：{error['message']}"
            if isinstance(error, str) and error:
                return f"HTTP {exc.code} {exc.reason}：{error}"
    return f"HTTP {exc.code} {exc.reason}" + (f"：{text}" if text else "")


def _parse_response(raw: str):
    """解析候选正文与用量；空正文一律抛 EmptyResponseError（不许当成功）。

    失败时把云端原文挂到异常上（raw_response / finish_reason），主链路据此落盘
    「拆解错误.json」；异常消息文本保持原样，stderr 与调用方行为都不变。
    """
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ApiError(f"响应不是 JSON（前 200 字：{raw[:200]!r}）",
                       raw_response=raw) from exc
    if not isinstance(envelope, dict):
        raise ApiError("响应 JSON 顶层不是对象", raw_response=raw)

    feedback = envelope.get("promptFeedback") or {}
    if feedback.get("blockReason"):
        raise ApiError(f"请求被拦截：blockReason={feedback.get('blockReason')}",
                       raw_response=raw)
    if envelope.get("error"):
        error = envelope["error"]
        message = error.get("message") if isinstance(error, dict) else str(error)
        blocked = envelope.get("candidates") or []
        raise ApiError(f"接口返回 error：{message}", raw_response=raw,
                       finish_reason=(blocked[0] or {}).get("finishReason") if blocked else None)

    candidates = envelope.get("candidates") or []
    if not candidates:
        raise EmptyResponseError("candidates 为空（直连但无候选，常见于走代理或额度问题）",
                                 raw_response=raw)

    texts = []
    for candidate in candidates:
        content = (candidate or {}).get("content") or {}
        for part in (content.get("parts") or []):
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                texts.append(part["text"])
    text = "".join(texts).strip()

    finish_reason = (candidates[0] or {}).get("finishReason")
    if finish_reason in ("SAFETY", "RECITATION"):
        raise ApiError(f"候选被 {finish_reason} 截断，正文不可用", raw_response=raw,
                       finish_reason=finish_reason)
    if not text:
        raise EmptyResponseError(f"正文长度为 0（finishReason={finish_reason}）",
                                 raw_response=raw, finish_reason=finish_reason)
    if len(text) < 300 and any(marker in text for marker in OFFLINE_MARKERS):
        raise ApiError(f"正文疑似模型下线通知：{text[:120]!r}", raw_response=raw,
                       finish_reason=finish_reason)

    usage = envelope.get("usageMetadata") or {}
    return text, usage


def call_model(prompt: str, video_path: Path, api_key: str, timeout: float = DEFAULT_TIMEOUT):
    """带重试的调用：最多 1 次首发 + 3 次重试，退避 5/10/20 秒。

    `video_path` 也可以是一张图（.png / .jpg）：单帧判定（frame_facts.py）走的是同一条
    路——同一个端点、同一套代理/空响应/重试/redact 纪律，不另写一套 HTTP。
    """
    payload = {
        "contents": [{
            "parts": [
                {"text": prompt},
                {
                    "inline_data": {
                        "mime_type": MIME_BY_SUFFIX.get(video_path.suffix.lower(), "video/mp4"),
                        "data": base64.standard_b64encode(video_path.read_bytes()).decode("ascii"),
                    }
                },
            ]
        }]
    }
    opener = _build_opener()
    attempts = 1 + len(BACKOFF_SECONDS)
    started = time.monotonic()
    last_error = "未知失败"
    # 最后一次尝试的证据（云端原文 / 原始响应 / finishReason）：重试用尽时挂到抛出的
    # ApiError 上，主链路据此写「拆解错误.json」。只留痕，不参与任何判定。
    last_raw, last_finish, last_cloud = "", None, ""

    for attempt in range(1, attempts + 1):
        last_raw, last_finish, last_cloud = "", None, ""
        try:
            print(f"[调用] 第 {attempt}/{attempts} 次 直连 {MODEL_LABEL}", flush=True)
            raw = _post_generate_content(opener, api_key, payload, timeout)
            text, usage = _parse_response(raw)
            elapsed = time.monotonic() - started
            print(f"[成功] 耗时 {elapsed:.1f}s，正文 {len(text)} 字", flush=True)
            return text, usage, elapsed
        except EmptyResponseError as exc:
            last_error = f"空响应：{redact(str(exc), api_key)}"
            last_cloud = str(exc)
            last_raw, last_finish = exc.raw_response, exc.finish_reason
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                body = ""
            last_error = f"HTTP {exc.code} {exc.reason}" + (f" / {body[:300]}" if body else "")
            last_error = redact(last_error, api_key)
            last_cloud = _http_cloud_message(exc, body)
            last_raw, last_finish = body, None
        except urllib.error.URLError as exc:
            last_error = redact(f"URLError：{exc.reason}", api_key)
        except (ssl.SSLError, OSError) as exc:
            last_error = redact(f"{type(exc).__name__}：{exc}", api_key)
        except ApiError as exc:
            last_error = redact(str(exc), api_key)
            last_cloud = exc.cloud_message
            last_raw, last_finish = exc.raw_response, exc.finish_reason
        except Exception as exc:                       # 兜底：绝不把异常当结果
            last_error = redact(f"{type(exc).__name__}：{exc}", api_key)

        last_cloud = last_cloud or last_error          # 取不到云端原文就退回失败描述

        print(f"[失败] 第 {attempt} 次：{last_error}", file=sys.stderr, flush=True)
        if attempt <= len(BACKOFF_SECONDS):
            delay = BACKOFF_SECONDS[attempt - 1]
            print(f"[退避] {delay}s 后重试", file=sys.stderr, flush=True)
            time.sleep(delay)

    raise ApiError(f"{attempts} 次尝试全部失败，最后一次：{last_error}",
                   raw_response=last_raw, finish_reason=last_finish,
                   cloud_message=last_cloud or last_error)


def clear_api_error(outdir: Path) -> None:
    """清掉上一次失败留下的证据：这一次成功了就不该有「拆解错误.json」。"""
    try:
        (Path(outdir) / ERROR_ARTIFACT_NAME).unlink(missing_ok=True)
    except OSError:                                    # 删不掉不算失败，不拖垮主流程
        pass


def write_api_error(outdir: Path, exc: ApiError, api_key: str = "") -> Path | None:
    """ApiError 失败时把云端原文落盘：`analysis/<dir>/拆解错误.json`。

    与 call_vlm.py 写 vision_error.json 同型——证据要能事后追查，不能只留在 stderr。
    schema 已冻结（键名被 discuss.py 读取，不许改名）：
        error_type / message（完整不截断）/ finish_reason / raw_response（原始响应前 500 字）
    写盘失败只告警，绝不掩盖原本的退出码 3（stderr 与退出码仍由调用方照旧处理）。
    """
    payload = {
        "error_type": "ApiError",
        "message": redact(exc.cloud_message, api_key),
        "finish_reason": exc.finish_reason,
        "raw_response": redact(exc.raw_response, api_key)[:RAW_RESPONSE_LIMIT],
    }
    path = Path(outdir) / ERROR_ARTIFACT_NAME
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    except OSError as exc_write:
        print(f"ERROR 写不了 {ERROR_ARTIFACT_NAME}：{exc_write}", file=sys.stderr)
        return None
    return path


# ---------------------------------------------------------------- 十层切段与校验

def split_layers(markdown: str):
    """按 `^#{1,6}` 切段；返回 (层标题 → 正文, 缺失清单)。

    缺失清单元素是 (层号, 标题, 原因)，原因分「标题缺失」「正文为空」。
    """
    sections = {}
    order = []
    current_title = None
    buffer = []

    def flush():
        if current_title is not None:
            sections.setdefault(current_title, []).extend(buffer)

    for line in markdown.splitlines():
        match = HEADING_RE.match(line)
        if match:
            flush()
            current_title = normalize_heading(match.group(2))
            buffer = []
            order.append(current_title)
        elif current_title is not None:
            buffer.append(line)
    flush()

    bodies = {title: "\n".join(lines).strip() for title, lines in sections.items()}

    missing = []
    for index, title in enumerate(LAYER_TITLES, start=1):
        key = normalize_heading(title)
        if key not in bodies:
            missing.append((index, title, "标题缺失"))
        elif not bodies[key]:
            missing.append((index, title, "正文为空"))
    return bodies, missing


def require_layers(markdown: str):
    """校验十层；有缺失就抛 LayerError。返回 层标题 → 正文。"""
    bodies, missing = split_layers(markdown)
    if missing:
        raise LayerError(missing)
    return {title: bodies[normalize_heading(title)] for title in LAYER_TITLES}


def format_missing(missing) -> str:
    """缺失清单 → 可读报告（stderr 用）。"""
    lines = [f"缺 {len(missing)} 层（十层必须逐字齐全）："]
    for index, title, reason in missing:
        lines.append(f"  缺第 {index} 层：{title}（{reason}）")
    return "\n".join(lines)


# ---------------------------------------------------------------- 八 声音结构化（AT-17 B1）
#
# 「八 声音」是十层里唯一有机器可读形态的一层（任务书 v2.6 起）：BGM 四字段行 +
# 音效点[] + 台词逐句带说话人（枚举仅 画外音/出镜者）。解析就放在 extract ——
# 这一层的生产者与格式所有者这边，dissect_gate 导入同一个函数来判（红线 25：
# 判据与消费同源，不另写一套正则）。
#
# 十层标题与 require_layers 一个字没动：结构化只是正文内容变了，切层/缺层判定照旧。

SOUND_TITLE = "八 声音"
NO_AUDIO_TEXT = "无音轨"
BGM_FIELDS = ("BGM.情绪", "BGM.节奏", "BGM.乐器/织体", "BGM.强弱走向")
SPEAKER_LABELS = ("画外音", "出镜者")
# 行内键名**不带冒号**（_value_after 自己补全角/半角冒号，带了就永远匹配不上）
DIALOGUE_MARK = "台词"
SOUND_POINT_MARK = "音效点"
LANGUAGE_MARK = "人声语言"
NO_DIALOGUE_VALUES = ("无", "无台词", "不适用")
TIME_SPAN_RE = re.compile(r"\d{1,2}:\d{2}(?:\.\d+)?\s*[-–—~～]\s*\d{1,2}:\d{2}(?:\.\d+)?")
QUOTED_RE = re.compile(r"[「『“\"](.+?)[」』”\"]")
SPEAKER_RE = re.compile(r"说话人\s*[：:]\s*([^｜|｜\s，,；;。]+)")


def _plain_lines(body: str) -> list[str]:
    """正文行 → 去项目符号/加粗后的行（只用来取键值，不改原文一个字）。"""
    lines = []
    for raw in body.splitlines():
        line = raw.replace("**", "").replace("`", "").strip()
        lines.append(re.sub(r"^[-*+•]\s*", "", line))
    return lines


def _value_after(line: str, key: str) -> str | None:
    """`键：值` / `键:值` → 值；不是这一行返回 None。"""
    for sep in ("：", ":"):
        if line.startswith(key + sep):
            return line[len(key) + len(sep):].strip()
    return None


def sound_body(markdown: str) -> str:
    """拆解 markdown 的「八 声音」正文（没有这一层时返回空串）。"""
    bodies, _missing = split_layers(markdown)
    return bodies.get(normalize_heading(SOUND_TITLE), "")


def audio_track_flag(markdown: str) -> bool | None:
    """拆解头部元信息里的 `音轨: 有/无` → True/False；读不到返回 None（旧产物/手写件）。"""
    match = re.search(r"音轨\s*[：:]\s*(有|无)", markdown)
    if not match:
        return None
    return match.group(1) == "有"


def parse_sound_layer(body: str) -> dict:
    """八 声音正文 → 结构化字段。只读文本：不猜、不补、不发明。

    返回：no_audio / bgm{字段: 值} / bgm_missing[] / sound_points[] /
    dialogue[{start_end, original, speaker}] / dialogue_missing_speaker[] /
    no_dialogue / spoken_language。
    """
    lines = _plain_lines(body)
    result = {
        "no_audio": body.replace("**", "").strip() == NO_AUDIO_TEXT,
        "bgm": {},
        "bgm_missing": [],
        "sound_points": [],
        "dialogue": [],
        "dialogue_missing_speaker": [],
        "no_dialogue": False,
        "spoken_language": "",
    }
    for key in BGM_FIELDS:
        value = ""
        for line in lines:
            found = _value_after(line, key)
            if found is not None:
                value = found
                break
        result["bgm"][key] = value
        if not value:
            result["bgm_missing"].append(key)

    for line in lines:
        value = _value_after(line, SOUND_POINT_MARK)
        if value and value not in NO_DIALOGUE_VALUES:
            result["sound_points"].append(value)

    for line in lines:
        value = _value_after(line, LANGUAGE_MARK)
        if value is not None:
            result["spoken_language"] = "" if value in NO_DIALOGUE_VALUES else value
            break

    for line in lines:
        value = _value_after(line, DIALOGUE_MARK)
        if value is None:
            continue
        if value in NO_DIALOGUE_VALUES:
            result["no_dialogue"] = True
            continue
        code = TIME_SPAN_RE.search(value)
        if not code:
            continue
        quoted = QUOTED_RE.search(value[code.end():])
        if not quoted or not quoted.group(1).strip():
            continue
        speaker = ""
        match = SPEAKER_RE.search(value)
        if match:
            speaker = next((label for label in SPEAKER_LABELS if label in match.group(1)), "")
        result["dialogue"].append({
            "start_end": re.sub(r"\s*[-–—~～]\s*", "-", code.group(0).strip()),
            "original": quoted.group(1).strip(),
            "speaker": speaker,
        })
        if not speaker:
            result["dialogue_missing_speaker"].append(quoted.group(1).strip())

    if not result["dialogue"] and not result["no_dialogue"]:
        result["no_dialogue"] = True          # 逐句台词与「台词：无」都没有 = 这片没台词
    return result


def ensure_sound_layer(markdown: str, has_audio: bool | None) -> tuple[str, str]:
    """无音轨片：八 声音正文必须只剩「无音轨」——省字段不省层（B0 附注 1）。

    返回 (正文, 动作)：""=未动 / "filled"=模型没写正文，补一行 / "replaced"=模型照样写了
    声音内容，但这条片子没有音轨（ffprobe 量出来的），按规则改写成「无音轨」——
    不采信没音轨的片子里编出来的 BGM/台词，也不删层（十层标题照旧，红线 24）。
    模型连八层标题都漏掉的，仍按缺层失败（不替它造标题）。
    """
    if has_audio is not False:
        return markdown, ""
    body = sound_body(markdown).strip()
    if body == NO_AUDIO_TEXT:
        return markdown, ""
    key = normalize_heading(SOUND_TITLE)
    bodies, _missing = split_layers(markdown)
    if key not in bodies:
        return markdown, ""
    out: list[str] = []
    inside = False
    done = False
    for line in markdown.splitlines():
        match = HEADING_RE.match(line)
        if match:
            inside = normalize_heading(match.group(2)) == key
            out.append(line)
            if inside and not done:
                out.append(NO_AUDIO_TEXT)
                done = True
            continue
        if inside:
            continue                      # 丢掉无音轨片里的声音内容（已记在动作里，不静默）
        out.append(line)
    if not done:
        return markdown, ""
    return "\n".join(out) + "\n", ("replaced" if body else "filled")


# ---------------------------------------------------------------- 落盘

def aspect_label(width: int, height: int) -> str:
    """像素尺寸 → 视频模型能选的画幅（只认 16:9 / 9:16 / 1:1 / 4:3 / 3:4，取最近）。

    源片是 1080×1888 这类非标准比例时，以前会原样写「135:236」——生成平台上根本
    没有这个选项。归一规则与理由见 pipeline/reverse/aspect.py。
    """
    from aspect import canonical_aspect  # 同目录模块：脚本直接跑时 sys.path[0] 就是这里

    return canonical_aspect(width, height)


# ---------------------------------------------------------------- 看帧判定（单帧专项判定）

# 「背对镜头」与「背朝画面右侧」不是一回事：前者是人背对观者，后者只说背部的朝向。
# 2026-11-02 实测：只看 背对/背向/背朝 三个词，会把「身体侧对镜头、背朝画面右侧」这句
# 正确的话读成「背对镜头」并报成矛盾。所以判「背对」必须跟上 镜头/观众 这个落点。
BACK_ON_CAMERA_RE = re.compile(r"背(?:对|向|朝)\s*(?:着)?\s*(?:镜头|摄像|相机|机位|观众)")
SIDE_ON_RE = re.compile(r"侧(?:对|身|面|向)|3/4|四分之三|斜侧")
QUARTER_BACK_RE = re.compile(r"侧背|侧后|后侧|斜背|半背")
BACK_WORD_RE = re.compile(r"背(?:对|向|朝|过)|后背|背影")
FRONT_ON_RE = re.compile(r"正对|正面|面对|面向|面朝|迎面|正朝|转正|冲着镜头")
FACING_MARKERS = ("侧对", "侧身", "侧面", "侧向", "侧背", "背对", "背向", "背朝", "背影",
                  "正对", "正面", "面对", "面向", "面朝", "朝向")
# 身体相对镜头的粗档。四档 + 不确定：侧与背之间本来就有「侧背」这一档，硬三选一
# 会把中间态判成两头中的一个（2026-11-02 实测：同一张图连判三次，有一次把侧对读成背对）。
FACING_CLASSES = ("正对", "侧对", "侧背", "背对", "不确定")
FACING_TOKENS = {"正对": "front", "侧对": "side", "侧背": "sideback", "背对": "back"}
UNCLEAR_FACING = ("看不清", "不确定", "拿不准", "说不清", "无法判断", "判断不了")
GAZE_MARKERS = ("视线", "目光", "眼神", "眼睛", "看向", "看着", "望着", "望向",
                "注视", "直视", "盯着", "垂眼", "闭眼", "看", "凝视", "抬眼")
AT_CAMERA_RE = re.compile(r"(?:对|朝|向|看|望|注视|直视|盯着|斜视|瞥向|瞟向)\s*(?:着)?\s*"
                          r"(?:镜头|摄像|相机|机位|观众)")
# 「直视」「注视」后面没跟任何宾语时，在这种对着人拍的片子里就是看镜头；
# 但只要后面跟了「前方/远处/手机/脚下…」就说明看的是别处，不算。
BARE_CAMERA_RE = re.compile(
    r"(?:直视|凝视|注视|盯着|看着|望向|看向|抬眼)\s*(?!前方|远处|远方|别处|一侧|侧面|旁边|"
    r"窗外|地面|脚下|手机|手中|手里|屏幕|背后|身后|画面外)")
AWAY_OBJECT_RE = re.compile(
    r"(?:看|望|凝视|注视|盯着|望向|看向)\s*(?:着)?\s*(?:前方|远处|远方|别处|一侧|侧面|旁边|"
    r"窗外|地面|脚下|手机|手中|手里|屏幕|背后|身后|画面外)")
NEG_CAMERA_RE = re.compile(r"(?:不|没|未|别|避开|移开|躲开)[^，,；;。]{0,4}"
                           r"(?:看|对|朝|望|注视)(?:着)?\s*(?:镜头|摄像|相机|机位)")
AWAY_WORDS = ("画外", "别处", "移开", "避开", "低头", "垂眼", "闭眼", "侧后方", "背影")



def _clause_with(text: str, markers) -> str:
    """按标点切成小句，返回第一个含指定标记的小句；一句都没有就返回空串。"""
    for clause in re.split(r"[，,、；;。\n]", text or ""):
        if any(marker in clause for marker in markers):
            return clause.strip()
    return ""


def facing_class(text: str, field: bool = False) -> str:
    """身体相对镜头的粗档：`正对` / `侧对` / `侧背` / `背对` / `不确定`。

    只看**第一句讲朝向的话**：一句话里前后提了两件事时，先说的那件才是对身体姿势的
    定性（「背对镜头，侧身向右转头」是先说背对；「身体侧对镜头、背朝画面右侧」是先
    说侧对）。

    分档的判据都是看得见、数得出的东西：
      正对 → 胸口整片可见、两侧肩头都在、看不见背脊；
      侧对 → 一侧肩线与腰侧轮廓清楚、胸口只露一半；
      侧背 → 主要看见后背与肩胛、背脊线清楚、胸口只剩一条边；
      背对 → 整片后背对着镜头、胸口完全看不见。
    卡在两条之间、或三样里两样看不清 → 不确定（写不确定不扣分，硬猜才让人没法用）。

    `field=True` 用于「朝向」列 / `body_orientation.facing` 这种本来就是回答朝向的
    短词：里面出现「背」就算背对。自由句子必须带 镜头/观众 这个落点——「背朝画面
    右侧」只说背在画面哪一侧，不是朝向读数（2026-11-02 那次误报就出在这里）。
    """
    raw = (text or "").strip()
    if not raw or raw in ("—", "-", "/", "无", "无人物"):
        return "不确定"
    if any(word in raw for word in UNCLEAR_FACING):
        return "不确定"
    clause = _clause_with(raw, FACING_MARKERS) or raw
    if QUARTER_BACK_RE.search(clause):
        return "侧背"
    if SIDE_ON_RE.search(clause):
        # 同一小句里既说侧面又说背 → 卡在中间那一档（「3/4 侧面、略背向」）
        return "侧背" if BACK_WORD_RE.search(clause) else "侧对"
    if BACK_ON_CAMERA_RE.search(clause):
        return "背对"
    if BACK_WORD_RE.search(clause):
        return "背对" if (field or "背影" in clause or "后背" in clause) else "不确定"
    if FRONT_ON_RE.search(clause):
        return "正对"
    return "不确定"


def camera_relation(text: str, field: bool = False) -> str:
    """身体相对镜头：`back` 背对 / `sideback` 侧背 / `side` 侧对 / `front` 正对 / 空。

    判据只有一份：facing_class()。判不出就返回空串（不猜）。
    调用方要比「是不是相反」时用 facing_conflict()，不要自己写 `==`/`!=`：
    侧与背之间是连续的四档，相邻两档不是矛盾。
    """
    return FACING_TOKENS.get(facing_class(text, field=field), "")


def facing_conflict(a: str, b: str) -> bool:
    """两个朝向读数是不是**真相反**：只有 正对 ↔ 背对 这一对算。

    侧对 ↔ 侧背、侧背 ↔ 背对是邻近态——同一张图连判两次就会在这几档之间摆
    （2026-11-02 实测：三次里有一次把侧对读成背对），拿它们扣帽子只会逼人复核
    一堆没错的句子。a/b 认中文档，也认 camera_relation 的旧记号。
    """
    names = {"front": "正对", "side": "侧对", "sideback": "侧背", "back": "背对"}
    return {names.get(a, a), names.get(b, b)} == {"正对", "背对"}


def gaze_relation(text: str) -> str:
    """视线落在哪：`camera` 看镜头 / `away` 不看镜头 / 空（判不出就不判）。"""
    clause = _clause_with(text, GAZE_MARKERS)
    if not clause:
        return ""
    if "看不清" in clause:
        return ""
    if NEG_CAMERA_RE.search(clause):
        return "away"
    if AT_CAMERA_RE.search(clause):
        return "camera"
    if AWAY_OBJECT_RE.search(clause):
        return "away"
    if any(word in clause for word in AWAY_WORDS):
        return "away"
    # 光说「直视」「注视」「盯着」而没讲看哪儿：对着人拍的片子里按看镜头算。
    # 以前这里一律回 away，于是「转头直视」被判成「不看镜头」，
    # 跟判定里的「直视镜头」凑成假冲突（2026-11-02 真机撞到）。
    if BARE_CAMERA_RE.search(clause):
        return "camera"
    return ""


def first_frame_text(body: str) -> str:
    """从整份拆解里取「首帧：」那一行的正文；没有就空串。"""
    match = re.search(r"^[ \t]*(?:[-*+][ \t]+)?首帧[ \t]*[：:][ \t]*(.*)$",
                      body or "", re.MULTILINE)
    return match.group(1).strip() if match else ""


def facing_clause(text: str) -> str:
    """一句话里讲身体朝向的那半句；没有就空串。"""
    return _clause_with(text, FACING_MARKERS)


def gaze_clause(text: str) -> str:
    """一句话里讲视线的那半句；没有就空串。"""
    return _clause_with(text, GAZE_MARKERS)


def _frame_entry(frame_facts: dict | None, seconds: float = 0.0) -> dict | None:
    for frame in (frame_facts or {}).get("帧") or []:
        try:
            if abs(float(frame.get("秒", -1e9)) - float(seconds)) < 1e-6:
                return frame
        except (TypeError, ValueError):
            continue
    return None


def _judged_class(body: dict, raw_facing: str) -> str:
    """判定里那一档朝向：优先用模型自己填的 facing_class；缺了 / 认不出就从自由描述词派生。

    「歧」原样保留：窗口聚合发现各帧说法不一致时就是这个值，不能再从空朝向词派生出
    「不确定」——那会把「三帧说法不一」说成「看不清」，是两件事。
    """
    stated = str((body or {}).get("facing_class") or "").strip()
    if stated in FACING_CLASSES or stated == "歧":
        return stated
    return facing_class(raw_facing, field=True)


def _facts_windows(frame_facts: dict | None) -> list[dict]:
    """看帧.json → 窗口表（每个窗口带「聚合」）。

    老格式（只有「帧」、没有「窗口」）由 frame_facts.readable_frames 折成单帧窗口：
    下游只认一种形状，不用到处写 `if 窗口 … else 帧`。
    """
    module = frame_facts_module()
    return module.readable_frames(frame_facts) if module is not None else []


def _window_entry(frame_facts: dict | None, start: float = 0.0) -> dict | None:
    """起点是 `start` 的那个窗口；没有返回 None。"""
    for window in _facts_windows(frame_facts):
        try:
            if abs(float(window.get("起", -1e9)) - float(start)) < 1e-6:
                return window
        except (TypeError, ValueError):
            continue
    return None


def _aggregate_of(window: dict | None) -> dict:
    """窗口里的聚合判定；老格式折出来的窗口也带聚合。"""
    aggregated = (window or {}).get("聚合")
    return aggregated if isinstance(aggregated, dict) else {}


def _window_phrase(window: dict | None) -> str:
    """冲突句子里的自称：窗口 0:0.5 → 「0–0.5 秒窗口」；单帧 0 → 「0 秒那一帧」。"""
    label = str((window or {}).get("窗口") or "")
    if ":" in label:
        return f"{label.replace(':', '–')} 秒窗口"
    return f"{label or '0'} 秒那一帧"


def _low_confidence(judgment: dict) -> bool:
    """这一条判定有没有「没定论」：窗口聚合低置信（有分歧），或老记录里同一帧多次判定不一。

    低置信只说明「这个字段没有共识」，不等于画面看不出来——所以只提示，不拿去判冲突。
    """
    if bool((judgment or {}).get("分歧")):
        return True
    module = frame_facts_module()
    return bool(module.votes_disagree(judgment)) if module is not None else False


def frame_facts_conflicts(first_frame: str, frame_facts: dict | None) -> list[str]:
    """首帧行 vs 看帧判定（首帧那个窗口）：朝向或视线**真相反**才记冲突。

    朝向只认 正对 ↔ 背对 这一对相反；侧对 / 侧背 / 背对 之间是连续的四档，相邻两档
    不算矛盾。窗口聚合低置信（三帧里说法不一）的朝向本身没有共识，不报——注入那一行
    已经写明「这一窗口三帧里 … 不一，以描述为准」。字段写「歧」的也不报。
    判不出的（字段空、写「—」）一律不报——不拿没看清的东西扣帽子。
    """
    window = _window_entry(frame_facts, 0.0)
    judgment = _aggregate_of(window)
    if not judgment:
        return []

    conflicts: list[str] = []
    low = _low_confidence(judgment)
    body = judgment.get("body_orientation")
    if isinstance(body, str):
        body = {"facing": body, "back_toward": ""}
    raw_facing = str((body or {}).get("facing") or "").strip()
    if not low and raw_facing and raw_facing != "歧":
        fact_class = _judged_class(body or {}, raw_facing)
        said_clause = facing_clause(first_frame)
        said_class = facing_class(said_clause)
        if facing_conflict(fact_class, said_class):
            conflicts.append(
                f"看帧判定（{_window_phrase(window)}）说身体「{raw_facing}」，"
                f"拆解首帧行写「{said_clause}」"
                f"——一个背对镜头、一个正对镜头，朝向对不上")

    raw_gaze = str(judgment.get("gaze") or "").strip()
    fact_gaze = gaze_relation(raw_gaze) if raw_gaze != "歧" else ""
    said_gaze_clause = gaze_clause(first_frame)
    said_gaze = gaze_relation(said_gaze_clause)
    if fact_gaze and said_gaze and fact_gaze != said_gaze:
        conflicts.append(
            f"看帧判定（{_window_phrase(window)}）说「{raw_gaze}」，"
            f"拆解首帧行写「{said_gaze_clause}」"
            f"——一个看镜头、一个不看，视线上对不上")
    return conflicts


def check_facts(body: str, cuts: list[float],
                frame_facts: dict | None = None) -> list[str]:
    """把模型写的分镜与机器实测对一遍，冲突如实返回（不自动改写，零兜底）。

    机器能测的事现在有三件：有没有硬切、有几处、0 秒那一帧的人朝哪边看哪边。
    模型答错就记一条冲突，写进产物头部与 stderr，人工看得见；不做静默修正。
    """
    conflicts: list[str] = []
    m = re.search(r"一镜到底\s*[：:]\s*(\S+)", body)
    says_one_take = (m.group(1) if m else "").startswith("是")
    if not cuts and m and not says_one_take:
        conflicts.append("机器实测 0 次硬切，拆解却答「一镜到底：否」")
    if cuts and m and says_one_take:
        conflicts.append(f"机器实测 {len(cuts)} 次硬切，拆解却答「一镜到底：是」")
    if not cuts:
        n_clip = len(re.findall(r"\|\s*剪辑\s*\|", body))
        if n_clip:
            conflicts.append(f"机器实测 0 次硬切，拆解却有 {n_clip} 镜标「剪辑」")
    conflicts.extend(frame_facts_conflicts(first_frame_text(body), frame_facts))
    return conflicts


# ---------------------------------------------------------------- 看帧判定注入


def load_frame_facts(outdir: Path) -> dict | None:
    """读任务目录里的 看帧.json；没有就返回 None（旧流程照跑，一个字都不变）。

    读不懂的文件只提示一句并跳过：拿半份判定去改写拆解，比不注入更糟。
    """
    path = Path(outdir) / FRAME_FACTS_NAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[警告] {FRAME_FACTS_NAME} 读不了，跳过看帧判定注入：{exc}", file=sys.stderr)
        return None
    if not isinstance(data, dict) or not (data.get("帧") or []):
        return None
    return data


INJECT_MODES = ("first", "all", "off")


def _pick_inject(frame_facts: dict | None, inject: str) -> dict | None:
    """挑哪几个窗口写进 prompt。

    2026-11-02 实测：把 5 帧判定全塞进正文，上游内容审核连续 8 次
    `PROHIBITED_CONTENT` 全拦（提示词 3302 字）；同一部片子完全不注入，一次就过
    （2378 字）。判定照跑、留在本地做对账，正文只给首帧那一个窗口——首帧要用的就是它。
    """
    if not frame_facts:
        return None
    mode = (inject or "first").strip().lower()
    if mode in ("off", "no", "none", "关"):
        return None
    if mode == "all":
        return frame_facts
    if frame_facts.get("窗口"):
        windows = [window for window in frame_facts.get("窗口") or []
                   if isinstance(window, dict) and _is_zero_second(window.get("起"))]
        if not windows:
            return None
        module = frame_facts_module()
        frames = module.flatten_frames(windows) if module is not None else []
        return {**frame_facts, "窗口": windows, "帧": frames}
    frames = [f for f in (frame_facts.get("帧") or [])
              if isinstance(f, dict) and _is_zero_second(f.get("秒"))]
    return {**frame_facts, "帧": frames} if frames else None


def _is_zero_second(value) -> bool:
    try:
        return abs(float(value)) < 1e-6
    except (TypeError, ValueError):
        return False


def _tail_window(probe: dict):
    """片尾窗口：末 1.5 秒、2 帧（抽帧秒数照常收到最后一帧能抽的位置）。

    电商宣传片收束普遍落在最后 1–2 秒（2026-11-04 pop1 实测），这一窗的判定
    就是第十层「图文与收束」的锚点来源；载不进 frame_facts.py 时返回 None（旧流程照跑）。
    """
    module = frame_facts_module()
    if module is None:
        return None
    try:
        duration = float(probe.get("duration") or 0.0)
    except (TypeError, ValueError):
        return None
    if duration <= 0:
        return None
    return module.make_window(max(0.0, duration - TAIL_WINDOW_WIDTH), duration,
                              TAIL_WINDOW_N, duration)


def _facts_fields(judgment: dict) -> list[str]:
    """注入行括号里的可核对字段：body / class / gaze / people / shot_size。

    低置信窗口里没共识的字段就是「歧」，照写——不替模型挑一个说法，也不把它藏起来。
    """
    body = judgment.get("body_orientation")
    if isinstance(body, str):
        body = {"facing": body, "back_toward": ""}
    body = body if isinstance(body, dict) else {}
    facing = str(body.get("facing") or "").strip()
    face_class = _judged_class(body, facing)
    back_toward = str(body.get("back_toward") or "").strip()
    fields: list[str] = []
    if facing:
        fields.append("body=" + facing + (f"/背朝{back_toward}" if back_toward else ""))
    if face_class not in ("不确定", ""):
        fields.append("class=" + face_class)
    if judgment.get("gaze"):
        fields.append(f"gaze={judgment['gaze']}")
    if judgment.get("people") not in (None, ""):
        fields.append(f"people={judgment['people']}")
    if judgment.get("shot_size"):
        fields.append(f"shot_size={judgment['shot_size']}")
    return fields


def _seconds_text(value) -> str:
    """秒数写进注入行：0.0 → 「0.0」，0.25 → 「0.25」，认不出就原样。

    不用 `:.1f`：0.25 会被写成 0.2，标注「描述来自哪一秒」就指错了。
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value if value is not None else "")
    text = f"{number:.3f}".rstrip("0")
    return text + "0" if text.endswith(".") else text


def build_frame_facts_block(frame_facts: dict | None, filler: bool = False,
                            fields_only: bool = False) -> str:
    """把每个窗口的聚合判定拼成一段事实注入；没有可用窗口就返回空串。

    `filler=True` 是**实验用**：块还在、长度差不多，但里面不含任何判定内容——
    用来分清上游拦截是「文本变长」触发的，还是「判定内容」触发的。

    每个窗口一行：起止秒 + 那句能直接进提示词的「描述」+ 它来自哪一秒 + 可核对的字段。
    字段只是给写表的人对账用，正句还是描述——描述写不清的窗口不注入，免得把半句话当成画面。

    字段里 `body=` 是自由描述词（「侧背对镜头」这种角度照原话说），`class=` 是粗档。
    聚合低置信的窗口，行内写明「这一窗口三帧里 A／B 不一，以描述为准」：不替模型挑一个，
    也不让它照着一个没定论的角度把整份拆解写歪。
    """
    if filler:
        # 等长替换：先拼出真实块，再把每行「秒：」之后的字全换成占位字，
        # 长度逐字不变——这样才能把「文本变长」和「判定内容」这两个变量分开。
        real = build_frame_facts_block(frame_facts, filler=False)
        if not real:
            return ""
        out_lines = []
        for line in real.splitlines():
            if line.startswith("- ") and "：" in line:
                head, _, tail = line.partition("：")
                out_lines.append(head + "：" + "占" * len(tail))
            else:
                out_lines.append(line)
        return "\n".join(out_lines)
    if fields_only:
        # 只给字段词表，不给自由描述：描述里的词是模型自己生成的，无法保证不踩审核；
        # 字段是有限枚举（正对/侧对/侧背/背对、直视镜头……），可控。描述留在 看帧.json。
        module = frame_facts_module()
        rows: list[str] = []
        for window in (frame_facts or {}).get("窗口") or []:
            if not isinstance(window, dict):
                continue
            aggregated = _aggregate_of(window)
            frames = [f for f in window.get("帧") or [] if isinstance(f, dict)]
            body = aggregated.get("body_orientation") or {}
            cls = body.get("facing_class") or aggregated.get("facing_class") or "歧"
            rows.append(f"- {_seconds_text(window.get('起'))}–{_seconds_text(window.get('止'))} 秒"
                        f"（{len(frames)} 帧）：class={cls}；gaze={aggregated.get('gaze') or '歧'}；"
                        f"people={aggregated.get('people')}；size={aggregated.get('shot_size') or '歧'}")
        for frame in (frame_facts or {}).get("帧") or []:
            if not isinstance(frame, dict) or (frame_facts or {}).get("窗口"):
                continue
            judge = frame.get("判定") or {}
            body = judge.get("body_orientation") or {}
            rows.append(f"- {_seconds_text(frame.get('秒'))} 秒："
                        f"class={body.get('facing_class') or '歧'}；"
                        f"gaze={judge.get('gaze') or '歧'}；people={judge.get('people')}；"
                        f"size={judge.get('shot_size') or '歧'}")
        if not rows:
            return ""
        return ("\n\n## 看帧判定（只有字段）\n"
                "下面每行的字段是对同一窗口多帧取值的机械汇总，**没有描述**；"
                "写首帧时按字段自己组织画面语言。\n" + "\n".join(rows))
    module = frame_facts_module()
    rows: list[str] = []
    windows = [window for window in (frame_facts or {}).get("窗口") or []
               if isinstance(window, dict)]
    if windows:
        for window in windows:
            aggregated = _aggregate_of(window)
            description = str(aggregated.get("描述") or "").strip()
            if not description:
                continue
            frames = [frame for frame in window.get("帧") or [] if isinstance(frame, dict)]
            start = _seconds_text(window.get("起"))
            end = _seconds_text(window.get("止"))
            single = len(frames) <= 1 or end == start
            if single:
                # 老记录 / 单帧窗口：照旧只写秒数（多帧窗口才有「起–止（N 帧）」）
                line = f"- {start} 秒：{description}"
                note = module.vote_note(aggregated) if module is not None else ""
            else:
                line = f"- {start}–{end} 秒（{len(frames)} 帧）：{description}"
                if aggregated.get("描述秒") not in (None, ""):
                    line += f"（这话来自 {_seconds_text(aggregated.get('描述秒'))} 秒那一帧）"
                note = module.window_note(aggregated) if module is not None else ""
            line += note
            fields = _facts_fields(aggregated)
            if fields:
                line += "（字段：" + "；".join(fields) + "）"
            rows.append(line)
    else:
        for frame in (frame_facts or {}).get("帧") or []:
            judgment = frame.get("判定")
            if not isinstance(judgment, dict):
                continue
            description = str(judgment.get("描述") or "").strip()
            if not description:
                continue
            fields = _facts_fields(judgment)
            suffix = "（字段：" + "；".join(fields) + "）" if fields else ""
            note = module.vote_note(judgment) if module is not None else ""
            rows.append(f"- {_seconds_text(frame.get('秒'))} 秒：{description}{note}{suffix}")
    if not rows:
        return ""
    return ("\n\n## 看帧判定（机器取窗口内多帧、每帧单独判定后按字段机械聚合，优先级高于你的观察）\n"
            + "每行开头那句「描述」是已经写好的画面，可以直接落进提示词和首帧；"
            + "括号里的字段只用来和你自己写的表对账，不要照抄成一句话。\n"
            + "\n".join(rows))


def build_tail_brand_block(frame_facts: dict | None, duration: float | None = None) -> str:
    """尾帧判定里的「有没有文字/字样/位置」→ 第十层「图文与收束」的对账锚点。

    仿 build_frame_facts_block 的既有机制：看帧.json 里有尾帧判定（机器逐帧读过片尾、
    且那几帧真的被问过「画面里有什么字」）才注入；没有就一个字不加——旧流程的提示词
    逐字不变。只对末段那几帧负责：全片图文仍由模型按自己看到的逐条登记。
    """
    if not frame_facts:
        return ""
    try:
        total = float(duration) if duration is not None else 0.0
    except (TypeError, ValueError):
        return ""
    if total <= 0:
        return ""
    windows = _facts_windows(frame_facts)
    if not windows:
        return ""
    try:
        last = max(windows, key=lambda w: (float(w.get("止") or 0.0),
                                           float(w.get("起") or 0.0)))
        end = float(last.get("止") or 0.0)
    except (TypeError, ValueError):
        return ""
    if end < total - TAIL_ANCHOR_GATE:
        return ""                       # 最后一个窗口不在片尾 → 它不是尾帧判定，不注入
    aggregated = _aggregate_of(last)
    if not aggregated or not aggregated.get("图文已判"):
        return ""                       # 这份判定没问过「画面里有什么字」→ 一个字不加
    rows = []
    for element in aggregated.get("图文") or []:
        if not isinstance(element, dict):
            continue
        text = str(element.get("文字") or "").strip()
        if not text:
            continue
        bits = [f"「{text}」"]
        for key, tag in (("位置", "位置"), ("形态", "形态"), ("动效", "动效")):
            value = str(element.get(key) or "").strip()
            if value and value != "无":
                bits.append(f"{tag}={value}")
        rows.append(f"- {_seconds_text(element.get('秒'))} 秒那一帧：{'｜'.join(bits)}")
    head = ("## 尾帧判定（机器抽了片尾窗口、逐帧问过「画面里有什么字」，"
            "第十层「图文与收束」照这个对账）")
    if rows:
        return ("\n\n" + head
                + "\n下面这些字是机器从片尾帧里照抄的（含大小写与标点），可以写进第十层的图文条目；"
                "尾帧窗没覆盖的时段仍按你自己看到的登记。\n" + "\n".join(rows))
    return ("\n\n" + head
            + f"\n- {_window_phrase(last)}：机器逐帧看过，没有持久性文字/图形标识"
            "（只对尾帧这几帧负责；全片图文仍按你看到的逐条登记，收束按「无品牌露出」一类照实写）")


# ---------------------------------------------------------------- 看帧判定的自动接线

# 默认只跑首帧窗口（0:0.5、3 帧）＋尾帧窗（末 1.5 秒、2 帧）：一次拆解本来就要调一次模型，
# 多花几次调用换「人物朝向/视线」与「片尾有没有品牌字」这类一眼能定的事实，值得；再多就是
# 另一个决定了，得人自己说（--frames auto|<秒列表>）。
FRAMES_DEFAULT = "first"
FRAMES_OFF = ("off", "no", "none", "关")
# 尾帧窗：收束动作通常落在最后 1–2 秒（pop1 实测），往回铺 1.5 秒、抽 2 帧——
# 2 帧是成本与稳定性的折中（与 auto 模式每窗 2 帧同口径）。
TAIL_WINDOW_WIDTH = 1.5
TAIL_WINDOW_N = 2
# 尾帧判定锚点的门槛：最后一个窗口的止秒离片长超过这个距离，它就不算「尾帧判定」，
# 不进第十层锚点（--frames 0,2.5,6 这类老口径里最后一个窗口可能停在片中部）。
TAIL_ANCHOR_GATE = TAIL_WINDOW_WIDTH + 0.5

_FRAME_FACTS_MODULE = None


def frame_facts_module():
    """按路径导入同目录的 frame_facts.py（那个目录不是包，没有 __init__.py）。

    导不进就返回 None：拆解照跑，只是没有看帧判定——不让一个可选步骤把主流程拖死。
    """
    global _FRAME_FACTS_MODULE
    if _FRAME_FACTS_MODULE is None:
        path = Path(__file__).resolve().parent / "frame_facts.py"
        try:
            spec = importlib.util.spec_from_file_location("_extract_frame_facts", path)
            if spec is None or spec.loader is None:
                raise ImportError(f"导不进 {path}")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except (ImportError, OSError, SyntaxError) as exc:
            print(f"[警告] 载不进 frame_facts.py，本次不做看帧判定：{exc}", file=sys.stderr)
            return None
        _FRAME_FACTS_MODULE = module
    return _FRAME_FACTS_MODULE


_PROGRESS_MODULE = None


def progress_module():
    """按路径导入同目录 progress.py（拆解进度文件归它管）；导不进返回 None。

    进度透出是给人看的（M6-T6F），绝不能拦拆解——失败只留一行警告。
    """
    global _PROGRESS_MODULE
    if _PROGRESS_MODULE is None:
        path = Path(__file__).resolve().parent / "progress.py"
        try:
            spec = importlib.util.spec_from_file_location("_extract_progress", path)
            if spec is None or spec.loader is None:
                raise ImportError(f"导不进 {path}")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except (ImportError, OSError, SyntaxError) as exc:
            print(f"[警告] 载不进 progress.py，本次不写拆解进度：{exc}", file=sys.stderr)
            return None
        _PROGRESS_MODULE = module
    return _PROGRESS_MODULE


def _window_labels(windows, probe: dict) -> list[str]:
    """窗口表 → 日志里给人看的名字（0:0.5 / 3 …）；老口径的裸秒数也认。

    只为打印，任何异常都不该拦住拆解——认不出就退回原始值的字符串。
    """
    module = frame_facts_module()
    if module is not None:
        try:
            return [str(window["窗口"]) for window in
                    module.normalize_windows(windows, probe.get("duration"))]
        except Exception:                       # noqa: BLE001 —— 日志用，不拦流程
            pass
    return [f"{float(value):g} 秒" for value in windows or []
            if isinstance(value, (int, float))]


def frames_of(mode: str, outdir: Path, probe: dict) -> tuple[list | None, str]:
    """`--frames` 的值 → (要保证有判定的窗口表, 人话说明)；None = 这次不自动跑。

    - `off`      不自动跑，回到「有 看帧.json 就注入、没有就跳过」的旧行为；
    - `auto`     按分镜表每镜起点 / 硬切+四分位取点，每个点开一个 0.5 秒窗口、各 2 帧
                 （每镜都要判，2 帧是成本与稳定性的折中）；
    - `0,2.5,6`  就这几秒，每个秒数单帧判一次（老口径，成本最省）；
    - 默认       首帧窗口 0:0.5，3 帧 + **尾帧窗**（末 1.5 秒、2 帧）——尾帧窗给第十层
                 「图文与收束」当判定锚点：电商片收束普遍落在最后 1–2 秒（2026-11-04 pop1
                 实测），拆解却看不见那几帧。同一时刻反复问不如换个时刻再问（2026-11-02 实测）。
    """
    raw = str(mode or "").strip().lower()
    if raw in FRAMES_OFF:
        return None, "off"
    module = frame_facts_module()
    if raw in ("", "first", "默认"):
        if module is None:
            return [0.0], "只抽 0 秒（载不进 frame_facts.py）"
        window = module.make_window(0.0, 0.5, module.DEFAULT_N, probe.get("duration"))
        windows = [window]
        note = f"首帧窗口 {window['窗口']}（{window['n']} 帧）"
        tail = _tail_window(probe)
        if tail is not None and tail["窗口"] != window["窗口"]:
            windows.append(tail)
            note += f"＋尾帧窗 {tail['窗口']}（{tail['n']} 帧，片尾收束判定）"
        return windows, note
    if raw == "auto":
        if module is None:
            return None, "auto（载不进 frame_facts.py）"
        starts = module.choose_times(outdir, probe["duration"], probe.get("hard_cuts") or [])
        windows = [module.make_window(start, start + module.DEFAULT_WINDOW_WIDTH, 2,
                                      probe.get("duration"))
                   for start in starts]
        return windows, f"auto：每个起点一个窗口、各 2 帧，共 {len(windows)} 个"

    seconds: list[float] = [0.0]
    limit = max(0.0, float(probe.get("duration") or 0) - 0.05)
    for piece in raw.replace("，", ",").split(","):
        text = piece.strip()
        if not text:
            continue
        try:
            value = float(text)
        except ValueError as exc:
            raise PrecheckError(
                f"--frames 认不出 {text!r}：写 off / auto，或逗号分隔的秒数（如 0,2.5,6）") from exc
        if value < 0:
            raise PrecheckError(f"--frames 不许是负数：{text!r}")
        # 超出片长的秒数收到最后一帧上：手抖多写一个 60 不该废掉整次（与 choose_times 同口径）
        value = round(min(value, limit), 3)
        if value not in seconds:
            seconds.append(value)
    return seconds, "指定秒数（每个秒数单帧判一次）"


def ensure_frame_facts(video_path: Path, outdir: Path, probe: dict, api_key: str,
                       timeout: float, windows: list | None) -> tuple[dict | None, str]:
    """拆解前先补看帧判定；返回 (要注入的判定, 没成的原因)。

    `windows` 是窗口记录（见 frame_facts.make_window）或裸秒数（老口径：那一刻单帧判一次）。
    原因非空 = 这一份没有完整的看帧判定：stderr 一行、产物头部一行，绝不静默。
    失败时若能读到一份可用的首帧窗口判定（之前就有的），照旧注入——只是要留痕。
    """
    try:
        existing = load_frame_facts(outdir)
    except OSError:                                    # 读盘异常不该拖死拆解
        existing = None
    if windows is None:
        return existing, ""

    module = frame_facts_module()
    if module is None:
        return existing, "载不进 pipeline/reverse/frame_facts.py"

    try:
        facts, summary = module.ensure_facts(
            Path(outdir), Path(video_path), probe, windows,
            lambda text, png: call_model(text, png, api_key, timeout))
    except Exception as exc:                           # noqa: BLE001 —— 可选步骤，绝不拖垮拆解
        facts, summary = existing, {"ok": False, "reason": f"{type(exc).__name__}：{exc}",
                                    "calls": 0, "created": False, "reused": 0, "missing": []}

    if summary.get("ok"):
        if not summary.get("calls"):
            print(f"[看帧] 已有 {summary.get('reused', 0)} 个窗口可用判定，不重复调用（{FRAME_FACTS_NAME}）")
        return facts, ""

    reason = str(summary.get("reason") or "判定没跑成")
    print(f"[警告] 看帧判定没跑成（{reason}）；拆解照跑，只是首帧行没有事实可对",
          file=sys.stderr, flush=True)
    fallback = facts if facts is not None else load_frame_facts(outdir)
    return fallback, reason


def build_document(video_display: str, meta: dict, body: str, conflicts: list[str] | None = None) -> str:
    """拼最终文件：一行元信息 + 规格 + 机器实测 + 模型正文。"""
    tokens = meta.get("total_tokens")
    token_text = str(tokens) if isinstance(tokens, int) else "未知"
    audio_text = "有" if meta.get("has_audio") else "无"
    header = (
        f"<!-- 视频: {video_display} | 时长: {meta.get('duration', 0):.2f}s | 音轨: {audio_text}"
        f" | 耗时: {meta.get('elapsed', 0):.1f}s | token: {token_text} | 模型: {MODEL_LABEL}"
        f" | 尝试: {meta.get('attempts_note', '1')} -->"
    )
    # 规格由 ffprobe 量出来，写在正文前面（页面和提示词都要能看见画幅与时长的真值）
    bits = []
    if meta.get("width") and meta.get("height"):
        bits.append(f"{meta['width']}×{meta['height']}")
    if meta.get("aspect"):
        bits.append(meta["aspect"])
    bits.append(f"{meta.get('duration', 0):.2f} 秒")
    if meta.get("fps"):
        bits.append(f"{meta['fps']} fps")
    bits.append("有音轨" if meta.get("has_audio") else "无音轨")
    cuts = meta.get("hard_cuts") or []
    bits.append(f"硬切 {len(cuts)} 次")
    spec = "> 规格：" + " · ".join(bits)
    out = header + "\n\n" + spec + "\n"
    note = str(meta.get("frame_facts_note") or "").strip()
    if note:
        out += f"> 看帧判定：{note}\n"
    if conflicts:
        out += "\n> ⚠️ 与机器实测冲突（未自动改写，请人工确认）：\n"
        for c in conflicts:
            out += f"> - {c}\n"
    return out + "\n" + body.rstrip() + "\n"


def build_meta(probe: dict, elapsed: float, total_tokens: int) -> dict:
    """探针结果 → 文档元信息。

    2026-11-01 真 bug：这里原来是个内联 dict，漏了 `hard_cuts`，
    于是规格行永远写「硬切 0 次」——测到了却没写进产物，比不测更糟。
    抽成函数就是为了让「探针 → 产物」这段接线可被测试盯住。
    """
    return {
        "duration": probe["duration"],
        "has_audio": probe["has_audio"],
        "width": probe.get("width", 0),
        "height": probe.get("height", 0),
        "fps": probe.get("fps", 0),
        "aspect": probe.get("aspect", ""),
        "elapsed": elapsed,
        "total_tokens": total_tokens,
        "attempts_note": "1",
        "hard_cuts": probe.get("hard_cuts") or [],
    }


def _atomic_write(target: Path, text: str) -> int:
    """先写同目录临时文件，成功后 rename；失败不留半截文件。返回字节数。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.with_name(f".{target.name}.tmp{os.getpid()}")
    data = text.encode("utf-8")
    try:
        with open(tmp_path, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
    except OSError:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return len(data)


def _display_path(path: Path) -> str:
    """仓库内路径显示成相对路径，方便对账。"""
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except (ValueError, OSError):
        return str(path)


def _usage_tokens(usage: dict) -> int:
    total = usage.get("totalTokenCount")
    if isinstance(total, int):
        return total
    prompt = usage.get("promptTokenCount") or 0
    output = usage.get("candidatesTokenCount") or 0
    return int(prompt) + int(output)


# ---------------------------------------------------------------- 子命令：只校验

def run_check_only(check_path: Path) -> int:
    """不调模型，只校验既有 拆解.md 的十层。"""
    if not check_path.is_file():
        print(f"ERROR 找不到文件：{_display_path(check_path)}", file=sys.stderr)
        return 2
    try:
        markdown = check_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"ERROR 读不到文件：{exc}", file=sys.stderr)
        return 2

    bodies, missing = split_layers(markdown)
    print(f"[校验] {_display_path(check_path)}，共 {len(markdown)} 字，识别到 {len(bodies)} 个小标题")
    if missing:
        print(format_missing(missing), file=sys.stderr)
        return 4

    for title in LAYER_TITLES:
        body_len = len(bodies[normalize_heading(title)])
        print(f"  ✓ {title}：{body_len} 字")
    print("十层齐全且非空")
    return 0


# ---------------------------------------------------------------- 主流程

def run_extract(video_path: Path, video_id: str, outdir: Path, timeout: float, intent: str = "",
                frames: str = FRAMES_DEFAULT, inject: str = "fields",
                model: str | None = None, base_url: str | None = None,
                api_key: str | None = None, diagnosis: str = "") -> int:
    # 1. 预检（含 --frames 的解析：认不出就当场退出，不去花错的钱）
    try:
        probe = probe_video(video_path)
        base_prompt = read_prompt()
        windows, frames_note = frames_of(frames, Path(outdir), probe)
        api_key = (api_key or "").strip() or load_api_key()
        if not api_key:
            raise PrecheckError(f"缺密钥：设置环境变量 {KEY_ENV} 或写进 HERMES_HOME/.env")
    except (PrecheckError, OSError) as exc:
        print(f"ERROR 预检失败：{exc}", file=sys.stderr)
        return 2

    # 渠道透传（M6-T7-C）：给了 --model/--base-url 才换端点与模型标签（主调用与看帧
    # 判定同走 call_model，一并生效）；都不给 → 模块常量原样不动，行为与旧版逐字节一致。
    global ENDPOINT, MODEL_LABEL
    if (model or "").strip() or (base_url or "").strip():
        ENDPOINT = resolve_endpoint(model, base_url)
        MODEL_LABEL = (model or "").strip() or MODEL_LABEL
        print(f"[渠道] 拆解模型 {MODEL_LABEL}｜端点 {ENDPOINT}", flush=True)

    display = _display_path(video_path)
    audio_text = "有" if probe["has_audio"] else "无"
    print(f"[预检] {display}")
    print(f"[预检] 时长 {probe['duration']:.2f}s，视频流 有，音轨 {audio_text}，流 {probe['codec_types']}")
    print(f"[看帧] 取帧规则：{frames_note}"
          + (f"｜已保证 {'、'.join(_window_labels(windows, probe))} 有判定"
             if windows else "｜本次不自动跑"))

    # 1.5 看帧判定：缺就先自动跑一次（默认首帧窗口 0:0.5、3 帧）；失败只留痕，不拦拆解
    frame_facts, frame_problem = ensure_frame_facts(
        Path(video_path), Path(outdir), probe, api_key, timeout, windows)
    prompt = build_prompt(base_prompt, intent, probe, frame_facts, inject, diagnosis=diagnosis)
    try:
        write_intent(Path(outdir), intent)
    except OSError as exc:
        print(f"ERROR 预检失败：写不了意图：{exc}", file=sys.stderr)
        return 2

    print(f"[提示词] {_display_path(PROMPT_PATH)} 围栏正文 {len(prompt)} 字（单一真源，未复制）")
    if frame_facts:
        injected = _pick_inject(frame_facts, inject) or {}
        block_preview = build_frame_facts_block(injected, filler=(inject == "filler"))
        rows = [ln for ln in block_preview.splitlines() if ln.startswith("- ")]
        print(f"[看帧] 注入 {len(rows)} 行判定（{FRAME_FACTS_NAME}，--inject {inject}；"
              f"判定共 {len(frame_facts.get('窗口') or [])} 个窗口 / "
              f"{len(frame_facts.get('帧') or [])} 帧）")
    if frame_problem:
        print(f"[警告] 本份拆解没有完整的看帧判定（{frame_problem}）；已写进产物头部",
              file=sys.stderr, flush=True)
    if not probe["has_audio"]:
        print("[警告] 没有音轨，「八 声音」可能为空，后续会按缺层失败", file=sys.stderr)

    # 2. 调模型（直连 + 重试）
    #    2026-11-02 实测：把看帧判定写进正文，同一部片子有时被上游内容审核拦下来
    #    （PROHIBITED_CONTENT，同一份请求换个时间又能过）。可选的事实块不该拖死主流程，
    #    所以被拦时先去掉注入重试一次，并留下痕迹——判定本身还在 看帧.json 里。
    #    大拆解是拆解里唯一的长阶段（10–20 分钟），进主调用前单独标出来（M6-T6F），
    #    轮询端才知道任务在正常跑；写不了进度不影响拆解。
    _progress = progress_module()
    if _progress is not None:
        _progress.write_progress(outdir, "大拆解·准备中")
    inject_problem = ""
    clear_api_error(outdir)                # 上一轮的失败证据不带进这一轮（成功就不该有）
    try:
        if _progress is not None:
            _progress.write_progress(outdir, "大拆解·已提交远程模型，等待返回")
        text, usage, elapsed = call_model(prompt, video_path, api_key, timeout)
        if _progress is not None:
            _progress.write_progress(outdir, "大拆解·模型已返回，校验与落盘中")
    except ApiError as exc:
        if frame_facts and _is_content_block(exc):
            print("[看帧] 上游内容审核拦下了带判定的请求，去掉注入重试一次"
                  "（判定仍留在 看帧.json，可事后核对）", file=sys.stderr, flush=True)
            prompt = build_prompt(base_prompt, intent, probe, frame_facts, "off",
                                  diagnosis=diagnosis)
            inject_problem = "带看帧判定的请求被上游内容审核拦下，本次未注入"
            try:
                if _progress is not None:
                    _progress.write_progress(outdir, "大拆解·已提交远程模型（去判定重试），等待返回")
                text, usage, elapsed = call_model(prompt, video_path, api_key, timeout)
                if _progress is not None:
                    _progress.write_progress(outdir, "大拆解·模型已返回，校验与落盘中")
            except ApiError as exc2:
                write_api_error(outdir, exc2, api_key)
                print(f"ERROR API 失败：{redact(str(exc2), api_key)}", file=sys.stderr)
                return 3
        else:
            write_api_error(outdir, exc, api_key)
            print(f"ERROR API 失败：{redact(str(exc), api_key)}", file=sys.stderr)
            return 3

    # 3. 缺层校验（不过就不落盘，避免下游吃半份产物）
    #    无音轨片先按「省字段不省层」改写八 声音正文（只留「无音轨」），
    #    十层判定逻辑一个字不动：真缺标题的照样判缺层。
    text, sound_action = ensure_sound_layer(text, probe.get("has_audio"))
    if sound_action == "filled":
        print(f"[八 声音] 无音轨片：模型没写本层正文，按「省字段不省层」补「{NO_AUDIO_TEXT}」"
              "（十层标题不动）", flush=True)
    elif sound_action == "replaced":
        print(f"[八 声音] 无音轨片：模型写了声音内容，但这条片子没有音轨（ffprobe 实测），"
              f"正文按规则改写为「{NO_AUDIO_TEXT}」（不采信编出来的 BGM/台词，十层标题不动）",
              flush=True)
    try:
        require_layers(text)
    except LayerError as exc:
        print(f"ERROR 模型输出不完整：{format_missing(exc.missing)}", file=sys.stderr)
        print("ERROR 未写盘（缺层产物不进 analysis/）", file=sys.stderr)
        return 4

    # 3.4 八 声音结构化回显：正文原样来自这一次模型调用（同一次带音轨多模态调用产出），
    #     这里只读回来核对落盘结果，不改写一个字；缺字段只告警，判不合格是门禁 D8 的事。
    sound = parse_sound_layer(sound_body(text))
    if probe.get("has_audio") is False:
        print(f"[八 声音] 无音轨片：落盘正文「{sound_body(text).strip()}」"
              "，BGM/音效/台词字段按规则省略", flush=True)
    else:
        speakers = sum(1 for row in sound["dialogue"] if row["speaker"])
        bgm_note = "四字段齐" if not sound["bgm_missing"] else "缺 " + "、".join(sound["bgm_missing"])
        print(f"[八 声音] 结构化落盘：BGM {bgm_note}｜音效点 {len(sound['sound_points'])} 条"
              f"｜台词 {len(sound['dialogue'])} 句（带说话人 {speakers} 句）", flush=True)
        if sound["bgm_missing"]:
            print(f"[警告] 八 声音缺 BGM 结构化字段：{'、'.join(sound['bgm_missing'])}"
                  "（拆解产物门禁 D8 会判不合格）", file=sys.stderr, flush=True)
        if sound["dialogue_missing_speaker"]:
            print(f"[警告] 八 声音有 {len(sound['dialogue_missing_speaker'])} 句台词没写说话人"
                  "（只许 画外音 / 出镜者；门禁 D8 会判不合格）", file=sys.stderr, flush=True)

    # 3.5 与机器实测对账：硬切次数是 ffmpeg 量出来的、0 秒那一帧的朝向是单帧判定读出来的，
    #     模型答错要留痕
    conflicts = check_facts(text, probe.get("hard_cuts") or [], frame_facts)

    # 4. 落盘
    total_tokens = _usage_tokens(usage)
    meta = build_meta(probe, elapsed, total_tokens)
    if frame_problem:
        meta["frame_facts_note"] = (f"只补到一部分（原因：{frame_problem}）" if frame_facts
                                    else f"本份拆解没有看帧判定（原因：{frame_problem}）")
    if inject_problem and frame_facts:
        frames = len(frame_facts.get("帧") or [])
        meta["frame_facts_note"] = (f"齐全（{frames} 帧都在 看帧.json，可事后核对），"
                                    f"但{inject_problem}")
    document = build_document(display, meta, text, conflicts)
    if conflicts:
        print("[警告] 与机器实测冲突（已写进产物头部，未自动改写）：", file=sys.stderr)
        for c in conflicts:
            print(f"  - {c}", file=sys.stderr)
    target = outdir / OUTPUT_NAME
    try:
        byte_count = _atomic_write(target, document)
    except OSError as exc:
        print(f"ERROR 写盘失败：{exc}", file=sys.stderr)
        return 2

    print(f"[写出] {_display_path(target)}（{byte_count} 字节）")
    print(f"[完成] 十层齐全，token={total_tokens}，耗时 {elapsed:.1f}s")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="S1 拆解：视频 → 十层 Markdown")
    parser.add_argument("video", nargs="?", help="视频路径")
    parser.add_argument("--id", dest="video_id", help="产物 id，默认由文件名推出")
    parser.add_argument("--outdir", help="产物目录，默认 analysis/reverse/<id>")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="单次请求超时秒数（默认 300）")
    parser.add_argument("--intent", default="", help="本次用户意图（可选）")
    parser.add_argument("--frames", default=FRAMES_DEFAULT, metavar="auto|off|<秒列表>",
                        help="看帧判定取哪些窗口：默认首帧窗口 0:0.5、3 帧 ＋ 尾帧窗（末 1.5 秒、2 帧，"
                             "给第十层「图文与收束」当判定锚点）；"
                             "auto = 每镜起点一个 0.5 秒窗口、各 2 帧；"
                             "off = 不自动跑（有 看帧.json 就注入，没有就跳过）；"
                             "0,2.5,6 = 指定秒数，各单帧判一次（老口径）")
    parser.add_argument("--check-only", metavar="拆解.md", help="只校验既有产物，不调模型")
    parser.add_argument("--inject", default="fields", metavar="fields|first|all|off|filler",
                        help="看帧判定写进 prompt 的幅度：first = 只给首帧那一窗口（默认，"
                             "首帧要用的就是它，也避开上游审核；实测 5 帧全塞会被 "
                             "PROHIBITED_CONTENT 连拦）；all = 每个窗口都写；off = 只算不写")
    parser.add_argument("--model", default=None,
                        help="拆解用多模态模型名（M6-T7-C；默认内置默认模型，行为不变）")
    parser.add_argument("--base-url", default=None,
                        help="渠道 base URL：不带版本段自动补 /v1beta，显式带则尊重")
    parser.add_argument("--api-key", default=None,
                        help="渠道密钥（默认读 JXIN_API_KEY / .env；不给就落回原读法）")
    parser.add_argument("--diagnosis", default=None,
                        help="门禁诊断文件（自动重拆用）：正文以非标题形态追加到任务书末尾；"
                             "不给 = 与旧版逐字节一致")
    args = parser.parse_args(argv)

    if args.check_only:
        return run_check_only(Path(args.check_only).expanduser())

    if not args.video:
        parser.error("缺少视频路径（或改用 --check-only <拆解.md>）")

    diagnosis = ""
    if args.diagnosis:
        try:
            diagnosis = Path(args.diagnosis).expanduser().read_text(encoding="utf-8").strip()
        except OSError as exc:
            print(f"ERROR 读不到门禁诊断 {args.diagnosis}：{exc}", file=sys.stderr)
            return 2

    video_path = Path(args.video).expanduser()
    video_id = args.video_id or make_video_id(video_path)
    outdir = Path(args.outdir).expanduser() if args.outdir else REPO_ROOT / "analysis" / "reverse" / video_id
    return run_extract(video_path, video_id, outdir, args.timeout, args.intent, args.frames, args.inject,
                       model=args.model, base_url=args.base_url, api_key=args.api_key,
                       diagnosis=diagnosis)


if __name__ == "__main__":
    raise SystemExit(main())
