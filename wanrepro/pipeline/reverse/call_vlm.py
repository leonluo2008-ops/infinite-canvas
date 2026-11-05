#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Call an OpenAI-compatible vision model with ordered JPEG frames."""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.request

DEFAULT_BASE_URL = "https://api.jxinapi.com/v1"
DEFAULT_MODEL = "gemini-3.8-flash"
KEY_ENV = "JXIN_API_KEY"
IMAGE_EXTENSIONS = {".jpg", ".jpeg"}
TIMESTAMP_RE = re.compile(r"(?:^|[_-])t(\d+(?:[._]\d+)?)(?:s)?$", re.IGNORECASE)
FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.IGNORECASE | re.DOTALL)


class InvalidResponseError(ValueError):
    """The provider response did not contain a JSON analysis object."""


def _post_json(url, payload, headers, timeout):
    """POST JSON through urllib; replace this function for fully offline tests."""
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8")


def _load_api_key() -> str:
    key = os.environ.get(KEY_ENV, "").strip()
    if key:
        return key

    # 整合包形态：包根 .env（app/.env）优先于开发机 hermes .env——客户机部署时把 key 放这里。
    # （本文件在 app/pipeline/reverse/ 下，parents[2] == app/；开发机仓内无 .env 时自然落到原路径。）
    candidates: list[Path] = [Path(__file__).resolve().parents[2] / ".env"]

    configured_home = os.environ.get("HERMES_HOME")
    if configured_home:
        candidates.append(Path(configured_home) / ".env")
    elif os.name == "nt":
        candidates.append(Path.home() / "AppData" / "Local" / "hermes" / ".env")
    else:
        candidates.append(Path.home() / ".hermes" / ".env")

    for env_path in candidates:
        try:
            lines = env_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            candidate = line.strip()
            if candidate.startswith(f"{KEY_ENV}="):
                return candidate.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def _positive_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a finite number greater than 0")
    return number


def _collect_frames(raw_frames: str) -> list[Path]:
    frames = []
    for raw_item in raw_frames.split(","):
        item = Path(raw_item.strip()).expanduser()
        if not raw_item.strip():
            raise ValueError("--frames contains an empty path")
        if not item.exists():
            raise FileNotFoundError(f"frame path does not exist: {item}")
        if item.is_dir():
            candidates = [path for path in item.iterdir()
                          if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS]
            if not candidates:
                raise ValueError(f"no JPEG images found in directory: {item}")
            frames.extend(_sort_frame_paths(candidates))
        elif item.is_file():
            if item.suffix.lower() not in IMAGE_EXTENSIONS:
                raise ValueError(f"frame must be a JPEG image (.jpg/.jpeg): {item}")
            frames.append(item)
        else:
            raise ValueError(f"frame path is not a file or directory: {item}")

    if not frames:
        raise ValueError("--frames did not resolve to any JPEG images")
    return frames


def _filename_time(path: Path):
    match = TIMESTAMP_RE.search(path.stem)
    if match:
        return float(match.group(1).replace("_", "."))
    if re.fullmatch(r"\d+(?:\.\d+)?s?", path.stem):
        return float(path.stem.rstrip("s"))
    return None


def _natural_sort_key(path: Path):
    parts = re.split(r"(\d+)", path.name.lower())
    return tuple((0, int(part)) if part.isdigit() else (1, part) for part in parts)


def _sort_frame_paths(paths: list[Path]) -> list[Path]:
    if all(_filename_time(path) is not None for path in paths):
        return sorted(paths, key=lambda path: (_filename_time(path), path.name))
    return sorted(paths, key=_natural_sort_key)


def _frame_time(path: Path, index: int) -> float:
    timestamp = _filename_time(path)
    return float(index) if timestamp is None else timestamp


def _build_messages(prompt: str, frames: list[Path]) -> list[dict]:
    content = [{"type": "text", "text": prompt}]
    for index, frame in enumerate(frames):
        timestamp = _frame_time(frame, index)
        content.append({"type": "text", "text": f"图{index + 1} 时间码 t={timestamp}s"})
        encoded = base64.b64encode(frame.read_bytes()).decode("ascii")
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{encoded}"},
        })
    return [{"role": "user", "content": content}]


