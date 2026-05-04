# -*- coding: utf-8 -*-
"""Background daemon for AI sandbox: scheduled batch decision runs.

When ``AI_SANDBOX_DAEMON_ENABLED=true``, the FastAPI lifespan starts
a daemon thread that, every ``AI_SANDBOX_DAEMON_INTERVAL_MINUTES``:
  1. Iterates ``AI_SANDBOX_DAEMON_WATCHLIST``.
  2. For each symbol, asks the AI agent for a buy/sell/hold call
     (compact prompt, single LLM round-trip — NOT a full /analyze).
  3. Submits the decision through ``AISandboxService.submit``.
  4. Also runs a P&L horizon rollup pass for older filled rows.

Hard rules:
  * Same daemon pattern as ``broker_auto_sync_service``: idempotent
    start / stop, daemon thread, responsive shutdown.
  * Pulls config every tick so a runtime flip can disable the loop
    without a process restart.
  * Each tick is best-effort. Per-symbol failures are logged + skipped;
    the loop keeps running.

This module is **safe to import** even when AI sandbox is disabled —
the worker function lazy-imports the LLM adapter so a misconfigured
LLM doesn't break startup.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any, List, Optional

logger = logging.getLogger(__name__)


_WORKER_THREAD: Optional[threading.Thread] = None
_STOP_EVENT: Optional[threading.Event] = None
_START_LOCK = threading.Lock()


def _is_enabled(config: Any) -> bool:
    return (
        bool(getattr(config, "ai_sandbox_enabled", False))
        and bool(getattr(config, "ai_sandbox_daemon_enabled", False))
    )


def _interval_seconds(config: Any) -> float:
    minutes = int(getattr(config, "ai_sandbox_daemon_interval_minutes", 60))
    return float(max(5, minutes) * 60)


def _watchlist(config: Any) -> List[str]:
    return list(getattr(config, "ai_sandbox_daemon_watchlist", []) or [])


def _prompt_version(config: Any) -> str:
    return str(getattr(config, "ai_sandbox_default_prompt_version", "v1"))


# =====================================================================
# Per-symbol decision logic
# =====================================================================

_COMPACT_PROMPT_TEMPLATE = """You are a quantitative trading agent running in a forward-simulation sandbox.
Your only job: emit ONE structured trade decision for the given stock based on its recent technicals.

Stock: {symbol}
Latest quote: {quote_summary}
Constraints:
  * Decision = exactly one of: buy / sell / hold
  * If decision is buy or sell, suggest a quantity in the range 1..{max_quantity}
  * Provide confidence in [0, 1]
  * Provide reasoning in <= 240 characters

