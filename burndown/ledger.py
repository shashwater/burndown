"""The revenue ledger: append-only, deduplicated by event id.

Duplicate delivery is a fact of life in event pipelines (retries, consumer
replays, multi-region double-processing). The ledger makes it harmless:
an event id can land exactly once.
"""
from __future__ import annotations

from typing import Iterator, Optional

from .events import UsageEvent


class Ledger:
    def __init__(self) -> None:
        self._events: dict[str, UsageEvent] = {}

    def append(self, event: UsageEvent) -> bool:
        """Append an event. Returns False (and changes nothing) on duplicate id."""
        if event.event_id in self._events:
            return False
        self._events[event.event_id] = event
        return True

    def __len__(self) -> int:
        return len(self._events)

    def __contains__(self, event_id: str) -> bool:
        return event_id in self._events

    def get(self, event_id: str) -> Optional[UsageEvent]:
        return self._events.get(event_id)

    def event_ids(self) -> set:
        return set(self._events)

    def events(self, customer_id: Optional[str] = None) -> Iterator[UsageEvent]:
        for ev in self._events.values():
            if customer_id is None or ev.customer_id == customer_id:
                yield ev
