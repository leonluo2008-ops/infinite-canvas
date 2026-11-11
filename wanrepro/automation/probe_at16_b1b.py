#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AT-16 B1b 探针：品牌图发号/提交合同**五点同源**端到端（纯离线：自起隔离实例 + 真写路径，真实任务只读）。

跑法：wanrepro/ 下 `python -m automation.probe_at16_b1b`（判 returncode，禁子串判通过）。
背景缺陷（auto-0042 实证）：pop-3 第 12 行（品牌视觉（置换位））已绑图，但 parse_checklist
把 image 置空、assign_numbers 对 brand 行禁占号、run_replace 按 pending_replace 流水装配
→ 提交清单 [1,2,12] ≠ 发图单位 2，自检次序对账 FAIL。B1b 修复：行→图号、正文@图片N、
提交清单（assets_confirmed）、确认快照、资产清单渲染＋引用总数——五点全部从同一份有效映射
（清单当前「资产图」绑定 + 组 + 行决策）装配（render.effective_units 一处判）。
自检**保持独立**：次序对账仍自己读盘面快照与正文比对，人为污染快照必须仍 FAIL。

隔离体系（probe_at16_b1a 同一套）：
  · probe_guard.projects_snapshot 跑前/跑后对账（真实 projects 零变化）；
  · tempfile.mkdtemp(prefix="wanprobe-at16b1b-") + iso/projects 作 WANREPRO_AUTO_DIR；
  · probe_guard.StubBridge() + probe_guard.free_port() 起 ui/server.py 隔离实例；
  · GET /api/hub/where 断言 tasks_dir == 探针根，不同源拒跑；
  · 夹具桥目录 analysis/reverse/probe-at16b1b-<场景> 跑完删；真实任务/桥只读。

写清单/上传/清空/决策**走 HTTP 真实写路径**（POST /api/asset/upload + POST /api/hub/asset）；
确认/渲染在探针进程内直调（隔离实例的 confirm 端点会另起后台渲染 job，与本探针抢状态）。
渲染直调 render.parse_layers / split_frontmatter / parse_sections / render_prompt /
render_assets（不发网络、不调模型）。
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]          # wanrepro/
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ui"))

from automation import config, probe_guard  # noqa: E402

RESULTS: list[tuple[str, bool]] = []
PNG_A = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c626001000000ffff03000006000557bfabd4"
    "0000000049454e44ae426082")
PNG_B = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "b5c3ce790000000d49444154789c62600100000005e2acb630"
    "0000000049454e44ae426082")
PNG_C = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000"
    "907753de0000000c49444154789c63e03a21070001d000f1245c91f4"
    "0000000049454e44ae426082")

# 六组场景各一套夹具（隔离根内 auto-96xx + 仓库 analysis 内 probe-at16b1b-s<N> 桥目录，跑完删）
TASKS = {n: f"auto-96{n:02d}" for n in range(1, 7)}
BRIDGES = {n: f"probe-at16b1b-s{n}" for n in range(1, 7)}
DISC_FIXTURE = ROOT / "tests" / "fixtures" / "at11-m1" / "拆解.md"

# 夹具清单（AT-16 B1b 实施书指定；与 at11-m1 拆解同一条 POP 片，行号 1/2/3/6/12）
CK_B1B = """# 待确认清单 · {bridge}

| # | 类别 | 模型看到什么 | 命名 | 资产图 | 锁什么 | 建议 | 用我的描述 |
|---|------|-------------|------|--------|--------|------|------------|
| 1 | 主体 | 细长圆柱体紫色表面，顶部为银色金属圆柱盖，管体侧面印有粉白字母排版 |  |  | 外形比例 | 建议准备 |  |
| 2 | 人物 | A：20-25岁女性，棕色长波浪卷发，画面位置：九宫格5·镜1 |  |  | 发型与轮廓 | 建议准备 |  |
| 3 | 场景 | 日光晴天海滨，白色拍岸海浪 |  |  | 光影方向 | 建议准备 |  |
| 6 | 道具 | A：紫色圆柱管身配亮银色金属盖的香水喷雾小物体 |  |  | 颜色、支点位置 | 建议准备 |  |
| 12 | 品牌视觉（置换位） | 参考片品牌位 标语品牌字（出现于：00:05-00:07）：「POP!」｜红色无衬线粗体大字｜正红 | 品牌文字 |  | 客户品牌字样（待填） | 必须换 |  |
"""


