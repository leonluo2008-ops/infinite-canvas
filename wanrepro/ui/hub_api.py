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
import os
import re
import shutil
import sys
import threading
import traceback
import urllib.parse
import uuid
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from automation import cards, config as auto_config, discuss, frames, generate, ingest, replace, selfcheck, state  # noqa: E402

# 任务目录根的**唯一来源** = automation.config.PROJECTS_DIR（认 WANREPRO_AUTO_DIR）。
# 禁止在这里另写一个 ROOT/"automation"/"projects"：硬编码会让「设 env 起隔离实例」变成
# 读真实目录/写隔离目录的伪隔离，探针按隔离目录首投得到的 id 去删真实任务（AT-14 B2 P0，
# 真实任务 auto-0001 已被误删一次）。
TASKS_DIR = auto_config.PROJECTS_DIR
VIDEO_FILE = "成片.mp4"
UPLOADS_DIR = ROOT / "uploads"
TOMBSTONES = TASKS_DIR / ".tombstones.json"     # 删除墓碑（数据合同 B0）

# 提交互斥：同一时刻只允许一个生成在跑（付费红线；桥端兜底，automation 内部还有并发闸）
HUB_JOBS: dict[str, dict] = {}
_SUBMIT_LOCK = threading.Lock()

# 任务级串行锁（AT-9 §六层3）：同一任务的写路径（贴图/成组/解组/确认/回退/删除）串行，
# 不同任务互不阻塞。锁只在 server 进程内有效（server 是这些写路径的唯一 HTTP 面）。
# AT-13 §2.7：**普通 Lock（非重入）**，由 `task_lock()` 这一公共入口统一持有；
# runner/CLI 是独立进程，线程锁不跨进程——跨进程守卫靠任务.json 上的磁盘标记（B2 落）。
_TASK_LOCKS: dict[str, threading.Lock] = {}
_TASK_LOCKS_GUARD = threading.Lock()
_TASK_LOCK_HELD = threading.local()      # 线程内已持有的任务号集合（同一线程重入直通，防自锁）


def _task_lock(local_id: str) -> threading.Lock:
    """取任务锁对象（**内部用**；持锁一律走 task_lock()，别直接 with 它）。"""
    with _TASK_LOCKS_GUARD:
        return _TASK_LOCKS.setdefault(local_id, threading.Lock())


@contextmanager
def task_lock(local_id: str):
    """任务级锁唯一公共入口（AT-13 §2.7 锁持有规则）。

    - 请求线程**短持**：校验状态 → 冻结配置 → 注册提交占用后立刻释放，不持锁等全局
      提交槽、也不持锁等后台线程跑完；
    - 后台线程写任务文件时短持同一把锁（进度回写也走这条，防 read-modify-save 覆盖）；
    - 同一线程重复进入=直通（底层是普通非重入 Lock，不加这层会自锁）；跨线程仍严格互斥。
    """
    held = getattr(_TASK_LOCK_HELD, "held", None)
    if held is None:
        held = _TASK_LOCK_HELD.held = set()
    if local_id in held:
        yield
        return
    lock = _task_lock(local_id)
    lock.acquire()
    held.add(local_id)
    try:
        yield
    finally:
        held.discard(local_id)
        lock.release()


def _write_task(task_dir: Path, mutate):
    """任务文件「读-改-写」短持改写（AT-13 §2.7：后台进度/阶段回写不覆盖别人刚写的状态）。

    mutate(task) 返回 False = 不改（不落盘）；其余返回值忽略。返回改写后的任务 dict。
    """
    with task_lock(task_dir.name):
        task = state.load(task_dir)
        if mutate(task) is False:
            return task
        state.save(task_dir, task)
        return task


class HubError(RuntimeError):
    """工作台业务错误——server 层转 4xx JSON，message 面向员工。status=HTTP 状态码。"""

    status = 409


class HubInputError(HubError):
    """请求内容不合法（配置缺字段/值域外/带敏感键）——400，不是状态冲突。"""

    status = 400


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


