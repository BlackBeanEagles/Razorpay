"""Settlement Q&A: a natural-language interface over the reconciliation engine's real output --
Track 4's second example direction ("Settlement Q&A agent"), built alongside the batch
reconciliation report rather than instead of it.

Same LLM tool-calling pattern as agent/llm_agent.py (Groq, OpenAI-compatible tools API), but
scoped to a distinct, narrower toolset that can only read reconcile_from_disk()'s real output --
it cannot search the catalog, check fairness, or move money. The model composes an answer; it
never invents an order's status or a number that didn't come from a tool result.
"""
import json
import os
import sys

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from reconciliation.reconciliation import reconcile_from_disk
from reconciliation.live_verification import check_live_drift
from agent.groq_client import chat_completion, LLM_AVAILABLE

MAX_TOOL_ITERATIONS = 4

SYSTEM_PROMPT = """You are the settlement/reconciliation Q&A assistant for TechBazaar's finance
team. You answer questions about the merchant's real reconciliation batch (orders vs.
settlements) using your tools -- you have no knowledge of order or settlement data except what
the tools return, so never state a number, order_id, or status you didn't just get from a tool
call this turn.

Tools:
- get_batch_summary -- overall totals: total orders, matched, match rate, exception counts by
  type, and the live vs synthetic_seed breakdown (by_source). Call this for any "how are we
  doing overall" / "what's our match rate" / "how many exceptions" question.
- get_order_detail(order_id) -- the specific status of one order: matched, or the exact
  exception type and reason. Call this whenever the user names a specific order_id.
- list_exceptions(exception_type, source) -- the itemized exception list, optionally filtered by
  type (amount_mismatch, missing_settlement, duplicate_settlement, status_exception,
  orphan_settlement) and/or source (live_ledger, synthetic_seed). Call this for "which orders
  have X problem" / "show me the live ones" / "what needs manual review" questions.
- get_live_drift_check -- independently re-verifies every real captured purchase against
  Razorpay's OWN current record, fetched fresh right now (not this app's own saved ledger data).
  Catches things the other tools can't, like a refund issued by hand straight in the Razorpay
  Dashboard that this app was never told about. Call this for "has anything changed since we
  charged it" / "does Razorpay agree with our records" / "any overcharges" questions. You cannot
  issue a refund yourself -- if it finds a remediable overcharge, tell the user and point them to
  the "Refund overcharge" button on the live-check panel; never claim you fixed it.

Be concise -- 2-4 sentences, plain language a non-engineer finance person would want, not a raw
dump of the JSON. If a question needs an order_id or filter you weren't given and can't
reasonably infer, ask for it rather than guessing. If the answer is "nothing matches" (e.g. no
exceptions of a type), say that plainly -- don't imply there might be more."""

TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "get_batch_summary",
            "description": "Overall reconciliation totals: total orders, matched, match rate, exception counts by type, and the live vs synthetic breakdown.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_order_detail",
            "description": "The reconciliation status of one specific order: matched, or the exact exception type and reason.",
            "parameters": {
                "type": "object",
                "properties": {"order_id": {"type": "string"}},
                "required": ["order_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_exceptions",
            "description": "The itemized exception list, optionally filtered by exception type and/or source.",
            "parameters": {
                "type": "object",
                "properties": {
                    "exception_type": {
                        "type": ["string", "null"],
                        "enum": ["amount_mismatch", "missing_settlement", "duplicate_settlement", "status_exception", "orphan_settlement", None],
                    },
                    "source": {"type": ["string", "null"], "enum": ["live_ledger", "synthetic_seed", None]},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_live_drift_check",
            "description": "Independently re-verifies every real captured purchase against Razorpay's own current record, fetched fresh right now. Catches drift this app's own saved records can't see, like a manual refund issued straight in the Razorpay Dashboard.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


def get_batch_summary() -> dict:
    r = reconcile_from_disk()
    return {
        "total_orders": r["total_orders"], "matched": r["matched"], "match_rate": r["match_rate"],
        "exception_counts": r["exception_counts"], "by_source": r["by_source"],
    }


def get_order_detail(order_id: str) -> dict:
    r = reconcile_from_disk()
    for e in r["exceptions"]:
        if e["order_id"] == order_id:
            return {"order_id": order_id, "status": e["type"], "reason": e["reason"], "source": e.get("source")}
    return {"order_id": order_id, "status": "matched_or_unknown",
            "reason": "No exception on record for this order_id -- it's either a clean match or not part of this batch at all."}


def list_exceptions(exception_type: str = None, source: str = None) -> dict:
    r = reconcile_from_disk()
    filtered = [
        e for e in r["exceptions"]
        if (exception_type is None or e["type"] == exception_type)
        and (source is None or e.get("source") == source)
    ]
    return {"count": len(filtered), "exceptions": filtered}


_TOOL_FUNCTIONS = {
    "get_batch_summary": lambda args: get_batch_summary(),
    "get_order_detail": lambda args: get_order_detail(args["order_id"]),
    "list_exceptions": lambda args: list_exceptions(args.get("exception_type"), args.get("source")),
    "get_live_drift_check": lambda args: check_live_drift(),
}


def _groq_chat(messages: list) -> dict:
    return chat_completion(messages, tools=TOOLS_SCHEMA, temperature=0.2)


def ask(question: str) -> dict:
    """Runs one question through the tool-calling loop. Returns {"answer", "tool_calls"} --
    tool_calls is the ordered list of (name, args, result) actually used, so the answer's
    provenance is inspectable, not just asserted."""
    if not LLM_AVAILABLE:
        return {"answer": "The settlement Q&A assistant isn't configured (no GROQ_API_KEY) -- use the reconciliation panel's table directly.", "tool_calls": []}

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    tool_calls_used = []

    for _ in range(MAX_TOOL_ITERATIONS):
        try:
            response = _groq_chat(messages)
        except requests.RequestException as e:
            # chat_completion() already retried through a transient rate-limit -- still failing
            # here means a real, sustained outage, not a blip worth another attempt.
            answer = (
                "The AI is getting rate-limited right now -- please wait a few seconds and try again."
                if isinstance(e, requests.HTTPError) and e.response is not None and e.response.status_code == 429
                else "Couldn't reach the AI model just now -- please try again in a moment."
            )
            return {"answer": answer, "tool_calls": tool_calls_used}

        choice = response["choices"][0]["message"]
        messages.append(choice)
        tool_calls = choice.get("tool_calls")

        if not tool_calls:
            return {"answer": choice.get("content") or "I'm not sure how to answer that.", "tool_calls": tool_calls_used}

        for call in tool_calls:
            fn_name = call["function"]["name"]
            try:
                fn_args = json.loads(call["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                fn_args = {}
            fn = _TOOL_FUNCTIONS.get(fn_name)
            result = fn(fn_args) if fn else {"error": f"Unknown tool: {fn_name}"}
            tool_calls_used.append({"name": fn_name, "args": fn_args, "result": result})
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result, default=str)})

    return {"answer": "Wasn't able to finish answering within a reasonable number of steps -- try a more specific question.", "tool_calls": tool_calls_used}
