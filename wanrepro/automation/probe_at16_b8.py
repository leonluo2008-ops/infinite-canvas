#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AT-16 B8 探针（后端时序侧）：**提交全程主状态可见**的事实链（纯离线，bridge 打桩，不付费）。

跑法：wanrepro/ 下 `python -m automation.probe_at16_b8`。

缺陷 D9（auto-0043 真机）：点提交 → 受理 200 → 4.5 秒渠道 502 → 回 unknown，主状态条全程
零变化（只有 toast），所有者感知「点了没反应」→ 重复点 → 每一下都在真实发单。

B8 的修法不动后端（automation/ 与 ui/hub_api.py 白名单外）：主状态条消费摘要 `submit`
（B5a-1 唯一生产源）+ 受理即刷新 + 在途轮询加密。所以本探针只证明**数据面真会迁移**：

  [0] 隔离：TASKS_DIR 落在本次临时根内；真实 projects 目录集前后一格不少（真实任务只读）。
  [1] 点提交前：state=approved 且 submit.form=none → 主状态说「已批准·待提交」。
  [2] 受理后**立刻**（≤0.5s）摘要就是 form=inflight → 主状态能说「提交处理中」
      ——这就是「受理即刷新」拿得到的那一帧；5s 轮询拿不到。
  [3] 渠道 502 后：form=unknown + label「结果未知·先核对」+ blocks_retry=true。
  [4] 迁移顺序与窗口：none → inflight → unknown 单向不走回头路；在途窗口实测 ≥1.0s
      （auto-0043 实测 4.5s，比一轮 5s 轮询还短）。
  [5] 防重复发单：在途时二次提交被 409「已有提交在跑」拒；回执落地后二次提交被
      「未知收单」拒——全程**只有一次创建调用**。
  [6] 夹具自清理：临时根连根删；真实 projects 不留垃圾。

夹具只用 auto-9701（临时根内）。断言全部按真实 do_submit 代码路径跑（只把 bridge 换成打桩）。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]          # wanrepro/
TMP_ROOT = Path(tempfile.mkdtemp(prefix="probe-b8-"))
os.environ["WANREPRO_AUTO_DIR"] = str(TMP_ROOT)     # 必须在 import hub_api 之前
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ui"))

from automation import bridge, state  # noqa: E402
import hub_api                        # noqa: E402

RESULTS: list[tuple[str, bool]] = []
REAL_PROJECTS = ROOT / "automation" / "projects"
PROBE_ID = "auto-9701"
FAKE_KEY = "sk-" + "probe-" + "AT16B8" + "-do-not-use"
PROMPT_TEXT = ("镜头一：产品特写，冷光，水珠沿瓶身滑落，慢推。\n" * 12)
SDGF_GEN = {"baseUrl": "https://api.juxin.example", "apiFormat": "sdgf",
            "渠道名": "聚鑫-SDGF", "model": "SD2.5-满血-GF-720P", "mode": "frames"}
SDGF_PARAMS = {"duration": 5, "ratio": "9:16", "resolution": "720", "audio": True}
STUB_DELAY = 1.5          # 渠道「慢回执」：模拟真实在途窗口（auto-0043 是 4.5s）


def check(name: str, cond: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" —— {detail}" if detail and not cond else ""))
    return bool(cond)


def real_projects() -> set[str]:
    if not REAL_PROJECTS.is_dir():
        return set()
    return {p.name for p in REAL_PROJECTS.iterdir() if p.is_dir()}


# ---------------------------------------------------------------- 夹具（自建自清理）
def seed_approved(local_id: str = PROBE_ID) -> Path:
    tdir = TMP_ROOT / local_id
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / "提示词.md").write_text(PROMPT_TEXT, encoding="utf-8")
    snap = state.snapshot_hash_v2(PROMPT_TEXT, dict(SDGF_PARAMS), dict(SDGF_GEN))
    task = {
        "local_id": local_id, "bridge_id": f"probe-b8-{local_id}",
        "created_at": "2026-01-01T00:00:00+08:00", "state": "approved",
        "history": [{"at": "2026-01-01T00:00:00+08:00", "from": None, "to": "approved",
                     "note": "probe-at16-b8 夹具"}],
        "intent": "AT-16 B8 探针夹具（不付费）",
        "gen_params": dict(SDGF_PARAMS), "gen_channel": dict(SDGF_GEN),
        "approval_snapshot": snap, "snapshot_version": 2,
        "approval": {"snapshot_sha256": snap, "at": "2026-01-01T00:00:00+08:00", "by": "probe"},
        "assets_confirmed": [], "empty_confirm": True,
    }
    state.save(tdir, task)
    return tdir


