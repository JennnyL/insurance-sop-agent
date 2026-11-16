"""A representative's consultation must never inherit the policyholder's dialogue."""

import json

import pytest
from conftest import Conversation

REP = "I'm David Chen, calling for my mother Margaret Chen. DOB 1985-03-15, email margaret@email.com. Her denied healthcare claim from January."


def authorize(chat):
    chat.say(REP)
    assert chat.state["identity_count"] == 3
    assert not chat.state["verified"]
    assert chat.state["representative_consent"]["status"] == "pending"
    assert chat.reply.count("pending") == 1
    assert "CL-2048" not in chat.reply
    chat.say(action="consent_check")
    assert chat.state["verified"]
    assert chat.reply.startswith("Policyholder consent approved. David Chen,")
    assert chat.state["selected_case_id"] is None
    assert chat.state["claim_confirmation"] == "CL-2048"
    assert "deadline" not in chat.reply.lower()
    assert len(chat.reply.split()) < 65
    chat.say("Yes, this claim.")
    assert chat.state["selected_case_id"] == "CL-2048"


def logout(chat):
    assert chat.client.post("/v1/auth/logout").status_code == 200
    chat.refresh()
    assert not chat.state["verified"]


def test_policyholder_history_never_becomes_representative_context(verified, app):
    verified.say("Why did nobody tell me? How was I notified?")
    verified.say("What if I cannot get the report?")
    verified.say(action="human")
    old_hid = verified.state["handoff"]["id"]
    verified.say(action="finish")
    old_draft = verified.state["draft"]["body"]
    assert "alternatives" in old_draft
    logout(verified)
    authorize(verified)
    state = verified.state
    assert state["caller"] == {"role": "representative", "name": "David Chen"}
    assert state["data_subject"] == {"party_id": "P9", "name": "Margaret Chen"}
    assert state["authorization_method"] == "policyholder_consent"
    assert state["memory"]["case_history"]["turn_count"] == 1
    assert not state["handoffs"] and state["handoff"] is None
    assert old_hid not in json.dumps(state)
    assert "notification history" not in verified.reply.lower()
    assert "alternatives" not in state["memory"]["case_history"]["discussed_topics"]
    assert "verified, Margaret" not in verified.reply
    verified.say("no, that’s all, thanks")
    assert state["session_id"] == verified.state["session_id"]
    assert verified.state["phase"] == "POST_PROCESS"
    assert "anything else" not in verified.reply.lower()
    assert "the policyholder's registered email" in verified.reply
    draft = verified.state["draft"]
    assert draft["recipient_label"] == "the policyholder's registered email"
    assert "David Chen as representative for policyholder Margaret Chen" in draft["body"]
    assert "policyholder's approved consent" in draft["body"]
    assert "document alternatives" not in draft["body"]
    assert "notification history" not in draft["body"].lower()
    assert "Customer:" not in draft["body"]


def test_new_conversation_keeps_grant_but_not_prior_discussion(verified):
    verified.say("What if I cannot get the report?")
    fresh = Conversation(verified.client)
    assert fresh.state["verified"]
    fresh.say(action="form", form={"case_id": "CL-2048"})
    assert fresh.state["memory"]["case_history"]["turn_count"] == 1
    assert "alternatives" not in fresh.state["memory"]["case_history"]["discussed_topics"]
    fresh.say(action="finish")
    assert "document alternatives" not in fresh.state["draft"]["body"]


def test_representative_new_tab_labels_and_grant(chat):
    authorize(chat)
    fresh = Conversation(chat.client)
    assert fresh.state["caller"]["name"] == "David Chen"
    assert "David Chen" in fresh.reply and "representative authorization" in fresh.reply
    assert len(fresh.state["available_claims"]) == 4
    fresh.say(action="form", form={"case_id": "CL-2048"})
    assert fresh.state["memory"]["case_history"]["turn_count"] == 1


def test_pending_consent_does_not_repeat_or_expose_claim(chat):
    chat.say(REP)
    chat.say(action="consent_timeout")
    assert chat.reply.count("pending") == 1
    assert len(chat.reply.split()) < 35
    assert "statement in chat" not in chat.reply.lower()
    assert not chat.state["verified"]


def test_claim_confirmation_waits_and_can_decline(chat):
    chat.say(REP)
    chat.say(action="consent_check")
    chat.say("Can the deadline be extended?")
    assert chat.state["selected_case_id"] is None
    assert not chat.state["evidence"]
    assert "confirm CL-2048" in chat.reply
    chat.say("no")
    assert chat.state["claim_confirmation"] is None
    chat.say(action="form", form={"case_id": "CL-2102"})
    assert chat.state["selected_case_id"] == "CL-2102"


@pytest.mark.parametrize(
    "ending",
    [
        "no, that's all, thanks",
        "No, that’s all, thanks.",
        "that's all",
        "nothing else, thank you",
        "没别的了，谢谢",
        "没有了",
    ],
)
def test_explicit_end_opens_summary_without_another_question(verified, ending):
    verified.say(ending)
    assert verified.state["draft"]
    assert "anything else" not in verified.reply.lower()


