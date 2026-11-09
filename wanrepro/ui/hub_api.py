"""自动化工作台后端（/api/hub/*）——员工中控台的桥端业务层。

设计约束（项目适配包 §7）：
- 不复制 automation 包逻辑——本模块只是 HTTP 面，动作全部转发 automation（状态机/停点守卫/幂等不重写）。
- submit 阻塞 5 分钟+ → 必须 JOBS 异步；拆解 1–3 分钟 → 同样异步。
- 进度不发明：gen_progress 只透传 SDGF progress（0–100 数字），写任务.json 供前端轮询。

本文件跑在 server.py 的请求线程/后台线程里，server 已 ROOT=wanrepro/，
automation 以子包 import（server.py 启动 cwd 不确定时由 _ensure_path 兜底）。
"""
from __future__ import annotations

import re
import sys
import threading
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from automation import cards, discuss, frames, generate, ingest, replace, state  # noqa: E402

TASKS_DIR = ROOT / "automation" / "projects"
VIDEO_FILE = "成片.mp4"

# 提交互斥：同一时刻只允许一个生成在跑（付费红线；桥端兜底，automation 内部还有并发闸）
HUB_JOBS: dict[str, dict] = {}
_SUBMIT_LOCK = threading.Lock()


class HubError(RuntimeError):
    """工作台业务错误——server 层转 4xx JSON，message 面向员工。"""


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _task_dir(local_id: str) -> Path:
    if not (local_id or "").startswith("auto-") or "/" in local_id or "\\" in local_id:
        raise HubError(f"任务号不合法: {local_id!r}")
    d = TASKS_DIR / local_id
    if not (d / "任务.json").is_file():
        raise HubError(f"任务不存在: {local_id}")
    return d


def _summary(task: dict, task_dir: Path) -> dict:
    out = (task_dir / VIDEO_FILE).is_file()
    return {
        "local_id": task["local_id"],
        "bridge_id": task.get("bridge_id"),
        "state": task["state"],
        "intent": task.get("intent", ""),
        "created_at": task.get("created_at"),
        "video_name": (task.get("ingestion") or {}).get("video_name", ""),
        "gen_progress": task.get("gen_progress"),
        "has_pending_replace": bool(task.get("pending_replace")),
        "has_output": out,
    }


def list_tasks() -> list[dict]:
    out = []
    if not TASKS_DIR.is_dir():
        return out
    for mf in sorted(TASKS_DIR.glob("auto-*/任务.json")):
        try:
            out.append(_summary(state.load(mf.parent), mf.parent))
        except Exception:  # noqa: BLE001 —— 单条坏任务不拖垮整个列表
            continue
    return list(reversed(out))  # 新任务在前


def _read(path: Path | None) -> str:
    try:
        return path.read_text(encoding="utf-8") if path and path.is_file() else ""
    except OSError:
        return ""


def _checklist_path(bridge_id: str) -> Path:
    return ROOT / "analysis" / "reverse" / bridge_id / "待确认清单.md"


def _prompt_path(task_dir: Path, task: dict) -> Path:
    return task_dir / "提示词.md"


def get_task(local_id: str) -> dict:
    task_dir = _task_dir(local_id)
    task = state.load(task_dir)
    d = _summary(task, task_dir)
    d.update({
        "summary_md": _read(ROOT / "analysis" / "reverse" / str(task.get("bridge_id")) / "拆解.md"),
        "checklist_md": _read(_checklist_path(str(task.get("bridge_id")))),
        "prompt_md": _read(_prompt_path(task_dir, task)),
        "pair_lines": task.get("pending_replace") or [],
        # AT-8 B2：置换台视觉包（合同 v3）。只读面——写路径见 set_asset。
        "asset_cards": cards.build(str(task.get("bridge_id") or ""), local_id),
        "error": (task.get("history") or [{}])[-1].get("note", "")
        if task["state"] == "failed" else "",
        "gen": task.get("gen") or {},
        "lapian_stage": task.get("lapian_stage") or "",
        "log_tail": _log_tail(task_dir),
    })
    return d


def _log_tail(task_dir: Path, n: int = 15) -> str:
    """拆解日志尾 n 行（排查用：job 结束 JOBS 清掉后，这里是唯一留痕）。"""
    log = task_dir / DISCUSS_LOG
    if not log.is_file():
        return ""
    try:
        lines = log.read_text(encoding="utf-8").splitlines()
        return "\n".join(lines[-n:])
    except OSError:
        return ""


