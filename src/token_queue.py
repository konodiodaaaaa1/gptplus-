"""持久化 token 队列 (tokens.jsonl), 带过期与已用标记."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

from .config import Config


class TokenQueue:
    def __init__(self, path: str, expiry_hours: int = 72) -> None:
        self.path = path
        self.expiry_hours = expiry_hours
        self._tz = timezone(timedelta(hours=8))

    def _read_all(self) -> list[dict]:
        if not os.path.exists(self.path):
            return []
        out: list[dict] = []
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out

    def _write_all(self, items: list[dict]) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            for r in items:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    def enqueue(self, fetch_token: str, metadata: dict | None = None) -> int:
        now = datetime.now(self._tz)
        expires = now + timedelta(hours=self.expiry_hours)
        record = {
            "fetch_token": fetch_token,
            "captured_at": now.isoformat(),
            "expires_at": expires.isoformat(),
            "used": False,
            "used_by": None,
            "used_at": None,
            **(metadata or {}),
        }
        items = self._read_all()
        items.append(record)
        self._write_all(items)
        return len(items) - 1

    def dequeue(self) -> tuple[int, dict] | None:
        now = datetime.now(self._tz)
        for i, r in enumerate(self._read_all()):
            if r.get("used"):
                continue
            try:
                exp = datetime.fromisoformat(str(r["expires_at"]))
            except Exception:
                continue
            if exp <= now:
                continue
            return i, r
        return None

    def mark_used(self, index: int, account_id: str) -> bool:
        items = self._read_all()
        if index < 0 or index >= len(items):
            return False
        items[index]["used"] = True
        items[index]["used_by"] = account_id
        items[index]["used_at"] = datetime.now(self._tz).isoformat()
        self._write_all(items)
        return True

    def status(self) -> str:
        items = self._read_all()
        now = datetime.now(self._tz)
        total = len(items)
        avail = used = expired = 0
        for r in items:
            try:
                exp = datetime.fromisoformat(str(r["expires_at"]))
            except Exception:
                continue
            if r.get("used"):
                used += 1
            elif exp <= now:
                expired += 1
            else:
                avail += 1
        lines = [f"total={total} available={avail} used={used} expired={expired}"]
        for i, r in enumerate(items):
            if r.get("used"):
                continue
            try:
                exp = datetime.fromisoformat(str(r["expires_at"]))
            except Exception:
                continue
            if exp <= now:
                continue
            preview = str(r["fetch_token"])[:20] + "..."
            lines.append(f"  [{i}] {preview}  expires={str(r['expires_at'])[:19]}")
        return "\n".join(lines)
