"""赛马记分器：按 docs/赛马-拆解变体-20261101.md §3 的口径自动打分。

用法：
    py -3 scripts/race_score.py <产物根目录>            # 目录下每个子目录是一份拆解
    py -3 scripts/race_score.py <产物根目录> --json     # 机器可读

口径（跑之前定死在文档里，这里只实现）：
  一致①  硬切口径：实测 0 硬切 → 「一镜到底」必须答「是」；实测 ≥3 → 必须答「否」
  一致②  剪辑行数：实测 0 硬切 → `转场方式=剪辑` 的镜数必须为 0；实测 ≥3 → 必须 >0
  臆测    禁词命中数 + 与机器事实冲突条数（越低越好）
  完整    后 5 列（站位/朝向/视线/镜头运动/景别）非空且非「—/无人物」的比例
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
import sys
from pathlib import Path

FORBIDDEN = ("画外手", "画外的手", "画面外的手", "似乎", "大概", "可能是", "隐约")
LAST5 = ("站位", "朝向", "视线", "镜头运动", "景别")
LAST5_FULL = ("镜号", "起秒-止秒", "运动主体", "运动形态", "速度", "画面内容",
              "光影变化", "转场方式", "站位", "朝向", "视线", "镜头运动", "景别")
CACHE = Path("C:/Users/leon3/AppData/Local/hermes/cache/scratch/race-truth.json")


def truth(video: Path) -> dict:
    """机器真值：硬切、暗场、骤降、时长、画幅、音轨。"""
    key = str(video)
    if CACHE.exists():
        data = json.loads(CACHE.read_text(encoding="utf-8"))
    else:
        data = {}
    if key in data:
        return data[key]

    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", str(video)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    info = json.loads(probe.stdout or "{}")
    duration = float((info.get("format") or {}).get("duration") or 0)
    has_audio = any(s.get("codec_type") == "audio" for s in info.get("streams", []))
    vs = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})

    cut_run = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(video),
         "-filter:v", "select='gt(scene,0.3)',showinfo", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    cuts = [l for l in (cut_run.stderr or "").splitlines()
            if "showinfo" in l and re.search(r"\bn:\s*\d+.*pts_time", l)]

    yavg = CACHE.with_suffix(".yavg.txt")
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(video),
         "-vf", f"signalstats,metadata=print:key=lavfi.signalstats.YAVG:file={yavg}", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    vals, t = [], None
    if yavg.exists():
        for line in yavg.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.search(r"pts_time:([0-9.]+)", line)
            if m:
                t = float(m.group(1))
                continue
            m = re.search(r"YAVG=([0-9.]+)", line)
            if m and t is not None:
                vals.append(float(m.group(1)))
        yavg.unlink(missing_ok=True)
    dark, drops = 0, 0
    if vals:
        med = statistics.median(vals)
        dark = 1 if any(v < med * 0.6 for v in vals) else 0
        drops = sum(1 for i in range(1, len(vals)) if vals[i - 1] - vals[i] > 25)

    data[key] = {"duration": duration, "has_audio": has_audio,
                 "cuts": len(cuts), "dark": dark, "drops": drops}
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return data[key]


def shot_rows(text: str) -> tuple[list[str], list[list[str]]]:
    """取九层里「七 分镜表」那张表 → (表头, 数据行)。三种写法都要认。

    2026-11-01 赛马实测到的第三种：模型省掉表头，第一行直接就是
    `1 | 00:00-01.97 | 海鲜拼盘 | …`。若把第一行当表头，后 5 列全判成缺失，
    会把「渲染器能处理」误记成「模型没写」——所以这里补一个契约列名表头。
    """
    body = ""
    m = re.search(r"##\s*七\s*分镜表(.*?)(?:\n##\s|\Z)", text, re.S)
    if m:
        body = m.group(1)
    rows: list[list[str]] = []
    for raw in body.splitlines():
        s = raw.strip()
        if s.count("|") < 2:
            continue
        if re.fullmatch(r"\|[\s:|-]+\|", s):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if cells and all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
            continue
        rows.append(cells)
    if not rows:
        return [], []
    first = rows[0][0].strip() if rows[0] else ""
    if re.fullmatch(r"\d{1,2}", first) or re.search(r"\d{1,2}:\d{2}", first):
        width = max(len(r) for r in rows)
        header = list(LAST5_FULL[:width]) + [""] * max(0, width - len(LAST5_FULL))
        return header, rows
    return rows[0], rows[1:]


def score(one: Path) -> dict:
    text = one.read_text(encoding="utf-8") if one.is_file() else (one / "拆解.md").read_text(encoding="utf-8")
    header, rows = shot_rows(text)
    cut_row_re = re.compile(r"一镜到底\s*[：:]\s*(\S+)")
    mm = cut_row_re.search(text)
    says_one_take = (mm.group(1) if mm else "").startswith("是")

    col = {name: i for i, name in enumerate(header)}
    clip_rows = 0
    for cells in rows:
        for i, c in enumerate(cells):
            if i < len(header) and header[i] == "转场方式" and c.strip() == "剪辑":
                clip_rows += 1
    if not rows:                              # 老契约没有转场列时，退化成全文数
        clip_rows = len(re.findall(r"\|\s*剪辑\s*\|", text))

    filled = total = 0
    for cells in rows:
        for name in LAST5:
            if name not in col:
                continue
            total += 1
            i = col[name]
            v = cells[i].strip() if i < len(cells) else ""
            if v and v not in ("—", "-", "无人物", "无"):
                filled += 1
    complete = (filled / total) if total else 0.0

    forbidden = sum(text.count(w) for w in FORBIDDEN)
    return {
        "has_last5": all(n in col for n in LAST5),
        "cols": len(header),
        "shots": len(rows),
        "one_take": says_one_take,
        "clip_rows": clip_rows,
        "complete": round(complete, 3),
        "forbidden": forbidden,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--videos", default="E:/OneDrive/工作文件夹/电商视频-廖总/uploads")
    ap.add_argument("--json", action="store_true", help="额外输出机器可读 JSON")
    args = ap.parse_args()

    root = Path(args.root)
    vids = {p.stem: p for p in Path(args.videos).glob("*.mp4")}
    out = []
    for one in sorted(root.iterdir()):
        if not one.is_dir():
            continue
        name = one.name
        stem = re.sub(r"^[AB]-", "", name)
        video = vids.get(stem)
        if not video:
            print(f"[跳过] {name}：找不到同名视频")
            continue
        t = truth(video)
        s = score(one)
        conflicts = []
        if not t["cuts"] and not s["one_take"]:
            conflicts.append("0 硬切却答「一镜到底：否」")
        if t["cuts"] >= 3 and s["one_take"]:
            conflicts.append(f"{t['cuts']} 次硬切却答「一镜到底：是」")
        if not t["cuts"] and s["clip_rows"]:
            conflicts.append(f"0 硬切却有 {s['clip_rows']} 镜标「剪辑」")
        if t["cuts"] >= 3 and not s["clip_rows"]:
            conflicts.append(f"{t['cuts']} 次硬切却 0 镜标「剪辑」")
        row = {"dir": name, "cuts": t["cuts"], "dark": t["dark"], "drops": t["drops"],
               **s, "conflicts": len(conflicts), "conflict_why": conflicts,
               "speculation": s["forbidden"] + len(conflicts)}
        out.append(row)

    print(f"{'目录':<26}{'硬切':>4}{'镜数':>5}{'列':>4}{'后5列':>6}{'完整率':>7}{'禁词':>5}{'冲突':>5}{'臆测分':>7}")
    for r in out:
        print(f"{r['dir']:<26}{r['cuts']:>4}{r['shots']:>5}{r['cols']:>4}"
              f"{'有' if r['has_last5'] else '无':>6}{r['complete']:>7.2f}"
              f"{r['forbidden']:>5}{r['conflicts']:>5}{r['speculation']:>7}")
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
