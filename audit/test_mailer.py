"""Acceptance tests for audit/mailer.py. Network calls to Resend are monkeypatched -- a test
suite must never depend on (or burn quota against) a real external email provider."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from audit import mailer


def test_placeholder_key_from_env_example_is_never_treated_as_configured():
    # The real bug this pins: .env.example's literal placeholder (re_xxxxxxxxxxxxxxxxxxxxxxxxxxxx)
    # starts with "re_" just like a genuine key would, so a naive prefix-only check reports it as
    # configured. Anyone who copies .env.example to .env without replacing this value would then
    # have send_email() attempt a real, doomed API call to Resend on every support request.
    assert mailer._is_real_key(mailer._PLACEHOLDER_KEY) is False


def test_empty_key_is_not_configured():
    assert mailer._is_real_key("") is False


def test_a_real_looking_key_is_configured():
    assert mailer._is_real_key("re_abc123realkey") is True


def test_a_key_with_the_wrong_prefix_is_not_configured():
    assert mailer._is_real_key("sk_wrongprefix") is False


def test_send_email_without_recipient_fails_honestly(monkeypatch):
    monkeypatch.setattr(mailer, "EMAIL_AVAILABLE", True)
    result = mailer.send_email("", "subject", "body")
    assert result["sent"] is False
    assert "no email address" in result["detail"].lower()


def test_send_email_when_not_configured_fails_honestly(monkeypatch):
    monkeypatch.setattr(mailer, "EMAIL_AVAILABLE", False)
    result = mailer.send_email("someone@example.com", "subject", "body")
    assert result["sent"] is False
    assert "not configured" in result["detail"].lower()


def test_send_email_success_path(monkeypatch):
    monkeypatch.setattr(mailer, "EMAIL_AVAILABLE", True)

    class FakeResponse:
        ok = True
        def json(self):
            return {"id": "resend_msg_123"}

    def fake_post(url, headers, json, timeout):
        assert url == mailer.RESEND_URL
        assert json["to"] == ["someone@example.com"]
        assert json["subject"] == "subject"
        return FakeResponse()

    monkeypatch.setattr(mailer.requests, "post", fake_post)
    result = mailer.send_email("someone@example.com", "subject", "body")
    assert result == {"sent": True, "provider_id": "resend_msg_123", "detail": "Sent via Resend."}


def test_send_email_provider_rejection_reported_honestly(monkeypatch):
    monkeypatch.setattr(mailer, "EMAIL_AVAILABLE", True)

    class FakeResponse:
        ok = False
        status_code = 422
        text = "Recipient not verified in sandbox mode"

    monkeypatch.setattr(mailer.requests, "post", lambda *a, **k: FakeResponse())
    result = mailer.send_email("someone@example.com", "subject", "body")
    assert result["sent"] is False
    assert "422" in result["detail"]


def test_send_email_network_failure_reported_honestly(monkeypatch):
    monkeypatch.setattr(mailer, "EMAIL_AVAILABLE", True)

    def raise_connection_error(*a, **k):
        raise mailer.requests.RequestException("connection refused")

    monkeypatch.setattr(mailer.requests, "post", raise_connection_error)
    result = mailer.send_email("someone@example.com", "subject", "body")
    assert result["sent"] is False
    assert "connection refused" in result["detail"]


def test_support_request_confirmation_includes_request_id_and_message():
    subject, body = mailer.support_request_confirmation("abc123", "why was my order blocked?")
    assert "abc123" in body
    assert "why was my order blocked?" in body
    assert "TechBazaar" in subject
