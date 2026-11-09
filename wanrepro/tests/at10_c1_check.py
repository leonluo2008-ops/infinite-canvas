"""AT-10 C1 单测：时间码格式族 + 分镜表混合夹具 + pop-4 代表帧复算。

守住五件事（方案权威 docs/nodes/AT-10.md §三 v2，commit 8294dbc）：
1. 时间码认全格式族且保小数：`02.03` / `2.03s` / `2.03秒` / `00:02.03` / `0:02.03` /
   `00:00:02.03` 都是 2.03；`00:12`=12.0、`02:03`=123.0（单 token 层按分:秒，不改 ss:cs）；
   `format_clock(5)` 仍是 `00:05`（旧整秒口径不破）、`format_clock(2.03)`=`00:02.03`。
2. 读层断链修好：分镜行「行首无 `|`、列间半角竖线」（pop-4 原样）与全角 `｜` 都能读进镜表；
   `00:00-02.03` 这种「冒号起 + 纯秒小数止」混合格式不再整表读不到。
3. 歧义两段 `NN:NN` 按整张分镜表联合判断：片长 6.85s 的表按 ss:cs 读成 2.03/4.07/6.80，
   旧 mm:ss 表（片长 8s）仍读成 2/4/6——不是逐 token 一律 ss:cs。
4. pop-4（auto-0012）代表帧：位置键典型镜优先（#1/#5→镜3、#2/#3/#4/#6→镜1），
   #7 无位置键 → 意图建议镜3；全部「精确」、无一落 3.42s 中段兜底；同镜 URL 相等、
   实际帧秒落在该镜开区间（右端 0.05s 排除）；同镜只抽一次；旧 `帧[]`/`定位` 字段原样在。
5. 未定位不冒充：代表镜不可用时镜次 None、定位「未定位」（本节由夹具断言抽帧失败路径）。

跑法：python -B tests/at10_c1_check.py
"""
from __future__ import annotations

import importlib.util
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]          # wanrepro/
CONFIRM_PATH = ROOT / "pipeline" / "reverse" / "confirm.py"
POP4 = "pop-4"
LOCAL = "auto-0012"
TOL = 0.01

sys.path.insert(0, str(ROOT))
from automation import cards, frames  # noqa: E402

spec = importlib.util.spec_from_file_location("at10_c1_confirm", CONFIRM_PATH)
confirm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(confirm)

results: list[tuple[bool, str]] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    results.append((bool(ok), name))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"｜{detail}" if detail else ""))


def seconds_of(pair: tuple[str, str]) -> tuple[float, float]:
    start, end = confirm.clock_to_seconds(pair[0]), confirm.clock_to_seconds(pair[1])
    if start is None or end is None:
        raise AssertionError(f"读不出时段 {pair}")
    return start, end


def close(left: float, right: float, tol: float = TOL) -> bool:
    return abs(float(left) - float(right)) <= tol


def spans_of(times: dict, shot: int) -> tuple[float, float]:
    if str(shot) not in times:
        raise AssertionError(f"镜 {shot} 不在镜表里：{times}")
    return seconds_of(times[str(shot)])


# ---------------------------------------------------------------- A 格式族

def section_formats() -> None:
    print("\n[A] 时间码格式族（frames.clock_seconds 与 confirm.clock_to_seconds 同口径）")
    cases = {
        "02.03": 2.03,
        "2.03s": 2.03,
        "2.03秒": 2.03,
        "00:02.03": 2.03,
        "0:02.03": 2.03,
        "00:00:02.03": 2.03,
    }
    for text, want in cases.items():
        got_f, got_c = frames.clock_seconds(text), confirm.clock_to_seconds(text)
        check(got_f is not None and close(got_f, want) and got_c is not None and close(got_c, want),
              f"「{text}」→ {want}", f"frames={got_f} confirm={got_c}")
    for text, want in (("00:12", 12.0), ("02:03", 123.0), ("00:87", 0.87)):
        got_f, got_c = frames.clock_seconds(text), confirm.clock_to_seconds(text)
        check(got_f is not None and close(got_f, want) and got_c is not None and close(got_c, want),
              f"「{text}」→ {want}（分:秒口径 / 00:87 特例）", f"frames={got_f} confirm={got_c}")
    check(confirm.format_clock(2.03) == "00:02.03", "format_clock(2.03)=00:02.03", confirm.format_clock(2.03))
    check(confirm.format_clock(6.8) == "00:06.80", "format_clock(6.8)=00:06.80", confirm.format_clock(6.8))
    check(confirm.format_clock(5) == "00:05", "format_clock(5)=00:05（整秒不带小数，旧断言不破）",
          confirm.format_clock(5))
    check(confirm.format_clock(123) == "02:03", "format_clock(123)=02:03", confirm.format_clock(123))
    check(confirm.probe_duration("<!-- 视频: x.mp4 | 时长: 6.85s -->") == 6.85
          and confirm.probe_duration("> 规格：1080×1888 · 6.85 秒") == 6.85
          and confirm.probe_duration("没有片长") is None,
          "probe_duration 从头部取片长（两种写法），取不到 None")


