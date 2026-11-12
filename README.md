# Notice2Action AI

**Smart Notice & Deadline Extractor.** Upload a university notice (PDF or image) and get a plain-language summary, verified deadlines, requirements, and a ready-to-track task list, stored permanently in your own Supabase PostgreSQL database.

## Problem statement

University notices are long, inconsistently formatted, often scanned, and bury the one date that matters. Students miss registration windows, fee deadlines, and document requirements because extracting "what do I actually need to do, and by when?" is tedious and error-prone.

Notice2Action AI turns a notice into structured information and actionable tasks, while being strict about not inventing facts, especially deadlines.

## Key features

- **Upload PDF, JPG, JPEG, PNG** with extension, size, and file-signature validation.
- **Text extraction**: PyMuPDF for digital PDFs, automatic per-page **OCR fallback** for scanned PDFs, OCR for images.
- **Human-in-the-loop**: review and correct extracted text before it is sent to Gemini.
- **Structured AI analysis**: summary, category, organization, department, venue, eligibility, required documents, contacts, instructions, deadlines, and action items.
- **Deadline honesty**: every deadline is labelled *Confirmed* (explicit in the notice), *Inferred - verify*, or *Date not specified*, with its supporting source text. Ambiguities and conflicts are listed under "Please double-check".
- **Task tracking**: pending / completed / overdue, priority and deadline filters, manual editing.
- **Notice library**: search, category and date filters, full analysis view, delete with confirmation, duplicate detection (no repeated AI calls for the same notice).
- **Permanent cloud storage**: Supabase PostgreSQL (primary) with an automatic SQLite fallback for local development.
- **Resilient Gemini integration**: classified errors, limited retries with backoff, automatic fallback model.
- **Optional access password** (`APP_PASSWORD`) for public deployments.
- **Accessible, colour-blind-safe UI**: every status has an icon and a text label, not just a colour.

## Technology stack

| Area | Technology |
| --- | --- |
| UI | Streamlit (multipage via `st.navigation`) |
| AI | Google Gemini via the official `google-genai` SDK |
| PDF text | PyMuPDF |
| OCR | Tesseract + `pytesseract`, Pillow pre-processing |
| Database | **Supabase PostgreSQL** via `psycopg` 3 + `psycopg_pool` (primary); SQLite (local fallback) |
| Data | Pandas |
| Tests | pytest (Gemini and OCR mocked; database tests run on both SQLite and PostgreSQL) |

## Architecture

```
Notice2Action AI/
├── app.py                  Entry point: page config, password gate, navigation, outage handler
├── pages.py                Page renderers (Dashboard, Analyze, My Notices, Tasks, Settings)
├── ui.py                   Theme CSS, components, escaping of untrusted text
├── auth.py                 Optional APP_PASSWORD gate
├── config.py               Settings: model, limits, DATABASE_URL lookup, secrets
├── database.py             Database repository: ALL SQL, validation, one interface for both backends
├── db_backends.py          PostgreSQL (pool, SSL, error translation) and SQLite backends
├── db_migrate.py           SQLite -> PostgreSQL data migration (dry-run, validated, idempotent)
├── db_tools.py             CLI: check / init / migrate-sqlite
├── ai_engine.py            Prompt, Gemini calls (retry + fallback), JSON validation
├── gemini_errors.py        Gemini / network error classification
├── document_processor.py   Upload validation + PDF/image text extraction
├── ocr_engine.py           Tesseract discovery, pre-processing, OCR
├── tasks.py                Pure logic: overdue status, labels, filters
├── supabase/migrations/    Version-controlled SQL schema (Supabase CLI compatible)
├── tests/                  pytest suite
├── data/                   Local SQLite file (git-ignored)
├── packages.txt            System packages for Streamlit Cloud (Tesseract)
└── .streamlit/             config.toml + secrets.toml.example
```

**Flow:** `Upload -> validate -> PyMuPDF text (OCR fallback) -> user review -> Gemini JSON -> validate_analysis() -> database -> UI`.

### Database design

```
notices (1) ──< deadlines (n)            ON DELETE CASCADE
notices (1) ──< action_items (n)         ON DELETE CASCADE
deadlines (1) ──< action_items (0..n)    ON DELETE SET NULL
```

