# -*- coding: utf-8 -*-
"""AT-5 续跑与审批——rendering → awaiting_approval → approved → generating → done。

三个子命令（审批停点 = approve 独立命令，必须人工敲）：
  py -3 -m automation.generate render  <id> [--set duration=10,ratio=16:9]
        渲染提示词（默认模板 seedance-flow，红线 21）→ awaiting_approval + 回显预演
  py -3 -m automation.generate approve <id>
        所有者批准（审批快照绑定：之后改输入自动作废）→ approved
  py -3 -m automation.generate submit  <id>
        提交生成（confirm=true，走 SDGF 服务端封装 /api/sdgf/video/*）→ 轮询 →
        成片落任务目录 → done

渠道配置（SDGF，与画布同源）：环境变量 WANREPRO_SDGF_CHANNEL
  = JSON {"baseUrl":..., "apiKey":..., "model":...}；或文件 channel.json 同构。
  不进 git（automation/projects/ 已 ignore；凭据只在此处集中读）。

生成参数默认：duration=5 ratio=9:16 resolution=720 audio=true（--set 覆盖）。
"""
import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from contextlib import nullcontext
from pathlib import Path

from . import bridge, config, local_assets, replace, state, submit_view

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_TEMPLATE = "seedance-flow"      # 红线 21：A 线默认模板
DEFAULT_PARAMS = {"duration": 5, "ratio": "9:16", "resolution": "720", "audio": True}
PROMPT_FILE = "提示词.md"
VIDEO_FILE = "成片.mp4"
GEN_POLL_INTERVAL = 5
GEN_POLL_MAX = 3600  # 思念云普遍 5min+，上限 1 小时
ROOT = Path(__file__).resolve().parents[1]
WAN_ENGINE_DIR = "生成"
WAN_RECORD = "记录.json"


class GenError(RuntimeError):
    pass


# ---------------------------------------------------------------- 脱敏（AT-13 §2.8）

# 「先脱敏后截断」的唯一实现：截断在前会把 key 切半，前半段照样泄出去（R1 实证）。
_MASK_RULES = (
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{6,}"),
    re.compile(r"\bsk-[A-Za-z0-9._~-]{4,}"),
    re.compile(r"(?i)\b(api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|token|secret|"
               r"password|authorization)\b(\"?\s*[:=]\s*\"?)([^\s\"',;&}]{3,})"),
)


def mask_text(text, secret: str = "") -> str:
    """把凭证从任何要落账/回显的文本里抹掉（已知 key 直接替换，其余按形态兜底）。"""
    out = str(text or "")
    if secret:
        out = out.replace(secret, "***")
    for rule in _MASK_RULES:
        out = rule.sub(lambda m: (f"{m.group(1)}{m.group(2)}***"
                                  if m.lastindex and m.lastindex >= 3 else "***"), out)
    return out


def safe_error(text, secret: str = "", limit: int = 600) -> str:
    """落账用错误文本：**先脱敏，后截断**（顺序不可换）。"""
    return mask_text(text, secret)[:limit]


def _channel_source() -> dict:
    """渠道兜底源：env WANREPRO_SDGF_CHANNEL > projects/channel.json（无浏览器 CLI 场景）。"""
    raw = os.environ.get("WANREPRO_SDGF_CHANNEL", "")
    if not raw:
        f = config.PROJECTS_DIR / "channel.json"
        if f.is_file():
            raw = f.read_text(encoding="utf-8")
    if not raw:
        return {}
    try:
        ch = json.loads(raw)
    except json.JSONDecodeError as e:
        raise GenError(f"渠道配置不是合法 JSON: {e}") from None
    return ch if isinstance(ch, dict) else {}


def api_format_of(task: dict) -> str:
    """协议的单一解析入口：已批准 gen_channel.apiFormat > env/file 兜底 > 历史默认 sdgf。"""
    approved = (task or {}).get("gen_channel")
    approved = approved if isinstance(approved, dict) else {}
    try:
        fallback = _channel_source()
    except GenError:
        fallback = {}
    fmt = str(approved.get("apiFormat") or fallback.get("apiFormat") or "sdgf").strip().lower()
    return fmt


def channel(gen_channel: dict | None = None, api_key: str | None = None) -> dict:
    """本次提交要用的渠道四元组 {baseUrl, apiKey, model, apiFormat}。

    AT-13 §2.1/§2.2：**baseUrl/model/apiFormat 以「已批准 gen_channel」为真源**（画布配置经
    approve 固化进任务文件），不再静默换成 env/channel.json 里另一个渠道——渠道身份进快照，
    换了渠道必须重批。key 不进快照也不落盘：hub 提交时由请求一次性透传（api_key 参数），
    无浏览器 CLI 才退回 env>file 兜底；缺失就报明确错误，绝不盲提交。
    wan 渠道按 CLI 现合同：**key 可空**（走本机 wan CLI 会员），model 不消费（固定 wan3.0）。
    """
    approved = gen_channel if isinstance(gen_channel, dict) else {}
    fallback = _channel_source()
    api_format = str(approved.get("apiFormat") or fallback.get("apiFormat") or "sdgf").strip().lower()
    if api_format not in ("sdgf", "wan"):
        raise GenError(f"apiFormat={api_format!r} 属画布专用协议，hub 提交链不承接（§2.1 只接 sdgf 系与 wan）")
    base_url = str(approved.get("baseUrl") or fallback.get("baseUrl") or "").strip()
    api_key = str(api_key or fallback.get("apiKey") or "").strip()
    model = str(approved.get("model") or fallback.get("model") or "").strip()
    needed = [("baseUrl", base_url)]
    if api_format == "sdgf":
        needed += [("apiKey", api_key), ("model", model)]
    missing = [name for name, value in needed if not value]
    if missing:
        raise GenError(
            f"缺渠道配置（{'/'.join(missing)}）：hub 上先选画布渠道与模型再批准"
            f"（已批准 gen_channel 优先，key 由批准后的提交一次性透传），或设 "
            f"WANREPRO_SDGF_CHANNEL / {config.PROJECTS_DIR / 'channel.json'}"
            f"（{{baseUrl,apiKey,model}}）")
    return {"baseUrl": base_url, "apiKey": api_key, "model": model, "apiFormat": api_format}


