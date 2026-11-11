#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AT-15 S1 探针：代表帧回退链（镜次直取 → 时段帧 → 诚实兜底）+ 分镜表宽松复算。

跑法：wanrepro/ 下 `python -m automation.probe_at15_s1`（纯离线：无 HTTP、不跑 ffmpeg、
零花钱；判据全对才 exit 0，任何一步失败如实 FAIL 并 exit 1 —— 判 returncode，禁子串判通过）。

现场（auto-0036 / pop-1）：八张卡代表帧全部落到同一张 3.02s 中段兜底帧、帧内框全空。实测断点
不是「只认出现于」，而是**分镜表整表被严格闸门打回**——`confirm.parse_shot_times` 要求末段右端
≈ 片长（±0.5s），pop-1 分镜表止 00:05 而片长 6.04s（片尾品牌定格），差 1.04s → `{}` →
`bounds` 为空 → 画面位置镜次与意图建议镜次两条路同时死掉 → 全兜底。修法见
`automation/cards.py:shot_bounds` 与 `cards.build.representative`。

判据：
  [1] 断点实证 + 宽松复算：严格闸门确实整表拒；宽松复算得 4 镜（0~1/1~2/2~3/3~5）；
      越界镜丢镜不丢表 / 段间不单调整表不认 / 单镜表不认；pop-4 严格路优先（宽松路不被调用）。
  [2] 夹具全量复算：8 卡代表帧 8/8 非兜底、秒值 {0.5,1.5,4.0} 三组、落镜开区间（右端 -0.05
      排除）、同镜 URL 相等且三组互异、帧内框非空且自身框标记正确、同镜只抽一次。
  [3] 回退链各路：③ 无镜次但有精确帧 → 代表帧=首个精确时段帧（镜次 None、不冒充）；
      ④ 全无 → 中段兜底（诚实）；镜次越界 → 不炸，顺延下一路。
  [4] 真数据只读（pop-1 四件 + auto-0036/任务.json SHA256 前后不变）+ 跑后零残留。

夹具 = `tests/fixtures/at15-s1/`（pop-1 只读快照，见其 README）。探针把夹具复制进临时
ANALYSIS 根再动手改造，源码夹具与真实盘面全程只读；`_ensure` 换成落 1×1 PNG 的替身，
不依赖 ffmpeg、不产生真实帧。
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]          # wanrepro/
sys.path.insert(0, str(ROOT))
from automation import cards, frames, replace  # noqa: E402

FIXTURE_SRC = ROOT / "tests" / "fixtures" / "at15-s1"
BRIDGE = "probe-at15-s1"
LOCAL = "auto-probe15"
REAL_BRIDGE = "pop-1"
REAL = ROOT / "analysis" / "reverse" / REAL_BRIDGE
REAL_TASK = ROOT / "automation" / "projects" / "auto-0036"
POP4 = ROOT / "analysis" / "reverse" / "pop-4"
DURATION = 6.04                                     # pop-1 片长（拆解.md 头部 + ffprobe 实测 6.037）
WANT_BOUNDS = {1: (0.0, 1.0), 2: (1.0, 2.0), 3: (2.0, 3.0), 4: (3.0, 5.0)}
WANT_SHOT = {1: 4, 2: 2, 3: 1, 4: 2, 5: 4, 6: 2, 7: 1, 8: 4}
WANT_SECS = {1: 4.0, 2: 1.5, 3: 0.5, 4: 1.5, 5: 4.0, 6: 1.5, 7: 0.5, 8: 4.0}
FIX_NAMES = ("拆解.md", "待确认清单.md", "待确认清单.json", "意图建议.md")
PNG_A = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c626001000000ffff03000006000557bfabd4"
    "0000000049454e44ae426082")

