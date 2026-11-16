"""Deferred representative questions and email recipient perspective."""

import pytest
from conftest import Conversation
from test_revision_v17 import REP

from app.interpreter import ModelFailure, OfflineInterpreter
from app.responder import AnswerDraft, Paragraph, validate_references, writing_facts


class ContextWriter(OfflineInterpreter):
    def __init__(self, fail=False):
        self.contexts = []
        self.fail = fail

    async def answer(self, context):
        self.contexts.append(context)
        if self.fail:
            raise ModelFailure("TEST_UNAVAILABLE")
        topics = list(dict.fromkeys(context["deferred_topics"] + ["status"]))
        facts = context["authorized_evidence"]
        chosen = [f for f in facts if f["id"].endswith("denial_reason")]
        if "denial" not in topics:
            chosen = facts[:1]
        return " ".join(f["text"] for f in chosen), {f["id"] for f in chosen}, topics, {"calls": 0}


def approve(chat):
    chat.say(REP)
    assert not chat.state["verified"]
    assert "denial" in chat.state["memory"]["pending_topics"]
    assert not chat.state["evidence"]
    chat.say(action="consent_check")
    assert chat.state["claim_confirmation"] == "CL-2048"
    assert not chat.state["evidence"]


@pytest.mark.parametrize(
    "selection",
    [
        {"action": "confirm_claim"},
        {"text": "Yes, this claim."},
        {"action": "form", "form": {"case_id": "CL-2048"}},
        {"text": "CL-2048"},
    ],
)
def test_confirmation_answers_deferred_denial_through_model(chat, app, selection):
    approve(chat)
    writer = ContextWriter()
    app.state.engine.interpreter = writer
    chat.say(**selection)
    assert chat.state["selected_case_id"] == "CL-2048"
    context = writer.contexts[-1]
    assert "denial" in context["deferred_topics"]
    assert "denial" in context["suggested_topics"]
    assert any(f["id"] == "CL-2048:denial_reason" for f in context["authorized_evidence"])
    assert "Confirm ONLY" not in context["server_guidance"]
    assert "recorded reason for denial" in chat.reply
    assert "denial" in chat.state["memory"]["case_history"]["discussed_topics"]
    assert not chat.state["memory"]["pending_topics"]


@pytest.mark.parametrize("decline_first", [False, True])
def test_other_claim_does_not_inherit_deferred_denial(chat, app, decline_first):
    approve(chat)
    if decline_first:
        chat.say("no")
    writer = ContextWriter()
    app.state.engine.interpreter = writer
    chat.say(action="form", form={"case_id": "CL-2102"})
    assert chat.state["selected_case_id"] == "CL-2102"
    assert "denial" not in writer.contexts[-1]["deferred_topics"]
    assert "Confirm ONLY" in writer.contexts[-1]["server_guidance"]
    assert "CL-2048" not in chat.reply


def test_failed_answer_preserves_deferred_question_for_retry(chat, app):
    approve(chat)
    writer = ContextWriter(fail=True)
    app.state.engine.interpreter = writer
    chat.say(action="confirm_claim")
    # 1.7.4: an unavailable writer falls back to the grounded template, which answers
    # the deferred denial question from the fixture instead of returning an error.
    assert "reliable answer" not in chat.reply
    assert "recorded reason for denial" in chat.reply
    assert not chat.state["memory"]["pending_topics"]


def test_new_conversation_does_not_import_deferred_question(chat, app):
    approve(chat)
    fresh = Conversation(chat.client)
    writer = ContextWriter()
    app.state.engine.interpreter = writer
    fresh.say(action="form", form={"case_id": "CL-2048"})
    assert "denial" not in writer.contexts[-1]["deferred_topics"]
    assert "Confirm ONLY" in writer.contexts[-1]["server_guidance"]


def test_unmatched_reference_preserves_original_question_and_confirmation(chat):
    approve(chat)
    chat.say(action="form", form={"case_id": "CL-999999"})
    assert chat.state["selected_case_id"] is None
    assert chat.state["claim_confirmation"] == "CL-2048"
    assert "denial" in chat.state["memory"]["pending_topics"]
    assert "No matching claim" in chat.reply
    chat.say(action="confirm_claim")
    assert "recorded reason for denial" in chat.reply


