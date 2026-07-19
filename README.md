# GPT Plus 模拟器订阅链路自动化工具

> **⚠️ 免责声明：本项目仅供学习与研究使用，不得用于任何违反服务条款或法律法规的场景。**
> 使用本项目所产生的一切后果由使用者自行承担。

## 项目背景

本项目用于在 MuMu 模拟器中学习研究 ChatGPT Android 应用的订阅链路结构，包括：

- RevenueCat 作为订阅中间层的工作机制
- Google Play 支付凭证（purchase token）与 OpenAI 账号绑定的关系
- MITM 代理在模拟器环境下的证书信任链
- Android 系统 CA 存储的运行时注入技术

通过自动化脚本复现完整的"支付 → 凭证获取 → 凭证提交 → 权益开通"链路形态，便于学习其设计原理与安全边界。

## 链路原理

```
Google Play 支付完成
        ↓
GPT App 获取 purchase token (fetch_token)
        ↓
POST https://api.revenuecat.com/v1/receipts
        {
          "fetch_token": "<Google Play 凭证>",
          "app_user_id": "<OpenAI account_id>",
          "product_ids": ["oai.chatgpt.plus"]
        }
        ↓
RevenueCat 验证凭证 + 绑定到指定 account_id
        ↓
OpenAI 后端读取 RevenueCat 权益 → 开通 Plus
```

关键点：`fetch_token` 与 `app_user_id` 是**相互独立**的两个参数。
- `fetch_token` 证明"有人付了钱"
- `app_user_id` 决定"给谁开通"

这种解耦设计是 RevenueCat 作为第三方订阅管理平台的固有形态。本项目通过 MITM 代理研究这一解耦关系在客户端的实现细节。

## 功能模块

| 命令 | 说明 |
|------|------|
| `status` | 自动检测 MuMu 模拟器状态（ADB / root / GPT app / Google 账号 / 代理） |
| `setup` | 注入 mitmproxy CA 证书到 MuMu 系统 CA 存储（tmpfs 覆盖挂载） |
| `intercept` | 启动 mitmproxy 拦截 RevenueCat 请求，捕获 `fetch_token` 入队 |
| `get-account-id` | 通过 OpenAI `accounts/check` 接口获取目标账号的 `account_id` |
| `activate` | 直接提交 `fetch_token + account_id` 激活 Plus（token 拼接） |
| `activate-from-queue` | 从 token 队列取下一个可用 token 自动激活 |
| `queue-status` | 查看 token 队列状态（可用 / 已用 / 过期） |
| `assemble` | 仅展示 token 拼接结果，不发请求（学习原理用） |

## 快速开始

### 环境准备

```bash
# Python 3.10+
pip install -r requirements.txt
# 或
pip install cryptography mitmproxy
```

需要安装：
- **MuMu 模拟器 12.0**（需开启 root，自带 Google Play 服务）
- **mitmproxy**（pip 安装即可）
- **ChatGPT Android app**（在 MuMu 内通过 Play Store 安装）

### 配置

```bash
cp config.example.toml config.toml
# 按需修改 config.toml, 或通过环境变量覆盖
```

关键配置项：

| 配置 | 说明 |
|------|------|
| `mumu.adb_path` | MuMu 自带 adb.exe 路径，留空自动检测 |
| `mumu.serial` | 设备序列号，留空自动枚举 |
| `mitm.upstream_proxy` | 上游翻墙代理（如 Clash），用于访问 OpenAI |
| `mitm.ignore_hosts_regex` | 不拦截的域名（Google Play 证书固定，必须透传） |

### 使用流程

