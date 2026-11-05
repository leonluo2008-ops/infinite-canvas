#!/usr/bin/env python3
"""提交契约（wan_submit）的行为检查：全离线，不碰网络、不花钱。

覆盖三件在真机上踩过的事：
1. `@图片N` 解析不到时 CLI 不报错 → 必须在提交前拦住（前缀补齐 / 死引用检测）。
2. 「只发被引用的图」会让标题整体前移 → 必须按 1..max(N) 补齐。
3. 参数越界、图片缺失、音轨与正文不一致 → 必须在本地就判失败。
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PASS = FAIL = 0


def check(ok: bool, label: str, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"[PASS] {label}")
    else:
        FAIL += 1
        print(f"[FAIL] {label}" + (f" —— {detail}" if detail else ""))


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod          # dataclass 需要按 __module__ 找回模块
    spec.loader.exec_module(mod)
    return mod


def fake_echo(mod, req) -> dict:
    """按实测形状造一份回显：prompt 原文 + originPrompt(uuid) + elements + reference。"""
    elements = {f"e{n}": {"type": "image",
                          "payload": {"title": f"图片{n}", "name": p.name}}
                for n, p in enumerate(req.images, start=1)}
    origin = req.prompt
    for n in range(1, len(req.images) + 1):
        origin = origin.replace(f"@图片{n}", "@{e%d}" % n)
    return {"prompt": req.prompt, "promptMeta": {"originPrompt": origin},
            "duration": req.duration, "ratio": req.ratio,
            "selectedResolution": req.resolution, "audio": req.audio,
            "elements": elements,
            "reference": {"refElementIds": [f"e{n}" for n in range(1, len(req.images) + 1)]}}


def main() -> int:
    mod = load("wan_submit", ROOT / "pipeline" / "gen" / "wan_submit.py")
    tmp = Path(tempfile.mkdtemp(prefix="wan-submit-"))
    a, b, c = tmp / "a.png", tmp / "b.png", tmp / "c.png"
    for p in (a, b, c):
        p.write_bytes(b"x")

    # ① 引用抽取
    check(mod.wanted_refs("x @图片2 y @图片1 z @图片2") == [1, 2], "① wanted_refs 去重升序")

    # ② 前缀补齐：只引用 @图片2 → 发前两张（不是只发第二张）
    files, note = mod.expand_prefix("看 @图片2", [a, b, c])
    check(files == [a, b], "② 只引用 @图片2 → 发 1..2 两张", f"{[p.name for p in files]} / {note}")

    # ③ 越界：@图片3 但只有 2 张 → 拒绝
    err = ""
    try:
        mod.expand_prefix("看 @图片3", [a, b])
    except mod.SubmitError as exc:
        err = str(exc)
    check("@图片3" in err and "只有 2 张" in err, "③ 引用超出可用图数 → 拒绝并说清", err)

    # ④ 无引用 → 纯文生视频
    files, _ = mod.expand_prefix("没有图号", [a])
    check(files == [], "④ 正文没有 @图片N → 不发图")

    # ⑤ 参数校验
    bad = mod.validate(mod.Request(prompt="x", duration=31, resolution="2K", ratio="5:4",
                                   images=(tmp / "nope.png",)))
    joined = "；".join(bad)
    check(all(k in joined for k in ("时长", "分辨率", "画幅", "不存在")),
          "⑤ 时长/分辨率/画幅/缺图 一次报全", joined[:160])

    # ⑥ 正常回显 → 无差异
    req = mod.Request(prompt="首帧 @图片2 手持 @图片1", images=(a, b),
                      duration=7, ratio="9:16", resolution="480P", audio=True)
    check(mod.check_echo(req, fake_echo(mod, req)) == [], "⑥ 正常回显 → 0 差异")

    # ⑦ 死引用：正文 @图片2 但只有 1 张（被命名成 图片1）
    req1 = mod.Request(prompt="看 @图片2", images=(a,), duration=5)
    ti1 = fake_echo(mod, req1)
    bad = "；".join(mod.check_echo(req1, ti1))
    check("没有对应图的图号" in bad, "⑦ 死引用被拦住（CLI 自己不报错）", bad[:160])

    # ⑧ 音轨不一致
    req2 = mod.Request(prompt="x", images=(), duration=5, audio=True)
    ti2 = fake_echo(mod, req2)
    ti2["audio"] = False
    check("音轨被改" in "；".join(mod.check_echo(req2, ti2)), "⑧ 音轨被改 → 报差异")

    # ⑨ 令牌随音轨变化
    t_on = mod.Request(prompt="x", images=(a,), duration=5, audio=True).token()
    t_off = mod.Request(prompt="x", images=(a,), duration=5, audio=False).token()
    check(t_on != t_off and len(t_on) == 16, "⑨ 令牌随音轨变化，16 位")

    # ⑩ 命令构造：--dry-run 在 --prompt 之前；图片用逗号串
    args = mod.dry_run_args(req)
    check(args.index("--dry-run") < args.index("--prompt"), "⑩ --dry-run 排在 --prompt 之前")
    check(args[args.index("--images") + 1] == f"{a},{b}", "⑪ 图片按顺序逗号拼接",
          args[args.index("--images") + 1])
    check("--audio-output=true" in args, "⑫ 音轨开关写进命令")

    # ⑬ 活路径里只有 wan_submit 允许出现命令字面量
    offenders = []
    for sub in ("pipeline", "ui", "scripts"):
        base = ROOT / sub
        if not base.exists():
            continue
        for p in base.rglob("*.py"):
            if p.name == "wan_submit.py":
                continue
            if re.search(r"[\"']omni2video[\"']", p.read_text(encoding="utf-8", errors="replace")):
                offenders.append(str(p.relative_to(ROOT)))
    check(not offenders, "⑬ 活路径里只有 wan_submit 拼 omni2video", "、".join(offenders))

    # ⑭ 未登录 → submit 直接拒绝，不调提交命令
    calls = []

    def runner(argv, timeout=None):
        calls.append(list(argv))
        return 0, '{"ok": true, "authenticated": false}', ""

    err = ""
    try:
        mod.submit(mod.Request(prompt="x", images=(), duration=5), run=runner)
    except mod.SubmitError as exc:
        err = str(exc)
    check("未登录" in err and len(calls) == 1, "⑭ 未登录时 submit 直接失败，不提交", err or f"calls={calls}")

    # ⑮ 写错形式的引用：CLI 静默不认，本地必须拦
    bad = "；".join(mod.validate(mod.Request(prompt="@图片 1 站在海边", images=(a,))))
    check("空格" in bad, "⑮ @图片 1（带空格）被拦住", bad[:120])
    bad = "；".join(mod.validate(mod.Request(prompt="图片1 站在海边", images=(a,))))
    check("少了 @" in bad, "⑮b 漏写 @ 被拦住", bad[:120])

    # ⑯ 0 字节图
    empty = tmp / "empty.png"
    empty.write_bytes(b"")
    bad = "；".join(mod.validate(mod.Request(prompt="x", images=(empty,), duration=5)))
    check("0 字节" in bad, "⑯ 0 字节图被拦住", bad[:120])

    # ⑰ 同名换图 → 令牌必须变（只算文件名挡不住）
    swap = tmp / "swap.png"
    swap.write_bytes(b"AA")
    t1 = mod.Request(prompt="x", images=(swap,), duration=5).token()
    swap.write_bytes(b"AB")
    t2 = mod.Request(prompt="x", images=(swap,), duration=5).token()
    check(t1 != t2, "⑰ 同名不同内容 → 令牌不同", f"{t1} vs {t2}")

    # ⑱ 音轨写成字符串 "false" 时不能当成 True
    req3 = mod.Request(prompt="x", images=(), duration=5, audio=True)
    ti3 = fake_echo(mod, req3)
    ti3["audio"] = "false"
    check("音轨被改" in "；".join(mod.check_echo(req3, ti3)), "⑱ audio:\"false\" 被识破")

    # ⑲ 模型被降级
    ti4 = fake_echo(mod, req3)
    ti4["modelVersion"] = "2_7"
    check("模型被换成" in "；".join(mod.check_echo(req3, ti4)), "⑲ modelVersion 被改 → 报差异")

    # ⑳ wait：没有新增 mp4 就是失败，绝不拿旧片顶
    outdir = tmp / "out"
    outdir.mkdir()
    (outdir / "old.mp4").write_bytes(b"old")

    def no_new(argv, timeout=None):
        return 0, "{}", ""

    err = ""
    try:
        mod.wait("t1", outdir, run=no_new)
    except mod.SubmitError as exc:
        err = str(exc)
    check("没有新增成片" in err, "⑳ 没有新片 → 判失败（不拿旧片顶）", err[:120])

    def with_new(argv, timeout=None):
        (outdir / "new.mp4").write_bytes(b"new")
        return 0, "{}", ""

    check(mod.wait("t2", outdir, run=with_new).name == "new.mp4", "㉑ 有新片 → 返回新片")

    print(f"\n  SUMMARY {PASS}/{PASS + FAIL} PASS / {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
