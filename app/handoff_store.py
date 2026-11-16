"""Independent support requests, representative presence and scoped chat access."""

import time

from .consultation import caller_context, correspondence_visible
from .representatives import receipt
from .state import auth_valid, event
from .support import TERMINAL, anonymous_access, fingerprint_update, make_ticket, public_ticket, update_ticket


class HandoffStore:
    def _save_handoff(self, db, ticket, owner):
        db.execute(
            "INSERT OR REPLACE INTO handoffs VALUES(?,?,?,?,?,?,?)",
            (
                ticket["id"],
                owner,
                ticket.get("subject"),
                ticket.get("case_id"),
                ticket["session_id"],
                self.seal(ticket),
                ticket["updated_at"],
            ),
        )

    def prepare_handoff(self, state, owner, reason):
        ticket = make_ticket(state, reason, self.settings)
        with self.tx() as db:
            self._owned(db, state["session_id"], owner)
            rows = db.execute(
                "SELECT payload FROM handoffs WHERE owner=? ORDER BY updated DESC", (owner,)
            ).fetchall()
            for row in rows:
                old = self.unseal(row[0])
                same = old.get("subject") == ticket["subject"] and old.get("case_id") in {
                    None,
                    ticket["case_id"],
                }
                if not ticket["subject"]:
                    same &= anonymous_access(old, state)
                if same and old["status"] not in TERMINAL and correspondence_visible(state, old):
                    return old, True
        return ticket, False

    def handoffs(self, state, owner, hid=None, *, include_anonymous=False):
        from .store import Conflict

        with self.tx() as db:
            self._owned(db, state["session_id"], owner)
            self._reconcile_support(db)
            rows = db.execute(
                "SELECT payload FROM handoffs WHERE owner=? ORDER BY updated DESC LIMIT 100", (owner,)
            ).fetchall()
            result = []
            for row in rows:
                ticket = self.unseal(row[0])
                if ticket.get("subject") and not correspondence_visible(state, ticket):
                    continue
                if hid and ticket["id"] != hid:
                    continue
                owned = auth_valid(state, self.settings) and ticket.get("subject") == state["auth"]["subject"]
                if not owned and not (include_anonymous and anonymous_access(ticket, state)):
                    continue
                result.append(public_ticket(ticket, state, self.settings))
            if hid and not result:
                raise Conflict("HANDOFF_NOT_FOUND", 404)
            return result

    def support_tickets(self, state, owner):
        """Turn-local anonymous receipt is usable; it is never an account list."""
        return self.handoffs(state, owner, include_anonymous=True)

    def handoff_update(self, hid, owner, body):
        from .store import Conflict

        with self.tx() as db:
            state = self._expire(db, self._owned(db, body.session_id, owner))
            self._reconcile_support(db)
            row = db.execute("SELECT * FROM handoffs WHERE id=? AND owner=?", (hid, owner)).fetchone()
            if not row:
                raise Conflict("HANDOFF_NOT_FOUND", 404)
            ticket = self.unseal(row["payload"])
            if (
                not auth_valid(state, self.settings)
                or ticket.get("subject") != state["auth"]["subject"]
                or not correspondence_visible(state, ticket)
            ):
                raise Conflict("HANDOFF_NOT_FOUND", 404)
            fp = fingerprint_update(self, body)
            message = (
                f"Request {hid}: "
                + {
                    "cancel": "cancelled.",
                    "note": "your message was added.",
                    "demo_acknowledge": "simulated acknowledgment recorded.",
                    "demo_reply": "simulated reply received.",
                    "demo_close": "simulated closure recorded.",
                }[body.action]
            )
            prior = db.execute(
                "SELECT fingerprint FROM handoff_updates WHERE handoff_id=? AND message_id=?",
                (hid, body.message_id),
            ).fetchone()
            if prior:
                if prior[0] != fp:
                    raise Conflict("IDEMPOTENCY_CONFLICT")
                return {**public_ticket(ticket, state, self.settings), "receipt": message}
            if body.expected_revision != ticket["revision"]:
                raise Conflict("HANDOFF_VERSION_CONFLICT")
            update_ticket(ticket, body.action, body.text, state, self.settings)
            self._save_handoff(db, ticket, owner)
            db.execute("INSERT INTO handoff_updates VALUES(?,?,?)", (hid, body.message_id, fp))
            self._events(
                db,
                state,
                None,
                [event("handoff.updated", handoff_id=hid, action=body.action, result=ticket["status"])],
            )
            state["messages"].append(
                {
                    "role": "system",
                    "text": message,
                    "at": time.time(),
                    "protected": True,
                    "epoch": state["auth_epoch"],
                }
            )
            state["version"] += 1
            # An in-flight model reply cannot overwrite this action receipt.
            db.execute(
                "UPDATE turns SET status='superseded' WHERE turn_id=(SELECT lease_id FROM sessions WHERE id=?)",
                (state["session_id"],),
            )
            db.execute(
                "UPDATE sessions SET state=?,version=?,lease_id=NULL,lease_until=NULL WHERE id=?",
                (self.seal(state), state["version"], state["session_id"]),
            )
            return {**public_ticket(ticket, state, self.settings), "receipt": message}

    def support_availability(self, owner=None):
        return {"channel": "MOCK", "configured": True, "demo_staff_enabled": False, "online_count": 0}

    def _reconcile_support(self, db):
        # Upgrade saved realtime requests to asynchronous requests once.
        for row in db.execute("SELECT * FROM handoffs").fetchall():
            ticket = self.unseal(row["payload"])
            if ticket["channel"] == "LIVE_CHAT" and ticket["status"] not in TERMINAL:
                ticket.update(
                    channel="MOCK",
                    status="SIMULATED_RECORDED",
                    representative=None,
                    revision=ticket["revision"] + 1,
                    updated_at=time.time(),
                )
                ticket["timeline"].append(
                    {
                        "at": time.time(),
                        "actor": "system",
                        "status": ticket["status"],
                        "text": "Converted to asynchronous review. No real representative is contacted in this demo. Continue with AI or wait with message acknowledgments; no response time is promised.",
                    }
                )
                self._save_handoff(db, ticket, row["owner"])

    def append_request_note(self, state, owner, text, unresolved=False, ticket_id=None):
        from .store import Conflict

        tickets = [t for t in self.support_tickets(state, owner) if t["can_update"]]
        if ticket_id:
            tickets = [t for t in tickets if t["id"] == ticket_id]
        preferred = (state.get("handoff") or {}).get("id")
        ticket = next((t for t in tickets if t["id"] == preferred), tickets[0] if tickets else None)
        if not ticket:
            raise Conflict("NO_OPEN_HUMAN_REQUEST", 409)
        # Called inside the turn transaction at commit, not as a separate write.
        state["pending_handoff_note"] = {"id": ticket["id"], "text": text, "unresolved": unresolved}
        return ticket["id"]

    def _commit_request_context(self, db, state, owner):
        # Bind anonymous requests from this conversation after successful verification.
        if state.get("auth"):
            for row in db.execute(
                "SELECT * FROM handoffs WHERE owner=? AND session_id=? AND subject IS NULL",
                (owner, state["session_id"]),
            ).fetchall():
                ticket = self.unseal(row["payload"])
                if not anonymous_access(ticket, state):
                    continue
                ticket["subject"] = state["auth"]["subject"]
                ticket["caller"] = caller_context(state)["caller"]
                ticket["summary"]["verified_identity"] = {
                    "party_id": state["auth"]["subject"],
                    "fields": state["auth"]["fields"],
                }
                ticket["summary"]["verified_at_request"] = False
                ticket["summary"]["verified_now"] = True
                ticket["summary"]["claim_description"] = {k: v["value"] for k, v in state["hints"].items()}
                ticket["revision"] += 1
                self._save_handoff(db, ticket, owner)
                receipt(state, f"Your earlier request {ticket['id']} is now linked to your verified account.")
                if (state.get("handoff") or {}).get("id") == ticket["id"]:
                    state["handoff"] = ticket
            if state["selected_case_id"]:
                for row in db.execute(
                    "SELECT * FROM handoffs WHERE owner=? AND session_id=? AND subject=? AND case_id IS NULL",
                    (owner, state["session_id"], state["auth"]["subject"]),
                ).fetchall():
                    ticket = self.unseal(row["payload"])
                    if ticket["status"] in TERMINAL:
                        continue
                    ticket["case_id"] = state["selected_case_id"]
                    ticket["summary"].update(
                        phase=state["phase"],
                        claim_description={k: v["value"] for k, v in state["hints"].items()},
                        discussion=[
                            m["text"]
                            for m in state["messages"][-6:]
                            if not m.get("protected") or m.get("epoch") == state["auth_epoch"]
                        ],
                        discussed=state["discussed"],
                    )
                    ticket.update(revision=ticket["revision"] + 1, updated_at=time.time())
                    self._save_handoff(db, ticket, owner)
                    receipt(state, f"Request {ticket['id']} is now linked to claim {ticket['case_id']}.")
                    if (state.get("handoff") or {}).get("id") == ticket["id"]:
                        state["handoff"] = ticket
        note = state.pop("pending_handoff_note", None)
        if note:
            row = db.execute(
                "SELECT payload FROM handoffs WHERE id=? AND owner=?", (note["id"], owner)
            ).fetchone()
            ticket = self.unseal(row[0])
            update_ticket(ticket, "note", note["text"], state, self.settings)
            if note["unresolved"]:
                ticket["summary"]["open_questions"] = list(
                    dict.fromkeys(ticket["summary"].get("open_questions", []) + [note["text"]])
                )[-20:]
            self._save_handoff(db, ticket, owner)
            state["handoff"] = ticket
            receipt(state, f"System confirmation: added to {ticket['id']}.")
