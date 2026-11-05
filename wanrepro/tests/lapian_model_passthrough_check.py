#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M6-T7-C 拉片渠道透传探针：只测参数拼装 / baseUrl 归一 / 默认一致性，不调模型、不花钱。

跑法：`py -3 tests/lapian_model_passthrough_check.py`    退出码 0 = 全过，1 = 有 FAIL

判据
----
Q1  bridge 透传拼装：/api/lapian/run body 的可选 model / channel{baseUrl,key,model}
    → （analyze.py 附加 argv, 子进程附加环境）；key 绝不上命令行（日志安全）。
Q2  baseUrl 归一两种形态：裸域名自动补 /v1beta；显式带 /v1、/v1beta 版本段则尊重原值。
Q3  不传时（含今天前端真实 body {id,intent}）命令行与环境与旧版逐字节一致。
Q4  extract.resolve_endpoint：默认端点逐字节不变；透传模型/渠道拼装同口径。
Q5  call_vlm --api-key 优先于环境/.env；--base-url/--model 忠实进请求（mock 网络，不出网）。
Q6  指纹「拆解模型」兼容：老指纹没有该字段 = 默认模型，不触发重拆（零回归）。
Q7  四个 CLI（analyze.py 根/extract/reverse analyze/call_vlm）都接上 --model/--base-url/--api-key。
Q8  reverse analyze 的 call_vlm 附加参数：都不给 → []（call_vlm 命令行与旧版一致）。
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REVERSE = ROOT / "pipeline" / "reverse"

sys.path.insert(0, str(ROOT / "ui"))
sys.path.insert(0, str(REVERSE))

import server  # noqa: E402
import extract  # noqa: E402
import call_vlm  # noqa: E402

results = []


def check(ok: bool, name: str, detail: str = "") -> bool:
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"｜{detail}" if detail else ""))
    return ok


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


root_analyze = load_module("_probe_root_analyze", ROOT / "analyze.py")
spec_analyze = load_module("_probe_reverse_analyze", REVERSE / "analyze.py")

# --------------------------------------------------------------- Q1 bridge 透传拼装

argv, env = server.lapian_channel_passthrough({})
check(argv == [] and env == {}, "Q1 空body → 不透传", f"{argv} / {env}")

argv, env = server.lapian_channel_passthrough({"id": "t1", "intent": "卖点多一点"})
check(argv == [] and env == {}, "Q1 今日前端真实body {id,intent} → 不透传", f"{argv} / {env}")

argv, env = server.lapian_channel_passthrough({"model": "mm-1"})
check(argv == ["--model", "mm-1"] and env == {}, "Q1 顶层model 简写 → --model", f"{argv}")

argv, env = server.lapian_channel_passthrough(
    {"channel": {"baseUrl": "https://api.jxinapi.com", "key": "sk-secret", "model": "mm-2"}})
check(argv == ["--model", "mm-2", "--base-url", "https://api.jxinapi.com/v1beta"],
      "Q1 channel 全量 → argv（裸域名补 /v1beta）", f"{argv}")
check(env == {"JXIN_API_KEY": "sk-secret"}, "Q1 key 只走环境变量", f"{env}")
check(all("sk-secret" not in part for part in argv), "Q1 key 不出现在任何 argv 片段")

argv, env = server.lapian_channel_passthrough(
    {"model": "top", "channel": {"model": "chan"}})
check(argv == ["--model", "chan"], "Q1 channel.model 优先于顶层 model", f"{argv}")

argv, env = server.lapian_channel_passthrough({"channel": None, "model": "  "})
check(argv == [] and env == {}, "Q1 channel=None / 空白model → 不透传", f"{argv} / {env}")

argv, _ = server.lapian_channel_passthrough(
    {"channel": {"baseUrl": "https://api.jxinapi.com"}})
check(argv == ["--base-url", "https://api.jxinapi.com/v1beta"],
      "Q1 只有baseUrl（渠道未选模型）→ 只透传 --base-url", f"{argv}")

# --------------------------------------------------------------- Q2 baseUrl 归一两种形态

