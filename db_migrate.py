"""Safe, repeatable SQLite -> PostgreSQL data migration.

Guarantees
* The SQLite file is opened READ-ONLY and is never modified or deleted.
* IDs and relationships are preserved.
* Re-running is safe: rows already present (same id) are skipped, never duplicated.
* Dry-run by default: ``migrate(..., apply=False)`` plans and validates without writing.
* Writes happen in ONE transaction and are validated before commit; any problem rolls
  everything back.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from database import Database, _strip_nul
from db_backends import DatabaseError

logger = logging.getLogger(__name__)

TABLES = ("notices", "deadlines", "action_items")


class MigrationAborted(Exception):
    """Raised inside the transaction to force a rollback when validation fails."""

    def __init__(self, problems: List[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass
class TableStats:
    source: int = 0
    to_insert: int = 0
    already_present: int = 0
    skipped: int = 0
    inserted: int = 0


@dataclass
class MigrationReport:
    source_path: str
    apply: bool
    stats: Dict[str, TableStats] = field(default_factory=lambda: {t: TableStats() for t in TABLES})
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    validation_problems: List[str] = field(default_factory=list)
    committed: bool = False

    @property
    def ok(self) -> bool:
        return not self.errors and not self.validation_problems


# ----------------------------------------------------------------------------
# Reading the source (read-only)
# ----------------------------------------------------------------------------
def read_source(sqlite_path: Path) -> Dict[str, List[Dict[str, Any]]]:
    path = Path(sqlite_path)
    if not path.is_file():
        raise DatabaseError(f"SQLite file not found: {path}", "config")
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)  # read-only: cannot modify the file
    conn.row_factory = sqlite3.Row
    try:
        present = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = [t for t in TABLES if t not in present]
        if missing:
            raise DatabaseError(f"This SQLite file is missing table(s): {', '.join(missing)}.", "config")
        return {t: [dict(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY id")] for t in TABLES}  # fixed names
    except sqlite3.DatabaseError as exc:
        raise DatabaseError(f"The SQLite file could not be read ({type(exc).__name__}).", "config") from exc
    finally:
        conn.close()


# ----------------------------------------------------------------------------
# Planning
# ----------------------------------------------------------------------------
def _target_state(db: Database) -> Dict[str, Dict[int, Dict[str, Any]]]:
    with db.connect() as s:
        return {
            "notices": {r["id"]: r for r in s.execute("SELECT id, content_hash FROM notices").rows},
            "deadlines": {r["id"]: r for r in s.execute("SELECT id, notice_id FROM deadlines").rows},
            "action_items": {r["id"]: r for r in s.execute("SELECT id, notice_id, deadline_id FROM action_items").rows},
        }


def plan(source: Dict[str, List[Dict[str, Any]]], db: Database, report: MigrationReport) -> Dict[str, List[Dict]]:
    """Decide which source rows to insert. Fills the report; returns the rows to insert."""
    target = _target_state(db)
    hash_to_id = {r["content_hash"]: i for i, r in target["notices"].items()}
    to_insert: Dict[str, List[Dict]] = {t: [] for t in TABLES}
    kept_notice_ids: Set[int] = set()
    kept_deadline_ids: Set[int] = set()

    for row in source["notices"]:
        st = report.stats["notices"]
        st.source += 1
        nid = row["id"]
        if not (row.get("title") or "").strip() or not row.get("content_hash"):
            report.errors.append(f"notices id={nid}: missing title or content_hash.")
            continue
        try:
            json.loads(row["analysis_json"])
        except (TypeError, ValueError):
            report.errors.append(f"notices id={nid}: analysis_json is not valid JSON.")
            continue
        if nid in target["notices"]:
            if target["notices"][nid]["content_hash"] == row["content_hash"]:
                st.already_present += 1
                kept_notice_ids.add(nid)
            else:
                report.errors.append(f"notices id={nid}: the target already has a DIFFERENT notice with this id.")
            continue
        if row["content_hash"] in hash_to_id:
            report.errors.append(f"notices id={nid}: the same document already exists in the target as id="
                                 f"{hash_to_id[row['content_hash']]}.")
            continue
        st.to_insert += 1
        kept_notice_ids.add(nid)
        to_insert["notices"].append(row)

    for row in source["deadlines"]:
        st = report.stats["deadlines"]
        st.source += 1
        did = row["id"]
        if row["notice_id"] not in kept_notice_ids:
            st.skipped += 1
            report.warnings.append(f"deadlines id={did}: parent notice {row['notice_id']} not migrated; skipped.")
            continue
        if did in target["deadlines"]:
            if target["deadlines"][did]["notice_id"] == row["notice_id"]:
                st.already_present += 1
                kept_deadline_ids.add(did)
            else:
                report.errors.append(f"deadlines id={did}: the target has this id under a different notice.")
            continue
        st.to_insert += 1
        kept_deadline_ids.add(did)
        to_insert["deadlines"].append(row)

    for row in source["action_items"]:
        st = report.stats["action_items"]
        st.source += 1
        aid = row["id"]
        if row["notice_id"] not in kept_notice_ids:
            st.skipped += 1
            report.warnings.append(f"action_items id={aid}: parent notice {row['notice_id']} not migrated; skipped.")
            continue
        if row.get("deadline_id") is not None and row["deadline_id"] not in kept_deadline_ids:
            report.warnings.append(f"action_items id={aid}: deadline {row['deadline_id']} missing; link cleared.")
            row = {**row, "deadline_id": None}
        if aid in target["action_items"]:
            if target["action_items"][aid]["notice_id"] == row["notice_id"]:
                st.already_present += 1
            else:
                report.errors.append(f"action_items id={aid}: the target has this id under a different notice.")
            continue
        st.to_insert += 1
        to_insert["action_items"].append(row)
    return to_insert


# ----------------------------------------------------------------------------
# Writing + validation
# ----------------------------------------------------------------------------
def _ts(b, value, report: MigrationReport, what: str):
    parsed = b.to_timestamp(value)
    if value and parsed is None:
        report.warnings.append(f"{what}: unreadable timestamp {str(value)[:30]!r}; using the current time.")
        return b.now()
    return parsed or b.now()


def _d(b, value, report: MigrationReport, what: str):
    parsed = b.to_date(value)
    if value and parsed is None:
        report.warnings.append(f"{what}: unreadable date {str(value)[:30]!r}; stored as empty.")
    return parsed


def _insert(s, b, rows: Dict[str, List[Dict]], report: MigrationReport) -> None:
    for r in rows["notices"]:
        res = s.execute(
            f"""INSERT INTO notices (id, title, category, summary, organization, department, announcement_date,
                    venue, source_language, file_name, content_hash, extracted_text, analysis_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, {b.json_param}, ?)
                ON CONFLICT (id) DO NOTHING""",
            (r["id"], _strip_nul(r["title"]), _strip_nul(r.get("category")) or "Other", _strip_nul(r.get("summary")),
             _strip_nul(r.get("organization")), _strip_nul(r.get("department")),
             _d(b, r.get("announcement_date"), report, f"notices id={r['id']} announcement_date"),
             _strip_nul(r.get("venue")), _strip_nul(r.get("source_language")), _strip_nul(r.get("file_name")),
             r["content_hash"], _strip_nul(r.get("extracted_text")),
             json.dumps(_strip_nul(json.loads(r["analysis_json"])), ensure_ascii=False),
             _ts(b, r.get("created_at"), report, f"notices id={r['id']} created_at")))
        report.stats["notices"].inserted += max(res.rowcount, 0)
    for r in rows["deadlines"]:
        res = s.execute(
            """INSERT INTO deadlines (id, notice_id, description, deadline_date, deadline_time, date_type,
                    source_text, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (id) DO NOTHING""",
            (r["id"], r["notice_id"], _strip_nul(r["description"]),
             _d(b, r.get("deadline_date"), report, f"deadlines id={r['id']} deadline_date"),
             _strip_nul(r.get("deadline_time")), r.get("date_type") or "unspecified", _strip_nul(r.get("source_text")),
             _ts(b, r.get("created_at"), report, f"deadlines id={r['id']} created_at")))
        report.stats["deadlines"].inserted += max(res.rowcount, 0)
    for r in rows["action_items"]:
        res = s.execute(
            """INSERT INTO action_items (id, notice_id, deadline_id, title, description, priority, status, due_date,
                    due_date_type, created_at, updated_at, completed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (id) DO NOTHING""",
            (r["id"], r["notice_id"], r.get("deadline_id"), _strip_nul(r["title"]), _strip_nul(r.get("description")),
             r.get("priority") or "medium", r.get("status") or "pending",
             _d(b, r.get("due_date"), report, f"action_items id={r['id']} due_date"),
             r.get("due_date_type") or "none",
             _ts(b, r.get("created_at"), report, f"action_items id={r['id']} created_at"),
             _ts(b, r.get("updated_at") or r.get("created_at"), report, f"action_items id={r['id']} updated_at"),
             b.to_timestamp(r.get("completed_at"))))
        report.stats["action_items"].inserted += max(res.rowcount, 0)


def _reset_sequences(s, b) -> None:
    """After inserting explicit ids, move PostgreSQL identity sequences past the highest id."""
    if b.name != "postgres":
        return
    for table in TABLES:
        s.execute(f"SELECT setval(pg_get_serial_sequence('public.{table}', 'id'), "
                  f"COALESCE((SELECT MAX(id) FROM public.{table}), 1), "
                  f"(SELECT COUNT(*) > 0 FROM public.{table}))")


def _validate(s, source: Dict[str, List[Dict]], report: MigrationReport) -> List[str]:
    """Verify, inside the transaction, that everything expected is present and consistent."""
    problems: List[str] = []
    n = {r["id"]: r for r in s.execute("SELECT id, content_hash FROM notices").rows}
    d = {r["id"]: r for r in s.execute("SELECT id, notice_id FROM deadlines").rows}
    a = {r["id"]: r for r in s.execute("SELECT id, notice_id, deadline_id FROM action_items").rows}

    kept_notices = {r["id"] for r in source["notices"]
                    if r["id"] in n and n[r["id"]]["content_hash"] == r["content_hash"]}
    for r in source["notices"]:
        if r["id"] not in kept_notices:
            problems.append(f"notices id={r['id']} is not present in the target after migration.")
    for r in source["deadlines"]:
        if r["notice_id"] in kept_notices:
            if r["id"] not in d:
                problems.append(f"deadlines id={r['id']} is missing in the target.")
            elif d[r["id"]]["notice_id"] != r["notice_id"]:
                problems.append(f"deadlines id={r['id']} points to a different notice in the target.")
    for r in source["action_items"]:
        if r["notice_id"] in kept_notices:
            if r["id"] not in a:
                problems.append(f"action_items id={r['id']} is missing in the target.")
            elif a[r["id"]]["notice_id"] != r["notice_id"]:
                problems.append(f"action_items id={r['id']} points to a different notice in the target.")
    # referential integrity of the whole target
    for row in d.values():
        if row["notice_id"] not in n:
            problems.append(f"deadlines id={row['id']} has no parent notice.")
    for row in a.values():
        if row["notice_id"] not in n:
            problems.append(f"action_items id={row['id']} has no parent notice.")
        if row["deadline_id"] is not None and row["deadline_id"] not in d:
            problems.append(f"action_items id={row['id']} points to a missing deadline.")
    return problems


def migrate(sqlite_path: Path, db: Database, apply: bool = False) -> MigrationReport:
    """Plan (and, with ``apply=True``, perform) the migration. Never touches the SQLite file."""
    source_path = Path(sqlite_path)
    report = MigrationReport(str(source_path), apply)
    if db.path is not None and source_path.resolve() == Path(db.path).resolve():
        report.errors.append("The source and target are the same database.")
        return report

    source = read_source(source_path)
    to_insert = plan(source, db, report)
    if report.errors:
        return report

    if not apply:  # dry run: also validate what the target would look like
        return report

    try:
        with db.connect() as s:  # one transaction: commit only if validation passes
            _insert(s, db.backend, to_insert, report)
            _reset_sequences(s, db.backend)
            problems = _validate(s, source, report)
            if problems:
                raise MigrationAborted(problems)
        report.committed = True
    except MigrationAborted as exc:
        report.validation_problems = exc.problems
        for t in TABLES:
            report.stats[t].inserted = 0  # rolled back
    return report
