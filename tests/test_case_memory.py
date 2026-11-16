import json
import time

import pytest
from conftest import SAMPLE, Conversation
from fastapi.testclient import TestClient

from app.store import Conflict, Store


def test_case_switch_restores_only_corresponding_history(verified, app):
    verified.say("How much was paid?")
    old_turns = verified.state["memory"]["case_history"]["turn_count"]
    verified.say(action="form", form={"case_id": "CL-2102"})
    assert verified.state["memory"]["case_history"]["case_id"] == "CL-2102"
    assert "denial" not in verified.state["memory"]["case_history"]["discussed_topics"]
    verified.say(action="form", form={"case_id": "CL-2048"})
    memory = verified.state["memory"]
    assert memory["case_history"]["turn_count"] == old_turns + 1
    assert "amounts" in memory["case_history"]["discussed_topics"]
    assert memory["recent_cases"] == ["CL-2048", "CL-2102"]
    assert any(e["kind"] == "memory.case_loaded" for e in verified.trace())
    with app.state.store.tx() as db:
        assert db.execute("SELECT COUNT(*) FROM case_memory").fetchone()[0] == 2
        for row in db.execute("SELECT payload FROM case_memory"):
            assert "pathology" not in row[0] and "Margaret" not in row[0]


def test_new_consultation_does_not_restore_prior_discussion(verified, app):
    verified.say("What if I cannot get the report?")
    verified.say(action="finish")
    verified.say(action="reset")
    assert not verified.state["memory"]["recent_cases"]
    assert verified.state["memory"]["case_history"] is None
    verified.say(SAMPLE)
    memory = verified.state["memory"]["case_history"]
    assert "alternatives" not in memory["discussed_topics"]
    assert memory["turn_count"] == 1
    assert verified.state["draft"] is None and verified.state["email_operation"] is None
    with app.state.store.tx() as db:
        data = app.state.store.unseal(db.execute("SELECT payload FROM case_memory").fetchone()[0])
    assert not {"auth", "identity", "consent", "draft", "email_operation"} & data.keys()
    assert "1985-03-15" not in json.dumps(data)


def test_memory_not_shared_across_browser_principals_or_subjects(verified, app):
    other = Conversation(TestClient(app))
    other.say(SAMPLE)
    assert other.state["memory"]["case_history"]["turn_count"] == 1
    verified.say(
        action="form", form={"name": "Ava Lopez", "dob": "1990-08-21", "email": "ava.lopez@email.com"}
    )
    assert verified.state["memory"]["recent_cases"] == []
    assert verified.state["memory"]["case_history"] is None


def test_memory_load_requires_durable_grant_and_owned_session(app):
    state = app.state.store.create("owner")
    with pytest.raises(Conflict, match="MEMORY_REQUIRES_VERIFICATION"):
        app.state.store.memories(state, "owner", "CL-2048")
    with pytest.raises(Conflict):
        app.state.store.memories(state, "stranger", "CL-2048")


def test_memory_survives_store_restart_and_expiry_does_not_resurrect(verified, app):
    verified.say("How much was paid?")
    reopened = Store(app.state.settings)
    with reopened.tx() as db:
        row = db.execute("SELECT payload FROM case_memory").fetchone()
        assert "amounts" in reopened.unseal(row[0])["discussed"]
        for table in ("case_memory", "profile_memory"):
            db.execute(
                f"UPDATE {table} SET updated=?", (time.time() - app.state.settings.session_seconds - 1,)
            )
    verified.say(action="reset")
    verified.say(SAMPLE)
    assert verified.state["memory"]["case_history"]["turn_count"] == 1
    assert "amounts" not in verified.state["memory"]["case_history"]["discussed_topics"]


def test_duplicate_turn_does_not_duplicate_case_memory(verified):
    state = verified.state
    body = {"message_id": "same", "expected_version": state["version"], "text": "How much was paid?"}
    path = f"/v1/sessions/{state['session_id']}/messages"
    first = verified.client.post(path, json=body).json()
    second = verified.client.post(path, json=body).json()
    assert first["memory"]["case_history"]["turn_count"] == second["memory"]["case_history"]["turn_count"]
