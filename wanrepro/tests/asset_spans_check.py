"""出现时段解析（AT-8 B1）：清单行 → 镜号 → 分镜表起止 → MM:SS 时段数组。

守住五件事：
1. 九 资产「出现于：镜 N-M」能换算成时段数组，多时段全收集（首尾相接才合并）；
2. 二 产品括号时段、第十层品牌位自带时间码都能直接用（品牌位一行三段要出三段）；
3. 镜号在分镜表没有行 → 该段丢弃或整行留空，**不抛错**（B2 走兜底帧）；
4. 清单 md 表格列数不变（时段只进 `待确认清单.json`），旧清单（6 列/8 列）照样读；
5. 真实样本 pop-3--7：必须换/建议准备 4 行时段非空、总非空 ≥10/12、#9 落在镜 5。

跑法：py -3 tests/asset_spans_check.py
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]          # wanrepro/
CONFIRM = ROOT / "pipeline" / "reverse" / "confirm.py"
REAL = ROOT / "analysis" / "reverse" / "pop-3--7"
PY = sys.executable
CLOCK = re.compile(r"^\d{2}:\d{2}$")
SPANS_NAME = "待确认清单.json"

DISSECTION = """<!-- 视频: fixture.mp4 | 时长: 8.00s | 音轨: 无 -->

> 规格：720×1280 · 9:16 竖屏 · 8.00 秒 · 30.0 fps · 无音轨 · 硬切 3 次

## 一 场景
纯白影棚，无缝背景，顶部柔光。

## 二 产品
**媒介产品（00:00-00:02）**：扁平方形设备，深灰金属中框，正面为显示屏。
**核心产品（00:02-00:06）**：细长圆柱形小物体，亮面金属盖，外覆橙色标签，中段伴随长方体外盒。

## 三 运动
扁平方形设备向镜头前抛出并悬停；后段小物体垂直下落。

## 四 光影
均匀漫射冷白光。

## 五 镜头
固定机位，中景到特写。

## 六 意境
干净、克制。

## 七 分镜表
| 镜号 | 起秒-止秒 | 运动主体 | 运动形态 | 速度 | 画面内容 |
|---|---|---|---|---|---|
| 1 | 00:00-00:02 | 人物 | 转身 | 快 | 车厢 |
| 2 | 00:02-00:04 | 核心产品 | 悬浮 | 慢 | 特写 |
| 3 | 00:04-00:06 | 核心产品 | 递出 | 匀速 | 穿屏 |
| 9 | 00:06-00:08 | 核心产品 | 下落 | 中速 | 矩阵 |

## 八 声音
无对白，电子节拍。

## 九 资产
* **人物（出现于：镜 1、3）**：女性，红褐色短发，深色上衣。
* **服装A（出现于：镜 1）**：深色工装背心。
* **道具A（出现于：镜 9-10）**：长方体块状物，橙面。
* **道具B（出现于：镜 4-5）**：细长条状构件，金属亮面。
* **道具C（出现于：镜 7）**：弧形软体块，灰色。
* **道具D（出现于：全片）**：长条形旋转构件，白色。

## 十 图文与收束
* **包装字A（出现于：00:02-00:03）**：「MARK」粗体无衬线字，居中｜橙色。
* **包装字B（出现于：00:05-00:06）**：「MARK」细体字，右下｜白色。
* **收束**：类型=产品与品牌字同框；版式=产品居中；动效时序=先定格后淡入
"""

OLD_CHECKLIST_6COL = """# 待确认清单 · {video_id}

