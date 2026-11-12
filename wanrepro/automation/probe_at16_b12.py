#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AT-16 B12 后端探针：参考图公网化**死链不放行**（图床全挂 = 本地前置拒绝，零渠道请求）。

跑法：wanrepro/ 下 `python -m automation.probe_at16_b12`。

缺陷 D12（所有者 2026-11-12 判定「图片没有上传」，主代理实测证实）：auto-0043 提交被渠道
403 fail_to_fetch_task 拒、思念云后台零任务。两个图源全挂——uguu /upload 404、
降级隧道 vc-t.aistar.work HTTP 530。旧 store_public_image 只要隧道拼出的 URL 以 https
开头就放行（**只查格式不查可达性**），死链发给渠道 → 渠道拉图失败拒创建。

本探针锁四态（卡片验收 ①②③④）+ 图源自述，全部走**真路**（进程内起真 HTTP 服务端、
真 generate.do_submit、真 bridge 客户端），只把「外部世界」换成可控夹具：

  [1] 双图床全挂 → 502 + stage=host_images + 人话指引，且渠道创建端点**一次都没被调用**
  [2] 拿到 URL 但拉不到（530 / 200 但 0 字节 / 404）→ 验活拦截，不放行
  [3] 验活通过 → 正常提交链（真 do_submit + 真 run 端点；渠道拿到的是验活过的直链）
  [4] 失败路径落账：state=awaiting_approval / gen_error 带逐图源自述 /
      asset_host=这次试过的图源 / dispatch 与 submit_unknown 都不留（可改后重提 ≠ 结果未知）
  [5] 第三图源（腾讯云 videocache）：没配凭证→人话里如实写「已跳过」（不静默）；
      配了→PUT 真发出、直链验活过了才用、来源如实标 tencent-videocache
  [6] 真实数据根对账 + 夹具只删自建 + UPLOADS 打桩还原

守卫（硬要求）：夹具只用 auto-9831..auto-9839（隔离根内）；清理只走
probe_guard.cleanup_created（禁裸 rmtree）；跑后 assert_no_loss。真实任务零写入。
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]          # wanrepro/
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ui"))                 # hub_api.py / server.py 在 ui/ 下

from automation import bridge, config, probe_guard, replace, state  # noqa: E402

RESULTS: list[tuple[str, bool]] = []

PNG_B12 = (bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c626001000000ffff03000006000557bfabd4"
    "0000000049454e44ae426082"))

TASK_CHAIN_OK = "auto-9831"      # [3] 验活通过 → 正常提交链
TASK_CHAIN_DOWN = "auto-9832"    # [4] 双挂 → 前置拒绝 + 失败路径落账
TASK_TENCENT = "auto-9833"       # [5] 第三图源可用
TASK_TENCENT_DEAD = "auto-9834"  # [5] 第三图源直链死
FIXTURE_IDS = (TASK_CHAIN_OK, TASK_CHAIN_DOWN, TASK_TENCENT, TASK_TENCENT_DEAD)
BRIDGE_PREFIX = "probe-at16b12-"

PROMPT_TEXT = ("镜头一：产品特写，冷光，水珠沿瓶身滑落，慢推。\n" * 12)
SDGF_GEN = {"baseUrl": "https://sdgf.probe.example.com", "apiFormat": "sdgf",
            "渠道名": "probe-SDGF", "model": "SD2.5-满血-GF-720P", "mode": "frames"}
SDGF_PARAMS = {"duration": 5, "ratio": "9:16", "resolution": "720", "audio": True}
FAKE_KEY = "sk-" + "probe-" + "AT16B12" + "-5a1c9f30"


def check(name: str, cond: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" —— {detail}" if detail and not cond else ""))
    return bool(cond)


def summary() -> int:
    fails = [n for n, ok in RESULTS if not ok]
    print(f"\n[probe-at16-b12] {len(RESULTS) - len(fails)}/{len(RESULTS)} PASS")
    for n in fails:
        print(f"  FAIL {n}")
    return 1 if fails else 0


