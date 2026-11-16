from __future__ import annotations
import base64, contextlib, http.client, http.cookiejar, io, json, os, socket, sqlite3, sys, tempfile, threading, time
import urllib.request
from urllib.error import HTTPError
from urllib.request import Request, build_opener, HTTPCookieProcessor
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
import sys; sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
# server.py 的 TOKEN 在 import 期读 WAN_TOKEN，必须先设 env 再 import，否则 gate 断言恒真
os.environ['WAN_TOKEN']='regression-token'
# S2-B1：先立「没有主密钥 env」的基线——无 key 时 MULTI_USER=0 必须照常启动（B-03 回归）
os.environ.pop('WANREPRO_CHANNEL_KEY', None)
# S2.6：拆解链探针要自己建任务目录（.源视频指纹.json），绝不碰真实 analysis/reverse
os.environ['WANREPRO_ANALYSIS_ROOT'] = tempfile.mkdtemp(prefix='s2-6-analysis-')
from wanrepro.ui import multiuser, server, channel_store
CHANNELS='/api/user/channels'
def enc_of(db, cid):
 # 直读 SQLite 取密文原件（WAL 已提交，另一连接可见）：断言「密文没被 masked 回写覆盖」
 c=sqlite3.connect(str(db))
 try:
  r=c.execute('SELECT api_key_enc FROM user_channels WHERE id=?',(cid,)).fetchone()
  return bytes(r[0]) if r and r[0] else None
 finally: c.close()
def serve(flag, db=None):
 os.environ['MULTI_USER']=flag; os.environ['WAN_TOKEN']='regression-token'
 if db is None: fd,path=tempfile.mkstemp(); os.close(fd); os.unlink(path)
 else: path=str(db)
 os.environ[multiuser.DB_PATH_ENV]=path
 srv=ThreadingHTTPServer(('127.0.0.1',0),server.Handler); srv.daemon_threads=True; threading.Thread(target=srv.serve_forever,daemon=True).start(); return srv,'http://127.0.0.1:%d'%srv.server_port
def req(base,path,method='GET',body=None,opener=None):
 q=Request(base+path,data=json.dumps(body).encode() if body is not None else None,method=method,headers={'Content-Type':'application/json'})
 try: r=(opener or build_opener()).open(q); return r.status,r.read()
 except HTTPError as e: return e.code,e.read()
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k): return None
PUB_IP = 'https://93.184.216.34'   # 公网字面 IP：探针不依赖 DNS
PKEY = 'sk-proxy-probe-0123456789abcdef'


