# -*- coding: utf-8 -*-
"""AT-9 B5 渲染自检三项——渲染完成时自动跑，结论落盘 projects/<id>/自检.json。

数据合同 B0「自检结果」：生产者=渲染后自检函数；存储=projects/<id>/自检.json，
每条 {项,结论,证据}；消费者=详情页审批区（✓/⚠ 可见，FAIL 可 override 但必显）；
旧任务无文件 → 前端显「未检查」。

三项全部机械可判（AT-9.md §六层4，闭合 C02）：
  ① 次序对账：行号→图号→实际提交图片次序三方映射一致。发号用 render.assign_numbers
    同源函数重算（清单行号与 @图片N 是两套编号，禁行号==图号字面比较），提交次序取
    确认时点快照 任务.json assets_confirmed（列表序=提交序）。
  ② 绑图行残留：已传图行的原物核心词（该行实体名+特征词，复用 B2 strip_core_residue
    的词表思路：entity_noun_core + 2-3 字组）不得残留在提示词正文（附注/替换说明除外）。
  ③ 锁点对账：必换未换集合 == 确认时点『按原样』勾选/empty_confirm 记录。独立从
    任务.json 快照核对（不自证）：assets_confirmed/empty_confirm/按原样确认 都在确认
    动作落账，这里只读比对。
自检器自身异常 → 写一条 FAIL 记录如实落盘，绝不打断渲染主链（空响应不算成功的反面：
检查器挂了也算「未通过」，不许静默当 PASS）。
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
import traceback
from pathlib import Path

from . import replace, state

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
SELFCHECK_NAME = "自检.json"
PASS, FAIL = "PASS", "FAIL"


def _load_render():
    """按路径加载 pipeline/reverse/render.py（发号/词表同源；它不是包，同 cards._confirm 加载法）。"""
    path = ROOT / "pipeline" / "reverse" / "render.py"
    sys.path.insert(0, str(path.parent))          # render 内部 `from aspect import …` 同目录依赖
    spec = importlib.util.spec_from_file_location("wanrepro_reverse_render", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_selfcheck(task_dir: Path) -> list[dict]:
    """渲染完成后调用：三项检查 → 落盘 自检.json → 返回条目（get_task 原样透出）。"""
    try:
        items = _check_all(task_dir)
    except Exception as e:  # noqa: BLE001 —— 检查器异常也如实落盘（FAIL），不打断渲染主链
        items = [{"项": "自检执行", "结论": FAIL,
                  "证据": f"自检器异常（渲染主链不受影响）: {e}；{''.join(traceback.format_exc().splitlines()[-2:])}"}]
    path = task_dir / SELFCHECK_NAME
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return items


def load_selfcheck(task_dir: Path) -> list[dict] | None:
    """读 自检.json（get_task 透出用）；无文件/坏文件/非列表 → None（前端显「未检查」）。"""
    path = task_dir / SELFCHECK_NAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, list):
        return None
    return [row for row in data if isinstance(row, dict)] or None


# ---------------------------------------------------------------- 三项

def _check_all(task_dir: Path) -> list[dict]:
    task = state.load(task_dir)
    bridge_id = str(task.get("bridge_id") or "")
    ck_path = ROOT / "analysis" / "reverse" / bridge_id / replace.LIST_NAME
    if not ck_path.is_file():
        raise FileNotFoundError(f"找不到清单 {ck_path}（自检以清单为行号真源）")
    checklist_md = ck_path.read_text(encoding="utf-8-sig")
    prompt_path = task_dir / "提示词.md"
    prompt = prompt_path.read_text(encoding="utf-8") if prompt_path.is_file() else ""

    rows = replace.parse_rows(checklist_md)
    must_rows = sorted({int(r["no"]) for r in rows if "必须换" in (r.get("advice") or "")})

    render = _load_render()
    ents = render.parse_checklist(ck_path)
    render.assign_numbers(ents)
    row_to_no = {int(e.row_no): int(e.no) for e in ents if e.no}

    submitted = [(int(x.get("no") or 0), str(x.get("url") or ""))
                 for x in (task.get("assets_confirmed") or []) if isinstance(x, dict)]
    ack_rows = {int(x) for x in ((task.get("按原样确认") or {}).get("rows") or [])}
    empty_confirm = bool(task.get("empty_confirm"))

    body = _prompt_body(prompt)
    return [
        _check_order(body, ents, row_to_no, submitted, ck_path),
        _check_residue(body, ents, render),
        _check_lock(rows, must_rows, submitted, ack_rows, empty_confirm),
    ]


def _prompt_body(prompt: str) -> str:
    """正文 = 提示词全文挖掉「# 附注」（贴平台前要删的人读节）与「## 本片替换说明」（meta 注记）。"""
    lines: list[str] = []
    skip_heading: tuple[str, ...] = ()
    for raw in prompt.splitlines():
        head = raw.strip()
        if head.startswith("# 附注"):
            skip_heading = ("# ", )
            continue
        if head.startswith("## 本片替换说明"):
            skip_heading = ("## ", "- ")
            continue
        if skip_heading:
            if not head or head.startswith(skip_heading):
                continue
            skip_heading = ()          # 遇到别的内容 = 该节结束，恢复扫描
        lines.append(raw)
    return "\n".join(lines)