def do_ingest(video_path: Path, intent: str) -> dict:
    """视频+意图 → 建任务（幂等键=视频内容 sha，同片重传命中同任务）。"""
    try:
        return ingest.ingest_media(video_path, intent, source_note=f"hub:{video_path.name}")
    except ingest.IngestError as e:
        raise HubError(str(e)) from None


def _spawn(job_kind: str, target, *args) -> dict:
    """统一异步：后台线程跑 target，HUB_JOBS 记状态，错误原文进 job。"""
    job_id = uuid.uuid4().hex[:12]
    HUB_JOBS[job_id] = {"status": "running", "kind": job_kind, "started": _now(),
                        "local_id": args[0] if args else None}

    def wrap():
        try:
            result = target(*args)
            HUB_JOBS[job_id].update({"status": "done", "result": result, "ended": _now()})
        except Exception as e:  # noqa: BLE001 —— 失败原文如实进 job，不吞
            HUB_JOBS[job_id].update({"status": "failed", "error": f"{e}",
                                     "stderr": traceback.format_exc()[-2000:],
                                     "ended": _now()})
    threading.Thread(target=wrap, daemon=True, name=f"hub-{job_kind}-{job_id}").start()
    return {"job_id": job_id}


DISCUSS_LOG = "拆解日志.txt"


def _stage_recorder(task_dir: Path):
    """拆解阶段记录器：每次 stage 变化 → 追加 拆解日志.txt + 回写任务.json lapian_stage。"""
    log = task_dir / DISCUSS_LOG

    def _emit(msg: str) -> None:
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
        try:
            with log.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass
        try:
            t = state.load(task_dir)
            state.save(task_dir, {**t, "lapian_stage": msg})
        except Exception:  # noqa: BLE001 —— 状态回写失败不拖垮拆解
            pass

    return _emit


def start_discuss(local_id: str) -> dict:
    """拆解（1–3 分钟）——异步，阶段变化落 拆解日志.txt + lapian_stage，前端轮询可见。"""
    task_dir = _task_dir(local_id)
    emit = _stage_recorder(task_dir)
    emit("拆解启动")

    def _run(td: Path):
        try:
            return discuss.run_discuss(td, verbose=False, on_stage=emit)
        except discuss.DiscussError as e:
            emit(f"终态：失败（{e}）")
            raise
        except Exception as e:  # noqa: BLE001
            emit(f"终态：环境故障（{e}）——状态保留，重跑续")
            raise
        else:
            emit("终态：awaiting_assets（等置换）")

    return _spawn("hub-discuss", _run, task_dir)


def do_assets(local_id: str, image_paths: list[Path]) -> dict:
    """替换图上传回填（上传图床+回填清单+写待确认）。返回配对回显。"""
    task_dir = _task_dir(local_id)
    try:
        run = replace.run_replace(task_dir, assets=[str(p) for p in image_paths],
                                  map_spec=None, name_spec=None, confirm=False, verbose=False)
    except replace.ReplaceError as e:
        raise HubError(str(e)) from None
    task = state.load(task_dir)
    return {"state": run, "filled": task.get("pending_replace") or []}


def do_confirm(local_id: str) -> dict:
    task_dir = _task_dir(local_id)
    try:
        st = replace.run_replace(task_dir, assets=None, map_spec=None, name_spec=None,
                                 confirm=True, verbose=False)
    except replace.ReplaceError as e:
        raise HubError(str(e)) from None
    return {"state": st}


def _bridge_of(task_dir: Path) -> str:
    bridge = str((state.load(task_dir) or {}).get("bridge_id") or "").strip()
    if not bridge:
        raise HubError(f"任务 {task_dir.name} 还没拆解（没有 bridge_id），没有清单可写")
    return bridge


def _record_pending(task_dir: Path, n: int, *, image_url: str | None,
                    clear: bool, text: str | None, desc: str) -> None:
    """`pending_replace` = 本次会话上传流水（语义保留：只记上传，不参与回显）。

    置换台逐卡上传同样要落这条流水，否则确认（rendering 守卫要 assets_confirmed）无据可依；
    「沿用/清空」是撤掉该行的流水，不是新增一条。
    """
    task = state.load(task_dir)
    pending = [dict(item) for item in (task.get("pending_replace") or []) if isinstance(item, dict)]
    before = [dict(item) for item in pending]
    if clear:
        pending = [item for item in pending if item.get("no") != n]
    elif image_url:
        entry = next((item for item in pending if item.get("no") == n), None)
        if entry is None:
            entry = {"no": n, "img": "", "url": "", "name": "", "desc": str(desc or "")[:40]}
            pending.append(entry)
        entry["url"] = image_url
    elif text is not None:
        entry = next((item for item in pending if item.get("no") == n), None)
        if entry is not None:
            entry["name"] = text
    if pending == before:
        return
    pending.sort(key=lambda item: item.get("no") or 0)
    state.save(task_dir, {**task, "pending_replace": pending})


