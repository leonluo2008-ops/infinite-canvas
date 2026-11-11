#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AT-16 B6 后端探针：**删除确认简化后的合同**（缺陷 D7 的后端半边）。

跑法：wanrepro/ 下 `python -m automation.probe_at16_b6`。

前端 B6 把「输任务名」的 Modal 换成 Popconfirm 二键确认（危险红钮），`confirm_name` 改为
组件自动带当前任务号。本探针锁住**桥端合同没有跟着被放宽**，以及前端那条请求形状真能删：

  [0] 隔离与同源：tempfile 造隔离根 → 补丁 config.PROJECTS_DIR 后才 import hub_api
      （TASKS_DIR 在 import 期求值）；起真实隔离服务端（ui/server.py + 假 bridge），
      /api/hub/where 走 assert_same_root 证明读写同源，不同源一律拒跑后续写入。
  [1] 前端形态能删（awaiting_assets / awaiting_approval）：
      POST body = {"confirm_name": "<local_id>"}（= web/src/pages/hub/hub-api.ts deleteHubTask 的实参形状）
      → 200，任务目录消失、墓碑记下任务号（防任务号复用）。
  [2] 合同没放宽：同一个 awaiting_approval 任务，body 不带 confirm_name
      → 409「拆解过的任务删除需任务名确认」，目录逐字节留着（二键确认≠取消后端校验）。
      旧前端形态（confirm_name = 完整任务名）仍 200（合同向后兼容，没被这次改动改窄）。
  [3] 运行态照旧拒：discussing + confirm_name → 409「任务运行中…删除拒绝」，目录留着。
  [4] 免确认态照旧：done / ingested 不带 confirm_name 也能删（后端行为零变化）。
  [5] 只有 POST 能删：GET /api/hub/task/<id>/delete → 非 200 且目录在
      （前端「取消」不发请求，后端也没有第二条删除入口）。
  [6] 真实任务零丢失 + 探针不留垃圾（真实 projects 目录集前后一致；隔离夹具只删自建）。

夹具只用 auto-9831..auto-9836（隔离根内），跑完连根删。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]          # wanrepro/
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ui"))                 # hub_api.py 在 ui/ 下（与 probe_hub 同法）

from automation import config, probe_guard  # noqa: E402

RESULTS: list[tuple[str, bool]] = []

TASK_ASSETS = "auto-9831"        # awaiting_assets：前端形态直删
TASK_APPROVAL = "auto-9832"      # awaiting_approval：无 confirm_name → 拒；带任务名 → 删
TASK_RUNNING = "auto-9833"       # discussing：运行态拒删
TASK_DONE = "auto-9834"          # done：免确认
TASK_INGESTED = "auto-9835"      # ingested：进程内免确认
TASK_GET = "auto-9836"           # GET 删除探测（非 200）
FIXTURE_IDS = (TASK_ASSETS, TASK_APPROVAL, TASK_RUNNING, TASK_DONE, TASK_INGESTED, TASK_GET)


def check(name: str, cond: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" —— {detail}" if detail and not cond else ""))
    return bool(cond)


def summary() -> int:
    fails = [n for n, ok in RESULTS if not ok]
    print(f"\n[probe-at16-b6] {len(RESULTS) - len(fails)}/{len(RESULTS)} PASS")
    for n in fails:
        print(f"  FAIL {n}")
    return 1 if fails else 0


def make_task(local_id: str, state_name: str, video_name: str = "") -> Path:
    """隔离根内的任务夹具（与 probe_at16_b5b 同形；不挂 bridge_id，避免碰真实 analysis 目录）。"""
    tdir = Path(config.PROJECTS_DIR) / local_id
    shutil.rmtree(tdir, ignore_errors=True)
    (tdir / "inbox").mkdir(parents=True, exist_ok=True)
    task = {
        "local_id": local_id,
        "bridge_id": "",
        "created_at": "2026-01-01T00:00:00+08:00",
        "state": state_name,
        "history": [{"at": "2026-01-01T00:00:00+08:00", "from": None, "to": state_name,
                     "note": "probe-at16-b6 夹具"}],
        "intent": "AT-16 B6 删除确认探针夹具",
        "assets": [],
        "ingestion": {"zip_sha256": f"probe-{local_id}", "zip_name": "probe",
                      "video_name": video_name, "text_name": "", "ignored": []},
    }
    (tdir / "任务.json").write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
    return tdir


