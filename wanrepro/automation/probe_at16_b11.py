#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AT-16 B11 探针（后端）：创建阶段渠道 4xx = 明确拒绝，不是「结果未知」。

跑法：wanrepro/ 下 `python -m automation.probe_at16_b11`。

缺陷 D11（auto-0043 真机）：渠道创建端点 POST /videos 回 **403 fail_to_fetch_task /
video_request_failed「视频请求失败，请凭 Request ID 联系支持」**——渠道明确说任务没建成。
但链路把它判成「结果未知·先核对」，逼所有者去渠道控制台核对 + 走「解除阻断」绕路。
两处根因：① ui/server.py sdgf_http 把渠道任何 HTTPError 一律包成 502（明确拒绝被抹成状态不明）；
② automation/submit_view.py 的 rejected 只认 400/409 + 本地预检，创建阶段 403 落进 unknown。

本探针锁后端两半：

  [0] 隔离：TASKS_DIR 必须落在本次临时根内（否则拒跑），真实 projects 目录集前后一致。
  [1] 创建链 4xx 透传（ui/server.py，假 opener，零真渠道）：
      403/422 带错误体 → 原码 + 「渠道创建端点明确拒绝」原文（含 fail_to_fetch_task 标记、key 脱敏）；
      500 → 502、URLError → 502、Timeout → 504（状态不明语义不变）；
      查询链 403 → 仍 502（透传只给创建链）；2xx 却无任务 id → 502（不敢当成功）。
  [2] 四态分类（submit_view 唯一判定源）：创建 403/400/409 → rejected；拿到 task_id 后的 403 →
      unknown；网络断连 → unknown；generating+taskId → inflight；无错误 → none；
      有 gen_job 的 4xx 原文不算创建拒绝（不是「任务没建成」）。
  [3] 人话：rejected 指引含「直接重提」、不含「先核对」；原因引原文标记，不推断「key 失效」。
  [4] hub 写路径自愈：approved 的旧误标夹具 → start_submit 不被阻断 → 只发 1 次创建 → 走 403
      受控回退 → 终态 rejected（submit_unknown/dispatch 清空 + history 记自愈）。
  [5] CLI 写路径自愈：generate.do_submit 直调（跨进程那条）同样自愈。
  [6] 不误伤：真「结果未知（断连）」夹具上提交仍被既有阻断挡住，创建调用 0 次。
  [7] 静态守卫：4xx 透传只挂在创建链；自愈判定单源；submit_view 不写盘。

夹具只用 auto-960x（临时根内，跑完连根删）。渠道全打桩，不付费。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import urllib.error
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]          # wanrepro/
TMP_ROOT = Path(tempfile.mkdtemp(prefix="probe-b11-"))
os.environ["WANREPRO_AUTO_DIR"] = str(TMP_ROOT)     # 必须在 import hub_api / server 之前
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ui"))

from automation import bridge, generate, state, submit_view  # noqa: E402
import hub_api                                               # noqa: E402
import server as bridge_server                               # noqa: E402

RESULTS: list[tuple[str, bool]] = []
REAL_PROJECTS = ROOT / "automation" / "projects"
FAKE_KEY = "sk-" + "probe-" + "AT16B11" + "-do-not-use"
PROMPT_TEXT = ("镜头一：产品特写，冷光，水珠沿瓶身滑落，慢推。\n" * 12)
SDGF_GEN = {"baseUrl": "https://api.juxin.example", "apiFormat": "sdgf",
            "渠道名": "聚鑫-SDGF", "model": "SD2.5-满血-GF-720P", "mode": "reference"}
SDGF_PARAMS = {"duration": 5, "ratio": "9:16", "resolution": "720", "audio": True}

# auto-0043 现场原文（结构一致，内容取自真机 gen_error 的原文标记）
CHANNEL_403 = ('{\\"code\\":\\"fail_to_fetch_task\\",\\"message\\":\\"{\\\\\\"code\\\\\\":\\\\\\"'
               'video_request_failed\\\\\\",\\\\\\"message\\\\\\":\\\\\\"生成请求失败。上游提示：'
               'video_request_failed: 视频请求失败，请凭Request ID联系支持。\\"}"}')
LEGACY_403 = ('提交结果未知：bridge POST /api/sdgf/video/run → HTTP 502: '
              '{"error": "渠道响应 HTTP 403：' + CHANNEL_403 + '"}')
