#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dissect_gate.py — 拆解产物质量门禁（D1–D7）＋自动重拆驱动（AT-15 R1）。

与 gate.py 同一套范式（PASS/FAIL/WARN 清单 + 退出码），一处根本区别：**拆解不花钱，
所以这里不签 token**。gate.py 问的是「这一条能不能花钱发出去」；本门禁问的是
「这份拆解产物下游（asset_cards / 提示词）能不能用」。

判据全部落在下游真正消费的东西上（红线 25：不新造判定逻辑，判据与消费同源）：
  D1 分镜表可解析  confirm.parse_shot_times（严格闸门）＋ cards._relaxed_bounds（宽松复算）
  D2 画面位置覆盖   cards.build 的「画面位置」（侧车同源）；必换行缺 → FAIL，覆盖 <6/8 → WARN
  D3 代表帧非兜底   cards.build 的「代表帧.来源 == 中段兜底」，占比 >20% → FAIL
  D4 框覆盖         cards.build 的「自身框」；必换行缺自身框 → FAIL
  D5 同图跨卡唯一   cards.build 的「代表帧.url」全同值 ∧ 全兜底 → FAIL
  D6 十层标题齐     analyze.local_layer_check（调用方传入；CLI 自己按路径加载）
  D7 逐行空时段     「出现时段」空 ∧「画面位置」空 ∧ 无精确定位帧 → 该行 WARN；>2 行 → FAIL

自动重拆（本模块驱动，重跑动作由调用方注入）：
  门禁 FAIL → build_diagnosis() 组装诊断（【…】非标题形态，红线 24）→ rework(诊断)
  （analyze.py 侧只重跑 视觉模型+提取+清单+意图+渲染，实测/抽帧不重跑）→ 再过门禁。
  计数落盘 `门禁.json`：按会话键 session_key 续算（断点续跑不重置），上限默认 2 次；
  到顶仍 FAIL → 状态「到顶标红」，产物照常交付，缺项写在门禁结果里，绝不静默拦截。

进度留痕：每次门禁轮次变化都经 stage 回调透出（analyze.py 接 mark_stage → 进度.json →
hub 既有阶段记录器追加「拆解日志.txt」＋回写任务.json 的 lapian_stage；用户要求拆解显进度）。

用法（只判不重拆，排查/CI 用）：
    py -3 pipeline/reverse/dissect_gate.py --outdir analysis/reverse/<id> [--json]
退出码：0 无阻断项（可带 WARN）｜1 有阻断项（打印清单）｜2 输入不成立（缺拆解.md）
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]          # wanrepro/
REVERSE = Path(__file__).resolve().parent
GATE_NAME = "门禁.json"
GATE_SCHEMA = "at15-dissect-gate-v1"
MAX_ROUNDS = 2
FALLBACK_SOURCE = "中段兜底"
MUST_REPLACE = "必须换"
# 十 图文与收束的品牌位没有实体位置可指（置换位是客户字样，不是画面里的东西）：
# 任务书不要求它写「画面位置：九宫格N·镜M」，门禁也就不拿它判 D2/D4——否则每条有品牌位的
# 片子都会恒 FAIL，红色横条变成噪音（红线：判据要判下游真正会错的东西）。
BRAND_CATEGORY = "品牌视觉"

P, F, W = "PASS", "FAIL", "WARN"

# 诊断提示：一条判据一句「怎么改」，交给下一轮视觉模型直接照做（写法指导，非禁令）。
DIAGNOSIS_HINTS = {
    "D0": "「拆解.md」与「待确认清单.md」都要落盘且非空，缺哪个补哪个。",
    "D1": "「七 分镜表」每镜一行、起秒-止秒写成 MM:SS-MM:SS 且可解析（末镜右端≈片长、不越界）；"
          "片尾品牌定格也单列一镜，不要整片只给一镜。",
    "D2": "每一条必换资产（建议=必须换）的末尾都要写「画面位置：九宫格N·镜M」，格取 1-9、"
          "镜号必须是分镜表里真实存在的镜。",
    "D3": "代表帧兜底 = 分镜表或画面位置读不出：先保证分镜表可解析（见 D1），"
          "再保证每条资产都有「出现于：镜N」或「出现于：MM:SS-MM:SS」。",
    "D4": "自身框缺失 = 该行画面位置没指向代表镜：画面位置的镜号要与这件东西实际出现的镜一致，"
          "格号 1-9 不越界。",
    "D5": "所有卡同图 = 分镜表整表被拒后全落中段兜底：按 D1 把分镜表写可解析，"
          "不允许整片只给一镜或时间码缺失。",
    "D6": "十层标题（一~十）必须原样齐全，每层正文不许为空。",
    "D7": "逐行空时段：每条资产都要写「出现于」，判断不出就写「出现于：未标注」，不许省略这一段。",
}


