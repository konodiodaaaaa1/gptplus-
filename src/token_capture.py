"""等待并解析 mitmproxy 拦截到的 RevenueCat POST /v1/receipts 请求, 提取 fetch_token.

实现方式: 通过 mitmproxy 的日志文件 (或 Python addon 写入的 JSONL) 检测。
生产环境推荐用内置 mitmproxy addon 直接拦截, 见 src/addon.py。
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Optional

from .config import Config


@dataclass
class CapturedToken:
    fetch_token: str
    app_user_id: str
    storefront: str = "US"


def run_capture_until_token(cfg: Config, timeout: int = 600) -> Optional[CapturedToken]:
    """轮询 token queue 文件直到出现新 token 或超时。

    实际拦截工作由 mitmproxy addon (addon.py) 完成, 它会直接写入 TokenQueue。
    本函数等待并返回最新入队的 token。
    """
    queue_file = cfg.token_queue_file
    start_count = _count_lines(queue_file)
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(2)
        lines = _read_lines(queue_file)
        if len(lines) > start_count:
            record = json.loads(lines[-1])
            return CapturedToken(
                fetch_token=str(record["fetch_token"]),
                app_user_id=str(record.get("original_app_user_id", "")),
                storefront=str(record.get("storefront", "US")),
            )
    return None


def _count_lines(path: str) -> int:
    return len(_read_lines(path))


def _read_lines(path: str) -> list[str]:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return [ln for ln in f.read().splitlines() if ln.strip()]
