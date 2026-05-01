# -*- coding: utf-8 -*-
"""Regression tests for Cloud Run-specific security hardening."""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from starlette.requests import Request


# Keep importing server.py deterministic when optional LLM deps are absent.
try:
    import litellm  # noqa: F401
except ModuleNotFoundError:
    sys.modules["litellm"] = MagicMock()

import server
from src.services.cloud_scheduler_service import _configured_oidc_audience


def _request_with_bearer(token: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/analyze",
            "headers": [(b"authorization", f"Bearer {token}".encode("ascii"))],
            "query_string": b"",
            "scheme": "https",
            "client": ("127.0.0.1", 1234),
            "server": ("testserver", 443),
            "root_path": "",
        }
    )


class CloudRunBearerAuthTests(unittest.TestCase):
    def test_non_jwt_bearer_does_not_call_oidc_verifier(self) -> None:
        with patch.dict(os.environ, {"API_TOKEN": "expected"}, clear=False):
            with patch.object(server, "_allowed_oidc_invokers", return_value=["sa@example.com"]):
                with patch.object(server, "_try_verify_oidc_token") as verifier:
                    with self.assertRaises(HTTPException) as ctx:
                        server._require_api_token(_request_with_bearer("wrong-token"))

        self.assertEqual(ctx.exception.status_code, 401)
        verifier.assert_not_called()

    def test_static_api_token_uses_constant_time_compare_path(self) -> None:
        with patch.dict(os.environ, {"API_TOKEN": "expected"}, clear=False):
            with patch.object(server, "_allowed_oidc_invokers", return_value=[]):
                with patch.object(server.hmac, "compare_digest", return_value=True) as compare:
                    server._require_api_token(_request_with_bearer("expected"))

        compare.assert_called_once_with("expected", "expected")

    def test_oidc_expected_audiences_accepts_comma_list(self) -> None:
        with patch.dict(
            os.environ,
            {"OIDC_EXPECTED_AUDIENCES": "https://svc.run.app/, https://rev.run.app"},
            clear=False,
        ):
            self.assertEqual(
                server._expected_oidc_audiences(),
                ["https://svc.run.app", "https://rev.run.app"],
            )

    def test_scheduler_uses_same_canonical_audience(self) -> None:
        with patch.dict(
            os.environ,
            {"OIDC_EXPECTED_AUDIENCES": "https://svc.run.app,https://rev.run.app"},
            clear=False,
        ):
            self.assertEqual(
                _configured_oidc_audience("https://fallback.run.app"),
                "https://svc.run.app",
            )


class CloudRunAnalyzeLimitTests(unittest.TestCase):
    def test_stock_list_limit_rejects_too_many_codes(self) -> None:
        with patch.object(server, "_CLOUD_RUN_MAX_STOCKS", 1):
            with self.assertRaises(ValueError):
                server._validate_cloudrun_stocks(["AAPL", "MSFT"])

    def test_stock_code_format_rejects_unsupported_characters(self) -> None:
        with self.assertRaises(ValueError):
            server._validate_cloudrun_stocks(["AAPL;DROP"])

    def test_info_no_longer_exposes_default_stock_list(self) -> None:
        with patch.dict(os.environ, {"STOCK_LIST": "AAPL,MSFT"}, clear=False):
            payload = asyncio.run(server.cloud_run_info())

        self.assertNotIn("default_stock_list", payload)
        self.assertIn("limits", payload)


class CloudRunAsyncTaskLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        server._TASKS.clear()

    def tearDown(self) -> None:
        server._TASKS.clear()

    def test_new_task_rejects_when_active_limit_reached(self) -> None:
        with patch.object(server, "_ASYNC_MAX_ACTIVE_TASKS", 1):
            server._new_task(["AAPL"])
            with self.assertRaisesRegex(RuntimeError, "too_many_active_async_tasks"):
                server._new_task(["MSFT"])

    def test_expired_tasks_are_purged_on_lookup(self) -> None:
        task_id = server._new_task(["AAPL"])
        server._TASKS[task_id]["expires_at"] = 0

        self.assertIsNone(server._get_task(task_id))
        self.assertNotIn(task_id, server._TASKS)


if __name__ == "__main__":
    unittest.main()