# ---------------------------------------------------------------- 小工具

def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _cards():
    """下游卡片组装件（同一个 cards.build，hub 与门禁判的是同一份东西）。"""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from automation import cards as module
    return module


def _default_layer_check():
    """D6 缺省实现：照 analyze.local_layer_check 的既有函数（不另写一套正则）。"""
    analyze_path = ROOT / "analyze.py"
    spec = importlib.util.spec_from_file_location("wanrepro_root_analyze", analyze_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    extract_spec = importlib.util.spec_from_file_location("wanrepro_reverse_extract",
                                                          REVERSE / "extract.py")
    extract_mod = importlib.util.module_from_spec(extract_spec)
    extract_spec.loader.exec_module(extract_mod)
    return lambda disc: module.local_layer_check(Path(disc), extract_mod)


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")


def short_ids(result: dict) -> str:
    """缺项 id 列表（日志一行能放下）。"""
    return "、".join(item["id"] for item in result.get("checks") or []
                    if item["verdict"] == F) or "无"


# ---------------------------------------------------------------- 判据

def evaluate(outdir: Path, *, bridge_id: str | None = None, local_id: str | None = None,
             layer_check=None) -> dict:
    """跑 D0/D1–D7，返回 {verdict, checks[{id,verdict,msg}], 缺项[], 兜底, 行数}。

    纯读：不写盘、不调模型、不改产物。判据对象 = cards.build()（下游同一份），
    只有 D1 额外调 confirm 的两条解析路，用来区分「严格可解析」与「宽松复算兜住」。
    """
    outdir = Path(outdir)
    bridge = bridge_id or outdir.name
    local = local_id or bridge
    cards = _cards()
    confirm = cards._confirm()

    disc = outdir / "拆解.md"
    checklist = outdir / "待确认清单.md"
    checks: list[dict] = []

    def add(cid: str, verdict: str, msg: str) -> None:
        checks.append({"id": cid, "verdict": verdict, "msg": msg})

    # D0 产物齐备（结构前置：后面几项都以这两份为输入）
    missing = [name for name, path in (("拆解.md", disc), ("待确认清单.md", checklist))
               if not path.is_file() or path.stat().st_size <= 0]
    if missing:
        add("D0 产物", F, "缺 " + "、".join(missing) + "（下游没有可判的东西）")
        return _finish(checks, built=[], markdown="")

    markdown = disc.read_text(encoding="utf-8-sig", errors="replace")
    try:
        built = cards.build(bridge, local)
    except Exception as exc:                       # noqa: BLE001 —— 组装炸了也是产物缺陷，如实记
        built = []
        add("D0 产物", F, f"cards.build 组装失败：{type(exc).__name__}: {exc}")
    else:
        add("D0 产物", P, f"拆解.md {len(markdown)} 字｜待确认清单 {len(built)} 行")

    # 片长与分镜表（与 cards.build 内部同一读法：视频 ffprobe 优先，退化为拆解头部）
    video = cards.frames.video_path(bridge)
    total = cards.frames.duration(video) or (confirm.probe_duration(markdown) or 0.0)
    layers = cards._split(markdown)
    shots_layer = layers.get("七 分镜表", "")
    strict = confirm.parse_shot_times(shots_layer, total or None) if shots_layer else {}
    bounds = cards.shot_bounds(shots_layer, total) if shots_layer else {}

    # D1 分镜表可解析
    if not shots_layer.strip():
        add("D1 分镜表", F, "缺「七 分镜表」层或该层为空")
    elif not bounds:
        add("D1 分镜表", F,
            f"严格与宽松都读不出镜时段（严格={strict or '空'}；片长 {total or '未测'}s）"
            "——代表帧只能全落中段兜底")
    else:
        add("D1 分镜表", P,
            f"{len(bounds)} 镜可解析（{'严格闸门' if strict else '宽松复算'}）；片长 {total:.2f}s")

    rows = list(built)
    by_no = {card["n"]: card for card in rows}

    def pos_of(card: dict):
        pos = card.get("画面位置")
        return pos if isinstance(pos, dict) and pos.get("格") and pos.get("镜次") else None

    def is_must(card: dict) -> bool:
        return MUST_REPLACE in str(card.get("建议") or "")

    def judge(card: dict) -> bool:
        """D2/D4 的判定对象：主列表里的九资产行（必换 ∪ 核心，品牌位剔除）。"""
        return (not str(card.get("类别") or "").startswith(BRAND_CATEGORY)
                and (bool(card.get("核心")) or is_must(card)))

    # D2 画面位置覆盖：必换/主列表行缺 → FAIL；九资产行覆盖率 < 6/8 → WARN
    must = [card for card in rows if judge(card)]
    must_missing = [card["n"] for card in must if pos_of(card) is None]
    asset_rows = [card for card in rows
                  if not str(card.get("类别") or "").startswith(BRAND_CATEGORY)]
    covered = [card for card in asset_rows if pos_of(card) is not None]
    if must_missing:
        add("D2 画面位置", F,
            f"主列表行缺画面位置：第 {'、'.join(map(str, must_missing))} 行"
            f"（判定 {len(must)} 行 / 共 {len(rows)} 行，覆盖 {len(covered)} 行）")
    elif asset_rows and len(covered) * 8 < len(asset_rows) * 6:
        add("D2 画面位置", W,
            f"覆盖 {len(covered)}/{len(asset_rows)} 行（<6/8）——单行缺口走复检清单，不触发重拆")
    else:
        add("D2 画面位置", P,
            f"主列表 {len(must)} 行全有画面位置｜九资产覆盖 {len(covered)}/{len(asset_rows)} 行")

    # D3 代表帧非兜底：兜底占比 > 20% → FAIL
    fallback = [card["n"] for card in rows
                if str((card.get("代表帧") or {}).get("来源") or "") == FALLBACK_SOURCE]
    share = (len(fallback) / len(rows)) if rows else 0.0
    if rows and share > 0.2:
        add("D3 代表帧", F,
            f"中段兜底 {len(fallback)}/{len(rows)} 行（{share:.0%} > 20%）："
            f"第 {'、'.join(map(str, fallback))} 行没有可用镜/时段")
    else:
        add("D3 代表帧", P, f"中段兜底 {len(fallback)}/{len(rows)} 行（≤20%）" if rows else "无行可判")

    # D4 框覆盖：主列表行缺自身框 → FAIL
    no_own = [card["n"] for card in must if not card.get("自身框")]
    if no_own:
        add("D4 自身框", F, f"主列表行缺自身框：第 {'、'.join(map(str, no_own))} 行"
                            "（画面位置没指向代表镜或格越界）")
    else:
        add("D4 自身框", P, f"主列表 {len(must)} 行自身框齐全")

    # D5 同图跨卡唯一：URL 全同值 ∧ 全兜底 → FAIL（S1 已防，判据固化）
    urls = {str((card.get("代表帧") or {}).get("url") or "") for card in rows} - {""}
    all_fallback = bool(rows) and len(fallback) == len(rows)
    if len(rows) >= 2 and len(urls) == 1 and all_fallback:
        add("D5 同图唯一", F,
            f"{len(rows)} 卡代表帧同一张图（{next(iter(urls))}）且全为兜底——分镜表整表被拒的现场")
    else:
        add("D5 同图唯一", P, f"代表帧 {len(urls)} 个不同值（{len(rows)} 行）")

    # D6 十层标题齐
    check = layer_check or _default_layer_check()
    try:
        ok6, detail6 = check(disc)
    except Exception as exc:                      # noqa: BLE001
        ok6, detail6 = False, f"{type(exc).__name__}: {exc}"
    add("D6 十层", P if ok6 else F, detail6)

    # D7 逐行空时段：时段空 ∧ 位置空 ∧ 无精确定位帧 → 该行 WARN；>2 行 → FAIL
    empty_rows = []
    for card in rows:
        exact = any(str(frame.get("定位") or "") == "精确" and frame.get("url")
                    for frame in (card.get("帧") or []))
        if not (card.get("出现时段") or []) and pos_of(card) is None and not exact:
            empty_rows.append(card["n"])
    if len(empty_rows) > 2:
        add("D7 空时段", F, f"{len(empty_rows)} 行时段/位置/精确帧全空：第 "
                            f"{'、'.join(map(str, empty_rows))} 行")
    elif empty_rows:
        add("D7 空时段", W, f"第 {'、'.join(map(str, empty_rows))} 行时段与位置全空"
                            "（单行缺口走复检清单，不触发重拆）")
    else:
        add("D7 空时段", P, "无时段/位置全空的行")

    return _finish(checks, built=rows, markdown=markdown,
                   extra={"兜底": len(fallback), "行数": len(rows),
                          "兜底行": fallback, "空时段行": empty_rows})


def _finish(checks: list[dict], *, built: list, markdown: str, extra: dict | None = None) -> dict:
    fails = [item for item in checks if item["verdict"] == F]
    warns = [item for item in checks if item["verdict"] == W]
    verdict = F if fails else (W if warns else P)
    result = {
        "verdict": verdict,
        "checks": checks,
        "缺项": [f"{item['id']}：{item['msg']}" for item in fails],
        "告警": [f"{item['id']}：{item['msg']}" for item in warns],
        "行数": len(built),
        "拆解哈希": hashlib.sha256(markdown.encode("utf-8")).hexdigest()[:16] if markdown else "",
    }
    result.update(extra or {})
    return result


def build_diagnosis(result: dict) -> str:
    """门禁结果 → 下一轮视觉模型的诊断块（非标题形态，红线 24：不得出现平级/下辖标题）。"""
    lines = ["【门禁诊断（上一版拆解产物的缺项：这一版必须修掉；十层标题、顺序、数量原样不动，"
             "不许新增标题、不许加节）】"]
    for item in result.get("checks") or []:
        if item["verdict"] != F:
            continue
        hint = DIAGNOSIS_HINTS.get(item["id"][:2], "")
        lines.append(f"- {item['id']}：{item['msg']}" + (f" —— {hint}" if hint else ""))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- 状态（计数落盘）

def load_state(outdir: Path, session_key: str) -> dict:
    """读门禁状态；会话键变了（换了片子/意图/模型）→ 计数归零，历史清空。"""
    state = _read_json(Path(outdir) / GATE_NAME)
    if state.get("schema") != GATE_SCHEMA or str(state.get("会话") or "") != str(session_key):
        return {"schema": GATE_SCHEMA, "会话": str(session_key), "轮次": 0, "上限": MAX_ROUNDS,
                "历史": []}
    if not isinstance(state.get("历史"), list):
        state["历史"] = []
    return state


def save_state(outdir: Path, state: dict) -> Path:
    path = Path(outdir) / GATE_NAME
    _write_json(path, state)
    return path


# ---------------------------------------------------------------- 驱动（门禁 + 重拆）

def run(outdir: Path, *, bridge_id: str | None = None, local_id: str | None = None,
        session_key: str = "", rework=None, max_rounds: int = MAX_ROUNDS,
        layer_check=None, stage=None) -> dict:
    """门禁 + 自动重拆驱动。返回最终门禁结果（含状态、轮次、缺项）。

    rework(diagnosis) -> tuple[bool, str]：调用方注入的重跑动作（analyze.py 里就是
    「只重跑视觉模型+提取+清单+意图+渲染」）。不给 rework = 只判不重拆（CLI/探针）。
    计数在**每轮门禁后**落盘：进程被杀/任务续跑都不会把已用轮次清零重来。
    """
    outdir = Path(outdir)
    emit = stage or (lambda message: None)
    state = load_state(outdir, session_key)
    rounds = int(state.get("轮次") or 0)
    state["上限"] = int(max_rounds)
    result = evaluate(outdir, bridge_id=bridge_id, local_id=local_id, layer_check=layer_check)

    while True:
        index = rounds + 1
        history = state.setdefault("历史", [])
        history.append({"轮": index, "verdict": result["verdict"], "缺项": result["缺项"],
                        "ts": _now()})
        if result["verdict"] == F:
            emit(f"门禁第{index}轮：FAIL {short_ids(result)}")
        elif result["verdict"] == W:
            emit(f"门禁第{index}轮：PASS（{len(result['告警'])} 项告警，走复检清单）")
        else:
            emit(f"门禁第{index}轮：PASS")

        if result["verdict"] != F:
            break
        if rework is None or rounds >= max_rounds:
            break
        rounds += 1
        state["轮次"] = rounds
        state["结果"] = result
        save_state(outdir, state)                        # 先落盘再重跑：中途崩溃也不退轮次
        emit(f"重拆第{rounds}/{max_rounds}次：按门禁诊断重跑视觉模型与提取（实测/抽帧不重跑）")
        try:
            ok, detail = rework(build_diagnosis(result))
        except Exception as exc:                        # noqa: BLE001 —— 重拆炸了要如实标红
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        if not ok:
            emit(f"重拆第{rounds}次失败：{detail}——产物按上一版交付，门禁标红")
            state["结果"] = result
            state["状态"] = "重拆失败·标红"
            state["重拆错误"] = str(detail)[:400]
            save_state(outdir, state)
            return _stamped(result, outdir, state, rounds, "重拆失败·标红")
        result = evaluate(outdir, bridge_id=bridge_id, local_id=local_id,
                          layer_check=layer_check)

    if result["verdict"] == F:
        status = "到顶标红" if rounds >= max_rounds else "标红"
    elif result["verdict"] == W:
        status = "重拆后通过（有告警）" if rounds else "通过（有告警）"
    else:
        status = "重拆后通过" if rounds else "通过"
    state["轮次"] = rounds
    state["结果"] = result
    state["状态"] = status
    save_state(outdir, state)
    return _stamped(result, outdir, state, rounds, status)


def _stamped(result: dict, outdir: Path, state: dict, rounds: int, status: str) -> dict:
    out = dict(result)
    out.update({"状态": status, "轮次": rounds, "上限": state.get("上限", MAX_ROUNDS),
                "门禁文件": str(Path(outdir) / GATE_NAME), "历史": list(state.get("历史") or [])})
    return out


def render(result: dict) -> str:
    """门禁结果 → 人看的一小段（步骤输出 / 日志用）。"""
    head = (f"拆解产物门禁｜{len(result.get('checks') or [])} 项："
            f"{len(result.get('缺项') or [])} FAIL / {len(result.get('告警') or [])} WARN"
            f"｜状态 {result.get('状态')}｜轮次 {result.get('轮次')}/{result.get('上限')}")
    lines = [head]
    for item in result.get("checks") or []:
        lines.append(f"  [{item['verdict']}] {item['id']} — {item['msg']}")
    for item in result.get("缺项") or []:
        lines.append(f"  ❌ {item}")
    return "\n".join(lines)


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="拆解产物质量门禁（D1–D7；不签 token、不花钱）")
    ap.add_argument("--outdir", required=True, help="拆解任务目录 analysis/reverse/<id>")
    ap.add_argument("--bridge", default=None, help="桥 id（默认取 outdir 目录名）")
    ap.add_argument("--session", default="", help="会话键（缺省按 outdir 路径算）")
    ap.add_argument("--json", action="store_true", help="只吐 JSON（机器读）")
    args = ap.parse_args(argv)
    outdir = Path(args.outdir).expanduser()
    if not (outdir / "拆解.md").is_file():
        print(f"ERROR 不是拆解任务目录（缺 拆解.md）：{outdir}", file=sys.stderr)
        return 2
    bridge = args.bridge or outdir.name
    session = args.session or hashlib.sha256(str(outdir.resolve()).encode()).hexdigest()[:16]
    state = load_state(outdir, session)
    result = evaluate(outdir, bridge_id=bridge, local_id=bridge)
    state.setdefault("历史", []).append({"轮": int(state.get("轮次") or 0) + 1,
                                        "verdict": result["verdict"], "缺项": result["缺项"],
                                        "ts": _now(), "来源": "cli"})
    state["结果"] = result
    state["状态"] = "通过（CLI 只判，未跑重拆）" if result["verdict"] != F else "标红（CLI 只判）"
    save_state(outdir, state)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render(result))
        print(f"记录：{outdir / GATE_NAME}")
    return 0 if result["verdict"] != F else 1


if __name__ == "__main__":
    raise SystemExit(main())
