# -*- coding: utf-8 -*-
"""Built-in factor implementations using only ``pandas`` + ``numpy``.

Each builtin is a function ``df -> pd.Series`` where ``df`` is a single
stock's daily OHLCV panel (already sorted ascending by date) and the
returned Series is the per-day factor signal (NaN where the lookback
window isn't satisfied).

Crucially, **every signal at index ``t`` is computed using only data
available at or before ``t``**. The evaluator pairs each signal with a
forward window starting at ``t+1``, so any look-ahead would invalidate
the IC numbers — keep these functions strictly causal.
"""

from __future__ import annotations

from typing import Final

import numpy as np
import pandas as pd


# Minimum-period threshold used by the rolling helpers — half the window
# so we still get a value during the warm-up period instead of all NaN
# at the start of a stock's history.
def _min_periods(n: int) -> int:
    return max(int(n) // 2, 1)


# ---------------------------------------------------------------------
# 1. return_1d
# ---------------------------------------------------------------------
def return_1d(df: pd.DataFrame) -> pd.Series:
    """Single-day return as the factor signal (close-to-close).

    Note: this is *historical* return — used as a factor, it captures
    short-term momentum / mean-reversion depending on the stock pool.
    Causal: at ``t`` we only need ``close[t]`` and ``close[t-1]``.
    """
    return df["close"].pct_change(1)


# ---------------------------------------------------------------------
# 2. return_5d
# ---------------------------------------------------------------------
def return_5d(df: pd.DataFrame) -> pd.Series:
    """5-day cumulative return — standard medium-term momentum baseline."""
    return df["close"].pct_change(5)


# ---------------------------------------------------------------------
# 3. ma_ratio_5_20
# ---------------------------------------------------------------------
def ma_ratio_5_20(df: pd.DataFrame) -> pd.Series:
    """Short-vs-long MA ratio: fast MA above slow MA → trend-up."""
    ma5 = df["close"].rolling(5, min_periods=_min_periods(5)).mean()
    ma20 = df["close"].rolling(20, min_periods=_min_periods(20)).mean()
    return ma5 / ma20.replace(0, np.nan) - 1.0


# ---------------------------------------------------------------------
# 4. volatility_20
# ---------------------------------------------------------------------
def volatility_20(df: pd.DataFrame) -> pd.Series:
    """20-day standard deviation of log returns (not annualized).

    Higher volatility often correlates with future drawdown — useful as
    a risk factor; sign is configurable per study.
    """
    log_ret = np.log(df["close"] / df["close"].shift(1).replace(0, np.nan))
    return log_ret.rolling(20, min_periods=_min_periods(20)).std()


# ---------------------------------------------------------------------
# 5. volume_zscore_20
# ---------------------------------------------------------------------
def volume_zscore_20(df: pd.DataFrame) -> pd.Series:
    """Volume z-score over a 20-day window — abnormal trading activity."""
    v = df["volume"]
    m = v.rolling(20, min_periods=_min_periods(20)).mean()
    s = v.rolling(20, min_periods=_min_periods(20)).std()
    return (v - m) / s.replace(0, np.nan)


# ---------------------------------------------------------------------
# 6. rsi_14
# ---------------------------------------------------------------------
def rsi_14(df: pd.DataFrame) -> pd.Series:
    """14-day RSI (Wilder smoothing). 0–100 range; >70 = overbought,
    <30 = oversold by classic interpretation. We use the simple-mean
    variant (not exponential) to match common A-share charting tools."""
    delta = df["close"].diff()
    gain = delta.clip(lower=0).rolling(14, min_periods=_min_periods(14)).mean()
    loss = (-delta.clip(upper=0)).rolling(14, min_periods=_min_periods(14)).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


# ---------------------------------------------------------------------
# 7. macd_histogram
# ---------------------------------------------------------------------
def macd_histogram(df: pd.DataFrame) -> pd.Series:
    """Standard MACD histogram (12/26/9 EMA). Positive crossing of
    zero historically signals trend reversal up; the histogram captures
    momentum of momentum."""
    ema12 = df["close"].ewm(span=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    return macd - signal


# ---------------------------------------------------------------------
# 8. turnover_or_volume_proxy
# ---------------------------------------------------------------------
def turnover_or_volume_proxy(df: pd.DataFrame) -> pd.Series:
    """Prefer ``volume_ratio`` (量比) when the data source provides it
    (most A-share fetchers do); fall back to a 20-day volume z-score
    so this factor is computable on US / HK pools too."""
    if "volume_ratio" in df.columns:
        ratio = df["volume_ratio"]
        if ratio.notna().any():
            return ratio
    return volume_zscore_20(df)


# ---------------------------------------------------------------------
# Registry-shaped exports for ``registry.py``
# ---------------------------------------------------------------------

BUILTIN_FACTORS: Final[dict] = {
    "return_1d": {
        "name": "1-Day Return",
        "fn": return_1d,
        "expected_direction": "unknown",
        "description": "Yesterday's close-to-close return. Short-term momentum/mean-reversion baseline.",
        "lookback_days": 2,
    },
    "return_5d": {
        "name": "5-Day Return",
        "fn": return_5d,
        "expected_direction": "unknown",
        "description": "5-day cumulative close-to-close return. Medium-term momentum baseline.",
        "lookback_days": 6,
    },
    "ma_ratio_5_20": {
        "name": "MA Ratio 5/20",
        "fn": ma_ratio_5_20,
        "expected_direction": "positive",
        "description": "Fast (5d) vs slow (20d) moving-average ratio minus 1. Positive = trend-up.",
        "lookback_days": 21,
    },
    "volatility_20": {
        "name": "Volatility 20d",
        "fn": volatility_20,
        "expected_direction": "negative",
        "description": "20-day standard deviation of log returns. Risk proxy.",
        "lookback_days": 21,
    },
    "volume_zscore_20": {
        "name": "Volume Z-Score 20d",
        "fn": volume_zscore_20,
        "expected_direction": "unknown",
        "description": "Volume z-score over 20-day window. Abnormal-activity detector.",
        "lookback_days": 21,
    },
    "rsi_14": {
        "name": "RSI 14d",
        "fn": rsi_14,
        "expected_direction": "negative",
        "description": "14-day RSI (simple mean). Mean-reversion signal: high RSI tends to underperform short-term.",
        "lookback_days": 15,
    },
    "macd_histogram": {
        "name": "MACD Histogram",
        "fn": macd_histogram,
        "expected_direction": "positive",
        "description": "MACD(12,26,9) histogram. Captures momentum of momentum; positive crossings often precede uptrends.",
        "lookback_days": 35,
    },
    "turnover_or_volume_proxy": {
        "name": "Turnover/Volume Proxy",
        "fn": turnover_or_volume_proxy,
        "expected_direction": "unknown",
        "description": "Volume_ratio (量比) when available; else 20-day volume z-score. Cross-market liquidity proxy.",
        "lookback_days": 21,
    },
}
