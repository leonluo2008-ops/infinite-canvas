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
WARN = "WARN"                       # 拆解门禁条目三态（自检四项只有 PASS/FAIL）


# ---------------------------------------------------------------- 人话说明（AT-16 B2）
#
# 用户话（结论 + 原因 + 怎么处理）由自检生产者出，前端只渲染——红线 25：前端不得解析证据
# 文字重判。这里每条「说明」和「证据」都由**同一份检查器发现**装配：发现（finding）里带
# 行号/图号，文字既拼证据原文又喂话术，动态数字不靠前端再解析一遍。
#
# 处理指引只能指向**真实存在的入口**：正文里「…」包起来的具名，要么是详情页按钮/区域的
# 文案（task-detail-drawer.tsx 里出现），要么是清单列名（待确认清单.md 表头）。probe_at16_b2
# 把每条指引的具名逐个对账，编造的入口当场判红。

HOW_REBIND = ("点「退回置换」回到待置换区：对点到的行在上传区「重传（覆盖）」重新绑图"
              "（组内行在组卡上传，全组联动）；不要这张图就点「移除」按原片处理，再确认置换重渲染")
HOW_RECONFIRM = ("点「退回置换」后重新确认置换：发号按清单当前绑定重算，重算后才写进提示词"
                 "（重算后仍对不上，说明清单「资产图」列绑错了行，按上面点到的行「重传（覆盖）」或「移除」）")
HOW_ACK = ("点「退回置换」回到待置换区：给这些行传图，或逐行勾上「知道，按原样」（按原片沿用），再确认置换")
HOW_GROUP = ("在待置换区该组组卡上传区「重传（覆盖）」统一成同一张图（组内行全组联动）；"
             "同号不同 URL 就把多余那张点「清空」按原片沿用，再确认置换")
HOW_GATE = ("点详情底部「重试」重跑拆解与渲染（门禁按重拆结果重算）；"
            "重拆轮次用尽仍缺项 → 按缺项补齐素材/意图后重跑")

ORDER_KIND_WORD = {
    "发号": "发号不连续",
    "正文号": "提示词里的图号和清单对不上",
    "提交条数": "提交的图数和该发的图数不一致",
    "组内多提交": "同一组多提交了一张",
    "次序错位": "提交次序和行对不上",
    "提交图不符": "提交的图不属于那一行",
}


def _how_prompt_word(bridge_id: str) -> str:
    """② 残留在提示词正文里 → 只能改清单「用我的描述」列（描述文字的唯一人改入口）后重渲染。"""
    return ("点「退回置换」回到待置换区，在清单 analysis/reverse/{b}/{n} 这些行的"
            "「用我的描述」列改写（删掉原物词），再确认置换重渲染（提示词按清单重出）"
            ).format(b=bridge_id or "&lt;桥id&gt;", n=replace.LIST_NAME)


def _finding(kind: str, text: str, row: int | None = None, no: int | None = None) -> dict:
    """一条检查器发现：文字=证据原文（与旧格式逐字一致），行号/图号=人话用的动态数字。"""
    return {"类型": kind, "文字": text, "行号": row, "图号": no}


def _evidence(findings: list[dict]) -> str:
    return "；".join(str(f["文字"]) for f in findings)


def _shown(findings: list[dict]) -> list[dict]:
    """透出给前端的结构化发现：行号/图号只带真值（None 不出现）。"""
    shown: list[dict] = []
    for f in findings:
        row: dict = {"文字": str(f["文字"])}
        if f.get("行号"):
            row["行号"] = int(f["行号"])
        if f.get("图号"):
            row["图号"] = int(f["图号"])
        shown.append(row)
    return shown


