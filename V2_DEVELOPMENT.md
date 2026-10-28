# V2 development databases

This branch still runs V1C behavior. No V2 business architecture is implemented.

Use the fail-closed launcher from the project root. Both explicit paths are
required. To check configuration without opening databases or calling models:

```sh
.venv/bin/python -m arbitrage.v2_dev --business-db /Users/garyshirk/agents-project/dev_data/v2/arbitrage.db --sessions-db /Users/garyshirk/agents-project/dev_data/v2/sessions.db --check
```

To initialize the existing business and SDK session schemas without starting the
application, copying history, or making model calls, replace `--check` with
`--initialize`. Initialization is idempotent; it never clears existing records.

To intentionally start the terminal application against these isolated files,
omit `--check`. Starting the loop alone makes no model call, but ordinary prompts
can invoke live agents. Do not run live evaluations without authorization.

The equivalent paired environment overrides are `ARBITRAGE_DATABASE_PATH` and
`ARBITRAGE_SESSIONS_DATABASE_PATH`, both absolute filesystem paths. Set both or
neither: a partial, empty, malformed, overlapping, or protected override is
refused before application databases open. Application startup resolves these
after `.env` loading. The launcher sets explicit environment overrides first,
so `.env` cannot replace them. It defaults the session ID to `v2_development`
unless `ARBITRAGE_SESSION_ID` is already explicitly set. Session IDs select
conversations within the configured file; they do not select another file.

With no path overrides, `python agent.py`, `CandidateRepository()`, and
`initialize_database()` retain their project-root V1C defaults. Prefer the V2
launcher rather than bare `python agent.py` for development. Explicit repository
paths remain supported for temporary test databases. Raw SQLite/SDK access and
arbitrary custom scripts are not sandboxed by this launcher.

The launcher rejects both protected root databases in either role, normalized
or symlink aliases, existing hard links, sidecar collisions, and overlapping
business/session files. Validation is not a defense against another process
maliciously replacing filesystem links after validation. Do not concurrently
retarget development paths or run unreviewed scripts.

`dev_data/` and all its contents are Git-ignored. Never commit databases, sidecars,
credentials, or runtime logs. Neither historical database is migrated or copied.
Run deterministic tests using their temporary fixtures:

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Database access inventory: application startup opens the configured business
repository and SDK session file; repository initialization and subsequent
methods use its explicit stored business path, including selection transactions
and read-only context snapshots. Direct `initialize_database()` and default
repository construction honor the paired overrides. Existing diagnostics do not
open these business/session files. Tests inject temporary paths or mock startup
stores; they do not require the development files or production secrets.
