# -*- coding: utf-8 -*-
"""E1 证据探针：残留词表收词规则的全量实测与回归基线（只读，不写任何任务/清单）。

用法（仓库根运行）：
    py -3 automation/probe_e1_residue.py               # 语料体检：样本量/误报率/判类/潜在误报面
    py -3 automation/probe_e1_residue.py --rules       # 规则候选对比：R0/R1/R2/R3/A/B/C/D
    py -3 automation/probe_e1_residue.py --task auto-0066   # 只读复现某任务的自检②（不落盘）

口径与 automation/selfcheck.py:348 _check_residue 完全一致：
正文=挖掉「# 附注」「## 本片替换说明」；词表=core ∪ 2-3 字组，len≥3；每词每文件最多报 1 条。

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

ACTION_WORDS = ("入画", "出画", "视角", "第一人称", "第二人称", "人称", "视点", "镜头", "运镜", "机位",
                "前景", "背景", "右手", "左手", "双手", "手部", "手持", "握持", "特写", "近景",
                "中景", "远景", "全景", "俯视", "仰视", "平视", "右下角", "左下角", "右上角",
                "左上角", "右下", "左下", "右上", "左上", "画面", "边缘", "转身", "托举", "推近",
                "拉远", "平移", "跟拍", "环绕", "俯拍", "仰拍", "空镜")
GENERIC_NAMES = {"我们的产品", "我们的道具", "我的产品", "产品", "道具", "人物", "场景",
                 "你上传的素材", "用户上传资产", "你的产品", "你的道具", "你的角色", "你的场景"}
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
    print(f"{'规则':<24}{'词条':>6}{'真残留':>7}{'误报':>6}{'误报率':>8}")
    for name, v in res.items():
        c = collections.Counter(v)
        tp = c.get("T", 0)
        fp = len(v) - tp
        print(f"{name:<24}{len(v):>6}{tp:>7}{fp:>6}{fp / max(len(v), 1) * 100:>7.1f}%")


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
    if "--rules" in argv:
        cmd_rules()
    elif "--task" in argv:
        idx = argv.index("--task")
        cmd_task(argv[idx + 1] if len(argv) > idx + 1 else "auto-0066")
    else:
        cmd_stats()


if __name__ == "__main__":
    main()