def post_delete(base: str, local_id: str, body: dict) -> tuple[int, dict]:
    """按**前端真实请求形状**打删除端点（hub-api.ts：JSON body {"confirm_name": …}）。"""
    code, payload = probe_guard.http_req(
        base, f"/api/hub/task/{local_id}/delete", method="POST",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"), ctype="application/json")
    return code, payload if isinstance(payload, dict) else {"raw": payload}


def main() -> int:
    real_root = Path(config.PROJECTS_DIR)
    real_before = probe_guard.projects_snapshot(real_root)

    iso_root = Path(tempfile.mkdtemp(prefix="wanprobe-at16b6-",
                                     dir=os.environ.get("TMPDIR") or tempfile.gettempdir()))
    iso_projects = iso_root / "projects"
    iso_projects.mkdir(parents=True, exist_ok=True)
    stub = probe_guard.StubBridge()
    port = probe_guard.free_port()
    base = f"http://127.0.0.1:{port}"
    proc = None
    try:
        # 读写同源：先补丁 config 再懒加载 hub_api（TASKS_DIR 在 import 期求值）
        config.PROJECTS_DIR = iso_projects
        import hub_api  # noqa: E402  —— 必须晚于 config 补丁，否则 TASKS_DIR 指真实根

        check("[0] 隔离根就位：hub_api.TASKS_DIR == 探针隔离根（import 期求值生效）",
              Path(hub_api.TASKS_DIR).resolve() == iso_projects.resolve(),
              f"hub_api.TASKS_DIR={hub_api.TASKS_DIR} 隔离根={iso_projects}")

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
            code, payload = probe_guard.http_req(base, "/api/hub/where", timeout=5)
            if code == 200 and isinstance(payload, dict):
                where = payload
                break
            time.sleep(0.3)
        try:
            probe_guard.assert_same_root(where or {}, iso_projects)
            check("[0] 隔离服务端起来且读写同源（/api/hub/where == 探针根）", where is not None,
                  f"{base} 无响应" if where is None else str(where)[:120])
            print(f"     数据根 {where.get('tasks_root') or where.get('projects') or where}")
        except probe_guard.GuardError as err:
            check("[0] 隔离服务端起来且读写同源（/api/hub/where == 探针根）", False, str(err))
            print("[probe-at16-b6] 拒跑：数据根不同源，后续写入 / 清理一律不执行。")
            return summary()

        # ── 夹具 ────────────────────────────────────────────
        t_assets = make_task(TASK_ASSETS, "awaiting_assets")
        t_approval = make_task(TASK_APPROVAL, "awaiting_approval", video_name="probe-b6.mp4")
        t_running = make_task(TASK_RUNNING, "discussing")
        t_done = make_task(TASK_DONE, "done")
        t_ingested = make_task(TASK_INGESTED, "ingested")
        t_get = make_task(TASK_GET, "awaiting_assets")
        full_name = f"{TASK_APPROVAL}·probe-b6.mp4"

        # ── [1] 前端形态直删 ─────────────────────────────────
        print("1) 前端形态（body={\"confirm_name\": 任务号}）能删 awaiting_assets / awaiting_approval")
        code, payload = post_delete(base, TASK_ASSETS, {"confirm_name": TASK_ASSETS})
        check("[1] awaiting_assets + confirm_name=任务号 → 200", code == 200,
              f"code={code} payload={json.dumps(payload, ensure_ascii=False)[:200]}")
        check("[1] 删除成功：任务目录已消失", not t_assets.exists(), str(t_assets))
        tombstones = iso_projects / ".tombstones.json"
        tomb_text = tombstones.read_text(encoding="utf-8") if tombstones.is_file() else ""
        check("[1] 已写墓碑防任务号复用", TASK_ASSETS in tomb_text, tomb_text[:200])
        code, payload = post_delete(base, TASK_APPROVAL, {"confirm_name": TASK_APPROVAL})
        check("[1] awaiting_approval + confirm_name=任务号 → 200", code == 200,
              f"code={code} payload={json.dumps(payload, ensure_ascii=False)[:200]}")
        check("[1] awaiting_approval 删除成功：目录已消失", not t_approval.exists())

        # ── [2] 合同没放宽 ───────────────────────────────────
        print("2) 合同没放宽：不带 confirm_name 仍被拒；旧形态（完整任务名）仍兼容")
        t_approval2 = make_task(TASK_APPROVAL, "awaiting_approval", video_name="probe-b6.mp4")
        before_bytes = (t_approval2 / "任务.json").read_bytes()
        code, payload = post_delete(base, TASK_APPROVAL, {"confirm_name": None})
        err = str(payload.get("error") or "")
        check("[2] awaiting_approval + confirm_name=null → 409 且说明要任务名确认",
              code == 409 and "需任务名确认" in err, f"code={code} err={err[:200]}")
        check("[2] 被拒时任务目录逐字节不变（不是删一半）",
              t_approval2.exists() and (t_approval2 / "任务.json").read_bytes() == before_bytes)
        code, payload = post_delete(base, TASK_APPROVAL, {"confirm_name": full_name})
        check("[2] 旧形态（confirm_name=完整任务名）仍 200 —— 合同向后兼容、没被改窄",
              code == 200 and not t_approval2.exists(),
              f"code={code} payload={json.dumps(payload, ensure_ascii=False)[:200]}")

        # ── [3] 运行态照旧拒 ─────────────────────────────────
        print("3) 运行态照旧拒绝（前端禁用 + 桥端 409 两道门都在）")
        running_bytes = (t_running / "任务.json").read_bytes()
        code, payload = post_delete(base, TASK_RUNNING, {"confirm_name": TASK_RUNNING})
        err = str(payload.get("error") or "")
        check("[3] discussing + confirm_name=任务号 → 409「任务运行中…删除拒绝」",
              code == 409 and "运行中" in err and "删除拒绝" in err, f"code={code} err={err[:200]}")
        check("[3] 被拒时任务目录逐字节不变",
              t_running.exists() and (t_running / "任务.json").read_bytes() == running_bytes)

        # ── [4] 免确认态照旧 ─────────────────────────────────
        print("4) 免确认态照旧：done / ingested 不带 confirm_name 也能删（后端行为零变化）")
        code, payload = post_delete(base, TASK_DONE, {"confirm_name": None})
        check("[4] done 免确认 → 200", code == 200 and not t_done.exists(),
              f"code={code} payload={json.dumps(payload, ensure_ascii=False)[:200]}")
        out = hub_api.delete_task(TASK_INGESTED, confirm_name=None)
        check("[4] 进程内 ingested 免确认 → ok=True 且目录消失",
              bool(out.get("ok")) and not t_ingested.exists(), str(out)[:200])

        # ── [5] 只有 POST 能删 ───────────────────────────────
        print("5) 只有 POST 能删（取消=不发请求，后端没有第二条删除入口）")
        get_bytes = (t_get / "任务.json").read_bytes()
        code, payload = probe_guard.http_req(base, f"/api/hub/task/{TASK_GET}/delete", method="GET")
        check("[5] GET /api/hub/task/<id>/delete → 非 200", code != 200, f"code={code} payload={payload}")
        check("[5] GET 打过去任务目录仍在（不可能靠 GET 误删）",
              t_get.exists() and (t_get / "任务.json").read_bytes() == get_bytes)

        # ── [6] 真实任务零丢失 + 不留垃圾 ────────────────────
        print("6) 真实数据根对账 + 夹具只删自建")
        real_after = probe_guard.projects_snapshot(real_root)
        try:
            probe_guard.assert_no_loss(real_before, real_after)
            check("[6] 真实项目目录集前后一致（零丢失 + 探针不留垃圾）", real_after == real_before,
                  f"多出 {sorted(set(real_after) - set(real_before))}" if real_after != real_before else "")
        except probe_guard.GuardError as err:
            check("[6] 真实项目目录集前后一致（零丢失 + 探针不留垃圾）", False, str(err))
        # 只清还活着的夹具（被删除端点真删掉的目录已不存在，probe_guard 会如实拒删）
        for fid in [name for name in FIXTURE_IDS if (iso_projects / name).is_dir()]:
            ok, why = probe_guard.cleanup_created(iso_projects, fid, before=set(), created=set(FIXTURE_IDS))
            if not ok:
                print(f"     清理异常：{why}")
        check("[6] 隔离根夹具全部清干净", not any((iso_projects / fid).exists() for fid in FIXTURE_IDS))
        return summary()
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


if __name__ == "__main__":
    raise SystemExit(main())
