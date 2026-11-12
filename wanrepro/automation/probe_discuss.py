# -*- coding: utf-8 -*-
"""AT-3 拆解执行器探针——覆盖 ROADMAP AT-3 全部验收判据。

判据：①done 后 has_* 三真 + 状态到 awaiting_assets；②产物齐不重跑（幂等）；
③环境级故障（假端口连不上）→ 抛错但状态保留 ingested（不误判 failed，重跑即续）。
[1] 用真视频真拆（视频拆解已授权自动执行；官号参考1 指纹命中复用产物，秒级）。
[3] 用 ffmpeg 现生成的逐字节唯一短片（幂等键=视频内容 sha，固定素材会命中 [1] 的任务）。
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
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


def make_unique_video(dst: Path) -> bool:
    """ffmpeg 现生成一条逐字节唯一的短视频（seed=时间戳+pid）——[3] 的独立素材。

    为什么必须换素材：ingest 的幂等键是**视频内容 sha256**（ingest.py `sha = hashlib.sha256(
    spec["video_bytes"])`），固定用 uploads/官号参考1.mp4 时 [3] 会命中 [1] 刚建的同视频任务
    （已 awaiting_assets），「状态保留 ingested」按构造就不可能成立——那是夹具债，不是产品行为。
    手法同 probe_at9_b1.make_unique_video；[3] 走到「桥连不上」就返回、不做真拆，故 2s 短片足够。
    """
    seed = int(time.time()) % 10 ** 8 + os.getpid() % 89   # gradients 的 seed 是有界整数，禁超 2^31
    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-f", "lavfi", "-i", f"gradients=size=360x640:duration=2:speed=0.1:seed={seed}",
           "-f", "lavfi", "-i", "testsrc2=size=360x640:rate=10:duration=2",
           "-filter_complex", "[0:v][1:v]blend=all_mode=grainmerge:all_opacity=0.35[v]",
           "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-t", "2", str(dst)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"  [FAIL] ffmpeg 生成唯一探针视频 —— {type(exc).__name__}: {exc}")
        return False
    good = proc.returncode == 0 and dst.is_file() and dst.stat().st_size > 0
    print(f"  [{'PASS' if good else 'FAIL'}] ffmpeg 生成唯一探针视频（seed=时间戳+pid，逐字节不重复）"
          + ("" if good else f" —— {(proc.stderr or '')[-200:]}"))
    return good


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
        # 独立素材：[3] 必须建**新**任务。幂等键=视频内容 sha，复用 [1] 的同一条片会命中旧任务
        # （awaiting_assets），「状态保留 ingested」就永不可能成立（历史夹具债，卡 t_d6733299）。
        uniq = tmp / f"probe-discuss-{int(time.time())}-{os.getpid()}.mp4"
        if not make_unique_video(uniq):
            ok = False
            uniq = None
        if uniq is not None:
            z3 = tmp / "t3.zip"
            with zipfile.ZipFile(z3, "w") as zf:
                zf.write(uniq, uniq.name)      # 每次换新素材 → 新视频 sha → 新任务
                zf.writestr("意图.txt", "失败路径探针")
            r3 = ingest_one(z3)
            tdir3 = config.PROJECTS_DIR / r3["local_id"]
            # 夹具自检（前置，不放宽任何判据）：若又命中幂等，下面两条断言必然假红，先在这里判死
            ok &= check("独立素材建了新任务（未幂等命中 [1] 的同视频任务）",
                        r3.get("result") == "OK" and r3.get("hit_existing") is False and tdir3 != tdir,
                        str(r3)[:220])
            real = config.BRIDGE_BASE
            config.BRIDGE_BASE = "http://127.0.0.1:1"  # 不存在端口
            try:
                run_discuss(tdir3, verbose=False)
                ok &= check("假端口应抛错", False, "居然没抛（环境故障被吞了）")
            except Exception as e:  # noqa: BLE001
                t3 = load(tdir3)
                ok &= check("抛的是环境错", "连不上" in str(e) or "Connection" in str(e), type(e).__name__)
                ok &= check("状态保留 ingested（不误判 failed）", t3["state"] == "ingested", t3["state"])
            finally:
                config.BRIDGE_BASE = real
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'ALL PASS' if ok else 'HAS FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(run())
