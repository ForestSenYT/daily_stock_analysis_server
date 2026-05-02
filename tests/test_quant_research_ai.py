# -*- coding: utf-8 -*-
"""Phase-5 tests — AI FactorSpec generation.

The LLM is the only "untrusted" producer in the lab; these tests pin
the validator contract that protects the rest of the pipeline. The
adapter is mocked end-to-end (no network, no API keys) — what we
verify is the *output discipline*: well-formed JSON passes, anything
else is rejected with a stable ``error.code`` that the API can
translate to a structured 400.

Coverage matrix:
1. Validator layer
   - ``parse_json_strict`` rejects Markdown wrappers, multiple objects,
     non-object roots.
   - ``validate_factor_spec_shape`` rejects missing keys, wrong types,
     bad direction / market enums, non-snake_case names, out-of-range
     windows.
   - ``validate_factor_spec_safety`` rejects expressions that miss the
     AST whitelist, dangerous marketing phrases, and inputs / expression
     drift.
   - The composite ``parse_and_validate`` round-trips a clean spec.
2. ``FactorGenerator`` (with mocked adapter)
   - Happy path: legal JSON → outcome with spec + diagnostics.
   - Markdown fence / non-JSON → ``markdown_wrapper`` /
     ``not_a_json_object`` codes (no exec).
   - Dangerous expression (``__import__``) → ``unsafe_expression``.
   - Dangerous phrase ("guaranteed return") → ``dangerous_phrase``.
   - LLM unavailable (no router) → ``llm_unavailable``.
3. Service + endpoints
   - ``QuantResearchService.generate_factor`` honours the disabled
     flag and surfaces validation errors as
     ``QuantResearchValidationError``.
   - ``POST /api/v1/quant/factors/generate`` returns 503 when the lab
     is disabled and 400 with a code when validation fails.
"""

from __future__ import annotations

import json
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# ``src.agent.llm_adapter`` imports ``from litellm import Router`` at
# module load, but other quant-research tests stub ``litellm`` with a
# bare ``ModuleType`` to keep dev shells light. When pytest runs that
# test before this one the bare stub wins and the import explodes.
# Pre-populate the attribute the adapter needs so order-of-collection
# becomes irrelevant; the real litellm (when installed) wins via
# ``setdefault`` so production behaviour is unaffected.
_litellm_stub = sys.modules.get("litellm")
if _litellm_stub is not None and not hasattr(_litellm_stub, "Router"):
    _litellm_stub.Router = type("Router", (), {})  # type: ignore[attr-defined]
sys.modules.setdefault("json_repair", types.ModuleType("json_repair"))
if not hasattr(sys.modules["json_repair"], "repair_json"):
    sys.modules["json_repair"].repair_json = lambda x: x  # type: ignore[attr-defined]

from src.quant_research.ai.factor_generator import FactorGenerator  # noqa: E402
from src.quant_research.ai.validators import (  # noqa: E402
    FactorGenerationError,
    parse_and_validate,
    parse_json_strict,
    validate_factor_spec_safety,
    validate_factor_spec_shape,
)


def _llm_response(content: str, *, provider: str = "mock-provider",
                  model: str = "mock-model/test", usage=None):
    """Construct a stand-in LLMResponse without importing it.

    The factor_generator only reads ``content``, ``provider``, ``model``,
    and ``usage`` from the response — using ``SimpleNamespace`` avoids
    pulling ``src.agent.llm_adapter`` (and its litellm import) into the
    test's import graph.
    """
    return SimpleNamespace(
        content=content,
        provider=provider,
        model=model,
        usage=usage or {},
    )


def _fake_config(enabled: bool = True):
    return SimpleNamespace(quant_research_enabled=enabled)


def _good_spec() -> dict:
    """Canonical valid FactorSpec. Each test mutates a copy."""
    return {
        "name": "ma_close_ratio_20",
        "hypothesis": (
            "Stocks trading above their 20-day moving average tend "
            "to revert; track close / mean(close, 20) - 1 as a "
            "stretch indicator."
        ),
        "inputs": ["close"],
        "expression": "div(close, mean(close, 20)) - 1",
        "window": 20,
        "expected_direction": "negative",
        "market_scope": "us",
        "risk_notes": [
            "Mean reversion can break in trending regimes.",
            "Sensitive to corporate actions; needs clean adjusted close.",
        ],
        "validation_plan": [
            "Compute 5-day forward IC and quintile spreads.",
            "Cross-check on cn / hk subsets to confirm robustness.",
        ],
    }


