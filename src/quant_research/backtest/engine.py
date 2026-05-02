# -*- coding: utf-8 -*-
"""Research backtest engine — independent from ``/api/v1/backtest/*``.

Distinction (do not confuse):
- The OLD ``src/core/backtest_engine.py`` validates whether the AI's
  *historical buy/hold/sell calls* hit / missed (after-the-fact decision
  audit). It reads ``analysis_history`` and writes ``backtest_results``.
- THIS module simulates a *factor-driven trading strategy* on raw
  OHLCV: pick stocks each rebalance day by factor rank, compute
  position-weighted PnL, deduct costs, report Sharpe / drawdown / etc.
  It never reads ``analysis_history`` and never writes
  ``backtest_results``.

Strategy types supported (Phase 3):
1. ``top_k_long_only`` — hold the top-K stocks by factor (equal weight),
   refresh on rebalance days.
2. ``quantile_long_short`` — long top quantile, short bottom quantile,
   equal-weight within each leg. Marked ``simulated=True`` because we
   don't model borrow / locate.
3. ``equal_weight_baseline`` — ignore factor entirely; equal-weight
   the entire stock pool. Useful as a comparison baseline.

Causality / no-lookahead invariant:
On each rebalance day ``t``, weights are computed from the factor value
at ``t-1`` (signal lag = 1 trading day). The day-``t`` PnL is then
``Σ weight × close-to-close return from t-1 to t``. This corresponds to
"the factor was visible at end of day t-1 and the trader rebalanced at
the t open / close" — concretely, no t-day data leaks into the t-day
position.

Deliberately out of scope for Phase 3:
- Multi-frequency (intraday) data
- Position-sizing beyond equal-weight within a leg
- Per-stock entry/exit limits, position constraints
- Persistence to a DB table (results live in an in-memory cache in
  ``service.py``; Phase 4+ may add ``quant_backtest_results``).
"""

from __future__ import annotations

import logging
import math
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from src.quant_research.backtest.costs import CostModel, cost_for_turnover
from src.quant_research.backtest.metrics import (
    annualized_return,
    annualized_volatility,
    calmar_ratio,
    information_ratio,
    max_drawdown,
    sharpe_ratio,
    sortino_ratio,
    total_return,
    turnover as compute_turnover,
    win_rate,
)
from src.quant_research.factors.registry import (
    get_builtin_factor_function,
    get_builtin_factor_meta,
)
from src.quant_research.factors.safe_expression import (
    DEFAULT_ALLOWED_INPUTS,
    SafeExpressionSpec,
    compile_safe_expression,
)

logger = logging.getLogger(__name__)


# Hard limits — endpoint must validate before calling.
MAX_STOCKS = 50
MAX_LOOKBACK_DAYS = 366
MIN_HISTORY_DAYS = 60  # need at least this much per stock to compute a 20-day MA + warm-up

VALID_STRATEGIES = ("top_k_long_only", "quantile_long_short", "equal_weight_baseline")
VALID_REBALANCE = ("daily", "weekly", "monthly")


# =====================================================================
# Inputs
# =====================================================================

@dataclass
class BacktestInputs:
    """Parsed + validated inputs.

    Endpoint translates Pydantic ``ResearchBacktestRequest`` → this
    dataclass so the engine has no Pydantic dependency itself.
    """
    strategy: str
    stocks: List[str]
    start_date: date
    end_date: date
    rebalance_frequency: str = "weekly"
    builtin_factor_id: Optional[str] = None
    expression: Optional[str] = None
    factor_name: Optional[str] = None
    top_k: Optional[int] = None
    quantile_count: int = 5
    initial_cash: float = 1_000_000.0
    cost_model: CostModel = field(default_factory=CostModel)
    min_holding_days: Optional[int] = None
    benchmark: Optional[str] = None  # ticker code, optional


# =====================================================================
# Result containers (engine-internal — endpoint maps to Pydantic)
# =====================================================================

@dataclass
class BacktestPositionSnapshot:
    """One point-in-time position record. ``date`` is the rebalance day."""
    date: date
    weights: Dict[str, float]
    nav: float
    cash_reserve: float = 0.0  # Phase 3: always 0 (fully invested)
    cost_deducted: float = 0.0  # cost charged on this rebalance day


