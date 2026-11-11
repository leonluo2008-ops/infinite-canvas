#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AT-16 B5b 后端探针：**换模型重批，心血不作废**（硬断言：资产 / 提示词 / 行决策逐字节不变）。

跑法：wanrepro/ 下 `python -m automation.probe_at16_b5b`。

缺陷 D6 后半（真机实证）：批准后想换模型 / 改配置的用户看不到「撤销批准 → 改模型 → 批准」
这条路，以为资产确认 / 置换 / 提示词这些心血会作废。前端 B5b 补的是**可见可用 + 起态门**；
本探针锁的是那条链在**后端事实层**真的不作废心血：

  [0] 隔离与同源：先快照真实 PROJECTS_DIR，tempfile 造隔离根，改 config.PROJECTS_DIR 后才 import
      hub_api（TASKS_DIR 在 import 期求值）；再起真实隔离服务端（ui/server.py + 假 bridge），
      /api/hub/where 走 assert_same_root 证明读写同源，不同源一律拒跑后续写入。
  [1] 允许态全链（待批准 → 批准 A → 撤销 → 批准 B）：
      · 批准 A：state=approved、snapshot_version 仍 2、approval.snapshot_sha256 == 当前输入快照
      · 撤销：回 awaiting_approval，approval/approval_snapshot 清掉（批准作废必须重批）
      · 批准 B（换 model）：内容快照 hash **改变**、版本仍 2（不递增）、gen_channel.model 是新模型
  [2] 心血不作废（判据经审计更正，逐条硬断言，任一条破了就 FAIL）：
      · 任务.json 保护字段（assets / assets_confirmed / pending_replace / empty_confirm /
        asset_host / ingestion / intent / local_id / bridge_id / created_at）**逐字节不变**
      · 提示词.md **原始字节**不变
      · sidecar 行决策（analysis/reverse/<bridge>/待确认清单.json）**字节不变**
      · 允许变化的只有运行时键（state / history / approval* / gen_params / gen_channel /
        snapshot_version / size_input / dispatch / submit_unknown / gen_error / gen_*）——
        且不许有键凭空消失（除 approval/approval_snapshot 被撤销清掉）
  [3] 提交用新配置：start_submit 冻结的 gen_channel/gen_params **就是** 批准 B 写下的那份
      （do_submit 用测试桩替换，不发真实付费请求），提交后 approval 快照没被改动。
  [4] 未知态不许引导撤销（前端起态门的后端理由）：
      · approved + submit_unknown → revoke_approval 抛 HubError 且文件逐字节不变
      · approved + dispatch=prepared → 同样拒
      · 人工解除走既有 release：无 confirm → HubInputError（HTTP 400）；confirm:true → 成功清标记，
        资产 / 提示词不变；解除后才允许撤销（UI 三步「核对 → 解除 → 改配置」在这层是通的）
  [5] HTTP 面：POST /api/hub/task/<id>/release 无 confirm → 400；带 confirm → 200；
      允许态 POST revoke → 200 且 state=awaiting_approval。
  [6] 真实任务零丢失 + 探针不留垃圾（真实项目目录集前后一致；隔离夹具与 sidecar 夹具只删自建）。

夹具只用 auto-9811..auto-9816（隔离根内）与 sidecar bridge_id `probe-b5b-heart`，跑完连根删。
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

from automation import config, probe_guard, replace  # noqa: E402

RESULTS: list[tuple[str, bool]] = []

TASK_FLOW = "auto-9811"        # 待批准 → 批准 A → 撤销 → 批准 B（[1][2][3]）
TASK_UNKNOWN = "auto-9812"     # approved + submit_unknown + dispatch=dispatched（[4] 进程内 + [5] HTTP）
TASK_DISPATCH = "auto-9813"    # approved + dispatch=prepared（[4]）
TASK_HTTP_OK = "auto-9814"     # approved 允许态（[5] HTTP revoke）
TASK_UNKNOWN_ONLY = "auto-9815"  # approved + 只有 submit_unknown（无 dispatch → 走 unknown 分支）
FIXTURE_IDS = (TASK_FLOW, TASK_UNKNOWN, TASK_DISPATCH, TASK_HTTP_OK, TASK_UNKNOWN_ONLY)
BRIDGE_HEART = "probe-b5b-heart"     # sidecar 夹具（跑完删）