def _say(结论人话: str, 原因: str = "", 处理: str = "", findings: list[dict] | None = None) -> dict:
    """「说明」对象：结论人话（一行）/原因/怎么处理/结构化发现——前端逐字渲染，不重判。"""
    said: dict = {"结论人话": 结论人话}
    if 原因:
        said["原因"] = 原因
    if 处理:
        said["处理"] = 处理
    shown = _shown(findings or [])
    if shown:
        said["发现"] = shown
    return said


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
        detail = f"自检器异常（渲染主链不受影响）: {e}；{''.join(traceback.format_exc().splitlines()[-2:])}"
        items = [{"项": "自检执行", "结论": FAIL, "证据": detail,
                  "说明": _say("自检没跑起来（检查器自己异常了）：这次的图没有机械核对过，别当已通过",
                              原因=detail,
                              处理="点详情底部「重试」重跑拆解与渲染让自检重跑；重复出现就把这条证据发给开发")}]
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
                "证据": "门禁.json 读不出（拆解产物门禁留痕损坏）——如实标红，重跑拆解可重建",
                "说明": _say("拆解产物的门禁留痕读不出来（文件损坏），这次拆解有没有过门禁无据可查",
                            原因="门禁.json 读不出（拆解产物门禁留痕损坏）",
                            处理=HOW_GATE)}
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
    # 人话（AT-16 B2）：结论人话/原因/怎么处理与证据同一份数据（状态/绿项/缺项/告警/轮次）。
    if verdict == PASS:
        说明 = _say(f"拆解产物的机械门禁过了：绿项 {passed}/{total}、无缺项、无告警（重拆 {rounds}/{limit}）",
                    处理="无需处理")
    else:
        verdict_word = "只出了告警" if verdict == WARN else "没通过"
        说明 = _say(f"拆解产物的机械门禁{verdict_word}：绿项 {passed}/{total}、缺项 {len(missing)}、"
                    f"告警 {len(warns)}（重拆 {rounds}/{limit}）",
                    原因="；".join([f"缺项：{x}" for x in missing] + [f"告警：{x}" for x in warns]) or lines[0],
                    处理=HOW_GATE,
                    findings=[{"文字": f"缺项：{x}"} for x in missing] + [{"文字": f"告警：{x}"} for x in warns])
    return {"项": GATE_ITEM_NAME, "结论": verdict, "证据": "\n".join(lines), "说明": 说明}


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
        _check_residue(body, ents, render, bridge_id),
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
        return {"项": "次序对账", "结论": PASS, "证据": "全沿用/无图：清单无绑图行、正文无 @图片N、提交快照为空——三方一致",
                "说明": _say("全沿用、没换图：清单没有绑图行、正文没有 @图片N、提交快照为空——三方一致",
                            处理="无需处理")}
    problems: list[dict] = []                       # 同一份发现：文字→证据原文，行号/图号→人话
    nums = sorted(set(row_to_no.values()))          # 唯一图号集合（组内多行同号算一个）
    if nums != list(range(1, len(nums) + 1)):
        problems.append(_finding("发号", f"发号不连续: row→图号={row_to_no}"))
    if refs != nums:
        problems.append(_finding("正文号", f"正文图号 {refs} ≠ 应发号 {nums}"))
    if len(submitted) != len(nums):
        problems.append(_finding("提交条数",
                                 f"提交快照 {len(submitted)} 条 ≠ 发图单位 {len(nums)} 个（组内多行应只提交一条）"))
    else:
        seen_units: set[tuple] = set()
        for i, (no, url) in enumerate(submitted, 1):
            unit = replace.row_unit(no, groups)
            if unit in seen_units:
                problems.append(_finding("组内多提交",
                                         f"提交第 {i} 张来自行 {no}，但该行所在单位已有提交（组内多行多提交）",
                                         row=no, no=i))
            seen_units.add(unit)
            if row_to_no.get(no) != i:
                problems.append(_finding("次序错位",
                                         f"提交第 {i} 张来自行 {no}，但发号映射给该行的是图{row_to_no.get(no)}（应为图{i}）",
                                         row=no, no=row_to_no.get(no)))
            if url and url not in url_by_row.get(no, []):
                problems.append(_finding("提交图不符",
                                         f"提交第 {i} 张 URL 不在清单第 {no} 行绑图里: {url[:60]}",
                                         row=no, no=i))
    ev = (f"行→图号={row_to_no}；正文@图片N={refs}；提交次序={[no for no, _ in submitted]}"
          f"（清单 {ck_path.parent.name}）")
    if not problems:
        return {"项": "次序对账", "结论": PASS, "证据": "三方映射一致：" + ev,
                "说明": _say("发号、提示词里的 @图片N、提交次序三方对得上，没有错位的图", 处理="无需处理")}
    kinds = {p["类型"] for p in problems}
    words = list(dict.fromkeys(ORDER_KIND_WORD[k] for k in (p["类型"] for p in problems) if k in ORDER_KIND_WORD))
    how: list[str] = []
    if kinds & {"发号", "正文号"}:
        how.append(HOW_RECONFIRM)
    if kinds & {"提交条数", "组内多提交", "次序错位", "提交图不符"}:
        how.append(HOW_REBIND)
    return {"项": "次序对账", "结论": FAIL,
            "证据": _evidence(problems) + "｜" + ev,
            "说明": _say(f"发的图和提交的图没对上：{len(problems)} 处——" + "、".join(words or ["对不上号"]),
                        原因=_evidence(problems),
                        处理="；".join(how) or HOW_RECONFIRM,
                        findings=problems)}


