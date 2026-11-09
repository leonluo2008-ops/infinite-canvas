# -*- coding: utf-8 -*-
"""AT-2 状态机探针——覆盖 ROADMAP AT-2 全部验收判据（纯本地，无需 bridge）。

判据：①非法迁移被拒（跳级/未确认续跑负例）；②审批必带快照；
③审批后改输入自动作废；④kill 后重跑从断点续跑（状态文件可恢复）；
⑤failed 任意态可进；⑥原子写无残留。
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from .state import (StateError, approval_stale, load,  # noqa: E402
                    save, snapshot_hash, transition)


def check(name: str, cond: bool, detail: str = "") -> bool:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))
    return cond


def new_task(tmp: Path, n: int, state: str = "ingested") -> Path:
    d = tmp / f"auto-{n:04d}"
    d.mkdir(parents=True)
    save(d, {"local_id": d.name, "bridge_id": f"t{n}", "created_at": "now",
             "state": state, "history": [], "intent": "", "assets": []})
    return d


def run() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="probe-state-"))
    ok = True
    try:
        print("[1] 正向链逐级迁移 → done")
        t = new_task(tmp, 1)
        for s in ["discussing", "awaiting_assets"]:
            transition(t, s)
        transition(t, "rendering", require={"assets_confirmed": True})
        transition(t, "awaiting_approval")
        transition(t, "approved", require={"approval.snapshot_sha256": "x" * 64},
                   note="所有者批准")
        transition(t, "generating")
        transition(t, "done")
        ok &= check("终态 done", load(t)["state"] == "done")
        ok &= check("history 完整 7 条迁移", len(load(t)["history"]) == 7)

        print("[2] 跳级/倒退被拒")
        t2 = new_task(tmp, 2)
        for bad in ["rendering", "approved", "done", "discussing2"]:
            try:
                transition(t2, bad)
                ok &= check(f"ingested→{bad} 应拒", False)
            except StateError:
                ok &= check(f"ingested→{bad} 拒", True)

        print("[2b] failed 唯一出口=人工重试拆解（2026-11-09）")
        t2b = new_task(tmp, 7)
        transition(t2b, "failed", note="模拟失败")
        try:
            transition(t2b, "discussing", note="人工重试拆解")
            ok &= check("failed→discussing 过（人工重试）", True)
        except StateError as e:
            ok &= check(f"failed→discussing 应过: {e}", False)
        for i, bad in enumerate(["awaiting_assets", "rendering", "done"]):
            t2c = new_task(tmp, 10 + i)
            transition(t2c, "failed", note="模拟失败")
            try:
                transition(t2c, bad)
                ok &= check(f"failed→{bad} 应拒", False)
            except StateError:
                ok &= check(f"failed→{bad} 拒", True)

        print("[3] 审批停点守卫：approved 不带快照被拒（未确认直接续跑负例）")
        t3 = new_task(tmp, 3)
        for s in ["discussing", "awaiting_assets"]:
            transition(t3, s)
        transition(t3, "rendering", require={"assets_confirmed": True})
        transition(t3, "awaiting_approval")
        try:
            transition(t3, "approved", note="没带快照")
            ok &= check("无快照批准应拒", False)
        except StateError as e:
            ok &= check("无快照批准拒", "审批快照" in str(e), str(e))
        try:
            transition(t3, "generating")
            ok &= check("跳过 approved 直接 generating 应拒", False)
        except StateError:
            ok &= check("跳过 approved 直接 generating 拒", True)
        try:
            transition(t3, "rendering", require={"assets_confirmed": True})
            ok &= check("approved 前倒回 rendering 应拒", False)
        except StateError:
            ok &= check("倒退 rendering 拒", True)

        print("[4] 审批后改输入 → 自动作废")
        sha1 = snapshot_hash("提示词A", {"duration": 5})
        transition(t3, "approved", require={"approval.snapshot_sha256": sha1})
        ok &= check("批准时快照一致 → 不作废", not approval_stale(t3, sha1))
        sha2 = snapshot_hash("提示词B", {"duration": 5})
        ok &= check("prompt 改动作废", approval_stale(t3, sha2))
        sha3 = snapshot_hash("提示词A", {"duration": 10})
        ok &= check("参数改动作废", approval_stale(t3, sha3))

        print("[5] kill 恢复：断点状态可读可续")
        t5 = new_task(tmp, 5, state="awaiting_assets")
        d5 = load(t5)  # 模拟进程重启后读回
        ok &= check("恢复读到 awaiting_assets", d5["state"] == "awaiting_assets")
        transition(t5, "rendering", require={"assets_confirmed": True})  # 断点续跑也过守卫
        ok &= check("断点续迁成功", load(t5)["state"] == "rendering")

        print("[6] failed 任意态可进 + 原子写")
        t6 = new_task(tmp, 6, state="generating")
        transition(t6, "failed", note="HTTP 500 原文")
        ok &= check("failed 任意态可进 + 无 .tmp 残留",
                    load(t6)["state"] == "failed" and not list(tmp.glob("auto-*/*.tmp")))
        bad = tmp / "auto-9999"
        bad.mkdir()
        (bad / "任务.json").write_text("{不是json", encoding="utf-8")
        try:
            load(bad)
            ok &= check("坏文件应拒", False)
        except StateError:
            ok &= check("坏文件拒读", True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'ALL PASS' if ok else 'HAS FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(run())
