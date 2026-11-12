# -*- coding: utf-8 -*-
"""AT-21 B0：fork（「复用为新版本」）的落地机制——源任务只读，新任务自带私有清单副本。

合同来源：`docs/审计收敛-AT21-AT22-双路裁决-20261112.md` §三（裁决 3）+ 卡 t_cc8ea89b。
本模块只做「建目录 + 复制该复制的 + 重写指向 + 受控初始化」，**不**做 HTTP、**不**做状态守卫、
**不**调审批/提交；那些分别归 `ui/hub_api.fork_task`（校验/锁）与 `state.init_fork_task`（出生态）。

一版一任务（D1）：fork 产出**新任务号**，源任务的 任务.json / 提示词.md / 成片.mp4 一个字节都不动。

继承 / 剥离（D2 + 卡面第 1/2/3/6 条）：

| 类别 | 落点 | 做法 |
|---|---|---|
| 拆解产物（十层/资产图/帧图/门禁） | `analysis/reverse/<bridge_id>/` | 写同一个 `bridge_id`，**零复制共享只读**（删除守卫 `_bridge_still_referenced` 按 bridge_id 天然覆盖） |
| 源视频 | 新任务 `inbox/` | 复制同 sha 的文件（uploads 删除守卫按「各任务 inbox 同 sha」扫描 → fork 天然被看见，删源后 fork 仍可读） |
| 源视频指纹 | 新任务目录 `.源视频指纹.json` | 逐字节同抄（冗余自述：删源任务后仍能说出这条片子是谁） |
| 待确认清单.md + 待确认清单.json | 新任务目录（**私有副本**） | 复制 + 把 `local:<源号>/` 重写为 `local:<新号>/`；此后 hub 链对 fork 的清单读写全走这份 |
| 资产图字节 | 新任务 `asset-uploads/` | 复制（内容寻址名不变，sha1 天然去重）；不复制则私有副本里的引用指不到盘 |
| 标量（意图/参数/三态/尺寸） | 任务.json | 直接抄值（见 `INHERIT_SCALARS`） |
| 审批 / 提交 / 生成痕迹 / 产物 / 运行时 | — | **不继承**（fork 出生即「零审批痕迹」，从 awaiting_assets 重新走所有者确认与批准） |

为什么用白名单抄值而不是「整份复制再删键」：整份复制会把将来新加的运行时字段悄悄带过去
（漏删一次就是一次付费/对账事故）；白名单漏掉一个字段最多是少抄一个参数，且探针会把
「该在的键」逐条断言回来。
"""
from __future__ import annotations

import shutil
from pathlib import Path

from . import config, ingest, replace, state

FORKABLE_STATES = ("done",)          # D6：只有出片（done）才有「复用为新版本」语义
VIDEO_EXTS = tuple(config.VIDEO_EXTS)

# 从源任务抄过来的标量（D2『标量抄值』）：意图、音频三态、生成参数、尺寸输入。
# 刻意**不含** gen_channel：渠道身份属于「本次批准」的绑定物（v2 快照的一部分），
# 抄过来会让「两版共用一个渠道配置」看起来像已批准过——批准时由前端重新选。
INHERIT_SCALARS = ("intent", "audio_mode", "audio_mode_依据", "gen_params", "size_input")


class ForkError(RuntimeError):
    """fork 失败——message 面向员工（说清缺什么、源任务怎么了）。"""


def _video_of(source_dir: Path, source: dict) -> Path:
    """定位源任务的源视频文件（inbox 里的那一条）。

    候选：① `ingestion.video_name`（= 桥侧落地名，摄取时就写进 inbox）；② inbox 里唯一的视频文件。
    定不到就抛——fork 出来的任务连源片都没有，等于把「可重新出片」这句承诺先弄虚了。
    """
    inbox = source_dir / "inbox"
    ing = source.get("ingestion") or {}
    name = str(ing.get("video_name") or "").strip()
    if name and (inbox / name).is_file():
        return inbox / name
    candidates = [p for p in inbox.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTS] \
        if inbox.is_dir() else []
    if len(candidates) == 1:
        return candidates[0]
    raise ForkError(
        f"源任务 {source.get('local_id')} 的 inbox 里定位不到源视频"
        f"（ingestion.video_name={name or '空'}，inbox 候选 {len(candidates)} 个）——"
        "没有源片就没法重新出片，先确认这条任务当初是走上传摄取的")


def _copy_private_list(shared_md: Path, target_md: Path, *, source_id: str, new_id: str) -> int:
    """共享清单 → 私有副本，并把 `local:<源号>/` 重写为 `local:<新号>/`。返回重写的引用数。

    逐字节解码再编码（不经 `Path.read_text` 的通用换行转换）：清单里的换行风格是历史产物，
    fork 只该改指向，不该顺手把整份文件的 CRLF 改成 LF（那会让「源清单字节不变」的对照失真）。
    """
    raw = shared_md.read_bytes()
    text = raw.decode("utf-8-sig")
    old_prefix = f"local:{source_id}/"
    new_prefix = f"local:{new_id}/"
    hits = text.count(old_prefix)
    target_md.write_bytes(text.replace(old_prefix, new_prefix).encode("utf-8"))
    return hits


