"""S3.12-B1：图床偏好服务端持久化（user_media_prefs + 清单/写偏好/连通测试三条路由）。

红线（S3.12 任务书 §二 B1 / C1–C7）
-----------------------------------
* **单人零改动**：`MULTI_USER=0` 时 `handle()` 首行即 `return False`——整条路由不存在
  （落到既有链给 404），不建表、不读库，与 channel_store 完全一致。
* **登录硬前置**：三条路由任何分支取不到会话用户一律 401（与 `_handle_channels` 同款）。
* **configured 判定与 server.host_image_detailed 同口径**（见 `_missing_envs` 注释）：
  tencent 双 env、local 单 env、litterbox/uguu 恒可用。不 import server（循环依赖），
  改一处必须同步另一处。
* **测试探针失败也是 200**：`ok` 字段承载结果，绝不把失败写成 4xx/5xx；所有分支
  都要捕获网络异常，异常冒到 500 就是事故。
* **不碰上传链路**：`host_image_detailed` / `store_public_image` / `do_asset_upload`
  是 B2 的活，本模块零改动（B1 只加路由）。
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler

try:  # 仓根包形态（server/探针导入）
    from wanrepro.ui import multiuser
except ImportError:  # 脚本直跑形态（ui/ 在 sys.path[0]）
    import multiuser  # type: ignore[no-redef]

# ------------------------------------------------------------------ 常量

ROUTE = "/api/user/media-host"
TEST_ROUTE = ROUTE + "/test"

COOLDOWN = 5            # 同用户两次连通测试的最小间隔（秒，C5）
COOLDOWN_CAP = 4096     # 冷却 dict 封顶（multiuser._cap 同款，防内存无界增长）
_MISSING_ENV_LIMIT = 120  # reason 截断长度

# 1x1 透明 PNG（67 字节）：手工拼的字节常量（PNG 签名 + IHDR + IDAT + IEND），
# 零第三方依赖。只用于连通测试的真实上传探针。
PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108000000003a7"
    "e9b550000000a4944415478da6360000000020001e527defc00000000494"
    "54e44ae426082")
assert len(PNG_1X1) == 67

# 冷却 dict + 锁（进程内；C5：键 = username）
_COOLDOWN: dict[str, float] = {}
_COOLDOWN_LOCK = threading.Lock()

SCHEMA = (
    # 不设外键；用户删除功能上线时需级联清理
    "CREATE TABLE IF NOT EXISTS user_media_prefs("
    "user_id INTEGER PRIMARY KEY, media_host TEXT NOT NULL, updated_at INTEGER NOT NULL)"
)

SOURCES_META = (
    ("tencent", "腾讯云图床"),
    ("local", "自托管直链"),
    ("litterbox", "litterbox(72h)"),
    ("uguu", "uguu(3h)"),
)


# ------------------------------------------------------------------ configured 判定

def _missing_envs(source_id: str) -> list[str]:
    """返回该源缺失的环境变量名列表（空列表 = configured）。

    **与 server.host_image_detailed 同口径，改一处必须同步另一处**（server :989-990/:1014）：
    tencent 需 WANREPRO_TENCENT_VC_BASE 与 WANREPRO_TENCENT_VC_TOKEN 都非空；
    local 需 WANREPRO_PUBLIC_BASE 非空；litterbox / uguu 无 env，恒已配置。
    （不能 import server——server import 本模块，会循环依赖，故独立实现同口径判定。）
    """
    if source_id == "tencent":
        missing = []
        if not (os.environ.get("WANREPRO_TENCENT_VC_BASE") or "").strip():
            missing.append("WANREPRO_TENCENT_VC_BASE")
        if not (os.environ.get("WANREPRO_TENCENT_VC_TOKEN") or "").strip():
            missing.append("WANREPRO_TENCENT_VC_TOKEN")
        return missing
    if source_id == "local":
        return [] if (os.environ.get("WANREPRO_PUBLIC_BASE") or "").strip() else ["WANREPRO_PUBLIC_BASE"]
    return []


def sources() -> list[dict]:
    """四源清单：id / label / configured / default（default 只标 tencent = 默认顺序第一源）。"""
    out = []
    for sid, label in SOURCES_META:
        out.append({
            "id": sid,
            "label": label,
            "configured": not _missing_envs(sid),
            "default": sid == "tencent",
        })
    return out


# ------------------------------------------------------------------ 存储

def connect() -> sqlite3.Connection:
    """沿用 multiuser 的库与 PRAGMA 惯例（与 channel_store.connect 同款）。"""
    path = multiuser.db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute(SCHEMA)
    conn.commit()
    return conn


def get_pref(uid: int) -> str | None:
    """该用户的首选图床源；没设过 / 库异常统一按 None（默认顺序）处理。"""
    conn = connect()
    try:
        row = conn.execute("SELECT media_host FROM user_media_prefs WHERE user_id=?", (uid,)).fetchone()
        return str(row[0]) if row else None
    finally:
        conn.close()


def set_pref(uid: int, value: str | None) -> None:
    """value=None 删行（恢复默认顺序）；否则 UPSERT（C1/C7）。"""
    conn = connect()
    try:
        if value is None:
            conn.execute("DELETE FROM user_media_prefs WHERE user_id=?", (uid,))
        else:
            conn.execute(
                "INSERT INTO user_media_prefs(user_id, media_host, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET media_host=excluded.media_host, "
                "updated_at=excluded.updated_at",
                (uid, value, int(time.time())))
        conn.commit()
    finally:
        conn.close()


# ------------------------------------------------------------------ 输出

def _send(h: BaseHTTPRequestHandler, obj: dict, status: int = 200) -> None:
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    h.send_response(status)
    h.send_header("Content-Type", "application/json; charset=utf-8")
    h.send_header("Content-Length", str(len(data)))
    h.send_header("Cache-Control", "no-store")
    h.end_headers()
    if h.command != "HEAD":
        h.wfile.write(data)


def _body(h: BaseHTTPRequestHandler) -> dict:
    try:
        raw = h.rfile.read(int(h.headers.get("Content-Length") or 0)) or b"{}"
        val = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise _Bad(400, "请求体不是合法 JSON") from None
    if not isinstance(val, dict):
        raise _Bad(400, "请求体必须是 JSON 对象")
    return val


class _Bad(Exception):
    """带 HTTP 状态码与文案的拒绝（400 校验失败）。"""

    def __init__(self, status: int, msg: str) -> None:
        super().__init__(msg)
        self.status = status
        self.msg = msg


# ------------------------------------------------------------------ 探针

def _server():
    """取 server 模块（双形态导入）。探针需要它的 env 读取 / UPLOADS / 上传函数 / 验活函数。"""
    try:
        from wanrepro.ui import server  # 仓根包形态
    except ImportError:
        import server  # type: ignore[no-redef]  # 脚本直跑形态
    return server


def _unconfigured_error(sid: str) -> str:
    missing = _missing_envs(sid)
    label = dict(SOURCES_META)[sid]
    return (f"该图床源未配置，无法设为默认：{label}（缺少环境变量：{'、'.join(missing)}）")


def _cooldown_hit(username: str) -> int:
    """命中冷却返回剩余秒数（至少 1），否则记录本次并回 0（C5：封顶 + 惰性清理）。"""
    now = time.time()
    with _COOLDOWN_LOCK:
        multiuser._cap(_COOLDOWN, COOLDOWN_CAP)
        for k in [k for k, t in _COOLDOWN.items() if now - t >= COOLDOWN]:
            _COOLDOWN.pop(k, None)                      # 惰性清理过期键
        last = _COOLDOWN.get(username)
        if last is not None and now - last < COOLDOWN:
            return max(1, int(COOLDOWN - (now - last)) + (1 if (now - last) % 1 else 0))
        _COOLDOWN[username] = now
    return 0


def _probe(sid: str) -> dict:
    """真实探针：按源区分验活口径（与生产分支逐一相同）→ {ok,id,url/ms/reason}。"""
    started = time.time()
    srv = _server()
    hex8 = secrets.token_hex(4)
    try:
        if sid == "tencent":
            base = (os.environ.get("WANREPRO_TENCENT_VC_BASE") or "").strip().rstrip("/")
            token = (os.environ.get("WANREPRO_TENCENT_VC_TOKEN") or "").strip()
            url = base + "/upload/m4/probe-" + hex8 + ".png"
            req = urllib.request.Request(url, data=PNG_1X1, method="PUT",
                                         headers={"X-Token": token, "Content-Type": "application/octet-stream"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                ok = 200 <= resp.status < 300
            if not ok:
                return {"ok": False, "id": sid, "reason": f"上传失败（HTTP {resp.status}）",
                        "ms": int((time.time() - started) * 1000)}
            return {"ok": True, "id": sid, "url": base + "/vc/m4/probe-" + hex8 + ".png",
                    "ms": int((time.time() - started) * 1000)}          # 不回取（与生产口径一致）
        if sid == "local":
            public_base = (os.environ.get("WANREPRO_PUBLIC_BASE") or "").strip().rstrip("/")
            key = "m4/probe-" + hex8 + ".png"
            dest = srv.UPLOADS / "vc" / key
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                dest.write_bytes(PNG_1X1)
                if dest.stat().st_size != 67:                            # 磁盘字节校验
                    return {"ok": False, "id": sid, "reason": "本地备份落盘校验失败（文件缺失或大小不一致）",
                            "ms": int((time.time() - started) * 1000)}
                check = public_base + "/vc/" + key
                with urllib.request.urlopen(check, timeout=30) as resp:   # 回读一次自己写的直链
                    resp.read()
                    alive = resp.status == 200
                if not alive:
                    return {"ok": False, "id": sid, "reason": "直链验活失败（回取非 200）",
                            "ms": int((time.time() - started) * 1000)}
                return {"ok": True, "id": sid, "url": check, "ms": int((time.time() - started) * 1000)}
            finally:
                try:
                    dest.unlink(missing_ok=True)                         # 结束后清理自己刚写的文件
                except OSError:
                    pass
        # litterbox / uguu：调 server 已有的上传函数，拿到 URL 后 image_url_alive 回取（同 accept 口径）
        upload = srv.upload_litterbox_bytes if sid == "litterbox" else srv.upload_uguu_bytes
        url = upload(PNG_1X1, "probe-" + hex8 + ".png")
        if not url:
            return {"ok": False, "id": sid, "reason": "上传失败（服务不可用）",
                    "ms": int((time.time() - started) * 1000)}
        alive, why = srv.image_url_alive(url)
        if not alive:
            return {"ok": False, "id": sid, "reason": f"直链验活失败（{why}）",
                    "ms": int((time.time() - started) * 1000)}
        return {"ok": True, "id": sid, "url": url, "ms": int((time.time() - started) * 1000)}
    except Exception as exc:  # noqa: BLE001 - 网络/解析异常一律落成 ok:false，绝不冒 500
        return {"ok": False, "id": sid, "reason": str(exc)[:_MISSING_ENV_LIMIT],
                "ms": int((time.time() - started) * 1000)}


# ------------------------------------------------------------------ 路由

def handle(h: BaseHTTPRequestHandler, method: str, path: str) -> bool:
    """server.py 的路由钩子：认领 GET/PUT `ROUTE` 与 POST `ROUTE/test`，否则 False。

    `MULTI_USER=0` 时整条路由不存在（False → 既有链给 404），与 channel_store.handle
    完全一致；登录态是硬前置：未登录一律 401。
    """
    if not multiuser.enabled():
        return False
    if path == ROUTE and method in ("GET", "PUT"):
        _handle_prefs(h, method)
        return True
    if path == TEST_ROUTE and method == "POST":
        _handle_test(h)
        return True
    return False


def _handle_prefs(h: BaseHTTPRequestHandler, method: str) -> None:
    username = multiuser._session_user(h)
    if username is None:
        _send(h, {"error": "未登录"}, 401)
        return
    uid = multiuser.user_id_for_username(username)
    if uid is None:
        # 会话有效但用户行没了（账号被删）：按未登录处理
        _send(h, {"error": "未登录"}, 401)
        return
    try:
        if method == "GET":
            _send(h, {"mode": "remote", "selected": get_pref(uid), "sources": sources()})
            return
        payload = _body(h)
        selected = payload.get("selected")
        if selected is None:
            set_pref(uid, None)
            _send(h, {"ok": True, "selected": None})
            return
        if not isinstance(selected, str) or selected not in dict(SOURCES_META):
            raise _Bad(400, "可选值：tencent / local / litterbox / uguu / null")
        if _missing_envs(selected):
            raise _Bad(400, _unconfigured_error(selected))
        set_pref(uid, selected)
        _send(h, {"ok": True, "selected": selected})
    except _Bad as err:
        _send(h, {"error": err.msg}, err.status)
    except sqlite3.Error:
        _send(h, {"error": "图床偏好存储失败，请稍后重试。"}, 500)


def _handle_test(h: BaseHTTPRequestHandler) -> None:
    username = multiuser._session_user(h)
    if username is None:
        _send(h, {"error": "未登录"}, 401)
        return
    uid = multiuser.user_id_for_username(username)
    if uid is None:
        _send(h, {"error": "未登录"}, 401)
        return
    try:
        payload = _body(h)
        sid = payload.get("id")
        if sid is None:
            sid = get_pref(uid) or "tencent"          # 缺省 = 当前 selected，再缺省 = tencent
        if not isinstance(sid, str) or sid not in dict(SOURCES_META):
            raise _Bad(400, "可选值：tencent / local / litterbox / uguu / null")
        wait = _cooldown_hit(username)
        if wait:
            _send(h, {"error": f"测试过于频繁，请 {wait} 秒后再试"}, 429)
            return
        missing = _missing_envs(sid)
        if missing:
            # B1 探针合同（§四 ⑦）：未配置源走 ok:false（reason 点名缺 env），不是 4xx——
            # 界面靠 ok 字段承载结果；PUT 才是 400 口径。
            _send(h, {"ok": False, "id": sid, "reason": _unconfigured_error(sid)})
            return
        _send(h, _probe(sid))
    except _Bad as err:
        _send(h, {"error": err.msg}, err.status)
    except sqlite3.Error:
        _send(h, {"error": "图床偏好存储失败，请稍后重试。"}, 500)
