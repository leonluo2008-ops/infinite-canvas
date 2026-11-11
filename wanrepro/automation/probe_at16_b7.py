#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AT-16 B7 后端探针：品牌视觉行可并入确认组（前后端双守卫移除）+ 无图不发号 + 五点同源。

跑法：wanprobe/ 下 `python -m automation.probe_at16_b7`（判 returncode，禁子串判通过）。
背景（缺陷 D8，所有者定）：品牌资产不得被禁止与产品合并——片尾可能无品牌露出、品牌标签
可能就是产品本身。B7 移除两处品牌专用拒绝（前端 merge-mode.mergeSelectable、后端
hub_api.group_rows），保留通用守卫（已在组/按原片/已移除/行不存在/≥2 行）。无图成员合同
（D2）不动：品牌无图可入组但不凭空发号（图号 0），绑定组图后再验五点。

隔离体系（probe_at16_b1b 同一套）：
  · probe_guard.projects_snapshot 跑前/跑后对账（真实 projects 零变化）；
  · tempfile.mkdtemp(prefix="wanprobe-at16b7-") + iso/projects 作 WANREPRO_AUTO_DIR；
  · probe_guard.StubBridge() + probe_guard.free_port() 起 ui/server.py 隔离实例；
  · GET /api/hub/where 断言 tasks_dir == 探针根，不同源拒跑；
  · 夹具桥目录 analysis/reverse/probe-at16b7-s<N> 跑完删；真实任务/桥只读。

场景：
  ① 品牌+产品（均先无图）成组 → 组写一张图 → 两行同号一个提交单位（rows=[1,12]）、五点一致
  ② 品牌无图入组成功（图号 0 不凭空发号）→ 绑组图后两行同号、五点一致
  ③ 通用守卫零削弱：已在组先拆、已移除先恢复、行不存在、<2 行
  ④ 组外同 URL 独立发号；解组后未合并品牌行零回归（不在组、有图独立占号）
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

TASKS = {n: f"auto-97{n:02d}" for n in range(1, 5)}
BRIDGES = {n: f"probe-at16b7-s{n}" for n in range(1, 5)}
DISC_FIXTURE = ROOT / "tests" / "fixtures" / "at11-m1" / "拆解.md"

# 夹具清单（与 B1b 同一条 POP 片：行 1 主体 / 2 人物 / 3 场景 / 12 品牌视觉（置换位））
CK_B7 = """# 待确认清单 · {bridge}

| # | 类别 | 模型看到什么 | 命名 | 资产图 | 锁什么 | 建议 | 用我的描述 |
|---|------|-------------|------|--------|--------|------|------------|
| 1 | 主体 | 细长圆柱体紫色表面，顶部为银色金属圆柱盖，管体侧面印有粉白字母排版 |  |  | 外形比例 | 建议准备 |  |
| 2 | 人物 | A：20-25岁女性，棕色长波浪卷发，画面位置：九宫格5·镜1 |  |  | 发型与轮廓 | 建议准备 |  |
| 3 | 场景 | 日光晴天海滨，白色拍岸海浪 |  |  | 光影方向 | 建议准备 |  |
| 12 | 品牌视觉（置换位） | 参考片品牌位 标语品牌字（出现于：00:05-00:07）：「POP!」｜红色无衬线粗体大字｜正红 | 品牌文字 |  | 客户品牌字样（待填） | 必须换 |  |
"""

BASE = ""
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
replace = None
selfcheck = None
hub_api = None


def check(name: str, cond: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" —— {detail}" if detail and not cond else ""))
    return bool(cond)


def summary() -> int:
    fails = [n for n, ok in RESULTS if not ok]
    print(f"\n[probe-at16-b7] {len(RESULTS) - len(fails)}/{len(RESULTS)} PASS")
    for n in fails:
        print(f"  FAIL {n}")
    return 1 if fails else 0


