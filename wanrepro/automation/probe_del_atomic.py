#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""删除通道原子性探针（卡 t_4f779245 验收口）。

跑法（wanrepro/ 下）：`python -m automation.probe_del_atomic`，退出码 0 = 全绿。

锁的是**真机会撞到的那三种结局**，不是函数返回值好看：

  [1] 无占用 → 整删：200、任务目录消失、墓碑在案（含 run_id）、隔离箱不留空壳。
  [2] 墓碑先行：注入墓碑写失败 → 409 且**零删除**（目录逐文件完整），任务仍可管。
  [3] Windows 占用（本进程真持 拆解日志.txt 的追加句柄）→ 409 人话：
      目录逐文件集合+内容与删前完全一致、墓碑**没有**该 id、任务仍在 API 里；释放句柄后
      同一个删除立刻成功 —— 「要么完整存在、要么完整消失」的正面证据。
  [4] 清箱失败（注入 `_rmtree_retry`）→ 200 + trash_pending：任务从 API 完整消失、墓碑在案、
      残留只在 `.trash/`（内含完整 任务.json）；`sweep_residue` dry-run 不动、apply 清干净。
  [5] 孤儿目录清扫：事故形态（只有日志、无 任务.json）被回收；**摄取中**（含 inbox/）与
      **新鲜**目录两条负例不动。
  [6] 无重建 + 隔离：删成功后以旧路径模拟执行体写盘（state.save / heartbeat / 日志追加）不再
      造出半任务；真实 projects/analysis/uploads 顶层集合跑前跑后逐字一致。

隔离手法：`WANREPRO_AUTO_DIR` / `WANREPRO_ANALYSIS_ROOT` → 本次临时根（任务根唯一来源
`automation.config.PROJECTS_DIR`）；夹具一律 auto-98xx；不起子进程、不连桥；跑完整个临时根删掉。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]                    # wanrepro/
TMP_ROOT = Path(tempfile.mkdtemp(prefix="probe-del-atomic-"))
TMP_ANALYSIS = TMP_ROOT / "_analysis"
REAL_PROJECTS = ROOT / "automation" / "projects"
REAL_ANALYSIS = ROOT / "analysis" / "reverse"
REAL_UPLOADS = ROOT / "uploads"

os.environ["WANREPRO_AUTO_DIR"] = str(TMP_ROOT)               # 必须在 import hub_api 之前
os.environ["WANREPRO_ANALYSIS_ROOT"] = str(TMP_ANALYSIS)
os.environ["WANREPRO_BRIDGE"] = "http://127.0.0.1:9"           # 本探针不连桥
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ui"))

import hub_api  # noqa: E402
from automation import state  # noqa: E402

RESULTS: list[tuple[str, bool]] = []
LOG_NAME = "拆解日志.txt"
CHILD_LOG_NAME = "拆解子进程日志.txt"


def check(name: str, cond, detail: str = "") -> bool:
    ok = bool(cond)
    RESULTS.append((name, ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail and not ok else ""))
    return ok


def iso(delta: int = 0) -> str:
    return (datetime.now(timezone.utc).astimezone()
            + timedelta(seconds=delta)).isoformat(timespec="seconds")


def snapshot(root: Path) -> set[str]:
    try:
        return {p.name for p in root.iterdir()}
    except OSError:
        return set()


BEFORE = {"projects": snapshot(REAL_PROJECTS), "analysis": snapshot(REAL_ANALYSIS),
          "uploads": snapshot(REAL_UPLOADS)}


