#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AT-16 B9 后端探针：**解除阻断（cleared）之后的换配置路径真通**，且心血逐字节不作废。

跑法：wanrepro/ 下 `python -m automation.probe_at16_b9`。

缺陷 D10（所有者 2026-11-12 08:13 实测 auto-0043）：人工解除阻断后任务仍是 approved，
配置只读、主操作只让人原配置重交（上次正是配置问题才撞 502），真正的换配置路
（B5b：撤销批准 → 回 awaiting_approval → 改 → 重批）在界面上撞不到。
前端 B9 补的是**直达条 + 一键接既有撤销链**（web/scripts/probe-at16-b9.mjs 锁可见性与接线）；
本探针锁那条链在**后端事实层**从 cleared 态出发是真通的，且不新造状态、不动心血：

  [0] 隔离与同源：快照真实 PROJECTS_DIR → tempfile 隔离根 → 改 config.PROJECTS_DIR 后才 import
      hub_api（TASKS_DIR 在 import 期求值）；起真实隔离服务端 + 假 bridge，/api/hub/where
      走 assert_same_root；不同源一律拒跑后续写入。
  [1] 走到 cleared 态用**真路**（不是手写夹具）：unknown 夹具 → POST release(confirm) → cleared。
      · 解除前 submit_view.build → form=unknown / blocks_retry=True（D10 的病态）
      · 解除后 submit_view.build → form=none / dispatch_phase=cleared / blocks_retry=False，
        state 仍是 approved —— **这就是前端直达条与主状态的判据源**（B9 前端探针吃的字段）
      · 解除记 history（人工动作留痕），gen_error / submit_unknown 都已清
  [2] cleared 态撤销批准放行：revoke_approval → awaiting_approval（配置这才解锁）；approval /
      approval_snapshot 清掉；gen_channel / gen_params 保留（改配置从它开始，草稿不丢）
  [3] 改模型 → 重批 → 提交：do_approve(model-b) 后 gen_channel.model=model-b、内容快照 hash 变、
      版本仍 2；start_submit 冻结的 gen_channel.model **就是** model-b（do_submit 打桩，不发付费请求）
  [4] 心血不作废（硬断言，逐条比字节，任一条破了就 FAIL）：release + revoke + 重批 + 提交 全链之后
      任务保护字段（assets / assets_confirmed / pending_replace / empty_confirm / asset_host /
      ingestion / intent / local_id / bridge_id / created_at / lapian_stage）逐字节不变；
      提示词.md 原始字节不变；资产文件字节不变；sidecar 行决策字节不变；只许运行时键变化
  [5] 边界（不许越界放宽）：
      · cleared 但另有 submit_unknown（解除后又撞未知）→ revoke 仍被拒且文件逐字节不变
      · cleared 但 dispatch 又落 prepared → revoke 仍被拒
      · awaiting_approval 态 revoke → 拒绝（没有可撤销的批准，别把这条路当万能回滚）
  [6] cleared 态「原配置重交」合法（卡片修法③）：不改配置直接 start_submit 受理成功（打桩），
      不被任何未知阻断挡住
  [7] 真实任务零丢失 + 夹具只删自建；sidecar 夹具目录前后一致

夹具只用 auto-9821..auto-9825（隔离根内）与 sidecar bridge_id `probe-b9-heart`，跑完连根删。
真实任务零写入，跑前跑后对账。后端**一行不改**（B9 白名单只开前端）。
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

from automation import config, probe_guard, replace  # noqa: E402

RESULTS: list[tuple[str, bool]] = []

TASK_CLEARED = "auto-9821"       # unknown → release → cleared → 撤销 → 改模型 → 重批 → 提交（[1][2][3][4][6]）
TASK_CLEARED_UNKNOWN = "auto-9822"   # cleared + 又撞未知（[5]）
TASK_CLEARED_DISPATCH = "auto-9823"  # cleared + dispatch=prepared（[5]）
TASK_HTTP = "auto-9824"          # cleared，HTTP 面 revoke（[7] 复用 [6] 之前）
TASK_PENDING = "auto-9825"       # awaiting_approval（[5] revoke 拒）
FIXTURE_IDS = (TASK_CLEARED, TASK_CLEARED_UNKNOWN, TASK_CLEARED_DISPATCH, TASK_HTTP, TASK_PENDING)
BRIDGE_HEART = "probe-b9-heart"

