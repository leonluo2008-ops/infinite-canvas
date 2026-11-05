#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Render reverse-analysis specs and nested product data into one Wan prompt."""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

CUT_MAP = {
    "static": "固定机位，摄影机静止",
    "push_in": "缓慢推近",
    "pull_back": "缓慢拉远",
    "pan_left": "向左横摇",
    "pan_right": "向右横摇",
    "tilt_up": "向上摇",
    "tilt_down": "向下摇",
    "truck_left": "向左平移",
    "truck_right": "向右平移",
    "pedestal_up": "升高机位",
    "pedestal_down": "降低机位",
    "orbit": "环绕移动",
    "handheld_drift": "手持轻微晃动",
    "zoom_in": "变焦推近",
    "zoom_out": "变焦拉远",
    "rack_focus": "焦点转移",
    "whip_pan": "甩镜",
}
SIZE_MAP = {
    "ECU": "大特写", "CU": "特写", "MCU": "近景", "MS": "中景",
    "MWS": "中全景", "WS": "全景", "EWS": "大远景", "TOPDOWN_FLATLAY": "垂直俯拍平铺",
}
ANGLE_MAP = {
    "eye_level": "平视", "high_angle": "俯拍", "low_angle": "仰拍", "overhead": "垂直向下",
    "dutch": "斜角", "over_shoulder": "过肩",
}
LIGHT_MAP = {
    "top": "顶光", "top_left": "左上主光", "top_right": "右上主光", "left": "左侧光",
    "right": "右侧光", "front": "正面光", "back": "逆光", "bottom": "底透光", "ambient": "环境光",
}
# VLM 常把 quality / color_temp 写成英文（实测输出过 soft / warm），
# 直接透传会在中文提示词里混进英文，中文模型对这两个词的理解也不稳。
QUALITY_MAP = {
    "soft": "柔光", "hard": "硬光", "diffuse": "漫射光", "diffused": "漫射光",
    "soft_light": "柔光", "hard_light": "硬光",
}
TEMP_MAP = {
    "warm": "暖", "cool": "冷", "neutral": "中性", "daylight": "中性", "cold": "冷",
}
# Official L100 asks for explicit no-dialogue when audio actually has no voice.
LANGUAGE_MAP = {
    "id": "印尼语", "es": "西班牙语", "zh": "中文", "en": "英语",
    "fr": "法语", "de": "德语", "ja": "日语", "ko": "韩语",
    "pt": "葡萄牙语", "ar": "阿拉伯语", "ru": "俄语", "th": "泰语",
}


def _timecode(seconds: float) -> str:
    minutes, remaining = divmod(seconds, 60)
    return f"{int(minutes):02d}:{remaining:05.2f}"



def _cn_quality(value: str) -> str:
    return QUALITY_MAP.get(value.strip().lower(), value)


def _cn_temp(value: str) -> str:
    return TEMP_MAP.get(value.strip().lower(), value)


_MISSING = {"none", "null", "nan"}
_CAMEL = re.compile(r"(?<![A-Za-z])[A-Z][a-z]+(?:[A-Z][a-z]+)+(?![A-Za-z])")
_BRAND_CONTEXT = re.compile(r"(?:\bAT\s+|@\s*)([A-Za-z][A-Za-z0-9]{3,})", re.I)


def _text(value: object) -> str | None:
    if value is None or isinstance(value, bool) or isinstance(value, (dict, list)):
        return None
    if isinstance(value, (int, float)) and not math.isfinite(value):
        return None
    result = str(value).strip()
    return result if result and result.lower() not in _MISSING else None


