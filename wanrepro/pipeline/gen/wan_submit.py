"""万相提交节点 —— 全项目**唯一**允许拼 `wan omni2video` 命令的地方。

契约：`docs/规范-万相提交-20261102.md`。改这里之前先改那份规范。

设计约束（血泪）：
- Windows 上必须直连 `node <pkg>/dist/index.js`，不能走 `wan.cmd`——cmd.exe 会在提示词
  第一个换行处切断整条命令，后面参数全丢且照样返回成功。
- `@图片N` 按元素标题解析；写错形式（`@图片 1`、`@图1`、漏 @）CLI 一律不认且**不报错**。
- 传给 `--images` 的第 k 个文件就是 `图片k`；平台会把**全部**传入图片发出去，不是只发被引用的。
- 没有新增成片 = 失败。绝不把目录里上一轮的旧片当成本次结果。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

# ---- 平台硬约束 --------------------------------------------------------------
MAX_IMAGES = 10
MAX_PROMPT_CHARS = 20000          # CLI 超限直接 exit 1，不自动截断
MIN_DURATION = 2
MAX_DURATION = 30
MAX_IMAGE_BYTES = 20 * 1024 * 1024
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}   # CLI help 只列 JPEG/PNG/WebP（无 BMP）
RESOLUTIONS = ("480P", "720P", "1080P")
RATIOS = ("adaptive", "16:9", "4:3", "1:1", "3:4", "9:16")
ONLY_MODEL = "wan3.0"             # CLI 拒绝其它值：omni2video only supports --model wan3.0

IMG_REF = re.compile(r"@图片(\d+)")
UUID_REF = re.compile(r"@\{([0-9a-f\-]+)\}")

# 写错形式的引用：CLI 不认，模型收到死文本
REF_LINT = (
    (re.compile(r"@图片\s+\d"), "@图片 与数字之间不能有空格"),
    (re.compile(r"@图\d"), "@图N 应写成 @图片N"),
    (re.compile(r"(?<![@\w])图片\s?\d+"), "图片N 前面少了 @"),
)

CLI_ENTRY_MSG = ("找不到 wan CLI 的 node 入口（重装 @wan-ai/cli；"
                 "不回退 wan.cmd——cmd.exe 会在提示词的第一个换行处切断整条命令）")


class CliEntryError(RuntimeError):
    """CLI 入口定位失败。"""


class SubmitError(ValueError):
    """提交参数不合法；调用方应当直接把 message 给人看。"""


# ---- CLI 入口与执行 ----------------------------------------------------------

def cmd_js_entry(cmd: Path) -> Path | None:
    """从 npm 生成的 `wan.cmd` 里读出真实 js 入口；读不到返回 None。"""
    try:
        text = cmd.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = re.search(r"%dp0%[\\/]+(?P<rel>[^\"%\r\n]+?\.js)", text, re.IGNORECASE)
    if not m:
        return None
    cand = (cmd.parent / m.group("rel").replace("\\", "/")).resolve()
    return cand if cand.is_file() else None


def cli_prefix() -> list[str]:
    """调 CLI 的前缀：非 Windows 直接用 wan；Windows 直连 node 入口。"""
    exe = shutil.which("wan")
    if os.name != "nt":
        return [exe or "wan"]

    node = shutil.which("node")
    if not node:
        raise CliEntryError(CLI_ENTRY_MSG + "：PATH 里没有 node")

    entry: Path | None = None
    roots: list[Path] = []
    for found in (exe, shutil.which("wan.cmd")):
        if not found:
            continue
        shim = Path(found)
        roots.append(shim.parent)
        shim = shim if shim.suffix.lower() == ".cmd" else shim.with_name(shim.name + ".cmd")
        if shim.is_file():
            entry = cmd_js_entry(shim)
            if entry:
                break
    if entry is None:
        for root in dict.fromkeys(roots):
            js = root / "node_modules" / "@wan-ai" / "cli" / "dist" / "index.js"
            if js.is_file():
                entry = js
                break
    if entry is None:
        raise CliEntryError(CLI_ENTRY_MSG)
    return [node, str(entry)]


def run_cli(args: list[str], timeout: int = 300) -> tuple[int, str, str]:
    """跑 wan CLI；参数用列表传，提示词里的换行不会被 shell 吃掉。"""
    try:
        p = subprocess.run([*cli_prefix(), *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except FileNotFoundError:
        raise CliEntryError(CLI_ENTRY_MSG) from None
    except subprocess.TimeoutExpired as err:
        out = (err.stdout or "") if isinstance(err.stdout, str) else ""
        e = (err.stderr or "") if isinstance(err.stderr, str) else ""
        return 124, out, e + f"\n超时（{timeout}s）"
    return p.returncode, p.stdout, p.stderr


# ---- 请求对象 ----------------------------------------------------------------

@dataclass(frozen=True)
class Request:
    """一次提交的完整输入。构造后不可变，便于记录与复核。"""

    prompt: str
    images: tuple[Path, ...] = ()
    duration: int = 5
    ratio: str = "9:16"
    resolution: str = "1080P"
    audio: bool = True
    model: str = ONLY_MODEL

    def token(self) -> str:
        """核对令牌：正文 + 参数 + 图片（名字 + 大小 + 内容前缀）。

        只算文件名挡不住「同名换图」，所以带内容指纹。
        """
        parts = [self.prompt, str(self.duration), self.ratio, self.resolution,
                 "audio=1" if self.audio else "audio=0",
                 "model=" + str(self.model or ""),
                 *[f"{p.name}:{img_sig(p)}" for p in self.images]]
        return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]


def img_sig(p: Path) -> str:
    """大小 + 前 256KB 的 sha256：够挡住换图，又不用整份读完。"""
    try:
        size = p.stat().st_size
        with p.open("rb") as f:
            head = f.read(262144)
    except OSError:
        return "missing"
    return f"{size:x}:{hashlib.sha256(head).hexdigest()[:12]}"


def wanted_refs(prompt: str) -> list[int]:
    """正文里出现的 @图片N（升序去重）。"""
    return sorted({int(m.group(1)) for m in IMG_REF.finditer(prompt)})


def lint_refs(prompt: str) -> list[str]:
    """抓「看起来像引用、但 CLI 不认」的写法；CLI 对这些一律静默失效。

    「本次参考图：图片1=香水、…」清单行是合法格式（号=images 下标，不带 @），
    只查正文引用，跳过清单行——否则误伤合法提交。
    """
    bad: list[str] = []
    body = "\n".join(line for line in prompt.splitlines() if not line.lstrip().startswith(("参考图：", "本次参考图：")))
    for pattern, why in REF_LINT:
        hits = sorted({m.group(0) for m in pattern.finditer(body)})
        if hits:
            bad.append(f"引用写法不对（{'、'.join(hits[:3])}）：{why}")
    for n in wanted_refs(prompt):
        if n < 1:
            bad.append(f"@图片{n} 无效：图号从 1 开始")
    return bad


def expand_prefix(prompt: str, images: list[Path]) -> tuple[list[Path], str]:
    """把「只发被引用的图」补成前缀 1..max(N)。

    为什么不能只发被引用的：CLI 按 `--images` 顺序把第 k 张命名成 `图片k`，
    只发第 2 张会被命名成 `图片1`，正文里的 `@图片2` 就成了死引用（实测：不报错）。
    """
    want = [n for n in wanted_refs(prompt) if n >= 1]
    if not want:
        return [], "纯文生视频（正文没有 @图片N）"
    if want[-1] > len(images):
        raise SubmitError(f"正文引用了 @图片{want[-1]}，但只有 {len(images)} 张有图资产"
                          f"（{'、'.join(p.name for p in images) or '无'}）——先补图")
    return list(images[: want[-1]]), f"按前缀发送 1..{want[-1]}"


def validate(req: Request) -> list[str]:
    """提交前静态校验；返回问题清单，空 = 通过。"""
    bad: list[str] = []
    if not req.prompt.strip():
        bad.append("正文为空")
    if len(req.prompt) > MAX_PROMPT_CHARS:
        bad.append(f"正文 {len(req.prompt)} 字，超过平台上限 {MAX_PROMPT_CHARS}（CLI 直接拒绝，不截断）")
    bad.extend(lint_refs(req.prompt))
    if not (MIN_DURATION <= req.duration <= MAX_DURATION):
        bad.append(f"时长 {req.duration} 秒越界（{MIN_DURATION}–{MAX_DURATION}）")
    if req.ratio not in RATIOS:
        bad.append(f"画幅 {req.ratio} 不支持（{'/'.join(RATIOS)}）")
    if req.resolution not in RESOLUTIONS:
        bad.append(f"分辨率 {req.resolution} 不支持（{'/'.join(RESOLUTIONS)}）")
    if req.model not in ("", None, ONLY_MODEL):
        bad.append(f"模型 {req.model} 不支持：omni2video 只认 {ONLY_MODEL}")
    if len(req.images) > MAX_IMAGES:
        bad.append(f"参考图 {len(req.images)} 张，超过上限 {MAX_IMAGES}")
    for p in req.images:
        if "," in str(p):
            bad.append(f"图片路径含逗号，会被 --images 切坏：{p}")
        if not p.is_file():
            bad.append(f"参考图不存在：{p}")
            continue
        size = p.stat().st_size
        if size == 0:
            bad.append(f"参考图是 0 字节：{p.name}")
        elif size > MAX_IMAGE_BYTES:
            bad.append(f"参考图超过 20MiB：{p.name}")
        if p.suffix.lower() not in IMAGE_SUFFIXES:
            bad.append(f"参考图格式不支持：{p.name}（{'/'.join(sorted(IMAGE_SUFFIXES))}）")
    want = wanted_refs(req.prompt)
    if want and want[-1] > len(req.images):
        bad.append(f"正文引用了 @图片{want[-1]}，但只给了 {len(req.images)} 张图")
    return bad


def estimate_credits(resolution: str, duration: int) -> int | None:
    """预估灵感值。只有 480P 有实测口径（2/秒），其它返回 None——不编数字。"""
    if resolution == "480P":
        return 2 * duration
    return None


# ---- 命令构造 ----------------------------------------------------------------

def _base_args(req: Request, *, extra: list[str] | None = None) -> list[str]:
    args = ["omni2video", *([] if extra is None else extra),
            "--prompt", req.prompt,
            "--duration", str(req.duration),
            "--ratio", req.ratio,
            "--resolution", req.resolution,
            "--audio-output=" + ("true" if req.audio else "false"),
            "--output", "json"]
    if req.model:
        args += ["--model", req.model]
    if req.images:
        args += ["--images", ",".join(str(p) for p in req.images)]
    return args


def dry_run_args(req: Request) -> list[str]:
    """预演命令。`--dry-run` 必须排在 `--prompt` 前面（截断时先丢的是后面的参数）。"""
    return _base_args(req, extra=["--dry-run"])


def submit_args(req: Request) -> list[str]:
    return _base_args(req)


# ---- 回显解析与核对 ----------------------------------------------------------

def parse_echo(raw: str) -> dict:
    """dry-run 的 JSON → taskInput；看不懂就抛 SubmitError。"""
    try:
        data = json.loads(raw.strip())
        ti = data["request"]["taskInput"]
    except (json.JSONDecodeError, KeyError, TypeError):
        raise SubmitError("预演输出看不懂：" + raw.strip()[:200]) from None
    if not isinstance(ti, dict):
        raise SubmitError("预演输出看不懂：taskInput 不是对象")
    return ti


def _flag(val) -> bool | None:
    """把 true/false 的几种写法归一；认不出返回 None。"""
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        low = val.strip().lower()
        if low in ("true", "1", "yes", "on"):
            return True
        if low in ("false", "0", "no", "off"):
            return False
        return None
    if isinstance(val, int):
        return bool(val)
    return None


def _first_diff(a: str, b: str) -> int:
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return i + 1
    return 0 if len(a) == len(b) else min(len(a), len(b)) + 1


def _title_to_key(ti: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, val in (ti.get("elements") or {}).items():
        payload = val.get("payload") if isinstance(val, dict) else None
        if isinstance(payload, dict):
            out[str(payload.get("title") or "")] = key
    return out


def expected_prompt(req: Request, ti: dict) -> str:
    """把正文里的 @图片N 按元素标题换成 @{uuid}，得到平台应当看到的 originPrompt。"""
    titles = _title_to_key(ti)
    return IMG_REF.sub(lambda m: "@{" + titles.get("图片" + m.group(1), "图片" + m.group(1)) + "}",
                       req.prompt)


def check_echo(req: Request, ti: dict) -> list[str]:
    """逐项核对平台回显；返回差异清单。

    实测形状（CLI 0.0.3）：
      taskInput.prompt                  = 原文（含 @图片N）
      taskInput.promptMeta.originPrompt = @图片N → @{uuid}
      taskInput.promptMeta.orderedKeys  = 解析成功的引用顺序
      taskInput.elements[uuid].payload  = {title: 图片N, name: 文件名}
    """
    bad: list[str] = []
    got_prompt = str(ti.get("prompt") or "")
    if got_prompt != req.prompt:
        pos = _first_diff(got_prompt, req.prompt)
        bad.append(f"正文被改（{len(got_prompt)} 字 vs {len(req.prompt)} 字，第 {pos} 字起不同）")
    meta = ti.get("promptMeta") or {}
    origin = str(meta.get("originPrompt") or "")
    refs = IMG_REF.findall(req.prompt)
    if not origin:
        bad.append("回显里没有 promptMeta.originPrompt（CLI 版本不支持？）")
    elif origin != expected_prompt(req, ti):
        bad.append("promptMeta.originPrompt 与「按标题替换后的正文」不一致")
    else:
        hits = len(UUID_REF.findall(origin))
        if hits != len(refs):
            bad.append(f"解析成功的引用 {hits} 处，正文里出现 {len(refs)} 处——有引用没解析上")
        ordered = meta.get("orderedKeys")
        if isinstance(ordered, list) and len(ordered) != len(refs):
            bad.append(f"orderedKeys {len(ordered)} 个，正文里引用 {len(refs)} 处")

    model = str(ti.get("modelVersion") or "")
    if model and model not in ("3_0", "3.0"):
        bad.append(f"模型被换成 {model}（应为 3_0）")
    if ti.get("duration") != req.duration:
        bad.append(f"时长被改：{ti.get('duration')} ≠ {req.duration}")
    if ti.get("ratio") != req.ratio:
        bad.append(f"画幅被改：{ti.get('ratio')} ≠ {req.ratio}")
    if ti.get("selectedResolution") != req.resolution:
        bad.append(f"分辨率被改：{ti.get('selectedResolution')} ≠ {req.resolution}")
    if _flag(ti.get("audio")) is not req.audio:
        bad.append(f"音轨被改：{ti.get('audio')!r} ≠ {req.audio}")

    # 元素按标题对齐，不依赖 JSON 键序
    tiles = _title_to_key(ti)
    if len(tiles) != len(req.images):
        bad.append(f"参考图被丢：发出 {len(tiles)} 张，应为 {len(req.images)} 张")
    else:
        for i, p in enumerate(req.images, start=1):
            key = tiles.get(f"图片{i}")
            if not key:
                bad.append(f"缺少元素 图片{i}")
                continue
            payload = (ti.get("elements") or {}).get(key, {})
            payload = payload.get("payload") if isinstance(payload, dict) else {}
            name = str((payload or {}).get("name") or "")
            if name != p.name:
                bad.append(f"图片{i} 的文件不对：{name} ≠ {p.name}")

    ref = ti.get("reference") or {}
    ids = ref.get("refElementIds") if isinstance(ref, dict) else None
    if isinstance(ids, list):
        want_ids = [tiles.get(f"图片{i}", "") for i in range(1, len(req.images) + 1)]
        if ids != want_ids:
            bad.append(f"reference 元素序不对：{len(ids)} 个，与 图片1..{len(req.images)} 不按序对应")

    dangling = [n for n in wanted_refs(req.prompt) if f"图片{n}" not in tiles]
    if dangling:
        bad.append("正文引用了没有对应图的图号：" + "、".join(f"@图片{n}" for n in dangling))
    return bad


def check_auth(run=run_cli) -> None:
    """提交前的登录检查；未登录直接失败，不进入付费流程。"""
    code, out, err = run(["auth", "status", "--output", "json"], timeout=60)
    if code != 0:
        raise SubmitError(f"查不到登录状态（退出 {code}）：{(err or out).strip()[:200]}")
    try:
        data = json.loads(out.strip())
    except json.JSONDecodeError:
        raise SubmitError("登录状态输出看不懂：" + out.strip()[:200]) from None
    if not (isinstance(data, dict) and data.get("authenticated") is True):
        raise SubmitError("万相未登录：先 `wan auth login`，否则提交必失败")


def submit(req: Request, run=run_cli, timeout: int = 300) -> str:
    """真提交，返回 taskId。调用方必须先跑 dry_run + check_echo + 令牌比对。"""
    problems = validate(req)
    if problems:
        raise SubmitError("；".join(problems))
    check_auth(run)
    code, out, err = run(submit_args(req), timeout)
    if code != 0:
        raise SubmitError(f"提交失败（退出 {code}）：{(err or out).strip()[:300]}")
    task_id = parse_task_id(out)
    if not task_id:
        raise SubmitError("提交返回里没有 taskId：" + out.strip()[:200])
    return task_id


def parse_task_id(stdout: str) -> str:
    """CLI 不同版本可能回 JSON 也可能回文本，两路都认。"""
    text = stdout.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict):
        for key in ("taskId", "task_id", "id"):
            if data.get(key):
                return str(data[key])
        inner = data.get("data")
        if isinstance(inner, dict):
            for key in ("taskId", "task_id", "id"):
                if inner.get(key):
                    return str(inner[key])
    m = re.search(r'"(?:taskId|task_id)"\s*:\s*"([^"]+)"', text)
    return m.group(1) if m else ""


def wait(task_id: str, save_dir: Path, run=run_cli, timeout: int = 1800) -> Path:
    """轮询到完成并把成片落盘；返回成片路径。

    **没有新增 mp4 就是失败**：目录里上一轮的旧片不算这次的结果。
    """
    save_dir.mkdir(parents=True, exist_ok=True)
    before = {p.name for p in save_dir.glob("*.mp4")}
    code, out, err = run(["task", "wait", task_id, "--poll-interval", "3",
                          "--timeout", str(timeout), "--save", "--save-dir", str(save_dir),
                          "--output", "json"], timeout=timeout + 60)
    if code != 0:
        raise SubmitError(f"等待成片失败（退出 {code}，taskId {task_id} 可能仍在云端，"
                          f"可用 `wan result get {task_id}` 对账）：{(err or out).strip()[:300]}")
    fresh = [p for p in save_dir.glob("*.mp4") if p.name not in before]
    if not fresh:
        raise SubmitError(f"任务完成但没有新增成片（目录里只有旧片；taskId {task_id}）")
    return max(fresh, key=lambda p: p.stat().st_mtime)


def credits(run=run_cli) -> int | None:
    """灵感值余额；查不到返回 None（不编数字）。"""
    code, out, err = run(["credits", "--output", "json"], timeout=60)
    if code != 0:
        return None
    try:
        data = json.loads(out.strip())
    except json.JSONDecodeError:
        return None
    for obj in (data, data.get("data") if isinstance(data, dict) else None):
        if isinstance(obj, dict):
            for key in ("availableCount", "credits", "available"):
                if isinstance(obj.get(key), int):
                    return obj[key]
    return None
