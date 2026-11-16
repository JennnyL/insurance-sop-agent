"""Caller identity and consultation boundaries, separate from insured claim facts."""

import time


def caller_context(state):
    auth = state.get("auth") or {}
    rep = state.get("representative") or auth.get("representative")
    caller = auth.get("caller") or (
        {"role": "representative", "name": rep.get("caller", "")}
        if rep
        else {"role": "policyholder", "name": ""}
    )
    return {
        "caller": caller,
        "data_subject": auth.get("data_subject"),
        "authorization_method": "policyholder_consent" if rep else "three_identity_fields",
    }


def recipient_label(state):
    return "the policyholder's registered email" if state.get("representative") else "your registered email"


def representation_statement(state):
    context = caller_context(state)
    if context["caller"]["role"] != "representative":
        return ""
    name = (context["data_subject"] or {}).get("name", "the policyholder")
    return (
        f"This consultation was conducted by {context['caller']['name']} as representative for "
        f"policyholder {name}, with the policyholder's approved consent."
    )


def correspondence_visible(state, record):
    """Claim consent does not grant access to another caller's correspondence."""
    rep = state.get("representative")
    if not rep:
        return True
    creator = record.get("caller") or {}
    # Legacy representative summaries may already contain imported policyholder
    # dialogue. Only records with explicit caller provenance are safe to expose.
    return creator.get("role") == "representative" and creator.get("name") == rep["caller"]


def ticket_facts(tickets):
    """Models get tracking facts, never other consultations' transcript/summary."""
    return [{k: t[k] for k in ("id", "case_id", "status", "label", "can_update") if k in t} for t in tickets]


def migrate_consultation(state, events):
    if state.get("consultation_schema") == 2:
        return
    from .state import clear_case, event, invalidate_draft

    state["consultation_schema"] = 2
    state["case_memory"] = {}
    state["discussed"] = []
    state["open_questions"] = []
    state["last_answer_topics"] = []
    state["profile_memory"] = {}
    had_draft = bool(state.get("draft"))
    invalidate_draft(state)
    if state.get("representative") and state.get("auth"):
        # Old representative replies/drafts may contain another caller's dialogue.
        # Keep consent and the grant, but never render or send those old artifacts.
        clear_case(state)
        state["privacy_boundary_at"] = time.time()
        state["phase"], state["lifecycle"] = "RESOLVE_INTENT", "ACTIVE"
        state["handoff"] = None
        state["messages"] = [
            {
                "role": "system",
                "protected": True,
                "epoch": state["auth_epoch"],
                "text": "Your representative authorization remains active. The previous conversation view and unsent draft were withdrawn after a privacy update. Choose a claim to start a fresh consultation summary.",
            }
        ]
    elif had_draft:
        state["messages"].append(
            {
                "role": "system",
                "protected": True,
                "epoch": state["auth_epoch"],
                "text": "The previous unsent summary was withdrawn after a consultation privacy update. Please review a new summary before sending.",
            }
        )
    state.pop("pending_handoff_note", None)
    state["pending_receipts"] = []
    events.append(event("consultation.privacy_updated", draft_withdrawn=had_draft))
