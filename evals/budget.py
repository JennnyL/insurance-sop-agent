"""Per-run, fail-closed spend guard for synthetic live evaluation only."""

import json
import re
from pathlib import Path

import httpx

from app.interpreter import ModelFailure

PRICE_SOURCE = "https://developers.openai.com/api/docs/models/gpt-4.1-mini"


class BudgetTransport(httpx.AsyncBaseTransport):
    # Standard pricing checked 2026-11-14; cached input conservatively full price.
    INPUT_RATE = 0.40 / 1_000_000
    OUTPUT_RATE = 1.60 / 1_000_000

    def __init__(self, limit_usd=1.0, max_calls=60, delegate=None, ledger_path=None):
        self.limit_usd = limit_usd
        self.max_calls = max_calls
        self.reserved_usd = 0.0
        self.receipts = []
        self.delegate = delegate or httpx.AsyncHTTPTransport(retries=0)
        self.ledger_path = Path(ledger_path) if ledger_path else None
        if self.ledger_path and self.ledger_path.exists():
            saved = json.loads(self.ledger_path.read_text())
            if saved["limit_usd"] != limit_usd or saved["maximum_physical_requests"] != max_calls:
                raise ValueError("Existing preview ledger budget must not change")
            self.reserved_usd = saved["conservative_reserved_usd"]
            self.receipts = saved["receipts"]

    def persist(self):
        if self.ledger_path:
            self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.ledger_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.report(), indent=2) + "\n")
            temporary.replace(self.ledger_path)

    async def handle_async_request(self, request):
        body = json.loads(request.content)
        if (
            str(request.url) != "https://api.openai.com/v1/responses"
            or request.method != "POST"
            or body.get("model") != "gpt-4.1-mini"
            or body.get("tools")
            or body.get("service_tier", "default") != "default"
            or not 1 <= body.get("max_output_tokens", 0) <= 2400
            or len(request.content) > 32_000
        ):
            raise ModelFailure("MODEL_EVAL_REQUEST_BLOCKED")
        # One token per serialized UTF-8 byte, plus generous protocol overhead.
        # Reserve full output cap, including failed, unknown and retried requests.
        reserve = (len(request.content) + 8192) * self.INPUT_RATE
        reserve += body["max_output_tokens"] * self.OUTPUT_RATE
        if len(self.receipts) >= self.max_calls or self.reserved_usd + reserve > self.limit_usd:
            raise ModelFailure("MODEL_EVAL_BUDGET_EXHAUSTED")
        self.reserved_usd += reserve
        receipt = {"index": len(self.receipts) + 1, "reserved_usd": round(reserve, 8)}
        self.receipts.append(receipt)
        # Reserve durably before network I/O when used by the bounded preview.
        self.persist()
        body["service_tier"] = "default"
        headers = dict(request.headers)
        headers.pop("content-length", None)
        outgoing = httpx.Request(
            request.method, request.url, headers=headers, json=body, extensions=request.extensions
        )
        try:
            response = await self.delegate.handle_async_request(outgoing)
            await response.aread()
            receipt["http_status"] = response.status_code
            payload = response.json()
            usage = payload.get("usage") or {}
            if all(isinstance(usage.get(k), int) for k in ("input_tokens", "output_tokens")):
                receipt["input_tokens"] = usage["input_tokens"]
                receipt["output_tokens"] = usage["output_tokens"]
                receipt["estimated_usd"] = round(
                    usage["input_tokens"] * self.INPUT_RATE + usage["output_tokens"] * self.OUTPUT_RATE, 8
                )
            else:
                receipt["usage_unknown"] = True
            for key in ("code", "type"):
                value = (payload.get("error") or {}).get(key)
                if isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", value):
                    receipt["error_" + key] = value
            return response
        except BaseException:
            receipt["usage_unknown"] = True
            raise
        finally:
            self.persist()

    async def aclose(self):
        # Application opens a client per turn; this ledger spans the whole run.
        pass

    async def finish(self):
        await self.delegate.aclose()

    def report(self):
        return {
            "limit_usd": self.limit_usd,
            "maximum_physical_requests": self.max_calls,
            "physical_requests": len(self.receipts),
            "price_source": PRICE_SOURCE,
            "price_checked_date": "2026-11-14",
            "input_usd_per_million": 0.40,
            "output_usd_per_million": 1.60,
            "input_tokens": sum(r.get("input_tokens", 0) for r in self.receipts),
            "output_tokens": sum(r.get("output_tokens", 0) for r in self.receipts),
            "usage_unknown_requests": sum(bool(r.get("usage_unknown")) for r in self.receipts),
            "estimated_usage_usd": round(sum(r.get("estimated_usd", 0) for r in self.receipts), 8),
            "conservative_reserved_usd": round(self.reserved_usd, 8),
            "note": "Per-run guard; reservations are not refunded. Estimates are not billing receipts.",
            "receipts": self.receipts,
        }
