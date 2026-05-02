# -*- coding: utf-8 -*-
"""Phase-1 sanity tests for the Quant Research Lab scaffold.

These tests must pass with the ``QUANT_RESEARCH_ENABLED`` flag in either
state and without any network / database access. They guard the
contract that endpoints return structured ``not_enabled`` payloads
instead of 5xx when the feature flag is off.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.quant_research.errors import (
    QuantResearchDisabledError,
    QuantResearchError,
    QuantResearchValidationError,
)
from src.quant_research.metrics import (
    SUPPORTED_BACKTEST_METRICS,
    SUPPORTED_FACTOR_METRICS,
)
from src.quant_research.repositories import QuantResearchRepository
from src.quant_research.schemas import (
    QuantResearchCapabilities,
    QuantResearchCapability,
    QuantResearchStatus,
)
from src.quant_research.service import QuantResearchService


def _fake_config(enabled: bool):
    """Lightweight Config double — only needs ``quant_research_enabled``.
    Avoids constructing a full ``Config`` (which requires many fields)."""
    return SimpleNamespace(quant_research_enabled=enabled)


class QuantResearchServiceDisabledFlagTests(unittest.TestCase):
    """When the master flag is off, every public method must return a
    structured ``not_enabled`` payload — never raise, never 5xx."""

    def test_status_returns_not_enabled_when_flag_off(self) -> None:
        service = QuantResearchService(config=_fake_config(False))
        result = service.status()

        self.assertIsInstance(result, QuantResearchStatus)
        self.assertFalse(result.enabled)
        self.assertEqual(result.status, "not_enabled")
        self.assertIn("QUANT_RESEARCH_ENABLED", result.message)
        # Every status response must carry a phase string for the WebUI.
        self.assertTrue(result.phase)

    def test_status_reports_ready_when_flag_on(self) -> None:
        service = QuantResearchService(config=_fake_config(True))
        result = service.status()

        self.assertTrue(result.enabled)
        self.assertEqual(result.status, "ready")
        # Message advertises which phase is live; current is Phase 2.
        self.assertTrue(
            "Phase" in result.message or "phase" in result.message.lower(),
            msg=f"status message should mention current phase: {result.message!r}",
        )

    def test_capabilities_lists_planned_features_regardless_of_flag(self) -> None:
        # The capability inventory is the same shape on/off; only the
        # top-level ``enabled`` flag differs. This lets the SPA render
        # placeholder cards before the flag is flipped.
        for flag in (False, True):
            with self.subTest(flag=flag):
                service = QuantResearchService(config=_fake_config(flag))
                caps = service.capabilities()
                self.assertIsInstance(caps, QuantResearchCapabilities)
                self.assertEqual(caps.enabled, flag)
                self.assertGreaterEqual(len(caps.capabilities), 5)

    def test_capabilities_only_implemented_phases_are_available(self) -> None:
        # Each phase flips its capability's ``available`` flag to True
        # only when the implementation lands. This test pins the
        # expected per-phase availability so future phases can't
        # accidentally claim availability before they're done.
        service = QuantResearchService(config=_fake_config(True))
        caps = service.capabilities()
        # Phases that are live in this build. Bump this set each time
        # a phase ships and its capability flips ``available=True``.
        live_phases = {"phase-2", "phase-3", "phase-4", "phase-5", "phase-6"}
        for cap in caps.capabilities:
            with self.subTest(capability=cap.name, phase=cap.phase):
                expected = cap.phase in live_phases
                self.assertEqual(
                    cap.available,
                    expected,
                    msg=(
                        f"Capability {cap.name} (phase={cap.phase}) availability "
                        f"= {cap.available}; expected {expected} based on "
                        f"live_phases={live_phases}."
                    ),
                )

    def test_capability_names_are_unique(self) -> None:
        service = QuantResearchService(config=_fake_config(True))
        caps = service.capabilities()
        names = [c.name for c in caps.capabilities]
        self.assertEqual(len(names), len(set(names)), msg=f"duplicate names: {names}")

    def test_each_capability_has_endpoint_list(self) -> None:
        # SPA relies on this to render the "Coming in phase X" UI without
        # extra round-trips.
        service = QuantResearchService(config=_fake_config(True))
        caps = service.capabilities()
        for cap in caps.capabilities:
            self.assertIsInstance(cap.endpoints, list)
            self.assertGreater(len(cap.endpoints), 0, msg=f"{cap.name} has no endpoints declared")


class QuantResearchSchemasContractTests(unittest.TestCase):
    """Pin the schema shapes so future phases don't accidentally rename
    fields the SPA already binds to."""

    def test_status_schema_round_trip(self) -> None:
        payload = QuantResearchStatus(
            enabled=False,
            status="not_enabled",
            message="hi",
            phase="phase-1-scaffold",
        )
        data = payload.model_dump()
        # Required keys for the SPA contract
        for key in ("enabled", "status", "message", "phase"):
            self.assertIn(key, data)

    def test_capability_schema_round_trip(self) -> None:
        cap = QuantResearchCapability(
            name="x",
            title="X",
            available=False,
            phase="phase-2",
            description="…",
            endpoints=["GET /x"],
            requires_optional_deps=[],
        )
        data = cap.model_dump()
        self.assertIn("requires_optional_deps", data)
        self.assertEqual(data["available"], False)


class QuantResearchRepositoryTests(unittest.TestCase):
    def test_no_persistence_when_no_db_injected(self) -> None:
        repo = QuantResearchRepository()
        self.assertFalse(repo.is_persistence_available())

    def test_repository_reports_persistence_when_db_injected(self) -> None:
        repo = QuantResearchRepository(db_manager=object())
        self.assertTrue(repo.is_persistence_available())

    def test_phase2_methods_raise_not_implemented(self) -> None:
        # Reminder for whoever lights up Phase 2 — they need to replace
        # these stubs, not silently no-op.
        repo = QuantResearchRepository()
        with self.assertRaises(NotImplementedError):
            repo.save_run_meta()
        with self.assertRaises(NotImplementedError):
            repo.get_run("any")


class QuantResearchErrorHierarchyTests(unittest.TestCase):
    """Endpoints translate these to structured JSON; the inheritance
    structure must stay stable."""

    def test_disabled_error_is_subclass(self) -> None:
        self.assertTrue(issubclass(QuantResearchDisabledError, QuantResearchError))

    def test_validation_error_carries_field(self) -> None:
        err = QuantResearchValidationError("bad cron", field="schedule_cron")
        self.assertEqual(err.field, "schedule_cron")
        self.assertEqual(str(err), "bad cron")


class QuantResearchMetricsRegistryTests(unittest.TestCase):
    """Registry is consumed by ``/capabilities`` (later phases). Keep it
    stable to avoid SPA churn."""

    def test_factor_metrics_includes_core_set(self) -> None:
        for required in ("ic", "rank_ic", "icir", "quantile_returns"):
            self.assertIn(required, SUPPORTED_FACTOR_METRICS)

    def test_backtest_metrics_includes_core_set(self) -> None:
        for required in ("sharpe", "max_drawdown", "annualized_return"):
            self.assertIn(required, SUPPORTED_BACKTEST_METRICS)


class QuantResearchEndpointImportTests(unittest.TestCase):
    """The endpoint module must import cleanly so the FastAPI app can
    boot regardless of feature-flag state."""

    def test_endpoint_module_imports(self) -> None:
        from api.v1.endpoints import quant_research as endpoint_module

        self.assertTrue(hasattr(endpoint_module, "router"))
        self.assertTrue(hasattr(endpoint_module, "quant_status"))
        self.assertTrue(hasattr(endpoint_module, "quant_capabilities"))
        self.assertTrue(hasattr(endpoint_module, "quant_healthcheck"))

    def test_router_is_registered_in_v1_router(self) -> None:
        # The aggregated v1 router must mount our prefix; if a future
        # refactor drops it the SPA will silently 404 — this catches it.
        from api.v1.router import router as v1_router

        prefixes = []
        for route in v1_router.routes:
            path = getattr(route, "path", "")
            if isinstance(path, str):
                prefixes.append(path)
        joined = " ".join(prefixes)
        self.assertIn("/api/v1/quant/status", joined)
        self.assertIn("/api/v1/quant/capabilities", joined)


class QuantResearchEndpointResponsesTests(unittest.TestCase):
    """End-to-end via FastAPI — verify the disabled-flag response shape."""

    def test_status_endpoint_returns_not_enabled_payload(self) -> None:
        from fastapi.testclient import TestClient
        from api.v1.endpoints import quant_research as endpoint_module
        from fastapi import FastAPI

        # Minimal app — no auth middleware, no SPA — just the router.
        app = FastAPI()
        app.include_router(endpoint_module.router, prefix="/api/v1/quant")

        # Force the service to see flag=off regardless of host env.
        with patch.object(
            endpoint_module,
            "_service",
            return_value=QuantResearchService(config=_fake_config(False)),
        ):
            client = TestClient(app)
            resp = client.get("/api/v1/quant/status")

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertFalse(body["enabled"])
        self.assertEqual(body["status"], "not_enabled")

    def test_capabilities_endpoint_returns_full_inventory(self) -> None:
        from fastapi.testclient import TestClient
        from api.v1.endpoints import quant_research as endpoint_module
        from fastapi import FastAPI

        app = FastAPI()
        app.include_router(endpoint_module.router, prefix="/api/v1/quant")

        with patch.object(
            endpoint_module,
            "_service",
            return_value=QuantResearchService(config=_fake_config(False)),
        ):
            client = TestClient(app)
            resp = client.get("/api/v1/quant/capabilities")

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertFalse(body["enabled"])
        self.assertGreaterEqual(len(body["capabilities"]), 5)
        self.assertTrue(all("name" in c for c in body["capabilities"]))

    def test_healthcheck_endpoint(self) -> None:
        from fastapi.testclient import TestClient
        from api.v1.endpoints import quant_research as endpoint_module
        from fastapi import FastAPI

        app = FastAPI()
        app.include_router(endpoint_module.router, prefix="/api/v1/quant")

        client = TestClient(app)
        resp = client.get("/api/v1/quant/healthcheck")

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])


if __name__ == "__main__":
    unittest.main()
