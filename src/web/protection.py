"""熔断器 + 断点保护 + 重试退避.

设计目标:
    - 熔断 (CircuitBreaker): 连续失败 N 次后打开熔断, 暂停一段时间再尝试, 避免对风控敏感的 API 反复试探
    - 断点保护 (Checkpoint): 每个账号的 pipeline 分阶段落盘, 中断后可从最近完成的阶段续跑
    - 重试退避 (retry_with_backoff): 对瞬时错误按指数退避重试, 区分可重试/不可重试错误
"""
from __future__ import annotations

import json
import os
import time
import functools
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional, TypeVar

_TZ = timezone(timedelta(hours=8))


# ============================================================
# 1. 熔断器
# ============================================================

class CircuitBreakerOpenError(Exception):
    """熔断器已打开, 调用应被拒绝."""


@dataclass
class CircuitState:
    name: str
    failure_count: int = 0
    failure_threshold: int = 3
    cooldown_seconds: int = 300
    opened_at: Optional[str] = None
    # OPEN / CLOSED / HALF_OPEN
    state: str = "CLOSED"

    def to_dict(self) -> dict:
        return asdict(self)

    def record_success(self) -> None:
        self.failure_count = 0
        self.state = "CLOSED"
        self.opened_at = None

    def record_failure(self) -> None:
        self.failure_count += 1
        if self.failure_count >= self.failure_threshold and self.state != "OPEN":
            self.state = "OPEN"
            self.opened_at = datetime.now(_TZ).isoformat()

    def allow(self) -> bool:
        """是否允许通过. OPEN 且未过冷却期 -> 拒绝; 过冷却期 -> HALF_OPEN 放行一次."""
        if self.state == "CLOSED":
            return True
        if self.state == "OPEN":
            if self.opened_at is None:
                return True
            opened = datetime.fromisoformat(self.opened_at)
            if datetime.now(_TZ) - opened >= timedelta(seconds=self.cooldown_seconds):
                self.state = "HALF_OPEN"
                return True
            return False
        # HALF_OPEN: 放行一次
        return True


class CircuitBreakerRegistry:
    """全局熔断器注册表 (进程内单例, 也可通过 /api/circuits 查询状态)."""

    def __init__(self) -> None:
        self._circuits: dict[str, CircuitState] = {}

    def get(self, name: str, failure_threshold: int = 3, cooldown_seconds: int = 300) -> CircuitState:
        if name not in self._circuits:
            self._circuits[name] = CircuitState(
                name=name,
                failure_threshold=failure_threshold,
                cooldown_seconds=cooldown_seconds,
            )
        return self._circuits[name]

    def all_states(self) -> dict[str, dict]:
        return {k: v.to_dict() for k, v in self._circuits.items()}

    def reset(self, name: Optional[str] = None) -> None:
        if name is None:
            self._circuits.clear()
        elif name in self._circuits:
            self._circuits[name].record_success()


# 全局单例
_breaker_registry = CircuitBreakerRegistry()


def get_breaker_registry() -> CircuitBreakerRegistry:
    return _breaker_registry


def with_circuit(name: str, failure_threshold: int = 3, cooldown_seconds: int = 300):
    """装饰器: 包裹的函数进入熔断器保护.

    用法:
        @with_circuit("play_login", failure_threshold=3, cooldown_seconds=300)
        def play_store_login(...): ...
    """
    def decorator(fn: Callable):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            breaker = _breaker_registry.get(name, failure_threshold, cooldown_seconds)
            if not breaker.allow():
                raise CircuitBreakerOpenError(
                    f"熔断器 [{name}] 已开启, 将在冷却 {breaker.cooldown_seconds}s 后恢复. "
                    f"当前失败计数 {breaker.failure_count}/{breaker.failure_threshold}"
                )
            try:
                result = fn(*args, **kwargs)
                breaker.record_success()
                return result
            except Exception as e:
                # 不把 KeyboardInterrupt 算作失败
                if isinstance(e, KeyboardInterrupt):
                    raise
                breaker.record_failure()
                raise
        return wrapper
    return decorator


