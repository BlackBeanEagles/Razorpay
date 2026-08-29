"""Append-only structured audit logger shared by Shelf, Parity, and Guardrail."""
import json
import os
import threading
from datetime import datetime, timezone

_LOCK = threading.Lock()
_LOG_PATH = os.path.join(os.path.dirname(__file__), "audit_log.jsonl")

# Every entry is tagged with the source that generated it. Without this, a batch-test run and a
# genuine customer action are indistinguishable in this one shared log -- which is exactly what
# made the dashboard's stat cards look synthetic even where they covered real usage: there was
# no way to tell test noise from a real customer's search/purchase. Defaults to "live" (real
# API/chat traffic); batch_tests/*.py scripts and the pytest suite (via the root conftest.py)
# call set_source() once at startup to tag everything they generate instead.
_SOURCE = "live"


def set_source(source: str) -> None:
    global _SOURCE
    _SOURCE = source


def log_event(component: str, event: str, input_summary: dict, result_summary: dict, outcome: str, log_path: str = _LOG_PATH) -> dict:
    """Append one audit entry. component in {shelf,parity,guardrail}; outcome in {ok,blocked,flagged,failed}."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "component": component,
        "event": event,
        "input_summary": input_summary,
        "result_summary": result_summary,
        "outcome": outcome,
        "source": _SOURCE,
    }
    with _LOCK:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    return entry


def read_all(log_path: str = _LOG_PATH) -> list:
    if not os.path.exists(log_path):
        return []
    entries = []
    with open(log_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


def clear_log(log_path: str = _LOG_PATH) -> None:
    with _LOCK:
        if os.path.exists(log_path):
            os.remove(log_path)