def test_plain_thanks_still_checks_for_questions(verified):
    verified.say("thanks")
    assert verified.state["closing_pending"]
    assert not verified.state["draft"]


def ticket(chat):
    return chat.client.get(f"/v1/sessions/{chat.state['session_id']}").json()["handoffs"][0]


def offer_note(chat):
    chat.say(action="human")
    hid = chat.state["handoff"]["id"]
    chat.say("Can the deadline be extended for me?")
    assert chat.state["handoff_note_offer"]["id"] == hid
    assert "Nothing has been added yet" in chat.reply
    return hid


def test_add_question_needs_consent_and_is_idempotent(verified):
    hid = offer_note(verified)
    before = ticket(verified)
    assert not any("extended for me" in m["text"] for m in before["timeline"])
    body = {
        "message_id": "note-consent",
        "expected_version": verified.state["version"],
        "action": "handoff_note_accept",
    }
    url = f"/v1/sessions/{verified.state['session_id']}/messages"
    result = verified.client.post(url, json=body)
    assert result.status_code == 200
    assert "added to " + hid in result.json()["messages"][-1]["text"]
    assert verified.client.post(url, json=body).status_code == 200
    after = ticket(verified)
    assert len(after["timeline"]) == len(before["timeline"]) + 1


@pytest.mark.parametrize("answer", ["no thanks", "What is the claim amount?"])
def test_decline_or_interrupt_does_not_append_question(verified, answer):
    offer_note(verified)
    before = ticket(verified)
    verified.say(answer)
    assert verified.state["handoff_note_offer"] is None
    assert ticket(verified)["revision"] == before["revision"]
    verified.say(action="handoff_note_accept")
    assert "no longer current" in verified.reply
    assert ticket(verified)["revision"] == before["revision"]


def test_case_switch_revokes_note_offer(verified):
    offer_note(verified)
    before = ticket(verified)
    verified.say(action="form", form={"case_id": "CL-2102"})
    verified.say(action="handoff_note_accept")
    assert "no longer current" in verified.reply
    assert ticket(verified)["revision"] == before["revision"]


def test_legacy_rep_draft_withdrawn_without_losing_authorization(chat, app):
    authorize(chat)
    chat.say(action="finish")
    old_offer = chat.state["draft"]["id"]
    with app.state.store.tx() as db:
        row = db.execute("SELECT state FROM sessions WHERE id=?", (chat.state["session_id"],)).fetchone()
        saved = app.state.store.unseal(row[0])
        saved.pop("consultation_schema")
        saved["messages"][-1]["text"] = "OLD PRIVATE CONVERSATION MARKER"
        saved["draft"]["body"] = "OLD PRIVATE CONVERSATION MARKER"
        db.execute(
            "UPDATE sessions SET state=? WHERE id=?", (app.state.store.seal(saved), saved["session_id"])
        )
    chat.refresh()
    assert chat.state["verified"]
    assert chat.state["caller"]["name"] == "David Chen"
    assert chat.state["draft"] is None
    assert "OLD PRIVATE" not in json.dumps(chat.state)
    assert "withdrawn" in chat.reply
    response = chat.client.post(
        f"/v1/sessions/{chat.state['session_id']}/messages",
        json={
            "message_id": "stale-send",
            "expected_version": chat.state["version"],
            "action": "email_send",
            "offer_id": old_offer,
        },
    )
    assert response.status_code == 409


def test_missing_caller_name_does_not_infer_david_from_relationship(chat):
    chat.say("calling for my mother Margaret Chen. DOB 1985-03-15, email margaret@email.com")
    assert not chat.state["verified"]
    assert chat.state["representative_consent"]["status"] == "identity"
    assert "own full name" in chat.reply
    chat.say("My name is David Chen")
    assert chat.state["identity_count"] == 3
    assert chat.state["representative_consent"]["status"] == "pending"


def test_model_inputs_contain_only_current_consultation(verified, app):
    from app.interpreter import OfflineInterpreter

    verified.say("Why did nobody tell me?")
    verified.say("What if I cannot get the report?")
    verified.say(action="human")
    old_hid = verified.state["handoff"]["id"]
    logout(verified)
    authorize(verified)
    seen = []

    class Writer(OfflineInterpreter):
        async def answer(self, context):
            seen.append(context)
            return (
                "The records do not establish an extension.",
                set(),
                ["deadline"],
                {"mode": "test", "calls": 0},
            )

    app.state.engine.interpreter = Writer()
    verified.say("Can the deadline be extended?")
    prompt = json.dumps(seen[-1])
    assert "nobody tell" not in prompt
    assert "cannot get the report" not in prompt
    assert "notification_history" not in prompt
    assert old_hid not in prompt
    assert seen[-1]["caller"]["name"] == "David Chen"
    assert seen[-1]["data_subject"]["name"] == "Margaret Chen"