NEW_403 = ('创建请求被明确拒绝（HTTP 403）：bridge POST /api/sdgf/video/run → HTTP 403: '
           '{"error": "渠道创建端点明确拒绝 HTTP 403：' + CHANNEL_403 + '"}')


def check(name: str, cond: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" —— {detail}" if detail and not cond else ""))
    return bool(cond)


def summary() -> int:
    fails = [n for n, ok in RESULTS if not ok]
    print(f"\n[probe-at16-b11] {len(RESULTS) - len(fails)}/{len(RESULTS)} PASS")
    for n in fails:
        print(f"  FAIL {n}")
    return 1 if fails else 0


def real_projects() -> set[str]:
    if not REAL_PROJECTS.is_dir():
        return set()
    return {p.name for p in REAL_PROJECTS.iterdir() if p.is_dir()}


def seed(local_id: str, **fields) -> Path:
    """隔离夹具（只写本次临时根）。"""
    d = TMP_ROOT / local_id
    d.mkdir(parents=True, exist_ok=True)
    task = {"local_id": local_id, "state": "approved", "history": []}
    task.update(fields)
    (d / "任务.json").write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
    return d


def seed_approved_for_submit(local_id: str, **fields) -> Path:
    """可真正走 do_submit 的夹具（提示词 + 批准快照 + 渠道草稿齐）。"""
    tdir = seed(local_id, **{**{
        "bridge_id": f"probe-b11-{local_id}",
        "created_at": "2026-01-01T00:00:00+08:00",
        "intent": "AT-16 B11 探针夹具（不付费）",
        "gen_params": dict(SDGF_PARAMS), "gen_channel": dict(SDGF_GEN),
        "assets_confirmed": [], "empty_confirm": True,
    }, **fields})
    (tdir / "提示词.md").write_text(PROMPT_TEXT, encoding="utf-8")
    task = json.loads((tdir / "任务.json").read_text(encoding="utf-8"))
    snap = state.snapshot_hash_v2(PROMPT_TEXT, dict(SDGF_PARAMS), dict(SDGF_GEN))
    task["approval_snapshot"] = snap
    task["snapshot_version"] = 2
    task["approval"] = {"snapshot_sha256": snap, "at": "2026-01-01T00:00:00+08:00", "by": "probe"}
    (tdir / "任务.json").write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
    return tdir


def row_of(local_id: str) -> dict:
    for row in hub_api.list_tasks():
        if row["local_id"] == local_id:
            return row
    return {}


class StubBridge:
    """渠道打桩：POST /api/sdgf/video/run 按指定 HTTP 码明确拒绝（不发真单、不花钱）。"""

    def __init__(self, status: int = 403) -> None:
        self.status = status
        self.posts: list[tuple[str, dict]] = []

    def install(self) -> "StubBridge":
        self._orig = (bridge.post_json, bridge.get, bridge.get_bytes)
        bridge.post_json = self.post_json
        bridge.get = self.get
        bridge.get_bytes = self.get_bytes
        return self

    def restore(self) -> None:
        bridge.post_json, bridge.get, bridge.get_bytes = self._orig

    def post_json(self, path: str, payload: dict) -> dict:
        self.posts.append((path, payload))
        body = json.dumps({"error": f"渠道创建端点明确拒绝 HTTP {self.status}：{CHANNEL_403}"},
                          ensure_ascii=False)
        err = bridge.BridgeError(f"bridge POST {path} → HTTP {self.status}: {body}")
        err.status = self.status
        raise err

    def get(self, path: str) -> dict:                 # 走到这里就是探针越界
        err = bridge.BridgeError(f"stub bridge 不该被 GET {path}")
        err.status = 500
        raise err

    def get_bytes(self, path: str) -> bytes:
        err = bridge.BridgeError(f"stub bridge 不该被 GET {path}")
        err.status = 500
        raise err


# ═══════════════════════════════════════════════ [1] 创建链 4xx 透传（假 opener）
def _http_error(url: str, code: int, body: bytes) -> urllib.error.HTTPError:
    import io
    return urllib.error.HTTPError(url, code, "stub", {}, io.BytesIO(body))


class _FakeRaisingOpener:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    def open(self, req, timeout=None):                # noqa: ANN001
        raise self.exc


class _FakeOkOpener:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def open(self, req, timeout=None):                # noqa: ANN001
        class _Resp:
            def __init__(self, body: bytes) -> None:
                self._body = body

            def read(self, n: int = -1) -> bytes:
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, *exc) -> bool:
                return False
        return _Resp(self.body)


