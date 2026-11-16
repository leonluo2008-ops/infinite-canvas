"""S2-B1：渠道服务端持久化（SQLite user_channels + AES-GCM 加解密 + CRUD 路由）。

红线（任务书 v1.2 §二 B1）
--------------------------
* **单人零改动**：`MULTI_USER=0` 时本模块的任何函数都不会被路由调用——不 import
  cryptography、不读 `WANREPRO_CHANNEL_KEY`（S0/单人包零第三方依赖）。
* **零兜底**：主密钥缺失/非法 → 503 + 明确文案，绝不静默降级成明文或空 key。
* **读接口零回显**：GET 只回 `***` + 末 4 位，服务端不留明文、不下发明文。
* **id 由客户端生成原样保存**：前端 nanoid 已嵌进模型引用串 `channelId::model`，
  服务端不得重发自己的 id。
* **masked 回写护栏**：前端把展示用的 masked 值原样 PUT 回来时，必须按
  id（优先）→ name+baseUrl（兜底）匹配旧行沿用原密文，不得覆盖成「***cdef」这种垃圾密文。
* **SSRF**：base_url 一律 https，解析出的 IP 拒绝 loopback/私网/link-local/保留段（含 IPv6），
  localhost 无例外；`apiFormat=wan` 的渠道（万相 CLI bridge，同源零 key）不入服务端渠道表。
"""
from __future__ import annotations

import base64
import ipaddress
import json
import os
import socket
import sqlite3
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler

try:  # 仓根包形态（server/探针导入）
    from wanrepro.ui import multiuser
except ImportError:  # 脚本直跑形态（ui/ 在 sys.path[0]）
    import multiuser  # type: ignore[no-redef]

# ------------------------------------------------------------------ 常量

ROUTE = "/api/user/channels"
METHODS = ("GET", "PUT", "DELETE")

KEY_ENV = "WANREPRO_CHANNEL_KEY"   # base64(32 字节)，systemd EnvironmentFile 注入，权限 600
KEY_BYTES = 32
VERSION = 1                        # 密文版本 = 密钥版本（轮换语义后置）
NONCE_BYTES = 12
TAG_BYTES = 16

# 本地/内网判定：这些渠道 key 只能由服务端代转发持有，服务端不接受指向内网的 base_url
BLOCKED_HOSTNAMES = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS user_channels(
  id TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  name TEXT NOT NULL,
  base_url TEXT NOT NULL,
  api_key_enc BLOB,
  api_format TEXT NOT NULL,
  models_json TEXT,
  updated_at INTEGER,
  UNIQUE(user_id, name)
);
CREATE INDEX IF NOT EXISTS idx_user_channels_user ON user_channels(user_id);
"""


class Reject(Exception):
    """带 HTTP 状态码的拒绝（422 校验失败 / 503 主密钥不可用）。"""

    def __init__(self, status: int, msg: str) -> None:
        super().__init__(msg)
        self.status = status
        self.msg = msg


# ------------------------------------------------------------------ 存储

def connect() -> sqlite3.Connection:
    """沿用 multiuser 的库与 PRAGMA 惯例（WAL + busy_timeout + foreign_keys）。

    注意：连接显式关掉 sqlite3 的隐式事务（isolation_level=None），PUT 的
    全量替换要自己包 `BEGIN IMMEDIATE`——否则 executescript/DDL 与业务写会混在
    两个事务里，中途 422 回滚不干净。
    """
    path = multiuser.db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    conn.commit()
    conn.isolation_level = None
    return conn


# ------------------------------------------------------------------ 主密钥 / 加解密

def master_key() -> bytes:
    """读并校验主密钥；任何缺失/非法都抛 503。

    刻意不做进程内缓存：轮换（重启后换 env）与探针的多阶段切换都以 env 为准，
    一次 base64 解码的成本可以忽略。
    """
    raw = (os.environ.get(KEY_ENV) or "").strip()
    if not raw:
        raise Reject(503, "服务端未配置渠道主密钥（WANREPRO_CHANNEL_KEY），渠道配置暂不可用。"
                          "请在服务端环境变量里注入 base64 编码的 32 字节密钥后重启。")
    try:
        key = base64.b64decode(raw, validate=True)
    except (ValueError, TypeError):
        raise Reject(503, "WANREPRO_CHANNEL_KEY 不是合法的 base64，渠道配置暂不可用。") from None
    if len(key) != KEY_BYTES:
        raise Reject(503, f"WANREPRO_CHANNEL_KEY 必须是 base64 编码的 32 字节密钥"
                          f"（当前解出 {len(key)} 字节），渠道配置暂不可用。")
    return key


def _aesgcm(key: bytes):
    """懒加载 cryptography：MULTI_USER=0 的进程永远走不到这里。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    return AESGCM(key)


