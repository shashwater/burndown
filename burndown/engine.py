"""The billing engine: ingest, accrual, credit burndown, invoices, reconcile.

Two ways to read the same truth:

- Live state is updated incrementally on every accepted event, so a
  customer's prepaid credit balance is always current (the burndown).
- invoice() is a pure replay over the ledger. The ledger is the source of
  truth, so a regenerated invoice reflects late events automatically, and
  reconcile() can rebuild live state from scratch and prove it matches.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field

from .events import UsageEvent, ValidationError, parse_event
from .ledger import Ledger
from .pricing import MICRO, PricingModel


def period_of(ts: float) -> str:
    """Billing period (calendar month, UTC) containing a unix timestamp."""
    return time.strftime("%Y-%m", time.gmtime(ts))


@dataclass
class CreditGrant:
    """Prepaid credits a customer burns down before anything is billed."""

    customer_id: str
    cents: int
    grant_id: str = ""
    expires_at: float | None = None
    remaining_micro: int = field(init=False)

    def __post_init__(self) -> None:
        self.remaining_micro = self.cents * MICRO
        if not self.grant_id:
            self.grant_id = "cg_" + uuid.uuid4().hex[:12]

    def copy(self) -> "CreditGrant":
        return CreditGrant(self.customer_id, self.cents, self.grant_id, self.expires_at)


@dataclass
class IngestResult:
    status: str  # "accepted" | "duplicate" | "rejected"
    event_id: str | None = None
    reason: str | None = None


@dataclass
class InvoiceLine:
    meter: str
    quantity: int
    amount_micro: int


@dataclass
class Invoice:
    customer_id: str
    period: str
    lines: list
    credits_applied_micro: int
    amount_due_micro: int
    event_count: int

    @property
    def gross_micro(self) -> int:
        return sum(line.amount_micro for line in self.lines)

    @staticmethod
    def _dollars(micro: int) -> str:
        return f"${micro / (100 * MICRO):,.2f}"

    def render(self) -> str:
        out = [f"Invoice for {self.customer_id} - {self.period} (UTC)"]
        for line in self.lines:
            out.append(
                f"  {line.meter:<16} {line.quantity:>14,} units   {self._dollars(line.amount_micro):>12}"
            )
        out.append(f"  {'gross':<16} {'':>14}        {self._dollars(self.gross_micro):>12}")
        if self.credits_applied_micro:
            out.append(
                f"  {'credits applied':<16} {'':>14}       -{self._dollars(self.credits_applied_micro):>11}"
            )
        out.append(f"  {'amount due':<16} {'':>14}        {self._dollars(self.amount_due_micro):>12}")
        return "\n".join(out)


def _expiry_key(grant: CreditGrant):
    # active grants burn earliest-expiry first; grants with no expiry burn last
    return (grant.expires_at is None, grant.expires_at or 0)


class Engine:
    def __init__(self) -> None:
        self.ledger = Ledger()
        self.pricing: dict[str, PricingModel] = {}
        self.grants: list[CreditGrant] = []
        self.dead_letter: list = []  # (raw_payload, reason)
        self._lock = threading.Lock()
        # live incremental state
        self._qty: dict = {}     # (customer, period, meter) -> units
        self._billed: dict = {}  # (customer, period) -> overage microcents
        self._burned: dict = {}  # (customer, period) -> credits burned microcents
        self._max_period: dict = {}  # customer -> latest billing period applied

    # --- setup -----------------------------------------------------------

    def add_meter(self, name: str, pricing: PricingModel) -> None:
        self.pricing[name] = pricing

    def add_credits(
        self, customer_id: str, cents: int, expires_at: float | None = None, grant_id: str | None = None
    ) -> CreditGrant:
        grant = CreditGrant(customer_id, cents, grant_id or "", expires_at)
        self.grants.append(grant)
        return grant

    # --- ingest ----------------------------------------------------------

    def ingest(self, raw) -> IngestResult:
        try:
            ev = raw if isinstance(raw, UsageEvent) else parse_event(raw)
        except ValidationError as exc:
            self.dead_letter.append((raw, str(exc)))
            return IngestResult("rejected", reason=str(exc))
        if ev.meter not in self.pricing:
            reason = f"unknown meter {ev.meter!r}"
            self.dead_letter.append((raw, reason))
            return IngestResult("rejected", event_id=ev.event_id, reason=reason)
        with self._lock:
            existing = self.ledger.get(ev.event_id)
            if existing is not None:
                if existing != ev:
                    return IngestResult(
                        "conflict",
                        event_id=ev.event_id,
                        reason="event_id reused with a different payload; first event kept",
                    )
                return IngestResult("duplicate", event_id=ev.event_id)
            self.ledger.append(ev)
            self._apply(ev)
        return IngestResult("accepted", event_id=ev.event_id)

    def _apply(self, ev: UsageEvent) -> None:
        period = period_of(ev.timestamp)
        max_period = self._max_period.get(ev.customer_id)
        if max_period is not None and period < max_period:
            # Cross-period out-of-order delivery: an earlier-month event can
            # change how shared credit grants SHOULD have been attributed.
            # Same-period reordering is provably harmless (burn totals are
            # arrival-order invariant), but cross-period is not, so re-anchor
            # this customer's live state from the ledger. No read can ever
            # return stale money.
            self._rebuild_customer(ev.customer_id)
            return
        self._max_period[ev.customer_id] = period if max_period is None else max(period, max_period)
        self._apply_incremental(ev)

    def _apply_incremental(self, ev: UsageEvent) -> None:
        period = period_of(ev.timestamp)
        key = (ev.customer_id, period, ev.meter)
        price = self.pricing[ev.meter]
        q0 = self._qty.get(key, 0)
        delta = price.cost_microcents(q0 + ev.quantity) - price.cost_microcents(q0)
        self._qty[key] = q0 + ev.quantity
        self._charge(ev.customer_id, period, delta, ev.timestamp, self.grants, self._billed, self._burned)

    def _rebuild_customer(self, customer_id: str) -> None:
        """Targeted repair: replay one customer's ledger history into live state."""
        self._qty = {k: v for k, v in self._qty.items() if k[0] != customer_id}
        self._billed = {k: v for k, v in self._billed.items() if k[0] != customer_id}
        self._burned = {k: v for k, v in self._burned.items() if k[0] != customer_id}
        for g in self.grants:
            if g.customer_id == customer_id:
                g.remaining_micro = g.cents * MICRO
        for ev in sorted(
            self.ledger.events(customer_id=customer_id),
            key=lambda e: (e.timestamp, e.event_id),
        ):
            self._apply_incremental(ev)
            period = period_of(ev.timestamp)
            if period > self._max_period.get(customer_id, ""):
                self._max_period[customer_id] = period

    @staticmethod
    def _charge(customer_id, period, micro, now, grants, billed, burned) -> None:
        remaining = micro
        active = [
            g
            for g in grants
            if g.customer_id == customer_id
            and g.remaining_micro > 0
            and (g.expires_at is None or g.expires_at > now)
        ]
        for g in sorted(active, key=_expiry_key):
            take = min(g.remaining_micro, remaining)
            if take:
                g.remaining_micro -= take
                burned[(customer_id, period)] = burned.get((customer_id, period), 0) + take
                remaining -= take
            if not remaining:
                break
        if remaining:
            billed[(customer_id, period)] = billed.get((customer_id, period), 0) + remaining

    # --- reads -----------------------------------------------------------

    def balance_cents(self, customer_id: str, now: float | None = None) -> int:
        """Live prepaid credit balance in cents (the real-time burndown)."""
        now = time.time() if now is None else now
        return (
            sum(
                g.remaining_micro
                for g in self.grants
                if g.customer_id == customer_id
                and (g.expires_at is None or g.expires_at > now)
            )
            // MICRO
        )

    def invoice(self, customer_id: str, period: str) -> Invoice:
        """Pure replay over the ledger; always consistent with late events."""
        events = sorted(
            self.ledger.events(customer_id=customer_id),
            key=lambda e: (e.timestamp, e.event_id),
        )
        grants = [g.copy() for g in self.grants if g.customer_id == customer_id]
        qty: dict = {}       # (period, meter) -> units (tiers reset per period)
        billed: dict = {}    # period -> overage microcents
        burned: dict = {}    # period -> credits microcents
        for ev in events:
            p = period_of(ev.timestamp)
            key = (p, ev.meter)
            price = self.pricing[ev.meter]
            q0 = qty.get(key, 0)
            delta = price.cost_microcents(q0 + ev.quantity) - price.cost_microcents(q0)
            qty[key] = q0 + ev.quantity
            self._charge(customer_id, p, delta, ev.timestamp, grants, billed, burned)

        lines = [
            InvoiceLine(meter=m, quantity=q, amount_micro=self.pricing[m].cost_microcents(q))
            for (p, m), q in sorted(qty.items())
            if p == period
        ]
        return Invoice(
            customer_id=customer_id,
            period=period,
            lines=lines,
            credits_applied_micro=burned.get((customer_id, period), 0),
            amount_due_micro=billed.get((customer_id, period), 0),
            event_count=sum(1 for e in events if period_of(e.timestamp) == period),
        )

    # --- reconciliation --------------------------------------------------

    def reconcile(self, external_events=None) -> dict:
        """Audit pass: rebuild live state from the ledger and report any drift.

        With re-anchoring on out-of-order ingest this should always match;
        a mismatch here means a bug, and the fresh state is adopted anyway.

        With external_events (an iterable of raw payloads, e.g. the producer's
        copy of the stream), also report which event ids are missing from the
        ledger and which are unknown to the producer.
        """
        fresh_qty: dict = {}
        fresh_billed: dict = {}
        fresh_burned: dict = {}
        fresh_grants = [g.copy() for g in self.grants]
        events = sorted(self.ledger.events(), key=lambda e: (e.timestamp, e.event_id))
        for ev in events:
            period = period_of(ev.timestamp)
            key = (ev.customer_id, period, ev.meter)
            price = self.pricing[ev.meter]
            q0 = fresh_qty.get(key, 0)
            delta = price.cost_microcents(q0 + ev.quantity) - price.cost_microcents(q0)
            fresh_qty[key] = q0 + ev.quantity
            self._charge(ev.customer_id, period, delta, ev.timestamp, fresh_grants, fresh_billed, fresh_burned)

        drift = []
        if fresh_qty != self._qty:
            drift.append("accrued quantities differ")
        if fresh_billed != self._billed:
            drift.append("billed overage differs")
        if fresh_burned != self._burned:
            drift.append("credits burned differ")
        live_remaining = {g.grant_id: g.remaining_micro for g in self.grants}
        for g in fresh_grants:
            if live_remaining.get(g.grant_id) != g.remaining_micro:
                drift.append(f"grant {g.grant_id} balance differs")
        if drift:
            # live cache lost the argument; the ledger is the truth
            self._qty, self._billed, self._burned = fresh_qty, fresh_billed, fresh_burned
            for g in self.grants:
                for fg in fresh_grants:
                    if g.grant_id == fg.grant_id:
                        g.remaining_micro = fg.remaining_micro

        report = {"matched": not drift, "drift": drift, "events_replayed": len(events)}
        if external_events is not None:
            external_ids = {e.get("event_id") for e in external_events if isinstance(e, dict)}
            external_ids.discard(None)
            ledger_ids = self.ledger.event_ids()
            report["missing_from_ledger"] = sorted(external_ids - ledger_ids)
            report["unknown_to_producer"] = sorted(ledger_ids - external_ids)
        return report
