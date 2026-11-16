"""Authoritative customer-facing capabilities and permitted alternatives."""

INTERFACE = {
    "Email enquiry": "Requires a current verified policyholder grant, including policyholder consent for representatives. Opens Subject + Your message + Related claim. Records a simulated question to the insurer with an E-reference. It cannot upload documents. Existing valid verification is reused; unverified callers must verify first.",
    "Human requests": "Asynchronous simulated H-ticket. Unverified callers can request help and receive a receipt in the current conversation; the account list is visible only after verification. Continue with AI answers questions; Wait for human only acknowledges saved messages. View or cancel verified account requests here. No real contact occurs.",
    "Finish consultation": "Offers a conversation summary card in chat. The customer chooses Send this summary or Skip email. Review conversation copy opens the same draft with a customer-note editor. Email sending is simulated.",
    "My claims": "Lists the verified policyholder's claims without a claim or policy number. All panels and new conversations reuse valid authorization until expiry or logout.",
}

SSN_ALTERNATIVES = "You do not have to provide an SSN. You can verify with any three of full name, date of birth, registered phone, and registered email. We need three matching fields to protect private claim information."

EMPATHY = {
    "frustration": [
        "I understand this is frustrating.",
        "I'm sorry this process has been frustrating.",
    ],
    "anger": ["I hear how upsetting this has been.", "I'm sorry this experience has left you angry."],
    "anxiety": [
        "I understand that uncertainty about a claim can be worrying. Let’s take this one step at a time.",
        "I hear your concern; we can work through this one step at a time.",
    ],
    "confusion": ["I can break this down into simpler steps.", "Let me clarify what happens next."],
    "refusal": [
        "That's okay—you can choose which identity fields to share.",
        "I respect your choice about the information you share.",
    ],
}
