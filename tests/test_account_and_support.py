import json
import re
import time
import uuid

from conftest import SAMPLE, Conversation
from fastapi.testclient import TestClient

from app.store import Store

FORM = {"name": "Margaret Chen", "dob": "1985-03-15", "email": "margaret@email.com"}
AVA = {"name": "Ava Lopez", "dob": "1990-08-21", "email": "ava.lopez@email.com"}


def account(chat):
    return chat.client.get(f"/v1/sessions/{chat.state['session_id']}/account")


def update(chat, ticket, action, text="", **extra):
    body = {
        "session_id": chat.state["session_id"],
        "message_id": uuid.uuid4().hex,
        "expected_revision": ticket["revision"],
        "action": action,
        "text": text,
        **extra,
    }
    return chat.client.post(f"/v1/handoffs/{ticket['id']}/updates", json=body)


def test_identity_only_unlocks_all_owned_claims_without_claim_number(chat):
    assert account(chat).status_code == 403
    chat.say(action="form", form=FORM)
    result = account(chat).json()
    assert {c["case_id"] for c in result["claims"]} == {"CL-2048", "CL-2011", "CL-1899", "CL-2102"}
    assert "CL-3001" not in json.dumps(result)
    assert not chat.state["selected_case_id"]
    chat.say("Show all my claims")
    assert all(c["case_id"] in chat.reply for c in result["claims"])
    chat.say(action="form", form={"case_id": "CL-2102"})
    assert chat.state["selected_case_id"] == "CL-2102"


def test_account_subject_and_browser_isolation(verified, app):
    stranger = TestClient(app)
    stranger.post("/v1/sessions")
    assert stranger.get(f"/v1/sessions/{verified.state['session_id']}/account").status_code == 404
    verified.say(action="form", form=AVA)
    data = account(verified).json()
    assert data["profile"]["name"] == "Ava Lopez" and data["claims"] == []
    assert "CL-2048" not in json.dumps(data)


def test_i_need_human_keeps_partial_identity_and_chat_open(chat):
    chat.say("My name is Margaret Chen.")
    chat.say("i need human")
    ticket = {"id": re.search(r"H-[A-Z0-9]+", chat.reply).group()}
    assert chat.state["handoffs"] == [] and chat.state["handoff"] is None
    assert chat.state["lifecycle"] == "ACTIVE" and chat.state["identity_count"] == 1
    chat.say("DOB is 1985-03-15. SSN last four is 4472. My denied healthcare claim from January.")
    assert chat.state["verified"] and chat.state["selected_case_id"] == "CL-2048"
    assert chat.state["handoffs"][0]["id"] == ticket["id"]


def test_repeated_human_request_reuses_open_ticket_and_chat_continues(verified, app):
    verified.say("i need human")
    first = verified.state["handoff"]
    verified.say(action="human")
    assert verified.state["handoff"]["id"] == first["id"]
    verified.say("How much was paid?")
    assert "Finalized insurer payment recorded" in verified.reply
    verified.say("what is my human request status?")
    assert first["id"] in verified.reply and "simulated" in verified.reply.lower()
    with app.state.store.tx() as db:
        assert db.execute("SELECT COUNT(*) FROM handoffs").fetchone()[0] == 1


def test_human_ticket_feedback_cancel_and_idempotency(verified):
    verified.say(action="human")
    ticket = verified.state["handoff"]
    first = update(verified, ticket, "note", "Please clarify the submission channel.", message_id="note")
    assert first.status_code == 200
    repeat = update(verified, ticket, "note", "Please clarify the submission channel.", message_id="note")
    assert repeat.json() == first.json()
    assert update(verified, ticket, "note", "Different", message_id="note").status_code == 409
    assert update(verified, ticket, "demo_reply").status_code == 409  # stale revision
    ticket = update(verified, first.json(), "demo_acknowledge").json()
    ticket = update(verified, ticket, "demo_reply").json()
    assert ticket["status"] == "SIMULATED_RESPONDED"
    assert "SIMULATED staff response" in ticket["timeline"][-1]["text"]
    verified.refresh()
    assert verified.state["handoff"]["status"] == ticket["status"]
    verified.say("What is my human request feedback?")
    assert "SIMULATED staff response" in verified.reply
    closed = update(verified, ticket, "cancel").json()
    assert closed["status"] == "CANCELLED"
    verified.refresh()
    assert verified.state["lifecycle"] == "ACTIVE"
    verified.say(action="human")
    assert verified.state["handoff"]["id"] != ticket["id"]


