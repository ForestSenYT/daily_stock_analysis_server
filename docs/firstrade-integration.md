# Firstrade 只读 Broker 集成

> Status: **Phase 1 — read-only** (this build).
> Master flag: `BROKER_FIRSTRADE_ENABLED` (default `false`).

## 功能简介

通过非官方 [`firstrade`](https://github.com/MaxxRK/firstrade-api) PyPI 包把 Firstrade 账户、余额、持仓、订单、交易历史快照同步到本地 SQLite，让 Agent 在做组合分析与个股建议时基于真实持仓上下文。

| 模块 | 在做什么 | 不做什么 |
| --- | --- | --- |
| `src/brokers/firstrade/client.py` | `from firstrade import account` 登录 / 拉数据 / 脱敏 | 不 import `firstrade.order`；不下单；不撤单 |
| `src/services/firstrade_sync_service.py` | 串起 client + repo + threading.Lock | 不暴露 client / session 给上层 |
| `src/repositories/broker_snapshot_repo.py` | 写 `broker_sync_runs` + `broker_snapshots` 两张表 | 写入前再做一次 `redact_sensitive_payload` |
| `api/v1/endpoints/broker.py` | 9 个端点（status / login / verify / sync / accounts / positions / orders / transactions / snapshot） | 不暴露下单 / 撤单端点 |
| `src/agent/tools/broker_tools.py` | `get_live_broker_portfolio_snapshot` — 读本地快照给 LLM | Agent 永远不登录 / 不同步 / 不下单 |
| `apps/dsa-web/src/components/portfolio/FirstradeSyncPanel.tsx` | Portfolio 页面同步面板 | 没有交易按钮 / 不在浏览器存储凭证 |

## ⚠️ 风险说明（务必先读）

1. **使用非官方 reverse-engineered API**：MaxxRK/firstrade-api 是 reverse-engineered，Firstrade 网站任何变动都可能让登录 / 数据接口失效。本项目固定 `firstrade==0.0.38`；若失效要么升级版本，要么停用集成。
2. **不构成投资建议**：Agent 基于本地脱敏快照做研究分析，不对真实交易决策负责。
3. **当前版本严格只读**：永远不会下单、撤单、提交期权交易。任何宣称"已下单"的输出都是 LLM 幻觉，应忽略。
4. **MFA session 在 Cloud Run 实例回收后会失效**：单实例进程持有 `FTSession`；如果 Cloud Run 在 `login()` 与 `verify_mfa(code)` 之间回收实例，前端会收到 HTTP 409 `session_lost`，需要重登录。生产部署建议设 `--min-instances=1`。

## 安装

```bash
# Firstrade 集成的可选依赖（不会污染 Cloud Run 主镜像）
pip install -r requirements-broker.txt
```

`requirements-broker.txt` 当前内容只有 `firstrade==0.0.38`。基础 `requirements.txt` 不含 `firstrade`，所以默认部署不会引入它。

## 配置

完整环境变量（默认值见 `.env.example` 的 Firstrade 段）：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `BROKER_FIRSTRADE_ENABLED` | `false` | 总开关。`false` 时所有端点返回 `not_enabled`，Agent 工具不注册。 |
| `BROKER_FIRSTRADE_READ_ONLY` | `true` | 永远应当为 `true`；额外防御层。 |
| `BROKER_FIRSTRADE_TRADING_ENABLED` | `false` | **即使为 `true` 也无任何交易能力**。本项目永不实现 place/cancel order。 |
| `BROKER_FIRSTRADE_USERNAME` / `_PASSWORD` | — | Firstrade 登录凭证。**不要提交真实值到 git。** |
| `BROKER_FIRSTRADE_PIN` | — | 部分账户的登录 PIN。 |
| `BROKER_FIRSTRADE_EMAIL` / `_PHONE` | — | 触发邮箱 / 短信验证码（手动二次输入）。 |
| `BROKER_FIRSTRADE_MFA_SECRET` | — | TOTP base32 secret（自动生成验证码，跳过手动二次输入）。 |
| `BROKER_FIRSTRADE_PROFILE_PATH` | `./data/broker_sessions/firstrade` | Session cookie 持久化目录。Cloud Run 上建议指向 GCS 挂载点 `/mnt/persistent/data/broker_sessions/firstrade`。 |
| `BROKER_FIRSTRADE_SAVE_SESSION` | `true` | 是否把 session cookie 写到磁盘。 |
| `BROKER_FIRSTRADE_SYNC_INTERVAL_SECONDS` | `60` | clamp 到 `[30, 3600]`。v1 仅作为后续 cron 预留。 |
| `BROKER_FIRSTRADE_SYNC_MARKET_HOURS_ONLY` | `true` | 后续 cron 用。 |
| `BROKER_FIRSTRADE_LLM_DATA_SCOPE` | `positions_and_balances` | 取值 `positions_only` / `positions_and_balances` / `full`，非法值回退为默认。 |
| `BROKER_ACCOUNT_HASH_SALT` | — | **必填**。当 `BROKER_FIRSTRADE_ENABLED=true` 而此项为空时，进程拒绝启动（避免跨部署可关联的弱哈希）。任意非空随机字符串即可，请保密。 |

## MFA 流程

`firstrade-api` 的 `FTSession.login()` 返回 `need_code` 布尔值：

- 如果配置了 `BROKER_FIRSTRADE_MFA_SECRET`（TOTP），库会自动生成验证码，`need_code=False`，登录一步到位。
- 如果配置了 `BROKER_FIRSTRADE_PIN` / `_EMAIL` / `_PHONE` 且 Firstrade 触发了短信 / 邮箱验证，`need_code=True`，前端需要二步：
  1. `POST /api/v1/broker/firstrade/login` → 返回 `{"status": "mfa_required"}`
  2. 用户在 WebUI 输入收到的验证码 → `POST /api/v1/broker/firstrade/login/verify` body `{"code": "..."}`
- **Cloud Run 实例回收风险**：第二步若收到 `409 broker_session_lost`，前端面板会自动回退到登录态，操作员需重新登录。

## API 使用

所有端点都在管理员 session 守护下（同 `/api/v1/portfolio/*`）：

```bash
# 状态
GET  /api/v1/broker/firstrade/status

# 登录 + MFA
POST /api/v1/broker/firstrade/login            # body: {}
POST /api/v1/broker/firstrade/login/verify     # body: {"code": "123456"}

# 同步（写本地快照）
POST /api/v1/broker/firstrade/sync             # body: {"date_range": "today"}

# 读本地快照（不会触发 Firstrade 调用）
GET  /api/v1/broker/firstrade/accounts
GET  /api/v1/broker/firstrade/positions?account_hash=...
GET  /api/v1/broker/firstrade/orders?account_hash=...
GET  /api/v1/broker/firstrade/transactions?account_hash=...&limit=50
GET  /api/v1/broker/firstrade/snapshot
```

每个返回都含 `status` 字段（`ok` / `not_enabled` / `not_installed` / `login_required` / `mfa_required` / `session_lost` / `failed`）；`session_lost` 走 HTTP 409，其它走 200。

## Agent 使用

Agent 工具：`get_live_broker_portfolio_snapshot`。

- **只读本地快照**：永远不会调用 `FirstradeReadOnlyClient`，不会登录，不会同步。
- **数据范围**遵循 `BROKER_FIRSTRADE_LLM_DATA_SCOPE`：
  - `positions_only`：symbol / qty / market_value / weight_pct / unrealized_pnl
  - `positions_and_balances`（默认）：以上 + cash / total_value / buying_power
  - `full`：以上 + open orders + recent transactions（仍不返回 raw_payload）
- **新鲜度**：每次返回都带 `as_of_iso` + `age_seconds`。如果超过 `max_age_seconds`（默认 3600s），返回 `status="stale"` + `warning`，但仍然把（旧）数据交给 LLM。
- **账户脱敏**：永远只输出 `account_alias`（如 `Firstrade ****1234`）+ `account_last4` + `account_hash`。
- **工具注册条件**：仅在进程启动时 `BROKER_FIRSTRADE_ENABLED=true` 才进入 `ToolRegistry`，否则不出现（比 quant_research 更紧）。

## WebUI 使用

Portfolio 页面顶部有「Firstrade 只读同步」面板：

1. 状态徽章：`未启用` / `缺少依赖` / `未登录` / `需要 MFA` / `已登录` / `同步中`。
2. 按钮：登录 Firstrade / 验证 MFA（若需要）/ 立即同步 / 刷新本地快照。
3. 快照：账户数 / 持仓数 / 订单数 三张统计卡 + 持仓明细前 10 条（账户别名 / 代码 / 数量 / 市值 / 浮盈 / 权重）。
4. **没有任何下单 / 撤单按钮**。

凭证存放：所有 Firstrade 凭证仅来自服务端 `.env`，**前端不会写入 localStorage / sessionStorage / IndexedDB**。

## 安全边界（再次强调）

| 项 | 行为 |
| --- | --- |
| 真实下单 | ❌ 没有实现 |
| 真实撤单 | ❌ 没有实现 |
| 期权交易 | ❌ 没有实现 |
| Agent 直接登录 Firstrade | ❌ 不可能 — Agent 工具只读本地 SQLite |
| Agent 触发 sync_now | ❌ 不可能 — 工具不调用 sync 服务的写路径 |
| 凭证暴露给 LLM | ❌ 三层防御：client `_sanitize_exception` → service redact → API 端点 `_harden_response` |
| 完整账号暴露给前端 / LLM | ❌ 仅 `account_hash`（16 位 sha256）+ `account_last4` + `account_alias` |
| Cookie / token 写到 DB / API / 日志 | ❌ `_REDACT_KEYS` 黑名单 + 数字串 regex `\b\d{8,}\b` 兜底 |

## 故障排查

| 现象 | 原因 | 解决 |
| --- | --- | --- |
| `/status` 返回 `not_enabled` | `BROKER_FIRSTRADE_ENABLED=false` | 改 env 后重启服务（注意：必须同时设置 `BROKER_ACCOUNT_HASH_SALT`，否则进程拒绝启动） |
| `/status` 返回 `not_installed` | 镜像未安装 firstrade 包 | `pip install -r requirements-broker.txt` 后重启 |
| `/login` 返回 `mfa_required` 但 WebUI 没弹出输入框 | 前端面板状态机未刷新 | 点「刷新本地快照」 |
| `/login/verify` 返回 HTTP 409 `session_lost` | Cloud Run 实例在两步之间回收 | 前端会自动回退到登录态；用户重新点「登录 Firstrade」 |
| `/sync` 写了 `status='failed'` 的 sync_run | Firstrade 网站接口变动 / 网络故障 | 看 `last_sync.error.error` 字段（已脱敏）；如确实是 SDK 问题，临时关闭 `BROKER_FIRSTRADE_ENABLED` |
| Agent 工具说 `no_snapshot` | 还没有同步过 / 同步失败 | 操作员到 Portfolio 页面点「立即同步」 |
| Agent 工具说 `stale` | 上次同步超过 1h（默认） | 操作员重新同步；LLM 仍然能看到上次的数据 + 警告 |

## 回滚方式

| 等级 | 操作 | 影响 |
| --- | --- | --- |
| 软回滚 | `BROKER_FIRSTRADE_ENABLED=false` 并重启 | 所有 broker 端点返回 `not_enabled`，Agent 工具不再注册，现有 Portfolio / Backtest / Agent 行为完全不受影响 |
| 数据清理（可选） | `DELETE FROM broker_snapshots; DELETE FROM broker_sync_runs;` | 清空所有本地快照；下次启用时会重新累积 |
| 代码回滚 | `git revert` 引入 broker 模块的 commit | 干净撤回；CHANGELOG 入口让回滚顺序清晰 |
| 移除依赖 | `pip uninstall firstrade` | 同时取消 `requirements-broker.txt` |

## 不在 v1 范围内（后续考虑）

- **真实下单 / 撤单 / 期权交易** — 永远在 v1 之外；如要实现，必须有独立 RFC 与全新的安全审计。
- **后台周期 sync** — 当前仅 WebUI / API 手动触发；后续可加 cron 调度。
- **导入 PortfolioService** — Firstrade 历史交易**不会**自动写入 `portfolio_trades`；快照只供 Agent 研究使用。后续若想做"真实持仓 → 本地账本"的 reconcile，必须有人工确认 + dedup。
