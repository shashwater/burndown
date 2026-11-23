import time
import unittest

from burndown import Engine, PerUnit, Tiered, parse_event, MICRO
from burndown.engine import period_of

NOV1 = 1761955200.0  # 2025-11-01 00:00:00 UTC
OCT15 = 1760486400.0  # 2025-10-15 00:00:00 UTC
DEC1 = 1764547200.0  # 2025-12-01 00:00:00 UTC


def ev(eid, customer="c_1", meter="input_tokens", qty=1000, ts=NOV1):
    return {"event_id": eid, "customer_id": customer, "meter": meter,
            "quantity": qty, "timestamp": ts}


def make_engine():
    engine = Engine()
    engine.add_meter("input_tokens", PerUnit(cents_per_million=250))
    engine.add_meter("output_tokens", Tiered([(10_000_000, 1000), (None, 800)]))
    return engine


class TestValidation(unittest.TestCase):
    def test_rejects_bad_events_with_reason(self):
        engine = make_engine()
        bad = [
            {"event_id": "x"},  # missing almost everything
            ev("q0", qty=0),
            ev("qn", qty=-5),
            ev("qt", qty="lots"),
            ev("ts", ts="yesterday"),
            ev("um", meter="nonexistent"),
        ]
        for raw in bad:
            result = engine.ingest(raw)
            self.assertEqual(result.status, "rejected", raw)
            self.assertTrue(result.reason)
        self.assertEqual(len(engine.ledger), 0)
        self.assertEqual(len(engine.dead_letter), len(bad))

    def test_parse_event_accepts_valid(self):
        parsed = parse_event(ev("ok1"))
        self.assertEqual(parsed.event_id, "ok1")
        self.assertEqual(parsed.quantity, 1000)


class TestLedgerDedupe(unittest.TestCase):
    def test_duplicate_event_id_lands_once(self):
        engine = make_engine()
        self.assertEqual(engine.ingest(ev("e1")).status, "accepted")
        self.assertEqual(engine.ingest(ev("e1")).status, "duplicate")
        self.assertEqual(len(engine.ledger), 1)

    def test_replay_never_changes_invoice(self):
        engine = make_engine()
        log = [ev(f"e{i}", qty=1000 * i, ts=NOV1 + i) for i in range(1, 50)]
        for raw in log:
            engine.ingest(raw)
        before = engine.invoice("c_1", "2025-11")
        for raw in log:  # full replay
            self.assertEqual(engine.ingest(raw).status, "duplicate")
        after = engine.invoice("c_1", "2025-11")
        self.assertEqual(before, after)


class TestPricing(unittest.TestCase):
    def test_per_unit_math(self):
        # 2.5M tokens at $2.50 / 1M = $6.25
        engine = make_engine()
        engine.ingest(ev("e1", qty=2_500_000))
        inv = engine.invoice("c_1", "2025-11")
        self.assertEqual(inv.lines[0].amount_micro, 625 * MICRO)

    def test_graduated_tiers(self):
        # 15M output tokens: first 10M at $10/1M, next 5M at $8/1M = $140
        engine = make_engine()
        engine.ingest(ev("e1", meter="output_tokens", qty=15_000_000))
        inv = engine.invoice("c_1", "2025-11")
        self.assertEqual(inv.lines[0].amount_micro, 14_000 * MICRO)

    def test_tiers_reset_each_period(self):
        engine = make_engine()
        engine.ingest(ev("e1", meter="output_tokens", qty=9_000_000, ts=NOV1))
        engine.ingest(ev("e2", meter="output_tokens", qty=9_000_000, ts=DEC1))
        nov = engine.invoice("c_1", "2025-11")
        dec = engine.invoice("c_1", "2025-12")
        # each month stays entirely in tier 1: 9M * $10/1M = $90
        self.assertEqual(nov.lines[0].amount_micro, 9_000 * MICRO)
        self.assertEqual(dec.lines[0].amount_micro, 9_000 * MICRO)

    def test_tiered_requires_open_final_tier(self):
        with self.assertRaises(ValueError):
            Tiered([(100, 5)])


