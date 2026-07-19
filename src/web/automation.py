"""MuMu UI 自动化编排: 通过 ADB input 模拟点击/输入.

注意: 不同 MuMu 版本/分辨率下 UI 元素坐标可能不同, 本模块采用
uiautomator dump + 文本匹配的方式定位元素, 对坐标依赖最小化。
具体应用流程(Play Store 登录 / GPT 登录)的实现是框架性的, 需要
调用方根据实际 UI 调整文本匹配规则。
"""
from __future__ import annotations

import re
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Optional

from . import db
from ..config import load_config
from ..mumu_detect import detect_mumu, MuMuInstance, _run_adb


@dataclass
class StepResult:
    ok: bool
    message: str
    screenshot_path: Optional[str] = None


def _adb(inst: MuMuInstance, args: list[str], timeout: int = 15) -> tuple[int, str]:
    return _run_adb(inst.adb_path, inst.serial, args, timeout=timeout)


def _tap(inst: MuMuInstance, x: int, y: int) -> None:
    _adb(inst, ["shell", "input", "tap", str(x), str(y)])


def _input_text(inst: MuMuInstance, text: str) -> None:
    # 用 %s 转义空格等特殊字符
    safe = text.replace(" ", "%s").replace("&", "\\&").replace("<", "\\<").replace(">", "\\>")
    _adb(inst, ["shell", "input", "text", safe])


def _key(inst: MuMuInstance, key: str) -> None:
    _adb(inst, ["shell", "input", "keyevent", key])


def _uiautomator_dump(inst: MuMuInstance) -> str:
    """dump 当前 UI 层次结构, 返回 XML 字符串."""
    _adb(inst, ["shell", "uiautomator", "dump", "/sdcard/ui.xml"], timeout=10)
    rc, out = _adb(inst, ["shell", "cat", "/sdcard/ui.xml"])
    return out if rc == 0 else ""