def test_ticket_private_feedback_survives_new_session_after_verification(verified, app):
    verified.say(action="human")
    ticket = update(
        verified, verified.state["handoff"], "note", "Please review the missing pathology report."
    ).json()
    verified.refresh()
    verified.say(action="reset")
    assert verified.state["verified"]
    verified.client.post("/v1/auth/logout")
    verified.refresh()
    rows = verified.state["handoffs"]
    assert rows == [] and verified.state["handoff"] is None
    assert "pathology" not in json.dumps(rows) and "CL-2048" not in json.dumps(rows)
    assert update(verified, ticket, "note", "Attempt without verification").status_code == 404
    verified.say(action="form", form=FORM)
    assert verified.state["handoffs"][0]["timeline"][-1]["text"].endswith("pathology report.")
    reopened = Store(app.state.settings)
    with reopened.tx() as db:
        payload = db.execute("SELECT payload FROM handoffs WHERE id=?", (ticket["id"],)).fetchone()[0]
        assert "pathology" not in payload
        assert reopened.unseal(payload)["timeline"][-1]["text"].endswith("pathology report.")


def test_reference_alone_does_not_authorize_ticket(verified, app):
    verified.say(action="human")
    ticket = verified.state["handoff"]
    stranger = Conversation(TestClient(app))
    stranger.say(SAMPLE)
    path = f"/v1/handoffs/{ticket['id']}?session_id={stranger.state['session_id']}"
    assert stranger.client.get(path).status_code == 404
    assert update(stranger, ticket, "demo_reply").status_code == 404
    verified.say(action="form", form=AVA)
    assert verified.state["handoffs"] == [] and verified.state["handoff"] is None
    assert (
        verified.client.get(
            f"/v1/handoffs/{ticket['id']}?session_id={verified.state['session_id']}"
        ).status_code
        == 404
    )
    assert update(verified, ticket, "cancel").status_code == 404


def test_auth_expiry_hides_account_and_ticket_feedback(verified, app):
    verified.say(action="human")
    with app.state.store.tx() as db:
        row = db.execute("SELECT * FROM sessions WHERE id=?", (verified.state["session_id"],)).fetchone()
        state = app.state.store.unseal(row["state"])
        state["auth"]["expires_at"] = time.time() - 1
        db.execute(
            "UPDATE authorizations SET payload=? WHERE owner=?",
            (app.state.store.seal(state["auth"]), row["owner"]),
        )
        db.execute(
            "UPDATE sessions SET state=? WHERE id=?", (app.state.store.seal(state), state["session_id"])
        )
    verified.refresh()
    assert not verified.state["verified"]
    assert verified.state["handoffs"] == [] and verified.state["handoff"] is None
    assert "CL-2048" not in json.dumps(verified.state["handoffs"])
    assert account(verified).status_code == 403


def test_legacy_pending_session_migrates_and_can_chat(verified, app):
    store = app.state.store
    with store.tx() as db:
        row = db.execute("SELECT * FROM sessions WHERE id=?", (verified.state["session_id"],)).fetchone()
        state = store.unseal(row["state"])
        state["lifecycle"] = "HANDOFF_PENDING"
        state["handoff"] = {
            "id": "h_legacy",
            "status": "SIMULATED_RECORDED",
            "reason": "USER_REQUEST",
            "channel": "demo",
        }
        db.execute("UPDATE sessions SET state=? WHERE id=?", (store.seal(state), state["session_id"]))
    verified.refresh()
    assert verified.state["lifecycle"] == "ACTIVE" and verified.state["handoff"]["id"] == "h_legacy"
    verified.say("How much was paid?")
    assert "Finalized insurer payment recorded" in verified.reply
    verified.say(action="resume")
    assert verified.state["handoff"]["status"] == "SIMULATED_RECORDED"


