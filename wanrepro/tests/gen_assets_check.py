#!/usr/bin/env python3
"""生成链路的资产映射测试：图号 ≠ 文件名。

踩过的坑：盘上的 `图片N.png` 里 N 是**清单行号**，提示词里的 `@图片N` 是**第 N 个有图的资产**。
按文件名直接对应会整体错位一整张——片子生成出来看不出错位，只会把人脸换成别人的。
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PASS = FAIL = 0


def check(ok: bool, name: str, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"[PASS] {name}")
    else:
        FAIL += 1
        print(f"[FAIL] {name}｜{detail}")


def load_module():
    spec = importlib.util.spec_from_file_location("wan_video", ROOT / "pipeline" / "gen" / "wan_video.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_task(base: Path, task_id: str, files: list[str]) -> Path:
    d = base / task_id / "assets"
    d.mkdir(parents=True, exist_ok=True)
    for name in files:
        (d / name).write_bytes(b"\x89PNG\r\n\x1a\n")
    return base


def main() -> int:
    mod = load_module()
    tmp = Path(tempfile.mkdtemp(prefix="gen-assets-"))
    mod.ANALYSIS = tmp

    # ① 行号 2..6（图片1 那行没图）→ @图片1 必须映射到 图片2.png
    make_task(tmp, "gap", ["图片2.png", "图片3.png", "图片4.png", "图片5.png", "图片6.png"])
    got = [p.name for p in mod.asset_images("gap", "开头 @图片1 中间 @图片3 结尾 @图片5")]
    check(got == ["图片2.png", "图片4.png", "图片6.png"], "① 按顺序映射，不是按文件名", f"得到 {got}")

    # ② 中间空号要跳过，不能把 图片6 当成 图片5
    make_task(tmp, "sparse", ["图片2.png", "图片6.png"])
    got = [p.name for p in mod.asset_images("sparse", "@图片1 和 @图片2")]
    check(got == ["图片2.png", "图片6.png"], "② 空号跳过", f"得到 {got}")

    # ③ 引用超出张数 → 报错，且说清缺哪个
    err = ""
    try:
        mod.asset_images("sparse", "@图片1 @图片2 @图片3")
    except ValueError as e:
        err = str(e)
    check("图片3" in err and "只有 2 张" in err, "③ 缺图整体报错并点名", err or "没有报错")

    # ④ 没有 @图片 的纯文生视频 → 不传图
    check(mod.asset_images("sparse", "纯文生视频，没有任何图号") == [], "④ 无图号返回空")

    print(f"  SUMMARY {PASS}/{PASS + FAIL} PASS / {FAIL} FAIL")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
