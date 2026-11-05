#!/usr/bin/env python3
"""Validate analysis JSON structure, required values, and cut-point recall."""

import argparse
import json
import math
import sys
from pathlib import Path


def matches_type(value, expected):
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "number":
        return type(value) in (int, float)
    if expected == "integer":
        return type(value) is int or (type(value) is float and value.is_integer())
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return False


def child_path(path, key):
    return f"{path}.{key}" if path else str(key)


def validate(value, schema, path="", errors=None):
    if errors is None:
        errors = []

    expected = schema.get("type")
    if expected is not None and not matches_type(value, expected):
        errors.append((path or "$", f"expected {expected}"))
        return errors

    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append((child_path(path, key), "missing required key"))
        for key, child_schema in schema.get("properties", {}).items():
            if key in value:
                validate(value[key], child_schema, child_path(path, key), errors)

    if isinstance(value, list):
        minimum = schema.get("minItems")
        if minimum is not None and len(value) < minimum:
            errors.append((path or "$", f"must contain at least {minimum} item(s)"))
        item_schema = schema.get("items")
        if item_schema is not None:
            for index, item in enumerate(value):
                validate(item, item_schema, f"{path}[{index}]" if path else f"[{index}]", errors)

    if isinstance(value, str):
        minimum = schema.get("minLength")
        if minimum is not None and len(value) < minimum:
            errors.append((path or "$", f"must contain at least {minimum} character(s)"))

    # enum：值必须落在给定集合里（不实现它就等于规则静默失效——2026-10-28 实测过）
    enum = schema.get("enum")
    if enum is not None and value not in enum:
        errors.append((path or "$", f"must be one of {enum}, got {value!r}"))

    return errors


def reject_constant(value):
    raise ValueError(f"invalid JSON constant: {value}")


def read_json(path, normalize=False):
    text = Path(path).read_text(encoding="utf-8")
    if normalize:
        lines = text.strip().splitlines()
        if len(lines) >= 2 and lines[0].strip().startswith("```") and lines[-1].strip() == "```":
            text = "\n".join(lines[1:-1])
    return json.loads(text, parse_constant=reject_constant)


def nonempty(value):
    return value is not None and value != "" and value != [] and value != {}


def find_key(value, key, path=""):
    found = []
    if isinstance(value, dict):
        for child_key, child in value.items():
            child_location = child_path(path, child_key)
            if child_key == key:
                found.append((child_location, child))
            found.extend(find_key(child, key, child_location))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(find_key(child, key, f"{path}[{index}]"))
    return found


def resolve_path(value, path):
    current = value
    for key in path.split("."):
        if not isinstance(current, dict) or key not in current:
            return False, None
        current = current[key]
    return True, current


def parse_expected(raw):
    key, separator, value = raw.partition("=")
    if not separator or not key or not value:
        raise ValueError(f"expected KEY=VALUE, got {raw!r}")
    try:
        expected = json.loads(value, parse_constant=reject_constant)
    except (json.JSONDecodeError, ValueError):
        expected = value
    return key, expected


def compare_expected(actual, expected, tolerance):
    if type(actual) in (int, float) and type(expected) in (int, float):
        return math.isfinite(actual) and math.isfinite(expected) and abs(actual - expected) <= tolerance
    return actual == expected


def check_expect(spec, raw, tolerance):
    key, expected = parse_expected(raw)
    if "." in key:
        found, actual = resolve_path(spec, key)
        if not found:
            return [(key, "missing key")]
        locations = [(key, actual)]
    else:
        locations = find_key(spec, key)
        if not locations:
            return [(key, "missing key")]
        if len(locations) != 1:
            measured = spec.get("meta", {}).get("measured", {}) if isinstance(spec, dict) else {}
            if isinstance(measured, dict) and key in measured:
                locations = [(f"meta.measured.{key}", measured[key])]
            else:
                return [(key, "ambiguous key; use a dotted path")]
    failures = []
    for location, actual in locations:
        if not compare_expected(actual, expected, tolerance):
            failures.append((location, f"expected {expected!r}, got {actual!r}"))
    return failures


