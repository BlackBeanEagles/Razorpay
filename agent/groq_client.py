"""Shared Groq chat-completions client for both LLM agents in this codebase (agent/llm_agent.py's
shopping agent, reconciliation/settlement_qa.py's finance Q&A agent) -- same model, same
OpenAI-compatible tool-calling API, same failure mode worth handling once instead of twice.

Groq's free/test tier caps tokens-per-minute, not just requests-per-minute (8000 tok/min on
this account). A single call here resends the full system prompt + tool schemas + the
conversation so far -- normal for the OpenAI-compatible chat-completions protocol, but it means
a handful of calls in quick succession (a multi-step tool-calling turn, or a user sending a
couple of messages close together) can burn through the per-minute budget and get a 429 back,
even though the account is nowhere near its daily request cap. That happened for real during
this project's own testing: 5 of 6 back-to-back chat messages failed with a raw
"429 Client Error" string surfaced straight to the user. chat_completion() retries through a
transient 429 using Groq's own x-ratelimit-reset-tokens header (or a standard Retry-After) to
wait exactly as long as needed, capped so a real outage still fails fast instead of hanging.
"""
import os
import re
import time

import requests

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
_PLACEHOLDER_KEY = "gsk_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

LLM_AVAILABLE = bool(GROQ_API_KEY) and GROQ_API_KEY != _PLACEHOLDER_KEY and GROQ_API_KEY.startswith("gsk_")

_MAX_RETRIES = 2
_MAX_WAIT_SECONDS = 12  # a real outage (not just a rate-limit blip) must still fail fast, not hang


def _parse_wait_seconds(resp) -> float:
    """Prefers the standard Retry-After header; falls back to Groq's own
    x-ratelimit-reset-tokens (e.g. "2.61s" or "1m30s") -- the exact moment enough budget frees
    up, which is normally much sooner than guessing with a fixed backoff would be."""
    retry_after = resp.headers.get("retry-after")
    if retry_after:
        try:
            return float(retry_after)
        except ValueError:
            pass
    reset = resp.headers.get("x-ratelimit-reset-tokens") or resp.headers.get("x-ratelimit-reset-requests")
    if reset:
        minutes = re.search(r"(\d+(?:\.\d+)?)m", reset)
        seconds = re.search(r"(\d+(?:\.\d+)?)s", reset)
        total = (float(minutes.group(1)) * 60 if minutes else 0) + (float(seconds.group(1)) if seconds else 0)
        if total > 0:
            return total
    return 1.0  # header missing entirely -- a short fixed wait beats not retrying at all


def chat_completion(messages: list, tools: list = None, temperature: float = 0.3, tool_choice: str = "auto") -> dict:
    """POSTs to Groq's chat-completions endpoint, retrying through a transient 429 up to
    _MAX_RETRIES times. Raises requests.HTTPError (via raise_for_status) for anything else, or
    if retries are exhausted -- callers already handle requests.RequestException for the
    "couldn't reach the model at all" case, so a still-failing 429 after retries reports through
    that same path rather than needing its own."""
    payload = {"model": GROQ_MODEL, "messages": messages, "temperature": temperature}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice

    for attempt in range(_MAX_RETRIES + 1):
        resp = requests.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json=payload,
            timeout=30,
        )
        if resp.status_code != 429 or attempt == _MAX_RETRIES:
            resp.raise_for_status()
            return resp.json()
        wait = min(_parse_wait_seconds(resp), _MAX_WAIT_SECONDS)
        time.sleep(wait)
