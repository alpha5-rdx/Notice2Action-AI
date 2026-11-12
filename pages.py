"""Streamlit page renderers for Notice2Action AI."""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Dict, Optional

import pandas as pd
import streamlit as st

import ai_engine
import auth
import config
import document_processor
import ocr_engine
import tasks as task_logic
import ui
from database import Database, DatabaseError

logger = logging.getLogger(__name__)

METHOD_LABELS = {
    "pdf-text": "Text extracted directly from the PDF",
    "pdf-ocr": "Scanned PDF read with OCR",
    "pdf-mixed": "PDF text combined with OCR for scanned pages",
    "image-ocr": "Image read with OCR",
}

# Fixed panel heights keep sibling boxes equal and let long content scroll
# inside the box instead of stretching the page.
DASHBOARD_PANEL_H = 560
LIST_PANEL_H = 640
SECTION_BOX_H = 230
SETTINGS_BOX_H = 340


@st.cache_resource(show_spinner="Connecting to the database...")
def _open_db() -> Database:
    """One Database (and one connection pool) per server process, not per Streamlit rerun."""
    return Database()


def get_db() -> Database:
    """Return the shared Database, or show a friendly notice and stop the page if it is unreachable."""
    try:
        return _open_db()
    except DatabaseError as exc:
        ui.database_error(exc)
        st.stop()


def go(page_key: str) -> None:
    st.switch_page(ui.PAGES[page_key])


def fmt_date(value: Optional[str]) -> str:
    if not value:
        return "Not specified"
    try:
        return date.fromisoformat(value).strftime("%d %b %Y")
    except ValueError:
        return value


def fmt_timestamp(value: str) -> str:
    return value.replace("T", " ")[:16]


def table_height(rows: int, max_height: int = 340) -> int:
    """Height for st.dataframe: fits small tables, scrolls (both axes) when large."""
    return min(38 * (rows + 1) + 6, max_height)


# =========================================================================
# Shared: analysis renderer
# =========================================================================
def _list_box(title: str, items) -> None:
    """Equal-size, scrollable box for a bullet list."""
    with ui.scroll_panel(SECTION_BOX_H):
        st.markdown(f'<div class="n2a-panel-title">{title}</div>', unsafe_allow_html=True)
        if not items:
            st.caption("Nothing mentioned in the notice.")
        for item in items or []:
            st.markdown(f"- {ui.md_escape(item)}")


def render_analysis(analysis: Dict[str, Any]) -> None:
    """Render a validated analysis dict as cards, sections and tables."""
    st.markdown(f"## {ui.md_escape(analysis['title'])}")
    ui.badges((analysis.get("category") or "Other", "indigo"),
              (f"Language: {analysis['source_language']}" if analysis.get("source_language") else "Language unknown", "gray"))

    if analysis.get("summary"):
        st.markdown("#### Summary")
        st.markdown(f'<div class="n2a-summary">{_esc(analysis["summary"])}</div>', unsafe_allow_html=True)

    if analysis.get("confidence_notes"):
        with ui.scroll_panel(150):
            st.markdown('<div class="n2a-panel-title">Please double-check</div>', unsafe_allow_html=True)
            for note in analysis["confidence_notes"]:
                st.markdown(f"- {ui.md_escape(note)}")

    st.markdown("#### Key details")
    cols = st.columns(4)
    facts = [("Organization", analysis.get("organization")), ("Department", analysis.get("department")),
             ("Announced", fmt_date(analysis.get("announcement_date")) if analysis.get("announcement_date") else None),
             ("Venue", analysis.get("venue"))]
    for col, (label, value) in zip(cols, facts):
        col.markdown(ui.fact(label, value), unsafe_allow_html=True)

    st.markdown("#### Deadlines")
    deadlines = analysis.get("deadlines") or []
    if deadlines:
        st.dataframe(pd.DataFrame([{
            "Deadline": d["description"],
            "Date": fmt_date(d.get("date")),
            "Time": d.get("time") or "-",
            "Status": task_logic.deadline_label(d.get("date_type"), bool(d.get("date"))),
            "Supporting text": d.get("source_text") or "-",
        } for d in deadlines]), hide_index=True, width="stretch", height=table_height(len(deadlines)))
        if any(d.get("date_type") == "inferred" for d in deadlines):
            st.caption("Dates marked 'Inferred - verify' were calculated, not announced. Confirm them with the issuer.")
    else:
        st.info("No deadlines were found in this notice.")

    st.markdown("#### Action items")
    actions = analysis.get("action_items") or []
    if actions:
        st.dataframe(pd.DataFrame([{
            "Task": a["title"],
            "Details": a.get("description") or "-",
            "Related deadline": a.get("related_deadline") or "None",
            "Priority": a["priority"].capitalize(),
            "Status": a["status"].capitalize(),
        } for a in actions]), hide_index=True, width="stretch", height=table_height(len(actions)))
    else:
        st.info("No action items were identified.")

    st.markdown("#### Requirements and contacts")
    left, right = st.columns(2)
    with left:
        _list_box("Eligibility", analysis.get("eligibility"))
        _list_box("Contact information", analysis.get("contact_information"))
    with right:
        _list_box("Required documents", analysis.get("required_documents"))
        _list_box("Important instructions", analysis.get("important_instructions"))


