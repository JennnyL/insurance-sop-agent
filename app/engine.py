import asyncio
import copy
import hashlib
import json
import re
import time

from .capabilities import EMPATHY, INTERFACE, SSN_ALTERNATIVES
from .case_lookup import reference_lookup, restore_after_no_match
from .claims import AccessDenied, EvidenceRenderer, Gateway
from .consultation import caller_context, recipient_label, representation_statement, ticket_facts
from .interpreter import ModelFailure, OfflineInterpreter, eligible_identity, normalize, source_span
from .next_steps import next_steps
from .privacy import redact
from .representatives import RepresentativeFlow, receipt
from .schemas import Hint, IdentityObservation, empty_observation
from .state import (
    ALLOWED_FIELDS,
    FIELD_LABELS,
    LISTED_FIELDS,
    auth_valid,
    clear_case,
    event,
    invalidate_draft,
    revoke_auth,
    transition,
    uid,
)
from .store import Conflict
from .summaries import SUMMARY_FOOTER, summary_context
from .support import anonymous_access, draft_hash, public_ticket

ACCEPT_EMAIL = {
    "yes",
    "yes please",
    "yes, please",
    "please send it",
    "send it",
    "send the summary",
    "send the email",
}
SKIP_EMAIL = {
    "no",
    "no thanks",
    "no, thanks",
    "skip",
    "skip email",
    "don't email me",
    "do not email me",
    "no email",
}
END_TEXT = {
    "thanks",
    "thank you",
    "thanks, that's all",
    "thank you, that's all",
    "that's all",
    "nothing else",
    "done",
    "all done",
    "没别的了",
    "没有了",
    "谢谢",
    "谢谢，没有了",
}
PAUSE_TEXT = {
    "wait",
    "hold on",
    "one moment",
    "pause",
    "pause please",
    "stop",
    "stop for a moment",
    "等一下",
    "等等",
    "暂停",
    "先停一下",
}


def consent_text(text):
    # Keep question marks, negations, internal punctuation and conditional clauses.
    return text.strip().lower().replace("’", "'").rstrip(".! ")


SERVICE_NOTE = (
    "Note: the AI model can’t be reached right now, so this reply comes from the built-in "
    "scripted assistant. Identity checks and claim records work the same way."
)
BUDGET_NOTE = (
    "Note: this demo has reached its local API spending limit, so replies now come from the built-in "
    "scripted assistant. Identity checks and claim records work the same way."
)
LIMIT_NOTE = (
    "Note: this conversation has reached its AI call limit, so replies now come from the built-in "
    "scripted assistant. Start a New session to reset the limit."
)


def fallback_note(code):
    if code == "MODEL_CONVERSATION_LIMIT":
        return LIMIT_NOTE
    return BUDGET_NOTE if "BUDGET" in code else SERVICE_NOTE


CONSENT_EXPLANATION = (
    "Claim details are the policyholder’s private information, so they can only be shared with the "
    "policyholder’s approval. Consent protects the policyholder’s privacy even from close family, and a "
    "relationship on file doesn’t authorize access by itself. The consent request is still pending. "
    "Choose Check policyholder consent to see whether it has been approved, or Request human help to "
    "have a claims representative follow up."
)
CONSENT_PUSHBACK = re.compile(
    r"\bwhy\b.{0,60}\b(?:consent|permission|approv\w*|approval)\b|ridiculous|just tell me|"
    r"\bI[’']?m (?:her|his|their) (?:son|daughter|child|husband|wife|spouse|partner|mother|father|parent)\b|"
    r"\bwe(?:[’']re| are) family\b",
    re.I,
)
HUMAN_OPTION = (
    "If you’d rather not verify here, choose Request human help and I’ll record a human handling "
    "request (H-reference) for a claims representative to review."
)
# One acknowledgment per reply: the server's empathy line, or else the first one found.
_EMPATHY_SENTENCE = re.compile(
    r"^(?:(?:I (?:understand|hear|know|can see|realize)|I can imagine|It[’']s understandable|"
    r"That (?:sounds|must be))\b[^.!?\d]{0,120}"
    r"\b(?:frustrat\w*|upset\w*|worr\w*|concern\w*|stress\w*|difficult|hard|angry|anger|confus\w*|"
    r"annoy\w*|anxi\w*|uncertain\w*|scar\w*|inconvenien\w*|feel\w*|privacy|personal information)"
    r"[^.!?\d]{0,80}[.!]|I(?:[’']m| am) (?:so |really |very )?sorry\b[^.!?\d]{0,120}[.!]|"
    r"I (?:respect|appreciate) your\b[^.!?\d]{0,100}[.!])$",
    re.I,
)
_EMPATHY_PHRASES = {p for phrases in EMPATHY.values() for p in phrases}


def _is_empathy(sentence):
    if sentence in _EMPATHY_PHRASES:
        return True
    if re.search(r"\b(?:can[’']?t|cannot|unable|won[’']?t|but)\b", sentence, re.I):
        return False  # a refusal or statement of limits is content, not a courtesy
    return bool(_EMPATHY_SENTENCE.match(sentence))


def limit_empathy(reply):
    """Keep at most one empathy sentence per reply; never touch other sentences."""
    seen, paragraphs = False, []
    for paragraph in reply.split("\n\n"):
        sentences = re.split(r"(?<=[.!?])\s+", paragraph)
        kept = []
        for sentence in sentences:
            if _is_empathy(sentence.strip()):
                if seen:
                    continue
                seen = True
            kept.append(sentence)
        text = " ".join(kept).strip()
        if text:
            paragraphs.append(text)
    return "\n\n".join(paragraphs) if paragraphs else reply


def mask_email(email):
    name, domain = email.split("@", 1)
    return name[:1] + "•••@" + domain


def confirms_claim(text):
    return bool(
        re.fullmatch(
            r"(?:yes(?:[,\s]+(?:this claim|that['’]s (?:right|correct)))?|correct|that['’]s right|是的|对)[.!。！\s]*",
            text.strip(),
            re.I,
        )
    )


def public_state(state, settings):
    verified = auth_valid(state, settings)
    messages = []
    for m in state["messages"]:
        if m.get("protected") and (not verified or m.get("epoch") != state["auth_epoch"]):
            continue
        messages.append({"role": m["role"], "text": m["text"], "at": m.get("at", 0)})
    draft = None
    if verified and state["draft"]:
        draft = {k: state["draft"][k] for k in ("id", "subject", "body", "hash", "expires_at", "paused")}
        draft["note"] = state["draft"].get("note", "")
        draft["recipient"] = mask_email(state["draft"]["recipient"])
        draft["footer"] = SUMMARY_FOOTER
        draft["recipient_label"] = recipient_label(state)
    evidence = (
        [
            {
                k: r[k]
                for k in ("id", "source", "record_id", "field_path", "kind", "source_as_of", "retrieved_at")
            }
            for r in state["evidence"]
        ]
        if verified
        else []
    )
    return {
        "session_id": state["session_id"],
        "episode_id": state["episode_id"],
        "phase": state["phase"],
        "lifecycle": state["lifecycle"],
        "version": state["version"],
        "authorization_epoch": state["auth_epoch"],
        "verified": verified,
        **(caller_context(state) if verified else {"caller": None, "data_subject": None}),
        "claim_confirmation": state.get("claim_confirmation") if verified else None,
        "handoff_note_offer": {"id": state["handoff_note_offer"]["id"]}
        if verified and state.get("handoff_note_offer")
        else None,
        "representative_consent": {
            k: state["representative"].get(k) for k in ("status", "consent_id", "index", "scenario")
        }
        if state.get("representative")
        else None,
        "support_mode": state.get("support_mode", "ai"),
        "anonymous_support_active": bool(
            state.get("handoff")
            and anonymous_access(state["handoff"], state)
            and state["handoff"]["status"] not in {"CANCELLED", "CLOSED", "SIMULATED_CLOSED"}
        ),
        "closing_pending": state.get("closing_pending", False),
        "next_steps": state.get("next_steps") if verified else None,
        "identity_collected": list(state["identity"]),
        "identity_count": len([f for f in state["identity"] if f in ALLOWED_FIELDS])
        if not verified
        else len(state["auth"]["fields"]),
        "refused_fields": state["refused_fields"],
        "verification_locked": state["verification_locked"],
        "memory": {
            "hints": {
                k: {"value": v["value"], "source_turn": v["source_turn"], "provenance": "user_reported"}
                for k, v in state["hints"].items()
            },
            "intents": [intent for intent in state["intents"] if intent != "human_help"],
            "pending_topics": [
                t for t in state.get("pending_topics", []) if t not in {"unknown", "verification"}
            ],
            "recent_cases": state.get("profile_memory", {}).get("recent_cases", []) if verified else [],
            "case_history": {
                "case_id": state["selected_case_id"],
                "turn_count": len(state.get("case_memory", {}).get("dialogue", [])),
                "discussed_topics": [
                    t
                    for t in state.get("case_memory", {}).get("discussed", [])
                    if t not in {"unknown", "verification"}
                ],
                "open_questions": state.get("case_memory", {}).get("open_questions", []),
                "updated_at": state.get("case_memory", {}).get("updated_at"),
            }
            if verified and state["selected_case_id"]
            else None,
        },
        "selected_case_id": state["selected_case_id"] if verified else None,
        "candidates": state["candidates"] if verified else [],
        "case_picker": state.get("case_picker") if verified else None,
        "resolution_kind": state["resolution_kind"] if verified else None,
        "messages": messages,
        "draft": draft,
        "email_operation": state["email_operation"] if verified else None,
        "handoff": public_ticket(state["handoff"], state, settings)
        if verified and state["handoff"] and state["handoff"].get("subject") == state["auth"]["subject"]
        else None,
        "evidence": evidence,
        "visited_phases": state["visited_phases"],
        "model_status": state["last_model_status"],
        "user_confirmed_resolved": state["user_confirmed_resolved"],
        "business_date": settings.business_date.isoformat(),
        "historical_clock": bool(settings.demo_as_of),
    }


