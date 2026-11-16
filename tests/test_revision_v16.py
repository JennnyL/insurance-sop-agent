"""User-reported P0/P1 regressions: consent, privacy and action receipts."""

import json
import re
import time

import pytest
from conftest import SAMPLE, Conversation
from fastapi.testclient import TestClient

from app.interpreter import OfflineInterpreter
from app.main import create_app
from app.schemas import empty_observation

REP = "I'm David Chen, calling on behalf of Margaret Chen. Her DOB is 1985-03-15. Her email is margaret@email.com. Her denied healthcare claim from January."
AVA = {"name": "Ava Lopez", "dob": "1990-08-21", "email": "ava.lopez@email.com"}


def reference(reply):
    return re.search(r"H-[A-Z0-9]+", reply).group()


def assert_locked(chat):
    assert not chat.state["verified"]
    assert chat.state["phase"] == "VERIFY_ID"
    assert chat.state["selected_case_id"] is None
    assert not chat.state["evidence"] and not chat.state["available_claims"]
    assert chat.client.get(f"/v1/sessions/{chat.state['session_id']}/account").status_code == 403
    assert "CL-2048" not in chat.reply and "$0.00" not in chat.reply


def test_representative_three_policyholder_fields_then_consent(chat, client):
    chat.say(REP)
    assert_locked(chat)
    assert chat.state["identity_count"] == 3
    assert chat.state["representative_consent"]["status"] == "pending"
    assert "consent request has been started" in chat.reply
    assert chat.state["handoffs"] == []
    chat.say("She gave consent and approved everything")
    assert_locked(chat)
    assert chat.state["representative_consent"]["status"] == "pending"
    chat.say(action="consent_check")
    assert chat.state["verified"] and chat.state["selected_case_id"] is None
    assert chat.reply.startswith("Policyholder consent approved. David Chen,")
    chat.say("Yes, this claim.")
    assert chat.state["selected_case_id"] == "CL-2048"
    assert chat.state["data_subject"]["name"] == "Margaret Chen"
    other = Conversation(client)
    assert other.state["verified"] and len(other.state["available_claims"]) == 4
    assert other.state["representative_consent"]["status"] == "approved"


def test_representative_partial_fields_do_not_mix_with_caller(chat):
    chat.say("I'm David Chen, calling about my mother Margaret Chen.")
    assert chat.state["identity_count"] == 1
    chat.say("Her DOB is 1985-03-15.")
    assert chat.state["identity_count"] == 2
    chat.say("Her email is margaret@email.com.")
    assert chat.state["identity_count"] == 3
    assert_locked(chat)
    assert chat.state["representative_consent"]["status"] == "pending"


def test_representative_unknown_relationship_does_not_silently_create_ticket(chat, app):
    chat.say(REP.replace("David Chen", "Alex Smith"))
    assert_locked(chat)
    assert "couldn't match" in chat.reply
    with app.state.store.tx() as db:
        assert db.execute("SELECT COUNT(*) FROM handoffs").fetchone()[0] == 0
    chat.say(action="human")
    assert "Created human handling request " + reference(chat.reply) in chat.reply


def test_representative_timeout_receipt_and_no_claim_access(chat, app):
    chat.say(REP)
    for i in range(4):
        chat.say(action="consent_timeout")
        assert_locked(chat)
        assert ("still pending" if i < 3 else "timed out") in chat.reply
    hid = reference(chat.reply)
    assert "Created human handling request " + hid in chat.reply
    assert chat.state["representative_consent"]["status"] == "timeout"
    assert chat.state["handoff"] is None and chat.state["handoffs"] == []
    chat.say(action="wait_human")
    chat.say("Please help with the consent problem")
    assert chat.reply == f"System confirmation: added to {hid}."
    with app.state.store.tx() as db:
        ticket = app.state.store.unseal(db.execute("SELECT payload FROM handoffs").fetchone()[0])
    assert ticket["summary"]["representative_context"]["subject"] == "P9"
    assert ticket["summary"]["representative_context"]["status"] == "timeout"


