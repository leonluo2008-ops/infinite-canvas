# -*- coding: utf-8 -*-
"""AT-5 续跑与审批探针——render/approve 守卫全覆盖；submit 只测守卫不花钱。

判据（ROADMAP AT-5）：①render 渲染真提示词落盘+预演参数+快照；
②非 approved 状态 submit 被拒（探针可验）；③approve 绑定快照、改输入作废；
④缺渠道配置 submit 拒（不盲提交）。
真提交（confirm=true）属付费动作：由所有者验收时按测试指南真跑，不进探针。
"""
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from . import config  # noqa: E402
from .discuss import run_discuss  # noqa: E402
from .generate import (DEFAULT_TEMPLATE, GEN_POLL_MAX, GenError,  # noqa: E402
                       do_approve, do_render, do_submit)
from .ingest import ingest_one  # noqa: E402
from .replace import run_replace  # noqa: E402
from .state import load, snapshot_hash  # noqa: E402

VIDEO = (Path(__file__).resolve().parent.parent / "uploads" / "官号参考1.mp4").resolve()
PNG_A = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c626001000000ffff03000006000557bfabd4"
    "0000000049454e44ae426082")


def check(name: str, cond: bool, detail: str = "") -> bool:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))
    return cond


def make_ready(tmp: Path, tag: str) -> Path:
    """建任务→拆解→置换→确认→rendering。返回任务目录。"""
    z = tmp / f"{tag}.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.write(VIDEO, VIDEO.name)
        zf.writestr("资产.png", PNG_A)
        zf.writestr("意图.txt", f"{tag} 任务")
    r = ingest_one(z)
    tdir = config.PROJECTS_DIR / r["local_id"]
    run_discuss(tdir, verbose=False)
    img = tmp / f"{tag}.png"
    img.write_bytes(PNG_A)
    run_replace(tdir, assets=[str(img)], map_spec=None, name_spec=None,
                confirm=False, verbose=False)
    run_replace(tdir, assets=None, map_spec=None, name_spec=None,
                confirm=True, verbose=False)
    return tdir


def run() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="probe-gen-"))
    config.PROJECTS_DIR = tmp / "projects"
    ok = True
    try:
        import os
        os.environ.pop("WANREPRO_SDGF_CHANNEL", None)  # 探针不提交，防误读

        print("[1] render：真渲染 → awaiting_approval + 提示词落盘 + 快照")
        t1 = make_ready(tmp, "r1")
        prev = do_render(t1, "")
        t = load(t1)
        pf = t1 / "提示词.md"
        ok &= check("状态 awaiting_approval", t["state"] == "awaiting_approval")
        ok &= check("提示词文件落盘且非空", pf.is_file() and pf.stat().st_size > 100)
        ok &= check("快照一致", t["approval_snapshot"] == prev["snapshot"])
        ok &= check("默认参数齐", t["gen_params"] == {"duration": 5, "ratio": "9:16",
                                                      "resolution": "720", "audio": True})

        print("[2] 未批准 submit 拒（停点守卫）")
        try:
            do_submit(t1, verbose=False)
            ok &= check("awaiting_approval submit 应拒", False)
        except GenError as e:
            ok &= check("awaiting_approval submit 拒", "先审批" in str(e), str(e)[:60])

        print("[3] approve → approved；改输入后作废")
        do_approve(t1, verbose=False)
        ok &= check("approved", load(t1)["state"] == "approved")
        snap_ok = load(t1)["approval"]["snapshot_sha256"]
        ok &= check("批准绑快照", snap_ok == prev["snapshot"])
        # 参数在批准后变 → submit 时作废
        t = load(t1)
        t["gen_params"]["duration"] = 10
        from .state import save
        save(t1, t)
        try:
            do_submit(t1, verbose=False)
            ok &= check("改参数后 submit 应作废拒", False)
        except GenError as e:
            ok &= check("改参数 submit 拒（审批作废）", "作废" in str(e), str(e)[:60])

        print("[4] 缺渠道配置 submit 拒（不盲提交）")
        t = load(t1)
        t["gen_params"]["duration"] = 5  # 改回原参数，快照恢复有效
        save(t1, t)
        try:
            do_submit(t1, verbose=False)
            ok &= check("缺渠道应拒", False)
        except GenError as e:
            ok &= check("缺渠道 submit 拒", "渠道配置" in str(e), str(e)[:60])
        # AT-13 B2 升级断言：这条当年「拒了但任务留在 generating」是绿灯过的 bug——
        # 前置拒绝必须受控回退（approved/generating → awaiting_approval），绝不许停在 generating。
        st = load(t1)["state"]
        ok &= check("缺渠道拒后任务不得停 generating（原 bug 曾绿灯）", st != "generating", st)
        ok &= check("缺渠道=前置拒绝 → 受控回退 awaiting_approval", st == "awaiting_approval", st)
        ok &= check("回退清掉批准（必须重批）", not load(t1).get("approval"), "")

        print("[5] 假渠道：提交被服务端拒 → 环境错不判任务死")
        # SDGF 端点若已部署：假 key 提交会失败——不真调（探针不触达付费）；
        # 此处只验证「渠道配置读取与校验」逻辑
        config.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        (config.PROJECTS_DIR / "channel.json").write_text(
            '{"baseUrl": "https://x", "apiKey": "k", "model": "m"}', encoding="utf-8")
        from .generate import channel
        ch = channel()
        ok &= check("channel.json 读取", ch["baseUrl"] == "https://x")
        (config.PROJECTS_DIR / "channel.json").unlink()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'ALL PASS' if ok else 'HAS FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(run())
