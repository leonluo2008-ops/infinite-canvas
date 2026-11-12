#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AT-18 C1 探针（**端到端**）：存量对账清理 + auto-0052 同构场景的四条出路回归。

跑法：wanrepro/ 下 `python automation/probe_at18_reconcile.py`（隔离，自清理，退出码 0=全绿）。

锁的是「存量卡死任务能不能被清掉」这件事本身，不是函数返回值。全程真实进程 + 真实 HTTP + 真实
子进程（对账脚本以子进程方式跑，证明交付件本身可用）：

  [1] 对账脚本 dry-run：中文报告四段齐全（扫描数 / 判定假运行数 / 实际降级数 / 跳过原因），
      且**零写入**（任务.json 字节不变）；活任务不误判
  [2] 对账脚本 --apply：失联任务降为 failed（文案逐字）、连跑第二次 lost 为空 / skipped{not_running}
      / history 只 +1 / failed_at 不变（幂等）
  [3] auto-0052 同构夹具（只有 inbox + 任务.json，无进程、零产物）：**看门狗自动扫描**把它降为 failed
      （没有人手动触发；failed_evidence.trigger=auto_scan），任务目录与 inbox 一个文件不少
  [4] 此前被拒的删除在 failed 后可成功（墓碑生效、目录消失）；同轮再证 discussing 仍拒删（原文不变）
  [5] 重试通道把 failed 任务重新跑起来（心跳锚整体重建、旧失败原因留 history）；
      在跑 job 时再重试 → 409（不起第二个线程）
  [6] 强制结束作为人工兜底：generating（自动扫描永不降级）与「看着还活着」的 discussing 都能
      人工置 failed，且什么都不删；缺二次确认 → 400；幂等 200

隔离手法（沿 probe_at18_hubexit.py 先例）：
  · WANREPRO_AUTO_DIR → 本次临时根（任务根唯一来源 automation.config.PROJECTS_DIR）；
  · WANREPRO_BRIDGE → 本探针自起的「黑洞」TCP 端口（接受连接、永不回包）——重试拉起的拆解 job
    停在 running，「在跑 job 再重试 409」才有可判窗口；
  · 夹具一律 auto-97xx、桥 id 一律 probe-at18-*：不碰真实 projects/ 内容；
    analysis/reverse/ 与 uploads/ 只做「跑前集合 ⊆ 跑前 ∪ 本探针新建」的有界核对 + 有界清理；
  · 看门狗**故意开着**（这条探针要证明的就是自动扫描），跑完 stop。

关于「等待超过冻结阈值」：阈值 900s 是冻结值，探针不改它、也不真等 15 分钟——夹具的持久时间戳
（history.at / 心跳时刻 / 文件 mtime）直接做旧到 now-1200s。判定是「时间戳证据的函数」而不是
「两次观测的差分」（方案 §2.2），所以做旧时间戳与真等 900s 在判定上等价，且可重跑、无睡眠。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
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
TMP_ROOT = Path(tempfile.mkdtemp(prefix="probe-at18-reconcile-"))
REAL_PROJECTS = ROOT / "automation" / "projects"
REAL_ANALYSIS = ROOT / "analysis" / "reverse"
REAL_UPLOADS = ROOT / "uploads"
CLI = ROOT / "automation" / "reconcile_lost_runs.py"

