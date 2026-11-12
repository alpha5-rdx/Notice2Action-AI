"""Connection-failure diagnostics: classification, sanitising, config sources and the diagnose command.

Everything is mocked: no test here contacts Supabase or any network host.
"""
import logging

import psycopg
import pytest

import config
import db_backends as dbb
import db_diagnose
import db_tools
from db_backends import DatabaseConnectionError, PostgresBackend

PASSWORD = "Pw-Sup3rSecret99"
REF = "abcdefghijklmnopqrst"                       # 20 lowercase letters: classic project ref shape
GOOD_URL = f"postgresql://postgres.{REF}:{PASSWORD}@aws-0-ap-northeast-1.pooler.supabase.com:5432/postgres"
BAD_URL = f"postgresql://postgres.PROJECT_REF.{REF}:{PASSWORD}@aws-0-ap-northeast-1.pooler.supabase.com:5432/postgres"
EAUTHQUERY = ('connection failed: connection to server at "52.68.3.1", port 5432 failed: '
              'FATAL:  (EAUTHQUERY) user not found in the database')
SECRETS = [PASSWORD, f"postgres.PROJECT_REF.{REF}", REF, "PROJECT_REF"]


def assert_no_secrets(text, *extra):
    for secret in (PASSWORD, REF, BAD_URL, GOOD_URL, *extra):
        assert secret not in text, f"leaked {secret[:6]}..."


# ------------------------------------------------------------ the original bug
@pytest.mark.parametrize("message", [
    "connection failed: connection to server at \"1.2.3.4\", port 5432 failed: FATAL: something unexpected",
    "connection failed",
    "could not connect to server",
])
def test_connection_failed_alone_is_no_longer_called_refused(message):
    """psycopg prefixes nearly every error with 'connection failed': that must not be read as a refusal."""
    err = dbb.translate_pg_error(psycopg.OperationalError(message))
    assert err.kind == "connect" and err.kind != "refused"


@pytest.mark.parametrize("message", [
    'connection failed: connection to server at "127.0.0.1", port 5432 failed: Connection refused',
    "connection failed: connection to server at \"x\", port 1 failed: Connection refused (0x0000274D/10061)",
    "[WinError 10061] No connection could be made because the target machine actively refused it",
])
def test_real_tcp_refusal_is_still_recognised(message):
    assert dbb.translate_pg_error(psycopg.OperationalError(message)).kind == "refused"


def test_a_server_fatal_mentioning_refused_is_not_a_tcp_refusal():
    err = dbb.translate_pg_error(psycopg.OperationalError(
        "connection failed: connection to server failed: FATAL: connection refused by upstream policy"))
    assert err.kind != "refused"


# ------------------------------------------------------ Supabase pooler errors
def test_eauthquery_is_explained_and_names_the_user_name_as_the_suspect():
    err = dbb.translate_pg_error(psycopg.OperationalError(EAUTHQUERY), SECRETS)
    assert err.kind == "auth" and "EAUTHQUERY" in str(err)
    assert "before the dot" in str(err) and "Session pooler" in str(err)
    assert 'The server said: "FATAL: (EAUTHQUERY) user not found in the database"' in str(err)
    assert "52.68.3.1" not in str(err)                      # hosts/IPs are kept out of the UI message


@pytest.mark.parametrize("text,kind", [
    ("FATAL: (ENOTFOUND) tenant/user postgres.abc not found", "auth"),
    ("FATAL: Tenant or user not found", "auth"),
    ("FATAL: (ECIRCUITBREAKER) too many authentication errors", "blocked"),
    ("FATAL: (EDBHANDLEREXITED) db handler exited", "upstream"),
    ("FATAL: (ECONNREFUSED) connection refused", "upstream"),
    ('FATAL: password authentication failed for user "postgres.abc"', "auth"),
    ('FATAL: database "nope" does not exist', "database"),
    ("FATAL: (EMAXCLIENT) max client connections reached", "capacity"),
])
def test_supabase_pooler_messages(text, kind):
    assert dbb.translate_pg_error(psycopg.OperationalError(f"connection failed: {text}")).kind == kind


