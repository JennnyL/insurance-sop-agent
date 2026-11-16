import hashlib
import json
import re
import time
from datetime import date
from decimal import Decimal
from pathlib import Path

from .config import ROOT
from .interpreter import normalize
from .next_steps import next_steps
from .state import ALLOWED_FIELDS, auth_valid, event

FIXTURES = ROOT / "apps" / "insurance_claims" / "fixtures"
DOC_ALIASES = {
    "pathology report": "original pathology report",
    "office note": "treating provider office note",
}


class AccessDenied(Exception):
    pass


class FixtureRepository:
    def __init__(self, path: Path = FIXTURES):
        self.policyholders = json.loads((path / "policyholders.json").read_text())
        self.claims = json.loads((path / "claims.json").read_text())
        self.guidelines = json.loads((path / "required_document_guideline.json").read_text())
        self.field_schema = json.loads((path / "claim_schema.json").read_text())
        self.representatives = json.loads((path / "representatives.json").read_text())
        self.consent_scenarios = json.loads((path / "consent_scenarios.json").read_text())
        self.hashes = {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(path.glob("*.json"))
        }
        parties = {p["party_id"] for p in self.policyholders}
        assert len(parties) == len(self.policyholders), "Duplicate party IDs"
        assert len({c["case_id"] for c in self.claims}) == len(self.claims), "Duplicate claim IDs"
        for p in self.policyholders:
            for field in ("name", "dob", "phone", "email", "policy_number"):
                assert normalize(field, p[field]), "Invalid policyholder field"
            assert p["id_type"] in ("ssn_last4", "national_id_last4")
            assert re.fullmatch(r"\d{4}", p["id_last4"])
        for c in self.claims:
            assert c["party_id"] in parties
            assert c["status"] in {"open", "closed", "denied"}
            assert c["case_type"] in {"auto", "healthcare", "dental"}
            date.fromisoformat(c["created_at"])
            if c.get("appeal_deadline"):
                date.fromisoformat(c["appeal_deadline"])
            for field in ("net_pay", "net_fee", "expected_reimbursement_amount", "allowed_max_amount"):
                assert re.fullmatch(r"\d+\.\d{2}", c[field]) and Decimal(c[field]) >= 0

    def verify(self, identity):
        supplied = {f: v["value"] for f, v in identity.items() if f in ALLOWED_FIELDS}
        if len(supplied) < 3:
            return "INCOMPLETE", None
        candidates = []
        for p in self.policyholders:
            if identity.get("policy_number") and identity["policy_number"]["value"] != p["policy_number"]:
                continue
            matched = True
            for field, value in supplied.items():
                if field in ("ssn_last4", "national_id_last4"):
                    valid = p["id_type"] == field and value == p["id_last4"]
                else:
                    values = [p[field]] + p.get(field + "_aliases", [])
                    valid = value in [normalize(field, v) for v in values]
                matched &= valid
            if matched:
                candidates.append(p)
        return ("PASS", candidates[0]["party_id"]) if len(candidates) == 1 else ("FAIL", None)


class Gateway:
    def __init__(self, repository, settings):
        self.repo, self.settings = repository, settings

    def authorize(self, state, phases):
        if (
            not auth_valid(state, self.settings)
            or state["phase"] not in phases
            or state["lifecycle"] != "ACTIVE"
        ):
            raise AccessDenied("A valid subject grant and allowed phase are required")
        return state["auth"]["subject"]

    def account(self, state):
        if not auth_valid(state, self.settings):
            raise AccessDenied("Verify three identity fields to view your account")
        subject = state["auth"]["subject"]
        person = next(p for p in self.repo.policyholders if p["party_id"] == subject)
        fields = (
            "case_id",
            "case_type",
            "status",
            "created_at",
            "denial_reason",
            "documents_needed",
            "appeal_deadline",
        )
        return {
            "profile": {"name": person["name"], "policy_number": person["policy_number"]},
            "claims": sorted(
                [{k: c.get(k) for k in fields} for c in self.repo.claims if c["party_id"] == subject],
                key=lambda c: c["created_at"],
                reverse=True,
            ),
            "source_note": "Existing demo claim records only. Source update time is not provided; this app does not file new claims or synchronize with an insurer.",
        }

    def find_cases(self, state, events):
        subject = self.authorize(state, {"RESOLVE_INTENT"})
        hints = {k: v["value"] for k, v in state["hints"].items()}
        cases = []
        for c in self.repo.claims:
            if c["party_id"] != subject:
                continue
            if any(hints.get(k) and hints[k] != c[k] for k in ("case_id", "case_type", "status")):
                continue
            created = date.fromisoformat(c["created_at"])
            if hints.get("month") and int(hints["month"]) != created.month:
                continue
            if hints.get("year") and int(hints["year"]) != created.year:
                continue
            cases.append(c)
        events.append(event("tool.called", tool="find_cases", result="OK", count=len(cases)))
        return cases

    def get_claim(self, state, events):
        subject = self.authorize(state, {"PROCESS_CASE", "POST_PROCESS"})
        for c in self.repo.claims:
            if c["case_id"] == state["selected_case_id"] and c["party_id"] == subject:
                events.append(event("tool.called", tool="get_claim", result="OK", case_id=c["case_id"]))
                return c.copy()
        raise AccessDenied("No accessible selected claim")

    def recipient(self, state):
        subject = self.authorize(state, {"POST_PROCESS"})
        return next(p["email"] for p in self.repo.policyholders if p["party_id"] == subject)


