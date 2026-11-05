#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T6 验收：第十层「图文与收束」全链机械适配 + 看帧尾帧窗（纯标准库，无 pytest）。

跑法：`py -3 tests/lapian_tenth_layer_check.py`    退出码 0 = 全过，1 = 有 FAIL

判据（docs/nodes/M6-T6尾帧品牌露出.md T6-B/D；prompts/视频拆解.md v2.2.0 第十层）
----------------------------------------------------------------
A  extract 层枚举：LAYER_TITLES 十层齐全（十 图文与收束 在列）；read_prompt 含第十层；
   十层夹具 --check-only 过（exit 0）；九层夹具按缺层失败（exit 4）
B  尾帧窗采样：默认采样 = 首帧窗（0:0.5、3 帧）＋ 尾帧窗（末 1.5 秒、2 帧）；
   看帧 JSON 有尾帧判定且判过「画面里有什么字」→ 提示词注入第十层锚点；
   没有尾帧判定 / 旧记录（没问过文字）→ 提示词与旧版逐字相同（一个字不加）
C  看帧判定图文字段：parse_judgment 两态归一 text_elements（显式空数组=已判真无字、
   漏答=未判不落键）；窗口聚合按「文字+位置」去重、
   旧记录图文已判=False；带字判定聚合出 图文+图文已判
D  confirm：`图文：无…` 是空态行（is_absent_state 命中、不产实体）；十层夹具跑真实
   confirm，清单里没有 图文/收束 实体行，收束信息不污染四类实体；
   品牌视觉行「建议」列直接写「必须换」（D6，T6-G bug①：置换位天然必换）
E  render：十层夹具（图文条目按 prompt 契约五字段竖线格式）× lumi-4section --optional C →
   exit 0，收束段在；品牌位换「客户品牌字样」、包装原字（GLOW WATER）不残留（B2）；
   字幕条目按层来源保留进正文（B1）、文案位换「客户文案（定稿后填入）」（B3）；
   「部分可辨」标记保留；资产清单有「品牌视觉（置换位）」行（B4）；
   不开可选段时参考字样/参考文案不进正文；
   字典式置换（T6 真机泄漏两处）：图文条目「位置」字段裸嵌的原品牌字（E6）、
   场景/产品等其他层描述里照抄的已登记字样（E7）与标语类字样（E7b）都不残留，
   不开可选段同样生效（E5）；
   资产清单消费端（T6-G）：品牌视觉行来自清单实体且恰好一行（E3f）、
   清单描述原字零残留+置换两态（E3g/E3h，bug②：道具行「表面有「ZING!」字样」）、
   「建议」列清空仍收行（E8，bug①）、行被人工删掉十层兜底仍在（E9）
F  投影：render_lapian 十层夹具 → DNA「片尾收束」块带收束三件套与图文条目；
   无品牌夹具 → `图文：无` 照实投影；九层夹具 → 片尾收束〔未取证〕；
   拉片是 IR 投影，原字照实保留、不做任何置换（F3 即证）

夹具全部现造（tempfile），不读真实任务、不联网、不调模型。
"""
from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import contextlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline" / "reverse"))

import extract as E  # noqa: E402
import confirm as C  # noqa: E402
import frame_facts as FF  # noqa: E402

results: list[tuple[bool, str]] = []

BRAND_TAIL_LAYER = """## 十 图文与收束

