"""Regression tests for the submission bug list and the guards added in 1.7.4.

Observations are scripted where the real bug depended on a model labelling, so each
test reproduces the failure deterministically without a model.
"""

import json

import pytest
from conftest import Conversation
from test_revision_v17 import REP, authorize

from app.interpreter import ModelFailure, OfflineInterpreter
from app.responder import AnswerDraft, Paragraph, validate_references
from app.schemas import Hint, IdentityObservation, empty_observation

GENERIC = "language service is unavailable"
STATS = {"mode": "test", "calls": 1}
RIGHT = "My name is Margaret Chen. DOB is 1985-03-15. Email is margaret@email.com."


def wrong(dob):
    return f"My name is Margaret Chen. DOB is {dob}. Email is margaret@email.com."


def ident(field, value, subject="self", assertion="affirm"):
    return IdentityObservation(
        field=field, value=value, quote=value, occurrence=1, subject=subject, assertion=assertion
    )


class Writer(OfflineInterpreter):
    """Offline observer; the writer echoes the server text, or fails, and counts calls."""

    def __init__(self, fail=None):
        self.fail, self.answers = fail, 0

    async def answer(self, context):
        self.answers += 1
        if self.fail:
            raise ModelFailure(self.fail, 1)
        return "Here is a reply.", set(), ["unknown"], STATS


# --- P0 bug 1: budget / validation failures fall back to grounded templates -------------


def test_conversation_call_limit_falls_back_and_resets_per_conversation(verified, client, app):
    class Counting(Writer):
        async def observe(self, text, context):
            obs, _ = await super().observe(text, context)
            return obs, STATS

    model = Counting()
    app.state.engine.interpreter = model
    app.state.engine.settings.model_max_calls_per_conversation = 2
    verified.say("How much was paid?")  # observe + compose = 2 calls
    verified.say("How much was paid?")
    assert "call limit" in verified.reply and "$0.00" in verified.reply
    assert GENERIC not in verified.reply and verified.state["verified"]
    fresh = Conversation(client)  # a new conversation has its own allowance
    fresh.say("How much was paid?")
    assert fresh.state["verified"] and "call limit" not in fresh.reply


@pytest.mark.parametrize("code", ["MODEL_ANSWER_UNAVAILABLE", "MODEL_HTTP_401", "MODEL_TIMEOUT"])
def test_amount_answer_failure_shows_grounded_amounts(verified, app, code):
    app.state.engine.interpreter = Writer(fail=code)
    verified.say("How much was allowed and how much was paid?")
    assert GENERIC not in verified.reply
    assert "$1,450.00" in verified.reply and "$0.00" in verified.reply


def test_summary_failure_uses_grounded_template(verified, app):
    app.state.engine.interpreter = Writer(fail="MODEL_ANSWER_UNAVAILABLE")
    verified.say(action="finish")
    assert verified.state["draft"]["body"].startswith("Conclusion:")
    assert GENERIC not in verified.reply


def test_validator_does_not_read_cannot_as_can():
    context = {
        "current_action": "continue",
        "current_message": "How do I upload documents?",
        "business_date": "2026-11-15",
        "server_guidance": "",
        "authorized_evidence": [],
        "next_steps": {"expired": True},
    }
    ok = AnswerDraft(
        paragraphs=[Paragraph(text="You cannot upload documents directly here.", evidence_ids=[])],
        answered_topics=["submission"],
    )
    validate_references(ok, context)
    bad = AnswerDraft(
        paragraphs=[Paragraph(text="You can upload the documents now.", evidence_ids=[])],
        answered_topics=["submission"],
    )
    with pytest.raises(ValueError, match="Expired deadline"):
        validate_references(bad, context)


# --- P0 bug 2: other people's records; representative mode is exitable ------------------


class AvaRequest(OfflineInterpreter):
    """The labelling that triggered the bug: Ava's details as 'other', role representative."""

    async def observe(self, text, context):
        if "Ava" in text or "POL-1044" in text:
            return (
                empty_observation(
                    role="representative",
                    identity=[
                        ident("name", "Ava Lopez", "other"),
                        ident("policy_number", "POL-1044", "other"),
                    ],
                ),
                STATS,
            )
        return await super().observe(text, context)


