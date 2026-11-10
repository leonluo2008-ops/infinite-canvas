#!/usr/bin/env python
"""AT-12 N1 后端探针：dismissed（资产移除）值域/落账 + 三处读点（渲染/锁点/入组互斥）+ get_task 两集合。

跑法：wanrepro/ 下 `python -m automation.probe_at12_n1`（纯离线：无 HTTP、无付费调用；
判据全对才 exit 0，任何一步失败如实 FAIL 并 exit 1——判 returncode，禁子串判通过）。

判据（docs/nodes/AT-12.md v2 §2.2/§3 + AT-11 §3.2/§3.5 语义基线）：
  [1] 值域与落账：load_row_decisions 认识 dismissed + 旧 keep_origin、丢弃未知值；
      save_row_decisions 只落白名单；set_asset dismissed 落账并回显；restore **清键**（不写 "restore"）。
  [2] 渲染读点：dismissed 行在发号前胜出（无 @图片N，与 keep_origin 同结果）；锁点剩余豁免。
  [3] 入组互斥（先降后合）：dismissed 行入组被拒；组内行移除被拒（组=原子单位，先拆开）。
  [4] 确认次序（B2 同构）：dismissed 行的 pending 不进 assets_confirmed，『按原片确认』同快照留痕。
  [5] get_task 透出「按原片行」（keep_origin ∪ dismissed）与「已移除行」（dismissed）两集合。

真数据只读：pop-4 清单/sidecar 探针前后 SHA256 必须相同；夹具目录 analysis/reverse/probe-at12-n1/
与临时任务目录用完即删（零残留）。
"""
from __future__ import annotations

import copy
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
from automation import cards, replace, selfcheck, state  # noqa: E402

POP4 = ROOT / "analysis" / "reverse" / "pop-4"
FIXTURE_BRIDGE = "probe-at12-n1"
FIXTURE_DIR = ROOT / "analysis" / "reverse" / FIXTURE_BRIDGE
TMP_TASK = Path(tempfile.mkdtemp(prefix="probe-at12-n1-task-"))

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" —— {detail}" if detail and not cond else ""))
    return bool(cond)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_hub():
    spec = importlib.util.spec_from_file_location("at12_probe_hub", ROOT / "ui" / "hub_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hub = load_hub()
render = selfcheck._load_render()
READONLY_WATCH = [POP4 / "待确认清单.md", POP4 / "待确认清单.json"]

HEADER = """# 待确认清单 · probe-at12-n1

| # | 类别 | 模型看到什么 | 命名 | 资产图 | 锁什么 | 建议 | 用我的描述 |
|---|------|-------------|------|--------|--------|------|------------|
"""


def md(image5: str = "") -> str:
    """5 行夹具：必须换 #1/#2/#5（#1 预置图，#5 可注入图）、建议准备 #3、品牌视觉 #7。"""
    return HEADER + "\n".join([
        "| 1 | 主体 | 紫色圆管产品 |  | https://x/a.png | 形状 | 必须换 |  |",
        "| 2 | 场景 | 海滨场景 |  |  | 光影 | 必须换 |  |",
        "| 3 | 道具 | 香水瓶道具 |  |  | 位置 | 建议准备 |  |",
        f"| 5 | 主体 | 品牌圆管 |  | {image5} | 形状 | 必须换 |  |",
        "| 7 | 品牌视觉（置换位） | 品牌标语字 |  |  | 字样 | 建议准备 |  |",
    ]) + "\n"


TASKS: dict[str, dict] = {}


def _state_load(task_dir):
    return copy.deepcopy(TASKS[str(task_dir)])


def _state_save(task_dir, task):
    TASKS[str(task_dir)] = copy.deepcopy(task)


def make_task(pending: list[dict] | None = None, st: str = "awaiting_assets") -> dict:
    return {"local_id": "probe-at12-n1", "bridge_id": FIXTURE_BRIDGE, "state": st,
            "intent": "置换探针", "created_at": "2026-01-01 00:00:00",
            "ingestion": {"video_name": "probe.mp4"}, "pending_replace": pending or [],
            "history": []}


def install_task(pending: list[dict] | None = None, st: str = "awaiting_assets") -> None:
    TASKS[str(TMP_TASK)] = make_task(pending, st)


def scenario(md_text: str | None = None, side: dict | None = None):
    """重置夹具盘面 + 安装 state/hub 内存替身，返回 patches（配合 _Patches 用）。"""
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    (FIXTURE_DIR / "待确认清单.md").write_text(md_text if md_text is not None else md(), encoding="utf-8")
    payload = {"同实体疑似": {}, "同实体确认": {}, "行决策覆盖": {}}
    payload.update(side or {})
    (FIXTURE_DIR / "待确认清单.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return [patch.object(state, "load", side_effect=_state_load),
            patch.object(state, "save", side_effect=_state_save),
            patch.object(hub, "_task_dir", return_value=TMP_TASK)]


class _Patches:
    def __init__(self, ps):
        self.ps = ps

    def __enter__(self):
        for p in self.ps:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self.ps):
            p.stop()
        return False


