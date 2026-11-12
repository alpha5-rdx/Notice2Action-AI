"""Smoke test: the whole Streamlit app running on PostgreSQL (DATABASE_URL), driven through the UI."""
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

import ai_engine
from conftest import NOTICE_TEXT

APP = str(Path(__file__).resolve().parent.parent / "app.py")
PAGES = ["page_dashboard", "page_analyze", "page_notices", "page_tasks", "page_settings"]


def script(body):
    return "import sys; sys.path.insert(0, r'.')\nimport streamlit as st, ui, pages\n" + body


def md(at):
    return " ".join(m.value for m in at.markdown)


@pytest.fixture
def pg_app(pg_db, pg_test_url, monkeypatch, payload):
    """App configured with DATABASE_URL, pre-loaded with one notice (2 deadlines, 2 tasks)."""
    monkeypatch.setenv("DATABASE_URL", pg_test_url)
    monkeypatch.setenv("NOTICE2ACTION_DB", "/nonexistent/must-not-be-used.db")     # proves SQLite is not used
    st.cache_resource.clear()
    pg_db.save_notice(ai_engine.validate_analysis(payload), NOTICE_TEXT, "n.pdf")
    yield pg_db
    try:
        st.cache_resource.clear()
    except Exception:
        pass


def test_app_boots_on_postgres_with_status_in_sidebar(pg_app):
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception, [e.value for e in at.exception]
    assert "Welcome to Notice2Action AI" in md(at)
    sidebar = " ".join(m.value for m in at.sidebar.markdown)
    assert "PostgreSQL" in sidebar and "Local SQLite" not in sidebar
    assert "All rights reserved" in sidebar


@pytest.mark.parametrize("page", PAGES)
def test_every_page_renders_on_postgres(pg_app, page):
    at = AppTest.from_string(script(f"pages.{page}()\n"), default_timeout=30).run()
    assert not at.exception, [e.value for e in at.exception]


def test_dashboard_numbers_come_from_postgres(pg_app):
    at = AppTest.from_string(script("pages.page_dashboard()\n"), default_timeout=30).run()
    text = md(at)
    assert "Semester Registration Notice" in text and "Analyzed notices" in text
    assert any(b.label == "View" for b in at.button)


def test_complete_edit_and_reopen_tasks_persist_in_postgres(pg_app):
    at = AppTest.from_string(script("pages.page_tasks()\n"), default_timeout=30).run()
    next(b for b in at.button if b.label == "Mark complete").click().run()
    assert "Task marked as completed" in md(at)
    assert any(t["status"] == "completed" for t in pg_app.list_tasks())

    task = pg_app.list_tasks()[0]          # the first card owns the first "Save changes" form
    next(t for t in at.text_input if t.label == "Title" and t.value == task["title"]).set_value("Edited on PG")
    next(b for b in at.button if b.label == "Save changes").click().run()
    assert "Notice updated successfully" in md(at)
    assert pg_app.get_task(task["id"])["title"] == "Edited on PG"

    next(b for b in at.button if b.label == "Reopen").click().run()
    assert all(t["status"] == "pending" for t in pg_app.list_tasks())


def test_delete_notice_cascades_in_postgres_and_shows_message(pg_app):
    at = AppTest.from_string(script("pages._delete_notice(1)\npages.page_notices()\n"), default_timeout=30).run()
    assert "Notice deleted successfully" in md(at)
    assert pg_app.table_counts() == {"notices": 0, "deadlines": 0, "action_items": 0}


def test_notice_detail_view_and_search_on_postgres(pg_app):
    at = AppTest.from_string(script("pages.page_notices()\n"), default_timeout=30)
    at.run()
    at.text_input[0].set_value("REGISTRATION").run()                               # ILIKE: case-insensitive
    assert any(b.label == "View" for b in at.button)
    at.text_input[0].set_value("no such notice").run()
    assert "No matching notices" in md(at)
    detail = AppTest.from_string(script("pages.page_notices()\n"), default_timeout=30)
    detail.session_state["selected_notice"] = 1
    detail.run()
    assert not detail.exception and "Semester Registration Notice" in md(detail)


def test_settings_shows_postgres_without_leaking_the_connection_string(pg_app, pg_test_url):
    at = AppTest.from_string(script("pages.page_settings()\n"), default_timeout=30).run()
    assert not at.exception
    page = md(at) + " ".join(s.value for s in at.success) + " ".join(c.value for c in at.caption)
    assert any("Connected to" in s.value for s in at.success)
    assert pg_test_url not in page and "postgresql://" not in page.lower()
    next(b for b in at.button if b.label == "Test database connection").click().run()
    assert any("answered in" in s.value for s in at.success)


def test_sqlite_fallback_warning_without_database_url(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTICE2ACTION_DB", str(tmp_path / "local.db"))
    st.cache_resource.clear()
    at = AppTest.from_string(script("pages.page_settings()\n"), default_timeout=30).run()
    assert any("local SQLite fallback" in w.value for w in at.warning)
    sidebar = AppTest.from_file(APP, default_timeout=30).run().sidebar
    assert "Local SQLite" in " ".join(m.value for m in sidebar.markdown)
    st.cache_resource.clear()
