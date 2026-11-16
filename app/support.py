"""Owned support requests and shared browser-chat / simulation record shapes."""

import hashlib
import json
import time

from .consultation import caller_context, correspondence_visible
from .state import auth_valid, uid

TERMINAL = {"SIMULATED_CLOSED", "CANCELLED", "CLOSED"}
LABELS = {
    "SIMULATED_RECORDED": "Awaiting representative review (simulated)",
    "SIMULATED_ACKNOWLEDGED": "Acknowledged (simulated)",
    "SIMULATED_RESPONDED": "Response available (simulated)",
    "SIMULATED_CLOSED": "Closed (simulated)",
    "CANCELLED": "Cancelled",
    "WAITING": "Waiting for an online representative",
    "OFFLINE": "No representative online · request saved",
    "CONNECTED": "Representative joined",
    "CLOSED": "Conversation ended",
}


def make_ticket(state, reason, settings):
    verified = auth_valid(state, settings)
    now = time.time()
    return {
        "id": "H-" + uid("h")[2:18].upper(),
        "caller": caller_context(state)["caller"],
        "channel": "MOCK",
        "status": "SIMULATED_RECORDED",
        "reason": reason,
        "subject": state["auth"]["subject"] if verified else None,
        "case_id": state["selected_case_id"] if verified else None,
        "session_id": state["session_id"],
        "authorization_epoch": state["auth_epoch"],
        "episode_id": state["episode_id"],
        "created_at": now,
        "updated_at": now,
        "revision": 1,
        "summary": {
            "phase": state["phase"],
            "verified_at_request": verified,
            "discussed": state["discussed"] if verified else [],
            "open_questions": (
                state["open_questions"]
                or ["Customer requests representative review of the unresolved discussion."]
            )
            if verified
            else ["Identity verification and the customer’s request need representative assistance."],
            "claim_description": {k: v["value"] for k, v in state["hints"].items()},
            "discussion": [
                m["text"]
                for m in state["messages"][-6:]
                if not m.get("protected") or verified and m.get("epoch") == state["auth_epoch"]
            ],
            "verified_identity": {"party_id": state["auth"]["subject"], "fields": state["auth"]["fields"]}
            if verified
            else None,
            "representative_context": {
                k: state["representative"].get(k)
                for k in ("caller", "subject", "fields", "status", "consent_id")
            }
            if state.get("representative")
            else None,
            "reason": reason,
            "contact_method": "Registered email or phone after identity verification",
        },
        "timeline": [
            {
                "at": now,
                "actor": "system",
                "status": "SIMULATED_RECORDED",
                "text": "Asynchronous request saved. In a connected service, a representative would contact you by registered email or phone; no response time is promised. This demo simulates handling and contacts nobody. Choose Continue with AI or Wait for human only.",
            }
        ],
    }


def public_ticket(ticket, state, settings):
    """A reference locates a record; only an owner plus grant unlocks its text."""
    sensitive = ticket.get("subject")
    unlocked = (
        auth_valid(state, settings)
        and state["auth"]["subject"] == sensitive
        and correspondence_visible(state, ticket)
        if sensitive
        else anonymous_access(ticket, state)
    )
    result = {k: ticket[k] for k in ("id", "channel", "status", "created_at", "updated_at", "revision")}
    result.update(
        label=LABELS.get(ticket["status"], ticket["status"]),
        response_time="NOT_CONFIGURED",
        simulated=ticket["channel"] == "MOCK",
        representative=ticket.get("representative"),
        details_locked=not unlocked,
        can_update=unlocked and ticket["status"] not in TERMINAL,
    )
    if unlocked:
        result.update(case_id=ticket.get("case_id"), summary=ticket["summary"], timeline=ticket["timeline"])
    return result


def anonymous_access(ticket, state):
    return (
        not ticket.get("subject")
        and ticket["session_id"] == state["session_id"]
        and ticket.get("authorization_epoch") == state["auth_epoch"]
        and ticket.get("episode_id") == state["episode_id"]
    )


def update_ticket(ticket, action, text, state, settings):
    # Fixed, clearly labelled simulation events cannot alter claim facts.
    from .store import Conflict

    if not public_ticket(ticket, state, settings)["can_update"]:
        raise Conflict("HANDOFF_LOCKED_OR_CLOSED", 409)
    status = ticket["status"]
    actor = "system"
    if action == "cancel":
        status, message = "CANCELLED", "Demo request cancelled. Your chat and claim records are unchanged."
    elif action == "note":
        actor, message = "customer", text.strip()
    elif ticket["channel"] != "MOCK":
        raise Conflict("SIMULATION_NOT_ALLOWED_FOR_LIVE_REQUEST", 409)
    elif action == "demo_acknowledge" and status == "SIMULATED_RECORDED":
        status, actor = "SIMULATED_ACKNOWLEDGED", "demo_representative"
        message = "SIMULATED staff update: this demo request has been acknowledged. No real representative is assigned and no response time is promised."
    elif action == "demo_reply" and status in {"SIMULATED_RECORDED", "SIMULATED_ACKNOWLEDGED"}:
        status, actor = "SIMULATED_RESPONDED", "demo_representative"
        message = (
            "SIMULATED staff response: your request is available for review in this demo. "
            "For actual help, use the claims-service contact listed on your insurance card or official insurer portal and provide your claim reference. "
            "Ask the representative to confirm the current status and any missing information. "
            "This demo does not provide a verified contact address or submit documents. No claim decision or deadline has changed. "
            "You may add a follow-up below; it will be saved here only."
        )
    elif action == "demo_close" and status == "SIMULATED_RESPONDED":
        status, actor = "SIMULATED_CLOSED", "demo_representative"
        message = "SIMULATED staff update: this demo request is closed. Closure does not mean the claim was resolved. You can create another request if needed."
    else:
        raise Conflict("INVALID_HANDOFF_TRANSITION")
    if len(ticket["timeline"]) >= 100:
        raise Conflict("HANDOFF_HISTORY_LIMIT", 429)
    ticket["status"], ticket["updated_at"] = status, time.time()
    ticket["revision"] += 1
    ticket["timeline"].append({"at": ticket["updated_at"], "actor": actor, "status": status, "text": message})


def fingerprint_update(store, body):
    return store.fingerprint(json.dumps(body.model_dump(exclude={"expected_revision"}), sort_keys=True))


def draft_hash(draft):
    return hashlib.sha256(
        json.dumps({k: draft[k] for k in ("body", "recipient", "subject")}, sort_keys=True).encode()
    ).hexdigest()
