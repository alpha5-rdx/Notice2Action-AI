"""Database outages, rerun safety and the optional password gate (Streamlit AppTest)."""
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import ai_engine
import auth
import db_backends
from database import Database
from conftest import NOTICE_TEXT

APP = str(Path(__file__).resolve().parent.parent / "app.py")
SECRET_URL = "postgresql://leaky_user:leaky_pw@db.example.supabase.co:5432/postgres"


def script(body):
    return "import sys; sys.path.insert(0, r'.')\nimport streamlit as st, ui, pages\n" + body


def md(at):
    return " ".join(m.value for m in at.markdown)


@pytest.fixture(autouse=True)
def clean_state(tmp_path, monkeypatch):
    import streamlit as st
    st.cache_resource.clear()
    monkeypatch.setenv("NOTICE2ACTION_DB", str(tmp_path / "app.db"))   # never touch the developer's real data
    yield
    st.cache_resource.clear()


# ------------------------------------------------------------------- outages
def _failing_open(*args, **kwargs):
    raise db_backends.DatabaseConnectionError(
        "Database login failed. Check the user and password in DATABASE_URL. (technical: OperationalError)", "auth")


@pytest.mark.parametrize("page", ["page_dashboard", "page_analyze", "page_notices", "page_tasks"])
def test_pages_show_friendly_notice_when_database_is_down(monkeypatch, page):
    import pages
    monkeypatch.setattr(pages, "_open_db", _failing_open)
    at = AppTest.from_string(script(f"pages.{page}()\n"), default_timeout=30).run()
    assert not at.exception, [e.value for e in at.exception]
    assert "Database unavailable" in md(at)
    assert any("Database login failed" in e.value for e in at.error)
    assert any(b.label == "Retry connection" for b in at.button)
    assert "leaky" not in md(at) + " ".join(e.value for e in at.error)


def test_settings_stays_usable_when_database_is_down(monkeypatch):
    import pages
    monkeypatch.setattr(pages, "_open_db", _failing_open)
    at = AppTest.from_string(script("pages.page_settings()\n"), default_timeout=30).run()
    assert not at.exception
    text = md(at)
    assert "Gemini API" in text and "OCR (Tesseract)" in text and "Database" in text      # other boxes still render
    assert any("Database login failed" in e.value for e in at.error)
    assert any(b.label == "Retry connection" for b in at.button)


def test_retry_button_reconnects(monkeypatch, tmp_path):
    import pages
    calls = {"n": 0}
    real = Database(tmp_path / "ok.db")

    def flaky_open():
        calls["n"] += 1
        if calls["n"] == 1:
            _failing_open()
        return real

    monkeypatch.setattr(pages, "_open_db", flaky_open)
    at = AppTest.from_string(script("pages.page_dashboard()\n"), default_timeout=30).run()
    assert "Database unavailable" in md(at)
    next(b for b in at.button if b.label == "Retry connection").click().run()
    assert not at.exception and "Database unavailable" not in md(at) and "Analyzed notices" in md(at)


def test_outage_in_the_middle_of_a_page_does_not_crash_the_app(monkeypatch):
    import pages

    def boom():
        raise db_backends.DatabaseError("The database connection was interrupted. Please retry.", "dropped")

    monkeypatch.setattr(pages, "page_dashboard", boom)
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception
    assert "Database unavailable" in md(at) and any("interrupted" in e.value for e in at.error)
    assert "All rights reserved" in " ".join(m.value for m in at.sidebar.markdown)   # app shell still alive


def test_unexpected_database_message_never_contains_the_connection_string(monkeypatch):
    err = db_backends.translate_pg_error(Exception(f"failed using {SECRET_URL}"))
    assert "leaky" not in str(err)
    assert "leaky" not in db_backends.redact(f"failed using {SECRET_URL}")


