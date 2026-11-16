import asyncio
import json
import time

import httpx
import pytest
from conftest import SAMPLE
from fastapi.testclient import TestClient

from app.config import Settings
from app.interpreter import ModelFailure, ModelInterpreter, OfflineInterpreter
from app.schemas import TurnRequest, empty_observation
from app.store import Conflict, Store


def owner(client, store):
    return store.fingerprint(client.cookies.get("sop_principal"))


def test_session_cookie_and_cross_session_isolation(app, client, chat):
    other = TestClient(app)
    response = other.get("/v1/sessions/" + chat.state["session_id"])
    assert response.status_code == 401
    other.post("/v1/sessions")
    assert other.get("/v1/sessions/" + chat.state["session_id"]).status_code == 404
    assert other.get("/v1/sessions/" + chat.state["session_id"] + "/trace").status_code == 404
    assert "HttpOnly" in client.post("/v1/sessions").headers["set-cookie"]


def test_browser_cannot_supply_authority(client, chat):
    response = client.post(
        "/v1/sessions/" + chat.state["session_id"] + "/messages",
        json={
            "message_id": "attack",
            "expected_version": 0,
            "text": "hi",
            "verified": True,
            "phase": "PROCESS_CASE",
        },
    )
    assert response.status_code == 422
    assert "hi" not in response.text
    assert client.post("/v1/sessions", headers={"Origin": "https://attacker.example"}).status_code == 403


def test_idempotency_and_conflict(client, chat):
    url = "/v1/sessions/" + chat.state["session_id"] + "/messages"
    payload = {"message_id": "same", "expected_version": 0, "text": SAMPLE}
    result = client.post(url, json=payload)
    assert result.status_code == 200 and result.json()["verified"]
    duplicate = client.post(url, json=payload)
    assert duplicate.status_code == 200
    assert duplicate.json()["version"] == result.json()["version"]
    assert client.post(url, json={**payload, "text": "modified"}).status_code == 409
    assert client.post(url, json={**payload, "message_id": "different"}).json()["error"] == "VERSION_CONFLICT"
    events = client.get("/v1/sessions/" + chat.state["session_id"] + "/trace").json()["events"]
    assert sum(e["kind"] == "verification.passed" for e in events) == 1


def test_restart_preserves_session_and_pending_candidates(app, chat, client):
    chat.say("My name is Margaret Chen. My denied healthcare claim from January.")
    new_store = Store(app.state.settings)
    s = new_store.load(chat.state["session_id"], owner(client, new_store))
    assert s["identity"]["name"]["value"] == "margaret chen"
    assert s["hints"]["month"]["value"] == "1"
    # Persistent bytes are encrypted; traces contain no candidate values.
    assert b"Margaret Chen" not in new_store.path.read_bytes()
    assert b"margaret chen" not in new_store.path.read_bytes()


def test_auth_expiry_hides_history_and_outbox(verified, app, client):
    verified.say(action="finish")
    verified.say("yes")
    store = app.state.store
    with store.tx() as db:
        row = db.execute("SELECT state FROM sessions WHERE id=?", (verified.state["session_id"],)).fetchone()
        s = store.unseal(row["state"])
        s["auth"]["expires_at"] = time.time() - 1
        db.execute(
            "UPDATE authorizations SET payload=? WHERE owner=(SELECT owner FROM sessions WHERE id=?)",
            (store.seal(s["auth"]), s["session_id"]),
        )
        db.execute("UPDATE sessions SET state=? WHERE id=?", (store.seal(s), s["session_id"]))
    verified.refresh()
    assert not verified.state["verified"] and verified.state["phase"] == "VERIFY_ID"
    assert "pathology" not in json.dumps(verified.state)
    with store.tx() as db:
        assert db.execute("SELECT status FROM outbox").fetchone()[0] == "CANCELLED"
    trace = verified.trace()
    assert not any("case_id" in e["details"] or "references" in e["details"] for e in trace)


def test_offer_expiry_requires_new_offer(verified, app):
    verified.say(action="finish")
    store = app.state.store
    with store.tx() as db:
        row = db.execute("SELECT state FROM sessions WHERE id=?", (verified.state["session_id"],)).fetchone()
        s = store.unseal(row["state"])
        s["draft"]["expires_at"] = time.time() - 1
        db.execute("UPDATE sessions SET state=? WHERE id=?", (store.seal(s), s["session_id"]))
    old = verified.state["draft"]["id"]
    verified.refresh()
    url = "/v1/sessions/" + verified.state["session_id"] + "/messages"
    r = verified.client.post(
        url,
        json={
            "message_id": "expired",
            "expected_version": verified.state["version"],
            "action": "email_send",
            "offer_id": old,
        },
    )
    assert r.status_code == 409 and r.json()["error"] == "OFFER_EXPIRED"
    verified.refresh()
    verified.say(action="finish")
    assert verified.state["draft"]["id"] != old