- `database.Database` is the only class the app uses. It writes SQL once with `?` placeholders; the backend adapts it (`%s`, `ILIKE`, `JSONB`, date types).
- **PostgreSQL schema** uses `BIGINT IDENTITY` keys, real `DATE` / `TIMESTAMPTZ` / `JSONB` types, `CHECK` constraints, and indexes for the library, category, deadline and overdue queries. See `supabase/migrations/`.
- **Duplicate notices** are prevented by a `UNIQUE(content_hash)` constraint plus `INSERT ... ON CONFLICT DO NOTHING`, so Streamlit reruns or simultaneous clicks cannot create duplicates.
- **Transactions**: a notice with all its deadlines and tasks is saved atomically; a failure rolls everything back.
- **One connection pool per server process** (`st.cache_resource`), not one connection per rerun. `prepare_threshold=None` makes it compatible with Supabase's pooler (PgBouncer/Supavisor).
- **Backend selection**: `DATABASE_URL` set -> PostgreSQL; not set -> local SQLite file `data/notice2action.db`. If `DATABASE_URL` is set but unreachable, the app does **not** silently fall back (that would hide your data); it shows a clear message with a Retry button.

### Security model

- The app connects **server-side** with a standard PostgreSQL connection string. It does not use the Supabase client, Supabase Auth, or the `anon` / `service_role` keys. **Never put a `service_role` key in this project.**
- The migration enables **Row Level Security with no policies** and revokes grants from `anon` / `authenticated`, so the tables cannot be read or written through Supabase's public REST API even though the `anon` key is public. The app connects as the database owner, which bypasses RLS.
- All SQL is parameterised. Credentials and connection strings are never logged or shown; database errors are translated into actionable messages that contain no secrets.
- AI output is untrusted: it is validated as JSON, never executed, and escaped before display (no injected links, images or HTML).
- Uploaded files are processed in memory and are not written to disk.

## Installation (Windows PowerShell)

```powershell
cd "C:\Users\Ali\Desktop\Notice2Action AI"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

If script execution is blocked, run `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` first. Always run the app with the project's virtual environment (`.\run.ps1` does this for you).

## Configuration

Copy the example file and fill in **your own** values (the real file is git-ignored):

```powershell
Copy-Item .streamlit\secrets.toml.example .streamlit\secrets.toml
notepad .streamlit\secrets.toml
```

```toml
DATABASE_URL   = "YOUR_SUPABASE_POSTGRESQL_CONNECTION_STRING"
GEMINI_API_KEY = "YOUR_GEMINI_API_KEY"
GEMINI_MODEL   = "YOUR_CONFIGURED_GEMINI_MODEL"   # e.g. gemini-3.5-flash
# APP_PASSWORD = "choose-a-long-passphrase"       # recommended for any public deployment
```

Every value can alternatively be an environment variable (`$env:DATABASE_URL = "..."` for the current PowerShell session). Placeholder values are ignored, so an untouched example file safely falls back to SQLite.

| Setting | Purpose |
| --- | --- |
| `DATABASE_URL` | Supabase PostgreSQL connection string. Not set -> local SQLite fallback |
| `GEMINI_API_KEY` | Google Gemini key |
| `GEMINI_MODEL` / `GEMINI_FALLBACK_MODEL` | Primary / fallback model (`none` disables fallback) |
| `APP_PASSWORD` | Require a password before anything is shown |
| `DATABASE_AUTO_MIGRATE` | `true` (default): apply `supabase/migrations` automatically on start |
| `OCR_LANGUAGES`, `TESSERACT_CMD` | OCR options |

## Supabase setup (one-time)

1. **Create a project** at [supabase.com](https://supabase.com) (New project). Choose a region near you and set a **strong database password**. Save it in a password manager; you cannot read it again later (you can reset it).
2. **Copy the connection string.** In the project, click **Connect** (or *Project Settings > Database > Connection string*) and choose the **Session pooler** string (port `5432`). It looks like:
   `postgresql://postgres.<project-ref>:[YOUR-PASSWORD]@aws-0-<region>.pooler.supabase.com:5432/postgres`
   - Use a **pooler** string, not the "Direct connection" (`db.<ref>.supabase.co`): direct hosts are IPv6-only unless you buy the IPv4 add-on, and Streamlit Cloud is IPv4. The Transaction pooler (port `6543`) also works; this app is compatible with it.
   - Replace `[YOUR-PASSWORD]` with the real password **without the brackets**. If it contains special characters, URL-encode them: `@` -> `%40`, `#` -> `%23`, `/` -> `%2F`, `:` -> `%3A`, `?` -> `%3F`. (Or choose a password with only letters and digits.)
   - **Copy the host and the user from the dialog; never build them by hand.** The pooler host has a cluster number (`aws-0-…`, `aws-1-…`) that cannot be derived from the region, and the user is exactly `postgres.<project-ref>` with the 20-character ref and nothing else. Replace only `[YOUR-PASSWORD]`.
   - Dashboard labels change occasionally; look for "Connection string" under the Database settings.