class _Echo(BaseHTTPRequestHandler):
    """B2 夹具：本地「上游渠道」，回显收到的头/路径/体，/sse 逐块吐事件，/slow 慢响应，/redir 3xx。"""
    protocol_version = 'HTTP/1.1'
    seen = []

    def log_message(self, *a):
        pass

    def do_GET(self): self._any()
    def do_POST(self): self._any()
    def do_PUT(self): self._any()
    def do_PATCH(self): self._any()
    def do_DELETE(self): self._any()

    def _any(self):
        n = int(self.headers.get('Content-Length') or 0)
        body = self.rfile.read(n) if n else b''
        rec = {'method': self.command, 'path': self.path,
               'headers': {k.lower(): v for k, v in self.headers.items()},
               'body': body.decode('utf-8', 'replace')}
        _Echo.seen.append(rec)
        if self.path.startswith('/sse'):
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Connection', 'close')
            self.end_headers()
            for i in range(5):
                self.wfile.write(b'data: {"i": %d}\n\n' % i)
                self.wfile.flush()
                time.sleep(0.4)
            self.close_connection = True
            return
        if self.path.startswith('/slow'):
            time.sleep(0.8)
        if self.path.startswith('/redir'):
            self.send_response(302)
            self.send_header('Location', 'https://elsewhere.invalid/x')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        payload = json.dumps(rec, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def phase_b2():
    """S2-B2：代转发 /api/proxy/<channelId>/<path>（V9/V10/V11 + 头白名单 + SSE + IF-C）。"""
    checks = []
    multiuser.FAILURES.clear(); multiuser.REG_ATTEMPTS.clear()
    _Echo.seen.clear()
    echo = ThreadingHTTPServer(('127.0.0.1', 0), _Echo); echo.daemon_threads = True
    threading.Thread(target=echo.serve_forever, daemon=True).start()
    eport = echo.server_port
    srv, base = serve('1')
    orig_pinned = channel_store._pinned_connection

    def _pin_fixture(host, port, ip):
        # 探针专用：把「按解析出的 IP + TLS/SNI 建连」换成指向本地明文 echo 上游。
        # 「按 IP 拨号、SNI/Host 仍用域名」由 _pin_dial() 结构化断言覆盖（不依赖自签证书）。
        return http.client.HTTPConnection('127.0.0.1', eport, timeout=30)

    channel_store._pinned_connection = _pin_fixture
    jarA = http.cookiejar.CookieJar(); opA = build_opener(HTTPCookieProcessor(jarA))
    jarB = http.cookiejar.CookieJar(); opB = build_opener(HTTPCookieProcessor(jarB))
    try:
        assert req(base, '/api/auth/register', 'POST', {'username': 'proxA', 'password': 'password1'}, opA)[0] == 200
        assert req(base, '/api/auth/register', 'POST', {'username': 'proxB', 'password': 'password1'}, opB)[0] == 200
        token = [c.value for c in jarA if c.name == 'wan_session'][0]
        st, b = req(base, CHANNELS, 'PUT', {'channels': [
            {'id': 'ch_p', 'name': '代理', 'baseUrl': PUB_IP, 'apiFormat': 'openai', 'models': ['m'], 'apiKey': PKEY},
            {'id': 'ch_g', 'name': '双子', 'baseUrl': PUB_IP, 'apiFormat': 'gemini', 'apiKey': PKEY + 'g'}]}, opA)
        assert st == 200, f'B2 setup channels failed: {st} {b}'

        def raw(path, method='GET', headers=None, body=None, timeout=20, send_len=True):
            c = http.client.HTTPConnection('127.0.0.1', srv.server_port, timeout=timeout)
            c.putrequest(method, path)
            for k, v in (headers or {}).items():
                c.putheader(k, v)
            if body is not None:
                c.putheader('Content-Length', str(len(body)))
            c.endheaders()
            if body:
                c.send(body)
            return c, c.getresponse()

        # --- 登录态硬前置：未登录一律 401 ---
        for m in ('GET', 'POST'):
            st, _ = req(base, '/api/proxy/ch_p/v1/models', m, {} if m == 'POST' else None)
            assert st == 401, f'anon {m} proxy must 401, got {st}'
        checks.append('proxy: unauthenticated GET/POST /api/proxy/<cid>/<path> -> 401')

        # --- V9：他人 channelId = 404，且与「不存在」同形（不泄露存在性）---
        st_a, body_a = req(base, '/api/proxy/ch_p/v1/models', opener=opB)
        st_x, body_x = req(base, '/api/proxy/no_such_channel/v1/models', opener=opB)
        assert st_a == 404 and st_x == 404, f'foreign/missing channelId must 404, got {st_a}/{st_x}'
        assert json.loads(body_a) == json.loads(body_x), 'foreign channelId must be indistinguishable from missing'
        checks.append('V9: user B proxying user A channelId -> 404, body identical to a nonexistent channelId')

        # --- V11 + 头白名单 + key 注入：Cookie/Origin/Referer/XFF/X-Real-IP 一律不上游 ---
        c, r = raw('/api/proxy/ch_p/v1/chat?foo=bar', 'POST',
                   {'Cookie': 'wan_session=' + token, 'Origin': 'https://evil.example',
                    'Referer': 'https://evil.example/p', 'X-Forwarded-For': '1.2.3.4',
                    'X-Real-IP': '1.2.3.4', 'Content-Type': 'application/json',
                    'Accept': 'application/json', 'X-Biz': 'trace-1'}, b'{"hello":1}')
        body = r.read(); c.close()
        assert r.status == 200, f'proxy POST should 200, got {r.status} {body}'
        rec = _Echo.seen[-1]; up = rec['headers']
        assert rec['method'] == 'POST' and rec['path'].startswith('/v1/chat?foo=bar'), f'path/query not forwarded: {rec["path"]}'
        assert up.get('authorization') == 'Bearer ' + PKEY, 'server must inject Authorization: Bearer <decrypted key>'
        assert up.get('content-type') == 'application/json' and up.get('x-biz') == 'trace-1', f'whitelist dropped legit headers: {up}'
        for bad in ('cookie', 'origin', 'referer', 'x-forwarded-for', 'x-real-ip'):
            assert bad not in up, f'{bad} must be stripped before hitting the upstream: {up}'
        assert token not in json.dumps(up), 'wan_session token must never reach the upstream'
        assert rec['body'] == '{"hello":1}', 'request body must be forwarded verbatim'
        checks.append('V11: proxy strips Cookie/Origin/Referer/X-Forwarded-*/X-Real-IP (no wan_session upstream), '
                      'keeps Content-Type/x-*, injects Authorization: Bearer <key>, forwards body+query')

        # --- gemini 型：key 走 ?key=，不发 Authorization ---
        c, r = raw('/api/proxy/ch_g/v1beta/models', 'GET', {'Cookie': 'wan_session=' + token})
        r.read(); c.close()
        rec = _Echo.seen[-1]
        assert r.status == 200 and 'key=' + PKEY + 'g' in rec['path'], f'gemini key not injected into query: {rec["path"]}'
        assert 'authorization' not in rec['headers'], 'gemini channel must not send Authorization header'
        checks.append('gemini-type channel injects ?key=<decrypted key> (no Authorization header)')

        # --- V10：日志零泄露（log_request/log_message 已覆写）---
        time.sleep(0.3)   # 先让前几个请求的日志行落盘，别混进本段捕获
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            c, r = raw('/api/proxy/ch_p/v1/chat?secret_query=abc', 'POST',
                       {'Cookie': 'wan_session=' + token, 'Content-Type': 'application/json'}, b'{}')
            r.read(); c.close()
            # 日志行在服务端线程的 finally 里写，可能晚于客户端读完——等它落盘再收网
            deadline = time.time() + 3
            while 'POST ch_p' not in buf.getvalue() and time.time() < deadline:
                time.sleep(0.05)
        log = buf.getvalue()
        assert r.status == 200, f'log-probe request should 200, got {r.status}'
        assert '[proxy]' in log and 'ch_p' in log, f'proxy must log channelId: {log!r}'
        for bad in (PKEY, 'Bearer', token, 'wan_session', 'secret_query', '/v1/chat'):
            assert bad not in log, f'log leaked {bad!r}: {log!r}'
        checks.append('V10: proxy log has time/method/channelId/status/duration only '
                      '(no key, no Authorization, no wan_session, no query, no path)')

        # --- V5：SSE 逐块到达（不设 Content-Length、逐块 flush；不能一次性到齐）---
        c, r = raw('/api/proxy/ch_p/sse', 'GET',
                   {'Cookie': 'wan_session=' + token, 'Accept': 'text/event-stream'}, timeout=20)
        assert r.status == 200, f'SSE should 200, got {r.status}'
        assert r.getheader('Content-Length') is None, 'SSE response must not carry Content-Length (IF-4)'
        t0 = time.time(); arrivals = []; payload = b''
        while True:
            chunk = r.read1(4096)
            if not chunk:
                break
            payload += chunk
            arrivals.append(time.time() - t0)
        c.close()
        assert payload.count(b'data:') == 5, f'SSE body incomplete: {payload!r}'
        assert len(arrivals) >= 3, f'SSE must arrive in >=3 separate chunks, got {len(arrivals)}'
        assert arrivals[-1] - arrivals[0] > 0.8, f'SSE arrived all at once ({arrivals[-1]-arrivals[0]:.2f}s) — buffered, not streamed'
        checks.append('V5: SSE streams chunk-by-chunk (%d arrivals spanning %.2fs, no Content-Length, Connection: close)'
                      % (len(arrivals), arrivals[-1] - arrivals[0]))

        # --- 禁重定向：3xx 原样透传（不跟随）---
        try:
            st_red = build_opener(NoRedirect()).open(Request(base + '/api/proxy/ch_p/redir',
                                                             headers={'Cookie': 'wan_session=' + token}))
            st_red, loc_red = st_red.status, st_red.headers.get('Location')
        except HTTPError as e:
            st_red, loc_red = e.code, e.headers.get('Location')
        assert st_red == 302 and loc_red == 'https://elsewhere.invalid/x', f'3xx must pass through verbatim, got {st_red} {loc_red}'
        checks.append('proxy does not follow redirects: upstream 302 + Location relayed verbatim')

        # --- 请求体 8MB 上限（先判长度，不读体）---
        c, r = raw('/api/proxy/ch_p/echo', 'POST',
                   {'Cookie': 'wan_session=' + token, 'Content-Length': str(8 * 1024 * 1024 + 1)})
        st_over = r.status; r.read(); c.close()
        assert st_over == 413, f'oversized body must 413, got {st_over}'
        checks.append('proxy rejects request bodies over 8MB (413)')

        # --- 单用户并发 2：第 3 个并发 429，结束后释放 ---
        out = {}
        def slow(k):
            c, r = raw('/api/proxy/ch_p/slow', 'GET', {'Cookie': 'wan_session=' + token}, timeout=30)
            out[k] = r.status; r.read(); c.close()
        threads = [threading.Thread(target=slow, args=(i,)) for i in range(2)]
        for t in threads: t.start()
        time.sleep(0.3)
        st3, _ = req(base, '/api/proxy/ch_p/slow', opener=opA)
        for t in threads: t.join()
        assert st3 == 429, f'3rd concurrent proxy for the same user must 429, got {st3}'
        assert out == {0: 200, 1: 200}, f'first two concurrent requests must both succeed: {out}'
        assert req(base, '/api/proxy/ch_p/v1/models', opener=opA)[0] == 200, 'slots must be released after the burst'
        checks.append('per-user concurrency limit 2: 3rd concurrent proxy -> 429, slots released afterwards')

        # --- IF-C：请求时重新解析 + 校验（保存时的公网 IP 不代表请求时还是）---
        real_gai = socket.getaddrinfo
        def rebind_gai(host, port, *a, **k):
            if host == '93.184.216.34':
                return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('169.254.169.254', port))]
            return real_gai(host, port, *a, **k)
        socket.getaddrinfo = rebind_gai
        try:
            st_rb, body_rb = req(base, '/api/proxy/ch_p/v1/models', opener=opA)
        finally:
            socket.getaddrinfo = real_gai
        assert st_rb == 403, f'DNS rebinding to link-local must 403 at proxy time, got {st_rb} {body_rb}'
        assert b'169.254.169.254' in body_rb, 'rejection must name the blocked address'
        checks.append('IF-C: proxy re-resolves at request time and rejects a rebound link-local IP (403)')

        # --- IF-C 结构化：connect 拨解析出的 IP，SNI/Host 仍是域名 ---
        pin = channel_store._PinnedHTTPSConnection('api.example.com', 443, '203.0.113.9', 5,
                                                   __import__('ssl').create_default_context())
        dialed = {}
        real_cc = socket.create_connection
        def fake_cc(addr, timeout=None, **k):
            dialed['addr'] = addr
            raise OSError('probe-stop')
        socket.create_connection = fake_cc
        try:
            try:
                pin.connect()
            except OSError:
                pass
        finally:
            socket.create_connection = real_cc
        assert dialed.get('addr', (None,))[0] == '203.0.113.9', f'connect must dial the resolved IP, not the hostname: {dialed}'
        assert pin.host == 'api.example.com', 'SNI/Host must stay the original domain (TLS cert still domain-checked)'
        checks.append('IF-C: _PinnedHTTPSConnection dials the resolved IP while keeping Host/SNI = channel domain')

        # --- IF-A：单行密文损坏降级，不整页 500 ---
        db = Path(os.environ[multiuser.DB_PATH_ENV])
        con = sqlite3.connect(str(db)); con.execute("UPDATE user_channels SET api_key_enc=? WHERE id='ch_g'",
                                                    (b'\x01' + b'\x00' * 40,)); con.commit(); con.close()
        st_ia, body_ia = req(base, CHANNELS, opener=opA)
        rows = json.loads(body_ia)['channels'] if st_ia == 200 else []
        broken = [r for r in rows if r['id'] == 'ch_g']
        assert st_ia == 200 and len(rows) == 2, f'one broken row must not 500 the whole list: {st_ia} {body_ia[:200]}'
        assert broken and broken[0]['apiKey'] == '***?' and broken[0].get('keyBroken') is True, f'broken row must degrade to ***?: {broken}'
        assert [r for r in rows if r['id'] == 'ch_p'][0]['apiKey'] == '***' + PKEY[-4:], 'healthy row must stay intact'
        checks.append('IF-A: a corrupted ciphertext row degrades to ***? (keyBroken) instead of 500-ing the whole GET')

    finally:
        channel_store._pinned_connection = orig_pinned
        srv.shutdown(); srv.server_close()
        echo.shutdown(); echo.server_close()
    return checks


