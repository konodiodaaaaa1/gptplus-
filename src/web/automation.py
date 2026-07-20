"""MuMu UI 自动化编排: 通过 ADB input 模拟点击/输入.

改造点 (失败检查 + 熔断 + 断点保护):
    1. 每个阶段 (play_login / gpt_login / wait_token / activate / verify) 都被
       CircuitBreaker 包裹, 连续失败 N 次自动熔断
    2. CheckpointStore 持久化每个账号已完成的阶段, 中断后可 resume_pipeline 从断点续跑
    3. 每步用 retry_with_backoff 处理瞬时错误
    4. 超时/截图/异常都记录到 TaskLog, 便于排查
"""
from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import db
from sqlmodel import select
from .protection import (
    CircuitBreakerOpenError,
    PIPELINE_STAGES,
    Checkpoint,
    get_checkpoint_store,
    get_breaker_registry,
    retry_with_backoff,
    with_circuit,
)
from ..config import load_config
from ..mumu_detect import detect_mumu, MuMuInstance, _run_adb

_TZ = timezone(timedelta(hours=8))


@dataclass
class StepResult:
    ok: bool
    message: str
    screenshot_path: Optional[str] = None
    stage: Optional[str] = None  # 完成了哪个阶段 (用于 checkpoint)


def _adb(inst: MuMuInstance, args: list[str], timeout: int = 15) -> tuple[int, str]:
    return _run_adb(inst.adb_path, inst.serial, args, timeout=timeout)


def _tap(inst: MuMuInstance, x: int, y: int) -> None:
    _adb(inst, ["shell", "input", "tap", str(x), str(y)])


def _input_text(inst: MuMuInstance, text: str) -> None:
    safe = text.replace(" ", "%s").replace("&", "\\&").replace("<", "\\<").replace(">", "\\>")
    _adb(inst, ["shell", "input", "text", safe])


def _key(inst: MuMuInstance, key: str) -> None:
    _adb(inst, ["shell", "input", "keyevent", key])


def _uiautomator_dump(inst: MuMuInstance) -> str:
    _adb(inst, ["shell", "uiautomator", "dump", "/sdcard/ui.xml"], timeout=10)
    rc, out = _adb(inst, ["shell", "cat", "/sdcard/ui.xml"])
    return out if rc == 0 else ""


def _find_element_by_text(xml: str, text_pattern: str) -> Optional[tuple[int, int, int, int]]:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    pat = re.compile(text_pattern, re.IGNORECASE)
    for elem in root.iter("node"):
        attrs = elem.attrib
        txt = attrs.get("text", "") + " " + attrs.get("content-desc", "")
        if pat.search(txt):
            bounds = attrs.get("bounds", "")
            m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
            if m:
                l, t, r, b = map(int, m.groups())
                return (l, t, r, b)
    return None


def _tap_center(bounds: tuple[int, int, int, int]) -> tuple[int, int]:
    l, t, r, b = bounds
    return (l + r) // 2, (t + b) // 2


def _wait_for_text(inst: MuMuInstance, pattern: str, timeout: int = 30, interval: float = 1.5) -> Optional[tuple[int, int, int, int]]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        xml = _uiautomator_dump(inst)
        b = _find_element_by_text(xml, pattern)
        if b:
            return b
        time.sleep(interval)
    return None


def _screenshot(inst: MuMuInstance, name: str) -> str:
    import os
    d = os.path.join("screenshots")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{name}.png")
    _adb(inst, ["shell", "screencap", "-p", "/sdcard/shot.png"])
    _adb(inst, ["pull", "/sdcard/shot.png", path])
    return path


def _get_instance() -> Optional[MuMuInstance]:
    cfg = load_config()
    return detect_mumu(cfg)


def _update_account_status(email: str, status: str, **fields) -> None:
    with db.get_session() as s:
        from .db import GoogleAccount
        acc = s.exec(select(GoogleAccount).where(GoogleAccount.email == email)).first()
        if acc:
            acc.status = status
            for k, v in fields.items():
                setattr(acc, k, v)
            acc.updated_at = datetime.now(_TZ).isoformat()
            s.add(acc)
            s.commit()


