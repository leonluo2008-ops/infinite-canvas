#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""识别一张资产图 → 一行短名（给提示词里的 @图片N 用）。

为什么要有它：所有者要的是「传图就能自动命名」，不是自己盯着一张白底图想名字。
名字必须短、必须是「颜色 + 款式」，才能在提示词里当指代词用。

用法
----
    py -3 pipeline/reverse/asset_name.py <图片路径> [--kind 主体|人物|场景|道具]
    # stdout 只有一行命名，例：粉色款 / 年轻女性深色卷发 / 灰蓝绒面地面

接口纪律（照抄仓库既有做法，不自己发明）
----------------------------------------
- 端点 `https://api.jxinapi.com/v1beta/models/gemini-3.8-flash:generateContent`（Gemini 原生格式）
- 密钥 `JXIN_API_KEY`：env 优先，其次 `$HERMES_HOME/.env`，再退 Windows 默认位置；**永不打印值**
- **必须直连**：`ProxyHandler({})`。走系统代理会 SSL EOF 或返回空正文
- 正文取 `candidates[0].content.parts[*].text` 拼接；**HTTP 200 但正文为空算失败**，
  退避 5/10/20s 重试 3 次，仍空 exit=3

退出码
------
0 成功｜2 图不存在 / 格式不认 / 太大｜3 API 失败（含空正文）
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ENDPOINT = "https://api.jxinapi.com/v1beta/models/gemini-3.8-flash:generateContent"
KEY_ENV = "JXIN_API_KEY"
MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}
MAX_BYTES = 18 * 1024 * 1024          # inline_data 请求体上限内留余量
RETRY_DELAYS = (5, 10, 20)            # 首次失败后的退避；总共 1 + 3 次
TIMEOUT = 120

EXIT_OK = 0
EXIT_INPUT = 2
EXIT_API = 3

KINDS = ("主体", "服装", "人物", "场景", "道具")

RULE_TAIL = (
    "严格只输出下面两行，不要解释、不要引号、不要项目符号：\n"
    "命名：<短名>\n"
    "描述：<一整句可见外形描述>\n"
    "命名行只写短名；描述行写形态、颜色、材质、款式、标签和结构等看得见的细节，"
    "不要猜身份、用途或看不清的文字。"
)

PROMPTS = {
    "主体": (
        "这是电商素材图里的产品图。给图里的产品起一个短名，用来在视频提示词里指代它。\n"
        "命名规则：\n"
        "1. 结构 = 颜色 + 款式（例：粉色款、磨砂玻璃泵头款、透明细长圆柱款）。\n"
        "2. 颜色取图里真实看到的颜色；说不出确切色名就用「浅/深 + 色系」，不要编。\n"
        "3. 长度 2–8 个汉字。\n"
        "4. 图上有清晰可读的标签文字（品名/色号/型号）时，只取最像品名的 6–10 个字符附在名字后面，"
        "中间空一格；不要整句照抄。\n"
        + RULE_TAIL
    ),
    "人物": (
        "这是人物参考图。给图里的人起一个短名，用来在视频提示词里指代他/她。\n"
        "命名规则：\n"
        "1. 结构 = 年龄段 + 性别 + 发型或服装（例：年轻女性深色卷发、中年男性灰色夹克）。\n"
        "2. 只写看得见的外形特征，不猜身份、职业、情绪。\n"
        "3. 长度 4–12 个汉字。\n"
        + RULE_TAIL
    ),
    "服装": (
        "这是服装参考图。给这套衣服起一个短名，用来在视频提示词里指代它。\n"
        "命名规则：\n"
        "1. 结构 = 颜色 + 款式（例：黑色运动背心、铁锈橙无袖连衣裙）。\n"
        "2. 只写看得见的颜色、版型、领口袖长，不写品牌、不猜材质成分。\n"
        "3. 长度 3–12 个汉字。\n"
        + RULE_TAIL
    ),
    "场景": (
        "这是场景/背景参考图。给这个场景起一个短名，用来在视频提示词里指代它。\n"
        "命名规则：\n"
        "1. 结构 = 材质或环境 + 光线（例：灰蓝绒面地面、左侧硬光白墙）。\n"
        "2. 只写看得见的材质、颜色、光的方向与软硬，不写氛围词。\n"
        "3. 长度 4–12 个汉字。\n"
        + RULE_TAIL
    ),
    "道具": (
        "这是道具参考图。给图里的道具起一个短名，用来在视频提示词里指代它。\n"
        "命名规则：\n"
        "1. 结构 = 颜色 + 物件类别（例：棕色长条构件、银色圆盘）。\n"
        "2. 模型认不出它是什么也照实写形状和材质（例：棕色长条构件），不要猜用途。\n"
        "3. 长度 2–8 个汉字。\n"
        + RULE_TAIL
    ),
}


