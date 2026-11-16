"""Acceptance journeys for asynchronous support and shared authorization (v1.5)."""

import json
import re
import time

import pytest
from conftest import SAMPLE, Conversation
from fastapi.testclient import TestClient

from app.responder import AnswerDraft, Paragraph, validate_references


def expire_shared(app, idle=True):
    with app.state.store.tx() as db:
        row = db.execute("SELECT * FROM authorizations WHERE payload IS NOT NULL").fetchone()
        auth = app.state.store.unseal(row["payload"])
        auth["last_used_at" if idle else "expires_at"] = time.time() - 4000
        db.execute(
            "UPDATE authorizations SET payload=? WHERE owner=?", (app.state.store.seal(auth), row["owner"])
        )


def test_identity_is_shared_across_conversations_tabs_and_panels(verified, client):
    cookie = client.cookies.get("sop_principal")
    assert cookie
    other = Conversation(client)
    assert other.state["verified"] and other.state["phase"] == "RESOLVE_INTENT"
    assert other.state["selected_case_id"] is None
    account = client.get(f"/v1/sessions/{other.state['session_id']}/account")
    assert account.status_code == 200 and len(account.json()["claims"]) == 4
    verified.say(action="reset")
    assert verified.state["verified"] and verified.state["selected_case_id"] is None
    assert client.get(f"/v1/sessions/{verified.state['session_id']}/account").status_code == 200


def test_logout_revokes_all_tabs_and_new_conversation(verified, client):
    other = Conversation(client)
    before = verified.state["authorization_epoch"]
    result = client.post("/v1/auth/logout")
    assert result.status_code == 200
    for chat in (verified, other):
        chat.refresh()
        assert not chat.state["verified"] and chat.state["authorization_epoch"] > before
        assert client.get(f"/v1/sessions/{chat.state['session_id']}/account").status_code == 403
        assert "CL-2048" not in json.dumps(chat.state["messages"])
    assert not Conversation(client).state["verified"]
    verified.say(SAMPLE)
    assert verified.state["verified"]


@pytest.mark.parametrize("idle", [True, False])
def test_expiry_is_shared_and_cannot_be_resurrected(app, verified, client, idle):
    other = Conversation(client)
    expire_shared(app, idle)
    assert not other.refresh()["verified"]
    assert not verified.refresh()["verified"]
    assert not Conversation(client).state["verified"]


def test_cookie_is_http_only_and_separate_browser_has_no_grant(app, verified):
    foreign = TestClient(app)
    result = foreign.post("/v1/sessions")
    assert "HttpOnly" in result.headers["set-cookie"]
    assert not result.json()["verified"]
    assert foreign.get(f"/v1/sessions/{verified.state['session_id']}").status_code == 404


def test_human_request_then_ai_and_wait_mode(verified):
    verified.say("i need human")
    ticket = verified.state["handoff"]
    assert ticket["channel"] == "MOCK" and ticket["id"].startswith("H-")
    assert ticket["id"] in verified.reply
    assert (
        "registered email" in verified.reply
        and "registered phone" in verified.reply
        and "no response time" in verified.reply
    )
    assert ticket["summary"]["verified_identity"]["party_id"] == "P9"
    assert ticket["summary"]["discussion"] and ticket["summary"]["reason"]
    verified.refresh()
    verified.say("How much was paid?")
    assert "$0.00" in verified.reply
    verified.say(action="wait_human")
    assert verified.state["support_mode"] == "wait"
    for text in ["why was it denied?", "what is RL?", "Please make an exception", "你好"]:
        verified.say(text)
        assert verified.reply == f"System confirmation: added to {ticket['id']}."
    verified.say(action="resume")
    assert ticket["id"] in verified.reply and "still awaiting" in verified.reply
    verified.say("What is the deadline?")
    assert "2026-03-18" in verified.reply and "has passed" in verified.reply


def test_human_before_verification_attaches_to_identity_and_new_conversation(chat, client):
    chat.say("i need human")
    hid = re.search(r"H-[A-Z0-9]+", chat.reply).group()
    chat.say(SAMPLE)
    assert chat.state["verified"]
    other = Conversation(client)
    assert hid in other.reply and other.state["handoffs"][0]["summary"]["verified_now"]
    assert not other.state["handoffs"][0]["details_locked"]
    assert other.state["handoffs"][0]["case_id"] == "CL-2048"
    assert other.state["handoffs"][0]["summary"]["claim_description"]["month"] == "1"


