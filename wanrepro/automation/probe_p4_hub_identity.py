#!/usr/bin/env python
"""P4 hub 身份探针（B2/P1c 并入）：隔离实例里验「实例→hub 任务→正文→自检→审批快照→approve/rollback」同一实例同一修订。

跑法（wanrepro/ 下）：
    python -m automation.probe_p4_hub_identity
    python -m automation.probe_p4_hub_identity --assembled <assembly-A.txt>

做什么（先数据根同源，再动任何写操作）：
  1) 起**隔离实例**（临时 WANREPRO_AUTO_DIR + 假 bridge），断言服务端自述 tasks_dir == 本探针根；
  2) 真 ingest（真端点，真视频字节）→ local_id/bridge_id；复投 HIT 同 id（视频 sha 幂等）；
  3) 用**真状态机**把该任务逐级推进到 awaiting_approval（discuss/render 两段在隔离实例里以夹具代替，
     不跑真 LLM、不产生任何费用）——「建到任务」≠「可审批」，这一步正是把它变成可审批；
  4) 用真端点 `POST /api/hub/task/<id>/prompt` 把**画布编排器算出的本次正文**写进 hub：
     写正文 → 重算审批快照 → 重跑自检（G16 唯一真源，不另写评分器）；
  5) 独立复算审批快照（state.snapshot_for / snapshot_hash_v2）→ 与 hub 回执逐字节对账；
  6) approve（v2 带画布配置）→ 已批准后改正文被拒（409，「编辑须撤销旧批准」真实成立）
     → revoke → 改正文产生新修订 → 再批准（v1 语义）→ rollback（丢弃当前提示词、清批准键）；
  7) 全程零提交（不调 /submit，任务文件无 dispatch/gen_job），真实 projects 一条不少、不留垃圾。

判据全对才 exit 0；任何一步失败如实 FAIL 并 exit 1（禁子串判通过）。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
from . import config  # noqa: E402
from . import state  # noqa: E402
from .probe_guard import (  # noqa: E402
    GuardError,
    StubBridge,
    assert_no_loss,
    assert_same_root,
    cleanup_created,
    free_port,
    http_req,
    multipart_body,
    projects_snapshot,
)

UPLOADS_DIR = _ROOT / "uploads"
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" —— {detail}" if detail and not cond else ""))


def _proc_tail(proc: subprocess.Popen, n: int = 1500) -> str:
    try:
        out = proc.stdout.read() if proc.stdout is not None else ""
    except Exception as err:  # noqa: BLE001
        return f"（读子进程输出失败：{err}）"
    out = (out or "").strip()
    return out[-n:] if out else "（无输出）"


def _wait_where(base: str, proc: subprocess.Popen, timeout: float = 90.0) -> dict:
    deadline = time.time() + timeout
    last = f"{base} 无响应"
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"隔离实例提前退出 rc={proc.returncode}｜输出：{_proc_tail(proc)}")
        try:
            s, d = http_req(base, "/api/hub/where", timeout=5)
        except OSError as err:
            last = f"连接未就绪：{err}"
            time.sleep(0.7)
            continue
        if s == 200 and isinstance(d, dict):
            return d
        last = f"{s} {str(d)[:120]}"
        time.sleep(0.7)
    raise RuntimeError(f"隔离实例 {timeout:.0f}s 内没就绪：{last}")


def _argv_path(flag: str) -> str | None:
    argv = sys.argv[1:]
    for i, arg in enumerate(argv):
        if arg == flag and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith(flag + "="):
            return arg.split("=", 1)[1]
    return None


def _assembled_body() -> tuple[str, str]:
    """本次正文：优先读画布编排器探针的产物（跨语言同一份正文），缺省用内置长正文。"""
    path = _argv_path("--assembled")
    if path:
        p = Path(path)
        if not p.is_absolute():
            p = (Path.cwd() / p).resolve()
        if p.is_file():
            text = p.read_text(encoding="utf-8")
            if text.strip():
                return text, f"编排器产物 {p.name}"
    marker = "P4HUBFIXTURE"
    body = (
        f"【{marker}】本次正文为隔离探针夹具（未提供 --assembled）。\n"
        "场景：客户模特在雨夜街头转身，灯箱反光落在面料上，镜头从三分之二侧面推近到近景。\n"
        "镜头：开场 0-3s 中景横移；推进 3-9s 环绕半周；收尾 9-15s 缓慢拉远留白。\n"
        "画面：冷蓝主光 + 暖色轮廓光，浅景深，颗粒感轻微，保持产品标识清晰可读。\n"
        "声音：仅保留环境声与鞋底触地声，无 BGM。\n"
        "禁止：人物面部崩坏、文字乱码、产品变形、多余肢体。\n"
    )
    return body, "内置夹具正文"


def _fixture_advance(task_dir: Path) -> list[str]:
    """用**真状态机**逐级推进到 awaiting_approval；discuss/render 产物用夹具落盘（不跑 LLM）。"""
    steps: list[str] = []
    state.transition(task_dir, "discussing", note="P4 探针夹具：拆解阶段（隔离实例，不跑真 LLM）")
    steps.append("ingested→discussing")
    state.transition(task_dir, "awaiting_assets", note="P4 探针夹具：拆解完成（夹具）")
    steps.append("discussing→awaiting_assets")
    # 真实合同（replace.py:638 口径）：空置换走 require={"empty_confirm": True}——
    # require 在守卫之前落键，extra 在守卫之后才 update，所以只放进 extra 会被停点守卫拦下。
    state.transition(task_dir, "rendering",
                     require={"empty_confirm": True},
                     extra={"assets_confirmed": [], "全沿用": {"at": state._now()}},
                     note="P4 探针夹具：确认置换（隔离实例，空置换显式确认 empty_confirm）")
    steps.append("awaiting_assets→rendering")
    # 渲染产物：hub 真实链会由 do_render 落盘提示词；隔离实例里以夹具代替（不跑真模板渲染）
    placeholder = "\n".join([
        "# P4 探针夹具渲染正文（将被画布编排器正文覆盖）",
        "这一行只是为了让「手改正文」端点有现正文可比对（save_prompt_text 要求现值非空）。",
        "场景：占位。镜头：占位。画面：占位。声音：仅环境声。",
        "禁止：占位。" * 8,
    ])
    (task_dir / "提示词.md").write_text(placeholder, encoding="utf-8")
    state.transition(task_dir, "awaiting_approval", note="P4 探针夹具：渲染完成（夹具正文）")
    steps.append("rendering→awaiting_approval")
    return steps


def main() -> int:
    real_root = Path(config.PROJECTS_DIR)
    real_before = projects_snapshot(real_root)
    iso_root = Path(tempfile.mkdtemp(prefix="wanprobe-p4hub-", dir=os.environ.get("TMPDIR") or tempfile.gettempdir()))
    iso_projects = iso_root / "projects"
    iso_projects.mkdir(parents=True, exist_ok=True)
    iso_before = projects_snapshot(iso_projects)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    stub = StubBridge()
    env = dict(os.environ, WANREPRO_AUTO_DIR=str(iso_projects), WANREPRO_BRIDGE=stub.base)
    proc: subprocess.Popen | None = None
    created_ids: set[str] = set()
    print(f"[probe-p4-hub] 自隔离模式：实例 {base}，数据根 {iso_projects}，假 bridge {stub.base}")

    def req(path: str, method: str = "GET", data: bytes | None = None, ctype: str | None = None,
            timeout: int = 180):
        return http_req(base, path, method, data, ctype, timeout=timeout)

    try:
        stub.thread.start()
        proc = subprocess.Popen(
            [sys.executable, str(_ROOT / "ui" / "server.py"), "--port", str(port), "--no-browser"],
            cwd=str(_ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        )
        try:
            where = _wait_where(base, proc)
        except RuntimeError as err:
            check("隔离实例起得来", False, str(err))
            return 1
        try:
            assert_same_root(where, iso_projects)
            check("服务端自述 TASKS_DIR == 探针根（读写同源）", True)
        except GuardError as err:
            check("服务端自述 TASKS_DIR == 探针根（读写同源）", False, str(err))
            return 1

        # ── 1. 真 ingest：身份闭合（同一视频 sha 幂等）
        print("1) ingest 身份闭合")
        videos = sorted(p for p in UPLOADS_DIR.glob("*.mp4") if p.is_file())
        if not videos:
            check("uploads 里有可用视频（真 ingest 需要真字节）", False, str(UPLOADS_DIR))
            return 1
        video = videos[0]
        body, ctype = multipart_body({"intent": "P4 探针：身份绑定"}, {"video": (video.name, video.read_bytes())})
        s, d = req("/api/hub/ingest", "POST", body, ctype)
        local_id = d.get("local_id") if isinstance(d, dict) else None
        bridge_id = d.get("bridge_id") if isinstance(d, dict) else None
        if local_id:
            created_ids.add(local_id)
        check(f"ingest 真建任务（{video.name}）→ 200 + local_id/bridge_id", s == 200 and bool(local_id) and bool(bridge_id),
              f"{s} {str(d)[:160]}")
        if not local_id:
            return 1
        s2, d2 = req("/api/hub/ingest", "POST", body, ctype)
        check("复投 → HIT 同任务（视频 sha 幂等，不产生第二条身份）",
              s2 == 200 and str(d2.get("result") or "").upper() == "HIT" and d2.get("local_id") == local_id,
              f"{s2} {str(d2)[:140]}")
        task_dir = iso_projects / local_id
        check("任务目录带 任务.json（approve/rollback 的硬前提，_task_dir 只认它）",
              (task_dir / "任务.json").is_file(), str(task_dir))
        task = state.load(task_dir)
        check("身份锚一致：local_id/bridge_id 与 ingest 回执同源",
              str(task.get("local_id")) == local_id and str(task.get("bridge_id")) == str(bridge_id),
              f"{task.get('local_id')}/{task.get('bridge_id')} vs {local_id}/{bridge_id}")
        s, _ = req("/api/hub/task/auto-9998")
        check("非法 local_id 被拒（不认无 任务.json 的壳）", s in (404, 409), str(s))

        # ── 2. 真状态机推进到待审批（discuss/render 用夹具，零费用）
        print("2) 推进到 awaiting_approval（真状态机 + 夹具产物）")
        steps = _fixture_advance(task_dir)
        task = state.load(task_dir)
        check("逐级迁移合法且状态=awaiting_approval", task.get("state") == "awaiting_approval", f"{steps} → {task.get('state')}")
        s, detail = req(f"/api/hub/task/{local_id}")
        check("GET 详情 200 且状态待审批", s == 200 and (detail.get("task") or {}).get("state") == "awaiting_approval", f"{s}")

        # ── 3. 画布本次正文 → hub（真保存语义：写正文 + 重算快照 + 重跑自检）
        print("3) 正文保存 + 快照 + 自检（真端点）")
        assembled, source = _assembled_body()
        payload = json.dumps({"text": assembled, "expect_text": None}, ensure_ascii=False).encode("utf-8")
        s, saved = req(f"/api/hub/task/{local_id}/prompt", "POST", payload, "application/json")
        check(f"保存正文 → 200（正文来源：{source}）", s == 200, f"{s} {str(saved)[:160]}")
        if s != 200:
            return 1
        prompt_file = task_dir / "提示词.md"
        check("正文逐字节落盘（画布终稿 == hub 提示词.md）",
              prompt_file.read_text(encoding="utf-8") == assembled,
              f"盘上 {len(prompt_file.read_text(encoding='utf-8'))} 字 vs 送入 {len(assembled)} 字")
        fresh = state.load(task_dir)
        expected_snapshot = state.snapshot_for(fresh, assembled)
        check("审批快照 = 独立复算值（同一正文 → 同一快照）",
              str(fresh.get("approval_snapshot")) == expected_snapshot,
              f"{fresh.get('approval_snapshot')} vs {expected_snapshot}")
        check("手改留痕（prompt_edited_at/prompt_edit_reason）",
              bool(fresh.get("prompt_edited_at")) and "手改" in str(fresh.get("prompt_edit_reason") or ""))
        selfcheck_items = (saved.get("task") or {}).get("自检") or saved.get("自检")
        check("自检真跑（非空 items 且每项有结论）",
              isinstance(selfcheck_items, list) and bool(selfcheck_items)
              and all("结论" in item for item in selfcheck_items),
              str(selfcheck_items)[:160])
        on_disk = json.loads((task_dir / "自检.json").read_text(encoding="utf-8")) if (task_dir / "自检.json").is_file() else None
        check("自检结果与盘上 自检.json 同源（G16：不另写评分器）",
              on_disk is None or selfcheck_items == on_disk, "盘上无 自检.json（自检未落盘）" if on_disk is None else "不一致")

        # ── 4. approve（v2 带画布配置）→ 已批准改正文被拒 → revoke → 新修订 → 再批准
        print("4) approve / 改正文被拒 / revoke / 新修订 / 再批准")
        config_v2 = {
            "渠道身份": {"baseUrl": "https://api.example.com", "apiFormat": "sdgf", "渠道名": "P4 探针渠道"},
            "model": "probe-model",
            "mode": "reference",
            "params": {"duration": 5, "size": "720x1280", "vquality": "720", "audio": False},
        }
        s, approved = req(f"/api/hub/approve/{local_id}", "POST",
                          json.dumps({"config": config_v2}, ensure_ascii=False).encode("utf-8"), "application/json")
        check("approve（带画布配置）→ 200 approved", s == 200 and approved.get("state") == "approved", f"{s} {str(approved)[:140]}")
        task_approved = state.load(task_dir)
        recomputed_v2 = state.snapshot_hash_v2(
            assembled,
            task_approved.get("gen_params") or {},
            task_approved.get("gen_channel") or {},
        )
        check("批准快照 = v2 独立复算（prompt+params+渠道身份+model+mode）",
              str((task_approved.get("approval") or {}).get("snapshot_sha256")) == recomputed_v2
              and int(task_approved.get("snapshot_version") or 0) == state.SNAPSHOT_VERSION,
              f"{str(task_approved.get('approval'))[:90]} vs {recomputed_v2}")
        check("gen_channel 落盘且 key 不落（渠道身份三键 + model + mode）",
              isinstance(task_approved.get("gen_channel"), dict)
              and set(("baseUrl", "apiFormat", "渠道名", "model", "mode")) <= set(task_approved["gen_channel"])
              and not any("key" == str(k).lower() for k in task_approved["gen_channel"]))

        before_bytes = prompt_file.read_bytes()
        s, refused = req(f"/api/hub/task/{local_id}/prompt", "POST",
                         json.dumps({"text": assembled + "\n改一行"}, ensure_ascii=False).encode("utf-8"),
                         "application/json")
        check("已批准后改正文被拒（409 + 人话：先撤销批准）",
              s == 409 and "批准" in str(refused.get("error") or ""), f"{s} {str(refused)[:140]}")
        check("被拒的写入零落地（提示词.md 逐字节不变）", prompt_file.read_bytes() == before_bytes)

        s, revoked = req(f"/api/hub/task/{local_id}/revoke", "POST",
                         json.dumps({"note": "P4 探针：改正文前撤销批准"}).encode("utf-8"), "application/json")
        check("revoke → 回到 awaiting_approval（受控回退边，不自动重批）",
              s == 200 and str((revoked or {}).get("state")) == "awaiting_approval", f"{s} {str(revoked)[:140]}")

        revised = assembled + "\n# 修订 R2\n本次修订由 P4 探针写入（验证旧批准对新正文失效）。"
        s, saved2 = req(f"/api/hub/task/{local_id}/prompt", "POST",
                        json.dumps({"text": revised, "expect_text": assembled}, ensure_ascii=False).encode("utf-8"),
                        "application/json")
        fresh2 = state.load(task_dir)
        check("乐观锁通过后写入新修订（expect_text=旧正文）", s == 200 and str(fresh2.get("approval_snapshot")) == state.snapshot_for(fresh2, revised),
              f"{s} {str(saved2)[:140]}")
        check("新修订快照与旧批准快照不同（旧批准对新正文失效）",
              str(fresh2.get("approval_snapshot")) != expected_snapshot)
        s, bad = req(f"/api/hub/task/{local_id}/prompt", "POST",
                     json.dumps({"text": revised + "x", "expect_text": assembled}, ensure_ascii=False).encode("utf-8"),
                     "application/json")
        check("乐观锁失败 → 409 且不写（别处改过要先刷新）", s == 409, f"{s} {str(bad)[:140]}")

        s, approved2 = req(f"/api/hub/approve/{local_id}", "POST", b"{}", "application/json")
        task2 = state.load(task_dir)
        check("再批准（v1 语义，无 config）→ approved 且快照=盘上新正文口径",
              s == 200 and approved2.get("state") == "approved"
              and str((task2.get("approval") or {}).get("snapshot_sha256")) == state.snapshot_hash_v2(
                  revised, task2.get("gen_params") or {}, task2.get("gen_channel") or {}),
              f"{s} {str(approved2)[:140]}")

        # ── 5. rollback：受控回退边只在 awaiting_approval（state.rollback_to_assets 守卫）
        print("5) rollback（受控回退边：approved 不可直退，先 revoke 再退回置换）")
        s_direct, direct = req(f"/api/hub/task/{local_id}/rollback", "POST", b"{}", "application/json")
        check("approved 上直接 rollback 被拒（回退边只在 awaiting_approval，不绕过批准）",
              s_direct == 409 and "awaiting_approval" in str(direct.get("error") or ""),
              f"{s_direct} {str(direct)[:140]}")
        s, revoked_again = req(f"/api/hub/task/{local_id}/revoke", "POST",
                               json.dumps({"note": "P4 探针：退回置换前撤销批准"}).encode("utf-8"),
                               "application/json")
        check("revoke → awaiting_approval（撤销批准后才允许退回）",
              s == 200 and str((revoked_again or {}).get("state")) == "awaiting_approval",
              f"{s} {str(revoked_again)[:140]}")
        s, rolled = req(f"/api/hub/task/{local_id}/rollback", "POST", b"{}", "application/json")
        task3 = state.load(task_dir)
        check("rollback → awaiting_assets", s == 200 and task3.get("state") == "awaiting_assets", f"{s} {str(rolled)[:140]}")
        leftover = [k for k in ("approval", "approval_snapshot", "gen_params", "gen_channel",
                                "snapshot_version", "dispatch", "submit_unknown", "gen_job") if k in task3]
        check("批准键连根清（approval/snapshot/gen_params/gen_channel/dispatch…）", not leftover, str(leftover))
        check("当前提示词已丢弃（画布侧必须重新走一遍修订）",
              not prompt_file.is_file() or not prompt_file.read_text(encoding="utf-8").strip(),
              f"{prompt_file.exists()}")

        # ── 6. 零付费提交
        print("6) 零付费提交")
        check("全程未调 /submit：任务文件无 dispatch/gen_job/submit_unknown",
              not any(k in task3 for k in ("dispatch", "gen_job", "submit_unknown")))
        check("隔离根里没有 记录.json（没有云端任务被创建）",
              not (iso_projects / local_id / "记录.json").exists(), local_id)

    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        try:
            stub.httpd.shutdown()
            stub.httpd.server_close()
        except Exception:  # noqa: BLE001
            pass
        # 只删本次自建（跑前不存在 ∧ 本次新建）；真实任务一条不动
        for lid in sorted(created_ids):
            _deleted, why = cleanup_created(iso_projects, lid, before=iso_before, created=created_ids)
            print(f"     [清理] {why}")
        shutil.rmtree(iso_root, ignore_errors=True)

    real_after = projects_snapshot(real_root)
    try:
        assert_no_loss(real_before, real_after)
        check("真实 projects 零丢失（跑后 ⊆ 跑前）", True)
    except GuardError as err:
        check("真实 projects 零丢失（跑后 ⊆ 跑前）", False, str(err))
    check("真实 projects 未被改动（目录集不变）", real_before == real_after,
          f"before={sorted(real_before)} after={sorted(real_after)}")

    fails = [r for r in RESULTS if not r[1]]
    print(f"\n[probe-p4-hub] {len(RESULTS) - len(fails)}/{len(RESULTS)} PASS")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