class StubBridge:
    """渠道打桩：慢 STUB_DELAY 秒后回 502（= auto-0043 的「渠道 502」现场）。"""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict]] = []
        self.lock = threading.Lock()

    def install(self) -> "StubBridge":
        self._orig = (bridge.post_json, bridge.get, bridge.get_bytes)
        bridge.post_json = self.post_json
        bridge.get = self.get
        bridge.get_bytes = self.get_bytes
        return self

    def restore(self) -> None:
        bridge.post_json, bridge.get, bridge.get_bytes = self._orig

    def post_json(self, path: str, payload: dict) -> dict:
        with self.lock:
            self.posts.append((path, payload))
        time.sleep(STUB_DELAY)                       # 在途窗口：请求已发出、回执未到
        err = bridge.BridgeError('bridge POST /api/sdgf/video/run → HTTP 502: '
                                 '{"error": "渠道响应 HTTP 403：video_request_failed"}')
        err.status = 502
        raise err

    def get(self, path: str) -> dict:                 # 走到这里就是探针越界（不许查真云端）
        err = bridge.BridgeError(f"stub bridge 不该被 GET {path}")
        err.status = 500
        raise err

    def get_bytes(self, path: str) -> bytes:
        err = bridge.BridgeError(f"stub bridge 不该被 GET {path}")
        err.status = 500
        raise err


def row_of(local_id: str = PROBE_ID) -> dict:
    for row in hub_api.list_tasks():
        if row["local_id"] == local_id:
            return row
    return {}