def test_expired_exception_is_saved_but_never_granted(verified):
    verified.say("i need human")
    hid = verified.state["handoff"]["id"]
    verified.say("Can you make an exception and accept a late appeal?")
    assert hid in verified.reply and "Only a representative" in verified.reply
    assert verified.state["handoff_note_offer"]
    verified.say(action="handoff_note_accept")
    ticket = verified.state["handoffs"][0]
    assert any("exception" in s for s in ticket["summary"]["open_questions"])
    assert verified.state["next_steps"]["expired"]
    assert verified.state["next_steps"]["actions"][0]["action"] == "handoff_status"


def test_expired_chat_buttons_and_summary_agree(verified):
    verified.say("What are the next steps?")
    assert "Only a representative" in verified.reply
    assert "Prepare a complete" not in verified.reply and "usually less than a week" not in verified.reply
    assert verified.state["next_steps"]["actions"] == [
        {"label": "Request representative assessment", "action": "human"}
    ]
    verified.say(action="human")
    hid = verified.state["handoff"]["id"]
    verified.say(action="finish")
    body = verified.state["draft"]["body"]
    assert [line.split(":")[0] for line in body.split("\n\n")] == [
        "Conclusion",
        "Discussion",
        "Next steps",
        "References",
    ]
    assert "CL-2048" in body and hid in body
    assert "unknown" not in body and "verification" not in body
    assert "has passed" in body and "representative assessment" in body and "simulated" not in body.lower()
    assert "Ask your provider" not in body and "submit documents" not in body


def test_scope_three_unrelated_questions_and_ambiguous_term(chat):
    for question in ["what is RL?", "what is the capital of France?", "write Python code"]:
        chat.say(question)
        assert any(
            t in chat.reply
            for t in ["can’t answer unrelated", "stay with insurance", "only help with insurance"]
        )
        assert not chat.state["verified"]
    assert "human" in chat.reply
    chat.say("what is ABC?")
    assert "on your insurance claim" in chat.reply
    chat.say("no")
    assert any(
        t in chat.reply for t in ["can’t answer unrelated", "stay with insurance", "only help with insurance"]
    )


def test_emotional_recovery_and_persuasion_bound(chat):
    chat.say("I already told you who I am. This is ridiculous. Just tell me why my claim was denied.")
    assert chat.reply.startswith("I understand")
    assert "protect" in chat.reply and "three" in chat.reply
    assert "CL-2048" not in chat.reply and "pathology" not in chat.reply
    chat.say("I refuse to give my SSN")
    assert "registered email" in chat.reply and "date of birth" in chat.reply
    assert "SSN last four" not in chat.reply.split("please provide")[-1]
    chat.say("This is ridiculous")
    assert "human" in chat.reply and chat.state["verification_locked"]


def test_notification_complaint_admits_missing_history(verified):
    verified.say("this is ridiculous, why did nobody tell me about the deadline?")
    assert verified.reply.startswith("I understand")
    assert "No notification history" in verified.reply


def test_identity_spans_preserve_claim_description_and_receipt(chat):
    chat.say(SAMPLE)
    user = chat.state["messages"][-2]["text"]
    assert "denied healthcare claim from January" in user
    for pii in ["Margaret Chen", "1985-03-15", "4472"]:
        assert pii not in user
    assert "[NAME]" in user and "[DOB]" in user
    assert "Identity verified, Margaret" in chat.reply
    assert "CL-2048" in chat.reply and "2026-01-12" in chat.reply
    chat.say(action="human")
    assert "January" in json.dumps(chat.state["handoff"]["summary"])


@pytest.mark.parametrize("thanks", ["thanks", "thank you", "谢谢"])
def test_natural_end_asks_then_offers_email(verified, thanks):
    verified.say(thanks)
    assert verified.state["closing_pending"] and not verified.state["draft"]
    assert "anything else" in verified.reply
    verified.say("no")
    assert verified.state["phase"] == "POST_PROCESS" and verified.state["draft"]
    assert "skip email" in verified.reply
    verified.say(action="email_skip")
    assert verified.state["lifecycle"] == "COMPLETED"
    assert verified.state["verified"]


def test_pending_ticket_cancel_and_resume_wait_mode(verified):
    verified.say(action="human")
    ticket = verified.state["handoff"]
    verified.say(action="wait_human")
    result = verified.client.post(
        f"/v1/handoffs/{ticket['id']}/cancel",
        json={
            "session_id": verified.state["session_id"],
            "message_id": "cancel-human",
            "expected_revision": ticket["revision"],
            "action": "cancel",
        },
    )
    assert result.status_code == 200
    verified.refresh()
    verified.say("How much was paid?")
    assert verified.state["support_mode"] == "ai" and "$0.00" in verified.reply
    assert verified.state["next_steps"]["actions"][0]["action"] == "human"


