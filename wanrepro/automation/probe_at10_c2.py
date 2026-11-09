#!/usr/bin/env python
"""AT-10 C2 探针：帧内框坐标生成 + asset_cards 扩展 + hub-api.ts 归一透传字段保真。

跑法：wanrepro/ 下 `python -B automation/probe_at10_c2.py [port]`（默认 8776 常驻工作台；
判据全对才 exit 0，任何一步失败如实 FAIL 并 exit 1——判 returncode，禁子串判通过）。

四组判据（docs/nodes/AT-10.md §三 C2 行 + 数据合同 C0 定稿）：
  [1] 本地复算 cards.build(pop-4, auto-0012)：帧内框集合按镜身份统一（镜1={2,3,4,6}、镜3={1,5}）、
      #7 无自身位置但保留同镜他人框且本卡全 False、逐卡本卡标记唯一、九宫格坐标矩阵、
      短名/角标同源、不变式「自身框 == 帧内框有本卡框」。
  [2] 未定位不画框（自建夹具，用完即删）：无拆解.md、无源视频 → 代表帧.定位=未定位、
      镜次=None、帧内框=[]、自身框=False。
  [3] 真机桥面（8776）：GET /api/hub/task/auto-0012 透出 帧内框，桥面框集合与本地复算一致
      （要求桥已重启到本批代码，未重启时 [3][4] FAIL 属预期）。
  [4] 字段保真（本卡硬判据）：[3] 的 JSON → esbuild 打包 hub-api.ts → normalizeAssetCards
      → 三个新字段逐卡深比较，与后端原文完全一致（禁丢键、禁改值）。
探针只读真数据（pop-4/auto-0012），不写清单、不动任务；临时文件用完即删。
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
BRIDGE = "pop-4"
LOCAL = "auto-0012"
BOX_KEYS = {"行", "短名", "角标", "格", "x%", "y%", "w%", "h%", "本卡"}
SHOT_OF = {1: 3, 2: 1, 3: 1, 4: 1, 5: 3, 6: 1, 7: 3}   # 代表镜（C1 baseline v2）

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


# ---------------------------------------------------------------- [1] 本地复算
def probe_local() -> dict[int, dict] | None:
    print(f"[1] 本地复算 cards.build({BRIDGE}, {LOCAL})：帧内框生成（只读真数据）")
    built = cards.build(BRIDGE, LOCAL)
    if not check("asset_cards 7 行", len(built) == 7, f"{len(built)} 行"):
        return None
    by_no = {card["n"]: card for card in built}

    check("每卡 帧内框 是 list",
          all(isinstance(card.get("帧内框"), list) for card in built),
          str([(c["n"], type(c.get("帧内框")).__name__) for c in built]))
    check("每框字段集合 = {行,短名,角标,格,x%,y%,w%,h%,本卡}",
          all(set(box) == BOX_KEYS for card in built for box in card["帧内框"]),
          str(sorted(set(frozenset(box) for c in built for box in c["帧内框"]))))

    # 集合按镜身份统一：框只附着在位置键所指镜，不跨镜复制
    rows_of = {no: {box["行"] for box in by_no[no]["帧内框"]} for no in by_no}
    shot1_ok = all(rows_of[no] == {2, 3, 4, 6} for no in (2, 3, 4, 6))
    shot3_ok = all(rows_of[no] == {1, 5} for no in (1, 5))
    check("镜1 帧的卡（#2/#3/#4/#6）帧内框行集合 == {2,3,4,6}", shot1_ok, f"{rows_of}")
    check("镜3 帧的卡（#1/#5）帧内框行集合 == {1,5}", shot3_ok, f"{rows_of}")
    check("#7 帧内框 == {1,5} 且 本卡 全 False（无自身位置但保留他人框）",
          rows_of[7] == {1, 5} and all(box["本卡"] is False for box in by_no[7]["帧内框"]),
          f"{[(b['行'], b['本卡']) for b in by_no[7]['帧内框']]}")

    # 一帧多框（集合 >1 个框）
    check("一帧多框：镜1 集合 4 框、镜3 集合 2 框",
          len(by_no[2]["帧内框"]) == 4 and len(by_no[1]["帧内框"]) == 2,
          f"镜1 {len(by_no[2]['帧内框'])}｜镜3 {len(by_no[1]['帧内框'])}")

    # 同帧多卡框集合一致（除 本卡）
    def strip_本卡(boxes: list[dict]) -> list[dict]:
        return [{k: v for k, v in box.items() if k != "本卡"} for box in boxes]

    base1 = strip_本卡(by_no[2]["帧内框"])
    base3 = strip_本卡(by_no[1]["帧内框"])
    check("同帧多卡去掉本卡后完全相等：镜1 四张卡",
          all(strip_本卡(by_no[no]["帧内框"]) == base1 for no in (3, 4, 6)),
          f"{[strip_本卡(by_no[no]['帧内框']) for no in (3, 4, 6)]}")
    check("同帧多卡去掉本卡后完全相等：镜3 两张卡", strip_本卡(by_no[5]["帧内框"]) == base3,
          f"{strip_本卡(by_no[5]['帧内框'])}")

    # 逐卡本卡标记唯一且正确
    def own_rows(no: int) -> list[int]:
        return [box["行"] for box in by_no[no]["帧内框"] if box["本卡"]]

    check("本卡标记逐卡正确：#2→行2、#4→行4、#1→行1、#5→行5（各自唯一）",
          own_rows(2) == [2] and own_rows(4) == [4] and own_rows(1) == [1] and own_rows(5) == [5],
          f"#2{own_rows(2)} #4{own_rows(4)} #1{own_rows(1)} #5{own_rows(5)}")

    # 九宫格坐标矩阵（1=左上…9=右下，行主序；坐标系=contain 后内容盒 0~100%）
    g5 = next(box for box in by_no[2]["帧内框"] if box["行"] == 2)
    g8 = next(box for box in by_no[1]["帧内框"] if box["行"] == 1)
    check("格5 → (33.33, 33.33, 33.33, 33.33)",
          (g5["x%"], g5["y%"], g5["w%"], g5["h%"]) == (33.33, 33.33, 33.33, 33.33),
          str((g5["x%"], g5["y%"], g5["w%"], g5["h%"])))
    check("格8 → (33.33, 66.67, 33.33, 33.33)",
          (g8["x%"], g8["y%"], g8["w%"], g8["h%"]) == (33.33, 66.67, 33.33, 33.33),
          str((g8["x%"], g8["y%"], g8["w%"], g8["h%"])))
    off = [box for card in built for box in card["帧内框"]
           if box["x%"] + box["w%"] > 100.0 or box["y%"] + box["h%"] > 100.0
           or box["w%"] <= 0 or box["h%"] <= 0]
    check("每框 x%+w%<=100 且 y%+h%<=100 且 w%,h%>0", not off, str(off[:3]))

    # 短名非空；角标 = 该清单行「建议」列原文（与 AT-8 卡片角标同源）
    md_path = replace.checklist_path(BRIDGE)
    advice = {row["no"]: row.get("advice") or "" for row in replace.parse_rows(
        md_path.read_text(encoding="utf-8-sig", errors="replace"))}
    bad_badge = [(card["n"], box["行"], box["角标"], advice.get(box["行"]))
                 for card in built for box in card["帧内框"]
                 if box["角标"] != advice.get(box["行"])]
    check("角标 == 该行建议列原文（禁新增识别调用）", not bad_badge, str(bad_badge[:3]))
    check("每框短名非空",
          all(box["短名"] for card in built for box in card["帧内框"]),
          str([(box["行"], box["短名"]) for c in built for box in c["帧内框"] if not box["短名"]]))

    # 不变式：自身框 == 帧内框里存在本卡框；pop-4 自身框图 #1-#6 True / #7 False
    invariant = all(card["自身框"] == any(box["本卡"] for box in card["帧内框"]) for card in built)
    check("不变式：每卡 自身框 == (帧内框 存在 本卡=True)", invariant, "")
    own = {no: by_no[no]["自身框"] for no in by_no}
    check("pop-4 自身框图 = #1-#6 True / #7 False",
          own == {1: True, 2: True, 3: True, 4: True, 5: True, 6: True, 7: False}, f"{own}")
    return by_no


# ---------------------------------------------------------------- [2] 未定位夹具
def probe_not_located() -> None:
    print("[2] 未定位不画框（自建夹具：无 拆解.md、无源视频；用完即删）")
    fixture = ROOT / "analysis" / "reverse" / f"_at10c2-fixture-{os.getpid()}"
    fixture.mkdir(parents=True, exist_ok=True)
    try:
        # 最小清单：表头 7 列 + 两行资产，建议列 必须换 / 可沿用（角标同源口径）
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
        built = cards.build(fixture.name, "auto-9999")
        ok_count = check("夹具两行 → 两卡", len(built) == 2, f"{len(built)} 卡")
        if ok_count:
            for card in built:
                rep = card.get("代表帧") or {}
                no = card["n"]
                check(f"#{no} 代表帧.定位=未定位、镜次=None（不冒充）",
                      rep.get("定位") == "未定位" and rep.get("镜次") is None, str(rep))
                check(f"#{no} 帧内框 == []（未定位不画框）", card.get("帧内框") == [],
                      str(card.get("帧内框")))
                check(f"#{no} 自身框=False", card.get("自身框") is False, str(card.get("自身框")))
    finally:
        shutil.rmtree(fixture, ignore_errors=True)     # 用完即删不留垃圾
    check("夹具目录已删", not fixture.exists(), str(fixture))


# ---------------------------------------------------------------- [3] 真机桥面
def probe_http(by_no: dict[int, dict] | None) -> dict | None:
    print(f"[3] 真机桥面 {BASE}：GET /api/hub/task/{LOCAL} → 帧内框透传（要求桥已重启到本批代码）")
    status, body = req(f"/api/hub/task/{LOCAL}")
    if not check(f"GET /api/hub/task/{LOCAL} → 200", status == 200 and isinstance(body, dict),
                 f"{status} {str(body)[:120]}"):
        return None
    cs = (body.get("task") or {}).get("asset_cards") or []
    if not check("桥面 asset_cards 7 行", len(cs) == 7, f"{len(cs)} 行"):
        return None
    check("桥面每卡带 帧内框 数组",
          all(isinstance(c.get("帧内框"), list) for c in cs),
          str([(c["n"], type(c.get("帧内框")).__name__) for c in cs]))
    http_by_no = {card["n"]: card for card in cs}
    rows_of = {no: {box["行"] for box in http_by_no[no].get("帧内框") or []} for no in http_by_no}
    check("镜像断言：#2 帧内框行集合 {2,3,4,6}", rows_of.get(2) == {2, 3, 4, 6}, f"{rows_of.get(2)}")
    check("镜像断言：#7 集合 {1,5} 且 本卡 全 False",
          rows_of.get(7) == {1, 5}
          and all(box.get("本卡") is False for box in http_by_no[7].get("帧内框") or []),
          f"{[(b.get('行'), b.get('本卡')) for b in http_by_no[7].get('帧内框') or []]}")
    if by_no:
        def norm(boxes: list[dict]) -> list[dict]:
            return sorted((tuple(sorted(box.items())) for box in boxes))

        mismatch = [no for no in by_no
                    if norm(http_by_no[no].get("帧内框") or []) != norm(by_no[no].get("帧内框") or [])]
        check("桥面框集合与本地复算一致（逐卡逐框）", not mismatch, f"不一致行 {mismatch}")
    return body


# ---------------------------------------------------------------- [4] 字段保真（esbuild → normalize）
def probe_fidelity(payload: dict | None) -> None:
    print("[4] 真实后端包 → esbuild 打包 hub-api.ts → normalizeAssetCards → 深比较（字段保真）")
    if payload is None:
        check("[3] 未取到桥面 JSON，[4] 跳过（前置缺失）", False, "依赖 [3] 的 payload")
        return
    esbuild_cmd = REPO / "web" / "node_modules" / ".bin" / "esbuild.cmd"   # Windows：.cmd 直接 exec
    esbuild_sh = REPO / "web" / "node_modules" / ".bin" / "esbuild"        # POSIX：.bin 下的 sh 脚本
    esbuild_js = REPO / "web" / "node_modules" / "esbuild" / "bin" / "esbuild"   # 包入口 JS，兜底用 node 跑
    if esbuild_cmd.is_file():
        esbuild_argv = [str(esbuild_cmd)]              # .cmd 由 shell 解析，不能喂给 node
    elif esbuild_sh.is_file():
        esbuild_argv = [str(esbuild_sh)]
    else:
        esbuild_argv = ["node", str(esbuild_js)]
    if not (esbuild_cmd.is_file() or esbuild_sh.is_file() or esbuild_js.is_file()):
        check("esbuild 可执行文件在场", False, f"{esbuild_js}")
        return
    tmp = Path(tempfile.mkdtemp(prefix="at10c2-fidelity-"))
    try:
        payload_path, bundle_path = tmp / "payload.json", tmp / "hub-api.bundle.mjs"
        out_path, runner_path = tmp / "normalized.json", tmp / "runner.mjs"
        payload_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        bundle = subprocess.run(
            esbuild_argv + [str(REPO / "web" / "src" / "pages" / "hub" / "hub-api.ts"),
                            "--bundle", "--format=esm", f"--outfile={bundle_path}"],
            capture_output=True, text=True, timeout=120)
        if not check("esbuild 打包 hub-api.ts 成功", bundle.returncode == 0,
                     (bundle.stderr or bundle.stdout)[-300:]):
            return
        # runner：动态 import bundle（file:/// 原生路径，MSYS 路径 node 不认），归一后落盘
        runner_path.write_text(
            "import { readFileSync, writeFileSync } from 'node:fs';\n"
            "const mod = await import(process.argv[2]);   // 动态 import（file:/// 原生路径）\n"
            "const payload = JSON.parse(readFileSync(process.argv[3], 'utf-8'));\n"
            "const cards = mod.normalizeAssetCards((payload.task || {}).asset_cards);\n"
            "writeFileSync(process.argv[4], JSON.stringify(cards), 'utf-8');\n"
            "console.log('cards=' + cards.length);\n",
            encoding="utf-8")
        run = subprocess.run(["node", str(runner_path), Path(bundle_path).as_uri(),
                              str(payload_path), str(out_path)],
                             capture_output=True, text=True, timeout=120)
        if not check("normalize runner 跑通", run.returncode == 0, (run.stderr or run.stdout)[-300:]):
            return
        normalized = json.loads(out_path.read_text(encoding="utf-8"))
        backend = {card["n"]: card for card in (payload.get("task") or {}).get("asset_cards") or []}
        if not check("归一输出 7 卡", len(normalized) == 7, f"{len(normalized)} 卡"):
            return
        # 逐卡深比较三个新字段：禁丢键、禁改值
        bad = []
        for card in normalized:
            src = backend[card["n"]]
            rep_s, rep_n = src.get("代表帧") or {}, card.get("代表帧") or {}
            for key in ("url", "秒", "镜次", "定位"):
                if rep_n.get(key) != rep_s.get(key):
                    bad.append((card["n"], "代表帧." + key, rep_s.get(key), rep_n.get(key)))
            if card.get("自身框") != src.get("自身框"):
                bad.append((card["n"], "自身框", src.get("自身框"), card.get("自身框")))
            boxes_s, boxes_n = src.get("帧内框") or [], card.get("帧内框") or []
            if len(boxes_s) != len(boxes_n):
                bad.append((card["n"], "帧内框框数", len(boxes_s), len(boxes_n)))
                continue
            for bs, bn in zip(boxes_s, boxes_n):
                for key in sorted(BOX_KEYS):
                    if bn.get(key) != bs.get(key):
                        bad.append((card["n"], f"帧内框[{bs.get('行')}].{key}", bs.get(key), bn.get(key)))
        check("三个新字段逐卡深比较与后端原文完全一致（禁丢键禁改值）", not bad, str(bad[:5]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)         # 临时文件用完即删


def main() -> int:
    print(f"[probe-at10-c2] 目标 {BASE}；样本 {BRIDGE}/{LOCAL}（只读）")
    by_no = probe_local()
    probe_not_located()
    payload = probe_http(by_no)
    probe_fidelity(payload)
    fails = [r for r in RESULTS if not r[1]]
    print(f"\n[probe-at10-c2] {len(RESULTS) - len(fails)}/{len(RESULTS)} PASS")
    for name, _ok, detail in fails:
        print(f"  FAIL 汇总: {name} —— {detail}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
