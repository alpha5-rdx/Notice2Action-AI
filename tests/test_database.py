from datetime import date

import pytest

import ai_engine
import tasks as task_logic
from conftest import NOTICE_TEXT, make_gemini_payload


@pytest.fixture
def analysis(payload):
    return ai_engine.validate_analysis(payload)


def save(db, analysis, text=NOTICE_TEXT):
    return db.save_notice(analysis, text, "notice.pdf")


# ---- Notices ----------------------------------------------------------------
def test_save_and_get_notice(db, analysis):
    notice_id, created = save(db, analysis)
    assert created is True
    notice = db.get_notice(notice_id)
    assert notice["title"] == analysis["title"]
    assert notice["analysis"]["deadlines"][0]["date"] == "2026-12-15"
    assert notice["created_at"]


def test_duplicate_notice_is_not_saved_twice(db, analysis):
    first, created1 = save(db, analysis)
    second, created2 = save(db, analysis, "  " + NOTICE_TEXT.upper() + "\n\n")  # whitespace/case-insensitive
    assert (first, created1, created2) == (first, True, False)
    assert second == first
    assert db.count_notices() == 1


def test_search_and_filters(db, analysis, payload):
    save(db, analysis)
    other = ai_engine.validate_analysis(make_gemini_payload(title="Hostel Fee Notice", category="Fee Payment"))
    db.save_notice(other, "different text", "b.png")
    assert len(db.list_notices()) == 2
    assert [n["title"] for n in db.list_notices(search="hostel")] == ["Hostel Fee Notice"]
    assert [n["title"] for n in db.list_notices(category="Fee Payment")] == ["Hostel Fee Notice"]
    assert len(db.list_notices(date_from=date.today(), date_to=date.today())) == 2
    assert db.list_notices(date_to=date(2000, 1, 1)) == []
    assert db.list_notices(search="100%") == []  # wildcard characters are escaped
    assert set(db.list_categories()) == {"Registration", "Fee Payment"}


def test_search_is_parameterised(db, analysis):
    save(db, analysis)
    assert db.list_notices(search="'; DROP TABLE notices; --") == []
    assert db.count_notices() == 1


def test_delete_cascades_to_deadlines_and_tasks(db, analysis):
    notice_id, _ = save(db, analysis)
    assert db.table_counts() == {"notices": 1, "deadlines": 2, "action_items": 2}
    assert db.delete_notice(notice_id) is True
    assert db.table_counts() == {"notices": 0, "deadlines": 0, "action_items": 0}
    assert db.delete_notice(notice_id) is False


def test_delete_only_affects_target_notice(db, analysis):
    a, _ = save(db, analysis)
    b, _ = db.save_notice(analysis, "another notice", None)
    db.delete_notice(a)
    assert db.count_notices() == 1
    assert len(db.list_tasks(notice_id=b)) == 2


# ---- Deadlines --------------------------------------------------------------
def test_deadlines_are_stored_with_types(db, analysis):
    notice_id, _ = save(db, analysis)
    deadlines = db.list_deadlines(notice_id)
    dated = [d for d in deadlines if d["deadline_date"]]
    assert dated[0]["date_type"] == "explicit" and dated[0]["deadline_time"] == "17:00"
    undated = [d for d in deadlines if not d["deadline_date"]]
    assert undated[0]["date_type"] == "unspecified"
    assert db.count_upcoming_deadlines(date(2026, 11, 12)) == 1
    assert db.count_upcoming_deadlines(date(2027, 1, 1)) == 0


# ---- Tasks ------------------------------------------------------------------
def test_tasks_created_with_deadline_links(db, analysis):
    notice_id, _ = save(db, analysis)
    tasks = {t["title"]: t for t in db.list_tasks(notice_id)}
    linked = tasks["Submit registration documents"]
    assert linked["due_date"] == "2026-12-15" and linked["due_date_type"] == "explicit"
    assert linked["deadline_id"] is not None and linked["status"] == "pending"
    free = tasks["Check late fee rules"]
    assert free["due_date"] is None and free["due_date_type"] == "none"


