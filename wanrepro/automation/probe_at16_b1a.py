#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AT-16 B1a 探针：显示换算点端到端（**纯离线**：自起隔离实例 + 真渲染联动，真实任务只读）。

跑法：wanrepro/ 下 `python -m automation.probe_at16_b1a`。
背景缺陷：hub 详情上传资产后图不显示——前端渲染点没过 hubAssetDisplayUrl（显示层唯一换算点），
`local:任务号/asset-uploads/xx.png` 被直接当图片 URL。本探针锁「数据→卡片→渲染」全链：

  [0] 隔离与同源：自起 ui/server.py（临时 WANREPRO_AUTO_DIR + 假 bridge），/api/hub/where
      自述 TASKS_DIR == 探针根，不一致拒跑（probe_hub 同款守卫，防误删真实任务）。
  [1] 上传链：POST /api/asset/upload（带 X-Task-Id）→ local 引用；GET /api/hub/asset-probe/<id>
      对该引用回 state=local 且 display=换算 URL；GET /api/hub/asset-file/<id>/<名> → 200 且
      字节与上传一致（缩略图/大图**真能取到**）。
  [2] 健康态三态不变：local 引用在盘 → local；删盘上文件 → expired；data:image → ok；
      display_url 换算结果与前端 hubAssetDisplayUrl 同源（404/超时族由 probe_at14_b3 [5] 回归守）。
  [3] 数据→卡片→真渲染联动：清单「资产图」列写 local 引用 → cards.build 的 已传图 == [引用]；
      get_task 透出 asset_cards 同值；把**真实上传引用**交给 web/scripts/probe-at16-b1a.mjs
      （--ref/--expect），由 react-dom/server 真渲染受控出口组件，断言 <img src>/<a href>
      都是换算 URL（即浏览器地址栏真正会去取的那个 URL）。
  [4] 自检独立性不动：夹具任务正例（提交快照与发号次序一致）→ 次序对账 PASS 不误红；
      人为污染提交快照（乱序）→ 次序对账仍 FAIL 且 get_task 透出 FAIL。

夹具任务只用 auto-9601..auto-9610（隔离根内，跑完连根删）；真实任务零写入，跑前跑后对账。
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]          # wanrepro/
sys.path.insert(0, str(ROOT))

from automation import config, probe_guard  # noqa: E402

RESULTS: list[tuple[str, bool]] = []
PNG_A = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c626001000000ffff03000006000557bfabd4"
    "0000000049454e44ae426082")
PNG_B = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "b5c3ce790000000d49444154789c62600100000005e2acb630"
    "0000000049454e44ae426082")
TASK_UPLOAD = "auto-9601"       # 上传链夹具（隔离根内）
TASK_CARDS = "auto-9602"        # 卡片+渲染联动夹具
TASK_SELF_OK = "auto-9603"      # 自检正例夹具
TASK_SELF_BAD = "auto-9604"     # 自检污染反例夹具
BRIDGE_FIX = "probe-at16b1a-fix"


def check(name: str, cond: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" —— {detail}" if detail and not cond else ""))
    return bool(cond)


def summary() -> int:
    fails = [n for n, ok in RESULTS if not ok]
    print(f"\n[probe-at16-b1a] {len(RESULTS) - len(fails)}/{len(RESULTS)} PASS")
    for n in fails:
        print(f"  FAIL {n}")
    return 1 if fails else 0


