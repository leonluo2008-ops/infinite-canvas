#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""看帧判定 · 判据 F1–F5（纯标准库，无 pytest）。

跑法：`py -3 tests/frame_facts_check.py`      退出码 0 = 全过，1 = 有 FAIL

判据（docs/archive/20261103/方案-看帧判定-20261102.md §4）
----------------------------------------
F1  所有者看过的那一帧（朝向「侧对镜头」、视线看镜头、1 人）写进夹具 `看帧.json` 后，
    `load_facts` 读得回来、三个字段都在。真实 pop-3-b3 只做软校验（有就读、没有就 SKIP）。
F2  抽出来的帧是原分辨率（1080×1888，与源片一致），不是缩略图（判据 F2 的机器口径：ffprobe 两侧）。
    真实任务缺文件/条目 → SKIP，不算 FAIL。
F3  有 `看帧.json` 时，extract 拼出来的提示词里出现「看帧判定」块且含 0 秒那句描述；没有时
    逐字不变（纯拼装断言，不联网；夹具现造）。
F4  夹具：`看帧.json` 说「正对镜头」而首帧行写「背对镜头」→ check_facts 记冲突，
    recheck 把它列进清单；两边一致时 0 冲突。
F5  留痕：`use` 之后 `original` 逐字等于改前原句、`applied` 逐字等于新句、`decidedAt` 有值。

夹具来源（2026-11-02 第二次修）
----------------------------
pop-3-b3 会被重拆，拆解.md 与 看帧.json 的内容随时变；测试不再拿真实任务当夹具。
所有带断言的拆解/判定都在 `tempfile` 里按下面的字符串模板现造；真实任务只做
「文件在不在、0 秒条目读不读得出」的软校验，缺了打 `[SKIP]`，不计 FAIL。

外加四条护栏
------------
① 判定 JSON 缺「描述」= 这次没判成（重试一次再失败就记 confidence=低，不许静默写空）；
② 真实 0 秒的「描述」不是字段拼接（没有 facing= / gaze= 这类词串，够长，有人有事有光）；
③ 真实 0 秒记录的 sha256 与盘上那张 png 对得上（记录指向的就是被看过的那张图）；
④ 建议值优先用 `看帧.json` 的 0 秒描述；没有它才退回分镜表「朝向/视线」拼接。

窗口多帧与机械聚合（2026-11-02 第四批）
------------------------------------
B3·窗口 ① `--window 0:0.5` + `--n 3` → 0 / 0.25 / 0.5 三帧：秒数递增、含首尾、抽出来仍是
     原分辨率（真 ffmpeg 抽一次验尺寸）；`n=1` 只取起点、越过片尾收到最后一帧。
B3·聚合 ② 逐字段计数：三帧一致 → 高、取该值；两同一异 → 低、该字段写「歧」、三帧说法按秒
     全列进「分歧」；描述取信息量最大那帧并标 `描述秒`；有帧没判成也记低并写明哪一秒。
B3·注入 ③ 低置信窗口注入行写「这一窗口三帧里 侧对／侧背 不一，以描述为准」，并标出描述
     来自哪一秒；该窗口的朝向不参与冲突（只提示）。
B3·兼容 ④ 同一帧不再靠投票定置信度（`judge_frame` 默认 1 次、`--votes` 三次档位不同也
     不记低）；老格式 看帧.json（单帧、无窗口、有 votes）仍能读、能注入、能参与冲突判定。

真实任务只读：pop-3-b3 的 看帧.json / 拆解.md 只读不写；所有写回都在临时目录的夹具上做。
真实 0 秒那一帧的 VLM 调用不在这里跑（会花钱）：F2 读的是已经跑过的那一份产物，
缺了就打 `[SKIP]` 并给出补跑命令，不拿「别人今晚重拆了」判我们功能坏。
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REAL = ROOT / "analysis" / "reverse" / "pop-3-b3"
FRAME_FACTS = ROOT / "pipeline" / "reverse" / "frame_facts.py"
EXTRACT = ROOT / "pipeline" / "reverse" / "extract.py"
RECHECK = ROOT / "pipeline" / "recheck.py"
RERUN = "py -3 pipeline/reverse/frame_facts.py --id pop-3-b3 --times 0"

# 所有者 2026-11-02 亲眼看过原图给的读法（判据 F1 的底线）；写在夹具里，不钉真实产物
WANT_FACING = "侧对镜头"
WANT_GAZE = "镜头"
WANT_PEOPLE = 1

# 这条片子原来的首帧句（复检前的那一句），拿它当「模型写错了」的夹具
BUGGY_LINE = "人物背对镜头，侧身向右转头，视线看向镜头。"
AGREE_LINE = "人物正对镜头站在车厢中央，视线看向镜头。"

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
- 一镜到底：{one_take}
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


def fixture_text(first_frame: str = BUGGY_LINE, one_take: str = "是") -> str:
    """现写一份九层拆解（字符串模板，不读任何真实任务）。"""
    return FIXTURE_TEMPLATE.format(first_frame=first_frame, one_take=one_take)


def facts_payload(facing: str, facing_class: str, description: str,
                  gaze: str = "直视镜头") -> dict:
    """一份看帧.json 夹具：0 秒那一帧的判定（与 frame_facts.py 的产物同形）。"""
    return {
        "id": "ff-fixture",
        "源视频": "fake.mp4",
        "源片尺寸": {"宽": 1080, "高": 1888},
        "帧": [{
            "秒": 0.0,
            "文件": "analysis/reverse/ff-fixture/看帧/0.png",
            "宽": 1080,
            "高": 1888,
            "判定": {
                "frame": 0, "people": WANT_PEOPLE, "subject": "年轻女性",
                "position": {"where": "画面中央", "height_ratio": "占画面高度约3/4"},
                "body_orientation": {"facing": facing, "facing_class": facing_class,
                                     "back_toward": "画面右侧"},
                "head_orientation": "向右回转，面向镜头",
                "gaze": gaze,
                "posture_action": "小臂抬到胸前",
                "shot_size": "中景", "objects": [],
                "confidence": "高",
                "描述": description,
            },
        }],
    }


