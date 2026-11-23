"""End-to-end demo: one month of AI API traffic through the billing engine.

Simulates the messy parts of a real usage pipeline - duplicate deliveries,
late-arriving events, invalid payloads, and a lost-event recovery - and shows
the invoice surviving all of them. Deterministic (seeded), exits nonzero if
any invariant breaks.

Run: python demo.py
"""
import random
import sys

from burndown import Engine, PerUnit, Tiered, MICRO

NOV1 = 1761955200.0  # 2025-11-01 00:00:00 UTC
DAY = 86400.0
PERIOD = "2025-11"


def dollars(micro):
    return f"${micro / (100 * MICRO):,.2f}"


def build_engine():
    engine = Engine()
    # input: $2.50 / 1M tokens; output: $10 / 1M for the first 10M, then $8
    engine.add_meter("input_tokens", PerUnit(cents_per_million=250))
    engine.add_meter("output_tokens", Tiered([(10_000_000, 1000), (None, 800)]))
    return engine


def generate_month(rng):
    """One month of usage events for three customers, as raw payloads."""
    events = []
    seq = 0
    for cust, weight in (("c_1001", 1.0), ("c_1002", 2.5), ("c_1003", 0.4)):
        for day in range(30):
            for _ in range(rng.randint(3, 8)):
                seq += 1
                ts = NOV1 + day * DAY + rng.uniform(0, DAY)
                events.append({
                    "event_id": f"ev_{cust}_{seq}",
                    "customer_id": cust,
                    "meter": "input_tokens",
                    "quantity": int(rng.uniform(20_000, 400_000) * weight),
                    "timestamp": ts,
                })
                seq += 1
                events.append({
                    "event_id": f"ev_{cust}_{seq}",
                    "customer_id": cust,
                    "meter": "output_tokens",
                    "quantity": int(rng.uniform(5_000, 120_000) * weight),
                    "timestamp": ts,
                })
    return events


def main():
    rng = random.Random(20251101)
    engine = build_engine()

    print("=" * 68)
    print("burndown demo: one month of usage-based billing for an AI API")
    print("=" * 68)

    # Prepaid credits: c_1001 has $25 expiring mid-month and $50 that doesn't.
    engine.add_credits("c_1001", 2500, expires_at=NOV1 + 15 * DAY, grant_id="cg_promo")
    engine.add_credits("c_1001", 5000, grant_id="cg_prepay")
    engine.add_credits("c_1003", 10000, grant_id="cg_enterprise")

    producer_log = generate_month(rng)
    duplicates = rng.sample(producer_log, k=len(producer_log) // 20)
    late = rng.sample(producer_log, k=len(producer_log) // 50)
    late_ids = {e["event_id"] for e in late}
    bad_payloads = [
        {"event_id": "broken_1", "customer_id": "c_1001", "meter": "input_tokens",
         "quantity": -50, "timestamp": NOV1},
        {"event_id": "broken_2", "customer_id": "c_1001", "meter": "input_tokens",
         "quantity": 100},  # no timestamp
    ]

    print(f"\n[1] Ingesting {len(producer_log)} events "
          f"(+{len(duplicates)} duplicate deliveries, +{len(bad_payloads)} invalid payloads)")
    on_time = [e for e in producer_log if e["event_id"] not in late_ids]
    results = {"accepted": 0, "duplicate": 0, "rejected": 0}
    for raw in on_time + duplicates + bad_payloads:
        results[engine.ingest(raw).status] += 1
    print(f"    accepted={results['accepted']}  duplicate={results['duplicate']}  "
          f"rejected={results['rejected']} (dead-lettered with reasons)")
    for raw, reason in engine.dead_letter:
        print(f"      dead-letter: {raw['event_id']}: {reason}")

    print("\n[2] Real-time credit burndown for c_1001 (prepaid $75.00):")
    for day in (10, 20, 29):
        now = NOV1 + day * DAY
        print(f"    day {day:>2}: credit balance {engine.balance_cents('c_1001', now=now) / 100:>8,.2f} USD")
    print("    (the $25 promo grant expires day 15 and stops burning; "
          "the $50 grant picks up)")

    print(f"\n[3] Month close - invoices for {PERIOD}:")
    for cust in ("c_1001", "c_1002", "c_1003"):
        print(engine.invoice(cust, PERIOD).render())
        print()

    print(f"[4] {len(late)} events arrive late, after the month closed")
    for raw in late:
        engine.ingest(raw)
    replayed = [dict(e) for e in producer_log]  # full producer replay
    dupes = sum(1 for e in replayed if engine.ingest(e).status == "duplicate")
    print(f"    full producer replay: {dupes}/{len(replayed)} correctly ignored as duplicates")

    print("\n[5] Recovery drill: a second pipeline copy 'loses' 1% of the stream")
    shadow = build_engine()
    shadow.add_credits("c_1001", 2500, expires_at=NOV1 + 15 * DAY, grant_id="cg_promo")
    shadow.add_credits("c_1001", 5000, grant_id="cg_prepay")
    shadow.add_credits("c_1003", 10000, grant_id="cg_enterprise")
    drop = {e["event_id"] for e in rng.sample(producer_log, k=max(1, len(producer_log) // 100))}
    for raw in producer_log:
        if raw["event_id"] not in drop:
            shadow.ingest(raw)
    report = shadow.reconcile(external_events=producer_log)
    print(f"    reconcile found {len(report['missing_from_ledger'])} events missing "
          f"from the shadow ledger; re-ingesting them")
    for raw in producer_log:
        if raw["event_id"] in report["missing_from_ledger"]:
            shadow.ingest(raw)

    print("\n[6] Invariants")
    checks = []
    for cust in ("c_1001", "c_1002", "c_1003"):
        main_inv = engine.invoice(cust, PERIOD)
        shadow_inv = shadow.invoice(cust, PERIOD)
        checks.append((f"{cust}: invoice survives duplicates + late events "
                        f"and matches the recovered shadow pipeline",
                       main_inv == shadow_inv))
        checks.append((f"{cust}: gross == credits applied + amount due",
                       main_inv.gross_micro == main_inv.credits_applied_micro + main_inv.amount_due_micro))
    report = engine.reconcile()
    checks.append(("live accrual cache matches a full ledger replay", report["matched"]))
    final = engine.invoice("c_1001", PERIOD)
    print(f"\n    Final invoice for c_1001 after late events:\n")
    print(final.render())

    failed = [name for name, ok in checks if not ok]
    print()
    for name, ok in checks:
        print(f"    [{'PASS' if ok else 'FAIL'}] {name}")
    if failed:
        print(f"\n{len(failed)} invariant(s) failed")
        return 1
    print(f"\nAll {len(checks)} invariants hold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
