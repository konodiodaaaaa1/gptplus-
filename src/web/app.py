"""FastAPI WebUI 后端.

启动:
    python -m src.web.app --host 0.0.0.0 --port 8080

接口:
    GET  /api/status                     MuMu 状态
    GET  /api/accounts                   列出所有账号
    POST /api/accounts/import            批量导入 [{email, password}, ...]
    POST /api/accounts/{email}/pipeline  对该账号跑完整链路
    DELETE /api/accounts/{email}         删除账号
    GET  /api/tokens                     列出 token 队列
    POST /api/tokens/activate            {fetch_token, account_id} 激活
    POST /api/tokens/activate-next       {account_id} 从队列取下一个激活
    GET  /api/logs                       任务日志
    GET  /api/sub2api/export             导出已激活账号供 sub2api
    POST /api/intercept/start            启动 mitmproxy 拦截
    POST /api/intercept/stop             停止 mitmproxy
"""
from __future__ import annotations

import argparse
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlmodel import select

from . import db, automation
from .protection import (
    get_breaker_registry,
    get_checkpoint_store,
    CircuitBreakerOpenError,
)
from .db import GoogleAccount, CapturedToken, TaskLog, get_session, log_task
from ..config import load_config
from ..mumu_detect import detect_mumu
from ..mitm_runner import start_mitmproxy, stop_mitmproxy, set_mumu_proxy_to_mitm
from ..token_queue import TokenQueue
from ..revenuecat import get_account_id, activate_plus, assemble_revenuecat_headers, assemble_revenuecat_body

import os

_TZ = timezone(timedelta(hours=8))
_HERE = os.path.dirname(__file__)

app = FastAPI(title="GPT Plus 模拟器 WebUI", version="0.1.0")
app.mount("/static", StaticFiles(directory=os.path.join(_HERE, "static")), name="static")

# 全局 mitmproxy 进程 (单实例)
_mitm_proc = None
_mitm_lock = threading.Lock()


# ============================================================
# 请求模型
# ============================================================

class AccountImport(BaseModel):
    email: str
    password: str


class BatchImport(BaseModel):
    accounts: list[AccountImport]


class ActivateReq(BaseModel):
    fetch_token: str
    account_id: str
    storefront: str = "US"


class ActivateNextReq(BaseModel):
    account_id: str
    storefront: str = "US"


# ============================================================
# 页面
# ============================================================

@app.get("/", response_class=HTMLResponse)
async def index():
    with open(os.path.join(_HERE, "static", "index.html"), "r", encoding="utf-8") as f:
        return f.read()


# ============================================================
# MuMu 状态
# ============================================================

@app.get("/api/status")
async def api_status():
    cfg = load_config()
    inst = detect_mumu(cfg)
    if inst is None:
        return {"online": False, "message": "未检测到 MuMu 模拟器"}
    return {
        "online": True,
        "adb_path": inst.adb_path,
        "serial": inst.serial,
        "android_version": inst.android_version,
        "rooted": inst.rooted,
        "gpt_installed": inst.gpt_installed,
        "google_accounts": inst.google_accounts,
        "current_proxy": inst.current_proxy,
        "mitm_running": _mitm_proc is not None and _mitm_proc.poll() is None,
    }


# ============================================================
# 账号管理
# ============================================================

@app.get("/api/accounts")
async def api_list_accounts():
    with get_session() as s:
        rows = s.exec(select(GoogleAccount).order_by(GoogleAccount.id)).all()
        return [r.dict() for r in rows]


@app.post("/api/accounts/import")
async def api_import(batch: BatchImport):
    added, skipped = 0, 0
    with get_session() as s:
        for a in batch.accounts:
            exists = s.exec(select(GoogleAccount).where(GoogleAccount.email == a.email)).first()
            if exists:
                skipped += 1
                continue
            s.add(GoogleAccount(email=a.email, password=a.password, status="pending"))
            added += 1
        s.commit()
    return {"added": added, "skipped": skipped}


@app.delete("/api/accounts/{email}")
async def api_delete_account(email: str):
    with get_session() as s:
        acc = s.exec(select(GoogleAccount).where(GoogleAccount.email == email)).first()
        if not acc:
            raise HTTPException(404, "account not found")
        s.delete(acc)
        s.commit()
    return {"deleted": email}

    return {"started": True, "email": email, "message": "任务已异步启动, 查看日志查看进度"}


# ============================================================
# Token 队列
# ============================================================

@app.get("/api/tokens")
async def api_list_tokens():
    with get_session() as s:
        rows = s.exec(select(CapturedToken).order_by(CapturedToken.id.desc())).all()
        return [r.dict() for r in rows]


@app.post("/api/tokens/activate")
async def api_activate(req: ActivateReq):
    cfg = load_config()
    ok = activate_plus(req.fetch_token, req.account_id, cfg=cfg, storefront=req.storefront)
    if ok:
        with get_session() as s:
            tok = s.exec(select(CapturedToken).where(CapturedToken.fetch_token == req.fetch_token)).first()
            if tok:
                tok.used = True
                tok.used_by_account_id = req.account_id
                tok.used_at = datetime.now(_TZ).isoformat()
                s.add(tok)
            acc = s.exec(select(GoogleAccount).where(GoogleAccount.gpt_account_id == req.account_id)).first()
            if acc:
                acc.plus_active = True
                acc.status = "subscribed"
                s.add(acc)
            s.commit()
        log_task("activate", "success", message=f"token={req.fetch_token[:16]}... -> {req.account_id}")
        return {"activated": True}
    log_task("activate", "failed", message=f"token={req.fetch_token[:16]}... -> {req.account_id}")
    return {"activated": False}


