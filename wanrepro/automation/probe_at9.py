#!/usr/bin/env python
"""AT-9 B5 探针：自检三项正/反例 + 疑似不联动 + done 删除 + retry 语义 + 确认后渲染续跑。

跑法：wanrepro/ 下 `python -m automation.probe_at9 [port]`（默认 8776 常驻工作台；
要求桥已重启到本批代码——retry 路由/自检钩子/done 删除都在新代码里）。
判据全对才 exit 0，任何一步失败如实 FAIL 并 exit 1（判 returncode，禁子串判通过）。

七组判据（AT-9.md §六层4、§七 B5、§六删除合同；数据合同 B0「自检结果」；C4 收口：夹具全部自建，
禁依赖真实任务产物——auto-0003/ai-3-2-2 被 UI 删除即断链的教训）：
  ⓪ 摘要 replaced 列（B5③查修）：自建任务 auto-9167（临时夹具）的 replaced = 已传图行数/必换行数
    且与清单实算一致、非空——「表格全显—」在桥端摘要与前端渲染两层都对得上。
  ① 自检正例：必换行全传图 → 四项全 PASS 不误红；get_task 透出 自检 四条 {项,结论,证据}（含 AT-11 组绑定一致性）。
  ② 自检反例：必换行无图+非必换行有图 → 锁点对账必红、次序对账不误红；乱序提交快照 →
    次序对账必红；已传图行特征词人工植入正文 → 残留必红（三项各自可能 FAIL）。
  ③ #3↔#5 疑似不自动联动：疑似在场、确认组为空（疑似≠已组）；不在确认组的行写路径单行、
    不联动他行；组写路径对非组行如实抛错。
  ④ done 态可删 + 墓碑：DELETABLE_STATES 的 completed→done 修正后，done 免确认删除 200、
    磁盘消失、墓碑在案、编号不复用；_job_local_id 修正后运行中 JOB 守卫对真实后台任务生效。
  ⑤ retry 语义：POST /api/hub/task/{id}/retry——非 failed → 409；failed + 产物齐 →
    200 且后台重跑即续（failed→discussing→awaiting_assets，不重发模型）。
  ⑥ 确认后关闭前端渲染仍推进：只 POST confirm（带 按原样 勾选记录），不调任何渲染接口 →
    状态 rendering→awaiting_approval 自行推进，自检.json 自动落盘且三项全 PASS。
探针临时任务（auto-916x）与临时目录用完清理；渲染/续跑夹具用仓库自建件 tests/fixtures/at9-probe。
"""
from __future__ import annotations

import contextlib
import io
import json
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]          # wanrepro/
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ui"))
sys.path.insert(0, str(ROOT / "pipeline" / "reverse"))

import hub_api                                        # noqa: E402
import render                                         # noqa: E402
from automation import cards, config, ingest, replace, selfcheck  # noqa: E402

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8776
BASE = f"http://127.0.0.1:{PORT}"
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 红线：本机直连不走代理
ANALYSIS = ROOT / "analysis" / "reverse"
PROJECTS = config.PROJECTS_DIR
NOW_TS = str(int(time.time()))

# 固定临时任务号（高于真实任务；探针尾全部清理，含自己的墓碑条目）
T_POS, T_NEG, T_LNK, T_DONE, T_RETRY, T_FLOW, T_SUM = (
    "auto-9161", "auto-9162", "auto-9163", "auto-9164", "auto-9165", "auto-9166", "auto-9167")
PROBE_IDS = {T_POS, T_NEG, T_LNK, T_DONE, T_RETRY, T_FLOW, T_SUM}
BRIDGE_POS = "probe-at9b5-pos"          # 正/反例与续跑流共用一批夹具桥目录
BRIDGE_LNK = "probe-at9b5-lnk"          # 疑似不联动夹具
BRIDGE_FLOW = "probe-at9b5-flow"        # ⑥ 确认续跑夹具（带产物三件套）
BRIDGE_DONE = "probe-at9b5-done"        # ④ done 删除夹具（专属桥目录验证删除范围）
BRIDGE_SUM = "probe-at9b5-sum"          # ⓪ replaced 摘要夹具（自建任务，不依赖真实任务）
# 渲染/续跑夹具：仓库自建件（版本受控，不随任务删除失效——AT-10 C4 裁定）
AUDIT_SCENE = ROOT / "tests" / "fixtures" / "at9-probe"
URL1 = "https://h.uguu.se/b5-u1.png"
URL11 = "https://h.uguu.se/b5-u11.png"

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" —— {detail}" if detail and not cond else ""))
    return bool(cond)


