"""Classify Gemini / network exceptions into actionable categories.

Nothing in here ever includes the API key or the notice text. Messages coming
back from Google are sanitised (keys redacted, length capped) before they are
logged or shown.
"""
from __future__ import annotations

import re
import socket
from dataclasses import dataclass
from typing import Any, Optional

# Error kinds
INVALID_REQUEST = "invalid_request"   # HTTP 400
AUTH = "auth"                         # HTTP 401/403 (or 400 "API key not valid")
NOT_FOUND = "not_found"               # HTTP 404 (model)
RATE_LIMIT = "rate_limit"             # HTTP 429
SERVER = "server"                     # HTTP 500/502/503
TIMEOUT = "timeout"                   # HTTP 504 / DEADLINE_EXCEEDED / client timeout
NETWORK = "network"                   # DNS, connection refused, TLS, proxy
API_ERROR = "api_error"               # any other HTTP error
UNKNOWN = "unknown"

RETRYABLE_KINDS = frozenset({RATE_LIMIT, SERVER})
FALLBACK_KINDS = frozenset({NOT_FOUND, RATE_LIMIT, SERVER, TIMEOUT})

_KEY_PATTERN = re.compile(r"AIza[0-9A-Za-z_\-]{10,}")
_MAX_MESSAGE = 240


@dataclass
class ErrorInfo:
    kind: str
    http_code: Optional[int] = None
    api_status: Optional[str] = None
    api_message: str = ""
    exception_type: str = ""

    @property
    def retryable(self) -> bool:
        return self.kind in RETRYABLE_KINDS

    @property
    def fallback_eligible(self) -> bool:
        return self.kind in FALLBACK_KINDS


def sanitize(text: Any) -> str:
    """Redact anything that looks like an API key and cap the length."""
    cleaned = _KEY_PATTERN.sub("[REDACTED]", str(text or ""))
    cleaned = " ".join(cleaned.split())
    return cleaned if len(cleaned) <= _MAX_MESSAGE else cleaned[:_MAX_MESSAGE] + "..."


def _http_libs():
    """HTTP libraries the SDK may use (newer google-genai versions use httpx2, older httpx)."""
    libs = []
    for name in ("httpx", "httpx2"):
        try:
            libs.append(__import__(name))
        except ImportError:
            continue
    return libs


def _is_instance_of_any(exc: BaseException, attr: str) -> bool:
    for lib in _http_libs():
        cls = getattr(lib, attr, None)
        if cls is not None and isinstance(exc, cls):
            return True
    return False


def _is_timeout(exc: BaseException) -> bool:
    return isinstance(exc, (TimeoutError, socket.timeout)) or _is_instance_of_any(exc, "TimeoutException")


def classify(exc: BaseException) -> ErrorInfo:
    """Map any exception raised by the SDK / transport to an ErrorInfo."""
    from google.genai import errors

    name = type(exc).__name__
    if isinstance(exc, errors.APIError):
        code = getattr(exc, "code", None)
        status = (getattr(exc, "status", None) or "").upper() or None
        message = sanitize(getattr(exc, "message", None))
        probe = f"{message} {getattr(exc, 'details', '')}".lower()  # matching only, never displayed

        if code in (401, 403):
            kind = AUTH
        elif code == 400 and ("api key" in probe and any(w in probe for w in ("not valid", "invalid", "expired"))
                              or "api_key_invalid" in probe):
            kind = AUTH
        elif code == 400:
            kind = INVALID_REQUEST
        elif code == 404:
            kind = NOT_FOUND
        elif code == 429 or status == "RESOURCE_EXHAUSTED":
            kind = RATE_LIMIT
        elif code == 504 or status == "DEADLINE_EXCEEDED":
            kind = TIMEOUT
        elif code is not None and code >= 500:
            kind = SERVER
        else:
            kind = API_ERROR
        return ErrorInfo(kind, code, status, message, name)

    if _is_timeout(exc):
        return ErrorInfo(TIMEOUT, exception_type=name)
    if _is_instance_of_any(exc, "TransportError") or _is_instance_of_any(exc, "InvalidURL"):
        return ErrorInfo(NETWORK, api_message=sanitize(exc), exception_type=name)
    if isinstance(exc, (ConnectionError, OSError)):
        return ErrorInfo(NETWORK, api_message=sanitize(exc), exception_type=name)
    return ErrorInfo(UNKNOWN, api_message=sanitize(exc), exception_type=name)


def user_message(info: ErrorInfo, model: str, timeout_s: int, attempts: int = 1) -> str:
    """Clear, actionable text for the Streamlit UI."""
    code = f" (HTTP {info.http_code})" if info.http_code else ""
    tried = f" after {attempts} attempts" if attempts > 1 else ""
    detail = f" Google said: {info.api_message}" if info.api_message else ""

    if info.kind == AUTH:
        return ("Gemini rejected the API key" + code + ". Check that GEMINI_API_KEY is correct and active, "
                "and that the Gemini API is enabled for its project. Then restart the app." + detail)
    if info.kind == INVALID_REQUEST:
        return (f"Gemini rejected the request as invalid{code}." + detail +
                " If this persists, try a different GEMINI_MODEL.")
    if info.kind == NOT_FOUND:
        return (f"Model '{model}' was not found or is not available to your account{code}. "
                "Set GEMINI_MODEL in .streamlit/secrets.toml to a model your key can use "
                "(for example gemini-3.5-flash) and restart the app.")
    if info.kind == RATE_LIMIT:
        return (f"Gemini rate limit or quota exceeded{code}{tried}. Wait a minute and retry, or check your "
                "quota and billing in Google AI Studio.")
    if info.kind == SERVER:
        return (f"Gemini had a server error{code}{tried}. This is on Google's side; wait a moment and try again.")
    if info.kind == TIMEOUT:
        return (f"Gemini did not answer within {timeout_s} seconds using model '{model}'{code}. "
                "The model is probably overloaded or slow. Try again, or set GEMINI_MODEL to a faster model "
                "such as gemini-3.1-flash-lite.")
    if info.kind == NETWORK:
        return ("Could not connect to Gemini. Check your internet connection, firewall, VPN or proxy settings, "
                "then try again.")
    if info.kind == API_ERROR:
        return f"Gemini returned an unexpected error{code}." + detail
    return f"Unexpected error while contacting Gemini ({info.exception_type or 'unknown'})."