# ---------------------------------------------------------------- 夹具
def probe_make_task(local_id: str, bridge_id: str) -> Path:
    tdir = Path(config.PROJECTS_DIR) / local_id
    shutil.rmtree(tdir, ignore_errors=True)
    (tdir / "inbox").mkdir(parents=True, exist_ok=True)
    task = {
        "local_id": local_id, "bridge_id": bridge_id,
        "created_at": "2026-01-01T00:00:00+08:00", "state": "awaiting_assets",
        "history": [{"at": "2026-01-01T00:00:00+08:00", "from": None, "to": "awaiting_assets",
                     "note": "probe-at16b7 夹具"}],
        "intent": "AT-16 B7 探针夹具", "assets": [],
        "ingestion": {"zip_sha256": f"probe-{local_id}", "zip_name": "probe",
                      "video_name": "", "text_name": "", "ignored": []},
    }
    (tdir / "任务.json").write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
    return tdir


def setup_scenario(n: int) -> tuple[str, Path]:
    bridge = BRIDGES[n]
    bdir = ROOT / "analysis" / "reverse" / bridge
    shutil.rmtree(bdir, ignore_errors=True)
    bdir.mkdir(parents=True, exist_ok=True)
    (bdir / "待确认清单.md").write_text(CK_B7.format(bridge=bridge), encoding="utf-8")
    shutil.copyfile(DISC_FIXTURE, bdir / "拆解.md")
    return bridge, probe_make_task(TASKS[n], bridge)


def ck_rows(bridge: str) -> dict[int, dict]:
    path = replace.checklist_path(bridge)
    return {r["no"]: r for r in replace.parse_rows(path.read_text(encoding="utf-8-sig"))}


# ---------------------------------------------------------------- 渲染 / 五点
def render_now(bridge: str) -> tuple[str, str, dict[int, int], dict[int, int]]:
    """直调渲染器（不发网络不调模型）→（提示词, 资产清单, 行→图号（有号行）, 行→图号（含 0））。"""
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
    all_no = {int(e.row_no): int(e.no) for e in ents}
    return prompt, assets_md, mapping, all_no


def units_of(bridge: str) -> list[dict]:
    render = replace.render_module()
    ents = render.parse_checklist(replace.checklist_path(bridge))
    return render.effective_units(ents, replace.load_groups(bridge),
                                  replace.load_row_decisions(bridge))


def five_points(bridge: str, tdir: Path, prompt: str, assets_md: str) -> dict:
    """五点同源：图号集合 == 正文集合 == 资产清单集合 == 引用总数 == 提交条数；提交第 i 条行→图号=i。"""
    units = units_of(bridge)
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


def confirm_and_selfcheck(tdir: Path, prompt: str) -> list[dict]:
    replace.run_replace(tdir, assets=None, map_spec=None, name_spec=None,
                        confirm=True, verbose=False)
    (tdir / "提示词.md").write_text(prompt, encoding="utf-8")
    return selfcheck.run_selfcheck(tdir)


def items_by_name(items: list[dict]) -> dict[str, dict]:
    return {x.get("项"): x for x in items}


# ---------------------------------------------------------------- HTTP 真实写路径
def upload(tid: str, blob: bytes, name: str) -> str:
    req = urllib.request.Request(
        BASE + "/api/asset/upload", data=blob, method="POST",
        headers={"Content-Type": "application/octet-stream",
                 "X-Filename": urllib.parse.quote(name, safe=""),
                 "X-Task-Id": urllib.parse.quote(tid, safe="")})
    resp = json.loads(OP.open(req, timeout=30).read())
    return str(resp.get("local") or "")


def hub_asset(tid: str, payload: dict) -> tuple[int, dict | bytes]:
    return probe_guard.http_req(
        BASE, "/api/hub/asset", "POST",
        json.dumps({"task_id": tid, **payload}, ensure_ascii=False).encode("utf-8"),
        "application/json", timeout=30)


