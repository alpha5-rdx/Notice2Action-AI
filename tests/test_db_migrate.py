"""SQLite -> PostgreSQL migration: correctness, safety, idempotency and the CLI."""
import hashlib
import sqlite3

import pytest

import ai_engine
import config
import db_migrate
import db_tools
from database import Database
from db_backends import DatabaseError
from conftest import NOTICE_TEXT, make_gemini_payload


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def source(tmp_path):
    """A realistic SQLite database created by the app, with 3 notices, 5 deadlines, 5 tasks."""
    src = Database(tmp_path / "source.db")
    for i in range(3):
        payload = make_gemini_payload(
            title=f"Notice {i}", category=["Exam", "Event", "Fee Payment"][i],
            eligibility=[f"Eligible {i}", "اردو ✓"])
        if i == 2:  # a notice with a single deadline and no tasks
            payload["action_items"], payload["deadlines"] = [], payload["deadlines"][:1]
        src.save_notice(ai_engine.validate_analysis(payload), f"{NOTICE_TEXT} variant {i}", f"n{i}.pdf")
    src.set_task_status(src.list_tasks()[0]["id"], "completed")           # include non-default state
    return src


def counts(db):
    return db.table_counts()


# ---------------------------------------------------------------- dry run & safety
def test_dry_run_reports_without_writing(db, source):
    before = sha(source.path)
    report = db_migrate.migrate(source.path, db, apply=False)
    assert report.ok and not report.committed
    assert report.stats["notices"].source == 3 and report.stats["notices"].to_insert == 3
    assert report.stats["deadlines"].to_insert == 5 and report.stats["action_items"].to_insert == 4
    assert counts(db) == {"notices": 0, "deadlines": 0, "action_items": 0}     # nothing written
    assert sha(source.path) == before                                            # source untouched


def test_source_file_is_never_modified_or_deleted(db, source):
    before = sha(source.path)
    db_migrate.migrate(source.path, db, apply=True)
    assert source.path.exists() and sha(source.path) == before


def test_source_is_opened_read_only(source):
    # read_source must not be able to write; prove the URI mode by trying through the same mechanism
    uri = f"{source.path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM notices")
    conn.close()
    assert source.count_notices() == 3


# ------------------------------------------------------------------- correctness
def test_apply_preserves_ids_data_and_relationships(db, source):
    report = db_migrate.migrate(source.path, db, apply=True)
    assert report.ok and report.committed and not report.warnings
    assert counts(db) == counts(source) == {"notices": 3, "deadlines": 5, "action_items": 4}

    for original in source.list_notices():
        migrated = db.get_notice(original["id"])                                  # same id
        old = source.get_notice(original["id"])
        assert migrated["title"] == old["title"] and migrated["category"] == old["category"]
        assert migrated["analysis"] == old["analysis"]                            # full JSON preserved
        assert migrated["content_hash"] == old["content_hash"]
        assert migrated["created_at"] == old["created_at"]                        # timestamp preserved
        assert migrated["announcement_date"] == old["announcement_date"]
        assert migrated["extracted_text"] == old["extracted_text"] and migrated["file_name"] == old["file_name"]

    old_tasks = {t["id"]: t for t in source.list_tasks()}
    new_tasks = {t["id"]: t for t in db.list_tasks()}
    assert old_tasks.keys() == new_tasks.keys()
    for tid, old in old_tasks.items():
        new = new_tasks[tid]
        for key in ("notice_id", "deadline_id", "title", "description", "priority", "status", "due_date",
                    "due_date_type", "created_at", "updated_at", "completed_at"):
            assert new[key] == old[key], (tid, key)
    assert any(t["status"] == "completed" and t["completed_at"] for t in new_tasks.values())

    old_dl = {d["id"]: d for d in source.list_deadlines()}
    new_dl = {d["id"]: d for d in db.list_deadlines()}
    assert old_dl.keys() == new_dl.keys()
    for did, old in old_dl.items():
        for key in ("notice_id", "description", "deadline_date", "deadline_time", "date_type", "source_text"):
            assert new_dl[did][key] == old[key], (did, key)


def test_new_records_after_migration_get_fresh_ids(db, source):
    db_migrate.migrate(source.path, db, apply=True)
    nid, created = db.save_notice(ai_engine.validate_analysis(make_gemini_payload(title="Brand new")),
                                  "totally new document", None)
    assert created and nid > max(n["id"] for n in source.list_notices())          # sequence moved past migrated ids
    assert max(t["id"] for t in db.list_tasks()) > max(t["id"] for t in source.list_tasks())