def check(name: str, cond: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" —— {detail}" if detail and not cond else ""))
    return bool(cond)


def summary() -> int:
    fails = [n for n, ok in RESULTS if not ok]
    print(f"\n[probe-at16-b1b] {len(RESULTS) - len(fails)}/{len(RESULTS)} PASS")
    for n in fails:
        print(f"  FAIL {n}")
    return 1 if fails else 0


# ---------------------------------------------------------------- 探针进程内助手

def probe_make_task(local_id: str, bridge_id: str) -> Path:
    """b1a 同款任务夹具（隔离根内 awaiting_assets，跑完连根删）。"""
    from automation import config as _config
    tdir = Path(_config.PROJECTS_DIR) / local_id
    shutil.rmtree(tdir, ignore_errors=True)
    (tdir / "inbox").mkdir(parents=True, exist_ok=True)
    task = {
        "local_id": local_id, "bridge_id": bridge_id,
        "created_at": "2026-01-01T00:00:00+08:00", "state": "awaiting_assets",
        "history": [{"at": "2026-01-01T00:00:00+08:00", "from": None, "to": "awaiting_assets",
                     "note": "probe-at16b1b 夹具"}],
        "intent": "AT-16 B1b 探针夹具", "assets": [],
        "ingestion": {"zip_sha256": f"probe-{local_id}", "zip_name": "probe",
                      "video_name": "", "text_name": "", "ignored": []},
    }
    (tdir / "任务.json").write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
    return tdir


def setup_scenario(n: int) -> tuple[str, Path]:
    """夹具桥目录（清单 + at11-m1 拆解）+ 隔离根内任务；返回 (bridge_id, task_dir)。"""
    bridge = BRIDGES[n]
    bdir = ROOT / "analysis" / "reverse" / bridge
    shutil.rmtree(bdir, ignore_errors=True)
    bdir.mkdir(parents=True, exist_ok=True)
    (bdir / "待确认清单.md").write_text(CK_B1B.format(bridge=bridge), encoding="utf-8")
    shutil.copyfile(DISC_FIXTURE, bdir / "拆解.md")
    return bridge, probe_make_task(TASKS[n], bridge)


def ck_rows(bridge: str) -> dict[int, dict]:
    path = replace.checklist_path(bridge)
    return {r["no"]: r for r in replace.parse_rows(path.read_text(encoding="utf-8-sig"))}


def render_now(bridge: str) -> tuple[str, str, dict[int, int]]:
    """直调渲染器（不发网络不调模型）：同一份有效映射 → （提示词, 资产清单, 行→图号）。"""
    render = replace.render_module()
    disc = (ROOT / "analysis" / "reverse" / bridge / "拆解.md").read_text(encoding="utf-8-sig")
    layers, _order = render.parse_layers(disc)
    fm, body = render.split_frontmatter(
        (ROOT / "templates" / "seedance-flow.md").read_text(encoding="utf-8-sig"))
    secs, _opt = render.parse_sections(body)
    ents = render.parse_checklist(replace.checklist_path(bridge))
    render.effective_units(ents, replace.load_groups(bridge), replace.load_row_decisions(bridge))
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        prompt = render.render_prompt(fm, secs, layers, ents,
                                      {"时长": "7", "画幅": "9:16"}, bridge, "seedance-flow")
        assets_md = render.render_assets(ents, layers, bridge)
    mapping = {int(e.row_no): int(e.no) for e in ents if e.no}
    return prompt, assets_md, mapping


