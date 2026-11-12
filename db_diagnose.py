"""Step-by-step, read-only diagnosis of a PostgreSQL / Supabase connection problem.

Run with:  python db_tools.py diagnose            (add --parse-only to skip all network access)

What it does (and does NOT do)
* Reads DATABASE_URL exactly like the app does and reports WHERE it came from.
* Parses it and checks it for common Supabase mistakes (structure only: lengths and character classes).
* Tests DNS, then a raw TCP connection and the PostgreSQL SSL handshake to every resolved IP address.
* Opens a real connection with the app's own settings and runs `SELECT 1` / `SHOW server_version`.
* Optional --controls: a few login attempts with FAKE passwords to learn how the pooler reacts.
* Never creates tables, never runs migrations, never writes data.
* Never prints the password, the project-ref, the full URL, APP_PASSWORD or the Gemini key:
  every line passes through a scrubber that masks them.
"""
from __future__ import annotations

import os
import random
import re
import socket
import string
import struct
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, quote, unquote, urlsplit

import config
from db_backends import (DatabaseError, build_conninfo, connection_secrets, sanitize_error_text, translate_pg_error,
                         url_warnings)

SSL_REQUEST = struct.pack("!II", 8, 80877103)   # PostgreSQL SSLRequest message
LIBPQ_ENV_VARS = ("PGHOST", "PGHOSTADDR", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD", "PGPASSFILE", "PGSERVICE",
                  "PGSERVICEFILE", "PGSSLMODE", "PGSSLROOTCERT", "PGOPTIONS", "PGCONNECT_TIMEOUT", "PGAPPNAME",
                  "PGCHANNELBINDING", "PGTARGETSESSIONATTRS")
FAKE_PASSWORD = "diagnostic-not-a-real-password"


class Reporter:
    """Prints lines after masking every known secret."""

    def __init__(self, emit: Callable[[str], None] = print):
        self._emit = emit
        self._secrets: List[str] = []

    def add_secrets(self, *values: Optional[str]) -> None:
        for value in values:
            if value and len(value) >= 4 and value not in self._secrets:
                self._secrets.append(value)

    def scrub(self, text: str) -> str:
        out = str(text)
        for secret in sorted(self._secrets, key=len, reverse=True):
            out = out.replace(secret, "***").replace(quote(secret, safe=""), "***")
        return out

    def say(self, line: str = "") -> None:
        self._emit(self.scrub(line))

    def title(self, text: str) -> None:
        self.say(f"\n== {text}")


def describe_chars(text: str) -> str:
    """Character-class summary that reveals stray/hidden characters without revealing the text."""
    import unicodedata

    lower = sum(c.isascii() and c.islower() for c in text)
    upper = sum(c.isascii() and c.isupper() for c in text)
    digits = sum(c.isascii() and c.isdigit() for c in text)
    others = [c for c in text if not (c.isascii() and c.isalnum())]
    parts = [f"{len(text)} chars", f"{lower} lowercase", f"{upper} uppercase", f"{digits} digits"]
    if others:
        distinct: List[str] = []
        for c in others:
            label = f"U+{ord(c):04X} {unicodedata.name(c, 'unnamed')}" if not (c.isprintable() and c.isascii()) else repr(c)
            if label not in distinct:
                distinct.append(label)
        parts.append(f"{len(others)} other: " + ", ".join(distinct[:8]))
    else:
        parts.append("0 other")
    return "; ".join(parts)


def user_segments(url: str) -> List[str]:
    """The user name split on dots: [role, project-ref, extra...]. Empty list when there is no user."""
    netloc = urlsplit(url).netloc
    if "@" not in netloc:
        return []
    user = unquote(netloc.rsplit("@", 1)[0].partition(":")[0])
    return user.split(".") if user else []


