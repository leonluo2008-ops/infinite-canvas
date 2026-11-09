#!/usr/bin/env python
"""AT-10 C4 收口探针：帧对镜次 + 框坐标 + 三态 + 共享帧去重（真实数据 pop-4/auto-0012 + 自建夹具）。

跑法：wanrepro/ 下 `python -m automation.probe_at10 [port]`（默认 8776 常驻工作台；
判据全对才 exit 0，任何一步失败如实 FAIL 并 exit 1——判 returncode，禁子串判通过）。

四组判据（docs/nodes/AT-10.md §三 C4 行 + baseline v2；缺陷 D1/D2/D3 反证锚定）：
  [1] 帧对镜次：镜表复算三段（0~2.03/2.03~4.07/4.07~6.80）；逐卡 代表帧.镜次=位置键镜
      （#1/#5→3、#2/#3/#4/#6→1、#7→意图镜3）；现抽帧 URL 文件名 `at8-shot-<镜号>-<秒>.png`
      的镜号==镜次（缓存复用帧按合同断言 来源=缓存 且秒已落镜内开区间）；秒落该镜开区间
      （右端 0.05s 排除）；**帧对镜次硬证**——帧画面经 16×16 灰度归一后与源视频同秒现抽帧
      逐像素比对（均值差 ≤8/255），即「帧就是那一镜那一秒的画」，
      中段兜底帧冒充镜次帧（D1）不可能过。
  [2] 框坐标：桥面 帧内框 九宫格矩阵 33.33/66.67、x+w/y+h≤100、行集合按镜身份
      （镜1={2,3,4,6}、镜3={1,5}）、逐卡本卡唯一、越界降级不出界；esbuild 打包
      frame-overlay.ts 纯几何模块 → 舞台按帧真实宽高比定尺寸（1080×1888→119×208）、
      contain 内容盒=整框、boxRect 像素换算在内容盒内、格5 四框同 rect、
      图例 rail chips 两两不相交、引线端点=框右缘中点（不按外框/固定 9:16 猜，L1 C07）。
  [3] 三态诚实：#1-#6 precise、#7 no-own-box（honest=「未能标注位置」）；
      frameCaption 逐字断言（`第1镜 · 1.01s · AI粗标`，不含旧「九宫格第N格」）；未定位自建
      夹具 → frameState=unlocated、帧内框=[]、文案=「未能定位到该资产出现的镜头 · 暂用整片
      画面」且不含镜次秒（D3 不再无感）；legacy（只有旧 帧[] 字段）分支文案不动。
  [4] 共享帧去重：同镜行代表帧 URL 相等且两组不同（组装级共享，非 URL 去重）；抽帧入口
      每目标文件至多 1 次、重复组装真 ffmpeg 0 次；桥面同镜 URL 相等、每张代表帧 URL
      GET 200 且 PNG 字节。
探针只读真数据（pop-4/auto-0012），不写清单、不动任务；未定位夹具与临时帧用完即删。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]          # wanrepro/
REPO = ROOT.parent                                  # 仓库根（web/ 在这）
sys.path.insert(0, str(ROOT))
from automation import cards, frames, replace  # noqa: E402

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
RATIO = 1080 / 1888                                 # pop-4 真帧宽高比（竖屏）

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


def _gray16(out_stem: Path, video: Path | None = None, sec: float | None = None,
            png: Path | None = None) -> bytes | None:
    """帧画面 → 16×16 灰度 raw（256 字节）：源视频按秒现抽，或对既有 PNG 归一。失败返回 None。"""
    raw = out_stem.with_suffix(".raw")
    if png is not None:
        cmd = ["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(png),
               "-frames:v", "1", "-vf", "scale=16:16", "-f", "rawvideo", "-pix_fmt", "gray",
               str(raw)]
    else:
        cmd = ["ffmpeg", "-v", "error", "-nostdin", "-y",
               "-ss", f"{max(0.0, sec or 0.0):.2f}", "-i", str(video), "-frames:v", "1",
               "-vf", "scale=16:16", "-f", "rawvideo", "-pix_fmt", "gray", str(raw)]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=120)
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0 or not raw.is_file() or raw.stat().st_size != 256:
        return None
    return raw.read_bytes()


# ---------------------------------------------------------------- [1] 帧对镜次
def probe_frame_matches_shot(by_no: dict[int, dict] | None) -> None:
    print("[1] 帧对镜次：镜表复算 + 代表镜身份链 + 字节级现抽比对（反 D1）")
    if by_no is None:
        check("本地复算失败，[1] 跳过（前置缺失）", False, "依赖 cards.build")
        return
    markdown = (POP4 / "拆解.md").read_text(encoding="utf-8-sig", errors="replace")
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

    got_shot = {no: (card.get("代表帧") or {}).get("镜次") for no, card in by_no.items()}
    check("代表镜 = 位置键镜（#1/#5→3、#2/#3/#4/#6→1、#7→意图镜3）", got_shot == EXPECT_SHOT,
          f"{got_shot}")
    check("逐卡帧秒落该镜开区间（秒<镜右端-0.05）",
          all(SHOT_BOUNDS[s][0] - 1e-9 <= float(by_no[n]["代表帧"]["秒"])
              <= SHOT_BOUNDS[s][1] - 0.05 + 1e-9 for n, s in EXPECT_SHOT.items()),
          str({n: by_no[n]["代表帧"]["秒"] for n in by_no}))

    # 帧文件身份链：现抽帧 `at8-shot-<镜号>-<秒>.png` 的镜号 == 代表帧.镜次；
    # 缓存复用帧（合同允许镜内开区间缓存，如镜3 复用 5.35s）断言 来源=缓存 且秒已落镜内
    bad_name = []
    for no, card in by_no.items():
        rep = card["代表帧"]
        name = Path(rep["url"]).name
        if rep.get("来源") == "缓存":
            if name.startswith(frames.SHOT_PREFIX) or name.startswith(frames.FALLBACK_PREFIX):
                bad_name.append((no, "缓存来源却带现抽文件名", name))
            continue
        parts = name.split("-")
        if not (name.startswith(frames.SHOT_PREFIX) and len(parts) >= 3
                and parts[2].isdigit() and int(parts[2]) == rep["镜次"]):
            bad_name.append((no, name))
    check("帧文件身份链：现抽文件名镜号==镜次；缓存复用帧来源=缓存（镜内秒已在开区间断言）", not bad_name,
          str(bad_name))

    # 帧对镜次硬证：帧画面 == 源视频同秒那一画。缓存复用帧的编码参数可能与现抽不同
    # （老缓存全宽 1080 vs 现抽 960），故比对双方都经 ffmpeg 归一到 16×16 灰度后逐像素比对
    # （同秒帧均值差 ≈ 个位灰阶；错帧/错镜是另一个画面，差值数量级不同）。
    video = frames.video_path(BRIDGE)
    if not check("源视频定位成功", bool(video) and Path(video).is_file(), str(video)):
        return
    tmp = Path(tempfile.mkdtemp(prefix="at10-frame-identity-"))
    try:
        mismatch = []
        seen: dict[float, bytes] = {}
        for no, card in by_no.items():
            sec = float(card["代表帧"]["秒"])
            url = card["代表帧"]["url"]
            local = ROOT / "analysis" / "reverse" / BRIDGE / "看帧" / Path(url).name
            if not local.is_file():
                mismatch.append((no, "本地帧文件缺席", url))
                continue
            want = seen.get(sec)
            if want is None:
                want = _gray16(tmp / f"re-{sec:.2f}", video, sec)
                seen[sec] = want
            got = _gray16(tmp / f"file-{Path(url).stem}", None, None, png=local)
            if want is None or got is None:
                mismatch.append((no, f"{sec}s 归一失败", "ffmpeg"))
                continue
            diff = sum(abs(a - b) for a, b in zip(want, got)) / len(want)
            if diff > 8.0:
                mismatch.append((no, f"{sec}s 画面与源视频同秒帧不符（灰度均值差 {diff:.1f}/255）",
                                 Path(url).name))
        check("帧对镜次硬证：每张代表帧画面 == 源视频同秒帧（16×16 灰度均值差 ≤8/255）", not mismatch,
              str(mismatch[:3]))
        fallback = [(no, Path(c["代表帧"]["url"]).name) for no, c in by_no.items()
                    if Path(c["代表帧"]["url"]).name.startswith(frames.FALLBACK_PREFIX)]
        check("无中段兜底帧冒充（无 at8-fallback-* 代表帧）", not fallback, str(fallback))
        check("无 D1 污染秒（没有任何卡用 3.42s 中段秒当代表帧）",
              all(close(float(c["代表帧"]["秒"]), 3.42) is False for c in by_no.values()),
              str({n: c["代表帧"]["秒"] for n, c in by_no.items()}))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)         # 临时帧用完即删


# ---------------------------------------------------------------- [2] 框坐标
def _esbuild_bundle(entry: Path, tag: str) -> Path | None:
    esbuild_cmd = REPO / "web" / "node_modules" / ".bin" / "esbuild.cmd"
    esbuild_sh = REPO / "web" / "node_modules" / ".bin" / "esbuild"
    esbuild_js = REPO / "web" / "node_modules" / "esbuild" / "bin" / "esbuild"
    if esbuild_cmd.is_file():
        argv = [str(esbuild_cmd)]
    elif esbuild_sh.is_file():
        argv = [str(esbuild_sh)]
    else:
        argv = ["node", str(esbuild_js)]
    if not (esbuild_cmd.is_file() or esbuild_sh.is_file() or esbuild_js.is_file()):
        return None
    tmp = Path(tempfile.mkdtemp(prefix=f"at10-{tag}-"))
    bundle = tmp / f"{tag}.bundle.mjs"
    run = subprocess.run(argv + [str(entry), "--bundle", "--format=esm", f"--outfile={bundle}"],
                         capture_output=True, text=True, timeout=120)
    if run.returncode != 0:
        print((run.stderr or run.stdout)[-300:])
        shutil.rmtree(tmp, ignore_errors=True)
        return None
    return bundle


def _node_json_runner(bundle: Path, runner_src: str, args: list[str]) -> tuple[bool, str, str]:
    tmp = bundle.parent
    runner = tmp / "runner.mjs"
    runner.write_text(runner_src, encoding="utf-8")
    out = tmp / "out.json"
    run = subprocess.run(["node", str(runner), Path(bundle).as_uri(), *args, str(out)],
                         capture_output=True, text=True, timeout=120)
    text = out.read_text(encoding="utf-8") if out.is_file() else ""
    return run.returncode == 0, text, (run.stderr or run.stdout)[-400:]


def probe_boxes(payload: dict | None, runner_text: str | None) -> None:
    print("[2] 框坐标：桥面九宫格矩阵 + frame-overlay.ts 几何换算（真实内容盒，禁外框猜）")
    if payload is None:
        check("桥面 payload 缺席，[2] 跳过（前置缺失）", False, "依赖桥面 JSON")
        return
    if runner_text is None:
        check("几何 runner 输出缺席，[2] 几何断言跳过（前置缺失）", False, "依赖 main 的 esbuild 打包")
        runner_text = "{}"
    g = json.loads(runner_text)
    cs = (payload.get("task") or {}).get("asset_cards") or []
    http_by_no = {card["n"]: card for card in cs}
    boxes_ok = all(isinstance(c.get("帧内框"), list) for c in cs)
    check("桥面每卡带 帧内框 数组", boxes_ok, str([(c["n"], type(c.get("帧内框")).__name__) for c in cs]))
    rows_of = {no: {b["行"] for b in (http_by_no[no].get("帧内框") or [])} for no in http_by_no}
    check("桥面框集合按镜身份：镜1 卡（#2/3/4/6）行集合={2,3,4,6}、镜3 卡（#1/5/7）={1,5}",
          all(rows_of[n] == {2, 3, 4, 6} for n in (2, 3, 4, 6))
          and all(rows_of[n] == {1, 5} for n in (1, 5, 7)), str(rows_of))
    own = {no: [b["行"] for b in (http_by_no[no].get("帧内框") or []) if b.get("本卡")]
           for no in http_by_no}
    check("逐卡本卡标记唯一且正确（#1→1…#6→6、#7 无）",
          own == {1: [1], 2: [2], 3: [3], 4: [4], 5: [5], 6: [6], 7: []}, str(own))
    matrix = {(b["x%"], b["y%"], b["w%"], b["h%"])
              for c in cs for b in (c.get("帧内框") or [])}
    check("九宫格坐标矩阵只出 33.33/66.67 组合（x/y∈{0,33.33,66.67}、w=h=33.33）",
          all(x in (0.0, 33.33, 66.67) and y in (0.0, 33.33, 66.67)
              and w == 33.33 and h == 33.33 for x, y, w, h in matrix), str(sorted(matrix)))
    off = [b for c in cs for b in (c.get("帧内框") or [])
           if b["x%"] + b["w%"] > 100.0 + 1e-9 or b["y%"] + b["h%"] > 100.0 + 1e-9]
    check("每框 x%+w%≤100 且 y%+h%≤100（防重叠合同）", not off, str(off[:3]))

    # frame-overlay.ts 纯几何换算（runner 输出由 main 打包计算后传入）
    check("舞台按帧真实宽高比定尺寸：1080×1888 → 119×208（高 208、宽 108–240 夹取）",
          g.get("stage") == {"w": 119, "h": 208}, str(g.get("stage")))
    if not g.get("stage"):
        return
    check("contain 内容盒 = 整框（真实比例下无 letterbox）",
          g["content"] == {"x": 0, "y": 0, "w": 119, "h": 208}, str(g["content"]))
    inside = [r for r in g["boxRects"]
              if not (0 <= r["rect"]["x"] and r["rect"]["x"] + r["rect"]["w"] <= g["content"]["w"] + 0.5
                      and 0 <= r["rect"]["y"] and r["rect"]["y"] + r["rect"]["h"] <= g["content"]["h"] + 0.5)]
    check("每框 px rect 在内容盒内（像素换算不越界）", not inside, str(inside[:3]))
    g5 = [r["rect"] for r in g["boxRects"]
          if r["行"] == 2 and r["card"] in (2, 3, 4, 6)]
    check("格5 四框同 rect（同格多框画满格叠加）",
          len(g5) == 4 and all(r == g5[0] for r in g5), str(g5))
    check("格5 框尺寸 ≈ 内容盒三分之一（39.67×69.33px @119×208）",
          g5 and close(g5[0]["w"], 119 / 3, 0.2) and close(g5[0]["h"], 208 / 3, 0.2),
          str(g5[:1]))
    check("框内 #行号 角标放得下（39.67×69.33 ≥ 30×16 门槛）",
          all(r["badge"] for r in g["boxRects"]), str([r["badge"] for r in g["boxRects"]]))
    chips = g["legend"]["chips"]
    cross = [(a["行"], b["行"]) for i, a in enumerate(chips) for b in chips[i + 1:]
             if a["rect"]["y"] < b["rect"]["y"] + b["rect"]["h"]
             and b["rect"]["y"] < a["rect"]["y"] + a["rect"]["h"]]
    check("图例 rail：卡2 的 4 条 chip 两两不相交", not cross, str(cross))
    check("图例 rail 在舞台右侧（x = 舞台宽+6）",
          bool(chips) and all(abs(c["rect"]["x"] - (g["stage"]["w"] + 6)) < 0.5 for c in chips),
          str(chips[:1]))
    by_row = {r["行"]: r["rect"] for r in g["boxRects"] if r["card"] == 2}
    link_ok = all(close(l["x2"], by_row[l["行"]]["x"] + by_row[l["行"]]["w"], 0.5)
                  and close(l["y2"], by_row[l["行"]]["y"] + by_row[l["行"]]["h"] / 2, 0.5)
                  for l in g["legend"]["links"])
    check("引线端点 = 该行框右缘中点（同格多框引线汇聚）", link_ok, str(g["legend"]["links"]))


# ---------------------------------------------------------------- [3] 三态
def probe_states(by_no: dict[int, dict] | None, payload: dict | None,
                 runner_text: str | None) -> None:
    print("[3] 三态诚实：precise / no-own-box / unlocated / legacy 逐字落地（反 D3）")
    if not (payload and runner_text):
        check("前置（桥面 payload / 几何 runner）缺失，[3] 跳过", False, "")
        return
    g = json.loads(runner_text)
    check("#1-#6 frameState=precise、#7=no-own-box",
          all(g["states"][str(n)] == "precise" for n in (1, 2, 3, 4, 5, 6))
          and g["states"]["7"] == "no-own-box", str(g["states"]))
    check("精确态帧标注逐字 = 「第1镜 · 1.01s · AI粗标」（不含旧「九宫格第N格」形态）",
          g["cap2"]["main"] == "第1镜 · 1.01s · AI粗标" and "九宫格" not in g["cap2"]["main"],
          str(g["cap2"]))
    check("精确态 honest 为空、hint=点图放大",
          g["cap2"]["honest"] == "" and g["cap2"]["hint"] == "点图放大", str(g["cap2"]))
    check("#7 无自身位置：honest 逐字 = 「未能标注位置」", g["cap7"]["honest"] == "未能标注位置",
          str(g["cap7"]))
    unlocated = g["capUnlocated"]
    check("未定位文案逐字 = 「未能定位到该资产出现的镜头 · 暂用整片画面」（不含镜次秒）",
          unlocated["main"] == "未能定位到该资产出现的镜头 · 暂用整片画面"
          and "镜" not in unlocated["main"].replace("镜头", "") and unlocated["honest"] == "",
          str(unlocated))
    check("legacy 分支文案不动（九宫格第5格 · 基于第1镜 · AI粗标）",
          g["capLegacy"]["main"] == "九宫格第5格 · 基于第1镜 · AI粗标", str(g["capLegacy"]))

    # 未定位夹具（自建，用完即删）：无 拆解.md、无源视频 → 不画框不标镜次
    fixture = ROOT / "analysis" / "reverse" / f"_at10c4-fixture-{os.getpid()}"
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
        (fixture / "待确认清单.json").write_text(json.dumps(sidecar, ensure_ascii=False),
                                                 encoding="utf-8")
        built = cards.build(fixture.name, "auto-9999")
        if check("未定位夹具两行 → 两卡", len(built) == 2, f"{len(built)} 卡"):
            bad = []
            for card in built:
                rep = card.get("代表帧") or {}
                if rep.get("定位") != "未定位" or rep.get("镜次") is not None:
                    bad.append((card["n"], "代表帧", rep))
                if card.get("帧内框") != [] or card.get("自身框") is not False:
                    bad.append((card["n"], "框", card.get("帧内框"), card.get("自身框")))
            check("未定位：定位=未定位、镜次=None、帧内框=[]、自身框=False（不冒充）", not bad, str(bad[:3]))
    finally:
        shutil.rmtree(fixture, ignore_errors=True)
    check("未定位夹具目录已删", not fixture.exists(), str(fixture))


# ---------------------------------------------------------------- [4] 共享帧去重
def probe_dedup(by_no: dict[int, dict] | None, payload: dict | None) -> None:
    print("[4] 共享帧去重：同镜 URL 相等 + 抽帧每目标一次 + 重复组装不重抽 + 桥面镜像")
    if by_no is None or payload is None:
        check("前置（本地复算 / 桥面 JSON）缺失，[4] 跳过", False, "")
        return
    urls = {no: (c.get("代表帧") or {}).get("url") or "" for no, c in by_no.items()}
    g1, g3 = {urls[n] for n in (2, 3, 4, 6)}, {urls[n] for n in (1, 5, 7)}
    check("本地：镜1 四卡共享同一帧 URL、镜3 三卡共享同一帧 URL、两组不同",
          len(g1) == 1 and len(g3) == 1 and g1 != g3 and "" not in g1 | g3,
          f"镜1 {g1}｜镜3 {g3}")

    # 抽帧入口每目标一次 + 重复组装 0 真抽
    ensure_calls: list[str] = []
    ffmpeg_calls: list[str] = []
    original_ensure, original_ffmpeg = frames._ensure, frames._ffmpeg_frame

    def spy_ensure(video, at, out):
        ensure_calls.append(Path(out).name)
        return original_ensure(video, at, out)

    def spy_ffmpeg(video, at, out):
        ffmpeg_calls.append(Path(out).name)
        return original_ffmpeg(video, at, out)

    frames._ensure, frames._ffmpeg_frame = spy_ensure, spy_ffmpeg
    try:
        cards.build(BRIDGE, LOCAL)
        first_ensure = list(ensure_calls)
        first_ffmpeg = list(ffmpeg_calls)
        cards.build(BRIDGE, LOCAL)
    finally:
        frames._ensure, frames._ffmpeg_frame = original_ensure, original_ffmpeg
    shot_targets = [n for n in first_ensure
                    if n.startswith(frames.SHOT_PREFIX) or n.startswith(frames.FALLBACK_PREFIX)]
    check("抽帧入口每目标文件至多 1 次（组装级共享）",
          len(shot_targets) == len(set(shot_targets)), str(shot_targets))
    check("重复组装不再抽帧（第二次 build 真 ffmpeg 0 次）",
          not ffmpeg_calls[len(first_ffmpeg):], f"第二次 {ffmpeg_calls[len(first_ffmpeg):]}")

    # 桥面镜像
    cs = (payload.get("task") or {}).get("asset_cards") or []
    hurls = {c["n"]: (c.get("代表帧") or {}).get("url") or "" for c in cs}
    h1, h3 = {hurls[n] for n in (2, 3, 4, 6)}, {hurls[n] for n in (1, 5, 7)}
    check("桥面：同镜行代表帧 URL 相等且两组不同",
          len(h1) == 1 and len(h3) == 1 and h1 != h3, f"镜1 {h1}｜镜3 {h3}")
    check("桥面 URL 与本地复算一致", hurls == urls, str([(n, hurls[n], urls[n]) for n in hurls
                                                         if hurls[n] != urls.get(n)]))
    bad = []
    for no, url in hurls.items():
        s, b = req(url)
        if s != 200 or not isinstance(b, bytes) or not b.startswith(b"\x89PNG"):
            bad.append((no, url, s))
    check("每张代表帧 URL GET 200 且 PNG 字节", not bad, str(bad[:3]))


# ---------------------------------------------------------------- 主流程
def probe_local() -> dict[int, dict] | None:
    built = cards.build(BRIDGE, LOCAL)
    if not check("本地复算 asset_cards 7 行", len(built) == 7, f"{len(built)} 行"):
        return None
    return {card["n"]: card for card in built}


def probe_http() -> dict | None:
    status, body = req(f"/api/hub/task/{LOCAL}")
    if not check(f"GET /api/hub/task/{LOCAL} → 200", status == 200 and isinstance(body, dict),
                 f"{status} {str(body)[:120]}"):
        return None
    return body


def main() -> int:
    print(f"[probe-at10] 目标 {BASE}；样本 {BRIDGE}/{LOCAL}（只读）")
    if not POP4.is_dir():
        check("pop-4 baseline 在场", False, str(POP4))
        return 1
    by_no = probe_local()
    payload = probe_http()
    probe_frame_matches_shot(by_no)
    runner_text = None
    bundle = _esbuild_bundle(REPO / "web" / "src" / "lib" / "hub" / "frame-overlay.ts", "ovl")
    if bundle is not None:
        runner_src = """
