import json
from datetime import date

import pytest

import ai_engine
import config
from ai_engine import AIResponseError, AIServiceError, MissingAPIKeyError
from conftest import make_gemini_payload


# ---- JSON validation --------------------------------------------------------
def test_valid_payload_is_normalised(payload):
    result = ai_engine.validate_analysis(payload)
    assert result["title"] == "Semester Registration Notice"
    assert result["category"] == "Registration"
    assert result["deadlines"][0]["date"] == "2026-12-15"
    assert result["deadlines"][0]["date_type"] == "explicit"
    assert all(a["status"] == "pending" for a in result["action_items"])


def test_missing_fields_become_null_or_empty():
    result = ai_engine.validate_analysis({"title": "Only a title"})
    assert result["summary"] is None and result["venue"] is None
    assert result["deadlines"] == [] and result["eligibility"] == []
    assert result["action_items"] == [] and result["confidence_notes"] == []


@pytest.mark.parametrize("bad", [[], "text", None, 5])
def test_non_object_rejected(bad):
    with pytest.raises(AIResponseError):
        ai_engine.validate_analysis(bad)


def test_empty_object_rejected():
    with pytest.raises(AIResponseError):
        ai_engine.validate_analysis({"deadlines": []})


def test_unknown_category_falls_back_to_other(payload):
    payload["category"] = "Banana"
    assert ai_engine.validate_analysis(payload)["category"] == "Other"


# ---- Deadline handling ------------------------------------------------------
def test_invalid_date_is_cleared_and_flagged(payload):
    payload["deadlines"][0]["date"] = "next Friday"
    result = ai_engine.validate_analysis(payload)
    d = result["deadlines"][0]
    assert d["date"] is None and d["date_type"] == "unspecified"
    assert any("not a valid date" in n for n in result["confidence_notes"])


def test_explicit_without_source_text_downgraded_to_inferred(payload):
    payload["deadlines"][0]["source_text"] = ""
    result = ai_engine.validate_analysis(payload)
    assert result["deadlines"][0]["date_type"] == "inferred"
    assert any("inferred" in n for n in result["confidence_notes"])


def test_inferred_date_stays_inferred(payload):
    payload["deadlines"][0]["date_type"] = "inferred"
    assert ai_engine.validate_analysis(payload)["deadlines"][0]["date_type"] == "inferred"


def test_missing_date_is_never_explicit(payload):
    payload["deadlines"][1]["date_type"] = "explicit"
    assert ai_engine.validate_analysis(payload)["deadlines"][1]["date_type"] == "unspecified"


def test_unknown_related_deadline_dropped(payload):
    payload["action_items"][0]["related_deadline"] = "Something not listed"
    assert ai_engine.validate_analysis(payload)["action_items"][0]["related_deadline"] is None


def test_ai_cannot_mark_tasks_completed_or_invent_priority(payload):
    payload["action_items"][0]["status"] = "completed"
    payload["action_items"][0]["priority"] = "urgent!!"
    item = ai_engine.validate_analysis(payload)["action_items"][0]
    assert item["status"] == "pending" and item["priority"] == "medium"


# ---- JSON parsing -----------------------------------------------------------
def test_parse_json_with_code_fence(payload_json):
    assert ai_engine.parse_json_response(f"```json\n{payload_json}\n```")["title"]


def test_parse_json_with_surrounding_prose(payload_json):
    assert ai_engine.parse_json_response(f"Here you go: {payload_json} Thanks")["title"]


@pytest.mark.parametrize("raw", ["", "   ", None, "not json at all", "{broken"])
def test_parse_json_invalid(raw):
    with pytest.raises(AIResponseError):
        ai_engine.parse_json_response(raw)


# ---- analyze_notice with mocked Gemini --------------------------------------
def test_analyze_notice_success(fake_client, payload_json):
    client = fake_client(payload_json)
    result = ai_engine.analyze_notice("some notice text", client=client, today=date(2026, 11, 12))
    assert result["deadlines"][0]["date"] == "2026-12-15"
    assert client.models.calls == 1


def test_analyze_notice_retries_once_on_bad_json(fake_client, payload_json):
    client = fake_client("garbage", payload_json)
    assert ai_engine.analyze_notice("text", client=client)["title"]
    assert client.models.calls == 2


def test_analyze_notice_fails_after_retry(fake_client):
    client = fake_client("garbage")
    with pytest.raises(AIResponseError):
        ai_engine.analyze_notice("text", client=client)
    assert client.models.calls == 2


def test_analyze_notice_empty_text(fake_client):
    with pytest.raises(ai_engine.AIEngineError):
        ai_engine.analyze_notice("   ", client=fake_client("{}"))


def test_long_text_is_truncated_with_note(fake_client, payload_json, monkeypatch):
    monkeypatch.setattr(config, "MAX_AI_INPUT_CHARS", 50)
    result = ai_engine.analyze_notice("x" * 500, client=fake_client(payload_json))
    assert any("very long" in n for n in result["confidence_notes"])


def test_prompt_contains_notice_and_rules():
    prompt = ai_engine.build_prompt("HELLO NOTICE", date(2026, 1, 2))
    assert "HELLO NOTICE" in prompt and "2026-01-02" in prompt
    assert "Never guess a deadline" in ai_engine.SYSTEM_INSTRUCTION


# ---- Missing API key --------------------------------------------------------
def test_missing_api_key_raises(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(config, "_secret", lambda name: None)
    assert config.get_api_key() is None
    with pytest.raises(MissingAPIKeyError, match="not configured"):
        ai_engine.build_client()
    with pytest.raises(MissingAPIKeyError):
        ai_engine.analyze_notice("text")


def test_placeholder_key_treated_as_missing(monkeypatch):
    monkeypatch.setattr(config, "_secret", lambda name: None)
    monkeypatch.setenv("GEMINI_API_KEY", "YOUR_GEMINI_API_KEY_HERE")
    assert config.get_api_key() is None


def test_key_read_from_environment(monkeypatch):
    monkeypatch.setattr(config, "_secret", lambda name: None)
    monkeypatch.setenv("GEMINI_API_KEY", "abc123")
    assert config.get_api_key() == "abc123"
    assert config.get_api_key_source() == "environment variable"


def test_model_is_configurable(monkeypatch):
    monkeypatch.setattr(config, "_secret", lambda name: None)
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    assert config.get_model_name() == config.DEFAULT_GEMINI_MODEL
    monkeypatch.setenv("GEMINI_MODEL", "my-model")
    assert config.get_model_name() == "my-model"