check(server.normalize_gemini_base("https://api.jxinapi.com") == "https://api.jxinapi.com/v1beta",
      "Q2 裸域名 → 补 /v1beta")
check(server.normalize_gemini_base("https://api.jxinapi.com/") == "https://api.jxinapi.com/v1beta",
      "Q2 尾斜杠归一后再补")
check(server.normalize_gemini_base("https://api.jxinapi.com/v1beta") == "https://api.jxinapi.com/v1beta",
      "Q2 显式 /v1beta → 尊重原值")
check(server.normalize_gemini_base("https://api.jxinapi.com/v1") == "https://api.jxinapi.com/v1",
      "Q2 显式 /v1 → 尊重原值（不二次追加）")
check(server.normalize_gemini_base("") == "", "Q2 空值 → 空串（不透传）")
check(server.normalize_gemini_base("http://127.0.0.1:8000") == "http://127.0.0.1:8000/v1beta",
      "Q2 带端口的本地网关 → 同样补默认版本")

# --------------------------------------------------------------- Q3 不传时与旧版逐字节一致

legacy = ["analyze.py", "uploads/v.mp4", "--id", "t1", "--yes", "--intent", "想卖货"]
check(server.analyze_argv("uploads/v.mp4", "t1", "想卖货") == legacy,
      "Q3 不透传 → analyze.py 命令行与旧版逐字节一致")
check(server.analyze_argv("uploads/v.mp4", "t1", "想卖货", ["--model", "mm"]) == legacy + ["--model", "mm"],
      "Q3 透传 → 旧 argv 原样 + 附加参数（不改既有部分）")

# --------------------------------------------------------------- Q4 extract 端点解析

check(extract.resolve_endpoint(None, None) == extract.ENDPOINT,
      "Q4 默认（不透传）→ 既有 ENDPOINT 逐字节一致")
check(extract.resolve_endpoint(extract.MODEL_LABEL, None) == extract.ENDPOINT,
      "Q4 只给默认模型名 → 仍落在既有 ENDPOINT")
check(extract.resolve_endpoint("mm-9", "https://api.jxinapi.com") ==
      "https://api.jxinapi.com/v1beta/models/mm-9:generateContent",
      "Q4 裸域名 + 模型 → 补 /v1beta 拼端点")
check(extract.resolve_endpoint("mm-9", "https://api.jxinapi.com/v1beta") ==
      "https://api.jxinapi.com/v1beta/models/mm-9:generateContent",
      "Q4 显式 /v1beta → 同一端点（bridge 已归一也不双补）")
check(extract.resolve_endpoint("mm-9", "https://api.jxinapi.com/v1") ==
      "https://api.jxinapi.com/v1/models/mm-9:generateContent",
      "Q4 显式 /v1 → 尊重原值")
check(extract.resolve_endpoint(None, "https://gw.example") ==
      "https://gw.example/v1beta/models/" + extract.MODEL_LABEL + ":generateContent",
      "Q4 只有baseUrl → 模型名落回既有默认（不新增硬编码）")

# --------------------------------------------------------------- Q5 call_vlm --api-key 优先级