* **品牌字（出现于：00:04-00:05）**：「SPARK!」｜粗体｜白色｜顶部居中｜淡入
* **包装字（出现于：00:00-00:05）**：「GLOW WATER」｜细体｜白色｜瓶身右侧｜无
* **包装字Z（出现于：00:00-00:05）**：「ZING!」｜细体｜白色｜灯箱正面｜无
* **字幕（出现于：00:02-00:05）**：「全场 5 折」｜粗体｜黄色｜底部｜弹出
* **水印（出现于：全片）**：「部分可辨：EAU …」｜细体｜灰色｜右上｜无
* **标语字（出现于：00:04-00:05）**：「BIG SALE」｜粗体｜红色｜顶部SPARK!正下方｜淡入
* **收束**：类型=产品与品牌字同框；版式=产品垂直居中悬浮、品牌字顶部居中、背景为虚化街景实拍；动效时序=产品翻转减速先定格（约 0.5 秒），品牌字随后淡入（约 0.3 秒）"""

NOBRAND_TAIL_LAYER = """## 十 图文与收束

* 图文：无（全片画面无持久性文字元素）
* **收束**：类型=无品牌露出；片尾（00:05-00:06）产品静置于台面中央，画面渐暗收束"""

NINE_LAYERS = ("一 场景", "二 产品", "三 运动", "四 光影", "五 镜头",
               "六 意境", "七 分镜表", "八 声音", "九 资产")

DISSECTION_TEMPLATE = """<!-- 视频: 夹具.mp4 | 时长: 6.00s | 音轨: 无 -->
> 规格：1080×1920 · 9:16 竖屏 · 6.00 秒 · 硬切 0 次

## 一 场景
米白哑光台面，均匀柔光，四周留白干净，背景立着印有「ZING!」字样的白色灯箱。

## 二 产品
1. 台面中央陈列的圆柱容器，哑光白，无标签。

## 三 运动
运动主体：无。全片静置，只有末段产品定格。

## 四 光影
顶部柔光，阴影淡，亮度全程稳定。

## 五 镜头
固定机位，微俯拍，中景，景深均匀。

## 六 意境
整体调性：干净克制。情绪基调：松弛，节奏平缓，重音落在末帧定格。

## 七 分镜表
- 一镜到底：是
- 首帧：产品静置台面中央，无人物。
镜号 | 起秒-止秒 | 运动主体 | 运动形态 | 速度 | 画面内容 | 光影变化 | 转场方式 | 站位 | 朝向 | 视线 | 镜头运动 | 景别 | 情绪/表演
1 | 00:00-00:06 | 无人物 | 静置 | 恒定 | 产品居中定格 | 亮度稳定 | 无剪切 | 无人物 | — | — | 固定 | 中景 | 无可见表情（无人物）

## 八 声音
人声语言：无台词。BGM：轻钢琴，末段渐弱收尾。

## 九 资产
* **道具A（出现于：全片）**：台面中央的圆柱容器，哑光白
* 场景A（出现于：全片）：米白哑光台面，均匀柔光
{tail_layer}
"""

CHECKLIST = """# 待确认清单 · 夹具

