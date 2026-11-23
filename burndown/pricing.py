"""Pricing models.

All money is integer microcents (1 cent = 1,000,000 microcents). Prices are
expressed as cents per million units, which is how LLM APIs actually price
tokens ($2.50 / 1M input tokens = 250 cents per million). That choice makes
the core formula exact integer math:

    cost_microcents = quantity * cents_per_million
"""
from __future__ import annotations

from dataclasses import dataclass

MICRO = 1_000_000  # microcents per cent


class PricingModel:
    """Maps a total quantity (within one billing period) to a cost."""

    def cost_microcents(self, quantity: int) -> int:
        raise NotImplementedError


@dataclass(frozen=True)
class PerUnit(PricingModel):
    """Flat price: every unit costs the same."""

    cents_per_million: int

    def cost_microcents(self, quantity: int) -> int:
        return quantity * self.cents_per_million


@dataclass(frozen=True)
class Tiered(PricingModel):
    """Graduated tiers: each slice of usage is billed at its own tier's rate.

    tiers is an ordered list of (limit, cents_per_million); units up to limit
    bill at that rate, overflow falls to the next tier. The final tier must
    have limit None (unbounded). Tiers reset every billing period.
    """

    tiers: tuple  # tuple[tuple[int | None, int], ...]

    def __init__(self, tiers):
        tiers = tuple(tiers)
        if not tiers or tiers[-1][0] is not None:
            raise ValueError("final tier must be (None, rate)")
        object.__setattr__(self, "tiers", tiers)

    def cost_microcents(self, quantity: int) -> int:
        total = 0
        lower = 0
        for limit, rate in self.tiers:
            if limit is None:
                total += max(0, quantity - lower) * rate
                break
            upper = min(quantity, limit)
            if upper > lower:
                total += (upper - lower) * rate
            if quantity <= limit:
                break
            lower = limit
        return total