def encrypt(key: bytes, plaintext: str) -> bytes:
    """密文格式 [1 字节版本][12B nonce][ciphertext+16B tag]。"""
    nonce = os.urandom(NONCE_BYTES)
    body = _aesgcm(key).encrypt(nonce, plaintext.encode("utf-8"), None)
    return bytes([VERSION]) + nonce + body


def decrypt(key: bytes, blob: bytes | None) -> str:
    if not blob:
        return ""
    blob = bytes(blob)
    if len(blob) < 1 + NONCE_BYTES + TAG_BYTES:
        raise Reject(500, "渠道密文已损坏（长度不足），请重新录入 API Key。")
    version, nonce, body = blob[0], blob[1:1 + NONCE_BYTES], blob[1 + NONCE_BYTES:]
    if version != VERSION:
        raise Reject(503, f"渠道密文版本 {version} 不受支持（当前主密钥版本 {VERSION}），请重新录入 API Key。")
    try:
        return _aesgcm(key).decrypt(nonce, body, None).decode("utf-8")
    except ValueError:  # InvalidTag
        raise Reject(503, "渠道密文无法用当前主密钥解密（密钥被轮换或密文被改动），请重新录入 API Key。") from None


def mask(plaintext: str) -> str:
    """展示用掩码：`***` + 末 4 位。空 key 回空串（wan 型/无 key 渠道本来就没有）。"""
    if not plaintext:
        return ""
    return "***" + plaintext[-4:]


# ------------------------------------------------------------------ SSRF 校验

def _blocked(addr) -> bool:
    if addr.version == 6 and addr.ipv4_mapped is not None:
        return _blocked(addr.ipv4_mapped)
    return (addr.is_loopback or addr.is_private or addr.is_link_local
            or addr.is_reserved or addr.is_multicast or addr.is_unspecified)