def test_error_details_capture_class_sqlstate_and_server_message():
    exc = psycopg.errors.InvalidPassword('FATAL: password authentication failed for user "alice"')
    err = dbb.translate_pg_error(exc)
    d = err.details
    assert d.exc_class == "InvalidPassword" and d.sqlstate == "28P01"
    assert "SQLSTATE 28P01" in str(err) and "technical: InvalidPassword" in str(err)
    assert "alice" not in str(err) and "alice" not in d.sanitized      # role names masked


def test_server_message_is_complete_and_deduplicated_for_multi_address_failures():
    text = ("connection failed: connection to server at \"1.1.1.1\", port 5432 failed: FATAL: (EAUTHQUERY) user not "
            "found in the database Multiple connection attempts failed. All failures were: - host: 'h', port: "
            "'5432', hostaddr: '2.2.2.2': connection failed: connection to server at \"2.2.2.2\", port 5432 failed: "
            "FATAL: (EAUTHQUERY) user not found in the database")
    d = dbb.extract_details(psycopg.OperationalError(text))
    assert d.server_message == "FATAL: (EAUTHQUERY) user not found in the database"     # not cut mid-word, not repeated


# ------------------------------------------------------------------ sanitising
def test_sanitize_masks_password_user_ref_and_urls():
    text = (f"failed for {GOOD_URL} user=postgres.{REF} password={PASSWORD} role \"postgres.{REF}\" "
            f"and url-encoded {PASSWORD.replace('-', '%2D')}")
    out = dbb.sanitize_error_text(text, [PASSWORD, f"postgres.{REF}", REF])
    assert_no_secrets(out)
    assert "postgres." not in out.replace("postgresql://***@", "")


def test_sanitize_masks_a_password_that_contains_an_at_sign():
    out = dbb.sanitize_error_text("could not connect to postgresql://u:S3cr3t-P@ss!@host:5432/db")
    assert "S3cr3t" not in out and "ss!" not in out


def test_connection_secrets_cover_pooler_user_and_ref():
    _ci, params = dbb.build_conninfo(GOOD_URL)
    secrets = dbb.connection_secrets(GOOD_URL, params)
    assert PASSWORD in secrets and REF in secrets and f"postgres.{REF}" in secrets and GOOD_URL in secrets


# ---------------------------------------------------- URL warnings (structure only)
def test_url_warnings_flag_the_leftover_placeholder_user():
    hints = " ".join(dbb.url_warnings(BAD_URL))
    assert "3 dot-separated parts" in hints and "CAPITAL LETTERS" in hints
    assert_no_secrets(hints, "PROJECT_REF")


def test_url_warnings_quiet_for_a_correct_url_and_other_hosts():
    assert dbb.url_warnings(GOOD_URL) == []
    assert dbb.url_warnings("postgresql://postgres:pw@db.example.com:5432/postgres") == []
    assert dbb.url_warnings("postgresql://u:p@localhost/test") == []


def test_url_warnings_other_pooler_mistakes():
    assert any("no '.<project-ref>'" in w for w in dbb.url_warnings(
        "postgresql://postgres:pw@aws-0-eu-west-1.pooler.supabase.com:5432/postgres"))
    assert any("not the usual 20" in w for w in dbb.url_warnings(
        "postgresql://postgres.SHORT:pw@aws-0-eu-west-1.pooler.supabase.com:5432/postgres"))


def test_url_hygiene_reports_hidden_characters_by_kind_only():
    problems = dbb.url_hygiene("postgresql://postgres.abc:pw\u200b@h/db\n")
    joined = " ".join(problems)
    assert "whitespace" in joined and "non-ASCII" in joined and "\u200b" not in joined


