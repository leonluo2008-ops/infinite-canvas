# -*- coding: utf-8 -*-
"""探针数据根守卫——把「真实任务只读」变成探针里的机器断言（AT-14 B2 P0 闭合）。

事故（2026-11-11）：`ui/hub_api.py` 的 `TASKS_DIR` 硬编码 `ROOT/automation/projects`，不认
`WANREPRO_AUTO_DIR`。于是「设 env 起隔离实例」是**伪隔离**：读走真实目录、写走隔离目录。
隔离目录为空 → 首投分配到 `auto-0001` → 探针按 `config.PROJECTS_DIR/created` 裸 `rmtree`，
删掉的是**真实任务 auto-0001**（不进回收站、不入 git、不可恢复）。

本模块固化三条不变量，所有会写/删任务目录的探针都必须走它：

1. **同源**：探针的 `PROJECTS_DIR` 必须等于被探测服务端自述的 `TASKS_DIR`
   （`GET /api/hub/where`）——不一致**拒跑**，不带病跑；
2. **只删自建**：清理只允许删「本次新建 ∧ 跑前不存在 ∧ 项目根的直接子目录」的任务目录，
   裸 `rmtree` 在本模块里不存在这个入口；
3. **有损即红**：跑后项目集必须 ⊆ 跑前（真实任务一条都不能少），丢失即 GuardError。

另附两个探针公用的小工具（HTTP 直连、multipart 拼装、空闲端口），避免各探针各写一份。
"""
from __future__ import annotations

import json
import shutil
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# 红线：本机直连不走系统代理
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class GuardError(RuntimeError):
    """数据根守卫断言失败（拒跑 / 有损）。消息即人话原因，直接贴报告。"""


# ── HTTP / multipart 小工具 ───────────────────────────────────────────
def http_req(base: str, path: str, method: str = "GET", data: bytes | None = None,
             ctype: str | None = None, timeout: int = 90) -> tuple[int, dict | bytes]:
    """发一个请求 → (status, json|bytes)。HTTPError 不当异常，按状态码如实返回。"""
    r = urllib.request.Request(base.rstrip("/") + path, data=data, method=method)
    if ctype:
        r.add_header("Content-Type", ctype)
    try:
        resp = OP.open(r, timeout=timeout)
        raw = resp.read()
        return resp.status, (json.loads(raw) if raw[:1] in (b"{", b"[") else raw)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return e.code, raw


def multipart_body(fields: dict[str, str], files: dict[str, tuple[str, bytes]],
                   boundary: str = "probe7f3a9c") -> tuple[bytes, str]:
    """拼 multipart 请求体 → (body, 可直接塞 Content-Type 的 boundary 参数串)。"""
    out = b""
    for k, v in fields.items():
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n").encode()
    for k, (fn, blob) in files.items():
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{fn}\"\r\n"
                f"Content-Type: application/octet-stream\r\n\r\n").encode() + blob + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return out, f"multipart/form-data; boundary={boundary}"