@dataclass
class BacktestDiagnostics:
    """Everything the user needs to interpret the result."""
    data_coverage: Dict[str, object]
    missing_symbols: List[str]
    insufficient_history_symbols: List[str]
    rebalance_count: int
    lookahead_bias_guard: bool
    assumptions: Dict[str, object]


@dataclass
class BacktestMetricsBundle:
    """Metric pack — every field is Optional because short / degenerate
    runs may legitimately fail to compute some of them."""
    total_return: Optional[float]
    annualized_return: Optional[float]
    annualized_volatility: Optional[float]
    sharpe: Optional[float]
    sortino: Optional[float]
    calmar: Optional[float]
    max_drawdown: Optional[float]
    win_rate: Optional[float]
    turnover: Optional[float]
    cost_drag: Optional[float]
    benchmark_return: Optional[float]
    excess_return: Optional[float]
    information_ratio: Optional[float]


@dataclass
class BacktestResult:
    """Top-level engine output."""
    run_id: str
    strategy: str
    factor_kind: str  # "builtin" | "expression" | "n/a" (baseline)
    factor_id: Optional[str]
    expression: Optional[str]
    stock_pool: List[str]
    start_date: date
    end_date: date
    rebalance_frequency: str
    nav_curve: List[Dict[str, object]]  # [{"date": iso, "nav": float}]
    metrics: BacktestMetricsBundle
    diagnostics: BacktestDiagnostics
    positions: List[BacktestPositionSnapshot]
    created_at: str  # ISO-8601 UTC


# =====================================================================
# Internals: data loading
# =====================================================================

def _load_close_panel(
    stocks: List[str],
    start: date,
    end: date,
    extra_buffer_days: int,
) -> Tuple[pd.DataFrame, Dict[str, pd.DataFrame], List[str], List[str]]:
    """Load each stock's history, return (close_panel, ohlcv_panels, missing, insufficient).

    ``close_panel``: rows = trading dates, cols = stock codes, vals = close.
    ``ohlcv_panels``: dict of full DataFrames per stock — needed for factor compute.
    """
    from src.services.history_loader import load_history_df

    span = (end - start).days + extra_buffer_days + 5
    closes: Dict[str, pd.Series] = {}
    panels: Dict[str, pd.DataFrame] = {}
    missing: List[str] = []
    insufficient: List[str] = []

    for code in stocks:
        try:
            df, _ = load_history_df(code, days=span, target_date=end)
        except Exception as exc:
            logger.warning("backtest: load_history_df(%s) raised: %s", code, exc)
            missing.append(code)
            continue
        if df is None or df.empty or "date" not in df.columns:
            missing.append(code)
            continue
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"]).dt.date
        df = df.sort_values("date").reset_index(drop=True)
        if len(df) < MIN_HISTORY_DAYS:
            insufficient.append(code)
            continue
        idx = df["date"].to_numpy()
        closes[code] = pd.Series(df["close"].astype(float).to_numpy(), index=idx, dtype=float)
        panels[code] = df

    if not closes:
        return pd.DataFrame(), {}, missing, insufficient

    close_panel = pd.DataFrame(closes)
    close_panel = close_panel.sort_index()
    return close_panel, panels, missing, insufficient


def _resolve_factor_function(
    builtin_id: Optional[str],
    expression: Optional[str],
) -> Tuple[Optional[Callable[[pd.DataFrame], pd.Series]], str, Optional[str]]:
    """Return (callable_or_None, factor_kind, builtin_id_or_None).

    ``equal_weight_baseline`` strategy passes both as None and gets
    ``(None, "n/a", None)`` back — the engine handles that by skipping
    factor compute entirely.
    """
    if builtin_id and expression:
        raise ValueError("Provide either builtin_factor_id or expression, not both.")
    if builtin_id:
        fn = get_builtin_factor_function(builtin_id)
        if fn is None:
            raise ValueError(f"Unknown builtin factor id: {builtin_id!r}")
        return fn, "builtin", builtin_id
    if expression:
        spec = SafeExpressionSpec(
            expression=expression,
            allowed_inputs=DEFAULT_ALLOWED_INPUTS,
        )
        compiled = compile_safe_expression(spec)

        def runner(df: pd.DataFrame) -> pd.Series:
            cols = {n: df[n] for n in DEFAULT_ALLOWED_INPUTS if n in df.columns}
            return compiled(cols)

        return runner, "expression", None
    return None, "n/a", None


