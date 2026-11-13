# -*- coding: utf-8 -*-
"""E1 证据探针：残留词表收词规则的全量实测与回归基线（只读，不写任何任务/清单）。

用法（仓库根运行）：
    py -3 automation/probe_e1_residue.py               # 语料体检：样本量/误报率/判类/潜在误报面
    py -3 automation/probe_e1_residue.py --rules       # 规则候选对比：R0/R1/R2/R3/A/B/C/D + 实际实现
    py -3 automation/probe_e1_residue.py --assert      # **落地验收**：断言真残留 213 零损失 + 误报清零
    py -3 automation/probe_e1_residue.py --task auto-0066   # 只读复现某任务的自检②（不落盘）

本探针**只读**（不写任务/清单/自检.json、不联网、不调模型），故不需要 `--isolated` 隔离实例。

口径与 automation/selfcheck.py:_check_residue 完全一致：
正文=挖掉「# 附注」「## 本片替换说明」；词表=core ∪ 2-3 字组，len≥3；每词每文件最多报 1 条。
AT-22 E1 R3 落地（B1）后：`--rules` 的「实际实现」行与 `--assert` 直调 `render.residue_core /
residue_words`（判定与清扫的唯一取词点），R0–R3 行仍是**历史候选规则的独立模拟**（用 render 的
同一张动作词表，但按各候选自己的口径跑），两者对不上即报错——这本身是取词口径的交叉校验。

`FA` 是人工判类锚点（19 条误报的命中行特征子串）：改判定口径前先跑本探针，
「真残留」条数不得下降、「误报」条数下降才算达标（红线见 docs/方案-AT22-E1-*）。
"""
from __future__ import annotations

