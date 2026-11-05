#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_image.py — 一切图片生成/编辑都必须跟随参考视频比例

规则（2026-10-28 定，起因：参考视频 576×1024 = 9:16，却用默认 1:1 生成，白花钱）：
  1. 比例唯一来源是 tech.json 的实测元数据，不接受手打
  2. 提交前必须比对「请求里的 ratio」与「参考视频 ratio」，不一致直接 abort
  3. 默认 dry-run；--execute 才真调，并打印任务号与前后余额

用法：
  py -3 pipeline/reverse/gen_image.py --tech analysis/reverse/<id>/tech.json \
      --images a.jpg,b.png --prompt "..." --outdir analysis/reverse/<id>/assets --dry-run|--execute
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

WAN = shutil.which("wan.cmd") or shutil.which("wan") or "wan"

# 平台支持的画幅（按需扩充）
SUPPORTED = {"1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3", "21:9"}


def ratio_from_tech(tech: dict) -> str:
    m = tech["meta"]
    w, h = m["width"], m["height"]
    if not w or not h:
        raise SystemExit("tech.json 里没有宽高，无法确定比例")
    val = w / h

    def ratio_value(r: str) -> float:
        a, b = r.split(":")
        return int(a) / int(b)

    best = min(SUPPORTED, key=lambda r: abs(ratio_value(r) - val))
    got = round(ratio_value(best), 4)
    if abs(got - val) > 0.02:
        raise SystemExit(f"参考视频比例 {round(val,3)} 不在支持集合内，需人工确认（{w}x{h}）")
    return best


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tech", required=True, help="参考视频的 tech.json（比例唯一来源）")
    ap.add_argument("--images", required=True, help="逗号分隔；第一张=待编辑底图，其余=参考图")
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--resolution", default="2K")
    ap.add_argument("--mode", default="standard", choices=["standard", "reference", "imaginative"])
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--gate", default=None, help="审计门禁 token（gate.py --kind image 签发）；--execute 必填")
    args = ap.parse_args()

    tech = json.loads(Path(args.tech).read_text(encoding="utf-8"))
    if args.execute:
        if not args.gate:
            raise SystemExit("❌ --execute 必须先过审计门禁（gate.py --kind image），再用 --gate 传入")
        import hashlib
        gp = Path(args.gate)
        if not gp.exists():
            gp = Path(__file__).resolve().parents[2] / ".wanrepro" / "gates" / f"{args.gate}.json"
        if not gp.exists():
            raise SystemExit(f"❌ 门禁 token 不存在：{args.gate}")
        tok = json.loads(gp.read_text(encoding="utf-8"))
        prompt_sha = hashlib.sha256(args.prompt.encode()).hexdigest()[:16]
        if tok.get("prompt_sha256") != prompt_sha:
            raise SystemExit("❌ 提示词与门禁 token 不一致，需重新过门禁")
        want = [a["sha256"] for a in tok.get("assets", [])]
        got = [hashlib.sha256(Path(p.strip()).read_bytes()).hexdigest()[:16] for p in args.images.split(",") if Path(p.strip()).exists()]
        if want != got:
            raise SystemExit("❌ 素材与门禁登记不一致（文件替换或顺序变化）")
        print(f"✅ 门禁通过：{args.gate}")
    ratio = ratio_from_tech(tech)
    print(f"参考视频比例：{ratio}（{tech['meta']['width']}x{tech['meta']['height']}）")

    cmd = [WAN, "image2image", "--images", args.images, "--prompt", args.prompt,
           "--generation-mode", args.mode, "--ratio", ratio,
           "--resolution", args.resolution]
    if args.execute:
        cmd += ["--save", "--save-dir", args.outdir, "--output", "json"]
    else:
        cmd += ["--dry-run"]

    print("命令：", " ".join(f'"{c}"' if " " in c else c for c in cmd))
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = (r.stdout or "") + (r.stderr or "")

    # 比例校验：dry-run / 实际请求里都必须出现 "ratio": "<ref ratio>"
    if f'"ratio": "{ratio}"' not in out:
        print("❌ 提交未带上参考比例，已中止", file=sys.stderr)
        print(out[-800:], file=sys.stderr)
        return 3

    if args.execute:
        import re
        tid = re.search(r'"taskId":\s*"([0-9a-f]+)"', out)
        print("✅ 已提交 taskId:", tid.group(1) if tid else "未解析到")
        print(out[-400:])
    else:
        print("✅ dry-run 通过：ratio =", ratio)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