def _build_factor_panel(
    factor_fn: Callable[[pd.DataFrame], pd.Series],
    ohlcv_panels: Dict[str, pd.DataFrame],
) -> pd.DataFrame:
    """Per-stock factor compute → cross-stock panel keyed by date."""
    series_map: Dict[str, pd.Series] = {}
    for code, df in ohlcv_panels.items():
        try:
            s = factor_fn(df)
        except Exception as exc:
            logger.warning("backtest: factor fn raised on %s: %s", code, exc)
            continue
        if not isinstance(s, pd.Series):
            continue
        s = pd.Series(s.to_numpy(), index=df["date"].to_numpy(), dtype=float)
        series_map[code] = s
    if not series_map:
        return pd.DataFrame()
    return pd.DataFrame(series_map).sort_index()


# =====================================================================
# Internals: rebalance schedule + weight policies
# =====================================================================

def _rebalance_dates(close_panel: pd.DataFrame, frequency: str) -> List:
    """Return the subset of trading dates on which we recompute weights."""
    if close_panel.empty:
        return []
    dates = list(close_panel.index)
    if frequency == "daily":
        return dates
    # For weekly / monthly, take the LAST trading day in each period —
    # corresponds to "rebalance at the close of week / month".
    df = pd.DataFrame(index=pd.to_datetime(dates))
    df["d"] = dates
    if frequency == "weekly":
        keys = df.index.to_period("W")
    elif frequency == "monthly":
        keys = df.index.to_period("M")
    else:
        raise ValueError(f"unknown rebalance_frequency: {frequency!r}")
    last_in_period = df.groupby(keys, sort=False).last()
    return list(last_in_period["d"])


def _compute_weights_top_k(factor_row: pd.Series, top_k: int) -> Dict[str, float]:
    """Pick top-k stocks by factor value, equal-weight them. Ties broken
    by alphabetical order (stable + reproducible)."""
    valid = factor_row.dropna()
    if len(valid) == 0:
        return {}
    k = min(top_k, len(valid))
    # Sort: factor desc, code asc (stable)
    ranked = valid.sort_values(ascending=False, kind="mergesort")
    selected = sorted(ranked.head(k).index.tolist())
    if not selected:
        return {}
    w = 1.0 / len(selected)
    return {code: w for code in selected}


def _compute_weights_quantile_long_short(
    factor_row: pd.Series, n_quantiles: int
) -> Dict[str, float]:
    """Long top quantile, short bottom quantile, equal-weight within each
    leg. Each leg's gross is 1.0 (so total gross is 2.0, net is 0.0)."""
    valid = factor_row.dropna()
    if len(valid) < n_quantiles:
        return {}
    try:
        ranks = valid.rank(method="first")
        bins = pd.qcut(ranks, q=n_quantiles, labels=False, duplicates="drop")
    except Exception:
        return {}
    top_mask = bins == n_quantiles - 1
    bot_mask = bins == 0
    longs = sorted(valid.index[top_mask].tolist())
    shorts = sorted(valid.index[bot_mask].tolist())
    out: Dict[str, float] = {}
    if longs:
        wl = 1.0 / len(longs)
        for c in longs:
            out[c] = wl
    if shorts:
        ws = 1.0 / len(shorts)
        for c in shorts:
            out[c] = out.get(c, 0.0) - ws
    return out


def _compute_weights_equal(stocks_alive: List[str]) -> Dict[str, float]:
    """Equal-weight every stock that has a non-NaN close on the rebalance day."""
    if not stocks_alive:
        return {}
    w = 1.0 / len(stocks_alive)
    return {code: w for code in stocks_alive}


# =====================================================================
# Driver
# =====================================================================

