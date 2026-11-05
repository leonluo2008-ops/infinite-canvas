#!/usr/bin/env python3
"""Rebuild a reverse analysis from a video in eight logged steps, including audio."""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
REVERSE = Path(__file__).resolve().parent
CHECKER = ROOT / "scripts" / "check_analysis.py"
SCHEMA = ROOT / "spec" / "analysis_v1_1.schema.json"


class StepError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def load_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"not a JSON object: {path}")
    return value


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2,
                               sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")


def run_script(filename: str, *args: object) -> str:
    command = [sys.executable, str(REVERSE / filename), *(str(arg) for arg in args)]
    env = {key: value for key, value in os.environ.items()
           if key not in ("PYTHONPATH", "PYTHONHOME")}
    result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if result.returncode:
        raise StepError(result.returncode, (result.stdout + result.stderr).strip())
    return result.stdout.strip()


def call_vlm_extra_args(model: str | None = None, base_url: str | None = None,
                        api_key: str | None = None) -> list[str]:
    """外部渠道透传（M6-T7-C）→ call_vlm.py 附加参数。

    都不给 → []：call_vlm 拿到与旧版完全一致的命令行（默认模型 + 环境/.env key，零回归）。
    归一口径在 bridge/UI 侧做；这里只忠实转发。
    """
    extra: list[str] = []
    if (model or "").strip():
        extra += ["--model", model.strip()]
    if (base_url or "").strip():
        extra += ["--base-url", base_url.strip()]
    if (api_key or "").strip():
        extra += ["--api-key", api_key.strip()]
    return extra


def fresh_outputs(outdir: Path, skip_vlm: bool) -> None:
    for name in ("tech.json", "audio.json", "spec.json", "spec_merged.json", "prompt.md",
                 "run.json", "vision_error.json"):
        (outdir / name).unlink(missing_ok=True)
    if not skip_vlm:
        (outdir / "raw_vision.json").unlink(missing_ok=True)
    for path in (outdir / "frames", outdir / "_work"):
        if path.exists():
            shutil.rmtree(path)


def collect_frames(tech: dict, outdir: Path) -> tuple[list[Path], str]:
    entries = tech.get("frames")
    if not isinstance(entries, list):
        raise ValueError("dissect did not return freshly extracted frames")
    folder = outdir / "_work" / "vlm_frames"
    folder.mkdir(parents=True, exist_ok=True)
    frames = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        t = entry.get("t_s")
        if type(t) not in (int, float) or not math.isfinite(t) or not float(t).is_integer():
            continue
        frame = Path(entry.get("file", ""))
        if not frame.is_file() or frame.parent.resolve() != (outdir / "frames").resolve():
            raise ValueError(f"current run frame missing or outside output folder: {frame}")
        chosen = folder / f"f_{int(t)}s.jpg"
        shutil.copyfile(frame, chosen)
        frames.append(chosen)
    if not frames or [int(p.stem[2:-1]) for p in frames] != list(range(len(frames))):
        raise ValueError("dissect did not provide one consecutive JPEG per second from zero")
    return frames, f"每秒 1 帧，共 {len(frames)} 张"


def check_spec(spec_path: Path, tech: dict, work: Path) -> str:
    spec = load_json(spec_path)
    measured = spec.get("meta", {}).get("measured", {})
    cuts = tech["cuts"]["stable_cuts"]
    if measured.get("cut_points_s") != cuts:
        raise ValueError("spec measured cut_points_s differ from fresh tech.stable_cuts")
    segments = sorted([*spec.get("shots", []), *spec.get("endcard", [])],
                      key=lambda item: item["start_s"])
    expected = [0.0, *cuts, tech["meta"]["duration_s"]]
    if len(segments) != len(expected) - 1 or any(
            item["start_s"] != left or item["end_s"] != right
            for item, left, right in zip(segments, expected, expected[1:])):
        raise ValueError("spec segments do not match fresh measured boundaries")
    # check_analysis requires nonempty physical contact. Unobserved segments are
    # deliberately empty and are excluded only in this temporary validation view.
    active = [item for item in spec["shots"] if not item.get("empty")]
    checked = dict(spec)
    checked["shots"] = active
    check_path = work / "check_spec.json"
    write_json(check_path, checked)
    command = [sys.executable, str(CHECKER), "--schema", str(SCHEMA),
               "--spec", str(check_path), "--expect",
               f"duration_s={tech['meta']['duration_s']}",
               "--require", "shots[].contact", "--require", "shots[].end_composition"]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if result.returncode:
        raise StepError(result.returncode, (result.stdout + result.stderr).strip())
    return result.stdout.strip()


