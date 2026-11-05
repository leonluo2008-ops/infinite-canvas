#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""人工复检（B1/B2/B3 的后端）判据脚本（纯标准库，无 pytest）。

跑法：`py -3 tests/recheck_check.py`        退出码 0 = 全过，1 = 有 FAIL

判据（docs/archive/20261103/方案-人工复检-20261102.md §4）
----------------------------------------
R1  对 pop-3-b3：这一条已经被人定过（首帧行按建议改过），清单不再重复报它；而当初那句
    「背对镜头…视线看向镜头」仍要被机器认出来，why 点名两处矛盾。另加一条不误报：
    「背朝画面右侧」不是「背对镜头」，不许把一句正确的话报成矛盾。
R2  选「用建议」→ 拆解.md 首帧行变成建议文本、重渲染后提示词第 25 行同样是它、status=replaced。
R3  选「保留原样」→ 拆解.md 逐字不变（sha256 前后比），status=kept。
R4  负向：四套模板产物里不出现「待复检 / 已复检 / 置信度」（0 命中）。
R5  机器实测冲突（硬切 0 次却写「一镜到底：否」）也会进清单——用构造拆解，不依赖真实片。

夹具说明（2026-11-02 第二次修）
--------------------
pop-3-b3 会被重拆，拆解.md 的内容随时变；测试不再拿真实任务当夹具。`base` 是下面
`FIXTURE_TEMPLATE` 现写的一份九层拆解，首帧行本来就是复检前模型写的原句
`背对镜头，侧身向右转头，视线看向镜头`，其余结构与真实片同形（5 镜分镜表）。
真实任务只做软校验：文件/条目不在 → `[SKIP]`，不计 FAIL。

R1 里「真实任务已闭环」和「复检后那句」两条读真实文件；若它已被重拆、原句不在，
就降级为「首帧行读得出来」的软校验——那是数据变了，不是复检功能坏了。

外加四条护栏
------------
① 建议值必须原样取自同一份拆解（首帧=分镜表第 1 镜「朝向/视线」两列，一个字没加）；
② 已定过态（kept）的条目不重复提醒；③ CLI 出错 = 退出码 + 一行 stderr（无 traceback）；
④ 模型自陈不确定的行也进清单，且不给机器建议（不许编）。

真实任务只读：pop-3-b3 的拆解.md 只在 R1/R4 里被读，不写。所有写回都在临时目录的
夹具上做（R2 走的是服务端 do_recheck 真路径，只把 `--id <任务>` 换成夹具的绝对路径）。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REAL = ROOT / "analysis" / "reverse" / "pop-3-b3"
RECHECK = ROOT / "pipeline" / "recheck.py"
RENDER = ROOT / "pipeline" / "reverse" / "render.py"
TEMPLATES = ("lumi-4section", "employee-checklist", "seven-section", "timeline-narrative")
FORBIDDEN = ("待复检", "已复检", "置信度")

BUGGY_LINE = "背对镜头，侧身向右转头，视线看向镜头"
FIXED_SENTENCE = "身体侧对镜头、背朝画面右侧"
FIXED_SENTENCE_2 = "脸与视线正对镜头"

