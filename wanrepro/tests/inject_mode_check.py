#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""--inject 只影响「写进 prompt 的帧数」，不影响判定本身。

2026-11-02 实测：5 帧判定全塞进正文 → 上游 `PROHIBITED_CONTENT` 连续 8 次拦；
同一部片子不注入 → 一次通过。所以默认只把 0 秒那一帧写进 prompt，其余帧留在
本地做对账。这个测试盯的就是「默认真的只写 0 秒那一帧」。
"""
from __future__ import annotations

import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "pipeline" / "reverse"))

import extract as E  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"[PASS] {name}" + (f"｜{detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"[FAIL] {name}" + (f"｜{detail}" if detail else ""))


def fixture() -> dict:
    def frame(sec: float, desc: str) -> dict:
        return {
            "秒": sec,
            "判定": {
                "body_orientation": {"facing": "侧背对镜头", "facing_class": "侧背"},
                "gaze": "直视镜头",
                "people": 1,
                "shot_size": "中景",
                "描述": desc,
            },
        }

    return {"帧": [frame(0.0, "零秒：女子侧背站立，回首直视镜头。"),
                   frame(2.0, "二秒：她压低重心向前。"),
                   frame(5.0, "五秒：手机悬浮在车厢中央。")]}


def main() -> int:
    facts = fixture()

    only = E._pick_inject(facts, "first")
    check("first 只留 0 秒那一帧", [f["秒"] for f in (only or {}).get("帧", [])] == [0.0],
          str([f["秒"] for f in (only or {}).get("帧", [])]))

    block = E.build_frame_facts_block(only)
    check("first 的注入块里只有 0 秒那行", block.count("\n- ") == 1, f"{block.count(chr(10) + '- ')} 行")
    check("first 的注入块里没有 2 秒那句", "压低重心" not in block)

    allf = E._pick_inject(facts, "all")
    check("all 保留全部 3 帧", len((allf or {}).get("帧", [])) == 3)
    check("all 的注入块有 3 行", E.build_frame_facts_block(allf).count("\n- ") == 3)

    check("off 返回 None（一个字都不注入）", E._pick_inject(facts, "off") is None)
    check("认不出的模式按 first 处理", len((E._pick_inject(facts, "乱写") or {}).get("帧", [])) == 1)
    check("没有 看帧.json 时返回 None", E._pick_inject(None, "first") is None)
    check("只有非 0 秒的帧时返回 None", E._pick_inject({"帧": facts["帧"][1:]}, "first") is None)

    real = E.load_frame_facts(REPO_ROOT / "analysis" / "reverse" / "pop-3-b3")
    if real:
        real_block = E.build_frame_facts_block(E._pick_inject(real, "first"))
        check("真任务 first 注入块里只有 0.0 秒那行", real_block.count("\n- ") == 1)
        check("真任务 first 的块比 all 短", len(real_block)
              < len(E.build_frame_facts_block(E._pick_inject(real, "all"))))
    else:
        print("[SKIP] 真任务没有 看帧.json（不影响通过）")

    # 上游内容审核拦截要认得出来（认不出就会当普通失败退出，可选的事实块拖死主流程）
    check("认得出 prompt_blocked:PROHIBITED_CONTENT",
          E._is_content_block(E.ApiError(
              'HTTP 400 / {"message":"Blocked by upstream content policy '
              '(prompt_blocked:PROHIBITED_CONTENT)"}')))
    check("认得出 request_body_blocked",
          E._is_content_block(E.ApiError('HTTP 400 / "code":"request_body_blocked"')))
    check("普通超时不当成内容拦截",
          not E._is_content_block(E.ApiError("timed out after 300s")))
    check("普通 500 不当成内容拦截",
          not E._is_content_block(E.ApiError("HTTP 500 Internal Server Error")))

    print("-" * 78)
    print(f"合计 {PASS + FAIL} 项：{PASS} PASS / {FAIL} FAIL")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
