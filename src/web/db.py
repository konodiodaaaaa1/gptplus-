"""SQLite 数据模型: 账号 + token 队列 + 任务."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlmodel import Field, SQLModel, Session, create_engine, select
from sqlalchemy import text as sql_text

_TZ = timezone(timedelta(hours=8))


class GoogleAccount(SQLModel, table=True):
    """批量导入的 Google 邮箱账号 (已有账号, 非自动注册)."""
    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(index=True, unique=True)
    password: str
    # 状态: pending / play_logging_in / play_logged_in / gpt_opened / purchasing / capturing / got_account_id / subscribed / failed / used
    status: str = Field(default="pending", index=True)
    # 备注与错误信息
    note: str = Field(default="")
    # 关联的 GPT account_id 与 JWT (登录成功后填充)
    gpt_account_id: Optional[str] = Field(default=None, index=True)
    gpt_jwt: Optional[str] = Field(default=None)
    # 目标 GPT 账号的 JWT (用于转移: 支付账号 A 的 Google 邮箱 + 目标账号 B 的 JWT)
    # 导入格式: email----password----jwt, 其中 jwt 是目标账号的 Bearer token
    target_jwt: Optional[str] = Field(default=None)
    # 是否 Plus 已激活
    plus_active: bool = Field(default=False)
    plus_expires: Optional[str] = Field(default=None)
    # 时间戳
    created_at: str = Field(default_factory=lambda: datetime.now(_TZ).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(_TZ).isoformat())


class CapturedToken(SQLModel, table=True):
    """通过 mitmproxy 捕获的 fetch_token."""
    id: Optional[int] = Field(default=None, primary_key=True)
    fetch_token: str = Field(index=True)
    captured_at: str = Field(default_factory=lambda: datetime.now(_TZ).isoformat())
    expires_at: str
    original_app_user_id: Optional[str] = Field(default=None)
    storefront: str = Field(default="US")
    used: bool = Field(default=False, index=True)
    used_by_account_id: Optional[str] = Field(default=None)
    used_at: Optional[str] = Field(default=None)


class TaskLog(SQLModel, table=True):
    """任务执行日志."""
    id: Optional[int] = Field(default=None, primary_key=True)
    task_type: str = Field(index=True)  # play_login / gpt_login / subscribe / activate
    target_email: Optional[str] = Field(default=None, index=True)
    status: str  # running / success / failed
    message: str = Field(default="")
    started_at: str = Field(default_factory=lambda: datetime.now(_TZ).isoformat())
    finished_at: Optional[str] = Field(default=None)


_engine = None


def get_engine():
    global _engine
    if _engine is None:
        db_path = os.environ.get("GPTPLUS_DB", "gptplus.db")
        _engine = create_engine(f"sqlite:///{db_path}", echo=False, connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(_engine)
        # 增量迁移: 给旧 DB 补新列 (SQLModel create_all 不会加列)
        _migrate_add_column(_engine, "googleaccount", "target_jwt", "TEXT")
    return _engine


def _migrate_add_column(engine, table: str, column: str, col_type: str) -> None:
    """幂等地给已有表补列 (SQLite ALTER TABLE ADD COLUMN)."""
    try:
        with engine.connect() as conn:
            # 检查列是否已存在
            result = conn.execute(sql_text(f"PRAGMA table_info({table})"))
            existing = {row[1] for row in result}
            if column not in existing:
                conn.execute(sql_text(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"))
                conn.commit()
    except Exception:
        pass  # 表不存在或其他问题, create_all 会处理


def get_session() -> Session:
    return Session(get_engine())


def log_task(task_type: str, status: str, message: str = "", target_email: str | None = None) -> TaskLog:
    with get_session() as s:
        log = TaskLog(task_type=task_type, status=status, message=message, target_email=target_email)
        if status != "running":
            log.finished_at = datetime.now(_TZ).isoformat()
        s.add(log)
        s.commit()
        s.refresh(log)
        return log


def list_recent_logs(limit: int = 50) -> list[TaskLog]:
    with get_session() as s:
        return list(s.exec(select(TaskLog).order_by(TaskLog.id.desc()).limit(limit)).all())
