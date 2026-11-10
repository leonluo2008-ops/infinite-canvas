# -*- coding: utf-8 -*-
"""AT-5 续跑与审批——rendering → awaiting_approval → approved → generating → done。

三个子命令（审批停点 = approve 独立命令，必须人工敲）：
  py -3 -m automation.generate render  <id> [--set duration=10,ratio=16:9]
        渲染提示词（默认模板 seedance-flow，红线 21）→ awaiting_approval + 回显预演
  py -3 -m automation.generate approve <id>
        所有者批准（审批快照绑定：之后改输入自动作废）→ approved
  py -3 -m automation.generate submit  <id>
        提交生成（confirm=true，走 SDGF 服务端封装 /api/sdgf/video/*）→ 轮询 →
        成片落任务目录 → done

渠道配置（SDGF，与画布同源）：环境变量 WANREPRO_SDGF_CHANNEL
  = JSON {"baseUrl":..., "apiKey":..., "model":...}；或文件 channel.json 同构。
  不进 git（automation/projects/ 已 ignore；凭据只在此处集中读）。

生成参数默认：duration=5 ratio=9:16 resolution=720 audio=true（--set 覆盖）。
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import bridge, config, replace, state

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_TEMPLATE = "seedance-flow"      # 红线 21：A 线默认模板
DEFAULT_PARAMS = {"duration": 5, "ratio": "9:16", "resolution": "720", "audio": True}
PROMPT_FILE = "提示词.md"
VIDEO_FILE = "成片.mp4"
GEN_POLL_INTERVAL = 5
GEN_POLL_MAX = 3600  # 思念云普遍 5min+，上限 1 小时


class GenError(RuntimeError):
    pass


def channel() -> dict:
    """SDGF 渠道配置：env WANREPRO_SDGF_CHANNEL > projects/channel.json。"""
    raw = os.environ.get("WANREPRO_SDGF_CHANNEL", "")
    if not raw:
        f = config.PROJECTS_DIR / "channel.json"
        if f.is_file():
            raw = f.read_text(encoding="utf-8")
    if not raw:
        raise GenError("缺 SDGF 渠道配置：设 WANREPRO_SDGF_CHANNEL 或 "
                       f"{config.PROJECTS_DIR / 'channel.json'}（{{baseUrl,apiKey,model}}）")
    try:
        ch = json.loads(raw)
    except json.JSONDecodeError as e:
        raise GenError(f"渠道配置不是合法 JSON: {e}") from None
    for k in ("baseUrl", "apiKey", "model"):
        if not ch.get(k):
            raise GenError(f"渠道配置缺 {k}")
    return ch


def parse_set(spec: str) -> dict:
    out = {}
    for part in (spec or "").split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        if k in ("duration",):
            out[k] = int(v)
        elif k in ("audio",):
            out[k] = v.lower() in ("1", "true", "yes")
        else:
            out[k] = v
    return out


def do_render(task_dir: Path, set_spec: str, verbose: bool = True) -> dict:
    """rendering → 渲染 → awaiting_approval（回显预演，等人工 approve）。"""
    task = state.load(task_dir)
    if task["state"] != "rendering":
        raise GenError(f"render 需 rendering 状态（先完成置换确认），当前 {task['state']}")
    params = {**DEFAULT_PARAMS, **parse_set(set_spec)}
    out = bridge.get(f"/api/render/{task['bridge_id']}/{DEFAULT_TEMPLATE}")
    prompt = out.get("text") or ""
    # 结构性校验（不假设模板格式——红线 14）：非空且够长即视为正文；
    # 模板差异（lumi 带 # 标题头 / seedance-flow 纯正文流）不是合同的一部分
    if len(prompt.strip()) < 200:
        raise GenError(f"渲染正文过短（{len(prompt.strip())} 字）不像提示词: {prompt[:120]}")
    (task_dir / PROMPT_FILE).write_text(prompt, encoding="utf-8")
    snap = state.snapshot_hash(prompt, params)
    state.transition(task_dir, "awaiting_approval",
                     extra={"gen_params": params, "approval_snapshot": snap},
                     note=f"预演就绪（模板 {DEFAULT_TEMPLATE}）")
    # AT-9 B5：渲染完成即自动自检（三项→ 自检.json；检查器异常也不打断渲染主链，
    # selfcheck 内部如实落 FAIL 记录）。CLI render 与 hub 渲染共用此处，合同一致。
    from . import selfcheck
    check_items = selfcheck.run_selfcheck(task_dir)
    bad = [x for x in check_items if x.get("结论") != "PASS"]
    if verbose:
        print(f"  自检：{len(check_items) - len(bad)}/{len(check_items)} 项通过 → {task_dir / selfcheck.SELFCHECK_NAME}")
        for item in bad:
            print(f"  [自检⚠] {item.get('项')}：{item.get('证据', '')[:200]}")
        print(f"[待审批] {task['local_id']} 预演参数: {json.dumps(params, ensure_ascii=False)}")
        print(f"  快照 {snap[:16]}…；提示词已落 {PROMPT_FILE}（{len(prompt)} 字）")
        print(f"  批准: py -3 -m automation.generate approve {task['local_id']}")
    return {"params": params, "snapshot": snap, "prompt_chars": len(prompt)}


def do_approve(task_dir: Path, verbose: bool = True) -> str:
    """awaiting_approval → approved（人工命令=审批停点）。"""
    task = state.load(task_dir)
    if task["state"] != "awaiting_approval":
        raise GenError(f"approve 需 awaiting_approval，当前 {task['state']}")
    snap = task.get("approval_snapshot")
    if not snap:
        raise GenError("任务缺审批快照（先跑 render）")
    state.transition(task_dir, "approved",
                     require={"approval.snapshot_sha256": snap},
                     extra={"approval.at": state._now(), "approval.by": "owner-cli"},
                     note="所有者批准")
    if verbose:
        print(f"[OK] {task['local_id']}: 已批准（快照 {snap[:16]}…）→ 可 submit")
    return "approved"


def do_submit(task_dir: Path, verbose: bool = True,
              on_progress=None) -> str:
    """approved → generating →（轮询）→ done；成片落任务目录。

    on_progress: 可选回调 (pct:int|None, waited:int)——Notion 中控台用它回写进度。
    """
    task = state.load(task_dir)
    if task["state"] == "generating":
        print(f"[续跑] {task['local_id']}: 上次提交中断，重查（产物优先）")
    elif task["state"] == "approved":
        pass
    else:
        raise GenError(f"submit 需 approved（先审批），当前 {task['state']}")

    prompt = (task_dir / PROMPT_FILE).read_text(encoding="utf-8")
    params = task["gen_params"]
    snap = state.snapshot_hash(prompt, params)
    if state.approval_stale(task_dir, snap):
        raise GenError("审批已作废（提示词/参数在批准后变过）——重跑 render + approve")

    # 产物优先：成片已在（kill 重启后）→ 直接 done
    vf = task_dir / VIDEO_FILE
    if vf.is_file() and vf.stat().st_size > 0:
        if task["state"] == "generating":
            state.transition(task_dir, "done", note="成片已落盘（恢复路径）")
            print(f"[SKIP] {task['local_id']}: 成片已在（{vf.stat().st_size} 字节）→ done")
            return "done"

    if task["state"] == "approved":
        state.transition(task_dir, "generating", note="提交生成")
        # AT-11 B1：提交图片按组去重——组内 N 行一图只提交一条（与发号映射同一 row_unit 口径）；
        # 组外同 URL 保持独立条目（禁全局 URL 去重）。确认快照已在确认时按组归一，
        # 这里兜底旧任务/外部写入的逐行快照，保证提交清单=发号单位。
        groups = replace.load_groups(str(task.get("bridge_id") or ""))
        entries = sorted((f for f in (task.get("assets_confirmed") or []) if isinstance(f, dict)),
                         key=lambda f: int(f.get("no") or 0))
        seen_units: set[tuple] = set()
        images: list[str] = []
        for f in entries:
            unit = replace.row_unit(int(f.get("no") or 0), groups)
            if unit in seen_units:
                continue
            seen_units.add(unit)
            url = str(f.get("url") or "")
            if url:
                images.append(url)
        body = {"prompt": prompt, "duration": params["duration"],
                "ratio": params["ratio"], "resolution": params["resolution"],
                "audio": params["audio"], "confirm": True,
                "channel": channel()}
        if images:
            body["images"] = [{"name": f"img{i}", "url": u} for i, u in enumerate(images, 1)]
        out = bridge.post_json("/api/sdgf/video/run", body)
        job = out.get("job") or ""
        task = state.load(task_dir)
        task["gen_job"] = job
        task["gen_task_id"] = out.get("taskId") or ""
        state.save(task_dir, task)
        if verbose:
            print(f"[提交] job={job} taskId={task['gen_task_id']}（思念云普遍 5 分钟+）")

    task = state.load(task_dir)
    job = task.get("gen_job") or ""
    if not job:
        raise GenError("任务缺 gen_job（提交未落账）——重跑 submit")

    waited = 0
    while waited < GEN_POLL_MAX:
        rec = bridge.get(f"/api/sdgf/video/job/{job}")
        st = rec.get("status")
        if st == "done":
            tid = rec.get("taskId") or task.get("gen_task_id")
            data = bridge.get_bytes(f"/api/sdgf/video/file/{tid}")
            if not data:
                raise GenError(f"成片端点空响应（taskId={tid}）")
            vf.write_bytes(data)
            if vf.stat().st_size <= 0:
                raise GenError("成片 0 字节——如实记失败")
            state.transition(task_dir, "done",
                             note=f"成片 {vf.stat().st_size} 字节（taskId={tid}）")
            if verbose:
                print(f"[OK] {task['local_id']}: 成片落盘 {vf}（{vf.stat().st_size} 字节）→ done")
            return "done"
        if st == "error":
            state.transition(task_dir, "failed", note=str(rec.get("error"))[:500])
            raise GenError(f"生成失败（原文）: {rec.get('error')}")
        pct = rec.get("progress")
        if on_progress is not None:
            try:
                on_progress(pct if isinstance(pct, (int, float)) else None, waited)
            except Exception:  # noqa: BLE001 —— 进度回写失败不拖垮生成主链
                pass
        if verbose and pct is not None and waited % 30 == 0:
            print(f"  生成中 {pct}%（{waited}s）")
        time.sleep(GEN_POLL_INTERVAL)
        waited += GEN_POLL_INTERVAL
    raise GenError(f"超 {GEN_POLL_MAX}s 未完成——不判失败，重跑 submit 续查（job={job}）")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="AT-5 续跑与审批")
    ap.add_argument("cmd", choices=["render", "approve", "submit"])
    ap.add_argument("local_id")
    ap.add_argument("--set", help="覆盖生成参数 duration=10,ratio=16:9,audio=false")
    ap.add_argument("--bridge")
    args = ap.parse_args(argv)
    if args.bridge:
        config.BRIDGE_BASE = args.bridge.rstrip("/")
    config.ensure_dirs()
    tdir = config.PROJECTS_DIR / args.local_id
    if not (tdir / "任务.json").is_file():
        raise SystemExit(f"找不到任务 {args.local_id}")
    try:
        if args.cmd == "render":
            do_render(tdir, args.set or "")
        elif args.cmd == "approve":
            do_approve(tdir)
        else:
            do_submit(tdir)
        return 0
    except (GenError, bridge.BridgeError, state.StateError) as e:
        print(f"[FAIL] {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