class TestCreditBurndown(unittest.TestCase):
    def test_credits_burn_before_billing(self):
        engine = make_engine()
        engine.add_credits("c_1", 1000)  # $10.00
        engine.ingest(ev("e1", qty=1_600_000))  # $4.00
        self.assertEqual(engine.balance_cents("c_1", now=NOV1 + 1), 600)
        inv = engine.invoice("c_1", "2025-11")
        self.assertEqual(inv.credits_applied_micro, 400 * MICRO)
        self.assertEqual(inv.amount_due_micro, 0)

    def test_overage_bills_after_credits_run_out(self):
        engine = make_engine()
        engine.add_credits("c_1", 400)
        engine.ingest(ev("e1", qty=4_000_000))  # $10.00
        self.assertEqual(engine.balance_cents("c_1", now=NOV1 + 1), 0)
        inv = engine.invoice("c_1", "2025-11")
        self.assertEqual(inv.amount_due_micro, 600 * MICRO)

    def test_expired_grants_do_not_burn(self):
        engine = make_engine()
        engine.add_credits("c_1", 1000, expires_at=NOV1 + 5)
        engine.ingest(ev("e1", qty=400_000, ts=NOV1 + 10))  # $1.00 after expiry
        inv = engine.invoice("c_1", "2025-11")
        self.assertEqual(inv.credits_applied_micro, 0)
        self.assertEqual(inv.amount_due_micro, 100 * MICRO)

    def test_earliest_expiry_burns_first(self):
        engine = make_engine()
        soon = engine.add_credits("c_1", 500, expires_at=NOV1 + 100)
        later = engine.add_credits("c_1", 500, expires_at=NOV1 + 10_000)
        engine.ingest(ev("e1", qty=1_200_000, ts=NOV1))  # $3.00
        self.assertEqual(soon.remaining_micro, 200 * MICRO)
        self.assertEqual(later.remaining_micro, 500 * MICRO)


class TestLateEventsAndPeriods(unittest.TestCase):
    def test_late_event_lands_in_its_own_period(self):
        engine = make_engine()
        engine.ingest(ev("e1", qty=1_000_000, ts=NOV1))
        oct_invoice_before = engine.invoice("c_1", "2025-10")
        self.assertEqual(oct_invoice_before.gross_micro, 0)
        # an October event delivered in November still bills October
        engine.ingest(ev("late1", qty=2_000_000, ts=OCT15))
        oct_invoice_after = engine.invoice("c_1", "2025-10")
        self.assertEqual(oct_invoice_after.gross_micro, 500 * MICRO)
        nov_invoice = engine.invoice("c_1", "2025-11")
        self.assertEqual(nov_invoice.gross_micro, 250 * MICRO)

    def test_period_helper(self):
        self.assertEqual(period_of(NOV1), "2025-11")
        self.assertEqual(period_of(OCT15), "2025-10")


class TestReconcile(unittest.TestCase):
    def test_clean_engine_reports_no_drift(self):
        engine = make_engine()
        for i in range(100):
            engine.ingest(ev(f"e{i}", qty=10_000, ts=NOV1 + i))
        report = engine.reconcile()
        self.assertTrue(report["matched"], report)
        self.assertEqual(report["events_replayed"], 100)

    def test_reconcile_finds_events_missing_from_ledger(self):
        engine = make_engine()
        producer_log = [ev(f"e{i}", qty=10_000, ts=NOV1 + i) for i in range(20)]
        for raw in producer_log[:18]:  # stream "loses" the last two
            engine.ingest(raw)
        report = engine.reconcile(external_events=producer_log)
        self.assertEqual(sorted(report["missing_from_ledger"]), ["e18", "e19"])
        # re-ingest the missing events and the totals match a full run
        engine.ingest(producer_log[18])
        engine.ingest(producer_log[19])
        full = make_engine()
        for raw in producer_log:
            full.ingest(raw)
        self.assertEqual(engine.invoice("c_1", "2025-11"), full.invoice("c_1", "2025-11"))

    def test_invoice_equals_live_accrual(self):
        engine = make_engine()
        engine.add_credits("c_1", 700)
        for i in range(30):
            engine.ingest(ev(f"i{i}", qty=250_000, ts=NOV1 + i * 60))
            engine.ingest(ev(f"o{i}", meter="output_tokens", qty=90_000, ts=NOV1 + i * 60))
        inv = engine.invoice("c_1", "2025-11")
        self.assertEqual(inv.gross_micro, inv.credits_applied_micro + inv.amount_due_micro)
        self.assertEqual(engine.balance_cents("c_1", now=NOV1 + 3600), 0)


if __name__ == "__main__":
    unittest.main()
