# -*- coding: utf-8 -*-
"""Quant Research Lab tools for the existing agent ToolRegistry.

Phase-6 contract
----------------
- These tools live in the **same** ``ToolRegistry`` as the existing data /
  analysis / search / market / backtest tools. We do **not** introduce a
  parallel agent framework, do **not** rewrite the LLM adapter, and do
  **not** edit the executor / orchestrator / system prompts.
- Every handler is feature-flag gated: when ``QUANT_RESEARCH_ENABLED`` is
  off the tool returns a structured ``not_enabled`` payload (never 5xx).
- All inputs are bounded: stock count, lookback days, forward window,
  result rows. The handler clamps before forwarding to the service so
  the agent cannot widen the cost surface beyond the API limits.
- The agent **cannot** pass arbitrary Python: ``evaluate_quant_factor``
  takes either a ``builtin_id`` or a ``FactorSpec``-style ``expression``
  string that is parsed by the existing AST whitelist
  (``factors/safe_expression.py``). ``run_quant_factor_backtest`` likewise
  only accepts safe inputs.
- These tools never write portfolio trades, never emit orders, and never
  modify any production table. They wrap the same Phase 2-5 service
  methods the HTTP API exposes — research only.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from src.agent.tools.registry import ToolDefinition, ToolParameter

logger = logging.getLogger(__name__)


# =====================================================================
# Hard caps. Tighter than the HTTP service caps so an LLM-driven call
# can never accidentally widen Cloud Run cost / latency. The HTTP layer
# remains authoritative for end users.
# =====================================================================

MAX_STOCKS = 25                  # half of HTTP cap (50)
MAX_BACKTEST_STOCKS = 25
MAX_LOOKBACK_DAYS = 366          # one trading year
MAX_FORWARD_WINDOW = 30          # half of HTTP cap (60)
MAX_QUANTILE_COUNT = 10
MAX_RISK_SYMBOLS = 25
MAX_RESULT_ROWS = 32             # cap on series we hand back to the LLM
DEFAULT_TIMEOUT_S = 60.0         # service runs synchronously; adapter clamps
MAX_HYPOTHESIS_LEN = 1000


_NOT_ENABLED_PAYLOAD = {
    "enabled": False,
    "status": "not_enabled",
    "message": (
        "Quant Research Lab is disabled "
        "(QUANT_RESEARCH_ENABLED=false). Ask the operator to flip the "
        "feature flag in /api/v1/system/config before retrying."
    ),
}


# =====================================================================
# Service factory + flag check (lazy — no imports at module load)
# =====================================================================

def _service():
    """Build a fresh QuantResearchService per call.

    Constructed lazily so importing this module does not pull in
    ``litellm`` / pandas at agent boot when the lab is disabled. The
    constructor itself is cheap (no DB hits).
    """
    from src.quant_research.service import QuantResearchService
    return QuantResearchService()


def _flag_disabled() -> bool:
    """True if the master flag is off. Mirrors service.enabled."""
    try:
        return not _service().enabled
    except Exception:
        # If the service can't even instantiate (config missing) treat
        # as disabled — the LLM should not retry.
        logger.exception("[quant_research_tools] service init failed")
        return True


def _err(message: str, *, code: str = "quant_research_error",
         field: Optional[str] = None) -> Dict[str, Any]:
    """Stable error envelope for the agent."""
    payload: Dict[str, Any] = {"error": code, "message": message}
    if field:
        payload["field"] = field
    return payload


def _truncate_series(items: List[Any], cap: int = MAX_RESULT_ROWS) -> Dict[str, Any]:
    """Trim a long series before handing it to the LLM and disclose the cut."""
    total = len(items)
    if total <= cap:
        return {"items": list(items), "total": total, "truncated": False}
    return {
        "items": list(items[:cap]),
        "total": total,
        "truncated": True,
        "truncation_note": f"Series truncated to first {cap} of {total} entries.",
    }


# =====================================================================
# 1) list_quant_factors — list built-in factor catalog
# =====================================================================

def _handle_list_quant_factors() -> Dict[str, Any]:
    if _flag_disabled():
        return _NOT_ENABLED_PAYLOAD
    try:
        registry = _service().list_factors()
    except Exception:
        logger.exception("[quant_research_tools] list_factors failed")
        return _err("Failed to list built-in factors.")
    return {
        "enabled": True,
        "builtins": [
            {
                "id": entry.id,
                "name": entry.name,
                "expected_direction": entry.expected_direction,
                "lookback_days": entry.lookback_days,
                "description": entry.description,
            }
            for entry in registry.builtins
        ],
    }


list_quant_factors_tool = ToolDefinition(
    name="list_quant_factors",
    description=(
        "List the built-in factor catalog from the Quant Research Lab. "
        "Read-only. Returns each factor's id, expected direction, "
        "lookback days, and short description so the agent can choose "
        "an appropriate baseline. Returns ``not_enabled`` when the "
        "QUANT_RESEARCH_ENABLED flag is off."
    ),
    parameters=[],
    handler=_handle_list_quant_factors,
    category="data",
)


# =====================================================================
# 2) evaluate_quant_factor — IC / RankIC / quantile returns on a pool
# =====================================================================

def _handle_evaluate_quant_factor(
    stocks: List[str],
    start_date: str,
    end_date: str,
    builtin_id: str = "",
    expression: str = "",
    factor_name: str = "",
    forward_window: int = 5,
    quantile_count: int = 5,
) -> Dict[str, Any]:
    if _flag_disabled():
        return _NOT_ENABLED_PAYLOAD

    # Mutual exclusivity — same rule the HTTP service enforces.
    has_builtin = bool((builtin_id or "").strip())
    has_expr = bool((expression or "").strip())
    if has_builtin == has_expr:
        return _err(
            "Provide exactly one of `builtin_id` or `expression`.",
            code="quant_research_validation",
            field="factor",
        )

    if not isinstance(stocks, list) or not stocks:
        return _err(
            "`stocks` must be a non-empty list of codes.",
            code="quant_research_validation", field="stocks",
        )
    if len(stocks) > MAX_STOCKS:
        return _err(
            f"Too many stocks for agent path (max {MAX_STOCKS}); "
            f"call /api/v1/quant/factors/evaluate directly for larger pools.",
            code="quant_research_validation", field="stocks",
        )

    forward_window = max(1, min(int(forward_window or 5), MAX_FORWARD_WINDOW))
    quantile_count = max(2, min(int(quantile_count or 5), MAX_QUANTILE_COUNT))

    try:
        from src.quant_research.errors import (
            QuantResearchDisabledError,
            QuantResearchValidationError,
        )
        from src.quant_research.schemas import (
            FactorEvaluationRequest,
            FactorSpec,
        )
    except Exception:
        logger.exception("[quant_research_tools] import failed")
        return _err("Quant Research Lab unavailable in this build.")

    factor = FactorSpec(
        name=(factor_name or "").strip() or None,
        builtin_id=(builtin_id or "").strip() or None,
        expression=(expression or "").strip() or None,
    )

    try:
        request = FactorEvaluationRequest(
            factor=factor,
            stocks=list(dict.fromkeys(stocks))[:MAX_STOCKS],
            start_date=start_date,
            end_date=end_date,
            forward_window=forward_window,
            quantile_count=quantile_count,
        )
    except Exception as exc:
        return _err(str(exc), code="quant_research_validation")

    try:
        result = _service().evaluate_factor(request)
    except QuantResearchDisabledError:
        return _NOT_ENABLED_PAYLOAD
    except QuantResearchValidationError as exc:
        return _err(str(exc), code="quant_research_validation",
                    field=getattr(exc, "field", None))
    except Exception:
        logger.exception("[quant_research_tools] evaluate_factor failed")
        return _err("Factor evaluation failed.")

    metrics = result.metrics
    return {
        "enabled": True,
        "run_id": result.run_id,
        "factor_kind": result.factor_kind,
        "factor": result.factor.model_dump(exclude_none=True),
        "stock_pool": result.stock_pool,
        "start_date": result.start_date,
        "end_date": result.end_date,
        "forward_window": result.forward_window,
        "quantile_count": result.quantile_count,
        "coverage": result.coverage.model_dump(),
        "metrics": {
            "ic_mean": metrics.ic_mean,
            "ic_std": metrics.ic_std,
            "icir": metrics.icir,
            "rank_ic_mean": metrics.rank_ic_mean,
            "daily_ic_count": metrics.daily_ic_count,
            "quantile_returns": metrics.quantile_returns,
            "long_short_spread": metrics.long_short_spread,
            "factor_turnover": metrics.factor_turnover,
            "autocorrelation": metrics.autocorrelation,
        },
        "diagnostics": result.diagnostics,
        "assumptions": result.assumptions,
        "is_research_only": True,
    }


evaluate_quant_factor_tool = ToolDefinition(
    name="evaluate_quant_factor",
    description=(
        "Evaluate a quant factor on a stock pool: cross-sectional IC, "
        "RankIC, ICIR, quantile mean returns, long-short spread. Pass "
        "EITHER `builtin_id` (from list_quant_factors) OR `expression` "
        "(parsed by the AST whitelist — no eval/exec). The handler "
        "rejects pools larger than 25 codes; for larger universes call "
        "the HTTP endpoint directly. Read-only; does not place trades."
    ),
    parameters=[
        ToolParameter(
            name="stocks", type="array",
            description="Stock pool, e.g. [\"NVDA\",\"AAPL\"]. Cap: 25.",
        ),
        ToolParameter(
            name="start_date", type="string",
            description="Inclusive ISO date YYYY-MM-DD (signal range start).",
        ),
        ToolParameter(
            name="end_date", type="string",
            description="Inclusive ISO date YYYY-MM-DD (signal range end).",
        ),
        ToolParameter(
            name="builtin_id", type="string", required=False, default="",
            description=(
                "Built-in factor id from list_quant_factors. Mutually "
                "exclusive with `expression`."
            ),
        ),
        ToolParameter(
            name="expression", type="string", required=False, default="",
            description=(
                "Free-form factor expression in the AST-whitelist grammar "
                "(OHLCV columns + 12 helper functions). Mutually "
                "exclusive with `builtin_id`."
            ),
        ),
        ToolParameter(
            name="factor_name", type="string", required=False, default="",
            description="Optional display name for the factor.",
        ),
        ToolParameter(
            name="forward_window", type="integer", required=False, default=5,
            description="Forward-return window in trading days (1..30).",
        ),
        ToolParameter(
            name="quantile_count", type="integer", required=False, default=5,
            description="Number of quantile buckets (2..10).",
        ),
    ],
    handler=_handle_evaluate_quant_factor,
    category="analysis",
)


# =====================================================================
# 3) run_quant_factor_backtest — research backtest (no orders)
# =====================================================================

def _handle_run_quant_factor_backtest(
    strategy: str,
    stocks: List[str],
    start_date: str,
    end_date: str,
    rebalance_frequency: str = "weekly",
    builtin_factor_id: str = "",
    expression: str = "",
    factor_name: str = "",
    top_k: int = 0,
    quantile_count: int = 5,
    initial_cash: float = 1_000_000.0,
    commission_bps: float = 10.0,
    slippage_bps: float = 5.0,
    benchmark: str = "",
) -> Dict[str, Any]:
    if _flag_disabled():
        return _NOT_ENABLED_PAYLOAD

    if not isinstance(stocks, list) or not stocks:
        return _err(
            "`stocks` must be a non-empty list of codes.",
            code="quant_research_validation", field="stocks",
        )
    if len(stocks) > MAX_BACKTEST_STOCKS:
        return _err(
            f"Too many stocks for agent path (max {MAX_BACKTEST_STOCKS}); "
            f"call /api/v1/quant/backtests/run directly for larger pools.",
            code="quant_research_validation", field="stocks",
        )

    try:
        from src.quant_research.errors import (
            QuantResearchDisabledError,
            QuantResearchValidationError,
        )
        from src.quant_research.schemas import ResearchBacktestRequest
    except Exception:
        logger.exception("[quant_research_tools] import failed")
        return _err("Quant Research Lab unavailable in this build.")

    try:
        request = ResearchBacktestRequest(
            strategy=(strategy or "").strip(),
            stocks=list(dict.fromkeys(stocks))[:MAX_BACKTEST_STOCKS],
            start_date=start_date,
            end_date=end_date,
            rebalance_frequency=(rebalance_frequency or "weekly").strip(),
            builtin_factor_id=(builtin_factor_id or "").strip() or None,
            expression=(expression or "").strip() or None,
            factor_name=(factor_name or "").strip() or None,
            top_k=int(top_k) if top_k else None,
            quantile_count=max(2, min(int(quantile_count or 5), MAX_QUANTILE_COUNT)),
            initial_cash=float(initial_cash) if initial_cash else 1_000_000.0,
            commission_bps=float(commission_bps),
            slippage_bps=float(slippage_bps),
            benchmark=(benchmark or "").strip() or None,
        )
    except Exception as exc:
        return _err(str(exc), code="quant_research_validation")

    try:
        result = _service().run_backtest(request)
    except QuantResearchDisabledError:
        return _NOT_ENABLED_PAYLOAD
    except QuantResearchValidationError as exc:
        return _err(str(exc), code="quant_research_validation",
                    field=getattr(exc, "field", None))
    except Exception:
        logger.exception("[quant_research_tools] run_backtest failed")
        return _err("Research backtest failed.")

    metrics = result.metrics
    nav_curve = _truncate_series(result.nav_curve)
    positions = _truncate_series(
        [p.model_dump() for p in result.positions]
    )
    return {
        "enabled": True,
        "run_id": result.run_id,
        "strategy": result.strategy,
        "factor_kind": result.factor_kind,
        "factor_id": result.factor_id,
        "expression": result.expression,
        "stock_pool": result.stock_pool,
        "start_date": result.start_date,
        "end_date": result.end_date,
        "rebalance_frequency": result.rebalance_frequency,
        "metrics": {
            "total_return": metrics.total_return,
            "annualized_return": metrics.annualized_return,
            "annualized_volatility": metrics.annualized_volatility,
            "sharpe": metrics.sharpe,
            "sortino": metrics.sortino,
            "calmar": metrics.calmar,
            "max_drawdown": metrics.max_drawdown,
            "win_rate": metrics.win_rate,
            "turnover": metrics.turnover,
            "cost_drag": metrics.cost_drag,
            "benchmark_return": metrics.benchmark_return,
            "excess_return": metrics.excess_return,
            "information_ratio": metrics.information_ratio,
        },
        "diagnostics": result.diagnostics.model_dump(),
        "nav_curve": nav_curve,
        "positions": positions,
        "is_research_only": True,
        "trade_orders_emitted": False,
    }


run_quant_factor_backtest_tool = ToolDefinition(
    name="run_quant_factor_backtest",
    description=(
        "Run a research-only factor backtest: top-k long-only, simulated "
        "long-short, or equal-weight baseline. Always uses a 1-day signal "
        "lag (no look-ahead). Pool capped at 25 codes for the agent path. "
        "Never sends orders, never writes portfolio trades. NAV curve and "
        "position snapshots are truncated to keep the LLM payload bounded."
    ),
    parameters=[
        ToolParameter(
            name="strategy", type="string",
            description=(
                "One of `top_k_long_only`, `quantile_long_short` (simulated "
                "shorts only), `equal_weight_baseline`."
            ),
        ),
        ToolParameter(
            name="stocks", type="array",
            description="Stock pool. Cap: 25.",
        ),
        ToolParameter(name="start_date", type="string",
                      description="Inclusive ISO date YYYY-MM-DD."),
        ToolParameter(name="end_date", type="string",
                      description="Inclusive ISO date YYYY-MM-DD."),
        ToolParameter(
            name="rebalance_frequency", type="string", required=False,
            default="weekly",
            description="`daily` | `weekly` | `monthly`. Defaults to weekly.",
        ),
        ToolParameter(
            name="builtin_factor_id", type="string", required=False, default="",
            description="Built-in factor id; mutually exclusive with `expression`.",
        ),
        ToolParameter(
            name="expression", type="string", required=False, default="",
            description="Safe-expression factor; mutually exclusive with `builtin_factor_id`.",
        ),
        ToolParameter(
            name="factor_name", type="string", required=False, default="",
            description="Optional display name for the factor.",
        ),
        ToolParameter(
            name="top_k", type="integer", required=False, default=0,
            description="Top-K count for `top_k_long_only`. 0 → service default (≈ pool/5).",
        ),
        ToolParameter(
            name="quantile_count", type="integer", required=False, default=5,
            description="Number of quantile buckets for `quantile_long_short`.",
        ),
        ToolParameter(
            name="initial_cash", type="number", required=False, default=1_000_000.0,
            description="Notional starting NAV (display only).",
        ),
        ToolParameter(
            name="commission_bps", type="number", required=False, default=10.0,
            description="Per-side commission in basis points (0..1000).",
        ),
        ToolParameter(
            name="slippage_bps", type="number", required=False, default=5.0,
            description="Per-side slippage in basis points (0..1000).",
        ),
        ToolParameter(
            name="benchmark", type="string", required=False, default="",
            description="Optional benchmark ticker (e.g. `SPY`) for excess-return / IR.",
        ),
    ],
    handler=_handle_run_quant_factor_backtest,
    category="analysis",
)


# =====================================================================
# 4) get_quant_research_run — fetch a cached backtest result
# =====================================================================

def _handle_get_quant_research_run(run_id: str) -> Dict[str, Any]:
    if _flag_disabled():
        return _NOT_ENABLED_PAYLOAD
    if not isinstance(run_id, str) or not run_id.strip():
        return _err("`run_id` is required.",
                    code="quant_research_validation", field="run_id")
    try:
        from src.quant_research.errors import QuantResearchDisabledError
        result = _service().get_backtest(run_id.strip())
    except QuantResearchDisabledError:
        return _NOT_ENABLED_PAYLOAD
    except Exception:
        logger.exception("[quant_research_tools] get_backtest failed")
        return _err("Failed to fetch research run.")
    if result is None:
        return _err(
            f"Run {run_id} not found in cache (expired or different "
            f"instance). Re-run `run_quant_factor_backtest` to regenerate.",
            code="not_found",
        )
    return {
        "enabled": True,
        "run_id": result.run_id,
        "strategy": result.strategy,
        "factor_kind": result.factor_kind,
        "metrics": result.metrics.model_dump(),
        "diagnostics": result.diagnostics.model_dump(),
        "is_research_only": True,
    }


get_quant_research_run_tool = ToolDefinition(
    name="get_quant_research_run",
    description=(
        "Look up a previously-run research backtest by its run_id. "
        "Returns metrics + diagnostics; the NAV curve and positions are "
        "intentionally omitted to keep the agent payload small. Returns "
        "a structured `not_found` if the run has aged out of the "
        "in-memory cache."
    ),
    parameters=[
        ToolParameter(
            name="run_id", type="string",
            description="Run id from a prior `run_quant_factor_backtest` call.",
        ),
    ],
    handler=_handle_get_quant_research_run,
    category="data",
)


# =====================================================================
# 5) get_quant_portfolio_risk — research risk on hypothetical weights
# =====================================================================

def _handle_get_quant_portfolio_risk(
    weights: Dict[str, float],
    start_date: str,
    end_date: str,
    benchmark_symbol: str = "",
    var_confidence: float = 0.95,
    concentration_threshold_pct: float = 35.0,
) -> Dict[str, Any]:
    if _flag_disabled():
        return _NOT_ENABLED_PAYLOAD
    if not isinstance(weights, dict) or not weights:
        return _err(
            "`weights` must be a non-empty mapping of symbol -> weight.",
            code="quant_research_validation", field="weights",
        )
    if len(weights) > MAX_RISK_SYMBOLS:
        return _err(
            f"Too many symbols for agent path (max {MAX_RISK_SYMBOLS}); "
            f"call /api/v1/quant/risk/evaluate directly for larger books.",
            code="quant_research_validation", field="weights",
        )
    try:
        from src.quant_research.errors import (
            QuantResearchDisabledError,
            QuantResearchValidationError,
        )
        from src.quant_research.schemas import PortfolioRiskResearchRequest
    except Exception:
        logger.exception("[quant_research_tools] import failed")
        return _err("Quant Research Lab unavailable in this build.")

    try:
        request = PortfolioRiskResearchRequest(
            weights={str(k): float(v) for k, v in weights.items()},
            start_date=start_date,
            end_date=end_date,
            benchmark_symbol=(benchmark_symbol or "").strip() or None,
            var_confidence=float(var_confidence),
            concentration_threshold_pct=float(concentration_threshold_pct),
        )
    except Exception as exc:
        return _err(str(exc), code="quant_research_validation")

    try:
        result = _service().evaluate_research_risk(request)
    except QuantResearchDisabledError:
        return _NOT_ENABLED_PAYLOAD
    except QuantResearchValidationError as exc:
        return _err(str(exc), code="quant_research_validation",
                    field=getattr(exc, "field", None))
    except Exception:
        logger.exception("[quant_research_tools] evaluate_research_risk failed")
        return _err("Research risk evaluation failed.")
    return {
        "enabled": True,
        "weights": result.weights,
        "daily_observation_count": result.daily_observation_count,
        "concentration": result.concentration,
        "volatility": result.volatility,
        "drawdown": result.drawdown,
        "var_confidence": result.var_confidence,
        "historical_var": result.historical_var,
        "historical_cvar": result.historical_cvar,
        "beta": result.beta,
        "beta_status": result.beta_status,
        "diagnostics": result.diagnostics,
        "is_research_only": True,
        "trade_orders_emitted": False,
    }


get_quant_portfolio_risk_tool = ToolDefinition(
    name="get_quant_portfolio_risk",
    description=(
        "Compute research risk on a hypothetical set of portfolio "
        "weights: concentration / HHI, historical VaR / CVaR, drawdown, "
        "volatility, and (optional) OLS beta vs a benchmark. Does NOT "
        "touch the live portfolio — agent must pass weights explicitly. "
        "Cap: 25 symbols."
    ),
    parameters=[
        ToolParameter(
            name="weights", type="object",
            description=(
                "Mapping of symbol -> target weight. Negative values are "
                "treated as simulated short legs."
            ),
        ),
        ToolParameter(name="start_date", type="string",
                      description="Inclusive ISO date YYYY-MM-DD."),
        ToolParameter(name="end_date", type="string",
                      description="Inclusive ISO date YYYY-MM-DD."),
        ToolParameter(
            name="benchmark_symbol", type="string", required=False, default="",
            description="Optional benchmark ticker for beta computation.",
        ),
        ToolParameter(
            name="var_confidence", type="number", required=False, default=0.95,
            description="Confidence level for historical VaR / CVaR (0,1).",
        ),
        ToolParameter(
            name="concentration_threshold_pct", type="number",
            required=False, default=35.0,
            description="Single-name weight % above which a symbol triggers an alert.",
        ),
    ],
    handler=_handle_get_quant_portfolio_risk,
    category="analysis",
)


# =====================================================================
# Exported tool list
# =====================================================================

ALL_QUANT_RESEARCH_TOOLS = [
    list_quant_factors_tool,
    evaluate_quant_factor_tool,
    run_quant_factor_backtest_tool,
    get_quant_research_run_tool,
    get_quant_portfolio_risk_tool,
]