def _extract_content(raw_response: str) -> str:
    envelope = json.loads(raw_response)
    try:
        content = envelope["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise InvalidResponseError("response is missing choices[0].message.content") from exc

    if isinstance(content, list):
        text_parts = [part.get("text", "") for part in content
                      if isinstance(part, dict) and part.get("type") == "text"]
        content = "\n".join(text_parts)
    if not isinstance(content, str):
        raise InvalidResponseError("response message content is not text")

    content = content.strip()
    fence = FENCE_RE.fullmatch(content)
    if fence:
        content = fence.group(1).strip()
    return content


def _http_error_body(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", errors="replace")
    except Exception:
        return ""


def _write_vision_error(out_path: Path, attempts: int, error_type: str,
                        error_message: str, raw_response: str) -> None:
    details = {
        "attempts": attempts,
        "last_error_type": error_type,
        "last_error": error_message,
        "raw_response": raw_response[:2000],
    }
    error_path = out_path.parent / "vision_error.json"
    error_path.parent.mkdir(parents=True, exist_ok=True)
    error_path.write_text(json.dumps(details, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frames", required=True, help="comma-separated JPEG paths and/or directories")
    parser.add_argument("--prompt", required=True, help="prompt template path")
    parser.add_argument("--out", required=True, help="output JSON path")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"model name (default: {DEFAULT_MODEL})")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help=f"OpenAI-compatible API base URL (default: {DEFAULT_BASE_URL})")
    parser.add_argument("--api-key", default=None,
                        help=f"API key override (default: {KEY_ENV} or HERMES_HOME/.env)")
    parser.add_argument("--max-retries", type=_positive_int, default=3,
                        help="maximum total attempts, including the first call (default: 3)")
    parser.add_argument("--timeout", type=_positive_float, default=120,
                        help="request timeout in seconds (default: 120)")
    args = parser.parse_args(argv)

    try:
        frames = _collect_frames(args.frames)
        prompt_path = Path(args.prompt).expanduser()
        if not prompt_path.is_file():
            raise FileNotFoundError(f"prompt file does not exist: {prompt_path}")
        prompt = prompt_path.read_text(encoding="utf-8")
        out_path = Path(args.out).expanduser()
        messages = _build_messages(prompt, frames)
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"ERROR input: {exc}", file=sys.stderr)
        return 2

    api_key = (args.api_key or "").strip() or _load_api_key()
    if not api_key:
        print(f"ERROR missing API key: set {KEY_ENV} or add it to HERMES_HOME/.env", file=sys.stderr)
        return 2

    payload = {"model": args.model, "messages": messages}
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    endpoint = args.base_url.rstrip("/") + "/chat/completions"
    raw_response = ""
    analysis = None
    last_error_type = "UnknownError"
    last_error_message = "unknown VLM request failure"

    for attempt in range(1, args.max_retries + 1):
        raw_response = ""
        try:
            response = _post_json(endpoint, payload, headers, args.timeout)
            raw_response = response.decode("utf-8", errors="replace") if isinstance(response, bytes) else str(response)
            content = _extract_content(raw_response)
            analysis = json.loads(content)
            if not isinstance(analysis, dict):
                raise InvalidResponseError("analysis JSON root must be an object")
            break
        except InvalidResponseError as exc:
            last_error_type = type(exc).__name__
            last_error_message = str(exc)
        except json.JSONDecodeError as exc:
            last_error_type = "InvalidJSONResponse"
            last_error_message = str(exc)
        except urllib.error.HTTPError as exc:
            raw_response = _http_error_body(exc)
            last_error_type = type(exc).__name__
            last_error_message = f"HTTP {exc.code}: {exc.reason}"
            if exc.code < 500:
                break
        except Exception as exc:
            last_error_type = type(exc).__name__
            last_error_message = str(exc) or type(exc).__name__

        if attempt < args.max_retries:
            time.sleep(2 ** (attempt - 1))

    if analysis is not None:
        output = json.dumps(analysis, ensure_ascii=False, indent=2) + "\n"
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(output, encoding="utf-8")
        except OSError as exc:
            print(f"ERROR could not write output JSON: {exc}", file=sys.stderr)
            return 2
        byte_count = len(output.encode("utf-8"))
        print(f"OK writes {out_path} bytes={byte_count}")
        return 0

    try:
        _write_vision_error(out_path, attempt, last_error_type, last_error_message, raw_response)
    except OSError as exc:
        print(f"ERROR could not write vision_error.json: {exc}", file=sys.stderr)
        return 2
    print(f"ERROR VLM failed after {attempt} attempt(s): {last_error_type}: {last_error_message}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
