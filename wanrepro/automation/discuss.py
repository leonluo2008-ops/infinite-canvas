# -*- coding: utf-8 -*-
"""AT-3 拆解自动执行器——ingested → discussing → awaiting_assets。

桥合同（server.py 实读 2026-11-08）：
  POST /api/lapian/run {id, intent} → {job}；busy=HTTP 409（排队语义，不是失败）
  GET  /api/job/<hex> → {status: running|done|error}；done 带 result；
       error 带 error 原文全量；404=服务重启过（JOBS 内存态丢失）

审计修正（路2 B4）：产物优先——执行前先按 bridge_id 查 has_* 三标志，
全真则判「已完成」直接迁移，不重跑（kill/重启后盲重发会新建任务的坑在此消解）。
"""
import argparse
import json
import sys
import time
from pathlib import Path

from . import bridge, config, state

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

POLL_INTERVAL = 3        # 秒
POLL_MAX_SECONDS = 1800  # 与桥端 analyze 子进程上限一致：长片大拆解实测可超 10 分钟
HEARTBEAT_INTERVAL = 60  # 远程等待心跳间隔（秒）：等待期每 60s 追加一行「还在等」日志
BUSY_RETRY_MAX = 60      # 409 排队：最多等 60 次 × 5s = 5 分钟
BUSY_RETRY_INTERVAL = 5


class DiscussError(RuntimeError):
    pass


# AT-15 R2：门禁轮次进拆解日志。R1 的透出链是 mark_stage → 进度.json → job 轮询逐阶段记日志，
# 但轮询粒度（3s）追不上门禁尾段的快速阶段变化（实测「投影」「门禁第1轮」整段被跳过），
# 门禁轮次在拆解日志里丢线。这里在 job done 终态补账一次：读 analysis/reverse/<bridge>/门禁.json
# （与自检面板/selfcheck.load_gate_item 同一真源，红线 25 同源不另判）追一行门禁终态，确定性落日志。
_ANALYSIS_ROOT = Path(__file__).resolve().parents[1] / "analysis" / "reverse"