def phase_admin():
    """S2.5（B1.2 / 判据 V7）：/api/auth/me 的 is_admin 三态 + 旧库 role 列 ALTER 迁移。

    只碰 S1 既有的 auth/me 与 users 表（最小扩展），不触碰渠道/代转发任何断言。
    """
    checks = []
    multiuser.FAILURES.clear(); multiuser.REG_ATTEMPTS.clear()
    srv, base = serve('1')
    try:
        # --- 未登录：仍 401，且响应体不携带 is_admin（匿名侧不该看到管理员口径）---
        st, body = req(base, '/api/auth/me')
        assert st == 401, f'anon /api/auth/me must stay 401, got {st}'
        assert b'is_admin' not in body, f'401 body must not carry is_admin: {body!r}'
        checks.append('V7a: anon GET /api/auth/me -> 401, body has no is_admin key')

        # --- admin 种子账号：/api/auth/me 返回 is_admin=true，且既有字段不变 ---
        opA = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
        assert req(base, '/api/auth/register', 'POST', {'username': 'owner', 'password': 'password1'}, opA)[0] == 200
        st, body = req(base, '/api/auth/me', opener=opA)
        me = json.loads(body)
        assert st == 200 and me.get('is_admin') is True, f'admin me must be is_admin=true, got {st} {me}'
        assert me.get('username') == 'owner' and isinstance(me.get('expiresAt'), int), f'me must keep username/expiresAt: {me}'

        # --- 普通账号：is_admin 显式 false（不是缺字段）---
        opB = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
        assert req(base, '/api/auth/register', 'POST', {'username': 'plain', 'password': 'password1'}, opB)[0] == 200
        st2, body2 = req(base, '/api/auth/me', opener=opB)
        me2 = json.loads(body2)
        assert st2 == 200 and me2.get('is_admin') is False, f'normal user me must be is_admin=false, got {st2} {me2}'
        assert 'is_admin' in me2, 'is_admin must be present (not omitted) for a logged-in normal user'
        checks.append('V7b: /api/auth/me is_admin = true for seeded admin (owner) / false for a plain account; username+expiresAt unchanged')

        # --- 登出后 is_admin 不得残留 ---
        assert req(base, '/api/auth/logout', 'POST', {}, opA)[0] == 200
        st3, body3 = req(base, '/api/auth/me', opener=opA)
        assert st3 == 401 and b'is_admin' not in body3, f'after logout me must be 401 w/o is_admin, got {st3} {body3!r}'
        checks.append('V7c: after logout /api/auth/me is 401 again (is_admin never leaks past the session)')
    finally:
        srv.shutdown(); srv.server_close()

    # --- 旧库迁移：S1 建表没有 role 列，connect() 必须 ALTER 补列并把 owner2 回填为 1 ---
    legacy = Path(tempfile.mkdtemp()) / 'legacy.sqlite3'
    now = int(time.time())
    c = sqlite3.connect(str(legacy))
    try:
        c.executescript("""
          CREATE TABLE users(id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE,
            pwd_hash BLOB NOT NULL, salt BLOB NOT NULL, created_at INTEGER NOT NULL);
          CREATE TABLE sessions(token TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
            expires_at INTEGER NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
        """)
        for name, tok in (('owner2', 'legacytok_admin'), ('someone', 'legacytok_plain')):
            c.execute("INSERT INTO users(username,pwd_hash,salt,created_at) VALUES(?,?,?,?)", (name, b'x', b'y', now))
            uid = c.execute("SELECT id FROM users WHERE username=?", (name,)).fetchone()[0]
            c.execute("INSERT INTO sessions(token,user_id,expires_at) VALUES(?,?,?)", (tok, uid, now + 86400))
        c.commit()
        pre_cols = {r[1] for r in c.execute("PRAGMA table_info(users)")}
    finally:
        c.close()
    assert 'role' not in pre_cols, f'legacy fixture must start without role column: {pre_cols}'

    srv, base = serve('1', db=legacy)
    try:
        def me_with(tok):
            q = Request(base + '/api/auth/me', headers={'Cookie': 'wan_session=' + tok})
            try:
                r = build_opener().open(q); return r.status, json.loads(r.read())
            except HTTPError as e:
                return e.code, json.loads(e.read())

        st_a, me_a = me_with('legacytok_admin')
        st_p, me_p = me_with('legacytok_plain')
        assert st_a == 200 and me_a['is_admin'] is True, f'legacy owner2 must be admin after ALTER backfill, got {st_a} {me_a}'
        assert st_p == 200 and me_p['is_admin'] is False, f'legacy plain user must stay non-admin (DEFAULT 0), got {st_p} {me_p}'
    finally:
        srv.shutdown(); srv.server_close()

    c = sqlite3.connect(str(legacy))
    try:
        cols = {r[1]: r for r in c.execute("PRAGMA table_info(users)")}
        roles = dict(c.execute("SELECT username, role FROM users"))
    finally:
        c.close()
    assert 'role' in cols, f'ALTER TABLE must add users.role, columns={sorted(cols)}'
    assert cols['role'][3] == 1 and cols['role'][4] == '0', f'role must be NOT NULL DEFAULT 0, got {cols["role"]}'
    assert roles == {'owner2': 1, 'someone': 0}, f'backfill must promote only seed admins, got {roles}'
    checks.append('V7d: legacy S1 DB (no role column) -> ALTER TABLE adds role INTEGER NOT NULL DEFAULT 0, backfills owner/owner2=1 (plain stays 0), me reflects it')
    return checks