def test_inferred_deadline_carries_over_to_task(db, payload):
    payload["deadlines"][0]["date_type"] = "inferred"
    notice_id, _ = save(db, ai_engine.validate_analysis(payload))
    task = [t for t in db.list_tasks(notice_id) if t["due_date"]][0]
    assert task["due_date_type"] == "inferred"


def test_status_updates(db, analysis):
    notice_id, _ = save(db, analysis)
    task = db.list_tasks(notice_id)[0]
    assert db.set_task_status(task["id"], "completed")
    done = db.get_task(task["id"])
    assert done["status"] == "completed" and done["completed_at"]
    db.set_task_status(task["id"], "pending")
    assert db.get_task(task["id"])["completed_at"] is None


def test_manual_task_editing(db, analysis):
    notice_id, _ = save(db, analysis)
    task = [t for t in db.list_tasks(notice_id) if t["due_date"] is None][0]
    db.update_task(task["id"], title="Renamed", priority="high", due_date="2027-01-05")
    edited = db.get_task(task["id"])
    assert edited["title"] == "Renamed" and edited["priority"] == "high"
    assert edited["due_date"] == "2027-01-05" and edited["due_date_type"] == "manual"
    db.update_task(task["id"], due_date=None)
    assert db.get_task(task["id"])["due_date_type"] == "none"


def test_invalid_task_updates_rejected(db, analysis):
    notice_id, _ = save(db, analysis)
    tid = db.list_tasks(notice_id)[0]["id"]
    with pytest.raises(ValueError):
        db.update_task(tid, notice_id=99)
    with pytest.raises(ValueError):
        db.update_task(tid, title="  ")
    with pytest.raises(ValueError):
        db.update_task(tid, due_date="not-a-date")
    with pytest.raises(ValueError, match="Priority"):
        db.update_task(tid, priority="urgent")
    with pytest.raises(ValueError, match="Status"):
        db.update_task(tid, status="done")


# ---- Status logic -----------------------------------------------------------
def test_effective_status_and_counts(db, analysis):
    notice_id, _ = save(db, analysis)
    tasks = db.list_tasks(notice_id)
    before = task_logic.task_counts(tasks, date(2026, 12, 1))
    assert before == {"pending": 2, "completed": 0, "overdue": 0}
    after = task_logic.task_counts(tasks, date(2026, 12, 20))
    assert after == {"pending": 1, "completed": 0, "overdue": 1}
    due = [t for t in tasks if t["due_date"]][0]
    db.set_task_status(due["id"], "completed")
    final = task_logic.task_counts(db.list_tasks(notice_id), date(2026, 12, 20))
    assert final == {"pending": 1, "completed": 1, "overdue": 0}


def test_filters_and_labels(db, analysis):
    notice_id, _ = save(db, analysis)
    annotated = task_logic.annotate_tasks(db.list_tasks(notice_id), date(2026, 12, 20))
    assert [t["title"] for t in task_logic.filter_tasks(annotated, status="Overdue")] == ["Submit registration documents"]
    assert [t["title"] for t in task_logic.filter_tasks(annotated, priority="Low")] == ["Check late fee rules"]
    assert len(task_logic.filter_tasks(annotated, deadline_kind="No deadline")) == 1
    assert len(task_logic.filter_tasks(annotated, deadline_kind="Confirmed")) == 1
    assert task_logic.filter_tasks(annotated, deadline_kind="Inferred") == []
    assert task_logic.deadline_label("explicit") == "Confirmed"
    assert "verify" in task_logic.deadline_label("inferred")
    assert task_logic.deadline_label("unspecified", has_date=False) == "Date not specified"
    assert task_logic.describe_due("2026-12-15", date(2026, 12, 14)) == "In 1 day(s)"
    assert task_logic.describe_due("2026-12-15", date(2026, 12, 15)) == "Due today"
    assert task_logic.describe_due(None) == "No date"


# ---- End-to-end with mocked Gemini ------------------------------------------
def test_pipeline_mocked_gemini_to_database(db, fake_client, payload_json):
    result = ai_engine.analyze_notice(NOTICE_TEXT, client=fake_client(payload_json))
    notice_id, created = db.save_notice(result, NOTICE_TEXT, "n.pdf")
    assert created and len(db.list_tasks(notice_id)) == 2
    assert db.category_counts() == {"Registration": 1}