def hub_group(tid: str, rows: list[int]) -> tuple[int, dict | bytes]:
    return probe_guard.http_req(
        BASE, f"/api/hub/task/{urllib.parse.quote(tid)}/group", "POST",
        json.dumps({"rows": rows}, ensure_ascii=False).encode("utf-8"),
        "application/json", timeout=30)


def hub_ungroup(tid: str, rows: list[int]) -> tuple[int, dict | bytes]:
    return probe_guard.http_req(
        BASE, f"/api/hub/task/{urllib.parse.quote(tid)}/ungroup", "POST",
        json.dumps({"rows": rows}, ensure_ascii=False).encode("utf-8"),
        "application/json", timeout=30)


def err_of(body) -> str:
    if isinstance(body, dict):
        return str(body.get("error") or body)
    return str(body)[:200]


# ---------------------------------------------------------------- 场景
def scenario_1() -> None:
    print("① 品牌+产品成组（先无图）→ 组写一张图 → 同图两行同号一个提交单位 + 五点一致")
    bridge, tdir = setup_scenario(1)
    code, body = hub_group(TASKS[1], [1, 12])
    check("① HTTP 成组 [1,12]（品牌视觉行 + 主体）→ 200（无品牌专用拒绝）", code == 200,
          f"{code} {err_of(body)}")
    check("① 组落 sidecar『同实体确认』 == [[1,12]]", ck_rows(bridge) and
          replace.load_groups(bridge) == [[1, 12]], str(replace.load_groups(bridge)))
    _prompt, _assets, mapping, _all = render_now(bridge)
    check("① 零图组 → 不发号（mapping 空，品牌不凭空占号）", mapping == {}, str(mapping))
    ref = upload(TASKS[1], PNG_A, "b7-brand.png")
    code, body = hub_asset(TASKS[1], {"n": 1, "image_url": ref})
    check("① HTTP 组写 #1 贴图 → 200（组联动）", code == 200, f"{code} {err_of(body)}")
    rows = ck_rows(bridge)
    check("① 组写联动到品牌行（#1/#12 同 URL）",
          rows[1].get("cur_assets") == ref and rows[12].get("cur_assets") == ref,
          json.dumps({k: rows[k].get("cur_assets") for k in (1, 12)}, ensure_ascii=False))
    prompt, assets_md, mapping, _all = render_now(bridge)
    units = units_of(bridge)
    check("① 同图两行同号（{1:1, 12:1}）", mapping == {1: 1, 12: 1}, str(mapping))
    check("① 一个提交单位（units 恰 1 个且 rows == [1,12]）",
          len(units) == 1 and units[0]["rows"] == [1, 12],
          json.dumps([{k: u[k] for k in ("no", "rows")} for u in units], ensure_ascii=False))
    order = items_by_name(confirm_and_selfcheck(tdir, prompt)).get("次序对账", {})
    check("① 自检次序对账 PASS", order.get("结论") == "PASS", str(order)[:220])
    snap = confirmed_of(tdir)
    check("① 确认快照恰 1 条（图号 1 = 组内两行共用的那一张图）",
          len(snap) == 1 and snap[0].get("no") == 1 and snap[0].get("url") == ref,
          json.dumps(snap, ensure_ascii=False)[:240])
    five_points(bridge, tdir, prompt, assets_md)