def test_summary_guard_rejects_invented_money_date_or_reference():
    context = {
        "authorized_evidence": [],
        "server_guidance": "Conclusion: claim CL-2048. References: CL-2048.",
        "business_date": "2026-11-15",
        "current_message": "",
        "current_action": "summary",
    }
    for invented in ["$900.00", "2027-02-01", "H-FAKE"]:
        answer = AnswerDraft(
            paragraphs=[
                Paragraph(text="Conclusion: " + invented, evidence_ids=[]),
                Paragraph(text="Discussion: claim.", evidence_ids=[]),
                Paragraph(text="Next steps: request review.", evidence_ids=[]),
                Paragraph(text="References: CL-2048.", evidence_ids=[]),
            ],
            answered_topics=[],
        )
        with pytest.raises(ValueError):
            validate_references(answer, context)


@pytest.mark.asyncio
async def test_logout_fences_inflight_ai_and_never_reinstates_grant(app, client):
    import asyncio

    from app.schemas import TurnRequest, empty_observation
    from app.store import Conflict

    chat = Conversation(client)
    chat.say(SAMPLE)
    started, release = asyncio.Event(), asyncio.Event()

    class Delayed:
        async def observe(self, text, context):
            started.set()
            await release.wait()
            return empty_observation(topics=["amounts"]), {"mode": "offline", "calls": 0}

    app.state.engine.interpreter = Delayed()
    owner = app.state.store.fingerprint(client.cookies.get("sop_principal"))
    task = asyncio.create_task(
        app.state.engine.turn(
            chat.state["session_id"],
            owner,
            TurnRequest(message_id="late-reply", expected_version=chat.state["version"], text="How much?"),
        )
    )
    await started.wait()
    app.state.store.logout(owner)
    release.set()
    with pytest.raises(Conflict):
        await task
    assert not chat.refresh()["verified"]
    assert not Conversation(client).state["verified"]


def test_wait_message_idempotency_and_cancellation_are_atomic(verified):
    verified.say(action="human")
    verified.say(action="wait_human")
    payload = {
        "message_id": "same-note",
        "expected_version": verified.state["version"],
        "text": "Please review this once.",
        "action": "message",
    }
    path = f"/v1/sessions/{verified.state['session_id']}/messages"
    first = verified.client.post(path, json=payload)
    second = verified.client.post(path, json=payload)
    assert first.status_code == second.status_code == 200
    notes = second.json()["handoffs"][0]["timeline"]
    assert sum(t["text"] == "Please review this once." for t in notes) == 1
    assert verified.client.post(path, json={**payload, "text": "changed"}).status_code == 409


def test_legacy_live_request_migrates_to_async(app, verified):
    verified.say(action="human")
    hid = verified.state["handoff"]["id"]
    with app.state.store.tx() as db:
        row = db.execute("SELECT * FROM handoffs WHERE id=?", (hid,)).fetchone()
        ticket = app.state.store.unseal(row["payload"])
        ticket.update(channel="LIVE_CHAT", status="OFFLINE")
        app.state.store._save_handoff(db, ticket, row["owner"])
    verified.refresh()
    assert verified.state["handoff"]["channel"] == "MOCK"
    assert verified.state["handoff"]["status"] == "SIMULATED_RECORDED"
    verified.say(action="wait_human")
    assert verified.say("hello")["messages"][-1]["text"] == f"System confirmation: added to {hid}."


def test_human_decision_for_payment_is_not_limited_to_deadlines(verified):
    verified.say(action="human")
    hid = verified.state["handoff"]["id"]
    verified.say("Can you approve my reimbursement?")
    assert hid in verified.reply and "Only a representative" in verified.reply
    assert verified.state["handoff_note_offer"]
    verified.say(action="handoff_note_accept")
    assert any("approve" in q for q in verified.state["handoff"]["summary"]["open_questions"])


def test_expired_guard_allows_human_request_but_blocks_document_submission():
    context = {
        "authorized_evidence": [],
        "server_guidance": "",
        "current_message": "",
        "business_date": "2026-11-15",
        "next_steps": {"expired": True},
    }
    good = AnswerDraft(
        paragraphs=[
            Paragraph(text="You can submit a human request for representative assessment.", evidence_ids=[])
        ],
        answered_topics=[],
    )
    validate_references(good, context)
    for text in [
        "You can submit your documents now.",
        "Please prepare a pathology report.",
        "You should upload the missing files.",
    ]:
        with pytest.raises(ValueError):
            validate_references(
                AnswerDraft(paragraphs=[Paragraph(text=text, evidence_ids=[])], answered_topics=[]), context
            )


