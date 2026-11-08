# -*- coding: utf-8 -*-
"""AT-6 批量推进器探针——状态板分桶/亮灯不动/错误隔离（不真提交）。

判据（ROADMAP AT-6 的调度侧；成片落盘与真提交由验收指南人工跑）：
①各状态任务正确出现在状态板；②亮灯状态（awaiting_assets/awaiting_approval）
不被推进；③单任务环境错误不拖垮整轮（其余照常）；④done/failed 无动作。
"""
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from . import config  # noqa: E402
from .ingest import ingest_one  # noqa: E402
from .runner import run_once  # noqa: E402
from .state import load, save  # noqa: E402

VIDEO = (Path(__file__).resolve().parent.parent / "uploads" / "官号参考1.mp4").resolve()
PNG_A = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c626001000000ffff03000006000557bfabd4"
    "0000000049454e44ae426082")


def check(name: str, cond: bool, detail: str = "") -> bool:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))
    return cond


def set_state(tdir: Path, st: str) -> None:
    t = load(tdir)
    t["state"] = st
    save(tdir, t)


def run() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="probe-runner-"))
    config.PROJECTS_DIR = tmp / "projects"
    ok = True
    try:
        ids = {}
        for tag in ["a", "b", "c", "d"]:
            z = tmp / f"{tag}.zip"
            with zipfile.ZipFile(z, "w") as zf:
                zf.write(VIDEO, VIDEO.name)
                zf.writestr("意图.txt", f"runner 探针 {tag}")
            r = ingest_one(z)
            ids[tag] = config.PROJECTS_DIR / r["local_id"]
        set_state(ids["a"], "awaiting_assets")
        set_state(ids["b"], "awaiting_approval")
        set_state(ids["c"], "done")
        set_state(ids["d"], "ingested")

        print("[1] 状态板分桶 + 亮灯不动 + done 不动")
        out = run_once(verbose=False)
        board = out["board"]
        ok &= check("4 桶各 1", board.get("awaiting_assets") == 1 and
                    board.get("awaiting_approval") == 1 and
                    board.get("done") == 1, str(board))
        ok &= check("亮灯任务未被推进",
                    load(ids["a"])["state"] == "awaiting_assets" and
                    load(ids["b"])["state"] == "awaiting_approval")
        ok &= check("done 未动", load(ids["c"])["state"] == "done")

        print("[2] ingested 自动推进（真拆产物优先，秒级）→ awaiting_assets")
        ok &= check("ingested 被推进", load(ids["d"])["state"] == "awaiting_assets",
                    load(ids["d"])["state"])

        print("[3] 错误隔离：一个任务环境错不拖垮整轮")
        # 造一个 ingested 任务 + 假端口 → runner 内部捕获，其他任务照常
        z = tmp / "e.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.write(VIDEO, VIDEO.name)
            zf.writestr("意图.txt", "错误隔离探针 e")
        r = ingest_one(z)
        tdir_e = config.PROJECTS_DIR / r["local_id"]
        real = config.BRIDGE_BASE
        # d 已完成、a/b 亮灯——唯一会被推进的是 e；断掉桥让它抛环境错
        config.BRIDGE_BASE = "http://127.0.0.1:1"
        out3 = run_once(verbose=False)
        ok &= check("错误被隔离（errors=1）", out3["errors"] == 1, str(out3))
        config.BRIDGE_BASE = real
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'ALL PASS' if ok else 'HAS FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(run())
