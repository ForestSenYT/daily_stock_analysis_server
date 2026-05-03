# -*- coding: utf-8 -*-
"""
===================================
Broker API — Firstrade read-only
===================================

All endpoints sit under ``/api/v1/broker/firstrade/*`` and require the
existing admin session middleware (which protects every ``/api/v1/*``
path). The router intentionally exposes ZERO trading endpoints —
no place_order, no cancel_order, no option order. Adding any of those
would cross the read-only contract this whole subsystem advertises in
``docs/firstrade-integration.md``.

Endpoint shape:

  * ``status``          — flag + last sync metadata, never 5xx
  * ``login``           — start FTSession; returns ``mfa_required`` when needed
  * ``login/verify``    — finish MFA; 409 on session_lost
  * ``sync``            — pull full snapshot into local SQLite
  * ``accounts``        — local masked accounts
  * ``positions``       — local positions (per most recent sync)
  * ``orders``          — local open / recent orders
  * ``transactions``    — local recent transactions
  * ``snapshot``        — full local snapshot for agent / WebUI
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Query

from api.v1.schemas.broker import (
    BrokerListResponse,
    BrokerSnapshotResponse,
    BrokerStatusResponse,
    FirstradeLoginResponse,
    FirstradeMfaVerifyRequest,
    FirstradeSyncRequest,
    FirstradeSyncResponse,
)
from src.brokers.base import redact_sensitive_payload
from src.services.firstrade_sync_service import (
    FirstradeSyncService,
    get_firstrade_sync_service,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# Generic message used when a domain failure surfaces; the actual
# detail is logged server-side, not echoed back, to keep tokens /
# session IDs from any vendor traceback off the wire.
_GENERIC_BROKER_ERROR = "Firstrade operation failed."


# =====================================================================
# Defensive response sanitiser
# =====================================================================
#
# The repo + service already redact everything sensitive; this is a
# THIRD layer that makes the API boundary the last line of defence.
# It does two things:
#   1. Strips any sensitive keys that may have slipped through.
#   2. Refuses to emit any string field containing a bare 8+ digit
#      run outside a known-numeric whitelist (account numbers /
#      cookie fragments often look like that). Bare digits are
#      replaced with ``***``.

# Numeric fields we DO expect to contain long-digit strings — these
# are exempt from the digit-run scrubbing.
_NUMERIC_WHITELIST = frozenset({
    "quantity", "price", "amount", "limit_price", "filled_quantity",
    "order_quantity", "market_value", "avg_cost", "last_price",
    "unrealized_pnl", "buying_power", "total_value", "cash",
    "account_count", "balance_count", "position_count", "order_count",
    "transaction_count", "id", "limit", "started_at", "finished_at",
    "as_of", "trade_date", "settle_date", "created_at",
})

_DIGIT_RUN_RE = re.compile(r"\b\d{8,}\b")


def _scrub_value(key: Any, value: Any) -> Any:
    """Recursive defensive scrubber. Returns a new structure."""
    if isinstance(value, dict):
        return {k: _scrub_value(k, v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_value(key, v) for v in value]
    if isinstance(value, tuple):
        return tuple(_scrub_value(key, v) for v in value)
    if isinstance(value, str):
        # Skip whitelisted numeric fields. ``last4`` is intentionally
        # 4 digits so the regex never matches; we still allow it
        # explicitly.
        normalized_key = str(key).strip().lower() if isinstance(key, str) else ""
        if normalized_key in _NUMERIC_WHITELIST or normalized_key.endswith("_id_hash"):
            return value
        return _DIGIT_RUN_RE.sub("***", value)
    return value


def _harden_response(payload: Any) -> Any:
    """Apply redaction + digit-run scrubbing in that order."""
    cleaned = redact_sensitive_payload(payload)
    return _scrub_value(None, cleaned)


# =====================================================================
# Service accessor + envelope helpers
# =====================================================================

def _service() -> FirstradeSyncService:
    return get_firstrade_sync_service()


def _envelope_to_status(payload: Dict[str, Any]) -> BrokerStatusResponse:
    return BrokerStatusResponse(**_harden_response(payload))


# =====================================================================
# Endpoints
# =====================================================================

@router.get(
    "/firstrade/status",
    response_model=BrokerStatusResponse,
    summary="Firstrade integration status",
    description=(
        "Master flag, login state, and the most recent sync run row. "
        "Safe to call when the feature flag is off — never 5xx for a "
        "healthy service."
    ),
)
def firstrade_status() -> BrokerStatusResponse:
    try:
        return _envelope_to_status(_service().get_status())
    except Exception:
        logger.exception("[broker] firstrade_status surfaced an unexpected error")
        raise HTTPException(
            status_code=503,
            detail={"error": "broker_error", "message": _GENERIC_BROKER_ERROR},
        )


@router.post(
    "/firstrade/login",
    response_model=FirstradeLoginResponse,
    summary="Open / resume a Firstrade FTSession",
    description=(
        "Triggers the read-only Firstrade login. Returns "
        "``status='mfa_required'`` if the vendor SDK signals that a "
        "verification code is needed; the caller should then prompt "
        "the user and POST the code to ``/login/verify``. "
        "**No trading capability is exposed by this endpoint.**"
    ),
    responses={
        200: {"description": "Login initiated."},
        503: {"description": "Feature disabled or dependency missing."},
    },
)
def firstrade_login() -> FirstradeLoginResponse:
    try:
        result = _service().login()
    except Exception:
        logger.exception("[broker] firstrade_login surfaced an unexpected error")
        raise HTTPException(
            status_code=503,
            detail={"error": "broker_error", "message": _GENERIC_BROKER_ERROR},
        )
    cleaned = _harden_response(result)
    if cleaned.get("status") == "not_enabled":
        raise HTTPException(
            status_code=503,
            detail={"error": "broker_not_enabled", "message": cleaned.get("message")},
        )
    if cleaned.get("status") == "not_installed":
        raise HTTPException(
            status_code=503,
            detail={"error": "broker_not_installed", "message": cleaned.get("message")},
        )
    return FirstradeLoginResponse(**cleaned)


@router.post(
    "/firstrade/login/verify",
    response_model=FirstradeLoginResponse,
    summary="Finish two-step MFA",
    description=(
        "Submits the verification code obtained out-of-band. Returns "
        "HTTP 409 with ``status='session_lost'`` if the singleton was "
        "recycled (e.g. on Cloud Run between login and verify) — the "
        "WebUI should reset the form and ask the user to login again."
    ),
    responses={
        200: {"description": "MFA accepted."},
        409: {"description": "Session lost between login and verify; please re-login."},
    },
)
def firstrade_login_verify(body: FirstradeMfaVerifyRequest) -> FirstradeLoginResponse:
    try:
        result = _service().verify_mfa(body.code)
    except Exception:
        logger.exception("[broker] firstrade_login_verify surfaced an unexpected error")
        raise HTTPException(
            status_code=503,
            detail={"error": "broker_error", "message": _GENERIC_BROKER_ERROR},
        )
    cleaned = _harden_response(result)
    if cleaned.get("status") == "session_lost":
        raise HTTPException(
            status_code=409,
            detail={
                "error": "broker_session_lost",
                "message": cleaned.get("message")
                or "MFA session is missing. Please re-login.",
            },
        )
    return FirstradeLoginResponse(**cleaned)


@router.post(
    "/firstrade/sync",
    response_model=FirstradeSyncResponse,
    summary="Pull latest snapshot from Firstrade into local SQLite",
    description=(
        "Manual sync trigger. Always writes a sync_run row regardless "
        "of success. The endpoint is **read-only**: it never sends "
        "trade orders, never modifies portfolio_trades, and stores "
        "only redacted payloads."
    ),
    responses={
        200: {"description": "Sync completed (status field disambiguates success vs failure)."},
    },
)
def firstrade_sync(body: FirstradeSyncRequest = FirstradeSyncRequest()) -> FirstradeSyncResponse:
    try:
        result = _service().sync_now(date_range=body.date_range)
    except Exception:
        logger.exception("[broker] firstrade_sync surfaced an unexpected error")
        raise HTTPException(
            status_code=503,
            detail={"error": "broker_error", "message": _GENERIC_BROKER_ERROR},
        )
    return FirstradeSyncResponse(**_harden_response(result))


@router.get(
    "/firstrade/accounts",
    response_model=BrokerListResponse,
    summary="List masked Firstrade accounts (local snapshot)",
    description=(
        "Returns ``account_alias`` (e.g. ``Firstrade ****1234``), "
        "``account_last4``, and ``account_hash``. Never includes the "
        "real account number."
    ),
)
def firstrade_accounts() -> BrokerListResponse:
    return BrokerListResponse(**_harden_response(_service().get_accounts()))


@router.get(
    "/firstrade/positions",
    response_model=BrokerListResponse,
    summary="Local positions snapshot",
)
def firstrade_positions(
    account_hash: str = Query(default="", description="Filter by masked account_hash."),
) -> BrokerListResponse:
    return BrokerListResponse(
        **_harden_response(
            _service().get_positions(account_hash=account_hash or None),
        ),
    )


@router.get(
    "/firstrade/orders",
    response_model=BrokerListResponse,
    summary="Local order snapshot (read-only — endpoint exposes no trading)",
)
def firstrade_orders(
    account_hash: str = Query(default=""),
) -> BrokerListResponse:
    return BrokerListResponse(
        **_harden_response(
            _service().get_orders(account_hash=account_hash or None),
        ),
    )


@router.get(
    "/firstrade/transactions",
    response_model=BrokerListResponse,
    summary="Local recent transactions snapshot",
)
def firstrade_transactions(
    account_hash: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=500),
) -> BrokerListResponse:
    return BrokerListResponse(
        **_harden_response(
            _service().get_transactions(
                account_hash=account_hash or None, limit=limit,
            ),
        ),
    )


@router.get(
    "/firstrade/snapshot",
    response_model=BrokerSnapshotResponse,
    summary="Full local Firstrade snapshot (accounts + balances + positions + orders + transactions)",
    description=(
        "Powers the WebUI Portfolio panel and the agent tool. All "
        "payloads have already been redacted at the repository layer; "
        "this endpoint applies one more sanitiser pass before sending "
        "the response."
    ),
)
def firstrade_snapshot() -> BrokerSnapshotResponse:
    return BrokerSnapshotResponse(**_harden_response(_service().get_snapshot()))
