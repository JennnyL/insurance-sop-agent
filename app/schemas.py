from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

IdentityField = Literal["name", "dob", "phone", "email", "ssn_last4", "national_id_last4", "policy_number"]
Intent = Literal[
    "status_inquiry", "denial_question", "document_guidance", "amount_question", "next_steps", "human_help"
]
Topic = Literal[
    "status",
    "denial",
    "documents",
    "alternatives",
    "amounts",
    "deadline",
    "timing",
    "submission",
    "receipt",
    "unknown",
    "verification",
]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IdentityObservation(Strict):
    field: IdentityField
    value: str
    quote: str
    occurrence: int
    subject: Literal["self", "other", "unknown"]
    assertion: Literal[
        "affirm", "replace", "retract", "refuse", "unknown", "negated", "quoted", "hypothetical"
    ]


class Hint(Strict):
    field: Literal["case_type", "status", "month", "year", "case_id"]
    value: str
    quote: str
    occurrence: int
    assertion: Literal["affirm", "replace", "retract", "negated", "unknown"]


class Observation(Strict):
    scope: Literal["in_scope", "out_of_scope", "mixed", "uncertain", "claim_term"]
    role: Literal["self", "representative", "unknown"]
    identity: list[IdentityObservation]
    hints: list[Hint]
    case_relation: Literal["same", "new", "uncertain"]
    subject_changed: bool
    intents: list[Intent]
    topics: list[Topic]
    emotion: Literal["neutral", "frustration", "anxiety", "anger", "confusion", "refusal"]
    emotion_quote: str
    action: Literal[
        "continue",
        "finish",
        "human",
        "handoff_status",
        "inquiry_status",
        "my_claims",
        "case_lookup",
        "consent_status",
        "refuse_all",
        "cancel",
        "confirm_no_match",
    ]
    email_preference: Literal["unmentioned", "want", "skip", "uncertain"]
    requires_human_decision: bool
    alternatives_exhausted: bool
    resolved_confirmation: bool
    style: Literal["warm", "concise", "stepwise"]


class LookupForm(Strict):
    name: str = Field(default="", max_length=100)
    dob: str = Field(default="", max_length=10, pattern=r"^(?:\d{4}-\d{2}-\d{2})?$")
    phone: str = Field(default="", max_length=30)
    email: str = Field(default="", max_length=150)
    ssn_last4: str = Field(default="", max_length=4, pattern=r"^(?:\d{4})?$")
    national_id_last4: str = Field(default="", max_length=4, pattern=r"^(?:\d{4})?$")
    policy_number: str = Field(default="", max_length=30, pattern=r"^(?:POL-\d+)?$")
    case_id: str = Field(default="", max_length=30, pattern=r"^(?:CL-\d+)?$")

    @model_validator(mode="after")
    def not_empty(self):
        if not any(value.strip() for value in self.model_dump().values()):
            raise ValueError("Enter identity details or a claim ID")
        return self


class TurnRequest(Strict):
    message_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    expected_version: int = Field(ge=0)
    text: str = Field(default="", max_length=4000)
    action: Literal[
        "message",
        "finish",
        "email_send",
        "email_skip",
        "email_cancel",
        "human",
        "reset",
        "cancel",
        "resume",
        "wait_human",
        "confirm_claim",
        "handoff_note_accept",
        "handoff_note_decline",
        "consent_check",
        "consent_timeout",
        "interrupt",
        "form",
        "my_claims",
        "handoff_status",
        "inquiry_status",
        "email_edit",
        "email_prepare",
    ] = "message"
    offer_id: str | None = Field(default=None, max_length=100)
    form: LookupForm | None = None
    email_note: str | None = Field(default=None, max_length=1000)
    target_message_id: str | None = Field(
        default=None, min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$"
    )

    @model_validator(mode="after")
    def form_matches_action(self):
        if (self.action == "form") != (self.form is not None):
            raise ValueError("A form payload requires the form action")
        if self.form and self.text:
            raise ValueError("Use either the form or message text")
        if (self.action == "email_edit") != (self.email_note is not None):
            raise ValueError("An email note requires the email_edit action")
        return self


class HandoffUpdate(Strict):
    session_id: str = Field(max_length=100)
    message_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    expected_revision: int = Field(ge=1)
    action: Literal["note", "cancel", "demo_acknowledge", "demo_reply", "demo_close"]
    text: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def note_required(self):
        if self.action == "note" and not self.text.strip():
            raise ValueError("Enter a follow-up message")
        if self.action != "note" and self.text:
            raise ValueError("Only customer notes accept text")
        return self


class InquiryCreate(Strict):
    message_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    case_id: str | None = Field(default=None, max_length=30, pattern=r"^CL-\d+$")
    subject: str = Field(min_length=1, max_length=150)
    body: str = Field(min_length=1, max_length=4000)

    @model_validator(mode="after")
    def nonempty(self):
        if not self.subject.strip() or not self.body.strip():
            raise ValueError("Enter a subject and message")
        return self


class InquiryUpdate(Strict):
    message_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    expected_revision: int = Field(ge=1)
    action: Literal["followup", "demo_reply", "close", "cancel"]
    text: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def nonempty(self):
        if self.action == "followup" and not self.text.strip():
            raise ValueError("Enter your follow-up")
        if self.action != "followup" and self.text:
            raise ValueError("Only followup accepts text")
        return self


def empty_observation(**overrides):
    return Observation(
        **{
            "scope": "in_scope",
            "role": "unknown",
            "identity": [],
            "hints": [],
            "case_relation": "same",
            "subject_changed": False,
            "intents": [],
            "topics": [],
            "emotion": "neutral",
            "emotion_quote": "",
            "action": "continue",
            "email_preference": "unmentioned",
            "requires_human_decision": False,
            "alternatives_exhausted": False,
            "resolved_confirmation": False,
            "style": "warm",
            **overrides,
        }
    )
