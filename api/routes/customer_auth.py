"""POST /api/customer/signup, /login, /logout, GET /api/customer/me -- shopper accounts.
Required before the AI agent can execute a purchase (see chat.py's require_customer dependency)."""
import os
import sys

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from api import customer_auth
from api import rate_limit
from api import session_revocation

router = APIRouter()


class SignupRequest(BaseModel):
    username: str = Field(min_length=3, max_length=40)
    password: str = Field(min_length=6, max_length=100)
    name: str = Field(min_length=1, max_length=80)
    # Plain str + a basic shape check, not pydantic's EmailStr -- that needs the optional
    # email-validator package, not already a dependency here. Good enough to catch an obvious
    # typo without adding a new library for one field.
    email: str = Field(min_length=5, max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class LoginRequest(BaseModel):
    username: str
    password: str


def _set_session_cookie(response: Response, request: Request, username: str, customer_id: str):
    token = customer_auth.create_session_token(username, customer_id)
    response.set_cookie(
        key=customer_auth.SESSION_COOKIE_NAME,
        value=token,
        max_age=customer_auth.SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )


@router.post("/api/customer/signup")
def signup(body: SignupRequest, request: Request, response: Response):
    try:
        result = customer_auth.signup(body.username, body.password, body.name, body.email)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    _set_session_cookie(response, request, result["username"], result["customer_id"])
    return result


@router.post("/api/customer/login")
def login(body: LoginRequest, request: Request, response: Response):
    rate_key = f"customer:{body.username}"
    locked_for = rate_limit.seconds_until_unlocked(rate_key)
    if locked_for > 0:
        raise HTTPException(status_code=429, detail=f"Too many failed attempts -- try again in {int(locked_for)}s.")

    account = customer_auth.verify_login(body.username, body.password)
    if account is None:
        rate_limit.record_failure(rate_key)
        raise HTTPException(status_code=401, detail="Invalid username or password.")
    rate_limit.record_success(rate_key)

    _set_session_cookie(response, request, body.username, account["customer_id"])
    return {"customer_id": account["customer_id"], "username": body.username, "name": account["name"]}


@router.post("/api/customer/logout")
def logout(request: Request, response: Response):
    # Deleting the cookie alone is cosmetic -- see api/routes/auth.py's matching logout for why
    # this actually revokes the session server-side instead.
    token = request.cookies.get(customer_auth.SESSION_COOKIE_NAME)
    if token:
        data = customer_auth.decode_session_token(token)
        if data is not None:
            session_revocation.revoke(data.get("jti"), data.get("expires_at", 0))
    response.delete_cookie(customer_auth.SESSION_COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/api/customer/me")
def me(request: Request):
    token = request.cookies.get(customer_auth.SESSION_COOKIE_NAME)
    if not token:
        return {"authenticated": False}
    data = customer_auth.decode_session_token(token)
    if data is None:
        return {"authenticated": False}
    return {"authenticated": True, "customer_id": data["customer_id"], "username": data["username"]}
