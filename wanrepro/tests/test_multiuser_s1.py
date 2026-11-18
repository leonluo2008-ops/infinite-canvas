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
            {'id': 'ch_g', 'name': '双子', 'baseUrl': PUB_IP, 'apiFormat': 'gemini', 'apiKey': PKEY + 'g'},
            {'id': 'ch_v', 'name': '带v1', 'baseUrl': PUB_IP + '/v1', 'apiFormat': 'openai', 'models': ['m'], 'apiKey': PKEY}]}, opA)
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

        # --- S3.7：baseUrl 自带 /v1 时代理路径不得双拼 ---
        # 断言 A：前端按「subpath 相对 baseUrl」合同只传 /models，服务端拼 base.path → 上游恰收一个 /v1
        c, r = raw('/api/proxy/ch_v/models', 'GET', {'Cookie': 'wan_session=' + token})
        r.read(); c.close()
        rec = _Echo.seen[-1]
        assert r.status == 200, f'baseUrl-with-/v1 proxy should 200, got {r.status}'
        assert rec['path'] == '/v1/models' and rec['path'].count('/v1') == 1, \
            f'baseUrl /v1 + subpath /models must reach upstream as exactly /v1/models, got: {rec["path"]}'
        checks.append("S3.7-A: channel baseUrl '<ip>/v1' + proxy subpath /models -> upstream path exactly '/v1/models' (no double /v1)")

        # 断言 B（锁定现状，防未来误加服务端去重）：旧前端形态 /v1/models 原样拼，上游收到 /v1/v1/models
        c, r = raw('/api/proxy/ch_v/v1/models', 'GET', {'Cookie': 'wan_session=' + token})
        r.read(); c.close()
        rec = _Echo.seen[-1]
        assert r.status == 200, f'legacy-form proxy should still 200, got {r.status}'
        assert rec['path'] == '/v1/v1/models', \
            f'server must NOT dedupe (audit R1): legacy subpath /v1/models forwards verbatim as /v1/v1/models, got: {rec["path"]}'
        checks.append("S3.7-B: server forwarding contract stays 'base.path + subpath' verbatim (legacy form -> '/v1/v1/models', no dedupe)")

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
        assert st_ia == 200 and len(rows) == 3, f'one broken row must not 500 the whole list: {st_ia} {body_ia[:200]}'
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


