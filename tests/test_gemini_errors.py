"""Gemini error classification, retry/backoff, fallback and diagnostics (all mocked)."""
import logging

import pytest
from google.genai import errors

try:  # the SDK uses httpx2 in recent versions, httpx in older ones
    import httpx2 as httpx
except ImportError:  # pragma: no cover
    import httpx

import ai_engine
import config
import gemini_errors as ge
from ai_engine import AIResponseError, AIServiceError
from conftest import FakeResponse

# Built at runtime so no literal in the repo matches Google's real key format (keeps secret scanners quiet).
FAKE_KEY = "AIza" + "Sy" + "FAKE" * 7 + "12345"


def api_error(code, message="boom", status="X"):
    return errors.APIError(code, {"error": {"code": code, "message": message, "status": status}})


def analyze(client, **kwargs):
    kwargs.setdefault("fallback", None)  # most tests exercise a single model
    return ai_engine.analyze_notice("Some notice text", client=client, model="primary-model", **kwargs)


# --------------------------------------------------------------- classification
@pytest.mark.parametrize("exc,kind", [
    (api_error(400, "Request contains an invalid argument.", "INVALID_ARGUMENT"), ge.INVALID_REQUEST),
    (api_error(400, "API key not valid. Please pass a valid API key.", "INVALID_ARGUMENT"), ge.AUTH),
    (api_error(400, "API key expired. Please renew the API key.", "INVALID_ARGUMENT"), ge.AUTH),
    (api_error(401), ge.AUTH),
    (api_error(403, "permission denied", "PERMISSION_DENIED"), ge.AUTH),
    (api_error(404, "model not found", "NOT_FOUND"), ge.NOT_FOUND),
    (api_error(429, "quota", "RESOURCE_EXHAUSTED"), ge.RATE_LIMIT),
    (api_error(500), ge.SERVER),
    (api_error(502), ge.SERVER),
    (api_error(503, "overloaded", "UNAVAILABLE"), ge.SERVER),
    (api_error(504, "Deadline expired", "DEADLINE_EXCEEDED"), ge.TIMEOUT),
    (api_error(418), ge.API_ERROR),
    (httpx.ReadTimeout("slow"), ge.TIMEOUT),
    (TimeoutError("slow"), ge.TIMEOUT),
    (httpx.ConnectError("dns"), ge.NETWORK),
    (ConnectionError("refused"), ge.NETWORK),
    (ValueError("???"), ge.UNKNOWN),
])
def test_classification(exc, kind):
    assert ge.classify(exc).kind == kind


def test_retryable_and_fallback_flags():
    assert ge.classify(api_error(429)).retryable and ge.classify(api_error(503)).retryable
    for code in (400, 401, 403, 404, 504):
        assert not ge.classify(api_error(code)).retryable
    assert ge.classify(api_error(404)).fallback_eligible
    assert ge.classify(api_error(504)).fallback_eligible
    assert not ge.classify(api_error(401)).fallback_eligible
    assert not ge.classify(api_error(400)).fallback_eligible


@pytest.mark.parametrize("code,status,fragments", [
    (400, "INVALID_ARGUMENT", ["rejected the request as invalid", "HTTP 400"]),
    (401, "UNAUTHENTICATED", ["rejected the API key", "GEMINI_API_KEY", "restart"]),
    (403, "PERMISSION_DENIED", ["rejected the API key", "HTTP 403"]),
    (404, "NOT_FOUND", ["primary-model", "not found", "GEMINI_MODEL"]),
    (429, "RESOURCE_EXHAUSTED", ["rate limit or quota", "HTTP 429"]),
    (500, "INTERNAL", ["server error", "HTTP 500"]),
    (503, "UNAVAILABLE", ["server error", "HTTP 503"]),
    (504, "DEADLINE_EXCEEDED", ["did not answer within", "primary-model", "faster model"]),
])
def test_user_messages_are_actionable(fake_client, code, status, fragments):
    with pytest.raises(AIServiceError) as err:
        analyze(fake_client(api_error(code, "detail", status)))
    for fragment in fragments:
        assert fragment in str(err.value)
    assert "temporarily unavailable" not in str(err.value)  # the old, misleading generic message


