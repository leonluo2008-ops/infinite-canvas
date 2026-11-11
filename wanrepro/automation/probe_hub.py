#!/usr/bin/env python
"""AT-7 工作台桥端探针：/api/hub/* 合同逐条断言（不动状态机的只读面 + 摄取幂等 + 守卫负例）。

跑法：wanrepro/ 下 `python -m automation.probe_hub [port]`（默认 8778 冒烟实例）。
判据全对才 exit 0；任何一步失败如实 FAIL 并 exit 1（禁子串判通过）。
注意：本探针会真实建一条任务（首投），冒烟完会删掉**本次新建**的任务目录再复投验证 HIT——
所以要求探针前后 projects 不留垃圾（断言目录集不变）。

**数据根守卫（AT-14 B2 P0，先于一切写操作）**：
  0) 先 GET /api/hub/where 让服务端自述 TASKS_DIR，断言它 == 本探针的 config.PROJECTS_DIR；
     不一致 / 端点不可用（服务端版本过旧）→ **拒跑 exit 1**——读/写分家的伪隔离会把自建
     id 删成真实任务（真实任务 auto-0001 就是这么没的）。
  1) 跑前给 projects 拍快照；清理只走 probe_guard.cleanup_created（本次新建 ∧ 跑前不存在
     ∧ 项目根直系），本文件不再出现裸 rmtree。
  2) 跑后断言项目集一格不少（⊆ 跑前）+ 不留垃圾（== 跑前）。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
from . import config  # noqa: E402
from .probe_guard import (  # noqa: E402
    GuardError,
    StubBridge,
    assert_no_loss,
    assert_same_root,
    cleanup_created,
    free_port,
    http_req,
    multipart_body,
    projects_snapshot,
)
from .state import STATES as ALL_STATES  # noqa: E402

UPLOADS_DIR = config._PKG_DIR.parents[0] / "uploads"  # wanrepro/uploads


def _argv_port() -> int:
    for a in sys.argv[1:]:
        if a.isdigit():
            return int(a)
    return 8778


PORT = _argv_port()
BASE = f"http://127.0.0.1:{PORT}"

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" —— {detail}" if detail and not cond else ""))


def req(path: str, method: str = "GET", data: bytes | None = None,
        ctype: str | None = None) -> tuple[int, dict | bytes]:
    return http_req(BASE, path, method, data, ctype)


def multipart(fields: dict[str, str], files: dict[str, tuple[str, bytes]]) -> bytes:
    body, _ctype = multipart_body(fields, files)
    return body


def main() -> int:
    print(f"[probe-hub] 目标 {BASE}")
    # ── 0. 数据根同源（拒跑守卫：先于任何写操作） ─────────────
    print("0) 数据根同源")
    s, w = req("/api/hub/where")
    try:
        assert_same_root(w if isinstance(w, dict) else {}, config.PROJECTS_DIR)
        check("服务端自述 TASKS_DIR == 探针 PROJECTS_DIR（读写同源）", s == 200,
              f"{s} {str(w)[:160]}")
        print(f"     同源确认：{w.get('tasks_dir')}")
    except GuardError as err:
        check("服务端自述 TASKS_DIR == 探针 PROJECTS_DIR（读写同源）", False, f"{s} {err}")
        print("\n[probe-hub] 拒跑：数据根不同源，后续写入/清理断言一律不执行"
              "（防把自建 id 删成真实任务）。旧版服务端需重启后才有 /api/hub/where。")
        print(f"\n[probe-hub] 0/{len(RESULTS)} PASS")
        return 1

    projects_before = projects_snapshot(config.PROJECTS_DIR)
    print(f"     跑前 projects：{sorted(projects_before)}")
    created_ids: set[str] = set()      # 只有「本次新建」才进可删集

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
        if created and fresh:
            created_ids.add(created)
        check("首投 → 200 且 local_id/bridge_id", s == 200 and created and d.get("bridge_id"),
              f"{s} {str(d)[:120]}")
        if created:
            s2, d2 = req("/api/hub/ingest", "POST", body, "multipart/form-data; boundary=probe7f3a9c")
            check("复投 → HIT 同任务（视频 sha 幂等）",
                  s2 == 200 and d2.get("result") == "HIT" and d2.get("local_id") == created,
                  f"{s2} {str(d2)[:120]}")
            # 只清本次新建的冒烟任务（HIT 命中的老任务一个不动；防线在 cleanup_created 里）
            deleted, why = cleanup_created(config.PROJECTS_DIR, created,
                                           before=projects_before, created=created_ids)
            print(f"     [清理] {why}")
            check("清理只碰本次新建（跑前已存在的任务一律拒绝删除）",
                  deleted == (created not in projects_before), why)

    # ── 3.5 真实任务零丢失 ────────────────────────────────────
    projects_after = projects_snapshot(config.PROJECTS_DIR)
    try:
        assert_no_loss(projects_before, projects_after)
        check("真实任务零丢失（跑后 ⊆ 跑前）", True)
    except GuardError as err:
        check("真实任务零丢失（跑后 ⊆ 跑前）", False, str(err))
    check("探针不留垃圾（projects 目录集不变）", projects_before == projects_after,
          f"before={sorted(projects_before)} after={sorted(projects_after)}")

    # ── 4. job 面 ─────────────────────────────────────────────
    s, _ = req("/api/hub/job/deadbeef")
    check("job 不存在 → 409/404", s in (404, 409))

    fails = [r for r in RESULTS if not r[1]]
    print(f"\n[probe-hub] {len(RESULTS) - len(fails)}/{len(RESULTS)} PASS")
    return 1 if fails else 0


def _wait_where(base: str, proc: subprocess.Popen, timeout: float = 90.0) -> dict:
    deadline = time.time() + timeout
    last = f"{base} 无响应"
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"隔离实例提前退出 rc={proc.returncode}")
        s, d = http_req(base, "/api/hub/where", timeout=5)
        if s == 200 and isinstance(d, dict):
            return d
        last = f"{s} {str(d)[:120]}"
        time.sleep(0.7)
    raise RuntimeError(f"隔离实例 {timeout:.0f}s 内没就绪：{last}")


def run_isolated() -> int:
    """`--isolated`：探针自己起临时实例（临时 WANREPRO_AUTO_DIR + 假 bridge）再跑全部断言。

    为什么要有这个模式：手工「设 env 起实例 + 另起探针」正是 auto-0001 事故的形状——
    服务端与探针的数据根只要有一点不同源，自建 id 就落到真实任务上。让探针自己起实例，
    端口/目录/bridge 全由探针掌握，真实 projects 只作只读基线。
    """
    real_root = Path(config.PROJECTS_DIR)
    real_before = projects_snapshot(real_root)
    iso_root = Path(tempfile.mkdtemp(prefix="wanprobe-hub-",
                                     dir=os.environ.get("TMPDIR") or tempfile.gettempdir()))
    iso_projects = iso_root / "projects"
    iso_projects.mkdir(parents=True, exist_ok=True)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    stub = StubBridge()
    env = dict(os.environ, WANREPRO_AUTO_DIR=str(iso_projects), WANREPRO_BRIDGE=stub.base)
    proc: subprocess.Popen | None = None
    print(f"[probe-hub] 自隔离模式：实例 {base}，数据根 {iso_projects}，假 bridge {stub.base}")
    try:
        stub.thread.start()
        proc = subprocess.Popen(
            [sys.executable, str(_ROOT / "ui" / "server.py"), "--port", str(port), "--no-browser"],
            cwd=str(_ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        )
        try:
            where = _wait_where(base, proc)
        except RuntimeError as err:
            print(f"  [FAIL] 隔离实例起不来：{err}")
            print(f"\n[probe-hub] 0/1 PASS")
            return 1
        print(f"     实例自述 tasks_dir={where.get('tasks_dir')}")

        # 先种一条任务（列表非空断言需要；也让本探针的首投走「真新建」路径）
        seed = next((p for p in sorted(UPLOADS_DIR.glob("*.mp4")) if p.name != "官号参考1.mp4"), None)
        if seed is None:
            seed = UPLOADS_DIR / "官号参考1.mp4"
            print("     [注意] uploads 里只有冒烟源片，种子任务=同片（本探针首投将走 HIT 路径）")
        body, ctype = multipart_body({"intent": "probe-hub 种子任务"},
                                     {"video": (seed.name, seed.read_bytes())})
        s, d = http_req(base, "/api/hub/ingest", "POST", body, ctype, timeout=180)
        seeded = d.get("local_id") if isinstance(d, dict) else None
        if s != 200 or not seeded:
            print(f"  [FAIL] 种任务失败 {s} {str(d)[:200]}")
            print(f"\n[probe-hub] 0/1 PASS")
            return 1
        print(f"     种子任务 {seeded}（源片 {seed.name}）")

        globals()["BASE"] = base
        config.PROJECTS_DIR = iso_projects          # 探针的读取根 = 服务端的写入根
        rc = main()
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        stub.httpd.shutdown()
        stub.httpd.server_close()
        shutil.rmtree(iso_root, ignore_errors=True)

    real_after = projects_snapshot(real_root)
    ok = real_after == real_before
    print(f"[probe-hub] 真实 projects {'未动' if ok else '有变化'}："
          f"before={sorted(real_before)} after={sorted(real_after)}")
    return rc if ok else 1


if __name__ == "__main__":
    if "--isolated" in sys.argv:
        raise SystemExit(run_isolated())
    raise SystemExit(main())