# 九层拆解骨架：自包含夹具（不读 analysis/reverse/ 下的任何任务）。
# 形状对齐真实片：六层 3 条 + 四层 3 条 + 5 镜分镜表 → timeline-narrative 的「首帧」正好落在第 25 行。
FIXTURE_TEMPLATE = """<!-- 测试夹具：视频 fake.mp4 | 时长: 15.00s | 音轨: 有 -->
> 规格：1080×1888 · 9:16 竖屏 · 15.00 秒 · 30.0 fps · 有音轨 · 硬切 0 次

## 一 场景
*   **车厢内景（00:00-00:04）**：对称车厢，橙色与暗棕色调，两侧带状顶灯，地面有反光。
*   **纯白空间（00:04-00:14）**：极简白色无缝空间，两侧垂直发光条，地面高反射。

## 二 产品
*   **媒介产品（00:00-00:06）**：深色直角矩形手机，正面全面屏，背部多摄模组。
*   **核心产品（00:04-00:14）**：细长圆柱形透明瓶，银色圆柱盖，橙色矩形标签。

## 三 运动
*   **主体与轨迹**：人物由背转正把手机向镜头抛出；人物自屏幕中向前迈步；瓶体垂直上浮。
*   **速度与模糊**：前快后缓，抛掷有旋转模糊，空中减速悬停。

## 四 光影
*   **光源与软硬**：前半段顶部条状柔光，穿屏后全方位高调均匀漫射白光。
- **动态阴影**：手机自转时机身表面有微弱自遮挡阴影，实体先于阴影入画。
*   **亮度变化**：整体画面由暗暖橙骤升为高亮纯白。

## 五 镜头
*   **机位与角度**：正面对称中心机位，初为平视，后略带仰角。
*   **运镜**：微拉后快速推进，穿屏后微仰固定。
*   **景别与景深**：中景起，特写过渡，结尾近景浅景深。

## 六 意境
*   **整体调性**：前卫超现实的潮流视觉。
*   **具体手段**：暖暗车厢与冷亮纯白空间的色差反差，配合穿屏与悬浮。
*   **节奏感**：00:01 抛机加速，00:03 悬停放慢，00:07 递物定格。

## 七 分镜表
- 一镜到底：是
- 首帧：{first_frame}

| 镜号 | 起秒-止秒 | 运动主体 | 运动形态 | 速度 | 画面内容 | 光影变化 | 转场方式 | 站位 | 朝向 | 视线 | 镜头运动 | 景别 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| 1 | 00:00-00:02 | 人物、手机 | 转身正对将手机向镜头抛出 | 快速 | 人物在车厢中央抛出手机 | 暖调顶光 | 无剪切 | 居中，占画面1/2 | 侧身偏右转正 | 镜头 | 微拉后固定 | 中景 |
| 2 | 00:02-00:05 | 手机 | 手机空中翻转悬停 | 慢速 | 手机旋转放大 | 暖调过渡冷白 | 穿屏 | 居中，占满前景 | 正对 | 镜头 | 快速推进 | 特写 |
| 3 | 00:05-00:08 | 人物、瓶 | 自屏幕向镜头递出瓶 | 中速 | 人物在白色空间递出瓶 | 高调无影软光 | 穿屏 | 居中，占画面2/3 | 正对 | 镜头 | 推进 | 近景 |
| 4 | 00:08-00:10 | 手、瓶 | 右下角手部入画接走瓶 | 平稳 | 手自右下伸入接住瓶 | 均匀白光 | 无剪切 | 居中偏右下 | 正对 | 瓶 | 微摇微退 | 特写 |
| 5 | 00:10-00:14 | 瓶群、人物 | 大量瓶体垂直上升 | 升格放缓 | 人物手持瓶居中凝视 | 纯白漫射光 | 无剪切 | 居中，占画面1/2 | 正对 | 镜头 | 微仰固定 | 中景 |

## 八 声音
*   **台词**：无人物对白。
*   **音乐**：强节奏重低音电子舞曲，明快的合成器音色。
*   **音效**：挥臂破风声、翻转悬浮嗡鸣、瓶群升空风铃音。

## 九 资产
*   **人物 女主角（出现于：00:00-00:14）**：二十岁出头女性，及肩微卷发，身形匀称。
*   **人物 互动手部（出现于：00:08-00:10）**：仅露出一只男性右手，手指修长。
*   **服装 服装A（出现于：00:00-00:04）**：深色紧身背心配同色短裤，浅色运动鞋。
*   **服装 服装B（出现于：00:04-00:14）**：亮橙色方领无袖连衣裙，细高跟凉鞋。
*   **道具 手机（出现于：00:00-00:06）**：深色直角微弧边框大屏手机。
*   **道具 透明瓶（出现于：00:04-00:14）**：圆柱形透明瓶配亮银色圆盖。
*   **道具 包装盒（出现于：00:10-00:11）**：垂直长方体橙色纸盒。
*   **场景 车厢（出现于：00:00-00:04）**：对称车厢结构，暖光顶灯。
*   **场景 白空间（出现于：00:04-00:14）**：全白无缝亮面空间，含垂直发光灯柱。
"""


