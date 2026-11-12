"""Central configuration for Notice2Action AI.

All tunables (model name, limits, paths) live here so they are never scattered
across the code base. Secrets are read from Streamlit secrets or environment
variables and are never hardcoded.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional, Tuple

APP_NAME = "Notice2Action AI"
APP_VERSION = "1.0.0"
APP_TAGLINE = "Turn institutional notices into clear, actionable tasks."

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

# --- Gemini -----------------------------------------------------------------
# Change the model without touching code: set GEMINI_MODEL in the environment
# or in .streamlit/secrets.toml.
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash"
# Used automatically when the primary model is missing, overloaded or timing out.
# Set GEMINI_FALLBACK_MODEL to "none" to disable.
DEFAULT_FALLBACK_MODEL = "gemini-3.1-flash-lite"
API_KEY_ENV_VAR = "GEMINI_API_KEY"
MODEL_ENV_VAR = "GEMINI_MODEL"
FALLBACK_MODEL_ENV_VAR = "GEMINI_FALLBACK_MODEL"
API_KEY_PLACEHOLDERS = {"", "YOUR_GEMINI_API_KEY", "YOUR_GEMINI_API_KEY_HERE", "your-api-key", "changeme"}
# NOTE: the SDK also sends this to Google as the server-side deadline, so an
# expired timeout surfaces as HTTP 504 DEADLINE_EXCEEDED.
GEMINI_TIMEOUT_SECONDS = 60
GEMINI_MAX_RETRIES = 2          # extra attempts for 429 / 500 / 502 / 503 only
GEMINI_RETRY_BASE_DELAY = 2.0   # seconds; doubles each retry (+ small jitter)
GEMINI_RETRY_MAX_DELAY = 10.0
MAX_AI_INPUT_CHARS = 60_000  # notices are short; this guards token usage

# --- Uploads ----------------------------------------------------------------
ALLOWED_EXTENSIONS = ("pdf", "jpg", "jpeg", "png")
MAX_FILE_SIZE_MB = 15
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
MAX_PDF_PAGES = 40

# --- Extraction / OCR -------------------------------------------------------
# A PDF page with fewer characters than this is treated as scanned -> OCR.
MIN_CHARS_PER_PAGE = 40
OCR_DPI = 200
MAX_OCR_PAGES = 15
OCR_LANGUAGES_ENV_VAR = "OCR_LANGUAGES"
DEFAULT_OCR_LANGUAGES = "eng"
TESSERACT_CMD_ENV_VAR = "TESSERACT_CMD"
TESSERACT_WINDOWS_PATHS = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
)

# --- Database ---------------------------------------------------------------
# Primary: Supabase PostgreSQL via DATABASE_URL (Streamlit secrets or environment).
# Fallback for local development: SQLite file (used only when DATABASE_URL is not set).
DB_PATH_ENV_VAR = "NOTICE2ACTION_DB"
DATABASE_URL_ENV_VAR = "DATABASE_URL"
DB_AUTO_MIGRATE_ENV_VAR = "DATABASE_AUTO_MIGRATE"
DATABASE_URL_PLACEHOLDERS = ("YOUR_SUPABASE_POSTGRESQL_CONNECTION_STRING", "your-supabase", "changeme")
MIGRATIONS_DIR = BASE_DIR / "supabase" / "migrations"
DB_POOL_MAX_SIZE = 5            # connections kept per Streamlit server process
DB_CONNECT_TIMEOUT_SECONDS = 10
APP_PASSWORD_ENV_VAR = "APP_PASSWORD"


def get_db_path() -> Path:
    override = os.environ.get(DB_PATH_ENV_VAR)
    return Path(override) if override else DATA_DIR / "notice2action.db"


def _is_db_placeholder(value: str) -> bool:
    return any(value.startswith(p) or value == p for p in DATABASE_URL_PLACEHOLDERS)


def get_database_url() -> Optional[str]:
    """PostgreSQL connection string from secrets/env, or None to use the SQLite fallback.

    Sources are tried in order (Streamlit secrets, then the environment) and placeholder values
    are skipped, so a placeholder in secrets.toml never hides a real value in the environment.
    The value is a credential: never log it. Use ``db_backends.redact`` for display.
    """
    return _setting(DATABASE_URL_ENV_VAR, ignore=_is_db_placeholder)


def get_database_url_source() -> Optional[str]:
    """Which source the active DATABASE_URL came from (never the value itself)."""
    for source, value in _source_values(DATABASE_URL_ENV_VAR):
        if not _is_db_placeholder(value):
            return source
    return None


def database_url_candidates() -> List[Tuple[str, str]]:
    """Every non-empty DATABASE_URL found, as (source, value). For diagnostics only: values are secrets."""
    return _source_values(DATABASE_URL_ENV_VAR)


def database_backend() -> str:
    return "postgres" if get_database_url() else "sqlite"


def db_auto_migrate() -> bool:
    return (_setting(DB_AUTO_MIGRATE_ENV_VAR) or "true").lower() not in {"0", "false", "no", "off"}


def get_app_password() -> Optional[str]:
    """Optional shared password gate for the whole app (recommended for public deployments)."""
    return _setting(APP_PASSWORD_ENV_VAR)


def _secret(name: str) -> Optional[str]:
    """Read a value from Streamlit secrets, tolerating a missing secrets file."""
    try:
        import streamlit as st

        value = st.secrets.get(name)
    except Exception:  # no secrets file / not running under Streamlit
        return None
    return str(value).strip() if value is not None else None


def _source_values(name: str) -> List[Tuple[str, str]]:
    """Non-empty values for ``name`` in priority order: Streamlit secrets, then the environment."""
    found: List[Tuple[str, str]] = []
    from_secrets = _secret(name)
    if from_secrets and from_secrets.strip():
        found.append(("Streamlit secrets", from_secrets.strip()))
    from_env = os.environ.get(name)
    if from_env and from_env.strip():
        found.append(("environment variable", from_env.strip()))
    return found


def _setting(name: str, ignore=None) -> Optional[str]:
    """First configured value for ``name``; values rejected by ``ignore`` (placeholders) are skipped."""
    for _source, value in _source_values(name):
        if ignore is None or not ignore(value):
            return value
    return None


def get_api_key() -> Optional[str]:
    """Return the Gemini API key or None when missing/placeholder."""
    return _setting(API_KEY_ENV_VAR, ignore=lambda v: v in API_KEY_PLACEHOLDERS)


def get_api_key_source() -> Optional[str]:
    for source, value in _source_values(API_KEY_ENV_VAR):
        if value not in API_KEY_PLACEHOLDERS:
            return source
    return None


def _is_placeholder(value: Optional[str]) -> bool:
    return not value or value.upper().startswith("YOUR_")


def get_model_name() -> str:
    value = _setting(MODEL_ENV_VAR)
    return DEFAULT_GEMINI_MODEL if _is_placeholder(value) else value


def get_fallback_model() -> Optional[str]:
    """Fallback model name, or None when disabled / identical to the primary."""
    value = _setting(FALLBACK_MODEL_ENV_VAR)
    value = DEFAULT_FALLBACK_MODEL if _is_placeholder(value) else value
    if value.lower() in {"none", "off", "false", "0"} or value == get_model_name():
        return None
    return value


def get_ocr_languages() -> str:
    return _setting(OCR_LANGUAGES_ENV_VAR) or DEFAULT_OCR_LANGUAGES
