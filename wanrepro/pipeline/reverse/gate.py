#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gate.py — 生成前的**审计门禁**（对应 MCP 工具 repro_gate）

用户 2026-10-28 明确：审计是**生成之前**的门禁，不是事后报告。
门禁只回答一件事：**这一条现在能不能花钱发出去？** 不能就列出阻断项，一个都不许漏。

它核 12 项（PASS/FAIL/WARN），全 PASS 才签发 gate token；视频生成走 `pipeline/gen/`
（提交节点 `wan_submit.py`，入口 `wan_video.py`），只认 token，
token 绑死「提示词 sha256 + 素材 sha256 与比例 + 分辨率 + 时长 + 预算」，任一项变了就作废。

G0 提示词头声明案例库版本与案例 id，且与当前 library 一致（强制现读案例库，不许凭记忆）
G1 存在本轮的 guide 读取凭证（.wanrepro/guides/*.json 且案例对得上）
G2 tech.json 与参考视频**重新 ffprobe** 一致（宽高、时长 ±0.1s）
G3 spec.json 存在且关键字段齐全、镜数与 tech 剪辑点自洽
G4 素材实测宽高比 == 参考比例（±0.01）—— 上次 1:1 出图就是这个没拦
G5 素材文件存在且登记 sha256
G6 提示词七段齐全（总体/素材/分镜/台词/音效或BGM/风格/一致性）；负向可选
G7 @图片N 数量与素材张数一致、编号连续
G8 时间轴首尾相接覆盖全片
G9 prompt_lint 无 ERROR
G10 预算：预估灵感值 ≤ --max-credits 且 ≤ 账户余额
G11 在途视频任务数 = 0（防止并发重复扣费）
G12 素材水印已确认清理（--ack-no-watermark 或 project.json watermark_cleared=true）

用法：
  py -3 pipeline/reverse/gate.py --prompt prompts/x.txt --tech analysis/reverse/<id>/tech.json \
      --images a.png,b.png --kind video --resolution 480P --max-credits 30 --ack-no-watermark
退出码：0 签发 token｜1 有阻断项（并打印清单）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import sys
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / ".wanrepro" / "gates"
WAN = shutil.which("wan.cmd") or shutil.which("wan") or "wan"
RATE = {"480P": 2.0, "720P": 3.0, "1080P": 6.0}   # 480P 已实测；720P/1080P 为估算，token 里标注

P, F, W, S = "PASS", "FAIL", "WARN", "SKIP"


def sha(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()[:16]


def img_size(path: Path) -> tuple[int, int]:
    im = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    if im is None:
        raise SystemExit(f"读不了图片：{path}")
    return int(im.shape[1]), int(im.shape[0])


def ffprobe(path: Path) -> dict:
    exe = shutil.which("ffprobe") or "ffprobe"
    r = subprocess.run([exe, "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=width,height", "-show_entries", "format=duration",
                        "-of", "json", str(path)], capture_output=True, text=True)
    d = json.loads(r.stdout or "{}")
    st = (d.get("streams") or [{}])[0]
    return {"width": st.get("width"), "height": st.get("height"),
            "duration_s": round(float(d.get("format", {}).get("duration", 0)), 3)}


def running_tasks() -> int:
    r = subprocess.run([WAN, "task", "list", "--page-size", "10", "--media-type", "video", "--output", "json"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    try:
        d = json.loads(r.stdout or "[]")
        items = d if isinstance(d, list) else (d.get("items") or [])
        return sum(1 for i in items if str(i.get("status")) == "1")
    except Exception:
        return -1


def credits() -> int:
    r = subprocess.run([WAN, "credits", "--output", "json"], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    m = re.search(r'"availableCount":\s*(\d+)', r.stdout or "")
    return int(m.group(1)) if m else -1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--tech", required=True)
    ap.add_argument("--images", required=True)
    ap.add_argument("--kind", default="video", choices=["video", "image"])
    ap.add_argument("--resolution", default="480P")
    ap.add_argument("--duration", type=float, default=None)
    ap.add_argument("--max-credits", type=float, default=0)
    ap.add_argument("--ack-no-watermark", action="store_true")
    ap.add_argument("--allow-concurrent", action="store_true")
    ap.add_argument("--no-live", action="store_true",
                    help="离线分析模式：不查余额与在途任务，G10/G11 记 SKIP 而不是 FAIL")
    args = ap.parse_args()

    prompt_path = Path(args.prompt)
    text = prompt_path.read_text(encoding="utf-8")
    profile_m = re.search(r"^#\s*profile:\s*(\w+)", text, re.M)
    profile = profile_m.group(1).lower() if profile_m else "film"   # film=整片语法 / shot=分镜语法
    tech = json.loads(Path(args.tech).read_text(encoding="utf-8"))
    meta = tech["meta"]
    ratio = "9:16" if meta["width"] < meta["height"] else "16:9"
    structure = tech.get("structure", {})
    # 生成段时长 = 参考片动作段（字卡/尾卡后期叠，不计入生成）
    gen_default = structure.get("action_end_s") or meta["duration_s"]
    duration = float(args.duration or gen_default)
    images = [Path(p.strip()) for p in args.images.split(",") if p.strip()]

    checks: list[tuple[str, str, str]] = []

    def add(cid: str, verdict: str, msg: str) -> None:
        checks.append((cid, verdict, msg))

    # G0 案例库版本与案例 id
    man_p = ROOT / "library" / "manifest.json"
    if not man_p.exists():
        raise SystemExit("旧规则库已于 2026-10-29 删除（所有者裁定），重建后另立版本；本检查/本功能暂不可用")
    else:
        man = json.loads(man_p.read_text(encoding="utf-8"))
        v = re.search(r"^#\s*library:\s*(\S+)", text, re.M)
        c = re.search(r"^#\s*cases:\s*(.+)$", text, re.M)
        if not v or not c:
            add("G0 案例库", F, "提示词头缺 `# library: <版本>` 与 `# cases: <id,...>` —— 写提示词必须先读案例库")
        elif v.group(1) != man["version"]:
            add("G0 案例库", F, f"提示词声明库版本 {v.group(1)}，当前为 {man['version']}（案例库已更新，需重读）")
        else:
            ids = [x.strip() for x in c.group(1).split(",") if x.strip()]
            # 案例来源：通用库 library/cases + 项目案例 projects/<id>/cases（项目案例不入库）
            known = {p.stem for p in (ROOT / "library" / "cases").glob("*.json")}
            known |= {p.stem for p in (ROOT / "projects").glob("*/cases/*.json")}
            missing = [i for i in ids if i not in known]
            add("G0 案例库", F if missing else P,
                f"库 {v.group(1)}，引用案例 {len(ids)} 条" + (f"，不存在：{missing}" if missing else ""))

    # G1 guide 读取凭证：必须取**最新**的一份，且库版本要与提示词头声明的一致
    # （旧实现按文件名排序取最后一个，文件名是随机哈希 → 会读到过期凭证）
    gdir = ROOT / ".wanrepro" / "guides"
    guides = sorted(gdir.glob("*.json"), key=lambda p: p.stat().st_mtime) if gdir.exists() else []
    if not guides:
        add("G1 读取凭证", W, "没有 guide 读取凭证（可能未跑 guide.py）；建议每次编写前先读")
    else:
        latest = json.loads(guides[-1].read_text(encoding="utf-8"))
        claimed = v.group(1) if "v" in dir() and hasattr(v, "group") else None
        same = (claimed is None) or (latest.get("library_version") == claimed)
        add("G1 读取凭证", P if same else W,
            f"{latest['stamp']}｜库 {latest.get('library_version')}｜案例 {len(latest['cases'])} 条"
            + ("" if same else f"｜与提示词声明的 {claimed} 不一致（凭证过期，重跑 guide）"))

    # G2 tech 与视频实测一致
    video = Path(tech.get("source") or "")
    if not video.exists():
        add("G2 参考实测", W, f"tech.json 里的源视频路径不可用（{tech.get('source')}），跳过复核")
    else:
        real = ffprobe(video)
        ok = (real["width"] == meta["width"] and real["height"] == meta["height"]
              and abs(real["duration_s"] - float(meta["duration_s"])) <= 0.1)
        add("G2 参考实测", P if ok else F,
            f"视频 {real['width']}x{real['height']} {real['duration_s']}s vs tech {meta['width']}x{meta['height']} {meta['duration_s']}s")

    # G3 spec 完整
    spec_p = Path(args.tech).with_name("spec.json")
    if not spec_p.exists():
        add("G3 拆解", F, "spec.json 不存在：先做语义拆解")
    else:
        sp = json.loads(spec_p.read_text(encoding="utf-8"))
        legacy_fields = ["global", "story", "audio_summary"]
        v13_fields = ["scene_env", "style", "lighting"]
        missing_legacy = [key for key in legacy_fields if not sp.get(key)]
        missing_v13 = [key for key in v13_fields if not sp.get(key)]
        shots = sp.get("shots") or []
        missing = []
        if not shots:
            missing.append("shots 缺失")
        if missing_legacy and missing_v13:
            missing.append(f"旧形态缺 {missing_legacy}；v1.3 新形态缺 {missing_v13}")
        shape = "旧形态" if not missing_legacy else "v1.3 新形态"
        add("G3 拆解", F if missing else P,
            "；".join(missing) if missing else f"镜数 {len(shots)}，{shape}字段齐")

    # G4 素材比例
    bad = []
    for p in images:
        if not p.exists():
            bad.append(f"{p.name} 不存在")
            continue
        w, h = img_size(p)
        got = w / h
        ref = meta["width"] / meta["height"]
        if abs(got - ref) > 0.01:
            bad.append(f"{p.name} {w}x{h} 比例 {got:.3f} ≠ 参考 {ref:.3f}")
    # 素材比例只提示，不阻断。
    # 用户 2026-10-29 明确：产品图不需要 9:16，图生视频按素材实际比例处理；
    # 资产图也由用户自行生成或处理，门禁不该拿比例去卡它。
    add("G4 素材比例", W if bad else P,
        "；".join(bad) + "（仅提示，不阻断）" if bad else f"{len(images)} 张素材比例均 = {ratio}")

    # G5 素材指纹
    fps = []
    for p in images:
        if p.exists():
            w, h = img_size(p)
            fps.append({"path": str(p), "sha256": sha(p), "w": w, "h": h})
    add("G5 素材指纹", P if len(fps) == len(images) else F, f"登记 {len(fps)}/{len(images)} 张")

    # G6 七段齐全
    segs = {"总体": r"总体|全片|整体", "素材": r"@图片1", "分镜": r"分镜|镜头\s*1|0\.0-",
            "台词": r"台词", "音效": r"音效|BGM|声音", "风格": r"风格", "一致性": r"一致性|保持"}
    miss = [k for k, pat in segs.items() if not re.search(pat, text)]
    if profile == "shot":
        add("G6 结构", P, "分镜语法：结构由 G14 依据体检（B13 摄影机路径 / B14 结束构图 / B18 单镜时长）覆盖")
    else:
        add("G6 结构", F if miss else P, f"缺段：{miss}" if miss else "七段齐全（负向可选）")

    # G7 素材引用
    refs = sorted(set(int(x) for x in re.findall(r"@图片(\d+)", text)))
    ok7 = len(refs) == len(images) and refs == list(range(1, len(images) + 1))
    add("G7 引用对应", P if ok7 else F, f"@图片{refs} vs 素材 {len(images)} 张")

    # G8 时间轴
    # 只看「分镜N（a-b）」的段；动作时间轴里的 0-6.6 之类是段内分解，不是分镜段
    if profile == "shot":
        mm = re.search(r"本镜\s*(\d+(?:\.\d+)?)\s*秒", text)
        if not mm:
            add("G8 时间轴", F, "分镜提示词缺「本镜 N 秒」")
        else:
            ok8 = abs(float(mm.group(1)) - duration) <= 0.6
            add("G8 时间轴", P if ok8 else F,
                f"本镜 {float(mm.group(1)):g}s vs 提交 {duration:.1f}s")
        spans = []
    else:
        spans = [(float(a), float(b)) for a, b in
                 re.findall(r"分镜\s*\d*\s*（\s*(\d+(?:\.\d+)?)\s*[-–~]\s*(\d+(?:\.\d+)?)\s*）", text)]
    if profile == "shot":
        pass
    elif not spans:
        add("G8 时间轴", F, "没有任何「分镜N（起-止）」分段")
    else:
        spans.sort()
        cursor, gaps = 0.0, []
        for a, b in spans:
            if abs(a - cursor) > 0.05:
                gaps.append(f"{cursor:.1f}->{a:.1f}")
            cursor = max(cursor, b)
        ok8 = not gaps and abs(cursor - duration) <= 0.6
        add("G8 时间轴", P if ok8 else F,
            f"断档 {gaps}" if gaps else f"{len(spans)} 段覆盖 0–{cursor:.1f}s（目标 {duration:.1f}s）")

    # G9 lint
    lint = subprocess.run([sys.executable, str(ROOT / "pipeline" / "reverse" / "prompt_lint.py"),
                           "--prompt", str(prompt_path), "--tech", args.tech,
                           "--assets", str(len(images))], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    errs = re.search(r"(\d+) ERROR", lint.stdout or "")
    nerr = int(errs.group(1)) if errs else -1
    add("G9 lint", P if nerr == 0 else F, f"{nerr} 个 ERROR（详见 lint 输出）")

    # G10 预算
    if args.kind == "video":
        est = round(duration * RATE.get(args.resolution, 2.0), 1)
        basis = "480P 实测 2/秒" if args.resolution == "480P" else f"{args.resolution} 估算 {RATE.get(args.resolution)}/秒"
    else:
        est = 0.5 * 4      # image2image 一次出 4 张，实测共 2 点
        basis = "实测 0.5/张 ×4"
    bal = credits()
    ok10 = est <= args.max_credits and (bal < 0 or est <= bal)
    if args.no_live:
        add("G10 预算", P if est <= args.max_credits else F,
            f"预估 {est}（{basis}）｜上限 {args.max_credits}｜余额未查（离线模式）")
    else:
        add("G10 预算", P if ok10 else F,
            f"预估 {est}（{basis}）｜上限 {args.max_credits}｜余额 {bal}")

    # G11 在途
    n_run = running_tasks()
    if args.no_live:
        # 编排器只做分析、不签发提交 token，就不去查在途任务；
        # 这里必须是 SKIP 而不是 FAIL——把"没查"显示成"不合格"是假信息。
        add("G11 在途互斥", S, "未检查：离线分析模式不签发提交 token")
    else:
        ok11 = n_run == 0 or args.allow_concurrent
        add("G11 在途互斥", P if ok11 else F, f"在途视频任务 {n_run} 个")

    # G12 水印
    proj = ROOT / ".wanrepro" / "project.json"
    cleared = args.ack_no_watermark
    if proj.exists():
        cleared = cleared or bool(json.loads(proj.read_text(encoding="utf-8")).get("watermark_cleared"))
    add("G12 水印", P if cleared else W,
        "已确认素材无水印" if cleared else "未确认素材是否已去水印（参考片来自 TikTok 时必查）")

    # G13 编写依据版本（提示词头 # basis: pb-xxxx 必须等于当前依据）
    basis_p = ROOT / "spec" / "prompt_basis.json"
    if not basis_p.exists():
        raise SystemExit("旧规则库已于 2026-10-29 删除（所有者裁定），重建后另立版本；本检查/本功能暂不可用")
    else:
        cur_basis = json.loads(basis_p.read_text(encoding="utf-8"))
        mb = re.search(r"^#\s*basis:\s*(\S+)", text, re.M)
        if not mb:
            add("G13 编写依据", F, "提示词头缺 `# basis: <版本>` —— 写提示词必须先读《编写依据》")
        elif mb.group(1) != cur_basis["version"]:
            add("G13 编写依据", F, f"声明 {mb.group(1)} ≠ 当前 {cur_basis['version']}（依据已更新，重读）")
        else:
            add("G13 编写依据", P,
                f"{cur_basis['version']}｜规则 {cur_basis['counts']['rules']} 条｜反例 {cur_basis['counts']['antipatterns']} 条")

    # G14 依据体检必须 0 ERROR（这是审计负面清单的落地）
    chk = ROOT / "pipeline" / "basis" / "check_basis.py"
    if not chk.exists():
        raise SystemExit("旧规则库已于 2026-10-29 删除（所有者裁定），重建后另立版本；本检查/本功能暂不可用")
    else:
        r14 = subprocess.run([sys.executable, str(chk), "--prompt", str(prompt_path), "--tech", str(args.tech)]
                             + (["--assets", str(len(images))] if args.kind == "video" else []),
                             cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace")
        m14 = re.search(r"汇总：\s*(\d+)\s*ERROR\s*/\s*(\d+)\s*WARN", r14.stdout)
        e14, w14 = (int(m14.group(1)), int(m14.group(2))) if m14 else (99, 0)
        fails14 = [ln.strip() for ln in r14.stdout.splitlines() if "❌" in ln]
        add("G14 依据体检", P if e14 == 0 else F,
            f"{e14} ERROR / {w14} WARN" + ("" if e14 == 0 else "｜" + "；".join(fails14[:3])))

    # ---------- 汇总 ----------
    blockers = [c for c in checks if c[1] == F]
    n_skip = sum(1 for c in checks if c[1] == S)
    skip_note = f" / {n_skip} SKIP（离线未查）" if n_skip else ""
    print(f"审计门禁｜{len(checks)} 项：{len(blockers)} FAIL / {sum(1 for c in checks if c[1] == W)} WARN{skip_note}")
    for cid, v, msg in checks:
        print(f"  [{v}] {cid} — {msg}")

    # 离线模式只出检查结果，不签发 token：token 代表"可以提交了"，而离线模式根本没查过账户状态。
    if args.no_live:
        print("\nℹ️ 离线分析模式：不签发提交 token。要提交前请带完整参数再跑一次门禁。")
        return 0 if not blockers else 1

    if blockers:
        print("\n❌ 拒绝放行。修完以下项再提交：")
        for cid, _, msg in blockers:
            print(f"   - {cid}: {msg}")
        return 1

    STATE.mkdir(parents=True, exist_ok=True)
    token = {
        "gate_id": "gt-" + hashlib.sha256(
            (sha(prompt_path) + "".join(f["sha256"] for f in fps) + ratio + str(duration) + args.resolution).encode()
        ).hexdigest()[:10],
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "kind": args.kind,
        "prompt_path": str(prompt_path),
        "prompt_sha256": sha(prompt_path),
        "assets": fps,
        "ratio": ratio,
        # 视频提交时 CLI 取整秒（round），token 必须存同一个口径，否则校验永远差 0.347
        "duration_s": (int(round(duration)) if args.kind == "video" else duration),
        "ref_duration_s": duration,
        "resolution": args.resolution,
        "est_credits": est,
        "max_credits": args.max_credits,
        "checks": [{"id": c, "verdict": v, "msg": m} for c, v, m in checks],
        "library_version": man["version"] if man_p.exists() else None,
    }
    out = STATE / f"{token['gate_id']}.json"
    out.write_text(json.dumps(token, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n✅ 放行：{token['gate_id']}（.wanrepro/gates/{token['gate_id']}.json）")
    print(f"   绑定：prompt {token['prompt_sha256']}｜{len(fps)} 张素材｜{ratio}｜{duration}s｜{args.resolution}｜预估 {est} 点")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
