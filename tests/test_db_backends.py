"""Connection configuration, error translation, failure handling and PostgreSQL schema checks."""
import logging
from pathlib import Path

import psycopg
import pytest

import config
import database
import db_backends as dbb
from db_backends import (DatabaseConfigError, DatabaseConnectionError, DatabaseError, DatabaseIntegrityError,
                         PostgresBackend, SQLiteBackend)

PASSWORD = "S3cr3t-P@ss!"
POOLER_URL = "postgresql://postgres.abcdefghijklmnop:S3cr3t%2DP%40ss%21@aws-0-eu-west-1.pooler.supabase.com:6543/postgres"


# ------------------------------------------------------------------ configuration
def test_database_url_read_from_environment(monkeypatch):
    assert config.get_database_url() is None and config.database_backend() == "sqlite"
    monkeypatch.setenv("DATABASE_URL", POOLER_URL)
    assert config.get_database_url() == POOLER_URL and config.database_backend() == "postgres"


def test_database_url_read_from_streamlit_secrets(monkeypatch):
    monkeypatch.setattr(config, "_secret", lambda name: POOLER_URL if name == "DATABASE_URL" else None)
    assert config.get_database_url() == POOLER_URL


@pytest.mark.parametrize("value", ["", "   ", "YOUR_SUPABASE_POSTGRESQL_CONNECTION_STRING", "changeme"])
def test_placeholder_database_url_means_not_configured(monkeypatch, value):
    monkeypatch.setenv("DATABASE_URL", value)
    assert config.get_database_url() is None


def test_backend_selection(monkeypatch, tmp_path):
    created = {}

    class Stub:
        name = "postgres"

        def __init__(self, url):
            created["url"] = url

    monkeypatch.setattr(database, "PostgresBackend", Stub)
    assert isinstance(database.make_backend(tmp_path / "a.db"), SQLiteBackend)      # explicit path wins
    monkeypatch.setenv("NOTICE2ACTION_DB", str(tmp_path / "b.db"))
    assert isinstance(database.make_backend(), SQLiteBackend)                       # no DATABASE_URL -> SQLite fallback
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")
    assert isinstance(database.make_backend(), Stub) and created["url"] == "postgresql://u:p@h/db"
    assert isinstance(database.make_backend(tmp_path / "c.db"), SQLiteBackend)      # explicit path still wins


def test_auto_migrate_flag(monkeypatch):
    assert config.db_auto_migrate() is True
    monkeypatch.setenv("DATABASE_AUTO_MIGRATE", "false")
    assert config.db_auto_migrate() is False


@pytest.mark.parametrize("url,fragment", [
    ("", "empty"),
    ("https://abc.supabase.co", "must start with postgresql://"),
    ("mysql://u:p@h/db", "must start with postgresql://"),
    ("postgresql://postgres:[YOUR-PASSWORD]@db.abc.supabase.co:5432/postgres", "placeholder"),
])
def test_invalid_urls_rejected_before_connecting(url, fragment):
    with pytest.raises(DatabaseConfigError, match=fragment):
        PostgresBackend(url)


def test_unparsable_url_gives_config_error():
    with pytest.raises(DatabaseConfigError, match="could not be parsed"):
        PostgresBackend("postgresql://user:pass@host:notaport/db")


# ------------------------------------------------- connection options (no network)
@pytest.fixture
def captured_pool(monkeypatch):
    """Build a PostgresBackend without touching the network and capture how the pool is configured."""
    import psycopg_pool
    seen = {}

    class FakePool:
        check_connection = staticmethod(lambda conn: None)

        def __init__(self, conninfo, **kwargs):
            seen["conninfo"], seen["kwargs"] = conninfo, kwargs

        def open(self, *a, **k):
            seen["opened"] = True

    monkeypatch.setattr(psycopg_pool, "ConnectionPool", FakePool)
    monkeypatch.setattr(PostgresBackend, "_probe", lambda self: None)
    return seen