def where() -> dict:
    """数据根自述（诊断面，`GET /api/hub/where`）——探针跑前用它证明「读写同源」。

    AT-14 B2 P0：TASKS_DIR 曾硬编码，隔离实例读真实/写隔离，探针误删真实任务。
    这里把服务端**实际用于读写任务**的目录原样报出，让探针能断言
    「服务端 TASKS_DIR == 我的 PROJECTS_DIR」，不一致就拒跑。
    """
    return {
        "tasks_dir": str(Path(TASKS_DIR).resolve()),
        "tasks_dir_raw": str(TASKS_DIR),
        "auto_dir_env": os.environ.get("WANREPRO_AUTO_DIR"),
        "uploads_dir": str(UPLOADS_DIR.resolve()),
        "tombstones": str(TOMBSTONES),
        "pid": os.getpid(),
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


def _active_submit(local_id: str) -> bool:
    """该任务是否有活跃提交占用（HUB_JOBS 运行中）——孤儿三步判定第①步的数据源。"""
    return any(rec.get("local_id") == local_id and rec.get("status") == "running"
               for rec in list(HUB_JOBS.values()))


def get_task(local_id: str) -> dict:
    task_dir = _task_dir(local_id)
    task = state.load(task_dir)
    d = _summary(task, task_dir)
    bridge_id = str(task.get("bridge_id") or "")
    decisions = replace.load_row_decisions(bridge_id) if bridge_id else {}
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
        # AT-12 N1 行决策两集合（服务端真源）：按原片行 = keep_origin ∪ dismissed（置换区折叠归此），
        # 已移除行 = dismissed（AT-12 语义「不需要替换→移除」，留痕供恢复）；逐卡真值仍在 asset_cards[].决策。
        "按原片行": sorted(decisions),
        "已移除行": sorted(n for n, value in decisions.items() if value == "dismissed"),
        # AT-9 B1 建议合同：意图建议结构化透出（复用 intent.py 解析，缺/坏/行号不匹配 → 待核对）
        "suggestions": _suggestions(
            _read(ROOT / "analysis" / "reverse" / str(task.get("bridge_id") or "") / SUGGESTION_NAME),
            checklist_md, summary_md),
        "error": (task.get("history") or [{}])[-1].get("note", "")
        if task["state"] == "failed" else "",
        "gen": task.get("gen") or {},
        "lapian_stage": task.get("lapian_stage") or "",
        "log_tail": _log_tail(task_dir),
        # ---- AT-13 B2 提交链透出（§2.4 孤儿三步）——只读，不在读路径上迁移状态 ----
        "gen_error": task.get("gen_error") or "",
        "submit_unknown": task.get("submit_unknown") or None,
        "dispatch": task.get("dispatch") or None,
        "cloud_task_id": task.get("cloud_task_id") or "",
        "active_submit": _active_submit(local_id),
        "submit_lost": generate.diagnose(task_dir, task,
                                         active_submit=_active_submit(local_id)),
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
    """统一异步：后台线程跑 target，HUB_JOBS 记状态，错误原文进 job。

    AT-13 §2.8：job 记录是**可 GET 的落账面**（/api/hub/job/<id>）——错误文本与 traceback
    必须脱敏后再存，traceback 不许裸存（R1 实证裸 traceback 会把 key 原样带出去）。
    """
    job_id = uuid.uuid4().hex[:12]
    HUB_JOBS[job_id] = {"status": "running", "kind": job_kind, "started": _now(),
                        "local_id": _job_local_id(args[0]) if args else None}

    def wrap():
        try:
            result = target(*args)
            HUB_JOBS[job_id].update({"status": "done", "result": result, "ended": _now()})
        except Exception as e:  # noqa: BLE001 —— 失败原文如实进 job，不吞
            HUB_JOBS[job_id].update({"status": "failed",
                                     "error": generate.safe_error(f"{e}", "", 2000),
                                     "stderr": generate.safe_error(traceback.format_exc(), "", 2000),
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
            _write_task(task_dir, lambda t: t.__setitem__("lapian_stage", msg))
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
    with task_lock(local_id):
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
    with task_lock(local_id):
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
    with task_lock(local_id):
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


def _must_rows(bridge: str) -> set[int]:
    """清单必换行集合（锁点计算用；清单读不了 → 空集）。"""
    try:
        md = replace.checklist_path(bridge).read_text(encoding="utf-8-sig")
    except (replace.ReplaceError, OSError):
        return set()
    return {int(r["no"]) for r in replace.parse_rows(md) if "必须换" in (r.get("advice") or "")}


def _row_has_image(bridge: str, no: int) -> bool:
    """清单该行「资产图」列是否非空（真源=清单 md，不是 pending 流水）。"""
    try:
        md = replace.checklist_path(bridge).read_text(encoding="utf-8-sig")
    except (replace.ReplaceError, OSError):
        return False
    row = next((r for r in replace.parse_rows(md) if r["no"] == no), None)
    return bool(row and (row.get("cur_assets") or "").strip())


def lock_remaining(bridge: str) -> list[int]:
    """锁点剩余（AT-11 §四）：必换行 −(已传图行 ∪ 组已传图的组成员 ∪ 按原片行)。

    必换行 ∈ 组且组已传图 → 整组被那张图换掉，不锁；行有决策（keep_origin / AT-12 dismissed）
    → 按原片，不锁。返回仍锁着的必换行号（前端「锁点剩余」徽标与 set_asset 回显共用此口径）。
    """
    must = _must_rows(bridge)
    if not must:
        return []
    free: set[int] = set(replace.load_row_decisions(bridge))
    groups = replace.load_groups(bridge)
    for no in sorted(must):
        if _row_has_image(bridge, no):
            free.add(no)
    for group in groups:
        if any(_row_has_image(bridge, n) for n in group):
            free |= set(group)
    return sorted(must - free)


def set_asset(local_id: str, n: int, *, image_url: str | None = None,
              clear: bool = False, text: str | None = None,
              decision: str | None = None) -> dict:
    """POST /api/hub/asset —— 置换台逐卡定向写（数据合同 v3 写合同 + AT-11 §3.5 扩展）。

    {task_id, n, image_url}     重传 = 覆盖该行资产图（fill_row 覆盖模式，不追加）
    {task_id, n, clear: true}   「沿用」= 清空该行绑定
    {task_id, n, text}          品牌视觉卡的文字，写清单「命名」列
    {task_id, n, decision}      AT-11/AT-12 行决策：keep_origin（这行不换了，按原片）/
                                dismissed（已移除，不需要替换，按原片处理）/ restore（恢复，清键）
    回显真源是清单 md（不是 pending_replace）。
    该行在确认组里（sidecar『同实体确认』）→ 图/文走 replace.set_group_write 单点整组联动；
    decision 走行级路径且**拒绝组内行**（组=原子单位，组内单行降级先拆开——AT-11 §3.2）；
    decision 需 awaiting_assets（确认后覆盖表锁定不可改——B2 次序合同）；
    疑似（同实体疑似）**不联动**——单行照常，别的行一个字不动。

    B5 复用说明：不新建端点——既有 set_asset 已有任务锁/行号校验/前端 postHubAsset 传输层；
    既有 clear 只清图片列+流水、无恢复语义与行决策状态，故加 decision 参数而非塞 clear。
    """
    task_dir = _task_dir(local_id)
    bridge = _bridge_of(task_dir)
    if decision is not None:
        if decision not in ("keep_origin", "dismissed", "restore"):
            raise HubError(f"decision 只能是 keep_origin / dismissed / restore，收到 {decision!r}")
        if image_url or clear or text is not None:
            raise HubError("decision 与 image_url / clear / text 互斥，只能给一个")
    elif image_url and clear:
        raise HubError("image_url 与 clear 只能给一个")
    if not image_url and not clear and text is None and decision is None:
        raise HubError("请求要有 image_url / clear:true / text / decision 之一")
    url = None
    if image_url:
        url = str(image_url).strip()
        if not url.startswith("https://"):
            raise HubError(f"image_url 必须是公网 HTTPS（SDGF 只收公网图）：{image_url!r}")
    with task_lock(local_id):
        if decision is not None:
            # ---- AT-11 §3.2/§3.5：单行降级/恢复（行决策覆盖） ----
            task = state.load(task_dir)
            if task["state"] != "awaiting_assets":
                raise HubError(f"行决策需 awaiting_assets（确认后覆盖表锁定不可改），"
                               f"当前 {task['state']}")
            members = replace.group_members(bridge, n)
            if len(members) >= 2:
                raise HubError(f"第 {n} 行在确认组 {members} 里（组=原子单位，"
                               f"不可单行降级，先拆开合并）")
            decisions = replace.load_row_decisions(bridge)
            if decision in replace.ORIGIN_DECISIONS:
                decisions[n] = decision
            else:
                if decisions.pop(n, None) is None:
                    raise HubError(f"第 {n} 行没有『按原片』决策，无需恢复")
            saved = replace.save_row_decisions(bridge, decisions)
            return {"ok": True, "task_id": local_id, "n": n, "决策": decision,
                    "覆盖": {str(k): v for k, v in sorted(saved.items())},
                    "锁点剩余": lock_remaining(bridge)}
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

def group_rows(local_id: str, rows: list[int], primary: int | None = None,
               name: str | None = None) -> dict:
    """成组：校验行号在清单、≥2 行、都不在别的组 → 写 sidecar『同实体确认』。

    AT-11 §3.1.1/§3.2 追加两拒绝：品牌视觉行不可入组（品牌位不读图不占号，
    render.py 品牌分支）；行有 keep_origin 不可入组（先降级再合并会留半组替换，
    先恢复再合并）。
    AT-11 §3.1.2（M2 确认小窗）：primary=主行（必须 ∈ rows，缺省=最小行）、
    name=组名——给了任一就落盘『组元数据』{组键: {主行, 组名}}，刷新不回跳最小行。
    """
    task_dir = _task_dir(local_id)
    bridge = _bridge_of(task_dir)
    rows = sorted({int(x) for x in rows})
    if len(rows) < 2:
        raise HubError(f"成组至少要 2 行（收到 {rows}）")
    with task_lock(local_id):
        try:
            checklist = replace.checklist_path(bridge).read_text(encoding="utf-8-sig")
        except replace.ReplaceError as e:
            raise HubError(str(e)) from None
        by_no = {r["no"]: r for r in replace.parse_rows(checklist)}
        missing = [n for n in rows if n not in by_no]
        if missing:
            raise HubError(f"行号不在清单里: {missing}（现有 {sorted(by_no)}）")
        brand = [n for n in rows if str(by_no[n].get("cat") or "").startswith("品牌视觉")]
        if brand:
            raise HubError(f"行 {brand} 是品牌视觉行（品牌位不读图不占号），"
                           f"不可入组——品牌行单独处理")
        ko = [n for n in rows if n in replace.load_row_decisions(bridge)]
        if ko:
            raise HubError(f"行 {ko} 已按原片/已移除（keep_origin/dismissed），先恢复再合并"
                           f"（先降级后合并会留半组替换）")
        groups = replace.load_groups(bridge)
        for g in groups:
            overlap = sorted(set(rows) & set(g))
            if overlap:
                raise HubError(f"行 {overlap} 已在组 {g} 里（先解组再成组）")
        saved = replace.save_groups(bridge, groups + [rows])
        meta: dict[str, dict] | None = None
        if primary is not None or (name is not None and str(name).strip()):
            if primary is not None:
                try:
                    primary_no = int(primary)
                except (TypeError, ValueError):
                    raise HubError(f"主行必须是整数行号，收到 {primary!r}") from None
                if primary_no not in rows:
                    raise HubError(f"主行 {primary_no} 不在本次成组行 {rows} 里")
            else:
                primary_no = rows[0]
            clean_name = re.sub(r"\s+", " ", str(name)).strip() if name is not None else ""
            key = replace.group_key(rows)
            cur = replace.load_group_meta(bridge)
            entry = cur.get(key) if isinstance(cur.get(key), dict) else {}
            cur[key] = {"主行": primary_no, "组名": clean_name,
                        **{k: v for k, v in entry.items() if k not in ("主行", "组名")}}
            meta = replace.save_group_meta(bridge, cur)
        out: dict = {"ok": True, "task_id": local_id, "本次": rows, "组": saved}
        if meta is not None:
            out["组元数据"] = meta
        return out


def ungroup_rows(local_id: str, rows: list[int]) -> dict:
    """解组：把行从所在确认组摘出（<2 行的组剔除）；没组里的行如实报。

    AT-11 §3.7：解除联动后组名/主行元数据键随组清理（组键不再对应现存组 → 删）；
    『行决策覆盖』不无故清（行级决策属于行自身，不是组的）。
    """
    task_dir = _task_dir(local_id)
    bridge = _bridge_of(task_dir)
    rows = sorted({int(x) for x in rows})
    if not rows:
        raise HubError("解组要给行号（rows）")
    with task_lock(local_id):
        groups = replace.load_groups(bridge)
        loose = [n for n in rows if not any(n in g for g in groups)]
        if loose:
            raise HubError(f"行 {loose} 不在任何确认组里")
        rest = [[n for n in g if n not in rows] for g in groups]
        saved = replace.save_groups(bridge, rest)
        meta = replace.sync_group_meta(bridge, saved)
        return {"ok": True, "task_id": local_id, "解组": rows, "组": saved,
                "组元数据": meta}


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


# ------------------------------------------------ 生成配置接纳（AT-13 §2.1/§2.2/§三）

# 画布口径画幅（web/src/lib/media-size.ts videoRatioOptions）——hub 面板与后端同源
CANVAS_RATIOS = ("1:1", "3:4", "4:3", "16:9", "9:16", "21:9", "auto")
# 画布 videoRatioOptions 的宽高比（media-size.ts 同源）；**数组顺序即 tie 顺序**——
# TS 侧 inferVideoRatio 用 reduce 严格小于比较，同距离保留更靠前的项，这里逐条对齐。
CANVAS_VIDEO_RATIO_PX = (("1:1", 1.0), ("3:4", 3 / 4), ("4:3", 4 / 3),
                         ("16:9", 16 / 9), ("9:16", 9 / 16), ("21:9", 21 / 9))
CANVAS_VIDEO_RATIO_FALLBACK = "16:9"          # media-size.ts#inferVideoRatio 的兜底值（非法串）
# 协议真实接收域：与 ui/server.py 的 SDGF_RATIOS / WAN_RATIOS 同源。server 反向 import 本模块，
# 不能倒过来 import，故此处是副本——B4 合同锁（§2.9 共享 fixture）一并锁死，改一处必红。
SDGF_RATIOS = ("16:9", "9:16", "1:1", "4:3", "3:4", "adaptive")
SDGF_RESOLUTIONS = ("480", "720", "1080")
WAN_RATIOS = ("16:9", "9:16", "1:1")
WAN_FIXED_MODELS = ("wan3.0", "wan-omni2video")     # §2.1：wan 固定模型语义，不消费 model 选择
CONFIG_PARAM_KEYS = ("duration", "size", "vquality", "audio", "rawSize")
# 敏感键：出现就 400（不是静默丢弃——调用方会以为 key 已生效）
_SENSITIVE_KEYS = ("apikey", "api_key", "key", "secret", "token", "authorization",
                   "password", "access_token", "accesskey")
_CREDENTIAL_QUERY = ("key", "api_key", "apikey", "token", "access_token", "secret",
                     "signature", "sig", "password", "accesskey")
_URL_USERINFO_RE = re.compile(r"^[a-zA-Z][\w+.-]*://[^/?#]*@")


def _config_base_url(raw) -> str:
    """渠道 baseUrl 校验：http(s) 且不带 userinfo / 凭据 query（§2.8 先脱敏后落账的前置）。"""
    url = str(raw or "").strip()
    if not url:
        raise HubInputError("渠道身份缺 baseUrl")
    if not re.match(r"^https?://", url, re.IGNORECASE):
        raise HubInputError(f"渠道 baseUrl 必须是 http(s) 地址：{url!r}")
    if _URL_USERINFO_RE.match(url):
        raise HubInputError("渠道 baseUrl 不能带 userinfo（user:pass@）——key 不拼 URL（§2.8）")
    query = urllib.parse.urlparse(url).query
    if query:
        bad = sorted(k for k in urllib.parse.parse_qs(query) if k.lower() in _CREDENTIAL_QUERY)
        if bad:
            raise HubInputError(f"渠道 baseUrl 的 query 里带凭据参数 {bad}——key 不进 URL（§2.8）")
    return url


def _config_api_format(raw) -> str:
    fmt = str(raw or "").strip().lower()
    if fmt not in ("sdgf", "wan"):
        raise HubInputError(f"apiFormat={raw!r} 属画布专用协议，hub 不承接"
                            f"（本节点只承接 sdgf 系与 wan，§2.1）")
    return fmt


def _config_model(api_format: str, raw) -> str:
    model = str(raw or "").strip()
    if api_format == "wan":
        if model and model not in WAN_FIXED_MODELS:
            raise HubInputError(f"万相模型固定 wan3.0、不消费 model 选择（§2.1），收到 {model!r}")
        return ""                                    # 固定语义：落空串，不进快照自由度
    if not model:
        raise HubInputError("SDGF 渠道必须给 model（裸模型名，渠道::模型 里的后半段）")
    return model


def _config_mode(api_format: str, raw) -> str:
    mode = str(raw or "").strip().lower()
    if api_format == "wan":
        if mode:
            raise HubInputError("万相没有 mode（自动多图参考语义，§2.1）——选 wan 时 model/mode 留空")
        return ""
    if mode not in ("", "frames", "reference"):
        raise HubInputError(f"mode 只能是 frames / reference（或留空），收到 {raw!r}")
    return mode


_PIXEL_SIZE_RE = re.compile(r"^(\d+)\s*[x×*]\s*(\d+)$", re.IGNORECASE)
_RATIO_SIZE_RE = re.compile(r"^(\d+(?:\.\d+)?):(\d+(?:\.\d+)?)$")


def _canvas_infer_video_ratio(size: str) -> str:
    """画布 `media-size.ts#inferVideoRatio` 的 PY 同合同实现（AT-14 §3.1「同一合同双实现」）。

    口径逐条对齐 TS 侧（改任一端，共享 fixture 的 size 用例必红）：
      · 空串 / auto → auto；
      · 画布枚举（CANVAS_RATIOS）→ 原值；
      · W×H（分隔符 x / X / × / *，允许空白）→ 就近规约到枚举；
      · W:H（可带小数）→ 就近规约到枚举；
      · 其余非法串 → 画布兜底 16:9（不另立规则）。
    tie 顺序：画布数组顺序（1:1 / 3:4 / 4:3 / 16:9 / 9:16 / 21:9），同距离保留靠前者。
    **与画布的唯一有意差异**：零维在画布会兜底 16:9，这里显式拒绝——不静默改用户参数。
    """
    raw = str(size if size is not None else "").strip()
    if not raw or raw == "auto":                  # 画布口径：只有字面 auto（AUTO 走兜底分支，与 TS 一致）
        return "auto"
    if raw in CANVAS_RATIOS:
        return raw
    target: float | None = None
    m = _PIXEL_SIZE_RE.match(raw)
    if m:
        w, h = int(m.group(1)), int(m.group(2))
        if w <= 0 or h <= 0:
            raise HubInputError(f"params.size 的像素宽高必须为正：{raw!r}")
        target = w / h
    else:
        m = _RATIO_SIZE_RE.match(raw)
        if m:
            w, h = float(m.group(1)), float(m.group(2))
            if w <= 0 or h <= 0:
                raise HubInputError(f"params.size 的宽高必须为正：{raw!r}")
            target = w / h
    if target is None:
        return CANVAS_VIDEO_RATIO_FALLBACK
    best, best_diff = CANVAS_VIDEO_RATIO_FALLBACK, abs(16 / 9 - target)
    for value, px in CANVAS_VIDEO_RATIO_PX:
        diff = abs(px - target)
        if diff < best_diff:                      # 严格小于：与 TS reduce 的 tie 行为一致
            best, best_diff = value, diff
    return best


def _ratio_from_size(raw) -> str:
    """画幅第一步（AT-14 §3.1）：画布 inferVideoRatio 同口径规约到枚举。

    旧实现做最大公约数归一，互素时字面直出 240:427 被渠道拒（D1 根因②）——
    画布引擎从不这样传，它就近规约到枚举。渠道接收域校验交给 `_protocol_ratio`。
    """
    return _canvas_infer_video_ratio(str(raw or ""))


def _protocol_ratio(api_format: str, ratio: str) -> str:
    """渠道不接受的画幅显式拒绝（§2.1「不做固定白名单混同」——说明白哪个值不行）。"""
    if api_format == "wan":
        if ratio not in WAN_RATIOS:
            raise HubInputError(f"万相只接 {'/'.join(WAN_RATIOS)} 三个画幅，{ratio} 不在其内")
        return ratio
    if ratio == "auto":
        return "adaptive"                            # SDGF 的自适应字面值
    if ratio not in SDGF_RATIOS:
        raise HubInputError(f"SDGF 不接画幅 {ratio}（支持 {'/'.join(SDGF_RATIOS)}；"
                            f"21:9 属画布专用，渠道不生效）")
    return ratio


def _config_resolution(raw) -> str:
    value = str(raw if raw is not None else "").strip()
    if value not in SDGF_RESOLUTIONS:
        raise HubInputError(f"params.vquality 只能是 {'/'.join(SDGF_RESOLUTIONS)}，收到 {raw!r}")
    return value


def _config_duration(raw) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise HubInputError(f"params.duration 必须是整数秒，收到 {raw!r}") from None
    if not 4 <= value <= 30:                          # 画布 VIDEO_SECONDS_MIN/MAX
        raise HubInputError(f"params.duration 必须在 4–30 秒，收到 {value}")
    return value


def _config_audio(raw) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    text = str(raw or "").strip().lower()
    if text in ("true", "1", "yes"):
        return True
    if text in ("false", "0", "no", ""):
        return False
    raise HubInputError(f"params.audio 只能是布尔值，收到 {raw!r}")


def _config_raw_size(raw) -> str:
    """用户原始画幅输入（**只留痕，不进快照、不参与提交值**）：截断防脏数据。"""
    return str(raw if raw is not None else "").strip()[:64]


def _normalize_config(config: dict, task: dict) -> tuple[dict, dict, dict]:
    """approve 携带的画布配置 → (gen_params, gen_channel, size_input)：白名单构造，key 一律不落。

    config = {渠道身份:{baseUrl,apiFormat,渠道名}, model, mode,
              params:{duration,size,rawSize,vquality,audio}}（§2.1，无 key）
    白名单外键丢弃（§三「read_json 后白名单取键」）；敏感键直接 400。
    gen_params 落桥提交体词表 {duration,ratio,resolution,audio}（画布 size/vquality 派生），
    gen_channel 落 {baseUrl,apiFormat,渠道名,model,mode}——两者合起来就是 v2 快照的输入。
    size_input = {raw, ratio}（AT-14 §3.1 留痕：原始输入 + 规约结果）——**不并进 gen_params**，
    否则会改变 v2 快照哈希输入（合同要求哈希输入不变）。
    """
    if not isinstance(config, dict):
        raise HubInputError("config 必须是 JSON 对象")
    lowered = {str(k).strip().lower(): k for k in config}
    hit = [lowered[k] for k in _SENSITIVE_KEYS if k in lowered]
    if hit:
        raise HubInputError(f"approve 的 config 不允许带敏感键 {hit}——批准只固化非敏感身份，"
                            f"key 在提交时一次性透传（§2.1）")
    identity = config.get("渠道身份")
    if not isinstance(identity, dict):
        raise HubInputError("config 缺渠道身份（渠道身份:{baseUrl,apiFormat,渠道名}）")
    base_url = _config_base_url(identity.get("baseUrl"))
    api_format = _config_api_format(identity.get("apiFormat"))
    name = str(identity.get("渠道名") or "").strip()
    if not name:
        raise HubInputError("渠道身份缺渠道名——同名模型不同渠道必须是不同快照（§2.2）")
    model = _config_model(api_format, config.get("model"))
    mode = _config_mode(api_format, config.get("mode"))
    raw_params = config.get("params")
    if not isinstance(raw_params, dict):
        raise HubInputError("config 缺 params（{duration,size,vquality,audio}）")
    base = task.get("gen_params") if isinstance(task.get("gen_params"), dict) else {}
    given = {k: raw_params[k] for k in CONFIG_PARAM_KEYS if k in raw_params}
    gen_params = {
        "duration": _config_duration(given.get("duration", base.get("duration", 5))),
        "ratio": _protocol_ratio(api_format,
                                 _ratio_from_size(given.get("size") or base.get("size")
                                                  or base.get("ratio"))),
        "resolution": _config_resolution(given.get("vquality") or base.get("vquality")
                                        or base.get("resolution")),
        "audio": _config_audio(given.get("audio", base.get("audio", True))),
    }
    gen_channel = {"baseUrl": base_url, "apiFormat": api_format, "渠道名": name,
                   "model": model, "mode": mode}
    # 原始输入留痕（前端给 rawSize；CLI/旧调用方没带 → 就记实际用的那个 size 字面）
    raw_size = _config_raw_size(given.get("rawSize")) or _config_raw_size(
        given.get("size") or base.get("size") or base.get("ratio"))
    size_input = {"raw": raw_size, "ratio": gen_params["ratio"]} if raw_size else {}
    return gen_params, gen_channel, size_input


def do_approve(local_id: str, config: dict | None = None) -> dict:
    """批准 = 审批停点（AT-13 §2.2）：先固化配置，再按 v2 算快照，再进 approved。

    带 config：校验 → 写 gen_params+gen_channel（无 key）→ v2 快照（prompt+params+渠道身份
    +model+mode）→ approved；submit 阶段零配置改动。
    不带 config：旧语义（v1 快照，老任务 / 无浏览器 CLI）。
    任务锁短持：校验+固化+迁移做完即释放（§2.7），不持锁等后台。
    """
    task_dir = _task_dir(local_id)
    with task_lock(local_id):
        task = state.load(task_dir)
        norm = _normalize_config(config, task) if config is not None else None
        try:
            st = generate.do_approve(task_dir, config=norm[:2] if norm is not None else None,
                                     verbose=False)
        except generate.GenError as e:
            raise HubError(str(e)) from None
        if norm is not None and norm[2]:
            # AT-14 §3.1：原始画幅输入落任务文件（gen_params 旁的 size_input，不进快照哈希）
            task = _write_task(task_dir, lambda t: t.__setitem__("size_input", norm[2]))
        task = state.load(task_dir)
    return {"state": st,
            "snapshot_version": state.snapshot_version(task),
            "gen_params": task.get("gen_params") or {},
            "gen_channel": task.get("gen_channel") or {},
            "size_input": task.get("size_input") or {}}


def start_submit(local_id: str, key: str | None = None) -> dict:
    """真提交（付费，前端只在「批准」后调用）——异步，进度写任务.json gen_progress。

    AT-13 §2.7 锁规则：本函数**短持**任务锁——校验状态 + 冻结已批准配置 + 注册提交占用
    （HUB_JOBS 建立）后立即释放；后台线程不持锁等全局提交槽，只在写任务文件时短持同一把锁。

    AT-13 §2.3 守卫生效点（hub 侧）：已有活跃提交 / dispatch 未完结 / submit_unknown 阻断
    → 409 拒绝，**一次创建都不发**（跨进程守卫在 generate.do_submit 里再兜一层）。
    state=generating + 有确定回执 → 走「续查」不创建（runner/CLI 同一条链）。
    key：提交时一次性透传的渠道 key（不落盘、不进 argv/URL、不进 job 记录）。
    """
    task_dir = _task_dir(local_id)

    def _run(td: Path, frozen_cfg: dict, api_key: str | None):
        def _progress(pct, waited):  # noqa: ANN001 —— generate 回调签名
            if pct is None:
                return
            _write_task(td, lambda t: t.__setitem__("gen_progress", int(pct)))
        with _SUBMIT_LOCK:
            return generate.do_submit(td, verbose=False, on_progress=_progress,
                                      frozen=frozen_cfg,
                                      write_guard=lambda: task_lock(td.name),
                                      api_key=api_key)

    with task_lock(local_id):
        task = state.load(task_dir)
        st = task["state"]
        if _active_submit(local_id):
            raise HubError("这个任务已有提交在跑（排队/执行中）——别重复点，等它出结果")
        if state.dispatch_blocks_create(task):
            raise HubError(f"上次提交没拿到确定回执（dispatch={state.dispatch_phase(task)}）："
                           f"云端可能已收单。先到渠道控制台核对没有这一单 →「解除阻断」→ 再提交；"
                           f"直接重提会重复扣费")
        if task.get("submit_unknown"):
            raise HubError("任务被「未知收单」挡住（submit_unknown）：先核对渠道无单 →「解除阻断」，"
                           "已有云端 taskId 的按该 id 续查，不许重提")
        if st not in ("approved", "generating"):
            raise HubError(f"提交需 approved（先批准），当前 {st}")
        # 冻结副本=已批准快照的输入；批准后若被改，do_submit 的快照比对会作废本次提交
        frozen = {"gen_params": dict(task.get("gen_params") or {}),
                  "gen_channel": dict(task.get("gen_channel") or {})}
        job = _spawn("hub-submit", _run, task_dir, frozen, key or "")
    return job


def revoke_approval(local_id: str, *, note: str = "撤销批准（改配置后重批）") -> dict:
    """受控回退边（AT-13 §2.4）：approved → awaiting_approval，清批准、保留资产/提示词/草稿配置。

    给「批准后改配置」用：§2.2 明确**不自动重批**——先撤销，再由所有者带新配置批准。
    """
    task_dir = _task_dir(local_id)
    with task_lock(local_id):
        try:
            task = state.reject_back_to_approval(task_dir, note=note)
        except state.StateError as e:
            raise HubError(str(e)) from None
    return {"ok": True, "state": task["state"], "note": note}


def release_submit_unknown(local_id: str, *, confirm: bool = False, note: str = "") -> dict:
    """解除「未知收单」阻断（AT-13 §2.3 人工动作）：只有核对过渠道无单才允许，全程记 history。"""
    task_dir = _task_dir(local_id)
    with task_lock(local_id):
        task = state.load(task_dir)
        blocked = bool(task.get("submit_unknown")) or state.dispatch_blocks_create(task)
        if not blocked:
            raise HubError("任务当前没有提交阻断（submit_unknown/dispatch），无需解除")
        if not confirm:
            raise HubInputError("解除阻断是人工动作：确认已在渠道控制台核对「没有这一单」后，"
                                "请求体带 confirm:true 再调")
        out = generate.release_unknown(task_dir,
                                       note=note or "人工核对渠道无单后解除阻断",
                                       guard=lambda: task_lock(local_id))
    return {"ok": True, "state": out["state"], "dispatch": out.get("dispatch")}


def recover_orphan(local_id: str, *, confirm: bool = False) -> dict:
    """generating 孤儿受控恢复（AT-13 §2.4）：四守卫（无活跃提交/无云端在途/无成片/已解除）
    全过才迁 awaiting_approval；任一不过如实 409，并回诊断。"""
    task_dir = _task_dir(local_id)
    with task_lock(local_id):
        task = state.load(task_dir)
        if not confirm:
            raise HubInputError("受控恢复是人工动作：确认渠道无单、无在途后再调，请求体带 confirm:true")
        ev = generate.cloud_evidence(task_dir, task)
        try:
            out = state.recover_orphan_generating(
                task_dir, active_submit=_active_submit(local_id),
                cloud_in_flight=bool(ev.get("in_flight")),
                cloud_task_id=ev.get("cloud_task_id") or "",
                note="人工核对渠道无单后受控恢复（generating 孤儿）")
        except state.StateError as e:
            raise HubError(str(e)) from None
    return {"ok": True, "state": out["state"],
            "diagnosis": generate.diagnose(task_dir, out)}


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
