#!/usr/bin/env python
"""AT-11 M1 后端探针：发号组感知 + keep_origin 次序 + 锁点 + 自检第4项 + set_asset 扩展。

跑法：wanrepro/ 下 `python -m automation.probe_at11_m1`（纯离线：无 HTTP、无付费调用；
判据全对才 exit 0，任何一步失败如实 FAIL 并 exit 1——判 returncode，禁子串判通过）。

六组判据（docs/nodes/AT-11.md v4 §3.2-3.5/§四；L1 审计 B1/B2/B3/B5 闭合锚定）：
  [1] 正例（pop-4 baseline：#1有图#5无图→一张唯一图分支）：成组 #1+#5 → 组写一张图
      → 确认快照恰 1 条 → 发号 {1:1, 5:1}（组内两行一图号）→ 真实 render_prompt 正文
      统一 @图片1 → 自检四项全 PASS。
  [2] 自检反例：同组两 URL → ④组绑定一致性 FAIL（同号不同 URL 同证）；组外两行同
      URL → 独立图号不误红；同组两行都进提交快照 → ①次序对账 FAIL。
  [3] set_asset decision（B5 扩展）：keep_origin 落『行决策覆盖』+回显锁点剩余；
      restore 删决策；与 image_url/clear/text 互斥；未知值拒绝；非 awaiting_assets
      拒绝；组内行拒绝（组=原子单位）；restore 无决策拒绝。
  [4] keep_origin 次序（B2）：确认置换同锁内有效决策——屏蔽行的 pending 不进
      assets_confirmed、『按原片确认』同快照落账；有效图全被屏蔽 → empty_confirm
      合同落账；发号前胜出（正文无 @图片N）。
  [5] 校验/边界（B3）：品牌视觉行入组拒绝；先降后合拒绝；解组清理『组元数据』
      键且『行决策覆盖』不无故清（不留半组替换的任何残留路径）。
  [6] cards 组卡投影（B4）：成员卡挂同一『组』投影（主行/代表帧/单框同源自主行，
      镜次并集）；主行无自身框 → 单框=None+单框降级（不搬他人框）；无组卡 组=None。
  [7] M5 缺陷（roll call 组=纯图号）：pop-4-variant 夹具（#1 命名=参考片旧描述
      「有粉色标签款」，#1/#5 同 URL）成组 [[1,5]] → 正文开头 roll call 恰为
      「全程使用参考图中的@图片1。」（#1 命名核心词全扫零命中）、唯一图号恰 [1]、
      自检四项全 PASS；反例：同夹具不成组 → roll call 仍保留命名（只清组、不越界
      清洗单行）。

真数据只读：pop-4 清单/sidecar、auto-0012 任务文件探针前后 SHA256 必须相同；
夹具目录 analysis/reverse/probe-at11-m1/ 与临时任务目录用完即删（零残留）。
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]          # wanrepro/
sys.path.insert(0, str(ROOT))
from automation import cards, replace, selfcheck, state  # noqa: E402
import importlib.util                                    # noqa: E402

POP4 = ROOT / "analysis" / "reverse" / "pop-4"
REAL_TASK_DIR = ROOT / "automation" / "projects" / "auto-0012"
FIXTURE_BRIDGE = "probe-at11-m1"
FIXTURE_DIR = ROOT / "analysis" / "reverse" / FIXTURE_BRIDGE

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" —— {detail}" if detail and not cond else ""))
    return bool(cond)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_hub():
    spec = importlib.util.spec_from_file_location("at11_probe_hub", ROOT / "ui" / "hub_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hub = load_hub()
render = selfcheck._load_render()
REAL_TASK = state.load(REAL_TASK_DIR)
REAL_MD = (POP4 / "待确认清单.md").read_text(encoding="utf-8-sig")
REAL_SIDE = (POP4 / "待确认清单.json").read_text(encoding="utf-8-sig")

TMP_TASK = Path(tempfile.mkdtemp(prefix="probe-at11-m1-task-"))
READONLY_WATCH = [POP4 / "待确认清单.md", POP4 / "待确认清单.json",
                  ROOT / "analysis" / "reverse" / "pop-4-variant" / "待确认清单.md",   # AT-11 M5 [7] 夹具源（只读复制）
                  REAL_TASK_DIR / "任务.json"]


# ---------------------------------------------------------------- 夹具与内存任务替身

def build_fixture() -> None:
    """夹具桥目录：pop-4 的清单/sidecar/拆解/意图建议副本（probe 自有目录，用完即删）。"""
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    for name in ("待确认清单.md", "待确认清单.json", "拆解.md", "意图建议.md"):
        src = POP4 / name
        if src.is_file():
            shutil.copy2(src, FIXTURE_DIR / name)


TASKS: dict[str, dict] = {}


def _state_load(task_dir):
    return copy.deepcopy(TASKS[str(task_dir)])


def _state_save(task_dir, task):
    TASKS[str(task_dir)] = copy.deepcopy(task)


def make_task(pending: list[dict], st: str = "awaiting_assets") -> dict:
    task = copy.deepcopy(REAL_TASK)
    keep = {"local_id", "bridge_id", "created_at", "intent", "ingestion", "gen_params"}
    task = {k: v for k, v in task.items() if k in keep}
    task.update({"local_id": "probe-at11-m1", "bridge_id": FIXTURE_BRIDGE,
                 "state": st, "pending_replace": copy.deepcopy(pending), "history": []})
    return task


def install_task(pending: list[dict], st: str = "awaiting_assets") -> None:
    TASKS[str(TMP_TASK)] = make_task(pending, st)


def scenario(md: str | None = None, side_extra: dict | None = None):
    """重置夹具盘面 + 安装 state/hub 内存替身，返回 patches 列表（start/stop 自己管）。"""
    (FIXTURE_DIR / "待确认清单.md").write_text(md if md is not None else REAL_MD, encoding="utf-8")
    payload = json.loads(REAL_SIDE)
    payload.update(side_extra or {})
    (FIXTURE_DIR / "待确认清单.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return [patch.object(state, "load", side_effect=_state_load),
            patch.object(state, "save", side_effect=_state_save),
            patch.object(hub, "_task_dir", return_value=TMP_TASK)]


class _Patches:
    def __init__(self, ps):
        self.ps = ps

    def __enter__(self):
        for p in self.ps:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self.ps):
            p.stop()
        return False


def build_prompt(groups: list[list[int]], decisions: dict[int, str]) -> str:
    """真实 render_prompt（seedance-flow 模板，内存实体已按组/覆盖发号）→ 正文。"""
    disc = (FIXTURE_DIR / "拆解.md").read_text(encoding="utf-8-sig")
    layers, _order = render.parse_layers(disc)
    tpl_text = (ROOT / "templates" / "seedance-flow.md").read_text(encoding="utf-8-sig")
    fm, body = render.split_frontmatter(tpl_text)
    secs, _opts = render.parse_sections(body)
    ents = render.assign_numbers(render.parse_checklist(FIXTURE_DIR / "待确认清单.md"),
                                 groups, decisions)
    reasons = render.suggestion_reasons(FIXTURE_DIR / "意图建议.md")
    return render.render_prompt(fm, secs, layers, ents, render.header_facts(disc),
                                FIXTURE_BRIDGE, "seedance-flow", reasons)


def run_confirm() -> dict:
    replace.run_replace(TMP_TASK, assets=None, map_spec=None, name_spec=None,
                        confirm=True, verbose=False)
    return TASKS[str(TMP_TASK)]


def selfcheck_items() -> dict[str, dict]:
    items = selfcheck._check_all(TMP_TASK)
    return {x["项"]: x for x in items}


def main() -> int:
    hashes_before = {str(p): sha256(p) for p in READONLY_WATCH}
    build_fixture()
    try:
        # ================================================= [1] 正例：pop-4 baseline 一张唯一图分支
        print("[1] 正例：成组 #1+#5 → 一张唯一图 → 一个图号 → 正文统一 @图片1 → 自检四项 PASS")
        url1 = next(r["cur_assets"].strip() for r in replace.parse_rows(REAL_MD) if r["no"] == 1)
        with _Patches(scenario()):
            install_task([])
            out = hub.group_rows("probe-at11-m1", [1, 5])
            check("成组 #1+#5 落 sidecar『同实体确认』", out.get("组") == [[1, 5]], str(out))
            hub.set_asset("probe-at11-m1", 1, image_url=url1)
            rows = {r["no"]: r["cur_assets"] for r in
                    replace.parse_rows((FIXTURE_DIR / "待确认清单.md").read_text(encoding="utf-8-sig"))}
            check("组联动：#1/#5 同 URL（B3 一张唯一图归一）",
                  rows.get(1) == rows.get(5) == url1, json.dumps(rows, ensure_ascii=False)[:160])
            task = TASKS[str(TMP_TASK)]
            check("组联动 pending 两行同 URL",
                  [x["no"] for x in task["pending_replace"]] == [1, 5]
                  and len({x["url"] for x in task["pending_replace"]}) == 1,
                  json.dumps(task["pending_replace"], ensure_ascii=False)[:160])
            task = run_confirm()
            confirmed = task.get("assets_confirmed") or []
            check("确认快照恰 1 条（组内 N 行一图一条）", len(confirmed) == 1 and confirmed[0]["no"] == 1,
                  json.dumps(confirmed, ensure_ascii=False)[:160])
            check("确认后 → rendering", task["state"] == "rendering", task["state"])
            ents = render.parse_checklist(FIXTURE_DIR / "待确认清单.md")
            groups = replace.load_groups(FIXTURE_BRIDGE)
            decisions = replace.load_row_decisions(FIXTURE_BRIDGE)
            render.assign_numbers(ents, groups, decisions)
            mapping = {e.row_no: e.no for e in ents if e.no}
            check("发号组感知 {1:1, 5:1}（L1 基线从 {1:1, 5:2} 修正）",
                  mapping == {1: 1, 5: 1}, str(mapping))
            prompt = build_prompt(groups, decisions)
            refs = sorted({int(n) for n in re.findall(r"@图片(\d+)", prompt)})
            check("真实 render_prompt 正文图号恰 [1]（统一 @图片1，L1 基线原为 [1,2]）",
                  refs == [1], f"refs={refs}")
            check("正文开头统一引用 @图片1（不含 @图片2）",
                  "@图片1" in prompt and "@图片2" not in prompt, prompt[:120])
            (TMP_TASK / "提示词.md").write_text(prompt, encoding="utf-8")
            by = selfcheck_items()
            check("自检四项全 PASS（次序/残留/锁点/组绑定一致性）",
                  list(by) == ["次序对账", "绑图行残留", "锁点对账", "组绑定一致性"]
                  and all(x["结论"] == "PASS" for x in by.values()),
                  json.dumps(by, ensure_ascii=False)[:400])

        # ================================================= [2] 自检反例
        print("[2] 反例：同组两 URL / 同号不同 URL → ④ FAIL；组外同 URL 独立图号不误红；同组两提交 → ① FAIL")
        md_lines = REAL_MD.splitlines()
        fixed = []
        url_b = url1.replace(".png", "-b.png")
        for line in md_lines:
            cells = replace.row_cells(line)
            if cells and re.fullmatch(r"\d+", cells[0] or "") and int(cells[0]) == 5 \
                    and "资产图" not in line and "---" not in cells[1]:
                while len(cells) < 5:
                    cells.append("")
                cells[4] = url_b
                line = "| " + " | ".join(cells) + " |"
            fixed.append(line)
        md_two_urls = "\n".join(fixed)
        with _Patches(scenario(md=md_two_urls, side_extra={"同实体确认": [[1, 5]]})):
            install_task([{"no": 1, "img": "", "url": url1, "name": "", "desc": ""},
                          {"no": 5, "img": "", "url": url_b, "name": "", "desc": ""}])
            t = TASKS[str(TMP_TASK)]
            t["assets_confirmed"] = [{"no": 1, "url": url1}, {"no": 5, "url": url_b}]
            (TMP_TASK / "提示词.md").write_text("全程使用参考图中的@图片1 产品。x" * 20, encoding="utf-8")
            by = selfcheck_items()
            check("同组两 URL → ④组绑定一致性 FAIL（同组两URL+同号不同URL 证据）",
                  by["组绑定一致性"]["结论"] == "FAIL"
                  and "同组两 URL" in by["组绑定一致性"]["证据"]
                  and "同号不同 URL" in by["组绑定一致性"]["证据"],
                  by["组绑定一致性"]["证据"][:240])
            check("同组两行都进提交快照 → ①次序对账 FAIL（组内多行多提交）",
                  by["次序对账"]["结论"] == "FAIL" and "提交" in by["次序对账"]["证据"],
                  by["次序对账"]["证据"][:240])
        with _Patches(scenario(side_extra={"同实体确认": [[1, 5]]})):
            install_task([])
            hub.set_asset("probe-at11-m1", 1, image_url=url1)   # 组写归一
            ents = render.parse_checklist(FIXTURE_DIR / "待确认清单.md")
            render.assign_numbers(ents, [[1, 5]], {})
            mapping = {e.row_no: e.no for e in ents if e.no}
            (TMP_TASK / "提示词.md").write_text(build_prompt([[1, 5]], {}), encoding="utf-8")
            t = TASKS[str(TMP_TASK)]
            t["assets_confirmed"] = [{"no": 1, "url": url1}]
            by = selfcheck_items()
            check("组已归一（组外两行同 URL 独立图号）→ 四项 PASS",
                  all(x["结论"] == "PASS" for x in by.values()) and mapping == {1: 1, 5: 1},
                  json.dumps({k: v["结论"] for k, v in by.items()}, ensure_ascii=False))
        with _Patches(scenario()):
            install_task([])
            ents = render.parse_checklist(FIXTURE_DIR / "待确认清单.md")
            render.assign_numbers(ents, [], {})      # 无组：同 URL 也独立发号
            mapping = {e.row_no: e.no for e in ents if e.no}
            same_url_rows = [n for n, no in mapping.items()
                             if any(e.row_no == n and e.image == url1 for e in ents)]
            check("无组时逐行独立发号（同 URL 不合并，禁全局 URL 去重）",
                  len(same_url_rows) != len(set(mapping[n] for n in same_url_rows))
                  or len(set(mapping.values())) == len(mapping),
                  str(mapping))

        # ================================================= [3] set_asset decision（B5 扩展）
        print("[3] set_asset decision：keep_origin/restore 落『行决策覆盖』+ 锁点剩余 + 校验")
        with _Patches(scenario()):
            install_task([])
            out = hub.set_asset("probe-at11-m1", 5, decision="keep_origin")
            check("keep_origin 落覆盖表并回显", out.get("覆盖") == {"5": "keep_origin"}
                  and out.get("决策") == "keep_origin", str(out))
            check("锁点剩余回显（#1 已传图 → 剩余 []）", out.get("锁点剩余") == [], str(out))
            out = hub.set_asset("probe-at11-m1", 5, decision="restore")
            check("restore 删决策（覆盖表空）", out.get("覆盖") == {}, str(out))
            for kwargs, why in [
                ({"decision": "bogus"}, "未知 decision 值"),
                ({"decision": "keep_origin", "image_url": url1}, "decision 与 image_url 互斥"),
                ({"decision": "keep_origin", "clear": True}, "decision 与 clear 互斥"),
                ({"decision": "keep_origin", "text": "x"}, "decision 与 text 互斥"),
            ]:
                try:
                    hub.set_asset("probe-at11-m1", 5, **kwargs)
                    check(f"拒绝：{why}", False, "未拒绝")
                except hub.HubError as e:
                    check(f"拒绝：{why}", "互斥" in str(e) or "keep_origin / restore" in str(e), str(e))
            try:
                hub.set_asset("probe-at11-m1", 5, decision="restore")
                check("拒绝：restore 无决策", False, "未拒绝")
            except hub.HubError as e:
                check("拒绝：restore 无决策（如实文案）", "没有『按原片』决策" in str(e), str(e))
            TASKS[str(TMP_TASK)]["state"] = "rendering"
            try:
                hub.set_asset("probe-at11-m1", 5, decision="keep_origin")
                check("拒绝：非 awaiting_assets（确认后覆盖表锁定）", False, "未拒绝")
            except hub.HubError as e:
                check("拒绝：非 awaiting_assets（确认后覆盖表锁定不可改）",
                      "awaiting_assets" in str(e), str(e))
        with _Patches(scenario(side_extra={"同实体确认": [[1, 5]]})):
            install_task([])
            try:
                hub.set_asset("probe-at11-m1", 1, decision="keep_origin")
                check("拒绝：组内单行降级（组=原子单位，先拆开）", False, "未拒绝")
            except hub.HubError as e:
                check("拒绝：组内单行降级（组=原子单位，先拆开）", "确认组" in str(e), str(e))

        # ================================================= [4] keep_origin 次序（B2）
        print("[4] keep_origin 次序：有效决策发号前胜出；全屏蔽 → empty_confirm 合同")
        with _Patches(scenario(side_extra={"行决策覆盖": {"5": "keep_origin"}})):
            install_task([{"no": 1, "img": "", "url": url1, "name": "", "desc": ""},
                          {"no": 5, "img": "", "url": url1, "name": "", "desc": ""}])
            task = run_confirm()
            confirmed = task.get("assets_confirmed") or []
            check("确认快照只含有效行（#5 keep_origin 被屏蔽，恰 1 条）",
                  [x["no"] for x in confirmed] == [1], json.dumps(confirmed)[:160])
            check("『按原片确认』同快照落账", (task.get("按原片确认") or {}).get("rows") == [5],
                  str(task.get("按原片确认")))
            check("确认后 → rendering", task["state"] == "rendering", task["state"])
            prompt = build_prompt([], {5: "keep_origin"})
            check("发号前胜出：#5 有效视图无图（无组时 #1 独立占号，#5 不占号）",
                  "@图片1" in prompt, prompt[:100])
        with _Patches(scenario(side_extra={"行决策覆盖": {"1": "keep_origin"}})):
            install_task([{"no": 1, "img": "", "url": url1, "name": "", "desc": ""}])
            task = run_confirm()
            check("有效图全被屏蔽 → empty_confirm 合同落账（assets_confirmed=[] + empty_confirm）",
                  task.get("assets_confirmed") == [] and task.get("empty_confirm") is True
                  and (task.get("按原片确认") or {}).get("rows") == [1],
                  json.dumps({k: task.get(k) for k in ("assets_confirmed", "empty_confirm", "按原片确认")},
                             ensure_ascii=False)[:240])
            ents = render.parse_checklist(FIXTURE_DIR / "待确认清单.md")
            render.assign_numbers(ents, [], {1: "keep_origin"})
            check("keep_origin 行不占号（正文无 @图片N）",
                  all(e.no == 0 for e in ents), str({e.row_no: e.no for e in ents}))
            (TMP_TASK / "提示词.md").write_text("纯文字描述正文。" * 40, encoding="utf-8")
            by = selfcheck_items()
            check("自检四项 PASS（锁点由『按原片确认』覆盖，不自证）",
                  all(x["结论"] == "PASS" for x in by.values()),
                  json.dumps({k: v["证据"] for k, v in by.items()}, ensure_ascii=False)[:360])

        # ================================================= [5] 校验/边界（B3）
        print("[5] 边界：品牌行入组拒绝；先降后合拒绝；解组清组元数据、行覆盖不无故清")
        with _Patches(scenario()):
            install_task([])
            try:
                hub.group_rows("probe-at11-m1", [1, 7])
                check("拒绝：品牌视觉行入组", False, "未拒绝")
            except hub.HubError as e:
                check("拒绝：品牌视觉行入组（品牌位不读图不占号）",
                      "品牌视觉" in str(e) and "7" in str(e), str(e))
            hub.set_asset("probe-at11-m1", 5, decision="keep_origin")
            try:
                hub.group_rows("probe-at11-m1", [1, 5])
                check("拒绝：先降后合", False, "未拒绝")
            except hub.HubError as e:
                check("拒绝：先降后合（先恢复再合并，不留半组替换）",
                      "keep_origin" in str(e), str(e))
            hub.set_asset("probe-at11-m1", 5, decision="restore")   # 先恢复再合并
            replace.save_group_meta(FIXTURE_BRIDGE,
                                    {"1-5": {"主行": 1, "组名": "紫色管状产品"}})
            hub.group_rows("probe-at11-m1", [1, 5])
            check("『组元数据』随成组保留（组键对应现存组）",
                  replace.load_group_meta(FIXTURE_BRIDGE).get("1-5", {}).get("主行") == 1,
                  str(replace.load_group_meta(FIXTURE_BRIDGE)))
            decisions_before = replace.load_row_decisions(FIXTURE_BRIDGE)
            out = hub.ungroup_rows("probe-at11-m1", [1, 5])
            check("解组清理『组元数据』键（不留 stale 组键）",
                  out.get("组") == [] and replace.load_group_meta(FIXTURE_BRIDGE) == {},
                  str(out))
            check("解组不动『行决策覆盖』（#5 keep_origin 保留）",
                  replace.load_row_decisions(FIXTURE_BRIDGE) == decisions_before,
                  str(replace.load_row_decisions(FIXTURE_BRIDGE)))
            check("半组残留零路径：解组后无组无元数据、决策如实保留",
                  replace.load_groups(FIXTURE_BRIDGE) == []
                  and replace.load_group_meta(FIXTURE_BRIDGE) == {},
                  "ungroup 后组表与元数据均空")

        # ================================================= [6] cards 组卡投影（B4）
        print("[6] cards 组卡投影：主行代表帧/单框同源；镜次并集；诚实降级不搬他人框")
        saved_groups = replace.load_groups
        saved_meta = replace.load_group_meta

        def groups_pop4(_bridge):
            return [[1, 5]]

        def meta_pop4(_bridge):
            return {"1-5": {"主行": 5, "组名": "紫色管状产品"}}
        try:
            with patch.object(replace, "load_groups", groups_pop4), \
                 patch.object(replace, "load_group_meta", meta_pop4):
                cbs = cards.build("pop-4", "auto-0012")
            by_no = {c["n"]: c for c in cbs}
            g1, g5 = by_no[1]["组"], by_no[5]["组"]
            check("成员卡挂同一『组』投影（主行=组元数据落盘值 5）",
                  g1 and g5 and g1 == g5 and g5["主行"] == 5 and g5["组名"] == "紫色管状产品",
                  json.dumps(g5, ensure_ascii=False, default=str)[:200])
            check("组代表帧同源自主行 #5（键镜3，非成员并集帧）",
                  g5["代表帧"] and g5["代表帧"].get("镜次") == 3,
                  json.dumps(g5["代表帧"], ensure_ascii=False)[:160])
            check("组单框=主行自身框（行=5，恰一框）",
                  g5["单框"] and g5["单框"]["行"] == 5 and g5["单框降级"] is False,
                  json.dumps(g5["单框"], ensure_ascii=False)[:160])
            check("镜次并集=成员位置键/意图建议镜次（含 3）",
                  3 in g5["镜次并集"] and g5["镜次并集"] == sorted(set(g5["镜次并集"])),
                  str(g5["镜次并集"]))
            check("无组卡 组=None（#2）", by_no[2]["组"] is None, str(by_no[2]["组"]))

            def meta_primary7(_bridge):
                return {"1-5": {"主行": 7, "组名": ""}}   # 非成员 → 落回缺省最小行
            with patch.object(replace, "load_groups", groups_pop4), \
                 patch.object(replace, "load_group_meta", meta_primary7):
                cbs = cards.build("pop-4", "auto-0012")
            g = next(c for c in cbs if c["n"] == 1)["组"]
            check("主行非法（非成员）→ 缺省最小行 1，不猜不炸", g["主行"] == 1, str(g["主行"]))

            def groups_57(_bridge):
                return [[5, 7]]

            def meta_57(_bridge):
                return {"5-7": {"主行": 7, "组名": ""}}   # #7 无自身框 → 诚实降级
            with patch.object(replace, "load_groups", groups_57), \
                 patch.object(replace, "load_group_meta", meta_57):
                cbs = cards.build("pop-4", "auto-0012")
            by_no = {c["n"]: c for c in cbs}
            g = by_no[5]["组"]
            check("主行无自身框 → 单框=None + 单框降级=True（绝不搬 #5 的框充数）",
                  g["主行"] == 7 and g["单框"] is None and g["单框降级"] is True,
                  json.dumps({k: g[k] for k in ("主行", "单框", "单框降级")}, ensure_ascii=False))
        finally:
            replace.load_groups = saved_groups
            replace.load_group_meta = saved_meta

        # ================================================= [7] M5 缺陷：roll call 合并组=纯图号引用
        print("[7] M5：pop-4-variant 夹具成组 [[1,5]] → roll call 纯 @图片1（去旧命名 bit）；反例不成组保留命名")
        VARIANT = ROOT / "analysis" / "reverse" / "pop-4-variant"
        VARIANT_MD = (VARIANT / "待确认清单.md").read_text(encoding="utf-8-sig")
        with _Patches(scenario(md=VARIANT_MD, side_extra={"同实体确认": [[1, 5]]})):
            for fname in ("拆解.md", "意图建议.md"):       # 夹具同源：variant 的拆解/意图建议副本
                shutil.copy2(VARIANT / fname, FIXTURE_DIR / fname)
            install_task([])
            ents = render.parse_checklist(FIXTURE_DIR / "待确认清单.md")
            render.assign_numbers(ents, [[1, 5]], {})
            mapping = {e.row_no: e.no for e in ents if e.no}
            check("夹具成立：组发号 {1:1, 5:1}，#1 命名=「有粉色标签款」（auto-0024 同源）在场",
                  mapping == {1: 1, 5: 1} and next(e for e in ents if e.row_no == 1).name == "有粉色标签款",
                  str(mapping))
            prompt = build_prompt([[1, 5]], {})
            body = selfcheck._prompt_body(prompt)
            roll_exact = "全程使用参考图中的@图片1。"
            # 流式正文连写为一段：roll call 是正文开头的精确前缀（auto-0024 现场同形态）。
            check("正文开头 roll call 恰为「全程使用参考图中的@图片1。」（组内纯图号，无命名 bit）",
                  body.startswith(roll_exact), body[:80])
            ent1 = next(e for e in ents if e.row_no == 1)
            core = render.entity_noun_core(ent1)
            grams = sorted({core} | set(render._noun_grams(core)), key=len, reverse=True)
            hits = [w for w in grams if w in body]
            check("#1 命名核心词全扫零命中（旧描述不进正文任何位置，§六.2 零旧描述残留）",
                  not hits, str(hits[:6]))
            refs = sorted({int(n) for n in re.findall(r"@图片(\d+)", prompt)})
            check("@图片N 唯一图号恰 [1]", refs == [1], f"refs={refs}")
            url_v = next(r["cur_assets"].strip() for r in replace.parse_rows(VARIANT_MD) if r["no"] == 1)
            t = TASKS[str(TMP_TASK)]
            t["assets_confirmed"] = [{"no": 1, "url": url_v}]
            (TMP_TASK / "提示词.md").write_text(prompt, encoding="utf-8")
            by = selfcheck_items()
            check("自检四项全 PASS（绑图行残留不再被 roll call 顶爆）",
                  list(by) == ["次序对账", "绑图行残留", "锁点对账", "组绑定一致性"]
                  and all(x["结论"] == "PASS" for x in by.values()),
                  json.dumps(by, ensure_ascii=False)[:400])
        with _Patches(scenario(md=VARIANT_MD)):
            install_task([])
            ents = render.parse_checklist(FIXTURE_DIR / "待确认清单.md")
            render.assign_numbers(ents, [], {})        # 反例：不成组 → 各自独立发号
            mapping = {e.row_no: e.no for e in ents if e.no}
            roll = render.entity_roll_call(ents)
            check("反例（防越界清洗）：不成组 roll call 保留命名「有粉色标签款」（只清组、单行零回归）",
                  mapping.get(1) == 1 and mapping.get(5) == 2
                  and "有粉色标签款" in roll and "@图片2" in roll,
                  f"mapping={mapping} roll={roll[:80]}")

    finally:
        shutil.rmtree(FIXTURE_DIR, ignore_errors=True)
        shutil.rmtree(TMP_TASK, ignore_errors=True)
        unchanged = {str(p): sha256(p) for p in READONLY_WATCH} == hashes_before
        no_fixture = not FIXTURE_DIR.exists() and not TMP_TASK.exists()
        print(f"[cleanup] 夹具清理：fixture_removed={not FIXTURE_DIR.exists()} "
              f"task_tmp_removed={not TMP_TASK.exists()} 真数据只读={unchanged}")
        check("真数据只读（pop-4 清单/sidecar、auto-0012 任务文件 SHA256 不变）", unchanged,
              "有文件被改动！" if not unchanged else "")
        check("跑后零残留（夹具目录与临时任务目录已删）", no_fixture, "")

    failed = [r for r in RESULTS if not r[1]]
    total = len(RESULTS)
    print(f"\nprobe_at11_m1: {total - len(failed)}/{total} PASS"
          + (f"，FAIL {len(failed)} 项" if failed else ""))
    for name, _ok, detail in failed:
        print(f"  [FAIL] {name} —— {detail[:200]}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
