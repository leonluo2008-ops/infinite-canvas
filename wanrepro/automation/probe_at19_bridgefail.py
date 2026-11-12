#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AT-19 探针：桥侧失败回写（analyze.py 退出码非 0 → 任务.json failed，原文优先）。

跑法：wanrepro/ 下 `python automation/probe_at19_bridgefail.py`（隔离，自清理，退出码 0=全绿）。

锁的是「用户看到的是不是桥侧原文」这件事本身，不是函数返回值。全程真实 HTTP（进程内假桥）
+ 真实 run_discuss + 真实任务.json，夹具全部在临时根里：

  [1] 退出码 3（job.error 带 stderr 尾部）→ 任务 failed；failed_reason **以桥侧原文开头**
      （原文逐字保留，不降级成兜底句）+「云端原文：未落盘」标注（没有 拆解错误.json），
      退出码进 failed_evidence，history 只 +1（附前后对比）
  [2] 原文缺失（job.error 空、进度.json 无失败标记）→ 走 AT-18 冻结文案兜底，
      failed_evidence.fallback_text=true
  [3] job 记录丢失（服务重启 → /api/job 404）+ 进度.json 已写「失败」→ 仍判 failed，
      原因以进度.json 的结构化 error 开头（+ 未落盘标注），退出码从文案里取回
  [4] 幂等：同一失败重复触发 → 不写 history、不刷新 failed_at；先被看门狗按兜底句判死、
      随后拿到原文 → 只升级原因（history / failed_at 不动）
  [5] 职责边界：discuss.py 全程不动 进度.json（字节哈希不变），也不存在第二套写盘入口；
      analyze.py 侧 mark_failure 写的 进度.json 与 discuss 的解析口径同源

隔离手法：
  · WANREPRO_AUTO_DIR → 本次临时根（任务根唯一来源 automation.config.PROJECTS_DIR）；
  · WANREPRO_BRIDGE → 本探针自起的可编排假桥（/api/tasks、/api/lapian/run、/api/job/<hex>）；
  · discuss._ANALYSIS_ROOT → 本次临时 analysis 根（真实 analysis/reverse 一个字节不碰）；
  · 夹具一律 auto-98xx、桥 id 一律 probe-at19-*；跑完整个临时根删除。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]           # wanrepro/
TMP_ROOT = Path(tempfile.mkdtemp(prefix="probe-at19-bridgefail-"))
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
    """进程内假 bridge：任务列表 + lapian/run + job 轮询（记录可随时改）。"""

    def __init__(self) -> None:
        self.bridge_id = "probe-at19-0000"
        self.jobs: dict[str, dict | None] = {}      # job id → 记录；None = 404（服务重启过）
        self.default_job: dict | None = None        # 未单独登记的 job 一律按它回
        self.last_job = ""
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
                    outer.last_job = job
                    outer.jobs[job] = outer.default_job
                    self._send(200, {"job": job})
                    return
                self._send(404, {"error": "probe bridge 不认这个 POST"})

            def do_GET(self):                   # noqa: N802
                if self.path == "/api/tasks":
                    self._send(200, {"tasks": [{"id": outer.bridge_id, "has_disc": False,
                                                "has_check": False, "has_prompt": False}]})
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
                         name="probe-at19-bridge").start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"


BRIDGE = ProbeBridge()
os.environ["WANREPRO_AUTO_DIR"] = str(PROJECTS)      # 必须在 import config 之前
os.environ["WANREPRO_BRIDGE"] = BRIDGE.base
sys.path.insert(0, str(ROOT))

from automation import config, discuss, state  # noqa: E402

discuss._ANALYSIS_ROOT = TMP_ANALYSIS                # 只读真实 analysis 的规矩：换成临时根
assert Path(config.PROJECTS_DIR).resolve() == PROJECTS.resolve(), config.PROJECTS_DIR


