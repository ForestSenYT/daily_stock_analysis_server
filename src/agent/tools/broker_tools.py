# -*- coding: utf-8 -*-
"""Agent tools for read-only Firstrade snapshots.

Phase-6 contract — what the agent is **allowed** to do:

  * Read the **most recent local snapshot** of accounts, positions,
    balances, open orders, and recent transactions.
  * Surface masked account aliases (``Firstrade ****1234``).
  * Annotate freshness via ``as_of_iso`` and ``age_seconds``; set
    ``status="stale"`` with a warning when older than the caller's
    ``max_age_seconds`` threshold (data is still returned so the LLM
    can reason "stale but here's what we knew").

What the agent is **forbidden** from doing:

  * Logging into Firstrade (the agent never calls
    ``FirstradeReadOnlyClient`` or ``FirstradeSyncService.login``).
  * Triggering ``sync_now`` (manual-only via WebUI / API).
  * Placing orders, cancelling orders, or placing option orders —
    these capabilities are not implemented anywhere in the project,
    and the agent tool registry exposes no such tools.
  * Returning credentials, cookies, tokens, full account numbers, or
    any other key listed in ``brokers.base._REDACT_KEYS``.

Output respects ``BROKER_FIRSTRADE_LLM_DATA_SCOPE``:

  * ``positions_only``        — symbol, qty, market_value, weight_pct,
                                 unrealized_pnl per position.
  * ``positions_and_balances`` — positions + cash / total_value /
                                 buying_power.
  * ``full``                  — positions + balances + open orders +
                                 recent transactions (still no
                                 ``raw_payload`` echoed).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.agent.tools.registry import ToolDefinition, ToolParameter

logger = logging.getLogger(__name__)


# Tighter caps than the HTTP layer so a wandering LLM cannot ask for
# pathologically large windows.
MAX_TRANSACTIONS = 25
MAX_ORDERS = 25
MAX_POSITIONS = 50
DEFAULT_MAX_AGE_SECONDS = 3600  # 1 hour — manual sync cadence


# =====================================================================
# Helpers
# =====================================================================

def _service():
    """Lazy import so importing this module never pulls broker code at
    boot when the feature is disabled."""
    from src.services.firstrade_sync_service import get_firstrade_sync_service
    return get_firstrade_sync_service()


def _config():
    from src.config import get_config
    return get_config()


def _flag_disabled() -> bool:
    try:
        return not bool(getattr(_config(), "broker_firstrade_enabled", False))
    except Exception:  # pragma: no cover — defensive
        logger.exception("[broker_tools] config probe failed")
        return True


_NOT_ENABLED_PAYLOAD: Dict[str, Any] = {
    "status": "not_enabled",
    "message": (
        "Firstrade broker integration is disabled "
        "(BROKER_FIRSTRADE_ENABLED=false). Operator must enable + sync "
        "before the agent can read live broker context."
    ),
}


def _err(message: str, *, code: str = "broker_error") -> Dict[str, Any]:
    return {"status": "failed", "error": code, "message": message}


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(text: Optional[str]) -> Optional[datetime]:
    if not text:
        return None
    try:
        # Allow both "Z" and "+00:00" suffixes.
        clean = text.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (TypeError, ValueError):
        return None


def _age_seconds(as_of: Optional[str]) -> Optional[int]:
    parsed = _parse_iso(as_of)
    if parsed is None:
        return None
    delta = _now_utc() - parsed
    return max(0, int(delta.total_seconds()))


def _resolve_scope() -> str:
    raw = getattr(_config(), "broker_firstrade_llm_data_scope", "") or ""
    cleaned = raw.strip().lower()
    if cleaned in {"positions_only", "positions_and_balances", "full"}:
        return cleaned
    return "positions_and_balances"


# =====================================================================
# Output projection — strictly drop ``payload`` (raw redacted blob)
# and keep only the small set of fields the LLM actually needs.
# =====================================================================

def _project_account(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "account_alias": row.get("account_alias") or "",
        "account_last4": row.get("account_last4") or "",
        "account_hash": row.get("account_hash") or "",
        "as_of": row.get("as_of"),
    }


def _project_balance(row: Dict[str, Any]) -> Dict[str, Any]:
    payload = row.get("payload") or {}
    return {
        "account_alias": row.get("account_alias") or payload.get("account_alias") or "",
        "account_hash": row.get("account_hash") or payload.get("account_hash") or "",
        "cash": payload.get("cash"),
        "buying_power": payload.get("buying_power"),
        "total_value": payload.get("total_value"),
        "currency": payload.get("currency") or "USD",
        "as_of": row.get("as_of"),
    }


def _project_position(
    row: Dict[str, Any], total_value: Optional[float] = None,
) -> Dict[str, Any]:
    payload = row.get("payload") or {}
    market_value = payload.get("market_value")
    weight_pct: Optional[float] = None
    if market_value is not None and total_value:
        try:
            weight_pct = round(float(market_value) / float(total_value) * 100.0, 2)
        except (TypeError, ValueError, ZeroDivisionError):
            weight_pct = None
    return {
        "account_alias": row.get("account_alias") or payload.get("account_alias") or "",
        "account_hash": row.get("account_hash") or payload.get("account_hash") or "",
        "symbol": row.get("symbol") or payload.get("symbol") or "",
        "quantity": payload.get("quantity"),
        "market_value": market_value,
        "avg_cost": payload.get("avg_cost"),
        "last_price": payload.get("last_price"),
        "unrealized_pnl": payload.get("unrealized_pnl"),
        # Same-day move — what the broker UI shows in 变更$ / 变更%.
        # The agent uses these for "is this position trending up or
        # down today?" reasoning; ``unrealized_pnl`` answers a
        # different question (lifetime P&L vs. avg_cost).
        "day_change": payload.get("day_change"),
        "day_change_pct": payload.get("day_change_pct"),
        "currency": payload.get("currency") or "USD",
        "weight_pct": weight_pct,
        "as_of": row.get("as_of"),
    }


def _project_order(row: Dict[str, Any]) -> Dict[str, Any]:
    payload = row.get("payload") or {}
    return {
        "account_alias": row.get("account_alias") or payload.get("account_alias") or "",
        "symbol": row.get("symbol") or payload.get("symbol") or "",
        "order_id_hash": row.get("entity_hash") or payload.get("order_id_hash") or "",
        "order_status": payload.get("order_status"),
        "order_side": payload.get("order_side"),
        "order_type": payload.get("order_type"),
        "order_quantity": payload.get("order_quantity"),
        "filled_quantity": payload.get("filled_quantity"),
        "limit_price": payload.get("limit_price"),
        "as_of": row.get("as_of"),
    }


def _project_transaction(row: Dict[str, Any]) -> Dict[str, Any]:
    payload = row.get("payload") or {}
    return {
        "account_alias": row.get("account_alias") or payload.get("account_alias") or "",
        "transaction_id_hash": row.get("entity_hash") or payload.get("transaction_id_hash") or "",
        "symbol": row.get("symbol") or payload.get("symbol") or "",
        "transaction_type": payload.get("transaction_type"),
        "trade_date": payload.get("trade_date"),
        "settle_date": payload.get("settle_date"),
        "amount": payload.get("amount"),
        "quantity": payload.get("quantity"),
        "currency": payload.get("currency") or "USD",
    }


def _filter_by_alias(
    rows: List[Dict[str, Any]], account_alias: Optional[str],
) -> List[Dict[str, Any]]:
    if not account_alias:
        return rows
    needle = account_alias.strip().lower()
    if not needle:
        return rows
    return [
        r for r in rows
        if needle in (r.get("account_alias") or "").lower()
        or needle in (r.get("account_last4") or "").lower()
        or needle == (r.get("account_hash") or "").lower()
    ]


# =====================================================================
# Tool handler
# =====================================================================

def _handle_get_live_broker_portfolio_snapshot(
    account_alias: str = "",
    include_balances: bool = True,
    include_orders: bool = True,
    include_transactions: bool = False,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
) -> Dict[str, Any]:
    if _flag_disabled():
        return _NOT_ENABLED_PAYLOAD

    try:
        snapshot = _service().get_snapshot()
    except Exception:  # noqa: BLE001 — boundary
        logger.exception("[broker_tools] get_snapshot failed")
        return _err("Failed to read local broker snapshot.")

    if snapshot.get("status") and snapshot.get("status") != "ok":
        # Pass through structured statuses (not_enabled / not_installed
        # / failed) as-is so the LLM gets a clear signal.
        return snapshot

    accounts_raw = snapshot.get("accounts") or []
    balances_raw = snapshot.get("balances") or []
    positions_raw = snapshot.get("positions") or []
    orders_raw = snapshot.get("orders") or []
    transactions_raw = snapshot.get("transactions") or []

    accounts = _filter_by_alias(accounts_raw, account_alias)
    if not accounts and not positions_raw:
        return {
            "status": "no_snapshot",
            "message": (
                "No Firstrade broker snapshot is available yet. "
                "An operator must run a sync from the Portfolio page "
                "(or POST /api/v1/broker/firstrade/sync) before the "
                "agent can read live positions."
            ),
        }

    balances = _filter_by_alias(balances_raw, account_alias)
    positions = _filter_by_alias(positions_raw, account_alias)
    orders = _filter_by_alias(orders_raw, account_alias)
    transactions = _filter_by_alias(transactions_raw, account_alias)

    scope = _resolve_scope()
    as_of = snapshot.get("as_of")
    age = _age_seconds(as_of)
    bound = max(0, int(max_age_seconds or DEFAULT_MAX_AGE_SECONDS))
    is_stale = age is not None and age > bound

    # Per-account totals so position weight can be computed.
    total_value_by_alias: Dict[str, float] = {}
    for b in balances:
        total = b.get("payload", {}).get("total_value") if "payload" in b else b.get("total_value")
        alias = b.get("account_alias") or ""
        if total is None or not alias:
            continue
        try:
            total_value_by_alias[alias] = float(total)
        except (TypeError, ValueError):
            continue

    out: Dict[str, Any] = {
        "status": "stale" if is_stale else "ok",
        "scope": scope,
        "as_of_iso": as_of,
        "age_seconds": age,
        "max_age_seconds": bound,
        "is_research_only": True,
        "trade_orders_emitted": False,
    }
    if is_stale:
        out["warning"] = (
            f"Snapshot is older than {bound}s (age={age}s). Ask the "
            "operator to run a fresh sync if you intend to make "
            "decisions on this data."
        )

    # Always include accounts (small, safe label).
    out["accounts"] = [_project_account(a) for a in accounts]

    # Truncate positions early — the LLM never benefits from a 200-row pool.
    truncated_positions = positions[:MAX_POSITIONS]
    projected_positions: List[Dict[str, Any]] = []
    for pos in truncated_positions:
        alias = pos.get("account_alias") or ""
        projected_positions.append(
            _project_position(pos, total_value=total_value_by_alias.get(alias)),
        )
    out["positions"] = projected_positions
    if len(positions) > MAX_POSITIONS:
        out["positions_truncated"] = True
        out["positions_total"] = len(positions)

    if scope in {"positions_and_balances", "full"} and include_balances:
        out["balances"] = [_project_balance(b) for b in balances]

    if scope == "full" and include_orders:
        truncated_orders = orders[:MAX_ORDERS]
        out["orders"] = [_project_order(o) for o in truncated_orders]
        if len(orders) > MAX_ORDERS:
            out["orders_truncated"] = True
            out["orders_total"] = len(orders)

    if scope == "full" and include_transactions:
        truncated_tx = transactions[:MAX_TRANSACTIONS]
        out["transactions"] = [_project_transaction(t) for t in truncated_tx]
        if len(transactions) > MAX_TRANSACTIONS:
            out["transactions_truncated"] = True
            out["transactions_total"] = len(transactions)

    return out


# =====================================================================
# ToolDefinition
# =====================================================================

get_live_broker_portfolio_snapshot_tool = ToolDefinition(
    name="get_live_broker_portfolio_snapshot",
    description=(
        "Read the most recent **local read-only Firstrade snapshot** "
        "to ground portfolio analysis in the user's actual positions. "
        "This tool does NOT log into Firstrade, does NOT trigger a "
        "sync, and does NOT place / cancel any orders. Returns masked "
        "account aliases (e.g. 'Firstrade ****1234'), positions with "
        "weight_pct, and (depending on configured LLM data scope) "
        "balances / open orders / recent transactions. Always reports "
        "freshness via ``as_of_iso`` + ``age_seconds``; flags "
        "``status='stale'`` with a warning when the snapshot is older "
        "than ``max_age_seconds``."
    ),
    parameters=[
        ToolParameter(
            name="account_alias", type="string", required=False, default="",
            description=(
                "Optional filter — match by alias suffix "
                "(e.g. '1234'), masked alias ('Firstrade ****1234'), "
                "or account_hash. Empty = all accounts."
            ),
        ),
        ToolParameter(
            name="include_balances", type="boolean", required=False, default=True,
            description=(
                "Include cash / buying_power / total_value when scope "
                "is 'positions_and_balances' or 'full'."
            ),
        ),
        ToolParameter(
            name="include_orders", type="boolean", required=False, default=True,
            description=(
                "Include recent open / filled orders when scope is "
                "'full'. Read-only — these are the user's existing "
                "orders, never new ones."
            ),
        ),
        ToolParameter(
            name="include_transactions", type="boolean", required=False, default=False,
            description=(
                "Include recent transactions when scope is 'full'. "
                "Defaults to False to keep payload tight."
            ),
        ),
        ToolParameter(
            name="max_age_seconds", type="integer", required=False,
            default=DEFAULT_MAX_AGE_SECONDS,
            description=(
                "Snapshot freshness threshold in seconds. The tool "
                "still returns the data when stale, but flags "
                "``status='stale'`` with a warning."
            ),
        ),
    ],
    handler=_handle_get_live_broker_portfolio_snapshot,
    category="data",
)


ALL_BROKER_TOOLS = [get_live_broker_portfolio_snapshot_tool]
