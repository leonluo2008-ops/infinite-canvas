#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ui/index.html 生成弹层判据（纯标准库，无 pytest 依赖，不联网、不调 CLI）。

跑法：`py -3 tests/ui_gen_modal_first_check.py`   退出码 0 = 全过，1 = 有 FAIL

为什么是静态判据：这条缺陷是**顺序**问题，不是返回值问题——`POST /api/generate/<id>`
（免费预演 + 两次 task list + 查余额）实测 4.6–5.0 秒，只要 `openGenerate()` 里
`await` 计划排在 `openModal()` 前面，用户点完按钮就有 5 秒看不到任何反馈。
用真浏览器点击验证的是「现在对不对」，这里的四条断言守的是「以后别改回去」。

① openModal( 出现在 openGenerate 里第一个 await api( 之前 —— 弹层先出，计划随后到
② 骨架里确认按钮是 disabled，文案「核对中…」
③ 下拉变化的那条路（schedule）先 showChecking() 再 setTimeout —— 重发期间必须重新禁用
④ recheck 用 seq 丢弃过期响应（参数改得快时旧请求会晚回来）
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "ui" / "index.html"

FAILS: list[str] = []
PASSES: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    (PASSES if ok else FAILS).append(f"{label}{('  ← ' + detail) if (detail and not ok) else ''}")


def open_generate_body(src: str) -> str:
    """切出 openGenerate() 的函数体（到下一个顶层 function 定义为止）。"""
    start = src.index("async function openGenerate()")
    rest = src[start:]
    m = re.search(r"\nfunction openModal\(", rest)
    return rest[: m.start()] if m else rest


def main() -> int:
    if not INDEX.is_file():
        print(f"FAIL  找不到 {INDEX}")
        return 1
    src = INDEX.read_text(encoding="utf-8")
    body = open_generate_body(src)

    # ① 弹层先出现：openModal( 在第一个 await api( 之前
    i_modal = body.find("openModal(")
    i_wait = body.find("await api(")
    check(i_modal >= 0, "① openGenerate 里调了 openModal")
    check(i_wait >= 0, "① openGenerate 里发起了计划请求")
    if i_modal >= 0 and i_wait >= 0:
        check(i_modal < i_wait, "① 先开弹层再等计划（否则点完按钮干等 ~5 秒）",
              f"openModal@{i_modal} 晚于首个 await api@{i_wait}")

    # ② 骨架里按钮就是禁用的「核对中…」
    m_btn = re.search(r'<button[^>]*id="genGo"[^>]*>', body)
    check(bool(m_btn), "② 骨架里有 id=genGo")
    if m_btn:
        check("disabled" in m_btn.group(0), "② 骨架按钮带 disabled", m_btn.group(0))
    check("核对中…" in body.split("id=\"genGo\"")[-1][:40], "② 骨架按钮文案是「核对中…」")

    # ③ 下拉变化：先 showChecking() 再起防抖计时
    m_sched = re.search(r"function schedule\(\)\s*\{(.*?)\n  \}", body, re.S)
    check(bool(m_sched), "③ 找到 schedule()")
    if m_sched:
        sch = m_sched.group(1)
        i_show = sch.find("showChecking()")
        i_timer = sch.find("setTimeout(")
        check(i_show >= 0, "③ schedule 里调了 showChecking()")
        check(i_show >= 0 and i_timer >= 0 and i_show < i_timer,
              "③ 重发前重新禁用按钮（旧令牌不许拿来提交新参数）", f"showChecking@{i_show} setTimeout@{i_timer}")

    # ④ 过期响应丢弃
    check("mine !== seq" in body, "④ recheck 用 seq 丢弃过期响应")

    # 另：核对通过才解禁（applyCheck 只在 pf.ok 分支里置 token）
    m_paint = re.search(r"function paintCheck\(pf\)\s*\{(.*?)\n  \}", body, re.S)
    check(bool(m_paint), "附加 找到 paintCheck()")
    if m_paint:
        pc = m_paint.group(1)
        i_ok = pc.find("pf && pf.ok")
        i_tok = pc.find("token = pf.token")
        check(i_ok >= 0 and i_tok > i_ok, "附加 令牌只在 preflight.ok 分支里拿到")
        check("setGo(\"核对没通过\", true)" in pc, "附加 核对没通过时按钮保持禁用")

    for line in PASSES:
        print("PASS  " + line)
    for line in FAILS:
        print("FAIL  " + line)
    print(f"\n{len(PASSES)} PASS / {len(FAILS)} FAIL")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
