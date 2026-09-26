import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agents import SQLiteSession

from arbitrage.application import (
    DEFAULT_SESSION_ID,
    SESSION_ID_ENVIRONMENT_VARIABLE,
    SESSIONS_DATABASE_PATH,
    get_session_id,
    qualification_input,
    record_application_response,
)
from arbitrage.persistence import CandidateRepository


class ApplicationConfigurationTests(unittest.TestCase):
    def test_default_session_id_is_unchanged(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(SESSION_ID_ENVIRONMENT_VARIABLE, None)
            self.assertEqual(get_session_id(), DEFAULT_SESSION_ID)

    def test_session_id_can_be_overridden_without_changing_database_paths(self):
        with patch.dict(
            os.environ,
            {SESSION_ID_ENVIRONMENT_VARIABLE: "v1b_test_2"},
            clear=False,
        ):
            self.assertEqual(get_session_id(), "v1b_test_2")

        project_root = Path(__file__).resolve().parent.parent
        self.assertEqual(SESSIONS_DATABASE_PATH, project_root / "sessions.db")
        self.assertEqual(CandidateRepository().database_path, project_root / "arbitrage.db")

    def test_qualification_uses_history_without_persisting_internal_input(self):
        with tempfile.TemporaryDirectory() as directory:
            session = SQLiteSession("test", Path(directory) / "sessions.db")
            try:
                asyncio.run(
                    session.add_items(
                        [
                            {"role": "user", "content": "I found shoes at Ross."},
                            {"role": "assistant", "content": "What is the style number?"},
                        ]
                    )
                )
                before = asyncio.run(session.get_items())
                input_items = qualification_input(
                    session,
                    "FD2722-001, size 10.",
                    "No durable Candidate or Evaluation is active.",
                )
                self.assertEqual(asyncio.run(session.get_items()), before)
                self.assertEqual(input_items[:-1], before)
                self.assertIn("FD2722-001", input_items[-1]["content"])
                self.assertIn("Application-owned", input_items[-1]["content"])
            finally:
                session.close()

    def test_application_owned_response_is_saved_as_user_visible_exchange(self):
        with tempfile.TemporaryDirectory() as directory:
            session = SQLiteSession("test", Path(directory) / "sessions.db")
            try:
                record_application_response(session, "Some shoes", "What model?")
                self.assertEqual(
                    asyncio.run(session.get_items()),
                    [
                        {"role": "user", "content": "Some shoes"},
                        {"role": "assistant", "content": "What model?"},
                    ],
                )
            finally:
                session.close()


if __name__ == "__main__":
    unittest.main()