def five_points(bridge: str, tdir: Path, prompt: str, assets_md: str) -> dict:
    """五点同源断言：图号集合 == 正文集合 == 资产清单集合 == 引用总数 == 提交条数，
    且提交第 i 条的 行→图号 恰为 i。任一不符如实 FAIL。"""
    render = replace.render_module()
    ents = render.parse_checklist(replace.checklist_path(bridge))
    units = render.effective_units(ents, replace.load_groups(bridge),
                                   replace.load_row_decisions(bridge))
    mapping = {row: u["no"] for u in units for row in u["rows"]}
    task = json.loads((tdir / "任务.json").read_text(encoding="utf-8-sig"))
    submitted = [int(x.get("no") or 0) for x in (task.get("assets_confirmed") or [])
                 if isinstance(x, dict)]
    data = {
        "行→图号": mapping,
        "正文@图片N": sorted({int(n) for n in re.findall(r"@图片(\d+)", prompt)}),
        "提交清单": submitted,
        "资产清单图号": sorted({int(n) for n in re.findall(r"\| 图片(\d+) \|", assets_md)}),
        "引用总数": int(re.search(r"本次引用 (\d+) 张图", assets_md).group(1)),
    }
    nums = sorted(set(mapping.values()))
    check("五点·图号集合 == 正文集合 == 资产清单集合",
          data["正文@图片N"] == nums and data["资产清单图号"] == nums,
          json.dumps(data, ensure_ascii=False))
    check("五点·引用总数 == 提交条数 == 图号数",
          data["引用总数"] == len(nums) == len(submitted),
          json.dumps(data, ensure_ascii=False))
    check("五点·提交第 i 条的 行→图号 恰为 i",
          [mapping.get(n) for n in submitted] == list(range(1, len(nums) + 1)),
          f"提交行序={submitted} 行→图号={mapping}")
    return data


def confirmed_of(tdir: Path) -> list[dict]:
    task = json.loads((tdir / "任务.json").read_text(encoding="utf-8-sig"))
    return [dict(x) for x in (task.get("assets_confirmed") or []) if isinstance(x, dict)]


def confirm_and_selfcheck(bridge: str, tdir: Path, prompt: str) -> list[dict]:
    """进程内确认（同源装配落 assets_confirmed）→ 写提示词 → 自检。"""
    replace.run_replace(tdir, assets=None, map_spec=None, name_spec=None,
                        confirm=True, verbose=False)
    (tdir / "提示词.md").write_text(prompt, encoding="utf-8")
    return selfcheck.run_selfcheck(tdir)


def items_by_name(items: list[dict]) -> dict[str, dict]:
    return {x.get("项"): x for x in items}


# ---------------------------------------------------------------- HTTP 真实写路径

def upload(tid: str, blob: bytes, name: str) -> str:
    """POST /api/asset/upload（raw body + X-Filename + X-Task-Id）→ local 引用。"""
    req = urllib.request.Request(
        BASE + "/api/asset/upload", data=blob, method="POST",
        headers={"Content-Type": "application/octet-stream",
                 "X-Filename": urllib.parse.quote(name, safe=""),
                 "X-Task-Id": urllib.parse.quote(tid, safe="")})
    resp = json.loads(OP.open(req, timeout=30).read())
    return str(resp.get("local") or "")


def hub_asset(tid: str, payload: dict) -> tuple[int, dict | bytes]:
    """POST /api/hub/asset：{n,image_url} / {n,clear:true} / {n,decision}。"""
    return probe_guard.http_req(
        BASE, "/api/hub/asset", "POST",
        json.dumps({"task_id": tid, **payload}, ensure_ascii=False).encode("utf-8"),
        "application/json", timeout=30)


# ---------------------------------------------------------------- 六组场景

