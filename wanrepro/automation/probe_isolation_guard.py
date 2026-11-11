# -*- coding: utf-8 -*-
"""隔离目录守卫探针（AT-14 B2 P0 闭合项）：证明「设 WANREPRO_AUTO_DIR 起的实例真隔离」。

事故形状（2026-11-11）：`ui/hub_api.py#TASKS_DIR` 硬编码真实目录，`automation.config` 认 env——
同一个进程里**读真实、写隔离**；空隔离目录首投拿到 auto-0001，探针按真实目录删掉了真实任务。

本探针自己起一个隔离实例（端口自选、`WANREPRO_AUTO_DIR` 指向临时目录），只断言、不碰真实目录：

正例
  P1 `/api/hub/where` 自述的 tasks_dir == 隔离目录（服务端认 env）
  P2 隔离实例任务列表为空（真实目录非空 ⇒ 证明列表不是从真实目录读的）
  P3 首投落在隔离目录（任务.json 存在），真实 projects 一条不动
  P4 隔离实例列表恰好只有这条（读路径也真的走隔离目录）
  P5 同源守卫对「写 A 读 B」**拒跑**（GuardError，事故形状）
  P6 同源守卫对不存在自述的服务端**拒跑**
反例（必须红）
  N1 清理守卫拒绝删除「跑前已存在」的任务（真实任务保护）
  N2 清理守卫拒绝删除「非本次新建」的任务
  N3 对照：旧裸 rmtree 语义在同样输入下会把跑前已存在的目录删掉（演示缺陷，用临时副本）

跑法：wanrepro/ 下 `py -3 -m automation.probe_isolation_guard`。exit 0=全绿。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from . import config  # noqa: E402
from .probe_guard import (  # noqa: E402
    GuardError,
    StubBridge as _StubBridge,
    assert_same_root,
    cleanup_created,
    free_port,
    http_req,
    multipart_body,
    projects_snapshot,
)

ROOT = Path(__file__).resolve().parents[1]          # wanrepro/
UPLOADS_DIR = ROOT / "uploads"
SOURCE_VIDEO = UPLOADS_DIR / "官号参考1.mp4"        # 只读：源片是唯一一份

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" —— {detail}" if detail and not cond else ""))


def _scratch() -> Path:
    return Path(os.environ.get("TMPDIR") or tempfile.gettempdir())


def _wait_ready(base: str, proc: subprocess.Popen, timeout: float = 90.0) -> tuple[dict, str]:
    """等隔离实例的 /api/hub/where 起来 → (自述, 启动日志尾巴)。"""
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"隔离实例提前退出 rc={proc.returncode}\n{_drain(proc)}")
        s, d = http_req(base, "/api/hub/where", timeout=5)
        if s == 200 and isinstance(d, dict):
            return d, last
        last = f"{s} {str(d)[:120]}"
        time.sleep(0.7)
    raise RuntimeError(f"隔离实例 {timeout:.0f}s 内没就绪：{last}\n{_drain(proc)}")


def _drain(proc: subprocess.Popen) -> str:
    if proc.stdout is None:
        return ""
    try:
        proc.terminate()
        out = proc.communicate(timeout=10)[0]
    except Exception:  # noqa: BLE001 —— 拿不到日志不该盖住主错
        out = ""
    return (out or "").strip()[-2000:]


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def main() -> int:
    real_root = Path(config.PROJECTS_DIR)
    real_before = projects_snapshot(real_root)
    uploads_before = {p.name for p in UPLOADS_DIR.glob("hub-incoming*")} if UPLOADS_DIR.is_dir() else set()
    print(f"[iso-guard] 真实 projects：{sorted(real_before)}（只读基线）")
    if not SOURCE_VIDEO.is_file():
        print(f"[iso-guard] 缺源片 {SOURCE_VIDEO}，探针不跑（fail fast）")
        return 1

    iso_root = Path(tempfile.mkdtemp(prefix="wanprobe-iso-", dir=str(_scratch())))
    iso_projects = iso_root / "projects"
    iso_projects.mkdir(parents=True, exist_ok=True)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    stub = _StubBridge()
    env = dict(os.environ, WANREPRO_AUTO_DIR=str(iso_projects), WANREPRO_BRIDGE=stub.base)
    print(f"[iso-guard] 隔离根 {iso_projects} → 实例 {base}；假 bridge {stub.base}"
          "（真实 bridge / 真实 uploads 全程只读不碰）")

    proc: subprocess.Popen | None = None
    try:
        stub.thread.start()
        proc = subprocess.Popen(
            [sys.executable, str(ROOT / "ui" / "server.py"), "--port", str(port), "--no-browser"],
            cwd=str(ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        )
        try:
            where, _ = _wait_ready(base, proc)
        except RuntimeError as err:
            check("隔离实例起得来（/api/hub/where 就绪）", False, str(err))
            print(f"\n[iso-guard] 0/{len(RESULTS)} PASS")
            return 1
        check("隔离实例起得来（/api/hub/where 就绪）", True)
        print(f"     自述：tasks_dir={where.get('tasks_dir')} auto_dir_env={where.get('auto_dir_env')}")

        # ── P1 服务端认 env ────────────────────────────────
        iso_claim = Path(str(where.get("tasks_dir", ""))).resolve()
        check("P1 服务端自述 tasks_dir == 隔离目录（TASKS_DIR 认 WANREPRO_AUTO_DIR）",
              iso_claim == iso_projects.resolve(), f"自述 {iso_claim} ≠ 隔离 {iso_projects.resolve()}")
        check("P1b 自述 auto_dir_env == 传入的 WANREPRO_AUTO_DIR",
              where.get("auto_dir_env") == str(iso_projects), str(where.get("auto_dir_env")))

        # ── P2 空隔离：列表为空（真实目录非空） ─────────────
        s, d = http_req(base, "/api/hub/tasks")
        tasks = d.get("tasks") if isinstance(d, dict) else None
        check("P2 隔离实例列表为空（读的是隔离目录，不是真实目录）",
              s == 200 and tasks == [], f"{s} {str(d)[:160]}")
        check("P2b 基线有效：真实目录里确有任务（否则 P2 无鉴别力）", len(real_before) > 0,
              f"真实 projects={sorted(real_before)}")

        # ── P3 首投落隔离目录，真实目录不动 ─────────────────
        body, ctype = multipart_body({"intent": "iso-guard 首投"}, {"video": (SOURCE_VIDEO.name, SOURCE_VIDEO.read_bytes())})
        s, d = http_req(base, "/api/hub/ingest", "POST", body, ctype, timeout=180)
        created = d.get("local_id") if isinstance(d, dict) else None
        check("P3 首投 → 200 + local_id（隔离实例真建任务）", s == 200 and bool(created),
              f"{s} {str(d)[:200]}")
        if created:
            tpath = iso_projects / created / "任务.json"
            check(f"P3b 任务落在隔离目录 {iso_projects.name}/{created}/任务.json", tpath.is_file(), str(tpath))
            check("P3c 首投走的是假 bridge（未给真实 bridge 建任务）",
                  len(stub.ids) == 1 and (d or {}).get("bridge_id") == stub.ids[0],
                  f"stub ids={stub.ids} bridge_id={(d or {}).get('bridge_id')}")
            real_mid = projects_snapshot(real_root)
            check("P3d 首投不进真实目录（真实 projects 集合不变）", real_mid == real_before,
                  f"before={sorted(real_before)} after={sorted(real_mid)}")
            # ── P4 读路径也走隔离 ──────────────────────────
            s2, d2 = http_req(base, "/api/hub/tasks")
            ids = [t.get("local_id") for t in (d2.get("tasks") or [])] if isinstance(d2, dict) else []
            check("P4 隔离实例列表恰好只有这条（读写同源）", ids == [created], f"{ids}")

        # ── P5/P6 同源守卫拒跑（反例必须红） ────────────────
        try:
            assert_same_root(where, iso_root / "another-projects")
            check("P5 同源守卫对「写隔离/读别处」拒跑", False, "没抛 GuardError（守卫失效）")
        except GuardError as err:
            check("P5 同源守卫对「写隔离/读别处」拒跑", True)
            print(f"     [拒跑样本] {err}")
        if real_root.resolve() != iso_projects.resolve():
            try:
                assert_same_root({"tasks_dir": str(real_root)}, iso_projects)
                check("P5b 事故形状（服务端写真实/探针读隔离）拒跑", False, "没抛 GuardError")
            except GuardError as err:
                check("P5b 事故形状（服务端写真实/探针读隔离）拒跑", True)
                print(f"     [拒跑样本] {err}")
        try:
            assert_same_root({}, iso_projects)
            check("P6 无自述的服务端（旧版/端点缺失）拒跑", False, "没抛 GuardError")
        except GuardError as err:
            check("P6 无自述的服务端（旧版/端点缺失）拒跑", True)
            print(f"     [拒跑样本] {err}")

        # ── N1/N2 清理守卫拒绝误删 ─────────────────────────
        if created:
            protected = iso_projects / created
            deleted, why = cleanup_created(iso_projects, created, before={created}, created={created})
            check("N1 清理守卫拒绝删除「跑前已存在」的任务（真实任务保护）",
                  deleted is False and protected.is_dir(), why)
            print(f"     [拒删样本] {why}")
            deleted2, why2 = cleanup_created(iso_projects, created, before=frozenset(), created=frozenset())
            check("N2 清理守卫拒绝删除「非本次新建」的任务",
                  deleted2 is False and protected.is_dir(), why2)
            print(f"     [拒删样本] {why2}")
            # 真正允许的路径仍然可用：本次新建 ∧ 跑前不存在 → 删掉（收尾也靠它）
            deleted3, why3 = cleanup_created(iso_projects, created, before=frozenset(), created={created})
            check("N2b 允许路径仍可删「本次新建 ∧ 跑前不存在」", deleted3 is True and not protected.exists(), why3)

        # ── N3 对照：旧裸 rmtree 语义会误删（缺陷演示，临时副本） ─
        legacy = iso_root / "legacy-sim"
        (legacy / "auto-0001").mkdir(parents=True)
        (legacy / "auto-0001" / "任务.json").write_text("{}", encoding="utf-8")
        before_set = projects_snapshot(legacy)
        # 旧代码：只要 d["result"]=="OK" 就直接 rmtree(root/created)——不看 id 归属
        shutil.rmtree(legacy / "auto-0001", ignore_errors=True)
        check("N3 对照：旧裸 rmtree 在同样输入下删掉了跑前已存在的目录（缺陷可复现）",
              not (legacy / "auto-0001").exists() and "auto-0001" in before_set)
    finally:
        if proc is not None:
            _stop(proc)
        stub.httpd.shutdown()
        stub.httpd.server_close()
        shutil.rmtree(iso_root, ignore_errors=True)

    # ── 收尾：真实目录 & uploads 残留 ───────────────────────
    real_after = projects_snapshot(real_root)
    check("真实 projects 一条不动（跑后 == 跑前）", real_after == real_before,
          f"before={sorted(real_before)} after={sorted(real_after)}")
    uploads_after = {p.name for p in UPLOADS_DIR.glob("hub-incoming*")} if UPLOADS_DIR.is_dir() else set()
    check("uploads 无新增 hub-incoming 残留（ingest 两段式已收尾）", uploads_after <= uploads_before,
          f"新增 {sorted(uploads_after - uploads_before)}")

    fails = [r for r in RESULTS if not r[1]]
    print(f"\n[iso-guard] {len(RESULTS) - len(fails)}/{len(RESULTS)} PASS")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