# 心血：审批链**绝不许动**的任务字段（逐字节比对）
HEART_KEYS = ("local_id", "bridge_id", "created_at", "intent", "assets",
              "assets_confirmed", "pending_replace", "empty_confirm", "asset_host",
              "ingestion", "lapian_stage")
# 允许合法变化的键（state/history/approval*/gen_* 都是运行时）
RUNTIME_KEYS = ("state", "history", "approval", "approval_snapshot", "snapshot_version",
                "gen_params", "gen_channel", "size_input", "dispatch", "submit_unknown",
                "submit_unknown_at", "gen_error", "gen_progress", "gen_job", "gen_task_id",
                "cloud_task_id", "last_job")

PROMPT_TEXT = "# 提示词\n\n1. 0-3 秒：产品特写，灯光左移。\n2. 3-6 秒：手部演示。\n\n> 这是探针夹具，逐字节比对用。\n"


def check(name: str, cond: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" —— {detail}" if detail and not cond else ""))
    return bool(cond)


def summary() -> int:
    fails = [n for n, ok in RESULTS if not ok]
    print(f"\n[probe-at16-b5b] {len(RESULTS) - len(fails)}/{len(RESULTS)} PASS")
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
                     "note": "probe-at16-b5b 夹具"}],
        "intent": "AT-16 B5b 心血不作废探针夹具", "assets": [],
        "ingestion": {"zip_sha256": f"probe-{local_id}", "zip_name": "probe",
                      "video_name": "", "text_name": "", "ignored": []},
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
    (tdir / "asset-uploads" / "aaa.png").write_bytes(b"\x89PNG\r\n\x1a\nPROBE-AAA")
    # sidecar 行决策（真源在 analysis/reverse/<bridge>/待确认清单.json）
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


def heart_bytes(tdir: Path) -> dict:
    """心血快照：任务保护字段（规范化 JSON）+ 提示词原始字节 + 资产文件字节。"""
    task = json.loads(read_raw(tdir))
    return {
        "fields": {k: json.dumps(task.get(k), ensure_ascii=False, sort_keys=True) for k in HEART_KEYS},
        "prompt": (tdir / "提示词.md").read_bytes() if (tdir / "提示词.md").is_file() else b"",
        "asset": (tdir / "asset-uploads" / "aaa.png").read_bytes()
        if (tdir / "asset-uploads" / "aaa.png").is_file() else b"",
        "keys": set(task.keys()),
    }


def diff_heart(before: dict, after: dict) -> list[str]:
    """返回变了的心血项（空 = 全都没动）。"""
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
    """画布 approve 配置（无 key，§2.1）。"""
    return {
        "渠道身份": {"baseUrl": "https://sdapi.probe.example.com", "apiFormat": "sdgf",
                     "渠道名": "probe-SDGF"},
        "model": model,
        "mode": "frames",
        "params": {"duration": duration, "size": size, "rawSize": size,
                   "vquality": "720", "audio": True},
    }


def expect_refused(name: str, local_id: str, tdir: Path, substr: str, fn) -> None:
    """调 fn()，断言抛 HubError 且消息含 substr、任务文件逐字节不变。"""
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
    ok = refused and substr in msg and read_raw(tdir) == before
    check(name, ok,
          f"refused={refused} 含「{substr}」={substr in msg} 文件不变={read_raw(tdir) == before} msg={msg[:160]}")


