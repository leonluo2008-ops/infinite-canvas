# -*- coding: utf-8 -*-
"""本地参考图资产——automation 任务目录下的内容寻址存储（AT-14 B3 A 阶段，唯一引用单点）。

定位（冻结设计 A1）：
  · 上传只落本地引用（local:任务号/asset-uploads/文件名），不再强行转公网图床；
  · 提交时才由 automation.generate._assemble_images 经 read() 取字节装配；
  · 路径/引用串的唯一出点在本模块——别处禁止手拼 local: 串或 asset-uploads 路径。

根目录 = automation.config.PROJECTS_DIR / <local_id> / "asset-uploads"
       = hub 工作台的任务目录（TASKS_DIR）下的 asset-uploads/，
         **不是** ui/server.py 的 bridge 资产根（analysis/reverse/<bridge_id>/assets），
         也不是 uploads/vc 图床备份——两套命名两套根，不混用。

文件名 = sha1(字节) 前 16 位十六进制 + 扩展名（魔数嗅探）：
  内容寻址 ⇒ 同字节同名 ⇒ 已存在直接复用不重写（幂等上传天然成立）。

引用串（单 token，无空格/逗号/中文）：
    local:<local_id>/asset-uploads/<16位hex>.<png|jpg|jpeg|webp>
只标准库，零第三方依赖。
"""
import hashlib
import os
import re
import secrets
from pathlib import Path

from . import config

__all__ = ["LocalAssetError", "is_ref", "parse_ref", "store", "read",
           "display_url", "local_id_ok", "asset_dir", "REF_RE", "NAME_RE"]

UPLOAD_DIR_NAME = "asset-uploads"          # 任务目录下的固定子目录名
REF_SCHEME = "local"

# local_id 与 hub 任务号同构（automation/state.py 的 auto-* 等）；64 位封顶防滥用
LOCAL_ID_RE = re.compile(r"^[0-9A-Za-z_-]{1,64}$")
# 文件名只认本模块自己落盘的形态：16 位小写 hex + 白名单扩展名
NAME_RE = re.compile(r"^[0-9a-f]{16}\.(png|jpg|jpeg|webp)$")
REF_RE = re.compile(
    rf"^{REF_SCHEME}:([0-9A-Za-z_-]{{1,64}})/{UPLOAD_DIR_NAME}/([0-9a-f]{{16}}\.(?:png|jpg|jpeg|webp))$")

MAX_BYTES = 20 * 1024 * 1024

# 魔数嗅探表（按序匹配；嗅探不出再看原始文件名后缀）
_MAGIC = (
    (b"\x89PNG", "png"),
    (b"\xff\xd8\xff", "jpg"),
)
WEBP_MAGIC = b"WEBP"                       # RIFF....WEBP：偏移 8
NAME_EXT = (".png", ".jpg", ".jpeg", ".webp")


class LocalAssetError(RuntimeError):
    """本地资产存取失败（字节不认/越界/引用串不合法/文件缺失）——人话消息，调用方直透。"""


def local_id_ok(local_id: str) -> bool:
    """任务号是否可作目录名（^[0-9A-Za-z_-]{1,64}$）——只验形态，不验存在。"""
    return bool(LOCAL_ID_RE.match(str(local_id or "")))


def asset_dir(local_id: str) -> Path:
    """该任务的资产目录（不建盘；建盘只在 store 里做）。"""
    if not local_id_ok(local_id):
        raise LocalAssetError(f"任务号不合法（只收字母数字下划线连字符，≤64 位）：{local_id!r}")
    return Path(config.PROJECTS_DIR) / local_id / UPLOAD_DIR_NAME