import { readFileSync, writeFileSync } from 'node:fs';
const mod = await import(process.argv[2]);
const input = JSON.parse(readFileSync(process.argv[3], 'utf-8'));
const out = {};
const stage = mod.stageSize(input.ratio);
out.stage = stage;
out.content = mod.containRect(input.ratio, stage);
const boxRects = [];
const legendItems = [];
for (const card of input.boxes) {
  for (const b of (card.帧内框 || [])) {
    const r = mod.boxRect(b, out.content);
    boxRects.push({ card: card.n, 行: b.行, rect: r, badge: mod.badgeFits(r) });
    if (card.n === 2) legendItems.push({ 行: b.行, box: r });
  }
}
out.boxRects = boxRects;
out.legend = mod.layoutLegend(legendItems, stage);
out.states = Object.fromEntries(input.boxes.map(c => [c.n, mod.frameState(c)]));
out.cap2 = mod.frameCaption(input.boxes.find(c => c.n === 2));
out.cap7 = mod.frameCaption(input.boxes.find(c => c.n === 7));
out.capUnlocated = mod.frameCaption({ 代表帧: { url: '', 定位: '未定位' } });
out.capLegacy = mod.frameCaption({ 帧: [{ url: 'x.png' }], 画面位置: { 格: 5, 镜次: 1 } }, 'legacy');
writeFileSync(process.argv[process.argv.length - 1], JSON.stringify(out), 'utf-8');
console.log('ok');
"""
        input_path = bundle.parent / "in.json"
        input_path.write_text(json.dumps({"ratio": RATIO, "boxes": payload["task"]["asset_cards"]}),
                              encoding="utf-8")
        ok_run, text, err = _node_json_runner(bundle, runner_src, [str(input_path)])
        if not check("几何 runner 跑通（三态/文案/图例计算）", ok_run and bool(text), err):
            text = None
        runner_text = text
        shutil.rmtree(bundle.parent, ignore_errors=True)
    else:
        check("frame-overlay.ts esbuild 打包成功", False, "esbuild 缺席")
    probe_boxes(payload, runner_text)
    probe_states(by_no, payload, runner_text)
    probe_dedup(by_no, payload)
    fails = [r for r in RESULTS if not r[1]]
    print(f"\n[probe-at10] {len(RESULTS) - len(fails)}/{len(RESULTS)} PASS")
    for name, _ok, detail in fails:
        print(f"  FAIL 汇总: {name} —— {detail}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