WORK = Path(tempfile.mkdtemp(prefix="probe-at15-s1-"))
FIX = WORK / BRIDGE
RESULTS: list[tuple[str, bool, str]] = []
ENSURE_CALLS: list[tuple[float, str]] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" —— {detail}" if detail and not cond else ""))
    return bool(cond)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def close(left, right, tol: float = 0.01) -> bool:
    return abs(float(left) - float(right)) <= tol


# ---------------------------------------------------------------- 夹具与替身

def stub_ensure(video, at, out):
    """抽帧替身：不跑 ffmpeg，落 1×1 PNG 并记账（同镜去重断言靠它）。"""
    out = Path(out)
    ENSURE_CALLS.append((float(at), out.name))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(PNG_A)
    return True, ""


def _fix_file(bridge_id: str, name: str) -> Path:
    """replace 层的桥目录解析替身：只认临时夹具根（真数据一律不碰）。"""
    path = WORK / str(bridge_id) / name
    if not path.is_file():
        raise replace.ReplaceError(f"夹具缺文件 {path}")
    return path


def reset_fixture(sidecar=None, intent_text: str | None = None) -> None:
    """把探针自带夹具复制进临时根（可覆盖 sidecar / 意图建议做单路判据）。"""
    FIX.mkdir(parents=True, exist_ok=True)
    for name in FIX_NAMES:
        shutil.copy2(FIXTURE_SRC / name, FIX / name)
    if sidecar is not None:
        (FIX / "待确认清单.json").write_text(
            json.dumps(sidecar, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if intent_text is not None:
        (FIX / "意图建议.md").write_text(intent_text, encoding="utf-8")


class Patches:
    """隔离补丁组：ANALYSIS 双根 + replace 桥目录解析 + 抽帧/时长替身。"""

    def __enter__(self):
        self.ps = [
            patch.object(cards, "ANALYSIS", WORK),
            patch.object(frames, "ANALYSIS", WORK),
            patch.object(replace, "checklist_path",
                         lambda bid: _fix_file(bid, "待确认清单.md")),
            patch.object(replace, "sidecar_path",
                         lambda bid: _fix_file(bid, "待确认清单.json")),
            patch.object(frames, "_ensure", stub_ensure),
            patch.object(frames, "duration", lambda video=None: DURATION),
        ]
        for p in self.ps:
            p.start()
        ENSURE_CALLS.clear()
        return self

    def __exit__(self, *exc):
        for p in reversed(self.ps):
            p.stop()
        return False


def base_sidecar() -> dict:
    return json.loads((FIXTURE_SRC / "待确认清单.json").read_text(encoding="utf-8-sig"))


def sidecar_with(no: int, position) -> dict:
    payload = base_sidecar()
    for row in payload.get("rows") or []:
        if row.get("n") == no:
            row["画面位置"] = position
    return payload


def intent_without(*rows: int) -> str:
    """意图建议.md 删掉指定行号（`- 5｜理由：…` 形态）——用来逼出回退链的下一路。"""
    text = (FIXTURE_SRC / "意图建议.md").read_text(encoding="utf-8-sig")
    drop = {f"- {no}｜" for no in rows}
    kept = [line for line in text.splitlines() if not line.strip().startswith(tuple(drop))]
    return "\n".join(kept) + "\n"


# ---------------------------------------------------------------- [1] 断点 + 宽松复算
def probe_shot_table() -> None:
    print("[1] 断点实证 + 分镜表宽松复算")
    md = (FIXTURE_SRC / "拆解.md").read_text(encoding="utf-8-sig")
    shots_layer = cards._split(md).get("七 分镜表", "")
    cf = cards._confirm()
    check("夹具含「七 分镜表」层", bool(shots_layer.strip()), f"字节 {len(shots_layer)}")
    strict = cf.parse_shot_times(shots_layer, DURATION)
    check("严格闸门（含片长）整表被拒 → {}（现场断点）", strict == {}, str(strict))
    rows = cf._clock_span_rows(shots_layer)
    ends = [frames.clock_seconds(end) for _no, _s, end in rows]
    check("分镜表末镜右端 5.00s vs 片长 6.04s（差 > 0.5s，正是闸门打回原因）",
          bool(rows) and ends and close(max(e for e in ends if e is not None), 5.0)
          and max(ends) < DURATION - 0.5, f"末镜右端 {max(ends) if ends else None}")
    got = cards.shot_bounds(shots_layer, DURATION)
    ok = set(got) == set(WANT_BOUNDS) and all(
        close(got[shot][0], want[0]) and close(got[shot][1], want[1])
        for shot, want in WANT_BOUNDS.items())
    check("宽松复算 → 4 镜 0~1 / 1~2 / 2~3 / 3~5（±0.01）", ok, str(got))

    over = ("镜号 | 起秒-止秒 | 画面内容\n"
            "1 | 00:00-00:20 | a\n"
            "2 | 00:20-00:30 | b\n")
    check("负例：单镜读法越界（片长 6.04 读成 20/30s）→ 丢镜后不足两镜 → {}",
          cards.shot_bounds(over, DURATION) == {},
          str(cards.shot_bounds(over, DURATION)))
    monotone = ("镜号 | 起秒-止秒 | 画面内容\n"
                "1 | 00:00-00:03 | a\n"
                "2 | 00:01-00:02 | b\n")
    check("负例：段间不单调 → 整表不认 {}（不猜镜边界）",
          cards.shot_bounds(monotone, DURATION) == {},
          str(cards.shot_bounds(monotone, DURATION)))
    single = "镜号 | 起秒-止秒 | 画面内容\n1 | 00:00-00:05 | a\n"
    check("负例：单镜表无法自证 → {}",
          cards.shot_bounds(single, DURATION) == {}, str(cards.shot_bounds(single, DURATION)))

    pop4_md = (POP4 / "拆解.md").read_text(encoding="utf-8-sig") if (POP4 / "拆解.md").is_file() else ""
    pop4_layer = cards._split(pop4_md).get("七 分镜表", "") if pop4_md else ""
    if pop4_layer:
        called: list[bool] = []
        original = cards._relaxed_bounds

        def spy(_layer, _total):
            called.append(True)
            return {}

        cards._relaxed_bounds = spy
        try:
            pop4 = cards.shot_bounds(pop4_layer, 6.85)
        finally:
            cards._relaxed_bounds = original
        want = {1: (0.0, 2.03), 2: (2.03, 4.07), 3: (4.07, 6.80)}
        ok = (set(pop4) == set(want)
              and all(close(pop4[s][0], w[0]) and close(pop4[s][1], w[1]) for s, w in want.items()))
        check("pop-4 严格路优先（0~2.03 / 2.03~4.07 / 4.07~6.80，宽松路零调用）",
              ok and not called, f"{pop4}｜宽松路调用 {bool(called)}")
    else:
        check("pop-4 严格路优先（夹具缺失，跳过）", False, f"{POP4}/拆解.md 不在")


# ---------------------------------------------------------------- [2] 夹具全量复算
def probe_full_build() -> dict[int, dict] | None:
    print("[2] 夹具全量复算：8 卡代表帧回退链（pop-1 / auto-0036 形态）")
    reset_fixture()
    with Patches():
        built = cards.build(BRIDGE, LOCAL)
        ensure_calls = list(ENSURE_CALLS)
    if not check("asset_cards 8 行", len(built) == 8, f"{len(built)} 行"):
        return None
    by_no = {card["n"]: card for card in built}
    reps = {no: card.get("代表帧") or {} for no, card in by_no.items()}
    got_shot = {no: rep.get("镜次") for no, rep in reps.items()}
    got_secs = {no: rep.get("秒") for no, rep in reps.items()}
    fallback = [no for no, rep in reps.items() if rep.get("来源") == "中段兜底"]
    check("代表镜 = 画面位置键镜次（#1/#5/#8→4、#2/#4/#6→2、#3/#7→1；#8 走意图建议）",
          got_shot == WANT_SHOT, str(got_shot))
    check("代表帧 8/8 非中段兜底（现场为 0/8）", not fallback, f"仍兜底 {fallback}")
    check("秒值三组 {0.5, 1.5, 4.0}（现场八卡同为 3.02）",
          got_secs == WANT_SECS, str(got_secs))
    outside = [(no, got_secs[no]) for no, shot in WANT_SHOT.items()
               if not (WANT_BOUNDS[shot][0] - 1e-9 <= float(got_secs[no])
                       <= WANT_BOUNDS[shot][1] - 0.05 + 1e-9)]
    check("每卡秒落代表镜开区间（右端 -0.05s 排除，不越到邻镜）", not outside, str(outside))
    urls = {no: rep.get("url") or "" for no, rep in reps.items()}
    group_shot2, group_shot1, group_shot4 = ({urls[n] for n in (2, 4, 6)},
                                             {urls[n] for n in (3, 7)},
                                             {urls[n] for n in (1, 5, 8)})
    check("同镜代表帧 URL 相等：镜2={#2,4,6}、镜1={#3,7}、镜4={#1,5,8}，三组互异",
          all(len(g) == 1 and "" not in g for g in (group_shot2, group_shot1, group_shot4))
          and len({group_shot2.pop(), group_shot1.pop(), group_shot4.pop()}) == 3,
          f"镜2 {group_shot2}｜镜1 {group_shot1}｜镜4 {group_shot4}")
    want_boxes = {1: 2, 2: 3, 3: 2, 4: 3, 5: 2, 6: 3, 7: 2, 8: 2}
    got_boxes = {no: len(card.get("帧内框") or []) for no, card in by_no.items()}
    check("帧内框非空且按镜成组（现场全空）", got_boxes == want_boxes, str(got_boxes))
    own = {no: card.get("自身框") for no, card in by_no.items()}
    check("自身框：#1-#7 True（画面位置键即代表镜）、#8 False（无格键，只借同镜他人框）",
          own == {1: True, 2: True, 3: True, 4: True, 5: True, 6: True, 7: True, 8: False}, str(own))
    empty_spans = [no for no, card in by_no.items() if not card.get("出现时段")]
    check("侧车出现时段为空的行（#2-#7）代表帧仍精确（镜次直取生效，卡级定位仍如实未精确定位）",
          bool(empty_spans) and all(reps[no].get("定位") == "精确" for no in empty_spans)
          and all(by_no[no].get("定位") == "未精确定位" for no in empty_spans),
          f"空时段行 {empty_spans}｜代表帧定位 {[reps[n].get('定位') for n in empty_spans]}")
    shot_targets = [name for _at, name in ensure_calls if name.startswith(frames.SHOT_PREFIX)]
    per_target: dict[str, int] = {}
    for name in shot_targets:
        per_target[name] = per_target.get(name, 0) + 1
    check("同镜只抽一次（每 at8-shot-* 目标至多 1 次，共 3 个目标）",
          len(per_target) == 3 and all(count == 1 for count in per_target.values()),
          str(per_target))
    return by_no


# ---------------------------------------------------------------- [3] 回退链各路
def probe_chain_paths() -> None:
    print("[3] 回退链各路：③ 时段帧 / ④ 诚实兜底 / 镜次越界不炸")
    reset_fixture(sidecar=sidecar_with(1, None), intent_text=intent_without(1))
    with Patches():
        card1 = next(c for c in cards.build(BRIDGE, LOCAL) if c["n"] == 1)
    first_exact = next((f for f in card1["帧"] if f.get("定位") == "精确"), None)
    rep = card1.get("代表帧") or {}
    check("③ 无镜次（画面位置/意图建议都缺）但有精确时段帧 → 代表帧=首个精确帧",
          first_exact is not None and rep.get("url") == first_exact.get("url")
          and rep.get("秒") == first_exact.get("秒") and rep.get("定位") == "精确",
          f"代表帧 {rep}｜首个精确帧 {first_exact}")
    check("③ 时段帧不冒充镜次（镜次 None、来源=时段帧、自身框 False）",
          rep.get("镜次") is None and rep.get("来源") == "时段帧" and card1.get("自身框") is False,
          f"{rep}｜自身框 {card1.get('自身框')}")

    reset_fixture(sidecar=sidecar_with(2, None), intent_text=intent_without(2))
    with Patches():
        card2 = next(c for c in cards.build(BRIDGE, LOCAL) if c["n"] == 2)
    rep2 = card2.get("代表帧") or {}
    check("④ 全无（无镜次、无从出现时段、无精确帧）→ 中段兜底，诚实标注",
          rep2.get("来源") == "中段兜底" and rep2.get("定位") == "未定位"
          and rep2.get("镜次") is None, str(rep2))

    reset_fixture(sidecar=sidecar_with(3, {"格": 5, "镜次": 9}))
    try:
        with Patches():
            card3 = next(c for c in cards.build(BRIDGE, LOCAL) if c["n"] == 3)
    except Exception as exc:                                  # noqa: BLE001 —— 越界必须不炸
        check("镜次越界（9 > 分镜表 4 镜）不炸", False, f"抛了 {type(exc).__name__}: {exc}")
        card3 = None
    if card3 is not None:
        rep3 = card3.get("代表帧") or {}
        check("镜次越界 → 不炸、顺延意图建议镜 1（秒 0.5）",
              rep3.get("镜次") == 1 and close(rep3.get("秒") or -1, 0.5), str(rep3))

    reset_fixture(sidecar=sidecar_with(3, {"格": 5, "镜次": 9}), intent_text=intent_without(3))
    with Patches():
        card3b = next(c for c in cards.build(BRIDGE, LOCAL) if c["n"] == 3)
    rep3b = card3b.get("代表帧") or {}
    check("镜次越界 ∧ 无意图建议 → 落到诚实兜底（不拿他镜帧冒充）",
          rep3b.get("来源") == "中段兜底" and rep3b.get("镜次") is None, str(rep3b))


# ---------------------------------------------------------------- [4] 真数据只读 + 残留
def probe_readonly(hashes_before: dict[str, str]) -> None:
    print("[4] 真数据只读 + 零残留")
    unchanged = {str(p): sha256(p) for p in WATCH} == hashes_before
    check("真数据只读（pop-1 拆解/清单/sidecar/意图建议、auto-0036/任务.json SHA256 不变）",
          unchanged, "有文件被改动！" if not unchanged else "")
    shutil.rmtree(WORK, ignore_errors=True)
    check("跑后零残留（临时夹具根已删）", not WORK.exists(), str(WORK))


WATCH = [REAL / "拆解.md", REAL / "待确认清单.md", REAL / "待确认清单.json",
         REAL / "意图建议.md", REAL_TASK / "任务.json"]


def main() -> int:
    hashes_before = {}
    for path in WATCH:
        if path.is_file():
            hashes_before[str(path)] = sha256(path)
        else:
            print(f"  [WARN] 只读校验文件不在场（跳过该条）：{path}")
    print(f"[probe-at15-s1] 临时夹具根 {WORK}；夹具源 {FIXTURE_SRC}（只读）")
    try:
        probe_shot_table()
        probe_full_build()
        probe_chain_paths()
    finally:
        probe_readonly(hashes_before)
    failed = [r for r in RESULTS if not r[1]]
    print(f"\nprobe_at15_s1: {len(RESULTS) - len(failed)}/{len(RESULTS)} PASS"
          + (f"，FAIL {len(failed)} 项" if failed else ""))
    for name, _ok, detail in failed:
        print(f"  [FAIL] {name} —— {detail[:240]}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
