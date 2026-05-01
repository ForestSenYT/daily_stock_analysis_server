# -*- coding: utf-8 -*-
"""Cross-sectional factor evaluator.

What it does
------------
Given a factor function (or a safe expression), a list of stock codes,
and an [start, end] window, this module:

1. Loads each stock's daily history via ``load_history_df`` (DB first,
   fetcher fallback) — same path the rest of the project uses.
2. Computes the factor signal per stock: a Series indexed by date,
   using ONLY data available at or before each row (no look-ahead).
3. Computes the forward return per stock: a Series whose value at
   date ``t`` is ``(close[t + window] / close[t]) - 1`` — strictly
   peeks into the future, paired with the at-``t`` signal.
4. Stacks both panels (rows = dates, columns = stock codes) and on
   each row computes:
   - Pearson IC (linear correlation of factor vs forward return)
   - Spearman RankIC (rank correlation, less sensitive to outliers)
5. Aggregates IC across days → ``ic_mean``, ``ic_std``, ``icir``.
6. Bucketizes each row by factor quantile and averages forward
   returns per bucket → ``quantile_returns`` and
   ``long_short_spread`` (top minus bottom).
7. Computes factor turnover (% of stocks changing quantile from one
   day to the next) and lag-1 self-autocorrelation.
8. Reports coverage (which dates / stocks made it into the panel).

What it does NOT do (out of scope for Phase 2)
----------------------------------------------
- Strategy backtest / equity curve / cost model — that's Phase 3.
- Persistence to ``quant_research_runs`` table — Phase 2 returns the
  result inline; persistence comes with the backtest engine in P3.
- Multiprocessing / concurrent stock loads — single-threaded for
  determinism; Cloud Run instance is small anyway.
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

from src.quant_research.factors.registry import (
    get_builtin_factor_function,
    get_builtin_factor_meta,
)
from src.quant_research.factors.safe_expression import (
    DEFAULT_ALLOWED_INPUTS,
    SafeExpressionSpec,
    UnsafeExpressionError,
    compile_safe_expression,
)

logger = logging.getLogger(__name__)

# Hard caps to keep Cloud Run happy. Endpoint will validate before calling.
MAX_STOCKS = 50
MAX_LOOKBACK_DAYS = 365
MAX_FORWARD_WINDOW = 60
MIN_STOCKS_PER_DAY_FOR_IC = 5  # cross-sectional IC needs at least this many


# =====================================================================
# Inputs
# =====================================================================

@dataclass
class FactorEvalInputs:
    """Everything the evaluator needs in one object — keeps the
    function signature small and lets the endpoint log the request as
    a single payload."""
    builtin_id: Optional[str] = None
    expression: Optional[str] = None
    stocks: List[str] = field(default_factory=list)
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    forward_window: int = 5
    quantile_count: int = 5
    factor_name: Optional[str] = None  # for display in result.factor.name


@dataclass
class FactorEvalOutputs:
    """The evaluator's structured result. Endpoint maps this 1-1 to the
    Pydantic FactorEvaluationResult schema."""
    run_id: str
    factor_name: str
    factor_kind: str  # "builtin" / "expression"
    factor_id: Optional[str]
    expression: Optional[str]
    stock_pool: List[str]
    start_date: date
    end_date: date
    forward_window: int
    quantile_count: int
    coverage: Dict[str, object]
    metrics: Dict[str, object]
    diagnostics: List[str]
    assumptions: Dict[str, object]


# =====================================================================
# Loaders
# =====================================================================

def _load_history_panel(
    stocks: List[str],
    start: date,
    end: date,
    extra_buffer_days: int,
) -> Dict[str, pd.DataFrame]:
    """Load each stock's history once into a dict keyed by stock code.

    Adds ``extra_buffer_days`` of warm-up before ``start`` so rolling
    indicators have data; drops them when stacking the panel.
    """
    from src.services.history_loader import load_history_df

    span = (end - start).days + extra_buffer_days + 5
    panels: Dict[str, pd.DataFrame] = {}
    for code in stocks:
        try:
            df, source = load_history_df(code, days=span, target_date=end)
        except Exception as exc:  # data layer should never crash the evaluator
            logger.warning("evaluator: load_history_df(%s) raised: %s", code, exc)
            continue
        if df is None or df.empty:
            continue
        # Coerce date column to a Python ``date`` and sort.
        if "date" not in df.columns:
            continue
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"]).dt.date
        df = df.sort_values("date").reset_index(drop=True)
        # Trim to a reasonable upper bound; pandas memory grows with cols
        # we don't need.
        keep_cols = [
            c for c in (
                "date", "open", "high", "low", "close",
                "volume", "amount", "pct_chg",
                "ma5", "ma10", "ma20", "volume_ratio",
            )
            if c in df.columns
        ]
        df = df[keep_cols]
        panels[code] = df
    return panels


# =====================================================================
# Factor compute
# =====================================================================

def _resolve_factor_callable(inputs: FactorEvalInputs) -> Tuple[Callable[[pd.DataFrame], pd.Series], str, Optional[str]]:
    """Return (callable, factor_kind, builtin_id_or_None).

    Raises on invalid input — endpoint must surface as 400.
    """
    if inputs.builtin_id and inputs.expression:
        raise ValueError("Provide either builtin_id or expression, not both.")

    if inputs.builtin_id:
        fn = get_builtin_factor_function(inputs.builtin_id)
        if fn is None:
            raise ValueError(f"Unknown builtin factor id: {inputs.builtin_id!r}")
        return fn, "builtin", inputs.builtin_id

    if inputs.expression:
        spec = SafeExpressionSpec(
            expression=inputs.expression,
            allowed_inputs=DEFAULT_ALLOWED_INPUTS,
        )
        compiled = compile_safe_expression(spec)

        def runner(df: pd.DataFrame) -> pd.Series:
            cols = {
                name: df[name]
                for name in DEFAULT_ALLOWED_INPUTS
                if name in df.columns
            }
            return compiled(cols)

        return runner, "expression", None

    raise ValueError("Either builtin_id or expression must be provided.")


def _causal_validation_label(factor_kind: str) -> str:
    if factor_kind == "builtin":
        return "builtin_registry_causal_review"
    if factor_kind == "expression":
        return "safe_expression_static_validation"
    return ""


def _factor_signal_for_stock(df: pd.DataFrame, fn: Callable[[pd.DataFrame], pd.Series]) -> pd.Series:
    """Compute the factor signal as a Series indexed by date for one stock.

    Returns an empty Series if the function fails — keeps the panel
    build tolerant of one bad stock.
    """
    try:
        signal = fn(df)
    except Exception as exc:
        logger.warning("evaluator: factor fn raised on stock: %s", exc)
        return pd.Series(dtype=float)
    if not isinstance(signal, pd.Series):
        return pd.Series(dtype=float)
    out = signal.copy()
    out.index = pd.to_datetime(df["date"]).dt.date.to_numpy()
    return out


def _forward_return_for_stock(df: pd.DataFrame, window: int) -> pd.Series:
    """Forward return from t to t+window, indexed by date ``t``."""
    if len(df) <= window:
        return pd.Series(dtype=float)
    close = df["close"].to_numpy()
    fwd = pd.Series(close, index=pd.to_datetime(df["date"]).dt.date.to_numpy(), dtype=float)
    return fwd.shift(-window) / fwd - 1.0


# =====================================================================
# Cross-sectional metrics
# =====================================================================

def _row_pearson(row_factor: pd.Series, row_fwd: pd.Series) -> Optional[float]:
    """Pearson correlation between two pandas Series (NaN-safe)."""
    df = pd.concat([row_factor, row_fwd], axis=1).dropna()
    if len(df) < MIN_STOCKS_PER_DAY_FOR_IC:
        return None
    f = df.iloc[:, 0]
    r = df.iloc[:, 1]
    if f.std() == 0 or r.std() == 0:
        return None
    return float(f.corr(r))


def _row_spearman(row_factor: pd.Series, row_fwd: pd.Series) -> Optional[float]:
    """Spearman rank correlation, computed as Pearson on the ranks so we
    don't need scipy (pandas' ``method="spearman"`` calls
    ``scipy.stats.spearmanr``)."""
    df = pd.concat([row_factor, row_fwd], axis=1).dropna()
    if len(df) < MIN_STOCKS_PER_DAY_FOR_IC:
        return None
    f_rank = df.iloc[:, 0].rank(method="average")
    r_rank = df.iloc[:, 1].rank(method="average")
    if f_rank.std() == 0 or r_rank.std() == 0:
        return None
    return float(f_rank.corr(r_rank))


def _quantile_means(
    factor_panel: pd.DataFrame,
    fwd_panel: pd.DataFrame,
    n_quantiles: int,
) -> Dict[str, object]:
    """For each date row, bucket stocks into quantiles by factor and
    average their forward returns. Aggregate per-bucket means across
    dates and report long-short spread (top - bottom)."""
    bucket_returns: Dict[int, List[float]] = {q: [] for q in range(1, n_quantiles + 1)}
    for d in factor_panel.index:
        row_f = factor_panel.loc[d].dropna()
        row_r = fwd_panel.loc[d].dropna()
        common = row_f.index.intersection(row_r.index)
        if len(common) < n_quantiles:
            continue  # not enough stocks to bucket
        f = row_f.loc[common]
        r = row_r.loc[common]
        try:
            qbins = pd.qcut(f.rank(method="first"), q=n_quantiles, labels=False, duplicates="drop")
        except Exception:
            continue
        for q in range(n_quantiles):
            mask = qbins == q
            if mask.any():
                bucket_returns[q + 1].append(float(r[mask].mean()))

    quantile_avg: Dict[int, Optional[float]] = {}
    for q, vals in bucket_returns.items():
        quantile_avg[q] = float(np.mean(vals)) if vals else None

    top = quantile_avg.get(n_quantiles)
    bot = quantile_avg.get(1)
    long_short = (top - bot) if (top is not None and bot is not None) else None

    return {
        "quantile_count": n_quantiles,
        "quantile_returns": quantile_avg,
        "long_short_spread": long_short,
    }


def _compute_turnover(factor_panel: pd.DataFrame, n_quantiles: int) -> Optional[float]:
    """Average % of stocks that change quantile bucket from t to t+1.

    Higher turnover = factor signals churn quickly = transaction costs
    matter more in the eventual backtest.
    """
    bucket_panel = pd.DataFrame(index=factor_panel.index, columns=factor_panel.columns, dtype=float)
    for d in factor_panel.index:
        row = factor_panel.loc[d].dropna()
        if len(row) < n_quantiles:
            continue
        try:
            qbins = pd.qcut(row.rank(method="first"), q=n_quantiles, labels=False, duplicates="drop")
            bucket_panel.loc[d, qbins.index] = qbins.values
        except Exception:
            continue
    diffs = []
    prev_row: Optional[pd.Series] = None
    for d in bucket_panel.index:
        cur = bucket_panel.loc[d]
        if prev_row is not None:
            both = pd.concat([prev_row, cur], axis=1).dropna()
            if not both.empty:
                changed = (both.iloc[:, 0] != both.iloc[:, 1]).mean()
                diffs.append(float(changed))
        prev_row = cur
    if not diffs:
        return None
    return float(np.mean(diffs))


def _compute_autocorrelation(factor_panel: pd.DataFrame) -> Optional[float]:
    """Average per-stock lag-1 autocorrelation of the raw factor values.

    Higher = factor values are persistent; close to 0 = noisy.
    """
    acs: List[float] = []
    for code in factor_panel.columns:
        series = factor_panel[code].dropna()
        if len(series) < 5:
            continue
        try:
            ac = float(series.autocorr(lag=1))
        except Exception:
            continue
        if math.isnan(ac):
            continue
        acs.append(ac)
    if not acs:
        return None
    return float(np.mean(acs))


# =====================================================================
# Driver
# =====================================================================

def evaluate_factor(inputs: FactorEvalInputs) -> FactorEvalOutputs:
    """End-to-end factor evaluation. Caller is responsible for
    enforcing user-facing input limits (this function trusts its
    input was already validated).

    Implements the no-look-ahead invariant: the factor signal at
    date ``t`` only sees rows ≤ ``t``; the forward-return at ``t``
    is ``close[t+window]/close[t] - 1`` and is paired only with the
    ``t`` signal value.
    """
    fn, factor_kind, builtin_id = _resolve_factor_callable(inputs)
    causal_validation = _causal_validation_label(factor_kind)
    if not causal_validation:
        raise ValueError("factor failed causal validation")

    # Look-up factor display name. For builtins, prefer the registry
    # entry's friendly name; for expressions, the user-supplied name (or
    # ``"custom expression"`` if not given).
    if factor_kind == "builtin":
        meta = get_builtin_factor_meta(builtin_id)  # type: ignore[arg-type]
        factor_name = inputs.factor_name or (meta.name if meta else builtin_id)
        lookback_buffer = (meta.lookback_days if meta else 30)
    else:
        factor_name = inputs.factor_name or "custom expression"
        lookback_buffer = 30

    end = inputs.end_date or date.today()
    start = inputs.start_date or (end - timedelta(days=90))
    if start > end:
        raise ValueError("start_date must be ≤ end_date")

    panels = _load_history_panel(
        stocks=inputs.stocks,
        start=start,
        end=end,
        extra_buffer_days=lookback_buffer + inputs.forward_window,
    )

    diagnostics: List[str] = []

    # Per-stock signal + forward-return Series
    factor_series: Dict[str, pd.Series] = {}
    fwd_series: Dict[str, pd.Series] = {}
    for code, df in panels.items():
        sig = _factor_signal_for_stock(df, fn)
        fwd = _forward_return_for_stock(df, inputs.forward_window)
        if sig.empty or fwd.empty:
            continue
        # Trim to the requested window (so warm-up data isn't aggregated).
        mask = (sig.index >= start) & (sig.index <= end)
        factor_series[code] = sig[mask]
        fwd_series[code] = fwd.reindex(sig.index)[mask]

    missing = sorted(set(inputs.stocks) - set(panels.keys()))
    if missing:
        diagnostics.append(
            f"{len(missing)} stocks could not be loaded "
            f"(no data in DB or fetcher): {missing[:10]}"
            + ("…" if len(missing) > 10 else "")
        )

    if not factor_series:
        return _empty_result(
            inputs=inputs,
            factor_name=factor_name,
            factor_kind=factor_kind,
            builtin_id=builtin_id,
            start=start,
            end=end,
            diagnostics=diagnostics + ["No stock had usable history; "
                                        "evaluation produced no metrics."],
            causal_validation=causal_validation,
        )

    # Build cross-sectional panels: rows = dates, cols = stocks.
    factor_panel = pd.DataFrame(factor_series)
    fwd_panel = pd.DataFrame(fwd_series)
    # Re-align indices in case some stocks have shorter histories
    common_index = factor_panel.index.intersection(fwd_panel.index)
    factor_panel = factor_panel.loc[common_index]
    fwd_panel = fwd_panel.loc[common_index]

    # ---- coverage report ------------------------------------------------
    requested_stocks = list(inputs.stocks)
    covered_stocks = sorted([c for c in requested_stocks if c in factor_series])
    total_obs = int(factor_panel.notna().sum().sum())
    expected_obs = factor_panel.shape[0] * len(requested_stocks)
    missing_obs = max(0, expected_obs - total_obs)
    coverage = {
        "requested_stocks": requested_stocks,
        "covered_stocks": covered_stocks,
        "missing_stocks": missing,
        "requested_days": len(common_index),
        "total_observations": total_obs,
        "missing_observations": missing_obs,
        "missing_rate": (missing_obs / expected_obs) if expected_obs else None,
    }

    # ---- IC / RankIC across days ---------------------------------------
    ics: List[float] = []
    rics: List[float] = []
    for d in factor_panel.index:
        ic = _row_pearson(factor_panel.loc[d], fwd_panel.loc[d])
        ric = _row_spearman(factor_panel.loc[d], fwd_panel.loc[d])
        if ic is not None:
            ics.append(ic)
        if ric is not None:
            rics.append(ric)

    ic_mean = float(np.mean(ics)) if ics else None
    ic_std = float(np.std(ics, ddof=1)) if len(ics) > 1 else None
    icir = (ic_mean / ic_std) if (ic_mean is not None and ic_std and ic_std > 0) else None
    rank_ic_mean = float(np.mean(rics)) if rics else None

    if not ics:
        diagnostics.append(
            f"No daily IC could be computed; "
            f"each row needs ≥ {MIN_STOCKS_PER_DAY_FOR_IC} stocks with both "
            f"factor and forward-return values."
        )

    # ---- quantile / turnover / autocorrelation -------------------------
    quant = _quantile_means(factor_panel, fwd_panel, inputs.quantile_count)
    turnover = _compute_turnover(factor_panel, inputs.quantile_count)
    autocorr = _compute_autocorrelation(factor_panel)

    metrics = {
        "ic": ics,                 # raw daily series — useful for diagnostics
        "rank_ic": rics,
        "ic_mean": ic_mean,
        "ic_std": ic_std,
        "icir": icir,
        "rank_ic_mean": rank_ic_mean,
        **quant,
        "factor_turnover": turnover,
        "autocorrelation": autocorr,
        "daily_ic_count": len(ics),
        "daily_rank_ic_count": len(rics),
    }

    return FactorEvalOutputs(
        run_id=uuid.uuid4().hex,
        factor_name=factor_name,
        factor_kind=factor_kind,
        factor_id=builtin_id,
        expression=inputs.expression,
        stock_pool=requested_stocks,
        start_date=start,
        end_date=end,
        forward_window=inputs.forward_window,
        quantile_count=inputs.quantile_count,
        coverage=coverage,
        metrics=metrics,
        diagnostics=diagnostics,
        assumptions={
            "lookback_buffer_days": lookback_buffer,
            "min_stocks_per_day_for_ic": MIN_STOCKS_PER_DAY_FOR_IC,
            "no_lookahead": bool(causal_validation),
            "causal_validation": causal_validation,
            "evaluator_version": "phase-2",
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def _empty_result(
    *,
    inputs: FactorEvalInputs,
    factor_name: str,
    factor_kind: str,
    builtin_id: Optional[str],
    start: date,
    end: date,
    diagnostics: List[str],
    causal_validation: str,
) -> FactorEvalOutputs:
    """Construct a structurally-valid result for the "no usable data"
    case so the API still returns 200 with explainable empties rather
    than 500-ing."""
    return FactorEvalOutputs(
        run_id=uuid.uuid4().hex,
        factor_name=factor_name,
        factor_kind=factor_kind,
        factor_id=builtin_id,
        expression=inputs.expression,
        stock_pool=list(inputs.stocks),
        start_date=start,
        end_date=end,
        forward_window=inputs.forward_window,
        quantile_count=inputs.quantile_count,
        coverage={
            "requested_stocks": list(inputs.stocks),
            "covered_stocks": [],
            "missing_stocks": list(inputs.stocks),
            "requested_days": 0,
            "total_observations": 0,
            "missing_observations": 0,
            "missing_rate": None,
        },
        metrics={
            "ic": [],
            "rank_ic": [],
            "ic_mean": None,
            "ic_std": None,
            "icir": None,
            "rank_ic_mean": None,
            "quantile_count": inputs.quantile_count,
            "quantile_returns": {q: None for q in range(1, inputs.quantile_count + 1)},
            "long_short_spread": None,
            "factor_turnover": None,
            "autocorrelation": None,
            "daily_ic_count": 0,
            "daily_rank_ic_count": 0,
        },
        diagnostics=diagnostics,
        assumptions={
            "no_lookahead": bool(causal_validation),
            "causal_validation": causal_validation,
            "evaluator_version": "phase-2",
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
        },
    )