def scenario_1() -> None:
    print("① 品牌图上传：只给第 12 行（品牌视觉（置换位））绑图 → 全链同源、无占位行、自检次序 PASS")
    bridge, tdir = setup_scenario(1)
    ref = upload(TASKS[1], PNG_A, "b1b-brand.png")
    code, body = hub_asset(TASKS[1], {"n": 12, "image_url": ref})
    check("① HTTP 贴图 #12 → 200（真实写路径收品牌行）", code == 200, f"{code} {str(body)[:160]}")
    check("① 清单第 12 行资产图 == 上传引用",
          ck_rows(bridge)[12].get("cur_assets") == ref, str(ck_rows(bridge)[12])[:160])
    prompt, assets_md, mapping = render_now(bridge)
    check("① 行→图号含 12→1（品牌行绑图即占号）", mapping.get(12) == 1, str(mapping))
    check("① 正文有 @图片1", "@图片1" in prompt, prompt[:160])
    confirm_and_selfcheck(bridge, tdir, prompt)
    snap = confirmed_of(tdir)
    check("① assets_confirmed == [{no:12, url:引用}]（一条、行 12）",
          len(snap) == 1 and snap[0].get("no") == 12 and snap[0].get("url") == ref,
          json.dumps(snap, ensure_ascii=False)[:200])
    five_points(bridge, tdir, prompt, assets_md)
    check("① 资产清单出现 | 图片1 | 品牌视觉（置换位） |（同表同号）",
          "| 图片1 | 品牌视觉（置换位） |" in assets_md, assets_md[:300])
    check("① 没有无编号占位行 | — | 品牌视觉（置换位）",
          "| — | 品牌视觉（置换位）" not in assets_md, assets_md[:300])
    order = items_by_name(selfcheck.run_selfcheck(tdir)).get("次序对账", {})
    check("① 自检次序对账 PASS（提交快照与发号单位一致）", order.get("结论") == "PASS",
          str(order)[:200])


def scenario_2() -> None:
    print("② 普通行重传覆盖：第 1 行 refA → refB → 清单只含 refB，快照不含 refA")
    bridge, tdir = setup_scenario(2)
    ref_a = upload(TASKS[2], PNG_A, "b1b-a.png")
    ref_b = upload(TASKS[2], PNG_B, "b1b-b.png")
    hub_asset(TASKS[2], {"n": 1, "image_url": ref_a})
    hub_asset(TASKS[2], {"n": 1, "image_url": ref_b})
    md = replace.checklist_path(bridge).read_text(encoding="utf-8-sig")
    check("② 清单第 1 行只含 refB（覆盖不追加）",
          ck_rows(bridge)[1].get("cur_assets") == ref_b and ref_a not in md,
          f"row1={ck_rows(bridge)[1].get('cur_assets')!r}")
    prompt, assets_md, mapping = render_now(bridge)
    check("② 行→图号 1→图1", mapping.get(1) == 1, str(mapping))
    confirm_and_selfcheck(bridge, tdir, prompt)
    snap = confirmed_of(tdir)
    check("② 确认快照 url == refB 且快照里没有 refA",
          len(snap) == 1 and snap[0].get("url") == ref_b
          and ref_a not in json.dumps(snap, ensure_ascii=False),
          json.dumps(snap, ensure_ascii=False)[:200])
    five_points(bridge, tdir, prompt, assets_md)