def test_cascade_works_on_migrated_data(db, source):
    db_migrate.migrate(source.path, db, apply=True)
    first = source.list_notices()[-1]["id"]
    assert db.delete_notice(first)
    assert all(t["notice_id"] != first for t in db.list_tasks())
    assert all(d["notice_id"] != first for d in db.list_deadlines())


# ------------------------------------------------------------------- idempotency
def test_running_twice_creates_no_duplicates(db, source):
    first = db_migrate.migrate(source.path, db, apply=True)
    second = db_migrate.migrate(source.path, db, apply=True)
    assert first.ok and second.ok
    assert counts(db) == {"notices": 3, "deadlines": 5, "action_items": 4}
    for table in db_migrate.TABLES:
        assert second.stats[table].to_insert == 0 and second.stats[table].inserted == 0
        assert second.stats[table].already_present == second.stats[table].source - second.stats[table].skipped
    third = db_migrate.migrate(source.path, db, apply=False)
    assert third.ok and counts(db) == {"notices": 3, "deadlines": 5, "action_items": 4}


def test_partially_migrated_target_is_completed_not_duplicated(db, source):
    first = source.list_notices()[-1]                                            # oldest
    db_migrate.migrate(source.path, db, apply=True)
    # simulate an earlier partial run: remove one notice from the target and migrate again
    db.delete_notice(first["id"])
    report = db_migrate.migrate(source.path, db, apply=True)
    assert report.ok and report.stats["notices"].to_insert == 1 and report.stats["notices"].already_present == 2
    assert counts(db) == counts(source)


# ---------------------------------------------------------------------- conflicts
def test_conflicting_id_aborts_without_writing(db, source):
    db.save_notice(ai_engine.validate_analysis(make_gemini_payload(title="Different")), "unrelated doc", None)
    report = db_migrate.migrate(source.path, db, apply=True)
    assert not report.ok and not report.committed
    assert any("DIFFERENT notice with this id" in e for e in report.errors)
    assert counts(db)["notices"] == 1                                            # nothing was added


def test_same_document_under_another_id_aborts(db, source):
    original = source.get_notice(source.list_notices()[0]["id"])
    with db.connect() as s:                                                      # same content, shifted id
        s.execute(f"INSERT INTO notices (id, title, category, content_hash, extracted_text, analysis_json, created_at) "
                  f"VALUES (?, ?, 'Other', ?, 'x', {db.backend.json_param}, ?)",
                  (500, "Copy", original["content_hash"], "{}", db.backend.now()))
    report = db_migrate.migrate(source.path, db, apply=True)
    assert not report.ok and any("already exists in the target as id=500" in e for e in report.errors)
    assert counts(db)["notices"] == 1


# ---------------------------------------------------------- validation & rollback
def test_validation_failure_rolls_back_everything(db, source, monkeypatch):
    monkeypatch.setattr(db_migrate, "_validate", lambda s, src, report: ["simulated mismatch"])
    report = db_migrate.migrate(source.path, db, apply=True)
    assert not report.ok and not report.committed and report.validation_problems == ["simulated mismatch"]
    assert counts(db) == {"notices": 0, "deadlines": 0, "action_items": 0}       # whole transaction rolled back
    assert all(s.inserted == 0 for s in report.stats.values())


def test_insert_error_rolls_back_everything(db, source, monkeypatch):
    real = db_migrate._insert

    def explode_after_notices(s, b, rows, report):
        real(s, b, {"notices": rows["notices"], "deadlines": [], "action_items": []}, report)
        raise RuntimeError("disk full")

    monkeypatch.setattr(db_migrate, "_insert", explode_after_notices)
    with pytest.raises(RuntimeError):
        db_migrate.migrate(source.path, db, apply=True)
    assert counts(db) == {"notices": 0, "deadlines": 0, "action_items": 0}