def _check_order(body: str, ents, row_to_no: dict[int, int],
                 submitted: list[tuple[int, str]], ck_path: Path) -> dict:
    """① 次序对账：行号→图号（assign_numbers 同源重算）→ 提交次序（确认快照列表序）。"""
    refs = sorted({int(n) for n in re.findall(r"@图片(\d+)", body)})
    url_by_row = {int(e.row_no): [u.strip() for u in re.split(r"[,，]", e.image) if u.strip()]
                  for e in ents if e.image}
    if not row_to_no and not refs and not submitted:
        return {"项": "次序对账", "结论": PASS, "证据": "全沿用/无图：清单无绑图行、正文无 @图片N、提交快照为空——三方一致"}
    problems: list[str] = []
    seq = sorted(row_to_no.values())
    if seq != list(range(1, len(seq) + 1)):
        problems.append(f"发号不连续: row→图号={row_to_no}")
    if refs != seq:
        problems.append(f"正文图号 {refs} ≠ 应发号 {seq}")
    if len(submitted) != len(seq):
        problems.append(f"提交快照 {len(submitted)} 条 ≠ 绑图行 {len(seq)} 行")
    else:
        for i, (no, url) in enumerate(submitted, 1):
            if row_to_no.get(no) != i:
                problems.append(f"提交第 {i} 张来自行 {no}，但发号映射给该行的是图{row_to_no.get(no)}（应为图{i}）")
            if url and url not in url_by_row.get(no, []):
                problems.append(f"提交第 {i} 张 URL 不在清单第 {no} 行绑图里: {url[:60]}")
    ev = (f"行→图号={row_to_no}；正文@图片N={refs}；提交次序={[no for no, _ in submitted]}"
          f"（清单 {ck_path.parent.name}）")
    return {"项": "次序对账", "结论": FAIL if problems else PASS,
            "证据": "；".join(problems) + "｜" + ev if problems else "三方映射一致：" + ev}


def _check_residue(body: str, ents, render) -> dict:
    """② 绑图行残留：已传图行的原物核心词（实体名+特征词）零残留（词表=strip_core_residue 同源）。

    词表只收 ≥3 字的组：2 字组（香水/黑色这类品类与颜色词）不是特征词——我们的产品
    常与原物同品类（香水换香水），别的行描述也常带同色词，2 字组必误红（实测见探针正例）。
    """
    scanned: list[str] = []
    hits: list[str] = []
    for e in ents:
        if not (e.no and e.has_asset_image):
            continue
        core = render.entity_noun_core(e)
        words = sorted({core} | set(render._noun_grams(core)), key=len, reverse=True)
        words = [w for w in words if len(w) >= 3]
        scanned.append(f"#{e.row_no}→图{e.no}({len(words)}词)")
        for w in words:
            for m in re.finditer(re.escape(w), body):
                left = max(0, m.start() - 10)
                hits.append(f"#{e.row_no} 的「{w}」残留于正文: …{body[left:m.end() + 10]}…")
                break
    ev = f"已传图行词表 {len(scanned)} 行（{'、'.join(scanned) or '无'}），正文 {len(body)} 字"
    return {"项": "绑图行残留", "结论": FAIL if hits else PASS,
            "证据": ("；".join(hits[:5]) + ("…" if len(hits) > 5 else "") + "｜" + ev)
            if hits else "零残留｜" + ev}


def _check_lock(rows: list[dict], must_rows: list[int],
                submitted: list[tuple[int, str]], ack_rows: set[int],
                empty_confirm: bool) -> dict:
    """③ 锁点对账：必换未换集合 == 确认时点『按原样』勾选/empty_confirm 记录（只读快照，不自证）。"""
    swapped = {no for no, _ in submitted}
    unswapped = sorted(set(must_rows) - swapped)
    covered = set(ack_rows) | ({int(r["no"]) for r in rows} if empty_confirm else set())
    missed = sorted(set(unswapped) - covered)
    ghost = sorted((covered & set(must_rows)) - set(unswapped))   # 记录了按原样但快照里其实换了
    ev = (f"必换={must_rows}；确认快照已传={sorted(swapped)}；未换={unswapped}；"
          f"按原样记录={sorted(ack_rows)}；empty_confirm={empty_confirm}")
    problems: list[str] = []
    if missed:
        problems.append(f"必换行 {missed} 未换且无『按原样』/empty_confirm 记录（锁点缺口）")
    if ghost:
        problems.append(f"行 {ghost} 记了按原样但确认快照里已传图（记录与快照矛盾）")
    return {"项": "锁点对账", "结论": FAIL if problems else PASS,
            "证据": ("；".join(problems) + "｜" + ev) if problems else "未换必换行都有确认时点记录｜" + ev}
