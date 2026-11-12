"""Persistence layer for notices, deadlines and tasks.

``Database`` is the only thing the app talks to. It runs the same parameterised SQL
(``?`` placeholders) on either backend:

* PostgreSQL / Supabase when ``DATABASE_URL`` is configured (primary, permanent storage)
* SQLite file otherwise (local development fallback)

Connection handling, transactions and driver quirks live in ``db_backends``.
"""
from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import config
from db_backends import (  # noqa: F401  (re-exported for callers)
    Backend, DatabaseConfigError, DatabaseConnectionError, DatabaseError, DatabaseIntegrityError,
    PostgresBackend, SQLiteBackend, redact,
)

TASK_EDITABLE_FIELDS = {"title", "description", "priority", "status", "due_date", "due_date_type"}
PRIORITIES = ("high", "medium", "low")
TASK_STATUSES = ("pending", "completed")
DUE_DATE_TYPES = ("explicit", "inferred", "manual", "none")
DEADLINE_DATE_TYPES = ("explicit", "inferred", "unspecified")

# Input limits (defence in depth; the AI layer already normalises its output)
MAX_TITLE = 500
MAX_SHORT = 300
MAX_TEXT = 8000
MAX_SEARCH = 200
MAX_DOCUMENT_CHARS = 2_000_000
MAX_ANALYSIS_BYTES = 2_000_000


