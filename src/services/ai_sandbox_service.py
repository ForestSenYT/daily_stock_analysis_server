# -*- coding: utf-8 -*-
"""AISandboxService — orchestrator for forward-simulation training.

Pipeline (mirrors TradingExecutionService but isolated end-to-end):

  intent → audit row 'pending' → RiskEngine → PaperExecutor (quote +
  fill) → audit row 'filled'/'failed'/'blocked'.

Key isolation invariants:
  * Writes ONLY to ``ai_sandbox_executions``. Never touches
    ``portfolio_trades`` or ``trade_executions``.
  * Always uses paper-mode fill price logic regardless of global
    ``TRADING_MODE``. Even if ``TRADING_MODE=live`` is set later,
    the sandbox path stays simulated.
  * Uses ``RiskEngine`` with sandbox-tuned config (lower max_value,
    larger allowlist) — config flag separate from the trading
    framework's risk config.

Reuse strategy (composition, not inheritance):
  * ``RiskEngine`` accepts any object with the right config attrs.
    Service builds a ``_SandboxRiskConfig`` proxy that points at
    sandbox-specific knobs.
  * ``PaperExecutor`` is instantiated with the sandbox config; its
    quote-fetch + fill-price methods are called directly. We don't
    use its full ``submit`` (we don't want it writing portfolio
    trades).
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.ai_sandbox.repo import (
    AISandboxRepository,
    DuplicateSandboxRequestError,
)
from src.ai_sandbox.types import (
    AISandboxIntent,
    AISandboxResult,
)
from src.config import get_config
from src.trading.executors.paper import PaperExecutor, _NotFillable
from src.trading.risk_engine import RiskEngine
from src.trading.types import (
    ExecutionStatus,
    OrderSide,
    OrderType,
    RiskAssessment,
    RiskFlag,
    RiskFlagCode,
    RiskSeverity,
)

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SandboxDisabledError(RuntimeError):
    """Raised when ``AI_SANDBOX_ENABLED=false`` and a method that
    requires the feature is called. Maps to HTTP 503 in the API."""


class _SandboxRiskConfigProxy:
    """Adapts the global Config to sandbox-specific risk thresholds.

    RiskEngine reads ``trading_*`` attrs; we substitute the
    ``ai_sandbox_*`` flavours so risk gates can be tuned independently
    of the live trading risk config.
    """

    def __init__(self, base_config: Any) -> None:
        self._base = base_config

    def __getattr__(self, name: str) -> Any:
        # Trading mode is fixed for the sandbox — we never run live
        # from the sandbox path.
        if name == "trading_mode":
            return "paper"
        if name == "trading_max_position_value":
            return float(getattr(self._base, "ai_sandbox_max_position_value", 5000.0))
        if name == "trading_max_position_pct":
            return float(getattr(self._base, "ai_sandbox_max_position_pct", 0.20))
        if name == "trading_max_daily_turnover":
            return float(getattr(self._base, "ai_sandbox_max_daily_turnover", 100000.0))
        if name == "trading_symbol_allowlist":
            return list(getattr(self._base, "ai_sandbox_symbol_allowlist", []) or [])
        if name == "trading_symbol_denylist":
            return list(getattr(self._base, "ai_sandbox_symbol_denylist", []) or [])
        if name == "trading_market_hours_strict":
            # Sandbox usually runs 24x7 — strict mode would block most ticks.
            return bool(getattr(self._base, "ai_sandbox_market_hours_strict", False))
        if name == "trading_paper_slippage_bps":
            return int(getattr(self._base, "ai_sandbox_paper_slippage_bps", 10))
        if name == "trading_paper_fee_per_trade":
            return float(getattr(self._base, "ai_sandbox_paper_fee_per_trade", 0.0))
        # Fallback to the underlying config
        return getattr(self._base, name)


class AISandboxService:
    """Process-level singleton via :func:`get_ai_sandbox_service`."""

    def __init__(
        self,
        *,
        config: Any = None,
        repo: Optional[AISandboxRepository] = None,
    ) -> None:
        base_cfg = config or get_config()
        self._base_config = base_cfg
        self._risk_config = _SandboxRiskConfigProxy(base_cfg)
        self._repo = repo or AISandboxRepository()
        # PaperExecutor is reused only for its quote-fetch + fill-price
        # methods. We never call ``executor.submit`` from here — the
        # service composes the steps so it can write to the sandbox
        # table instead of portfolio_trades.
        self._executor_for_quote = PaperExecutor(config=self._risk_config)
        self._mutation_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_enabled(self) -> bool:
        return bool(getattr(self._base_config, "ai_sandbox_enabled", False))

    def get_status(self) -> Dict[str, Any]:
        cfg = self._base_config
        if not self.is_enabled():
            return {
                "status": "disabled",
                "message": (
                    "AI Sandbox is disabled. "
                    "Set AI_SANDBOX_ENABLED=true to enable."
                ),
            }
        return {
            "status": "ready",
            "max_position_value": float(
                getattr(cfg, "ai_sandbox_max_position_value", 5000.0)
            ),
            "max_position_pct": float(
                getattr(cfg, "ai_sandbox_max_position_pct", 0.20)
            ),
            "max_daily_turnover": float(
                getattr(cfg, "ai_sandbox_max_daily_turnover", 100000.0)
            ),
            "symbol_allowlist": list(
                getattr(cfg, "ai_sandbox_symbol_allowlist", []) or []
            ),
            "paper_slippage_bps": int(
                getattr(cfg, "ai_sandbox_paper_slippage_bps", 10)
            ),
            "daemon_enabled": bool(
                getattr(cfg, "ai_sandbox_daemon_enabled", False)
            ),
            "daemon_interval_minutes": int(
                getattr(cfg, "ai_sandbox_daemon_interval_minutes", 60)
            ),
            "daemon_watchlist": list(
                getattr(cfg, "ai_sandbox_daemon_watchlist", []) or []
            ),
        }

    def submit(self, intent: AISandboxIntent) -> Dict[str, Any]:
        """Run the sandbox pipeline. Always returns a dict — never
        raises for ordinary outcomes (block / fail). Re-raises only
        on duplicate request_uid (caller maps to 409)."""
        if not self.is_enabled():
            raise SandboxDisabledError(
                "AI Sandbox is disabled; set AI_SANDBOX_ENABLED=true."
            )
        if not intent.request_uid:
            raise ValueError("AISandboxIntent.request_uid is required")
        with self._mutation_lock:
            return self._submit_locked(intent)

    def _submit_locked(self, intent: AISandboxIntent) -> Dict[str, Any]:
        # 1. Audit row — start
        try:
            self._repo.start_execution(intent)
        except DuplicateSandboxRequestError:
            failed = AISandboxResult(
                intent=intent,
                status=ExecutionStatus.FAILED,
                error_code="DUPLICATE_REQUEST_UID",
                error_message=(
                    f"request_uid={intent.request_uid!r} already submitted"
                ),
            )
            return failed.to_dict()

        # 2. Risk evaluation
        try:
            assessment = self._evaluate_risk(intent)
        except Exception as exc:  # noqa: BLE001 — defensive boundary
            logger.exception("[ai-sandbox] risk engine failed")
            result = AISandboxResult(
                intent=intent,
                status=ExecutionStatus.FAILED,
                error_code="RISK_ENGINE_ERROR",
                error_message=str(exc)[:240],
            )
            self._repo.finish_execution(intent.request_uid, result)
            return result.to_dict()

        if assessment.decision == "block":
            block_msgs = [
                f.message for f in assessment.flags
                if f.severity == RiskSeverity.BLOCK
            ]
            result = AISandboxResult(
                intent=intent,
                status=ExecutionStatus.BLOCKED,
                risk_assessment=assessment,
                error_code="RISK_BLOCKED",
                error_message="; ".join(block_msgs[:3]) or "blocked by risk engine",
            )
            self._repo.finish_execution(intent.request_uid, result)
            return result.to_dict()

        # 3. Paper fill — reuse PaperExecutor's quote + fill price
        order_request = intent.to_order_request()
        try:
            quote = self._executor_for_quote._resolve_quote(order_request)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[ai-sandbox] quote resolution failed for %s: %s",
                intent.symbol, exc,
            )
            quote = None

        if quote is None or self._executor_for_quote._extract_price(
            quote, side=intent.side,
        ) is None:
            result = AISandboxResult(
                intent=intent,
                status=ExecutionStatus.FAILED,
                risk_assessment=assessment,
                error_code="QUOTE_UNAVAILABLE",
                error_message=(
                    f"No quote for {intent.symbol!r} (Firstrade + data_provider)"
                ),
                quote_payload=quote,
            )
            self._repo.finish_execution(intent.request_uid, result)
            return result.to_dict()

        try:
            fill_price = self._executor_for_quote._derive_fill_price(
                order_request, quote,
            )
        except _NotFillable as exc:
            result = AISandboxResult(
                intent=intent,
                status=ExecutionStatus.FAILED,
                risk_assessment=assessment,
                error_code="LIMIT_NOT_REACHABLE",
                error_message=str(exc),
                quote_payload=quote,
            )
            self._repo.finish_execution(intent.request_uid, result)
            return result.to_dict()
        except Exception as exc:  # noqa: BLE001
            logger.exception("[ai-sandbox] fill-price computation failed")
            result = AISandboxResult(
                intent=intent,
                status=ExecutionStatus.FAILED,
                risk_assessment=assessment,
                error_code="FILL_PRICE_FAILED",
                error_message=str(exc)[:240],
                quote_payload=quote,
            )
            self._repo.finish_execution(intent.request_uid, result)
            return result.to_dict()

        # 4. Successful sandbox fill — no portfolio_trades write
        result = AISandboxResult(
            intent=intent,
            status=ExecutionStatus.FILLED,
            fill_price=fill_price,
            fill_quantity=float(intent.quantity),
            fill_time=_utc_now_iso(),
            risk_assessment=assessment,
            quote_payload=quote,
        )
        self._repo.finish_execution(intent.request_uid, result)
        return result.to_dict()

    def list_recent(
        self,
        *,
        agent_run_id: Optional[str] = None,
        symbol: Optional[str] = None,
        status: Optional[str] = None,
        prompt_version: Optional[str] = None,
        limit: int = 50,
    ) -> Dict[str, Any]:
        rows = self._repo.list_executions(
            agent_run_id=agent_run_id, symbol=symbol,
            status=status, prompt_version=prompt_version, limit=limit,
        )
        return {"items": rows, "count": len(rows)}

    def metrics(
        self,
        *,
        since_days: Optional[int] = None,
        prompt_version: Optional[str] = None,
        symbol: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self._repo.aggregate_metrics(
            since_days=since_days, prompt_version=prompt_version,
            symbol=symbol,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _evaluate_risk(self, intent: AISandboxIntent) -> RiskAssessment:
        engine = RiskEngine(self._risk_config)
        order_request = intent.to_order_request()
        # Sandbox doesn't have account-bound positions — pass None
        # for portfolio snapshot. RiskEngine downgrades the position
        # checks to WARNING in that case.
        broker_status = self._fetch_broker_status()
        # Daily turnover rollup is across the SANDBOX, not the live
        # audit table. Compute here.
        daily_turnover = self._sandbox_daily_turnover()
        estimated_price = self._estimate_price(intent)
        return engine.evaluate(
            order_request,
            portfolio_snapshot=None,
            broker_status=broker_status,
            daily_turnover_so_far=daily_turnover,
            estimated_price=estimated_price,
        )

    def _fetch_broker_status(self) -> Optional[Dict[str, Any]]:
        try:
            from src.services.firstrade_sync_service import (
                get_firstrade_sync_service,
            )
            svc = get_firstrade_sync_service()
            return svc.get_status() if svc else None
        except Exception:  # noqa: BLE001
            return None

    def _estimate_price(self, intent: AISandboxIntent) -> Optional[float]:
        order_request = intent.to_order_request()
        try:
            quote = self._executor_for_quote._resolve_quote(order_request)
            if quote:
                return (
                    quote.get("last") or quote.get("ask") or quote.get("bid")
                )
        except Exception:  # noqa: BLE001
            pass
        return None

    def _sandbox_daily_turnover(self) -> float:
        """Compute today's sandbox turnover for the risk gate's
        ``trading_max_daily_turnover`` check. Counts FILLED + PENDING
        rows, multiplied by their requested qty × price (fill_price
        if filled, else limit_price)."""
        from sqlalchemy import select
        from src.storage import AISandboxExecution
        from datetime import datetime, time
        target_day = datetime.utcnow().date()
        day_start = datetime.combine(target_day, time.min)
        day_end = datetime.combine(target_day, time.max)
        with self._repo.db.get_session() as session:
            rows = session.execute(
                select(AISandboxExecution).where(
                    AISandboxExecution.requested_at >= day_start,
                    AISandboxExecution.requested_at <= day_end,
                    AISandboxExecution.status.in_(
                        (ExecutionStatus.PENDING.value, ExecutionStatus.FILLED.value),
                    ),
                )
            ).scalars().all()
        total = 0.0
        for r in rows:
            qty = float(r.quantity or 0)
            price = (
                float(r.fill_price)
                if r.fill_price is not None
                else float(r.limit_price)
                if r.limit_price is not None else 0.0
            )
            total += qty * price
        return round(total, 6)


# =====================================================================
# Module-level singleton
# =====================================================================

_INSTANCE: Optional[AISandboxService] = None
_INSTANCE_LOCK = threading.Lock()


def get_ai_sandbox_service() -> AISandboxService:
    global _INSTANCE
    if _INSTANCE is not None:
        return _INSTANCE
    with _INSTANCE_LOCK:
        if _INSTANCE is None:
            _INSTANCE = AISandboxService()
    return _INSTANCE


def reset_ai_sandbox_service() -> None:
    global _INSTANCE
    with _INSTANCE_LOCK:
        _INSTANCE = None