| # | 类别 | 模型看到什么 | 命名 | 资产图 | 锁什么 |
|---|------|-------------|------|--------|--------|
| 1 | 主体 | 扁平方形设备，深灰金属中框 | 旧命名A |  | 外形比例 |
| 2 | 人物 | 1 位女性，面部正对镜头 |  |  | 发型与轮廓 |
"""

results: list[tuple[bool, str]] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    results.append((bool(ok), name))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"｜{detail}" if detail else ""))


def run_confirm(task_dir: Path) -> subprocess.CompletedProcess:
    return subprocess.run([PY, str(CONFIRM), str(task_dir / "拆解.md")],
                          cwd=str(ROOT), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=120)


def load_spans(task_dir: Path) -> dict:
    payload = json.loads((task_dir / SPANS_NAME).read_text(encoding="utf-8"))
    return {row["n"]: row for row in payload["rows"]}


def span_pairs(spans: list) -> list:
    return [(span["起"], span["止"]) for span in spans]


def check_format(rows: dict, label: str) -> None:
    bad = [(row["n"], span) for row in rows.values() for span in row["出现时段"]
           if not CLOCK.match(span["起"]) or not CLOCK.match(span["止"])
           or span["起"] > span["止"]]
    check(not bad, f"{label} 时段格式合法（MM:SS 且 起≤止）", f"坏项 {bad}" if bad else "")


def section_fixture(tmp: Path) -> None:
    print("\n[1] 夹具：镜号换算 / 多时段 / 缺镜号 / 全片 / 品牌位多段")
    task = tmp / "fixture"
    task.mkdir(parents=True, exist_ok=True)
    (task / "拆解.md").write_text(DISSECTION, encoding="utf-8")
    (task / "待确认清单.md").write_text(OLD_CHECKLIST_6COL.format(video_id="fixture"),
                                       encoding="utf-8")
    proc = run_confirm(task)
    check(proc.returncode == 0, "confirm 退出码 0（旧 6 列清单在场）", proc.stderr.strip()[:200])
    if proc.returncode != 0:
        return
    check((task / SPANS_NAME).is_file(), f"写出 {SPANS_NAME}")
    rows = load_spans(task)

    def spans_of(category: str, startswith: str) -> list:
        for row in rows.values():
            if row["类别"] != category:
                continue
            # 九 资产 行按「出现于」来源标记定位，主体行按来源时间码定位
            if row["来源"].endswith(startswith) or row["来源"] == startswith:
                return row["出现时段"]
        raise AssertionError(f"找不到 {category} / {startswith}：{list(rows.values())}")

    check(span_pairs(spans_of("主体", "出现于 00:00-00:02")) == [("00:00", "00:02")],
          "二 产品括号时段直接成段", spans_of("主体", "出现于 00:00-00:02"))
    check(span_pairs(spans_of("主体", "出现于 00:02-00:06")) == [("00:02", "00:06")],
          "核心产品 00:02-00:06", spans_of("主体", "出现于 00:02-00:06"))
    check(span_pairs(spans_of("人物", "镜 1、3")) == [("00:00", "00:02"), ("00:04", "00:06")],
          "人物 镜 1、3 → 两段（不相接不合并）", spans_of("人物", "镜 1、3"))
    check(span_pairs(spans_of("服装", "镜 1")) == [("00:00", "00:02")],
          "服装A 镜 1", spans_of("服装", "镜 1"))
    check(span_pairs(spans_of("道具", "镜 9-10")) == [("00:06", "00:08")],
          "道具A 镜 9-10：镜 10 无行 → 只出镜 9，不炸", spans_of("道具", "镜 9-10"))
    check(span_pairs(spans_of("道具", "镜 4-5")) == [],
          "道具B 镜 4-5：分镜表无行 → 空数组", spans_of("道具", "镜 4-5"))
    check(span_pairs(spans_of("道具", "镜 7")) == [],
          "道具C 镜 7：分镜表无行 → 空数组", spans_of("道具", "镜 7"))
    check(span_pairs(spans_of("道具", "全片")) == [("00:00", "00:08")],
          "道具D 全片 → 首镜起末镜止", spans_of("道具", "全片"))
    check(span_pairs(spans_of("品牌视觉（置换位）", "正文时间码"))
          == [("00:02", "00:03"), ("00:05", "00:06")],
          "品牌位两段时间码全收集", spans_of("品牌视觉（置换位）", "正文时间码"))
    check_format(rows, "夹具")

    header = [line for line in (task / "待确认清单.md").read_text(encoding="utf-8").splitlines()
              if line.startswith("| # |")]
    check(bool(header) and len(header[0].strip("|").split("|")) == 8,
          "清单表头仍是 8 列（时段不进表格）", header[0] if header else "缺表头")
    check("出现时段" not in (task / "待确认清单.md").read_text(encoding="utf-8"),
          "清单正文不出现「出现时段」字样")

    # 旧清单（6 列）读取兼容 + 人工列保全 + 二次运行幂等
    proc2 = run_confirm(task)
    rows2 = load_spans(task)
    check(proc2.returncode == 0 and len(rows2) == len(rows), "二次运行行数不变（幂等）",
          f"{len(rows)} → {len(rows2)}")
    check(all(rows2[n]["出现时段"] == rows[n]["出现时段"] for n in rows), "二次运行时段不变")

    print("\n[2] 旧清单兼容：6 列（无 建议/用我的描述、无时段）")
    legacy = tmp / "legacy"
    legacy.mkdir(parents=True, exist_ok=True)
    (legacy / "拆解.md").write_text(DISSECTION, encoding="utf-8")
    (legacy / "待确认清单.md").write_text(OLD_CHECKLIST_6COL.format(video_id="legacy"),
                                         encoding="utf-8")
    sys.path.insert(0, str(CONFIRM.parent))
    import confirm as C  # noqa: E402

    parsed = C.parse_checklist(legacy / "待确认清单.md")
    check(len(parsed) == 2 and parsed[0]["建议"] == "" and parsed[1]["用我的描述"] == "",
          "parse_checklist 读 6 列旧清单不炸", parsed)
    proc3 = run_confirm(legacy)
    check(proc3.returncode == 0, "6 列旧清单重跑不炸", proc3.stderr.strip()[:200])
    kept = (legacy / "待确认清单.md").read_text(encoding="utf-8")
    check("旧命名A" in kept, "人工「命名」列在重跑后保留")

    print("\n[3] 空输入：分镜表整表无时间码 → 靠镜号的行留空，自带时间码的行不受影响")
    broken = tmp / "broken"
    broken.mkdir(parents=True, exist_ok=True)
    no_time = re.sub(r"\|\s*\d{2}:\d{2}-\d{2}:\d{2}\s*\|", "| — |", DISSECTION)
    check(no_time != DISSECTION, "夹具自检：分镜表时间码已全部抹掉")
    (broken / "拆解.md").write_text(no_time, encoding="utf-8")
    proc4 = run_confirm(broken)
    check(proc4.returncode == 0, "时间码读不出时 confirm 仍成功", proc4.stderr.strip()[:200])
    if (broken / SPANS_NAME).is_file():
        rows4 = load_spans(broken)
        by_cat = {}
        for row in rows4.values():
            by_cat.setdefault(row["类别"], []).append(row)
        check(all(not row["出现时段"] for row in by_cat.get("服装", []))
              and all(not row["出现时段"] for row in by_cat.get("人物", [])),
              "镜号换算不出时段 → 服装/人物留空数组",
              [row["出现时段"] for row in by_cat.get("服装", []) + by_cat.get("人物", [])])
        check(all(row["出现时段"] for row in by_cat.get("主体", [])),
              "主体自带的括号时段不受分镜表影响",
              [row["出现时段"] for row in by_cat.get("主体", [])])


def section_real() -> None:
    print("\n[4] 真实样本 pop-3--7（B1 验收判据）")
    if not (REAL / "拆解.md").is_file():
        print("[SKIP] 本机没有 analysis/reverse/pop-3--7/拆解.md")
        return
    tmp = Path(tempfile.mkdtemp(prefix="span-real-"))
    try:
        task = tmp / "pop-3--7"
        task.mkdir()
        for name in ("拆解.md", "待确认清单.md"):
            if (REAL / name).is_file():
                shutil.copy2(REAL / name, task / name)
        proc = run_confirm(task)
        check(proc.returncode == 0, "pop-3--7 重跑退出码 0", proc.stderr.strip()[:200])
        rows = load_spans(task)
        check(len(rows) == 12, "清单 12 行", len(rows))
        check_format(rows, "pop-3--7")
        need = [2, 7, 9, 12]
        empty = [n for n in need if not rows[n]["出现时段"]]
        check(not empty, "必须换/建议准备 4 行（#2/#7/#9/#12）时段非空", f"空行 {empty}")
        located = sum(1 for row in rows.values() if row["出现时段"])
        check(located >= 10, f"总非空 ≥10/12（实得 {located}/12）")
        check(rows[9]["出现时段"][0]["起"] == "00:10",
              "#9（镜 5）落在 00:10", rows[9]["出现时段"])
        check(7 in rows[2]["同实体疑似"] and 2 in rows[7]["同实体疑似"],
              "#2↔#7 同实体疑似对称", (rows[2]["同实体疑似"], rows[7]["同实体疑似"]))
        check(9 not in rows[2]["同实体疑似"],
              "#2 描述里的另一件东西不算同实体（#9 不误判）", rows[2]["同实体疑似"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="span-check-"))
    try:
        section_fixture(tmp)
        section_real()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    fails = [name for ok, name in results if not ok]
    print(f"\n{len(results) - len(fails)}/{len(results)} PASS"
          + (f"；FAIL：{'、'.join(fails)}" if fails else ""))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