def _esc(text: str) -> str:
    """Untrusted text -> safe for an HTML block (see ui.safe_html)."""
    return ui.safe_html(text)


# =========================================================================
# A. Dashboard
# =========================================================================
def page_dashboard() -> None:
    db = get_db()
    today = date.today()
    ui.hero(f"Welcome to {config.APP_NAME}", config.APP_TAGLINE)

    all_tasks = task_logic.annotate_tasks(db.list_tasks(), today)
    counts = task_logic.task_counts(all_tasks, today)
    total = db.count_notices()

    cols = st.columns(4)
    with cols[0]:
        ui.metric_card("Analyzed notices", total, "Saved in your library", "blue", "▤")
    with cols[1]:
        ui.metric_card("Deadlines ahead", db.count_upcoming_deadlines(today), "From today onward", "warn", "◷")
    with cols[2]:
        ui.metric_card("Overdue tasks", counts["overdue"], "Past their date", "danger", "!")
    with cols[3]:
        ui.metric_card("Completed tasks", counts["completed"], f"{counts['pending']} still pending", "ok", "✓")

    st.write("")
    if st.button("Upload a notice", type="primary", icon=":material/upload_file:"):
        go("analyze")

    if total == 0:
        st.write("")
        ui.empty_state("No notices analyzed yet",
                       "Upload a PDF or image of a notice to get a summary, deadlines and a task list.")
        return

    left, right = st.columns([3, 2], gap="large")
    with left:
        st.markdown("### Recently analyzed")
        with ui.scroll_panel(DASHBOARD_PANEL_H):
            for notice in db.list_notices()[:100]:
                with st.container(border=True):
                    c1, c2 = st.columns([4, 1], vertical_alignment="center")
                    c1.markdown(ui.title_html(notice["title"]), unsafe_allow_html=True)
                    c1.markdown(
                        ui.badge_html(notice["category"], "blue")
                        + f'<span style="color:#586577;font-size:.8rem">analysed {fmt_timestamp(notice["created_at"])}</span>',
                        unsafe_allow_html=True)
                    if c2.button("View", key=f"dash_view_{notice['id']}", icon=":material/visibility:"):
                        st.session_state["selected_notice"] = notice["id"]
                        go("notices")
    with right:
        st.markdown("### Category distribution")
        with ui.scroll_panel(DASHBOARD_PANEL_H):
            distribution = db.category_counts()
            _category_chart(distribution)
            st.dataframe(pd.DataFrame({"Category": list(distribution), "Notices": list(distribution.values())}),
                         hide_index=True, width="stretch", height=table_height(len(distribution), 260))


def _category_chart(distribution: Dict[str, int]) -> None:
    """Horizontal bars in a colour-blind-safe palette, with value labels (not colour-only)."""
    import altair as alt

    df = pd.DataFrame({"Category": list(distribution), "Notices": list(distribution.values())})
    base = alt.Chart(df).encode(
        y=alt.Y("Category:N", sort="-x", title=None),
        x=alt.X("Notices:Q", title=None, axis=alt.Axis(tickMinStep=1)),
    )
    bars = base.mark_bar(cornerRadiusEnd=4).encode(
        color=alt.Color("Category:N", legend=None,
                        scale=alt.Scale(domain=list(distribution), range=ui.CHART_PALETTE)),
        tooltip=["Category", "Notices"],
    )
    labels = base.mark_text(align="left", dx=4, color="#0f1f33").encode(text="Notices:Q")
    st.altair_chart((bars + labels).properties(height=max(150, 34 * len(df))), width="stretch")