def test_pii_and_secret_not_in_public_projection(verified, app):
    result = json.dumps(verified.state) + json.dumps(verified.trace())
    for value in ("4472", "1985-03-15", "margaret@email.com", "+16505212836"):
        assert value not in result
    app.state.settings.model_api_key = "test-secret-canary"
    assert "test-secret-canary" not in verified.client.get("/v1/config").text
    assert "test-secret-canary" not in verified.client.get("/healthz").text


async def test_reset_fences_inflight_model(app):
    started, released = asyncio.Event(), asyncio.Event()

    class Waiting:
        async def observe(self, text, context):
            started.set()
            await released.wait()
            return await OfflineInterpreter().observe(text, context)

    engine = app.state.engine
    engine.interpreter = Waiting()
    state = app.state.store.create("owner")
    request = TurnRequest(message_id="old", expected_version=0, text=SAMPLE)
    task = asyncio.create_task(engine.turn(state["session_id"], "owner", request))
    await started.wait()
    app.state.store.control(
        state["session_id"], "owner", TurnRequest(message_id="reset", expected_version=0, action="reset")
    )
    released.set()
    with pytest.raises(Conflict):
        await task
    result = app.state.store.load(state["session_id"], "owner")
    assert result["phase"] == "VERIFY_ID" and result["auth"] is None


def test_database_lease_rejects_concurrent_turn(app):
    store = app.state.store
    s = store.create("owner")
    first = TurnRequest(message_id="one", expected_version=0, text="hi")
    state, turn, _ = store.begin(s["session_id"], "owner", first)
    with pytest.raises(Conflict, match="TURN_IN_PROGRESS"):
        store.begin(
            s["session_id"], "owner", TurnRequest(message_id="two", expected_version=0, text="hi again")
        )
    store.commit(state, turn, [], final=True)


def test_crashed_lease_can_resume_without_double_verification(app):
    store = app.state.store
    s = store.create("owner")
    request = TurnRequest(message_id="same", expected_version=0, text="hi")
    state, turn, _ = store.begin(s["session_id"], "owner", request)
    with store.tx() as db:
        db.execute("UPDATE sessions SET lease_until=0 WHERE id=?", (s["session_id"],))
    resumed, resumed_turn, replay = store.begin(s["session_id"], "owner", request)
    assert resumed_turn == turn and not replay
    store.commit(resumed, resumed_turn, [], final=True)
    assert store.begin(s["session_id"], "owner", request)[2] is True


async def test_claim_failure_keeps_committed_grant(app):
    engine = app.state.engine
    store = app.state.store
    s = store.create("owner")
    original = engine.gateway.find_cases

    def fail(*args):
        raise RuntimeError("injected service failure")

    engine.gateway.find_cases = fail
    s = await engine.turn(
        s["session_id"], "owner", TurnRequest(message_id="first", expected_version=0, text=SAMPLE)
    )
    assert s["auth"] and s["phase"] == "RESOLVE_INTENT"
    assert s["hints"]["month"]["value"] == "1"
    engine.gateway.find_cases = original
    s = await engine.turn(
        s["session_id"],
        "owner",
        TurnRequest(message_id="retry", expected_version=s["version"], text="continue"),
    )
    assert s["selected_case_id"] == "CL-2048"


