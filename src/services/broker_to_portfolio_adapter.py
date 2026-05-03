# -*- coding: utf-8 -*-
"""Adapter: BrokerSnapshot → PortfolioAccountSnapshot shape.

The PortfolioPage (and its risk / allocation / chart consumers) speak
the dict shape produced by :class:`PortfolioService.get_portfolio_snapshot`.
The Firstrade read-only sync writes its data into the
``broker_snapshots`` table in a different shape (per-row JSON payloads
keyed by ``account_hash`` + ``snapshot_type``).

This module is a **pure translator** with zero side effects:
  * No DB writes.
  * No Firstrade calls.
  * No FX conversion (the caller — PortfolioService — owns FX).
  * No double-counting: each broker account becomes a single
    ``PortfolioAccountSnapshot`` with its own synthetic negative
    ``account_id`` derived deterministically from ``account_hash``,
    so it can never collide with a real (positive auto-incremented)
    account row.

The synthetic ``account_id`` is:

    account_id = -(int(account_hash[:8], 16) & 0x7FFFFFFF) - 1

Always negative, always in ``[-(2**31), -1]``, stable across calls so
the frontend can keep selection / drilldown state by id.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# Field constants — mirror PortfolioPositionItem.price_source enum.
_PRICE_SOURCE_BROKER = "broker_live"
_PRICE_PROVIDER_FIRSTRADE = "firstrade"


def _safe_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _synthetic_account_id(account_hash: str) -> int:
    """Map a 16-char hex account_hash to a stable negative int that
    cannot collide with any real ``portfolio_accounts.id``."""
    if not account_hash:
        # Fall back to a fixed sentinel so the row is still distinct
        # from any real account.
        return -1
    try:
        # Take the first 8 hex chars (32 bits), mask to positive, negate.
        n = int(account_hash[:8], 16) & 0x7FFFFFFF
        return -(n + 1)  # +1 so we never return 0
    except ValueError:
        return -1


def _adapt_position(
    row: Dict[str, Any],
    *,
    base_currency: str,
) -> Dict[str, Any]:
    """Translate one broker ``snapshot_type='position'`` row into
    a ``PortfolioPositionItem`` dict (snake_case)."""
    payload = row.get("payload") or {}
    symbol = (
        row.get("symbol")
        or payload.get("symbol")
        or payload.get("ticker")
        or ""
    )
    quantity = _safe_float(payload.get("quantity"))
    avg_cost = _safe_float(payload.get("avg_cost"))
    last_price = _safe_float(payload.get("last_price"))
    market_value = _safe_float(payload.get("market_value"))
    unrealized_pnl = _safe_float(payload.get("unrealized_pnl"))
    if not market_value and quantity and last_price:
        market_value = quantity * last_price
    total_cost = avg_cost * quantity if quantity else 0.0
    return {
        "symbol": str(symbol).upper(),
        "market": "us",  # Firstrade is US-only
        "currency": base_currency,
        "quantity": quantity,
        "avg_cost": avg_cost,
        "total_cost": total_cost,
        "last_price": last_price,
        "market_value_base": market_value,
        "unrealized_pnl_base": unrealized_pnl,
        "valuation_currency": base_currency,
        "price_source": _PRICE_SOURCE_BROKER,
        "price_provider": _PRICE_PROVIDER_FIRSTRADE,
        "price_date": row.get("as_of") or payload.get("as_of"),
        "price_stale": False,
        "price_available": bool(last_price),
    }


def _balance_for_account(
    balances: List[Dict[str, Any]], account_hash: str,
) -> Dict[str, Any]:
    for bal in balances:
        if bal.get("account_hash") == account_hash:
            return bal.get("payload") or {}
    return {}


def _positions_for_account(
    positions: List[Dict[str, Any]], account_hash: str,
) -> List[Dict[str, Any]]:
    return [p for p in positions if p.get("account_hash") == account_hash]


def broker_snapshot_to_portfolio_accounts(
    snapshot: Optional[Dict[str, Any]],
    *,
    base_currency: str = "USD",
    cost_method: str = "live",
) -> List[Dict[str, Any]]:
    """Translate a broker snapshot dict into a list of
    ``PortfolioAccountSnapshot``-shaped dicts (one per broker account).

    The output dict mirrors what :meth:`PortfolioService.get_portfolio_snapshot`
    appends to ``accounts_payload``. Aggregates (cash, market value,
    equity, unrealized PnL) are computed in the broker's native
    currency; FX conversion is the caller's responsibility.

    Returns an empty list when ``snapshot`` is None / has no
    accounts — the caller can safely concatenate without checks.
    """
    if not snapshot or not isinstance(snapshot, dict):
        return []
    accounts = snapshot.get("accounts") or []
    if not accounts:
        return []

    balances = snapshot.get("balances") or []
    positions = snapshot.get("positions") or []
    broker_name = snapshot.get("broker") or "firstrade"
    snapshot_as_of = snapshot.get("as_of")

    out: List[Dict[str, Any]] = []
    for account_row in accounts:
        account_hash = account_row.get("account_hash") or ""
        account_alias = (
            account_row.get("account_alias")
            or f"{broker_name.title()} ****{account_row.get('account_last4') or '????'}"
        )
        account_balance = _balance_for_account(balances, account_hash)
        raw_positions = _positions_for_account(positions, account_hash)

        adapted_positions = [
            _adapt_position(p, base_currency=base_currency)
            for p in raw_positions
        ]
        # Filter out zero-quantity rows so the UI doesn't show empty
        # placeholders — that matches the existing portfolio behaviour.
        adapted_positions = [
            p for p in adapted_positions if p.get("quantity")
        ]

        total_market_value = sum(
            _safe_float(p.get("market_value_base")) for p in adapted_positions
        )
        unrealized_pnl = sum(
            _safe_float(p.get("unrealized_pnl_base")) for p in adapted_positions
        )
        total_cash = _safe_float(account_balance.get("cash"))
        # Prefer broker-reported total_value; fall back to cash + MV.
        total_equity = _safe_float(account_balance.get("total_value"))
        if not total_equity:
            total_equity = total_cash + total_market_value

        out.append({
            "account_id": _synthetic_account_id(account_hash),
            "account_name": account_alias,
            "owner_id": None,
            "broker": broker_name,
            "market": "us",
            "base_currency": base_currency,
            "as_of": (
                snapshot_as_of
                or account_row.get("as_of")
                or ""
            ),
            "cost_method": cost_method,
            "total_cash": round(total_cash, 6),
            "total_market_value": round(total_market_value, 6),
            "total_equity": round(total_equity, 6),
            "realized_pnl": 0.0,  # not tracked in broker snapshot
            "unrealized_pnl": round(unrealized_pnl, 6),
            "fee_total": 0.0,
            "tax_total": 0.0,
            "fx_stale": False,
            "positions": adapted_positions,
        })
    return out