# =========================================================================
# B + C. Analyze
# =========================================================================
def _reset_analysis_state() -> None:
    for key in ("extraction", "analysis_result", "review_text"):
        st.session_state.pop(key, None)


def page_analyze() -> None:
    db = get_db()
    ui.page_header("Analyze Notice", "Upload a notice, review the extracted text, then let AI structure it.")

    if config.get_api_key() is None:
        st.warning("Gemini API key is not configured. You can extract text, but analysis needs a key. "
                   "See the Settings page for setup steps.")

    allowed = ", ".join(e.upper() for e in config.ALLOWED_EXTENSIONS)
    uploaded = st.file_uploader(
        f"Drag and drop a notice ({allowed}, up to {config.MAX_FILE_SIZE_MB} MB)",
        type=list(config.ALLOWED_EXTENSIONS), accept_multiple_files=False,
    )

    if uploaded is None:
        if st.session_state.get("upload_sig"):
            _reset_analysis_state()
            st.session_state["upload_sig"] = None
        _show_saved_result(db)
        if "analysis_result" not in st.session_state:
            ui.empty_state("No file selected", "Choose a PDF, JPG or PNG to begin.")
        return

    data = uploaded.getvalue()
    signature = (uploaded.name, len(data))
    if st.session_state.get("upload_sig") != signature:
        _reset_analysis_state()
        st.session_state["upload_sig"] = signature

    try:
        document_processor.validate_upload(uploaded.name, data)
    except document_processor.DocumentError as exc:
        st.error(str(exc))
        return

    st.caption(f"{ui.md_escape(uploaded.name)}  |  {len(data) / 1024:.0f} KB")
    if st.button("Extract text", type="primary", icon=":material/text_snippet:",
                 disabled="extraction" in st.session_state):
        with st.spinner("Extracting text (OCR can take a little longer)..."):
            try:
                result = document_processor.process_document(uploaded.name, data)
            except document_processor.DocumentError as exc:
                st.error(str(exc))
                return
            except Exception as exc:
                logger.exception("Unexpected extraction failure: %s", type(exc).__name__)
                st.error("Something went wrong while reading the file. Please try a different file.")
                return
        st.session_state["extraction"] = {"filename": uploaded.name, "result": result}
        st.session_state["review_text"] = result.text
        st.session_state.pop("analysis_result", None)
        st.rerun()

    extraction = st.session_state.get("extraction")
    if not extraction:
        return

    result = extraction["result"]
    st.success(f"{METHOD_LABELS.get(result.method, result.method)} | "
               f"{result.pages} page(s) | {len(st.session_state.get('review_text', result.text)):,} characters")
    for warning in result.warnings:
        st.warning(warning)

    st.markdown("### Review extracted text")
    st.caption("Fix any OCR mistakes (names, dates, numbers) before analysis. This text is what gets sent to Gemini.")
    text = st.text_area("Extracted text", key="review_text", height=320, label_visibility="collapsed")

    if st.button("Analyze Notice", type="primary", icon=":material/auto_awesome:", disabled=not text.strip()):
        _run_analysis(db, text, extraction["filename"])

    _show_saved_result(db)


