#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""存量假运行任务对账入口（AT-18 C1）——扫描 → 判定 → （可选）降级 → 报告。

跑法（wanrepro/ 下）：

    python automation/reconcile_lost_runs.py              # dry-run，只读，打清单
    python automation/reconcile_lost_runs.py --apply      # 真的把判定为假运行的降级为 failed
    python automation/reconcile_lost_runs.py --json       # 机器可读（探针/自动化用）
    python automation/reconcile_lost_runs.py --root <dir> # 指到别的任务根（默认 WANREPRO_AUTO_DIR / projects）

为什么要这个入口：进程/服务重启后，`judge_lost_run` 只依赖持久时间戳（心跳时刻、history 时刻、
文件 mtime），所以**存量卡死任务在服务起来的那一刻就能判**，不需要等后台看门狗跑 300s 周期。
本脚本就是「人/运维主动对一次账」的入口，和后台 watchdog、HTTP scan 接口共用同一套判定函数：
它**不重算静默、不自造文案**，直接调 `ui.hub_api.scan_lost_runs()`（该函数内部只调
`automation/state.py` 的 `activity_snapshot` / `judge_lost_run` / `fail_orphan_task`）。

安全边界（方案 §5.3）：
  · dry-run 是**纯读**（只调快照 + 判定，不碰 `fail_orphan_task`）；
  · `--apply` 只写 5 个键（state / history / failed_reason / failed_at / failed_evidence）；
  · 不删任务目录、不删拆解产物、不动参考视频、不动 `lapian_*` / pending_replace / approval；
  · 单条坏任务进 `errors[]`，其余照扫（一次读不了的任务不该把整轮对账打断）。

退出码：0 = 本轮完成（含「有任务被判假运行但 dry-run 没降」）；1 = 有任务读不了（errors 非空）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]          # wanrepro/


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="存量假运行任务对账（默认 dry-run，只读）；--apply 才降级为 failed",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="真的降级判定为假运行的任务（默认只打清单）")
    ap.add_argument("--json", action="store_true", dest="as_json",
                    help="输出机器可读 JSON（探针/自动化用），默认打中文报告")
    ap.add_argument("--root", default=None,
                    help="任务根（默认 automation.config.PROJECTS_DIR，认 WANREPRO_AUTO_DIR）")
    return ap.parse_args(argv)


def _import_hub_api(root: str | None):
    """按最终任务根 import 桥端业务层（config.PROJECTS_DIR 在 import 时读 env，必须先设）。"""
    if root:
        os.environ["WANREPRO_AUTO_DIR"] = str(Path(root).expanduser().resolve())
    for p in (str(ROOT), str(ROOT / "ui")):
        if p not in sys.path:
            sys.path.insert(0, p)
    import hub_api                       # noqa: PLC0415 —— 必须在 env 设定之后
    return hub_api


def _report(result: dict, task_root: Path, applied: bool) -> None:
    lost = list(result.get("lost") or [])
    skipped = list(result.get("skipped") or [])
    errors = list(result.get("errors") or [])

    # 跳过原因按 reason_code 聚合计数（报告口径是「原因」，不是「任务数」）
    reasons: dict[str, int] = {}
    for item in skipped:
        code = str(item.get("reason_code") or "unknown")
        reasons[code] = reasons.get(code, 0) + 1

    degraded = sum(1 for item in lost if item.get("changed"))

    print(f"[对账] 任务根：{task_root}")
    print(f"[对账] 模式：{'apply（真的降级）' if applied else 'dry-run（只读，不改任何任务）'}")
    print(f"[对账] 冻结阈值：{int(result.get('threshold_seconds') or 0)} 秒"
          f"（state.FAKE_RUN_SILENCE_SECONDS，改动须回方案评审）")
    print(f"[对账] 检查时间：{result.get('checked_at') or ''}")
    print(f"[对账] 扫描数（运行态任务）：{int(result.get('scanned') or 0)}")
    print(f"[对账] 判定假运行数：{len(lost)}")
    print(f"[对账] 实际降级数：{degraded}"
          + ("（dry-run 恒为 0——清单确认后加 --apply）" if not applied else ""))
    if reasons:
        print("[对账] 跳过原因：")
        for code, n in sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"         {code}: {n}")
    else:
        print("[对账] 跳过原因：无")

    if lost:
        print("[对账] 假运行清单：")
        for item in lost:
            print(f"         {item.get('local_id')}  {item.get('state')}"
                  f"  静默 {int(item.get('silence_seconds') or 0)}s"
                  f"  最近活动 {item.get('last_activity_at') or '（无证据）'}"
                  f"（{item.get('last_activity_source') or '—'}）"
                  f"  原因：{item.get('reason')}"
                  f"  changed={bool(item.get('changed'))}")
    if skipped:
        print("[对账] 跳过清单：")
        for item in skipped:
            print(f"         {item.get('local_id')}  {item.get('state')}"
                  f"  → {item.get('reason_code')}")
    if errors:
        print("[对账] 错误（单条坏任务不打断整轮，其余已照扫）：")
        for item in errors:
            print(f"         {item.get('local_id')}: {item.get('error')}")
    print(f"[对账] 小结：扫描 {int(result.get('scanned') or 0)} / 判定 {len(lost)}"
          f" / 降级 {degraded} / 跳过 {len(skipped)} / 错误 {len(errors)}")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    hub_api = _import_hub_api(args.root)
    task_root = Path(hub_api.TASKS_DIR)
    result = hub_api.scan_lost_runs(apply=bool(args.apply))
    if args.as_json:
        print(json.dumps({"task_root": str(task_root), **result}, ensure_ascii=False, indent=2))
    else:
        _report(result, task_root, bool(args.apply))
    return 1 if result.get("errors") else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
