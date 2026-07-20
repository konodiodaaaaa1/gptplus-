"""启动/停止 mitmproxy 进程并配置 MuMu 全局代理."""
from __future__ import annotations

import os
import subprocess
import time
from typing import Optional

from .config import Config
from .mumu_detect import MuMuInstance, _run_adb


def start_mitmproxy(cfg: Config, capture_addon_path: Optional[str] = None) -> Optional[subprocess.Popen]:
    mitmdump = _find_mitmdump()
    if not mitmdump:
        return None

    args: list[str] = []
    if capture_addon_path:
        args += ["-s", capture_addon_path]
    # upstream 模式: 链式到翻墙代理
    if cfg.upstream_proxy:
        args += ["--mode", f"upstream:{cfg.upstream_proxy}"]
    args += ["-p", str(cfg.mitm_port), "--no-http2"]
    if cfg.ignore_hosts_regex:
        args += ["--ignore-hosts", cfg.ignore_hosts_regex]

    log_dir = os.path.join(os.path.dirname(cfg.token_queue_file) or ".", "mitm_logs")
    os.makedirs(log_dir, exist_ok=True)
    # 行缓冲 (buffering=1): 让 [addon] INTERCEPTED 等日志实时写入文件, 便于排查
    out_log = open(os.path.join(log_dir, "mitm.out.log"), "w", encoding="utf-8", buffering=1)
    err_log = open(os.path.join(log_dir, "mitm.err.log"), "w", encoding="utf-8", buffering=1)

    # 把 DB / 队列文件路径以绝对路径传给 addon 子进程, 确保双写落到同一份 DB
    env = os.environ.copy()
    env["GPTPLUS_DB"] = os.path.abspath(env.get("GPTPLUS_DB", "gptplus.db"))
    env["TOKEN_QUEUE_FILE"] = os.path.abspath(cfg.token_queue_file)
    env["TOKEN_EXPIRY_HOURS"] = str(cfg.token_expiry_hours)

    proc = subprocess.Popen(
        [mitmdump, *args],
        stdout=out_log, stderr=err_log,
        env=env,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    time.sleep(3)
    if proc.poll() is not None:
        return None
    print(f"[mitm_runner] mitmproxy PID={proc.pid} 监听 {cfg.mitm_host}:{cfg.mitm_port}")
    return proc


def stop_mitmproxy(proc: Optional[subprocess.Popen], inst: Optional[MuMuInstance], cfg: Config) -> None:
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    # 还原 MuMu 代理为上游翻墙代理 (避免切断网络)
    if inst is not None:
        target = cfg.upstream_proxy.replace("http://", "").replace("https://", "") if cfg.upstream_proxy else ":0"
        _run_adb(inst.adb_path, inst.serial, ["shell", f"settings put global http_proxy {target}"])
        print(f"[mitm_runner] MuMu 代理已还原为: {target or '(无)'}")


def set_mumu_proxy_to_mitm(inst: MuMuInstance, cfg: Config) -> bool:
    """把 MuMu 全局代理指向 mitmproxy (需要本机局域网 IP)."""
    # 推断本机 IP: 取与 MuMu serial 同网段的 IPv4
    host_ip = _guess_host_ip(inst.serial)
    if not host_ip:
        print("[mitm_runner] 无法推断本机 IP, 请手动设置 MITM_HOST")
        return False
    target = f"{host_ip}:{cfg.mitm_port}"
    rc, _ = _run_adb(inst.adb_path, inst.serial, ["shell", f"settings put global http_proxy {target}"])
    if rc == 0:
        print(f"[mitm_runner] MuMu 代理已设置为 {target}")
    return rc == 0


def _find_mitmdump() -> Optional[str]:
    # 1) PATH
    from shutil import which
    p = which("mitmdump")
    if p:
        return p
    # 2) Python Scripts 目录
    for base in os.environ.get("PATH", "").split(os.pathsep):
        cand = os.path.join(base, "mitmdump.exe" if os.name == "nt" else "mitmdump")
        if os.path.exists(cand):
            return cand
    # 3) sys.executable 同目录
    import sys
    cand2 = os.path.join(os.path.dirname(sys.executable), "mitmdump.exe" if os.name == "nt" else "mitmdump")
    if os.path.exists(cand2):
        return cand2
    return None


def _guess_host_ip(mumu_serial: str) -> str:
    """从 MuMu serial (可能是 192.168.x.x:5555 或 127.0.0.1:5555) 推断本机 IP."""
    if mumu_serial.startswith("127.0.0.1") or mumu_serial.startswith("localhost"):
        return "127.0.0.1"
    try:
        import socket
        # 取本机任意 IPv4
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        return ""
