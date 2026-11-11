#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AT-15 R2 探针：任务书 v2.5（每行位置正例 + 片尾分镜正例）＋ 门禁结果进自检面板。

跑法：`wanrepro/` 下 `python -m automation.probe_at15_r2`（纯离线：无 HTTP、不调模型、
零花钱）。判据全对才 exit 0，任何一条 FAIL 如实打印并 exit 1。

判据：
  [1] 任务书 v2.5 合同（红线 24：只动九资产层/分镜表层行内指引）——frontmatter
      version=2.5；正文围栏内 `^## ` 标题集合与 extract.LAYER_TITLES 逐字一致
      （无平级标题新增/改名）；七/九两层各含 v2.5 关键锚点；{{镜头语言契约}} 占位
      原样保留（read_prompt 契约注入不受影响）。
  [2] 正例必须能被真实解析器吃下（写法指导的前提是下游真认）——
      a. 九资产两条完整行样例：confirm.asset_entries 解析出 名字/出现于/画面位置，
         spans_from_appearance 复合写法取显式时间码，entry_position 取 格/镜次。
      b. 分镜表末镜正例行：拼真实表头喂 confirm.parse_shot_times —— 末镜止 00:06
         与片长 6.0s 过 _spans_plausible 闸门（末段≈片长 ±0.5s）；pop-1 形态
         （止 00:05、片长 6.04s）严格闸门拒收（事故形态回归锚）。
  [3] 门禁结果进自检面板（automation/selfcheck 读路径汇总）——
      门禁.json PASS/FAIL/损坏三态 → load_gate_item 的 项/结论/证据（绿项数、
      重拆 x/y、缺项 ❌ 前缀）；load_selfcheck 无自检.json 也透出门禁条目、
      有自检.json 追加不覆盖；无门禁.json 行为与旧版一致。
  [4] 真数据只读 + 零残留：pop-1 四件 + auto-0036 任务.json 的 SHA256 前后不变，
      本探针全部产物落 tempfile 并清理。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]          # wanrepro/
sys.path.insert(0, str(ROOT))
from automation import selfcheck  # noqa: E402

PROMPT_PATH = ROOT / "prompts" / "视频拆解.md"
REAL_POP1 = ROOT / "analysis" / "reverse" / "pop-1"
REAL_TASK = ROOT / "automation" / "projects" / "auto-0036" / "任务.json"
POP1_SNAPSHOT = ("拆解.md", "待确认清单.md", "待确认清单.json", "意图建议.md")

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f" —— {detail}" if detail and not cond else ""))
    return bool(cond)