# ============================================================
# 各阶段实现 (全部被 CircuitBreaker 保护)
# ============================================================

@with_circuit("play_login", failure_threshold=3, cooldown_seconds=300)
def stage_play_login(email: str, password: str) -> StepResult:
    inst = _get_instance()
    if inst is None:
        return StepResult(False, "未检测到 MuMu", stage="play_login")
    try:
        _adb(inst, ["shell", "am", "start", "-a", "android.settings.ADD_ACCOUNT_SETTINGS"])
        time.sleep(2)
        b = _wait_for_text(inst, r"Google", timeout=10)
        if not b:
            return StepResult(False, "未找到 Google 账号选项",
                              _screenshot(inst, f"play_{email}_step1"), stage="play_login")
        _tap(inst, *_tap_center(b))
        time.sleep(3)

        b = _wait_for_text(inst, r"邮箱|email|Email", timeout=15)
        if not b:
            return StepResult(False, "未找到邮箱输入框",
                              _screenshot(inst, f"play_{email}_step2"), stage="play_login")
        _tap(inst, *_tap_center(b))
        time.sleep(1)
        _input_text(inst, email)
        _key(inst, "KEYCODE_ENTER")
        time.sleep(3)

        b = _wait_for_text(inst, r"密码|password|Password", timeout=15)
        if not b:
            return StepResult(False, "未找到密码输入框 (可能需要二次验证)",
                              _screenshot(inst, f"play_{email}_step3"), stage="play_login")
        _tap(inst, *_tap_center(b))
        time.sleep(1)
        _input_text(inst, password)
        _key(inst, "KEYCODE_ENTER")
        time.sleep(5)

        xml = _uiautomator_dump(inst)
        if re.search(r"无法登录|wrong|incorrect|错误", xml, re.I):
            return StepResult(False, "登录失败: 凭证错误或触发风控",
                              _screenshot(inst, f"play_{email}_fail"), stage="play_login")
        if re.search(r"验证|verify|2-step|两步", xml, re.I):
            return StepResult(False, "需要二次验证, 请手动完成",
                              _screenshot(inst, f"play_{email}_2fa"), stage="play_login")

        _update_account_status(email, "play_logged_in")
        return StepResult(True, "Play Store 登录成功", stage="play_login")
    except Exception as e:
        return StepResult(False, f"异常: {e}",
                          _screenshot(inst, f"play_{email}_err") if inst else None,
                          stage="play_login")


@with_circuit("gpt_login", failure_threshold=3, cooldown_seconds=300)
def stage_gpt_login(email: str) -> StepResult:
    inst = _get_instance()
    if inst is None:
        return StepResult(False, "未检测到 MuMu", stage="gpt_login")
    try:
        _adb(inst, ["shell", "am", "start", "-n", "com.openai.chatgpt/.MainActivity"])
        time.sleep(5)
        rc, out = _adb(inst,
                       ["shell", "su -c 'cat /data/data/com.openai.chatgpt/shared_prefs/com_revenuecat_purchases_preferences.xml'"])
        m = re.search(r'\.new">([0-9a-f-]{36})<', out)
        if m:
            account_id = m.group(1)
            _update_account_status(email, "gpt_logged_in", gpt_account_id=account_id)
            return StepResult(True, f"GPT 已登录, account_id={account_id}", stage="gpt_login")
        return StepResult(False, "未检测到 account_id, 需手动完成 OAuth 登录",
                          _screenshot(inst, f"gpt_{email}"), stage="gpt_login")
    except Exception as e:
        return StepResult(False, f"异常: {e}", stage="gpt_login")