# 所有者亲眼看过的那一帧（F1 夹具）
OWNER_FACTS = facts_payload(
    WANT_FACING, "侧对",
    "对称暖色调的车厢通道中，顶光柔和漫射，穿着黑色运动服的女子居中侧站，"
    "小臂抬于胸前，回首直视镜头，透出冷静自信的力量。")
# 与「背对镜头」构成真相反的那一帧（F4/F5 夹具）
OPPOSITE_FACTS = facts_payload(
    "正对镜头", "正对",
    "0 秒这一帧里人物正对镜头站在车厢中央，双臂自然下垂，顶灯从上方落下。")

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


def real_facts() -> dict | None:
    path = REAL / "看帧.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def entry_at(facts: dict | None, seconds: float) -> dict:
    for frame in (facts or {}).get("帧") or []:
        try:
            if abs(float(frame.get("秒", -1e9)) - seconds) < 1e-6:
                return frame
        except (TypeError, ValueError):
            continue
    return {}


def judgment_at(facts: dict | None, seconds: float) -> dict:
    judgment = entry_at(facts, seconds).get("判定")
    return judgment if isinstance(judgment, dict) else {}


def make_task(root: Path, name: str, text: str, facts: dict | None = None) -> Path:
    """临时目录里造一个任务目录：拆解.md（+ 可选的 看帧.json）。"""
    folder = root / "analysis" / "reverse" / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "拆解.md").write_text(text, encoding="utf-8", newline="\n")
    if facts is not None:
        (folder / "看帧.json").write_text(json.dumps(facts, ensure_ascii=False, indent=2) + "\n",
                                       encoding="utf-8", newline="\n")
    return folder


# ------------------------------------------------------------------ 取帧规则


def frame_selection(mod, fixture: Path) -> None:
    """取帧规则（不联网）：t=0 必抽；有分镜表抽每镜起秒（上限 8）；没有就硬切+四分位（上限 12）。"""
    table = mod.choose_times(fixture, 15.0, [])
    starts = mod.shot_starts(fixture)
    check(table and table[0] == 0.0 and table == sorted(table) and len(table) <= 8,
          "取帧① 有分镜表：t=0 在首位、升序、不超过 8 帧", f"{table}")
    check(starts and [t for t in table if t] == [t for t in starts if t][:8],
          "取帧② 有分镜表：抽的就是每镜起秒", f"起秒 {starts}")

    blind = mod.choose_times(ROOT / "no-such-task", 15.0, [3.0, 9.5])
    check(blind[0] == 0.0 and blind == sorted(blind) and len(blind) <= 12
          and 3.0 in blind and 9.5 in blind,
          "取帧③ 没有分镜表：硬切点 + 四分位，上限 12", f"{blind}")

    chosen = mod.choose_times(fixture, 15.0, [], [2.5, 6])
    check(chosen == [0.0, 2.5, 6.0], "取帧④ 给了 --times 就只用它（t=0 仍然必抽）", f"{chosen}")
    check(mod.choose_times(fixture, 15.0, [], [60]) == [0.0, 14.95],
          "取帧⑤ 超出片长的秒数收到最后一帧（不炸整次）", str(mod.choose_times(fixture, 15.0, [], [60])))


# ------------------------------------------------------------------ F1


def f1_owner_reading(mod, tmp: Path) -> None:
    """所有者亲眼看过的那一帧：写成 看帧.json 夹具后，load_facts 读得回那三样。"""
    folder = make_task(tmp, "ff-f1", fixture_text(), OWNER_FACTS)
    loaded = mod.load_facts(folder)
    if not check(loaded is not None, "F1 ① 夹具 看帧.json 读得出来（load_facts）"):
        return
    judgment = judgment_at(loaded, 0.0)
    check(bool(judgment), "F1 ② 有 0 秒那一条判定",
          f"帧秒={[f.get('秒') for f in (loaded or {}).get('帧', [])]}")
    body = judgment.get("body_orientation") or {}
    check(body.get("facing") == WANT_FACING,
          f"F1 ③ body_orientation.facing == 「{WANT_FACING}」", repr(body.get("facing")))
    check(WANT_GAZE in str(judgment.get("gaze") or ""),
          f"F1 ④ gaze 含「{WANT_GAZE}」", repr(judgment.get("gaze")))
    check(judgment.get("people") == WANT_PEOPLE,
          f"F1 ⑤ people == {WANT_PEOPLE}", repr(judgment.get("people")))
    check(bool(str(judgment.get("描述") or "").strip()), "F1 ⑥ 这一帧带着能进提示词的「描述」",
          str(judgment.get("描述") or "")[:60])


def f1_real_soft(facts: dict | None) -> None:
    """真实 pop-3-b3 只做结构软校验：有就读出来，缺任何一样都 SKIP，不判 FAIL。

    具体朝向词会随每次重拆变（2026-11-02 重拆后从「侧对」写成「侧背面」），
    把它钉死等于让测试跟着别人的数据跑，所以这里只验「读得出、字段齐」。
    """
    if facts is None:
        skip("F1 ⓪ pop-3-b3 的 看帧.json 在盘上（软校验）", RERUN)
        return
    judgment = judgment_at(facts, 0.0)
    if not judgment:
        skip("F1 ⓪ pop-3-b3 有 0 秒那条判定（软校验）",
             f"帧秒={[f.get('秒') for f in (facts or {}).get('帧', [])]}")
        return
    body = judgment.get("body_orientation") or {}
    check(bool(str(body.get("facing") or "").strip()),
          "F1 ⓪ pop-3-b3 的 0 秒判定给了朝向（软校验）", repr(body.get("facing")))
    check(bool(str(judgment.get("gaze") or "").strip()),
          "F1 ⓪b pop-3-b3 的 0 秒判定给了视线（软校验）", repr(judgment.get("gaze")))
    people = judgment.get("people")
    check(isinstance(people, int) and people >= 1,
          "F1 ⓪c pop-3-b3 的 0 秒判定人数是正整数（软校验）", repr(people))
    check(bool(str(judgment.get("描述") or "").strip()),
          "F1 ⓪d pop-3-b3 的 0 秒判定带描述（软校验）", str(judgment.get("描述") or "")[:48])


