"""One decision about permissible next actions for chat, buttons and summaries."""

from datetime import date


def next_steps(claim, today, pending=None, representative=False):
    """Representative callers get wording that names who acts (the representative or the policyholder)."""
    deadline = claim.get("appeal_deadline") if claim else None
    expired = bool(deadline and date.fromisoformat(deadline) < today)
    if expired:
        text = "The recorded appeal deadline has passed. Only a representative can assess whether any options remain; no exception, extension or late acceptance is established by these records. "
        if representative:
            text += (
                f"The representative's existing request {pending['id']} is available for representative assessment. The representative or policyholder may check this request for updates or add a question; no response time is promised."
                if pending
                else "The representative or policyholder may submit a human handling request for representative assessment; no response time is promised."
            )
        else:
            text += (
                f"Your existing request {pending['id']} is available for representative assessment. You can check this request for updates or choose to add a question; no response time is promised."
                if pending
                else "You can submit a human handling request for representative assessment; no response time is promised."
            )
        return {
            "expired": True,
            "deadline": deadline,
            "actions": [
                {
                    "label": "View human request" if pending else "Request representative assessment",
                    "action": "handoff_status" if pending else "human",
                }
            ],
            "text": text,
        }
    return {
        "expired": False,
        "deadline": deadline,
        "actions": [{"label": "Ask about next steps", "action": "question"}],
        "text": (
            "The representative or policyholder may ask a claims representative"
            if representative
            else "You can ask a claims representative"
        )
        + " to confirm applicable submission requirements and any unrecorded time limits. No claim action has been performed.",
    }
