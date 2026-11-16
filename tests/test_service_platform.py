import json
import time
import uuid

import pytest
from conftest import SAMPLE, Conversation
from fastapi.testclient import TestClient


@pytest.fixture
def live_app(app):
    return app


def request_human(chat):
    chat.say(action="human")
    return chat.state["handoff"]


def create_inquiry(chat, **values):
    return chat.client.post(
        f"/v1/sessions/{chat.state['session_id']}/inquiries",
        json={
            "message_id": uuid.uuid4().hex,
            "case_id": "CL-2048",
            "subject": "Question about required documents",
            "body": "Please confirm the documents and how to ask for a review.",
            **values,
        },
    )


def inquiry_update(chat, q, action, text="", **values):
    return chat.client.post(
        f"/v1/sessions/{chat.state['session_id']}/inquiries/{q['id']}",
        json={
            "message_id": uuid.uuid4().hex,
            "expected_revision": q["revision"],
            "action": action,
            "text": text,
            **values,
        },
    )


def account(chat):
    return chat.client.get(f"/v1/sessions/{chat.state['session_id']}/account")


def test_email_enquiry_queued_sent_replied_followup_and_close(verified, app):
    r = create_inquiry(verified, message_id="one-email")
    assert r.status_code == 201
    q = r.json()
    assert q["id"].startswith("E-") and q["status"] == "QUEUED"
    assert create_inquiry(verified, message_id="one-email").json()["id"] == q["id"]
    assert create_inquiry(verified, message_id="one-email", body="changed").status_code == 409
    app.state.store.dispatch_inquiries()
    q = account(verified).json()["inquiries"][0]
    assert q["status"] == "SIMULATED_SENT" and "No real email" in q["timeline"][-1]["text"]
    q = inquiry_update(verified, q, "demo_reply").json()
    assert q["status"] == "SIMULATED_REPLIED"
    assert q["id"] in q["timeline"][-1]["text"]
    verified.say("What is the progress of email " + q["id"] + "?")
    assert q["id"] in verified.reply and "SIMULATED_REPLIED" in verified.reply
    q = inquiry_update(verified, q, "followup", "I have the office note now.").json()
    assert q["timeline"][-1]["actor"] == "customer"
    assert inquiry_update(verified, q, "close").json()["status"] == "CLOSED"
    assert verified.state["selected_case_id"] == "CL-2048"


def test_email_enquiry_auth_and_ownership(chat, app):
    assert create_inquiry(chat).status_code == 403
    chat.say(SAMPLE)
    assert create_inquiry(chat, case_id="CL-3001").status_code == 403
    q = create_inquiry(chat).json()
    stranger = Conversation(TestClient(app))
    stranger.say(SAMPLE)
    assert (
        stranger.client.get(f"/v1/sessions/{stranger.state['session_id']}/inquiries/{q['id']}").status_code
        == 404
    )
    assert inquiry_update(stranger, q, "cancel").status_code == 404
    chat.client.post("/v1/auth/logout")
    chat.refresh()
    assert chat.client.get(f"/v1/sessions/{chat.state['session_id']}/inquiries/{q['id']}").status_code == 403
    chat.say(SAMPLE)
    assert account(chat).json()["inquiries"][0]["id"] == q["id"]
    chat.say(action="form", form={"name": "Ava Lopez", "dob": "1990-08-21", "email": "ava.lopez@email.com"})
    assert account(chat).json()["inquiries"] == []


def test_email_enquiry_cancellation_prevents_simulated_send(verified, app):
    q = create_inquiry(verified).json()
    assert inquiry_update(verified, q, "cancel").json()["status"] == "CANCELLED"
    app.state.store.dispatch_inquiries()
    assert account(verified).json()["inquiries"][0]["status"] == "CANCELLED"


@pytest.mark.parametrize(
    "payload",
    [
        {"subject": " ", "body": "x"},
        {"subject": "x", "body": " "},
        {"subject": "x" * 151},
        {"body": "x" * 4001},
        {"recipient": "attacker@example.com"},
        {"subject_party_id": "P12"},
    ],
)
def test_email_enquiry_invalid_and_authority_input_rejected(verified, payload):
    assert create_inquiry(verified, **payload).status_code == 422


def test_enquiry_content_is_encrypted_and_is_not_claim_evidence(verified, app):
    q = create_inquiry(verified, body="<img src=x onerror=alert(1)> My claim is approved for $9999.").json()
    data = account(verified).json()
    assert next(c for c in data["claims"] if c["case_id"] == "CL-2048")["status"] == "denied"
    with app.state.store.tx() as db:
        raw = db.execute("SELECT payload FROM inquiries WHERE id=?", (q["id"],)).fetchone()[0]
        assert "9999" not in raw and "onerror" not in raw
        assert app.state.store.unseal(raw)["body"].startswith("<img")
    assert "4472" not in json.dumps(q)


