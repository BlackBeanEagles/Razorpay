"""POST /api/auth/login, POST /api/auth/logout, GET /api/auth/me -- admin session management
for the merchant/audit dashboard (see api/auth.py for the hashing/signing mechanics)."""
import os
import sys

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from api import auth
from api import rate_limit
from api import session_revocation

router = APIRouter()


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/api/auth/login")
def login(body: LoginRequest, request: Request, response: Response):
    rate_key = f"admin:{body.username}"
    locked_for = rate_limit.seconds_until_unlocked(rate_key)
    if locked_for > 0:
        raise HTTPException(status_code=429, detail=f"Too many failed attempts -- try again in {int(locked_for)}s.")

    if not auth.verify_credentials(body.username, body.password):
        rate_limit.record_failure(rate_key)
        raise HTTPException(status_code=401, detail="Invalid username or password.")
    rate_limit.record_success(rate_key)

    token = auth.create_session_token(body.username)
    response.set_cookie(
        key=auth.SESSION_COOKIE_NAME,
        value=token,
        max_age=auth.SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )
    return {"ok": True, "username": body.username}


@router.post("/api/auth/logout")
def logout(request: Request, response: Response):
    # Deleting the cookie alone is cosmetic -- the token itself (if captured before logout,
    # e.g. from a shared computer) would otherwise stay fully valid until its natural expiry.
    # Revoking its jti server-side means it's actually rejected on the next request, not just
    # forgotten by this one browser.
    token = request.cookies.get(auth.SESSION_COOKIE_NAME)
    if token:
        data = auth.decode_session_token(token)
        if data is not None:
            session_revocation.revoke(data.get("jti"), data.get("expires_at", 0))
    response.delete_cookie(auth.SESSION_COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/api/auth/me")
def me(request: Request):
    token = request.cookies.get(auth.SESSION_COOKIE_NAME)
    if not token:
        return {"authenticated": False}
    data = auth.decode_session_token(token)
    if data is None:
        return {"authenticated": False}
    return {"authenticated": True, "username": data["username"]}