# ---------------------------------------------------------------- 外部世界的夹具
class FakeNet:
    """假图床/假直链（进程内 HTTP）：530 死链 / 200 空体 / 404 / 活的 200 / WebDAV PUT。"""

    def __init__(self) -> None:
        outer = self
        self.hits: list[tuple[str, str]] = []
        self.puts: list[tuple[str, str, bytes]] = []

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):        # 不污染探针输出
                return

            def _send(self, code: int, body: bytes = b"") -> None:
                self.send_response(code)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body and self.command != "HEAD":
                    self.wfile.write(body)

            def _route(self) -> None:
                outer.hits.append((self.command, self.path))
                p = self.path.split("?")[0]
                if p.startswith("/dead/"):
                    self._send(530)                       # CF 隧道挂：真实形态
                elif p.startswith("/gone/"):
                    self._send(404)                       # 文件不在
                elif p.startswith("/empty/"):
                    self._send(200, b"")                  # 200 但 0 字节 = 文件不在
                elif p.startswith("/ok/"):
                    self._send(200, b"\x89PNG\r\n\x1a\nPROBE-B12")
                elif p.startswith("/upload/"):
                    n = int(self.headers.get("Content-Length") or 0)
                    blob = self.rfile.read(n) if n else b""
                    outer.puts.append((p, self.headers.get("X-Token") or "", blob))
                    self._send(201)
                else:
                    self._send(404)

            def do_GET(self):      # noqa: N802
                self._route()

            def do_HEAD(self):     # noqa: N802
                self._route()

            def do_PUT(self):      # noqa: N802
                self._route()

            def do_POST(self):     # noqa: N802
                self._route()

        self.port = probe_guard.free_port()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), H)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "FakeNet":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class AppServer:
    """进程内起真 ui/server.py Handler —— 补丁（图床打桩）在同进程内即时生效，走真路由与真错误体。"""

    def __init__(self) -> None:
        self.port = probe_guard.free_port()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "AppServer":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


PROBE_HTTPS = "https://b12.probe.invalid/"     # 假公网域名：只换传输层，不换判定逻辑


class HttpsShim:
    """把 PROBE_HTTPS 的请求转到本机假图床 —— 让「渠道只收 https」这条合同照样成立。

    生产环境这里是真的 TLS 出站；探针不签证书、不出网，只把这一层换成本地 http。
    """

    def __init__(self, net_base: str) -> None:
        self.net = net_base.rstrip("/")
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def open(self, req, timeout=None, **kw):      # noqa: ANN001 —— 与 urllib opener 同形
        url = str(req.full_url)
        if not url.startswith(PROBE_HTTPS):
            raise urllib.error.URLError(f"探针只放行 {PROBE_HTTPS}，收到 {url[:60]}")
        local = self.net + "/" + url[len(PROBE_HTTPS):]
        mapped = urllib.request.Request(local, method=req.get_method(),
                                        headers=dict(req.header_items()))
        return self.opener.open(mapped, timeout=timeout)


# ---------------------------------------------------------------- 任务夹具
CREATED: set[str] = set()
BEFORE_SET: frozenset[str] = frozenset()


def make_task(local_id: str, ref: str, **extra) -> Path:
    """approved + v2 快照自洽 + 一张已确认资产（本地引用）——提交时会被装配成 dataUrl。"""
    tdir = Path(config.PROJECTS_DIR) / local_id
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / "提示词.md").write_text(PROMPT_TEXT, encoding="utf-8")
    params, ch = dict(SDGF_PARAMS), dict(SDGF_GEN)
    snap = state.snapshot_hash_v2(PROMPT_TEXT, params, ch)
    task = {"local_id": local_id, "bridge_id": BRIDGE_PREFIX + local_id,
            "created_at": "2026-01-01T00:00:00+08:00", "state": "approved",
            "history": [{"at": "2026-01-01T00:00:00+08:00", "from": None, "to": "approved",
                         "note": "probe-at16-b12 夹具"}],
            "intent": "AT-16 B12 图床死链探针夹具",
            "gen_params": params, "gen_channel": ch,
            "approval_snapshot": snap, "snapshot_version": 2,
            "approval": {"snapshot_sha256": snap, "at": "2026-01-01T00:00:00+08:00", "by": "probe"},
            "assets_confirmed": [{"no": 1, "img": "ref.png", "url": ref,
                                  "name": "探针参考图", "desc": "", "decision": "replace"}],
            "empty_confirm": True}
    task.update(extra)
    state.save(tdir, task)
    CREATED.add(local_id)
    return tdir


def load(tdir: Path) -> dict:
    return json.loads((tdir / "任务.json").read_text(encoding="utf-8"))