def stage_wait_token(email: str, timeout: int = 600) -> StepResult:
    """等待 mitmproxy 捕获 token. 这一步无熔断 (是在等用户操作), 但有超时.

    改造: 每 ~15s 写一条 running 日志, 让 WebUI 在漫长等待期间也有实时反馈.
    """
    store = get_checkpoint_store()
    deadline = time.time() + timeout
    from .db import CapturedToken
    with db.get_session() as s:
        last_count = len(s.exec(select(CapturedToken)).all())
    start_ts = time.time()
    last_log = 0.0
    while time.time() < deadline:
        time.sleep(3)
        with db.get_session() as s:
            now_count = len(s.exec(select(CapturedToken)).all())
        now_ts = time.time()
        if now_ts - last_log >= 15:
            elapsed = int(now_ts - start_ts)
            db.log_task("wait_token", "running", target_email=email,
                        message=f"等待 token 中 ({elapsed}s/{timeout}s), 队列 {now_count} 条")
            last_log = now_ts
        if now_count > last_count:
            db.log_task("wait_token", "success", target_email=email,
                        message=f"token 已捕获 (队列 {now_count} 条)")
            return StepResult(True, "token 已捕获", stage="wait_token")
    db.log_task("wait_token", "failed", target_email=email,
                message=f"等待 token 超时 ({timeout}s)")
    return StepResult(False, f"等待 token 超时 ({timeout}s)", stage="wait_token")


@with_circuit("activate", failure_threshold=3, cooldown_seconds=600)
def stage_activate(email: str) -> StepResult:
    inst = _get_instance()
    if inst is None:
        return StepResult(False, "未检测到 MuMu", stage="activate")
    # 从缓存 token 取下一个, 提交给该账号的 account_id
    with db.get_session() as s:
        from .db import GoogleAccount, CapturedToken
        acc = s.exec(select(GoogleAccount).where(GoogleAccount.email == email)).first()
        if not acc or not acc.gpt_account_id:
            return StepResult(False, "缺少 account_id", stage="activate")
        tok = s.exec(select(CapturedToken).where(CapturedToken.used == False).order_by(CapturedToken.id)).first()
        if not tok:
            return StepResult(False, "token 队列为空", stage="activate")
        fetch_token = tok.fetch_token
        account_id = acc.gpt_account_id

    from ..revenuecat import activate_plus
    cfg = load_config()
    ok = activate_plus(fetch_token, account_id, cfg=cfg)
    if ok:
        with db.get_session() as s:
            tok_obj = s.exec(select(CapturedToken).where(CapturedToken.fetch_token == fetch_token)).first()
            if tok_obj:
                tok_obj.used = True
                tok_obj.used_by_account_id = account_id
                tok_obj.used_at = datetime.now(_TZ).isoformat()
                s.add(tok_obj)
            acc_obj = s.exec(select(GoogleAccount).where(GoogleAccount.email == email)).first()
            if acc_obj:
                acc_obj.plus_active = True
                acc_obj.status = "subscribed"
                s.add(acc_obj)
            s.commit()
        return StepResult(True, "激活成功", stage="activate")
    return StepResult(False, "RevenueCat 激活失败", stage="activate")


def stage_verify(email: str) -> StepResult:
    """验证 Plus 状态. 通过 RevenueCat subscribers 接口确认."""
    with db.get_session() as s:
        from .db import GoogleAccount
        acc = s.exec(select(GoogleAccount).where(GoogleAccount.email == email)).first()
        if not acc or not acc.gpt_account_id or not acc.gpt_jwt:
            # 没有 JWT 无法独立验证, 信任 activate 阶段的返回
            return StepResult(True, "跳过独立验证 (无 JWT)", stage="verify")
        from ..revenuecat import _get_json
        import urllib.request
        cfg = load_config()
        url = f"https://api.revenuecat.com/v1/subscribers/{acc.gpt_account_id}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {cfg.revenuecat_api_key}"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                import json
                data = json.loads(r.read())
            ents = data.get("subscriber", {}).get("entitlements", {})
            if cfg.entitlement_id in ents:
                expires = ents[cfg.entitlement_id].get("expires_date")
                _update_account_status(email, "subscribed", plus_expires=expires)
                return StepResult(True, f"Plus 已开通, 到期 {expires}", stage="verify")
            return StepResult(False, "Plus 未生效", stage="verify")
        except Exception as e:
            return StepResult(False, f"验证异常: {e}", stage="verify")