def test_remote_hosts_use_ssl_and_pooler_settings(captured_pool):
    backend = PostgresBackend(POOLER_URL)
    info = psycopg.conninfo.conninfo_to_dict(captured_pool["conninfo"])
    assert info["sslmode"] == "require"                       # SSL forced for remote hosts
    assert info["user"] == "postgres.abcdefghijklmnop" and info["port"] == "6543"
    assert info["password"] == PASSWORD                        # URL-decoded correctly
    assert info["connect_timeout"] == str(config.DB_CONNECT_TIMEOUT_SECONDS)
    kwargs = captured_pool["kwargs"]["kwargs"]
    assert kwargs["prepare_threshold"] is None                 # required for Supabase transaction pooler
    assert captured_pool["kwargs"]["max_size"] == config.DB_POOL_MAX_SIZE
    assert captured_pool["kwargs"]["open"] is False and captured_pool["opened"]
    assert backend.describe()["label"] == "Supabase PostgreSQL" and backend.describe()["port"] == 6543


def test_local_hosts_do_not_force_ssl_and_explicit_sslmode_is_kept(captured_pool):
    PostgresBackend("postgresql://u:p@localhost:5432/postgres")
    assert "sslmode" not in psycopg.conninfo.conninfo_to_dict(captured_pool["conninfo"])
    PostgresBackend("postgresql://u:p@db.example.com:5432/postgres?sslmode=verify-full")
    assert psycopg.conninfo.conninfo_to_dict(captured_pool["conninfo"])["sslmode"] == "verify-full"


def test_describe_never_exposes_credentials(captured_pool):
    described = PostgresBackend(POOLER_URL).describe()
    assert PASSWORD not in str(described) and "postgres.abcdefghijklmnop" not in str(described)


# ------------------------------------------------------------- redaction / errors
def test_redact_hides_credentials():
    text = f"could not connect to postgresql://user:{PASSWORD}@host:5432/db and postgres://a:b@c/d"
    out = dbb.redact(text)
    assert PASSWORD not in out and "user" not in out and "***@host:5432/db" in out and "***@c/d" in out


@pytest.mark.parametrize("message,kind", [
    ('connection failed: FATAL: password authentication failed for user "postgres"', "auth"),
    ("FATAL: Tenant or user not found", "auth"),
    ('could not translate host name "db.abc.supabase.co" to address: Name or service not known', "dns"),
    ("connection failed: SSL connection has been closed unexpectedly", "ssl"),
    ("connection timeout expired", "timeout"),
    ("connection failed: Connection refused", "refused"),
    ("FATAL: remaining connection slots are reserved", "capacity"),
    ("server closed the connection unexpectedly", "dropped"),
    ("something odd", "query"),
])
def test_error_translation(message, kind):
    err = dbb.translate_pg_error(psycopg.OperationalError(message))
    assert isinstance(err, DatabaseError) and err.kind == kind
    assert "technical: OperationalError" in str(err)


def test_translated_messages_are_actionable_and_secret_free():
    auth = str(dbb.translate_pg_error(psycopg.OperationalError(f'password authentication failed for "{PASSWORD}"')))
    assert "URL-encode" in auth and "postgres.<project-ref>" in auth
    dns = str(dbb.translate_pg_error(psycopg.OperationalError("could not translate host name")))
    assert "pooler" in dns.lower()


def test_unencoded_at_sign_in_password_is_detected_and_not_leaked(caplog):
    url = f"postgresql://someuser:{PASSWORD}@127.0.0.1:1/postgres"      # PASSWORD contains a raw '@'
    with caplog.at_level(logging.DEBUG), pytest.raises(DatabaseConfigError, match="URL-encode") as err:
        PostgresBackend(url, connect_timeout=3)
    assert PASSWORD not in str(err.value) + caplog.text and "someuser" not in str(err.value) + caplog.text


def test_connection_refused_is_friendly_and_leaks_nothing(caplog):
    url = "postgresql://someuser:S3cr3t%2DP%40ss%21@127.0.0.1:1/postgres"  # properly encoded; nothing listens on port 1
    with caplog.at_level(logging.DEBUG), pytest.raises(DatabaseConnectionError) as err:
        PostgresBackend(url, connect_timeout=3)
    shown = str(err.value) + caplog.text
    assert PASSWORD not in shown and "S3cr3t" not in shown and "someuser" not in shown and url not in shown
    assert err.value.kind in {"refused", "timeout"}


