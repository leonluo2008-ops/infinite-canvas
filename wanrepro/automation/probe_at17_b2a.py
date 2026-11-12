#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AT-17 B2a 探针：渲染层音频消费（五模板声音段 + 三态纯字符串规则 + hub 透传）。

跑法（`wanrepro/` 下，**纯离线零花钱**：不调模型、不跑 ffmpeg、不生成视频）：
  python -m automation.probe_at17_b2a               # 全部判据
  python -m automation.probe_at17_b2a --seed-demo   # 只重种三态演示任务（at17-demo-b2a-*）
判据全对才 exit 0；任何一条 FAIL 如实打印并 exit 1（判 returncode，禁子串判通过）。

判据：
  [1] 三态纯字符串规则（附注 10）：结构化片——
      沿用原片 = BGM 四字段原值 + 音效点逐条都在场；
      按新主体调整 = 骨架（情绪/节奏/织体/强弱走向）在、旧主体音效点（「盖子拧开…咔」）剥净；
      不要 = 官方三档式「无 BGM，仅保留环境声」且原片 BGM 词（轻快/木琴/钢琴）一个不剩；
      自由散文（v2.5 老产物）与无音轨片各走各的分支；未给三态 → 原路径（老调用方产物不变）。
  [2] 五模板落位：lumi 第 4 段 / seven 第 5 段 / timeline 第 4 段 / employee 口播行（行内）+
      分镜台词行（[画外音] | 画外音：原文） / seedance 第 3 段声音短句——各渲染一条，落位正确，
      正文不出现 `BGM.情绪` 这类字段名回显。
  [3] seedance-flow 流式分支：八 声音 不再出现在「未落入任何段的子块」丢弃清单里，
      三态产物声音段各异（附注 9：显式消费，不靠 DIMENSIONS 兜底）。
  [4] hub 端到端：隔离实例（探针自建自删）——GET /api/render 带/不带 audio_mode →
      返回提示词的声音段随之变化；枚举外值 400；hub 链 generate.do_render 把任务.json 的
      audio_mode 真带进 bridge query（任务.json=不要、audio_mode.json=调整，两者可区分）。
  [5] 演示任务留档：analysis/reverse/at17-demo-b2a-{bgm,nobgm,noaudio} 三条（复用 B2c 拆解
      产物重渲染，--seed-demo 可重种），产物声音段与三态一致。
  [6] 真实任务只读（auto-0006/0043 任务.json SHA256 不变）+ 探针自建目录自删、零残留。
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]          # wanrepro/
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pipeline" / "reverse"))
sys.path.insert(0, str(ROOT / "ui"))

from automation import config, generate, state      # noqa: E402
from automation.probe_guard import free_port, http_req  # noqa: E402

import server                                        # noqa: E402 —— 画布链 / 枚举交叉断言
INTENT = None                                        # 延迟到 [1] 里按路径加载（与 render 同一份）


def module_by_path(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RENDER = module_by_path("probe_b2a_render", ROOT / "pipeline" / "reverse" / "render.py")
INTENT = module_by_path("probe_b2a_intent", ROOT / "pipeline" / "reverse" / "intent.py")

ANALYSIS = ROOT / "analysis" / "reverse"
FIXTURE = ROOT / "tests" / "fixtures" / "at15-r1"
LEGACY = FIXTURE / "拆解.md"                       # 结构化夹具（v2.6 形态）
LEGACY_PROSE = ROOT / "tests" / "fixtures" / "at15-s1" / "拆解.md"   # v2.5 自由散文（只读）
REAL_TASKS = (ROOT / "automation" / "projects" / "auto-0006" / "任务.json",
              ROOT / "automation" / "projects" / "auto-0043" / "任务.json")
DEMO_PREFIX = "at17-demo-b2a-"
DEMO_SRC_PREFIX = "at17-demo-b2c-"

WORK = Path(tempfile.mkdtemp(prefix="probe-at17-b2a-"))
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f" —— {detail}" if detail and not cond else ""))
    return bool(cond)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------- 夹具

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
SOUND_NO_BGM = "\n".join((
    "- BGM.情绪：无BGM",
    "- BGM.节奏：无BGM",
    "- BGM.乐器/织体：无BGM",
    "- BGM.强弱走向：无BGM",
    "- 音效点：00:02-00:03 布料摩擦声",
    "- 人声语言：无",
    "- 台词：无",
))


