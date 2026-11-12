"""Release-readiness and security audit expressed as tests (repo hygiene, secrets, config, rendering)."""
import fnmatch
import re
import tomllib
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import ui

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".venv", ".git", "__pycache__", ".pytest_cache", "data", ".kiro", "tests", "node_modules"}
TEXT_SUFFIXES = {".py", ".md", ".toml", ".example", ".sql", ".txt", ".ps1", ".json", ".yml", ".yaml", ".cfg"}


def project_files():
    for path in ROOT.rglob("*"):
        rel = path.relative_to(ROOT)
        if not path.is_file() or SKIP_DIRS & set(rel.parts):
            continue
        if rel.as_posix() == ".streamlit/secrets.toml":      # the developer's local, git-ignored secrets
            continue
        if path.suffix in TEXT_SUFFIXES or path.name in {".gitignore"}:
            yield rel, path.read_text(encoding="utf-8", errors="ignore")


# ----------------------------------------------------------------- .gitignore
def load_ignore_rules():
    rules = []
    for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            rules.append(line)
    return rules


def is_ignored(rel: str, rules) -> bool:
    ignored = False
    for rule in rules:
        negate = rule.startswith("!")
        pattern = rule[1:] if negate else rule
        directory = pattern.endswith("/")
        pattern = pattern.rstrip("/").lstrip("/")
        parts = rel.split("/")
        if directory:
            hit = any(fnmatch.fnmatch(p, pattern) for p in parts[:-1]) or fnmatch.fnmatch("/".join(parts[:-1]), pattern)
        else:
            hit = fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(parts[-1], pattern)
        if hit:
            ignored = not negate
    return ignored


@pytest.mark.parametrize("path", [
    ".streamlit/secrets.toml", ".env", ".env.local", "prod.env", "data/notice2action.db", "data/backup.sqlite3",
    "notice2action.db", ".venv/Scripts/python.exe", "tests/__pycache__/x.pyc", "__pycache__/a.pyc",
    ".pytest_cache/v/cache", "uploads/notice.pdf", "tmp/scan.png", "app.log", "supabase/.temp/project-ref",
    "supabase/.env", "server.pem", "service-account.key",
    ".streamlit/n2ai key.txt", ".streamlit/notes.txt", ".streamlit/anything-else.toml", "gemini api key.txt",
    "my_token.txt", "credentials.json", "db password.txt",
])
def test_sensitive_paths_are_git_ignored(path):
    assert is_ignored(path, load_ignore_rules()), f"{path} must be ignored"


@pytest.mark.parametrize("path", [
    ".streamlit/secrets.toml.example", ".streamlit/config.toml", "data/.gitkeep", "requirements.txt",
    "requirements-dev.txt", "packages.txt", "supabase/migrations/20261112000000_initial_schema.sql", "README.md",
    "app.py", "database.py", "tests/test_database.py",
])
def test_project_files_are_not_ignored(path):
    assert not is_ignored(path, load_ignore_rules()), f"{path} must be committed"


# -------------------------------------------------------------------- secrets
def test_no_credentials_in_project_files():
    findings = []
    patterns = {
        "Google API key": re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),
        "JWT / Supabase key": re.compile(r"eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}\."),
        "Supabase secret key": re.compile(r"sb_(secret|publishable)_[A-Za-z0-9_\-]{10,}"),
        "database URL with a real-looking password": re.compile(
            r"postgres(?:ql)?://[^\s:@/]+:(?!\*\*\*|\[YOUR|YOUR_|PASSWORD|password|<|\$|%s|\{)[^\s@\"']{4,}@"),
        "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    }
    for rel, text in project_files():
        for label, regex in patterns.items():
            for match in regex.finditer(text):
                findings.append(f"{rel}: {label}: {match.group(0)[:25]}...")
    assert not findings, "\n".join(findings)