# ---------------------------------------------------------------- B 读层夹具

HEADER = "镜号 | 起秒-止秒 | 运动主体 | 运动形态 | 速度 | 画面内容"
ROWS_MIXED = [
    ("1", "00:00-02.03"),
    ("2", "02.03-04.07"),
    ("3", "04.07-06.80"),
]
ROWS_AMBIGUOUS = [
    ("1", "00:00-02:03"),
    ("2", "02:03-04:07"),
    ("3", "04:07-06:80"),
]
ROWS_MMSS = [
    ("1", "00:00-00:02"),
    ("2", "00:02-00:04"),
    ("3", "00:04-00:06"),
    ("4", "00:06-00:08"),
]


def shot_table(rows, sep: str = " | ", leading: bool = False) -> str:
    lines = [HEADER.replace(" | ", sep)]
    for number, span in rows:
        line = f"{number}{sep}{span}{sep}主体{sep}动作{sep}匀速{sep}画面"
        lines.append(("| " + line + " |") if leading else line)
    return "\n".join(lines)


def expect_three(times: dict, label: str) -> None:
    want = [(0.0, 2.03), (2.03, 4.07), (4.07, 6.80)]
    got = []
    ok = True
    for index, (start, end) in enumerate(want, start=1):
        try:
            values = spans_of(times, index)
        except AssertionError:
            ok = False
            break
        got.append(values)
        ok = ok and close(values[0], start) and close(values[1], end)
    check(ok, f"{label} 镜表三段 = 0~2.03 / 2.03~4.07 / 4.07~6.80（±0.01）",
          f"实得 {[tuple(round(x, 2) for x in pair) for pair in got]}")


def section_fixtures() -> None:
    print("\n[B] 分镜表混合夹具（半角无行首竖线 / 全角 / 歧义两段 / 旧 mm:ss）")
    half = shot_table(ROWS_MIXED)
    header, rows = confirm.split_shots(half)
    check(len(rows) == 3, "半角竖线（行首无 |，pop-4 原样）split_shots 出 3 行", f"{len(rows)} 行")
    check(header.get("起秒-止秒") == 1, "表头认出「起秒-止秒」列", header)

    # 真实断链路径：先过 split_layers（_norm_pipes 会把半角转全角），再从层里读
    layers = confirm.split_layers("## 七 分镜表\n" + half + "\n")
    layer = layers.get("七 分镜表", "")
    check("｜" in layer and "|" not in layer,
          "split_layers 仍按既有语义把半角转全角（不动 _norm_pipes）", layer.splitlines()[0][:24])
    times = confirm.parse_shot_times(layer, 6.85)
    expect_three(times, "半角（经 split_layers）")

    times = confirm.parse_shot_times(shot_table(ROWS_MIXED, sep=" ｜ "), 6.85)
    expect_three(times, "全角 ｜")
    times = confirm.parse_shot_times(shot_table(ROWS_MIXED), None)
    expect_three(times, "半角（无片长）")

    times = confirm.parse_shot_times(shot_table(ROWS_AMBIGUOUS), 6.85)
    expect_three(times, "歧义两段 00:00-02:03…（duration=6.85 → 联合判为 ss:cs）")

    times = confirm.parse_shot_times(shot_table(ROWS_MMSS), 8.0)
    got = [seconds_of(times[str(i)]) for i in range(1, 5)]
    want = [(0.0, 2.0), (2.0, 4.0), (4.0, 6.0), (6.0, 8.0)]
    check(len(got) == 4 and all(close(a[0], b[0]) and close(a[1], b[1]) for a, b in zip(got, want)),
          "旧 mm:ss 表（duration=8.0）仍 0~2 / 2~4 / 4~6 / 6~8", got)

    # 歧义无法唯一判断 → 该镜丢弃（消费端未定位），不猜：
    # 歧义镜按 ss:cs 会与后一段明确小数镜矛盾（末段右端 5.20 距片长 6.85 差 1.65s），
    # 按 mm:ss 又越出片长——两种全局模式都不成立，只留不含歧义 token 的镜。
    broken = shot_table([("1", "00:00-02:03"), ("2", "00:03.10-00:05.20")])
    times = confirm.parse_shot_times(broken, 6.85)
    check("1" not in times and "2" in times,
          "两种全局模式都矛盾时只丢歧义镜（不猜→未定位）", times)

    # 散文里的数量区间不能当时间码
    check(confirm.spans_from_text("20-25 岁拉丁外观女性") == [],
          "spans_from_text 不吃「20-25 岁」散文区间")
    check(confirm.spans_from_text("包装字A（出现于：00:02-02.03）") == [{"起": "00:02", "止": "00:02.03"}],
          "spans_from_text 仍能读「00:02-02.03」混合格式",
          confirm.spans_from_text("包装字A（出现于：00:02-02.03）"))