def _run_analysis(db: Database, text: str, filename: str) -> None:
    existing = db.find_duplicate(text)
    if existing is not None:
        notice = db.get_notice(existing)
        st.session_state["analysis_result"] = {"analysis": notice["analysis"], "notice_id": existing, "created": False}
        return
    try:
        client = ai_engine.build_client()
        with st.spinner("Gemini is analysing the notice..."):
            analysis = ai_engine.analyze_notice(text, client=client)
    except ai_engine.MissingAPIKeyError as exc:
        st.error(str(exc))
        return
    except ai_engine.AIServiceError as exc:
        st.error(str(exc))
        with st.expander("Technical details"):
            st.json({k: str(v) for k, v in exc.details().items()})
            st.caption("No API key or document text is included here. Full diagnostics are in the terminal log. "
                       "You can also run 'Test Gemini connection' on the Settings page.")
        return
    except ai_engine.AIEngineError as exc:
        st.error(str(exc))
        return
    except Exception as exc:
        logger.exception("Unexpected analysis failure: %s", type(exc).__name__)
        st.error("An unexpected error occurred during analysis. Please try again.")
        return
    try:
        notice_id, created = db.save_notice(analysis, text, filename)
    except DatabaseError as exc:  # message is safe: no credentials or connection strings
        logger.warning("Saving the notice failed: kind=%s", exc.kind)
        st.error(f"The analysis succeeded but could not be saved. {exc}")
        st.session_state["analysis_result"] = {"analysis": analysis, "notice_id": None, "created": False}
        return
    except Exception as exc:
        logger.exception("Failed to save notice: %s", type(exc).__name__)
        st.error("The analysis succeeded but saving it failed. Check the database in Settings.")
        st.session_state["analysis_result"] = {"analysis": analysis, "notice_id": None, "created": False}
        return
    st.session_state["analysis_result"] = {"analysis": analysis, "notice_id": notice_id, "created": created}


def _show_saved_result(db: Database) -> None:
    saved = st.session_state.get("analysis_result")
    if not saved:
        return
    st.divider()
    if saved["notice_id"] is None:
        st.warning("This analysis was not saved.")
    elif saved["created"]:
        st.success(f"Analysis complete and saved to My Notices (ID {saved['notice_id']}).")
    else:
        st.info(f"This notice was already analyzed (ID {saved['notice_id']}). Showing the saved analysis; "
                "no duplicate was created and no AI call was made.")
    render_analysis(saved["analysis"])
    if saved["notice_id"] is not None:
        c1, c2 = st.columns(2)
        if c1.button("View in My Notices", icon=":material/folder_open:", width="stretch"):
            st.session_state["selected_notice"] = saved["notice_id"]
            go("notices")
        if c2.button("Go to Tasks & Deadlines", icon=":material/event_available:", width="stretch"):
            go("tasks")


# =========================================================================
# D. My Notices
# =========================================================================
def _delete_notice(notice_id: int) -> bool:
    """Delete a notice and queue the confirmation message."""
    deleted = get_db().delete_notice(notice_id)
    if st.session_state.get("selected_notice") == notice_id:
        st.session_state.pop("selected_notice", None)
    if deleted:
        ui.flash("Notice deleted successfully", "success")
    else:
        ui.flash("That notice no longer exists", "warning")
    return deleted


@st.dialog("Delete this notice?")
def _confirm_delete(notice_id: int, title: str) -> None:
    st.write(f"**{ui.md_escape(title)}**")
    st.write("This permanently removes the notice together with its deadlines and tasks. This cannot be undone.")
    c1, c2 = st.columns(2)
    if c1.button("Delete", type="primary", icon=":material/delete:", width="stretch"):
        _delete_notice(notice_id)
        st.rerun()
    if c2.button("Cancel", icon=":material/close:", width="stretch"):
        st.rerun()


