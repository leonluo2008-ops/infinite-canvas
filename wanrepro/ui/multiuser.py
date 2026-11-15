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
COOKIE = "wan_session"


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


def handle(h: BaseHTTPRequestHandler, method: str, path: str) -> bool:
    if not enabled(): return False
    if path == "/multiuser/" and method == "GET":
        page = Path(__file__).with_name("multiuser.html").read_bytes()
        h.send_response(200); h.send_header("Content-Type", "text/html; charset=utf-8")
        h.send_header("Content-Length", str(len(page))); h.end_headers(); h.wfile.write(page)
        return True
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
        _send(h, {"ok":True}, headers={"Set-Cookie": f"{COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"})
        return True
    username, password = str(body.get("username") or "").strip(), str(body.get("password") or "")
    if not username or len(username) > 128 or len(password) < 8:
        _send(h, {"error":"用户名必填，密码至少 8 位"}, 400); return True
    if path.endswith("register"):
        code = str(body.get("inviteCode") or "")
        salt = secrets.token_bytes(16)
        pwd = hashlib.scrypt(password.encode(), salt=salt, n=2**15, r=8, p=1, dklen=32, maxmem=64*1024*1024)
        try:
            with connect() as db:
                db.execute("BEGIN IMMEDIATE")
                inv = db.execute("SELECT * FROM invites WHERE code=?", (code,)).fetchone()
                now = int(time.time())
                if not inv or inv["revoked"] or inv["used_count"] >= inv["max_uses"] or (inv["expires_at"] is not None and inv["expires_at"] <= now):
                    db.rollback(); _send(h, {"error":"邀请码无效、过期、已撤销或已用尽"}, 403); return True
                db.execute("INSERT INTO users(username,pwd_hash,salt,created_at) VALUES(?,?,?,?)", (username,pwd,salt,now))
                uid = db.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()[0]
                db.execute("UPDATE invites SET used_count=used_count+1 WHERE code=?", (code,))
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
        valid = False
        if user:
            candidate = hashlib.scrypt(password.encode(), salt=user["salt"], n=2**15,r=8,p=1,dklen=32,maxmem=64*1024*1024)
            valid = hmac.compare_digest(candidate, user["pwd_hash"])
        if not valid:
            with LOCK: FAILURES[username] = times + [now]
            _send(h,{"error":"用户名或密码错误"},401); return True
        with LOCK: FAILURES.pop(username, None)
        token = secrets.token_urlsafe(32); expiry = int(now)+30*24*3600
        with connect() as db: db.execute("INSERT INTO sessions(token,user_id,expires_at) VALUES(?,?,?)",(token,user["id"],expiry))
    _send(h,{"username":username,"expiresAt":expiry},headers={"Set-Cookie":f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000"})
    return True


def route_missing(path: str) -> bool:
    return path.startswith("/api/auth/") or path.startswith("/multiuser/")