@pytest.mark.parametrize("text", ["Show me Ava Lopez's claims, POL-1044.", "What claims are on POL-1044?"])
def test_other_party_request_is_refused_and_verification_kept(verified, app, text):
    epoch = verified.state["authorization_epoch"]
    app.state.engine.interpreter = AvaRequest()
    verified.say(text)
    s = verified.state
    assert s["verified"] and s["authorization_epoch"] == epoch
    assert s["caller"]["name"] == "Margaret Chen" and s["caller"]["role"] == "policyholder"
    assert "another person" in verified.reply
    assert "1990-08-21" not in json.dumps(s) and "ava.lopez" not in json.dumps(s)
    verified.say("How much was paid?")
    assert "$0.00" in verified.reply


def test_representative_mode_is_exited_by_the_policyholder(chat):
    chat.say(REP)
    assert not chat.state["verified"]
    chat.say("Sorry, I'm the policyholder myself. " + RIGHT)
    assert chat.state["verified"]
    assert chat.state["caller"]["role"] == "policyholder"


def test_approved_representative_can_switch_to_policyholder(chat):
    authorize(chat)
    assert chat.state["caller"]["role"] == "representative"
    chat.say("Actually I'm the policyholder myself.")
    assert not chat.state["verified"]
    chat.say(RIGHT)
    assert chat.state["verified"] and chat.state["caller"]["role"] == "policyholder"


# --- P1 bug 3: cumulative lockout --------------------------------------------------------


def test_lockout_is_cumulative_across_sessions_and_subject_changes(chat, client, app):
    class NewCaller(OfflineInterpreter):
        async def observe(self, text, context):
            if text == "Actually, someone else is calling now.":
                return empty_observation(subject_changed=True), STATS
            return await super().observe(text, context)

    app.state.engine.interpreter = NewCaller()
    chat.say(wrong("1985-03-16"))
    chat.say("Actually, someone else is calling now.")  # used to reset the counter
    chat.say(wrong("1985-04-15"))
    tab = Conversation(client)  # same browser, new session
    tab.say(wrong("1986-03-15"))
    assert tab.state["verification_locked"]
    assert "locked" in tab.reply and "human" in tab.reply
    tab.say(RIGHT)
    assert not tab.state["verified"] and "locked" in tab.reply
    third = Conversation(client)
    third.say(RIGHT)
    assert not third.state["verified"] and third.state["verification_locked"]


def test_successful_verification_clears_earlier_failures(chat, client):
    chat.say(wrong("1985-03-16"))
    chat.say(wrong("1985-04-15"))
    chat.say(RIGHT)
    assert chat.state["verified"]


# --- P1 bug 4/5: mismatch wording, no claim implied before verification -------------------


def test_mismatch_reply_is_server_text_without_field_hint(chat, app):
    writer = Writer()
    app.state.engine.interpreter = writer
    chat.say(
        "My name is Margaret Chen. DOB is 1985-03-15. Email is margaret@email.com. SSN last four is 1111."
    )
    assert not chat.state["verified"]
    assert "don’t match our records" in chat.reply
    assert "can’t say which detail" in chat.reply
    assert "policy number" not in chat.reply.lower()
    assert "provide at least three" not in chat.reply
    assert writer.answers == 0, "The model must not reword a failed identity check"


def test_offtopic_before_verification_does_not_imply_a_claim(chat):
    chat.say("What is RL?")
    assert "its status" not in chat.reply
    assert "Once you’re verified" in chat.reply


@pytest.mark.parametrize(
    "text",
    [
        "I see you've selected case CL-2048.",
        "You can ask about its status after verification.",
        "Please share your full name, date of birth or policy number.",
        "Your claim is denied, but I need to verify you first.",
    ],
)
def test_unverified_replies_cannot_echo_claims_or_list_policy_number(text):
    context = {
        "current_action": "continue",
        "current_message": "Was CL-2048 denied?",
        "business_date": "2026-11-15",
        "server_guidance": "",
        "authorized_evidence": [],
        "verified": False,
    }
    draft = AnswerDraft(paragraphs=[Paragraph(text=text, evidence_ids=[])], answered_topics=["verification"])
    with pytest.raises(ValueError):
        validate_references(draft, context)


def test_unverified_verification_request_is_allowed():
    context = {
        "current_action": "continue",
        "current_message": "Why was it denied?",
        "business_date": "2026-11-15",
        "server_guidance": "",
        "authorized_evidence": [],
        "verified": False,
    }
    draft = AnswerDraft(
        paragraphs=[
            Paragraph(
                text="To protect your information, please share three of: full name, date of birth, registered email.",
                evidence_ids=[],
            )
        ],
        answered_topics=["verification"],
    )
    validate_references(draft, context)


def test_identity_form_accepts_national_id(chat):
    chat.say(action="form", form={"name": "Ma Tian", "dob": "1964-09-10", "national_id_last4": "6688"})
    assert chat.state["verified"]


