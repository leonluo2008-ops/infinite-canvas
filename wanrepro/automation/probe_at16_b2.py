#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AT-16 B2 探针：自检人话化（生产者侧同源）+ 历史兼容 + 读层透传 + 提示词区布局（端到端）。

跑法：wanrepro/ 下 `python -m automation.probe_at16_b2`（判 returncode，禁子串判通过）。
覆盖：
  [0] 隔离实例：tempfile 根 + probe_guard StubBridge + /api/hub/where 同源拒跑；真实 projects 零变化。
  [1] 生产者话术：真跑 selfcheck.run_selfcheck（真确认/真渲染）→ 每条都有「说明」；异常条目的
      结论人话/原因/处理齐备，行号/图号来自检查器自己的发现（锁点缺口点出 #12）。
  [2] 反编造：处理/原因里每个「…」具名必须对得上——要么是详情页真实文案（task-detail-drawer.tsx
      里出现），要么是清单列名（待确认清单.md 表头）；引用的清单路径必须真实存在。
  [3] 读层透传：GET /api/hub/task 把「说明」原样带出（新结构）；旧结构（自检.json 无说明）不伪造。
  [4] 拆解门禁条目：读路径汇总的门禁条目同样有「说明」+ 处理指向「重试」。
  [5] 历史兼容：真实 auto-0042 自检.json **只读**（跑前跑后 sha256 一致、无说明、证据可读）。
  [6] 前端联动：node scripts/probe-at16-b2.mjs --selfcheck <真产出> --legacy <auto-0042> 全绿。
"""
from __future__ import annotations

import contextlib
import hashlib
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
TASK_NEW = "auto-9711"
TASK_LEGACY = "auto-9712"
BRIDGE = "probe-at16b2"
REAL_TASK = "auto-0042"                              # 真实历史任务（只读）
WEB = ROOT.parent / "web"
DRAWER = WEB / "src" / "pages" / "hub" / "task-detail-drawer.tsx"

PNG_A = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c626001000000ffff03000006000557bfabd4"
    "0000000049454e44ae426082")

CK_B2 = """# 待确认清单 · {bridge}

