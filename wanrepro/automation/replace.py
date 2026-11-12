# -*- coding: utf-8 -*-
"""AT-4 置换接收器——员工把替换图交给 Agent，代上传+回填清单+回显确认。

P1（置换场所）所有者挂起中：本模块是「输入面 A=交 Agent」的实现；
若日后拍板 B（画布同步），输入面换数据源即可，下游合同不变
（清单回填完成 + assets_confirmed → rendering）。

桥合同（server.py 实读 2026-11-08）：
  POST /api/asset/upload：原始字节 + X-Filename（png/jpg/webp ≤20MB）
    → {url: 公网HTTPS直链}（uguu 图床；SDGF images 只收公网 HTTPS）
  清单文件：analysis/reverse/<bridge_id>/待确认清单.md 的 Markdown 表格
    （| # | 类别 | 模型看到什么 | 命名 | 资产图 | … |，# 为行号）

用法：
  py -3 -m automation.replace auto-0001 --assets <图目录或文件...> [--map "1=a.png,2=b.png"]
    [--name "1=我们的产品"]        # 写回显，状态留 awaiting_assets
  py -3 -m automation.replace auto-0001 --confirm  # 确认后 → rendering
缺省映射：按「必须换」行优先、其余配「主体」行序，回显供确认。
"""
import argparse
import json
import os
import re
import sys
import threading
from pathlib import Path

from . import bridge, config, local_assets, state

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

IMG_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
LIST_NAME = "待确认清单.md"
SIDECAR_NAME = "待确认清单.json"          # 清单侧车（同实体确认 / 组元数据 / 行决策覆盖 / 出现时段）
# fork 私有清单副本标记（AT-21 B0 / 双路审计裁决 3）：任务.json 置 true = 该任务的清单与 sidecar 在
# **自己任务目录**里，共享的 analysis/reverse/<bridge_id>/ 那份从此与它无关（源任务永不被 fork 触碰）。
PRIVATE_FIELD = "checklist_private"


class ReplaceError(RuntimeError):
    pass


# ── 清单写锁（AT-22 B0 / 双路审计 M-3）──────────────────────────────────────
# 同一份 `analysis/reverse/<bridge_id>/待确认清单.md` 有两条写路径：
#   · hub 链  hub_api.set_asset → replace.set_row / set_group_write（读-改-整文件重写）；
#   · 画布链  server._update_checklist_cell ← set_checklist_image/desc/name
#     （`name_and_store` 每次上传自动预填描述，是高频写者）。
# 两条链跑在**同一个 server 进程**里（server.py 开头 import hub_api），从前各持一把互不相识的
# 锁（hub 链 task_lock(local_id)、画布链 server.WRITE_LOCK），并发时后写者把先写者的整份重写
# 覆盖掉 = 丢更新。锁放在本模块：清单的唯一读写原语都在这里，hub_api/server 都已经是它的下游，
# 谁都不用为拿锁多引一个模块。
# 用 RLock 而不是 Lock：set_group_write 在一把锁内逐行调 set_row（整组=一次原子写），
# 同一线程必须能重入。锁序只有 task_lock → CHECKLIST_LOCK 一个方向（画布链不反持 task_lock），
# 不会出现环。CLI 回填 run_replace 的「上传期间长窗口」不在此锁范围内（属 B3 单一写路径收口）。
CHECKLIST_LOCK = threading.RLock()


def analysis_root() -> Path:
    """拆解产物根：`WANREPRO_ANALYSIS_ROOT` 优先，否则 ROOT/analysis/reverse。

    与 `ui/hub_api._analysis_reverse()`、`automation/state._ANALYSIS_REVERSE` **同一真源**：
    清单是本模块写的、hub_api 读的同一份文件，根不一致就是「读 A 写 B」的伪隔离
    （probe_guard 记的那类事故），所以这条也认环境变量，探针/自托管才能把父子一起指向临时根。
    """
    env = (os.environ.get("WANREPRO_ANALYSIS_ROOT") or "").strip()
    return Path(env).expanduser() if env else Path(__file__).resolve().parent.parent / "analysis" / "reverse"


def task_dir_of(local_id: str) -> Path:
    """任务目录（`config.PROJECTS_DIR` 认 WANREPRO_AUTO_DIR，隔离探针同源）。"""
    return config.PROJECTS_DIR / str(local_id)


