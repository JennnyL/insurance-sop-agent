import time
import uuid

PHASES = ["VERIFY_ID", "RESOLVE_INTENT", "PROCESS_CASE", "POST_PROCESS"]
# national_id_last4 stands in for SSN digits for policyholders whose record uses a national ID.
ALLOWED_FIELDS = ("name", "dob", "phone", "email", "ssn_last4", "national_id_last4")
FIELD_LABELS = {
    "name": "full name",
    "dob": "date of birth (YYYY-MM-DD)",
    "phone": "registered phone",
    "email": "registered email",
    "ssn_last4": "last four SSN digits (or national ID digits if you have no SSN)",
    "national_id_last4": "last four national ID digits",
}
LISTED_FIELDS = ("name", "dob", "phone", "email", "ssn_last4")
WELCOME = "Hello, I’m your claims support assistant. I can help explain a claim, its amounts, or the documents you need. First, to protect your information, please share any three: full name, date of birth, registered phone, registered email, or the last four digits of your SSN. You can tell me what you’re calling about at the same time."


def uid(prefix):
    return prefix + "_" + uuid.uuid4().hex


def new_state(sid=None, version=0, epoch=0):
    return {
        "consultation_schema": 2,
        "session_id": sid or uid("s"),
        "episode_id": uid("ep"),
        "phase": "VERIFY_ID",
        "lifecycle": "ACTIVE",
        "version": version,
        "auth_epoch": epoch,
        "episode_version": 0,
        "created_at": time.time(),
        "auth": None,
        "representative": None,
        "empathy_used": [],
        "identity": {},
        "refused_fields": [],
        "identity_conflicts": [],
        "verification_attempts": 0,
        "verification_locked": False,
        "hints": {},
        "intents": [],
        "pending_topics": [],
        "selected_case_id": None,
        "candidates": [],
        "case_picker": None,
        "resolution_kind": None,
        "no_match_asked": False,
        "scope_retries": 0,
        "email_preference": "unmentioned",
        "draft": None,
        "email_operation": None,
        "handoff": None,
        "support_mode": "ai",
        "closing_pending": False,
        "persuasion_count": 0,
        "discussed": [],
        "last_answer_topics": [],
        "profile_memory": {},
        "case_memory": {},
        "evidence": [],
        "open_questions": [],
        "user_confirmed_resolved": False,
        "visited_phases": ["VERIFY_ID"],
        "last_emotion": "neutral",
        "last_model_status": None,
        "messages": [{"role": "assistant", "text": WELCOME, "protected": False, "epoch": epoch}],
    }


def auth_valid(state, settings, now=None):
    now = time.time() if now is None else now
    auth = state.get("auth")
    return bool(
        auth
        and (not auth.get("representative") or auth["representative"].get("status") == "approved")
        and len(set(auth.get("fields", [])) & set(ALLOWED_FIELDS)) >= 3
        and auth["epoch"] == state["auth_epoch"]
        and now < auth["expires_at"]
        and now - auth["last_used_at"] < settings.auth_idle_seconds
    )


def event(kind, **details):
    return {"kind": kind, "details": details, "at": time.time()}


def transition(state, target, events, rule):
    edges = {
        "VERIFY_ID": {"RESOLVE_INTENT"},
        "RESOLVE_INTENT": {"PROCESS_CASE", "VERIFY_ID"},
        "PROCESS_CASE": {"POST_PROCESS", "RESOLVE_INTENT", "VERIFY_ID"},
        "POST_PROCESS": {"PROCESS_CASE", "RESOLVE_INTENT", "VERIFY_ID"},
    }
    old = state["phase"]
    if old == target:
        return
    if target not in edges[old]:
        raise ValueError("Illegal phase transition")
    state["phase"] = target
    if target not in state["visited_phases"]:
        state["visited_phases"].append(target)
    events.append(event("phase.changed", source=old, target=target, rule=rule))


def invalidate_draft(state):
    state["draft"] = None
    state["email_operation"] = None


def clear_case(state):
    state.pop("handoff_note_offer", None)
    state.pop("claim_confirmation", None)
    state["next_steps"] = None
    state["closing_pending"] = False
    state.update(
        hints={},
        intents=[],
        pending_topics=[],
        selected_case_id=None,
        candidates=[],
        case_picker=None,
        resolution_kind=None,
        no_match_asked=False,
        discussed=[],
        last_answer_topics=[],
        case_memory={},
        evidence=[],
        open_questions=[],
        user_confirmed_resolved=False,
    )
    state["episode_version"] += 1
    invalidate_draft(state)


def revoke_auth(state, events, reason, preserve_hints=False):
    hints, intents = state["hints"], state["intents"]
    state["auth_epoch"] += 1
    state["auth"] = None
    state["handoff"] = None
    state["representative"] = None
    state["support_mode"] = "ai"
    state["pending_receipts"] = []
    state.pop("pending_handoff_note", None)
    state["messages"] = [
        {"role": "assistant", "text": WELCOME, "protected": False, "epoch": state["auth_epoch"]}
    ]
    state["profile_memory"] = {}
    state["identity"] = {}
    state["identity_conflicts"] = []
    state["refused_fields"] = []
    state["verification_attempts"] = 0
    state["verification_locked"] = False
    state["persuasion_count"] = 0
    clear_case(state)
    if preserve_hints:
        state["hints"], state["intents"] = hints, intents
    transition(state, "VERIFY_ID", events, reason)
    events.append(event("auth.revoked", rule=reason))
