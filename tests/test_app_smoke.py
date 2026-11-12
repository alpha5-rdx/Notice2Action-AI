"""Render every page headlessly with Streamlit's AppTest to catch wiring errors."""
import pytest
from streamlit.testing.v1 import AppTest

import ai_engine
from conftest import NOTICE_TEXT

PAGES = ["page_dashboard", "page_analyze", "page_notices", "page_tasks", "page_settings"]


def _script(page):
    return (
        "import sys; sys.path.insert(0, r'.')\n"
        "import ui, pages\n"
        "ui.inject_css()\n"
        f"pages.{page}()\n"
    )


@pytest.fixture(autouse=True)
def _fresh_db_cache():
    """pages.get_db is cached process-wide; reset it so each test uses its own DB."""
    import streamlit as st
    st.cache_resource.clear()
    yield
    st.cache_resource.clear()


@pytest.fixture
def seeded(tmp_path, monkeypatch, payload):
    monkeypatch.setenv("NOTICE2ACTION_DB", str(tmp_path / "smoke.db"))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    from database import Database
    db = Database()
    db.save_notice(ai_engine.validate_analysis(payload), NOTICE_TEXT, "n.pdf")
    return db


@pytest.mark.parametrize("page", PAGES)
def test_page_renders_with_data(seeded, page):
    at = AppTest.from_string(_script(page), default_timeout=30).run()
    assert not at.exception, [e.value for e in at.exception]


@pytest.mark.parametrize("page", PAGES)
def test_page_renders_with_empty_database(tmp_path, monkeypatch, page):
    monkeypatch.setenv("NOTICE2ACTION_DB", str(tmp_path / "empty.db"))
    at = AppTest.from_string(_script(page), default_timeout=30).run()
    assert not at.exception, [e.value for e in at.exception]


def test_notice_detail_view_renders(seeded):
    at = AppTest.from_string(_script("page_notices"), default_timeout=30)
    at.session_state["selected_notice"] = 1
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("Semester Registration Notice" in m.value for m in at.markdown)


def test_gemini_failure_shows_actionable_error_and_details(seeded, monkeypatch):
    import ai_engine
    import gemini_errors as ge
    info = ge.ErrorInfo(ge.NOT_FOUND, 404, "NOT_FOUND", "gone", "ClientError")
    err = ai_engine.AIServiceError("Model 'x' was not found. Set GEMINI_MODEL.", info, "x", 1)

    def fail(*args, **kwargs):
        raise err
    monkeypatch.setattr(ai_engine, "build_client", lambda: object())
    monkeypatch.setattr(ai_engine, "analyze_notice", fail)
    script = ("import sys; sys.path.insert(0, r'.')\nimport pages\n"
              "pages._run_analysis(pages.get_db(), 'brand new text', 'f.pdf')\n")
    at = AppTest.from_string(script, default_timeout=30).run()
    assert not at.exception
    assert "was not found" in at.error[0].value
    assert any(e.label == "Technical details" for e in at.expander)


def test_complete_task_button_updates_database(seeded):
    at = AppTest.from_string(_script("page_tasks"), default_timeout=30).run()
    next(b for b in at.button if b.label == "Mark complete").click().run()
    assert not at.exception
    assert any(t["status"] == "completed" for t in seeded.list_tasks())