def _good_spec_json() -> str:
    return json.dumps(_good_spec())


# =====================================================================
# 1) Layer-by-layer validator tests
# =====================================================================

class ParseJsonStrictTests(unittest.TestCase):
    def test_accepts_clean_object(self) -> None:
        out = parse_json_strict(_good_spec_json())
        self.assertEqual(out["name"], "ma_close_ratio_20")

    def test_rejects_markdown_fence(self) -> None:
        raw = "```json\n" + _good_spec_json() + "\n```"
        with self.assertRaises(FactorGenerationError) as ctx:
            parse_json_strict(raw)
        self.assertEqual(ctx.exception.code, "markdown_wrapper")

    def test_rejects_leading_prose(self) -> None:
        raw = "Sure! Here is your factor:\n" + _good_spec_json()
        with self.assertRaises(FactorGenerationError) as ctx:
            parse_json_strict(raw)
        self.assertEqual(ctx.exception.code, "not_a_json_object")

    def test_rejects_array_root(self) -> None:
        with self.assertRaises(FactorGenerationError) as ctx:
            parse_json_strict("[1, 2, 3]")
        # Array starts with '[' so the structural prefix-check fires first.
        self.assertEqual(ctx.exception.code, "not_a_json_object")

    def test_rejects_invalid_json(self) -> None:
        with self.assertRaises(FactorGenerationError) as ctx:
            parse_json_strict("{not really json}")
        self.assertEqual(ctx.exception.code, "invalid_json")

    def test_rejects_empty_response(self) -> None:
        with self.assertRaises(FactorGenerationError) as ctx:
            parse_json_strict("   ")
        self.assertEqual(ctx.exception.code, "empty_response")


class ValidateShapeTests(unittest.TestCase):
    def test_accepts_clean_spec(self) -> None:
        out = validate_factor_spec_shape(_good_spec())
        # name should be normalized but unchanged here.
        self.assertEqual(out["name"], "ma_close_ratio_20")
        self.assertEqual(out["expected_direction"], "negative")
        self.assertEqual(out["market_scope"], "us")

    def test_rejects_missing_required_key(self) -> None:
        spec = _good_spec()
        del spec["window"]
        with self.assertRaises(FactorGenerationError) as ctx:
            validate_factor_spec_shape(spec)
        self.assertEqual(ctx.exception.code, "missing_keys")

    def test_rejects_non_snake_case_name(self) -> None:
        spec = _good_spec()
        spec["name"] = "BadName"
        with self.assertRaises(FactorGenerationError) as ctx:
            validate_factor_spec_shape(spec)
        self.assertEqual(ctx.exception.code, "invalid_name")

    def test_rejects_unknown_input_column(self) -> None:
        spec = _good_spec()
        spec["inputs"] = ["close", "rumour_score"]
        with self.assertRaises(FactorGenerationError) as ctx:
            validate_factor_spec_shape(spec)
        self.assertEqual(ctx.exception.code, "value_not_allowed")

    def test_rejects_window_too_large(self) -> None:
        spec = _good_spec()
        spec["window"] = 5000
        with self.assertRaises(FactorGenerationError) as ctx:
            validate_factor_spec_shape(spec)
        self.assertEqual(ctx.exception.code, "field_out_of_range")

    def test_rejects_invalid_direction(self) -> None:
        spec = _good_spec()
        spec["expected_direction"] = "moonshot"
        with self.assertRaises(FactorGenerationError) as ctx:
            validate_factor_spec_shape(spec)
        self.assertEqual(ctx.exception.code, "value_not_allowed")

    def test_cannot_generate_path(self) -> None:
        spec = {"error": "cannot_generate", "reason": "ambiguous"}
        with self.assertRaises(FactorGenerationError) as ctx:
            validate_factor_spec_shape(spec)
        self.assertEqual(ctx.exception.code, "cannot_generate")