# ---------------------------------------------------------------- C pop-4 复算

EXPECT_SHOT = {1: 3, 2: 1, 3: 1, 4: 1, 5: 3, 6: 1, 7: 3}


def section_pop4() -> None:
    print(f"\n[C] pop-4({LOCAL}) 代表帧复算（只读；缺帧时允许真实 ffmpeg 抽帧）")
    if not (ROOT / "analysis" / "reverse" / POP4 / "待确认清单.md").is_file():
        check(False, f"本机缺 analysis/reverse/{POP4}/待确认清单.md")
        return

    calls: list[tuple[float, str]] = []
    original_ensure = frames._ensure

    def spy_ensure(video, at, out):
        calls.append((float(at), Path(out).name))
        return original_ensure(video, at, out)

    frames._ensure = spy_ensure
    try:
        built = cards.build(POP4, LOCAL)
    finally:
        frames._ensure = original_ensure
    by_no = {card["n"]: card for card in built}
    check(len(built) == 7, "清单 7 行", f"{len(built)} 行")

    got_shot = {no: (card.get("代表帧") or {}).get("镜次") for no, card in by_no.items()}
    check(got_shot == EXPECT_SHOT, "代表镜 = 位置键典型镜优先（#1/#5→镜3、#2/3/4/6→镜1、#7→意图镜3）",
          f"{got_shot}")

    located = {no: (card.get("代表帧") or {}).get("定位") for no, card in by_no.items()}
    check(all(value == "精确" for value in located.values()), "全部代表帧 定位=精确", f"{located}")
    secs = {no: float((card.get("代表帧") or {}).get("秒")) for no, card in by_no.items()}
    check(all(not close(value, 3.42) for value in secs.values()),
          "无一行代表帧秒 = 3.42（旧的整片中段兜底）", f"{secs}")

    bounds = {1: (0.0, 2.03), 3: (4.07, 6.80)}
    shot_open = {shot: (start, end - 0.05) for shot, (start, end) in bounds.items()}
    out_of = [no for no, value in secs.items()
              if not (shot_open[EXPECT_SHOT[no]][0] <= value <= shot_open[EXPECT_SHOT[no]][1])]
    check(not out_of, "实际帧秒落在代表镜开区间（镜1 [0,1.98]、镜3 [4.07,6.75]）",
          f"越界行 {out_of}；秒 {secs}")

    urls = {no: (card.get("代表帧") or {}).get("url") for no, card in by_no.items()}
    group1 = [urls[no] for no in (2, 3, 4, 6)]
    group3 = [urls[no] for no in (1, 5, 7)]
    check(all(urls[no] for no in urls), "代表帧 url 都非空", f"{urls}")
    check(len(set(group1)) == 1 and len(set(group3)) == 1 and group1[0] != group3[0],
          "同镜共享同一帧：#2/#3/#4/#6 一组、#1/#5/#7 一组，两组不同",
          f"镜1={group1[0]} 镜3={group3[0]}")

    own = {no: card.get("自身框") for no, card in by_no.items()}
    check(own == {1: True, 2: True, 3: True, 4: True, 5: True, 6: True, 7: False},
          "自身框标志：位置键正指代表镜 #1-#6 True、#7（意图回退、无自身位置证据）False", f"{own}")

    shot_calls = [(at, name) for at, name in calls
                  if name.startswith(frames.SHOT_PREFIX) or name.startswith(frames.FALLBACK_PREFIX)]
    per_target: dict[str, int] = {}
    for _at, name in shot_calls:
        per_target[name] = per_target.get(name, 0) + 1
    check(len(shot_calls) <= 2, "同镜只抽一次：代表帧抽帧入口调用 ≤2 次",
          f"{len(shot_calls)} 次 {shot_calls}")
    check(all(count == 1 for count in per_target.values()),
          "每个代表帧目标文件只被抽一次（组装级共享，非 URL 去重）", f"{per_target}")
    check(len({name for _at, name in shot_calls}) == len(per_target),
          "同镜行没有重复抽帧（目标文件互不相同或只出现一次）")

    old_ok = all(isinstance(card.get("帧"), list) and card["帧"]
                 and card.get("定位") in ("精确", "未精确定位", "未定位") for card in built)
    check(old_ok, "旧字段仍在：每张卡 帧 非空 list、定位 ∈ {精确,未精确定位,未定位}",
          f"{[(card['n'], len(card['帧']), card['定位']) for card in built]}")
    check(all({"url", "秒", "镜次", "定位"} <= set(card["代表帧"]) for card in built),
          "代表帧 字段齐全（url/秒/镜次/定位）", f"{sorted(built[0]['代表帧'])}")

    # 未定位不冒充：把帧目录指到临时任务上（无源视频、无缓存）→ 抽不到就是未定位，镜次 None
    original_analysis = frames.ANALYSIS
    temp_analysis = Path(tempfile.mkdtemp(prefix="at10-c1-"))
    frames.ANALYSIS = temp_analysis
    try:
        fallback = frames.build_fallback_frame("空任务", "auto-9999", None)
        missing = frames.build_shot_frame("空任务", "auto-9999", 2, 0.0, 1.0, [], None)
    finally:
        frames.ANALYSIS = original_analysis
        shutil.rmtree(temp_analysis, ignore_errors=True)
    check(fallback["定位"] == "未定位" and fallback["镜次"] is None,
          "无源视频：中段兜底帧 定位=未定位、镜次=None（不拿首帧/他镜帧冒充）", fallback)
    check(missing["定位"] == "未定位" and missing["镜次"] is None and missing["url"] == "",
          "抽帧失败：代表帧 url 空、镜次 None、未定位", missing)