def page_notices() -> None:
    db = get_db()
    ui.page_header("My Notices", "Your saved notice library.")
    ui.render_flash()

    selected = st.session_state.get("selected_notice")
    if selected is not None:
        notice = db.get_notice(selected)
        if notice is None:
            st.session_state.pop("selected_notice", None)
            st.warning("That notice no longer exists.")
        else:
            c1, c2, c3 = st.columns([1.2, 1.2, 4])
            if c1.button("Back to list", icon=":material/arrow_back:", width="stretch"):
                st.session_state.pop("selected_notice", None)
                st.rerun()
            if c2.button("Delete", key="detail_delete", icon=":material/delete:", width="stretch"):
                _confirm_delete(notice["id"], notice["title"])
            st.caption(f"Analyzed {fmt_timestamp(notice['created_at'])}"
                       + (f"  |  File: {ui.md_escape(notice['file_name'])}" if notice["file_name"] else ""))
            render_analysis(notice["analysis"])
            with st.expander("Extracted source text"):
                with ui.scroll_panel(300):
                    st.text(notice["extracted_text"] or "")
            return

    categories = ["All"] + db.list_categories()
    f1, f2, f3 = st.columns([3, 2, 3])
    search = f1.text_input("Search", placeholder="Title, summary or organization")
    category = f2.selectbox("Category", categories)
    date_range = f3.date_input("Analyzed between", value=(), help="Optional date range")
    date_from = date_to = None
    if isinstance(date_range, (tuple, list)) and len(date_range) == 2:
        date_from, date_to = date_range

    notices = db.list_notices(search, category, date_from, date_to)
    if not notices:
        if db.count_notices() == 0:
            ui.empty_state("Your library is empty", "Analyzed notices will appear here.")
        else:
            ui.empty_state("No matching notices", "Try a different search or clear the filters.")
        return

    st.caption(f"{len(notices)} notice(s)")
    with ui.scroll_panel(LIST_PANEL_H):
        for n in notices:
            with st.container(border=True):
                c1, c2, c3 = st.columns([6, 1.3, 1.3], vertical_alignment="center")
                c1.markdown(ui.title_html(n["title"]), unsafe_allow_html=True)
                c1.markdown(
                    f'<div class="n2a-meta">{ui.badge_html(n["category"], "blue")}'
                    f'{_esc(n["organization"] or "Organization not specified")}  |  '
                    f'analysed {fmt_timestamp(n["created_at"])}</div>', unsafe_allow_html=True)
                c1.markdown(ui.clamp(n["summary"], 2, muted=True), unsafe_allow_html=True)
                if c2.button("View", key=f"view_{n['id']}", icon=":material/visibility:", width="stretch"):
                    st.session_state["selected_notice"] = n["id"]
                    st.rerun()
                if c3.button("Delete", key=f"del_{n['id']}", icon=":material/delete:", width="stretch"):
                    _confirm_delete(n["id"], n["title"])


# =========================================================================
# E. Tasks & Deadlines
# =========================================================================
def page_tasks() -> None:
    db = get_db()
    today = date.today()
    ui.page_header("Tasks & Deadlines", "Track what you need to do and when.")
    ui.render_flash()

    tasks_all = task_logic.annotate_tasks(db.list_tasks(), today)
    counts = task_logic.task_counts(tasks_all, today)
    cols = st.columns(3)
    with cols[0]:
        ui.metric_card("Pending", counts["pending"], "Still to do", "blue", "○")
    with cols[1]:
        ui.metric_card("Overdue", counts["overdue"], "Past their date", "danger", "!")
    with cols[2]:
        ui.metric_card("Completed", counts["completed"], "Done", "ok", "✓")

    st.markdown("### Upcoming deadlines")
    horizon = today + timedelta(days=60)
    upcoming = [d for d in db.list_deadlines()
                if d["deadline_date"] and today <= date.fromisoformat(d["deadline_date"]) <= horizon]
    if upcoming:
        st.dataframe(pd.DataFrame([{
            "Date": fmt_date(d["deadline_date"]),
            "When": task_logic.describe_due(d["deadline_date"], today),
            "Deadline": d["description"],
            "Time": d["deadline_time"] or "-",
            "Status": task_logic.deadline_label(d["date_type"]),
            "Notice": d["notice_title"],
        } for d in upcoming]), hide_index=True, width="stretch", height=table_height(len(upcoming), 300))
        st.caption("Showing dated deadlines in the next 60 days. Only 'Confirmed' dates were stated in the notice.")
    else:
        st.info("No dated deadlines in the next 60 days.")

    st.markdown("### Tasks")
    f1, f2, f3 = st.columns(3)
    status = f1.selectbox("Status", ["All", "Pending", "Overdue", "Completed"])
    priority = f2.selectbox("Priority", ["All", "High", "Medium", "Low"])
    kind = f3.selectbox("Deadline", ["All", "Confirmed", "Inferred", "No deadline"])
    visible = task_logic.filter_tasks(tasks_all, status, priority, kind)

    if not tasks_all:
        ui.empty_state("No tasks yet", "Tasks are created automatically when you analyze a notice.")
        return
    if not visible:
        ui.empty_state("No tasks match these filters", "Adjust the filters above.")
        return

    st.caption(f"{len(visible)} task(s)")
    with ui.scroll_panel(LIST_PANEL_H):
        for t in visible:
            _render_task(db, t, today)


