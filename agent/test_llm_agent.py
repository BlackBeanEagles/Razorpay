"""Acceptance tests for agent/llm_agent.py's per-session conversation memory
(_SESSION_HISTORY et al). Monkeypatches _groq_chat directly -- no real Groq call, no real
tool execution -- these tests are about whether prior turns actually reach the model on the
next call, not about the shopping pipeline itself (that's covered elsewhere)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from agent import llm_agent
from agent.llm_agent import run_chat_turn_llm_stream, run_chat_turn_llm, _get_history, _append_history


def _isolate(monkeypatch):
    # A fresh dict per test -- the real one is a module-level singleton shared across the whole
    # process, and a leftover session_id/history from an earlier test must never leak into this one.
    monkeypatch.setattr(llm_agent, "_SESSION_HISTORY", {})


def _plain_reply_response(text: str) -> dict:
    """A Groq response with no tool_calls -- the model just answering in plain text, the
    simplest way to reach the "final" branch that saves history."""
    return {"choices": [{"message": {"content": text}}]}


def test_first_message_in_a_session_has_no_prior_history(monkeypatch):
    _isolate(monkeypatch)
    captured = []

    def fake_groq_chat(messages):
        captured.append(list(messages))
        return _plain_reply_response("Hi there!")

    monkeypatch.setattr(llm_agent, "_groq_chat", fake_groq_chat)
    list(run_chat_turn_llm_stream("hello", "c1", "m_default", session_id="sess_1"))

    # Only the system prompt + this one user message -- nothing else, since no history exists yet.
    assert len(captured[0]) == 2
    assert captured[0][1] == {"role": "user", "content": "hello"}


def test_second_message_in_the_same_session_includes_the_first_exchange(monkeypatch):
    _isolate(monkeypatch)
    responses = iter([
        _plain_reply_response("I found Wireless Earbuds X200 for Rs.1799 -- want it?"),
        _plain_reply_response("Sure, adding it now."),
    ])
    monkeypatch.setattr(llm_agent, "_groq_chat", lambda messages: next(responses))

    list(run_chat_turn_llm_stream("find wireless earbuds", "c1", "m_default", session_id="sess_1"))

    captured = []
    monkeypatch.setattr(llm_agent, "_groq_chat", lambda messages: captured.append(list(messages)) or _plain_reply_response("ok"))
    list(run_chat_turn_llm_stream("add to cart", "c1", "m_default", session_id="sess_1"))

    # This is the actual bug this test guards against: the second call must see the first
    # exchange, or "add to cart" arrives with no idea what "it" refers to.
    sent = captured[0]
    assert {"role": "user", "content": "find wireless earbuds"} in sent
    assert {"role": "assistant", "content": "I found Wireless Earbuds X200 for Rs.1799 -- want it?"} in sent
    assert sent[-1] == {"role": "user", "content": "add to cart"}


def test_different_sessions_never_share_history(monkeypatch):
    _isolate(monkeypatch)
    monkeypatch.setattr(llm_agent, "_groq_chat", lambda messages: _plain_reply_response("noted"))
    list(run_chat_turn_llm_stream("first session message", "c1", "m_default", session_id="sess_1"))

    captured = []
    monkeypatch.setattr(llm_agent, "_groq_chat", lambda messages: captured.append(list(messages)) or _plain_reply_response("ok"))
    list(run_chat_turn_llm_stream("second session message", "c1", "m_default", session_id="sess_2"))

    assert len(captured[0]) == 2  # no leakage from sess_1
    assert "first session message" not in str(captured[0])


def test_different_customers_never_share_history_even_with_the_same_session_id(monkeypatch):
    _isolate(monkeypatch)
    monkeypatch.setattr(llm_agent, "_groq_chat", lambda messages: _plain_reply_response("noted"))
    list(run_chat_turn_llm_stream("c1's message", "c1", "m_default", session_id="sess_shared"))

    captured = []
    monkeypatch.setattr(llm_agent, "_groq_chat", lambda messages: captured.append(list(messages)) or _plain_reply_response("ok"))
    list(run_chat_turn_llm_stream("c2's message", "c2", "m_default", session_id="sess_shared"))

    assert "c1's message" not in str(captured[0])


def test_no_session_id_means_no_memory_at_all(monkeypatch):
    # A caller that deliberately doesn't pass session_id (e.g. a one-shot batch/demo call)
    # keeps the old, context-free behavior exactly -- this is an opt-in, not a forced change.
    _isolate(monkeypatch)
    monkeypatch.setattr(llm_agent, "_groq_chat", lambda messages: _plain_reply_response("noted"))
    list(run_chat_turn_llm_stream("first", "c1", "m_default"))

    captured = []
    monkeypatch.setattr(llm_agent, "_groq_chat", lambda messages: captured.append(list(messages)) or _plain_reply_response("ok"))
    list(run_chat_turn_llm_stream("second", "c1", "m_default"))

    assert len(captured[0]) == 2


def test_history_is_capped_to_the_most_recent_exchanges(monkeypatch):
    _isolate(monkeypatch)
    for i in range(10):
        _append_history("c1", "sess_1", f"message {i}", f"reply {i}")
    history = _get_history("c1", "sess_1")
    assert len(history) == llm_agent._MAX_HISTORY_TURNS * 2
    # Oldest exchanges are dropped first -- the most recent ones survive.
    assert history[-1] == {"role": "assistant", "content": "reply 9"}
    assert {"role": "user", "content": "message 0"} not in history


def test_a_rate_limit_error_does_not_get_saved_to_history(monkeypatch):
    # A transient failure isn't a real exchange the model actually participated in --
    # persisting it would just pollute the next turn's context with an error it never "said".
    _isolate(monkeypatch)
    import requests

    def raise_error(messages):
        raise requests.ConnectionError("simulated outage")

    monkeypatch.setattr(llm_agent, "_groq_chat", raise_error)
    list(run_chat_turn_llm_stream("hello", "c1", "m_default", session_id="sess_1"))
    assert _get_history("c1", "sess_1") == []


def test_run_chat_turn_llm_non_streaming_wrapper_also_threads_session_id(monkeypatch):
    _isolate(monkeypatch)
    monkeypatch.setattr(llm_agent, "_groq_chat", lambda messages: _plain_reply_response("first reply"))
    run_chat_turn_llm("first", "c1", "m_default", session_id="sess_1")

    captured = []
    monkeypatch.setattr(llm_agent, "_groq_chat", lambda messages: captured.append(list(messages)) or _plain_reply_response("second reply"))
    run_chat_turn_llm("second", "c1", "m_default", session_id="sess_1")

    assert {"role": "assistant", "content": "first reply"} in captured[0]