def submit_params(params: dict) -> dict:
    """审批参数 → bridge 提交体四字段（两套词表都认，缺键取默认）。

    v1 任务 gen_params 已是桥词表 {duration, ratio, resolution, audio}；B1 起 approve 落的是
    由画布配置（size/vquality）派生的同形词表。这里再兜一层画布原名别名，防外部写入的任务
    文件缺 ratio/resolution 时抛 KeyError 把任务吊死。
    """
    p = params if isinstance(params, dict) else {}
    try:
        duration = int(p.get("duration", DEFAULT_PARAMS["duration"]))
    except (TypeError, ValueError):
        duration = DEFAULT_PARAMS["duration"]
    return {"duration": duration,
            "ratio": str(p.get("ratio") or p.get("size") or DEFAULT_PARAMS["ratio"]),
            "resolution": str(p.get("resolution") or p.get("vquality")
                              or DEFAULT_PARAMS["resolution"]),
            "audio": bool(p.get("audio", DEFAULT_PARAMS["audio"]))}


def parse_set(spec: str) -> dict:
    out = {}
    for part in (spec or "").split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        if k in ("duration",):
            out[k] = int(v)
        elif k in ("audio",):
            out[k] = v.lower() in ("1", "true", "yes")
        else:
            out[k] = v
    return out


def do_render(task_dir: Path, set_spec: str, verbose: bool = True) -> dict:
    """rendering → 渲染 → awaiting_approval（回显预演，等人工 approve）。"""
    task = state.load(task_dir)
    if task["state"] != "rendering":
        raise GenError(f"render 需 rendering 状态（先完成置换确认），当前 {task['state']}")
    params = {**DEFAULT_PARAMS, **parse_set(set_spec)}
    out = bridge.get(f"/api/render/{task['bridge_id']}/{DEFAULT_TEMPLATE}")
    prompt = out.get("text") or ""
    # 结构性校验（不假设模板格式——红线 14）：非空且够长即视为正文；
    # 模板差异（lumi 带 # 标题头 / seedance-flow 纯正文流）不是合同的一部分
    if len(prompt.strip()) < 200:
        raise GenError(f"渲染正文过短（{len(prompt.strip())} 字）不像提示词: {prompt[:120]}")
    (task_dir / PROMPT_FILE).write_text(prompt, encoding="utf-8")
    # 快照按任务自身版本算（老任务=v1 旧算法；已升 v2 的任务按 v2）——AT-13 §2.5 版本分流
    snap = state.snapshot_for({**task, "gen_params": params}, prompt)
    state.transition(task_dir, "awaiting_approval",
                     extra={"gen_params": params, "approval_snapshot": snap},
                     note=f"预演就绪（模板 {DEFAULT_TEMPLATE}）")
    # AT-9 B5：渲染完成即自动自检（三项→ 自检.json；检查器异常也不打断渲染主链，
    # selfcheck 内部如实落 FAIL 记录）。CLI render 与 hub 渲染共用此处，合同一致。
    from . import selfcheck
    check_items = selfcheck.run_selfcheck(task_dir)
    bad = [x for x in check_items if x.get("结论") != "PASS"]
    if verbose:
        print(f"  自检：{len(check_items) - len(bad)}/{len(check_items)} 项通过 → {task_dir / selfcheck.SELFCHECK_NAME}")
        for item in bad:
            print(f"  [自检⚠] {item.get('项')}：{item.get('证据', '')[:200]}")
        print(f"[待审批] {task['local_id']} 预演参数: {json.dumps(params, ensure_ascii=False)}")
        print(f"  快照 {snap[:16]}…；提示词已落 {PROMPT_FILE}（{len(prompt)} 字）")
        print(f"  批准: py -3 -m automation.generate approve {task['local_id']}")
    return {"params": params, "snapshot": snap, "prompt_chars": len(prompt)}


def do_approve(task_dir: Path, config: tuple | None = None, verbose: bool = True) -> str:
    """awaiting_approval → approved（人工命令 / hub 批准按钮 = 审批停点）。

    config=None：旧语义——批准 render 时落下的 approval_snapshot（**v1 快照**，老任务与 CLI）。
    config=(gen_params, gen_channel)：AT-13 §2.2 审批时序——服务端已校验固化的非敏感配置
    （无 key）此时落进任务文件并按 **v2 快照**（prompt+params+渠道身份+model+mode）绑定。
    """
    task = state.load(task_dir)
    st = task["state"]
    if st != "awaiting_approval":
        if st == "approved" and config is not None:
            version = state.snapshot_version(task)
            raise GenError(f"任务已批准（快照 v{version}）：改配置请先「撤销批准」走受控回退边"
                           f"（approved→awaiting_approval，不自动重批），再带新配置批准")
        raise GenError(f"approve 需 awaiting_approval，当前 {st}")
    if config is not None:
        gen_params, gen_channel = config
        prompt = (task_dir / PROMPT_FILE).read_text(encoding="utf-8")
        snap = state.snapshot_hash_v2(prompt, gen_params, gen_channel)
        state.transition(task_dir, "approved",
                         require={"approval.snapshot_sha256": snap},
                         extra={"gen_params": gen_params, "gen_channel": gen_channel,
                                "approval_snapshot": snap,
                                "snapshot_version": state.SNAPSHOT_VERSION,
                                "approval": {"snapshot_sha256": snap, "at": state._now(),
                                             "by": "owner-hub"}},
                         note="所有者批准（携带画布生成配置，快照 v2）")
        if verbose:
            print(f"[OK] {task['local_id']}: 已批准（渠道 {gen_channel.get('渠道名')} · "
                  f"模型 {gen_channel.get('model') or '—'}，快照 v2 {snap[:16]}…）→ 可 submit")
        return "approved"
    snap = task.get("approval_snapshot")
    if not snap:
        raise GenError("任务缺审批快照（先跑 render）")
    state.transition(task_dir, "approved",
                     require={"approval.snapshot_sha256": snap},
                     extra={"approval": {"snapshot_sha256": snap, "at": state._now(),
                                         "by": "owner-cli"}},
                     note="所有者批准")
    if verbose:
        print(f"[OK] {task['local_id']}: 已批准（快照 {snap[:16]}…）→ 可 submit")
    return "approved"


