#!/usr/bin/env python
"""AT-8 B2 探针：frame 映射 + asset_cards 视觉包 + 写路径（POST /api/hub/asset）。

跑法：`python -m automation.probe_hub_at8 [port]`（默认 8776 常驻 bridge）。
判据全对才 exit 0；任一条失败如实 FAIL 并 exit 1（禁子串判通过）。

真跑口径（都是真 HTTP + 真 ffmpeg + 真写 md）：
  · auto-0002（bridge_id=pop-3--7，带 B1 sidecar）12 张卡；#9 取帧秒∈[10,11]；每张帧 URL GET 200
  · 核心卡集合按合同 v3 公式实算；场景类 0 张进主列表
  · POST asset n=9 贴图 → 清单 md 第 9 行资产图变更、第 2 行一字节不动（md + get_task 回显双查）
  · POST asset n=9 clear → md/回显都为空
  · POST asset n=12 text → 写清单「命名」列
  · 无 sidecar 的旧任务 auto-0001（bridge_id=1-35）→ 整片中段帧 + 「未精确定位」，不炸
探针跑完把清单 md 与 任务.json 原文写回（字节级还原），不给现场留垃圾。
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from . import cards, config, frames, replace  # noqa: E402

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8776
BASE = f"http://127.0.0.1:{PORT}"
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 红线：本机直连不走代理
ROOT = Path(__file__).resolve().parents[1]
ANALYSIS = ROOT / "analysis" / "reverse"

FIELDS = ("n", "类别", "短名", "建议", "核心", "出现时段", "帧", "已传图", "描述", "同实体疑似")
TASK_ID = "auto-0002"
BRIDGE = "pop-3--7"
OLD_TASK = "auto-0001"
OLD_BRIDGE = "1-35"
PROBE_URL = "https://h.uguu.se/at8-probe-n9.png"

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" —— {detail}" if detail and not cond else ""))


def req(path: str, method: str = "GET", payload: dict | None = None) -> tuple[int, dict | bytes]:
    data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    if data is not None:
        r.add_header("Content-Type", "application/json")
    try:
        resp = OP.open(r, timeout=180)
        raw = resp.read()
        ctype = resp.headers.get("Content-Type") or ""
        body = json.loads(raw) if "json" in ctype else raw
        return resp.status, body
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return e.code, raw


def task_cards(task_id: str) -> list[dict]:
    status, body = req(f"/api/hub/task/{task_id}")
    if status != 200 or not isinstance(body, dict):
        return []
    return ((body.get("task") or {}).get("asset_cards") or [])


def md_row(md_text: str, no: int) -> dict:
    for row in replace.parse_rows(md_text):
        if row["no"] == no:
            return row
    return {}


def clock(text: str) -> float:
    """MM:SS → 秒（与 frames.clock_seconds 同一解析，供区间断言）。"""
    return float(frames.clock_seconds(text) or 0.0)


def raw_line(md_text: str, no: int) -> str:
    """清单里第 no 行的原始文本（字节级比对用，绕开任何列解析）。"""
    prefix = f"| {no} |"
    for line in md_text.splitlines():
        if line.replace("｜", "|").startswith(prefix):
            return line
    return ""


def main() -> int:
    print(f"[probe-hub-at8] 目标 {BASE}")
    md_path = ANALYSIS / BRIDGE / "待确认清单.md"
    task_file = config.PROJECTS_DIR / TASK_ID / "任务.json"
    if not md_path.is_file() or not task_file.is_file():
        check(f"样本 {TASK_ID}/{BRIDGE} 的清单与任务文件存在", False,
              f"{md_path} / {task_file}")
        return 1
    md_before = md_path.read_bytes()
    task_before = task_file.read_bytes()

    try:
        # ── 1. 视觉包结构 ────────────────────────────────────────
        print("1) asset_cards 结构（auto-0002 / pop-3--7）")
        cs = task_cards(TASK_ID)
        check("asset_cards 是数组且 12 行全在", isinstance(cs, list) and len(cs) == 12, f"{len(cs)} 行")
        check("按 n 升序且 n=1..12", [c.get("n") for c in cs] == list(range(1, 13)),
              str([c.get("n") for c in cs]))
        missing = [f"{c.get('n')}:{k}" for c in cs for k in FIELDS if k not in c]
        check("合同 v3 字段齐全（n/类别/短名/建议/核心/出现时段/帧/已传图/描述/同实体疑似）",
              not missing, "缺 " + ", ".join(missing[:6]))
        frames_ok = all(isinstance(c.get("帧"), list) and c["帧"] for c in cs)
        check("每张卡至少 1 帧", frames_ok,
              str([c.get("n") for c in cs if not c.get("帧")]))
        check("帧数与出现时段等长（时段空 = 1 张兜底帧）",
              all(len(c["帧"]) == max(1, len(c["出现时段"])) for c in cs),
              str([(c["n"], len(c["帧"]), len(c["出现时段"])) for c in cs
                   if len(c["帧"]) != max(1, len(c["出现时段"]))]))
        check("帧对象三字段齐全（url/秒/来源）",
              all({"url", "秒", "来源"} <= set(f) for c in cs for f in c["帧"]),
              str([(c["n"], f) for c in cs for f in c["帧"] if not {"url", "秒", "来源"} <= set(f)][:2]))

        # ── 2. 取帧秒落点 ───────────────────────────────────────
        print("2) 取帧落点（#9 包装盒卡：机器断言）")
        card9 = next(c for c in cs if c["n"] == 9)
        check("#9 出现时段 = 00:10-00:12", card9["出现时段"] == [{"起": "00:10", "止": "00:12"}],
              str(card9["出现时段"]))
        sec9 = float(card9["帧"][0]["秒"])
        check("#9 取帧秒 ∈ [10,11]", 10.0 <= sec9 <= 11.0, f"秒={sec9}")
        check("#9 取帧来源 ∈ {现抽,缓存}", card9["帧"][0]["来源"] in ("缓存", "现抽"),
              card9["帧"][0]["来源"])
        outside = []
        for c in cs:
            for span, frame in zip(c["出现时段"], c["帧"]):
                start, end = clock(span["起"]), clock(span["止"])
                if not (start - 1e-9 <= float(frame["秒"]) <= end + 1e-9):
                    outside.append((c["n"], frame["秒"], span))
        check("每个取帧秒都落在该资产出现时段内", not outside, str(outside[:3]))

        # ── 3. 帧 URL 全量 GET ──────────────────────────────────
        print("3) 帧 URL GET")
        bad = []
        for c in cs:
            for frame in c["帧"]:
                status, body = req(frame["url"])
                if status != 200 or not isinstance(body, bytes) or not body.startswith(b"\x89PNG"):
                    bad.append((c["n"], frame["url"], status, len(body) if isinstance(body, bytes) else body))
        check("每张帧 URL GET 200 且是 PNG 字节", not bad, str(bad[:3]))

        # ── 4. 核心过滤 ─────────────────────────────────────────
        print("4) 核心过滤 / 场景排除")
        core = {c["n"] for c in cs if c["核心"]}
        expected = {c["n"] for c in cs if cards.is_core(c["类别"], c["建议"])}
        check("核心 = 合同 v3 公式实算集合", core == expected, f"{sorted(core)} != {sorted(expected)}")
        check("场景类 0 张进主列表",
              not any(next(c for c in cs if c["n"] == n)["类别"] == "场景" for n in core),
              str(sorted(core)))
        must = {c["n"] for c in cs if c["建议"] in ("必须换", "建议准备")}
        check("必须换/建议准备 4 行全在核心（#2/#7/#9/#12）", must <= core and must == {2, 7, 9, 12},
              f"must={sorted(must)} core={sorted(core)}")
        check("核心实算 4 张 = {2,7,9,12}（主体「可沿用」#1 归次要，不进核心）",
              len(core) == 4 and core == {2, 7, 9, 12}, f"核心 {len(core)} 张 {sorted(core)}")
        print(f"  [info] 核心卡 {len(core)} 张 = {sorted(core)}（主体「可沿用」#1 不进核心"
              f"——主列表只放要动手的行）")

        # ── 5. 已传图真源 = 清单 md ─────────────────────────────
        print("5) 已传图真源（清单 md，不是 pending_replace）")
        md0 = md_path.read_text(encoding="utf-8")
        row2 = md_row(md0, 2)
        card2 = next(c for c in cs if c["n"] == 2)
        check("#2 已传图 == 清单 md 资产图列", card2["已传图"] == cards.split_urls(row2["cur_assets"]),
              f"card={card2['已传图']} md={row2['cur_assets']!r}")
        check("#2 已传图非空（历史 uguu 图）", bool(card2["已传图"]), str(card2["已传图"]))
        check("#9 初始已传图为空（清单该行未填）", next(c for c in cs if c["n"] == 9)["已传图"] == [],
              str(next(c for c in cs if c["n"] == 9)["已传图"]))
        twin2, twin7 = card2["短名"], next(c for c in cs if c["n"] == 7)["短名"]
        check("同实体 #2/#7 短名可区分且带对方类别", twin2 != twin7 and "道具" in twin2 and "主体" in twin7,
              f"{twin2} / {twin7}")

        # ── 6. 写路径 ───────────────────────────────────────────
        print("6) 写路径 POST /api/hub/asset")
        status, body = req("/api/hub/asset", "POST",
                           {"task_id": TASK_ID, "n": 9, "image_url": PROBE_URL})
        check("贴图 → 200 且回显该 URL", status == 200 and body.get("已传图") == [PROBE_URL],
              f"{status} {body}")
        md1 = md_path.read_text(encoding="utf-8")
        row9_new = md_row(md1, 9)
        check("清单 md 第 9 行资产图已变更为该 URL", row9_new.get("cur_assets") == PROBE_URL,
              repr(row9_new.get("cur_assets")))
        check("清单 md 第 2 行整行一字不动", raw_line(md1, 2) == raw_line(md0, 2), "")
        after = task_cards(TASK_ID)
        check("get_task 回显 #9 已传图 == [URL]",
              next(c for c in after if c["n"] == 9)["已传图"] == [PROBE_URL],
              str(next(c for c in after if c["n"] == 9)["已传图"]))
        status, body = req("/api/hub/asset", "POST",
                           {"task_id": TASK_ID, "n": 9, "image_url": PROBE_URL})
        check("重传覆盖不追加（再贴一次仍是 1 张）",
              status == 200 and body.get("已传图") == [PROBE_URL], str(body))

        status, body = req("/api/hub/asset", "POST", {"task_id": TASK_ID, "n": 9, "clear": True})
        check("clear → 200 且回显空", status == 200 and body.get("已传图") == [], f"{status} {body}")
        check("清单 md 第 9 行资产图已清空",
              md_row(md_path.read_text(encoding="utf-8"), 9).get("cur_assets") == "",
              repr(md_row(md_path.read_text(encoding="utf-8"), 9).get("cur_assets")))
        check("get_task 回显 #9 已传图为空",
              next(c for c in task_cards(TASK_ID) if c["n"] == 9)["已传图"] == [], "")

        status, body = req("/api/hub/asset", "POST", {"task_id": TASK_ID, "n": 12, "text": "探针品牌字样"})
        check("text → 200 且回显命名", status == 200 and body.get("命名") == "探针品牌字样",
              f"{status} {body}")
        check("清单 md 第 12 行「命名」列写入",
              md_row(md_path.read_text(encoding="utf-8"), 12).get("name") == "探针品牌字样",
              repr(md_row(md_path.read_text(encoding="utf-8"), 12).get("name")))
        status, _ = req("/api/hub/asset", "POST", {"task_id": TASK_ID, "n": 99, "image_url": PROBE_URL})
        check("不存在的行号 → 409", status == 409, str(status))
        status, _ = req("/api/hub/asset", "POST", {"task_id": TASK_ID, "n": 9, "image_url": "http://x/y.png"})
        check("非 HTTPS image_url → 409", status == 409, str(status))
        status, _ = req("/api/hub/asset", "POST", {"task_id": TASK_ID, "n": 9})
        check("空请求体 → 409", status == 409, str(status))
    finally:
        md_path.write_bytes(md_before)
        task_file.write_bytes(task_before)
        print("  [info] 清单 md 与 任务.json 已按字节还原")

    # ── 7. 旧任务兜底 ──────────────────────────────────────────
    print("7) 无 sidecar 旧任务（auto-0001 / 1-35）")
    check("1-35 确实没有出现时段侧车", not (ANALYSIS / OLD_BRIDGE / "待确认清单.json").is_file(), "")
    old = task_cards(OLD_TASK)
    check("旧任务 asset_cards 非空且不炸", bool(old), f"{len(old)} 行")
    check("旧任务出现时段全为空（走兜底）", all(c["出现时段"] == [] for c in old),
          str([c["n"] for c in old if c["出现时段"]]))
    check("旧任务帧来源 = 中段兜底、定位 = 未精确定位",
          all(f["来源"] == "中段兜底" and f["定位"] == "未精确定位" for c in old for f in c["帧"]),
          str([(c["n"], f["来源"], f["定位"]) for c in old for f in c["帧"]
               if f["来源"] != "中段兜底"][:3]))
    check("旧任务卡级定位标注 = 未精确定位", all(c.get("定位") == "未精确定位" for c in old),
          str([c.get("定位") for c in old]))
    bad_old = []
    for c in old:
        for frame in c["帧"]:
            status, body = req(frame["url"])
            if status != 200 or not isinstance(body, bytes) or not body.startswith(b"\x89PNG"):
                bad_old.append((c["n"], frame["url"], status))
    check("旧任务帧 URL GET 200", not bad_old, str(bad_old[:3]))

    # ── 8. 还原校验 + 帧映射结果表 ─────────────────────────────
    print("8) 现场还原与帧映射结果表")
    check("清单 md 已字节级还原", md_path.read_bytes() == md_before, "")
    check("任务.json 已字节级还原（pending_replace 未留探针流水）",
          task_file.read_bytes() == task_before, "")
    print("   n  类别          短名            建议      核心  定位          帧(秒/来源)")
    for c in task_cards(TASK_ID):
        shots = "  ".join(f"{f['秒']:g}/{f['来源']}" for f in c["帧"])
        print(f"  {c['n']:>2}  {c['类别']:<12}  {c['短名']:<14}  {c['建议']:<8}  "
              f"{'是' if c['核心'] else '否':<3}  {c['定位']:<8}  {shots}")

    fails = [r for r in RESULTS if not r[1]]
    print(f"\n[probe-hub-at8] {len(RESULTS) - len(fails)}/{len(RESULTS)} PASS")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