def set_asset(local_id: str, n: int, *, image_url: str | None = None,
              clear: bool = False, text: str | None = None) -> dict:
    """POST /api/hub/asset —— 置换台逐卡定向写（数据合同 v3 写合同）。

    {task_id, n, image_url}   重传 = 覆盖该行资产图（fill_row 覆盖模式，不追加）
    {task_id, n, clear: true} 「沿用」= 清空该行绑定
    {task_id, n, text}        品牌视觉卡的文字，写清单「命名」列
    回显真源是清单 md（不是 pending_replace）。
    """
    task_dir = _task_dir(local_id)
    bridge = _bridge_of(task_dir)
    if image_url and clear:
        raise HubError("image_url 与 clear 只能给一个")
    if not image_url and not clear and text is None:
        raise HubError("请求要有 image_url / clear:true / text 之一")
    try:
        if image_url:
            url = str(image_url).strip()
            if not url.startswith("https://"):
                raise HubError(f"image_url 必须是公网 HTTPS（SDGF 只收公网图）：{image_url!r}")
            row = replace.set_row(bridge, n, image_urls=[url], overwrite=True)
        elif clear:
            row = replace.set_row(bridge, n, image_urls=[], overwrite=True)
        else:
            row = replace.set_row(bridge, n, name=re.sub(r"\s+", " ", str(text)).strip())
    except replace.ReplaceError as e:
        raise HubError(str(e)) from None
    _record_pending(task_dir, n, image_url=(str(image_url).strip() if image_url else None),
                    clear=clear, text=text, desc=row.get("desc") or "")
    return {"ok": True, "task_id": local_id, "n": n,
            "已传图": cards.split_urls(row.get("cur_assets") or ""),
            "命名": row.get("name") or ""}


def frame_file(local_id: str, filename: str) -> Path:
    """置换台帧图（/api/hub/frame/<task>/<文件>）→ 磁盘路径；文件不在/名字不合法 → HubError。"""
    task_dir = _task_dir(local_id)
    bridge = _bridge_of(task_dir)
    try:
        return frames.resolve(bridge, filename)
    except frames.FrameError as e:
        raise HubError(str(e)) from None


def do_render(local_id: str) -> dict:
    """渲染（秒级，同步）→ 待审批 + 预演参数 + 提示词全文。"""
    task_dir = _task_dir(local_id)
    try:
        prev = generate.do_render(task_dir, "5,9:16", verbose=False)
    except generate.GenError as e:
        raise HubError(str(e)) from None
    task = state.load(task_dir)
    return {"state": task["state"], "preview": prev,
            "prompt_md": _read(_prompt_path(task_dir, task))}


def do_approve(local_id: str) -> dict:
    task_dir = _task_dir(local_id)
    try:
        st = generate.do_approve(task_dir, verbose=False)
    except generate.GenError as e:
        raise HubError(str(e)) from None
    return {"state": st}


def start_submit(local_id: str) -> dict:
    """真提交（付费，前端只在「批准」后调用）——异步，进度写任务.json gen_progress。"""
    task_dir = _task_dir(local_id)
    task = state.load(task_dir)
    if task["state"] != "approved":
        raise HubError(f"提交需 approved（先批准），当前 {task['state']}")

    def _run(td: Path):
        with _SUBMIT_LOCK:
            def _progress(pct, waited):  # noqa: ANN001 —— generate 回调签名
                if pct is None:
                    return
                t = state.load(td)
                state.save(td, {**t, "gen_progress": int(pct)})
            return generate.do_submit(td, verbose=False, on_progress=_progress)

    return _spawn("hub-submit", _run, task_dir)


def output_file(local_id: str) -> Path:
    task_dir = _task_dir(local_id)
    f = task_dir / VIDEO_FILE
    if not f.is_file():
        raise HubError(f"成片未就绪: {local_id}")
    return f


def job_status(job_id: str) -> dict:
    rec = HUB_JOBS.get(job_id)
    if not rec:
        raise HubError(f"job 不存在: {job_id}")
    return rec