def run_backtest(inputs: BacktestInputs) -> BacktestResult:
    """End-to-end backtest. Caller pre-validates input limits.

    Implements close-to-close PnL with a strict 1-or-more-day signal
    lag — the structural no-lookahead guarantee the engine advertises
    via ``diagnostics.lookahead_bias_guard=True``:

      - rebalance day d: weights = f(factor_panel.loc[signal_d])
        where ``signal_d`` is the latest index in ``factor_panel`` with
        ``signal_d < d`` (see ``prior_dates[-1]`` below). When the
        factor refreshes daily this collapses to a 1-trading-day lag;
        on weekly/monthly rebalances the lag may be longer but is
        never zero.
      - daily PnL between rebalances = Σ weight × close-to-close return
      - cost charged on rebalance day after PnL (so it deducts from NAV
        before tomorrow starts)
    """
    if inputs.strategy not in VALID_STRATEGIES:
        raise ValueError(f"Unknown strategy: {inputs.strategy!r}")
    if inputs.rebalance_frequency not in VALID_REBALANCE:
        raise ValueError(f"Unknown rebalance_frequency: {inputs.rebalance_frequency!r}")
    if inputs.start_date > inputs.end_date:
        raise ValueError("start_date must be ≤ end_date")
    if (inputs.end_date - inputs.start_date).days > MAX_LOOKBACK_DAYS:
        raise ValueError(f"Backtest window too long (> {MAX_LOOKBACK_DAYS} days).")
    if len(inputs.stocks) > MAX_STOCKS:
        raise ValueError(f"Too many stocks (> {MAX_STOCKS}).")

    # Look-back buffer so factor warm-up doesn't truncate the run window.
    lookback_buffer = 30
    if inputs.builtin_factor_id:
        meta = get_builtin_factor_meta(inputs.builtin_factor_id)
        if meta:
            lookback_buffer = max(lookback_buffer, meta.lookback_days + 5)

    close_panel, ohlcv_panels, missing, insufficient = _load_close_panel(
        stocks=inputs.stocks,
        start=inputs.start_date,
        end=inputs.end_date,
        extra_buffer_days=lookback_buffer,
    )

    diagnostics_assumptions: Dict[str, object] = {
        "commission_bps": inputs.cost_model.commission_bps,
        "slippage_bps": inputs.cost_model.slippage_bps,
        "rebalance_frequency": inputs.rebalance_frequency,
        "allows_short": inputs.strategy == "quantile_long_short",
        "simulated_short_leg": inputs.strategy == "quantile_long_short",
        "min_holding_days": inputs.min_holding_days or 0,
        "trading_days_per_year": 252,
        "engine_version": "phase-3",
    }

    if close_panel.empty:
        return _empty_result(
            inputs=inputs,
            missing=missing,
            insufficient=insufficient,
            assumptions=diagnostics_assumptions,
            note="No stock had usable history; backtest produced no metrics.",
        )

    # --- Factor compute (skip for baseline) ----------------------------
    factor_fn, factor_kind, factor_id = _resolve_factor_function(
        builtin_id=inputs.builtin_factor_id,
        expression=inputs.expression,
    )
    if inputs.strategy == "equal_weight_baseline":
        factor_panel: Optional[pd.DataFrame] = None
    else:
        if factor_fn is None:
            raise ValueError(
                "Strategy requires a factor; pass builtin_factor_id or expression."
            )
        factor_panel = _build_factor_panel(factor_fn, ohlcv_panels)
        if factor_panel.empty:
            return _empty_result(
                inputs=inputs,
                missing=missing,
                insufficient=insufficient,
                assumptions=diagnostics_assumptions,
                note="Factor returned no usable values; backtest produced no metrics.",
            )

    # --- Trim to evaluation window ------------------------------------
    in_window_mask = (close_panel.index >= inputs.start_date) & (close_panel.index <= inputs.end_date)
    eval_dates = close_panel.index[in_window_mask]
    if len(eval_dates) < 2:
        return _empty_result(
            inputs=inputs,
            missing=missing,
            insufficient=insufficient,
            assumptions=diagnostics_assumptions,
            note="Not enough trading days in [start_date, end_date] for a meaningful backtest.",
        )

    # --- Rebalance schedule -------------------------------------------
    rebal_dates = set(_rebalance_dates(close_panel.loc[eval_dates], inputs.rebalance_frequency))

    # --- Daily simulation loop ----------------------------------------
    nav = inputs.initial_cash
    weights: Dict[str, float] = {}  # current weights
    nav_records: List[Dict[str, object]] = []
    daily_returns: List[float] = []
    weight_history: List[Dict[str, float]] = []
    weight_dates: List = []
    position_snapshots: List[BacktestPositionSnapshot] = []
    total_cost = 0.0
    last_rebalance_d: Optional = None

    eval_dates_list = list(eval_dates)
    for i, d in enumerate(eval_dates_list):
        # 1) Apply day's PnL FIRST (using yesterday's weights) — "weights
        # at start of day d" = "weights set at end of day d-1".
        if i > 0 and weights:
            prev_d = eval_dates_list[i - 1]
            cur_close = close_panel.loc[d]
            prev_close = close_panel.loc[prev_d]
            day_return = 0.0
            for code, w in weights.items():
                if code not in cur_close.index:
                    continue
                pc = prev_close.get(code, np.nan)
                cc = cur_close.get(code, np.nan)
                if pd.isna(pc) or pd.isna(cc) or pc == 0:
                    continue
                day_return += w * (cc / pc - 1.0)
            nav *= 1.0 + day_return
            daily_returns.append(day_return)
        else:
            daily_returns.append(0.0)

        # 2) On rebalance days, recompute weights using factor at d-1
        # (1-trading-day lag — the no-lookahead invariant lives here).
        if d in rebal_dates:
            new_weights: Dict[str, float] = {}
            if inputs.strategy == "equal_weight_baseline":
                alive = [
                    code for code in close_panel.columns
                    if not pd.isna(close_panel.at[d, code])
                ]
                new_weights = _compute_weights_equal(alive)
            else:
                # Use factor at the trading day strictly before d.
                # ``factor_panel`` may include dates outside eval window;
                # find the last available date < d.
                if factor_panel is None or factor_panel.empty:
                    new_weights = {}
                else:
                    prior_dates = factor_panel.index[factor_panel.index < d]
                    if len(prior_dates):
                        signal_d = prior_dates[-1]
                        signal_row = factor_panel.loc[signal_d]
                        # Drop stocks with missing close on the rebalance day
                        # so the weight can actually be executed.
                        cur_close = close_panel.loc[d]
                        viable = signal_row[
                            signal_row.index.intersection(
                                cur_close.dropna().index
                            )
                        ]
                        if inputs.strategy == "top_k_long_only":
                            top_k = inputs.top_k or max(1, len(viable.dropna()) // 5)
                            new_weights = _compute_weights_top_k(viable, top_k)
                        elif inputs.strategy == "quantile_long_short":
                            new_weights = _compute_weights_quantile_long_short(
                                viable, inputs.quantile_count
                            )

            # 3) Cost charged on weight diff
            all_codes = set(weights) | set(new_weights)
            l1 = sum(abs(new_weights.get(c, 0.0) - weights.get(c, 0.0)) for c in all_codes)
            turnover_dollars = nav * l1  # two-sided gross turnover
            day_cost = cost_for_turnover(turnover_dollars, inputs.cost_model)
            nav -= day_cost
            total_cost += day_cost
            # Optional min holding: if last rebalance is too recent, skip.
            if (
                inputs.min_holding_days
                and last_rebalance_d is not None
                and (d - last_rebalance_d).days < inputs.min_holding_days
            ):
                # Skip this rebalance — keep prior weights, refund cost we
                # just charged (we shouldn't have touched the book).
                nav += day_cost
                total_cost -= day_cost
            else:
                weights = new_weights
                last_rebalance_d = d
                position_snapshots.append(
                    BacktestPositionSnapshot(
                        date=d,
                        weights=dict(weights),
                        nav=nav,
                        cash_reserve=0.0,
                        cost_deducted=day_cost,
                    )
                )

        # Always record the NAV at end of day
        nav_records.append({"date": d.isoformat(), "nav": float(nav)})
        weight_history.append(dict(weights))
        weight_dates.append(d)

    # --- Post-process: NAV / returns series --------------------------
    nav_series = pd.Series(
        [r["nav"] for r in nav_records],
        index=[d.isoformat() for d in eval_dates_list],
        dtype=float,
    )
    daily_returns_series = pd.Series(daily_returns, dtype=float)
    weights_panel = pd.DataFrame(weight_history, index=eval_dates_list).fillna(0.0)

    # --- Benchmark ----------------------------------------------------
    bench_total: Optional[float] = None
    bench_daily_series: Optional[pd.Series] = None
    if inputs.benchmark:
        bench_panel, _, bm_missing, bm_insuf = _load_close_panel(
            stocks=[inputs.benchmark],
            start=inputs.start_date,
            end=inputs.end_date,
            extra_buffer_days=lookback_buffer,
        )
        if not bench_panel.empty and inputs.benchmark in bench_panel.columns:
            bench_close = bench_panel[inputs.benchmark]
            in_w = (bench_close.index >= inputs.start_date) & (bench_close.index <= inputs.end_date)
            bench_close = bench_close[in_w].dropna()
            if len(bench_close) >= 2:
                bench_total = float(bench_close.iloc[-1] / bench_close.iloc[0] - 1.0)
                bench_daily_series = bench_close.pct_change().dropna().reset_index(drop=True)

    # --- Metrics -----------------------------------------------------
    tr = total_return(nav_series)
    metrics = BacktestMetricsBundle(
        total_return=tr,
        annualized_return=annualized_return(nav_series),
        annualized_volatility=annualized_volatility(daily_returns_series),
        sharpe=sharpe_ratio(daily_returns_series),
        sortino=sortino_ratio(daily_returns_series),
        calmar=calmar_ratio(nav_series, daily_returns_series),
        max_drawdown=max_drawdown(nav_series),
        win_rate=win_rate(daily_returns_series),
        turnover=compute_turnover(weights_panel),
        cost_drag=(total_cost / inputs.initial_cash) if inputs.initial_cash else None,
        benchmark_return=bench_total,
        excess_return=(tr - bench_total) if bench_total is not None else None,
        information_ratio=(
            information_ratio(daily_returns_series, bench_daily_series)
            if bench_daily_series is not None and len(bench_daily_series) > 1 else None
        ),
    )

    diag = BacktestDiagnostics(
        data_coverage={
            "requested_stocks": list(inputs.stocks),
            "covered_stocks": sorted(close_panel.columns.tolist()),
            "trading_days_in_window": len(eval_dates),
        },
        missing_symbols=missing,
        insufficient_history_symbols=insufficient,
        rebalance_count=len(position_snapshots),
        lookahead_bias_guard=True,  # by construction, see header docstring
        assumptions=diagnostics_assumptions,
    )

    return BacktestResult(
        run_id=uuid.uuid4().hex,
        strategy=inputs.strategy,
        factor_kind=factor_kind,
        factor_id=factor_id,
        expression=inputs.expression,
        stock_pool=list(inputs.stocks),
        start_date=inputs.start_date,
        end_date=inputs.end_date,
        rebalance_frequency=inputs.rebalance_frequency,
        nav_curve=nav_records,
        metrics=metrics,
        diagnostics=diag,
        positions=position_snapshots,
        created_at=datetime.now(timezone.utc).isoformat(),
    )


def _empty_result(
    *,
    inputs: BacktestInputs,
    missing: List[str],
    insufficient: List[str],
    assumptions: Dict[str, object],
    note: str,
) -> BacktestResult:
    """Construct a structurally-valid result for the "no usable data"
    case so the API still returns 200 with explanation."""
    factor_kind = "n/a"
    factor_id = inputs.builtin_factor_id
    if inputs.builtin_factor_id:
        factor_kind = "builtin"
    elif inputs.expression:
        factor_kind = "expression"
    return BacktestResult(
        run_id=uuid.uuid4().hex,
        strategy=inputs.strategy,
        factor_kind=factor_kind,
        factor_id=factor_id,
        expression=inputs.expression,
        stock_pool=list(inputs.stocks),
        start_date=inputs.start_date,
        end_date=inputs.end_date,
        rebalance_frequency=inputs.rebalance_frequency,
        nav_curve=[],
        metrics=BacktestMetricsBundle(
            total_return=None, annualized_return=None, annualized_volatility=None,
            sharpe=None, sortino=None, calmar=None, max_drawdown=None,
            win_rate=None, turnover=None, cost_drag=None,
            benchmark_return=None, excess_return=None, information_ratio=None,
        ),
        diagnostics=BacktestDiagnostics(
            data_coverage={
                "requested_stocks": list(inputs.stocks),
                "covered_stocks": [],
                "trading_days_in_window": 0,
                "note": note,
            },
            missing_symbols=missing,
            insufficient_history_symbols=insufficient,
            rebalance_count=0,
            lookahead_bias_guard=True,
            assumptions=assumptions,
        ),
        positions=[],
        created_at=datetime.now(timezone.utc).isoformat(),
    )
