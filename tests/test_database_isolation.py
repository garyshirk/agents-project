import asyncio
import contextlib
import io
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agents import SQLiteSession

from arbitrage import application, database_config as config, v2_dev
from arbitrage.persistence import CandidateRepository, initialize_database


class DatabaseIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.business = self.root / "dev" / "business.db"
        self.sessions = self.root / "dev" / "sessions.db"
        self.old_business = self.root / "protected-business.db"
        self.old_sessions = self.root / "protected-sessions.db"
        for path in (self.old_business, self.old_sessions):
            path.write_bytes(b"protected test fixture, not a real production database")
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {}, clear=True).start()
        patch.object(config, "DEFAULT_BUSINESS_DATABASE", self.old_business).start()
        patch.object(config, "DEFAULT_SESSION_DATABASE", self.old_sessions).start()

    def env(self):
        return {config.BUSINESS_DATABASE_ENV: str(self.business),
                config.SESSION_DATABASE_ENV: str(self.sessions)}

    def args(self, mode=None):
        args = ["--business-db", str(self.business), "--sessions-db", str(self.sessions)]
        return args + ([mode] if mode else [])

    def launch(self, mode):
        with contextlib.redirect_stdout(io.StringIO()):
            v2_dev.main(self.args(mode))

    def test_legacy_defaults_are_unchanged_without_overrides(self):
        self.assertEqual(config.get_database_paths({}), config.DatabasePaths(self.old_business, self.old_sessions))
        project = Path(__file__).resolve().parent.parent
        self.assertEqual(application.SESSIONS_DATABASE_PATH, project / "sessions.db")

    def test_both_explicit_paths_are_resolved(self):
        self.assertEqual(config.get_database_paths(self.env()), config.DatabasePaths(self.business, self.sessions))

    def test_partial_configuration_is_refused(self):
        for key, value in self.env().items():
            with self.subTest(key=key), self.assertRaises(ValueError):
                config.get_database_paths({key: value})

    def test_malformed_configuration_is_refused(self):
        for value in ("", " ", "relative.db", ":memory:", "file:/tmp/example.db", " /tmp/example.db", "/tmp/bad\x00.db"):
            for key in self.env():
                env = self.env() | {key: value}
                with self.subTest(value=value, key=key), self.assertRaises(ValueError):
                    config.get_database_paths(env)

    def test_either_protected_database_is_refused_in_either_role(self):
        for protected in (self.old_business, self.old_sessions):
            for key in self.env():
                with self.subTest(protected=protected, key=key), self.assertRaises(ValueError):
                    config.get_database_paths(self.env() | {key: str(protected)})

    def test_normalized_alias_is_refused(self):
        alias = self.root / "unused" / ".." / self.old_business.name
        with self.assertRaises(ValueError):
            config.isolated_database_paths(alias, self.sessions)

    def test_symlink_aliases_are_refused(self):
        for index, old in enumerate((self.old_business, self.old_sessions)):
            link = self.root / f"alias-{index}.db"
            link.symlink_to(old)
            with self.assertRaises(ValueError):
                config.isolated_database_paths(link, self.sessions)

    def test_parent_symlink_alias_is_refused(self):
        link = self.root / "parent-link"
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            config.isolated_database_paths(link / self.old_business.name, self.sessions)

    def test_existing_hardlink_alias_is_refused(self):
        link = self.root / "hardlink.db"
        link.hardlink_to(self.old_sessions)
        with self.assertRaises(ValueError):
            config.isolated_database_paths(self.business, link)

    def test_protected_sidecars_are_refused(self):
        for suffix in ("-wal", "-shm", "-journal"):
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                config.isolated_database_paths(str(self.old_business) + suffix, self.sessions)

    def test_existing_development_sidecar_alias_is_refused(self):
        self.business.parent.mkdir()
        Path(str(self.business) + "-wal").symlink_to(self.old_business)
        with self.assertRaises(ValueError):
            config.isolated_database_paths(self.business, self.sessions)

    def test_overlapping_databases_or_sidecars_are_refused(self):
        for suffix in config.SQLITE_SUFFIXES:
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                config.isolated_database_paths(self.business, str(self.business) + suffix)

    def test_directory_and_file_parent_are_refused(self):
        for bad in (self.root, self.old_business / "child.db"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                config.isolated_database_paths(bad, self.sessions)

    def test_launcher_requires_both_paths(self):
        for args in ([], ["--business-db", str(self.business)], ["--sessions-db", str(self.sessions)]):
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                v2_dev.main(args + ["--check"])
        self.assertFalse(self.business.parent.exists())

    def test_launcher_refuses_unsafe_configuration_before_creating_anything(self):
        args = ["--business-db", str(self.business), "--sessions-db", str(self.old_sessions), "--initialize"]
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            v2_dev.main(args)
        self.assertFalse(self.business.parent.exists())

    def test_check_mode_never_opens_database_or_calls_application(self):
        with patch("sqlite3.connect", side_effect=AssertionError("Database opened")), \
                patch.object(application, "main", side_effect=AssertionError("Application called")):
            self.launch("--check")
        self.assertFalse(self.business.parent.exists())

    def test_initialize_creates_empty_supported_schemas_without_application(self):
        with patch.object(application, "main", side_effect=AssertionError("Application called")), \
                patch.object(application.Runner, "run_sync", side_effect=AssertionError("Agent called")):
            self.launch("--initialize")
        for path, tables in ((self.business, ("candidates", "candidate_evaluations", "evaluation_artifacts")),
                             (self.sessions, ("agent_sessions", "agent_messages"))):
            with contextlib.closing(sqlite3.connect(path)) as connection:
                for table in tables:
                    self.assertEqual(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
        self.assertEqual(self.old_business.read_bytes(), b"protected test fixture, not a real production database")

    def test_default_repository_and_initializer_use_paired_configuration(self):
        self.business.parent.mkdir()
        with patch.dict(os.environ, self.env()):
            initialize_database()
            self.assertEqual(CandidateRepository().database_path, self.business)
        self.assertFalse(self.sessions.exists())

    def test_explicit_test_repository_does_not_use_environment_databases(self):
        path = self.root / "explicit-test.db"
        with patch.dict(os.environ, self.env()):
            self.assertEqual(CandidateRepository(path).database_path, path)
        self.assertFalse(self.business.exists())
        self.assertFalse(self.sessions.exists())

    def test_application_quit_uses_both_explicit_databases_without_model(self):
        self.business.parent.mkdir()
        with patch.dict(os.environ, self.env()), patch.object(application, "load_dotenv"), \
                patch("builtins.input", return_value="quit"), \
                patch.object(application.Runner, "run_sync", side_effect=AssertionError("Agent called")):
            application.main()
        self.assertTrue(self.business.exists())
        self.assertTrue(self.sessions.exists())

    def test_application_partial_configuration_fails_before_sqlite(self):
        with patch.dict(os.environ, {config.BUSINESS_DATABASE_ENV: str(self.business)}), \
                patch.object(application, "load_dotenv"), \
                patch("sqlite3.connect", side_effect=AssertionError("Database opened")), self.assertRaises(ValueError):
            application.main()

    def test_default_repository_and_initializer_refuse_partial_configuration(self):
        for operation in (CandidateRepository, initialize_database):
            with self.subTest(operation=operation.__name__), \
                    patch.dict(os.environ, {config.SESSION_DATABASE_ENV: str(self.sessions)}), \
                    patch("sqlite3.connect", side_effect=AssertionError("Database opened")), self.assertRaises(ValueError):
                operation()

    def test_actual_launcher_quit_passes_paths_to_application(self):
        with patch.object(application, "load_dotenv"), patch("builtins.input", return_value="quit"), \
                patch.object(application.Runner, "run_sync", side_effect=AssertionError("Agent called")):
            self.launch(None)
        self.assertTrue(self.business.exists())
        self.assertTrue(self.sessions.exists())
        self.assertEqual(os.environ["ARBITRAGE_SESSION_ID"], "v2_development")

    def test_launcher_overrides_ambient_paths_and_does_not_start_loop_during_setup(self):
        with patch.dict(os.environ, {config.BUSINESS_DATABASE_ENV: str(self.old_business),
                                     config.SESSION_DATABASE_ENV: str(self.old_sessions), "OPENAI_API_KEY": "test-secret-not-printed"}), \
                patch.object(application, "main") as main:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                v2_dev.main(self.args())
            main.assert_called_once_with()
            self.assertEqual(config.get_database_paths(), config.DatabasePaths(self.business, self.sessions))
            self.assertNotIn("test-secret-not-printed", output.getvalue())

    def test_same_session_id_does_not_import_another_database_history(self):
        self.business.parent.mkdir()
        old = SQLiteSession("same-id", self.root / "other-session-test.db")
        new = SQLiteSession("same-id", self.sessions)
        try:
            asyncio.run(old.add_items([{"role": "user", "content": "other store history"}]))
            self.assertEqual(asyncio.run(new.get_items()), [])
        finally:
            new.close()
            old.close()


if __name__ == "__main__":
    unittest.main()
