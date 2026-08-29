"""Acceptance tests for audit_log's source tagging (live vs batch_test vs unit_test) -- the
distinction the dashboard's live-activity stats rely on to never count test/batch noise as real
customer activity."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from audit import audit_log

_TEST_LOG_PATH = os.path.join(os.path.dirname(__file__), "_isolated_test_audit_log.jsonl")


def setup():
    if os.path.exists(_TEST_LOG_PATH):
        os.remove(_TEST_LOG_PATH)
    audit_log.set_source("unit_test")  # each test starts from the same known state


def teardown_module():
    if os.path.exists(_TEST_LOG_PATH):
        os.remove(_TEST_LOG_PATH)
    audit_log.set_source("unit_test")  # restore -- conftest.py's session-wide setting


def _read(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def test_log_event_tags_current_source():
    # pytest 8 dropped nose-style auto-invocation of a bare setup()/teardown() -- it is NEVER
    # called automatically here, only when a test calls it itself (verified empirically; this
    # matches the existing convention every other stateful test file in this repo already
    # follows, e.g. guardrail/test_guardrail.py calling setup() as its own first line).
    setup()
    audit_log.set_source("live")
    audit_log.log_event("shelf", "search", {}, {}, "ok", log_path=_TEST_LOG_PATH)
    entries = _read(_TEST_LOG_PATH)
    assert entries[0]["source"] == "live"


def test_set_source_changes_subsequent_entries():
    setup()
    audit_log.set_source("batch_test")
    audit_log.log_event("shelf", "search", {}, {}, "ok", log_path=_TEST_LOG_PATH)
    audit_log.set_source("live")
    audit_log.log_event("shelf", "search", {}, {}, "ok", log_path=_TEST_LOG_PATH)
    entries = _read(_TEST_LOG_PATH)
    assert [e["source"] for e in entries] == ["batch_test", "live"]


def test_conftest_tags_this_pytest_run_as_unit_test():
    # The root conftest.py calls set_source("unit_test") at collection time -- if this test
    # itself runs under plain pytest (not some isolated harness), the module-level _SOURCE
    # should already be "unit_test" by the time any test body executes.
    setup()
    assert audit_log._SOURCE == "unit_test"