def test_prior_policyholder_ticket_and_email_not_exposed_to_rep(verified, app):
    verified.say(action="human")
    hid = verified.state["handoff"]["id"]
    verified.say(action="finish")
    verified.say(action="email_send", offer_id=verified.state["draft"]["id"])
    app.state.store.dispatch()
    logout(verified)
    authorize(verified)
    assert (
        verified.client.get(
            f"/v1/handoffs/{hid}", params={"session_id": verified.state["session_id"]}
        ).status_code
        == 404
    )
    account = verified.client.get(f"/v1/sessions/{verified.state['session_id']}/account").json()
    assert not account["emails"]


def test_cancellation_invalidates_add_question_consent(verified):
    offer_note(verified)
    current = ticket(verified)
    result = verified.client.post(
        f"/v1/handoffs/{current['id']}/updates",
        json={
            "session_id": verified.state["session_id"],
            "message_id": "cancel-before-consent",
            "expected_revision": current["revision"],
            "action": "cancel",
            "text": "",
        },
    )
    assert result.status_code == 200
    verified.refresh()
    verified.say(action="handoff_note_accept")
    assert "no longer current" in verified.reply
    assert ticket(verified)["revision"] == current["revision"] + 1


def test_consent_request_and_human_creation_both_announced(chat):
    chat.say(REP + " I need human help.")
    assert not chat.state["verified"]
    assert chat.state["representative_consent"]["status"] == "pending"
    assert "consent request has been started" in chat.reply
    assert "Created human handling request" in chat.reply


def test_confirmation_is_server_owned_not_model_case_guess(chat, app):
    from app.interpreter import OfflineInterpreter

    chat.say(REP)
    chat.say(action="consent_check")

    class Observer(OfflineInterpreter):
        async def observe(self, text, context):
            raise AssertionError("Explicit confirmation must not ask a model to re-interpret the claim")

    app.state.engine.interpreter = Observer()
    chat.say(action="confirm_claim")
    assert chat.state["selected_case_id"] == "CL-2048"
    assert chat.state["messages"][-2]["role"] == "system"


@pytest.mark.parametrize("text", ["yes but not that claim", "yes, but no, the other one"])
def test_qualified_yes_does_not_confirm_the_wrong_claim(chat, text):
    chat.say(REP)
    chat.say(action="consent_check")
    chat.say(text)
    assert chat.state["selected_case_id"] is None
    assert chat.state["phase"] == "RESOLVE_INTENT"


def test_summary_topics_reflect_spoken_answer_not_unused_evidence(chat, app):
    from app.interpreter import OfflineInterpreter

    class Writer(OfflineInterpreter):
        async def answer(self, context):
            return (
                "Claim CL-2048 remains denied.",
                {"CL-2048:status,case_type,created_at"},
                ["status"],
                {"mode": "test", "calls": 0},
            )

    app.state.engine.interpreter = Writer()
    chat.say(
        "I'm Margaret Chen, DOB 1985-03-15, email margaret@email.com. My denied healthcare claim from January."
    )
    assert chat.state["memory"]["case_history"]["discussed_topics"] == ["status"]


@pytest.mark.parametrize(
    "action,text",
    [
        ("continue", "I, David Chen, am assisting you with this matter."),
        ("continue", "I am Margaret Chen."),
        ("continue", "I can email your registered address."),
        ("summary", "Discussion: David Chen called about the claim."),
        ("summary", "Discussion: The representative handled this summary with consent."),
    ],
)
def test_model_cannot_impersonate_caller_or_invent_recipient(action, text):
    from app.responder import AnswerDraft, Paragraph, validate_references

    context = {
        "current_action": action,
        "caller": {"role": "representative", "name": "David Chen"},
        "data_subject": {"name": "Margaret Chen"},
        "authorized_evidence": [],
        "server_guidance": "",
        "business_date": "2026-11-15",
        "current_message": "",
    }
    with pytest.raises(ValueError, match="impersonate|recipient|inserts|registered email"):
        validate_references(
            AnswerDraft(paragraphs=[Paragraph(text=text, evidence_ids=[])], answered_topics=[]), context
        )


def test_summary_writer_receives_facts_without_actor_metadata():
    from app.responder import writing_facts

    context = {
        "current_action": "summary",
        "caller": {"name": "David Chen"},
        "data_subject": {"name": "Margaret Chen"},
        "authorization_method": "policyholder_consent",
        "authorized_evidence": [{"id": "claim", "text": "Current claim fact"}],
    }
    facts = writing_facts(context)
    assert not {"caller", "data_subject", "authorization_method"} & facts.keys()
    assert facts["authorized_evidence"] == context["authorized_evidence"]
    assert context["caller"]["name"] == "David Chen"
    context["current_action"] = "continue"
    assert writing_facts(context)["caller"]["name"] == "David Chen"


def test_legacy_representative_correspondence_requires_explicit_caller_provenance():
    from app.consultation import correspondence_visible

    state = {"representative": {"caller": "David Chen"}}
    old = {
        "summary": {
            "representative_context": {"caller": "David Chen"},
            "discussion": ["Imported historical text"],
        }
    }
    assert not correspondence_visible(state, old)
    assert correspondence_visible(state, {"caller": {"role": "representative", "name": "David Chen"}})