Output STRICT JSON (no markdown):
{{
  "decision": "buy" | "sell" | "hold",
  "quantity": <int>,
  "confidence": <float 0..1>,
  "reasoning": "<short string>"
}}
"""


def _summarise_quote(quote: dict) -> str:
    if not quote:
        return "no quote available"
    parts = []
    for key in ("last", "bid", "ask", "high", "low"):
        v = quote.get(key)
        if v is not None:
            parts.append(f"{key}={v}")
    return ", ".join(parts) or str(quote)


def _decide_one_symbol(symbol: str, *, config: Any) -> Optional[dict]:
    """Ask the LLM for a buy/sell/hold decision on ``symbol``.
    Returns the structured decision dict or None on failure."""
    try:
        from src.services.firstrade_sync_service import (
            get_firstrade_sync_service,
        )
        from data_provider import DataFetcherManager
    except Exception as exc:  # noqa: BLE001
        logger.warning("[ai-sandbox-daemon] data sources unavailable: %s", exc)
        return None

    # 1. Get a quote
    quote = None
    try:
        ft = get_firstrade_sync_service()
        if ft:
            quote = ft.get_quote(symbol)
    except Exception:  # noqa: BLE001
        quote = None
    if quote is None:
        try:
            mgr = DataFetcherManager()
            rt = mgr.get_realtime_quote(symbol, log_final_failure=False)
            if rt is not None:
                quote = (
                    rt.to_dict() if hasattr(rt, "to_dict")
                    else dict(rt) if isinstance(rt, dict) else None
                )
        except Exception:  # noqa: BLE001
            quote = None

    if not quote:
        logger.info(
            "[ai-sandbox-daemon] %s: no quote available; skipping",
            symbol,
        )
        return None

    # 2. Build compact prompt
    max_quantity = int(_max_qty_from_config(config, quote))
    prompt = _COMPACT_PROMPT_TEMPLATE.format(
        symbol=symbol,
        quote_summary=_summarise_quote(quote),
        max_quantity=max_quantity,
    )

    # 3. Call LLM via existing adapter
    try:
        from src.agent.llm_adapter import LLMToolAdapter
        adapter = LLMToolAdapter(config)
        response = adapter.call_completion(
            messages=[{"role": "user", "content": prompt}],
            tools=None,
            max_tokens=300,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[ai-sandbox-daemon] LLM call failed for %s: %s", symbol, exc,
        )
        return None

    # 4. Parse JSON
    text = (response or {}).get("content") or ""
    if not text:
        return None
    try:
        from src.agent.runner import try_parse_json
        parsed = try_parse_json(text)
    except Exception:  # noqa: BLE001
        parsed = None
    if not isinstance(parsed, dict):
        logger.debug(
            "[ai-sandbox-daemon] %s: LLM returned non-JSON, skipping (%s)",
            symbol, text[:120],
        )
        return None

    decision = (parsed.get("decision") or "").lower().strip()
    if decision not in ("buy", "sell", "hold"):
        return None
    return {
        "decision": decision,
        "quantity": int(parsed.get("quantity") or 0),
        "confidence": float(parsed.get("confidence") or 0.0),
        "reasoning": str(parsed.get("reasoning") or "")[:240],
        "model_used": (response or {}).get("model") or "",
        "quote": quote,
    }


def _max_qty_from_config(config: Any, quote: dict) -> float:
    """Choose a sensible max quantity hint for the LLM. Caps at
    ``ai_sandbox_max_position_value / price`` so even if the LLM
    suggests max, the RiskEngine still has slack."""
    try:
        max_value = float(getattr(config, "ai_sandbox_max_position_value", 5000.0))
    except Exception:  # noqa: BLE001
        max_value = 5000.0
    price = (quote or {}).get("last") or (quote or {}).get("ask") or 0
    try:
        price = float(price or 0)
    except (TypeError, ValueError):
        price = 0
    if price <= 0:
        return 10.0
    return max(1, int(max_value // price))


def _run_one_tick() -> None:
    """One full daemon tick: decide each watchlist symbol + submit
    if non-hold, then run P&L rollup."""
    from src.config import get_config
    from src.services.ai_sandbox_service import get_ai_sandbox_service
    from src.ai_sandbox.types import AISandboxIntent
    from src.trading.types import OrderSide, OrderType

    config = get_config()
    if not _is_enabled(config):
        return
    watchlist = _watchlist(config)
    if not watchlist:
        logger.info("[ai-sandbox-daemon] watchlist empty; nothing to do")
        return
    svc = get_ai_sandbox_service()
    run_id = f"daemon-{uuid.uuid4().hex[:16]}"
    prompt_version = _prompt_version(config)

    submitted = 0
    held = 0
    skipped = 0
    for symbol in watchlist:
        try:
            decision = _decide_one_symbol(symbol, config=config)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[ai-sandbox-daemon] %s decision failed: %s", symbol, exc,
            )
            skipped += 1
            continue
        if decision is None:
            skipped += 1
            continue
        if decision["decision"] == "hold":
            held += 1
            continue
        if decision["quantity"] <= 0:
            skipped += 1
            continue

        side = (
            OrderSide.BUY if decision["decision"] == "buy" else OrderSide.SELL
        )
        intent = AISandboxIntent(
            symbol=symbol,
            side=side,
            quantity=float(decision["quantity"]),
            order_type=OrderType.MARKET,
            agent_run_id=run_id,
            prompt_version=prompt_version,
            confidence_score=float(decision["confidence"]),
            reasoning_text=decision["reasoning"],
            model_used=decision["model_used"],
            request_uid=f"{run_id}-{symbol}",
        )
        try:
            svc.submit(intent)
            submitted += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[ai-sandbox-daemon] submit failed for %s: %s", symbol, exc,
            )
            skipped += 1

    logger.info(
        "[ai-sandbox-daemon] tick done: run_id=%s submitted=%d held=%d skipped=%d",
        run_id, submitted, held, skipped,
    )

    # P&L rollup
    try:
        from src.services.ai_sandbox_pnl_service import AISandboxPnlService
        AISandboxPnlService().compute_pnl_for_pending(limit=50)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[ai-sandbox-daemon] pnl rollup failed: %s", exc)


def _worker(stop_event: threading.Event) -> None:
    from src.config import get_config

    logger.info("[ai-sandbox-daemon] worker thread started")
    while not stop_event.is_set():
        config = get_config()
        if not _is_enabled(config):
            logger.info(
                "[ai-sandbox-daemon] disabled at tick start; exiting worker"
            )
            return
        interval = _interval_seconds(config)
        try:
            _run_one_tick()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[ai-sandbox-daemon] tick raised: %s", exc)
        deadline = time.monotonic() + interval
        while not stop_event.is_set() and time.monotonic() < deadline:
            stop_event.wait(timeout=min(5.0, deadline - time.monotonic()))
    logger.info("[ai-sandbox-daemon] worker thread stopped")


def start_ai_sandbox_daemon() -> bool:
    """Idempotent start. Returns True if a fresh worker started."""
    global _WORKER_THREAD, _STOP_EVENT
    from src.config import get_config

    config = get_config()
    if not _is_enabled(config):
        logger.info(
            "[ai-sandbox-daemon] disabled (sandbox=%s, daemon=%s)",
            getattr(config, "ai_sandbox_enabled", False),
            getattr(config, "ai_sandbox_daemon_enabled", False),
        )
        return False
    with _START_LOCK:
        if _WORKER_THREAD is not None and _WORKER_THREAD.is_alive():
            return False
        _STOP_EVENT = threading.Event()
        _WORKER_THREAD = threading.Thread(
            target=_worker, args=(_STOP_EVENT,),
            name="ai-sandbox-daemon", daemon=True,
        )
        _WORKER_THREAD.start()
        logger.info(
            "[ai-sandbox-daemon] started (interval=%d min, watchlist=%s)",
            int(getattr(config, "ai_sandbox_daemon_interval_minutes", 60)),
            _watchlist(config),
        )
        return True


def stop_ai_sandbox_daemon(*, timeout: float = 10.0) -> None:
    global _WORKER_THREAD, _STOP_EVENT
    with _START_LOCK:
        if _STOP_EVENT is not None:
            _STOP_EVENT.set()
        worker = _WORKER_THREAD
    if worker is not None and worker.is_alive():
        worker.join(timeout=timeout)


def is_running() -> bool:
    return _WORKER_THREAD is not None and _WORKER_THREAD.is_alive()