# ------------------------------------------------------- 提交意图标记与受控迁移（§2.3/§2.4）

def dispatch_of(task: dict) -> dict:
    """任务上的 dispatch 结构化值（无标记 → {}；兼容早期扁平串只读）。"""
    raw = (task or {}).get("dispatch")
    if isinstance(raw, dict):
        return dict(raw)
    return {"phase": str(raw)} if raw else {}


def _dispatch_write(task_dir: Path, guard, phase: str, *, note: str = "",
                    job: str | None = None, carrier: str | None = None,
                    asset_host: list | None = None) -> dict:
    """落一次 dispatch 阶段（**单独一次原子写**，与外呼严格分先后：先落盘再发请求）。

    asset_host：可选，装配链自述的图床来源列表（AT-14 B3 A）——给了就在**同一次 save** 里
    落 task["asset_host"]，不另开写盘窗口。
    """
    with guard():
        task = state.load(task_dir)
        cur = dispatch_of(task)
        entry = {"phase": phase, "at": state._now(), "note": note,
                 "job": job if job is not None else str(cur.get("job") or ""),
                 "carrier": carrier if carrier is not None else str(cur.get("carrier") or "")}
        task["dispatch"] = entry
        if asset_host is not None:
            task["asset_host"] = list(asset_host)
        state.save(task_dir, task)
        return task


def _mark_submit_unknown(task_dir: Path, guard, *, reason: str, secret: str = "") -> dict:
    """标「未知收单」持久阻断（§2.3）：只落标记，不猜结果、不重提。"""
    msg = safe_error(reason, secret)
    with guard():
        task = state.load(task_dir)
        task["submit_unknown"] = {"at": state._now(), "reason": msg,
                                  "dispatch": state.dispatch_phase(task)}
        task["gen_error"] = msg
        state.save(task_dir, task)
        return task


def release_unknown(task_dir: Path, *, note: str = "人工核对渠道无单后解除阻断",
                    guard=None) -> dict:
    """人工解除「未知收单」阻断（§2.3）：清 submit_unknown/gen_error，dispatch 记 cleared。

    这是**人工动作**（到渠道控制台核对确实没有这一单之后），解除记 history；解除本身不迁移状态。
    """
    with (guard or nullcontext)():
        task = state.load(task_dir)
        prev = task.get("submit_unknown")
        cur = task["state"]
        task.pop("submit_unknown", None)
        task.pop("gen_error", None)
        old = dispatch_of(task)
        task["dispatch"] = {"phase": "cleared", "at": state._now(), "note": note,
                            "job": str(old.get("job") or ""),
                            "carrier": str(old.get("carrier") or "")}
        detail = ""
        if isinstance(prev, dict) and prev.get("reason"):
            detail = f"（原阻断：{safe_error(prev['reason'], '', 120)}）"
        task.setdefault("history", []).append(
            {"at": state._now(), "from": cur, "to": cur,
             "note": safe_error(f"{note}{detail}", "", 300)})
        state.save(task_dir, task)
        return task


def heal_create_reject_block(task_dir: Path, *, guard=None) -> dict:
    """AT-16 B11：创建阶段渠道 4xx 的历史误判自愈（命中即清阻断，未命中零写盘）。

    旧版 sdgf_http 把渠道任何 HTTPError 都包成 502，于是 auto-0043 现场那条
    「403 fail_to_fetch_task / video_request_failed（渠道明确说任务没建成）」被落成
    submit_unknown + dispatch=dispatched，把一个「改配置直接重提」逼成「先核对控制台 +
    解除阻断」。判定与展示同源（submit_view.create_reject_evidence：原文有创建端点 4xx 标记、
    无云端 taskId/gen_job、阻断标记还在）——三件套同时成立才动，其余一律不动。
    动法则复用人工路径的 release_unknown（清阻断 + dispatch=cleared + 记 history）。
    """
    with (guard or nullcontext)():
        task = state.load(task_dir)
        code = submit_view.create_reject_evidence(task)
        if not code:
            return task
        return release_unknown(
            task_dir, guard=guard,
            note=f"创建阶段渠道 HTTP {code} 明确未建单（历史误判自动解除，无需人工核对）")


def wan_record(carrier: str) -> dict:
    """万相承载任务的引擎记录（`analysis/reverse/<carrier>/生成/记录.json`，唯一状态源）。"""
    name = str(carrier or "")
    if not name or "/" in name or "\\" in name:
        return {}
    path = ROOT / "analysis" / "reverse" / name / WAN_ENGINE_DIR / WAN_RECORD
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def cloud_evidence(task_dir: Path, task: dict) -> dict:
    """云端在途证据（§2.4 第二步）：SDGF=gen_task_id；wan=记录.json.taskId（**不是**本地 wan-* id）。"""
    fmt = api_format_of(task)
    if fmt == "wan":
        carrier = str(dispatch_of(task).get("carrier") or "")
        rec = wan_record(carrier)
        cid = str(rec.get("taskId") or "")
        status = str(rec.get("status") or "")
        return {"apiFormat": "wan", "carrier": carrier, "cloud_task_id": cid,
                "in_flight": bool(cid) and status == "running",
                "record_status": status, "record_phase": str(rec.get("phase") or "")}
    cid = str(task.get("gen_task_id") or task.get("cloud_task_id") or "")
    return {"apiFormat": "sdgf", "cloud_task_id": cid,
            "in_flight": bool(cid) and task.get("state") == "generating"}


