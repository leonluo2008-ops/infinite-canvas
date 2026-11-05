#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""make_compare_page.py — 把「原视频 + 分析结果」做成一页可对照的 HTML。

用途：验收分析结果时，左边放视频，右边放每一段的拆解，点时间码跳转。
不生成任何新内容，只把 spec.json 里已有的字段排版出来。

用法：
    py -3 scripts/make_compare_page.py --spec analysis/reverse/ai1/spec.json \\
        --video "原始资料/POP TK视频/参考视频/ai视频参考1.mp4" \\
        --outdir 交付/分析验收-20261029 --name ai1
"""
from __future__ import annotations

import argparse
import html
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CSS = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { margin:0; background:#111214; color:#e8e8ea;
       font:14px/1.65 "Segoe UI","Microsoft YaHei",system-ui,sans-serif; }
.wrap { display:flex; gap:24px; max-width:1400px; margin:0 auto; padding:24px; align-items:flex-start; }
.left { position:sticky; top:24px; width:min(420px,42vw); flex:0 0 auto; }
video { width:100%; border-radius:10px; background:#000; display:block; }
h1 { font-size:18px; margin:0 0 4px; }
.meta { color:#9aa0a6; font-size:12.5px; margin-bottom:14px; }
.bar { position:relative; height:26px; margin:10px 0 4px; background:#1c1e21; border-radius:6px; }
.bar .seg { position:absolute; top:0; bottom:0; border-right:1px solid #111214; }
.bar .seg span { position:absolute; left:5px; top:4px; font-size:11px; color:#cfd3d8; }
.bar .cut { position:absolute; top:-3px; width:2px; height:32px; background:#ff6b3d; cursor:pointer; }
.bar .cut::after { content:attr(data-t); position:absolute; top:-17px; left:-14px;
                   font-size:10px; color:#ff9b73; white-space:nowrap; }
.right { flex:1 1 auto; min-width:0; }
.card { border:1px solid #26292d; border-radius:10px; padding:14px 16px; margin-bottom:14px; background:#16181b; }
.card.empty { opacity:.55; }
.card h2 { font-size:14px; margin:0 0 10px; color:#fff; }
.card h2 .t { color:#ff9b73; font-variant-numeric:tabular-nums; }
.kv { display:grid; grid-template-columns:88px 1fr; gap:6px 12px; margin:0; }
.kv dt { color:#8b9198; font-size:12.5px; }
.kv dd { margin:0; }
ol.steps { margin:0; padding-left:20px; }
ol.steps li { margin:1px 0; }
.tag { display:inline-block; padding:1px 7px; border-radius:20px; background:#24272b;
       color:#b9bec4; font-size:11.5px; margin-right:6px; }
.warn { color:#ffb020; }
.gbox { border:1px dashed #333; border-radius:10px; padding:12px 14px; margin-bottom:14px; }
.gbox h3 { font-size:13px; margin:0 0 8px; color:#cfd3d8; }
table { border-collapse:collapse; width:100%; }
td, th { text-align:left; padding:3px 8px 3px 0; vertical-align:top; font-size:13px; }
th { color:#8b9198; font-weight:500; font-size:12px; }
.note { color:#7d848b; font-size:12px; margin-top:10px; }
"""

JS = """
function seek(t){ const v=document.getElementById('v'); v.currentTime=Math.max(0,t); v.play(); }
"""


def fmt(v) -> str:
    if v is None or v == "":
        return '<span class="warn">未提供</span>'
    if isinstance(v, (list, dict)):
        return html.escape(json.dumps(v, ensure_ascii=False))
    return html.escape(str(v))


