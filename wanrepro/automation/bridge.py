# -*- coding: utf-8 -*-
"""bridge HTTP 客户端——automation 对 bridge 的唯一出口。

合同依据（wanrepro/ui/server.py 实读，2026-11-08）：
  · POST /api/video/upload：multipart 字段名 file（server.py:1666），但 multipart
    上限 64MB（server.py:1656）；原始字节通道（application/octet-stream +
    X-Filename percent-encoded）上限 MAX_UPLOAD=500MB——本客户端统一走原始字节。
  · 响应 {ok, id, name, size, elapsed}；上传成功即建任务目录+指纹。
  · GET /api/tasks → {tasks: [...]}（server.py:1352）。
  · bridge 默认端口：直跑 ui/server.py = 8776；vite autoStartBridge = 8779
    （web/vite.config.ts:45）。配置化在 config.WANREPRO_BRIDGE。
零第三方依赖：urllib 标准库。
"""
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from . import config


class BridgeError(RuntimeError):
    """bridge 调用失败——message 带 HTTP 状态与响应体片段，如实抛出。"""


def _request(method: str, path: str, *, body: bytes | None = None,
             headers: dict[str, str] | None = None, timeout: int = 120) -> Any:
    url = f"{config.BRIDGE_BASE}{path}"
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Accept", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:300]
        raise BridgeError(f"bridge {method} {path} → HTTP {e.code}: {detail}") from None
    except urllib.error.URLError as e:
        raise BridgeError(f"bridge {method} {path} 连不上（{config.BRIDGE_BASE}）: {e.reason}") from None
    if not raw:
        raise BridgeError(f"bridge {method} {path} → 空响应（按失败处理，不得当结果）")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise BridgeError(f"bridge {method} {path} → 响应不是 JSON: {raw[:200]}") from None


def get(path: str) -> Any:
    return _request("GET", path)


def post_json(path: str, payload: dict) -> Any:
    # 中文必须 UTF-8 落字节——git-bash curl 直传 -d 会把 GBK 字节当 JSON，实测被拒
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return _request("POST", path, body=body, headers={"Content-Type": "application/json"})


def upload_video(video: Path) -> dict:
    """POST /api/video/upload（原始字节 + X-Filename）→ {ok, id, name, size}。

    multipart 通道有 64MB 上限（server.py:1656），原始字节通道 500MB——统一走后者。
    """
    encoded = urllib.parse.quote(video.name, safe="")
    out = _request("POST", "/api/video/upload", body=video.read_bytes(), timeout=600,
                   headers={"Content-Type": "application/octet-stream",
                            "X-Filename": encoded})
    if not out.get("ok") or not out.get("id"):
        raise BridgeError(f"上传未回任务 id: {json.dumps(out, ensure_ascii=False)[:200]}")
    return out


def upload_asset(image: Path) -> dict:
    """POST /api/asset/upload（原始字节 + X-Filename）→ {url, bytes, source}。

    url 是公网 HTTPS 直链（uguu 优先，失败降级 local-tunnel——server.py:1607）；
    SDGF images 只收公网 HTTPS，调用方须校验 url 前缀。
    """
    encoded = urllib.parse.quote(image.name, safe="")
    return _request("POST", "/api/asset/upload", body=image.read_bytes(), timeout=120,
                    headers={"Content-Type": "application/octet-stream",
                             "X-Filename": encoded})


def tasks() -> list[dict]:
    """GET /api/tasks → 任务摘要列表。"""
    return list(config and get("/api/tasks").get("tasks") or [])


def task_exists(bridge_id: str) -> bool:
    return any(t.get("id") == bridge_id for t in tasks())
