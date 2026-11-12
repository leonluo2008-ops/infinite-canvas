#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""探针：拆解失败的「云端原文」优先回写（discuss 侧）。

跑法：wanrepro/ 下 `python automation/probe_cloudraw_fail.py`（隔离，自清理，退出码 0=全绿）。

缺陷背景：analyze.py 退出码 3 时 discuss 只拿到桥侧 tail（analyze.py 输出的最后一段），
extract.py 打印的云端原始错误行被它的总结行挤出窗口 → 任务.json.failed_reason 只剩
「拆解失败（退出码 3）…」这类转述。修复后云端原文由 extract.py 落盘
`analysis/<dir>/拆解错误.json`（schema 冻结：error_type/message/finish_reason/raw_response），
discuss 的失败分支先读它并把「云端原文：{message}」摆到主位。

锁的是「用户在任务详情里看到的是不是云端原文」，全程真实 HTTP（进程内假桥）+ 真实
run_discuss + 真实 任务.json，夹具全部在临时根里：

  [1] 有 拆解错误.json → failed_reason 以「云端原文：」开头且含完整云端消息；退出码 /
      finish_reason / 原转述行 / raw_response 全部作为附属信息保留；evidence.cloud_error
      逐字透传（不截断/不摘要）、cloud_raw=on_disk
  [2] 无 拆解错误.json → 原转述照旧（开头逐字）+「云端原文：未落盘」，cloud_raw=missing，
      evidence 里没有 cloud_error（不伪造）
  [3] 桥侧连转述都没有 → AT-18 冻结文案兜底（老契约不动）
  [4] 拆解错误.json 坏 JSON / message 空 → 视为未落盘（不崩、不伪装）
  [5] 成功路径零变化：桥 done + 产物齐 → awaiting_assets，任务.json 无 failed_* 字段；
      目录里遗留的旧 拆解错误.json 不会被误判成失败
  [6] 非桥侧失败（source != bridge_exit）不读 拆解错误.json：遗留文件不许错挂到本次失败

隔离手法：
  · WANREPRO_AUTO_DIR → 本次临时根（任务根唯一来源 automation.config.PROJECTS_DIR）；
  · WANREPRO_BRIDGE → 本探针自起的可编排假桥（/api/tasks、/api/lapian/run、/api/job/<hex>）；
  · discuss._ANALYSIS_ROOT → 本次临时 analysis 根（真实 analysis/reverse 一个字节不碰）；
  · 夹具一律 auto-99xx、桥 id 一律 probe-cloud-*；跑完整个临时根删除。
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]           # wanrepro/
TMP_ROOT = Path(tempfile.mkdtemp(prefix="probe-cloudraw-"))
TMP_ANALYSIS = TMP_ROOT / "_analysis"                # discuss._ANALYSIS_ROOT 的替身
PROJECTS = TMP_ROOT / "projects"
PROJECTS.mkdir(parents=True, exist_ok=True)

RESULTS: list[tuple[str, bool]] = []


def check(name: str, cond, detail: str = "") -> bool:
    ok = bool(cond)
    RESULTS.append((name, ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail and not ok else ""))
    return ok


# ---------------------------------------------------------------- 可编排假桥

class ProbeBridge:
    """进程内假 bridge：任务列表（产物三标志可切）+ lapian/run + job 轮询。"""

    def __init__(self) -> None:
        self.bridge_id = "probe-cloud-0000"
        self.jobs: dict[str, dict | None] = {}
        self.default_job: dict | None = None
        self.has = {"has_disc": False, "has_check": False, "has_prompt": False}
        self.products_after_run = False        # lapian/run 之后产物是否齐（成功路径用）
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                return

            def _send(self, code: int, payload: dict) -> None:
                raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):                  # noqa: N802
                if self.path == "/api/lapian/run":
                    n = int(self.headers.get("Content-Length") or 0)
                    if n:
                        self.rfile.read(n)
                    job = f"{len(outer.jobs) + 1:08x}"
                    outer.jobs[job] = outer.default_job
                    if outer.products_after_run:
                        outer.has = {"has_disc": True, "has_check": True, "has_prompt": True}
                    self._send(200, {"job": job})
                    return
                self._send(404, {"error": "probe bridge 不认这个 POST"})

            def do_GET(self):                   # noqa: N802
                if self.path == "/api/tasks":
                    item = {"id": outer.bridge_id, **outer.has}
                    self._send(200, {"tasks": [item]})
                    return
                if self.path.startswith("/api/job/"):
                    job = self.path.rsplit("/", 1)[-1]
                    rec = outer.jobs.get(job)
                    if rec is None:
                        self._send(404, {"error": "没有这个任务号（可能服务重启过）"})
                        return
                    self._send(200, rec)
                    return
                self._send(404, {"error": "probe bridge 不认这个 GET"})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = int(self.httpd.server_address[1])
        threading.Thread(target=self.httpd.serve_forever, daemon=True,
                         name="probe-cloudraw-bridge").start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"


