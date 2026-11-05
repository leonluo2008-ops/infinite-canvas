#!/usr/bin/env python3
"""Offline mock tests for pipeline/reverse/call_vlm.py."""

import contextlib
import base64
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import traceback
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "pipeline" / "reverse" / "call_vlm.py"
SPEC_PATH = ROOT / "spec" / "analysis_v1_1.schema.json"
CHECK_PATH = ROOT / "scripts" / "check_analysis.py"

spec = importlib.util.spec_from_file_location("call_vlm", MODULE_PATH)
call_vlm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(call_vlm)


VALID_ANALYSIS = {
    "meta": {
        "measured": {
            "duration_s": 1.0,
            "width": 640,
            "height": 480,
            "fps": 30.0,
            "cut_points_s": [],
        }
    },
    "shots": [
        {
            "contact": [
                {
                    "from": "手",
                    "to": "盒子",
                    "type": "接触",
                    "direction": "向左",
                    "driven": "盒子向左移动",
                }
            ],
            "end_composition": "盒子停在画面中央，背景桌面清晰可见。",
        }
    ],
    "conflicts": [],
}


def response(content):
    return json.dumps({"choices": [{"message": {"content": content}}]}, ensure_ascii=False)


@contextlib.contextmanager
def mock_api_key():
    old_key = os.environ.get("JXIN_API_KEY")
    os.environ["JXIN_API_KEY"] = "offline-test-key"
    try:
        yield
    finally:
        if old_key is None:
            os.environ.pop("JXIN_API_KEY", None)
        else:
            os.environ["JXIN_API_KEY"] = old_key


def make_inputs(directory):
    frame = directory / "t0_5s.jpg"
    frame.write_bytes(b"offline jpeg fixture")
    prompt = directory / "prompt.md"
    prompt.write_text("只输出 JSON。", encoding="utf-8")
    return frame, prompt


def run_case(name, test):
    try:
        test()
    except Exception:
        print(f"FAIL {name}")
        traceback.print_exc()
        return False
    print(f"PASS {name}")
    return True


def scenario_a():
    with tempfile.TemporaryDirectory(prefix="vlm-a-") as tmp:
        directory = Path(tmp)
        frame, prompt = make_inputs(directory)
        out = directory / "result.json"
        calls = []
        original_post = call_vlm._post_json

        def invalid_json(url, payload, headers, timeout):
            calls.append((url, payload, headers, timeout))
            return "not json"

        call_vlm._post_json = invalid_json
        original_sleep = call_vlm.time.sleep
        call_vlm.time.sleep = lambda seconds: None
        try:
            with mock_api_key(), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                code = call_vlm.main(["--frames", str(frame), "--prompt", str(prompt), "--out", str(out)])
        finally:
            call_vlm._post_json = original_post
            call_vlm.time.sleep = original_sleep

        error_path = out.parent / "vision_error.json"
        assert code == 2, f"expected exit 2, got {code}"
        assert len(calls) == 3, f"expected 3 calls, got {len(calls)}"
        assert error_path.is_file(), "vision_error.json was not written"
        error = json.loads(error_path.read_text(encoding="utf-8"))
        assert error.get("attempts") == 3, error
        assert error.get("last_error_type"), error
        assert error.get("raw_response") == "not json", error