| # | 类别 | 模型看到什么 | 命名 | 资产图 | 锁什么 | 建议 | 用我的描述 |
|---|------|-------------|------|--------|--------|------|------------|
| 1 | 主体 | 细长圆柱体紫色表面，顶部为银色金属圆柱盖，管体侧面印有粉白字母排版 |  |  | 外形比例 | 必须换 |  |
| 2 | 人物 | A：20-25岁女性，棕色长波浪卷发，画面位置：九宫格5·镜1 |  |  | 发型与轮廓 | 建议准备 |  |
| 6 | 道具 | A：紫色圆柱管身配亮银色金属盖的香水喷雾小物体 |  |  | 颜色、支点位置 | 建议准备 |  |
| 12 | 品牌视觉（置换位） | 参考片品牌位 标语品牌字（出现于：00:05-00:07）：「POP!」｜红色无衬线粗体大字｜正红 | 品牌文字 |  | 客户品牌字样（待填） | 必须换 |  |
"""

GATE_FAIL = {
    "状态": "标红（缺项 2）",
    "轮次": 2,
    "上限": 2,
    "结果": {
        "verdict": "FAIL",
        "checks": [{"id": "D1", "verdict": "PASS"}, {"id": "D2", "verdict": "FAIL"}],
        "缺项": ["D2 分镜表缺片尾覆盖", "D5 每行缺画面位置"],
        "告警": [],
    },
}


def check(name: str, cond: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))
    return bool(cond)


def summary() -> int:
    fails = [n for n, ok in RESULTS if not ok]
    print(f"\n[probe-at16-b2] {len(RESULTS) - len(fails)}/{len(RESULTS)} PASS")
    for n in fails:
        print(f"  FAIL {n}")
    return 1 if fails else 0


# ---------------------------------------------------------------- 探针进程内助手

def probe_make_task(local_id: str, bridge_id: str, state: str = "awaiting_assets") -> Path:
    tdir = Path(config.PROJECTS_DIR) / local_id
    shutil.rmtree(tdir, ignore_errors=True)
    (tdir / "inbox").mkdir(parents=True, exist_ok=True)
    task = {
        "local_id": local_id, "bridge_id": bridge_id,
        "created_at": "2026-01-01T00:00:00+08:00", "state": state,
        "history": [{"at": "2026-01-01T00:00:00+08:00", "from": None, "to": state,
                     "note": "probe-at16b2 夹具"}],
        "intent": "AT-16 B2 探针夹具", "assets": [],
        "ingestion": {"zip_sha256": f"probe-{local_id}", "zip_name": "probe",
                      "video_name": "", "text_name": "", "ignored": []},
    }
    (tdir / "任务.json").write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
    return tdir


def setup_bridge() -> None:
    bdir = ROOT / "analysis" / "reverse" / BRIDGE
    shutil.rmtree(bdir, ignore_errors=True)
    bdir.mkdir(parents=True, exist_ok=True)
    (bdir / "待确认清单.md").write_text(CK_B2.format(bridge=BRIDGE), encoding="utf-8")
    shutil.copyfile(ROOT / "tests" / "fixtures" / "at11-m1" / "拆解.md", bdir / "拆解.md")
    (bdir / "门禁.json").write_text(json.dumps(GATE_FAIL, ensure_ascii=False, indent=2), encoding="utf-8")


def render_now() -> str:
    """直调渲染器（不发网络不调模型）产出提示词 md。"""
    render = replace.render_module()
    disc = (ROOT / "analysis" / "reverse" / BRIDGE / "拆解.md").read_text(encoding="utf-8-sig")
    layers, _order = render.parse_layers(disc)
    fm, body = render.split_frontmatter((ROOT / "templates" / "seedance-flow.md").read_text(encoding="utf-8-sig"))
    secs, _opt = render.parse_sections(body)
    ents = render.parse_checklist(replace.checklist_path(BRIDGE))
    render.effective_units(ents, replace.load_groups(BRIDGE), replace.load_row_decisions(BRIDGE))
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        prompt = render.render_prompt(fm, secs, layers, ents, {"时长": "7", "画幅": "9:16"},
                                      BRIDGE, "seedance-flow")
    return prompt


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


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def say_of(item: dict) -> dict:
    say = item.get("说明")
    return say if isinstance(say, dict) else {}


def quoted(text: str) -> list[str]:
    return re.findall(r"「([^」]+)」", text or "")


# ---------------------------------------------------------------- 场景

def scenario_producer() -> Path:
    """①/②/④：真确认 + 真渲染 + 真自检（污染提交快照/漏锁点 → 两条 FAIL），人话与反编造断言。"""
    print("① 生产者话术：真跑自检（异常条目必须有结论/原因/处理，数字来自检查器）")
    tdir = probe_make_task(TASK_NEW, BRIDGE)
    ref = upload(TASK_NEW, PNG_A, "b2-row1.png")
    code, _body = hub_asset(TASK_NEW, {"n": 1, "image_url": ref})
    check("① HTTP 贴图 #1 → 200（真实写路径）", code == 200, str(code))
    prompt = render_now()
    (tdir / "提示词.md").write_text(prompt, encoding="utf-8")
    replace.run_replace(tdir, assets=None, map_spec=None, name_spec=None, confirm=True, verbose=False)

    # 污染：提交快照多一条不属于任何行的图（模拟组内多提交/错位）；第 12 行必换却没图也没按原样
    task = json.loads((tdir / "任务.json").read_text(encoding="utf-8-sig"))
    task["assets_confirmed"] = list(task.get("assets_confirmed") or []) + [
        {"no": 6, "url": "https://example.invalid/not-in-checklist.png"}]
    (tdir / "任务.json").write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")

    items = selfcheck.run_selfcheck(tdir)
    by = {item["项"]: item for item in items}
    check("① 自检落盘四条检查项", set(by) == {"次序对账", "绑图行残留", "锁点对账", "组绑定一致性"},
          str(sorted(by)))
    check("① 每条检查项都带「说明」（生产者侧同源，前端不用猜）",
          all(say_of(item) for item in items), str([item["项"] for item in items]))
    check("① 每条都有「结论人话」", all(say_of(item).get("结论人话") for item in items),
          str({k: say_of(v).get("结论人话", "")[:24] for k, v in by.items()}))

    order = by["次序对账"]
    check("① 次序对账按污染判 FAIL（自检独立性不破）", order["结论"] == "FAIL", order["证据"][:160])
    order_say = say_of(order)
    check("① 次序对账人话：结论说清「几处/什么毛病」",
          "没对上" in order_say.get("结论人话", "") and "提交的图数" in order_say.get("结论人话", ""),
          order_say.get("结论人话", ""))
    check("① 次序对账原因含检查器发现的动态数字（提交 3 条 vs 发图单位 2 个）",
          "提交快照" in order_say.get("原因", "") and "发图单位" in order_say.get("原因", ""),
          order_say.get("原因", "")[:200])
    check("① 次序对账处理指向真实可执行入口（上传区重传/移除）",
          "重传（覆盖）" in order_say.get("处理", "") and "移除" in order_say.get("处理", ""),
          order_say.get("处理", "")[:200])
    check("① 次序对账「发现」结构化（逐条带检查器原文，不靠前端抠证据）",
          bool(order_say.get("发现")) and any("发图单位" in str(f.get("文字")) for f in order_say["发现"]),
          json.dumps(order_say.get("发现"), ensure_ascii=False)[:240])

    lock = by["锁点对账"]
    lock_say = say_of(lock)
    check("① 锁点对账判 FAIL（#12 必换没图也没按原样）", lock["结论"] == "FAIL", lock["证据"][:160])
    check("① 锁点人话点出 #12（动态行号来自检查器）",
          "#12" in lock_say.get("结论人话", "") and any(f.get("行号") == 12 for f in lock_say.get("发现", [])),
          json.dumps(lock_say, ensure_ascii=False)[:300])
    check("① 锁点处理指向「知道，按原样」真实勾选入口",
          "知道，按原样" in lock_say.get("处理", ""), lock_say.get("处理", "")[:160])

    residue = by["绑图行残留"]
    check("① PASS 条目的处理是「无需处理」（不硬编指引）",
          residue["结论"] != "PASS" or say_of(residue).get("处理") == "无需处理",
          f"{residue['结论']} / {say_of(residue).get('处理', '')[:60]}")

    print("② 反编造：处理/原因里的「…」具名必须对得上真入口/真列名")
    drawer_src = DRAWER.read_text(encoding="utf-8")
    ck_header = (ROOT / "analysis" / "reverse" / BRIDGE / "待确认清单.md").read_text(encoding="utf-8-sig").splitlines()[2]
    columns = [cell.strip() for cell in ck_header.strip("|").split("|")]
    bad: list[str] = []
    labels: set[str] = set()
    for name, item in by.items():
        say = say_of(item)
        for field in ("处理", "原因", "结论人话"):
            for label in quoted(say.get(field, "")):
                labels.add(label)
                if label not in drawer_src and label not in columns:
                    bad.append(f"{name}.{field}「{label}」")
    check("② 全部指引具名都能对到详情页文案或清单列名（零编造）", not bad, "、".join(bad[:6]))
    check("② 具名集合非空（真的查到了入口，不是空跑）", len(labels) >= 4, "、".join(sorted(labels)))

    gate = next((row for row in (selfcheck.load_selfcheck(tdir) or []) if row.get("项") == selfcheck.GATE_ITEM_NAME), {})
    gate_say = say_of(gate)
    check("③ 门禁条目（读路径汇总）也有「说明」+ 处理指向「重试」",
          bool(gate_say.get("结论人话")) and "重试" in gate_say.get("处理", ""),
          json.dumps(gate_say, ensure_ascii=False)[:240])
    check("③ 门禁证据格式不变（首行仍是「状态 …｜绿项 …」摘要）",
          gate.get("证据", "").startswith("状态 ") and "绿项" in gate.get("证据", "").splitlines()[0],
          str(gate.get("证据", "")[:80]))

    print("③ 读层透传：GET /api/hub/task 原样带出「说明」")
    code, payload = probe_guard.http_req(BASE, f"/api/hub/task/{TASK_NEW}", timeout=30)
    if code != 200:
        import traceback
        try:
            hub_api.get_task(TASK_NEW)
        except Exception:
            traceback.print_exc()
    task_payload = (payload or {}).get("task") if isinstance(payload, dict) else None
    rows = (task_payload or {}).get("自检") if isinstance(task_payload, dict) else None
    api_order = next((row for row in (rows or []) if row.get("项") == "次序对账"), {})
    check("③ bridge get_task 200 且带自检束", code == 200 and bool(rows), f"{code} {str(payload)[:120]}")
    check("③ get_task 透出的「说明.处理」与生产者原文一致",
          say_of(api_order).get("处理") == order_say.get("处理"),
          json.dumps(say_of(api_order).get("处理", ""), ensure_ascii=False)[:160])
    api_lock = next((row for row in (rows or []) if row.get("项") == "锁点对账"), {})
    check("③ get_task 透出的「发现」原样带行号（锁点 #12）",
          any(f.get("行号") == 12 for f in say_of(api_lock).get("发现", [])),
          json.dumps(say_of(api_lock).get("发现"), ensure_ascii=False)[:200])

    # 旧结构：自检.json 手工去掉说明（模拟历史文件）→ 读层不伪造
    legacy_dir = probe_make_task(TASK_LEGACY, BRIDGE, state="awaiting_approval")
    old_items = [{"项": row["项"], "结论": row["结论"], "证据": row["证据"]} for row in items]
    (legacy_dir / "自检.json").write_text(json.dumps(old_items, ensure_ascii=False, indent=2), encoding="utf-8")
    code, payload = probe_guard.http_req(BASE, f"/api/hub/task/{TASK_LEGACY}", timeout=30)
    task_payload = (payload or {}).get("task") if isinstance(payload, dict) else None
    rows = (task_payload or {}).get("自检") if isinstance(task_payload, dict) else []
    file_rows = [row for row in rows if row.get("项") != "拆解门禁"]
    check("③ 旧结构（无说明）经读层照旧透出，不伪造说明",
          code == 200 and file_rows and all("说明" not in row for row in file_rows),
          json.dumps([(r.get("项"), "说明" in r) for r in file_rows], ensure_ascii=False))
    check("③ 旧结构证据原文完整保留",
          all(row.get("证据") for row in file_rows), str([r.get("证据", "")[:20] for r in file_rows]))

    out = Path(os.environ.get("TMPDIR") or tempfile.gettempdir()) / "probe-at16-b2-selfcheck.json"
    out.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def scenario_legacy_real() -> Path:
    """④ 真实 auto-0042 自检.json 只读：sha256 不变、无说明、证据可读。"""
    print("④ 历史兼容：真实 auto-0042 只读")
    real_dir = Path(config.PROJECTS_DIR) / REAL_TASK
    if not real_dir.is_dir():
        # 探针把 config 指到隔离根了，真实路径按仓库默认位置单独拼
        real_dir = ROOT / "automation" / "projects" / REAL_TASK
    sc = real_dir / selfcheck.SELFCHECK_NAME
    if not sc.is_file():
        check("④ 真实任务 auto-0042 自检.json 存在（只读验证对象）", False, str(sc))
        return sc
    before = sha256(sc)
    raw = json.loads(sc.read_text(encoding="utf-8-sig"))
    items = selfcheck.load_selfcheck(real_dir) or []
    rows = [row for row in items if row.get("项") != selfcheck.GATE_ITEM_NAME]
    after = sha256(sc)
    check("④ auto-0042 自检.json 跑前跑后 sha256 一致（只读增强不改历史文件）",
          before == after, f"{before[:12]} / {after[:12]}")
    check("④ 历史条目没有「说明」（不伪造处理指引）", all("说明" not in row for row in rows),
          str([(r.get("项"), "说明" in r) for r in rows]))
    check("④ 历史条目证据原文非空（降级显示可读）",
          all(str(row.get("证据") or "").strip() for row in rows),
          str([len(str(r.get("证据") or "")) for r in rows]))
    check("④ 历史条目里确有异常（降级显示场景真实存在）",
          any(row.get("结论") != "PASS" for row in rows),
          str([(r.get("项"), r.get("结论")) for r in rows]))
    return sc


def scenario_frontend(selfcheck_json: Path, legacy_json: Path) -> None:
    print("⑤ 前端联动：node probe-at16-b2（真产出 + 真历史文件）")
    cmd = ["node", "scripts/probe-at16-b2.mjs", "--selfcheck", str(selfcheck_json), "--legacy", str(legacy_json)]
    proc = subprocess.run(cmd, cwd=str(WEB), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=900)
    tail = "\n".join([line for line in (proc.stdout or "").splitlines() if "FAIL" in line or "PASS," in line])
    check("⑤ 前端探针全绿（布局 + 透传 + 历史降级 + 真产出渲染）", proc.returncode == 0, tail[-600:] or (proc.stderr or "")[-400:])
    for line in (proc.stdout or "").splitlines():
        if line.strip().startswith("[PASS]") and ("[D]" in line or "probe-at16-b2" in line):
            print(f"     {line.strip()}")


# ---------------------------------------------------------------- 入口

def main() -> int:
    real_root = ROOT / "automation" / "projects"
    real_before = probe_guard.projects_snapshot(real_root)
    iso_root = Path(tempfile.mkdtemp(prefix="wanprobe-at16b2-", dir=os.environ.get("TMPDIR") or tempfile.gettempdir()))
    iso_projects = iso_root / "projects"
    iso_projects.mkdir(parents=True, exist_ok=True)
    stub = probe_guard.StubBridge()
    port = probe_guard.free_port()
    global BASE, OP
    BASE = f"http://127.0.0.1:{port}"
    OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))

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
            print("[probe-at16-b2] 拒跑：数据根不同源，后续写入/清理一律不执行。")
            return summary()

        setup_bridge()
        out_json = scenario_producer()
        legacy_json = scenario_legacy_real()
        scenario_frontend(out_json, legacy_json)
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
        for tid in (TASK_NEW, TASK_LEGACY):
            probe_guard.cleanup_created(iso_projects, tid, before=frozenset(),
                                        created=frozenset((TASK_NEW, TASK_LEGACY)))
        shutil.rmtree(iso_root, ignore_errors=True)
        shutil.rmtree(ROOT / "analysis" / "reverse" / BRIDGE, ignore_errors=True)
        real_after = probe_guard.projects_snapshot(real_root)
        ok = real_after == real_before
        print(f"[probe-at16-b2] 真实 projects 未动：before={len(real_before)} after={len(real_after)}"
              if ok else f"[probe-at16-b2] 真实 projects 有变化：before={sorted(real_before)} after={sorted(real_after)}")
        if not ok:
            RESULTS.append(("真实 projects 零变化", False))


if __name__ == "__main__":
    raise SystemExit(main())
