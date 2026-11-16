"""一期账号骨架：SQLite 注册、登录与可撤销会话。"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path

DB_PATH_ENV = "WANREPRO_MULTIUSER_DB"
LOCK = threading.Lock()
FAILURES: dict[str, list[float]] = {}
REG_ATTEMPTS: dict[str, list[float]] = {}  # 注册限速（按来源 IP，3600s 窗口内 10 次）
COOKIE = "wan_session"
DUMMY_SALT = secrets.token_bytes(16)  # 未知用户也走一次等价 scrypt，抹平时序侧信道


def _cap(d: dict, limit: int = 4096) -> None:
    """限速字典封顶，防内存无界增长。"""
    if len(d) > limit:
        for k in list(d)[: len(d) - limit]:
            d.pop(k, None)


def _cookie(value: str, max_age: int) -> str:
    parts = f"{COOKIE}={value}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}"
    if os.environ.get("MULTI_USER_COOKIE_SECURE", "").strip() == "1":
        parts += "; Secure"  # HTTPS 部署时经环境变量打开
    return parts


def enabled() -> bool:
    return os.environ.get("MULTI_USER", "0").strip() == "1"


def db_path() -> Path:
    return Path(os.environ.get(DB_PATH_ENV) or Path(__file__).resolve().parents[1] / "data" / "multiuser.sqlite3")


def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript("""
      CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE,
        pwd_hash BLOB NOT NULL, salt BLOB NOT NULL, created_at INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS invites(code TEXT PRIMARY KEY, max_uses INTEGER NOT NULL,
        used_count INTEGER NOT NULL DEFAULT 0, expires_at INTEGER, revoked INTEGER NOT NULL DEFAULT 0,
        created_by TEXT, created_at INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
        expires_at INTEGER NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
    """)
    return conn


def issue_invite(max_uses: int = 1, expires_at: int | None = None, created_by: str = "") -> str:
    code = secrets.token_urlsafe(32)
    with connect() as db:
        db.execute("INSERT INTO invites(code,max_uses,expires_at,created_by,created_at) VALUES(?,?,?,?,?)",
                   (code, max_uses, expires_at, created_by, int(time.time())))
    return code


def _send(h: BaseHTTPRequestHandler, obj: dict, status: int = 200, headers: dict | None = None) -> None:
    data = json.dumps(obj, ensure_ascii=False).encode()
    h.send_response(status)
    h.send_header("Content-Type", "application/json; charset=utf-8")
    h.send_header("Content-Length", str(len(data)))
    h.send_header("Cache-Control", "no-store")
    for k, v in (headers or {}).items(): h.send_header(k, v)
    h.end_headers()
    h.wfile.write(data)


def _body(h: BaseHTTPRequestHandler) -> dict:
    try:
        val = json.loads(h.rfile.read(int(h.headers.get("Content-Length") or 0)) or b"{}")
        return val if isinstance(val, dict) else {}
    except (ValueError, UnicodeDecodeError):
        return {}


def _token(h: BaseHTTPRequestHandler) -> str:
    for item in (h.headers.get("Cookie") or "").split(";"):
        k, _, v = item.strip().partition("=")
        if k == COOKIE: return v
    return ""


def _session_user(h: BaseHTTPRequestHandler) -> str | None:
    """有效会话返回用户名，否则 None（登录态守卫用）。"""
    with connect() as db:
        row = db.execute("SELECT u.username FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=? AND s.revoked=0 AND s.expires_at>?", (_token(h), int(time.time()))).fetchone()
    return row["username"] if row else None


def handle(h: BaseHTTPRequestHandler, method: str, path: str) -> bool:
    if not enabled(): return False
    if path == "/multiuser/" and method == "GET":
        page = Path(__file__).with_name("multiuser.html").read_bytes()
        h.send_response(200); h.send_header("Content-Type", "text/html; charset=utf-8")
        h.send_header("Content-Length", str(len(page))); h.end_headers(); h.wfile.write(page)
        return True
    # 登录态守卫已并入 login_guard（route_get 入口处调用）
    if path not in ("/api/auth/register", "/api/auth/login", "/api/auth/logout", "/api/auth/me"):
        return False
    if method == "GET" and path == "/api/auth/me":
        with connect() as db:
            row = db.execute("SELECT u.username,s.expires_at FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=? AND s.revoked=0 AND s.expires_at>?", (_token(h), int(time.time()))).fetchone()
        _send(h, {"username": row["username"], "expiresAt": row["expires_at"]} if row else {"error":"未登录"}, 200 if row else 401)
        return True
    if method != "POST":
        _send(h, {"error":"方法不允许"}, 405); return True
    body = _body(h)
    if path == "/api/auth/logout":
        with connect() as db: db.execute("UPDATE sessions SET revoked=1 WHERE token=?", (_token(h),))
        _send(h, {"ok":True}, headers={"Set-Cookie": _cookie("", 0)})
        return True
    username, password = str(body.get("username") or "").strip(), str(body.get("password") or "")
    if not username or len(username) > 128 or len(password) < 8:
        _send(h, {"error":"用户名必填，密码至少 8 位"}, 400); return True
    if path.endswith("register"):
        # 注册限速（按来源 IP）：未认证端点不该被无限刷（含 scrypt 成本）
        ip = h.client_address[0] if h.client_address else "?"
        now_f = time.time()
        with LOCK:
            att = [t for t in REG_ATTEMPTS.get(ip, []) if now_f - t < 3600]
            REG_ATTEMPTS[ip] = att + [now_f]
            _cap(REG_ATTEMPTS)
            if len(att) >= 10:
                _send(h, {"error": "注册尝试过于频繁，请稍后再试"}, 429); return True
        now = int(time.time())
        salt = secrets.token_bytes(16)
        pwd = hashlib.scrypt(password.encode(), salt=salt, n=2**15, r=8, p=1, dklen=32, maxmem=64*1024*1024)
        try:
            with connect() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("INSERT INTO users(username,pwd_hash,salt,created_at) VALUES(?,?,?,?)", (username,pwd,salt,now))
                uid = db.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()[0]
                token = secrets.token_urlsafe(32); expiry = now + 30*24*3600
                db.execute("INSERT INTO sessions(token,user_id,expires_at) VALUES(?,?,?)", (token,uid,expiry))
                db.commit()
        except sqlite3.IntegrityError:
            _send(h, {"error":"用户名已存在"}, 409); return True
    else:
        now = time.time()
        with LOCK:
            times = [t for t in FAILURES.get(username, []) if now-t < 600]
            if len(times) >= 5:
                FAILURES[username] = times; _send(h,{"error":"登录失败次数过多，请 10 分钟后重试"},429); return True
        with connect() as db: user = db.execute("SELECT * FROM users WHERE username=?",(username,)).fetchone()
        # 未知用户也走一次等价 scrypt：抹平「已知用户 vs 未知用户」的耗时侧信道
        candidate = hashlib.scrypt(password.encode(), salt=(user["salt"] if user else DUMMY_SALT), n=2**15,r=8,p=1,dklen=32,maxmem=64*1024*1024)
        valid = bool(user) and hmac.compare_digest(candidate, user["pwd_hash"])
        if not valid:
            with LOCK:
                FAILURES[username] = times + [now]; _cap(FAILURES)
            _send(h,{"error":"用户名或密码错误"},401); return True
        with LOCK: FAILURES.pop(username, None)
        token = secrets.token_urlsafe(32); expiry = int(now)+30*24*3600
        with connect() as db: db.execute("INSERT INTO sessions(token,user_id,expires_at) VALUES(?,?,?)",(token,user["id"],expiry))
    _send(h,{"username":username,"expiresAt":expiry},headers={"Set-Cookie":_cookie(token, 2592000)})
    return True


def route_missing(path: str) -> bool:
    return path.startswith("/api/auth/") or path.startswith("/multiuser/")


def login_guard(h: BaseHTTPRequestHandler, method: str, path: str) -> bool:
    """MULTI_USER=1 时：未登录访问任何页面路径（非 /api、非 /multiuser）一律 302 跳登录页；
    已登录返回 False 交回既有链路——画布静态产物由 server 既有 web-dist 机制负责（复用，不另造）。"""
    if method != "GET" or path.startswith("/api/") or path.startswith("/multiuser/"):
        return False
    if _session_user(h) is not None:
        return False
    h.send_response(302); h.send_header("Location", "/multiuser/"); h.send_header("Content-Length", "0"); h.end_headers()
    return True
