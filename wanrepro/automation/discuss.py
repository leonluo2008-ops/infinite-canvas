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
import os
import re
import sys
import time
from pathlib import Path

from . import bridge, config, remote_wait, state

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

POLL_INTERVAL = 3        # 秒
POLL_MAX_SECONDS = 1800  # 与桥端 analyze 子进程上限一致：长片大拆解实测可超 10 分钟
HEARTBEAT_INTERVAL = 60  # 远程等待心跳间隔（秒）：等待期每 60s 追加一行「还在等」日志
BUSY_RETRY_MAX = 60      # 409 排队：最多等 60 次 × 5s = 5 分钟
BUSY_RETRY_INTERVAL = 5


def _env_seconds(name: str, default: float) -> float:
    """间隔加速位（AT-16 回归探针用）：环境变量覆盖，**不设 = 上面那个常量**。

    为什么必须是环境变量而不是模块属性：AT-19 起执行体跑在独立子进程
    （automation/discuss_runner.py），探针在自己进程里改 `discuss.POLL_INTERVAL` 到不了
    真正的执行体——那种加速度在进程化之后恒等于没设。环境变量由 hub_api._spawn_process
    随 Popen 透传给子进程，父子同值。

    非法值/空串/非正数一律回落默认（生产行为逐字不变；默认值就是常量本身，故
    「不设环境变量」与进程化之前完全一致）。
    """
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return float(default)
    try:
        val = float(raw)
    except ValueError:
        return float(default)
    return val if val > 0 else float(default)


class DiscussError(RuntimeError):
    pass


class SlotBusy(DiscussError):
    """拆解槽（远程一侧的排他位）被占、等到上限仍未拿到——**环境级**，不是远程判失败。

    语义为什么单独成一类：`start_and_wait` 的 409 排队不是「这一轮拆解失败」，而是
    「压根没提交上去」。若和任务级失败混在一起，run_discuss 会把它写成 failed，文案里那句
    「本任务没有被远程判失败」与磁盘状态互相打脸。run_discuss 对它只透传不落 failed，
    执行体按环境级退出码退场（任务保留 discussing，稍后重试即续）。

    消息一律由 `automation/remote_wait.py` 生成（唯一文案源）：「在等谁的远程返回、
    已等多久、预计还需多久、为什么只能等」——不再给用户刷「第 N 次失败」的计数。
    """
    slot: dict | None = None


# AT-15 R2：门禁轮次进拆解日志。R1 的透出链是 mark_stage → 进度.json → job 轮询逐阶段记日志，
# 但轮询粒度（3s）追不上门禁尾段的快速阶段变化（实测「投影」「门禁第1轮」整段被跳过），
# 门禁轮次在拆解日志里丢线。这里在 job done 终态补账一次：读 analysis/reverse/<bridge>/门禁.json
# （与自检面板/selfcheck.load_gate_item 同一真源，红线 25 同源不另判）追一行门禁终态，确定性落日志。
_ANALYSIS_ROOT = Path(os.environ.get("WANREPRO_ANALYSIS_ROOT") or "").expanduser() \
    if (os.environ.get("WANREPRO_ANALYSIS_ROOT") or "").strip() \
    else Path(__file__).resolve().parents[1] / "analysis" / "reverse"


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


