#!/usr/bin/env python
"""AT-9 B2 探针：残留修复 + 确认组联动 + 全沿用/回退边 + 删除守护/墓碑 + 短名映射表。

跑法：wanrepro/ 下 `python -m automation.probe_at9_b2 [port]`（默认 8776 常驻工作台，
判据全对才 exit 0，任何一步失败如实 FAIL 并 exit 1——判 returncode，禁子串判通过）。

六组判据（AT-9.md §六层2/层3/层4、§四操作合同、§六删除合同；数据合同 B0）：
  ① 渲染残留：ai-3-2-2 内存夹具复现 L1 P129 场景（#1/#11 绑图）——已传图行原物核心词
    固定词表零残留；未绑图场景原描述一字不动（不误伤）；绑图行 row→图号发号分离断言。
  ② 确认组：成组/解组端点写 sidecar『同实体确认』；组联动=replace.set_group_write 单点
    （清单+上传流水+回显三处一致）；任务级串行锁；『同实体疑似』不联动（#3↔#5 反例）。
  ③ 全沿用：empty_confirm:true 显式空置换确认通过并推进 awaiting_assets→rendering；
    rendering 停点守卫仍在（空确认不可绕过）。
  ④ 回退边：awaiting_approval→awaiting_assets 受控回退（丢提示词、note 记录），退回后再确认成功。
  ⑤ 删除：REST /api/hub/task/{id}/delete；运行中与排队 JOB 409；拆解过但 idle 需任务名确认；
    删除范围=任务目录+专属 analysis 目录+uploads 视频；墓碑 .tombstones.json 防编号复用。
  ⑥ 短名映射表：核心产品→本品主体/道具→按实体名/人物→按描述特征/退路几何+行号；
    无括注；跨类别同实体联名（at8 #2/#7 兼容）。
探针临时任务（auto-910x/auto-9199）与临时目录用完清理，不留垃圾（真拆产物不涉及）。
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import threading
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
from automation import cards, config, ingest, replace  # noqa: E402

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8776
BASE = f"http://127.0.0.1:{PORT}"
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 红线：本机直连不走代理
ANALYSIS = ROOT / "analysis" / "reverse"
PROJECTS = config.PROJECTS_DIR
UPLOADS = ROOT / "uploads"
NOW_TS = str(int(__import__("time").time()))

# 固定任务号（高于真实任务 auto-000x；探针尾清理任务目录与自己的墓碑条目）
T_GRP, T_EMPTY, T_GUARD, T_ROLL = "auto-9101", "auto-9102", "auto-9103", "auto-9104"
T_DELCONF, T_RUN, T_JOB, T_DEL = ("auto-9105", "auto-9106", "auto-9107", "auto-9199")
PROBE_IDS = {T_GRP, T_EMPTY, T_GUARD, T_ROLL, T_DELCONF, T_RUN, T_JOB, T_DEL}
BRIDGE_GRP = "probe-at9b2-grp"
BRIDGE_DEL = "probe-at9b2-del"
PROBE_VIDEO = "probe-at9b2-del.mp4"

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" —— {detail}" if detail and not cond else ""))
    return bool(cond)


def req(path: str, method: str = "GET", payload: dict | None = None) -> tuple[int, dict | bytes]:
    data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    if data is not None:
        r.add_header("Content-Type", "application/json")
    try:
        resp = OP.open(r, timeout=180)
        raw = resp.read()
        ctype = resp.headers.get("Content-Type") or ""
        return resp.status, (json.loads(raw) if "json" in ctype else raw)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return e.code, raw


def md_row(md_text: str, no: int) -> dict:
    return next((r for r in replace.parse_rows(md_text) if r["no"] == no), {})


# ---------------------------------------------------------------- 夹具
CHECKLIST_MD = """# 待确认清单 · {bridge}