def module_by_path(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader, f"导不进 {path}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ANALYZE = module_by_path("probe_at19_analyze", ROOT / "analyze.py")

STDERR_TAIL = ("Traceback (most recent call last):\n"
               '  File "pipeline/reverse/extract.py", line 412, in main\n'
               "    raise RuntimeError('镜头 3 资产名缺失')\n"
               "RuntimeError: 镜头 3 资产名缺失")
BRIDGE_ERROR = f"analyze.py 退出 3：{STDERR_TAIL}"


def seed(local_id: str, *, bridge_id: str, state_name: str = "ingested") -> Path:
    d = PROJECTS / local_id
    d.mkdir(parents=True, exist_ok=True)
    task = {"local_id": local_id, "bridge_id": bridge_id, "state": state_name,
            "created_at": "2026-11-12T17:00:00+08:00", "intent": "AT-19 探针夹具（隔离根内）",
            "history": [{"at": "2026-11-12T17:00:00+08:00", "from": None,
                         "to": state_name, "note": "夹具"}]}
    (d / "任务.json").write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
    return d


def task_of(d: Path) -> dict:
    return json.loads((d / "任务.json").read_text(encoding="utf-8"))


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_discuss_expect_fail(task_dir: Path) -> tuple[bool, str]:
    try:
        discuss.run_discuss(task_dir, verbose=False)
    except discuss.DiscussError as exc:
        return True, str(exc)
    except Exception as exc:                 # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    return False, "居然没抛（应为任务级失败）"


def dump_history(label: str, d: Path) -> None:
    task = task_of(d)
    print(f"    {label} history：")
    for row in task.get("history") or []:
        print(f"      {row.get('at')}  {row.get('from')} → {row.get('to')}  ｜{row.get('note')}")


# ---------------------------------------------------------------- [1] 退出码 3 原文
print("\n[1] analyze.py 退出码 3（job.error 带 stderr 尾部）→ failed，原因=桥侧原文")
D1 = seed("auto-9801", bridge_id="probe-at19-a", state_name="ingested")
dump_history("跑前", D1)
BRIDGE.bridge_id = "probe-at19-a"
BRIDGE.jobs.clear()
BRIDGE.default_job = {"status": "error", "error": BRIDGE_ERROR}
ok, msg = run_discuss_expect_fail(D1)
dump_history("跑后", D1)
t1 = task_of(D1)
check("[1] 抛的是任务级失败", ok, msg)
check("[1] 任务终态 failed", t1["state"] == "failed", str(t1["state"]))
check("[1] failed_reason 以桥侧原文开头（逐字保留）",
      str(t1.get("failed_reason") or "").startswith(BRIDGE_ERROR),
      repr(t1.get("failed_reason")))
check("[1] 缺 拆解错误.json → 显式标「云端原文：未落盘」",
      "云端原文：未落盘" in str(t1.get("failed_reason") or ""),
      repr(t1.get("failed_reason")))
check("[1] failed_evidence.cloud_raw=missing",
      (t1.get("failed_evidence") or {}).get("cloud_raw") == "missing",
      str(t1.get("failed_evidence")))
check("[1] 原文不是冻结文案（不误用兜底）",
      t1.get("failed_reason") != state.fake_run_reason("discussing"),
      repr(t1.get("failed_reason")))
check("[1] failed_evidence.trigger=bridge_exit",
      (t1.get("failed_evidence") or {}).get("trigger") == "bridge_exit", str(t1.get("failed_evidence")))
check("[1] 退出码 3 落到 evidence", (t1.get("failed_evidence") or {}).get("exit_code") == 3,
      str(t1.get("failed_evidence")))
failed_rows = [r for r in t1["history"] if r.get("to") == "failed"]
check("[1] history 只有一条 failed 迁移", len(failed_rows) == 1, str(t1["history"]))
check("[1] history note 与 failed_reason 同源", failed_rows and failed_rows[0]["note"] == t1["failed_reason"],
      str(failed_rows))
check("[1] failed_at 已落盘", bool(t1.get("failed_at")), str(t1.get("failed_at")))

# ---------------------------------------------------------------- [2] 原文缺失 → 兜底
print("\n[2] 原文缺失（job.error 空 + 无进度.json）→ AT-18 冻结文案兜底")
D2 = seed("auto-9802", bridge_id="probe-at19-b2empty")
BRIDGE.bridge_id = "probe-at19-b2empty"
BRIDGE.jobs.clear()
BRIDGE.default_job = {"status": "error"}                   # 没有 error 字段
ok, msg = run_discuss_expect_fail(D2)
t2 = task_of(D2)
FALLBACK = state.fake_run_reason("discussing")
check("[2] 任务终态 failed", t2["state"] == "failed", str(t2["state"]))
check("[2] failed_reason = 冻结文案（原文缺失才兜底）", t2.get("failed_reason") == FALLBACK,
      repr(t2.get("failed_reason")))
check("[2] evidence 标了 fallback_text=true",
      (t2.get("failed_evidence") or {}).get("fallback_text") is True, str(t2.get("failed_evidence")))

# ---------------------------------------------------------------- [3] job 丢失 + 进度.json
print("\n[3] job 记录丢失（404）+ 进度.json 已写失败 → 仍判 failed，原因取结构化 error")
D3 = seed("auto-9803", bridge_id="probe-at19-b3lost")
PROGRESS_DIR = TMP_ANALYSIS / "probe-at19-b3lost"
PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
(PROGRESS_DIR / "进度.json").write_text(json.dumps(
    {"stage": "失败", "error": "拆解失败（退出码 3；详见日志）", "ts": 1794473000},
    ensure_ascii=False), encoding="utf-8")
BRIDGE.bridge_id = "probe-at19-b3lost"
BRIDGE.jobs.clear()
BRIDGE.default_job = None                                 # 服务重启过：job 404
ok, msg = run_discuss_expect_fail(D3)
t3 = task_of(D3)
check("[3] 任务终态 failed（job 丢了也判失败）", t3["state"] == "failed", str(t3["state"]))
check("[3] 原因以进度.json 的结构化 error 开头",
      str(t3.get("failed_reason") or "").startswith("拆解失败（退出码 3；详见日志）"),
      repr(t3.get("failed_reason")))
check("[3] 缺 拆解错误.json → 显式标「云端原文：未落盘」",
      "云端原文：未落盘" in str(t3.get("failed_reason") or ""),
      repr(t3.get("failed_reason")))
check("[3] 退出码从原文里取回 3", (t3.get("failed_evidence") or {}).get("exit_code") == 3,
      str(t3.get("failed_evidence")))

# ---------------------------------------------------------------- [4] 幂等 + 原文升级
print("\n[4] 幂等：同一失败重复触发不产生第二次迁移；兜底句可被原文原地升级")
hist_before = list(task_of(D1)["history"])
at_before = task_of(D1).get("failed_at")
again = discuss._fail_task(D1, BRIDGE_ERROR, source="bridge_exit", exit_code=3)
t1b = task_of(D1)
check("[4] 重复触发 → changed=false / already=true",
      again.get("changed") is False and again.get("already") is True, str(again))
check("[4] history 条数不变", len(t1b["history"]) == len(hist_before),
      f"{len(hist_before)} → {len(t1b['history'])}")
check("[4] failed_at 不被刷新", t1b.get("failed_at") == at_before,
      f"{at_before} → {t1b.get('failed_at')}")

D4 = seed("auto-9804", bridge_id="probe-at19-b4upgrade", state_name="discussing")
state.fail_orphan_task(D4, reason=state.fake_run_reason("discussing"), trigger="probe")
t4a = task_of(D4)
hist4 = len(t4a["history"])
at4 = t4a.get("failed_at")
up = discuss._fail_task(D4, BRIDGE_ERROR, source="bridge_exit", exit_code=3)
t4b = task_of(D4)
check("[4] 兜底句 + 后来的原文 → 升级 changed=true/upgraded=true",
      up.get("changed") is True and up.get("upgraded") is True, str(up))
check("[4] 升级后 failed_reason 以原文开头（+ 未落盘标注）",
      str(t4b.get("failed_reason") or "").startswith(BRIDGE_ERROR)
      and "云端原文：未落盘" in str(t4b.get("failed_reason") or ""),
      repr(t4b.get("failed_reason")))
check("[4] 升级不写 history", len(t4b["history"]) == hist4, f"{hist4} → {len(t4b['history'])}")
check("[4] 升级不刷新 failed_at", t4b.get("failed_at") == at4, f"{at4} → {t4b.get('failed_at')}")

# ---------------------------------------------------------------- [5] 职责边界
print("\n[5] 职责边界：discuss 不写 进度.json；analyze 是进度.json 的唯一写者")
D5 = seed("auto-9805", bridge_id="probe-at19-b5border")
P5 = TMP_ANALYSIS / "probe-at19-b5border"
P5.mkdir(parents=True, exist_ok=True)
(P5 / "进度.json").write_text(json.dumps({"stage": "抽帧", "ts": 1794473000}, ensure_ascii=False),
                              encoding="utf-8")
before = sha(P5 / "进度.json")
BRIDGE.bridge_id = "probe-at19-b5border"
BRIDGE.jobs.clear()
BRIDGE.default_job = {"status": "error", "error": BRIDGE_ERROR}
run_discuss_expect_fail(D5)
check("[5] discuss 全程未改 进度.json（字节哈希不变）", sha(P5 / "进度.json") == before)
discuss_src = (ROOT / "automation" / "discuss.py").read_text(encoding="utf-8")
check("[5] discuss.py 里没有 进度.json 的写入口", "write_progress" not in discuss_src)
analyze_src = (ROOT / "analyze.py").read_text(encoding="utf-8")
check("[5] analyze.py 里没有 任务.json 的写入口",
      "state.transition(" not in analyze_src and "state.save(" not in analyze_src)

D6 = seed("auto-9806", bridge_id="probe-at19-b6progress")
outdir6 = TMP_ANALYSIS / "probe-at19-b6progress"
ANALYZE.mark_failure(outdir6, "拆解", 3, "详见日志")
prog = json.loads((outdir6 / "进度.json").read_text(encoding="utf-8"))
check("[5] analyze.mark_failure 写 stage=失败", prog.get("stage") == "失败", str(prog))
check("[5] analyze 失败文案带步骤 + 退出码",
      prog.get("error") == "拆解失败（退出码 3；详见日志）", repr(prog.get("error")))
text5, code5 = discuss._progress_failure("probe-at19-b6progress")
check("[5] discuss 能解析 analyze 写的进度.json（同源口径）",
      text5 == prog.get("error") and code5 == 3, f"{text5!r}/{code5}")

# ---------------------------------------------------------------- 汇总
print("")
failed = [n for n, ok in RESULTS if not ok]
print(f"共 {len(RESULTS)} 项断言：PASS {len(RESULTS) - len(failed)} / FAIL {len(failed)}")
if failed:
    for n in failed:
        print(f"  FAIL ｜ {n}")
shutil.rmtree(TMP_ROOT, ignore_errors=True)
raise SystemExit(1 if failed else 0)