def _run_payload(data_url: str, confirm: bool = True) -> dict:
    return {"prompt": "ok",
            "images": [{"name": "ref.png", "dataUrl": data_url}],
            "duration": 5, "ratio": "9:16", "resolution": "720", "audio": True,
            "confirm": confirm,
            "channel": {"baseUrl": "https://sdgf.probe.example.com", "apiKey": FAKE_KEY,
                        "model": "SD2.5-满血-GF-720P"}}


def _post_run(app_base: str, payload: dict) -> tuple[int, dict]:
    code, body = probe_guard.http_req(app_base, "/api/sdgf/video/run", method="POST",
                                      data=json.dumps(payload).encode("utf-8"),
                                      ctype="application/json", timeout=120)
    return code, (body if isinstance(body, dict) else {})


def main() -> int:
    real_projects = Path(config.PROJECTS_DIR)
    real_before = probe_guard.projects_snapshot(real_projects)
    global BEFORE_SET
    BEFORE_SET = real_before
    side_root = replace.sidecar_path("__probe_probe__").parent

    iso_root = Path(tempfile.mkdtemp(prefix="wanprobe-at16b12-",
                                    dir=os.environ.get("TMPDIR") or tempfile.gettempdir()))
    iso_projects = iso_root / "projects"
    iso_projects.mkdir(parents=True, exist_ok=True)
    iso_uploads = iso_root / "uploads"
    iso_uploads.mkdir(parents=True, exist_ok=True)

    config.PROJECTS_DIR = iso_projects          # 必须早于 import hub_api / server（import 期求值）
    import hub_api  # noqa: E402
    import server   # noqa: E402
    from automation import generate, local_assets, submit_view  # noqa: E402
    globals().update({"server": server, "hub_api": hub_api, "generate": generate,
                      "local_assets": local_assets, "submit_view": submit_view})

    real_uploads = server.UPLOADS
    real_patches = (server.PUBLIC_IMAGE_TUNNEL, server.VC_UPLOAD_BASE, server.VC_PUBLIC_BASE,
                    server.upload_uguu_bytes, server.sdgf_create, server.sdgf_run_job,
                    server._VERIFY_OPENERS)
    real_bridge_base, real_get, real_get_bytes = config.BRIDGE_BASE, bridge.get, bridge.get_bytes
    real_vc_token = os.environ.get("TENCENT_VC_TOKEN")
    net = app = None

    class Chan:
        """渠道创建端点桩：只记账，绝不出网（零扣费）。"""

        def __init__(self) -> None:
            self.calls: list[dict] = []

        def create(self, base: str, key: str, body: dict) -> str:
            self.calls.append(dict(body))
            return "cloud-b12-1"

    chan = Chan()
    patched = False

    try:
        check("[0] 隔离根就位：hub_api.TASKS_DIR == 探针隔离根（import 期求值生效）",
              Path(hub_api.TASKS_DIR).resolve() == iso_projects.resolve(),
              f"TASKS_DIR={hub_api.TASKS_DIR} 隔离根={iso_projects}")
        server.UPLOADS = iso_uploads
        server.sdgf_create = chan.create               # 渠道创建：桩（记账、不出网）
        server.sdgf_run_job = lambda *a, **k: None     # 建单后的轮询线程：本探针不测
        net, app = FakeNet().start(), AppServer().start()
        server._VERIFY_OPENERS = (HttpsShim(net.base),)   # 验活走「https」→ 本机假图床
        data_url = "data:image/png;base64," + base64.b64encode(PNG_B12).decode("ascii")

        # ── [1] 双图床全挂 → 前置拒绝 + 人话指引 + 零外发 ─────────────────────
        print("1) 双图床全挂（uguu 404 + 隧道 530）→ 前置拒绝、渠道零请求")
        os.environ.pop("TENCENT_VC_TOKEN", None)       # 未配第三图源：必须如实说「已跳过」
        server.upload_uguu_bytes = lambda data, name: ""
        server._UGUU_LAST_ERROR = "HTTP 404"
        server.PUBLIC_IMAGE_TUNNEL = f"{PROBE_HTTPS}dead/"
        chan.calls.clear()
        code, body = _post_run(app.base, _run_payload(data_url))
        err = str(body.get("error") or "")
        check("[1] 双挂 → HTTP 502 + stage=host_images（机器可读，不靠文案）",
              code == 502 and body.get("stage") == "host_images",
              f"code={code} body={json.dumps(body, ensure_ascii=False)[:200]}")
        check("[1] 人话指引三要素：图床不可用 / 参考图没有公网地址 / 没发渠道请求",
              "图床不可用" in err and "参考图没有公网地址" in err and "没有向渠道发任何请求" in err,
              err[:200])
        check("[1] 逐图源自述：uguu 上传失败（HTTP 404）+ 隧道 直链验活失败（HTTP 530）",
              "uguu 上传失败" in err and "HTTP 404" in err and "HTTP 530" in err, err[:240])
        check("[1] 未配腾讯云凭证 → 如实写「已跳过」，不静默",
              "未配置 TENCENT_VC_TOKEN，已跳过" in err, err[:240])
        check("[1] 渠道创建端点**零调用**（本地前置拒绝：零外发、零扣费）",
              chan.calls == [], f"被调用了 {len(chan.calls)} 次")
        check("[1] 错误体带结构化自述（sources 两条 + skipped 一条）",
              len(body.get("sources") or []) == 2 and len(body.get("skipped") or []) == 1,
              json.dumps({"sources": body.get("sources"), "skipped": body.get("skipped")},
                         ensure_ascii=False)[:240])

        # ── [2] 拿到 URL 但拉不到 → 验活拦截不放行 ──────────────────────────
        print("2) 图床返回「格式对但拉不到」的 URL → 验活拦截")
        ok_dead, why_dead = server.image_url_alive(f"{PROBE_HTTPS}dead/x.png")
        check("[2] 验活：530 死链 → 不可达（HTTP 530）", ok_dead is False and why_dead == "HTTP 530", why_dead)
        ok_empty, why_empty = server.image_url_alive(f"{PROBE_HTTPS}empty/x.png")
        check("[2] 验活：200 但 0 字节 → 判死（文件不在）", ok_empty is False and "0 字节" in why_empty, why_empty)
        ok_gone, why_gone = server.image_url_alive(f"{PROBE_HTTPS}gone/x.png")
        check("[2] 验活：404 → 判死（HTTP 404）", ok_gone is False and why_gone == "HTTP 404", why_gone)
        ok_live, why_live = server.image_url_alive(f"{PROBE_HTTPS}ok/x.png")
        check("[2] 验活：真能取到字节 → 可达（HTTP 200）", ok_live is True and why_live == "HTTP 200", why_live)

        server.upload_uguu_bytes = lambda data, name: f"{PROBE_HTTPS}gone/ref.png"   # 给了 URL，文件不在
        chan.calls.clear()
        code, body = _post_run(app.base, _run_payload(data_url))
        err = str(body.get("error") or "")
        check("[2] uguu 返回的 URL 拉不到（404）→ 验活拦下，不放行给渠道",
              code == 502 and "HTTP 404" in err and chan.calls == [],
              f"code={code} err={err[:160]}")

        server.upload_uguu_bytes = lambda data, name: ""
        server._UGUU_LAST_ERROR = "HTTP 404"
        server.PUBLIC_IMAGE_TUNNEL = f"{PROBE_HTTPS}empty/"          # 200 空体 = 文件不在
        chan.calls.clear()
        code, body = _post_run(app.base, _run_payload(data_url))
        err = str(body.get("error") or "")
        check("[2] 隧道返回 200 但 0 字节 → 同样拦下（不当活人）",
              code == 502 and "0 字节" in err and chan.calls == [], f"code={code} err={err[:160]}")

        # ── [3] 验活通过 → 正常提交链 ──────────────────────────────────────
        print("3) 验活通过 → 正常提交链（真 do_submit + 真 run 端点）")
        server.upload_uguu_bytes = lambda data, name: f"{PROBE_HTTPS}ok/ref.png"
        server.PUBLIC_IMAGE_TUNNEL = f"{PROBE_HTTPS}dead/"
        ref = local_assets.store(TASK_CHAIN_OK, PNG_B12, "ref.png")
        tdir_ok = make_task(TASK_CHAIN_OK, ref)
        config.BRIDGE_BASE = app.base                     # bridge 客户端真打到本探针实例
        bridge.get = (lambda path: {"status": "done", "taskId": "cloud-b12-1"}
                      if "/api/sdgf/video/job/" in path else real_get(path))
        bridge.get_bytes = lambda path, timeout=600: b"PROBE-B12-MP4"
        chan.calls.clear()
        gen_err = ""
        try:
            generate.do_submit(tdir_ok, verbose=False, api_key=FAKE_KEY)
        except generate.GenError as exc:
            gen_err = str(exc)
        except BaseException as exc:                      # noqa: BLE001 —— 如实记，不吞
            gen_err = f"{type(exc).__name__}: {exc}"
        task_ok = load(tdir_ok)
        check("[3] 提交链走通（state=done，无异常）",
              task_ok.get("state") == "done" and not gen_err,
              f"state={task_ok.get('state')} err={gen_err[:160]}")
        check("[3] 渠道只被调用一次，拿到的是验活过的直链",
              len(chan.calls) == 1 and (chan.calls[0].get("images") or []) == [f"{PROBE_HTTPS}ok/ref.png"],
              json.dumps(chan.calls, ensure_ascii=False)[:200])
        check("[3] 成功路径 asset_host = ['uguu']（来源如实落账，不伪称）",
              task_ok.get("asset_host") == ["uguu"], str(task_ok.get("asset_host")))

        # ── [4] 失败路径落账（不是「结果未知」）──────────────────────────────
        print("4) 双挂走完整提交链 → awaiting_approval + gen_error 自述 + asset_host 落账")
        os.environ["TENCENT_VC_TOKEN"] = "probe-vc-token"      # 本机真实状态：第三图源凭证已配
        server.VC_UPLOAD_BASE = f"{net.base}/upload/"           # 上传走明文直连（与生产一致）
        server.VC_PUBLIC_BASE = "https://127.0.0.1:1/vc/"       # https 前缀但拉不到（生产形态）
        server.upload_uguu_bytes = lambda data, name: ""
        server._UGUU_LAST_ERROR = "HTTP 404"
        server.PUBLIC_IMAGE_TUNNEL = f"{PROBE_HTTPS}dead/"
        ref2 = local_assets.store(TASK_CHAIN_DOWN, PNG_B12, "ref.png")
        tdir_down = make_task(TASK_CHAIN_DOWN, ref2)
        chan.calls.clear()
        net.puts.clear()
        gen_err = ""
        try:
            generate.do_submit(tdir_down, verbose=False, api_key=FAKE_KEY)
        except generate.GenError as exc:
            gen_err = str(exc)
        task_down = load(tdir_down)
        check("[4] 抛 GenError 且是人话「图床不可用」", "图床不可用" in gen_err, gen_err[:160])
        check("[4] 状态回 awaiting_approval（受控回退：可改后重提，不是终态失败）",
              task_down.get("state") == "awaiting_approval", str(task_down.get("state")))
        check("[4] dispatch / submit_unknown 都不留（**不许**记成「结果未知」）",
              "dispatch" not in task_down and not task_down.get("submit_unknown"),
              json.dumps({k: task_down.get(k) for k in ("dispatch", "submit_unknown")}, ensure_ascii=False))
        check("[4] gen_error 带逐图源自述（uguu / 隧道 530 / 腾讯云直链验活失败）",
              "uguu 上传失败" in str(task_down.get("gen_error"))
              and "HTTP 530" in str(task_down.get("gen_error"))
              and "腾讯云图床" in str(task_down.get("gen_error"))
              and "直链验活失败" in str(task_down.get("gen_error")),
              str(task_down.get("gen_error"))[:260])
        check("[4] asset_host 落账 = 这次真试过的图源（失败路径也自述）",
              task_down.get("asset_host") == ["uguu", "local-tunnel", "tencent-videocache"],
              str(task_down.get("asset_host")))
        check("[4] 第三图源真被尝试（PUT 真发出，不是写在文案里）", len(net.puts) == 1,
              f"puts={len(net.puts)}")
        check("[4] 渠道创建端点零调用", chan.calls == [], f"{len(chan.calls)} 次")
        view = submit_view.build(task_down, active_submit=False)
        check("[4] submit_view 判 rejected 且不阻断重提（前端形态与后端事实同源）",
              view.get("form") == "rejected" and view.get("blocks_retry") is False,
              json.dumps({k: view.get(k) for k in ("form", "blocks_retry", "dispatch_phase")},
                         ensure_ascii=False))

        # ── [5] 第三图源：腾讯云 videocache ────────────────────────────────
        print("5) 第三图源：配了凭证才走（PUT 真发出、验活过才用），直链死照样拦")
        os.environ["TENCENT_VC_TOKEN"] = "probe-vc-token"
        server.VC_UPLOAD_BASE = f"{net.base}/upload/"      # 上传走明文直连（与生产一致）
        server.VC_PUBLIC_BASE = f"{PROBE_HTTPS}ok/"        # 直链必须 https（渠道合同）
        net.puts.clear()
        got = server.host_image_detailed(PNG_B12, "ref.png")
        check("[5] 腾讯云顶上：来源=tencent-videocache，直链取自 VC_PUBLIC_BASE",
              got.get("source") == "tencent-videocache"
              and str(got.get("url")).startswith(f"{PROBE_HTTPS}ok/"),
              json.dumps({k: got.get(k) for k in ("source", "url")}, ensure_ascii=False)[:200])
        check("[5] WebDAV PUT 真发出（带 X-Token、字节数一致）",
              len(net.puts) == 1 and net.puts[0][1] == "probe-vc-token"
              and len(net.puts[0][2]) == len(PNG_B12),
              f"puts={[(p[0], p[1], len(p[2])) for p in net.puts]}")

        server.VC_PUBLIC_BASE = "https://127.0.0.1:1/vc/"   # 生产形态：https 前缀但拉不到
        net.puts.clear()
        chan.calls.clear()
        code, body = _post_run(app.base, _run_payload(data_url))
        err = str(body.get("error") or "")
        check("[5] 腾讯云直链拉不到 → 照样拦下（不因「配了凭证」就放行）",
              code == 502 and "腾讯云图床" in err and "直链验活失败" in err and chan.calls == [],
              f"code={code} err={err[:200]}")
        check("[5] 这次 PUT 仍真发出（换图源≠假装成功）", len(net.puts) == 1, f"puts={len(net.puts)}")

        # ── [6] 对账与清理 ───────────────────────────────────────────────
        print("6) 真实数据根对账 + 夹具只删自建")
        real_after = probe_guard.projects_snapshot(real_projects)
        try:
            probe_guard.assert_no_loss(real_before, real_after)
            check("[6] 真实项目目录集前后一致（零丢失 + 隔离根外零写入）",
                  real_after == real_before,
                  f"多出 {sorted(set(real_after) - set(real_before))}" if real_after != real_before else "")
        except probe_guard.GuardError as err_guard:
            check("[6] 真实项目目录集前后一致（零丢失 + 隔离根外零写入）", False, str(err_guard))
        for fid in sorted(CREATED):
            ok_del, why = probe_guard.cleanup_created(iso_projects, fid, before=BEFORE_SET, created=CREATED)
            if not ok_del:
                print(f"     清理异常：{why}")
        check("[6] 隔离根夹具全部清干净", not any((iso_projects / fid).exists() for fid in CREATED))
        return summary()
    finally:
        for fid in sorted(CREATED):                    # 兜底：异常路径也别留垃圾（仍只删自建）
            probe_guard.cleanup_created(iso_projects, fid, before=BEFORE_SET, created=CREATED)
        if side_root.is_dir():                          # 本探针建的 sidecar 目录（bridge_id 前缀）
            for d in side_root.glob(BRIDGE_PREFIX + "*"):
                if d.is_dir() and not d.is_symlink():
                    shutil.rmtree(d)
        server.UPLOADS = real_uploads
        (server.PUBLIC_IMAGE_TUNNEL, server.VC_UPLOAD_BASE, server.VC_PUBLIC_BASE,
         server.upload_uguu_bytes, server.sdgf_create, server.sdgf_run_job,
         server._VERIFY_OPENERS) = real_patches
        config.BRIDGE_BASE = real_bridge_base
        bridge.get, bridge.get_bytes = real_get, real_get_bytes
        if real_vc_token is None:
            os.environ.pop("TENCENT_VC_TOKEN", None)
        else:
            os.environ["TENCENT_VC_TOKEN"] = real_vc_token
        for srv in (net, app):
            if srv is not None:
                srv.stop()
        shutil.rmtree(iso_root, ignore_errors=True)
        restored = (server.UPLOADS is real_uploads and config.BRIDGE_BASE == real_bridge_base
                    and os.environ.get("TENCENT_VC_TOKEN") == real_vc_token)
        check("[6] 打桩全部还原（UPLOADS / 图床常量 / bridge 客户端 / 环境变量）", restored)


if __name__ == "__main__":
    raise SystemExit(main())