def req(path: str, method: str = "GET", payload: dict | None = None,
        timeout: int = 120) -> tuple[int, dict | bytes]:
    data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    if data is not None:
        r.add_header("Content-Type", "application/json")
    try:
        resp = OP.open(r, timeout=timeout)
        raw = resp.read()
        ctype = resp.headers.get("Content-Type") or ""
        return resp.status, (json.loads(raw) if "json" in ctype else raw)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return e.code, raw


def make_task(local_id: str, state_name: str, bridge_id: str = "",
              video_name: str = "", *, prompt: bool = False,
              extra: dict | None = None) -> Path:
    """B2 同款任务夹具（临时号，跑完删）。"""
    tdir = PROJECTS / local_id
    shutil.rmtree(tdir, ignore_errors=True)
    (tdir / "inbox").mkdir(parents=True, exist_ok=True)
    if video_name:
        (tdir / "inbox" / video_name).write_bytes(b"probe-fake-video")
    task = {
        "local_id": local_id, "bridge_id": bridge_id or f"probe-at9b5-{local_id}",
        "created_at": "2026-01-01T00:00:00+08:00", "state": state_name,
        "history": [{"at": "2026-01-01T00:00:00+08:00", "from": None, "to": state_name,
                     "note": "probe-at9b5 夹具"}],
        "intent": "AT-9 B5 探针夹具", "assets": [],
        "ingestion": {"zip_sha256": f"probe-{local_id}", "zip_name": "probe",
                      "video_name": video_name, "text_name": "", "ignored": []},
    }
    if prompt:
        (tdir / "提示词.md").write_text("# 提示词\n\n夹具正文。\n", encoding="utf-8")
    if extra:
        task.update(extra)
    (tdir / "任务.json").write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
    return tdir


def write_checklist(bridge: str, text: str) -> Path:
    bdir = ANALYSIS / bridge
    bdir.mkdir(parents=True, exist_ok=True)
    path = bdir / "待确认清单.md"
    path.write_text(text, encoding="utf-8")
    return path


def render_fixture_prompt(bridge: str, uploads: dict[int, str]) -> tuple[str, list[tuple[int, int]]]:
    """B2 同源渲染夹具：自建夹具拆解 + 指定绑图集合 → 提示词全文与发号映射（stdout 静音）。"""
    ck_path = ANALYSIS / bridge / "待确认清单.md"
    disc = (AUDIT_SCENE / "拆解.md").read_text(encoding="utf-8-sig")
    layers, _order = render.parse_layers(disc)
    fm, body = render.split_frontmatter(
        (ROOT / "templates" / "seedance-flow.md").read_text(encoding="utf-8-sig"))
    sections, _ = render.parse_sections(body)
    ents = render.parse_checklist(ck_path)
    for ent in ents:
        ent.image = uploads.get(ent.row_no, "")
    render.assign_numbers(ents)
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        prompt = render.render_prompt(fm, sections, layers, ents,
                                      {"时长": "22", "画幅": "9:16"}, bridge, "seedance-flow")
    mapping = [(e.row_no, e.no) for e in ents if e.no]
    return prompt, mapping


