#!/usr/bin/env python
"""AT-10 C1 探针：pop-4 复算断言（读层断链修复 + 消歧 v2 + 代表帧 + 共享帧去重 + 右端排除）。

跑法：wanrepro/ 下 `python -m automation.probe_at10_c1 [port]`（默认 8776 常驻工作台；
判据全对才 exit 0，任何一步失败如实 FAIL 并 exit 1——判 returncode，禁子串判通过）。

八组判据（docs/nodes/AT-10.md §三 C1 行 + L1 审计 C01/C02/C03/C06/C11/C12 闭合口径）：
  ① 镜表复算：pop-4 真拆 七 层 → 三段 0~2.03 / 2.03~4.07 / 4.07~6.80（±0.01）；头部片长 6.85s；
     `00:00-02:03` 歧义两段整表联判为 ss:cs（2.03/4.07/6.80，不读成 123s）。
  ② 代表镜优先级：#1/#5→镜3（画面位置键）、#2/#3/#4/#6→镜1（位置键）、#7→意图建议镜3（无键回退）。
  ③ 实际帧秒落代表镜开区间（右端 0.05s 排除；镜3 允许复用镜内缓存 5.35s）。
  ④ 同镜行代表帧 URL 相等：#2/3/4/6 一组、#1/5/7 一组，两组不同（组装级共享，非 URL 去重）。
  ⑤ 替身统计：抽帧入口/真 ffmpeg 按目标文件计数——同镜只抽一次、每目标至多一次；
     连续两次 build，第二次真抽帧 0 次（重复组装不再抽）。
  ⑥ #7 无自身框标志（自身框=False），#1-#6 True；未定位路径镜次=None、不冒充。
  ⑦ 旧字段零变化（AT-8 断言口径）：帧[] 非空且 url/秒/来源 齐全、帧数与出现时段等长、
     卡级 定位 ∈ {精确,未精确定位,未定位}；sidecar 空时段行仍「中段兜底/未精确定位」。
  ⑧ 真机桥面（8776）：get_task auto-0012 透出 代表帧/自身框，同镜 URL 经 HTTP 相等，
     代表帧 URL GET 200 且 PNG 字节（要求桥已重启到本批代码）。
探针只读真数据（pop-4/auto-0012），不写清单、不动任务；新抽的代表帧是运行时产物，与既有 at8-* 同类。
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]          # wanrepro/
sys.path.insert(0, str(ROOT))
from automation import cards, frames  # noqa: E402

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8776
BASE = f"http://127.0.0.1:{PORT}"
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 红线：本机直连不走代理
CONFIRM = cards._confirm()                          # 复用 pipeline/reverse/confirm.py 解析件
POP4 = ROOT / "analysis" / "reverse" / "pop-4"
BRIDGE = "pop-4"
LOCAL = "auto-0012"
TOL = 0.01
EXPECT_SHOT = {1: 3, 2: 1, 3: 1, 4: 1, 5: 3, 6: 1, 7: 3}
SHOT_BOUNDS = {1: (0.0, 2.03), 2: (2.03, 4.07), 3: (4.07, 6.80)}
DURATION = 6.85

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" —— {detail}" if detail and not cond else ""))
    return bool(cond)


def req(path: str) -> tuple[int, dict | bytes]:
    r = urllib.request.Request(BASE + path)
    try:
        resp = OP.open(r, timeout=60)
        raw = resp.read()
        ctype = resp.headers.get("Content-Type") or ""
        return resp.status, (json.loads(raw) if "json" in ctype else raw)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return e.code, raw
    except urllib.error.URLError as exc:
        return 0, str(exc).encode()


def close(left: float, right: float, tol: float = TOL) -> bool:
    return abs(float(left) - float(right)) <= tol


# ---------------------------------------------------------------- ① 镜表复算
def probe_shot_table() -> bool | None:
    print("1) pop-4 镜表复算（读层断链修复 + 消歧 v2）")
    if not (POP4 / "拆解.md").is_file():
        check(f"真拆数据 {POP4}/拆解.md 在场", False, str(POP4))
        return False
    markdown = (POP4 / "拆解.md").read_text(encoding="utf-8-sig", errors="replace")
    check("头部片长 = 6.85s", close(CONFIRM.probe_duration(markdown) or 0.0, DURATION),
          f"probe_duration={CONFIRM.probe_duration(markdown)}")
    layers = CONFIRM.split_layers(markdown)
    times = CONFIRM.parse_shot_times(layers.get("七 分镜表", ""), DURATION)
    ok, got = True, []
    for shot, (start, end) in SHOT_BOUNDS.items():
        pair = times.get(str(shot))
        if not pair:
            ok = False
            break
        s, e = frames.clock_seconds(pair[0]), frames.clock_seconds(pair[1])
        got.append((shot, s, e))
        ok = ok and s is not None and e is not None and close(s, start) and close(e, end)
    check("镜表三段 = 0~2.03 / 2.03~4.07 / 4.07~6.80（±0.01）", ok, f"{times}｜解析 {got}")

    mixed = ("镜号 | 起秒-止秒 | 运动主体 | 画面内容\n"
             "1 | 00:00-02:03 | 人物 | 掷瓶升空\n"
             "2 | 02:03-04:07 | 瓶盖 | 升空特写\n"
             "3 | 04:07-06:80 | 瓶身 | 落沙定格\n")
    amb = CONFIRM.parse_shot_times(mixed, DURATION)
    amb_ok = all(f"{shot}" in amb for shot in (1, 2, 3)) and \
        all(close(frames.clock_seconds(amb[str(shot)][1]), end)
            for shot, end in ((1, 2.03), (2, 4.07), (3, 6.80)))
    check("歧义两段 00:00-02:03 整表联判 ss:cs（2.03/4.07/6.80，非 123s）", amb_ok, f"{amb}")
    return bool(times)


# ---------------------------------------------------------------- ②-⑦ 本地复算
def probe_cards_local() -> dict[int, dict] | None:
    print(f"2) cards.build({BRIDGE}, {LOCAL}) 复算（替身统计抽帧）")
    ensure_calls: list[tuple[float, str]] = []
    ffmpeg_calls: list[str] = []
    original_ensure, original_ffmpeg = frames._ensure, frames._ffmpeg_frame

    def spy_ensure(video, at, out):
        ensure_calls.append((float(at), Path(out).name))
        return original_ensure(video, at, out)

    def spy_ffmpeg(video, at, out):
        ffmpeg_calls.append(Path(out).name)
        return original_ffmpeg(video, at, out)

    frames._ensure, frames._ffmpeg_frame = spy_ensure, spy_ffmpeg
    try:
        built = cards.build(BRIDGE, LOCAL)
        first_ffmpeg = list(ffmpeg_calls)
        first_ensure = list(ensure_calls)
        built_again = cards.build(BRIDGE, LOCAL)          # 重复组装：同任务读包第二次
    finally:
        frames._ensure, frames._ffmpeg_frame = original_ensure, original_ffmpeg

    if not check("asset_cards 7 行", len(built) == 7, f"{len(built)} 行"):
        return None
    by_no = {card["n"]: card for card in built}

    # ② 代表镜
    got_shot = {no: (card.get("代表帧") or {}).get("镜次") for no, card in by_no.items()}
    check("代表镜 #1/#5=3、#2/#3/#4/#6=1、#7=意图镜3", got_shot == EXPECT_SHOT, f"{got_shot}")

    # ③ 帧秒开区间（右端 0.05 排除）
    secs = {no: float((card.get("代表帧") or {}).get("秒") or -1) for no, card in by_no.items()}
    outside = [(no, secs[no]) for no, shot in EXPECT_SHOT.items()
               if not (SHOT_BOUNDS[shot][0] - 1e-9 <= secs[no] <= SHOT_BOUNDS[shot][1] - 0.05 + 1e-9)]
    check("实际帧秒落代表镜开区间（秒<镜右端-0.05；镜3 允许 5.35 缓存）", not outside,
          f"越界 {outside}｜全部 {secs}")

    # ④ 同镜 URL 相等
    urls = {no: (card.get("代表帧") or {}).get("url") or "" for no, card in by_no.items()}
    group1 = {urls[no] for no in (2, 3, 4, 6)}
    group3 = {urls[no] for no in (1, 5, 7)}
    check("同镜行代表帧 URL 相等：镜1={#2,3,4,6}、镜3={#1,5,7}，两组不同",
          len(group1) == 1 and len(group3) == 1 and group1 != group3 and "" not in group1 | group3,
          f"镜1 {group1}｜镜3 {group3}")

    # ⑤ 替身统计：同镜只抽一次；第二次组装真抽帧 0 次
    shot_targets = [name for _at, name in first_ensure
                    if name.startswith(frames.SHOT_PREFIX) or name.startswith(frames.FALLBACK_PREFIX)]
    per_target: dict[str, int] = {}
    for name in shot_targets:
        per_target[name] = per_target.get(name, 0) + 1
    check("抽帧入口每目标文件至多 1 次（组装级共享，非 URL 去重）",
          all(count == 1 for count in per_target.values()), f"{per_target}")
    ffmpeg_shot = [name for name in first_ffmpeg
                   if name.startswith(frames.SHOT_PREFIX) or name.startswith(frames.FALLBACK_PREFIX)]
    check("真 ffmpeg 抽帧每目标至多 1 次（首次冷启 ≤1/目标，热缓存可为 0）",
          len(set(ffmpeg_shot)) == len(ffmpeg_shot) and len(ffmpeg_shot) <= len(per_target),
          f"真抽 {ffmpeg_shot}｜入口 {per_target}")
    check("重复组装不再抽帧（第二次 build 真 ffmpeg 0 次）",
          not ffmpeg_calls[len(first_ffmpeg):], f"第二次 {ffmpeg_calls[len(first_ffmpeg):]}")

    # ⑥ 自身框标志 + 未定位不冒充
    own = {no: card.get("自身框") for no, card in by_no.items()}
    check("#7 无自身框标志（自身框=False），#1-#6 True",
          own == {1: True, 2: True, 3: True, 4: True, 5: True, 6: True, 7: False}, f"{own}")
    contract = all({"url", "秒", "镜次", "定位"} <= set(card.get("代表帧") or {})
                   for card in built)
    check("代表帧合同字段 {url,秒,镜次,定位} 齐全", contract,
          str(sorted((by_no[1].get("代表帧") or {}).keys())))
    check("定位 ∈ {精确, 未定位}（代表帧两值合同）",
          all(card["代表帧"]["定位"] in ("精确", "未定位") for card in built),
          str([card["代表帧"]["定位"] for card in built]))
    original_analysis = frames.ANALYSIS
    temp_analysis = Path(tempfile.mkdtemp(prefix="at10-c1-probe-"))
    frames.ANALYSIS = temp_analysis
    try:
        missing_ok = frames.build_shot_frame("不存在的桥任务", "auto-9999", 2, 0.0, 1.0, [], None)
    finally:
        frames.ANALYSIS = original_analysis
        shutil.rmtree(temp_analysis, ignore_errors=True)   # 探针不留垃圾：临时帧目录用完即删
    check("抽不到帧 → 未定位、镜次 None、url 空（不冒充）",
          missing_ok["定位"] == "未定位" and missing_ok["镜次"] is None and missing_ok["url"] == "",
          str(missing_ok))
    # ⑦ 旧字段零变化（AT-8 断言口径）
    frames_ok = all(isinstance(card.get("帧"), list) and card["帧"] for card in built)
    check("旧字段：每张卡 帧 非空 list", frames_ok, str([c["n"] for c in built if not c.get("帧")]))
    check("旧字段：帧数与出现时段等长（时段空 = 1 张兜底帧）",
          all(len(c["帧"]) == max(1, len(c["出现时段"])) for c in built),
          str([(c["n"], len(c["帧"]), len(c["出现时段"])) for c in built
               if len(c["帧"]) != max(1, len(c["出现时段"]))]))
    check("旧字段：帧对象 url/秒/来源 齐全",
          all({"url", "秒", "来源"} <= set(f) for c in built for f in c["帧"]), "")
    check("旧字段：卡级定位 ∈ {精确,未精确定位,未定位}",
          all(c.get("定位") in ("精确", "未精确定位", "未定位") for c in built),
          str([c.get("定位") for c in built]))
    stale = [c["n"] for c in built
             if not c["出现时段"]
             and (c["帧"][0].get("来源"), c["帧"][0].get("定位")) != ("中段兜底", "未精确定位")]
    check("旧字段：sidecar 空时段行仍 中段兜底/未精确定位（AT-8 语义不动）", not stale,
          f"异常行 {stale}")
    return by_no


# ---------------------------------------------------------------- ⑧ 真机桥面
def probe_http(by_no: dict[int, dict] | None) -> None:
    print(f"3) 真机桥面 {BASE}（get_task auto-0012 → 代表帧透传 + 帧 URL 可取）")
    status, body = req("/api/hub/task/auto-0012")
    if not check("GET /api/hub/task/auto-0012 → 200", status == 200 and isinstance(body, dict),
                 f"{status} {str(body)[:120]}"):
        return
    cs = (body.get("task") or {}).get("asset_cards") or []
    if not check("桥面 asset_cards 7 行", len(cs) == 7, f"{len(cs)} 行"):
        return
    http_by_no = {card["n"]: card for card in cs}
    check("桥面每卡带 代表帧+自身框（新字段透传）",
          all(isinstance(c.get("代表帧"), dict) and isinstance(c.get("自身框"), bool) for c in cs),
          str([(c["n"], sorted(c.get("代表帧") or {}), c.get("自身框")) for c in cs][:2]))
    got_shot = {no: (c.get("代表帧") or {}).get("镜次") for no, c in http_by_no.items()}
    check("桥面代表镜 = 本地复算（#1/#5=3、#2/3/4/6=1、#7=3）", got_shot == EXPECT_SHOT, f"{got_shot}")
    check("桥面 #7 自身框=False",
          http_by_no[7].get("自身框") is False, str(http_by_no[7].get("自身框")))
    urls = {no: (c.get("代表帧") or {}).get("url") or "" for no, c in http_by_no.items()}
    g1, g3 = {urls[n] for n in (2, 3, 4, 6)}, {urls[n] for n in (1, 5, 7)}
    check("桥面同镜行代表帧 URL 相等且两组不同", len(g1) == 1 and len(g3) == 1 and g1 != g3,
          f"镜1 {g1}｜镜3 {g3}")
    if by_no:
        check("桥面 URL 与本地复算一致（同一组装函数）",
              all(urls[no] == (by_no[no].get("代表帧") or {}).get("url") for no in urls), "")
    bad = []
    for no, url in urls.items():
        s, b = req(url)
        if s != 200 or not isinstance(b, bytes) or not b.startswith(b"\x89PNG"):
            bad.append((no, url, s))
    check("每张代表帧 URL GET 200 且 PNG 字节", not bad, str(bad[:3]))


def main() -> int:
    print(f"[probe-at10-c1] 目标 {BASE}；样本 {BRIDGE}/{LOCAL}（只读）")
    probe_shot_table()
    by_no = probe_cards_local()
    probe_http(by_no)
    fails = [r for r in RESULTS if not r[1]]
    print(f"\n[probe-at10-c1] {len(RESULTS) - len(fails)}/{len(RESULTS)} PASS")
    for name, _ok, detail in fails:
        print(f"  FAIL 汇总: {name} —— {detail}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