@pytest.mark.parametrize("existing_request", [False, True])
def test_summary_attributes_question_to_representative(chat, existing_request):
    approve(chat)
    chat.say(action="confirm_claim")
    assert "recorded reason for denial" in chat.reply
    if existing_request:
        chat.say(action="human")
    chat.say("Can the deadline be extended for me?")
    chat.say("no, that's all, thanks")
    body = chat.state["draft"]["body"]
    assert "David Chen asked about" in body
    assert "reason for the recorded denial" in body
    assert "Unresolved representative questions" in body
    assert body.count("policyholder's approved consent") == 1
    assert "you asked" not in body.lower()
    assert "your existing request" not in body.lower()
    assert "The representative or policyholder may" in body
    assert "You can" not in body
    if existing_request:
        assert "The representative's existing request" in body
    else:
        assert "The representative or policyholder may submit a human handling request" in body


def summary_answer(discussion):
    return AnswerDraft(
        paragraphs=[
            Paragraph(text=text, evidence_ids=[])
            for text in [
                "Conclusion: No decision was changed.",
                discussion,
                "Next steps: Assessment is needed.",
                "References: None.",
            ]
        ],
        answered_topics=[],
    )


def summary_facts():
    return {
        "current_action": "summary",
        "current_message": "Write summary",
        "business_date": "2026-11-15",
        "authorized_evidence": [],
        "server_guidance": "",
        "summary_audience": {"recipient": "policyholder", "question_author": "representative"},
        "caller": {"role": "representative", "name": "David Chen"},
        "data_subject": {"name": "Margaret Chen"},
    }


@pytest.mark.parametrize(
    "wording",
    [
        "You asked whether it could be extended.",
        "The representative asked about your request for an extension.",
    ],
)
def test_summary_rejects_attributing_davids_question_to_recipient(wording):
    with pytest.raises(ValueError, match="representative"):
        validate_references(summary_answer("Discussion: " + wording), summary_facts())


def test_summary_writer_and_reviewer_get_audience_without_actor_metadata():
    context = summary_facts()
    facts = writing_facts(context)
    assert facts["summary_audience"]["question_author"] == "representative"
    assert "caller" not in facts and "data_subject" not in facts
    validate_references(
        summary_answer("Discussion: The representative asked whether the deadline could be extended."),
        context,
    )


def test_next_steps_explicit_actor_required_only_for_representative_summary():
    context = summary_facts()
    answer = summary_answer("Discussion: The representative asked about the deadline.")
    answer.paragraphs[2].text = "Next steps: You may submit a human handling request."
    with pytest.raises(ValueError, match="name the actor explicitly"):
        validate_references(answer, context)
    self_context = {
        **context,
        "caller": {"role": "policyholder"},
        "summary_audience": {"question_author": "policyholder"},
    }
    validate_references(answer, self_context)
    answer.paragraphs[
        2
    ].text = "Next steps: The representative or policyholder may submit a human handling request."
    validate_references(answer, context)


def test_status_only_answer_cannot_silently_discard_deferred_denial():
    context = {**summary_facts(), "current_action": "continue", "deferred_topics": ["denial"]}
    answer = AnswerDraft(
        paragraphs=[Paragraph(text="The claim is selected.", evidence_ids=[])], answered_topics=["status"]
    )
    with pytest.raises(ValueError, match="deferred questions"):
        validate_references(answer, context)


def test_denial_answer_citing_status_need_not_duplicate_status_topic():
    context = {
        **summary_facts(),
        "current_action": "continue",
        "deferred_topics": ["status", "denial"],
        "authorized_evidence": [
            {"id": "CL-2048:status,case_type,created_at", "text": "CL-2048 is denied."},
            {"id": "CL-2048:denial_reason", "text": "The report was missing."},
        ],
    }
    answer = AnswerDraft(
        paragraphs=[
            Paragraph(
                text="CL-2048 is denied because the report was missing.",
                evidence_ids=[e["id"] for e in context["authorized_evidence"]],
            )
        ],
        answered_topics=["denial"],
    )
    validate_references(answer, context)