CK_FULL = """# 待确认清单 · {bridge}

| # | 类别 | 模型看到什么 | 命名 | 资产图 | 锁什么 | 建议 | 用我的描述 |
|---|------|-------------|------|--------|--------|------|------------|
| 1 | 主体 | 黑色双耳奖杯造型香水小物体。上黑下透明渐变玻璃质感 | {name1} | {img1} | 外形比例 | 必须换 |  |
| 2 | 人物 | A：20-25岁女性，波波头短发，五官立体 |  |  | 发型轮廓 | 可沿用 |  |
| 3 | 人物 | B：20-25岁女性，深色半扎高马尾长卷发 |  |  | 发型轮廓 | 可沿用 |  |
| 10 | 道具 | 哑光黑色开合式外包装纸盒 |  |  | 颜色 | 建议准备 |  |
| 11 | 道具 | C：双耳奖杯形态玻璃香水小物体，黑透渐变 |  | {img11} | 颜色、支点位置 | 必须换 |  |
"""


def seed_selfcheck_inputs(tdir: Path, prompt: str, submitted: list[dict],
                          acks: list[int] | None = None,
                          empty_confirm: bool = False) -> None:
    """写 提示词.md + 确认时点快照（assets_confirmed/按原样确认/empty_confirm）进任务夹具。"""
    (tdir / "提示词.md").write_text(prompt, encoding="utf-8")
    task = json.loads((tdir / "任务.json").read_text(encoding="utf-8"))
    task["assets_confirmed"] = submitted
    if acks:
        task["按原样确认"] = {"rows": sorted(set(acks)), "at": "probe"}
    if empty_confirm:
        task["empty_confirm"] = True
    (tdir / "任务.json").write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")


def items_by_name(items: list[dict]) -> dict[str, dict]:
    return {x.get("项"): x for x in items}


# ---------------------------------------------------------------- ⓪ replaced 摘要列（自建任务）
def probe_replaced_summary() -> None:
    print("0) 摘要 replaced 列（自建任务 auto-9167）：已传图行数/必换行数 非空且与清单实算一致")
    make_task(T_SUM, "awaiting_assets", BRIDGE_SUM)
    # 必换行 #1/#11 都填图（与 ① 正例同形态）→ 实算 2/2
    write_checklist(BRIDGE_SUM, CK_FULL.format(bridge=BRIDGE_SUM, name1="", img1=URL1, img11=URL11))
    ck_rows = replace.parse_rows(
        (ANALYSIS / BRIDGE_SUM / "待确认清单.md").read_text(encoding="utf-8-sig"))
    filled = sum(1 for r in ck_rows if (r.get("cur_assets") or "").strip())
    must = sum(1 for r in ck_rows if "必须换" in (r.get("advice") or ""))
    status, body = req("/api/hub/tasks")
    rows = {t.get("local_id"): t for t in (body.get("tasks") or [])} if status == 200 else {}
    check("GET /api/hub/tasks → 200", status == 200, str(status))
    t = rows.get(T_SUM)
    if not check(f"自建任务 {T_SUM} 在列表（摘要覆盖临时任务）", bool(t), str(list(rows))[:160]):
        return
    got = str(t.get("replaced") or "")
    check(f"replaced 非空且 = 已传图行数/必换行数（清单实算 {filled}/{must}）",
          got == f"{filled}/{must}" and filled > 0, f"got={got!r}")
    check(f"{T_SUM} 详情接口同值（摘要与详情两层面一致）",
          str(req(f"/api/hub/task/{T_SUM}")[1].get("task", {}).get("replaced") or "") == got, got)


