"""Human-only selection tests: temporary databases, no model execution."""

import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from arbitrage import application, persistence
from arbitrage.candidate_workflow import CandidateWorkflow
from arbitrage.contracts import (
    EvaluationArtifactType, EvaluationSelectionEvent, EvaluationStatus,
    UpdateCandidateEvaluationRequest, WorkflowEvaluationAction,
)
from arbitrage.evaluation_selection import handle_evaluation_command
from arbitrage.persistence import CandidateRepository, EvaluationSelectionRefused
from tests.test_lead_decision_contract import candidate_request


class EvaluationSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "selection.db"
        self.repository = CandidateRepository(self.path)
        self.original_workflow = CandidateWorkflow(self.repository)
        self.started = self.original_workflow.start_candidate_evaluation(candidate_request())
        self.original_workflow.record_human_input("Original hypothetical scenario.")
        # A restarted workflow has neither active IDs nor conversation history.
        self.workflow = CandidateWorkflow(self.repository)
        self.command = f"/evaluation select {self.started.evaluation_id}"

    def select(self, command=None, *, session_id="fresh-selection-test"):
        return handle_evaluation_command(
            self.workflow, self.command if command is None else command, session_id=session_id,
        )

    def rows(self):
        with closing(sqlite3.connect(self.path)) as connection:
            return {name: connection.execute(f"SELECT * FROM {name} ORDER BY 1").fetchall()
                    for name in ("candidates", "candidate_evaluations", "evaluation_artifacts")}

    def events(self):
        return [a for a in self.repository.list_evaluation_artifacts(self.started.evaluation_id)
                if a.artifact_type == EvaluationArtifactType.EVALUATION_SELECTION]

    def update_raw(self, sql, parameters=()):
        # Deliberately malformed/terminal fixtures are confined to this temporary DB.
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(sql, parameters)

    def assert_refused_without_writes(self, expected_code, command=None):
        before = self.rows()
        active = (self.workflow.active.candidate_id, self.workflow.active.evaluation_id)
        result = self.select(command)
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, expected_code)
        self.assertEqual(before, self.rows())
        self.assertEqual(active, (self.workflow.active.candidate_id, self.workflow.active.evaluation_id))
        return result

    def test_valid_selection_after_restart_reattaches_exact_persisted_ids(self):
        self.assertFalse(self.workflow.active.is_active)
        result = self.select()
        self.assertTrue(result.success, result)
        self.assertFalse(result.already_active)
        self.assertEqual(result.candidate_id, self.started.candidate_id)
        self.assertEqual(result.evaluation_id, self.started.evaluation_id)
        candidate, evaluation = self.workflow._active_records()
        self.assertEqual((candidate.candidate_id, evaluation.evaluation_id),
                         (self.started.candidate_id, self.started.evaluation_id))

    def test_selection_preserves_records_history_and_counts(self):
        before = self.rows()
        self.assertTrue(self.select().success)
        after = self.rows()
        self.assertEqual(before["candidates"], after["candidates"])
        self.assertEqual(before["candidate_evaluations"], after["candidate_evaluations"])
        self.assertEqual(before["evaluation_artifacts"], after["evaluation_artifacts"][:-1])
        self.assertEqual(len(after["evaluation_artifacts"]), len(before["evaluation_artifacts"]) + 1)
        self.assertIsNone(self.repository.get_candidate(self.started.candidate_id).latest_evaluation_id)

    def test_audit_event_is_typed_exact_and_uses_persistence_timestamp(self):
        result = self.select()
        artifact = self.events()[0]
        event = EvaluationSelectionEvent.model_validate_json(artifact.payload_json)
        self.assertEqual(event.action, "EVALUATION_SELECTED")
        self.assertEqual(event.source, "HUMAN_TERMINAL")
        self.assertEqual(event.human_command, self.command)
        self.assertEqual(event.evaluation_id, self.started.evaluation_id)
        self.assertEqual(event.candidate_id, self.started.candidate_id)
        self.assertEqual(event.session_id, "fresh-selection-test")
        self.assertEqual(result.artifact_id, artifact.artifact_id)
        self.assertEqual(artifact.sequence_number, 2)
        self.assertIsNotNone(artifact.created_at.utcoffset())
        self.assertEqual(self.repository.list_evaluation_artifacts(self.started.evaluation_id)[0].artifact_type,
                         EvaluationArtifactType.HUMAN_INPUT)

    def test_absent_session_id_stays_absent(self):
        self.assertTrue(self.select(session_id=None).success)
        event = EvaluationSelectionEvent.model_validate_json(self.events()[0].payload_json)
        self.assertIsNone(event.session_id)

    def test_invalid_session_provenance_is_not_invented(self):
        for identifier in ("", " ", Mock()):
            before = self.rows()
            result = self.select(session_id=identifier)
            self.assertEqual(result.error_code, "INVALID_SESSION_ID")
            self.assertEqual(before, self.rows())
            self.assertFalse(self.workflow.active.is_active)

    def test_selection_does_not_execute_agents_captures_or_profitability(self):
        with patch("agents.Runner.run") as run, patch("agents.Runner.run_sync") as sync, \
                patch("agents.Runner.run_streamed") as streamed, \
                patch("arbitrage.tools.profitability._calculate_profitability") as arithmetic, \
                patch("arbitrage.evaluation_capture.run_required_acquisition_capture") as acquisition, \
                patch("arbitrage.evaluation_capture.run_required_selling_capture") as selling:
            self.assertTrue(self.select().success)
        for operation in (run, sync, streamed, arithmetic, acquisition, selling):
            operation.assert_not_called()

    def test_in_progress_status_is_not_changed(self):
        result = self.select()
        self.assertEqual(result.evaluation_status, EvaluationStatus.IN_PROGRESS)
        self.assertEqual(self.repository.get_evaluation(self.started.evaluation_id).status, EvaluationStatus.IN_PROGRESS)

    def test_waiting_selection_does_not_resume(self):
        self.original_workflow.update_candidate_evaluation(
            UpdateCandidateEvaluationRequest(action=WorkflowEvaluationAction.WAIT_FOR_INPUT))
        before = self.rows()
        result = self.select()
        self.assertEqual(result.evaluation_status, EvaluationStatus.WAITING_FOR_INPUT)
        self.assertEqual(before["candidate_evaluations"], self.rows()["candidate_evaluations"])
        # Existing explicit lifecycle RESUME still works after reattachment.
        resumed = self.workflow.update_candidate_evaluation(
            UpdateCandidateEvaluationRequest(action=WorkflowEvaluationAction.RESUME))
        self.assertEqual(resumed.evaluation_id, self.started.evaluation_id)
        self.assertEqual(resumed.candidate_id, self.started.candidate_id)
        self.assertEqual(resumed.evaluation_status, EvaluationStatus.IN_PROGRESS)

    def test_unknown_evaluation_id_is_not_substituted(self):
        self.assert_refused_without_writes("EVALUATION_NOT_FOUND", f"/evaluation select {uuid4()}")

    def test_missing_ids_extra_arguments_and_ambiguous_commands_rejected(self):
        for text in ("/evaluation", "/evaluation select", "/evaluation select ",
                     self.command + " extra", self.command + " " + str(uuid4()),
                     self.command + "\n", self.command + " ", self.command.replace(" select ", "  select "),
                     self.command.replace(" select ", "\tselect "), " " + self.command,
                     "\n" + self.command, "/evaluation latest"):
            with self.subTest(command=text):
                self.assert_refused_without_writes("INVALID_COMMAND", text)

    def test_malformed_noncanonical_and_inferred_ids_rejected(self):
        for identifier in ("latest", "only", "not-a-uuid", self.started.evaluation_id.replace("-", ""),
                           self.started.evaluation_id.upper(), "'" + self.started.evaluation_id + "'"):
            with self.subTest(identifier=identifier):
                self.assert_refused_without_writes("INVALID_EVALUATION_ID", f"/evaluation select {identifier}")

    def test_terminal_evaluations_rejected(self):
        for status in ("COMPLETED", "FAILED", "CANCELLED"):
            self.update_raw("UPDATE candidate_evaluations SET status=?, completed_at=? WHERE evaluation_id=?",
                            (status, datetime.now(timezone.utc).isoformat(), self.started.evaluation_id))
            self.assert_refused_without_writes("INELIGIBLE_EVALUATION")

    def test_incompatible_candidate_states_rejected(self):
        for status in ("VIABLE", "REJECTED", "CLOSED"):
            self.update_raw("UPDATE candidates SET lifecycle_status=? WHERE candidate_id=?", (status, self.started.candidate_id))
            self.assert_refused_without_writes("INELIGIBLE_CANDIDATE")

    def test_missing_candidate_rejected_without_repair(self):
        self.update_raw("DELETE FROM candidates WHERE candidate_id=?", (self.started.candidate_id,))
        self.assert_refused_without_writes("CANDIDATE_NOT_FOUND")

    def test_invalid_persisted_relationship_rejected(self):
        original = persistence._candidate_from_row
        def mismatch(row):
            return original(row).model_copy(update={"candidate_id": str(uuid4())})
        with patch.object(persistence, "_candidate_from_row", side_effect=mismatch):
            self.assert_refused_without_writes("RECORD_MISMATCH")

    def test_corrupt_persisted_data_rejected_without_repair(self):
        self.update_raw("UPDATE candidates SET product_identity_json=? WHERE candidate_id=?",
                        ("invalid json", self.started.candidate_id))
        self.assert_refused_without_writes("INVALID_PERSISTED_DATA")

    def test_different_active_investigation_is_not_replaced(self):
        another = CandidateWorkflow(self.repository)
        other = another.start_candidate_evaluation(candidate_request())
        self.workflow.active = another.active
        self.assert_refused_without_writes("ACTIVE_CONFLICT")
        self.assertEqual(self.workflow.active.evaluation_id, other.evaluation_id)

    def test_different_evaluation_for_same_active_candidate_is_not_replaced(self):
        other = self.repository.create_evaluation(self.started.candidate_id, trigger=candidate_request().trigger)
        self.workflow.active = self.original_workflow.active
        self.assert_refused_without_writes("ACTIVE_CONFLICT", f"/evaluation select {other.evaluation_id}")

    def test_partial_active_state_is_not_silently_repaired(self):
        self.workflow.active.candidate_id = self.started.candidate_id
        self.assert_refused_without_writes("INVALID_ACTIVE_STATE")

    def test_duplicate_selection_is_idempotent_and_does_not_append(self):
        self.assertTrue(self.select().success)
        before = self.rows()
        repeated = self.select()
        self.assertTrue(repeated.success)
        self.assertTrue(repeated.already_active)
        self.assertIsNone(repeated.artifact_id)
        self.assertEqual(before, self.rows())
        self.assertEqual(len(self.events()), 1)

    def test_concurrent_same_workflow_selection_is_idempotent(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: self.select(), range(2)))
        self.assertTrue(all(result.success for result in results))
        self.assertEqual(sum(result.already_active for result in results), 1)
        self.assertEqual(len(self.events()), 1)

    def test_concurrent_different_selections_do_not_replace_each_other(self):
        other = CandidateWorkflow(self.repository).start_candidate_evaluation(candidate_request())
        commands = [self.command, f"/evaluation select {other.evaluation_id}"]
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(self.select, commands))
        accepted = [result for result in results if result.success]
        refused = [result for result in results if not result.success]
        self.assertEqual(len(accepted), 1)
        self.assertEqual(refused[0].error_code, "ACTIVE_CONFLICT")
        self.assertEqual(self.workflow.active.evaluation_id, accepted[0].evaluation_id)
        with closing(sqlite3.connect(self.path)) as connection:
            count = connection.execute("SELECT COUNT(*) FROM evaluation_artifacts WHERE artifact_type='EVALUATION_SELECTION'").fetchone()[0]
        self.assertEqual(count, 1)

    def test_repeated_selection_revalidates_terminal_target(self):
        self.assertTrue(self.select().success)
        self.update_raw("UPDATE candidate_evaluations SET status='FAILED',completed_at=? WHERE evaluation_id=?",
                        (datetime.now(timezone.utc).isoformat(), self.started.evaluation_id))
        self.assert_refused_without_writes("INELIGIBLE_EVALUATION")

    def test_sqlite_append_failure_preserves_memory_and_database(self):
        with patch.object(self.repository, "_append_selection_event", side_effect=sqlite3.OperationalError("test write failure")):
            self.assert_refused_without_writes("STORAGE_FAILURE")

    def test_failure_after_insert_rolls_back_audit(self):
        append = self.repository._append_selection_event
        def fail_after_insert(*args, **kwargs):
            append(*args, **kwargs)
            raise sqlite3.OperationalError("test failure after pending insert")
        with patch.object(self.repository, "_append_selection_event", side_effect=fail_after_insert):
            self.assert_refused_without_writes("STORAGE_FAILURE")

    def test_commit_failure_restores_memory_and_rolls_back_audit(self):
        @contextmanager
        def commit_failure(path):
            connection = sqlite3.connect(path)
            connection.row_factory = sqlite3.Row
            try:
                yield connection
                self.assertTrue(self.workflow.active.is_active)
                raise sqlite3.OperationalError("test commit failure")
            finally:
                connection.rollback()
                connection.close()
        with patch.object(persistence, "_connection", commit_failure):
            self.assert_refused_without_writes("STORAGE_FAILURE")

    def test_activation_failure_restores_memory_and_rolls_back_audit(self):
        def partial_failure(candidate_id, evaluation_id):
            self.workflow.active.candidate_id = candidate_id
            raise RuntimeError("test unexpected activation error")
        with patch.object(self.workflow, "_activate_selection", side_effect=partial_failure):
            self.assert_refused_without_writes("ACTIVATION_FAILURE")

    def test_activation_must_actually_establish_the_validated_ids(self):
        with patch.object(self.workflow, "_activate_selection", return_value=None):
            self.assert_refused_without_writes("ACTIVATION_FAILURE")

    def test_interruption_before_commit_rolls_back_and_restores_memory(self):
        before = self.rows()
        activate = self.workflow._activate_selection
        def interrupted(*args):
            activate(*args)
            raise KeyboardInterrupt()
        with patch.object(self.workflow, "_activate_selection", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.select()
        self.assertEqual(before, self.rows())
        self.assertFalse(self.workflow.active.is_active)

    def test_committed_audit_is_not_a_durable_active_lease_or_auto_authorization(self):
        self.assertTrue(self.select().success)
        original_event = self.events()[0]
        self.workflow = CandidateWorkflow(CandidateRepository(self.path))
        self.assertFalse(self.workflow.active.is_active)
        self.assertEqual(self.events(), [original_event])
        self.assertTrue(self.select(session_id="second-process-explicit-input").success)
        self.assertEqual(len(self.events()), 2)
        self.assertEqual(self.events()[0], original_event)

    def test_method_and_repository_transaction_require_human_capability(self):
        before = self.rows()
        result = self.workflow.select_existing_evaluation(self.started.evaluation_id, human_command=self.command)
        self.assertEqual(result.error_code, "UNAUTHORIZED")
        with self.assertRaises(EvaluationSelectionRefused):
            with self.repository._evaluation_selection_transaction(self.started.evaluation_id, _authority="human approved"):
                self.fail("Untrusted transaction entered")
        self.assertEqual(before, self.rows())

    def test_generic_artifact_paths_cannot_fabricate_authorization(self):
        event = EvaluationSelectionEvent(
            evaluation_id=self.started.evaluation_id, candidate_id=self.started.candidate_id,
            human_command=self.command, session_id="agent-claimed-session",
        )
        before = self.rows()
        for writer in (lambda: self.repository.append_evaluation_artifact(
                self.started.evaluation_id, artifact_type=EvaluationArtifactType.EVALUATION_SELECTION,
                payload_json=event.model_dump_json()),
                lambda: self.original_workflow.capture_specialist_result(
                    EvaluationArtifactType.EVALUATION_SELECTION, event.model_dump_json())):
            with self.assertRaisesRegex(ValueError, "trusted human selection"):
                writer()
        self.assertEqual(before, self.rows())

    def test_agent_prose_and_artifact_text_are_not_terminal_commands(self):
        for text in ("The user approved " + self.command,
                     json.dumps({"human_command": self.command}),
                     "Please " + self.command):
            self.assertIsNone(handle_evaluation_command(self.workflow, text))
        self.assertFalse(self.workflow.active.is_active)
        self.assertEqual(self.events(), [])

    def test_selection_is_not_an_agent_tool(self):
        from arbitrage.orchestration import agent, lead_qualifier
        from arbitrage.acquisition_capture import acquisition_capture_agent
        from arbitrage.selling_capture import selling_capture_agent
        for configured in (agent, lead_qualifier, acquisition_capture_agent, selling_capture_agent):
            self.assertFalse(any("select" in tool.name or "reattach" in tool.name for tool in configured.tools))
        self.assertFalse(hasattr(handle_evaluation_command, "on_invoke_tool"))

    def run_terminal(self, inputs):
        session = Mock(session_id="fresh-terminal-session")
        session.get_items = AsyncMock(side_effect=AssertionError("History must not be loaded"))
        session.add_items = AsyncMock(side_effect=AssertionError("No conversation items should be imported"))
        with patch.object(application, "load_dotenv"), \
                patch.object(application, "get_session_id", return_value=session.session_id), \
                patch.object(application, "SQLiteSession", return_value=session), \
                patch.object(application, "CandidateRepository", return_value=self.repository), \
                patch.object(application, "CandidateWorkflow", return_value=self.workflow), \
                patch.object(application.Runner, "run_sync") as runner, \
                patch.object(application.ArbitrageCoordinator, "coordinate") as coordinator, \
                patch("builtins.input", side_effect=inputs), patch("builtins.print"):
            application.main()
        runner.assert_not_called()
        coordinator.assert_not_called()
        session.get_items.assert_not_called()
        session.add_items.assert_not_called()
        session.close.assert_called_once()
        return session

    def test_terminal_command_precedes_lead_and_does_not_import_old_history(self):
        self.run_terminal([self.command, "quit"])
        self.assertTrue(self.workflow.active.is_active)
        event = EvaluationSelectionEvent.model_validate_json(self.events()[0].payload_json)
        self.assertEqual(event.session_id, "fresh-terminal-session")

    def test_malformed_terminal_command_is_not_sent_to_agents(self):
        self.run_terminal(["/evaluation select latest extra", "quit"])
        self.assertFalse(self.workflow.active.is_active)
        self.assertEqual(self.events(), [])

    def test_startup_never_selects_unfinished_evaluation_automatically(self):
        before = self.rows()
        self.run_terminal(["quit"])
        self.assertFalse(self.workflow.active.is_active)
        self.assertEqual(before, self.rows())

    def test_existing_identity_interface_and_evidence_remain_unchanged(self):
        self.assertTrue(self.select().success)
        self.assertEqual(self.workflow._active_records(), self.original_workflow._active_records())
        self.assertEqual(self.workflow.economic_evidence_snapshot().identity_resolutions, ())
        # No identity command or proposal is needed to exercise the active interface.
        self.assertEqual([a.artifact_type for a in self.repository.list_evaluation_artifacts(self.started.evaluation_id)],
                         [EvaluationArtifactType.HUMAN_INPUT, EvaluationArtifactType.EVALUATION_SELECTION])

    def test_selection_audit_does_not_change_economic_evidence_selection(self):
        from tests.acquisition_readiness_fixtures import append_economic_readiness
        append_economic_readiness(self.repository, self.started.evaluation_id)
        before = self.repository.economic_evidence_snapshot(self.started.evaluation_id)
        self.assertTrue(self.select().success)
        self.assertEqual(before, self.workflow.economic_evidence_snapshot())

    def test_blocked_evaluation_can_be_selected_without_replaying_failed_work(self):
        from arbitrage.contracts import ProfitabilityToolResult
        failure = self.repository.append_evaluation_artifact(
            self.started.evaluation_id, artifact_type=EvaluationArtifactType.PROFITABILITY,
            payload_json=ProfitabilityToolResult(success=False, result=None, error="IDENTITY_CONFLICT").model_dump_json(),
        )
        self.assertTrue(self.select().success)
        artifacts = self.repository.list_evaluation_artifacts(self.started.evaluation_id)
        self.assertEqual(artifacts[-2], failure)
        self.assertEqual(sum(a.artifact_type == EvaluationArtifactType.PROFITABILITY for a in artifacts), 1)
        self.assertIsNone(self.repository.get_evaluation(self.started.evaluation_id).profitability_result)

    def test_concurrent_terminal_status_change_is_seen_before_selection(self):
        connected = threading.Event()
        connect = persistence._connect
        def mark_connection(path):
            connection = connect(path)
            connected.set()
            return connection
        writer = sqlite3.connect(self.path)
        self.addCleanup(writer.close)
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE candidate_evaluations SET status='FAILED',completed_at=? WHERE evaluation_id=?",
                       (datetime.now(timezone.utc).isoformat(), self.started.evaluation_id))
        with ThreadPoolExecutor(max_workers=1) as executor, patch.object(persistence, "_connect", side_effect=mark_connection):
            future = executor.submit(self.select)
            self.assertTrue(connected.wait(2))
            writer.commit()
            result = future.result(timeout=3)
        self.assertEqual(result.error_code, "INELIGIBLE_EVALUATION")
        self.assertFalse(self.workflow.active.is_active)
        self.assertEqual(self.events(), [])
        self.assertEqual(self.repository.get_evaluation(self.started.evaluation_id).status, EvaluationStatus.FAILED)

    def test_status_writes_cannot_interleave_with_validation_and_activation(self):
        activate = self.workflow._activate_selection
        def checked_activation(*args):
            with closing(sqlite3.connect(self.path, timeout=0)) as other:
                with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                    other.execute("UPDATE candidate_evaluations SET status='WAITING_FOR_INPUT' WHERE evaluation_id=?",
                                  (self.started.evaluation_id,))
            activate(*args)
        with patch.object(self.workflow, "_activate_selection", side_effect=checked_activation):
            self.assertTrue(self.select().success)
        self.assertEqual(self.repository.get_evaluation(self.started.evaluation_id).status, EvaluationStatus.IN_PROGRESS)