# ============================================================
# 2. 断点保护 (Checkpoint)
# ============================================================

# Pipeline 阶段定义 (按顺序执行)
PIPELINE_STAGES = [
    "play_login",        # Play Store 登录
    "gpt_login",         # GPT app 登录 (获取 account_id)
    "wait_token",        # 等待 mitmproxy 捕获 token
    "activate",          # 提交 RevenueCat 激活
    "verify",            # 验证 Plus 已开通
]


@dataclass
class Checkpoint:
    email: str
    current_stage: str = "play_login"
    completed_stages: list[str] = field(default_factory=list)
    failed_stage: Optional[str] = None
    last_error: Optional[str] = None
    attempts: int = 0
    max_attempts: int = 3
    updated_at: str = field(default_factory=lambda: datetime.now(_TZ).isoformat())

    def to_dict(self) -> dict:
        return asdict(self)

    def is_done(self) -> bool:
        return len(self.completed_stages) >= len(PIPELINE_STAGES)

    def next_stage(self) -> Optional[str]:
        if self.is_done():
            return None
        return PIPELINE_STAGES[len(self.completed_stages)]

    def mark_stage_success(self, stage: str) -> None:
        if stage not in self.completed_stages:
            self.completed_stages.append(stage)
        self.current_stage = self.next_stage() or "done"
        self.failed_stage = None
        self.last_error = None
        self.updated_at = datetime.now(_TZ).isoformat()

    def mark_stage_failed(self, stage: str, err: str) -> None:
        self.failed_stage = stage
        self.last_error = err
        self.attempts += 1
        self.updated_at = datetime.now(_TZ).isoformat()

    def can_retry(self) -> bool:
        return self.attempts < self.max_attempts


class CheckpointStore:
    """断点持久化 (JSON 文件), 支持中断后恢复."""

    def __init__(self, path: str = "checkpoints.json") -> None:
        self.path = path
        self._cache: dict[str, Checkpoint] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for email, d in data.items():
                self._cache[email] = Checkpoint(**d)
        except Exception:
            pass

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        data = {email: cp.to_dict() for email, cp in self._cache.items()}
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def get(self, email: str) -> Checkpoint:
        if email not in self._cache:
            self._cache[email] = Checkpoint(email=email)
        return self._cache[email]

    def update(self, cp: Checkpoint) -> None:
        self._cache[cp.email] = cp
        self._save()

    def reset(self, email: str) -> None:
        self._cache.pop(email, None)
        self._save()

    def all_states(self) -> dict[str, dict]:
        return {email: cp.to_dict() for email, cp in self._cache.items()}


_checkpoint_store: Optional[CheckpointStore] = None


def get_checkpoint_store() -> CheckpointStore:
    global _checkpoint_store
    if _checkpoint_store is None:
        path = os.environ.get("GPTPLUS_CHECKPOINTS", "checkpoints.json")
        _checkpoint_store = CheckpointStore(path)
    return _checkpoint_store


# ============================================================
# 3. 重试退避
# ============================================================

T = TypeVar("T")


def retry_with_backoff(
    fn: Callable[..., T],
    *args,
    max_attempts: int = 3,
    base_delay: float = 2.0,
    max_delay: float = 30.0,
    retryable_exceptions: tuple = (Exception,),
    logger: Optional[Callable[[str], None]] = None,
    **kwargs,
) -> T:
    """对可重试异常按指数退避重试.

    Args:
        fn: 要调用的函数
        max_attempts: 最大尝试次数 (含首次)
        base_delay: 首次失败后等待秒数
        max_delay: 退避上限
        retryable_exceptions: 哪些异常视为可重试 (默认所有 Exception)
        logger: 可选日志回调
    """
    last_exc: Optional[Exception] = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn(*args, **kwargs)
        except retryable_exceptions as e:
            last_exc = e
            if attempt >= max_attempts:
                break
            delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
            if logger:
                logger(f"[retry] attempt {attempt}/{max_attempts} failed: {e}, retry in {delay:.1f}s")
            time.sleep(delay)
    assert last_exc is not None
    raise last_exc
