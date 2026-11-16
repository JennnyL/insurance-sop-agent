import json

import httpx
import pytest

from app.interpreter import ModelFailure
from evals.budget import BudgetTransport


def request(**overrides):
    return httpx.Request(
        "POST",
        "https://api.openai.com/v1/responses",
        json={
            "model": "gpt-4.1-mini",
            "max_output_tokens": 2400,
            "input": "synthetic",
            **overrides,
        },
    )


@pytest.mark.asyncio
async def test_budget_blocks_before_network_and_counts_physical_calls():
    seen = []

    def handler(req):
        seen.append(json.loads(req.content))
        return httpx.Response(200, json={"usage": {"input_tokens": 100, "output_tokens": 20}})

    transport = BudgetTransport(max_calls=1, delegate=httpx.MockTransport(handler))
    await transport.handle_async_request(request())
    with pytest.raises(ModelFailure, match="BUDGET_EXHAUSTED"):
        await transport.handle_async_request(request())
    assert len(seen) == 1 and seen[0]["service_tier"] == "default"
    assert transport.report()["estimated_usage_usd"] == 0.000072
    assert transport.report()["physical_requests"] == 1
    await transport.finish()


@pytest.mark.asyncio
async def test_dollar_limit_stops_first_request():
    def handler(req):
        pytest.fail("No network request is allowed")

    transport = BudgetTransport(limit_usd=0.00001, delegate=httpx.MockTransport(handler))
    with pytest.raises(ModelFailure, match="BUDGET_EXHAUSTED"):
        await transport.handle_async_request(request())
    assert not transport.receipts
    await transport.finish()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"model": "expensive-unknown"},
        {"max_output_tokens": 2401},
        {"tools": [{"type": "web_search"}]},
        {"input": "x" * 32000},
        {"service_tier": "priority"},
    ],
)
async def test_unbudgeted_request_is_blocked(overrides):
    def handler(req):
        pytest.fail("Blocked requests must never reach the provider")

    transport = BudgetTransport(delegate=httpx.MockTransport(handler))
    with pytest.raises(ModelFailure, match="REQUEST_BLOCKED"):
        await transport.handle_async_request(request(**overrides))
    assert not transport.receipts
    await transport.finish()


@pytest.mark.asyncio
async def test_unknown_usage_keeps_reservation_and_excludes_error_text():
    transport = BudgetTransport(
        delegate=httpx.MockTransport(
            lambda req: httpx.Response(
                429,
                json={
                    "error": {
                        "code": "insufficient_quota",
                        "type": "insufficient_quota",
                        "message": "PRIVATE",
                    },
                },
            )
        )
    )
    await transport.handle_async_request(request())
    report = transport.report()
    assert report["usage_unknown_requests"] == 1
    assert report["conservative_reserved_usd"] > 0
    assert "PRIVATE" not in json.dumps(report)
    await transport.finish()


@pytest.mark.asyncio
async def test_persistent_preview_budget_reserved_before_network_and_survives_restart(tmp_path):
    ledger = tmp_path / "spend.json"

    def handler(req):
        saved = json.loads(ledger.read_text())
        assert saved["physical_requests"] == 1
        assert saved["conservative_reserved_usd"] > 0
        return httpx.Response(200, json={"usage": {"input_tokens": 100, "output_tokens": 20}})

    first = BudgetTransport(max_calls=1, delegate=httpx.MockTransport(handler), ledger_path=ledger)
    await first.handle_async_request(request())
    await first.finish()
    second = BudgetTransport(max_calls=1, delegate=httpx.MockTransport(handler), ledger_path=ledger)
    with pytest.raises(ModelFailure, match="BUDGET_EXHAUSTED"):
        await second.handle_async_request(request())
    assert second.report()["estimated_usage_usd"] == 0.000072
    await second.finish()


def test_local_limit_is_explained_without_losing_verified_progress(verified, app):
    async def limited(*args, **kwargs):
        raise ModelFailure("MODEL_EVAL_BUDGET_EXHAUSTED")

    app.state.engine.interpreter.observe = limited
    case_id = verified.state["selected_case_id"]
    verified.say("Why was this claim denied?")
    # 1.7.4: a spent budget falls back to the scripted parser and grounded templates.
    assert "local API spending limit" in verified.reply
    assert "did not pass validation" not in verified.reply
    assert verified.state["verified"] and verified.state["selected_case_id"] == case_id
    assert "pathology" in verified.reply.lower()