def main() -> int:
    real_root = Path(config.PROJECTS_DIR)
    real_before = probe_guard.projects_snapshot(real_root)
    iso_root = Path(tempfile.mkdtemp(prefix="wanprobe-at16b1a-",
                                     dir=os.environ.get("TMPDIR") or tempfile.gettempdir()))
    iso_projects = iso_root / "projects"
    iso_projects.mkdir(parents=True, exist_ok=True)
    stub = probe_guard.StubBridge()
    port = probe_guard.free_port()
    base = f"http://127.0.0.1:{port}"
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 本机直连不走代理

    # 读写同源：补丁 config 后再懒加载其余模块（cards/selfcheck/hub_api/replace 都以 config 为根）。
    # 注意传 Path（hub_api.TASKS_DIR = config.PROJECTS_DIR 会在 import 期做 `TASKS_DIR / 名` 运算）。
    config.PROJECTS_DIR = iso_projects
    from automation import cards, generate, local_assets  # noqa: E402
    from automation.probe_at9 import (  # noqa: E402  复用既有夹具助手（CK_FULL/渲染/快照注入）
        CK_FULL,
        items_by_name,
        render_fixture_prompt,
        seed_selfcheck_inputs,
        write_checklist,
    )
    import hub_api  # noqa: E402
    from automation import selfcheck  # noqa: E402

    proc = None
    try:
        stub.thread.start()
        env = dict(os.environ, WANREPRO_AUTO_DIR=str(iso_projects), WANREPRO_BRIDGE=stub.base)
        proc = subprocess.Popen(
            [sys.executable, str(ROOT / "ui" / "server.py"), "--port", str(port), "--no-browser"],
            cwd=str(ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace")
        where = None
        for _ in range(200):
            if proc.poll() is not None:
                break
            code, payload = probe_guard.http_req(base, "/api/hub/where", timeout=5)
            if code == 200 and isinstance(payload, dict):
                where = payload
                break
            time.sleep(0.3)
        try:
            probe_guard.assert_same_root(where or {}, iso_projects)
            check("[0] 隔离实例起来且读写同源（/api/hub/where == 探针根）", where is not None,
                  f"{base} 无响应" if where is None else str(where)[:120])
            print(f"     数据根 {where.get('tasks_dir')}")
        except probe_guard.GuardError as err:
            check("[0] 隔离实例起来且读写同源（/api/hub/where == 探针根）", False, str(err))
            print("[probe-at16-b1a] 拒跑：数据根不同源，后续写入/清理一律不执行。")
            return summary()

        # ── [1] 上传链 ─────────────────────────────────────────
        print("1) 上传链：X-Task-Id 上传 → local 引用 → asset-probe/asset-file 真取图")
        probe_make_task(TASK_UPLOAD, "awaiting_assets", "probe-at16b1a-up")
        # 前端同款形态：原始字节直传（Content-Type octet-stream + X-Filename/X-Task-Id 头），
        # /api/asset/upload 按 raw body 收（multipart 是 /api/video/upload 的事，别混）。
        req = urllib.request.Request(
            base + "/api/asset/upload", data=PNG_A, method="POST",
            headers={"Content-Type": "application/octet-stream",
                     "X-Filename": urllib.parse.quote("b1a.png", safe=""),
                     "X-Task-Id": urllib.parse.quote(TASK_UPLOAD, safe="")})
        resp = json.loads(op.open(req, timeout=30).read())
        ref = str(resp.get("local") or "")
        display = str(resp.get("url") or "")
        check("[1] 上传回 source=local 且是合法 local 引用",
              resp.get("source") == "local" and local_assets.is_ref(ref), json.dumps(resp, ensure_ascii=False)[:160])
        check("[1] 回包 url 即换算 URL（/api/hub/asset-file/...）",
              display == local_assets.display_url(ref) and display.startswith("/api/hub/asset-file/"),
              f"ref={ref} display={display}")

        # 前端真实时序：上传成功 → postHubAsset 把引用写进任务（流水/清单）。按流水合同种上，
        # asset-probe 才有真实扫描源（服务端只扫 pending_replace / assets_confirmed 两个来源）。
        tj = iso_projects / TASK_UPLOAD / "任务.json"
        task = json.loads(tj.read_text(encoding="utf-8"))
        task["pending_replace"] = [{"no": 1, "url": ref}]
        tj.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")

        code, payload = probe_guard.http_req(base, f"/api/hub/asset-probe/{TASK_UPLOAD}", "POST", b"{}",
                                             "application/json", timeout=30)
        rows = [r for r in ((payload or {}).get("refs") or []) if isinstance(r, dict) and r.get("ref") == ref]
        check("[1] asset-probe：真实引用 state=local（健康态注入源不误报）",
              code == 200 and len(rows) == 1 and rows[0].get("state") == "local",
              f"{code} {json.dumps(payload, ensure_ascii=False)[:200]}")
        check("[1] asset-probe：display 与前端换算点同源（ref 原串一并带回，供按原串查键）",
              bool(rows) and rows[0].get("display") == display and rows[0].get("ref") == ref,
              json.dumps(rows[:1], ensure_ascii=False))

        code, got = probe_guard.http_req(base, display, timeout=30)
        check("[1] GET 换算 URL → 200 且字节与上传一致（缩略图/大图真能取到）",
              code == 200 and got == PNG_A, f"{code} len={len(got) if isinstance(got, bytes) else 'n/a'}")

        # ── [2] 三态不变（local / expired / data-image ok；http 404·超时族由 probe_at14_b3 [5] 回归守）──
        print("2) 健康态三态不变：local=在盘、expired=删盘、data:image=ok；display_url 换算不动")
        check("[2] 在盘 local 引用 → ref_state=local", generate.ref_state(ref) == "local", ref)
        _lid, name = local_assets.parse_ref(ref)
        (iso_projects / TASK_UPLOAD / "asset-uploads" / name).unlink()
        check("[2] 删盘上文件 → ref_state=expired（不猜本地副本）", generate.ref_state(ref) == "expired", ref)
        data_ref = "data:image/png;base64,iVBORw0KGgo="
        check("[2] data:image 引用 → ref_state=ok", generate.ref_state(data_ref) == "ok", data_ref)
        check("[2] data:image 引用不是 local 引用（前端换算点对它原样透传，渲染层已由 node 探针覆盖）",
              not local_assets.is_ref(data_ref))
        check("[2] 删盘后重存同字节 → 同名引用恢复（内容寻址幂等，后续夹具可用）",
              local_assets.store(TASK_UPLOAD, PNG_A, "b1a.png") == ref)

        # ── [3] 数据→卡片→真渲染联动 ──────────────────────────
        print("3) 数据→卡片→真渲染：清单写 local 引用 → 已传图原串 → 真渲染出换算 URL")
        ref2 = local_assets.store(TASK_CARDS, PNG_B, "b1a-b.png")   # 第二个引用（#11 行）
        write_checklist(BRIDGE_FIX, CK_FULL.format(bridge=BRIDGE_FIX, name1="", img1=ref, img11=ref2))
        tdir_cards = probe_make_task(TASK_CARDS, "awaiting_assets", BRIDGE_FIX)
        got_cards = cards.build(BRIDGE_FIX, TASK_CARDS)
        by_n = {c.get("n"): c for c in got_cards}
        check("[3] cards.build：#1 已传图 == [local 引用原串]（数据层不存换算 URL）",
              by_n.get(1, {}).get("已传图") == [ref], str(by_n.get(1, {}).get("已传图")))
        check("[3] cards.build：#11 已传图 == [第二个 local 引用]",
              by_n.get(11, {}).get("已传图") == [ref2], str(by_n.get(11, {}).get("已传图")))
        detail = hub_api.get_task(TASK_CARDS)
        api_cards = {c.get("n"): c for c in (detail.get("asset_cards") or [])}
        check("[3] get_task 透出 asset_cards 已传图同值（前端拿到的是原串）",
              api_cards.get(1, {}).get("已传图") == [ref], str(api_cards.get(1, {}).get("已传图"))[:160])

        web_dir = ROOT.parent / "web"
        node = shutil.which("node")
        if not node:
            check("[3] node 真渲染联动（web/scripts/probe-at16-b1a.mjs --ref）", False, "PATH 里没有 node")
        else:
            run = subprocess.run(
                [node, "scripts/probe-at16-b1a.mjs", "--ref", ref,
                 "--expect", local_assets.display_url(ref)],
                cwd=str(web_dir), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=420)
            check("[3] 真渲染联动：受控出口组件对真实上传引用渲染出换算 URL（rc=0）",
                  run.returncode == 0,
                  (run.stdout or "")[-600:] + (run.stderr or "")[-300:])
            print("     node 探针：" + " / ".join(
                line.strip() for line in (run.stdout or "").splitlines() if line.strip().startswith("probe-at16-b1a:")))

        # ── [4] 自检独立性不动 ─────────────────────────────────
        print("4) 自检独立性：正例不误红；人为污染提交快照（乱序）→ 次序对账仍 FAIL")
        prompt, mapping = render_fixture_prompt(BRIDGE_FIX, {1: ref, 11: ref2})
        check("[4] 夹具发号映射 = [(1,1),(11,2)]（发号跟已传图，行号≠图号）",
              mapping == [(1, 1), (11, 2)], str(mapping))
        tdir_ok = probe_make_task(TASK_SELF_OK, "awaiting_approval", BRIDGE_FIX)
        seed_selfcheck_inputs(tdir_ok, prompt, submitted=[{"no": 1, "url": ref}, {"no": 11, "url": ref2}])
        by_ok = items_by_name(selfcheck.run_selfcheck(tdir_ok))
        check("[4] 正例（快照与发号同序）→ 次序对账 PASS（不误红）",
              by_ok.get("次序对账", {}).get("结论") == "PASS", str(by_ok.get("次序对账"))[:200])

        tdir_bad = probe_make_task(TASK_SELF_BAD, "awaiting_approval", BRIDGE_FIX)
        seed_selfcheck_inputs(tdir_bad, prompt, submitted=[{"no": 11, "url": ref2}, {"no": 1, "url": ref}])
        by_bad = items_by_name(selfcheck.run_selfcheck(tdir_bad))
        check("[4] 污染提交快照（乱序）→ 次序对账 FAIL（自检仍独立读实际快照）",
              by_bad.get("次序对账", {}).get("结论") == "FAIL" and "应为图1" in str(by_bad.get("次序对账", {}).get("证据", "")),
              str(by_bad.get("次序对账"))[:240])
        detail_bad = hub_api.get_task(TASK_SELF_BAD)
        rows_bad = [x for x in (detail_bad.get("自检") or []) if x.get("项") == "次序对账"]
        check("[4] get_task 透出污染后的次序对账 FAIL（前端红条数据源不动）",
              len(rows_bad) == 1 and rows_bad[0].get("结论") == "FAIL", str(rows_bad)[:200])

        return summary()
    finally:
        if proc is not None:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
            else:
                out = (proc.stdout.read() or "")[-400:] if proc.stdout else ""
                if out.strip():
                    print(f"     [服务端尾日志] {out.strip()[-300:]}")
        with contextlib.suppress(Exception):
            stub.httpd.shutdown()
            stub.httpd.server_close()
        # 清理：自建夹具（隔离根连根删 + analysis 里的夹具桥目录）
        shutil.rmtree(iso_root, ignore_errors=True)
        analysis = ROOT / "analysis" / "reverse" / BRIDGE_FIX
        shutil.rmtree(analysis, ignore_errors=True)
        real_after = probe_guard.projects_snapshot(real_root)
        ok = real_after == real_before
        print(f"[probe-at16-b1a] 真实 projects {'未动' if ok else '有变化'}："
              f"before={sorted(real_before)} after={sorted(real_after)}")
        if not ok:
            RESULTS.append(("真实 projects 零变化", False))


def probe_make_task(local_id: str, state_name: str, bridge_id: str) -> Path:
    """B2 同款任务夹具（隔离根内，跑完连根删）。与 probe_at9.make_task 同形，避免跨探针耦合 config 捕获。"""
    from automation import config as _config
    tdir = Path(_config.PROJECTS_DIR) / local_id
    shutil.rmtree(tdir, ignore_errors=True)
    (tdir / "inbox").mkdir(parents=True, exist_ok=True)
    task = {
        "local_id": local_id, "bridge_id": bridge_id,
        "created_at": "2026-01-01T00:00:00+08:00", "state": state_name,
        "history": [{"at": "2026-01-01T00:00:00+08:00", "from": None, "to": state_name,
                     "note": "probe-at16b1a 夹具"}],
        "intent": "AT-16 B1a 探针夹具", "assets": [],
        "ingestion": {"zip_sha256": f"probe-{local_id}", "zip_name": "probe",
                      "video_name": "", "text_name": "", "ignored": []},
    }
    (tdir / "任务.json").write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
    return tdir


if __name__ == "__main__":
    raise SystemExit(main())
