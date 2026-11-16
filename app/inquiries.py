"""Account/case-scoped simulated insurer-email threads; no external mail adapter."""

import json
import time

from .consultation import caller_context, correspondence_visible
from .state import auth_valid, event, uid


class InquiryStore:
    def _inquiry_auth(self, db, state, owner):
        from .store import Conflict

        current = self._expire(db, self._owned(db, state["session_id"], owner))
        if (
            not auth_valid(current, self.settings)
            or not auth_valid(state, self.settings)
            or current["auth"]["subject"] != state["auth"]["subject"]
            or current["auth_epoch"] != state["auth_epoch"]
        ):
            raise Conflict("INQUIRY_REQUIRES_VERIFICATION", 403)
        return current["auth"]["subject"]

    def _save_inquiry(self, db, obj, owner, subject):
        db.execute(
            "INSERT OR REPLACE INTO inquiries VALUES(?,?,?,?,?,?)",
            (obj["id"], owner, subject, obj.get("case_id"), self.seal(obj), obj["updated_at"]),
        )

    def inquiries(self, state, owner, reference=None):
        from .store import Conflict

        with self.tx() as db:
            subject = self._inquiry_auth(db, state, owner)
            result = [
                self.unseal(r[0])
                for r in db.execute(
                    "SELECT payload FROM inquiries WHERE owner=? AND subject=? ORDER BY updated DESC LIMIT 100",
                    (owner, subject),
                )
            ]
            result = [r for r in result if correspondence_visible(state, r)]
            if reference:
                result = [r for r in result if r["id"] == reference]
                if not result:
                    raise Conflict("INQUIRY_NOT_FOUND", 404)
            return result

    def create_inquiry(self, state, owner, body):
        from .store import Conflict

        with self.tx() as db:
            subject = self._inquiry_auth(db, state, owner)
            fp = self.fingerprint(subject + json.dumps(body.model_dump(), sort_keys=True))
            prior = db.execute(
                "SELECT * FROM inquiry_requests WHERE owner=? AND message_id=?", (owner, body.message_id)
            ).fetchone()
            if prior:
                if prior["fingerprint"] != fp:
                    raise Conflict("IDEMPOTENCY_CONFLICT")
                row = db.execute(
                    "SELECT payload FROM inquiries WHERE id=? AND subject=? AND owner=?",
                    (prior["inquiry_id"], subject, owner),
                ).fetchone()
                if not row:
                    raise Conflict("INQUIRY_NOT_FOUND", 404)
                obj = self.unseal(row[0])
                if not correspondence_visible(state, obj):
                    raise Conflict("INQUIRY_NOT_FOUND", 404)
                return obj
            now = time.time()
            obj = {
                "id": "E-" + uid("e")[2:18].upper(),
                "caller": caller_context(state)["caller"],
                "case_id": body.case_id,
                "subject": body.subject.strip(),
                "body": body.body.strip(),
                "recipient": "Claims enquiries · simulated official inbox",
                "status": "QUEUED",
                "simulated": True,
                "revision": 1,
                "created_at": now,
                "updated_at": now,
                "timeline": [
                    {
                        "at": now,
                        "actor": "system",
                        "text": "Your enquiry is queued for simulated sending. No real recipient is configured and no external email will be sent.",
                    }
                ],
            }
            self._save_inquiry(db, obj, owner, subject)
            db.execute(
                "INSERT INTO inquiry_requests VALUES(?,?,?,?)", (owner, body.message_id, fp, obj["id"])
            )
            self._events(
                db,
                state,
                None,
                [event("inquiry.created", inquiry_id=obj["id"], case_id=body.case_id, result="QUEUED")],
            )
            return obj

    def update_inquiry(self, state, owner, reference, body):
        from .store import Conflict

        with self.tx() as db:
            subject = self._inquiry_auth(db, state, owner)
            row = db.execute(
                "SELECT payload FROM inquiries WHERE id=? AND owner=? AND subject=?",
                (reference, owner, subject),
            ).fetchone()
            if not row:
                raise Conflict("INQUIRY_NOT_FOUND", 404)
            obj = self.unseal(row[0])
            if not correspondence_visible(state, obj):
                raise Conflict("INQUIRY_NOT_FOUND", 404)
            key = "update-" + body.message_id
            fp = self.fingerprint(
                subject
                + reference
                + json.dumps(body.model_dump(exclude={"expected_revision"}), sort_keys=True)
            )
            prior = db.execute(
                "SELECT fingerprint FROM inquiry_requests WHERE owner=? AND message_id=?", (owner, key)
            ).fetchone()
            if prior:
                if prior[0] != fp:
                    raise Conflict("IDEMPOTENCY_CONFLICT")
                return obj
            if body.expected_revision != obj["revision"]:
                raise Conflict("INQUIRY_VERSION_CONFLICT")
            if len(obj["timeline"]) >= 100:
                raise Conflict("INQUIRY_HISTORY_LIMIT", 429)
            status, actor = obj["status"], "system"
            if body.action == "cancel" and status == "QUEUED":
                status, text = "CANCELLED", "Pending simulated enquiry cancelled. Nothing was sent."
            elif body.action == "followup" and status in {"SIMULATED_SENT", "SIMULATED_REPLIED"}:
                status, actor, text = "SIMULATED_SENT", "customer", body.text.strip()
            elif body.action == "demo_reply" and status == "SIMULATED_SENT":
                status, actor = "SIMULATED_REPLIED", "demo_insurer"
                text = f"SIMULATED official reply for {reference}: we have received your enquiry. Please confirm the claim reference and the specific information you would like reviewed. This demonstration does not change your claim's status, payment or deadlines. You can add a follow-up in this thread."
            elif body.action == "close" and status in {"SIMULATED_SENT", "SIMULATED_REPLIED"}:
                status, text = (
                    "CLOSED",
                    "Enquiry thread closed. This does not mark the insurance claim as resolved.",
                )
            else:
                raise Conflict("INVALID_INQUIRY_TRANSITION")
            obj.update(status=status, updated_at=time.time(), revision=obj["revision"] + 1)
            obj["timeline"].append({"at": obj["updated_at"], "actor": actor, "text": text})
            self._save_inquiry(db, obj, owner, subject)
            db.execute("INSERT INTO inquiry_requests VALUES(?,?,?,?)", (owner, key, fp, reference))
            self._events(
                db,
                state,
                None,
                [event("inquiry.updated", inquiry_id=reference, action=body.action, result=status)],
            )
            return obj

    def dispatch_inquiries(self):
        with self.tx() as db:
            for row in db.execute(
                "SELECT * FROM inquiries WHERE updated<?", (time.time() - self.settings.email_delay,)
            ).fetchall():
                obj = self.unseal(row["payload"])
                if obj["status"] != "QUEUED":
                    continue
                obj.update(status="SIMULATED_SENT", updated_at=time.time(), revision=obj["revision"] + 1)
                obj["timeline"].append(
                    {
                        "at": time.time(),
                        "actor": "system",
                        "text": "Simulated sending recorded. No real email was sent. Awaiting a simulated official reply; use the demo reply control to test the return path.",
                    }
                )
                self._save_inquiry(db, obj, row["owner"], row["subject"])