```bash
# 1. 检测 MuMu 状态
python -m src.gptplus_flow status

# 2. 注入 CA 证书（MuMu 需 root）
python -m src.gptplus_flow setup

# 3. 启动拦截（在 MuMu 内完成 Google Play 支付，token 自动入队）
python -m src.gptplus_flow intercept --timeout 600

# 4. 获取目标账号 account_id（需目标账号的 JWT）
python -m src.gptplus_flow get-account-id --jwt "<BEARER_JWT>"

# 5. 查看队列
python -m src.gptplus_flow queue-status

# 6. 激活（直接指定 token 或从队列取）
python -m src.gptplus_flow activate --fetch-token "<TOKEN>" --account-id "<ACCOUNT_ID>"
# 或
python -m src.gptplus_flow activate-from-queue --account-id "<ACCOUNT_ID>"

# 学习用: 仅查看 token 拼接结果, 不发请求
python -m src.gptplus_flow assemble --fetch-token "<TOKEN>" --account-id "<ACCOUNT_ID>"
```

## 项目结构

```
gptplus-simulator/
├── src/
│   ├── gptplus_flow.py     # 主入口 + CLI
│   ├── config.py           # 配置加载（环境变量 > toml > 默认值）
│   ├── mumu_detect.py      # MuMu 自动检测（ADB / root / 应用 / 账号）
│   ├── ca_inject.py        # mitmproxy CA 证书注入（tmpfs 覆盖挂载）
│   ├── mitm_runner.py      # mitmproxy 进程管理 + MuMu 代理切换
│   ├── token_capture.py    # 等待并解析捕获到的 token
│   ├── token_queue.py      # 持久化 token 队列（过期 / 已用标记）
│   ├── revenuecat.py       # RevenueCat / OpenAI API 交互（token 拼接 + 提交）
│   └── addon.py            # mitmproxy addon（拦截 /v1/receipts, 保存 token, 阻断真实请求）
├── scripts/
│   └── mitmdump.bat        # mitmdump 启动脚本
├── config.example.toml     # 配置示例
├── pyproject.toml
├── requirements.txt
├── LICENSE
└── README.md
```


## WebUI (账号管理 + 流程编排 + sub2api 入库)

本项目附带 FastAPI WebUI, 提供批量账号导入、MuMu 自动化流程编排、token 队列管理、sub2api 导出。

### 启动

```bash
python -m src.web.app --host 127.0.0.1 --port 8080
```

浏览器访问 http://127.0.0.1:8080

### 功能面板

| 面板 | 说明 |
|------|------|
| MuMu 状态 | ADB/root/GPT app/Google 账号/代理/mitmproxy 实时状态 |
| 批量导入 | 每行一条 `email----password` 或 `email:password` 导入已有 Google 账号 |
| 账号管理 | 状态列表 (pending/play_logged_in/gpt_logged_in/subscribed/failed), 一键单跑/批量跑 |
| Token 队列 | mitmproxy 捕获的 fetch_token, 支持取下一个激活或手动指定 |
| sub2api 导出 | 一键导出已激活 Plus 账号 JSON / 配置片段 |
| 任务日志 | 实时滚动显示任务执行 |

### API 路由

| Method | Path | 说明 |
|--------|------|------|
| GET | `/` | WebUI 首页 |
| GET | `/api/status` | MuMu 状态 |
| GET | `/api/accounts` | 列出账号 |
| POST | `/api/accounts/import` | 批量导入 |
| DELETE | `/api/accounts/{email}` | 删除账号 |
| POST | `/api/accounts/{email}/pipeline` | 跑完整流程 (Play 登录 -> GPT 登录) |
| GET | `/api/tokens` | token 队列 |
| POST | `/api/tokens/activate` | 指定 token + account_id 激活 |
| POST | `/api/tokens/activate-next` | 取下一个 token 激活 |
| POST | `/api/intercept/start` | 启动 mitmproxy |
| POST | `/api/intercept/stop` | 停止 mitmproxy |
| GET | `/api/logs` | 任务日志 |
| GET | `/api/sub2api/export` | 导出已激活账号 |
| GET | `/api/sub2api/config` | sub2api 配置片段 |

### 关于"自动化 Gmail 注册"

本工具**不提供**自动注册 Gmail 账号的功能, 原因: (1) Gmail 注册有 hCaptcha、手机验证、设备指纹风控, 无稳定绕过方案; (2) 批量注册违反 Google 服务条款。