def diagnose(task_dir: Path, task: dict, *, active_submit: bool = False) -> dict:
    """疑似孤儿三步判定（§2.4）——只判定不动状态；透给 hub get_task 与 CLI。"""
    ev = cloud_evidence(task_dir, task)
    video = task_dir / VIDEO_FILE
    has_video = video.is_file() and video.stat().st_size > 0
    phase = state.dispatch_phase(task)
    unknown = bool(task.get("submit_unknown"))
    out = {"state": task.get("state"), "apiFormat": ev["apiFormat"],
           "active_submit": bool(active_submit), "cloud_task_id": ev["cloud_task_id"],
           "cloud_in_flight": ev["in_flight"], "has_video": has_video,
           "dispatch": phase, "submit_unknown": unknown,
           "step": 3, "submit_lost": False, "reason": ""}
    if task.get("state") == "generating":
        if active_submit:
            out.update(step=1, reason="提交进行中（活跃提交占用）——禁重试")
        elif ev["in_flight"]:
            out.update(step=2, reason=f"有云端 taskId（{ev['cloud_task_id']}）——续查该任务，不重提")
        elif has_video:
            out.update(step=2, reason="成片已落盘——走完成/续查路径")
        elif unknown or phase in ("prepared", "dispatched"):
            out.update(step=3, submit_lost=True,
                       reason="无在途证据且无成片＝疑似孤儿：先到渠道控制台核对无单，再受控恢复")
        else:
            out.update(step=3, submit_lost=True,
                       reason="无在途证据且无成片＝疑似孤儿（无提交痕迹）：核对无单后受控恢复")
    return out


# ---------------------------------------------------------------- 提交体装配

def _submit_images(task: dict) -> list[str]:
    """提交图清单：按图号序、组内去重（与发号映射同一 row_unit 口径；组外同 URL 不合并）。"""
    groups = replace.load_groups(str(task.get("bridge_id") or ""))
    entries = sorted((f for f in (task.get("assets_confirmed") or []) if isinstance(f, dict)),
                     key=lambda f: int(f.get("no") or 0))
    seen_units: set[tuple] = set()
    images: list[str] = []
    for f in entries:
        unit = replace.row_unit(int(f.get("no") or 0), groups)
        if unit in seen_units:
            continue
        seen_units.add(unit)
        url = str(f.get("url") or "")
        if url:
            images.append(url)
    return images


def url_to_data_url(url: str, no: int) -> str:
    """公网图 URL → dataUrl（万相 CLI 只解 dataUrl）。下载失败即抛=前置拒绝，不花钱。

    错误分两类（AT-14 B3：过期的重传，网络抖的重试——文案让员工一眼知道怎么办）：
      · 403/404/410 = 图床直链已失效 → 「已过期，请重传」；
      · 其余（连不上/超时/IO）→ 「不可用，请重试」。
    """
    u = str(url or "").strip()
    if u.lower().startswith("data:image/"):
        return u
    if not u.lower().startswith("http"):
        raise GenError(f"参考图 {no} 不是可下载的 URL：{u[:80]!r}")
    req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0 (wanrepro-hub)"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            blob = resp.read()
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    except urllib.error.HTTPError as e:
        if e.code in (403, 404, 410):
            raise GenError(f"参考图 {no} 已过期（HTTP {e.code}）——图已过期，请重传") from None
        raise GenError(f"参考图 {no} 不可用（HTTP {e.code}）——图片不可用，请重试") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise GenError(f"参考图 {no} 不可用（{e}）——图片不可用，请重试") from None
    if not blob:
        raise GenError(f"参考图 {no} 是 0 字节（{u[:80]}）")
    if len(blob) > 20 * 1024 * 1024:
        raise GenError(f"参考图 {no} 超过 20 MB（{len(blob) / 1048576:.1f} MB）")
    mime = ctype if ctype.startswith("image/") else "image/png"
    return f"data:{mime};base64," + base64.b64encode(blob).decode("ascii")


# 装配体元素的 dataUrl mime（按落盘扩展名；jpg/jpeg 同 mime）
_DATAURL_MIME = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}


def _assemble_images(refs: list[str], api_format: str) -> list[dict]:
    """引用列表 → 装配体 images 元素（AT-14 B3 A：提交时才装配，复用既有链）。

    三类输入：
      · local 引用 → 本地读字节转 dataUrl（**零网络请求**）；读失败=前置拒绝（图丢了让员工重传）；
      · data:image → 原样透传（已是装配体形态）；
      · 老 https URL → wan 转 dataUrl（url_to_data_url）；sdgf 原样带 url
        （服务端 sdgf_plan 对 {name,url} / {name,dataUrl} 两种都原生收）。
    元素 name 按图号固定（图片N / imgN），组内去重语义在 _submit_images，已保证序号=原图号序。
    """
    items: list[dict] = []
    for i, u in enumerate(refs, 1):
        u = str(u or "").strip()
        if local_assets.is_ref(u):
            try:
                blob = local_assets.read(u)
            except local_assets.LocalAssetError:
                raise GenError(f"参考图 {i} 本地资产缺失（{u}）——请重传") from None
            _lid, name = local_assets.parse_ref(u)
            ext = name.rsplit(".", 1)[-1]
            mime = _DATAURL_MIME.get(ext, "image/png")
            items.append({"name": f"图片{i}.{ext}",
                          "dataUrl": f"data:{mime};base64," + base64.b64encode(blob).decode("ascii")})
        elif u.lower().startswith("data:image/"):
            items.append({"name": f"图片{i}", "dataUrl": u})
        elif u.lower().startswith("http"):
            if api_format == "wan":
                items.append({"name": f"图片{i}", "dataUrl": url_to_data_url(u, i)})
            else:
                items.append({"name": f"img{i}", "url": u})
        else:
            raise GenError(f"参考图 {i} 不是可用的引用（local 引用/dataUrl/公网 URL）：{u[:80]!r}")
    return items


def _sdgf_payload(prompt: str, params: dict, items: list[dict], ch: dict) -> tuple[str, dict]:
    body = {"prompt": prompt, "duration": params["duration"], "ratio": params["ratio"],
            "resolution": params["resolution"], "audio": params["audio"], "confirm": True,
            "channel": {"baseUrl": ch["baseUrl"], "apiKey": ch["apiKey"], "model": ch["model"]}}
    if items:
        body["images"] = items                 # 元素可带 url 或 dataUrl——服务端 sdgf_plan 两种都收
    return "/api/sdgf/video/run", body