def private_scope(local_id: str, task: dict | None = None) -> str | None:
    """**清单作用域**：返回该任务的私有清单副本所在任务号（= local_id），共享任务 → None。

    AT-21 B0 / 裁决 3 的唯一判定点：fork 任务（任务.json `checklist_private: true`）的清单与 sidecar
    在自己任务目录里，其余任务继续读写 `analysis/reverse/<bridge_id>/` 那份共享文件。

    为什么按「任务声明 + 文件在场」双条件判，而不是只看文件在不在：
      · 只按文件在不在 → 私有副本一旦被误删，fork 会**静默回落到共享清单**，于是它后面的每一次改
        描述/换图都写进源任务的清单里——正是裁决 3 要根除的污染形态；
      · 所以声明了私有就必须有文件，缺文件直接抛（宁可报错，不许悄悄写源清单）。
    """
    lid = str(local_id or "").strip()
    if not lid:
        return None
    data = task
    if data is None:
        mf = task_dir_of(lid) / "任务.json"
        if not mf.is_file():
            return None                      # 没有任务文件的调用方（画布链/单测夹具）走共享口径
        try:
            data = json.loads(mf.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise ReplaceError(f"任务 {lid} 的任务文件读不了，无法判定清单作用域") from None
    if not isinstance(data, dict) or not data.get(PRIVATE_FIELD):
        return None
    private_md = task_dir_of(lid) / LIST_NAME
    if not private_md.is_file():
        raise ReplaceError(
            f"任务 {lid} 声明了私有清单副本（{PRIVATE_FIELD}），但 {private_md} 不在——"
            "不回落共享清单（那会把改动写进源任务）")
    return lid


def checklist_path(bridge_id: str, task_id: str | None = None) -> Path:
    """清单路径：给了 task_id（= `private_scope()` 的返回值）走**私有副本**，否则共享 bridge 清单。

    私有副本缺失时抛错不回落（见 `private_scope` 的说明）。
    """
    if task_id:
        private = task_dir_of(task_id) / LIST_NAME
        if not private.is_file():
            raise ReplaceError(f"任务 {task_id} 的私有清单副本不存在：{private}（不回落共享清单）")
        return private
    # 桥任务目录 analysis/reverse/<id>/（根可注入，见 analysis_root）
    p = analysis_root() / bridge_id / LIST_NAME
    if not p.is_file():
        raise ReplaceError(f"找不到清单 {p}（任务是否已拆解？）")
    return p


def row_cells(raw: str) -> list[str]:
    """表格行 → 单元格（含首格 #）。容全角竖线，但**不把正文里的 ｜ 当分隔符**。

    半角竖线优先；只有半角切不出 ≥5 格、行里又有全角竖线时才按全角切。
    反过来的话，正文本身带「｜」的行（pop-3--7 品牌位一行写了三处包装字）会被切成错位的假列，
    「建议」「资产图」全部串位——那是把人手填的列当成了模型写的正文。
    """
    body = raw.strip().strip("|｜").strip()
    if "|" in body:
        cells = [c.strip() for c in body.split("|")]
        if len(cells) >= 5:
            return cells
    if "｜" in body:
        return [c.strip() for c in body.strip("｜").split("｜")]
    return [body]


def parse_rows(md_text: str) -> list[dict]:
    """读层归一：行号/类别/描述/命名/现有资产图列/建议/我的描述。容全角竖线（红线：不假设格式）。

    AT-22 E3（§4.1 一处读层）：第 8 列「用我的描述」在这里解一次，`cards`（资产卡预填）与
    `hub_api.set_asset`（现值/幂等/乐观锁）共用——不另写第二份正则或解码器。解码走
    `render.decode_user_desc`（格式所有者一处：写侧 `render.encode_user_desc` 与它成对）。
    """
    rows = []
    for raw in md_text.splitlines():
        cells = row_cells(raw)
        if len(cells) < 5 or not re.fullmatch(r"\d+", cells[0] or ""):
            continue
        if cells[1] in ("类别", "---", "------"):
            continue
        rows.append({"no": int(cells[0]), "line": raw, "cat": cells[1],
                     "desc": cells[2],
                     "name": cells[3],
                     "cur_assets": cells[4],
                     "advice": cells[6] if len(cells) > 6 else "",
                     "my_desc": _decode_desc_cell(cells[7] if len(cells) > 7 else "")})
    return rows


def _decode_desc_cell(value: str) -> str:
    """「用我的描述」列解码：只认 render 的格式所有者（写侧 encode_user_desc 与它成对）。"""
    return render_module().decode_user_desc(str(value or ""))


def fill_row(md_text: str, no: int, asset_urls: list[str], name: str | None,
             overwrite: bool = False, desc: str | None = None) -> str:
    """按 # 写「资产图」列（与「命名」列 / 「用我的描述」列）。返回新全文。行格式以解析到的原样为准。

    缺省（overwrite=False）与历史行为一致：资产图列**追加**（多图逗号隔开）。
    overwrite=True = 置换台定向重传/「沿用」清空：整格替换（asset_urls 为空即清空该行绑定）。
    desc（AT-22 E3）：给了就整格替换第 8 列（空串 = 清回「按原物描述渲染」）；编码只走
    `render.encode_user_desc`（与读侧 decode 成对，格式所有者一处）。
    """
    out = []
    hit = False
    for raw in md_text.splitlines():
        cells = row_cells(raw)
        if cells and re.fullmatch(r"\d+", cells[0] or "") and int(cells[0]) == no:
            while len(cells) < 8:
                cells.append("")
            if desc is not None:
                cells[7] = render_module().encode_user_desc(str(desc))
            joined = ",".join(asset_urls)
            if overwrite:
                cells[4] = joined
            elif joined:
                cells[4] = joined if not cells[4] else f"{cells[4]},{joined}"
            if name:
                cells[3] = name
            out.append("| " + " | ".join(cells) + " |")
            hit = True
        else:
            out.append(raw)
    if not hit:
        raise ReplaceError(f"清单里没有第 {no} 行")
    if desc is not None:
        out = _ensure_desc_column(out)
    return "\n".join(out) + ("\n" if md_text.endswith("\n") else "")


DESC_HEADER = "用我的描述"


def _ensure_desc_column(lines: list[str]) -> list[str]:
    """老清单（AT-22 E3 之前 = 7 列）→ 补表头与分隔行的第 8 列，让新列在表格里真的出现。

    只认**表头行**（次格 == 「类别」）与其下一行分隔行；两行本来就有 8 格就一格不动（幂等）。
    写行已在 fill_row 里补到 8 格——表头不补的话 markdown 会渲染出「无名第 8 列」，用户看不到列名。
    """
    out = list(lines)
    for i, raw in enumerate(out):
        cells = row_cells(raw)
        if len(cells) >= 2 and cells[1] == "类别" and len(cells) < 8:
            while len(cells) < 8:
                cells.append("")
            cells[7] = DESC_HEADER
            out[i] = "| " + " | ".join(cells[:8]) + " |"
            if i + 1 < len(out):
                sep = row_cells(out[i + 1])
                if _is_sep_row(sep) and len(sep) < 8:
                    while len(sep) < 8:
                        sep.append("")
                    sep[7] = "------------"
                    out[i + 1] = "| " + " | ".join(sep[:8]) + " |"
            return out
    return out


def _is_sep_row(cells: list[str]) -> bool:
    """markdown 分隔行？全格是 `---`（可带对齐冒号）或空格子，且至少一格是 `---`。"""
    def is_sep(cell: str) -> bool:
        return bool(re.fullmatch(r":?-{2,}:?", (cell or "").strip()))

    return bool(cells) and all(is_sep(c) or not (c or "").strip() for c in cells) \
        and any(is_sep(c) for c in cells)


def set_row(bridge_id: str, no: int, *, image_urls: list[str] | None = None,
            name: str | None = None, overwrite: bool = False,
            desc: str | None = None, task_id: str | None = None) -> dict:
    """定向写清单一行（AT-8 置换台写路径）：资产图列覆盖/清空 + 命名列 +「用我的描述」列。

    写路径格式定死（数据合同 v3）：前端先走 uguu 拿 URL，再调这里写 md；不新增 multipart。
    AT-22 B0：读-改-整文件重写**全程持 CHECKLIST_LOCK**（与画布链 `_update_checklist_cell`
    同一把锁）——读在锁内才是关键：锁外读、锁内写照样丢更新。
    AT-21 B0：`task_id` 给了就写该任务的**私有清单副本**（fork），否则写共享 bridge 清单。
    """
    if image_urls is None and name is None and desc is None:
        raise ReplaceError("没有要写的东西：给 image_urls / name / desc 之一")
    path = checklist_path(bridge_id, task_id)
    with CHECKLIST_LOCK:
        before = path.read_text(encoding="utf-8")
        after = fill_row(before, no, list(image_urls or []), name, overwrite=overwrite,
                         desc=desc)
        if after != before:
            # 保持整文件 write_text（AT-22 B0 的并发合同：所有写者持 CHECKLIST_LOCK，读者也持锁后
            # 结构上读不到半截）。故意不用 tmp+os.replace：Windows 上无锁写者的 replace 会抛
            # PermissionError(13)（B0 反向对照实测），把「零丢失」的语义换成「抛异常」不是这里要的。
            path.write_text(after, encoding="utf-8")
    row = next((r for r in parse_rows(after) if r["no"] == no), None)
    if row is None:
        raise ReplaceError(f"清单里没有第 {no} 行")
    return row


# ---------------------------------------------------------------- 同实体确认组（AT-9 B2）

GROUPS_KEY = "同实体确认"          # sidecar 顶层键：[[1,11],...]（数据合同 B0）


def sidecar_path(bridge_id: str, task_id: str | None = None) -> Path:
    """清单侧车路径（同 `checklist_path` 的作用域规则：给了 task_id 走私有副本）。"""
    if task_id:
        return task_dir_of(task_id) / SIDECAR_NAME
    return analysis_root() / bridge_id / SIDECAR_NAME


def load_groups(bridge_id: str, task_id: str | None = None) -> list[list[int]]:
    """sidecar 顶层『同实体确认』→ [[1,11],...]；无 sidecar/无键/坏值 → []（合同：空=无组，行为同现状）。"""
    path = sidecar_path(bridge_id, task_id)
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return []
    raw = payload.get(GROUPS_KEY) if isinstance(payload, dict) else None
    out: list[list[int]] = []
    for group in raw or []:
        if isinstance(group, list) and len(group) >= 2 \
                and all(isinstance(x, int) and x >= 1 for x in group):
            out.append(sorted(set(group)))
    return out


def save_groups(bridge_id: str, groups: list[list[int]], task_id: str | None = None) -> list[list[int]]:
    """规范后整组写回 sidecar（保留其它键不动）：行去重升序、共员组合并、<2 的组剔除。"""
    merged: list[list[int]] = []
    for group in groups:
        cur = sorted({int(x) for x in group})
        touch = [g for g in merged if set(g) & set(cur)]
        for g in touch:
            merged.remove(g)
            cur = sorted(set(cur) | set(g))
        if len(cur) >= 2:
            merged.append(cur)
    merged.sort()
    path = sidecar_path(bridge_id, task_id)
    payload: dict = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(loaded, dict):
                payload = loaded
        except (OSError, json.JSONDecodeError):
            payload = {}                     # 坏 sidecar：保留键失败就只写组键（组是本函数的责权）
    payload[GROUPS_KEY] = merged
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return merged


def group_members(bridge_id: str, no: int, task_id: str | None = None) -> list[int]:
    """含 no 的确认组（升序）；无组 → []（调用方走单行路径）。"""
    for group in load_groups(bridge_id, task_id):
        if no in group:
            return list(group)
    return []


def group_key(members: list[int]) -> str:
    """组键（AT-11 §四）：成员升序连字，如 [1,3,5] → "1-3-5"（『组元数据』的键）。"""
    return "-".join(str(n) for n in sorted({int(m) for m in members}))


def row_unit(no: int, groups: list[list[int]]) -> tuple:
    """行 → 发号/去重单位（AT-11 B1 同一映射的公共口径）：
    组成员 → ("g", 组下标)（整组一个单位）；组外行 → ("s", 行号)（独立单位，同 URL 也不合并）。"""
    for gi, group in enumerate(groups):
        if no in group:
            return ("g", gi)
    return ("s", int(no))


def load_sidecar_payload(bridge_id: str, task_id: str | None = None) -> dict:
    """sidecar 顶层 payload（容错：无文件/坏 JSON/非对象 → {}）。组/决策/元数据读写共用。"""
    path = sidecar_path(bridge_id, task_id)
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _save_sidecar_key(bridge_id: str, key: str, value, task_id: str | None = None) -> None:
    """写 sidecar 单个顶层键，其它键原样保留（原子写，同 save_groups）。"""
    payload = load_sidecar_payload(bridge_id, task_id)
    payload[key] = value
    tmp = sidecar_path(bridge_id, task_id).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(sidecar_path(bridge_id, task_id))


# ---------------------------------------------------------------- 组元数据 + 行决策覆盖（AT-11 §四）

GROUP_META_KEY = "组元数据"          # {"1-3-5": {"主行": 1, "组名": "紫色管状产品"}}
DECISIONS_KEY = "行决策覆盖"        # {"1": "keep_origin" | "dismissed"}

# AT-12 N1 值域：keep_origin=「这行不换了（按原片）」（AT-11 旧值，历史数据继续读）、
# dismissed=「已移除（不需要替换，按原片处理）」——两者在**渲染/发号/锁点/入组互斥**上语义完全
# 相同（都是「按原片，不要替换」），前端折叠条把它们同归「已移除·按原片」，故后端统一用一个
# 「按原片行」口径，避免两套豁免逻辑走偏。
DECISION_VALUES = ("keep_origin", "dismissed")
ORIGIN_DECISIONS = frozenset(DECISION_VALUES)


def is_origin_decision(value: str | None) -> bool:
    """该决策值是否「按原片」语义（keep_origin / dismissed 皆算）。"""
    return value in ORIGIN_DECISIONS


def load_group_meta(bridge_id: str, task_id: str | None = None) -> dict[str, dict]:
    """『组元数据』→ {组键: {主行, 组名}}；无/坏 → {}（合同：空=无元数据，主行走缺省最小行）。"""
    raw = load_sidecar_payload(bridge_id, task_id).get(GROUP_META_KEY)
    out: dict[str, dict] = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            if isinstance(key, str) and isinstance(value, dict):
                out[key] = value
    return out


def save_group_meta(bridge_id: str, meta: dict[str, dict], task_id: str | None = None) -> dict[str, dict]:
    """整键规范写回『组元数据』（保留 sidecar 其它键）；组键=group_key(成员)。"""
    clean = {str(k): dict(v) for k, v in meta.items() if isinstance(v, dict)}
    _save_sidecar_key(bridge_id, GROUP_META_KEY, clean, task_id)
    return clean


def sync_group_meta(bridge_id: str, groups: list[list[int]],
                    task_id: str | None = None) -> dict[str, dict]:
    """组表变化后对账『组元数据』：组键不再对应任何现存组 → 该键删除（AT-11 §3.7 解组清理）。
    『行决策覆盖』不动（行级覆盖不因解组无故清——那是行自身的决策，不是组的）。"""
    live = {group_key(g) for g in groups}
    meta = load_group_meta(bridge_id, task_id)
    stale = [k for k in meta if k not in live]
    if stale:
        for key in stale:
            meta.pop(key)
        _save_sidecar_key(bridge_id, GROUP_META_KEY, meta, task_id)
    return meta


def load_row_decisions(bridge_id: str, task_id: str | None = None) -> dict[int, str]:
    """『行决策覆盖』→ {行号: "keep_origin" | "dismissed"}；无/坏/未知值 → 丢弃（只认白名单值）。"""
    raw = load_sidecar_payload(bridge_id, task_id).get(DECISIONS_KEY)
    out: dict[int, str] = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            if str(key).isdigit() and value in ORIGIN_DECISIONS:
                out[int(key)] = str(value)
    return out


def save_row_decisions(bridge_id: str, decisions: dict[int, str],
                       task_id: str | None = None) -> dict[int, str]:
    """整键规范写回『行决策覆盖』（保留 sidecar 其它键）；值白名单=keep_origin / dismissed。"""
    clean = {int(n): str(d) for n, d in decisions.items() if str(d) in ORIGIN_DECISIONS}
    _save_sidecar_key(bridge_id, DECISIONS_KEY, {str(n): d for n, d in sorted(clean.items())}, task_id)
    return clean


def set_group_write(task_dir: Path, no: int, *, image_url: str | None = None,
                    clear: bool = False, text: str | None = None,
                    desc: str | None = None) -> dict:
    """确认组写路径**单点**（AT-9 §六层3）：校验组存在 → 整组写清单 → 同步上传流水 → 写后回显组内全行。

    同实体疑似**不联动**（组里没有就抛错，调用方走单行路径）；任一行写失败如实抛
    ReplaceError（错误不报整组成功）；调用方持任务级串行锁。
    desc（AT-22 E3 §3.2 组联动）：组=同一个东西，整组写同一个描述——不静默只改一行，否则
    同一张图在正文里出现两个描述。
    AT-21 B0：组表与清单都按**任务作用域**读写（fork 任务的私有副本），源任务零触碰。
    """
    task = state.load(task_dir)
    scope = private_scope(str(task.get("local_id") or task_dir.name), task)
    bridge = str(task.get("bridge_id") or "")
    members = group_members(bridge, no, scope)
    if len(members) < 2:
        raise ReplaceError(f"第 {no} 行不在任何确认组里（疑似不联动，先在详情页成组）")
    rows: list[dict] = []
    # AT-22 B0：整组写在同一把清单锁内（RLock 可重入）——组是原子单位，不许画布链的清单写
    # 插进组内两行之间。锁只罩「读-改-写清单」，上传流水/任务文件在锁外照旧。
    with CHECKLIST_LOCK:
        for m in members:
            try:
                if image_url is not None:
                    rows.append(set_row(bridge, m, image_urls=[image_url], overwrite=True, task_id=scope))
                elif clear:
                    rows.append(set_row(bridge, m, image_urls=[], overwrite=True, task_id=scope))
                elif desc is not None:
                    rows.append(set_row(bridge, m, desc=desc, task_id=scope))
                else:
                    rows.append(set_row(bridge, m, name=str(text or ""), task_id=scope))
            except ReplaceError as e:
                raise ReplaceError(f"组 {members} 写清单在第 {m} 行失败（不报整组成功）: {e}") from None
    # 同步上传流水（语义同 _record_pending：清=撤流水；传图=落流水；命名=改已有流水）
    pending = [dict(x) for x in (task.get("pending_replace") or []) if isinstance(x, dict)]
    descs = {r["no"]: r.get("desc") or "" for r in rows}
    for m in members:
        if clear:
            pending = [x for x in pending if x.get("no") != m]
        elif image_url is not None:
            entry = next((x for x in pending if x.get("no") == m), None)
            if entry is None:
                entry = {"no": m, "img": "", "url": "", "name": "",
                         "desc": str(descs.get(m, ""))[:40]}
                pending.append(entry)
            entry["url"] = image_url
        elif text is not None:
            entry = next((x for x in pending if x.get("no") == m), None)
            if entry is not None:
                entry["name"] = text
    pending.sort(key=lambda x: x.get("no") or 0)
    state.save(task_dir, {**task, "pending_replace": pending})
    return {"members": members, "rows": rows}


def collect_images(src: str) -> list[Path]:
    p = Path(src)
    if p.is_file():
        imgs = [p] if p.suffix.lower() in IMG_EXTS else []
    elif p.is_dir():
        imgs = sorted(q for q in p.iterdir() if q.suffix.lower() in IMG_EXTS)
    else:
        raise ReplaceError(f"资产图路径不存在: {src}")
    if not imgs:
        raise ReplaceError(f"{src} 里没有 png/jpg/webp 图片")
    return imgs


def default_mapping(rows: list[dict], images: list[Path]) -> list[tuple[int, Path]]:
    """缺省配对：「必须换」行优先 → 其余按「主体」行序。"""
    must = [r["no"] for r in rows if "必须换" in r["line"]]
    subjects = [r["no"] for r in rows if r["cat"] == "主体"]
    order = must + [n for n in subjects if n not in must]
    if len(images) > len(order):
        raise ReplaceError(f"图 {len(images)} 张多于可配行 {len(order)}（主体+必须换）——用 --map 显式指定")
    return list(zip(order, images))


def parse_map(spec: str) -> list[tuple[int, str]]:
    out = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise ReplaceError(f"--map 项缺 '='：{part}（形态 1=图.png）")
        k, v = part.split("=", 1)
        if not k.strip().isdigit():
            raise ReplaceError(f"--map 行号必须是数字：{part}")
        out.append((int(k.strip()), v.strip()))
    if not out:
        raise ReplaceError("--map 是空的")
    return out


_RENDER_MODULE = None


def render_module():
    """按路径加载 pipeline/reverse/render.py（发号/有效映射同一口径）。

    与 selfcheck._load_render 同法：render.py 不是包，pipeline 也不该被 automation
    反向 import 成依赖——只在确认时懒加载一次。
    """
    global _RENDER_MODULE
    if _RENDER_MODULE is None:
        import importlib.util
        path = Path(__file__).resolve().parent.parent / "pipeline" / "reverse" / "render.py"
        sys.path.insert(0, str(path.parent))       # render 内部 `from aspect import …` 同目录依赖
        spec = importlib.util.spec_from_file_location("wanrepro_replace_render", path)
        if spec is None or spec.loader is None:
            raise ReplaceError(f"导不进 {path.name}（确认需要发号同源）")
        _RENDER_MODULE = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_RENDER_MODULE)
    return _RENDER_MODULE


