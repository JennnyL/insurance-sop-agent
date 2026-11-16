"""Redact identity spans while preserving the customer's insurance question."""

import re


def redact(text, observation=None, people=()):
    spans = []
    for item in observation.identity if observation else []:
        if item.field == "policy_number" or item.assertion in {"refuse", "retract"} or not item.quote:
            continue
        matches = list(re.finditer(re.escape(item.quote), text))
        if 0 < item.occurrence <= len(matches):
            m = matches[item.occurrence - 1]
            spans.append((m.start(), m.end(), "[" + item.field.upper() + "]"))
    for start, end, label in sorted(spans, reverse=True):
        text = text[:start] + label + text[end:]
    for person in people:
        for field in ("name", "dob", "phone", "email", "id_last4"):
            if person.get(field):
                text = re.sub(re.escape(person[field]), "[" + field.upper() + "]", text, flags=re.I)
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[EMAIL]", text)
    text = re.sub(r"(?<!\d)\+?\d[\d ()-]{8,}\d(?!\d)", "[PHONE]", text)
    text = re.sub(
        r"(?i)(\b(?:dob|date of birth|birthday)(?:\s+is)?\s*[:=]?\s*)\d{4}-\d{2}-\d{2}", r"\1[DOB]", text
    )
    text = re.sub(
        r"(?i)(\b(?:ssn|social security)(?: last (?:four|4))?(?: is)?\s*[:=]?\s*)\d[\d -]{2,10}\d",
        r"\1[SSN_LAST4]",
        text,
    )
    text = re.sub(r"(?i)(\bmy name is\s+)[A-Za-z]+(?:[ -][A-Za-z]+){0,2}(?=,|\.|;|$)", r"\1[NAME]", text)
    return text