def main() -> int:
    real_root = Path(config.PROJECTS_DIR)
    real_before = probe_guard.projects_snapshot(real_root)
    side_root = replace.sidecar_path("__probe_probe__").parent
    side_before = sorted(p.name for p in side_root.glob("*")) if side_root.is_dir() else []

    iso_root = Path(tempfile.mkdtemp(prefix="wanprobe-at16b5b-",
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
            print(f"     数据根 {where.get('tasks_dir')}")
        except probe_guard.GuardError as err:
            check("[0] 隔离服务端起来且读写同源（/api/hub/where == 探针根）", False, str(err))
            print("[probe-at16-b5b] 拒跑：数据根不同源，后续写入 / 清理一律不执行。")
            return summary()

        # ── 夹具 ────────────────────────────────────────────
        t_flow = heart_fixture(TASK_FLOW, BRIDGE_HEART)
        t_unknown = heart_fixture(TASK_UNKNOWN, BRIDGE_HEART + "-unknown", "approved")
        # unknown 夹具：批准态 + 未知收单标记（cloud id 有 = 可续查）
        task_u = json.loads(read_raw(t_unknown))
        task_u.update({"approval": {"at": "2026-01-02T00:00:00+08:00", "by": "probe",
                                    "snapshot_sha256": "x"},
                       "approval_snapshot": "x", "snapshot_version": 2,
                       "gen_params": {"duration": 8, "ratio": "16:9", "resolution": "720", "audio": True},
                       "gen_channel": {"baseUrl": "https://sdapi.probe.example.com", "apiFormat": "sdgf",
                                       "渠道名": "probe-SDGF", "model": "model-a", "mode": "frames"},
                       "submit_unknown": {"at": "2026-01-02T00:05:00+08:00",
                                          "reason": "提交结果未知：HTTP 502 渠道 403", "dispatch": "dispatched"},
                       "gen_error": "提交结果未知：HTTP 502 渠道 403",
                       "cloud_task_id": "cloud-unknown-1",
                       "dispatch": {"phase": "dispatched", "at": "2026-01-02T00:04:00+08:00"}})
        (t_unknown / "任务.json").write_text(json.dumps(task_u, ensure_ascii=False, indent=2), encoding="utf-8")

        t_dispatch = heart_fixture(TASK_DISPATCH, BRIDGE_HEART + "-disp", "approved")
        task_d = json.loads(read_raw(t_dispatch))
        task_d.update({"approval": {"at": "2026-01-02T00:00:00+08:00", "by": "probe", "snapshot_sha256": "x"},
                       "approval_snapshot": "x", "snapshot_version": 2,
                       "dispatch": {"phase": "prepared", "at": "2026-01-02T00:04:00+08:00"}})
        (t_dispatch / "任务.json").write_text(json.dumps(task_d, ensure_ascii=False, indent=2), encoding="utf-8")

        t_http_ok = heart_fixture(TASK_HTTP_OK, BRIDGE_HEART + "-http", "approved")
        task_h = json.loads(read_raw(t_http_ok))
        task_h.update({"approval": {"at": "2026-01-02T00:00:00+08:00", "by": "probe", "snapshot_sha256": "x"},
                       "approval_snapshot": "x", "snapshot_version": 2,
                       "gen_params": {"duration": 8, "ratio": "16:9", "resolution": "720", "audio": True},
                       "gen_channel": {"baseUrl": "https://sdapi.probe.example.com", "apiFormat": "sdgf",
                                       "渠道名": "probe-SDGF", "model": "model-a", "mode": "frames"}})
        (t_http_ok / "任务.json").write_text(json.dumps(task_h, ensure_ascii=False, indent=2), encoding="utf-8")

        # 只有 submit_unknown（无 dispatch）→ 后端走 unknown 分支（revoke 报「未知收单」）
        t_unknown_only = heart_fixture(TASK_UNKNOWN_ONLY, BRIDGE_HEART + "-uonly", "approved")
        task_uo = json.loads(read_raw(t_unknown_only))
        task_uo.update({"approval": {"at": "2026-01-02T00:00:00+08:00", "by": "probe", "snapshot_sha256": "x"},
                        "approval_snapshot": "x", "snapshot_version": 2,
                        "submit_unknown": {"at": "2026-01-02T00:06:00+08:00",
                                           "reason": "提交结果未知：连接被重置", "dispatch": ""},
                        "cloud_task_id": "cloud-unknown-2"})
        (t_unknown_only / "任务.json").write_text(json.dumps(task_uo, ensure_ascii=False, indent=2), encoding="utf-8")

        side_path = replace.sidecar_path(BRIDGE_HEART)
        side_bytes_before = side_path.read_bytes()
        side_decisions_before = replace.load_row_decisions(BRIDGE_HEART)

        # ── [1] 允许态全链 ───────────────────────────────────
        print("1) 允许态全链：待批准 → 批准 A → 撤销 → 批准 B（换 model）")
        heart_before = heart_bytes(t_flow)

        out_a = hub_api.do_approve(TASK_FLOW, config=config_of("model-a"))
        task_a = json.loads(read_raw(t_flow))
        hash_a = task_a["approval"]["snapshot_sha256"]
        check("[1] 批准 A：state=approved 且返回 snapshot_version=2", out_a.get("state") == "approved"
              and out_a.get("snapshot_version") == 2, json.dumps(out_a, ensure_ascii=False)[:200])
        check("[1] 批准 A：任务 snapshot_version 仍 2（不递增）", task_a.get("snapshot_version") == 2,
              str(task_a.get("snapshot_version")))
        check("[1] 批准 A：gen_channel.model=model-a、gen_params 按配置固化",
              (task_a.get("gen_channel") or {}).get("model") == "model-a"
              and (task_a.get("gen_params") or {}).get("duration") == 8,
              json.dumps(task_a.get("gen_channel"), ensure_ascii=False))
        prompt_now = (t_flow / "提示词.md").read_text(encoding="utf-8")
        check("[1] 批准 A：approval.snapshot_sha256 == state.snapshot_for(任务, 提示词)（内容快照真源）",
              hash_a == state_snapshot(task_a, prompt_now), f"{hash_a[:16]} vs {state_snapshot(task_a, prompt_now)[:16]}")

        bad_a = diff_heart(heart_before, heart_bytes(t_flow))
        check("[2] 批准 A 不动心血（资产确认 / 上传流水 / 置换确认 / 提示词 / 资产文件逐字节不变）",
              not bad_a, f"变了：{bad_a}")

        revoked = hub_api.revoke_approval(TASK_FLOW)
        task_r = json.loads(read_raw(t_flow))
        check("[1] 撤销：回 awaiting_approval", revoked.get("state") == "awaiting_approval"
              and task_r.get("state") == "awaiting_approval", str(revoked))
        check("[1] 撤销：approval / approval_snapshot 已清（批准作废必须重批）",
              "approval" not in task_r and "approval_snapshot" not in task_r,
              json.dumps({k: task_r.get(k) for k in ("approval", "approval_snapshot")}, ensure_ascii=False))
        check("[1] 撤销：保留 gen_params / gen_channel（草稿配置不丢，改配置从它开始）",
              bool(task_r.get("gen_params")) and (task_r.get("gen_channel") or {}).get("model") == "model-a",
              json.dumps(task_r.get("gen_channel"), ensure_ascii=False))
        bad_r = diff_heart(heart_before, heart_bytes(t_flow))
        check("[2] 撤销不动心血（硬断言：逐字节）", not bad_r, f"变了：{bad_r}")

        out_b = hub_api.do_approve(TASK_FLOW, config=config_of("model-b"))
        task_b = json.loads(read_raw(t_flow))
        hash_b = task_b["approval"]["snapshot_sha256"]
        check("[1] 批准 B（换 model）：内容快照 hash 改变", hash_b != hash_a, f"A={hash_a[:16]} B={hash_b[:16]}")
        check("[1] 批准 B：schema / snapshot 版本仍 2（不递增）",
              task_b.get("snapshot_version") == 2 and out_b.get("snapshot_version") == 2,
              str(task_b.get("snapshot_version")))
        check("[1] 批准 B：新配置生效（gen_channel.model=model-b）",
              (task_b.get("gen_channel") or {}).get("model") == "model-b",
              json.dumps(task_b.get("gen_channel"), ensure_ascii=False))
        check("[1] 批准 B：hash == 当前输入快照（换 model 就是换快照输入）",
              hash_b == state_snapshot(task_b, prompt_now))
        bad_b = diff_heart(heart_before, heart_bytes(t_flow))
        check("[2] 撤销→换模型→重批 全链不动心血（资产 / 提示词 / 资产文件逐字节不变）", not bad_b, f"变了：{bad_b}")

        # ── [2] sidecar 行决策 ───────────────────────────────
        print("2) sidecar 行决策：整条重批链不写决策（字节不变）")
        check("[2] sidecar 文件字节不变", side_path.read_bytes() == side_bytes_before)
        check("[2] 行决策读回一致（keep_origin / dismissed 没被重批改掉）",
              replace.load_row_decisions(BRIDGE_HEART) == side_decisions_before == {1: "keep_origin", 2: "dismissed"},
              str(replace.load_row_decisions(BRIDGE_HEART)))

        # ── [2] 运行时键集合封闭性 ───────────────────────────
        print("3) 只许运行时键变化（没有键凭空消失 / 乱改）")
        after_keys = set(task_b.keys())
        new_keys = sorted(after_keys - heart_before["keys"])
        check("[2] 新增键都在运行时白名单内（不新造持久字段）",
              all(k in RUNTIME_KEYS for k in new_keys), f"多出：{new_keys}")
        check("[2] 保护字段一个都没少", all(k in after_keys for k in HEART_KEYS),
              f"缺：{[k for k in HEART_KEYS if k not in after_keys]}")

        # ── [3] 提交用新配置 ─────────────────────────────────
        print("4) 提交用新配置：start_submit 冻结的就是批准 B 那份（do_submit 用测试桩，不发付费请求）")
        from automation import generate  # noqa: E402

        captured: dict = {}
        orig_do_submit = generate.do_submit

        def fake_do_submit(task_dir, verbose=False, on_progress=None, frozen=None,
                           write_guard=None, api_key=None):  # noqa: ANN001
            captured["frozen"] = frozen
            captured["api_key"] = api_key
            return {"fake": True}

        generate.do_submit = fake_do_submit
        try:
            job = hub_api.start_submit(TASK_FLOW, key="sk-probe")
            job_id = str(job.get("job_id") or "")
            for _ in range(100):
                rec = hub_api.HUB_JOBS.get(job_id) or {}
                if rec.get("status") in ("done", "failed"):
                    break
                time.sleep(0.05)
        finally:
            generate.do_submit = orig_do_submit
        rec = hub_api.HUB_JOBS.get(job_id) or {}
        check("[3] start_submit 受理（后台 job 跑完 done）", rec.get("status") == "done", json.dumps(rec, ensure_ascii=False)[:200])
        frozen = captured.get("frozen") or {}
        check("[3] 冻结配置 = 批准 B 的 gen_channel（model-b）",
              (frozen.get("gen_channel") or {}).get("model") == "model-b",
              json.dumps(frozen.get("gen_channel"), ensure_ascii=False))
        check("[3] 冻结配置 = 批准 B 的 gen_params", frozen.get("gen_params") == task_b.get("gen_params"),
              json.dumps(frozen.get("gen_params"), ensure_ascii=False))
        check("[3] key 一次性透传（不落盘：任务文件里没有 sk-probe）",
              "sk-probe" not in read_raw(t_flow) and captured.get("api_key") == "sk-probe")
        task_after_submit = json.loads(read_raw(t_flow))
        check("[3] 提交不改快照 / 不动心血（approval hash 仍是 B）",
              task_after_submit["approval"]["snapshot_sha256"] == hash_b
              and not diff_heart(heart_before, heart_bytes(t_flow)),
              f"hash={str(task_after_submit.get('approval', {}).get('snapshot_sha256'))[:16]}")

        # ── [4] 未知态不许引导撤销 ───────────────────────────
        print("5) 未知态：撤销被后端拒（所以 UI 绝不能引导撤销），人工解除走既有 release")
        expect_refused("[4] approved + dispatch=dispatched + submit_unknown：revoke 拒（dispatch 分支先命中）",
                       TASK_UNKNOWN, t_unknown, "解除阻断", lambda: hub_api.revoke_approval(TASK_UNKNOWN))
        expect_refused("[4] approved + 只有 submit_unknown：revoke 报「未知收单」且文件逐字节不变",
                       TASK_UNKNOWN_ONLY, t_unknown_only, "未知收单",
                       lambda: hub_api.revoke_approval(TASK_UNKNOWN_ONLY))
        expect_refused("[4] approved + dispatch=prepared：revoke 同样拒",
                       TASK_DISPATCH, t_dispatch, "dispatch=prepared",
                       lambda: hub_api.revoke_approval(TASK_DISPATCH))

        heart_unknown = heart_bytes(t_unknown)
        code, payload = probe_guard.http_req(base, f"/api/hub/task/{TASK_UNKNOWN}/release",
                                            method="POST", data=b"{}", ctype="application/json")
        check("[5] HTTP release 无 confirm → 400 且说明是人工动作",
              code == 400 and "confirm" in str((payload or {}).get("error") or ""),
              f"code={code} payload={json.dumps(payload, ensure_ascii=False)[:200]}")
        code, payload = probe_guard.http_req(base, f"/api/hub/task/{TASK_UNKNOWN}/release",
                                            method="POST",
                                            data=json.dumps({"confirm": True}).encode("utf-8"),
                                            ctype="application/json")
        task_u2 = json.loads(read_raw(t_unknown))
        check("[5] HTTP release 带 confirm → 200 且 submit_unknown 已清",
              code == 200 and not task_u2.get("submit_unknown"),
              f"code={code} payload={json.dumps(payload, ensure_ascii=False)[:200]}")
        check("[4] 解除后 dispatch 记 cleared（阻断真被解掉，不是只清标记）",
              (task_u2.get("dispatch") or {}).get("phase") == "cleared",
              json.dumps(task_u2.get("dispatch"), ensure_ascii=False))
        check("[2] 解除阻断不动心血（资产确认 / 置换 / 提示词逐字节不变）", not diff_heart(heart_unknown, heart_bytes(t_unknown)),
              f"变了：{diff_heart(heart_unknown, heart_bytes(t_unknown))}")
        check("[4] 解除后 history 记了这次人工动作",
              any("解除" in str((row or {}).get("note") or "") for row in (task_u2.get("history") or [])),
              json.dumps((task_u2.get("history") or [])[-1:], ensure_ascii=False)[:200])
        out_after_release = hub_api.revoke_approval(TASK_UNKNOWN)
        check("[4] 解除之后撤销才放行（UI 三步：核对 → 解除 → 改配置 在这层是通的）",
              out_after_release.get("state") == "awaiting_approval", str(out_after_release))

        # ── [5] HTTP 允许态撤销 ──────────────────────────────
        code, payload = probe_guard.http_req(base, f"/api/hub/task/{TASK_HTTP_OK}/revoke",
                                            method="POST", data=b"{}", ctype="application/json")
        check("[5] HTTP 允许态 revoke → 200 且 state=awaiting_approval",
              code == 200 and (payload or {}).get("state") == "awaiting_approval",
              f"code={code} payload={json.dumps(payload, ensure_ascii=False)[:200]}")

        # ── [6] 真实任务零丢失 + 不留垃圾 ────────────────────
        print("6) 真实数据根对账 + 夹具只删自建")
        real_after = probe_guard.projects_snapshot(real_root)
        try:
            probe_guard.assert_no_loss(real_before, real_after)
            check("[6] 真实项目目录集前后一致（零丢失 + 探针不留垃圾）", real_after == real_before,
                  f"多出 {sorted(set(real_after) - set(real_before))}" if real_after != real_before else "")
        except probe_guard.GuardError as err:
            check("[6] 真实项目目录集前后一致（零丢失 + 探针不留垃圾）", False, str(err))
        for fid in FIXTURE_IDS:
            ok, why = probe_guard.cleanup_created(iso_projects, fid, before=set(), created=set(FIXTURE_IDS))
            if not ok:
                print(f"     清理异常：{why}")
        check("[6] 隔离根夹具全部清干净", not any((iso_projects / fid).exists() for fid in FIXTURE_IDS))
        for bridge in (BRIDGE_HEART, BRIDGE_HEART + "-unknown", BRIDGE_HEART + "-disp",
                       BRIDGE_HEART + "-http", BRIDGE_HEART + "-uonly"):
            shutil.rmtree(replace.sidecar_path(bridge).parent, ignore_errors=True)
        side_after = sorted(p.name for p in side_root.glob("*")) if side_root.is_dir() else []
        check("[6] sidecar 夹具目录清干净（analysis/reverse 前后一致）", side_after == side_before,
              f"多出 {sorted(set(side_after) - set(side_before))}")
        return summary()
    finally:
        # 崩溃也要把 sidecar 夹具收干净（真源在 analysis/reverse/<bridge>/，不在隔离根里）
        for bridge in (BRIDGE_HEART, BRIDGE_HEART + "-unknown", BRIDGE_HEART + "-disp",
                       BRIDGE_HEART + "-http", BRIDGE_HEART + "-uonly"):
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


def state_snapshot(task: dict, prompt: str) -> str:
    """任务自身版本口径的「当前输入快照」（唯一入口 state.snapshot_for）。"""
    from automation import state

    return state.snapshot_for(task, prompt)


if __name__ == "__main__":
    raise SystemExit(main())
