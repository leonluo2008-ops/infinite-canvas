"""自动化工作台后端（/api/hub/*）——员工中控台的桥端业务层。

设计约束（项目适配包 §7）：
- 不复制 automation 包逻辑——本模块只是 HTTP 面，动作全部转发 automation（状态机/停点守卫/幂等不重写）。
- submit 阻塞 5 分钟+ → 必须 JOBS 异步；拆解 1–3 分钟 → 同样异步。
- 进度不发明：gen_progress 只透传 SDGF progress（0–100 数字），写任务.json 供前端轮询。

本文件跑在 server.py 的请求线程/后台线程里，server 已 ROOT=wanrepro/，
automation 以子包 import（server.py 启动 cwd 不确定时由 _ensure_path 兜底）。
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import threading
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from automation import cards, discuss, frames, generate, ingest, replace, selfcheck, state  # noqa: E402

TASKS_DIR = ROOT / "automation" / "projects"
VIDEO_FILE = "成片.mp4"
UPLOADS_DIR = ROOT / "uploads"
TOMBSTONES = TASKS_DIR / ".tombstones.json"     # 删除墓碑（数据合同 B0）

# 提交互斥：同一时刻只允许一个生成在跑（付费红线；桥端兜底，automation 内部还有并发闸）
HUB_JOBS: dict[str, dict] = {}
_SUBMIT_LOCK = threading.Lock()

# 任务级串行锁（AT-9 §六层3）：同一任务的写路径（贴图/成组/解组/确认/回退/删除）串行，
# 不同任务互不阻塞。锁只在 server 进程内有效（server 是这些写路径的唯一 HTTP 面）。
_TASK_LOCKS: dict[str, threading.Lock] = {}
_TASK_LOCKS_GUARD = threading.Lock()


def _task_lock(local_id: str) -> threading.Lock:
    with _TASK_LOCKS_GUARD:
        return _TASK_LOCKS.setdefault(local_id, threading.Lock())


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


def _intent_mod():
    """按路径加载 pipeline/reverse/intent.py（它不是包，同 cards._confirm 的加载法）。

    建议合同（AT-9 B1）：意图建议的结构化解析**复用 intent.py 规范解析**（parse_suggestions
    三节校验/全半角竖线/行号与镜次校验都有），禁新写解析器。
    """
    import importlib.util
    path = ROOT / "pipeline" / "reverse" / "intent.py"
    spec = importlib.util.spec_from_file_location("wanrepro_reverse_intent", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _replaced_count(bridge_id: str) -> str:
    """已换数=已传图行数/必换行数（数据合同：实时计算，不落盘）；清单还没有（未拆解）→ 空串。"""
    if not bridge_id:
        return ""
    path = ROOT / "analysis" / "reverse" / bridge_id / "待确认清单.md"
    if not path.is_file():
        return ""
    rows = replace.parse_rows(path.read_text(encoding="utf-8-sig", errors="replace"))
    if not rows:
        return ""
    filled = sum(1 for r in rows if (r.get("cur_assets") or "").strip())
    must = sum(1 for r in rows if "必须换" in (r.get("advice") or ""))
    return f"{filled}/{must}"


def _summary(task: dict, task_dir: Path) -> dict:
    out = (task_dir / VIDEO_FILE).is_file()
    # AT-9 B1 表格摘要三字段（数据合同）：任务名/阶段/已换数——列表页不拉详情
    task_name = f"{task['local_id']}·{(task.get('ingestion') or {}).get('video_name', '')}"
    stage = str(task.get("lapian_stage") or "") if task["state"] == "discussing" else ""
    return {
        "local_id": task["local_id"],
        "bridge_id": task.get("bridge_id"),
        "task_name": task_name,
        "stage": stage,
        "replaced": _replaced_count(str(task.get("bridge_id") or "")),
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


SUGGESTION_NAME = "意图建议.md"


def _suggestions(raw: str, checklist_md: str, disc_md: str) -> list[dict]:
    """AT-9 B1 建议合同：意图建议.md 原文 → intent.py 规范解析 → 按当前清单行透出。

    [{n, 档位, 理由, 镜次, 待核对}]：清单每一行一条。缺文件（raw 空）/ 解析失败 / 行号与
    当前清单对不上 → 该行 待核对=True 且 档位 恒空（**禁自动归「可沿用」**——那是替换判断，
    只能人给）。行号交叉核对、镜次校验、三节完整性全部复用 intent.parse_suggestions，这里只做编排。
    """
    rows = replace.parse_rows(checklist_md) if checklist_md.strip() else []
    items: dict[int, dict] = {}
    if raw.strip() and rows and disc_md.strip():
        intent = _intent_mod()
        try:
            normalized, _inv, _dup, _bad = intent.parse_suggestions(raw, disc_md, checklist_md)
        except intent.IntentError:
            normalized = ""                       # 解析失败 → 全部行待核对，不猜
        for section, lines in intent._section_bodies(normalized).items():
            for line in lines:
                match = intent.ITEM_RE.match(line)
                if not match:
                    continue
                n = int(match.group("row"))
                items[n] = {"n": n, "档位": section, "理由": match.group("reason"),
                            "镜次": [int(x) for x in re.findall(r"\d+", match.group("shots"))],
                            "待核对": False}
    return [items.get(row["no"])
            or {"n": row["no"], "档位": "", "理由": "", "镜次": [], "待核对": True}
            for row in sorted(rows, key=lambda r: r["no"])]


def _checklist_path(bridge_id: str) -> Path:
    return ROOT / "analysis" / "reverse" / bridge_id / "待确认清单.md"


def _prompt_path(task_dir: Path, task: dict) -> Path:
    return task_dir / "提示词.md"


def get_task(local_id: str) -> dict:
    task_dir = _task_dir(local_id)
    task = state.load(task_dir)
    d = _summary(task, task_dir)
    summary_md = _read(ROOT / "analysis" / "reverse" / str(task.get("bridge_id")) / "拆解.md")
    checklist_md = _read(_checklist_path(str(task.get("bridge_id"))))
    d.update({
        "summary_md": summary_md,
        "checklist_md": checklist_md,
        "prompt_md": _read(_prompt_path(task_dir, task)),
        # AT-9 B5 自检合同：渲染完成时落盘的 自检.json 三条 {项,结论,证据}；无文件 → None（前端「未检查」）
        "自检": selfcheck.load_selfcheck(task_dir),
        "pair_lines": task.get("pending_replace") or [],
        # AT-8 B2：置换台视觉包（合同 v3）。只读面——写路径见 set_asset。
        "asset_cards": cards.build(str(task.get("bridge_id") or ""), local_id),
        # AT-9 B1 建议合同：意图建议结构化透出（复用 intent.py 解析，缺/坏/行号不匹配 → 待核对）
        "suggestions": _suggestions(
            _read(ROOT / "analysis" / "reverse" / str(task.get("bridge_id") or "") / SUGGESTION_NAME),
            checklist_md, summary_md),
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
                        "local_id": _job_local_id(args[0]) if args else None}

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


def _job_local_id(value) -> str:
    """删除守护按 local_id 字符串匹配排队 JOB；实参是任务目录 Path 时取目录名（B5 修正：原先存 Path
    与字符串永不相等，运行中 JOB 的删除守护对真实后台任务形同虚设）。"""
    return Path(value).name if isinstance(value, Path) else str(value)


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


def _spawn_discuss(task_dir: Path, first_line: str) -> dict:
    """拆解后台执行体（发起拆解与 failed 重试共用；重跑即续语义全在 discuss.run_discuss）。"""
    emit = _stage_recorder(task_dir)
    emit(first_line)

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


def start_discuss(local_id: str) -> dict:
    """拆解（1–3 分钟）——异步，阶段变化落 拆解日志.txt + lapian_stage，前端轮询可见。"""
    return _spawn_discuss(_task_dir(local_id), "拆解启动")


def start_retry(local_id: str) -> dict:
    """POST /api/hub/task/{id}/retry —— failed 态续跑入口（§四：重试/环境级故障重跑即续）。

    复用既有重跑即续语义：discuss.run_discuss 对 failed 先做 failed→discussing（状态机
    唯一合法出口），随后产物优先——拆解产物齐就不重跑模型直接落 awaiting_assets，
    未齐才真重发拆解。非 failed → 409（HubError 由 server 层转 409）。
    """
    task_dir = _task_dir(local_id)
    task = state.load(task_dir)
    if task["state"] != "failed":
        raise HubError(f"重试需 failed 状态，当前 {task['state']}（只有失败任务可重试）")
    return _spawn_discuss(task_dir, "人工重试：failed → discussing（重跑即续，产物优先）")


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


def do_confirm(local_id: str, empty_confirm: bool = False,
               ack_rows: list[int] | None = None) -> dict:
    """确认置换 → rendering，并后台续跑渲染（§五：确认即推进，前端关页不负责执行）。

    empty_confirm=true = 全沿用（空置换显式确认，AT-9 §四）。
    ack_rows = 确认时点「知道，按原样」勾选的未换行号——落任务.json『按原样确认』，
    是自检③锁点对账的确认时点记录（不自证）。
    """
    task_dir = _task_dir(local_id)
    with _task_lock(local_id):
        try:
            st = replace.run_replace(task_dir, assets=None, map_spec=None, name_spec=None,
                                     confirm=True, verbose=False,
                                     empty_confirm=empty_confirm, ack_rows=ack_rows)
        except replace.ReplaceError as e:
            raise HubError(str(e)) from None
    # 确认后由后端触发渲染（AT-9 §五「确认后立即关闭 Drawer，渲染照常推进（后端触发）」）：
    # rendering 态下后台跑 do_render → awaiting_approval + 自检；失败如实落 failed（可重试/删除）。
    job = _spawn("hub-render", _render_and_advance, task_dir)
    return {"state": st, "全沿用": bool(empty_confirm), "job_id": job.get("job_id")}


def _render_and_advance(task_dir: Path) -> dict:
    """后台渲染推进体：rendering → awaiting_approval（自检在 do_render 内自动跑）。

    渲染失败 → 任务级失败如实落 failed（带原文），绝不把任务吊死在 rendering
    （RUNNING_STATES 不可删不可重试，吊死即死锁）。
    """
    try:
        return do_render(task_dir.name)
    except Exception as e:  # noqa: BLE001
        try:
            if state.load(task_dir)["state"] == "rendering":
                state.transition(task_dir, "failed", note=f"渲染失败（原文）：{e}")
        except Exception:  # noqa: BLE001 —— 状态回写失败不吞掉原异常
            pass
        raise


def do_rollback(local_id: str) -> dict:
    """退回置换（AT-9 受控回退边）：awaiting_approval → awaiting_assets，丢弃当前提示词。"""
    task_dir = _task_dir(local_id)
    with _task_lock(local_id):
        try:
            task = state.rollback_to_assets(task_dir, note="退回置换（丢弃当前提示词）")
        except state.StateError as e:
            raise HubError(str(e)) from None
    return {"state": task["state"], "note": "退回置换（丢弃当前提示词）"}


# ---------------------------------------------------------------- 删除端点（AT-9 §六删除合同）

DELETABLE_STATES = ("ingested", "failed", "done")             # 免确认就能删的 idle 态（done=状态机真实终态；「completed」不是状态机状态，2026-11-09 B5 修正）
TEARDOWN_IDLE_STATES = ("awaiting_assets", "awaiting_approval")  # 拆解过但 idle：要任务名确认
RUNNING_STATES = ("ingesting", "discussing", "rendering", "generating")  # 一律 409


def _tombstones_load() -> list[dict]:
    try:
        data = json.loads(TOMBSTONES.read_text(encoding="utf-8-sig"))
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _tombstones_append(local_id: str) -> None:
    items = _tombstones_load()
    items.append({"id": local_id, "时间": _now()})
    tmp = TOMBSTONES.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(TOMBSTONES)


def _bridge_still_referenced(bridge_id: str, exclude: str) -> bool:
    """bridge_id 是否还有别的任务在用（读不了的坏任务按「在用」算——宁可留着不可错删）。"""
    for mf in TASKS_DIR.glob("auto-*/任务.json"):
        if mf.parent.name == exclude:
            continue
        try:
            if str(json.loads(mf.read_text(encoding="utf-8")).get("bridge_id") or "") == bridge_id:
                return True
        except (OSError, json.JSONDecodeError):
            return True
    return False


def _uploads_video_shared(video_name: str, exclude: str) -> bool:
    """uploads/<片名> 是否被别的任务同内容引用（同 sha 才算共享——同片名不同内容不背锅）。"""
    f = UPLOADS_DIR / video_name
    if not f.is_file():
        return False
    sha = ingest._sha256_file(f)
    for mf in TASKS_DIR.glob("auto-*/inbox/*"):
        if mf.parent.parent.name == exclude:
            continue
        try:
            if mf.is_file() and ingest._sha256_file(mf) == sha:
                return True
        except OSError:
            continue
    return False


def delete_task(local_id: str, *, confirm_name: str | None = None) -> dict:
    """POST /api/hub/task/{id}/delete —— AT-9 §六删除合同。

    仅 idle 态可删：ingested/failed/done 免确认；拆解过但 idle
    （awaiting_assets/awaiting_approval）请求体必须带 confirm_name=任务号（或任务名）。
    运行中（ingesting/discussing/rendering/generating）与排队 JOB → 409。
    删除范围=projects/<id>/ + 专属 analysis/reverse/<bridge_id>/（无其他任务引用才删）
    + uploads/<参考片>（无其他任务同内容才删）；写墓碑 projects/.tombstones.json。
    """
    task_dir = _task_dir(local_id)
    task = state.load(task_dir)
    st = task["state"]
    running_job = next((rec for rec in HUB_JOBS.values()
                        if rec.get("local_id") == local_id and rec.get("status") == "running"),
                       None)
    if running_job:
        raise HubError(f"任务有排队/运行中的 job（{running_job.get('kind')}），删除拒绝")
    if st in RUNNING_STATES:
        raise HubError(f"任务运行中（{st}），删除拒绝（等跑完或失败后再删）")
    if st not in DELETABLE_STATES + TEARDOWN_IDLE_STATES:
        raise HubError(f"状态 {st} 不可删除（仅 {DELETABLE_STATES} 免确认；"
                       f"{TEARDOWN_IDLE_STATES} 需任务名确认）")
    task_name = f"{local_id}·{(task.get('ingestion') or {}).get('video_name', '')}"
    if st in TEARDOWN_IDLE_STATES and (confirm_name or "").strip() not in {local_id, task_name}:
        raise HubError(f"拆解过的任务删除需任务名确认：请求体带 confirm_name="
                       f"\"{local_id}\"（或完整任务名「{task_name}」）")
    bridge_id = str(task.get("bridge_id") or "")
    video_name = str((task.get("ingestion") or {}).get("video_name") or "")
    with _task_lock(local_id):
        shutil.rmtree(task_dir)
        removed_analysis = False
        if bridge_id and not _bridge_still_referenced(bridge_id, exclude=local_id):
            target = ROOT / "analysis" / "reverse" / bridge_id
            if target.is_dir():
                shutil.rmtree(target)
                removed_analysis = True
        removed_upload = False
        safe_video = video_name and "/" not in video_name and "\\" not in video_name
        if safe_video:
            f = UPLOADS_DIR / video_name
            if f.is_file() and not _uploads_video_shared(video_name, exclude=local_id):
                f.unlink()
                removed_upload = True
        _tombstones_append(local_id)
    return {"ok": True, "deleted": local_id, "state_was": st,
            "analysis_dir_removed": removed_analysis,
            "uploads_video_removed": removed_upload,
            "tombstone": True}


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
    该行在确认组里（sidecar『同实体确认』）→ 走 replace.set_group_write 单点整组联动；
    疑似（同实体疑似）**不联动**——单行照常，别的行一个字不动。
    """
    task_dir = _task_dir(local_id)
    bridge = _bridge_of(task_dir)
    if image_url and clear:
        raise HubError("image_url 与 clear 只能给一个")
    if not image_url and not clear and text is None:
        raise HubError("请求要有 image_url / clear:true / text 之一")
    url = None
    if image_url:
        url = str(image_url).strip()
        if not url.startswith("https://"):
            raise HubError(f"image_url 必须是公网 HTTPS（SDGF 只收公网图）：{image_url!r}")
    with _task_lock(local_id):
        try:
            members = replace.group_members(bridge, n)
            if len(members) >= 2:
                out = replace.set_group_write(task_dir, n, image_url=url,
                                              clear=clear, text=text)
                return {"ok": True, "task_id": local_id, "n": n, "组": out["members"],
                        "rows": [{"n": r["no"],
                                  "已传图": cards.split_urls(r.get("cur_assets") or ""),
                                  "命名": r.get("name") or ""} for r in out["rows"]]}
            if url:
                row = replace.set_row(bridge, n, image_urls=[url], overwrite=True)
            elif clear:
                row = replace.set_row(bridge, n, image_urls=[], overwrite=True)
            else:
                row = replace.set_row(bridge, n, name=re.sub(r"\s+", " ", str(text)).strip())
            _record_pending(task_dir, n, image_url=url, clear=clear, text=text,
                            desc=row.get("desc") or "")
        except replace.ReplaceError as e:
            raise HubError(str(e)) from None
    return {"ok": True, "task_id": local_id, "n": n,
            "已传图": cards.split_urls(row.get("cur_assets") or ""),
            "命名": row.get("name") or ""}


