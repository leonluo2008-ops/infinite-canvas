"""表格形态容错：不带前置竖线的分镜表也必须能渲染成镜头行。

2026-11-01 实测事故：视觉模型第二次跑拆解时把分镜表写成
`1 | 00:00-00:02 | 人物 | …`（没有行首 `|`），渲染器只认行首竖线，
整张表被当成散文丢掉——分镜段直接没了。同一轮里我放宽判定时又踩了第二个坑：
把 `* 镜号01：…` 这种项目符号当成表头，整表被拼成「* 镜号01：* 镜号02｜…」。

这条测试锁住两件事：
1. 两种表格写法（行首竖线 / 不带竖线）都能出同样的镜头行；
2. 项目符号行不许被吞进表格。

跑法：py -3 tests/table_shape_tolerance.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TASK = REPO / "analysis" / "reverse" / "_tbl-check"
PY = sys.executable

HEADER_PIPED = "| 镜号 | 起秒-止秒 | 运动主体 | 运动形态 | 速度 | 画面内容 | 光影变化 | 转场方式 | 站位 | 朝向 | 视线 | 镜头运动 | 景别 |"
SEP_PIPED = "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |"
ROW_A = "| 1 | 00:00-00:02 | 人物 | 转身甩手 | 快 | 车厢内回眸转身 | 暖光 | 无剪切 | 画面中心 | 背对转侧身 | 镜头 | 固定 | 中景 |"
ROW_B = "| 2 | 00:02-00:04 | 手机 | 悬浮翻转 | 慢 | 手机翻滚 | 反光 | 无剪切 | 画面正中 | — | — | 慢推 | 特写 |"

HEADER_BARE = HEADER_PIPED.strip("|").strip()
SEP_BARE = "--- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | ---"
ROW_A_BARE = ROW_A.strip("|").strip()
ROW_B_BARE = ROW_B.strip("|").strip()

BODY_HEAD = """## 一 场景
白棚，纯白背景无阴影。

## 二 产品
橙色圆柱瓶身，银色瓶盖。

## 三 运动
由左至右横向飞行。

## 四 光影
均匀高调软光。

## 五 镜头
固定机位微推，中景到特写。

## 六 意境
干净、克制、产品为主。

## 七 分镜表
- 一镜到底：是
- 首帧：人物位于画面左侧，背对镜头。

{table}

## 八 声音
无对白，电子节拍。

## 九 资产
* **人物（出现于：00:00-00:04）**：女性，短发，深色上衣。
"""

BODY_BULLET_TRAP = """## 一 场景
白棚。

## 二 产品
橙色圆柱瓶身。

## 三 运动
横向飞行。

## 四 光影
均匀软光。

## 五 镜头
固定。

## 六 意境
干净。

## 七 分镜表
- 一镜到底：是
* 镜号01：这是项目符号写的分镜，不是表格
- 首帧：人物位于左侧。

## 八 声音
无。

## 九 资产
* **人物（出现于：00:00-00:04）**：女性。
"""


def write_task(body: str) -> None:
    if TASK.exists():
        shutil.rmtree(TASK)
    TASK.mkdir(parents=True)
    (TASK / "拆解.md").write_text(body, encoding="utf-8")


def render(template: str = "lumi-4section") -> tuple[int, str]:
    proc = subprocess.run(
        [PY, str(REPO / "pipeline" / "reverse" / "render.py"), "--id", TASK.name, "--template", template],
        cwd=str(REPO), capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    out_file = TASK / f"提示词-{template}.md"
    text = out_file.read_text(encoding="utf-8") if out_file.exists() else ""
    return proc.returncode, text


def shot_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.strip().startswith("镜号 1") or ln.strip().startswith("镜号 2")]


def main() -> int:
    fails: list[str] = []

    write_task(BODY_HEAD.format(table="\n".join([HEADER_PIPED, SEP_PIPED, ROW_A, ROW_B])))
    code_piped, text_piped = render()
    if code_piped != 0:
        fails.append(f"带竖线的表渲染失败 exit={code_piped}")
    piped = shot_lines(text_piped)
    if len(piped) != 2:
        fails.append(f"带竖线的表应出 2 条镜头行，实得 {len(piped)}")

    write_task(BODY_HEAD.format(table="\n".join([HEADER_BARE, SEP_BARE, ROW_A_BARE, ROW_B_BARE])))
    code_bare, text_bare = render()
    if code_bare != 0:
        fails.append(f"不带竖线的表渲染失败 exit={code_bare}")
    bare = shot_lines(text_bare)
    if len(bare) != 2:
        fails.append(f"不带竖线的表应出 2 条镜头行，实得 {len(bare)}")
    if piped and bare and [x.strip() for x in piped] != [x.strip() for x in bare]:
        fails.append("两种写法渲染出的镜头行不一致")

    write_task(BODY_BULLET_TRAP)
    code_trap, text_trap = render()
    if code_trap != 0:
        fails.append(f"项目符号陷阱用例渲染失败 exit={code_trap}")
    if "* 镜号01：* 镜号" in text_trap:
        fails.append("项目符号行被当成表头，整表被拼坏")

    if TASK.exists():
        shutil.rmtree(TASK)

    if fails:
        for f in fails:
            print(f"[FAIL] {f}")
        print(f"{len(fails)} FAIL")
        return 1
    print("3 PASS / 0 FAIL")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
