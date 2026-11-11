# -*- coding: utf-8 -*-
"""AT-9 B5 渲染自检——渲染完成时自动跑，结论落盘 projects/<id>/自检.json。

数据合同 B0「自检结果」：生产者=渲染后自检函数；存储=projects/<id>/自检.json，
每条 {项,结论,证据}；消费者=详情页审批区（✓/⚠ 可见，FAIL 可 override 但必显）；
旧任务无文件 → 前端显「未检查」。

四项全部机械可判（AT-9.md §六层4 + AT-11 §3.3 第4项）：
  ① 次序对账：行号→图号→实际提交图片次序三方映射一致。发号用 render.assign_numbers
    同源函数重算（清单行号与 @图片N 是两套编号，禁行号==图号字面比较；组内 N 行共用
    1 图号——AT-11 B1），提交次序取确认时点快照 任务.json assets_confirmed（列表序=提交序，
    确认时已按组去重）。
  ② 绑图行残留：已传图行的原物核心词（该行实体名+特征词，复用 B2 strip_core_residue
    的词表思路：entity_noun_core + 2-3 字组）不得残留在提示词正文（附注/替换说明除外）。
  ③ 锁点对账：必换未换集合 == 确认时点『按原样』/『按原片』/empty_confirm 记录。独立从
    任务.json 快照核对（不自证）：assets_confirmed/empty_confirm/按原样确认/按原片确认
    都在确认动作落账，这里只读比对。
  ④ 组绑定一致性（AT-11）：确认组内行图号/URL 必须一致；同组两 URL→FAIL；同号不同
    URL→FAIL。
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
    """读 自检.json（get_task 透出用）；无文件/坏文件/非列表 → None（前端显「未检查」）。

    AT-15 R2：追加一条「拆解门禁」合成条目（只读 analysis/reverse/<bridge>/门禁.json，
    不落盘、不改 B5 自检合同的原有四项）——门禁数据是渲染后门禁步落的另一份文件，
    前端自检面板只认 自检.json 这一束，所以在读路径汇总（白名单判据汇总位）。
    """
    items: list[dict] | None = None
    path = task_dir / SELFCHECK_NAME
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            data = None
        if isinstance(data, list):
            rows = [row for row in data if isinstance(row, dict)]
            items = rows or None
    gate = load_gate_item(task_dir)
    if gate is not None:
        items = (items or []) + [gate]
    return items


# ---------------------------------------------------------------- 拆解门禁汇总（AT-15 R2）

GATE_ITEM_NAME = "拆解门禁"          # 前端按 项==GATE_ITEM_NAME 识别并单独渲染红/绿条


def _gate_bridge_id(task_dir: Path) -> str:
    """任务号 → 桥 id（只读 任务.json 单键；state.load 的校验在读路径上太重）。"""
    try:
        data = json.loads((Path(task_dir) / "任务.json").read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return ""
    return str(data.get("bridge_id") or "").strip()


def load_gate_item(task_dir: Path) -> dict | None:
    """analysis/reverse/<bridge>/门禁.json → 自检面板一条 {项,结论,证据}；无文件 → None。

    结论三态沿用门禁自己的 verdict（PASS/WARN/FAIL）；证据第一行是摘要（状态/绿项数/
    重拆轮次），缺项与告警逐条排在后面——前端只渲染，不解释（红线 25：渲染承接既有自检位）。
    """
    bridge_id = _gate_bridge_id(task_dir)
    if not bridge_id:
        return None
    gate_path = ROOT / "analysis" / "reverse" / bridge_id / "门禁.json"
    if not gate_path.is_file():
        return None
    try:
        gate = json.loads(gate_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        gate = None
    if not isinstance(gate, dict):
        return {"项": GATE_ITEM_NAME, "结论": FAIL,
                "证据": "门禁.json 读不出（拆解产物门禁留痕损坏）——如实标红，重跑拆解可重建"}
    result = gate.get("结果") if isinstance(gate.get("结果"), dict) else {}
    verdict = str(result.get("verdict") or "").strip().upper()
    checks = result.get("checks") if isinstance(result.get("checks"), list) else []
    total = len(checks)
    passed = sum(1 for item in checks if isinstance(item, dict) and item.get("verdict") == PASS)
    missing = [str(x) for x in (result.get("缺项") or []) if str(x).strip()]
    warns = [str(x) for x in (result.get("告警") or []) if str(x).strip()]
    status = str(gate.get("状态") or "").strip()
    rounds = gate.get("轮次") if isinstance(gate.get("轮次"), int) else 0
    limit = gate.get("上限") if isinstance(gate.get("上限"), int) else "?"
    if not verdict:
        verdict = FAIL if "标红" in status else (WARN if "告警" in status else PASS)
    lines = [f"状态 {status or '未知'}｜绿项 {passed}/{total}"
             f"｜缺项 {len(missing)}｜告警 {len(warns)}｜重拆 {rounds}/{limit}"]
    lines += [f"❌ {item}" for item in missing]
    lines += [f"⚠ {item}" for item in warns]
    return {"项": GATE_ITEM_NAME, "结论": verdict, "证据": "\n".join(lines)}


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
    # AT-11 B1：发号同源重算带组/行决策覆盖（与 render 主入口同一 sidecar 数据源）
    groups = replace.load_groups(bridge_id)
    decisions = replace.load_row_decisions(bridge_id)
    render.assign_numbers(ents, groups, decisions)
    row_to_no = {int(e.row_no): int(e.no) for e in ents if e.no}

    submitted = [(int(x.get("no") or 0), str(x.get("url") or ""))
                 for x in (task.get("assets_confirmed") or []) if isinstance(x, dict)]
    ack_rows = {int(x) for x in ((task.get("按原样确认") or {}).get("rows") or [])}
    ko_rows = {int(x) for x in ((task.get("按原片确认") or {}).get("rows") or [])}   # AT-11 B2 确认时点快照
    empty_confirm = bool(task.get("empty_confirm"))

    body = _prompt_body(prompt)
    return [
        _check_order(body, ents, row_to_no, submitted, ck_path, groups),
        _check_residue(body, ents, render),
        _check_lock(rows, must_rows, submitted, ack_rows, empty_confirm, ko_rows, groups),
        _check_groups(ents, row_to_no, submitted, groups),
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
                 submitted: list[tuple[int, str]], ck_path: Path,
                 groups: list[list[int]] | None = None) -> dict:
    """① 次序对账：行号→图号（assign_numbers 同源重算，组内 N 行共用 1 图号——AT-11 B1）
    → 提交次序（确认快照列表序=提交序，确认时已按组去重）。"""
    groups = groups or []
    refs = sorted({int(n) for n in re.findall(r"@图片(\d+)", body)})
    url_by_row = {int(e.row_no): [u.strip() for u in re.split(r"[,，]", e.image) if u.strip()]
                  for e in ents if e.image}
    if not row_to_no and not refs and not submitted:
        return {"项": "次序对账", "结论": PASS, "证据": "全沿用/无图：清单无绑图行、正文无 @图片N、提交快照为空——三方一致"}
    problems: list[str] = []
    nums = sorted(set(row_to_no.values()))          # 唯一图号集合（组内多行同号算一个）
    if nums != list(range(1, len(nums) + 1)):
        problems.append(f"发号不连续: row→图号={row_to_no}")
    if refs != nums:
        problems.append(f"正文图号 {refs} ≠ 应发号 {nums}")
    if len(submitted) != len(nums):
        problems.append(f"提交快照 {len(submitted)} 条 ≠ 发图单位 {len(nums)} 个（组内多行应只提交一条）")
    else:
        seen_units: set[tuple] = set()
        for i, (no, url) in enumerate(submitted, 1):
            unit = replace.row_unit(no, groups)
            if unit in seen_units:
                problems.append(f"提交第 {i} 张来自行 {no}，但该行所在单位已有提交（组内多行多提交）")
            seen_units.add(unit)
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
        if e.kind.startswith("品牌视觉"):
            # AT-16 B1b：品牌位有图号后「命名」是客户字样而非原物核心词，按它取词会误红。
            continue
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
                empty_confirm: bool, ko_rows: set[int] | None = None,
                groups: list[list[int]] | None = None) -> dict:
    """③ 锁点对账：必换未换集合 == 确认时点『按原样』/『按原片』/empty_confirm 记录（只读快照，不自证）。

    AT-11 §四锁点新规则：必换行 ∈ 组且组已传图（整组被那张图换掉）→ 不锁；
    行有按原片决策（keep_origin / AT-12 dismissed）→ 不锁——两者都不需要『按原样』勾选。
    """
    ko_rows = ko_rows or set()
    groups = groups or []
    swapped = {no for no, _ in submitted}
    for group in groups:                        # 组已传图 → 组内所有行视为已换
        if any(no in swapped for no in group):
            swapped |= set(group)
    unswapped = sorted(set(must_rows) - swapped)
    covered = (set(ack_rows) | ko_rows
               | ({int(r["no"]) for r in rows} if empty_confirm else set()))
    missed = sorted(set(unswapped) - covered)
    ghost = sorted((covered & set(must_rows)) - set(unswapped))   # 记录了按原样/按原片但快照里其实换了
    ev = (f"必换={must_rows}；确认快照已传={sorted(swapped)}；未换={unswapped}；"
          f"按原样记录={sorted(ack_rows)}；按原片记录={sorted(ko_rows)}；empty_confirm={empty_confirm}")
    problems: list[str] = []
    if missed:
        problems.append(f"必换行 {missed} 未换且无『按原样』/『按原片』/empty_confirm 记录（锁点缺口）")
    if ghost:
        problems.append(f"行 {ghost} 记了按原样/按原片但确认快照里已传图（记录与快照矛盾）")
    return {"项": "锁点对账", "结论": FAIL if problems else PASS,
            "证据": ("；".join(problems) + "｜" + ev) if problems else "未换必换行都有确认时点记录｜" + ev}


def _check_groups(ents, row_to_no: dict[int, int],
                  submitted: list[tuple[int, str]],
                  groups: list[list[int]]) -> dict:
    """④ 组绑定一致性（AT-11 B1 新增）：组内行图号/URL 必须一致。

    同组两 URL → FAIL；同号不同 URL → FAIL。次序对账读新映射后，这里只管「同组绑定
    是否真的归一」：组内 N 行 1 图号 1 URL 才算合并成功。
    """
    url_by_row: dict[int, list[str]] = {}
    for e in ents:
        urls = [u.strip() for u in re.split(r"[,，]", e.image) if u.strip()]
        if urls:
            url_by_row[int(e.row_no)] = urls
    problems: list[str] = []
    for group in groups:
        nums = sorted({row_to_no[n] for n in group if row_to_no.get(n)})
        if not nums:
            continue                            # 组内全无图：不占号不提交，合并仍成立（零图分支）
        if len(nums) != 1:
            problems.append(f"组 {group} 图号不一致: {[{n: row_to_no[n]} for n in group if row_to_no.get(n)]}")
        urls = sorted({u for n in group for u in url_by_row.get(n, [])})
        if len(urls) > 1:
            problems.append(f"组 {group} 绑定了 {len(urls)} 个不同 URL（同组两 URL）: "
                            + "、".join(u[:40] for u in urls[:3]))
    num_urls: dict[int, set[str]] = {}
    for no, urls in url_by_row.items():
        num = row_to_no.get(no)
        if num:
            num_urls.setdefault(num, set()).update(urls)
    for num in sorted(num_urls):
        if len(num_urls[num]) > 1:
            problems.append(f"图{num} 对应多个 URL（同号不同 URL）: "
                            + "、".join(sorted(u[:40] for u in num_urls[num])[:3]))
    seen: dict[tuple, str] = {}
    for no, url in submitted:
        unit = replace.row_unit(no, groups)
        if unit in seen and seen[unit] != url:
            problems.append(f"提交快照里同组两 URL（行 {no} 与行 "
                            f"{next(n for n, u in submitted if replace.row_unit(n, groups) == unit and u == seen[unit])}）")
        seen.setdefault(unit, url)
    ev = (f"确认组={groups or '无'}；行→图号={row_to_no}；组内 URL 归一后提交 {len(submitted)} 条")
    return {"项": "组绑定一致性", "结论": FAIL if problems else PASS,
            "证据": ("；".join(problems) + "｜" + ev) if problems else "组内行同图号同 URL（无组或全组归一）｜" + ev}
