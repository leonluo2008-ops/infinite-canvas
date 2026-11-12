# -*- coding: utf-8 -*-
"""删除合同认落盘片名探针（t_a94ccd30）——重名改名场景删对文件、老任务指纹兜底、未定位如实留痕。

缺陷复述：修复前 `ingest._create_task` 把上传原名写进 `ingestion.video_name`，而桥侧
`server.place_upload` 重名不覆盖（x.mp4 → x-2.mp4）——`hub_api.delete_task` 按原名删
`uploads/<原名>` 会删掉**同名的旧文件**（uploads/官号参考1.mp4 这类真实参考片），
本次真正落地的 x-N.mp4 反而留下，还自报 `uploads_video_removed: true`。

修复（本探针验证的合同）：
  1. `_create_task` 以桥返回落地名写 `video_name`，原名另存 `source_name`（展示用）；
  2. 老任务（任务.json 只有原名）走 `analysis/reverse/<bridge_id>/.源视频指纹.json`
     的「路径」反查 uploads 落盘文件再删；
  3. 候选必须与任务 inbox **同内容**（sha 相等）——同名不同内容的存量片绝不顶锅；
     定位失败照常删任务，但 `uploads_video_removed=false` + `uploads_video_skip` 如实留痕。

隔离与安全（对齐 probe_discuss）：任务根 patch 到临时根、hub_api 晚于补丁 import；
uploads / analysis/reverse 是**真目录**——探针自建的夹具（占位片+两条真上传）跑完全部
按删除合同清掉，占位片由探针亲手删，尾部断言两目录顶层集合跑前跑后完全一致（零新增）。
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from . import config  # noqa: E402
from .bridge import upload_video  # noqa: E402
from .probe_discuss import make_unique_video  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FAILS: list[str] = []
CHECKS = [0]


def check(name: str, cond: bool, detail: str = "") -> bool:
    CHECKS[0] += 1
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"｜{detail}" if detail else ""))
    if not cond:
        FAILS.append(f"{name}｜{detail}")
    return cond


def sha256_file(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def snapshot() -> dict:
    uploads = {p.name for p in (ROOT / "uploads").iterdir()} if (ROOT / "uploads").is_dir() else set()
    analysis = ({p.name for p in (ROOT / "analysis" / "reverse").iterdir()}
                if (ROOT / "analysis" / "reverse").is_dir() else set())
    return {"uploads": uploads, "analysis": analysis}


def write_task(temp_root: Path, local_id: str, bridge_id: str, video_name: str,
               source_name: str, video_bytes: bytes) -> Path:
    """在临时根手工建一条 ingested 任务（inbox 放真视频字节，供删除合同做同内容对账）。"""
    tdir = temp_root / local_id
    inbox = tdir / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / video_name).write_bytes(video_bytes)
    ing = {"zip_sha256": hashlib.sha256(video_bytes).hexdigest(),
           "zip_name": f"probe:{local_id}", "video_name": video_name,
           "text_name": "", "ignored": []}
    if source_name:
        ing["source_name"] = source_name
    task = {"local_id": local_id, "bridge_id": bridge_id, "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "state": "ingested",
            "history": [{"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "from": None, "to": "ingested",
                         "note": "probe_del_landed_video"}],
            "intent": "探针任务：删除合同认落盘片名", "assets": [], "ingestion": ing}
    (tdir / "任务.json").write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
    return tdir


def main() -> int:
    print("[probe-del-landed-video] 删除合同认落盘片名：重名改名删对文件 / 指纹兜底 / 未定位留痕")
    before = snapshot()
    temp_root = Path(tempfile.mkdtemp(prefix="probe-del-landed-"))
    config.PROJECTS_DIR = temp_root            # 任务根指到临时根（真桥看不见这些任务）
    sys.path.insert(0, str(ROOT / "ui"))
    import hub_api                             # noqa: PLC0415 —— 必须晚于 PROJECTS_DIR 补丁
    ok = check("hub_api 任务根 = 探针临时根（隔离生效，绝不碰真实任务）",
               Path(hub_api.TASKS_DIR).resolve() == temp_root.resolve(), str(hub_api.TASKS_DIR))

    stem = f"probe-delname-{int(time.time())}-{os.getpid()}"
    decoy_name = f"{stem}.mp4"
    decoy_path = ROOT / "uploads" / decoy_name
    decoy_bytes = f"decoy-{stem}-{time.time_ns()}".encode("utf-8")
    tmpdir = Path(tempfile.mkdtemp(prefix="probe-del-landed-src-"))
    src = tmpdir / "clip.mp4"
    made = check("ffmpeg 生成逐字节唯一测试短片（同内容投两次用）", bool(make_unique_video(src)),
                 str(src))
    if not made or not ok:
        shutil.rmtree(temp_root, ignore_errors=True)
        shutil.rmtree(tmpdir, ignore_errors=True)
        return 1
    video_bytes = src.read_bytes()
    # 上传叶子名必须就是占位片名（桥按 X-Filename 的叶子名落地改名）——两份同字节、同名、不同目录
    up_a = tmpdir / "a" / decoy_name
    up_b = tmpdir / "b" / decoy_name
    up_a.parent.mkdir(parents=True, exist_ok=True)
    up_b.parent.mkdir(parents=True, exist_ok=True)
    up_a.write_bytes(video_bytes)
    up_b.write_bytes(video_bytes)

    # 占位片：直接写 uploads（不是有效视频没关系——它只需要占住这个名字，逼桥侧重名改名）
    decoy_path.write_bytes(decoy_bytes)
    ok &= check("占位片已落 uploads（同内容上传将从 -2 开始改名）",
                decoy_path.is_file() and decoy_name in snapshot()["uploads"] - before["uploads"],
                str(decoy_path))

    try:
        up1 = upload_video(up_a)               # 叶子名 = 占位片名 → 落地 -2
        up2 = upload_video(up_b)               # 再投一次同内容同名 → 落地 -3
        land1 = str(up1.get("name") or "")
        land2 = str(up2.get("name") or "")
        ok &= check(f"第一次上传被改名落地（{decoy_name} 被占 → -2）",
                    land1 == f"{stem}-2.mp4" and (ROOT / "uploads" / land1).is_file(),
                    f"up1.name={land1!r}")
        ok &= check("第二次同内容同名上传继续改名落地（→ -3）",
                    land2 == f"{stem}-3.mp4" and (ROOT / "uploads" / land2).is_file(),
                    f"up2.name={land2!r}")

        # 任务 A（新任务形态）：video_name=落地名 + source_name=原名 —— 删 A 只掉 -2。
        # 注意顺序：A 删完才建 B/C——同内容的 inbox 同时在盘会触发 _uploads_video_shared
        # 的共享保护（那是给真实共享场景的，夹具里必须串行）。
        tdir_a = write_task(temp_root, "auto-9001", str(up1["id"]), land1, decoy_name, video_bytes)
        res_a = hub_api.delete_task("auto-9001")
        ok &= check("A 删除合同执行成功", bool(res_a.get("ok")) and res_a.get("deleted") == "auto-9001",
                    str(res_a)[:220])
        ok &= check("A uploads_video_removed=true 且删的是落地名 -2（video_name 判定）",
                    res_a.get("uploads_video_removed") is True
                    and res_a.get("uploads_video") == land1
                    and res_a.get("uploads_video_source") == "video_name"
                    and not (ROOT / "uploads" / land1).is_file(),
                    f"uploads_video={res_a.get('uploads_video')!r} source={res_a.get('uploads_video_source')!r}")
        ok &= check("A 删后占位片逐字节仍在（没被顶锅）", decoy_path.is_file()
                    and sha256_file(decoy_path) == hashlib.sha256(decoy_bytes).hexdigest(), "")

        # 任务 B（修复前老任务形态）：任务.json 只有上传原名 —— 指纹「路径」反查删 -3
        write_task(temp_root, "auto-9002", str(up2["id"]), decoy_name, "", video_bytes)
        res_b = hub_api.delete_task("auto-9002")
        ok &= check("B（老任务无落地名）删除合同执行成功", bool(res_b.get("ok")), str(res_b)[:220])
        ok &= check("B 走指纹反查删的是落地名 -3（不是同名的占位片！）",
                    res_b.get("uploads_video_removed") is True
                    and res_b.get("uploads_video") == land2
                    and res_b.get("uploads_video_source") == "fingerprint"
                    and not (ROOT / "uploads" / land2).is_file(),
                    f"uploads_video={res_b.get('uploads_video')!r} source={res_b.get('uploads_video_source')!r}")
        ok &= check("B 删后占位片仍逐字节仍在（修复前这里就是数据事故点）",
                    decoy_path.is_file()
                    and sha256_file(decoy_path) == hashlib.sha256(decoy_bytes).hexdigest(), "")

        # 任务 C：video_name 指向不存在的片、无 bridge_id（无指纹可反查）→ 如实留痕不假报 true
        write_task(temp_root, "auto-9003", "", "probe-delname-missing.mp4", "", video_bytes)
        res_c = hub_api.delete_task("auto-9003")
        ok &= check("C 定位不到 uploads 片：removed=false 且 skip 原因非空（不静默假成功）",
                    res_c.get("ok") is True and res_c.get("uploads_video_removed") is False
                    and bool(res_c.get("uploads_video_skip")),
                    f"removed={res_c.get('uploads_video_removed')} skip={res_c.get('uploads_video_skip')!r}")

        ok &= check("A/B 的专属 analysis/reverse/<bridge_id> 随删除清掉",
                    not (ROOT / "analysis" / "reverse" / str(up1["id"])).exists()
                    and not (ROOT / "analysis" / "reverse" / str(up2["id"])).exists(),
                    f"{up1['id']}, {up2['id']}")
        ok &= check("墓碑写入临时根（编号不复用）",
                    json.loads((temp_root / ".tombstones.json").read_text(encoding="utf-8-sig"))
                    and True, "")
    finally:
        # 收尾：探针亲手删占位片（先验它还是我们写的那份），再断言零新增
        tail_ok = True
        if decoy_path.is_file():
            tail_ok &= sha256_file(decoy_path) == hashlib.sha256(decoy_bytes).hexdigest()
            decoy_path.unlink()
        after = snapshot()
        tail_ok &= after["uploads"] == before["uploads"] and after["analysis"] == before["analysis"]
        new_up = sorted(after["uploads"] - before["uploads"])
        gone_up = sorted(before["uploads"] - after["uploads"])
        ok &= check("尾部零新增：uploads 与 analysis/reverse 顶层集合跑前跑后完全一致",
                    tail_ok, f"uploads 新增={new_up[:6]} 消失={gone_up[:6]}")
        shutil.rmtree(temp_root, ignore_errors=True)
        shutil.rmtree(tmpdir, ignore_errors=True)

    print(f"\n{CHECKS[0] - len(FAILS)}/{CHECKS[0]} 项断言通过" + ("" if not FAILS
          else "；失败项：\n" + "\n".join(f"  - {f}" for f in FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(main())
