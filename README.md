# burndown

A usage-based billing engine for AI APIs, built to study how Stripe-style
usage billing actually works: idempotent event ingestion, a deduplicated
revenue ledger, meters, graduated pricing, prepaid credit burndown, and
invoices that survive duplicate, late, and replayed events.

Stripe published the architecture of their usage-based billing system
("How we built it: Usage-based billing", Jan 2025): stateless validation at
the edge, asynchronous ingest onto an event bus, an idempotent revenue
ledger, credit burndown pricing, and timestamp-based reconciliation for
delayed or duplicate events. burndown implements that model at toy scale in
dependency-free Python, with the correctness properties written as actual
testable invariants.

## What it does

- **Idempotent ledger** - every usage event carries an idempotency key and
  each id lands exactly once, so producer retries and full-stream replays
  never change an invoice. Reusing an id with a *different* payload is
  reported as a conflict (first event kept), never silently absorbed.
- **Meters and pricing** - per-unit or graduated-tier pricing per meter,
  with tiers resetting each billing period. Prices are cents per million
  units, the way LLM APIs price tokens ($2.50 / 1M input tokens).
- **Credit burndown** - prepaid credit grants drain in real time,
  earliest-expiring first; expired grants stop burning, and usage past the
  balance bills as overage. `balance_cents()` is the live balance.
- **Invoices from replay** - `invoice()` is a pure function of the ledger,
  so a late event lands in its own billing period automatically and a
  regenerated invoice is always current.
- **Reconciliation** - `reconcile()` rebuilds all live state from the
  ledger and reports drift; handed the producer's copy of the stream, it
  reports exactly which event ids were lost so they can be re-ingested.
- **Dead-letter with reasons** - invalid payloads are rejected with a
  reason, never silently dropped (the thing a real deployment would page a
  webhook about).
- **HTTP front door** - optional stdlib server: `POST /v1/events`,
  `GET /v1/balance`, `GET /v1/invoice`.

## Quickstart

```
python demo.py                          # a month of AI API traffic, end to end
python bench.py --events 200000         # throughput micro-benchmark
python -m unittest discover -s tests    # 29 tests
python -m burndown.server --port 8080   # ingest API
```

No dependencies. Python 3.10+.

## What the demo shows

Three customers generate a month of token traffic. Along the way:

- 5% of events are delivered twice - all correctly ignored as duplicates
- invalid payloads are dead-lettered with reasons
- a customer's prepaid credits drain day by day, with a mid-month expiring
  promo grant handing off to a no-expiry grant
- 2% of events arrive *after* month close and still land in the correct
  billing period
- a second pipeline copy "loses" 1% of the stream; reconcile finds the
  missing events and the recovered pipeline's invoices match exactly

```
[6] Invariants
    [PASS] c_1001: invoice survives duplicates + late events and matches the recovered shadow pipeline
    [PASS] c_1001: gross == credits applied + amount due
    [PASS] c_1002: invoice survives duplicates + late events and matches the recovered shadow pipeline
    [PASS] c_1002: gross == credits applied + amount due
    [PASS] c_1003: invoice survives duplicates + late events and matches the recovered shadow pipeline
    [PASS] c_1003: gross == credits applied + amount due
    [PASS] live accrual cache matches a full ledger replay
```

## Measured throughput

`bench.py`, single-threaded and in-memory on a shared 2-vCPU cloud
container:

- ~200k events/s through ingest + accrual (173k-210k across runs at 200k events; ~187k/s at 1M)
- full-ledger reconcile of 200k events: 0.42s

This is a correctness study, not a load test; the number just shows the
accounting work itself is cheap.

## Design notes

Money is integer microcents (1 cent = 10^6) everywhere; prices are cents
per million units, so `quantity * cents_per_million` is exact integer math
and rounding only happens once, at display.

Live state (balances, accrual) is an incremental cache; the ledger is the
source of truth, and invoices are computed as a pure replay over it.

Out-of-order delivery is handled by re-anchoring: when a customer with
credit grants accepts an event older than one already applied, the engine
replays that customer's history in event-time order. This matters because
with graduated pricing an event's marginal cost depends on accumulation
order, and grant eligibility depends on event time - so arrival order could
otherwise change which grant a charge burns from. Customers without grants
are unaffected by ordering and skip the replay, so in-order streams never
pay for it.

Tested behavior (not a proof): hand-computed scenarios whose expected
credit assignment was worked out independently of the engine - including
same-month events straddling a grant's expiry - plus 75 randomized shuffled
interleavings across customers, months, and expiring grants, all matching
timestamp-ordered ingestion exactly; and `reconcile()`, a full audit
rebuild from the ledger, reporting no drift on every suite run. Given the
producer's copy of the stream, `reconcile()` also reports exactly which
event ids were lost. This mirrors the split Stripe describes between
real-time ratings and reconciliation.

What this deliberately does **not** implement: the distributed
infrastructure from the Stripe post - no event bus, no stream processor, no
active-active regions. The HTTP server validates and applies synchronously.
What carries over is the correctness model: stateless validation,
idempotent ingest, replay-derived truth, timestamp-based reconciliation.

## Limits

In-memory, single-node, calendar-month (UTC) billing periods. The point is
the correctness model - dedupe, replay safety, burndown accounting - not
distributed scale.

## License

MIT