class ValidateSafetyTests(unittest.TestCase):
    def test_accepts_clean_spec(self) -> None:
        shaped = validate_factor_spec_shape(_good_spec())
        validation = validate_factor_spec_safety(shaped)
        self.assertEqual(validation.flagged_phrases, [])
        self.assertGreater(validation.expression_node_count, 0)

    def test_rejects_dunder_attribute(self) -> None:
        shaped = validate_factor_spec_shape(_good_spec())
        shaped["expression"] = "close.__class__"
        with self.assertRaises(FactorGenerationError) as ctx:
            validate_factor_spec_safety(shaped)
        self.assertEqual(ctx.exception.code, "unsafe_expression")

    def test_rejects_import_call(self) -> None:
        shaped = validate_factor_spec_shape(_good_spec())
        shaped["expression"] = "__import__('os')"
        with self.assertRaises(FactorGenerationError) as ctx:
            validate_factor_spec_safety(shaped)
        self.assertEqual(ctx.exception.code, "unsafe_expression")

    def test_rejects_dangerous_phrase(self) -> None:
        shaped = validate_factor_spec_shape(_good_spec())
        shaped["risk_notes"] = ["This factor produces guaranteed return."]
        with self.assertRaises(FactorGenerationError) as ctx:
            validate_factor_spec_safety(shaped)
        self.assertEqual(ctx.exception.code, "dangerous_phrase")

    def test_rejects_chinese_dangerous_phrase(self) -> None:
        shaped = validate_factor_spec_shape(_good_spec())
        shaped["validation_plan"] = ["这个策略保证盈利"]
        with self.assertRaises(FactorGenerationError) as ctx:
            validate_factor_spec_safety(shaped)
        self.assertEqual(ctx.exception.code, "dangerous_phrase")

    def test_rejects_inputs_expression_drift(self) -> None:
        shaped = validate_factor_spec_shape(_good_spec())
        # Declared inputs say "close" but expression uses only volume.
        shaped["inputs"] = ["volume"]
        shaped["expression"] = "div(close, mean(close, 20)) - 1"
        with self.assertRaises(FactorGenerationError) as ctx:
            validate_factor_spec_safety(shaped)
        self.assertEqual(ctx.exception.code, "inputs_expression_mismatch")


class ParseAndValidateTests(unittest.TestCase):
    def test_round_trip_clean_spec(self) -> None:
        out = parse_and_validate(_good_spec_json())
        self.assertEqual(out.spec["name"], "ma_close_ratio_20")
        self.assertEqual(out.flagged_phrases, [])

    def test_rejects_markdown_wrapper_at_composite_layer(self) -> None:
        with self.assertRaises(FactorGenerationError) as ctx:
            parse_and_validate("```\n" + _good_spec_json() + "\n```")
        self.assertEqual(ctx.exception.code, "markdown_wrapper")


# =====================================================================
# 2) FactorGenerator with mocked adapter
# =====================================================================

def _make_adapter(content: str, *, available: bool = True) -> MagicMock:
    """Build a mocked LLMToolAdapter that returns the given content."""
    adapter = MagicMock()
    adapter.is_available = available
    adapter.call_text.return_value = _llm_response(
        content,
        usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
    )
    return adapter


