#!/usr/bin/env python3
"""万相视频生成 bridge 三端点检查（mock 子进程——绝不真跑 wan CLI，不花钱）。

覆盖（对应 docs 里的 bridge 契约）：
- POST /api/wan/video/run：dataUrl 按 mime 解码、按列表顺序落盘 assets/图片N.<ext>；
  提示词-canvas.md 落盘；dry（confirm=false）命令行**不含** --confirm；返回 {job, taskId}。
- GET /api/wan/video/job/<jobId>：伪造 生成/记录.json 各阶段 → 计划/已提交/生成中/下载中/完成/失败；
  子进程 exit != 0 → failed + stderr 尾部一句。
- GET /api/wan/video/file/<taskId>：无成片 404；有成片 200 video/mp4（Range 206）。
- 并发：万相 CLI 同时只认一条，第二条 409。

子进程替身：patch server.run_py（worker 唯一的子进程出口），命令行只记录不执行。
"""

from __future__ import annotations

import base64
import importlib.util
import io
import json
import re
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PASS = FAIL = 0


def check(ok: bool, name: str, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"[PASS] {name}")
    else:
        FAIL += 1
        print(f"[FAIL] {name}｜{detail}")


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def data_url(mime: str, blob: bytes) -> str:
    return f"data:{mime};base64," + base64.b64encode(blob).decode()


PNG = b"\x89PNG\r\n\x1a\n" + b"png-payload-"
JPG = b"\xff\xd8\xff" + b"jpg-payload----"
WEBP = b"RIFF\x24\x00\x00\x00WEBP" + b"webp-payload"
MP4 = b"\x00\x00\x00\x18ftypisom\x00\x02\x00" + b"mp4-payload"


def make_handler(server, *, path: str, payload: dict | None = None,
                 headers: dict | None = None):
    """把 socket 层掐掉：read_json/send_json 换成内存版；wfile 用 BytesIO 承接 send_mp4。"""
    outer = self_ref = None

    class H(server.Handler):
        def __init__(self) -> None:
            self.path = path
            self.command = "POST" if payload is not None else "GET"
            self.headers = dict(headers or {})
            self._payload = dict(payload or {})
            self.sent: list[tuple[int, object]] = []
            self.wfile = io.BytesIO()
            self.request_version = "HTTP/1.1"
            self.client_address = ("127.0.0.1", 0)
            # send_mp4 → send_response → log_request 需要的请求行属性（无 socket）
            self.requestline = f"{self.command} {path} HTTP/1.1"

        def read_json(self) -> dict:
            return dict(self._payload)

        def send_json(self, obj, status: int = 200) -> None:
            self.sent.append((status, obj))

    return H()


def wait_job_done(server, job_id: str, timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with server.JOBS_LOCK:
            rec = dict(server.JOBS.get(job_id) or {})
        if rec and rec.get("status") != "running":
            return rec
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} 没在 {timeout}s 内结束（替身子进程卡住？）")


def write_record(base: Path, task_id: str, rec: dict) -> None:
    d = base / task_id / "生成"
    d.mkdir(parents=True, exist_ok=True)
    (d / "记录.json").write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")