@pytest.mark.parametrize(
    "literal", ["January 1, 2030", "1 January 2030", "2030年1月1日", "USD 9999", "9999 dollars", "9999美元"]
)
def test_written_dates_and_amounts_require_tool_facts(literal):
    context = {
        "authorized_evidence": [],
        "server_guidance": "Customer says " + literal,
        "current_message": literal,
        "business_date": "2026-11-15",
    }
    with pytest.raises(ValueError):
        validate_references(
            AnswerDraft(paragraphs=[Paragraph(text=literal, evidence_ids=[])], answered_topics=[]), context
        )


def test_scope_gate_cannot_be_rewritten_as_claim_clarification(app, verified):
    from app.interpreter import OfflineInterpreter

    class UnreliableWriter(OfflineInterpreter):
        async def answer(self, context):
            raise AssertionError("Out-of-scope gate must not become a claim-writing request")

    app.state.engine.interpreter = UnreliableWriter()
    for question in ["what is RL?", "write Python code", "give me a recipe"]:
        verified.say(question)
        assert any(
            t in verified.reply
            for t in ["can’t answer unrelated", "stay with insurance", "only help with insurance"]
        )
    assert "human claims representative" in verified.reply


def test_model_cannot_omit_new_verification_or_selected_claim_receipt(app, chat):
    from app.interpreter import OfflineInterpreter

    class BriefWriter(OfflineInterpreter):
        async def answer(self, context):
            return (
                "The recorded claim was denied. Is that the claim you mean?",
                [],
                ["status"],
                {"mode": "offline", "calls": 0},
            )

    app.state.engine.interpreter = BriefWriter()
    chat.say(SAMPLE)
    assert "Margaret" in chat.reply and "CL-2048" in chat.reply and "2026-01-12" in chat.reply


def test_plain_thanks_works_even_when_observer_would_misclassify(app, verified):
    class NoObservation:
        async def observe(self, text, context):
            raise AssertionError("Whole-message closing cue does not require classification")

    app.state.engine.interpreter = NoObservation()
    verified.say("thanks")
    assert verified.state["closing_pending"]
    verified.say("no")
    assert verified.state["draft"] and verified.state["phase"] == "POST_PROCESS"


def test_human_only_decision_does_not_switch_modes_or_repeat_receipt(app, verified):
    from app.interpreter import OfflineInterpreter
    from app.schemas import empty_observation

    verified.say(action="human")
    hid = verified.state["handoff"]["id"]

    class OvereagerHandoff(OfflineInterpreter):
        async def observe(self, text, context):
            return empty_observation(action="human", requires_human_decision=True), {
                "mode": "offline",
                "calls": 0,
            }

    app.state.engine.interpreter = OvereagerHandoff()
    verified.say("Can you approve the late appeal?")
    assert hid in verified.reply and "Only a representative" in verified.reply
    assert "Your asynchronous human handling request is" not in verified.reply
    assert verified.state["support_mode"] == "ai"


@pytest.mark.parametrize(
    "sentence",
    [
        "Your request has been forwarded to a representative.",
        "A representative will assess your request.",
        "CL-2048 has been denied since 2026-01-12.",
    ],
)
def test_no_fabricated_external_handling_or_status_date(sentence):
    context = {
        "authorized_evidence": [
            {"id": "claim", "text": "CL-2048 created 2026-01-12", "source": "claims", "kind": "fact"}
        ],
        "current_message": "",
        "server_guidance": "",
        "business_date": "2026-11-15",
    }
    with pytest.raises(ValueError):
        validate_references(
            AnswerDraft(paragraphs=[Paragraph(text=sentence, evidence_ids=["claim"])], answered_topics=[]),
            context,
        )


def test_form_selection_packet_does_not_mix_expiry_or_human_context(app, chat):
    from app.interpreter import OfflineInterpreter

    class SelectionWriter(OfflineInterpreter):
        async def answer(self, context):
            assert context["next_steps"] is None
            assert context["support_requests"] == []
            assert context["conversation"] == []
            assert all(
                e["id"].endswith(":status,case_type,created_at") for e in context["authorized_evidence"]
            )
            assert "2026-03-18" not in context["server_guidance"]
            return "Claim CL-2048 is denied.", [], ["status"], {"mode": "offline", "calls": 0}

    chat.say(action="human")
    app.state.engine.interpreter = SelectionWriter()
    chat.say(
        action="form",
        form={
            "name": "Margaret Chen",
            "dob": "1985-03-15",
            "email": "margaret@email.com",
            "case_id": "CL-2048",
        },
    )
    assert chat.state["verified"] and "CL-2048" in chat.reply