def gate_without_generation(prompt: Path, tech: Path, images: list[Path],
                            duration: float) -> tuple[int, str]:
    """离线跑 gate 的检查项：不查余额、不查在途任务，也不签发提交 token。

    以前是把 credits/running_tasks 打成 -1，结果 G10/G11 在报表里显示成 FAIL——
    明明"没查"，却写成"不合格"，这是假信息。现在走 gate 自己的 --no-live。
    """
    module_spec = importlib.util.spec_from_file_location("reverse_audit_gate", REVERSE / "gate.py")
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    saved_argv = sys.argv
    output = io.StringIO()
    try:
        sys.argv = [str(REVERSE / "gate.py"), "--prompt", str(prompt),
                    "--tech", str(tech), "--images", ",".join(map(str, images)),
                    "--no-live",
                    # 预算上限按实际时长算：480P 实测 2 灵感值/秒，留 10 点余量。
                    # 写死 30 会误伤 16.7 秒的片子（33.4 点 > 30），这不是超预算，是上限设错。
                    "--duration", str(duration),
                    "--max-credits", str(int(duration * 2) + 10)]
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            try:
                code = module.main()
            except SystemExit as exc:
                raise StepError(2, f"gate aborted: {exc}; {output.getvalue()}") from exc
    finally:
        sys.argv = saved_argv
    return code, output.getvalue()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--outdir", type=Path, required=True)
    parser.add_argument("--product", type=Path)
    parser.add_argument("--legacy-spec", type=Path)
    parser.add_argument("--assets", help="comma-separated input image paths for gate and prompt")
    parser.add_argument("--skip-vlm", action="store_true", help="use existing raw_vision.json")
    parser.add_argument("--model", default=None,
                        help="call_vlm 模型名透传（M6-T7-C；默认其内置默认模型）")
    parser.add_argument("--base-url", default=None, help="call_vlm base URL 透传（默认其内置默认值）")
    parser.add_argument("--api-key", default=None,
                        help="call_vlm API key 透传（默认读 JXIN_API_KEY / .env）")
    args = parser.parse_args(argv)
    video = args.video.expanduser().resolve()
    outdir = args.outdir.expanduser().resolve()
    product = args.product.expanduser().resolve() if args.product else None
    legacy = args.legacy_spec.expanduser().resolve() if args.legacy_spec else None
    if not video.is_file() or (product and not product.is_file()) or (legacy and not legacy.is_file()):
        parser.error("video, product, and legacy-spec must exist when specified")
    if video == outdir or outdir in video.parents or (legacy and (legacy == outdir or outdir in legacy.parents)):
        parser.error("outdir must not contain the video or legacy spec")
    outdir.mkdir(parents=True, exist_ok=True)
    raw = outdir / "raw_vision.json"
    if args.skip_vlm and not raw.is_file():
        parser.error("--skip-vlm requires existing raw_vision.json in outdir")
    fresh_outputs(outdir, args.skip_vlm)
    tech_path, audio_path, spec_path = (outdir / "tech.json", outdir / "audio.json",
                                        outdir / "spec.json")
    work = outdir / "_work"
    steps: list[dict] = []

    def step(index: int, name: str, artifacts: list[Path], action, *, advisory=False,
             warn_on_code=False):
        print(f"[{index}/8] {name} ...", end=" ", flush=True)
        started = time.perf_counter()
        code, detail = 0, ""
        fatal = False
        try:
            result = action()
            if isinstance(result, tuple):
                code, detail = result
            else:
                detail = str(result or "")
            if code and not advisory:
                raise StepError(code, detail)
        except StepError as exc:
            code, detail, fatal = exc.code, str(exc), True
        except (OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
            code, detail, fatal = 2, f"{type(exc).__name__}: {exc}", True
        except Exception as exc:
            code, detail, fatal = 2, f"{type(exc).__name__}: {exc}", True
        seconds = round(time.perf_counter() - started, 3)
        steps.append({"name": name, "duration_s": seconds,
                      "artifacts": [str(p) for p in artifacts if p.exists()], "exit_code": code})
        status = "WARN" if code and warn_on_code else "FAIL" if code else "OK"
        print(f"{status}（{seconds:.2f}s）" + (f" {detail}" if detail else ""), flush=True)
        if code and fatal:
            raise StepError(code, f"[{index}/8] {name} 失败，exit={code}: {detail}")
        return code, detail

    def measurement() -> str:
        run_script("dissect.py", str(video), "-o", str(outdir))
        tech = load_json(tech_path)
        if tech.get("source") != str(video) or "stable_cuts" not in tech.get("cuts", {}):
            raise ValueError("dissect produced an incomplete or wrong-source tech.json")
        return f"时长 {tech['meta']['duration_s']}s / 切点 {len(tech['cuts']['stable_cuts'])} 个"

    def audio_analysis() -> tuple[int, str]:
        try:
            run_script("analyze_audio.py", "--video", video, "--out", audio_path)
            result = load_json(audio_path)
        except Exception as exc:
            # Even a crashed ASR subprocess must override stale/visual audio with unknown.
            try:
                from .analyze_audio import unknown_audio
            except ImportError:
                from analyze_audio import unknown_audio
            result = unknown_audio(f"音频分析失败：{type(exc).__name__}: {exc}")
            write_json(audio_path, result)
        if result.get("has_voice") is None:
            return 1, "; ".join(result.get("notes") or ["音频分析未知，台词待人工确认"])
        return 0, (f"人声 {result['has_voice']} / 语种 {result.get('language')} / "
                   f"语音段 {len(result.get('dialogue') or [])}")

    def vlm() -> str:
        if args.skip_vlm:
            load_json(raw)
            return f"复用显式 --skip-vlm 的 {raw}"
        if not ((args.api_key or "").strip() or os.environ.get("JXIN_API_KEY")):
            raise ValueError("JXIN_API_KEY is required; .env files are not read by analyze.py")
        frames = sorted((work / "vlm_frames").glob("f_*s.jpg"),
                        key=lambda p: int(p.stem[2:-1]))
        if not frames:
            raise ValueError("no frames from current measurement")
        return run_script("call_vlm.py", "--frames", ",".join(map(str, frames)),
                          "--prompt", REVERSE / "prompts" / "vision_v1_3.md",
                          "--out", raw,
                          *call_vlm_extra_args(args.model, args.base_url, args.api_key))

    def synthesis() -> str:
        run_script("build_spec.py", "--vlm", work / "inj.json", "--tech", tech_path,
                   "--audio", audio_path, "--out", spec_path)
        spec = load_json(spec_path)
        if not legacy:
            return f"分段 {len(spec['shots']) + len(spec['endcard'])} 个"
        merge_source = work / "merge_input.json"
        merged_segments = sorted([*spec["shots"], *spec["endcard"]],
                                 key=lambda shot: shot["start_s"])
        write_json(merge_source, {**spec, "shots": merged_segments})
        output = outdir / "spec_merged.json"
        run_script("merge_spec.py", "--spec", legacy, "--injected", merge_source,
                   "--shots-mode", "replicable", "--out", output)
        # Legacy merge copies only picture measurements; current audio owns this field.
        merged = load_json(output)
        merged["audio"] = spec["audio"]
        write_json(output, merged)
        return f"分段 {len(merged_segments)} 个 / legacy 合并 {output.name}"

    def deliver() -> tuple[int, str]:
        if product is None:
            return 0, "未提供 --product，跳过提示词和门禁"
        render_spec = outdir / "spec_merged.json" if legacy else spec_path
        render_doc = load_json(render_spec)
        render_doc["shots"] = [shot for shot in render_doc["shots"]
                                if not shot.get("empty")]
        render_view = work / "render_spec.json"
        write_json(render_view, render_doc)
        images = [Path(p.strip()).expanduser().resolve() for p in args.assets.split(",") if p.strip()] if args.assets else []
        if not images:
            asset = load_json(product).get("product", {}).get("asset", {}).get("image")
            if asset:
                images = [(ROOT / asset).resolve()]
        prompt = outdir / "prompt.md"
        run_script("render_prompt.py", render_view, "-p", product, "-o", prompt)
        active_shots = render_doc["shots"]
        if not active_shots:
            raise ValueError("no non-empty shots to audit")
        duration = max(shot["end_s"] for shot in active_shots) - min(
            shot["start_s"] for shot in active_shots)
        code, output = gate_without_generation(prompt, tech_path, images, duration)
        fail_count = sum("[FAIL]" in line for line in output.splitlines())
        return code, f"提示词完成；门禁 FAIL {fail_count} 项（未调用 wan）"

    try:
        step(1, "视频实测", [tech_path, outdir / "frames"], measurement)
        step(2, "音频分析", [audio_path], audio_analysis, advisory=True, warn_on_code=True)
        step(3, "逐秒收帧", [work / "vlm_frames"],
             lambda: collect_frames(load_json(tech_path), outdir)[1])
        step(4, "视觉模型" if not args.skip_vlm else "读取现有 VLM", [raw], vlm)
        step(5, "注入实测", [work / "inj.json"],
             lambda: run_script("inject_measured.py", "--vlm", raw, "--tech", tech_path,
                                "--out", work / "inj.json"))
        step(6, "合成实测分段", [spec_path, outdir / "spec_merged.json"], synthesis)
        checked_path = outdir / "spec_merged.json" if legacy else spec_path
        step(7, "分析断言", [work / "check_spec.json"],
             lambda: check_spec(checked_path, load_json(tech_path), work))
        step(8, "提示词与门禁", [outdir / "prompt.md"],
             deliver, advisory=True)
        code = 0
    except StepError as exc:
        print(exc, file=sys.stderr, flush=True)
        code = exc.code or 1
    finally:
        run_path = outdir / "run.json"
        write_json(run_path, {"video": str(video), "steps": steps, "exit_code": code})
        print("步骤 | 耗时(s) | exit | 产物")
        for item in steps:
            print(f"{item['name']} | {item['duration_s']:.3f} | {item['exit_code']} | "
                  + ", ".join(Path(p).name for p in item["artifacts"]))
        print(f"汇总 exit={code}；记录 {run_path}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