def fixture_text(first_frame: str = BUGGY_LINE) -> str:
    """现写一份九层拆解（字符串模板，不读任何真实任务）。"""
    return FIXTURE_TEMPLATE.format(first_frame=first_frame)


results: list[tuple[str, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> bool:
    results.append(("PASS" if ok else "FAIL", name, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"｜{detail}" if detail else ""))
    return ok


def skip(name: str, detail: str = "") -> None:
    """真实任务的软校验：文件/条目不在 → 记 SKIP（不计 FAIL）。"""
    results.append(("SKIP", name, detail))
    print(f"[SKIP] {name}" + (f"｜{detail}" if detail else ""))


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"导不进 {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *args], cwd=str(ROOT), capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_task(root: Path, name: str, text: str) -> Path:
    """临时目录里造一个任务目录：只要 拆解.md（不复制真实任务的任何文件）。"""
    folder = root / "analysis" / "reverse" / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "拆解.md").write_text(text, encoding="utf-8", newline="\n")
    return folder


def render(task: Path, template: str, out: Path) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    proc = run(["pipeline/reverse/render.py", "--id", str(task), "--template", template,
                "--out-dir", str(out)])
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout).strip()[-400:])
    return out / f"提示词-{template}.md"


def line_no(path: Path, needle: str) -> tuple[int, str]:
    """→ (1 基行号, 整行)；找不到 → (0, "")。"""
    for number, text in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if needle in text:
            return number, text
    return 0, ""


def shot_cell(text: str, column: str) -> str:
    """从拆解.md 的分镜表第 1 镜取一格（按表头找列号）。"""
    header: list[str] | None = None
    for raw in text.splitlines():
        s = raw.strip()
        if s.count("|") < 2:
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if header is None:
            if any("镜号" in c for c in cells):
                header = cells
            continue
        if cells and cells[0].isdigit():
            index = header.index(column) if column in header else -1
            return cells[index] if 0 <= index < len(cells) else ""
    return ""


# ------------------------------------------------------------------ R1


def r1_real_task(mod, real: str, buggy: str, tmp: Path) -> None:
    """pop-3-b3 只读：这一条已经闭环，而当初那句真 bug 仍要认得出、正确的句子不误报。

    真实拆解可能被重拆（今晚就重拆过两次）：文件/句子不在 → SKIP；在则照旧断言。
    """
    if not real:
        skip("R1 ① 真实任务已闭环：首帧行按建议改过之后不再重复提醒", "真实 拆解.md 不在")
    else:
        items = mod.detect(str(REAL))
        check(not [i for i in items if i.get("id") == "firstframe-orientation"],
              "R1 ① 真实任务已闭环：首帧行按建议改过之后不再重复提醒",
              f"现算 {len(items)} 条：{[i.get('id') for i in items]}")

    if not real:
        skip("R1 ② 真实首帧行是复检后的那一句（只读核对）", "真实 拆解.md 不在")
    elif FIXED_SENTENCE in real and FIXED_SENTENCE_2 in real:
        check(True, "R1 ② 真实首帧行是复检后的那一句（只读核对）")
    else:
        number, line = line_no(REAL / "拆解.md", "首帧：")
        check(number > 0 and bool(line.strip()),
              "R1 ② 真实首帧行读得出来（已重拆，复检后原句不在；软校验）",
              f"第 {number} 行：{line.strip()[:60]}")

    check(buggy != real and BUGGY_LINE in buggy,
          "R1 ③ 夹具：首帧行是复检前模型写的原句")

    # 「背朝画面右侧」不是「背对镜头」：这句现在是对的，不许再报
    quiet = make_task(tmp, "rc-quiet", fixture_text(
        "人物侧对镜头、背朝画面右侧；头向右回转，脸与视线正对镜头。"))
    check(not [i for i in mod.detect(str(quiet)) if i.get("id") == "firstframe-orientation"],
          "R1 ④ 「背朝画面右侧」不被误读成「背对镜头」（0 误报）")

    # 当初那句：同一句里「背对镜头」×「视线看向镜头」——仍然要报，why 点名两处
    items = mod.detect(str(make_task(tmp, "rc-r1", buggy)))
    first = next((i for i in items if i.get("id") == "firstframe-orientation"), None)
    check(first is not None, "R1 ⑤ 真 bug 那句仍产出 firstframe-orientation",
          f"id={[i['id'] for i in items]}")
    if first is None:
        return
    why = first.get("why", "")
    check("背对" in why and "看向镜头" in why,
          "R1 ⑥ why 点名「背对」与「看向镜头」的矛盾", why[:120])
    check("侧身偏右转正" in why, "R1 ⑦ why 引用了分镜表第 1 镜的「朝向：侧身偏右转正」", why)
    check(first.get("status") == "pending", "R1 ⑧ 新发现的条目状态是 pending", first.get("status"))
    if (REAL / "拆解.md").is_file():
        check(True, "R1 ⑨ 真实拆解还在（只读，本用例没动它）",
              sha(REAL / "拆解.md")[:16])
    else:
        skip("R1 ⑨ 真实拆解还在（只读，本用例没动它）", "文件不在")