3. **Put it in `.streamlit/secrets.toml`** as `DATABASE_URL` (see Configuration).
4. **Create the tables** (pick one):
   - **Automatic (default):** just start the app. It applies any missing migrations.
   - **CLI:** `python db_tools.py init`
   - **Manual in Supabase:** open *SQL Editor > New query*, paste the contents of `supabase/migrations/20261112000000_initial_schema.sql`, click **Run**. It is safe to run twice.
   - **Supabase CLI:** `supabase link --project-ref <ref>` then `supabase db push`.
5. **Verify the connection:**
   ```powershell
   .\.venv\Scripts\python db_tools.py check
   ```
   Expected: `Connected: PostgreSQL ...` and `Tables: notices=0, deadlines=0, action_items=0`. In the app, *Settings > Database > Test database connection* does the same.

## Migrating existing SQLite data

Your current data lives in `data/notice2action.db`. The migration tool copies it into PostgreSQL **without ever modifying or deleting the SQLite file** (it is opened read-only).

```powershell
# 1. DRY RUN (default): shows what would be copied and detects conflicts. Writes nothing.
.\.venv\Scripts\python db_tools.py migrate-sqlite

# 2. APPLY: copies the data in ONE transaction, validated before commit.
.\.venv\Scripts\python db_tools.py migrate-sqlite --apply

# Use another SQLite file:
.\.venv\Scripts\python db_tools.py migrate-sqlite --sqlite "D:\backup\notice2action.db" --apply
```

- **IDs and relationships are preserved**, and the PostgreSQL identity counters are moved past the migrated IDs so new records never collide.
- **Safe to repeat**: rows already present are skipped, never duplicated. A re-run reports `to insert 0`.
- **Validation** before commit: every source row exists in the target, notice/deadline/task links match, and there are no orphans. Any problem rolls the whole migration back.
- Conflicts (the same id with different content, or the same document under another id) abort with an explanation; nothing is written.
- Keep the SQLite file as a backup until you have checked the data in the app.

## Run the app

```powershell
.\run.ps1
```

or

```powershell
.\.venv\Scripts\streamlit run app.py
```

The sidebar shows the active database: **PostgreSQL / Supabase PostgreSQL** (permanent) or **Local SQLite** (development fallback).

## Deploying to Streamlit Community Cloud

