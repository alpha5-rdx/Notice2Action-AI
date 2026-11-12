"""Database backends: Supabase PostgreSQL (primary) and SQLite (local fallback).

All connection handling lives here so the rest of the app never touches a driver:

* ``connect()`` yields a ``Session`` inside a transaction (commit on success,
  rollback on any exception, connection always released).
* SQL is written once with ``?`` placeholders; each backend adapts it.
* Driver exceptions are translated into ``DatabaseError`` subclasses whose
  messages are safe to show to users (no credentials, no connection strings).
"""
from __future__ import annotations

import logging
import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence
from urllib.parse import quote, unquote, urlsplit

import config

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------------
# Errors (messages are user-facing and must never contain secrets)
# ----------------------------------------------------------------------------
class DatabaseError(Exception):
    def __init__(self, message: str, kind: str = "error", details: "Optional[ErrorDetails]" = None):
        super().__init__(message)
        self.kind = kind
        self.details = details  # exception class / SQLSTATE / sanitized text; never secrets


class DatabaseConfigError(DatabaseError):
    """DATABASE_URL is missing parts, malformed, or still a placeholder."""


class DatabaseConnectionError(DatabaseError):
    """The server could not be reached or rejected the login."""


class DatabaseIntegrityError(DatabaseError):
    """A constraint (foreign key, unique, check) rejected the write."""


# Greedy up to the LAST '@' of the token, so a password containing an unencoded '@' is fully hidden.
_URL_CREDENTIALS = re.compile(r"(postgres(?:ql)?://)\S*@", re.IGNORECASE)


def redact(text: Any) -> str:
    """Hide the credentials part of any connection string inside ``text``."""
    return _URL_CREDENTIALS.sub(r"\1***@", str(text))


def is_local_host(host: Optional[str]) -> bool:
    return not host or host in {"localhost", "127.0.0.1", "::1"} or host.startswith("/")


def describe_url(url: str) -> Dict[str, Any]:
    """Non-secret facts about a connection URL (never the user or password)."""
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        host, port = None, None
        parts = urlsplit("")
    return {"host": host or "unknown", "port": port or 5432, "database": (parts.path or "/").lstrip("/") or "postgres"}


@dataclass(frozen=True)
class ErrorDetails:
    """Facts about a driver exception. Never contains the password or the connection string."""

    exc_class: str                  # e.g. OperationalError
    sqlstate: Optional[str]         # five-character code when the server/driver supplied one
    sanitized: str                  # full message, secrets masked (may contain the host); for logs and the CLI
    server_message: str             # short server text with hosts and quoted names masked; safe for the UI


def sanitize_error_text(text: Any, secrets: Sequence[str] = (), limit: Optional[int] = 600) -> str:
    """Mask credentials in free text (connection strings, known secrets, user/role names)."""
    out = redact(text)
    for secret in secrets:
        if secret and len(secret) >= 3:
            out = out.replace(secret, "***").replace(quote(secret, safe=""), "***")
    out = re.sub(r'(?i)(for (?:user|role) )"[^"]*"', r'\1"***"', out)
    out = re.sub(r'(?i)\b(user|password|passwd|username)\s*=\s*\S+', r'\1=***', out)
    out = " ".join(out.split())
    return out if limit is None or len(out) <= limit else out[:limit] + "..."


def _public_server_message(sanitized: str) -> str:
    """The server-side part of a libpq error, safe to show in the UI (no hosts, no quoted names).

    Takes the FIRST server message (libpq repeats the same failure once per resolved address).
    """
    match = re.search(r"\b(FATAL|ERROR|PANIC):\s*(.+?)(?=\s+(?:FATAL|ERROR|PANIC):|\s+Multiple connection attempts|"
                      r"\s+-\s+host:|$)", sanitized)
    if match:
        text = f"{match.group(1)}: {match.group(2)}"
    else:
        tail = re.search(r"failed:\s*(.+?)(?=\s+Multiple connection attempts|\s+-\s+host:|$)", sanitized)
        text = tail.group(1) if tail else ""
    text = re.sub(r'"[^"]*"', '"***"', text)
    text = re.sub(r"\(\d{1,3}(?:\.\d{1,3}){3}\)|\([0-9a-fA-F:]{3,}\)", "(ip)", text)
    return text.strip()[:200]


