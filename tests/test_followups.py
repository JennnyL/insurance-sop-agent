import pytest
from conftest import SAMPLE


@pytest.mark.parametrize("question", ["asdfgh?", "What about the other thing?", "我想问另外那个问题"])
def test_unrecognized_question_does_not_replay_stored_intent(verified, question):
    original = verified.reply
    count = sum(e["kind"] == "tool.called" for e in verified.trace())
    verified.say(question)
    assert "couldn’t identify which claim question" in verified.reply
    assert "recorded reason for denial" not in verified.reply
    assert verified.reply != original
    assert sum(e["kind"] == "tool.called" for e in verified.trace()) == count
    assert verified.state["selected_case_id"] == "CL-2048"
    verified.say("How much was paid?")
    assert "Finalized insurer payment recorded" in verified.reply


def test_consecutive_different_topics_and_chinese(verified):
    cases = [
        ("How much was paid?", "Finalized insurer payment recorded"),
        ("需要什么材料？", "The review file is missing"),
        ("截止日期是什么？", "recorded appeal deadline is 2026-03-18"),
        ("提交后多久处理？", "Only a representative"),
        ("材料提交到哪里？", "Only a representative"),
        ("材料收到了吗？", "cannot check receipt"),
        ("理赔进度如何？", "Claim CL-2048 is denied"),
        ("金额是多少？", "Finalized insurer payment recorded"),
    ]
    previous = verified.reply
    for question, answer in cases:
        verified.say(question)
        assert answer in verified.reply
        assert verified.reply != previous
        assert "recorded reason for denial" not in verified.reply
        previous = verified.reply


def test_unknown_question_pauses_summary_without_replaying_denial(verified):
    verified.say(action="finish")
    verified.say("What about that other thing?")
    assert verified.state["phase"] == "POST_PROCESS"
    assert verified.state["draft"]["paused"]
    assert "couldn’t identify" in verified.reply
    verified.say("Where do I submit the documents?")
    assert verified.state["phase"] == "PROCESS_CASE"
    assert verified.state["draft"] is None
    assert "Only a representative" in verified.reply


def test_why_before_verification_never_reads_claims(chat):
    chat.say("Why verify my identity?")
    assert "Identity verification protects" in chat.reply
    assert not any(e["kind"] == "tool.called" for e in chat.trace())


def test_new_claim_does_not_retain_denial_reference(verified):
    verified.say("Actually, I mean my auto claim from February, not the healthcare claim.")
    assert verified.state["selected_case_id"] == "CL-2102"
    verified.say("why")
    assert "recorded reason for denial" not in verified.reply
    assert "pathology" not in verified.reply


def test_pending_early_question_is_still_answered(chat):
    chat.say("How much was paid on my denied healthcare claim from January?")
    chat.say(SAMPLE)
    assert "Finalized insurer payment recorded" in chat.reply
    assert "recorded reason for denial" in chat.reply