def check_required_path(spec, path):
    if not path or ".." in path or path.startswith(".") or path.endswith("."):
        raise ValueError(f"invalid --require path: {path!r}")
    segments = path.split(".")
    for segment in segments:
        key = segment[:-2] if segment.endswith("[]") else segment
        if not key or "[" in key or "]" in key:
            raise ValueError(f"invalid --require path: {path!r}")
    errors = []

    def visit(value, index, location):
        segment = segments[index]
        array_each = segment.endswith("[]")
        key = segment[:-2] if array_each else segment
        if not key or "[" in key or "]" in key:
            raise ValueError(f"invalid --require path: {path!r}")
        key_location = child_path(location, key)
        if not isinstance(value, dict) or key not in value:
            errors.append((key_location, "missing required key"))
            return
        child = value[key]
        if array_each:
            if not isinstance(child, list):
                errors.append((key_location, "expected array"))
                return
            if not child:
                errors.append((key_location, "array must not be empty"))
                return
            for item_index, item in enumerate(child):
                item_location = f"{key_location}[{item_index}]"
                if index == len(segments) - 1:
                    if not nonempty(item):
                        errors.append((item_location, "must not be empty"))
                elif not isinstance(item, dict):
                    errors.append((item_location, "expected object"))
                else:
                    visit(item, index + 1, item_location)
            return
        if index < len(segments) - 1:
            if not isinstance(child, dict):
                errors.append((key_location, "expected object"))
            else:
                visit(child, index + 1, key_location)
        elif not nonempty(child):
            errors.append((key_location, "must not be empty"))

    visit(spec, 0, "")
    return errors


def match_cuts(predictions, cuts, tolerance):
    predicted = sorted(predictions)
    truth = sorted(cuts)
    pred_index = truth_index = matched = 0
    while pred_index < len(predicted) and truth_index < len(truth):
        difference = predicted[pred_index] - truth[truth_index]
        if abs(difference) <= tolerance:
            matched += 1
            pred_index += 1
            truth_index += 1
        elif difference < 0:
            pred_index += 1
        else:
            truth_index += 1
    return matched


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", required=True, help="JSON Schema file")
    parser.add_argument("--spec", required=True, help="analysis JSON file")
    parser.add_argument("--normalize", action="store_true", help="strip an outer Markdown JSON fence")
    parser.add_argument("--expect", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--require", action="append", default=[], metavar="PATH")
    parser.add_argument("--expect-conflicts", action="store_true")
    parser.add_argument("--eval-groundtruth", metavar="GT.json")
    parser.add_argument("--tolerance", type=float, default=0.15)
    parser.add_argument("--min-recall", type=float, default=0.8)
    args = parser.parse_args()

    if not math.isfinite(args.tolerance) or args.tolerance < 0:
        print("ERROR args: --tolerance must be a finite non-negative number", file=sys.stderr)
        return 2
    if not math.isfinite(args.min_recall) or not 0 <= args.min_recall <= 1:
        print("ERROR args: --min-recall must be between 0 and 1", file=sys.stderr)
        return 2
    try:
        for expectation in args.expect:
            parse_expected(expectation)
        for required_path in args.require:
            check_required_path({}, required_path)
    except ValueError as exc:
        print(f"ERROR args: {exc}", file=sys.stderr)
        return 2

    try:
        schema = read_json(args.schema)
        if not isinstance(schema, dict):
            raise ValueError("schema root must be an object")
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        print(f"ERROR schema: {exc}", file=sys.stderr)
        return 2

    try:
        spec = read_json(args.spec, normalize=args.normalize)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        print(f"ERROR spec: {exc}", file=sys.stderr)
        return 2

    errors = validate(spec, schema)
    for expectation in args.expect:
        errors.extend(check_expect(spec, expectation, 0.01))
    for required_path in args.require:
        errors.extend(check_required_path(spec, required_path))
    if args.expect_conflicts:
        conflicts = spec.get("conflicts") if isinstance(spec, dict) else None
        if not isinstance(conflicts, list) or not conflicts:
            errors.append(("conflicts", "must be a non-empty array"))

    if args.eval_groundtruth:
        try:
            groundtruth = read_json(args.eval_groundtruth)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            print(f"ERROR groundtruth: {exc}", file=sys.stderr)
            return 2
        cuts = groundtruth.get("cuts") if isinstance(groundtruth, dict) else None
        if not isinstance(cuts, list) or not cuts or any(type(cut) not in (int, float) or not math.isfinite(cut) for cut in cuts):
            print("ERROR groundtruth: expected a non-empty numeric cuts array", file=sys.stderr)
            return 2
        found, predictions = resolve_path(spec, "meta.measured.cut_points_s")
        if not found or not isinstance(predictions, list) or not predictions:
            errors.append(("groundtruth", "no predictions to compare"))
        elif any(type(point) not in (int, float) or not math.isfinite(point) for point in predictions):
            errors.append(("groundtruth", "predictions must be finite numbers"))
        else:
            matched = match_cuts(predictions, cuts, args.tolerance)
            recall = matched / len(cuts)
            print(f"RECALL {recall:.2f} ({matched}/{len(cuts)}, tolerance={args.tolerance:g})")
            if recall < args.min_recall:
                errors.append(("groundtruth", f"recall {recall:.2f} is below minimum {args.min_recall:g}"))

    errors = list(dict.fromkeys(errors))
    for path, reason in errors:
        print(f"FAIL {path}: {reason}")
    print("SUMMARY fail" if errors else "SUMMARY pass")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
