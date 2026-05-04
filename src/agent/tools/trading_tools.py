# -*- coding: utf-8 -*-
"""Agent tools for the Phase A trading framework.

**Strict contract** — what this tool is allowed to do:

  * Build an ``OrderRequest``-shaped dict from agent-provided
    parameters (symbol, side, quantity, order_type, limit_price,
    market, etc.).
  * Tag the dict with ``source='agent'`` and an
    ``agent_session_id`` so the eventual submission is auditable.
  * Generate a fresh ``request_uid`` (UUID4) so the user can submit
    the proposal idempotently.

**What this tool MUST NOT do:**

  * Call ``TradingExecutionService.submit`` (or any executor). The
    user is the only entity allowed to submit in Phase A.
  * Touch ``firstrade.order`` / ``firstrade.trade`` — banned
    package-wide.
  * Persist anything (no audit row, no portfolio_trade write).

The user reviews the emitted intent in the WebUI and clicks
"提交" → that's the only path that calls the submit endpoint.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, Optional

from src.agent.tools.registry import ToolDefinition, ToolParameter

logger = logging.getLogger(__name__)


# =====================================================================
# Helpers
# =====================================================================


def _config():
    from src.config import get_config
    return get_config()


def _is_disabled() -> bool:
    try:
        return getattr(_config(), "trading_mode", "disabled") == "disabled"
    except Exception:  # pragma: no cover — defensive
        logger.exception("[trading_tools] config probe failed")
        return True


_NOT_ENABLED_PAYLOAD: Dict[str, Any] = {
    "status": "not_enabled",
    "message": (
        "Trading framework is disabled (TRADING_MODE=disabled). "
        "Operator must enable paper or live mode before the agent can "
        "emit trade intents."
    ),
}


def _err(message: str, *, code: str = "trading_tool_error") -> Dict[str, Any]:
    return {"status": "failed", "error": code, "message": message}


# =====================================================================
# propose_trade — emit-only intent builder
# =====================================================================

def _handle_propose_trade(
    symbol: str,
    side: str,
    quantity: float,
    order_type: str = "market",
    limit_price: Optional[float] = None,
    time_in_force: str = "day",
    market: Optional[str] = None,
    currency: Optional[str] = None,
    note: Optional[str] = None,
    account_id: Optional[int] = None,
    agent_session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a structured ``OrderRequest`` intent dict. No execution.

    The user reviews + submits via the WebUI. The agent's job ends at
    emitting the intent — it CANNOT call submit.
    """
    if _is_disabled():
        return dict(_NOT_ENABLED_PAYLOAD)

    side_norm = (side or "").strip().lower()
    if side_norm not in ("buy", "sell"):
        return _err(
            f"side must be 'buy' or 'sell', got {side!r}",
            code="invalid_side",
        )
    order_type_norm = (order_type or "market").strip().lower()
    if order_type_norm not in ("market", "limit"):
        return _err(
            f"order_type must be 'market' or 'limit', got {order_type!r}",
            code="invalid_order_type",
        )
    tif_norm = (time_in_force or "day").strip().lower()
    if tif_norm not in ("day", "gtc"):
        return _err(
            f"time_in_force must be 'day' or 'gtc', got {time_in_force!r}",
            code="invalid_time_in_force",
        )
    try:
        qty_f = float(quantity)
    except (TypeError, ValueError):
        return _err(f"quantity must be a number, got {quantity!r}",
                    code="invalid_quantity")
    if qty_f <= 0:
        return _err("quantity must be > 0", code="invalid_quantity")
    if order_type_norm == "limit":
        if limit_price is None or float(limit_price) <= 0:
            return _err(
                "limit_price > 0 is required for limit orders",
                code="invalid_limit_price",
            )

    market_norm = (market or "").strip().lower() or None
    if market_norm and market_norm not in ("us", "cn", "hk"):
        return _err(
            f"market must be one of us|cn|hk, got {market!r}",
            code="invalid_market",
        )

    request_uid = f"agent-{uuid.uuid4().hex[:24]}"

    intent = {
        "symbol": str(symbol).strip().upper(),
        "side": side_norm,
        "quantity": qty_f,
        "order_type": order_type_norm,
        "limit_price": float(limit_price) if limit_price is not None else None,
        "time_in_force": tif_norm,
        "market": market_norm,
        "currency": currency,
        "note": note,
        "account_id": int(account_id) if account_id is not None else None,
        "agent_session_id": agent_session_id,
        "source": "agent",
        "request_uid": request_uid,
    }

    return {
        "status": "proposal_emitted",
        "intent": intent,
        "next_step": (
            "User must review this intent in the WebUI and click "
            "'提交' to call POST /api/v1/trading/submit. The agent "
            "cannot submit on the user's behalf in Phase A."
        ),
        "message": (
            f"Trade proposal: {side_norm.upper()} {qty_f} {symbol} "
            f"({order_type_norm}). request_uid={request_uid}."
        ),
    }


propose_trade_tool = ToolDefinition(
    name="propose_trade",
    description=(
        "Emit a structured trade intent for the user to review and "
        "submit. **READ-ONLY in Phase A** — the tool returns an "
        "OrderRequest-shaped dict; it does NOT execute, does NOT "
        "submit to the trading service, and does NOT touch any "
        "broker API. The user reviews the proposal in the WebUI and "
        "clicks 提交 to call POST /api/v1/trading/submit. Returns "
        "``not_enabled`` when TRADING_MODE=disabled."
    ),
    parameters=[
        ToolParameter(
            name="symbol",
            type="string",
            description="Stock symbol (e.g. 'AAPL', '600519').",
            required=True,
        ),
        ToolParameter(
            name="side",
            type="string",
            description="'buy' or 'sell'.",
            required=True,
        ),
        ToolParameter(
            name="quantity",
            type="number",
            description="Number of shares; must be > 0.",
            required=True,
        ),
        ToolParameter(
            name="order_type",
            type="string",
            description="'market' (default) or 'limit'.",
            required=False,
        ),
        ToolParameter(
            name="limit_price",
            type="number",
            description="Required for 'limit' orders; must be > 0.",
            required=False,
        ),
        ToolParameter(
            name="time_in_force",
            type="string",
            description="'day' (default) or 'gtc'.",
            required=False,
        ),
        ToolParameter(
            name="market",
            type="string",
            description="'us' / 'cn' / 'hk'. Optional; the trading "
                        "service can derive from the account.",
            required=False,
        ),
        ToolParameter(
            name="currency",
            type="string",
            description="3-8 chars (USD / CNY / HKD). Optional.",
            required=False,
        ),
        ToolParameter(
            name="note",
            type="string",
            description="Free-text note that will be stored alongside "
                        "the audit row.",
            required=False,
        ),
        ToolParameter(
            name="account_id",
            type="integer",
            description="PortfolioAccount.id. Optional — defaults to "
                        "Config.trading_paper_account_id.",
            required=False,
        ),
        ToolParameter(
            name="agent_session_id",
            type="string",
            description="Opaque session id for audit linkage.",
            required=False,
        ),
    ],
    handler=_handle_propose_trade,
    category="action",
)


ALL_TRADING_TOOLS = [propose_trade_tool]