def guard_suggest_verbatim(mod, base: str, tmp: Path) -> None:
    """护栏①：建议值只能是分镜表第 1 镜两列的原样拼接，不许有自己编的句子。

    `base` 是不带 看帧.json 的那份夹具——此时退回分镜表拼接（有 看帧.json 时优先用它的
    描述，那条在 tests/frame_facts_check.py 里单测）。
    """
    items = mod.detect(str(make_task(tmp, "rc-sug", base)))
    first = next((i for i in items if i["id"] == "firstframe-orientation"), None)
    if first is None:
        check(False, "护栏① 首帧条目没产出，无法核对建议值来源")
        return
    want = "首帧：" + shot_cell(base, "朝向") + "，" + shot_cell(base, "视线")
    check(first["suggest"] == want, "护栏① 建议值 = 分镜表第 1 镜「朝向/视线」原样拼接",
          f"实际 {first['suggest']!r}｜应为 {want!r}")
    check("身体侧对" not in first["suggest"] and "回望" not in first["suggest"],
          "护栏① 建议值里没有主 agent 那套改写（身体侧对/回望）", first["suggest"])


# ------------------------------------------------------------------ R2


def r2_api_use(server, mod, base: str, tmp: Path) -> None:
    """POST /api/recheck/<id> 走服务端真路径：写回 → 重渲染 → 返回新提示词。"""
    task = make_task(tmp, "rc-r2", base)
    dissection = task / "拆解.md"
    before = sha(dissection)

    real_run_py = server.run_py

    def run_py_fixture(args: list[str], timeout: int):
        """把 `--id <任务名>` 换成夹具的绝对路径：别的都照跑，不碰仓库里的任务目录。"""
        if args and args[0] in ("pipeline/recheck.py", "pipeline/reverse/render.py"):
            args = list(args)
            index = args.index("--id") + 1
            args[index] = str(task)
        return real_run_py(args, timeout)

    class FakeHandler(server.Handler):
        """掐掉 socket 那一层：只测 do_recheck 自己的判断（不监听端口、不起服务）。"""

        def __init__(self, payload: dict) -> None:
            self._payload = dict(payload)
            self.path = "/api/recheck/rc-r2"
            self.command = "POST"
            self.headers = {}
            self.sent: list[tuple[int, dict]] = []

        def read_json(self) -> dict:
            return dict(self._payload)

        def send_json(self, obj, status: int = 200) -> None:
            self.sent.append((status, obj))

    real_analysis = server.ANALYSIS
    server.ANALYSIS = tmp / "analysis" / "reverse"
    server.run_py = run_py_fixture
    try:
        handler = FakeHandler({"action": "use", "id": "firstframe-orientation",
                               "template": "timeline-narrative"})
        handler.do_recheck("rc-r2")
    finally:
        server.ANALYSIS = real_analysis
        server.run_py = real_run_py

    status, body = handler.sent[-1] if handler.sent else (0, {})
    check(status == 200, "R2 ① POST /api/recheck 返回 200", f"{status} {str(body)[:160]}")
    if status != 200:
        return

    ledger = json.loads((task / "复检清单.json").read_text(encoding="utf-8"))
    item = next((i for i in ledger if i.get("id") == "firstframe-orientation"), None)
    suggest = (item or {}).get("suggest", "")
    check(bool(item) and item.get("status") == "replaced",
          "R2 ② 复检清单.json 该条 status=replaced", (item or {}).get("status"))

    number, line = line_no(dissection, "首帧：")
    check(line.strip() == "- " + suggest,
          "R2 ③ 拆解.md 首帧行 == 建议文本（行首列表符号保留）", f"第 {number} 行：{line!r}")
    check(sha(dissection) != before, "R2 ④ 拆解.md 确实被原子写过（sha256 变了）")

    prompt = body.get("prompt") or {}
    text = prompt.get("text") or ""
    prompt_path = task / "提示词-timeline-narrative.md"
    check(prompt_path.is_file(), "R2 ⑤ 写回后自动重渲染了当前模板", str(prompt_path.name))
    if not prompt_path.is_file():
        return
    on_disk = prompt_path.read_text(encoding="utf-8")
    check(text == on_disk, "R2 ⑥ 接口返回的提示词 == 盘上刚渲染出来的那一份",
          f"接口 {len(text)} 字｜盘上 {len(on_disk)} 字")
    pno, pline = line_no(prompt_path, "首帧：")
    check(pno == 25 and pline == suggest,
          "R2 ⑦ 提示词第 25 行 == 建议文本（逐字相等）", f"第 {pno} 行：{pline!r}")
    check(text.count("首帧：") == 1 and "待人工复检" not in text,
          "R2 ⑧ 提示词里没有掺清单用语（「待人工复检」0 处）")

    # 服务端把清单一起还回来，页面据此重画卡片
    check(isinstance(body.get("items"), list) and isinstance(body.get("pending"), int),
          "R2 ⑨ 返回体带 items / pending",
          f"pending={body.get('pending')} items={len(body.get('items') or [])}")
    mod = mod  # noqa: B018 - 保持签名一致（本用例只用服务端路径）