HEART_KEYS = ("local_id", "bridge_id", "created_at", "intent", "assets",
              "assets_confirmed", "pending_replace", "empty_confirm", "asset_host",
              "ingestion", "lapian_stage")
RUNTIME_KEYS = ("state", "history", "approval", "approval_snapshot", "snapshot_version",
                "gen_params", "gen_channel", "size_input", "dispatch", "submit_unknown",
                "submit_unknown_at", "gen_error", "gen_progress", "gen_job", "gen_task_id",
                "cloud_task_id", "last_job")

PROMPT_TEXT = "# 提示词\n\n1. 0-3 秒：产品特写，灯光左移。\n2. 3-6 秒：手部演示。\n\n> B9 探针夹具（cleared 态换配置链），逐字节比对用。\n"


def check(name: str, cond: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" —— {detail}" if detail and not cond else ""))
    return bool(cond)


def summary() -> int:
    fails = [n for n, ok in RESULTS if not ok]
    print(f"\n[probe-at16-b9] {len(RESULTS) - len(fails)}/{len(RESULTS)} PASS")
    for n in fails:
        print(f"  FAIL {n}")
    return 1 if fails else 0


def make_task(local_id: str, state_name: str, bridge_id: str, **extra) -> Path:
    tdir = Path(config.PROJECTS_DIR) / local_id
    shutil.rmtree(tdir, ignore_errors=True)
    (tdir / "inbox").mkdir(parents=True, exist_ok=True)
    task = {
        "local_id": local_id, "bridge_id": bridge_id,
        "created_at": "2026-01-01T00:00:00+08:00", "state": state_name,
        "history": [{"at": "2026-01-01T00:00:00+08:00", "from": None, "to": state_name,
                     "note": "probe-at16-b9 夹具"}],
        "intent": "AT-16 B9 cleared 态换配置探针夹具", "assets": [], "ingestion": {"zip_sha256": f"probe-{local_id}",
                                                                                  "zip_name": "probe", "video_name": "",
                                                                                  "text_name": "", "ignored": []},
    }
    task.update(extra)
    (tdir / "任务.json").write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
    return tdir


