"""POST /api/chat and POST /api/chat/stream -- BUILD_SPEC.md section 8.

Requires a logged-in customer session -- the agent's purchase step forwards to Razorpay,
so a shopper must be authenticated before they can reach checkout. customer_id comes from
the verified session, never from the request body, so it can't be spoofed by the client.

Uses the real Groq-powered LLM agent (agent/llm_agent.py) when GROQ_API_KEY is set;
otherwise falls back to the deterministic router (agent/agent.py) -- both expose the same
response shape, and /api/chat/stream works identically either way (see run_chat_turn_stream's
docstring for what "streaming" means in the fallback case)."""
import json
import os
import sys

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from agent.agent import run_chat_turn, run_chat_turn_stream
from agent.llm_agent import run_chat_turn_llm, run_chat_turn_llm_stream
from agent.groq_client import LLM_AVAILABLE
from api.customer_auth import require_customer

router = APIRouter()


class ChatRequest(BaseModel):
    session_id: str
    message: str
    mandate_id: str


@router.post("/api/chat")
def chat(body: ChatRequest, customer_id: str = Depends(require_customer)):
    if LLM_AVAILABLE:
        return run_chat_turn_llm(body.message, customer_id, body.mandate_id, body.session_id)
    return run_chat_turn(body.message, customer_id, body.mandate_id)


@router.post("/api/chat/stream")
def chat_stream(body: ChatRequest, customer_id: str = Depends(require_customer)):
    def event_source():
        if LLM_AVAILABLE:
            stream = run_chat_turn_llm_stream(body.message, customer_id, body.mandate_id, body.session_id)
        else:
            stream = run_chat_turn_stream(body.message, customer_id, body.mandate_id)
        for event in stream:
            yield f"data: {json.dumps(event, default=str)}\n\n"

    return StreamingResponse(event_source(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
