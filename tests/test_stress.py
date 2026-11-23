"""Stress tests: independent, example-based checks that don't share code
with the engine's own invariants.

- A fully hand-computed invoice (expected dollar figures derived here, not
  by the engine).
- Arrival-order invariance: shuffled interleavings across customers and
  months must produce byte-identical invoices and balances, including late
  events that cross credit-grant expiry boundaries.
- Exact tier boundaries and rounding.
- Idempotency-key conflicts (same id, different payload).
"""
import random
import unittest

from burndown import Engine, PerUnit, Tiered, MICRO

NOV1 = 1761955200.0  # 2025-11-01 00:00:00 UTC
DAY = 86400.0


def ev(eid, customer, meter, qty, ts):
    return {"event_id": eid, "customer_id": customer, "meter": meter,
            "quantity": qty, "timestamp": ts}


def make_engine():
    engine = Engine()
    engine.add_meter("input_tokens", PerUnit(cents_per_million=250))
    engine.add_meter("output_tokens", Tiered([(10_000_000, 1000), (None, 800)]))
    return engine


class TestHandComputedInvoice(unittest.TestCase):
    """Every expected number below is worked out here, not by the engine.

    Events for c_h:
      Nov 3:  input 4M  -> 4M * $2.50/1M = $10.00
      Nov 10: output 5M -> 5M * $10/1M   = $50.00
      Nov 20: input 8M  -> $20.00
      Nov 25: output 12M-> marginal from 5M to 17M: 5M*$10/1M + 2M*$8/1M = $66.00
               wait: marginal = cost(17M) - cost(5M) = $156 - $50 = $106.00

    Lines (per-period totals): input 12M = $30.00; output 17M
      = 10M*$10/1M + 7M*$8/1M = $156.00. Gross $186.00.

    Grants: $25 promo expiring Nov 16, $50 prepay no expiry.
      Nov 3 charge $10 -> promo $10 (left $15)
      Nov 10 charge $50 -> promo $15, prepay $35 (prepay left $15)
      Nov 20 charge $20 -> prepay $15, billed $5
      Nov 25 charge $106 -> billed $106
    Credits applied $75.00, amount due $111.00.
    """

    EVENTS = [
        ev("h1", "c_h", "input_tokens", 4_000_000, NOV1 + 2 * DAY),
        ev("h2", "c_h", "output_tokens", 5_000_000, NOV1 + 9 * DAY),
        ev("h3", "c_h", "input_tokens", 8_000_000, NOV1 + 19 * DAY),
        ev("h4", "c_h", "output_tokens", 12_000_000, NOV1 + 24 * DAY),
    ]

    def build(self, events):
        engine = make_engine()
        engine.add_credits("c_h", 2500, expires_at=NOV1 + 15 * DAY, grant_id="cg_promo")
        engine.add_credits("c_h", 5000, grant_id="cg_prepay")
        for raw in events:
            self.assertEqual(engine.ingest(raw).status, "accepted")
        return engine

    def test_matches_hand_arithmetic(self):
        engine = self.build(self.EVENTS)
        inv = engine.invoice("c_h", "2025-11")
        lines = {l.meter: (l.quantity, l.amount_micro) for l in inv.lines}
        self.assertEqual(lines["input_tokens"], (12_000_000, 3_000 * MICRO))
        self.assertEqual(lines["output_tokens"], (17_000_000, 15_600 * MICRO))
        self.assertEqual(inv.gross_micro, 18_600 * MICRO)
        self.assertEqual(inv.credits_applied_micro, 7_500 * MICRO)
        self.assertEqual(inv.amount_due_micro, 11_100 * MICRO)
        self.assertEqual(engine.balance_cents("c_h", now=NOV1 + 29 * DAY), 0)

    def test_same_result_when_nov3_event_arrives_after_month_close(self):
        shuffled = [self.EVENTS[1], self.EVENTS[2], self.EVENTS[3], self.EVENTS[0]]
        engine = self.build(shuffled)
        self.assertEqual(engine.invoice("c_h", "2025-11"),
                         self.build(self.EVENTS).invoice("c_h", "2025-11"))