# ---------------------------------------------------------------- 失败回写（桥侧原文 → 任务.json）
#
# 职责边界（本模块内唯一一套）：
#   · analyze.py（桥侧）只写 `analysis/reverse/<bridge_id>/进度.json`，失败写
#     {"stage": "失败", "error": "<步骤>失败（退出码 N）"}；它**不碰** 任务.json。
#   · 本模块只写任务目录的 `任务.json`（失败原因/history/证据），**不写** 进度.json。
#   · 桥侧 stderr 尾部由 ui/server.py::analyze_video 采集，原样拼进 /api/job 的 error 字段
#     （形如「analyze.py 退出 3：<stderr 尾部>」）——这是错误原文的主通道；进度.json 的
#     error 是结构化备用通道（job 记录随服务重启丢掉时只剩它）。
#
# 同一次失败只允许一次任务态迁移：所有任务级失败都经 _fail_task 落盘，它按
# 「state 已 failed 且原因相同 → 不动盘」幂等；看门狗（fail_orphan_task，AT-18 唯一降级
# 入口）先按冻结文案判死、随后桥侧原文才到的场景，只升级原因，不写 history、不刷新 failed_at。
# 与 hub 侧退出码文件回写不重叠：那条链只在「执行体已不存在」时降级，本模块只在
# run_discuss 活着并把失败报出来的路径里降级，二者互斥。
#
# 云端原文优先（缺陷：失败详情只有转述、云端原始错误被 tail 挤掉）：
#   桥侧 tail 只留 analyze.py 输出的最后一段，extract 打印的「接口返回 error：…」
#   被它的总结行挤出窗口——于是用户看不到真正的失败原因。可读的原文由 extract.py 落盘到
#   `analysis/<bridge_id>/拆解错误.json`（schema 冻结：error_type / message / finish_reason /
#   raw_response，对齐 call_vlm 的 vision_error.json），本模块只做两件事：
#     1) 读它，把「云端原文：{message}」摆到 failed_reason 主位；
#     2) 退出码 / finish_reason / raw_response / 原转述行全部降级为附属信息跟在后面（不丢弃）。
#   读不到（旧产物、非 extract 失败）→ 保留原转述行为 + 显式「云端原文：未落盘」，
#   绝不静默伪装成有原文。全文逐字透传进 failed_evidence.cloud_error（不截断、不摘要、不重判）。

CLOUD_ERROR_NAME = "拆解错误.json"
CLOUD_RAW_ON_DISK = "on_disk"      # failed_evidence.cloud_raw：原文已落盘并已进主位
CLOUD_RAW_MISSING = "missing"      # failed_evidence.cloud_raw：查过、没落盘（未落盘标注同源）

_BRIDGE_EXIT_RE = re.compile(r"analyze\.py 退出 (\d+)")
_PROGRESS_EXIT_RE = re.compile(r"退出码 (\d+)")