@app.post("/api/tokens/activate-next")
async def api_activate_next(req: ActivateNextReq):
    """从队列取下一个可用 token 激活到指定 account_id."""
    with get_session() as s:
        tok = s.exec(
            select(CapturedToken).where(CapturedToken.used == False).order_by(CapturedToken.id)
        ).first()
        if not tok:
            raise HTTPException(404, "no available token in queue")
        fetch_token = tok.fetch_token
    return await api_activate(ActivateReq(fetch_token=fetch_token, account_id=req.account_id, storefront=req.storefront))


# ============================================================
# 拦截控制
# ============================================================

@app.post("/api/intercept/start")
async def api_intercept_start():
    global _mitm_proc
    with _mitm_lock:
        if _mitm_proc is not None and _mitm_proc.poll() is None:
            return {"running": True, "message": "already running"}
        cfg = load_config()
        addon_path = os.path.join(_HERE, "..", "addon.py")
        _mitm_proc = start_mitmproxy(cfg, capture_addon_path=os.path.abspath(addon_path))
        if _mitm_proc is None:
            raise HTTPException(500, "mitmproxy 启动失败, 请确认已 pip install mitmproxy")
        # 把 MuMu 代理切到 mitmproxy
        inst = detect_mumu(cfg)
        if inst:
            set_mumu_proxy_to_mitm(inst, cfg)
        return {"running": True, "pid": _mitm_proc.pid}


@app.post("/api/intercept/stop")
async def api_intercept_stop():
    global _mitm_proc
    with _mitm_lock:
        if _mitm_proc is None:
            return {"stopped": True, "message": "not running"}
        cfg = load_config()
        inst = detect_mumu(cfg)
        stop_mitmproxy(_mitm_proc, inst, cfg)
        _mitm_proc = None
        return {"stopped": True}


# ============================================================
# 日志
# ============================================================

@app.get("/api/logs")
async def api_logs(limit: int = 50):
    return [l.dict() for l in db.list_recent_logs(limit)]

# ============================================================
# 熔断器与断点 (失败保护)
# ============================================================

@app.get("/api/circuits")
async def api_circuits():
    """查看所有熔断器状态 (CLOSED/OPEN/HALF_OPEN + 失败计数)."""
    return get_breaker_registry().all_states()


@app.post("/api/circuits/{name}/reset")
async def api_circuit_reset(name: str):
    """手动重置某个熔断器 (或全部)."""
    get_breaker_registry().reset(name)
    return {"reset": name}


@app.post("/api/circuits/reset-all")
async def api_circuit_reset_all():
    get_breaker_registry().reset()
    return {"reset": "all"}


@app.get("/api/checkpoints")
async def api_checkpoints():
    """查看所有账号的 pipeline 断点状态."""
    return get_checkpoint_store().all_states()


@app.get("/api/checkpoints/{email}")
async def api_checkpoint_get(email: str):
    cp = get_checkpoint_store().get(email)
    return cp.to_dict()


@app.post("/api/checkpoints/{email}/reset")
async def api_checkpoint_reset(email: str):
    """重置某账号的断点 (下次跑会从头开始)."""
    get_checkpoint_store().reset(email)
    return {"reset": email}


class PipelineReq(BaseModel):
    resume: bool = True


@app.post("/api/accounts/{email}/pipeline")
async def api_run_pipeline(email: str, resume: bool = True):
    """对该账号异步跑完整链路. resume=True 从断点续跑, False 从头开始."""
    def _worker():
        try:
            automation.run_pipeline_with_checkpoint(email, resume=resume)
        except CircuitBreakerOpenError as e:
            db.log_task("pipeline", "failed", message=str(e), target_email=email)
        except Exception as e:
            db.log_task("pipeline", "failed", message=f"unexpected: {e}", target_email=email)
    threading.Thread(target=_worker, daemon=True).start()
    return {"started": True, "email": email, "resume": resume,
            "message": "任务已异步启动, 查看 /api/logs 或 /api/checkpoints/" + email + " 看进度"}


# ============================================================
# sub2api 导出接口
# ============================================================

@app.get("/api/sub2api/export")
async def api_sub2api_export():
    """导出已激活 Plus 的账号供 sub2api 使用.

    返回格式兼容常见 sub2api 项目: [{email, account_id, jwt, plus_expires, storefront}]
    """
    with get_session() as s:
        rows = s.exec(select(GoogleAccount).where(GoogleAccount.plus_active == True)).all()
        return [{
            "email": r.email,
            "account_id": r.gpt_account_id,
            "jwt": r.gpt_jwt,
            "plus_expires": r.plus_expires,
            "storefront": "US",
        } for r in rows]


@app.get("/api/sub2api/config")
async def api_sub2api_config():
    """生成 sub2api 兼容的配置片段."""
    with get_session() as s:
        rows = s.exec(select(GoogleAccount).where(GoogleAccount.plus_active == True)).all()
        accounts = [{
            "email": r.email,
            "account_id": r.gpt_account_id,
            "token": r.gpt_jwt,
        } for r in rows if r.gpt_jwt]
    return {
        "accounts": accounts,
        "api_base": "https://android.chat.openai.com/backend-api",
        "note": "sub2api 兼容格式, 请按你实际 sub2api 项目要求调整字段",
    }


def main() -> None:
    import uvicorn
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args()
    print(f"WebUI: http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
