#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""selfcheck.py — 让用户自己抽查昨晚成果，不依赖任何口头说明。

跑法（项目根目录）：
    py -3 scripts/selfcheck.py

它会自己重新跑一遍分析链路，把每一项功能的实测结果打出来。
只有"实际跑出来是这样"才打 PASS；没跑、缺数据、跑不通，一律打 SKIP/FAIL 并说明原因。

不调用 wan，不花一分钱，不发任何视频任务。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = [sys.executable]
ENV = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
TMP = Path(tempfile.mkdtemp(prefix="selfcheck_"))
ENV["PYTHONPYCACHEPREFIX"] = str(TMP / "pyc")

results: list[tuple[str, str, str]] = []


def add(name: str, ok: bool, detail: str, skip: bool = False) -> None:
    results.append((name, "SKIP" if skip else ("PASS" if ok else "FAIL"), detail))


def run(args: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=str(ROOT), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=ENV, timeout=timeout)


AI1 = "原始资料/POP TK视频/参考视频/ai视频参考1.mp4"
P484 = "原始资料/POP TK视频/参考视频/微信视频2026-10-15_125005_484.mp4"
AI1_TRUTH = [6.433, 8.333, 12.5, 16.7]
P484_TRUTH = [9.343]


def close(a: float, b: float, tol: float = 0.15) -> bool:
    return abs(a - b) <= tol


def check_cuts() -> None:
    """C1/C2 切点检测：对两条参考片重新测，跟人眼标注比对。"""
    for tag, video, truth in (("ai1", AI1, AI1_TRUTH), ("484", P484, P484_TRUTH)):
        out = TMP / f"dissect_{tag}"
        r = run(PY + ["pipeline/reverse/dissect.py", video, "-o", str(out)])
        if r.returncode != 0:
            add(f"C1 切点检测/{tag}", False, f"dissect 退出码 {r.returncode}: {(r.stderr or r.stdout)[-200:]}")
            continue
        tech = json.loads((out / "tech.json").read_text(encoding="utf-8"))
        got = tech["cuts"]["stable_cuts"]
        ok = len(got) == len(truth) and all(any(close(g, t) for t in truth) for g in got) \
             and all(any(close(t, g) for g in got) for t in truth)
        add(f"C1 切点检测/{tag}", ok, f"实测 {got}｜人眼真值 {truth}")

    # 旧误报必须不再出现（字幕换词、产品小窗进出）
    out = TMP / "dissect_ai1"
    if (out / "tech.json").exists():
        got = json.loads((out / "tech.json").read_text(encoding="utf-8"))["cuts"]["stable_cuts"]
        bad = [t for t in (7.733, 10.633, 11.2, 11.7) if any(close(t, g, 0.2) for g in got)]
        add("C2 字幕/小窗不误报", not bad, f"旧误报点残留 {bad}" if bad else "7.733/10.633/11.2/11.7 均未出现")


