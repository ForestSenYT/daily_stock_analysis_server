# Trading Framework — Phase A (Paper-Only)

## 概览

Phase A 落地了「自动化交易」的**骨架 + 纸面执行**。不动真钱。整个子系统由
单一 env 总开关 `TRADING_MODE` 决定：

| 值 | 含义 |
|---|---|
| `disabled`（默认） | 所有 `/api/v1/trading/*` 端点返回 503，WebUI 面板**完全隐藏**，agent `propose_trade` 工具不注册。 |
| `paper` | `PaperExecutor` 上线：用最近一笔实时报价模拟成交，写入 `portfolio_trades` 时打标 `source='paper'` 与真实交易物理隔离。 |
| `live` | **当前**抛 `NotImplementedError`。Phase B 单独 session 解锁。 |

读-only 不变量在 Phase A **完整保留**：
- `from firstrade import order` / `firstrade.trade` 仍 ban，CI grep 防线
  扩展过来允许 `executors/live.py`（仅 stub）和 `tests/test_trading_invariant_guard.py`
  作为白名单。
- agent `propose_trade` 工具**只发出意图字典**，绝不调用
  `TradingExecutionService.submit`。用户是 Phase A 唯一能提交的实体。

## 数据流（paper 路径成功）

```
[User] 在 PortfolioPage → 交易执行面板填表 → 提交
   ↓
[FE]  POST /api/v1/trading/submit { symbol, side, qty, …, request_uid:UUID }
   ↓
[API] 校验 TRADING_MODE != disabled → 503 否则
   ↓
[SVC] TradingExecutionService.submit(req)
   ├─ audit_repo.start_execution(req)             ← row.status='pending'
   ├─ RiskEngine.evaluate(req, snapshot, broker_status, turnover, price)
   │    6 个硬检查 + 1 个 info-only：
   │      参数完整性 / allow-deny list / 市场时段 /
   │      持仓上限（绝对+占比）/ 卖侧 oversell /
   │      日内成交额上限 / broker 在线（info-only）
   ├─ if decision='block':
   │      → OrderResult(BLOCKED), audit finish, notify, return
   ├─ executor = get_executor(PAPER)
   ├─ PaperExecutor.submit(req, assessment):
   │    1. quote ← Firstrade.get_quote 优先；失败回落 DataFetcherManager
   │    2. fill_price = ask×(1+slip) / bid×(1-slip) / LIMIT 边界
   │    3. PortfolioService.record_trade(source='paper', trade_uid=request_uid, …)
   │    4. OrderResult(FILLED, …, quote_payload=quote)
   ├─ audit_repo.finish_execution(uid, result)
   └─ notify (rate-limited token bucket, 30/min)
   ↓
[API] OrderSubmitResponse → 200
   ↓
[FE]  toast + refresh executions list
```

失败路径（quote 不可用、record_trade 失败、live mode、风险阻断…）一律落到
audit row 的 `status` + `error_code`，前端能 readable 显示，没有静默吞掉。

## 配置

完整 env 变量列表（`.env.example` 已同步）：

```bash
# 总开关（必须）。disabled / paper / live。Phase A 只 paper 实际可用。
TRADING_MODE=disabled

# PaperExecutor 滑点（基点，整数）。MARKET BUY = ask×(1+bps/10000)。
TRADING_PAPER_SLIPPAGE_BPS=5

# PaperExecutor 单笔手续费（绝对金额）。0 = 模拟零佣 Firstrade。
TRADING_PAPER_FEE_PER_TRADE=0.0

# 风险阀值
TRADING_MAX_POSITION_VALUE=10000.0   # 单笔最大持仓金额
TRADING_MAX_POSITION_PCT=0.10        # 单笔占组合权益上限（0..1）
TRADING_MAX_DAILY_TURNOVER=50000.0   # 单日总成交额上限
TRADING_SYMBOL_ALLOWLIST=AAPL,MSFT   # 空 = 不限制；逗号分隔
TRADING_SYMBOL_DENYLIST=             # 永远生效
TRADING_MARKET_HOURS_STRICT=true     # false = info-only 标记不阻断
TRADING_NOTIFICATION_ENABLED=true
TRADING_PAPER_ACCOUNT_ID=             # 推荐：单独建一个 paper 账户

# Broker 自动同步（独立功能 — 跟 trading 解耦）
BROKER_FIRSTRADE_AUTO_SYNC_ENABLED=false
BROKER_FIRSTRADE_AUTO_SYNC_INTERVAL_MINUTES=30
```

## API

