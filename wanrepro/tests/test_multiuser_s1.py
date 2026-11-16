from __future__ import annotations
import base64, http.cookiejar, json, os, sqlite3, sys, tempfile, threading, time
import urllib.request
from urllib.error import HTTPError
from urllib.request import Request, build_opener, HTTPCookieProcessor
from http.server import ThreadingHTTPServer
from pathlib import Path
import sys; sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
# server.py 的 TOKEN 在 import 期读 WAN_TOKEN，必须先设 env 再 import，否则 gate 断言恒真
os.environ['WAN_TOKEN']='regression-token'
# S2-B1：先立「没有主密钥 env」的基线——无 key 时 MULTI_USER=0 必须照常启动（B-03 回归）
os.environ.pop('WANREPRO_CHANNEL_KEY', None)
from wanrepro.ui import multiuser, server
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
  checks.append('MULTI_USER=0: GET/POST/PUT/DELETE %s all 404; boots with no WANREPRO_CHANNEL_KEY; cryptography never imported'%CHANNELS)
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
 print('SUMMARY: PASS %d/%d'%(len(checks),len(checks)))
 for c in checks: print('PASS: '+c)
 return 0
if __name__=='__main__': raise SystemExit(main())