import collections
import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "pipeline/reverse"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _load_render():
    path = ROOT / "pipeline/reverse/render.py"
    spec = importlib.util.spec_from_file_location("render_e1probe", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["render_e1probe"] = mod
    spec.loader.exec_module(mod)
    return mod


render = _load_render()

# 人工判类锚点：这 9 行正文是误报（不是原物残留），其余命中行一律判「真残留」。
# 结构见 docs/方案-AT22-E1-残留词表收词规则-20261112.md §4.2（五类源结构）。
FA = [
    ("1-103", "提示词-lumi-4section.md", "焦点始终锁死在手持产品与外包装正面细节", "F-ACT"),
    ("1-85", "提示词-lumi-4section.md", "运动主体为单手及双手持握的产品", "F-GEN"),
    ("1-94", "提示词-lumi-4section.md", "运动主体为入画的手以及手持的产品实体", "F-GEN"),
    ("3", "提示词-lumi-4section.md", "粉红色标签款（透明圆柱形喷雾小小物体）", "F-LABEL"),
    ("3", "提示词-lumi-4section.md", "第一人称右手持粉色喷雾瓶从右下角边缘", "F-ACT"),
    ("3", "提示词-lumi-4section.md", "机位与运镜：第一人称视点", "F-ACT"),
    ("3", "提示词-lumi-4section.md", "镜号 01｜00:00-00:02｜", "F-ACT"),
    ("3", "提示词-lumi-4section.md", "街头偶遇浪漫松弛感；情绪基调", "F-ACT"),
    ("3", "提示词-seedance-flow.md", "全程使用参考图中的@图片1。0-2秒", "F-ACT"),
]

# 动作/方位/视角词表与通用占位名：**唯一来源是实现侧**（render.ACTION_VIEW_WORDS /
# render.GENERIC_ENTITY_NAMES，AT-22 E1 R3）。探针只做模拟与对账，不再自带副本——两处各存一份
# 就是口径分裂的开始（E1 约束 2）。REAL_NOUNS 只服务于被否决的候选 B/C，留在探针里。
ACTION_WORDS = render.ACTION_VIEW_WORDS
GENERIC_NAMES = render.GENERIC_ENTITY_NAMES
REAL_NOUNS = ("瓶", "盒", "袋", "罐", "杯", "碗", "刀", "剪", "表", "机", "车", "柱", "球", "管",
              "笔", "包", "纸", "卡", "链", "环", "盖", "喷雾", "香水", "液体", "膏", "霜", "精",
              "奖杯", "托盘", "餐具", "鞋", "衣", "镜", "灯", "椅", "桌")

RULES = {
    "R0 现状": dict(drop=(), label=False, gen=False, kind=None, real=False),
    "R1 动作词先删": dict(drop=ACTION_WORDS, label=False, gen=False, kind=None, real=False),
    "R2 +通用名闸门": dict(drop=ACTION_WORDS, label=False, gen=True, kind=None, real=False),
    "R3 +标签行豁免（推荐）": dict(drop=ACTION_WORDS, label=True, gen=True, kind=None, real=False),
    "A 仅主体进表": dict(drop=(), label=False, gen=False, kind="主体", real=False),
    "B 实物名词闸门": dict(drop=(), label=False, gen=False, kind=None, real=True),
    "C R3+B": dict(drop=ACTION_WORDS, label=True, gen=True, kind=None, real=True),
}


def prompt_body(prompt: str) -> str:
    """与 selfcheck._prompt_body 同口径。"""
    lines, skip = [], ()
    for raw in prompt.splitlines():
        head = raw.strip()
        if head.startswith("# 附注"):
            skip = ("# ",); continue
        if head.startswith("## 本片替换说明"):
            skip = ("## ", "- "); continue
        if skip:
            if not head or head.startswith(skip):
                continue
            skip = ()
        lines.append(raw)
    return "\n".join(lines)


def line_class(bridge: str, prompt: str, line: str) -> str:
    for b, p, sub, cls in FA:
        if b == bridge and p == prompt and sub in line:
            return cls
    return "T"


def words_of(core: str, drop: tuple[str, ...] = (), min_len: int = 3) -> list[str]:
    for w in drop:
        core = core.replace(w, "")
    return [w for w in sorted({core} | set(render._noun_grams(core)), key=len, reverse=True)
            if len(w) >= min_len]


def is_own_label(line: str, names: set[str]) -> bool:
    s = line.strip()
    for n in names:
        if not n or not s.startswith(n):
            continue
        rest = s[len(n):].strip()
        if rest.startswith("（") and rest.endswith("）") and len(rest) <= 30:
            return True
    return False


def corpus():
    """→ [(bridge, dir, ents(bound), [提示词文件])]，只收「清单+提示词都在」的任务。"""
    out = []
    root = ROOT / "analysis/reverse"
    if not root.is_dir():
        return out
    try:
        from automation import replace as rep
    except Exception:                                            # noqa: BLE001
        rep = None
    for d in sorted(root.iterdir()):
        ck = d / "待确认清单.md"
        if not d.is_dir() or not ck.is_file():
            continue
        files = [p for p in sorted(d.iterdir()) if p.name.startswith("提示词") and p.suffix == ".md"]
        if not files:
            continue
        ents = render.parse_checklist(ck)
        if rep is not None and any(e.image for e in ents) and not any(e.no for e in ents):
            try:
                render.assign_numbers(ents, rep.load_groups(d.name), rep.load_row_decisions(d.name))
            except Exception:                                    # noqa: BLE001
                pass
        bound = [e for e in ents if not e.kind.startswith("品牌视觉") and e.no and e.has_asset_image]
        out.append((d.name, d, bound, files))
    return out


def cmd_stats() -> None:
    data = corpus()
    hits, tables = [], []
    pure_fp, mixed_fp = [], []
    for bridge, _d, bound, files in data:
        for e in bound:
            tables.append((bridge, e.row_no, e.kind, render.entity_noun_core(e),
                           len(words_of(render.entity_noun_core(e)))))
        for p in files:
            body = prompt_body(p.read_text(encoding="utf-8", errors="replace"))
            lines = body.splitlines()
            t = f = 0
            for e in bound:
                for w in words_of(render.entity_noun_core(e)):
                    m = re.search(re.escape(w), body)
                    if not m:
                        continue
                    i = body.count("\n", 0, m.start())
                    cls = line_class(bridge, p.name, lines[i])
                    hits.append({"bridge": bridge, "prompt": p.name, "row": e.row_no,
                                 "word": w, "cls": cls})
                    if cls == "T":
                        t += 1
                    else:
                        f += 1
            if f and not t:
                pure_fp.append((bridge, p.name, f))
            elif f:
                mixed_fp.append((bridge, p.name, t, f))
    cls = collections.Counter(h["cls"] for h in hits)
    fmt = collections.Counter(h["prompt"] for h in hits)
    tasks_bound = {b for b, _d, bound, _f in data if bound}
    fp_tasks = {h["bridge"] for h in hits if h["cls"] != "T"}
    print(f"可测任务={len(data)}  绑图行={len(tables)}  有绑图行的任务={len(tasks_bound)}  提示词文件="
          f"{sum(len(f) for _b, _d, _e, f in data)}")
    print(f"词表规模 平均={sum(t[4] for t in tables) / max(len(tables), 1):.1f} 词/行 最大={max((t[4] for t in tables), default=0)}")
    print(f"命中词条={len(hits)}  按模板={dict(fmt)}")
    fp = len(hits) - cls["T"]
    print(f"判类={dict(cls)}  误报={fp} ({fp / max(len(hits), 1) * 100:.1f}%)  "
          f"出现误报的任务={sorted(fp_tasks)} ({len(fp_tasks)}/{len(tasks_bound)})")
    print(f"纯误报文件（只因误报判红）={len(pure_fp)}: {pure_fp}")
    print(f"真残留+误报并存的文件={mixed_fp}")
    small = [t for t in tables if "小物体" in t[3]]
    print(f"核心词含泛化占位词「小物体」的绑图行={len(small)}/{len(tables)}"
          f"（正文一旦出现该词即同时误报）")
    print("\n误报明细:")
    for h in hits:
        if h["cls"] != "T":
            print(f"  {h['cls']:<8}{h['bridge']:<8}{h['prompt'][:20]:<22}行{h['row']}  词「{h['word']}」")
    impl = scan_impl()
    ic = collections.Counter(h["cls"] for h in impl)
    print(f"\n▲ 以上「现状」段是 R0 历史基线（人工判类锚点）。实际实现（render.residue_words，"
          f"AT-22 E1 R3）：命中={len(impl)} 真残留={ic.get('T', 0)} 误报={len(impl) - ic.get('T', 0)}"
          f"　落地验收跑 --assert")


def scan_impl() -> list[dict]:
    """**实际实现**（render.residue_core/residue_words + 自身标签行豁免）在语料上的命中明细。

    与 selfcheck._check_residue 同口径（每词每文件最多 1 条，落在自身标签行上的命中不算证据）。
    """
    hits: list[dict] = []
    for bridge, _d, bound, files in corpus():
        names = {e.name for e in bound if e.name}
        for p in files:
            body = prompt_body(p.read_text(encoding="utf-8", errors="replace"))
            lines = body.splitlines()
            for e in bound:
                for w in render.residue_words(e):
                    for m in re.finditer(re.escape(w), body):
                        i = body.count("\n", 0, m.start())
                        if is_own_label(lines[i], names):
                            continue
                        hits.append({"bridge": bridge, "prompt": p.name, "row": e.row_no,
                                     "word": w, "cls": line_class(bridge, p.name, lines[i])})
                        break
    return hits


def cmd_rules() -> None:
    data = corpus()
    res = {n: [] for n in RULES}
    res["D 长度阈值≥4"] = []
    for bridge, _d, bound, files in data:
        names = {e.name for e in bound if e.name}
        for p in files:
            body = prompt_body(p.read_text(encoding="utf-8", errors="replace"))
            lines = body.splitlines()
            for name, cfg in RULES.items():
                for e in bound:
                    core = render.entity_noun_core(e)
                    if cfg["kind"] and e.kind != cfg["kind"]:
                        continue
                    if cfg["gen"] and core in GENERIC_NAMES:
                        continue
                    if cfg["real"] and not any(n in core for n in REAL_NOUNS):
                        continue
                    for w in words_of(core, cfg["drop"]):
                        m = re.search(re.escape(w), body)
                        if not m:
                            continue
                        i = body.count("\n", 0, m.start())
                        if cfg["label"] and is_own_label(lines[i], names):
                            continue
                        res[name].append(line_class(bridge, p.name, lines[i]))
            for e in bound:                                     # D：只抬长度阈值
                for w in words_of(render.entity_noun_core(e), (), 4):
                    m = re.search(re.escape(w), body)
                    if not m:
                        continue
                    i = body.count("\n", 0, m.start())
                    res["D 长度阈值≥4"].append(line_class(bridge, p.name, lines[i]))
    res["★ 实际实现（render.residue_words）"] = [h["cls"] for h in scan_impl()]
    print(f"{'规则':<34}{'词条':>6}{'真残留':>7}{'误报':>6}{'误报率':>8}")
    for name, v in res.items():
        c = collections.Counter(v)
        tp = c.get("T", 0)
        fp = len(v) - tp
        print(f"{name:<34}{len(v):>6}{tp:>7}{fp:>6}{fp / max(len(v), 1) * 100:>7.1f}%")


def _ent(name: str, kind: str = "道具", row_no: int = 6, no: int = 1):
    return render.Entity(kind, "", name, "x.png", "", no=no, row_no=row_no)


def cmd_assert() -> int:
    """落地验收断言（AT-22 B1）：真残留零损失 + 误报清零 + 删词不跳行 + 单一取词点。"""
    ok = True

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal ok
        ok = ok and cond
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))

    # 语料是活的（每建一条新任务，analysis/reverse 里就多一份提示词），所以 E1 红线写成
    # **不变量**而不是某一刻的快照数字：真残留「不得下降」（≥ 基线 213）、误报「不涨」（≤1 已认账）。
    # 逐字快照数字（== 213 / == 36）会在任何新任务落地时假红——2026-11-13 新增任务
    # video-0d294878（auto-0068）就带来 +8 真残留 / +1 绑图行，与取词口径无关。
    print("[1] 全量任务重放（实际实现）")
    hits = scan_impl()
    cls = collections.Counter(h["cls"] for h in hits)
    tp, fp = cls.get("T", 0), len(hits) - cls.get("T", 0)
    fps = [h for h in hits if h["cls"] != "T"]
    print(f"      命中={len(hits)} 真残留={tp} 误报={fp} 明细={dict(cls)}")
    check("真残留 ≥213 条零损失（E1 验收红线：既有基线一条不丢）", tp >= 213, f"实测 {tp}")
    # E1 §七.2 已认账的残余：品类泛化词「外包装」撞拍摄手法句，靠 E4 行×词白名单兜底。
    known = [h for h in fps if h["bridge"] == "1-103" and h["word"] == "外包装"]
    check("误报 19→0（允许 ±2 边界，须逐条解释）",
          fp <= 2 and (fp == 0 or len(known) == fp),
          "无残余" if fp == 0 else f"残余 {fp} 条，逐条：" +
          "；".join(f"{h['bridge']}/{h['row']}「{h['word']}」{h['cls']}" for h in fps))
    for h in fps:
        why = ("E1 §七.2 已认账的品类泛化词（3 字组「外包装」撞拍摄手法句），靠 E4 用户白名单兜底"
               if h in known else "**未认账的误报**（须当缺陷处理，禁静默容忍）")
        print(f"      残余误报：{h['bridge']} 行{h['row']}「{h['word']}」{h['cls']} → {why}")
    pure = collections.Counter((h["bridge"], h["prompt"]) for h in fps)
    pure_only = [k for k in pure if not any(
        h["bridge"] == k[0] and h["prompt"] == k[1] and h["cls"] == "T" for h in hits)]
    check("纯误报文件 3→0（只因误报判红的文件）", not pure_only, str(pure_only))

    print("[2] 收词口径（空表=不建表，不是跳过检查）")
    tables, empty = [], []
    for bridge, _d, bound, _f in corpus():
        for e in bound:
            core_core, core_r3, words = render.entity_noun_core(e), render.residue_core(e), render.residue_words(e)
            tables.append((bridge, e.row_no, core_core, core_r3, len(words)))
            if not words:
                empty.append((bridge, e.row_no, core_core))
    check("绑图行 ≥36 / 空表 6（5 行通用占位名 + 1 行纯动作名）",
          len(tables) >= 36 and len(empty) == 6,
          f"绑图行={len(tables)} 空表={len(empty)}：{empty}")
    check("通用占位名不进表（我们的产品 ×5）",
          sum(1 for _b, _r, n in empty if n in GENERIC_NAMES) == 5, str(empty))
    check("纯动作名 3#6 自然空表（用户撞到的那一例）",
          any(b == "3" and r == 6 for b, r, _n in empty), str(empty))

    print("[3] 删词不跳行（E1 §五 反例逐例实跑）")
    for name, n_words, must in (("从右下角入画的第一人称右手", 0, None),
                                ("入画式香水瓶", 3, "香水瓶"),
                                ("第一人称视角的黑色香水瓶", 5, "香水瓶"),
                                ("前景右手握持的喷雾瓶", 3, "喷雾瓶")):
        words = render.residue_words(_ent(name))
        cond = len(words) == n_words and (must is None or must in words)
        check(f"「{name}」→ {len(words)} 词" + (f"（含「{must}」）" if must else ""),
              cond, str(words))

    print("[4] 单一取词点（判定与清扫同源，禁两套口径）")
    line_action = "前景右手自右下角入画@图片1，无剪切。"
    kept = render.strip_core_residue(line_action, [_ent("从右下角入画的第一人称右手")])
    check("清扫器不再吃掉合法构图词（E1 §1.2 缺陷）", kept == line_action, kept)
    eaten = render.strip_core_residue("取出黑色奖杯香水@图片1，无剪切。",
                                      [_ent("双耳奖杯形态玻璃香水小物体", "主体", 1, 1)])
    check("原物核心词照旧被吃掉（AT-9 B2 零回归）", eaten == "取出@图片1，无剪切。", eaten)
    import inspect
    from automation import selfcheck as sc
    check("render.strip_core_residue 走 residue_core", "residue_core(" in inspect.getsource(render.strip_core_residue))
    check("selfcheck._check_residue 走 residue_words", "residue_words(" in inspect.getsource(sc._check_residue))

    print("[5] 现场复现 auto-0066（用户撞到的那一例，只读）")
    task_dir = ROOT / "automation/projects/auto-0066"
    if not task_dir.is_dir():
        check("auto-0066 现场可复现", False, "任务目录不存在（语料被清）")
    else:
        from automation import replace as rep
        task = sc.state.load(task_dir)
        bridge_id = str(task.get("bridge_id") or "")
        ents = render.parse_checklist(ROOT / "analysis/reverse" / bridge_id / rep.LIST_NAME)
        render.assign_numbers(ents, rep.load_groups(bridge_id), rep.load_row_decisions(bridge_id))
        body = sc._prompt_body((task_dir / "提示词.md").read_text(encoding="utf-8"))
        res = sc._check_residue(body, ents, render, bridge_id)
        words = [w for w in ("右下角", "角入画", "下角入") if w in str(res["证据"])]
        check("自检②不再报「右下角/角入画/下角入」", not words, str(res["证据"])[:200])
        check("该任务词表 #6 为 0 词", "#6→图1(0词)" in str(res["证据"]), str(res["证据"])[:200])

    print(f"\n{'全绿（exit 0）' if ok else 'FAIL（exit 1）'}")
    return 0 if ok else 1