def _copy_asset_uploads(source_dir: Path, target_dir: Path) -> int:
    """复制资产图字节（内容寻址文件名不变）。返回复制的文件数。"""
    src = source_dir / "asset-uploads"
    if not src.is_dir():
        return 0
    dst = target_dir / "asset-uploads"
    dst.mkdir(parents=True, exist_ok=True)
    count = 0
    for f in sorted(src.iterdir()):
        if f.is_file():
            shutil.copy2(f, dst / f.name)
            count += 1
    return count


def derive_task(source_dir: Path, *, note: str = "") -> dict:
    """从一条 **done** 任务派生新任务 → 返回 {local_id, derived_from, bridge_id, 复制明细...}。

    源目录全程只读（`state.load` + `read_bytes` + 复制），本函数不写源目录任何一个字节。
    """
    source_dir = Path(source_dir)
    source = state.load(source_dir)
    source_id = str(source.get("local_id") or source_dir.name)
    st = str(source.get("state") or "")
    if st not in FORKABLE_STATES:
        raise ForkError(f"只有已出片（done）的任务能复用为新版本，{source_id} 当前是 {st}")
    bridge = str(source.get("bridge_id") or "").strip()
    if not bridge:
        raise ForkError(f"源任务 {source_id} 没有 bridge_id（没拆解过），没有可复用的拆解产物")
    try:
        shared_md = replace.checklist_path(bridge)
    except replace.ReplaceError as exc:
        raise ForkError(f"源任务的待确认清单不在：{exc}") from None

    video = _video_of(source_dir, source)

    new_id = ingest._next_local_id()
    new_dir = config.PROJECTS_DIR / new_id
    if new_dir.exists():
        raise ForkError(f"目标任务目录已存在：{new_dir}（编号分配撞车，未做任何复制）")
    new_dir.mkdir(parents=True)
    inbox = new_dir / "inbox"
    inbox.mkdir()

    # ① 源视频：同 sha 复制进自己 inbox（删除守卫按 inbox 同 sha 判「还在被用」）
    (inbox / video.name).write_bytes(video.read_bytes())

    # ② 源视频指纹：同抄（源桥侧的指纹在 analysis/reverse/<bridge>/ 里，删源任务后仍在，
    #    这里再抄一份到任务目录只作自述冗余，不改任何读路径）
    fp_src = replace.analysis_root() / bridge / ".源视频指纹.json"
    fp_copied = False
    if fp_src.is_file():
        (new_dir / ".源视频指纹.json").write_bytes(fp_src.read_bytes())
        fp_copied = True

    # ③ 资产图字节 + ④ 私有清单副本（引用重写指自己）+ ⑤ 私有 sidecar
    uploaded = _copy_asset_uploads(source_dir, new_dir)
    rewritten = _copy_private_list(shared_md, new_dir / replace.LIST_NAME,
                                   source_id=source_id, new_id=new_id)
    shared_side = replace.sidecar_path(bridge)
    if shared_side.is_file():
        (new_dir / replace.SIDECAR_NAME).write_bytes(shared_side.read_bytes())

    task: dict = {
        "local_id": new_id,
        "bridge_id": bridge,
        "created_at": state._now(),
        "state": state.FORK_BOOTSTRAP_STATE,
        "history": [],
        # 标量抄值（D2）：意图/三态/参数/尺寸
        **{k: source[k] for k in INHERIT_SCALARS if k in source},
        "assets": list(source.get("assets") or []),
        # ingestion 抄值但**去掉 zip_sha256**：那是「同一条片子已摄取过」的幂等键，抄过来会让
        # 之后重新上传同一段视频时命中 fork 而不是原任务（编号与产物会串到派生版上）。
        "ingestion": {k: v for k, v in dict(source.get("ingestion") or {}).items()
                      if k != "zip_sha256"},
        "pending_replace": [],
        replace.PRIVATE_FIELD: True,
        "derived_from": {
            "local_id": source_id,
            "bridge_id": bridge,
            "at": state._now(),
            "changed": [],       # 派生瞬间还没改任何东西；改动维度由后续版本 UI 按实际编辑填
            "源任务状态": st,
        },
    }
    state.init_fork_task(new_dir, task,
                         note=note or f"派生自 {source_id}（复用拆解产物，未跑模型）")
    return {
        "local_id": new_id,
        "bridge_id": bridge,
        "derived_from": task["derived_from"],
        "video": video.name,
        "video_bytes": video.stat().st_size,
        "指纹同抄": fp_copied,
        "资产图复制": uploaded,
        "清单引用重写": rewritten,
        "私有清单": str(new_dir / replace.LIST_NAME),
        "私有侧车": str(new_dir / replace.SIDECAR_NAME),
    }
