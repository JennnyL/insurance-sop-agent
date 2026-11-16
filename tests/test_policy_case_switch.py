"""Policy/claim locators select owned cases without reauthentication or context loss."""

import pytest


@pytest.mark.parametrize("method", ["chat", "form"])
def test_policy_only_lists_candidates_and_selection_restores_case_memory(verified, method):
    verified.say("Can I use a scan?")
    epoch = verified.state["authorization_epoch"]
    if method == "chat":
        verified.say("POL-9921")
    else:
        verified.say(action="form", form={"policy_number": "POL-9921"})
    s = verified.state
    assert s["verified"] and s["authorization_epoch"] == epoch
    assert s["selected_case_id"] == "CL-2048"
    assert {c["case_id"] for c in s["case_picker"]["matches"]} == {"CL-2048", "CL-2102", "CL-2011", "CL-1899"}
    verified.say(action="form", form={"case_id": "CL-2102"})
    assert verified.state["selected_case_id"] == "CL-2102" and verified.state["case_picker"] is None
    assert "pathology" not in verified.reply.lower()
    verified.say("How much was paid?")
    assert "$0.00" in verified.reply
    verified.say("POL-9921")
    verified.say("CL-2048")
    assert verified.state["selected_case_id"] == "CL-2048"
    assert "documents" in verified.state["memory"]["case_history"]["discussed_topics"]
    assert verified.state["authorization_epoch"] == epoch


@pytest.mark.parametrize("lookup", ["POL-0000", "POL-8836", "CL-999999", "CL-3001"])
def test_missing_or_other_person_reference_preserves_active_case_and_auth(verified, lookup):
    before = verified.state
    verified.say(lookup)
    s = verified.state
    assert s["verified"] and s["authorization_epoch"] == before["authorization_epoch"]
    assert s["selected_case_id"] == before["selected_case_id"] == "CL-2048"
    if lookup == "POL-8836":
        # Another policyholder's policy: refused outright, own verification unchanged.
        assert "another person" in verified.reply and "verification is unchanged" in verified.reply
    else:
        assert s["case_picker"] and not s["case_picker"]["matches"]
        assert "No matching" in verified.reply and "previous claim remains selected" in verified.reply
    assert "Ma Tian" not in verified.reply and "diagnosis report" not in verified.reply
    verified.say("How much was paid?")
    assert "$0.00" in verified.reply


def test_missing_description_restores_prior_case(verified):
    verified.say("Actually I mean my dental claim from February.")
    assert verified.state["selected_case_id"] == "CL-2048"
    assert "No claim matched" in verified.reply
    verified.say("Why was it denied?")
    assert "pathology report" in verified.reply


def test_policy_alone_never_authenticates(chat):
    chat.say("POL-9921")
    assert not chat.state["verified"] and chat.state["identity_count"] == 0
    assert not chat.state["case_picker"] and "CL-2048" not in chat.reply


def test_verified_policy_with_no_claims(chat):
    chat.say(action="form", form={"name": "Ava Lopez", "dob": "1990-08-21", "email": "ava.lopez@email.com"})
    profile = chat.client.get(f"/v1/sessions/{chat.state['session_id']}/account").json()["profile"]
    chat.say(action="form", form={"policy_number": profile["policy_number"]})
    assert chat.state["verified"] and not chat.state["case_picker"]["matches"]
    assert "no claims" in chat.reply


def test_policy_picker_and_missing_lookup_do_not_pollute_active_case_memory(verified):
    before = verified.state["memory"]["case_history"]["turn_count"]
    verified.say("POL-9921")
    assert verified.state["memory"]["case_history"]["turn_count"] == before
    verified.say("CL-3001")
    assert verified.state["memory"]["case_history"]["turn_count"] == before
    verified.say("What documents do I need?")
    assert verified.state["memory"]["case_history"]["turn_count"] == before + 1
