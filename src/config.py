"""配置加载: 优先级 环境变量 > config.toml > 内置默认值

所有"敏感/个人"配置都不硬编码在代码里, 而是从环境变量或 config.toml 读取。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib  # py311+
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]


REVENUECAT_API_KEY_DEFAULT = "goog_DPguJtknNxbQBStStwhWGRsghUw"  # GPT Android 公开 RevenueCat 公钥, 非个人配置
PRODUCT_ID_DEFAULT = "oai.chatgpt.plus"
ENTITLEMENT_ID_DEFAULT = "chatgpt_plus"
TOKEN_EXPIRY_HOURS_DEFAULT = 72


# MuMu 默认安装路径候选 (按优先级)
MUMU_ADB_CANDIDATES = [
    r"D:\Program Files\Netease\MuMuPlayer-12.0\nx_device\12.0\shell\adb.exe",
    r"C:\Program Files\Netease\MuMuPlayer-12.0\nx_device\12.0\shell\adb.exe",
    r"D:\Program Files\Netease\MuMuPlayer-12.0\shell\adb.exe",
    r"C:\Program Files\Netease\MuMuPlayer-12.0\shell\adb.exe",
]

MUMU_SERIAL_CANDIDATES = [
    "127.0.0.1:5555",
    "127.0.0.1:7555",
    "192.168.2.4:5555",  # 局域网 MuMu 默认
]


@dataclass
class Config:
    # MuMu
    adb_path: str = ""
    serial: str = ""
    mumu_adb_candidates: list[str] = field(default_factory=lambda: list(MUMU_ADB_CANDIDATES))
    mumu_serial_candidates: list[str] = field(default_factory=lambda: list(MUMU_SERIAL_CANDIDATES))

    # mitmproxy
    mitm_host: str = "0.0.0.0"
    mitm_port: int = 8888
    upstream_proxy: str = ""  # 例如 http://127.0.0.1:7890 (上游翻墙代理); 空表示直连
    ignore_hosts_regex: str = (
        r"googleapis\.com|google\.com|gstatic\.com|googleusercontent\.com|"
        r"googlevideo\.com|nie\.netease\.com|netease\.com|mumu"
    )
    mitm_addon_module: str = "gptplus_simulator_tools.addon"

    # 文件路径
    token_queue_file: str = "tokens.jsonl"
    mitm_ca_pem: str = ""  # 默认 ~/.mitmproxy/mitmproxy-ca-cert.pem

    # RevenueCat / OpenAI
    revenuecat_api_key: str = REVENUECAT_API_KEY_DEFAULT
    revenuecat_url: str = "https://api.revenuecat.com/v1/receipts"
    openai_account_check_url: str = "https://android.chat.openai.com/backend-api/accounts/check/v4-2023-04-27"
    product_id: str = PRODUCT_ID_DEFAULT
    entitlement_id: str = ENTITLEMENT_ID_DEFAULT
    token_expiry_hours: int = TOKEN_EXPIRY_HOURS_DEFAULT

    # RevenueCat 请求头模板 (模拟 GPT Android app)
    headers_template: dict[str, str] = field(default_factory=lambda: {
        "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 16; Pixel 9 Build/BP4A.260205.001)",
        "Connection": "Keep-Alive",
        "Accept-Encoding": "gzip",
        "Content-Type": "application/json",
        "X-Platform": "android",
        "X-Platform-Flavor": "native",
        "X-Platform-Version": "36",
        "X-Platform-Device": "Pixel 9",
        "X-Platform-Brand": "google",
        "X-Version": "9.22.1",
        "X-Preferred-Locales": "zh_CN",
        "X-Client-Locale": "zh-CN",
        "X-Client-Version": "1.2026.062",
        "X-Client-Bundle-ID": "com.openai.chatgpt",
        "X-Observer-Mode-Enabled": "false",
        "X-Custom-Entitlements-Computation": "true",
        "X-Storefront": "US",
        "X-Is-Debug-Build": "false",
        "X-Kotlin-Version": "2.3.10",
        "X-Is-Backgrounded": "false",
        "X-Billing-Client-Sdk-Version": "8.0.0",
        "X-RevenueCat-ETag": "",
    })


def _from_env(cfg: Config) -> Config:
    if v := os.environ.get("MUMU_ADB_PATH"):
        cfg.adb_path = v
    if v := os.environ.get("MUMU_SERIAL"):
        cfg.serial = v
    if v := os.environ.get("MITM_HOST"):
        cfg.mitm_host = v
    if v := os.environ.get("MITM_PORT"):
        cfg.mitm_port = int(v)
    if v := os.environ.get("UPSTREAM_PROXY"):
        cfg.upstream_proxy = v
    if v := os.environ.get("TOKEN_QUEUE_FILE"):
        cfg.token_queue_file = v
    if v := os.environ.get("REVENUECAT_API_KEY"):
        cfg.revenuecat_api_key = v
    if v := os.environ.get("OPENAI_JWT"):
        # 方便测试, 不强制
        pass
    return cfg


def _from_toml(cfg: Config, path: str) -> Config:
    if tomllib is None or not os.path.exists(path):
        return cfg
    with open(path, "rb") as f:
        data = tomllib.load(f)
    mumu = data.get("mumu", {})
    if v := mumu.get("adb_path"):
        cfg.adb_path = v
    if v := mumu.get("serial"):
        cfg.serial = v
    if v := mumu.get("adb_candidates"):
        cfg.mumu_adb_candidates = list(v)
    if v := mumu.get("serial_candidates"):
        cfg.mumu_serial_candidates = list(v)

    mitm = data.get("mitm", {})
    cfg.mitm_host = mitm.get("host", cfg.mitm_host)
    cfg.mitm_port = int(mitm.get("port", cfg.mitm_port))
    cfg.upstream_proxy = mitm.get("upstream_proxy", cfg.upstream_proxy)
    if v := mitm.get("ignore_hosts_regex"):
        cfg.ignore_hosts_regex = v

    api = data.get("api", {})
    cfg.revenuecat_api_key = api.get("revenuecat_api_key", cfg.revenuecat_api_key)
    cfg.revenuecat_url = api.get("revenuecat_url", cfg.revenuecat_url)
    cfg.openai_account_check_url = api.get("openai_account_check_url", cfg.openai_account_check_url)
    cfg.product_id = api.get("product_id", cfg.product_id)
    cfg.entitlement_id = api.get("entitlement_id", cfg.entitlement_id)

    files = data.get("files", {})
    cfg.token_queue_file = files.get("token_queue", cfg.token_queue_file)
    cfg.mitm_ca_pem = files.get("mitm_ca_pem", cfg.mitm_ca_pem)
    return cfg


def load_config(explicit_path: str | None = None) -> Config:
    cfg = Config()
    candidates = []
    if explicit_path:
        candidates.append(explicit_path)
    candidates.append("config.toml")
    for p in candidates:
        if os.path.exists(p):
            cfg = _from_toml(cfg, p)
            break
    cfg = _from_env(cfg)
    if not cfg.mitm_ca_pem:
        cfg.mitm_ca_pem = str(Path.home() / ".mitmproxy" / "mitmproxy-ca-cert.pem")
    return cfg