def test_startup_failure_log_and_message_carry_details_and_hint_but_no_secrets(monkeypatch, caplog):
    def fake_connect(*args, **kwargs):
        raise psycopg.OperationalError(EAUTHQUERY)

    monkeypatch.setattr(psycopg, "connect", fake_connect)
    with caplog.at_level(logging.DEBUG), pytest.raises(DatabaseConnectionError) as err:
        PostgresBackend(BAD_URL)
    shown = str(err.value) + caplog.text
    assert "EAUTHQUERY" in shown and "exception=OperationalError" in caplog.text and "sqlstate=" in caplog.text
    assert "Possible cause in DATABASE_URL" in str(err.value) and "3 dot-separated parts" in str(err.value)
    assert err.value.details.exc_class == "OperationalError"
    assert_no_secrets(shown, "PROJECT_REF")


# ------------------------------------------------- where the URL comes from
def test_placeholder_in_secrets_does_not_hide_real_value_in_environment(monkeypatch):
    monkeypatch.setattr(config, "_secret", lambda n: "YOUR_SUPABASE_POSTGRESQL_CONNECTION_STRING" if n == "DATABASE_URL" else None)
    monkeypatch.setenv("DATABASE_URL", GOOD_URL)
    assert config.get_database_url() == GOOD_URL
    assert config.get_database_url_source() == "environment variable"


def test_secrets_win_over_a_different_environment_value_and_both_are_listed(monkeypatch):
    other = GOOD_URL.replace("ap-northeast-1", "eu-west-1")
    monkeypatch.setattr(config, "_secret", lambda n: GOOD_URL if n == "DATABASE_URL" else None)
    monkeypatch.setenv("DATABASE_URL", other)
    assert config.get_database_url() == GOOD_URL and config.get_database_url_source() == "Streamlit secrets"
    assert [s for s, _v in config.database_url_candidates()] == ["Streamlit secrets", "environment variable"]


def test_no_source_means_sqlite_fallback_and_no_silent_switch(monkeypatch):
    assert config.get_database_url() is None and config.get_database_url_source() is None
    monkeypatch.setenv("DATABASE_URL", GOOD_URL)
    assert config.database_backend() == "postgres"        # configured => PostgreSQL, never SQLite


def test_api_key_source_logic_unchanged(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "abc123456")
    assert config.get_api_key() == "abc123456" and config.get_api_key_source() == "environment variable"
    monkeypatch.setenv("GEMINI_API_KEY", "YOUR_GEMINI_API_KEY")
    assert config.get_api_key() is None and config.get_api_key_source() is None


# ------------------------------------------------------------ diagnose command
class Capture:
    def __init__(self):
        self.lines = []

    def __call__(self, line):
        self.lines.append(line)

    @property
    def text(self):
        return "\n".join(self.lines)


@pytest.fixture
def with_url(monkeypatch):
    def _set(url, extra_env=None):
        monkeypatch.setenv("DATABASE_URL", url)
        monkeypatch.setenv("APP_PASSWORD", "app-pass-123456")
        monkeypatch.setenv("GEMINI_API_KEY", "gemini-key-ABCDEF123456")
        for k, v in (extra_env or {}).items():
            monkeypatch.setenv(k, v)
    return _set


def test_parse_only_makes_no_network_calls_and_hides_everything(with_url, monkeypatch):
    with_url(BAD_URL)
    monkeypatch.setattr(db_diagnose, "_resolve", lambda *a, **k: pytest.fail("network used in --parse-only"))
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: pytest.fail("connection attempted in --parse-only"))
    out = Capture()
    code = db_diagnose.run(emit=out, parse_only=True)
    assert code == 1
    assert "3 dot-separated part(s)" in out.text and "part 3 (EXTRA)      : 20 chars; 20 lowercase" in out.text
    assert "10 uppercase" in out.text and "CAPITAL LETTERS" in out.text
    assert_no_secrets(out.text, "PROJECT_REF", "app-pass-123456", "gemini-key-ABCDEF123456")