def main() -> int:
    before = real_projects()
    stub = StubBridge().install()
    created = False
    try:
        # ════════════════════════════════════════════════ [0] 隔离
        print("0) 隔离：读写同源在临时根内（真实任务零写入）")
        check("[0] TASKS_DIR == 临时根（不一致必须拒跑）",
              Path(hub_api.TASKS_DIR).resolve() == TMP_ROOT.resolve(),
              f"{hub_api.TASKS_DIR} != {TMP_ROOT}")

        seed_approved()
        created = True

        # ════════════════════════════════════════════════ [1] 点提交前
        print("1) 点提交前：主状态按 state 说「已批准·待提交」")
        base = row_of()
        base_submit = base.get("submit") or {}
        check("[1] state=approved 且 submit.form=none（不编造提交结果）",
              base.get("state") == "approved" and base_submit.get("form") == "none",
              f"state={base.get('state')} form={base_submit.get('form')}")

        # ════════════════════════════════════════════════ [2][3][4] 真提交（桥打桩）
        print(f"2) 受理 → 在途 → 502 回执：摘要按 submit_view 逐帧迁移（渠道慢 {STUB_DELAY}s）")
        t0 = time.monotonic()
        job = hub_api.start_submit(PROBE_ID, FAKE_KEY)
        check("[2] 提交被受理（返回 job_id，后端异步）", bool(job.get("job_id")), str(job))

        frames: list[tuple[float, str, str, str, str]] = []      # (t, state, form, label, phase)
        second_inflight: dict | None = None
        first_inflight_at: float | None = None
        first_unknown_at: float | None = None
        inflight_active: bool | None = None
        deadline = t0 + 12.0
        while time.monotonic() < deadline:
            row = row_of()
            sub = row.get("submit") or {}
            form = str(sub.get("form") or "")
            now = time.monotonic() - t0
            if not frames or frames[-1][2] != form:
                frames.append((round(now, 2), str(row.get("state") or ""), form,
                               str(sub.get("label") or ""), str(sub.get("dispatch_phase") or "")))
            if form == "inflight":
                first_inflight_at = first_inflight_at if first_inflight_at is not None else now
                if inflight_active is None:
                    inflight_active = bool(sub.get("active_submit"))
                if second_inflight is None:            # 在途期间再点一次（禁重复发单）
                    try:
                        hub_api.start_submit(PROBE_ID, FAKE_KEY)
                        second_inflight = {"ok": True}
                    except hub_api.HubError as exc:
                        second_inflight = {"ok": False, "status": getattr(exc, "status", 409), "msg": str(exc)}
            if form == "unknown":
                first_unknown_at = first_unknown_at if first_unknown_at is not None else now
                break
            time.sleep(0.1)

        print("   逐帧：" + json.dumps([{"t": f[0], "state": f[1], "form": f[2], "label": f[3], "phase": f[4]}
                                        for f in frames], ensure_ascii=False))
        forms = [f[2] for f in frames]
        check("[2] 受理后 0.5s 内摘要已是 inflight（受理即刷新拿得到这一帧）",
              first_inflight_at is not None and first_inflight_at <= 0.5,
              f"first inflight at {first_inflight_at}")
        inflight_label = next((f[3] for f in frames if f[2] == "inflight"), "")
        check("[2] 在途标签是人话「提交处理中…」（主状态条照它上屏）",
              "提交处理中" in inflight_label, inflight_label)
        check("[2] 在途帧 active_submit=true（本地线程在跑，不是靠 state 猜）",
              inflight_active is True, f"active_submit={inflight_active}")

        unknown_label = next((f[3] for f in frames if f[2] == "unknown"), "")
        check("[3] 502 回执落地 → form=unknown、标签「结果未知·先核对」",
              "unknown" in forms and unknown_label == "结果未知·先核对", unknown_label)
        final_submit = row_of().get("submit") or {}
        check("[3] unknown 帧 blocks_retry=true（禁盲目重提）+ dispatch_phase 已落 dispatched",
              final_submit.get("blocks_retry") is True and final_submit.get("dispatch_phase") == "dispatched",
              f"blocks_retry={final_submit.get('blocks_retry')} phase={final_submit.get('dispatch_phase')}")
        check("[3] 错误码从原文抠 HTTP 502（不翻译原因）",
              final_submit.get("error_code") == "HTTP 502", repr(final_submit.get("error_code")))

        check("[4] 迁移顺序 none → inflight → unknown（单向，不走回头路）",
              ["none"] + forms == ["none", "inflight", "unknown"], str(["none"] + forms))
        window = (first_unknown_at - first_inflight_at) if (first_inflight_at is not None and first_unknown_at is not None) else -1
        print(f"   在途窗口实测 {window:.2f}s（auto-0043 真机 4.5s；固定 5s 轮询会整个跳过）")
        check("[4] 在途窗口 ≥1.0s（窗口短于轮询粒度 = 主状态必然零变化，这就是 D9）",
              window >= 1.0, f"{window:.2f}s")

        # ════════════════════════════════════════════════ [5] 防重复发单
        print("5) 防重复发单：点两下也只有一个创建调用")
        check("[5] 在途时二次提交被 409「已有提交在跑」拒",
              second_inflight is not None and second_inflight.get("ok") is False
              and "已有提交在跑" in str(second_inflight.get("msg") or ""),
              json.dumps(second_inflight, ensure_ascii=False)[:200])
        again = None
        try:
            hub_api.start_submit(PROBE_ID, FAKE_KEY)
            again = {"ok": True}
        except hub_api.HubError as exc:
            again = {"ok": False, "msg": str(exc)}
        check("[5] 回执落地后二次提交被既有阻断守卫拒（先核对，不许重提）",
              again.get("ok") is False and "核对" in str(again.get("msg") or ""),
              json.dumps(again, ensure_ascii=False)[:200])
        with stub.lock:
            posts = list(stub.posts)
        check("[5] 全程只有 1 次渠道创建调用（重复点击没有变成重复发单）",
              len(posts) == 1, f"{len(posts)} 次：{[p[0] for p in posts]}")
        check("[5] 那次调用打的是 sdgf 创建端点（不是别的链）",
              bool(posts) and "/api/sdgf/video/run" in posts[0][0], posts[0][0] if posts else "")
    finally:
        stub.restore()
        # ════════════════════════════════════════════════ [6] 清理与真实任务只读
        print("6) 清理：临时根连根删，真实 projects 一格不少")
        after = real_projects()
        check("[6] 真实 projects 目录集前后一致（本次探针零写入真实任务）",
              before == after, f"新增 {sorted(after - before)} / 丢失 {sorted(before - after)}")
        if created:
            shutil.rmtree(TMP_ROOT, ignore_errors=True)
        check("[6] 临时根已删（无残留夹具）", not TMP_ROOT.exists(), str(TMP_ROOT))
        check("[6] 真实 projects 仍无 auto-9701（夹具没漏到真实根）",
              not (REAL_PROJECTS / PROBE_ID).exists())

    fails = [n for n, ok in RESULTS if not ok]
    print(f"\n[probe-at16-b8] {len(RESULTS) - len(fails)}/{len(RESULTS)} PASS")
    for name in fails:
        print(f"  FAIL {name}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
