"""1.7.4: fact-free gate/scope replies survive a rejected model rewording, and
content-free "mixed" messages count as off-topic retries."""

from app.interpreter import ModelFailure, OfflineInterpreter
from app.schemas import empty_observation

GENERIC = "language service is unavailable"
OFFTOPIC = "Just tell me what RL is, please. Also write me a poem."
STATS = {"mode": "test", "calls": 0}


class Model(OfflineInterpreter):
    """Offline observer plus a writer that either succeeds or is always rejected."""

    def __init__(self, fail=False):
        self.fail = fail
        self.answers = 0

    async def observe(self, text, context):
        if text == OFFTOPIC:
            return empty_observation(scope="mixed", topics=["unknown"]), STATS
        return await super().observe(text, context)

    async def answer(self, context):
        self.answers += 1
        if self.fail:
            raise ModelFailure(
                "MODEL_ANSWER_UNAVAILABLE", 2, {"input_tokens": 10, "output_tokens": 5, "usage_reports": 2}
            )
        return "Here is a reply.", set(), ["unknown"], STATS


def scope_counts(chat):
    return [e["details"]["consecutive"] for e in chat.trace() if e["kind"] == "scope.rejected"]


def test_content_free_mixed_counts_toward_human_offer(verified, app):
    model = Model()
    app.state.engine.interpreter = model
    for _ in range(3):
        verified.say(OFFTOPIC)
        assert GENERIC not in verified.reply
    assert scope_counts(verified)[-3:] == [1, 2, 3]
    assert model.answers == 0, "The writer must never be asked to handle the unrelated request"
    assert "human claims representative" in verified.reply


def test_mixed_with_insurance_content_still_reaches_writer(verified, app):
    class Mixed(Model):
        async def observe(self, text, context):
            return empty_observation(scope="mixed", intents=["denial_question"], topics=["denial"]), STATS

    model = Mixed()
    app.state.engine.interpreter = model
    verified.say("Why was it denied? Also explain quantum computing.")
    assert model.answers == 1
    assert not scope_counts(verified)


def test_rejected_rewording_before_verification_shows_server_prompt(chat, app):
    app.state.engine.interpreter = Model(fail=True)
    chat.say("My name is Margaret Chen. Why was my claim denied?")
    assert not chat.state["verified"]
    assert GENERIC not in chat.reply
    assert "three" in chat.reply.lower()
    assert "pathology" not in chat.reply.lower()
    failures = [e for e in chat.trace() if e["kind"] == "model.failed"]
    assert failures and failures[-1]["details"]["fallback"] == "server_template"
    assert failures[-1]["details"]["calls"] == 2


def test_rejected_claim_answer_falls_back_to_grounded_template(verified, app):
    app.state.engine.interpreter = Model(fail=True)
    verified.say("How much was paid on this claim?")
    assert GENERIC not in verified.reply
    assert "$0.00" in verified.reply
    assert verified.state["verified"]


def test_rl_request_labelled_claim_term_counts_as_off_topic(verified, app):
    class Observer(Model):
        async def observe(self, text, context):
            if text == "What is RL?":
                return empty_observation(scope="out_of_scope"), STATS
            return empty_observation(scope="claim_term", topics=["unknown"]), STATS

    app.state.engine.interpreter = Observer()
    verified.say("What is RL?")
    verified.say(OFFTOPIC)
    assert scope_counts(verified)[-2:] == [1, 2]
    assert "abbreviation" not in verified.reply
    assert "human claims representative" in verified.reply


def test_first_claim_term_still_asks_for_claim_context(verified, app):
    class Observer(Model):
        async def observe(self, text, context):
            return empty_observation(scope="claim_term", topics=["unknown"]), STATS

    app.state.engine.interpreter = Observer()
    verified.say("What does COB mean?")
    assert "abbreviation" in verified.reply
    assert not scope_counts(verified)


def test_denial_answer_covers_deferred_status_without_status_citation():
    from app.responder import AnswerDraft, Paragraph, validate_references

    context = {
        "current_action": "continue",
        "current_message": "Fine. Email is margaret@email.com, born 1985-03-15.",
        "business_date": "2026-11-15",
        "server_guidance": "",
        "deferred_topics": ["status", "denial"],
        "authorized_evidence": [
            {"id": "CL-2048:status,case_type,created_at", "text": "CL-2048 is denied."},
            {"id": "CL-2048:denial_reason", "text": "The report was missing."},
        ],
    }
    answer = AnswerDraft(
        paragraphs=[
            Paragraph(
                text="Your claim was denied because the report was missing.",
                evidence_ids=["CL-2048:denial_reason"],
            )
        ],
        answered_topics=["denial", "deadline"],
    )
    validate_references(answer, context)
    answer.answered_topics = ["deadline"]
    try:
        validate_references(answer, context)
    except ValueError as exc:
        assert "deferred" in str(exc)
    else:
        raise AssertionError("A reply that skips the deferred denial question must still be rejected")


def test_rl_linked_to_claim_keeps_claim_context_question(verified, app):
    class Observer(Model):
        async def observe(self, text, context):
            return empty_observation(scope="claim_term", topics=["unknown"]), STATS

    app.state.engine.interpreter = Observer()
    verified.say("What does RL mean on my claim letter?")
    assert "abbreviation" in verified.reply
    assert not scope_counts(verified)