def seed(local_id: str, state_name: str = "failed", *, run_id: str = "probe-run-del",
         with_logs: bool = True, bridge_id: str = "") -> Path:
    """隔离根里造一条任务；带心跳侧车（run_id / pid 是墓碑与占用诊断的对账锚）。"""
    d = TMP_ROOT / local_id
    d.mkdir(parents=True, exist_ok=True)
    task = {"local_id": local_id, "state": state_name, "bridge_id": bridge_id,
            "created_at": iso(-5000), "intent": "删除通道原子性探针夹具（隔离根内）",
            "history": [{"at": iso(-600), "from": "ingested", "to": state_name, "note": "夹具"}]}
    (d / "任务.json").write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
    (d / "心跳.json").write_text(json.dumps({
        "schema": 1, "run_id": run_id, "started_at": iso(-1200),
        "last_beat_at": iso(-3), "beat_count": 11, "phase": "拆解中",
        "artifact_count": 0, "artifact_digest": "", "ended_at": "", "outcome": "",
        "exec": "process", "pid": os.getpid(), "pid_created_at": "", "exit_code": None,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    if with_logs:
        (d / LOG_NAME).write_text("第一行日志\n第二行日志\n", encoding="utf-8")
        (d / CHILD_LOG_NAME).write_text("子进程日志\n", encoding="utf-8")
    return d


def fingerprint(d: Path) -> dict[str, str]:
    """目录内逐文件 → sha256（集合 + 内容双比，不依赖 NTFS 名字序）。"""
    out: dict[str, str] = {}
    for p in sorted(Path(d).rglob("*")):
        if p.is_file():
            out[str(p.relative_to(d)).replace("\\", "/")] = \
                hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def tombstones() -> list[dict]:
    f = TMP_ROOT / ".tombstones.json"
    if not f.is_file():
        return []
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return [x for x in data if isinstance(x, dict)]
    except (OSError, json.JSONDecodeError):
        return []


def tomb_of(local_id: str) -> list[dict]:
    return [t for t in tombstones() if t.get("id") == local_id]


def delete(local_id: str) -> tuple[str, dict, int | None]:
    """调删除 → (verdict, 回执|错误, 状态码)。状态码取 HubError.status（server 层据此回 HTTP）。"""
    try:
        return "ok", hub_api.delete_task(local_id), 200
    except hub_api.HubError as exc:
        return "err", {"error": str(exc)}, int(getattr(exc, "status", 0) or 0)
    except Exception as exc:                       # noqa: BLE001 —— 非 HubError 就是「会变成 500」的形态
        return "crash", {"error": f"{type(exc).__name__}: {exc}"}, None


def listed(local_id: str) -> bool:
    return any(t.get("local_id") == local_id for t in hub_api.list_tasks())


def main() -> int:
    print("=== [1] 无占用 → 整删（提交点=改名进隔离箱）===")
    local = "auto-9801"
    d = seed(local, "failed", run_id="probe-run-del")
    verdict, resp, code = delete(local)
    check("[1a] 删除 200 / deleted 回执", verdict == "ok" and resp.get("deleted") == local,
          f"{verdict} {resp}")
    check("[1a] 任务目录真的没了", not d.exists())
    check("[1a] 墓碑在案 + 记 run_id（编号不复用、活执行体退场锚）",
          any(t.get("run_id") == "probe-run-del" for t in tomb_of(local)), str(tomb_of(local)))
    check("[1a] 回执声明整删完成（trash_pending=False）",
          resp.get("trash_pending") is False and resp.get("trash_path") == "", str(resp.get("trash_pending")))
    check("[1a] 隔离箱不留空壳（删净后箱子自身移除）", not hub_api.TRASH_DIR.exists(),
          str(hub_api.TRASH_DIR))
    check("[1a] 删除后 API 里查不到（_task_dir 报任务不存在）",
          _raises_huberror(lambda: hub_api._task_dir(local)))
    # 旧回执键一个不许少（审计 I5：只增不改）
    keys = {"ok", "deleted", "state_was", "analysis_dir_removed", "uploads_video_removed",
            "uploads_video", "uploads_video_source", "uploads_video_skip", "tombstone",
            "executor_alive", "executor_run_id", "ignored_stale_jobs"}
    check("[1a] 旧回执键一个不少（只增不改）", keys <= set(resp), str(sorted(keys - set(resp))))

    print("=== [2] 墓碑先行：墓碑写不成 ⇒ 零删除 + 409 ===")
    local = "auto-9802"
    d = seed(local, "failed")
    before = fingerprint(d)
    original_append = hub_api._tombstones_append

    def _boom(*a, **kw):
        raise OSError("injected: 墓碑文件写不了")

    hub_api._tombstones_append = _boom
    try:
        verdict, resp, code = delete(local)
    finally:
        hub_api._tombstones_append = original_append
    check("[2a] 墓碑写失败 → 409（不是 500/非 HubError 崩溃）",
          verdict == "err" and code == 409, f"{verdict} {code} {resp}")
    check("[2a] 文案说清「墓碑写入失败 + 未做任何删除」",
          "墓碑写入失败" in str(resp.get("error")) and "没动" in str(resp.get("error")),
          str(resp.get("error")))
    check("[2a] 目录逐文件集合+内容完全没变", d.is_dir() and fingerprint(d) == before,
          f"{sorted(fingerprint(d))} vs {sorted(before)}")
    check("[2a] 墓碑里没有它（注入点在写入之前，本次没写出任何条目）", tomb_of(local) == [],
          str(tomb_of(local)))
    verdict, resp, code = delete(local)
    check("[2b] 墓碑恢复可写后同一个删除即可成功（不是永久锁死）",
          verdict == "ok" and not d.exists(), f"{verdict} {resp}")

    # 2c：墓碑文件损坏 → 拒绝覆盖写（审计 IMPL-3：把「读不懂」当空表会一次抹掉全部历史条目）
    local = "auto-9811"
    d = seed(local, "failed")
    before = fingerprint(d)
    tomb_file = TMP_ROOT / ".tombstones.json"
    good_tombs = tomb_file.read_text(encoding="utf-8") if tomb_file.is_file() else "[]"
    tomb_file.write_text('[{"id": "auto-9700", "时间": "坏' , encoding="utf-8")
    verdict, resp, code = delete(local)
    check("[2c] 墓碑文件损坏 → 409 中止（不许把历史条目覆盖成只剩新条目）",
          verdict == "err" and code == 409 and "墓碑写入失败" in str(resp.get("error")),
          f"{verdict} {code} {resp}")
    check("[2c] 损坏文件原样保留（没被覆盖）",
          tomb_file.read_text(encoding="utf-8").startswith('[{"id": "auto-9700"'),
          tomb_file.read_text(encoding="utf-8")[:60])
    check("[2c] 任务目录零删除", d.is_dir() and fingerprint(d) == before)
    tomb_file.write_text(good_tombs, encoding="utf-8")
    verdict, resp, code = delete(local)
    check("[2c] 墓碑修复后同一个删除即可成功", verdict == "ok" and not d.exists(), f"{verdict} {resp}")

    # 2d：等任务文件锁超时（执行体在途写盘）→ 409 换口径，不诬指「文件被占用」
    local = "auto-9812"
    d = seed(local, "failed")
    before = fingerprint(d)
    original_lock = hub_api.state._file_lock

    def _busy(td):
        raise state.StateError("任务文件锁等待超时（5.0s）：夹具注入")

    hub_api.state._file_lock = _busy
    try:
        verdict, resp, code = delete(local)
    finally:
        hub_api.state._file_lock = original_lock
    err = str(resp.get("error") or "")
    check("[2d] 文件锁忙 → 409 + 换口径（「执行体正在写任务文件」而非「被占用」）",
          verdict == "err" and code == 409 and "执行体正在写任务文件" in err and "整目录搬不走" not in err,
          f"{verdict} {code} {err}")
    check("[2d] 零删除 + 墓碑已回滚",
          d.is_dir() and fingerprint(d) == before and tomb_of(local) == [],
          f"exists={d.is_dir()} tomb={tomb_of(local)}")

    # 2e：校验通过后源目录被别人删掉（FileNotFoundError 不是「占用」）→ 幂等成功 + 墓碑保留
    local = "auto-9813"
    d = seed(local, "failed", run_id="probe-run-vanish")
    original_lock = hub_api.state._file_lock

    class _VanishLock:
        def __init__(self, td):
            self.td = td

        def __enter__(self):
            shutil.rmtree(self.td, ignore_errors=True)   # 等锁期间目录被别人删掉

        def __exit__(self, *exc):
            return False

    hub_api.state._file_lock = lambda td: _VanishLock(td)
    try:
        verdict, resp, code = delete(local)
    finally:
        hub_api.state._file_lock = original_lock
    check("[2e] 目录已不在 → 按幂等成功收尾（200 + task_dir_missing）",
          verdict == "ok" and resp.get("task_dir_missing") is True, f"{verdict} {resp}")
    check("[2e] 墓碑保留（目录消失 ⇔ 墓碑在，编号不复用）",
          any(t.get("id") == local for t in tomb_of(local)), str(tomb_of(local)))
    check("[2e] 没有被误报成「被占用」（审计 IMPL-1 的回归锁）",
          "被占用" not in str(resp.get("error") or ""), str(resp.get("error")))

    print("=== [3] Windows 占用（本进程真持日志句柄）→ 409 + 目录完整 ===")
    local = "auto-9803"
    d = seed(local, "failed", run_id="probe-run-busy")
    handle = open(d / LOG_NAME, "a", encoding="utf-8")          # 与事故形态同款：追加句柄不释放
    handle.write("executor still writing\n")                    # 先让执行体写一行（快照必须取在写之后）
    handle.flush()
    before = fingerprint(d)
    verdict, resp, code = delete(local)
    err = str(resp.get("error") or "")
    check("[3a] 占用时删除 → 409 人话（绝不是 PermissionError/500）",
          verdict == "err" and code == 409, f"{verdict} {code} {err}")
    check("[3a] 文案点名被占用文件 + 说明零删除",
          LOG_NAME in err and "未做任何删除" in err, err)
    check("[3a] 文案不谎称「谁持有文件」，只报心跳是否新鲜",
          "心跳仍新鲜" in err and "正持有" not in err, err)
    check("[3b] 目录逐文件集合+内容与删前完全一致（一个字节没动）",
          d.is_dir() and fingerprint(d) == before,
          f"missing={sorted(set(before) - set(fingerprint(d)))} extra={sorted(set(fingerprint(d)) - set(before))}")
    check("[3b] 墓碑里没有它（拒删不假装删）", tomb_of(local) == [], str(tomb_of(local)))
    check("[3b] 任务仍在 API 列表里（可管、可再删）", listed(local))
    check("[3b] _task_dir 仍可取", hub_api._task_dir(local) == d)
    check("[3b] 隔离箱没留残货（409 路径也清空箱）", not hub_api.TRASH_DIR.exists())
    handle.close()
    verdict, resp, code = delete(local)
    check("[3c] 句柄释放后同一个删除 → 200 且目录消失",
          verdict == "ok" and not d.exists(), f"{verdict} {resp}")
    check("[3c] 这次墓碑补齐（含 run_id）",
          any(t.get("run_id") == "probe-run-busy" for t in tomb_of(local)), str(tomb_of(local)))

    print("=== [4] 清箱失败 → trash_pending（任务已消失，残货只在隔离箱）===")
    local = "auto-9804"
    d = seed(local, "failed", run_id="probe-run-trash")
    original_rmtree = hub_api._rmtree_retry
    hub_api._rmtree_retry = lambda *a, **kw: (False, "injected: PermissionError WinError 32")
    try:
        verdict, resp, code = delete(local)
    finally:
        hub_api._rmtree_retry = original_rmtree
    trash_path = Path(str(resp.get("trash_path") or "")) if resp.get("trash_path") else None
    check("[4a] 改名成功即算删成：200 + trash_pending=True",
          verdict == "ok" and resp.get("trash_pending") is True, f"{verdict} {resp}")
    check("[4a] 原位任务目录已完整消失", not d.exists())
    check("[4a] 残留只在隔离箱里，且**含完整 任务.json**（不是半删）",
          trash_path is not None and trash_path.is_dir() and (trash_path / "任务.json").is_file(),
          str(resp.get("trash_path")))
    check("[4a] 墓碑在案（API 消失 ⇔ 墓碑在，二者一致）",
          len(tomb_of(local)) == 1, str(tomb_of(local)))
    check("[4a] 任务不在 API 列表里", not listed(local))
    dry = hub_api.sweep_residue(apply=False, trash_min_age=0)
    check("[4b] 清扫器 dry-run 只报不动（pending=dry_run，残货还在）",
          any(x.get("name") == trash_path.name and x.get("reason") == "dry_run"
              for x in dry.get("pending") or []) and trash_path.is_dir(), str(dry.get("pending"))[:300])
    done = hub_api.sweep_residue(apply=True, trash_min_age=0)
    check("[4c] 清扫器 apply 把残货清干净",
          any(x.get("name") == trash_path.name for x in done.get("removed") or [])
          and not trash_path.exists(), str(done.get("removed"))[:300])
    check("[4c] 箱空后箱子自身被移除", not hub_api.TRASH_DIR.exists())

    print("=== [5] 孤儿目录清扫：事故形态回收，摄取中/新鲜两条负例不动 ===")
    old = time.time() - 3600
    orphan = TMP_ROOT / "auto-9805"                              # 事故形态：只剩日志
    orphan.mkdir(parents=True)
    (orphan / LOG_NAME).write_text("残留日志\n", encoding="utf-8")
    (orphan / CHILD_LOG_NAME).write_text("残留日志\n", encoding="utf-8")
    os.utime(orphan, (old, old))
    ingesting = TMP_ROOT / "auto-9806"                            # 摄取中：inbox/ 有了，任务.json 还没写
    (ingesting / "inbox").mkdir(parents=True)
    (ingesting / "inbox" / "probe.mp4").write_bytes(b"\x00" * 32)
    os.utime(ingesting, (time.time() - 120, time.time() - 120))
    stale_inbox = TMP_ROOT / "auto-9809"                          # 很老的 inbox 残货：只上报，不自动删
    (stale_inbox / "inbox").mkdir(parents=True)
    (stale_inbox / "inbox" / "probe.mp4").write_bytes(b"\x00" * 32)
    os.utime(stale_inbox, (old - 3600, old - 3600))
    fresh = TMP_ROOT / "auto-9807"                                # 新鲜孤儿：可能是正在崩的路径
    fresh.mkdir(parents=True)
    (fresh / LOG_NAME).write_text("刚写的\n", encoding="utf-8")
    sweep = hub_api.sweep_residue(apply=True)
    removed_names = {x.get("name") for x in sweep.get("removed") or []}
    pending = {x.get("name"): x.get("reason") for x in sweep.get("pending") or []}
    check("[5a] 事故形态孤儿目录被回收", "auto-9805" in removed_names and not orphan.exists(),
          str(removed_names))
    check("[5b] 摄取中（含 inbox/）不动 —— 审计 BLOCKING-2 的负例",
          ingesting.is_dir() and pending.get("auto-9806") == "ingest_in_progress",
          f"exists={ingesting.is_dir()} reason={pending.get('auto-9806')}")
    check("[5b] 很老的 inbox 残货也只上报（inbox_stale_manual），仍不自动删",
          stale_inbox.is_dir() and pending.get("auto-9809") == "inbox_stale_manual",
          f"exists={stale_inbox.is_dir()} reason={pending.get('auto-9809')}")
    check("[5c] 新鲜孤儿不动（静置阈值）",
          fresh.is_dir() and pending.get("auto-9807") == "too_young",
          f"exists={fresh.is_dir()} reason={pending.get('auto-9807')}")
    check("[5d] 清扫回执带判据阈值（可解释、可复核）",
          sweep.get("orphan_min_age_seconds") == hub_api.ORPHAN_MIN_AGE_SECONDS
          and sweep.get("trash_dir") == str(hub_api.TRASH_DIR), str(sweep.get("orphan_min_age_seconds")))
    shutil.rmtree(fresh, ignore_errors=True)

    print("=== [6] 删成功之后不会被执行体写盘「复活」+ 隔离 ===")
    local = "auto-9808"
    d = seed(local, "failed", run_id="probe-run-late")
    verdict, resp, code = delete(local)
    check("[6a] 前提：删除成功", verdict == "ok" and not d.exists(), f"{verdict} {resp}")
    check("[6a] 迟到的 state.save 不重建目录（抛错且目录不存在）",
          _raises_oserror(lambda: state.save(d, {"local_id": local, "state": "failed"})) and not d.exists())
    check("[6a] 迟到的心跳不重建目录", _raises_oserror(lambda: state.heartbeat_beat(d)) and not d.exists())
    check("[6a] 迟到的日志追加不重建目录",
          _raises_oserror(lambda: open(d / LOG_NAME, "a", encoding="utf-8")) and not d.exists())
    for key in BEFORE:
        after = snapshot({"projects": REAL_PROJECTS, "analysis": REAL_ANALYSIS,
                          "uploads": REAL_UPLOADS}[key])
        check(f"[6b] 真实 {key}/ 顶层集合跑前跑后一致", BEFORE[key] == after,
              f"-{sorted(BEFORE[key] - after)} +{sorted(after - BEFORE[key])}")
    check("[6c] 任务根 / 分析根自述都指向本次临时根（没有写进真实目录）",
          str(hub_api.TASKS_DIR) == str(TMP_ROOT)
          and str(hub_api._analysis_reverse()).startswith(str(TMP_ROOT)),
          f"{hub_api.TASKS_DIR} / {hub_api._analysis_reverse()}")

    passed = sum(1 for _, ok in RESULTS if ok)
    print(f"\n{'ALL PASS' if passed == len(RESULTS) else 'FAILED'} —— {passed}/{len(RESULTS)}")
    return 0 if passed == len(RESULTS) else 1


def _raises_huberror(fn) -> bool:
    try:
        fn()
    except hub_api.HubError as exc:
        return "任务不存在" in str(exc)
    except Exception:                              # noqa: BLE001
        return False
    return False


def _raises_oserror(fn) -> bool:
    try:
        fn()
    except OSError:
        return True
    except Exception:                              # noqa: BLE001 —— 别的异常不算「拒绝写盘」
        return False
    return False


if __name__ == "__main__":
    code = 1
    try:
        code = main()
    finally:
        hub_api.HUB_JOBS.clear()
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
    sys.exit(code)
