import asyncio
import contextlib
import secrets
import sqlite3
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .claims import AccessDenied, FixtureRepository
from .config import ROOT, Settings
from .engine import Engine, public_state
from .interpreter import PROMPT_VERSION, ModelInterpreter, OfflineInterpreter
from .next_steps import next_steps
from .responder import RESPONSE_PROMPT_VERSION
from .schemas import HandoffUpdate, InquiryCreate, InquiryUpdate, TurnRequest
from .state import auth_valid
from .store import Conflict, Store


def create_app(settings=None, interpreter=None):
    settings = settings or Settings()
    mode = settings.mode
    store = Store(settings)
    repo = FixtureRepository()
    engine = Engine(
        settings,
        store,
        repo,
        interpreter or (ModelInterpreter(settings) if mode == "openai" else OfflineInterpreter()),
    )

    @asynccontextmanager
    async def lifespan(app):
        async def maintain():
            counter = 0
            while True:
                await asyncio.sleep(0.5)
                try:
                    store.dispatch()
                    store.dispatch_inquiries()
                    counter += 1
                    if counter % 120 == 0:
                        store.cleanup()
                except sqlite3.Error:
                    # A failed transaction cannot fabricate a send. Retry on the next tick.
                    pass

        task = asyncio.create_task(maintain())
        yield
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    app = FastAPI(title="Insurance SOP Harness", version=__version__, lifespan=lifespan, redoc_url=None)
    app.state.engine, app.state.store, app.state.settings = engine, store, settings

    @app.middleware("http")
    async def protections(request, call_next):
        origin = request.headers.get("origin")
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if origin and urlparse(origin).netloc != request.headers.get("host"):
                return JSONResponse({"error": "ORIGIN_REJECTED"}, status_code=403)
            if request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"error": "ORIGIN_REJECTED"}, status_code=403)
            if int(request.headers.get("content-length", "0")) > 20000:
                return JSONResponse({"error": "REQUEST_TOO_LARGE"}, status_code=413)
        response = await call_next(request)
        response.headers.update(
            {
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Cache-Control": "no-store",
                "X-Frame-Options": "DENY",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
            }
        )
        if request.url.path.startswith("/docs"):
            # FastAPI's Swagger page uses a pinned major-version CDN bundle and an inline initializer.
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                "img-src 'self' data: https://fastapi.tiangolo.com; connect-src 'self'; "
                "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
            )
        return response

    @app.exception_handler(Conflict)
    async def conflict_handler(request, exc):
        return JSONResponse({"error": exc.code}, status_code=exc.status)

    @app.exception_handler(AccessDenied)
    async def access_handler(request, exc):
        return JSONResponse({"error": "ACCESS_DENIED"}, status_code=403)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request, exc):
        return JSONResponse(
            {"error": "INVALID_REQUEST", "fields": [list(e["loc"]) for e in exc.errors()]}, status_code=422
        )

    @app.exception_handler(sqlite3.Error)
    async def database_handler(request, exc):
        return JSONResponse({"error": "STORAGE_UNAVAILABLE"}, status_code=503)

    def owner(request):
        token = request.cookies.get("sop_principal")
        if not token:
            raise HTTPException(401, "Session cookie required")
        return store.fingerprint(token)

    def view(state, principal):
        result = public_state(state, settings)
        result["handoffs"] = store.handoffs(state, principal)
        current = (result.get("handoff") or {}).get("id")
        result["handoff"] = next((h for h in result["handoffs"] if h["id"] == current), None)
        result["support_availability"] = store.support_availability(principal)
        result["available_claims"] = (
            engine.gateway.account(state)["claims"]
            if result["verified"] and not state["selected_case_id"]
            else []
        )
        if result["verified"] and state["selected_case_id"]:
            claim = next(
                (
                    c
                    for c in repo.claims
                    if c["case_id"] == state["selected_case_id"] and c["party_id"] == state["auth"]["subject"]
                ),
                None,
            )
            pending = next(
                (
                    t
                    for t in result["handoffs"]
                    if t["can_update"] and t.get("case_id") in {None, state["selected_case_id"]}
                ),
                None,
            )
            result["next_steps"] = next_steps(
                claim,
                settings.business_date,
                pending,
                representative=bool((state.get("auth") or {}).get("representative")),
            )
        return result

    @app.get("/healthz")
    def health():
        with store.tx() as db:
            db.execute("SELECT 1")
        return {"status": "ok", "profile": "R0", "model_mode": mode, "app_version": __version__}

    @app.get("/v1/config")
    def config():
        return {
            "model_mode": mode,
            "model_name": settings.model_name if mode == "openai" else "Scripted offline parser",
            "prompt_version": PROMPT_VERSION,
            "response_prompt_version": RESPONSE_PROMPT_VERSION,
            "sop_version": "1.1",
            "app_version": __version__,
            "business_date": settings.business_date.isoformat(),
            "historical_clock": bool(settings.demo_as_of),
            "email_mode": "simulated",
            "handoff_mode": "asynchronous_simulated",
            "inquiry_mode": "simulated",
            "fixture_hashes": repo.hashes,
        }

    @app.post("/v1/sessions", status_code=201)
    def create_session(request: Request, response: Response):
        ip = request.client.host if request.client else "local"
        store.rate("create:" + store.fingerprint(ip), 30, 3600)
        token = request.cookies.get("sop_principal") or secrets.token_urlsafe(32)
        response.set_cookie(
            "sop_principal",
            token,
            httponly=True,
            secure=settings.secure_cookie,
            samesite="strict",
            max_age=settings.session_seconds,
            path="/",
        )
        principal = store.fingerprint(token)
        return view(store.create(principal), principal)

    @app.post("/v1/auth/logout")
    def logout(request: Request):
        return store.logout(owner(request))

    @app.get("/v1/sessions/{sid}")
    def get_session(sid: str, request: Request):
        principal = owner(request)
        return view(store.load(sid, principal), principal)

    @app.post("/v1/sessions/{sid}/activity")
    def activity(sid: str, request: Request):
        principal = owner(request)
        store.rate("activity:" + principal, 60, 60)
        state = store.activity(sid, principal)
        return {"verified": public_state(state, settings)["verified"]}

    @app.post("/v1/sessions/{sid}/messages")
    async def message(sid: str, body: TurnRequest, request: Request):
        principal = owner(request)
        store.load(sid, principal)
        store.rate("turn:" + principal, 60, 60)
        if body.action == "message" and not body.text.strip():
            raise Conflict("EMPTY_MESSAGE", 422)
        return view(await engine.turn(sid, principal, body), principal)

    @app.get("/v1/sessions/{sid}/account")
    def account(sid: str, request: Request):
        principal = owner(request)
        state = store.load(sid, principal)
        return {
            **engine.gateway.account(state),
            "emails": store.emails(state, principal),
            "inquiries": store.inquiries(state, principal),
        }

    @app.post("/v1/sessions/{sid}/inquiries", status_code=201)
    def create_inquiry(sid: str, body: InquiryCreate, request: Request):
        principal = owner(request)
        state = store.load(sid, principal)
        account = engine.gateway.account(state)
        if body.case_id and body.case_id not in {c["case_id"] for c in account["claims"]}:
            raise Conflict("CLAIM_NOT_ACCESSIBLE", 403)
        store.rate("inquiry:" + principal, 20, 60)
        return store.create_inquiry(state, principal, body)

    @app.get("/v1/sessions/{sid}/inquiries/{reference}")
    def get_inquiry(sid: str, reference: str, request: Request):
        principal = owner(request)
        return store.inquiries(store.load(sid, principal), principal, reference)[0]

    @app.post("/v1/sessions/{sid}/inquiries/{reference}")
    def update_inquiry(sid: str, reference: str, body: InquiryUpdate, request: Request):
        principal = owner(request)
        store.rate("inquiry:" + principal, 20, 60)
        return store.update_inquiry(store.load(sid, principal), principal, reference, body)

    @app.get("/v1/handoffs/{hid}")
    def handoff(hid: str, session_id: str, request: Request):
        principal = owner(request)
        state = store.load(session_id, principal)
        return store.handoffs(state, principal, hid)[0]

    @app.post("/v1/handoffs/{hid}/updates")
    def update_handoff(hid: str, body: HandoffUpdate, request: Request):
        principal = owner(request)
        store.rate("support:" + principal, 30, 60)
        return store.handoff_update(hid, principal, body)

    @app.post("/v1/handoffs/{hid}/cancel")
    def cancel_handoff(hid: str, body: HandoffUpdate, request: Request):
        if body.action != "cancel":
            raise Conflict("INVALID_REQUEST", 422)
        principal = owner(request)
        store.rate("support:" + principal, 30, 60)
        return store.handoff_update(hid, principal, body)

    @app.get("/v1/sessions/{sid}/trace")
    def trace(sid: str, request: Request):
        state = store.load(sid, owner(request))
        records = store.trace(sid, owner(request))
        records = [r for r in records if r["details"].get("auth_epoch") == state["auth_epoch"]]
        if not auth_valid(state, settings):
            records = [r for r in records if not r["kind"].startswith(("handoff.", "representative."))]
        # Avoid revealing case IDs from an expired grant in historical diagnostics.
        for r in records:
            if (
                not public_state(state, settings)["verified"]
                or r["details"].get("auth_epoch") != state["auth_epoch"]
            ):
                r["details"].pop("case_id", None)
                r["details"].pop("references", None)
        turns = [r for r in records if r["kind"] == "turn.finished"]
        calls = [
            r["details"] for r in records if r["kind"] in {"model.observed", "model.composed", "model.failed"}
        ]
        return {
            "events": records,
            "metrics": {
                "completed_turns": len(turns),
                "model_calls": sum(r.get("calls", 0) for r in calls),
                "input_tokens": sum(r.get("input_tokens", 0) for r in calls),
                "output_tokens": sum(r.get("output_tokens", 0) for r in calls),
                "usage_unknown_calls": sum(
                    max(0, r.get("calls", 0) - r.get("usage_reports", 0)) for r in calls
                ),
                "model_failures": sum(r["kind"] == "model.failed" for r in records),
                "verification_failures": sum(r["kind"] == "verification.failed" for r in records),
                "cost_usd": None,
                "cost_note": "Pricing not configured. Tokens include received usage reports only; missing usage is unknown, not free.",
            },
        }

    @app.get("/")
    def index():
        return FileResponse(ROOT / "app/static/index.html")

    app.mount("/static", StaticFiles(directory=ROOT / "app/static"), name="static")
    return app


app = create_app()