def test_unknown_host_fails_gracefully():
    with pytest.raises(DatabaseConnectionError) as err:
        PostgresBackend("postgresql://u:S3cr3t%2DP%40ss%21@no-such-host.invalid:5432/postgres", connect_timeout=3)
    assert PASSWORD not in str(err.value) and "S3cr3t" not in str(err.value)


def test_outage_after_startup_raises_clean_database_error(pg_db):
    pg_db.count_notices()                     # works
    pg_db.backend.pool.close()                # simulate the connection pool being lost
    with pytest.raises(DatabaseError) as err:
        pg_db.count_notices()
    assert "postgresql://" not in str(err.value).lower()


def test_integrity_errors_are_translated(db):
    with pytest.raises(DatabaseIntegrityError):
        with db.connect() as s:
            s.execute("INSERT INTO deadlines (notice_id, description, date_type, created_at) VALUES (?, ?, ?, ?)",
                      (999999, "orphan", "explicit", db.backend.now()))


def test_check_constraints_enforced_by_the_database(db):
    """Even bypassing the Python validation, the database itself rejects invalid values."""
    from conftest import NOTICE_TEXT, make_gemini_payload
    import ai_engine
    nid, _ = db.save_notice(ai_engine.validate_analysis(make_gemini_payload()), NOTICE_TEXT, None)
    now = db.backend.now()
    with pytest.raises(DatabaseIntegrityError):
        with db.connect() as s:
            s.execute("INSERT INTO action_items (notice_id, title, priority, created_at, updated_at) "
                      "VALUES (?, 'x', 'urgent', ?, ?)", (nid, now, now))
    with pytest.raises(DatabaseIntegrityError):
        with db.connect() as s:
            s.execute("INSERT INTO deadlines (notice_id, description, date_type, created_at) "
                      "VALUES (?, 'x', 'maybe', ?)", (nid, now))


def test_blank_title_rejected_by_postgres_check(pg_db):
    with pytest.raises(DatabaseIntegrityError):
        with pg_db.connect() as s:
            s.execute("INSERT INTO notices (title, category, content_hash, analysis_json, created_at) "
                      f"VALUES (?, 'Other', ?, {pg_db.backend.json_param}, ?)", ("   ", "h", "{}", pg_db.backend.now()))


def test_sqlite_errors_are_translated(tmp_path):
    bad = tmp_path / "notadb.db"
    bad.write_bytes(b"this is not a database" * 100)
    with pytest.raises(DatabaseError):
        database.Database(bad)


