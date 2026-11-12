#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AT-22 B0 探针：清单写锁统一（task_lock/WRITE_LOCK 合一）+ set_asset 状态守卫 + 并发不丢更新。

跑法（wanrepro/ 下）：`python automation/probe_at22_b0.py --isolated`，退出码 0 = 全绿。
**不带 `--isolated` 一律拒跑**（本探针会写清单/任务/侧车，写类探针只许在隔离根内跑）。

锁的是卡面验收（不是函数返回值好看）：

  [0] 隔离生效：清单路径 / 画布链根 / hub 任务根 / 配置任务根四处全部落在临时根内——
      否则「设了 env 却写真实目录」的伪隔离会把真实任务带进来（probe_guard 记的事故形态）；
  [1] 一把锁：主线程持 `replace.CHECKLIST_LOCK` 时，hub 链写原语（`replace.set_row`）与
      画布链写口（`server._update_checklist_cell`←`set_checklist_name`）**双双被挡在外面**，
      放锁后都能正常完成；反向再验一把：持 `server.WRITE_LOCK` **不再**阻塞清单写（两锁已解耦，
      清单只认 CHECKLIST_LOCK）；
  [2] 零丢失：两写者同起（Barrier）、同一行不同列（hub 写「资产图」列 × 画布写「命名」列），
      读→写窗口内各注入 80ms 让两者真重叠；多轮后每行两列都是最后一次写的值，
      「类别/描述」列与行集合逐字未变（整文件重写的丢更新会同时吃掉别人的列）；
  [3] 反向对照：把画布链侧临时换回「自有锁」（复现修前形态），同样的并发**必须丢更新**——
      证明 [2] 不是靠运气过的，探针对该缺陷是敏感的；
  [4] m-3 状态守卫：approved / generating 态下 image_url / clear / text 三支全 409（HubError，
      人话点名出口），清单与任务文件逐字节不变；awaiting_assets 照旧可写（守卫不误伤）；
  [5] 真实目录只读：真实 projects / analysis/reverse / uploads 跑前跑后**一条都没少**、
      本探针夹具号（auto-9900~9904）一条都没落进真实目录（其余新增按「同仓并发跑别人探针的痕迹」
      如实打印、不吞）；临时根跑完删净。

隔离手法：`--isolated` 下 `WANREPRO_AUTO_DIR` + `WANREPRO_ANALYSIS_ROOT` 指向本次临时根
（**在 import hub_api / ui.server 之前设**，两者与 automation.replace/state 同源认这两个变量）；
桥地址指向死端口（本探针不连桥）；夹具一律 auto-99xx，bridge_id = 夹具号，清单落在临时根内。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]                    # wanrepro/

# ── 写类探针硬约束：不带 --isolated 直接拒跑（连临时根都不建）──────────────
if "--isolated" not in sys.argv:
    print("[probe-at22-b0] 拒跑：本探针会写清单/任务/侧车，必须带 --isolated。\n"
          "  例：python automation/probe_at22_b0.py --isolated")
    raise SystemExit(2)

REAL_PROJECTS = ROOT / "automation" / "projects"
REAL_ANALYSIS = ROOT / "analysis" / "reverse"
REAL_UPLOADS = ROOT / "uploads"


def snapshot(root: Path) -> set[str]:
    try:
        return {p.name for p in root.iterdir()}
    except OSError:
        return set()


BEFORE = {"projects": snapshot(REAL_PROJECTS), "analysis": snapshot(REAL_ANALYSIS),
          "uploads": snapshot(REAL_UPLOADS)}

TMP_ROOT = Path(tempfile.mkdtemp(prefix="probe-at22-b0-"))
TMP_PROJECTS = TMP_ROOT / "projects"
TMP_ANALYSIS = TMP_ROOT / "_analysis"
TMP_PROJECTS.mkdir(parents=True, exist_ok=True)
TMP_ANALYSIS.mkdir(parents=True, exist_ok=True)
os.environ["WANREPRO_AUTO_DIR"] = str(TMP_PROJECTS)          # 必须在 import 之前
os.environ["WANREPRO_ANALYSIS_ROOT"] = str(TMP_ANALYSIS)
os.environ["WANREPRO_BRIDGE"] = "http://127.0.0.1:9"          # 本探针不连桥
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ui"))