# ---------------------------------------------------------------- D pop-3--7 真实 mm:ss 反例

def section_real_mmss() -> None:
    print("\n[D] pop-3--7 真实 mm:ss 反例（15s 长片的两段整数是分:秒，不是 ss:cs）")
    disc = ROOT / "analysis" / "reverse" / "pop-3--7" / "拆解.md"
    if not disc.is_file():
        check(False, "本机缺 analysis/reverse/pop-3--7/拆解.md（真实反例样本）")
        return
    markdown = disc.read_text(encoding="utf-8-sig", errors="replace")
    layers = confirm.split_layers(markdown)
    duration = confirm.probe_duration(markdown)
    check(close(duration or 0.0, 15.0), "pop-3--7 头部片长 = 15.00s", f"probe_duration={duration}")
    times = confirm.parse_shot_times(layers.get("七 分镜表", ""), duration)
    want = {"4": (8.0, 10.0), "5": (10.0, 12.0), "6": (12.0, 15.0)}
    got = {}
    for shot, pair in want.items():
        if str(shot) in times:
            got[shot] = seconds_of(times[str(shot)])
    ok = (len(got) == 3
          and all(close(got[shot][0], pair[0]) and close(got[shot][1], pair[1])
                  for shot, pair in want.items()))
    check(ok, "真实分镜 00:08-00:10 / 00:10-00:12 / 00:12-00:15 → 8~10 / 10~12 / 12~15 秒"
              "（mm:ss；禁读成 0.08~0.10 的 ss:cs）", f"{got}｜全表 {times}")


def main() -> int:
    section_formats()
    section_fixtures()
    section_pop4()
    section_real_mmss()
    fails = [name for ok, name in results if not ok]
    print(f"\n{len(results) - len(fails)}/{len(results)} PASS"
          + (f"；FAIL：{'、'.join(fails)}" if fails else ""))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