def effective_entries(bridge_id: str, task_id: str | None = None) -> list[dict]:
    """确认时点提交清单（AT-16 B1b 同源装配）：清单当前有效绑定 → 行→图号→提交条目。

    真源=待确认清单.md「资产图」列，**不是** pending_replace 上传流水：流水只是本次会话的
    上传痕迹，用户「清空/覆盖」后流水与盘面会脱节；品牌行更曾整行只活在流水里——两套合同
    打架就是「提交快照 3 ≠ 发图单位 2」的根因。组内多行只出一条（组=一个发号单位）、
    组外同 URL 各自独立、按原片行先胜出、清空即出局，全部由 render.effective_units 判。

    条目形状沿用历史快照 {"no","img","url","name","desc"}：no 是**行号**（自检按行号查发号
    映射），列表序=提交序=图号序。
    AT-21 B0：`task_id` 给了就按该任务的私有清单副本 + 私有 sidecar 装配（fork 的资产/组/决策）。
    """
    render = render_module()
    path = checklist_path(bridge_id, task_id)
    ents = render.parse_checklist(path)
    units = render.effective_units(ents, load_groups(bridge_id, task_id),
                                   load_row_decisions(bridge_id, task_id))
    descs = {r["no"]: str(r.get("desc") or "")
             for r in parse_rows(path.read_text(encoding="utf-8-sig"))}
    out: list[dict] = []
    for unit in units:
        url = str(unit.get("url") or "")
        if not url:
            continue
        row_no = int(unit["row_no"])
        img = url.rsplit("/", 1)[-1] if local_assets.is_ref(url) else ""
        out.append({"no": row_no, "img": img, "url": url,
                    "name": unit["entity"].name or "",
                    "desc": descs.get(row_no, "")[:40]})
    return out