import hub_api  # noqa: E402  hub 链（set_asset）
import server   # noqa: E402  画布链（set_checklist_*，ui/server.py）
from automation import config, replace  # noqa: E402

RESULTS: list[tuple[str, bool]] = []
LIST_NAME = "待确认清单.md"
# 本探针的夹具号（隔离根内自建；跑后要断言它们一条都没落进真实 projects）
FIXTURES = ("auto-9900", "auto-9901", "auto-9902", "auto-9903", "auto-9904")
HEADER = ("| # | 类别 | 模型看到什么 | 命名 | 资产图 | 已见 | 建议 | 用我的描述 |",
          "| --- | --- | --- | --- | --- | --- | --- | --- |")


def check(name: str, cond, detail: str = "") -> bool:
    ok = bool(cond)
    RESULTS.append((name, ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail and not ok else ""))
    return ok


def iso(delta: int = 0) -> str:
    return (datetime.now(timezone.utc).astimezone()
            + timedelta(seconds=delta)).isoformat(timespec="seconds")


def under_tmp(p) -> bool:
    try:
        return Path(p).resolve().is_relative_to(TMP_ROOT.resolve())
    except (OSError, AttributeError):
        return False


# ── 夹具 ────────────────────────────────────────────────────────────────
def seed(local_id: str, *, state_name: str = "awaiting_assets", rows: int = 4) -> Path:
    """隔离根里造一条任务 + 一份待确认清单（bridge_id = 任务号，两链写的就是同一份）。"""
    d = TMP_PROJECTS / local_id
    d.mkdir(parents=True, exist_ok=True)
    task = {"local_id": local_id, "state": state_name, "bridge_id": local_id,
            "created_at": iso(-600), "intent": "AT-22 B0 清单写锁探针夹具（隔离根内）",
            "history": [{"at": iso(-600), "from": "ingested", "to": state_name, "note": "夹具"}]}
    (d / "任务.json").write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
    bd = TMP_ANALYSIS / local_id
    bd.mkdir(parents=True, exist_ok=True)
    lines = ["# 待确认清单（AT-22 B0 探针夹具）", "", *HEADER]
    for n in range(1, rows + 1):
        lines.append(f"| {n} | 主体 | 描述{n} |  |  |  | 必须换 |  |")
    (bd / LIST_NAME).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return d


def set_state(d: Path, state_name: str) -> None:
    """探针造态：直接改任务.json 的 state（不走状态机，只为造出目标态的入口前置）。"""
    f = d / "任务.json"
    task = json.loads(f.read_text(encoding="utf-8"))
    task["state"] = state_name
    f.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")


def hub_ref(local_id: str, i: int) -> str:
    """合法本地引用串（local:任务号/asset-uploads/16位hex.png）。"""
    return f"local:{local_id}/asset-uploads/{i:016x}.png"


def canvas_name(i: int) -> str:
    return f"命名-{i:03d}"


# ── [0] 隔离生效 ─────────────────────────────────────────────────────────
def isolation_phase() -> bool:
    print("[0] 隔离生效：四处根都指向临时根（伪隔离 = 写真实目录）")
    lid = "auto-9900"
    seed(lid, rows=1)
    ok = True
    ok &= check("清单路径在临时根内", under_tmp(replace.checklist_path(lid)),
                str(replace.checklist_path(lid)))
    ok &= check("清单侧车路径在临时根内", under_tmp(replace.sidecar_path(lid)),
                str(replace.sidecar_path(lid)))
    ok &= check("画布链拆解根（server.ANALYSIS）在临时根内", under_tmp(server.ANALYSIS),
                str(server.ANALYSIS))
    ok &= check("hub 链任务根（hub_api.TASKS_DIR）在临时根内", under_tmp(hub_api.TASKS_DIR),
                str(hub_api.TASKS_DIR))
    ok &= check("配置任务根（config.PROJECTS_DIR）在临时根内", under_tmp(config.PROJECTS_DIR),
                str(config.PROJECTS_DIR))
    return ok


