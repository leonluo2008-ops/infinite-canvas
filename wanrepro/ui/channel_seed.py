"""
S4 种子化：用户首次访问渠道时名下渠道为 0 → 从 config/channel-config.json 种入预置三渠道（无 key）。
设计（owner 2026-11-18 拍板「渠道=服务器端用户级隔离，前端不存」）：
- 种子只播一次：用户删掉预置渠道后 PUT 全量替换即为其真实意图，绝不回灌。
- GET 懒播种：_list_channels 前查空，空则种；单事务 BEGIN IMMEDIATE 防并发双种。
- 预置渠道 apiKey 空 → api_key_enc 存 b''（与 _view 的 keyBroken 分支兼容，前端显示「未填 Key」）。
- id 用模板文件自带 id；若与全局主键冲突（他用户 mint 过同 id 的概率极低，模板 id 是 nanoid）→ 409 原样上抛语义不可接受，
  故种子 id 强制加 uid 前缀（seed-<uid>-<templateId>）保证全局唯一。
"""
import json
import sqlite3
import sys
import time
from pathlib import Path

try:  # 仓根包形态（探针/测试导入）
    from wanrepro.ui import multiuser
except ImportError:  # 脚本直跑形态（ui/ 在 sys.path[0]）
    import multiuser  # type: ignore[no-redef]


def _connect() -> sqlite3.Connection:
    """沿用 channel_store.connect 的 PRAGMA 惯例（WAL + busy_timeout + isolation_level=None）。"""
    path = multiuser.db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    try:  # 复用 channel_store 的建表脚本（同一套 SCHEMA；双形态导入与 multiuser 同口径）
        from wanrepro.ui import channel_store
    except ImportError:
        import channel_store  # type: ignore[no-redef]
    conn.executescript(channel_store.SCHEMA)
    conn.commit()
    conn.isolation_level = None
    return conn

PRESET_PATH = Path(__file__).resolve().parent.parent / "config" / "channel-config.json"


def preset_names() -> set[str]:
    """预置渠道名称集合（channel_store._view 下发 isPreset 的唯一依据）。"""
    items = _load_preset()
    if not items:
        print("[channel_seed] 预置模板不可读或为空，isPreset 判定降级为全 false", file=sys.stderr)
        return set()
    return {str(c["name"]).strip() for c in items if str(c.get("name") or "").strip()}


def _load_preset() -> list[dict]:
    try:
        body = json.loads(PRESET_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []
    items = body.get("channels") if isinstance(body, dict) else None
    if not isinstance(items, list):
        return []
    return [c for c in items if isinstance(c, dict) and str(c.get("id") or "").strip() and str(c.get("name") or "").strip()]


def seed_if_empty(uid: int) -> None:
    """名下渠道为 0 → 种入预置（无 key）。失败静默（下次 GET 再试），绝不阻塞渠道读取。"""
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        n = conn.execute("SELECT COUNT(*) FROM user_channels WHERE user_id=?", (uid,)).fetchone()[0]
        if n:
            conn.execute("COMMIT")
            return
        now = int(time.time())
        rows = []
        for item in _load_preset():
            cid = f"seed-{uid}-{str(item['id']).strip()}"[:128]
            try:
                conn.execute(
                    "INSERT INTO user_channels(id,user_id,name,base_url,api_key_enc,api_format,models_json,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (cid, uid, str(item["name"]).strip(), str(item.get("baseUrl") or "").strip(),
                     b"", str(item.get("apiFormat") or "openai").strip(),
                     json.dumps(item.get("models") or [], ensure_ascii=False), now))
                rows.append(cid)
            except sqlite3.IntegrityError:
                conn.execute("ROLLBACK")
                return  # 并发播种撞行：另一个请求已种好，直接放弃
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
    finally:
        conn.close()
