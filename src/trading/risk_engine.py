# -*- coding: utf-8 -*-
"""Pre-trade risk engine — Phase A.

Pure function over (config, portfolio_snapshot, broker_status, now).
No DB writes. No broker calls. The orchestrator runs it before any
executor sees an OrderRequest.

Six hard checks (in evaluation order):

  1. **Parameter sanity** — quantity > 0, limit_price for LIMIT, etc.
  2. **Symbol allowlist / denylist** — denylist always wins.
  3. **Market hours** — strict mode rejects orders when the symbol's
     market is closed; non-strict mode emits an INFO flag and allows.
  4. **Position size limits** — BUY only:
        - absolute: `quantity * estimated_price <= trading_max_position_value`
        - percent : new total `<= trading_max_position_pct * portfolio_total_equity`
  5. **Sell-side oversell** — refuse SELL > held quantity.
  6. **Daily turnover cap** — sum of today's pending+filled rows must
     not exceed `trading_max_daily_turnover` after this trade.

Plus an info-only check (Phase A): broker session liveness. In Phase B
this is upgraded to a hard block for `live` mode.

The output ``RiskAssessment`` carries a frozen ``config_snapshot`` of
every threshold applied — lets future audits replay decisions without
having to know what env was loaded at the moment.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.trading.types import (
    OrderRequest,
    OrderSide,
    OrderType,
    RiskAssessment,
    RiskFlag,
    RiskFlagCode,
    RiskSeverity,
)

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Markets we know how to evaluate. Anything else falls back to "always
# open" (with an INFO flag so operators notice).
_KNOWN_MARKETS = {"us", "cn", "hk"}


class RiskEngine:
    """Stateless evaluator. Construct once per-process or per-call —
    the heavy lifting (config snapshot, portfolio fetch) is the
    *caller's* job.
    """

    def __init__(self, config: Any) -> None:
        self._config = config

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def evaluate(
        self,
        request: OrderRequest,
        *,
        portfolio_snapshot: Optional[Dict[str, Any]] = None,
        broker_status: Optional[Dict[str, Any]] = None,
        daily_turnover_so_far: float = 0.0,
        estimated_price: Optional[float] = None,
        now: Optional[datetime] = None,
    ) -> RiskAssessment:
        """Run all checks and return a frozen :class:`RiskAssessment`.

        Parameters
        ----------
        request:
            The intent to evaluate.
        portfolio_snapshot:
            Output of ``PortfolioService.get_portfolio_snapshot()`` for
            the relevant ``account_id`` — used for position size, pct
            and oversell checks. ``None`` skips those checks (treated
            as "no holdings yet").
        broker_status:
            Output of ``firstrade_sync_service.get_status()`` — used
            for the info-only ``BROKER_NOT_LOGGED_IN`` flag.
        daily_turnover_so_far:
            Sum already booked today (paper or live, depending on mode).
            The engine adds this trade's notional and compares against
            ``trading_max_daily_turnover``.
        estimated_price:
            Caller-supplied price for valuation when ``order_type=MARKET``
            (e.g. last-known quote). When ``None`` for a MARKET order,
            the position-size check is **skipped with a WARNING flag**
            rather than blocking — the caller knows the conditions.
        now:
            Override for deterministic tests. Defaults to UTC now.
        """
        flags: List[RiskFlag] = []
        config_snapshot = self._snapshot_config()

        # 1. Parameter sanity
        flags.extend(self._check_parameters(request))

        # 2. Allowlist / denylist
        flags.extend(self._check_symbol_lists(request))

        # 3. Market hours
        flags.extend(self._check_market_hours(request, now=now))

        # 4. Position size (BUY only) — needs estimated_price
        flags.extend(self._check_position_size(
            request, portfolio_snapshot=portfolio_snapshot,
            estimated_price=estimated_price,
        ))

        # 5. Sell-side oversell
        flags.extend(self._check_oversell(
            request, portfolio_snapshot=portfolio_snapshot,
        ))

        # 6. Daily turnover
        flags.extend(self._check_daily_turnover(
            request, daily_turnover_so_far=daily_turnover_so_far,
            estimated_price=estimated_price,
        ))

        # Info-only: broker session liveness
        flags.extend(self._check_broker_status(broker_status))

        decision = (
            "block" if any(f.severity == RiskSeverity.BLOCK for f in flags)
            else "allow"
        )
        return RiskAssessment(
            flags=flags,
            decision=decision,
            evaluated_at=_utc_now_iso(),
            config_snapshot=config_snapshot,
        )

    # ------------------------------------------------------------------
    # Individual checks
    # ------------------------------------------------------------------

    def _check_parameters(self, request: OrderRequest) -> List[RiskFlag]:
        flags: List[RiskFlag] = []
        if request.quantity is None or request.quantity <= 0:
            flags.append(RiskFlag(
                code=RiskFlagCode.INVALID_PARAMETERS,
                severity=RiskSeverity.BLOCK,
                message=f"quantity must be > 0, got {request.quantity!r}",
            ))
        if request.order_type == OrderType.LIMIT:
            if request.limit_price is None or request.limit_price <= 0:
                flags.append(RiskFlag(
                    code=RiskFlagCode.INVALID_PARAMETERS,
                    severity=RiskSeverity.BLOCK,
                    message="LIMIT order requires limit_price > 0",
                ))
        if not request.symbol:
            flags.append(RiskFlag(
                code=RiskFlagCode.INVALID_PARAMETERS,
                severity=RiskSeverity.BLOCK,
                message="symbol is required",
            ))
        return flags

    def _check_symbol_lists(self, request: OrderRequest) -> List[RiskFlag]:
        flags: List[RiskFlag] = []
        symbol = (request.symbol or "").upper()
        denylist = [s.upper() for s in (self._config.trading_symbol_denylist or [])]
        if symbol in denylist:
            flags.append(RiskFlag(
                code=RiskFlagCode.SYMBOL_DENYLISTED,
                severity=RiskSeverity.BLOCK,
                message=f"symbol {symbol!r} is in trading_symbol_denylist",
            ))
            return flags  # denylist short-circuits
        allowlist = [s.upper() for s in (self._config.trading_symbol_allowlist or [])]
        if allowlist and symbol not in allowlist:
            flags.append(RiskFlag(
                code=RiskFlagCode.SYMBOL_NOT_ALLOWED,
                severity=RiskSeverity.BLOCK,
                message=f"symbol {symbol!r} is not in trading_symbol_allowlist",
            ))
        return flags

    def _check_market_hours(
        self,
        request: OrderRequest,
        *,
        now: Optional[datetime] = None,
    ) -> List[RiskFlag]:
        market = (request.market or "").lower()
        strict = bool(getattr(self._config, "trading_market_hours_strict", True))
        if not market or market not in _KNOWN_MARKETS:
            return [RiskFlag(
                code=RiskFlagCode.MARKET_CLOSED,
                severity=RiskSeverity.INFO,
                message=(
                    f"market {market!r} is not recognised; market-hours "
                    "check skipped"
                ),
            )]
        is_open = self._is_market_open(market, now=now)
        if is_open:
            return []
        severity = RiskSeverity.BLOCK if strict else RiskSeverity.INFO
        return [RiskFlag(
            code=RiskFlagCode.MARKET_CLOSED,
            severity=severity,
            message=(
                f"{market.upper()} market is currently closed"
                + ("" if strict else " (strict mode disabled — order allowed)")
            ),
        )]

    def _is_market_open(self, market: str, *, now: Optional[datetime] = None) -> bool:
        """Lightweight session check.

        We deliberately don't call into ``exchange-calendars`` here
        (heavy + holiday-aware) — for risk-gate purposes a UTC-hour
        approximation is good enough. Holidays slip through; that's
        fine for paper trading because the price provider will yield
        a stale quote anyway. Phase B can swap in the proper calendar.
        """
        anchor = now or datetime.now(timezone.utc)
        if anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=timezone.utc)
        hour_utc = anchor.hour + anchor.minute / 60.0
        weekday = anchor.weekday()  # 0=Mon .. 6=Sun
        if market == "us":
            # US regular session: 14:30-21:00 UTC, Mon-Fri
            return weekday < 5 and 14.5 <= hour_utc <= 21.0
        if market == "cn":
            # A-share: 01:30-03:30 UTC, 05:00-07:00 UTC, Mon-Fri
            return weekday < 5 and (
                1.5 <= hour_utc <= 3.5 or 5.0 <= hour_utc <= 7.0
            )
        if market == "hk":
            # HK: 01:30-04:00 UTC, 05:00-08:00 UTC, Mon-Fri
            return weekday < 5 and (
                1.5 <= hour_utc <= 4.0 or 5.0 <= hour_utc <= 8.0
            )
        return True  # unknown markets fall through; covered by separate flag

    def _check_position_size(
        self,
        request: OrderRequest,
        *,
        portfolio_snapshot: Optional[Dict[str, Any]],
        estimated_price: Optional[float],
    ) -> List[RiskFlag]:
        # Position-size limits only meaningful for BUY.
        if request.side != OrderSide.BUY:
            return []
        # Pick the price for valuation: limit_price for LIMIT,
        # estimated_price (caller-provided last-known quote) for MARKET.
        price: Optional[float] = None
        if request.order_type == OrderType.LIMIT:
            price = request.limit_price
        elif estimated_price is not None:
            price = float(estimated_price)
        if price is None or price <= 0:
            return [RiskFlag(
                code=RiskFlagCode.INVALID_PARAMETERS,
                severity=RiskSeverity.WARNING,
                message=(
                    "position-size check skipped: no price available "
                    "(provide estimated_price for MARKET orders)"
                ),
            )]
        notional = float(request.quantity) * price
        flags: List[RiskFlag] = []
        max_value = float(getattr(self._config, "trading_max_position_value", 0.0))
        if max_value > 0 and notional > max_value:
            flags.append(RiskFlag(
                code=RiskFlagCode.POSITION_SIZE_EXCEEDED,
                severity=RiskSeverity.BLOCK,
                message=(
                    f"order notional ${notional:,.2f} exceeds "
                    f"trading_max_position_value ${max_value:,.2f}"
                ),
                detail={"notional": notional, "max": max_value},
            ))
        # Pct-of-equity check
        max_pct = float(getattr(self._config, "trading_max_position_pct", 0.0))
        if max_pct > 0 and portfolio_snapshot:
            equity = float(
                portfolio_snapshot.get("total_equity")
                or portfolio_snapshot.get("total_market_value")
                or 0.0
            )
            if equity > 0:
                pct = notional / equity
                if pct > max_pct:
                    flags.append(RiskFlag(
                        code=RiskFlagCode.POSITION_PCT_EXCEEDED,
                        severity=RiskSeverity.BLOCK,
                        message=(
                            f"order notional ${notional:,.2f} would be "
                            f"{pct*100:.1f}% of equity (max {max_pct*100:.1f}%)"
                        ),
                        detail={"notional": notional, "equity": equity, "pct": pct, "max_pct": max_pct},
                    ))
        return flags

    def _check_oversell(
        self,
        request: OrderRequest,
        *,
        portfolio_snapshot: Optional[Dict[str, Any]],
    ) -> List[RiskFlag]:
        if request.side != OrderSide.SELL:
            return []
        if not portfolio_snapshot:
            # No snapshot → can't tell. Emit a WARNING so the caller
            # knows we couldn't enforce, but don't block (the
            # PortfolioService.record_trade has a hard oversell guard
            # at the repo layer as belt-and-suspenders).
            return [RiskFlag(
                code=RiskFlagCode.OVERSELL,
                severity=RiskSeverity.WARNING,
                message=(
                    "oversell check skipped: no portfolio snapshot "
                    "(repo-layer guard remains in effect)"
                ),
            )]
        held = self._held_quantity(portfolio_snapshot, request.symbol)
        if request.quantity > held:
            return [RiskFlag(
                code=RiskFlagCode.OVERSELL,
                severity=RiskSeverity.BLOCK,
                message=(
                    f"sell quantity {request.quantity} exceeds "
                    f"held {held} for {request.symbol.upper()}"
                ),
                detail={"requested": float(request.quantity), "held": float(held)},
            )]
        return []

    @staticmethod
    def _held_quantity(snapshot: Dict[str, Any], symbol: str) -> float:
        """Walk the portfolio snapshot's positions and return total
        held quantity for ``symbol``. Snapshot shape is the same one
        that ``PortfolioService.get_portfolio_snapshot`` produces."""
        if not isinstance(snapshot, dict):
            return 0.0
        target = (symbol or "").upper()
        total = 0.0
        # The snapshot may be the per-account block OR the aggregate
        # (containing accounts[]). Handle both.
        if "positions" in snapshot:
            blocks = [snapshot]
        else:
            blocks = snapshot.get("accounts") or []
        for block in blocks:
            for pos in (block.get("positions") or []):
                if not isinstance(pos, dict):
                    continue
                if str(pos.get("symbol", "")).upper() == target:
                    try:
                        total += float(pos.get("quantity") or 0.0)
                    except (TypeError, ValueError):
                        continue
        return total

    def _check_daily_turnover(
        self,
        request: OrderRequest,
        *,
        daily_turnover_so_far: float,
        estimated_price: Optional[float],
    ) -> List[RiskFlag]:
        max_turnover = float(getattr(self._config, "trading_max_daily_turnover", 0.0))
        if max_turnover <= 0:
            return []
        # Estimate this order's notional: limit price for LIMIT, caller
        # quote for MARKET; if neither, skip (warning).
        price: Optional[float] = None
        if request.order_type == OrderType.LIMIT:
            price = request.limit_price
        elif estimated_price is not None:
            price = float(estimated_price)
        if price is None or price <= 0:
            return [RiskFlag(
                code=RiskFlagCode.DAILY_TURNOVER_EXCEEDED,
                severity=RiskSeverity.WARNING,
                message=(
                    "turnover check skipped: no price for MARKET order "
                    "(provide estimated_price)"
                ),
            )]
        order_notional = float(request.quantity) * price
        new_total = float(daily_turnover_so_far) + order_notional
        if new_total > max_turnover:
            return [RiskFlag(
                code=RiskFlagCode.DAILY_TURNOVER_EXCEEDED,
                severity=RiskSeverity.BLOCK,
                message=(
                    f"daily turnover ${new_total:,.2f} would exceed "
                    f"trading_max_daily_turnover ${max_turnover:,.2f}"
                ),
                detail={
                    "today": daily_turnover_so_far,
                    "this_order": order_notional,
                    "new_total": new_total,
                    "max": max_turnover,
                },
            )]
        return []

    def _check_broker_status(
        self,
        broker_status: Optional[Dict[str, Any]],
    ) -> List[RiskFlag]:
        # Phase A: info-only. Phase B: hard block when mode=live.
        if not broker_status:
            return []
        logged_in = bool(broker_status.get("logged_in"))
        if not logged_in:
            return [RiskFlag(
                code=RiskFlagCode.BROKER_NOT_LOGGED_IN,
                severity=RiskSeverity.INFO,
                message=(
                    "broker session is not logged in; paper mode does "
                    "not require it. Live mode (Phase B) will block."
                ),
                detail={
                    "broker": broker_status.get("broker"),
                    "status": broker_status.get("status"),
                },
            )]
        return []

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _snapshot_config(self) -> Dict[str, Any]:
        c = self._config
        return {
            "trading_mode": getattr(c, "trading_mode", "disabled"),
            "trading_max_position_value": float(
                getattr(c, "trading_max_position_value", 0.0)
            ),
            "trading_max_position_pct": float(
                getattr(c, "trading_max_position_pct", 0.0)
            ),
            "trading_max_daily_turnover": float(
                getattr(c, "trading_max_daily_turnover", 0.0)
            ),
            "trading_symbol_allowlist": list(
                getattr(c, "trading_symbol_allowlist", []) or []
            ),
            "trading_symbol_denylist": list(
                getattr(c, "trading_symbol_denylist", []) or []
            ),
            "trading_market_hours_strict": bool(
                getattr(c, "trading_market_hours_strict", True)
            ),
            "trading_paper_slippage_bps": int(
                getattr(c, "trading_paper_slippage_bps", 0)
            ),
            "trading_paper_fee_per_trade": float(
                getattr(c, "trading_paper_fee_per_trade", 0.0)
            ),
        }
