# -*- coding: utf-8 -*-
"""Transaction-cost model for the Research Backtest engine.

Phase 3 keeps the model simple but conservative: every dollar of
turnover incurs a fixed fraction of itself as a deduction from NAV.

    cost_dollars[t] = turnover_dollars[t] * (commission_bps + slippage_bps) / 1e4

Where ``turnover_dollars`` is the Σ |w_t − w_{t−1}| × NAV_{t−1} amount
of capital that was reallocated on day ``t``. Halving (one-sided
turnover) is left to the caller of ``turnover()`` — this module
operates on the raw two-sided weight diff so commission applies to
both legs of every trade.

Rationale:
- Real broker commissions are typically a few basis points (e.g. 1–5
  bps for institutional, 5–15 bps for retail). Default to 10 bps.
- Slippage = the difference between the price you assumed (close) and
  what you actually got. For liquid US large-caps, 5 bps is a
  reasonable conservative default; more for HK/A-share microcaps.
- We charge cost AFTER the daily PnL on the *previous* weights, so the
  rebalance "happens" at the end of day t and reduces NAV before t+1
  starts. This matches the standard close-to-close convention used by
  ``engine.py``.

Out of scope (intentional, for Phase 3):
- Tiered commission schedules
- Bid/ask aware slippage (would need quote data we don't have)
- Borrow / margin financing on the short leg
- Tax / stamp-duty (A-share specific)
"""

from __future__ import annotations

from dataclasses import dataclass


# Defaults reasonable for US equity research. Endpoint validates user
# overrides into [0, MAX_BPS] before reaching the engine.
DEFAULT_COMMISSION_BPS = 10.0
DEFAULT_SLIPPAGE_BPS = 5.0
MAX_BPS = 1000.0  # 10% — anyone hitting this should reconsider their params


@dataclass(frozen=True)
class CostModel:
    """Immutable cost spec carried through the backtest run.

    Both fields are in basis points (1 bp = 0.01%). They're stored
    separately so the report can disclose them individually (some
    teams want to break out market-impact assumptions vs broker fees).
    """
    commission_bps: float = DEFAULT_COMMISSION_BPS
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS

    @property
    def total_bps(self) -> float:
        return self.commission_bps + self.slippage_bps

    @property
    def total_rate(self) -> float:
        """Total cost as a decimal fraction (e.g. 0.0015 for 15 bps)."""
        return self.total_bps / 10000.0

    @classmethod
    def validated(cls, commission_bps: float, slippage_bps: float) -> "CostModel":
        """Construct after clamping to ``[0, MAX_BPS]``. Raises ``ValueError``
        for negatives or NaN — those are user errors, not regimes we
        want to silently tolerate.
        """
        for label, value in (("commission_bps", commission_bps), ("slippage_bps", slippage_bps)):
            if value is None or value != value or value < 0 or value > MAX_BPS:
                raise ValueError(
                    f"{label}={value!r} is invalid (must be 0 ≤ x ≤ {MAX_BPS})"
                )
        return cls(commission_bps=float(commission_bps), slippage_bps=float(slippage_bps))


def cost_for_turnover(turnover_dollars: float, model: CostModel) -> float:
    """Convert a dollar amount of two-sided turnover into a dollar cost.

    Pure function so it's trivial to unit test. The engine calls this
    once per rebalance day after computing the L1 weight diff.
    """
    if turnover_dollars < 0:
        raise ValueError("turnover_dollars must be ≥ 0")
    return float(turnover_dollars) * model.total_rate
