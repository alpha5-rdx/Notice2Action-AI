"""UI behaviour: flash messages, delete/update confirmations, sidebar text, scroll panels."""
import pytest
from streamlit.testing.v1 import AppTest

import ai_engine
from conftest import NOTICE_TEXT


def _script(body):
    return "import sys; sys.path.insert(0, r'.')\nimport streamlit as st, ui, pages\n" + body


@pytest.fixture(autouse=True)
def _fresh_db_cache():
    import streamlit as st
    st.cache_resource.clear()
    yield
    st.cache_resource.clear()


@pytest.fixture
def seeded(tmp_path, monkeypatch, payload):
    monkeypatch.setenv("NOTICE2ACTION_DB", str(tmp_path / "ui.db"))
    from database import Database
    db = Database()
    db.save_notice(ai_engine.validate_analysis(payload), NOTICE_TEXT, "n.pdf")
    return db


def _markdown(at):
    return " ".join(m.value for m in at.markdown)


# ------------------------------------------------------------------ sidebar text
def test_sidebar_footer_text_updated(seeded):
    from pathlib import Path
    app_path = Path(__file__).resolve().parent.parent / "app.py"
    at = AppTest.from_file(str(app_path), default_timeout=30).run()
    assert not at.exception
    text = " ".join(m.value for m in at.sidebar.markdown)
    assert "v1.0.0 | All rights reserved" in text
    assert "AI can make mistakes" not in text


# -------------------------------------------------------------- deletion message
def test_delete_shows_temporary_message_and_removes_notice(seeded):
    script = _script("pages._delete_notice(1)\npages.page_notices()\n")
    at = AppTest.from_string(script, default_timeout=30).run()
    assert not at.exception
    banner = _markdown(at)
    assert "Notice deleted successfully" in banner and "n2a-flash success" in banner
    assert seeded.count_notices() == 0


def test_deleting_missing_notice_warns(seeded):
    at = AppTest.from_string(_script("pages._delete_notice(999)\npages.page_notices()\n"), default_timeout=30).run()
    assert "That notice no longer exists" in _markdown(at) and "n2a-flash warning" in _markdown(at)


def test_flash_is_shown_once_only(seeded):
    script = _script("ui.flash('Hello once')\nui.render_flash()\nui.render_flash()\n")
    at = AppTest.from_string(script, default_timeout=30).run()
    assert _markdown(at).count("Hello once") == 1


def test_flash_banner_is_escaped():
    at = AppTest.from_string(_script("ui.flash('<script>alert(1)</script>')\nui.render_flash()\n"),
                             default_timeout=30).run()
    assert "<script>alert" not in _markdown(at) and "&lt;script&gt;" in _markdown(at)


def test_flash_css_auto_fades_and_floats():
    css = ui_css()
    assert "position:fixed" in css and "animation:n2a-flash 6s" in css and "@keyframes n2a-flash" in css


def ui_css():
    import ui
    return ui.CSS


# -------------------------------------------------------------- updation message
def test_save_changes_shows_updated_message(seeded):
    at = AppTest.from_string(_script("pages.page_tasks()\n"), default_timeout=30).run()
    assert not at.exception
    task = seeded.list_tasks()[0]
    next(t for t in at.text_input if t.label == "Title" and t.value == task["title"]).set_value("Renamed by test")
    next(b for b in at.button if b.label == "Save changes").click().run()
    assert not at.exception
    assert "Notice updated successfully" in _markdown(at)
    assert seeded.get_task(task["id"])["title"] == "Renamed by test"


def test_complete_and_reopen_show_messages(seeded):
    at = AppTest.from_string(_script("pages.page_tasks()\n"), default_timeout=30).run()
    next(b for b in at.button if b.label == "Mark complete").click().run()
    assert "Task marked as completed" in _markdown(at)
    next(b for b in at.button if b.label == "Reopen").click().run()
    assert "Task reopened" in _markdown(at)


def test_invalid_edit_does_not_flash_success(seeded):
    at = AppTest.from_string(_script("pages.page_tasks()\n"), default_timeout=30).run()
    task = seeded.list_tasks()[0]
    next(t for t in at.text_input if t.label == "Title" and t.value == task["title"]).set_value("   ")
    next(b for b in at.button if b.label == "Save changes").click().run()
    assert "Notice updated successfully" not in _markdown(at)
    assert any("Title cannot be empty" in e.value for e in at.error)


# ---------------------------------------------------- scroll panels / equal boxes
def test_dashboard_lists_all_notices_inside_scroll_panels(seeded, payload):
    for i in range(12):  # many entries must not grow the page: they live in fixed-height panels
        seeded.save_notice(ai_engine.validate_analysis(payload), f"another notice {i}", None)
    at = AppTest.from_string(_script("pages.page_dashboard()\n"), default_timeout=30).run()
    assert not at.exception
    assert len([b for b in at.button if b.label == "View"]) == 13  # no 5-item cap any more


def test_panel_constants_are_fixed_heights():
    import pages
    for name in ("DASHBOARD_PANEL_H", "LIST_PANEL_H", "SECTION_BOX_H", "SETTINGS_BOX_H"):
        assert isinstance(getattr(pages, name), int) and getattr(pages, name) > 100
    assert pages.table_height(1) < pages.table_height(50) == 340  # small tables fit, big ones scroll


def test_clamp_and_title_helpers_escape_and_pad():
    import ui
    assert "&lt;b&gt;" in ui.clamp("<b>x</b>") and "&nbsp;" in ui.clamp(None)  # empty still reserves its lines
    assert "n2a-clamp one" in ui.clamp("x", lines=1)
    assert "&lt;i&gt;" in ui.title_html("<i>t</i>")


def test_all_pages_still_render_with_many_entries(seeded, payload):
    for i in range(30):
        seeded.save_notice(ai_engine.validate_analysis(payload), f"bulk {i}", None)
    for page in ("page_dashboard", "page_notices", "page_tasks", "page_settings"):
        at = AppTest.from_string(_script(f"pages.{page}()\n"), default_timeout=30).run()
        assert not at.exception, (page, [e.value for e in at.exception])
