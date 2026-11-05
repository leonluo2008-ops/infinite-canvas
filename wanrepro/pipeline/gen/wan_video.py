#!/usr/bin/env python3
"""用万相 CLI 把一条提示词生成成一条视频。

引擎契约（和 reverse/ 那套一致）：
- 中间产物都是文件：`生成/记录.json` 是唯一状态源，`生成/成片-<模板>.mp4` 是成品
- 入口支持 `--id`；三阶段独立可调（计划 / 提交 / 等待下载）
- 无 `input()`、无 pager；错误用退出码 + stderr 一句话
- **花钱动作必须显式 `--confirm`**：不带就只打计划，不提交

退出码：0 成功｜2 参数错或没有 --confirm｜3 CLI/网络失败｜4 已有任务在跑

用法：
  py -3 pipeline/gen/wan_video.py --id pop-3-b3 --template lumi-4section            # 只打计划
  py -3 pipeline/gen/wan_video.py --id pop-3-b3 --template lumi-4section --confirm  # 真花钱
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ANALYSIS = ROOT / "analysis" / "reverse"
sys.path.insert(0, str(Path(__file__).resolve().parent))
import wan_submit as WS  # noqa: E402  —— 提交契约的唯一持有者（见 docs/规范-万相提交-20261102.md）
RECORD_NAME = "记录.json"
GEN_DIR_NAME = "生成"
IMG_RE = re.compile(r"图片(\d+)")
RUNNING_GRACE = 60 * 60            # 记录里写着 running 但超过这么久没更新 → 当成上一次跑挂了
TASK_LIST_TIMEOUT = 90             # `wan task list`：只为取任务号集合，别等太久
DRY_RUN_TIMEOUT = 300             # 预演：CLI 只在本地构造请求体，正常几秒；给足余量但绝不无限等


CliEntryError = WS.CliEntryError          # 入口/执行归 wan_submit（唯一出口）


def task_dir(task_id: str) -> Path:
    if not re.fullmatch(r"[0-9A-Za-z._-]{1,64}", task_id or ""):
        raise ValueError(f"任务号不合法：{task_id!r}")
    d = ANALYSIS / task_id
    if not d.is_dir():
        raise ValueError(f"没有这个任务：analysis/reverse/{task_id}")
    return d


def gen_dir(task_id: str) -> Path:
    d = task_dir(task_id) / GEN_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def prompt_file(task_id: str, template: str) -> Path:
    """→ 提示词-<模板>.md；不指定模板就取 mtime 最新的那版。"""
    d = task_dir(task_id)
    if template:
        stem = template[:-3] if template.endswith(".md") else template
        path = d / f"提示词-{stem}.md"
        if not path.is_file():
            raise ValueError(f"没有这版提示词：{path.name}（先点「生成提示词」）")
        return path
    files = sorted(d.glob("提示词-*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        raise ValueError(f"{task_id} 还没有任何提示词文件，先点「生成提示词」")
    return files[0]


def asset_images(task_id: str, prompt: str) -> list[Path]:
    """按提示词里出现的 @图片N 收集参考图，顺序就是图号顺序（CLI 按 order 认 @图片N）。

    **图号 ≠ 文件名**：盘上的 `图片N.png` 里的 N 是**清单行号**，提示词里的 @图片N 是
    **第 N 个有图的资产**（下载图片包时也是这么重排成 1..K 的）。所以按文件名取会整体错位
    一整张——错位生成出来的片子看不出错位，只会把人脸/衣服换成别人的，比报错更糟。
    缺图就整体报错。
    """
    d = task_dir(task_id) / "assets"
    want = WS.wanted_refs(prompt)
    if not want:
        return []
    ordered: list[Path] = []
    for f in d.glob("图片*"):
        m = IMG_RE.fullmatch(f.stem)
        if m and f.is_file():
            ordered.append((int(m.group(1)), f))
    ordered.sort(key=lambda x: x[0])                       # 按清单行号
    files = [f for _, f in ordered]                        # 第 k 个 = 图号 k
    try:
        picked, _ = WS.expand_prefix(prompt, files)        # 补齐 1..max(N)，越界即抛
    except WS.SubmitError as exc:
        raise ValueError(str(exc)) from None
    return picked


def credits() -> int | None:
    """灵感值余额；查不到返回 None（不编数字）。"""
    try:
        return WS.credits(run_cli)
    except CliEntryError:
        return None                      # 入口没了由 preflight 判失败，不该在查余额这一步先炸


cli_prefix = WS.cli_prefix
run_cli = WS.run_cli


def task_ids() -> set[str] | None:
    """`wan task list` 里的全部 taskId；取不到返回 None（调用方按失败处理）。

    比对必须用**集合**而不是「最新一条」：并发下（另一个窗口也在生成）最新一条随时会变，
    只比最新一条就会把别人的任务算成自己的意外提交。
    """
    code, out, err = run_cli(["task", "list", "--output", "json"], timeout=TASK_LIST_TIMEOUT)
    if code != 0:
        return None
    try:
        data = json.loads(out.strip() or "[]")
    except json.JSONDecodeError:
        return None
    rows = data if isinstance(data, list) else (data.get("data") if isinstance(data, dict) else None)
    if not isinstance(rows, list):
        return None
    return {str(r["taskId"]) for r in rows if isinstance(r, dict) and r.get("taskId")}


def element_rows(elements: object) -> list[tuple[str, str]]:
    """taskInput.elements（id → {payload:{title,name,...}}）→ [(title, name)]，保持原顺序。"""
    if not isinstance(elements, dict):
        return []
    rows: list[tuple[str, str]] = []
    for val in elements.values():
        payload = val.get("payload") if isinstance(val, dict) else None
        payload = payload if isinstance(payload, dict) else {}
        rows.append((str(payload.get("title") or ""), str(payload.get("name") or "")))
    return rows


def preflight(prompt: str, duration: int, ratio: str, resolution: str,
              images: list[Path], audio: bool = True) -> dict:
    """提交前预演：用 `--dry-run` 让 CLI 打出它**真正要发的请求体**，逐项核对。

    这不是可选项。上一次就是正文被 cmd.exe 切断、参数全丢，任务照样成功、钱照样扣，
    生成出来的东西和提示词毫无关系——只有把「发出去的请求」拿回来比对才能发现。

    两道关：
    1. 逐字比对正文、时长、画幅、分辨率、参考图张数与顺序；
    2. **意外提交守卫**：预演前后各取一次 `wan task list` 的 taskId 集合，出现新增 ⇒
       这次「预演」其实真提交了（`--dry-run` 若没生效/被吞掉就会这样），立刻判失败。
       守卫成立的前提是「参数真的作为 argv 传到了 node」——所以绝不能走 `wan.cmd`。
    """
    want = [p.name for p in images]
    out: dict = {
        "ok": False,
        "mismatches": [],
        "actual": {"chars": 0, "duration": None, "ratio": "", "resolution": "", "images": 0, "audio": None},
        "images": [{"no": i, "file": name} for i, name in enumerate(want, start=1)],
        "token": "",                     # 建好 req 后再算（带图片内容指纹）
        # 记录.json 要的那几个关键字段（不是整段 taskInput：正文已单独存盘）
        "requestSnapshot": {},
    }
    bad: list[str] = out["mismatches"]

    req = WS.Request(prompt=prompt, images=tuple(images), duration=duration,
                     ratio=ratio, resolution=resolution, audio=audio)
    out["token"] = req.token()
    bad.extend(WS.validate(req))          # 平台约束：时长/画幅/分辨率/图/正文写法
    before = task_ids()
    code, raw, err = run_cli(WS.dry_run_args(req), timeout=DRY_RUN_TIMEOUT)
    after = task_ids()

    if before is None or after is None:
        bad.append("取不到 wan task list，无法确认预演没有真提交（保守判失败，不提交）")
    else:
        fresh = sorted(after - before)
        if fresh:
            bad.append("预演意外提交，已中止：新任务 " + "、".join(fresh))

    if code != 0:
        bad.append(f"预演失败（退出 {code}）：{(err or raw).strip()[:200]}")
        return out
    try:
        request = json.loads(raw.strip())["request"]
        task_input = request["taskInput"]
    except (json.JSONDecodeError, KeyError, TypeError):
        bad.append("预演输出看不懂：" + raw.strip()[:200])
        return out
    if not isinstance(task_input, dict) or not isinstance(request, dict):
        bad.append("预演输出看不懂：request/taskInput 不是对象")
        return out

    got_prompt = "" if task_input.get("prompt") is None else str(task_input.get("prompt"))
    got_elements = element_rows(task_input.get("elements"))
    got_titles = [t for t, _ in got_elements]
    got_audio = bool(task_input.get("audio"))

    # 逐项核对全部交给提交契约模块，避免这里和 wan_submit 各写一套
    bad.extend(WS.check_echo(req, task_input))

    out["actual"] = {
        "chars": len(got_prompt),
        "duration": task_input.get("duration"),
        "ratio": task_input.get("ratio"),
        "resolution": task_input.get("selectedResolution"),
        "images": len(got_titles),
        "audio": got_audio,
    }
    out["requestSnapshot"] = {
        "taskType": request.get("taskType"),
        "deductMode": request.get("deductMode"),
        "promptChars": len(got_prompt),
        "promptSha256": hashlib.sha256(got_prompt.encode("utf-8")).hexdigest(),
        "promptFirstLine": got_prompt.splitlines()[0] if got_prompt else "",
        "duration": task_input.get("duration"),
        "ratio": task_input.get("ratio"),
        "resolution": task_input.get("selectedResolution"),
        "audio": task_input.get("audio"),
        "elements": [{"title": t, "name": n} for t, n in got_elements],
    }
    out["ok"] = not bad
    return out


def record_path(task_id: str) -> Path:
    return gen_dir(task_id) / RECORD_NAME


def read_record(task_id: str) -> dict:
    path = record_path(task_id)
    if not path.is_file():
        return {}
    try:
        data = json.loads(read_text(path) or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def save_record(task_id: str, data: dict) -> None:
    write_text(record_path(task_id), json.dumps(data, ensure_ascii=False, indent=2))


def busy(rec: dict) -> bool:
    """上一次还在跑并且没超时 → 不许再提交，否则双击两次就扣两次钱。"""
    if rec.get("status") != "running":
        return False
    return time.time() - float(rec.get("started") or 0) < RUNNING_GRACE


def other_running(task_id: str) -> str:
    """别的任务还在跑 → 返回那个任务号。

    并发两条会各扣各的钱，所以宁可拦住；只认 记录.json 里的 running（过期由 GRACE 放行）。
    """
    for path in ANALYSIS.glob("*/生成/记录.json"):
        other = path.parent.parent.name
        if other == task_id:
            continue
        try:
            rec = json.loads(read_text(path) or "{}")
        except (json.JSONDecodeError, OSError):
            continue
        if busy(rec):
            return other
    return ""


def plan(task_id: str, template: str, duration: int, ratio: str, resolution: str,
         audio: bool = True) -> dict:
    path = prompt_file(task_id, template)
    prompt = read_text(path)
    images = asset_images(task_id, prompt)
    balance = credits()
    pf = preflight(prompt, duration, ratio, resolution, images, audio)
    estimate = WS.estimate_credits(resolution.upper(), duration)
    if estimate is not None and balance is not None and balance < estimate:
        pf["ok"] = False
        pf["mismatches"].append(f"余额不足：{balance} 个，预估要 {estimate} 个")
    return {
        "ok": True,
        "task": task_id,
        "template": path.stem[len("提示词-"):],
        "promptFile": path.name,
        "promptChars": len(prompt),
        "images": [p.name for p in images],
        "duration": duration,
        "ratio": ratio,
        "resolution": resolution,
        "audio": audio,
        "credits": balance,
        # 实测口径：480P = 2 灵感值/秒（2026-10-28 15s 实测扣 30）。其它分辨率单价未实测。
        "estimate": estimate,
        "estimateNote": ("按 480P 实测 2 灵感值/秒" if estimate is not None
                         else "该分辨率单价未实测，以完成后余额差为准"),
        "busy": busy(read_record(task_id)),
        "otherRunning": other_running(task_id),
        # 计划本身就带核对结果：页面不用另走一条入口，看到的计划就是「真要发的那个请求」
        "preflight": pf,
    }


def failed_record(info: dict, pf: dict, prompt: str, images: list[Path], reason: str) -> dict:
    """核对没过的记录。**只写 failed，绝不写 running**（写 running 会让页面显示「生成中」，那是假的）。"""
    stamp = time.time()
    return {
        "status": "failed", "taskId": "", "template": info.get("template", ""),
        "duration": info.get("duration"), "ratio": info.get("ratio"),
        "resolution": info.get("resolution"),
        "images": [p.name for p in images], "promptChars": len(prompt),
        "promptFirstLine": prompt.splitlines()[0] if prompt else "",
        "started": stamp, "startedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "creditsBefore": info.get("credits"), "error": reason, "preflight": pf,
    }


def generate(args: argparse.Namespace) -> int:
    task_id, template = args.id, args.template
    audio = str(getattr(args, "audio_output", "true")).lower() != "false"
    info = plan(task_id, template, args.duration, args.ratio, args.resolution, audio)
    if info["busy"]:
        print(f"已经有一条在跑（{task_id}），等它出来再点", file=sys.stderr)
        return 4
    if info.get("otherRunning"):
        print(f"另一个任务还在跑（{info['otherRunning']}），并发会重复扣费——等它出来再点", file=sys.stderr)
        return 4

    prompt_path = prompt_file(task_id, template)
    prompt = read_text(prompt_path)
    images = asset_images(task_id, prompt)
    tpl = prompt_path.stem[len("提示词-"):]
    out_dir = gen_dir(task_id)

    # **先核对，再花钱**：预演（--dry-run，免费）过了才轮到提交。
    # 上一次就是没这一步：正文被截断、参数全丢，任务照样成功、15 灵感值照样扣。
    pf = info["preflight"]
    if not pf.get("ok"):
        reason = "提交前核对未通过，没有提交：" + "；".join(pf.get("mismatches") or ["原因不明"])
        save_record(task_id, failed_record(info, pf, prompt, images, reason))
        print(reason, file=sys.stderr)
        return 3

    before = info.get("credits")
    if before is None:
        before = credits()

    rec = {
        "status": "running", "taskId": "", "template": tpl,
        "duration": args.duration, "ratio": args.ratio, "resolution": args.resolution,
        "images": [p.name for p in images], "promptChars": len(prompt),
        "promptFirstLine": prompt.splitlines()[0] if prompt else "",
        "started": time.time(), "startedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "creditsBefore": before, "error": "",
        # 事后对账全靠这三块：核对结果、CLI 真正构造出来的请求、正文首行
        "preflight": pf, "requestSnapshot": pf.get("requestSnapshot") or {},
    }
    save_record(task_id, rec)
    print(json.dumps(info, ensure_ascii=False))

    req = WS.Request(prompt=prompt, images=tuple(images), duration=args.duration,
                     ratio=args.ratio, resolution=args.resolution, audio=audio)
    problems = WS.validate(req)
    if problems:
        rec.update(status="failed", error="；".join(problems), preflight=info.get("preflight"))
        save_record(task_id, rec)
        print("；".join(problems), file=sys.stderr)
        return 2
    try:
        task_token = WS.submit(req, run=run_cli, timeout=300)
    except WS.SubmitError as exc:
        rec.update(status="failed", elapsed=round(time.time() - rec["started"], 1),
                   error=str(exc)[:300])
        save_record(task_id, rec)
        print(rec["error"], file=sys.stderr)
        return 3
    rec.update(taskId=task_token)
    save_record(task_id, rec)

    try:
        src = WS.wait(task_token, out_dir, run=run_cli, timeout=1800)
    except WS.SubmitError as exc:
        rec.update(status="failed", elapsed=round(time.time() - rec["started"], 1),
                   error=str(exc)[:300])
        save_record(task_id, rec)
        print(rec["error"], file=sys.stderr)
        return 3

    final = out_dir / f"成片-{tpl}.mp4"
    if final.exists():
        final.unlink()                       # 同名旧片直接换成新的：不堆副本
    src.replace(final)
    after = credits()
    rec.update(status="done", elapsed=round(time.time() - rec["started"], 1),
               finishedAt=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               video=final.name, videoBytes=final.stat().st_size,
               creditsAfter=after,
               spent=(before - after) if (before is not None and after is not None) else None)
    save_record(task_id, rec)
    print(json.dumps({k: rec[k] for k in ("status", "taskId", "video", "spent", "elapsed")},
                     ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="万相 CLI 生成视频（一条提示词 → 一条片子）")
    ap.add_argument("--id", required=True, help="任务号（analysis/reverse/<id>）")
    ap.add_argument("--template", default="", help="用哪版提示词；省略取最新")
    ap.add_argument("--duration", type=int, default=15)
    ap.add_argument("--ratio", default="9:16", choices=list(WS.RATIOS))
    ap.add_argument("--resolution", default="1080P", choices=list(WS.RESOLUTIONS))
    ap.add_argument("--audio-output", choices=["true", "false"], default="true",
                    help="让模型输出音轨（万相默认 true；正文写了声音却关掉就是自相矛盾）")
    ap.add_argument("--confirm", action="store_true", help="真的提交（会扣灵感值）")
    args = ap.parse_args(argv)

    try:
        if not args.confirm:
            audio = str(args.audio_output).lower() != "false"
            print(json.dumps(plan(args.id, args.template, args.duration,
                                  args.ratio, args.resolution, audio), ensure_ascii=False))
            return 0
        return generate(args)
    except CliEntryError as err:              # 不回退 wan.cmd：宁可不出片，也不出一部参数全错的片
        print(str(err), file=sys.stderr)
        return 3
    except ValueError as err:
        print(str(err), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