1. Push the repository to GitHub (you do this yourself; make sure `.streamlit/secrets.toml` and `data/*.db` are **not** committed. `.gitignore` already excludes them).
2. On [share.streamlit.io](https://share.streamlit.io) create the app from `app.py`. Under **Advanced settings** choose **Python 3.12 or newer**.
3. Streamlit Cloud installs `requirements.txt` automatically and installs `packages.txt` (Tesseract OCR) with apt.
4. Open **App settings > Secrets** and paste:
   ```toml
   DATABASE_URL   = "postgresql://postgres.<ref>:<url-encoded-password>@aws-0-<region>.pooler.supabase.com:5432/postgres"
   GEMINI_API_KEY = "<your key>"
   GEMINI_MODEL   = "gemini-3.5-flash"
   APP_PASSWORD   = "<a long passphrase>"
   ```
5. **Set `APP_PASSWORD` (or restrict viewers in Streamlit's sharing settings).** Without it, anyone with the URL can read, add and delete notices in your database and use your Gemini quota. There are no per-user accounts: all visitors share one notice library.
6. Changing secrets restarts the app. Use **Reboot app** if it still shows an old connection.
7. The free Supabase tier **pauses projects after about a week of inactivity**; restore the project from the Supabase dashboard if the app reports it cannot connect.

## Gemini API setup

1. Create a key in [Google AI Studio](https://aistudio.google.com/apikey) and put it in `GEMINI_API_KEY` (never commit it).
2. Choose a model with `GEMINI_MODEL` (default `gemini-3.5-flash`). If it is missing, overloaded or timing out, the app automatically tries `GEMINI_FALLBACK_MODEL` (default `gemini-3.1-flash-lite`) and tells you when it did. Model availability differs per account; use **Settings > Test Gemini connection**.

| Error | Meaning | What to do |
| --- | --- | --- |
| 400 `INVALID_ARGUMENT` | Invalid request (or "API key not valid") | Check the key and model name |
| 401 / 403 | Key rejected or no permission | Fix `GEMINI_API_KEY`, enable the Gemini API, restart |
| 404 | Model not found for your account | Set `GEMINI_MODEL` to an available model |
| 429 | Rate limit or quota | Wait, or check quota and billing (retried automatically, up to 2 times) |
| 500 / 502 / 503 | Google-side error, e.g. "high demand" | Retried automatically, then the fallback model is used |
| 504 / timeout | No answer within 60 s | Use a faster model; the fallback is tried |

## OCR installation (Windows)

OCR is needed for scanned PDFs and images. Text-based PDFs work without it.

1. Install Tesseract using the UB Mannheim Windows installer (https://github.com/UB-Mannheim/tesseract/wiki), or:
   ```powershell
   winget install --id UB-Mannheim.TesseractOCR
   ```
2. Keep the default path `C:\Program Files\Tesseract-OCR`. The app finds it automatically, or add it to `PATH`, or set `$env:TESSERACT_CMD = "C:\Program Files\Tesseract-OCR\tesseract.exe"`.
3. For other languages select the language data during installation and set `$env:OCR_LANGUAGES = "eng+urd"`.
4. Open **Settings** to confirm "Tesseract ... is ready". (On Streamlit Cloud, `packages.txt` installs it for you.)

## Database troubleshooting

The Settings page and the error panel show a specific message; the terminal log has the diagnostics (never credentials).

| Symptom / message | Cause | Fix |
| --- | --- | --- |
| "Using the local SQLite fallback" | `DATABASE_URL` missing or still the placeholder | Add the real connection string and restart |
| "DATABASE_URL still contains a placeholder" | `[YOUR-PASSWORD]` not replaced | Insert your password, no brackets |
| "must start with postgresql://" | Wrong value pasted (e.g. the project URL `https://...`) | Copy the *Connection string* from Database settings |
| "contains an unencoded '@'" | Special characters in the password | URL-encode them (`@` -> `%40`) or reset to a simpler password |
| `FATAL: (EAUTHQUERY) user not found in the database` | The pooler found your project but no login role for the **user name**. Classic cause: extra text in the user name, e.g. a leftover template word (`postgres.PROJECT_REF.<ref>`) | The user must be exactly `postgres.<20-character ref>`. Remove everything else (and the extra dot). Run `python db_tools.py diagnose --parse-only` to see the user's structure |
| `FATAL: (ENOTFOUND) tenant/user … not found` / "Tenant or user not found" | The pooler cannot match host + user to a project: wrong project ref, or wrong pooler host (`aws-0` vs `aws-1`, other region) | Copy host **and** user together from *Connect*; never type the host from the region |
| `FATAL: password authentication failed` | The project and user are right, the password is not | Reset the database password in Supabase; URL-encode special characters |
| `(ECIRCUITBREAKER)` / "Circuit breaker open" | Too many failed logins from your address | Wait about 10 minutes, fix the credentials, retry once |
| `(EDBHANDLEREXITED)` / pooler cannot reach the database | The project is paused or restarting | Restore the project in the Supabase dashboard |
| "refused … before any login happened" | A real network-level refusal (firewall, VPN, wrong port) | Check the port, VPN/antivirus, and run `Test-NetConnection <host> -Port <port>` |
| "host could not be found" | Direct host `db.<ref>.supabase.co` is IPv6-only | Use the Session/Transaction **pooler** string |
| "did not respond in time" | Network/firewall, wrong port, or paused project | Check the port (5432/6543), restore a paused project |
| "no free connections" | Pooler/plan connection limit | Wait and retry; lower `DB_POOL_MAX_SIZE` in `config.py` (default 5) |
| "Tables not found" (`db_tools.py check`) | Migration not applied | `python db_tools.py init` or run the SQL in the SQL Editor |
| App still uses old settings on Streamlit Cloud | Secrets cached | Save secrets, then **Reboot app** |
| Data looks missing after redeploy | App used the SQLite fallback (temporary disk) | Set `DATABASE_URL`; run the migration for old data |

Quick checks:

```powershell
.\.venv\Scripts\python db_tools.py check     # connection, server version, row counts
.\.venv\Scripts\python db_tools.py init      # (re)apply migrations
```

Step-by-step connection diagnosis (read-only; never creates tables or runs migrations; the password, user name, project ref, `APP_PASSWORD` and the Gemini key are always masked):

```powershell
.\.venv\Scripts\python db_tools.py diagnose --parse-only   # analyse DATABASE_URL only, no network at all
.\.venv\Scripts\python db_tools.py diagnose                # + DNS, TCP, SSL handshake per IP, one real SELECT 1
.\.venv\Scripts\python db_tools.py diagnose --controls     # + 2-3 FAKE-password probes: does the pooler know your project?
.\.venv\Scripts\python db_tools.py diagnose --controls-only --port 6543   # fewest attempts; other pooler port
```

It reports where `DATABASE_URL` came from (secrets file or a stale environment variable), how it parses (user parts, host, port, SSL), whether TCP and the PostgreSQL protocol are reachable, and the exact exception class, SQLSTATE and sanitized server message. Every failed login counts toward the pooler's circuit breaker, so run it sparingly.

A `kind=refused` message used to appear for **any** psycopg connection failure. It now means a genuine network-level refusal only; server-side errors are reported with the server's own text.

Backups: Supabase offers daily backups on paid plans. On the free tier export regularly (for example `pg_dump` with the same connection string, or Supabase's dashboard export).

## Testing

```powershell
pip install -r requirements-dev.txt
.\.venv\Scripts\python -m pytest -q
```

- Gemini and OCR are mocked: no API key, network access, or Tesseract needed.
- Database tests run on **SQLite and PostgreSQL**. For PostgreSQL either install `requirements-dev.txt` (starts a throw-away local PostgreSQL automatically) or point `TEST_DATABASE_URL` at an **empty test database**. Without either, the PostgreSQL tests are skipped.
- Safety: tests use `TEST_DATABASE_URL` only (never `DATABASE_URL`), clear their tables, and refuse any remote database whose name does not contain "test". **Never point `TEST_DATABASE_URL` at your Supabase production database.**

```powershell
$env:TEST_DATABASE_URL = "postgresql://postgres:password@localhost:5432/n2a_test"
.\.venv\Scripts\python -m pytest -q
```

## Screenshots

> Placeholders. Add images under `docs/screenshots/` and update the paths.

| Page | Screenshot |
| --- | --- |
| Dashboard | `![Dashboard](docs/screenshots/dashboard.png)` |
| Analyze Notice | `![Analyze](docs/screenshots/analyze.png)` |
| Analysis results | `![Results](docs/screenshots/results.png)` |
| My Notices | `![Notices](docs/screenshots/notices.png)` |
| Tasks & Deadlines | `![Tasks](docs/screenshots/tasks.png)` |

## Limitations and future improvements

**Limitations**
- AI output can be wrong and OCR quality depends on the scan. Always verify dates against the original notice.
- One shared notice library: there are no per-user accounts. `APP_PASSWORD` is a single shared password, not user authentication.
- Notice text is stored in your database and sent to the Gemini API; do not upload documents you are not allowed to share with those services.
- Only the first 40 PDF pages (15 OCR pages) are processed; very long text is truncated before analysis.
- Handwriting and low-resolution photos are recognised poorly.
- Timestamps are shown in the server's local time zone (UTC on Streamlit Cloud).

**Future improvements**
- Per-user accounts with row-level security.
- Calendar export (.ics) and email/desktop reminders.
- Batch upload and multi-notice comparison.
- Gemini multimodal input for difficult scans.
- Full-text search index for very large libraries.
