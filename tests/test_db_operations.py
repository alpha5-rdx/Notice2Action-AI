"""Behaviour that must be identical on SQLite and PostgreSQL (the `db` fixture runs both)."""
import threading
from contextlib import contextmanager
from datetime import date, timedelta

import pytest

import ai_engine
from conftest import NOTICE_TEXT, make_gemini_payload


def analysis(**overrides):
    return ai_engine.validate_analysis(make_gemini_payload(**overrides))


# ------------------------------------------------------------------ transactions
def test_save_notice_is_atomic(db, monkeypatch):
    """A failure while writing the children must roll back the notice and deadlines too."""
    real_connect = db.backend.connect

    @contextmanager
    def failing_connect():
        with real_connect() as session:
            class Proxy:
                def execute(self, sql, params=()):
                    if "INSERT INTO action_items" in sql:
                        raise RuntimeError("simulated failure")
                    return session.execute(sql, params)
            yield Proxy()

    monkeypatch.setattr(db.backend, "connect", failing_connect)
    with pytest.raises(RuntimeError):
        db.save_notice(analysis(), NOTICE_TEXT, "n.pdf")
    monkeypatch.undo()
    assert db.table_counts() == {"notices": 0, "deadlines": 0, "action_items": 0}
    assert db.save_notice(analysis(), NOTICE_TEXT, "n.pdf")[1] is True        # and a retry works


def test_failed_update_changes_nothing(db):
    nid, _ = db.save_notice(analysis(), NOTICE_TEXT, None)
    task = db.list_tasks(nid)[0]
    with pytest.raises(ValueError):
        db.update_task(task["id"], title="Changed", priority="urgent")
    assert db.get_task(task["id"])["title"] == task["title"]


# ----------------------------------------------------------- duplicates / reruns
def test_repeated_saves_never_duplicate(db):
    results = [db.save_notice(analysis(), NOTICE_TEXT, "n.pdf") for _ in range(4)]   # like repeated reruns
    assert len({r[0] for r in results}) == 1 and [r[1] for r in results] == [True, False, False, False]
    assert db.table_counts() == {"notices": 1, "deadlines": 2, "action_items": 2}


def test_concurrent_saves_create_exactly_one_record(db):
    outcomes, errors = [], []

    def worker():
        try:
            outcomes.append(db.save_notice(analysis(), NOTICE_TEXT, "n.pdf"))
        except Exception as exc:  # pragma: no cover - failure path is asserted below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors, errors
    assert sum(1 for _, created in outcomes if created) == 1
    assert db.count_notices() == 1 and db.table_counts()["deadlines"] == 2     # children written once


# --------------------------------------------------------------------- data fidelity
def test_analysis_round_trips_exactly(db):
    a = analysis(eligibility=["Ünïcödé ✓", "اردو متن"], confidence_notes=["emoji 🎓 ok"])
    nid, _ = db.save_notice(a, NOTICE_TEXT + " 🎓 اردو", "ñ.pdf")
    saved = db.get_notice(nid)
    assert saved["analysis"] == a and saved["file_name"] == "ñ.pdf"
    assert saved["extracted_text"].endswith("🎓 اردو")


def test_nul_characters_from_ocr_are_removed(db):
    nid, _ = db.save_notice(analysis(summary="bad\x00summary"), "text with\x00nul", "f.pdf")
    saved = db.get_notice(nid)
    assert "\x00" not in saved["extracted_text"] and saved["summary"] == "badsummary"
    assert db.find_duplicate("text with\x00nul") == nid and db.find_duplicate("text withnul") == nid


def test_dates_and_timestamps_have_stable_formats(db):
    nid, _ = db.save_notice(analysis(), NOTICE_TEXT, None)
    notice = db.get_notice(nid)
    assert notice["announcement_date"] == "2026-11-01"
    date.fromisoformat(notice["created_at"][:10])
    assert len(notice["created_at"]) == 19 and notice["created_at"][10] == "T"
    task = db.list_tasks(nid)[0]
    assert task["due_date"] == "2026-12-15" and task["created_at"][10] == "T" and task["completed_at"] is None
    db.set_task_status(task["id"], "completed")
    assert db.get_task(task["id"])["completed_at"][10] == "T"


def test_blank_and_oversized_values(db):
    with pytest.raises(ValueError):
        db.save_notice({"title": "   "}, "x", None)
    nid, _ = db.save_notice(analysis(title="T" * 5000, venue="V" * 5000), NOTICE_TEXT, None)
    saved = db.get_notice(nid)
    assert len(saved["title"]) == 500 and len(saved["venue"]) == 300


