# -*- coding: utf-8 -*-
"""AT-1 任务摄取器——zip（或目录批量）→ 校验 → 桥任务 → 任务.json。

约定（宽松，docs/需求-自动化任务.md v1.1 待裁定项 1 的建议口径）：
  zip 内容：视频恰好 1 个（.mp4/.mov/.webm/.avi/.mkv）；资产图 0..N 张；
  意图文本 0..1 个（.txt/.md，取第一个，UTF-8/GBK 自动识别）；
  其余文件忽略但在任务.json 里留清单（不静默丢弃信息）。

幂等：zip 内容 sha256 为幂等键——同 zip 重发返回已有任务，不重复建桥任务。

返回契约（AT-10 C5）：所有入口都带回 `hit_existing`（True=命中已有任务未新建、False=新建）
+ `result`（HIT / OK / DRY-OK）+ `video_name`——前端据此弹「该视频已有任务 auto-00xx」并
打开该任务，而不是重走建任务/拆解（旧字段保留，调用方零破坏）。

产物：projects/auto-<N>/任务.json（AT-2 状态机的载体，state 恒从 ingested 起）。

CLI：
  py -3 -m automation.ingest <zip 或目录> [--dry-run]
  --dry-run：只校验解包不上传（免费预检）；正式摄取会真建桥任务（免费，
  只是文件落盘+ffprobe 指纹，无模型调用——与「视频拆解已授权」口径一致的上传层）。

失败即失败：任何 zip 校验不过 → 该包记 FAIL 原因，exit code 非 0；绝不静默跳过。
"""
import argparse
import hashlib
import io
import json
import re
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from . import bridge, config

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_text_auto(data: bytes) -> str:
    """意图文本读层归一：UTF-8 失败试 GBK，都失败按 replace（红线：不假设格式）。"""
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return data.decode(enc).strip()
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace").strip()


def inspect_zip(zip_path: Path) -> dict:
    """校验并解析 zip → {video_name, video_bytes, assets, intent, ignored}。

    校验不过抛 IngestError（带明确缺项/问题，不静默）。
    """
    videos, assets, texts, ignored = [], [], [], []
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = Path(info.filename).name  # 扁平取叶子名，嵌套目录结构不敏感
            ext = Path(name).suffix.lower()
            if ext in config.VIDEO_EXTS:
                videos.append((name, info))
            elif ext in config.IMAGE_EXTS:
                assets.append((name, info))
            elif ext in config.TEXT_EXTS:
                texts.append((name, info))
            else:
                ignored.append(name)
    if not videos:
        raise IngestError("zip 里没有视频（支持 " + "/".join(sorted(config.VIDEO_EXTS)) + "）")
    if len(videos) > 1:
        raise IngestError(f"zip 里有 {len(videos)} 个视频（{', '.join(n for n, _ in videos)}）——一个任务一条片，请拆包")
    with zipfile.ZipFile(zip_path) as zf:
        vname, vinfo = videos[0]
        vbytes = zf.read(vinfo)
        intent = ""
        if texts:
            intent = _read_text_auto(zf.read(texts[0][1]))
        asset_names = [n for n, _ in assets]
    if not vbytes:
        raise IngestError(f"视频 {vname} 是 0 字节")
    return {"video_name": vname, "video_bytes": vbytes, "assets": asset_names,
            "intent": intent, "ignored": ignored, "text_name": texts[0][0] if texts else ""}


class IngestError(RuntimeError):
    """摄取校验失败——message 面向人（工作人员能看懂缺什么）。"""