# ── [1] 一把锁 ───────────────────────────────────────────────────────────
def lock_identity_phase() -> bool:
    print("[1] 一把锁：两链写口都受 replace.CHECKLIST_LOCK 管，且不再受 WRITE_LOCK 管")
    lid = "auto-9901"
    seed(lid, rows=3)
    ok = True
    writers = (
        ("hub 链 replace.set_row",
         lambda: replace.set_row(lid, 1, image_urls=[hub_ref(lid, 101)], overwrite=True)),
        ("画布链 set_checklist_name",
         lambda: server.set_checklist_name(lid, 1, "命名-锁")),
    )
    for label, fn in writers:
        done: list[int] = []
        with replace.CHECKLIST_LOCK:
            t = threading.Thread(target=lambda f=fn: (f(), done.append(1)), daemon=True)
            t.start()
            t.join(0.4)
            blocked = not done
        t.join(10)
        ok &= check(f"持 CHECKLIST_LOCK 时 {label} 被挡住（同一把锁）", blocked,
                    "没挡住 = 这条链拿的不是同一把锁")
        ok &= check(f"放锁后 {label} 正常完成", bool(done), "放锁后还没完成 = 死等/异常")

    # 反向：清单写不再被 WRITE_LOCK 挡（否则说明画布链还在用旧锁，锁并未合一）
    done2: list[int] = []
    with server.WRITE_LOCK:
        t2 = threading.Thread(target=lambda: (server.set_checklist_name(lid, 2, "命名-W"),
                                             done2.append(1)), daemon=True)
        t2.start()
        t2.join(0.4)
        blocked_w = not done2
    t2.join(10)
    ok &= check("持 WRITE_LOCK 不阻塞清单写（画布链已改用共享锁）", not blocked_w,
                "仍被 WRITE_LOCK 挡住 = 画布链还在用旧锁")
    ok &= check("WRITE_LOCK 放掉后该写已完成", bool(done2), "异常未完成")
    return ok


# ── [2]/[3] 并发交错：零丢失 / 反向对照必须丢 ─────────────────────────────
class OverlapWindow:
    """把两条链「读 → 写」的窗口各拉长 delay 秒，让真重叠发生。

    注入点都在**锁内**且不改生产代码：hub 链 `replace.fill_row`（set_row 在读完 md 后调它）、
    画布链 `server.read_text`（_update_checklist_cell 读完 md 才逐行改）。修好了两写者被同一把
    锁串起来，睡多久都不会丢；没修好就会读到同一份旧内容、各自整文件重写 → 后写者吃掉先写者。
    """

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self._fill_row = None
        self._read_text = None

    def __enter__(self) -> "OverlapWindow":
        delay = self.delay
        self._fill_row = replace.fill_row
        self._read_text = server.read_text

        def slow_fill_row(md_text, no, asset_urls, name, overwrite=False, *args, **kwargs):
            # AT-22 E3：fill_row 新增 desc 关键字——代理必须透传，否则 B0 的并发窗口会把
            # 「签名变了」误报成「两链并发写异常」。
            time.sleep(delay)
            return self._fill_row(md_text, no, asset_urls, name, overwrite, *args, **kwargs)

        def slow_read_text(path):
            out = self._read_text(path)
            time.sleep(delay)
            return out

        replace.fill_row = slow_fill_row
        server.read_text = slow_read_text
        return self

    def __exit__(self, *exc) -> None:
        replace.fill_row = self._fill_row
        server.read_text = self._read_text