def _parse_structure(url: str) -> Dict[str, Any]:
    """Structural facts about the URL. Contains no secret values."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    try:
        port = parts.port
    except ValueError:
        port = None
    segments = user_segments(url)
    region = ""
    match = re.match(r"^aws-\d+-([a-z0-9-]+)\.pooler\.supabase\.com$", host)
    if match:
        region = match.group(1)
    userinfo = parts.netloc.rsplit("@", 1)[0] if "@" in parts.netloc else ""
    raw_password = userinfo.partition(":")[2]
    return {
        "scheme": parts.scheme, "segments": segments,
        "password_present": bool(raw_password),
        "password_has_reserved_unencoded": bool(re.search(r"[@/?#\[\]]", raw_password)),
        "password_is_placeholder": "YOUR" in raw_password.upper() and ("[" in raw_password or "PASSWORD" in raw_password.upper()),
        "host": host, "port": port, "database": parts.path.lstrip("/") or "(default)",
        "host_kind": ("pooler" if host.endswith(".pooler.supabase.com")
                      else ("direct" if re.match(r"^db\.[a-z0-9]+\.supabase\.co$", host) else "other")),
        "region": region,
        "query": {k: v for k, v in parse_qsl(parts.query) if k.lower() not in {"password", "passfile"}},
    }


def _resolve(host: str, port: int) -> List[Tuple[int, tuple]]:
    seen, out = set(), []
    for family, _t, _p, _c, sockaddr in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
        if sockaddr[0] not in seen:
            seen.add(sockaddr[0])
            out.append((family, sockaddr))
    return out


def _tcp_and_ssl(family: int, sockaddr: tuple, timeout: float) -> Tuple[str, str]:
    """(tcp result, ssl-handshake result) for one address."""
    started = time.monotonic()
    try:
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(sockaddr)
    except OSError as exc:
        return f"FAILED ({type(exc).__name__}: {exc.strerror or exc})", "not tested"
    tcp = f"connected in {(time.monotonic() - started) * 1000:.0f} ms"
    try:
        sock.sendall(SSL_REQUEST)
        reply = sock.recv(1)
        meaning = {b"S": "server supports SSL (a PostgreSQL protocol endpoint answered)",
                   b"N": "server answered but does NOT offer SSL", b"E": "server answered with a protocol error"}
        ssl = meaning.get(reply, f"unexpected reply {reply!r} (not a PostgreSQL endpoint?)")
    except OSError as exc:
        ssl = f"no reply ({type(exc).__name__})"
    finally:
        sock.close()
    return tcp, ssl


def _attempt(r: Reporter, label: str, conninfo: str, secrets: List[str], timeout: int,
             extra: Optional[Dict[str, str]] = None) -> Tuple[bool, Optional[DatabaseError]]:
    import psycopg
    from psycopg.conninfo import make_conninfo

    ci = make_conninfo(conninfo, **(extra or {})) if extra else conninfo
    started = time.monotonic()
    try:
        with psycopg.connect(ci, connect_timeout=timeout, prepare_threshold=None) as conn:
            one = conn.execute("SELECT 1").fetchone()[0]
            version = conn.execute("SHOW server_version").fetchone()[0]
            database = conn.execute("SELECT current_database()").fetchone()[0]
        r.say(f"  [ OK ] {label}: SELECT 1 -> {one}; server_version={version}; database={database}; "
              f"{(time.monotonic() - started) * 1000:.0f} ms")
        return True, None
    except Exception as exc:
        err = translate_pg_error(exc, secrets)
        d = err.details
        r.say(f"  [FAIL] {label}: {(time.monotonic() - started) * 1000:.0f} ms")
        r.say(f"         exception class : {d.exc_class if d else type(exc).__name__}")
        r.say(f"         SQLSTATE        : {(d.sqlstate if d and d.sqlstate else 'not provided (connection-level failure)')}")
        r.say(f"         classified as   : {err.kind}")
        r.say(f"         server message  : {d.server_message if d and d.server_message else '(none: failure happened before the server replied)'}")
        r.say(f"         sanitized detail: {d.sanitized if d else sanitize_error_text(exc, secrets)}")
        return False, err


# --------------------------------------------------------------------------- control probes
def _probe_message(conninfo: str, timeout: int, secrets: List[str]) -> Tuple[str, str]:
    """(kind, short public server message) for one deliberately fake-credential login attempt."""
    import psycopg

    try:
        with psycopg.connect(conninfo, connect_timeout=timeout, prepare_threshold=None) as conn:
            conn.execute("SELECT 1")
        return "connected", "(connected)"
    except Exception as exc:
        err = translate_pg_error(exc, secrets)
        return err.kind, (err.details.server_message if err.details else "") or type(exc).__name__


def _controls(r: Reporter, params: Dict[str, Any], segments: List[str], timeout: int) -> Dict[str, Tuple[str, str]]:
    """Login attempts with FAKE passwords to learn how this pooler reacts. Each is one failed login."""
    from psycopg.conninfo import make_conninfo

    random_ref = "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(20))
    full_user = ".".join(segments)
    cases: Dict[str, Tuple[str, str]] = {
        "A": ("a random, non-existent project ref (baseline: how an UNKNOWN project is rejected)",
              f"postgres.{random_ref}"),
    }
    if len(segments) > 2:
        classic = [seg for seg in segments[1:] if re.fullmatch(r"[a-z0-9]{20}", seg)]
        if len(classic) == 1:   # keep only the part shaped like a real project ref, drop everything else
            cases["D"] = ("your user reduced to 'postgres.<the 20-character ref>' (all other parts dropped)",
                          f"{segments[0]}.{classic[0]}")
        else:
            cases["D"] = ("your user WITHOUT the extra dot-separated part(s): 'postgres.<project-ref>'",
                          ".".join(segments[:2]))
    else:
        cases["B"] = ("YOUR project ref with a non-existent role", f"n2a_diag_nosuchrole.{'.'.join(segments[1:])}")
        cases["C"] = ("YOUR user (role + ref) with a FAKE password", full_user)
    results: Dict[str, Tuple[str, str]] = {}
    r.say("  (each line is one login attempt with a fake password; no real credential is sent)")
    for key, (label, user) in cases.items():
        probe = dict(params, user=user, password=FAKE_PASSWORD)
        kind, message = _probe_message(make_conninfo(**probe), timeout, [FAKE_PASSWORD, user, random_ref, *segments[1:]])
        results[key] = (kind, message)
        r.say(f"  {key}) {label}:")
        r.say(f"       -> {kind}: {message}")
    return results


def interpret_controls(results: Dict[str, Tuple[str, str]], extra_parts: bool) -> List[str]:
    low = {k: v[1].lower() for k, v in results.items()}
    unknown = lambda m: "enotfound" in m or "tenant or user not found" in m  # noqa: E731
    lines: List[str] = []
    if extra_parts and "D" in low:
        if "password authentication failed" in low["D"] or "scram" in low["D"]:
            lines.append("CONFIRMED: with the extra part(s) removed, the pooler recognises your project and role and only "
                         "rejects the fake password. The extra text in your DATABASE_URL user name is the problem.")
        elif unknown(low["D"]):
            lines.append("Even without the extra part(s) the pooler does not recognise this project ref on this host: "
                         "the ref itself or the pooler host (aws-0 vs aws-1, region) is wrong. Copy both from Connect.")
        elif "eauthquery" in low["D"]:
            lines.append("Without the extra part(s) the pooler still cannot find the login role: the database role may "
                         "be missing/disabled, or the project is not fully provisioned.")
        else:
            lines.append("The control result is not a known pattern; compare it with the real failure above.")
        return lines
    a, b, c = low.get("A", ""), low.get("B", ""), low.get("C", "")
    if unknown(a) and unknown(b):
        lines.append("Your project ref is NOT recognised by this pooler host (treated like a random ref): wrong ref in "
                     "the user name, or the project is on a different pooler host (aws-0 vs aws-1, other region).")
    elif a == b == c and "eauthquery" in a:
        lines.append("This pooler host answers EAUTHQUERY for EVERY user, even a random project: use the exact host "
                     "from the Connect dialog.")
    elif unknown(a) and not unknown(b):
        lines.append("The pooler recognises your project ref (a random ref is rejected differently), so host and ref are "
                     "probably right; the remaining suspect is the role/credentials.")
        if "password authentication failed" in c:
            lines.append("With a fake password the pooler reaches the password check: your role exists. Re-check the "
                         "real password text for stray characters.")
        elif "eauthquery" in c:
            lines.append("Even with your ref and role the lookup finds no login role: the role may be missing/disabled.")
    else:
        lines.append("The control results do not match a known pattern; compare them with the real failure above.")
    return lines


# --------------------------------------------------------------------------- main entry
def run(emit: Callable[[str], None] = print, timeout: int = 10, port_override: Optional[int] = None,
        controls: bool = False, parse_only: bool = False, controls_only: bool = False) -> int:
    r = Reporter(emit)
    r.say("Notice2Action AI - database connection diagnosis (read-only; secrets are masked)")

    # ------------------------------------------------------------ 1. configuration source
    r.title("1. Where DATABASE_URL comes from")
    secrets_file = config.BASE_DIR / ".streamlit" / "secrets.toml"
    r.say(f"  .streamlit/secrets.toml present : {secrets_file.exists()}")
    candidates = config.database_url_candidates()
    for source, value in candidates:
        r.add_secrets(value)
        flag = " (PLACEHOLDER: ignored)" if config._is_db_placeholder(value) else ""
        r.say(f"  found in {source:<22}: yes, {len(value)} characters{flag}")
    if not candidates:
        r.say("  DATABASE_URL is not set anywhere -> the app uses the local SQLite fallback.")
        r.say("\nResult: nothing to diagnose. Add DATABASE_URL to .streamlit/secrets.toml.")
        return 2
    real = [(s, v) for s, v in candidates if not config._is_db_placeholder(v)]
    if len({v for _s, v in real}) > 1:
        r.say("  [WARN] secrets.toml and the environment hold DIFFERENT values. The app uses: "
              f"{config.get_database_url_source()} (secrets win). Remove the stale one:")
        r.say("         PowerShell:  Remove-Item Env:DATABASE_URL")
    elif len(real) > 1:
        r.say("  both sources hold the same value.")
    url = config.get_database_url()
    if not url:
        r.say("  Only placeholders found -> the app uses the local SQLite fallback.")
        return 2
    r.say(f"  ACTIVE source                   : {config.get_database_url_source()}")
    r.add_secrets(url, config.get_app_password(), config.get_api_key())

    set_env = [n for n in LIBPQ_ENV_VARS if os.environ.get(n)]
    r.say("  libpq environment variables set : " + (", ".join(set_env) if set_env else "none"))
    if set_env:
        r.say("  [WARN] libpq reads these as defaults for anything the URL does not specify. PGOPTIONS in particular "
              "is rejected by Supabase poolers; PGSSLMODE/PGHOST can silently change behaviour.")
        r.add_secrets(os.environ.get("PGPASSWORD"))

    # ------------------------------------------------------------ 2. parsing
    r.title("2. How the URL is parsed (structure only)")
    s = _parse_structure(url)
    segments: List[str] = s["segments"]
    effective_port = port_override or s["port"] or 5432
    findings: List[str] = list(url_warnings(url))
    r.say(f"  scheme                : {s['scheme']}")
    if segments:
        r.say(f"  user name has         : {len(segments)} dot-separated part(s) (a Supabase pooler user has exactly 2)")
        for index, segment in enumerate(segments):
            if index == 0:
                r.say(f"    part 1 (role)       : {segment!r}  <- must be an existing login role (Supabase: 'postgres')")
            elif index == 1:
                r.say(f"    part 2 (project-ref): {describe_chars(segment)}")
            else:
                r.say(f"    part {index + 1} (EXTRA)      : {describe_chars(segment)}")
        r.say("    (classic Supabase project refs are 20 lowercase letters/digits; the values themselves are never shown)")
    else:
        r.say("  user name             : (none)")
    r.say(f"  password present      : {s['password_present']}")
    r.say(f"  host                  : {s['host']}  ({s['host_kind']}" + (f", region {s['region']}" if s['region'] else "") + ")")
    r.say(f"  port                  : {s['port']}" + (f"  (overridden to {port_override} for this run)" if port_override else "")
          + {5432: "  (Session pooler / direct)", 6543: "  (Transaction pooler)"}.get(effective_port, ""))
    r.say(f"  database              : {s['database']}")
    r.say(f"  URL query options     : {s['query'] or 'none'}")
    if s["host_kind"] == "direct":
        findings.append("Direct host (db.<ref>.supabase.co) is IPv6-only unless you have the IPv4 add-on: use a pooler string.")
    if s["password_has_reserved_unencoded"]:
        findings.append("The password contains reserved characters that are not URL-encoded (@ / ? # [ ]).")
    if s["password_is_placeholder"] or not s["password_present"]:
        findings.append("The password part is missing or still a placeholder such as [YOUR-PASSWORD].")
    if s["database"] not in {"postgres", "(default)"}:
        findings.append(f"Database name is '{s['database']}'; Supabase's database is normally 'postgres'.")
    if effective_port not in (5432, 6543) and s["host_kind"] == "pooler":
        findings.append(f"Unusual pooler port {effective_port}; Supabase uses 5432 (session) or 6543 (transaction).")
    try:
        conninfo, params = build_conninfo(url, timeout)
    except DatabaseError as exc:
        r.say(f"  [FAIL] The app would reject this URL before connecting: {exc}")
        return 2
    r.add_secrets(*connection_secrets(url, params), *segments[1:])
    r.say(f"  SSL mode the app uses : {params.get('sslmode', 'prefer')}")
    r.say(f"  connect timeout       : {params.get('connect_timeout')} s")
    for finding in findings:
        r.say(f"  [WARN] {finding}")
    if not findings:
        r.say("  no structural problems found")
    if parse_only:
        r.say("\n(--parse-only: no network access was made)")
        return 1 if findings else 0

    # ------------------------------------------------------------ 3. DNS / TCP / SSL handshake
    host = params.get("host", s["host"])
    r.title(f"3. Network path to {host}:{effective_port}")
    try:
        addrs = _resolve(host, effective_port)
    except OSError as exc:
        r.say(f"  [FAIL] DNS lookup failed ({type(exc).__name__}). The name does not resolve from this machine.")
        return 1
    r.say(f"  DNS resolved {len(addrs)} address(es): " + ", ".join(
        f"{a[0]} ({'IPv6' if f == socket.AF_INET6 else 'IPv4'})" for f, a in addrs))
    reachable: List[Tuple[int, tuple]] = []
    for family, sockaddr in addrs:
        tcp, ssl = _tcp_and_ssl(family, sockaddr, min(timeout, 6))
        r.say(f"  {sockaddr[0]:<40} TCP: {tcp}")
        r.say(f"  {'':<40} SSL handshake: {ssl}")
        if tcp.startswith("connected"):
            reachable.append((family, sockaddr))
    if not reachable:
        r.say("\nConclusion: the refusal/timeout happens at the NETWORK level on this machine's path to the pooler "
              "(firewall, VPN, antivirus, proxy or ISP). No PostgreSQL login was attempted.")
        return 1
    r.say("  -> TCP and the PostgreSQL protocol are reachable, so a plain 'connection refused' is NOT what is "
          "happening. Any failure below was returned by the server/pooler itself.")

    # ------------------------------------------------------------ 4. real connection
    r.title("4. Real PostgreSQL connection using the app's own settings")
    secrets = connection_secrets(url, params)
    extra = {"port": str(port_override)} if port_override else None
    if controls_only:   # skip the real login (it already failed): fewer failed attempts against the pooler
        r.say("  skipped (--controls-only): the real login is not repeated.")
        ok, err, controls = False, None, True
    else:
        ok, err = _attempt(r, "default (as the app connects)", conninfo, secrets, timeout, extra)
    per_address_results: List[bool] = []
    server_replied = bool(err and err.details and re.search(r"\b(FATAL|ERROR)\b", err.details.sanitized))
    if not ok and server_replied:
        r.say("\n  The SERVER replied with an error, so every IP is expected to behave the same. Not retrying "
              "per address (extra failed logins can trip the pooler's circuit breaker).")
    if not ok and len(reachable) > 1 and not server_replied and not controls_only:
        r.say("\n  Retrying each IP address separately to rule out an IPv4/IPv6 routing issue:")
        for family, sockaddr in reachable:
            good, _ = _attempt(r, f"via {sockaddr[0]}", conninfo, secrets, timeout,
                               dict(extra or {}, hostaddr=sockaddr[0]))
            per_address_results.append(good)

    control_lines: List[str] = []
    if controls and not ok:
        r.title("4b. Control probes (fake passwords) - does the pooler recognise your project?")
        if len(segments) < 2:
            r.say("  skipped: the user name has no '.<project-ref>' part to test.")
        else:
            control_lines = interpret_controls(_controls(r, params, segments, timeout), len(segments) > 2)

    # ------------------------------------------------------------ 5. conclusion
    r.title("5. Conclusion")
    if ok:
        r.say("  A real PostgreSQL query succeeded: this machine CAN connect with the configured URL.")
        r.say("  If the app still fails, restart it (cached connection) and check the app's own log.")
        return 0
    if any(per_address_results):
        r.say("  The default attempt failed but a specific IP address worked: an IPv4/IPv6 routing problem on this "
              "machine/network. Prefer an IPv4 network or the Session pooler.")
        return 1
    kind = err.kind if err else ("auth" if controls_only else "unknown")
    origin = {
        "auth": "the Supabase pooler (Supavisor): it answered at protocol level and refused the login during its own "
                "user lookup, before any application query. This is NOT a local network refusal and NOT a TCP refusal.",
        "blocked": "the Supabase pooler (circuit breaker) after repeated failed logins.",
        "upstream": "the DATABASE SERVER behind the pooler (the pooler answered but could not reach Postgres).",
        "database": "the database server: that database name does not exist.",
        "ssl": "the SSL negotiation between this machine and the pooler.",
        "capacity": "the pooler/database connection limit.",
        "refused": "this machine's network path (refused before any server logic ran).",
        "timeout": "the network path or an unresponsive/paused project.",
        "connect": "the server side (a message came back) but it is not one this tool recognises; see 'server message'.",
    }.get(kind, "unclear; see the exception details above.")
    r.say(f"  Failure originates from: {origin}")
    r.say(f"  Classified error kind  : {kind}")
    for line in control_lines:
        r.say(f"  Control probes say     : {line}")
    for finding in findings:
        r.say(f"  Likely related         : {finding}")
    r.say("  Next step: " + {
        "auth": "In Supabase open Connect > Session pooler, copy the string again and replace ONLY the password "
                "placeholder. The user must be exactly postgres.<project-ref>, and the host must be the one shown there.",
        "blocked": "wait ~10 minutes, fix the credentials, retry once.",
        "upstream": "check the project is Active (not paused) in the Supabase dashboard; wait for it to finish starting.",
        "timeout": "check VPN/firewall, and that the project is not paused.",
    }.get(kind, "compare with the Connect dialog in Supabase and re-run this diagnosis."))
    return 1