def scenario_3() -> None:
    print("③ 清空：1/6/12 都绑图 → 清空第 1 行 → 后续行连续重编号 6→1、12→2")
    bridge, tdir = setup_scenario(3)
    ref1 = upload(TASKS[3], PNG_A, "b1b-r1.png")
    ref6 = upload(TASKS[3], PNG_B, "b1b-r6.png")
    ref12 = upload(TASKS[3], PNG_C, "b1b-r12.png")
    hub_asset(TASKS[3], {"n": 1, "image_url": ref1})
    hub_asset(TASKS[3], {"n": 6, "image_url": ref6})
    hub_asset(TASKS[3], {"n": 12, "image_url": ref12})
    code, body = hub_asset(TASKS[3], {"n": 1, "clear": True})
    check("③ HTTP 清空 #1 → 200", code == 200, f"{code} {str(body)[:160]}")
    check("③ 清单第 1 行已空", ck_rows(bridge)[1].get("cur_assets") == "",
          repr(ck_rows(bridge)[1].get("cur_assets")))
    prompt, assets_md, mapping = render_now(bridge)
    check("③ 行→图号 连续重编号 6→1、12→2", mapping == {6: 1, 12: 2}, str(mapping))
    confirm_and_selfcheck(bridge, tdir, prompt)
    snap = confirmed_of(tdir)
    check("③ 快照 2 条且不含被清 URL",
          len(snap) == 2 and {x.get("no") for x in snap} == {6, 12}
          and ref1 not in json.dumps(snap, ensure_ascii=False),
          json.dumps(snap, ensure_ascii=False)[:200])
    five_points(bridge, tdir, prompt, assets_md)
    order = items_by_name(selfcheck.run_selfcheck(tdir)).get("次序对账", {})
    check("③ 自检次序对账 PASS", order.get("结论") == "PASS", str(order)[:200])


def scenario_4() -> None:
    print("④ dismiss / restore：第 6 行 dismiss → 屏蔽；restore → 重新入列")
    bridge, tdir = setup_scenario(4)
    ref1 = upload(TASKS[4], PNG_A, "b1b-d1.png")
    ref6 = upload(TASKS[4], PNG_B, "b1b-d6.png")
    hub_asset(TASKS[4], {"n": 1, "image_url": ref1})
    hub_asset(TASKS[4], {"n": 6, "image_url": ref6})
    code, body = hub_asset(TASKS[4], {"n": 6, "decision": "dismissed"})
    check("④ HTTP dismiss #6 → 200", code == 200, f"{code} {str(body)[:160]}")
    prompt_d, _a, mapping_d = render_now(bridge)
    check("④ dismiss 后 行→图号 == {1:1}", mapping_d == {1: 1}, str(mapping_d))
    code, body = hub_asset(TASKS[4], {"n": 6, "decision": "restore"})
    check("④ HTTP restore #6 → 200", code == 200, f"{code} {str(body)[:160]}")
    prompt, assets_md, mapping = render_now(bridge)
    check("④ restore 后 行→图号 == {1:1, 6:2}", mapping == {1: 1, 6: 2}, str(mapping))
    confirm_and_selfcheck(bridge, tdir, prompt)
    snap = confirmed_of(tdir)
    check("④ restore 后快照 2 条（行 1、行 6）",
          len(snap) == 2 and [x.get("no") for x in snap] == [1, 6],
          json.dumps(snap, ensure_ascii=False)[:200])
    five_points(bridge, tdir, prompt, assets_md)