def test_network_and_timeout_messages(fake_client):
    with pytest.raises(AIServiceError, match="Could not connect to Gemini") as net:
        analyze(fake_client(httpx.ConnectError("dns")))
    assert net.value.kind == ge.NETWORK
    with pytest.raises(AIServiceError, match="did not answer within") as slow:
        analyze(fake_client(httpx.ReadTimeout("slow")))
    assert slow.value.kind == ge.TIMEOUT


def test_error_details_are_safe_and_complete(fake_client):
    with pytest.raises(AIServiceError) as err:
        analyze(fake_client(api_error(404, "gone", "NOT_FOUND")))
    details = err.value.details()
    assert details["Model"] == "primary-model" and details["HTTP status"] == 404
    assert details["Error type"] == ge.NOT_FOUND and details["API status"] == "NOT_FOUND"


# ----------------------------------------------------------------------- retries
def test_503_then_success_is_retried_once(fake_client, payload_json, sleeps):
    client = fake_client(api_error(503, status="UNAVAILABLE"), payload_json)
    assert analyze(client)["title"]
    assert client.models.calls == 2 and len(sleeps) == 1


def test_429_exhausts_limited_retries_with_backoff(fake_client, sleeps):
    client = fake_client(api_error(429, status="RESOURCE_EXHAUSTED"))
    with pytest.raises(AIServiceError, match="after 3 attempts") as err:
        analyze(client)
    assert client.models.calls == config.GEMINI_MAX_RETRIES + 1 == 3
    assert err.value.attempts == 3 and err.value.kind == ge.RATE_LIMIT
    assert len(sleeps) == 2 and sleeps[1] > sleeps[0]  # exponential backoff
    assert max(sleeps) <= config.GEMINI_RETRY_MAX_DELAY + 0.5


@pytest.mark.parametrize("code", [500, 502, 503])
def test_all_temporary_5xx_are_retried(fake_client, code):
    client = fake_client(api_error(code))
    with pytest.raises(AIServiceError):
        analyze(client)
    assert client.models.calls == 3


@pytest.mark.parametrize("code", [400, 401, 403, 404, 504])
def test_non_temporary_errors_are_never_retried(fake_client, code, sleeps):
    client = fake_client(api_error(code))
    with pytest.raises(AIServiceError):
        analyze(client)
    assert client.models.calls == 1 and sleeps == []


@pytest.mark.parametrize("exc", [httpx.ReadTimeout("slow"), httpx.ConnectError("dns")])
def test_client_side_timeouts_and_network_errors_not_retried(fake_client, exc):
    client = fake_client(exc)
    with pytest.raises(AIServiceError):
        analyze(client)
    assert client.models.calls == 1


# ---------------------------------------------------------------------- fallback
def test_missing_primary_model_uses_fallback(fake_client, payload_json):
    def scripted(model):
        return api_error(404, status="NOT_FOUND") if model == "primary-model" else payload_json
    client = fake_client(scripted)
    result = analyze(client, fallback="backup-model")
    assert client.models.models_used == ["primary-model", "backup-model"]
    assert any("fallback model 'backup-model'" in n for n in result["confidence_notes"])


def test_timeout_on_primary_uses_fallback_without_retrying_primary(fake_client, payload_json):
    def scripted(model):
        return api_error(504, status="DEADLINE_EXCEEDED") if model == "primary-model" else payload_json
    client = fake_client(scripted)
    analyze(client, fallback="backup-model")
    assert client.models.models_used == ["primary-model", "backup-model"]


def test_persistent_503_retries_then_falls_back(fake_client, payload_json):
    def scripted(model):
        return api_error(503) if model == "primary-model" else payload_json
    client = fake_client(scripted)
    analyze(client, fallback="backup-model")
    assert client.models.models_used == ["primary-model"] * 3 + ["backup-model"]


@pytest.mark.parametrize("code", [400, 401, 403])
def test_auth_and_invalid_request_never_trigger_fallback(fake_client, code):
    client = fake_client(api_error(code))
    with pytest.raises(AIServiceError):
        analyze(client, fallback="backup-model")
    assert client.models.models_used == ["primary-model"]


def test_both_models_failing_reports_both(fake_client):
    client = fake_client(lambda model: api_error(404 if model == "primary-model" else 503))
    with pytest.raises(AIServiceError) as err:
        analyze(client, fallback="backup-model")
    assert err.value.kind == ge.NOT_FOUND
    assert "fallback model 'backup-model' also failed" in str(err.value)