async def test_model_request_wire_schema_no_fixture_leak(tmp_path):
    captured = []

    def respond(request):
        payload = json.loads(request.content)
        captured.append(payload)
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "usage": {"input_tokens": 10, "output_tokens": 20},
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": empty_observation().model_dump_json()}],
                    }
                ],
            },
        )

    settings = Settings(data_dir=tmp_path, model_mode="openai", model_api_key="synthetic-token")
    obs, stats = await ModelInterpreter(settings, httpx.MockTransport(respond)).observe(
        "hello", {"phase": "VERIFY_ID"}
    )
    assert obs.scope == "in_scope" and stats["calls"] == 1
    payload = captured[0]
    assert payload["store"] is False
    schema = payload["text"]["format"]["schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    assert "policyholders.json" not in json.dumps(payload) and "CL-2048" not in json.dumps(payload)
    assert "synthetic-token" not in json.dumps(payload)


@pytest.mark.parametrize("mode", ["refusal", "extra_authority", "invalid", "incomplete", "http_error"])
async def test_model_bad_responses_fail_closed(tmp_path, mode):
    calls = []

    def respond(request):
        calls.append(1)
        if mode == "http_error":
            return httpx.Response(401, json={"error": {"message": "not exposed"}})
        if mode == "refusal":
            blocks = [{"type": "refusal", "refusal": "No"}]
        else:
            blocks = [
                {
                    "type": "output_text",
                    "text": json.dumps({**empty_observation().model_dump(), "verified": True})
                    if mode == "extra_authority"
                    else "{}"
                    if mode == "invalid"
                    else empty_observation().model_dump_json(),
                }
            ]
        return httpx.Response(
            200,
            json={
                "status": "incomplete" if mode == "incomplete" else "completed",
                "output": [{"content": blocks}],
            },
        )

    settings = Settings(data_dir=tmp_path, model_mode="openai", model_api_key="synthetic-token")
    with pytest.raises(ModelFailure):
        await ModelInterpreter(settings, httpx.MockTransport(respond)).observe("hello", {})
    assert len(calls) <= 2


def test_session_retention_deletes_data(app):
    store = app.state.store
    s = store.create("owner")
    with store.tx() as db:
        db.execute("UPDATE sessions SET created=? WHERE id=?", (time.time() - 90000, s["session_id"]))
    store.cleanup()
    with pytest.raises(Conflict):
        store.load(s["session_id"], "owner")


@pytest.mark.parametrize("mistake", ["occurrence", "label_in_quote", "stale_hint"])
async def test_model_repairs_mechanical_source_errors_without_relaxing_gate(tmp_path, mistake):
    from app.schemas import Hint, IdentityObservation

    text = "My name is Alex Rivera. SSN last four is 1234."
    good = empty_observation(
        identity=[
            IdentityObservation(
                field="name",
                value="Alex Rivera",
                quote="Alex Rivera",
                occurrence=1,
                subject="self",
                assertion="affirm",
            ),
            IdentityObservation(
                field="ssn_last4",
                value="1234",
                quote="1234",
                occurrence=1,
                subject="self",
                assertion="affirm",
            ),
        ]
    )
    bad = good.model_copy(deep=True)
    if mistake == "occurrence":
        bad.identity[1].occurrence = 2
    elif mistake == "label_in_quote":
        bad.identity[0].quote = "My name is Alex Rivera"
    else:
        bad.hints = [Hint(field="case_type", value="auto", quote="auto", occurrence=1, assertion="affirm")]
    captured = []

    def respond(request):
        captured.append(json.loads(request.content))
        obs = bad if len(captured) == 1 else good
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "usage": {"input_tokens": 100, "output_tokens": 50},
                "output": [{"content": [{"type": "output_text", "text": obs.model_dump_json()}]}],
            },
        )

    settings = Settings(data_dir=tmp_path, model_mode="openai", model_api_key="synthetic-token")
    observation, stats = await ModelInterpreter(settings, httpx.MockTransport(respond)).observe(text, {})
    assert observation == good
    if mistake == "stale_hint":
        assert stats["calls"] == 1 and stats["rejected_hint_sources"] == 1
        assert not observation.hints
    else:
        assert stats["calls"] == 2 and stats["input_tokens"] == 200 and stats["output_tokens"] == 100
        assert "failed mechanical source validation" in captured[1]["input"][0]["content"]


async def test_repeated_source_error_fails_closed(tmp_path):
    from app.schemas import IdentityObservation

    bad = empty_observation(
        identity=[
            IdentityObservation(
                field="name",
                value="Invented Person",
                quote="Invented Person",
                occurrence=1,
                subject="self",
                assertion="affirm",
            )
        ]
    )
    calls = []

    def respond(request):
        calls.append(1)
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {"content": [{"type": "output_text", "text": bad.model_dump_json()}]},
                ],
            },
        )

    settings = Settings(data_dir=tmp_path, model_mode="openai", model_api_key="synthetic-token")
    with pytest.raises(ModelFailure, match="MODEL_SOURCE_INVALID"):
        await ModelInterpreter(settings, httpx.MockTransport(respond)).observe("hello", {})
    assert len(calls) == 2


async def test_human_intent_action_conflict_gets_bounded_repair(tmp_path):
    calls = []

    def respond(request):
        calls.append(json.loads(request.content))
        observation = empty_observation(
            intents=["human_help"], action="continue" if len(calls) == 1 else "human"
        )
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {"content": [{"type": "output_text", "text": observation.model_dump_json()}]},
                ],
            },
        )

    settings = Settings(data_dir=tmp_path, model_mode="openai", model_api_key="synthetic-token")
    observation, stats = await ModelInterpreter(settings, httpx.MockTransport(respond)).observe(
        "I want to speak with a human representative.", {}
    )
    assert observation.action == "human" and stats["calls"] == 2
    assert "human_help intent conflicts" in calls[1]["input"][0]["content"]
