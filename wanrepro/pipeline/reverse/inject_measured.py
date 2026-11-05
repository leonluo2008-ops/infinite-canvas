#!/usr/bin/env python3
"""Inject measured video metadata into a VLM analysis and record disagreements."""

import argparse
import json
import math
import sys
from pathlib import Path

TOLERANCE = 0.01
LEGACY_FIELDS = (
    "duration_s",
    "width",
    "height",
    "fps",
    "aspect_ratio",
    "shot_count",
    "cut_points_s",
)


def reject_constant(value):
    raise ValueError(f"invalid JSON constant: {value}")


def read_json(path):
    text = Path(path).read_text(encoding="utf-8")
    return json.loads(text, parse_constant=reject_constant)


def is_number(value):
    return type(value) in (int, float) and math.isfinite(value)


def numbers_equal(left, right):
    return is_number(left) and is_number(right) and abs(left - right) <= TOLERANCE


def arrays_equal(left, right):
    return (
        isinstance(left, list)
        and isinstance(right, list)
        and len(left) == len(right)
        and all(numbers_equal(a, b) for a, b in zip(left, right))
    )


def aspect_value(value):
    if not isinstance(value, str):
        return value if is_number(value) else None
    numerator, separator, denominator = value.partition(":")
    if not separator:
        try:
            result = float(value)
        except ValueError:
            return None
        return result if math.isfinite(result) else None
    try:
        numerator = float(numerator)
        denominator = float(denominator)
    except ValueError:
        return None
    if not math.isfinite(numerator) or not math.isfinite(denominator) or denominator == 0:
        return None
    return numerator / denominator


def values_equal(field, vlm_value, measured_value):
    if field == "cut_points_s":
        return arrays_equal(vlm_value, measured_value)
    if field == "aspect_ratio":
        vlm_ratio = aspect_value(vlm_value)
        measured_ratio = aspect_value(measured_value)
        return numbers_equal(vlm_ratio, measured_ratio)
    if is_number(vlm_value) and is_number(measured_value):
        return numbers_equal(vlm_value, measured_value)
    return vlm_value == measured_value


def get_tech_measurements(tech):
    meta = tech.get("meta")
    if not isinstance(meta, dict) or not is_number(meta.get("duration_s")):
        raise ValueError("missing or invalid meta.duration_s")

    for key in ("width", "height", "fps"):
        value = meta.get(key)
        if not is_number(value):
            raise ValueError(f"missing or invalid meta.{key}")
    cuts = tech.get("cuts")
    stable_cuts = cuts.get("stable_cuts") if isinstance(cuts, dict) else None
    if not isinstance(stable_cuts, list) or any(not is_number(point) for point in stable_cuts):
        raise ValueError("missing or invalid cuts.stable_cuts")

    return {
        "duration_s": meta["duration_s"],
        "width": meta["width"],
        "height": meta["height"],
        "fps": meta["fps"],
        "cut_points_s": stable_cuts,
    }


def validate_shots(vlm, source_path):
    shots = vlm.get("shots")
    if not isinstance(shots, list) or not shots:
        print(f"FAIL {source_path}: shots")
        return False
    # 单个镜头允许 contact 为空（例如平台尾卡没有物理接触），
    # 但整份分析至少要有一个镜头写出了接触/受力，否则等于这一镜什么都没发生。
    actionable = 0
    for index, shot in enumerate(shots):
        if not isinstance(shot, dict):
            print(f"FAIL {source_path}: shots[{index}] is not an object")
            return False
        contact = shot.get("contact")
        if contact is None:
            print(f"FAIL {source_path}: shots[{index}].contact missing")
            return False
        if not isinstance(contact, list):
            print(f"FAIL {source_path}: shots[{index}].contact must be an array")
            return False
        if contact:
            actionable += 1
    if actionable == 0:
        print(f"FAIL {source_path}: no shot reports any contact (nothing physical was observed)")
        return False
    return True


def inject(vlm, measured):
    meta = vlm.get("meta")
    if not isinstance(meta, dict):
        meta = {}
        vlm["meta"] = meta

    aspect_ratio = f"{measured['width']}:{measured['height']}"
    measured_by_field = {
        **measured,
        "aspect_ratio": aspect_ratio,
        "shot_count": len(measured["cut_points_s"]) + 1,
    }
    conflicts = vlm.get("conflicts")
    if not isinstance(conflicts, list):
        conflicts = []

    for field in LEGACY_FIELDS:
        if field not in meta:
            continue
        vlm_value = meta[field]
        measured_value = measured_by_field[field]
        if not values_equal(field, vlm_value, measured_value):
            conflict = {
                "field": field,
                "vlm": vlm_value,
                "measured": measured_value,
                "source": "inject_measured",
            }
            if conflict not in conflicts:
                conflicts.append(conflict)
        del meta[field]

    meta["measured"] = measured
    if conflicts:
        vlm["conflicts"] = conflicts
    elif "conflicts" in vlm:
        vlm["conflicts"] = conflicts
    return vlm


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vlm", required=True, help="VLM analysis JSON")
    parser.add_argument("--tech", required=True, help="measured tech.json")
    parser.add_argument("--out", required=True, help="output JSON path")
    args = parser.parse_args()

    try:
        vlm = read_json(args.vlm)
        if not isinstance(vlm, dict):
            raise ValueError("VLM root must be an object")
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        print(f"ERROR vlm: {exc}", file=sys.stderr)
        return 2

    if not validate_shots(vlm, args.vlm):
        return 1

    try:
        tech = read_json(args.tech)
        if not isinstance(tech, dict):
            raise ValueError("tech root must be an object")
        measured = get_tech_measurements(tech)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        print(f"ERROR tech: {exc}", file=sys.stderr)
        return 2

    output = inject(vlm, measured)
    try:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n",
            encoding="utf-8",
        )
    except (OSError, TypeError, ValueError) as exc:
        print(f"ERROR out: {exc}", file=sys.stderr)
        return 2

    print(f"OK {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