# ------------------------------------------------------------------ F2


def f2_original_resolution(facts: dict | None, mod) -> None:
    """帧图尺寸必须等于源片尺寸：缩略图会让判定看错。真实任务缺件 → SKIP。"""
    if facts is None:
        skip("F2 ⓪ 真实任务有 0 秒那条记录（含文件路径）", RERUN)
        return
    entry = entry_at(facts, 0.0)
    if not entry:
        skip("F2 ⓪ 真实任务有 0 秒那条记录（含文件路径）", RERUN)
        return
    check(bool(entry.get("文件")), "F2 ⓪ 真实任务有 0 秒那条记录（含文件路径）",
          str(entry.get("文件")))
    png = ROOT / str(entry.get("文件") or "")
    if not png.is_file():
        skip("F2 ① 帧图在盘上", str(entry.get("文件")))
        return

    video = ROOT / str((facts or {}).get("源视频") or "")
    if not video.is_file():
        skip("F2 ② 源片在盘上", str((facts or {}).get("源视频")))
        return
    try:
        frame_size = mod.probe_size(png)
        source_size = mod.probe_size(video)
    except Exception as exc:                            # noqa: BLE001 —— 量不出来就是 FAIL
        check(False, "F2 ③ 能量出两边的尺寸", f"{type(exc).__name__}：{exc}")
        return
    check(frame_size == source_size,
          "F2 ③ 帧图尺寸 == 源片尺寸（原分辨率，不缩放）",
          f"帧 {frame_size[0]}×{frame_size[1]}｜源片 {source_size[0]}×{source_size[1]}")
    check(frame_size == (1080, 1888), "F2 ④ 尺寸就是 1080×1888", str(frame_size))
    check(png.stat().st_size > 200 * 1024,
          "F2 ⑤ 文件体积不像缩略图（>200KB）", f"{png.stat().st_size} 字节")

    recorded = str(entry.get("sha256") or "")
    check(recorded == sha(png), "F2 ⑥ 记录里的 sha256 == 盘上那张图（记录指向被看过的那张）",
          recorded[:16])


# ------------------------------------------------------------------ F3


def f3_prompt_injection(extract, facts: dict | None, tmp: Path) -> None:
    """有 看帧.json → 提示词里出现「看帧判定」块且含 0 秒描述；没有 → 一个字都不变。

    夹具是现造的 OWNER_FACTS（不读真实任务的产物）。
    """
    base = extract.read_prompt()
    check(extract.build_prompt(base, "", None, None) == base,
          "F3 ① 没有 看帧.json 时提示词逐字等于原文（旧流程不受影响）")

    description = str(judgment_at(facts, 0.0).get("描述") or "")
    if not check(bool(description), "F3 ② 夹具 0 秒有描述可注入", "OWNER_FACTS"):
        return
    text = extract.build_prompt(base, "", None, facts, inject="first")
    check("## 看帧判定" in text, "F3 ③ 提示词里出现「看帧判定」块")
    check(description in text, "F3 ④ --inject first 时块里有 0 秒那句描述（逐字）", description[:50])
    check("- 0.0 秒：" in text and "body=" in text and "gaze=" in text,
          "F3 ⑤ --inject first：每帧一行「秒数 + 描述 + 可核对字段」")
    default_text = extract.build_prompt(base, "", None, facts)
    check("class=" in default_text and "gaze=" in default_text,
          "F3 ⑤-b 默认（fields）只给字段词表")
    check(description not in default_text,
          "F3 ⑤-c 默认（fields）不把整句描述塞进正文（上游会拦，实测 4/4 拦）")
    check(text.index("## 看帧判定") > text.index("## 九 资产") - 1,
          "F3 ⑥ 块排在九层正文之后（不打断结构）", f"位于第 {text.index('## 看帧判定')} 字")

    # 走真入口：load_frame_facts 从任务目录读，读不到就 None
    folder = make_task(tmp, "ff-load", "x", facts)
    check(extract.load_frame_facts(folder) is not None, "F3 ⑦ load_frame_facts 读得出夹具的 看帧.json")
    check(extract.load_frame_facts(tmp / "nope") is None, "F3 ⑧ 没有这个文件时返回 None（跳过注入）")

    broken = tmp / "broken"
    broken.mkdir(parents=True, exist_ok=True)
    (broken / "看帧.json").write_text("{不是 JSON", encoding="utf-8")
    check(extract.load_frame_facts(broken) is None, "F3 ⑨ 看帧.json 坏了 → 跳过注入，不炸拆解")

    # ⑩ 真入口：run_extract 拼出来的那段提示词里必须有这块（调用层换成替身，不联网）
    outdir = tmp / "ff-entry"
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "看帧.json").write_text(json.dumps(facts, ensure_ascii=False), encoding="utf-8")
    captured: dict = {}
    real_call, real_probe = extract.call_model, extract.probe_video

    def fake_probe(path, timeout=60.0):
        return {"duration": 1.0, "has_video": True, "has_audio": True, "width": 1080,
                "height": 1888, "fps": 30.0, "aspect": "9:16",
                "codec_types": ["video", "audio"], "stderr": "", "hard_cuts": []}

    def fake_call(prompt, path, key, timeout=300.0):
        captured["prompt"] = prompt
        return "\n\n".join(f"## {title}\n（占位正文）" for title in extract.LAYER_TITLES) + "\n", {}, 0.1

    try:
        extract.call_model, extract.probe_video = fake_call, fake_probe
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = extract.run_extract(Path("fake.mp4"), "ff-entry", outdir, 5.0, "")
    finally:
        extract.call_model, extract.probe_video = real_call, real_probe
    check(code == 0 and "## 看帧判定" in captured.get("prompt", ""),
          "F3 ⑩ 真入口 run_extract 也注入（拼给模型的提示词里有这块，不是只在函数里）",
          f"exit={code} 提示词 {len(captured.get('prompt', ''))} 字")


