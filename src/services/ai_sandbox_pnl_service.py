# -*- coding: utf-8 -*-
"""Post-fact P&L horizon computation for AI sandbox executions.

Walks the ``ai_sandbox_executions`` rows that:
  * have status='filled'
  * are old enough that at least the 1-day horizon has elapsed
  * don't yet have ``pnl_horizons_json`` filled in

For each row, fetches the symbol's daily OHLCV via
``DataFetcherManager`` and computes the close-to-close return at
1 / 3 / 7 / 30 trading-day horizons. BUY-side rows treat positive
return as a "win"; SELL-side rows invert the convention.

This is invoked:
  * On-demand via ``compute_pnl_for_pending(limit=N)``
  * By a periodic daemon thread (1× per hour by default).

Best-effort: any per-symbol failure is logged and skipped, so a
single broken symbol doesn't poison the whole batch.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from src.ai_sandbox.repo import AISandboxRepository
from src.ai_sandbox.types import PnlHorizons

logger = logging.getLogger(__name__)


# Horizon definitions in calendar days (we use calendar days, not
# trading days, for simplicity — over 1/3/7/30 day windows the
# difference is small for forward-sim metrics).
_HORIZONS_DAYS = (1, 3, 7, 30)


class AISandboxPnlService:
    """Pure-function service: takes an executions repo + a data
    fetcher, fills in P&L horizons row-by-row."""

    def __init__(
        self,
        *,
        repo: Optional[AISandboxRepository] = None,
    ) -> None:
        self._repo = repo or AISandboxRepository()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def compute_pnl_for_pending(self, *, limit: int = 50) -> Dict[str, int]:
        """Walk pending rows and compute their P&L horizons.

        Returns counters: ``{"scanned": N, "computed": M, "skipped": K}``.
        ``computed`` is the number of rows that successfully wrote
        ``pnl_horizons_json``; ``skipped`` is rows where data was
        unavailable.
        """
        rows = self._repo.find_pending_pnl_computation(
            min_age_days=1, limit=limit,
        )
        counters = {"scanned": len(rows), "computed": 0, "skipped": 0}
        if not rows:
            return counters
        # Lazy fetcher manager — same pattern as cross-sectional service
        try:
            from data_provider import DataFetcherManager
            fetcher = DataFetcherManager()
        except Exception as exc:  # noqa: BLE001
            logger.info(
                "[ai-sandbox-pnl] DataFetcherManager unavailable; skipping batch (%s)",
                exc,
            )
            counters["skipped"] = len(rows)
            return counters

        for row in rows:
            try:
                horizons = self._compute_one(row, fetcher=fetcher)
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "[ai-sandbox-pnl] row %s (%s) compute failed: %s",
                    row.get("id"), row.get("symbol"), exc,
                )
                counters["skipped"] += 1
                continue
            if horizons is None:
                counters["skipped"] += 1
                continue
            ok = self._repo.update_pnl_horizons(row["request_uid"], horizons)
            if ok:
                counters["computed"] += 1
            else:
                counters["skipped"] += 1
        logger.info(
            "[ai-sandbox-pnl] batch done: scanned=%d computed=%d skipped=%d",
            counters["scanned"], counters["computed"], counters["skipped"],
        )
        return counters

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _compute_one(
        self,
        row: Dict[str, Any],
        *,
        fetcher: Any,
    ) -> Optional[PnlHorizons]:
        symbol = row.get("symbol")
        fill_price = row.get("fill_price")
        fill_time_iso = row.get("fill_time")
        side = (row.get("side") or "").lower()
        if not symbol or fill_price is None or not fill_time_iso:
            return None
        fill_dt = self._parse_iso(fill_time_iso)
        if fill_dt is None:
            return None
        fill_d = fill_dt.date()
        # Need OHLCV from fill_d onward. Pull a generous window so all
        # 4 horizons are covered (30 trading + buffer).
        try:
            df, _src = fetcher.get_daily_data(symbol, days=60)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[ai-sandbox-pnl] fetcher.get_daily_data %s failed: %s",
                symbol, exc,
            )
            return None
        if df is None or len(df) == 0:
            return None
        # Build a ``date → close`` map. Frames vary across sources —
        # 'date' and 'close' columns are the lingua franca.
        try:
            date_to_close: Dict[date, float] = {}
            for _, r in df.iterrows():
                d = self._row_date(r)
                c = r.get("close") if hasattr(r, "get") else r["close"]
                if d is not None and c is not None:
                    date_to_close[d] = float(c)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[ai-sandbox-pnl] frame parse %s failed: %s", symbol, exc,
            )
            return None
        if not date_to_close:
            return None

        sorted_dates = sorted(date_to_close.keys())
        # First trading date STRICTLY after fill_d
        future_dates = [d for d in sorted_dates if d > fill_d]
        if not future_dates:
            return None  # Nothing to compute yet

        horizons: Dict[int, Optional[float]] = {h: None for h in _HORIZONS_DAYS}
        prices: Dict[int, Optional[float]] = {h: None for h in _HORIZONS_DAYS}
        for h in _HORIZONS_DAYS:
            target = self._closest_at_or_after(future_dates, fill_d + timedelta(days=h))
            if target is None:
                continue
            close = date_to_close.get(target)
            if close is None or fill_price <= 0:
                continue
            raw_ret = (close - float(fill_price)) / float(fill_price)
            # Convention: BUY positive return = win, SELL positive
            # return = loss. Store the SIGNED return relative to
            # the position direction.
            if side == "sell":
                raw_ret = -raw_ret
            horizons[h] = round(raw_ret * 100, 4)  # %
            prices[h] = float(close)

        return PnlHorizons(
            horizon_1d=horizons[1],
            horizon_3d=horizons[3],
            horizon_7d=horizons[7],
            horizon_30d=horizons[30],
            horizon_1d_price=prices[1],
            horizon_3d_price=prices[3],
            horizon_7d_price=prices[7],
            horizon_30d_price=prices[30],
            computed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

    @staticmethod
    def _parse_iso(s: str) -> Optional[datetime]:
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _row_date(row: Any) -> Optional[date]:
        d = row.get("date") if hasattr(row, "get") else row["date"]
        if d is None:
            return None
        if isinstance(d, date) and not isinstance(d, datetime):
            return d
        if isinstance(d, datetime):
            return d.date()
        try:
            return datetime.fromisoformat(str(d)[:10]).date()
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _closest_at_or_after(
        sorted_dates: List[date], target: date,
    ) -> Optional[date]:
        """Return the smallest date in ``sorted_dates`` that is >= target.
        Used because a "+1 day" horizon may land on a weekend/holiday;
        we step forward to the next trading day."""
        for d in sorted_dates:
            if d >= target:
                return d
        return None