def heart_fixture(local_id: str, bridge_id: str, state_name: str = "awaiting_approval") -> Path:
    """「心血」齐全的夹具：资产确认 / 上传流水（pending_replace）/ 置换确认 / 行决策 / 提示词。"""
    tdir = make_task(
        local_id, state_name, bridge_id,
        assets=[{"no": 1, "file": "asset-1.png", "kind": "product"}],
        pending_replace=[
            {"no": 1, "img": "asset-1.png", "url": f"local:{local_id}/asset-uploads/aaa.png",
             "name": "亮粉色标签款", "desc": "透明圆柱形喷雾", "note": "owner 手填描述"},
            {"no": 2, "img": "asset-2.png", "url": f"local:{local_id}/asset-uploads/bbb.png",
             "name": "银灰款", "desc": "替换原片第 2 行", "note": ""},
        ],
        assets_confirmed=[
            {"no": 1, "img": "aaa.png", "url": f"local:{local_id}/asset-uploads/aaa.png",
             "name": "亮粉色标签款", "desc": "透明圆柱形喷雾", "decision": "replace"},
        ],
        empty_confirm=False,
        asset_host=["uguu", "tencent-videocache"],
        lapian_stage="门禁终态：通过｜PASS｜绿项 8/8",
    )
    (tdir / "提示词.md").write_text(PROMPT_TEXT, encoding="utf-8")
    (tdir / "asset-uploads").mkdir(exist_ok=True)
    (tdir / "asset-uploads" / "aaa.png").write_bytes(b"\x89PNG\r\n\x1a\nPROBE-B9")
    side = replace.sidecar_path(bridge_id)
    side.parent.mkdir(parents=True, exist_ok=True)
    side.write_text(json.dumps({
        "行决策覆盖": {"1": "keep_origin", "2": "dismissed"},
        "同实体确认": [[1, 2]],
        "组元数据": {"1,2": {"主行": 1, "组名": "同款产品"}},
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return tdir


def read_raw(tdir: Path) -> str:
    return (tdir / "任务.json").read_text(encoding="utf-8")


def load(tdir: Path) -> dict:
    return json.loads(read_raw(tdir))


def heart_bytes(tdir: Path) -> dict:
    task = load(tdir)
    return {
        "fields": {k: json.dumps(task.get(k), ensure_ascii=False, sort_keys=True) for k in HEART_KEYS},
        "prompt": (tdir / "提示词.md").read_bytes() if (tdir / "提示词.md").is_file() else b"",
        "asset": (tdir / "asset-uploads" / "aaa.png").read_bytes()
        if (tdir / "asset-uploads" / "aaa.png").is_file() else b"",
        "keys": set(task.keys()),
    }


def diff_heart(before: dict, after: dict) -> list[str]:
    bad = [k for k in HEART_KEYS if before["fields"].get(k) != after["fields"].get(k)]
    if before["prompt"] != after["prompt"]:
        bad.append("提示词.md")
    if before["asset"] != after["asset"]:
        bad.append("asset-uploads/aaa.png")
    lost = sorted((before["keys"] - after["keys"]) - set(RUNTIME_KEYS))
    if lost:
        bad.append(f"键丢失{lost}")
    return bad


def config_of(model: str, size: str = "16:9", duration: int = 8) -> dict:
    return {
        "渠道身份": {"baseUrl": "https://sdapi.probe.example.com", "apiFormat": "sdgf",
                     "渠道名": "probe-SDGF"},
        "model": model,
        "mode": "frames",
        "params": {"duration": duration, "size": size, "rawSize": size,
                   "vquality": "720", "audio": True},
    }


def expect_refused(name: str, tdir: Path, substr: str, fn) -> None:
    import hub_api  # noqa: E402

    before = read_raw(tdir)
    refused, msg = False, ""
    try:
        fn()
        msg = "未抛异常（放行了！）"
    except hub_api.HubError as e:
        refused = True
        msg = str(e)
    except Exception as e:  # noqa: BLE001 —— 其它异常也算失败，如实记录
        msg = f"抛了 {type(e).__name__}: {e}"
    check(name, refused and substr in msg and read_raw(tdir) == before,
          f"refused={refused} 含「{substr}」={substr in msg} 文件不变={read_raw(tdir) == before} msg={msg[:160]}")


def unknown_state(task: dict, cloud: str = "cloud-b9-1") -> dict:
    """D10 的真实前态：批准后提交撞 502 → 未知收单标记 + dispatch=dispatched。"""
    task.update({
        "approval": {"at": "2026-01-02T00:00:00+08:00", "by": "probe", "snapshot_sha256": "x"},
        "approval_snapshot": "x", "snapshot_version": 2,
        "gen_params": {"duration": 8, "ratio": "16:9", "resolution": "720", "audio": True},
        "gen_channel": {"baseUrl": "https://sdapi.probe.example.com", "apiFormat": "sdgf",
                        "渠道名": "probe-SDGF", "model": "model-a", "mode": "frames"},
        "submit_unknown": {"at": "2026-01-02T00:05:00+08:00",
                           "reason": "提交结果未知：HTTP 502 渠道 403", "dispatch": "dispatched"},
        "gen_error": "提交结果未知：HTTP 502 渠道 403",
        "cloud_task_id": cloud,
        "dispatch": {"phase": "dispatched", "at": "2026-01-02T00:04:00+08:00"},
    })
    return task


def main() -> int:
    real_root = Path(config.PROJECTS_DIR)
    real_before = probe_guard.projects_snapshot(real_root)
    side_root = replace.sidecar_path("__probe_probe__").parent
    side_before = sorted(p.name for p in side_root.glob("*")) if side_root.is_dir() else []

    iso_root = Path(tempfile.mkdtemp(prefix="wanprobe-at16b9-",
                                     dir=os.environ.get("TMPDIR") or tempfile.gettempdir()))
    iso_projects = iso_root / "projects"
    iso_projects.mkdir(parents=True, exist_ok=True)
    stub = probe_guard.StubBridge()
    port = probe_guard.free_port()
    base = f"http://127.0.0.1:{port}"
    proc = None
    try:
        config.PROJECTS_DIR = iso_projects
        import hub_api  # noqa: E402  —— 必须晚于 config 补丁，否则 TASKS_DIR 指真实根
        from automation import generate, state, submit_view  # noqa: E402

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
        except probe_guard.GuardError as err:
            check("[0] 隔离服务端起来且读写同源（/api/hub/where == 探针根）", False, str(err))
            print("[probe-at16-b9] 拒跑：数据根不同源，后续写入 / 清理一律不执行。")
            return summary()

        # ── 夹具 ────────────────────────────────────────────
        t_cleared = heart_fixture(TASK_CLEARED, BRIDGE_HEART, "approved")
        (t_cleared / "任务.json").write_text(
            json.dumps(unknown_state(load(t_cleared)), ensure_ascii=False, indent=2), encoding="utf-8")

        t_cu = heart_fixture(TASK_CLEARED_UNKNOWN, BRIDGE_HEART + "-cu", "approved")
        task_cu = unknown_state(load(t_cu), cloud="cloud-b9-cu")
        task_cu["dispatch"] = {"phase": "cleared", "at": "2026-01-02T00:10:00+08:00"}
        task_cu["submit_unknown"] = {"at": "2026-01-02T00:11:00+08:00",
                                     "reason": "提交结果未知：解除后又撞 502", "dispatch": "cleared"}
        (t_cu / "任务.json").write_text(json.dumps(task_cu, ensure_ascii=False, indent=2), encoding="utf-8")

        t_cd = heart_fixture(TASK_CLEARED_DISPATCH, BRIDGE_HEART + "-cd", "approved")
        task_cd = load(t_cd)
        task_cd.update({"approval": {"at": "2026-01-02T00:00:00+08:00", "by": "probe", "snapshot_sha256": "x"},
                        "approval_snapshot": "x", "snapshot_version": 2,
                        "dispatch": {"phase": "prepared", "at": "2026-01-02T00:12:00+08:00"}})
        (t_cd / "任务.json").write_text(json.dumps(task_cd, ensure_ascii=False, indent=2), encoding="utf-8")

        t_http = heart_fixture(TASK_HTTP, BRIDGE_HEART + "-http", "approved")
        task_h = unknown_state(load(t_http), cloud="cloud-b9-http")
        (t_http / "任务.json").write_text(json.dumps(task_h, ensure_ascii=False, indent=2), encoding="utf-8")

        t_pending = heart_fixture(TASK_PENDING, BRIDGE_HEART + "-pend", "awaiting_approval")

        side_path = replace.sidecar_path(BRIDGE_HEART)
        side_bytes_before = side_path.read_bytes()
        side_decisions_before = replace.load_row_decisions(BRIDGE_HEART)

        # ── [1] 走到 cleared 态用真路 ────────────────────────
        print("1) clears 态用真路走来：unknown → POST release(confirm) → cleared（不手写 cleared 夹具）")
        task_u = load(t_cleared)
        before_view = submit_view.build(task_u, active_submit=False)
        check("[1] 解除前：submit_view.build → form=unknown / blocks_retry=True（D10 的病态）",
              before_view.get("form") == "unknown" and before_view.get("blocks_retry") is True
              and before_view.get("dispatch_phase") == "dispatched",
              json.dumps({k: before_view.get(k) for k in ("form", "blocks_retry", "dispatch_phase")}, ensure_ascii=False))

        heart_before = heart_bytes(t_cleared)
        code, payload = probe_guard.http_req(base, f"/api/hub/task/{TASK_CLEARED}/release",
                                            method="POST",
                                            data=json.dumps({"confirm": True}).encode("utf-8"),
                                            ctype="application/json")
        task_rel = load(t_cleared)
        check("[1] POST release(confirm) → 200 且 submit_unknown / gen_error 清掉",
              code == 200 and not task_rel.get("submit_unknown") and not task_rel.get("gen_error"),
              f"code={code} payload={json.dumps(payload, ensure_ascii=False)[:160]}")
        check("[1] 解除后 dispatch 记 cleared（审计留痕）",
              state.dispatch_phase(task_rel) == "cleared", json.dumps(task_rel.get("dispatch"), ensure_ascii=False))
        check("[1] 解除后 state 仍是 approved（D10 的坑就在这里：配置依旧只读）",
              task_rel.get("state") == "approved", str(task_rel.get("state")))
        check("[1] 解除记 history（人工动作留痕，不是无声删除）",
              any("解除" in str((row or {}).get("note") or "") for row in (task_rel.get("history") or [])),
              json.dumps((task_rel.get("history") or [])[-1:], ensure_ascii=False)[:200])

        after_view = submit_view.build(task_rel, active_submit=False)
        check("[1] 解除后：submit_view.build → form=none（不再喊「未知」，主状态/前端判据随事实走）",
              after_view.get("form") == "none", after_view.get("form"))
        check("[1] 解除后：dispatch_phase=cleared / blocks_retry=False（**前端直达条判据源**）",
              after_view.get("dispatch_phase") == "cleared" and after_view.get("blocks_retry") is False,
              json.dumps({k: after_view.get(k) for k in ("dispatch_phase", "blocks_retry")}, ensure_ascii=False))
        check("[2] 解除阻断不动心血（资产确认 / 置换 / 提示词逐字节不变）",
              not diff_heart(heart_before, heart_bytes(t_cleared)), f"变了：{diff_heart(heart_before, heart_bytes(t_cleared))}")

        # ── [2] cleared 态撤销批准放行 ───────────────────────
        print("2) cleared 态撤销批准：放行 → awaiting_approval（配置解锁），草稿保留")
        revoked = hub_api.revoke_approval(TASK_CLEARED)
        task_rv = load(t_cleared)
        check("[2] cleared 态 revoke 放行 → state=awaiting_approval（UI「配置解锁」的后端依据）",
              revoked.get("state") == "awaiting_approval" and task_rv.get("state") == "awaiting_approval",
              str(revoked))
        check("[2] approval / approval_snapshot 已清（批准作废必须重批）",
              "approval" not in task_rv and "approval_snapshot" not in task_rv,
              json.dumps({k: task_rv.get(k) for k in ("approval", "approval_snapshot")}, ensure_ascii=False))
        check("[2] gen_channel / gen_params 保留（改配置从原配置开始，草稿不丢）",
              (task_rv.get("gen_channel") or {}).get("model") == "model-a" and bool(task_rv.get("gen_params")),
              json.dumps(task_rv.get("gen_channel"), ensure_ascii=False))
        check("[2] 撤销不动心血（逐字节）", not diff_heart(heart_before, heart_bytes(t_cleared)),
              f"变了：{diff_heart(heart_before, heart_bytes(t_cleared))}")

        # ── [3] 改模型 → 重批 → 提交 ─────────────────────────
        print("3) 改模型 → 重批 → 提交：冻结配置 model=新值（do_submit 打桩，不发付费请求）")
        prompt_now = (t_cleared / "提示词.md").read_text(encoding="utf-8")
        out_b = hub_api.do_approve(TASK_CLEARED, config=config_of("model-b"))
        task_b = load(t_cleared)
        check("[3] 重批：state=approved、snapshot_version 仍 2（不递增）",
              out_b.get("state") == "approved" and task_b.get("snapshot_version") == 2,
              json.dumps(out_b, ensure_ascii=False)[:160])
        check("[3] 重批：gen_channel.model=model-b（新配置真生效）",
              (task_b.get("gen_channel") or {}).get("model") == "model-b",
              json.dumps(task_b.get("gen_channel"), ensure_ascii=False))
        check("[3] 重批：approval hash == 当前输入快照（换 model 就是换快照输入）",
              task_b["approval"]["snapshot_sha256"] == state.snapshot_for(task_b, prompt_now),
              str(task_b["approval"]["snapshot_sha256"])[:16])
        check("[4] 撤销 → 换模型 → 重批 全链不动心血（逐字节）",
              not diff_heart(heart_before, heart_bytes(t_cleared)), f"变了：{diff_heart(heart_before, heart_bytes(t_cleared))}")
        check("[4] sidecar 行决策字节不变（重批链不写决策）", side_path.read_bytes() == side_bytes_before)
        check("[4] 行决策读回一致（keep_origin / dismissed 没被重批改掉）",
              replace.load_row_decisions(BRIDGE_HEART) == side_decisions_before == {1: "keep_origin", 2: "dismissed"},
              str(replace.load_row_decisions(BRIDGE_HEART)))
        after_keys = set(task_b.keys())
        new_keys = sorted(after_keys - heart_before["keys"])
        check("[4] 新增键都在运行时白名单内（不新造持久状态）",
              all(k in RUNTIME_KEYS for k in new_keys), f"多出：{new_keys}")
        check("[4] 保护字段一个都没少", all(k in after_keys for k in HEART_KEYS),
              f"缺：{[k for k in HEART_KEYS if k not in after_keys]}")

        frozen_seen: dict = {}
        orig_do_submit = generate.do_submit

        def fake_do_submit(task_dir, verbose=False, on_progress=None, frozen=None,
                           write_guard=None, api_key=None):  # noqa: ANN001
            frozen_seen["frozen"] = frozen
            frozen_seen["api_key"] = api_key
            return {"fake": True}

        generate.do_submit = fake_do_submit
        try:
            job = hub_api.start_submit(TASK_CLEARED, key="sk-b9")
            job_id = str(job.get("job_id") or "")
            for _ in range(100):
                rec = hub_api.HUB_JOBS.get(job_id) or {}
                if rec.get("status") in ("done", "failed"):
                    break
                time.sleep(0.05)
        finally:
            generate.do_submit = orig_do_submit
        rec = hub_api.HUB_JOBS.get(job_id) or {}
        frozen = frozen_seen.get("frozen") or {}
        check("[3] 提交受理（后台 job 跑完 done）", rec.get("status") == "done", json.dumps(rec, ensure_ascii=False)[:160])
        check("[3] **提交 payload model=新值**：冻结 gen_channel.model=model-b",
              (frozen.get("gen_channel") or {}).get("model") == "model-b",
              json.dumps(frozen.get("gen_channel"), ensure_ascii=False))
        check("[3] 冻结 gen_params == 本次批准的 gen_params", frozen.get("gen_params") == task_b.get("gen_params"),
              json.dumps(frozen.get("gen_params"), ensure_ascii=False))
        check("[3] key 一次性透传（不落盘：任务文件里没有 sk-b9）",
              "sk-b9" not in read_raw(t_cleared) and frozen_seen.get("api_key") == "sk-b9")
        check("[4] 提交不改快照 / 不动心血（approval hash 仍是这次重批的）",
              load(t_cleared)["approval"]["snapshot_sha256"] == task_b["approval"]["snapshot_sha256"]
              and not diff_heart(heart_before, heart_bytes(t_cleared)))

        # ── [5] 边界：不许越界放宽 ───────────────────────────
        print("5) 边界：cleared 不等于万能回滚（未知 / 在途仍拦，待批准没得撤销）")
        expect_refused("[5] cleared + 又撞未知（submit_unknown）→ revoke 仍拒，文件逐字节不变",
                       t_cu, "未知收单", lambda: hub_api.revoke_approval(TASK_CLEARED_UNKNOWN))
        expect_refused("[5] cleared + dispatch 又落 prepared → revoke 仍拒",
                       t_cd, "dispatch=prepared", lambda: hub_api.revoke_approval(TASK_CLEARED_DISPATCH))
        expect_refused("[5] awaiting_approval（已撤销过）→ revoke 拒（没有可撤销的批准）",
                       t_pending, "需 approved", lambda: hub_api.revoke_approval(TASK_PENDING))

        # ── [6] cleared 态原配置重交合法 + [7] HTTP 面 ──────
        print("6) cleared 态「原配置重交」合法（卡片修法③）")
        check("[7] HTTP release 无 confirm → 400（人工动作合同没变）",
              probe_guard.http_req(base, f"/api/hub/task/{TASK_HTTP}/release", method="POST",
                                   data=b"{}", ctype="application/json")[0] == 400)
        code, _ = probe_guard.http_req(base, f"/api/hub/task/{TASK_HTTP}/release", method="POST",
                                       data=json.dumps({"confirm": True}).encode("utf-8"),
                                       ctype="application/json")
        check("[7] HTTP release 带 confirm → 200", code == 200, str(code))
        code, payload = probe_guard.http_req(base, f"/api/hub/task/{TASK_HTTP}/revoke", method="POST",
                                             data=b"{}", ctype="application/json")
        check("[7] HTTP cleared 态 revoke → 200 且 state=awaiting_approval",
              code == 200 and (payload or {}).get("state") == "awaiting_approval",
              f"code={code} payload={json.dumps(payload, ensure_ascii=False)[:160]}")

        # 原配置重交：cleared 之后不改配置直接提交 → 受理（打桩），不被未知阻断挡住
        t_plain = heart_fixture("auto-9826", BRIDGE_HEART + "-plain", "approved")
        (t_plain / "任务.json").write_text(
            json.dumps(unknown_state(load(t_plain), cloud="cloud-b9-plain"), ensure_ascii=False, indent=2),
            encoding="utf-8")
        FIXTURE_IDS_EXTRA = ("auto-9826",)
        probe_guard.http_req(base, "/api/hub/task/auto-9826/release", method="POST",
                            data=json.dumps({"confirm": True}).encode("utf-8"), ctype="application/json")
        view_plain = submit_view.build(load(t_plain), active_submit=False)
        check("[6] cleared 后再看事实：form=none / blocks_retry=False（原配置重交合法，未被阻断）",
              view_plain.get("form") == "none" and view_plain.get("blocks_retry") is False,
              json.dumps({k: view_plain.get(k) for k in ("form", "blocks_retry")}, ensure_ascii=False))
        generate.do_submit = fake_do_submit
        try:
            job2 = hub_api.start_submit("auto-9826", key="sk-b9-plain")
            job2_id = str(job2.get("job_id") or "")
            for _ in range(100):
                rec2 = hub_api.HUB_JOBS.get(job2_id) or {}
                if rec2.get("status") in ("done", "failed"):
                    break
                time.sleep(0.05)
        finally:
            generate.do_submit = orig_do_submit
        check("[6] cleared 态原配置重交：start_submit 受理成功（不因 cleared 被拒）",
              (hub_api.HUB_JOBS.get(job2_id) or {}).get("status") == "done",
              json.dumps(hub_api.HUB_JOBS.get(job2_id) or {}, ensure_ascii=False)[:200])

        # ── [8] 真实数据根对账 + 清理 ────────────────────────
        print("7) 真实数据根对账 + 夹具只删自建")
        real_after = probe_guard.projects_snapshot(real_root)
        try:
            probe_guard.assert_no_loss(real_before, real_after)
            check("[8] 真实项目目录集前后一致（零丢失 + 探针不留垃圾）", real_after == real_before,
                  f"多出 {sorted(set(real_after) - set(real_before))}" if real_after != real_before else "")
        except probe_guard.GuardError as err:
            check("[8] 真实项目目录集前后一致（零丢失 + 探针不留垃圾）", False, str(err))
        created = set(FIXTURE_IDS) | set(FIXTURE_IDS_EXTRA)
        for fid in sorted(created):
            ok, why = probe_guard.cleanup_created(iso_projects, fid, before=set(), created=created)
            if not ok:
                print(f"     清理异常：{why}")
        check("[8] 隔离根夹具全部清干净", not any((iso_projects / fid).exists() for fid in created))
        for bridge in (BRIDGE_HEART, BRIDGE_HEART + "-cu", BRIDGE_HEART + "-cd",
                       BRIDGE_HEART + "-http", BRIDGE_HEART + "-pend", BRIDGE_HEART + "-plain"):
            shutil.rmtree(replace.sidecar_path(bridge).parent, ignore_errors=True)
        side_after = sorted(p.name for p in side_root.glob("*")) if side_root.is_dir() else []
        check("[8] sidecar 夹具目录清干净（analysis/reverse 前后一致）", side_after == side_before,
              f"多出 {sorted(set(side_after) - set(side_before))}")
        return summary()
    finally:
        for bridge in (BRIDGE_HEART, BRIDGE_HEART + "-cu", BRIDGE_HEART + "-cd",
                       BRIDGE_HEART + "-http", BRIDGE_HEART + "-pend", BRIDGE_HEART + "-plain"):
            shutil.rmtree(replace.sidecar_path(bridge).parent, ignore_errors=True)
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