# ------------------------------------------------------------------ F4


def f4_conflict(mod, extract, tmp: Path) -> None:
    """看帧判定说正对、首帧行写相反那一头 → 记冲突并进清单；一致时 0 冲突。

    夹具现造：拆解模板里首帧句 = 「背对镜头，侧身向右转头，视线看向镜头」，
    OPPOSITE_FACTS 的 0 秒判定 = 「正对镜头」。二者是真相反（正对 ↔ 背对）。
    """
    opposite = json.loads(json.dumps(OPPOSITE_FACTS, ensure_ascii=False))
    buggy = fixture_text()
    check(BUGGY_LINE in buggy, "F4 ① 夹具：首帧行是「背对镜头，侧身向右转头，视线看向镜头」")

    conflicts = extract.check_facts(buggy, [], opposite)
    check(any("看帧" in c for c in conflicts),
          "F4 ② check_facts() 记下与看帧判定的冲突", "｜".join(conflicts)[:160])
    check(any("背对" in c and "正对" in c for c in conflicts),
          "F4 ③ 冲突点明了「背对 vs 正对」这两头")

    folder = make_task(tmp, "ff-conflict", buggy, opposite)
    items = mod.detect(str(folder))
    first = next((i for i in items if i.get("id") == "firstframe-orientation"), None)
    check(first is not None, "F4 ④ recheck 把它列进清单",
          f"id={[i['id'] for i in items]}")
    if first:
        check("看帧判定" in first.get("why", ""), "F4 ⑤ 清单里的 why 指名了看帧判定",
              first.get("why", "")[:120])
        check(first.get("status") == "pending", "F4 ⑥ 新发现的条目状态是 pending",
              first.get("status"))
        check(first.get("suggest", "").startswith("首帧：")
              and str(judgment_at(opposite, 0.0).get("描述") or "") in first.get("suggest", ""),
              "F4 ⑦ 建议值优先用看帧判定的 0 秒描述", first.get("suggest", "")[:80])

    listing = run(["pipeline/recheck.py", "--id", str(folder)])
    check(listing.returncode == 0 and "看帧判定" in listing.stdout,
          "F4 ⑧ 命令行列清单也看得到这一条", f"exit={listing.returncode}")

    agree = fixture_text(AGREE_LINE)
    check(extract.check_facts(agree, [], opposite) == [],
          "F4 ⑨ 首帧行与看帧判定一致时 0 冲突（不误报）",
          "｜".join(extract.check_facts(agree, [], opposite))[:160])
    plain = make_task(tmp, "ff-agree", agree, opposite)
    check(not [i for i in mod.detect(str(plain)) if i.get("id") == "firstframe-orientation"],
          "F4 ⑩ 一致时清单里不冒这一条")


# ------------------------------------------------------------------ F5


def f5_ledger(mod, tmp: Path) -> None:
    """use 之后 original 保留改前原句、applied 存新文案、decidedAt 记时间。"""
    facts = OPPOSITE_FACTS
    base = fixture_text()

    # ① 改成一个「仍然与看帧判定对不上」的样子：现算还会命中同一条 → 走 hit 分支（就是踩坑那条）
    folder = make_task(tmp, "ff-use-hit", base, facts)
    before = next(line for line in base.splitlines() if line.startswith("- 首帧："))
    before_body = before[2:].strip()
    new_text = "首帧：人物背对镜头站在车厢中央，视线投向画面左侧。"
    mod.apply(str(folder), "firstframe-orientation", "use", new_text)
    ledger = json.loads((folder / "复检清单.json").read_text(encoding="utf-8"))
    item = next((i for i in ledger if i.get("id") == "firstframe-orientation"), None) or {}
    check(item.get("original") == before_body,
          "F5 ① original 逐字等于改前原句", f"{item.get('original')!r}")
    check(item.get("applied") == new_text, "F5 ② applied 逐字等于新句", f"{item.get('applied')!r}")
    check(bool(item.get("decidedAt")), "F5 ③ decidedAt 有值", str(item.get("decidedAt")))
    check("text" not in item, "F5 ④ 不再写旧的 text 字段（改前改后分两个字段）",
          str(sorted(item)))
    now_line = next((line for line in (folder / "拆解.md").read_text(encoding="utf-8").splitlines()
                     if line.startswith("- 首帧：")), "")
    check(now_line.strip() == "- " + new_text, "F5 ⑤ 拆解.md 那一行真的是新句", now_line)

    # ② 改成与看帧判定一致 → 冲突消失，走「没有这条了」分支，留痕同样要齐
    folder2 = make_task(tmp, "ff-use-clean", base, facts)
    clean_text = "首帧：" + str(judgment_at(facts, 0.0).get("描述") or "侧对镜头")
    mod.apply(str(folder2), "firstframe-orientation", "use", clean_text)
    ledger2 = json.loads((folder2 / "复检清单.json").read_text(encoding="utf-8"))
    item2 = next((i for i in ledger2 if i.get("id") == "firstframe-orientation"), {}) or {}
    check(item2.get("status") == "replaced" and item2.get("original") == before_body
          and item2.get("applied") == clean_text,
          "F5 ⑥ 冲突改没了的路径也留住改前/改后", json.dumps(item2, ensure_ascii=False)[:120])
    check(not [i for i in mod.detect(str(folder2)) if i.get("id") == "firstframe-orientation"],
          "F5 ⑦ 改完冲突真的消失（清单不再现算这一条）")