def scenario_b():
    with tempfile.TemporaryDirectory(prefix="vlm-b-") as tmp:
        directory = Path(tmp)
        frame, prompt = make_inputs(directory)
        out = directory / "result.json"
        frame_dir = directory / "frames"
        frame_dir.mkdir()
        fine_frame = frame_dir / "t0_035s.jpg"
        fine_frame.write_bytes(b"fine timestamp frame")
        directory_late = frame_dir / "t0_8s.jpg"
        directory_late.write_bytes(b"late directory frame")
        collected = call_vlm._collect_frames(str(frame_dir))
        assert collected == [fine_frame, directory_late], collected
        assert call_vlm._frame_time(collected[0], 0) == 0.035
        calls = []
        sleeps = []
        original_post = call_vlm._post_json
        original_sleep = call_vlm.time.sleep
        frame_late = directory / "t0_8s.jpg"
        frame_late.write_bytes(b"late frame")

        def flaky_post(url, payload, headers, timeout):
            calls.append((url, payload, headers, timeout))
            if len(calls) < 3:
                raise urllib.error.HTTPError(url, 500, "mock server error", {}, io.BytesIO(b"server error"))
            return response(json.dumps(VALID_ANALYSIS, ensure_ascii=False))

        call_vlm._post_json = flaky_post
        call_vlm.time.sleep = sleeps.append
        try:
            frames_arg = ",".join((str(frame_late), str(frame)))
            with mock_api_key(), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                code = call_vlm.main(["--frames", frames_arg, "--prompt", str(prompt), "--out", str(out)])
        finally:
            call_vlm._post_json = original_post
            call_vlm.time.sleep = original_sleep

        assert code == 0, f"expected exit 0, got {code}"
        assert len(calls) == 3, f"expected 3 calls, got {len(calls)}"
        assert sleeps == [1, 2], f"expected waits [1, 2], got {sleeps}"
        url, payload, headers, timeout = calls[0]
        content = payload["messages"][0]["content"]
        assert url.endswith("/chat/completions"), url
        assert headers["Authorization"] == "Bearer offline-test-key"
        assert timeout == 120
        frame_texts = [part["text"] for part in content if part.get("type") == "text" and part.get("text", "").startswith("图")]
        assert frame_texts == ["图1 时间码 t=0.8s", "图2 时间码 t=0.5s"], frame_texts
        images = [part for part in content if part.get("type") == "image_url"]
        assert len(images) == 2, images
        first_encoded = images[0]["image_url"]["url"].split(",", 1)[1]
        assert base64.b64decode(first_encoded) == frame_late.read_bytes()
        assert out.is_file(), "output JSON was not written"
        assert json.loads(out.read_text(encoding="utf-8")) == VALID_ANALYSIS


def scenario_c():
    with tempfile.TemporaryDirectory(prefix="vlm-c-") as tmp:
        directory = Path(tmp)
        frame, prompt = make_inputs(directory)
        out = directory / "result.json"
        original_post = call_vlm._post_json
        fenced = "```json\n" + json.dumps(VALID_ANALYSIS, ensure_ascii=False) + "\n```"
        call_vlm._post_json = lambda url, payload, headers, timeout: response(fenced)
        try:
            with mock_api_key(), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                code = call_vlm.main(["--frames", str(frame), "--prompt", str(prompt), "--out", str(out)])
        finally:
            call_vlm._post_json = original_post

        assert code == 0, f"expected exit 0, got {code}"
        assert json.loads(out.read_text(encoding="utf-8")) == VALID_ANALYSIS
        checked = subprocess.run(
            [sys.executable, str(CHECK_PATH), "--schema", str(SPEC_PATH), "--spec", str(out), "--normalize"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert checked.returncode == 0, f"check_analysis failed: {checked.stdout}\n{checked.stderr}"
        assert "SUMMARY pass" in checked.stdout, checked.stdout


def scenario_d():
    with tempfile.TemporaryDirectory(prefix="vlm-d-") as tmp:
        directory = Path(tmp)
        frame, prompt = make_inputs(directory)
        out = directory / "result.json"
        original_post = call_vlm._post_json
        call_vlm._post_json = lambda *args: (_ for _ in ()).throw(AssertionError("network mock should not run"))
        old_home = os.environ.get("HERMES_HOME")
        old_key = os.environ.get("JXIN_API_KEY")
        os.environ["HERMES_HOME"] = str(directory / "empty-hermes")
        os.environ.pop("JXIN_API_KEY", None)
        try:
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
                code = call_vlm.main(["--frames", str(frame), "--prompt", str(prompt), "--out", str(out)])
        finally:
            call_vlm._post_json = original_post
            if old_home is None:
                os.environ.pop("HERMES_HOME", None)
            else:
                os.environ["HERMES_HOME"] = old_home
            if old_key is None:
                os.environ.pop("JXIN_API_KEY", None)
            else:
                os.environ["JXIN_API_KEY"] = old_key

        assert code == 2, f"expected exit 2, got {code}"
        assert "JXIN_API_KEY" in stream.getvalue(), stream.getvalue()
        assert not out.exists(), "output should not exist without API key"


def main():
    results = [
        run_case("A_invalid_json_circuit_breaker", scenario_a),
        run_case("B_http_500_retry_backoff", scenario_b),
        run_case("C_fenced_json_schema_normalize", scenario_c),
        run_case("D_missing_api_key", scenario_d),
    ]
    passed = sum(results)
    print(f"SUMMARY {passed}/{len(results)} PASS")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