BRIDGE = ProbeBridge()
os.environ["WANREPRO_AUTO_DIR"] = str(PROJECTS)      # 必须在 import config 之前
os.environ["WANREPRO_BRIDGE"] = BRIDGE.base
os.environ["WANREPRO_POLL_INTERVAL"] = "0.01"        # 轮询加速（生产默认 3s 不变）
sys.path.insert(0, str(ROOT))

from automation import config, discuss, state  # noqa: E402

discuss._ANALYSIS_ROOT = TMP_ANALYSIS                # 只读真实 analysis 的规矩：换成临时根
assert Path(config.PROJECTS_DIR).resolve() == PROJECTS.resolve(), config.PROJECTS_DIR


def seed(local_id: str, *, bridge_id: str, state_name: str = "ingested") -> Path:
    d = PROJECTS / local_id
    d.mkdir(parents=True, exist_ok=True)
    task = {"local_id": local_id, "bridge_id": bridge_id, "state": state_name,
            "created_at": "2026-11-12T19:20:00+08:00", "intent": "云端原文探针夹具",
            "history": [{"at": "2026-11-12T19:20:00+08:00", "from": None,
                         "to": state_name, "note": "夹具"}]}
    (d / "任务.json").write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
    return d


def task_of(d: Path) -> dict:
    return json.loads((d / "任务.json").read_text(encoding="utf-8"))


def plant_cloud(bridge_id: str, payload) -> Path:
    """模拟 extract.py 落盘（schema 冻结：error_type/message/finish_reason/raw_response）。"""
    folder = TMP_ANALYSIS / bridge_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / discuss.CLOUD_ERROR_NAME
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def run_discuss(task_dir: Path) -> tuple[bool, str, str]:
    """跑 run_discuss → (是否任务级失败, 异常文案, 终态)。"""
    try:
        final = discuss.run_discuss(task_dir, verbose=False)
        return False, "", str(final)
    except discuss.DiscussError as exc:
        return True, str(exc), ""
    except Exception as exc:                 # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}", ""


# 一次真实云端失败的样子：message 是完整原文（含 HTTP 401 具体文案），raw_response 是响应体前 500 字
CLOUD_MESSAGE = ("接口返回 error：HTTP 401 Unauthorized｜error=invalid_api_key｜"
                 "message=Incorrect API key provided: sk-abc***123. "
                 "You can find your API key at https://platform.example.com/keys")
RAW_RESPONSE = ('{"error": {"message": "Incorrect API key provided: sk-abc***123. '
                'You can find your API key at https://platform.example.com/keys", '
                '"type": "invalid_request_error", "code": "invalid_api_key"}}')
CLOUD_FIXTURE = {"error_type": "ApiError", "message": CLOUD_MESSAGE,
                 "finish_reason": "length", "raw_response": RAW_RESPONSE}
STDERR_TAIL = ("Traceback (most recent call last):\n"
               '  File "pipeline/reverse/extract.py", line 593, in call_model\n'
               "    raise ApiError(...)\n"
               "ERROR API 失败：接口返回 error：HTTP 401 Unauthorized")
BRIDGE_ERROR = f"analyze.py 退出 3：{STDERR_TAIL}"

print("=" * 78)
print("探针：拆解失败「云端原文」优先回写（discuss 侧）")
print(f"隔离根：{TMP_ROOT}")
print("=" * 78)

# ---------------------------------------------------------------- [1] 有云端原文
print("\n[1] analysis/<dir>/拆解错误.json 存在 → failed_reason 主位是云端原文，转述降为附属")
D1 = seed("auto-9901", bridge_id="probe-cloud-a")
plant_cloud("probe-cloud-a", CLOUD_FIXTURE)
BRIDGE.bridge_id = "probe-cloud-a"
BRIDGE.jobs.clear()
BRIDGE.products_after_run = False
BRIDGE.default_job = {"status": "error", "error": BRIDGE_ERROR}
ok, msg, _ = run_discuss(D1)
t1 = task_of(D1)
reason1 = str(t1.get("failed_reason") or "")
ev1 = t1.get("failed_evidence") or {}
print(f"    failed_reason（{len(reason1)} 字）：\n      " + reason1.replace("\n", "\n      "))
check("[1] 抛的是任务级失败", ok, msg)
check("[1] 任务终态 failed", t1.get("state") == "failed", str(t1.get("state")))
check("[1] failed_reason 以「云端原文：」开头",
      reason1.startswith(f"云端原文：{CLOUD_MESSAGE}"), repr(reason1[:80]))
