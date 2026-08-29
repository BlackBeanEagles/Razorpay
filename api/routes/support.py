"""POST /api/support-request (customer, files a request) and GET /api/support-requests +
POST /api/support-requests/{id}/resolve (admin, reviews them). See audit/support_log.py."""
import os
import sys

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from audit.support_log import log_support_request, read_all, mark_resolved, update_confirmation_email, update_resolution_email
from audit.mailer import send_email, support_request_confirmation, support_request_resolved
from api.customer_auth import require_customer, email_for_customer
from api.auth import require_admin

router = APIRouter()


class SupportRequest(BaseModel):
    component: str = "general"  # "general" covers the checkout-page contact form -- not every
    # request is reacting to a specific blocked/flagged feature outcome; "can be related to
    # anything" is the whole point of that entry point.
    related_summary: str = ""
    message: str


@router.post("/api/support-request")
def file_support_request(body: SupportRequest, customer_id: str = Depends(require_customer)):
    entry = log_support_request(customer_id, body.component, body.related_summary, body.message)
    to_email = email_for_customer(customer_id)
    subject, email_body = support_request_confirmation(entry["request_id"], body.message)
    email_result = send_email(to_email, subject, email_body)
    update_confirmation_email(entry["request_id"], email_result)
    entry["confirmation_email"] = email_result
    return entry


@router.get("/api/support-requests")
def list_support_requests(_admin: str = Depends(require_admin)):
    return list(reversed(read_all()))


@router.post("/api/support-requests/{request_id}/resolve")
def resolve_support_request(request_id: str, _admin: str = Depends(require_admin)):
    entry = mark_resolved(request_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Unknown support request_id: {request_id}")
    to_email = email_for_customer(entry["customer_id"])
    subject, email_body = support_request_resolved(request_id, entry["message"])
    email_result = send_email(to_email, subject, email_body)
    update_resolution_email(request_id, email_result)
    entry["resolution_email"] = email_result
    return entry