def run_interleaved(lid: str, *, rows: int, rounds: int) -> tuple[list[tuple[int, str, str]], list[str]]:
    """两写者同起（Barrier），第 i 轮都写行 (i%rows)+1：hub 写「资产图」× 画布写「命名」。"""
    errs: list[str] = []
    plan: list[tuple[int, str, str]] = []
    for i in range(rounds):
        plan.append(((i % rows) + 1, canvas_name(i), hub_ref(lid, i)))
    for n, nm, ref in plan:
        barrier = threading.Barrier(2)

        def hub_worker(no=n, url=ref):
            barrier.wait()
            try:
                replace.set_row(lid, no, image_urls=[url], overwrite=True)
            except Exception as err:                    # noqa: BLE001 —— 失败如实记，不静默
                errs.append(f"hub #{no}: {err!r}")

        def canvas_worker(no=n, name=nm):
            barrier.wait()
            try:
                server.set_checklist_name(lid, no, name)
            except Exception as err:                    # noqa: BLE001
                errs.append(f"canvas #{no}: {err!r}")

        t1 = threading.Thread(target=hub_worker, daemon=True)
        t2 = threading.Thread(target=canvas_worker, daemon=True)
        t1.start()
        t2.start()
        t1.join(30)
        t2.join(30)
        if t1.is_alive() or t2.is_alive():
            errs.append(f"第 {n} 轮有写者 30s 未返回（疑似死锁）")
    return plan, errs


def verify_md(lid: str, rows: int, plan: list[tuple[int, str, str]]) -> list[str]:
    """对照最后一轮期望值逐行核对：命名 + 资产图都要在，别的列一个字节不许动。"""
    path = replace.checklist_path(lid)
    got = {r["no"]: r for r in replace.parse_rows(path.read_text(encoding="utf-8"))}
    want_name = {n: nm for n, nm, _ in plan}
    want_assets = {n: ref for n, _, ref in plan}
    lost: list[str] = []
    if sorted(got) != list(range(1, rows + 1)):
        lost.append(f"行集合变了：{sorted(got)}")
    for n in range(1, rows + 1):
        row = got.get(n) or {}
        if row.get("name") != want_name[n]:
            lost.append(f"#{n} 命名丢了：{row.get('name')!r} ≠ {want_name[n]!r}")
        if row.get("cur_assets") != want_assets[n]:
            lost.append(f"#{n} 资产图丢了：{row.get('cur_assets')!r} ≠ {want_assets[n]!r}")
        if row.get("desc") != f"描述{n}":
            lost.append(f"#{n} 描述被吃掉：{row.get('desc')!r}")
        if row.get("cat") != "主体":
            lost.append(f"#{n} 类别被吃掉：{row.get('cat')!r}")
    return lost


class _SeparateLockView:
    """复现修前形态：画布链侧看不到 replace.CHECKLIST_LOCK，拿的是自己的一把新锁。"""

    CHECKLIST_LOCK = threading.Lock()


def concurrency_phase() -> bool:
    print("[2] 并发交错写同一份清单：零丢失（hub 写资产图列 × 画布写命名列，读→写窗口各 80ms）")
    rows, rounds = 4, 12
    lid = "auto-9902"
    seed(lid, rows=rows)
    with OverlapWindow(0.08):
        plan, errs = run_interleaved(lid, rows=rows, rounds=rounds)
    ok = check("[2] 两链并发写无异常/无死锁", not errs, "; ".join(errs[:3]))
    lost = verify_md(lid, rows, plan)
    ok &= check(f"[2] {rounds} 轮 × {rows} 行零丢失（命名+资产图双列俱在，其余列未动）",
                not lost, "; ".join(lost[:5]))
    print(f"      清单现状（{rounds} 轮后）：")
    for row in replace.parse_rows(replace.checklist_path(lid).read_text(encoding="utf-8")):
        print(f"        #{row['no']} 命名={row['name']!r} 资产图={row['cur_assets']!r} 描述={row['desc']!r}")

    print("[3] 反向对照：把画布链换回「自有锁」→ 同样并发**必须丢更新**（证明探针敏感）")
    lid2 = "auto-9903"
    seed(lid2, rows=3)
    old_view = server.auto_replace
    server.auto_replace = _SeparateLockView()
    try:
        with OverlapWindow(0.08):
            plan2, errs2 = run_interleaved(lid2, rows=3, rounds=6)
    finally:
        server.auto_replace = old_view
    lost2 = verify_md(lid2, 3, plan2)
    ok &= check("[3] 修前形态（两把锁）确实丢更新 → 探针对该缺陷敏感",
                bool(lost2), "复现不出丢更新 = 探针测不到这个缺陷，[2] 的绿不足信")
    if lost2:
        print(f"      反向对照丢的东西（预期）：{lost2[0]}" + (f" 等 {len(lost2)} 处" if len(lost2) > 1 else ""))
    check("[3] 反向对照无异常", not errs2, "; ".join(errs2[:3]))
    return ok