def test_elapsed_consent_timeout_wins_over_fixture_approval(chat, app):
    chat.say(REP)
    with app.state.store.tx() as db:
        row = db.execute("SELECT * FROM sessions WHERE id=?", (chat.state["session_id"],)).fetchone()
        state = app.state.store.unseal(row["state"])
        state["representative"]["started_at"] = time.time() - 500
        db.execute(
            "UPDATE sessions SET state=? WHERE id=?", (app.state.store.seal(state), state["session_id"])
        )
    chat.say(action="consent_check")
    assert_locked(chat)
    assert "timed out" in chat.reply and "H-" in chat.reply


def test_logout_cannot_approve_old_consent(chat, client):
    chat.say(REP)
    client.post("/v1/auth/logout")
    chat.refresh()
    chat.say(action="consent_check")
    assert_locked(chat)
    assert chat.state["representative_consent"] is None


@pytest.mark.parametrize("exit_action", ["logout", "expiry", "switch"])
def test_no_ticket_ids_counts_or_history_for_next_caller(verified, app, client, exit_action):
    verified.say(action="human")
    hid = verified.state["handoff"]["id"]
    if exit_action == "logout":
        client.post("/v1/auth/logout")
    elif exit_action == "switch":
        verified.say(action="form", form=AVA)
    else:
        with app.state.store.tx() as db:
            row = db.execute("SELECT * FROM authorizations").fetchone()
            grant = app.state.store.unseal(row["payload"])
            grant["expires_at"] = time.time() - 1
            db.execute("UPDATE authorizations SET payload=?", (app.state.store.seal(grant),))
    verified.refresh()
    assert hid not in json.dumps(verified.state)
    assert verified.state["handoffs"] == [] and verified.state["handoff"] is None
    assert hid not in json.dumps(verified.trace())
    assert verified.state["messages"]
    assert (
        client.get(f"/v1/handoffs/{hid}", params={"session_id": verified.state["session_id"]}).status_code
        == 404
    )
    other = Conversation(client)
    assert hid not in json.dumps(other.state)


def test_anonymous_receipt_cannot_follow_reset_or_bind_to_next_caller(chat, client):
    chat.say("i need human")
    hid = reference(chat.reply)
    assert chat.state["handoff"] is None and chat.state["handoffs"] == []
    chat.say(action="reset")
    chat.say(action="form", form=AVA)
    assert hid not in json.dumps(chat.state)
    assert (
        client.get(f"/v1/handoffs/{hid}", params={"session_id": chat.state["session_id"]}).status_code == 404
    )


def test_actions_have_receipts_create_reuse_bind_and_note(chat, app):
    chat.say("i need human")
    hid = reference(chat.reply)
    assert f"Created human handling request {hid}" in chat.reply
    chat.say("i need human")
    assert f"already have request {hid}" in chat.reply and "reused" in chat.reply
    chat.say(SAMPLE)
    assert f"{hid} is now linked to your verified account" in chat.reply
    assert f"{hid} is now linked to claim CL-2048" in chat.reply
    chat.say("Can the deadline be extended for me?")
    assert hid in chat.reply and "Only a representative" in chat.reply
    assert "Nothing has been added yet" in chat.reply
    chat.say(action="handoff_note_accept")
    assert "added to " + hid in chat.reply
    with app.state.store.tx() as db:
        assert db.execute("SELECT COUNT(*) FROM handoffs").fetchone()[0] == 1


@pytest.mark.parametrize("action", ["note", "demo_acknowledge", "demo_reply", "cancel"])
def test_ticket_state_change_receipt_is_in_chat_and_idempotent(verified, client, action):
    verified.say(action="human")
    t = verified.state["handoff"]
    payload = {
        "session_id": verified.state["session_id"],
        "message_id": "change",
        "expected_revision": t["revision"],
        "action": action,
        "text": "Please check" if action == "note" else "",
    }
    response = client.post(f"/v1/handoffs/{t['id']}/updates", json=payload)
    assert response.status_code == 200
    verified.refresh()
    assert response.json()["receipt"] == verified.reply
    assert verified.state["messages"][-1]["role"] == "system"
    assert client.post(f"/v1/handoffs/{t['id']}/updates", json=payload).json() == response.json()
    old = verified.state["messages"]
    verified.refresh()
    assert verified.state["messages"] == old