def test_secrets_example_contains_placeholders_only():
    text = (ROOT / ".streamlit" / "secrets.toml.example").read_text(encoding="utf-8")
    values = re.findall(r'^\s*([A-Z_]+)\s*=\s*"([^"]*)"', text, re.MULTILINE)
    keys = {k for k, _ in values}
    assert {"DATABASE_URL", "GEMINI_API_KEY", "GEMINI_MODEL"} <= keys
    for key, value in values:
        if key in {"DATABASE_URL", "GEMINI_API_KEY", "APP_PASSWORD"}:
            assert "YOUR" in value or "PASSWORD" in value.upper(), f"{key} must be a placeholder"


def test_no_service_role_or_supabase_client_in_code():
    """The app uses a server-side PostgreSQL connection only: no Supabase keys, no Supabase client."""
    imports = re.compile(r"^\s*(from|import)\s+supabase\b", re.MULTILINE)
    for rel, text in project_files():
        if rel.suffix == ".py":
            for forbidden in ("service_role", "SUPABASE_KEY", "SUPABASE_SERVICE", "create_client(", "supabase-js"):
                assert forbidden not in text, f"{rel} references {forbidden}"
            assert not imports.search(text), f"{rel} imports the Supabase client"


def test_logging_statements_never_mention_secrets():
    pattern = re.compile(r"logger\.\w+\([^)]*\b(password|api_key|DATABASE_URL|conninfo|secret)\b", re.IGNORECASE)
    offenders = [str(rel) for rel, text in project_files() if rel.suffix == ".py" and pattern.search(text)]
    assert not offenders, offenders


# --------------------------------------------------------------- dependencies
def test_runtime_requirements_are_exactly_pinned_and_minimal():
    lines = [l.strip() for l in (ROOT / "requirements.txt").read_text().splitlines() if l.strip() and not l.startswith("#")]
    assert lines and all("==" in l for l in lines), lines
    names = {re.split(r"[=\[<>]", l)[0].lower() for l in lines}
    assert {"streamlit", "google-genai", "pymupdf", "pytesseract", "pillow", "pandas", "psycopg", "psycopg-pool"} <= names
    assert "pytest" not in names and "pixeltable-pgserver" not in names       # test tools stay out of production
    assert any(l.startswith("psycopg[binary]==") for l in lines)


def test_dev_requirements_extend_runtime():
    text = (ROOT / "requirements-dev.txt").read_text()
    assert "-r requirements.txt" in text and "pytest==" in text


def test_streamlit_cloud_system_packages_declared():
    assert "tesseract-ocr" in (ROOT / "packages.txt").read_text()


# ----------------------------------------------------------- streamlit config
def test_streamlit_server_security_settings():
    cfg = tomllib.loads((ROOT / ".streamlit" / "config.toml").read_text())
    assert cfg.get("server", {}).get("enableXsrfProtection", True) is True
    assert cfg.get("server", {}).get("enableCORS", True) is True
    assert cfg.get("server", {}).get("maxUploadSize", 200) <= 25
    assert cfg.get("client", {}).get("showErrorDetails", "full") in {"type", "none"}   # no stack traces to visitors
    assert cfg.get("browser", {}).get("gatherUsageStats") is False


# ------------------------------------------------------------ SQL migrations
def test_migrations_lock_down_the_public_api():
    sql = "\n".join(p.read_text(encoding="utf-8") for p in sorted((ROOT / "supabase" / "migrations").glob("*.sql")))
    for table in ("notices", "deadlines", "action_items"):
        assert re.search(rf"ALTER TABLE public\.{table}\s+ENABLE ROW LEVEL SECURITY", sql), table
    assert re.search(r"REVOKE ALL ON .*FROM anon", sql) and re.search(r"REVOKE ALL ON .*FROM authenticated", sql)
    assert not re.search(r"GRANT\s+.*\s+TO\s+(anon|authenticated|public)", sql, re.IGNORECASE)
    assert "CREATE POLICY" not in sql          # deny-by-default: no API access policies at all


