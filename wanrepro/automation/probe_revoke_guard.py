#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AT-16 探针：revoke 后端守卫（revoke_approval 与 start_submit 同源阻断判定）。

跑法：wanrepro/ 下 `python -m automation.probe_revoke_guard`。

背景缺陷：`ui/hub_api.py` 的 revoke_approval 只持任务锁直调 state.reject_back_to_approval，
无任何提交链阻断守卫；而 reject_back_to_approval 只校验 state==approved，然后连根清掉
approval + SUBMIT_RUNTIME_KEYS（含 submit_unknown / dispatch / cloud_task_id）。于是任务
已 approved 且带 submit_unknown 或 dispatch=prepared/dispatched 时，一次 revoke 就把
「未知收单先核对」的阻断标记清掉 → 之后可直接重提 → **重复付费提交**。
前端起态门只是 UI 层，API 必须自守。

修复后断言（阻断判定收口到 hub_api._submit_block_kind，start_submit / revoke_approval 共用）：

  [0] 隔离与同源：先快照真实 PROJECTS_DIR，tempfile 造隔离根，改 config.PROJECTS_DIR 后才
      import hub_api（TASKS_DIR 在 import 期求值），断言 hub_api.TASKS_DIR == 隔离根；
      另起真实隔离服务端（ui/server.py + 假 bridge），/api/hub/where 走 assert_same_root
      证明读写同源，不同源拒跑后续写入（防误写/误删真实任务）。
  [1] unknown：approved + submit_unknown → hub_api.revoke_approval 抛 HubError 且消息含
      「未知收单」；任务文件与调用前**逐字节相等**（submit_unknown/approval/state 全保留）。
  [2] dispatch=prepared：拒（消息含 dispatch=prepared），文件不变。
  [3] dispatch=dispatched：拒，文件不变。
  [4] active：HUB_JOBS 里塞一条该 local_id 的 running 记录 → 拒（消息含「提交在跑」），
      文件不变；断言后清掉这条探针自塞的 HUB_JOBS 记录。
  [5] 允许态（approved、无任何阻断，带 approval/approval_snapshot/assets_confirmed/
      gen_params/gen_channel + 提示词.md）→ revoke 成功返回 {ok:true, state:'awaiting_approval'}；
      任务文件 state=awaiting_approval、approval/approval_snapshot 已清、
      assets_confirmed/gen_params/gen_channel 保留、提示词.md 仍在、
      history 末条 from=approved to=awaiting_approval。
  [6] 非 approved 起态（awaiting_approval / generating / ingested 各一）→ 拒（消息含
      「撤销批准需 approved」），文件不变。
  [7] HTTP 面（证明「API 级绕过」已封）：对 unknown 夹具 POST /api/hub/task/<id>/revoke
      → 409 且 error 含「未知收单」、文件不变；对允许态夹具 POST → 200 且 state=awaiting_approval。
  [8] 真实任务零丢失 + 探针不留垃圾（真实项目目录集前后一致），隔离夹具只删自建
      （cleanup_created）。

夹具任务只用 auto-9801..auto-9808（隔离根内，跑完连根删；避开其它探针的 9701/9512 段）；
真实任务零写入，跑前跑后对账。
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
TASK_UNKNOWN = "auto-9801"        # approved + submit_unknown（[1] 进程内 + [7] HTTP 复用）
TASK_PREPARED = "auto-9802"       # approved + dispatch={phase:'prepared'}
TASK_DISPATCHED = "auto-9803"     # approved + dispatch={phase:'dispatched'}
TASK_ACTIVE = "auto-9804"         # approved + HUB_JOBS running
TASK_ALLOWED = "auto-9805"        # approved 全量合法夹具（[5] 进程内 revoke）
TASK_WAITING = "auto-9806"        # awaiting_approval（[6]）
TASK_GENERATING = "auto-9807"     # generating（[6]）
TASK_INGESTED = "auto-9808"       # ingested（[6]）
TASK_HTTP_OK = "auto-9809"        # approved 全量合法夹具（[7] HTTP revoke 成功）
FIXTURE_IDS = (TASK_UNKNOWN, TASK_PREPARED, TASK_DISPATCHED, TASK_ACTIVE, TASK_ALLOWED,
               TASK_WAITING, TASK_GENERATING, TASK_INGESTED, TASK_HTTP_OK)


def check(name: str, cond: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" —— {detail}" if detail and not cond else ""))
    return bool(cond)