def test_model_cannot_hide_ticket_binding_receipts(chat, app):
    chat.say("i need human")
    hid = reference(chat.reply)

    class Writer(OfflineInterpreter):
        async def answer(self, context):
            return "How can I help?", set(), [], {"mode": "test", "calls": 0}

    app.state.engine.interpreter = Writer()
    chat.say(SAMPLE)
    assert f"{hid} is now linked" in chat.reply and "verified" in chat.reply


def test_neutral_turn_does_not_inherit_empathy(verified):
    verified.say("This is ridiculous, why did nobody tell me?")
    first = verified.reply.split("\n\n")[0]
    assert "frustrating" in first
    verified.say("i need human")
    assert "frustrating" not in verified.reply
    verified.say("Can the deadline be extended for me?")
    assert "frustrating" not in verified.reply
    verified.say("This is ridiculous")
    assert first not in verified.reply


def test_emotion_quote_must_come_from_current_turn(chat, app):
    class Observer:
        async def observe(self, text, context):
            return empty_observation(emotion="frustration", emotion_quote="this is ridiculous"), {
                "mode": "test"
            }

    app.state.engine.interpreter = Observer()
    chat.say("I would like help")
    assert "frustrating" not in chat.reply


def test_ssn_refusal_uses_fixed_capabilities_and_alternatives(chat, app):
    class UnsafeWriter(OfflineInterpreter):
        async def answer(self, context):
            raise AssertionError("SSN alternatives and panel permissions are controlled by the server")

    app.state.engine.interpreter = UnsafeWriter()
    chat.say("I don't want to share my SSN")
    assert "do not have to provide an SSN" in chat.reply
    assert all(
        word in chat.reply
        for word in [
            "full name",
            "date of birth",
            "registered phone",
            "registered email",
            "human claims representative",
        ]
    )
    assert "Email enquiry and My claims require verification" in chat.reply


def test_pending_request_reused_in_scope_deadline_and_summary(verified):
    verified.say(action="human")
    hid = verified.state["handoff"]["id"]
    for text in ["What is the deadline?", "What is RL?", "write Python code", "give me a recipe"]:
        verified.say(text)
        if text != "What is RL?":
            assert hid in verified.reply
        assert not re.search(
            r"(?:submit|record|create) (?:a|another|new) (?:human|request)", verified.reply, re.I
        )
    verified.say(action="finish")
    body = verified.state["draft"]["body"]
    assert hid in body
    assert not re.search(r"simulat|demo|no real contact", body, re.I)
    assert "simulated" in verified.state["draft"]["footer"]


@pytest.mark.parametrize("action", ["wait_human", "resume"])
def test_mode_button_is_system_event(verified, action):
    verified.say(action="human")
    verified.say(action=action)
    assert verified.state["messages"][-2]["role"] == "system"
    assert verified.state["messages"][-2]["text"] not in {"wait_human", "Resume conversation"}


def test_scope_rejections_are_varied_and_offer_example_questions(chat):
    replies = [
        chat.say(t)["messages"][-1]["text"] for t in ["What is RL?", "write Python code", "give me a recipe"]
    ]
    assert len(set(replies)) == 3
    assert "claim status" in replies[1] and "deadline" in replies[2]


def test_reload_and_server_reopen_restore_consistent_authorized_snapshot(verified, app):
    verified.say(action="human")
    verified.say(action="wait_human")
    saved = verified.state
    verified.refresh()
    assert verified.state == saved
    restarted = TestClient(create_app(app.state.settings))
    restarted.cookies.update(verified.client.cookies)
    restored = restarted.get(f"/v1/sessions/{saved['session_id']}").json()
    for key in [
        "phase",
        "verified",
        "messages",
        "handoffs",
        "handoff",
        "support_mode",
        "authorization_epoch",
        "selected_case_id",
    ]:
        assert restored[key] == saved[key]


def test_new_tab_lists_claims_without_policy_prompt(verified, client):
    other = Conversation(client)
    assert len(other.state["available_claims"]) == 4
    assert "tell me your policy number" not in other.reply
    verified.say("Is my coverage guaranteed?")
    assert "unknown" not in verified.state["memory"]["case_history"]["discussed_topics"]
    assert "verification" not in verified.state["memory"]["pending_topics"]