def test_parse_only_is_clean_for_a_correct_url(with_url):
    with_url(GOOD_URL)
    out = Capture()
    assert db_diagnose.run(emit=out, parse_only=True) == 0
    assert "no structural problems found" in out.text and "[WARN]" not in out.text
    assert "2 dot-separated part(s)" in out.text
    assert_no_secrets(out.text)


def test_diagnose_without_database_url_explains_sqlite_fallback():
    out = Capture()
    assert db_diagnose.run(emit=out) == 2 and "SQLite fallback" in out.text


def test_diagnose_reports_stale_environment_value(with_url, monkeypatch):
    with_url(GOOD_URL)
    monkeypatch.setattr(config, "_secret", lambda n: GOOD_URL.replace("ap-northeast-1", "eu-west-1")
                        if n == "DATABASE_URL" else None)
    out = Capture()
    db_diagnose.run(emit=out, parse_only=True)
    assert "DIFFERENT values" in out.text and "Remove-Item Env:DATABASE_URL" in out.text


def test_diagnose_warns_about_libpq_environment_variables(with_url):
    with_url(GOOD_URL, {"PGOPTIONS": "-c search_path=x", "PGPASSWORD": "pg-env-password-1"})
    out = Capture()
    db_diagnose.run(emit=out, parse_only=True)
    assert "PGOPTIONS" in out.text and "PGPASSWORD" in out.text and "pg-env-password-1" not in out.text


@pytest.fixture
def mock_network(monkeypatch):
    import socket
    monkeypatch.setattr(db_diagnose, "_resolve", lambda host, port: [(socket.AF_INET, ("52.68.3.1", port)),
                                                                     (socket.AF_INET, ("54.64.190.72", port))])
    monkeypatch.setattr(db_diagnose, "_tcp_and_ssl", lambda f, a, t: ("connected in 10 ms", "server supports SSL"))


def test_full_diagnosis_of_the_eauthquery_failure(with_url, mock_network, monkeypatch):
    with_url(BAD_URL)
    calls = []

    def fake_connect(conninfo, **kwargs):
        calls.append(psycopg.conninfo.conninfo_to_dict(conninfo).get("user", ""))
        user = calls[-1]
        if user.count(".") == 1 and user.endswith(REF) and "PROJECT_REF" not in user:     # the corrected user
            raise psycopg.OperationalError('connection failed: FATAL: password authentication failed for user "x"')
        if user.endswith("." + REF) or user.endswith(".PROJECT_REF"):
            raise psycopg.OperationalError(EAUTHQUERY)
        raise psycopg.OperationalError("connection failed: FATAL: (ENOTFOUND) tenant/user not found")

    monkeypatch.setattr(psycopg, "connect", fake_connect)
    out = Capture()
    assert db_diagnose.run(emit=out, controls=True) == 1
    text = out.text
    assert "exception class : OperationalError" in text and "SQLSTATE" in text
    assert "classified as   : auth" in text and "FATAL: (EAUTHQUERY) user not found in the database" in text
    assert "NOT a local network refusal" in text
    assert "CONFIRMED" in text and "extra text in your DATABASE_URL user name" in text
    assert "Not retrying per address" in text                 # the server replied: no extra failed logins
    assert_no_secrets(text, "PROJECT_REF", "app-pass-123456", "gemini-key-ABCDEF123456", "diagnostic-not-a-real")
    assert len(calls) == 3                                    # 1 real login + 2 fake-password controls


def test_controls_only_skips_the_real_login_and_per_address_retries(with_url, mock_network, monkeypatch):
    with_url(BAD_URL)
    users = []

    def fake_connect(conninfo, **kwargs):
        users.append(psycopg.conninfo.conninfo_to_dict(conninfo)["password"])
        raise psycopg.OperationalError("connection failed: FATAL: (ENOTFOUND) tenant/user not found")

    monkeypatch.setattr(psycopg, "connect", fake_connect)
    out = Capture()
    db_diagnose.run(emit=out, controls_only=True)
    assert len(users) == 2 and all(p == db_diagnose.FAKE_PASSWORD for p in users)   # no real password ever sent
    assert PASSWORD not in users and "skipped (--controls-only)" in out.text


