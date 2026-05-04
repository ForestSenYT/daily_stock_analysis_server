# -*- coding: utf-8 -*-
"""Cross-broker dataclasses + the redaction / masking primitives that
every broker connector MUST funnel its outputs through.

Why a single place
------------------
The repo already has many security-sensitive code paths. Broker data
adds another category — credentials, cookies, full account numbers,
session tokens — that must never leave this package un-scrubbed. By
centralising the dataclasses *and* the redactor here, the snapshot
repository, the sync service, the API endpoints, and the agent tool
all reuse one enforcement point.

Hard rules enforced at this layer:
  * No payload (dict, list, scalar) leaves the package without going
    through :func:`redact_sensitive_payload`.
  * Account numbers are split into ``account_hash`` + ``account_last4``
    + ``account_alias`` at construction time; we never carry the raw
    number outside :class:`brokers.firstrade.client.FirstradeReadOnlyClient`'s
    in-memory mapping.
  * No vendor order / trade types are ever imported here.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


# =====================================================================
# Result / login structures
# =====================================================================

@dataclass
class BrokerLoginResult:
    """Outcome of an attempt to establish a broker session.

    ``status`` is one of:
        ``ok``               — fully logged in and ready to query.
        ``mfa_required``     — credentials accepted; awaiting code.
        ``login_required``   — caller must restart from username/password.
        ``not_installed``    — vendor SDK isn't on this Python image.
        ``not_enabled``      — feature flag is off.
        ``session_lost``     — singleton recycled between two-step MFA.
        ``failed``           — generic, see ``message``.
    """
    status: str
    message: Optional[str] = None
    account_count: int = 0
    broker: str = "firstrade"


# =====================================================================
# Snapshot dataclasses — these are the public DTOs that the sync
# service / repo / API / agent tool exchange. They never carry raw
# credentials or full account numbers.
# =====================================================================

@dataclass
class BrokerAccount:
    broker: str
    account_hash: str
    account_last4: str
    account_alias: str
    as_of: Optional[str] = None
    raw_payload: Dict[str, Any] = field(default_factory=dict)


@dataclass
class BrokerBalance:
    broker: str
    account_hash: str
    account_last4: str
    account_alias: str
    cash: Optional[float] = None
    buying_power: Optional[float] = None
    total_value: Optional[float] = None
    currency: Optional[str] = None
    as_of: Optional[str] = None
    raw_payload: Dict[str, Any] = field(default_factory=dict)


@dataclass
class BrokerPosition:
    broker: str
    account_hash: str
    account_last4: str
    account_alias: str
    symbol: str
    quantity: Optional[float] = None
    market_value: Optional[float] = None
    avg_cost: Optional[float] = None
    last_price: Optional[float] = None
    # ``unrealized_pnl`` is the lifetime open-position P&L (vs. avg_cost).
    # ``day_change`` / ``day_change_pct`` are the SAME-DAY price move,
    # which is what the Firstrade web UI shows in its "变更$ / 变更%"
    # columns. Most users care about same-day moves for monitoring,
    # so we surface both to keep the panel content matching what they
    # see in the broker's own UI.
    unrealized_pnl: Optional[float] = None
    day_change: Optional[float] = None
    day_change_pct: Optional[float] = None
    currency: Optional[str] = None
    as_of: Optional[str] = None
    raw_payload: Dict[str, Any] = field(default_factory=dict)


@dataclass
class BrokerOrder:
    broker: str
    account_hash: str
    account_last4: str
    account_alias: str
    order_id_hash: str
    symbol: Optional[str] = None
    order_status: Optional[str] = None
    order_side: Optional[str] = None
    order_type: Optional[str] = None
    order_quantity: Optional[float] = None
    filled_quantity: Optional[float] = None
    limit_price: Optional[float] = None
    as_of: Optional[str] = None
    raw_payload: Dict[str, Any] = field(default_factory=dict)


@dataclass
class BrokerTransaction:
    broker: str
    account_hash: str
    account_last4: str
    account_alias: str
    transaction_id_hash: str
    symbol: Optional[str] = None
    transaction_type: Optional[str] = None
    trade_date: Optional[str] = None
    settle_date: Optional[str] = None
    amount: Optional[float] = None
    quantity: Optional[float] = None
    currency: Optional[str] = None
    raw_payload: Dict[str, Any] = field(default_factory=dict)


@dataclass
class BrokerSnapshot:
    """A point-in-time roll-up: accounts + their balances/positions/...

    The sync service builds one of these per ``sync_now()`` call and
    hands it to :class:`BrokerSnapshotRepository`. Persistence layer is
    expected to redact ``raw_payload`` once more on its way to storage
    even though it's already redacted here, as defence-in-depth.
    """
    broker: str
    as_of: str
    accounts: List[BrokerAccount] = field(default_factory=list)
    balances: List[BrokerBalance] = field(default_factory=list)
    positions: List[BrokerPosition] = field(default_factory=list)
    orders: List[BrokerOrder] = field(default_factory=list)
    transactions: List[BrokerTransaction] = field(default_factory=list)


# =====================================================================
# Account masking + stable hashing
# =====================================================================

def mask_account_number(account_number: str) -> Tuple[str, str]:
    """Return ``(account_last4, account_alias)``.

    ``account_alias`` follows the convention ``"Firstrade ****1234"``
    so the WebUI / agent can show a human-readable label without ever
    surfacing the full number. Non-digit characters in the input are
    preserved when computing last4 to be tolerant of dashes / spaces.
    """
    if not account_number:
        return "", "Firstrade ****"
    digits = re.sub(r"\D", "", str(account_number))
    last4 = digits[-4:] if digits else ""
    alias = f"Firstrade ****{last4}" if last4 else "Firstrade ****"
    return last4, alias


def hash_account_number(account_number: str, salt: str) -> str:
    """Stable account identifier for cross-request matching.

    Uses ``sha256(salt | account_number)`` truncated to 16 hex chars —
    long enough to make collisions astronomically unlikely (~7e-19 at
    1M accounts) while small enough to log without bloating responses.
    A non-empty ``salt`` is required; the caller (Config) is responsible
    for fail-fast at startup so we never silently use a guessable
    constant. Empty inputs return an empty string.
    """
    if not account_number:
        return ""
    if not salt:
        # Defensive: must be unreachable in production because Config
        # rejects this combination at boot. Hard-fail rather than fall
        # back to a guessable hash.
        raise ValueError(
            "hash_account_number requires a non-empty salt; "
            "BROKER_ACCOUNT_HASH_SALT must be set when broker integration "
            "is enabled."
        )
    digest = hashlib.sha256(f"{salt}|{account_number}".encode("utf-8")).hexdigest()
    return digest[:16]


def hash_broker_id(raw_id: str, salt: str) -> str:
    """Same construction as :func:`hash_account_number` but used for
    order_id / transaction_id so the agent / UI can correlate without
    surfacing the raw broker-side identifier (which is sometimes a
    numeric string that could be guessed)."""
    if not raw_id:
        return ""
    if not salt:
        raise ValueError(
            "hash_broker_id requires a non-empty salt; "
            "BROKER_ACCOUNT_HASH_SALT must be set when broker integration "
            "is enabled."
        )
    digest = hashlib.sha256(f"{salt}|{raw_id}".encode("utf-8")).hexdigest()
    return digest[:16]


# =====================================================================
# Redaction
# =====================================================================
#
# Recursive case-insensitive key blacklist. Anything in this set is
# stripped from any dict that reaches the repository, the API, or the
# agent. We DON'T try to detect JWT-shaped values or long alphanumeric
# strings — that approach catches false positives like SHA hashes,
# transaction IDs, and order IDs. Key-name redaction is the one rule
# that's both effective and predictable.
#
# NOTE: ``account``, ``account_number``, ``accountNo`` are also redacted
# because the vendor library tends to echo them back inside nested
# objects. The connector exposes them only via the masked aliases above.
_REDACT_KEYS = frozenset({
    "username",
    "password",
    "pin",
    "mfa",
    "mfa_secret",
    "secret",
    "token",
    "access_token",
    "access-token",
    "refresh_token",
    "session_token",
    "ftat",
    "sid",
    "cookie",
    "cookies",
    "set_cookie",
    "authorization",
    "account",
    "account_id",
    "account_number",
    "accountno",
    "accountnumber",
    "acct",
    "acctnumber",
    "acct_number",
    "session_id",
    "sessionid",
    "auth",
    "csrf",
    "csrf_token",
    "xsrf",
    "xsrf_token",
    "credentials",
    "credential",
})

_REDACT_CANONICAL_KEYS = frozenset(
    "".join(ch for ch in key.lower() if ch.isalnum())
    for key in _REDACT_KEYS
) | frozenset({
    "accesstoken",
    "refreshtoken",
    "sessiontoken",
    "setcookie",
    "accountid",
    "orderno",
    "ordernumber",
    "orderid",
    "transactionid",
    "historyid",
    "tradeid",
    "csrftoken",
    "xsrftoken",
})

_REDACTED_VALUE = "***REDACTED***"


def _is_sensitive_key(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    normalized = key.strip().lower().replace("-", "_")
    if normalized in _REDACT_KEYS:
        return True
    canonical = "".join(ch for ch in key.strip().lower() if ch.isalnum())
    return canonical in _REDACT_CANONICAL_KEYS


def redact_sensitive_payload(payload: Any) -> Any:
    """Return a deep copy of ``payload`` with every sensitive key
    replaced by ``***REDACTED***``.

    Lists and tuples are recursed into; sets are coerced to lists in the
    output (we don't expect set payloads from the vendor SDK, but if
    one slips in we keep behavior deterministic).

    The original ``payload`` is left untouched.
    """
    if isinstance(payload, dict):
        out: Dict[str, Any] = {}
        for k, v in payload.items():
            if _is_sensitive_key(k):
                out[k] = _REDACTED_VALUE
            else:
                out[k] = redact_sensitive_payload(v)
        return out
    if isinstance(payload, list):
        return [redact_sensitive_payload(item) for item in payload]
    if isinstance(payload, tuple):
        return tuple(redact_sensitive_payload(item) for item in payload)
    if isinstance(payload, set):
        return [redact_sensitive_payload(item) for item in payload]
    return payload
