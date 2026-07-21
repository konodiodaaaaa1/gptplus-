"""MuMu UI 自动化编排: 通过 ADB input 模拟点击/输入.

两阶段 Pipeline (与 GPT Plus 订阅转移原理与操作指南一致):

Phase A — 捕获 token (用支付账号完成 Google Play 购买, mitmproxy 拦截):
    ensure_prereqs → play_login → gpt_open → purchase → wait_token

Phase B — 激活 (用目标账号的 account_id 提交 token):
    get_account_id → activate → verify

改造点 (失败检查 + 熔断 + 断点保护):
    1. 每个阶段都被 CircuitBreaker 包裹, 连续失败 N 次自动熔断
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
# Phase A 阶段: 捕获 token
# ============================================================

@with_circuit("ensure_prereqs", failure_threshold=2, cooldown_seconds=120)
def stage_ensure_prereqs(email: str) -> StepResult:
    """自动检查/启动前置条件: mitmproxy + CA 注入 + MuMu 代理.

    确保拦截环境就绪, 否则 wait_token 会空等到超时.
    """
    cfg = load_config()
    inst = detect_mumu(cfg)
    if inst is None:
        return StepResult(False, "未检测到 MuMu", stage="ensure_prereqs")

    try:
        prerequisites_ok = True
        messages = []

        # 1. 检查 mitmproxy 是否在运行
        from ..mitm_runner import start_mitmproxy, set_mumu_proxy_to_mitm
        # 通过 /api/intercept/start 的逻辑检查 — 直接看端口
        import socket
        mitm_running = False
        try:
            with socket.create_connection(("127.0.0.1", cfg.mitm_port), timeout=2):
                mitm_running = True
        except OSError:
            pass

        if not mitm_running:
            messages.append("mitmproxy 未运行, 自动启动")
            import os
            addon_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "addon.py")
            mitm_proc = start_mitmproxy(cfg, capture_addon_path=os.path.abspath(addon_path))
            if mitm_proc is None:
                return StepResult(False, "mitmproxy 启动失败, 请确认已 pip install mitmproxy",
                                  stage="ensure_prereqs")
            # 等待端口就绪
            for _ in range(10):
                time.sleep(1)
                try:
                    with socket.create_connection(("127.0.0.1", cfg.mitm_port), timeout=2):
                        mitm_running = True
                        break
                except OSError:
                    pass
            if not mitm_running:
                return StepResult(False, "mitmproxy 启动后端口未就绪", stage="ensure_prereqs")
            # 记录进程到 app.py 的全局变量 (通过环境变量标记)
            os.environ["GPTPLUS_MITM_AUTO_STARTED"] = "1"
            messages.append("mitmproxy 已自动启动")

        # 2. 检查 CA 是否已注入 (检测设备系统 CA 目录中是否有 mitmproxy 证书)
        rc, out = _adb(inst, ["shell", "su -c 'ls /system/etc/security/cacerts/'"])
        has_ca = "mitmproxy" in out.lower() or rc != 0  # rc!=0 可能没root, 跳过
        if not has_ca and inst.rooted:
            from ..ca_inject import install_mitm_ca
            if install_mitm_ca(inst, cfg):
                messages.append("CA 证书已自动注入")
            else:
                messages.append("CA 注入失败 (可能需重启 MuMu)")

        # 3. 设置 MuMu 代理指向 mitmproxy
        rc_proxy, proxy_out = _adb(inst, ["shell", "settings get global http_proxy"])
        current_proxy = proxy_out.strip()
        # 检查代理是否已指向 mitm (包含 mitm 端口)
        if str(cfg.mitm_port) not in current_proxy or current_proxy == ":0" or current_proxy == "":
            set_mumu_proxy_to_mitm(inst, cfg)
            messages.append("MuMu 代理已指向 mitmproxy")
        else:
            messages.append("MuMu 代理已配置")

        _update_account_status(email, "prereqs_ok")
        all_msg = "; ".join(messages) if messages else "所有前置条件已就绪"
        return StepResult(True, all_msg, stage="ensure_prereqs")

    except Exception as e:
        return StepResult(False, f"前置检查异常: {e}",
                          _screenshot(inst, f"prereqs_{email}_err") if inst else None,
                          stage="ensure_prereqs")


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


@with_circuit("gpt_open", failure_threshold=3, cooldown_seconds=300)
def stage_gpt_open(email: str) -> StepResult:
    """打开 GPT App, 确认主界面已加载.

    此阶段只打开 app, 不获取 account_id (account_id 在 Phase B 通过
    目标账号 JWT 获取, 而非支付账号的 RC prefs).
    """
    inst = _get_instance()
    if inst is None:
        return StepResult(False, "未检测到 MuMu", stage="gpt_open")
    try:
        _adb(inst, ["shell", "am", "start", "-n", "com.openai.chatgpt/.MainActivity"])
        time.sleep(5)

        # 确认 GPT App 已启动 — 等待主界面出现 (聊天入口或侧边栏)
        xml = _uiautomator_dump(inst)
        if re.search(r"ChatGPT|chat|new chat|新对话|menu|菜单", xml, re.I):
            _update_account_status(email, "gpt_opened")
            return StepResult(True, "GPT App 已打开, 主界面已加载", stage="gpt_open")

        # 可能需要登录 — 检测登录按钮
        if re.search(r"log in|sign in|登录|登录", xml, re.I):
            return StepResult(False, "GPT App 需要登录, 请先在设备上完成 GPT 账号登录",
                              _screenshot(inst, f"gpt_open_{email}"), stage="gpt_open")

        return StepResult(False, "GPT App 未检测到主界面",
                          _screenshot(inst, f"gpt_open_{email}"), stage="gpt_open")
    except Exception as e:
        return StepResult(False, f"异常: {e}", stage="gpt_open")


@with_circuit("purchase", failure_threshold=3, cooldown_seconds=300)
def stage_purchase(email: str) -> StepResult:
    """提示用户在 MuMu 上手动完成 Plus 订阅购买.

    操作指南步骤1: "进入订阅页面, 选择 Plus, 完成 Google Play 支付".
    由于 GPT App 各版本 UI 布局差异大, 订阅/支付按钮位置不确定,
    改为等待用户手动完成购买. 购买完成后 mitmproxy 会自动拦截
    RevenueCat POST /v1/receipts 并保存 token.
    """
    inst = _get_instance()
    if inst is None:
        return StepResult(False, "未检测到 MuMu", stage="purchase")
    try:
        # 确保 GPT App 在前台
        _adb(inst, ["shell", "am", "start", "-n", "com.openai.chatgpt/.MainActivity"])
        time.sleep(2)

        _update_account_status(email, "purchasing")
        db.log_task("purchase", "running", target_email=email,
                    message="⏳ 请在 MuMu 上手动操作: 打开 GPT App → 点击 Plus 订阅 → 完成 Google Play 支付. "
                            "mitmproxy 会自动拦截 token.")

        # 轮询等待 token 被捕获 (与 wait_token 逻辑相同, 但这里有即时提示)
        # 实际的 token 等待由后续 stage_wait_token 完成, 这里只等待用户开始购买
        # 检查是否有新的 token 出现, 最多等 120 秒作为"购买引导"窗口
        from .db import CapturedToken
        with db.get_session() as s:
            initial_count = len(s.exec(select(CapturedToken)).all())

        deadline = time.time() + 120  # 给用户 2 分钟操作时间
        last_log = 0.0
        while time.time() < deadline:
            time.sleep(3)
            now_ts = time.time()
            if now_ts - last_log >= 30:
                elapsed = int(now_ts - (deadline - 120))
                db.log_task("purchase", "running", target_email=email,
                            message=f"等待手动完成购买 ({elapsed}s/120s) — "
                                    "请在 MuMu 上: GPT App → Plus 订阅 → Google Play 支付")
                last_log = now_ts
            # 如果 token 已经被捕获, 说明用户已完成购买, 可以提前结束
            with db.get_session() as s:
                current_count = len(s.exec(select(CapturedToken)).all())
            if current_count > initial_count:
                db.log_task("purchase", "success", target_email=email,
                            message="检测到 token 已被捕获, 购买完成")
                return StepResult(True, "手动购买完成, token 已被 mitmproxy 拦截",
                                  stage="purchase")

        # 120 秒内未检测到 token — 仍然进入 wait_token 阶段 (那里有更长的超时)
        return StepResult(True,
                          "请在 MuMu 上完成 Plus 订阅购买 (后续 wait_token 阶段会等待 token). "
                          "GPT App → Plus 订阅 → Google Play 支付完成后, "
                          "mitmproxy 会自动拦截 token",
                          stage="purchase")

    except Exception as e:
        return StepResult(False, f"异常: {e}",
                          _screenshot(inst, f"purchase_{email}_err") if inst else None,
                          stage="purchase")


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


# ============================================================
# Phase B 阶段: 激活 (转移)
# ============================================================

@with_circuit("get_account_id", failure_threshold=3, cooldown_seconds=300)
def stage_get_account_id(email: str) -> StepResult:
    """用目标账号的 JWT 请求 OpenAI accounts/check 获取 account_id.

    这是转移的核心: 支付账号 A 的 token + 目标账号 B 的 account_id.
    必须用目标账号的 JWT (不是支付账号的), 才能拿到目标 account_id.
    """
    with db.get_session() as s:
        from .db import GoogleAccount
        acc = s.exec(select(GoogleAccount).where(GoogleAccount.email == email)).first()
        if not acc:
            return StepResult(False, "账号不存在", stage="get_account_id")

        # 优先使用 target_jwt (转移场景: 导入时指定的目标账号 JWT)
        # 其次使用 gpt_jwt (自激活场景: 同一个账号的 JWT)
        jwt_token = acc.target_jwt or acc.gpt_jwt

    if not jwt_token:
        return StepResult(False,
                          "缺少目标账号 JWT, 无法获取 account_id。"
                          "导入格式: email----password----jwt (jwt 为目标 GPT 账号的 Bearer token)",
                          stage="get_account_id")

    from ..revenuecat import get_account_id
    cfg = load_config()
    account_id = get_account_id(jwt_token, cfg)
    if account_id:
        _update_account_status(email, "got_account_id", gpt_account_id=account_id)
        # 如果 target_jwt 存在但 gpt_jwt 为空, 也把 target_jwt 写入 gpt_jwt 以便 verify 使用
        with db.get_session() as s:
            acc_obj = s.exec(select(GoogleAccount).where(GoogleAccount.email == email)).first()
            if acc_obj and not acc_obj.gpt_jwt and acc_obj.target_jwt:
                acc_obj.gpt_jwt = acc_obj.target_jwt
                s.add(acc_obj)
                s.commit()
        return StepResult(True, f"目标 account_id={account_id} (通过 JWT accounts/check 获取)", stage="get_account_id")

    return StepResult(False, "JWT accounts/check 未返回 account_id, 请检查 JWT 是否有效",
                      stage="get_account_id")


@with_circuit("activate", failure_threshold=3, cooldown_seconds=600)
def stage_activate(email: str) -> StepResult:
    """用捕获的 token + 目标 account_id 提交 RevenueCat 激活.

    token 来自 Phase A (支付账号的 Google Play 购买),
    account_id 来自 stage_get_account_id (目标账号的 JWT → accounts/check).
    两者独立, 实现"转移".
    """
    inst = _get_instance()
    if inst is None:
        return StepResult(False, "未检测到 MuMu", stage="activate")
    # 从缓存 token 取下一个未过期、未使用的 token, 提交给目标 account_id
    now_iso = datetime.now(_TZ).isoformat()
    with db.get_session() as s:
        from .db import GoogleAccount, CapturedToken
        acc = s.exec(select(GoogleAccount).where(GoogleAccount.email == email)).first()
        if not acc or not acc.gpt_account_id:
            return StepResult(False, "缺少 target account_id (需先完成 get_account_id 阶段)", stage="activate")
        tok = s.exec(
            select(CapturedToken)
            .where(CapturedToken.used == False)
            .where(CapturedToken.expires_at > now_iso)  # 过滤过期 token (与 gptplustest 一致)
            .order_by(CapturedToken.id)
        ).first()
        if not tok:
            return StepResult(False, "token 队列为空 (无未过期的可用 token)", stage="activate")
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
        return StepResult(True, "激活成功 (token 已转移到目标账号)", stage="activate")
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
    "ensure_prereqs": lambda email, cp: stage_ensure_prereqs(email),
    "play_login": lambda email, cp: stage_play_login(email, _get_password(email)),
    "gpt_open": lambda email, cp: stage_gpt_open(email),
    "purchase": lambda email, cp: stage_purchase(email),
    "wait_token": lambda email, cp: stage_wait_token(email),
    "get_account_id": lambda email, cp: stage_get_account_id(email),
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
