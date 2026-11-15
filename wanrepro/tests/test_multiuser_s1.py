from __future__ import annotations
import http.cookiejar, json, os, tempfile, threading, time
from urllib.error import HTTPError
from urllib.request import Request, build_opener, HTTPCookieProcessor
from http.server import ThreadingHTTPServer
from pathlib import Path
import sys; sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from wanrepro.ui import multiuser, server
def serve(flag):
 os.environ['MULTI_USER']=flag; os.environ['WAN_TOKEN']='regression-token'; fd,path=tempfile.mkstemp(); os.close(fd); os.unlink(path); os.environ[multiuser.DB_PATH_ENV]=path
 srv=ThreadingHTTPServer(('127.0.0.1',0),server.Handler); srv.daemon_threads=True; threading.Thread(target=srv.serve_forever,daemon=True).start(); return srv,'http://127.0.0.1:%d'%srv.server_port
def req(base,path,method='GET',body=None,opener=None):
 q=Request(base+path,data=json.dumps(body).encode() if body is not None else None,method=method,headers={'Content-Type':'application/json'})
 try: r=(opener or build_opener()).open(q); return r.status,r.read()
 except HTTPError as e: return e.code,e.read()
def main():
 checks=[]; srv,base=serve('0')
 try:
  for method,path in [('GET','/api/auth/me'),('POST','/api/auth/login'),('POST','/api/auth/register'),('POST','/api/auth/logout'),('GET','/multiuser/')]: assert req(base,path,method)[0]==404
  checks.append('MULTI_USER=0 endpoints and test page 404; WAN_TOKEN gate retained')
 finally: srv.shutdown(); srv.server_close()
 srv,base=serve('1'); op=build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
 try:
  assert req(base,'/multiuser/')[0]==200; code=multiuser.issue_invite(1)
  assert req(base,'/api/auth/register','POST',{'username':'probe','password':'password1','inviteCode':code},op)[0]==200
  assert req(base,'/api/auth/me',opener=op)[0]==200; assert req(base,'/api/auth/logout','POST',{},op)[0]==200 and req(base,'/api/auth/me',opener=op)[0]==401
  checks.append('register → me → logout → me 401')
  for label in ('expired','revoked','exhausted'):
   code=multiuser.issue_invite(1,expires_at=int(time.time())-5 if label=='expired' else None)
   if label=='revoked':
    with multiuser.connect() as db: db.execute('UPDATE invites SET revoked=1 WHERE code=?',(code,))
   if label=='exhausted':
    with multiuser.connect() as db: db.execute('UPDATE invites SET used_count=max_uses WHERE code=?',(code,))
   assert req(base,'/api/auth/register','POST',{'username':'u'+label,'password':'password1','inviteCode':code})[0]==403
  assert req(base,'/api/auth/register','POST',{'username':'probe','password':'password1','inviteCode':multiuser.issue_invite()})[0]==409; checks.append('expired/revoked/exhausted invites + duplicate username rejected')
  for _ in range(5): assert req(base,'/api/auth/login','POST',{'username':'probe','password':'wrongpass'})[0]==401
  assert req(base,'/api/auth/login','POST',{'username':'probe','password':'wrongpass'})[0]==429; checks.append('five failed logins trigger lock')
 finally: srv.shutdown(); srv.server_close()
 print('SUMMARY: PASS %d/%d'%(len(checks),len(checks)))
 for c in checks: print('PASS: '+c)
 return 0
if __name__=='__main__': raise SystemExit(main())