def _render_task(db: Database, t: Dict[str, Any], today: date) -> None:
    state = t["effective_status"]
    with st.container(border=True):
        ui.accent_bar(state)
        c1, c2 = st.columns([6, 2], vertical_alignment="center")
        with c1:
            st.markdown(ui.title_html(t["title"]), unsafe_allow_html=True)
            st.markdown(ui.clamp(t["description"], 2, muted=True), unsafe_allow_html=True)
            due_text = (f"{fmt_date(t['due_date'])} ({task_logic.describe_due(t['due_date'], today)})"
                        if t["due_date"] else "No deadline")
            ui.badges((state.capitalize(), state), (f"{t['priority'].capitalize()} priority", t["priority"]),
                      (task_logic.deadline_label(t["due_date_type"], bool(t["due_date"])), t["due_date_type"]))
            st.caption(f"Due: {due_text}  |  From: {ui.md_escape(t['notice_title'])}")
        with c2:
            if state == "completed":
                if st.button("Reopen", key=f"reopen_{t['id']}", icon=":material/replay:", width="stretch"):
                    db.set_task_status(t["id"], "pending")
                    ui.flash("Task reopened", "info")
                    st.rerun()
            elif st.button("Mark complete", key=f"done_{t['id']}", icon=":material/check_circle:", width="stretch"):
                db.set_task_status(t["id"], "completed")
                ui.flash("Task marked as completed", "success")
                st.rerun()

        with st.expander("Edit task"):
            with st.form(f"edit_{t['id']}"):
                title = st.text_input("Title", value=t["title"])
                description = st.text_area("Description", value=t["description"] or "")
                e1, e2, e3 = st.columns(3)
                priority = e1.selectbox("Priority", ["high", "medium", "low"],
                                        index=["high", "medium", "low"].index(t["priority"]))
                new_status = e2.selectbox("Status", ["pending", "completed"],
                                          index=0 if t["status"] == "pending" else 1)
                has_due = e3.checkbox("Has a deadline", value=bool(t["due_date"]))
                due_value = st.date_input("Deadline date",
                                          value=date.fromisoformat(t["due_date"]) if t["due_date"] else today)
                if st.form_submit_button("Save changes", type="primary", icon=":material/save:"):
                    if not title.strip():
                        st.error("Title cannot be empty.")
                    else:
                        changes: Dict[str, Any] = {"title": title.strip(), "description": description.strip() or None,
                                                   "priority": priority, "status": new_status}
                        new_due = due_value.isoformat() if has_due else None
                        if new_due != t["due_date"]:
                            changes["due_date"] = new_due
                        db.update_task(t["id"], **changes)
                        ui.flash("Notice updated successfully", "success")
                        st.rerun()


# =========================================================================
# F. Settings
# =========================================================================
def _database_panel(db: Optional[Database], db_error: Optional[DatabaseError]) -> None:
    """Database status for the Settings page. Never shows credentials."""
    configured = config.get_database_url() is not None
    if db is None:  # configured but unreachable
        st.error(str(db_error) if db_error else "The database is unavailable.")
        if st.button("Retry connection", icon=":material/refresh:", key="settings_db_retry"):
            st.cache_resource.clear()
            st.rerun()
        st.caption("Fix DATABASE_URL, then retry. See the README section 'Database troubleshooting'.")
        return

    info = db.describe()
    if info["kind"] == "postgres":
        st.success(f"Connected to **{info['label']}** (permanent cloud storage).")
        st.write(f"**Host:** `{info['host']}:{info['port']}`  |  **Database:** `{info['database']}`")
        st.write(f"**SSL mode:** `{info['ssl']}`  |  **Credentials:** hidden")
        if st.button("Test database connection", icon=":material/database:", key="settings_db_test"):
            try:
                ping = db.check()
                st.success(f"{ping['version']} answered in {ping['seconds'] * 1000:.0f} ms.")
            except DatabaseError as exc:
                st.error(str(exc))
    else:
        st.warning("Using the **local SQLite fallback**. Data lives in a file on this machine and is lost on hosts "
                   "with temporary storage (such as Streamlit Community Cloud). Set DATABASE_URL to use Supabase.")
        st.write(f"**File:** `{info['location']}`  |  **Size:** {info['size_bytes'] / 1024:.1f} KB")
    if configured and auth.gate_enabled() is False:
        st.warning("No APP_PASSWORD is set. Anyone with the app link can read and delete the notices in this "
                   "database. Set APP_PASSWORD (or restrict viewers in Streamlit Cloud) before sharing the link.")
    counts = db.table_counts()
    c1, c2, c3 = st.columns(3)
    c1.metric("Notices", counts["notices"])
    c2.metric("Deadlines", counts["deadlines"])
    c3.metric("Tasks", counts["action_items"])


