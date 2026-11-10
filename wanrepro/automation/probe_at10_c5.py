#!/usr/bin/env python
"""AT-10 C5 收口探针：幂等命中「有反馈」+ 探针任务自清理。

跑法：wanrepro/ 下 `python -m automation.probe_at10_c5 [port]`（默认 8776 常驻工作台；
判据全对才 exit 0，任何一步失败如实 FAIL 并 exit 1——判 returncode，禁子串判通过）。

两组判据（docs/nodes/AT-10.md §三 C5 行）：
  [1] 后端命中形状（真 HTTP，真 multipart）：自建夹具任务（projects/auto-9188 + 同内容视频）
      投 POST /api/hub/ingest → 必须回 {local_id=夹具号, result="HIT", hit_existing=true,
      bridge_id=夹具桥号, video_name, note 含「未重复建任务」}；重复投第二次仍命中同一条
      （幂等稳定）；两次都**零新建**（projects 目录集合、/api/hub/tasks 列表集合除夹具外不变）。
      缺 video 字段仍 400（端点校验没被命中分支吃掉）。
  [2] 前端判定/接线：cd web && node scripts/probe-at10-c5.mjs（esbuild 打包 ingest-hit.ts 断言
      命中判定与文案；源码护栏：两个入口都「先判命中再触发拆解」、命中提前 return、命中回报
      onCreated(localId, true)、表格视图命中时 setOpenId 打开详情）。
夹具自建自清（跑完删 projects/auto-9188），探针不依赖真实任务、不留任务、不留临时文件。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]          # wanrepro/
REPO = ROOT.parent                                  # 仓库根（web/ 在这）
sys.path.insert(0, str(ROOT))

from automation import config, ingest  # noqa: E402

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8776
BASE = f"http://127.0.0.1:{PORT}"
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 红线：本机直连不走代理
PROJECTS = config.PROJECTS_DIR
FIX_ID, FIX_BRIDGE, FIX_VIDEO = "auto-9188", "probe-c5-hit", "probe-c5-hit.mp4"

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
    return _open(r)


def _open(r: urllib.request.Request) -> tuple[int, dict | bytes]:
    try:
        resp = OP.open(r, timeout=120)
        raw = resp.read()
        ctype = resp.headers.get("Content-Type") or ""
        return resp.status, (json.loads(raw) if "json" in ctype else raw)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return e.code, raw


def post_multipart(fields: dict[str, str], files: dict[str, tuple[str, bytes]]) -> tuple[int, dict | bytes]:
    """手搓 multipart（服务端 parse_multipart：video=文件字段、intent=文本字段）。"""
    boundary = f"----probe-at10c5-{uuid.uuid4().hex[:12]}"
    out = bytearray()
    for k, v in fields.items():
        out += f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    for k, (name, blob) in files.items():
        out += (f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; '
                f'filename="{name}"\r\nContent-Type: video/mp4\r\n\r\n').encode()
        out += blob + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    r = urllib.request.Request(BASE + "/api/hub/ingest", data=bytes(out), method="POST")
    r.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    return _open(r)


def task_ids() -> set[str]:
    return {p.name for p in PROJECTS.glob("auto-*") if p.is_dir()}


def api_task_ids() -> set[str] | None:
    status, body = req("/api/hub/tasks")
    if status != 200 or not isinstance(body, dict) or not isinstance(body.get("tasks"), list):
        return None
    return {str(t.get("local_id")) for t in body["tasks"]}


def build_fixture() -> bytes:
    """自建夹具任务：projects/auto-9188/{任务.json, inbox/<视频>}，视频内容逐次唯一。"""
    tdir = PROJECTS / FIX_ID
    (tdir / "inbox").mkdir(parents=True, exist_ok=True)
    blob = (f"probe-at10-c5-fixture-{time.time()}-{uuid.uuid4().hex}".encode() * 64)
    video = tdir / "inbox" / FIX_VIDEO
    video.write_bytes(blob)
    (tdir / "任务.json").write_text(json.dumps({
        "local_id": FIX_ID,
        "bridge_id": FIX_BRIDGE,
        "created_at": "2026-11-10T10:00:00+08:00",
        "state": "ingested",
        "history": [],
        "intent": "AT-10 C5 探针夹具（自建，跑完删）",
        "assets": [],
        "ingestion": {"zip_sha256": ingest._sha256_file(video), "zip_name": "probe-at10-c5",
                      "video_name": FIX_VIDEO, "text_name": "", "ignored": []},
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return blob


def probe_hit_shape() -> None:
    print("1) 幂等命中 HTTP 形状（同视频重投 → 回已有任务号 + 零新建）")
    before_dirs = task_ids()
    before_api = api_task_ids()
    if not check("列表端点可用（GET /api/hub/tasks → 200）", before_api is not None, str(before_api)):
        return
    check("夹具任务号 auto-9188 跑前不存在（不覆盖真实任务）", FIX_ID not in before_dirs, str(sorted(before_dirs)))
    blob = build_fixture()
    after_fixture = task_ids()
    check("夹具任务已就位（projects/auto-9188/任务.json）",
          (PROJECTS / FIX_ID / "任务.json").is_file() and after_fixture == before_dirs | {FIX_ID},
          str(sorted(after_fixture - before_dirs)))

    status, body = post_multipart({"intent": "AT-10 C5 幂等命中探针"}, {"video": (FIX_VIDEO, blob)})
    if not check("POST /api/hub/ingest（同内容视频）→ 200", status == 200 and isinstance(body, dict),
                 f"{status} {str(body)[:200]}"):
        return
    detail = json.dumps(body, ensure_ascii=False)[:220]
    check("回带已有任务号（local_id=auto-9188）", body.get("local_id") == FIX_ID, detail)
    check("回带 result=HIT（旧字段兼容）", str(body.get("result") or "").upper() == "HIT", detail)
    check("回带 hit_existing=true（C5 显式标记，前端据此判命中）", body.get("hit_existing") is True, detail)
    check("回带已有任务的 bridge_id（不是新建的桥号）", str(body.get("bridge_id") or "") == FIX_BRIDGE, detail)
    check("回带 video_name（提示里能点名是哪条片）", str(body.get("video_name") or "") == FIX_VIDEO, detail)
    check("note 明说「未重复建任务」", "未重复建任务" in str(body.get("note") or ""), detail)
    check("返回字段齐全（local_id/bridge_id/result/hit_existing/video_name/note）",
          {"local_id", "bridge_id", "result", "hit_existing", "video_name", "note"} <= set(body),
          str(sorted(body)))
    check("命中不新建任务目录（projects 集合仍 = 跑前 + 夹具）", task_ids() == before_dirs | {FIX_ID},
          str(sorted(task_ids() - before_dirs - {FIX_ID})))
    check("命中不在列表里多出一条（tasks 集合仍 = 跑前 + 夹具）", api_task_ids() == before_api | {FIX_ID},
          str(sorted((api_task_ids() or set()) - before_api - {FIX_ID})))

    status2, body2 = post_multipart({"intent": "再投一次"}, {"video": (FIX_VIDEO, blob)})
    check("重复投第二次仍命中同一条（幂等稳定）",
          status2 == 200 and isinstance(body2, dict)
          and body2.get("local_id") == FIX_ID and body2.get("hit_existing") is True,
          f"{status2} {str(body2)[:160]}")
    check("第二次也零新建", task_ids() == before_dirs | {FIX_ID},
          str(sorted(task_ids() - before_dirs - {FIX_ID})))

    status3, body3 = post_multipart({"intent": "无视频字段"}, {})
    check("缺 video 字段 → 400（命中分支没吃掉端点校验）", status3 == 400,
          f"{status3} {str(body3)[:160]}")


def probe_frontend() -> None:
    print("2) 前端判定与接线（esbuild 打包 ingest-hit.ts + 源码护栏）")
    proc = subprocess.run(["node", "scripts/probe-at10-c5.mjs"], cwd=str(REPO / "web"),
                          capture_output=True, text=True, timeout=300)
    tail = (proc.stdout or "").strip().splitlines()
    check("web/scripts/probe-at10-c5.mjs 全绿（0 失败）",
          proc.returncode == 0 and bool(tail) and " FAIL " not in (proc.stdout or ""),
          f"rc={proc.returncode} {(proc.stderr or '')[-200:]}")
    if tail:
        for line in tail:
            if line.startswith("[probe") or line.strip().startswith("FAIL"):
                print(f"    {line.strip()}")


def cleanup_fixture() -> None:
    shutil.rmtree(PROJECTS / FIX_ID, ignore_errors=True)


def main() -> int:
    print(f"[probe-at10-c5] 目标 {BASE}；夹具 {FIX_ID} 自建自清")
    try:
        probe_hit_shape()
    finally:
        cleanup_fixture()
    after = task_ids()
    status, body = req("/api/hub/tasks")
    ids = {str(t.get("local_id")) for t in (body.get("tasks") or [])} if isinstance(body, dict) else set()
    check("探针自清理：夹具任务目录已删（projects 无 auto-9188）", FIX_ID not in after, str(sorted(after)))
    check("探针自清理：列表端点也已无夹具条目", FIX_ID not in ids, str(sorted(ids))[:200])
    probe_frontend()
    fails = [r for r in RESULTS if not r[1]]
    print(f"\n[probe-at10-c5] {len(RESULTS) - len(fails)}/{len(RESULTS)} PASS"
          + ("" if fails else "（夹具已清理）"))
    for name, ok, detail in fails:
        print(f"  FAIL 汇总: {name} —— {detail}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