# ── [4] m-3 状态守卫 ─────────────────────────────────────────────────────
def state_guard_phase() -> bool:
    print("[4] m-3 状态守卫：approved / generating 拒改清单，awaiting_assets 照旧可写")
    lid = "auto-9904"
    d = seed(lid, rows=3)
    md = TMP_ANALYSIS / lid / LIST_NAME
    task_file = d / "任务.json"
    ok = True
    cases = (
        ("approved · 重传图", "approved", {"image_url": hub_ref(lid, 201)}),
        ("approved · 沿用清空", "approved", {"clear": True}),
        ("approved · 改命名", "approved", {"text": "改个名"}),
        ("generating · 重传图", "generating", {"image_url": hub_ref(lid, 202)}),
        ("generating · 改命名", "generating", {"text": "改个名"}),
    )
    for label, state_name, kwargs in cases:
        set_state(d, state_name)
        before_md, before_task = md.read_bytes(), task_file.read_bytes()
        rejected, msg, status = False, "", None
        try:
            hub_api.set_asset(lid, 1, **kwargs)
        except hub_api.HubError as err:
            rejected, msg, status = True, str(err), getattr(err, "status", None)
        except Exception as err:                        # noqa: BLE001 —— 非 HubError 也是红
            msg = f"{type(err).__name__}: {err}"
        ok &= check(f"[4] {label} → 409 拒绝", rejected and status == 409, msg or "竟然写成功了")
        ok &= check(f"[4] {label} 拒绝文案点名真实出口", ("锁定" in msg) and ("撤销批准" in msg), msg)
        ok &= check(f"[4] {label} 后清单逐字节不变", md.read_bytes() == before_md, "清单被改了")
        ok &= check(f"[4] {label} 后任务文件逐字节不变", task_file.read_bytes() == before_task,
                    "任务文件被改了（不该落任何流水）")

    set_state(d, "awaiting_assets")
    out = hub_api.set_asset(lid, 2, image_url=hub_ref(lid, 203))
    ok &= check("[4] awaiting_assets 照旧可写（守卫不误伤正常路径）", out.get("ok") is True, str(out)[:120])
    ok &= check("[4] awaiting_assets 写后清单里出现该引用", hub_ref(lid, 203) in md.read_text(encoding="utf-8"),
                "没写进清单")
    return ok


# ── 入口 ────────────────────────────────────────────────────────────────
def main() -> int:
    print(f"[probe-at22-b0] 隔离根 {TMP_ROOT}")
    ok = True
    try:
        ok &= isolation_phase()
        ok &= lock_identity_phase()
        ok &= concurrency_phase()
        ok &= state_guard_phase()
    finally:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)

    after = {"projects": snapshot(REAL_PROJECTS), "analysis": snapshot(REAL_ANALYSIS),
             "uploads": snapshot(REAL_UPLOADS)}
    for name in ("projects", "analysis", "uploads"):
        lost = sorted(BEFORE[name] - after[name])
        ok &= check(f"[5] 真实 {name} 没有条目消失（不删真实数据）", not lost, f"丢了 {lost}")
        mine = sorted(x for x in (after[name] - BEFORE[name]) if x in FIXTURES)
        ok &= check(f"[5] 本探针夹具没落进真实 {name}", not mine, f"夹具落进真实目录：{mine}")
        extra = sorted((after[name] - BEFORE[name]) - set(mine))
        if extra:
            print(f"      注：{name} 跑期间另有新增 {extra[:6]}{'…' if len(extra) > 6 else ''}"
                  f"（非本探针产物：同仓并发跑别的探针/任务的痕迹，本探针唯一写点是隔离根）")
    ok &= check("[5] 临时根已删净", not TMP_ROOT.exists(), str(TMP_ROOT))

    passed = sum(1 for _, good in RESULTS if good)
    total = len(RESULTS)
    print(f"\n[probe-at22-b0] {passed}/{total} PASS")
    return 0 if (ok and passed == total) else 1


if __name__ == "__main__":
    raise SystemExit(main())