def test_diagnosis_distinguishes_a_local_network_refusal(with_url, monkeypatch):
    import socket
    with_url(GOOD_URL)
    monkeypatch.setattr(db_diagnose, "_resolve", lambda h, p: [(socket.AF_INET, ("52.68.3.1", p))])
    monkeypatch.setattr(db_diagnose, "_tcp_and_ssl", lambda f, a, t: ("FAILED (ConnectionRefusedError: refused)", "not tested"))
    out = Capture()
    assert db_diagnose.run(emit=out) == 1
    assert "NETWORK level" in out.text and "No PostgreSQL login was attempted" in out.text


def test_diagnosis_success_requires_a_real_query(with_url, mock_network, monkeypatch):
    with_url(GOOD_URL)

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql):
            rows = {"SELECT 1": (1,), "SHOW server_version": ("17.4",), "SELECT current_database()": ("postgres",)}

            class Cur:
                def fetchone(self_inner):
                    return rows[sql]
            return Cur()

    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: Conn())
    out = Capture()
    assert db_diagnose.run(emit=out) == 0
    assert "SELECT 1 -> 1" in out.text and "A real PostgreSQL query succeeded" in out.text
    assert_no_secrets(out.text)


def test_describe_chars_shows_hidden_characters_without_the_text():
    desc = db_diagnose.describe_chars("abc\u200bDEF_")
    assert "U+200B" in desc and "ZERO WIDTH SPACE" in desc and "abc" not in desc
    assert db_diagnose.user_segments(BAD_URL) == ["postgres", "PROJECT_REF", REF]
    assert db_diagnose.user_segments("postgresql://host/db") == []


@pytest.mark.parametrize("results,extra,expect", [
    ({"A": ("auth", "FATAL: (ENOTFOUND) tenant/user *** not found"),
      "D": ("auth", 'FATAL: password authentication failed for user "***"')}, True, "CONFIRMED"),
    ({"A": ("auth", "FATAL: (ENOTFOUND) x"), "D": ("auth", "FATAL: (ENOTFOUND) x")}, True, "does not recognise this project"),
    ({"A": ("auth", "FATAL: (ENOTFOUND) x"), "B": ("auth", "FATAL: (ENOTFOUND) x"), "C": ("auth", "FATAL: (ENOTFOUND) x")},
     False, "NOT recognised"),
    ({"A": ("auth", "FATAL: (EAUTHQUERY) y"), "B": ("auth", "FATAL: (EAUTHQUERY) y"), "C": ("auth", "FATAL: (EAUTHQUERY) y")},
     False, "EVERY user"),
])
def test_control_interpretation(results, extra, expect):
    assert expect in " ".join(db_diagnose.interpret_controls(results, extra))


# --------------------------------------------------------------------- db_tools
def test_check_failure_prints_class_sqlstate_and_sanitised_detail_only(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", BAD_URL)
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: (_ for _ in ()).throw(psycopg.OperationalError(EAUTHQUERY)))
    assert db_tools.main(["check"]) == 2
    out = capsys.readouterr().out
    assert "EAUTHQUERY" in out and "exception=OperationalError" in out and "sqlstate=n/a" in out
    assert "kind=auth" in out and "db_tools.py diagnose" in out
    assert_no_secrets(out, "PROJECT_REF")


def test_app_never_falls_back_to_sqlite_when_postgres_is_configured_but_down(monkeypatch, tmp_path):
    import database
    monkeypatch.setenv("DATABASE_URL", GOOD_URL)
    monkeypatch.setenv("NOTICE2ACTION_DB", str(tmp_path / "must-not-be-used.db"))
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: (_ for _ in ()).throw(psycopg.OperationalError(EAUTHQUERY)))
    with pytest.raises(DatabaseConnectionError):
        database.Database()
    assert not (tmp_path / "must-not-be-used.db").exists()          # no silent SQLite database was created
