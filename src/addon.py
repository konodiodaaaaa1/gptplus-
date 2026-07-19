"""mitmproxy addon: 拦截 RevenueCat POST /v1/receipts, 提取 fetch_token 并入队,
阻断真实请求避免被支付账号消费 (保留 72h 内可转移).

用法:
    mitmdump -s src/addon.py -p 8888 --mode upstream:http://127.0.0.1:7890 --no-http2

环境变量:
    TOKEN_QUEUE_FILE  token 队列文件路径 (默认 ./tokens.jsonl)
    TOKEN_EXPIRY_HOURS  token 过期小时 (默认 72)
"""
from __future__ import annotations

import json
import os
import sys
import types
from datetime import datetime, timedelta, timezone

# mitmproxy 脚本加载器在 sys.modules 未注册本模块时会出问题, 这里做防御性注册
if sys.modules.get(__name__) is None:
    sys.modules[__name__] = types.ModuleType(__name__)

_QUEUE_FILE = os.environ.get("TOKEN_QUEUE_FILE", "tokens.jsonl")
_EXPIRY_HOURS = int(os.environ.get("TOKEN_EXPIRY_HOURS", "72"))
_TZ = timezone(timedelta(hours=8))


def _append_token(fetch_token: str, app_user_id: str, storefront: str) -> int:
    now = datetime.now(_TZ)
    expires = now + timedelta(hours=_EXPIRY_HOURS)
    record = {
        "fetch_token": fetch_token,
        "captured_at": now.isoformat(),
        "expires_at": expires.isoformat(),
        "used": False,
        "used_by": None,
        "used_at": None,
        "original_app_user_id": app_user_id,
        "storefront": storefront,
    }
    os.makedirs(os.path.dirname(_QUEUE_FILE) or ".", exist_ok=True)
    items = []
    if os.path.exists(_QUEUE_FILE):
        with open(_QUEUE_FILE, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    items.append(line)
    items.append(json.dumps(record, ensure_ascii=False) + "\n")
    with open(_QUEUE_FILE, "w", encoding="utf-8") as f:
        f.writelines(items)
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
        print(f"[addon] INTERCEPTED token #{idx} owner={app_user_id} token={str(fetch_token)[:24]}...")
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
            flow.response = flow.request.make_response(
                200,
                json.dumps(fake, ensure_ascii=False).encode("utf-8"),
                {"Content-Type": "application/json"},
            )
            print("[addon] BLOCKED real request, saved token for transfer")
        except Exception as e:
            print(f"[addon] make_response failed: {e}")


addons = [TokenCaptureAddon()]