def _sniff_ext(data: bytes, raw_name: str) -> str:
    """字节魔数 → 扩展名（不带点）；认不出再看 raw_name 后缀；仍认不出抛人话。"""
    if data[:8][:4] == _MAGIC[0][0]:
        return _MAGIC[0][1]
    if data[:3] == _MAGIC[1][0]:
        return _MAGIC[1][1]
    if data[:4] == b"RIFF" and data[8:12] == WEBP_MAGIC:
        return "webp"
    suffix = Path(str(raw_name or "")).suffix.lower()
    if suffix in NAME_EXT:
        return suffix.lstrip(".")
    raise LocalAssetError("参考图只收 png/jpg/webp")


def is_ref(s: str) -> bool:
    """s 是否是本模块发出的本地引用串（形态校验，不碰盘）。"""
    return bool(REF_RE.match(str(s or "").strip()))


def parse_ref(ref: str) -> tuple[str, str]:
    """引用串 → (local_id, 文件名)。不合法抛 LocalAssetError。"""
    m = REF_RE.match(str(ref or "").strip())
    if not m:
        raise LocalAssetError(f"不是合法的本地引用串（应形如 {REF_SCHEME}:任务号/{UPLOAD_DIR_NAME}/文件名）：{str(ref or '')[:80]!r}")
    return m.group(1), m.group(2)


def display_url(ref: str) -> str:
    """引用串 → 服务端取图 URL（GET /api/hub/asset-file/<local_id>/<文件名>）。"""
    local_id, name = parse_ref(ref)
    return f"/api/hub/asset-file/{local_id}/{name}"


def store(local_id: str, data: bytes, raw_name: str) -> str:
    """字节 → 原子落盘 → 引用串。同内容已存在直接复用（内容寻址幂等）。"""
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise LocalAssetError("参考图是空的（0 字节）——请重传")
    if len(data) > MAX_BYTES:
        raise LocalAssetError(f"参考图太大（{len(data) / 1048576:.1f} MB），上限 20 MB")
    return store_at(asset_dir(local_id).parent, local_id, data, raw_name)


def store_at(task_dir: Path, local_id: str, data: bytes, raw_name: str) -> str:
    """同 store，但字节落在调用方给的任务目录（隔离夹具的任务根不在全局 PROJECTS_DIR 时用，
    防止探针/CLI 离线场景往真实 projects 写入）。引用串格式与 store 完全一致。"""
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise LocalAssetError("参考图是空的（0 字节）——请重传")
    if len(data) > MAX_BYTES:
        raise LocalAssetError(f"参考图太大（{len(data) / 1048576:.1f} MB），上限 20 MB")
    ext = _sniff_ext(bytes(data), raw_name)
    name = hashlib.sha1(bytes(data)).hexdigest()[:16] + "." + ext
    d = Path(task_dir) / UPLOAD_DIR_NAME
    dest = d / name
    if not dest.is_file():                    # 同名=同内容（sha1），在就复用不重写
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f".tmp-{secrets.token_hex(8)}"
        try:
            tmp.write_bytes(bytes(data))
            os.replace(tmp, dest)             # 同目录原子改名：断电不留半截文件
        finally:
            tmp.unlink(missing_ok=True)       # replace 成功后 unlink 是 no-op
    return f"{REF_SCHEME}:{local_id}/{UPLOAD_DIR_NAME}/{name}"


def read(ref: str) -> bytes:
    """引用串 → 字节。越界/缺失一律 LocalAssetError（调用方按 404/前置拒绝处理）。"""
    local_id, name = parse_ref(ref)
    if not NAME_RE.match(name):
        raise LocalAssetError(f"本地资产文件名不合法：{name!r}")
    root = asset_dir(local_id).resolve()
    target = (root / name).resolve()
    # 防穿越：解析后必须仍在本任务 asset-uploads **直下**（引用串已限一层，这里防符号链接绕行）
    if target.parent != root or not target.is_file():
        raise LocalAssetError(f"本地资产缺失：{ref}")
    try:
        return target.read_bytes()
    except OSError as e:
        raise LocalAssetError(f"本地资产读不出来（{e}）：{ref}") from None
