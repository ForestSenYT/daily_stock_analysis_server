# -*- coding: utf-8 -*-
"""Repository layer for Quant Research Lab.

Phase 1: no DB tables yet. We expose a single read helper that the
status / capabilities endpoints can ask "is the Lab persistence layer
ready?" without coupling them to SQLAlchemy directly.

Phase 2 onwards will add real ORM models for ``quant_research_runs``,
``quant_factor_evaluations``, ``quant_backtest_results`` etc. — those
will be declared on the same ``Base`` as existing tables (via
``src.storage`` import) so ``Base.metadata.create_all`` keeps working
without migrations.

Deliberately does NOT import the existing ``DatabaseManager`` at module
import time — Cloud Run can boot before the singleton is initialized,
and we want this module loadable in any context.
"""

from __future__ import annotations

from typing import Optional


class QuantResearchRepository:
    """Facade for future quant-research persistence operations.

    Methods are stubs in Phase 1. The class exists so that ``service.py``
    can take it as a dependency from day one — letting tests inject a
    fake without ever talking to SQLite.
    """

    def __init__(self, db_manager: Optional[object] = None) -> None:
        # Accept ``Any`` to avoid importing ``DatabaseManager`` here. In
        # Phase 2+ we'll narrow the type.
        self._db = db_manager

    # ------------------------------------------------------------------
    # Status helpers (Phase 1)
    # ------------------------------------------------------------------

    def is_persistence_available(self) -> bool:
        """True when a working database backend is reachable.

        Phase 1: returns True iff a DatabaseManager-like object was
        injected. Doesn't actually open a session — Phase 2 may run a
        cheap ``SELECT 1`` here.
        """
        return self._db is not None

    # ------------------------------------------------------------------
    # Phase 2+ stubs (no-op for now)
    # ------------------------------------------------------------------

    def save_run_meta(self, *_args, **_kwargs):  # pragma: no cover - phase 2
        raise NotImplementedError(
            "Quant Research persistence is not implemented yet (Phase 1 scaffold)."
        )

    def get_run(self, run_id: str):  # pragma: no cover - phase 2
        raise NotImplementedError(
            "Quant Research persistence is not implemented yet (Phase 1 scaffold)."
        )