def phase_s2_6():
    """S2.6：拆解（/api/lapian/run）与 SDGF（/api/sdgf/video/run）接入服务端渠道。

    判据对应任务书 §三：
      * W1 登录用户带 channelId 拆解 → 子进程收到的 JXIN_API_KEY == 该用户渠道解密 key，
        baseUrl/model 透传（argv），前端一个 key 字节都没传；
      * W2 他人 channelId → 404（与不存在同形）、未登录带 channelId → 401；
      * W3 无 channelId → 现行为逐字节一致（前端带 key 用带 key 的、否则不注入环境）；
      * W4 SDGF channelId 提交走用户 key（sdgf_create 收到的是解密后的 key）；
      * W5 响应/任务记录零 key 明文；
      * 零兜底：渠道没有密文 → 拆解 409、SDGF preflight 拒绝，绝不回落服务器 env 密钥。

    拆解子进程用 `server.run_py` 捕获点（argv/附加环境就是 run_py 交给子进程的那一份，
    与真实 spawn 同源；不真跑 analyze.py＝不花钱、不碰网络）。
    """
    checks = []
    multiuser.FAILURES.clear(); multiuser.REG_ATTEMPTS.clear()
    os.environ['WANREPRO_CHANNEL_KEY'] = base64.b64encode(bytes(range(32))).decode()
    srv, base = serve('1')
    db = Path(os.environ[multiuser.DB_PATH_ENV])
    opA = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
    opB = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
    PUB = 'https://93.184.216.34'
    KEY_A, KEY_B, KEY_LEGACY = 'sk-user-a-0001', 'sk-user-b-0002', 'sk-legacy-0003'
    real_run_py, real_create, real_query = server.run_py, server.sdgf_create, server.sdgf_query
    real_delay, real_uploads = server.SDGF_FAILED_RECHECK_DELAY, server.UPLOADS
    seen: list[dict] = []
    made: list[dict] = []
    tmp_uploads = Path(tempfile.mkdtemp(prefix='s2-6-uploads-'))
    try:
        assert req(base, '/api/auth/register', 'POST',
                   {'username': 'ua', 'password': 'password1'}, opA)[0] == 200
        assert req(base, '/api/auth/register', 'POST',
                   {'username': 'ub', 'password': 'password1'}, opB)[0] == 200
        conn = sqlite3.connect(str(db))
        uid_a = conn.execute("SELECT id FROM users WHERE username='ua'").fetchone()[0]
        conn.close()
        st, _ = req(base, CHANNELS, 'PUT', {'channels': [
            {'id': 'ch_a', 'name': 'A渠道', 'baseUrl': PUB, 'apiFormat': 'gemini',
             'models': ['vision-1', 'text-1'], 'apiKey': KEY_A}]}, opA)
        assert st == 200, st
        st, _ = req(base, CHANNELS, 'PUT', {'channels': [
            {'id': 'ch_b', 'name': 'B渠道', 'baseUrl': PUB, 'apiFormat': 'gemini',
             'models': ['vision-1'], 'apiKey': KEY_B}]}, opB)
        assert st == 200, st
        # 零兜底负向夹具：渠道行在、密文为空（api_key_enc 可空）——「渠道存在且有密文」必须被单独判死
        conn = sqlite3.connect(str(db))
        conn.execute("INSERT INTO user_channels(id,user_id,name,base_url,api_key_enc,api_format,models_json,updated_at)"
                     " VALUES(?,?,?,?,NULL,?,?,?)",
                     ('ch_nokey', uid_a, '空密文渠道', PUB, 'gemini', '["vision-1"]', int(time.time())))
        conn.commit(); conn.close()

        # ---- 拆解任务夹具（临时 ANALYSIS 根 + 仓库内可解析的 mp4 路径）----
        vid_dir = server.ROOT / 'uploads' / 'probe-s2-6'
        vid_dir.mkdir(parents=True, exist_ok=True)
        video = vid_dir / 'clip.mp4'
        video.write_bytes(b'\x00\x00\x00\x18ftypmp42' + b'\x00' * 64)
        task_id = 's26task'
        tdir = server.ANALYSIS / task_id
        tdir.mkdir(parents=True, exist_ok=True)
        (tdir / '.源视频指纹.json').write_text(
            json.dumps({'路径': video.relative_to(server.ROOT).as_posix()}, ensure_ascii=False),
            encoding='utf-8')

        def fake_run_py(args, timeout, extra_env=None):
            seen.append({'args': list(args), 'env': dict(extra_env or {})})
            return 0, '', ''
        server.run_py = fake_run_py

        def wait_job(job_id, timeout=10.0):
            end = time.time() + timeout
            while time.time() < end:
                with server.JOBS_LOCK:
                    rec = dict(server.JOBS.get(job_id) or {})
                if rec.get('status') in ('done', 'error', 'failed'):
                    return rec
                time.sleep(0.05)
            raise AssertionError(f'job {job_id} 10s 内没结束')

        # ---- W1：登录用户带 channelId 拆解 → 解密 key 注入子进程 ----
        st, body = req(base, '/api/lapian/run', 'POST',
                       {'id': task_id, 'intent': '重点看产品镜头', 'channelId': 'ch_a',
                        'model': 'vision-1'}, opA)
        blob = body.decode('utf-8')
        assert st == 200 and json.loads(blob).get('job'), f'lapian run with channelId: {st} {blob[:200]}'
        assert KEY_A not in blob and '***' not in blob, 'response must not echo any key material'
        rec = wait_job(json.loads(blob)['job'])
        assert rec.get('status') == 'done', f'analyze job should finish: {rec}'
        assert seen, 'run_py must have been called'
        got = seen[-1]
        assert got['env'].get('JXIN_API_KEY') == KEY_A, f"child env key must be A's decrypted key, got {got['env'].get('JXIN_API_KEY')!r}"
        assert '--model' in got['args'] and got['args'][got['args'].index('--model') + 1] == 'vision-1', got['args']
        assert '--base-url' in got['args'] and got['args'][got['args'].index('--base-url') + 1] == PUB + '/v1beta', got['args']
        assert KEY_A not in ' '.join(got['args']), 'key must never ride the command line (child logs it)'
        checks.append('W1 lapian channelId: child env JXIN_API_KEY == user channel decrypted key; baseUrl/model via argv; '
                      'zero key in response or argv')

        # ---- W2：他人 channelId 404（与不存在同形）、未登录 401 ----
        st_other, b_other = req(base, '/api/lapian/run', 'POST',
                               {'id': task_id, 'intent': 'x', 'channelId': 'ch_a', 'model': 'vision-1'}, opB)
        st_ghost, b_ghost = req(base, '/api/lapian/run', 'POST',
                                {'id': task_id, 'intent': 'x', 'channelId': 'ch_ghost', 'model': 'vision-1'}, opB)
        assert st_other == 404 and st_ghost == 404, f'other/ghost channelId must both 404, got {st_other}/{st_ghost}'
        assert json.loads(b_other) == json.loads(b_ghost), f'他人 id 与不存在必须同形：{b_other!r} vs {b_ghost!r}'
        st_anon, b_anon = req(base, '/api/lapian/run', 'POST',
                              {'id': task_id, 'intent': 'x', 'channelId': 'ch_a', 'model': 'vision-1'})
        assert st_anon == 401, f'anon + channelId must 401, got {st_anon}'
        assert KEY_A not in b_other.decode() + b_ghost.decode() + b_anon.decode(), 'error bodies must not leak keys'
        checks.append('W2 lapian: other user channelId 404 with identical body as unknown id; anon + channelId 401')

        # ---- W3：无 channelId 旧路径逐字节一致（登录态无关）----
        seen.clear()
        st, body = req(base, '/api/lapian/run', 'POST',
                       {'id': task_id, 'intent': 'x', 'channel': {'baseUrl': PUB, 'key': KEY_LEGACY,
                                                                  'model': 'legacy-model'}})
        assert st == 200, f'legacy lapian body must keep working anon, got {st} {body[:120]}'
        wait_job(json.loads(body)['job'])
        got = seen[-1]
        assert got['env'].get('JXIN_API_KEY') == KEY_LEGACY, got['env']
        assert got['args'][got['args'].index('--model') + 1] == 'legacy-model', got['args']
        assert got['args'][got['args'].index('--base-url') + 1] == PUB + '/v1beta', got['args']
        seen.clear()
        st, body = req(base, '/api/lapian/run', 'POST', {'id': task_id, 'intent': 'x'})
        assert st == 200, f'plain lapian body must keep working, got {st}'
        wait_job(json.loads(body)['job'])
        got = seen[-1]
        assert got['env'] == {}, f'no channel → zero extra env (server env fallback untouched), got {got["env"]}'
        assert got['args'] == ['analyze.py', video.relative_to(server.ROOT).as_posix(), '--id', task_id,
                               '--yes', '--intent', 'x'], f'argv must be byte-identical to baseline, got {got["args"]}'
        checks.append('W3 no channelId: legacy channel{baseUrl,key,model} body → same env/argv as baseline; '
                      'bare {id,intent} → zero extra env + byte-identical argv')

        # ---- 零兜底：渠道存在但没有密文 → 409（不回落 env）----
        st, body = req(base, '/api/lapian/run', 'POST',
                       {'id': task_id, 'intent': 'x', 'channelId': 'ch_nokey', 'model': 'vision-1'}, opA)
        assert st == 409 and 'API Key' in body.decode(), f'channel without ciphertext must 409 with hint, got {st} {body[:200]}'
        checks.append('zero-fallback: channelId whose channel has no ciphertext -> 409 (never falls back to server env key)')

        # ---- W4：SDGF channelId 提交走用户 key ----
        server.UPLOADS = tmp_uploads
        server.SDGF_FAILED_RECHECK_DELAY = 0.05
        server.sdgf_create = lambda api_base, key, body: (made.append(
            {'api_base': api_base, 'key': key, 'body': body}) or 'tsk-s26')
        server.sdgf_query = lambda api_base, key, task_id_: {'status': 'failed', 'progress': None,
                                                             'url': '', 'error': 'probe-forced'}
        sdgf_body = {'prompt': 'ok', 'images': ['https://cdn.example.com/ref-1.png'],
                     'duration': 8, 'ratio': '9:16', 'resolution': '720', 'audio': True,
                     'channelId': 'ch_a', 'model': 'sd2.5-720p-ap'}
        st, body = req(base, '/api/sdgf/video/run', 'POST', sdgf_body, opA)
        dry = json.loads(body)
        assert st == 200 and dry.get('dry') is True and dry['preflight']['ok'] is True, f'dry with channelId: {st} {body[:300]}'
        assert dry['preview']['url'] == PUB + '/v1/videos', dry['preview']['url']
        assert dry['preview']['headers']['Authorization'] == 'Bearer ***', dry['preview']['headers']
        assert KEY_A not in body.decode() and '***' not in body.decode().replace('Bearer ***', ''), 'dry must not echo keys'
        assert not made, 'dry must not submit'
        st, body = req(base, '/api/sdgf/video/run', 'POST', {**sdgf_body, 'confirm': True}, opA)
        assert st == 200 and json.loads(body).get('dry') is False, f'confirm with channelId: {st} {body[:200]}'
        assert made and made[0]['key'] == KEY_A, f"sdgf_create must receive A's decrypted key, got {made[:1]}"
        assert made[0]['api_base'] == PUB + '/v1', made[0]['api_base']
        assert made[0]['body'].get('model') == 'sd2.5-720p-ap' and 'apiKey' not in made[0]['body'], made[0]['body']
        rec = wait_job(json.loads(body)['job'])
        assert rec.get('status') == 'failed' and KEY_A not in str(rec.get('error') or ''), \
            f'job state must never carry the key: {rec}'
        record = server.sdgf_task_dir('tsk-s26') / server.SDGF_RECORD
        assert record.is_file(), 'task record should be written'
        assert KEY_A not in record.read_text(encoding='utf-8'), 'on-disk task record must not hold the key'
        checks.append("W4/W5 sdgf channelId: create receives the user's decrypted key + channel baseUrl; "
                      'dry preview/response/job state/on-disk record all key-free')

        # ---- SDGF：他人 404 / 未登录 401 / 空密文 preflight 拒绝 ----
        st_other, b1 = req(base, '/api/sdgf/video/run', 'POST', sdgf_body, opB)
        st_ghost, b2 = req(base, '/api/sdgf/video/run', 'POST', {**sdgf_body, 'channelId': 'ch_ghost'}, opB)
        assert st_other == 404 and st_ghost == 404 and b1 == b2, f'sdgf other/ghost must 404 identically: {st_other}/{st_ghost}'
        assert req(base, '/api/sdgf/video/run', 'POST', sdgf_body)[0] == 401, 'sdgf anon + channelId must 401'
        st, body = req(base, '/api/sdgf/video/run', 'POST', {**sdgf_body, 'channelId': 'ch_nokey'}, opA)
        no_key = json.loads(body)
        assert st == 200 and no_key['preflight']['ok'] is False, f'nokey dry must fail preflight: {st} {body[:200]}'
        assert any('密文缺失' in r for r in no_key['preflight']['reasons']), no_key['preflight']['reasons']
        made.clear()
        st, body = req(base, '/api/sdgf/video/run', 'POST',
                       {**sdgf_body, 'channelId': 'ch_nokey', 'confirm': True}, opA)
        assert st == 400 and not made, f'confirm with key-less server channel must 400 and send nothing: {st} {made}'
        checks.append('sdgf: other/unknown channelId 404 same shape, anon 401, key-less server channel preflight-refused '
                      '(zero request sent)')
    finally:
        server.run_py, server.sdgf_create, server.sdgf_query = real_run_py, real_create, real_query
        server.SDGF_FAILED_RECHECK_DELAY, server.UPLOADS = real_delay, real_uploads
        srv.shutdown(); srv.server_close()
    return checks


