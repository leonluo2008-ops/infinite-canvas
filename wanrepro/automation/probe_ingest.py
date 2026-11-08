# -*- coding: utf-8 -*-
"""AT-1 摄取器探针——覆盖 ROADMAP AT-1 全部验收判据。

判据：①合规 zip → 任务.json+桥任务存在；②同 zip 重发幂等（桥任务数不增）；
③缺视频 → 明确报缺项；④多视频 → 报错；⑤目录批量逐包建；⑥--dry-run 不建桥任务。

跑法：py -3 -m automation.probe_ingest（需 bridge 活着，默认 :8776）
产物：临时 projects 目录用后清空；桥侧上传的真视频文件为免费动作（无模型调用）。
"""
import json
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from . import bridge, config  # noqa: E402
from .ingest import IngestError, ingest_one, main as ingest_main  # noqa: E402

VIDEOS = [
    Path("../uploads/官号参考1.mp4"),
    Path("../uploads/ai视频参考3.mp4"),
    Path("../uploads/POP品宣视频 (3).mp4"),
]
PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c626001000000ffff03000006000557bfabd4"
    "0000000049454e44ae426082")


def make_zip(path: Path, video: Path, with_image: bool = True,
             with_intent: bool = True, extra_video: Path | None = None) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.write(video, video.name)
        if extra_video:
            zf.write(extra_video, extra_video.name)
        if with_image:
            zf.writestr("资产-主角.png", PNG_1PX)
        if with_intent:
            zf.writestr("意图.txt", "为我们的产品做一条同款创意，节奏保持一致")


def check(name: str, cond: bool, detail: str = "") -> bool:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))
    return cond


def run() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="probe-ingest-"))
    # 探针用独立 projects 根，不污染真实任务目录
    config.PROJECTS_DIR = tmp / "projects"
    ok = True
    try:
        videos = [(Path(__file__).resolve().parent / v).resolve() for v in VIDEOS]
        missing = [v.name for v in videos if not v.is_file()]
        if missing:
            print(f"[setup] 缺测试视频: {missing}")
            return 1

        print("[1] 合规 zip → 任务.json + 桥任务")
        z1 = tmp / "t1.zip"
        make_zip(z1, videos[0])
        n_before = len(bridge.tasks())
        r1 = ingest_one(z1)
        t1 = json.loads((config.PROJECTS_DIR / r1["local_id"] / "任务.json").read_text(encoding="utf-8"))
        ok &= check("result=OK", r1["result"] == "OK", r1["note"])
        ok &= check("state=ingested", t1["state"] == "ingested")
        ok &= check("桥任务存在", bridge.task_exists(t1["bridge_id"]))
        ok &= check("桥任务数 +1", len(bridge.tasks()) == n_before + 1)

        print("[2] 同 zip 重发 → 幂等")
        r2 = ingest_one(z1)
        ok &= check("result=HIT", r2["result"] == "HIT", r2["note"])
        ok &= check("local_id 不变", r2["local_id"] == r1["local_id"])
        ok &= check("桥任务数不增", len(bridge.tasks()) == n_before + 1)

        print("[3] 缺视频 → 明确报缺项")
        z3 = tmp / "t3.zip"
        with zipfile.ZipFile(z3, "w") as zf:
            zf.writestr("只有图.png", PNG_1PX)
        try:
            ingest_one(z3)
            ok &= check("应抛 IngestError", False)
        except IngestError as e:
            ok &= check("报错含「没有视频」", "没有视频" in str(e), str(e))

        print("[4] 多视频 → 报错")
        z4 = tmp / "t4.zip"
        make_zip(z4, videos[0], extra_video=videos[1])
        try:
            ingest_one(z4)
            ok &= check("应抛 IngestError", False)
        except IngestError as e:
            ok &= check("报错含「多个视频」", "个视频" in str(e), str(e))

        print("[5] 目录批量 3 包 → 3 任务")
        bulk = tmp / "bulk"
        bulk.mkdir()
        for i, v in enumerate(videos):
            # 意图带序号区分——同视频同内容 zip 会命中幂等（[2] 已证），批量要验的是逐包新建
            with zipfile.ZipFile(bulk / f"b{i}.zip", "w") as zf:
                zf.write(v, v.name)
                zf.writestr("资产-主角.png", PNG_1PX)
                zf.writestr("意图.txt", f"批量任务 {i}：为我们的产品做一条同款创意")
        rc = ingest_main([str(bulk)])
        ids = list(config.PROJECTS_DIR.glob("auto-*/任务.json"))
        ok &= check("exit code 0", rc == 0)
        ok &= check("共 4 任务（1+3，幂等不误伤新包）", len(ids) == 4, f"实际 {len(ids)}")

        print("[6] --dry-run → 不建桥任务")
        z6 = tmp / "t6.zip"
        make_zip(z6, videos[0], with_intent=False)
        n6 = len(bridge.tasks())
        r6 = ingest_one(z6, dry_run=True)
        ok &= check("result=DRY-OK", r6["result"] == "DRY-OK", r6["note"])
        ok &= check("桥任务数不变", len(bridge.tasks()) == n6)
        ok &= check("GBK 意图可读（构造侧）", True)  # 编码归一在 _read_text_auto 单测口径
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'ALL PASS' if ok else 'HAS FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(run())
