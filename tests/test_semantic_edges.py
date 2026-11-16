import time

import pytest

from app.claims import AccessDenied
from app.schemas import TurnRequest, empty_observation
from app.state import new_state


def test_natural_dob_does_not_become_claim_month(chat):
    chat.say(
        "My name is Margaret Chen. My DOB is March 15, 1985. SSN last four is 4472. What is my denied healthcare claim from January?"
    )
    assert chat.state["verified"]
    assert chat.state["memory"]["hints"]["month"]["value"] == "1"
    assert chat.state["selected_case_id"] == "CL-2048"


def test_negated_old_hint_is_removed(chat):
    chat.say("My name is Margaret Chen. My denied healthcare claim from January.")
    chat.say("Actually, it is not denied.")
    assert "status" not in chat.state["memory"]["hints"]
    assert chat.state["phase"] == "VERIFY_ID"


def test_ambiguous_conflicting_identity_is_not_silently_overwritten(chat):
    chat.say("My name is Margaret Chen. DOB is 1985-03-15.")
    chat.say("DOB is 1990-08-21. SSN last four is 4472.")
    assert not chat.state["verified"]
    chat.say("Correction: DOB is 1985-03-15.")
    assert chat.state["verified"]


def test_handoff_resume_is_not_connected(chat):
    chat.say("I want to talk to a human.")
    assert chat.state["lifecycle"] == "ACTIVE"
    chat.say(action="resume")
    assert chat.state["lifecycle"] == "ACTIVE"
    assert chat.state["handoff"] is None and chat.state["handoffs"] == []
    assert "H-" in chat.reply
    assert not chat.state["verified"]


def test_new_subject_does_not_inherit_partial_identity(chat):
    chat.say("My name is Margaret Chen. DOB is 1985-03-15.")
    chat.say("Actually my name is Ava Lopez.")
    assert chat.state["identity_count"] == 1
    assert not chat.state["verified"]


def test_refused_incorrect_ssn_can_be_replaced_by_email(chat):
    chat.say("My name is Margaret Chen. DOB is 1985-03-15. SSN last four is 1111.")
    assert not chat.state["verified"]
    chat.say("I don't want to share my SSN. My email is margaret@email.com.")
    assert chat.state["verified"]


def test_forged_underspecified_grant_rejected(app):
    s = new_state()
    s.update(
        phase="RESOLVE_INTENT",
        auth={
            "subject": "P9",
            "epoch": 0,
            "fields": ["name"],
            "last_used_at": time.time(),
            "expires_at": time.time() + 60,
        },
    )
    with pytest.raises(AccessDenied):
        app.state.engine.gateway.find_cases(s, [])


def test_reset_cancels_pending_email(verified, app):
    verified.say(action="finish")
    verified.say("yes")
    verified.say(action="reset")
    app.state.store.dispatch()
    with app.state.store.tx() as db:
        assert db.execute("SELECT status FROM outbox").fetchone()[0] == "CANCELLED"
    assert verified.state["verified"]  # New conversation keeps authorization.


def test_same_case_question_never_reads_other_claim(verified):
    verified.say("How much was paid?")
    events = verified.trace()
    assert {e["details"]["case_id"] for e in events if e["details"].get("tool") == "get_claim"} == {"CL-2048"}


async def test_fake_model_cannot_invent_source_values(app):
    from app.schemas import IdentityObservation

    class Fake:
        async def observe(self, text, context):
            fields = [
                IdentityObservation(
                    field=f, value=v, quote="hello", occurrence=1, subject="self", assertion="affirm"
                )
                for f, v in [("name", "Margaret Chen"), ("dob", "1985-03-15"), ("ssn_last4", "4472")]
            ]
            return empty_observation(identity=fields), {"mode": "fake", "calls": 1, "status": "ok"}

    app.state.engine.interpreter = Fake()
    s = app.state.store.create("owner")
    s = await app.state.engine.turn(
        s["session_id"], "owner", TurnRequest(message_id="fake", expected_version=0, text="hello")
    )
    assert s["auth"] is None and not s["identity"]