def with_sound_body(markdown: str, body: str, audio: str = "有") -> str:
    """换掉「八 声音」正文并同步头部音轨标记（与 probe_at17_b1/b2c 同款做法）。"""
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


def disc_bgm() -> str:
    return with_sound_body(LEGACY.read_text(encoding="utf-8-sig"), SOUND_BGM)


def disc_no_bgm() -> str:
    return with_sound_body(LEGACY.read_text(encoding="utf-8-sig"), SOUND_NO_BGM)


def disc_no_audio() -> str:
    return with_sound_body(LEGACY.read_text(encoding="utf-8-sig"), "无音轨", audio="无")


def disc_legacy() -> str:
    return LEGACY_PROSE.read_text(encoding="utf-8-sig")


# ---------------------------------------------------------------- 渲染小工具

def template(name: str):
    text = (ROOT / "templates" / f"{name}.md").read_text(encoding="utf-8-sig")
    fm, body = RENDER.split_frontmatter(text)
    main_secs, _opt = RENDER.parse_sections(body)
    return fm, main_secs


def render_with(tpl: str, disc: str, mode: str) -> str:
    layers, _order = RENDER.parse_layers(disc)
    fm, secs = template(tpl)
    ents: list = []
    facts = RENDER.header_facts(disc)
    return RENDER.render_prompt(fm, secs, layers, ents, facts, "probe-b2a", tpl, {},
                                audio_mode=mode, audio_src="探针")


def render_quiet(tpl: str, disc: str, mode: str) -> tuple[str, str]:
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        text = render_with(tpl, disc, mode)
    return text, buf.getvalue()


def section_of(text: str, title: str) -> str:
    """取 `## <title>` 到下一个任意级标题 / 结尾的段落正文。"""
    match = re.search(rf"^## {re.escape(title)}\s*$", text, re.M)
    if not match:
        return ""
    rest = text[match.end():]
    nxt = re.search(r"^#{1,6} ", rest, re.M)
    return (rest[:nxt.start()] if nxt else rest).strip()


def section_like(text: str, prefix: str) -> str:
    """按标题前缀取段（标题带变量，如「✅ 分镜（N个镜头，0–10 秒）」）。"""
    match = re.search(rf"^## {re.escape(prefix)}.*$", text, re.M)
    if not match:
        return ""
    rest = text[match.end():]
    nxt = re.search(r"^#{1,6} ", rest, re.M)
    return (rest[:nxt.start()] if nxt else rest).strip()


# ---------------------------------------------------------------- [1] 三态规则