# 阶段路由
_STAGES = {
    "play_login": lambda email, cp: stage_play_login(email, _get_password(email)),
    "gpt_login": lambda email, cp: stage_gpt_login(email),
    "wait_token": lambda email, cp: stage_wait_token(email),
    "activate": lambda email, cp: stage_activate(email),
    "verify": lambda email, cp: stage_verify(email),
}


def _get_password(email: str) -> str:
    with db.get_session() as s:
        from .db import GoogleAccount
        acc = s.exec(select(GoogleAccount).where(GoogleAccount.email == email)).first()
        return acc.password if acc else ""


# ============================================================
# Pipeline 主控 (断点续跑)
# ============================================================

def run_pipeline_with_checkpoint(email: str, resume: bool = True) -> StepResult:
    """带断点保护的完整 pipeline.

    Args:
        email: 账号邮箱
        resume: True=从断点续跑; False=从头开始
    """
    store = get_checkpoint_store()
    if resume:
        cp = store.get(email)
    else:
        store.reset(email)
        cp = store.get(email)
    db.log_task("pipeline", "running", target_email=email,
                message=f"stage={cp.current_stage} attempts={cp.attempts}")

    while not cp.is_done():
        stage = cp.next_stage()
        if stage is None:
            break
        # 熔断检查
        breaker = get_breaker_registry().get(stage)
        if not breaker.allow():
            msg = f"熔断器 [{stage}] 已开启, 跳过. 冷却 {breaker.cooldown_seconds}s 后可重试."
            db.log_task(stage, "failed", message=msg, target_email=email)
            return StepResult(False, msg, stage=stage)

        # 超过最大重试次数
        if not cp.can_retry() and cp.failed_stage == stage:
            msg = f"阶段 {stage} 已失败 {cp.attempts}/{cp.max_attempts} 次, 停止. 需人工干预."
            db.log_task(stage, "failed", message=msg, target_email=email)
            _update_account_status(email, "failed", note=msg)
            return StepResult(False, msg, stage=stage)

        # 执行阶段 (带重试退避)
        stage_fn = _STAGES.get(stage)
        if stage_fn is None:
            cp.mark_stage_failed(stage, f"未知阶段 {stage}")
            store.update(cp)
            continue

        try:
            result = retry_with_backoff(
                stage_fn, email, cp,
                max_attempts=1,  # 每个阶段只跑一次, 重试由 cp.attempts 控制
                base_delay=2.0,
                logger=lambda m: db.log_task(stage, "running", message=m, target_email=email),
            )
        except CircuitBreakerOpenError as e:
            cp.mark_stage_failed(stage, str(e))
            store.update(cp)
            return StepResult(False, str(e), stage=stage)
        except Exception as e:
            cp.mark_stage_failed(stage, f"异常: {e}")
            store.update(cp)
            db.log_task(stage, "failed", message=str(e), target_email=email)
            continue

        if result.ok:
            cp.mark_stage_success(stage)
            store.update(cp)
            get_breaker_registry().get(stage).record_success()
            db.log_task(stage, "success", target_email=email, message=result.message[:100])
        else:
            cp.mark_stage_failed(stage, result.message)
            store.update(cp)
            # 业务失败也计入熔断 (系统异常由装饰器计, 业务失败由这里计)
            get_breaker_registry().get(stage).record_failure()
            db.log_task(stage, "failed", target_email=email, message=result.message[:100])
            # 失败后停止 (不自动跳过), 等下次 resume
            return StepResult(False, f"阶段 {stage} 失败: {result.message}",
                              result.screenshot_path, stage=stage)

    if cp.is_done():
        db.log_task("pipeline", "success", target_email=email)
        return StepResult(True, "全部阶段完成", stage="done")
    return StepResult(False, f"未完成, 当前阶段 {cp.current_stage}", stage=cp.current_stage)


# 兼容旧调用
def run_full_pipeline(email: str) -> StepResult:
    return run_pipeline_with_checkpoint(email, resume=True)
