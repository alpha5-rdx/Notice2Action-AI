"""Database administration CLI for Notice2Action AI.

    python db_tools.py check                       test the connection (no changes)
    python db_tools.py init                        create / upgrade the PostgreSQL schema
    python db_tools.py migrate-sqlite              DRY RUN: show what would be copied from SQLite
    python db_tools.py migrate-sqlite --apply      copy SQLite data into PostgreSQL

The connection string is read from DATABASE_URL (environment or .streamlit/secrets.toml).
It is never printed. The SQLite file is opened read-only and never modified.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import config
from database import Database
from db_backends import DatabaseError
from db_migrate import TABLES, migrate

OK, FAIL = "[ OK ]", "[FAIL]"


def _open_postgres() -> Database:
    if not config.get_database_url():
        raise DatabaseError("DATABASE_URL is not set. Add it to .streamlit/secrets.toml or the environment first.",
                            "config")
    return Database(init=False)


def cmd_check(_args) -> int:
    db = _open_postgres() if config.get_database_url() else Database()
    info = db.describe()
    print(f"{OK} Backend  : {info['label']}")
    if info["kind"] == "postgres":
        print(f"      Host     : {info['host']}:{info['port']}  database={info['database']}  ssl={info['ssl']}")
    else:
        print(f"      File     : {info['location']}")
    ping = db.check()
    print(f"{OK} Connected: {ping['version']} ({ping['seconds'] * 1000:.0f} ms)")
    try:
        counts = db.table_counts()
        print(f"{OK} Tables   : " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    except DatabaseError:
        print(f"{FAIL} Tables not found. Run:  python db_tools.py init")
        return 1
    return 0


def cmd_init(_args) -> int:
    db = _open_postgres()
    applied = db.init_schema()
    print(f"{OK} Schema up to date." + (f" Applied: {', '.join(applied)}" if applied else " Nothing new to apply."))
    return 0


def cmd_migrate(args) -> int:
    db = _open_postgres()
    db.init_schema()
    sqlite_path = Path(args.sqlite) if args.sqlite else config.get_db_path()
    print(f"Source (read-only): {sqlite_path}")
    info = db.describe()
    print(f"Target            : {info['label']} at {info['host']}:{info['port']}/{info['database']}")
    print("Mode              : " + ("APPLY (writes to the target)" if args.apply else "DRY RUN (no changes)"))
    report = migrate(sqlite_path, db, apply=args.apply)

    print(f"\n{'table':<14}{'in source':>10}{'to insert':>11}{'already there':>15}{'skipped':>9}"
          + (f"{'inserted':>10}" if args.apply else ""))
    for t in TABLES:
        s = report.stats[t]
        print(f"{t:<14}{s.source:>10}{s.to_insert:>11}{s.already_present:>15}{s.skipped:>9}"
              + (f"{s.inserted:>10}" if args.apply else ""))
    for w in report.warnings:
        print(f"  warning: {w}")
    for e in report.errors + report.validation_problems:
        print(f"  {FAIL} {e}")

    if not report.ok:
        print(f"\n{FAIL} Nothing was written." if args.apply else f"\n{FAIL} Fix the problems above before applying.")
        return 1
    if args.apply:
        print(f"\n{OK} Migration committed and validated (counts, ids, relationships). SQLite file left untouched.")
    else:
        print(f"\n{OK} Dry run clean. Re-run with --apply to copy the data.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Notice2Action AI database tools")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="test the database connection").set_defaults(func=cmd_check)
    sub.add_parser("init", help="apply SQL migrations to PostgreSQL").set_defaults(func=cmd_init)
    diag = sub.add_parser("diagnose", help="read-only, step-by-step connection diagnosis (secrets are masked)")
    diag.add_argument("--timeout", type=int, default=10, help="seconds per connection attempt (default 10)")
    diag.add_argument("--port", type=int, help="try another port with the same credentials (e.g. 6543)")
    diag.add_argument("--parse-only", action="store_true", help="only analyse DATABASE_URL; make NO network connection")
    diag.add_argument("--controls-only", action="store_true",
                      help="like --controls but WITHOUT repeating the real login (fewest failed attempts)")
    diag.add_argument("--controls", action="store_true",
                      help="also send 3 login attempts with FAKE passwords to learn whether the pooler knows your "
                           "project (each counts as one failed login)")
    diag.set_defaults(func=lambda a: __import__("db_diagnose").run(timeout=a.timeout, port_override=a.port,
                                                                    controls=a.controls, parse_only=a.parse_only,
                                                                    controls_only=a.controls_only))
    mig = sub.add_parser("migrate-sqlite", help="copy SQLite data into PostgreSQL (dry run unless --apply)")
    mig.add_argument("--sqlite", help="path to the SQLite file (default: data/notice2action.db)")
    mig.add_argument("--apply", action="store_true", help="actually write to PostgreSQL")
    mig.set_defaults(func=cmd_migrate)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    try:
        return args.func(args)
    except DatabaseError as exc:
        print(f"{FAIL} {exc}")
        details = getattr(exc, "details", None)
        if details is not None:  # already sanitized: no password, user or connection string
            print(f"      kind={exc.kind}  exception={details.exc_class}  sqlstate={details.sqlstate or 'n/a'}")
            print(f"      detail: {details.sanitized}")
        if config.get_database_url():
            print("      Run `python db_tools.py diagnose` for a step-by-step, read-only connection diagnosis.")
        return 2


if __name__ == "__main__":
    sys.exit(main())