# ------------------------------------------------------------------ B3 ①–⑤ 投票与口径


def vote_json(facing: str, description: str, posture: str = "左手抬到胸前握拳",
              confidence: str = "高") -> str:
    """一次判定的模型正文（测试夹具）：朝向只给自由描述词，facing_class 由解析层补。"""
    return json.dumps({
        "frame": 0, "people": 1, "subject": "年轻女性",
        "position": {"where": "画面中央偏右", "height_ratio": "占画面高度约3/4"},
        "body_orientation": {"facing": facing, "back_toward": "画面右侧"},
        "head_orientation": "向左回转，面向镜头",
        "gaze": "直视镜头",
        "posture_action": posture,
        "shot_size": "中景", "objects": [],
        "confidence": confidence, "描述": description,
    }, ensure_ascii=False)


BRIEF = "她侧对镜头站着。"
RICH = "她站在过道中间，左手抬到胸前握拳，头回转过来看向镜头，顶灯从上方落下。"


def vote_payload(judgment: dict) -> dict:
    return {"id": "ff-vote", "源视频": "fake.mp4", "源片尺寸": {"宽": 1080, "高": 1888},
            "帧": [{"秒": 0.0, "文件": "analysis/reverse/ff-vote/看帧/0.png",
                    "宽": 1080, "高": 1888, "判定": judgment}]}


def judging(mod, answers: list[str]) -> tuple[dict, str, int]:
    """按顺序喂 `answers` 给 judge_frame，返回 (判定, 错误, 实际调用次数)。"""
    seq = iter(answers)
    calls: list[int] = []

    def call(_prompt, _path):
        calls.append(1)
        return next(seq), {}, 0.1

    judgment, error = mod.judge_frame(call, "prompt", Path("0.png"), votes=len(answers))
    return judgment, error, len(calls)


def frame_record(seconds: float, judgment: dict) -> dict:
    """看帧.json 里的一帧记录（窗口的「帧」数组用这个形状）。"""
    return {"秒": seconds, "文件": f"analysis/reverse/ff-window/看帧/{seconds:g}.png",
            "sha256": "a" * 64, "宽": 1080, "高": 1888, "判定": judgment}


def window_payload(judgments: list[tuple[float, dict]], aggregated: dict) -> dict:
    """新格式 看帧.json 夹具：一个窗口（0:0.5）+ 聚合 + 扁平的「帧」。"""
    frames = [frame_record(seconds, judgment) for seconds, judgment in judgments]
    return {"id": "ff-window", "源视频": "fake.mp4",
            "源片尺寸": {"宽": 1080, "高": 1888},
            "窗口": [{"窗口": "0:0.5", "起": 0.0, "止": 0.5, "帧": frames,
                      "聚合": aggregated}],
            "帧": frames}


def judged(mod, facing: str, description: str, posture: str = "左手抬到胸前握拳") -> dict:
    """一次判定的产物（走真解析层，facing_class 由 parse_judgment 补）。"""
    return mod.parse_judgment(vote_json(facing, description, posture=posture))


def window_sampling(mod, tmp: Path) -> None:
    """B3·窗口 ① 0:0.5 + n=3 → 三帧、秒数递增、含首尾、原分辨率。"""
    check(mod.parse_window("0:0.5") == (0.0, 0.5),
          "B3·窗口 ① --window 0:0.5 → (0.0, 0.5)", str(mod.parse_window("0:0.5")))
    check(mod.parse_window("0-0.5") == (0.0, 0.5) and mod.parse_window("2:2.5s") == (2.0, 2.5),
          "B3·窗口 ① 0-0.5 / 2:2.5s 也认（写法宽容，值不猜）",
          f"{mod.parse_window('0-0.5')} / {mod.parse_window('2:2.5s')}")
    try:
        mod.parse_window("0.5:0.2")
        check(False, "B3·窗口 ① 止秒小于起秒必须报错")
    except mod.FrameFactsError as exc:
        check("止秒" in str(exc), "B3·窗口 ① 止秒小于起秒 → 报错不猜", str(exc)[:40])

    times = mod.window_times(0.0, 0.5, 3)
    check(times == [0.0, 0.25, 0.5], "B3·窗口 ① 0:0.5 + 3 帧 = 0 / 0.25 / 0.5", str(times))
    check(times == sorted(times) and len(set(times)) == 3
          and times[0] == 0.0 and times[-1] == 0.5,
          "B3·窗口 ① 秒数递增、含首尾、三帧互不相同")
    check(mod.window_times(1.0, 1.5, 2) == [1.0, 1.5], "B3·窗口 ① n=2 取首尾两点",
          str(mod.window_times(1.0, 1.5, 2)))
    check(mod.window_times(0.0, 0.5, 1) == [0.0], "B3·窗口 ① n=1 只取起点（老口径单帧）")
    clamped = mod.window_times(0.0, 0.5, 3, duration=0.3)
    check(clamped and clamped[0] == 0.0 and max(clamped) <= 0.25 and clamped == sorted(clamped),
          "B3·窗口 ① 窗口越过片尾 → 收到最后一帧能抽的位置（不炸整次）", str(clamped))

    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        skip("B3·窗口 ① 真抽三帧仍是原分辨率", "本机没有 ffmpeg/ffprobe")
        return
    video = tmp / "window-src.mp4"
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                           "-i", "testsrc2=s=1080x1888:r=30:d=1", "-pix_fmt", "yuv420p",
                           "-y", str(video)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0 or not video.is_file():
        skip("B3·窗口 ① 真抽三帧仍是原分辨率", f"造不出测试视频：{(proc.stderr or '')[:80]}")
        return
    source = mod.probe_size(video)
    grabbed = [(seconds, mod.grab_frame(video, seconds,
                                        tmp / "window-frames" / f"{seconds:g}.png", source))
               for seconds in times]
    check([seconds for seconds, _ in grabbed] == times and len(grabbed) == 3,
          "B3·窗口 ① 真抽三帧，秒数就是 0 / 0.25 / 0.5", str([s for s, _ in grabbed]))
    check(all((item["宽"], item["高"]) == source for _, item in grabbed),
          "B3·窗口 ① 三帧都是原分辨率（等于源片，不是缩略图）",
          f"源片 {source[0]}×{source[1]}｜帧 {[item['宽'] for _, item in grabbed]}")
    check(len({item["sha256"] for _, item in grabbed}) == 3,
          "B3·窗口 ① 三帧各是各的画面（不同时刻，不是同一张图）")