captured: dict = {}
real_post, real_load = call_vlm._post_json, call_vlm._load_api_key
tmp = Path(tempfile.mkdtemp(prefix="t7c-"))
frame = tmp / "f_0s.jpg"
frame.write_bytes(b"\xff\xd8fake-jpeg")
prompt = tmp / "p.md"
prompt.write_text("PROMPT", encoding="utf-8")
try:
    def fake_post(url, payload, headers, timeout):
        captured.update(url=url, payload=payload, headers=headers)
        return json.dumps({"choices": [{"message": {"content": json.dumps({"ok": True})}}]})

    call_vlm._post_json = fake_post
    call_vlm._load_api_key = lambda: "env-fallback-key"

    out1 = tmp / "raw1.json"
    code = call_vlm.main(["--frames", str(frame), "--prompt", str(prompt), "--out", str(out1),
                          "--model", "m-probe", "--base-url", "https://gw.example/v9",
                          "--api-key", "sk-probe"])
    check(code == 0 and out1.is_file(), "Q5 显式 --api-key → 调用成功出产物", f"exit={code}")
    check(captured.get("url") == "https://gw.example/v9/chat/completions",
          "Q5 --base-url 忠实直拼 /chat/completions（不自动改写）", captured.get("url", ""))
    check(captured.get("payload", {}).get("model") == "m-probe", "Q5 --model 进请求 payload")
    check(captured.get("headers", {}).get("Authorization") == "Bearer sk-probe",
          "Q5 显式 key 优先于环境/.env")

    out2 = tmp / "raw2.json"
    call_vlm.main(["--frames", str(frame), "--prompt", str(prompt), "--out", str(out2)])
    check(captured.get("headers", {}).get("Authorization") == "Bearer env-fallback-key",
          "Q5 不传 --api-key → 落回环境/.env 读法（行为不变）")

    call_vlm._load_api_key = lambda: ""
    code = call_vlm.main(["--frames", str(frame), "--prompt", str(prompt), "--out", str(tmp / "raw3.json")])
    check(code == 2, "Q5 无处取 key → 退出码 2（不静默）", f"exit={code}")
finally:
    call_vlm._post_json, call_vlm._load_api_key = real_post, real_load

# --------------------------------------------------------------- Q6 指纹「拆解模型」兼容

video = tmp / "v.mp4"
video.write_bytes(b"fake-video")
outdir = tmp / "task"
outdir.mkdir()
default_fp = root_analyze.fingerprint_for(video, outdir)
check(default_fp.get("拆解模型") == "", "Q6 指纹默认记空拆解模型（与老指纹同口径）")
override_fp = root_analyze.fingerprint_for(video, outdir, "mm@https://api.jxinapi.com")
check(override_fp.get("拆解模型") == "mm@https://api.jxinapi.com", "Q6 透传时记下模型身份")

old_fp = {"大小": 1, "mtime_ns": 1}          # 老指纹：没有「拆解模型」字段
check(root_analyze.compare_model(old_fp, default_fp) == (True, ""),
      "Q6 老指纹 × 不透传 → 一致（零回归，不触发重拆）")
check(root_analyze.compare_model(old_fp, override_fp)[0] is False,
      "Q6 老指纹 × 指定模型 → 判变（按选定模型重拆）")
check(root_analyze.compare_model({"拆解模型": "a@b"}, default_fp)[0] is False,
      "Q6 指定模型 × 退回默认 → 判变")
check(root_analyze.compare_model({"拆解模型": "a@b"},
                                 root_analyze.fingerprint_for(video, outdir, "a@b")) == (True, ""),
      "Q6 同模型同渠道 → 一致（不反复重拆）")

# --------------------------------------------------------------- Q7 四个 CLI 的 argparse 接线

for script in (ROOT / "analyze.py", REVERSE / "extract.py",
               REVERSE / "analyze.py", REVERSE / "call_vlm.py"):
    proc = subprocess.run([sys.executable, str(script), "--help"], cwd=str(ROOT),
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=120)
    ok = proc.returncode == 0 and all(flag in proc.stdout
                                      for flag in ("--model", "--base-url", "--api-key"))
    check(ok, f"Q7 {script.name}（{'根' if script.parent == ROOT else 'reverse/'}）三个旗标都接上",
          f"exit={proc.returncode}")

# --------------------------------------------------------------- Q8 reverse analyze 的 call_vlm 附加参数

check(spec_analyze.call_vlm_extra_args() == [], "Q8 不透传 → call_vlm 无附加参数（命令行与旧版一致）")
check(spec_analyze.call_vlm_extra_args("m", "https://gw.example/v1", "k") ==
      ["--model", "m", "--base-url", "https://gw.example/v1", "--api-key", "k"],
      "Q8 三项透传 → 忠实转发（归一在 bridge 侧）")

# --------------------------------------------------------------- 收尾

failed = results.count(False)
print(f"\n共 {len(results)} 项，失败 {failed} 项")
sys.exit(1 if failed else 0)
