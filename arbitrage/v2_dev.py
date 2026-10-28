"""Fail-closed V2 launcher. Checking/initialization never starts an agent."""

import argparse
import os

from arbitrage.database_config import (
    BUSINESS_DATABASE_ENV, SESSION_DATABASE_ENV, isolated_database_paths,
)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--business-db", required=True)
    parser.add_argument("--sessions-db", required=True)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true", help="Validate paths only; no database opens or writes.")
    modes.add_argument("--initialize", action="store_true", help="Initialize schemas only; no application loop.")
    args = parser.parse_args(argv)
    try:
        paths = isolated_database_paths(args.business_db, args.sessions_db)
    except ValueError as error:
        parser.error(str(error))

    print(f"V2 business database: {paths.business}")
    print(f"V2 session database: {paths.sessions}")
    if args.check:
        return

    # Explicit values override any path settings in .env (load_dotenv defaults
    # to override=False). No credentials are copied or displayed.
    os.environ[BUSINESS_DATABASE_ENV] = str(paths.business)
    os.environ[SESSION_DATABASE_ENV] = str(paths.sessions)
    os.environ.setdefault("ARBITRAGE_SESSION_ID", "v2_development")
    paths.business.parent.mkdir(parents=True, exist_ok=True)
    paths.sessions.parent.mkdir(parents=True, exist_ok=True)
    if args.initialize:
        from agents import SQLiteSession
        from arbitrage.persistence import CandidateRepository

        CandidateRepository(paths.business)
        session = SQLiteSession(os.environ["ARBITRAGE_SESSION_ID"], paths.sessions)
        session.close()
        print("V2 schemas initialized; no application loop or model call started.")
        return

    from arbitrage.application import main as application_main

    application_main()


if __name__ == "__main__":
    main()
