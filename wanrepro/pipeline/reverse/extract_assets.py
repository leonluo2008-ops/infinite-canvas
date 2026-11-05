#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""extract_assets.py — 从参考视频里抽出「可喂给万相」的复刻素材

为什么需要这一步：
  只给一张产品图，万相知道"产品长什么样"，但不知道"原片长什么样"——
  背景、台面、道具摆放、光线方向、产品在画面里的位置与大小，全靠模型自由发挥。
  实测证据：结构复刻达标，但只给单图时产品会偏位、道具形态漂移。

产出（assets 目录）：
  图片1  产品主图（我方，唯一产品外观依据）
  图片2  场景板 —— 从参考帧清理掉产品与手之后的干净背景（承载背景/台面/道具布局/光线）
  图片3  构图参考 —— 参考帧原样（只用来看产品的位置、大小、朝向，不参考其外观）

清理动作（移除竞品瓶与手）用万相自己的图像编辑能力做，产物风格与视频模型同源：
  wan image2image --image <f> --prompt "移除…" --generation-mode standard
默认只 dry-run 打印命令；加 --execute 才真调（计费，需用户批准）。

用法：
  py -3 pipeline/reverse/extract_assets.py --tech <项目>/analysis/tech.json \
      --spec <项目>/analysis/spec.json -o <项目>/analysis/assets \
      --product "<项目>/product.png" [--execute]
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

CLEAN_PROMPT = (
    "移除画面中的产品本体和所有手部，用周围的背景、台面与道具自然补全被遮挡的区域；"
    "保留原有道具的位置、数量、材质、颜色，保留原有光线方向与亮度分布，"
    "不要新增任何物体或文字，不要改变构图与视角"
)


def pick_frames(tech: dict, spec: dict) -> dict:
    """挑两张帧：①产品在场且手最少的一帧（做构图参考）②产品在场最早一帧（做场景板基底）"""
    product = next((s for s in spec["shots"] if s["product_presence"]["present"]), None)
    if not product:
        raise SystemExit("spec 里没有标记产品出现的镜头，无法抽帧")
    acts = product.get("action_steps", [])
    # 手最早出现的时间点，之前的一帧通常最干净
    first_hand = min((a["t_s"] for a in acts if "手" in (a.get("who") or "")), default=None)

    frames = tech.get("frames", [])
    if not frames:
        raise SystemExit("tech.json 没有 frames，请先跑 dissect.py（不要加 --no-frames）")

    def near(t: float) -> dict:
        return min(frames, key=lambda f: abs(f["t_s"] - t))

    # 场景板基底：帧间差异最小的一秒（手最少动/画面最接近静态）
    fd = tech.get("framediff_per_second") or []
    calm_sec = min(fd, key=lambda x: x["mean_score"])["sec"] if fd else 0
    scene_base = near(float(calm_sec) + 0.5)
    # 构图参考：产品在画面里的中段（最稳定的展示位）
    leave = product["product_presence"].get("leaves_frame_at_s") or product["end_s"]
    compose = near((product["start_s"] + min(leave, product["end_s"])) / 2)
    return {"scene_base": scene_base, "compose": compose, "first_hand_s": first_hand,
            "calm_sec": calm_sec}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tech", required=True)
    ap.add_argument("--spec", required=True)
    ap.add_argument("--product", required=True, help="我方产品主图路径")
    ap.add_argument("-o", "--outdir", required=True)
    ap.add_argument("--execute", action="store_true", help="真调 wan image2image 清理（计费）")
    args = ap.parse_args()

    tech = json.loads(Path(args.tech).read_text(encoding="utf-8"))
    spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    picked = pick_frames(tech, spec)
    print(f"手最早出现：{picked['first_hand_s']}s")

    # 图片1 产品
    p1 = outdir / ("图片1_产品主图" + Path(args.product).suffix)
    shutil.copyfile(args.product, p1)
    # 图片3 构图参考（参考帧原样）
    p3 = outdir / "图片3_构图参考.jpg"
    shutil.copyfile(picked["compose"]["file"], p3)
    # 图片2 场景板基底（待清理）
    base = outdir / "图片2_场景板_待清理.jpg"
    shutil.copyfile(picked["scene_base"]["file"], base)

    cleaned = outdir / "图片2_场景板.jpg"
    cmd = ["wan", "image2image", "--image", str(base), "--prompt", CLEAN_PROMPT,
           "--generation-mode", "standard", "--save", "--save-dir", str(outdir)]
    print("\n场景板清理命令：")
    print("  " + " ".join(f'"{c}"' if " " in c else c for c in cmd))

    status = "pending_clean"
    if args.execute:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        out = (r.stdout or "") + (r.stderr or "")
        print(out[-800:])
        produced = sorted(outdir.glob("wan_image*.png")) + sorted(outdir.glob("Wan_image*.png"))
        if produced:
            produced[-1].replace(cleaned)
            status = "cleaned"
        else:
            status = "clean_failed"

    manifest = {
        "source_tech": str(args.tech),
        "source_spec": str(args.spec),
        "status": status,
        "assets": [
            {"ref": "@图片1", "file": str(p1), "role": "产品主图",
             "use": "唯一产品外观依据：瓶身形状、瓶盖材质与颜色、标签文字与配色"},
            {"ref": "@图片2", "file": str(cleaned if status == "cleaned" else base),
             "role": "场景板", "status": status,
             "use": "只参考背景、台面、道具布局与光线方向，不参考其中任何产品"},
            {"ref": "@图片3", "file": str(p3), "role": "构图参考",
             "use": "只参考产品在画面中的位置、大小与朝向，不参考其外观与标签"},
        ],
        "clean_prompt": CLEAN_PROMPT,
    }
    (outdir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), "utf-8")
    print(f"\n✅ manifest: {outdir / 'manifest.json'}  (状态 {status})")
    if status == "pending_clean":
        print("   场景板还没清理——竞品瓶仍在图里，直接喂给模型会被画回来。加 --execute 再跑。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
