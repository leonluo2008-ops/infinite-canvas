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
* **B2 代转发**（`/api/proxy/<channelId>/<path>`）：登录态 + 渠道归属校验（他人 channelId=404
  且与不存在同形）→ 解密 key → 请求头白名单（Cookie/Origin/Referer/Host/X-Forwarded-*/
  X-Real-IP 全剥，防 wan_session 外泄）→ **请求时重新解析域名、校验 IP、按该 IP 建连**
  （SNI/Host 仍是域名，IF-C 防 DNS rebinding）→ 禁跟随重定向 → 流式写回（不设
  Content-Length、逐块 flush、`Connection: close` 定界，IF-4）→ 8MB/120s/单用户并发 2 →
  日志只记 时间/方法/channelId/上游状态/耗时（零 key、零 Authorization、零 query，B-05/IF-5）。
"""
from __future__ import annotations

import base64
import http.client
import ipaddress
import json
import os
import re
import socket
import sqlite3
import ssl
import sys
import threading
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

# ---- B2 代转发 ----
PROXY_PREFIX = "/api/proxy/"
_PROXY_RE = re.compile(r"^/api/proxy/([^/]+)(/.*)?$")
PROXY_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD")

BODY_LIMIT = 8 * 1024 * 1024        # 请求体 8MB 上限
UPSTREAM_TIMEOUT = 120.0            # 上游连接/读超时 120s
CLIENT_TIMEOUT = 120.0              # 服务端 socket 读超时（IF-8：daemon_threads 无超时，慢客户端堆线程）
PER_USER_CONCURRENCY = 2            # 单用户并发上限
STREAM_CHUNK = 64 * 1024            # 流式转发块大小

# 只有这些头会被送往上游；不在白名单的一律丢弃（防 wan_session 等会话令牌外泄给第三方渠道）
PASS_HEADERS = frozenset({"content-type", "accept", "accept-encoding", "accept-language", "user-agent"})
HOP_HEADERS = frozenset({"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
                         "te", "trailer", "transfer-encoding", "upgrade"})
# 强制剥离：Cookie/Origin/Referer/Host/X-Forwarded-*/X-Real-IP + 鉴权类（key 只能由服务端注入）
STRIP_HEADERS = HOP_HEADERS | frozenset({
    "cookie", "origin", "referer", "host", "content-length", "authorization",
    "x-api-key", "x-goog-api-key", "x-real-ip", "x-forwarded-for", "x-forwarded-host",
    "x-forwarded-proto", "x-forwarded-port", "x-forwarded-server", "forwarded", "via",
    "set-cookie", "expect", "range",
})

_ACTIVE: dict[int, int] = {}
_ACTIVE_LOCK = threading.Lock()

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
    except Exception as err:  # noqa: BLE001 - InvalidTag 不是 ValueError，必须显式识别
        from cryptography.exceptions import InvalidTag
        if isinstance(err, (ValueError, InvalidTag)):
            # InvalidTag = 密文被改动或主密钥已轮换；UnicodeDecodeError = 解出的不是文本
            raise Reject(503, "渠道密文无法用当前主密钥解密（密钥被轮换或密文被改动），请重新录入 API Key。") from None
        raise


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


BROKEN_MASK = "***?"

_PRESET_NAMES_CACHE: set[str] | None = None


def _preset_names() -> set[str]:
    """预置渠道名称集合（进程内缓存，模板变更需重启）。失败时不写缓存，下次重读。"""
    global _PRESET_NAMES_CACHE
    if _PRESET_NAMES_CACHE is None:
        try:
            from wanrepro.ui import channel_seed  # 仓根包形态（探针/测试导入）
        except ImportError:
            import channel_seed  # type: ignore[no-redef]  # 脚本直跑形态：ui/ 平级导入
        _PRESET_NAMES_CACHE = channel_seed.preset_names()
    return _PRESET_NAMES_CACHE


def _view(key: bytes, row: sqlite3.Row) -> dict:
    """对外视图：key 只出 masked，零回显。

    IF-A（B1 审计补充合同）：**单行密文损坏/主密钥轮换不得拖死整页 GET**——只把这一行
    的 key 降级成 `***?` 并标 `keyBroken`，其余健康渠道照常返回（否则一条坏行 = 整页 500，
    用户连「重新录入」的入口都点不到）。降级值不含任何密文/明文片段。
    """
    try:
        models = json.loads(row["models_json"]) if row["models_json"] else []
    except (ValueError, TypeError):
        models = []
    broken = False
    try:
        masked = mask(decrypt(key, row["api_key_enc"]))
    except Reject:
        masked, broken = BROKEN_MASK, True
    view = {
        "id": row["id"],
        "name": row["name"],
        "baseUrl": row["base_url"],
        "apiFormat": row["api_format"],
        "models": models if isinstance(models, list) else [],
        "apiKey": masked,
        "updatedAt": row["updated_at"] or 0,
        "isPreset": (row["name"] or "").strip() in _preset_names(),
    }
    if broken:
        view["keyBroken"] = True
    return view


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
                if old is None:
                    raise Reject(422, f"渠道「{name}」需要重新输入完整 API Key"
                                      f"（服务端没有可沿用的密文，请重新输入完整 API Key）。")
                # 预置渠道出厂无 key（密文 b""）：空 key 行回写属正常（未填 Key 的中性状态），
                # 只要服务端已有该行就沿用原密文，不因为「没有密文可沿用」而整批 422。
                enc = bytes(old["api_key_enc"] or b"")
            else:
                enc = encrypt(key, submitted)

            rows.append((cid, uid, name, base_url, enc, api_format, models_json, now))
        # 预置渠道不可删：全量替换语义下，payload 里没带的预置行（按名称判定）原样保留，
        # 否则「前端不提交无 key 渠道 → PUT 整集替换 → 三条预置被删」会让所有者规则失效。
        incoming = {row[2] for row in rows}          # row[2] = name
        incoming_ids = {row[0] for row in rows}      # row[0] = id（同 id 改名不算「缺失」）
        preset = _preset_names()
        if preset:
            keep_rows = []
            for old_row in existing.values():
                nm = (old_row["name"] or "").strip()
                if nm in preset and nm not in incoming and old_row["id"] not in incoming_ids:
                    keep_rows.append((old_row["id"], uid, old_row["name"], old_row["base_url"],
                                      bytes(old_row["api_key_enc"] or b""), old_row["api_format"],
                                      old_row["models_json"], old_row["updated_at"]))
            rows.extend(keep_rows)
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
        # IF-B（B1 审计补充合同）：409 文案必须**泛化**——带 sqlite 原文（"UNIQUE constraint
        # failed: user_channels.id"）等于给攻击者一个 channelId 存在性探针。
        raise Reject(409, "渠道保存冲突：渠道名称或 ID 已被占用，请刷新页面后重试。") from None
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


# ------------------------------------------------------------------ 代转发（B2）

class _Replied(Exception):
    """响应已经写回（早退路径）：只带状态码给日志用，不再重复写。"""

    def __init__(self, status: int) -> None:
        super().__init__(str(status))
        self.status = status


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """按**已校验的 IP** 建连，SNI/证书校验/Host 头仍用原域名。

    IF-C（B1 审计补充合同，硬性）：`validate_base_url` 保存时解析出的 IP **不落库**，
    所以代转发必须在请求时自行解析域名、校验 IP，然后**按解析出的 IP 建连**——否则
    DNS rebinding 的 TOCTOU 窗口仍然敞开（第一次解析是公网、第二次解析变 169.254.x）。
    `server_hostname=self.host` 保证 TLS 证书仍按域名校验、Host 头仍是域名。
    """

    def __init__(self, host: str, port: int, ip: str, timeout: float,
                 context: ssl.SSLContext) -> None:
        super().__init__(host, port, timeout=timeout, context=context)
        self._pinned_ip = ip

    def connect(self) -> None:  # type: ignore[override]
        sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


def _pinned_connection(host: str, port: int, ip: str):
    """建连工厂（探针可替换，用于把「按 IP 建连」换成指向本地 echo 上游）。"""
    return _PinnedHTTPSConnection(host, port, ip, UPSTREAM_TIMEOUT, ssl.create_default_context())


def _resolve_pinned(host: str, port: int) -> tuple[str, list[str]]:
    """请求时重新解析域名 + 逐个校验 IP，返回 (首选 IP, 全部合法 IP)。

    fail-closed：解析出的**任一**地址落在受限段就整条拒绝（与 `validate_base_url` 同口径）。
    """
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise Reject(502, f"渠道域名无法解析：{host}") from None
    except (UnicodeError, ValueError):
        raise Reject(502, "渠道域名不合法。") from None
    ips: list[str] = []
    for info in infos:
        ip = str(info[4][0]).split("%")[0]
        if ip in ips:
            continue
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            raise Reject(502, "渠道域名解析出非法 IP。") from None
        if _blocked(addr):
            raise Reject(403, f"渠道地址解析到受限地址 {ip}：服务端拒绝代转发到本机/内网/链路本地/保留段"
                              f"（DNS rebinding 防线）。")
        ips.append(ip)
    if not ips:
        raise Reject(502, f"渠道域名无法解析：{host}")
    return ips[0], ips


# ---- 请求头白名单 / 日志 -------------------------------------------------

def _upstream_headers(h: BaseHTTPRequestHandler) -> dict[str, str]:
    """只透传 Content-Type/Accept/Accept-Encoding/Accept-Language/UA + x-* 自定义业务头。

    B-05：wan_session 是 HttpOnly，但它照样随请求头外发——Cookie/Origin/Referer/Host/
    X-Forwarded-*/X-Real-IP 必须**强制剥离**，否则会话令牌直接交给第三方渠道。
    Accept-Encoding 收口成 identity：压缩会把 SSE 事件流攒成整块，逐块到达就没了。
    """
    out: dict[str, str] = {}
    for name, value in h.headers.items():
        low = name.lower()
        if low in STRIP_HEADERS:
            continue
        if low not in PASS_HEADERS and not low.startswith("x-"):
            continue
        if low == "accept-encoding":
            continue
        out[name] = value
    out["Accept-Encoding"] = "identity"
    return out


def _quiet(h: BaseHTTPRequestHandler) -> None:
    """实例级遮蔽 log_request/log_message：基线会打完整 self.path（含 query）。

    IF-5/B-05：不覆盖则 V10 恒假。实例属性只在本次代转发期间生效（finally 里摘掉），
    不影响同连接后续的其它端点。
    """
    h.log_request = lambda *a, **k: None  # type: ignore[method-assign]
    h.log_message = lambda *a, **k: None  # type: ignore[method-assign]


def _unquiet(h: BaseHTTPRequestHandler) -> None:
    h.__dict__.pop("log_request", None)
    h.__dict__.pop("log_message", None)


def _proxy_log(method: str, cid: str, status: int, elapsed: float) -> None:
    """只记 时间/方法/channelId/上游状态/耗时：零 key、零 Authorization、零 URL query、零路径。"""
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    sys.stderr.write(f"[proxy] {ts} {method} {cid or '-'} {status or '-'} {elapsed:.2f}s\n")


# ---- 并发闸 --------------------------------------------------------------

def _acquire_slot(uid: int) -> bool:
    with _ACTIVE_LOCK:
        cur = _ACTIVE.get(uid, 0)
        if cur >= PER_USER_CONCURRENCY:
            return False
        _ACTIVE[uid] = cur + 1
        return True


def _release_slot(uid: int) -> None:
    with _ACTIVE_LOCK:
        cur = _ACTIVE.get(uid, 1)
        if cur <= 1:
            _ACTIVE.pop(uid, None)
        else:
            _ACTIVE[uid] = cur - 1


# ---- 流式写回 ------------------------------------------------------------

def _write_stream(h: BaseHTTPRequestHandler, resp: http.client.HTTPResponse) -> None:
    """SSE/流式保真：不设 Content-Length，逐块 read+write+flush。

    IF-4：server.py 的 `send_body` 恒发 Content-Length，流会被挂死——这条路径自己写响应头。
    上游没给长度（chunked/SSE）时用 `Connection: close` 定界（HTTP/1.1 下合法的无长度方案），
    有长度就原样透传并保留 keep-alive。
    """
    length = (resp.getheader("Content-Length") or "").strip()
    passthrough: list[tuple[str, str]] = []
    for name, value in resp.getheaders():
        low = name.lower()
        if low in HOP_HEADERS or low in ("content-length", "set-cookie", "date", "server",
                                        "keep-alive", "upgrade"):
            continue          # set-cookie 也剥：上游不得在服务端域上覆盖 wan_session
        passthrough.append((name, value))
    h.send_response(resp.status, resp.reason)
    for name, value in passthrough:
        h.send_header(name, value)
    if length.isdigit():
        h.send_header("Content-Length", length)
    else:
        h.send_header("Connection", "close")
        h.close_connection = True
    h.send_header("Cache-Control", "no-store")
    h.send_header("X-Accel-Buffering", "no")     # 反代（nginx）不得缓冲事件流
    h.end_headers()
    if h.command == "HEAD":
        return
    while True:
        try:
            chunk = resp.read1(STREAM_CHUNK)
        except (socket.timeout, OSError, http.client.HTTPException):
            break        # 上游中途断/超时：头已发出，只能收尾（客户端看到流结束）
        if not chunk:
            break
        h.wfile.write(chunk)
        h.wfile.flush()


# ---- 转发主体 ------------------------------------------------------------

def _find_channel(uid: int, cid: str) -> sqlite3.Row | None:
    """只查**该用户**的渠道：他人 channelId 与不存在同形（404，不泄露存在性）。"""
    conn = connect()
    try:
        return conn.execute("SELECT * FROM user_channels WHERE id=? AND user_id=?",
                            (cid, uid)).fetchone()
    finally:
        conn.close()


def _read_request_body(h: BaseHTTPRequestHandler) -> bytes:
    te = (h.headers.get("Transfer-Encoding") or "").strip().lower()
    if te:
        h.close_connection = True     # 请求体没读，keep-alive 会错帧
        raise Reject(411, "代转发需要 Content-Length（不支持分块请求体）。")
    raw_len = (h.headers.get("Content-Length") or "").strip()
    if not raw_len:
        return b""
    try:
        length = int(raw_len)
    except ValueError:
        raise Reject(400, "Content-Length 非法。") from None
    if length < 0:
        raise Reject(400, "Content-Length 非法。")
    if length > BODY_LIMIT:
        h.close_connection = True     # 不读这么大的体，直接断（keep-alive 会错帧）
        raise Reject(413, f"请求体超过 {BODY_LIMIT // (1024 * 1024)}MB 上限。")
    if not length:
        return b""
    try:
        body = h.rfile.read(length)
    except socket.timeout:
        raise Reject(408, "读取请求体超时。") from None
    if len(body) != length:
        raise Reject(400, "请求体不完整。")
    return body


def _forward(h: BaseHTTPRequestHandler, method: str, uid: int, cid: str, subpath: str) -> int:
    row = _find_channel(uid, cid)
    if row is None:
        _send(h, {"error": "没有这个渠道"}, 404)
        raise _Replied(404)
    key = master_key()                       # 缺失/非法 → 503（零兜底）
    try:
        plaintext = decrypt(key, row["api_key_enc"])
    except Reject as err:
        # 单渠道密文坏了只坏这一条：文案不含密文/明文片段
        raise Reject(err.status, "该渠道的 API Key 无法解密（密文损坏或主密钥已轮换），请重新录入完整 API Key。") from None
    if not plaintext:
        raise Reject(409, "该渠道没有可用的 API Key，请重新录入完整 API Key。")

    api_format = (row["api_format"] or "").strip().lower()
    base = urllib.parse.urlsplit(row["base_url"] or "")
    if base.scheme.lower() != "https":
        raise Reject(502, "渠道接口地址必须是 https，服务端拒绝代转发。")
    host = (base.hostname or "").strip().lower()
    if not host:
        raise Reject(502, "渠道接口地址缺少主机名。")
    port = base.port or 443

    body = _read_request_body(h)             # 8MB 上限（超限 413，先于读体判定）
    ip, ips = _resolve_pinned(host, port)    # IF-C：请求时解析 + 校验 + 按 IP 建连

    target = (base.path.rstrip("/") + "/" + subpath.lstrip("/")) or "/"
    if not target.startswith("/"):
        target = "/" + target
    query = urllib.parse.urlparse(h.path).query
    hdrs = _upstream_headers(h)
    if api_format == "gemini":
        query = (query + "&" if query else "") + "key=" + urllib.parse.quote(plaintext, safe="")
    else:
        hdrs["Authorization"] = "Bearer " + plaintext
    url_target = target + ("?" + query if query else "")

    resp = None
    conn = None
    last_err: BaseException | None = None
    try:
        for candidate in ips:
            c = _pinned_connection(host, port, candidate)
            try:
                c.request(method, url_target, body=body if body else None, headers=hdrs)
                resp = c.getresponse()
                conn = c
                break
            except (OSError, http.client.HTTPException) as err:
                last_err = err
                try:
                    c.close()
                except Exception:  # noqa: BLE001 - 关掉坏连接，异常无意义
                    pass
        if resp is None:
            if isinstance(last_err, socket.timeout):
                raise Reject(504, "上游渠道响应超时（120s），请稍后重试。")
            raise Reject(502, "无法连接上游渠道，请检查渠道接口地址与网络。")
        # 禁重定向：http.client 不跟随 3xx，状态/头/体原样透传
        _write_stream(h, resp)
        return resp.status
    finally:
        for obj in (resp, conn):
            if obj is not None:
                try:
                    obj.close()
                except Exception:  # noqa: BLE001
                    pass


def _handle_proxy(h: BaseHTTPRequestHandler, raw_cid: str, subpath: str) -> None:
    started = time.time()
    cid = urllib.parse.unquote(raw_cid)[:128]
    method = (h.command or "GET").upper()
    status = 0
    _quiet(h)
    prev_timeout: float | None = None
    slot_uid: int | None = None
    try:
        try:                                  # IF-8：daemon_threads 无超时，慢客户端堆线程
            prev_timeout = h.connection.gettimeout()
            h.connection.settimeout(CLIENT_TIMEOUT)
        except OSError:
            prev_timeout = None
        if method not in PROXY_METHODS:
            _send(h, {"error": "方法不允许"}, 405, {"Allow": ", ".join(PROXY_METHODS)})
            raise _Replied(405)
        username = multiuser._session_user(h)
        if username is None:
            _send(h, {"error": "未登录"}, 401)
            raise _Replied(401)
        uid = multiuser.user_id_for_username(username)
        if uid is None:
            _send(h, {"error": "未登录"}, 401)
            raise _Replied(401)
        if not _acquire_slot(uid):
            _send(h, {"error": f"该账号同时进行的生成请求已达上限（{PER_USER_CONCURRENCY}），请稍后再试。"}, 429)
            raise _Replied(429)
        slot_uid = uid
        status = _forward(h, method, uid, cid, subpath)
    except _Replied as done:
        status = done.status
    except Reject as err:
        status = err.status
        _send(h, {"error": err.msg}, err.status)
    except sqlite3.Error:
        status = 500
        _send(h, {"error": "渠道存储失败"}, 500)
    finally:
        if slot_uid is not None:
            _release_slot(slot_uid)
        if prev_timeout is not None:
            try:
                h.connection.settimeout(prev_timeout)
            except OSError:
                pass
        _unquiet(h)
        _proxy_log(method, cid, status, time.time() - started)


# ------------------------------------------------------------------ 路由入口

def handle(h: BaseHTTPRequestHandler, method: str, path: str) -> bool:
    """server.py 的路由钩子：认领 `/api/user/channels` 与 `/api/proxy/...`，否则 False。

    `MULTI_USER=0` 时整条路由不存在（False → 既有链路给 404），与「单人零改动」一致；
    登录态是硬前置：未登录一律 401（不是 404/405）；他人 channelId 一律 404。
    """
    if not multiuser.enabled():
        return False
    if path == ROUTE:
        _handle_channels(h, method, path)
        return True
    m = _PROXY_RE.match(path)
    if not m:
        return False
    _handle_proxy(h, m.group(1), m.group(2) or "")
    return True


def _handle_channels(h: BaseHTTPRequestHandler, method: str, path: str) -> None:
    username = multiuser._session_user(h)
    if username is None:
        _send(h, {"error": "未登录"}, 401)
        return
    if method not in METHODS:
        _send(h, {"error": "方法不允许"}, 405, {"Allow": ", ".join(METHODS)})
        return
    uid = multiuser.user_id_for_username(username)
    if uid is None:
        # 会话有效但用户行没了（账号被删）：按未登录处理，别把数据写到空 user_id 上
        _send(h, {"error": "未登录"}, 401)
        return
    try:
        if method == "GET":
            # S4 种子化：名下渠道为 0 → 种入预置三渠道（无 key）。只播一次，用户删掉不回灌（PUT 即为真实意图）。
            try:
                try:
                    from wanrepro.ui import channel_seed  # 仓根包形态（探针/测试导入）
                except ImportError:
                    import channel_seed  # type: ignore[no-redef]  # 脚本直跑形态：ui/ 平级导入
                channel_seed.seed_if_empty(uid)
            except Exception:
                pass  # 种子失败不阻塞读取（下次 GET 再试）
            _send(h, {"channels": _list_channels(uid)})
        elif method == "PUT":
            channels = _replace_channels(h, uid)
            _send(h, {"ok": True, "channels": channels})
        else:
            _clear_channels(uid)
            _send(h, {"ok": True, "channels": []})
    except Reject as err:
        _send(h, {"error": err.msg}, err.status)
    except sqlite3.Error:
        # 存储异常文案不带 sqlite 原文（IF-B 同口径：别给存在性/结构探针）
        _send(h, {"error": "渠道存储失败，请稍后重试。"}, 500)