def numbering(decisions: dict[int, str]) -> dict[int, int]:
    ents = render.parse_checklist(FIXTURE_DIR / "待确认清单.md")
    render.assign_numbers(ents, [], decisions)
    return {e.row_no: e.no for e in ents if e.no}


def run_confirm() -> dict:
    replace.run_replace(TMP_TASK, assets=None, map_spec=None, name_spec=None,
                        confirm=True, verbose=False)
    return TASKS[str(TMP_TASK)]


def main() -> int:
    hashes_before = {str(p): sha256(p) for p in READONLY_WATCH}
    try:
        # ================================================= [1] 值域与落账
        print("[1] 值域与落账：dismissed 认识 / 未知值丢弃 / restore 清键")
        with _Patches(scenario(side={"行决策覆盖": {"1": "dismissed", "2": "keep_origin", "3": "bogus"}})):
            install_task()
            loaded = replace.load_row_decisions(FIXTURE_BRIDGE)
            check("load_row_decisions 认识 dismissed + 旧 keep_origin、丢弃未知值",
                  loaded == {1: "dismissed", 2: "keep_origin"}, str(loaded))
            saved = replace.save_row_decisions(FIXTURE_BRIDGE, {1: "dismissed", 2: "keep_origin", 3: "bogus"})
            check("save_row_decisions 白名单（bogus 不落盘）", saved == {1: "dismissed", 2: "keep_origin"}, str(saved))
            out = hub.set_asset("probe-at12-n1", 5, decision="dismissed")
            check("set_asset dismissed 落账并回显",
                  out.get("覆盖") == {"1": "dismissed", "2": "keep_origin", "5": "dismissed"}
                  and out.get("决策") == "dismissed", str(out))
            out = hub.set_asset("probe-at12-n1", 5, decision="restore")
            check("restore 清键（覆盖表不含 5，不写 \"restore\"）",
                  "5" not in (out.get("覆盖") or {}) and out.get("决策") == "restore", str(out))
            try:
                hub.set_asset("probe-at12-n1", 5, decision="bogus")
                check("拒绝未知 decision 值", False, "未拒绝")
            except hub.HubError as e:
                check("拒绝未知 decision 值（文案含新值域 dismissed）",
                      "keep_origin" in str(e) and "dismissed" in str(e), str(e))
            try:
                hub.set_asset("probe-at12-n1", 5, decision="dismissed", image_url="https://x/c.png")
                check("拒绝 decision 与 image_url 互斥", False, "未拒绝")
            except hub.HubError as e:
                check("拒绝 decision 与 image_url 互斥", "互斥" in str(e), str(e))

        # ================================================= [2] 渲染 / 锁点读点
        print("[2] 渲染/锁点读点：dismissed 行不占号 + 锁点豁免（与 keep_origin 同结果）")
        with _Patches(scenario(md_text=md(image5="https://x/b.png"))):
            install_task()
            base = numbering({})
            check("基线：有图行都占号（#1=1、#5=2）", base == {1: 1, 5: 2}, str(base))
            dismissed = numbering({5: "dismissed"})
            keep = numbering({5: "keep_origin"})
            check("dismissed 行发号前胜出（#5 不占号）", dismissed == {1: 1}, str(dismissed))
            check("dismissed 与 keep_origin 发号结果完全同义", dismissed == keep, f"{dismissed} vs {keep}")
        with _Patches(scenario()):          # #5 无图 → 锁点里
            install_task()
            lock_before = hub.lock_remaining(FIXTURE_BRIDGE)
            check("锁点基线：必换未换 = #2/#5", lock_before == [2, 5], str(lock_before))
            hub.set_asset("probe-at12-n1", 5, decision="dismissed")
            check("dismissed 必换行锁点豁免（剩余 #2）", hub.lock_remaining(FIXTURE_BRIDGE) == [2],
                  str(hub.lock_remaining(FIXTURE_BRIDGE)))
            hub.set_asset("probe-at12-n1", 5, decision="restore")
            check("restore 后重新进锁点（剩余 #2/#5）", hub.lock_remaining(FIXTURE_BRIDGE) == [2, 5],
                  str(hub.lock_remaining(FIXTURE_BRIDGE)))

        # ================================================= [3] 入组互斥
        print("[3] 入组互斥：先降后合拒绝；组内行移除拒绝（组=原子单位）")
        with _Patches(scenario()):
            install_task()
            hub.set_asset("probe-at12-n1", 5, decision="dismissed")
            try:
                hub.group_rows("probe-at12-n1", [1, 5])
                check("拒绝：dismissed 行入组（先恢复再合并）", False, "未拒绝")
            except hub.HubError as e:
                check("拒绝：dismissed 行入组（点名 keep_origin/dismissed）",
                      "dismissed" in str(e) and "先恢复再合并" in str(e), str(e))
            hub.set_asset("probe-at12-n1", 5, decision="restore")
            out = hub.group_rows("probe-at12-n1", [1, 5])
            check("恢复后成组成功（#1+#5）", out.get("组") == [[1, 5]], str(out))
            try:
                hub.set_asset("probe-at12-n1", 1, decision="dismissed")
                check("拒绝：组内行移除（组=原子单位，先拆开）", False, "未拒绝")
            except hub.HubError as e:
                check("拒绝：组内行移除（组=原子单位，先拆开）", "确认组" in str(e), str(e))

        # ================================================= [4] 确认次序
        print("[4] 确认次序：dismissed 行 pending 屏蔽；『按原片确认』同快照落账")
        with _Patches(scenario(side={"行决策覆盖": {"5": "dismissed"}})):
            install_task([{"no": 1, "img": "", "url": "https://x/a.png", "name": "", "desc": ""},
                          {"no": 5, "img": "", "url": "https://x/a.png", "name": "", "desc": ""}])
            task = run_confirm()
            confirmed = task.get("assets_confirmed") or []
            check("确认快照只含有效行（#5 dismissed 被屏蔽）",
                  [x["no"] for x in confirmed] == [1], json.dumps(confirmed, ensure_ascii=False)[:160])
            check("『按原片确认』同快照留痕（rows=[5]）",
                  (task.get("按原片确认") or {}).get("rows") == [5], str(task.get("按原片确认")))
            check("确认后 → rendering", task["state"] == "rendering", task["state"])

        # ================================================= [5] get_task 两集合
        print("[5] get_task 透出：按原片行（keep_origin ∪ dismissed）/ 已移除行（dismissed）")
        with _Patches(scenario(side={"行决策覆盖": {"1": "keep_origin", "2": "dismissed", "5": "dismissed"}})), \
             patch.object(cards, "build", return_value=[]), \
             patch.object(hub.cards, "build", return_value=[]):
            install_task()
            detail = hub.get_task("probe-at12-n1")
            check("按原片行 = 升序 keep_origin∪dismissed", detail.get("按原片行") == [1, 2, 5],
                  str(detail.get("按原片行")))
            check("已移除行 = 仅 dismissed", detail.get("已移除行") == [2, 5], str(detail.get("已移除行")))
        with _Patches(scenario()):
            install_task()
            detail = hub.get_task("probe-at12-n1")
            check("无覆盖任务两集合都为空（旧任务不炸）",
                  detail.get("按原片行") == [] and detail.get("已移除行") == [],
                  f"{detail.get('按原片行')} / {detail.get('已移除行')}")
    finally:
        shutil.rmtree(FIXTURE_DIR, ignore_errors=True)
        shutil.rmtree(TMP_TASK, ignore_errors=True)

    hashes_after = {str(p): sha256(p) for p in READONLY_WATCH}
    check("真数据只读：pop-4 清单/sidecar 前后 SHA256 一致", hashes_before == hashes_after,
          str({k: (v, hashes_after.get(k)) for k, v in hashes_before.items() if hashes_after.get(k) != v}))
    check("夹具目录已清理（零残留）", not FIXTURE_DIR.exists() and not TMP_TASK.exists(),
          f"{FIXTURE_DIR} / {TMP_TASK}")

    total = len(RESULTS)
    failed = [name for name, ok, _ in RESULTS if not ok]
    print(f"\n[probe-at12-n1] {total - len(failed)}/{total} PASS")
    for name in failed:
        print(f"  FAIL {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