def check_pipeline() -> None:
    """C3 编排器：一条命令跑通全链，产物齐全。"""
    raw = ROOT / "analysis/reverse/ai1/raw_vision_v1_3.json"
    if not raw.exists():
        add("C3 编排器全链", False, "缺 analysis/reverse/ai1/raw_vision_v1_3.json，跳过（需先跑一次真实 VLM）", skip=True)
        return
    work = TMP / "chain"
    work.mkdir(parents=True)
    shutil.copy(raw, work / "raw_vision.json")
    r = run(PY + ["pipeline/reverse/analyze.py", "--video", AI1, "--outdir", str(work),
                  "--product", "projects/pop-484-test/product.json", "--skip-vlm"])
    files = ["tech.json", "raw_vision.json", "spec.json", "prompt.md", "run.json"]
    missing = [f for f in files if not (work / f).exists()]
    add("C3 编排器全链", r.returncode == 0 and not missing,
        f"退出码 {r.returncode}｜产物 {len(files) - len(missing)}/{len(files)}" +
        (f"｜缺 {missing}" if missing else ""))

    # C4 spec 自洽：实测切点 == 分段边界；分段首尾相接
    if (work / "spec.json").exists() and (work / "tech.json").exists():
        spec = json.loads((work / "spec.json").read_text(encoding="utf-8"))
        tech = json.loads((work / "tech.json").read_text(encoding="utf-8"))
        measured = (spec.get("meta") or {}).get("measured") or {}
        mc = measured.get("cut_points_s") or []
        tc = tech["cuts"]["stable_cuts"]
        same = len(mc) == len(tc) and all(close(a, b, 0.001) for a, b in zip(mc, tc))
        shots = [s for s in spec.get("shots") or [] if not s.get("empty")]
        contiguous = all(close(shots[i]["end_s"], shots[i + 1]["start_s"], 0.001)
                         for i in range(len(shots) - 1))
        add("C4 spec 与实测一致", same and contiguous and len(shots) > 0,
            f"spec 记的切点 {mc}｜实测 {tc}｜分段首尾相接 {contiguous}｜非空段 {len(shots)}")

    # C5 提示词可交付：不允许出现空值/内部枚举/未提供素材的引用
    if (work / "prompt.md").exists():
        text = (work / "prompt.md").read_text(encoding="utf-8")
        bad_tokens = [t for t in ("None", "null", "nan", "TOPDOWN", "overhead", "soft", "warm") if t in text]
        refs = sorted(set(int(n) for n in re.findall(r"@图片(\d+)", text)))
        imgs = 1  # product.json 里只挂 1 张产品图
        ok = not bad_tokens and refs and max(refs) <= imgs and "# duration-override" in text
        add("C5 提示词可交付", ok,
            f"空值/内部词 {bad_tokens or '无'}｜引用图片 {refs}（素材 {imgs} 张）｜有时长覆盖声明 {'# duration-override' in text}")

    # C6 门禁：离线模式下不允许出现 FAIL
    if (work / "prompt.md").exists():
        r = run(PY + ["pipeline/reverse/gate.py", "--prompt", str(work / "prompt.md"),
                      "--tech", str(work / "tech.json"),
                      "--images", "原始资料/POP_white_11_78pct_1800x1800(1)/POP_white_11_78pct_1800x1800/去盒后的白底单瓶香水-8.png",
                      "--duration", "16.7", "--max-credits", "43", "--no-live"])
        line = next((l for l in (r.stdout or "").splitlines() if "审计门禁" in l), "无输出")
        add("C6 审计门禁", "0 FAIL" in line, line.strip())


def check_generic() -> None:
    """C7 通用性：工具层不许出现测试产品名/产品特征。"""
    hits = []
    for p in (ROOT / "pipeline").rglob("*.py"):
        if p.name in {"selfcheck.py"}:
            continue
        txt = p.read_text(encoding="utf-8", errors="replace")
        for kw in ("POP!", "P.O.P", "香水", "484"):
            if kw in txt:
                hits.append(f"{p.relative_to(ROOT)}:{kw}")
    add("C7 工具层无产品硬编码", not hits, "、".join(hits[:5]) if hits else "pipeline/ 下 0 处产品名")

    # C8（依据版本存在性）已摘除：旧规则库已于 2026-10-29 删除（所有者裁定），重建后另立版本；
    # 本检查暂不可用——不报 PASS，也不伪装成 SKIP。
    print("⚠ C8 已摘除：旧规则库已于 2026-10-29 删除（所有者裁定），重建后另立版本；本检查暂不可用")


def main() -> int:
    check_cuts()
    check_pipeline()
    check_generic()

    width = max(len(n) for n, _, _ in results)
    print("\n" + "=" * 78)
    print("自查结果（只认实际跑出来的输出）")
    print("=" * 78)
    for name, verdict, detail in results:
        print(f"{verdict:4s}  {name.ljust(width)}  {detail}")
    n_fail = sum(1 for _, v, _ in results if v == "FAIL")
    n_skip = sum(1 for _, v, _ in results if v == "SKIP")
    print("-" * 78)
    print(f"合计 {len(results)} 项：{len(results) - n_fail - n_skip} PASS / {n_fail} FAIL / {n_skip} SKIP")
    print(f"临时目录（可删）：{TMP}")
    return 1 if n_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