def extract_details(exc: BaseException, secrets: Sequence[str] = ()) -> ErrorDetails:
    sqlstate = getattr(exc, "sqlstate", None)
    if not sqlstate:
        sqlstate = getattr(getattr(exc, "diag", None), "sqlstate", None)
    full = sanitize_error_text(exc, secrets, limit=None)   # classify and extract from the untruncated text
    shown = full if len(full) <= 700 else full[:700] + "..."
    return ErrorDetails(type(exc).__name__, sqlstate, shown, _public_server_message(full))


def translate_pg_error(exc: BaseException, secrets: Sequence[str] = ()) -> DatabaseError:
    """Map a psycopg / pool exception to an actionable, secret-free DatabaseError.

    NOTE: psycopg prefixes almost every connection failure with "connection failed:", so that phrase
    says nothing about the cause. Only the specific server/OS text is used to classify.
    """
    details = extract_details(exc, secrets)
    low = sanitize_error_text(exc, secrets, limit=None).lower()   # classify on the full text, not the shortened one
    name = details.exc_class
    code = f", SQLSTATE {details.sqlstate}" if details.sqlstate else ""
    tail = f" (technical: {name}{code})"
    said = f' The server said: "{details.server_message}".' if details.server_message else ""

    def err(message: str, kind: str, include_said: bool = True) -> DatabaseConnectionError:
        return DatabaseConnectionError(message + (said if include_said else "") + tail, kind, details)

    if "ecircuitbreaker" in low or "circuit breaker" in low:
        return err("The Supabase pooler temporarily blocked connections after repeated failed logins. Wait about "
                   "10 minutes, correct the credentials, then retry.", "blocked")
    if "edbhandlerexited" in low or "db_handler" in low or ("econnrefused" in low and "fatal" in low):
        return err("The Supabase pooler is reachable but could not connect to the database server behind it. The "
                   "project is probably paused, restarting or still starting. Open the Supabase dashboard, restore or "
                   "wait for the project, then retry.", "upstream")
    if "eauthquery" in low and "user not found" in low:
        return err("The Supabase pooler is reachable and answered, but its authentication lookup found no login role "
                   "for this database user (EAUTHQUERY). Most often: the part of the user name before the dot is not an "
                   "existing role that can log in (for Supabase it is 'postgres'), the user name contains stray or "
                   "hidden characters from copy/paste, or the project ref after the dot is not the project this "
                   "pooler serves. Re-copy the whole string from Supabase > Connect > Session pooler.", "auth")
    if "eauthquery" in low:
        return err("The Supabase pooler could not complete its authentication lookup for this user (EAUTHQUERY). "
                   "Re-copy the connection string from Supabase > Connect and check the role's login status.", "auth")
    if "tenant or user not found" in low or "tenant/user" in low or "(enotfound)" in low:
        return err("The Supabase pooler does not recognise this project/user. Most likely: (1) the user is not "
                   "'postgres.<project-ref>', (2) the pooler host region does not match your project's region, or "
                   "(3) the project is paused or deleted. Re-copy the Session pooler string from Supabase > Connect.",
                   "auth")
    if any(s in low for s in ("password authentication failed", "authentication failed", "no pg_hba.conf entry",
                              "invalid password", "sasl authentication", "scram")) \
            or re.search(r'role "[^"]*" does not exist', low):
        return err("Database login failed. Check the user and password in DATABASE_URL. For the Supabase pooler the "
                   "user is 'postgres.<project-ref>'. If the password contains special characters (@ : / # ?), "
                   "URL-encode them.", "auth")
    if re.search(r'database "[^"]*" does not exist', low):
        return err("The database name in DATABASE_URL does not exist. For Supabase it is normally 'postgres'.",
                   "database")
    if any(s in low for s in ("could not translate host", "name or service not known", "getaddrinfo",
                              "nodename nor servname", "no address associated", "no such host is known")):
        return err("The database host could not be found. Check the host in DATABASE_URL. Direct Supabase hosts "
                   "(db.<ref>.supabase.co) are IPv6-only; use the Connection Pooler string from the Supabase "
                   "dashboard.", "dns", include_said=False)
    if re.search(r"\bssl\b|\btls\b|sslmode|certificate", low):
        return err("A secure (SSL) connection to the database could not be established. Remote databases require SSL.",
                   "ssl")
    if any(s in low for s in ("timeout expired", "timed out", "pool timeout", "couldn't get a connection")) \
            or name in {"PoolTimeout", "ConnectionTimeout"}:
        return err("The database did not respond in time. Check your network, the host/port in DATABASE_URL, and "
                   "that the Supabase project is not paused.", "timeout", include_said=False)
    if any(s in low for s in ("too many connections", "remaining connection slots", "max client connections",
                              "emaxclient")):
        return err("The database has no free connections right now. Wait a moment and retry, or use the pooled "
                   "connection string.", "capacity")
    if ("connection refused" in low or "actively refused" in low or "10061" in low
            or "network is unreachable" in low) and "fatal" not in low:
        return err("The connection was refused at the network level before any login happened. Check the host and "
                   "port in DATABASE_URL, your firewall/VPN, and that the Supabase project is running.", "refused",
                   include_said=False)
    if any(s in low for s in ("server closed the connection", "connection is closed", "terminating connection",
                              "connection lost", "consuming input failed", "pool is closed")):
        return err("The database connection was interrupted. Please retry.", "dropped", include_said=False)
    if "connection failed" in low or "could not connect" in low:
        return err("The database connection could not be established.", "connect")
    return DatabaseError("The database could not complete the request. Check the terminal log for details." + tail,
                         "query", details)