def test_fallback_not_used_when_primary_succeeds(fake_client, payload_json):
    client = fake_client(payload_json)
    result = analyze(client, fallback="backup-model")
    assert client.models.models_used == ["primary-model"]
    assert not any("fallback" in n for n in result["confidence_notes"])


# ------------------------------------------------- model selection / request shape
def test_configured_model_is_passed_to_the_sdk(fake_client, payload_json, monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL", "my-configured-model")
    client = fake_client(payload_json)
    ai_engine.analyze_notice("Some notice text", client=client, fallback=None)
    assert client.models.requests[0]["model"] == "my-configured-model"


def test_default_model_used_when_unconfigured(fake_client, payload_json):
    client = fake_client(payload_json)
    ai_engine.analyze_notice("text", client=client, fallback=None)
    assert client.models.requests[0]["model"] == config.DEFAULT_GEMINI_MODEL


def test_request_asks_for_json_with_system_instruction(fake_client, payload_json):
    client = fake_client(payload_json)
    analyze(client)
    request = client.models.requests[0]
    assert "Some notice text" in request["contents"]
    assert request["config"].response_mime_type == "application/json"
    assert request["config"].system_instruction == ai_engine.SYSTEM_INSTRUCTION


def test_fallback_model_config(monkeypatch):
    assert config.get_fallback_model() == config.DEFAULT_FALLBACK_MODEL
    monkeypatch.setenv("GEMINI_FALLBACK_MODEL", "none")
    assert config.get_fallback_model() is None
    monkeypatch.setenv("GEMINI_FALLBACK_MODEL", "custom-fb")
    assert config.get_fallback_model() == "custom-fb"
    monkeypatch.setenv("GEMINI_MODEL", "custom-fb")
    assert config.get_fallback_model() is None  # identical to the primary


# --------------------------------------------------------------- response parsing
def test_empty_response_text_raises_clear_error(fake_client):
    client = fake_client(FakeResponse(None))
    with pytest.raises(AIResponseError, match="empty response"):
        analyze(client)
    assert client.models.calls == 2  # one extra attempt


def test_blocked_prompt_is_reported(fake_client):
    class Feedback:
        block_reason = "SAFETY"
    with pytest.raises(AIResponseError, match="blocked this content.*SAFETY"):
        analyze(fake_client(FakeResponse(None, prompt_feedback=Feedback())))


def test_invalid_json_then_valid_recovers(fake_client, payload_json):
    assert analyze(fake_client("{not json", payload_json))["title"]


def test_markdown_fenced_json_accepted(fake_client, payload_json):
    assert analyze(fake_client(f"```json\n{payload_json}\n```"))["title"]


# -------------------------------------------------------- logging / secret hygiene
def test_logs_have_diagnostics_but_no_secrets_or_document_text(fake_client, caplog):
    secret_text = "CONFIDENTIAL-NOTICE-BODY-98765"
    exc = api_error(400, f"API key not valid: {FAKE_KEY}", "INVALID_ARGUMENT")
    with caplog.at_level(logging.INFO), pytest.raises(AIServiceError) as err:
        ai_engine.analyze_notice(secret_text, client=fake_client(exc), model="primary-model", fallback=None)
    log = caplog.text
    assert "model=primary-model" in log and "kind=auth" in log and "http=400" in log
    assert "INVALID_ARGUMENT" in log
    assert secret_text not in log
    assert FAKE_KEY not in log and FAKE_KEY not in str(err.value)
    assert "[REDACTED]" in log


def test_sanitize_redacts_keys_and_truncates():
    assert FAKE_KEY not in ge.sanitize(f"bad key={FAKE_KEY}")
    assert len(ge.sanitize("x" * 5000)) < 300


# ----------------------------------------------------------------- test_connection
def test_connection_ok(fake_client, payload_json):
    result = ai_engine.test_connection(client=fake_client('{"ok": true}'), model="primary-model")
    assert result["ok"] and result["model"] == "primary-model"


def test_connection_failure_reports_details(fake_client):
    result = ai_engine.test_connection(client=fake_client(api_error(404, status="NOT_FOUND")), model="primary-model")
    assert not result["ok"] and "primary-model" in result["message"]
    assert result["details"]["HTTP status"] == 404


def test_connection_without_key():
    result = ai_engine.test_connection()
    assert not result["ok"] and "not configured" in result["message"]