def probe_three_states() -> None:
    print("[1] 三态纯字符串规则（结构化 / 自由散文 / 无音轨 / 未给三态）")
    keep = render_with("lumi-4section", disc_bgm(), "沿用原片")
    adjust = render_with("lumi-4section", disc_bgm(), "按新主体调整")
    mute = render_with("lumi-4section", disc_bgm(), "不要")
    s_keep = section_of(keep, "声音与收束")
    s_adj = section_of(adjust, "声音与收束")
    s_mute = section_of(mute, "声音与收束")

    check("沿用原片：BGM 四字段原值全在场（轻快/中速/木琴/轻起渐强收）",
          all(x in s_keep for x in ("轻快", "中速", "木琴", "轻起渐强收")), s_keep[:200])
    check("沿用原片：音效点原样保留（「盖子拧开的一声「咔」」）",
          "盖子拧开的一声「咔」" in s_keep and "音效点" in s_keep, s_keep[:200])
    check("沿用原片：台词逐句带说话人（画外音 / 出镜者两类）",
          "画外音「Esta fragancia es increíble」" in s_keep
          and "出镜者「Mírame」" in s_keep, s_keep[:240])

    check("按新主体调整：骨架保留（情绪/节奏/织体/强弱走向都在）",
          all(x in s_adj for x in ("情绪（轻快）", "节奏（中速", "乐器/织体（木琴", "强弱走向（轻起渐强收）")),
          s_adj[:240])
    check("按新主体调整：旧主体音效点剥净（「盖子」「咔」「00:01-00:02」一个不剩）",
          "盖子" not in s_adj and "「咔」" not in s_adj and "00:01-00:02" not in s_adj, s_adj[:240])
    check("按新主体调整：不是沿用（没有原片音效点描述），且写明按新主体重新拟声",
          "重新拟声" in s_adj and "不沿用原片声源" in s_adj, s_adj[:240])
    check("按新主体调整：台词不在三态作用域内（照旧逐句在场，等改写链中文化）",
          "画外音「Esta fragancia es increíble」" in s_adj, s_adj[:240])

    check("不要：官方三档式「无 BGM，仅保留环境声」在场",
          "无 BGM" in s_mute and "仅保留环境声" in s_mute, s_mute[:200])
    check("不要：原片 BGM 词一个不剩（轻快/木琴/钢琴/渐弱/盖子）",
          not any(x in s_mute for x in ("轻快", "木琴", "钢琴", "渐弱", "盖子")), s_mute[:200])

    legacy_keep = section_of(render_with("lumi-4section", disc_legacy(), "沿用原片"), "声音与收束")
    legacy_adj = section_of(render_with("lumi-4section", disc_legacy(), "按新主体调整"), "声音与收束")
    legacy_mute = section_of(render_with("lumi-4section", disc_legacy(), "不要"), "声音与收束")
    check("自由散文 + 沿用：八层原句原样在场（不加工散文）",
          "背景音乐为节奏轻快" in legacy_keep, legacy_keep[:200])
    check("自由散文 + 调整：不逐字沿用散文（原句不在），只给骨架指令",
          "背景音乐为节奏轻快" not in legacy_adj and "骨架" in legacy_adj, legacy_adj[:200])
    check("自由散文 + 不要：散文整段不带（无原片 BGM 词）",
          "节奏轻快" not in legacy_mute and "无 BGM" in legacy_mute, legacy_mute[:200])

    no_audio = {m: section_of(render_with("lumi-4section", disc_no_audio(), m), "声音与收束")
                for m in RENDER.AUDIO_MODE_VALUES}
    check("无音轨片：三态都只写一行「无音轨」，不编 BGM / 音效点 / 台词",
          all(v.startswith("无音轨") and len(v.strip().splitlines()) == 1
              and not any(x in v for x in ("轻快", "BGM：", "音效点：")) for v in no_audio.values()),
          json.dumps(no_audio, ensure_ascii=False)[:240])

    unset = render_with("lumi-4section", disc_bgm(), "")
    unset_sec = section_of(unset, "声音与收束")
    check("未给三态 → 原路径（八 声音 逐块倒出，字段名在场；老调用方产物不变）",
          "BGM.情绪：轻快" in unset_sec, unset_sec[:200])
    check("未给三态 → 产物里不写音频三态附注（老产物一字不变）", "音频三态：" not in unset)

    check("三态枚举与 intent / server 同一套（沿用原片 / 按新主体调整 / 不要）",
          tuple(RENDER.AUDIO_MODE_VALUES) == tuple(INTENT.AUDIO_MODE_VALUES)
          == tuple(server.AUDIO_MODE_VALUES)
          == ("沿用原片", "按新主体调整", "不要"),
          f"{RENDER.AUDIO_MODE_VALUES} / {INTENT.AUDIO_MODE_VALUES} / {server.AUDIO_MODE_VALUES}")


# ---------------------------------------------------------------- [2] 五模板落位

def probe_templates() -> None:
    print("[2] 五模板落位（声音段 / 口播行内 / 台词行 / 流式短句）")
    disc = disc_bgm()
    for tpl, title in (("lumi-4section", "声音与收束"),
                       ("seven-section", "五 声音"),
                       ("timeline-narrative", "声音与收尾")):
        text = render_with(tpl, disc, "按新主体调整")
        sec = section_of(text, title)
        check(f"{tpl}：第 {title} 段落位正确（骨架在场、旧主体音效点不在、无字段名回显）",
              "骨架" in sec and "盖子" not in sec and "BGM.情绪" not in text,
              sec[:200])

    employee = render_with("employee-checklist", disc, "按新主体调整")
    oral = section_of(employee, "✅ 口播")
    shots = section_like(employee, "✅ 分镜")
    check("employee-checklist：口播行压成一行（声音不独立成段），骨架在场",
          "骨架" in oral and "\n" not in oral.strip() and "盖子" not in oral, oral[:240])
    check("employee-checklist：分镜段台词行带说话人（[画外音] | 画外音：原文）",
          "[画外音] | 画外音：Esta fragancia es increíble" in shots, shots[-160:])
    check("employee-checklist：出镜者也照同一格式渲染（两类枚举）",
          "[出镜者] | 出镜者：Mírame" in shots, shots[-160:])
    check("employee-checklist：正文字段名回显为零（BGM.情绪 / 音效点：00:01 这类）",
          "BGM.情绪" not in employee and "人声语言：" in employee or "人声语言" not in employee,
          "字段名回显检查")

    flow_keep, _ = render_quiet("seedance-flow", disc, "沿用原片")
    flow_adj, _ = render_quiet("seedance-flow", disc, "按新主体调整")
    flow_mute, _ = render_quiet("seedance-flow", disc, "不要")
    check("seedance-flow：声音短句在正文里（沿用=原值、调整=骨架、不要=无 BGM）",
          "盖子拧开的一声「咔」" in flow_keep and "骨架" in flow_adj and "无 BGM" in flow_mute,
          f"沿用={flow_keep[-120:]}｜调整={flow_adj[-120:]}")
    check("seedance-flow：台词与说话人进流式正文（不靠附注、不靠字段回显）",
          "台词：画外音「Esta fragancia es increíble」" in flow_adj, flow_adj[-160:])
    check("seedance-flow：流式正文无标题无附注无字段名回显",
          not re.search(r"^#{1,6}\s", flow_adj, re.M) and "附注" not in flow_adj
          and "BGM.情绪" not in flow_adj, flow_adj[:160])