WebUI 的批量导入面向**用户已合法拥有的 Google 账号**, 用于在 MuMu 中自动化完成 Play Store 登录、GPT 登录、订阅流程对接, 并把结果 (account_id / JWT / 订阅状态) 入库供 sub2api 调用。

### 数据库

SQLite (默认 `gptplus.db`, 环境变量 `GPTPLUS_DB` 可改), 三张表:

- `googleaccount`: 导入账号 + GPT account_id/JWT + Plus 状态
- `capturedtoken`: mitmproxy 捕获的 fetch_token
- `tasklog`: 任务日志

## 技术细节

### MuMu 自动检测

候选 adb 路径 + 候选 serial，逐个尝试：
1. 在默认安装路径查找 `adb.exe`
2. `adb devices` 枚举已连接设备
3. 对每个设备探测 `ro.product.brand` / `ro.product.model` 判断是否 MuMu
4. 探测 Android 版本、root 状态、GPT app、Google 账号、当前代理

### CA 证书注入

Android 12+ 的 `/system` 是只读 ext4 分区，无法直接写入。本项目采用 **tmpfs 覆盖挂载** `/system/etc/security/cacerts`：

1. 备份原系统 CA 到 `/data/local/tmp/cacerts_backup/`
2. 计算 mitmproxy CA 的 OpenSSL `subject_hash_old`，命名为 `<hash>.0`
3. 将 mitmproxy CA 加入备份目录
4. `mount -t tmpfs tmpfs /system/etc/security/cacerts`
5. 把备份目录内容复制回 tmpfs

**重启后 tmpfs 消失，证书自动还原**（需重新运行 `setup`）。

### Google Play 证书固定

Google Play Services 对 `play-fe.googleapis.com` 等域做了证书固定（certificate pinning），不信任系统 CA。本项目通过 mitmproxy 的 `--ignore-hosts` 透传这些域名到上游代理，避免拦截失败导致 Play 支付流程异常。

### Token 捕获与阻断

mitmproxy addon 拦截 `POST api.revenuecat.com/v1/receipts`：
1. 解析请求体，提取 `fetch_token` 和 `app_user_id`
2. 保存到 `tokens.jsonl`（带 72 小时过期标记）
3. **阻断真实请求**，返回假 200 响应

阻断设计的目的：让支付账号本身不被消费权益，token 在 72 小时 Google Play 自动退款窗口内可被研究其转移特性。

### Token 拼接

```python
body = {
    "fetch_token": "<Google Play 凭证>",
    "product_ids": ["oai.chatgpt.plus"],
    "platform_product_ids": [{"product_id": "oai.chatgpt.plus"}],
    "app_user_id": "<OpenAI account_id>",  # ← 决定开通目标
    "is_restore": False,
    "observer_mode": False,
    "purchase_completed_by": "revenuecat",
    "initiation_source": "unsynced_active_purchases",
    "sdk_originated": False,
    "payload_version": 1,
}
```

配合 GPT Android app 实际发送的请求头（`X-Platform: android`、`Authorization: Bearer <RevenueCat公钥>` 等）一起提交。

## 注意事项

- **本项目仅用于学习研究**。在实际环境中使用可能违反 OpenAI / Google Play / RevenueCat 的服务条款。
- **Google 账号需正常可用**。被封禁的账号无法完成 Google Play 支付，自然无法获取 `fetch_token`。
- **token 有 72 小时有效期**。Google Play 对未 Acknowledge 的支付会自动退款。
- **每次 MuMu 重启需重新 `setup`**（tmpfs 证书会丢失）。
- **不影响日常使用**：`intercept` 结束后会自动还原 MuMu 代理到上游翻墙代理。

## 已知限制

- Google Play Services 的证书固定无法绕过（设计如此，本项目选择透传而非对抗）
- 需要一个未被风控的 Google 账号完成支付
- 目标 GPT 账号的 JWT 需要通过其他合法途径获取（本工具不提供获取方式）

## License

MIT — 见 [LICENSE](LICENSE)
