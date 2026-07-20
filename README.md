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
│   ├── __init__.py
│   ├── gptplus_flow.py     # 主入口 + CLI
│   ├── config.py           # 配置加载（环境变量 > toml > 默认值）
│   ├── mumu_detect.py      # MuMu 自动检测（ADB / root / 应用 / 账号）
│   ├── ca_inject.py        # mitmproxy CA 证书注入（tmpfs 覆盖挂载）
│   ├── mitm_runner.py      # mitmproxy 进程管理 + MuMu 代理切换
│   ├── token_capture.py    # 等待并解析捕获到的 token
│   ├── token_queue.py      # 持久化 token 队列（过期 / 已用标记）
│   ├── revenuecat.py       # RevenueCat / OpenAI API 交互（token 拼接 + 提交）
│   ├── addon.py            # mitmproxy addon（拦截 /v1/receipts, 保存 token, 阻断真实请求）
│   └── web/
│       ├── app.py          # FastAPI WebUI 后端
│       ├── automation.py   # MuMu UI 自动化编排 + 断点续跑
│       ├── db.py           # SQLite 数据模型
│       ├── protection.py   # 熔断器 / 断点 / 重试退避
│       └── static/
│           ├── index.html  # 单页前端
│           └── style.css   # 前端样式
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
| GET | `/api/circuits` | 熔断器状态 |
| POST | `/api/circuits/{name}/reset` | 重置单个熔断器 |
| POST | `/api/circuits/reset-all` | 重置全部熔断器 |
| GET | `/api/checkpoints` | 所有账号断点 |
| GET | `/api/checkpoints/{email}` | 单账号断点 |
| POST | `/api/checkpoints/{email}/reset` | 重置单账号断点 |
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


## 测试覆盖度说明

> 为避免误导, 这里如实标注各部分的真实测试状态。本项目的链路较长, 部分环节依赖外部账号与支付, 未能完成端到端样本验证。
> 下方标记 `【2026-07-20 实测】` 的项为当天在真机 MuMu 12 + Clash Verge + mitmproxy 环境下真实跑通的验证。

### 已验证通过 (有测试样本)

| 模块 | 验证方式 | 结果 |
|------|----------|------|
| MuMu 自动检测 (ADB / root / GPT app / Google 账号 / 代理) | 真机 MuMu 12 + ADB | ✅ 全部字段正确返回 |
| mitmproxy CA 注入 (tmpfs 覆盖挂载) | MuMu root + Android 12 | ✅ 注入成功, RevenueCat TLS 拦截生效 |
| mitmproxy 拦截 RevenueCat `/v1/subscribers/` | 真实 GPT app 流量 | ✅ 拦截 + 解密成功 |
| mitmproxy addon 捕获 `fetch_token` 逻辑 | 单元 + mock 流量 | ✅ 入队 + 阻断 + 假 200 |
| RevenueCat `assemble_revenuecat_headers / body` (token 拼接) | 字段比对真实抓包 | ✅ 字段完全一致 |
| `mumu_mock_subscription_flow.py demo` (mock 端到端) | 5 个测试场景 | ✅ 全部通过 (200/409/409) |
| WebUI 后端 API 路由 + 首页 | uvicorn 冒烟测试 | ✅ 全部 200 (含新增 /api/setup, DELETE /api/logs) |
| WebUI 前端单页 | 浏览器加载 | ✅ 正常渲染 (含注入CA/清空日志按钮, 5s 轮询日志) |
| 熔断器 (CircuitBreaker) | 离线场景 3 次失败触发 | ✅ OPEN + 冷却 + reset |
| 断点保护 (CheckpointStore) | 中断 + resume 续跑 | ✅ 断点持久化 + 正确恢复 |
| `detect_mumu` 超时保护 | MuMu 离线场景 | ✅ 8 秒内返回 None |
| **WebUI `/api/setup` 一键注入 CA** `【2026-07-20 实测】` | 真机 MuMu root, POST /api/setup | ✅ 返回 `installed:true`, setup success 日志入 DB |
| **addon token 双写 (tokens.jsonl + SQLite capturedtoken)** `【2026-07-20 实测】` | MOCK token 经 mitm 代理发送 | ✅ 文件 + DB 同时落入, 字段一致, 幂等去重生效 |
| **addon 阻断真实请求 + 假 200** `【2026-07-20 实测】` | 修复 `Response.make` 后, MOCK token 返回假 200 | ✅ 真实 RevenueCat 未收到请求, token 不被消费 |
| **mitm → Clash upstream 链式出口** `【2026-07-20 实测】` | mitm 8888 经 Clash 7890 出口测 | ✅ revenuecat 200 / openai 401 / google 204 全通 |
| **config.toml 真实加载 (tomli fallback)** `【2026-07-20 实测】` | Python 3.10 + tomli, load_config 验证 | ✅ upstream_proxy 正确加载为 http://127.0.0.1:7890 |
| **mitm 日志行缓冲实时输出** `【2026-07-20 实测】` | mitm.out.log 监控 | ✅ `[addon] INTERCEPTED` / `BLOCKED` 实时可见 |
| **`stage_wait_token` 实时进度日志** `【2026-07-20 实测】` | pipeline 运行中查 /api/logs | ✅ 每 15s 写一条 `等待 token 中 (Ns/600s), 队列 M 条` |
| **pipeline 断点续跑 (resume=true)** `【2026-07-20 实测】` | 设断点 wait_token, 启动 pipeline | ✅ 从 wait_token 开始, checkpoint store + mtime reload 生效 |
| **activate 代码路径完整** `【2026-07-20 实测】` | MOCK token 经 pipeline activate 阶段 | ✅ 代码路径通, MOCK token 被真实 RevenueCat 拒绝非代码问题 |
| **account_id 从 device 读取** `【2026-07-20 实测】` | 读 MuMu RevenueCat prefs XML | ✅ 真实读到 `3dd94892-8498-4cdc-bf0f-48a0b6ad089f` |
| **sub2api 导出端点格式** `【2026-07-20 实测】` | /api/sub2api/export + /api/sub2api/config | ✅ 产出正确格式 (email/account_id/jwt/plus_expires/storefront) |
| **WebUI 实时日志轮询** `【2026-07-20 实测】` | 前端 5s 轮询 /api/logs | ✅ tasklog 表实时写入, 前端即时显示 |

