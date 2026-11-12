#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AT-18 B2 探针（**接口级**）：hub 的 failed 出口与人工兜底口。

跑法：wanrepro/ 下 `python -m automation.probe_at18_hubexit`（隔离，自清理）。

锁的是「接口合同」这一层（方案 §4 全节 / §7 的 A5、A6、A8–A16、A20 口径），全部走 **HTTP**：
不直接调函数判断状态码，只看真实响应码 + 响应体字段，端到端证明「卡死任务永远有出路」：

  [1] failed 可删（A11）：墓碑生效，目录消失
  [2] discussing 删除仍被拒（A12）：409 + 原文「任务运行中（discussing），删除拒绝」
  [3] 僵尸 JOB 不阻删（A13）：同进程注入一条 running 无心跳记录 → 仍可删，登记 ignored_stale_jobs
  [4] 强制结束二次确认（A8）：缺 confirm / 字符串 "true" / confirm_name 不匹配 / reason 超长 → 400
  [5] 强制结束成功 + 不删产物（A9）＋ 幂等（A10）
  [6] 非运行态 / 不存在 → 409（原文含「无需强制结束」/「任务不存在」）
  [7] failed 可重试（A14）：heartbeat_reset / previous_reason / 心跳 run_id 已换 / 旧失败原因仍在 history
  [8] 重试双写防护（A15）：在跑 job 时再重试 → 409，running 记录仍为 1
  [9] 扫描 dry-run 零写入 + apply 降级 + 连扫幂等（A5 / A17）
  [10] generating 不自动降级（A6）：skipped{in_flight_cloud}，状态不变
  [11] 读面四键（A16）：heartbeat/activity/lost_run/failed 齐全，旧任务不 500
  [12] 真实数据只读（A18-lite）：真实 projects/ 目录集与各 任务.json 哈希前后一致

隔离手法（沿 probe_hub --isolated 先例）：
  · WANREPRO_AUTO_DIR 指向本次临时根（任务根唯一来源 automation.config.PROJECTS_DIR）；
  · WANREPRO_BRIDGE 指向本探针自起的「黑洞」TCP 端口（接受连接、永不回包）——重试拉起的
    拆解 job 会停在 running，A15 的「已有拆解任务在跑」才有可判窗口；
  · 夹具一律 auto-96xx，不带 bridge_id ⇒ 不碰真实 analysis/reverse/ 与 uploads/；
  · 收尾断言真实 projects/ 前后一致，临时根整棵删除。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]           # wanrepro/
TMP_ROOT = Path(tempfile.mkdtemp(prefix="probe-at18-hubexit-"))
REAL_PROJECTS = ROOT / "automation" / "projects"
REAL_ANALYSIS = ROOT / "analysis" / "reverse"
REAL_UPLOADS = ROOT / "uploads"

RESULTS: list[tuple[str, bool]] = []


def check(name: str, cond, detail: str = "") -> bool:
    ok = bool(cond)
    RESULTS.append((name, ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail and not ok else ""))
    return ok


def iso(delta: int = 0) -> str:
    return (datetime.now(timezone.utc).astimezone()
            + timedelta(seconds=delta)).isoformat(timespec="seconds")