def scenario_5() -> None:
    print("⑤ 组去重：sidecar [1,6] 成组；#1 组写联动 #6；#2 独立绑同 URL → {1:1, 6:1, 2:2}")
    bridge, tdir = setup_scenario(5)
    replace.save_groups(bridge, [[1, 6]])          # sidecar 同实体确认（实施书指定写法）
    ref_x = upload(TASKS[5], PNG_A, "b1b-x.png")
    code, body = hub_asset(TASKS[5], {"n": 1, "image_url": ref_x})
    check("⑤ HTTP 组写 #1 → 200（联动 #6）", code == 200, f"{code} {str(body)[:160]}")
    code, body = hub_asset(TASKS[5], {"n": 2, "image_url": ref_x})
    check("⑤ HTTP 独立贴 #2（同 URL）→ 200", code == 200, f"{code} {str(body)[:160]}")
    rows = ck_rows(bridge)
    check("⑤ 清单：#1/#6（组联动）与 #2（独立）都 == refX",
          rows[1].get("cur_assets") == ref_x and rows[6].get("cur_assets") == ref_x
          and rows[2].get("cur_assets") == ref_x,
          json.dumps({k: rows[k].get("cur_assets") for k in (1, 2, 6)}, ensure_ascii=False))
    prompt, assets_md, mapping = render_now(bridge)
    check("⑤ 行→图号 == {1:1, 6:1, 2:2}（组内同号、组外独立）",
          mapping == {1: 1, 6: 1, 2: 2}, str(mapping))
    confirm_and_selfcheck(bridge, tdir, prompt)
    snap = confirmed_of(tdir)
    check("⑤ 快照 2 条（行 1、行 2，组内只一条）",
          len(snap) == 2 and [x.get("no") for x in snap] == [1, 2],
          json.dumps(snap, ensure_ascii=False)[:200])
    five_points(bridge, tdir, prompt, assets_md)
    by = items_by_name(selfcheck.run_selfcheck(tdir))
    check("⑤ 自检次序对账 PASS", by.get("次序对账", {}).get("结论") == "PASS",
          str(by.get("次序对账"))[:200])
    check("⑤ 自检组绑定一致性 PASS", by.get("组绑定一致性", {}).get("结论") == "PASS",
          str(by.get("组绑定一致性"))[:200])


def scenario_6() -> None:
    print("⑥ 人为污染提交快照 → 自检仍 FAIL（红绿双态：污染前同夹具 PASS）")
    bridge, tdir = setup_scenario(6)
    replace.save_groups(bridge, [[1, 6]])
    ref_x = upload(TASKS[6], PNG_B, "b1b-g.png")
    hub_asset(TASKS[6], {"n": 1, "image_url": ref_x})
    hub_asset(TASKS[6], {"n": 2, "image_url": ref_x})
    prompt, _assets, mapping = render_now(bridge)
    check("⑥ 正例夹具 行→图号 == {1:1, 6:1, 2:2}", mapping == {1: 1, 6: 1, 2: 2}, str(mapping))
    confirm_and_selfcheck(bridge, tdir, prompt)
    by = items_by_name(selfcheck.run_selfcheck(tdir))
    check("⑥ 污染前同夹具 次序对账 PASS（防恒真）",
          by.get("次序对账", {}).get("结论") == "PASS", str(by.get("次序对账"))[:200])
    tj = tdir / "任务.json"
    task = json.loads(tj.read_text(encoding="utf-8-sig"))
    real = [dict(x) for x in task.get("assets_confirmed") or []]

    # (a) 快照追加幽灵条目 → 次序对账 FAIL 且证据含「≠ 发图单位」
    task["assets_confirmed"] = real + [{"no": 12, "img": "", "url": ref_x, "name": "", "desc": ""}]
    tj.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
    by_a = items_by_name(selfcheck.run_selfcheck(tdir))
    order_a = by_a.get("次序对账", {})
    check("⑥a 幽灵条目 → 次序对账 FAIL", order_a.get("结论") == "FAIL", str(order_a)[:220])
    check("⑥a 证据含「≠ 发图单位」", "≠ 发图单位" in str(order_a.get("证据")), str(order_a.get("证据"))[:220])
    rows_a = [x for x in (hub_api.get_task(TASKS[6]).get("自检") or []) if x.get("项") == "次序对账"]
    check("⑥a get_task 透出 FAIL（前端红条数据源不动）",
          len(rows_a) == 1 and rows_a[0].get("结论") == "FAIL", str(rows_a)[:200])

    # (b) 快照两条对调 → 次序对账 FAIL 且证据含「应为图1」
    task["assets_confirmed"] = list(reversed(real))
    tj.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
    by_b = items_by_name(selfcheck.run_selfcheck(tdir))
    order_b = by_b.get("次序对账", {})
    check("⑥b 快照对调 → 次序对账 FAIL", order_b.get("结论") == "FAIL", str(order_b)[:220])
    check("⑥b 证据含「应为图1」", "应为图1" in str(order_b.get("证据")), str(order_b.get("证据"))[:220])
    rows_b = [x for x in (hub_api.get_task(TASKS[6]).get("自检") or []) if x.get("项") == "次序对账"]
    check("⑥b get_task 透出 FAIL", len(rows_b) == 1 and rows_b[0].get("结论") == "FAIL",
          str(rows_b)[:200])