DISCUSS_REASON = "拆解进程失联，可重试或删除"

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
    """接受 TCP 连接但永不回包的假桥：让重试拉起的拆解 job 停在 running。"""

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
os.environ["WANREPRO_BRIDGE"] = f"http://127.0.0.1:{BH.port}"
os.environ.pop("WANREPRO_WATCHDOG", None)             # 看门狗开着（本探针的验证对象）
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
         heartbeat_at: str | None = None, inbox: bool = False,
         extra: dict | None = None) -> Path:
    """建一个任务目录。inbox=True 但 heartbeat_at=None 就是 auto-0052 的形态：
    只有 inbox/ + 任务.json，没有心跳、没有产物、没有进程。"""
    d = TMP_ROOT / local_id
    d.mkdir(parents=True, exist_ok=True)
    task = {"local_id": local_id, "state": state_name, "created_at": iso(-5000),
            "intent": "AT-18 C1 探针夹具（隔离根内）",
            "history": [{"at": iso(history_age), "from": "ingested",
                         "to": state_name, "note": "夹具"}]}
    task.update(extra or {})
    (d / "任务.json").write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
    if heartbeat_at is not None:
        (d / "心跳.json").write_text(json.dumps({
            "schema": 1, "run_id": "probe-old-run", "started_at": iso(-1200),
            "last_beat_at": heartbeat_at, "beat_count": 4, "phase": "夹具心跳",
            "artifact_count": 0, "artifact_digest": "", "ended_at": "", "outcome": "",
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    if inbox:
        (d / "inbox").mkdir(exist_ok=True)
        video = d / "inbox" / "probe.mp4"
        video.write_bytes(b"probe-fixture-0" * 64)
        # 参考视频是「创建那一刻放进去的」，之后两小时没有任何产物：mtime 跟着 history 做旧，
        # 否则文件刚写下去就等于「刚刚有活动」，auto-0052 那种「上传完就再没动过」的形态造不出来。
        stamp = time.time() + history_age
        os.utime(video, (stamp, stamp))
        os.utime(d / "inbox", (stamp, stamp))
    return d


def task_of(local_id: str) -> dict:
    return json.loads((TMP_ROOT / local_id / "任务.json").read_text(encoding="utf-8"))


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_cli(*args: str) -> tuple[int, str]:
    """以子进程跑交付件本身（证明运维手里的入口可用，不是探针里偷偷调函数）。"""
    env = dict(os.environ)
    env["WANREPRO_AUTO_DIR"] = str(TMP_ROOT)
    proc = subprocess.run([sys.executable, str(CLI), *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", env=env, timeout=120)
    return int(proc.returncode), (proc.stdout or "") + (proc.stderr or "")


def run_cli_json(*args: str) -> tuple[int, dict, str]:
    code, out = run_cli(*args, "--json")
    try:
        return code, json.loads(out[out.index("{"):]), out
    except (ValueError, json.JSONDecodeError):
        return code, {}, out


def snapshot_dir(base: Path) -> set[str]:
    return {p.name for p in base.iterdir()} if base.is_dir() else set()


def wait_state(local_id: str, want: str, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if task_of(local_id).get("state") == want:
                return True
        except (OSError, json.JSONDecodeError):
            pass
        time.sleep(0.5)
    return False


def main() -> int:
    print(f"[probe-at18-reconcile] 隔离根：{TMP_ROOT}")
    print(f"  假桥（黑洞）：127.0.0.1:{BH.port}   hub HTTP：127.0.0.1:{PORT}")
    check("阈值是冻结值 900s（A0）", hub_api.state.FAKE_RUN_SILENCE_SECONDS == 900,
          str(hub_api.state.FAKE_RUN_SILENCE_SECONDS))
    check("降级文案是冻结文案", hub_api.state.FAKE_RUN_REASONS.get("discussing") == DISCUSS_REASON,
          str(hub_api.state.FAKE_RUN_REASONS))

    real_projects_before = {d.name: (sha(d / "任务.json") if (d / "任务.json").is_file() else "")
                            for d in REAL_PROJECTS.iterdir() if d.is_dir()}
    real_analysis_before = snapshot_dir(REAL_ANALYSIS)
    real_uploads_before = snapshot_dir(REAL_UPLOADS)

    # ---------------- [1] 存量对账 dry-run（交付件子进程） ----------------
    print("\n[1] 对账脚本 dry-run：报告四段齐全 + 零写入 + 活任务不误判")
    d_lost = seed("auto-9701", "discussing", history_age=-1200,
                  heartbeat_at=iso(-1200), inbox=True)          # 心跳也停了的失联任务
    seed("auto-9702", "discussing", history_age=-5, heartbeat_at=iso(-5))   # 活着的
    before_hash = sha(d_lost / "任务.json")
    code, text = run_cli()
    check("dry-run 退出码 0", code == 0, f"{code}\n{text[-800:]}")
    check("报告含「扫描数（运行态任务）」段", "扫描数（运行态任务）" in text, text[-800:])
    check("报告含「判定假运行数」段", "判定假运行数" in text, text[-800:])
    check("报告含「实际降级数」段", "实际降级数" in text, text[-800:])
    check("报告含「跳过原因」段", "跳过原因" in text, text[-800:])
    check("dry-run 报告点名失联任务与逐字原因",
          "auto-9701" in text and DISCUSS_REASON in text, text[-800:])
    check("dry-run 零写入（任务.json 字节不变）", sha(d_lost / "任务.json") == before_hash)
    check("dry-run 后任务仍是 discussing", task_of("auto-9701")["state"] == "discussing")

    code, js, raw = run_cli_json()
    check("dry-run --json 可解析且 applied=false", code == 0 and js.get("applied") is False,
          raw[-500:])
    lost_ids = [x.get("local_id") for x in js.get("lost") or []]
    skip_ids = {x.get("local_id"): x.get("reason_code") for x in js.get("skipped") or []}
    check("dry-run 清单只含 auto-9701", lost_ids == ["auto-9701"], str(lost_ids))
    check("活任务 auto-9702 进 skipped{silence_below_threshold}（不误杀）",
          skip_ids.get("auto-9702") == "silence_below_threshold", str(skip_ids))
    check("扫描数是运行态任务数（2）", int(js.get("scanned") or 0) == 2, str(js.get("scanned")))

    # ---------------- [2] --apply 真降级 + 幂等 ----------------
    print("\n[2] 对账脚本 --apply：降级 → 连跑第二次不二次降级")
    code, js, raw = run_cli_json("--apply")
    item = next((x for x in js.get("lost") or [] if x.get("local_id") == "auto-9701"), {})
    check("apply 退出码 0 且 applied=true", code == 0 and js.get("applied") is True, raw[-500:])
    check("清单项 changed=true", item.get("changed") is True, str(item))
    task = task_of("auto-9701")
    check("任务已降级 failed", task["state"] == "failed", task["state"])
    check("失败原因逐字等于冻结文案", task.get("failed_reason") == DISCUSS_REASON,
          str(task.get("failed_reason")))
    check("history 末条 discussing→failed", (task.get("history") or [{}])[-1].get("from") == "discussing")
    check("failed_evidence.trigger=auto_scan",
          (task.get("failed_evidence") or {}).get("trigger") == "auto_scan",
          str(task.get("failed_evidence")))
    check("任务目录与 inbox 一个文件不少（只改状态与事件）",
          (d_lost / "inbox" / "probe.mp4").is_file() and (d_lost / "任务.json").is_file())
    hist_len, failed_at = len(task.get("history") or []), task.get("failed_at")

    code, js2, raw = run_cli_json("--apply")
    lost_ids = [x.get("local_id") for x in js2.get("lost") or []]
    skip2 = {x.get("local_id"): x.get("reason_code") for x in js2.get("skipped") or []}
    check("连跑第二次 lost[] 不再含 auto-9701（幂等）", "auto-9701" not in lost_ids, str(lost_ids))
    check("连跑第二次进 skipped{not_running}", skip2.get("auto-9701") == "not_running", str(skip2))
    check("连跑第二次 history 只 +1（无二次降级）",
          len(task_of("auto-9701").get("history") or []) == hist_len, str(hist_len))
    check("连跑第二次 failed_at 不变", task_of("auto-9701").get("failed_at") == failed_at)

    # ---------------- [3] auto-0052 同构：看门狗自动扫描 ----------------
    print("\n[3] auto-0052 同构夹具：只有 inbox + 任务.json，看门狗自动降级（无人手动触发）")
    a_dir = seed("auto-9703", "discussing", history_age=-1200, inbox=True)   # 无心跳、零产物
    check("夹具形态与 auto-0052 一致（无心跳文件）", not (a_dir / "心跳.json").exists())
    check("起手状态 discussing", task_of("auto-9703")["state"] == "discussing")
    started = hub_api.start_lost_run_watchdog()
    check("看门狗已启动（本探针不手动扫描 auto-9703）", started is True, str(started))
    t0 = time.time()
    degraded = wait_state("auto-9703", "failed", timeout=90)
    took = time.time() - t0
    check("看门狗在 90s 内把 auto-9703 降为 failed", degraded,
          f"等了 {took:.0f}s，仍是 {task_of('auto-9703').get('state')}")
    task_a = task_of("auto-9703")
    check("自动降级文案逐字一致", task_a.get("failed_reason") == DISCUSS_REASON,
          str(task_a.get("failed_reason")))
    check("failed_evidence.trigger=auto_scan（不是 manual_force）",
          (task_a.get("failed_evidence") or {}).get("trigger") == "auto_scan",
          str(task_a.get("failed_evidence")))
    check("自动降级发生在首跑延迟之后（不是伪造的瞬时写）", took >= 25, f"{took:.0f}s")
    check("降级后任务目录与 inbox 仍在（不删任何东西）",
          (a_dir / "任务.json").is_file() and (a_dir / "inbox" / "probe.mp4").is_file())

    # ---------------- [4] 此前被拒的删除 → failed 后可删（墓碑） ----------------
    print("\n[4] 删除：discussing 仍拒删 → failed 后可删（墓碑生效）")
    code, body = call("POST", "/api/hub/task/auto-9702/delete", {"confirm_name": None})
    check("活着的 discussing 删除 → 409（守卫未被放宽）", code == 409, f"{code} {body}")
    check("拒删原文含「任务运行中（discussing），删除拒绝」",
          "任务运行中（discussing），删除拒绝" in str(body.get("error") or ""), str(body))
    check("discussing 任务目录仍在", (TMP_ROOT / "auto-9702").is_dir())

    code, body = call("POST", "/api/hub/task/auto-9703/delete", {"confirm_name": None})
    check("自动降级后的 failed 删除 → 200", code == 200, f"{code} {body}")
    check("响应 tombstone=true / state_was=failed",
          body.get("tombstone") is True and body.get("state_was") == "failed", str(body))
    check("任务目录已消失（黑洞被清掉）", not (TMP_ROOT / "auto-9703").exists())
    tomb = (json.loads((TMP_ROOT / ".tombstones.json").read_text(encoding="utf-8"))
            if (TMP_ROOT / ".tombstones.json").is_file() else [])
    check("墓碑出现 auto-9703（可追溯）", any(x.get("id") == "auto-9703" for x in tomb),
          str([x.get("id") for x in tomb]))

    # ---------------- [5] 重试通道把它重新跑起来 ----------------
    print("\n[5] 重试：failed → discussing（心跳锚重建、旧原因留痕），在跑 job 再重试 → 409")
    seed("auto-9704", "failed", history_age=-400, heartbeat_at=iso(-400), extra={
        "bridge_id": "probe-at18-9704",       # 只用于让 run_discuss 走到桥调用（产物目录不存在）
        "failed_reason": DISCUSS_REASON, "failed_at": iso(-400),
        "failed_evidence": {"trigger": "auto_scan", "silence_seconds": 400}})
    old_hb = json.loads((TMP_ROOT / "auto-9704" / "心跳.json").read_text(encoding="utf-8"))
    old_notes = [h.get("note") for h in task_of("auto-9704").get("history") or []]
    code, body = call("POST", "/api/hub/task/auto-9704/retry", {})
    check("failed 重试 → 200", code == 200, f"{code} {body}")
    check("heartbeat_reset=true", body.get("heartbeat_reset") is True, str(body))
    check("previous_reason 等于原失败原因", body.get("previous_reason") == DISCUSS_REASON,
          str(body.get("previous_reason")))
    hb = json.loads((TMP_ROOT / "auto-9704" / "心跳.json").read_text(encoding="utf-8"))
    check("心跳 run_id 已整体重建（≠ 旧 run_id）",
          hb.get("run_id") == body.get("job_id") and hb.get("run_id") != old_hb.get("run_id"),
          f"{old_hb.get('run_id')} → {hb.get('run_id')}")
    check("重试后状态回到 discussing", wait_state("auto-9704", "discussing", 10),
          task_of("auto-9704").get("state"))
    check("旧失败原因仍在 history（可追溯）",
          all(n in [h.get("note") for h in task_of("auto-9704").get("history") or []] for n in old_notes))
    code, body = call("POST", "/api/hub/task/auto-9704/retry", {})
    check("在跑 job 时再重试 → 409（不起第二个线程）", code == 409, f"{code} {body}")
    check("409 体含「已有拆解任务在跑」", "已有拆解任务在跑" in str(body.get("error") or ""), str(body))
    check("HUB_JOBS 中该任务 running 记录数 == 1",
          len(hub_api._job_running_records("auto-9704")) == 1,
          str(len(hub_api._job_running_records("auto-9704"))))

    # ---------------- [6] 强制结束：人工兜底口 ----------------
    print("\n[6] 强制结束：generating（自动扫描永不降级）与「看着还活着」的 discussing 都能人工兜底")
    seed("auto-9705", "generating", history_age=-2000, extra={
        "cloud_task_id": "t_probe_at18_reconcile", "gen_job": "probe-job"})
    code, js3, raw = run_cli_json()
    skip3 = {x.get("local_id"): x.get("reason_code") for x in js3.get("skipped") or []}
    check("扫描把 generating 进 skipped{in_flight_cloud}（不自动降级）",
          skip3.get("auto-9705") == "in_flight_cloud", str(skip3))
    check("generating 状态未被自动改动", task_of("auto-9705")["state"] == "generating")

    code, body = call("POST", "/api/hub/task/auto-9705/force-finish", {"confirm": True})
    check("强制结束缺 confirm_name → 400", code == 400, f"{code} {body}")
    files_before = sorted(p.name for p in (TMP_ROOT / "auto-9705").iterdir())
    code, body = call("POST", "/api/hub/task/auto-9705/force-finish",
                      {"confirm": True, "confirm_name": "auto-9705",
                       "reason": "人工强制结束（探针：自动通道不覆盖 generating）"})
    check("带二次确认 → 200 / state=failed / state_was=generating",
          code == 200 and body.get("state") == "failed" and body.get("state_was") == "generating",
          f"{code} {body}")
    task_5 = task_of("auto-9705")
    check("failed_evidence.trigger=manual_force",
          (task_5.get("failed_evidence") or {}).get("trigger") == "manual_force",
          str(task_5.get("failed_evidence")))
    check("人工原因写入 failed_reason",
          task_5.get("failed_reason") == "人工强制结束（探针：自动通道不覆盖 generating）",
          str(task_5.get("failed_reason")))
    check("强制结束不删任何东西（文件集合不变）",
          sorted(p.name for p in (TMP_ROOT / "auto-9705").iterdir()) == files_before,
          f"{files_before} → {sorted(p.name for p in (TMP_ROOT / 'auto-9705').iterdir())}")

    seed("auto-9706", "discussing", history_age=-5, heartbeat_at=iso(-5))     # 看着还活着
    before_hash = sha(TMP_ROOT / "auto-9706" / "任务.json")
    code, js4, raw = run_cli_json()
    lost4 = [x.get("local_id") for x in js4.get("lost") or []]
    check("「看着还活着」的任务不在 lost[]（扫描不误杀）", "auto-9706" not in lost4, str(lost4))
    check("扫描没有动它的任务文件", sha(TMP_ROOT / "auto-9706" / "任务.json") == before_hash)
    code, body = call("POST", "/api/hub/task/auto-9706/force-finish",
                      {"confirm": True, "confirm_name": "auto-9706"})
    check("人工判断误判时仍可强制结束 → 200", code == 200 and body.get("state") == "failed",
          f"{code} {body}")
    check("默认人工原因（所有者操作）",
          task_of("auto-9706").get("failed_reason") == "人工强制结束（所有者操作）",
          str(task_of("auto-9706").get("failed_reason")))
    code, body = call("POST", "/api/hub/task/auto-9706/force-finish",
                      {"confirm": True, "confirm_name": "auto-9706"})
    check("强制结束幂等：already=true / changed=false",
          code == 200 and body.get("already") is True and body.get("changed") is False,
          f"{code} {body}")
    code, body = call("POST", "/api/hub/task/auto-9706/retry", {})
    check("兜底不是死路：强制结束后的任务可重试 → 200", code == 200, f"{code} {body}")

    # ---------------- [7] 真实数据只读 + 有界清理 ----------------
    print("\n[7] 真实数据只读（A18）：projects / analysis / uploads 集合不因本探针变化")
    real_projects_after = {d.name: (sha(d / "任务.json") if (d / "任务.json").is_file() else "")
                           for d in REAL_PROJECTS.iterdir() if d.is_dir()}
    check("真实 projects/ 目录集与各 任务.json 哈希前后一致",
          real_projects_after == real_projects_before,
          f"{set(real_projects_after) ^ set(real_projects_before)}")
    new_analysis = snapshot_dir(REAL_ANALYSIS) - real_analysis_before
    stray = sorted(n for n in new_analysis if n.startswith("probe-at18"))
    for name in stray:                     # 有界清理：只删本探针前缀、且父目录校验
        target = REAL_ANALYSIS / name
        if target.parent == REAL_ANALYSIS and not target.is_symlink():
            shutil.rmtree(target, ignore_errors=True)
    check("真实 analysis/reverse/ 无本探针新建目录（新建的已按前缀清理）",
          snapshot_dir(REAL_ANALYSIS) == real_analysis_before, str(stray))
    check("真实 uploads/ 集合不变", snapshot_dir(REAL_UPLOADS) == real_uploads_before)

    fails = [n for n, ok in RESULTS if not ok]
    print(f"\n[probe-at18-reconcile] {len(RESULTS) - len(fails)}/{len(RESULTS)} PASS")
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
        print(f"[probe-at18-reconcile] 已清理隔离根：{TMP_ROOT}")
    raise SystemExit(code)