def summary() -> int:
    fails = [n for n, ok in RESULTS if not ok]
    print(f"\n[probe-revoke-guard] {len(RESULTS) - len(fails)}/{len(RESULTS)} PASS")
    for n in fails:
        print(f"  FAIL {n}")
    return 1 if fails else 0


def make_task(local_id: str, state_name: str, bridge_id: str, **extra) -> Path:
    """隔离根内的任务夹具（与其它探针同形，跑完连根删）。"""
    tdir = Path(config.PROJECTS_DIR) / local_id
    shutil.rmtree(tdir, ignore_errors=True)
    (tdir / "inbox").mkdir(parents=True, exist_ok=True)
    task = {
        "local_id": local_id, "bridge_id": bridge_id,
        "created_at": "2026-01-01T00:00:00+08:00", "state": state_name,
        "history": [{"at": "2026-01-01T00:00:00+08:00", "from": None, "to": state_name,
                     "note": "probe-revoke-guard 夹具"}],
        "intent": "AT-16 revoke 守卫探针夹具", "assets": [],
        "ingestion": {"zip_sha256": f"probe-{local_id}", "zip_name": "probe",
                      "video_name": "", "text_name": "", "ignored": []},
    }
    task.update(extra)
    (tdir / "任务.json").write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
    return tdir


def read_task_raw(tdir: Path) -> str:
    return (tdir / "任务.json").read_text(encoding="utf-8")


def approved_min(local_id: str) -> dict:
    """最小 approved 夹具字段（无任何阻断）。"""
    return {"approval": {"at": "2026-01-02T00:00:00+08:00", "by": "probe"},
            "approval_snapshot": {"snapshot_version": 2}}


def expect_revoke_refused(name: str, tdir: Path, local_id: str, substr: str) -> None:
    """调 hub_api.revoke_approval，断言抛 HubError 且消息含 substr、任务文件逐字节不变。"""
    import hub_api  # noqa: E402

    before = read_task_raw(tdir)
    refused, msg = False, ""
    try:
        hub_api.revoke_approval(local_id)
        msg = "未抛异常（撤销被放行了！）"
    except hub_api.HubError as e:
        refused = True
        msg = str(e)
    except Exception as e:  # noqa: BLE001 —— 其它异常也算拒跑失败，如实记录
        msg = f"抛了 {type(e).__name__}: {e}"
    ok = refused and substr in msg and read_task_raw(tdir) == before
    check(name, ok,
          f"refused={refused} 含「{substr}」={substr in msg} 文件不变={read_task_raw(tdir) == before} msg={msg[:160]}")


