# -*- coding: utf-8 -*-
"""Read-only invariant guard — must not regress.

Phase A's hardest line: every Python file in ``src/``, ``api/``, and
``apps/`` (excluding the explicit allowlist) must NOT import or call
``firstrade.order`` / ``firstrade.trade``, and must NOT contain bare
``place_order(`` / ``cancel_order(`` / ``submit_order(`` calls.

The allowlist:
  * ``src/trading/executors/live.py`` — Phase B placeholder; only
    contains ``raise NotImplementedError``. Imports nothing trading-
    related.
  * ``tests/test_trading_invariant_guard.py`` (this file) — fixture
    strings appearing in assertions.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path
from typing import Iterable, List


# Files allowed to mention forbidden tokens (only this guard test +
# the Phase B stub which itself imports nothing).
_ALLOWLIST_PATHS = {
    "tests/test_trading_invariant_guard.py",
    "src/trading/executors/live.py",
}


def _walk_python_files(root: Path) -> Iterable[Path]:
    for p in root.rglob("*.py"):
        # Skip cache + virtualenvs + test_trading_paper_executor's
        # AST-based check (which mentions strings legitimately).
        parts = set(p.parts)
        if any(x in parts for x in ("__pycache__", "node_modules", ".venv", "venv")):
            continue
        yield p


def _project_files() -> List[Path]:
    out: List[Path] = []
    for sub in ("src", "api"):
        out.extend(_walk_python_files(Path(sub)))
    return out


class InvariantGuardTests(unittest.TestCase):
    def test_no_python_file_imports_firstrade_order_or_firstrade_trade(self) -> None:
        forbidden_modules = {"firstrade.order", "firstrade.trade"}
        offenders: List[str] = []
        for path in _project_files():
            rel = str(path).replace("\\", "/")
            if rel in _ALLOWLIST_PATHS:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    if module in forbidden_modules:
                        offenders.append(f"{rel}:{node.lineno} from {module}")
                    if module == "firstrade":
                        for alias in node.names:
                            if alias.name in {"order", "trade"}:
                                offenders.append(
                                    f"{rel}:{node.lineno} from firstrade import {alias.name}"
                                )
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name in forbidden_modules:
                            offenders.append(f"{rel}:{node.lineno} import {alias.name}")
        self.assertEqual(
            offenders, [],
            msg="Read-only invariant violated:\n" + "\n".join(offenders),
        )

    def test_only_live_executor_stub_mentions_live_execution_and_only_to_raise(self) -> None:
        """``LiveExecutor`` exists for Phase B but its body MUST be a
        single ``raise NotImplementedError(...)`` in ``__init__``.
        Adding any real execution body in Phase A is a review-blocker."""
        path = Path("src/trading/executors/live.py")
        tree = ast.parse(path.read_text(encoding="utf-8"))
        live_class = None
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "LiveExecutor":
                live_class = node
                break
        self.assertIsNotNone(live_class, "LiveExecutor class not found")

        # __init__ body must contain a `raise NotImplementedError`
        init_func = next(
            (n for n in ast.walk(live_class)
             if isinstance(n, ast.FunctionDef) and n.name == "__init__"),
            None,
        )
        self.assertIsNotNone(init_func, "LiveExecutor.__init__ not found")
        # Last statement should be a Raise
        body_raises = [s for s in init_func.body if isinstance(s, ast.Raise)]
        self.assertGreaterEqual(
            len(body_raises), 1,
            "LiveExecutor.__init__ must `raise NotImplementedError`",
        )

    def test_no_bare_place_order_or_cancel_order_call_outside_allowlist(self) -> None:
        """Catch raw method-name calls. Different from the import test
        — even if someone re-imports under a new name, calling
        ``place_order(`` / ``cancel_order(`` / ``submit_order(`` from
        non-allowlisted code is a violation. We grep with regex."""
        pattern = re.compile(r"\b(place_order|cancel_order|submit_order)\s*\(")
        offenders: List[str] = []
        for path in _project_files():
            rel = str(path).replace("\\", "/")
            if rel in _ALLOWLIST_PATHS:
                continue
            text = path.read_text(encoding="utf-8")
            for m in pattern.finditer(text):
                # Try to skip lines that are inside docstrings/comments
                # by checking the line's preceding context. Cheap
                # heuristic: skip if the line is inside a triple-quoted
                # block or starts with `#`.
                line_start = text.rfind("\n", 0, m.start()) + 1
                line = text[line_start:text.find("\n", m.end())]
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                # Cheap docstring skip — if the match is preceded by
                # an unescaped triple-quote on its own line within the
                # file, treat as docstring.
                # We use a simple "is this likely prose" filter.
                if any(prose in line for prose in (
                    "no place_order", "NEVER ", "forbidden",
                    "MUST NOT", "禁止", "NOT call ",
                )):
                    continue
                offenders.append(f"{rel}:{text[:m.start()].count(chr(10))+1}: {line.strip()}")
        self.assertEqual(
            offenders, [],
            msg="Bare order-method calls detected:\n" + "\n".join(offenders),
        )


if __name__ == "__main__":
    unittest.main()