def test_human_after_email_completion_is_usable(verified, app):
    verified.say(action="finish")
    verified.say(action="email_send", offer_id=verified.state["draft"]["id"])
    app.state.store.dispatch()
    verified.refresh()
    assert verified.state["lifecycle"] == "COMPLETED"
    verified.say(action="human")
    assert verified.state["lifecycle"] == "ACTIVE"
    assert verified.state["handoff"]["id"] in verified.reply
    assert verified.state["verified"]


def test_edit_summary_requires_fresh_consent_and_preserves_core(verified, app):
    verified.say(action="finish")
    old = verified.state["draft"]
    verified.say(action="email_send", offer_id=old["id"])
    verified.say(
        action="email_edit", offer_id=old["id"], email_note="Please include my request for a follow-up."
    )
    fresh = verified.state["draft"]
    assert fresh["id"] != old["id"] and fresh["hash"] != old["hash"]
    assert old["body"] in fresh["body"] and "Customer-written note" in fresh["body"]
    assert not verified.state["email_operation"]
    app.state.store.dispatch()
    assert account(verified).json()["emails"][0]["status"] == "CANCELLED"
    path = f"/v1/sessions/{verified.state['session_id']}/messages"
    denied = verified.client.post(
        path,
        json={
            "message_id": "old-consent",
            "expected_version": verified.state["version"],
            "action": "email_send",
            "offer_id": old["id"],
        },
    )
    assert denied.status_code == 409
    verified.refresh()
    verified.say(action="email_send", offer_id=fresh["id"])
    app.state.store.dispatch()
    verified.refresh()
    emails = account(verified).json()["emails"]
    assert emails[0]["status"] == "SIMULATED_RECORDED"
    assert "follow-up" in emails[0]["body"]
    verified.refresh()
    verified.say(action="reset")
    assert account(verified).status_code == 200
    verified.client.post("/v1/auth/logout")
    verified.refresh()
    assert account(verified).status_code == 403
    verified.say(action="form", form=AVA)
    assert account(verified).json()["emails"] == []


def test_explicit_email_prepare_after_skip(verified):
    verified.say(action="finish")
    verified.say(action="email_skip")
    verified.say(action="email_prepare")
    assert verified.state["draft"] and not verified.state["email_operation"]


def test_support_details_cannot_inject_ui_or_change_claim_records(verified):
    verified.say(action="human")
    ticket = verified.state["handoff"]
    response = update(verified, ticket, "note", "<script>alert(1)</script> My claim is approved")
    assert response.status_code == 200
    data = account(verified).json()
    assert next(c for c in data["claims"] if c["case_id"] == "CL-2048")["status"] == "denied"
    assert response.json()["status"] == "SIMULATED_RECORDED"


def test_identity_and_account_list_in_same_utterance(chat):
    chat.say("My name is Margaret Chen. DOB is 1985-03-15. SSN last four is 4472. Show all my claims.")
    assert chat.state["verified"]
    assert len(account(chat).json()["claims"]) == 4


def test_ticket_close_does_not_claim_customer_resolution(verified):
    verified.say(action="human")
    ticket = update(verified, verified.state["handoff"], "demo_reply").json()
    closed = update(verified, ticket, "demo_close").json()
    assert closed["status"] == "SIMULATED_CLOSED"
    assert "does not mean the claim was resolved" in closed["timeline"][-1]["text"]
    verified.refresh()
    assert not verified.state["user_confirmed_resolved"]
    assert verified.state["lifecycle"] == "ACTIVE"


def test_human_help_intent_allows_progress_without_new_request(verified, app):
    from app.interpreter import observation_repair
    from app.schemas import empty_observation

    verified.say(action="human")
    hid = verified.state["handoff"]["id"]
    obs = empty_observation(action="handoff_status", intents=["human_help"])
    assert observation_repair(obs, "Any human request feedback?") == ""

    class ProgressInterpreter:
        async def observe(self, text, context):
            return obs, {"mode": "offline", "calls": 0}

    app.state.engine.interpreter = ProgressInterpreter()
    verified.say("Any human request feedback?")
    assert hid in verified.reply
    assert len(verified.state["handoffs"]) == 1