def main() -> int:
    real_root = Path(config.PROJECTS_DIR)
    real_before = probe_guard.projects_snapshot(real_root)
    iso_root = Path(tempfile.mkdtemp(prefix="wanprobe-revokeguard-",
                                     dir=os.environ.get("TMPDIR") or tempfile.gettempdir()))
    iso_projects = iso_root / "projects"
    iso_projects.mkdir(parents=True, exist_ok=True)
    stub = probe_guard.StubBridge()
    port = probe_guard.free_port()
    base = f"http://127.0.0.1:{port}"

    # 读写同源：先补丁 config 再懒加载（hub_api.TASKS_DIR = config.PROJECTS_DIR，import 期求值）
    config.PROJECTS_DIR = iso_projects
    import hub_api  # noqa: E402

    proc = None
    try:
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
            print(f"     数据根 {where.get('tasks_dir')}")
        except probe_guard.GuardError as err:
            check("[0] 隔离服务端起来且读写同源（/api/hub/where == 探针根）", False, str(err))
            print("[probe-revoke-guard] 拒跑：数据根不同源，后续写入/清理一律不执行。")
            return summary()

        # ── 夹具（全部落在隔离根） ────────────────────────────
        t_unknown = make_task(TASK_UNKNOWN, "approved", "probe-rg-unknown",
                              **approved_min(TASK_UNKNOWN), submit_unknown=True,
                              cloud_task_id="cloud-unknown-1")
        t_prepared = make_task(TASK_PREPARED, "approved", "probe-rg-prepared",
                               **approved_min(TASK_PREPARED),
                               dispatch={"phase": "prepared", "at": "2026-01-02T00:01:00+08:00"})
        t_dispatched = make_task(TASK_DISPATCHED, "approved", "probe-rg-dispatched",
                                 **approved_min(TASK_DISPATCHED),
                                 dispatch={"phase": "dispatched", "at": "2026-01-02T00:02:00+08:00"})
        t_active = make_task(TASK_ACTIVE, "approved", "probe-rg-active",
                             **approved_min(TASK_ACTIVE))
        t_allowed = make_task(TASK_ALLOWED, "approved", "probe-rg-allowed",
                              approval={"at": "2026-01-02T00:00:00+08:00", "by": "probe"},
                              approval_snapshot={"snapshot_version": 2, "gen_params": {"x": 1}},
                              assets_confirmed=True, gen_params={"prompt": "p"}, gen_channel={"渠道名": "probe"},
                              empty_confirm=True)
        (t_allowed / "提示词.md").write_text("# 提示词\nprobe\n", encoding="utf-8")
        t_waiting = make_task(TASK_WAITING, "awaiting_approval", "probe-rg-waiting")
        t_generating = make_task(TASK_GENERATING, "generating", "probe-rg-generating")
        t_ingested = make_task(TASK_INGESTED, "ingested", "probe-rg-ingested")
        t_http_ok = make_task(TASK_HTTP_OK, "approved", "probe-rg-http-ok",
                              approval={"at": "2026-01-02T00:00:00+08:00", "by": "probe"},
                              approval_snapshot={"snapshot_version": 2},
                              assets_confirmed=True, gen_params={"prompt": "p"},
                              gen_channel={"渠道名": "probe"})
        (t_http_ok / "提示词.md").write_text("# 提示词\nprobe\n", encoding="utf-8")

        # ── [1] unknown：拒 + 文件逐字节不变 ──────────────────
        print("1) approved + submit_unknown → 拒（「未知收单」阻断不许被 revoke 连根清掉）")
        expect_revoke_refused("[1] unknown：revoke 抛 HubError 且消息含「未知收单」、任务文件不变",
                              t_unknown, TASK_UNKNOWN, "未知收单")
        after = json.loads(read_task_raw(t_unknown))
        check("[1] unknown：submit_unknown/approval/state 全保留（阻断标记没被清）",
              after.get("submit_unknown") is True and after.get("state") == "approved"
              and bool(after.get("approval")) and after.get("cloud_task_id") == "cloud-unknown-1",
              json.dumps({k: after.get(k) for k in ("submit_unknown", "state", "approval", "cloud_task_id")},
                         ensure_ascii=False))

        # ── [2]/[3] dispatch prepared / dispatched：拒 ────────
        print("2) approved + dispatch=prepared/dispatched → 拒（结果未知，先核对渠道）")
        expect_revoke_refused("[2] dispatch=prepared：拒（消息含 dispatch=prepared）、文件不变",
                              t_prepared, TASK_PREPARED, "dispatch=prepared")
        expect_revoke_refused("[3] dispatch=dispatched：拒、文件不变",
                              t_dispatched, TASK_DISPATCHED, "dispatch=dispatched")

        # ── [4] active：HUB_JOBS running → 拒 ─────────────────
        print("3) approved + 活跃提交（HUB_JOBS running）→ 拒")
        fake_job = {"job_id": "probe-rg-job-1", "kind": "hub-submit", "local_id": TASK_ACTIVE,
                    "status": "running"}
        hub_api.HUB_JOBS[fake_job["job_id"]] = fake_job
        try:
            expect_revoke_refused("[4] active：拒（消息含「提交在跑」）、文件不变",
                                  t_active, TASK_ACTIVE, "提交在跑")
        finally:
            hub_api.HUB_JOBS.pop(fake_job["job_id"], None)   # 清掉探针自塞的记录

        # ── [5] 允许态：revoke 成功，回退受控 ─────────────────
        print("4) 允许态 approved（无阻断）→ revoke 成功，清批准、保资产/提示词/草稿")
        raw_before_ok = read_task_raw(t_allowed)
        out = hub_api.revoke_approval(TASK_ALLOWED)
        check("[5] revoke 成功返回 {ok:true, state:awaiting_approval}",
              out.get("ok") is True and out.get("state") == "awaiting_approval", repr(out))
        after_ok = json.loads(read_task_raw(t_allowed))
        check("[5] 任务 state → awaiting_approval", after_ok.get("state") == "awaiting_approval",
              str(after_ok.get("state")))
        check("[5] approval / approval_snapshot 已清（批准作废必须重批）",
              "approval" not in after_ok and "approval_snapshot" not in after_ok,
              json.dumps({k: after_ok.get(k) for k in ("approval", "approval_snapshot")}, ensure_ascii=False))
        check("[5] assets_confirmed / gen_params / gen_channel 保留（草稿不丢）",
              after_ok.get("assets_confirmed") is True and after_ok.get("gen_params") == {"prompt": "p"}
              and after_ok.get("gen_channel") == {"渠道名": "probe"},
              json.dumps({k: after_ok.get(k) for k in ("assets_confirmed", "gen_params", "gen_channel")},
                         ensure_ascii=False))
        check("[5] 提示词.md 仍在（不走丢提示词的回退边）", (t_allowed / "提示词.md").is_file())
        hist = after_ok.get("history") or [{}]
        check("[5] history 末条 from=approved to=awaiting_approval",
              hist[-1].get("from") == "approved" and hist[-1].get("to") == "awaiting_approval",
              json.dumps(hist[-1], ensure_ascii=False))
        check("[5] 未被清的键与调用前一致（只动该动的）",
              json.dumps({k: v for k, v in json.loads(raw_before_ok).items()
                          if k not in ("approval", "approval_snapshot", "history", "state")},
                         ensure_ascii=False, sort_keys=True)
              == json.dumps({k: v for k, v in after_ok.items()
                             if k not in ("approval", "approval_snapshot", "history", "state")},
                            ensure_ascii=False, sort_keys=True))

        # ── [6] 非 approved 起态：一律拒 ──────────────────────
        print("5) 非 approved 起态（awaiting_approval/generating/ingested）→ 拒")
        expect_revoke_refused("[6] awaiting_approval：拒（消息含「撤销批准需 approved」）、文件不变",
                              t_waiting, TASK_WAITING, "撤销批准需 approved")
        expect_revoke_refused("[6] generating：拒、文件不变", t_generating, TASK_GENERATING,
                              "撤销批准需 approved")
        expect_revoke_refused("[6] ingested：拒、文件不变", t_ingested, TASK_INGESTED,
                              "撤销批准需 approved")

        # ── [7] HTTP 面：API 级绕过已封 ───────────────────────
        print("6) HTTP 面：POST /api/hub/task/<id>/revoke 同样被守卫拦住")
        before_http = read_task_raw(t_unknown)
        code, payload = probe_guard.http_req(base, f"/api/hub/task/{TASK_UNKNOWN}/revoke",
                                             method="POST", data=b"{}", ctype="application/json")
        check("[7] HTTP unknown：409 且 error 含「未知收单」、文件不变",
              code == 409 and "未知收单" in str((payload or {}).get("error") or "")
              and read_task_raw(t_unknown) == before_http,
              f"code={code} payload={json.dumps(payload, ensure_ascii=False)[:200]}")
        code, payload = probe_guard.http_req(base, f"/api/hub/task/{TASK_HTTP_OK}/revoke",
                                             method="POST", data=b"{}", ctype="application/json")
        after_http = json.loads(read_task_raw(t_http_ok))
        check("[7] HTTP 允许态：200 且 state=awaiting_approval（正常撤销不被误伤）",
              code == 200 and (payload or {}).get("state") == "awaiting_approval"
              and after_http.get("state") == "awaiting_approval",
              f"code={code} payload={json.dumps(payload, ensure_ascii=False)[:200]}")

        # ── [8] 真实任务零丢失 + 不留垃圾 ─────────────────────
        print("7) 真实数据根对账 + 隔离夹具只删自建")
        real_after = probe_guard.projects_snapshot(real_root)
        try:
            probe_guard.assert_no_loss(real_before, real_after)
            no_garbage = real_after == real_before
            check("[8] 真实项目目录集前后一致（零丢失 + 探针不留垃圾）", no_garbage,
                  f"多出 {sorted(set(real_after) - set(real_before))}" if not no_garbage else "")
        except probe_guard.GuardError as err:
            check("[8] 真实项目目录集前后一致（零丢失 + 探针不留垃圾）", False, str(err))
        for fid in FIXTURE_IDS:
            ok, why = probe_guard.cleanup_created(iso_projects, fid,
                                                  before=set(), created=set(FIXTURE_IDS))
            if not ok:
                print(f"     清理异常：{why}")
        check("[8] 隔离根夹具全部清干净（cleanup_created 只删自建）",
              not any((iso_projects / fid).exists() for fid in FIXTURE_IDS))
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