def render_card(shot: dict, idx: int) -> str:
    empty = shot.get("empty")
    start, end = shot.get("start_s"), shot.get("end_s")
    head = f'<h2>分镜 {idx} <span class="t">{start:.2f}–{end:.2f}s</span></h2>'
    if empty:
        return (f'<div class="card empty">{head}<div class="note">该段没有任何描述：'
                f'{html.escape(str(shot.get("why", "VLM 未描述此段")))}（不编造内容）</div></div>')

    rows = []
    for label, key in (("景别", "shot_size"), ("机位", "camera_view"), ("运镜", "camera_move"),
                       ("主体", "subject_primary")):
        rows.append(f"<dt>{label}</dt><dd>{fmt(shot.get(key))}</dd>")

    steps = shot.get("action_steps") or []
    if steps:
        items = "".join(
            f'<li><a href="#" onclick="seek({s.get("t_s", 0)});return false" style="color:#7cc4ff">'
            f'{s.get("t_s", "?")}s</a> {html.escape(str(s.get("who", "")))} '
            f'{html.escape(str(s.get("physical_step", "")))}'
            f'{"（" + html.escape(str(s.get("direction"))) + "）" if s.get("direction") else ""}</li>'
            for s in steps)
        rows.append(f'<dt>动作</dt><dd><ol class="steps">{items}</ol></dd>')

    props = shot.get("props") or []
    if props:
        rows.append(f'<dt>道具</dt><dd>{fmt(props)}</dd>')

    contacts = shot.get("contact") or []
    if contacts:
        items = "".join(
            f'<li>{html.escape(str(c.get("from", "?")))} → {html.escape(str(c.get("to", "?")))}'
            f'　<span class="tag">{html.escape(str(c.get("type", "")))}</span>'
            f'{"方向 " + html.escape(str(c.get("direction"))) if c.get("direction") else ""}'
            f'{"｜" + html.escape(str(c.get("driven"))) if c.get("driven") else ""}</li>'
            for c in contacts)
        rows.append(f'<dt>接触与受力</dt><dd><ul style="margin:0;padding-left:18px">{items}</ul></dd>')

    rows.append(f'<dt>结束构图</dt><dd>{fmt(shot.get("end_composition"))}</dd>')
    pp = shot.get("product_presence")
    if pp:
        rows.append(f'<dt>产品</dt><dd>{fmt(pp)}</dd>')
    return f'<div class="card">{head}<dl class="kv">{"".join(rows)}</dl></div>'


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", required=True)
    ap.add_argument("--video", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--name", required=True, help="输出文件名前缀，如 ai1")
    args = ap.parse_args()

    spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    video = Path(args.video)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    video_copy = outdir / f"{args.name}.mp4"
    if not video_copy.exists():
        shutil.copy(video, video_copy)

    measured = (spec.get("meta") or {}).get("measured") or {}
    duration = float(measured.get("duration_s") or 0)
    shots = spec.get("shots") or []
    endcard = spec.get("endcard") or []

    # 时间轴
    segs = ""
    colors = ["#2b3a4a", "#3a2b4a", "#2b4a3a", "#4a3a2b", "#3a3a4a"]
    for i, s in enumerate(shots):
        left = (s.get("start_s") or 0) / duration * 100
        width = ((s.get("end_s") or 0) - (s.get("start_s") or 0)) / duration * 100
        segs += (f'<div class="seg" style="left:{left:.3f}%;width:{width:.3f}%;'
                 f'background:{colors[i % len(colors)]}"><span>{i + 1}</span></div>')
    cuts = "".join(
        f'<div class="cut" style="left:{c / duration * 100:.3f}%" data-t="{c:.2f}s" '
        f'onclick="seek({c})"></div>'
        for c in (measured.get("cut_points_s") or []))

    cards = "".join(render_card(s, i + 1) for i, s in enumerate(shots))
    if endcard:
        cards += (f'<div class="card empty"><h2>尾卡 <span class="t">'
                  f'{endcard[0].get("start_s", 0):.2f}–{endcard[-1].get("end_s", 0):.2f}s</span></h2>'
                  f'<div class="note">平台尾卡，不参与复刻（{len(endcard)} 段）</div></div>')

    glob = spec
    gbox = f"""
    <div class="gbox">
      <h3>全片</h3>
      <table>
        <tr><th style="width:88px">场景</th><td>{fmt(glob.get('scene_env'))}</td></tr>
        <tr><th>光线</th><td>{fmt(glob.get('lighting'))}</td></tr>
        <tr><th>风格</th><td>{fmt(glob.get('style'))}</td></tr>
        <tr><th>字幕</th><td>{fmt(glob.get('onscreen_text'))}</td></tr>
        <tr><th>音频</th><td>{fmt(glob.get('audio'))}</td></tr>
      </table>
    </div>"""

    page = f"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<title>分析对照 · {html.escape(args.name)}</title><style>{CSS}</style></head>
<body><div class="wrap">
  <div class="left">
    <h1>{html.escape(video.name)}</h1>
    <div class="meta">{measured.get('width')}×{measured.get('height')} ·
      {duration:.3f}s · {measured.get('fps')}fps ·
      切点 {', '.join(f'{c:.2f}' for c in (measured.get('cut_points_s') or [])) or '无'}</div>
    <video id="v" src="{html.escape(video_copy.name)}" controls preload="metadata"></video>
    <div class="bar">{segs}{cuts}</div>
    <div class="note">点橙色竖线或时间码可跳到该处；色块是分段编号。</div>
    <div class="note">左列数字来自 ffprobe/像素测量，右列描述来自视觉模型，两者分开标注；
      冲突会写进 spec 的 conflicts 字段。</div>
  </div>
  <div class="right">{gbox}{cards}</div>
</div><script>{JS}</script></body></html>"""
    out = outdir / f"{args.name}.html"
    out.write_text(page, encoding="utf-8")
    print(f"写出 {out}（{out.stat().st_size} 字节），视频副本 {video_copy.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