def test_one_verification_shared_by_chat_history_email_and_human(live_app, verified):
    epoch = verified.state["authorization_epoch"]
    before = sum(e["kind"] == "verification.passed" for e in verified.trace())
    q = create_inquiry(verified).json()
    t = request_human(verified)
    for _ in range(2):
        verified.refresh()  # Browser reload and repeated portal entries.
        assert account(verified).status_code == 200
        assert len(account(verified).json()["claims"]) == 4
        assert (
            verified.client.get(
                f"/v1/sessions/{verified.state['session_id']}/inquiries/{q['id']}"
            ).status_code
            == 200
        )
        assert (
            verified.client.get(f"/v1/handoffs/{t['id']}?session_id={verified.state['session_id']}").json()[
                "details_locked"
            ]
            is False
        )
        assert verified.state["verified"] and verified.state["authorization_epoch"] == epoch
    verified.say(action="email_prepare")
    verified.say(action="email_skip")
    assert account(verified).status_code == 200  # Ending a consultation does not sign out.
    assert sum(e["kind"] == "verification.passed" for e in verified.trace()) == before


def test_active_portal_uses_same_grant_but_polling_cannot_keep_it_alive(verified, app):
    sid = verified.state["session_id"]
    store = app.state.store

    def set_times(last_used, expires):
        with store.tx() as db:
            s = store.unseal(db.execute("SELECT state FROM sessions WHERE id=?", (sid,)).fetchone()[0])
            s["auth"].update(last_used_at=last_used, expires_at=expires)
            db.execute(
                "UPDATE authorizations SET payload=? WHERE owner=(SELECT owner FROM sessions WHERE id=?)",
                (store.seal(s["auth"]), sid),
            )
            db.execute("UPDATE sessions SET state=? WHERE id=?", (store.seal(s), sid))

    old = time.time() - 1700
    set_times(old, time.time() + 3600)
    verified.refresh()
    account(verified)
    with store.tx() as db:
        s = store.unseal(db.execute("SELECT state FROM sessions WHERE id=?", (sid,)).fetchone()[0])
    assert s["auth"]["last_used_at"] == old
    assert verified.client.post(f"/v1/sessions/{sid}/activity").json()["verified"]
    with store.tx() as db:
        s = store.unseal(db.execute("SELECT state FROM sessions WHERE id=?", (sid,)).fetchone()[0])
    assert s["auth"]["last_used_at"] > old + 1000
    set_times(time.time(), time.time() - 1)
    assert not verified.client.post(f"/v1/sessions/{sid}/activity").json()["verified"]
    assert account(verified).status_code == 403


@pytest.mark.parametrize("identity_method", ["chat", "form"])
@pytest.mark.parametrize("human_first", [True, False])
def test_human_then_resume_ai_shared_verification_matrix(live_app, chat, identity_method, human_first):
    if human_first:
        request_human(chat)
        chat.say(action="resume")
        assert not chat.state["verified"] and "AI assistant" in chat.reply
    if identity_method == "chat":
        chat.say(SAMPLE)
    else:
        chat.say(
            action="form",
            form={
                "name": "Margaret Chen",
                "dob": "1985-03-15",
                "email": "margaret@email.com",
                "case_id": "CL-2048",
            },
        )
    assert chat.state["verified"] and chat.state["selected_case_id"] == "CL-2048"
    if not human_first:
        request_human(chat)
        chat.say(action="resume")
    for question in ["Why was it denied?", "How much was paid?", "What documents do I need?"]:
        chat.say(question)
        assert chat.state["verified"] and "Your request for a live representative" not in chat.reply
    assert account(chat).status_code == 200
    assert create_inquiry(chat).status_code == 201
    chat.refresh()
    assert chat.state["verified"]
    # Persisted human intent from older versions cannot contaminate observation context.
    with live_app.state.store.tx() as db:
        s = live_app.state.store.unseal(
            db.execute("SELECT state FROM sessions WHERE id=?", (chat.state["session_id"],)).fetchone()[0]
        )
    s["intents"].append("human_help")
    assert "human_help" not in live_app.state.engine._safe_context(s)["intents"]


def test_resume_checks_identity_collected_by_legacy_human_misroute(live_app, chat):
    request_human(chat)
    store = live_app.state.store
    with store.tx() as db:
        s = store.unseal(
            db.execute("SELECT state FROM sessions WHERE id=?", (chat.state["session_id"],)).fetchone()[0]
        )
        # Reproduce the saved fields from the reported old-controller failure.
        s["identity"] = {
            "name": {"value": "margaret chen"},
            "dob": {"value": "1985-03-15"},
            "ssn_last4": {"value": "4472"},
        }
        s["intents"] = ["human_help"]
        db.execute("UPDATE sessions SET state=? WHERE id=?", (store.seal(s), s["session_id"]))
    chat.say(action="resume")
    assert chat.state["verified"] and account(chat).status_code == 200