# ------------------------------------------------------------------ R3


def r3_keep(mod, base: str, tmp: Path) -> None:
    """选「保留原样」：拆解.md 一个字节都不动，只记 kept。"""
    task = make_task(tmp, "rc-r3", base)
    dissection = task / "拆解.md"
    before = sha(dissection)
    first = mod.refresh(str(task))
    check(mod.pending_count(first) >= 1, "R3 ① 夹具里先有 1 条待复检", f"{mod.pending_count(first)} 项")
    items = mod.apply(str(task), "firstframe-orientation", "keep")
    item = next((i for i in items if i["id"] == "firstframe-orientation"), None)
    check((item or {}).get("status") == "kept", "R3 ② status=kept", (item or {}).get("status"))
    check(sha(dissection) == before, "R3 ③ 拆解.md 逐字不变（sha256 相同）", sha(dissection)[:16])
    again = mod.refresh(str(task))
    check(mod.pending_count(again) == 0, "R3 ④ 点过「保留原样」后不再重复提醒",
          f"pending={mod.pending_count(again)}")


# ------------------------------------------------------------------ R4


def r4_no_forbidden_words(base: str, tmp: Path) -> None:
    """四套模板产物里 0 处「待复检 / 已复检 / 置信度」。"""
    task = make_task(tmp, "rc-r4", base)
    hits: list[str] = []
    for template in TEMPLATES:
        path = render(task, template, tmp / f"out-{template}")
        text = path.read_text(encoding="utf-8")
        for word in FORBIDDEN:
            if word in text:
                hits.append(f"{template}×{word}")
    check(not hits, "R4 ① 四套模板产物 0 命中「待复检/已复检/置信度」", "、".join(hits) or "4 套都干净")

    # 真实任务盘上已有的四份提示词也是同一口径（只读；没有就 SKIP）
    real_prompts = sorted(REAL.glob("提示词-*.md"))
    if not real_prompts:
        skip("R4 ② pop-3-b3 盘上四份提示词同样 0 命中", "真实任务没有 提示词-*.md")
    else:
        real_hits = []
        for path in real_prompts:
            text = path.read_text(encoding="utf-8")
            real_hits += [f"{path.name}×{w}" for w in FORBIDDEN if w in text]
        check(not real_hits, "R4 ② pop-3-b3 盘上四份提示词同样 0 命中",
              "、".join(real_hits) or f"{len(real_prompts)} 份都干净")