### 未能完成样本验证 (后续需补)

| 模块 | 未验证原因 | 风险点 |
|------|------------|--------|
| **Google Play 支付 → `fetch_token` 真实捕获** | 测试用 Google 账号被封禁/无支付方式, 无法完成支付 | mitmproxy addon 逻辑已用 MOCK token 验证双写+阻断, 但未走过真实支付流量 |
| **`activate` 真实激活 Plus** | 缺少有效 `fetch_token` 样本 | RevenueCat 请求体字段已比对真实抓包, 代码路径已通, 但未提交过真实 token |
| **Pipeline 端到端真实跑通 (Play 登录 → GPT 登录 → 捕获 → 激活)** | 缺有效 Google 账号完成支付, 链路在 wait_token → activate 之间断 | 各阶段单元逻辑 + 串联调度已验证 (MOCK token 走通到 activate), 仅缺真实 token 收口 |
| **`stage_play_login` UI 自动化** | MuMu 在线时账号已登录, 再走 ADD_ACCOUNT 流程多余 | 不同 MuMu 版本/分辨率下 UI 文本可能不同, 需按实际调整 `_wait_for_text` 匹配规则 |
| **`stage_gpt_login` OAuth 自动化** | GPT 登录走 Auth0 浏览器跳转, 自动化复杂度高, 当前仅检测已登录态 | 完整 OAuth 输入流程未实现, 当前需手动完成登录后由系统检测 account_id |
| **`stage_verify` 独立验证** | 依赖 `gpt_jwt`, 未在真实账号上跑 | 逻辑已实现, 待样本验证 |
| **sub2api 导出与真实 sub2api 项目对接** | 缺少已真实激活 Plus 的账号 | 字段格式已验证导出正确, 实际对接需按目标 sub2api 项目调整 |

### 后续验证计划

1. 准备一个未被封禁、有支付方式的 Google 账号
2. 在 MuMu 内完成真实 Plus 支付, 验证 `fetch_token` 被 addon 捕获 (双写文件 + DB)
3. 用捕获的 token + 目标 GPT 账号 `account_id` 跑 `activate`, 验证 Plus 开通
4. 跑通完整 pipeline, 验证断点/熔断在真实流程中的行为
5. 验证 sub2api 导出字段与目标 sub2api 项目对接

### 已知限制

### 已知限制

- Google Play Services 对 `play-fe.googleapis.com` 做了证书固定, 本项目选择 `--ignore-hosts` 透传而非对抗
- `stage_play_login` 的 UI 文本匹配在不同 Android 版本可能需要调整
- `stage_gpt_login` 的 OAuth 自动化未完整实现, 当前需手动完成登录后由系统检测 account_id

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