check("[1] 含完整云端消息（逐字，不被截断）", CLOUD_MESSAGE in reason1, repr(reason1))
check("[1] 附属信息带退出码 3", "退出码 3" in reason1, repr(reason1))
check("[1] 附属信息带 finish_reason", "finish_reason=length" in reason1, repr(reason1))
check("[1] 原转述行保留（不丢）", "［转述］" in reason1 and BRIDGE_ERROR in reason1, repr(reason1))
check("[1] raw_response 保留在附属信息", "［原始响应］" in reason1 and RAW_RESPONSE[:40] in reason1,
      repr(reason1))
check("[1] 云端原文不是主位之后的唯一信息（主位=message 首行）",
      reason1.splitlines()[0] == f"云端原文：{CLOUD_MESSAGE}", repr(reason1.splitlines()[:1]))
check("[1] evidence.cloud_error 逐字透传（四键齐全、不截断）",
      ev1.get("cloud_error") == CLOUD_FIXTURE, json.dumps(ev1.get("cloud_error"), ensure_ascii=False))
check("[1] evidence.cloud_raw=on_disk", ev1.get("cloud_raw") == "on_disk", str(ev1))
check("[1] evidence 其余既有键不受影响（trigger/reason_source/fallback_text）",
      ev1.get("trigger") == "bridge_exit" and ev1.get("reason_source") == "bridge_exit"
      and ev1.get("fallback_text") is False, str(ev1))
check("[1] 退出码 3 仍落 evidence", ev1.get("exit_code") == 3, str(ev1))
check("[1] history 只有一条 failed 迁移",
      len([r for r in t1.get("history") or [] if r.get("to") == "failed"]) == 1, str(t1.get("history")))
check("[1] history note 与 failed_reason 同源",
      [r for r in t1["history"] if r.get("to") == "failed"][0].get("note") == reason1, str(t1.get("history")))

# ---------------------------------------------------------------- [2] 无云端原文
print("\n[2] 没有 拆解错误.json（旧产物）→ 原转述照旧 + 「云端原文：未落盘」，不伪装")
D2 = seed("auto-9902", bridge_id="probe-cloud-b")
BRIDGE.bridge_id = "probe-cloud-b"
BRIDGE.jobs.clear()
BRIDGE.default_job = {"status": "error", "error": BRIDGE_ERROR}
ok, msg, _ = run_discuss(D2)
t2 = task_of(D2)
reason2 = str(t2.get("failed_reason") or "")
ev2 = t2.get("failed_evidence") or {}
print(f"    failed_reason：\n      " + reason2.replace("\n", "\n      "))
check("[2] 任务终态 failed", t2.get("state") == "failed", str(t2.get("state")))
check("[2] 转述与改动前逐字一致（开头即桥侧原文）", reason2.startswith(BRIDGE_ERROR), repr(reason2))
check("[2] 显式标「云端原文：未落盘」", "云端原文：未落盘" in reason2, repr(reason2))
check("[2] evidence.cloud_raw=missing", ev2.get("cloud_raw") == "missing", str(ev2))
check("[2] evidence 里没有 cloud_error（不伪造原文）", "cloud_error" not in ev2, str(ev2))

# ---------------------------------------------------------------- [3] 连转述都没有
print("\n[3] 桥侧转述也缺失（job.error 空、无进度.json、无云端原文）→ AT-18 冻结文案兜底")
D3 = seed("auto-9903", bridge_id="probe-cloud-c")
BRIDGE.bridge_id = "probe-cloud-c"
BRIDGE.jobs.clear()
BRIDGE.default_job = {"status": "error"}
ok, msg, _ = run_discuss(D3)
t3 = task_of(D3)
FALLBACK = state.fake_run_reason("discussing")
check("[3] 任务终态 failed", t3.get("state") == "failed", str(t3.get("state")))
check("[3] failed_reason = 冻结文案（老契约不变）", t3.get("failed_reason") == FALLBACK,
      repr(t3.get("failed_reason")))
check("[3] evidence.fallback_text=true",
      (t3.get("failed_evidence") or {}).get("fallback_text") is True, str(t3.get("failed_evidence")))