def test_sql_is_parameterised_not_interpolated():
    """f-strings in SQL may only interpolate fixed identifiers/fragments, never values."""
    for name in ("database.py", "db_migrate.py", "db_backends.py", "db_tools.py"):
        text = (ROOT / name).read_text(encoding="utf-8")
        for match in re.finditer(r'f"""?[^"]*\b(SELECT|INSERT|UPDATE|DELETE)\b[^"]*\{([^}]+)\}', text):
            expr = match.group(2)
            assert re.fullmatch(r"(b|self\.backend|pg_db\.backend)\.json_param|assignments|table|t|like|rel|"
                                r"self\.backend\.like_op|self\.backend\.json_param", expr.strip()), \
                f"{name}: suspicious interpolation {{{expr}}} in SQL"


# ----------------------------------------------------- rendering untrusted text
INJECTION = "![x](https://evil.example/pixel.png?q=1) [click](https://evil.example) # Big <script>alert(1)</script> $$x$$"


def test_md_escape_neutralises_markdown_html_and_math():
    escaped = ui.md_escape(INJECTION)
    # every Markdown-significant character must be preceded by a backslash
    assert not re.search(r"(?<!\\)[\[\]()!<>$#*_`|~&]", escaped), escaped
    assert ui.md_escape(None) == "" and ui.md_escape("line1\nline2") == "line1 line2"
    assert ui.md_escape("plain text, 12 Dec 2026") == "plain text, 12 Dec 2026"       # ordinary text untouched


def test_safe_html_entity_encodes_everything_dangerous():
    out = ui.safe_html(INJECTION + "\nsecond line")
    assert "&lt;script&gt;" in out and "<br>second line" in out
    remaining = re.sub(r"&#?\w+;", "", out.replace("<br>", ""))          # drop the entities themselves
    for raw in ("![", "](", "<", ">", "$", "#", "*", "_", "`", "[", "]", "(", ")", '"', "'", "&"):
        assert raw not in remaining, f"{raw!r} must be entity-encoded"
    assert ui.safe_html("Ünïcode ✓ اردو") == "Ünïcode ✓ اردو"                      # non-ASCII text is preserved
    assert ui.safe_html(None) == ""


def test_html_helpers_use_safe_html_for_untrusted_text():
    for html in (ui.fact("Venue", INJECTION), ui.clamp(INJECTION), ui.title_html(INJECTION),
                 ui.badge_html(INJECTION)):
        assert "<script" not in html and "![" not in html and "](" not in html


def test_analysis_with_malicious_text_renders_inert(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTICE2ACTION_DB", str(tmp_path / "x.db"))
    body = (
        "import sys; sys.path.insert(0, r'.')\nimport pages, ai_engine\n"
        f"bad = {INJECTION!r}\n"
        "a = ai_engine.validate_analysis({'title': bad, 'summary': bad, 'eligibility': [bad], 'venue': bad,\n"
        "    'required_documents': [bad], 'important_instructions': [bad], 'contact_information': [bad],\n"
        "    'confidence_notes': [bad], 'organization': bad,\n"
        "    'deadlines': [{'description': bad, 'date': '2026-12-01', 'date_type': 'explicit', 'source_text': bad}],\n"
        "    'action_items': [{'title': bad, 'description': bad}]})\n"
        "pages.render_analysis(a)\n"
    )
    at = AppTest.from_string(body, default_timeout=30).run()
    assert not at.exception, [e.value for e in at.exception]
    joined = " ".join(m.value for m in at.markdown)
    assert "![" not in joined and "](https" not in joined, "markdown image/link syntax must be escaped"
    assert not re.search(r"(?<!\\)<\s*script", joined), "unescaped HTML must never reach the page"
    assert "&lt;script&gt;" in joined or "\\<script" in joined      # shown as text instead
