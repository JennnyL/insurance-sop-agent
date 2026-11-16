"""Reference lookup without changing the active case until a valid selection exists."""

from .interpreter import eligible_identity, source_span
from .state import ALLOWED_FIELDS, event, transition

CASE_FIELDS = (
    "hints",
    "intents",
    "pending_topics",
    "selected_case_id",
    "candidates",
    "resolution_kind",
    "no_match_asked",
    "discussed",
    "last_answer_topics",
    "case_memory",
    "evidence",
    "open_questions",
    "user_confirmed_resolved",
)


def reference_lookup(state, observation, text, gateway, events):
    # Supplying a policy locator does not change the authenticated person.
    if (
        not state["auth"]
        or any(i.field in ALLOWED_FIELDS for i in observation.identity)
        or observation.action not in {"continue", "case_lookup", "my_claims"}
    ):
        return None
    policies = []
    for item in observation.identity:
        if item.field == "policy_number" and (eligible := eligible_identity(item, text)):
            policies.append(eligible[0])
    refs = [
        h.value
        for h in observation.hints
        if h.field == "case_id"
        and h.assertion in {"affirm", "replace"}
        and source_span(text, h.quote, h.occurrence)
    ]
    if not policies and not refs:
        return None
    account = gateway.account(state)
    claims = account["claims"]
    if policies and (len(set(policies)) != 1 or policies[0] != account["profile"]["policy_number"]):
        return offer(
            state,
            policies[-1],
            [],
            "No matching policy was found in your verified account. Check the policy number or use My claims. Your previous claim remains selected.",
            events,
        )
    if refs and refs[-1] not in {c["case_id"] for c in claims}:
        return offer(
            state,
            refs[-1],
            [],
            "No matching claim was found in your verified account. Check the claim number or search by policy. Your previous claim remains selected.",
            events,
        )
    if policies and not refs:
        return offer(
            state,
            policies[-1],
            claims,
            "These are the claims for your verified policy. A policy can contain several claims. Choose a claim to switch; your current claim stays selected until then."
            if claims
            else "This policy belongs to your verified account, but the demo has no claims for it. You can request representative assessment or send an email enquiry.",
            events,
        )
    # A valid explicit case selection proceeds through the normal case controller.
    state["case_picker"] = None
    return None


def offer(state, query, claims, notice, events):
    matches = [{k: c[k] for k in ("case_id", "case_type", "status", "created_at")} for c in claims]
    state["case_picker"] = {
        "query": query,
        "notice": notice,
        "matches": matches,
        "current_case_id": state["selected_case_id"],
    }
    events.append(event("case.lookup", count=len(matches), result="CANDIDATES" if matches else "NO_MATCH"))
    rows = "\n".join(
        f"{c['case_id']} · {c['case_type']} · {c['status']} · created {c['created_at']}" for c in matches
    )
    return notice + ("\n" + rows if rows else "")


def restore_after_no_match(state, previous, events):
    if not previous or not previous["selected_case_id"] or previous["auth_epoch"] != state["auth_epoch"]:
        return None
    for key in CASE_FIELDS:
        if key in previous:
            state[key] = previous[key]
        else:
            state.pop(key, None)
    transition(state, "PROCESS_CASE", events, "UNMATCHED_LOOKUP_RESTORED_PREVIOUS_CASE")
    return offer(
        state,
        "Claim description",
        [],
        "No claim matched that description in your account. Your previous claim remains selected. Check the description, enter a policy/claim number, or use My claims. No claim has been switched.",
        events,
    )
