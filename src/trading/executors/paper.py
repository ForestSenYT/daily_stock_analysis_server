# -*- coding: utf-8 -*-
"""PaperExecutor — simulated fills against the latest live quote.

Pipeline:
  1. Pull a quote: prefer ``firstrade_sync_service.get_quote(symbol)``
     (real bid/ask incl. extended-hours). Fall back to
     ``DataFetcherManager.get_realtime_quote()`` when the user isn't
     logged in to Firstrade.
  2. Compute a fill price:
       - LIMIT BUY:  fill iff ask <= limit; price = min(ask, limit)
       - LIMIT SELL: fill iff bid >= limit; price = max(bid, limit)
       - MARKET BUY:  ask * (1 + slippage_bps/10000)
       - MARKET SELL: bid * (1 - slippage_bps/10000)
  3. Persist via ``PortfolioService.record_trade(source='paper',
     trade_uid=request.request_uid, ...)``. The ``source='paper'``
     tag keeps the trade out of real-money aggregates.
  4. Return a frozen ``OrderResult`` carrying the executor's view of
     the world (fill price, fill quantity, the quote that was used
     for verifiability).

Hard rules:
  * NO ``from firstrade import order`` / ``trade`` import — paper
    trades never touch the real broker.
  * Best-effort everywhere: if the quote chain fails, return
    ``FAILED + QUOTE_UNAVAILABLE`` instead of raising — the audit row
    captures the structured reason.
  * Idempotent: the trade_uid is reused as ``request_uid`` so a
    retry with the same UID hits ``DuplicateTradeUidError`` at the
    portfolio repo and returns ``FAILED + DUPLICATE_REQUEST``.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any, Dict, Optional

from src.trading.executors.base import BaseExecutor
from src.trading.types import (
    ExecutionMode,
    ExecutionStatus,
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderType,
    RiskAssessment,
)

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class PaperExecutor(BaseExecutor):
    """Simulate fills using the latest live quote + slippage."""

    def submit(
        self,
        request: OrderRequest,
        risk_assessment: Optional[RiskAssessment] = None,
    ) -> OrderResult:
        # 1. Pull the quote
        quote = self._resolve_quote(request)
        if quote is None or self._extract_price(quote, side=request.side) is None:
            return OrderResult(
                request=request,
                status=ExecutionStatus.FAILED,
                mode=ExecutionMode.PAPER,
                risk_assessment=risk_assessment,
                error_code="QUOTE_UNAVAILABLE",
                error_message=(
                    f"No real-time quote available for {request.symbol!r} "
                    "(checked Firstrade then data-provider chain)."
                ),
            )

        # 2. Derive fill price
        try:
            fill_price = self._derive_fill_price(request, quote)
        except _NotFillable as exc:
            # LIMIT order whose limit price is on the wrong side of
            # the spread — paper executor honours the constraint and
            # returns FAILED (rather than blindly filling).
            return OrderResult(
                request=request,
                status=ExecutionStatus.FAILED,
                mode=ExecutionMode.PAPER,
                risk_assessment=risk_assessment,
                error_code="LIMIT_NOT_REACHABLE",
                error_message=str(exc),
                quote_payload=quote,
            )
        except Exception as exc:  # noqa: BLE001 — defensive boundary
            logger.exception("[paper-executor] fill-price computation failed")
            return OrderResult(
                request=request,
                status=ExecutionStatus.FAILED,
                mode=ExecutionMode.PAPER,
                risk_assessment=risk_assessment,
                error_code="FILL_PRICE_FAILED",
                error_message=str(exc)[:240],
                quote_payload=quote,
            )

        # 3. Persist via PortfolioService (source='paper')
        try:
            from src.services.portfolio_service import (
                PortfolioOversellError,
                PortfolioService,
                PortfolioConflictError,
            )
            svc = PortfolioService()
            fee = float(getattr(self._config, "trading_paper_fee_per_trade", 0.0) or 0.0)
            account_id = self._resolve_account_id(request)
            if account_id is None:
                return OrderResult(
                    request=request,
                    status=ExecutionStatus.FAILED,
                    mode=ExecutionMode.PAPER,
                    risk_assessment=risk_assessment,
                    error_code="ACCOUNT_REQUIRED",
                    error_message=(
                        "OrderRequest.account_id is required for paper "
                        "trades (no Config.trading_paper_account_id default set)."
                    ),
                    quote_payload=quote,
                )
            trade_id_dict = svc.record_trade(
                account_id=account_id,
                symbol=request.symbol,
                trade_date=date.today(),
                side=request.side.value,
                quantity=float(request.quantity),
                price=fill_price,
                fee=fee,
                tax=0.0,
                market=request.market,
                currency=request.currency,
                trade_uid=request.request_uid,
                note=f"[paper] {request.note or ''}".strip(),
                source="paper",
            )
        except PortfolioOversellError as exc:
            return OrderResult(
                request=request,
                status=ExecutionStatus.BLOCKED,
                mode=ExecutionMode.PAPER,
                risk_assessment=risk_assessment,
                error_code="OVERSELL",
                error_message=str(exc)[:240],
                quote_payload=quote,
            )
        except PortfolioConflictError as exc:
            return OrderResult(
                request=request,
                status=ExecutionStatus.FAILED,
                mode=ExecutionMode.PAPER,
                risk_assessment=risk_assessment,
                error_code="DUPLICATE_REQUEST",
                error_message=str(exc)[:240],
                quote_payload=quote,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[paper-executor] record_trade failed")
            return OrderResult(
                request=request,
                status=ExecutionStatus.FAILED,
                mode=ExecutionMode.PAPER,
                risk_assessment=risk_assessment,
                error_code="RECORD_TRADE_FAILED",
                error_message=str(exc)[:240],
                quote_payload=quote,
            )

        # 4. Compose the result
        return OrderResult(
            request=request,
            status=ExecutionStatus.FILLED,
            mode=ExecutionMode.PAPER,
            fill_price=fill_price,
            fill_quantity=float(request.quantity),
            fill_time=_utc_now_iso(),
            realised_fee=float(getattr(self._config, "trading_paper_fee_per_trade", 0.0) or 0.0),
            realised_tax=0.0,
            risk_assessment=risk_assessment,
            portfolio_trade_id=int(trade_id_dict.get("id")) if trade_id_dict else None,
            quote_payload=quote,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _resolve_account_id(self, request: OrderRequest) -> Optional[int]:
        if request.account_id is not None:
            return int(request.account_id)
        cfg_default = getattr(self._config, "trading_paper_account_id", None)
        if cfg_default is not None:
            return int(cfg_default)
        return None

    def _resolve_quote(self, request: OrderRequest) -> Optional[Dict[str, Any]]:
        """Try Firstrade first (real bid/ask incl. extended hours);
        fall back to the data-provider chain. Both calls are
        defensive — exceptions become ``None`` so the caller can return
        a structured ``QUOTE_UNAVAILABLE`` result."""
        # 1. Firstrade real-time quote (best for US extended hours).
        try:
            from src.services.firstrade_sync_service import (
                get_firstrade_sync_service,
            )
            svc = get_firstrade_sync_service()
            ft_quote = svc.get_quote(request.symbol) if svc else None
            if ft_quote and (ft_quote.get("last") or ft_quote.get("bid") or ft_quote.get("ask")):
                return self._normalize_firstrade_quote(ft_quote)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[paper-executor] Firstrade quote unavailable for %s: %s",
                request.symbol, exc,
            )

        # 2. Fallback to DataFetcherManager
        try:
            from data_provider import DataFetcherManager
            mgr = DataFetcherManager()
            rt = mgr.get_realtime_quote(request.symbol, log_final_failure=False)
            if rt is not None:
                return self._normalize_realtime_quote(rt)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[paper-executor] data_provider quote unavailable for %s: %s",
                request.symbol, exc,
            )
        return None

    @staticmethod
    def _normalize_firstrade_quote(q: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize Firstrade SymbolQuote-shaped dict to a common schema.
        Always contains ``bid``, ``ask``, ``last`` (any/all may be None)."""
        def _to_float(v: Any) -> Optional[float]:
            if v in (None, "", "—", "-"):
                return None
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
        return {
            "source": "firstrade",
            "symbol": str(q.get("symbol", "")).upper(),
            "bid": _to_float(q.get("bid")),
            "ask": _to_float(q.get("ask")),
            "last": _to_float(q.get("last")),
            "high": _to_float(q.get("high")),
            "low": _to_float(q.get("low")),
            "today_close": _to_float(q.get("today_close")),
            "open": _to_float(q.get("open")),
            "volume": q.get("volume"),
            "quote_time": q.get("quote_time"),
            "realtime": q.get("realtime"),
        }

    @staticmethod
    def _normalize_realtime_quote(rt: Any) -> Dict[str, Any]:
        """Normalize a UnifiedRealtimeQuote-shaped object/dict."""
        if hasattr(rt, "to_dict"):
            d = rt.to_dict()
        elif isinstance(rt, dict):
            d = dict(rt)
        else:
            return {"source": "fallback", "last": None, "bid": None, "ask": None}
        last = d.get("price") or d.get("last") or d.get("close")
        return {
            "source": d.get("source") or "fallback",
            "symbol": str(d.get("code") or d.get("symbol") or "").upper(),
            "bid": last,  # data-provider chains rarely give bid/ask
            "ask": last,
            "last": last,
            "high": d.get("high"),
            "low": d.get("low"),
            "open": d.get("open_price") or d.get("open"),
            "today_close": d.get("pre_close"),
            "volume": d.get("volume"),
            "change_pct": d.get("change_pct"),
        }

    @staticmethod
    def _extract_price(quote: Dict[str, Any], *, side: OrderSide) -> Optional[float]:
        """Pick the canonical price for the given side. Falls back to
        ``last`` if bid/ask are missing — better than nothing for a
        paper fill."""
        if side == OrderSide.BUY:
            return quote.get("ask") or quote.get("last")
        return quote.get("bid") or quote.get("last")

    def _derive_fill_price(
        self,
        request: OrderRequest,
        quote: Dict[str, Any],
    ) -> float:
        ask = quote.get("ask") or quote.get("last")
        bid = quote.get("bid") or quote.get("last")
        slip_bps = float(getattr(self._config, "trading_paper_slippage_bps", 0) or 0)
        slip = slip_bps / 10000.0

        if request.order_type == OrderType.MARKET:
            if request.side == OrderSide.BUY:
                if ask is None:
                    raise _NotFillable("MARKET BUY: no ask price")
                return round(float(ask) * (1.0 + slip), 6)
            if bid is None:
                raise _NotFillable("MARKET SELL: no bid price")
            return round(float(bid) * (1.0 - slip), 6)

        # LIMIT
        if request.limit_price is None:
            raise _NotFillable("LIMIT order missing limit_price")
        limit = float(request.limit_price)
        if request.side == OrderSide.BUY:
            if ask is None:
                raise _NotFillable("LIMIT BUY: no ask price")
            if float(ask) > limit:
                raise _NotFillable(
                    f"LIMIT BUY: ask {ask} > limit {limit}; not fillable"
                )
            return round(min(float(ask), limit), 6)
        if bid is None:
            raise _NotFillable("LIMIT SELL: no bid price")
        if float(bid) < limit:
            raise _NotFillable(
                f"LIMIT SELL: bid {bid} < limit {limit}; not fillable"
            )
        return round(max(float(bid), limit), 6)


class _NotFillable(Exception):
    """Internal: limit price not on the right side of the spread."""
