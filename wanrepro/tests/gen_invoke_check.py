#!/usr/bin/env python3
"""生成链路的调用检查：正文和参数到底发出去了没有。

踩过的坑（2026-11-01 实跑）：Python 把 1843 字的提示词当命令行参数传给 npm 生成的
`wan.cmd`，cmd.exe 在**第一个换行处**切断整条命令——后台只收到一行 37 字，时长/画幅/
分辨率/参考图全回落到默认值（5s / adaptive / 720P / 0 图），任务照样显示成功、15 灵感值
照样扣。所以这里测的不是「函数返回了什么」，而是「CLI 真正要发的那个请求」。

J1 走**生产路径**（cli_prefix() → run_cli() → plan()），只跑 `--dry-run`（免费）；
J2 用**内存替身**模拟被截断的 CLI 返回（反向验证；绝不真调 wan.cmd，那会真提交真扣钱）；
J3 证明 `plan()` 和服务端 `POST /api/generate/<id>` 都真的接上了核对。
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import io
import re
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REAL_ANALYSIS = ROOT / "analysis" / "reverse"
PASS = FAIL = 0


def check(ok: bool, name: str, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"[PASS] {name}")
    else:
        FAIL += 1
        print(f"[FAIL] {name}｜{detail}")


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def long_prompt() -> str:
    """40 行、1800+ 字的正文：刚好是「一行标题 + 大量换行」。"""
    head = "# 提示词 · long-check · timeline-narrative"
    body = [f"00:{i:02d}，中景，人物手持 @图片{(i % 5) + 1} 从画面左侧入画，光从右上斜切下来，"
            f"背景是一整面高亮白墙，衣物纹理在第 {i} 帧仍然清晰可辨，脚步声落在第 {i} 拍。"
            for i in range(1, 39)]
    return "\n".join([head, ""] + body)


def make_task(base: Path, task_id: str, prompt: str, files: list[str]) -> Path:
    d = base / task_id
    (d / "assets").mkdir(parents=True, exist_ok=True)
    (d / "提示词-timeline-narrative.md").write_text(prompt, encoding="utf-8")
    for name in files:
        (d / "assets" / name).write_bytes(b"\x89PNG\r\n\x1a\n")
    return d


class CliDouble:
    """内存替身：假装是 `wan` 的返回，**不启动任何进程**。

    反向验证只能这么测：真调 `wan.cmd` 就等于把「被截断的预演」再演一遍，
    `--dry-run` 会被同一次截断吞掉，于是真提交、真扣灵感值（方案 §2.1 踩过）。
    """

    def __init__(self, *, truncate: bool = True, grow_task: bool = False,
                 images: list[Path] | None = None) -> None:
        self.truncate = truncate
        self.grow_task = grow_task
        self.images = list(images or [])
        self.lists = 0
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], timeout: int | None = None):
        self.calls.append(list(args))
        if args[:2] == ["task", "list"]:
            self.lists += 1
            rows = [{"taskId": "0" * 32}]
            if self.grow_task and self.lists >= 2:        # 预演之后多出一条 ⇒ 说明这次「预演」真提交了
                rows.append({"taskId": "f" * 32})
            return 0, json.dumps(rows, ensure_ascii=False), ""

        def arg(flag: str, fallback: str) -> str:
            return args[args.index(flag) + 1] if flag in args else fallback

        prompt = arg("--prompt", "")
        if self.truncate:                                  # cmd.exe 在第一个换行处切断 + 后续参数全丢
            body = {"prompt": prompt.splitlines()[0] if prompt else "", "duration": 5,
                    "ratio": "adaptive", "selectedResolution": "720P", "elements": {}}
        else:
            elements = {f"e{n}": {"type": "image",
                                  "payload": {"title": f"图片{n}", "name": p.name}}
                        for n, p in enumerate(self.images, start=1)}
            # 真的 CLI 会把 @图片N 换成 @{uuid} 放进 promptMeta.originPrompt（2026-11-02 实测）
            origin = prompt
            for n in range(1, len(self.images) + 1):
                origin = origin.replace(f"@图片{n}", "@{e%d}" % n)
            ordered = [f"e{m.group(1)}" for m in re.finditer(r"@\{e(\d+)\}", origin)]
            body = {"prompt": prompt, "duration": int(arg("--duration", "5")),
                    "ratio": arg("--ratio", "adaptive"),
                    "selectedResolution": arg("--resolution", "720P"),
                    "audio": True, "elements": elements,
                    "promptMeta": {"originPrompt": origin, "orderedKeys": ordered},
                    "reference": {"type": "ref-element",
                                  "refElementIds": [f"e{n}" for n in range(1, len(self.images) + 1)]}}
        payload = {"request": {"taskType": "omni_video_generate", "deductMode": "credit_mode",
                               "taskInput": body}}
        return 0, json.dumps(payload, ensure_ascii=False), ""


def real_plan_is_good(pf: dict, prompt: str, want: list[str], *, duration: int, ratio: str,
                      resolution: str) -> tuple[bool, str]:
    """把「预演拿回来的请求」逐项对一遍；返回 (是否全对, 说明)。"""
    snap = pf.get("requestSnapshot") or {}
    actual = pf.get("actual") or {}
    bad = []
    if not pf.get("ok"):
        bad.append(f"ok={pf.get('ok')} mismatches={pf.get('mismatches')}")
    if snap.get("promptSha256") != hashlib.sha256(prompt.encode("utf-8")).hexdigest():
        bad.append("request.taskInput.prompt 与原文不是逐字相等（sha256 不同）")
    if actual.get("chars") != len(prompt) or snap.get("promptChars") != len(prompt):
        bad.append(f"字数 {actual.get('chars')} / {snap.get('promptChars')} ≠ {len(prompt)}")
    if (snap.get("duration"), snap.get("ratio"), snap.get("resolution")) != (duration, ratio, resolution):
        bad.append(f"参数 {snap.get('duration')}/{snap.get('ratio')}/{snap.get('resolution')}"
                   f" ≠ {duration}/{ratio}/{resolution}")
    got = [(e.get("title"), e.get("name")) for e in snap.get("elements") or []]
    want_pairs = [(f"图片{i}", name) for i, name in enumerate(want, start=1)]
    if got != want_pairs:
        bad.append(f"elements {got} ≠ {want_pairs}")
    return not bad, "；".join(bad)


# ------------------------------------------------------------------ J1


def j1_production_path(mod, tmp: Path) -> None:
    """40 行 1800+ 字 → 走 cli_prefix()/run_cli()/plan() 真跑一次预演（免费）。"""
    prompt = long_prompt()
    files = [f"图片{n}.png" for n in (2, 3, 4, 5, 6)]
    make_task(tmp, "long-check", prompt, files)
    mod.ANALYSIS = tmp

    prefix = mod.cli_prefix()
    check(
        len(prefix) == 2 and Path(prefix[0]).name.lower().startswith("node")
        and prefix[1].lower().endswith(".js") and ".cmd" not in " ".join(prefix).lower(),
        "① cli_prefix() 直连 node 入口，前缀里没有 wan.cmd",
        f"得到 {prefix}",
    )

    plan = mod.plan("long-check", "timeline-narrative", 15, "9:16", "480P")
    pf = plan.get("preflight") or {}
    ok, why = real_plan_is_good(pf, prompt, files, duration=15, ratio="9:16", resolution="480P")
    check(pf.get("ok") is True, "② 40 行 1800+ 字预演通过（正文/参数/图全对）", why)
    check(ok, "③ request.taskInput.prompt 与原文逐字相等 + 参数一致 + elements=图数",
          why)
    check(len(prompt.splitlines()) >= 40 and len(prompt) >= 1800,
          "④ 测试正文确实是 40 行 1800+ 字", f"{len(prompt.splitlines())} 行 / {len(prompt)} 字")
    check((pf.get("actual") or {}).get("chars") == len(prompt) and len(prompt) >= 1800,
          "⑤ actual.chars 等于正文字数", f"{pf.get('actual')} vs {len(prompt)}")


# ------------------------------------------------------------------ J2


def j2_truncated(mod) -> None:
    """内存替身：CLI 返回「被截断的请求」→ 必须判失败，且说清是正文被改。"""
    prompt = long_prompt()
    images = [Path(f"图片{n}.png") for n in (2, 3, 4, 5, 6)]
    real_run = mod.run_cli
    try:
        double = CliDouble(truncate=True, images=images)
        mod.run_cli = double
        pf = mod.preflight(prompt, 15, "9:16", "480P", images)
        bad = "；".join(pf.get("mismatches") or [])
        check(pf.get("ok") is False, "⑥ 被截断的返回 → preflight 判失败", f"得到 ok={pf.get('ok')}")
        check("正文被改" in bad, "⑦ 差异里点名「正文被改」", bad[:160])
        check(all(k in bad for k in ("时长被改", "画幅被改", "分辨率被改", "参考图被丢")),
              "⑧ 时长/画幅/分辨率/参考图四项也各自点名", bad[:200])
        check(len(double.calls) >= 3, "⑨ 替身确实被调到（1 次预演 + 前后两次 task list）",
              f"{len(double.calls)} 次")

        grow = CliDouble(truncate=False, grow_task=True, images=images)
        mod.run_cli = grow
        pf = mod.preflight(prompt, 15, "9:16", "480P", images)
        bad = "；".join(pf.get("mismatches") or [])
        check(pf.get("ok") is False and "预演意外提交" in bad,
              "⑩ 预演后任务列表出现新任务号 → 判「预演意外提交」并中止", bad[:200])

        # 令牌要跟参数绑定：改了分辨率，令牌必须变（服务端靠它拦「参数被改过」）
        t480 = mod.WS.Request(prompt=prompt, images=tuple(images), duration=15,
                              ratio="9:16", resolution="480P").token()
        t720 = mod.WS.Request(prompt=prompt, images=tuple(images), duration=15,
                              ratio="9:16", resolution="720P").token()
        check(t480 != t720 and len(t480) == 16 and all(c in "0123456789abcdef" for c in t480),
              "⑪ 令牌 = sha256 前 16 位十六进制，且随参数变化", f"{t480} / {t720}")
    finally:
        mod.run_cli = real_run


def j2b_no_fallback(mod) -> None:
    """找不到 node 入口必须抛错（退出码 3 的来源），**绝不回退 wan.cmd**。"""
    real_which = mod.WS.shutil.which
    try:
        mod.WS.shutil.which = lambda name: ("C:/fake/node.exe" if name == "node" else None)
        err = ""
        try:
            mod.cli_prefix()
        except mod.CliEntryError as exc:
            err = str(exc)
    finally:
        mod.WS.shutil.which = real_which
    check("node 入口" in err and "cmd" in err,
          "⑫ 定位不到入口 → 抛 CliEntryError（一句话原因，不回退 wan.cmd）", err or "没有报错")


def j2c_generate_refuses(mod, tmp: Path) -> None:
    """核对没过时 generate() 必须写 failed（不是 running）、退出码 3、一次都不提交。"""
    prompt = long_prompt()
    files = [f"图片{n}.png" for n in (2, 3, 4, 5, 6)]
    make_task(tmp, "gen-fail", prompt, files)
    mod.ANALYSIS = tmp
    real_run = mod.run_cli
    try:
        double = CliDouble(truncate=True, images=[Path(n) for n in files])
        mod.run_cli = double
        args = argparse.Namespace(id="gen-fail", template="timeline-narrative", duration=15,
                                  ratio="9:16", resolution="480P", confirm=True)
        with contextlib.redirect_stderr(io.StringIO()):
            code = mod.generate(args)
        rec = mod.read_record("gen-fail")
        submits = [c for c in double.calls if c[:1] == ["omni2video"] and "--dry-run" not in c]
        pf = rec.get("preflight") or {}
        check(code == 3 and rec.get("status") == "failed" and not submits,
              "⑬ 核对没过 → generate() 退出码 3，记录写 failed（绝不写 running），不提交",
              f"code={code} status={rec.get('status')} submits={len(submits)}")
        check(pf.get("ok") is False and "核对未通过" in str(rec.get("error")),
              "⑭ 失败记录里留着 error 与 preflight（事后能看清为什么没提交）",
              f"error={str(rec.get('error'))[:80]} preflight.ok={pf.get('ok')}")
    finally:
        mod.run_cli = real_run


# ------------------------------------------------------------------ J3


def j3_wiring(mod) -> None:
    """plan() 与 POST /api/generate 都真的带上了 preflight（证明接线，不是只测函数）。"""
    mod.ANALYSIS = REAL_ANALYSIS
    plan = mod.plan("pop-3-b3", "timeline-narrative", 15, "9:16", "480P")
    pf = plan.get("preflight") or {}
    check(isinstance(pf, dict) and "token" in pf, "⑮ plan() 的返回值里带 preflight 对象",
          f"keys={list(pf)}")
    # 字数不写死：核对的意思是「发出去的正文 == 盘上那份提示词」，盘上多少就是多少。
    # （写死 1843 在拆解改了首帧那一句之后会假红——契约没变，是数字过期了。）
    want_chars = len((REAL_ANALYSIS / "pop-3-b3" / "提示词-timeline-narrative.md")
                     .read_text(encoding="utf-8"))
    check(pf.get("ok") is True and (pf.get("actual") or {}).get("chars") == want_chars,
          f"⑯ pop-3-b3 / timeline-narrative：ok=true、actual.chars == 盘上提示词 {want_chars} 字",
          f"ok={pf.get('ok')} actual={pf.get('actual')} bad={pf.get('mismatches')}")
    want = [{"no": i, "file": f"图片{n}.png"} for i, n in enumerate((2, 3, 4, 5, 6), start=1)]
    check(pf.get("images") == want, "⑰ 图片顺序 = 图片1..5 → 图片2..6.png", f"{pf.get('images')}")

    server = load_module("ui_server", ROOT / "ui" / "server.py")

    class FakeHandler(server.Handler):
        """把 socket 那一层掐掉，只测 do_generate 自己的判断（不监听端口、不起服务）。"""

        def __init__(self, payload: dict) -> None:
            self._payload = dict(payload)
            self.path = "/api/generate/pop-3-b3"
            self.command = "POST"
            self.headers = {}
            self.sent: list[tuple[int, dict]] = []

        def read_json(self) -> dict:
            return dict(self._payload)

        def send_json(self, obj, status: int = 200) -> None:
            self.sent.append((status, obj))

    base = {"template": "timeline-narrative", "duration": 15, "ratio": "9:16", "resolution": "480P"}
    handler = FakeHandler(base)
    handler.do_generate("pop-3-b3")
    status, body = handler.sent[-1]
    pf = (body.get("plan") or {}).get("preflight") or {}
    check(status == 200 and pf.get("ok") is True and (pf.get("actual") or {}).get("chars") == want_chars,
          f"⑱ POST /api/generate/<id>（不带 confirm）返回 plan.preflight.ok=true（{want_chars} 字）",
          f"{status} {json.dumps(body, ensure_ascii=False)[:200]}")

    # —— 提交闸门：线程体换成替身，就算走错分支也绝不会真提交 ——
    started: list[list[str]] = []
    real_run_generate = server.Handler._run_generate
    real_run_py = server.run_py
    try:
        server.Handler._run_generate = lambda self, task_id, args: started.append(list(args))

        bad_token = FakeHandler({**base, "confirm": True, "preflightToken": "0000000000000000"})
        bad_token.do_generate("pop-3-b3")
        status, body = bad_token.sent[-1]
        check(status == 409 and not started and "preflight" in body,
              "⑲ 令牌对不上 → 409，且**没有**起生成线程", f"{status} started={len(started)}")

        no_token = FakeHandler({**base, "confirm": True})
        no_token.do_generate("pop-3-b3")
        check(no_token.sent[-1][0] == 409 and not started,
              "⑳ 不带令牌 → 409，不提交", f"{no_token.sent[-1][0]}")

        # 核对没过（服务端自己重跑的结果）→ 409，即使令牌「对」
        failed_plan = {"ok": True, "template": "timeline-narrative", "busy": False,
                       "preflight": {"ok": False, "mismatches": ["正文被改：发出 37 字，应为 1843 字"],
                                     "actual": {"chars": 37}, "images": [], "token": "abcdefabcdefabcd"}}
        server.run_py = lambda args, timeout: (0, json.dumps(failed_plan, ensure_ascii=False), "")
        failed = FakeHandler({**base, "confirm": True, "preflightToken": "abcdefabcdefabcd"})
        failed.do_generate("pop-3-b3")
        status, body = failed.sent[-1]
        check(status == 409 and not started and (body.get("preflight") or {}).get("ok") is False,
              "㉑ 服务端复核不通过 → 409 并回差异，不提交",
              f"{status} {json.dumps(body, ensure_ascii=False)[:160]}")

        # 正路：核对通过 + 令牌一致 → 起生成线程（线程体是替身，不真跑）
        good_plan = {"ok": True, "template": "timeline-narrative", "busy": False,
                     "preflight": {"ok": True, "mismatches": [], "token": "abcdefabcdefabcd",
                                   "actual": {"chars": 1843, "images": 5, "duration": 15,
                                              "ratio": "9:16", "resolution": "480P"},
                                   "images": [{"no": 1, "file": "图片2.png"}]}}
        server.run_py = lambda args, timeout: (0, json.dumps(good_plan, ensure_ascii=False), "")
        good = FakeHandler({**base, "confirm": True, "preflightToken": "abcdefabcdefabcd"})
        good.do_generate("pop-3-b3")
        status, body = good.sent[-1]
        check(status == 200 and body.get("started") is True and len(started) == 1
              and "--confirm" in started[0],
              "㉒ 核对通过 + 令牌一致 → 提交（参数带 --confirm）",
              f"{status} {body} started={started}")
    finally:
        server.Handler._run_generate = real_run_generate
        server.run_py = real_run_py


def main() -> int:
    mod = load_module("wan_video", ROOT / "pipeline" / "gen" / "wan_video.py")
    tmp = Path(tempfile.mkdtemp(prefix="gen-invoke-"))
    try:
        j1_production_path(mod, tmp)
        j2_truncated(mod)
        j2b_no_fallback(mod)
        j2c_generate_refuses(mod, tmp)
        j3_wiring(mod)
    finally:
        mod.ANALYSIS = REAL_ANALYSIS

    print(f"  SUMMARY {PASS}/{PASS + FAIL} PASS / {FAIL} FAIL")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