def scenario_2() -> None:
    print("② 品牌无图入组成功（图号 0）→ 绑组图后两行同号、五点一致")
    bridge, tdir = setup_scenario(2)
    ref = upload(TASKS[2], PNG_A, "b7-r1.png")
    code, body = hub_asset(TASKS[2], {"n": 1, "image_url": ref})
    check("② HTTP #1 贴图 → 200", code == 200, f"{code} {err_of(body)}")
    code, body = hub_group(TASKS[2], [1, 12])
    check("② HTTP 成组 [1,12]（品牌行无图也成功）→ 200", code == 200, f"{code} {err_of(body)}")
    _prompt, _assets, mapping, all_no = render_now(bridge)
    check("② 品牌无图 → 图号 0（不凭空发号）", all_no.get(12) == 0, str(all_no))
    check("② 同组只有持图行占号（{1:1}）", mapping == {1: 1}, str(mapping))
    units = units_of(bridge)
    check("② 发号单位只含持图行（units rows == [1]）",
          len(units) == 1 and units[0]["rows"] == [1],
          json.dumps([{k: u[k] for k in ("no", "rows")} for u in units], ensure_ascii=False))
    ref2 = upload(TASKS[2], PNG_B, "b7-group.png")
    code, body = hub_asset(TASKS[2], {"n": 12, "image_url": ref2})
    check("② HTTP 组写（从品牌行贴 #12）→ 200（组联动两行）", code == 200, f"{code} {err_of(body)}")
    rows = ck_rows(bridge)
    check("② 绑组图后 #1/#12 同 URL",
          rows[1].get("cur_assets") == ref2 and rows[12].get("cur_assets") == ref2,
          json.dumps({k: rows[k].get("cur_assets") for k in (1, 12)}, ensure_ascii=False))
    prompt, assets_md, mapping, _all = render_now(bridge)
    units = units_of(bridge)
    check("② 绑组图后两行同号（{1:1, 12:1}）", mapping == {1: 1, 12: 1}, str(mapping))
    check("② 绑组图后一个提交单位 rows == [1,12]",
          len(units) == 1 and units[0]["rows"] == [1, 12],
          json.dumps([{k: u[k] for k in ("no", "rows")} for u in units], ensure_ascii=False))
    order = items_by_name(confirm_and_selfcheck(tdir, prompt)).get("次序对账", {})
    check("② 自检次序对账 PASS", order.get("结论") == "PASS", str(order)[:220])
    five_points(bridge, tdir, prompt, assets_md)


def scenario_3() -> None:
    print("③ 通用守卫零削弱：已在组先拆、已移除先恢复、行不存在、<2 行")
    _bridge, _tdir = setup_scenario(3)
    code, body = hub_group(TASKS[3], [1, 12])
    check("③ 基线成组 [1,12] → 200", code == 200, f"{code} {err_of(body)}")
    code, body = hub_group(TASKS[3], [12, 2])
    check("③ 拒绝：已组行再入组（先拆开）→ 409 + 已在组",
          code == 409 and "已在组" in err_of(body), f"{code} {err_of(body)}")
    code, body = hub_ungroup(TASKS[3], [1, 12])
    check("③ 解组 [1,12] → 200", code == 200, f"{code} {err_of(body)}")
    code, body = hub_asset(TASKS[3], {"n": 12, "decision": "dismissed"})
    check("③ HTTP dismiss #12（品牌行按原片）→ 200", code == 200, f"{code} {err_of(body)}")
    code, body = hub_group(TASKS[3], [1, 12])
    check("③ 拒绝：已移除行入组（先恢复再合并）→ 409 + dismissed",
          code == 409 and "先恢复再合并" in err_of(body) and "dismissed" in err_of(body),
          f"{code} {err_of(body)}")
    code, body = hub_asset(TASKS[3], {"n": 12, "decision": "restore"})
    check("③ HTTP restore #12 → 200", code == 200, f"{code} {err_of(body)}")
    code, body = hub_group(TASKS[3], [1, 12])
    check("③ 恢复后成组成功（[1,12]）→ 200", code == 200 and replace.load_groups(BRIDGES[3]) == [[1, 12]],
          f"{code} {err_of(body)}")
    hub_ungroup(TASKS[3], [1, 12])
    code, body = hub_group(TASKS[3], [1, 99])
    check("③ 拒绝：行号不在清单 → 409 + 行号不在清单里",
          code == 409 and "行号不在清单里" in err_of(body), f"{code} {err_of(body)}")
    code, body = hub_group(TASKS[3], [12])
    check("③ 拒绝：成组 < 2 行 → 409 + 至少要 2 行",
          code == 409 and "至少要 2 行" in err_of(body), f"{code} {err_of(body)}")