# ---------------------------------------------------------------- 确认组端点（AT-9 B2）

def group_rows(local_id: str, rows: list[int]) -> dict:
    """成组：校验行号在清单、≥2 行、都不在别的组 → 写 sidecar『同实体确认』。"""
    task_dir = _task_dir(local_id)
    bridge = _bridge_of(task_dir)
    rows = sorted({int(x) for x in rows})
    if len(rows) < 2:
        raise HubError(f"成组至少要 2 行（收到 {rows}）")
    with _task_lock(local_id):
        try:
            checklist = replace.checklist_path(bridge).read_text(encoding="utf-8-sig")
        except replace.ReplaceError as e:
            raise HubError(str(e)) from None
        nos = {r["no"] for r in replace.parse_rows(checklist)}
        missing = [n for n in rows if n not in nos]
        if missing:
            raise HubError(f"行号不在清单里: {missing}（现有 {sorted(nos)}）")
        groups = replace.load_groups(bridge)
        for g in groups:
            overlap = sorted(set(rows) & set(g))
            if overlap:
                raise HubError(f"行 {overlap} 已在组 {g} 里（先解组再成组）")
        saved = replace.save_groups(bridge, groups + [rows])
        return {"ok": True, "task_id": local_id, "本次": rows, "组": saved}


def ungroup_rows(local_id: str, rows: list[int]) -> dict:
    """解组：把行从所在确认组摘出（<2 行的组剔除）；没组里的行如实报。"""
    task_dir = _task_dir(local_id)
    bridge = _bridge_of(task_dir)
    rows = sorted({int(x) for x in rows})
    if not rows:
        raise HubError("解组要给行号（rows）")
    with _task_lock(local_id):
        groups = replace.load_groups(bridge)
        loose = [n for n in rows if not any(n in g for g in groups)]
        if loose:
            raise HubError(f"行 {loose} 不在任何确认组里")
        rest = [[n for n in g if n not in rows] for g in groups]
        saved = replace.save_groups(bridge, rest)
        return {"ok": True, "task_id": local_id, "解组": rows, "组": saved}


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