# ---------------------------------------------------------------- 入口

def main() -> int:
    real_root = Path(config.PROJECTS_DIR)
    real_before = probe_guard.projects_snapshot(real_root)
    iso_root = Path(tempfile.mkdtemp(prefix="wanprobe-at16b1b-",
                                     dir=os.environ.get("TMPDIR") or tempfile.gettempdir()))
    iso_projects = iso_root / "projects"
    iso_projects.mkdir(parents=True, exist_ok=True)
    stub = probe_guard.StubBridge()
    port = probe_guard.free_port()
    global BASE, OP
    BASE = f"http://127.0.0.1:{port}"
    OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 本机直连不走代理

    # 读写同源：先补丁 config 再懒加载其余模块（replace/selfcheck/hub_api 都以 config 为根）。
    config.PROJECTS_DIR = iso_projects
    global replace, selfcheck, hub_api
    from automation import replace, selfcheck  # noqa: E402
    import hub_api  # noqa: E402

    proc = None
    try:
        stub.thread.start()
        env = dict(os.environ, WANREPRO_AUTO_DIR=str(iso_projects), WANREPRO_BRIDGE=stub.base)
        proc = subprocess.Popen(
            [sys.executable, str(ROOT / "ui" / "server.py"), "--port", str(port), "--no-browser"],
            cwd=str(ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace")
        where = None
        for _ in range(200):
            if proc.poll() is not None:
                break
            code, payload = probe_guard.http_req(BASE, "/api/hub/where", timeout=5)
            if code == 200 and isinstance(payload, dict):
                where = payload
                break
            time.sleep(0.3)
        try:
            probe_guard.assert_same_root(where or {}, iso_projects)
            check("[0] 隔离实例起来且读写同源（/api/hub/where == 探针根）", where is not None,
                  f"{BASE} 无响应" if where is None else str(where)[:120])
            print(f"     数据根 {where.get('tasks_dir')}")
        except probe_guard.GuardError as err:
            check("[0] 隔离实例起来且读写同源（/api/hub/where == 探针根）", False, str(err))
            print("[probe-at16-b1b] 拒跑：数据根不同源，后续写入/清理一律不执行。")
            return summary()

        scenario_1()
        scenario_2()
        scenario_3()
        scenario_4()
        scenario_5()
        scenario_6()
        return summary()
    finally:
        if proc is not None:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
            else:
                out = (proc.stdout.read() or "")[-400:] if proc.stdout else ""
                if out.strip():
                    print(f"     [服务端尾日志] {out.strip()[-300:]}")
        with contextlib.suppress(Exception):
            stub.httpd.shutdown()
            stub.httpd.server_close()
        # 清理：只删自建（隔离根连根删 + 仓库 analysis 内的夹具桥目录）
        for tid in TASKS.values():
            probe_guard.cleanup_created(iso_projects, tid, before=frozenset(),
                                        created=frozenset(TASKS.values()))
        shutil.rmtree(iso_root, ignore_errors=True)
        for bridge in BRIDGES.values():
            shutil.rmtree(ROOT / "analysis" / "reverse" / bridge, ignore_errors=True)
        real_after = probe_guard.projects_snapshot(real_root)
        ok = real_after == real_before
        print(f"[probe-at16-b1b] 真实 projects 未动："
              f"before={sorted(real_before)} after={sorted(real_after)}" if ok else
              f"[probe-at16-b1b] 真实 projects 有变化："
              f"before={sorted(real_before)} after={sorted(real_after)}")
        if not ok:
            RESULTS.append(("真实 projects 零变化", False))


if __name__ == "__main__":
    raise SystemExit(main())