class FactorGeneratorBehaviourTests(unittest.TestCase):
    def test_happy_path_returns_outcome(self) -> None:
        adapter = _make_adapter(_good_spec_json())
        gen = FactorGenerator(adapter=adapter)
        outcome = gen.generate("rebound after a 3-day dip")

        self.assertEqual(outcome.spec["name"], "ma_close_ratio_20")
        self.assertEqual(outcome.model, "mock-model/test")
        self.assertGreater(outcome.expression_node_count, 0)
        self.assertEqual(outcome.usage.get("total_tokens"), 150)
        adapter.call_text.assert_called_once()

    def test_include_raw_echoes_response(self) -> None:
        adapter = _make_adapter(_good_spec_json())
        out = FactorGenerator(adapter=adapter).generate(
            "ma reversion", include_raw=True,
        )
        self.assertIsNotNone(out.raw_response)
        self.assertIn("ma_close_ratio_20", out.raw_response or "")

    def test_markdown_fence_rejected(self) -> None:
        adapter = _make_adapter("```json\n" + _good_spec_json() + "\n```")
        gen = FactorGenerator(adapter=adapter)
        with self.assertRaises(FactorGenerationError) as ctx:
            gen.generate("hypothesis")
        self.assertEqual(ctx.exception.code, "markdown_wrapper")

    def test_non_json_rejected(self) -> None:
        adapter = _make_adapter("Sorry, I cannot help with that.")
        with self.assertRaises(FactorGenerationError) as ctx:
            FactorGenerator(adapter=adapter).generate("hypothesis")
        self.assertEqual(ctx.exception.code, "not_a_json_object")

    def test_dangerous_expression_rejected(self) -> None:
        spec = _good_spec()
        spec["expression"] = "__import__('os').system('rm -rf /')"
        adapter = _make_adapter(json.dumps(spec))
        with self.assertRaises(FactorGenerationError) as ctx:
            FactorGenerator(adapter=adapter).generate("hypothesis")
        self.assertEqual(ctx.exception.code, "unsafe_expression")
        # Ensure the adapter was called exactly once — we should never
        # retry with the same unsafe payload.
        self.assertEqual(adapter.call_text.call_count, 1)

    def test_dangerous_phrase_rejected(self) -> None:
        spec = _good_spec()
        spec["hypothesis"] = "This delivers guaranteed return for retail."
        adapter = _make_adapter(json.dumps(spec))
        with self.assertRaises(FactorGenerationError) as ctx:
            FactorGenerator(adapter=adapter).generate("hypothesis")
        self.assertEqual(ctx.exception.code, "dangerous_phrase")

    def test_llm_unavailable(self) -> None:
        adapter = _make_adapter(_good_spec_json(), available=False)
        with self.assertRaises(FactorGenerationError) as ctx:
            FactorGenerator(adapter=adapter).generate("hypothesis")
        self.assertEqual(ctx.exception.code, "llm_unavailable")

    def test_provider_error_response(self) -> None:
        # Adapter returns provider="error" when all fallbacks fail.
        adapter = MagicMock()
        adapter.is_available = True
        adapter.call_text.return_value = _llm_response(
            "boom", provider="error", model="",
        )
        with self.assertRaises(FactorGenerationError) as ctx:
            FactorGenerator(adapter=adapter).generate("hypothesis")
        self.assertEqual(ctx.exception.code, "llm_call_failed")

    def test_empty_content_rejected(self) -> None:
        adapter = MagicMock()
        adapter.is_available = True
        adapter.call_text.return_value = _llm_response(
            "", provider="mock", model="m",
        )
        with self.assertRaises(FactorGenerationError) as ctx:
            FactorGenerator(adapter=adapter).generate("hypothesis")
        self.assertEqual(ctx.exception.code, "empty_response")

    def test_adapter_exception_wrapped(self) -> None:
        adapter = MagicMock()
        adapter.is_available = True
        adapter.call_text.side_effect = RuntimeError("network down")
        with self.assertRaises(FactorGenerationError) as ctx:
            FactorGenerator(adapter=adapter).generate("hypothesis")
        self.assertEqual(ctx.exception.code, "llm_call_failed")

    def test_empty_hypothesis_rejected_locally(self) -> None:
        # Should fail before the adapter is called.
        adapter = _make_adapter(_good_spec_json())
        with self.assertRaises(FactorGenerationError) as ctx:
            FactorGenerator(adapter=adapter).generate("   ")
        self.assertEqual(ctx.exception.code, "empty_field")
        adapter.call_text.assert_not_called()


# =====================================================================
# 3) Service + endpoint integration (still no network)
# =====================================================================

