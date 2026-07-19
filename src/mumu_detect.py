"""自动检测 MuMu 模拟器状态。

检测策略:
1. 在默认安装路径候选里找 adb.exe
2. 通过 `adb devices` 列出已连接设备
3. 对每个设备探测: Android 版本 / root / GPT 应用 / Google 账号 / 当前代理
4. 返回第一个看起来像 MuMu 的实例
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Optional

from .config import Config


@dataclass
class MuMuInstance:
    adb_path: str
    serial: str
    android_version: str = ""
    rooted: bool = False
    gpt_installed: bool = False
    google_accounts: list[str] = field(default_factory=list)
    current_proxy: str = ""
    model: str = ""


def _run_adb(adb: str, serial: str, args: list[str], timeout: int = 10) -> tuple[int, str]:
    cmd = [adb, "-s", serial, *args]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except FileNotFoundError:
        return 127, f"adb not found: {adb}"
    except subprocess.TimeoutExpired:
        return 124, "timeout"


def _find_adb_binary(cfg: Config) -> str | None:
    if cfg.adb_path and os.path.exists(cfg.adb_path):
        return cfg.adb_path
    for cand in cfg.mumu_adb_candidates:
        if os.path.exists(cand):
            return cand
    # 兜底: 系统 PATH 里的 adb
    p = shutil.which("adb")
    return p


def _list_devices(adb: str) -> list[str]:
    try:
        r = subprocess.run([adb, "devices"], capture_output=True, text=True, timeout=8)
    except Exception:
        return []
    serials = []
    for line in r.stdout.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            serials.append(parts[0])
    return serials


def _looks_like_mumu(adb: str, serial: str) -> bool:
    # 厂商/型号匹配
    rc, out = _run_adb(adb, serial, ["shell", "getprop ro.product.brand; getprop ro.product.manufacturer; getprop ro.product.model"])
    text = out.lower()
    if any(k in text for k in ("mumu", "netease", "nemu")):
        return True
    # 也可以通过存在 /system/bin/nemu 这类标志判断
    rc2, out2 = _run_adb(adb, serial, ["shell", "ls /system/bin/nemu* 2>/dev/null; ls /system/lib64/libnemu* 2>/dev/null"])
    if "nemu" in out2.lower():
        return True
    # 默认放行: 用户也可以手动 --serial 指定
    return True


def _probe_device(adb: str, serial: str) -> MuMuInstance:
    inst = MuMuInstance(adb_path=adb, serial=serial)

    # 可达性检查: 设备不可达时 _run_adb 会返回 "device not found" 字样
    rc, out = _run_adb(adb, serial, ["shell", "getprop ro.build.version.release"])
    if rc != 0 or not out.strip() or "not found" in out.lower() or "error" in out.lower()[:20]:
        return inst
    inst.android_version = out.strip().splitlines()[0]

    _, out = _run_adb(adb, serial, ["shell", "getprop ro.product.model"])
    line = out.strip().splitlines()[0] if out.strip() else ""
    inst.model = line if "not found" not in line.lower() else ""

    rc, _ = _run_adb(adb, serial, ["shell", "su -c id"])
    inst.rooted = (rc == 0)

    _, out = _run_adb(adb, serial, ["shell", "pm list packages com.openai.chatgpt"])
    inst.gpt_installed = "com.openai.chatgpt" in out

    _, out = _run_adb(adb, serial, ["shell", "dumpsys account"])
    inst.google_accounts = re.findall(r"Account \{name=([^,]+@[^,]+), type=com\.google\}", out)

    _, out = _run_adb(adb, serial, ["shell", "settings get global http_proxy"])
    proxy = out.strip().splitlines()[0] if out.strip() else ""
    if "not found" not in proxy.lower():
        inst.current_proxy = proxy
    return inst


def detect_mumu(cfg: Config) -> Optional[MuMuInstance]:
    """返回第一个检测到的 MuMu 实例, 找不到返回 None.

    整体超时保护: 探测过程最多 8 秒, 超时返回 None, 避免 WebUI /api/status 卡死.
    """
    import threading as _th
    box: list = []

    def _detect() -> None:
        adb = _find_adb_binary(cfg)
        if not adb:
            return
        if cfg.serial:
            inst = _probe_device(adb, cfg.serial)
            if inst.android_version:
                box.append(inst)
                return
        for serial in _list_devices(adb):
            if not _looks_like_mumu(adb, serial):
                continue
            inst = _probe_device(adb, serial)
            if inst.android_version:
                box.append(inst)
                return
        for serial in cfg.mumu_serial_candidates[:2]:
            if ":" in serial:
                try:
                    subprocess.run([adb, "connect", serial], capture_output=True, text=True, timeout=3)
                except Exception:
                    pass
            inst = _probe_device(adb, serial)
            if inst.android_version:
                box.append(inst)
                return

    t = _th.Thread(target=_detect, daemon=True)
    t.start()
    t.join(timeout=8)
    return box[0] if box else None