def _number(value: object) -> float | None:
    try:
        n = float(value) if not isinstance(value, bool) else float("nan")
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def _obj(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _warn(section: str, reason: str) -> None:
    print(f"WARN missing {section}: {reason}", file=sys.stderr)


def _reference_brands(spec: dict) -> set[str]:
    """Infer only unambiguous brand-shaped words from reference screen text."""
    entries = spec.get("onscreen_text")
    if not isinstance(entries, list):
        entries = [text for shot in (spec.get("shots") or []) if isinstance(shot, dict)
                   for text in (shot.get("onscreen_text") or []) if isinstance(text, dict)]
    tokens: set[str] = set()
    for entry in entries:
        if isinstance(entry, dict):
            for key in ("text", "style"):
                raw = _text(entry.get(key)) or ""
                tokens.update(_CAMEL.findall(raw))
                tokens.update(_BRAND_CONTEXT.findall(raw))
    return tokens


def _scrub(text: str, brands: set[str]) -> str:
    """Remove reference brands without altering customer-authored product fields."""
    for token in sorted(brands, key=len, reverse=True):
        text = re.sub(r"(?<![A-Za-z])" + re.escape(token) + r"(?![A-Za-z])",
                      "", text, flags=re.I)
    # A brand/channel-text clause in the reference end frame is not customer copy.
    clauses = re.split(r"[，；]", text)
    clauses = [clause.strip() for clause in clauses if clause.strip()
               and not any(label in clause for label in ("品牌标识", "渠道文字"))]
    return "，".join(clauses).strip("，； ")


def _pos_from_bbox(b: dict) -> str | None:
    """Keep coordinates: using '居中' lets the model shift the product."""
    coords = [_number(b.get(key)) for key in ("x", "y", "w", "h")]
    if any(value is None for value in coords):
        return None
    x, y, w, h = coords
    return (f"画面中心固定在水平 x≈{x + w / 2:.2f}、垂直 y≈{y + h / 2:.2f}"
            "（0~1 相对坐标，原点左上）")


def _joined(items: object) -> str | None:
    if not isinstance(items, list):
        return None
    values = list(dict.fromkeys(value for item in items if (value := _text(item))))
    return "；".join(values) if values else None


def _endcard_copy(brand: dict) -> str | None:
    value = brand.get("endcard")
    if isinstance(value, dict):
        return _text(value.get("text")) or _text(value.get("copy"))
    return _text(value)


def _asset_refs(assets: list | None) -> set[int]:
    if not isinstance(assets, list):
        return {1}
    refs = set()
    for item in assets:
        if isinstance(item, dict):
            match = re.fullmatch(r"@图片(\d+)", str(item.get("ref", "")))
            if match:
                refs.add(int(match.group(1)))
    return refs or {1}


def _sanitize_image_refs(text: str, asset_refs: set[int]) -> str:
    text = re.sub(
        r"与\s*@图片(\d+)\s*一致",
        lambda match: (match.group(0) if int(match.group(1)) in asset_refs
                       else "与产品主图的呈现一致"),
        text,
    )
    return re.sub(
        r"@图片(\d+)",
        lambda match: match.group(0) if int(match.group(1)) in asset_refs else "产品主图",
        text,
    )


def _header(spec: dict, shots: list[dict], *, drop_platform_card: bool) -> list[str]:
    root = Path(__file__).resolve().parents[2]
    basis_p = root / "spec" / "prompt_basis.json"
    man_p = root / "library" / "manifest.json"
    if not basis_p.exists() or not man_p.exists():
        raise SystemExit("旧规则库已于 2026-10-29 删除（所有者裁定），重建后另立版本；本检查/本功能暂不可用")
    basis = json.loads(basis_p.read_text(encoding="utf-8"))
    manifest = json.loads(man_p.read_text(encoding="utf-8"))
    case_ids = []
    for filename in manifest.get("files", {}).get("cases", []):
        case_path = root / "library" / "cases" / filename
        case = json.loads(case_path.read_text(encoding="utf-8"))
        if case.get("evidence_level") == "C" and _text(case.get("id")):
            case_ids.append(_text(case.get("id")))

    lines = [
        f"# basis: {basis['version']}",
        f"# library: {manifest['version']}",
        f"# cases: {','.join(case_ids) if case_ids else 'none'}",
    ]
    measured = _obj(_obj(spec.get("meta")).get("measured"))
    duration = _number(measured.get("duration_s"))
    cut_points = measured.get("cut_points_s")
    last_cut = (_number(cut_points[-1]) if isinstance(cut_points, list) and cut_points
                else None)
    intervals = []
    for shot in shots:
        start, end = _number(shot.get("start_s")), _number(shot.get("end_s"))
        if start is None or end is None or end <= start:
            continue
        if drop_platform_card and spec.get("endcard") and last_cut is not None and start < last_cut < end:
            end = last_cut
        intervals.append((start, end))
    if duration is not None and intervals:
        first_start = min(start for start, _ in intervals)
        coverage_end = max(end for _, end in intervals)
        covered = coverage_end - first_start
        gap = duration - covered
        if gap > 0.5:
            reason = "平台尾卡" if spec.get("endcard") else "未纳入复刻的片尾段"
            lines.append(
                # 这里必须写实际复刻时长，不是四舍五入到整秒：写 9 会被执行方当成 9.0 去生成，
                # 实际 9.34，差 0.34s 就是片尾多一截空镜。
                f"# duration-override: {covered:.2f} 参考片 {duration:.3f}s 的后 {gap:.1f}s 为{reason}，"
                f"不参与复刻，只生成动作段 {covered:.2f}s"
            )
    return lines


def render(spec: dict, product: dict, *, drop_platform_card: bool = True,
           assets: list | None = None) -> str:
    """Emit eight ordered sections; missing inputs omit their section with WARN."""
    asset_refs = _asset_refs(assets)
    measured = _obj(_obj(spec.get("meta")).get("measured"))
    legacy = _obj(spec.get("global"))  # Merged v1.2 retains original semantic fields.
    style = _obj(spec.get("style") or legacy.get("style_three"))
    lighting = _obj(spec.get("lighting") or legacy.get("lighting"))
    product_info = _obj(product.get("product")) or product
    brand = _obj(product.get("brand")) or product
    scene = _text(spec.get("scene_env")) or _text(legacy.get("scene_env"))
    # 空段（build_spec 因 VLM 未描述而标 empty）不是可复刻镜头，渲染前就剔除，
    # 否则会生成一个只有时间码、没有内容的「分镜N」，既过不了 B04（0.6s 太短）也会误导模型。
    shots = [shot for shot in (spec.get("shots") or [])
             if isinstance(shot, dict) and not shot.get("empty")]
    if drop_platform_card:
        shots = [shot for shot in shots if shot.get("transition_out") != "platform_card"]
    brands = _reference_brands(spec)
    sections: list[str] = []

    duration = _number(measured.get("duration_s"))
    width, height = _number(measured.get("width")), _number(measured.get("height"))
    look, palette, mood = (_text(style.get(key)) for key in ("look", "palette", "mood"))
    if (duration is not None and width is not None and height is not None
            and width.is_integer() and height.is_integer() and width > 0 and height > 0
            and look and palette and mood and shots):
        divisor = math.gcd(int(width), int(height))
        ratio = f"{int(width) // divisor}:{int(height) // divisor}"
        transition = "一镜到底，无剪辑" if len(shots) == 1 else "镜头间硬切衔接"
        # 片长取「要复刻的那几镜」的总跨度，不是参考片总长。
        # 实测坑：参考片 13.347s，其中 9.343s 之后是平台尾卡；若把 13.35 写进提示词，
        # 模型会照着 13.35 秒去编后面的动作（v7 结尾空 1.8s 就是这么来的）。
        first_start = _number(shots[0].get("start_s"))
        last_end = _number(shots[-1].get("end_s"))
        span = round(last_end - first_start, 2) if (first_start is not None and last_end is not None) else None
        if span is None:
            _warn("总体描述", "shots 缺 start_s/end_s，无法算复刻段时长")
        else:
            sections.append(f"总体描述：{look}，主色{palette}，情绪{mood}。"
                            f"全片 {span:.2f} 秒，{len(shots)} 个镜头，{transition}，{ratio}。")
    else:
        _warn("总体描述", "缺 meta.measured 时长/宽高、style 三要素或 shots")

    appearance = _text(product_info.get("appearance"))
    if appearance:
        sections.append("素材引用：@图片1 = 产品主图，唯一产品外观依据："
                        f"{appearance}；不参考其背景与摆放角度。")
    else:
        _warn("素材引用", "缺 product.appearance")

    onscreen = spec.get("onscreen_text")
    if not isinstance(onscreen, list):
        onscreen = [t for shot in shots for t in (shot.get("onscreen_text") or [])
                    if isinstance(t, dict)]
    copy = _endcard_copy(brand)
    if not copy or not any(copy in (_text(t.get("text")) or "")
                           for t in onscreen if isinstance(t, dict)):
        copy = None
        _warn("屏幕文字", "onscreen_text 无客户 brand.endcard 文案")

    for index, shot in enumerate(shots, 1):
        start, end = _number(shot.get("start_s")), _number(shot.get("end_s"))
        if start is None or end is None or end <= start:
            _warn(f"分镜{index}", "缺合法 start_s/end_s")
            continue
        # Merged v1.2 kept the old shot end at full-video duration; the measured
        # cut and separate endcard specify the shorter replicable action window.
        if drop_platform_card and spec.get("endcard"):
            cuts = measured.get("cut_points_s")
            if isinstance(cuts, list):
                within = [cut for value in cuts if (cut := _number(value)) is not None
                          and start < cut < end]
                if within:
                    end = min(within)
        lines = [f"分镜{index}（{start:.2f}-{end:.2f}）："]
        size = _text(shot.get("shot_size"))
        view = _text(shot.get("camera_view")) or _text(shot.get("camera_angle"))
        if size and view:
            camera = [SIZE_MAP.get(size, size), ANGLE_MAP.get(view, view)]
            movement = _text(shot.get("camera_move"))
            if movement:
                camera.append(CUT_MAP.get(movement, movement))
            speed = _text(shot.get("move_speed"))
            line = "机位与运镜：" + "，".join(camera)
            lines.append(line + (f"（速度{speed}）" if movement and speed else "") + "。")
        else:
            _warn(f"分镜{index} 机位与运镜", "缺 shot_size/camera_view")
        if scene:
            lines.append(f"环境：{_scrub(scene, brands).rstrip('。')}。")
        else:
            _warn(f"分镜{index} 环境", "缺 scene_env")
        subject = _text(shot.get("subject_primary"))
        if subject and (subject := _scrub(subject, brands)):
            lines.append(f"主体：{subject.rstrip('。')}。")
        else:
            _warn(f"分镜{index} 主体", "缺 subject_primary")
        presence = _obj(shot.get("product_presence"))
        position = _pos_from_bbox(_obj(presence.get("bbox_rel")))
        share, name = _number(presence.get("screen_share_pct")), _text(product_info.get("name"))
        if presence.get("present") is True and name and position and share is not None:
            lines.append(f"产品：@图片1 的 {name}，{position}，占画面约 {share:g}%。")
        elif presence.get("present") is True:
            _warn(f"分镜{index} 产品", "缺 product.name、bbox_rel 或 screen_share_pct")
        actions = []
        for step in (shot.get("action_steps") or []):
            if not isinstance(step, dict):
                continue
            values = [_text(step.get(key)) for key in ("t_s", "who", "physical_step", "direction")]
            if all(values):
                t, who, physical, direction = values
                physical = _scrub(physical, brands)
                if "黑色瓶盖" in physical and "黑色瓶盖" not in (appearance or ""):
                    physical = physical.replace("黑色瓶盖", "瓶盖")
                if physical:
                    actions.append(f"第{t}秒，{who}{physical}，{direction}")
        if actions:
            lines.append("动作：" + "；".join(actions) + "。")
        else:
            _warn(f"分镜{index} 动作", "缺完整 action_steps")
        props = []
        for prop in (shot.get("props") or []):
            if isinstance(prop, dict):
                values = [_text(prop.get(key)) for key in
                          ("item", "count_start", "count_end", "change_rule")]
                if all(values):
                    item, before, after, rule = values
                    props.append(f"{_scrub(item, brands)} {before}→{after} 个，{_scrub(rule, brands)}")
        if props:
            lines.append("道具：" + "；".join(props) + "。")
        contacts = []
        for contact in (shot.get("contact") or []):
            if isinstance(contact, dict):
                values = [_text(contact.get(key)) for key in
                          ("from", "to", "type", "direction", "driven")]
                if all(values):
                    origin, target, kind, direction, driven = (_scrub(v, brands) for v in values)
                    contacts.append(f"{origin} → {target}，{kind}，{direction}，{driven}")
        if contacts:
            lines.append("接触与受力：" + "；".join(contacts) + "。")
        else:
            _warn(f"分镜{index} 接触与受力", "缺完整 contact")
        ending = _text(shot.get("end_composition"))
        if ending and (ending := _scrub(ending, brands)):
            lines.append(f"结束构图：{ending.rstrip('。')}。")
        else:
            _warn(f"分镜{index} 结束构图", "缺 end_composition")
        if copy:
            lines.append(f"屏幕文字：{copy}。")
        sections.append("\n".join(lines))

    audio = _obj(spec.get("audio"))
    if audio.get("has_voice") is True:
        language = _text(audio.get("language"))
        language_name = LANGUAGE_MAP.get(language, language or "待人工确认")
        dialogue = [line for line in (audio.get("dialogue") or [])
                    if isinstance(line, dict) and _text(line.get("text"))]
        lines = [f"台词：语种为{language_name}，共 {len(dialogue)} 句（按语音段计）。"]
        probability = _number(audio.get("language_prob"))
        if probability is not None and probability < 0.8:
            lines.append("语种置信度低，需人工核对。")
        for index, line in enumerate(dialogue, 1):
            start, end = _number(line.get("t_from_s")), _number(line.get("t_to_s"))
            timing = (f"{_timecode(start)}-{_timecode(end)}" if start is not None and end is not None
                      else "时间码待人工确认")
            # ASR is the audio evidence: preserve every word of the original transcript.
            lines.append(f"{index}. [{timing}] {line['text']}")
        if not dialogue:
            transcript = (_text(audio.get("voice_transcript")) or _text(audio.get("transcript"))
                          or _text(audio.get("voice")))
            lines.append(f"原文（未分句，时间码待人工确认）：{transcript}" if transcript
                         else "音频实测有人声，台词待人工确认。")
            _warn("台词", "audio.has_voice 为 true，但无带时间码的 dialogue")
        sections.append("\n".join(lines))
    elif audio.get("has_voice") is False:
        sections.append("台词：无台词，不生成人物对白，不要口播。依据：音频实测无人声。")
    else:
        sections.append("台词：音频未分析成功，台词待人工确认。")
        _warn("台词", "音频未分析成功，台词待人工确认")
    notes = _text(audio.get("notes")) or _joined(audio.get("notes"))
    sound = []
    if notes:
        sound.append(f"音效/BGM：音频实测备注：{notes.rstrip('。')}。")
    if audio.get("music_likely") is True:
        sound.append("BGM：有背景音乐（曲风待客户确认）。")
    if sound:
        sections.append("\n".join(sound))
    else:
        _warn("音效/BGM", "缺音频实测备注或音乐判定")
    light_direction, quality, temp = (_text(lighting.get(key)) for key in
                                      ("direction", "quality", "color_temp"))
    if all((look, palette, light_direction, quality, temp)):
        line = (f"风格：{look}，{palette}；{LIGHT_MAP.get(light_direction, light_direction)}，"
                f"{_cn_quality(quality)}，{_cn_temp(temp)}色温")
        note = _text(lighting.get("notes"))
        sections.append(line + (f"，{_scrub(note, brands)}" if note else "") + "。")
    else:
        _warn("风格", "缺 style.look/palette 或 lighting.direction/quality/color_temp")
    consistency = _joined(product_info.get("consistency"))
    if consistency:
        sections.append(f"一致性：{consistency}。")
    else:
        _warn("一致性", "缺 product.consistency")
    negative = _joined(product_info.get("negative"))
    if negative:
        sections.append(f"负向清单：{negative}。")
    else:
        _warn("负向清单", "缺 product.negative")
    body = _sanitize_image_refs("\n\n".join(sections), asset_refs)
    header = _header(spec, shots, drop_platform_card=drop_platform_card)
    return "\n".join(header) + "\n\n" + body


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("spec")
    ap.add_argument("-p", "--product", required=True)
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--keep-platform-card", action="store_true")
    ap.add_argument("--assets", help="extract_assets.py 产出的 manifest.json")
    args = ap.parse_args()

    spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    product = json.loads(Path(args.product).read_text(encoding="utf-8"))
    assets_path = Path(args.assets) if args.assets else None
    assets = (json.loads(assets_path.read_text(encoding="utf-8")).get("assets")
              if assets_path else None)
    text = render(spec, product, drop_platform_card=not args.keep_platform_card,
                  assets=assets)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text + "\n", encoding="utf-8")
    print(f"OK {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