def ingest_media(video: Path, intent: str, *, images: list[Path] | None = None,
                 source_note: str = "") -> dict:
    """直连摄取入口（Notion 中控台用）：视频文件 + 意图文本 + 可选图，不经 zip。

    幂等键=视频内容 sha256（与 zip 路径共用 projects/*/任务.json 的 ingestion.zip_sha256
    字段——同视频无论从 zip 还是 Notion 来都命中同一条）。
    """
    if not video.is_file() or video.stat().st_size <= 0:
        raise IngestError(f"视频文件不可用（不存在或 0 字节）: {video}")
    if video.suffix.lower() not in config.VIDEO_EXTS:
        raise IngestError(f"不认的视频格式 {video.suffix}（支持 {'/'.join(sorted(config.VIDEO_EXTS))}）")
    sha = _sha256_file(video)
    existing = _existing_by_sha().get(sha)
    if existing:
        d = json.loads((existing / "任务.json").read_text(encoding="utf-8"))
        return {"local_id": d["local_id"], "bridge_id": d.get("bridge_id"),
                "result": "HIT", "hit_existing": True,
                "video_name": (d.get("ingestion") or {}).get("video_name", ""),
                "note": f"同视频已摄取过（{d['local_id']}），未重复建任务"}
    return _create_task(video_name=video.name, video_bytes=video.read_bytes(),
                        intent=intent.strip(), assets=[p.name for p in (images or [])],
                        ignored=[], sha=sha,
                        source=source_note or f"media:{video.name}")


def _iter_zips(target: Path) -> list[Path]:
    if target.is_file() and target.suffix.lower() == ".zip":
        return [target]
    if target.is_dir():
        zips = sorted(target.glob("*.zip"))
        if not zips:
            raise IngestError(f"目录里没有 zip：{target}")
        return zips
    raise IngestError(f"不是 zip 文件也不是目录：{target}")


def _existing_by_sha() -> dict[str, Path]:
    """projects/*/任务.json 的 zip_sha256 → 任务目录（幂等查找）。"""
    out: dict[str, Path] = {}
    if not config.PROJECTS_DIR.is_dir():
        return out
    for mf in config.PROJECTS_DIR.glob("auto-*/任务.json"):
        try:
            d = json.loads(mf.read_text(encoding="utf-8"))
            sha = d.get("ingestion", {}).get("zip_sha256")
            if sha:
                out[sha] = mf.parent
        except (json.JSONDecodeError, OSError):
            continue  # 坏文件不挡新摄取，但也不会被当幂等命中
    return out


TOMBSTONE_NAME = ".tombstones.json"      # 删除墓碑（AT-9 数据合同 B0）：projects/.tombstones.json


def tombstone_file() -> Path:
    return config.PROJECTS_DIR / TOMBSTONE_NAME


def load_tombstones() -> list[dict]:
    """墓碑 → [{id,时间}]；文件缺/坏 → []（坏条目跳过，不挡摄取）。"""
    f = tombstone_file()
    if not f.is_file():
        return []
    try:
        data = json.loads(f.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict) and str(item.get("id") or "").strip()]


def _tombstoned_nums() -> set[int]:
    out: set[int] = set()
    for item in load_tombstones():
        m = re.fullmatch(r"auto-(\d+)", str(item.get("id") or ""))
        if m:
            out.add(int(m.group(1)))
    return out


def _next_local_id() -> str:
    ensure = config.PROJECTS_DIR
    ensure.mkdir(parents=True, exist_ok=True)
    nums = [int(p.name.split("-")[1]) for p in ensure.glob("auto-*")
            if p.is_dir() and p.name.split("-")[1].isdigit()]
    # 编号跳过墓碑（AT-9 删除合同）：删掉的任务号不复用，防旧引用/旧产物错接到新任务
    nums.extend(_tombstoned_nums())
    return f"auto-{max(nums, default=0) + 1:04d}"


