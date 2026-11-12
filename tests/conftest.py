import io
import json
import os
import sys
from pathlib import Path

import pymupdf
import pytest
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import Database  # noqa: E402

NOTICE_TEXT = (
    "Department of Computer Science. Notice: Semester registration closes on 2026-12-15 at 5 PM. "
    "Students must submit the fee receipt and ID card to the Exam Cell, Room 204."
)


def make_gemini_payload(**overrides):
    payload = {
        "title": "Semester Registration Notice",
        "category": "Registration",
        "summary": "Students must register for the semester before the deadline.",
        "organization": "Example University",
        "department": "Computer Science",
        "announcement_date": "2026-11-01",
        "deadlines": [{
            "description": "Registration closes",
            "date": "2026-12-15",
            "time": "17:00",
            "date_type": "explicit",
            "source_text": "Semester registration closes on 2026-12-15 at 5 PM.",
        }, {
            "description": "Late fee window",
            "date": None,
            "time": None,
            "date_type": "unspecified",
            "source_text": "A late fee applies afterwards.",
        }],
        "eligibility": ["All enrolled students"],
        "required_documents": ["Fee receipt", "ID card"],
        "venue": "Exam Cell, Room 204",
        "contact_information": ["exam@example.edu"],
        "important_instructions": ["Bring originals."],
        "action_items": [{
            "title": "Submit registration documents",
            "description": "Take the fee receipt and ID card to the Exam Cell.",
            "related_deadline": "Registration closes",
            "priority": "high",
            "status": "pending",
        }, {
            "title": "Check late fee rules",
            "description": None,
            "related_deadline": None,
            "priority": "low",
            "status": "pending",
        }],
        "source_language": "English",
        "confidence_notes": [],
    }
    payload.update(overrides)
    return payload


class FakeResponse:
    def __init__(self, text, prompt_feedback=None, candidates=None):
        self.text = text
        self.prompt_feedback = prompt_feedback
        self.candidates = candidates


class FakeModels:
    """Scripted stand-in for client.models.

    Each output is a JSON string, an Exception to raise, a FakeResponse, or a
    callable(model_name) returning one of those. The last output repeats forever.
    """

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.requests = []

    @property
    def calls(self):
        return len(self.requests)

    @property
    def models_used(self):
        return [r["model"] for r in self.requests]

    def generate_content(self, **kwargs):
        self.requests.append(kwargs)
        item = self.outputs.pop(0) if len(self.outputs) > 1 else self.outputs[0]
        if callable(item):
            item = item(kwargs["model"])
        if isinstance(item, Exception):
            raise item
        return item if isinstance(item, FakeResponse) else FakeResponse(item)


class FakeClient:
    """Stand-in for google.genai.Client; no network or paid calls."""

    def __init__(self, *outputs):
        self.models = FakeModels(outputs)


@pytest.fixture(autouse=True)
def hermetic_config(monkeypatch):
    """Tests must never read the developer's real secrets.toml or environment.

    In particular DATABASE_URL is cleared so no test can reach a real (Supabase) database.
    """
    import config
    monkeypatch.setattr(config, "_secret", lambda name: None)
    for var in ("GEMINI_API_KEY", "GEMINI_MODEL", "GEMINI_FALLBACK_MODEL", "DATABASE_URL",
                "DATABASE_AUTO_MIGRATE", "APP_PASSWORD", "NOTICE2ACTION_DB"):
        monkeypatch.delenv(var, raising=False)


# --------------------------------------------------------------------------
# PostgreSQL test server (optional). Resolution order:
#   1. TEST_DATABASE_URL  - a throw-away database you control (local, or a name containing "test")
#   2. pixeltable-pgserver (requirements-dev.txt) - an embedded local PostgreSQL started automatically
#   3. otherwise PostgreSQL tests are skipped
# Tests TRUNCATE tables, so a remote database is only accepted if its name contains "test".
# --------------------------------------------------------------------------
_PG_STATE = {"resolved": False, "url": None, "server": None, "reason": ""}


def _resolve_pg(tmp_root):
    if _PG_STATE["resolved"]:
        return _PG_STATE["url"]
    _PG_STATE["resolved"] = True
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        try:
            import pixeltable_pgserver as pgserver
            server = pgserver.get_server(Path(tmp_root) / "pgdata", cleanup_mode="stop")
            _PG_STATE["server"] = server
            url = server.get_uri()
        except Exception as exc:  # not installed / cannot start
            _PG_STATE["reason"] = f"no PostgreSQL test server ({type(exc).__name__})"
            return None
    from db_backends import describe_url, is_local_host
    target = describe_url(url)
    if not (is_local_host(target["host"]) or "test" in target["database"].lower()):
        pytest.exit("Refusing to run tests against a non-local database whose name does not contain 'test'.", 2)
    _PG_STATE["url"] = url
    return url


@pytest.fixture(scope="session")
def pg_test_url(tmp_path_factory):
    url = _resolve_pg(tmp_path_factory.mktemp("pg"))
    if not url:
        pytest.skip(_PG_STATE["reason"] or "set TEST_DATABASE_URL (or install requirements-dev.txt) for PostgreSQL tests")
    return url


def pytest_sessionfinish(session, exitstatus):
    server = _PG_STATE.get("server")
    if server is not None:
        try:
            server.cleanup()
        except Exception:
            pass


@pytest.fixture
def pg_db(pg_test_url):
    """A Database on the PostgreSQL test server with empty tables."""
    db = Database(url=pg_test_url)
    with db.connect() as s:
        s.execute("TRUNCATE notices, deadlines, action_items RESTART IDENTITY CASCADE")
    yield db
    db.close()


@pytest.fixture(params=["sqlite", "postgres"])
def db(request, tmp_path):
    """The same test runs against every backend."""
    if request.param == "sqlite":
        yield Database(tmp_path / "test.db")
    else:
        yield request.getfixturevalue("pg_db")


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    """Skip retry backoff delays; record them so tests can assert on them."""
    import ai_engine
    delays = []
    monkeypatch.setattr(ai_engine, "_sleep", delays.append)
    return delays


@pytest.fixture
def sleeps(no_real_sleep):
    return no_real_sleep


@pytest.fixture
def fake_client():
    return lambda *outputs: FakeClient(*outputs)


@pytest.fixture
def payload():
    return make_gemini_payload()


@pytest.fixture
def payload_json(payload):
    return json.dumps(payload)


@pytest.fixture
def text_pdf_bytes():
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_textbox(pymupdf.Rect(50, 50, 550, 400), NOTICE_TEXT, fontsize=12)
    data = doc.tobytes()
    doc.close()
    return data


@pytest.fixture
def scanned_pdf_bytes():
    """A PDF whose only content is an image (no extractable text layer)."""
    img = Image.new("RGB", (600, 300), "white")
    ImageDraw.Draw(img).text((20, 20), "scanned notice", fill="black")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_image(page.rect, stream=buf.getvalue())
    data = doc.tobytes()
    doc.close()
    return data


@pytest.fixture
def png_bytes():
    img = Image.new("RGB", (400, 200), "white")
    ImageDraw.Draw(img).text((10, 10), "hello", fill="black")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()