| # | 类别 | 模型看到什么 | 命名 | 资产图 | 锁什么 | 建议 | 用我的描述 |
|---|------|-------------|------|--------|--------|------|------------|
| 1 | 主体 | 黑色双耳奖杯造型香水小物体。上黑下透明渐变玻璃质感，顶部黑色圆锥盖 |  |  | 外形比例 | 必须换 |  |
| 2 | 道具 | C：双耳奖杯形态玻璃香水小物体，黑透渐变 |  |  | 颜色、支点位置 | 必须换 |  |
| 3 | 人物 | A：20-25岁女性，波波头短发，五官立体 |  |  | 发型轮廓 | 可沿用 |  |
"""


def sidecar_payload(bridge: str) -> dict:
    return {
        "schema": "at8-asset-spans-v1",
        "video_id": bridge,
        "source": "拆解.md",
        "rows": [
            {"n": 1, "类别": "主体", "出现时段": [], "画面位置": {"格": 5, "镜次": 1},
             "同实体疑似": [2], "来源": "探针夹具"},
            {"n": 2, "类别": "道具", "出现时段": [], "画面位置": {"格": 5, "镜次": 1},
             "同实体疑似": [1, 3], "来源": "探针夹具"},
            {"n": 3, "类别": "人物", "出现时段": [], "画面位置": {"格": 2, "镜次": 1},
             "同实体疑似": [2], "来源": "探针夹具"},
        ],
    }


def make_task(local_id: str, state_name: str, bridge_id: str = "",
              video_name: str = "", *, prompt: bool = False) -> Path:
    tdir = PROJECTS / local_id
    if tdir.is_dir():
        shutil.rmtree(tdir)
    (tdir / "inbox").mkdir(parents=True, exist_ok=True)
    if video_name:
        (tdir / "inbox" / video_name).write_bytes(b"probe-fake-video")
    task = {
        "local_id": local_id, "bridge_id": bridge_id or f"probe-at9b2-{local_id}",
        "created_at": "2026-01-01T00:00:00+08:00", "state": state_name,
        "history": [{"at": "2026-01-01T00:00:00+08:00", "from": None, "to": state_name,
                     "note": "probe-at9b2 夹具"}],
        "intent": "AT-9 B2 探针夹具", "assets": [],
        "ingestion": {"zip_sha256": f"probe-{local_id}", "zip_name": "probe",
                      "video_name": video_name, "text_name": "", "ignored": []},
    }
    if prompt:
        (tdir / "提示词.md").write_text("# 提示词\n\n旧提示词正文，退回后必须消失。\n", encoding="utf-8")
        task["approval"] = {"snapshot_sha256": "probe", "by": "probe"}
        task["approval_snapshot"] = "probe"
        task["gen_params"] = {"duration": 5}
    (tdir / "任务.json").write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
    return tdir


def make_bridge(bridge_id: str) -> Path:
    bdir = ANALYSIS / bridge_id
    if bdir.is_dir():
        shutil.rmtree(bdir)
    bdir.mkdir(parents=True, exist_ok=True)
    (bdir / "待确认清单.md").write_text(CHECKLIST_MD.format(bridge=bridge_id), encoding="utf-8")
    payload = sidecar_payload(bridge_id)
    (bdir / "待确认清单.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return bdir


def cleanup_fixtures() -> None:
    """探针尾清理：临时任务目录、临时 bridge 目录、自己的墓碑条目、uploads 夹具视频。"""
    for tid in PROBE_IDS:
        shutil.rmtree(PROJECTS / tid, ignore_errors=True)
    for b in (BRIDGE_GRP, BRIDGE_DEL):
        shutil.rmtree(ANALYSIS / b, ignore_errors=True)
    (UPLOADS / PROBE_VIDEO).unlink(missing_ok=True)
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


# ---------------------------------------------------------------- ① 渲染残留
FIX_WORDS_UPLOADED = ("黑色奖杯香水瓶", "奖杯香水", "双耳奖杯", "黑透渐变",
                      "香水小物体", "尖顶雕花圆盖")
AUDIT_SCENE = ROOT / "analysis" / "reverse" / "ai-3-2-2"


def render_case(uploads: set[int]) -> tuple[str, list[tuple[int, int]]]:
    check_path = AUDIT_SCENE / "待确认清单.md"
    rows = replace.parse_rows(check_path.read_text(encoding="utf-8-sig"))
    disc = (AUDIT_SCENE / "拆解.md").read_text(encoding="utf-8-sig")
    layers, _order = render.parse_layers(disc)
    fm, body = render.split_frontmatter(
        (ROOT / "templates" / "seedance-flow.md").read_text(encoding="utf-8-sig"))
    sections, _ = render.parse_sections(body)
    ents = render.parse_checklist(check_path)
    for ent in ents:
        ent.image = "https://example.invalid/probe-b2.png" if ent.row_no in uploads else ""
    render.assign_numbers(ents)
    import contextlib
    import io
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        prompt = render.render_prompt(fm, sections, layers, ents,
                                      {"时长": "22", "画幅": "9:16"}, "ai-3-2-2", "seedance-flow")
    mapping = [(e.row_no, e.no) for e in ents if e.image]
    return prompt, mapping


def probe_residue() -> None:
    print("1) 渲染残留修复（L1 P129 场景：#1/#11 绑图 → 原物核心词零残留）")
    if not AUDIT_SCENE.is_dir():
        check("ai-3-2-2 参考夹具在场", False, str(AUDIT_SCENE))
        return
    prompt, mapping = render_case({1, 11})
    residual = {w: prompt.count(w) for w in FIX_WORDS_UPLOADED if w in prompt}
    check("已传图行（#1/#11）原物核心词固定词表零残留", not residual, str(residual))
    refs = sorted(set(int(n) for n in re.findall(r"@图片(\d+)", prompt)))
    check("绑图行按序发号 @图片1/@图片2（row_no 与图片 no 分离）", refs == [1, 2], str(refs))
    check("发号映射 row→图号 = [(1,1),(11,2)]", mapping == [(1, 1), (11, 2)], str(mapping))
    check("正文非空且含生成平台可用指令形态", len(prompt.strip()) >= 200, f"{len(prompt)} 字")
    check("修复形态在场：「取出@图片1」（动词保留、原物名词链吃掉）", "取出@图片1" in prompt,
            prompt[:120])
    none_prompt, _ = render_case(set())
    check("未绑图场景原描述保留（黑色奖杯香水瓶在场，不误伤）",
          "黑色奖杯香水瓶" in none_prompt, none_prompt[:120])
    check("未绑图场景不产生任何图号", not re.findall(r"@图片\d+", none_prompt), "")
    wrong_prompt, wrong_map = render_case({10})
    check("只绑 #10：#10 自己的核心短语不残留",
          "哑光黑色开合式外包装纸盒" not in wrong_prompt and wrong_map == [(10, 1)],
          f"map={wrong_map}")
    check("只绑 #10：#1/#11 未绑行描述原样保留（不越界清洗）",
          "黑色奖杯香水瓶" in wrong_prompt, "")


# ---------------------------------------------------------------- ② 确认组
def probe_group() -> None:
    global BRIDGE_GRP
    print("2) 确认组：成组/解组端点 + 组联动三处一致 + 串行锁 + 疑似不联动")
    make_task(T_GRP, "awaiting_assets", BRIDGE_GRP)
    make_bridge(BRIDGE_GRP)
    md_path = ANALYSIS / BRIDGE_GRP / "待确认清单.md"
    side_path = ANALYSIS / BRIDGE_GRP / "待确认清单.json"
    task_file = PROJECTS / T_GRP / "任务.json"

    status, body = req(f"/api/hub/task/{T_GRP}/group", "POST", {"rows": [1, 2]})
    check("POST group rows=[1,2] → 200", status == 200, f"{status} {str(body)[:120]}")
    saved = json.loads(side_path.read_text(encoding="utf-8-sig")).get("同实体确认")
    check("sidecar 顶层『同实体确认』= [[1,2]]（数据合同键位）", saved == [[1, 2]], str(saved))

    status, body = req("/api/hub/asset", "POST",
                       {"task_id": T_GRP, "n": 1, "image_url": "https://h.uguu.se/b2-probe-u1.png"})
    check("组内贴图 n=1 → 200 且回显组=[1,2]", status == 200 and body.get("组") == [1, 2],
          f"{status} {str(body)[:160]}")
    md = md_path.read_text(encoding="utf-8-sig")
    url1, url2 = md_row(md, 1).get("cur_assets"), md_row(md, 2).get("cur_assets")
    check("①清单：组内两行资产图列都已写同一 URL",
          url1 == url2 == "https://h.uguu.se/b2-probe-u1.png", f"r1={url1!r} r2={url2!r}")
    check("②上传流水：pending_replace 组内两行各一条同 URL",
          [(x.get("no"), x.get("url")) for x in json.loads(task_file.read_text(encoding="utf-8"))
           .get("pending_replace") or []] == [(1, "https://h.uguu.se/b2-probe-u1.png"),
                                              (2, "https://h.uguu.se/b2-probe-u1.png")],
          str(json.loads(task_file.read_text(encoding="utf-8")).get("pending_replace")))
    echo_rows = body.get("rows") or []
    check("③写后回显：响应 rows 覆盖组内全行且已传图一致",
          [r.get("n") for r in echo_rows] == [1, 2]
          and all(r.get("已传图") == ["https://h.uguu.se/b2-probe-u1.png"] for r in echo_rows),
          str(echo_rows))
    check("组外行（#3）一字不动", md_row(md, 3).get("cur_assets") == "", md_row(md, 3)["line"][:60])

    status, body = req("/api/hub/asset", "POST",
                       {"task_id": T_GRP, "n": 3, "image_url": "https://h.uguu.se/b2-probe-u3.png"})
    md = md_path.read_text(encoding="utf-8-sig")
    check("行 3 只有『同实体疑似』（2↔3）→ 贴图只写自己，组行 1/2 不被联动",
          status == 200 and md_row(md, 3).get("cur_assets") == "https://h.uguu.se/b2-probe-u3.png"
          and md_row(md, 1).get("cur_assets") == "https://h.uguu.se/b2-probe-u1.png"
          and md_row(md, 2).get("cur_assets") == "https://h.uguu.se/b2-probe-u1.png",
          f"{status} r1={md_row(md, 1).get('cur_assets')!r}")

    status, body = req(f"/api/hub/task/{T_GRP}/group", "POST", {"rows": [1]})
    check("成组负例：单行 → 409", status == 409, f"{status} {str(body)[:100]}")
    status, body = req(f"/api/hub/task/{T_GRP}/group", "POST", {"rows": [1, 99]})
    check("成组负例：行号不在清单 → 409", status == 409, f"{status} {str(body)[:100]}")
    status, body = req(f"/api/hub/task/{T_GRP}/group", "POST", {"rows": [2, 3]})
    check("成组负例：成员已在别的组 → 409", status == 409, f"{status} {str(body)[:100]}")

    # 串行：并发 4 线程同组写 → 全部 200，终态一致（任务级锁：不串味、不撕裂）
    urls = [f"https://h.uguu.se/b2-race-{i}.png" for i in range(4)]
    codes: list[int] = []

    def _hit(u: str) -> None:
        s, _b = req("/api/hub/asset", "POST", {"task_id": T_GRP, "n": 1, "image_url": u})
        codes.append(s)
    threads = [threading.Thread(target=_hit, args=(u,)) for u in urls]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    md = md_path.read_text(encoding="utf-8-sig")
    final1, final2 = md_row(md, 1).get("cur_assets"), md_row(md, 2).get("cur_assets")
    pending = json.loads(task_file.read_text(encoding="utf-8")).get("pending_replace") or []
    pend_by_no = {x.get("no"): x.get("url") for x in pending}
    check("并发写全部受理（4/4 → 200）", codes == [200] * 4, str(codes))
    check("串行终态一致：清单两行同 URL 且 ∈ 并发集合",
          final1 == final2 and final1 in urls, f"r1={final1!r} r2={final2!r}")
    check("串行终态一致：流水 {1,2} 与清单同 URL、组外行 3 流水保留、无重复条目",
          pend_by_no.get(1) == pend_by_no.get(2) == final1
          and pend_by_no.get(3) == "https://h.uguu.se/b2-probe-u3.png"
          and len(pending) == 3,
          str(pend_by_no))
    check("锁面：同任务同锁、不同任务不同锁",
          hub_api._task_lock("auto-x") is hub_api._task_lock("auto-x")
          and hub_api._task_lock("auto-x") is not hub_api._task_lock("auto-y"), "")

    status, body = req(f"/api/hub/task/{T_GRP}/ungroup", "POST", {"rows": [1]})
    saved = json.loads(side_path.read_text(encoding="utf-8-sig")).get("同实体确认")
    check("解组 rows=[1] → 200 且组表清空", status == 200 and saved == [], f"{status} {saved}")
    status, body = req("/api/hub/asset", "POST",
                       {"task_id": T_GRP, "n": 1, "image_url": "https://h.uguu.se/b2-solo.png"})
    md = md_path.read_text(encoding="utf-8-sig")
    check("解组后贴图只写单行（联动解除）",
          status == 200 and "组" not in body
          and md_row(md, 1).get("cur_assets") == "https://h.uguu.se/b2-solo.png"
          and md_row(md, 2).get("cur_assets") == final1,
          f"{status} r2={md_row(md, 2).get('cur_assets')!r}")
    status, body = req(f"/api/hub/task/{T_GRP}/ungroup", "POST", {"rows": [2]})
    check("解组负例：不在组里的行 → 409", status == 409, f"{status} {str(body)[:100]}")


# ---------------------------------------------------------------- ③ 全沿用 ④ 回退边
def probe_confirm_rollback() -> None:
    print("3) 全沿用（empty_confirm）+ 4) 回退边（退回置换）")
    make_task(T_EMPTY, "awaiting_assets")
    status, body = req(f"/api/hub/confirm/{T_EMPTY}", "POST", {})
    check("空置换 + 无 empty_confirm → 409（不许静默放行）", status == 409, f"{status} {str(body)[:120]}")
    status, body = req(f"/api/hub/confirm/{T_EMPTY}", "POST", {"empty_confirm": True})
    check("empty_confirm:true → 200 且 state=rendering（全沿用放行）",
          status == 200 and body.get("state") == "rendering", f"{status} {str(body)[:120]}")
    task = json.loads((PROJECTS / T_EMPTY / "任务.json").read_text(encoding="utf-8"))
    check("全沿用记录落 state（empty_confirm=true）", task.get("empty_confirm") is True,
          str(task.get("empty_confirm")))
    check("assets_confirmed 落 []（提交链可消费）", task.get("assets_confirmed") == [],
          str(task.get("assets_confirmed")))
    check("history note 记全沿用", "全沿用" in ((task.get("history") or [{}])[-1].get("note") or ""), "")

    make_task(T_GUARD, "awaiting_assets")
    import automation.state as state_mod
    try:
        state_mod.transition(PROJECTS / T_GUARD, "rendering")
        guard = False
    except state_mod.StateError:
        guard = True
    check("停点守卫仍在：无确认无 empty_confirm 的 rendering 迁移被拒（不可绕过）", guard, "")

    make_task(T_ROLL, "awaiting_approval", prompt=True)
    status, body = req(f"/api/hub/task/{T_ROLL}/rollback", "POST", {})
    check("POST rollback → 200 且 state=awaiting_assets（受控回退边）",
          status == 200 and body.get("state") == "awaiting_assets", f"{status} {str(body)[:120]}")
    tdir = PROJECTS / T_ROLL
    task = json.loads((tdir / "任务.json").read_text(encoding="utf-8"))
    check("退回丢弃提示词（提示词.md 不在）", not (tdir / "提示词.md").is_file(), "")
    check("退回作废审批/预演参数（approval/approval_snapshot/gen_params 均不在）",
          not any(k in task for k in ("approval", "approval_snapshot", "gen_params")),
          str([k for k in ("approval", "approval_snapshot", "gen_params") if k in task]))
    check("回退 note 记录（history 尾条）",
          "退回置换" in ((task.get("history") or [{}])[-1].get("note") or "")
          and (task.get("history") or [{}])[-1].get("to") == "awaiting_assets",
          str((task.get("history") or [{}])[-1]))
    status, body = req(f"/api/hub/task/{T_EMPTY}/rollback", "POST", {})
    check("回退负例：rendering 态不可退（只有 awaiting_approval 能退）→ 409",
          status == 409, f"{status} {str(body)[:100]}")
    status, body = req(f"/api/hub/confirm/{T_ROLL}", "POST", {"empty_confirm": True})
    check("退回后再确认成功（awaiting_assets → rendering，判据4 全链）",
          status == 200 and body.get("state") == "rendering", f"{status} {str(body)[:120]}")


# ---------------------------------------------------------------- ⑤ 删除守护 + 墓碑
def probe_delete() -> None:
    print("5) 删除端点：守护 + 范围 + 墓碑防复用")

    # 5a. 拆解过但 idle：无确认 409，带任务名确认 200（含专属 analysis 目录 + uploads 视频删除）
    make_task(T_DELCONF, "awaiting_assets", BRIDGE_DEL, PROBE_VIDEO)
    make_bridge(BRIDGE_DEL)
    (UPLOADS / PROBE_VIDEO).write_bytes(b"probe-fake-video")
    status, body = req(f"/api/hub/task/{T_DELCONF}/delete", "POST", {})
    check("awaiting_assets 无 confirm_name → 409", status == 409, f"{status} {str(body)[:120]}")
    status, body = req(f"/api/hub/task/{T_DELCONF}/delete", "POST", {"confirm_name": "错误名字"})
    check("awaiting_assets confirm_name 错 → 409", status == 409, f"{status} {str(body)[:120]}")
    status, body = req(f"/api/hub/task/{T_DELCONF}/delete", "POST", {"confirm_name": T_DELCONF})
    check("带任务名确认 → 200 删除成功", status == 200 and body.get("ok") is True,
          f"{status} {str(body)[:160]}")
    check("删除范围①：projects/<id> 已消失", not (PROJECTS / T_DELCONF).exists(), "")
    check("删除范围②：专属 analysis/reverse/<bridge> 已消失", not (ANALYSIS / BRIDGE_DEL).exists(), "")
    check("删除范围③：uploads 参考视频已消失", not (UPLOADS / PROBE_VIDEO).is_file(), "")
    tombs = json.loads((PROJECTS / ".tombstones.json").read_text(encoding="utf-8-sig")) \
        if (PROJECTS / ".tombstones.json").is_file() else []
    check("墓碑已记 id（projects/.tombstones.json）",
          any(x.get("id") == T_DELCONF for x in tombs), str(tombs)[:200])

    # 5b. 运行中 409 / 排队 JOB 409
    make_task(T_RUN, "rendering")
    status, body = req(f"/api/hub/task/{T_RUN}/delete", "POST", {"confirm_name": T_RUN})
    check("运行中（rendering）删除 → 409", status == 409, f"{status} {str(body)[:120]}")
    check("运行中任务删除后仍在盘（409 拒绝即不动）", (PROJECTS / T_RUN).exists(), "")
    make_task(T_JOB, "ingested")
    hub_api.HUB_JOBS["probe-b2-fake"] = {"status": "running", "kind": "hub-discuss",
                                         "local_id": T_JOB, "started": "probe"}
    try:
        hub_api.delete_task(T_JOB, confirm_name=None)
        job_guard = False
    except hub_api.HubError:
        job_guard = True
    check("排队/运行中 JOB 存在时删除被拒（HubError，守护在端点层）", job_guard, "")
    hub_api.HUB_JOBS.pop("probe-b2-fake", None)
    status, body = req(f"/api/hub/task/{T_JOB}/delete", "POST", {})
    check("JOB 清掉后同任务可删（守护只在 job 在跑时挡）", status == 200, f"{status} {str(body)[:120]}")

    # 5c. ingested 免确认删除 + 墓碑防编号复用（删的是当前最大号 auto-9199）
    make_task(T_DEL, "ingested")
    status, body = req(f"/api/hub/task/{T_DEL}/delete", "POST", {})
    check("ingested idle 免确认删除 → 200", status == 200, f"{status} {str(body)[:160]}")
    nxt = ingest._next_local_id()
    check("墓碑防复用：_next_local_id 跳过已删的 auto-9199（回 auto-9200）",
          nxt == "auto-9200", f"next={nxt}")
    check("被删任务号不可再取（nxt != auto-9199）", nxt != T_DEL, nxt)

    # 探针自己的墓碑条目收尾还原在 cleanup_fixtures；这里先断言 .tombstones.json 结构合法
    raw = json.loads((PROJECTS / ".tombstones.json").read_text(encoding="utf-8-sig"))
    check("墓碑文件结构 = [{id,时间}] 且新条目带时间",
          isinstance(raw, list) and all({"id", "时间"} <= set(x) for x in raw)
          and any(x.get("id") == T_DEL for x in raw), str(raw)[:200])


def _task_dir_safe(local_id: str) -> Path:
    return hub_api._task_dir(local_id)


# ---------------------------------------------------------------- ⑥ 短名映射表
def probe_short_names() -> None:
    print("6) 短名映射表：本品主体/实体名/描述特征/几何退路，无括注")
    for bridge, tid, label in (("pop-3--7", "auto-0002", "pop 样本"), ("ai-3-2-2", "auto-0003", "ai 样本")):
        cs = cards.build(bridge, tid)
        if not check(f"{label} asset_cards 非空（{bridge}）", bool(cs), f"{len(cs)} 张"):
            return
        paren = [(c["n"], c["短名"]) for c in cs if "（" in c["短名"] or "）" in c["短名"]]
        check(f"{label} 全部短名无括注（旧『对方类别括注』已替换）", not paren, str(paren))
        check(f"{label} 每张卡带『同实体确认』字段（确认组读侧透出，无组=[]）",
              all(isinstance(c.get("同实体确认"), list) for c in cs), "")
    pop = {c["n"]: c for c in cards.build("pop-3--7", "auto-0002")}
    check("核心产品 → 本品主体（#2 短名=本品主体…）", pop[2]["短名"].startswith("本品主体"),
          pop[2]["短名"])
    check("非核心主体保实体名（#1=媒介产品…）", "媒介产品" in pop[1]["短名"], pop[1]["短名"])
    check("跨类别同实体联名：#2 带对方称呼（道具…）、#7 带本品主体（主体…），两卡可区分",
          "道具" in pop[2]["短名"] and "主体" in pop[7]["短名"] and pop[2]["短名"] != pop[7]["短名"],
          f"{pop[2]['短名']} / {pop[7]['短名']}")
    check("联名不带括号形态（·同件X）", "·同件" in pop[2]["短名"] and "（" not in pop[2]["短名"],
          pop[2]["短名"])
    ai = {c["n"]: c for c in cards.build("ai-3-2-2", "auto-0003")}
    check("人物 → 按描述特征（#3 波波头…/#5 马尾…，特征可区分）",
          "波波头" in ai[3]["短名"] and "马尾" in ai[5]["短名"] and ai[3]["短名"] != ai[5]["短名"],
          f"{ai[3]['短名']} / {ai[5]['短名']}")
    check("同类别疑似不加标注（#3↔#5 疑似在场、确认组为空——疑似≠已组）",
          ai[3]["同实体疑似"] == [5] and ai[5]["同实体疑似"] == [3]
          and ai[3]["同实体确认"] == [] and ai[5]["同实体确认"] == [],
          f"疑似={ai[3]['同实体疑似']}/{ai[5]['同实体疑似']} 确认={ai[3]['同实体确认']}/{ai[5]['同实体确认']}")
    check("道具 → 按实体名（ai #9 道具A / #10 道具B）",
          ai[9]["短名"] == "道具A" and ai[10]["短名"] == "道具B",
          f"{ai[9]['短名']}/{ai[10]['短名']}")
    check("退路·几何称呼+行号（画面位置在场 → 左侧位·行N）",
          cards._grid_fallback(3, {"格": 4}) == "左侧位·行3"
          and cards._grid_fallback(9, {"格": 9}) == "右下位·行9",
          f"{cards._grid_fallback(3, {'格': 4})}/{cards._grid_fallback(9, {'格': 9})}")
    check("退路·位置缺失 → 行N", cards._grid_fallback(12, None) == "行12"
          and cards._grid_fallback(5, {"格": 99}) == "行5",
          f"{cards._grid_fallback(12, None)}/{cards._grid_fallback(5, {'格': 99})}")
    check("人物特征表提取（无特征 → 裸人物）",
          cards._person_short_name("一位安静的角色") == "人物"
          and "波波头" in cards._person_short_name("A：20-25岁女性，波波头短发"),
          cards._person_short_name("A：20-25岁女性，波波头短发"))


def main() -> int:
    print(f"[probe-at9-b2] 目标 {BASE}；临时任务 {sorted(PROBE_IDS)} 用完清理")
    try:
        probe_residue()
        probe_group()
        probe_confirm_rollback()
        probe_delete()
        probe_short_names()
    finally:
        cleanup_fixtures()
    fails = [r for r in RESULTS if not r[1]]
    print(f"\n[probe-at9-b2] {len(RESULTS) - len(fails)}/{len(RESULTS)} PASS"
          + ("（夹具已清理）" if not fails else ""))
    for name, ok, detail in fails:
        print(f"  FAIL 汇总: {name} —— {detail}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