# ---------------------------------------------------------------- ① 正例 ② 反例（进程内，判定器本身）
def probe_selfcheck_cases() -> None:
    print("1) 自检正例：必换行(#1/#11)全传图 → 三项全 PASS 不误红；get_task 透出 {项,结论,证据}")
    write_checklist(BRIDGE_POS, CK_FULL.format(bridge=BRIDGE_POS, name1="", img1=URL1, img11=URL11))
    tdir = make_task(T_POS, "awaiting_approval", BRIDGE_POS, prompt=True)
    prompt, mapping = render_fixture_prompt(BRIDGE_POS, {1: URL1, 11: URL11})
    check("夹具发号映射 row→图号 = [(1,1),(11,2)]（发号与行号分离的活证据）",
          mapping == [(1, 1), (11, 2)], str(mapping))
    seed_selfcheck_inputs(tdir, prompt,
                          submitted=[{"no": 1, "url": URL1}, {"no": 11, "url": URL11}])
    items = selfcheck.run_selfcheck(tdir)
    by = items_by_name(items)
    check("自检恰四项（次序对账/绑图行残留/锁点对账/组绑定一致性——AT-11 第4项）",
          list(by) == ["次序对账", "绑图行残留", "锁点对账", "组绑定一致性"], str(list(by)))
    check("正例三项全 PASS（不误红）",
          all(x.get("结论") == "PASS" for x in items), json.dumps(items, ensure_ascii=False)[:300])
    check("次序对账证据给出 row→图号→提交次序三方（非行号==图号字面比较）",
          "行→图号={1: 1, 11: 2}" in by["次序对账"]["证据"] and "提交次序=[1, 11]" in by["次序对账"]["证据"],
          by["次序对账"]["证据"][:160])
    check("字段合同：每条恰 {项,结论,证据}",
          all(set(x.keys()) == {"项", "结论", "证据"} for x in items), str([set(x.keys()) for x in items]))
    check("自检.json 落盘 projects/<id>/自检.json", (tdir / "自检.json").is_file(), str(tdir))
    detail = hub_api.get_task(T_POS)
    check("get_task 透出 自检（四项全 PASS）",
          isinstance(detail.get("自检"), list) and len(detail["自检"]) == 4
          and all(x["结论"] == "PASS" for x in detail["自检"]),
          str(detail.get("自检"))[:160])

    print("2) 自检反例：必换行无图+非必换行有图 → 锁点必红、次序不误红；乱序提交与残留各自必红")
    write_checklist(BRIDGE_POS, CK_FULL.format(bridge=BRIDGE_POS, name1="", img1="", img11=URL11))
    tdir = make_task(T_NEG, "awaiting_approval", BRIDGE_POS, prompt=True)
    prompt, mapping = render_fixture_prompt(BRIDGE_POS, {11: URL11})     # 错传形态：图落在非必换行 11，必换行 1 没图
    check("反例夹具发号 = [(11,1)]（发号跟着已传行走，行号≠图号）", mapping == [(11, 1)], str(mapping))
    seed_selfcheck_inputs(tdir, prompt, submitted=[{"no": 11, "url": URL11}])   # 无按原样记录、无 empty_confirm
    items = selfcheck.run_selfcheck(tdir)
    by = items_by_name(items)
    check("反例：锁点对账 FAIL（必换行 1 未换且无确认时点记录）",
          by["锁点对账"]["结论"] == "FAIL" and "必换行 [1] 未换" in by["锁点对账"]["证据"],
          by["锁点对账"]["证据"][:200])
    check("反例：次序对账不误红（错传是锁点的职责，次序本身一致）",
          by["次序对账"]["结论"] == "PASS", by["次序对账"]["证据"][:160])
    check("反例：绑图行残留不误红（#11 自己的特征词已被修复链吃掉）",
          by["绑图行残留"]["结论"] == "PASS", by["绑图行残留"]["证据"][:160])

    # ②b 乱序提交快照 → 次序对账必红（判据可能 FAIL：发号序 vs 提交序不等）
    write_checklist(BRIDGE_POS, CK_FULL.format(bridge=BRIDGE_POS, name1="", img1=URL1, img11=URL11))
    tdir2 = make_task(T_NEG, "awaiting_approval", BRIDGE_POS, prompt=True)
    prompt_ok, _ = render_fixture_prompt(BRIDGE_POS, {1: URL1, 11: URL11})   # 提示词/清单一致，只有提交次序颠倒
    seed_selfcheck_inputs(tdir2, prompt_ok,
                          submitted=[{"no": 11, "url": URL11}, {"no": 1, "url": URL1}])
    by2 = items_by_name(selfcheck.run_selfcheck(tdir2))
    check("乱序提交快照 → 次序对账 FAIL（提交第1张应来自发号第1的行）",
          by2["次序对账"]["结论"] == "FAIL" and "应为图1" in by2["次序对账"]["证据"],
          by2["次序对账"]["证据"][:200])

    # ②c 已传图行特征词人工植入正文 → 残留必红（残留检查可能 FAIL；单绑图行夹具，证据不被截断淹没）
    write_checklist(BRIDGE_POS, CK_FULL.format(bridge=BRIDGE_POS, name1="", img1="", img11=URL11))
    prompt_single, _ = render_fixture_prompt(BRIDGE_POS, {11: URL11})
    sentence = "特写双耳奖杯形态玻璃香水小物体静置桌面。"
    if "# 附注" in prompt_single:
        prompt_bad = prompt_single.replace("# 附注", sentence + "\n\n# 附注")
    else:                                   # seedance-flow 流式正文没有附注节，直接接在正文尾
        prompt_bad = prompt_single.rstrip() + "\n" + sentence + "\n"
    by3 = items_by_name(_selfcheck_with(tdir2, prompt_bad))
    check("已传图行(#11)特征词在正文 → 绑图行残留 FAIL",
          by3["绑图行残留"]["结论"] == "FAIL" and "双耳奖杯形态玻璃香水小物体" in by3["绑图行残留"]["证据"],
          by3["绑图行残留"]["证据"][:200])