async def test_question_topics_survive_identity_only_turn_and_clear_on_case_switch(app):
    from app.interpreter import OfflineInterpreter

    class ObservedTopics(OfflineInterpreter):
        async def observe(self, text, context):
            observation, stats = await super().observe(text, context)
            observation.topics = ["denial"] if "denied" in text else ["status"]
            return observation, stats

    app.state.engine.interpreter = ObservedTopics()
    state = app.state.store.create("topic-owner")
    for index, text in enumerate(
        [
            "My name is Margaret Chen. My denied healthcare claim from January.",
            "My DOB is 1985-03-15. SSN last four is 4472.",
        ]
    ):
        state = await app.state.engine.turn(
            state["session_id"],
            "topic-owner",
            TurnRequest(message_id=f"topic-{index}", expected_version=state["version"], text=text),
        )
    assert state["phase"] == "PROCESS_CASE"
    assert "pathology report" in state["messages"][-1]["text"]
    assert not state.get("pending_topics")
    state = await app.state.engine.turn(
        state["session_id"],
        "topic-owner",
        TurnRequest(
            message_id="topic-switch",
            expected_version=state["version"],
            text="Actually, I mean my auto claim from February.",
        ),
    )
    assert state["selected_case_id"] == "CL-2102"
    assert "pathology report" not in state["messages"][-1]["text"]


@pytest.mark.parametrize(
    "previous",
    [
        "My name is Nobody Exists. DOB is 1900-01-01. SSN last four is 0000.",
        "My name is Nobody Exists.",
        "My name is Nobody Exists. DOB is 1900-01-01. Email is nobody@example.com. Policy POL-0000.",
        "My name is Margaret Chen. DOB is 1985-03-15. SSN last four is 4472. Email is wrong@example.com.",
    ],
)
def test_complete_sample_replaces_previous_unverified_identity(chat, previous):
    from conftest import SAMPLE

    chat.say(previous)
    assert not chat.state["verified"]
    assert not any(e["kind"] == "tool.called" for e in chat.trace())
    chat.say(SAMPLE)
    assert chat.state["verified"], chat.reply
    assert chat.state["phase"] == "PROCESS_CASE"
    assert chat.state["selected_case_id"] == "CL-2048"
    events = [e["kind"] for e in chat.trace()]
    assert events.index("verification.passed") < events.index("tool.called")


def test_full_resubmission_recovers_existing_conflict(chat):
    from conftest import SAMPLE

    chat.say("My name is Margaret Chen. DOB is 1985-03-15.")
    chat.say("DOB is 1990-08-21. SSN last four is 4472.")
    assert not chat.state["verified"]
    chat.say(SAMPLE)
    assert chat.state["verified"]
    assert chat.state["selected_case_id"] == "CL-2048"


def test_resubmission_still_checks_every_current_field(chat):
    from conftest import SAMPLE

    chat.say("My name is Nobody Exists. DOB is 1900-01-01. Email is nobody@example.com.")
    chat.say(SAMPLE + " My email is still.wrong@example.com.")
    assert not chat.state["verified"]
    assert not any(e["kind"] == "tool.called" for e in chat.trace())
    failures = [e for e in chat.trace() if e["kind"] == "verification.failed"]
    assert len(failures) == 2


def test_resubmission_does_not_choose_between_conflicting_current_values(chat):
    from conftest import SAMPLE

    chat.say("My name is Nobody Exists.")
    chat.say(SAMPLE + " DOB is 1990-08-21.")
    assert not chat.state["verified"]
    assert not any(e["kind"] == "tool.called" for e in chat.trace())


@pytest.mark.parametrize("failed_attempts", [2, 3])
def test_complete_resubmission_preserves_attempt_limit(chat, failed_attempts):
    from conftest import SAMPLE

    for _ in range(failed_attempts):
        chat.say("My name is Nobody Exists. DOB is 1900-01-01. SSN last four is 0000.")
    chat.say(SAMPLE)
    assert chat.state["verified"] == (failed_attempts < 3)
    assert chat.state["verification_locked"] == (failed_attempts == 3)
    assert len([e for e in chat.trace() if e["kind"] == "verification.failed"]) == failed_attempts


def test_identity_resubmission_preserves_earlier_claim_question(chat):
    chat.say(
        "My name is Nobody Exists. DOB is 1900-01-01. SSN last four is 0000. My denied healthcare claim from January."
    )
    chat.say("My name is Margaret Chen. DOB is 1985-03-15. SSN last four is 4472.")
    assert chat.state["verified"]
    assert chat.state["selected_case_id"] == "CL-2048"
    assert "pathology report" in chat.reply
    assert chat.state["memory"]["hints"]["month"]["source_turn"] == "1"
