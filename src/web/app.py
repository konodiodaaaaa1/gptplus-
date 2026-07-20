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
import json
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


class ConfigReq(BaseModel):
    adb_path: str = ""
    serial: str = ""
    mitm_port: int = 8888
    upstream_proxy: str = ""


# ============================================================
# 页面
# ============================================================

@app.get("/", response_class=HTMLResponse)
async def index():
    with open(os.path.join(_HERE, "static", "index.html"), "r", encoding="utf-8") as f:
        return f.read()


# ============================================================
# 对接配置 (MuMu adb/serial + mitm 上游代理)
# ============================================================

def _save_config_toml(adb_path: str, serial: str, mitm_port: int, upstream_proxy: str) -> str:
    """把对接配置写回 config.toml (保留 [api] [files] 段).

    toml literal string (单引号) 不处理转义, 含反斜杠的 Windows 路径安全.
    """
    cfg_path = os.path.abspath(os.path.join(_HERE, "..", "..", "config.toml"))
    # 读现有 [api] [files] 保留
    existing = {}
    try:
        import tomllib
        with open(cfg_path, "rb") as f:
            existing = tomllib.load(f)
    except Exception:
        pass
    lines = ["[mumu]"]
    if adb_path:
        lines.append(f"adb_path = '{adb_path}'")
    if serial:
        lines.append(f"serial = '{serial}'")
    lines.append("")
    lines.append("[mitm]")
    lines.append('host = "0.0.0.0"')
    lines.append(f"port = {int(mitm_port)}")
    if upstream_proxy:
        lines.append(f'upstream_proxy = "{upstream_proxy}"')
    lines.append("")
    api_sec = existing.get("api", {})
    if api_sec:
        lines.append("[api]")
        for k, v in api_sec.items():
            if isinstance(v, str):
                lines.append(f'{k} = "{v}"')
            elif isinstance(v, bool):
                lines.append(f'{k} = {"true" if v else "false"}')
            else:
                lines.append(f'{k} = {v}')
        lines.append("")
    files_sec = existing.get("files", {})
    if files_sec:
        lines.append("[files]")
        for k, v in files_sec.items():
            if isinstance(v, str):
                lines.append(f'{k} = "{v}"')
            else:
                lines.append(f'{k} = {v}')
        lines.append("")
    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return cfg_path


@app.get("/api/config")
async def api_get_config():
    """读当前对接配置 (MuMu adb/serial + mitm 端口 + 上游代理)."""
    cfg = load_config()
    return {
        "adb_path": cfg.adb_path,
        "serial": cfg.serial,
        "mitm_host": cfg.mitm_host,
        "mitm_port": cfg.mitm_port,
        "upstream_proxy": cfg.upstream_proxy,
        "mitm_running": _mitm_proc is not None and _mitm_proc.poll() is None,
    }


@app.post("/api/config")
async def api_set_config(req: ConfigReq):
    """保存对接配置到 config.toml. upstream_proxy 变更需重启 mitm 才生效."""
    cfg_path = _save_config_toml(req.adb_path, req.serial, req.mitm_port, req.upstream_proxy)
    mitm_running = _mitm_proc is not None and _mitm_proc.poll() is None
    hint = None
    if mitm_running:
        hint = "mitm 正在运行, upstream_proxy 变更需停止后重新启动拦截才生效"
    log_task("config", "success",
             message=f"adb={req.adb_path or '(自动)'} serial={req.serial or '(自动)'} "
                     f"port={req.mitm_port} upstream={req.upstream_proxy or '(直连)'}")
    return {"saved": True, "config_path": cfg_path, "mitm_running": mitm_running, "hint": hint}


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

def _sync_tokens_file_to_db() -> None:
    """把 tokens.jsonl 中未入 DB 的 token 同步进 capturedtoken 表.

    addon 捕获 token 时已双写文件 + DB; 此函数做容错: 当 addon 因 DB 锁/异常
    漏写 DB 但写了文件时, WebUI 拉取 /api/tokens 会把文件里的 token 补进 DB.
    """
    cfg = load_config()
    qfile = cfg.token_queue_file
    if not os.path.exists(qfile):
        return
    try:
        with open(qfile, "r", encoding="utf-8") as f:
            items = []
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    items.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return
    if not items:
        return
    with get_session() as s:
        for r in items:
            ft = r.get("fetch_token")
            if not ft:
                continue
            exists = s.exec(select(CapturedToken).where(CapturedToken.fetch_token == ft)).first()
            if exists:
                continue
            s.add(CapturedToken(
                fetch_token=ft,
                captured_at=r.get("captured_at", ""),
                expires_at=r.get("expires_at", ""),
                original_app_user_id=r.get("original_app_user_id"),
                storefront=r.get("storefront", "US"),
                used=bool(r.get("used", False)),
                used_by_account_id=r.get("used_by"),
                used_at=r.get("used_at"),
            ))
        s.commit()


@app.get("/api/tokens")
async def api_list_tokens():
    _sync_tokens_file_to_db()
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
# CA 证书注入 (setup)
# ============================================================

@app.post("/api/setup")
async def api_setup():
    """注入 mitmproxy CA 证书到 MuMu 系统 CA 存储 (tmpfs 覆盖挂载).

    前置: MuMu root + 已运行过一次 mitmdump 生成 ~/.mitmproxy/mitmproxy-ca-cert.pem.
    每次 MuMu 重启后需重新注入 (tmpfs 证书会丢失).
    """
    cfg = load_config()
    inst = detect_mumu(cfg)
    if inst is None:
        raise HTTPException(503, "未检测到 MuMu 模拟器")
    from ..ca_inject import install_mitm_ca
    ok = install_mitm_ca(inst, cfg)
    log_task("setup", "success" if ok else "failed",
             message="CA 注入成功" if ok else "CA 注入失败 (检查 root / mitmproxy CA 是否已生成)")
    if not ok:
        raise HTTPException(500, "CA 注入失败, 请检查 MuMu root 状态及 mitmproxy CA 是否已生成")
    return {"installed": True, "ca_path": cfg.mitm_ca_pem}


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


@app.delete("/api/logs")
async def api_logs_clear():
    """清空所有任务日志."""
    from sqlmodel import delete
    with get_session() as s:
        s.exec(delete(TaskLog))
        s.commit()
    return {"cleared": True}

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
