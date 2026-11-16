import asyncio

import pytest
from conftest import SAMPLE

from app.interpreter import OfflineInterpreter
from app.schemas import TurnRequest
from app.store import Conflict


class GatedInterpreter:
    def __init__(self, blocked, ignore_cancel=False):
        self.blocked = blocked
        self.ignore_cancel = ignore_cancel
        self.started = asyncio.Event()
        self.released = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def observe(self, text, context):
        if text == self.blocked:
            self.started.set()
            try:
                await self.released.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                if not self.ignore_cancel:
                    raise
                await self.released.wait()
        return await OfflineInterpreter().observe(text, context)


async def say(app, state, mid, text="", action="message", **kwargs):
    return await app.state.engine.turn(
        state["session_id"],
        "owner",
        TurnRequest(message_id=mid, expected_version=state["version"], text=text, action=action, **kwargs),
    )


async def test_interrupt_keeps_committed_identity_and_accepts_correction(app):
    state = await say(
        app,
        app.state.store.create("owner"),
        "name",
        "My name is Margaret Chen. My denied healthcare claim from January.",
    )
    blocked = "My DOB is 1900-01-01. SSN last four is 0000."
    gate = GatedInterpreter(blocked)
    app.state.engine.interpreter = gate
    old = asyncio.create_task(say(app, state, "old", blocked))
    await gate.started.wait()
    state = await say(app, state, "stop", action="interrupt", target_message_id="old")
    with pytest.raises(Conflict, match="TURN_SUPERSEDED"):
        await old
    assert gate.cancelled.is_set()
    assert state["identity"]["name"]["value"] == "margaret chen"
    assert "dob" not in state["identity"]
    assert state["phase"] == "VERIFY_ID" and not state["auth"]
    state = await say(app, state, "correct", "DOB is 1985-03-15. SSN last four is 4472.")
    assert state["selected_case_id"] == "CL-2048"
    assert not app.state.engine.observations


async def test_targeted_interrupt_survives_interim_commit_and_pauses_email(app):
    store = app.state.store
    state = await say(app, store.create("owner"), "verify", SAMPLE)
    state = await say(app, state, "finish", action="finish")
    state = await say(app, state, "consent", "yes")
    gate = GatedInterpreter("Wait, how much has actually been paid?")
    app.state.engine.interpreter = gate
    old = asyncio.create_task(say(app, state, "old", gate.blocked))
    await gate.started.wait()
    assert store.load(state["session_id"], "owner")["version"] > state["version"]
    state = await say(app, state, "stop", action="interrupt", target_message_id="old")
    with pytest.raises(Conflict):
        await old
    assert state["auth"] and state["draft"]["paused"]
    store.dispatch()
    assert store.load(state["session_id"], "owner")["email_operation"]["status"] == "QUEUED"
    state = await say(app, state, "question", "What is the allowed amount?")
    assert state["phase"] == "PROCESS_CASE" and not state["draft"]
    store.dispatch()
    with store.tx() as db:
        assert db.execute("SELECT status FROM outbox").fetchone()[0] == "CANCELLED"


async def test_interrupt_before_original_request_arrives_prevents_late_start(app):
    state = app.state.store.create("owner")
    original = state.copy()
    state = await say(app, state, "stop", action="interrupt", target_message_id="delayed")
    with pytest.raises(Conflict, match="TURN_SUPERSEDED"):
        await say(app, original, "delayed", SAMPLE)
    state = app.state.store.load(state["session_id"], "owner")
    assert state["phase"] == "VERIFY_ID" and not state["identity"]
    assert not any(e["kind"] == "model.observed" for e in app.state.store.trace(state["session_id"], "owner"))


