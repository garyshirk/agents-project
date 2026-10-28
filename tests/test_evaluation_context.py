"""Temporary persisted snapshots only; inspection must never execute agents."""

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from arbitrage import application, persistence
from arbitrage.candidate_workflow import CandidateWorkflow
from arbitrage.contracts import (
    EvaluationArtifactType as Kind, EvaluationSelectionEvent, EvaluationStatus,
    IdentityResolution, IdentityResolutionStatus, ProductPackaging,
    ProfitabilityToolResult, ResaleResult, SourcingTextReport,
    UpdateCandidateEvaluationRequest, WorkflowEvaluationAction,
)
from arbitrage.evaluation_context import (
    Applicability, BlockerState, EvaluationContext, build_evaluation_context,
    handle_evaluation_context_command, render_evaluation_context,
)
from arbitrage.evaluation_selection import handle_evaluation_command
from arbitrage.identity_resolution import identity_digest, ORIGIN
from arbitrage.persistence import CandidateRepository, _IDENTITY_RESOLUTION_AUTHORITY
from arbitrage.profitability_request_assembly import assemble_profitability_request
from arbitrage.tools.profitability import _calculate_profitability
from tests import test_profitability_request_assembly as fixtures
from tests.test_lead_decision_contract import candidate_request


class EvaluationContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "context.db"
        self.repo = CandidateRepository(self.path)
        starter = CandidateWorkflow(self.repo)
        self.started = starter.start_candidate_evaluation(candidate_request())
        starter.record_human_input("Hypothetical scenario; no inventory observed.")
        self.fixture = fixtures.ProfitabilityRequestAssemblyTests()
        self.append(Kind.SOURCING_REPORT, SourcingTextReport(report_text="Exact source report."))
        self.append(Kind.ACQUISITION_COST_FINDINGS, self.fixture.acquisition_findings())
        self.resale = self.fixture.resale()
        self.write_resale()
        self.workflow = CandidateWorkflow(self.repo)
        self.select()

    def select(self):
        result = handle_evaluation_command(self.workflow, f"/evaluation select {self.started.evaluation_id}", session_id="fresh-context-test")
        self.assertTrue(result.success, result)

    def append(self, kind, value, metadata=None, authority=None):
        return self.repo.append_evaluation_artifact(
            self.started.evaluation_id, artifact_type=kind, payload_json=value.model_dump_json(),
            context_json=json.dumps(metadata) if metadata is not None else None, _identity_authority=authority,
        )

    def write_resale(self, target=True):
        candidate = self.repo.get_candidate(self.started.candidate_id)
        metadata = {"authoritative_target": {"origin": "active_persisted_candidate", "candidate_id": candidate.candidate_id,
                     "evaluation_id": self.started.evaluation_id, "product_identity": candidate.product_identity.model_dump(mode="json")}} if target else None
        self.append(Kind.RESALE, self.resale, metadata)
        self.append(Kind.SELLING_COST_FINDINGS, self.fixture.selling_findings())

    def inspect(self):
        return handle_evaluation_context_command(self.workflow, "/evaluation context")

    def context(self):
        result = self.inspect()
        self.assertTrue(result.success, result)
        return result.context

    def rows(self):
        with closing(sqlite3.connect(self.path)) as connection:
            return {name: connection.execute(f"SELECT * FROM {name} ORDER BY 1").fetchall()
                    for name in ("candidates", "candidate_evaluations", "evaluation_artifacts")}

    def raw(self, sql, params=()):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(sql, params)

    def assert_read_only_error(self, code):
        before = self.rows()
        pair = (self.workflow.active.candidate_id, self.workflow.active.evaluation_id)
        result = self.inspect()
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, code)
        self.assertEqual(self.rows(), before)
        self.assertEqual(pair, (self.workflow.active.candidate_id, self.workflow.active.evaluation_id))

    def findings(self, state):
        return [item.category for item in self.context().findings if item.state == state]

    def conflict(self):
        self.resale.researched_identity.material_variant.colorway = "Grey/Black/White"
        self.write_resale()

    def resolution(self, status=IdentityResolutionStatus.CONFIRMED):
        # Fixtures are persisted directly in this temporary DB; no identity command executes.
        candidate = self.repo.get_candidate(self.started.candidate_id)
        evidence = self.repo.economic_evidence_snapshot(self.started.evaluation_id)
        record = IdentityResolution(
            candidate_id=candidate.candidate_id, evaluation_id=self.started.evaluation_id,
            attribute_path="material_variant.colorway", candidate_value=candidate.product_identity.material_variant.colorway,
            resale_value=self.resale.researched_identity.material_variant.colorway,
            candidate_identity_sha256=identity_digest(candidate.product_identity),
            resale_identity_sha256=identity_digest(self.resale.researched_identity),
            resale_artifact_id=evidence.resale.artifact_id, resale_sequence=evidence.resale.sequence_number,
            supporting_evidence=[dict(url="https://example.com/evidence", description="Fixture evidence only")],
            explanation="Explicit fixture equivalence, not independent product verification.",
            proposed_by="APPLICATION_FROM_RESALE_EVIDENCE", status="PROPOSED",
            proposal_artifact_id=None, human_command="/identity propose colorway",
        )
        proposal = self.append(Kind.IDENTITY_RESOLUTION, record, {"origin": ORIGIN}, _IDENTITY_RESOLUTION_AUTHORITY)
        if status != IdentityResolutionStatus.PROPOSED:
            record = record.model_copy(update={"status": status, "proposal_artifact_id": proposal.artifact_id,
                "human_command": f"/identity {'confirm' if status == IdentityResolutionStatus.CONFIRMED else 'reject'} {proposal.artifact_id}"})
            self.append(Kind.IDENTITY_RESOLUTION, record, {"origin": ORIGIN}, _IDENTITY_RESOLUTION_AUTHORITY)
        return proposal

    def failure(self, metadata=None):
        return self.append(Kind.PROFITABILITY, ProfitabilityToolResult(success=False, result=None, error="IDENTITY_CONFLICT"), metadata)

    def success(self, provenance=True):
        # Prepare real historical contract shapes using existing deterministic code.
        candidate, evaluation = self.workflow._active_records()
        evidence = self.repo.economic_evidence_snapshot(evaluation.evaluation_id)
        assembly = assemble_profitability_request(candidate, evaluation, evidence)
        self.assertTrue(assembly.success, assembly.failures)
        result = _calculate_profitability(assembly.request)
        metadata = {"request": assembly.request.model_dump(mode="json"), "result": result.model_dump(mode="json"),
                    "provenance": assembly.provenance.model_dump(mode="json")} if provenance else None
        return self.append(Kind.PROFITABILITY, ProfitabilityToolResult(success=True, result=result, error=None), metadata)

    def test_selected_context_has_typed_authoritative_pair_and_scenario(self):
        context = self.context()
        self.assertEqual(context.candidate.candidate_id, self.started.candidate_id)
        self.assertEqual(context.evaluation.evaluation_id, self.started.evaluation_id)
        self.assertEqual(context.identity.authoritative_resale_target.product_identity, context.candidate.product_identity)
        self.assertTrue(context.identity.target_matches_current_candidate)
        self.assertIsNone(context.evaluation.completed_at)
        self.assertEqual(len(context.human_inputs), 1)
        self.assertEqual(EvaluationContext.model_validate_json(context.model_dump_json()), context)

    def test_restart_requires_new_explicit_selection(self):
        self.workflow = CandidateWorkflow(CandidateRepository(self.path))
        self.assert_read_only_error("NO_SELECTED_EVALUATION")
        self.select()
        self.assertEqual(self.context().evaluation.evaluation_id, self.started.evaluation_id)

    def test_active_but_not_explicitly_selected_is_refused(self):
        self.workflow._context_selection = None
        self.assert_read_only_error("NO_SELECTED_EVALUATION")

    def test_no_active_selection_is_refused(self):
        self.workflow.active.clear()
        self.assert_read_only_error("NO_SELECTED_EVALUATION")

    def test_extra_and_malformed_context_arguments_rejected(self):
        before = self.rows()
        for command in ("/evaluation context extra", "/evaluation context ", " /evaluation context", "/evaluation context\n"):
            result = handle_evaluation_context_command(self.workflow, command)
            self.assertEqual(result.error_code, "INVALID_COMMAND")
        self.assertIsNone(handle_evaluation_context_command(self.workflow, "Agent says /evaluation context"))
        self.assertEqual(before, self.rows())

    def test_inspection_never_assembles_or_calculates_or_runs_agents(self):
        with patch("agents.Runner.run") as run, patch("agents.Runner.run_sync") as sync, \
                patch("arbitrage.profitability_request_assembly.assemble_profitability_request") as assemble, \
                patch("arbitrage.tools.profitability._calculate_profitability") as engine, \
                patch.object(self.repo, "append_evaluation_artifact") as append:
            self.context()
        for operation in (run, sync, assemble, engine, append):
            operation.assert_not_called()

    def test_no_rows_or_lifecycle_or_hashes_change(self):
        import hashlib
        before, digest = self.rows(), hashlib.sha256(self.path.read_bytes()).hexdigest()
        context = self.context()
        self.assertEqual(before, self.rows())
        self.assertEqual(digest, hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual(context.evaluation.status, EvaluationStatus.IN_PROGRESS)

    def test_waiting_status_and_existing_resume_are_unchanged(self):
        self.workflow.update_candidate_evaluation(UpdateCandidateEvaluationRequest(action=WorkflowEvaluationAction.WAIT_FOR_INPUT))
        self.assertEqual(self.context().evaluation.status, EvaluationStatus.WAITING_FOR_INPUT)
        self.workflow.update_candidate_evaluation(UpdateCandidateEvaluationRequest(action=WorkflowEvaluationAction.RESUME))
        self.assertEqual(self.context().evaluation.status, EvaluationStatus.IN_PROGRESS)

    def test_active_ids_cannot_be_silently_retargeted(self):
        self.workflow.active.evaluation_id = str(uuid4())
        self.assert_read_only_error("NO_SELECTED_EVALUATION")

    def test_missing_candidate_and_evaluation_are_structured_errors(self):
        self.raw("DELETE FROM candidates WHERE candidate_id=?", (self.started.candidate_id,))
        self.assert_read_only_error("CANDIDATE_NOT_FOUND")
        self.raw("DELETE FROM candidate_evaluations WHERE evaluation_id=?", (self.started.evaluation_id,))
        self.assert_read_only_error("EVALUATION_NOT_FOUND")

    def test_relationship_mismatch_is_not_repaired(self):
        self.raw("UPDATE candidate_evaluations SET candidate_id=? WHERE evaluation_id=?", (str(uuid4()), self.started.evaluation_id))
        self.assert_read_only_error("RECORD_MISMATCH")

    def test_terminal_or_changed_lifecycle_is_refused(self):
        self.raw("UPDATE candidate_evaluations SET status='FAILED',completed_at=? WHERE evaluation_id=?",
                 (datetime.now(timezone.utc).isoformat(), self.started.evaluation_id))
        self.assert_read_only_error("INELIGIBLE_EVALUATION")

    def test_changed_candidate_status_is_refused(self):
        self.raw("UPDATE candidates SET lifecycle_status='CLOSED' WHERE candidate_id=?", (self.started.candidate_id,))
        self.assert_read_only_error("INELIGIBLE_CANDIDATE")

    def test_artifact_inventory_is_ordered_and_complete(self):
        context = self.context()
        artifacts = self.repo.list_evaluation_artifacts(self.started.evaluation_id)
        self.assertEqual([item.reference.artifact_id for item in context.artifact_inventory], [item.artifact_id for item in artifacts])
        self.assertEqual([item.reference.sequence_number for item in context.artifact_inventory], sorted(item.sequence_number for item in artifacts))
        self.assertTrue(all(item.created_at.tzinfo is not None for item in context.artifact_inventory))

    def test_latest_selection_and_exact_references_use_existing_sequence_rules(self):
        new_report = self.append(Kind.SOURCING_REPORT, SourcingTextReport(report_text="Replacement research."))
        context = self.context()
        selected = {item.artifact_type: item for item in context.evidence.selected}
        self.assertEqual(selected[Kind.SOURCING_REPORT].artifact_id, new_report.artifact_id)
        self.assertFalse(context.evidence.acquisition_ready)
        self.assertTrue(context.evidence.selling_ready)
        self.assertIn("STALE_CAPTURE", self.findings(BlockerState.ESTABLISHED_BLOCKER))

    def test_missing_research_and_capture_are_distinct(self):
        self.raw("DELETE FROM evaluation_artifacts WHERE artifact_type IN ('SOURCING_REPORT','SELLING_COST_FINDINGS')")
        categories = self.findings(BlockerState.ESTABLISHED_BLOCKER)
        self.assertIn("MISSING_RESEARCH", categories)
        self.assertIn("MISSING_CAPTURE", categories)

    def test_cross_evaluation_artifacts_are_not_selected(self):
        other = CandidateWorkflow(self.repo).start_candidate_evaluation(candidate_request())
        self.repo.append_evaluation_artifact(other.evaluation_id, artifact_type=Kind.SOURCING_REPORT,
                                            payload_json=SourcingTextReport(report_text="Unrelated.").model_dump_json())
        self.assertTrue(all(item.evaluation_id == self.started.evaluation_id for item in self.context().artifact_inventory))

    def test_strict_colorway_conflict_and_width_packaging_gaps(self):
        self.resale.researched_identity.material_variant.colorway = "Grey/Black/White"
        self.resale.researched_identity.material_variant.width = "D - Medium"
        self.resale.researched_identity.material_variant.packaging = None
        self.write_resale()
        identity = self.context().identity
        self.assertEqual(identity.assessment.conflicting_fields, ["material_variant.colorway"])
        self.assertEqual(identity.one_sided_missing, ["material_variant.width", "material_variant.packaging"])
        self.assertIn("IDENTITY_CONFLICT", self.findings(BlockerState.ESTABLISHED_BLOCKER))
        self.assertIn("INSUFFICIENT_IDENTITY_EVIDENCE", self.findings(BlockerState.ESTABLISHED_BLOCKER))

    def test_both_absent_optional_attributes_are_unknown_not_conflicting(self):
        identity = self.context().identity
        self.assertIn("upc_gtin", identity.both_absent)
        self.assertIn("upc_gtin", identity.nonblocking_unknowns)
        self.assertTrue(identity.assessment.accepted)

    def test_applicable_colorway_confirmation_uses_shared_rules(self):
        self.conflict()
        self.resolution()
        identity = self.context().identity
        self.assertTrue(identity.assessment.accepted)
        self.assertEqual(identity.assessment.human_confirmed_fields, ["material_variant.colorway"])
        self.assertEqual(sum(item.confirmation_applicable for item in identity.resolutions), 1)
        self.assertEqual(identity.assessment.conflicting_fields, ["material_variant.colorway"])

    def test_new_resale_artifact_invalidates_confirmation(self):
        self.conflict()
        self.resolution()
        self.write_resale()
        identity = self.context().identity
        self.assertFalse(identity.assessment.accepted)
        self.assertFalse(any(item.confirmation_applicable for item in identity.resolutions))

    def test_superseded_and_rejected_proposals_do_not_authorize(self):
        self.conflict()
        self.resolution()
        self.resolution(IdentityResolutionStatus.PROPOSED)
        self.assertFalse(self.context().identity.assessment.accepted)
        self.resolution(IdentityResolutionStatus.REJECTED)
        self.assertFalse(self.context().identity.assessment.accepted)
        self.assertIn("REJECTED", self.context().identity.resolution_summary)

    def test_confirmation_does_not_hide_other_missing_attributes(self):
        self.resale.researched_identity.material_variant.width = "D"
        self.conflict()
        self.resolution()
        self.assertIn("material_variant.width", self.context().identity.assessment.blocking_fields)

    def test_profitability_attempts_preserve_order_stages_and_categories(self):
        first = self.failure({"failure_stage": "request_assembly", "assembly_failures": [{"code": "IDENTITY_CONFLICT"}]})
        success = self.success()
        last = self.failure({"failure_stage": "tool_execution", "error_type": "ValueError"})
        history = self.context().profitability
        self.assertEqual([item.reference.artifact_id for item in history.attempts], [first.artifact_id, success.artifact_id, last.artifact_id])
        self.assertEqual(history.attempts[0].recorded_failure_categories, ["IDENTITY_CONFLICT"])
        self.assertEqual(history.attempts[-1].recorded_failure_stage, "tool_execution")
        self.assertEqual(history.attempts[-1].recorded_error_type, "ValueError")
        self.assertTrue(history.historical_success_exists)
        self.assertIsNotNone(history.attempts[1].stored_request)
        self.assertIsNotNone(history.attempts[1].stored_context_result)

    def test_historical_success_is_never_automatically_certified_current(self):
        self.success()
        history = self.context().profitability
        self.assertTrue(history.historical_success_exists)
        self.assertEqual(history.current_success_applicability, Applicability.UNDETERMINED)

    def test_stale_success_is_not_current_when_recorded_inputs_differ(self):
        self.success()
        self.write_resale()
        history = self.context().profitability
        self.assertTrue(history.historical_success_exists)
        self.assertEqual(history.current_success_applicability, Applicability.NOT_CURRENT)

    def test_missing_success_provenance_is_undetermined(self):
        self.success(provenance=False)
        self.assertEqual(self.context().profitability.current_success_applicability, Applicability.UNDETERMINED)

    def test_failed_attempt_without_full_provenance_is_not_success(self):
        self.failure()
        history = self.context().profitability
        self.assertFalse(history.historical_success_exists)
        self.assertEqual(history.attempts[0].applicability, Applicability.NOT_CURRENT)
        self.assertIsNone(history.attempts[0].recorded_failure_stage)

    def test_actual_result_cache_is_preserved_not_reinterpreted_as_current(self):
        success = self.success()
        cached = self.repo.get_evaluation(self.started.evaluation_id).profitability_result
        self.failure()
        context = self.context()
        self.assertEqual(context.profitability.persisted_result_cache, cached)
        self.assertIn(success.artifact_id, context.profitability.cache_matching_attempt_ids)
        self.assertNotEqual(context.profitability.current_success_applicability, Applicability.CURRENTLY_ESTABLISHED)

    def test_identity_blocks_later_guards_without_calling_them(self):
        self.conflict()
        with patch("arbitrage.evaluation_context.validate_outbound_shipping") as shipping, \
                patch("arbitrage.evaluation_context.validate_marketplace_fee_scope") as fee:
            context = self.context()
        shipping.assert_not_called()
        fee.assert_not_called()
        self.assertIn("SHIPPING_COVERAGE", context.guards_not_evaluated)
        self.assertIn("MARKETPLACE_FEE_SCOPE", context.guards_not_evaluated)

    def test_material_unknowns_are_potential_without_engine_execution(self):
        self.conflict()
        self.assertIn("MATERIAL_ECONOMIC_UNKNOWN", self.findings(BlockerState.POTENTIAL_BLOCKER))
        self.assertNotIn("MATERIAL_ECONOMIC_UNKNOWN", self.findings(BlockerState.ESTABLISHED_BLOCKER))

    def test_shipping_guard_can_be_independently_assessed_without_arithmetic(self):
        selling = self.fixture.selling_findings()
        selling.findings = [finding for finding in selling.findings if finding.cost_id != "seller_paid_outbound_shipping"]
        self.append(Kind.SELLING_COST_FINDINGS, selling)
        self.assertIn("SHIPPING_COVERAGE", self.findings(BlockerState.ESTABLISHED_BLOCKER))
        self.assertIn("MARKETPLACE_FEE_SCOPE", self.context().guards_not_evaluated)

    def test_fee_guard_uses_existing_policy_only(self):
        selling = self.fixture.selling_findings()
        selling.findings.append(self.fixture.finding("payment_processing_charge", modeled="2.5", unresolved=None))
        self.append(Kind.SELLING_COST_FINDINGS, selling)
        self.assertIn("FEE_SCOPE_UNRESOLVED", self.findings(BlockerState.ESTABLISHED_BLOCKER))

    def test_historical_optional_identity_and_fee_fields_remain_readable(self):
        evidence = self.repo.economic_evidence_snapshot(self.started.evaluation_id)
        payload = json.loads(evidence.resale.payload_json)
        payload["researched_identity"].pop("material_variant")
        for item in payload["evidence_items"]:
            item.pop("source_kind", None)
            item.pop("source_name", None)
        self.raw("UPDATE evaluation_artifacts SET payload_json=?,context_json=NULL WHERE artifact_id=?",
                 (json.dumps(payload), evidence.resale.artifact_id))
        context = self.context()
        self.assertIsNone(context.identity.researched_identity.material_variant)
        self.assertIsNone(context.identity.authoritative_resale_target)
        self.assertFalse(context.identity.assessment.accepted)

    def test_malformed_required_artifact_fails_without_repair(self):
        evidence = self.repo.economic_evidence_snapshot(self.started.evaluation_id)
        self.raw("UPDATE evaluation_artifacts SET payload_json='not-json' WHERE artifact_id=?", (evidence.resale.artifact_id,))
        self.assert_read_only_error("INVALID_ARTIFACT")

    def test_malformed_context_metadata_is_not_silently_dropped(self):
        evidence = self.repo.economic_evidence_snapshot(self.started.evaluation_id)
        self.raw("UPDATE evaluation_artifacts SET context_json='not-json' WHERE artifact_id=?", (evidence.resale.artifact_id,))
        self.assert_read_only_error("INVALID_ARTIFACT")

    def test_summary_is_bounded_and_does_not_dump_raw_artifact_errors(self):
        self.failure({"failure_stage": "request_assembly"})
        summary = render_evaluation_context(self.context())
        self.assertIn("HUMAN-SUPPLIED", summary)
        self.assertIn("AGENT-REPORTED", summary)
        self.assertIn("APPLICATION-ENFORCED", summary)
        self.assertIn("DETERMINISTICALLY VALIDATED", summary)
        self.assertIn("UNKNOWN / UNVERIFIED", summary)
        self.assertNotIn('"report_text"', summary)
        self.assertLess(len(summary), 5500)

    def test_unchanged_snapshot_has_deterministic_complete_output(self):
        first, second = self.context(), self.context()
        self.assertEqual(first.model_dump_json(), second.model_dump_json())
        self.assertEqual(render_evaluation_context(first), render_evaluation_context(second))

    def test_context_is_not_an_agent_tool_or_prompt_injection(self):
        from arbitrage.orchestration import agent
        self.assertFalse(any("context" in tool.name for tool in agent.tools))
        self.assertFalse(hasattr(handle_evaluation_context_command, "on_invoke_tool"))

    def test_selection_does_not_automatically_reconstruct_context(self):
        with patch("arbitrage.evaluation_context.build_evaluation_context") as builder:
            self.select()
        builder.assert_not_called()

    def test_terminal_context_and_startup_never_call_lead_or_history(self):
        session = Mock(session_id="isolated-context-test", get_items=AsyncMock(), add_items=AsyncMock())
        with patch.object(application, "load_dotenv"), \
                patch.object(application, "SQLiteSession", return_value=session), \
                patch.object(application, "CandidateRepository", return_value=self.repo), \
                patch.object(application, "CandidateWorkflow", return_value=self.workflow), \
                patch.object(application.Runner, "run_sync") as runner, \
                patch.object(application.ArbitrageCoordinator, "coordinate") as coordinate, \
                patch("builtins.input", side_effect=["/evaluation context", "quit"]), patch("builtins.print"):
            application.main()
        runner.assert_not_called()
        coordinate.assert_not_called()
        session.get_items.assert_not_called()
        session.add_items.assert_not_called()

    def test_no_automatic_inspection_on_startup(self):
        session = Mock(session_id="isolated-test")
        with patch.object(application, "load_dotenv"), patch.object(application, "SQLiteSession", return_value=session), \
                patch.object(application, "CandidateRepository", return_value=self.repo), \
                patch("builtins.input", side_effect=["quit"]), \
                patch("arbitrage.evaluation_context.build_evaluation_context") as builder:
            application.main()
        builder.assert_not_called()

    def test_read_connection_is_query_only_and_transaction_is_real(self):
        real_connect = sqlite3.connect
        seen = []
        def connect(*args, **kwargs):
            self.assertTrue(kwargs.get("uri"))
            self.assertIn("mode=ro", args[0])
            connection = real_connect(*args, **kwargs)
            def trace(sql):
                if sql.startswith("SELECT * FROM candidates"):
                    seen.append(connection.in_transaction)
                    with self.assertRaises(sqlite3.OperationalError):
                        connection.execute("UPDATE candidates SET notes='forbidden'")
            connection.set_trace_callback(trace)
            return connection
        with patch.object(persistence.sqlite3, "connect", side_effect=connect):
            self.context()
        self.assertEqual(seen, [True])

    def test_wal_concurrent_write_produces_old_consistent_snapshot_not_mixed_data(self):
        with closing(sqlite3.connect(self.path)) as setup:
            setup.execute("PRAGMA journal_mode=WAL")  # Temporary test DB only.
        real_connect = sqlite3.connect
        writer = real_connect(self.path)
        self.addCleanup(writer.close)
        fired = []
        old = self.repo.economic_evidence_snapshot(self.started.evaluation_id).sourcing_report.artifact_id
        old_notes = self.repo.get_candidate(self.started.candidate_id).notes
        def connect(*args, **kwargs):
            connection = real_connect(*args, **kwargs)
            def trace(sql):
                if sql.startswith("SELECT * FROM candidates") and not fired:
                    fired.append(True)
                    writer.execute("UPDATE candidates SET notes='concurrent revision' WHERE candidate_id=?", (self.started.candidate_id,))
                    sequence = writer.execute("SELECT MAX(sequence_number)+1 FROM evaluation_artifacts WHERE evaluation_id=?", (self.started.evaluation_id,)).fetchone()[0]
                    writer.execute("INSERT INTO evaluation_artifacts(evaluation_id,sequence_number,artifact_type,payload_json,context_json,created_at) VALUES (?,?,?,?,?,?)",
                        (self.started.evaluation_id, sequence, Kind.SOURCING_REPORT.value, SourcingTextReport(report_text="New revision").model_dump_json(), None, datetime.now(timezone.utc).isoformat()))
                    writer.commit()
            connection.set_trace_callback(trace)
            return connection
        with patch.object(persistence.sqlite3, "connect", side_effect=connect):
            context = self.context()
        self.assertEqual(fired, [True])
        self.assertEqual(context.candidate.notes, old_notes)
        self.assertIn(old, [item.artifact_id for item in context.evidence.selected])
        self.assertNotEqual(self.repo.economic_evidence_snapshot(self.started.evaluation_id).sourcing_report.artifact_id, old)

    def test_busy_database_returns_structured_error_without_retry_or_repair(self):
        writer = sqlite3.connect(self.path)
        self.addCleanup(writer.close)
        writer.execute("BEGIN EXCLUSIVE")
        try:
            result = self.inspect()
            self.assertEqual(result.error_code, "SNAPSHOT_UNAVAILABLE")
            self.assertEqual(self.workflow.active.evaluation_id, self.started.evaluation_id)
        finally:
            writer.rollback()
