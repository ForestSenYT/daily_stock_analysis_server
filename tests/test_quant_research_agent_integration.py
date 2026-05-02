# -*- coding: utf-8 -*-
"""Phase-6 tests — Quant Research integration into the existing Agent.

These tests pin the contract:

1. The five quant tools are visible from the same ToolRegistry that
   already serves data / analysis / search / market / backtest tools —
   we did NOT introduce a parallel registry.
2. With the master flag off, every quant tool returns a structured
   ``not_enabled`` payload; with the flag on, a validation error
   surfaces a stable ``error`` envelope instead of bubbling 500s.
3. The new ``quant_research`` skill loads, is **not** part of the
   default-active set, but **is** user-invocable; existing skills
   (``bull_trend`` etc.) still drive default behaviour.
4. The ``/api/v1/agent/skills`` endpoint still exposes the lab as an
   opt-in skill with the right Chinese display name.

All tests are deterministic, in-memory, network-free.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tests.litellm_stub import ensure_litellm_stub

ensure_litellm_stub()


def _fake_config(enabled: bool = True):
    """Lightweight Config double for QuantResearchService."""
    return SimpleNamespace(
        quant_research_enabled=enabled,
        agent_skill_dir=None,
    )


# =====================================================================
# 1) ToolRegistry exposure
# =====================================================================

class QuantResearchToolRegistryTests(unittest.TestCase):
    """The five quant tools must live in the same registry that already
    serves the existing 18 tools — not a parallel one."""

    QUANT_TOOL_NAMES = {
        "list_quant_factors",
        "evaluate_quant_factor",
        "run_quant_factor_backtest",
        "get_quant_research_run",
        "get_quant_portfolio_risk",
    }

    EXISTING_TOOL_NAMES = {
        # Data
        "get_realtime_quote", "get_daily_history", "get_chip_distribution",
        "get_analysis_context", "get_stock_info", "get_portfolio_snapshot",
        "get_capital_flow",
        # Analysis
        "analyze_trend", "calculate_ma", "get_volume_analysis", "analyze_pattern",
        # Search
        "search_stock_news", "search_comprehensive_intel",
        # Market
        "get_market_indices", "get_sector_rankings",
        # Backtest
        "get_skill_backtest_summary", "get_strategy_backtest_summary",
        "get_stock_backtest_summary",
    }

    def setUp(self) -> None:
        # Force a fresh registry build per-test so we don't rely on
        # global cache state set by previous suites.
        import src.agent.factory as factory
        factory._TOOL_REGISTRY = None
        self._registry = factory.get_tool_registry()

    def test_quant_tools_are_registered(self) -> None:
        names = set(self._registry.list_names())
        missing = self.QUANT_TOOL_NAMES - names
        self.assertFalse(
            missing,
            msg=f"Quant tools missing from registry: {missing}",
        )

    def test_existing_tools_still_present(self) -> None:
        names = set(self._registry.list_names())
        missing = self.EXISTING_TOOL_NAMES - names
        self.assertFalse(
            missing,
            msg=(
                "Existing tools dropped after Phase-6 wiring: "
                f"{missing}. Order of pools must stay backward-compatible."
            ),
        )

    def test_quant_tools_appear_after_existing_tools(self) -> None:
        # Prepending the quant pool would bump every existing tool's
        # OpenAI-tool index — provider-sensitive. Pin the order.
        names = self._registry.list_names()
        last_existing_index = max(
            (i for i, n in enumerate(names) if n in self.EXISTING_TOOL_NAMES),
            default=-1,
        )
        first_quant_index = min(
            (i for i, n in enumerate(names) if n in self.QUANT_TOOL_NAMES),
            default=10**9,
        )
        self.assertGreater(first_quant_index, last_existing_index)

    def test_quant_tool_categories_are_data_or_analysis(self) -> None:
        # Categories drive UI grouping; keep them aligned with the
        # existing data / analysis split.
        for name in self.QUANT_TOOL_NAMES:
            tool = self._registry.get(name)
            self.assertIsNotNone(tool)
            self.assertIn(tool.category, {"data", "analysis"})


# =====================================================================
# 2) Feature-flag gating + safety bounds
# =====================================================================

class QuantResearchToolFlagTests(unittest.TestCase):
    """Each handler must short-circuit to ``not_enabled`` when the flag
    is off, and surface validation errors as a clean envelope when on."""

    def _patch_service(self, enabled: bool):
        """Return a context manager that patches the lazy service factory."""
        from src.quant_research.service import QuantResearchService
        return patch(
            "src.agent.tools.quant_research_tools._service",
            return_value=QuantResearchService(config=_fake_config(enabled)),
        )

    def test_list_factors_returns_not_enabled_when_off(self) -> None:
        from src.agent.tools.quant_research_tools import _handle_list_quant_factors
        with self._patch_service(False):
            result = _handle_list_quant_factors()
        self.assertFalse(result["enabled"])
        self.assertEqual(result["status"], "not_enabled")

    def test_evaluate_factor_returns_not_enabled_when_off(self) -> None:
        from src.agent.tools.quant_research_tools import _handle_evaluate_quant_factor
        with self._patch_service(False):
            result = _handle_evaluate_quant_factor(
                stocks=["AAPL"], start_date="2026-01-01",
                end_date="2026-01-31", builtin_id="return_5d",
            )
        self.assertFalse(result["enabled"])

    def test_run_backtest_returns_not_enabled_when_off(self) -> None:
        from src.agent.tools.quant_research_tools import _handle_run_quant_factor_backtest
        with self._patch_service(False):
            result = _handle_run_quant_factor_backtest(
                strategy="equal_weight_baseline",
                stocks=["AAPL", "MSFT"],
                start_date="2026-01-01", end_date="2026-01-31",
            )
        self.assertFalse(result["enabled"])

    def test_get_run_returns_not_enabled_when_off(self) -> None:
        from src.agent.tools.quant_research_tools import _handle_get_quant_research_run
        with self._patch_service(False):
            result = _handle_get_quant_research_run(run_id="x")
        self.assertFalse(result["enabled"])

    def test_portfolio_risk_returns_not_enabled_when_off(self) -> None:
        from src.agent.tools.quant_research_tools import _handle_get_quant_portfolio_risk
        with self._patch_service(False):
            result = _handle_get_quant_portfolio_risk(
                weights={"AAPL": 1.0},
                start_date="2026-01-01", end_date="2026-01-31",
            )
        self.assertFalse(result["enabled"])

    # --- safety bounds -------------------------------------------------

    def test_evaluate_factor_rejects_both_builtin_and_expression(self) -> None:
        from src.agent.tools.quant_research_tools import _handle_evaluate_quant_factor
        with self._patch_service(True):
            result = _handle_evaluate_quant_factor(
                stocks=["AAPL"], start_date="2026-01-01",
                end_date="2026-01-31",
                builtin_id="return_5d",
                expression="close",
            )
        self.assertEqual(result["error"], "quant_research_validation")
        self.assertEqual(result["field"], "factor")

    def test_evaluate_factor_rejects_neither_builtin_nor_expression(self) -> None:
        from src.agent.tools.quant_research_tools import _handle_evaluate_quant_factor
        with self._patch_service(True):
            result = _handle_evaluate_quant_factor(
                stocks=["AAPL"], start_date="2026-01-01",
                end_date="2026-01-31",
            )
        self.assertEqual(result["error"], "quant_research_validation")

    def test_evaluate_factor_caps_stock_pool(self) -> None:
        from src.agent.tools.quant_research_tools import (
            MAX_STOCKS,
            _handle_evaluate_quant_factor,
        )
        with self._patch_service(True):
            result = _handle_evaluate_quant_factor(
                stocks=[f"S{i:03d}" for i in range(MAX_STOCKS + 5)],
                start_date="2026-01-01", end_date="2026-01-31",
                builtin_id="return_5d",
            )
        self.assertEqual(result["error"], "quant_research_validation")
        self.assertEqual(result["field"], "stocks")

    def test_run_backtest_caps_stock_pool(self) -> None:
        from src.agent.tools.quant_research_tools import (
            MAX_BACKTEST_STOCKS,
            _handle_run_quant_factor_backtest,
        )
        with self._patch_service(True):
            result = _handle_run_quant_factor_backtest(
                strategy="equal_weight_baseline",
                stocks=[f"S{i:03d}" for i in range(MAX_BACKTEST_STOCKS + 5)],
                start_date="2026-01-01", end_date="2026-01-31",
            )
        self.assertEqual(result["error"], "quant_research_validation")
        self.assertEqual(result["field"], "stocks")

    def test_portfolio_risk_caps_symbol_count(self) -> None:
        from src.agent.tools.quant_research_tools import (
            MAX_RISK_SYMBOLS,
            _handle_get_quant_portfolio_risk,
        )
        weights = {f"S{i:03d}": 1.0 for i in range(MAX_RISK_SYMBOLS + 5)}
        with self._patch_service(True):
            result = _handle_get_quant_portfolio_risk(
                weights=weights,
                start_date="2026-01-01", end_date="2026-01-31",
            )
        self.assertEqual(result["error"], "quant_research_validation")
        self.assertEqual(result["field"], "weights")

    def test_get_run_requires_run_id(self) -> None:
        from src.agent.tools.quant_research_tools import _handle_get_quant_research_run
        with self._patch_service(True):
            result = _handle_get_quant_research_run(run_id="")
        self.assertEqual(result["error"], "quant_research_validation")
        self.assertEqual(result["field"], "run_id")

    def test_get_run_returns_not_found_for_unknown(self) -> None:
        from src.agent.tools.quant_research_tools import _handle_get_quant_research_run
        with self._patch_service(True):
            result = _handle_get_quant_research_run(run_id="never-existed")
        self.assertEqual(result["error"], "not_found")


# =====================================================================
# 3) Agent system stays untouched: tools reach the LLM via the same
#    OpenAI-format declarations as everything else.
# =====================================================================

class QuantResearchToOpenAIToolsTests(unittest.TestCase):
    def test_quant_tools_appear_in_openai_schema(self) -> None:
        import src.agent.factory as factory
        factory._TOOL_REGISTRY = None
        registry = factory.get_tool_registry()
        names = {t["function"]["name"] for t in registry.to_openai_tools()}
        for required in (
            "list_quant_factors",
            "evaluate_quant_factor",
            "run_quant_factor_backtest",
            "get_quant_research_run",
            "get_quant_portfolio_risk",
        ):
            self.assertIn(required, names)


# =====================================================================
# 4) Skill loads, is opt-in only, default skill set unchanged
# =====================================================================

class QuantResearchSkillManagerTests(unittest.TestCase):
    """The new skill must load, must NOT default-activate, and must
    appear in user-invocable selectors."""

    def setUp(self) -> None:
        import src.agent.factory as factory
        # Force a clean SkillManager prototype rebuild — the global
        # cache might be stale from earlier suites.
        factory._SKILL_MANAGER_PROTOTYPE = None
        factory._SKILL_MANAGER_CUSTOM_DIR = factory._SENTINEL
        self._manager = factory.get_skill_manager(_fake_config(True))

    def test_quant_research_skill_loads(self) -> None:
        skill = self._manager.get("quant_research")
        self.assertIsNotNone(skill, msg="quant_research skill failed to load")
        self.assertEqual(skill.display_name, "量化研究助手")
        self.assertGreater(len(skill.instructions), 200)

    def test_skill_is_user_invocable_but_not_default_active(self) -> None:
        skill = self._manager.get("quant_research")
        self.assertIsNotNone(skill)
        self.assertTrue(skill.user_invocable)
        self.assertFalse(skill.default_active)
        self.assertFalse(skill.default_router)

    def test_skill_required_tools_match_registered_quant_tools(self) -> None:
        skill = self._manager.get("quant_research")
        self.assertIsNotNone(skill)
        expected = {
            "list_quant_factors",
            "evaluate_quant_factor",
            "run_quant_factor_backtest",
            "get_quant_research_run",
            "get_quant_portfolio_risk",
        }
        self.assertEqual(set(skill.required_tools), expected)

    def test_default_active_set_does_not_include_quant_research(self) -> None:
        from src.agent.skills.defaults import get_default_active_skill_ids
        defaults = set(get_default_active_skill_ids(self._manager.list_skills()))
        self.assertNotIn(
            "quant_research", defaults,
            msg=(
                "quant_research must NOT auto-activate; default skill "
                "set must remain stable for existing /chat behaviour."
            ),
        )

    def test_bull_trend_remains_primary_default(self) -> None:
        # Pinning this prevents a future skill drop-in from accidentally
        # winning the primary default slot.
        from src.agent.skills.defaults import get_primary_default_skill_id
        primary = get_primary_default_skill_id(self._manager.list_skills())
        self.assertEqual(primary, "bull_trend")


# =====================================================================
# 5) /api/v1/agent/skills endpoint shape — quant_research surfaces with
#    its Chinese display name and stays optional.
# =====================================================================

class QuantResearchAgentSkillsEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        import src.agent.factory as factory
        factory._SKILL_MANAGER_PROTOTYPE = None
        factory._SKILL_MANAGER_CUSTOM_DIR = factory._SENTINEL

    def test_skills_endpoint_includes_quant_research(self) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from api.v1.endpoints import agent as agent_module

        app = FastAPI()
        app.include_router(agent_module.router, prefix="/api/v1/agent")

        client = TestClient(app)
        resp = client.get("/api/v1/agent/skills")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        ids = {s["id"]: s for s in body["skills"]}
        self.assertIn("quant_research", ids)
        self.assertEqual(ids["quant_research"]["name"], "量化研究助手")
        # Default skill id must NOT be quant_research.
        self.assertNotEqual(body["default_skill_id"], "quant_research")


# =====================================================================
# 6) TOOL_DISPLAY_NAMES coverage
# =====================================================================

class QuantResearchToolDisplayNamesTests(unittest.TestCase):
    def test_chinese_display_names_cover_every_quant_tool(self) -> None:
        from api.v1.endpoints.agent import TOOL_DISPLAY_NAMES
        for name in (
            "list_quant_factors",
            "evaluate_quant_factor",
            "run_quant_factor_backtest",
            "get_quant_research_run",
            "get_quant_portfolio_risk",
        ):
            self.assertIn(
                name, TOOL_DISPLAY_NAMES,
                msg=f"TOOL_DISPLAY_NAMES missing entry for {name}",
            )
            self.assertTrue(TOOL_DISPLAY_NAMES[name].strip())


if __name__ == "__main__":
    unittest.main()