def window_aggregation(mod, extract) -> None:
    """B3·聚合 ② 全一致 → 高；两同一异 → 低 + 分歧列全；描述取信息最多那帧并标秒。"""
    agree = [(0.0, judged(mod, "侧背对镜头", BRIEF, posture="")),
             (0.25, judged(mod, "侧背对镜头", BRIEF, posture="")),
             (0.5, judged(mod, "侧背对镜头", BRIEF, posture=""))]
    aggregated = mod.finalize(mod.aggregate(agree))
    check(aggregated.get("confidence") == "高"
          and aggregated.get("body_orientation", {}).get("facing_class") == "侧背",
          "B3·聚合 ② 三帧都说「侧背」→ confidence=高、取该值",
          f"{aggregated.get('confidence')} / {aggregated.get('body_orientation', {}).get('facing_class')}")
    check(not aggregated.get("分歧") and aggregated.get("帧数") == 3,
          "B3·聚合 ② 一致时不写分歧、帧数=3", str(aggregated.get("分歧")))
    check(aggregated.get("gaze") == "直视镜头" and aggregated.get("people") == 1
          and aggregated.get("shot_size") == "中景",
          "B3·聚合 ② 视线 / 人数 / 景别都取到一致值",
          f"{aggregated.get('gaze')} / {aggregated.get('people')} / {aggregated.get('shot_size')}")

    mixed = [(0.0, judged(mod, "侧对镜头", BRIEF, posture="")),
             (0.25, judged(mod, "侧背对镜头", RICH, posture="左臂抬到胸前握拳")),
             (0.5, judged(mod, "背对镜头", BRIEF, posture=""))]
    low = mod.finalize(mod.aggregate(mixed))
    check(low.get("confidence") == "低" and low.get("body_orientation", {}).get("facing_class") == "歧",
          "B3·聚合 ② 两同一异 → confidence=低、该字段写「歧」",
          f"{low.get('confidence')} / {low.get('body_orientation', {}).get('facing_class')}")
    line = (low.get("分歧") or [""])[0]
    check("0 秒「侧对」" in line and "0.25 秒「侧背」" in line and "0.5 秒「背对」" in line,
          "B3·聚合 ② 分歧把三帧说法按秒全列出来（不合成、不平均）", line)
    check(low.get("分歧值", {}).get("facing_class") == ["侧对", "侧背", "背对"],
          "B3·聚合 ② 分歧值按出现顺序留档", str(low.get("分歧值")))
    check(low.get("描述") == RICH and low.get("描述秒") == 0.25,
          "B3·聚合 ② 描述取信息最多那帧，并标明来自 0.25 秒",
          f"{str(low.get('描述'))[:24]}｜来自 {low.get('描述秒')}")
    note = mod.window_note(low)
    check("三帧" in note and "侧对／侧背／背对" in note and "不一，以描述为准" in note,
          "B3·聚合 ② 低置信窗口的一句实话", note)

    broken = [(0.0, judged(mod, "侧背对镜头", BRIEF)),
              (0.5, mod.failed_judgment("调用失败：超时"))]
    partial = mod.finalize(mod.aggregate(broken))
    check(partial.get("confidence") == "低"
          and any("0.5 秒这一帧没判成" in line for line in partial.get("分歧") or []),
          "B3·聚合 ② 有帧没判成 → 低置信，并在分歧里写明哪一秒",
          "｜".join(partial.get("分歧") or [])[:80])


def window_injection(mod, extract) -> None:
    """B3·注入 ③ 低置信窗口：写「不一，以描述为准」，并标出描述来自哪一秒。"""
    mixed = [(0.0, judged(mod, "侧对镜头", BRIEF, posture="")),
             (0.25, judged(mod, "侧背对镜头", RICH, posture="左臂抬到胸前握拳")),
             (0.5, judged(mod, "背对镜头", BRIEF, posture=""))]
    payload = window_payload(mixed, mod.finalize(mod.aggregate(mixed)))
    picked = extract._pick_inject(payload, "first") or {}
    check([window.get("窗口") for window in picked.get("窗口") or []] == ["0:0.5"],
          "B3·注入 ③ first 只留首帧那个窗口（0:0.5）",
          str([window.get("窗口") for window in picked.get("窗口") or []]))
    block = extract.build_frame_facts_block(picked)
    check(block.count("\n- ") == 1, "B3·注入 ③ 注入块只有一个窗口一行", f"{block.count(chr(10) + '- ')} 行")
    check("0.0–0.5 秒（3 帧）" in block, "B3·注入 ③ 行首写明起止秒与帧数", block.splitlines()[-1][:48])
    check("这一窗口三帧里 侧对／侧背／背对 不一，以描述为准" in block,
          "B3·注入 ③ 低置信窗口写「不一，以描述为准」", block.splitlines()[-1][:110])
    check("这话来自 0.25 秒那一帧" in block and RICH in block,
          "B3·注入 ③ 描述来自哪一秒写得出来（0.25 秒）")
    check("class=歧" in block and "body=歧" in block,
          "B3·注入 ③ 没共识的字段照实写「歧」，不替模型挑一个", block.splitlines()[-1][-60:])
    check(extract.frame_facts_conflicts("首帧：人物正对镜头站着。", payload) == [],
          "B3·注入 ③ 聚合低置信时朝向不参与冲突（只提示）")