# ----------------------------------------------------------------- search / filters
def test_search_is_case_insensitive_and_literal(db):
    db.save_notice(analysis(title="Hostel Fee 100% Notice_A"), "one", None)
    db.save_notice(analysis(title="Exam Schedule"), "two", None)
    assert [n["title"] for n in db.list_notices(search="HOSTEL")] == ["Hostel Fee 100% Notice_A"]
    assert [n["title"] for n in db.list_notices(search="100%")] == ["Hostel Fee 100% Notice_A"]
    assert [n["title"] for n in db.list_notices(search="notice_a")] == ["Hostel Fee 100% Notice_A"]
    assert db.list_notices(search="%") == [n for n in db.list_notices(search="%")] and len(db.list_notices(search="%")) == 1
    assert db.list_notices(search="x" * 5000) == []                       # oversized search is truncated safely
    assert db.list_notices(search="'; DROP TABLE notices; --") == [] and db.count_notices() == 2


def test_date_range_filter_boundaries(db):
    db.save_notice(analysis(), NOTICE_TEXT, None)
    today = date.today()
    assert len(db.list_notices(date_from=today, date_to=today)) == 1
    assert len(db.list_notices(date_from=today - timedelta(days=1), date_to=today + timedelta(days=1))) == 1
    assert db.list_notices(date_from=today + timedelta(days=1)) == []
    assert db.list_notices(date_to=today - timedelta(days=1)) == []


def test_category_listing_and_ordering(db):
    db.save_notice(analysis(category="Exam"), "a", None)
    db.save_notice(analysis(category="Exam"), "b", None)
    db.save_notice(analysis(category="Event"), "c", None)
    assert db.category_counts() == {"Exam": 2, "Event": 1} and db.list_categories() == ["Event", "Exam"]
    assert [n["id"] for n in db.list_notices()] == sorted([n["id"] for n in db.list_notices()], reverse=True)


# --------------------------------------------------------------- deadlines / tasks
def test_upcoming_and_overdue_queries(db):
    nid, _ = db.save_notice(analysis(), NOTICE_TEXT, None)
    assert db.count_upcoming_deadlines(date(2026, 12, 15)) == 1            # inclusive
    assert db.count_upcoming_deadlines(date(2026, 12, 16)) == 0
    deadlines = db.list_deadlines(nid)
    assert [d["deadline_date"] for d in deadlines] == ["2026-12-15", None]  # dated first, undated last
    assert deadlines[0]["notice_title"] == "Semester Registration Notice"
    tasks = db.list_tasks(nid)
    assert [t["due_date"] for t in tasks] == ["2026-12-15", None]


def test_task_edit_and_status_cycle(db):
    nid, _ = db.save_notice(analysis(), NOTICE_TEXT, None)
    task = db.list_tasks(nid)[1]
    assert db.update_task(task["id"], title="  New title  ", description="", priority="high",
                          due_date="2027-02-01") is True
    edited = db.get_task(task["id"])
    assert (edited["title"], edited["description"], edited["priority"]) == ("New title", None, "high")
    assert (edited["due_date"], edited["due_date_type"]) == ("2027-02-01", "manual")
    db.set_task_status(task["id"], "completed")
    db.set_task_status(task["id"], "pending")
    again = db.get_task(task["id"])
    assert again["status"] == "pending" and again["completed_at"] is None and again["updated_at"] >= edited["updated_at"]
    assert db.update_task(task["id"], due_date=None) and db.get_task(task["id"])["due_date_type"] == "none"
    assert db.update_task(987654, title="nobody") is False


def test_cascade_and_set_null_behaviour(db):
    nid, _ = db.save_notice(analysis(), NOTICE_TEXT, None)
    other, _ = db.save_notice(analysis(), "another notice", None)
    assert db.delete_notice(nid) is True
    assert db.table_counts() == {"notices": 1, "deadlines": 2, "action_items": 2}
    assert all(t["notice_id"] == other for t in db.list_tasks())
    # removing a single deadline keeps the task but clears the link (SET NULL)
    deadline_id = db.list_tasks(other)[0]["deadline_id"]
    with db.connect() as s:
        s.execute("DELETE FROM deadlines WHERE id = ?", (deadline_id,))
    assert db.list_tasks(other)[0]["deadline_id"] is None and len(db.list_tasks(other)) == 2


def test_unknown_ids_are_handled(db):
    assert db.get_notice(424242) is None and db.get_task(424242) is None
    assert db.delete_notice(424242) is False and db.find_duplicate("never saved") is None


def test_schema_init_is_repeatable(db):
    first = db.init_schema()
    assert db.init_schema() == [] and isinstance(first, list)
    assert db.table_counts() == {"notices": 0, "deadlines": 0, "action_items": 0}


def test_check_reports_version(db):
    info = db.check()
    assert info["version"] and info["seconds"] >= 0
    assert db.describe()["kind"] in {"sqlite", "postgres"}
