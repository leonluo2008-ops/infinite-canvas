#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AT-4.5 探针：/api/sdgf/video/run|job|file 三端点的服务端合同（全部本地，不碰真渠道）。

判据（AT-4.5 验收）：
  ① 鉴权头与两族参数方言构造正确（满血名 vs 别名族的比例/声音生效位）；
  ② confirm 不是布尔 true → 不发任何请求（dry），不占并发位、不花钱；
  ③ 非法 body 被拒（坏 JSON / 空 prompt / images 不是列表 / 预演不通过仍要提交）；
  ④ 渠道凭证不回显（预览只写 Bearer ***，渠道错误体里带 key 也要抹成 ***）；
  ⑤ 真提交路径（confirm=true）走本地假渠道全链：建任务 → 进度透传 → 代下成片 → /file 可取；
  ⑥ 服务重启语义：成片目录按渠道任务 id 命名，/file 不依赖内存 job 记录。

假渠道只监听 127.0.0.1 随机端口，返回的是 SDGF 合同形状的 JSON——不连任何真实渠道。
用法：python ui/probe-sdgf-endpoints.py
"""

from __future__ import annotations

import base64
import importlib.util
import json
import shutil
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PASS = FAIL = 0
KEY = "sk-probe-secret-0001"
VIDEO = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 2048      # 够长、可辨认的假 mp4 字节


def check(ok: bool, name: str, detail: str = "") -> bool:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f" —— {detail}" if detail else ""))
    return ok


def section(title: str) -> None:
    print(f"\n[{title}]")


def load_server():
    path = ROOT / "ui" / "server.py"
    spec = importlib.util.spec_from_file_location("ui_server_sdgf_probe", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class MockChannel:
    """假渠道：只实现 /v1/videos 家族三个端点，记录收到的每一个请求（含鉴权头）。"""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.polls: dict[str, int] = {}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args) -> None:      # 探针不刷访问日志
                pass

            def _send(self, status: int, data: bytes, ctype: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _json(self, obj: dict) -> None:
                self._send(200, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                outer.requests.append({"method": "POST", "path": self.path,
                                       "headers": dict(self.headers), "raw": raw})
                body = json.loads(raw.decode("utf-8") or "{}")
                prompt = str(body.get("prompt") or "")
                task_id = {"ok": "mock-ok-1", "fail": "mock-fail-1",
                           "direct": "mock-direct-1"}.get(prompt, "mock-ok-1")
                self._json({"id": task_id})

            def do_GET(self) -> None:
                outer.requests.append({"method": "GET", "path": self.path,
                                       "headers": dict(self.headers), "raw": b""})
                if self.path.endswith("/content"):
                    self._send(200, VIDEO, "video/mp4")
                    return
                if self.path.startswith("/files/"):
                    self._send(200, VIDEO, "video/mp4")
                    return
                task_id = self.path.rsplit("/", 1)[-1]
                if task_id == "mock-fail-1":
                    auth = self.headers.get("Authorization") or ""
                    self._json({"status": "failed", "progress": 0,
                                "error": {"message": f"站方内部错误，回显凭证 {auth}"}})
                    return
                if task_id == "mock-direct-1":
                    self._json({"status": "completed", "progress": 100,
                                "metadata": {"url": f"http://127.0.0.1:{outer.port}/files/{task_id}.mp4"}})
                    return
                outer.polls[task_id] = outer.polls.get(task_id, 0) + 1
                if outer.polls[task_id] == 1:
                    self._json({"status": "in_progress", "progress": 42})
                else:
                    self._json({"status": "completed", "progress": 100, "metadata": {"url": ""}})

        self._handler = Handler
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def post_requests(self) -> list[dict]:
        return [r for r in self.requests if r["method"] == "POST"]


def wait_job(server, job: str, timeout: float = 10.0) -> tuple[dict, list[dict]]:
    """轮询 job 直到 done/failed；返回（终态, 观察到的所有状态）。"""
    seen: list[dict] = []
    end = time.time() + timeout
    while time.time() < end:
        state = server.sdgf_job_state(job)
        seen.append(state)
        if state["status"] in ("done", "failed"):
            return state, seen
        time.sleep(0.05)
    raise AssertionError(f"job {job} 在 {timeout}s 内没结束：{seen[-1] if seen else '无状态'}")


def run() -> int:
    print("AT-4.5 SDGF 三端点探针（假渠道，本地，不花钱）")
    server = load_server()

    class FakeHandler(server.Handler):
        """把 socket 层掐掉，只测端点自己的判断（不监听端口、不起服务）。"""

        def __init__(self, payload: dict) -> None:
            self._payload = dict(payload)
            self.path = "/api/sdgf/video/run"
            self.command = "POST"
            self.headers = {}
            self.sent: list[tuple[int, dict]] = []

        def read_json(self) -> dict:
            return dict(self._payload)

        def send_json(self, obj, status: int = 200) -> None:
            self.sent.append((status, obj))

    mock = MockChannel()
    tmp = Path(tempfile.mkdtemp(prefix="probe-sdgf-"))
    real_uploads = server.UPLOADS
    real_interval, real_fail_delay = server.SDGF_POLL_INTERVAL, server.SDGF_FAILED_RECHECK_DELAY
    server.UPLOADS = tmp
    server.SDGF_POLL_INTERVAL = 0.05                     # 后台轮询别真等 2.5s
    server.SDGF_FAILED_RECHECK_DELAY = 0.05
    ok = True
    try:
        channel = {"baseUrl": mock.base, "apiKey": KEY, "model": "sd2.5-720p-ap"}
        base_body = {"prompt": "ok", "images": ["https://cdn.example.com/ref-1.png"],
                     "duration": 8, "ratio": "9:16", "resolution": "720",
                     "audio": True, "channel": channel}

        section("1 构造合同：鉴权头 + 两族方言（纯本地，不联网）")
        ok &= check(server.sdgf_api_base("https://api.example.com/") == "https://api.example.com/v1"
                    and server.sdgf_api_base("https://api.example.com/v1/") == "https://api.example.com/v1",
                    "baseUrl 归一：补 /v1，已带不重复")
        full = server.sdgf_plan({**base_body, "channel": {**channel, "model": "SD2.5-满血-GF-720P"}})
        fb = full["preview"]["body"]
        ok &= check(fb.get("enableSound") == "on" and "metadata" not in fb,
                    "满血名：顶层 enableSound=on、不塞 metadata", json.dumps(fb, ensure_ascii=False))
        alias = server.sdgf_plan(base_body)
        ab = alias["preview"]["body"]
        ok &= check(ab.get("aspect_ratio") == "9:16" and ab.get("metadata") == {"ratio": "9:16", "enableSound": "on"}
                    and "enableSound" not in ab,
                    "别名族：aspect_ratio 双写 metadata.ratio + enableSound 进 metadata",
                    json.dumps(ab, ensure_ascii=False))
        muted = server.sdgf_plan({**base_body, "audio": False})["preview"]["body"]
        ok &= check("enableSound" not in muted and "enableSound" not in (muted.get("metadata") or {}),
                    "audio=false：两族都不发声音字段", json.dumps(muted, ensure_ascii=False))
        modes = [server.sdgf_plan({**base_body, "images": ["https://cdn.example.com/a.png"] * n,
                                   "prompt": "ok"})["preview"]["body"]["mode"] for n in (0, 1, 2, 3)]
        ok &= check(modes == ["文生视频", "首帧生视频", "首尾帧", "参考生视频"],
                    "mode 按图数：0/1/2/3 → 文生/首帧/首尾/参考", str(modes))
        legacy = server.sdgf_plan({**base_body, "duration": 30,
                                   "channel": {**channel, "model": "sd2-720p-ds-v933"}})
        ok &= check(legacy["preview"]["body"].get("duration") == 15
                    and any("上限 15 秒" in n for n in legacy["preview"]["notes"]),
                    "SD2.0 旧合同：30 秒截断到 15 并留 note", json.dumps(legacy["preview"]["body"], ensure_ascii=False))
        mismatch = server.sdgf_plan({**base_body, "resolution": "1080"})["preflight"]
        ok &= check(not mismatch["ok"] and any("模型名档位" in r for r in mismatch["reasons"]),
                    "resolution 与模型名档位不一致 → preflight 拒绝", str(mismatch["reasons"]))
        insecure = server.sdgf_plan({**base_body, "channel": {**channel, "baseUrl": "http://api.example.com"}})
        ok &= check(not insecure["preflight"]["ok"] and any("必须是 https" in r for r in insecure["preflight"]["reasons"]),
                    "非回环 http 渠道 → 拒绝（凭证不走明文）", str(insecure["preflight"]["reasons"]))
        http_img = server.sdgf_plan({**base_body, "images": ["http://cdn.example.com/a.png"]})["preflight"]
        ok &= check(not http_img["ok"] and any("https 直链" in r for r in http_img["reasons"]),
                    "参考图非 https → preflight 拒绝", str(http_img["reasons"]))
        png = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\nraw").decode()
        data_plan = server.sdgf_plan({**base_body, "images": [{"name": "ref.png", "dataUrl": png}]})
        ok &= check(data_plan["preflight"]["ok"]
                    and data_plan["preview"]["images"][0]["source"] == "dataUrl"
                    and str(data_plan["preview"]["body"]["images"][0]).startswith("<dataUrl:"),
                    "dataUrl 图：dry 只做本地校验，预览里标「待传图床」",
                    json.dumps(data_plan["preview"]["body"], ensure_ascii=False))
        real_store = server.store_public_image
        server.store_public_image = lambda data, name: ("https://mock.host/ref.png", "uguu")
        try:
            hosted = server.sdgf_host_images(data_plan)
            ok &= check(hosted["images"] == ["https://mock.host/ref.png"],
                        "真提交前 dataUrl 换公网 URL（占位被替换）", str(hosted.get("images")))
        finally:
            server.store_public_image = real_store

        section("2 confirm=false 必须是 dry：不发任何请求、不花钱")
        for label, confirm in (("缺 confirm", None), ("confirm=false", False),
                               ("confirm='false'（字符串）", "false"), ("confirm=1（整数）", 1)):
            payload = dict(base_body)
            if confirm is not None:
                payload["confirm"] = confirm
            h = FakeHandler(payload)
            h.do_sdgf_video_run()
            status, body = h.sent[-1]
            text = json.dumps(body, ensure_ascii=False)
            ok &= check(status == 200 and body.get("dry") is True and body.get("preflight", {}).get("ok") is True,
                        f"{label} → dry 预演 200/preflight.ok", f"{status} {text[:160]}")
            ok &= check(KEY not in text and body.get("preview", {}).get("headers", {}).get("Authorization") == "Bearer ***",
                        f"{label} → 预览不回显 key（Bearer ***）", text[:160])
        ok &= check(len(mock.requests) == 0, "dry 期间假渠道零请求", f"{len(mock.requests)} 次")
        h = FakeHandler(base_body)
        h.do_sdgf_video_run()
        preview = h.sent[-1][1]["preview"]
        ok &= check(preview["url"] == f"{mock.base}/v1/videos"
                    and preview["body"]["model"] == "sd2.5-720p-ap"
                    and preview["body"]["duration"] == 8
                    and preview["body"]["aspect_ratio"] == "9:16"
                    and preview["body"]["images"] == ["https://cdn.example.com/ref-1.png"],
                    "预演体 = 将提交的完整参数（URL/模型/时长/比例/图）", json.dumps(preview, ensure_ascii=False)[:200])

        section("3 非法 body 拒绝")
        class RawHandler(server.Handler):
            def __init__(self, raw: bytes) -> None:
                self._raw = raw

            def read_body(self) -> bytes:
                return self._raw

            def send_json(self, obj, status: int = 200) -> None:
                self.sent = (status, obj)

        try:
            RawHandler(b"{not json").read_json()
            ok &= check(False, "坏 JSON 应 400")
        except server.ApiError as err:
            ok &= check(err.status == 400, "坏 JSON → 400", err.msg)
        for label, payload, want in (
            ("空 prompt", {**base_body, "prompt": "   "}, "prompt 不能为空"),
            ("images 不是列表", {**base_body, "images": "https://cdn.example.com/a.png"}, "images 必须是列表"),
        ):
            h = FakeHandler(payload)
            try:
                h.do_sdgf_video_run()
                ok &= check(False, f"{label} 应 400")
            except server.ApiError as err:
                ok &= check(err.status == 400 and want in err.msg, f"{label} → 400", err.msg)
        bad = FakeHandler({**base_body, "duration": 2, "confirm": True})
        bad.do_sdgf_video_run()
        status, body = bad.sent[-1] if bad.sent else (None, {})
        ok &= check(status == 400 and body.get("preflight", {}).get("ok") is False
                    and "未提交" in str(body.get("error")) and not mock.post_requests(),
                    "confirm=true + 预演不通过 → 400、未提交、渠道零请求",
                    f"{status} {json.dumps(body, ensure_ascii=False)[:200]}")

        section("4 confirm=true 全链（本地假渠道）：建任务 → 进度透传 → 代下 → /file")
        run_ok = FakeHandler({**base_body, "confirm": True})
        run_ok.do_sdgf_video_run()
        status, created = run_ok.sent[-1]
        job, task_id = created.get("job", ""), created.get("taskId", "")
        ok &= check(status == 200 and bool(job) and task_id == "mock-ok-1" and created.get("dry") is False,
                    "run 立即回 {job, taskId}", json.dumps(created, ensure_ascii=False)[:160])
        post = mock.post_requests()[0]
        sent = json.loads(post["raw"].decode("utf-8"))
        ok &= check(post["path"] == "/v1/videos"
                    and post["headers"].get("Authorization") == f"Bearer {KEY}"
                    and post["headers"].get("Content-Type", "").startswith("application/json"),
                    "提交：POST /v1/videos + Authorization: Bearer <key> + JSON",
                    f"{post['path']} {post['headers'].get('Authorization')}")
        ok &= check("aspect_ratio" in sent and sent.get("metadata", {}).get("ratio") == "9:16"
                    and sent.get("metadata", {}).get("enableSound") == "on",
                    "提交体带别名族双写方言", json.dumps(sent, ensure_ascii=False)[:200])
        state, seen = wait_job(server, job)
        ok &= check(state["status"] == "done" and state.get("bytes") == len(VIDEO),
                    "job done 且 bytes = 成片面大小", json.dumps(state, ensure_ascii=False))
        ok &= check(any(s.get("progress") == 42 for s in seen),
                    "进度 42 原样透传（running 态 progress 字段）", str([s.get("progress") for s in seen]))
        content_get = [r for r in mock.requests if r["method"] == "GET" and r["path"].endswith("/content")]
        ok &= check(len(content_get) == 1 and content_get[0]["headers"].get("Authorization") == f"Bearer {KEY}",
                    "metadata.url 为空 → 走鉴权 content 端点下载",
                    str(content_get[:1]))
        final = server.sdgf_task_dir("mock-ok-1") / server.SDGF_FINAL
        ok &= check(final.is_file() and final.read_bytes() == VIDEO,
                    "成片落 uploads/sdgf/<taskId>/成片.mp4", str(final))
        record = (server.sdgf_task_dir("mock-ok-1") / server.SDGF_RECORD).read_text(encoding="utf-8")
        ok &= check(KEY not in record and "mock-ok-1" in record,
                    "任务记录不含凭证", record[:160].replace("\n", " "))

        file_h = FakeHandler({})
        file_h.send_mp4 = lambda path: file_h.sent.append((200, {"path": str(path)}))
        file_h.serve_sdgf_video("mock-ok-1")
        ok &= check(file_h.sent[-1][1]["path"] == str(final), "/file/<taskId> 直接给本地成片", str(file_h.sent[-1]))
        try:
            file_h.serve_sdgf_video("no-such-task")
            ok &= check(False, "未知 taskId 应 404")
        except server.ApiError as err:
            ok &= check(err.status == 404, "未知 taskId → 404", err.msg)
        try:
            file_h.serve_sdgf_video("../etc/passwd")
            ok &= check(False, "穿越 taskId 应 400")
        except server.ApiError as err:
            ok &= check(err.status == 400, "路径穿越 taskId → 400", err.msg)

        section("5 直链下载不带鉴权头（key 不出渠道域）")
        dir_h = FakeHandler({**base_body, "prompt": "direct", "confirm": True})
        dir_h.do_sdgf_video_run()
        dstate, _ = wait_job(server, dir_h.sent[-1][1]["job"])
        direct_gets = [r for r in mock.requests if r["method"] == "GET" and r["path"].startswith("/files/")]
        ok &= check(dstate["status"] == "done" and direct_gets
                    and "Authorization" not in direct_gets[0]["headers"],
                    "metadata.url 完整 http(s)：直取且不带 Authorization", str(direct_gets[:1]))

        section("6 渠道判失败：错误透出但 key 抹成 ***")
        fail_h = FakeHandler({**base_body, "prompt": "fail", "confirm": True})
        fail_h.do_sdgf_video_run()
        fstate, _ = wait_job(server, fail_h.sent[-1][1]["job"])
        ftext = json.dumps(fstate, ensure_ascii=False)
        ok &= check(fstate["status"] == "failed" and KEY not in ftext and "***" in ftext,
                    "failed 状态回错误原因、凭证已抹", ftext[:220])

        section("7 并发上限 2：第三条 409，dry 不占位")
        held = [server.SDGF_JOBS.acquire(blocking=False), server.SDGF_JOBS.acquire(blocking=False)]
        try:
            dry_h = FakeHandler(base_body)
            dry_h.do_sdgf_video_run()
            ok &= check(dry_h.sent[-1][0] == 200 and dry_h.sent[-1][1].get("dry") is True,
                        "并发槽占满时 dry 仍可用（不占位）", str(dry_h.sent[-1])[:120])
            busy = FakeHandler({**base_body, "confirm": True})
            try:
                busy.do_sdgf_video_run()
                ok &= check(False, "第三条 confirm=true 应 409")
            except server.ApiError as err:
                ok &= check(err.status == 409, "第三条 confirm=true → 409", err.msg)
        finally:
            for _ in held:
                server.SDGF_JOBS.release()

        section("8 job 不存在 → 404")
        try:
            server.sdgf_job_state("0000000000000000")
            ok &= check(False, "未知 job 应 404")
        except server.ApiError as err:
            ok &= check(err.status == 404, "未知 job → 404", err.msg)

        all_text = json.dumps([run_ok.sent, dir_h.sent, fail_h.sent, h.sent, bad.sent], ensure_ascii=False)
        ok &= check(KEY not in all_text, "所有端点响应里都不出现 key", "命中即失败")
    finally:
        server.UPLOADS = real_uploads
        server.SDGF_POLL_INTERVAL, server.SDGF_FAILED_RECHECK_DELAY = real_interval, real_fail_delay
        mock.close()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{PASS} PASS / {FAIL} FAIL → {'ALL PASS' if ok and FAIL == 0 else 'HAS FAIL'}")
    return 0 if ok and FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(run())