class TestTierBoundariesAndRounding(unittest.TestCase):
    def test_exact_tier_boundary(self):
        tiered = Tiered([(10_000_000, 1000), (None, 800)])
        self.assertEqual(tiered.cost_microcents(10_000_000), 10_000_000 * 1000)
        self.assertEqual(tiered.cost_microcents(10_000_001), 10_000_000 * 1000 + 800)
        self.assertEqual(tiered.cost_microcents(0), 0)
        self.assertEqual(PerUnit(250).cost_microcents(0), 0)
        # single unit at $2.50/1M costs exactly 250 microcents ($0.00025)
        self.assertEqual(PerUnit(250).cost_microcents(1), 250)

    def test_no_float_drift_over_many_small_events(self):
        engine = make_engine()
        for i in range(10_000):
            engine.ingest(ev(f"r{i}", "c_r", "input_tokens", 3, NOV1 + i))
        inv = engine.invoice("c_r", "2025-11")
        # 30,000 units * 250 microcents = 7,500,000 microcents = $0.075 -> 7.5 cents, exact
        self.assertEqual(inv.gross_micro, 7_500_000)


class TestIdempotencyConflict(unittest.TestCase):
    def test_same_id_different_payload_is_flagged_and_first_wins(self):
        engine = make_engine()
        self.assertEqual(engine.ingest(ev("k1", "c_1", "input_tokens", 1000, NOV1)).status, "accepted")
        conflict = engine.ingest(ev("k1", "c_1", "input_tokens", 999_999, NOV1))
        self.assertEqual(conflict.status, "conflict")
        self.assertIn("first event kept", conflict.reason)
        inv = engine.invoice("c_1", "2025-11")
        self.assertEqual(inv.lines[0].quantity, 1000)
        self.assertEqual(len(engine.ledger), 1)

    def test_same_id_same_payload_is_plain_duplicate(self):
        engine = make_engine()
        engine.ingest(ev("k1", "c_1", "input_tokens", 1000, NOV1))
        self.assertEqual(engine.ingest(ev("k1", "c_1", "input_tokens", 1000, NOV1)).status, "duplicate")