def validate_base_url(raw: str) -> str:
    """注册/更新渠道时的 SSRF 收口：https + 解析 IP 全部落在公网。

    审计 B-04：**删除 localhost 例外**——服务端代转发渠道一律 https 且拒绝内网，
    两端（前端提示/服务端校验）口径一致。
    """
    url = (raw or "").strip()
    if not url:
        raise Reject(422, "渠道接口地址（baseUrl）必填。")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme.lower() != "https":
        raise Reject(422, f"渠道接口地址必须是 https:// 开头（服务端代转发不接受明文 http）：{url}")
    host = (parsed.hostname or "").strip().strip("[]").lower()
    if not host:
        raise Reject(422, f"渠道接口地址缺少主机名：{url}")
    if host in BLOCKED_HOSTNAMES or host.endswith(".localhost"):
        raise Reject(422, f"渠道接口地址不能指向本机（{host}）：服务端不接受 localhost 渠道。")
    try:
        infos = socket.getaddrinfo(host, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise Reject(422, f"渠道接口地址的域名无法解析：{host}") from None
    except (UnicodeError, ValueError):
        raise Reject(422, f"渠道接口地址的域名不合法：{host}") from None
    if not infos:
        raise Reject(422, f"渠道接口地址的域名无法解析：{host}")
    seen: set[str] = set()
    for info in infos:
        ip = str(info[4][0]).split("%")[0]
        if ip in seen:
            continue
        seen.add(ip)
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            raise Reject(422, f"渠道接口地址解析出非法 IP：{ip}") from None
        if _blocked(addr):
            raise Reject(422, f"渠道接口地址解析到受限地址 {ip}：服务端拒绝保存本机/内网/链路本地/保留段渠道。")
    return url


# ------------------------------------------------------------------ 输出

def _send(h: BaseHTTPRequestHandler, obj: dict, status: int = 200,
          headers: dict[str, str] | None = None) -> None:
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    h.send_response(status)
    h.send_header("Content-Type", "application/json; charset=utf-8")
    h.send_header("Content-Length", str(len(data)))
    h.send_header("Cache-Control", "no-store")
    for k, v in (headers or {}).items():
        h.send_header(k, v)
    h.end_headers()
    if h.command != "HEAD":
        h.wfile.write(data)


def _body(h: BaseHTTPRequestHandler) -> dict:
    try:
        raw = h.rfile.read(int(h.headers.get("Content-Length") or 0)) or b"{}"
        val = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise Reject(400, "请求体不是合法 JSON") from None
    if not isinstance(val, dict):
        raise Reject(400, "请求体必须是 JSON 对象")
    return val


def _view(key: bytes, row: sqlite3.Row) -> dict:
    """对外视图：key 只出 masked，零回显。"""
    try:
        models = json.loads(row["models_json"]) if row["models_json"] else []
    except (ValueError, TypeError):
        models = []
    return {
        "id": row["id"],
        "name": row["name"],
        "baseUrl": row["base_url"],
        "apiFormat": row["api_format"],
        "models": models if isinstance(models, list) else [],
        "apiKey": mask(decrypt(key, row["api_key_enc"])),
        "updatedAt": row["updated_at"] or 0,
    }


# ------------------------------------------------------------------ CRUD

def _list_channels(uid: int) -> list[dict]:
    key = master_key()
    conn = connect()
    try:
        rows = conn.execute("SELECT * FROM user_channels WHERE user_id=? ORDER BY updated_at, rowid",
                            (uid,)).fetchall()
        return [_view(key, row) for row in rows]
    finally:
        conn.close()


def _replace_channels(h: BaseHTTPRequestHandler, uid: int) -> list[dict]:
    """PUT 全量替换：BEGIN IMMEDIATE 事务内删旧插新，任何一条 422 整批回滚。"""
    key = master_key()
    payload = _body(h)
    items = payload.get("channels")
    if not isinstance(items, list):
        raise Reject(422, "channels 必须是数组（PUT 为整集全量替换）。")
    now = int(time.time())
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = {row["id"]: row for row in conn.execute("SELECT * FROM user_channels WHERE user_id=?", (uid,))}
        by_pair = {(row["name"], row["base_url"]): row for row in existing.values()}
        rows: list[tuple] = []
        names: set[str] = set()
        for idx, item in enumerate(items):
            label = f"第 {idx + 1} 个渠道"
            if not isinstance(item, dict):
                raise Reject(422, f"{label}不是对象。")
            cid = str(item.get("id") or "").strip()
            name = str(item.get("name") or "").strip()
            base_url_raw = str(item.get("baseUrl") or item.get("base_url") or "")
            api_format = str(item.get("apiFormat") or item.get("api_format") or "").strip() or "openai"
            if not cid:
                raise Reject(422, f"{label}缺 id（id 由客户端生成，服务端不重发，请勿删除该字段）。")
            if len(cid) > 128:
                raise Reject(422, f"{label}的 id 过长（最多 128 字符）。")
            if not name:
                raise Reject(422, f"{label}缺渠道名称。")
            if len(name) > 128:
                raise Reject(422, f"{label}的渠道名称过长（最多 128 字符）。")
            if name in names:
                raise Reject(422, f"渠道名称重复：{name}")
            names.add(name)
            if api_format.strip().lower() == "wan":
                # 万相 CLI bridge 走本机同源、零 key，不在服务端渠道表里（标 localOnly）。
                # 先于 SSRF 判定，避免把「仅本地模式可用」误报成「地址无法解析」。
                raise Reject(422, f"渠道「{name}」是 wan 型（万相 CLI 同源零 key），只能在本地模式使用，"
                                  f"服务端不支持保存。")
            base_url = validate_base_url(base_url_raw)
            models = item.get("models")
            models_json = json.dumps(models, ensure_ascii=False) if isinstance(models, list) else None

            submitted = item.get("apiKey", item.get("api_key"))
            submitted = "" if submitted is None else str(submitted)
            submitted = submitted.strip()

            # masked 回写护栏：空/缺省/等于本用户当前 masked 值 → 沿用原密文，不覆盖
            keep = not submitted
            old = existing.get(cid)
            fallback = by_pair.get((name, base_url))
            if not keep and old is not None:
                stored = decrypt(key, old["api_key_enc"])
                if stored and submitted == mask(stored):
                    keep = True
            elif not keep and fallback is not None:
                stored = decrypt(key, fallback["api_key_enc"])
                if stored and submitted == mask(stored):
                    keep = True
                    old = fallback
            if keep:
                if old is None or not old["api_key_enc"]:
                    raise Reject(422, f"渠道「{name}」需要重新输入完整 API Key"
                                      f"（服务端没有可沿用的密文，请重新输入完整 API Key）。")
                enc = bytes(old["api_key_enc"])
            else:
                enc = encrypt(key, submitted)

            rows.append((cid, uid, name, base_url, enc, api_format, models_json, now))
        conn.execute("DELETE FROM user_channels WHERE user_id=?", (uid,))
        if rows:
            conn.executemany(
                "INSERT INTO user_channels(id,user_id,name,base_url,api_key_enc,api_format,models_json,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?)", rows)
        conn.execute("COMMIT")
    except sqlite3.IntegrityError as err:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise Reject(409, f"渠道保存冲突（{err}）：请检查渠道名称/ID 是否重复，刷新后重试。") from None
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()
    return _list_channels(uid)


def _clear_channels(uid: int) -> None:
    master_key()                       # 渠道路由统一先校验主密钥（缺失 503，不是静默空操作）
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM user_channels WHERE user_id=?", (uid,))
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


# ------------------------------------------------------------------ 路由入口

def handle(h: BaseHTTPRequestHandler, method: str, path: str) -> bool:
    """server.py 的路由钩子：认领 `/api/user/channels` 返回 True，否则 False。

    `MULTI_USER=0` 时整条路由不存在（False → 既有链路给 404），与「单人零改动」一致；
    登录态是硬前置：未登录 GET/PUT/DELETE 一律 401（不是 404/405）。
    """
    if not multiuser.enabled():
        return False
    if path != ROUTE:
        return False
    username = multiuser._session_user(h)
    if username is None:
        _send(h, {"error": "未登录"}, 401)
        return True
    if method not in METHODS:
        _send(h, {"error": "方法不允许"}, 405, {"Allow": ", ".join(METHODS)})
        return True
    uid = multiuser.user_id_for_username(username)
    if uid is None:
        # 会话有效但用户行没了（账号被删）：按未登录处理，别把数据写到空 user_id 上
        _send(h, {"error": "未登录"}, 401)
        return True
    try:
        if method == "GET":
            _send(h, {"channels": _list_channels(uid)})
        elif method == "PUT":
            channels = _replace_channels(h, uid)
            _send(h, {"ok": True, "channels": channels})
        else:
            _clear_channels(uid)
            _send(h, {"ok": True, "channels": []})
    except Reject as err:
        _send(h, {"error": err.msg}, err.status)
    except sqlite3.Error as err:
        _send(h, {"error": f"渠道存储失败：{err}"}, 500)
    return True