def _gate_summary_line(bridge_id: str) -> str:
    try:
        gate = json.loads((_ANALYSIS_ROOT / bridge_id / "门禁.json").read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(gate, dict):
        return ""
    result = gate.get("结果") if isinstance(gate.get("结果"), dict) else {}
    checks = result.get("checks") if isinstance(result.get("checks"), list) else []
    passed = sum(1 for item in checks if isinstance(item, dict) and item.get("verdict") == "PASS")
    missing = len(result.get("缺项") or [])
    warns = len(result.get("告警") or [])
    verdict = str(result.get("verdict") or "?")
    rounds = gate.get("轮次") if isinstance(gate.get("轮次"), int) else 0
    limit = gate.get("上限") if isinstance(gate.get("上限"), int) else "?"
    return (f"门禁终态：{gate.get('状态') or '?'}｜{verdict}｜绿项 {passed}/{len(checks)}"
            f"｜缺项 {missing}｜告警 {warns}｜重拆 {rounds}/{limit}")


def _emit_gate_summary(task: dict, on_stage=None) -> None:
    """job done 后补一行门禁终态（无 门禁.json = 旧任务/门禁没跑成，如实不写）。"""
    bridge_id = str(task.get("bridge_id") or "").strip()
    if not bridge_id:
        return
    line = _gate_summary_line(bridge_id)
    if line and on_stage:
        on_stage(line)


def _products_ready(bridge_id: str) -> bool:
    p = state.probe_bridge_products(bridge_id)
    return bool(p and p["has_disc"] and p["has_check"] and p["has_prompt"])


# ---------------------------------------------------------------- 音频三态落盘（AT-17 B2c）
# 三态默认值的推导只有一份实现：pipeline/reverse/intent.py 的 audio_mode()（纯字符串、
# 零模型调用，来源=B1 已落盘的结构化字段）。这里只负责「拆解完成时把同一份值落到两处」：
#   · 任务目录 `audio_mode.json`（画布链节点状态，intent.write_audio_mode）
#   · 任务.json 字段 `audio_mode`（hub 链落盘面）
# 两条链同源同值，不许各推一套。

_intent_module = None


def _intent_mod():
    """按路径加载 pipeline/reverse/intent.py（它不是包；顶层无网络调用）。"""
    global _intent_module
    if _intent_module is None:
        import importlib.util
        path = Path(__file__).resolve().parents[1] / "pipeline" / "reverse" / "intent.py"
        spec = importlib.util.spec_from_file_location("wanrepro_reverse_intent_for_discuss", path)
        if spec is None or spec.loader is None:
            raise DiscussError(f"导不进 {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _intent_module = module
    return _intent_module


def derive_audio_mode(bridge_id: str) -> dict:
    """按拆解推导三态：{audio_mode, 依据, 待核对}；读不到/推导失败 → {}（禁编值）。"""
    bridge_id = str(bridge_id or "").strip()
    if not bridge_id:
        return {}
    outdir = _ANALYSIS_ROOT / bridge_id
    try:
        disc_text = (outdir / "拆解.md").read_text(encoding="utf-8-sig")
    except OSError:
        return {}
    try:
        intent = _intent_mod()
        return intent.audio_mode(disc_text, intent.audio_json(outdir))
    except Exception:  # noqa: BLE001 —— 推导是附加值，不许把拆解判死
        return {}


AUDIO_MODE_FIELD = "audio_mode"


def persist_audio_mode(task_dir: Path) -> dict:
    """拆解完成即落三态：画布链旁路 `audio_mode.json` + hub 链 任务.json 字段（同一份推导）。

    已存在不覆盖：B3 界面上改选的值，重跑拆解也留得住。返回 {ok, audio_mode?, 依据?, why?}。
    """
    task = state.load(task_dir)
    current = str(task.get(AUDIO_MODE_FIELD) or "").strip()
    if current:
        return {"ok": True, "audio_mode": current, "why": "已有，未覆盖"}
    bridge_id = str(task.get("bridge_id") or "").strip()
    outdir = _ANALYSIS_ROOT / bridge_id
    try:
        disc_text = (outdir / "拆解.md").read_text(encoding="utf-8-sig")
    except OSError:
        return {"ok": False, "why": f"读不到 {outdir / '拆解.md'}——不写、不猜值"}
    try:
        intent = _intent_mod()
        _path, info = intent.write_audio_mode(outdir, disc_text)
    except Exception as exc:  # noqa: BLE001 —— 附加值落盘失败不许把拆解判死
        return {"ok": False, "why": f"三态落盘失败（{type(exc).__name__}：{exc}）"}
    task[AUDIO_MODE_FIELD] = info[AUDIO_MODE_FIELD]
    state.save(task_dir, task)
    return {"ok": True, "audio_mode": info[AUDIO_MODE_FIELD], "依据": info.get("依据", ""),
            "待核对": bool(info.get("待核对"))}


def _fmt_wait(seconds: float) -> str:
    """等待时长人话：65 →「1分5秒」，7 →「7秒」（心跳行与阶段行同一口径）。"""
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}秒"
    return f"{total // 60}分{total % 60}秒"


def start_and_wait(task: dict, on_stage=None, on_heartbeat=None,
                   heartbeat_interval: float | None = None) -> str:
    """POST lapian/run（409 排队重试）→ 轮询 job 至 done/error。返回 job id。

    on_stage(msg)：阶段回调（排队/提交/每次 stage 变化）——hub 工作台用它落
    拆解日志.txt 并回写任务.json 的 lapian_stage；CLI 模式不传。

    on_heartbeat(msg)：**远程等待心跳**（AT-16 B4b），与 on_stage **分离**是硬合同：
      · 等待期每 heartbeat_interval 秒（默认 HEARTBEAT_INTERVAL=60）调一次，行内含总耗时，
        文案只说「本地等待循环仍在运行」+ 预期时长——桥端 /api/job 只回 status/stage，
        没有可采信的远程百分比，编一个就是假进度；
      · 绝不能走 on_stage：hub 侧 on_stage 每次回写 lapian_stage 与 lapian_stage_at，
        心跳借道就等于每次把阶段等待计时清零（B4a 双锚作废，正是本批要绕开的覆盖）；
      · 终态（done/error/超时/异常）后不再心跳——心跳只在轮询循环体内、终态判定之后发，
        done 那一轮直接 return，不会补发。
    """

    def _emit(msg: str) -> None:
        if on_stage:
            try:
                on_stage(msg)
            except Exception:  # noqa: BLE001 —— 进度回调失败不拖垮拆解主链
                pass

    def _beat(elapsed: float) -> None:
        """等待心跳（AT-16 B4b）：**只**调 on_heartbeat——hub 侧该回调只追加 拆解日志.txt，
        不写任务.json 任何 lapian_* 字段（阶段文案与阶段起点都归 on_stage 独有）。"""
        if not on_heartbeat:
            return
        try:
            on_heartbeat("等待中：本地等待循环仍在运行"
                         f"（总耗时 {_fmt_wait(elapsed)}；远程模型通常 5–15 分钟，慢时更久，不用重开）")
        except Exception:  # noqa: BLE001 —— 心跳失败不拖垮拆解主链
            pass

    for attempt in range(BUSY_RETRY_MAX):
        try:
            out = bridge.post_json("/api/lapian/run",
                                   {"id": task["bridge_id"], "intent": task.get("intent", "")})
            job = out.get("job")
            if not job:
                raise DiscussError(f"lapian/run 未回 job: {json.dumps(out, ensure_ascii=False)[:200]}")
            break
        except bridge.BridgeError as e:
            if "HTTP 409" in str(e):
                _emit(f"拆解槽忙，排队重试（{attempt + 1}/{BUSY_RETRY_MAX}，{BUSY_RETRY_INTERVAL}s 后）")
                print(f"  [排队] 桥忙（{BUSY_RETRY_INTERVAL}s 后重试 {attempt + 1}/{BUSY_RETRY_MAX}）")
                time.sleep(BUSY_RETRY_INTERVAL)
                continue
            raise
    else:
        raise DiscussError(f"桥持续忙 {BUSY_RETRY_MAX} 次，放弃（任务未失败，重跑本命令续）")
    _emit(f"已提交拆解（job={job}），远程模型通常 5–15 分钟，慢时更久，不用重开")

    waited = 0
    last_stage = ""
    beat_every = HEARTBEAT_INTERVAL if heartbeat_interval is None else float(heartbeat_interval)
    t0 = time.monotonic()
    next_beat = beat_every
    while waited < POLL_MAX_SECONDS:
        try:
            rec = bridge.get(f"/api/job/{job}")
        except bridge.BridgeError as e:
            if "HTTP 404" in str(e):
                # 服务重启过（JOBS 丢）——产物优先：产物在就判完成
                if _products_ready(task["bridge_id"]):
                    print("  [恢复] job 404 但产物齐——判已完成")
                    return job
                raise DiscussError(f"job 丢失（桥重启）且产物未齐: {e}") from None
            raise
        st = rec.get("status")
        if st == "done":
            _emit(f"拆解完成（耗时 {rec.get('elapsed', '?')}s）")
            _emit_gate_summary(task, on_stage=_emit)
            return job
        if st == "error":
            _emit(f"失败：{rec.get('error')}")
            raise DiscussError(f"桥侧拆解失败（原文）: {rec.get('error')}")
        stage = str(rec.get("stage") or "")
        if stage and stage != last_stage:
            last_stage = stage
            _emit(f"{stage}（已 {rec.get('elapsed', waited)}s）")
        time.sleep(POLL_INTERVAL)
        waited += POLL_INTERVAL
        # 心跳只在**等待期**发：终态分支在上面已 return，所以 done/error 之后不会有心跳补发
        if beat_every > 0:
            elapsed = time.monotonic() - t0
            if elapsed >= next_beat:
                _beat(elapsed)
                next_beat = elapsed + beat_every      # 一次只补一行，不追赶补发
    raise DiscussError(f"拆解超 {POLL_MAX_SECONDS}s 未完成（job={job}）——不判失败，可重跑续查")


def run_discuss(task_dir: Path, *, verbose: bool = True, on_stage=None, on_heartbeat=None) -> str:
    """执行拆解到 awaiting_assets。返回终态。

    on_stage(msg)：阶段回调透传给 start_and_wait（hub 工作台落日志/回写状态）。
    on_heartbeat(msg)：远程等待心跳回调，透传给 start_and_wait（只追加日志行，不碰阶段/锚）。

    失败语义分级（审计 WARN「busy=排队不记 failed」同类）：
      · 任务级失败（桥报 error / done 但产物缺 / job 丢失且产物未齐）
        → failed 终态，history 记原文；
      · 环境级故障（桥连不上 / 超时放弃）→ 状态不迁移（留 ingested/discussing），
        重跑本命令即续——环境恢复不该把任务判死。
    """
    task = state.load(task_dir)
    cur = task["state"]
    if cur == "ingested":
        pass
    elif cur == "failed":
        # 人工重跑（CLI 重跑/工作台重试按钮都是人工动作）：failed 不判死，重跑即续
        state.transition(task_dir, "discussing", note="人工重试拆解")
        if verbose:
            print(f"[重试] {task['local_id']}: failed → discussing（人工重试）")
    elif cur == "discussing":
        if verbose:
            print(f"[续跑] {task['local_id']}: 上次拆解中断（discussing），重查产物后续跑")
    elif cur == "awaiting_assets" and _products_ready(task["bridge_id"]):
        if verbose:
            print(f"[SKIP] {task['local_id']}: 拆解产物已齐（产物优先，不重跑）")
        return "awaiting_assets"
    else:
        raise DiscussError(f"状态 {cur} 不可启动拆解（应 ingested/discussing 或已完成）")

    # 产物优先：kill/重启恢复时可能桥里已完成但本地状态还停在 ingested/discussing
    if _products_ready(task["bridge_id"]):
        if cur == "ingested":
            state.transition(task_dir, "discussing", note="产物已齐，直接迁移")
        state.transition(task_dir, "awaiting_assets", note="桥侧产物优先判完成")
        if verbose:
            print(f"[SKIP] {task['local_id']}: 桥侧产物已齐（恢复路径）")
        return "awaiting_assets"

    if cur == "ingested":
        state.transition(task_dir, "discussing", note="拆解启动")
    try:
        job = start_and_wait(task, on_stage=on_stage, on_heartbeat=on_heartbeat)
        # done 后核产物（失败即失败：桥说 done 但产物缺 → 如实报）
        if not _products_ready(task["bridge_id"]):
            raise DiscussError("桥报 done 但 has_disc/has_check/has_prompt 不全——如实记失败")
        state.transition(task_dir, "awaiting_assets",
                         note=f"拆解完成 job={job}", extra={"last_job": job})
        if verbose:
            print(f"[OK] {task['local_id']}: 拆解完成 → awaiting_assets（job={job}）")
        return "awaiting_assets"
    except bridge.BridgeError as e:
        # 环境级：状态留在 discussing，重跑即续（不判 failed）
        if verbose:
            print(f"[环境故障] {task['local_id']}: {e} —— 状态保留 discussing，重跑续")
        raise
    except DiscussError as e:
        state.transition(task_dir, "failed", note=str(e))
        raise


def _resolve(local: str) -> Path:
    d = config.PROJECTS_DIR / local
    if not (d / "任务.json").is_file():
        cand = list(config.PROJECTS_DIR.glob(f"auto-*"))
        raise SystemExit(f"找不到任务 {local}（projects 根：{config.PROJECTS_DIR}；现有: "
                         f"{[p.name for p in cand] if cand else '无'}）")
    return d


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="AT-3 拆解自动执行器")
    ap.add_argument("local_id", help="任务号（auto-0001 形态）")
    ap.add_argument("--bridge", help="bridge 基地址")
    args = ap.parse_args(argv)
    if args.bridge:
        config.BRIDGE_BASE = args.bridge.rstrip("/")
    config.ensure_dirs()
    try:
        run_discuss(_resolve(args.local_id))
        # AT-17 B2c：拆解完成即落三态（CLI 侧；hub 侧在 hub_api._spawn_discuss 里持锁写）
        info = persist_audio_mode(_resolve(args.local_id))
        print(("[音频三态] " + str(info.get("audio_mode"))) if info.get("ok")
              else f"[音频三态] 未落盘：{info.get('why')}")
        return 0
    except (DiscussError, bridge.BridgeError, state.StateError) as e:
        print(f"[FAIL] {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
