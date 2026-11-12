"""Notice2Action AI - Streamlit entry point.

Run with:  streamlit run app.py
"""
import logging

import streamlit as st

import auth
import config
import pages
import ui
from db_backends import DatabaseError, describe_url

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# Keep HTTP-client chatter out of the terminal (and any URL details with it).
for _noisy in ("httpx", "httpcore", "google_genai"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

st.set_page_config(page_title=config.APP_NAME, page_icon=":material/task_alt:", layout="wide")
ui.inject_css()

# Optional access password (APP_PASSWORD). Nothing below runs until the visitor is signed in.
if not auth.ensure_authenticated():
    st.stop()


def _database_status() -> tuple[str, bool]:
    """Label for the sidebar without opening a connection on every rerun."""
    url = config.get_database_url()
    if not url:
        return "Local SQLite", False
    host = describe_url(url)["host"]
    return ("Supabase PostgreSQL" if host.endswith((".supabase.com", ".supabase.co")) else "PostgreSQL"), True


ui.PAGES.update({
    "dashboard": st.Page(pages.page_dashboard, title="Dashboard", icon=":material/dashboard:",
                         default=True),
    "analyze": st.Page(pages.page_analyze, title="Analyze Notice", icon=":material/upload_file:",
                       url_path="analyze"),
    "notices": st.Page(pages.page_notices, title="My Notices", icon=":material/folder_open:",
                       url_path="notices"),
    "tasks": st.Page(pages.page_tasks, title="Tasks & Deadlines", icon=":material/event_available:",
                     url_path="tasks"),
    "settings": st.Page(pages.page_settings, title="Settings", icon=":material/settings:",
                        url_path="settings"),
})

navigation = st.navigation(list(ui.PAGES.values()), position="hidden")

ui.highlight_active_nav(navigation.url_path)

with st.sidebar:
    ui.brand()
    ui.side_label("Menu")
    for page in ui.PAGES.values():
        st.page_link(page)
    ui.side_label("Status")
    _db_label, _db_cloud = _database_status()
    ui.sidebar_status(config.get_api_key() is not None, config.get_model_name(), _db_label, _db_cloud)
    if auth.gate_enabled():
        if st.button("Sign out", icon=":material/logout:", key="n2a_signout", width="stretch"):
            auth.sign_out()
            st.rerun()
    ui.side_footer(f"v{config.APP_VERSION} | All rights reserved")

try:
    navigation.run()
except DatabaseError as exc:  # outage while a page was running: show a notice, keep the app alive
    ui.database_error(exc)
