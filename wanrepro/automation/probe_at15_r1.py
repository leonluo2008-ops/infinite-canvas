#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AT-15 R1 探针：拆解产物门禁（D1–D7）＋自动重拆（三态 + 计数落盘）＋复合时段解析。

跑法：`wanrepro/` 下 `python -m automation.probe_at15_r1`（纯离线：无 HTTP、不跑 ffmpeg、
零花钱；`frames._ensure` 换成落 1×1 PNG 的替身）。判据全对才 exit 0，任何一条 FAIL
如实打印并 exit 1（判 returncode，禁子串判通过）。

判据：
  [1] 判据正例（好产物）：8 项全 PASS、0 FAIL、0 WARN；D1 报「宽松复算」（pop-1 形态：
      分镜表止 00:05、片长 6.04s）；D2/D4 只判九资产主列表行（品牌位置换位剔除，不恒红）。
  [2] 判据负例（逐项逼红）：空分镜表 → D1；清必换行画面位置 → D2+D4；断意图建议+清画面位置
      → D3+D5+D7；删层标题 → D6；单行空时段 → D7 只 WARN（不触发重拆）。
  [3] 驱动三态：首轮 FAIL 且诊断落盘（非标题形态、含缺项 id）→ 重拆成功（轮次 1、状态
      「重拆后通过」）→ 重拆无效则到顶（轮次 2、状态「到顶标红」，缺项仍如实留着）。
  [4] 计数落盘：同会话续跑不再重拆（rework 调用数不增）；换会话键归零；门禁.json 字段齐
      （schema/会话/轮次/上限/状态/历史）。
  [5] 复合时段解析（原 S2 并入 R1）：`00:00-00:03 / 镜1-3` 在分镜表读不出时也拿得到时段
      （旧实现先认镜号 → 表被拒即丢）；纯镜号写法仍按分镜表换算；`全片` 取整片。
  [6] 接线与纪律：analyze.py 门禁步挂在 stage_render 之后并透传 session_key/rework；
      extract.py 有 --diagnosis 且诊断块非标题形态、空诊断逐字节不变。
  [7] 真数据只读（pop-1 四件 + auto-0036/任务.json + 夹具源 SHA256 前后不变）+ 零残留。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
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

FIXTURE_SRC = ROOT / "tests" / "fixtures" / "at15-r1"
BRIDGE = "probe-at15-r1"
LOCAL = "auto-probe15"
REAL = ROOT / "analysis" / "reverse" / "pop-1"
REAL_TASK = ROOT / "automation" / "projects" / "auto-0036" / "任务.json"
DURATION = 6.04
FIX_NAMES = ("拆解.md", "待确认清单.md", "待确认清单.json", "意图建议.md")
PNG_A = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c626001000000ffff03000006000557bfabd4"
    "0000000049454e44ae426082")

WORK = Path(tempfile.mkdtemp(prefix="probe-at15-r1-"))
FIX = WORK / BRIDGE
RESULTS: list[tuple[str, bool, str]] = []
WATCH = [REAL / n for n in FIX_NAMES] + [REAL_TASK] + [FIXTURE_SRC / n for n in FIX_NAMES]


def check(name: str, cond: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f" —— {detail}" if detail and not cond else ""))
    return bool(cond)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def module_by_path(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------- 夹具与替身

def stub_ensure(video, at, out):
    """抽帧替身：不跑 ffmpeg，落 1×1 PNG。"""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(PNG_A)
    return True, ""


def _fix_file(bridge_id: str, name: str) -> Path:
    path = WORK / str(bridge_id) / name
    if not path.is_file():
        raise replace.ReplaceError(f"夹具缺文件 {path}")
    return path


class Patches:
    """隔离补丁组：ANALYSIS 双根 + replace 桥目录解析 + 抽帧/时长替身。"""

    def __enter__(self):
        self.ps = [
            patch.object(cards, "ANALYSIS", WORK),
            patch.object(frames, "ANALYSIS", WORK),
            patch.object(replace, "checklist_path", lambda bid: _fix_file(bid, "待确认清单.md")),
            patch.object(replace, "sidecar_path", lambda bid: _fix_file(bid, "待确认清单.json")),
            patch.object(frames, "_ensure", stub_ensure),
            patch.object(frames, "duration", lambda video=None: DURATION),
        ]
        for p in self.ps:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self.ps):
            p.stop()
        return False


