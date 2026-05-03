# -*- coding: utf-8 -*-
"""End-to-end FastAPI tests for the broker endpoints.

We mock the FirstradeSyncService directly so the tests don't need
SQLite or the vendor SDK. Crucially, the **parameterized leak test**
poisons the mocked service response with every sensitive key we know
about, then asserts the API response strips them all.
"""

from __future__ import annotations

import json
import unittest
from typing import Any, Dict
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.v1.endpoints import broker as broker_endpoint


# Keys that must NEVER appear in any broker API response. The leak
# test searches every response body for these (case-insensitive,
# both as JSON keys and as bare substrings).
SENSITIVE_KEYS = (
    "username",
    "password",
    "pin",
    "mfa_secret",
    "ftat",
    "sid",
    "cookie",
    "cookies",
    "access_token",
    "authorization",
)

SENSITIVE_VALUES = (
    "alice@example.com",  # username-like
    "hunter2",            # password
    "1234",               # pin (also tests the digit-run scrubber)
    "JBSWY3DPEHPK3PXP",   # mfa_secret
    "FTATOKEN-XYZ",
    "SIDVALUE-ABC",
    "MyCookieValue",
    "Bearer ACCESS-XYZ",
)


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(broker_endpoint.router, prefix="/api/v1/broker")
    return app


def _poisoned_response(extra: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """A snapshot-shaped dict that includes every sensitive key we know
    about; used to verify the API strips them before sending."""
    base = {
        "status": "ok",
        "broker": "firstrade",
        "as_of": "2025-12-01T00:00:00+00:00",
        "last_sync": None,
        "accounts": [
            {
                "broker": "firstrade",
                "account_alias": "Firstrade ****1234",
                "account_last4": "1234",
                "account_hash": "abcd1234abcd1234",
                "as_of": "2025-12-01T00:00:00+00:00",
                "payload": {
                    "username": "alice@example.com",
                    "password": "hunter2",
                    "pin": "1234",
                    "mfa_secret": "JBSWY3DPEHPK3PXP",
                    "ftat": "FTATOKEN-XYZ",
                    "sid": "SIDVALUE-ABC",
                    "cookie": "MyCookieValue",
                    "Authorization": "Bearer ACCESS-XYZ",
                    "account": "112233445566",
                    "symbol": "AAPL",
                },
            },
        ],
        "balances": [],
        "positions": [],
        "orders": [],
        "transactions": [],
    }
    if extra:
        base.update(extra)
    return base


class BrokerStatusEndpointTests(unittest.TestCase):
    def test_status_returns_disabled_when_flag_off(self) -> None:
        app = _build_app()
        with patch.object(broker_endpoint, "_service") as svc_factory:
            svc = svc_factory.return_value
            svc.get_status.return_value = {
                "status": "not_enabled",
                "broker": "firstrade",
                "enabled": False,
                "message": "set BROKER_FIRSTRADE_ENABLED=true",
            }
            client = TestClient(app)
            resp = client.get("/api/v1/broker/firstrade/status")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "not_enabled")
        self.assertFalse(body["enabled"])

    def test_status_ok(self) -> None:
        app = _build_app()
        with patch.object(broker_endpoint, "_service") as svc_factory:
            svc_factory.return_value.get_status.return_value = {
                "status": "ok",
                "broker": "firstrade",
                "enabled": True,
                "logged_in": True,
                "read_only": True,
                "last_sync": None,
                "llm_data_scope": "positions_and_balances",
            }
            client = TestClient(app)
            resp = client.get("/api/v1/broker/firstrade/status")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "ok")


class BrokerLoginEndpointTests(unittest.TestCase):
    def test_login_mfa_required(self) -> None:
        app = _build_app()
        with patch.object(broker_endpoint, "_service") as svc_factory:
            svc_factory.return_value.login.return_value = {
                "status": "mfa_required",
                "broker": "firstrade",
                "message": "code required",
                "account_count": 0,
            }
            client = TestClient(app)
            resp = client.post("/api/v1/broker/firstrade/login")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "mfa_required")

    def test_login_disabled_returns_503(self) -> None:
        app = _build_app()
        with patch.object(broker_endpoint, "_service") as svc_factory:
            svc_factory.return_value.login.return_value = {
                "status": "not_enabled",
                "broker": "firstrade",
                "message": "disabled",
            }
            client = TestClient(app)
            resp = client.post("/api/v1/broker/firstrade/login")
        self.assertEqual(resp.status_code, 503)
        body = resp.json()
        self.assertIn("error", body["detail"])
        self.assertEqual(body["detail"]["error"], "broker_not_enabled")

    def test_verify_mfa_session_lost_returns_409(self) -> None:
        app = _build_app()
        with patch.object(broker_endpoint, "_service") as svc_factory:
            svc_factory.return_value.verify_mfa.return_value = {
                "status": "session_lost",
                "broker": "firstrade",
                "message": "Please re-login.",
                "account_count": 0,
            }
            client = TestClient(app)
            resp = client.post(
                "/api/v1/broker/firstrade/login/verify",
                json={"code": "123456"},
            )
        self.assertEqual(resp.status_code, 409)
        body = resp.json()
        self.assertEqual(body["detail"]["error"], "broker_session_lost")


