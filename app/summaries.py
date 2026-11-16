"""Summary facts and writing contract; uses the same grounded model reviewer."""

import copy

from .consultation import caller_context, ticket_facts
from .next_steps import next_steps

TOPIC_LABELS = {
    "status": "claim status",
    "denial": "reason for the recorded denial",
    "documents": "missing documents",
    "alternatives": "document alternatives",
    "amounts": "recorded amounts",
    "deadline": "appeal deadline",
    "timing": "review timing",
    "submission": "available next steps",
    "receipt": "document receipt",
    "notification": "the availability of notification records",
}

SUMMARY_FOOTER = "Demo notice: this conversation copy and all email and representative handling are simulated. No real email is sent and no real representative is contacted."


def summary_context(engine, state, owner, events):
    representative = caller_context(state)["caller"]["role"] == "representative"
    tickets = [t for t in engine.store.handoffs(state, owner) if not t["details_locked"]]
    tickets = [t for t in tickets if t.get("case_id") in {None, state["selected_case_id"]}]
    pending = next((t for t in tickets if t["can_update"]), None)
    claim = engine.gateway.get_claim(state, events) if state["selected_case_id"] else None
    policy = next_steps(claim, engine.settings.business_date, pending, representative=representative)
    evidence = []
    if claim:
        working = copy.deepcopy(state)
        topics = list(
            dict.fromkeys(["status", "deadline"] + [t for t in state["discussed"] if t in TOPIC_LABELS])
        )
        _, evidence = engine.renderer.answer(
            working, topics, events, pending=pending, notification="notification" in state["discussed"]
        )
    if representative:
        for record in [policy, *evidence]:
            record["text"] = (
                record["text"]
                .replace("Your existing request", "The representative's existing request")
                .replace("You can", "The representative or policyholder may")
            )
    conclusion = (
        f"Claim {claim['case_id']} remains {claim['status']}. No claim decision or submission was made in this conversation."
        if claim
        else "No matching claim was found in the available account records; a representative can check the reference."
    )
    topics = (
        ", ".join(dict.fromkeys(TOPIC_LABELS[t] for t in state["discussed"] if t in TOPIC_LABELS))
        or "your claim question"
    )
    references = (
        ", ".join(([claim["case_id"]] if claim else []) + [t["id"] for t in tickets])
        or "No matching claim reference"
    )
    sections = [
        f"Conclusion: {conclusion}",
        f"Discussion: The representative asked about {topics}."
        if representative
        else f"Discussion: We discussed {topics}.",
        f"Next steps: {policy['text']}",
        f"References: {references}.",
    ]
    if state["open_questions"]:
        author = "representative" if representative else "customer"
        sections[1] += f" Unresolved {author} questions (quoted): " + "; ".join(state["open_questions"])
    fallback = "\n\n".join(sections)
    return fallback, {
        **caller_context(state),
        "summary_audience": {
            "recipient": "policyholder",
            "question_author": "representative" if representative else "policyholder",
        },
        "current_action": "summary",
        "input_method": "summary",
        "current_message": "Write the conversation email summary from these verified facts.",
        "conversation": [],
        "remembered_case_dialogue": [],
        "suggested_topics": [],
        "phase": "POST_PROCESS",
        "verified": True,
        "business_date": engine.settings.business_date.isoformat(),
        "emotion": "neutral",
        "next_steps": policy,
        "support_requests": ticket_facts(tickets),
        "authorized_evidence": [{k: e[k] for k in ("id", "text", "source", "kind")} for e in evidence],
        "server_guidance": "Write exactly four short paragraphs with headings Conclusion:, Discussion:, Next steps:, References:. "
        "Summarize only topics discussed in THIS consultation. Account records and prior request existence do not mean their history was discussed. Discussion must use only the listed discussion topics. Use customer-facing language; never emit internal topic labels unknown or verification. State the claim status once only. "
        "Next steps must identify who acts and any known deadline; if time is not established say no response time is promised. "
        "Include every claim and human request reference below. Do not invent dates, amounts, notifications or options. "
        "The application adds a separate demonstration notice in the email footer. Do not put demo/simulated/no-real-contact language in these four body paragraphs. Do not promise actual contact or assignment; describe the recorded request and needed assessment. "
        "Customer questions are attributed questions, not claim facts. Grounded structure:\n" + fallback,
    }