def guidance_applicability(metadata, case, business_date):
    """Explicit demo scope; absent required dimensions never become wildcards."""
    if not metadata.get("approved") or not metadata.get("owner"):
        return "UNKNOWN"
    if metadata.get("conflict"):
        return "CONFLICT"
    if metadata.get("scope") == "DEMO_FIXTURE_GLOBAL":
        return "VALID" if metadata.get("demo_only") else "UNKNOWN"
    for key in metadata.get("required_dimensions", []):
        if key not in case or key not in metadata:
            return "UNKNOWN"
        if case[key] != metadata[key]:
            return "NOT_APPLICABLE"
    if not metadata.get("valid_from") or not metadata.get("valid_until"):
        return "UNKNOWN"
    if (
        not date.fromisoformat(metadata["valid_from"])
        <= business_date
        <= date.fromisoformat(metadata["valid_until"])
    ):
        return "NOT_APPLICABLE"
    return "VALID"


class EvidenceRenderer:
    def __init__(self, gateway):
        self.gateway = gateway
        self.guidance_metadata = {
            "owner": "R0 demo fixtures",
            "approved": True,
            "scope": "DEMO_FIXTURE_GLOBAL",
            "demo_only": True,
        }

    def answer(self, state, topics, events, pending=None, notification=False):
        c = self.gateway.get_claim(state, events)
        guide = self.gateway.repo.guidelines
        pieces = []
        refs = []

        def add(text, path, source="claims.json", kind="fact"):
            ref = {
                "id": c["case_id"] + ":" + path,
                "source": source,
                "record_id": c["case_id"],
                "field_path": path,
                "text": text,
                "kind": kind,
                "subject": state["auth"]["subject"],
                "auth_epoch": state["auth_epoch"],
                "episode_version": state["episode_version"],
                "source_as_of": None,
                "retrieved_at": time.time(),
            }
            pieces.append(text)
            refs.append(ref)

        def guidance(text, path):
            result = guidance_applicability(self.guidance_metadata, c, self.gateway.settings.business_date)
            if result == "VALID":
                add(text, path, "required_document_guideline.json", "guidance")
            else:
                add(
                    "I cannot establish that this guidance applies. A claims representative needs to review it.",
                    "guidance_unavailable",
                    "sop-policy",
                    "limitation",
                )

        policy = next_steps(
            c,
            self.gateway.settings.business_date,
            pending,
            representative=bool((state.get("auth") or {}).get("representative")),
        )
        expired = policy["expired"]
        topics = list(dict.fromkeys(topics or ["status"]))
        if expired:
            topics = list(dict.fromkeys(topics + ["deadline"]))
            add(policy["text"], "permitted_next_steps", "sop-policy", "limitation")
        state["last_answer_topics"] = topics.copy()
        if "status" in topics or not state["discussed"]:
            add(
                f"Claim {c['case_id']} is {c['status']}. It is a {c['case_type']} claim created on {c['created_at']}.",
                "status,case_type,created_at",
            )
        docs = c.get("documents_needed", [])
        if "denial" in topics:
            if c.get("denial_reason"):
                add("The recorded reason for denial is that " + c["denial_reason"] + ".", "denial_reason")
                if "denial" not in state["discussed"] and "documents" not in topics:
                    topics.append("documents")
                if "denial" not in state["discussed"] and "deadline" not in topics:
                    topics.append("deadline")
            else:
                add("This record does not contain a denial reason.", "denial_reason")
        if any(t in topics for t in ("documents", "alternatives", "submission")):
            if docs:
                add("The review file is missing: " + ", ".join(docs) + ".", "documents_needed")
            else:
                add(
                    "This record does not list any requested documents. That does not confirm that all materials have been received.",
                    "documents_needed",
                )
        if "documents" in topics and docs and not expired:
            for doc in docs:
                key = DOC_ALIASES.get(doc, doc)
                entry = guide["document_guidance"].get(key)
                if entry:
                    guidance(entry["en"], "document_guidance." + key)
                else:
                    guidance(
                        "There is no dedicated checklist for "
                        + doc
                        + ". "
                        + guide["case_type_guidance"].get(c["case_type"], {}).get("en", "")
                        + " Prepare a complete, readable copy showing the claim reference, relevant dates, and issuing provider.",
                        "case_type_guidance." + c["case_type"],
                    )
        if "alternatives" in topics and docs and not expired:
            for doc in docs:
                key = DOC_ALIASES.get(doc, doc)
                entry = guide["document_alternative_guidance"].get(
                    key, guide["document_alternative_guidance"]["default"]
                )
                guidance(
                    entry["en"],
                    "document_alternative_guidance."
                    + (key if key in guide["document_alternative_guidance"] else "default"),
                )
            add(
                "A substitute is subject to review; I cannot guarantee acceptance or change the claim decision.",
                "alternative_boundary",
                "sop-policy",
                "limitation",
            )
        if "amounts" in topics:
            labels = {
                "expected_reimbursement_amount": "Expected insurer reimbursement",
                "allowed_max_amount": "Allowed maximum for a covered service",
                "net_pay": "Finalized insurer payment recorded",
                "net_fee": "Adjusted fee schedule rate",
            }
            for field, label in labels.items():
                add(f"{label}: ${Decimal(c[field]):,.2f}.", field)
            add(
                "The allowed maximum is not a promised payment, and expected reimbursement is distinct from payment recorded. These fields alone do not establish your out-of-pocket responsibility.",
                "amount_definitions",
                "claim_schema.json",
                "definition",
            )
        if "deadline" in topics:
            deadline = c.get("appeal_deadline")
            today = self.gateway.settings.business_date
            if deadline:
                d = date.fromisoformat(deadline)
                timing = "has passed" if d < today else "is today" if d == today else "is upcoming"
                add(
                    f"The recorded appeal deadline is {deadline}; it {timing} as of {today.isoformat()}.",
                    "appeal_deadline",
                )
                if d < today:
                    add(
                        "A claims representative must check whether any options remain after that date. I cannot extend the deadline or promise that a late appeal will be accepted.",
                        "expired_deadline",
                        "sop-policy",
                        "limitation",
                    )
                elif d == today:
                    add(
                        "No cutoff time is provided. Please ask a claims representative to confirm the submission requirements.",
                        "deadline_today",
                        "sop-policy",
                        "limitation",
                    )
                else:
                    add(
                        "Please confirm the submission requirements before the recorded deadline; preparing documents does not submit an appeal.",
                        "deadline_boundary",
                        "sop-policy",
                        "limitation",
                    )
            else:
                add(
                    "No appeal deadline is provided in this claim record. A representative can confirm any applicable time limit.",
                    "appeal_deadline",
                )
        if expired and "timing" in topics:
            add(
                "These records do not establish a processing-time estimate for a request after the deadline; representative assessment is needed first.",
                "late_review_timing",
                "sop-policy",
                "limitation",
            )
        if expired and "submission" in topics:
            add(
                "No submission or appeal has been performed. These records do not establish an available late-submission channel; request representative assessment first.",
                "late_submission",
                "sop-policy",
                "limitation",
            )
        if "timing" in topics and not expired:
            if docs:
                guidance(
                    "The general demo guidance describes review after missing files are received as usually less than a week, with intake or another review cycle sometimes needed. This is an estimate, not a guarantee or confirmation that review has restarted.",
                    "claim_followup_settings.average_processing_time_after_submission",
                )
            else:
                add(
                    "This record does not provide a processing-time estimate for this claim.",
                    "processing_time",
                    "sop-policy",
                    "limitation",
                )
        if "submission" in topics and not expired:
            if docs:
                guidance(guide["default_guidance"]["en"], "default_guidance")
            add(
                "I do not have an actual portal link, mailing address, or upload tool here. A claims representative can confirm the correct channel. No documents or appeal have been submitted by this chat.",
                "submission_boundary",
                "sop-policy",
                "limitation",
            )
        if "receipt" in topics:
            add(
                "I cannot check receipt of an upload with these records. A claims representative can verify whether the files were attached; an unchanged status alone does not establish receipt.",
                "receipt_boundary",
                "sop-policy",
                "limitation",
            )
        if "unknown" in topics:
            add(
                "The available claim records do not establish an answer to that part of your question. I can request human review; I cannot make a coverage decision, give medical or legal advice, or guarantee an outcome.",
                "unsupported",
                "sop-policy",
                "limitation",
            )
        if notification:
            topics = list(dict.fromkeys(topics + ["notification"]))
            add(
                "No notification history is available in these records. I cannot establish whether, when or how you were notified.",
                "notification_history",
                "sop-policy",
                "limitation",
            )
        for ref in refs:
            state["evidence"] = [r for r in state["evidence"] if r["id"] != ref["id"]] + [ref]
        state["discussed"] = list(dict.fromkeys(state["discussed"] + topics))
        state["open_questions"] = (
            ["Human review needed for unsupported question"] if "unknown" in topics else []
        )
        events.append(event("answer.grounded", references=[r["id"] for r in refs]))
        return "\n\n".join(pieces), refs