def _create_task(*, video_name: str, video_bytes: bytes, intent: str, assets: list[str],
                 ignored: list[str], sha: str, source: str) -> dict:
    """共同建任务路径（zip / Notion 直连两入口收敛于此——同一动作单一实现）。"""
    local_id = _next_local_id()
    tdir = config.PROJECTS_DIR / local_id
    inbox = tdir / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / video_name).write_bytes(video_bytes)

    up = bridge.upload_video(inbox / video_name)
    if not bridge.task_exists(up["id"]):
        raise IngestError(f"上传回了 id={up['id']} 但 /api/tasks 里查不到——桥任务未建成")

    task = {
        "local_id": local_id,
        "bridge_id": up["id"],
        "created_at": _now(),
        "state": "ingested",
        "history": [{"at": _now(), "from": None, "to": "ingested",
                     "note": f"摄取自 {source}"}],
        "intent": intent,
        "assets": assets,
        "ingestion": {"zip_sha256": sha, "zip_name": source,
                      "video_name": video_name, "text_name": "",
                      "ignored": ignored},
    }
    (tdir / "任务.json").write_text(
        json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"local_id": local_id, "bridge_id": up["id"], "result": "OK",
            "hit_existing": False, "video_name": video_name,
            "note": f"视频 {video_name} 已建桥任务 {up['id']}；"
                    f"资产图 {len(assets)} 张待置换；意图 {len(intent)} 字"}


def ingest_one(zip_path: Path, *, dry_run: bool = False) -> dict:
    """摄取单个 zip → 任务摘要 dict（local_id/bridge_id/状态/说明）。"""
    sha = _sha256_file(zip_path)
    existing = _existing_by_sha().get(sha)
    if existing:
        d = json.loads((existing / "任务.json").read_text(encoding="utf-8"))
        return {"local_id": d["local_id"], "bridge_id": d.get("bridge_id"),
                "result": "HIT", "hit_existing": True,
                "video_name": (d.get("ingestion") or {}).get("video_name", ""),
                "note": f"同 zip 已摄取过（{d['local_id']}），未重复建任务"}

    spec = inspect_zip(zip_path)
    if dry_run:
        return {"local_id": None, "bridge_id": None, "result": "DRY-OK",
                "hit_existing": False, "video_name": spec["video_name"],
                "note": f"校验通过：视频 {spec['video_name']}，资产图 {len(spec['assets'])} 张，"
                        f"意图 {len(spec['intent'])} 字——未上传（--dry-run）"}
    # 幂等键=视频内容 sha（不是 zip 文件 sha）：同片无论打 zip 还是从工作台直传都命中同一条
    sha = hashlib.sha256(spec["video_bytes"]).hexdigest()
    existing = _existing_by_sha().get(sha)
    if existing:
        d = json.loads((existing / "任务.json").read_text(encoding="utf-8"))
        return {"local_id": d["local_id"], "bridge_id": d.get("bridge_id"),
                "result": "HIT", "hit_existing": True,
                "video_name": (d.get("ingestion") or {}).get("video_name", ""),
                "note": f"同视频已摄取过（{d['local_id']}），未重复建任务"}

    return _create_task(video_name=spec["video_name"], video_bytes=spec["video_bytes"],
                        intent=spec["intent"], assets=spec["assets"],
                        ignored=spec["ignored"], sha=sha, source=zip_path.name)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="AT-1 任务摄取器")
    ap.add_argument("target", help="zip 文件路径，或含多个 zip 的目录")
    ap.add_argument("--dry-run", action="store_true", help="只校验不上传")
    ap.add_argument("--bridge", help=f"bridge 基地址（默认 {config.BRIDGE_BASE}）")
    args = ap.parse_args(argv)
    if args.bridge:
        config.BRIDGE_BASE = args.bridge.rstrip("/")

    target = Path(args.target)
    fails: list[tuple[str, str]] = []
    for zp in _iter_zips(target):
        try:
            r = ingest_one(zp, dry_run=args.dry_run)
            print(f"[{r['result']}] {zp.name} → {r['local_id'] or '-'}: {r['note']}")
        except (IngestError, bridge.BridgeError) as e:
            fails.append((zp.name, str(e)))
            print(f"[FAIL] {zp.name}: {e}")
    if fails:
        print(f"\n{len(fails)} 个包摄取失败（原文如上，未静默跳过）", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
