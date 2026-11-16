"""Cookie-owner authorization, independent of individual conversations.

The HttpOnly principal cookie is an opaque bearer; party, fields and epoch stay
in SQLite. Every read/commit reconciles the shared grant, including late turns.
"""

import copy

from .consultation import correspondence_visible
from .state import WELCOME, auth_valid, event, revoke_auth


class AuthorizationStore:
    def _authorization(self, db, owner):
        row = db.execute("SELECT * FROM authorizations WHERE owner=?", (owner,)).fetchone()
        if row is None:
            # One-time upgrade of existing verified browsers. A revoked row is
            # retained, so an old conversation can never resurrect a grant.
            grant, epoch = None, 0
            for session in db.execute(
                "SELECT state FROM sessions WHERE owner=? ORDER BY created DESC", (owner,)
            ):
                old = self.unseal(session[0])
                epoch = max(epoch, old["auth_epoch"])
                if grant is None and auth_valid(old, self.settings):
                    grant = old["auth"]
            if grant:
                grant["epoch"] = epoch
            db.execute("INSERT INTO authorizations VALUES(?,?,?)", (owner, epoch, self.seal(grant)))
            return epoch, grant
        epoch, grant = row["epoch"], self.unseal(row["payload"])
        if grant and "caller" not in grant and grant.get("representative"):
            rep = grant["representative"]
            grant["caller"] = {"role": "representative", "name": rep["caller"]}
            grant["data_subject"] = {
                "party_id": grant["subject"],
                "name": (rep.get("relationship") or {}).get("buyer_name", "the policyholder"),
            }
            db.execute("UPDATE authorizations SET payload=? WHERE owner=?", (self.seal(grant), owner))
        if grant and not auth_valid({"auth": grant, "auth_epoch": epoch}, self.settings):
            epoch, grant = epoch + 1, None
            db.execute(
                "UPDATE authorizations SET epoch=?,payload=? WHERE owner=?", (epoch, self.seal(None), owner)
            )
        return epoch, grant

    def _sync_authorization(self, db, state, owner, events):
        epoch, grant = self._authorization(db, owner)
        old = state.get("auth")
        changed = (
            state["auth_epoch"] != epoch
            or bool(old) != bool(grant)
            or (old and grant and old["subject"] != grant["subject"])
        )
        if changed:
            if old or state["auth_epoch"] != epoch:
                revoke_auth(state, events, "SHARED_AUTHORIZATION_CHANGED")
            state["auth_epoch"] = epoch
            state["auth"] = copy.deepcopy(grant)
            state["representative"] = copy.deepcopy(grant.get("representative")) if grant else None
            if grant:
                state["identity"] = {}
                state["verification_locked"] = False
                state["verification_attempts"] = 0
                state["phase"] = "RESOLVE_INTENT"
                state["visited_phases"] = list(dict.fromkeys(state["visited_phases"] + ["RESOLVE_INTENT"]))
                events.append(event("authorization.reused", party_id=grant["subject"]))
            else:
                state["messages"] = [
                    {
                        "role": "assistant",
                        "text": "You are signed out or your authorization has expired. " + WELCOME,
                        "protected": False,
                        "epoch": epoch,
                    }
                ]
                events.append(event("authorization.required"))
        elif grant:
            state["auth"] = copy.deepcopy(grant)
        return changed

    def _commit_authorization(self, db, state, row, events):
        from .store import Conflict

        epoch, shared = self._authorization(db, row["owner"])
        durable = self.unseal(row["state"])
        if durable["auth_epoch"] != epoch:
            raise Conflict("AUTHORIZATION_REVOKED", 403)
        if any(e["kind"] == "auth.revoked" for e in events):
            epoch += 1
            state["auth_epoch"] = epoch
            if state["auth"]:
                state["auth"]["epoch"] = epoch
            shared = state["auth"]
        elif any(e["kind"] == "verification.passed" for e in events):
            shared = state["auth"]
        elif state["auth"]:
            if not shared or state["auth"]["subject"] != shared["subject"]:
                raise Conflict("AUTHORIZATION_REVOKED", 403)
            shared["last_used_at"] = max(shared["last_used_at"], state["auth"]["last_used_at"])
            state["auth"] = copy.deepcopy(shared)
        db.execute(
            "UPDATE authorizations SET epoch=?,payload=? WHERE owner=?",
            (epoch, self.seal(shared), row["owner"]),
        )

    def logout(self, owner):
        with self.tx() as db:
            epoch, _ = self._authorization(db, owner)
            db.execute(
                "UPDATE authorizations SET epoch=?,payload=? WHERE owner=?",
                (epoch + 1, self.seal(None), owner),
            )
            # Immediately fence every conversation and queued outbound operation.
            for row in db.execute("SELECT * FROM sessions WHERE owner=?", (owner,)).fetchall():
                self._expire(db, row)
        return {"verified": False, "authorization_epoch": epoch + 1}

    def _welcome_authorized(self, db, state, owner):
        if not state.get("auth"):
            return
        rep = state.get("representative")
        greeting = (
            f"{rep['caller']}, your representative authorization is still active. Choose the policyholder’s claim below."
            if rep
            else "Your identity is still verified. Your account’s claims are listed below; choose one to continue."
        )
        state["messages"] = [
            {
                "role": "assistant",
                "text": "You are with the AI assistant. " + greeting,
                "protected": True,
                "epoch": state["auth_epoch"],
            }
        ]
        for row in db.execute("SELECT payload FROM handoffs WHERE owner=? ORDER BY updated DESC", (owner,)):
            ticket = self.unseal(row[0])
            if (
                ticket.get("subject") == state["auth"]["subject"]
                and correspondence_visible(state, ticket)
                and ticket["status"]
                not in {
                    "CANCELLED",
                    "CLOSED",
                    "SIMULATED_CLOSED",
                }
            ):
                state["handoff"] = ticket
                state["messages"][0]["text"] += (
                    f" Your request {ticket['id']} is still awaiting handling. View updates or cancel it in Human requests. This is simulated; no real representative is contacted."
                )
                break