def legacy_votes(mod, extract, tmp: Path) -> None:
    """B3·兼容 ④ 同一帧不再靠投票定置信度；老格式（单帧、无窗口、有 votes）能读能注入。"""
    single, error, calls = judging(mod, [vote_json("侧背对镜头", RICH, posture="左臂抬到胸前")])
    check(calls == 1 and not error and not single.get("votes"),
          "B3·兼容 ④ judge_frame 默认只问一次、不写 votes",
          f"{calls} 次 / votes={len(single.get('votes') or [])} / {error}")

    three, _err3, calls3 = judging(mod, [
        vote_json("侧对镜头", BRIEF, posture=""),
        vote_json("侧背对镜头", RICH),
        vote_json("背对镜头", RICH),
    ])
    check(calls3 == 3 and len(three.get("votes") or []) == 3,
          "B3·兼容 ④ --votes 3 真的问 3 次，三次读数都留痕", f"{calls3} 次")
    check(three.get("confidence") == "高",
          "B3·兼容 ④ 同一帧三次档位不一也**不**因此记低置信（投票不再是置信度依据）",
          f"{three.get('confidence')}｜{mod.vote_note(three) or '无提示'}")
    check(mod.clamp_votes(3) == 3 and mod.clamp_votes(9) == 3 and mod.clamp_votes(1) == 1,
          "B3·兼容 ④ --votes 3 / 9 / 1 取值 = 3 / 3 / 1",
          f"{mod.clamp_votes(3)} / {mod.clamp_votes(9)} / {mod.clamp_votes(1)}")

    # 老格式：无「窗口」、只有「帧」、带 votes（两次判定不一）
    old_judgment = {
        "people": 1, "subject": "年轻女性",
        "body_orientation": {"facing": "背对镜头", "back_toward": "画面右侧"},
        "gaze": "直视镜头", "posture_action": "右手扶着扶手", "shot_size": "中景",
        "confidence": "高",
        "描述": "车厢里她背对镜头站着，右手扶着扶手，顶灯从上方落下。",
        "votes": [
            {"facing": "侧对镜头", "facing_class": "侧对", "confidence": "高", "描述": "她侧对镜头。"},
            {"facing": "背对镜头", "facing_class": "背对", "confidence": "高", "描述": "她背对镜头。"},
        ],
    }
    old_payload = vote_payload(old_judgment)
    folder = make_task(tmp, "ff-legacy-votes", "x", old_payload)
    loaded = mod.load_facts(folder)
    check(loaded is not None and mod.usable_entry(mod.seconds_entry(loaded, 0.0)),
          "B3·兼容 ④ 老格式（无窗口、有 votes）仍能读、仍算「已有可用判定」")
    block = extract.build_frame_facts_block(loaded)
    check("body=背对镜头" in block and "class=背对" in block,
          "B3·兼容 ④ 老格式能注入：body / class 都在", block.splitlines()[-1][:80])
    check("两次判定不一" in block,
          "B3·兼容 ④ 老记录的「两次判定不一」提示照旧保留", block.splitlines()[-1][:100])
    check(extract.frame_facts_conflicts("首帧：人物正对镜头站在过道中央。", loaded) == [],
          "B3·兼容 ④ 老记录两次判定不一 → 朝向不参与冲突（与旧行为一致）")

    # 老格式但没有 votes：照样参与冲突判定（真相反要报）
    plain = dict(old_judgment)
    plain.pop("votes")
    plain_payload = vote_payload(plain)
    plain_conflicts = extract.frame_facts_conflicts("首帧：人物正对镜头站在过道中央。",
                                                    plain_payload)
    check(len(plain_conflicts) == 1 and "朝向对不上" in plain_conflicts[0],
          "B3·兼容 ④ 老格式没有 votes → 照旧参与冲突判定（背对 vs 正对）",
          "｜".join(plain_conflicts)[:90])
    check("老" not in str(mod.content_digest(plain_payload)) and mod.content_digest(plain_payload),
          "B3·兼容 ④ 老格式的内容摘要照旧算得出来（指纹不因改读法失效）",
          str(mod.content_digest(plain_payload))[:12])


def conflict_tolerance(extract) -> None:
    """B3 ③ 冲突只认真相反：邻近态不报，正对 ↔ 背对 才报。"""

    def facts_with(facing: str, face_class: str | None = None) -> dict:
        body = {"facing": facing, "back_toward": "画面右侧"}
        if face_class:
            body["facing_class"] = face_class
        return {"帧": [{"秒": 0.0, "判定": {"body_orientation": body, "gaze": "直视镜头",
                                            "描述": "她站在过道中间。"}}]}

    near1 = extract.frame_facts_conflicts("首帧：人物背对镜头站在车厢中央，手扶着扶手。",
                                         facts_with("侧背对镜头", "侧背"))
    check(near1 == [], "B3 ③ 拆解「背对」+ 判定「侧背」→ 不报冲突（邻近态）",
          "｜".join(near1)[:90])
    near2 = extract.frame_facts_conflicts("首帧：人物侧对镜头站在车厢中央。",
                                         facts_with("侧背对镜头", "侧背"))
    check(near2 == [], "B3 ③ 拆解「侧对」+ 判定「侧背」→ 不报冲突（邻近态）",
          "｜".join(near2)[:90])
    near3 = extract.frame_facts_conflicts("首帧：人物侧身偏右转正。",
                                         facts_with("侧对镜头", "侧对"))
    check(near3 == [], "B3 ③ 同一档（侧对 / 侧对）→ 不报冲突", "｜".join(near3)[:90])
    opposite = extract.frame_facts_conflicts("首帧：人物正对镜头站在车厢中央。",
                                            facts_with("背对镜头", "背对"))
    check(len(opposite) == 1 and "朝向对不上" in opposite[0],
          "B3 ③ 拆解「正对」+ 判定「背对」→ 报冲突（真相反）", "｜".join(opposite)[:90])
    unknown = extract.frame_facts_conflicts("首帧：人物正对镜头站着。", facts_with("不确定镜头"))
    check(unknown == [], "B3 ③ 判定「不确定」→ 不报冲突（不拿没看清的扣帽子）",
          "｜".join(unknown)[:90])