class Engine(RepresentativeFlow):
    def __init__(self, settings, store, repository, interpreter):
        self.settings, self.store, self.repo, self.interpreter = settings, store, repository, interpreter
        self.gateway = Gateway(repository, settings)
        self.renderer = EvidenceRenderer(self.gateway)
        self.observations = {}

    async def _observe(self, sid, turn_id, text, context):
        return await self._model_task(sid, turn_id, self.interpreter.observe(text, context))

    async def _model_task(self, sid, turn_id, awaitable):
        task = asyncio.create_task(awaitable)
        self.observations[(sid, turn_id)] = task
        try:
            return await task
        except asyncio.CancelledError:
            raise Conflict("TURN_SUPERSEDED") from None
        finally:
            self.observations.pop((sid, turn_id), None)

    async def _compose(self, state, owner, turn_id, request, obs, guidance, events):
        for phrases in EMPATHY.values():
            for phrase in phrases:
                guidance = guidance.removeprefix(phrase).lstrip()
        verified = auth_valid(state, self.settings)
        service_lookup = obs.action in {"my_claims", "handoff_status", "inquiry_status", "case_lookup"}
        deferred_topics = (
            [t for t in state.get("pending_topics", []) if t not in {"unknown", "verification"}]
            if verified and state["selected_case_id"] and not service_lookup
            else []
        )
        form_selection = (
            request.action in {"form", "confirm_claim"}
            and verified
            and state["selected_case_id"]
            and not service_lookup
            and not set(deferred_topics) - {"status"}
        )
        evidence = []
        if (
            verified
            and state["selected_case_id"]
            and state["phase"] in {"PROCESS_CASE", "POST_PROCESS"}
            and obs.action not in {"my_claims", "handoff_status", "inquiry_status", "case_lookup"}
        ):
            working = copy.deepcopy(state)
            working["discussed"] = []
            _, evidence = self.renderer.answer(
                working,
                [
                    "status",
                    "denial",
                    "amounts",
                    "alternatives",
                    "submission",
                    "receipt",
                    "documents",
                    "deadline",
                    "timing",
                    "unknown",
                ],
                events,
                pending=self._pending_support(state, owner),
                notification=bool(re.search(r"notif|nobody tell|no one told|通知", request.text, re.I)),
            )
        if form_selection and evidence:
            evidence = [e for e in evidence if e.get("field_path") == "status,case_type,created_at"]
        current_message = redact(request.text, obs, self.repo.policyholders)
        if request.action == "form":
            locators = {
                key: value
                for key, value in request.form.model_dump().items()
                if key in {"policy_number", "case_id"} and value
            }
            current_message = "Form submitted. " + str(locators)
        if verified and obs.action == "case_lookup" and state.get("case_picker"):
            evidence = [
                {
                    "id": "account:" + claim["case_id"],
                    "text": f"{claim['case_id']}: {claim['case_type']}, status {claim['status']}, created {claim['created_at']}",
                    "source": "verified_account",
                    "kind": "case_locator",
                }
                for claim in state["case_picker"]["matches"]
            ]
        history = [
            {"role": m["role"], "text": m["text"][:1200]}
            for m in state["messages"]
            if not m.get("protected")
            or (
                verified
                and m.get("epoch") == state["auth_epoch"]
                and "case_id" in m
                and m["case_id"] == state["selected_case_id"]
            )
        ][-4:]
        if (
            state["phase"] == "PROCESS_CASE"
            and state["resolution_kind"] == "MATCH"
            and obs.scope != "out_of_scope"
            and obs.action not in {"my_claims", "handoff_status", "inquiry_status", "case_lookup"}
        ):
            guidance += (
                "\nThe caller is verified and this claim is selected. Answer the current question using the evidence; "
                "acknowledge a newly passed verification and newly selected claim when server guidance includes that confirmation. Do not repeat the whole overview on later turns. Ask a clarification only if needed. "
                "No claim, appeal, upload or payment action has been performed. The user can finish for an optional summary."
            )
            if obs.scope == "mixed":
                guidance += " Briefly decline the unrelated part of the message."
        if form_selection:
            guidance = "The identity is verified and the form selected a claim. Confirm ONLY the claim ID, creation date and current status from the provided status fact, then invite a question. Do not discuss deadlines, documents, amounts or human requests on this selection-only turn. The server adds identity and selection receipts. No claim action has been performed."
        elif deferred_topics:
            guidance += "\nThe caller already asked about deferred_topics before the identity/consent/selection gates. Those gates have now passed for this selected claim. Answer these unanswered questions now using authorized evidence, even if the current input only confirms selection. Do not ask the caller to repeat them."
        context = {
            **caller_context(state),
            "current_action": obs.action,
            "lookup_result": state.get("case_picker") if obs.action == "case_lookup" else None,
            "interface": INTERFACE,
            "input_method": request.action,
            "current_message": current_message,
            "conversation": [] if service_lookup or form_selection else history,
            "remembered_case_dialogue": [
                {"user": item["user"][:500], "assistant": item["assistant"][:1000]}
                for item in ([] if service_lookup else state.get("case_memory", {}).get("dialogue", [])[-3:])
            ],
            "remembered_open_questions": []
            if service_lookup
            else state.get("case_memory", {}).get("open_questions", []),
            "phase": state["phase"],
            "verified": verified,
            "business_date": self.settings.business_date.isoformat(),
            "user_reported_hints": {}
            if service_lookup
            else {k: v["value"] for k, v in state["hints"].items()},
            "last_answer_topics": [] if service_lookup else state.get("last_answer_topics", []),
            "recent_cases": state.get("profile_memory", {}).get("recent_cases", []),
            "suggested_topics": list(dict.fromkeys(obs.topics + deferred_topics)),
            "deferred_topics": deferred_topics,
            "emotion": "neutral",
            "empathy_handled": True,
            "next_steps": None if form_selection else state.get("next_steps"),
            "server_guidance": guidance
            + "\nAddress caller.name, never data_subject.name when caller.role is representative. The data subject owns the policy and registered email; do not call it the representative's own email or claim. Discuss only this consultation; do not claim historical ticket content was discussed. Never say a question was added to a request until an action receipt confirms it. Keep answers concise and avoid repeating the claim overview.\nEmotion was evaluated from the current message only. The server supplies any empathy separately. Do not add an emotional acknowledgment or repeat one from history. Product capabilities are fixed by interface; unverified callers cannot use Email enquiry without first verifying. If an open applicable H request exists, refer to it; never suggest creating a new one. "
            + "\nInterface: My claims lists your account records. Email enquiry opens the simulated official-email enquiry form and E-reference progress/history linked to your account and selected claim. No real destination mailbox is configured. Email enquiry is for writing a question to the insurer; Human requests is an asynchronous escalation. Finish consultation offers an optional conversation copy to the customer with Send or Skip. Do not direct users to an external mail client. Reuse the current valid session verification for claims, history and email; do not ask to verify again while verified is true. Human requests tracks asynchronous simulated escalation. Never imply a live human is chatting. The speaker is always the AI assistant.",
            "support_requests": ticket_facts(
                [] if form_selection else self.store.support_tickets(state, owner)[:3]
            ),
            "authorized_candidates": state["candidates"] if verified else [],
            "authorized_evidence": [
                {k: item[k] for k in ("id", "text", "source", "kind")} for item in evidence
            ],
        }
        prior_topics = state.get("_topics_before_response", state["discussed"])
        reply, cited, topics, stats = await self._model_task(
            state["session_id"], turn_id, self.interpreter.answer(context)
        )
        self.store.assert_current(state["session_id"], owner, turn_id)
        if verified and not auth_valid(state, self.settings):
            raise AccessDenied("Grant expired during response generation")
        state["model_calls_used"] = state.get("model_calls_used", 0) + stats.get("calls", 0)
        events.append(event("model.composed", **stats))
        events.append(event("answer.model_grounded", references=sorted(cited)))
        state["last_model_status"] = stats
        state.pop("_topics_before_response", None)
        for item in evidence:
            if item["id"] in cited and not service_lookup:
                state["evidence"] = [old for old in state["evidence"] if old["id"] != item["id"]] + [item]
        if state["selected_case_id"] and obs.action not in {
            "my_claims",
            "handoff_status",
            "inquiry_status",
            "case_lookup",
        }:
            state["last_answer_topics"] = topics
            notification = (
                ["notification"]
                if any(e["id"] in cited and e.get("field_path") == "notification_history" for e in evidence)
                else []
            )
            state["discussed"] = list(dict.fromkeys(prior_topics + topics + notification))
            addressed = set(topics)
            if any(ref.endswith(":status,case_type,created_at") for ref in cited):
                addressed.add("status")
            state["pending_topics"] = [t for t in state.get("pending_topics", []) if t not in addressed]
        return reply

    def _safe_context(self, state):
        return {
            "phase": state["phase"],
            "verified": bool(state["auth"]),
            "identity_fields_collected": list(state["identity"]),
            "refused_fields": state["refused_fields"],
            "case_hints": {k: v["value"] for k, v in state["hints"].items()},
            "intents": [intent for intent in state["intents"] if intent != "human_help"],
            "discussed_topics": state["discussed"],
            "last_answer_topics": state.get("last_answer_topics", []),
            "no_match_asked": state["no_match_asked"],
            "email_offer_displayed": bool(state["draft"]),
            "has_human_request": bool(state["handoff"]),
            "representative_active": bool(state.get("representative")),
            "closing_pending": state.get("closing_pending", False),
            "ambiguous_term": state.get("ambiguous_term", False),
            "last_assistant_question": self._verification_prompt(state)
            if state["phase"] == "VERIFY_ID"
            else state["messages"][-1]["text"][-1600:]
            if state["messages"]
            else "",
        }

    def _user_text(self, request, observation, was_verify):
        if request.action == "form":
            return (
                "[Identity / claim lookup form submitted; private values are not retained in chat history.]"
            )
        if request.action != "message":
            return {
                "finish": "Finish & review summary",
                "email_send": "Send this summary (demo)",
                "email_skip": "Skip email",
                "human": "Requested human help",
                "resume": "Continued with AI",
                "wait_human": "Switched to wait-only mode",
                "handoff_note_accept": "Confirmed adding the question to the human request",
                "handoff_note_decline": "Declined adding the question to the human request",
                "confirm_claim": "Confirmed the selected claim",
                "consent_check": "Checked policyholder consent",
                "consent_timeout": "Selected simulated consent timeout",
            }.get(request.action, request.action)
        return redact(request.text, observation, self.repo.policyholders)

    def _human(self, state, owner, events, reason, request_text=""):
        state["handoff"], reused = self.store.prepare_handoff(state, owner, reason)
        state["lifecycle"] = "ACTIVE"
        ticket = state["handoff"]
        receipt(
            state,
            f"You already have request {ticket['id']}; I reused it. No duplicate request was created."
            if reused
            else f"Created human handling request {ticket['id']}.",
        )
        if reused and request_text:
            self.store.append_request_note(state, owner, request_text, unresolved=True)
        ticket["summary"]["handoff_reason"] = request_text or reason.replace("_", " ").lower()
        if state["auth"]:
            person = next(p for p in self.repo.policyholders if p["party_id"] == state["auth"]["subject"])
            ticket["summary"]["verified_identity"] = {
                "party_id": person["party_id"],
                "name": person["name"],
                "fields": state["auth"]["fields"],
            }
        events.append(
            event("handoff.recorded", handoff_id=ticket["id"], result=ticket["status"], reason=reason)
        )
        state["support_mode"] = "ai"
        return (
            f"Your asynchronous human handling request is {ticket['id']}. "
            f"A representative would follow up through {recipient_label(state)} or the policyholder’s registered phone; no response time is promised. "
            "This demo saves the request and simulates feedback; no real person is contacted. "
            "You are still with the AI assistant. Choose Continue with AI for insurance questions, or Wait for human only to save messages with a system acknowledgment. "
            "View updates or cancel in Human requests."
        )

    def _verification_receipt(self, state):
        name = next(p["name"] for p in self.repo.policyholders if p["party_id"] == state["auth"]["subject"])
        if state.get("representative"):
            return f"Policyholder consent approved. {state['representative']['caller']}, you are authorized to discuss policyholder {name}’s claims."
        return f"Identity verified, {name.split()[0]}."

    def _pending_support(self, state, owner):
        return next(
            (
                t
                for t in self.store.support_tickets(state, owner)
                if t["can_update"] and t.get("case_id") in {None, state["selected_case_id"]}
            ),
            None,
        )

    def _support_option(self, state, owner):
        pending = self._pending_support(state, owner)
        return (
            f"You already have request {pending['id']} — {pending['label']}. You can view updates or add a question to that request."
            if pending
            else "You can request help from a human claims representative; I can record an asynchronous request."
        )

    def _ssn_alternatives(self, state, owner):
        return (
            SSN_ALTERNATIVES
            + " "
            + self._support_option(state, owner)
            + " Email enquiry and My claims require verification; once verified, all panels reuse it."
        )

    def _empathy(self, state, emotion):
        used = state.setdefault("empathy_used", [])
        phrase = next((p for p in EMPATHY.get(emotion, []) if p not in used), "")
        if phrase:
            used.append(phrase)
        return phrase

    def _verification_prompt(self, state, failed=False, represented=False):
        if state.get("representative") and not represented:
            return self._representative_prompt(state)
        if state["verification_locked"] and state.get("verification_attempts", 0) < 3:
            return "I won’t keep asking for identity details. Claim details stay protected until verification. You can request human help or end this session."
        if state["verification_locked"]:
            return (
                "Verification is locked: three attempts didn’t match our records. For security, I can’t check "
                "further identity details in this browser for 30 minutes, even if they are correct. "
                "I can record a request for a human claims representative, or you can end this session."
            )
        if failed:
            return "I couldn’t verify the information together. You can correct a specific field, or resend a complete set of at least three identity fields and I’ll use that new set for the next check. You can also choose alternative identity fields or request human help."
        fields = [f for f in LISTED_FIELDS if f not in state["identity"] and f not in state["refused_fields"]]
        if "national_id_last4" in state["identity"] and "ssn_last4" in fields:
            fields.remove("ssn_last4")
        count = len([f for f in state["identity"] if f in ALLOWED_FIELDS])
        need = max(0, 3 - count)
        hint = " I’ve saved your claim description for after verification." if state["hints"] else ""
        choices = ", ".join(FIELD_LABELS[f] for f in fields)
        if not choices or count + len(fields) < 3:
            return "I respect that choice. There aren’t enough remaining identity options to complete verification here. You can request human help or end the session."
        prompt = (
            f"To protect your claim details, I need to verify at least three different identity fields. I have received {count}; please provide {need} more from: {choices}."
            if need
            else "Please clarify or correct the identity details so they can be checked together."
        ) + hint
        return (
            prompt.replace("your claim details", "the policyholder's claim details").replace(
                "identity fields", "policyholder identity fields"
            )
            if represented
            else prompt
        )

    def _server_guards(self, state, obs, request, events):
        """Deterministic corrections to the observation that the model must not decide."""
        text = request.text
        # A claim ID is a claim selection, never a policy number.
        obs.identity = [
            i
            for i in obs.identity
            if not (i.field == "policy_number" and re.fullmatch(r"\s*CL-\d+\s*", i.value, re.I))
        ]
        claim_ids = list(dict.fromkeys(x.upper() for x in re.findall(r"\bCL-\d+\b", text, re.I)))
        if len(claim_ids) == 1 and not any(h.field == "case_id" for h in obs.hints):
            match = re.search(r"\bCL-\d+\b", text, re.I)
            obs.hints.append(
                Hint(
                    field="case_id",
                    value=claim_ids[0],
                    quote=match.group(0),
                    occurrence=1,
                    assertion="affirm",
                )
            )
            if obs.action == "case_lookup":
                obs.action = "continue"
        auth = state.get("auth")
        if not auth:
            return None
        # The summary can only go to the registered address; a different address is
        # neither an identity correction nor a new recipient.
        if re.search(
            r"\b(?:send|email|e-mail|forward|mail|deliver|cc)\b[^.?!\n]{0,60}\b(?:to|at)\b[^.?!\n]{0,40}[\w.+-]+@[\w-]+\.[\w.]+"
            r"|\b(?:other|different|another|new|alternate)\s+(?:email|e-mail|address|inbox)\b",
            text,
            re.I,
        ):
            obs.identity = [i for i in obs.identity if i.field not in {"email", "phone"}]
            obs.subject_changed = False
            subject = next(p for p in self.repo.policyholders if p["party_id"] == auth["subject"])
            events.append(event("summary.recipient_change_refused"))
            return (
                f"For privacy, the conversation summary can only go to {recipient_label(state)} on file "
                f"({mask_email(subject['email'])}). I can’t send it to a different address or change contact "
                "details in this chat."
                + (
                    " You can still choose Send this summary or Skip email."
                    if state.get("draft")
                    else " When you finish, you can choose to send the summary there or skip email."
                )
            )
        if auth.get("representative"):
            return None
        # A verified policyholder asking for someone else's records is refused without
        # touching their own verification or switching to representative mode.
        own = next(p for p in self.repo.policyholders if p["party_id"] == auth["subject"])
        own_names = {normalize("name", n) for n in [own["name"], *own.get("name_aliases", [])]}
        others = [p for p in self.repo.policyholders if p["party_id"] != auth["subject"]]
        named_other = any(
            re.search(r"\b" + re.escape(n) + r"\b", text, re.I)
            for p in others
            for n in [p["name"], *p.get("name_aliases", [])]
        )
        foreign = [
            i
            for i in obs.identity
            if i.subject == "other"
            or (
                i.field == "policy_number"
                and normalize("policy_number", i.value) in {p["policy_number"] for p in others}
            )
            or (
                i.field == "name"
                and i.assertion == "affirm"
                and normalize("name", i.value) not in own_names
                and not re.search(r"\b(?:my name is|i am|i'm|i’m|this is)\b", text, re.I)
            )
        ]
        if not (named_other or foreign):
            return None
        obs.identity, obs.role, obs.subject_changed = [], "self", False
        events.append(event("access.other_party_refused"))
        return (
            "I can only discuss claims on the account you verified. I can’t look up or share another person’s "
            "policy, claims or details. If you are calling on someone else’s behalf, choose Logout / Switch caller "
            "and start a new verification as their representative. Your own verification is unchanged."
        )

    def _calls_left(self, state):
        return self.settings.model_max_calls_per_conversation - state.get("model_calls_used", 0)

    def _mismatch_prompt(self, state):
        if state["verification_locked"]:
            return (
                "One or more of the details you provided don’t match our records. "
                + self._verification_prompt(state)
            )
        attempt = state.get("verification_attempts", 0)
        return (
            "One or more of the details you provided don’t match our records, so I can’t verify your identity yet. "
            "For your privacy I can’t say which detail didn’t match. Please check them and send at least three of: "
            "full name, date of birth, registered phone, registered email, or the last four digits of your SSN "
            "(or national ID if you have no SSN). "
            f"Attempt {attempt} of 3; after three unsuccessful attempts verification is locked for 30 minutes."
        )

    def _question_prompt(self, state):
        message = (
            "I couldn’t identify which claim question you want answered. "
            "You can ask about status, the denial reason, documents or alternatives, amounts, "
            "deadlines, submission, or review timing. Your verified identity and claim context are saved."
        )
        if (state["last_model_status"] or {}).get("mode") == "offline":
            message += (
                " This offline demo recognizes limited English and Chinese examples. "
                "Try ‘How much was paid?’ or ‘需要什么材料？’."
            )
        if state["draft"]:
            message += " The summary email remains paused while we clarify your question."
        return message

    def _resubmit_identity(self, state, observation, text, events):
        """A complete current-message bundle replaces earlier unverified inputs."""
        if (
            state["auth"]
            or state["phase"] != "VERIFY_ID"
            or state["verification_locked"]
            or observation.role == "representative"
            or not (state["identity"] or state["identity_conflicts"])
        ):
            return False
        current = {}
        for item in observation.identity:
            eligible = eligible_identity(item, text)
            if item.field not in ALLOWED_FIELDS or not eligible:
                continue
            value, _ = eligible
            if item.field in current and current[item.field] != value:
                return False
            current[item.field] = value
        if len(current) < 3:
            return False
        events.append(
            event(
                "verification.resubmitted",
                fields=sorted(current),
                replaced_fields=sorted(state["identity"]),
                prior_failed_attempts=state["verification_attempts"],
            )
        )
        state["identity"] = {}
        state["identity_conflicts"] = []
        return True

    def _merge(self, state, observation, request, events):
        changed = False
        clarification = False
        if observation.subject_changed and state["auth"]:
            revoke_auth(state, events, "SUBJECT_CHANGED")
        elif observation.subject_changed:
            # Before verification there is no grant to revoke: start a clean identity set
            # without wiping the visible conversation. Failures stay counted per browser.
            state["identity"], state["identity_conflicts"] = {}, []
            events.append(event("verification.identity_reset", rule="SUBJECT_CHANGED"))
        corrections = [
            i
            for i in observation.identity
            if i.assertion in {"replace", "retract"}
            and i.subject == "self"
            and source_span(request.text, i.quote, i.occurrence)
        ]
        if state["auth"] and corrections:
            revoke_auth(state, events, "IDENTITY_CORRECTED", preserve_hints=True)
        case_ids = [
            h.value
            for h in observation.hints
            if h.field == "case_id"
            and h.assertion in {"affirm", "replace"}
            and source_span(request.text, h.quote, h.occurrence)
        ]
        explicit_switch = bool(
            case_ids
            and (
                state["candidates"]
                or (state["selected_case_id"] and case_ids[-1] != state["selected_case_id"])
            )
        )
        if observation.case_relation == "new" or explicit_switch:
            # Choosing from the offered list keeps the question asked while it was ambiguous.
            carried = (
                state.get("pending_topics", [])
                if explicit_switch and case_ids[-1] in {c["case_id"] for c in state["candidates"] or []}
                else []
            )
            clear_case(state)
            state["pending_topics"] = carried
            if explicit_switch and not observation.intents:
                state["intents"] = ["status_inquiry"]
                observation.intents = ["status_inquiry"]
            if state["auth"]:
                transition(state, "RESOLVE_INTENT", events, "CASE_CHANGED")
            events.append(event("memory.case_reset"))
        elif observation.case_relation == "uncertain" and observation.hints:
            clarification = True
        if not state["auth"]:
            changed = self._resubmit_identity(state, observation, request.text, events)
            for item in observation.identity:
                span = source_span(request.text, item.quote, item.occurrence)
                if not span:
                    events.append(
                        event("observation.rejected", reason="SOURCE_NOT_IN_MESSAGE", field=item.field)
                    )
                    clarification = True
                    continue
                identity_subject = "other" if state.get("representative") else "self"
                if item.subject != identity_subject:
                    events.append(event("observation.rejected", reason="WRONG_SUBJECT", field=item.field))
                    continue
                if item.assertion == "refuse":
                    if item.field not in state["refused_fields"]:
                        state["refused_fields"].append(item.field)
                    if item.field in state["identity"]:
                        state["identity"].pop(item.field)
                        state["identity_conflicts"] = [
                            f for f in state["identity_conflicts"] if f != item.field
                        ]
                        changed = True
                    continue
                if item.assertion == "retract":
                    state["identity"].pop(item.field, None)
                    state["identity_conflicts"] = [f for f in state["identity_conflicts"] if f != item.field]
                    changed = True
                    continue
                eligible = eligible_identity(item, request.text, identity_subject)
                if not eligible:
                    if item.assertion in {"affirm", "replace"}:
                        clarification = True
                    events.append(
                        event(
                            "observation.rejected",
                            reason="ASSERTION_NOT_AFFIRMED_OR_AMBIGUOUS",
                            field=item.field,
                        )
                    )
                    continue
                value, span = eligible
                old = state["identity"].get(item.field)
                if old and old["value"] != value and item.assertion != "replace":
                    if item.field not in state["identity_conflicts"]:
                        state["identity_conflicts"].append(item.field)
                    clarification = True
                    continue
                if (
                    not old
                    or old["value"] != value
                    or (item.assertion == "replace" and item.field in state["identity_conflicts"])
                ):
                    changed = True
                state["identity"][item.field] = {
                    "value": value,
                    "source_turn": request.message_id,
                    "span": span,
                }
                if item.assertion == "replace":
                    state["identity_conflicts"] = [f for f in state["identity_conflicts"] if f != item.field]
                state["refused_fields"] = [f for f in state["refused_fields"] if f != item.field]
                events.append(
                    event("memory.identity_recorded", field=item.field, source_turn=request.message_id)
                )
        if observation.case_relation != "uncertain":
            for hint in observation.hints:
                span = source_span(request.text, hint.quote, hint.occurrence)
                if not span:
                    events.append(
                        event("observation.rejected", reason="SOURCE_NOT_IN_MESSAGE", field=hint.field)
                    )
                    continue
                if hint.assertion == "retract" or (
                    hint.assertion == "negated"
                    and state["hints"].get(hint.field, {}).get("value") == hint.value
                ):
                    state["hints"].pop(hint.field, None)
                    continue
                if hint.assertion not in {"affirm", "replace"}:
                    continue
                value = hint.value
                valid = (
                    (hint.field == "month" and value.isdigit() and 1 <= int(value) <= 12)
                    or (hint.field == "year" and value.isdigit() and 1900 <= int(value) <= 2100)
                    or (hint.field == "case_type" and value in {"healthcare", "auto", "dental"})
                    or (hint.field == "status" and value in {"denied", "closed", "open"})
                    or (
                        hint.field == "case_id"
                        and bool(re.fullmatch(r"CL-\d+", value))
                        and value in hint.quote.upper()
                    )
                )
                if not valid:
                    continue
                old = state["hints"].get(hint.field)
                if old and old["value"] != value and hint.assertion != "replace":
                    clarification = True
                    continue
                state["hints"][hint.field] = {"value": value, "source_turn": request.message_id, "span": span}
                events.append(event("memory.hint_recorded", field=hint.field, source_turn=request.message_id))
        business_intents = [intent for intent in observation.intents if intent != "human_help"]
        if business_intents:
            state["intents"] = list(dict.fromkeys(business_intents))
        if state["phase"] in {"VERIFY_ID", "RESOLVE_INTENT"}:
            state["pending_topics"] = list(
                dict.fromkeys(
                    state.get("pending_topics", [])
                    + [
                        topic
                        for topic in observation.topics
                        if observation.scope != "out_of_scope"
                        and not (observation.scope == "mixed" and topic == "unknown")
                    ]
                )
            )
        if observation.email_preference in {"want", "skip"}:
            state["email_preference"] = observation.email_preference
            events.append(event("summary.preference", decision=observation.email_preference))
        return changed, clarification

    async def _post(self, state, owner, turn_id, events):
        transition(state, "POST_PROCESS", events, "CASE_WORK_COMPLETE")
        if state["email_preference"] == "skip":
            state["lifecycle"] = "COMPLETED"
            invalidate_draft(state)
            events.append(event("summary.skipped", early_preference=True))
            return "We’ve finished this consultation. I’ve remembered your choice to skip email; no email was queued. Thank you for chatting with me."
        subject = "Insurance claim consultation summary"
        body, context = summary_context(self, state, owner, events)
        if hasattr(self.interpreter, "answer") and self._calls_left(state) > 0:
            try:
                body, _, _, stats = await self._model_task(
                    state["session_id"], turn_id, self.interpreter.answer(context)
                )
                self.store.assert_current(state["session_id"], owner, turn_id)
                state["model_calls_used"] = state.get("model_calls_used", 0) + stats.get("calls", 0)
                events.append(event("model.composed", purpose="summary", **stats))
            except ModelFailure as exc:
                # The grounded four-section template is used as-is.
                self.store.assert_current(state["session_id"], owner, turn_id)
                state["model_calls_used"] = state.get("model_calls_used", 0) + exc.calls
                events.append(
                    event(
                        "model.failed",
                        purpose="summary",
                        fallback="summary_template",
                        status=exc.code,
                        calls=exc.calls,
                        **exc.usage,
                    )
                )
        statement = representation_statement(state)
        if statement:
            body = body.replace(
                "Discussion: The representative asked",
                f"Discussion: {statement} {caller_context(state)['caller']['name']} asked",
                1,
            )
        state["closing_pending"] = False
        recipient = self.gateway.recipient(state)
        digest = hashlib.sha256(
            json.dumps({"body": body, "recipient": recipient, "subject": subject}, sort_keys=True).encode()
        ).hexdigest()
        state["draft"] = {
            "id": uid("offer"),
            "subject": subject,
            "body": body,
            "base_body": body,
            "note": "",
            "recipient": recipient,
            "hash": digest,
            "epoch": state["auth_epoch"],
            "episode_version": state["episode_version"],
            "episode_id": state["episode_id"],
            "expires_at": time.time() + self.settings.offer_seconds,
            "consent": False,
            "paused": False,
        }
        events.append(event("summary.offered", offer_id=state["draft"]["id"]))
        return f"I’ve prepared a summary of what we discussed, the recorded outcome, and next steps. Review it below. Would you like me to record an email to {recipient_label(state)} ({mask_email(recipient)}) in the demo outbox, or skip email? No real email is sent in this demo."

    def _accept_email(self, state, offer_id, events):
        draft = state["draft"]
        if (
            not auth_valid(state, self.settings)
            or state["phase"] != "POST_PROCESS"
            or not draft
            or offer_id != draft["id"]
        ):
            raise Conflict("OFFER_NOT_CURRENT")
        if (
            draft["expires_at"] <= time.time()
            or draft["epoch"] != state["auth_epoch"]
            or draft["episode_version"] != state["episode_version"]
        ):
            raise Conflict("OFFER_EXPIRED")
        if state["email_operation"]:
            draft["paused"] = False
            return (
                "This summary already has an email operation. I’ll keep the existing record; it won’t be queued twice.",
                None,
            )
        if self.gateway.recipient(state) != draft["recipient"]:
            raise Conflict("RECIPIENT_CHANGED")
        draft["consent"], draft["paused"] = True, False
        operation = {
            "id": uid("mail"),
            "offer_id": draft["id"],
            "hash": draft["hash"],
            "recipient": draft["recipient"],
            "body": draft["body"] + "\n\n" + SUMMARY_FOOTER,
            "subject": draft["subject"],
            "subject_party_id": state["auth"]["subject"],
            "caller": caller_context(state)["caller"],
            "case_id": state["selected_case_id"],
            "epoch": draft["epoch"],
            "episode_id": state["episode_id"],
        }
        state["email_operation"] = {"id": operation["id"], "status": "QUEUED"}
        events.append(event("summary.consent", offer_id=draft["id"], purpose="conversation_summary"))
        events.append(event("operation.queued", operation_id=operation["id"]))
        return (
            "Your summary is queued for the simulated email outbox. You can cancel while it is pending. No real email will be sent.",
            operation,
        )

    async def turn(self, sid, owner, request):
        if request.action == "message" and consent_text(request.text).rstrip("。！") in PAUSE_TEXT:
            request = request.model_copy(update={"action": "interrupt"})
        if request.action in {"reset", "cancel", "email_cancel", "interrupt"}:
            state = self.store.control(sid, owner, request)
            for (active_sid, active_turn), task in list(self.observations.items()):
                if active_sid == sid:
                    try:
                        self.store.assert_current(sid, owner, active_turn)
                    except Conflict:
                        task.cancel()
            return state
        state, turn_id, replay = self.store.begin(sid, owner, request)
        if replay:
            return state
        if request.action == "form":
            request = request.model_copy(
                update={
                    "text": "\n".join(
                        f"{key}: {value.strip()}"
                        for key, value in request.form.model_dump().items()
                        if value.strip()
                    )
                }
            )
        events, operation = [], None
        was_verify = not bool(state["auth"])
        prefix = []
        rendered_topics = None
        skip_compose = False
        if not state["auth"]:
            failures = self.store.verify_failures(owner)
            if failures >= self.store.VERIFY_LOCK_AFTER:
                state["verification_locked"] = True
                state["verification_attempts"] = failures
        try:
            if state["lifecycle"] != "ACTIVE":
                if state["lifecycle"] == "COMPLETED" and request.action in {
                    "human",
                    "resume",
                    "message",
                    "form",
                    "resume",
                    "my_claims",
                    "handoff_status",
                    "inquiry_status",
                    "finish",
                    "email_prepare",
                }:
                    state["lifecycle"] = "ACTIVE"
                    invalidate_draft(state)
                    if state["phase"] == "POST_PROCESS":
                        transition(state, "PROCESS_CASE", events, "CONSULTATION_RESUMED")
                    events.append(event("session.resumed"))
                elif request.action == "human" and state["lifecycle"] == "CANCELLED":
                    state["lifecycle"] = "ACTIVE"
                else:
                    reply = "This consultation has ended. Start a new session to continue."
                    return self._finish(
                        state, turn_id, request, empty_observation(), events, reply, was_verify
                    )
            if request.action == "wait_human":
                tickets = [t for t in self.store.support_tickets(state, owner) if t["can_update"]]
                if not tickets:
                    raise Conflict("NO_OPEN_HUMAN_REQUEST")
                state["support_mode"] = "wait"
                chosen = next(
                    (t for t in tickets if t["id"] == (state.get("handoff") or {}).get("id")), tickets[0]
                )
                with self.store.tx() as db:
                    row = db.execute(
                        "SELECT payload FROM handoffs WHERE id=? AND owner=?", (chosen["id"], owner)
                    ).fetchone()
                    state["handoff"] = self.store.unseal(row[0])
                return self._finish(
                    state,
                    turn_id,
                    request,
                    empty_observation(),
                    events,
                    f"Wait-only mode for {state['handoff']['id']}. This is the AI assistant interface; messages receive system confirmations only. Choose Continue with AI whenever you want answers.",
                    was_verify,
                )
            if state.get("support_mode") == "wait" and request.action == "message":
                if any(t["can_update"] for t in self.store.support_tickets(state, owner)):
                    hid = self.store.append_request_note(
                        state, owner, redact(request.text, people=self.repo.policyholders)
                    )
                    return self._finish(
                        state,
                        turn_id,
                        request,
                        empty_observation(),
                        events,
                        f"System confirmation: added to {hid}.",
                        was_verify,
                    )
                state["support_mode"] = "ai"
                prefix.append("That human request is closed. You are back with the AI assistant.")
            if request.action == "resume":
                state["support_mode"] = "ai"
                state["intents"] = [intent for intent in state["intents"] if intent != "human_help"]
                if state["phase"] == "VERIFY_ID" and state["identity"]:
                    # Older sessions may have collected PII while misrouting the turn
                    # as human help. Check that existing, source-validated bundle now.
                    _, events = self._verify_identity(state, owner, turn_id, True, False, events)
                events.append(event("conversation.ai_resumed"))
                reply = (
                    "You are back with the AI assistant. " + self._verification_prompt(state)
                    if not auth_valid(state, self.settings)
                    else "You are back with the AI assistant. Your identity is still verified. What would you like to know about your claim?"
                )
                pending = [t for t in self.store.support_tickets(state, owner) if t["can_update"]]
                if pending:
                    reply += f" Your request {pending[0]['id']} is still awaiting handling. View updates or cancel in Human requests."
                return self._finish(state, turn_id, request, empty_observation(), events, reply, was_verify)
            if state["draft"]:
                state["draft"]["paused"] = True
                self.store.commit(state, turn_id, [event("summary.paused", reason="NEW_INPUT")])
            # Consent is interpreted by a whole-message grammar, not a model substring.
            email_text = consent_text(request.text)
            offer = state.get("handoff_note_offer")
            accept_note = request.action == "handoff_note_accept" or (
                offer
                and email_text
                in {"yes", "yes please", "yes, please", "add it", "please add it", "好", "添加"}
            )
            decline_note = request.action == "handoff_note_decline" or (
                offer and email_text in {"no", "no thanks", "no, thanks", "don't add it", "不用", "不要"}
            )
            if accept_note or decline_note:
                state.pop("handoff_note_offer", None)
                valid = (
                    offer
                    and auth_valid(state, self.settings)
                    and offer["epoch"] == state["auth_epoch"]
                    and offer["case_id"] == state["selected_case_id"]
                )
                ticket = next(
                    (
                        t
                        for t in self.store.support_tickets(state, owner)
                        if valid
                        and t["id"] == offer["id"]
                        and t["can_update"]
                        and t["revision"] == offer["revision"]
                    ),
                    None,
                )
                if not ticket:
                    reply = "That add-question offer is no longer current. Nothing was added; please review the request again."
                elif decline_note:
                    reply = f"Nothing was added to {offer['id']}. We can continue with your claim questions."
                else:
                    self.store.append_request_note(
                        state, owner, offer["text"], unresolved=True, ticket_id=offer["id"]
                    )
                    reply = f"Your question was added to {offer['id']} for representative assessment. No deadline or claim decision has changed."
                    events.append(event("handoff.note_confirmed", handoff_id=offer["id"]))
                return self._finish(state, turn_id, request, empty_observation(), events, reply, was_verify)
            # Any other turn supersedes this proposal; a later 'yes' cannot submit it.
            state.pop("handoff_note_offer", None)
            if request.action == "email_edit":
                draft = state.get("draft")
                if not auth_valid(state, self.settings) or not draft or request.offer_id != draft["id"]:
                    raise Conflict("OFFER_NOT_CURRENT")
                note = request.email_note.strip()
                draft["base_body"] = draft.get("base_body", draft["body"])
                draft["note"] = note
                draft["body"] = draft["base_body"] + (
                    "\n\nCustomer-written note (not verified claim facts):\n" + note if note else ""
                )
                draft.update(
                    id=uid("offer"),
                    hash=draft_hash(draft),
                    consent=False,
                    paused=False,
                    expires_at=time.time() + self.settings.offer_seconds,
                )
                state["email_operation"] = None
                events.append(event("summary.edited", offer_id=draft["id"]))
                return self._finish(
                    state,
                    turn_id,
                    request,
                    empty_observation(),
                    events,
                    "Your note is saved in a new draft. Review the full summary and choose Send this summary or Skip email. Nothing has been sent.",
                    was_verify,
                )
            if state["phase"] == "POST_PROCESS" and (
                request.action == "email_send" or email_text in ACCEPT_EMAIL
            ):
                reply, operation = self._accept_email(
                    state,
                    request.offer_id
                    if request.action == "email_send"
                    else state["draft"]["id"]
                    if state["draft"]
                    else None,
                    events,
                )
                return self._finish(
                    state, turn_id, request, empty_observation(), events, reply, was_verify, operation
                )
            if state["phase"] == "POST_PROCESS" and (
                request.action == "email_skip" or email_text in SKIP_EMAIL
            ):
                state["lifecycle"] = "COMPLETED"
                invalidate_draft(state)
                events.append(event("summary.skipped", early_preference=False))
                return self._finish(
                    state,
                    turn_id,
                    request,
                    empty_observation(),
                    events,
                    "Email skipped. No pending email will be sent. Thank you for chatting with me.",
                    was_verify,
                )
            explicit_end = bool(
                re.fullmatch(
                    r"(?:(?:no|nope)[,\s]*)?(?:that['’]s all|nothing else|all done|没别的了|没有了|不用了)(?:[,，\s]*(?:thanks|thank you|谢谢))?[.!。！\s]*",
                    request.text.strip(),
                    re.I,
                )
            )
            closing_no = explicit_end or (
                state.get("closing_pending")
                and consent_text(request.text).rstrip("。！")
                in {
                    "no",
                    "no thanks",
                    "nothing else",
                    "no, nothing else",
                    "no nothing else",
                    "that's all",
                    "没别的了",
                    "没有了",
                    "不用了",
                }
            )
            if closing_no or (
                request.action == "message" and consent_text(request.text).rstrip("。！") in END_TEXT
            ):
                obs = empty_observation(action="finish")
            elif request.action == "confirm_claim" or (
                state.get("claim_confirmation") and confirms_claim(request.text)
            ):
                if not state.get("claim_confirmation") or not auth_valid(state, self.settings):
                    raise Conflict("CLAIM_CONFIRMATION_NOT_CURRENT")
                obs = empty_observation(intents=["status_inquiry"], topics=["status"])
            elif request.action == "form":
                obs = empty_observation(role="self")
                for field, raw in request.form.model_dump().items():
                    value = raw.strip()
                    if not value:
                        continue
                    if field == "case_id":
                        obs.hints.append(
                            Hint(field="case_id", value=value, quote=value, occurrence=1, assertion="replace")
                        )
                        obs.intents = ["status_inquiry"]
                        obs.topics = ["status"]
                    else:
                        obs.identity.append(
                            IdentityObservation(
                                field=field,
                                value=normalize(field, value) or value,
                                quote=value,
                                occurrence=1,
                                subject="other" if state.get("representative") else "self",
                                assertion="replace",
                            )
                        )
                obs.subject_changed = bool(
                    state["auth"] and any(i.field in ALLOWED_FIELDS for i in obs.identity)
                )
                events.append(event("form.received", fields=list(request.form.model_fields_set)))
            elif request.action in {"consent_check", "consent_timeout"}:
                obs = empty_observation(action="consent_status")
            elif request.action in {"human", "my_claims", "handoff_status", "inquiry_status", "case_lookup"}:
                obs = empty_observation(action=request.action)
            elif request.action in {"finish", "email_prepare"}:
                if request.action == "email_prepare":
                    state["email_preference"] = "want"
                obs = empty_observation(action="finish")
            elif request.action in {"email_send", "email_skip"}:
                raise Conflict("NO_EMAIL_OFFER")
            else:
                try:
                    if self._calls_left(state) <= 0:
                        raise ModelFailure("MODEL_CONVERSATION_LIMIT")
                    obs, stats = await self._observe(sid, turn_id, request.text, self._safe_context(state))
                    self.store.assert_current(sid, owner, turn_id)
                    state["model_calls_used"] = state.get("model_calls_used", 0) + stats.get("calls", 0)
                    state["last_model_status"] = stats
                    events.append(event("model.observed", action=obs.action, topics=obs.topics, **stats))
                except ModelFailure as exc:
                    # Keep the conversation going with the scripted parser instead of an
                    # error. Identity, ownership and workflow gates are unchanged.
                    self.store.assert_current(sid, owner, turn_id)
                    obs, _ = await OfflineInterpreter().observe(request.text, self._safe_context(state))
                    state["model_calls_used"] = state.get("model_calls_used", 0) + exc.calls
                    state["last_model_status"] = {
                        "mode": "openai",
                        "status": exc.code,
                        "calls": exc.calls,
                        **exc.usage,
                        "fallback": "offline_parser",
                    }
                    events.append(event("model.failed", **state["last_model_status"]))
                    state["_service_note"] = fallback_note(exc.code)
                    skip_compose = True
            if (
                obs.action == "human"
                and obs.requires_human_decision
                and not re.search(r"human|representative|agent|person|someone|人工|客服", request.text, re.I)
            ):
                # Asking the AI to approve something is not consent to another handoff.
                obs.action = "continue"
                obs.intents = [i for i in obs.intents if i != "human_help"]
                obs.topics = obs.topics or ["unknown"]
            # RL without any claim context is an explicit scope boundary, even
            # when the observer incorrectly labels the abbreviation ambiguous.
            if request.action == "message" and re.fullmatch(
                r"\s*(?:(?:what (?:is|does)|explain|define)\s+rl(?:\s+mean)?|rl\s*是什么|什么是\s*rl)[?？.!。\s]*",
                request.text,
                re.I,
            ):
                obs.scope, obs.action = "out_of_scope", "continue"
                obs.intents, obs.topics = [], []
            # A "mixed" label without any usable insurance content is an off-topic
            # retry. Count it so repeated attempts reach the human-help offer and the
            # writer is never asked to handle the unrelated request.
            if (
                request.action == "message"
                and obs.scope == "mixed"
                and obs.action == "continue"
                and not obs.identity
                and not obs.hints
                and not obs.intents
                and set(obs.topics) <= {"unknown"}
                and not obs.requires_human_decision
                and obs.email_preference == "unmentioned"
            ):
                obs.scope, obs.topics = "out_of_scope", []
            # RL is a declared scope boundary. If the observer still labels a longer RL
            # request as a possible claim abbreviation, treat it as off-topic unless the
            # caller links it to their insurance record. Other new terms keep the
            # claim-context question.
            if (
                request.action == "message"
                and obs.scope == "claim_term"
                and re.search(r"\bRL\b|reinforcement learning", request.text, re.I)
                and not re.search(
                    r"claim|policy|insurance|bill|statement|letter|EOB|理赔|保单|保险", request.text, re.I
                )
            ):
                obs.scope, obs.topics = "out_of_scope", []
            if obs.scope == "claim_term":
                state["ambiguous_term"] = True
                return self._finish(
                    state,
                    turn_id,
                    request,
                    obs,
                    events,
                    "Is that an abbreviation you saw on your insurance claim? I can help explain claim terminology in its context.",
                    was_verify,
                )
            if state.pop("ambiguous_term", False) and consent_text(request.text) in {
                "no",
                "nope",
                "not on my claim",
                "不是",
                "不是理赔里的",
            }:
                obs.scope = "out_of_scope"
                obs.action = "continue"
            if (
                obs.action == "finish"
                and state["phase"] == "PROCESS_CASE"
                and request.action == "message"
                and not closing_no
                and not re.search(r"summary|email|总结|邮件", request.text, re.I)
            ):
                state["closing_pending"] = True
                return self._finish(
                    state,
                    turn_id,
                    request,
                    obs,
                    events,
                    "You're welcome. Is there anything else I can help with before we review your optional email summary?",
                    was_verify,
                )
            if obs.action != "finish":
                state["closing_pending"] = False
            if state["auth"] and not auth_valid(state, self.settings):
                revoke_auth(state, events, "AUTH_EXPIRED", preserve_hints=True)
            # Emotion needs evidence from THIS message. History cannot supply it.
            if obs.emotion != "neutral" and (not obs.emotion_quote or obs.emotion_quote not in request.text):
                obs.emotion = "neutral"
            empathy = self._empathy(state, obs.emotion)
            if empathy:
                prefix.append(empathy)
            if not state["auth"] and obs.emotion in {"refusal", "frustration", "anger"}:
                state["persuasion_count"] = state.get("persuasion_count", 0) + 1
                if state["persuasion_count"] == 2:
                    # The second refusal names the concrete human option (fixed server text).
                    state["_human_option"] = True
                if state["persuasion_count"] >= 3:
                    first_lock = not state["verification_locked"]
                    state["verification_locked"] = True
                    prefix.append(
                        "I won't keep pressing you. I can submit a human handling request; claim details remain protected until verification."
                    )
                    if first_lock and obs.action != "human" and request.action == "message":
                        # Stop persuading now: no field lists or SSN alternatives after this.
                        return self._finish(
                            state, turn_id, request, obs, events, "\n\n".join(prefix), was_verify
                        )
            state["last_emotion"] = obs.emotion
            if request.action == "message":
                guard_reply = self._server_guards(state, obs, request, events)
                if guard_reply:
                    return self._finish(state, turn_id, request, obs, events, guard_reply, was_verify)
            self._identify_representative(state, obs, request, events)
            previous_case = (
                copy.deepcopy(state)
                if auth_valid(state, self.settings) and state["selected_case_id"]
                else None
            )
            if obs.action == "consent_status":
                if request.action == "message" and not re.search(
                    r"check|status|has .{0,25}(?:approved|consented)|同意了[吗么]|查询|查看|状态",
                    request.text,
                    re.I,
                ):
                    pending_reply = (
                        "\n\n".join(
                            (
                                []
                                if any(p in _EMPATHY_PHRASES for p in prefix)
                                else ["I understand this is frustrating."]
                            )
                            + prefix
                            + [CONSENT_EXPLANATION]
                        )
                        if CONSENT_PUSHBACK.search(request.text)
                        and (state.get("representative") or {}).get("status") == "pending"
                        else "I still need to check the policyholder’s approval. Choose Check policyholder consent; claim details remain private until it is confirmed."
                    )
                    return self._finish(state, turn_id, request, obs, events, pending_reply, was_verify)
                reply, events = self._check_representative_consent(
                    state, owner, turn_id, events, request.action == "consent_timeout"
                )
                if reply is not None:
                    return self._finish(state, turn_id, request, obs, events, reply, was_verify)
                prefix.append(self._verification_receipt(state))
                obs.action = "continue"
                state["intents"] = state["intents"] or ["status_inquiry"]
            confirmation = state.get("claim_confirmation")
            if confirmation and auth_valid(state, self.settings):
                selected_confirmation = any(
                    h.field == "case_id"
                    and h.value == confirmation
                    and h.assertion in {"affirm", "replace"}
                    and source_span(request.text, h.quote, h.occurrence)
                    for h in obs.hints
                )
                if request.action == "confirm_claim" or confirms_claim(request.text) or selected_confirmation:
                    obs.scope = "in_scope"
                    obs.action, obs.case_relation = "continue", "same"
                    obs.hints = []
                    # This is a server-owned confirmation, not a user-derived hint span.
                    state["hints"]["case_id"] = {"value": confirmation, "source_turn": turn_id}
                    obs.intents = obs.intents or ["status_inquiry"]
                    obs.topics = obs.topics or ["status"]
                    state.pop("claim_confirmation", None)
                elif (
                    request.action == "form"
                    or any(h.field == "case_id" for h in obs.hints)
                    or obs.action in {"case_lookup", "human", "my_claims"}
                ):
                    # Only a valid different selection may discard deferred questions.
                    # _merge clears that case; a failed lookup keeps the offered claim.
                    if not any(h.field == "case_id" for h in obs.hints):
                        state.pop("claim_confirmation", None)
                elif obs.scope != "out_of_scope":
                    if re.fullmatch(r"no[.!\s]*|不是|不对", request.text.strip(), re.I):
                        clear_case(state)
                        reply = "Please choose the policyholder’s claim below, or give its claim ID or policy number."
                    else:
                        reply = f"Before we discuss details, please confirm {confirmation}, or choose another claim below."
                    return self._finish(state, turn_id, request, obs, events, reply, was_verify)
            lookup_reply = reference_lookup(state, obs, request.text, self.gateway, events)
            if lookup_reply is not None:
                obs.action = "case_lookup"
                touched, clarify = False, False
            else:
                # A policy is an account locator, never a change to verified identity.
                if state["auth"] and obs.identity and all(i.field == "policy_number" for i in obs.identity):
                    obs.subject_changed = False
                    obs.identity = []
                touched, clarify = self._merge(state, obs, request, events)
            if (
                obs.action in {"human", "handoff_status", "inquiry_status", "case_lookup"}
                and state["phase"] == "VERIFY_ID"
            ):
                result, events = self._verify_identity(state, owner, turn_id, touched, clarify, events)
                if result == "PASS":
                    prefix.append(self._verification_receipt(state))
            if (
                state.get("representative")
                and not state["auth"]
                and obs.action != "human"
                and obs.scope != "out_of_scope"
            ):
                if state["phase"] == "VERIFY_ID":
                    _, events = self._verify_identity(state, owner, turn_id, touched, clarify, events)
                prompt = self._representative_prompt(state)
                if (
                    state["representative"].get("status") == "pending"
                    and request.action == "message"
                    and (
                        obs.emotion in {"frustration", "anger", "confusion"}
                        or CONSENT_PUSHBACK.search(request.text)
                    )
                ):
                    # Pushback while consent is pending: acknowledge, explain why, say how to proceed.
                    prompt = CONSENT_EXPLANATION
                    if not any(p in _EMPATHY_PHRASES for p in prefix):
                        prefix.insert(0, "I understand this is frustrating.")
                reply = "\n\n".join(prefix + [prompt])
                return self._finish(state, turn_id, request, obs, events, reply, was_verify)
            if obs.action == "my_claims" and not state["auth"]:
                # Identity + account-list requests in one utterance still verify now.
                obs.action = "continue"
                state["intents"] = ["status_inquiry"]
            if obs.action == "case_lookup":
                reply = (
                    lookup_reply or "Enter a policy number or claim ID, or use My claims to choose a claim."
                )
            elif obs.action == "human":
                reply = self._human(
                    state,
                    owner,
                    events,
                    "USER_REQUEST",
                    request_text=redact(request.text, obs, self.repo.policyholders),
                )
            elif obs.action == "handoff_status":
                tickets = self.store.support_tickets(state, owner)
                reply = (
                    "Your human-help requests:\n"
                    + "\n\n".join(
                        t["id"]
                        + " — "
                        + t["label"]
                        + (
                            ". Verify your identity to read the details."
                            if t["details_locked"]
                            else "\n" + "\n".join(item["text"] for item in t["timeline"][-2:])
                        )
                        for t in tickets[:5]
                    )
                    + "\nThese are asynchronous simulated requests. Open Human requests to read updates, add a follow-up or cancel. No real representative is connected and no response time is promised. You can continue with AI."
                    if tickets
                    else "You have no human-help requests visible here. Choose Request human help to submit an asynchronous handling request with a tracking reference."
                )
            elif obs.action == "inquiry_status":
                if not auth_valid(state, self.settings):
                    reply = self._verification_prompt(state)
                else:
                    inquiries = self.store.inquiries(state, owner)
                    references = re.findall(r"\bE-[A-Z0-9]+\b", request.text.upper())
                    if references:
                        inquiries = [item for item in inquiries if item["id"] in references]
                    reply = "Your simulated insurer email enquiries:\n" + (
                        "\n\n".join(
                            f"{item['id']} · {item.get('case_id') or 'General account enquiry'} · {item['subject']} · {item['status']}\n"
                            + "\n".join(
                                f"{entry['actor']}: {entry['text']}" for entry in item["timeline"][-2:]
                            )
                            for item in inquiries[:5]
                        )
                        or "No matching enquiries were found for your verified account."
                    )
                    reply += "\nUse Email enquiry to compose an enquiry, look up its tracking reference, or add a follow-up. All sending and official replies are simulated; no real insurer mailbox is configured."
            elif obs.action == "my_claims":
                reply = (
                    "Your account’s existing demo claims:\n"
                    + (
                        "\n".join(
                            f"{c['case_id']}: {c['case_type']}, created {c['created_at']}, status {c['status']}"
                            for c in self.gateway.account(state)["claims"]
                        )
                        or "No claims were found in the available records."
                    )
                    + "\nChoose a claim in My claims, or tell me which one you want to discuss."
                    if auth_valid(state, self.settings)
                    else self._verification_prompt(state)
                )
            elif obs.action == "cancel":
                revoke_auth(state, events, "USER_CANCELLED")
                state["lifecycle"] = "CANCELLED"
                reply = "Your session is ended. No pending email will be sent."
            elif obs.action == "refuse_all" and not state["auth"]:
                state["verification_locked"] = True
                state["identity"] = {}
                reply = "I respect your decision and won’t ask you to keep verifying. I can’t disclose claim details without verification. You can request human help or end this session."
            else:
                if obs.scope in {"out_of_scope", "mixed"}:
                    prefix.append(
                        "I can help with insurance claims and this support process; I can’t answer unrelated questions."
                    )
                if obs.scope == "out_of_scope":
                    state["scope_retries"] += 1
                    events.append(event("scope.rejected", consecutive=state["scope_retries"]))
                    if state["scope_retries"] == 1:
                        reply = (
                            "We can continue with your insurance claim. You can ask about its status or recorded appeal deadline."
                            if state["auth"]
                            else "Once you’re verified, I can help with claim status, documents, amounts and deadlines."
                        )
                    else:
                        prefix = [p for p in prefix if "unrelated questions" not in p]
                        reply = (
                            (
                                "Let's stay with insurance claims. "
                                if state["scope_retries"] == 2
                                else "I can only help with insurance support here. "
                            )
                            + "Try ‘What is my claim status?’ or ‘What does the deadline mean?’ "
                            + self._support_option(state, owner)
                        )
                else:
                    state["scope_retries"] = 0
                    reply = None
                    if "verification" in obs.topics:
                        prefix.append(
                            "Identity verification protects your private claim information. Please choose at least three of full name, date of birth, registered phone, registered email, or SSN last four. You do not have to provide an SSN; another allowed field is fine."
                        )
                        obs.topics = [topic for topic in obs.topics if topic != "verification"]
                        state["pending_topics"] = [
                            topic for topic in state.get("pending_topics", []) if topic != "verification"
                        ]
                        if state["auth"] and not obs.topics and not obs.hints and obs.action == "continue":
                            reply = "Your identity is already verified for this session. We can continue from your current claim step. Any pending summary email remains paused until you confirm it."
                    if state["phase"] == "VERIFY_ID":
                        result, events = self._verify_identity(
                            state, owner, turn_id, touched, clarify, events
                        )
                        if result == "PASS":
                            prefix.append(self._verification_receipt(state))
                        if state["phase"] == "VERIFY_ID":
                            if result == "FAIL" or state["verification_locked"]:
                                # Server wording only: never reworded as "incomplete" and never
                                # hinting which detail failed.
                                skip_compose = True
                            reply = (
                                self._mismatch_prompt(state)
                                if result == "FAIL"
                                else self._verification_prompt(state, bool(state["identity_conflicts"]))
                            )
                    if not reply and clarify:
                        reply = "I want to keep the details straight. Is that a correction to the same claim or information for a different claim? Please state the corrected detail explicitly."
                    if not reply and state["phase"] == "RESOLVE_INTENT":
                        if not state["intents"]:
                            reply = "What would you like to check: the claim status, a denial, required documents, amounts, or next steps? I’ve kept any claim description you already shared."
                        else:
                            candidates = self.gateway.find_cases(state, events)
                            # A question about a denial narrows the matches to denied claims.
                            denied = [c for c in candidates if c["status"] == "denied"]
                            if (
                                len(candidates) > 1
                                and "denial_question" in state["intents"]
                                and len(denied) == 1
                            ):
                                candidates = denied
                            state["candidates"] = [
                                {k: c[k] for k in ("case_id", "case_type", "created_at", "status")}
                                for c in candidates
                            ]
                            if len(candidates) == 1:
                                state["selected_case_id"] = candidates[0]["case_id"]
                                c = candidates[0]
                                prefix.append(
                                    f"Selected {c['case_id']}, the {c['case_type']} claim created on {c['created_at']}."
                                )
                                state["resolution_kind"] = "MATCH"
                                state["candidates"] = []
                                transition(state, "PROCESS_CASE", events, "CASE_RESOLVED")
                                state["profile_memory"], state["case_memory"] = self.store.memories(
                                    state, owner, state["selected_case_id"]
                                )
                                if state["case_memory"]:
                                    state["discussed"] = state["case_memory"].get("discussed", [])
                                    state["open_questions"] = state["case_memory"].get("open_questions", [])
                                    state["last_answer_topics"] = state["case_memory"].get(
                                        "last_answer_topics", []
                                    )
                                    events.append(
                                        event(
                                            "memory.case_loaded",
                                            case_id=state["selected_case_id"],
                                            turns=len(state["case_memory"].get("dialogue", [])),
                                        )
                                    )
                            elif len(candidates) > 1:
                                skip_compose = True
                                state["pending_topics"] = list(
                                    dict.fromkeys(
                                        state.get("pending_topics", [])
                                        + [
                                            t
                                            for t in obs.topics
                                            if t not in {"unknown", "verification", "status"}
                                        ]
                                    )
                                )
                                rows = [
                                    f"{c['case_id']}: {c['case_type']}, created {c['created_at']}, {c['status']}"
                                    for c in candidates
                                ]
                                reply = (
                                    "I found more than one matching claim on your account. Which one do you mean?\n\n"
                                    + "\n".join(rows)
                                )
                            elif state["no_match_asked"] and obs.action == "confirm_no_match":
                                state["resolution_kind"] = "NO_MATCH"
                                transition(state, "PROCESS_CASE", events, "NO_MATCH_CONFIRMED")
                            else:
                                reply = restore_after_no_match(state, previous_case, events)
                                if reply:
                                    obs.action = "case_lookup"
                                else:
                                    state["no_match_asked"] = True
                                    reply = "I couldn’t find a matching claim in your verified account’s demo records. Please check the claim reference or description. If you have no other details, say ‘no other details’ and I can explain the next step."
                    if not reply and state["phase"] == "POST_PROCESS":
                        if obs.topics and obs.action != "finish":
                            invalidate_draft(state)
                            state["episode_version"] += 1
                            transition(state, "PROCESS_CASE", events, "CASE_REOPENED")
                            if obs.email_preference == "skip":
                                prefix.append(
                                    "I’ve cancelled the pending summary email. Let’s address your question first."
                                )
                        elif obs.email_preference == "skip":
                            state["lifecycle"] = "COMPLETED"
                            invalidate_draft(state)
                            events.append(event("summary.skipped", early_preference=False))
                            reply = "I’ve kept your choice to skip email. No pending email will be sent."
                        elif obs.action == "finish" and (
                            not state["draft"] or state["draft"]["expires_at"] <= time.time()
                        ):
                            reply = await self._post(state, owner, turn_id, events)
                        elif obs.scope == "uncertain":
                            reply = self._question_prompt(state)
                        else:
                            reply = "Email is optional. Use ‘Send this summary’ to confirm the displayed content and registered address, or choose ‘Skip email’. If you have another claim question, I can help with that first."
                    if not reply and state["phase"] == "PROCESS_CASE":
                        if obs.action == "finish":
                            state["user_confirmed_resolved"] |= obs.resolved_confirmation
                            reply = await self._post(state, owner, turn_id, events)
                        elif state["resolution_kind"] == "NO_MATCH":
                            state["discussed"] = ["No matching claim in available records"]
                            reply = "No matching claim was found in the available records. A representative can check the claim reference or whether it is recorded elsewhere. I haven’t changed or submitted anything. You can request human help or finish for a summary."
                        else:
                            # Keep deferred questions until an answer succeeds. The model must
                            # receive them even when this turn only confirms the selected claim.
                            topics = list(dict.fromkeys(obs.topics + state.get("pending_topics", []))) or [
                                {
                                    "status_inquiry": "status",
                                    "denial_question": "denial",
                                    "document_guidance": "documents",
                                    "amount_question": "amounts",
                                    "next_steps": "submission",
                                }.get(i, "status")
                                for i in (obs.intents or (state["intents"] if not state["discussed"] else []))
                            ]
                            if not topics:
                                reply = self._question_prompt(state)
                                events.append(event("question.clarification_requested"))
                            else:
                                if hasattr(self.interpreter, "answer"):
                                    state["_topics_before_response"] = list(state["discussed"])
                                rendered_topics = list(topics)
                                reply, _ = self.renderer.answer(
                                    state,
                                    topics,
                                    events,
                                    pending=self._pending_support(state, owner),
                                    notification=bool(
                                        re.search(r"notif|nobody tell|no one told|通知", request.text, re.I)
                                    ),
                                )
                                if not hasattr(self.interpreter, "answer"):
                                    state["pending_topics"] = [
                                        t for t in state.get("pending_topics", []) if t not in topics
                                    ]
                            if topics and obs.alternatives_exhausted:
                                reply += "\n\nSince reasonable replacements and substitutes aren’t available, a human claims representative needs to review manual options. Would you like human help?"
                                state["open_questions"] = ["Manual review of unavailable documents needed"]
                            elif topics:
                                reply += "\n\nWhat else would you like to clarify? You can also finish and review a summary."
                    if not reply:
                        reply = "Could you tell me a little more about what you need help with on your insurance claim?"
            if (
                auth_valid(state, self.settings)
                and state["selected_case_id"]
                and state["lifecycle"] == "ACTIVE"
                and obs.scope != "uncertain"
            ):
                claim = self.gateway.get_claim(state, [])
                pending = next(
                    (
                        t
                        for t in self.store.support_tickets(state, owner)
                        if t["can_update"] and t.get("case_id") in {None, claim["case_id"]}
                    ),
                    None,
                )
                state["next_steps"] = next_steps(
                    claim,
                    self.settings.business_date,
                    pending,
                    representative=bool((state.get("auth") or {}).get("representative")),
                )
                decision = obs.requires_human_decision or bool(
                    re.search(
                        r"exception|extend|late.{0,30}(?:appeal|accept)|accept.{0,30}late|破例|例外|延期|过期.{0,15}受理",
                        request.text,
                        re.I,
                    )
                )
                if decision:
                    note = redact(request.text, obs, self.repo.policyholders)
                    state["open_questions"] = list(dict.fromkeys(state["open_questions"] + [note]))
                    if pending:
                        state["handoff_note_offer"] = {
                            "id": pending["id"],
                            "revision": pending["revision"],
                            "text": note,
                            "case_id": state["selected_case_id"],
                            "epoch": state["auth_epoch"],
                        }
                        prefix.append(
                            f"Only a representative can decide that. Would you like me to add this question to {pending['id']}? Nothing has been added yet; no extension or exception has been granted."
                        )
                    else:
                        prefix.append(
                            "Only a representative can decide that. You can submit a human handling request; I cannot change a claim decision, payment or deadline."
                        )
            ssn_refusal = any(i.field == "ssn_last4" and i.assertion == "refuse" for i in obs.identity)
            if ssn_refusal:
                reply = self._ssn_alternatives(state, owner)
                prefix = [empathy] if empathy else []
            reply = "\n\n".join(prefix + [reply])
            if (
                hasattr(self.interpreter, "answer")
                and request.action in {"message", "form", "confirm_claim"}
                and obs.scope != "out_of_scope"
                and not ssn_refusal
                and not skip_compose
                and obs.action
                in {
                    "continue",
                    "confirm_no_match",
                    "my_claims",
                    "handoff_status",
                    "inquiry_status",
                    "case_lookup",
                }
                and state["lifecycle"] == "ACTIVE"
            ):
                try:
                    if self._calls_left(state) <= 0:
                        raise ModelFailure("MODEL_CONVERSATION_LIMIT")
                    reply = await self._compose(state, owner, turn_id, request, obs, reply, events)
                except ModelFailure as exc:
                    # The server already wrote a complete reply: a gate/scope prompt or a
                    # grounded claim template rendered from the fixtures. If the model's
                    # wording is rejected or unavailable, show that instead of an error.
                    self.store.assert_current(state["session_id"], owner, turn_id)
                    state["model_calls_used"] = state.get("model_calls_used", 0) + exc.calls
                    state["last_model_status"] = {
                        "mode": "openai",
                        "status": exc.code,
                        "calls": exc.calls,
                        **exc.usage,
                        "fallback": "server_template",
                    }
                    events.append(event("model.failed", **state["last_model_status"]))
                    state.pop("_topics_before_response", None)
                    if rendered_topics:
                        state["pending_topics"] = [
                            t for t in state.get("pending_topics", []) if t not in rendered_topics
                        ]
                    if exc.code != "MODEL_ANSWER_UNAVAILABLE":
                        state["_service_note"] = fallback_note(exc.code)
                if was_verify and auth_valid(state, self.settings):
                    receipt = self._verification_receipt(state)
                    if not (
                        receipt.split(", ")[-1].rstrip(".") in reply[:200]
                        and "verified" in reply[:200].lower()
                    ):
                        reply = receipt + "\n\n" + reply
                if empathy:
                    reply = empathy + "\n\n" + reply
                if state.get("handoff_note_offer"):
                    if not re.search(r"(?:would you like|shall I|want me).{0,80}(?:add|append)", reply, re.I):
                        reply += f"\n\nWould you like me to add this question to {state['handoff_note_offer']['id']}?"
                    if "Nothing has been added yet" not in reply:
                        reply += " Nothing has been added yet."
                just_selected = any(
                    e["kind"] == "phase.changed" and e["details"].get("rule") == "CASE_RESOLVED"
                    for e in events
                )
                if just_selected:
                    selected = self.gateway.get_claim(state, [])
                    if selected["case_id"] not in reply or selected["created_at"] not in reply:
                        reply += f" Claim reference: {selected['case_id']} ({selected['case_type']}, created {selected['created_at']})."
                note = state.get("pending_handoff_note")
                if note and note["unresolved"] and note["id"] not in reply:
                    reply += f" Only a representative can decide; your unresolved question is recorded in {note['id']}."
            return self._finish(state, turn_id, request, obs, events, reply, was_verify, operation)
        except ModelFailure as exc:
            if "_topics_before_response" in state:
                state["discussed"] = state.pop("_topics_before_response")
            state["last_model_status"] = {
                "mode": "openai",
                "status": exc.code,
                "calls": exc.calls,
                **exc.usage,
            }
            events.append(event("model.failed", **state["last_model_status"]))
            failure_reply = (
                "AI replies are paused because this demo has reached its local API-call or spending limit. "
                "Your confirmed progress is saved. The demo operator needs to renew the test allowance "
                "before AI replies can continue. Any pending email remains paused."
                if exc.code == "MODEL_EVAL_BUDGET_EXHAUSTED"
                else "I couldn’t produce a reliable answer because the language service is unavailable or its response did not pass validation. Your confirmed progress is saved. Please try again, use the available buttons, or request human help. Any pending email remains paused."
            )
            return self._finish(
                state,
                turn_id,
                request,
                empty_observation(),
                events,
                failure_reply,
                True,
            )
        except (AccessDenied, Conflict):
            # Release our lease on a rejected action, without applying uncommitted mutations.
            current = self.store.load(sid, owner)
            try:
                self.store.commit(
                    current, turn_id, [event("turn.rejected", reason="POLICY_OR_VERSION")], final=True
                )
            except Conflict:
                pass
            raise
        except Exception:
            current = self.store.load(sid, owner)
            return self._finish(
                current,
                turn_id,
                request,
                empty_observation(),
                [event("turn.failed", reason="DEPENDENCY_ERROR")],
                "A service error interrupted this step. Your last confirmed progress is saved. Please try again or request human help.",
                True,
            )

    def _verify_identity(self, state, owner, turn_id, touched, clarify, events):
        """Commit one reusable grant before any account-service or claim access."""
        if state.get("representative"):
            return self._verify_represented(state, owner, touched, clarify, events)
        result = "INCOMPLETE"
        if self.store.verify_failures(owner) >= self.store.VERIFY_LOCK_AFTER:
            state["verification_locked"] = True
        if not state["verification_locked"] and touched and not state["identity_conflicts"] and not clarify:
            if len([f for f in state["identity"] if f in ALLOWED_FIELDS]) >= 3:
                self.store.rate("verify:" + owner, 15, 600)
            result, subject = self.repo.verify(state["identity"])
            if result == "PASS":
                self.store.clear_verify_failures(owner)
                fields = [f for f in state["identity"] if f in ALLOWED_FIELDS]
                events = self._grant_identity(state, owner, turn_id, subject, fields, events)
            elif result == "FAIL":
                state["verification_attempts"] = self.store.record_verify_failure(owner)
                state["verification_locked"] = state["verification_attempts"] >= self.store.VERIFY_LOCK_AFTER
                events.append(event("verification.failed", attempt=state["verification_attempts"]))
        return result, events

    def _grant_identity(self, state, owner, turn_id, subject, fields, events):
        person = next(p for p in self.repo.policyholders if p["party_id"] == subject)
        rep = state.get("representative")
        state["auth"] = {
            "caller": {
                "role": "representative" if rep else "policyholder",
                "name": rep["caller"] if rep else person["name"],
            },
            "data_subject": {"party_id": subject, "name": person["name"]},
            "subject": subject,
            "fields": fields,
            "epoch": state["auth_epoch"],
            "issued_at": time.time(),
            "last_used_at": time.time(),
            "expires_at": time.time() + self.settings.auth_max_seconds,
        }
        if state.get("representative"):
            state["auth"]["representative"] = copy.deepcopy(state["representative"])
        state["identity"] = {}
        transition(state, "RESOLVE_INTENT", events, "VERIFICATION_PASSED")
        events.append(event("verification.passed", field_count=len(fields)))
        receipt(state, self._verification_receipt(state))
        self.store.commit(state, turn_id, events)
        state["profile_memory"], _ = self.store.memories(state, owner)
        pending = self._pending_support(state, owner)
        if pending:
            receipt(
                state,
                f"You already have request {pending['id']} — {pending['label']}. View updates or cancel in Human requests.",
            )
        return []

    def _finish(self, state, turn_id, request, observation, events, reply, was_verify, operation=None):
        note = state.pop("_service_note", None)
        if note and note not in reply:
            reply = reply + "\n\n" + note
        if state.pop("_human_option", False) and not state["auth"]:
            if not re.search(r"request human help", reply, re.I):
                reply = reply + "\n\n" + HUMAN_OPTION
        reply = limit_empathy(reply)
        protected = bool(state["auth"])
        service_turn = (
            observation.action in {"human", "my_claims", "handoff_status", "inquiry_status", "case_lookup"}
            or request.action == "resume"
        )
        message_case = None if service_turn else state["selected_case_id"]
        if state["auth"]:
            state["auth"]["last_used_at"] = time.time()
        state["messages"].extend(
            [
                {
                    "role": "user" if request.action == "message" else "system",
                    "at": time.time(),
                    "text": self._user_text(request, observation, was_verify),
                    "protected": protected,
                    "epoch": state["auth_epoch"],
                    "case_id": message_case,
                },
                {
                    "role": "assistant",
                    "at": time.time(),
                    "text": reply,
                    "protected": protected,
                    "epoch": state["auth_epoch"],
                    "case_id": message_case,
                },
            ]
        )
        state["messages"] = state["messages"][-100:]
        events.append(event("turn.finished", phase=state["phase"], result="RESPONSE_AVAILABLE"))
        self.store.commit(state, turn_id, events, final=True, operation=operation)
        return state