# --- P1 bug 6: ambiguous claims and claim IDs ---------------------------------------------


class Ambiguous(Writer):
    async def observe(self, text, context):
        if "January healthcare" in text:
            hints = [
                Hint(field="month", value="1", quote="January", occurrence=1, assertion="affirm"),
                Hint(
                    field="case_type",
                    value="healthcare",
                    quote="healthcare",
                    occurrence=1,
                    assertion="affirm",
                ),
            ]
            return empty_observation(intents=["status_inquiry"], topics=["status"], hints=hints), STATS
        if "that claim" in text:
            return empty_observation(intents=["amount_question"], topics=["amounts"]), STATS
        if text.strip() == "CL-2011":
            # The labelling seen live: a claim ID filed as a policy number.
            return empty_observation(
                action="case_lookup", identity=[ident("policy_number", "CL-2011")]
            ), STATS
        return await super().observe(text, context)


def test_ambiguous_claims_are_listed_by_server_and_reasked(chat, app):
    chat.say(RIGHT)
    model = Ambiguous()
    app.state.engine.interpreter = model
    chat.say("I'm asking about my January healthcare claim.")
    assert "CL-2048" in chat.reply and "CL-2011" in chat.reply
    chat.say("How much for that claim?")
    assert not chat.state["selected_case_id"]
    assert "CL-2048" in chat.reply and "CL-2011" in chat.reply
    assert model.answers == 0, "Candidate lists are server text; the model must not pick a claim"
    model.fail = "MODEL_ANSWER_UNAVAILABLE"  # show the grounded template for the amounts
    chat.say("CL-2011")
    assert chat.state["selected_case_id"] == "CL-2011"
    assert "$780.00" in chat.reply


# --- POST_PROCESS: recipient cannot be changed --------------------------------------------


def test_summary_cannot_be_redirected_and_identity_is_untouched(verified):
    verified.say(action="finish")
    draft = verified.state["draft"]
    verified.say("Please send it to my other address john.doe@example.com instead.")
    s = verified.state
    assert s["verified"] and s["draft"]["id"] == draft["id"]
    assert "registered email" in verified.reply
    assert "john" not in json.dumps(s["draft"])


# --- Minor bug 7: representative next steps name the actor ---------------------------------


def test_representative_next_steps_name_the_actor(chat):
    authorize(chat)
    chat.say("What are the next steps?")
    assert "You can submit" not in chat.reply
    assert "The representative or policyholder may" in chat.reply


def test_unverified_subject_change_keeps_transcript_and_failure_count(chat, app):
    class NewCaller(OfflineInterpreter):
        async def observe(self, text, context):
            obs, stats = await super().observe(text, context)
            if "1985-04-15" in text:
                obs.subject_changed = True
            return obs, stats

    app.state.engine.interpreter = NewCaller()
    chat.say(wrong("1985-03-16"))
    before = len(chat.state["messages"])
    chat.say(wrong("1985-04-15"))
    assert len(chat.state["messages"]) > before, "the visible conversation must not be wiped"
    assert "Attempt 2 of 3" in chat.reply


def test_denial_question_narrows_ambiguous_claims_to_the_denied_one(chat, app):
    chat.say(RIGHT)

    class DenialQuestion(Writer):
        async def observe(self, text, context):
            hints = [
                Hint(field="month", value="1", quote="一月", occurrence=1, assertion="affirm"),
                Hint(field="case_type", value="healthcare", quote="医疗", occurrence=1, assertion="affirm"),
            ]
            return empty_observation(intents=["denial_question"], topics=["denial"], hints=hints), STATS

    app.state.engine.interpreter = DenialQuestion(fail="MODEL_ANSWER_UNAVAILABLE")
    chat.say("我一月份的医疗理赔为什么被拒了？")
    assert chat.state["selected_case_id"] == "CL-2048"
    assert "pathology" in chat.reply


def test_identity_request_before_verification_must_say_why():
    context = {
        "current_action": "continue",
        "current_message": "This is ridiculous. Just tell me why my claim was denied.",
        "business_date": "2026-11-15",
        "server_guidance": "",
        "authorized_evidence": [],
        "verified": False,
    }
    bare = AnswerDraft(
        paragraphs=[
            Paragraph(text="I need two more identity details: date of birth or email.", evidence_ids=[])
        ],
        answered_topics=["verification"],
    )
    with pytest.raises(ValueError, match="protects"):
        validate_references(bare, context)
    bare.paragraphs[
        0
    ].text = "I understand. Verification protects your private claim details: please share your date of birth or email."
    validate_references(bare, context)