def call_sdgf(fn, opener, *args, **kwargs):
    """换掉 server.SDGF_DIRECT 跑一次渠道函数，返回 (异常或 None, 结果)。"""
    orig = bridge_server.SDGF_DIRECT
    bridge_server.SDGF_DIRECT = opener
    try:
        return None, fn(*args, **kwargs)
    except bridge_server.ApiError as exc:
        return exc, None
    finally:
        bridge_server.SDGF_DIRECT = orig


def main() -> int:
    before = real_projects()
    stub = StubBridge(403).install()
    created: list[str] = []
    try:
        # ════════════════════════════════════════════════ [0] 隔离
        print("0) 隔离：读写同源在临时根内（真实任务零写入）")
        print(f"   temp root = {TMP_ROOT}")
        check("[0] TASKS_DIR == 临时根（不一致必须拒跑）",
              Path(hub_api.TASKS_DIR).resolve() == TMP_ROOT.resolve(),
              f"{hub_api.TASKS_DIR} != {TMP_ROOT}")

        # ════════════════════════════════════════════════ [1] 创建链 4xx 透传
        print("1) 创建链渠道 4xx 透传真实状态码（5xx/断连/超时仍 502/504）")
        body403 = json.dumps({"code": "fail_to_fetch_task",
                              "message": "video_request_failed: 视频请求失败，请凭Request ID联系支持 "
                                         f"（回显的 key={FAKE_KEY}）"}, ensure_ascii=False).encode("utf-8")
        err, _ = call_sdgf(bridge_server.sdgf_create, _FakeRaisingOpener(_http_error("u", 403, body403)),
                           "https://api.juxin.example", FAKE_KEY, {"prompt": "x"})
        msg = str(getattr(err, "msg", ""))
        check("[1] 创建链 403 → ApiError.status=403（不再一律 502）",
              getattr(err, "status", None) == 403, f"status={getattr(err, 'status', None)} msg={msg[:120]}")
        check("[1] 403 原文保留渠道标记（fail_to_fetch_task / video_request_failed）",
              "fail_to_fetch_task" in msg and "video_request_failed" in msg, msg[:200])
        check("[1] 403 原文写清「渠道创建端点明确拒绝 HTTP 403」（上层据此判 rejected）",
              "渠道创建端点明确拒绝 HTTP 403" in msg, msg[:200])
        check("[1] 403 响应里的 key 已脱敏（先脱敏后截断纪律不变）",
              FAKE_KEY not in msg and "***" in msg, msg[:200])

        err, _ = call_sdgf(bridge_server.sdgf_create,
                           _FakeRaisingOpener(_http_error("u", 422, b'{"error":"bad"}')),
                           "https://api.juxin.example", FAKE_KEY, {"prompt": "x"})
        check("[1] 创建链 422 → 原码 422（4xx 一族都透传，不是只认 403）",
              getattr(err, "status", None) == 422, f"status={getattr(err, 'status', None)}")

        err, _ = call_sdgf(bridge_server.sdgf_create,
                           _FakeRaisingOpener(_http_error("u", 500, b'{"error":"boom"}')),
                           "https://api.juxin.example", FAKE_KEY, {"prompt": "x"})
        check("[1] 创建链 5xx → 仍 502（云端可能已收单，语义不变）",
              getattr(err, "status", None) == 502, f"status={getattr(err, 'status', None)}")
        err, _ = call_sdgf(bridge_server.sdgf_create,
                           _FakeRaisingOpener(urllib.error.URLError("conn refused")),
                           "https://api.juxin.example", FAKE_KEY, {"prompt": "x"})
        check("[1] 创建链断连 → 仍 502（状态不明）", getattr(err, "status", None) == 502,
              f"status={getattr(err, 'status', None)}")
        err, _ = call_sdgf(bridge_server.sdgf_create, _FakeRaisingOpener(TimeoutError()),
                           "https://api.juxin.example", FAKE_KEY, {"prompt": "x"})
        check("[1] 创建链超时 → 仍 504（状态不明）", getattr(err, "status", None) == 504,
              f"status={getattr(err, 'status', None)}")
        err, _ = call_sdgf(bridge_server.sdgf_create, _FakeOkOpener(b'{"ok":true}'),
                           "https://api.juxin.example", FAKE_KEY, {"prompt": "x"})
        check("[1] 创建 2xx 但没有任务 id → 仍 502（不敢当成功）",
              getattr(err, "status", None) == 502, f"status={getattr(err, 'status', None)}")

        err, _ = call_sdgf(bridge_server.sdgf_query,
                           _FakeRaisingOpener(_http_error("u", 403, body403)),
                           "https://api.juxin.example", FAKE_KEY, "task-abc")
        check("[1] 查询链 403 → 仍 502（透传只给创建链，查询阶段状态不明语义正确）",
              getattr(err, "status", None) == 502, f"status={getattr(err, 'status', None)}")

        # ════════════════════════════════════════════════ [2] 四态分类
        print("2) 四态分类：创建阶段 4xx → rejected；拿到 task_id 之后 → 保持原语义")
        seed("auto-9601", state="approved", gen_error=LEGACY_403,
             submit_unknown={"at": "2026-11-12T09:05:52+08:00", "reason": LEGACY_403,
                             "dispatch": "dispatched"},
             dispatch={"phase": "dispatched", "at": "2026-11-12T09:05:46+08:00"})
        seed("auto-9602", state="awaiting_approval", gen_error=NEW_403)
        seed("auto-9603", state="awaiting_approval",
             gen_error='创建请求被明确拒绝（HTTP 400）：渠道创建端点明确拒绝 HTTP 400：{"error":"duration 不支持"}')
        seed("auto-9604", state="awaiting_approval",
             gen_error="渠道前置拒绝（HTTP 409）：并发闸满")
        seed("auto-9605", state="approved", cloud_task_id="task-abc123",
             gen_error='渠道响应 HTTP 403：{"code":"fail_to_fetch_task"}')
        seed("auto-9606", state="approved",
             submit_unknown={"at": "2026-11-12T09:20:00+08:00",
                             "reason": "提交结果未知：bridge POST /api/sdgf/video/run 连不上（conn refused）",
                             "dispatch": "dispatched"},
             dispatch={"phase": "dispatched", "at": "2026-11-12T09:20:00+08:00"},
             gen_error="提交结果未知（云端可能已收单，勿重复提交）：渠道连不上（conn refused）")
        seed("auto-9607", state="generating", cloud_task_id="task-live9",
             gen_error='渠道响应 HTTP 403：{"code":"fail_to_fetch_task"}')
        seed("auto-9608", state="approved")
        seed("auto-9609", state="approved", gen_job="job-1",
             gen_error='渠道响应 HTTP 403：{"code":"fail_to_fetch_task"}')

        expect = {"auto-9601": "rejected", "auto-9602": "rejected", "auto-9603": "rejected",
                  "auto-9604": "rejected", "auto-9605": "unknown", "auto-9606": "unknown",
                  "auto-9607": "inflight", "auto-9608": "none", "auto-9609": "unknown"}
        for local_id, form in expect.items():
            got = (row_of(local_id).get("submit") or {}).get("form")
            check(f"[2] {local_id} → form={form}", got == form, f"得到 {got!r}")

        check("[2] 形态取值仍只在四类 + none 之内（不发明第五种）",
              all((row_of(i).get("submit") or {}).get("form") in submit_view.FORMS for i in expect))

        # ════════════════════════════════════════════════ [3] 人话与许可矩阵
        print("3) 人话：可改后直接重提；原因只引原文标记")
        sv = row_of("auto-9601")["submit"]
        guide = " ".join(sv["guidance"])
        causes = " ".join(sv["causes"])
        check("[3] rejected 指引含「直接重提」", "直接重提" in guide, guide[:200])
        check("[3] rejected 指引不含「先核对」（那是 unknown 的话）", "先核对" not in guide, guide[:200])
        check("[3] rejected 指引写明「无需解除阻断」", "无需解除阻断" in guide, guide[:200])
        check("[3] rejected 指引保留 Request ID 联系渠道支持的原文提示",
              "Request ID" in guide, guide[:200])
        check("[3] rejected 原因引原文标记（HTTP 403），不推断成「key 失效」",
              "HTTP 403" in causes and "key 失效" not in causes, causes[:200])
        check("[3] 创建 403 rejected：blocks_retry=False（可重提）、can_resume=False（没单可续）",
              sv["blocks_retry"] is False and sv["can_resume"] is False,
              json.dumps({k: sv[k] for k in ("blocks_retry", "can_resume")}, ensure_ascii=False))
        check("[3] 短错误码从原文抠出 HTTP 403（不翻译原因）", sv["error_code"] == "HTTP 403",
              repr(sv.get("error_code")))
        check("[3] 有 cloud_task_id 的 403 仍是 unknown + 禁重提（不许当创建拒绝）",
              row_of("auto-9605")["submit"]["blocks_retry"] is True)

        # ════════════════════════════════════════════════ [4] 自愈判定边界
        print("4) 自愈判据三件套：原文 4xx ∧ 无云端 id/job ∧ 阻断还在")
        ev_9601 = submit_view.create_reject_evidence(state.load(TMP_ROOT / "auto-9601"))
        check("[4] 旧误标夹具（auto-9601）= 命中自愈证据", ev_9601 == "403", repr(ev_9601))
        check("[4] 无阻断标记的 rejected（auto-9602）不算自愈（无可解除的东西）",
              submit_view.create_reject_evidence(state.load(TMP_ROOT / "auto-9602")) == "")
        check("[4] 有云端 taskId（auto-9605）不算自愈",
              submit_view.create_reject_evidence(state.load(TMP_ROOT / "auto-9605")) == "")
        check("[4] 断连型未知（auto-9606）不算自愈（原文里没有创建端点 4xx）",
              submit_view.create_reject_evidence(state.load(TMP_ROOT / "auto-9606")) == "")

        # ════════════════════════════════════════════════ [5] hub 写路径自愈
        print("5) hub 提交路径：旧误标不再挡「直接重提」，只发 1 次创建")
        tdir = seed_approved_for_submit("auto-9611", gen_error=LEGACY_403,
                                       submit_unknown={"at": "2026-11-12T09:05:52+08:00",
                                                       "reason": LEGACY_403, "dispatch": "dispatched"},
                                       dispatch={"phase": "dispatched",
                                                 "at": "2026-11-12T09:05:46+08:00"})
        created.append("auto-9611")
        job: dict = {}
        blocked_err = ""
        try:
            job = hub_api.start_submit("auto-9611", FAKE_KEY)
        except Exception as exc:                       # noqa: BLE001
            blocked_err = f"{type(exc).__name__}: {exc}"
        check("[5] 旧误标夹具上提交被受理（不再报「未知收单 / 先核对」）",
              bool(job.get("job_id")) and not blocked_err, blocked_err)
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            if (hub_api.HUB_JOBS.get(job.get("job_id")) or {}).get("status") != "running":
                break
            time.sleep(0.1)
        sv = row_of("auto-9611")["submit"]
        check("[5] 终态 form=rejected（渠道 403 明确拒绝 → 受控回退）", sv["form"] == "rejected",
              f"form={sv['form']} state={row_of('auto-9611').get('state')}")
        check("[5] state 回到 awaiting_approval（改配置后重批再提这条正路）",
              row_of("auto-9611").get("state") == "awaiting_approval",
              str(row_of("auto-9611").get("state")))
        healed = state.load(tdir)
        check("[5] submit_unknown 已清（防重复扣费的前提不成立，阻断作废）",
              not healed.get("submit_unknown"),
              json.dumps(healed.get("submit_unknown"), ensure_ascii=False)[:160])
        check("[5] dispatch 不再是 prepared/dispatched（不再判「云端可能已收单」）",
              not state.dispatch_blocks_create(healed), str(healed.get("dispatch"))[:160])
        check("[5] gen_error 换成明确的创建拒绝（原文标记齐全）",
              "渠道创建端点明确拒绝 HTTP 403" in str(healed.get("gen_error") or ""),
              str(healed.get("gen_error") or "")[:200])
        hist = " ".join(str(h.get("note") or "") for h in (healed.get("history") or []))
        check("[5] history 记了这次误判自愈（可追溯，不是悄悄改盘）",
              "历史误判自动解除" in hist, hist[-200:])

        # ════════════════════════════════════════════════ [6] CLI 写路径自愈
        print("6) CLI / runner 那条跨进程路径：do_submit 直调同样自愈")
        tdir2 = seed_approved_for_submit("auto-9612", gen_error=LEGACY_403,
                                        submit_unknown={"at": "2026-11-12T09:05:52+08:00",
                                                        "reason": LEGACY_403, "dispatch": "dispatched"},
                                        dispatch={"phase": "dispatched",
                                                  "at": "2026-11-12T09:05:46+08:00"})
        created.append("auto-9612")
        cli_err = ""
        try:
            generate.do_submit(tdir2, verbose=False, api_key=FAKE_KEY)
        except Exception as exc:                       # noqa: BLE001 —— 期望 GenError（渠道拒绝）
            cli_err = f"{type(exc).__name__}: {exc}"
        healed2 = state.load(tdir2)
        check("[6] CLI 路径没被旧阻断挡（错误是「创建被明确拒绝」，不是「未知收单」）",
              "明确拒绝" in cli_err and "未知收单" not in cli_err, cli_err[:200])
        check("[6] CLI 路径终态 rejected（state=awaiting_approval + 无 submit_unknown）",
              healed2["state"] == "awaiting_approval" and not healed2.get("submit_unknown"),
              f"state={healed2['state']} blocked={bool(healed2.get('submit_unknown'))}")
        check("[6] CLI 路径 dispatch 已清（不残留 dispatched）",
              not state.dispatch_blocks_create(healed2), str(healed2.get("dispatch"))[:160])

        # ════════════════════════════════════════════════ [7] 不误伤真未知
        print("7) 不误伤：真「结果未知（断连）」仍禁重提，创建调用 0 次")
        seed_approved_for_submit("auto-9613",
                                 gen_error="提交结果未知（云端可能已收单，勿重复提交）：渠道连不上",
                                 submit_unknown={"at": "2026-11-12T09:20:00+08:00",
                                                 "reason": "提交结果未知：连接被拒",
                                                 "dispatch": "dispatched"},
                                 dispatch={"phase": "dispatched",
                                           "at": "2026-11-12T09:20:00+08:00"})
        created.append("auto-9613")
        posts_before = len(stub.posts)
        unknown_err = ""
        try:
            hub_api.start_submit("auto-9613", FAKE_KEY)
        except Exception as exc:                       # noqa: BLE001
            unknown_err = str(exc)
        check("[7] 真未知仍被既有阻断挡住（文案指向核对/解除阻断）",
              bool(unknown_err) and ("核对" in unknown_err or "未知收单" in unknown_err),
              unknown_err[:200])
        check("[7] 这次一次创建调用都没发（阻断=零付费风险）", len(stub.posts) == posts_before,
              f"{posts_before} → {len(stub.posts)}")
        check("[7] 真未知夹具的 form 仍是 unknown（自愈只认创建端点 4xx 原文）",
              (row_of("auto-9613").get("submit") or {}).get("form") == "unknown",
              str((row_of("auto-9613").get("submit") or {}).get("form")))
        print(f"   全程创建调用 {len(stub.posts)} 次（全部被桩拦下，零真实付费）")

        # ════════════════════════════════════════════════ [8] 静态守卫
        print("8) 静态守卫：透传只挂创建链、自愈判定单源、判定层不写盘")
        server_py = (ROOT / "ui" / "server.py").read_text(encoding="utf-8")
        gen_py = (ROOT / "automation" / "generate.py").read_text(encoding="utf-8")
        hub_py = (ROOT / "ui" / "hub_api.py").read_text(encoding="utf-8")
        view_py = (ROOT / "automation" / "submit_view.py").read_text(encoding="utf-8")
        check("[8] passthrough_4xx=True 只出现在创建链（sdgf_create 一处）",
              server_py.count("passthrough_4xx=True") == 1,
              str(server_py.count("passthrough_4xx=True")))
        check("[8] 查询/下载链仍走 502 包装（「渠道响应 HTTP」那行还在，不是全量放开）",
              'raise ApiError(502, f"渠道响应 HTTP {err.code}' in server_py)
        check("[8] 创建阶段 4xx 判定只有一处生产（submit_view._CREATE_4XX_RE）",
              view_py.count("_CREATE_4XX_RE") == 2, str(view_py.count("_CREATE_4XX_RE")))
        check("[8] 自愈判定单源：generate 与 hub_api 都调 submit_view.create_reject_evidence",
              "submit_view.create_reject_evidence" in gen_py
              and "heal_create_reject_block" in hub_py)
        check("[8] 判定层不写盘（submit_view 无 save/write/mkdir/rmtree）",
              not any(tok in view_py for tok in ("state.save(", "write_text(", "mkdir(", "rmtree(")))
        check("[8] 没有为这件事另造端点（server.py 无 create-reject 路由）",
              "create-reject" not in server_py)
    finally:
        stub.restore()
        # ════════════════════════════════════════════════ [9] 清理
        print("9) 清理：临时根连根删；真实 projects 一格不少（真实任务只读）")
        hub_api.HUB_JOBS.clear()
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
        after = real_projects()
        check("[9] 真实 projects 目录集前后一致（本次探针零写入真实任务）",
              before == after, f"新增 {sorted(after - before)} / 丢失 {sorted(before - after)}")
        print(f"   临时根已删（{TMP_ROOT} 存在={TMP_ROOT.exists()}）")
    return summary()


if __name__ == "__main__":
    raise SystemExit(main())