# ------------------------------------------------------------------ R5


def r5_machine_fact(mod, base: str, tmp: Path) -> None:
    """硬切 0 次却写「一镜到底：否」→ 必须进清单。"""
    broken = base.replace("- 一镜到底：是", "- 一镜到底：否")
    check(broken != base, "R5 ① 构造夹具：一镜到底改成「否」（机器实测 0 次硬切）")
    task = make_task(tmp, "rc-r5", broken)
    items = mod.detect(str(task))
    hit = next((i for i in items if i.get("id") == "machine-fact-onetake"), None)
    check(hit is not None, "R5 ② check_facts() 的冲突并进了清单（machine-fact-onetake）",
          f"id={[i['id'] for i in items]}")
    if hit is None:
        return
    check(hit.get("kind") == "机器实测冲突", "R5 ③ kind=机器实测冲突", hit.get("kind"))
    check("0 次硬切" in hit.get("original", "") or "硬切" in hit.get("why", ""),
          "R5 ④ 原文/为什么里写清了「机器实测 0 次硬切」",
          f"original={hit.get('original')!r}")
    check(hit.get("suggest") == "一镜到底：是", "R5 ⑤ 建议值把答案改成机器实测的那个",
          repr(hit.get("suggest")))
    after = mod.apply(str(task), "machine-fact-onetake", "use")
    _no, line = line_no(task / "拆解.md", "一镜到底：")
    check(line.strip() == "- 一镜到底：是", "R5 ⑥ 「用建议」只改那一行", line)
    left = [i["id"] for i in mod.detect(str(task))]
    check("machine-fact-onetake" not in left,
          "R5 ⑦ 改完这一条冲突消失（清单不再报它，别的条目照旧）", f"还剩 {left}")
    check(any(i["id"] == "machine-fact-onetake" and i["status"] == "replaced" for i in after),
          "R5 ⑧ 历史记一条 replaced")


# ------------------------------------------------------------------ R6 朝向口径

def frame_facts(facing: str) -> dict:
    """夹具：0 秒那一帧说 `facing`（老格式：无 votes / 无 facing_class）。"""
    return {"id": "rc-facts", "源视频": "fake.mp4", "源片尺寸": {"宽": 1080, "高": 1888},
            "帧": [{"秒": 0.0, "文件": "analysis/reverse/rc-facts/看帧/0.png",
                    "宽": 1080, "高": 1888,
                    "判定": {"people": 1,
                             "body_orientation": {"facing": facing, "back_toward": "画面右侧"},
                             "gaze": "直视镜头", "confidence": "高",
                             "描述": "车厢里她站在过道中间，回头看向镜头。"}}]}


def with_first_frame(base: str, line: str) -> str:
    """把拆解里「首帧：」那一行换成 `line`（其余逐字不动）；不是正好一行 → 空串。"""
    out, hits = re.subn(r"(?m)^[ \t]*(?:[-*+][ \t]+)?首帧[ \t]*[：:][ \t]*.*$",
                        "- 首帧：" + line, base)
    return out if hits == 1 else ""