def scenario_4() -> None:
    print("④ 组外同 URL 独立发号；解组后未合并品牌行零回归（不在组、有图独立占号）")
    bridge, tdir = setup_scenario(4)
    ref = upload(TASKS[4], PNG_A, "b7-x.png")
    code, body = hub_group(TASKS[4], [1, 12])
    check("④ HTTP 成组 [1,12] → 200", code == 200, f"{code} {err_of(body)}")
    code, body = hub_asset(TASKS[4], {"n": 1, "image_url": ref})
    check("④ HTTP 组写 #1 → 200（#1/#12 同 URL）", code == 200, f"{code} {err_of(body)}")
    code, body = hub_asset(TASKS[4], {"n": 2, "image_url": ref})
    check("④ HTTP 组外同行贴同一 URL → 200", code == 200, f"{code} {err_of(body)}")
    prompt, assets_md, mapping, _all = render_now(bridge)
    check("④ 组外同 URL 独立发号（{1:1, 12:1, 2:2}）", mapping == {1: 1, 12: 1, 2: 2}, str(mapping))
    code, body = hub_ungroup(TASKS[4], [1, 12])
    check("④ 解组 [1,12] → 200", code == 200, f"{code} {err_of(body)}")
    prompt, assets_md, mapping, _all = render_now(bridge)
    check("④ 解组后未合并品牌行零回归：不在任何组、有图独立占号（{1:1, 2:2, 12:3}）",
          replace.load_groups(bridge) == [] and mapping == {1: 1, 2: 2, 12: 3}, str(mapping))
    check("④ 解组保留当前传图（品牌行仍持组写归一图）",
          ck_rows(bridge)[12].get("cur_assets") == ref, repr(ck_rows(bridge)[12].get("cur_assets")))
    order = items_by_name(confirm_and_selfcheck(tdir, prompt)).get("次序对账", {})
    check("④ 自检次序对账 PASS", order.get("结论") == "PASS", str(order)[:220])
    five_points(bridge, tdir, prompt, assets_md)


# ---------------------------------------------------------------- 入口
def main() -> int:
    real_root = Path(config.PROJECTS_DIR)
    real_before = probe_guard.projects_snapshot(real_root)
    iso_root = Path(tempfile.mkdtemp(prefix="wanprobe-at16b7-",
                                     dir=os.environ.get("TMPDIR") or tempfile.gettempdir()))
    iso_projects = iso_root / "projects"
    iso_projects.mkdir(parents=True, exist_ok=True)
    stub = probe_guard.StubBridge()
    port = probe_guard.free_port()
    global BASE, replace, selfcheck, hub_api
    BASE = f"http://127.0.0.1:{port}"

    config.PROJECTS_DIR = iso_projects
    from automation import replace as _replace, selfcheck as _selfcheck  # noqa: E402
    import hub_api as _hub_api  # noqa: E402
    replace, selfcheck, hub_api = _replace, _selfcheck, _hub_api

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
            print("[probe-at16-b7] 拒跑：数据根不同源，后续写入/清理一律不执行。")
            return summary()

        scenario_1()
        scenario_2()
        scenario_3()
        scenario_4()
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
        for tid in TASKS.values():
            probe_guard.cleanup_created(iso_projects, tid, before=frozenset(),
                                        created=frozenset(TASKS.values()))
        shutil.rmtree(iso_root, ignore_errors=True)
        for bridge in BRIDGES.values():
            shutil.rmtree(ROOT / "analysis" / "reverse" / bridge, ignore_errors=True)
        real_after = probe_guard.projects_snapshot(real_root)
        ok = real_after == real_before
        print(f"[probe-at16-b7] 真实 projects 未动：before={sorted(real_before)} "
              f"after={sorted(real_after)}" if ok else
              f"[probe-at16-b7] 真实 projects 有变化：before={sorted(real_before)} "
              f"after={sorted(real_after)}")
        if not ok:
            RESULTS.append(("真实 projects 零变化", False))


if __name__ == "__main__":
    raise SystemExit(main())