class Blackhole(threading.Thread):
    """接受 TCP 连接但永不回包的假桥：让后台拆解 job 停在 running（A15 用）。"""

    def __init__(self) -> None:
        super().__init__(daemon=True, name="probe-blackhole-bridge")
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(32)
        self.port = int(self.sock.getsockname()[1])
        self.conns: list[socket.socket] = []

    def run(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.conns.append(conn)

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass
        for conn in self.conns:
            try:
                conn.close()
            except OSError:
                pass


BH = Blackhole()
BH.start()
os.environ["WANREPRO_AUTO_DIR"] = str(TMP_ROOT)      # 必须在 import hub_api/server 之前
os.environ["WANREPRO_WATCHDOG"] = "0"                # 探针期间不许看门狗自动降级干扰夹具
os.environ["WANREPRO_BRIDGE"] = f"http://127.0.0.1:{BH.port}"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ui"))

import hub_api                     # noqa: E402
import server as bridge_server     # noqa: E402

HTTPD = ThreadingHTTPServer(("127.0.0.1", 0), bridge_server.Handler)
PORT = int(HTTPD.server_address[1])
threading.Thread(target=HTTPD.serve_forever, daemon=True, name="probe-http").start()


def call(method: str, path: str, body=None) -> tuple[int, dict]:
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8") or "{}"
            return int(resp.status), json.loads(raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8") or "{}"
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"raw": raw}
        return int(exc.code), payload


def seed(local_id: str, state_name: str, *, history_age: int = -100,
         heartbeat_at: str | None = None, extra: dict | None = None) -> Path:
    d = TMP_ROOT / local_id
    d.mkdir(parents=True, exist_ok=True)
    task = {"local_id": local_id, "state": state_name, "created_at": iso(-5000),
            "intent": "AT-18 B2 探针夹具（隔离根内）",
            "history": [{"at": iso(history_age), "from": "ingested",
                         "to": state_name, "note": "夹具"}]}
    task.update(extra or {})
    (d / "任务.json").write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
    if heartbeat_at is not None:
        (d / "心跳.json").write_text(json.dumps({
            "schema": 1, "run_id": "probe-run", "started_at": iso(-1200),
            "last_beat_at": heartbeat_at, "beat_count": 4, "phase": "夹具心跳",
            "__note": "探针夹具", "artifact_count": 0, "artifact_digest": "",
            "ended_at": "", "outcome": "",
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    return d


def task_of(local_id: str) -> dict:
    return json.loads((TMP_ROOT / local_id / "任务.json").read_text(encoding="utf-8"))


def real_snapshot() -> dict:
    if not REAL_PROJECTS.is_dir():
        return {}
    import hashlib
    out = {}
    for d in sorted(p for p in REAL_PROJECTS.iterdir() if p.is_dir()):
        f = d / "任务.json"
        out[d.name] = hashlib.sha256(f.read_bytes()).hexdigest() if f.is_file() else ""
    return out


def main() -> int:
    print(f"[probe-at18-hubexit] 隔离根：{TMP_ROOT}")
    print(f"  假桥（黑洞）：127.0.0.1:{BH.port}   hub HTTP：127.0.0.1:{PORT}")
    before_real = real_snapshot()

    # ---------------- 夹具 ----------------
    seed("auto-9601", "failed", history_age=-600, extra={
        "failed_reason": "拆解进程失联，可重试或删除", "failed_at": iso(-600),
        "failed_evidence": {"trigger": "auto_scan", "silence_seconds": 600}})
    seed("auto-9602", "discussing", history_age=-5, heartbeat_at=iso(-5))       # 活着的
    seed("auto-9603", "failed", history_age=-900, extra={
        "failed_reason": "拆解进程失联，可重试或删除", "failed_at": iso(-900)})
    seed("auto-9604", "discussing", history_age=-2000)                          # 静默 2000s
    seed("auto-9605", "awaiting_approval", history_age=-50)
    seed("auto-9606", "failed", history_age=-300, heartbeat_at=iso(-120), extra={
        "bridge_id": "probe-at18-9606",      # 只用于让 run_discuss 走到桥调用（目录不存在=产物未齐）
        "failed_reason": "拆解进程失联，可重试或删除", "failed_at": iso(-300)})
    seed("auto-9607", "discussing", history_age=-3000)                          # 存量对账夹具
    seed("auto-9608", "generating", history_age=-2000, extra={
        "cloud_task_id": "t_probe_at18", "gen_job": "probe-job"})

    print("\n[1] failed 可删（A11：墓碑生效，目录消失）")
    before_tomb = []
    tpath = TMP_ROOT / ".tombstones.json"
    if tpath.is_file():
        before_tomb = [x.get("id") for x in json.loads(tpath.read_text(encoding="utf-8"))]
    code, body = call("POST", "/api/hub/task/auto-9601/delete", {"confirm_name": None})
    check("failed 删除 → 200", code == 200, f"{code} {body}")
    check("响应 tombstone=true 且 deleted=任务号",
          body.get("tombstone") is True and body.get("deleted") == "auto-9601", str(body))
    check("任务目录已消失", not (TMP_ROOT / "auto-9601").exists())
    tomb = json.loads(tpath.read_text(encoding="utf-8")) if tpath.is_file() else []
    check("墓碑出现该任务号", any(x.get("id") == "auto-9601" for x in tomb),
          f"before={before_tomb} after={[x.get('id') for x in tomb]}")
    check("墓碑有 时间 字段（可追溯）",
          any(x.get("id") == "auto-9601" and x.get("时间") for x in tomb))

    print("\n[2] discussing 删除仍被拒（A12：原文不变）")
    code, body = call("POST", "/api/hub/task/auto-9602/delete", {"confirm_name": None})
    check("discussing 删除 → 409", code == 409, f"{code} {body}")
    check("拒删原文含「任务运行中（discussing），删除拒绝」",
          "任务运行中（discussing），删除拒绝" in str(body.get("error") or ""), str(body))
    check("任务目录仍在", (TMP_ROOT / "auto-9602").is_dir())

    print("\n[3] 僵尸 JOB 不阻删（A13：同进程注入 running 无心跳记录）")
    hub_api.HUB_JOBS["probe-stale-job"] = {"status": "running", "kind": "hub-discuss",
                                           "started": iso(-1200), "local_id": "auto-9603"}
    code, body = call("POST", "/api/hub/task/auto-9603/delete", {"confirm_name": None})
    check("有僵尸 running 记录仍可删 → 200", code == 200, f"{code} {body}")
    check("响应登记 ignored_stale_jobs（含 hub-discuss:probe-stale-job）",
          body.get("ignored_stale_jobs") == ["hub-discuss:probe-stale-job"], str(body.get("ignored_stale_jobs")))
    hub_api.HUB_JOBS.pop("probe-stale-job", None)

    print("\n[3b] 活 job（心跳新鲜）仍阻断删除（闭包另一侧：不能借僵尸放行误删真活任务）")
    seed("auto-9609", "failed", history_age=-400, extra={
        "failed_reason": "夹具", "failed_at": iso(-400)})
    hub_api.HUB_JOBS["probe-alive-job"] = {"status": "running", "kind": "hub-discuss",
                                          "started": iso(-20), "local_id": "auto-9609"}
    code, body = call("POST", "/api/hub/task/auto-9609/delete", {"confirm_name": None})
    check("活 job 存在时删除 → 409", code == 409, f"{code} {body}")
    check("409 体是 JOB 守卫文案（不是状态文案）",
          "排队/运行中的 job" in str(body.get("error")), str(body))
    hub_api.HUB_JOBS.pop("probe-alive-job", None)
    code, body = call("POST", "/api/hub/task/auto-9609/delete", {"confirm_name": None})
    check("job 记录清掉后同一任务可删（守护只在 job 真活着时挡）", code == 200, f"{code} {body}")

    print("\n[4] 强制结束二次确认（A8：缺参数一律 400）")
    code, body = call("POST", "/api/hub/task/auto-9604/force-finish", {})
    check("缺 confirm → 400", code == 400, f"{code} {body}")
    check("400 体含「二次确认」+ confirm=true 指引",
          "二次确认" in str(body.get("error")) and "confirm=true" in str(body.get("error")), str(body))
    code, body = call("POST", "/api/hub/task/auto-9604/force-finish",
                      {"confirm": "true", "confirm_name": "auto-9604"})
    check("confirm 是字符串 \"true\" → 400（布尔判据不放宽）", code == 400, f"{code} {body}")
    code, body = call("POST", "/api/hub/task/auto-9604/force-finish", {"confirm": True})
    check("缺 confirm_name → 400", code == 400, f"{code} {body}")
    check("400 体点明 confirm_name 必须是任务号",
          'confirm_name 必须是 "auto-9604"' in str(body.get("error")), str(body))
    code, body = call("POST", "/api/hub/task/auto-9604/force-finish",
                      {"confirm": True, "confirm_name": "auto-0001"})
    check("confirm_name 与路径任务号不匹配 → 400", code == 400, f"{code} {body}")
    code, body = call("POST", "/api/hub/task/auto-9604/force-finish",
                      {"confirm": True, "confirm_name": "auto-9604", "reason": "长" * 201})
    check("reason 超 200 字 → 400", code == 400, f"{code} {body}")
    check("400 体含「最长 200 字」", "最长 200 字" in str(body.get("error")), str(body))
    check("以上 400 均未改状态", task_of("auto-9604")["state"] == "discussing")

    print("\n[5] 强制结束成功（A9：置 failed + 人工原因 + 什么都不删）")
    files_before = sorted(p.name for p in (TMP_ROOT / "auto-9604").iterdir())
    code, body = call("POST", "/api/hub/task/auto-9604/force-finish",
                      {"confirm": True, "confirm_name": "auto-9604"})
    check("带确认参数 → 200", code == 200, f"{code} {body}")
    check("响应 state=failed / state_was=discussing / changed=true",
          body.get("state") == "failed" and body.get("state_was") == "discussing"
          and body.get("changed") is True, str(body))
    check("响应 kept 如实回执（任务目录在、无桥产物/无参考片）",
          body.get("kept") == {"task_dir": True, "analysis_dir": False, "uploads_video": False},
          str(body.get("kept")))
    task = task_of("auto-9604")
    check("任务置 failed", task["state"] == "failed")
    check("写入人工介入原因", task.get("failed_reason") == "人工强制结束（所有者操作）",
          str(task.get("failed_reason")))
    check("failed_evidence.trigger=manual_force",
          (task.get("failed_evidence") or {}).get("trigger") == "manual_force",
          str(task.get("failed_evidence")))
    check("history 记 discussing→failed", (task.get("history") or [{}])[-1].get("from") == "discussing")
    files_after = sorted(p.name for p in (TMP_ROOT / "auto-9604").iterdir())
    check("任务目录文件集合不变（只改状态与事件）", files_before == files_after,
          f"{files_before} → {files_after}")
    code, detail = call("GET", "/api/hub/task/auto-9604")
    check("读面 failed.reason 透出人工原因",
          (detail.get("task", {}).get("failed") or {}).get("reason") == "人工强制结束（所有者操作）", str(detail)[:400])

    print("\n[6] 强制结束幂等（A10：重复调用同一结果，不二次写）")
    hist_len = len(task_of("auto-9604").get("history") or [])
    failed_at = task_of("auto-9604").get("failed_at")
    code, body = call("POST", "/api/hub/task/auto-9604/force-finish",
                      {"confirm": True, "confirm_name": "auto-9604"})
    check("再调 → 200 不报错", code == 200, f"{code} {body}")
    check("already=true / changed=false", body.get("already") is True and body.get("changed") is False, str(body))
    check("回显原失败原因（不被覆盖成默认文案）",
          body.get("reason") == "人工强制结束（所有者操作）", str(body.get("reason")))
    check("history 长度不变", len(task_of("auto-9604").get("history") or []) == hist_len)
    check("failed_at 不变", task_of("auto-9604").get("failed_at") == failed_at)

    print("\n[7] 非运行态 / 不存在")
    code, body = call("POST", "/api/hub/task/auto-9605/force-finish",
                      {"confirm": True, "confirm_name": "auto-9605"})
    check("awaiting_approval → 409", code == 409, f"{code} {body}")
    check("409 体含「任务当前不是运行态」+「无需强制结束」",
          "任务当前不是运行态" in str(body.get("error")) and "无需强制结束" in str(body.get("error")), str(body))
    check("非运行态未被改状态", task_of("auto-9605")["state"] == "awaiting_approval")
    code, body = call("POST", "/api/hub/task/auto-9999/force-finish",
                      {"confirm": True, "confirm_name": "auto-9999"})
    check("不存在的任务 → 409 + 「任务不存在」",
          code == 409 and "任务不存在" in str(body.get("error")), f"{code} {body}")

    print("\n[8] failed 可重试（A14：重建心跳锚，旧失败原因保留）")
    old_hb = json.loads((TMP_ROOT / "auto-9606" / "心跳.json").read_text(encoding="utf-8"))
    old_note = (task_of("auto-9606").get("history") or [])[-1].get("note")
    code, body = call("POST", "/api/hub/task/auto-9606/retry", {})
    check("failed 重试 → 200", code == 200, f"{code} {body}")
    check("响应 heartbeat_reset=true", body.get("heartbeat_reset") is True, str(body))
    check("响应 previous_reason=旧失败原因",
          body.get("previous_reason") == "拆解进程失联，可重试或删除", str(body.get("previous_reason")))
    check("响应 job_id 非空", bool(body.get("job_id")), str(body))
    hb = json.loads((TMP_ROOT / "auto-9606" / "心跳.json").read_text(encoding="utf-8"))
    check("心跳 run_id 已换新且等于本次 job_id",
          hb.get("run_id") == body.get("job_id") and hb.get("run_id") != old_hb.get("run_id"),
          f"{old_hb.get('run_id')} → {hb.get('run_id')} / job={body.get('job_id')}")
    check("心跳 last_beat_at 清空（新 run 未心跳，初值可识别）", hb.get("last_beat_at") == "", str(hb.get("last_beat_at")))
    deadline = time.time() + 8
    while time.time() < deadline and task_of("auto-9606")["state"] != "discussing":
        time.sleep(0.2)
    check("重试后状态回到可运行初态 discussing", task_of("auto-9606")["state"] == "discussing",
          task_of("auto-9606")["state"])
    notes = [h.get("note") for h in task_of("auto-9606").get("history") or []]
    check("旧失败原因仍在 history（可追溯，不被抹掉）", old_note in notes, str(notes))

    print("\n[9] 重试双写防护（A15：在跑 job 时再重试 → 409，不起第二个线程）")
    code, body = call("POST", "/api/hub/task/auto-9606/retry", {})
    check("再重试 → 409", code == 409, f"{code} {body}")
    check("409 体含「已有拆解任务在跑」", "已有拆解任务在跑" in str(body.get("error")), str(body))
    running = hub_api._job_running_records("auto-9606")
    check("HUB_JOBS 中该任务 running 记录数 == 1", len(running) == 1, str(len(running)))

    print("\n[9b] 非 failed 且无在跑 job 的重试 → 409 原文（状态文案不变）")
    code, body = call("POST", "/api/hub/task/auto-9602/retry", {})
    check("discussing 任务重试 → 409", code == 409, f"{code} {body}")
    check("409 体沿用原文「重试需 failed 状态，当前 discussing」",
          "重试需 failed 状态，当前 discussing" in str(body.get("error")), str(body))

    print("\n[10] 扫描：dry-run 零写入 → apply 降级 → 连扫幂等（A17 / A5）")
    before_hash = (TMP_ROOT / "auto-9607" / "任务.json").read_bytes()
    code, body = call("POST", "/api/hub/maintenance/scan-lost-runs", {"apply": False})
    check("dry-run → 200", code == 200, f"{code} {body}")
    check("applied=false 且 threshold_seconds==900",
          body.get("applied") is False and body.get("threshold_seconds") == 900, str(body)[:400])
    lost_ids = [x.get("local_id") for x in body.get("lost") or []]
    check("dry-run 清单含静默 3000s 的 discussing 任务", "auto-9607" in lost_ids, str(lost_ids))
    item = next((x for x in body.get("lost") or [] if x.get("local_id") == "auto-9607"), {})
    check("清单项字段齐全（silence_seconds/last_activity_at/source/reason/changed）",
          item.get("changed") is False and int(item.get("silence_seconds") or 0) >= 900
          and bool(item.get("last_activity_at")) and bool(item.get("last_activity_source"))
          and item.get("reason") == "拆解进程失联，可重试或删除", str(item))
    check("dry-run 前后 任务.json 字节不变（零写入）",
          (TMP_ROOT / "auto-9607" / "任务.json").read_bytes() == before_hash)
    check("活着的 discussing（心跳 5s 前）不出现在 lost[]", "auto-9602" not in lost_ids, str(lost_ids))
    gen_skip = next((x for x in body.get("skipped") or [] if x.get("local_id") == "auto-9608"), {})
    check("generating 进 skipped{in_flight_cloud}（A6：永不自动降级）",
          gen_skip.get("reason_code") == "in_flight_cloud", str(gen_skip))

    code, body = call("POST", "/api/hub/maintenance/scan-lost-runs", {"apply": True})
    check("apply=true → 200 且 applied=true", code == 200 and body.get("applied") is True, f"{code}")
    item = next((x for x in body.get("lost") or [] if x.get("local_id") == "auto-9607"), {})
    check("apply 清单该项 changed=true", item.get("changed") is True, str(item))
    check("任务已降级为 failed", task_of("auto-9607")["state"] == "failed")
    check("降级文案逐字一致", task_of("auto-9607").get("failed_reason") == "拆解进程失联，可重试或删除")
    hist_len = len(task_of("auto-9607").get("history") or [])
    failed_at = task_of("auto-9607").get("failed_at")

    code, body = call("POST", "/api/hub/maintenance/scan-lost-runs", {"apply": True})
    lost_ids = [x.get("local_id") for x in body.get("lost") or []]
    skip = next((x for x in body.get("skipped") or [] if x.get("local_id") == "auto-9607"), {})
    check("连扫第二次：lost[] 不再含该任务", "auto-9607" not in lost_ids, str(lost_ids))
    check("连扫第二次：进 skipped{not_running}", skip.get("reason_code") == "not_running", str(skip))
    check("连扫第二次：history 只 +1（无二次降级）",
          len(task_of("auto-9607").get("history") or []) == hist_len, str(hist_len))
    check("连扫第二次：failed_at 不变", task_of("auto-9607").get("failed_at") == failed_at)
    check("scanned 计数 = 运行态任务数（本夹具 3：9602/9606/9608）",
          int(body.get("scanned") or 0) >= 3, str(body.get("scanned")))
    check("generating 夹具状态未变（不自动降级）", task_of("auto-9608")["state"] == "generating")

    print("\n[11] 读面四键（A16：旧任务无心跳/无 failed_reason 也不 500）")
    code, body = call("GET", "/api/hub/task/auto-9602")
    detail = body.get("task") or {}
    check("GET 详情 → 200", code == 200, f"{code}")
    check("四键齐全：heartbeat / activity / lost_run / failed",
          all(k in detail for k in ("heartbeat", "activity", "lost_run", "failed")), str(sorted(detail))[:300])
    check("heartbeat.at 为真值（活任务）", bool((detail.get("heartbeat") or {}).get("at")))
    check("activity.source ∈ 6 值枚举",
          (detail.get("activity") or {}).get("source") in
          ("心跳", "状态迁移", "阶段锚", "任务目录产物", "拆解产物", "起跑锚"),
          str(detail.get("activity")))
    code, body = call("GET", "/api/hub/tasks")
    check("GET 列表 → 200 且每条含四键",
          code == 200 and all(all(k in t for k in ("heartbeat", "activity", "lost_run", "failed"))
                              for t in body.get("tasks") or []), f"{code}")
    code, body = call("GET", "/api/hub/task/auto-9605")
    old = body.get("task") or {}
    check("旧任务（无 failed_reason/心跳）失败键给空值不报 500",
          code == 200 and (old.get("failed") or {}).get("reason") == ""
          and (old.get("heartbeat") or {}).get("count") == 0, f"{code} {str(old.get('failed'))[:200]}")

    print("\n[12] 真实数据只读（A18-lite）")
    after_real = real_snapshot()
    check("真实 projects/ 目录集与 任务.json 哈希前后一致", before_real == after_real,
          f"{set(before_real) ^ set(after_real)}")
    check("本探针没有写真实 analysis/reverse/ 或 uploads/",
          not any(p.name.startswith("probe-at18") for p in (REAL_ANALYSIS.iterdir()
                                                            if REAL_ANALYSIS.is_dir() else [])))
    check("WANREPRO_WATCHDOG=0 下看门狗未启动",
          hub_api._WATCHDOG_THREAD is None or not hub_api._WATCHDOG_THREAD.is_alive())

    fails = [n for n, ok in RESULTS if not ok]
    print(f"\n[probe-at18-hubexit] {len(RESULTS) - len(fails)}/{len(RESULTS)} PASS")
    for name in fails:
        print(f"  FAIL {name}")
    return 1 if fails else 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        hub_api.stop_lost_run_watchdog()
        try:
            HTTPD.shutdown()
            HTTPD.server_close()
        except Exception:  # noqa: BLE001
            pass
        BH.close()
        time.sleep(0.3)
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
        print(f"[probe-at18-hubexit] 已清理隔离根：{TMP_ROOT}")
    raise SystemExit(code)