def page_settings() -> None:
    db: Optional[Database] = None
    db_error: Optional[DatabaseError] = None
    try:
        db = _open_db()
    except DatabaseError as exc:  # keep Settings usable so the user can see how to fix the connection
        db_error = exc
    ui.page_header("Settings", "Configuration status and setup guidance.")

    key_source = config.get_api_key_source()
    top_left, top_right = st.columns(2, gap="large")
    bottom_left, bottom_right = st.columns(2, gap="large")

    with top_left:
        st.markdown("### Gemini API")
        with ui.scroll_panel(SETTINGS_BOX_H):
            if key_source:
                st.success(f"API key detected (from {key_source}). The key itself is never displayed.")
            else:
                st.error("No Gemini API key configured.")
            fallback = config.get_fallback_model()
            st.write(f"**Model:** `{config.get_model_name()}`  (override with `GEMINI_MODEL`)")
            st.write(f"**Fallback:** `{fallback or 'disabled'}`  (`GEMINI_FALLBACK_MODEL`)")
            if key_source and st.button("Test Gemini connection", icon=":material/bolt:"):
                with st.spinner("Contacting Gemini..."):
                    result = ai_engine.test_connection()
                if result["ok"]:
                    st.success(f"{result['message']} Model `{result['model']}` answered in {result['seconds']:.1f}s.")
                else:
                    st.error(result["message"])
                    if result["details"]:
                        st.json({k: str(v) for k, v in result["details"].items()})
            with st.expander("How to configure your API key", expanded=key_source is None):
                st.markdown(
                    "1. Create a key in [Google AI Studio](https://aistudio.google.com/apikey).\n"
                    "2. Choose **one** way to provide it:\n"
                    "   - **Secrets file:** copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` and paste your key.\n"
                    "   - **Environment variable:** set it for the current PowerShell session.\n"
                    "3. Restart the app after changing the key."
                )
                st.code('$env:GEMINI_API_KEY = "paste-your-key-here"', language="powershell")
                st.code('GEMINI_API_KEY = "paste-your-key-here"', language="toml")
                st.caption("Never commit secrets.toml. It is already listed in .gitignore.")

    with top_right:
        st.markdown("### OCR (Tesseract)")
        with ui.scroll_panel(SETTINGS_BOX_H):
            ocr = ocr_engine.get_status()
            if ocr.available:
                st.success(f"Tesseract {ocr.version} is ready.")
                st.write(f"**Path:** `{ocr.path}`")
                st.write(f"**Configured language(s):** `{config.get_ocr_languages()}`")
                st.write(f"**Installed packs:** {', '.join(ocr.languages) or 'unknown'}")
            else:
                st.warning(f"OCR is unavailable. {ocr.message} Scanned PDFs and images cannot be read until it is "
                           "installed (see the README for Windows instructions).")

    with bottom_left:
        st.markdown("### Database")
        with ui.scroll_panel(SETTINGS_BOX_H):
            _database_panel(db, db_error)

    with bottom_right:
        st.markdown("### Application")
        with ui.scroll_panel(SETTINGS_BOX_H):
            st.write(f"**{config.APP_NAME}** v{config.APP_VERSION}")
            st.write(f"Accepted files: {', '.join(e.upper() for e in config.ALLOWED_EXTENSIONS)} "
                     f"(max {config.MAX_FILE_SIZE_MB} MB).")
            st.write(f"Gemini timeout: {config.GEMINI_TIMEOUT_SECONDS}s  |  Retries: {config.GEMINI_MAX_RETRIES}")
            st.caption("AI output can contain mistakes. Always verify dates and requirements against the original notice.")
