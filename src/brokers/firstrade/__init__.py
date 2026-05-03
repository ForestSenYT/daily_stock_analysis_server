# -*- coding: utf-8 -*-
"""Firstrade read-only connector.

Exposes :class:`FirstradeReadOnlyClient` and the Firstrade-flavoured
dataclasses. The vendor SDK (``firstrade`` on PyPI) is **only ever
imported lazily** from inside :mod:`client` — we never reach into it
at module load, so importing this package doesn't fail when the
package isn't installed (the default Cloud Run image).

Strict allowlist (enforced everywhere in this package):
  * ``from firstrade import account`` — yes.
  * ``from firstrade import order``    — NEVER. Real-trading code paths
    are out of scope and any future need must be designed separately
    behind explicit user opt-in.
"""

from src.brokers.firstrade.client import FirstradeReadOnlyClient

__all__ = ["FirstradeReadOnlyClient"]