def main() -> int:
    global server
    server = load_module("ui_server_wan_bridge", ROOT / "ui" / "server.py")
    tmp = Path(tempfile.mkdtemp(prefix="wan-bridge-"))
    real_analysis, real_run_py = server.ANALYSIS, server.run_py
    server.ANALYSIS = tmp

    recorded: list[list[str]] = []
    gate = threading.Event()

    def fake_run_py(args, timeout=0, extra_env=None):
        recorded.append(list(args))
        gate.wait(10)                       # 关闸 = 子进程"还在跑"，用来测并发 409
        return 0, '{"status": "done"}', ""

    server.run_py = fake_run_py

    body = {
        "prompt": "一镜到底，@图片1 香水 与 @图片2 女性 在晨光里转身。",
        "images": [
            {"name": "甲.png", "dataUrl": data_url("image/png", PNG)},
            {"name": "乙故意写错.png", "dataUrl": data_url("image/jpeg", JPG)},   # mime 说了算
            {"name": "丙.webp", "dataUrl": data_url("image/webp", WEBP)},
        ],
        "duration": 8, "ratio": "9:16", "resolution": "1080", "audio": False,
        "confirm": False,
    }

    try:
        # ---- ① run：dry 命令行 + dataUrl 落盘 ----
        h = make_handler(server, path="/api/wan/video/run", payload=body)
        h.route_post("/api/wan/video/run")
        status, resp = h.sent[-1]
        task_id = str(resp.get("taskId") or "")
        check(status == 200 and re.fullmatch(r"[0-9a-f]{16}", str(resp.get("job") or "")) is not None
              and re.fullmatch(r"wan-\d+", task_id) is not None,
              "① run 返回 200 + {job: 16位hex, taskId: wan-<unixts>}", f"{status} {resp}")

        d = tmp / task_id
        prompt_text = (d / "提示词-canvas.md").read_text(encoding="utf-8")
        check(prompt_text == body["prompt"], "② prompt 原样写入 提示词-canvas.md",
              prompt_text[:40])
        saved = [(d / "assets" / f"图片{n}.{ext}") for n, ext in
                 ((1, "png"), (2, "jpg"), (3, "webp"))]
        check(all(p.is_file() for p in saved)
              and saved[0].read_bytes() == PNG and saved[1].read_bytes() == JPG
              and saved[2].read_bytes() == WEBP,
              "③ images 按 dataUrl 的 mime 解码、按列表顺序落盘 图片1..3（文件名不参与判型）",
              str([p.name for p in saved if not p.is_file()]))

        argv = recorded[-1]
        check("--confirm" not in argv,
              "④ dry（confirm=false）命令行不含 --confirm（引擎只打计划不提交）", str(argv))
        pairs = dict(zip(argv, argv[1:]))
        check(pairs.get("--id") == task_id and pairs.get("--template") == "canvas"
              and pairs.get("--duration") == "8" and pairs.get("--ratio") == "9:16"
              and pairs.get("--resolution") == "1080P" and pairs.get("--audio-output") == "false",
              "⑤ 参数名与 wan_video.py 的 argparse 逐一对上（480/720/1080 → P 后缀）", str(argv))

        # ---- ⑥ 并发 409：子进程还"在跑"时第二条必须被挡 ----
        h2 = make_handler(server, path="/api/wan/video/run", payload={**body, "confirm": True})
        try:
            h2.route_post("/api/wan/video/run")
            check(False, "⑥ 并发第二条 → 409", f"没拦住：{h2.sent}")
        except server.ApiError as err:
            check(err.status == 409, "⑥ 并发第二条 → 409（CLI 同时只认一条）", err.msg)

        gate.set()
        rec = wait_job_done(server, str(resp["job"]))
        check(rec.get("status") == "done" and rec.get("exit") == 0,
              "⑦ 替身子进程跑完 → JOBS 记录 done + 退出码 0（并发位随后释放）", str(rec))

        # ---- ⑧ confirm=true 命令行含 --confirm（audio 键整个缺省 → true）----
        no_audio_body = {k: v for k, v in body.items() if k != "audio"}
        h3 = make_handler(server, path="/api/wan/video/run",
                          payload={**no_audio_body, "confirm": True})
        h3.route_post("/api/wan/video/run")
        task2 = str(h3.sent[-1][1].get("taskId") or "")
        argv2 = recorded[-1]
        check("--confirm" in argv2
              and argv2[argv2.index("--audio-output") + 1] == "true",
              "⑧ confirm=true 命令行带 --confirm；audio 缺省 → --audio-output true", str(argv2))
        wait_job_done(server, str(h3.sent[-1][1]["job"]))

        # ---- ⑨ 入参校验 400 ----
        cases = [
            ({**body, "prompt": "  "}, "prompt 为空"),
            ({**body, "duration": 31}, "duration=31"),
            ({**body, "duration": "x"}, "duration 非整数"),
            ({**body, "ratio": "4:3"}, "ratio 不在白名单"),
            ({**body, "resolution": "4320"}, "resolution 不在白名单"),
            ({**body, "images": [{"name": "坏.png", "dataUrl": "data:image/gif;base64,AAAA"}]},
             "dataUrl mime 不支持"),
            ({**body, "images": [{"name": "坏.png", "dataUrl": "not-a-data-url"}]},
             "dataUrl 形式不对"),
        ]
        ok400 = True
        for bad, why in cases:
            hb = make_handler(server, path="/api/wan/video/run", payload=bad)
            try:
                hb.route_post("/api/wan/video/run")
                ok400 = False
                print(f"    [!] {why} 没拦住：{hb.sent}")
            except server.ApiError as err:
                ok400 = ok400 and err.status == 400
        # ---- ⑤b resolution=480 放行（2026-11-05 修：CLI RESOLUTIONS 含 480P）----
        try:
            hb2 = make_handler(server, path="/api/wan/video/run", payload={**body, "resolution": "480"})
            hb2.route_post("/api/wan/video/run")
            check(hb2.sent and hb2.sent[0][0] == 200 and "job" in hb2.sent[0][1], "⑤b resolution=480 放行（CLI 支持 480P，不再 400）", str(hb2.sent)[:200])
        except server.ApiError as err:
            check(False, "⑤b resolution=480 放行（CLI 支持 480P，不再 400）", f"仍被拒：{err.status} {err.msg}")
        check(ok400, "⑨ 空 prompt / 时长越界 / 非法 ratio·resolution / 坏 dataUrl → 全部 400",
              "见上面 [!] 行")

        # ---- ⑩~⑯ job 状态映射（伪造 记录.json 各阶段）----
        def put_job(job_id: str, task: str, **kw) -> None:
            with server.JOBS_LOCK:
                server.JOBS[job_id] = {"status": "running", "started": time.time(),
                                       "kind": "wan-video", "task_id": task, **kw}

        def job_state(job_id: str) -> tuple[int, dict]:
            hj = make_handler(server, path=f"/api/wan/video/job/{job_id}")
            hj.route_get(f"/api/wan/video/job/{job_id}")
            return hj.sent[-1]

        try:
            server.wan_video_job_state("no-such-job")
            check(False, "⑩ 未知的 job 号 → 404", "没抛")
        except server.ApiError as err:
            check(err.status == 404, "⑩ 未知的 job 号 → 404", err.msg)

        put_job("j1", "wan-401")
        st, out = job_state("j1")
        check(st == 200 and out.get("status") == "running" and out.get("stage") == "计划",
              "⑪ 没有记录.json → running/计划（还在预演）", str(out))

        write_record(tmp, "wan-402", {"status": "running"})
        put_job("j2", "wan-402")
        st, out = job_state("j2")
        check(out.get("status") == "running" and out.get("stage") == "已提交",
              "⑫ 记录 running 且无 taskId → 已提交（CLI 提交中）", str(out))

        write_record(tmp, "wan-403", {"status": "running", "taskId": "cloud-abc"})
        put_job("j3", "wan-403")
        st, out = job_state("j3")
        check(out.get("status") == "running" and out.get("stage") == "生成中",
              "⑬ 记录 running + taskId、还没有 mp4 → 生成中", str(out))

        gen403 = tmp / "wan-403" / "生成"
        (gen403 / "cloud-abc.mp4").write_bytes(MP4)      # CLI --save 先落的原始名
        st, out = job_state("j3")
        check(out.get("status") == "running" and out.get("stage") == "下载中",
              "⑭ 生成/ 出现新鲜 mp4（还不是成片名）→ 下载中", str(out))

        write_record(tmp, "wan-404", {"status": "done", "video": "成片-canvas.mp4"})
        put_job("j4", "wan-404")
        st, out = job_state("j4")
        check(out.get("status") == "done" and out.get("stage") == "完成",
              "⑮ 记录 done → done/完成", str(out))

        write_record(tmp, "wan-405", {"status": "failed",
                                      "error": "提交前核对未通过，没有提交：正文被改"})
        put_job("j5", "wan-405")
        st, out = job_state("j5")
        check(out.get("status") == "failed" and "正文被改" in str(out.get("error")),
              "⑯ 记录 failed → failed + 记录里的原因", str(out))

        # ---- ⑰ 子进程退出码非 0 → failed + stderr 尾部一句 ----
        gate.clear()
        server.run_py = lambda args, timeout=0, extra_env=None: (
            recorded.append(list(args)), (2, "", "参数错\n时长越界（31 秒）"))[1]
        h4 = make_handler(server, path="/api/wan/video/run",
                          payload={**body, "confirm": True})
        h4.route_post("/api/wan/video/run")
        rec = wait_job_done(server, str(h4.sent[-1][1]["job"]))
        st, out = job_state(str(h4.sent[-1][1]["job"]))
        check(rec.get("exit") == 2 and out.get("status") == "failed"
              and out.get("error") == "时长越界（31 秒）",
              "⑰ 子进程 exit=2 → failed + stderr 尾部一句（不是整段）", f"{rec} / {out}")
        check("时长越界" in (tmp / str(h4.sent[-1][1]["taskId"]) / "生成" / "bridge日志.txt")
              .read_text(encoding="utf-8"),
              "⑱ 子进程 stdout/stderr 落 生成/bridge日志.txt（事后能对账）", "")
    finally:
        gate.set()
        server.run_py = real_run_py
        server.ANALYSIS = real_analysis

    # ---- ⑲⑳ file 端点（真实 send_mp4，BytesIO 承接）----
    server.ANALYSIS = tmp
    try:
        hf = make_handler(server, path="/api/wan/video/file/wan-none")
        try:
            hf.route_get("/api/wan/video/file/wan-none")
            check(False, "⑲ 没有成片 → 404", f"返回了 {hf.sent}")
        except server.ApiError as err:
            check(err.status == 404, "⑲ 没有成片 → 404", err.msg)

        try:
            hf2 = make_handler(server, path="/api/wan/video/file/..%2Fevil")
            hf2.route_get("/api/wan/video/file/..%2Fevil")
            check(False, "⑳ 非法 taskId → 400", f"返回了 {hf2.sent}")
        except server.ApiError as err:
            check(err.status == 400, "⑳ 非法 taskId → 400", err.msg)

        final_dir = tmp / "wan-900" / "生成"
        final_dir.mkdir(parents=True, exist_ok=True)
        (final_dir / "成片-canvas.mp4").write_bytes(MP4)
        hf3 = make_handler(server, path="/api/wan/video/file/wan-900")
        hf3.route_get("/api/wan/video/file/wan-900")
        raw = hf3.wfile.getvalue()
        check(b"HTTP/1.1 200" in raw and b"Content-Type: video/mp4" in raw
              and raw.endswith(MP4),
              "㉑ 有成片 → 200 video/mp4，字节原样（route_get 真路由命中）",
              raw[:80].decode("latin-1", "replace"))

        hf4 = make_handler(server, path="/api/wan/video/file/wan-900",
                           headers={"Range": "bytes=0-7"})
        hf4.route_get("/api/wan/video/file/wan-900")
        raw = hf4.wfile.getvalue()
        check(b"HTTP/1.1 206" in raw and b"Content-Range: bytes 0-7/" in raw
              and raw.endswith(MP4[:8]),
              "㉒ Range 请求 → 206 + Content-Range（<video> 拖进度条用）", raw[:120])
    finally:
        server.ANALYSIS = real_analysis

    print(f"  SUMMARY {PASS}/{PASS + FAIL} PASS / {FAIL} FAIL")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
