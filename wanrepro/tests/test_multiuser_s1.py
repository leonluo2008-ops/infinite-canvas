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
from wanrepro.ui import multiuser, server, channel_store
CHANNELS='/api/user/channels'
def enc_of(db, cid):
 # 直读 SQLite 取密文原件（WAL 已提交，另一连接可见）：断言「密文没被 masked 回写覆盖」
 c=sqlite3.connect(str(db))
 try:
  r=c.execute('SELECT api_key_enc FROM user_channels WHERE id=?',(cid,)).fetchone()
  return bytes(r[0]) if r and r[0] else None
 finally: c.close()
def serve(flag):
 os.environ['MULTI_USER']=flag; os.environ['WAN_TOKEN']='regression-token'; fd,path=tempfile.mkstemp(); os.close(fd); os.unlink(path); os.environ[multiuser.DB_PATH_ENV]=path
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
 print('SUMMARY: PASS %d/%d'%(len(checks),len(checks)))
 for c in checks: print('PASS: '+c)
 return 0
if __name__=='__main__': raise SystemExit(main())
