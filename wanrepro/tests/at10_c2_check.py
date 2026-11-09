"""AT-10 C2 单测：九宫格坐标矩阵 + frame_boxes 纯函数 + pop-4 帧内框复算 + 未定位夹具。

守住四件事（方案权威 docs/nodes/AT-10.md §三 数据合同 C0 定稿 + C2 行）：
1. grid_rect 九格全覆盖且越界降级：1..9 逐格期望值表（1=左上…9=右下，行主序，w=h=33.33）；
   0/10/"5"/None/True/5.0 一律 None（不猜、不默认格 5，bool 不算 int）。
2. frame_boxes 纯函数：同镜多行收进同一集合、格非法的行被丢弃、行号升序、本卡恒 False、
   短名/角标取值与卡片同源。
3. pop-4 真数据复算：镜1 集合={2,3,4,6}、镜3={1,5}、#7 他人框保留本卡全 False、逐卡本卡唯一、
   坐标矩阵、不变式「自身框 == 帧内框有本卡框」；旧字段（帧[]/定位/画面位置/代表帧 C1 断言）零变化。
4. 未定位不画框：自建夹具（无拆解.md、无源视频）→ 帧内框 == [] 且自身框 False（用完即删）。

跑法：python -B tests/at10_c2_check.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]          # wanrepro/
POP4 = "pop-4"
LOCAL = "auto-0012"

sys.path.insert(0, str(ROOT))
from automation import cards, frames, replace  # noqa: E402

results: list[tuple[bool, str]] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    results.append((bool(ok), name))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"｜{detail}" if detail else ""))


# ---------------------------------------------------------------- A grid_rect 坐标矩阵

EXPECT_RECT = {
    1: (0.0, 0.0), 2: (33.33, 0.0), 3: (66.67, 0.0),
    4: (0.0, 33.33), 5: (33.33, 33.33), 6: (66.67, 33.33),
    7: (0.0, 66.67), 8: (33.33, 66.67), 9: (66.67, 66.67),
}


def section_grid_rect() -> None:
    print("\n[A] grid_rect 九宫格坐标矩阵（1=左上…9=右下，行主序；内容盒 0~100%）")
    for cell, (x, y) in EXPECT_RECT.items():
        rect = cards.grid_rect(cell)
        want = (x, y, 33.33, 33.33)
        got = (rect["x%"], rect["y%"], rect["w%"], rect["h%"]) if rect else None
        check(got == want, f"格{cell} → x%={x} y%={y} w%=33.33 h%=33.33", f"{got}")
    check(cards.grid_rect(1) is not None and set(cards.grid_rect(1)) == {"x%", "y%", "w%", "h%"},
          "返回对象键 = {x%,y%,w%,h%}", str(sorted(cards.grid_rect(1))))
    for bad in (0, 10, "5", None, True, 5.0, -1, False):
        check(cards.grid_rect(bad) is None, f"格 {bad!r} → None（越界/类型降级，不猜不默认格5）",
              f"{cards.grid_rect(bad)}")


# ---------------------------------------------------------------- B frame_boxes 纯函数

ROWS = [
    {"no": 1, "cat": "人物", "desc": "女A持瓶", "advice": "必须换"},
    {"no": 2, "cat": "主体", "desc": "产品瓶身", "advice": "建议准备"},
    {"no": 3, "cat": "场景", "desc": "沙滩背景", "advice": "可沿用"},
    {"no": 4, "cat": "道具", "desc": "道具杯", "advice": ""},           # 建议列空 → 角标空串
]
SPANS = {
    1: {"画面位置": {"格": 2, "镜次": 1}},
    2: {"画面位置": {"格": 8, "镜次": 1}},
    3: {"画面位置": {"格": 0, "镜次": 1}},                             # 格非法 → 不出框
    4: {"画面位置": {"格": 3, "镜次": 2}},                             # 键镜不同 → 不进镜1集合
}
NAMES = {1: "波波头人物", 2: "本品主体"}                                # 3/4 缺短名 → 兜底


def section_frame_boxes() -> None:
    print("\n[B] frame_boxes 纯函数（同镜集合 / 格非法丢弃 / 升序 / 本卡恒 False / 短名角标）")
    boxes = cards.frame_boxes(ROWS, SPANS, NAMES, 1)
    check([box["行"] for box in boxes] == [1, 2], "镜1 集合 = 行1+行2（行3 格0 被丢弃、行4 键镜不同）",
          f"{[box['行'] for box in boxes]}")
    check(all(box["本卡"] is False for box in boxes), "纯函数里 本卡 恒 False",
          str([box["本卡"] for box in boxes]))
    check(boxes[0]["短名"] == "波波头人物" and boxes[1]["短名"] == "本品主体",
          "短名取 names 字典", f"{[box['短名'] for box in boxes]}")
    check(boxes[0]["角标"] == "必须换" and boxes[1]["角标"] == "建议准备",
          "角标 = 该行「建议」列原文", f"{[box['角标'] for box in boxes]}")
    check(boxes[1]["格"] == 8 and (boxes[1]["x%"], boxes[1]["y%"]) == (33.33, 66.67),
          "框坐标 = grid_rect 矩阵（格8 → 33.33/66.67）", str(boxes[1]))
    empty = cards.frame_boxes(ROWS, SPANS, NAMES, 2)
    check(len(empty) == 1 and empty[0]["行"] == 4 and empty[0]["短名"] == "道具"
          and empty[0]["角标"] == "",
          "镜2 集合：行4 进集合；names 缺 → 类别兜底；建议空 → 角标空串", str(empty))
    check(cards.frame_boxes([], SPANS, NAMES, 1) == [], "空行表 → 空集合")


# ---------------------------------------------------------------- C pop-4 真数据复算

SHOT_OF = {1: 3, 2: 1, 3: 1, 4: 1, 5: 3, 6: 1, 7: 3}   # 代表镜（C1 baseline v2）


def section_pop4() -> None:
    print(f"\n[C] pop-4({LOCAL}) 帧内框复算（只读真数据）")
    if not (ROOT / "analysis" / "reverse" / POP4 / "待确认清单.md").is_file():
        check(False, f"本机缺 analysis/reverse/{POP4}/待确认清单.md")
        return
    built = cards.build(POP4, LOCAL)
    by_no = {card["n"]: card for card in built}
    check(len(built) == 7, "清单 7 行", f"{len(built)} 行")

    rows_of = {no: {box["行"] for box in by_no[no]["帧内框"]} for no in by_no}
    check(all(rows_of[no] == {2, 3, 4, 6} for no in (2, 3, 4, 6)),
          "镜1 帧的卡（#2/#3/#4/#6）帧内框行集合 == {2,3,4,6}", f"{rows_of}")
    check(all(rows_of[no] == {1, 5} for no in (1, 5)),
          "镜3 帧的卡（#1/#5）帧内框行集合 == {1,5}", f"{rows_of}")
    check(rows_of[7] == {1, 5} and all(box["本卡"] is False for box in by_no[7]["帧内框"]),
          "#7 无位置键：他人框（镜3 的 {1,5}）保留、本卡全 False",
          f"{[(b['行'], b['本卡']) for b in by_no[7]['帧内框']]}")

    def own_rows(no: int) -> list[int]:
        return [box["行"] for box in by_no[no]["帧内框"] if box["本卡"]]

    unique_ok = all(own_rows(no) == ([no] if no != 7 else []) for no in by_no)
    check("逐卡本卡标记唯一且正确：#1-#6 各只有自己一行、#7 无本卡框",
          unique_ok, f"{[(no, own_rows(no)) for no in sorted(by_no)]}")

    matrix_ok = True
    for card in built:
        for box in card["帧内框"]:
            x, y = EXPECT_RECT[box["格"]]
            matrix_ok = matrix_ok and (box["x%"], box["y%"], box["w%"], box["h%"]) == (x, y, 33.33, 33.33)
            matrix_ok = matrix_ok and box["x%"] + box["w%"] <= 100.0 and box["y%"] + box["h%"] <= 100.0
    check("坐标矩阵逐框命中期望值表（x%+w%<=100、y%+h%<=100）", matrix_ok, "")

    invariant = all(card["自身框"] == any(box["本卡"] for box in card["帧内框"]) for card in built)
    own = {no: by_no[no]["自身框"] for no in sorted(by_no)}
    check("不变式：每卡 自身框 == (帧内框 存在 本卡=True)", invariant, f"{own}")
    check("自身框图 = #1-#6 True / #7 False",
          own == {1: True, 2: True, 3: True, 4: True, 5: True, 6: True, 7: False}, f"{own}")

    # 同帧多卡框集合一致（去掉本卡）
    def strip(boxes: list[dict]) -> list[dict]:
        return sorted(tuple(sorted((k, v) for k, v in box.items() if k != "本卡")) for box in boxes)

    base1 = strip(by_no[2]["帧内框"])
    base3 = strip(by_no[1]["帧内框"])
    check("镜1 四张卡去掉本卡后完全相等",
          all(strip(by_no[no]["帧内框"]) == base1 for no in (3, 4, 6)), "")
    check("镜3 两张卡（含 #7）去掉本卡后完全相等",
          strip(by_no[5]["帧内框"]) == base3 and strip(by_no[7]["帧内框"]) == base3, "")

    # 角标 = 清单「建议」列原文；短名非空
    md_path = replace.checklist_path(POP4)
    advice = {row["no"]: row.get("advice") or "" for row in replace.parse_rows(
        md_path.read_text(encoding="utf-8-sig", errors="replace"))}
    check("角标 == 该行建议列原文",
          all(box["角标"] == advice.get(box["行"])
              for card in built for box in card["帧内框"]), "")
    check("每框短名非空",
          all(box["短名"] for card in built for box in card["帧内框"]), "")

    # 旧字段零变化（回归红线：帧[]/定位/画面位置/代表帧 C1 断言）
    old_ok = all(isinstance(card.get("帧"), list) and card["帧"]
                 and card.get("定位") in ("精确", "未精确定位", "未定位")
                 for card in built)
    check("旧字段：每张卡 帧 非空 list、卡级定位 ∈ {精确,未精确定位,未定位}",
          old_ok, f"{[(c['n'], len(c['帧']), c['定位']) for c in built]}")
    check("旧字段：画面位置原样透传（#1/#5 格8镜3、#2/3/4/6 格5镜1、#7 None）",
          all((by_no[no]["画面位置"] or {}) == ({"格": 8, "镜次": 3} if no in (1, 5)
                                                else {"格": 5, "镜次": 1} if no != 7 else {})
              for no in by_no), f"{[(c['n'], c['画面位置']) for c in built]}")
    got_shot = {no: (by_no[no].get("代表帧") or {}).get("镜次") for no in by_no}
    check("旧字段（C1）：代表镜 = #1/#5→3、#2/3/4/6→1、#7→3", got_shot == SHOT_OF, f"{got_shot}")
    check("旧字段（C1）：代表帧字段齐全 {url,秒,镜次,定位}",
          all({"url", "秒", "镜次", "定位"} <= set(card["代表帧"]) for card in built),
          f"{sorted(built[0]['代表帧'])}")

    # 未定位不画框（自建夹具：无 拆解.md、无源视频；用完即删）
    fixture = ROOT / "analysis" / "reverse" / f"_at10c2-fixture-{os.getpid()}"
    fixture.mkdir(parents=True, exist_ok=True)
    try:
        lines = [
            "| # | 类别 | 模型看到什么 | 命名 | 资产图 | 锁什么 | 建议 | 用我的描述 |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
            "| 1 | 主体 | 产品瓶身特写 | 本品 |  | 轮廓 | 必须换 | 产品瓶身 |",
            "| 2 | 人物 | 女性持瓶 | 人物A |  | 手部 | 可沿用 | 女性持瓶 |",
        ]
        (fixture / "待确认清单.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        sidecar = {
            "schema": "at8-asset-spans-v1",
            "rows": [
                {"n": 1, "画面位置": {"格": 3, "镜次": 2}, "出现时段": [], "同实体疑似": [], "来源": "夹具"},
                {"n": 2, "画面位置": {"格": 5, "镜次": 2}, "出现时段": [], "同实体疑似": [], "来源": "夹具"},
            ],
        }
        (fixture / "待确认清单.json").write_text(json.dumps(sidecar, ensure_ascii=False), encoding="utf-8")
        built_fx = cards.build(fixture.name, "auto-9999")
        fx_ok = len(built_fx) == 2
        check("夹具两行 → 两卡", fx_ok, f"{len(built_fx)} 卡")
        if fx_ok:
            for card in built_fx:
                rep = card.get("代表帧") or {}
                no = card["n"]
                check(f"夹具 #{no}：定位=未定位、镜次=None、帧内框 == []、自身框 False",
                      rep.get("定位") == "未定位" and rep.get("镜次") is None
                      and card.get("帧内框") == [] and card.get("自身框") is False,
                      f"{rep}｜{card.get('帧内框')}｜{card.get('自身框')}")
    finally:
        shutil.rmtree(fixture, ignore_errors=True)     # 用完即删
    check("夹具目录已删", not fixture.exists(), str(fixture))

    # 抽帧失败路径（临时帧目录，用完即删）
    original_analysis = frames.ANALYSIS
    temp_analysis = Path(tempfile.mkdtemp(prefix="at10-c2-"))
    frames.ANALYSIS = temp_analysis
    try:
        missing = frames.build_shot_frame("空任务", "auto-9999", 2, 0.0, 1.0, [], None)
    finally:
        frames.ANALYSIS = original_analysis
        shutil.rmtree(temp_analysis, ignore_errors=True)
    check("抽帧失败 → 未定位、镜次 None（配合 boxes=[] 的合同闭环）",
          missing["定位"] == "未定位" and missing["镜次"] is None, str(missing))


def main() -> int:
    section_grid_rect()
    section_frame_boxes()
    section_pop4()
    fails = [name for ok, name in results if not ok]
    print(f"\n{len(results) - len(fails)}/{len(results)} PASS"
          + (f"；FAIL：{'、'.join(fails)}" if fails else ""))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
