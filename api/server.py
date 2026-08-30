"""Backend API layer -- BUILD_SPEC.md section 8. Thin wrapper over Shelf/Parity/Guardrail;
serves the frontend as static files at "/" and the API under "/api/*".

Run: uvicorn api.server:app --reload --port 8000
"""
import os
import sys

from dotenv import load_dotenv

# Must run before importing anything below -- api.auth, guardrail.guardrail, etc. all read
# os.environ.get(...) as module-level constants at import time, so .env has to be loaded
# into the process environment first or those imports silently fall back to insecure defaults.
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from api.routes import allowance_subscriptions, audit, auth, bank_statement, catalog, chat, customer_auth, disputes, live_verification, mandates, purchase, settlement_qa, support, webhooks

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")

app = FastAPI(title="Shelf + Parity + Guardrail API")

# The frontend is served by this same app at "/", so it never actually needs cross-origin
# access -- same-origin requests bypass CORS entirely. Scoped to localhost/127.0.0.1 (the only
# origins this app is ever actually served from) rather than "*", which is unnecessary and,
# while not currently exploitable here (allow_credentials is never set to True alongside it),
# is a wide-open default not worth carrying if this ever becomes a template for something else.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8000", "http://127.0.0.1:8000"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat.router)
app.include_router(mandates.router)
app.include_router(audit.router)
app.include_router(catalog.router)
app.include_router(auth.router)
app.include_router(customer_auth.router)
app.include_router(purchase.router)
app.include_router(settlement_qa.router)
app.include_router(live_verification.router)
app.include_router(disputes.router)
app.include_router(allowance_subscriptions.router)
app.include_router(bank_statement.router)
app.include_router(support.router)
app.include_router(webhooks.router)


@app.middleware("http")
async def no_cache_static(request, call_next):
    """Prevents the browser from serving a stale cached frontend after edits -- this bit us
    once already (app.js kept 304'ing against an old cached copy after a fix)."""
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