class TestArrivalOrderInvariance(unittest.TestCase):
    """Randomized property: any arrival interleaving - across customers,
    months, and grant-expiry boundaries - yields identical invoices,
    balances, and a clean reconcile."""

    def scenario(self, rng):
        engine = make_engine()
        # grants engineered to straddle event timestamps
        engine.add_credits("c_a", 3000, expires_at=NOV1 + 10 * DAY, grant_id="a_exp")
        engine.add_credits("c_a", 2000, grant_id="a_open")
        engine.add_credits("c_b", 1500, expires_at=NOV1 + 20 * DAY, grant_id="b_exp")
        events = []
        seq = 0
        for _ in range(120):
            seq += 1
            cust = rng.choice(["c_a", "c_b", "c_c"])
            meter = rng.choice(["input_tokens", "output_tokens"])
            # timestamps across Oct 20 - Dec 10, crossing the Nov boundary
            ts = NOV1 + rng.uniform(-12, 39) * DAY
            events.append(ev(f"s{seq}", cust, meter, rng.randint(100, 2_000_000), ts))
        return engine, events

    def totals(self, engine, customers, periods):
        return {
            (c, p): engine.invoice(c, p) for c in customers for p in periods
        } | {("bal", c): engine.balance_cents(c, now=NOV1 + 60 * DAY) for c in customers}

    def test_shuffled_arrivals_all_agree(self):
        customers, periods = ("c_a", "c_b", "c_c"), ("2025-10", "2025-11", "2025-12")
        for seed in range(25):
            rng = random.Random(seed)
            reference_engine, events = self.scenario(rng)
            for raw in sorted(events, key=lambda e: (e["timestamp"], e["event_id"])):
                reference_engine.ingest(raw)
            reference = self.totals(reference_engine, customers, periods)

            for shuffle_seed in (1, 2, 3):
                shuffler = random.Random(1000 * seed + shuffle_seed)
                shuffled = events[:]
                shuffler.shuffle(shuffled)
                engine, _ = self.scenario(rng)
                for raw in shuffled:
                    engine.ingest(raw)
                # invoices are pure replay: always identical, even before reconcile
                self.assertEqual(
                    {k: v for k, v in self.totals(engine, customers, periods).items() if k[0] != "bal"},
                    {k: v for k, v in reference.items() if k[0] != "bal"},
                    f"seed={seed} shuffle={shuffle_seed}")
                # the live cache may drift on adversarial orderings; reconcile repairs it
                engine.reconcile()
                self.assertEqual(self.totals(engine, customers, periods), reference,
                                 f"post-reconcile seed={seed} shuffle={shuffle_seed}")
                self.assertTrue(engine.reconcile()["matched"],
                                f"stable after repair seed={seed} shuffle={shuffle_seed}")

    def test_late_event_predating_grant_expiry_still_burns_it(self):
        # grant expires Nov 10; event timestamped Nov 5 arrives Nov 25.
        # Event time rules: it burns the grant, in both live and replay paths.
        engine = make_engine()
        engine.add_credits("c_l", 1000, expires_at=NOV1 + 9 * DAY, grant_id="g")
        late = ev("late", "c_l", "input_tokens", 2_000_000, NOV1 + 4 * DAY)  # $5.00
        engine.ingest(late)
        inv = engine.invoice("c_l", "2025-11")
        self.assertEqual(inv.credits_applied_micro, 500 * MICRO)
        self.assertEqual(inv.amount_due_micro, 0)
        self.assertTrue(engine.reconcile()["matched"])


if __name__ == "__main__":
    unittest.main()


class TestCrossPeriodDriftAndRepair(unittest.TestCase):
    """The known hard case, documented: shared credit grants + out-of-order
    arrival across a period boundary. Which MONTH a credit burn is attributed
    to depends on arrival order in the live cache (a Nov event can consume
    credits that an earlier, not-yet-arrived Oct event should have used).
    Invoices are pure replay, so they are correct throughout; reconcile()
    detects and repairs the live attribution drift. This is the same reason
    Stripe reconciles real-time ratings against the ledger."""

    def test_drift_then_repair(self):
        e_oct = ev("oct", "c_d", "input_tokens", 8_000_000, NOV1 - 6 * DAY)  # $20, Oct 26
        e_nov = ev("nov", "c_d", "input_tokens", 8_000_000, NOV1 + 4 * DAY)  # $20, Nov 5

        def build(order):
            engine = make_engine()
            engine.add_credits("c_d", 3000, grant_id="g")  # $30, no expiry
            for raw in order:
                engine.ingest(raw)
            return engine

        adversarial = build([e_nov, e_oct])  # November event ingested first
        ordered = build([e_oct, e_nov])

        # invoices (pure replay) agree even before any reconcile
        self.assertEqual(adversarial.invoice("c_d", "2025-10"), ordered.invoice("c_d", "2025-10"))
        self.assertEqual(adversarial.invoice("c_d", "2025-11"), ordered.invoice("c_d", "2025-11"))
        # correct attribution: October's $20 burns credits first, November bills $10
        self.assertEqual(ordered.invoice("c_d", "2025-10").amount_due_micro, 0)
        self.assertEqual(ordered.invoice("c_d", "2025-11").amount_due_micro, 1000 * MICRO)

        # the adversarial live cache drifted; reconcile detects and repairs it
        report = adversarial.reconcile()
        self.assertFalse(report["matched"])
        self.assertTrue(adversarial.reconcile()["matched"])  # stable after repair
        self.assertEqual(adversarial.balance_cents("c_d", now=NOV1 + 30 * DAY), 0)


if __name__ == "__main__":
    unittest.main()
