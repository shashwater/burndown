"""Usage event model and stateless validation.

Mirrors the first hop of a real usage pipeline: validate at the edge,
before an event is allowed anywhere near the ledger. Bad events are
rejected with a reason, never silently dropped.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class ValidationError(ValueError):
    """Raised when a raw payload is not a well-formed usage event."""


@dataclass(frozen=True)
class UsageEvent:
    """One recorded unit of usage.

    event_id is the idempotency key: the ledger dedupes on it, so producers
    can retry freely and replays are safe.
    """

    event_id: str
    customer_id: str
    meter: str
    quantity: int
    timestamp: float  # event time, unix seconds (UTC)
    meta: dict = field(default_factory=dict)


def parse_event(raw: Any) -> UsageEvent:
    """Validate a raw payload and return a UsageEvent, or raise ValidationError."""
    if not isinstance(raw, dict):
        raise ValidationError("event must be a JSON object")

    event_id = raw.get("event_id")
    if not isinstance(event_id, str) or not event_id:
        raise ValidationError("event_id must be a non-empty string")

    customer_id = raw.get("customer_id")
    if not isinstance(customer_id, str) or not customer_id:
        raise ValidationError("customer_id must be a non-empty string")

    meter = raw.get("meter")
    if not isinstance(meter, str) or not meter:
        raise ValidationError("meter must be a non-empty string")

    quantity = raw.get("quantity")
    if isinstance(quantity, bool) or not isinstance(quantity, int):
        raise ValidationError("quantity must be an integer")
    if quantity <= 0:
        raise ValidationError("quantity must be positive")

    timestamp = raw.get("timestamp")
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
        raise ValidationError("timestamp must be a number (unix seconds)")

    meta = raw.get("meta", {})
    if not isinstance(meta, dict):
        raise ValidationError("meta must be an object when present")

    return UsageEvent(
        event_id=event_id,
        customer_id=customer_id,
        meter=meter,
        quantity=quantity,
        timestamp=float(timestamp),
        meta=meta,
    )
