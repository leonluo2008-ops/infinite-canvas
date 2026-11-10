#!/usr/bin/env python
"""AT-9 B1 探针：位置字段注入形态 + 真拆 sidecar 新键/十层完整 + 摘要三字段 + 建议结构化兜底。

跑法：wanrepro/ 下 `python -m automation.probe_at9_b1 [port]`（默认 8776 常驻工作台，
需先起 ui/server.py；判据全对才 exit 0，任何一步失败如实 FAIL 并 exit 1——判 returncode，
禁子串判通过）。

真拆口径（视频拆解免费已授权，自动执行）：
  · ffmpeg 现生成一条 6s 竖版视频（gradients seed=时间戳+pid → 逐字节唯一）。幂等键=视频
    内容 sha——同视频重拆会命中旧任务，所以每次运行都换新素材，绝不复用 uploads 旧片。
  · ingest_media（经 bridge 建任务）→ run_discuss（真多模态拆解，1–3 分钟）→ 断言：
    拆解.md 十层标题逐层在场（红线：注入不撕结构）＋ 九 资产 每条资产条目带
    「画面位置：九宫格N·镜M」＋ sidecar（待确认清单.json）每行有「画面位置」新键。
  · 真拆任务与桥侧分析产物**跑完即删**（AT-10 C5：回归探针不留任务——原先「留盘当证据」导致
    projects/ 里堆积 13 条探针任务；现在断言全部跑完后调删除合同（POST /api/hub/task/{id}/delete，
    confirm_name=任务号）清掉任务目录+专属 analysis 目录+uploads 视频，并在尾部断言「跑后列表零新增」）。
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]          # wanrepro/
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ui"))                 # hub_api 在 ui/（探针直调兜底用）

from automation import cards, config, replace  # noqa: E402
from automation.discuss import run_discuss  # noqa: E402
from automation.ingest import ingest_media  # noqa: E402
from automation.state import load  # noqa: E402

import hub_api  # noqa: E402  （ui/ 已在 sys.path：删除合同直调兜底）

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8776
BASE = f"http://127.0.0.1:{PORT}"
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 红线：本机直连不走代理
ANALYSIS = ROOT / "analysis" / "reverse"
TASK2, BRIDGE2 = "auto-0002", "pop-3--7"            # AT-8 既有样本任务（回归参照）
PROJECTS = config.PROJECTS_DIR
PROJECTS_BEFORE: set[str] = set()                   # 跑前任务号集合（尾部「零新增」对账）


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


EXTRACT = _load("at9b1_extract", ROOT / "pipeline" / "reverse" / "extract.py")
CONFIRM = _load("at9b1_confirm", ROOT / "pipeline" / "reverse" / "confirm.py")

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


def valid_pos(pos) -> bool:
    return (isinstance(pos, dict)
            and isinstance(pos.get("格"), int) and 1 <= pos["格"] <= 9
            and isinstance(pos.get("镜次"), int) and pos["镜次"] >= 1)


# ---------------------------------------------------------------- [1] 注入形态
def probe_injection() -> None:
    print("1) 拆解任务书【§N】注入形态（十层标题一个不动）")
    md = (ROOT / "prompts" / "视频拆解.md").read_text(encoding="utf-8")
    fence_match = EXTRACT.FENCE_RE.search(md)
    if not check("提示词正文围栏存在（extract.FENCE_RE 命中第一段 ``` 围栏）", bool(fence_match)):
        return
    fence = fence_match.group(1)
    marker = "【§9·画面位置】"
    bodies = CONFIRM.split_layers(fence)
    check("【§9·画面位置】在 fence 内且落在 九 资产 层正文里（非标题形态）",
          marker in fence and marker in bodies.get("九 资产", ""))
    check("注入指引含九宫格 1-9 编号法与「画面位置：九宫格N·镜M」产出格式",
          "九宫格" in fence and "画面位置：九宫格" in fence)
    heading_lines = [line.strip() for line in fence.splitlines()
                     if re.match(r"^ {0,3}#{1,6}[ \t]", line)]
    titles = [EXTRACT.HEADING_RE.match(line).group(2).strip() for line in heading_lines]
    check("fence 前 10 个标题 = 十层固定标题逐字一致（无新增/改动/换序）",
          titles[:10] == list(EXTRACT.LAYER_TITLES), str(titles[:12]))
    check("注入没有变成标题（任何标题行都不含 画面位置/§）",
          not any(("画面位置" in t or "§" in t) for t in titles), str(titles))
    try:
        head = subprocess.run(
            ["git", "-C", str(ROOT.parent), "show", "HEAD:wanrepro/prompts/视频拆解.md"],
            capture_output=True, timeout=30).stdout.decode("utf-8", errors="replace")
        head_fence = EXTRACT.FENCE_RE.search(head)
        head_titles = [EXTRACT.HEADING_RE.match(line.strip()).group(2).strip()
                       for line in (head_fence.group(1).splitlines() if head_fence else [])
                       if re.match(r"^ {0,3}#{1,6}[ \t]", line.strip())]
        check("围栏内标题清单与 git HEAD 逐字一致（B1 只许加非标题注入，标题结构零变化）",
              bool(head_fence) and head_titles == titles,
              f"HEAD={head_titles[:12]} NOW={titles[:12]}")
    except (OSError, subprocess.TimeoutExpired) as exc:
        check("围栏内标题清单与 git HEAD 逐字一致", False, f"git 不可用: {exc}")
    check("十层标题常数仍是 10 层", len(EXTRACT.LAYER_TITLES) == 10)


# ---------------------------------------------------------------- [2] 真拆
def make_unique_video(dst: Path) -> bool:
    seed = int(time.time()) % 10 ** 8 + os.getpid() % 89   # gradients 的 seed 是有界整数，禁超 2^31
    # gradients(种子唯一底色) + testsrc2(移动方块/彩条/计数) 混合 → 内容更丰富、且逐字节不重复
    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-f", "lavfi", "-i", f"gradients=size=576x1024:duration=6:speed=0.1:seed={seed}",
           "-f", "lavfi", "-i", "testsrc2=size=576x1024:rate=15:duration=6",
           "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
           "-filter_complex", "[0:v][1:v]blend=all_mode=grainmerge:all_opacity=0.35[v]",
           "-map", "[v]", "-map", "2:a",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(dst)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180, shell=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        check("ffmpeg 生成唯一探针视频", False, str(exc))
        return False
    return check("ffmpeg 生成唯一探针视频（seed=时间戳+pid，逐字节不重复）",
                 proc.returncode == 0 and dst.is_file() and dst.stat().st_size > 0,
                 (proc.stderr or "")[-200:])


def probe_real_teardown() -> tuple[str, str]:
    print("2) 真拆：换新视频 → bridge 建任务 → 拆解（已授权）→ 产物断言")
    global PROJECTS_BEFORE
    projects_before = {p.name for p in config.PROJECTS_DIR.glob("auto-*")}
    PROJECTS_BEFORE = set(projects_before)
    tmp = Path(tempfile.mkdtemp(prefix="probe-at9b1-"))
    video = tmp / f"probe-at9b1-{int(time.time())}.mp4"
    if not make_unique_video(video):
        return "", ""
    result = ingest_media(video, intent="AT-9 B1 真拆探针：验证画面位置注入后十层结构完整、"
                                         "每条资产带九宫格位置；产品保留原片节奏")
    check("ingest_media → OK（幂等键=视频 sha，新素材必不 HIT）",
          result.get("result") == "OK" and result.get("local_id") and result.get("bridge_id"),
          json.dumps(result, ensure_ascii=False)[:160])
    check("新建路径返回 hit_existing=False（AT-10 C5 命中标记不误报）",
          result.get("hit_existing") is False,
          json.dumps(result, ensure_ascii=False)[:160])
    local_id, bridge_id = result.get("local_id") or "", str(result.get("bridge_id") or "")
    check("新任务目录在 projects 下且此前不存在", local_id not in projects_before and
          (config.PROJECTS_DIR / local_id / "任务.json").is_file(), local_id)
    try:
        final_state = run_discuss(config.PROJECTS_DIR / local_id)
    except Exception as exc:  # noqa: BLE001 —— 失败原文进断言，不静默
        check("run_discuss 真拆完成 → awaiting_assets", False, f"{type(exc).__name__}: {exc}")
        return local_id, bridge_id
    check("run_discuss 真拆完成 → awaiting_assets", final_state == "awaiting_assets", final_state)

    disc_path = ANALYSIS / bridge_id / "拆解.md"
    disc_md = disc_path.read_text(encoding="utf-8-sig", errors="replace") if disc_path.is_file() else ""
    bodies, missing = EXTRACT.split_layers(disc_md)
    check("拆解.md 十层标题逐层在场（红线3：注入不撕结构）",
          not missing and all(bodies.get(t, "").strip() for t in EXTRACT.LAYER_TITLES),
          f"缺层: {missing}")

    entries = CONFIRM.asset_entries(bodies.get("九 资产", ""))
    check("九 资产 至少解析出 1 条资产条目", len(entries) >= 1, f"{len(entries)} 条")
    bad = [(e["类别"] + (e["名字"] or ""), e["画面位置"],
            (e["正文"] or "")[-30:]) for e in entries if not valid_pos(e.get("画面位置"))]
    check("每条资产条目都带合法「画面位置」（格∈1-9、镜次≥1）", not bad, str(bad[:3]))
    cells = sorted({e["画面位置"]["格"] for e in entries if valid_pos(e.get("画面位置"))})
    print(f"  [info] 条目 {len(entries)} 条，九宫格落点 = {cells}")

    sidecar_path = ANALYSIS / bridge_id / "待确认清单.json"
    ok_side = check("sidecar 待确认清单.json 存在", sidecar_path.is_file(), str(sidecar_path))
    if ok_side:
        payload = json.loads(sidecar_path.read_text(encoding="utf-8-sig"))
        rows = payload.get("rows") or []
        check("sidecar 每行都有「画面位置」新键（null 也是键）",
              bool(rows) and all("画面位置" in row for row in rows), f"{len(rows)} 行")
        located = [row["画面位置"] for row in rows if valid_pos(row.get("画面位置"))]
        check("sidecar 至少 1 行画面位置非空且合法（九资产层→sidecar 新键已产出）",
              bool(located), json.dumps([row.get("画面位置") for row in rows], ensure_ascii=False))
    cards_out = cards.build(bridge_id, local_id)
    check("cards.build 每张卡带「画面位置」字段（读侧透传）",
          bool(cards_out) and all("画面位置" in card for card in cards_out),
          f"{len(cards_out)} 张")
    return local_id, bridge_id


# ---------------------------------------------------------------- [3] 摘要三字段
def expected_summary(local_id: str) -> dict:
    t = json.loads((config.PROJECTS_DIR / local_id / "任务.json").read_text(encoding="utf-8"))
    video_name = (t.get("ingestion") or {}).get("video_name", "")
    ck_path = ANALYSIS / str(t.get("bridge_id") or "") / "待确认清单.md"
    if ck_path.is_file():
        rows = replace.parse_rows(ck_path.read_text(encoding="utf-8-sig", errors="replace"))
        filled = sum(1 for r in rows if (r.get("cur_assets") or "").strip())
        must = sum(1 for r in rows if "必须换" in (r.get("advice") or ""))
        replaced = f"{filled}/{must}" if rows else ""
    else:
        replaced = ""
    return {"task_name": f"{local_id}·{video_name}",
            "stage": str(t.get("lapian_stage") or "") if t["state"] == "discussing" else "",
            "replaced": replaced}


def probe_summary(new_task: str) -> None:
    print("3) 表格摘要三字段（task_name/stage/replaced，合同：实时计算不落盘）")
    status, body = req("/api/hub/tasks")
    tasks = body.get("tasks") if isinstance(body, dict) else None
    if not check("GET /api/hub/tasks → 200 且非空列表", status == 200 and bool(tasks), str(status)):
        return
    check("每条摘要都带 task_name/stage/replaced 三个新字段",
          all(all(k in t for k in ("task_name", "stage", "replaced")) for t in tasks),
          str([t.get("local_id") for t in tasks if "task_name" not in t][:4]))
    for local_id in (TASK2, new_task):
        if not local_id:
            continue
        got = next((t for t in tasks if t.get("local_id") == local_id), None)
        exp = expected_summary(local_id)
        check(f"{local_id} task_name = 目录名·参考片文件名",
              got is not None and got.get("task_name") == exp["task_name"],
              f"got={got and got.get('task_name')!r} exp={exp['task_name']!r}")
        check(f"{local_id} stage = discussing 时的拉片阶段（其余为空）",
              got is not None and got.get("stage") == exp["stage"],
              f"got={got and got.get('stage')!r} exp={exp['stage']!r}")
        check(f"{local_id} replaced = 已传图行数/必换行数（与清单实算对账）",
              got is not None and got.get("replaced") == exp["replaced"],
              f"got={got and got.get('replaced')!r} exp={exp['replaced']!r}")
    if new_task:
        check(f"{new_task} 拆完未传图 → replaced 应为 0/N 形态",
              re.fullmatch(r"0/\d+", exp.get("replaced") or "") is not None, exp.get("replaced", ""))


# ---------------------------------------------------------------- [4] 建议结构化
def probe_suggestions(new_task: str) -> None:
    print("4) 意图建议结构化透出（复用 intent.py）+ 缺值兜底")
    status, body = req(f"/api/hub/task/{TASK2}")
    task = body.get("task") if isinstance(body, dict) else None
    if not check(f"GET /api/hub/task/{TASK2} → 200", status == 200 and bool(task), str(status)):
        return
    sug = task.get("suggestions")
    check("get_task 带 suggestions 列表", isinstance(sug, list) and bool(sug), str(type(sug)))
    if isinstance(sug, list) and sug:
        ck_rows = replace.parse_rows(task.get("checklist_md") or "")
        check("suggestions 与当前清单逐行对齐（n 集合一致且有序）",
              [s["n"] for s in sug] == [r["no"] for r in sorted(ck_rows, key=lambda x: x["no"])],
              str([s.get("n") for s in sug]))
        shape_bad = [s for s in sug
                     if not {"n", "档位", "理由", "镜次", "待核对"} <= set(s)]
        check("每条含 n/档位/理由/镜次/待核对 五字段", not shape_bad, str(shape_bad[:2]))
        check("档位只取 intent 固定三节或空",
              all(s.get("档位") in ("", "必须换", "建议准备", "可沿用") for s in sug),
              str({s.get("档位") for s in sug}))
        check("待核对行档位恒空（禁自动归「可沿用」）",
              all(s.get("档位") == "" for s in sug if s.get("待核对")),
              str([s for s in sug if s.get("待核对") and s.get("档位")][:3]))
        filled = [s for s in sug if s.get("档位")]
        check(f"真样本有 {len(filled)} 条带档位建议（理由/镜次随行）",
              bool(filled) and all(s.get("理由") for s in filled),
              str(filled[:1]))

    # 直调兜底（不落盘的夹具）：缺文件 / 解析失败 / 行号不匹配 → 待核对，绝不归「可沿用」
    import hub_api
    ck = (ANALYSIS / BRIDGE2 / "待确认清单.md").read_text(encoding="utf-8-sig")
    dm = (ANALYSIS / BRIDGE2 / "拆解.md").read_text(encoding="utf-8-sig")
    for label, raw in (("缺文件（raw 空）", ""), ("解析失败（无固定三节）", "随手写两句话，没有节"),
                       ("行号不匹配（只给 99）", "# 必须换\n- 99｜理由：x｜镜次：1\n")):
        out = hub_api._suggestions(raw, ck, dm)
        check(f"兜底·{label} → 清单每行都「待核对」且档位空",
              len(out) == len(replace.parse_rows(ck)) and
              all(s["待核对"] and s["档位"] == "" and s["n"] >= 1 for s in out),
              f"{len(out)} 条")
    check("兜底·三案都没有自动归「可沿用」",
          not any(s["档位"] == "可沿用" for raw in ("", "无节", "# 必须换\n- 99｜理由：x｜镜次：1\n")
                  for s in hub_api._suggestions(raw, ck, dm)))

    if new_task:
        status, body = req(f"/api/hub/task/{new_task}")
        task = body.get("task") if isinstance(body, dict) else {}
        sug = task.get("suggestions") or []
        check(f"新任务 {new_task} 的 get_task 也透出结构化建议（意图非空 → 有档位行）",
              bool(sug) and any(s.get("档位") for s in sug),
              f"{len(sug)} 条")


def probe_cleanup(local_id: str, bridge_id: str) -> None:
    """AT-10 C5：探针自清理——断言全跑完后删掉本次真拆任务，回归不留任务。

    删除走生产删除合同（POST /api/hub/task/{id}/delete，confirm_name=任务号）：
    任务目录 + 专属 analysis/reverse/<bridge_id>/ + uploads 视频一并清掉，写墓碑防号复用；
    bridge 没起时直调 automation 层 delete_task（同一实现，不另写删除逻辑）。
    """
    print("5) 探针自清理：删掉本次真拆任务（回归不留任务）")
    deleted: dict = {}
    status, body = req(f"/api/hub/task/{local_id}/delete", "POST", {"confirm_name": local_id})
    via_http = status == 200 and isinstance(body, dict) and body.get("ok") is True
    if via_http:
        deleted = body
    else:
        try:
            deleted = hub_api.delete_task(local_id, confirm_name=local_id)
            via_http = bool(deleted.get("ok"))
        except Exception as exc:  # noqa: BLE001 —— 失败原文进断言，不静默
            check("删除合同执行成功（HTTP 或 automation 直调）", False,
                  f"HTTP {status} {str(body)[:120]} / 直调 {type(exc).__name__}: {exc}")
            return
    check("删除合同执行成功（状态 awaiting_assets → confirm_name 确认后删）", via_http,
          str(deleted)[:200])
    check("任务目录已删除（projects/<id> 不在盘上）", not (PROJECTS / local_id).exists(),
          str(PROJECTS / local_id))
    check("专属 analysis 产物随任务删除（不留在 analysis/reverse 下）",
          not (ANALYSIS / bridge_id).exists() if bridge_id else False, bridge_id)
    status, body = req("/api/hub/tasks")
    tasks = body.get("tasks") if isinstance(body, dict) else None
    check("列表端点已不含探针任务（GET /api/hub/tasks 无该条）",
          status == 200 and isinstance(tasks, list)
          and all(t.get("local_id") != local_id for t in tasks),
          f"{status} / {str([t.get('local_id') for t in (tasks or [])])[:160]}")
    after = {p.name for p in PROJECTS.glob("auto-*")}
    check("跑后 projects/ 任务号集合与跑前完全一致（回归零新增）",
          after == PROJECTS_BEFORE,
          f"新增={sorted(after - PROJECTS_BEFORE)} 消失={sorted(PROJECTS_BEFORE - after)}")
    try:
        tombs = json.loads((PROJECTS / ".tombstones.json").read_text(encoding="utf-8-sig"))
        tomb_ids = {str(x.get("id")) for x in tombs if isinstance(x, dict)}
    except (OSError, json.JSONDecodeError):
        tomb_ids = set()
    check("删除写了墓碑（.tombstones.json 含本次任务号，编号不复用）", local_id in tomb_ids, local_id)


def main() -> int:
    print(f"[probe-at9-b1] 目标 {BASE}；真拆会建一条新任务，跑完自清理（回归零新增）")
    new_task, new_bridge = "", ""
    try:
        probe_injection()
        new_task, new_bridge = probe_real_teardown()
        probe_summary(new_task)
        probe_suggestions(new_task)
    finally:
        if new_task:
            probe_cleanup(new_task, new_bridge)
    fails = [r for r in RESULTS if not r[1]]
    print(f"\n[probe-at9-b1] {len(RESULTS) - len(fails)}/{len(RESULTS)} PASS"
          + (f"（探针任务 {new_task} 已自清理）" if new_task else ""))
    for name, ok, detail in fails:
        print(f"  FAIL 汇总: {name} —— {detail}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