# ------------------------------------------------------------------ 护栏


def guards(mod, extract, recheck, facts: dict | None, tmp: Path) -> None:
    """① 缺描述=没判成；② 描述不是字段拼接；④ 建议值退回分镜表。"""
    sample = json.dumps({
        "frame": 0, "people": 1, "subject": "年轻女性",
        "body_orientation": {"facing": "侧对镜头", "back_toward": "画面右侧"},
        "gaze": "直视镜头", "posture_action": "右手抬到胸前握拳", "shot_size": "中景",
        "confidence": "高",
    }, ensure_ascii=False)
    try:
        mod.parse_judgment(sample)
        check(False, "护栏① 缺「描述」的 JSON 必须算没判成")
    except ValueError as exc:
        check("描述" in str(exc), "护栏① 缺「描述」的 JSON 被判为没判成", str(exc))

    try:
        mod.parse_judgment("这是一段没有 JSON 的话")
        check(False, "护栏① 没有 JSON 的正文必须报错")
    except ValueError as exc:
        check(True, "护栏① 没有 JSON 的正文报错（不当结果存下）", str(exc)[:60])

    calls: list[int] = []

    def bad_call(prompt: str, path: Path):
        calls.append(1)
        return sample, {}, 0.1                 # 永远缺「描述」

    judgment, error = mod.judge_frame(bad_call, "prompt", Path("0.png"))
    check(len(calls) == 2, "护栏① 解析失败会再问一次（共 2 次调用）", f"{len(calls)} 次")
    check(judgment.get("confidence") == "低" and bool(error),
          "护栏① 两次都不行 → confidence=低 且带原因（不许静默写空）",
          f"{judgment.get('confidence')} / {error}")
    check(bool(judgment.get("错误")) and "描述" in str(judgment.get("错误")),
          "护栏① 低置信度那条把原因也写进产物", str(judgment.get("错误"))[:80])

    description = str(judgment_at(facts, 0.0).get("描述") or "")
    if not description:
        skip("护栏② 真实描述是一句话，不是字段拼接", "真实 看帧.json 没有 0 秒描述")
    else:
        check(len(description) >= 25 and "facing=" not in description and "gaze=" not in description
              and "body=" not in description,
              "护栏② 真实描述是一句话，不是字段拼接", description[:60])

    # ④ 没有 看帧.json → 建议值退回分镜表第 1 镜的「朝向 / 视线」两列
    no_facts = make_task(tmp, "ff-fallback", fixture_text())
    first = next((i for i in recheck.detect(str(no_facts))
                  if i["id"] == "firstframe-orientation"), None)
    check(bool(first) and first.get("suggest") == "首帧：侧身偏右转正，镜头",
          "护栏④ 没有看帧判定时，建议值退回分镜表拼接（口径不变）",
          repr((first or {}).get("suggest")))


# ------------------------------------------------------------------ 主流程


def gaze_relation_caliber(extract) -> None:
    """视线口径：说了看哪儿才算，没说的不许硬判成「不看镜头」。

    2026-11-02 真机：首帧写「转头直视」，判定是「直视镜头」，旧口径把前者判成 away，
    于是凑出一条假冲突「一个看镜头、一个不看」写进产物头部。
    """
    cases = {
        "转头直视": "camera",          # 旧口径在这里回 away
        "目光斜视镜头": "camera",
        "直视镜头": "camera",
        "眼睛直直看着镜头": "camera",
        "低头看手机": "away",
        "视线落在别处": "away",
        "凝视远方": "away",
        "目光移开": "away",
    }
    for text, want in cases.items():
        got = extract.gaze_relation(text)
        check(got == want, f"gaze_relation「{text}」= {want}", f"实际 {got or '（判不出）'}")
    conflict = extract.gaze_relation("转头直视") != extract.gaze_relation("直视镜头")
    check(not conflict, "「转头直视」与「直视镜头」不算冲突")


def main() -> int:
    print("=" * 78)
    print("看帧判定 · 判据 F1–F5（真实任务只读；写回都在临时夹具上做）")
    print(f"仓库：{ROOT}")
    print("=" * 78)

    mod = load_module("frame_facts", FRAME_FACTS)
    extract = load_module("extract", EXTRACT)
    recheck = load_module("recheck", RECHECK)
    facts = real_facts()                                # 真实任务只读；缺了下面走 SKIP

    with tempfile.TemporaryDirectory(prefix="frame-facts-check-") as tmpdir:
        tmp = Path(tmpdir)
        fixture = make_task(tmp, "ff-fixture", fixture_text(), OPPOSITE_FACTS)
        frame_selection(mod, fixture)
        f1_owner_reading(mod, tmp)
        f1_real_soft(facts)
        f2_original_resolution(facts, mod)
        f3_prompt_injection(extract, OWNER_FACTS, tmp)
        f4_conflict(recheck, extract, tmp)
        f5_ledger(recheck, tmp)
        window_sampling(mod, tmp)
        window_aggregation(mod, extract)
        window_injection(mod, extract)
        legacy_votes(mod, extract, tmp)
        conflict_tolerance(extract)
        gaze_relation_caliber(extract)
        guards(mod, extract, recheck, facts, tmp)

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