def r6_facing_tolerance(mod, base: str, tmp: Path) -> None:
    """B3 ③：邻近态不报、真相反要报；B2：_facing 不把「侧背」读成「背对」。"""
    check(mod._facing("侧背对镜头") == "侧背" and mod._facing("背对镜头") == "背对"
          and mod._facing("正对镜头") == "正对",
          "R6 ① _facing 四档各归各的（侧背 / 背对 / 正对）",
          f"{mod._facing('侧背对镜头')} / {mod._facing('背对镜头')} / {mod._facing('正对镜头')}")
    check(mod._facing("背朝画面右侧") == "" and mod._facing("背朝画面右侧", field=True) == "背对",
          "R6 ② 「背朝画面右侧」不是朝向读数（只有朝向短词才认「背」）",
          f"{mod._facing('背朝画面右侧')!r} / {mod._facing('背朝画面右侧', field=True)!r}")

    near_text = with_first_frame(base, "人物背对镜头站在车厢中央，手扶着扶手。")
    check(bool(near_text), "R6 ③ 夹具：首帧行换成「背对镜头…手扶着扶手」")
    near = make_task(tmp, "rc-near", near_text)
    (near / "看帧.json").write_text(json.dumps(frame_facts("侧背对镜头"), ensure_ascii=False),
                                    encoding="utf-8")
    ids = [i["id"] for i in mod.detect(str(near))]
    check("firstframe-orientation" not in ids,
          "R6 ④ 拆解「背对」+ 判定「侧背」→ 清单不报冲突（邻近态）", f"id={ids}")

    opposite = make_task(tmp, "rc-opp", with_first_frame(base, "人物正对镜头站在车厢中央，手扶着扶手。"))
    (opposite / "看帧.json").write_text(json.dumps(frame_facts("背对镜头"), ensure_ascii=False),
                                       encoding="utf-8")
    items = mod.detect(str(opposite))
    hit = next((i for i in items if i.get("id") == "firstframe-orientation"), None)
    check(hit is not None, "R6 ⑤ 拆解「正对」+ 判定「背对」→ 清单报冲突（真相反）",
          f"id={[i['id'] for i in items]}")
    if hit:
        check("看帧判定" in hit.get("why", "") and "正对" in hit.get("why", ""),
              "R6 ⑥ why 指名看帧判定与「正对」那一头", hit.get("why", "")[:120])


# ------------------------------------------------------------------ B3 接线（静态）


def wire_ui() -> None:
    """卡片真在 DOM 里、真在提示词预览上方、三个按钮都在、不拦生成。

    浏览器亲验归主 agent；这里只盯「id 与函数对不上」这类一改就废的低级错。
    """
    html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    need = ['id="recheckCard"', 'id="recheckBody"', 'id="recheckTitle"',
            'data-act="use"', 'data-act="keep"', 'data-act="edit"', 'data-act="submit"',
            '$("#recheckBody").addEventListener("click"', "function renderRecheck",
            "async function loadRecheck", "loadRecheck(id)"]
    missing = [key for key in need if key not in html]
    check(not missing, "B3 ① 复检卡的 id / 三个按钮 / 读清单函数都在", f"缺 {missing or '无'}")

    card = html.find('id="recheckCard"')
    prompt = html.find("<h2>提示词预览</h2>")
    check(0 < card < prompt, "B3 ② 卡片排在「提示词预览」上方", f"card@{card} prompt@{prompt}")

    start = html.find("function refreshButtons")
    body = html[start:html.find("\n}", start) + 2]
    check("recheck" not in body, "B3 ③ 「生成视频」不看复检状态（只提醒，不拦生成）", body.strip()[:80])


# ------------------------------------------------------------------ 护栏