def _check_residue(body: str, ents, render, bridge_id: str = "") -> dict:
    """② 绑图行残留：已传图行的原物核心词（实体名+特征词）零残留（词表=strip_core_residue 同源）。

    词表只收 ≥3 字的组：2 字组（香水/黑色这类品类与颜色词）不是特征词——我们的产品
    常与原物同品类（香水换香水），别的行描述也常带同色词，2 字组必误红（实测见探针正例）。
    """
    scanned: list[str] = []
    hits: list[dict] = []
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
                hits.append(_finding("残留",
                                     f"#{e.row_no} 的「{w}」残留于正文: …{body[left:m.end() + 10]}…",
                                     row=e.row_no, no=e.no))
                break
    ev = f"已传图行词表 {len(scanned)} 行（{'、'.join(scanned) or '无'}），正文 {len(body)} 字"
    if not hits:
        return {"项": "绑图行残留", "结论": PASS, "证据": "零残留｜" + ev,
                "说明": _say("已传图的每一行，原物的名字和特征词都没残留在提示词正文里", 处理="无需处理")}
    shown = "；".join(str(h["文字"]) for h in hits[:5])
    rows = list(dict.fromkeys(h["行号"] for h in hits if h.get("行号")))
    return {"项": "绑图行残留", "结论": FAIL,
            "证据": shown + ("…" if len(hits) > 5 else "") + "｜" + ev,
            "说明": _say("提示词正文还留着我方要换掉的原物描述词："
                         f"{len(hits)} 处（涉及行 {'、'.join(f'#{r}' for r in rows) or '未知'}）",
                        原因=shown + ("…" if len(hits) > 5 else ""),
                        处理=_how_prompt_word(bridge_id),
                        findings=hits)}


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
    problems: list[dict] = []
    if missed:
        problems.append(_finding("锁点缺口",
                                 f"必换行 {missed} 未换且无『按原样』/『按原片』/empty_confirm 记录（锁点缺口）"))
    if ghost:
        problems.append(_finding("记录矛盾",
                                 f"行 {ghost} 记了按原样/按原片但确认快照里已传图（记录与快照矛盾）"))
    if not problems:
        return {"项": "锁点对账", "结论": PASS, "证据": "未换必换行都有确认时点记录｜" + ev,
                "说明": _say("标了必须换的每一行，要么真换了，要么有『按原样/按原片』的确认记录", 处理="无需处理")}
    findings = ([_finding("锁点缺口", f"#{n} 标了必须换，但没传图、也没勾按原样", row=n) for n in missed]
                + [_finding("记录矛盾", f"#{n} 记了按原片/按原样，但提交快照里其实已经传了图", row=n) for n in ghost])
    words = []
    if missed:
        words.append(f"必换行 {'、'.join(f'#{n}' for n in missed)} 没换也没说按原样")
    if ghost:
        words.append(f"行 {'、'.join(f'#{n}' for n in ghost)} 的按原样记录与已传图矛盾")
    how = []
    if missed:
        how.append(HOW_ACK)
    if ghost:
        how.append(HOW_RECONFIRM)
    return {"项": "锁点对账", "结论": FAIL,
            "证据": _evidence(problems) + "｜" + ev,
            "说明": _say("锁点对不上：" + "；".join(words),
                        原因=_evidence(problems),
                        处理="；".join(how),
                        findings=findings)}


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
    problems: list[dict] = []
    for group in groups:
        nums = sorted({row_to_no[n] for n in group if row_to_no.get(n)})
        if not nums:
            continue                            # 组内全无图：不占号不提交，合并仍成立（零图分支）
        if len(nums) != 1:
            problems.append(_finding("组多号",
                                     f"组 {group} 图号不一致: {[{n: row_to_no[n]} for n in group if row_to_no.get(n)]}"))
        urls = sorted({u for n in group for u in url_by_row.get(n, [])})
        if len(urls) > 1:
            problems.append(_finding("组两URL",
                                     f"组 {group} 绑定了 {len(urls)} 个不同 URL（同组两 URL）: "
                                     + "、".join(u[:40] for u in urls[:3])))
    num_urls: dict[int, set[str]] = {}
    for no, urls in url_by_row.items():
        num = row_to_no.get(no)
        if num:
            num_urls.setdefault(num, set()).update(urls)
    for num in sorted(num_urls):
        if len(num_urls[num]) > 1:
            problems.append(_finding("同号两URL",
                                     f"图{num} 对应多个 URL（同号不同 URL）: "
                                     + "、".join(sorted(u[:40] for u in num_urls[num])[:3]),
                                     no=num))
    seen: dict[tuple, str] = {}
    for no, url in submitted:
        unit = replace.row_unit(no, groups)
        if unit in seen and seen[unit] != url:
            problems.append(_finding("提交同组两URL",
                                     f"提交快照里同组两 URL（行 {no} 与行 "
                                     f"{next(n for n, u in submitted if replace.row_unit(n, groups) == unit and u == seen[unit])}）",
                                     row=no))
        seen.setdefault(unit, url)
    ev = (f"确认组={groups or '无'}；行→图号={row_to_no}；组内 URL 归一后提交 {len(submitted)} 条")
    if not problems:
        return {"项": "组绑定一致性", "结论": PASS, "证据": "组内行同图号同 URL（无组或全组归一）｜" + ev,
                "说明": _say("每个组内部都是同一张图、同一个图号（没有组时等于没这条约束）", 处理="无需处理")}
    return {"项": "组绑定一致性", "结论": FAIL,
            "证据": _evidence(problems) + "｜" + ev,
            "说明": _say(f"合并过的组里对不上：{len(problems)} 处——组内本应只有一张图/一个图号",
                        原因=_evidence(problems),
                        处理=HOW_GROUP,
                        findings=problems)}
