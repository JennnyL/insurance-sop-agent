"""1.7.5: consent pushback explanation, explicit human option on the second refusal,
no persuasion after the third, one empathy sentence per reply, single version source."""

import json

from test_forms_and_answers import ComposableOffline, answer, approve, wire

import app as app_package
from app.engine import CONSENT_EXPLANATION, limit_empathy

REP = (
    "I'm David Chen, calling on behalf of Margaret Chen. Her DOB is 1985-03-15. "
    "Her email is margaret@email.com. Her denied healthcare claim from January."
)
PUSHBACK = "Why do you need her consent? I'm her son, this is ridiculous. Just tell me."
EMPATHY_STARTS = ("I understand", "I hear", "I'm sorry", "I’m sorry", "That's okay", "I respect")


def empathy_sentences(reply):
    import re

    sentences = re.split(r"(?<=[.!?])\s+|\n\n", reply)
    return [s for s in sentences if s.strip().startswith(EMPATHY_STARTS)]


def test_consent_pushback_is_acknowledged_explained_and_actionable(chat):
    chat.say(REP)
    assert chat.state["representative_consent"]["status"] == "pending"
    chat.say(PUSHBACK)
    reply = chat.reply
    assert reply.startswith("I understand this is frustrating.")
    assert "private information" in reply and "even from close family" in reply
    assert "Check policyholder consent" in reply and "Request human help" in reply
    assert "still pending" in reply
    assert len(empathy_sentences(reply)) == 1
    # The gate is unchanged: still no access and no claim facts.
    assert not chat.state["verified"]
    assert "CL-2048" not in reply and "pathology" not in reply


def test_plain_consent_question_without_pushback_keeps_pending_template(chat):
    chat.say(REP)
    chat.say("Okay.")
    assert CONSENT_EXPLANATION not in chat.reply


def test_second_refusal_names_the_human_request_option(chat):
    chat.say("I want to know about my denied claim.")
    chat.say("I refuse to give my SSN")
    assert "won't keep pressing" not in chat.reply
    chat.say("This is ridiculous, I'm not sharing anything else.")
    assert "Request human help" in chat.reply
    assert "human handling request" in chat.reply
    assert not chat.state["verified"] and not chat.state["verification_locked"]


def test_third_refusal_stops_persuading_even_on_an_ssn_refusal(chat):
    for _ in range(3):
        chat.say("I refuse to give my SSN")
    reply = chat.reply
    assert chat.state["verification_locked"]
    assert "won't keep pressing" in reply and "human handling request" in reply
    assert "You do not have to provide an SSN" not in reply
    assert "date of birth" not in reply
    assert len(empathy_sentences(reply)) <= 1


def test_limit_empathy_keeps_only_the_first_acknowledgment():
    stacked = (
        "That's okay—you can choose which identity fields to share.\n\n"
        "I understand your concern about sharing personal information. To protect your privacy we need "
        "three fields. Without this verification, I can't provide claim details."
    )
    out = limit_empathy(stacked)
    assert out.startswith("That's okay")
    assert "I understand your concern" not in out
    assert "To protect your privacy we need three fields." in out
    assert "I can't provide claim details." in out


def test_limit_empathy_never_removes_content_sentences():
    reply = (
        "I understand this is frustrating.\n\n"
        "I understand you want the amount: $1,450.00 was billed. I'm sorry, but I can't change the decision."
    )
    assert limit_empathy(reply) == reply


def test_model_empathy_is_not_stacked_on_server_empathy(verified, app):
    def handler(request):
        body = json.loads(request.content)
        if body["text"]["format"]["name"] == "grounded_answer":
            return wire(
                answer(
                    "I understand your concern about this. The record says the pathology report and "
                    "office note are missing, which is why this claim was denied.",
                    ["CL-2048:denial_reason"],
                    ["denial"],
                )
            )
        return wire(approve())

    app.state.engine.interpreter = ComposableOffline(app.state.settings, handler)
    verified.say("This is ridiculous, why was it denied?")
    assert verified.reply.startswith("I understand this is frustrating.")
    assert "I understand your concern" not in verified.reply
    assert "pathology report" in verified.reply
    assert len(empathy_sentences(verified.reply)) == 1


def test_health_and_config_report_the_package_version(client, app):
    version = app_package.__version__
    assert client.get("/healthz").json()["app_version"] == version
    assert client.get("/v1/config").json()["app_version"] == version
    assert app.version == version
