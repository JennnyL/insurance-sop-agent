"""Simulated policyholder consent, separate from matching identity fields.

Relationship fixtures identify the requested relationship; they never grant access.
Only the server-owned consent sequence can approve a representative grant.
"""

import re
import time

from .interpreter import normalize, source_span
from .schemas import IdentityObservation
from .state import ALLOWED_FIELDS, event, revoke_auth, uid


def receipt(state, text):
    pending = state.setdefault("pending_receipts", [])
    if text not in pending:
        pending.append(text)


class RepresentativeFlow:
    def _leave_representative_mode(self, state, obs, request, events):
        """Representative mode is always exitable by the caller identifying as the policyholder."""
        rep = state.get("representative")
        if not rep:
            return False
        says_self = re.search(
            r"\b(?:i(?:'m| am|’m) the policyholder|policyholder myself|(?:it'?s|it’s|this is) my own (?:claim|policy|account)|"
            r"i(?:'m| am|’m) not (?:calling )?(?:for|on behalf of) (?:anyone|someone|somebody) else|"
            r"not (?:a|the) representative)\b|我(?:就)?是(?:投保人|本人)",
            request.text,
            re.I,
        )
        own_fields = {
            i.field
            for i in obs.identity
            if i.subject == "self"
            and i.field in ALLOWED_FIELDS
            and i.field != "name"
            and i.assertion in {"affirm", "replace"}
        }
        third_party = re.search(
            r"\b(?:her|his|their|on behalf of|calling (?:for|about)|my (?:mother|father|parent|son|daughter|wife|husband|mom|dad))\b"
            r"|代",
            request.text,
            re.I,
        )
        own_bundle = (
            obs.role == "self"
            and len(own_fields) >= 2
            and not third_party
            and not any(i.subject == "other" for i in obs.identity)
        )
        if not (says_self or own_bundle):
            return False
        if state.get("auth"):
            revoke_auth(state, events, "REPRESENTATIVE_MODE_EXITED")
        state["representative"] = None
        state["identity"], state["identity_conflicts"] = {}, []
        obs.role, obs.subject_changed = "self", False
        if says_self and not third_party:
            # The observer saw a representative call; details in this message are the caller's own.
            for item in obs.identity:
                if item.subject == "other":
                    item.subject = "self"
        events.append(event("representative.mode_exited"))
        receipt(
            state,
            "Understood: you are the policyholder. I have left representative mode; please verify with your own details.",
        )
        return True

    def _identify_representative(self, state, obs, request, events):
        if self._leave_representative_mode(state, obs, request, events):
            return
        rep = state.get("representative")
        explicit = re.search(
            r"\b(?:on behalf of|calling (?:for|about) my (?:mother|father|parent|son|daughter|wife|husband)|my (?:mother|father|parent|son|daughter|wife|husband)'s (?:claim|policy))\b|代.{0,15}(?:来电|理赔|查询)",
            request.text,
            re.I,
        )
        if explicit or any(i.subject == "other" for i in obs.identity):
            obs.role = "representative"
        if obs.role != "representative" and not rep:
            return
        # Fail closed on clear representative language even when the model misses
        # the role. These spans come only from the current message, never fixtures.
        for subject, label in [
            ("self", r"my (?:full )?name is|i'm|i am|i’m"),
            ("other", r"my mother|my father|on behalf of|her name is|his name is"),
        ]:
            for match in re.finditer(
                r"(?i:" + label + r")\s+([A-Z][a-zA-Z'-]+(?: [A-Z][a-zA-Z'-]+){1,3})\b", request.text
            ):
                name = match.group(1)
                if not any(i.field == "name" and i.quote == name for i in obs.identity):
                    obs.identity.append(
                        IdentityObservation(
                            field="name",
                            value=name,
                            quote=name,
                            occurrence=1,
                            subject=subject,
                            assertion="affirm",
                        )
                    )
                else:
                    for item in obs.identity:
                        if item.field == "name" and item.quote == name:
                            item.subject = subject
        for item in obs.identity:
            span = source_span(request.text, item.quote, item.occurrence)
            if not span or item.field == "name":
                continue
            before = request.text[max(0, span[0] - 50) : span[0]]
            if re.search(
                r"\b(?:her|his|their)\s+(?:dob|date of birth|email|phone|ssn|birthday)(?:\s+(?:is|last|four|digits)|:)*\s*$",
                before,
                re.I,
            ):
                item.subject = "other"
        callers = [
            i
            for i in obs.identity
            if i.field == "name"
            and i.subject == "self"
            and i.assertion in {"affirm", "replace"}
            and source_span(request.text, i.quote, i.occurrence)
            and normalize("name", i.value) == normalize("name", i.quote)
        ]
        caller = callers[-1].value if callers else (rep or {}).get("caller", "")
        changing_target = (
            rep
            and rep["status"] in {"approved", "pending"}
            and any(
                i.subject == "other"
                and i.assertion in {"replace", "retract"}
                and source_span(request.text, i.quote, i.occurrence)
                for i in obs.identity
            )
        )
        if changing_target:
            revoke_auth(state, events, "REPRESENTED_IDENTITY_CORRECTED")
            rep = None
            receipt(
                state,
                "The policyholder identity details changed. The earlier consent no longer applies; the policyholder must be verified and consent again.",
            )
        if state.get("auth") and (
            not rep or (caller and normalize("name", caller) != normalize("name", rep["caller"]))
        ):
            revoke_auth(state, events, "REPRESENTATIVE_CHANGED")
            rep = None
        if rep and not rep["caller"] and caller:
            rep["caller"] = caller
            rep["caller_identified"] = True
        if not rep or (caller and normalize("name", caller) != normalize("name", rep["caller"])):
            state["identity"] = {}
            state["identity_conflicts"] = []
            state["representative"] = rep = {"caller": caller, "status": "identity", "relationship": None}
        matches = [
            r
            for r in self.repo.representatives
            if normalize("name", r["rep_name"]) == normalize("name", caller)
        ]
        if len(matches) == 1:
            rep["relationship"] = matches[0]
            rep["caller"] = matches[0]["rep_name"]
        # A role assertion cannot replace or revoke an identity already established
        # for this same representative + policyholder pair.
        obs.subject_changed = False

    def _verify_represented(self, state, owner, touched, clarify, events):
        rep = state["representative"]
        touched = rep.pop("caller_identified", False) or touched
        if rep["status"] in {"pending", "timeout", "approved"}:
            return "INCOMPLETE", events
        if self.store.verify_failures(owner) >= self.store.VERIFY_LOCK_AFTER:
            state["verification_locked"] = True
        if (
            not rep["caller"]
            or not rep["relationship"]
            or state["verification_locked"]
            or not touched
            or clarify
            or state["identity_conflicts"]
        ):
            return "INCOMPLETE", events
        if len([f for f in state["identity"] if f in ALLOWED_FIELDS]) >= 3:
            self.store.rate("verify:" + owner, 15, 600)
        result, subject = self.repo.verify(state["identity"])
        if result == "PASS" and subject == rep["relationship"]["buyer_party_id"]:
            rep.update(
                status="pending",
                consent_id=uid("consent"),
                subject=subject,
                fields=[f for f in state["identity"] if f in ALLOWED_FIELDS],
                started_at=time.time(),
                index=0,
                scenario=self.settings.consent_scenario,
                epoch=state["auth_epoch"],
            )
            events.append(
                event("representative.consent_requested", consent_id=rep["consent_id"], status="pending")
            )
            receipt(state, self._representative_prompt(state))
            return "PENDING", events
        if result == "FAIL" or result == "PASS":
            state["verification_attempts"] = self.store.record_verify_failure(owner)
            state["verification_locked"] = state["verification_attempts"] >= self.store.VERIFY_LOCK_AFTER
            events.append(event("verification.failed", attempt=state["verification_attempts"]))
            return "FAIL", events
        return result, events

    def _representative_prompt(self, state):
        rep = state["representative"]
        if rep["status"] == "pending":
            return "The policyholder’s three identity fields matched. A consent request has been started and is still pending. Claim details stay private until approval. Choose Check policyholder consent or request human help."
        if rep["status"] == "timeout":
            return "The policyholder consent request timed out. Claim details remain locked. Your human handling request is saved; no real person has been contacted."
        if not rep["caller"]:
            return "You are calling for someone else. Please give your own full name so I can check the representative relationship, then provide three of the policyholder's identity fields. Their consent is also required before I can disclose claim details."
        if not rep["relationship"]:
            return "I couldn't match the representative relationship in the available records. No claim information can be disclosed. Please check your name and relationship, or request human help."
        return (
            "I found the representative relationship. This does not authorize access. "
            + self._verification_prompt(state, represented=True)
        )

    def _check_representative_consent(self, state, owner, turn_id, events, timeout=False):
        rep = state.get("representative")
        if not rep or rep["status"] != "pending":
            return "There is no pending policyholder consent request to check.", events
        if rep["epoch"] != state["auth_epoch"]:
            return "This consent request is no longer current. Please verify the policyholder again.", events
        if timeout:
            rep["scenario"] = "timeout"
        sequence = self.repo.consent_scenarios[rep["scenario"]]["status_sequence"]
        rep["index"] += 1
        expired = time.time() - rep["started_at"] >= self.settings.consent_timeout_seconds
        status = sequence[min(rep["index"], len(sequence) - 1)]
        if expired or (status == "pending" and rep["index"] >= len(sequence) - 1):
            rep["status"] = "timeout"
            receipt(
                state,
                "The simulated policyholder consent request timed out. No authorization was granted; claim details remain locked.",
            )
            events.append(
                event("representative.consent_changed", consent_id=rep["consent_id"], status="timeout")
            )
            return self._human(state, owner, events, "REPRESENTATIVE_CONSENT_TIMEOUT"), events
        if status == "approved":
            rep["status"] = "approved"
            events.append(
                event("representative.consent_changed", consent_id=rep["consent_id"], status="approved")
            )
            events = self._grant_identity(state, owner, turn_id, rep["subject"], rep["fields"], events)
            claims = self.gateway.find_cases(state, events)
            state["candidates"] = [
                {k: c[k] for k in ("case_id", "case_type", "created_at", "status")} for c in claims
            ]
            if len(claims) == 1:
                claim = claims[0]
                state["claim_confirmation"] = claim["case_id"]
                prompt = f"Is {claim['case_id']} ({claim['case_type']}, created {claim['created_at']}) the claim you want to discuss?"
            else:
                prompt = "Please choose the policyholder’s claim from the list below."
            # Approval and identity are one leading receipt, never a trailing paragraph.
            approved = self._verification_receipt(state)
            state["pending_receipts"] = [r for r in state.get("pending_receipts", []) if r != approved]
            return approved + "\n\n" + prompt, events
        events.append(event("representative.consent_checked", consent_id=rep["consent_id"], status="pending"))
        return (
            "Checked policyholder consent: still pending. Claim details remain private. You can check again or request human help.",
            events,
        )