def module_by_path(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prompt_body() -> str:
    """正文围栏内容（extract.read_prompt 同口径：第一个 ``` 围栏）。"""
    text = PROMPT_PATH.read_text(encoding="utf-8")
    match = re.search(r"```(?:\w+)?\r?\n(.*?)\r?\n```", text, re.S)
    assert match, "提示词正文围栏缺失"
    return match.group(1)


# ---------------------------------------------------------------- [1] 任务书合同

def probe_prompt_contract() -> None:
    print("[1] 任务书 v2.5 合同（结构不变 + 锚点在场）")
    raw = PROMPT_PATH.read_text(encoding="utf-8")
    check("frontmatter version=2.5",
          bool(re.search(r"^version:\s*2\.5\s*$", raw, re.M)),
          "frontmatter 没有 version: 2.5")
    body = prompt_body()
    extract = module_by_path("r2_extract", ROOT / "pipeline" / "reverse" / "extract.py")
    titles = [f"## {title}" for title in extract.LAYER_TITLES]
    found = [line.strip() for line in body.splitlines() if line.strip().startswith("## ")]
    # 历史事实：正文围栏延伸到文件尾，body 里还有三个定版依据段（赛马实测/接口纪律/下游消费）
    # ——它们在 v2.4.0 就存在且发给模型，不算结构变化；判据 = 十层标题逐字一致 + 没有任何新标题。
    meta_tail = ["## 赛马实测记录（2026-10-29）", "## 接口纪律（踩过的坑）",
                 "## 下游怎么消费（渲染器不要碰 JSON 键名）"]
    check("正文平级标题 == 十层标题 + 3 个定版段（无新增/改名，红线 24）",
          found == titles + meta_tail, f"实际 {found}")
    ten_region = body.split(meta_tail[0], 1)[0]
    region_titles = [line.strip() for line in ten_region.splitlines() if line.strip().startswith("## ")]
    check("十层区域内部只含十层标题", region_titles == titles, f"实际 {region_titles}")
    layer7 = body.split("## 七 分镜表", 1)[1].split("## 八 声音", 1)[0]
    layer9 = body.split("## 九 资产", 1)[1].split("## 十 图文与收束", 1)[0]
    check("七 分镜表含「整表覆盖到片尾」指引",
          "整表必须从 00:00 连续覆盖到片尾" in layer7)
    check("七 分镜表含末镜片尾定格正例（00:05-00:06）",
          bool(re.search(r"^\s*0?5\s*\|\s*00:05-00:06\s*\|", layer7, re.M)))
    check("七 分镜表点名 pop-1 事故形态（止秒比片尾短 → 整表判不可解析）",
          "pop-1" in layer7 and "判成不可解析" in layer7)
    check("九 资产含复合出现于格式（HH:MM-HH:MM / 镜N-M）",
          "出现于：HH:MM-HH:MM / 镜N-M" in layer9)
    check("九 资产含每行完整写法正例块",
          "每行完整写法正例" in layer9
          and bool(re.search(r"^\s*\*\s*\*\*.+（出现于：\d{2}:\d{2}-\d{2}:\d{2} / 镜[\d、\- ]+）\*\*：.+画面位置：九宫格\d·镜\d+\s*$",
                             layer9, re.M)))
    check("{{镜头语言契约}} 占位仍恰好一处（read_prompt 注入不受影响）",
          body.count("{{镜头语言契约}}") == 1)
    # read_prompt 真跑一次：占位被契约全文替换成功
    try:
        prompt = extract.read_prompt()
        ok = "{{镜头语言契约}}" not in prompt and len(prompt) > 5000
        check("extract.read_prompt() 契约替换成功", ok)
    except Exception as exc:                        # noqa: BLE001
        check("extract.read_prompt() 契约替换成功", False, f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------- [2] 正例可解析

SHOT_HEADER = ("镜号 | 起秒-止秒 | 运动主体 | 运动形态 | 速度 | 画面内容 | 光影变化 | "
               "转场方式 | 站位 | 朝向 | 视线 | 镜头运动 | 景别 | 情绪/表演")


def probe_samples_parse() -> None:
    print("[2] 正例必须能被真实解析器吃下")
    confirm = module_by_path("r2_confirm", ROOT / "pipeline" / "reverse" / "confirm.py")
    body = prompt_body()
    layer9 = body.split("## 九 资产", 1)[1].split("## 十 图文与收束", 1)[0]

    block = layer9.split("每行完整写法正例", 1)[1].split("【§9·画面位置】", 1)[0]
    bullets = confirm.bullets(block)
    sample_lines = [(label, text) for label, text in bullets
                    if "（出现于：" in label and "画面位置" in text]
    check("提取到两条完整行样例", len(sample_lines) == 2,
          f"实际提取 {len(sample_lines)} 条")
    shot_times = {"1": ("00:00", "00:02"), "2": ("00:02", "00:03"), "3": ("00:03", "00:05")}
    for label, text in sample_lines:
        kind, name, appear = confirm.parse_asset_entry(label, text)
        pos = confirm.entry_position(label, text)
        spans = confirm.spans_from_appearance(appear, shot_times)
        tag = name or "?"
        check(f"样例行解析：{tag} 名字/类别", kind in ("服装", "道具") and bool(name),
              f"kind={kind} name={name!r}")
        check(f"样例行解析：{tag} 复合出现于取显式时间码",
              bool(spans) and isinstance(spans[0], dict) and bool(spans[0].get("起")),
              f"appear={appear!r} spans={spans}")
        check(f"样例行解析：{tag} 画面位置 格1-9/镜次在场",
              bool(pos) and 1 <= pos["格"] <= 9 and pos["镜次"] > 0,
              f"pos={pos}")

    # 分镜表末镜正例：真实表头 + 提示词里的正例行
    layer7 = body.split("## 七 分镜表", 1)[1].split("## 八 声音", 1)[0]
    row = next((ln.strip() for ln in layer7.splitlines()
                if re.match(r"^\s*0?5\s*\|\s*00:05-00:06\s*\|", ln)), "")
    check("提取到末镜片尾定格正例行", bool(row), "没找到 05 | 00:05-00:06 行")
    table = "## 七 分镜表\n\n" + SHOT_HEADER + "\n" + row + "\n"
    strict = confirm.parse_shot_times(table, 6.0)
    check("末镜正例整表严格闸门可解析（末段 00:06 ≈ 片长 6.0s）",
          strict.get("5") == ("00:05", "00:06"), f"strict={strict}")
    # pop-1 形态回归锚：止 00:05、片长 6.04s → 严格闸门拒收
    pop1_row = ("04 | 00:03-00:05 | 核心产品瓶身 | 翻滚后悬浮 | 减速 | 瓶身居中 | "
                "高光带 | 无剪切 | 正中 | — | — | 固定机位 | 特写 | 无可见表情（无人物）")
    pop1_table = "## 七 分镜表\n\n" + SHOT_HEADER + "\n" + pop1_row + "\n"
    strict_pop1 = confirm.parse_shot_times(pop1_table, 6.04)
    check("pop-1 形态（止 00:05 / 片长 6.04s）严格闸门拒收（事故锚不被 v2.5 掩盖）",
          strict_pop1 == {}, f"strict={strict_pop1}")


# ---------------------------------------------------------------- [3] 门禁进自检面板

GATE_PASS = {
    "schema": "at15-dissect-gate-v1", "会话": "k1", "轮次": 0, "上限": 2, "状态": "通过",
    "结果": {
        "verdict": "PASS",
        "checks": [
            {"id": "D1 分镜表", "verdict": "PASS", "msg": "5 镜可解析"},
            {"id": "D6 十层", "verdict": "PASS", "msg": "十层齐全"},
        ],
        "缺项": [], "告警": [],
    },
}
GATE_FAIL = {
    "schema": "at15-dissect-gate-v1", "会话": "k2", "轮次": 2, "上限": 2, "状态": "到顶标红",
    "结果": {
        "verdict": "FAIL",
        "checks": [
            {"id": "D1 分镜表", "verdict": "FAIL", "msg": "读不出镜时段"},
            {"id": "D5 同图唯一", "verdict": "PASS", "msg": "2 个不同值"},
        ],
        "缺项": ["D1 分镜表：读不出镜时段"], "告警": [],
    },
}


def _write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def probe_gate_panel() -> None:
    print("[3] 门禁结果进自检面板（读路径汇总）")
    work = Path(tempfile.mkdtemp(prefix="probe-at15-r2-"))
    # selfcheck.ROOT 指向真实 wanrepro 根；探针里改指沙箱根（零残留，finally 还原）。
    real_root = selfcheck.ROOT
    selfcheck.ROOT = work / "wanrepro-root"
    try:
        bridge = "probe-r2"
        task_dir = work / "auto-r2"
        task_dir.mkdir(parents=True)
        _write(task_dir / "任务.json", {"local_id": "auto-r2", "bridge_id": bridge})
        gate_dir = selfcheck.ROOT / "analysis" / "reverse" / bridge
        gate_path = gate_dir / "门禁.json"

        _write(gate_path, GATE_PASS)
        item = selfcheck.load_gate_item(task_dir)
        check("PASS 门禁 → 条目 项=拆解门禁 结论=PASS",
              bool(item) and item["项"] == selfcheck.GATE_ITEM_NAME and item["结论"] == "PASS",
              f"item={item}")
        check("PASS 证据含绿项数 2/2 与重拆 0/2",
              bool(item) and "绿项 2/2" in item["证据"] and "重拆 0/2" in item["证据"],
              f"证据={item['证据']!r}" if item else "")
        check("PASS 证据无 ❌/⚠ 缺项行",
              bool(item) and "❌" not in item["证据"] and "⚠" not in item["证据"])

        _write(gate_path, GATE_FAIL)
        item = selfcheck.load_gate_item(task_dir)
        check("FAIL 门禁 → 结论=FAIL 且证据含缺项 ❌ 行",
              bool(item) and item["结论"] == "FAIL" and "❌ D1 分镜表：读不出镜时段" in item["证据"])
        check("FAIL 证据含重拆轮次 2/2 与状态 到顶标红",
              bool(item) and "重拆 2/2" in item["证据"] and "到顶标红" in item["证据"])

        gate_path.write_text("{broken", encoding="utf-8")
        item = selfcheck.load_gate_item(task_dir)
        check("门禁.json 损坏 → 如实标红（FAIL，不静默吞）",
              bool(item) and item["结论"] == "FAIL" and "读不出" in item["证据"])

        # load_selfcheck：无 自检.json 只有门禁 → 透出门禁条目；有 自检.json → 追加不覆盖
        _write(gate_path, GATE_PASS)
        rows = selfcheck.load_selfcheck(task_dir)
        check("无 自检.json 只有门禁 → 返回 1 条门禁条目",
              rows and len(rows) == 1 and rows[0]["项"] == selfcheck.GATE_ITEM_NAME)
        _write(task_dir / "自检.json", [{"项": "次序对账", "结论": "PASS", "证据": "ok"}])
        rows = selfcheck.load_selfcheck(task_dir)
        check("有 自检.json → 原 1 条 + 追加门禁 = 2 条，原条目不动",
              rows and len(rows) == 2 and rows[0]["项"] == "次序对账"
              and rows[1]["项"] == selfcheck.GATE_ITEM_NAME)

        other = work / "auto-r3"
        other.mkdir()
        _write(other / "任务.json", {"local_id": "auto-r3", "bridge_id": "no-such-bridge"})
        check("无 门禁.json → load_gate_item None（旧任务零回归）",
              selfcheck.load_gate_item(other) is None)
        check("无 任务.json → load_gate_item None（不炸读路径）",
              selfcheck.load_gate_item(work / "auto-none") is None)
    finally:
        selfcheck.ROOT = real_root
        shutil.rmtree(work, ignore_errors=True)


def probe_discuss_gate_log() -> None:
    print("[3b] 拆解日志补账：job done 后读 门禁.json 追门禁终态行")
    from automation import discuss
    real_root = discuss._ANALYSIS_ROOT
    work = Path(tempfile.mkdtemp(prefix="probe-at15-r2-log-"))
    discuss._ANALYSIS_ROOT = work / "analysis" / "reverse"
    try:
        _write(discuss._ANALYSIS_ROOT / "probe-r2" / "门禁.json", GATE_FAIL)
        line = discuss._gate_summary_line("probe-r2")
        check("门禁 FAIL → 补账行含 状态/绿项数/重拆轮次",
              "到顶标红" in line and "绿项 1/2" in line and "重拆 2/2" in line and "缺项 1" in line,
              f"line={line!r}")
        check("无 门禁.json → 补账行为空串（旧任务不写）",
              discuss._gate_summary_line("no-such-bridge") == "")
        bad_dir = discuss._ANALYSIS_ROOT / "bad"
        bad_dir.mkdir(parents=True, exist_ok=True)
        (bad_dir / "门禁.json").write_text("{oops", encoding="utf-8")
        check("坏 门禁.json → 补账行为空串（不炸日志链）",
              discuss._gate_summary_line("bad") == "")
    finally:
        discuss._ANALYSIS_ROOT = real_root
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------- [4] 真数据只读

def probe_readonly(before: dict[str, str]) -> None:
    print("[4] 真数据只读 + 零残留")
    names = [REAL_POP1 / n for n in POP1_SNAPSHOT] + [REAL_TASK]
    changed = [str(p) for p in names if sha256(p) != before[str(p)]]
    check("pop-1 四件 + auto-0036 任务.json SHA256 前后不变", not changed, f"变了: {changed}")


def main() -> int:
    print("=" * 72)
    print("probe_at15_r2 · 任务书 v2.5 正例 + 门禁进自检面板（离线零花钱）")
    print("=" * 72)
    names = [REAL_POP1 / n for n in POP1_SNAPSHOT] + [REAL_TASK]
    before = {str(p): sha256(p) for p in names if p.is_file()}
    probe_prompt_contract()
    probe_samples_parse()
    probe_gate_panel()
    probe_discuss_gate_log()
    probe_readonly(before)
    failed = [name for name, ok, _detail in RESULTS if not ok]
    total = len(RESULTS)
    print("=" * 72)
    print(f"结果：{total - len(failed)}/{total} PASS"
          + (f"；FAIL {len(failed)}：{'、'.join(failed)}" if failed else "（全绿）"))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
