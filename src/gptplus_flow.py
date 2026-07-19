#!/usr/bin/env python3
"""
GPT Plus 模拟器订阅链路自动化工具（仅供学习）

链路概览:
    Google Play 支付 -> fetch_token -> RevenueCat /v1/receipts -> 绑定 account_id -> 开通 Plus

本工具实现:
    1. 自动检测 MuMu 模拟器（默认安装路径 + ADB 连接）
    2. 自动注入 mitmproxy CA 证书到 MuMu 系统 CA 存储
    3. 启动 mitmproxy 拦截 RevenueCat 请求, 提取并保存 fetch_token
    4. 通过 OpenAI accounts/check 获取目标账号 account_id
    5. 组装 RevenueCat 请求头 + 请求体 (token 拼接) 并提交激活

免责声明: 本工具仅供学习与研究使用, 不得用于任何违反服务条款或法律法规的场景。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from dataclasses import dataclass

# 用 import 的方式把模块内子组件挂进来
from .config import load_config
from .mumu_detect import detect_mumu, MuMuInstance
from .ca_inject import install_mitm_ca
from .mitm_runner import start_mitmproxy, stop_mitmproxy
from .token_capture import run_capture_until_token
from .token_queue import TokenQueue
from .revenuecat import (
    get_account_id,
    activate_plus,
    assemble_revenuecat_headers,
    assemble_revenuecat_body,
)


def cmd_status(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    inst = detect_mumu(cfg)
    if inst is None:
        print("[status] 未检测到运行中的 MuMu 模拟器")
        print("        请先启动 MuMu, 或通过 --adb / --serial 指定连接方式")
        return 1
    print(f"[status] MuMu 检测成功")
    print(f"  adb_path       : {inst.adb_path}")
    print(f"  serial         : {inst.serial}")
    print(f"  android_version: {inst.android_version}")
    print(f"  rooted         : {inst.rooted}")
    print(f"  proxy          : {inst.current_proxy or '(无)'}")
    print(f"  gpt_installed  : {inst.gpt_installed}")
    print(f"  google_accounts: {inst.google_accounts}")
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    inst = detect_mumu(cfg)
    if inst is None:
        print("[setup] 未检测到 MuMu")
        return 1
    print("[setup] 注入 mitmproxy CA 证书到 MuMu 系统 CA 存储 ...")
    if install_mitm_ca(inst, cfg):
        print("[setup] CA 注入完成")
    else:
        print("[setup] CA 注入失败")
        return 2
    return 0


def cmd_intercept(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    inst = detect_mumu(cfg)
    if inst is None:
        print("[intercept] 未检测到 MuMu")
        return 1
    print(f"[intercept] 启动 mitmproxy 卡片监听 {cfg.mitm_host}:{cfg.mitm_port} (上游 {cfg.upstream_proxy})")
    mitm_proc = start_mitmproxy(cfg, capture_addon_path=args.addon)
    if mitm_proc is None:
        print("[intercept] mitmproxy 启动失败, 请确认已 `pip install mitmproxy`")
        return 2
    try:
        queue = TokenQueue(cfg.token_queue_file)
        print("[intercept] 等待 RevenueCat POST /v1/receipts 请求 ...")
        print(f"[intercept] 在 MuMu 内打开 GPT app -> 升级 Plus -> 完成 Google Play 支付")
        print(f"[intercept] token 将自动入队: {cfg.token_queue_file}")
        token = run_capture_until_token(cfg, timeout=args.timeout)
        if token:
            idx = queue.enqueue(token.fetch_token, metadata={
                "original_app_user_id": token.app_user_id,
                "storefront": token.storefront,
            })
            print(f"[intercept] 捕获成功, token 队列索引 #{idx}")
            print(f"[intercept] fetch_token = {token.fetch_token[:32]}...")
            return 0
        else:
            print("[intercept] 超时, 未捕获到 token")
            return 3
    finally:
        stop_mitmproxy(mitm_proc, inst, cfg)


def cmd_get_account_id(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    account_id = get_account_id(args.jwt, cfg)
    if account_id:
        print(f"account_id: {account_id}")
        return 0
    return 1


def cmd_activate(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    ok = activate_plus(args.fetch_token, args.account_id, storefront=args.storefront, cfg=cfg)
    return 0 if ok else 1


def cmd_activate_from_queue(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    queue = TokenQueue(cfg.token_queue_file)
    item = queue.dequeue()
    if item is None:
        print("没有可用 token")
        return 1
    idx, record = item
    fetch_token = str(record["fetch_token"])
    print(f"使用队列 token #{idx}: {fetch_token[:24]}...")
    ok = activate_plus(fetch_token, args.account_id, storefront=args.storefront, cfg=cfg)
    if ok:
        queue.mark_used(idx, args.account_id)
        return 0
    return 1


def cmd_queue_status(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    queue = TokenQueue(cfg.token_queue_file)
    print(queue.status())
    return 0


def cmd_assemble(args: argparse.Namespace) -> int:
    """仅展示 token 拼接结果, 不发请求。用于学习原理。"""
    cfg = load_config(args.config)
    headers = assemble_revenuecat_headers(cfg, storefront=args.storefront)
    body = assemble_revenuecat_body(args.fetch_token, args.account_id)
    print("=== RevenueCat POST /v1/receipts Headers ===")
    for k, v in headers.items():
        print(f"  {k}: {v}")
    print("\n=== Request Body (token 拼接结果) ===")
    import json as _json
    print(_json.dumps(body, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="gptplus-flow",
        description="GPT Plus 模拟器订阅链路自动化工具（仅供学习）",
    )
    ap.add_argument("-c", "--config", default=None, help="配置文件路径 (默认读取 ./config.toml 或环境变量)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="检测 MuMu 模拟器状态").set_defaults(func=cmd_status)

    sub.add_parser("setup", help="注入 mitmproxy CA 证书到 MuMu").set_defaults(func=cmd_setup)

    p_int = sub.add_parser("intercept", help="启动 mitmproxy 拦截 RevenueCat 捕获 fetch_token")
    p_int.add_argument("--addon", default=None, help="mitmproxy addon 脚本路径 (默认使用内置 addon)")
    p_int.add_argument("--timeout", type=int, default=600, help="等待 token 超时秒数 (默认 600)")
    p_int.set_defaults(func=cmd_intercept)

    p_g = sub.add_parser("get-account-id", help="通过 OpenAI accounts/check 获取 account_id")
    p_g.add_argument("--jwt", required=True, help="目标 GPT 账号 Bearer JWT")
    p_g.set_defaults(func=cmd_get_account_id)

    p_a = sub.add_parser("activate", help="直接提交 fetch_token + account_id 激活 Plus")
    p_a.add_argument("--fetch-token", required=True)
    p_a.add_argument("--account-id", required=True)
    p_a.add_argument("--storefront", default="US")
    p_a.set_defaults(func=cmd_activate)

    p_aq = sub.add_parser("activate-from-queue", help="从 token 队列取下一个可用 token 激活")
    p_aq.add_argument("--account-id", required=True)
    p_aq.add_argument("--storefront", default="US")
    p_aq.set_defaults(func=cmd_activate_from_queue)

    sub.add_parser("queue-status", help="查看 token 队列状态").set_defaults(func=cmd_queue_status)

    p_as = sub.add_parser("assemble", help="仅展示 token 拼接结果 (学习用, 不发请求)")
    p_as.add_argument("--fetch-token", required=True)
    p_as.add_argument("--account-id", required=True)
    p_as.add_argument("--storefront", default="US")
    p_as.set_defaults(func=cmd_assemble)

    return ap


def main() -> None:
    ap = build_parser()
    args = ap.parse_args()
    rc = args.func(args)
    sys.exit(rc)


if __name__ == "__main__":
    main()
