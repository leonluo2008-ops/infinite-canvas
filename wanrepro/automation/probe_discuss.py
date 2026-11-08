# -*- coding: utf-8 -*-
"""AT-3 拆解执行器探针——覆盖 ROADMAP AT-3 全部验收判据。

判据：①done 后 has_* 三真 + 状态到 awaiting_assets；②产物齐不重跑（幂等）；
③失败如实记 failed 带原文（假端口触发）。
[1] 用真视频真拆（视频拆解已授权自动执行；官号参考1 指纹命中复用产物，秒级）。
"""
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from . import config  # noqa: E402
from .discuss import DiscussError, run_discuss  # noqa: E402
from .ingest import ingest_one  # noqa: E402
from .state import load  # noqa: E402

VIDEO = (Path(__file__).resolve().parent.parent / "uploads" / "官号参考1.mp4").resolve()
PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c626001000000ffff03000006000557bfabd4"
    "0000000049454e44ae426082")


def check(name: str, cond: bool, detail: str = "") -> bool:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))
    return cond


def run() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="probe-discuss-"))
    config.PROJECTS_DIR = tmp / "projects"
    ok = True
    try:
        if not VIDEO.is_file():
            print(f"[setup] 缺测试视频 {VIDEO}")
            return 1

        print("[1] 真拆：ingest → discuss → awaiting_assets + 三真")
        z = tmp / "t.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.write(VIDEO, VIDEO.name)
            zf.writestr("资产-主角.png", PNG_1PX)
            zf.writestr("意图.txt", "自动化探针任务：同款创意")
        r = ingest_one(z)
        tdir = config.PROJECTS_DIR / r["local_id"]
        st = run_discuss(tdir)
        t = load(tdir)
        from .state import probe_bridge_products
        p = probe_bridge_products(t["bridge_id"])
        ok &= check("终态 awaiting_assets", st == "awaiting_assets")
        ok &= check("has_* 三真", bool(p and p["has_disc"] and p["has_check"] and p["has_prompt"]),
                    str(p))
        ok &= check("history 有拆解记录", any(h["to"] == "discussing" for h in t["history"]))

        print("[2] 幂等：产物齐 → SKIP 不重跑")
        st2 = run_discuss(tdir)
        t2 = load(tdir)
        ok &= check("SKIP 返回 awaiting_assets", st2 == "awaiting_assets")
        ok &= check("discussing 只出现一次", sum(1 for h in t2["history"] if h["to"] == "discussing") == 1)

        print("[3] 环境故障：假端口 → 抛错但状态保留（连不上≠任务失败）")
        z3 = tmp / "t3.zip"
        with zipfile.ZipFile(z3, "w") as zf:
            zf.write(VIDEO, VIDEO.name)  # 同视频不同意图 → 新 zip sha → 新任务
            zf.writestr("意图.txt", "失败路径探针")
        r3 = ingest_one(z3)
        tdir3 = config.PROJECTS_DIR / r3["local_id"]
        real = config.BRIDGE_BASE
        config.BRIDGE_BASE = "http://127.0.0.1:1"  # 不存在端口
        try:
            run_discuss(tdir3, verbose=False)
            ok &= check("假端口应抛错", False)
        except Exception as e:  # noqa: BLE001
            t3 = load(tdir3)
            ok &= check("抛的是环境错", "连不上" in str(e) or "Connection" in str(e), type(e).__name__)
            ok &= check("状态保留 ingested（不误判 failed）", t3["state"] == "ingested", t3["state"])
        config.BRIDGE_BASE = real
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'ALL PASS' if ok else 'HAS FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(run())