# ------------------------------------------------------------ PostgreSQL schema
@pytest.fixture
def fresh_pg(pg_test_url):
    """Empty schema: drop everything, then let Database() recreate it from the migrations."""
    with psycopg.connect(pg_test_url, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS action_items, deadlines, notices, schema_migrations CASCADE")
    db = database.Database(url=pg_test_url)
    yield db
    db.close()


def _rows(db, sql, params=()):
    with db.connect() as s:
        return s.execute(sql, params).rows


def test_schema_initialisation_is_applied_and_idempotent(pg_test_url):
    with psycopg.connect(pg_test_url, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS action_items, deadlines, notices, schema_migrations CASCADE")
    db = database.Database(url=pg_test_url, init=False)
    applied = db.init_schema()
    assert applied == [p.stem for p in sorted(config.MIGRATIONS_DIR.glob("*.sql"))] and applied
    assert db.init_schema() == []                    # second run applies nothing
    assert db.table_counts() == {"notices": 0, "deadlines": 0, "action_items": 0}
    versions = [r["version"] for r in _rows(db, "SELECT version FROM schema_migrations")]
    assert versions == applied
    db.close()


def test_migration_file_can_be_run_twice(fresh_pg):
    sql = next(iter(sorted(config.MIGRATIONS_DIR.glob("*.sql")))).read_text(encoding="utf-8")
    with fresh_pg.connect() as s:
        s.script(sql)
        s.script(sql)                                # CREATE ... IF NOT EXISTS: idempotent


def test_expected_columns_and_types(fresh_pg):
    cols = {(r["table_name"], r["column_name"]): (r["data_type"], r["is_nullable"]) for r in _rows(
        fresh_pg, "SELECT table_name, column_name, data_type, is_nullable FROM information_schema.columns "
                  "WHERE table_schema = 'public'")}
    assert cols[("notices", "id")][0] == "bigint"
    assert cols[("notices", "announcement_date")] == ("date", "YES")
    assert cols[("notices", "created_at")] == ("timestamp with time zone", "NO")
    assert cols[("notices", "analysis_json")] == ("jsonb", "NO")
    assert cols[("notices", "content_hash")] == ("text", "NO")
    assert cols[("deadlines", "deadline_date")] == ("date", "YES")
    assert cols[("deadlines", "date_type")] == ("text", "NO")
    for name in ("due_date",):
        assert cols[("action_items", name)] == ("date", "YES")
    for name in ("created_at", "updated_at"):
        assert cols[("action_items", name)] == ("timestamp with time zone", "NO")
    assert cols[("action_items", "completed_at")] == ("timestamp with time zone", "YES")
    for needed in ("title", "description", "priority", "status", "due_date_type", "deadline_id", "notice_id"):
        assert ("action_items", needed) in cols


def test_foreign_keys_and_cascade_rules(fresh_pg):
    rules = {(r["table_name"], r["column_name"]): r["delete_rule"] for r in _rows(
        fresh_pg, """SELECT kcu.table_name, kcu.column_name, rc.delete_rule
                     FROM information_schema.referential_constraints rc
                     JOIN information_schema.key_column_usage kcu
                       ON kcu.constraint_name = rc.constraint_name AND kcu.constraint_schema = rc.constraint_schema
                     WHERE rc.constraint_schema = 'public'""")}
    assert rules[("deadlines", "notice_id")] == "CASCADE"
    assert rules[("action_items", "notice_id")] == "CASCADE"
    assert rules[("action_items", "deadline_id")] == "SET NULL"


def test_indexes_exist(fresh_pg):
    names = {r["indexname"] for r in _rows(fresh_pg, "SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")}
    for expected in ("idx_notices_created_at", "idx_notices_category", "idx_deadlines_notice", "idx_deadlines_date",
                     "idx_actions_notice", "idx_actions_deadline", "idx_actions_status", "idx_actions_due_date"):
        assert expected in names
    assert "notices_content_hash_key" in names       # UNIQUE(content_hash) backs duplicate detection


def test_row_level_security_enabled_everywhere(fresh_pg):
    rls = {r["relname"]: r["relrowsecurity"] for r in _rows(
        fresh_pg, "SELECT c.relname, c.relrowsecurity FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                  "WHERE n.nspname = 'public' AND c.relkind = 'r'")}
    for table in ("notices", "deadlines", "action_items", "schema_migrations"):
        assert rls[table] is True, f"RLS must be enabled on {table}"


def test_api_roles_get_no_table_access_when_they_exist(pg_test_url):
    """On Supabase the anon/authenticated roles exist; they must have no privileges on app tables."""
    with psycopg.connect(pg_test_url, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS action_items, deadlines, notices, schema_migrations CASCADE")
        for role in ("anon", "authenticated"):
            exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
            if not exists:
                conn.execute(f"CREATE ROLE {role} NOLOGIN")      # simulate Supabase's API roles
        conn.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO anon, authenticated")
    db = database.Database(url=pg_test_url)
    try:
        grants = _rows(db, "SELECT grantee, table_name FROM information_schema.role_table_grants "
                           "WHERE table_schema = 'public' AND grantee IN ('anon', 'authenticated')")
        assert grants == [], f"API roles must not have table grants: {grants}"
    finally:
        db.close()
        with psycopg.connect(pg_test_url, autocommit=True) as conn:
            conn.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM anon, authenticated")


def test_session_timezone_does_not_change_stored_instants(fresh_pg):
    """created_at is TIMESTAMPTZ: the stored instant is independent of the server/session timezone."""
    fresh_pg.save_notice({"title": "tz", "deadlines": [], "action_items": []}, "tz text")
    row = _rows(fresh_pg, "SELECT created_at FROM notices")[0]
    assert len(row["created_at"]) == 19 and "T" in row["created_at"]


def test_migrations_directory_is_present():
    files = list(Path(config.MIGRATIONS_DIR).glob("*.sql"))
    assert files and all(f.stem[:14].isdigit() for f in files)    # Supabase CLI timestamp naming