async def test_late_uncooperative_result_cannot_read_claims_or_replace_new_answer(app):
    state = app.state.store.create("owner")
    gate = GatedInterpreter(SAMPLE, ignore_cancel=True)
    app.state.engine.interpreter = gate
    old = asyncio.create_task(say(app, state, "old", SAMPLE))
    await gate.started.wait()
    state = await say(app, state, "stop", action="interrupt", target_message_id="old")
    await gate.cancelled.wait()
    state = await say(
        app,
        state,
        "new",
        "My name is Ava Lopez. DOB is 1990-08-21. My email is ava.lopez@email.com. What is my claim status?",
    )
    gate.released.set()
    with pytest.raises(Conflict, match="TURN_SUPERSEDED"):
        await old
    final = app.state.store.load(state["session_id"], "owner")
    assert final["auth"]["subject"] == "P7"
    assert final["version"] == state["version"]
    assert final["selected_case_id"] is None
    assert "pathology report" not in final["messages"][-1]["text"]
    assert not any(
        e["kind"] == "tool.called" and e["details"].get("tool") == "get_claim"
        for e in app.state.store.trace(state["session_id"], "owner")
    )


async def test_replayed_interrupt_does_not_cancel_new_turn(app):
    state = app.state.store.create("owner")
    request = TurnRequest(message_id="stop", expected_version=0, action="interrupt", target_message_id="old")
    state = await app.state.engine.turn(state["session_id"], "owner", request)
    gate = GatedInterpreter(SAMPLE)
    app.state.engine.interpreter = gate
    new = asyncio.create_task(say(app, state, "new", SAMPLE))
    await gate.started.wait()
    await app.state.engine.turn(state["session_id"], "owner", request)
    assert not gate.cancelled.is_set()
    with pytest.raises(Conflict, match="INTERRUPT_TARGET_CHANGED"):
        await say(app, state, "stale-stop", action="interrupt", target_message_id="old")
    gate.released.set()
    final = await new
    assert final["selected_case_id"] == "CL-2048"


@pytest.mark.parametrize("text", ["wait", "hold on", "pause please", "等一下", "先停一下"])
def test_brief_pause_preserves_state_without_model(chat, text):
    chat.say("My name is Margaret Chen. My denied healthcare claim from January.")
    before = len([e for e in chat.trace() if e["kind"] == "model.observed"])
    chat.say(text)
    assert chat.state["phase"] == "VERIFY_ID" and chat.state["identity_count"] == 1
    assert "paused here" in chat.reply
    assert len([e for e in chat.trace() if e["kind"] == "model.observed"]) == before
    chat.say("DOB is 1985-03-15. SSN last four is 4472.")
    assert chat.state["selected_case_id"] == "CL-2048"


def test_verification_detour_answers_question_and_returns_to_missing_fields(chat):
    chat.say("My name is Margaret Chen. My denied healthcare claim from January.")
    chat.say("Wait, why do you need to verify my identity?")
    assert "protects your private claim information" in chat.reply
    assert "provide 2 more" in chat.reply
    assert chat.state["identity_count"] == 1 and not chat.state["verified"]
    chat.say("DOB is 1985-03-15. SSN last four is 4472.")
    assert chat.state["selected_case_id"] == "CL-2048"


def test_pause_does_not_recall_completed_operation(verified, app):
    verified.say(action="finish")
    verified.say("yes")
    app.state.store.dispatch()
    verified.refresh()
    verified.say("wait")
    assert verified.state["lifecycle"] == "COMPLETED"
    assert verified.state["email_operation"]["status"] == "SIMULATED_RECORDED"
    assert "cannot be recalled" in verified.reply


def test_interruption_is_owner_scoped(app, client, chat):
    from fastapi.testclient import TestClient

    stranger = TestClient(app)
    stranger.post("/v1/sessions")
    response = stranger.post(
        f"/v1/sessions/{chat.state['session_id']}/messages",
        json={
            "message_id": "attack",
            "expected_version": 0,
            "action": "interrupt",
            "target_message_id": "old",
        },
    )
    assert response.status_code == 404


def test_email_refusal_with_new_question_cancels_mail_and_keeps_conversation(verified, app):
    verified.say(action="finish")
    verified.say("yes")
    verified.say("Wait, do not email me. First explain how much was paid.")
    assert verified.state["lifecycle"] == "ACTIVE"
    assert verified.state["phase"] == "PROCESS_CASE"
    assert not verified.state["draft"]
    assert "Finalized insurer payment recorded" in verified.reply
    app.state.store.dispatch()
    with app.state.store.tx() as db:
        assert db.execute("SELECT status FROM outbox").fetchone()[0] == "CANCELLED"
    verified.say(action="finish")
    assert verified.state["lifecycle"] == "COMPLETED" and not verified.state["draft"]