def main():
 checks=[]; srv,base=serve('0')
 try:
  for method,path in [('GET','/api/auth/me'),('POST','/api/auth/login'),('POST','/api/auth/register'),('POST','/api/auth/logout'),('GET','/multiuser/'),('GET','/api/auth/whatever'),('GET','/multiuser/deep/page')]: assert req(base,path,method)[0]==404
  checks.append('MULTI_USER=0 endpoints/test page/wildcard subpaths 404; WAN_TOKEN gate retained')
  # S2-B1 单人回归：渠道新路由全动词 404（不扩则 V8 恒真），且无主密钥 env 下零依赖启动
  for method in ('GET','POST','PUT','DELETE'):
   st,_=req(base,CHANNELS,method,{'channels':[]} if method in ('POST','PUT') else None)
   assert st==404, f'MULTI_USER=0 {method} {CHANNELS} must 404, got {st}'
  assert os.environ.get('WANREPRO_CHANNEL_KEY') is None
  assert 'cryptography' not in sys.modules, 'MULTI_USER=0 must not import cryptography'
  # S2-B2 单人回归：代转发端点也必须整条不存在（否则单人包凭空多一个 SSRF 面）
  for method in ('GET','POST'):
   st,_=req(base,'/api/proxy/ch_any/v1/models',method,{} if method=='POST' else None)
   assert st==404, f'MULTI_USER=0 {method} /api/proxy/... must 404, got {st}'
  checks.append('MULTI_USER=0: GET/POST/PUT/DELETE %s all 404; GET/POST /api/proxy/* 404; boots with no WANREPRO_CHANNEL_KEY; cryptography never imported'%CHANNELS)
  # WAN_TOKEN 强制探针：公网来源 XFF + 非 localhost Host，无 token 401、带 token 302（种 cookie 合同）
  pub=dict(XFF='8.8.8.8', HOST='canvas.example.com')
  q=Request(base+'/api/tasks',headers={'X-Forwarded-For':pub['XFF'],'Host':pub['HOST']})
  try: st=build_opener().open(q).status
  except HTTPError as e: st=e.code
  assert st==401, f'no-token should be 401, got {st}'
  q2=Request(base+'/api/tasks?token=regression-token',headers={'X-Forwarded-For':pub['XFF'],'Host':pub['HOST']})
  try: st2=build_opener(NoRedirect()).open(q2).status
  except HTTPError as e: st2=e.code
  assert st2==302, f'token should 302 (cookie seeding), got {st2}'
  checks.append('WAN_TOKEN enforced for public-origin requests (no-token 401, token 302)')
 finally: srv.shutdown(); srv.server_close()
 srv,base=serve('1'); op=build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
 try:
  assert req(base,'/multiuser/')[0]==200
  # 登录态守卫：未登录访问画布首页必须 302 跳登录页
  try: st_root=build_opener(NoRedirect()).open(Request(base+'/')).status
  except HTTPError as e: st_root=e.code
  assert st_root==302, f'anon / should 302 to login, got {st_root}'
  # 注册（无邀请码）-> me -> 登出 -> me 401
  assert req(base,'/api/auth/register','POST',{'username':'probe','password':'password1'},op)[0]==200
  # S2-B1 零兜底：MULTI_USER=1 且无主密钥 env 时，渠道路由必须 503 + 明确文案（不是空列表/明文降级）
  st_nokey,body_nokey=req(base,CHANNELS,opener=op)
  assert st_nokey==503, f'missing master key must 503, got {st_nokey}'
  assert b'WANREPRO_CHANNEL_KEY' in body_nokey, '503 body must name the missing env (explicit message)'
  checks.append('MULTI_USER=1 without WANREPRO_CHANNEL_KEY: GET channels 503 + explicit message (no silent fallback)')
  assert req(base,'/api/auth/me',opener=op)[0]==200; assert req(base,'/api/auth/logout','POST',{},op)[0]==200 and req(base,'/api/auth/me',opener=op)[0]==401
  checks.append('register (no invite) -> / guard -> me -> logout -> me 401')
  # 带邀请码字段注册也照常成功（字段被忽略）——向后兼容旧客户端
  assert req(base,'/api/auth/register','POST',{'username':'legacy','password':'password1','inviteCode':'whatever'},op)[0]==200
  assert req(base,'/api/auth/logout','POST',{},op)[0]==200
  checks.append('legacy inviteCode field ignored (backward compatible)')
  # 重复用户名拒绝
  op2=build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
  assert req(base,'/api/auth/register','POST',{'username':'probe','password':'password1'},op2)[0]==409
  checks.append('duplicate username rejected (409)')
  # 登录限速：5 次错密码后锁
  for _ in range(5): assert req(base,'/api/auth/login','POST',{'username':'probe','password':'wrongpass'})[0]==401
  assert req(base,'/api/auth/login','POST',{'username':'probe','password':'wrongpass'})[0]==429; checks.append('five failed logins trigger lock')
  # 注册限速：同 IP 3600s 窗口 10 次后 429
  last=0
  for i in range(12):
   last=req(base,'/api/auth/register','POST',{'username':'flood%d'%i,'password':'password1'})[0]
  assert last==429, f'register flood should end 429, got {last}'
  checks.append('register rate limit per IP triggers 429')
 finally: srv.shutdown(); srv.server_close()

 # ---------------- S2-B1：渠道加密存储 + CRUD + SSRF ----------------
 # 限速字典是进程内状态（按来源 IP），上一相的注册洪水不跨相——只清探针自己的计数，不动 S1 逻辑
 multiuser.REG_ATTEMPTS.clear(); multiuser.FAILURES.clear()
 os.environ['WANREPRO_CHANNEL_KEY']=base64.b64encode(bytes(range(32))).decode()
 srv,base=serve('1'); db=Path(os.environ[multiuser.DB_PATH_ENV])
 opA=build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
 PUB='https://93.184.216.34'  # 公网字面 IP：探针不依赖 DNS（域名解析留给真实渠道）
 KEY='sk-probe-PLAINTEXT-0123456789abcdef'; MASKED='***'+KEY[-4:]
 try:
  assert req(base,'/api/auth/register','POST',{'username':'chan','password':'password1'},opA)[0]==200
  # 登录态必需：未登录 GET/PUT/DELETE 一律 401（不是 404/405）
  for method,payload in (('GET',None),('PUT',{'channels':[]}),('DELETE',None)):
   st,_=req(base,CHANNELS,method,payload)
   assert st==401, f'anon {method} {CHANNELS} must 401, got {st}'
  checks.append('channels: unauthenticated GET/PUT/DELETE -> 401 (not 404/405)')

  def put(chans):
   st,b=req(base,CHANNELS,'PUT',{'channels':chans},opA)
   return st,(json.loads(b) if b else {})

  # SSRF（V7）：localhost 例外已删——链路本地/回环/明文 http 全拒
  for bad in ('https://169.254.169.254/latest/meta-data/','https://127.0.0.1:8779/','http://127.0.0.1:8779/'):
   st,b=put([{'id':'ch_bad','name':'bad','baseUrl':bad,'apiFormat':'openai','apiKey':KEY}])
   assert st==422, f'SSRF baseUrl {bad} must be rejected with 422, got {st} {b}'
  checks.append('SSRF: https://169.254.169.254/ , https://127.0.0.1:8779/ and plain http all rejected 422')

  # wan 型渠道（万相 CLI 同源零 key）不入服务端渠道表，标 localOnly
  st,b=put([{'id':'ch_wan','name':'万相','baseUrl':PUB,'apiFormat':'wan','apiKey':''}])
  assert st==422, f'apiFormat=wan must be rejected (localOnly), got {st} {b}'
  checks.append('apiFormat=wan channel rejected 422 (localOnly, not stored server-side)')

  CH=[{'id':'ch_1','name':'聚鑫','baseUrl':PUB,'apiFormat':'sdgf','models':['m1','m2'],'apiKey':KEY}]
  st,b=put(CH)
  assert st==200 and b['channels'][0]['apiKey']==MASKED, f'PUT should 200 + masked, got {st} {b}'
  assert KEY.encode() not in json.dumps(b).encode(), 'PUT response must not echo the plaintext key'
  # 读接口零回显（V3）
  st,body=req(base,CHANNELS,opener=opA); data=json.loads(body); ch=data['channels']
  assert st==200 and len(ch)==1 and ch[0]['apiKey']==MASKED, f'GET masked failed: {ch}'
  assert KEY.encode() not in body, 'GET channels must not contain the plaintext key'
  assert ch[0]['id']=='ch_1' and ch[0]['models']==['m1','m2'], f'client id/models must round-trip: {ch[0]}'
  checks.append('logged-in GET/PUT channels expose masked ***last4 only (client id/models round-trip, zero echo)')

  # 盘上零明文（V2）：主库 + WAL 都不含 key 明文
  blob=b''.join(p.read_bytes() for p in (db, Path(str(db)+'-wal')) if p.is_file())
  assert KEY.encode() not in blob and b'0123456789abcdef' not in blob, 'SQLite (db + WAL) must not hold the plaintext key'
  checks.append('SQLite hexdump (db + -wal) holds no plaintext key (AES-GCM at rest, V2)')

  enc1=enc_of(db,'ch_1')
  assert enc1 and enc1[0]==1 and len(enc1)==1+12+len(KEY.encode())+16, f'ciphertext layout must be [1B ver][12B nonce][ct+16B tag]: {enc1!r}'
  checks.append('ciphertext layout = [1B version=1][12B nonce][ciphertext+16B tag]')
  # masked 回写护栏：前端把展示值原样 PUT 回来，密文必须原样保留（不得覆盖成 "***cdef"）
  st,b=put([{'id':'ch_1','name':'聚鑫','baseUrl':PUB,'apiFormat':'sdgf','models':['m1','m2'],'apiKey':MASKED}])
  assert st==200, f'masked write-back should 200, got {st} {b}'
  assert enc_of(db,'ch_1')==enc1, 'masked write-back must keep the original ciphertext'
  # V1 反向断言：只改渠道名保存 → 服务端密文不变
  st,b=put([{'id':'ch_1','name':'聚鑫-改名','baseUrl':PUB,'apiFormat':'sdgf','models':['m1','m2'],'apiKey':MASKED}])
  assert st==200 and b['channels'][0]['name']=='聚鑫-改名', f'rename failed: {st} {b}'
  assert enc_of(db,'ch_1')==enc1, 'rename-only save must keep the ciphertext (V1 reverse)'
  # 空 key 且无旧密文可沿用 → 422「请重新输入完整 API Key」（整批回滚）
  st,b2=put([{'id':'ch_new','name':'新渠道','baseUrl':PUB,'apiFormat':'openai'}])
  assert st==422 and 'API Key' in b2.get('error',''), f'key-less new channel must 422 with hint, got {st} {b2}'
  assert len(json.loads(req(base,CHANNELS,opener=opA)[1])['channels'])==1, '422 must roll back the whole PUT (full replacement aborted)'
  checks.append('masked write-back keeps ciphertext; rename-only keeps ciphertext (V1 reverse); key-less new channel 422 + rollback')

  # 方法/登录态语义复位：匿名 PUT/DELETE 仍 401，登录态 POST 405
  assert req(base,CHANNELS,'PUT',{'channels':[]})[0]==401 and req(base,CHANNELS,'DELETE')[0]==401
  assert req(base,CHANNELS,'POST',{'channels':[]},opA)[0]==405
  # DELETE 清空该用户渠道集
  st,_=req(base,CHANNELS,'DELETE',opener=opA)
  assert st==200 and json.loads(req(base,CHANNELS,opener=opA)[1])['channels']==[], 'DELETE must clear the channel set'
  checks.append('DELETE clears the user channel set; POST -> 405; anon PUT/DELETE still 401')
 finally: srv.shutdown(); srv.server_close()
 checks += phase_b2()
 checks += phase_admin()
 checks += phase_s2_6()
 print('SUMMARY: PASS %d/%d'%(len(checks),len(checks)))
 for c in checks: print('PASS: '+c)
 return 0
if __name__=='__main__': raise SystemExit(main())
