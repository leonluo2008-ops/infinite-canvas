# -*- coding: utf-8 -*-
"""卡C路A（2026-11-15）：变体任务直建——在**既有拆解产物**（bridge_id）上直接开新任务。

合同来源：`docs/方案-卡C一拆多用变体派生-20261115.md`（路线 A：拆解完成即可派生，不要求源先出片）
+ `docs/审计-卡C-路2-Claude-20261115.md` N1/N2。与 fork（automation/fork.py）的分工：

| 维度 | fork（既有，零改动） | variant（本模块） |
|---|---|---|
| 入口身份 | 一条 done 的 hub 任务（源任务只读） | 一份拆解产物 bridge_id（画布拉片链产物，可能没有对应 hub 任务） |
| 源视频 | 复制源任务 inbox | 解析既有副本：显式指定 → 同 bridge 任务 inbox → uploads 落地副本 |
| 幂等 | —— | **刻意绕过** ingest 的视频 sha 幂等键（ingestion 不写 zip_sha256）：同源视频 N 个变体各得独立任务号——这是本入口存在的意义 |
| 出生态 | state.FORK_BOOTSTRAP_STATE | state.VARIANT_BOOTSTRAP_STATE（同为 awaiting_assets） |
| 拆解产物 | 同 bridge_id 零复制共享只读 | 同 bridge_id 零复制共享只读（复用 fork.py 的共享引用模式；删除守卫 `_bridge_still_referenced` 按 bridge_id 天然覆盖变体） |

不动的东西：fork.py 既有函数 / FORKABLE_STATES（done-fork 行为零变化）；源画布实例与其他任务
一个字节不碰（本模块只读解析源视频副本，写入只发生在新变体任务目录里）。

清单/侧车：复制共享份为**私有副本**（checklist_private=true，replace.private_scope 判定）。
不做 `local:<源号>/` 引用重写——变体没有「源 hub 任务」，共享清单里也不存在可重写的源号引用
（画布链产物的资产引用按 bridge 侧路径落）；此后 hub 链对变体的清单读写全走私有副本。
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from . import config, ingest, replace, state

# uploads 落地副本根（wanrepro/uploads）。模块级常量：隔离探针 monkeypatch 它即可测
# 「指纹 → uploads 副本」解析路径，不用碰真实 uploads 一个字节。
UPLOADS_ROOT = Path(__file__).resolve().parents[1] / "uploads"
FP_NAME = ".源视频指纹.json"          # 与 ui/hub_api.FP_NAME / ui/server.py FP_NAME 同名合同


class VariantError(RuntimeError):
    """变体直建失败——message 面向人（说清缺什么、拆解产物怎么了）。"""


def _sha_first_mb(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with path.open("rb") as f:
        h.update(f.read(1024 * 1024))
    return h.hexdigest()


def _video_from_task_inbox(bridge_id: str) -> Path | None:
    """同 bridge_id 既有任务的 inbox 里找源视频副本（fork._video_of 的同 bridge 扫描变体）。"""
    candidates: list[Path] = []
    for mf in sorted(config.PROJECTS_DIR.glob("auto-*/任务.json")):
        try:
            data = json.loads(mf.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if str(data.get("bridge_id") or "") != bridge_id:
            continue
        inbox = mf.parent / "inbox"
        if not inbox.is_dir():
            continue
        candidates.extend(p for p in inbox.iterdir()
                          if p.is_file() and p.suffix.lower() in config.VIDEO_EXTS)
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        # 同 bridge 多条副本 → 全部同 sha（摄取守卫），任取第一份即可；不报错不打断变体直建。
        return sorted(candidates)[0]
    return None


def _video_from_uploads(bridge_id: str) -> Path | None:
    """拆解指纹记录的 uploads 落地副本（画布拉片链的产物形态：fingerprint.路径 = uploads/<落地名>）。"""
    fp = replace.analysis_root() / bridge_id / FP_NAME
    if not fp.is_file():
        return None
    try:
        rec = json.loads(fp.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    raw = str(rec.get("路径") or "").strip()
    if not raw:
        return None
    name = Path(raw).name                       # 只取落地名：防指纹被改写成路径穿越
    cand = UPLOADS_ROOT / name
    if cand.is_file() and cand.stat().st_size > 0:
        return cand
    return None


def resolve_source_video(bridge_id: str) -> Path:
    """定位源视频副本：同 bridge 任务 inbox → uploads 落地副本。都定不到就抛。

    为什么这个顺序：任务 inbox 是摄取时逐字节抄的（删除守卫按 inbox 同 sha 判「还在被用」），
    最稳；uploads 副本次之（bridge 落地段写的，同内容）。两条路都断 = 这条 bridge 的源片已不可得，
    变体建出来也没法重新出片——如实报错，不落半套。
    """
    video = _video_from_task_inbox(bridge_id) or _video_from_uploads(bridge_id)
    if video is None:
        raise VariantError(
            f"拆解产物 {bridge_id} 定位不到源视频副本（同 bridge 任务的 inbox 与 uploads 落地副本都空）——"
            "没有源片就没法重新出片，先确认这条拆解的源视频还在")
    return video


def create_variant_task(bridge_id: str, intent: str, *, video: Path | None = None,
                        note: str = "") -> dict:
    """从既有拆解产物直建一个变体任务 → {local_id, bridge_id, state, 源视频, 复制明细...}。

    只写新任务目录；bridge 产物目录 / 其他任务 / uploads 全程只读。
    """
    bridge = str(bridge_id or "").strip()
    if not bridge:
        raise VariantError("bridge_id 不能为空：变体直建挂的是既有拆解产物")
    intent = str(intent or "").strip()
    if not intent:
        raise VariantError("意图（intent）不能为空：变体的意图落在它自己的任务上，不继承猜值")
    try:
        shared_md = replace.checklist_path(bridge)
    except replace.ReplaceError as exc:
        raise VariantError(f"拆解产物不在（bridge_id={bridge} 没有可复用的待确认清单）：{exc}") from None

    if video is not None:
        video = Path(video)
        if not video.is_file() or video.stat().st_size <= 0:
            raise VariantError(f"指定的源视频不可用（不存在或 0 字节）: {video}")
        if video.suffix.lower() not in config.VIDEO_EXTS:
            raise VariantError(f"不认的视频格式 {video.suffix}（支持 {'/'.join(sorted(config.VIDEO_EXTS))}）")
    else:
        video = resolve_source_video(bridge)

    # 编号复用 ingest._next_local_id（墓碑号自动跳过）；幂等键刻意绕过：ingestion 不写 zip_sha256，
    # 同源视频 N 个变体各得独立任务号（本入口存在的意义，见模块头注释）。
    new_id = ingest._next_local_id()
    new_dir = config.PROJECTS_DIR / new_id
    if new_dir.exists():
        raise VariantError(f"目标任务目录已存在：{new_dir}（编号分配撞车，未做任何复制）")
    new_dir.mkdir(parents=True)
    inbox = new_dir / "inbox"
    inbox.mkdir()
    (inbox / video.name).write_bytes(video.read_bytes())

    # 拆解指纹：同抄（自述冗余，不改任何读路径——与 fork 同口径）
    fp_src = replace.analysis_root() / bridge / FP_NAME
    fp_copied = False
    if fp_src.is_file():
        (new_dir / FP_NAME).write_bytes(fp_src.read_bytes())
        fp_copied = True

    # 私有清单副本（逐字节照抄，不做 local: 重写——变体没有源 hub 任务号可重写）+ 私有侧车
    (new_dir / replace.LIST_NAME).write_bytes(shared_md.read_bytes())
    shared_side = replace.sidecar_path(bridge)
    side_copied = False
    if shared_side.is_file():
        (new_dir / replace.SIDECAR_NAME).write_bytes(shared_side.read_bytes())
        side_copied = True

    task: dict = {
        "local_id": new_id,
        "bridge_id": bridge,
        "created_at": state._now(),
        "state": state.VARIANT_BOOTSTRAP_STATE,
        "history": [],
        "intent": intent,
        "assets": [],
        # ingestion 刻意**不含 zip_sha256**（幂等键绕过）；variant 标记供排障/对账辨认出生通道
        "ingestion": {"video_name": video.name, "source_name": video.name,
                      "text_name": "", "ignored": [], "variant": True},
        "pending_replace": [],
        replace.PRIVATE_FIELD: True,
        "variant_of": {
            "bridge_id": bridge,
            "at": state._now(),
            "video": video.name,
            "video_bytes": video.stat().st_size,
            "源视频指纹": _sha_first_mb(video),
        },
    }
    state.init_variant_task(new_dir, task,
                            note=note or f"变体直建自拆解产物 {bridge}（未跑拆解，未跑模型）")
    return {
        "local_id": new_id,
        "bridge_id": bridge,
        "state": state.VARIANT_BOOTSTRAP_STATE,
        "video": video.name,
        "video_bytes": video.stat().st_size,
        "指纹同抄": fp_copied,
        "私有清单": str(new_dir / replace.LIST_NAME),
        "私有侧车": str(new_dir / replace.SIDECAR_NAME) if side_copied else "",
    }