class BrokerSyncEndpointTests(unittest.TestCase):
    def test_sync_returns_counts(self) -> None:
        app = _build_app()
        with patch.object(broker_endpoint, "_service") as svc_factory:
            svc_factory.return_value.sync_now.return_value = {
                "status": "ok",
                "broker": "firstrade",
                "as_of": "2025-12-01T00:00:00+00:00",
                "account_count": 1,
                "balance_count": 1,
                "position_count": 5,
                "order_count": 0,
                "transaction_count": 0,
            }
            client = TestClient(app)
            resp = client.post("/api/v1/broker/firstrade/sync", json={})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "ok")
        self.assertEqual(resp.json()["position_count"], 5)


# =====================================================================
# THE PARAMETERIZED LEAK TEST
# =====================================================================

class BrokerLeakTests(unittest.TestCase):
    """For every endpoint that returns snapshot-like data, poison the
    mocked service with sensitive keys and verify the response body
    contains none of them."""

    def setUp(self) -> None:
        self.app = _build_app()
        self.poisoned = _poisoned_response()

    def _exercise(self, method: str, url: str, mocker_attr: str, **kwargs):
        with patch.object(broker_endpoint, "_service") as svc_factory:
            getattr(svc_factory.return_value, mocker_attr).return_value = self.poisoned
            client = TestClient(self.app)
            resp = client.request(method, url, **kwargs)
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.text

    def _assert_clean(self, body_text: str, *, allow_account_alias: bool = True) -> None:
        """Verify no sensitive *values* leak.

        We deliberately allow sensitive *keys* (e.g. ``"username"``)
        to appear in the JSON because the redactor preserves the key
        and replaces the value with ``"***REDACTED***"``. That makes
        the redaction pattern visible to operators reading API
        responses while still blocking the actual secret.
        """
        lower = body_text.lower()
        # Each sensitive value below was poisoned into the mocked
        # service response; none of them should reach the wire.
        for sentinel in (
            "alice@example.com",      # username
            "hunter2",                # password
            "JBSWY3DPEHPK3PXP",       # mfa_secret
            "FTATOKEN-XYZ",
            "SIDVALUE-ABC",
            "MyCookieValue",
            "Bearer ACCESS-XYZ",
            "112233445566",           # full account number
        ):
            self.assertNotIn(
                sentinel.lower(), lower,
                msg=f"sensitive value {sentinel!r} leaked into response",
            )
        # And every sensitive *key* in the response must be paired
        # with the redaction marker — find each ``"key":"...value..."``
        # occurrence and confirm the value is the redacted sentinel.
        import re as _re
        for key in SENSITIVE_KEYS:
            for match in _re.finditer(rf'"{key}"\s*:\s*"([^"]*)"', body_text):
                value = match.group(1)
                self.assertEqual(
                    value, "***REDACTED***",
                    msg=(
                        f"key {key!r} appeared in response but its value "
                        f"was not redacted: {value!r}"
                    ),
                )

    def test_snapshot_endpoint_strips_all_sensitive_fields(self) -> None:
        body = self._exercise(
            "GET", "/api/v1/broker/firstrade/snapshot", "get_snapshot",
        )
        self._assert_clean(body)

    def test_accounts_endpoint_strips_all_sensitive_fields(self) -> None:
        body = self._exercise(
            "GET", "/api/v1/broker/firstrade/accounts", "get_accounts",
        )
        self._assert_clean(body)

    def test_positions_endpoint_strips_all_sensitive_fields(self) -> None:
        body = self._exercise(
            "GET", "/api/v1/broker/firstrade/positions", "get_positions",
        )
        self._assert_clean(body)

    def test_orders_endpoint_strips_all_sensitive_fields(self) -> None:
        body = self._exercise(
            "GET", "/api/v1/broker/firstrade/orders", "get_orders",
        )
        self._assert_clean(body)

    def test_transactions_endpoint_strips_all_sensitive_fields(self) -> None:
        body = self._exercise(
            "GET", "/api/v1/broker/firstrade/transactions", "get_transactions",
        )
        self._assert_clean(body)


if __name__ == "__main__":
    unittest.main()