def free_port() -> int:
    """要一个当前空闲的本机端口（绑 0 取端口后立刻释放；有极小竞态，够探针用）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class StubBridge:
    """进程内假 bridge（只实现 ingest 用到的两个面）——隔离跑时不许打真实 bridge。

    ingest 只用 `POST /api/video/upload`（→ ok/id/name/size）和 `GET /api/tasks`（校验 id 存在）。
    打真实 bridge 会往真实 `uploads/` 落 `片名-N.mp4` 副本（历史上攒了 100+ 个），
    所以隔离跑把这条依赖也换成假的。用法：`with StubBridge() as stub: env["WANREPRO_BRIDGE"]=stub.base`。
    """

    def __init__(self) -> None:
        self.ids: list[str] = []
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):        # 不污染探针输出
                return

            def _send(self, code: int, payload: dict) -> None:
                raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self) -> None:        # noqa: N802
                if self.path != "/api/video/upload":
                    self._send(404, {"error": "stub bridge 只认 /api/video/upload"})
                    return
                n = int(self.headers.get("Content-Length") or 0)
                blob = self.rfile.read(n) if n else b""
                name = urllib.parse.unquote(self.headers.get("X-Filename") or "probe.mp4")
                with outer._lock:
                    bid = f"probe-bridge-{len(outer.ids) + 1:04d}"
                    outer.ids.append(bid)
                self._send(200, {"ok": True, "id": bid, "name": name, "size": len(blob)})

            def do_GET(self) -> None:         # noqa: N802
                if self.path == "/api/tasks":
                    with outer._lock:
                        tasks = [{"id": i} for i in outer.ids]
                    self._send(200, {"tasks": tasks})
                    return
                self._send(404, {"error": "stub bridge 只认 /api/tasks"})

        self.port = free_port()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> "StubBridge":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


# ── 不变量 1：同源 ───────────────────────────────────────────────────
def projects_snapshot(root: str | Path) -> frozenset[str]:
    """任务目录集快照（只认直接子目录 auto-*；不递归、不跟随 symlink）。"""
    p = Path(root)
    if not p.is_dir():
        return frozenset()
    return frozenset(d.name for d in p.glob("auto-*") if d.is_dir() and not d.is_symlink())


def assert_same_root(server_where: dict, probe_projects_dir: str | Path) -> None:
    """断言「服务端写入目录 == 探针读取目录」，否则 GuardError（拒跑）。"""
    if not isinstance(server_where, dict):
        raise GuardError(f"服务端没给自述（/api/hub/where 返回 {type(server_where).__name__}）——"
                         "无法证明读写同源，拒跑")
    claimed = server_where.get("tasks_dir")
    if not claimed:
        raise GuardError("服务端自述里没有 tasks_dir（服务端版本过旧或 /api/hub/where 不可用）——"
                         "无法证明读写同源，拒跑")
    server_root = Path(str(claimed)).resolve()
    probe_root = Path(probe_projects_dir).resolve()
    if server_root != probe_root:
        raise GuardError(f"数据根不同源：服务端写入 {server_root} ≠ 探针读取 {probe_root}——"
                         "拒跑（读/写分家的伪隔离会误删真实任务）")


# ── 不变量 3：有损即红 ───────────────────────────────────────────────
def assert_no_loss(before: frozenset[str] | set[str], after: frozenset[str] | set[str]) -> None:
    """跑后集合必须 ⊆ 跑前；丢一条就 GuardError。多出来的由调用方决定算不算垃圾。"""
    lost = sorted(set(before) - set(after))
    if lost:
        raise GuardError(f"真实任务丢失 {lost}（跑前 {sorted(before)} → 跑后 {sorted(after)}）")


# ── 不变量 2：只删自建 ───────────────────────────────────────────────
def cleanup_created(root: str | Path, local_id: str, *, before: set[str] | frozenset[str],
                    created: set[str] | frozenset[str]) -> tuple[bool, str]:
    """删本次探针自建的任务目录 → (是否删除, 人话原因)。任一条守卫不过就拒删。

    守卫顺序即事故防线：跑前已存在（真实任务）> 非本次新建 > 非普通目录 > 非项目根直系。
    """
    root_p = Path(root)
    if local_id in before:
        return False, f"拒删 {local_id}：跑前已存在（真实任务只读）"
    if local_id not in created:
        return False, f"拒删 {local_id}：不是本次新建的 id"
    target = root_p / local_id
    if not target.is_dir() or target.is_symlink():
        return False, f"拒删 {target}：不存在或不是普通目录"
    try:
        parent_ok = target.resolve().parent == root_p.resolve()
    except OSError:
        parent_ok = False
    if not parent_ok:
        return False, f"拒删 {target}：不在项目根的直接子目录下"
    shutil.rmtree(target)
    return True, f"已删自建任务 {target}"
