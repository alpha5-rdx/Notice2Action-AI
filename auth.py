"""Optional shared-password gate for the whole app.

Set ``APP_PASSWORD`` (Streamlit secrets or environment) to require a password before
any page, data or Gemini call is reachable. When it is not set the app is open, which is
fine for local use but NOT for a public deployment that shares one cloud database.

This is a deliberately simple gate for a single-team app. It is not per-user
authentication. For private sharing you can also use Streamlit Community Cloud's
viewer restrictions.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from typing import Optional

import streamlit as st

import config

_STATE_OK = "_n2a_authenticated"
_STATE_FAILS = "_n2a_failed_attempts"
MAX_PASSWORD_LENGTH = 256


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


def password_matches(candidate: str, expected: Optional[str]) -> bool:
    """Constant-time comparison (hashes first so lengths do not leak)."""
    if not expected or candidate is None or len(candidate) > MAX_PASSWORD_LENGTH:
        return False
    return hmac.compare_digest(_digest(candidate), _digest(expected))


def gate_enabled() -> bool:
    return bool(config.get_app_password())


def is_authenticated() -> bool:
    return (not gate_enabled()) or bool(st.session_state.get(_STATE_OK))


def sign_out() -> None:
    st.session_state.pop(_STATE_OK, None)


def ensure_authenticated() -> bool:
    """Render the login form when required. Returns True when the visitor may continue."""
    if is_authenticated():
        return True

    st.markdown('<div style="height:12vh"></div>', unsafe_allow_html=True)
    _, middle, _ = st.columns([1, 1.2, 1])
    with middle:
        with st.container(border=True):
            st.markdown(f"### {config.APP_NAME}")
            st.caption("This app is protected. Enter the access password to continue.")
            with st.form("n2a_login", clear_on_submit=True):
                password = st.text_input("Password", type="password", max_chars=MAX_PASSWORD_LENGTH)
                submitted = st.form_submit_button("Sign in", type="primary", width="stretch")
            if submitted:
                if password_matches(password, config.get_app_password()):
                    st.session_state[_STATE_OK] = True
                    st.session_state.pop(_STATE_FAILS, None)
                    st.rerun()
                fails = st.session_state.get(_STATE_FAILS, 0) + 1
                st.session_state[_STATE_FAILS] = fails
                time.sleep(min(0.5 * 2 ** (fails - 1), 5))  # slow down guessing within a session
                st.error("Incorrect password.")
    return False