def test_save_failure_shows_database_message_and_keeps_analysis(monkeypatch, tmp_path):
    import pages
    db = Database(tmp_path / "x.db")
    monkeypatch.setattr(ai_engine, "build_client", lambda: object())
    monkeypatch.setattr(ai_engine, "analyze_notice",
                        lambda *a, **k: ai_engine.validate_analysis({"title": "T", "summary": "S"}))
    monkeypatch.setattr(Database, "save_notice", lambda *a, **k: (_ for _ in ()).throw(
        db_backends.DatabaseError("The database could not complete the request.", "query")))
    monkeypatch.setattr(pages, "_open_db", lambda: db)
    at = AppTest.from_string(script("pages._run_analysis(pages.get_db(), 'doc text', 'f.pdf')\n"),
                             default_timeout=30).run()
    assert not at.exception
    assert any("could not be saved" in e.value for e in at.error)


# ------------------------------------------------------------ rerun safety
def test_repeated_analyze_clicks_do_not_duplicate_records_or_ai_calls(monkeypatch, tmp_path):
    import pages
    db = Database(tmp_path / "once.db")
    calls = {"n": 0}

    def fake_analyze(text, client=None, **kwargs):
        calls["n"] += 1
        return ai_engine.validate_analysis({"title": "Once", "summary": "S",
                                            "deadlines": [{"description": "D", "date": "2026-12-01",
                                                           "date_type": "explicit", "source_text": "s"}],
                                            "action_items": [{"title": "Do it", "related_deadline": "D"}]})

    monkeypatch.setattr(ai_engine, "build_client", lambda: object())
    monkeypatch.setattr(ai_engine, "analyze_notice", fake_analyze)
    monkeypatch.setattr(pages, "_open_db", lambda: db)
    body = "for _ in range(3):\n    pages._run_analysis(pages.get_db(), 'same document', 'a.pdf')\n"
    at = AppTest.from_string(script(body), default_timeout=30).run()
    assert not at.exception
    assert calls["n"] == 1 and db.table_counts() == {"notices": 1, "deadlines": 1, "action_items": 1}


def test_database_object_is_created_once_per_process(monkeypatch, tmp_path):
    import pages
    created = []
    real_init = Database.__init__

    def counting_init(self, *a, **k):
        created.append(1)
        real_init(self, *a, **k)

    monkeypatch.setattr(Database, "__init__", counting_init)
    for _ in range(4):                                   # four simulated reruns / sessions
        AppTest.from_string(script("pages.get_db().count_notices()\n"), default_timeout=30).run()
    assert len(created) == 1                             # st.cache_resource: one pool, not one per rerun


# --------------------------------------------------------------- password gate
def test_password_matching_is_safe():
    assert auth.password_matches("s3cret", "s3cret")
    assert not auth.password_matches("S3cret", "s3cret")
    assert not auth.password_matches("", "s3cret") and not auth.password_matches("x", None)
    assert not auth.password_matches("x" * 10_000, "x" * 10_000)             # oversize input rejected


def test_app_is_open_when_no_password_configured():
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception and "Welcome to Notice2Action AI" in md(at)
    assert not any(b.label == "Sign out" for b in at.sidebar.button)


def test_gate_blocks_everything_until_the_right_password(monkeypatch, tmp_path):
    Database(tmp_path / "app.db").save_notice(ai_engine.validate_analysis({"title": "Private notice"}), NOTICE_TEXT)
    monkeypatch.setenv("APP_PASSWORD", "correct horse")
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception
    assert any("protected" in c.value for c in at.caption)
    assert at.text_input[0].proto.type == 1
    assert "Private notice" not in md(at) and "Welcome to Notice2Action AI" not in md(at)
    assert not at.sidebar.markdown                                           # no navigation, no status, nothing

    at.text_input[0].set_value("wrong password")
    at.button[0].click().run()
    assert any("Incorrect password" in e.value for e in at.error)
    assert "Welcome to Notice2Action AI" not in md(at)

    at.text_input[0].set_value("correct horse")
    at.button[0].click().run()
    assert not at.exception and "Welcome to Notice2Action AI" in md(at)
    assert any(b.label == "Sign out" for b in at.sidebar.button)

    next(b for b in at.sidebar.button if b.label == "Sign out").click().run()
    assert any("protected" in c.value for c in at.caption) and "Welcome to Notice2Action AI" not in md(at)


def test_gate_applies_from_streamlit_secrets_too(monkeypatch):
    import config
    monkeypatch.setattr(config, "_secret", lambda name: "from-secrets" if name == "APP_PASSWORD" else None)
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert at.text_input[0].proto.type == 1