def _wan_payload(prompt: str, params: dict, items: list[dict], ch: dict) -> tuple[str, dict]:
    """万相派发（§2.1 R2 IMPL）：装配体直接消费（元素都是 dataUrl），走 wan 承载端点（不派 SDGF 体）。"""
    body = {"prompt": prompt, "duration": params["duration"], "ratio": params["ratio"],
            "resolution": params["resolution"], "audio": params["audio"], "confirm": True}
    if items:
        body["images"] = items
    return "/api/wan/video/run", body


def ref_state(ref: str, timeout: float = 8.0) -> str:
    """引用健康态（AT-14 B3 probe 面板数据源）：local|ok|expired|unknown。

    local 引用看盘上文件在不在（不猜、不发网络请求）；data:image 恒 ok；
    https 先 HEAD 失败再 GET：200/206=ok，403/404/410=expired，其余=unknown。
    **禁止**按文件名猜「本地有没有同名副本」——真源只有引用串自己。
    """
    u = str(ref or "").strip()
    if local_assets.is_ref(u):
        try:
            local_assets.read(u)
            return "local"
        except local_assets.LocalAssetError:
            return "expired"
    if u.lower().startswith("data:image/"):
        return "ok"
    if not u.lower().startswith("http"):
        return "unknown"

    def _probe(method: str) -> str:
        req = urllib.request.Request(u, method=method,
                                     headers={"User-Agent": "Mozilla/5.0 (wanrepro-hub)"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return "ok" if resp.status in (200, 206) else "unknown"
        except urllib.error.HTTPError as e:
            if e.code in (403, 404, 410):
                return "expired"
            return "unknown"
        except (urllib.error.URLError, TimeoutError, OSError):
            return "unknown"

    st = _probe("HEAD")
    return st if st != "unknown" else _probe("GET")


def _pre_reject(task_dir: Path, guard, message: str, *, note: str = "",
                secret: str = "", extra: dict | None = None) -> None:
    """前置拒绝：受控回退 approved→awaiting_approval（清批准，保留资产/提示词/草稿配置）。

    extra：随同一次 save 落盘的附加键（AT-16 B12 的图源自述 asset_host）。
    """
    msg = safe_error(message, secret)
    with guard():
        task = state.load(task_dir)
        if task["state"] != "approved":
            return                       # 续查路径的状态由调用方给明确错误，这里不动
        state.reject_back_to_approval(task_dir, note=note or "前置拒绝：受控回退重批",
                                      error=msg, extra=extra)


def _enter_generating(task_dir: Path, guard, *, job: str, cloud: str, carrier: str = "",
                      gen_task_id: str = "", note: str = "云端已收单",
                      extra: dict | None = None) -> dict:
    """进入 generating：gen_job / 云端 id / dispatch=confirmed **同一次 save 原子写**（§2.4）。

    extra：附加键（如 AT-14 B3 的 asset_host）并入同一次 transition 的 extra——不另写盘。
    """
    with guard():
        task = state.load(task_dir)
        if task["state"] == "generating":
            return task
        state.transition(task_dir, "generating",
                         extra={"gen_job": job, "gen_task_id": gen_task_id,
                                "cloud_task_id": cloud, "gen_error": "",
                                "dispatch": {"phase": "confirmed", "at": state._now(),
                                             "note": note, "job": job, "carrier": carrier},
                                **(extra or {})},
                         note=note)
        return state.load(task_dir)


def _finish(task_dir: Path, data: bytes, *, guard, verbose: bool, note: str) -> str:
    """成片落盘 → done（写盘失败/0 字节一律如实抛，不当成功）。"""
    vf = task_dir / VIDEO_FILE
    vf.write_bytes(data)
    if vf.stat().st_size <= 0:
        raise GenError("成片 0 字节——如实记失败")
    with guard():
        task = state.load(task_dir)
        if task["state"] != "generating":
            raise GenError(f"成片已落盘但任务状态是 {task['state']}（应为 generating）——人工核对")
        state.transition(task_dir, "done", note=safe_error(note, "", 500))
    if verbose:
        print(f"[OK] {task_dir.name}: 成片落盘 {vf}（{vf.stat().st_size} 字节）→ done")
    return "done"


def _try_file(task_id: str) -> bytes | None:
    if not task_id:
        return None
    try:
        return bridge.get_bytes(f"/api/sdgf/video/file/{task_id}")
    except bridge.BridgeError:
        return None


def _poll_sdgf(task_dir: Path, *, job: str, cloud: str, guard, verbose: bool,
               on_progress=None, secret: str = "") -> str:
    """SDGF 轮询：done/error 双终态、failed 方言、超时未知与重启续查（§2.6/§5）。"""
    waited = 0
    while waited < GEN_POLL_MAX:
        try:
            rec = bridge.get(f"/api/sdgf/video/job/{job}")
        except bridge.BridgeError as e:
            data = _try_file(cloud)                 # JOBS 是内存态：重启后按云端 id 找成片
            if data:
                return _finish(task_dir, data, guard=guard, verbose=verbose,
                               note=f"重启恢复：按云端 taskId={cloud} 取回成片")
            raise GenError(safe_error(
                f"轮询端点丢了（{e}）——按云端 taskId={cloud or '?'} 续查；本任务未判失败、别重提",
                secret)) from None
        st = str(rec.get("status") or "")
        if st == "done":
            tid = str(rec.get("taskId") or cloud or "")
            data = _try_file(tid)
            if data is None:
                data = bridge.get_bytes(f"/api/sdgf/video/file/{tid}")   # 抛错即如实
            return _finish(task_dir, data, guard=guard, verbose=verbose,
                           note=f"成片 taskId={tid}")
        if st in ("failed", "error"):            # 双方言终态（§2.6）
            msg = safe_error(f"云端判失败（{st}）：{rec.get('error') or '原因见渠道'}", secret)
            with guard():
                task = state.load(task_dir)
                if task["state"] != "failed":
                    state.transition(task_dir, "failed", extra={"gen_error": msg}, note=msg)
            raise GenError(msg)
        pct = rec.get("progress")
        if on_progress is not None:
            try:
                on_progress(pct if isinstance(pct, (int, float)) else None, waited)
            except Exception:  # noqa: BLE001 —— 进度回写失败不拖垮生成主链
                pass
        if verbose and pct is not None and waited % 30 == 0:
            print(f"  生成中 {pct}%（{waited}s）")
        time.sleep(GEN_POLL_INTERVAL)
        waited += GEN_POLL_INTERVAL
    msg = safe_error(f"超 {GEN_POLL_MAX}s 未完成：云端可能仍在跑（taskId={cloud or '?'}）"
                     f"——本任务不判失败、不重提，按 taskId 续查", secret)
    with guard():
        task = state.load(task_dir)
        task["gen_error"] = msg
        state.save(task_dir, task)
    raise GenError(msg)


def _wan_failure_kind(phase: str, cloud_id: str) -> str:
    """万相失败四层（§2.3 R2 N-03）——只认结构化阶段，不猜错误文案。

    phase=planning/pre_rejected：引擎连 `WS.submit` 都没走到 → 前置拒绝（可回退重试）；
    phase=submitting/submit_unknown 且无云端 id：已外呼但拿不到 id → 提交结果未知（阻断）；
    有云端 id 但没终态（wait_timeout）→ 可能还在跑；有 id + cloud_failed → 确认失败。
    """
    if phase in ("pre_rejected", "preflight_failed", "planning"):
        return "pre_rejected"
    if not cloud_id:
        return "submit_unknown"
    if phase in ("cloud_failed", "wait_failed", "failed"):
        return "cloud_failed"
    return "cloud_timeout"          # 有云端 id 但没拿到终态：可能还在跑


def _poll_wan(task_dir: Path, *, job: str, carrier: str, guard, verbose: bool,
              on_progress=None, secret: str = "") -> str:
    """万相轮询：结构化四阶段（前置拒绝/提交未知/已知 id 等待超时/确认失败）+ 云端 id 回执。"""
    waited = 0
    while waited < GEN_POLL_MAX:
        try:
            rec = bridge.get(f"/api/wan/video/job/{job}")
        except bridge.BridgeError:
            try:                                     # JOBS 丢了（重启）→ 按承载目录读记录
                rec = bridge.get(f"/api/wan/video/state/{carrier}")
            except bridge.BridgeError as e:
                raise GenError(safe_error(
                    f"万相状态查不到（承载 {carrier}，job={job or '?'}）：{e}"
                    f"——任务未判失败，先到 analysis/reverse/{carrier}/生成/记录.json 核对",
                    secret)) from None
        status = str(rec.get("status") or "")
        phase = str(rec.get("phase") or "")
        cloud = str(rec.get("cloudTaskId") or rec.get("cloud_task_id") or "")
        if cloud:
            task = state.load(task_dir)
            if str(task.get("cloud_task_id") or "") != cloud:
                with guard():                        # 云端 id 回执（本地 wan-* 不算云端 id）
                    task = state.load(task_dir)
                    task["cloud_task_id"] = cloud
                    state.save(task_dir, task)
            if state.load(task_dir)["state"] == "approved":
                _enter_generating(task_dir, guard, job=job, cloud=cloud, carrier=carrier,
                                  note="万相云端已收单（id 回执与 generating 同盘）")
        if status == "done":
            if not cloud:
                raise GenError(safe_error(
                    f"万相报完成但拿不到云端 id（承载 {carrier}）——人工核对任务目录", secret))
            data = bridge.get_bytes(f"/api/wan/video/file/{carrier}")
            return _finish(task_dir, data, guard=guard, verbose=verbose,
                           note=f"成片（万相 taskId={cloud}，承载 {carrier}）")
        if status == "failed":
            kind = _wan_failure_kind(phase, cloud)
            msg = safe_error(f"万相失败（阶段 {phase or '未知'}）：{rec.get('error') or '原因见记录'}", secret)
            if kind == "pre_rejected":
                if state.load(task_dir)["state"] == "generating":
                    state.recover_orphan_generating(
                        task_dir, note="万相前置拒绝（本次提交已收尾）：受控回退重批")
                else:
                    state.reject_back_to_approval(task_dir, note="万相前置拒绝：受控回退重批",
                                                  error=msg)
                raise GenError(msg)
            if kind == "cloud_failed":
                with guard():
                    task = state.load(task_dir)
                    if task["state"] != "failed":
                        state.transition(task_dir, "failed", extra={"gen_error": msg}, note=msg)
                raise GenError(msg)
            if kind == "cloud_timeout":
                _enter_generating(task_dir, guard, job=job, cloud=cloud, carrier=carrier,
                                  note="万相等待超时（云端可能仍在跑）")
                with guard():
                    task = state.load(task_dir)
                    task["gen_error"] = msg
                    state.save(task_dir, task)
                raise GenError(msg)
            _mark_submit_unknown(task_dir, guard,
                                 reason=f"万相提交结果未知（无云端 taskId）：{rec.get('error') or phase}",
                                 secret=secret)
            raise GenError(safe_error(f"万相提交结果未知（无云端 taskId）：{msg}"
                                      f"——已阻断，先核对渠道无单", secret)) from None
        if on_progress is not None:
            try:
                on_progress(None, waited)            # 万相没有百分比：只报等待时长
            except Exception:  # noqa: BLE001
                pass
        if verbose and waited % 30 == 0:
            print(f"  万相生成中（阶段 {str(rec.get('stage') or '')}，{waited}s）")
        time.sleep(GEN_POLL_INTERVAL)
        waited += GEN_POLL_INTERVAL
    msg = safe_error(f"超 {GEN_POLL_MAX}s 未完成：万相云端可能仍在跑（承载 {carrier}"
                     f"{'，taskId=' + cloud if cloud else ''}）——本任务不判失败、不重提", secret)
    with guard():
        task = state.load(task_dir)
        task["gen_error"] = msg
        state.save(task_dir, task)
    raise GenError(msg)


def _resume(task_dir: Path, task: dict, *, guard, verbose: bool, on_progress=None,
            api_key: str | None = None) -> str:
    """generating 续查（**不创建**）：有确定回执就按既有 job/云端 id 继续等/取片。"""
    secret = api_key or ""
    phase = state.dispatch_phase(task)
    job = str(task.get("gen_job") or "")
    ev = cloud_evidence(task_dir, task)
    if phase == "confirmed" or job or ev["cloud_task_id"]:
        if ev["apiFormat"] == "wan":
            return _poll_wan(task_dir, job=job, carrier=str(dispatch_of(task).get("carrier") or ""),
                             guard=guard, verbose=verbose, on_progress=on_progress,
                             secret=secret)
        return _poll_sdgf(task_dir, job=job, cloud=str(ev["cloud_task_id"]), guard=guard,
                          verbose=verbose, on_progress=on_progress, secret=secret)
    if not task.get("submit_unknown"):           # 无在途证据 = 疑似孤儿：按未知收单处理，禁重提
        _mark_submit_unknown(task_dir, guard,
                             reason="generating 但无 gen_job/云端 id/成片——疑似孤儿，"
                                    "先核对渠道无单再受控恢复",
                             secret=secret)
    raise GenError("generating 疑似孤儿（无活跃提交、无云端 taskId、无成片）——已标 submit_unknown；"
                   "核对渠道无单后「解除阻断」并走受控恢复，别重提")


def do_submit(task_dir: Path, verbose: bool = True,
              on_progress=None, frozen: dict | None = None,
              write_guard=None, api_key: str | None = None) -> str:
    """approved → generating →（轮询）→ done；成片落任务目录。

    提交结果二分类（AT-13 §2.3）：
      · 前置拒绝（校验失败/缺配置/并发满 409/状态不符）→ 受控回退 awaiting_approval，可修正重试；
      · 未知收单（超时/断连/非 JSON/无 id/回执写盘失败）→ submit_unknown 持久阻断，禁重提；
      · 外呼**之前**先落 dispatch=prepared/dispatched（磁盘标记，天然跨进程），成功回执与
        generating/gen_job/云端 id 同一次 save 原子写（§2.4，消除两次写盘窗口）。
    on_progress: 可选回调 (pct:int|None, waited:int)——Notion 中控台用它回写进度。
    frozen: 接纳时冻结的配置副本 {gen_params, gen_channel}（AT-13 §2.7：后台线程用冻结副本）。
    write_guard: 写任务文件用的短持上下文（hub 传任务锁；缺省 nullcontext = CLI 单进程语义）。
    api_key: hub 提交时一次性透传的 key（不落盘、不进 argv/URL）。
    """
    guard = write_guard or nullcontext
    task = state.load(task_dir)
    # AT-16 B11：旧版把「创建阶段渠道 4xx（明确未建单）」误标成 submit_unknown；
    # 判定与展示同源（submit_view），命中就先按人工路径解除（记 history），
    # 否则下面两道阻断守卫会把「改配置后直接重提」堵死。
    task = heal_create_reject_block(task_dir, guard=guard)
    st = task["state"]
    if st not in ("approved", "generating"):
        raise GenError(f"submit 需 approved（先审批），当前 {st}")

    # 守卫（§2.3）：submit_unknown 是持久阻断，**不依赖 dispatch 是否还在**（重启/人工改盘后
    # dispatch 可能已清而阻断仍在）——单看它也要挡住，否则会绕过阻断重复建单、重复扣费。
    if task.get("submit_unknown"):
        prev = task.get("submit_unknown") or {}
        raise GenError("提交结果未知（submit_unknown 阻断未解除）——先到渠道控制台核对没有这一单，"
                       "再人工「解除阻断」；不许重复创建。原因："
                       + safe_error(str(prev.get("reason") or "未记录"), api_key or "", 160))

    # 守卫（§2.3 R2 N-01）：有 dispatch、无 confirmed 回执 → 禁建。hub/runner/CLI 共用这一条。
    if state.dispatch_blocks_create(task):
        phase = state.dispatch_phase(task)
        _mark_submit_unknown(task_dir, guard,
                             reason=f"上次提交没拿到确定回执（dispatch={phase}）——云端可能已收单",
                             secret=api_key or "")
        raise GenError("提交结果未知（有 dispatch、无 confirmed 回执）——已标 submit_unknown："
                       "先到渠道控制台核对没有这一单，再人工「解除阻断」；不许重复创建")

    frozen = frozen or {}
    prompt = (task_dir / PROMPT_FILE).read_text(encoding="utf-8")
    # 快照按**原样落账的参数**算（不做提交体归一），归一化只在拼 body 时做——
    # 否则外部写入的 "5"/5 之类类型差异会被误判成「审批作废」。
    raw_params = dict(frozen.get("gen_params") or task.get("gen_params") or {})
    gen_channel = dict(frozen.get("gen_channel") or task.get("gen_channel") or {})
    snap = state.snapshot_for({**task, "gen_params": raw_params,
                               "gen_channel": gen_channel}, prompt)
    params = submit_params(raw_params)
    if state.approval_stale(task_dir, snap):
        raise GenError("审批已作废（提示词/参数/渠道/模型在批准后变过）——重跑 render + approve")

    # 产物优先：成片已在（kill 重启后）→ 直接 done
    vf = task_dir / VIDEO_FILE
    if vf.is_file() and vf.stat().st_size > 0:
        if st == "generating":
            with guard():
                state.transition(task_dir, "done", note="成片已落盘（恢复路径）")
            print(f"[SKIP] {task['local_id']}: 成片已在（{vf.stat().st_size} 字节）→ done")
            return "done"

    secret = api_key or ""
    if st == "generating":                       # 续查路径：只查不建
        return _resume(task_dir, task, guard=guard, verbose=verbose,
                       on_progress=on_progress, api_key=api_key)

    # ---- 预检全部在写 dispatch 之前：这里出错=前置拒绝，未产生任何副作用 ----
    try:
        ch = channel(gen_channel, api_key=api_key)
        images = _submit_images(task)
        # AT-14 B3 A：提交时才装配（local 引用读本地/dataUrl 透传/老 https 经既有下载链），
        # 时序合同=必须在 _dispatch_write(prepared) 之前；GenError 仍走 _pre_reject（不扣费）。
        items = _assemble_images(images, ch["apiFormat"])
        endpoint, body = (_wan_payload(prompt, params, items, ch)
                          if ch["apiFormat"] == "wan"
                          else _sdgf_payload(prompt, params, items, ch))
    except GenError as e:
        _pre_reject(task_dir, guard, str(e), note=f"前置拒绝：{safe_error(e, secret, 200)}",
                    secret=secret)
        raise GenError(str(e)) from None

    # ---- 外呼前持久标记（跨进程守卫）：先落盘，再发请求 ----
    _dispatch_write(task_dir, guard, "prepared", note="提交意图已落盘（外呼之前）")
    _dispatch_write(task_dir, guard, "dispatched", note="已向云端创建端点发出请求")
    try:
        out = bridge.post_json(endpoint, body)
    except bridge.BridgeError as e:
        status = getattr(e, "status", None)
        payload = getattr(e, "payload", None)
        payload = payload if isinstance(payload, dict) else {}
        if str(payload.get("stage") or getattr(e, "stage", "") or "") == "host_images":
            # AT-16 B12：参考图没有公网地址 = **我们自己的**前置拒绝（本地就挡下了：
            # 渠道零请求、零扣费）→ 回 awaiting_approval 等人修图床再重试。
            # 判定只认 stage 标记、不猜文案；也**不许**记成「提交结果未知」——
            # 那会把人引导去渠道核单，而这次根本没有发出任何渠道请求。
            msg = safe_error(str(payload.get("error") or e), secret)
            tried = [str(row.get("source") or "") for row in (payload.get("sources") or [])
                     if isinstance(row, dict) and row.get("source")]
            _pre_reject(task_dir, guard, msg,
                        note="前置拒绝：参考图没有公网地址（图床不可用，零渠道请求）",
                        secret=secret,
                        extra={"asset_host": tried} if tried else None)   # 失败路径也落图源自述
            raise GenError(msg) from None
        if isinstance(status, int) and 400 <= status < 500:
            # AT-16 B11：创建阶段 4xx = 渠道/服务端明确拒绝（任务没建成，可改后直接重提）。
            # 400/409 是老的两条（预检/并发闸），403/422… 是 B11 起从渠道原码透传上来的
            # （原文标记 渠道创建端点明确拒绝 HTTP 4xx）——一律受控回退，绝不标「未知收单」。
            msg = safe_error(f"创建请求被明确拒绝（HTTP {status}）：{e}", secret)
            _pre_reject(task_dir, guard, msg, note=f"前置拒绝（HTTP {status}）：受控回退重批",
                        secret=secret)
            raise GenError(msg) from None
        _mark_submit_unknown(task_dir, guard, reason=f"提交结果未知：{e}", secret=secret)
        raise GenError(safe_error(
            f"提交结果未知（云端可能已收单，勿重复提交）：{e}", secret)) from None

    job = str(out.get("job") or "")
    carrier = str(out.get("taskId") or "")
    if ch["apiFormat"] == "wan":
        if not carrier:
            _mark_submit_unknown(task_dir, guard, reason="万相承载端点没回任务号", secret=secret)
            raise GenError("万相承载端点没回任务号——提交结果未知，已阻断")
        # 云端 id 未回执前**不进 generating**（生成态必须与确定回执同盘）；这里只记承载。
        # asset_host 如实落账：wan 是本地读字节转 dataUrl（零图床），不伪称 uguu（AT-14 B3 A7）
        _dispatch_write(task_dir, guard, "dispatched",
                        note="万相承载任务已建（等云端 id 回执）", job=job, carrier=carrier,
                        asset_host=["local-dataurl"] * len(items))
        if verbose:
            print(f"[提交] 万相承载 {carrier}（job={job}；云端 id 待回执）")
        return _poll_wan(task_dir, job=job, carrier=carrier, guard=guard, verbose=verbose,
                         on_progress=on_progress, secret=secret)

    cloud = str(out.get("taskId") or "")
    if not cloud:
        _mark_submit_unknown(task_dir, guard, reason="创建响应里没有任务 id", secret=secret)
        raise GenError("提交结果未知（创建响应没有任务 id）——已阻断，先核对渠道无单")
    # AT-14 B3 A7：服务端装配链自述的图床来源（imageSources）如实落账（同一次 guard 写盘）
    sources = out.get("imageSources")
    extra = {"asset_host": list(sources)} if isinstance(sources, list) else None
    _enter_generating(task_dir, guard, job=job, cloud=cloud, gen_task_id=cloud,
                      note="提交生成（gen_job 与云端 id 同盘）", extra=extra)
    if verbose:
        print(f"[提交] job={job} taskId={cloud}（思念云普遍 5 分钟+）")
    return _poll_sdgf(task_dir, job=job, cloud=cloud, guard=guard, verbose=verbose,
                      on_progress=on_progress, secret=secret)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="AT-5 续跑与审批")
    ap.add_argument("cmd", choices=["render", "approve", "submit"])
    ap.add_argument("local_id")
    ap.add_argument("--set", help="覆盖生成参数 duration=10,ratio=16:9,audio=false")
    ap.add_argument("--bridge")
    args = ap.parse_args(argv)
    if args.bridge:
        config.BRIDGE_BASE = args.bridge.rstrip("/")
    config.ensure_dirs()
    tdir = config.PROJECTS_DIR / args.local_id
    if not (tdir / "任务.json").is_file():
        raise SystemExit(f"找不到任务 {args.local_id}")
    try:
        if args.cmd == "render":
            do_render(tdir, args.set or "")
        elif args.cmd == "approve":
            do_approve(tdir)
        else:
            do_submit(tdir)
        return 0
    except (GenError, bridge.BridgeError, state.StateError) as e:
        print(f"[FAIL] {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
