"""mitmproxy addon: 拦截 RevenueCat POST /v1/receipts, 提取 fetch_token 并入队,
阻断真实请求避免被支付账号消费 (保留 72h 内可转移).

token 存储双写:
    1. tokens.jsonl            (文件队列, CLI 工具兼容)
    2. SQLite gptplus.db       (capturedtoken 表, WebUI 实时读取)

用法:
    mitmdump -s src/addon.py -p 8888 --mode upstream:http://127.0.0.1:7890 --no-http2

环境变量:
    TOKEN_QUEUE_FILE   token 队列文件路径 (默认 ./tokens.jsonl)
    TOKEN_EXPIRY_HOURS token 过期小时 (默认 72)
    GPTPLUS_DB         SQLite 数据库路径 (默认 ./gptplus.db)
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import types
from datetime import datetime, timedelta, timezone

# mitmproxy 脚本加载器在 sys.modules 未注册本模块时会出问题, 这里做防御性注册
if sys.modules.get(__name__) is None:
    sys.modules[__name__] = types.ModuleType(__name__)

_QUEUE_FILE = os.environ.get("TOKEN_QUEUE_FILE", "tokens.jsonl")
_EXPIRY_HOURS = int(os.environ.get("TOKEN_EXPIRY_HOURS", "72"))
_DB_PATH = os.environ.get("GPTPLUS_DB", "gptplus.db")
_TZ = timezone(timedelta(hours=8))


def _ensure_db_table(conn: sqlite3.Connection) -> None:
    """幂等建表, 兼容 WebUI 未启动时 addon 先写 DB 的场景."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS capturedtoken (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fetch_token TEXT,
            captured_at TEXT,
            expires_at TEXT,
            original_app_user_id TEXT,
            storefront TEXT DEFAULT 'US',
            used INTEGER DEFAULT 0,
            used_by_account_id TEXT,
            used_at TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS ix_capturedtoken_fetch_token ON capturedtoken (fetch_token)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_capturedtoken_used ON capturedtoken (used)")
    conn.commit()


def _append_token_to_db(fetch_token: str, app_user_id: str, storefront: str,
                        captured_at: str, expires_at: str) -> bool:
    """把 token 写入 SQLite capturedtoken 表 (WebUI 实时读取). 幂等: 同一 token 不重复插入."""
    try:
        conn = sqlite3.connect(_DB_PATH, timeout=5)
        try:
            _ensure_db_table(conn)
            existing = conn.execute(
                "SELECT id FROM capturedtoken WHERE fetch_token = ?", (fetch_token,)
            ).fetchone()
            if existing:
                print(f"[addon] token 已在 DB (id={existing[0]}), 跳过插入")
                return True
            conn.execute(
                """
                INSERT INTO capturedtoken
                    (fetch_token, captured_at, expires_at, original_app_user_id, storefront, used)
                VALUES (?, ?, ?, ?, ?, 0)
                """,
                (fetch_token, captured_at, expires_at, app_user_id, storefront),
            )
            conn.commit()
            print(f"[addon] token 已写入 SQLite capturedtoken 表 (db={_DB_PATH})")
            return True
        finally:
            conn.close()
    except Exception as e:
        print(f"[addon] DB 写入失败 (db={_DB_PATH}): {e}")
        return False


def _append_token(fetch_token: str, app_user_id: str, storefront: str) -> int:
    now = datetime.now(_TZ)
    expires = now + timedelta(hours=_EXPIRY_HOURS)
    captured_at = now.isoformat()
    expires_at = expires.isoformat()
    record = {
        "fetch_token": fetch_token,
        "captured_at": captured_at,
        "expires_at": expires_at,
        "used": False,
        "used_by": None,
        "used_at": None,
        "original_app_user_id": app_user_id,
        "storefront": storefront,
    }
    # 1. 文件队列 (CLI 工具兼容), 幂等: 已存在则不重复
    os.makedirs(os.path.dirname(_QUEUE_FILE) or ".", exist_ok=True)
    items = []
    already_in_file = False
    if os.path.exists(_QUEUE_FILE):
        with open(_QUEUE_FILE, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                items.append(line)
                try:
                    if json.loads(line).get("fetch_token") == fetch_token:
                        already_in_file = True
                except Exception:
                    pass
    if not already_in_file:
        items.append(json.dumps(record, ensure_ascii=False) + "\n")
        with open(_QUEUE_FILE, "w", encoding="utf-8") as f:
            f.writelines(items)
    # 2. SQLite capturedtoken 表 (WebUI 实时读取)
    _append_token_to_db(fetch_token, str(app_user_id), str(storefront), captured_at, expires_at)
    return len(items) - 1


class TokenCaptureAddon:
    """拦截 RevenueCat /v1/receipts, 保存 fetch_token, 返回假 200 阻断真实提交."""

    def request(self, flow):  # type: ignore[no-untyped-def]
        try:
            host = flow.request.pretty_host
            path = flow.request.path
            method = flow.request.method
        except Exception:
            return
        if method != "POST":
            return
        if "revenuecat.com" not in str(host) or not str(path).startswith("/v1/receipts"):
            return
        try:
            body = json.loads(flow.request.get_text() or "{}")
        except Exception:
            print("[addon] cannot parse request body")
            return
        fetch_token = body.get("fetch_token")
        app_user_id = body.get("app_user_id", "unknown")
        if not fetch_token:
            print("[addon] no fetch_token in body")
            return
        storefront = flow.request.headers.get("X-Storefront", "US")
        idx = _append_token(str(fetch_token), str(app_user_id), str(storefront))
        print(f"[addon] INTERCEPTED token #{idx} owner={app_user_id} token={str(fetch_token)[:24]}...", flush=True)
        # 阻断: 返回假 200
        fake = {
            "subscriber": {
                "original_app_user_id": str(app_user_id),
                "first_seen": datetime.now(_TZ).isoformat(),
                "last_seen": datetime.now(_TZ).isoformat(),
                "entitlements": {},
                "subscriptions": {},
                "non_subscriptions": {},
            }
        }
        try:
            from mitmproxy import http as _mitm_http
            flow.response = _mitm_http.Response.make(
                200,
                json.dumps(fake, ensure_ascii=False).encode("utf-8"),
                {"Content-Type": "application/json"},
            )
            print("[addon] BLOCKED real request, returned fake 200", flush=True)
        except Exception as e:
            print(f"[addon] make_response failed: {e}", flush=True)


addons = [TokenCaptureAddon()]
