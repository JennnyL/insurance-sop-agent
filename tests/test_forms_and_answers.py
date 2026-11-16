import asyncio
import json

import httpx
import pytest
from conftest import SAMPLE

from app.interpreter import ModelInterpreter, OfflineInterpreter
from app.schemas import TurnRequest
from app.store import Conflict

FORM = {"name": "Margaret Chen", "dob": "1985-03-15", "ssn_last4": "4472", "case_id": "CL-2048"}


def test_form_identity_and_chat_followup(chat):
    chat.say(action="form", form=FORM)
    assert chat.state["verified"] and chat.state["selected_case_id"] == "CL-2048"
    assert "1985-03-15" not in json.dumps(chat.state)
    chat.say("How much was paid?")
    assert "Finalized insurer payment recorded" in chat.reply


def test_chat_identity_can_be_completed_with_form(chat):
    chat.say("My name is Margaret Chen. My denied healthcare claim from January.")
    chat.say(action="form", form={"dob": "1985-03-15", "email": "margaret@email.com"})
    assert chat.state["verified"] and chat.state["selected_case_id"] == "CL-2048"


def test_form_claim_id_alone_still_requires_verification(chat):
    chat.say(action="form", form={"case_id": "CL-2048"})
    assert not chat.state["verified"]
    assert not any(e["kind"] == "tool.called" for e in chat.trace())
    chat.say(SAMPLE)
    assert chat.state["selected_case_id"] == "CL-2048"


def test_bad_form_then_good_form_and_cross_account_lookup(chat):
    chat.say(action="form", form={**FORM, "name": "Nobody Exists", "email": "wrong@example.com"})
    assert not chat.state["verified"]
    chat.say(action="form", form=FORM)
    assert chat.state["verified"]
    chat.say(action="form", form={"case_id": "CL-2102"})
    assert chat.state["selected_case_id"] == "CL-2102"
    chat.say(action="form", form={"case_id": "CL-3001"})
    assert chat.state["selected_case_id"] == "CL-2102"  # Failed lookup preserves prior case.
    assert "diagnosis report" not in chat.reply


def test_form_reverify_another_person_clears_old_claim(verified):
    verified.say(
        action="form", form={"name": "Ava Lopez", "dob": "1990-08-21", "email": "ava.lopez@email.com"}
    )
    assert verified.state["verified"]
    assert verified.state["selected_case_id"] is None
    assert not verified.state["evidence"]


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "form", "form": {}},
        {"action": "form", "form": {"verified": True}},
        {"action": "message", "form": FORM},
        {"action": "form", "form": FORM, "text": "extra"},
        {"action": "form", "form": {"ssn_last4": "123"}},
    ],
)
def test_invalid_form_payloads_rejected(client, payload):
    state = client.post("/v1/sessions").json()
    response = client.post(
        f"/v1/sessions/{state['session_id']}/messages",
        json={"message_id": "form", "expected_version": 0, **payload},
    )
    assert response.status_code == 422


class ComposableOffline(OfflineInterpreter):
    def __init__(self, settings, handler):
        self.model = ModelInterpreter(settings, transport=httpx.MockTransport(handler))

    async def answer(self, context):
        return await self.model.answer(context)


def wire(payload):
    return httpx.Response(
        200,
        json={
            "status": "completed",
            "usage": {"input_tokens": 80, "output_tokens": 25},
            "output": [{"content": [{"type": "output_text", "text": json.dumps(payload)}]}],
        },
    )


def answer(text, refs=None, topics=None):
    return {"paragraphs": [{"text": text, "evidence_ids": refs or []}], "answered_topics": topics or []}


def approve(**overrides):
    return {
        "supported": True,
        "addresses_question": True,
        "follows_workflow": True,
        "reason": "Supported",
        **overrides,
    }


def test_model_writes_contextual_answer_beyond_observer_topic_rules(verified, app):
    calls = []
    generated = "The record says the pathology report and office note are missing, which is why this claim was denied."

    def handler(request):
        body = json.loads(request.content)
        context = json.loads(body["input"][1]["content"])
        calls.append(body["text"]["format"]["name"])
        if calls[-1] == "grounded_answer":
            assert context["current_message"] == "please explain why"
            assert context["verified"]
            assert any("CL-2048" in m["text"] for m in context["conversation"])
            assert all(item["id"].startswith("CL-2048:") for item in context["authorized_evidence"])
            assert "CL-3001" not in json.dumps(context)
            return wire(answer(generated, ["CL-2048:denial_reason"], ["denial"]))
        return wire(approve())

    app.state.engine.interpreter = ComposableOffline(app.state.settings, handler)
    verified.say("please explain why")
    assert verified.reply == generated
    assert calls == ["grounded_answer", "answer_review"]
    assert verified.state["model_status"]["calls"] == 2


def test_preverification_writer_has_no_records_or_raw_identity(chat, app):
    def handler(request):
        body = json.loads(request.content)
        if body["text"]["format"]["name"] == "grounded_answer":
            context = json.loads(body["input"][1]["content"])
            assert not context["verified"] and not context["authorized_evidence"]
            assert "Margaret Chen" not in json.dumps(context)
            return wire(
                answer(
                    "I can help once we verify your identity, which protects your private claim records. Please add two identity fields; you can use your date of birth and registered email."
                )
            )
        return wire(approve())

    app.state.engine.interpreter = ComposableOffline(app.state.settings, handler)
    chat.say("My name is Margaret Chen.")
    assert "Please add two identity fields" in chat.reply
    assert not any(e["kind"] == "tool.called" for e in chat.trace())


