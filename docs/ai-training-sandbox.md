# AI Training Sandbox + Labels — Phase A+C

## 概览

**两个独立子系统，一起跟 Phase A 交易框架并存**：

* **A · Forward simulation 沙盒**：让 AI agent 自主跑买卖决策，
  用最近的实时行情**模拟成交**，写入独立审计表。**完全不进**
  `portfolio_trades` 也**不进** `trade_executions`——纯研究数据。
  收集 1 / 3 / 7 / 30 日 P&L horizon 用于评估 prompt / 模型表现。

* **C · 训练数据标注**：手动给 AI 历史预测（/analyze 报告 OR
  沙盒执行）打标签 `correct` / `incorrect` / `unclear`，可附实际
  后续表现。累积成可导出的 fine-tune 数据集。

总开关 `AI_SANDBOX_ENABLED=false`（默认）→ 所有 `/api/v1/ai-sandbox/*`
和 `/api/v1/ai-training/*` 端点 503，WebUI 面板隐藏。

## 硬不变量

1. **沙盒永远走纸面**：即使 `TRADING_MODE=live`（Phase B 解锁后）
   沙盒路径仍走 `PaperExecutor` 的 quote+fill 模拟逻辑。
2. **零写入 portfolio_trades / trade_executions**：单测
   `test_does_not_write_portfolio_trades` 守门。
3. **隔离 RiskEngine 配置**：沙盒用 `_SandboxRiskConfigProxy` 把
   通用 RiskEngine 重定向到 `ai_sandbox_*` 阀值，跟 trading 框架的
   `trading_*` 阀值完全独立。
4. **`firstrade.order` 仍 banned**：沙盒不下真实订单，invariant
   测试已扩展涵盖 ai_sandbox 包。

## 数据流

```
[trigger]                        [pipeline]                   [audit]
  ┌──────────┐
  │ manual   │── POST /run-once ──┐
  ├──────────┤                    │
  │ batch    │── POST /run-batch ─┤    AISandboxService.submit
  ├──────────┤                    ├──>  1. start_execution → row(pending)
  │ daemon   │── 60 min interval ─┘    2. RiskEngine.evaluate
  └──────────┘                         3. PaperExecutor._resolve_quote +
                                          _derive_fill_price
                                       4. AISandboxResult(filled|blocked|failed)
                                       5. finish_execution → row(final)

[periodic rollup, 1×/h]
  AISandboxPnlService.compute_pnl_for_pending(limit=50)
    ├─ pulls daily OHLCV via DataFetcherManager
    ├─ horizons: fill_d+1d / +3d / +7d / +30d (closest trading day)
    └─ writes pnl_horizons_json + pnl_computed_at on each row
```

## 配置

```bash
# 总开关
AI_SANDBOX_ENABLED=false

# 风险阀值（独立于 trading 框架的 TRADING_*）
AI_SANDBOX_MAX_POSITION_VALUE=5000.0
AI_SANDBOX_MAX_POSITION_PCT=0.20
AI_SANDBOX_MAX_DAILY_TURNOVER=100000.0
AI_SANDBOX_SYMBOL_ALLOWLIST=AAPL,MSFT,NVDA
AI_SANDBOX_SYMBOL_DENYLIST=
AI_SANDBOX_MARKET_HOURS_STRICT=false
AI_SANDBOX_PAPER_SLIPPAGE_BPS=10
AI_SANDBOX_PAPER_FEE_PER_TRADE=0.0

# 后台 daemon（可选，空 watchlist 时不会做任何事）
AI_SANDBOX_DAEMON_ENABLED=false
AI_SANDBOX_DAEMON_INTERVAL_MINUTES=60
AI_SANDBOX_DAEMON_WATCHLIST=AAPL,MSFT,NVDA,GOOG
AI_SANDBOX_DEFAULT_PROMPT_VERSION=v1
```

## API

### 沙盒 (`/api/v1/ai-sandbox/*`)

| Method | Path | 用途 |
|---|---|---|
| GET | `/status` | 状态 + 阀值快照 |
| POST | `/submit` | 直接提交一个 AISandboxIntent（外部 caller / 已有 AI 决策） |
| POST | `/run-once` | 服务端调 LLM 为单股出决策然后提交 |
| POST | `/run-batch` | 同上，批量（最多 20 只） |
| GET | `/executions` | 查最近 N 条执行 |
| GET | `/metrics` | 聚合指标（胜率 / 平均收益） |
| POST | `/pnl/compute` | 手动触发 P&L horizon 回算 |

### 标注 (`/api/v1/ai-training/*`)

| Method | Path | 用途 |
|---|---|---|
| POST | `/labels` | 标注（upsert：同 (kind, id) 重新标会覆盖） |
| DELETE | `/labels?source_kind=...&source_id=...` | 删标签 |
| GET | `/labels` | 列出标签（按 kind / label 过滤） |
| GET | `/labels/stats` | 数据集统计 |

## ORM

新表（已加入 storage.py）：

```python
class AISandboxExecution(Base):
    __tablename__ = 'ai_sandbox_executions'
    # 核心 + AI metadata + status + pnl_horizons_json
    # 完全独立，没有 FK 到 portfolio_accounts

class AITrainingLabel(Base):
    __tablename__ = 'ai_training_labels'
    # source_kind ∈ {analysis_history, ai_sandbox}, source_id INT
    # UNIQUE(source_kind, source_id) 保证一对一
```

## 单测

`tests/test_ai_sandbox.py` 18 cases：
- types round-trip + frozen invariant
- repo CRUD + dedup
- service: disabled / blocked / filled / quote-unavailable / **isolation**（不写 portfolio_trades）
- labels: upsert / overwrite / filter / stats / delete

## Phase A 整合点

- 复用 `RiskEngine`（通过 `_SandboxRiskConfigProxy` 切到沙盒阀值）
- 复用 `PaperExecutor._resolve_quote` + `_derive_fill_price`（**不**调 `submit`）
- 复用 `firstrade_sync_service.get_quote` + `DataFetcherManager` fallback 链路
- 复用 `LLMToolAdapter` 给 daemon 发对 LLM 的 prompt

## 后续 Phase B 关系

沙盒不依赖 Phase B 解锁。即使将来真接通 `firstrade.order`：
- 沙盒路径**仍然只走纸面**（硬不变量）
- 沙盒 audit 表跟 live audit (`trade_executions`) 永远独立
- 标签可以指向 live `trade_executions.id`（未来加 `source_kind='live'` 时）

## 验证

```powershell
python -m pytest tests/test_ai_sandbox.py -v
# 期望 18 passed

# 启用沙盒后
$env:AI_SANDBOX_ENABLED = "true"
$env:AI_SANDBOX_SYMBOL_ALLOWLIST = "AAPL,MSFT,NVDA"
# restart server, 然后：
curl http://localhost:8000/api/v1/ai-sandbox/status   # → status=ready
curl -X POST http://localhost:8000/api/v1/ai-sandbox/run-once `
  -H "Content-Type:application/json" `
  -d '{"symbol":"AAPL"}'
```

## 回滚

| 级别 | 操作 |
|---|---|
| 软 | `AI_SANDBOX_ENABLED=false` 重启 → 端点 503，UI 隐藏 |
| 中 | `git revert <commit>` |
| 硬 | `DROP TABLE ai_sandbox_executions; DROP TABLE ai_training_labels` |
