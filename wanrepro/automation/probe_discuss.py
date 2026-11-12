# -*- coding: utf-8 -*-
"""AT-3 拆解执行器探针——覆盖 ROADMAP AT-3 全部验收判据。

判据：①done 后 has_* 三真 + 状态到 awaiting_assets；②产物齐不重跑（幂等）；
③环境级故障（假端口连不上）→ 抛错但状态保留 ingested（不误判 failed，重跑即续）。
[1] 用真视频真拆（视频拆解已授权自动执行）。
[3] 用 ffmpeg 现生成的逐字节唯一短片（幂等键=视频内容 sha，固定素材会命中 [1] 的任务）。

自清理（AT-10 C5 规范，对齐 `automation/probe_at9_b1.py::probe_cleanup`）：
  桥侧每收一条片就落 `uploads/<片名>` + `analysis/reverse/<bridge_id>/`。本探针原先跑完不收拾，
  每跑一次在**真**目录留 2 条（[1] 真拆 + [3] 环境故障），历次累积成 uploads 72 个片、
  analysis/reverse 169 个目录。现在成功路径与异常路径（try/finally）都按**生产删除合同**
  删掉本次建的两条任务：`hub_api.delete_task(local_id, confirm_name=local_id)`
  ——任务目录 + 专属 analysis/reverse/<bridge_id> + 专属 uploads 视频 + 墓碑，单一实现，
  不另写删除逻辑；尾部再断言 analysis/reverse 与 uploads 的跑前/跑后集合完全一致（回归零新增）。
  不走 HTTP（probe_at9_b1 的首选路）是因为本探针把任务根指到临时根：真桥 8776 看不见这些任务，
  只能直调同一份 delete_task。
  两条安全约束（临时根看不见真任务，删错不可逆）：
    · `hub_api.TASKS_DIR` 是 import 期求值 → 必须在 `config.PROJECTS_DIR` 补丁**之后** import
      （AT-16 B6 同款约定）；删除前再断言它确实指着临时根，不符就一条都不删
      （真实任务 auto-0001 曾被隔离探针误删一次：AT-14 B2 P0）。
    · `_bridge_still_referenced` / `_uploads_video_shared` 在临时根下扫不到真任务，所以只许删
      **本次新建**的 bridge_id / 片名——删除前逐个与跑前快照对账，撞上跑前已存在的名字直接
      拒绝删除并如实 FAIL（uploads/官号参考1.mp4 是真实参考片，删不得）。
      对账用的是**落地片名**（t_a94ccd30）：[1] 故意撞存量名官号参考1.mp4，桥侧改名落
      官号参考1-<N>.mp4，任务.json 记落地名——删除合同只删本次那条，正是该修复的端到端回归。
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from . import config  # noqa: E402
from .discuss import run_discuss  # noqa: E402
from .ingest import ingest_one  # noqa: E402
from .state import load  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
VIDEO = (ROOT / "uploads" / "官号参考1.mp4").resolve()
PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c626001000000ffff03000006000557bfabd4"
    "0000000049454e44ae426082")


def check(name: str, cond: bool, detail: str = "") -> bool:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))
    return cond


def make_unique_video(dst: Path) -> bool:
    """ffmpeg 现生成一条逐字节唯一的短视频（seed=时间戳+pid）——[3] 的独立素材。

    为什么必须换素材：ingest 的幂等键是**视频内容 sha256**（ingest.py `sha = hashlib.sha256(
    spec["video_bytes"])`），固定用 uploads/官号参考1.mp4 时 [3] 会命中 [1] 刚建的同视频任务
    （已 awaiting_assets），「状态保留 ingested」按构造就不可能成立——那是夹具债，不是产品行为。
    手法同 probe_at9_b1.make_unique_video；[3] 走到「桥连不上」就返回、不做真拆，故 2s 短片足够。
    """
    seed = int(time.time()) % 10 ** 8 + os.getpid() % 89   # gradients 的 seed 是有界整数，禁超 2^31
    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-f", "lavfi", "-i", f"gradients=size=360x640:duration=2:speed=0.1:seed={seed}",
           "-f", "lavfi", "-i", "testsrc2=size=360x640:rate=10:duration=2",
           "-filter_complex", "[0:v][1:v]blend=all_mode=grainmerge:all_opacity=0.35[v]",
           "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-t", "2", str(dst)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"  [FAIL] ffmpeg 生成唯一探针视频 —— {type(exc).__name__}: {exc}")
        return False
    good = proc.returncode == 0 and dst.is_file() and dst.stat().st_size > 0
    print(f"  [{'PASS' if good else 'FAIL'}] ffmpeg 生成唯一探针视频（seed=时间戳+pid，逐字节不重复）"
          + ("" if good else f" —— {(proc.stderr or '')[-200:]}"))
    return good


# ---------------------------------------------------------------- 自清理（AT-10 C5）
def _hub_api():
    """删除合同的唯一入口。**必须**在 config.PROJECTS_DIR 补丁之后 import：TASKS_DIR 是 import 期求值。"""
    sys.path.insert(0, str(ROOT / "ui"))       # hub_api 在 ui/（不是包，靠 path 兜底）
    import hub_api                             # noqa: PLC0415 —— 晚于补丁，见上
    return hub_api


def _snapshot(hub) -> dict:
    """真目录跑前/跑后快照：analysis/reverse 与 uploads 的顶层目录名、文件名集合。

    只取顶层：删除合同删的就是这两个根下的顶层子树（projects/<id>、analysis/<bridge_id>、
    uploads/<片名>），顶层集合相等即「一条没多、一条没少」。
    """
    out: dict[str, dict[str, set]] = {}
    for label, root in (("analysis", Path(hub._analysis_reverse())), ("uploads", Path(hub.UPLOADS_DIR))):
        dirs: set[str] = set()
        files: set[str] = set()
        if root.is_dir():
            for p in root.iterdir():
                (dirs if p.is_dir() else files).add(p.name)
        out[label] = {"dirs": dirs, "files": files}
    return out


def _diff_text(before: dict, after: dict) -> str:
    parts = []
    for label in ("analysis", "uploads"):
        b, a = before[label], after[label]
        new = sorted(a["dirs"] - b["dirs"]) + sorted(a["files"] - b["files"])
        gone = sorted(b["dirs"] - a["dirs"]) + sorted(b["files"] - a["files"])
        parts.append(f"{label} {len(b['dirs'])}→{len(a['dirs'])} 目录 / "
                     f"{len(b['files'])}→{len(a['files'])} 文件"
                     + (f"｜新增={new[:6]}" if new else "")
                     + (f"｜消失={gone[:6]}" if gone else ""))
    return "；".join(parts)


def probe_cleanup(hub, created: list[tuple[str, str, str]], before: dict) -> bool:
    """删掉本次建的任务（生产删除合同 + 删除前三重对账）。返回断言结果。

    对账（任一不过就跳过删除并 FAIL——残留如实留着，绝不拿别人的文件顶锅）：
      1) hub_api 的任务根 = 探针临时根（否则 delete_task 会去真实 projects 找同名任务）；
      2) 本次 bridge_id 不在跑前 analysis 集合里；
      3) 本次片名不在跑前 uploads 集合里。
    """
    print("[4] 自清理：按删除合同删掉本次建的任务（回归零新增）")
    if not created:
        return check("本次没有新建任务（无需清理）", True)

    temp_root = Path(config.PROJECTS_DIR).resolve()
    if Path(hub.TASKS_DIR).resolve() != temp_root:
        return check("删除只作用于探针临时根（hub_api 任务根 = config.PROJECTS_DIR）", False,
                     f"hub_api.TASKS_DIR={hub.TASKS_DIR} ≠ {temp_root}——拒绝删除，防误删真实任务")
    ok = check("删除只作用于探针临时根（hub_api 任务根 = config.PROJECTS_DIR）", True, str(temp_root))

    analysis_root = Path(hub._analysis_reverse())
    uploads_root = Path(hub.UPLOADS_DIR)
    for local_id, bridge_id, video_name in created:
        pre_ok = check(f"{local_id}｜bridge_id「{bridge_id}」跑前不存在（只删本次新建）",
                       bool(bridge_id) and bridge_id not in before["analysis"]["dirs"],
                       f"bridge_id={bridge_id}")
        pre_ok &= check(f"{local_id}｜片名「{video_name}」跑前不在 uploads（只删本次新建）",
                        bool(video_name) and video_name not in before["uploads"]["files"],
                        f"片名={video_name}")
        if not pre_ok:
            ok = False
            print(f"  [skip] {local_id} 未删：前置对账不过，残留如实留着")
            continue
        try:
            res = hub.delete_task(local_id, confirm_name=local_id)
        except Exception as exc:  # noqa: BLE001 —— 失败原文进断言，不静默
            ok &= check(f"{local_id} 删除合同执行成功", False, f"{type(exc).__name__}: {exc}")
            continue
        ok &= check(f"{local_id} 删除合同执行成功（state_was={res.get('state_was')}）",
                    bool(res.get("ok")) and res.get("deleted") == local_id, str(res)[:220])
        ok &= check(f"{local_id} 任务目录已删除（projects/<id> 不在盘上）",
                    not (Path(config.PROJECTS_DIR) / local_id).exists(),
                    str(Path(config.PROJECTS_DIR) / local_id))
        ok &= check(f"{local_id} 专属 analysis/reverse/{bridge_id} 随任务删除（不留在真目录）",
                    res.get("analysis_dir_removed") is True and not (analysis_root / bridge_id).exists(),
                    f"{analysis_root / bridge_id}｜flag={res.get('analysis_dir_removed')}")
        ok &= check(f"{local_id} 专属 uploads/{video_name} 随任务删除（片名对上才删）",
                    res.get("uploads_video_removed") is True
                    and not (uploads_root / video_name).is_file(),
                    f"{uploads_root / video_name}｜flag={res.get('uploads_video_removed')}")
    return ok


def probe_zero_new(hub, before: dict) -> bool:
    """尾部断言：跑后 analysis/reverse 与 uploads 的顶层集合与跑前完全一致。"""
    after = _snapshot(hub)
    detail = _diff_text(before, after)
    ok = check("跑后 analysis/reverse 顶层集合与跑前完全一致（零新增）",
               after["analysis"] == before["analysis"], detail)
    ok &= check("跑后 uploads 顶层集合与跑前完全一致（零新增）",
                after["uploads"] == before["uploads"], detail)
    return ok


def run() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="probe-discuss-"))
    config.PROJECTS_DIR = tmp / "projects"       # 隔离根：任务目录不进真实 projects
    ok = True
    created: list[tuple[str, str, str]] = []     # 本次新建的 (local_id, bridge_id, 片名)
    hub = None
    before: dict | None = None
    try:
        if not VIDEO.is_file():
            print(f"[setup] 缺测试视频 {VIDEO}")
            return 1
        hub = _hub_api()                          # 晚于 PROJECTS_DIR 补丁：TASKS_DIR = 临时根
        before = _snapshot(hub)
        print(f"[setup] 隔离任务根 {config.PROJECTS_DIR}；真实目录基线 "
              f"analysis/reverse {len(before['analysis']['dirs'])} 目录、"
              f"uploads {len(before['uploads']['files'])} 文件")

        print("[1] 真拆：ingest → discuss → awaiting_assets + 三真")
        # 片名故意撞存量（t_a94ccd30 修复验证）：uploads/官号参考1.mp4 是真实参考片，
        # 桥侧 place_upload 重名不覆盖 → 本次落地为 官号参考1-<N>.mp4。修复后任务.json
        # video_name 记**落地名**（删除合同只删本次那条）、原名另存 source_name；
        # 修复前记原名，删除合同会拿旧文件顶锅（数据事故）。素材内容一字不改（幂等键=视频内容 sha）。
        ref_name = VIDEO.name
        z = tmp / "t.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.write(VIDEO, ref_name)
            zf.writestr("资产-主角.png", PNG_1PX)
            zf.writestr("意图.txt", "自动化探针任务：同款创意")
        r = ingest_one(z)
        landed = str(r.get("landed_video_name") or "")
        created.append((r["local_id"], r["bridge_id"], landed))
        ok &= check("[1] 重名上传被桥改名落地，任务.json 记落地名（删除合同删的就是本次那条）",
                    r.get("video_name") == ref_name and landed != ref_name
                    and (Path(hub.UPLOADS_DIR) / landed).is_file(),
                    f"原名={r.get('video_name')!r} 落地名={landed!r}")
        st_json = load(config.PROJECTS_DIR / r["local_id"])
        ok &= check("[1] 任务.json：video_name=落地名、source_name=上传原名",
                    (st_json.get("ingestion") or {}).get("video_name") == landed
                    and (st_json.get("ingestion") or {}).get("source_name") == ref_name,
                    str(st_json.get("ingestion"))[:200])
        tdir = config.PROJECTS_DIR / r["local_id"]
        st = run_discuss(tdir)
        t = load(tdir)
        from .state import probe_bridge_products
        p = probe_bridge_products(t["bridge_id"])
        ok &= check("终态 awaiting_assets", st == "awaiting_assets")
        ok &= check("has_* 三真", bool(p and p["has_disc"] and p["has_check"] and p["has_prompt"]),
                    str(p))
        ok &= check("history 有拆解记录", any(h["to"] == "discussing" for h in t["history"]))

        print("[2] 幂等：产物齐 → SKIP 不重跑")
        st2 = run_discuss(tdir)
        t2 = load(tdir)
        ok &= check("SKIP 返回 awaiting_assets", st2 == "awaiting_assets")
        ok &= check("discussing 只出现一次", sum(1 for h in t2["history"] if h["to"] == "discussing") == 1)

        print("[3] 环境故障：假端口 → 抛错但状态保留（连不上≠任务失败）")
        # 独立素材：[3] 必须建**新**任务。幂等键=视频内容 sha，复用 [1] 的同一条片会命中旧任务
        # （awaiting_assets），「状态保留 ingested」就永不可能成立（历史夹具债，卡 t_d6733299）。
        uniq = tmp / f"probe-discuss-{int(time.time())}-{os.getpid()}.mp4"
        if not make_unique_video(uniq):
            ok = False
            uniq = None
        if uniq is not None:
            z3 = tmp / "t3.zip"
            with zipfile.ZipFile(z3, "w") as zf:
                zf.write(uniq, uniq.name)      # 每次换新素材 → 新视频 sha → 新任务
                zf.writestr("意图.txt", "失败路径探针")
            r3 = ingest_one(z3)
            created.append((r3["local_id"], r3["bridge_id"],
                            str(r3.get("landed_video_name") or r3["video_name"])))
            tdir3 = config.PROJECTS_DIR / r3["local_id"]
            # 夹具自检（前置，不放宽任何判据）：若又命中幂等，下面两条断言必然假红，先在这里判死
            ok &= check("独立素材建了新任务（未幂等命中 [1] 的同视频任务）",
                        r3.get("result") == "OK" and r3.get("hit_existing") is False and tdir3 != tdir,
                        str(r3)[:220])
            real = config.BRIDGE_BASE
            config.BRIDGE_BASE = "http://127.0.0.1:1"  # 不存在端口
            try:
                run_discuss(tdir3, verbose=False)
                ok &= check("假端口应抛错", False, "居然没抛（环境故障被吞了）")
            except Exception as e:  # noqa: BLE001
                t3 = load(tdir3)
                ok &= check("抛的是环境错", "连不上" in str(e) or "Connection" in str(e), type(e).__name__)
                ok &= check("状态保留 ingested（不误判 failed）", t3["state"] == "ingested", t3["state"])
            finally:
                config.BRIDGE_BASE = real
    finally:
        # 自清理放在 finally：成功路径、中途异常、Ctrl-C 都会走到这里（不留任务）
        try:
            if hub is not None and before is not None:
                try:
                    ok &= probe_cleanup(hub, created, before)
                    ok &= probe_zero_new(hub, before)
                except Exception as exc:  # noqa: BLE001 —— 清理自身炸掉也必须留痕，继续收临时根
                    print(f"  [FAIL] 自清理/零新增对账自身异常：{type(exc).__name__}: {exc}")
                    ok = False
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'ALL PASS' if ok else 'HAS FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(run())
