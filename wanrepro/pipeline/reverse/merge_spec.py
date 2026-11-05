#!/usr/bin/env python3
"""Merge measured M1 fields into an existing semantic analysis spec."""

import argparse
import json
import sys
from pathlib import Path


def reject_constant(value):
    raise ValueError(f"invalid JSON constant: {value}")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"), parse_constant=reject_constant)


def merge(old_spec, injected):
    if not isinstance(old_spec, dict):
        raise ValueError("old spec root must be an object")
    if not isinstance(injected, dict):
        raise ValueError("injected root must be an object")

    old_shots = old_spec.get("shots")
    injected_shots = injected.get("shots")
    if not isinstance(old_shots, list) or not isinstance(injected_shots, list):
        raise ValueError("both documents must contain a shots array")
    if len(old_shots) != len(injected_shots):
        print(f"FAIL shots count mismatch: old={len(old_shots)} injected={len(injected_shots)}")
        return None

    injected_meta = injected.get("meta")
    if not isinstance(injected_meta, dict) or not isinstance(injected_meta.get("measured"), dict):
        raise ValueError("injected document must contain meta.measured object")
    if not isinstance(injected.get("conflicts"), list):
        raise ValueError("injected document must contain conflicts array")
    if not isinstance(old_spec.get("meta"), dict):
        raise ValueError("old spec must contain meta object")

    # Copy only the injected M1 fields; all other old semantic data remains untouched.
    result = dict(old_spec)
    result["meta"] = dict(old_spec["meta"])
    result["meta"]["measured"] = injected_meta["measured"]
    result["conflicts"] = injected["conflicts"]
    result["shots"] = []

    for index, (old_shot, injected_shot) in enumerate(zip(old_shots, injected_shots)):
        if not isinstance(old_shot, dict) or not isinstance(injected_shot, dict):
            raise ValueError(f"shots[{index}] must be an object in both documents")
        if "contact" not in injected_shot or "end_composition" not in injected_shot:
            raise ValueError(f"injected shots[{index}] must contain contact and end_composition")
        shot = dict(old_shot)
        shot["contact"] = injected_shot["contact"]
        shot["end_composition"] = injected_shot["end_composition"]
        result["shots"].append(shot)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True, help="old semantic spec JSON")
    parser.add_argument("--injected", required=True, help="M1-enriched intermediate JSON")
    parser.add_argument("--out", required=True, help="output spec_v2 JSON path")
    parser.add_argument("--shots-mode", choices=("strict", "replicable"), default="strict",
                        help="strict=两端镜头数必须相等；replicable=只把带 contact 的注入镜头并入 shots，"
                             "其余（如平台尾卡）原样存到顶层 endcard，不参与复刻")
    args = parser.parse_args()

    input_paths = {Path(args.spec).resolve(), Path(args.injected).resolve()}
    out_path = Path(args.out)
    if out_path.resolve() in input_paths:
        print("ERROR out: output path must differ from both input paths", file=sys.stderr)
        return 2

    try:
        old_spec = read_json(args.spec)
        injected = read_json(args.injected)
        endcard = None
        if args.shots_mode == "replicable" and isinstance(injected.get("shots"), list):
            keep = [s for s in injected["shots"] if isinstance(s, dict) and s.get("contact")]
            endcard = [s for s in injected["shots"] if not (isinstance(s, dict) and s.get("contact"))]
            injected = dict(injected)
            injected["shots"] = keep
        result = merge(old_spec, injected)
        if result is not None and endcard is not None:
            result["endcard"] = endcard
        if result is not None and args.shots_mode == "replicable":
            # 旧 spec 的每个 shot 覆盖的是"整条参考片"，而实测切点说明片尾还有平台尾卡。
            # 不把镜头边界对齐到实测切点，提示词就会写成"全片 13.35 秒、1 个镜头（0.00-9.34）"——
            # 模型会照 13.35 秒去补后面的动作（v7 结尾空 1.8 秒就是这么来的）。
            cuts = (result.get("meta", {}).get("measured", {}) or {}).get("cut_points_s") or []
            shots = result.get("shots") or []
            # 实测切点切出来的是「全部分段」，其中被剔到 endcard 的那一段不参与复刻。
            replicated = len(cuts) + 1 - len(endcard or [])
            if cuts and replicated >= 1 and len(shots) == replicated:
                bounds = [0.0] + [float(c) for c in cuts[:replicated]]
                for index, shot in enumerate(shots):
                    shot["start_s"] = round(bounds[index], 3)
                    shot["end_s"] = round(bounds[index + 1], 3)
                result.setdefault("conflicts", []).append({
                    "field": "shots[].start_s/end_s", "vlm": "旧 spec 覆盖整片",
                    "measured": f"按实测切点对齐为 {bounds} + 片尾",
                    "source": "merge_spec",
                })
        if result is not None:
            # 旧 spec 的 meta 是历史人工值，可能已过期（实测过：shot_count 与 cut_points 都可能是空的）。
            # 保留原值（下游兼容），但把差异显式记进 conflicts，避免下游误读成"单镜到底"。
            measured = result.get("meta", {}).get("measured", {})
            legacy = result.get("meta", {})
            pairs = [
                ("duration_s", legacy.get("duration_s"), measured.get("duration_s"), 0.01),
                ("fps", legacy.get("fps"), measured.get("fps"), 0.01),
                ("shot_count", legacy.get("shot_count"),
                 (len(measured.get("cut_points_s") or []) + 1) if measured.get("cut_points_s") is not None else None, 0),
            ]
            conflicts = result.setdefault("conflicts", [])
            for field, old_value, new_value, tol in pairs:
                if old_value is None or new_value is None:
                    continue
                try:
                    if abs(float(old_value) - float(new_value)) > tol:
                        conflicts.append({"field": field, "vlm": old_value, "measured": new_value,
                                          "source": "legacy_spec"})
                except (TypeError, ValueError):
                    continue
        if result is None:
            return 1
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n",
            encoding="utf-8",
        )
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        print(f"ERROR merge: {exc}", file=sys.stderr)
        return 2

    print(f"OK {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