# ---------------------------------------------------------------- [3] 流式分支不丢八层

def probe_flow_no_drop() -> None:
    print("[3] 流式分支：八 声音 不被 WARN 丢弃（附注 9）")
    for mode in RENDER.AUDIO_MODE_VALUES:
        text, err = render_quiet("seedance-flow", disc_bgm(), mode)
        dropped = [ln for ln in err.splitlines() if "未落入任何段" in ln]
        sound_dropped = [ln for ln in dropped if "八 声音" in ln]
        check(f"seedance-flow / {mode}：八 声音 不在丢弃清单里",
              not sound_dropped, (sound_dropped or dropped)[:200] if sound_dropped else
              ("丢弃清单里已无八 声音（其它层的既有 WARN 与音频无关）" if dropped else "零丢弃"))
        check(f"seedance-flow / {mode}：声音段真进正文（不是空段落）",
              ("BGM" in text), text[-120:])
    unset_text, unset_err = render_quiet("seedance-flow", disc_bgm(), "")
    check("未给三态：流式仍按原路径（八 声音 落回丢弃清单，老行为不变）",
          "八 声音" in unset_err, unset_err[:160])


# ---------------------------------------------------------------- [4] hub 端到端

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


def _seed_hub_task(projects: Path, local_id: str, bridge_id: str, audio_mode: str) -> Path:
    task_dir = projects / local_id
    task_dir.mkdir(parents=True, exist_ok=True)
    task = {
        "local_id": local_id, "bridge_id": bridge_id,
        "created_at": "2026-01-01T00:00:00+08:00", "state": "rendering",
        "history": [{"at": "2026-01-01T00:00:00+08:00", "from": None, "to": "ingested",
                     "note": "probe-at17-b2a 夹具"},
                    {"at": "2026-01-01T00:00:01+08:00", "from": "ingested", "to": "rendering",
                     "note": "probe-at17-b2a 夹具"}],
        "intent": "AT-17 B2a 渲染层音频消费探针夹具", "assets": [],
        "audio_mode": audio_mode,
        "ingestion": {"zip_sha256": f"probe-{local_id}", "zip_name": "probe",
                      "video_name": "", "text_name": "", "ignored": []},
    }
    (task_dir / "任务.json").write_text(json.dumps(task, ensure_ascii=False, indent=2),
                                        encoding="utf-8")
    return task_dir