def cmd_task(task_id: str) -> None:
    """只读复现某任务的自检②（不写 自检.json）。"""
    sys.path.insert(0, str(ROOT))
    from automation import selfcheck as sc
    from automation import replace as rep

    task_dir = ROOT / "automation/projects" / task_id
    task = sc.state.load(task_dir)
    bridge_id = str(task.get("bridge_id") or "")
    ck = ROOT / "analysis/reverse" / bridge_id / rep.LIST_NAME
    ents = render.parse_checklist(ck)
    render.assign_numbers(ents, rep.load_groups(bridge_id), rep.load_row_decisions(bridge_id))
    prompt = (task_dir / "提示词.md").read_text(encoding="utf-8")
    body = sc._prompt_body(prompt)
    res = sc._check_residue(body, ents, render, bridge_id)
    print(f"任务={task_id} 桥={bridge_id} 正文={len(body)} 字")
    print(f"结论={res['结论']}")
    print(f"证据={res['证据']}")
    for f in res["说明"].get("发现", []):
        print(f"  - {f}")


def main() -> None:
    argv = sys.argv[1:]
    code = 0
    if "--assert" in argv:
        code = cmd_assert()
    elif "--rules" in argv:
        cmd_rules()
    elif "--task" in argv:
        idx = argv.index("--task")
        cmd_task(argv[idx + 1] if len(argv) > idx + 1 else "auto-0066")
    else:
        cmd_stats()
    sys.exit(code)


if __name__ == "__main__":
    main()