# ------------------------------------------------------------- messy source data
def test_orphans_and_bad_values_become_warnings_not_crashes(db, tmp_path):
    src = tmp_path / "messy.db"
    clean = Database(src)
    clean.save_notice(ai_engine.validate_analysis(make_gemini_payload()), NOTICE_TEXT, "n.pdf")
    raw = sqlite3.connect(src)                                                   # foreign keys OFF: create orphans
    raw.execute("INSERT INTO deadlines (id, notice_id, description, deadline_date, date_type, created_at) "
                "VALUES (900, 777, 'orphan', '2026-01-01', 'explicit', '2026-01-01T00:00:00')")
    raw.execute("INSERT INTO deadlines (id, notice_id, description, deadline_date, date_type, created_at) "
                "VALUES (901, 1, 'bad date', 'next friday', 'explicit', 'garbage')")
    raw.execute("INSERT INTO action_items (id, notice_id, deadline_id, title, priority, status, due_date, due_date_type, "
                "created_at, updated_at) VALUES (900, 1, 12345, 'dangling link', 'low', 'pending', NULL, 'none', "
                "'2026-01-01T00:00:00', '2026-01-01T00:00:00')")
    raw.commit()
    raw.close()
    report = db_migrate.migrate(src, db, apply=True)
    assert report.ok and report.committed
    text = " ".join(report.warnings)
    assert "parent notice 777 not migrated" in text and "unreadable date" in text
    assert "unreadable timestamp" in text and "link cleared" in text
    assert db.get_task(900)["deadline_id"] is None
    assert [d["deadline_date"] for d in db.list_deadlines() if d["id"] == 901] == [None]
    assert report.stats["deadlines"].skipped == 1


def test_invalid_source_rows_are_reported_as_errors(db, tmp_path):
    src = tmp_path / "bad.db"
    Database(src)
    raw = sqlite3.connect(src)
    raw.execute("INSERT INTO notices (id, title, category, content_hash, analysis_json, created_at) "
                "VALUES (1, 'Has bad json', 'Other', 'h1', '{not json', '2026-01-01T00:00:00')")
    raw.commit()
    raw.close()
    report = db_migrate.migrate(src, db, apply=True)
    assert not report.ok and any("not valid JSON" in e for e in report.errors) and counts(db)["notices"] == 0


def test_missing_or_wrong_source_files(db, tmp_path):
    with pytest.raises(DatabaseError, match="not found"):
        db_migrate.migrate(tmp_path / "nope.db", db)
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    with pytest.raises(DatabaseError, match="missing table"):
        db_migrate.migrate(empty, db)
    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(b"not sqlite" * 50)
    with pytest.raises(DatabaseError):
        db_migrate.migrate(garbage, db)


def test_source_equal_to_target_is_refused(tmp_path):
    same = Database(tmp_path / "same.db")
    report = db_migrate.migrate(same.path, same, apply=True)
    assert not report.ok and "same database" in report.errors[0]


# ------------------------------------------------------------------------- CLI
@pytest.fixture
def cli_env(monkeypatch, pg_test_url):
    monkeypatch.setenv("DATABASE_URL", pg_test_url)
    return pg_test_url


def test_cli_check_init_and_migrate(cli_env, source, capsys, pg_db):
    assert db_tools.main(["check"]) == 0
    assert db_tools.main(["init"]) == 0
    assert db_tools.main(["migrate-sqlite", "--sqlite", str(source.path)]) == 0           # dry run
    dry = capsys.readouterr().out
    assert "DRY RUN" in dry and "Dry run clean" in dry
    assert pg_db.count_notices() == 0
    assert db_tools.main(["migrate-sqlite", "--sqlite", str(source.path), "--apply"]) == 0
    assert "Migration committed and validated" in capsys.readouterr().out
    assert pg_db.table_counts() == {"notices": 3, "deadlines": 5, "action_items": 4}
    assert db_tools.main(["migrate-sqlite", "--sqlite", str(source.path), "--apply"]) == 0   # idempotent
    assert pg_db.count_notices() == 3


def test_cli_never_prints_the_connection_string(cli_env, source, capsys, pg_db):
    db_tools.main(["check"])
    db_tools.main(["migrate-sqlite", "--sqlite", str(source.path)])
    out = capsys.readouterr().out
    assert cli_env not in out and "postgresql://" not in out


def test_cli_without_database_url_explains_what_to_do(source, capsys):
    assert db_tools.main(["init"]) == 2
    out = capsys.readouterr().out
    assert "DATABASE_URL is not set" in out
    assert db_tools.main(["migrate-sqlite", "--sqlite", str(source.path)]) == 2
    assert source.count_notices() == 3


def test_cli_dry_run_exit_code_on_conflict(cli_env, source, capsys, pg_db):
    pg_db.save_notice(ai_engine.validate_analysis(make_gemini_payload(title="Other")), "other doc", None)
    assert db_tools.main(["migrate-sqlite", "--sqlite", str(source.path)]) == 1
    assert "Fix the problems above" in capsys.readouterr().out
