# -*- coding: utf-8 -*-
"""AT-4 置换接收器探针——覆盖 ROADMAP AT-4 全部验收判据（真桥真上传，免费）。

判据：①上传+回填+回显全链留痕；②未确认不进 rendering（负例）；
③清单命名/URL 回填一致；④--confirm → rendering；⑤--map 错误行号拒。
"""
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from . import config  # noqa: E402
from .discuss import run_discuss  # noqa: E402
from .ingest import ingest_one  # noqa: E402
from .replace import (LIST_NAME, ReplaceError, checklist_path,  # noqa: E402
                      run_replace)
from .state import load  # noqa: E402

VIDEO = (Path(__file__).resolve().parent.parent / "uploads" / "官号参考1.mp4").resolve()
PNG_A = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c626001000000ffff03000006000557bfabd4"
    "0000000049454e44ae426082")
ROOT = Path(__file__).resolve().parent.parent


def check(name: str, cond: bool, detail: str = "") -> bool:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))
    return cond


def run() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="probe-replace-"))
    config.PROJECTS_DIR = tmp / "projects"
    ok = True
    try:
        for f, b in [("a.png", PNG_A), ("b.png", PNG_A)]:
            (tmp / f).write_bytes(b)
        z = tmp / "t.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.write(VIDEO, VIDEO.name)
            zf.writestr("资产-主角.png", PNG_A)
            zf.writestr("意图.txt", "置换探针")
        r = ingest_one(z)
        tdir = config.PROJECTS_DIR / r["local_id"]
        run_discuss(tdir, verbose=False)  # 产物优先：官号参考1 已拆过，秒级
        bid = load(tdir)["bridge_id"]
        assert load(tdir)["state"] == "awaiting_assets"

        print("[1] 回填：上传→公网 URL→清单行→回显留痕")
        st = run_replace(tdir, assets=[str(tmp / "a.png")], map_spec=None,
                         name_spec="1=我们的产品", confirm=False, verbose=True)
        t = load(tdir)
        md = checklist_path(bid).read_text(encoding="utf-8")
        row1 = next(ln for ln in md.splitlines() if re.match(r"^\|\s*1\s*\|", ln.replace("｜", "|")))
        ok &= check("状态仍 awaiting_assets（未确认不推进）", st == "awaiting_assets")
        ok &= check("清单 #1 资产图列含 https URL", "https://" in row1.split("|")[5])
        ok &= check("清单 #1 命名列=我们的产品", "我们的产品" in row1.split("|")[4])
        ok &= check("pending_replace 留痕", bool(t.get("pending_replace")))

        print("[2] 负例：未回填就 --confirm 拒；状态不推进")
        z2 = tmp / "t2.zip"
        with zipfile.ZipFile(z2, "w") as zf:
            zf.write(VIDEO, VIDEO.name)
            zf.writestr("意图.txt", "负例任务")
        r2 = ingest_one(z2)
        tdir2 = config.PROJECTS_DIR / r2["local_id"]
        run_discuss(tdir2, verbose=False)
        try:
            run_replace(tdir2, assets=None, map_spec=None, name_spec=None, confirm=True)
            ok &= check("无回填 confirm 应拒", False)
        except ReplaceError as e:
            ok &= check("无回填 confirm 拒", "没有待确认" in str(e), str(e)[:60])

        print("[3] --map 显式配对 + 错行号拒")
        try:
            run_replace(tdir2, assets=[str(tmp / "b.png")], map_spec="999=b.png",
                        name_spec=None, confirm=False, verbose=False)
            ok &= check("行号 999 应拒", False)
        except ReplaceError as e:
            ok &= check("行号 999 拒", "不在清单" in str(e), str(e)[:60])

        print("[4] --confirm → rendering")
        st4 = run_replace(tdir, assets=None, map_spec=None, name_spec=None,
                          confirm=True, verbose=False)
        t4 = load(tdir)
        ok &= check("终态 rendering", st4 == "rendering")
        ok &= check("assets_confirmed 留痕", bool(t4.get("assets_confirmed")))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'ALL PASS' if ok else 'HAS FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(run())
