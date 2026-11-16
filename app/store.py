"""Short SQLite transactions, durable leases, CAS, and a local simulated outbox."""

import hashlib
import hmac
import json
import os
import sqlite3
import time
from contextlib import contextmanager

from cryptography.fernet import Fernet

from .authorization import AuthorizationStore
from .consultation import caller_context, correspondence_visible, migrate_consultation
from .handoff_store import HandoffStore
from .inquiries import InquiryStore
from .state import auth_valid, event, new_state, revoke_auth, uid
from .support import make_ticket


class Conflict(Exception):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


class Store(AuthorizationStore, InquiryStore, HandoffStore):
    def __init__(self, settings):
        self.settings = settings
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(settings.data_dir, 0o700)
        key_path = settings.data_dir / "state.key"
        try:
            fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(Fernet.generate_key())
        except FileExistsError:
            pass
        self.key = key_path.read_bytes()
        self.cipher = Fernet(self.key)
        self.path = settings.data_dir / "harness.sqlite3"
        with self.tx() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS authorizations(owner TEXT PRIMARY KEY, epoch INTEGER NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, owner TEXT NOT NULL, state TEXT NOT NULL, version INTEGER NOT NULL,
                    lease_id TEXT, lease_until REAL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS turns(session_id TEXT NOT NULL, message_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    turn_id TEXT NOT NULL UNIQUE, status TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY(session_id,message_id));
                CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, turn_id TEXT,
                    version INTEGER NOT NULL, kind TEXT NOT NULL, details TEXT NOT NULL, at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY, session_id TEXT NOT NULL, offer_id TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL, status TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS rates(key TEXT PRIMARY KEY, count INTEGER NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS profile_memory(owner TEXT NOT NULL, subject TEXT NOT NULL, payload TEXT NOT NULL, updated REAL NOT NULL,
                    PRIMARY KEY(owner,subject));
                CREATE TABLE IF NOT EXISTS case_memory(owner TEXT NOT NULL, subject TEXT NOT NULL, case_id TEXT NOT NULL, payload TEXT NOT NULL, updated REAL NOT NULL,
                    PRIMARY KEY(owner,subject,case_id));
                CREATE TABLE IF NOT EXISTS handoffs(id TEXT PRIMARY KEY, owner TEXT NOT NULL, subject TEXT, case_id TEXT,
                    session_id TEXT NOT NULL, payload TEXT NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS handoff_updates(handoff_id TEXT NOT NULL, message_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, PRIMARY KEY(handoff_id,message_id));
                CREATE TABLE IF NOT EXISTS inquiries(id TEXT PRIMARY KEY, owner TEXT NOT NULL, subject TEXT NOT NULL,
                    case_id TEXT, payload TEXT NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS inquiry_requests(owner TEXT NOT NULL, message_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, inquiry_id TEXT NOT NULL, PRIMARY KEY(owner,message_id));
            """)
        os.chmod(self.path, 0o600)

    @contextmanager
    def tx(self):
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def seal(self, obj):
        return self.cipher.encrypt(json.dumps(obj, separators=(",", ":")).encode()).decode()

    def unseal(self, value):
        return json.loads(self.cipher.decrypt(value.encode()))

    def fingerprint(self, value):
        return hmac.new(self.key, value.encode(), hashlib.sha256).hexdigest()

    def request_fingerprint(self, request):
        payload = request.model_dump(exclude={"expected_version"})
        if payload.get("target_message_id") is None:
            payload.pop("target_message_id", None)
        if payload.get("form") is None:
            payload.pop("form", None)
        if payload.get("email_note") is None:
            payload.pop("email_note", None)
        return self.fingerprint(json.dumps(payload, sort_keys=True))

    def rate(self, key, limit, seconds):
        now = time.time()
        with self.tx() as db:
            row = db.execute("SELECT * FROM rates WHERE key=?", (key,)).fetchone()
            if row and row["expires"] > now and row["count"] >= limit:
                raise Conflict("RATE_LIMITED", 429)
            count = row["count"] + 1 if row and row["expires"] > now else 1
            expires = row["expires"] if row and row["expires"] > now else now + seconds
            db.execute("INSERT OR REPLACE INTO rates VALUES(?,?,?)", (key, count, expires))

    # Identity-check failures are counted per browser owner, not per conversation, so a
    # new session, a subject change or a logout cannot reset a verification lockout.
    VERIFY_LOCK_SECONDS = 1800
    VERIFY_LOCK_AFTER = 3

    def verify_failures(self, owner):
        with self.tx() as db:
            row = db.execute("SELECT * FROM rates WHERE key=?", ("verify_fail:" + owner,)).fetchone()
        return row["count"] if row and row["expires"] > time.time() else 0

    def record_verify_failure(self, owner):
        now, key = time.time(), "verify_fail:" + owner
        with self.tx() as db:
            row = db.execute("SELECT * FROM rates WHERE key=?", (key,)).fetchone()
            count = row["count"] + 1 if row and row["expires"] > now else 1
            db.execute(
                "INSERT OR REPLACE INTO rates VALUES(?,?,?)", (key, count, now + self.VERIFY_LOCK_SECONDS)
            )
        return count

    def clear_verify_failures(self, owner):
        with self.tx() as db:
            db.execute("DELETE FROM rates WHERE key=?", ("verify_fail:" + owner,))

    def create(self, owner):
        state = new_state()
        with self.tx() as db:
            self._sync_authorization(db, state, owner, [])
            self._welcome_authorized(db, state, owner)
            db.execute(
                "INSERT INTO sessions VALUES(?,?,?,?,NULL,NULL,?)",
                (state["session_id"], owner, self.seal(state), 0, time.time()),
            )
            self._events(db, state, None, [event("session.created", profile="R0")])
        return state

    def _owned(self, db, sid, owner):
        row = db.execute("SELECT * FROM sessions WHERE id=? AND owner=?", (sid, owner)).fetchone()
        if not row:
            raise Conflict("SESSION_NOT_FOUND", 404)
        return row

    def _events(self, db, state, turn_id, events):
        for e in events:
            db.execute(
                "INSERT INTO audit(session_id,turn_id,version,kind,details,at) VALUES(?,?,?,?,?,?)",
                (
                    state["session_id"],
                    turn_id,
                    state["version"],
                    e["kind"],
                    json.dumps({**e["details"], "auth_epoch": state["auth_epoch"]}),
                    e["at"],
                ),
            )

    def _cancel_invalid(self, db, state):
        offer_id = state["draft"]["id"] if state.get("draft") else ""
        db.execute(
            "UPDATE outbox SET status='CANCELLED',updated=? WHERE session_id=? AND status='QUEUED' AND offer_id!=?",
            (time.time(), state["session_id"], offer_id),
        )

    def _expire(self, db, row):
        state = self.unseal(row["state"])
        events = []
        self._sync_authorization(db, state, row["owner"], events)
        migrate_consultation(state, events)
        now = time.time()
        if state["lifecycle"] == "HANDOFF_PENDING":
            # Migrate existing demos without requiring the customer to reset.
            legacy = state.get("handoff") or {}
            ticket = make_ticket(state, legacy.get("reason", "USER_REQUEST"), self.settings)
            ticket["id"] = legacy.get("id", ticket["id"])
            self._save_handoff(db, ticket, row["owner"])
            state["handoff"] = ticket
            state["lifecycle"] = "ACTIVE"
            events.append(event("handoff.chat_restored", handoff_id=ticket["id"]))
        if now - row["created"] >= self.settings.session_seconds and state["lifecycle"] != "EXPIRED":
            revoke_auth(state, events, "SESSION_EXPIRED")
            state["lifecycle"] = "EXPIRED"
            state["messages"] = []
        elif state["auth"] and not auth_valid(state, self.settings, now):
            revoke_auth(state, events, "AUTH_EXPIRED", preserve_hints=True)
            state["messages"].append(
                {
                    "role": "assistant",
                    "text": "Your identity verification has expired. Please verify again to view claim details.",
                    "protected": False,
                    "epoch": state["auth_epoch"],
                }
            )
        if events:
            state["version"] += 1
            if row["lease_id"]:
                db.execute("UPDATE turns SET status='superseded' WHERE turn_id=?", (row["lease_id"],))
            db.execute(
                "UPDATE sessions SET state=?,version=?,lease_id=NULL,lease_until=NULL WHERE id=?",
                (self.seal(state), state["version"], state["session_id"]),
            )
            self._cancel_invalid(db, state)
            self._events(db, state, None, events)
        return state

    def emails(self, state, owner):
        if not auth_valid(state, self.settings):
            return []
        with self.tx() as db:
            self._owned(db, state["session_id"], owner)
            rows = db.execute(
                "SELECT o.* FROM outbox o JOIN sessions s ON s.id=o.session_id WHERE s.owner=? ORDER BY o.updated DESC LIMIT 50",
                (owner,),
            ).fetchall()
            result = []
            for row in rows:
                payload = self.unseal(row["payload"])
                if payload.get("subject_party_id") == state["auth"]["subject"] and correspondence_visible(
                    state, payload
                ):
                    result.append(
                        {
                            **{k: payload[k] for k in ("id", "subject", "body", "case_id")},
                            "status": row["status"],
                            "updated_at": row["updated"],
                        }
                    )
            return result

    def load(self, sid, owner):
        with self.tx() as db:
            return self._expire(db, self._owned(db, sid, owner))

    def activity(self, sid, owner):
        """Share the grant across explicit portal actions; polling never renews it."""
        with self.tx() as db:
            state = self._expire(db, self._owned(db, sid, owner))
            if auth_valid(state, self.settings):
                state["auth"]["last_used_at"] = time.time()
                db.execute(
                    "UPDATE authorizations SET payload=? WHERE owner=?", (self.seal(state["auth"]), owner)
                )
                db.execute("UPDATE sessions SET state=? WHERE id=?", (self.seal(state), sid))
            return state

    def begin(self, sid, owner, request):
        fingerprint = self.request_fingerprint(request)
        with self.tx() as db:
            row = self._owned(db, sid, owner)
            state = self._expire(db, row)
            row = self._owned(db, sid, owner)
            prior = db.execute(
                "SELECT * FROM turns WHERE session_id=? AND message_id=?", (sid, request.message_id)
            ).fetchone()
            if prior:
                if prior["status"] == "superseded" and not prior["fingerprint"]:
                    raise Conflict("TURN_SUPERSEDED")
                if prior["fingerprint"] != fingerprint:
                    raise Conflict("IDEMPOTENCY_CONFLICT")
                if prior["status"] == "completed":
                    return state, prior["turn_id"], True
                if prior["status"] == "superseded":
                    raise Conflict("TURN_SUPERSEDED")
                if row["lease_until"] and row["lease_until"] > time.time():
                    raise Conflict("TURN_IN_PROGRESS")
                db.execute("UPDATE turns SET status='running' WHERE turn_id=?", (prior["turn_id"],))
                turn_id = prior["turn_id"]
            else:
                if request.expected_version != state["version"]:
                    raise Conflict("VERSION_CONFLICT")
                if row["lease_id"] and row["lease_until"] > time.time():
                    raise Conflict("TURN_IN_PROGRESS")
                if row["lease_id"]:
                    db.execute("UPDATE turns SET status='superseded' WHERE turn_id=?", (row["lease_id"],))
                turn_id = uid("t")
                db.execute(
                    "INSERT INTO turns VALUES(?,?,?,?,?,?)",
                    (sid, request.message_id, fingerprint, turn_id, "running", time.time()),
                )
            db.execute(
                "UPDATE sessions SET lease_id=?,lease_until=? WHERE id=?",
                (turn_id, time.time() + self.settings.lease_seconds, sid),
            )
            self._events(db, state, turn_id, [event("turn.accepted")])
            return state, turn_id, False

    def assert_current(self, sid, owner, turn_id):
        with self.tx() as db:
            row = self._owned(db, sid, owner)
            self._expire(db, row)
            row = self._owned(db, sid, owner)
            if row["lease_id"] != turn_id or not row["lease_until"] or row["lease_until"] < time.time():
                raise Conflict("TURN_SUPERSEDED")

    def _memory_owner(self, state, owner):
        context = caller_context(state)
        return self.fingerprint(
            json.dumps(
                [
                    owner,
                    context["caller"],
                    state["auth"]["subject"],
                    state["episode_id"],
                    state["auth_epoch"],
                ],
                sort_keys=True,
            )
        )

    def memories(self, state, owner, case_id=None):
        with self.tx() as db:
            row = self._owned(db, state["session_id"], owner)
            durable = self.unseal(row["state"])
            if (
                not auth_valid(durable, self.settings)
                or not auth_valid(state, self.settings)
                or durable["auth"]["subject"] != state["auth"]["subject"]
                or durable["auth_epoch"] != state["auth_epoch"]
            ):
                raise Conflict("MEMORY_REQUIRES_VERIFICATION", 403)
            subject = durable["auth"]["subject"]
            owner = self._memory_owner(state, owner)
            cutoff = time.time() - self.settings.session_seconds
            profile = db.execute(
                "SELECT payload FROM profile_memory WHERE owner=? AND subject=? AND updated>?",
                (owner, subject, cutoff),
            ).fetchone()
            case = (
                db.execute(
                    "SELECT payload FROM case_memory WHERE owner=? AND subject=? AND case_id=? AND updated>?",
                    (owner, subject, case_id, cutoff),
                ).fetchone()
                if case_id
                else None
            )
            return self.unseal(profile[0]) if profile else {}, self.unseal(case[0]) if case else {}

    def _remember(self, db, state, owner, turn_id):
        if (
            not auth_valid(state, self.settings)
            or not state["selected_case_id"]
            or state["messages"][-1].get("case_id") != state["selected_case_id"]
        ):
            return
        subject, case_id = state["auth"]["subject"], state["selected_case_id"]
        owner = self._memory_owner(state, owner)
        cutoff = time.time() - self.settings.session_seconds
        prior = db.execute(
            "SELECT payload FROM profile_memory WHERE owner=? AND subject=? AND updated>?",
            (owner, subject, cutoff),
        ).fetchone()
        profile = self.unseal(prior[0]) if prior else {}
        profile["recent_cases"] = [case_id] + [c for c in profile.get("recent_cases", []) if c != case_id][:9]
        profile["refused_fields"] = list(
            dict.fromkeys(profile.get("refused_fields", []) + state["refused_fields"])
        )
        prior = db.execute(
            "SELECT payload FROM case_memory WHERE owner=? AND subject=? AND case_id=? AND updated>?",
            (owner, subject, case_id, cutoff),
        ).fetchone()
        memory = self.unseal(prior[0]) if prior else {}
        dialogue = [t for t in memory.get("dialogue", []) if t["turn_id"] != turn_id]
        dialogue.append(
            {
                "turn_id": turn_id,
                "user": state["messages"][-2]["text"],
                "assistant": state["messages"][-1]["text"],
                "at": time.time(),
            }
        )
        memory.update(
            case_id=case_id,
            dialogue=dialogue[-8:],
            discussed=state["discussed"],
            last_answer_topics=state.get("last_answer_topics", []),
            open_questions=state["open_questions"],
            previous_guidance=[r["text"] for r in state["evidence"] if r["kind"] == "guidance"][-5:],
            updated_at=time.time(),
        )
        db.execute(
            "INSERT OR REPLACE INTO profile_memory VALUES(?,?,?,?)",
            (owner, subject, self.seal(profile), time.time()),
        )
        db.execute(
            "INSERT OR REPLACE INTO case_memory VALUES(?,?,?,?,?)",
            (owner, subject, case_id, self.seal(memory), time.time()),
        )
        state["profile_memory"], state["case_memory"] = profile, memory

    def commit(self, state, turn_id, events, final=False, operation=None):
        with self.tx() as db:
            row = db.execute("SELECT * FROM sessions WHERE id=?", (state["session_id"],)).fetchone()
            if (
                not row
                or row["lease_id"] != turn_id
                or row["version"] != state["version"]
                or row["lease_until"] < time.time()
            ):
                raise Conflict("TURN_SUPERSEDED")
            self._commit_authorization(db, state, row, events)
            if state["auth"] and not auth_valid(state, self.settings):
                raise Conflict("AUTH_EXPIRED")
            if any(e["kind"] == "handoff.recorded" for e in events):
                ticket = state["handoff"]
                # Reusing an open request never overwrites concurrently received feedback.
                if not db.execute("SELECT 1 FROM handoffs WHERE id=?", (ticket["id"],)).fetchone():
                    self._save_handoff(db, ticket, row["owner"])
            self._commit_request_context(db, state, row["owner"])
            if final:
                # Receipts are appended AFTER model prose and the associated writes,
                # in the same transaction. A model cannot hide a committed action.
                for text in state.pop("pending_receipts", []):
                    if text not in state["messages"][-1]["text"]:
                        state["messages"][-1]["text"] += "\n\n" + text
                if not any(e["kind"] in {"model.failed", "turn.failed"} for e in events):
                    self._remember(db, state, row["owner"], turn_id)
            state["version"] += 1
            db.execute(
                "UPDATE sessions SET state=?,version=?,lease_id=?,lease_until=? WHERE id=?",
                (
                    self.seal(state),
                    state["version"],
                    None if final else turn_id,
                    None if final else row["lease_until"],
                    state["session_id"],
                ),
            )
            self._cancel_invalid(db, state)
            if operation:
                db.execute(
                    "INSERT INTO outbox VALUES(?,?,?,?,?,?,?)",
                    (
                        operation["id"],
                        state["session_id"],
                        operation["offer_id"],
                        self.seal(operation),
                        "QUEUED",
                        time.time(),
                        time.time(),
                    ),
                )
            self._events(db, state, turn_id, events)
            if final:
                db.execute("UPDATE turns SET status='completed' WHERE turn_id=?", (turn_id,))

    def control(self, sid, owner, request):
        # Controls fence a specific in-flight input, even after an interim commit.
        with self.tx() as db:
            row = self._owned(db, sid, owner)
            old = self._expire(db, row)
            row = self._owned(db, sid, owner)
            fingerprint = self.request_fingerprint(request)
            prior = db.execute(
                "SELECT * FROM turns WHERE session_id=? AND message_id=?", (sid, request.message_id)
            ).fetchone()
            if prior:
                if prior["fingerprint"] != fingerprint:
                    raise Conflict("IDEMPOTENCY_CONFLICT")
                return old
            target = None
            if request.target_message_id:
                if request.target_message_id == request.message_id:
                    raise Conflict("INVALID_INTERRUPT_TARGET", 422)
                target = db.execute(
                    "SELECT * FROM turns WHERE session_id=? AND message_id=?",
                    (sid, request.target_message_id),
                ).fetchone()
                if row["lease_id"] and (not target or target["turn_id"] != row["lease_id"]):
                    raise Conflict("INTERRUPT_TARGET_CHANGED")
                if target and target["status"] == "superseded":
                    raise Conflict("INTERRUPT_TARGET_CHANGED")
            active_target = bool(target and row["lease_id"] == target["turn_id"])
            if request.expected_version != old["version"] and not active_target:
                raise Conflict("VERSION_CONFLICT")
            if request.target_message_id and not target:
                # An interrupt may arrive before the original HTTP request.
                # This tombstone prevents that late request from starting work.
                db.execute(
                    "INSERT INTO turns VALUES(?,?,?,?,?,?)",
                    (sid, request.target_message_id, "", uid("t"), "superseded", time.time()),
                )
            events = []
            if request.action == "reset":
                state = new_state(sid, old["version"] + 1, old["auth_epoch"])
                self._sync_authorization(db, state, owner, events)
                self._welcome_authorized(db, state, owner)
                events.append(event("session.reset"))
            else:
                state = old
                state["version"] += 1
                if request.action == "interrupt":
                    if state.get("draft") and state["lifecycle"] == "ACTIVE":
                        state["draft"]["paused"] = True
                        events.append(event("summary.paused", reason="USER_INTERRUPTED"))
                    text = (
                        "I’ve paused here. Your confirmed progress is saved. Send your correction or next question when you’re ready. Any pending summary email is paused."
                        if state["lifecycle"] == "ACTIVE"
                        else "There is no active reply to stop. This consultation has already ended; completed operations cannot be recalled."
                    )
                    state["messages"].append(
                        {
                            "role": "assistant",
                            "text": text,
                            "protected": bool(state["auth"]),
                            "epoch": state["auth_epoch"],
                        }
                    )
                    events.append(event("turn.interrupted", target_message_id=request.target_message_id))
                elif request.action == "cancel":
                    revoke_auth(state, events, "USER_CANCELLED")
                    state["lifecycle"] = "CANCELLED"
                    state["identity"] = {}
                    state["messages"].append(
                        {
                            "role": "assistant",
                            "text": "This session has ended. No pending email will be sent.",
                            "protected": False,
                            "epoch": state["auth_epoch"],
                        }
                    )
                elif request.action == "email_cancel":
                    op = state.get("email_operation")
                    if op and op["status"] == "SIMULATED_RECORDED":
                        text = "The simulated email was already recorded. No real email was sent; the existing record cannot be recalled."
                    else:
                        state["draft"] = None
                        if op:
                            op["status"] = "CANCELLED"
                        state["email_preference"] = "skip"
                        text = "The pending email has been cancelled. No email will be sent."
                    state["messages"].append(
                        {"role": "assistant", "text": text, "protected": False, "epoch": state["auth_epoch"]}
                    )
                    events.append(event("summary.revoked"))
            self._commit_authorization(db, state, row, events)
            if row["lease_id"]:
                db.execute("UPDATE turns SET status='superseded' WHERE turn_id=?", (row["lease_id"],))
            turn_id = uid("t")
            db.execute(
                "INSERT INTO turns VALUES(?,?,?,?,?,?)",
                (sid, request.message_id, fingerprint, turn_id, "completed", time.time()),
            )
            db.execute(
                "UPDATE sessions SET state=?,version=?,lease_id=NULL,lease_until=NULL,created=? WHERE id=?",
                (self.seal(state), state["version"], state["created_at"], sid),
            )
            self._cancel_invalid(db, state)
            self._events(db, state, turn_id, events)
            return state

    def trace(self, sid, owner):
        state = self.load(sid, owner)
        with self.tx() as db:
            return [
                {**dict(r), "details": json.loads(r["details"])}
                for r in db.execute(
                    "SELECT id,turn_id,version,kind,details,at FROM audit WHERE session_id=? AND at>=? ORDER BY id",
                    (sid, state.get("privacy_boundary_at", 0)),
                ).fetchall()
            ]

    def dispatch(self):
        """R0 has no external send. Recording the simulated result is fully transactional."""
        with self.tx() as db:
            rows = db.execute(
                "SELECT * FROM outbox WHERE status='QUEUED' AND created<?",
                (time.time() - self.settings.email_delay,),
            ).fetchall()
            for op in rows:
                row = db.execute("SELECT * FROM sessions WHERE id=?", (op["session_id"],)).fetchone()
                if not row:
                    continue
                if row["lease_id"]:
                    continue  # Any accepted input pauses dispatch, including failed interpretation.
                state = self._expire(db, row)
                draft = state.get("draft")
                payload = self.unseal(op["payload"])
                valid = (
                    state["phase"] == "POST_PROCESS"
                    and state["lifecycle"] == "ACTIVE"
                    and auth_valid(state, self.settings)
                    and draft
                    and draft["id"] == op["offer_id"]
                    and draft["hash"] == payload["hash"]
                    and draft["recipient"] == payload["recipient"]
                    and draft["epoch"] == state["auth_epoch"]
                    and draft["episode_version"] == state["episode_version"]
                    and draft["expires_at"] > time.time()
                    and draft.get("consent") is True
                    and not draft.get("paused")
                )
                if not valid:
                    if draft and draft.get("paused"):
                        continue
                    db.execute(
                        "UPDATE outbox SET status='CANCELLED',updated=? WHERE id=?", (time.time(), op["id"])
                    )
                    if (state.get("email_operation") or {}).get("id") == op["id"]:
                        state["email_operation"]["status"] = "CANCELLED"
                    else:
                        continue
                else:
                    db.execute(
                        "UPDATE outbox SET status='SIMULATED_RECORDED',updated=? WHERE id=?",
                        (time.time(), op["id"]),
                    )
                    state["email_operation"]["status"] = "SIMULATED_RECORDED"
                    state["lifecycle"] = "COMPLETED"
                    state["identity"] = {}
                    state["messages"].append(
                        {
                            "role": "assistant",
                            "text": "Your summary email is recorded in the demo outbox. No real email was sent. You can inspect the saved summary below. Thank you for chatting with me.",
                            "protected": True,
                            "epoch": state["auth_epoch"],
                        }
                    )
                state["version"] += 1
                db.execute(
                    "UPDATE sessions SET state=?,version=? WHERE id=?",
                    (self.seal(state), state["version"], state["session_id"]),
                )
                self._events(
                    db,
                    state,
                    None,
                    [
                        event(
                            "operation.result",
                            operation_id=op["id"],
                            result=state["email_operation"]["status"],
                        )
                    ],
                )

    def cleanup(self):
        with self.tx() as db:
            stale = [
                r[0]
                for r in db.execute(
                    "SELECT id FROM sessions WHERE created<?", (time.time() - self.settings.session_seconds,)
                )
            ]
            for sid in stale:
                db.execute("DELETE FROM sessions WHERE id=?", (sid,))
                db.execute("DELETE FROM turns WHERE session_id=?", (sid,))
                db.execute("DELETE FROM outbox WHERE session_id=?", (sid,))
            db.execute("DELETE FROM audit WHERE at<?", (time.time() - 7 * 86400,))
            db.execute("DELETE FROM rates WHERE expires<?", (time.time(),))
            db.execute(
                "DELETE FROM inquiry_requests WHERE inquiry_id IN (SELECT id FROM inquiries WHERE updated<?)",
                (time.time() - 7 * 86400,),
            )
            db.execute("DELETE FROM inquiries WHERE updated<?", (time.time() - 7 * 86400,))
            db.execute(
                "DELETE FROM handoff_updates WHERE handoff_id IN (SELECT id FROM handoffs WHERE updated<?)",
                (time.time() - 7 * 86400,),
            )
            db.execute("DELETE FROM handoffs WHERE updated<?", (time.time() - 7 * 86400,))
            for table in ("profile_memory", "case_memory"):
                db.execute(
                    f"DELETE FROM {table} WHERE updated<?", (time.time() - self.settings.session_seconds,)
                )