def test_model_can_acknowledge_unmatched_user_supplied_case_id_without_disclosure(verified, app):
    generated = "I couldn’t find claim CL-3001 in your account’s records. Please check the reference."

    def handler(request):
        body = json.loads(request.content)
        if body["text"]["format"]["name"] == "grounded_answer":
            context = json.loads(body["input"][1]["content"])
            assert not context["authorized_evidence"]
            return wire(answer(generated))
        return wire(approve())

    app.state.engine.interpreter = ComposableOffline(app.state.settings, handler)
    verified.say("Actually, check CL-3001 instead.")
    assert verified.reply == generated
    assert verified.state["selected_case_id"] == "CL-2048"


def test_returning_to_case_supplies_its_memory_to_writer(verified, app):
    verified.say("How much was paid?")
    verified.say(action="form", form={"case_id": "CL-2102"})
    seen = []

    def handler(request):
        body = json.loads(request.content)
        if body["text"]["format"]["name"] == "grounded_answer":
            context = json.loads(body["input"][1]["content"])
            memory = json.dumps(context["remembered_case_dialogue"])
            assert "How much was paid?" in memory
            assert "CL-2102" not in memory
            assert "CL-2102" not in json.dumps(context["conversation"])
            seen.append(True)
            return wire(
                answer("Claim CL-2048 is denied.", ["CL-2048:status,case_type,created_at"], ["status"])
            )
        return wire(approve())

    app.state.engine.interpreter = ComposableOffline(app.state.settings, handler)
    verified.say(action="form", form={"case_id": "CL-2048"})
    assert seen and "CL-2048 is denied" in verified.reply


@pytest.mark.parametrize("method", ["chat", "form"])
@pytest.mark.parametrize("reference", ["POL-9921", "POL-0000"])
def test_policy_writer_receives_search_result_without_previous_claim_question(
    verified, app, method, reference
):
    verified.say("How much was paid?")
    previous_sources = verified.state["evidence"]
    seen = []

    def handler(request):
        body = json.loads(request.content)
        if body["text"]["format"]["name"] == "grounded_answer":
            context = json.loads(body["input"][1]["content"])
            assert reference in context["current_message"]
            assert context["current_action"] == "case_lookup"
            assert context["conversation"] == context["remembered_case_dialogue"] == []
            assert context["user_reported_hints"] == {} and not context["last_answer_topics"]
            assert "pathology" not in json.dumps(context)
            matches = context["lookup_result"]["matches"]
            assert len(matches) == (4 if reference == "POL-9921" else 0)
            refs = [e["id"] for e in context["authorized_evidence"]]
            assert refs == ["account:" + c["case_id"] for c in matches]
            seen.append(True)
            return wire(
                answer(
                    "Please select a matching claim."
                    if matches
                    else "No matching policy was found. Your previous claim CL-2048 remains selected.",
                    refs,
                )
            )
        return wire(approve())

    app.state.engine.interpreter = ComposableOffline(app.state.settings, handler)
    if method == "form":
        verified.say(action="form", form={"policy_number": reference})
    else:
        verified.say(reference)
    assert seen and verified.state["selected_case_id"] == "CL-2048"
    assert verified.state["evidence"] == previous_sources


@pytest.mark.parametrize(
    "fault", ["unknown_citation", "invented_amount", "review_reject", "review_unavailable"]
)
def test_ungrounded_or_unreviewed_answer_is_not_shown(verified, app, fault):
    def handler(request):
        body = json.loads(request.content)
        if body["text"]["format"]["name"] == "grounded_answer":
            refs = ["CL-3001:net_pay"] if fault == "unknown_citation" else ["CL-2048:net_pay"]
            text = (
                "You have received $999999.00."
                if fault == "invented_amount"
                else "Your payment is guaranteed."
            )
            return wire(answer(text, refs, ["amounts"]))
        if fault == "review_unavailable":
            return httpx.Response(503)
        return wire(approve(supported=False))

    app.state.engine.interpreter = ComposableOffline(app.state.settings, handler)
    verified.say("How much was paid?")
    # 1.7.4: the rejected draft is never shown; the grounded fixture template answers instead.
    assert "999999" not in verified.reply and "payment is guaranteed" not in verified.reply
    assert "$0.00" in verified.reply
    assert "couldn’t produce a reliable answer" not in verified.reply
    assert verified.state["verified"]


@pytest.mark.asyncio
async def test_interrupt_fences_model_writer_result(app):
    started = asyncio.Event()

    class SlowAnswer(OfflineInterpreter):
        async def answer(self, context):
            started.set()
            await asyncio.Event().wait()

    engine = app.state.engine
    engine.interpreter = SlowAnswer()
    state = app.state.store.create("owner")
    pending = asyncio.create_task(
        engine.turn(
            state["session_id"], "owner", TurnRequest(message_id="slow", expected_version=0, text=SAMPLE)
        )
    )
    await asyncio.wait_for(started.wait(), 2)
    stopped = await engine.turn(
        state["session_id"],
        "owner",
        TurnRequest(message_id="stop", expected_version=0, action="interrupt", target_message_id="slow"),
    )
    with pytest.raises(Conflict, match="TURN_SUPERSEDED"):
        await pending
    assert stopped["auth"]  # The durable verification checkpoint survives.
    assert "paused" in stopped["messages"][-1]["text"]