def run_replace(task_dir: Path, *, assets: list[str] | None, map_spec: str | None,
                name_spec: str | None, confirm: bool, verbose: bool = True,
                empty_confirm: bool = False, ack_rows: list[int] | None = None) -> str:
    task = state.load(task_dir)
    # AT-21 B0：一切清单/sidecar 读写都按本任务的作用域（fork → 私有副本，其余 → 共享 bridge 清单）。
    scope = private_scope(str(task.get("local_id") or task_dir.name), task)
    cur = task["state"]
    if confirm:
        if cur != "awaiting_assets":
            raise ReplaceError(f"确认置换需 awaiting_assets，当前 {cur}")
        pending = [dict(x) for x in (task.get("pending_replace") or [])
                   if isinstance(x, dict)]
        bridge_id = str(task.get("bridge_id") or "")
        # AT-11 §3.4（B2）：同一确认动作内先算有效决策——「按原片行」（keep_origin / AT-12 dismissed）
        # 在发号/组装有效上传清单之前胜出；原值（清单图列/命名/流水）不动，restore 后原样回来。
        ko_rows = {n for n, d in load_row_decisions(bridge_id, scope).items() if is_origin_decision(d)}
        # AT-16 B1b：提交清单、正文引用、资产清单、确认快照同一份有效映射（清单当前绑定）。
        # 组去重与按原片屏蔽都在 render.effective_units 里完成；上传流水 pending 只作回显。
        confirmed = effective_entries(bridge_id, scope)
        acks = sorted({int(x) for x in (ack_rows or [])})
        ko_snap = sorted(ko_rows)
        if confirmed:
            extra: dict = {"assets_confirmed": confirmed}
            # 『按原样』勾选记录（AT-9 B5 锁点对账的数据源）：未换必换行逐行确认的落账，
            # 自检③从这份确认时点快照核对（不自证）；空列表不落键（无记录=如实）。
            if acks:
                extra["按原样确认"] = {"rows": acks, "at": state._now()}
            # 『按原片』行决策快照（AT-11 B2）：与 assets_confirmed 同一确认时点落账，
            # 自检③只读这份快照（不读 live 覆盖表自证）；确认后覆盖表锁定（set_asset 校验 awaiting_assets）。
            if ko_snap:
                extra["按原片确认"] = {"rows": ko_snap, "at": state._now()}
            state.transition(task_dir, "rendering",
                             require={"assets_confirmed": confirmed},
                             note="所有者确认置换",
                             extra=extra)
        else:
            if not ko_snap and not empty_confirm:
                raise ReplaceError("没有待确认的置换（先跑 --assets 回填再 --confirm；"
                                   "全沿用=空置换，必须显式 empty_confirm=true）")
            # 有效图为空（无 pending，或 pending 全被 keep_origin 屏蔽）→ 走 empty_confirm
            # 既有合同落账；keep_origin 是显式行决策（等价确认），『按原片确认』同快照留痕。
            extra = {"assets_confirmed": [], "empty_confirm": True,
                     "全沿用": {"at": state._now()}}
            note = "所有者确认全沿用（空置换，empty_confirm）"
            if ko_snap:
                extra["按原片确认"] = {"rows": ko_snap, "at": state._now()}
                note = "所有者确认按原片（keep_origin 屏蔽全部有效图，empty_confirm 合同落账）"
            state.transition(task_dir, "rendering",
                             require={"empty_confirm": True},
                             note=note,
                             extra=extra)
        if verbose:
            how = f"提交 {len(confirmed)} 张图" if confirmed else "全沿用（空置换）"
            print(f"[OK] {task['local_id']}: 置换已确认 → rendering（{how}）")
        return "rendering"

    if cur != "awaiting_assets":
        raise ReplaceError(f"回填需 awaiting_assets（先完成拆解），当前 {cur}")

    images: list[Path] = []
    for a in assets or []:
        images.extend(collect_images(a))
    if not images and not map_spec:
        raise ReplaceError("没有输入：--assets <目录/文件> 或 --map 必须给一个")

    md_path = checklist_path(task["bridge_id"], scope)
    md = md_path.read_text(encoding="utf-8")
    rows = parse_rows(md)
    by_no = {r["no"]: r for r in rows}

    pairs: list[tuple[int, Path]] = []
    if map_spec:
        files = {p.name: p for p in images}
        for no, fname in parse_map(map_spec):
            if no not in by_no:
                raise ReplaceError(f"--map 行 #{no} 不在清单（行号见回显）")
            if fname not in files:
                raise ReplaceError(f"--map 引用的图不在输入里: {fname}")
            pairs.append((no, files[fname]))
    else:
        pairs = default_mapping(rows, images)

    names = dict(parse_map(name_spec)) if name_spec else {}

    filled = []
    for no, img in pairs:
        # AT-14 B3 A：带 task_id 上传 → 服务端落本地引用，清单/流水存引用串（提交时才装配）；
        # 旧服务端不认 X-Task-Id 时不回 local，回退 url（公网 HTTPS），两者都不合法才抛。
        # 隔离场景回退（probe_generate 夹具/CLI 离线）：bridge 不认识该任务（409）或不可达时，
        # 直接 local_assets.store 落任务目录本地引用——本地引用本就是 B3 目标形态，无需服务端。
        try:
            up = bridge.upload_asset(img, task_id=str(task.get("local_id") or ""))
        except Exception:
            up = {}
        ref = str(up.get("local") or "")
        if not local_assets.is_ref(ref):
            ref = local_assets.store_at(task_dir, str(task.get("local_id") or ""), img.read_bytes(), img.name)
        url = ref
        md = fill_row(md, no, [url], names.get(no))
        filled.append({"no": no, "img": img.name, "url": url,
                       "name": names.get(no, ""), "desc": by_no[no]["desc"][:40]})

    md_path.write_text(md, encoding="utf-8")
    state.save(task_dir, {**task, "pending_replace": filled})
    if verbose:
        print(f"[回显] {task['local_id']} 置换配对（确认无误后跑 --confirm）：")
        for f in filled:
            n = f" 命名「{f['name']}」" if f["name"] else ""
            print(f"  #{f['no']:>2} ← {f['img']}{n}  （原描述: {f['desc']}…）")
    return "awaiting_assets"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="AT-4 置换接收器")
    ap.add_argument("local_id")
    ap.add_argument("--assets", nargs="*", help="资产图目录或文件（可多个）")
    ap.add_argument("--map", help="显式配对：1=a.png,2=b.png")
    ap.add_argument("--name", help="命名回填：1=我们的产品")
    ap.add_argument("--confirm", action="store_true", help="确认置换 → rendering")
    ap.add_argument("--empty-confirm", action="store_true",
                    help="全沿用：空置换显式确认（AT-9 §四），与 --confirm 同用 → rendering")
    ap.add_argument("--bridge")
    args = ap.parse_args(argv)
    if args.bridge:
        config.BRIDGE_BASE = args.bridge.rstrip("/")
    config.ensure_dirs()
    tdir = config.PROJECTS_DIR / args.local_id
    if not (tdir / "任务.json").is_file():
        raise SystemExit(f"找不到任务 {args.local_id}")
    try:
        run_replace(tdir, assets=args.assets, map_spec=args.map,
                    name_spec=args.name, confirm=args.confirm,
                    empty_confirm=args.empty_confirm)
        return 0
    except (ReplaceError, bridge.BridgeError, state.StateError) as e:
        print(f"[FAIL] {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
