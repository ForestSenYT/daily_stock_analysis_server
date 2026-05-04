# -*- coding: utf-8 -*-
"""Cross-sectional quant context service.

For a given symbol, compute its **percentile rank** across each
builtin quant factor relative to a baseline pool (e.g. SP500 proxy
for US stocks, HS300 proxy for A-shares). The result is injected
into the agent's user message so the technical sub-agent can reason
about "where this stock sits among its peers" — context that single-
stock factor values can't provide.

Design choices (v1, intentionally minimal):

  * **Hardcoded baseline pools** — 20 well-known names per market.
    Avoids index-membership lookups, FX-locale issues, and slow
    universe loaders. Good enough for relative ranking; can grow
    to full SP500 / HS300 in a follow-up if the agent's reasoning
    benefits from a wider sample.
  * **Process-level in-memory cache, 24h TTL** — no DB writes, no
    schema migrations, no Postgres round-trip. Cache keys roll over
    daily so factor values don't go stale. Each Cloud Run container
    rebuilds its own cache (acceptable: pool computation is < 5s).
  * **Best-effort** — any individual pool member's data fetch can
    fail without poisoning the result; the percentile is computed
    over whatever subset succeeded. Returns ``None`` if fewer than
    5 pool members produced a value (then the agent gets nothing,
    same as if the feature were off).
  * **Reuse, don't reinvent** — calls
    :func:`src.services.history_loader.get_or_load_history` so we
    share the OHLCV cache that ``/analyze`` already populates.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# =====================================================================
# Baseline pools
# =====================================================================
#
# Hand-picked liquid names per market, balanced across sectors so
# the percentile ranking captures cross-sector behaviour. Treat
# this as a **stable v1 list** — adding / removing names changes
# every cached row's percentile, so we don't churn it.

_US_POOL: Tuple[str, ...] = (
    # Mega-cap tech
    "AAPL", "MSFT", "NVDA", "GOOG", "META", "AMZN", "TSLA",
    # Semiconductors / hardware
    "AMD", "AVGO", "INTC",
    # Financials
    "JPM", "BAC", "V",
    # Healthcare
    "JNJ", "UNH", "LLY",
    # Consumer
    "WMT", "HD", "COST",
    # Energy / industrial
    "XOM",
)

_CN_POOL: Tuple[str, ...] = (
    "600519",  # 贵州茅台
    "000858",  # 五粮液
    "600036",  # 招商银行
    "601318",  # 中国平安
    "000333",  # 美的集团
    "000651",  # 格力电器
    "002594",  # 比亚迪
    "300750",  # 宁德时代
    "600276",  # 恒瑞医药
    "600887",  # 伊利股份
    "601398",  # 工商银行
    "600028",  # 中国石化
    "601857",  # 中国石油
    "000001",  # 平安银行
    "600030",  # 中信证券
    "601012",  # 隆基绿能
    "002415",  # 海康威视
    "600585",  # 海螺水泥
    "601888",  # 中国中免
    "603288",  # 海天味业
)

_HK_POOL: Tuple[str, ...] = (
    "00700",  # 腾讯
    "00941",  # 中国移动
    "01299",  # 友邦保险
    "00388",  # 港交所
    "00939",  # 建设银行
    "00005",  # 汇丰控股
    "01398",  # 工商银行(HK)
    "03988",  # 中国银行
    "02318",  # 中国平安(HK)
    "01810",  # 小米集团
    "09988",  # 阿里巴巴(HK)
    "00883",  # 中国海洋石油
    "02628",  # 中国人寿
    "00386",  # 中国石化(HK)
    "00857",  # 中国石油(HK)
    "01928",  # 金沙中国
    "00027",  # 银河娱乐
    "01177",  # 中国生物制药
    "01088",  # 中国神华
    "00981",  # 中芯国际
)


def _resolve_pool(market: str) -> Tuple[str, ...]:
    m = (market or "").lower().strip()
    if m == "us":
        return _US_POOL
    if m == "cn":
        return _CN_POOL
    if m == "hk":
        return _HK_POOL
    return ()


# =====================================================================
# Cache (in-memory, 24h TTL)
# =====================================================================

# Cache entry: (computed_at_unix_ts, payload_dict_or_None).
_CACHE: Dict[Tuple[str, str, str], Tuple[float, Optional[Dict[str, Any]]]] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL_SECONDS = 24 * 3600  # 1 day


def _cache_key(symbol: str, market: str) -> Tuple[str, str, str]:
    today = date.today().isoformat()
    return (symbol.upper().strip(), (market or "").lower().strip(), today)


def _cache_get(key: Tuple[str, str, str]) -> Optional[Optional[Dict[str, Any]]]:
    """Returns the cached value (which may itself be None — meaning
    a previous compute attempt yielded nothing). Returns the SENTINEL
    ``...`` (Ellipsis) if the cache has no entry, so callers can
    distinguish 'cached as failed' from 'never tried'."""
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry is None:
            return ...  # cache miss
        ts, payload = entry
        if time.time() - ts > _CACHE_TTL_SECONDS:
            _CACHE.pop(key, None)
            return ...
        return payload


def _cache_set(key: Tuple[str, str, str], payload: Optional[Dict[str, Any]]) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = (time.time(), payload)


def clear_cache() -> None:
    """Test helper. Drops the whole cache."""
    with _CACHE_LOCK:
        _CACHE.clear()


# =====================================================================
# Factor computation against the pool
# =====================================================================


def _safe_finite(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _percentile_rank(target_value: float, sample: List[float]) -> Optional[float]:
    """Return ``target_value``'s percentile in ``sample`` (0..100).

    Standard average-rank method: a value at position ``k`` of an
    ``n``-length sorted sample (with ties counted half-half) yields
    ``100 * k / n``. Higher percentile = larger value vs the pool.
    """
    if not sample:
        return None
    below = sum(1 for v in sample if v < target_value)
    equal = sum(1 for v in sample if v == target_value)
    n = len(sample)
    rank = below + 0.5 * equal
    return round(100.0 * rank / n, 1)


def _load_pool_history(
    pool: Tuple[str, ...],
    *,
    end_date: date,
    db: Any,
) -> Dict[str, "Any"]:
    """Pull the most recent ~60 trading days of OHLCV for each pool
    member from the local DB. Returns ``{symbol: pd.DataFrame}``.

    Skips members whose history is missing or shorter than 30 rows
    (the same warm-up minimum quant_signals_service applies).
    """
    import pandas as pd

    start_date = end_date - timedelta(days=89)
    out: Dict[str, "Any"] = {}
    for sym in pool:
        try:
            bars = db.get_data_range(sym, start_date, end_date)
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[xsec_quant] pool member %s history fetch failed: %s",
                sym, exc,
            )
            continue
        if not bars or len(bars) < 30:
            continue
        try:
            df = pd.DataFrame([bar.to_dict() for bar in bars])
            if "close" not in df.columns:
                continue
            out[sym] = df
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[xsec_quant] pool member %s frame build failed: %s",
                sym, exc,
            )
            continue
    return out


def _interpret_percentile(p: float, expected_direction: str) -> str:
    """Tag the percentile with a bullish / bearish / neutral hint
    based on the factor's expected direction. Helps the LLM read
    the table at a glance."""
    if p >= 80:
        if expected_direction == "positive":
            return "high (factor signals bullish)"
        if expected_direction == "negative":
            return "high (factor signals bearish)"
        return "high"
    if p <= 20:
        if expected_direction == "positive":
            return "low (factor signals bearish)"
        if expected_direction == "negative":
            return "low (factor signals bullish)"
        return "low"
    return "mid"


def compute_cross_sectional_context(
    symbol: str,
    market: str,
    *,
    db: Any,
    target_df: Optional["Any"] = None,
    end_date: Optional[date] = None,
) -> Optional[Dict[str, Any]]:
    """Compute ``symbol``'s percentile in each builtin factor vs the
    market's baseline pool.

    Parameters
    ----------
    symbol:
        The stock being analysed (e.g. ``"AAPL"``).
    market:
        ``"us"`` / ``"cn"`` / ``"hk"`` (case-insensitive). Anything
        else returns ``None``.
    db:
        DatabaseManager-shaped object that exposes
        ``get_data_range(code, start, end)`` returning OHLCV rows.
        The pipeline already has one in scope.
    target_df:
        Optional pre-loaded OHLCV DataFrame for ``symbol`` so we
        don't re-query the DB. The pipeline already builds one.
    end_date:
        Snapshot anchor; defaults to today.

    Returns
    -------
    Dict with shape::

        {
          "symbol": "AAPL",
          "market": "us",
          "as_of": "2026-05-04",
          "pool_size_eligible": 18,
          "pool_size_total": 20,
          "factors": [
            {
              "id": "rsi_14",
              "name": "14-day RSI",
              "value": 86.50,
              "percentile": 95.0,
              "expected_direction": "negative",
              "interpretation": "high (factor signals bearish)",
            },
            ...
          ]
        }

    Returns ``None`` when:
      * market is unsupported,
      * fewer than 5 pool members produced any factor value
        (rank would be unreliable),
      * pool loading raised an unexpected error.
    """
    pool = _resolve_pool(market)
    if not pool:
        return None
    if not symbol:
        return None

    target_symbol = symbol.upper().strip()
    end = end_date or date.today()

    try:
        from src.quant_research.factors.builtins import BUILTIN_FACTORS
    except Exception as exc:  # noqa: BLE001
        logger.info(
            "[xsec_quant] BUILTIN_FACTORS unavailable; skipping (%s)", exc,
        )
        return None

    pool_dfs = _load_pool_history(pool, end_date=end, db=db)
    if len(pool_dfs) < 5:
        logger.info(
            "[xsec_quant] %s/%s — only %d pool members loaded; skipping rank",
            target_symbol, market, len(pool_dfs),
        )
        return None

    # Make sure the target's df is available — load it if caller
    # didn't pass one. Without target's factor value we can't rank.
    if target_df is None:
        target_dfs = _load_pool_history((target_symbol,), end_date=end, db=db)
        target_df = target_dfs.get(target_symbol)
    if target_df is None or len(target_df) < 30:
        logger.info(
            "[xsec_quant] %s — target history insufficient; skipping rank",
            target_symbol,
        )
        return None

    factors_out: List[Dict[str, Any]] = []
    for fid, entry in sorted(BUILTIN_FACTORS.items()):
        fn = entry.get("fn")
        if fn is None:
            continue
        # Compute target's latest factor value.
        try:
            target_series = fn(target_df)
            target_val = _safe_finite(target_series.iloc[-1]) if len(target_series) else None
        except Exception as exc:  # noqa: BLE001
            logger.debug("[xsec_quant] target factor %s failed: %s", fid, exc)
            continue
        if target_val is None:
            continue
        # Compute pool's latest factor values.
        pool_vals: List[float] = []
        for sym, df in pool_dfs.items():
            if sym == target_symbol:
                continue  # don't rank against itself
            try:
                series = fn(df)
            except Exception:  # noqa: BLE001
                continue
            v = _safe_finite(series.iloc[-1]) if len(series) else None
            if v is not None:
                pool_vals.append(v)
        if len(pool_vals) < 5:
            continue
        pct = _percentile_rank(target_val, pool_vals)
        if pct is None:
            continue
        expected = entry.get("expected_direction") or "unknown"
        factors_out.append({
            "id": fid,
            "name": entry.get("name") or fid,
            "value": round(target_val, 6),
            "percentile": pct,
            "expected_direction": expected,
            "interpretation": _interpret_percentile(pct, expected),
        })

    if not factors_out:
        return None

    return {
        "symbol": target_symbol,
        "market": (market or "").lower(),
        "as_of": end.isoformat(),
        "pool_size_eligible": len(pool_dfs),
        "pool_size_total": len(pool),
        "factors": factors_out,
    }


def get_or_compute(
    symbol: str,
    market: str,
    *,
    db: Any,
    target_df: Optional["Any"] = None,
) -> Optional[Dict[str, Any]]:
    """Cached wrapper around :func:`compute_cross_sectional_context`.

    Cache key is ``(symbol, market, today)`` — entries roll over
    daily and live for ``_CACHE_TTL_SECONDS`` (24h) regardless.
    A previously cached ``None`` result is returned as ``None``
    without re-computing — this prevents hammering the DB when
    the same pool is consistently failing to load.
    """
    if not symbol:
        return None
    key = _cache_key(symbol, market)
    cached = _cache_get(key)
    if cached is not ...:
        return cached
    result = compute_cross_sectional_context(
        symbol, market, db=db, target_df=target_df,
    )
    _cache_set(key, result)
    return result