def _selfcheck_with(tdir: Path, prompt: str) -> list[dict]:
    (tdir / "提示词.md").write_text(prompt, encoding="utf-8")
    return selfcheck.run_selfcheck(tdir)


# ---------------------------------------------------------------- ③ #3↔#5 疑似不自动联动
CK_LNK = """# 待确认清单 · {bridge}

| # | 类别 | 模型看到什么 | 命名 | 资产图 | 锁什么 | 建议 | 用我的描述 |
|---|------|-------------|------|--------|--------|------|------------|
| 3 | 人物 | A：20-25岁女性，波波头短发，五官立体 |  |  | 发型轮廓 | 可沿用 |  |
| 5 | 人物 | B：20-25岁女性，深色半扎高马尾长卷发 |  |  | 发型轮廓 | 可沿用 |  |
"""


def lnk_sidecar(bridge: str) -> None:
    payload = {"schema": "at8-asset-spans-v1", "video_id": bridge, "source": "probe-at9b5",
               "rows": [{"n": 3, "类别": "人物", "出现时段": [], "同实体疑似": [5], "来源": "探针夹具"},
                        {"n": 5, "类别": "人物", "出现时段": [], "同实体疑似": [3], "来源": "探针夹具"}]}
    (ANALYSIS / bridge / "待确认清单.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def probe_suspect_no_autolink() -> None:
    print("3) #3↔#5 疑似不自动联动：疑似≠已组；写路径只认确认组，贴图不波及疑似行")
    make_task(T_LNK, "awaiting_assets", BRIDGE_LNK)
    write_checklist(BRIDGE_LNK, CK_LNK.format(bridge=BRIDGE_LNK))
    lnk_sidecar(BRIDGE_LNK)
    cs = {c["n"]: c for c in cards.build(BRIDGE_LNK, T_LNK)}
    check("自建夹具疑似互连：#3↔#5", cs[3]["同实体疑似"] == [5] and cs[5]["同实体疑似"] == [3],
          f"{cs[3]['同实体疑似']}/{cs[5]['同实体疑似']}")
    check("自建夹具确认组为空（默认不联动）",
          cs[3]["同实体确认"] == [] and cs[5]["同实体确认"] == [],
          f"{cs[3]['同实体确认']}/{cs[5]['同实体确认']}")
    status, body = req("/api/hub/asset", "POST",
                       {"task_id": T_LNK, "n": 5, "image_url": URL11})
    md = (ANALYSIS / BRIDGE_LNK / "待确认清单.md").read_text(encoding="utf-8-sig")
    r3 = next((r for r in replace.parse_rows(md) if r["no"] == 3), {})
    r5 = next((r for r in replace.parse_rows(md) if r["no"] == 5), {})
    check("贴图 #5 → 200（单行照常）", status == 200, f"{status} {str(body)[:120]}")
    check("#5 已写图、疑似行 #3 一字不动（疑似不联动）",
          (r5.get("cur_assets") or "") == URL11 and (r3.get("cur_assets") or "") == "",
          f"r5={r5.get('cur_assets')!r} r3={r3.get('cur_assets')!r}")
    link_write = False
    try:
        replace.set_group_write(PROJECTS / T_LNK, 3, image_url=URL1)
    except replace.ReplaceError:
        link_write = True
    check("组写路径对非确认组行（疑似）如实抛错——联动只能来自确认组", link_write)


# ---------------------------------------------------------------- ④ done 态可删 + 墓碑
def probe_done_delete() -> None:
    print("4) done 态可删（DELETABLE_STATES completed→done）+ 墓碑防复用 + 运行中 JOB 守卫")
    make_task(T_DONE, "done", BRIDGE_DONE)
    write_checklist(BRIDGE_DONE, CK_LNK.format(bridge=BRIDGE_DONE))
    status, body = req(f"/api/hub/task/{T_DONE}/delete", "POST", {})
    check("done 免确认删除 → 200（修正后 done 是可删终态）",
          status == 200 and body.get("ok") is True and body.get("state_was") == "done",
          f"{status} {str(body)[:160]}")
    check("删除范围①：projects/<id> 已消失", not (PROJECTS / T_DONE).exists(), "")
    check("删除范围②：专属 analysis 桥目录已消失", not (ANALYSIS / BRIDGE_DONE).exists(), "")
    tombs = json.loads((PROJECTS / ".tombstones.json").read_text(encoding="utf-8-sig")) \
        if (PROJECTS / ".tombstones.json").is_file() else []
    check("墓碑在案（.tombstones.json 含该 id）", any(x.get("id") == T_DONE for x in tombs), str(tombs)[:200])
    nxt = ingest._next_local_id()
    check("编号防复用：_next_local_id 不再给该号", nxt != T_DONE, f"next={nxt}")
    try:
        from automation import state as state_mod
        bad = PROJECTS / T_DONE
        bad.mkdir(parents=True, exist_ok=True)
        (bad / "任务.json").write_text(json.dumps({"local_id": T_DONE, "state": "completed"}),
                                       encoding="utf-8")
        state_mod.load(bad)
        completed_rejected = False
    except state_mod.StateError:
        completed_rejected = True
    check("「completed」不是状态机状态（旧 DELETABLE_STATES 项从未可达，done 才是真终态）",
          completed_rejected, "")
    shutil.rmtree(PROJECTS / T_DONE, ignore_errors=True)

    check("_job_local_id(任务目录 Path) → 任务号字符串（运行中 JOB 守卫可比对）",
          hub_api._job_local_id(PROJECTS / T_DONE) == T_DONE,
          str(hub_api._job_local_id(PROJECTS / T_DONE)))
    make_task(T_DONE, "done", BRIDGE_DONE)
    write_checklist(BRIDGE_DONE, CK_LNK.format(bridge=BRIDGE_DONE))
    # 运行中 JOB 守卫：HUB_JOBS 是 server 进程内存态，进程内直调 delete_task 才读得到同一份
    # （B2 探针同法；HTTP 面打不到探针进程里塞的假 job）
    hub_api.HUB_JOBS["probe-b5-fake"] = {"status": "running", "kind": "hub-render",
                                         "local_id": T_DONE, "started": "probe"}
    try:
        hub_api.delete_task(T_DONE)
        job_guard = False
    except hub_api.HubError:
        job_guard = True
    finally:
        hub_api.HUB_JOBS.pop("probe-b5-fake", None)
    check("运行中 JOB 挂着时删除被拒（HubError；删除合同：排队 JOB 一律拒绝）", job_guard, "")
    check("被拒删除任务仍在盘（守护挡下即不动）", (PROJECTS / T_DONE).exists(), "")
    status, body = req(f"/api/hub/task/{T_DONE}/delete", "POST", {})
    check("JOB 清掉后同任务可删（守护只在 job 在跑时挡）", status == 200, f"{status} {str(body)[:120]}")


# ---------------------------------------------------------------- ⑤ retry 语义
def seed_bridge_products(bridge: str) -> None:
    """夹具桥目录补齐产物三件套（has_disc/has_check/has_prompt）——产物优先续跑的依据。"""
    bdir = ANALYSIS / bridge
    bdir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(AUDIT_SCENE / "拆解.md", bdir / "拆解.md")
    prompts = sorted(AUDIT_SCENE.glob("提示词-*.md"))
    if prompts:
        shutil.copyfile(prompts[0], bdir / prompts[0].name)


def _wait_state(local_id: str, want: str, seconds: int = 60) -> str:
    deadline = time.time() + seconds
    last = ""
    while time.time() < deadline:
        s, body = req(f"/api/hub/task/{local_id}")
        last = str((body.get("task") or {}).get("state") or "") if isinstance(body, dict) else ""
        if last == want:
            return last
        time.sleep(2)
    return last


def probe_retry() -> None:
    print("5) retry 语义：非 failed → 409；failed + 产物齐 → 后台重跑即续 awaiting_assets")
    make_task(T_RETRY, "awaiting_assets", BRIDGE_LNK)
    status, body = req(f"/api/hub/task/{T_RETRY}/retry", "POST", {})
    check("非 failed 重试 → 409（只有失败任务可重试）",
          status == 409 and "failed" in str(body.get("error", "")), f"{status} {str(body)[:120]}")

    seed_bridge_products(BRIDGE_LNK)
    write_checklist(BRIDGE_LNK, CK_LNK.format(bridge=BRIDGE_LNK))
    make_task(T_RETRY, "failed", BRIDGE_LNK,
              extra={"history": [{"at": "probe", "from": "discussing", "to": "failed",
                                  "note": "probe 夹具：环境级故障"}]})
    status, body = req(f"/api/hub/task/{T_RETRY}/retry", "POST", {})
    check("failed 重试受理 → 200 带 job_id（异步续跑）",
          status == 200 and bool(body.get("job_id")), f"{status} {str(body)[:160]}")
    end_state = _wait_state(T_RETRY, "awaiting_assets", 60)
    check("重跑即续：failed → … → awaiting_assets（产物优先，未重发模型）",
          end_state == "awaiting_assets", f"end={end_state}")
    task = json.loads((PROJECTS / T_RETRY / "任务.json").read_text(encoding="utf-8"))
    notes = "｜".join(str(h.get("note") or "") for h in (task.get("history") or []))
    check("history 记人工重试（failed 唯一合法出口）",
          "人工重试" in notes and "discussing" in [h.get("to") for h in task.get("history") or []],
          notes[:200])
    if body.get("job_id"):
        s, rec = req(f"/api/hub/job/{body['job_id']}")
        check("续跑 job 收尾 done", s == 200 and rec.get("status") == "done", str(rec)[:120])


# ---------------------------------------------------------------- ⑥ 确认后渲染续跑
CK_FLOW = """# 待确认清单 · {bridge}

| # | 类别 | 模型看到什么 | 命名 | 资产图 | 锁什么 | 建议 | 用我的描述 |
|---|------|-------------|------|--------|--------|------|------------|
| 1 | 主体 | 黑色双耳奖杯造型香水小物体。上黑下透明渐变玻璃质感 |  |  | 外形比例 | 必须换 |  |
| 3 | 人物 | A：20-25岁女性，波波头短发，五官立体 |  |  | 发型轮廓 | 可沿用 |  |
| 5 | 人物 | B：20-25岁女性，深色半扎高马尾长卷发 |  |  | 发型轮廓 | 可沿用 |  |
| 10 | 道具 | 哑光黑色开合式外包装纸盒 |  |  | 颜色 | 建议准备 |  |
| 11 | 道具 | C：双耳奖杯形态玻璃香水小物体，黑透渐变 |  |  | 颜色、支点位置 | 必须换 |  |
"""


def probe_confirm_render_flow() -> None:
    print("6) 确认后关闭前端渲染仍推进：只 POST confirm（带 按原样）→ rendering 自行推进 + 自检自动落盘")
    make_task(T_FLOW, "awaiting_assets", BRIDGE_FLOW)
    seed_bridge_products(BRIDGE_FLOW)
    write_checklist(BRIDGE_FLOW, CK_FLOW.format(bridge=BRIDGE_FLOW))
    status, body = req("/api/hub/asset", "POST",
                       {"task_id": T_FLOW, "n": 1, "image_url": URL1})
    check("置换台贴图 #1 → 200（真实写路径）", status == 200, f"{status} {str(body)[:120]}")
    # 确认（前端此时已关页：此后不再调任何渲染接口）——必换行 11 未换，带「按原样」勾选
    status, body = req(f"/api/hub/confirm/{T_FLOW}", "POST", {"按原样": [11]})
    check("confirm → 200 且 state=rendering（停点迁移在后端）",
          status == 200 and body.get("state") == "rendering", f"{status} {str(body)[:160]}")
    check("confirm 回 job_id（后端已接手渲染，前端零执行责任）", bool(body.get("job_id")),
          str(body)[:120])
    end_state = _wait_state(T_FLOW, "awaiting_approval", 90)
    check("无前端参与：rendering 自行推进到 awaiting_approval（渲染续跑不依赖页面）",
          end_state == "awaiting_approval", f"end={end_state}")
    task = json.loads((PROJECTS / T_FLOW / "任务.json").read_text(encoding="utf-8"))
    check("『按原样』勾选记录落确认时点快照（自检③数据源）",
          (task.get("按原样确认") or {}).get("rows") == [11], str(task.get("按原样确认")))
    detail = hub_api.get_task(T_FLOW)
    items = detail.get("自检") or []
    check("渲染完成自检自动跑：get_task 透出四项 {项,结论,证据}",
          len(items) == 4 and all(set(x) == {"项", "结论", "证据"} for x in items), str(items)[:200])
    check("全链正例四项全 PASS（1 张图 + 1 行按原样）",
          all(x["结论"] == "PASS" for x in items), json.dumps(items, ensure_ascii=False)[:300])
    check("锁点证据反映确认时点记录（未换=[11] 且有记录）",
          "未换=[11]" in (items_by_name(items).get("锁点对账", {}).get("证据") or ""),
          str(items_by_name(items).get("锁点对账", {}).get("证据"))[:200])


# ---------------------------------------------------------------- 清理与入口
def cleanup_fixtures() -> None:
    for tid in PROBE_IDS:
        shutil.rmtree(PROJECTS / tid, ignore_errors=True)
    for b in (BRIDGE_POS, BRIDGE_LNK, BRIDGE_FLOW, BRIDGE_DONE, BRIDGE_SUM):
        shutil.rmtree(ANALYSIS / b, ignore_errors=True)
    tfile = PROJECTS / ".tombstones.json"
    if tfile.is_file():
        try:
            items = json.loads(tfile.read_text(encoding="utf-8-sig"))
            items = [x for x in items if isinstance(x, dict) and x.get("id") not in PROBE_IDS]
            if items:
                tfile.write_text(json.dumps(items, ensure_ascii=False, indent=2) + "\n",
                                 encoding="utf-8")
            else:
                tfile.unlink()
        except (OSError, json.JSONDecodeError):
            pass


def main() -> int:
    print(f"[probe-at9] 目标 {BASE}；临时任务 {sorted(PROBE_IDS)} 用完清理；夹具自建不碰真实任务")
    if not AUDIT_SCENE.is_dir():
        check("自建夹具在场（tests/fixtures/at9-probe）", False, str(AUDIT_SCENE))
        return 1
    try:
        probe_replaced_summary()
        probe_selfcheck_cases()
        probe_suspect_no_autolink()
        probe_done_delete()
        probe_retry()
        probe_confirm_render_flow()
    finally:
        cleanup_fixtures()
    fails = [r for r in RESULTS if not r[1]]
    print(f"\n[probe-at9] {len(RESULTS) - len(fails)}/{len(RESULTS)} PASS"
          + ("（夹具已清理）" if not fails else ""))
    for name, ok, detail in fails:
        print(f"  FAIL 汇总: {name} —— {detail}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