def reset_fixture(markdown: str | None = None, sidecar: dict | None = None,
                  intent: str | None = None) -> None:
    """把夹具复制进临时根（可整体替换拆解/侧车/意图建议做单路判据）。"""
    FIX.mkdir(parents=True, exist_ok=True)
    for name in FIX_NAMES:
        shutil.copy2(FIXTURE_SRC / name, FIX / name)
    (FIX / "门禁.json").unlink(missing_ok=True)
    for leftover in FIX.glob("门禁诊断-*"):
        leftover.unlink()
    if markdown is not None:
        (FIX / "拆解.md").write_text(markdown, encoding="utf-8")
    if sidecar is not None:
        (FIX / "待确认清单.json").write_text(
            json.dumps(sidecar, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if intent is not None:
        (FIX / "意图建议.md").write_text(intent, encoding="utf-8")


def base_markdown() -> str:
    return (FIXTURE_SRC / "拆解.md").read_text(encoding="utf-8-sig")


def base_sidecar() -> dict:
    return json.loads((FIXTURE_SRC / "待确认清单.json").read_text(encoding="utf-8-sig"))


def without_shot_table(markdown: str) -> str:
    """把「七 分镜表」的表格行删空（只剩表头）——现场「整表被拒」的形态。"""
    return re.sub(r"(镜号[^\n]*\n)(?:[^\n#]*\n)+", r"\1", markdown)


def without_layer(markdown: str, title: str) -> str:
    lines = [line for line in markdown.splitlines()
             if not re.match(r"^#+\s*" + re.escape(title), line)]
    return "\n".join(lines) + "\n"


def sidecar_clear(sidecar: dict, rows: list[int], *, position: bool = True,
                  spans: bool = True) -> dict:
    out = json.loads(json.dumps(sidecar))
    for row in out.get("rows") or []:
        if row.get("n") in rows:
            if position:
                row["画面位置"] = None
            if spans:
                row["出现时段"] = []
    return out


def evaluate_now():
    return GATE.evaluate(FIX, bridge_id=BRIDGE, local_id=LOCAL)


def ids(result: dict) -> set[str]:
    return {item["id"].split()[0] for item in result["checks"] if item["verdict"] == "FAIL"}


GATE = module_by_path("probe_at15_r1_dissect_gate", ROOT / "pipeline" / "reverse" / "dissect_gate.py")


# ---------------------------------------------------------------- [1] 正例
def probe_positive() -> None:
    print("[1] 判据正例：好产物 8 项全 PASS")
    reset_fixture()
    with Patches():
        result = evaluate_now()
    verdicts = {item["id"]: item["verdict"] for item in result["checks"]}
    check("8 项判据齐全（D0/D1/D2/D3/D4/D5/D6/D7）",
          len(result["checks"]) == 8, str(list(verdicts)))
    check("verdict=PASS、0 FAIL、0 WARN",
          result["verdict"] == "PASS" and not result["缺项"] and not result["告警"],
          f"{result['verdict']}｜{result['缺项']}｜{result['告警']}")
    check("D1 报「宽松复算」（pop-1 形态：分镜表止 5s、片长 6.04s）",
          verdicts.get("D1 分镜表") == "PASS" and any("宽松复算" in item["msg"] for item in result["checks"]),
          str([item["msg"] for item in result["checks"] if item["id"].startswith("D1")]))
    check("D2/D4 只判九资产主列表行（2 行），品牌位不参与（否则恒红）",
          any("主列表 2 行全有画面位置" in item["msg"] for item in result["checks"])
          and any("主列表 2 行自身框齐全" in item["msg"] for item in result["checks"]),
          str([item["msg"] for item in result["checks"] if item["id"][:2] in ("D2", "D4")]))
    check("D3 兜底 0/8、D5 代表帧 3 个不同值（S1 已修的现场形态）",
          result.get("兜底") == 0 and any("3 个不同值" in item["msg"] for item in result["checks"]),
          f"兜底 {result.get('兜底')}")

    # 严格路也能过：把末镜右端改成片长
    strict_md = base_markdown().replace("00:03-00:05", "00:03-00:06.04")
    reset_fixture(markdown=strict_md)
    with Patches():
        strict_result = evaluate_now()
    check("分镜表末镜对齐片长时 D1 走严格闸门（同一份产物、只差这一处）",
          any("严格闸门" in item["msg"] for item in strict_result["checks"]
              if item["id"].startswith("D1")),
          str([item["msg"] for item in strict_result["checks"] if item["id"].startswith("D1")]))


# ---------------------------------------------------------------- [2] 负例
def probe_negative() -> None:
    print("[2] 判据负例：逐项逼红")
    reset_fixture(markdown=without_shot_table(base_markdown()))
    with Patches():
        d1 = evaluate_now()
    check("空分镜表 → D1 FAIL（并如实带出兜底/D5/D7 的连锁缺项）",
          "D1" in ids(d1) and d1["verdict"] == "FAIL", str(ids(d1)))

    reset_fixture(sidecar=sidecar_clear(base_sidecar(), [1]))
    with Patches():
        d2 = evaluate_now()
    check("清掉必换行（#1）画面位置 → D2 FAIL ∧ D4 FAIL",
          {"D2", "D4"} <= ids(d2), str(ids(d2)))

    reset_fixture(sidecar=sidecar_clear(base_sidecar(), list(range(1, 9))),
                  intent="")
    with Patches():
        d3 = evaluate_now()
    check("清掉全部画面位置 ∧ 断掉意图建议 → D3 兜底 8/8 FAIL ∧ D5 同图 FAIL",
          {"D3", "D5"} <= ids(d3) and d3.get("兜底") == 8, f"{ids(d3)}｜兜底 {d3.get('兜底')}")

    reset_fixture(markdown=without_layer(base_markdown(), "十 图文与收束"))
    with Patches():
        d6 = evaluate_now()
    check("删掉一层标题 → D6 FAIL（十层不齐）", "D6" in ids(d6), str(ids(d6)))

    reset_fixture(sidecar=sidecar_clear(base_sidecar(), [2]))
    with Patches():
        one_row = evaluate_now()
    check("单行空时段（可沿用行）→ 只 WARN、不 FAIL（不为此烧重拆）",
          one_row["verdict"] == "WARN" and any(
              item["id"].startswith("D7") and item["verdict"] == "WARN"
              for item in one_row["checks"]),
          f"{one_row['verdict']}｜{[i['id'] for i in one_row['checks'] if i['verdict'] != 'PASS']}")

    reset_fixture(sidecar=sidecar_clear(base_sidecar(), [2, 3, 4]))
    with Patches():
        many_rows = evaluate_now()
    check("3 行空时段 → D7 FAIL（>2 行）", "D7" in ids(many_rows), str(ids(many_rows)))


# ---------------------------------------------------------------- [3] 驱动三态
def probe_driver_states() -> None:
    print("[3] 驱动三态：门禁拒 / 重拆成功 / 到顶标红")
    # 首轮 FAIL + 诊断落盘
    reset_fixture(markdown=without_shot_table(base_markdown()))
    stages: list[str] = []
    seen: list[str] = []

    def rework_ok(diagnosis: str) -> tuple[bool, str]:
        seen.append(diagnosis)
        (FIX / "门禁诊断-第1轮.md").write_text(diagnosis, encoding="utf-8")
        (FIX / "拆解.md").write_text(base_markdown(), encoding="utf-8")
        shutil.copy2(FIXTURE_SRC / "待确认清单.json", FIX / "待确认清单.json")
        return True, "重拆完成"

    with Patches():
        ok_result = GATE.run(FIX, bridge_id=BRIDGE, local_id=LOCAL, session_key="sess-ok",
                             rework=rework_ok, stage=stages.append)
    check("首轮门禁 FAIL 时诊断被组装（非标题形态、含缺项 id、无 `#`）",
          bool(seen) and "#" not in seen[0] and "D1" in seen[0] and "【" in seen[0],
          seen[0][:120] if seen else "没调用 rework")
    check("重拆成功 → PASS｜状态「重拆后通过」｜轮次 1/2",
          ok_result["verdict"] == "PASS" and ok_result["状态"] == "重拆后通过"
          and ok_result["轮次"] == 1, f"{ok_result['verdict']}｜{ok_result['状态']}｜{ok_result['轮次']}")
    check("阶段透出留痕（门禁轮次 + 重拆动作，hub 记录器据此写 拆解日志.txt/lapian_stage）",
          any("门禁第1轮：FAIL" in line for line in stages)
          and any("重拆第1/2次" in line for line in stages)
          and any("门禁第2轮：PASS" in line for line in stages), str(stages))

    # 重拆无效 → 到顶标红
    reset_fixture(markdown=without_shot_table(base_markdown()))
    calls: list[str] = []

    def rework_noop(diagnosis: str) -> tuple[bool, str]:
        calls.append(diagnosis)
        return True, "重拆完成（产物没改善）"

    stages.clear()
    with Patches():
        capped = GATE.run(FIX, bridge_id=BRIDGE, local_id=LOCAL, session_key="sess-cap",
                          rework=rework_noop, stage=stages.append)
    check("重拆无效 → 2 轮后到顶：verdict 仍 FAIL｜状态「到顶标红」｜轮次 2/2",
          capped["verdict"] == "FAIL" and capped["状态"] == "到顶标红" and capped["轮次"] == 2,
          f"{capped['verdict']}｜{capped['状态']}｜{capped['轮次']}")
    check("到顶仍如实留缺项（产物照常交付，不静默拦截）",
          bool(capped["缺项"]) and "D1" in ids(capped), str(capped["缺项"])[:160])
    check("到顶后产物仍在盘（拆解.md/待确认清单.md 没被删）",
          (FIX / "拆解.md").is_file() and (FIX / "待确认清单.md").is_file())

    # 重拆异常 → 如实标红，不抛死
    reset_fixture(markdown=without_shot_table(base_markdown()))

    def rework_boom(_diagnosis: str):
        raise RuntimeError("模拟视觉模型通道断开")

    with Patches():
        broken = GATE.run(FIX, bridge_id=BRIDGE, local_id=LOCAL, session_key="sess-boom",
                          rework=rework_boom)
    check("重拆过程炸掉 → 状态「重拆失败·标红」且不抛异常（产物交付优先）",
          broken["verdict"] == "FAIL" and broken["状态"] == "重拆失败·标红",
          f"{broken['verdict']}｜{broken['状态']}")


# ---------------------------------------------------------------- [4] 计数落盘
def probe_counter() -> None:
    print("[4] 计数落盘：续跑不重置 / 换会话归零 / 字段齐")
    reset_fixture(markdown=without_shot_table(base_markdown()))
    calls: list[str] = []

    def rework_noop(_diagnosis: str) -> tuple[bool, str]:
        calls.append("x")
        return True, "无改善"

    with Patches():
        first = GATE.run(FIX, bridge_id=BRIDGE, local_id=LOCAL, session_key="sess-count",
                         rework=rework_noop)
        used = len(calls)
        again = GATE.run(FIX, bridge_id=BRIDGE, local_id=LOCAL, session_key="sess-count",
                         rework=rework_noop)
        extra = len(calls) - used
        fresh_session = GATE.run(FIX, bridge_id=BRIDGE, local_id=LOCAL, session_key="sess-other",
                                 rework=rework_noop)
        extra_other = len(calls) - used - extra
    check("同会话续跑不再重拆（门禁已到顶 → 轮次不重置、rework 零调用）",
          used == 2 and extra == 0 and again["轮次"] == 2,
          f"首跑 {used} 次｜续跑又 {extra} 次｜轮次 {again['轮次']}")
    check("换会话键 → 轮次归零重新计数（新片子不该被上一部吃掉的额度）",
          extra_other >= 1 and fresh_session["轮次"] >= 1, f"新会话又重拆 {extra_other} 次")
    state = json.loads((FIX / "门禁.json").read_text(encoding="utf-8"))
    check("门禁.json 字段齐（schema/会话/轮次/上限/状态/历史）",
          state.get("schema") == "at15-dissect-gate-v1" and state.get("会话") == "sess-other"
          and state.get("上限") == 2 and isinstance(state.get("历史"), list)
          and state.get("状态") and isinstance(state.get("轮次"), int), str(sorted(state)))


# ---------------------------------------------------------------- [5] 复合时段 + 注入纪律
def probe_parse_and_injection() -> None:
    print("[5] 复合时段解析（原 S2 并入 R1）＋ 诊断注入纪律")
    confirm = cards._confirm()
    empty_table: dict = {}
    composite = confirm.spans_from_appearance("00:00-00:03 / 镜1-3", empty_table)
    check("复合写法在分镜表读不出时仍拿得到时段（旧实现先认镜号 → 表被拒即丢）",
          composite == [{"起": "00:00", "止": "00:03"}], str(composite))
    shot_times = {"1": ("00:00", "00:01"), "2": ("00:01", "00:02")}
    only_shot = confirm.spans_from_appearance("镜 1、2", shot_times)
    check("纯镜号写法仍按分镜表换算（相邻镜合成一段，行为不变）",
          only_shot == [{"起": "00:00", "止": "00:02"}], str(only_shot))
    whole = confirm.spans_from_appearance("全片", {"1": ("00:00", "00:02"), "2": ("00:02", "00:06")})
    check("「全片」取首镜起→末镜止（行为不变）",
          whole == [{"起": "00:00", "止": "00:06"}], str(whole))
    mixed = confirm.spans_from_appearance("00:04-00:05 / 镜2", shot_times)
    check("时段与镜号同时给且镜号不在表里 → 用显式时段，不落空",
          mixed == [{"起": "00:04", "止": "00:05"}], str(mixed))

    extract = module_by_path("probe_at15_r1_extract", ROOT / "pipeline" / "reverse" / "extract.py")
    base = "## 一 概览\n正文\n## 七 分镜表\n正文\n"
    check("空诊断 → 任务书逐字节不变（旧调用零回归）",
          extract.build_prompt(base, "意图") == extract.build_prompt(base, "意图", diagnosis=""),
          "")
    diagnosis = GATE.build_diagnosis({"checks": [
        {"id": "D1 分镜表", "verdict": "FAIL", "msg": "严格与宽松都读不出镜时段"},
        {"id": "D2 画面位置", "verdict": "PASS", "msg": "不缺"},
    ]})
    check("诊断只带 FAIL 项、含「必须修掉」口径、无 `#`（红线 24）",
          "D1" in diagnosis and "D2" not in diagnosis and "必须修掉" in diagnosis
          and "#" not in diagnosis, diagnosis[:120])
    injected = extract.build_prompt(base, "意图", diagnosis=diagnosis + "## 不该出现的标题\n")
    check("诊断块以非标题形态追加到任务书末尾（`#` 被剥掉、正文留下）",
          injected.rstrip().endswith("不该出现的标题")
          and "## 不该出现的标题" not in injected and "D1" in injected,
          injected[-160:])
    check("诊断不改十层标题数量（注入前后标题数一致）",
          len(re.findall(r"(?m)^#+\s*[一二三四五六七八九十]", injected))
          == len(re.findall(r"(?m)^#+\s*[一二三四五六七八九十]", base)), "")


# ---------------------------------------------------------------- [6] 接线
def probe_wiring() -> None:
    print("[6] 接线与纪律：analyze.py 门禁步 + extract.py --diagnosis")
    source = (ROOT / "analyze.py").read_text(encoding="utf-8")
    check("analyze.py 挂了门禁步（dissect_gate + 拆解产物门禁 banner）",
          "dissect_gate" in source and "拆解产物门禁" in source, "")
    check("门禁调用在 stage_render 之后（render 后挂步，符合方案 §1）",
          source.index("stage_render(video_id, tpl_name)") < source.index("gate.run("), "")
    check("门禁透传 session_key 与 rework（计数会话键 + 重拆驱动都在引擎侧）",
          "session_key=gate_session_key" in source and "rework=rework" in source, "")
    check("重拆只重跑 视觉模型/清单/意图/渲染（diagnosis + force=True）",
          "diagnosis=diag_path, force=True" in source, "")
    extract_source = (ROOT / "pipeline" / "reverse" / "extract.py").read_text(encoding="utf-8")
    check("extract.py 有 --diagnosis 且写入 child_args 之外的直读路径",
          '"--diagnosis"' in extract_source and "build_diagnosis_block" in extract_source, "")
    check("门禁不签 token（拆解不花钱：模块内无 gate token 目录写入）",
          "gates" not in GATE.__dict__ and "token" not in json.dumps(
              [n for n in dir(GATE) if not n.startswith("_")]), "")


# ---------------------------------------------------------------- [7] 只读 + 残留
def probe_readonly(before: dict[str, str]) -> None:
    print("[7] 真数据只读 + 零残留")
    after = {path: sha256(Path(path)) for path in before}
    changed = [path for path in before if after[path] != before[path]]
    check("真数据只读（pop-1 四件 + auto-0036/任务.json + 夹具源 SHA256 不变）",
          not changed, str(changed))
    shutil.rmtree(WORK, ignore_errors=True)
    check("跑后零残留（临时夹具根已删）", not WORK.exists(), str(WORK))


def main() -> int:
    before: dict[str, str] = {}
    for path in WATCH:
        if path.is_file():
            before[str(path)] = sha256(path)
        else:
            print(f"  [WARN] 只读校验文件不在场（跳过该条）：{path}")
    print(f"[probe-at15-r1] 临时夹具根 {WORK}；夹具源 {FIXTURE_SRC}（只读）")
    try:
        probe_positive()
        probe_negative()
        probe_driver_states()
        probe_counter()
        probe_parse_and_injection()
        probe_wiring()
    finally:
        probe_readonly(before)
    failed = [row for row in RESULTS if not row[1]]
    print(f"\nprobe_at15_r1: {len(RESULTS) - len(failed)}/{len(RESULTS)} PASS"
          + (f"，FAIL {len(failed)} 项" if failed else ""))
    for name, _ok, detail in failed:
        print(f"  [FAIL] {name} —— {detail[:240]}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