| # | 类别 | 模型看到什么 | 命名 | 资产图 | 锁什么 | 建议 | 用我的描述 |
|---|------|-------------|------|--------|--------|------|------------|
| 1 | 主体 | 台面中央陈列的圆柱容器，哑光白 | 产品 | tests/fixtures/占位.png | 外形比例 | | |
| 2 | 道具 | A：台面上的白色灯箱，表面有「ZING!」字样，底部字幕条「全场 5 折」 |  |  | 颜色、支点位置、圆弧幅度 | | |
| 3 | 品牌视觉（置换位） | 参考片品牌位 品牌字（出现于：00:04-00:05）：「SPARK!」｜顶部居中｜淡入（原字已置换，不要求备参考片品牌图） |  |  | 客户品牌字样（待填） | | |
"""

TAIL_JUDGMENT = {
    "frame": 14.95, "people": 0, "subject": "无人物",
    "body_orientation": {"facing": "—", "facing_class": "不确定", "back_toward": ""},
    "gaze": "—", "posture_action": "—", "shot_size": "中景",
    "objects": ["台面中央的圆柱容器"],
    "text_elements": [
        {"文字": "SPARK!", "位置": "顶部居中", "形态": "白色无衬线粗体大字", "动效": "淡入"},
    ],
    "confidence": "高",
    "描述": "产品垂直居中定格，顶部悬浮白色粗体大字。",
}


def check(ok: bool, name: str, detail: str = "") -> bool:
    results.append((ok, name))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"｜{detail}" if detail else ""))
    return ok


def make_task(root: Path, name: str, tail_layer: str) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    text = DISSECTION_TEMPLATE.format(tail_layer=tail_layer)
    (d / "拆解.md").write_text(text, encoding="utf-8", newline="\n")
    (d / "待确认清单.md").write_text(CHECKLIST, encoding="utf-8", newline="\n")
    return d


def make_tail_facts(seconds=(13.5, 14.95), with_text=True) -> dict:
    """看帧.json 夹具：只有尾帧窗（2 帧），末窗判定可带 text_elements。"""
    frames = []
    for s in seconds:
        judgment = json.loads(json.dumps(TAIL_JUDGMENT, ensure_ascii=False))
        if not with_text:
            judgment.pop("text_elements", None)
        judgment["frame"] = s
        frames.append({"秒": s, "文件": f"看帧/{s:g}.png", "sha256": "a" * 64,
                       "宽": 1080, "高": 1920, "判定": judgment})
    agg = FF.finalize(FF.aggregate([(f["秒"], f["判定"]) for f in frames]))
    return {"id": "fx", "源视频": "fx.mp4",
            "源片尺寸": {"宽": 1080, "高": 1920},
            "窗口": [{"窗口": "13.5:15", "起": 13.5, "止": 15.0, "n": 2,
                      "帧": frames, "聚合": agg, "模型": "test", "时间": "2026-11-04 10:00:00"}],
            "帧": frames}


# ---------------------------------------------------------------- A 层枚举

def section_a() -> None:
    check(len(E.LAYER_TITLES) == 10 and E.LAYER_TITLES[-1] == "十 图文与收束",
          "A1 LAYER_TITLES 十层齐全，第十层逐字是「十 图文与收束」", str(E.LAYER_TITLES[-1]))
    prompt = E.read_prompt()
    check("## 十 图文与收束" in prompt, "A2 read_prompt 的围栏正文含第十层（单一真源已升位）")
    with tempfile.TemporaryDirectory() as td:
        d10 = make_task(Path(td), "ten", BRAND_TAIL_LAYER)
        r10 = subprocess.run([sys.executable, str(ROOT / "pipeline" / "reverse" / "extract.py"),
                              "--check-only", str(d10 / "拆解.md")],
                             capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT))
        check(r10.returncode == 0 and "十层齐全且非空" in r10.stdout,
              "A3 十层夹具 --check-only 通过", (r10.stderr.strip().splitlines() or [""])[-1][:90])
        d9 = make_task(Path(td), "nine", "")
        r9 = subprocess.run([sys.executable, str(ROOT / "pipeline" / "reverse" / "extract.py"),
                             "--check-only", str(d9 / "拆解.md")],
                            capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT))
        check(r9.returncode == 4 and "缺第 10 层" in r9.stderr,
              "A4 九层旧夹具按缺第十层失败（exit 4，不静默）",
              (r9.stderr.strip().splitlines() or [""])[-1][:90])


# ---------------------------------------------------------------- B 尾帧窗与注入

def section_b() -> None:
    probe = {"duration": 15.0}
    windows, note = E.frames_of("", Path("."), probe)
    check([(w["窗口"], w["n"], w["帧秒"]) for w in (windows or [])]
          == [("0:0.5", 3, [0.0, 0.25, 0.5]), ("13.5:15", 2, [13.5, 14.95])],
          "B1 默认采样 = 首帧窗 3 帧 ＋ 尾帧窗（末 1.5 秒、2 帧）", note)

    base = E.read_prompt()
    facts = make_tail_facts()
    with_text = E.build_prompt(base, "", {"duration": 15.0}, facts)
    check("## 尾帧判定" in with_text and "「SPARK!」" in with_text and "位置=顶部居中" in with_text,
          "B2 有尾帧判定且判过文字 → 注入第十层锚点（字样/位置在）")
    check(E.build_prompt(base, "", {"duration": 15.0}, facts, inject="off") == E.build_prompt(base, "", {"duration": 15.0}, None),
          "B3 --inject off 时一个字不加（锚点也关）")
    old_facts = make_tail_facts(with_text=False)
    no_facts = E.build_prompt(base, "", {"duration": 15.0}, None)
    without = E.build_prompt(base, "", {"duration": 15.0}, old_facts)
    check("## 尾帧判定" not in without and without == no_facts,
          "B4 旧记录（没问过文字）→ 提示词与「无判定」版逐字相同（没有则一个字不加）")
    mid = make_tail_facts()
    mid["窗口"][0]["止"] = 5.0
    mid["窗口"][0]["起"] = 3.5
    mid["窗口"][0]["窗口"] = "3.5:5"
    check(E.build_tail_brand_block(mid, 15.0) == "",
          "B5 末窗止秒离片长太远 → 不算尾帧判定，不注入")


# ---------------------------------------------------------------- C 看帧图文字段

def section_c() -> None:
    raw = json.dumps(TAIL_JUDGMENT, ensure_ascii=False)
    judgment = FF.parse_judgment(raw)
    check(judgment["text_elements"][0]["文字"] == "SPARK!"
          and judgment["text_elements"][0]["位置"] == "顶部居中",
          "C1 parse_judgment 归一 text_elements（文字/位置/形态/动效）")
    legacy = json.loads(raw)
    legacy.pop("text_elements")
    missed = FF.parse_judgment(json.dumps(legacy, ensure_ascii=False))
    check("text_elements" not in missed and bool(str(missed.get("描述") or "").strip()),
          "C2 模型漏答 text_elements = 未判（不落键，整帧不失败）——不冒充「看过无字」")
    explicit_empty = FF.parse_judgment(json.dumps(
        {**json.loads(raw), "text_elements": []}, ensure_ascii=False))
    check(explicit_empty.get("text_elements") == [],
          "C2-b 显式答空数组 = 已判真无字（键照落，聚合才可设图文已判）")
    agg = FF.finalize(FF.aggregate([(14.5, judgment), (14.95, judgment)]))
    check(agg.get("图文已判") is True and len(agg.get("图文") or []) == 1,
          "C3 两帧看到同一个字 → 按「文字+位置」去重成一条，图文已判=True",
          json.dumps(agg.get("图文"), ensure_ascii=False)[:80])
    old_style = {"people": 0, "subject": "无人物", "body_orientation": {"facing": "—"},
                 "gaze": "—", "posture_action": "—", "shot_size": "中景",
                 "confidence": "高", "描述": "产品定格。"}
    agg_old = FF.finalize(FF.aggregate([(14.5, old_style)]))
    check(agg_old.get("图文已判") is False and agg_old.get("图文") == [],
          "C4 旧记录没问过文字 → 图文已判=False（注入端据此一个字不加）")


# ---------------------------------------------------------------- D confirm

def section_d() -> None:
    absent_line = "* 图文：无（全片画面无持久性文字元素）"
    check(C.is_absent_state(absent_line), "D1 `图文：无…` 判空态命中")
    check(C.extract_assets(absent_line, []) == [], "D2 空态行不产实体")
    check(not C.is_absent_state("**收束**：类型=产品与品牌字同框；版式=…"),
          "D3 收束行不是空态（它是真事实），也不进四类实体")
    with tempfile.TemporaryDirectory() as td:
        for name, layer in (("brand", BRAND_TAIL_LAYER), ("nobrand", NOBRAND_TAIL_LAYER)):
            d = make_task(Path(td), name, layer)
            r = subprocess.run([sys.executable, str(ROOT / "pipeline" / "reverse" / "confirm.py"),
                                str(d / "拆解.md")],
                               capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT))
            sheet = (d / "待确认清单.md").read_text(encoding="utf-8") if (d / "待确认清单.md").is_file() else ""
            check(r.returncode == 0, f"D4 十层夹具 confirm 正常（{name}）",
                  (r.stderr.strip().splitlines() or [""])[-1][:90])
            check("| 图文 |" not in sheet and "| 收束 |" not in sheet
                  and "图文：" not in sheet.replace("模型看到什么", ""),
                  f"D5 清单里没有 图文/收束 实体行（{name}）", sheet[:120].replace("\n", "／"))
            if name == "brand":
                brows = [ln for ln in sheet.splitlines()
                         if ln.strip().startswith("|") and "品牌视觉" in ln]
                bcells = [c.strip() for c in brows[0].strip().strip("|").split("|")] if brows else []
                check(len(bcells) > 6 and bcells[6] == "必须换",
                      "D6 confirm 生成的品牌视觉行「建议」列=必须换（置换位天然必换，T6-G bug①）",
                      "｜".join(bcells))


# ---------------------------------------------------------------- E render

def section_e() -> None:
    with tempfile.TemporaryDirectory() as td:
        d = make_task(Path(td), "render-brand", BRAND_TAIL_LAYER)
        out = Path(td) / "out"
        r = subprocess.run([sys.executable, str(ROOT / "pipeline" / "reverse" / "render.py"),
                            "--id", str(d), "--template", "lumi-4section",
                            "--optional", "C", "--out-dir", str(out)],
                           capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT))
        check(r.returncode == 0, "E1 十层夹具 × lumi --optional C 渲染成功",
              (r.stderr.strip().splitlines() or [""])[-1][:120])
        prompt = (out / "提示词-lumi-4section.md").read_text(encoding="utf-8") if r.returncode == 0 else ""
        check("品牌收束与图文" in prompt, "E2 收束段进产物（可选段 C 打开）")
        body = prompt.split("# 附注")[0]
        check("客户品牌字样" in prompt and "SPARK" not in body,
              "E3 字样已换成「客户品牌字样」占位，参考片原字不进正文", "SPARK 残留" if "SPARK" in prompt else "")
        check("GLOW" not in body and "客户品牌字样" in body,
              "E3b 包装原字（GLOW WATER）不残留——五字段契约夹具在 B2 边界上复现过此泄漏",
              "GLOW 残留" if "GLOW" in body else "")
        check("字幕（出现于" in body and "客户文案（定稿后填入）" in body and "全场 5 折" not in body,
              "E3c 字幕条目按层来源保留进正文（B1），文案位换「客户文案」占位——不误换品牌、也不照搬（B3）",
              body[:160].replace("\n", "／"))
        check("部分可辨" in body and "EAU …" in body,
              "E3d 「部分可辨」不确定性标记原样保留，不被任何占位抹掉（B3）",
              body[:160].replace("\n", "／"))
        check("| 品牌视觉（置换位） |" in (out / "资产清单.md").read_text(encoding="utf-8"),
              "E3e 资产清单有「品牌视觉（置换位）」行，值为「客户品牌字样（待填）」（B4）")
        check("SPARK!" not in body and "顶部「客户品牌字样」正下方" in body,
              "E6 图文条目「位置」字段裸嵌的原品牌字也进字典置换——泄漏不只在「文字内容」引号内（T6 真机泄漏①）",
              body[:220].replace("\n", "／"))
        check("ZING" not in body and "印有「客户品牌字样」字样的白色灯箱" in body,
              "E7 跨层字典置换：场景/产品等其他层描述里照抄的已登记字样同样换占位（T6 真机泄漏②）",
              body[:220].replace("\n", "／"))
        check("BIG SALE" not in body,
              "E7b 标语类字样（文案位）同进字典——跨层、裸现一律换「客户文案」占位，不猜不漏")
        check("收束" in prompt and ("版式" in prompt or "动效" in prompt),
              "E4 收束三件套（类型/版式/动效时序）跟着进段")
        out_plain = Path(td) / "out-plain"
        r2 = subprocess.run([sys.executable, str(ROOT / "pipeline" / "reverse" / "render.py"),
                             "--id", str(d), "--template", "lumi-4section", "--out-dir", str(out_plain)],
                            capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT))
        plain = (out_plain / "提示词-lumi-4section.md").read_text(encoding="utf-8") if r2.returncode == 0 else ""
        check(r2.returncode == 0 and "SPARK" not in plain and "GLOW" not in plain and "全场 5 折" not in plain
              and "ZING" not in plain,
              "E5 默认（不开可选段）渲染成功，参考字样与参考文案也不泄漏进正文（字典收口不开段也生效）")

        # ---- T6-G：资产清单消费端——品牌视觉行落表 + 描述原字置换 -----------------
        sheet = (out / "资产清单.md").read_text(encoding="utf-8") if r.returncode == 0 else ""
        brand_rows = [ln for ln in sheet.splitlines() if "| 品牌视觉（置换位） |" in ln]
        check(len(brand_rows) == 1 and "清单第 3 行" in brand_rows[0] if brand_rows else False,
              "E3f 品牌视觉行来自待确认清单实体（免「建议」条件收行），恰好一行不与十层兜底重复",
              "；".join(brand_rows)[:180])
        leaked = [w for w in ("ZING", "GLOW", "SPARK", "BIG SALE", "全场 5 折") if w in sheet]
        check(not leaked,
              "E3g 资产清单描述原字零残留——道具行「表面有「ZING!」字样」这类照抄同样置换（T6-G bug②）",
              f"残留 {'、'.join(leaked)}" if leaked else sheet[:160].replace("\n", "／"))
        check("客户品牌字样" in sheet and "客户文案（定稿后填入）" in sheet,
              "E3h 清单置换两态：品牌/包装类→客户品牌字样，字幕/标语类→客户文案（定稿后填入）")
        # bug① 回归：品牌行「建议」列被清空（意图回填实况）→ 行仍要进资产表
        cl_path = d / "待确认清单.md"
        blanked = []
        for ln in cl_path.read_text(encoding="utf-8").splitlines():
            if ln.strip().startswith("|") and "品牌视觉" in ln:
                cells = [c.strip() for c in ln.strip().strip("|").split("|")]
                if len(cells) > 6:
                    cells[6] = ""
                ln = "| " + " | ".join(cells) + " |"
            blanked.append(ln)
        cl_path.write_text("\n".join(blanked) + "\n", encoding="utf-8", newline="\n")
        out_ns = Path(td) / "out-nosuggest"
        r_ns = subprocess.run([sys.executable, str(ROOT / "pipeline" / "reverse" / "render.py"),
                               "--id", str(d), "--template", "lumi-4section", "--out-dir", str(out_ns)],
                              capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT))
        sheet_ns = (out_ns / "资产清单.md").read_text(encoding="utf-8") if r_ns.returncode == 0 else ""
        check(r_ns.returncode == 0 and len([x for x in sheet_ns.splitlines()
                                            if "| 品牌视觉（置换位） |" in x]) == 1,
              "E8 清单品牌行「建议」列清空后仍进资产表——消费端免「建议」条件（T6-G bug①：pop2 源片实况）",
              (r_ns.stderr.strip().splitlines() or [""])[:1].__str__())
        # 人工删改鲁棒：品牌行整个删掉 → 十层兜底仍出置换位行
        cl_path.write_text("\n".join(ln for ln in blanked if "品牌视觉" not in ln) + "\n",
                           encoding="utf-8", newline="\n")
        out_dl = Path(td) / "out-deleted"
        r_dl = subprocess.run([sys.executable, str(ROOT / "pipeline" / "reverse" / "render.py"),
                               "--id", str(d), "--template", "lumi-4section", "--out-dir", str(out_dl)],
                              capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT))
        sheet_dl = (out_dl / "资产清单.md").read_text(encoding="utf-8") if r_dl.returncode == 0 else ""
        check(r_dl.returncode == 0 and "| 品牌视觉（置换位） |" in sheet_dl,
              "E9 清单品牌行被人工删掉 → 「十」层兜底仍出置换位行（人工删改后也鲁棒）",
              (r_dl.stderr.strip().splitlines() or [""])[:1].__str__())


# ---------------------------------------------------------------- F 投影

def section_f() -> None:
    render = ROOT / "pipeline" / "reverse" / "render_lapian.py"
    with tempfile.TemporaryDirectory() as td:
        d = make_task(Path(td), "lap-brand", BRAND_TAIL_LAYER)
        r = subprocess.run([sys.executable, str(render), "--id", str(d), "--spec", "jimeng-lapian"],
                           capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT))
        md = (d / "拉片-jimeng-lapian.md").read_text(encoding="utf-8") if r.returncode == 0 else ""
        check(r.returncode == 0, "F1 十层夹具拉片投影成功", (r.stderr.strip().splitlines() or [""])[-1][:90])
        seg = re.search(r"- 片尾收束：(.*?)(?=\n【|\Z)", md, re.S)
        body = seg.group(1) if seg else ""
        check("类型=产品与品牌字同框" in body and "动效时序" in body,
              "F2 DNA「片尾收束」块带收束三件套", body[:80].replace("\n", "／"))
        check("品牌字（出现于" in body and "「SPARK!」" in body,
              "F3 图文条目逐条进收束块（拉片是 IR 投影，照实记录）")
        d2 = make_task(Path(td), "lap-nobrand", NOBRAND_TAIL_LAYER)
        r2 = subprocess.run([sys.executable, str(render), "--id", str(d2), "--spec", "jimeng-lapian"],
                            capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT))
        md2 = (d2 / "拉片-jimeng-lapian.md").read_text(encoding="utf-8") if r2.returncode == 0 else ""
        seg2 = re.search(r"- 片尾收束：(.*?)(?=\n【|\Z)", md2, re.S)
        body2 = seg2.group(1) if seg2 else ""
        check(r2.returncode == 0 and "图文：无" in body2 and "类型=无品牌露出" in body2,
              "F4 无品牌夹具：空态照实投影（图文：无 ＋ 收束=无品牌露出）", body2[:80].replace("\n", "／"))
        d3 = make_task(Path(td), "lap-nine", "")
        r3 = subprocess.run([sys.executable, str(render), "--id", str(d3), "--spec", "jimeng-lapian"],
                            capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT))
        md3 = (d3 / "拉片-jimeng-lapian.md").read_text(encoding="utf-8") if r3.returncode == 0 else ""
        seg3 = re.search(r"- 片尾收束：(.*?)(?=\n【|\Z)", md3, re.S)
        body3 = seg3.group(1) if seg3 else ""
        check(r3.returncode == 0 and "〔未取证〕" in body3,
              "F5 九层旧夹具：片尾收束按〔未取证〕标注，不编不空", body3[:80])


def main() -> int:
    print("=" * 78)
    print("T6 第十层「图文与收束」全链检查（夹具现造，真实链路：extract→confirm→render→投影）")
    print("=" * 78)
    section_a()
    section_b()
    section_c()
    section_d()
    section_e()
    section_f()
    failed = [name for ok, name in results if not ok]
    print("-" * 78)
    print(f"合计 {len(results)} 项：{len(results) - len(failed)} PASS / {len(failed)} FAIL")
    for name in failed:
        print(f"  FAIL：{name}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
