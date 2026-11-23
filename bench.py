"""Throughput micro-benchmark: how fast does the ingest + accrual path run
single-threaded, in memory, on this machine? Run: python bench.py [--events N]
"""
import argparse
import random
import time

from burndown import Engine, PerUnit, Tiered


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--events", type=int, default=200_000)
    args = parser.parse_args()

    rng = random.Random(7)
    engine = Engine()
    engine.add_meter("input_tokens", PerUnit(cents_per_million=250))
    engine.add_meter("output_tokens", Tiered([(10_000_000, 1000), (None, 800)]))
    engine.add_credits("c_bench", 5_000_000)

    log = [
        {
            "event_id": f"bench_{i}",
            "customer_id": "c_bench",
            "meter": "input_tokens" if i % 2 == 0 else "output_tokens",
            "quantity": rng.randint(1_000, 500_000),
            "timestamp": 1761955200.0 + i,
        }
        for i in range(args.events)
    ]

    start = time.perf_counter()
    for raw in log:
        engine.ingest(raw)
    ingest_s = time.perf_counter() - start

    start = time.perf_counter()
    invoice = engine.invoice("c_bench", "2025-11")
    invoice_ms = (time.perf_counter() - start) * 1000

    start = time.perf_counter()
    report = engine.reconcile()
    reconcile_s = time.perf_counter() - start

    print(f"events ingested : {args.events:,}")
    print(f"ingest          : {ingest_s:.2f}s  ({args.events / ingest_s:,.0f} events/s)")
    print(f"invoice         : {invoice_ms:.1f}ms for one customer-month")
    print(f"full reconcile  : {reconcile_s:.2f}s  (matched={report['matched']})")
    print(f"ledger size     : {len(engine.ledger):,} events")
    print(f"invoice due     : ${invoice.amount_due_micro / 1e8:,.2f}")


if __name__ == "__main__":
    main()