# ---------------------------------------------------------------- [4] 坏文件
print("\n[4] 拆解错误.json 坏了（非 JSON / message 空）→ 视为未落盘，不崩不伪装")
D4 = seed("auto-9904", bridge_id="probe-cloud-d")
plant_cloud("probe-cloud-d", "<html>502 Bad Gateway</html>")
BRIDGE.bridge_id = "probe-cloud-d"
BRIDGE.jobs.clear()
BRIDGE.default_job = {"status": "error", "error": BRIDGE_ERROR}
ok, msg, _ = run_discuss(D4)
t4 = task_of(D4)
check("[4] 任务终态 failed（坏文件不拖垮主链）", t4.get("state") == "failed", str(t4.get("state")))
check("[4] 坏 JSON → 未落盘标注", "云端原文：未落盘" in str(t4.get("failed_reason") or ""),
      repr(t4.get("failed_reason")))
check("[4] 坏 JSON → evidence 无 cloud_error",
      "cloud_error" not in (t4.get("failed_evidence") or {}), str(t4.get("failed_evidence")))

D4b = seed("auto-9904b", bridge_id="probe-cloud-d2")
plant_cloud("probe-cloud-d2", {"error_type": "ApiError", "message": "  ",
                               "finish_reason": None, "raw_response": ""})
BRIDGE.bridge_id = "probe-cloud-d2"
BRIDGE.jobs.clear()
BRIDGE.default_job = {"status": "error", "error": BRIDGE_ERROR}
run_discuss(D4b)
t4b = task_of(D4b)
check("[4] message 空壳 → 未落盘（不拿空壳当原文）",
      "云端原文：未落盘" in str(t4b.get("failed_reason") or ""), repr(t4b.get("failed_reason")))

# ---------------------------------------------------------------- [5] 成功路径零变化
print("\n[5] 成功路径零变化：桥 done + 产物齐 → awaiting_assets，无 failed_* 字段")
D5 = seed("auto-9905", bridge_id="probe-cloud-ok")
plant_cloud("probe-cloud-ok", CLOUD_FIXTURE)         # 目录里遗留的旧失败证据
BRIDGE.bridge_id = "probe-cloud-ok"
BRIDGE.jobs.clear()
BRIDGE.products_after_run = True
BRIDGE.default_job = {"status": "done", "elapsed": 1}
ok, msg, final = run_discuss(D5)
t5 = task_of(D5)
check("[5] 成功返回 awaiting_assets", final == "awaiting_assets", f"{final} / {msg}")
check("[5] 任务终态 awaiting_assets", t5.get("state") == "awaiting_assets", str(t5.get("state")))
check("[5] 成功路径不写 failed_reason", not t5.get("failed_reason"), repr(t5.get("failed_reason")))
check("[5] 成功路径不写 failed_evidence", not t5.get("failed_evidence"), str(t5.get("failed_evidence")))
check("[5] 遗留 拆解错误.json 不被误判为失败",
      (TMP_ANALYSIS / "probe-cloud-ok" / discuss.CLOUD_ERROR_NAME).is_file(),
      "夹具应仍在盘上（成功路径不消费）")

# ---------------------------------------------------------------- [6] 非桥侧失败不读云端原文
print("\n[6] source!=bridge_exit 的失败不读 拆解错误.json（遗留文件不许错挂到本次失败）")
D6 = seed("auto-9906", bridge_id="probe-cloud-stale", state_name="discussing")
plant_cloud("probe-cloud-stale", CLOUD_FIXTURE)
res6 = discuss._fail_task(D6, "桥报 done 但 has_disc/check/prompt 不全——如实记失败", source="discuss")
t6 = task_of(D6)
ev6 = t6.get("failed_evidence") or {}
check("[6] 仍判 failed 并保留转述", t6.get("state") == "failed"
      and str(t6.get("failed_reason") or "").startswith("桥报 done 但"), repr(t6.get("failed_reason")))
check("[6] 标了未落盘（不借用遗留原文）", "云端原文：未落盘" in str(t6.get("failed_reason") or ""),
      repr(t6.get("failed_reason")))
check("[6] evidence 无 cloud_error", "cloud_error" not in ev6, str(ev6))
check("[6] 返回的 reason 与盘上一致", res6.get("reason") == t6.get("failed_reason"), str(res6))

# ---------------------------------------------------------------- 汇总
print("\n" + "=" * 78)
passed = sum(1 for _, ok in RESULTS if ok)
print(f"共 {len(RESULTS)} 项断言：PASS {passed} / FAIL {len(RESULTS) - passed}")
for name, ok in RESULTS:
    if not ok:
        print(f"  FAIL ｜ {name}")
print("=" * 78)

BRIDGE.httpd.shutdown()
shutil.rmtree(TMP_ROOT, ignore_errors=True)
raise SystemExit(0 if passed == len(RESULTS) else 1)
