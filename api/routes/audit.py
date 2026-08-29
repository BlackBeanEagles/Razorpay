"""GET /api/audit-log and GET /api/batch-results -- BUILD_SPEC.md section 8.

Admin-only: this is the merchant's internal pricing history, transaction ledger, and audit
trail, not something a shopper on the storefront should be able to pull up directly."""
import json
import os
import sys

from fastapi import APIRouter, Depends, Query

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from audit.audit_log import read_all
from audit.support_log import read_all as read_all_support_requests
from api.auth import require_admin

router = APIRouter()

RESULTS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "batch_tests", "results")
RESULT_FILES = {
    "shelf": "shelf_batch_results.json",
    "parity": "parity_batch_results.json",
    "guardrail": "guardrail_batch_results.json",
    "full_pipeline": "full_pipeline_batch_results.json",
    "reconciliation": "reconciliation_batch_results.json",
}


@router.get("/api/audit-log")
def audit_log(limit: int = Query(default=50, ge=1, le=1000), source: str = Query(default=None), _admin: str = Depends(require_admin)):
    # limit is bounded (ge=1) specifically because entries[-0:] == the WHOLE list, not an
    # empty one -- limit=0 would otherwise silently return everything instead of nothing.
    #
    # source filters BEFORE the limit is applied, not after -- a heavy batch/test run adds
    # hundreds of entries in a burst, and applying limit first (then filtering) could push
    # every real "live" entry out of the window entirely even though it's the thing the caller
    # actually asked for. This is not hypothetical: it happened for real in this project's own
    # testing, where 278 batch_test entries after a handful of live ones made the live-only
    # drill-down come back empty despite a genuine blocked purchase being in the log.
    entries = read_all()
    if source:
        entries = [e for e in entries if e.get("source") == source]
    return list(reversed(entries[-limit:]))


@router.get("/api/batch-results")
def batch_results(_admin: str = Depends(require_admin)):
    results = {}
    for key, filename in RESULT_FILES.items():
        path = os.path.join(RESULTS_DIR, filename)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                results[key] = json.load(f)
        else:
            results[key] = None
    return results


@router.get("/api/live-stats")
def live_stats(_admin: str = Depends(require_admin)):
    """Real counts from genuine customer/AI-buyer activity -- filtered to source == "live"
    entries only, so a batch-test or pytest run (tagged "batch_test"/"unit_test", see
    audit/audit_log.py's set_source) can never be counted as if it were a real customer action.
    Distinct from GET /api/batch-results, which is the fixed proof-batch scores: this is "is
    this actually working for real customers right now", not "did the test suite pass"."""
    shelf = {"total": 0, "ok": 0, "no_match": 0}
    parity = {"total": 0, "ok": 0, "flagged": 0}
    guardrail = {"total": 0, "success": 0, "blocked": 0, "failed_verification": 0, "checkout_required": 0}
    first_at, last_at = None, None

    for entry in read_all():
        if entry.get("source") != "live":
            continue
        ts = entry["timestamp"]
        if first_at is None or ts < first_at:
            first_at = ts
        if last_at is None or ts > last_at:
            last_at = ts

        component, event, outcome = entry["component"], entry["event"], entry["outcome"]
        if component == "shelf" and event == "search":
            shelf["total"] += 1
            shelf["ok" if outcome == "ok" else "no_match"] += 1
        elif component == "parity" and event == "fairness_check":
            parity["total"] += 1
            parity["ok" if outcome == "ok" else "flagged"] += 1
        elif component == "guardrail" and event in ("purchase_forwarded", "verification"):
            guardrail["total"] += 1
            status = (entry.get("result_summary") or {}).get("status")
            if status == "success":
                guardrail["success"] += 1
            elif status == "checkout_required":
                guardrail["checkout_required"] += 1
            elif status == "failed_verification":
                guardrail["failed_verification"] += 1
            else:
                guardrail["blocked"] += 1

    support_by_component = {}
    for req in read_all_support_requests():
        c = req["component"]
        support_by_component[c] = support_by_component.get(c, 0) + (1 if req["status"] == "open" else 0)

    return {
        "shelf": shelf, "parity": parity, "guardrail": guardrail,
        "first_live_event_at": first_at, "last_live_event_at": last_at,
        "open_support_requests_by_component": support_by_component,
    }


PROFILES_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "customer_profiles.json")


@router.get("/api/ai-agents")
def ai_agents(_admin: str = Depends(require_admin)):
    """Every registered external AI buyer (mcp_server/techbazaar_mcp_server.py's
    register_ai_buyer -- a distinct id namespace from human cNNN customers, see that module's
    docstring) plus its real activity pulled straight out of the shared audit trail, not a
    separate log: fairness checks it triggered and every purchase/verification event attributed
    to it via requesting_customer_id. Nothing here is synthesized for display -- an agent with
    no audit activity yet just shows zero counts and a null last_activity."""
    with open(PROFILES_PATH, encoding="utf-8") as f:
        profiles = json.load(f)
    agents = {
        p["customer_id"]: {
            "ai_buyer_id": p["customer_id"], "name": p.get("display_name", p["customer_id"]),
            "fairness_checks": 0, "last_verdict": None, "purchases": [], "last_activity": None,
        }
        for p in profiles if p.get("is_ai_buyer")
    }

    for entry in read_all():
        inp = entry.get("input_summary") or {}
        cid = inp.get("customer_id") or inp.get("requesting_customer_id")
        agent = agents.get(cid)
        if agent is None:
            continue
        ts = entry["timestamp"]
        if agent["last_activity"] is None or ts > agent["last_activity"]:
            agent["last_activity"] = ts
        if entry["component"] == "parity" and entry["event"] == "fairness_check":
            agent["fairness_checks"] += 1
            agent["last_verdict"] = (entry.get("result_summary") or {}).get("verdict")
        elif entry["component"] == "guardrail" and entry["event"] in ("purchase_forwarded", "verification"):
            res = entry.get("result_summary") or {}
            agent["purchases"].append({
                "timestamp": ts, "event": entry["event"], "status": res.get("status"),
                "product_id": inp.get("product_id"),
                "amount_inr": inp.get("amount_inr"),
                "razorpay_order_id": inp.get("razorpay_order_id") or res.get("razorpay_order_id"),
            })

    for agent in agents.values():
        agent["purchases"].sort(key=lambda p: p["timestamp"], reverse=True)

    ordered = sorted(agents.values(), key=lambda a: a["last_activity"] or "", reverse=True)
    return {"agents": ordered}
