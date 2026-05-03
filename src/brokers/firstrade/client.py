# -*- coding: utf-8 -*-
"""Firstrade read-only client.

This is the ONLY place in the project that touches the unofficial
``firstrade`` PyPI package. Everything in this module is structured to
make the read-only invariant impossible to break by accident:

  * ``from firstrade import account`` is the single allowed import. No
    ``order`` / ``trade`` / ``cancel`` modules. A unit test (and a
    repo-wide grep in CI) double-checks this stays true.
  * Every public method returns dataclasses from
    :mod:`brokers.base` — already-redacted, hash-keyed, and stripped of
    full account numbers.
  * Errors are routed through :func:`_sanitize_exception` so vendor
    tracebacks (which can carry request URLs with cookies) never reach
    a logger or an API response.
  * The vendor SDK is imported lazily — importing this module on a
    Cloud Run image without ``requirements-broker.txt`` installed is a
    no-op until ``login()`` is actually called.

Singleton expectations (enforced by the sync service):
  * One process holds one ``FirstradeReadOnlyClient`` instance so the
    vendor's ``FTSession`` survives between ``login()`` and
    ``verify_mfa(code)``.
  * Concurrent ``sync_now`` calls are serialised by a ``threading.Lock``
    in the sync service, not here. This client itself is not
    thread-safe — that's the service's job.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.brokers.base import (
    BrokerAccount,
    BrokerBalance,
    BrokerLoginResult,
    BrokerOrder,
    BrokerPosition,
    BrokerSnapshot,
    BrokerTransaction,
    hash_account_number,
    hash_broker_id,
    mask_account_number,
    redact_sensitive_payload,
)

logger = logging.getLogger(__name__)


# Recognised Firstrade transaction-history range codes. The vendor SDK
# accepts these literal strings; anything else is rejected by us so
# downstream layers never have to defensively pass arbitrary strings
# to the SDK.
_VALID_DATE_RANGES = frozenset(
    {"today", "1w", "1m", "2m", "mtd", "ytd", "ly", "cust"}
)


@dataclass
class _FirstradeSdkHandles:
    """Bundle the lazy-loaded SDK references so we don't sprinkle
    ``import`` statements through every method."""
    session: Any  # FTSession instance
    account_data: Any  # FTAccountData instance, or None until logged in


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sanitize_exception(exc: BaseException) -> str:
    """Build a logger / API safe one-line description of ``exc``.

    Vendor exceptions sometimes embed request URLs with sid / ftat
    cookies in the message (e.g. ``HTTPError: 401 Client Error: ...
    for url: https://invest-api.firstrade.com/...?sid=...&ftat=...``).
    This helper:
      * keeps only the exception class name + a short hint
      * strips any ``sid=`` / ``ftat=`` / ``token=`` query fragments
      * truncates at 240 chars
    """
    cls_name = type(exc).__name__
    raw = str(exc)
    cleaned = re.sub(
        r"(?i)(sid|ftat|token|cookie|authorization|password|pin|mfa)\s*=\s*[^&\s\"']+",
        r"\1=***",
        raw,
    )
    # Strip query-strings entirely from any surviving URLs as a belt-
    # and-suspenders measure — we never need them in error messages.
    cleaned = re.sub(r"(https?://[^\s\"']+?)\?[^\s\"']+", r"\1?<redacted>", cleaned)
    cleaned = cleaned.replace("\n", " ").replace("\r", " ").strip()
    if len(cleaned) > 240:
        cleaned = cleaned[:237] + "..."
    return f"{cls_name}: {cleaned}" if cleaned else cls_name


# =====================================================================
# Field extraction helpers (defensive — vendor field names drift)
# =====================================================================

def _first_present(obj: Any, *names: str) -> Any:
    """Return the first attribute / dict key from ``obj`` matching one
    of ``names`` (case-insensitive on dict keys, case-exact on
    attributes). ``None`` if nothing matches.

    The vendor SDK's underlying responses sometimes appear as raw
    dicts (when the library forwards JSON) and sometimes as light
    object wrappers; this helper hides the difference."""
    if obj is None:
        return None
    for name in names:
        if hasattr(obj, name):
            try:
                value = getattr(obj, name)
                if value is not None:
                    return value
            except Exception:  # pragma: no cover — defensive
                continue
    if isinstance(obj, dict):
        lowered = {str(k).lower(): k for k in obj.keys()}
        for name in names:
            actual = lowered.get(str(name).lower())
            if actual is not None and obj[actual] is not None:
                return obj[actual]
    return None


def _to_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None


def _as_dict(obj: Any) -> Dict[str, Any]:
    """Best-effort conversion of vendor objects into a JSON-serialisable
    dict, with sensitive keys already redacted.

    Handles dataclass-style objects (``__dict__``), pydantic models
    (``model_dump`` / ``dict()``), plain dicts, and falls back to
    ``str()`` for anything else.
    """
    if obj is None:
        return {}
    payload: Any
    if isinstance(obj, dict):
        payload = obj
    elif hasattr(obj, "model_dump") and callable(obj.model_dump):
        try:
            payload = obj.model_dump()
        except Exception:  # pragma: no cover
            payload = {}
    elif hasattr(obj, "dict") and callable(obj.dict):
        try:
            payload = obj.dict()
        except Exception:  # pragma: no cover
            payload = {}
    elif hasattr(obj, "__dict__"):
        payload = {
            k: v for k, v in vars(obj).items()
            if not k.startswith("_")
        }
    else:
        payload = {"value": str(obj)}
    if not isinstance(payload, dict):
        payload = {"value": str(payload)}
    return redact_sensitive_payload(payload)


# =====================================================================
# Client
# =====================================================================

class FirstradeReadOnlyClient:
    """Stateful client around ``firstrade.account``.

    Lifecycle:
        1. ``login()`` opens an FTSession.
        2. If the result is ``mfa_required``, the caller obtains a code
           out-of-band and calls ``verify_mfa(code)``.
        3. Each subsequent ``list_accounts()`` / ``get_*()`` reuses the
           cached ``FTAccountData`` and refreshes the per-account
           details on demand.
        4. The vendor SDK persists a session cookie file under
           ``profile_path``; on a new process boot a previous session
           may be resumed without prompting for credentials again.

    The client never sends real-trading requests. It does not expose
    ``order`` / ``trade`` methods, and the vendor's ``firstrade.order``
    module is never imported.
    """

    BROKER_NAME = "firstrade"

    def __init__(self, config: Any = None) -> None:
        self._config = config or self._resolve_config()
        self._sdk: Optional[_FirstradeSdkHandles] = None
        # Hash → real-account-number mapping is **only** kept in memory.
        # On a process restart the next ``list_accounts`` call rebuilds
        # it; the snapshot rows persist enough alias / last4 metadata
        # that the agent tool never depends on this map.
        self._account_map: Dict[str, str] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_enabled(self) -> bool:
        return bool(getattr(self._config, "broker_firstrade_enabled", False))

    def is_logged_in(self) -> bool:
        return self._sdk is not None and self._sdk.account_data is not None

    def login(self) -> BrokerLoginResult:
        """Open or resume an FTSession, returning the next-step status."""
        if not self.is_enabled():
            return BrokerLoginResult(
                status="not_enabled",
                broker=self.BROKER_NAME,
                message="BROKER_FIRSTRADE_ENABLED is false.",
            )
        try:
            account_module = self._import_sdk()
        except _SdkNotInstalled as exc:
            return BrokerLoginResult(
                status="not_installed",
                broker=self.BROKER_NAME,
                message=str(exc),
            )

        with self._lock:
            try:
                profile_path = self._config.broker_firstrade_profile_path
                Path(profile_path).expanduser().mkdir(parents=True, exist_ok=True)
                # The vendor SDK's signature has churned across versions
                # (e.g. 0.0.38 does NOT accept ``save_session``). Build
                # the kwarg set we'd ideally pass, then drop any name
                # the installed FTSession.__init__ rejects so we don't
                # crash construction. Anything we drop is logged once
                # so behaviour drift stays visible.
                desired_kwargs: Dict[str, Any] = {
                    "username": self._config.broker_firstrade_username,
                    "password": self._config.broker_firstrade_password,
                    "pin": self._config.broker_firstrade_pin or None,
                    "email": self._config.broker_firstrade_email or None,
                    "phone": self._config.broker_firstrade_phone or None,
                    "mfa_secret": self._config.broker_firstrade_mfa_secret or None,
                    "profile_path": profile_path,
                    "save_session": bool(self._config.broker_firstrade_save_session),
                }
                ft_kwargs = self._filter_supported_kwargs(
                    account_module.FTSession, desired_kwargs,
                )
                ft_session = account_module.FTSession(**ft_kwargs)
                need_code = ft_session.login()
            except Exception as exc:  # noqa: BLE001 — boundary
                logger.warning(
                    "[firstrade] login failed: %s",
                    _sanitize_exception(exc),
                )
                return BrokerLoginResult(
                    status="failed",
                    broker=self.BROKER_NAME,
                    message=_sanitize_exception(exc),
                )

            if need_code:
                # Persist the half-logged-in session so verify_mfa can
                # complete the flow.
                self._sdk = _FirstradeSdkHandles(session=ft_session, account_data=None)
                return BrokerLoginResult(
                    status="mfa_required",
                    broker=self.BROKER_NAME,
                    message="A verification code is required.",
                )

            account_data = self._build_account_data(account_module, ft_session)
            self._sdk = _FirstradeSdkHandles(
                session=ft_session, account_data=account_data,
            )
            return BrokerLoginResult(
                status="ok",
                broker=self.BROKER_NAME,
                account_count=len(
                    getattr(account_data, "account_numbers", None) or []
                ),
            )

    def verify_mfa(self, code: str) -> BrokerLoginResult:
        """Complete the second step of MFA. Returns ``session_lost`` if
        the singleton was recycled between ``login()`` and this call —
        the API maps that to a 409 so the frontend resets cleanly."""
        if not self.is_enabled():
            return BrokerLoginResult(
                status="not_enabled", broker=self.BROKER_NAME,
            )
        try:
            account_module = self._import_sdk()
        except _SdkNotInstalled as exc:
            return BrokerLoginResult(
                status="not_installed", broker=self.BROKER_NAME,
                message=str(exc),
            )

        with self._lock:
            if self._sdk is None or self._sdk.session is None:
                return BrokerLoginResult(
                    status="session_lost",
                    broker=self.BROKER_NAME,
                    message=(
                        "MFA session is missing. The Firstrade login step "
                        "must be repeated before submitting a verification "
                        "code."
                    ),
                )
            try:
                self._sdk.session.login_two(str(code).strip())
                account_data = self._build_account_data(
                    account_module, self._sdk.session,
                )
                self._sdk.account_data = account_data
            except Exception as exc:  # noqa: BLE001 — boundary
                logger.warning(
                    "[firstrade] MFA verification failed: %s",
                    _sanitize_exception(exc),
                )
                return BrokerLoginResult(
                    status="failed",
                    broker=self.BROKER_NAME,
                    message=_sanitize_exception(exc),
                )
            return BrokerLoginResult(
                status="ok",
                broker=self.BROKER_NAME,
                account_count=len(
                    getattr(self._sdk.account_data, "account_numbers", None) or []
                ),
            )

    def logout(self) -> None:
        with self._lock:
            self._sdk = None
            self._account_map.clear()

    # ----------------------- read paths --------------------------

    def list_accounts(self) -> List[BrokerAccount]:
        """Enumerate the user's real Firstrade accounts.

        ``firstrade==0.0.38`` exposes the canonical list as
        ``account_data.account_numbers`` — a list of bare account-number
        strings. We DO NOT iterate ``account_data.all_accounts`` (which
        is the raw HTTP response wrapper containing ``statusCode`` etc.,
        NOT a per-account list — earlier diagnostic logs confirmed this).
        """
        sdk = self._require_logged_in()
        if sdk is None:
            return []
        try:
            account_numbers = list(
                getattr(sdk.account_data, "account_numbers", None) or []
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[firstrade] list_accounts: account_numbers read failed: %s",
                _sanitize_exception(exc),
            )
            return []
        # Per-row "real account" already carries the masking we need;
        # log only the *count*, not the values.
        logger.info(
            "[firstrade] list_accounts: account_numbers count=%d "
            "(via account_data.account_numbers, the canonical anchor)",
            len(account_numbers),
        )
        salt = self._salt()
        accounts: List[BrokerAccount] = []
        skipped = 0
        for raw in account_numbers:
            real_account = str(raw or "").strip()
            if not real_account:
                skipped += 1
                continue
            account_hash = hash_account_number(real_account, salt)
            last4, alias = mask_account_number(real_account)
            self._account_map[account_hash] = real_account
            accounts.append(
                BrokerAccount(
                    broker=self.BROKER_NAME,
                    account_hash=account_hash,
                    account_last4=last4,
                    account_alias=alias,
                    as_of=_now_iso(),
                    raw_payload={},  # account_numbers is just a string list, no payload
                )
            )
        if skipped:
            logger.warning(
                "[firstrade] list_accounts: %d empty entries skipped",
                skipped,
            )
        return accounts

    def get_balances(
        self, account_hash_or_alias: Optional[str] = None,
    ) -> List[BrokerBalance]:
        """Read per-account balances.

        Vendor's ``get_account_balances(account=X)`` returns an HTTP
        envelope dict::

            {"statusCode": 200, "message": "...", "result": {...balance fields...}, "error": ...}

        The actual balance fields live under ``result``. Production
        diagnostic logs confirmed this shape — the earlier "0 positions"
        regression came from reading non-existent dynamic attributes
        instead of the return value.
        """
        sdk = self._require_logged_in()
        if sdk is None:
            return []
        ad = sdk.account_data
        balances: List[BrokerBalance] = []
        for real, account_hash, last4, alias in self._resolve_target_accounts(
            account_hash_or_alias
        ):
            ret = self._invoke_read_method(
                ad,
                method_name="get_account_balances",
                account=real,
                phase_label="balances",
            )
            balance_payload = self._extract_response_dict(ret, key="result")
            balances.append(self._payload_to_balance(
                balance_payload, account_hash, last4, alias,
            ))
        return balances

    def get_positions(
        self, account_hash_or_alias: Optional[str] = None,
    ) -> List[BrokerPosition]:
        """Read per-account positions.

        Vendor's ``get_positions(account=X)`` returns an HTTP envelope
        dict::

            {"statusCode": 200, "account": "...", "items": [position_dicts],
             "total_market_value": ..., "total_daychange_amount": ...,
             "total_gainloss": ..., "pagination": {...}, ...}

        Each row in ``items`` is a per-position detail dict. We pulled
        this shape directly from production logs after a long debugging
        session that mistook earlier SDK builds (which side-effect-
        populated ``securities_held``) for the current 0.0.38 contract.
        """
        sdk = self._require_logged_in()
        if sdk is None:
            return []
        ad = sdk.account_data
        positions: List[BrokerPosition] = []
        for real, account_hash, last4, alias in self._resolve_target_accounts(
            account_hash_or_alias
        ):
            ret = self._invoke_read_method(
                ad,
                method_name="get_positions",
                account=real,
                phase_label="positions",
            )
            for item in self._extract_response_items(ret):
                if isinstance(item, dict):
                    symbol = str(
                        _first_present(item, "symbol", "ticker", "sym") or ""
                    ).strip()
                    positions.append(self._payload_to_position(
                        symbol, item, account_hash, last4, alias,
                    ))
                else:
                    # Defensive: some SDK builds return bare ticker
                    # strings inside ``items``. Preserve the symbol;
                    # detail fields stay None.
                    positions.append(self._payload_to_position(
                        str(item).strip(), {}, account_hash, last4, alias,
                    ))
        return positions

    def get_orders(
        self, account_hash_or_alias: Optional[str] = None,
    ) -> List[BrokerOrder]:
        """Read per-account orders.

        Vendor's ``get_orders(account=X)`` returns
        ``{"statusCode": ..., "account": ..., "items": [order_dicts],
        "pagination": ..., ...}``. Per-order rows live under ``items``.
        """
        sdk = self._require_logged_in()
        if sdk is None:
            return []
        ad = sdk.account_data
        orders: List[BrokerOrder] = []
        for real, account_hash, last4, alias in self._resolve_target_accounts(
            account_hash_or_alias
        ):
            ret = self._invoke_read_method(
                ad,
                method_name="get_orders",
                account=real,
                phase_label="orders",
            )
            for raw in self._extract_response_items(ret):
                orders.append(self._payload_to_order(
                    raw, account_hash, last4, alias,
                ))
        return orders

    def get_transactions(
        self,
        account_hash_or_alias: Optional[str] = None,
        date_range: str = "today",
    ) -> List[BrokerTransaction]:
        """Read per-account transaction history.

        Vendor's ``get_account_history(account=X)`` returns
        ``{"statusCode": ..., "items": [transaction_dicts], "page": ..., ...}``.
        """
        normalized = (date_range or "today").strip().lower()
        if normalized not in _VALID_DATE_RANGES:
            logger.warning(
                "[firstrade] get_transactions: unsupported date_range=%r; "
                "falling back to 'today'.",
                date_range,
            )
            normalized = "today"
        if normalized == "cust":
            logger.info(
                "[firstrade] custom date range not implemented in v1; "
                "returning today's history."
            )
            normalized = "today"
        sdk = self._require_logged_in()
        if sdk is None:
            return []
        ad = sdk.account_data
        results: List[BrokerTransaction] = []
        for real, account_hash, last4, alias in self._resolve_target_accounts(
            account_hash_or_alias
        ):
            ret = self._invoke_read_method(
                ad,
                method_name="get_account_history",
                account=real,
                phase_label="history",
                extra_kwargs={"period": normalized},
            )
            for raw in self._extract_response_items(ret):
                results.append(self._payload_to_transaction(
                    raw, account_hash, last4, alias,
                ))
        return results

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _resolve_config(self) -> Any:
        from src.config import get_config
        return get_config()

    def _salt(self) -> str:
        salt = (
            getattr(self._config, "broker_account_hash_salt", "") or ""
        ).strip()
        if not salt:
            # Defensive: Config refuses to boot without this when the
            # feature is enabled, so reaching this branch means somebody
            # constructed the client manually with a degenerate config
            # (e.g., in a test). Hard-fail rather than ship weak hashes.
            raise RuntimeError(
                "FirstradeReadOnlyClient requires broker_account_hash_salt "
                "to be set on the Config (set BROKER_ACCOUNT_HASH_SALT)."
            )
        return salt

    def _import_sdk(self):
        try:
            from firstrade import account as account_module  # noqa: F401
        except ImportError as exc:
            raise _SdkNotInstalled(
                "The 'firstrade' package is not installed. Run "
                "`pip install -r requirements-broker.txt` and redeploy."
            ) from exc
        return account_module

    def _build_account_data(self, account_module: Any, ft_session: Any) -> Any:
        try:
            return account_module.FTAccountData(ft_session)
        except Exception as exc:  # noqa: BLE001 — boundary
            logger.warning(
                "[firstrade] FTAccountData construction failed: %s",
                _sanitize_exception(exc),
            )
            raise

    def _require_logged_in(self) -> Optional[_FirstradeSdkHandles]:
        if not self.is_enabled():
            return None
        if not self.is_logged_in():
            return None
        return self._sdk

    @staticmethod
    def _safe_len(seq: Any) -> int:
        # IMPORTANT: do NOT call list() on bare strings — Python iterates
        # them character-by-character (e.g. ``len(list("12345")) == 5``)
        # which would mis-count a single account as N accounts. Route
        # through the normalizer so the count matches what
        # ``list_accounts`` actually iterates over.
        try:
            return len(FirstradeReadOnlyClient._normalize_accounts_iterable(seq))
        except Exception:
            return 0

    @staticmethod
    def _normalize_accounts_iterable(raw: Any) -> List[Any]:
        """Coerce vendor's ``all_accounts`` into a real list, regardless
        of its actual shape.

        ``firstrade==0.0.38`` returns a **single account-number string**
        for a one-account user; older / newer revisions sometimes wrap
        accounts in a dict (`{"12345678": {...details}}`) or a tuple or
        a fancy iterator. We DON'T want ``list("12345678")`` because
        that expands to per-character pseudo-accounts.
        """
        if raw is None:
            return []
        # Single scalar — wrap as one element. ``bytes`` included for
        # safety even though we don't expect them.
        if isinstance(raw, (str, bytes, int)):
            return [raw]
        # Dict — typically maps account_number → details. Keys ARE the
        # account numbers; values are extra metadata we currently don't
        # use, but we preserve them inside ``raw_payload`` if present
        # by returning ``{key, **value}`` shaped dicts.
        if isinstance(raw, dict):
            normalized: List[Any] = []
            for k, v in raw.items():
                if isinstance(v, dict):
                    merged = {**v, "_account_key": k}
                    normalized.append(merged)
                else:
                    normalized.append(k)
            return normalized
        # Lists / tuples / sets / iterators — convert generically. The
        # try/except guards against exotic types whose ``__iter__``
        # raises (e.g. lazy proxies).
        try:
            return list(raw)
        except Exception:
            return []

    # Cache so we only log dropped-kwarg warnings once per class+kwargset.
    _LOGGED_DROPPED_KWARGS: set = set()

    @classmethod
    def _filter_supported_kwargs(
        cls, target_callable: Any, kwargs: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Return only the kwargs that ``target_callable`` accepts.

        The vendor SDK's signatures have changed across versions (e.g.
        ``firstrade==0.0.38`` does not accept ``save_session``). Rather
        than hard-couple this client to one library version, we
        introspect the constructor and drop unsupported kwargs.
        Dropped names are logged exactly once per (callable id, name)
        pair so behaviour drift stays visible without spamming logs.
        """
        import inspect
        try:
            sig = inspect.signature(target_callable)
        except (TypeError, ValueError):
            # If introspection fails, fall back to the original kwargs;
            # construction will raise its own clear error.
            return dict(kwargs)
        params = sig.parameters
        # If the target accepts **kwargs, all names are fine.
        accepts_var_kw = any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
        )
        if accepts_var_kw:
            return dict(kwargs)
        accepted: Dict[str, Any] = {}
        for name, value in kwargs.items():
            if name in params:
                accepted[name] = value
                continue
            cache_key = (id(target_callable), name)
            if cache_key not in cls._LOGGED_DROPPED_KWARGS:
                cls._LOGGED_DROPPED_KWARGS.add(cache_key)
                logger.warning(
                    "[firstrade] dropping unsupported FTSession kwarg %r "
                    "(SDK %s does not accept it)",
                    name,
                    getattr(target_callable, "__module__", "?"),
                )
        return accepted

    @staticmethod
    def _extract_real_account_number(raw: Any) -> str:
        """Pull the real account number out of a vendor account row.

        ``firstrade==0.0.38`` returns ``all_accounts`` as a **list of
        plain account-number strings** (not dicts) — older / newer
        releases sometimes wrap them in a small object or dict. We
        accept both shapes, plus a numeric form, so this connector
        survives small SDK reshuffles. The result is used only inside
        this client's in-memory ``_account_map`` and never leaves the
        package via any DTO / log / response.
        """
        # 1) Bare string / number — the common 0.0.38 shape.
        if isinstance(raw, (str, int)):
            return str(raw).strip()

        # 2) Dict / object with one of the known field names.
        # ``_account_key`` is the synthetic key our ``_normalize_accounts_iterable``
        # injects when the vendor returns ``{account_number: {details}}`` —
        # check it FIRST because it carries the dict key (i.e. the
        # actual account number) which is the canonical identifier.
        candidate = _first_present(
            raw,
            "_account_key",
            "account",
            "account_number",
            "accountNo",
            "accountNumber",
            "accountID",
            "account_id",
            "id",
            "number",
            "AcctNumber",
            "acct_number",
        )
        if candidate is None:
            return ""
        return str(candidate).strip()

    def _resolve_target_accounts(
        self, account_hash_or_alias: Optional[str],
    ) -> List[Tuple[str, str, str, str]]:
        """Return ``[(real_account, account_hash, last4, alias), ...]``
        for the requested filter. ``None`` selects every known
        account; otherwise we match either by hash or by alias suffix
        (last4)."""
        if not self._account_map:
            self.list_accounts()  # refresh map; idempotent
        targets: List[Tuple[str, str, str, str]] = []
        wanted = (account_hash_or_alias or "").strip().lower() or None
        for account_hash, real_account in list(self._account_map.items()):
            last4, alias = mask_account_number(real_account)
            if wanted is None or wanted in {
                account_hash.lower(),
                alias.lower(),
                last4.lower(),
            }:
                targets.append((real_account, account_hash, last4, alias))
        return targets

    # ------------------------------------------------------------------
    # Per-account fetch helpers — split out so the iteration logic
    # stays readable. Each one converts the vendor's per-call result
    # into our DTO list, redacting the raw payload on the way out.
    # ------------------------------------------------------------------

    # =================================================================
    # Read pipeline (firstrade==0.0.38)
    # =================================================================
    #
    # The MaxxRK/firstrade-api library's read methods return HTTP
    # response envelopes — dicts shaped like::
    #
    #   data.get_positions(account=X)       → {"statusCode", "account",
    #                                          "items": [position_dicts],
    #                                          "total_market_value", ...}
    #   data.get_account_balances(account=X) → {"statusCode", "result":
    #                                           {balance_fields}, ...}
    #   data.get_orders(account=X)          → {"items": [order_dicts], ...}
    #   data.get_account_history(account=X) → {"items": [tx_dicts], ...}
    #
    # The per-row data lives inside ``items`` (lists) or ``result``
    # (single detail dict for balances). The earlier "0 positions /
    # 10 fake orders / 7 fake transactions" regression came from
    # reading non-existent dynamic attrs and falling back to
    # ``list(envelope.values())`` which exposed envelope metadata as
    # fake rows.

    def _invoke_read_method(
        self,
        account_data: Any,
        *,
        method_name: str,
        account: str,
        phase_label: str,
        extra_kwargs: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Invoke a vendor read method and return the response envelope.

        Tries ``fn(account=X, **extra_kwargs)`` first (canonical 0.0.38
        signature); falls back to positional if the SDK rejects
        keyword args. Errors are sanitised and the call is best-effort
        (returns ``None``) so one failing endpoint doesn't poison the
        rest of the sync.
        """
        fn = getattr(account_data, method_name, None)
        if fn is None or not callable(fn):
            logger.info(
                "[firstrade] %s: vendor SDK has no callable %r; skipping",
                phase_label, method_name,
            )
            return None
        kwargs = dict(extra_kwargs or {})
        kwargs["account"] = account
        last_4 = mask_account_number(account)[0]
        try:
            ret = fn(**kwargs)
        except TypeError as exc:
            logger.debug(
                "[firstrade] %s: kwarg call hit TypeError (%s); "
                "retrying positional",
                phase_label, _sanitize_exception(exc),
            )
            try:
                ret = fn(account, *list((extra_kwargs or {}).values()))
            except Exception as exc2:  # noqa: BLE001
                logger.warning(
                    "[firstrade] %s: vendor call %s(****%s, ...) failed: %s",
                    phase_label, method_name, last_4,
                    _sanitize_exception(exc2),
                )
                return None
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[firstrade] %s: vendor call %s(account=****%s) failed: %s",
                phase_label, method_name, last_4, _sanitize_exception(exc),
            )
            return None
        # One-shot diagnostic dump so we can spot SDK shape drift in
        # production without redeploying. Rate-limited by phase label.
        self._log_post_call_attrs(account_data, phase_label, account, ret)
        return ret

    @staticmethod
    def _extract_response_items(response: Any) -> List[Any]:
        """Pull the per-row list out of a vendor HTTP envelope.

        Accepts a handful of variations defensively:
          * already-a-list → returned as-is
          * dict with ``items`` / ``data`` / ``rows`` / ``results`` →
            return that list
          * anything else → ``[]``
        We deliberately do NOT fall back to ``list(dict.values())`` —
        that's exactly the bug that exposed envelope metadata
        (``statusCode``, ``pagination``, …) as fake rows.
        """
        if response is None:
            return []
        if isinstance(response, list):
            return list(response)
        if isinstance(response, dict):
            for key in ("items", "data", "rows", "results"):
                value = response.get(key)
                if isinstance(value, list):
                    return list(value)
        return []

    @staticmethod
    def _extract_response_dict(
        response: Any, key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Pull a per-row detail dict out of a vendor HTTP envelope.

        Used for ``get_account_balances`` whose detail lives under
        ``result``. If ``key`` is set we descend into that key; if the
        envelope already looks like a flat detail dict (it carries one
        of the known balance hint fields) we return it unchanged.
        """
        if response is None:
            return {}
        if not isinstance(response, dict):
            return {}
        if key is None:
            return response
        value = response.get(key)
        if isinstance(value, dict):
            return value
        if isinstance(value, (int, float)):
            return {"total_value": float(value)}
        # Envelope but inner key absent — accept the outer dict as a
        # flat detail dict if it carries any recognised balance field.
        for hint in (
            "cash", "buying_power", "buyingPower", "total_value",
            "equity", "account_value", "net_account_value",
        ):
            if hint in response:
                return response
        return {}

    # Per-class one-shot logger to avoid spamming logs with the same
    # diagnostic dump on every sync.
    _LOGGED_POST_CALL: set = set()

    @classmethod
    def _log_post_call_attrs(
        cls, account_data: Any, phase_label: str,
        account: str, ret_value: Any,
    ) -> None:
        """One-time INFO log per (phase, sample-key-presence) showing
        the shape of the dynamic attribute that should hold details
        right after a side-effect method has been called.

        Only the attribute *shape* is logged (key types / nested key
        sample) — never raw values, never full account numbers."""
        key = phase_label
        if key in cls._LOGGED_POST_CALL:
            return
        cls._LOGGED_POST_CALL.add(key)
        last_4 = mask_account_number(account)[0]
        candidates = (
            "securities_held", "positions",
            "orders", "order_history",
            "account_balances", "balance",
            "account_history", "history", "transactions",
        )
        for attr_name in candidates:
            attr = getattr(account_data, attr_name, None)
            if attr is None:
                continue
            if isinstance(attr, dict):
                first_key = next(iter(attr.keys()), None)
                first_val = attr.get(first_key) if first_key is not None else None
                # Probe inner shape: if inner is dict, sample its keys.
                inner_keys: List[str] = []
                if isinstance(first_val, dict):
                    inner_keys = sorted(str(k) for k in first_val.keys())[:20]
                logger.info(
                    "[firstrade] post-%s: %s = dict(len=%d, "
                    "first_key_type=%s, first_value_type=%s, "
                    "inner_keys_sample=%s)",
                    phase_label, attr_name, len(attr),
                    type(first_key).__name__,
                    type(first_val).__name__,
                    inner_keys,
                )
            elif isinstance(attr, list):
                first_item = attr[0] if attr else None
                inner_keys: List[str] = []
                if isinstance(first_item, dict):
                    inner_keys = sorted(str(k) for k in first_item.keys())[:20]
                logger.info(
                    "[firstrade] post-%s: %s = list(len=%d, "
                    "first_item_type=%s, inner_keys=%s)",
                    phase_label, attr_name, len(attr),
                    type(first_item).__name__, inner_keys,
                )
        # Also log the return value shape.
        ret_keys: List[str] = []
        if isinstance(ret_value, dict):
            ret_keys = sorted(str(k) for k in ret_value.keys())[:20]
        elif isinstance(ret_value, list) and ret_value and isinstance(ret_value[0], dict):
            ret_keys = sorted(str(k) for k in ret_value[0].keys())[:20]
        logger.info(
            "[firstrade] post-%s: ret_type=%s, ret_keys=%s, account_last4=****%s",
            phase_label, type(ret_value).__name__, ret_keys, last_4,
        )

    # -----------------------------------------------------------------
    # Payload → DTO mappers (used by the new pipeline). These read from
    # vendor's already-populated detail dicts, so the field names below
    # match what the SDK actually writes (verified via post-call logs).
    # -----------------------------------------------------------------

    def _payload_to_balance(
        self, payload: Dict[str, Any],
        account_hash: str, last4: str, alias: str,
    ) -> BrokerBalance:
        return BrokerBalance(
            broker=self.BROKER_NAME,
            account_hash=account_hash,
            account_last4=last4,
            account_alias=alias,
            cash=_to_float(
                _first_present(payload, "cash", "available_cash", "cash_balance")
            ),
            buying_power=_to_float(
                _first_present(payload, "buying_power", "buyingPower", "available")
            ),
            total_value=_to_float(
                _first_present(
                    payload, "total_value", "equity", "account_value",
                    "total_account_value", "net_account_value",
                )
            ),
            currency=_first_present(payload, "currency") or "USD",
            as_of=_now_iso(),
            raw_payload=_as_dict(payload),
        )

    def _payload_to_position(
        self,
        symbol: str,
        detail: Dict[str, Any],
        account_hash: str,
        last4: str,
        alias: str,
    ) -> BrokerPosition:
        # Vendor 0.0.38 per-position field names (derived from the
        # ``total_*`` aggregates visible in the envelope and confirmed
        # via production diagnostic logs): ``daychange_amount``,
        # ``daychange_percent``, ``gainloss``, ``gainloss_percent``,
        # ``market_value``, ``cost_basis``.
        last_price = _to_float(
            _first_present(
                detail, "last_price", "lastPrice", "price", "current_price",
                "currentPrice", "last", "mark",
            )
        )
        prev_close = _to_float(
            _first_present(
                detail, "prev_close", "previousClose", "previous_close",
                "prev_close_price",
            )
        )
        day_change = _to_float(
            _first_present(
                detail,
                "daychange_amount", "daychangeAmount",  # vendor 0.0.38
                "day_change", "dayChange", "change", "change_amount",
                "net_change", "change_dollar",
            )
        )
        day_change_pct = _to_float(
            _first_present(
                detail,
                "daychange_percent", "daychangePercent",  # vendor 0.0.38
                "day_change_pct", "dayChangePct", "change_pct",
                "change_percent", "percent_change",
            )
        )
        if day_change is None and last_price is not None and prev_close:
            day_change = last_price - prev_close
        if (
            day_change_pct is None
            and last_price is not None
            and prev_close
        ):
            try:
                day_change_pct = (last_price - prev_close) / prev_close * 100.0
            except ZeroDivisionError:
                day_change_pct = None
        return BrokerPosition(
            broker=self.BROKER_NAME,
            account_hash=account_hash,
            account_last4=last4,
            account_alias=alias,
            symbol=str(
                _first_present(detail, "symbol", "ticker", "sym") or symbol
            ),
            quantity=_to_float(
                _first_present(detail, "quantity", "qty", "shares", "amount")
            ),
            market_value=_to_float(
                _first_present(
                    detail, "market_value", "marketValue", "value",
                    "current_market_value", "total_value",
                )
            ),
            avg_cost=_to_float(
                _first_present(
                    detail,
                    "cost_basis", "costBasis",  # vendor 0.0.38
                    "avg_cost", "average_cost", "averageCost",
                    "cost", "avg_price", "average_price",
                )
            ),
            last_price=last_price,
            unrealized_pnl=_to_float(
                _first_present(
                    detail,
                    "gainloss", "gainLoss",  # vendor 0.0.38
                    "unrealized_pnl", "unrealizedPnl",
                    "unrealized_gain_loss", "pnl", "gain_loss",
                )
            ),
            day_change=day_change,
            day_change_pct=day_change_pct,
            currency=_first_present(detail, "currency") or "USD",
            as_of=_now_iso(),
            raw_payload=_as_dict(detail),
        )

    def _payload_to_order(
        self,
        raw: Any,
        account_hash: str,
        last4: str,
        alias: str,
    ) -> BrokerOrder:
        salt = self._salt()
        raw_id = str(
            _first_present(
                raw, "order_id", "orderId", "id", "OrderId", "orderno",
                "order_no", "order_number",
            ) or ""
        )
        return BrokerOrder(
            broker=self.BROKER_NAME,
            account_hash=account_hash,
            account_last4=last4,
            account_alias=alias,
            order_id_hash=hash_broker_id(raw_id, salt) if raw_id else "",
            symbol=str(_first_present(raw, "symbol", "ticker") or "") or None,
            order_status=_first_present(
                raw, "order_status", "status", "state",
            ),
            order_side=_first_present(raw, "side", "action", "buy_sell"),
            order_type=_first_present(raw, "order_type", "type"),
            order_quantity=_to_float(
                _first_present(raw, "quantity", "qty", "shares", "order_quantity")
            ),
            filled_quantity=_to_float(
                _first_present(raw, "filled", "filled_quantity", "executed")
            ),
            limit_price=_to_float(
                _first_present(raw, "limit_price", "price", "limitPrice")
            ),
            as_of=_now_iso(),
            raw_payload=_as_dict(raw),
        )

    def _payload_to_transaction(
        self,
        raw: Any,
        account_hash: str,
        last4: str,
        alias: str,
    ) -> BrokerTransaction:
        salt = self._salt()
        raw_id = str(
            _first_present(
                raw, "transaction_id", "transactionId", "id", "TransactionId",
                "history_id", "trade_id",
            ) or ""
        )
        return BrokerTransaction(
            broker=self.BROKER_NAME,
            account_hash=account_hash,
            account_last4=last4,
            account_alias=alias,
            transaction_id_hash=(
                hash_broker_id(raw_id, salt) if raw_id else ""
            ),
            symbol=str(_first_present(raw, "symbol", "ticker") or "") or None,
            transaction_type=_first_present(
                raw, "type", "transaction_type", "action", "activity",
            ),
            trade_date=str(
                _first_present(raw, "trade_date", "date", "transaction_date") or ""
            ) or None,
            settle_date=str(
                _first_present(raw, "settle_date", "settlement_date") or ""
            ) or None,
            amount=_to_float(
                _first_present(raw, "amount", "net_amount", "value")
            ),
            quantity=_to_float(
                _first_present(raw, "quantity", "qty", "shares")
            ),
            currency=_first_present(raw, "currency") or "USD",
            raw_payload=_as_dict(raw),
        )

    # ------------------------------------------------------------------
    # Snapshot composition
    # ------------------------------------------------------------------


    def build_snapshot(self, *, date_range: str = "today") -> BrokerSnapshot:
        """Convenience: aggregate accounts + balances + positions +
        orders + transactions into one snapshot. Used by the sync
        service; can also be called from tests for shape verification.
        """
        accounts = self.list_accounts()
        return BrokerSnapshot(
            broker=self.BROKER_NAME,
            as_of=_now_iso(),
            accounts=accounts,
            balances=self.get_balances(),
            positions=self.get_positions(),
            orders=self.get_orders(),
            transactions=self.get_transactions(date_range=date_range),
        )


class _SdkNotInstalled(RuntimeError):
    """Raised internally when ``import firstrade`` fails so the public
    API can map it to a structured ``not_installed`` response."""