def probe_hub_end_to_end() -> None:
    print("[4] hub 端到端：GET /api/render 带/不带 audio_mode；hub 链 query 透传")
    tid = f"probe-at17b2a-{os.getpid()}"
    probe_dir = ANALYSIS / tid
    iso = WORK / "hub"
    iso_projects = iso / "projects"
    iso_projects.mkdir(parents=True, exist_ok=True)
    real_before = {p.name for p in ANALYSIS.iterdir() if p.is_dir()}
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env = dict(os.environ, WANREPRO_AUTO_DIR=str(iso_projects))
    proc: subprocess.Popen | None = None
    try:
        # 探针自建任务目录（跑完自删）：拆解 + 清单 + audio_mode.json=按新主体调整
        probe_dir.mkdir(parents=True, exist_ok=True)
        (probe_dir / "拆解.md").write_text(disc_bgm(), encoding="utf-8")
        shutil.copy2(FIXTURE / "待确认清单.md", probe_dir / "待确认清单.md")
        (probe_dir / "audio_mode.json").write_text(json.dumps(
            {"audio_mode": "按新主体调整", "依据": "BGM.情绪=轻快", "待核对": False},
            ensure_ascii=False, indent=2), encoding="utf-8")

        # 实例输出落文件而不是 PIPE：PIPE 不排空会把服务端写日志的线程卡死（实测第 4 个请求超时）。
        log_path = iso / "server.log"
        log_fh = open(log_path, "w", encoding="utf-8")           # noqa: SIM115
        proc = subprocess.Popen(
            [sys.executable, str(ROOT / "ui" / "server.py"), "--port", str(port), "--no-browser"],
            cwd=str(ROOT), env=env, stdout=log_fh, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        )
        where = _wait_where(base, proc)
        check("隔离实例自述 tasks_dir == 临时根（读写同源）",
              Path(str(where.get("tasks_dir") or "")) == iso_projects, str(where)[:160])

        q = urllib.parse.quote
        st, data = http_req(base, f"/api/render/{tid}/seedance-flow?audio_mode={q('不要')}",
                            timeout=120)
        text_mute = str((data or {}).get("text") or "") if isinstance(data, dict) else ""
        check("GET /api/render?audio_mode=不要 → 200 且声音段是「无 BGM，仅保留环境声」",
              st == 200 and "无 BGM" in text_mute and "轻快" not in text_mute,
              f"{st} {text_mute[-160:]}")

        st, data = http_req(base, f"/api/render/{tid}/seedance-flow?audio_mode={q('沿用原片')}",
                            timeout=120)
        text_keep = str((data or {}).get("text") or "") if isinstance(data, dict) else ""
        check("GET /api/render?audio_mode=沿用原片 → 原片 BGM 原值 + 音效点都在场",
              st == 200 and "轻快" in text_keep and "盖子拧开的一声「咔」" in text_keep,
              f"{st} {text_keep[-160:]}")

        st, data = http_req(base, f"/api/render/{tid}/seedance-flow", timeout=120)
        text_auto = str((data or {}).get("text") or "") if isinstance(data, dict) else ""
        check("不带 audio_mode → 用任务目录 audio_mode.json（按新主体调整：骨架在、盖子不在）",
              st == 200 and "骨架" in text_auto and "盖子" not in text_auto,
              f"{st} {text_auto[-160:]}")

        st, data = http_req(base, f"/api/render/{tid}/seedance-flow?audio_mode={q('混合')}",
                            timeout=60)
        check("枚举外 audio_mode → 400（不静默丢弃，调用方不会误以为生效）",
              st == 400, f"{st} {str(data)[:120]}")

        # hub 链：任务.json=不要，audio_mode.json=按新主体调整——两者可区分，证明 query 真带上了
        hub_dir = _seed_hub_task(iso_projects, "auto-probe17b2a", tid, "不要")
        old_base = config.BRIDGE_BASE
        config.BRIDGE_BASE = base
        try:
            out = generate.do_render(hub_dir, "5,9:16", verbose=False)
        finally:
            config.BRIDGE_BASE = old_base
        prompt_path = hub_dir / generate.PROMPT_FILE
        hub_prompt = prompt_path.read_text(encoding="utf-8") if prompt_path.is_file() else ""
        check("hub 链 generate.do_render 成功推进到 awaiting_approval",
              state.load(hub_dir)["state"] == "awaiting_approval", f"{out}")
        check("hub 链把任务.json 的 audio_mode 带进 bridge query（产物是「不要」，不是 json 里的「调整」）",
              "无 BGM" in hub_prompt and "骨架" not in hub_prompt, hub_prompt[-200:])
    except Exception as err:                     # noqa: BLE001 —— 探针自己崩了也要如实报
        check(f"hub 端到端跑通（异常：{err}）", False, str(err)[:300])
    finally:
        try:
            log_fh.close()                                      # noqa: F821
        except Exception:                                       # noqa: BLE001
            pass
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        shutil.rmtree(probe_dir, ignore_errors=True)
        real_after = {p.name for p in ANALYSIS.iterdir() if p.is_dir()}
        check("探针自建分析目录已自删（真实 analysis 目录集一格不多不少）",
              real_after == real_before,
              f"多了：{sorted(real_after - real_before)}｜少了：{sorted(real_before - real_after)}")
        shutil.rmtree(iso, ignore_errors=True)


# ---------------------------------------------------------------- [5] 演示留档