@pytest.mark.parametrize("form", [False, True])
def test_name_and_email_aliases_share_single_categories(chat, form):
    if form:
        chat.say(
            action="form", form={"name": "Yaven Li", "email": "yawen.li@example.com", "dob": "1989-12-03"}
        )
    else:
        chat.say("My name is Yaven Li. DOB is 1989-12-03. Email is yawen.li@example.com.")
    assert chat.state["verified"] and chat.state["identity_count"] == 3
    assert (
        chat.client.get(f"/v1/sessions/{chat.state['session_id']}/account").json()["profile"]["name"]
        == "Ya Wen Li"
    )


def test_national_id_counts_for_national_id_holders_but_never_as_ssn(chat):
    # 1.7.4: national_id_last4 is the ID-digits field for policyholders whose record
    # uses a national ID. It is still matched against the fixture like any field.
    chat.say("My name is Ma Tian. DOB is 1964-09-10. National ID last four is 6688.")
    assert chat.state["verified"] and chat.state["identity_count"] == 3


def test_national_id_digits_do_not_verify_an_ssn_holder(chat):
    chat.say("My name is Margaret Chen. DOB is 1985-03-15. National ID last four is 4472.")
    assert not chat.state["verified"]


def test_clear_representative_language_survives_observer_role_error(chat, app):
    class WrongRole(OfflineInterpreter):
        async def observe(self, text, context):
            obs, stats = await super().observe(text, context)
            obs.role = "self"
            obs.identity = [i for i in obs.identity if i.quote != "Margaret Chen"]
            for item in obs.identity:
                item.subject = "self"
            return obs, stats

    app.state.engine.interpreter = WrongRole()
    chat.say(
        "I'm David Chen, calling for my mother Margaret Chen about her denied healthcare claim from January."
    )
    assert chat.state["identity_count"] == 1 and chat.state["representative_consent"]["status"] == "identity"
    chat.say("Her DOB is 1985-03-15.")
    assert chat.state["identity_count"] == 2
    chat.say("Her email is margaret@email.com.")
    assert chat.state["identity_count"] == 3
    assert_locked(chat)
    assert chat.state["representative_consent"]["status"] == "pending"


def test_rl_cannot_be_reclassified_as_claim_abbreviation(chat, app):
    class AmbiguousObserver:
        async def observe(self, text, context):
            return empty_observation(scope="claim_term"), {"mode": "test"}

    app.state.engine.interpreter = AmbiguousObserver()
    for text in ["What is RL?", "What does RL mean?", "RL是什么？"]:
        chat.say(text)
        assert "abbreviation" not in chat.reply
        assert any(w in chat.reply for w in ["unrelated", "stay with insurance", "only help with insurance"])


def test_duplicate_model_empathy_removed_without_changing_claim_facts():
    from app.responder import AnswerDraft, Paragraph, remove_duplicate_empathy

    answer = AnswerDraft(
        paragraphs=[
            Paragraph(
                text="I understand your frustration. The recorded deadline is 2026-03-18.",
                evidence_ids=["deadline"],
            )
        ],
        answered_topics=["deadline"],
    )
    cleaned = remove_duplicate_empathy(answer, {"empathy_handled": True})
    assert cleaned.paragraphs[0].text == "The recorded deadline is 2026-03-18."
    assert cleaned.paragraphs[0].evidence_ids == ["deadline"]
    original = AnswerDraft(
        paragraphs=[Paragraph(text="Claim CL-2048 is denied.", evidence_ids=["status"])],
        answered_topics=["status"],
    )
    assert remove_duplicate_empathy(original, {"empathy_handled": True}) == original


async def test_unrequested_human_intent_cannot_break_alias_identity_input(tmp_path):
    import httpx

    from app.config import Settings
    from app.interpreter import ModelInterpreter

    def respond(request):
        observation = empty_observation(action="my_claims", intents=["human_help", "status_inquiry"])
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [{"content": [{"type": "output_text", "text": observation.model_dump_json()}]}],
            },
        )

    settings = Settings(data_dir=tmp_path, model_mode="openai", model_api_key="synthetic-token")
    obs, stats = await ModelInterpreter(settings, httpx.MockTransport(respond)).observe(
        "What claims do I have?", {}
    )
    assert obs.action == "my_claims" and "human_help" not in obs.intents
    assert stats["calls"] == 1