def _strip_nul(value: Any) -> Any:
    """PostgreSQL rejects NUL (0x00) in text and JSONB; OCR/PDF text can contain it."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, list):
        return [_strip_nul(v) for v in value]
    if isinstance(value, dict):
        return {_strip_nul(k): _strip_nul(v) for k, v in value.items()}
    return value


def _clip(value: Any, limit: int) -> Optional[str]:
    if value is None:
        return None
    text = str(_strip_nul(value)).strip()
    return text[:limit] if text else None


def content_fingerprint(text: str) -> str:
    """Hash of whitespace/case-normalised text, used to detect duplicate notices."""
    normalised = re.sub(r"\s+", " ", _strip_nul(text) or "").strip().lower()
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def _like(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def make_backend(path: Optional[Path] = None, url: Optional[str] = None) -> Backend:
    """Pick the backend: explicit url > explicit path > configured DATABASE_URL > default SQLite file."""
    if url:
        return PostgresBackend(url)
    if path:
        return SQLiteBackend(Path(path))
    configured = config.get_database_url()
    if configured:
        return PostgresBackend(configured)
    return SQLiteBackend(config.get_db_path())


class Database:
    def __init__(self, path: Optional[Path] = None, url: Optional[str] = None,
                 backend: Optional[Backend] = None, init: bool = True):
        self.backend: Backend = backend or make_backend(path, url)
        self.path: Optional[Path] = getattr(self.backend, "path", None)  # SQLite file, else None
        if init and (self.backend.name == "sqlite" or config.db_auto_migrate()):
            self.init_schema()

    # ------------------------------------------------------------- plumbing
    @property
    def backend_name(self) -> str:
        return self.backend.name

    @contextmanager
    def connect(self) -> Iterator[Any]:
        with self.backend.connect() as session:
            yield session

    def init_schema(self) -> List[str]:
        """Create / upgrade the schema. Returns migration versions applied (PostgreSQL only)."""
        return self.backend.init_schema()

    def describe(self) -> Dict[str, Any]:
        return self.backend.describe()

    def check(self) -> Dict[str, Any]:
        """Round-trip to the database; raises DatabaseError with a friendly message on failure."""
        return self.backend.ping()

    def close(self) -> None:
        self.backend.close()

    # ------------------------------------------------------------- notices
    def find_duplicate(self, text: str) -> Optional[int]:
        with self.connect() as s:
            row = s.execute("SELECT id FROM notices WHERE content_hash = ?", (content_fingerprint(text),)).one()
        return row["id"] if row else None

    def save_notice(self, analysis: Dict[str, Any], extracted_text: str,
                    file_name: Optional[str] = None) -> Tuple[int, bool]:
        """Persist a notice with its deadlines and action items in ONE transaction.

        Returns (notice_id, created). If an identical notice already exists, the existing
        id is returned with created=False and nothing is written. Duplicate detection is
        enforced by a UNIQUE constraint, so concurrent saves / Streamlit reruns cannot
        create duplicates.
        """
        title = _clip(analysis.get("title"), MAX_TITLE)
        if not title:
            raise ValueError("A notice needs a title.")
        text = _strip_nul(extracted_text or "")
        if len(text) > MAX_DOCUMENT_CHARS:
            raise ValueError("The document text is too large to store.")
        clean_analysis = _strip_nul(analysis)
        payload = json.dumps(clean_analysis, ensure_ascii=False)
        if len(payload.encode("utf-8")) > MAX_ANALYSIS_BYTES:
            raise ValueError("The analysis is too large to store.")

        b = self.backend
        now = b.now()
        with self.connect() as s:
            inserted = s.execute(
                f"""INSERT INTO notices (title, category, summary, organization, department,
                        announcement_date, venue, source_language, file_name, content_hash,
                        extracted_text, analysis_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, {b.json_param}, ?)
                    ON CONFLICT (content_hash) DO NOTHING
                    RETURNING id""",
                (
                    title, _clip(analysis.get("category"), 100) or "Other", _clip(analysis.get("summary"), MAX_TEXT),
                    _clip(analysis.get("organization"), MAX_SHORT), _clip(analysis.get("department"), MAX_SHORT),
                    b.to_date(analysis.get("announcement_date")), _clip(analysis.get("venue"), MAX_SHORT),
                    _clip(analysis.get("source_language"), 100), _clip(file_name, MAX_SHORT),
                    content_fingerprint(text), text, payload, now,
                ),
            ).one()
            if inserted is None:  # identical notice already stored
                existing = s.execute("SELECT id FROM notices WHERE content_hash = ?",
                                     (content_fingerprint(text),)).one()
                return existing["id"], False
            notice_id = inserted["id"]

            deadline_ids: Dict[str, int] = {}
            deadline_info: Dict[str, Tuple[Any, str]] = {}
            for d in analysis.get("deadlines", []):
                description = _clip(d.get("description"), MAX_SHORT) or "Deadline"
                date_type = d.get("date_type") if d.get("date_type") in DEADLINE_DATE_TYPES else "unspecified"
                d_date = b.to_date(d.get("date"))
                row = s.execute(
                    """INSERT INTO deadlines (notice_id, description, deadline_date, deadline_time,
                           date_type, source_text, created_at) VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id""",
                    (notice_id, description, d_date, _clip(d.get("time"), 100), date_type,
                     _clip(d.get("source_text"), MAX_TEXT), now),
                ).one()
                key = description.lower()
                deadline_ids.setdefault(key, row["id"])
                deadline_info.setdefault(key, (d_date, date_type))

            for a in analysis.get("action_items", []):
                a_title = _clip(a.get("title"), MAX_TITLE)
                if not a_title:
                    continue
                key = (_clip(a.get("related_deadline"), MAX_SHORT) or "").lower()
                deadline_id = deadline_ids.get(key)
                due_date, due_type = None, "none"
                if deadline_id is not None:
                    d_date, d_type = deadline_info[key]
                    if d_date:
                        due_date, due_type = d_date, d_type
                priority = a.get("priority") if a.get("priority") in PRIORITIES else "medium"
                s.execute(
                    """INSERT INTO action_items (notice_id, deadline_id, title, description,
                           priority, status, due_date, due_date_type, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)""",
                    (notice_id, deadline_id, a_title, _clip(a.get("description"), MAX_TEXT),
                     priority, due_date, due_type, now, now),
                )
        return notice_id, True

    def get_notice(self, notice_id: int) -> Optional[Dict[str, Any]]:
        with self.connect() as s:
            row = s.execute("SELECT * FROM notices WHERE id = ?", (notice_id,)).one()
        if row is None:
            return None
        analysis = row.pop("analysis_json")
        row["analysis"] = json.loads(analysis) if isinstance(analysis, (str, bytes)) else analysis
        return row

    def list_notices(self, search: str = "", category: Optional[str] = None,
                     date_from: Optional[date] = None, date_to: Optional[date] = None) -> List[Dict[str, Any]]:
        """List notices, filtered by title/summary text, category and analysis date."""
        like = self.backend.like_op
        sql = ["SELECT id, title, category, summary, organization, file_name, created_at FROM notices WHERE 1=1"]
        params: List[Any] = []
        term = (search or "").strip()[:MAX_SEARCH]
        if term:
            sql.append(f"AND (title {like} ? ESCAPE '\\' OR summary {like} ? ESCAPE '\\' "
                       f"OR organization {like} ? ESCAPE '\\')")
            params += [_like(term)] * 3
        if category and category != "All":
            sql.append("AND category = ?")
            params.append(category)
        if date_from:
            sql.append("AND created_at >= ?")
            params.append(self.backend.day_start(date_from))
        if date_to:
            sql.append("AND created_at < ?")
            params.append(self.backend.day_start(date_to + timedelta(days=1)))
        sql.append("ORDER BY created_at DESC, id DESC")
        with self.connect() as s:
            return s.execute(" ".join(sql), params).rows

    def delete_notice(self, notice_id: int) -> bool:
        """Delete a notice; its deadlines and action items are removed by cascade."""
        with self.connect() as s:
            result = s.execute("DELETE FROM notices WHERE id = ?", (notice_id,))
        return result.rowcount > 0

    def list_categories(self) -> List[str]:
        with self.connect() as s:
            rows = s.execute("SELECT DISTINCT category FROM notices ORDER BY category").rows
        return [r["category"] for r in rows]

    # ---------------------------------------------------------------- deadlines
    def list_deadlines(self, notice_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = ("SELECT d.*, n.title AS notice_title FROM deadlines d "
               "JOIN notices n ON n.id = d.notice_id")
        params: List[Any] = []
        if notice_id is not None:
            sql += " WHERE d.notice_id = ?"
            params.append(notice_id)
        sql += " ORDER BY d.deadline_date IS NULL, d.deadline_date, d.id"
        with self.connect() as s:
            return s.execute(sql, params).rows

    def count_upcoming_deadlines(self, today: date) -> int:
        with self.connect() as s:
            row = s.execute("SELECT COUNT(*) AS c FROM deadlines WHERE deadline_date >= ?",
                            (self.backend.to_date(today),)).one()
        return int(row["c"])

    # -------------------------------------------------------------------- tasks
    def list_tasks(self, notice_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = ("SELECT a.*, n.title AS notice_title FROM action_items a "
               "JOIN notices n ON n.id = a.notice_id")
        params: List[Any] = []
        if notice_id is not None:
            sql += " WHERE a.notice_id = ?"
            params.append(notice_id)
        sql += " ORDER BY a.due_date IS NULL, a.due_date, a.id"
        with self.connect() as s:
            return s.execute(sql, params).rows

    def get_task(self, task_id: int) -> Optional[Dict[str, Any]]:
        with self.connect() as s:
            return s.execute("SELECT * FROM action_items WHERE id = ?", (task_id,)).one()

    def update_task(self, task_id: int, **fields: Any) -> bool:
        """Update editable task fields. Invalid names or values raise ValueError."""
        unknown = set(fields) - TASK_EDITABLE_FIELDS
        if unknown:
            raise ValueError(f"Cannot update fields: {', '.join(sorted(unknown))}")
        if not fields:
            return False
        if "title" in fields:
            title = _clip(fields["title"], MAX_TITLE)
            if not title:
                raise ValueError("Task title cannot be empty.")
            fields["title"] = title
        if "description" in fields:
            fields["description"] = _clip(fields["description"], MAX_TEXT)
        if "priority" in fields and fields["priority"] not in PRIORITIES:
            raise ValueError(f"Priority must be one of: {', '.join(PRIORITIES)}.")
        if "status" in fields and fields["status"] not in TASK_STATUSES:
            raise ValueError(f"Status must be one of: {', '.join(TASK_STATUSES)}.")
        if "due_date_type" in fields and fields["due_date_type"] not in DUE_DATE_TYPES:
            raise ValueError(f"Deadline type must be one of: {', '.join(DUE_DATE_TYPES)}.")
        if "due_date" in fields:
            if fields["due_date"] is None:
                fields["due_date_type"] = "none"
            else:
                fields["due_date"] = date.fromisoformat(str(fields["due_date"])[:10])  # ValueError if invalid
                fields.setdefault("due_date_type", "manual")

        b = self.backend
        now = b.now()
        if "status" in fields:
            fields["completed_at"] = now if fields["status"] == "completed" else None
        fields["updated_at"] = now
        if "due_date" in fields:
            fields["due_date"] = b.to_date(fields["due_date"])
        assignments = ", ".join(f"{name} = ?" for name in fields)  # names come from a fixed allow-list
        with self.connect() as s:
            result = s.execute(f"UPDATE action_items SET {assignments} WHERE id = ?", [*fields.values(), task_id])
        return result.rowcount > 0

    def set_task_status(self, task_id: int, status: str) -> bool:
        return self.update_task(task_id, status=status)

    # -------------------------------------------------------------------- stats
    def count_notices(self) -> int:
        with self.connect() as s:
            return int(s.execute("SELECT COUNT(*) AS c FROM notices").one()["c"])

    def category_counts(self) -> Dict[str, int]:
        with self.connect() as s:
            rows = s.execute("SELECT category, COUNT(*) AS c FROM notices "
                             "GROUP BY category ORDER BY c DESC, category").rows
        return {r["category"]: int(r["c"]) for r in rows}

    def table_counts(self) -> Dict[str, int]:
        with self.connect() as s:
            return {
                t: int(s.execute(f"SELECT COUNT(*) AS c FROM {t}").one()["c"])  # fixed table names
                for t in ("notices", "deadlines", "action_items")
            }