DEMOS = ((f"{DEMO_PREFIX}bgm", "调整=骨架在场、旧主体音效点不在", "骨架", ("盖子",)),
         (f"{DEMO_PREFIX}nobgm", "沿用=无BGM 声明 + 原音效点", "无BGM", ("骨架",)),
         (f"{DEMO_PREFIX}noaudio", "不要=无音轨，不写声音", "无音轨", ("轻快", "音效点：", "BGM：")))


def seat_demos() -> list[str]:
    """复用 B2c 三条演示任务的拆解产物重渲染（零模型调用），产物留档供 :3000 查验。"""
    seeded: list[str] = []
    for name, _label, _must, _absent in DEMOS:
        key = name.rsplit("-", 1)[-1]
        src = ANALYSIS / f"{DEMO_SRC_PREFIX}{key}"
        if not src.is_dir():
            print(f"    [跳过] 源演示任务不在：{src.name}（先跑 probe_at17_b2c --seed-demo）")
            continue
        outdir = ANALYSIS / name
        outdir.mkdir(parents=True, exist_ok=True)
        for fn in ("拆解.md", "待确认清单.md", "待确认清单.json", "意图.txt", "audio_mode.json"):
            if (src / fn).is_file():
                shutil.copy2(src / fn, outdir / fn)
        cp = subprocess.run([sys.executable, str(ROOT / "pipeline" / "reverse" / "render.py"),
                             "--id", name, "--template", "seedance-flow"],
                            cwd=str(ROOT), capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
        print(f"    [种] {name}：render exit={cp.returncode} → "
              f"{(cp.stdout or cp.stderr).strip().splitlines()[-1][:90] if (cp.stdout or cp.stderr).strip() else '无输出'}")
        seeded.append(name)
    return seeded


def probe_demos() -> None:
    print("[5] 演示任务留档（at17-demo-b2a-*，三态各一条，:3000 任务抽屉提示词区可查）")
    for name, label, must, absent in DEMOS:
        outdir = ANALYSIS / name
        prompt = outdir / "提示词-seedance-flow.md"
        text = prompt.read_text(encoding="utf-8") if prompt.is_file() else ""
        check(f"演示任务 {name} 在盘且有 seedance-flow 提示词（{label}）",
              bool(text) and must in text and not any(x in text for x in absent),
              f"缺则跑 --seed-demo 重种｜{text[-120:] if text else '无提示词'}")
        check(f"演示任务 {name} 的 audio_mode.json 与 拆解.md 同目录",
              (outdir / "拆解.md").is_file() and (outdir / "audio_mode.json").is_file())


# ---------------------------------------------------------------- [6] 只读与残留

def probe_readonly_and_cleanup() -> None:
    print("[6] 真实任务只读 + 零残留")
    present = [p for p in REAL_TASKS if p.is_file()]
    missing = [p.parent.name for p in REAL_TASKS if not p.is_file()]
    check("真实任务快照基线非空（至少 auto-0043 任务.json 在盘）", bool(present),
          f"缺：{missing}")
    if missing:
        print(f"     如实记录：{'、'.join(missing)} 的 任务.json 不在盘（目录只剩 inbox），不参与 SHA 对账")
    check("临时工作根已清理（探针自建目录不留在盘上）",
          not any(WORK.glob("hub/projects/*")) if WORK.is_dir() else True)


# ---------------------------------------------------------------- 入口

def main() -> int:
    before = {p: sha256(p) for p in REAL_TASKS if p.is_file()}
    print(f"[probe-at17-b2a] 工作根 {WORK}")
    probe_three_states()
    probe_templates()
    probe_flow_no_drop()
    probe_hub_end_to_end()
    probe_demos()
    after = {p: sha256(p) for p in REAL_TASKS if p.is_file()}
    check("真实任务 auto-0006/0043 任务.json SHA256 前后一致（只读）",
          before == after and bool(before),
          f"{ {p.name: (before.get(p) == after.get(p)) for p in before} }")
    probe_readonly_and_cleanup()
    shutil.rmtree(WORK, ignore_errors=True)
    fails = [r for r in RESULTS if not r[1]]
    print(f"\nprobe_at17_b2a: {len(RESULTS) - len(fails)}/{len(RESULTS)} PASS")
    if fails:
        print("FAIL 汇总：")
        for name, _ok, detail in fails:
            print(f"  - {name}｜{detail[:200]}")
    return 1 if fails else 0


if __name__ == "__main__":
    if "--seed-demo" in sys.argv:
        print(f"[seed-demo] {seat_demos()}")
        raise SystemExit(0)
    raise SystemExit(main())