def _find_element_by_text(xml: str, text_pattern: str) -> Optional[tuple[int, int, int, int]]:
    """在 UI XML 中查找包含指定文本的元素, 返回其 bounds (left, top, right, bottom)."""
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
    """轮询 UI 直到出现匹配文本的元素, 返回 bounds 或 None."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        xml = _uiautomator_dump(inst)
        b = _find_element_by_text(xml, pattern)
        if b:
            return b
        time.sleep(interval)
    return None


def _screenshot(inst: MuMuInstance, name: str) -> str:
    """截图保存到 screenshots/, 返回路径."""
    import os
    d = os.path.join("screenshots")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{name}.png")
    _adb(inst, ["shell", "screencap", "-p", "/sdcard/shot.png"])
    _adb(inst, ["pull", "/sdcard/shot.png", path])
    return path


# ============================================================
# 高层自动化流程
# ============================================================

def _get_instance() -> Optional[MuMuInstance]:
    cfg = load_config()
    return detect_mumu(cfg)


def play_store_login(email: str, password: str) -> StepResult:
    """在 MuMu 内自动化 Play Store 添加 Google 账号流程.

    流程 (会因 Android 版本略有差异):
      1. 打开 Settings -> Accounts -> Add account -> Google
      2. 输入 email -> Next
      3. 输入 password -> Next
      4. 处理可能的二次验证 (本框架只提示, 不自动绕过)
    """
    inst = _get_instance()
    if inst is None:
        return StepResult(False, "未检测到 MuMu")
    db.log_task("play_login", "running", target_email=email)

    try:
        # 1. 打开账号设置
        _adb(inst, ["shell", "am", "start", "-a", "android.settings.ADD_ACCOUNT_SETTINGS"])
        time.sleep(2)
        b = _wait_for_text(inst, r"Google", timeout=10)
        if not b:
            return StepResult(False, "未找到 Google 账号选项", _screenshot(inst, f"play_{email}_step1"))
        _tap(inst, *_tap_center(b))
        time.sleep(3)

        # 2. 输入邮箱
        b = _wait_for_text(inst, r"邮箱|email|Email", timeout=15)
        if not b:
            return StepResult(False, "未找到邮箱输入框", _screenshot(inst, f"play_{email}_step2"))
        _tap(inst, *_tap_center(b))
        time.sleep(1)
        _input_text(inst, email)
        _key(inst, "KEYCODE_ENTER")
        time.sleep(3)

        # 3. 输入密码
        b = _wait_for_text(inst, r"密码|password|Password", timeout=15)
        if not b:
            return StepResult(False, "未找到密码输入框 (可能需要二次验证)", _screenshot(inst, f"play_{email}_step3"))
        _tap(inst, *_tap_center(b))
        time.sleep(1)
        _input_text(inst, password)
        _key(inst, "KEYCODE_ENTER")
        time.sleep(5)

        # 4. 检查结果
        xml = _uiautomator_dump(inst)
        if re.search(r"无法登录|wrong|incorrect|错误", xml, re.I):
            return StepResult(False, "登录失败: 凭证错误或触发风控", _screenshot(inst, f"play_{email}_fail"))
        # 如果出现二次验证提示, 截图返回
        if re.search(r"验证|verify|2-step|两步", xml, re.I):
            return StepResult(False, "需要二次验证, 请手动完成", _screenshot(inst, f"play_{email}_2fa"))

        with db.get_session() as s:
            from .db import GoogleAccount
            acc = s.exec(s.select(GoogleAccount).where(GoogleAccount.email == email)).first()
            if acc:
                acc.status = "play_logged_in"
                acc.updated_at = __import__("datetime").datetime.now(__import__("datetime").timezone(__import__("datetime").timedelta(hours=8))).isoformat()
                s.add(acc)
                s.commit()
        db.log_task("play_login", "success", target_email=email)
        return StepResult(True, "Play Store 登录成功")
    except Exception as e:
        db.log_task("play_login", "failed", message=str(e), target_email=email)
        return StepResult(False, f"异常: {e}", _screenshot(inst, f"play_{email}_err"))


def gpt_app_login(email: str) -> StepResult:
    """打开 GPT app, 触发登录流程.

    GPT app 登录走 Auth0/OAuth 浏览器跳转, 完整自动化复杂度高。
    本函数负责打开 app + 进入登录页, 实际 OAuth 输入需手动或额外脚本。
    成功后从 mitmproxy 流量或缓存提取 JWT 与 account_id。
    """
    inst = _get_instance()
    if inst is None:
        return StepResult(False, "未检测到 MuMu")
    db.log_task("gpt_login", "running", target_email=email)
    try:
        _adb(inst, ["shell", "am", "start", "-n", "com.openai.chatgpt/.MainActivity"])
        time.sleep(5)
        # 检查是否已登录 (通过 RevenueCat preferences 缓存的 account_id)
        rc, out = _adb(inst, ["shell", "su -c 'cat /data/data/com.openai.chatgpt/shared_prefs/com_revenuecat_purchases_preferences.xml'"])
        import re
        m = re.search(r'\.new">([0-9a-f-]{36})<', out)
        if m:
            account_id = m.group(1)
            with db.get_session() as s:
                from .db import GoogleAccount
                acc = s.exec(s.select(GoogleAccount).where(GoogleAccount.email == email)).first()
                if acc:
                    acc.gpt_account_id = account_id
                    acc.status = "gpt_logged_in"
                    s.add(acc)
                    s.commit()
            db.log_task("gpt_login", "success", message=f"account_id={account_id}", target_email=email)
            return StepResult(True, f"GPT 已登录, account_id={account_id}")
        # 否则提示需要手动登录
        db.log_task("gpt_login", "failed", message="未检测到 account_id, 需手动完成 OAuth 登录", target_email=email)
        return StepResult(False, "GPT app 需手动完成 OAuth 登录", _screenshot(inst, f"gpt_{email}"))
    except Exception as e:
        db.log_task("gpt_login", "failed", message=str(e), target_email=email)
        return StepResult(False, f"异常: {e}")


def run_full_pipeline(email: str) -> StepResult:
    """对一个账号跑完整链路: Play 登录 -> GPT 登录 -> 等待订阅捕获.

    订阅捕获本身由 mitmproxy addon 在后台完成 (需要在 WebUI 启动拦截)。
    本函数编排前两步, 第三步等待 token 入队。
    """
    with db.get_session() as s:
        from .db import GoogleAccount
        acc = s.exec(s.select(GoogleAccount).where(GoogleAccount.email == email)).first()
        if not acc:
            return StepResult(False, f"账号 {email} 不在数据库中")
        pwd = acc.password

    r1 = play_store_login(email, pwd)
    if not r1.ok:
        return r1
    r2 = gpt_app_login(email)
    if not r2.ok:
        return r2
    # 第三步: 等待 token 捕获 (订阅触发需用户在 app 内点升级)
    return StepResult(True, "前两步完成, 请在 GPT app 内点击升级 Plus, token 会自动入队")