# ----------------------------------------------------------------------------
# Shared pieces
# ----------------------------------------------------------------------------
@dataclass
class Result:
    rows: List[Dict[str, Any]] = field(default_factory=list)
    rowcount: int = 0

    def one(self) -> Optional[Dict[str, Any]]:
        return self.rows[0] if self.rows else None


SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS notices (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    title           TEXT NOT NULL,
    category        TEXT NOT NULL DEFAULT 'Other',
    summary         TEXT,
    organization    TEXT,
    department      TEXT,
    announcement_date TEXT,
    venue           TEXT,
    source_language TEXT,
    file_name       TEXT,
    content_hash    TEXT NOT NULL UNIQUE,
    extracted_text  TEXT,
    analysis_json   TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deadlines (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    notice_id     INTEGER NOT NULL REFERENCES notices(id) ON DELETE CASCADE,
    description   TEXT NOT NULL,
    deadline_date TEXT,
    deadline_time TEXT,
    date_type     TEXT NOT NULL DEFAULT 'unspecified'
                  CHECK (date_type IN ('explicit', 'inferred', 'unspecified')),
    source_text   TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS action_items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    notice_id     INTEGER NOT NULL REFERENCES notices(id) ON DELETE CASCADE,
    deadline_id   INTEGER REFERENCES deadlines(id) ON DELETE SET NULL,
    title         TEXT NOT NULL,
    description   TEXT,
    priority      TEXT NOT NULL DEFAULT 'medium'
                  CHECK (priority IN ('high', 'medium', 'low')),
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'completed')),
    due_date      TEXT,
    due_date_type TEXT NOT NULL DEFAULT 'none'
                  CHECK (due_date_type IN ('explicit', 'inferred', 'manual', 'none')),
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    completed_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_notices_created  ON notices(created_at);
CREATE INDEX IF NOT EXISTS idx_notices_category ON notices(category);
CREATE INDEX IF NOT EXISTS idx_deadlines_notice ON deadlines(notice_id);
CREATE INDEX IF NOT EXISTS idx_deadlines_date   ON deadlines(deadline_date);
CREATE INDEX IF NOT EXISTS idx_actions_notice   ON action_items(notice_id);
CREATE INDEX IF NOT EXISTS idx_actions_status   ON action_items(status);
CREATE INDEX IF NOT EXISTS idx_actions_due      ON action_items(due_date);
"""


class Backend:
    """Interface shared by both backends (see Database for how it is used)."""

    name = "base"
    like_op = "LIKE"
    json_param = "?"

    def connect(self):  # pragma: no cover - interface
        raise NotImplementedError

    def init_schema(self) -> List[str]:  # pragma: no cover - interface
        raise NotImplementedError

    def describe(self) -> Dict[str, Any]:  # pragma: no cover - interface
        raise NotImplementedError

    def ping(self) -> Dict[str, Any]:  # pragma: no cover - interface
        raise NotImplementedError

    def close(self) -> None:
        return None

    # value helpers: dates/timestamps are exchanged with the app as ISO strings
    def now(self):  # pragma: no cover - interface
        raise NotImplementedError

    def to_date(self, value: Any):  # pragma: no cover - interface
        raise NotImplementedError

    def to_timestamp(self, value: Any):  # pragma: no cover - interface
        raise NotImplementedError

    def day_start(self, day: date):  # pragma: no cover - interface
        raise NotImplementedError


def _parse_date(value: Any) -> Optional[date]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _parse_timestamp(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


# ----------------------------------------------------------------------------
# SQLite (local fallback)
# ----------------------------------------------------------------------------
class SQLiteSession:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Result:
        cur = self.conn.execute(sql, list(params))
        rows = [dict(r) for r in cur.fetchall()] if cur.description else []
        return Result(rows, cur.rowcount)

    def script(self, sql: str) -> None:
        self.conn.executescript(sql)


class SQLiteBackend(Backend):
    name = "sqlite"
    like_op = "LIKE"  # case-insensitive for ASCII in SQLite

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self) -> Iterator[SQLiteSession]:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            with conn:  # commit / rollback
                yield SQLiteSession(conn)
        except sqlite3.IntegrityError as exc:
            raise DatabaseIntegrityError("The database rejected the data (constraint violation).", "integrity") from exc
        except sqlite3.Error as exc:
            logger.warning("SQLite error: %s", type(exc).__name__)
            raise DatabaseError(f"The local database could not complete the request ({type(exc).__name__}).",
                                "query") from exc
        finally:
            conn.close()

    def init_schema(self) -> List[str]:
        with self.connect() as s:
            s.script(SQLITE_SCHEMA)
        return []

    def describe(self) -> Dict[str, Any]:
        size = self.path.stat().st_size if self.path.exists() else 0
        return {"kind": "sqlite", "label": "Local SQLite (development fallback)", "location": str(self.path),
                "size_bytes": size}

    def ping(self) -> Dict[str, Any]:
        started = time.monotonic()
        with self.connect() as s:
            version = s.execute("SELECT sqlite_version() AS v").one()["v"]
        return {"version": f"SQLite {version}", "database": self.path.name, "seconds": time.monotonic() - started}

    def now(self) -> str:
        return datetime.now().isoformat(timespec="seconds")

    def to_date(self, value: Any) -> Optional[str]:
        parsed = _parse_date(value)
        return parsed.isoformat() if parsed else None

    def to_timestamp(self, value: Any) -> Optional[str]:
        parsed = _parse_timestamp(value)
        if parsed is None:
            return None
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone().replace(tzinfo=None)
        return parsed.isoformat(timespec="seconds")

    def day_start(self, day: date) -> str:
        return f"{day.isoformat()}T00:00:00"


# ----------------------------------------------------------------------------
# PostgreSQL / Supabase
# ----------------------------------------------------------------------------
class PgSession:
    def __init__(self, backend: "PostgresBackend", conn):
        self.backend = backend
        self.conn = conn

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Result:
        cur = self.conn.execute(self.backend.prepare_sql(sql), list(params))
        rows = [self.backend.normalize_row(r) for r in cur.fetchall()] if cur.description else []
        return Result(rows, cur.rowcount)

    def script(self, sql: str) -> None:
        """Run a multi-statement script (no parameters)."""
        self.conn.execute(sql)  # no params => simple query protocol, multiple statements allowed


def validate_url(url: str) -> None:
    if not url:
        raise DatabaseConfigError("DATABASE_URL is empty.", "config")
    if "[YOUR-PASSWORD]" in url or "YOUR_SUPABASE" in url or "YOUR_PASSWORD" in url:
        raise DatabaseConfigError(
            "DATABASE_URL still contains a placeholder. Replace [YOUR-PASSWORD] with your real database password "
            "(without the brackets).", "config")
    if not re.match(r"^postgres(ql)?://", url, re.IGNORECASE):
        raise DatabaseConfigError(
            "DATABASE_URL must start with postgresql:// (copy the 'Connection string' from Supabase: "
            "Project Settings > Database).", "config")


def build_conninfo(url: str, timeout: Optional[int] = None) -> "tuple[str, Dict[str, Any]]":
    """Validate DATABASE_URL and turn it into (libpq conninfo, parameter dict).

    The single place where the URL is parsed, shared by the app and the diagnostics so they cannot
    disagree. Both return values contain the password: never log or print them.
    """
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    url = (url or "").strip()
    validate_url(url)
    timeout = timeout or config.DB_CONNECT_TIMEOUT_SECONDS
    if urlsplit(url).netloc.count("@") > 1:
        # libpq would read part of the password as the host and report a confusing "host not found"
        raise DatabaseConfigError(
            "DATABASE_URL contains an unencoded '@' (usually in the password). URL-encode special characters "
            "in the password: @ becomes %40, # becomes %23, / becomes %2F, : becomes %3A, ? becomes %3F.", "config")
    try:
        parts = urlsplit(url)
        parts.port  # raises ValueError for a non-numeric / out-of-range port
        if not parts.hostname:
            raise ValueError("no host")
        params = conninfo_to_dict(url)
    except (psycopg.Error, ValueError):
        raise DatabaseConfigError(
            "DATABASE_URL could not be parsed. Use: postgresql://USER:PASSWORD@HOST:PORT/postgres "
            "(URL-encode special characters in the password).", "config") from None
    if "sslmode" not in params and not is_local_host(params.get("host")):
        params["sslmode"] = "require"  # remote databases always use SSL
    params.setdefault("connect_timeout", str(timeout))
    params.setdefault("application_name", "notice2action-ai")
    return make_conninfo(**params), params


def url_hygiene(value: str) -> List[str]:
    """Copy/paste problems in the raw URL text, reported by kind only (never the characters themselves)."""
    problems = []
    if any(c.isspace() for c in value):
        problems.append("contains whitespace or newline characters")
    if any(ord(c) < 32 or ord(c) == 127 for c in value if not c.isspace()):
        problems.append("contains control characters")
    if any(ord(c) > 127 for c in value):
        problems.append("contains non-ASCII or invisible characters (for example a zero-width space or smart quotes)")
    if re.search(r"[\"'`]", value):
        problems.append("contains quote characters")
    if re.search(r"[<>\[\]{}\\]", value.split("@", 1)[0]):
        problems.append("contains < > [ ] { } or a backslash before the host (a placeholder left in?)")
    return problems


def url_warnings(url: str) -> List[str]:
    """Heuristic, secret-free hints about a DATABASE_URL that will probably not work. Never blocks anything."""
    warnings: List[str] = []
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
    except ValueError:
        return warnings
    user = unquote(parts.netloc.rsplit("@", 1)[0].partition(":")[0]) if "@" in parts.netloc else ""
    if host.endswith(".pooler.supabase.com"):
        segments = user.split(".")
        if len(segments) > 2:
            warnings.append(
                f"The user name has {len(segments)} dot-separated parts, but Supabase pooler users have exactly two: "
                "'postgres.<project-ref>'. Remove everything after the project ref (and the dot before it).")
        elif len(segments) == 1:
            warnings.append("The user name has no '.<project-ref>' part; shared-pooler users are "
                            "'postgres.<project-ref>'.")
        placeholders = [seg for seg in segments[1:] if re.fullmatch(r"[A-Z][A-Z_\-]{2,}", seg)]
        if placeholders:
            warnings.append("Part of the user name is written in CAPITAL LETTERS (and underscores), like a template "
                            "variable name. That looks like a placeholder that was never replaced by the real project ref.")
        ref = segments[1] if len(segments) >= 2 else ""
        if ref and not re.fullmatch(r"[a-z0-9]{20}", ref):
            warnings.append("The project ref (the part after 'postgres.') is not the usual 20 lowercase letters/digits; "
                            "compare it with Supabase > Project Settings > General > Reference ID.")
    warnings.extend(f"The DATABASE_URL text {problem}." for problem in url_hygiene(url))
    return warnings


def connection_secrets(url: str, params: Dict[str, Any]) -> List[str]:
    """Strings to mask in any diagnostic text: password, pooler user / project ref, and the URL itself."""
    user, password = str(params.get("user") or ""), str(params.get("password") or "")
    ref = user.split(".", 1)[1] if "." in user else ""
    return [s for s in (password, user if "." in user else "", ref, url) if s]


class PostgresBackend(Backend):
    name = "postgres"
    like_op = "ILIKE"
    json_param = "CAST(? AS JSONB)"

    BOOTSTRAP_SQL = """
        CREATE TABLE IF NOT EXISTS public.schema_migrations (
            version    TEXT PRIMARY KEY,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        ALTER TABLE public.schema_migrations ENABLE ROW LEVEL SECURITY;
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
                REVOKE ALL ON public.schema_migrations FROM anon; END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
                REVOKE ALL ON public.schema_migrations FROM authenticated; END IF;
        END $$;
    """

    def __init__(self, url: str, *, max_size: Optional[int] = None, connect_timeout: Optional[int] = None):
        try:
            import psycopg
            from psycopg.conninfo import conninfo_to_dict, make_conninfo
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool
        except ImportError as exc:  # pragma: no cover
            raise DatabaseConfigError("The PostgreSQL driver is missing. Run: pip install -r requirements.txt",
                                      "config") from exc
        self._psycopg = psycopg
        url = (url or "").strip()
        timeout = connect_timeout or config.DB_CONNECT_TIMEOUT_SECONDS
        self._url_hints = url_warnings(url)                      # structure-only hints, no secrets
        self._conninfo, params = build_conninfo(url, timeout)  # holds the password; never logged or displayed
        self._secrets = connection_secrets(url, params)         # masked if a driver message ever echoes them
        self._target = describe_url(url)
        self._target["ssl"] = params.get("sslmode", "prefer")
        self._timeout = timeout

        self._probe()  # fail fast with a precise, friendly error
        self.pool = ConnectionPool(
            self._conninfo,
            min_size=1,
            max_size=max_size or config.DB_POOL_MAX_SIZE,
            kwargs={"row_factory": dict_row, "prepare_threshold": None},  # None: required by PgBouncer/Supavisor
            check=ConnectionPool.check_connection,  # drop connections the server closed while idle
            timeout=timeout + 5,
            max_idle=300,
            max_lifetime=1800,
            open=False,
            name="notice2action",
        )
        self.pool.open()

    def _probe(self) -> None:
        try:
            with self._psycopg.connect(self._conninfo, connect_timeout=self._timeout) as conn:
                conn.execute("SELECT 1")
        except Exception as exc:
            err = translate_pg_error(exc, self._secrets)
            d = err.details
            logger.warning(
                "Database connection failed: host=%s port=%s kind=%s exception=%s sqlstate=%s detail=%s",
                self._target["host"], self._target["port"], err.kind,
                d.exc_class if d else type(exc).__name__, (d.sqlstate if d and d.sqlstate else "-"),
                d.sanitized if d else "-")
            if self._url_hints:  # the most useful clue when login fails: something looks wrong in the URL itself
                logger.warning("The configured database URL looks suspicious: %s", " | ".join(self._url_hints))
                err = type(err)(f"{err} Possible cause in DATABASE_URL: {' '.join(self._url_hints)}", err.kind, d)
            raise err from None

    @contextmanager
    def connect(self) -> Iterator[PgSession]:
        psycopg = self._psycopg
        try:
            with self.pool.connection() as conn:  # commit on success, rollback on error, always released
                yield PgSession(self, conn)
        except DatabaseError:
            raise
        except psycopg.errors.IntegrityError as exc:
            raise DatabaseIntegrityError("The database rejected the data (constraint violation).", "integrity") from exc
        except psycopg.errors.DataError as exc:
            raise DatabaseError("The database rejected a value (invalid data).", "data") from exc
        except psycopg.Error as exc:
            err = translate_pg_error(exc, self._secrets)
            logger.warning("Database error: kind=%s exception=%s sqlstate=%s", err.kind, type(exc).__name__,
                           (err.details.sqlstate if err.details and err.details.sqlstate else "-"))
            raise err from None
        except Exception as exc:
            if type(exc).__name__ in {"PoolTimeout", "PoolClosed"}:
                err = translate_pg_error(exc, self._secrets)
                logger.warning("Database pool error: kind=%s exception=%s", err.kind, type(exc).__name__)
                raise err from None
            raise

    # -- SQL adaptation ------------------------------------------------------
    @staticmethod
    def prepare_sql(sql: str) -> str:
        return sql.replace("%", "%%").replace("?", "%s")

    @staticmethod
    def normalize_row(row: Dict[str, Any]) -> Dict[str, Any]:
        out = {}
        for key, value in row.items():
            if isinstance(value, datetime):
                if value.tzinfo is not None:
                    value = value.astimezone().replace(tzinfo=None)  # server-local wall time, like SQLite rows
                value = value.isoformat(timespec="seconds")
            elif isinstance(value, date):
                value = value.isoformat()
            out[key] = value
        return out

    def now(self) -> datetime:
        return datetime.now().astimezone()

    def to_date(self, value: Any) -> Optional[date]:
        return _parse_date(value)

    def to_timestamp(self, value: Any) -> Optional[datetime]:
        parsed = _parse_timestamp(value)
        if parsed is not None and parsed.tzinfo is None:
            parsed = parsed.astimezone()  # naive values are local time
        return parsed

    def day_start(self, day: date) -> datetime:
        return datetime(day.year, day.month, day.day).astimezone()

    # -- schema / info ------------------------------------------------------
    def migration_files(self) -> List[Path]:
        return sorted(config.MIGRATIONS_DIR.glob("*.sql"))

    def init_schema(self) -> List[str]:
        """Apply pending SQL files from supabase/migrations (idempotent, concurrency-safe).

        Returns the versions applied by this call.
        """
        files = self.migration_files()
        if not files:
            raise DatabaseConfigError(f"No migration files found in {config.MIGRATIONS_DIR}.", "config")
        with self.connect() as s:
            s.script(self.BOOTSTRAP_SQL)
        applied_now: List[str] = []
        for path in files:
            version = path.stem
            with self.connect() as s:
                s.execute("SELECT pg_advisory_xact_lock(727274)")  # one migrator at a time
                done = s.execute("SELECT 1 AS x FROM public.schema_migrations WHERE version = ?", (version,)).one()
                if done:
                    continue
                s.script(path.read_text(encoding="utf-8"))
                s.execute("INSERT INTO public.schema_migrations (version) VALUES (?)", (version,))
                applied_now.append(version)
                logger.info("Applied database migration %s", version)
        return applied_now

    def describe(self) -> Dict[str, Any]:
        host = self._target["host"]
        label = "Supabase PostgreSQL" if host.endswith((".supabase.com", ".supabase.co")) else "PostgreSQL"
        return {"kind": "postgres", "label": label, **self._target}

    def ping(self) -> Dict[str, Any]:
        started = time.monotonic()
        with self.connect() as s:
            row = s.execute("SELECT version() AS v, current_database() AS d").one()
        return {"version": row["v"].split(",")[0], "database": row["d"], "seconds": time.monotonic() - started}

    def close(self) -> None:
        try:
            self.pool.close()
        except Exception:  # pragma: no cover - best effort
            logger.debug("Pool close failed", exc_info=False)
