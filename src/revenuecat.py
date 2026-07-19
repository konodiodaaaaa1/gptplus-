"""RevenueCat / OpenAI accounts API 交互层."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .config import Config


def _post_json(url: str, payload: dict, headers: dict, timeout: int = 15) -> tuple[int, dict]:
    req = urllib.request.Request(
        url, method="POST",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **headers},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {"_raw": str(e)}


def _get_json(url: str, headers: dict, timeout: int = 15) -> tuple[int, dict]:
    req = urllib.request.Request(url, method="GET", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {"_raw": str(e)}


def get_account_id(jwt: str, cfg: Config) -> str | None:
    headers = {"Authorization": f"Bearer {jwt}", "Accept": "application/json"}
    status, body = _get_json(cfg.openai_account_check_url, headers)
    if status != 200:
        print(f"[get_account_id] HTTP {status}: {body}")
        return None
    aid = body.get("account_id") or body.get("account", {}).get("account_id") if isinstance(body.get("account"), dict) else None
    if not aid:
        # 兜底: 有时 account_id 在外层
        aid = body.get("account_id")
    if not aid:
        print(f"[get_account_id] 响应无 account_id: {json.dumps(body, ensure_ascii=False)[:300]}")
        return None
    return str(aid)


def assemble_revenuecat_headers(cfg: Config, storefront: str = "US") -> dict[str, str]:
    h = dict(cfg.headers_template)
    h["X-Storefront"] = storefront
    h["Authorization"] = f"Bearer {cfg.revenuecat_api_key}"
    return h


def assemble_revenuecat_body(fetch_token: str, app_user_id: str, is_restore: bool = False) -> dict[str, Any]:
    """token 拼接: fetch_token + app_user_id 组装成完整 receipt 提交体."""
    return {
        "fetch_token": fetch_token,
        "product_ids": [cfg_product_id()],
        "platform_product_ids": [{"product_id": cfg_product_id()}],
        "app_user_id": app_user_id,
        "is_restore": is_restore,
        "observer_mode": False,
        "purchase_completed_by": "revenuecat",
        "initiation_source": "unsynced_active_purchases",
        "sdk_originated": False,
        "payload_version": 1,
    }


_cfg_singleton = None


def cfg_product_id() -> str:
    # 避免 revenuecat 模块对 Config 全局依赖, 使用 caller 传入 cfg 的 product_id
    # 在本项目中 assemble_revenuecat_body 由 activate_plus 调用, 内部已用 cfg
    # 此函数仅为兼容旧调用保留, 默认返回标准 product_id
    return "oai.chatgpt.plus"


def submit_to_revenuecat(fetch_token: str, app_user_id: str, cfg: Config,
                         storefront: str = "US", is_restore: bool = False) -> tuple[int, dict]:
    headers = assemble_revenuecat_headers(cfg, storefront)
    # 组装 body 时使用 cfg.product_id
    body = {
        "fetch_token": fetch_token,
        "product_ids": [cfg.product_id],
        "platform_product_ids": [{"product_id": cfg.product_id}],
        "app_user_id": app_user_id,
        "is_restore": is_restore,
        "observer_mode": False,
        "purchase_completed_by": "revenuecat",
        "initiation_source": "unsynced_active_purchases",
        "sdk_originated": False,
        "payload_version": 1,
    }
    print(f"[submit_to_revenuecat] POST {cfg.revenuecat_url}")
    print(f"  app_user_id={app_user_id}")
    print(f"  fetch_token={fetch_token[:30]}...")
    return _post_json(cfg.revenuecat_url, body, headers)


def parse_subscription_result(result: dict, cfg: Config) -> str:
    sub = result.get("subscriber", {})
    if not isinstance(sub, dict):
        return f"No subscriber object. keys={list(result.keys())}"
    ents = sub.get("entitlements", {})
    if isinstance(ents, dict):
        info = ents.get(cfg.entitlement_id)
        if isinstance(info, dict):
            return f"Plus active, expires={info.get('expires_date', 'unknown')}"
    subs = sub.get("subscriptions", {})
    if isinstance(subs, dict):
        s = subs.get(cfg.product_id)
        if isinstance(s, dict):
            return f"Subscribed, expires={s.get('expires_date', 'unknown')}"
    return f"No active subscription. keys={list(result.keys())}"


def activate_plus(fetch_token: str, app_user_id: str, cfg: Config,
                  storefront: str = "US") -> bool:
    status, result = submit_to_revenuecat(fetch_token, app_user_id, cfg, storefront=storefront)
    if status == 200:
        print(f"[activate_plus] SUCCESS status={status}")
        print(f"[activate_plus] {parse_subscription_result(result, cfg)}")
        return True
    print(f"[activate_plus] FAILED status={status}")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return False
