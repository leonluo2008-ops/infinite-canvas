#!/usr/bin/env python
"""AT-7 工作台桥端探针：/api/hub/* 合同逐条断言（不动状态机的只读面 + 摄取幂等 + 守卫负例）。

跑法：wanrepro/ 下 `python -m automation.probe_hub [port]`（默认 8778 冒烟实例）。
判据全对才 exit 0；任何一步失败如实 FAIL 并 exit 1（禁子串判通过）。
注意：本探针会真实建一条任务（首投），冒烟完会删掉任务目录再复投验证 HIT——
所以要求探针前后 projects 不留垃圾（断言目录数不变）。
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from . import config  # noqa: E402
from .state import STATES as ALL_STATES  # noqa: E402

UPLOADS_DIR = config._PKG_DIR.parents[0] / "uploads"  # wanrepro/uploads

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8778
BASE = f"http://127.0.0.1:{PORT}"
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 红线：本机直连不走代理

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" —— {detail}" if detail and not cond else ""))


def req(path: str, method: str = "GET", data: bytes | None = None,
        ctype: str | None = None) -> tuple[int, dict | bytes]:
    r = urllib.request.Request(BASE + path, data=data, method=method)
    if ctype:
        r.add_header("Content-Type", ctype)
    try:
        resp = OP.open(r, timeout=90)
        raw = resp.read()
        return resp.status, (json.loads(raw) if raw[:1] in (b"{", b"[") else raw)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return e.code, raw


def multipart(fields: dict[str, str], files: dict[str, tuple[str, bytes]]) -> bytes:
    bnd = "probe7f3a9c"
    out = b""
    for k, v in fields.items():
        out += (f"--{bnd}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n").encode()
    for k, (fn, blob) in files.items():
        out += (f"--{bnd}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{fn}\"\r\n"
                f"Content-Type: application/octet-stream\r\n\r\n").encode() + blob + b"\r\n"
    return out + f"--{bnd}--\r\n".encode()


def main() -> int:
    print(f"[probe-hub] 目标 {BASE}")
    projects_before = sorted(p.name for p in config.PROJECTS_DIR.glob("auto-*")) if config.PROJECTS_DIR.is_dir() else []

    # ── 1. 只读面 ─────────────────────────────────────────────
    print("1) 任务列表/详情/成片 404")
    s, d = req("/api/hub/tasks")
    check("GET /api/hub/tasks → 200 且 tasks 是列表", s == 200 and isinstance(d.get("tasks"), list))
    if isinstance(d.get("tasks"), list) and d["tasks"]:
        first = d["tasks"][0]["local_id"]
        s2, d2 = req(f"/api/hub/task/{first}")
        t = d2.get("task", {})
        check(f"GET /api/hub/task/{first} → 200 含 state/intent",
              s2 == 200 and t.get("state") in ALL_STATES and "intent" in t)
    else:
        check("列表非空（先有一条任务再跑探针）", False, str(d)[:120])
    s, _ = req("/api/hub/task/auto-9999")
    check("任务不存在 → 409/404", s in (404, 409))
    s, _ = req("/api/hub/file/auto-9999")
    check("成片未就绪 → 409/404", s in (404, 409))

    # ── 2. 守卫负例（不花钱不动状态） ─────────────────────────
    print("2) 停点守卫")
    s, d = req("/api/hub/submit/auto-0001", "POST", b"{}")
    check("未批准 submit → 409 + 人话错误", s == 409 and "approved" in str(d.get("error", "")),
          f"{s} {str(d)[:100]}")
    # 「无可确认置换」负例只在 auto-0001 真处于 awaiting_assets 且无 pending 时成立；
    # 真实任务可能被所有者退回置换留了 pending（2026-11-10 实况）——此时盲发 confirm 会
    # 真确认推进真实任务（探针红线：真实任务只读）。先读状态，前置不满足 → 如实跳过不发包。
    s, d = req("/api/hub/task/auto-0001")
    t0001 = (d.get("task") or {}) if isinstance(d, dict) else {}
    st0001, pending0001 = t0001.get("state"), bool(t0001.get("pair_lines"))
    if st0001 == "awaiting_assets" and not pending0001:
        s, d = req("/api/hub/confirm/auto-0001", "POST", b"{}")
        check("无可确认置换 confirm → 409", s == 409, f"{s} {str(d)[:100]}")
    else:
        check(f"无可确认置换 confirm → 409（auto-0001 当前 {st0001}"
              f"{'有 pending' if pending0001 else ''}，前置不满足，不发确认防误推进）",
              True, "真实任务状态依赖断言，按当前状态如实跳过")

    # ── 3. 摄取幂等（真建一条→复投 HIT→清理） ─────────────────
    print("3) 摄取/幂等")
    video = UPLOADS_DIR / "官号参考1.mp4"
    if not video.is_file():
        check("冒烟视频存在（uploads/官号参考1.mp4）", False, str(video))
    else:
        body = multipart({"intent": "probe-hub 冒烟"}, {"video": (video.name, video.read_bytes())})
        s, d = req("/api/hub/ingest", "POST", body, "multipart/form-data; boundary=probe7f3a9c")
        created = d.get("local_id") if isinstance(d, dict) else None
        fresh = isinstance(d, dict) and d.get("result") == "OK"  # HIT=此前冒烟已建同片任务，不删
        check("首投 → 200 且 local_id/bridge_id", s == 200 and created and d.get("bridge_id"),
              f"{s} {str(d)[:120]}")
        if created:
            s2, d2 = req("/api/hub/ingest", "POST", body, "multipart/form-data; boundary=probe7f3a9c")
            check("复投 → HIT 同任务（视频 sha 幂等）",
                  s2 == 200 and d2.get("result") == "HIT" and d2.get("local_id") == created,
                  f"{s2} {str(d2)[:120]}")
            # 只清本次新建的冒烟任务（HIT 命中的老任务一个不动）
            import shutil
            if fresh:
                shutil.rmtree(config.PROJECTS_DIR / created, ignore_errors=True)
    projects_after = sorted(p.name for p in config.PROJECTS_DIR.glob("auto-*")) if config.PROJECTS_DIR.is_dir() else []
    check("探针不留垃圾（projects 目录集不变）", projects_before == projects_after,
          f"before={projects_before} after={projects_after}")

    # ── 4. job 面 ─────────────────────────────────────────────
    s, _ = req("/api/hub/job/deadbeef")
    check("job 不存在 → 409/404", s in (404, 409))

    fails = [r for r in RESULTS if not r[1]]
    print(f"\n[probe-hub] {len(RESULTS) - len(fails)}/{len(RESULTS)} PASS")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