class QuantResearchServicePhase5Tests(unittest.TestCase):
    def test_disabled_flag_blocks_generation(self) -> None:
        from src.quant_research.errors import QuantResearchDisabledError
        from src.quant_research.schemas import FactorGenerationRequest
        from src.quant_research.service import QuantResearchService

        service = QuantResearchService(config=_fake_config(False))
        request = FactorGenerationRequest(hypothesis="anything")
        with self.assertRaises(QuantResearchDisabledError):
            service.generate_factor(request)

    def test_generate_factor_happy_path(self) -> None:
        from src.quant_research.schemas import FactorGenerationRequest
        from src.quant_research.service import QuantResearchService

        adapter = _make_adapter(_good_spec_json())
        with patch(
            "src.quant_research.ai.factor_generator.FactorGenerator._get_adapter",
            return_value=adapter,
        ):
            service = QuantResearchService(config=_fake_config(True))
            request = FactorGenerationRequest(
                hypothesis="reversion above the 20-day MA",
            )
            response = service.generate_factor(request)

        self.assertTrue(response.enabled)
        self.assertEqual(response.spec.name, "ma_close_ratio_20")
        self.assertEqual(response.model, "mock-model/test")

    def test_generate_factor_unsafe_expression_surfaces_validation_error(self) -> None:
        from src.quant_research.errors import QuantResearchValidationError
        from src.quant_research.schemas import FactorGenerationRequest
        from src.quant_research.service import QuantResearchService

        bad_spec = _good_spec()
        bad_spec["expression"] = "__import__('os').system('whoami')"
        adapter = _make_adapter(json.dumps(bad_spec))
        with patch(
            "src.quant_research.ai.factor_generator.FactorGenerator._get_adapter",
            return_value=adapter,
        ):
            service = QuantResearchService(config=_fake_config(True))
            request = FactorGenerationRequest(hypothesis="anything")
            with self.assertRaises(QuantResearchValidationError) as ctx:
                service.generate_factor(request)
        # field carries the FactorGenerationError code so the API can
        # surface it to clients.
        self.assertEqual(ctx.exception.field, "unsafe_expression")


class Phase5EndpointResponseTests(unittest.TestCase):
    """Sanity-check the FastAPI wiring without network or DB."""

    def _build_app(self, enabled: bool):
        from fastapi import FastAPI
        from api.v1.endpoints import quant_research as endpoint_module
        from src.quant_research.service import QuantResearchService

        app = FastAPI()
        app.include_router(endpoint_module.router, prefix="/api/v1/quant")
        # Force the endpoint factory to a fake-config service.
        patcher = patch.object(
            endpoint_module,
            "_service",
            return_value=QuantResearchService(config=_fake_config(enabled)),
        )
        return app, patcher

    def test_disabled_returns_503(self) -> None:
        from fastapi.testclient import TestClient

        app, patcher = self._build_app(enabled=False)
        with patcher:
            client = TestClient(app)
            resp = client.post(
                "/api/v1/quant/factors/generate",
                json={"hypothesis": "test"},
            )
        self.assertEqual(resp.status_code, 503)
        body = resp.json()["detail"]
        self.assertEqual(body["error"], "quant_research_disabled")

    def test_unsafe_expression_returns_400(self) -> None:
        from fastapi.testclient import TestClient

        app, patcher = self._build_app(enabled=True)
        bad_spec = _good_spec()
        bad_spec["expression"] = "__import__('os')"
        adapter = _make_adapter(json.dumps(bad_spec))
        with patcher, patch(
            "src.quant_research.ai.factor_generator.FactorGenerator._get_adapter",
            return_value=adapter,
        ):
            client = TestClient(app)
            resp = client.post(
                "/api/v1/quant/factors/generate",
                json={"hypothesis": "anything"},
            )
        self.assertEqual(resp.status_code, 400)
        body = resp.json()["detail"]
        self.assertEqual(body["error"], "quant_research_validation")
        self.assertEqual(body["field"], "unsafe_expression")

    def test_capability_listed_as_available(self) -> None:
        # Phase-5 should be live in this build; Phase-6 still placeholder.
        from src.quant_research.service import QuantResearchService

        service = QuantResearchService(config=_fake_config(True))
        caps = service.capabilities()
        live = {c.name: c.available for c in caps.capabilities}
        self.assertTrue(live["ai_factor_generation"])
        self.assertFalse(live["agent_integration"])


if __name__ == "__main__":
    unittest.main()
