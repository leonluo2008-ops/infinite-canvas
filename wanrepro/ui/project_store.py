"""S3-B1：画布项目按用户隔离的服务端持久化（SQLite `user_projects` + 逐项目 CRUD 路由）。

合同（任务书 `S3-画布数据按用户隔离.md` §2.1，冻结版）
------------------------------------------------------
* **逐项目增量 + 墓碑**，不是整集 PUT：
    - `GET    /api/user/projects`        清单（id/name/updatedAt/deleted/deletedAt/bytes/version，
      **不含 content** —— 首屏 KB 级）
    - `GET    /api/user/projects/<id>`   单项目 content（打开项目时按需拉）
    - `PUT    /api/user/projects/<id>`   单项目 upsert（body = content + updatedAt [+ baseUpdatedAt + name]）
    - `DELETE /api/user/projects/<id>`   写**墓碑行**（deleted=1 + deleted_at），不物理删
* **归属校验全部单语句** `WHERE project_id=? AND user_id=?`：他人 id 与不存在走同一条 SQL、
  同一个 None 分支 → 404 响应**逐字节同形**（禁「先查存在再判归属」，那是时序侧信道）。
* **CAS 409 带服务端版本**：`baseUpdatedAt` 与服务端 `version` 不等 → 409 + 服务端版本视图；
  未带 baseUpdatedAt 时也不允许把 `updatedAt` 往回写（stale write 同样 409）——这是 V3
  「旧设备再 PUT 后仍不复活」的服务端半边。
* **超体积 413**：单项目 content 序列化后 8MB 上限，先判 Content-Length（不读体）、再判实际序列化长度。
  声明长度超限时**先把体 drain 掉再回 413**：Windows 上「带未读数据 close」会发 RST，客户端可能
  根本收不到 413（看到的是连接重置）。
* **时间戳域（任务书 §2.1 v1.2，统一 int epoch 毫秒）**：`updatedAt` / `baseUpdatedAt` / `deletedAt`
  以及响应里的 `updatedAt/deletedAt/version/maxUpdatedAt/serverTime` 全部是 **ms**；入参一律过
  `_ts()` 域校验（`1e11 ≤ ts ≤ 4e15`），**不合域 422，不做 ×1000 归一猜测**（秒级/ISO 字符串/
  浮点/2**63 全是 422，不许猜单位）。DELETE 的 `deletedAt` **必填**（跨设备墓碑排序靠客户端时钟），
  服务端不代填。
* **keep-alive 不错帧**：凡「请求体没读就走」的早退（401/405/非法 id 404/体读不完整 400）一律
  `close_connection=True` 并在响应里发 `Connection: close`——否则残留的请求体字节会被当成下一个
  请求的请求行，客户端收到 400 / 读到别人的响应。短 Content-Length 撒谎（声明 N 实发 <N）时
  截断体**绝不进写路径**，读满校验在 `_body()` 里先于任何落库。
* **单人零改动**：`MULTI_USER=0` 时 `handle()` 直接 False（既不建表、也不写任何 owner 标记），
  路由不存在 → 既有链路照旧 404。
* **零兜底**：不给服务端时间戳冒充客户端 updatedAt（LWW 判序必须是客户端的时钟），
  缺/非法 updatedAt → 422；冲突不静默覆盖，墓碑不被 stale 写入复活。

所有者判定辅助字段（供 B2 前端「跨账号确认弹窗」用，见 GET 清单响应）
-------------------------------------------------------------------
    owner      : str  —— 服务端认定的本清单所有者的 username（权威口径）
    ownerId    : int  —— 该 username 的 users.id（username 改名时用它对账）
    serverTime : int  —— 服务端当前 int epoch **毫秒**（`Date.now()` 口径；客户端时钟超前/落后判据）
    unit       : str  —— "ms"（清单自证时间戳单位，与 serverTime/updatedAt/version 同一域）
    maxUpdatedAt / maxVersion : int —— 本账号所有行的 updatedAt 汇总与最大版本（ms）
    count / liveCount / tombstoneCount : int

前端把首屏 GET 拿到的 `owner` 写进本地缓存载荷（任务书 §2.4 的 `owner` 字段）。下次启动若
「本地已有 owner 且 != 响应 owner」且本地项目数 > 0 → 弹「检测到本机 N 个项目来自其他账号，
导入到当前账号？」。服务端不参与该决策，只提供可判定字段。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler

try:  # 仓根包形态（server/探针导入）
    from wanrepro.ui import multiuser
except ImportError:  # 脚本直跑形态（ui/ 在 sys.path[0]）
    import multiuser  # type: ignore[no-redef]

# ------------------------------------------------------------------ 常量

ROUTE = "/api/user/projects"
_ITEM_RE = re.compile(r"^/api/user/projects/([^/]+)$")
LIST_METHODS = ("GET",)
ITEM_METHODS = ("GET", "PUT", "DELETE")

CONTENT_LIMIT = 8 * 1024 * 1024      # 单项目 content 序列化后 8MB
BODY_LIMIT = CONTENT_LIMIT + 64 * 1024   # 请求体上限 = content 上限 + 信封余量（先判长度不读体）
PROJECT_ID_MAX = 128
NAME_MAX = 200
# 时间戳域：统一 int epoch **毫秒**（任务书 §2.1 v1.2）。1e11 ≈ 1973 年的 ms、也高于任何 10 位
# 秒级时间戳 → 「秒级值」必然 <1e11 被拒；4e15 远小于 2**63-1（SQLite INTEGER 上界），越界值不会
# 走到 sqlite 层抛 OverflowError。**不做 ×1000 归一猜测**：猜单位就是替客户端决定 LWW 判序。
TS_MIN = 10 ** 11
TS_MAX = 4 * 10 ** 15
TS_UNIT = "ms"
# drain 上限：声明的体超过它就直接断（别为一个恶意 100MB 声明白读一堆字节）
DRAIN_MAX = 32 * 1024 * 1024
# 早退兜底的 drain 小口：让客户端能把本次响应读走（Windows 带未读数据 close → RST）
DRAIN_SMALL = 64 * 1024
# 客户端 id（nanoid 系）字符集；不在集合内一律按「没有这个项目」404（同一形状，不做 422 区分）
_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9_.:\-]{1,%d}$" % PROJECT_ID_MAX)

SCHEMA = """
CREATE TABLE IF NOT EXISTS user_projects(
  user_id INTEGER NOT NULL REFERENCES users(id),
  project_id TEXT NOT NULL,
  name TEXT,
  content_json TEXT,
  bytes INTEGER NOT NULL DEFAULT 0,
  deleted INTEGER NOT NULL DEFAULT 0,
  deleted_at INTEGER,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY(user_id, project_id)
);
CREATE INDEX IF NOT EXISTS idx_user_projects_user ON user_projects(user_id, updated_at);
"""

_SCHEMA_LOCK = threading.Lock()
_SCHEMA_DONE: set[str] = set()


class Reject(Exception):
    """带 HTTP 状态码的拒绝（400/404/409/413/422）。"""

    def __init__(self, status: int, msg: str, extra: dict | None = None) -> None:
        super().__init__(msg)
        self.status = status
        self.msg = msg
        self.extra = extra or {}


# ------------------------------------------------------------------ 存储

def connect() -> sqlite3.Connection:
    """沿用 multiuser / channel_store 的库与 PRAGMA 惯例（WAL + busy_timeout + foreign_keys）。

    与 channel_store.connect 的唯一区别：建表**每进程每库一次**（`_SCHEMA_DONE`），不再每个
    请求都 executescript——读路径要真的是只读（清单接口是全站最高频的读），DDL 只在首连做。
    隐式事务照样关掉（isolation_level=None）：写路径自己包 `BEGIN IMMEDIATE`。
    """
    path = multiuser.db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    key = os.path.abspath(str(path))
    if key not in _SCHEMA_DONE:
        with _SCHEMA_LOCK:
            if key not in _SCHEMA_DONE:
                conn.executescript(SCHEMA)
                conn.commit()
                _SCHEMA_DONE.add(key)
    conn.isolation_level = None
    return conn


# ------------------------------------------------------------------ 输出 / 入参

def _send(h: BaseHTTPRequestHandler, obj: dict, status: int = 200,
          headers: dict[str, str] | None = None) -> None:
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    h.send_response(status)
    h.send_header("Content-Type", "application/json; charset=utf-8")
    h.send_header("Content-Length", str(len(data)))
    h.send_header("Cache-Control", "no-store")
    if h.close_connection and not (headers or {}).get("Connection"):
        # 显式告诉客户端「这连接到此为止」：不发这个头，客户端会拿同一个 socket 发下一个请求，
        # 而我们已经在关它了 → 客户端看到的是连接重置而不是本次响应。
        h.send_header("Connection", "close")
    for k, v in (headers or {}).items():
        h.send_header(k, v)
    h.end_headers()
    if h.command != "HEAD":
        h.wfile.write(data)


def _declared_len(h: BaseHTTPRequestHandler) -> int:
    """声明的请求体长度（非法/缺失 → 0）。"""
    raw = (h.headers.get("Content-Length") or "").strip()
    try:
        return max(int(raw or 0), 0)
    except ValueError:
        return 0


def _drain(h: BaseHTTPRequestHandler, n: int, cap: int = DRAIN_MAX) -> None:
    """把 n 字节未读请求体读掉丢弃（最多 cap）。

    Windows 上「缓冲区里还有未读数据就 close」会发 RST，正在写响应的对端可能收不到响应体；
    所以先把它读走再关。读不动（对端已断）就放弃——反正要关连接。
    cap 的两档：413 走 DRAIN_MAX（要让整包走完才能把 413 送达）；早退走 DRAIN_SMALL
    （未登录请求也一样要读，别让 401 变成 32MB 的白工）。
    """
    left = min(n, cap)
    while left > 0:
        try:
            chunk = h.rfile.read(min(65536, left))
        except OSError:
            return
        if not chunk:
            return
        left -= len(chunk)


def _refuse(h: BaseHTTPRequestHandler, obj: dict, status: int,
            headers: dict[str, str] | None = None) -> None:
    """早退响应统一兜（keep-alive 不错帧）：请求体没读，残留字节会被当成下一个请求的请求行。

    401（未登录）/405（方法不允许）/404（非法 id，与不存在同形）都可能在「体没读」的状态下早退；
    尤其 PUT/DELETE 带体时，不关连接就会让下一个请求以 JSON 体开头 → 服务端 400 或串响应。
    """
    _drain(h, _declared_len(h), DRAIN_SMALL)
    h.close_connection = True
    _send(h, obj, status, headers)


def _body(h: BaseHTTPRequestHandler, empty_ok: bool = False) -> dict:
    """读请求体：先按 Content-Length 判 413（不读体），再判 JSON 合法性。

    任何「体没读满」的退出都置 `close_connection`：读不满 = 帧已废，不能在这个连接上继续解析。
    """
    te = (h.headers.get("Transfer-Encoding") or "").strip().lower()
    if te:
        h.close_connection = True     # 体没读，keep-alive 会错帧
        raise Reject(411, "项目快照需要 Content-Length（不支持分块请求体）。")
    raw_len = (h.headers.get("Content-Length") or "").strip()
    try:
        length = int(raw_len or 0)
    except ValueError:
        h.close_connection = True     # 不知道自己该读多少 → 这个连接的帧已经不可信
        raise Reject(400, "Content-Length 非法。") from None
    if length < 0:
        h.close_connection = True
        raise Reject(400, "Content-Length 非法。")
    if length > BODY_LIMIT:
        # 先把体读走（到 DRAIN_MAX 为止）再回 413：Windows 上带未读数据 close 会 RST，
        # 客户端会「收不到 413、只看到连接被重置」。读走之后 413 才真的能送达。
        _drain(h, length)
        h.close_connection = True
        raise Reject(413, f"单项目快照超过 {CONTENT_LIMIT // (1024 * 1024)}MB 上限"
                          f"（请求体 {length} 字节），服务端拒绝保存。")
    if not length:
        if empty_ok:
            return {}
        raise Reject(400, "请求体为空")
    try:
        raw = h.rfile.read(length)
    except OSError:
        h.close_connection = True
        raise Reject(400, "请求体读取失败") from None
    if len(raw) != length:
        # 短读：客户端声明的长度与实际不符（撒谎或半路断了）。帧已废 → 关连接，
        # 且**绝不进写路径**（截断的 JSON 在这里就死掉，下面压根不会被调用）。
        h.close_connection = True
        raise Reject(400, "请求体不完整。")
    try:
        val = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise Reject(400, "请求体不是合法 JSON") from None
    if not isinstance(val, dict):
        raise Reject(400, "请求体必须是 JSON 对象")
    return val


def _int_or_none(value) -> int | None:
    if isinstance(value, bool):        # bool 是 int 的子类，但 true/false 不是时间戳
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _ts(value) -> int | None:
    """入参时间戳 → int（域内），否则 None（调用方 422）。

    域 = `1e11 ≤ ts ≤ 4e15`（int epoch 毫秒）。**不做单位归一**：秒级 10 位值必然 <1e11 被拒、
    ISO 字符串解析不出整数被拒、2**63 这类越界值在进 SQL 之前死掉（否则 sqlite 抛 OverflowError）。
    """
    ts = _int_or_none(value)
    if ts is None or ts < TS_MIN or ts > TS_MAX:
        return None
    return ts


TS_DOMAIN_HINT = (f"（必须是 int epoch 毫秒：{int(TS_MIN)} ≤ 值 ≤ {int(TS_MAX)}，与 JS Date.now() 同域；"
                  f"服务端不做单位归一，秒级/ISO 字符串一律拒收）")



# ------------------------------------------------------------------ 行视图

def _version(row: sqlite3.Row) -> int:
    """单行版本 = max(updated_at, deleted_at)：墓碑靠 deleted_at 抬版本，写路径靠它做 CAS。"""
    return max(int(row["updated_at"] or 0), int(row["deleted_at"] or 0))


def _meta(row: sqlite3.Row) -> dict:
    """清单/响应元数据（**不含 content**）。deletedAt 对活行为 0（JS 侧直接比大小）。"""
    return {
        "id": row["project_id"],
        "name": row["name"] or "",
        "updatedAt": int(row["updated_at"] or 0),
        "deleted": bool(row["deleted"]),
        "deletedAt": int(row["deleted_at"] or 0),
        "bytes": int(row["bytes"] or 0),
        "version": _version(row),
    }


def _content_of(row: sqlite3.Row):
    """墓碑的 content 一律不下发（返回 None）；坏 JSON 当空（不让一行脏数据 500 整页）。"""
    if row["deleted"] or not row["content_json"]:
        return None
    try:
        return json.loads(row["content_json"])
    except (ValueError, TypeError):
        return None


# ------------------------------------------------------------------ 读

def _list_projects(uid: int, owner: str) -> dict:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM user_projects WHERE user_id=? ORDER BY updated_at DESC, project_id",
            (uid,)).fetchall()
    finally:
        conn.close()
    items = [_meta(row) for row in rows]
    live = sum(1 for i in items if not i["deleted"])
    return {
        "projects": items,
        "count": len(items),
        "liveCount": live,
        "tombstoneCount": len(items) - live,
        # ---- 所有者判定辅助字段（B2 跨账号弹窗用，见模块 docstring）----
        "owner": owner,
        "ownerId": uid,
        # Date.now() 口径（ms）：与 updatedAt/deletedAt/version 同一个域，前端不用猜单位
        "serverTime": int(time.time() * 1000),
        "unit": TS_UNIT,
        "maxUpdatedAt": max((i["updatedAt"] for i in items), default=0),
        "maxVersion": max((i["version"] for i in items), default=0),
    }


def _get_project(uid: int, pid: str) -> dict:
    """单项目 content。他人 id 与不存在同一条 SQL + 同一个 None 分支 → 404 逐字节同形。"""
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM user_projects WHERE project_id=? AND user_id=?",
                           (pid, uid)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise Reject(404, "没有这个项目")
    view = _meta(row)
    view["content"] = _content_of(row)
    return view


# ------------------------------------------------------------------ 写

def _conflict(row: sqlite3.Row) -> Reject:
    """409 + 服务端版本（不含 content：冲突是罕见路径，前端要内容就再 GET 一次，别让 409 变 8MB）。"""
    return Reject(409, "项目已被更新的改动覆盖（版本冲突），请刷新后重试。",
                  {"conflict": True, "server": _meta(row)})


def _put_project(h: BaseHTTPRequestHandler, uid: int, pid: str) -> dict:
    """单项目 upsert：CAS（baseUpdatedAt）+ 墓碑防复活 + LWW（updatedAt 不许回退）。

    冲突判据顺序（每一条都是 409 带服务端版本，绝不静默覆盖）：
      1. 带了 baseUpdatedAt 且 != 服务端 version  → 409（客户端基于旧版本写）；
      2. 行是墓碑：updatedAt <= deleted_at        → 409（stale 写入不得复活，V3）；
         只有 updatedAt > deleted_at 才是显式复活（前端 mergeCanvasData 的既有语义）；
      3. 活行：updatedAt < 服务端 updated_at      → 409（不许回写更旧的版本）。
    """
    body = _body(h)
    content = body.get("content")
    if not isinstance(content, (dict, list)):
        raise Reject(422, "content 必填，且必须是 JSON 对象/数组（单项目快照）")
    ts = _ts(body.get("updatedAt"))
    if ts is None:
        raise Reject(422, "updatedAt 缺失或不在毫秒域" + TS_DOMAIN_HINT)
    base_raw = body.get("baseUpdatedAt")
    base = None
    if base_raw is not None:
        base = _ts(base_raw)
        if base is None:
            raise Reject(422, "baseUpdatedAt 不在毫秒域" + TS_DOMAIN_HINT)

    # name：body.name 优先，其次 content.name；**两者都没有 → 保留旧行的 name**（不改名不等于清空名）
    name_raw = body.get("name")
    name: str | None = None
    if isinstance(name_raw, str) and name_raw.strip():
        name = name_raw.strip()[:NAME_MAX]
    else:
        inner = content.get("name") if isinstance(content, dict) else None
        if isinstance(inner, str) and inner.strip():
            name = inner.strip()[:NAME_MAX]

    try:
        blob = json.dumps(content, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (ValueError, TypeError):
        raise Reject(422, "content 不是可序列化的 JSON") from None
    if len(blob) > CONTENT_LIMIT:
        raise Reject(413, f"单项目快照 {len(blob)} 字节，超过 {CONTENT_LIMIT // (1024 * 1024)}MB 上限，"
                          f"服务端拒绝保存。")

    now = int(time.time() * 1000)      # 服务端落地时刻（ms，与客户端时间戳同域，只用于 created_at 审计）
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM user_projects WHERE project_id=? AND user_id=?",
                           (pid, uid)).fetchone()
        if row is not None:
            ver = _version(row)
            if base is not None and base != ver:
                raise _conflict(row)
            if row["deleted"]:
                if ts <= int(row["deleted_at"] or 0):
                    raise _conflict(row)          # 墓碑优先：stale 写入不复活
            elif ts < ver:
                raise _conflict(row)              # 不回写更旧的版本
            if name is None:                      # 事务内取旧 name：改名与否都不许把名字清空
                name = (row["name"] or "")[:NAME_MAX]
        # 无行（含「客户端以为有、服务端没有」的跨账号残留）→ 直接落盘，服务端没有可丢的数据
        conn.execute(
            "INSERT INTO user_projects(user_id,project_id,name,content_json,bytes,deleted,deleted_at,"
            "created_at,updated_at) VALUES(?,?,?,?,?,0,NULL,?,?)"
            " ON CONFLICT(user_id,project_id) DO UPDATE SET"
            " name=excluded.name, content_json=excluded.content_json, bytes=excluded.bytes,"
            " deleted=0, deleted_at=NULL, updated_at=excluded.updated_at",
            (uid, pid, name or "", blob.decode("utf-8"), len(blob), now, ts))
        # COMMIT 之后不再复读：同一事务里读到的就是提交后可见的那一行（提交后另开快照再 SELECT
        # 既多一次 IO，也给了「读到的不是自己刚写的那行」的口子）
        row = conn.execute("SELECT * FROM user_projects WHERE project_id=? AND user_id=?",
                           (pid, uid)).fetchone()
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()
    return _meta(row)


def _delete_project(h: BaseHTTPRequestHandler, uid: int, pid: str) -> dict:
    """写墓碑（deleted=1 + deleted_at），**不物理删**。

    非 oracle：无论别人有没有这个 id，都只按 (user_id, project_id) 动自己那行 → 一律 200，
    不存在「他人 id 404 / 自己 200」这种存在性探针。**没有行就什么都不写**（不建幽灵墓碑行：
    否则随便 DELETE 一个瞎猜的 id 都会在盘上留下痕迹，等于把 200 变成一个可观测的写入）。
    幂等：重复删除只抬 deleted_at。

    `deletedAt` **必填**（int epoch 毫秒）：墓碑的排序语义是「updatedAt > deletedAt 才复活」，
    跨设备一致性的时钟必须是客户端的；服务端代填（time.time()）会让服务端时钟参与 LWW 判序 ——
    零兜底禁止。缺失/非法/秒级一律 422。
    """
    body = _body(h, empty_ok=True)        # 空体不算传输错误，但下面会因为缺 deletedAt 而 422
    stamp = _ts(body.get("deletedAt"))
    if stamp is None:
        raise Reject(422, "deletedAt 缺失或不在毫秒域" + TS_DOMAIN_HINT)
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM user_projects WHERE project_id=? AND user_id=?",
                           (pid, uid)).fetchone()
        if row is None:
            # 没有这行 → 不插入任何东西，仍回 200 + 调用方给的墓碑视图（非 oracle、无幽灵行）
            conn.execute("COMMIT")
            return _meta({"project_id": pid, "name": "", "bytes": 0,
                          "deleted": 1, "deleted_at": stamp, "updated_at": 0})
        conn.execute(
            "UPDATE user_projects SET deleted=1,"
            " deleted_at=CASE WHEN ? > COALESCE(deleted_at,0) THEN ? ELSE deleted_at END"
            " WHERE project_id=? AND user_id=?",
            (stamp, stamp, pid, uid))
        # COMMIT 之后不再复读（事务内读到的就是提交后的那一行）
        row = conn.execute("SELECT * FROM user_projects WHERE project_id=? AND user_id=?",
                           (pid, uid)).fetchone()
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()
    return _meta(row)


# ------------------------------------------------------------------ 路由入口

def handle(h: BaseHTTPRequestHandler, method: str, path: str) -> bool:
    """server.py 的路由钩子：认领 `/api/user/projects[/<id>]`，否则 False。

    `MULTI_USER=0` 时整条路由不存在（False → 既有链路 404，且不建表、不写任何 owner 标记）。
    登录态是硬前置：未登录一律 401（不是 404/405）。
    """
    if not multiuser.enabled():
        return False
    if path == ROUTE or path == ROUTE + "/":
        _handle(h, method, "", is_list=True)
        return True
    m = _ITEM_RE.match(path)
    if not m:
        return False
    _handle(h, method, urllib.parse.unquote(m.group(1)), is_list=False)
    return True


def _handle(h: BaseHTTPRequestHandler, method: str, pid: str, is_list: bool) -> None:
    # 以下每条早退都走 _refuse：体没读就关连接（keep-alive 错帧的根因，见 _refuse docstring）
    username = multiuser._session_user(h)
    if username is None:
        _refuse(h, {"error": "未登录"}, 401)
        return
    # 登录态硬前置：未登录永远 401（不是 405），方法判定排在它后面
    if is_list:
        if method not in LIST_METHODS:
            _refuse(h, {"error": "方法不允许"}, 405, {"Allow": ", ".join(LIST_METHODS)})
            return
    else:
        if method not in ITEM_METHODS:
            _refuse(h, {"error": "方法不允许"}, 405, {"Allow": ", ".join(ITEM_METHODS)})
            return
        if not _PROJECT_ID_RE.match(pid or ""):
            _refuse(h, {"error": "没有这个项目"}, 404)   # 与「他人/不存在」同形，不做 422 区分
            return
    uid = multiuser.user_id_for_username(username)
    if uid is None:
        # 会话有效但用户行没了（账号被删）：按未登录处理，别把数据写到空 user_id 上
        _refuse(h, {"error": "未登录"}, 401)
        return
    try:
        if is_list:
            _send(h, _list_projects(uid, username))
        elif method == "GET":
            _send(h, {"project": _get_project(uid, pid)})
        elif method == "PUT":
            _send(h, {"ok": True, "project": _put_project(h, uid, pid)})
        else:
            _send(h, {"ok": True, "project": _delete_project(h, uid, pid)})
    except Reject as err:
        _send(h, {"error": err.msg, **err.extra}, err.status)
    except sqlite3.Error:
        # 存储异常文案不带 sqlite 原文（别给结构/存在性探针）
        _send(h, {"error": "项目存储失败，请稍后重试。"}, 500)