def phase_s2_7():
    """S2.7：拆解耗时分解（耗时.json + GET /api/lapian/timing）。

    判据（对应任务书 ④/⑤）：
      * 耗时.json 生成、字段完整（总耗时/抽帧耗时/大拆解等待/门禁重拆轮次/阶段/归属/退出码）；
      * 端点三态鉴权：未登录 401、他人任务与不存在**同形** 404、自己的任务 200 available:true；
      * 引擎产物未被污染：拆解/看帧/门禁/门禁诊断/frames 的 mtime 与内容在 job 结束后逐字节不变，
        产物目录里只多 耗时.json（+多账户下的 .归属.json）；
      * 单人模式无感：/api/lapian/run argv/返回体不变、端点 404、无归属侧车、job 响应字段不变；
      * 监控口约：耗时.json 缺失/坏 JSON/字段缺失 → 200 + available:false（绝不 500）。

    夹具：`server.run_py` 换成「按引擎真实口径落产物」的假子进程（写 进度.json 三个阶段的
    心跳、frames 两帧带可控 mtime、门禁诊断-第1轮.md、门禁.json）——采集读的就是这些真文件；
    不调模型、不出网、不花钱。
    """
    import hashlib

    def sha(path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    def snapshot(d: Path):
        """引擎产物快照（不含监控自己的 耗时.json / .归属.json）。"""
        out = {}
        for p in sorted(d.rglob('*')):
            if not p.is_file() or p.name in ('耗时.json', '.归属.json'):
                continue
            st = p.stat()
            out[p.relative_to(d).as_posix()] = (st.st_mtime_ns, st.st_size, sha(p))
        return out

    def make_fixture(task_id: str, frames_span=(0.0, 4.0)):
        vid_dir = server.ROOT / 'uploads' / 'probe-s2-7'
        vid_dir.mkdir(parents=True, exist_ok=True)
        video = vid_dir / 'clip.mp4'
        video.write_bytes(b'\x00\x00\x00\x18ftypmp42' + b'\x00' * 64)
        tdir = server.ANALYSIS / task_id
        (tdir / 'frames').mkdir(parents=True, exist_ok=True)
        (tdir / '.源视频指纹.json').write_text(
            json.dumps({'路径': video.relative_to(server.ROOT).as_posix()}, ensure_ascii=False),
            encoding='utf-8')
        return video, tdir

    checks = []
    multiuser.FAILURES.clear(); multiuser.REG_ATTEMPTS.clear()
    os.environ['WANREPRO_CHANNEL_KEY'] = base64.b64encode(bytes(range(32))).decode()
    srv, base = serve('1')
    opA = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
    opB = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
    real_run_py = server.run_py
    seen: list[dict] = []

    def write_progress(tdir: Path, stage: str):
        (tdir / '进度.json').write_text(json.dumps({'stage': stage, 'ts': int(time.time())},
                                                   ensure_ascii=False), encoding='utf-8')

    def engine_like_run_py(stage_sleep=1.1, rounds=1, frames_span=(0.0, 4.0)):
        """按引擎口径落产物后返回退出码 0；阶段之间留出采样窗口。"""
        def fake(args, timeout, extra_env=None):
            seen.append({'args': list(args), 'env': dict(extra_env or {})})
            tdir = server.ANALYSIS / args[args.index('--id') + 1]
            (tdir / '拆解.md').write_text('# 拆解\n## 一 结构\n内容\n', encoding='utf-8')
            (tdir / '看帧.json').write_text(json.dumps({'秒': [{'秒': 1, '判定': '产品'}],
                                                        '看帧判定哈希': 'x'}, ensure_ascii=False),
                                            encoding='utf-8')
            base_ts = 1_700_000_000
            for index in range(2):
                p = tdir / 'frames' / f'f_{index:04d}s.jpg'
                p.write_bytes(b'jpg')
                os.utime(p, (base_ts + frames_span[index], base_ts + frames_span[index]))
            write_progress(tdir, '抽帧'); time.sleep(stage_sleep)
            write_progress(tdir, '大拆解'); time.sleep(stage_sleep)
            write_progress(tdir, '汇总'); time.sleep(1.3)
            for round_no in range(1, rounds + 1):
                (tdir / f'门禁诊断-第{round_no}轮.md').write_text('诊断\n', encoding='utf-8')
            (tdir / '门禁.json').write_text(json.dumps({'轮次': rounds, '状态': '通过'},
                                                       ensure_ascii=False), encoding='utf-8')
            write_progress(tdir, '完成')
            return 0, '', ''
        return fake

    def wait_job(job_id, timeout=20.0):
        end = time.time() + timeout
        while time.time() < end:
            with server.JOBS_LOCK:
                rec = dict(server.JOBS.get(job_id) or {})
            if rec.get('status') in ('done', 'error', 'failed'):
                return rec
            time.sleep(0.05)
        raise AssertionError(f'job {job_id} {timeout}s 内没结束')

    try:
        assert req(base, '/api/auth/register', 'POST',
                   {'username': 'ta', 'password': 'password1'}, opA)[0] == 200
        assert req(base, '/api/auth/register', 'POST',
                   {'username': 'tb', 'password': 'password1'}, opB)[0] == 200
        video, tdir = make_fixture('s27task')
        server.run_py = engine_like_run_py()

        # ---- T1：多账户下跑一次拆解 → 耗时.json 落盘且字段完整 ----
        st, body = req(base, '/api/lapian/run', 'POST', {'id': 's27task', 'intent': '看产品'},
                       opA)
        assert st == 200, f'lapian run: {st} {body[:200]}'
        rec = wait_job(json.loads(body)['job'])
        assert rec.get('status') == 'done', f'job must finish: {rec}'
        timing_path = tdir / '耗时.json'
        assert timing_path.is_file(), '耗时.json must be written at job end'
        data = json.loads(timing_path.read_text(encoding='utf-8'))
        for key in ('总耗时', '抽帧耗时', '大拆解等待', '门禁重拆轮次', '阶段', '归属', '退出码', '采集'):
            assert key in data, f'耗时.json missing {key}: {sorted(data)}'
        assert data['归属'] == 'ta', f"owner must be the initiator: {data.get('归属')!r}"
        assert data['退出码'] == 0, data['退出码']
        assert isinstance(data['总耗时'], (int, float)) and data['总耗时'] > 0, data['总耗时']
        assert abs(float(data['抽帧耗时']) - 4.0) < 0.01, f"抽帧耗时 must follow frame mtimes: {data['抽帧耗时']}"
        assert data['抽帧']['帧数'] == 2, data['抽帧']
        assert isinstance(data['大拆解等待'], (int, float)) and data['大拆解等待'] > 0.5, \
            f"大拆解等待 must come from the 大拆解 stage window: {data['大拆解等待']}"
        assert data['门禁重拆轮次'] == 1, data['门禁重拆轮次']
        stages = [str(w.get('阶段')) for w in data['阶段']]
        assert stages[:3] == ['抽帧', '大拆解', '汇总'], f'stage timeline: {stages}'
        assert data['采集']['样本数'] >= 1, data['采集']
        checks.append('S2.7-T1 耗时.json: 总耗时/抽帧耗时(帧 mtime 极差=4.0s)/大拆解等待(阶段窗口)/'
                      '门禁重拆轮次=1/阶段序列[抽帧,大拆解,汇总]/归属=发起来源/退出码=0')

        # ---- T2：端点三态鉴权（自己 200 / 他人 404 / 不存在 404 同形 / 未登录 401）----
        st, body = req(base, '/api/lapian/timing?task=s27task', opener=opA)
        own = json.loads(body)
        assert st == 200 and own.get('available') is True, f'owner timing: {st} {body[:200]}'
        assert own.get('总耗时') == data['总耗时'] and own.get('task') == 's27task', own
        st_other, b_other = req(base, '/api/lapian/timing?task=s27task', opener=opB)
        st_ghost, b_ghost = req(base, '/api/lapian/timing?task=s27ghost', opener=opB)
        assert st_other == 404 and st_ghost == 404, f'other/ghost must 404: {st_other}/{st_ghost}'
        assert b_other == b_ghost, f'他人任务与不存在必须同形：{b_other!r} vs {b_ghost!r}'
        st_anon, b_anon = req(base, '/api/lapian/timing?task=s27task')
        assert st_anon == 401, f'anon must 401, got {st_anon}'
        assert 'ta' not in b_anon.decode() and 'ta' not in b_other.decode(), 'error bodies must not leak owner'
        # 无归属记录的存量任务（多账户下）同样 404：不猜归属
        stale = server.ANALYSIS / 's27stale'
        stale.mkdir(parents=True, exist_ok=True)
        (stale / '耗时.json').write_text(json.dumps({'总耗时': 1, '阶段': []}), encoding='utf-8')
        assert req(base, '/api/lapian/timing?task=s27stale', opener=opA)[0] == 404, \
            'ownerless legacy task must 404 (no owner guessing)'
        checks.append('S2.7-T2 /api/lapian/timing 三态：自己 200 available:true(总耗时与文件一致)、'
                      '他人与不存在 404 逐字节同形、未登录 401、无归属存量任务 404（不猜归属）')

        # ---- T3：引擎产物未被污染（mtime/内容逐字节不变；目录只多耗时.json/.归属.json）----
        assert data['采集']['样本数'] >= 1
        before = snapshot(tdir)
        for _ in range(2):
            assert req(base, '/api/lapian/timing?task=s27task', opener=opA)[0] == 200
        after = snapshot(tdir)
        assert before == after, f'timing endpoint must be read-only: {set(before.items()) ^ set(after.items())}'
        files = {p.name for p in tdir.iterdir() if p.is_file()}
        extra = files - {'.源视频指纹.json', '意图.txt', '进度.json', '拆解.md', '看帧.json',
                         '门禁.json', '门禁诊断-第1轮.md', '耗时.json', '.归属.json'}
        assert not extra, f'采集不得新增别的文件：{sorted(extra)}'
        assert (tdir / '.归属.json').is_file(), '多账户模式必须写归属侧车'
        # 采集器不改引擎写点的关键证据：frames mtime 仍是夹具设定值（1_700_000_000/4）
        stamps = sorted(p.stat().st_mtime for p in (tdir / 'frames').iterdir())
        assert abs(stamps[0] - 1_700_000_000) < 1 and abs(stamps[-1] - 1_700_000_004) < 1, stamps
        checks.append('S2.7-T3 产物零污染：端点只读(两次调用前后 mtime/sha256 全等)、目录只多 耗时.json 与 .归属.json、'
                      'frames mtime 仍为夹具设定值')

        # ---- T4：可用性口径（缺失/坏 JSON/字段缺失 → 200 + available:false，绝不 500）----
        good = timing_path.read_text(encoding='utf-8')
        timing_path.unlink()
        st, body = req(base, '/api/lapian/timing?task=s27task', opener=opA)
        miss = json.loads(body)
        assert st == 200 and miss.get('available') is False and miss.get('missing'), \
            f'missing 耗时.json must be available:false: {st} {body[:200]}'
        timing_path.write_text('{不是 JSON', encoding='utf-8')
        st, body = req(base, '/api/lapian/timing?task=s27task', opener=opA)
        assert st == 200 and json.loads(body).get('available') is False, f'broken JSON: {st} {body[:200]}'
        partial = json.loads(good)
        partial.pop('门禁重拆轮次')
        timing_path.write_text(json.dumps(partial, ensure_ascii=False), encoding='utf-8')
        st, body = req(base, '/api/lapian/timing?task=s27task', opener=opA)
        got = json.loads(body)
        assert st == 200 and got.get('available') is False and '门禁重拆轮次' in got.get('missing', []), \
            f'missing field must be available:false: {st} {body[:200]}'
        timing_path.write_text(good, encoding='utf-8')
        checks.append('S2.7-T4 监控口约：耗时.json 缺失/坏 JSON/字段缺失 → 200 + available:false + missing 清单（无 500）')

        # ---- T5：写盘失败不影响拆解主链（零兜底）----
        broken_dir = server.ANALYSIS / 's27nofile'
        video2, _ = make_fixture('s27nofile')
        # 让 耗时.json 指向一个目录：os.replace 必失败 → 采集静默跳过，job 照常 done
        (broken_dir / '耗时.json').mkdir(parents=True, exist_ok=True)
        server.run_py = engine_like_run_py(stage_sleep=0.05, rounds=0)
        st, body = req(base, '/api/lapian/run', 'POST', {'id': 's27nofile', 'intent': 'x'}, opA)
        assert st == 200, f'{st} {body[:200]}'
        rec = wait_job(json.loads(body)['job'])
        assert rec.get('status') == 'done', f'timing write failure must not break the job: {rec}'
        checks.append('S2.7-T5 零兜底：耗时.json 写不出来（路径被目录占住）→ 拆解照常 done，采集静默跳过')

        # ---- T6：单人模式无感（argv/返回体不变、端点 404、无归属侧车）----
        srv.shutdown(); srv.server_close()
        srv, base = serve('0')
        video3, tdir3 = make_fixture('s27single')
        server.run_py = engine_like_run_py(stage_sleep=0.05, rounds=0)
        seen.clear()
        st, body = req(base, '/api/lapian/run', 'POST', {'id': 's27single', 'intent': 'x'})
        assert st == 200, f'single-user lapian run: {st} {body[:200]}'
        job_id = json.loads(body)['job']
        assert json.loads(body) == {'job': job_id}, f'run response must stay {{job}} only: {body[:120]}'
        rec = wait_job(job_id)
        assert rec.get('status') == 'done', rec
        assert seen and seen[-1]['args'] == ['analyze.py', video3.relative_to(server.ROOT).as_posix(),
                                            '--id', 's27single', '--yes', '--intent', 'x'], seen[-1]['args']
        assert seen[-1]['env'] == {}, 'single-user mode must inject zero extra env'
        st, body = req(base, '/api/lapian/timing?task=s27single')
        assert st == 404, f'MULTI_USER=0 timing endpoint must 404 (single-user baseline), got {st}'
        st, body = req(base, f'/api/job/{job_id}')
        job_view = json.loads(body)
        assert set(job_view) == {'status', 'result', 'elapsed'}, f'/api/job must keep its shape: {sorted(job_view)}'
        assert (tdir3 / '耗时.json').is_file(), '单人模式同样落 耗时.json（可直接开文件看，端点不开放）'
        assert not (tdir3 / '.归属.json').exists(), '单人模式不得写归属侧车'
        checks.append('S2.7-T6 单人模式无感：/api/lapian/run argv+返回体不变、零附加 env、/api/job 字段集合不变、'
                      '/api/lapian/timing 404、无 .归属.json（仅多一个 耗时.json 文件）')
    finally:
        server.run_py = real_run_py
        srv.shutdown(); srv.server_close()
    return checks


def phase_s3_b1():
    """S3-B1：画布项目服务端存储（user_projects + 逐项目 CRUD + 墓碑 + CAS）。

    时间戳域（任务书 §2.1 v1.2，B1 审计 BL1/BL2）：全链路 **int epoch 毫秒**（`Date.now()` 口径）。
    本相位的所有造数一律用 ms 级时间戳——秒级造数会让「单位断裂」在探针里完全看不出来：服务端把
    2500 当版本、前端发 1750000000000 当版本，两边永远对不上而探针全绿。所以下面：
      * `T0 = int(time.time()*1000)` 就是「现在」，一切更新/墓碑都落在它附近；
      * 秒级值（`T0//1000`）、ISO 字符串、2**63、越界值必须被 422 拒收（服务端不做单位归一猜测）；
      * DELETE 的 `deletedAt` 必填（ms），服务端不代填——否则服务端时钟会混进 LWW 判序。

    判据（任务书 §2.1 / §三 V3-V6 的服务端半边）：
      * CRUD 全动词：GET 清单（不含 content，回 unit:"ms"）/ GET 单项目 content / PUT upsert / DELETE 墓碑；
      * 归属：B 读 A 的 id 与读不存在的 id **逐字节同形** 404；B 的清单不含 A 的任何行；同名 id 在
        两个账号下互不影响（per-user 命名空间）；
      * CAS：baseUpdatedAt 过时不通过 → 409 + 服务端版本；不带 base 也不许把 updatedAt 往回写；
      * 体积：单项目 content 超 8MB → 413（含「声明长度超上限」的 413 必须真能送达客户端），且不落行；
      * 墓碑：DELETE 写 deleted=1 而**不物理删**（盘上行还在、content 不下发）——跨设备用 ms 级 ts
        验「旧设备 stale PUT 不复活」；显式 updatedAt > deletedAt 才复活（mergeCanvasData 既有语义）；
        删一个从来不存在/瞎猜的 id：一律 200 但**不留幽灵墓碑行**（非 oracle 也不留可观测写入）；
      * 未登录 401；MULTI_USER=0 全动词 404 且不建表、不写任何 owner 标记；
      * 并发 upsert 不损行（不同 id 同时写全部落盘；同一 id 连打不出 500、不丢行）；
      * keep-alive 不错帧：同一长连接上「带体的 404/405」之后继续 GET 必须照常 200（不是 400）；
      * Content-Length 撒谎（短读）→ 400 且截断体绝不落库。
    """
    checks = []
    multiuser.FAILURES.clear(); multiuser.REG_ATTEMPTS.clear()
    os.environ.pop('WANREPRO_CHANNEL_KEY', None)      # 项目存储与渠道主密钥无关（零交叉依赖）
    srv, base = serve('1')
    db = Path(os.environ[multiuser.DB_PATH_ENV])
    PROJ = '/api/user/projects'
    T0 = int(time.time() * 1000)          # Date.now() 口径的唯一时间基准（ms）

    def jreq(path, method='GET', body=None, op=None):
        st, raw = req(base, path, method, body, op)
        try:
            return st, json.loads(raw), raw
        except ValueError:
            return st, {}, raw

    def put(pid, payload, op=None):
        return jreq(PROJ + '/' + pid, 'PUT', payload, op)

    def rows(pid=None):
        c = sqlite3.connect(str(db))
        try:
            q = 'SELECT * FROM user_projects' + (' WHERE project_id=?' if pid else '')
            cur = c.execute(q, (pid,) if pid else ())
            return cur.fetchall(), [d[0] for d in cur.description]
        finally:
            c.close()

    def dev(method, path, payload, token):
        """另一台设备：每次一条新连接（同一个 session cookie），用来造「跨设备」时序。"""
        data = None if payload is None else json.dumps(payload).encode()
        c = http.client.HTTPConnection('127.0.0.1', srv.server_port, timeout=20)
        try:
            hdrs = {'Cookie': 'wan_session=' + token}
            if data is not None:
                hdrs['Content-Type'] = 'application/json'; hdrs['Content-Length'] = str(len(data))
            c.request(method, path, body=data, headers=hdrs)
            r = c.getresponse(); raw = r.read()
            try:
                return r.status, json.loads(raw), raw
            except ValueError:
                return r.status, {}, raw
        finally:
            c.close()

    jarA = http.cookiejar.CookieJar(); opA = build_opener(HTTPCookieProcessor(jarA))
    opB = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
    try:
        assert req(base, '/api/auth/register', 'POST',
                   {'username': 'projA', 'password': 'password1'}, opA)[0] == 200
        assert req(base, '/api/auth/register', 'POST',
                   {'username': 'projB', 'password': 'password1'}, opB)[0] == 200
        tokenA = [c.value for c in jarA if c.name == 'wan_session'][0]

        # ---- S3-T1 登录态硬前置：未登录全动词 401（不是 404/405）----
        for method, path, body in (('GET', PROJ, None), ('GET', PROJ + '/p1', None),
                                   ('PUT', PROJ + '/p1', {'content': {'id': 'p1'}, 'updatedAt': T0}),
                                   ('DELETE', PROJ + '/p1', {'deletedAt': T0}),
                                   ('POST', PROJ, {}), ('POST', PROJ + '/p1', {})):
            st, _, _ = jreq(path, method, body)
            assert st == 401, f'anon {method} {path} must 401, got {st}'
        checks.append('S3-T1 未登录：GET 清单/GET 单项目/PUT/DELETE/POST 全部 401（不是 404/405）')

        # ---- S3-T2 PUT upsert + GET 单项目 content 往返（ms 时间戳）----
        content1 = {'id': 'p1', 'name': '项目一', 'nodes': [{'id': 'n1', 'x': 1}], 'edges': []}
        st, body, _ = put('p1', {'content': content1, 'updatedAt': T0}, opA)
        assert st == 200 and body['ok'] is True, f'PUT p1: {st} {body}'
        p = body['project']
        assert p['id'] == 'p1' and p['updatedAt'] == T0 and p['version'] == T0, p
        assert p['deleted'] is False and p['deletedAt'] == 0 and p['bytes'] > 0, p
        assert p['name'] == '项目一', f'name must come from content.name when body.name is absent: {p}'
        assert 'content' not in p, 'PUT 响应只回元数据（不回 content，避免 8MB 回显）'
        st, body, _ = jreq(PROJ + '/p1', op=opA)
        assert st == 200 and body['project']['content'] == content1, f'GET single round-trip: {st} {body}'
        assert body['project']['version'] == T0, body['project']
        checks.append('S3-T2 PUT /api/user/projects/p1 upsert（ms 时间戳；name 取 content.name，响应只回元数据）；'
                      'GET 单项目 content 逐字段往返、version=updatedAt')

        # ---- S3-T3 清单：不含 content + 单位自证 + 所有者判定辅助字段 ----
        st, body, raw = jreq(PROJ, op=opA)
        assert st == 200, f'GET list: {st} {body}'
        items = body['projects']
        assert len(items) == 1 and items[0]['id'] == 'p1', items
        assert 'content' not in items[0] and 'content_json' not in items[0], f'清单不许带 content: {items[0]}'
        assert len(raw) < 2048, f'清单必须是 KB 级（当前 {len(raw)} 字节）'
        c = sqlite3.connect(str(db)); uid_a = c.execute("SELECT id FROM users WHERE username='projA'").fetchone()[0]; c.close()
        assert body['owner'] == 'projA' and body['ownerId'] == uid_a, \
            f'清单必须带所有者判定字段 owner/ownerId：{ {k: body[k] for k in ("owner", "ownerId")} }'
        # 时间戳单位自证：serverTime 必须是 Date.now() 口径（ms），不是秒
        assert body['unit'] == 'ms', f'清单必须回 unit:"ms"：{body.get("unit")}'
        assert isinstance(body['serverTime'], int) and body['serverTime'] > 10 ** 11, body.get('serverTime')
        assert abs(body['serverTime'] - int(time.time() * 1000)) < 60_000, \
            f'serverTime 必须是 ms 级当前时刻：{body["serverTime"]}'
        assert body['maxUpdatedAt'] == T0 and body['maxVersion'] == T0, body
        assert body['count'] == 1 and body['liveCount'] == 1 and body['tombstoneCount'] == 0, body
        checks.append('S3-T3 清单 KB 级（<2KB、无 content）+ 单位自证 unit:"ms"/serverTime 为 ms 级当前时刻 '
                      '+ 所有者判定辅助字段 owner/ownerId/maxUpdatedAt/maxVersion/count/liveCount/tombstoneCount')

        # ---- S3-T4 CAS：baseUpdatedAt 命中 → 200；过时 → 409 + 服务端版本；版本回退 → 409 ----
        content2 = {'id': 'p1', 'name': '项目一', 'nodes': [{'id': 'n1', 'x': 2}], 'edges': []}
        st, body, _ = put('p1', {'content': content2, 'updatedAt': T0 + 1000, 'baseUpdatedAt': T0}, opA)
        assert st == 200 and body['project']['version'] == T0 + 1000, f'CAS hit: {st} {body}'
        st, body, _ = put('p1', {'content': content2, 'updatedAt': T0 + 2000, 'baseUpdatedAt': T0}, opA)
        assert st == 409 and body.get('conflict') is True, f'stale base must 409: {st} {body}'
        assert body['server']['version'] == T0 + 1000 and body['server']['updatedAt'] == T0 + 1000, \
            f'409 必须带服务端版本：{body.get("server")}'
        assert 'content' not in body['server'], '409 不回 content（要内容就再 GET）'
        st, body, _ = put('p1', {'content': content1, 'updatedAt': T0 + 500}, opA)
        assert st == 409 and body['server']['updatedAt'] == T0 + 1000, \
            f'不带 base 也不许回写更旧的 updatedAt: {st} {body}'
        st, body, _ = jreq(PROJ + '/p1', op=opA)
        assert body['project']['content'] == content2 and body['project']['updatedAt'] == T0 + 1000, \
            '冲突后服务端内容不得被改'
        checks.append('S3-T4 CAS（ms）：baseUpdatedAt=T0 命中→200(version T0+1000)；过时 base→409+server.version='
                      'T0+1000（无 content）；不带 base 回写 updatedAt=T0+500→409；冲突零覆盖')

        # ---- S3-T5 归属：B 读 A 的 id 与读不存在的 id 同形 404；清单隔离；同名 id 互不影响 ----
        st_other, b_other, raw_other = jreq(PROJ + '/p1', op=opB)
        st_ghost, b_ghost, raw_ghost = jreq(PROJ + '/no_such_project_xyz', op=opB)
        assert st_other == 404 and st_ghost == 404, f'foreign/ghost must 404: {st_other}/{st_ghost}'
        assert raw_other == raw_ghost, f'他人 id 与不存在必须逐字节同形：{raw_other!r} vs {raw_ghost!r}'
        st, body, _ = jreq(PROJ, op=opB)
        assert st == 200 and body['projects'] == [] and body['count'] == 0, f'B 的清单必须为空：{body}'
        assert body['owner'] == 'projB', body['owner']
        # 同一 project_id 在两个账号下是两行（per-user 命名空间），B 的写/删不碰 A 的行
        st, body, _ = put('p1', {'content': {'id': 'p1', 'name': 'B的p1', 'nodes': []}, 'updatedAt': T0}, opB)
        assert st == 200, f'B 写自己的 p1: {st} {body}'
        st, body, _ = jreq(PROJ + '/p1', op=opA)
        assert st == 200 and body['project']['content']['name'] == '项目一', 'A 的 p1 不得被 B 覆盖'
        st, body, _ = jreq(PROJ + '/p1', op=opB)
        assert body['project']['content']['name'] == 'B的p1', f'B 的 p1 必须是 B 自己的行: {body}'
        allr, _ = rows('p1')
        assert len(allr) == 2, f'同名 id 必须按 (user_id, project_id) 存两行：{len(allr)}'
        checks.append('S3-T5 V6 归属：B GET A 的 id 404 与不存在逐字节同形；B 清单为空（owner=projB）；'
                      '同一 id 在 A/B 各存一行、互不覆盖')

        # ---- S3-T6 墓碑：DELETE 带客户端 ms deletedAt（服务端不代填）+ 不物理删 + content 不下发 ----
        DEL_AT = T0 + 5000
        st, body, _ = jreq(PROJ + '/p1', 'DELETE', {'deletedAt': DEL_AT}, opA)
        assert st == 200 and body['project']['deleted'] is True and body['project']['deletedAt'] == DEL_AT, \
            f'DELETE 必须回墓碑且 deletedAt 就是客户端给的值: {st} {body}'
        deleted_at = body['project']['deletedAt']
        allr, cols = rows('p1')
        a_row = [r for r in allr if r[cols.index('user_id')] == uid_a]
        assert len(a_row) == 1, f'墓碑必须留在盘上（不物理删）：{len(a_row)}'
        assert a_row[0][cols.index('deleted')] == 1 and a_row[0][cols.index('deleted_at')] == deleted_at, \
            'deleted=1 + deleted_at 必须落库'
        st, body, _ = jreq(PROJ, op=opA)
        tomb = [i for i in body['projects'] if i['id'] == 'p1'][0]
        assert tomb['deleted'] is True and tomb['deletedAt'] == deleted_at, f'清单必须带墓碑：{tomb}'
        assert body['liveCount'] == 0 and body['tombstoneCount'] == 1, body
        st, body, _ = jreq(PROJ + '/p1', op=opA)
        assert st == 200 and body['project']['deleted'] is True and body['project']['content'] is None, \
            f'墓碑的 content 一律不下发: {st} {body}'
        checks.append('S3-T6 墓碑：DELETE{deletedAt:<ms>} → deleted=1+deletedAt 落盘（值=客户端给的 ms，'
                      '服务端零代填；盘上行仍在，不物理删）；清单带墓碑；单项目 GET 回 deleted:true 且 content=null')

        # ---- S3-T7 V3（ms 级复验）：旧设备 stale PUT 不复活；updatedAt>deletedAt 才复活 ----
        st, body, _ = put('p1', {'content': content1, 'updatedAt': DEL_AT - 1000}, opA)
        assert st == 409 and body['server']['deleted'] is True, f'stale PUT 不得复活墓碑: {st} {body}'
        allr, cols = rows('p1')
        a_row = [r for r in allr if r[cols.index('user_id')] == uid_a][0]
        assert a_row[cols.index('deleted')] == 1 and a_row[cols.index('deleted_at')] == deleted_at, \
            'stale PUT 之后墓碑必须原样（不复活、不改 deleted_at）'
        st, body, _ = put('p1', {'content': {'id': 'p1', 'name': '复活', 'nodes': []},
                                 'updatedAt': deleted_at + 1000, 'baseUpdatedAt': deleted_at}, opA)
        assert st == 200 and body['project']['deleted'] is False and body['project']['deletedAt'] == 0, \
            f'updatedAt > deletedAt 应复活: {st} {body}'
        assert body['project']['version'] == deleted_at + 1000, body['project']
        checks.append('S3-T7 V3 墓碑优先（ms 级造数）：updatedAt=deletedAt-1000 的旧设备 PUT → 409 且墓碑原样'
                      '（不复活、deleted_at 不动）；updatedAt=deletedAt+1000 + base=墓碑版本 → 复活（deleted=0/deletedAt=0）')

        # ---- S3-T7b 墓碑时序边界 ts == deletedAt（B2 复审顺手②：S3-T7 只测了 ±1000，等号边界半恒真）----
        # 判据：project_store._put_project 的墓碑分支必须用 `ts <= deleted_at` 判 409（墓碑优先），
        # 因此 **ts == deleted_at 不得复活**；而 CAS 已通过（base==version）时这一条是**唯一**拦截者，
        # 若有人把它写成 `<` 或先判 CAS 通过就放行，等号用例才会红 —— 这就是本条要锁死的东西。
        st, body, _ = put('p_t7b', {'content': {'id': 'p_t7b', 'name': '边界项目', 'nodes': []},
                                    'updatedAt': T0 + 30000}, opA)
        assert st == 200 and body['project']['version'] == T0 + 30000, f'边界项目建行: {st} {body}'
        edge_del = T0 + 31000
        st, body, _ = jreq(PROJ + '/p_t7b', 'DELETE', {'deletedAt': edge_del}, opA)
        assert st == 200 and body['project']['deleted'] is True and body['project']['deletedAt'] == edge_del, \
            f'边界墓碑落盘: {st} {body}'
        assert body['project']['version'] == edge_del, f'墓碑版本 = max(updated_at, deleted_at): {body["project"]}'

        # (1) ts == deleted_at，且**不带** base → 409
        st, nb, _ = put('p_t7b', {'content': {'id': 'p_t7b', 'name': '等号复活', 'nodes': []},
                                  'updatedAt': edge_del}, opA)
        assert st == 409, f'updatedAt == deleted_at 不得复活（墓碑优先是 <=）, got {st} {nb}'
        assert nb['server']['deleted'] is True and nb['server']['deletedAt'] == edge_del, nb

        # (2) ts == deleted_at 且 base 正好等于墓碑版本（CAS 通过）→ 仍然 409（这条才是半恒真锁）
        st, nb, _ = put('p_t7b', {'content': {'id': 'p_t7b', 'name': '等号复活', 'nodes': []},
                                  'updatedAt': edge_del, 'baseUpdatedAt': edge_del}, opA)
        assert st == 409, f'base 命中也必须被墓碑规则拦下, got {st} {nb}'
        allr, cols = rows('p_t7b')
        a_row = [r for r in allr if r[cols.index('user_id')] == uid_a][0]
        assert a_row[cols.index('deleted')] == 1 and a_row[cols.index('deleted_at')] == edge_del, \
            '等号边界被拒后墓碑必须原样（不被改写、不被复活）'

        # (3) ts == deleted_at + 1 → 复活（边界的另一侧必须真的能过）
        st, body, _ = put('p_t7b', {'content': {'id': 'p_t7b', 'name': '越界一毫秒复活', 'nodes': []},
                                    'updatedAt': edge_del + 1, 'baseUpdatedAt': edge_del}, opA)
        assert st == 200 and body['project']['deleted'] is False and body['project']['version'] == edge_del + 1, \
            f'deleted_at+1 应复活: {st} {body}'
        assert body['project']['deletedAt'] == 0, body['project']

        # (4) 同 deletedAt 重复 DELETE（幂等）：版本不倒退、行仍为墓碑
        st, body, _ = jreq(PROJ + '/p_t7b', 'DELETE', {'deletedAt': edge_del + 1}, opA)
        assert st == 200 and body['project']['deleted'] is True, f'再次删除: {st} {body}'
        st, body, _ = jreq(PROJ + '/p_t7b', 'DELETE', {'deletedAt': edge_del + 1}, opA)
        assert st == 200 and body['project']['deletedAt'] == edge_del + 1 and body['project']['version'] == edge_del + 1, \
            f'重复 DELETE 幂等（deleted_at 不被更小的值改小）: {body["project"]}'
        checks.append('S3-T7b 墓碑等号边界：updatedAt == deletedAt（带/不带 baseUpdatedAt）一律 409 且墓碑原样；'
                      'deletedAt+1 复活；重复 DELETE 幂等（版本不倒退）')

        # ---- S3-T8 体积：content 超 8MB → 413 且不落行 ----
        big = {'id': 'pbig', 'name': '巨大', 'blob': 'x' * (8 * 1024 * 1024)}
        st, body, _ = put('pbig', {'content': big, 'updatedAt': T0 + 9000}, opA)
        assert st == 413, f'超 8MB 必须 413, got {st} {str(body)[:200]}'
        allr, _ = rows('pbig')
        assert not allr, '413 之后不许落任何行'
        st, body, _ = jreq(PROJ, op=opA)
        assert all(i['id'] != 'pbig' for i in body['projects']), '清单不得出现被拒的项目'
        checks.append('S3-T8 单项目 content 超 8MB → 413（不落行、不出现在清单）')

        # ---- S3-T9 并发 upsert 不损行 ----
        errs = []

        def cput(pid, ts, tag):
            payload = json.dumps({'content': {'id': pid, 'tag': tag, 'nodes': []}, 'updatedAt': ts}).encode()
            conn = http.client.HTTPConnection('127.0.0.1', srv.server_port, timeout=20)
            try:
                conn.request('PUT', PROJ + '/' + pid, body=payload,
                             headers={'Content-Type': 'application/json',
                                      'Content-Length': str(len(payload)),
                                      'Cookie': 'wan_session=' + tokenA})
                r = conn.getresponse(); r.read()
                if r.status not in (200, 409):
                    errs.append((pid, r.status))
            except Exception as exc:                      # noqa: BLE001 - 探针要区分「网络炸了」
                errs.append((pid, repr(exc)))
            finally:
                conn.close()

        threads = [threading.Thread(target=cput, args=('p_cc%d' % i, T0 + 10000 + i, 't%d' % i))
                   for i in range(6)]
        threads += [threading.Thread(target=cput, args=('p_hot', T0 + 20000 + i, 'h%d' % i)) for i in range(4)]
        for t in threads: t.start()
        for t in threads: t.join()
        assert not errs, f'并发 upsert 出现 500/异常：{errs}'
        st, body, _ = jreq(PROJ, op=opA)
        ids = {i['id'] for i in body['projects']}
        assert {'p_cc%d' % i for i in range(6)} <= ids, f'并发写不同 id 有丢行：{sorted(ids)}'
        hot = [i for i in body['projects'] if i['id'] == 'p_hot'][0]
        assert T0 + 20000 <= hot['updatedAt'] <= T0 + 20003, f'同一 id 连打后版本必须是某个成功写的值：{hot}'
        allr, _ = rows('p_hot')
        assert len(allr) == 1, f'同一 id 只许一行：{len(allr)}'
        checks.append('S3-T9 并发 upsert：6 个不同 id 同时写全部落盘（零 500）；同一 id 4 连打只留一行、'
                      '版本落在成功写集合内（BEGIN IMMEDIATE 串行化）')

        # ---- S3-T10 方法/ID 卫生 ----
        st, _, _ = jreq(PROJ, 'POST', {}, opA)
        assert st == 405, f'POST 清单必须 405, got {st}'
        st, _, _ = jreq(PROJ + '/p1', 'POST', {}, opA)
        assert st == 405, f'POST 单项目必须 405, got {st}'
        st, _raw_bad = req(base, PROJ + '/bad%2Fid', 'GET', None, opA)      # 解码后含 '/' → 不当成 id
        assert st == 404, f'非法 id 必须 404（与不存在同形）, got {st}'
        st, nb, _ = put('p_nobase', {'content': {'id': 'p_nobase'}}, opA)
        assert st == 422 and 'updatedAt' in nb.get('error', ''), f'缺 updatedAt 必须 422: {st} {nb}'
        checks.append('S3-T10 方法/入参卫生：POST 清单与单项目 405；非法 id 404（同形）；缺 updatedAt 422'
                      '（服务端不代填时间戳）')

        # ---- S3-T12 BL1 时间戳域：秒级/ISO/2**63/越界一律 422，边界值本身合法 ----
        bad_ts = [('秒级 10 位', T0 // 1000), ('1e11 下界-1', 10 ** 11 - 1),
                  ('4e15 上界+1', 4 * 10 ** 15 + 1), ('2**63', 2 ** 63),
                  ('ISO 字符串', '2026-11-16T10:00:00.000Z'), ('浮点半毫秒', T0 + 0.5),
                  ('负值', -T0)]
        for label, ts in bad_ts:
            st, nb, _ = put('p_ts', {'content': {'id': 'p_ts'}, 'updatedAt': ts}, opA)
            assert st == 422 and 'updatedAt' in nb.get('error', ''), \
                f'updatedAt={label} 必须 422（不做单位归一猜测），got {st} {nb}'
        assert not rows('p_ts')[0], '被 422 拒收的时间戳不许落任何行'
        # 域边界本身合法（1e11 与 4e15 都收）
        st, nb, _ = put('p_edge', {'content': {'id': 'p_edge'}, 'updatedAt': 10 ** 11}, opA)
        assert st == 200 and nb['project']['updatedAt'] == 10 ** 11, f'下界应合法: {st} {nb}'
        st, nb, _ = put('p_edge', {'content': {'id': 'p_edge'}, 'updatedAt': 4 * 10 ** 15,
                                   'baseUpdatedAt': 10 ** 11}, opA)
        assert st == 200 and nb['project']['updatedAt'] == 4 * 10 ** 15, f'上界应合法: {st} {nb}'
        # baseUpdatedAt 同域：秒级 base → 422（不是「当成另一个单位」）
        st, nb, _ = put('p_edge', {'content': {'id': 'p_edge'}, 'updatedAt': T0 + 1000,
                                   'baseUpdatedAt': T0 // 1000}, opA)
        assert st == 422 and 'baseUpdatedAt' in nb.get('error', ''), f'秒级 baseUpdatedAt 必须 422: {st} {nb}'
        checks.append('S3-T12 BL1 时间戳域：updatedAt 秒级/1e11-1/4e15+1/2**63/ISO 串/浮点/负值 → 全部 422 且不落行'
                      '（零单位猜测）；边界 1e11 与 4e15 合法；baseUpdatedAt 同域（秒级 base → 422）')

        # ---- S3-T13 BL2 DELETE 必带 ms deletedAt：缺/非法一律 422，服务端不代填 ----
        st, _, _ = put('p_del', {'content': {'id': 'p_del', 'name': '待删'}, 'updatedAt': T0 + 100}, opA)
        assert st == 200, f'准备 p_del: {st}'
        for label, payload in (('无体', None), ('空对象', {}),
                               ('ISO 字符串', {'deletedAt': '2026-11-16T10:00:00.000Z'}),
                               ('秒级', {'deletedAt': T0 // 1000}), ('2**63', {'deletedAt': 2 ** 63}),
                               ('null', {'deletedAt': None})):
            st, nb, _ = jreq(PROJ + '/p_del', 'DELETE', payload, opA)
            assert st == 422 and 'deletedAt' in nb.get('error', ''), \
                f'DELETE deletedAt={label} 必须 422（服务端不代填），got {st} {nb}'
        allr, cols = rows('p_del')
        assert len(allr) == 1 and allr[0][cols.index('deleted')] == 0 and allr[0][cols.index('deleted_at')] is None, \
            f'被 422 拒收的 DELETE 不许改动这一行：{allr}'
        checks.append('S3-T13 BL2 DELETE 必带 deletedAt（ms）：无体/空对象/ISO 串/秒级/2**63/null 全部 422，'
                      '且服务端那一行原样（不代填时间戳、不落墓碑）')

        # ---- S3-T14 跨设备墓碑（ms 级）：另一台设备删除后，旧设备的 stale PUT 不复活 ----
        DT = T0 + 30000
        st, _, _ = dev('PUT', PROJ + '/p_tomb', {'content': {'id': 'p_tomb', 'name': '跨设备'}, 'updatedAt': T0 + 20000},
                       tokenA)
        assert st == 200, f'设备A 建 p_tomb: {st}'
        st, body, _ = dev('DELETE', PROJ + '/p_tomb', {'deletedAt': DT}, tokenA)   # 设备B（另一条连接）
        assert st == 200 and body['project']['deletedAt'] == DT, f'设备B 删除: {st} {body}'
        for stale in (T0 + 21000, DT - 1):          # 旧设备仍以为它活着 / 只差一毫秒
            st, body, _ = dev('PUT', PROJ + '/p_tomb', {'content': {'id': 'p_tomb', 'name': '旧设备'}, 'updatedAt': stale},
                              tokenA)
            assert st == 409 and body['server']['deleted'] is True and body['server']['deletedAt'] == DT, \
                f'旧设备 updatedAt={stale} 不得复活墓碑: {st} {body}'
        allr, cols = rows('p_tomb')
        assert allr[0][cols.index('deleted')] == 1 and allr[0][cols.index('deleted_at')] == DT, '墓碑必须原样'
        st, body, _ = dev('PUT', PROJ + '/p_tomb',
                          {'content': {'id': 'p_tomb', 'name': '显式复活'}, 'updatedAt': DT + 1000,
                           'baseUpdatedAt': DT}, tokenA)
        assert st == 200 and body['project']['deleted'] is False and body['project']['version'] == DT + 1000, \
            f'ms 级显式更新的 updatedAt 才复活: {st} {body}'
        checks.append('S3-T14 跨设备墓碑（ms）：设备B DELETE{deletedAt:DT} 后，设备A 的 updatedAt=T0+21000 与 '
                      'DT-1 两次 PUT 均 409 且 deleted_at 仍=DT；updatedAt=DT+1000+base=DT 才复活')

        # ---- S3-T15 BL3 keep-alive：带体的早退（404/405）之后，同一连接继续 GET 不得收到 400 ----
        def kl_probe(method, path, payload, want):
            conn = http.client.HTTPConnection('127.0.0.1', srv.server_port, timeout=20)
            try:
                data = json.dumps(payload).encode()
                conn.request(method, path, body=data,
                             headers={'Content-Type': 'application/json',
                                      'Content-Length': str(len(data)),
                                      'Cookie': 'wan_session=' + tokenA})
                r1 = conn.getresponse(); b1 = r1.read()
                assert r1.status == want, f'{method} {path} 应 {want}, got {r1.status} {b1[:200]}'
                assert 'close' in (r1.getheader('Connection') or '').lower(), \
                    f'早退响应必须发 Connection: close（体没读），got {r1.getheader("Connection")!r}'
                # 同一连接继续发（客户端按 Connection: close 重连）：绝不允许 400 错帧
                conn.request('GET', PROJ, headers={'Cookie': 'wan_session=' + tokenA})
                r2 = conn.getresponse(); b2 = r2.read()
                assert r2.status == 200, f'早退后同连接 GET 必须 200（不许 400 错帧），got {r2.status} {b2[:200]}'
                assert b'Bad request' not in b2 and json.loads(b2)['owner'] == 'projA', '响应必须完整且是自己的清单'
            finally:
                conn.close()

        kl_probe('PUT', PROJ + '/bad%2Fid', {'content': {'id': 'x'}, 'updatedAt': T0}, 404)
        kl_probe('POST', PROJ, {'content': {'id': 'x'}, 'updatedAt': T0}, 405)
        checks.append('S3-T15 BL3 keep-alive 不错帧：带体的 PUT 非法pid(404) 与 POST 清单(405) 之后，同一条连接'
                      '继续 GET 仍是 200 且响应完整（早退一律 Connection: close，残留体不会串成下一个请求行）')

        # ---- S3-T16 BL3 短读：Content-Length 撒谎 → 400 且截断体绝不落库 ----
        short = json.dumps({'content': {'id': 'p_short', 'blob': 'y' * 200}, 'updatedAt': T0}).encode()
        s = socket.create_connection(('127.0.0.1', srv.server_port), timeout=20)
        try:
            head = ('PUT %s/p_short HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n'
                    'Content-Length: %d\r\nCookie: wan_session=%s\r\n\r\n'
                    % (PROJ, len(short) + 64, tokenA)).encode()
            s.sendall(head + short[:len(short) // 2])       # 只发一半，然后半关写端
            s.shutdown(socket.SHUT_WR)
            resp = b''
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                resp += chunk
        finally:
            s.close()
        assert b' 400 ' in resp.split(b'\r\n')[0], f'短读必须 400，got {resp.split(chr(13).encode())[0][:120]!r}'
        allr, _ = rows('p_short')
        assert not allr, f'截断的体绝不许落库：{allr}'
        checks.append('S3-T16 BL3 短读：Content-Length 撒谎（声明 %d 实发 %d 后半关）→ 400 + 连接关闭，'
                      '截断体零落库' % (len(short) + 64, len(short) // 2))

        # ---- S3-T17 impl②：PUT 未带 name 且 content 无 name → 保留旧行 name（不许清空）----
        st, b, _ = put('p_name', {'content': {'id': 'p_name', 'name': '旧名'}, 'updatedAt': T0 + 40000}, opA)
        assert st == 200 and b['project']['name'] == '旧名', f'初建 p_name: {st} {b}'
        st, b, _ = put('p_name', {'content': {'id': 'p_name', 'nodes': []}, 'updatedAt': T0 + 40001}, opA)
        assert st == 200 and b['project']['name'] == '旧名', f'未带 name 必须保留旧名（不许清空）: {b}'
        st, b, _ = put('p_name', {'content': {'id': 'p_name'}, 'updatedAt': T0 + 40002, 'name': ''}, opA)
        assert st == 200 and b['project']['name'] == '旧名', f'空字符串 name 也必须保留旧名: {b}'
        st, b, _ = put('p_name', {'content': {'id': 'p_name'}, 'updatedAt': T0 + 40003, 'name': '新名'}, opA)
        assert st == 200 and b['project']['name'] == '新名', f'body.name 必须生效: {b}'
        st, b, _ = put('p_name', {'content': {'id': 'p_name', 'name': '内容名'}, 'updatedAt': T0 + 40004}, opA)
        assert st == 200 and b['project']['name'] == '内容名', f'content.name 必须生效: {b}'
        allr, cols = rows('p_name')
        assert allr[0][cols.index('name')] == '内容名', allr
        checks.append('S3-T17 impl② name 保全：未带 name 且 content 无 name / name="" → 保留旧行 name（事务内取旧值）；'
                      'body.name 与 content.name 仍各自生效')

        # ---- S3-T18 polish：DELETE 一个从未存在的 id → 200 但零幽灵墓碑行（非 oracle 也不留写入痕迹）----
        st, b, _ = jreq(PROJ + '/p_never_existed', 'DELETE', {'deletedAt': T0 + 50000}, opA)
        assert st == 200 and b['project']['deleted'] is True and b['project']['deletedAt'] == T0 + 50000, \
            f'删不存在的 id 仍一律 200（非 oracle）: {st} {b}'
        allr, _ = rows('p_never_existed')
        assert not allr, f'不存在就不许建幽灵墓碑行：{allr}'
        st, body, _ = jreq(PROJ, op=opA)
        assert all(i['id'] != 'p_never_existed' for i in body['projects']), '幽灵行不许出现在清单里'
        st, _, _ = jreq(PROJ + '/p_never_existed', op=opA)
        assert st == 404, f'删过的不存在 id GET 仍 404（与从未存在同形），got {st}'
        checks.append('S3-T18 polish：DELETE 从未存在的 id → 200（非 oracle，回调用方给的墓碑视图），'
                      '但盘上零行、清单零项（不建幽灵墓碑行）')

        # ---- S3-T19 impl④ 413 drain：声明长度超 BODY_LIMIT 时，服务端必须先把体读走再回 413 ----
        # 判据设计：只发一半声明体 → 服务端**不许**已经回响应（说明它在 drain；修复前是「立刻关」，
        # 真机上带未读数据 close 会发 RST，客户端根本收不到 413）。补发剩余体后才许收到 413。
        TOTAL19 = 9 * 1024 * 1024
        s19 = socket.create_connection(('127.0.0.1', srv.server_port), timeout=30)
        try:
            s19.sendall(('PUT %s/p_cl HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n'
                         'Content-Length: %d\r\nCookie: wan_session=%s\r\n\r\n'
                         % (PROJ, TOTAL19, tokenA)).encode())
            sent = 1024 * 1024
            s19.sendall(b'x' * sent)
            s19.settimeout(0.8)
            try:
                early = s19.recv(1)
            except socket.timeout:
                early = b''
            assert early == b'', \
                f'只发了 {sent}/{TOTAL19} 字节就收到响应/断开 → 服务端没 drain 就关连接（真机上 413 会被 RST 吞掉）'
            s19.settimeout(30)
            while sent < TOTAL19:                       # 补发剩余体：drain 读满后才会回 413
                n = min(1024 * 1024, TOTAL19 - sent)
                s19.sendall(b'x' * n); sent += n
            resp = b''
            while b'\r\n\r\n' not in resp:
                chunk = s19.recv(65536)
                if not chunk:
                    break
                resp += chunk
            head, _, tail = resp.partition(b'\r\n\r\n')
            while b'\r\n\r\n' in resp and len(tail) < 200:   # 413 文案很短，读满一屏即可
                chunk = s19.recv(65536)
                if not chunk:
                    break
                tail += chunk
        finally:
            s19.close()
        assert b' 413 ' in head.split(b'\r\n')[0], f'声明 {TOTAL19} 字节必须收到 413，got {head[:120]!r}'
        assert '8MB' in tail.decode('utf-8', 'replace'), f'413 文案必须说清 8MB 上限：{tail[:200]!r}'
        assert not rows('p_cl')[0], '413 之后不许落行'
        checks.append('S3-T19 impl④ 413 drain：声明 %d 字节（> BODY_LIMIT）只发 1MB 时服务端无响应（仍在 drain，'
                      '不是立刻关连接）/ 补满后收到 413+8MB 文案 / 零落行' % TOTAL19)
    finally:
        srv.shutdown(); srv.server_close()

    # ---- S3-T11 单人模式零改动：全动词 404 + 不建表（零 owner 标记写入）----
    multiuser.REG_ATTEMPTS.clear(); multiuser.FAILURES.clear()
    srv, base = serve('0')
    db2 = Path(os.environ[multiuser.DB_PATH_ENV])
    try:
        for method, path, body in (('GET', PROJ, None), ('GET', PROJ + '/p1', None),
                                   ('PUT', PROJ + '/p1', {'content': {'id': 'p1'}, 'updatedAt': T0}),
                                   ('DELETE', PROJ + '/p1', {'deletedAt': T0}),
                                   ('GET', PROJ + '/a/b/c', None)):
            st, _, _ = jreq(path, method, body)
            assert st == 404, f'MULTI_USER=0 {method} {path} must 404, got {st}'
        c = sqlite3.connect(str(db2))
        try:
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            c.close()
        assert 'user_projects' not in tables, f'单人模式不得建 user_projects（零 owner 标记写入）：{sorted(tables)}'
    finally:
        srv.shutdown(); srv.server_close()
    checks.append('S3-T11 单人模式零改动：GET 清单/GET 单项目/PUT/DELETE/深层子路径全部 404，'
                  '且 user_projects 表根本没被建出来（零 owner 标记写入）')
    return checks


def phase_s3_5_b1():
    """S3.5-B1：/api/bootstrap-config 响应加 presetVersion（文件字节 sha1 前 8 位现算）+ 模板文件入库。"""
    import hashlib
    checks=[]
    preset = Path(server.ROOT)/'config'/'channel-config.json'
    assert preset.is_file(), f'template must be committed to the repo at {preset}'
    original = preset.read_bytes()
    file_doc = json.loads(original.decode('utf-8'))
    want_ver = hashlib.sha1(original).hexdigest()[:8]
    srv,base=serve('0')
    try:
        st,body=req(base,'/api/bootstrap-config')
        assert st==200, f'bootstrap-config must 200, got {st}'
        data=json.loads(body)
        assert set(data)==set(file_doc)|{'presetVersion'}, f'body keys must be template keys + presetVersion, got {sorted(data)}'
        assert data['channels']==file_doc['channels'], 'channels must equal the repo template file (原样透出，老消费方只读 .channels)'
        assert len(data['channels'])==3, f'template must hold the 3 channels (聚鑫/SDGF/Ollama Cloud), got {len(data["channels"])}'
        ver=data['presetVersion']
        assert isinstance(ver,str) and len(ver)==8 and all(c in '0123456789abcdef' for c in ver), f'presetVersion must be 8-char lowercase hex, got {ver!r}'
        assert ver==want_ver, f'presetVersion must be sha1(file bytes)[:8]: want {want_ver}, got {ver}'
        assert 'apiKey' not in body.decode('utf-8'), 'template response must carry zero apiKey (no key in repo or wire)'
        assert json.loads(req(base,'/api/bootstrap-config')[1])['presetVersion']==ver, 'version must be stable for an unchanged file'
        checks.append('bootstrap-config: 200 body = template channels (原样, 3 渠道) + presetVersion=sha1(file bytes)[:8] (8 位小写 hex, 稳定), 全响应零 apiKey')
    finally: srv.shutdown(); srv.server_close()
    srv,base=serve('0')
    try:
        # 现算口径：文件一改版本即变，零维护（临时改写后立即还原原字节）
        mutated=dict(file_doc); mutated['channels']=file_doc['channels'][:1]
        preset.write_text(json.dumps(mutated,ensure_ascii=False),encoding='utf-8')
        try:
            st,body=req(base,'/api/bootstrap-config')
            assert st==200, f'mutated template must still 200, got {st}'
            v2=json.loads(body)['presetVersion']
            assert v2==hashlib.sha1(preset.read_bytes()).hexdigest()[:8] and v2!=want_ver, f'presetVersion must be recomputed per response: {v2} vs {want_ver}'
        finally: preset.write_bytes(original)
        checks.append('bootstrap-config: presetVersion 每次响应现算（改文件即变，无需维护版本号）')
        # 文件缺失：404 「no preset」旧行为逐字保持
        hidden=preset.with_name('channel-config.json.probe-hidden')
        preset.rename(hidden)
        try:
            st,b=req(base,'/api/bootstrap-config')
            assert st==404 and b'no preset' in b, f'missing template must stay 404 "no preset", got {st} {b[:80]}'
        finally: hidden.rename(preset)
        assert preset.read_bytes()==original, 'probe must restore the template file byte-exactly'
        checks.append('bootstrap-config: 文件缺失仍 404「no preset」（旧行为不变）；探针按原字节还原模板')
    finally: srv.shutdown(); srv.server_close()
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
  # DELETE 清空该用户渠道集；紧随其后的 GET 会触发 S4 懒播种（0 条 → 种回三条预置，无 key）
  st,_=req(base,CHANNELS,'DELETE',opener=opA)
  assert st==200, f'DELETE must 200, got {st}'
  after=json.loads(req(base,CHANNELS,opener=opA)[1])['channels']
  assert len(after)==3 and all(c.get('isPreset') is True for c in after), \
      f'after DELETE the set must be exactly the 3 seeded presets, got {[c.get("name") for c in after]}'
  assert 'ch_1' not in {c['id'] for c in after}, 'DELETE must physically remove the user-created channels'
  checks.append('DELETE clears the user channel set (subsequent GET re-seeds the 3 presets only; user channels physically gone); POST -> 405; anon PUT/DELETE still 401')
 finally: srv.shutdown(); srv.server_close()

 # ---------------- S3.9-B0：isPreset 探针（惰性播种 GET 下发 isPreset，自建渠道 False） ----------------
 srv,base=serve('1')
 opP=build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
 try:
  assert req(base,'/api/auth/register','POST',{'username':'preset_probe','password':'password1'},opP)[0]==200
  # ① 新用户首 GET 触发播种：恰好 3 条，全 isPreset=True，apiKey 空串
  st,body=req(base,CHANNELS,opener=opP); chans=json.loads(body)['channels']
  assert st==200 and len(chans)==3, f'seed GET should yield 3 preset channels, got {st} {len(chans)}'
  assert all(c['isPreset'] is True for c in chans), f'all seeded channels must be isPreset=True: {chans}'
  assert all(c['apiKey']=='' for c in chans), f'seeded channels must have empty apiKey: {chans}'
  checks.append('S3.9-B0: fresh-user GET seeds exactly 3 preset channels, all isPreset=True with empty apiKey')
  # ② 同一用户 PUT 一条自建渠道（PUT 为整集全量替换；预置行不可删 → 服务端保留 payload 里没带的预置）
  st,b=req(base,CHANNELS,'PUT',{'channels':[{'id':'ch_mine','name':'自建一号','baseUrl':PUB,'apiFormat':'openai','apiKey':KEY}]},opP)
  assert st==200, f'PUT own channel failed: {st} {b}'
  chans=json.loads(req(base,CHANNELS,opener=opP)[1])['channels']
  mine=[c for c in chans if c['name']=='自建一号']
  assert len(mine)==1 and mine[0]['isPreset'] is False, f'own channel must be isPreset=False: {mine}'
  assert sum(1 for c in chans if c['isPreset'] is True)==3, f'the 3 presets must survive a PUT that omits them: {chans}'
  checks.append('S3.9-B0: user-added channel isPreset=False; presets survive a full-replacement PUT that omits them (un-deletable via API)')
 finally: srv.shutdown(); srv.server_close()
 checks += phase_b2()
 checks += phase_admin()
 checks += phase_s2_6()
 checks += phase_s2_7()
 checks += phase_s3_b1()
 checks += phase_s3_5_b1()
 print('SUMMARY: PASS %d/%d'%(len(checks),len(checks)))
 for c in checks: print('PASS: '+c)
 return 0
if __name__=='__main__': raise SystemExit(main())