def _cloud_error(bridge_id: str) -> dict | None:
    """读 extract.py 落盘的云端原文 → dict（原样，冻结 schema 透传）或 None。

    路径与 extract.py 写入一致：`analysis/<bridge_id>/拆解错误.json`（同一个
    WANREPRO_ANALYSIS_ROOT / analysis.reverse 根，本模块 _ANALYSIS_ROOT 与 analyze.py 的
    ANALYSIS_ROOT 默认同源）。文件缺失、JSON 坏、message 空 → None：调用方据此走
    「未落盘」分支，不许拿空壳当原文。
    """
    tid = str(bridge_id or "").strip()
    if not tid:
        return None
    try:
        data = json.loads((_ANALYSIS_ROOT / tid / CLOUD_ERROR_NAME).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if not str(data.get("message") or "").strip():
        return None
    return data


def _exit_code_of(text: str, exit_code: int | None) -> int | None:
    """退出码：调用方给的优先，没给就从原文里取回（口径与 _bridge_failure/_progress_failure 同源）。"""
    if isinstance(exit_code, int):
        return exit_code
    for pattern in (_BRIDGE_EXIT_RE, _PROGRESS_EXIT_RE):
        m = pattern.search(str(text or ""))
        if m:
            return int(m.group(1))
    return None


def _failure_detail(bridge_id: str, text: str, exit_code: int | None,
                    cloud: dict | None) -> tuple[str, str]:
    """组装用户可见的失败详情 → (failed_reason, cloud_raw 标记)。

    · 有云端原文：主位「云端原文：{message}」，退出码/finish_reason/原转述行/raw_response
      逐条作为附属信息追加——转述照留，只是不再占主位；
    · 没有原文但转述在：转述原样保留（与改动前逐字一致）+ 显式「云端原文：未落盘」；
    · 两者都空：返回 ("", ...)，交给 _fail_task 的 AT-18 冻结文案兜底（那条契约不动）。
    """
    transcript = str(text or "").strip()
    code = _exit_code_of(transcript, exit_code)
    if cloud is None:
        if not transcript:
            return "", CLOUD_RAW_MISSING
        where = f"analysis/{bridge_id}/{CLOUD_ERROR_NAME}"
        return (f"{transcript}\n云端原文：未落盘（未找到 {where}：旧产物或非云端失败）",
                CLOUD_RAW_MISSING)

    msg = str(cloud.get("message") or "").strip()
    lines = [f"云端原文：{msg}"]
    meta: list[str] = []
    if code is not None:
        meta.append(f"退出码 {code}")
    finish = cloud.get("finish_reason")
    if finish not in (None, ""):
        meta.append(f"finish_reason={finish}")
    error_type = str(cloud.get("error_type") or "").strip()
    if error_type:
        meta.append(f"错误类型 {error_type}")
    if meta:
        lines.append("［附属］" + "｜".join(meta))
    if transcript:
        lines.append(f"［转述］{transcript}")
    raw_response = str(cloud.get("raw_response") or "").strip()
    if raw_response:
        lines.append(f"［原始响应］{raw_response}")
    return "\n".join(lines), CLOUD_RAW_ON_DISK


def _task_failure(message: str, *, bridge_error: str | None = None,
                  exit_code: int | None = None, source: str = "discuss") -> "DiscussError":
    """造一个任务级失败异常，并把「原文」随异常带出（调用方不必解析文案）。

    bridge_error：
      · None → 不是桥侧失败，原文就是 str(e) 这条诊断；
      · ""   → 是桥侧失败但原文缺失（job.error 与 进度.json 都空）→ 走冻结文案兜底；
      · 非空 → 桥侧原文（原样写进 failed_reason，禁改写）。
    """
    err = DiscussError(message)
    err.bridge_error = bridge_error
    err.exit_code = exit_code
    err.fail_source = source
    return err


def _bridge_failure(rec: dict) -> tuple[str, int | None]:
    """从 /api/job 的 error 记录取（桥侧原文, 退出码）——不改写原文，只归一取字段。"""
    raw = rec.get("error")
    if isinstance(raw, dict):
        text = str(raw.get("message") or raw.get("error") or "").strip()
    else:
        text = str(raw or "").strip()
    if not text:
        text = str(rec.get("stderr_tail") or rec.get("detail") or "").strip()
    code: int | None = None
    m = _BRIDGE_EXIT_RE.search(text)
    if m:
        code = int(m.group(1))
    elif isinstance(rec.get("exit"), int):
        code = int(rec["exit"])
    return text, code


def _progress_failure(bridge_id: str) -> tuple[str, int | None]:
    """读桥侧 进度.json 的失败标记（analyze.py 唯一写者）→ (error, 退出码)。

    只在 job 记录丢了（服务重启 → JOBS 内存态丢失）或 job.error 为空时用；
    进度.json 不是「失败」阶段一律返回 ("", None)——正在跑的任务不许被判失败。
    """
    tid = str(bridge_id or "").strip()
    if not tid:
        return "", None
    try:
        data = json.loads((_ANALYSIS_ROOT / tid / "进度.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return "", None
    if not isinstance(data, dict) or str(data.get("stage") or "") != "失败":
        return "", None
    text = str(data.get("error") or "").strip()
    code = data.get("退出码")
    if not isinstance(code, int):
        m = _PROGRESS_EXIT_RE.search(text)
        code = int(m.group(1)) if m else None
    return text, code


def _fail_task(task_dir: Path, reason: str, *, source: str = "discuss",
               exit_code: int | None = None, cloud_error: dict | None = None) -> dict:
    """任务级失败 → failed 的唯一写入口（discuss 侧）。返回 {changed, already?, reason}。

    · 原文优先：桥侧失败（source=bridge_exit）先读 `拆解错误.json` 的云端原文——
      读到 → failed_reason 主位「云端原文：…」（转述降为附属，见 _failure_detail）；
      读不到 → 原转述照旧 + 「云端原文：未落盘」；两者都没有（reason 为空）才用
      AT-18 冻结文案兜底，本模块**不新造**第二套用户可见文案；
    · 云端原文**逐字透传**进 failed_evidence.cloud_error（冻结 schema 原样，不截断/不摘要/
      不重判），failed_evidence.cloud_raw 标 on_disk/missing 供前端直接取，不必解析文案；
    · 写盘委托 `state.write_failed`（AT-19 §4.3 唯一写盘核）：进程间文件锁 + run_id 栅栏 +
      起态准入都在那里，本函数只管「取哪一句原文」；
    · 幂等：已是 failed 且原因相同 → 不动盘（不写 history、不刷新 failed_at）——同一次失败
      被重复触发不会产生第二次状态迁移；先按兜底句判死、随后拿到原文 → 只升级原因。
    """
    task_dir = Path(task_dir)
    task = state.load(task_dir)
    cur = str(task.get("state") or "")
    bridge_id = str(task.get("bridge_id") or "").strip()
    src = str(source or "discuss")
    # 云端原文只在桥侧失败路径上查：其它失败类型（超时/产物不全）若读同一目录，会把上一次
    # 尝试遗留的 拆解错误.json 错挂到本次失败上——那是新的「猜原因」，宁可标「未落盘」。
    if isinstance(cloud_error, dict):
        cloud = cloud_error
    else:
        cloud = _cloud_error(bridge_id) if src == "bridge_exit" else None
    text, cloud_raw = _failure_detail(bridge_id, str(reason or ""), exit_code, cloud)
    fallback = not text
    if fallback:
        text = state.fake_run_reason(cur or "discussing")
    probe: dict = {"fallback_text": fallback, "cloud_raw": cloud_raw}
    if cloud is not None:
        probe["cloud_error"] = dict(cloud)
    res = state.write_failed(task_dir, reason=text, source=src,
                             exit_code=_exit_code_of(text, exit_code),
                             run_id=os.environ.get("WANREPRO_EXEC_RUN_ID", ""),
                             probe=probe)
    out = dict(res or {})
    out["reason"] = str(out.get("reason") or text)
    return out


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

    # 409 = 拆解槽被占（排队语义，不是失败）。文案走 remote_wait 唯一源：
    # 说清「在等谁的远程返回 / 已等了多久 / 预计还需多久」，不再给用户刷「第 N 次失败」。
    # 日志节奏：首次必写一行，之后每 60s 追一行（与等待心跳同频）——5s 一次会把日志刷成计数墙。
    bridge_id = str(task.get("bridge_id") or "")
    busy_waited = 0.0
    last_slot: dict | None = None
    emit_every = max(1, int(round(60 / float(BUSY_RETRY_INTERVAL))))
    for attempt in range(BUSY_RETRY_MAX):
        try:
            out = bridge.post_json("/api/lapian/run",
                                   {"id": task["bridge_id"], "intent": task.get("intent", "")})
            job = out.get("job")
            if not job:
                raise DiscussError(f"lapian/run 未回 job: {json.dumps(out, ensure_ascii=False)[:200]}")
            break
        except bridge.BridgeError as e:
            if e.status == 409 or "HTTP 409" in str(e):
                payload = e.payload or {}
                slot = payload.get("slot") if isinstance(payload.get("slot"), dict) else {}
                last_slot = slot or None
                line = remote_wait.busy_line(slot, bridge_id=bridge_id, waited=busy_waited,
                                             task_busy=str(payload.get("task_id") or ""))
                if attempt % emit_every == 0:
                    _emit(line)
                print(f"  [排队] {line}（第 {attempt + 1}/{BUSY_RETRY_MAX} 次探测，"
                      f"{BUSY_RETRY_INTERVAL}s 后复查）")
                time.sleep(BUSY_RETRY_INTERVAL)
                busy_waited += BUSY_RETRY_INTERVAL
                continue
            raise
    else:
        raise SlotBusy(remote_wait.busy_exhausted_reason(last_slot, bridge_id=bridge_id,
                                                         waited=busy_waited))
    _emit(f"已提交拆解（job={job}），远程模型通常 5–15 分钟，慢时更久，不用重开")

    waited = 0
    last_stage = ""
    # 间隔一律走「env 覆盖 ∨ 模块常量」：默认（无 env）与进程化之前逐字一致；
    # 显式 heartbeat_interval 参数优先级最高（单元级探针直接传参，不经 env）。
    poll_interval = _env_seconds("WANREPRO_POLL_INTERVAL", POLL_INTERVAL)
    beat_every = (_env_seconds("WANREPRO_HEARTBEAT_INTERVAL", HEARTBEAT_INTERVAL)
                  if heartbeat_interval is None else float(heartbeat_interval))
    t0 = time.monotonic()
    next_beat = beat_every
    while waited < POLL_MAX_SECONDS:
        try:
            rec = bridge.get(f"/api/job/{job}")
        except bridge.BridgeError as e:
            if "HTTP 404" in str(e):
                # 服务重启过（JOBS 丢）——产物优先：产物在就判完成；否则看桥侧进度文件
                if _products_ready(task["bridge_id"]):
                    print("  [恢复] job 404 但产物齐——判已完成")
                    return job
                text, code = _progress_failure(task["bridge_id"])
                if text:
                    raise _task_failure(f"桥侧拆解失败（原文）: {text}",
                                        bridge_error=text, exit_code=code,
                                        source="bridge_exit") from None
                raise _task_failure(f"job 丢失（桥重启）且产物未齐: {e}") from None
            raise
        st = rec.get("status")
        if st == "done":
            _emit(f"拆解完成（耗时 {rec.get('elapsed', '?')}s）")
            _emit_gate_summary(task, on_stage=_emit)
            return job
        if st == "error":
            text, code = _bridge_failure(rec)
            if not text:
                # job.error 空 → 结构化备用通道（analyze.py 写的 进度.json）
                text, code = _progress_failure(task["bridge_id"])
            _emit(f"失败：{text or '桥侧未记下原因'}")
            raise _task_failure(f"桥侧拆解失败（原文）: {text}" if text else "桥侧拆解失败（原文缺失）",
                                bridge_error=text, exit_code=code, source="bridge_exit")
        stage = str(rec.get("stage") or "")
        if stage and stage != last_stage:
            last_stage = stage
            _emit(f"{stage}（已 {rec.get('elapsed', waited)}s）")
        time.sleep(poll_interval)
        waited += poll_interval
        # 心跳只在**等待期**发：终态分支在上面已 return，所以 done/error 之后不会有心跳补发
        if beat_every > 0:
            elapsed = time.monotonic() - t0
            if elapsed >= next_beat:
                _beat(elapsed)
                next_beat = elapsed + beat_every      # 一次只补一行，不追赶补发
    raise _task_failure(f"拆解超 {POLL_MAX_SECONDS}s 未完成（job={job}）——按失败落盘，可重试续查"
                        "（重跑产物优先，不再盲重发）")


def run_discuss(task_dir: Path, *, verbose: bool = True, on_stage=None, on_heartbeat=None) -> str:
    """执行拆解到 awaiting_assets。返回终态。

    on_stage(msg)：阶段回调透传给 start_and_wait（hub 工作台落日志/回写状态）。
    on_heartbeat(msg)：远程等待心跳回调，透传给 start_and_wait（只追加日志行，不碰阶段/锚）。

    失败语义分级（审计 WARN「busy=排队不记 failed」同类）：
      · 任务级失败（桥报 error / done 但产物缺 / job 丢失且产物未齐 / 轮询超时）
        → failed 终态：failed_reason 写**桥侧原文**（job.error 的 stderr 尾部，或
          进度.json 的结构化 error），原文缺失才用 AT-18 冻结文案兜底；history 记同一句；
      · 环境级故障（桥连不上 / 非 404 的 HTTP 故障）→ 状态不迁移（留 ingested/discussing），
        重跑本命令即续——环境恢复不该把任务判死。
    写盘职责：本函数只写 `任务.json`（state.transition / _fail_task），进度.json 归桥侧
    analyze.py，两边不互相覆盖，故同一次失败只产生一条 failed 迁移。
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
    except SlotBusy as e:
        # 槽占用等到上限 = 本轮**没提交上去**（环境级，不是远程判失败）：不写 failed，
        # 状态留在 discussing，稍后再点重试即续。与「远程返回了但失败」（下一支 → failed）
        # 严格两分——「本任务没有被远程判失败」这句话必须与磁盘上的状态一致。
        if verbose:
            print(f"[排队超时] {task['local_id']}: {e} —— 状态保留 discussing，稍后重试续")
        raise
    except DiscussError as e:
        # 任务级失败：唯一写入口落盘——桥侧原文优先，缺失才用冻结文案兜底；
        # 幂等（同一失败重复触发不再迁移），并只写 任务.json（进度.json 归桥侧）。
        raw = getattr(e, "bridge_error", None)
        info = _fail_task(task_dir,
                          str(e) if raw is None else str(raw or ""),
                          source=str(getattr(e, "fail_source", "") or "discuss"),
                          exit_code=getattr(e, "exit_code", None))
        if verbose:
            print(f"[FAIL] {task['local_id']}: → failed（原因：{info.get('reason')}）")
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