class Fail(Exception):
    def __init__(self, code: int, msg: str) -> None:
        super().__init__(msg)
        self.code = code
        self.msg = msg


# ---------------------------------------------------------------- 密钥 / 请求


def load_api_key() -> str:
    key = os.environ.get(KEY_ENV, "").strip()
    if key:
        return key
    configured_home = os.environ.get("HERMES_HOME")
    if configured_home:
        env_path = Path(configured_home) / ".env"
    elif os.name == "nt":
        env_path = Path.home() / "AppData" / "Local" / "hermes" / ".env"
    else:
        env_path = Path.home() / ".hermes" / ".env"
    try:
        lines = env_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(f"{KEY_ENV}="):
            return stripped.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def build_opener() -> urllib.request.OpenerDirector:
    """直连：空 ProxyHandler 关掉系统代理；显式 HTTPSHandler 带默认证书链。"""
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
    )


def call_once(opener: urllib.request.OpenerDirector, key: str, mime: str,
              b64: str, prompt: str) -> str:
    body = {
        "contents": [{
            "parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": mime, "data": b64}},
            ]
        }]
    }
    req = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    with opener.open(req, timeout=TIMEOUT) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        raise Fail(EXIT_API, f"响应不是 JSON：{raw[:160]}") from None

    cands = payload.get("candidates") or []
    if not cands:
        # 有些网关用 promptFeedback.blockReason 说明为什么没候选
        reason = (payload.get("promptFeedback") or {}).get("blockReason", "")
        raise Fail(EXIT_API, f"candidates 为空{f'（{reason}）' if reason else ''}")
    parts = (cands[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
    return text


# ---------------------------------------------------------------- 命名清洗


QUOTE_CHARS = "「」『』“”‘’\"'`*·"


def clean_name(text: str) -> str:
    """模型爱加解释和多行；这里只留名字那一行。"""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return ""
    name = lines[0]
    name = re.sub(r"^(?:命名|短名|名称|答案|Name|name)\s*[:：]\s*", "", name)
    name = re.sub(r"^[-*•\d.、)\s]+", "", name)
    name = name.strip().strip(QUOTE_CHARS).strip()
    name = re.sub(r"[。．.，,、；;！!？?]+$", "", name)
    name = re.sub(r"\s+", " ", name)
    if len(name) > 20:                       # 再长就不是「短名」了
        head = name[:20]
        if " " in head[8:]:                  # 尽量切在词边界，别把英文词劈成两半
            head = head[:head.rfind(" ")]
        name = head.rstrip(" -·|：:")
    return name


# ---------------------------------------------------------------- 主流程


def split_name_desc(text: str) -> tuple[str, str]:
    """解析模型的双行输出；旧模型只返回一行时描述为空。"""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    name_line = ""
    desc_line = ""
    for line in lines:
        m = re.match(r"^(?:命名|短名|名称|答案|Name|name)\s*[:：]\s*(.*)$", line)
        if m and not name_line:
            name_line = m.group(1)
            continue
        m = re.match(r"^(?:描述|说明|外观描述|Description|description)\s*[:：]\s*(.*)$", line)
        if m and not desc_line:
            desc_line = m.group(1).strip()
            continue
    if not name_line:
        name_line = lines[0] if lines else ""
        if len(lines) > 1:
            desc_line = lines[1]
    name = clean_name(name_line)
    # 描述是用户资产的事实字段：只去掉模型误加的字段前缀/引号，不做 sanitize 或截断。
    desc = desc_line.strip().strip(QUOTE_CHARS).strip()
    desc = re.sub(r"^(?:描述|说明|Description|description)\s*[:：]\s*", "", desc).strip()
    return name, desc


def identify(image: Path, kind: str = "主体") -> tuple[str, str]:
    """看图 → (短名, 描述)。失败抛 Fail。"""
    if kind not in PROMPTS:
        raise Fail(EXIT_INPUT, f"--kind 只认 {'/'.join(KINDS)}，收到「{kind}」")
    if not image.is_file():
        raise Fail(EXIT_INPUT, f"图不存在：{image}")
    mime = MIME_BY_SUFFIX.get(image.suffix.lower())
    if not mime:
        raise Fail(EXIT_INPUT,
                   f"格式不认：{image.suffix or '(无扩展名)'}；只认 {', '.join(sorted(MIME_BY_SUFFIX))}")
    size = image.stat().st_size
    if size == 0:
        raise Fail(EXIT_INPUT, "图是空文件（0 字节）")
    if size > MAX_BYTES:
        raise Fail(EXIT_INPUT,
                   f"图太大：{size / 1048576:.1f}MB > {MAX_BYTES // 1048576}MB")

    key = load_api_key()
    if not key:
        raise Fail(EXIT_API, f"没有 {KEY_ENV}（env 或 $HERMES_HOME/.env）")

    b64 = base64.b64encode(image.read_bytes()).decode("ascii")
    opener = build_opener()
    prompt = PROMPTS[kind]
    delays = list(RETRY_DELAYS)
    last = ""
    attempt = 0
    while True:
        attempt += 1
        try:
            text = call_once(opener, key, mime, b64, prompt)
            name, desc = split_name_desc(text)
            if name:
                if attempt > 1:
                    print(f"[asset_name] 第 {attempt} 次成功", file=sys.stderr)
                return name, desc
            last = "正文为空或没有命名"
        except Fail as err:
            last = err.msg
        except urllib.error.HTTPError as err:
            body = ""
            try:
                body = err.read().decode("utf-8", errors="replace")[:200]
            except Exception:                       # noqa: BLE001 - 读错误体失败不影响判定
                body = ""
            last = f"HTTP {err.code} {body}".strip()
        except Exception as err:                    # noqa: BLE001 - 网络层失败一律可重试
            last = f"{type(err).__name__}: {err}"

        if not delays:
            raise Fail(EXIT_API, f"API 连续 {attempt} 次失败，最后一次：{last}")
        wait = delays.pop(0)
        print(f"[asset_name] 第 {attempt} 次失败（{last}），{wait}s 后重试",
              file=sys.stderr)
        time.sleep(wait)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="看一眼资产图，输出一行短名")
    ap.add_argument("image", help="图片路径（png/jpg/jpeg/webp）")
    ap.add_argument("--kind", default="主体", choices=list(KINDS),
                    help="实体类别（决定命名口径，默认 主体）")
    args = ap.parse_args(argv)

    started = time.time()
    try:
        name, desc = identify(Path(args.image), args.kind)
    except Fail as err:
        print(f"[FAIL exit={err.code}] {err.msg}", file=sys.stderr)
        return err.code
    elapsed = time.time() - started
    # 第一行仍是原来的短名，旧脚本按第一行读取时不受影响；第二行是新增描述。
    print(name)
    print(f"描述：{desc}")
    print(f"[asset_name] {args.kind} · {elapsed:.1f}s · {Path(args.image).name}",
          file=sys.stderr)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
