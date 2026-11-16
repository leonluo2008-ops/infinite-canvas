from __future__ import annotations
import http.cookiejar, json, os, tempfile, threading, time
import urllib.request
from urllib.error import HTTPError
from urllib.request import Request, build_opener, HTTPCookieProcessor
from http.server import ThreadingHTTPServer
from pathlib import Path
import sys; sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
# server.py 的 TOKEN 在 import 期读 WAN_TOKEN，必须先设 env 再 import，否则 gate 断言恒真
os.environ['WAN_TOKEN']='regression-token'
from wanrepro.ui import multiuser, server
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
 print('SUMMARY: PASS %d/%d'%(len(checks),len(checks)))
 for c in checks: print('PASS: '+c)
 return 0
if __name__=='__main__': raise SystemExit(main())