def guards(mod, base: str, tmp: Path) -> None:
    """② 不确定行也进清单但不给建议；③ CLI 出错 = 退出码 + 一行 stderr；④ 只改一行。"""
    uncertain = base.replace(
        "- **动态阴影**：手机自转时机身表面有微弱自遮挡阴影，实体先于阴影入画。",
        "- **动态阴影**：疑似有微弱自遮挡阴影，看不清边缘。")
    task = make_task(tmp, "rc-guard", uncertain)
    items = mod.detect(str(task))
    hit = next((i for i in items if i.get("kind") == "模型自陈不确定"), None)
    check(hit is not None, "护栏② 模型自陈不确定（疑似/看不清）的行进了清单",
          f"id={[i['id'] for i in items]}")
    check(bool(hit) and hit.get("suggest") == "", "护栏② 这一条不给机器建议（不编）",
          repr((hit or {}).get("suggest")))

    listing = run(["pipeline/recheck.py", "--id", str(task)])
    check(listing.returncode == 0 and "uncertain" in listing.stdout,
          "护栏③ CLI --id <夹具> 能列出清单", f"exit={listing.returncode}")

    bad = run(["pipeline/recheck.py", "--id", str(task), "--use", "no-such-item"])
    err = [ln for ln in bad.stderr.strip().splitlines() if ln.strip()]
    check(bad.returncode != 0 and len(err) == 1 and "Traceback" not in bad.stderr,
          "护栏③ 复检项不存在 → 非 0 退出 + 一行 stderr（无 traceback）",
          f"exit={bad.returncode} stderr={err}")

    missing = run(["pipeline/recheck.py", "--id", "no-such-task-xyz"])
    check(missing.returncode != 0 and "Traceback" not in missing.stderr,
          "护栏③ 任务不存在 → 非 0 退出 + 一句话",
          f"exit={missing.returncode} {missing.stderr.strip()!r}")

    # 自己改：只换那一行，别的行一个字不动
    first = next((i for i in items if i["id"] == "firstframe-orientation"), None)
    before = (task / "拆解.md").read_text(encoding="utf-8").splitlines()
    mod.apply(str(task), "firstframe-orientation", "use", "首帧：人物在画面中心偏后，侧背对镜头。")
    after = (task / "拆解.md").read_text(encoding="utf-8").splitlines()
    changed = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
    check(first is not None and len(before) == len(after) and len(changed) == 1,
          "护栏④ 「自己改」只动那一行，行数与其余行不变", f"改了第 {changed} 行")

    keys = {"id", "kind", "where", "original", "suggest", "why", "status"}
    ledger = json.loads((task / "复检清单.json").read_text(encoding="utf-8"))
    extra = {k for item in ledger for k in item} - keys - {"text", "applied", "decidedAt"}
    check(not extra, "护栏④ 清单字段不夹带内部锚点（只有约定的那几项）", f"多出 {extra or '无'}")


# ------------------------------------------------------------------ 主流程


def main() -> int:
    print("=" * 78)
    print("人工复检 · 判据 R1–R5（真实任务只读；写回都在临时夹具上做）")
    print(f"仓库：{ROOT}")
    print("=" * 78)

    mod = load_module("recheck", RECHECK)
    server = load_module("ui_server", ROOT / "ui" / "server.py")
    real_path = REAL / "拆解.md"
    real = real_path.read_text(encoding="utf-8") if real_path.is_file() else ""
    # 夹具现造：首帧行就是复检前模型写的那句（真实文件已被重拆过两次，不再当夹具）
    base = fixture_text()

    with tempfile.TemporaryDirectory(prefix="recheck-check-") as tmpdir:
        tmp = Path(tmpdir)
        r1_real_task(mod, real, base, tmp)
        guard_suggest_verbatim(mod, base, tmp)
        r2_api_use(server, mod, base, tmp)
        r3_keep(mod, base, tmp)
        r4_no_forbidden_words(base, tmp)
        r5_machine_fact(mod, base, tmp)
        r6_facing_tolerance(mod, base, tmp)
        wire_ui()
        guards(mod, base, tmp)

    failed = [name for status, name, _ in results if status == "FAIL"]
    skipped = [name for status, name, _ in results if status == "SKIP"]
    passed = len(results) - len(failed) - len(skipped)
    print("-" * 78)
    print(f"合计 {len(results)} 项：{passed} PASS / {len(failed)} FAIL / {len(skipped)} SKIP")
    if skipped:
        print("SKIP 清单：")
        for name in skipped:
            print(f"  SKIP：{name}")
    if failed:
        for name in failed:
            print(f"  FAIL：{name}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