| Method | Path | 用途 |
|---|---|---|
| GET | `/api/v1/trading/status` | 状态 + 阀值快照 |
| POST | `/api/v1/trading/submit` | 提交 OrderRequest |
| POST | `/api/v1/trading/risk/preview` | 跑 RiskEngine 不写 audit |
| GET | `/api/v1/trading/executions?mode=paper&limit=50` | 历史审计行 |

### Idempotency

POST `/submit` 强制要求 `request_uid` 字段（≥8 字节）。重复提交同一 UID
返回 **409 Conflict** 而不是双成交。前端每次表单提交都生成新 UUID4。

## ORM

新表 `trade_executions` —— 一行一次 OrderRequest 提交，**不论结果**：

| 列 | 用途 |
|---|---|
| `request_uid` | UUID，UNIQUE，幂等锚 |
| `mode` | paper / live |
| `status` | pending / filled / blocked / failed |
| `risk_flags_json` | RiskAssessment.flags 全量 |
| `risk_decision` | allow / block |
| `request_payload_json` | OrderRequest.to_dict() 全量 |
| `result_payload_json` | OrderResult.to_dict() (less request) |
| `portfolio_trade_id` | paper 路径填实际 PortfolioTrade.id |

3 个复合索引覆盖：`(mode, status, requested_at)` 用于「最近 paper 单」、
`(account_id, requested_at)` 用于按账户查、`(symbol, requested_at)` 用于
日内成交额 rollup。

`portfolio_trades` 加了 `source` 列（VARCHAR(16) DEFAULT 'manual'）：
manual / paper / live / imported / broker_sync。`PaperExecutor` 是
唯一写入 `source='paper'` 的代码路径。

## Phase A 不变量（CI-enforced）

详见 `tests/test_trading_invariant_guard.py`：

1. 整个 `src/` `api/` 目录，**没有任何文件** import `firstrade.order` 或
   `firstrade.trade`。allowlist：`tests/test_trading_invariant_guard.py`
   （fixture）+ `src/trading/executors/live.py`（仅 stub）。
2. `LiveExecutor.__init__` 必须以 `raise NotImplementedError(...)` 结束。
3. 没有任何文件在非允许处出现 bare `place_order(`/`cancel_order(`/`submit_order(`。

## Phase B 解锁清单（**不在本 session**）

- `LiveExecutor.submit` 真实现：选 firstrade.order / IBKR / Alpaca。
- 提交前 confirm-token 流程。
- 可选 dual-approval 模式。
- Kill-switch 端点（即时把 `trading_mode` 翻到 `disabled`）。
- broker session 在线 = 硬阻断（live 模式）。
- 日内 P&L 上限（不止 turnover）。

## Phase C 解锁清单（更远）

- agent `confidence_level >= threshold` AND 用户白名单
  自动 submit。Phase A 完全禁止。

## 自动同步（独立小功能）

`BROKER_FIRSTRADE_AUTO_SYNC_ENABLED=true` 时，FastAPI 启动期会拉一个
后台 daemon 线程，每 N 分钟跑一次 `firstrade_sync_service.sync_now()`。
session 失效 / 没登录时静默跳过；下个 tick 重试。Cloud Run 上多实例
各跑各的，sync_now 内部锁保证不会真冲突。

```bash
BROKER_FIRSTRADE_AUTO_SYNC_ENABLED=true
BROKER_FIRSTRADE_AUTO_SYNC_INTERVAL_MINUTES=30   # 1..1440
```

## 验证

```powershell
# 1. 不变量（必须通过）
python -m pytest tests/test_trading_invariant_guard.py -v
# Expected: 3 passed

# 2. Trading 套件
python -m pytest tests/test_trading_*.py tests/test_agent_propose_trade.py -v
# Expected: 46 passed

# 3. 端到端 smoke（TRADING_MODE=paper）
$env:TRADING_MODE = "paper"
$env:TRADING_PAPER_ACCOUNT_ID = "<active portfolio account id>"
$env:TRADING_SYMBOL_ALLOWLIST = "AAPL"
# restart server, 然后：
$body = @{
    symbol = "AAPL"; side = "buy"; quantity = 1;
    order_type = "market"; request_uid = [guid]::NewGuid().ToString()
} | ConvertTo-Json
curl -X POST http://localhost:8000/api/v1/trading/submit -d $body -H "Content-Type:application/json"
```

## 回滚

| 级别 | 操作 | 影响 |
|---|---|---|
| 软 | `TRADING_MODE=disabled` 重启 | 所有端点 503，UI 面板隐藏，agent 工具不注册 |
| 中 | `git revert <commit>` | 删除新文件 + 还原修改文件；audit 表保留作历史 |
| 硬 | `DROP TABLE trade_executions; ALTER TABLE portfolio_trades DROP COLUMN source` | 完全删除新增的 schema（不推荐——审计数据有价值） |
