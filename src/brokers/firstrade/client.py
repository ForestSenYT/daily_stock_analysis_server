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
                account_count=self._safe_len(getattr(account_data, "all_accounts", None)),
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
                account_count=self._safe_len(
                    getattr(self._sdk.account_data, "all_accounts", None)
                ),
            )

    def logout(self) -> None:
        with self._lock:
            self._sdk = None
            self._account_map.clear()

    # ----------------------- read paths --------------------------

    def list_accounts(self) -> List[BrokerAccount]:
        sdk = self._require_logged_in()
        if sdk is None:
            return []
        accounts: List[BrokerAccount] = []
        try:
            raw_attr = getattr(sdk.account_data, "all_accounts", None)
            raw_accounts = self._normalize_accounts_iterable(raw_attr)
            # Optional collapse: when the vendor returned a dict with N
            # sub-account "views" (cash / margin / IRA / Roth / options
            # etc.) we present them as a SINGLE primary account in the
            # UI/Agent surface. The flag is on by default — flip it off
            # via ``BROKER_FIRSTRADE_MERGE_SUB_ACCOUNTS=false`` if you
            # genuinely hold multiple independent accounts.
            should_merge = bool(
                getattr(self._config, "broker_firstrade_merge_sub_accounts", True)
            )
            pre_merge_len = len(raw_accounts)
            if (
                should_merge
                and isinstance(raw_attr, dict)
                and pre_merge_len > 1
            ):
                # Keep the first dict entry as the canonical primary;
                # the other sub-accounts will dispatch to this one when
                # vendor doesn't tag rows per-account.
                raw_accounts = raw_accounts[:1]
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[firstrade] list_accounts iteration failed: %s",
                _sanitize_exception(exc),
            )
            return []
        logger.info(
            "[firstrade] list_accounts: vendor returned attr_type=%s, "
            "iterable_len=%d, after_merge=%d, item_types=%s",
            type(raw_attr).__name__,
            pre_merge_len,
            len(raw_accounts),
            [type(r).__name__ for r in raw_accounts[:5]],
        )
        salt = self._salt()
        skipped = 0
        for raw in raw_accounts:
            real_account = self._extract_real_account_number(raw)
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
                    raw_payload=_as_dict(raw),
                )
            )
        if skipped or not accounts:
            # Log row-shape diagnostics WITHOUT leaking the account
            # numbers themselves: only the type + key list. This makes
            # SDK-shape drift debuggable from logs alone.
            shapes = []
            for raw in raw_accounts[:5]:  # cap so log line stays small
                if isinstance(raw, dict):
                    shapes.append("dict(keys=%s)" % sorted(raw.keys())[:8])
                else:
                    shapes.append(type(raw).__name__)
            logger.warning(
                "[firstrade] list_accounts: total=%d, extracted=%d, skipped=%d. "
                "Vendor row shape sample (first 5): %s",
                len(raw_accounts), len(accounts), skipped, shapes,
            )
        return accounts

    def get_balances(
        self, account_hash_or_alias: Optional[str] = None,
    ) -> List[BrokerBalance]:
        return self._iter_per_account(
            account_hash_or_alias,
            self._fetch_balance_for,
        )

    def get_positions(
        self, account_hash_or_alias: Optional[str] = None,
    ) -> List[BrokerPosition]:
        return self._fetch_once_and_dispatch(
            method_names=("get_positions", "positions"),
            account_hash_or_alias=account_hash_or_alias,
            row_to_dto=self._raw_to_position,
        )

    def get_orders(
        self, account_hash_or_alias: Optional[str] = None,
    ) -> List[BrokerOrder]:
        return self._fetch_once_and_dispatch(
            method_names=("get_orders", "orders"),
            account_hash_or_alias=account_hash_or_alias,
            row_to_dto=self._raw_to_order,
        )

    def get_transactions(
        self,
        account_hash_or_alias: Optional[str] = None,
        date_range: str = "today",
    ) -> List[BrokerTransaction]:
        normalized = (date_range or "today").strip().lower()
        if normalized not in _VALID_DATE_RANGES:
            logger.warning(
                "[firstrade] get_transactions: unsupported date_range=%r; "
                "falling back to 'today'.",
                date_range,
            )
            normalized = "today"
        if normalized == "cust":
            # Phase-1: schema reserves "cust" but we don't ship the
            # custom-range fetch yet — silently downgrade.
            logger.info(
                "[firstrade] custom date range not implemented in v1; "
                "returning today's history."
            )
            normalized = "today"
        return self._fetch_once_and_dispatch(
            method_names=("get_history", "get_transactions", "transactions"),
            account_hash_or_alias=account_hash_or_alias,
            row_to_dto=self._raw_to_transaction,
            extra_args=(normalized,),
        )

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

    def _iter_per_account(
        self,
        account_hash_or_alias: Optional[str],
        fetcher,
        flat: bool = False,
    ):
        results: List[Any] = []
        sdk = self._require_logged_in()
        if sdk is None:
            return results
        for real, ah, last4, alias in self._resolve_target_accounts(account_hash_or_alias):
            try:
                fetched = fetcher(real, ah, last4, alias)
            except Exception as exc:  # noqa: BLE001 — boundary
                logger.warning(
                    "[firstrade] fetch on %s failed: %s",
                    ah,  # never log alias / real number
                    _sanitize_exception(exc),
                )
                continue
            if fetched is None:
                continue
            if flat and isinstance(fetched, list):
                results.extend(fetched)
            else:
                results.append(fetched)
        return results

    # ------------------------------------------------------------------
    # Per-account fetch helpers — split out so the iteration logic
    # stays readable. Each one converts the vendor's per-call result
    # into our DTO list, redacting the raw payload on the way out.
    # ------------------------------------------------------------------

    def _fetch_balance_for(
        self, real: str, account_hash: str, last4: str, alias: str,
    ) -> Optional[BrokerBalance]:
        sdk = self._sdk
        if sdk is None:
            return None
        # The vendor library exposes per-account balance under
        # ``account_balance`` / ``balance``. Fall through gracefully if
        # the attribute moves around between SDK versions.
        for method in ("get_account_balance", "get_balance", "account_balance"):
            fn = getattr(sdk.account_data, method, None)
            if fn is None:
                continue
            try:
                raw = fn(real) if callable(fn) else fn
            except TypeError:
                # Some attributes are dicts, not callables.
                raw = fn  # type: ignore[assignment]
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(_sanitize_exception(exc)) from None
            return BrokerBalance(
                broker=self.BROKER_NAME,
                account_hash=account_hash,
                account_last4=last4,
                account_alias=alias,
                cash=_to_float(_first_present(raw, "cash", "Cash", "available_cash")),
                buying_power=_to_float(_first_present(raw, "buying_power", "BuyingPower")),
                total_value=_to_float(_first_present(raw, "total_value", "TotalValue", "equity")),
                currency=_first_present(raw, "currency", "Currency") or "USD",
                as_of=_now_iso(),
                raw_payload=_as_dict(raw),
            )
        return None

    # -----------------------------------------------------------------
    # NEW pipeline (vendor-call-once + per-row dispatch)
    # -----------------------------------------------------------------
    #
    # The Firstrade SDK ignores the ``real_account_number`` argument
    # passed to ``get_positions`` / ``get_orders`` — it returns the
    # full set of positions regardless. Iterating per sub-account
    # (the original design) duplicated every row N times where N =
    # number of sub-accounts. The new flow:
    #
    #   1. Resolve the user-requested filter (or all sub-accounts).
    #   2. Pick a *primary* sub-account to drive the single vendor
    #      call (the vendor still wants A account-number argument).
    #   3. Iterate rows once. For each row, look at any vendor-side
    #      account-id field (``account`` / ``account_id`` / etc.) —
    #      if present and matching one of OUR sub-account hashes,
    #      assign the row to that sub-account; otherwise fall back
    #      to the primary.
    #
    # This survives both: (a) SDKs that DO tag rows per account
    # (correctly distributed), (b) SDKs that don't (everything goes
    # under primary, no duplication).

    def _fetch_once_and_dispatch(
        self,
        *,
        method_names,
        account_hash_or_alias: Optional[str],
        row_to_dto,
        extra_args: Tuple = (),
    ) -> List[Any]:
        sdk = self._require_logged_in()
        if sdk is None:
            return []
        # All known sub-accounts (may be one) — needed both for the
        # per-row dispatch table and for filtering by user input.
        all_subs = self._resolve_target_accounts(None)
        if not all_subs:
            return []
        # Apply the user's filter AFTER we know the full list, so the
        # dispatch table still covers every sub-account that vendor
        # might tag a row with.
        wanted = (account_hash_or_alias or "").strip().lower() or None
        primary_real, primary_hash, primary_last4, primary_alias = all_subs[0]
        # Reverse map: real_account_number → (hash, last4, alias).
        real_to_meta = {
            real: (ah, l4, al)
            for real, ah, l4, al in all_subs
        }

        # Single vendor call with the primary account.
        raw_iter: Iterable[Any] = []
        for method in method_names:
            fn = getattr(sdk.account_data, method, None)
            if fn is None:
                continue
            try:
                raw_iter = self._invoke_vendor_method(
                    fn, primary_real, *extra_args,
                )
            except _VendorCallFailed as exc:
                raise RuntimeError(str(exc)) from None
            break
        rows = list(raw_iter or [])

        # One log line per call so you can see this in production logs:
        # how many rows we got, whether vendor tagged them with
        # account ids, what types they look like.
        tagged_count = 0
        for raw in rows[:5]:  # only sample for the log line
            row_account = self._extract_row_account_id(raw)
            if row_account:
                tagged_count += 1
        logger.info(
            "[firstrade] vendor returned %d rows from %s; "
            "%d/%d sample rows are tagged with an account id",
            len(rows), method_names[0], tagged_count, min(5, len(rows)),
        )
        # Also dump the first row's *shape* (keys / attrs, NO values)
        # so we can map vendor field names without guessing. Sensitive
        # keys are filtered before logging — see _safe_keys filter.
        if rows:
            self._log_row_shape(method_names[0], rows[0])
            # If the row is a bare string (a ticker symbol), the
            # detailed position object lives in some other attribute
            # on ``account_data`` (e.g. ``data.positions[ticker]``).
            # Dump every attribute on the account_data instance so we
            # can find the right detail source on the next round.
            if isinstance(rows[0], str):
                self._log_account_data_attrs(sdk.account_data, rows[0])

        results: List[Any] = []
        for raw in rows:
            row_account = self._extract_row_account_id(raw)
            if row_account and row_account in real_to_meta:
                ah, l4, al = real_to_meta[row_account]
            else:
                # Vendor didn't tag the row — fall back to primary.
                ah, l4, al = primary_hash, primary_last4, primary_alias
            # Apply user filter AFTER dispatch so users can
            # request ``account_alias`` and still get the right rows.
            if wanted is not None and wanted not in {
                ah.lower(), l4.lower(), al.lower(),
            }:
                continue
            try:
                dto = row_to_dto(raw, ah, l4, al)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[firstrade] row → DTO conversion failed: %s",
                    _sanitize_exception(exc),
                )
                continue
            if dto is not None:
                results.append(dto)
        return results

    @staticmethod
    def _invoke_vendor_method(fn, real, *extra):
        """Call vendor ``fn(real, *extra)`` with graceful fallbacks for
        signatures that take fewer args, that return non-iterables,
        or that are bare attributes (not callables)."""
        if not callable(fn):
            return fn or []
        for args in ((real, *extra), (real,), ()):
            try:
                return fn(*args)
            except TypeError:
                continue
            except Exception as exc:  # noqa: BLE001
                raise _VendorCallFailed(_sanitize_exception(exc)) from None
        # Every signature attempt raised TypeError — surface that.
        raise _VendorCallFailed(
            "vendor method signature did not match any of (real, *extra) / (real,) / ()"
        )

    @staticmethod
    def _log_account_data_attrs(account_data: Any, sample_ticker: str) -> None:
        """Dump every public attribute on ``FTAccountData`` so we can
        find where vendor stores the position / order / balance
        details. Called when ``get_positions`` returns bare strings
        (i.e. tickers) instead of objects — meaning details live
        elsewhere on the account_data instance.

        Logs SHAPE only (type + keys + sample-key) — never the
        underlying values, so it's safe even if those dicts contain
        prices / dollar amounts.
        """
        try:
            public = sorted(k for k in dir(account_data) if not k.startswith("_"))
        except Exception:
            return
        # First, the high-level attribute list so we can spot the
        # right detail dict.
        logger.info(
            "[firstrade] account_data public attrs: %s",
            public[:60],
        )
        # Then, for each attribute that looks like a dict / list, dump
        # its shape — this is the most useful signal (the one that
        # has ``sample_ticker`` as a key is almost certainly the
        # detail source).
        for attr_name in public:
            try:
                attr = getattr(account_data, attr_name, None)
            except Exception:
                continue
            if attr is None or callable(attr):
                continue
            if isinstance(attr, dict):
                keys = list(attr.keys())
                first_key = keys[0] if keys else None
                first_val = attr.get(first_key) if first_key is not None else None
                # Whether ``sample_ticker`` is a key — strong hint
                # that this is the detail dict for our position rows.
                ticker_match = sample_ticker in keys
                logger.info(
                    "[firstrade] account_data.%s: dict, len=%d, "
                    "sample_key=%r, sample_value_type=%s, "
                    "ticker_in_keys=%s",
                    attr_name,
                    len(keys),
                    str(first_key)[:40] if first_key is not None else None,
                    type(first_val).__name__,
                    ticker_match,
                )
            elif isinstance(attr, (list, tuple)):
                logger.info(
                    "[firstrade] account_data.%s: %s, len=%d, "
                    "first_item_type=%s",
                    attr_name,
                    type(attr).__name__,
                    len(attr),
                    type(attr[0]).__name__ if attr else None,
                )

    @staticmethod
    def _log_row_shape(method_name: str, sample: Any) -> None:
        """Dump the first vendor row's KEYS / ATTR NAMES (never values)
        so we can see exactly what field names the SDK is using —
        without that, ``_first_present`` is just guessing and the UI
        ends up showing dashes everywhere.

        Filters out anything that looks sensitive before logging."""
        sensitive = {
            "username", "password", "pin", "mfa_secret", "ftat",
            "sid", "cookie", "cookies", "authorization", "account",
            "account_number", "accountno", "accountnumber",
            "token", "access_token", "secret",
        }
        sample_type = type(sample).__name__
        dict_keys: List[str] = []
        attr_keys: List[str] = []
        if isinstance(sample, dict):
            dict_keys = sorted(str(k) for k in sample.keys())
        if hasattr(sample, "__dict__"):
            try:
                attr_keys = sorted(
                    k for k in vars(sample).keys() if not k.startswith("_")
                )
            except TypeError:
                attr_keys = []
        # Public non-callable attrs (covers @property and slots).
        try:
            public_attrs = sorted(
                k for k in dir(sample)
                if not k.startswith("_")
                and k.lower() not in sensitive
            )
        except Exception:  # pragma: no cover — defensive
            public_attrs = []
        safe_dict_keys = [k for k in dict_keys if k.lower() not in sensitive]
        safe_attr_keys = [k for k in attr_keys if k.lower() not in sensitive]
        logger.info(
            "[firstrade] sample row shape from %s: type=%s, "
            "dict_keys=%s, attr_keys=%s, public_attrs=%s",
            method_name,
            sample_type,
            safe_dict_keys[:40],
            safe_attr_keys[:40],
            public_attrs[:40],
        )

    @staticmethod
    def _extract_row_account_id(raw: Any) -> str:
        """Pull the vendor-side account id out of a raw position / order /
        transaction row, BEFORE redaction strips it. Empty when absent."""
        candidate = _first_present(
            raw,
            "account",
            "account_number",
            "accountNo",
            "accountNumber",
            "accountID",
            "account_id",
            "AcctNumber",
            "acct_number",
        )
        if candidate is None:
            return ""
        return str(candidate).strip()

    # -----------------------------------------------------------------
    # row → DTO mappers (used by the new pipeline; redaction applied
    # via ``_as_dict`` inside ``raw_payload``).
    # -----------------------------------------------------------------

    def _raw_to_position(
        self, raw: Any, account_hash: str, last4: str, alias: str,
    ) -> BrokerPosition:
        # Day-change candidates — the vendor SDK has churned through
        # several names; we try the common ones in order. If the
        # vendor returns ``last_price`` + ``prev_close`` only (no
        # pre-computed change), we derive day_change ourselves below.
        day_change = _to_float(
            _first_present(
                raw,
                "day_change",
                "dayChange",
                "DayChange",
                "change",
                "Change",
                "change_amount",
                "ChangeAmount",
                "net_change",
            )
        )
        day_change_pct = _to_float(
            _first_present(
                raw,
                "day_change_pct",
                "dayChangePct",
                "DayChangePercent",
                "change_pct",
                "ChangePercent",
                "ChangePct",
                "percent_change",
            )
        )
        last_price = _to_float(
            _first_present(raw, "last_price", "LastPrice", "price")
        )
        prev_close = _to_float(
            _first_present(
                raw, "prev_close", "previousClose", "PreviousClose",
            )
        )
        # Fallbacks: derive from last/prev when vendor didn't supply.
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
            symbol=str(_first_present(raw, "symbol", "Symbol", "ticker") or ""),
            quantity=_to_float(
                _first_present(raw, "quantity", "Quantity", "qty", "shares")
            ),
            market_value=_to_float(
                _first_present(raw, "market_value", "MarketValue", "value")
            ),
            avg_cost=_to_float(
                _first_present(raw, "avg_cost", "AvgCost", "average_cost")
            ),
            last_price=last_price,
            unrealized_pnl=_to_float(
                _first_present(raw, "unrealized_pnl", "UnrealizedPnl", "pnl")
            ),
            day_change=day_change,
            day_change_pct=day_change_pct,
            currency=_first_present(raw, "currency", "Currency") or "USD",
            as_of=_now_iso(),
            raw_payload=_as_dict(raw),
        )

    def _raw_to_order(
        self, raw: Any, account_hash: str, last4: str, alias: str,
    ) -> BrokerOrder:
        salt = self._salt()
        raw_id = str(
            _first_present(raw, "order_id", "orderId", "id", "OrderId") or ""
        )
        return BrokerOrder(
            broker=self.BROKER_NAME,
            account_hash=account_hash,
            account_last4=last4,
            account_alias=alias,
            order_id_hash=hash_broker_id(raw_id, salt) if raw_id else "",
            symbol=str(_first_present(raw, "symbol", "Symbol", "ticker") or "") or None,
            order_status=_first_present(raw, "status", "Status", "order_status"),
            order_side=_first_present(raw, "side", "Side", "action"),
            order_type=_first_present(raw, "order_type", "OrderType", "type"),
            order_quantity=_to_float(
                _first_present(raw, "quantity", "Quantity", "qty")
            ),
            filled_quantity=_to_float(
                _first_present(raw, "filled", "Filled", "filled_quantity")
            ),
            limit_price=_to_float(
                _first_present(raw, "limit_price", "LimitPrice", "price")
            ),
            as_of=_now_iso(),
            raw_payload=_as_dict(raw),
        )

    def _raw_to_transaction(
        self, raw: Any, account_hash: str, last4: str, alias: str,
    ) -> BrokerTransaction:
        salt = self._salt()
        raw_id = str(
            _first_present(
                raw,
                "transaction_id",
                "transactionId",
                "id",
                "TransactionId",
            )
            or ""
        )
        return BrokerTransaction(
            broker=self.BROKER_NAME,
            account_hash=account_hash,
            account_last4=last4,
            account_alias=alias,
            transaction_id_hash=(
                hash_broker_id(raw_id, salt) if raw_id else ""
            ),
            symbol=str(_first_present(raw, "symbol", "Symbol", "ticker") or "") or None,
            transaction_type=_first_present(
                raw, "type", "Type", "transaction_type", "action",
            ),
            trade_date=str(
                _first_present(raw, "trade_date", "TradeDate", "date") or ""
            ) or None,
            settle_date=str(
                _first_present(raw, "settle_date", "SettleDate") or ""
            ) or None,
            amount=_to_float(
                _first_present(raw, "amount", "Amount", "net_amount")
            ),
            quantity=_to_float(
                _first_present(raw, "quantity", "Quantity", "qty")
            ),
            currency=_first_present(raw, "currency", "Currency") or "USD",
            raw_payload=_as_dict(raw),
        )

    # ---- legacy per-account fetcher kept for compatibility ----------
    # ``_fetch_balance_for`` is still called from ``get_balances`` via
    # ``_iter_per_account`` because balances genuinely differ per
    # sub-account (cash account ≠ margin account ≠ IRA cash).
    #
    # The old ``_fetch_positions_for`` / ``_fetch_orders_for`` /
    # ``_fetch_transactions_for`` are intentionally removed — the new
    # ``_fetch_once_and_dispatch`` replaces them and avoids the
    # N×duplication bug that produced "5 accounts, 15 positions".

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
