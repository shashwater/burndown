"""Minimal stdlib HTTP front door, mirroring the async-ingest pattern:

POST /v1/events   one event or a list; validated statelessly, then applied.
                  Per-event results come back in the response, and rejected
                  events land on the dead-letter list (the part a real
                  deployment would page a webhook about).
GET  /v1/balance?customer=c_1001        live prepaid credit balance
GET  /v1/invoice?customer=c_1001&period=2026-11

Run: python -m burndown.server --port 8080
"""
from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .engine import Engine
from .pricing import PerUnit, Tiered


def build_engine() -> Engine:
    engine = Engine()
    engine.add_meter("input_tokens", PerUnit(cents_per_million=250))
    engine.add_meter("output_tokens", Tiered([(10_000_000, 1000), (None, 800)]))
    return engine


def make_handler(engine: Engine):
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def _json(self, code: int, payload) -> None:
            body = json.dumps(payload, indent=2).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # keep the demo quiet
            pass

        def do_POST(self) -> None:
            if urlparse(self.path).path != "/v1/events":
                return self._json(404, {"error": "not found"})
            try:
                raw = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            except (ValueError, TypeError):
                return self._json(400, {"error": "body must be JSON"})
            events = raw if isinstance(raw, list) else [raw]
            with lock:
                results = [engine.ingest(e).__dict__ for e in events]
            self._json(202, {"results": results})

        def do_GET(self) -> None:
            url = urlparse(self.path)
            qs = parse_qs(url.query)
            customer = (qs.get("customer") or [""])[0]
            if url.path == "/v1/balance":
                if not customer:
                    return self._json(400, {"error": "customer is required"})
                with lock:
                    balance = engine.balance_cents(customer)
                return self._json(200, {"customer": customer, "credit_balance_cents": balance})
            if url.path == "/v1/invoice":
                period = (qs.get("period") or [""])[0]
                if not customer or not period:
                    return self._json(400, {"error": "customer and period are required"})
                with lock:
                    inv = engine.invoice(customer, period)
                return self._json(
                    200,
                    {
                        "customer": inv.customer_id,
                        "period": inv.period,
                        "lines": [line.__dict__ for line in inv.lines],
                        "credits_applied_micro": inv.credits_applied_micro,
                        "amount_due_micro": inv.amount_due_micro,
                        "event_count": inv.event_count,
                    },
                )
            self._json(404, {"error": "not found"})

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="burndown ingest server")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(build_engine()))
    print(f"burndown listening on http://127.0.0.1:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
