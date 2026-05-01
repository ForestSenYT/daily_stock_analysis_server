# Quant Research Lab

> Status: **Phase 1 — scaffold only** (this build).
> Master flag: `QUANT_RESEARCH_ENABLED` (default `false`).

## What it is

The Quant Research Lab is a research-grade quantitative module that lives
**alongside** the existing AI stock-analysis stack — not on top of it.
It is intentionally separate from the AI-decision validation backtest
under `/api/v1/backtest/*`.

| | Existing `/api/v1/backtest/*` | New `/api/v1/quant/*` |
| --- | --- | --- |
| Question it answers | "Were the AI's past buy/hold/sell calls correct?" | "Does this factor / strategy idea hold up out-of-sample?" |
| Input | Historical `analysis_history` rows | Arbitrary stock pool + factor / strategy spec |
| Output | Hit-rate / win-rate per AI decision | IC / RankIC / Sharpe / drawdown / quantile returns |
| Touches AI prompts? | No (read-only) | No (no LLM in core path; LLM only generates FactorSpec JSON in P5) |
| Trades? | No (simulated only) | No (research only — never sends orders) |

Both modules share the underlying `stock_daily` (OHLCV) table for data
and the `PortfolioRiskService` helpers for some risk metrics, but their
**API surface, schemas, and business logic are fully separated**.

## Endpoints (Phase 1)

All endpoints sit under `/api/v1/quant/*` and require an admin session
cookie (same as the rest of `/api/v1/system/*`). They are safe to call
even when the feature flag is off — they return a structured payload
describing the lab as `not_enabled` rather than raising 5xx.

### `GET /api/v1/quant/status`

Returns master flag value + which roadmap phase is live in this build.

**Response (`QuantResearchStatus`)**:
```json
{
  "enabled": false,
  "status": "not_enabled",
  "message": "Quant Research Lab is disabled. Set QUANT_RESEARCH_ENABLED=true to enable it.",
  "phase": "phase-1-scaffold"
}
```

When the flag is on, the response shape is identical but `status` becomes
`"ready"` (Phase 1) or `"operational"` (Phase 3+).

### `GET /api/v1/quant/capabilities`

Returns the capability inventory — every planned feature, with
`available: true|false` per phase. Useful for the SPA to render
placeholder cards.

**Response excerpt (`QuantResearchCapabilities`)**:
```json
{
  "enabled": false,
  "capabilities": [
    {
      "name": "factor_evaluation",
      "title": "Factor Evaluation",
      "available": false,
      "phase": "phase-2",
      "description": "Evaluate built-in or AI-generated factors on a stock pool: coverage, IC/RankIC, ICIR, ...",
      "endpoints": ["GET  /api/v1/quant/factors", "POST /api/v1/quant/factors/evaluate"],
      "requires_optional_deps": []
    },
    ...
  ]
}
```

### `GET /api/v1/quant/healthcheck`

Cheap `{"ok": true}` ping so deploy verification can confirm the router
is mounted without exercising service logic.

## Roadmap

| Phase | Feature | Status |
| --- | --- | --- |
| **P1** | Scaffold + feature flag + status / capabilities endpoints | ✅ this build |
| P2 | Factor library: 8 built-in factors, IC / RankIC, quantile returns, safe-expression AST validator | planned |
| P3 | Research backtest engine (top-k long-only, simulated long-short), Sharpe / Sortino / drawdown / turnover | planned |
| P4 | Portfolio optimizer (equal-weight, inverse-vol, max-Sharpe), VaR / CVaR | planned |
| P5 | AI FactorSpec generation — LLM emits validated JSON only, never Python code | planned |
| P6 | Agent integration — opt-in tools + skill, default skill set unchanged | planned |
| P7 | SPA — `/quant` route, factor explorer, backtest result charts (Recharts) | planned |

## Configuration

| Env var | Default | Effect |
| --- | --- | --- |
| `QUANT_RESEARCH_ENABLED` | `false` | Master flag. When `false`, all `/api/v1/quant/*` endpoints return `not_enabled`. Toggle from the WebUI Settings → Quant Research Lab section once Phase 2+ ships. |

## Optional dependencies

The base service (Cloud Run image) does **not** install any quant-specific
library. Phase 2+ may need a few; they live in `requirements-quant.txt`
which is **not** part of the Cloud Run image build.

To install locally for development:
```bash
pip install -r requirements-quant.txt
```

The base code paths must remain importable when these libs are missing —
each phase will use lazy `import ... ; except ImportError` and emit a
structured `not_supported` error if the user invokes a path that
requires the missing dep.

## Safety guarantees

- **No live trading.** This module never connects to a broker, sends
  orders, or modifies the existing `portfolio_trades` table. All output
  is `simulated` / `target weights` / `factor scores`.
- **No code execution from LLM output.** Phase 5 will let an AI propose
  a `FactorSpec`, but the expression is parsed by an AST whitelist
  (`safe_expression.py`) before any evaluation. `eval` / `exec` /
  `__import__` / shell / file / network are forbidden.
- **No look-ahead bias.** Each evaluator interface explicitly partitions
  signal data (≤ t) and forward-window data (> t); breaching the
  partition fails fast.
- **Existing functionality untouched.** `AGENT_SYSTEM_PROMPT`,
  `CHAT_SYSTEM_PROMPT`, `/api/v1/backtest/*` semantics, `analysis_history`
  schema, `portfolio_trades` schema — all frozen.

## Cloud Run notes

- Memory budget: the optimizer + backtest engines (Phases 3–4) will impose
  hard caps on `max_lookback_days` / `max_stock_pool_size` to fit within
  the existing 2 GiB instance.
- Long-running runs use the same async pattern as `/analyze/async`:
  `POST /run` returns 202 immediately, status pollable via `GET /run/{id}`.
- All persistence goes through the GCS-mounted SQLite at
  `/mnt/persistent/data/stock_analysis.db` — no new database connection
  is added.
