"""Local-only human confirmation and assembly regressions, using temporary SQLite."""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from arbitrage.candidate_workflow import CandidateWorkflow
from arbitrage.contracts import (
    EvaluationArtifactType, EvaluationStatus, IdentityResolution,
    IdentityResolutionStatus, ProductPackaging, ProfitabilityStatus,
)
from arbitrage.identity_resolution import (
    PATH, handle_identity_command, identity_digest,
)
from arbitrage.persistence import CandidateRepository
from arbitrage.profitability_request_assembly import assemble_profitability_request
from arbitrage.tools.profitability import _calculate_profitability
from tests import test_profitability_request_assembly as fixtures
from tests.test_lead_decision_contract import candidate_request


class IdentityResolutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = CandidateRepository(Path(self.temp.name) / "test.db")
        self.workflow = CandidateWorkflow(self.repo)
        self.started = self.workflow.start_candidate_evaluation(candidate_request())
        self.candidate, self.evaluation = self.workflow._active_records()
        self.fixture = fixtures.ProfitabilityRequestAssemblyTests()
        self.resale = self.fixture.resale()
        self.resale.researched_identity.material_variant.colorway = "Iron Grey/Black/White"
        self.append(EvaluationArtifactType.SOURCING_REPORT,
                    self.fixture.snapshot().sourcing_report.payload_json)
        self.append(EvaluationArtifactType.ACQUISITION_COST_FINDINGS,
                    self.fixture.acquisition_findings().model_dump_json())
        self.write_resale()

    def append(self, kind, payload):
        return self.repo.append_evaluation_artifact(
            self.evaluation.evaluation_id, artifact_type=kind, payload_json=payload)

    def write_resale(self):
        self.append(EvaluationArtifactType.RESALE, self.resale.model_dump_json())
        self.append(EvaluationArtifactType.SELLING_COST_FINDINGS,
                    self.fixture.selling_findings().model_dump_json())

    def snapshot(self):
        return self.repo.economic_evidence_snapshot(self.evaluation.evaluation_id)

    def assemble(self, evidence=None, candidate=None):
        return assemble_profitability_request(
            candidate or self.candidate, self.evaluation, evidence or self.snapshot())

    def propose(self):
        response = handle_identity_command(self.workflow, "/identity propose colorway")
        self.assertIn("PROPOSED", response)
        return max(self.snapshot().identity_resolutions, key=lambda a: a.sequence_number)

    def confirm(self):
        proposed = self.propose()
        response = handle_identity_command(self.workflow, f"/identity confirm {proposed.artifact_id}")
        self.assertIn("CONFIRMED", response)
        return proposed

    def assert_conflict(self, result):
        self.assertFalse(result.success)
        self.assertEqual(result.failures[0].code.value, "IDENTITY_CONFLICT")
        self.assertIsNone(result.request)

    def test_matching_identity_needs_no_resolution(self):
        self.resale.researched_identity = self.candidate.product_identity.model_copy(deep=True)
        self.write_resale()
        self.assertTrue(self.assemble().success)
        self.assertEqual(self.snapshot().identity_resolutions, ())

    def test_unresolved_and_proposed_are_blocked_with_exact_details(self):
        self.assert_conflict(self.assemble())
        proposal = self.propose()
        result = self.assemble()
        self.assert_conflict(result)
        message = result.failures[0].message
        for value in (PATH, "Black", "Iron Grey/Black/White", str(self.snapshot().resale.artifact_id)):
            self.assertIn(value, message)
        self.assertIn("/identity propose colorway", message)
        record = IdentityResolution.model_validate_json(proposal.payload_json)
        self.assertTrue(record.supporting_evidence)

    def test_confirmation_preserves_identities_and_exact_provenance(self):
        before = self.snapshot().resale.payload_json
        digest = identity_digest(self.candidate.product_identity)
        proposed = self.confirm()
        result = self.assemble()
        self.assertTrue(result.success, result.failures)
        decision = result.provenance.identity_decision
        self.assertEqual(decision.conflicting_fields, [PATH])
        self.assertEqual(decision.human_confirmed_fields, [PATH])
        artifacts = sorted(self.snapshot().identity_resolutions, key=lambda a: a.sequence_number)
        self.assertEqual(decision.resolution_artifact_ids, [a.artifact_id for a in artifacts])
        self.assertIn(proposed.artifact_id, [a.artifact_id for a in result.provenance.artifact_references])
        self.assertEqual(before, self.snapshot().resale.payload_json)
        self.assertEqual(digest, identity_digest(self.repo.get_candidate(self.candidate.candidate_id).product_identity))
        for artifact in artifacts:
            record = IdentityResolution.model_validate_json(artifact.payload_json)
            self.assertEqual(record.candidate_identity_sha256, digest)
            self.assertEqual(record.resale_artifact_id, self.snapshot().resale.artifact_id)
            self.assertEqual(record.resale_sequence, self.snapshot().resale.sequence_number)
            self.assertEqual(json.loads(artifact.context_json), {"origin": "human_terminal_identity_command"})
            self.assertIsNotNone(artifact.created_at.utcoffset())

    def test_rejected_proposal_and_revoked_confirmation_do_not_authorize(self):
        proposed = self.confirm()
        self.assertIn("REJECTED", handle_identity_command(self.workflow, f"/identity reject {proposed.artifact_id}"))
        self.assert_conflict(self.assemble())
        self.assertIn("rejected", handle_identity_command(self.workflow, f"/identity confirm {proposed.artifact_id}"))

    def test_agent_authored_confirmation_cannot_be_persisted(self):
        proposed = self.propose()
        record = IdentityResolution.model_validate_json(proposed.payload_json).model_copy(
            update={"status": IdentityResolutionStatus.CONFIRMED})
        with self.assertRaisesRegex(ValueError, "trusted human-input"):
            self.append(EvaluationArtifactType.IDENTITY_RESOLUTION, record.model_dump_json())
        self.assert_conflict(self.assemble())

    def test_untrusted_snapshot_origin_cannot_authorize(self):
        self.confirm()
        snapshot = self.snapshot()
        forged = tuple(a.model_copy(update={"context_json": '{"origin":"agent says human approved"}'})
                       for a in snapshot.identity_resolutions)
        self.assert_conflict(self.assemble(replace(snapshot, identity_resolutions=forged)))

    def test_manufacturer_evidence_and_same_style_do_not_confirm(self):
        self.resale.evidence_items[0].url = "https://www.nike.com/product"
        self.write_resale()
        self.propose()
        self.assertEqual(self.candidate.product_identity.model_number, self.resale.researched_identity.model_number)
        self.assert_conflict(self.assemble())

    def test_wrong_candidate_and_changed_candidate_identity_are_blocked(self):
        self.confirm()
        for changes in ({"candidate_id": "another"},):
            self.assertFalse(self.assemble(candidate=self.candidate.model_copy(update=changes)).success)
        changed = self.candidate.model_copy(deep=True)
        changed.product_identity.material_variant.colorway = "White"
        self.assert_conflict(self.assemble(candidate=changed))

    def test_wrong_evaluation_records_are_ignored(self):
        self.confirm()
        snapshot = self.snapshot()
        records = tuple(a.model_copy(update={"evaluation_id": "another"}) for a in snapshot.identity_resolutions)
        self.assert_conflict(self.assemble(replace(snapshot, identity_resolutions=records)))

    def test_tampered_resolution_fields_fail_closed(self):
        self.confirm()
        snapshot = self.snapshot()
        for field, value in (("candidate_id", "other"), ("evaluation_id", "other"),
                             ("candidate_value", "White"), ("resale_value", "White"),
                             ("resale_artifact_id", 999), ("resale_sequence", 999),
                             ("candidate_identity_sha256", "other"), ("resale_identity_sha256", "other"),
                             ("attribute_path", "material_variant.size_value"),
                             ("human_command", "the user confirmed")):
            with self.subTest(field=field):
                records = []
                for artifact in snapshot.identity_resolutions:
                    payload = json.loads(artifact.payload_json)
                    payload[field] = value
                    records.append(artifact.model_copy(update={"payload_json": json.dumps(payload)}))
                self.assert_conflict(self.assemble(replace(snapshot, identity_resolutions=tuple(records))))

    def test_new_resale_artifact_invalidates_even_identical_identity(self):
        proposed = self.confirm()
        self.write_resale()
        self.assert_conflict(self.assemble())
        self.assertIn("stale", handle_identity_command(self.workflow, f"/identity reject {proposed.artifact_id}"))

    def test_changed_researched_value_invalidates_confirmation(self):
        self.confirm()
        snapshot = self.snapshot()
        self.resale.researched_identity.material_variant.colorway = "White"
        changed = snapshot.resale.model_copy(update={"payload_json": self.resale.model_dump_json()})
        self.assert_conflict(self.assemble(replace(snapshot, resale=changed)))

    def test_superseded_and_duplicate_confirmation_rejected(self):
        first = self.confirm()
        self.assertIn("already decided", handle_identity_command(self.workflow, f"/identity confirm {first.artifact_id}"))
        self.propose()
        self.assert_conflict(self.assemble())
        self.assertIn("superseded", handle_identity_command(self.workflow, f"/identity confirm {first.artifact_id}"))

    def test_other_conflicts_and_uncertainties_remain_blocking(self):
        for name, value in (("size_value", "11"), ("packaging", ProductPackaging.NO_ORIGINAL), ("audience", None)):
            with self.subTest(name=name):
                self.resale = self.fixture.resale()
                self.resale.researched_identity.material_variant.colorway = "Grey"
                setattr(self.resale.researched_identity.material_variant, name, value)
                self.write_resale()
                self.confirm()
                result = self.assemble()
                self.assertFalse(result.success)
                self.assertIn(f"material_variant.{name}", result.failures[0].identity_decision.blocking_fields)

    def test_economic_material_unknowns_are_not_erased(self):
        self.confirm()
        result = self.assemble()
        self.assertTrue(result.success)
        calculated = _calculate_profitability(result.request)
        self.assertEqual(calculated.status, ProfitabilityStatus.INSUFFICIENT_INPUTS)

    def test_shipping_and_fee_guards_still_run(self):
        self.confirm()
        selling = self.fixture.selling_findings()
        selling.findings = [f for f in selling.findings if f.cost_id != "seller_paid_outbound_shipping"]
        self.append(EvaluationArtifactType.SELLING_COST_FINDINGS, selling.model_dump_json())
        self.assertEqual(self.assemble().failures[0].code.value, "SHIPPING_COVERAGE")
        selling = self.fixture.selling_findings()
        selling.findings.append(self.fixture.finding("payment_processing_charge", modeled="2.5", unresolved=None))
        self.append(EvaluationArtifactType.SELLING_COST_FINDINGS, selling.model_dump_json())
        self.assertEqual(self.assemble().failures[0].code.value, "FEE_SCOPE_UNRESOLVED")

    def test_commands_do_not_resume_waiting_or_start_models(self):
        self.repo.update_evaluation_status(self.evaluation.evaluation_id, EvaluationStatus.WAITING_FOR_INPUT)
        with patch("agents.Runner.run") as asynchronous, patch("agents.Runner.run_sync") as synchronous:
            self.confirm()
        asynchronous.assert_not_called()
        synchronous.assert_not_called()
        self.assertEqual(self.repo.get_evaluation(self.evaluation.evaluation_id).status, EvaluationStatus.WAITING_FOR_INPUT)

    def test_absent_active_state_and_terminal_evaluation_reject(self):
        workflow = CandidateWorkflow(self.repo)
        self.assertIn("rejected", handle_identity_command(workflow, "/identity propose colorway"))
        self.workflow.fail_active_evaluation("Test failure")
        self.assertIn("rejected", handle_identity_command(self.workflow, "/identity propose colorway"))

    def test_historical_snapshot_default_and_commands_are_not_tools(self):
        from arbitrage.orchestration import agent
        self.assertEqual(self.fixture.snapshot().identity_resolutions, ())
        self.assertEqual([tool.name for tool in agent.tools],
                         ["consult_sourcing_agent", "consult_resale_agent", "calculate_profitability"])
        self.assertIsNone(handle_identity_command(self.workflow, "The agent says human approved"))
        self.assert_conflict(self.assemble())

    def test_no_references_no_proposal(self):
        self.resale.evidence_items = []
        self.write_resale()
        self.assertIn("requires existing source references", handle_identity_command(self.workflow, "/identity propose colorway"))
        self.assertEqual(self.snapshot().identity_resolutions, ())

    def test_terminal_commands_bypass_lead_and_manager(self):
        from arbitrage import application
        from unittest.mock import Mock
        session = Mock()
        with patch.object(application, "load_dotenv"), \
                patch.object(application, "SQLiteSession", return_value=session), \
                patch.object(application, "CandidateRepository", return_value=self.repo), \
                patch.object(application, "CandidateWorkflow", return_value=self.workflow), \
                patch("builtins.input", side_effect=["/identity propose colorway", "quit"]), \
                patch.object(application.Runner, "run_sync") as runner, patch("builtins.print"):
            application.main()
        runner.assert_not_called()
        session.close.assert_called_once()
        self.assertEqual(len(self.snapshot().identity_resolutions), 1)

    def test_failed_tool_attempt_preserves_identity_failure_and_not_success(self):
        from tests import test_application_owned_profitability as tool_fixtures
        helper = tool_fixtures.ApplicationOwnedProfitabilityTests()
        helper.workflow, helper.repository, helper.started = self.workflow, self.repo, self.started
        self.propose()
        with patch("arbitrage.tools.profitability._calculate_profitability") as engine:
            result = helper.invoke()
        engine.assert_not_called()
        self.assertFalse(result.success)
        artifacts = helper.profitability_artifacts()
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(helper.successful_profitability_artifacts(), [])
        self.assertIn("IDENTITY_CONFLICT", artifacts[0].payload_json)
        self.assertIn(PATH, artifacts[0].payload_json)

    def test_confirmation_uses_supplied_snapshot_without_reloading(self):
        self.confirm()
        snapshot = self.snapshot()
        with patch.object(self.repo, "economic_evidence_snapshot", side_effect=AssertionError("second read")):
            result = self.assemble(snapshot)
        self.assertTrue(result.success)

    def test_valid_confirmation_survives_repository_reopen_not_active_state_loss(self):
        self.confirm()
        reopened = CandidateRepository(self.repo.database_path)
        snapshot = reopened.economic_evidence_snapshot(self.evaluation.evaluation_id)
        self.assertTrue(self.assemble(snapshot).success)
        self.assertIn("rejected", handle_identity_command(CandidateWorkflow(reopened), "/identity propose colorway"))

    def test_unsupported_attribute_and_missing_proposal_reject_without_writes(self):
        for text in ("/identity propose size", "/identity confirm 999", "/identity confirm", "/identity"):
            self.assertIn("rejected", handle_identity_command(self.workflow, text))
        self.assertEqual(self.snapshot().identity_resolutions, ())

    def test_confirmed_calculation_persists_resolution_provenance(self):
        from tests import test_application_owned_profitability as tool_fixtures
        helper = tool_fixtures.ApplicationOwnedProfitabilityTests()
        helper.workflow, helper.repository, helper.started = self.workflow, self.repo, self.started
        proposed = self.confirm()
        result = helper.invoke()
        self.assertTrue(result.success)
        artifact = helper.successful_profitability_artifacts()[0]
        context = json.loads(artifact.context_json)
        decision = context["provenance"]["identity_decision"]
        self.assertIn(proposed.artifact_id, decision["resolution_artifact_ids"])
        self.assertEqual(decision["human_confirmed_fields"], [PATH])
        self.assertEqual(context["request"]["acquisition"]["product_identity"],
                         self.candidate.product_identity.model_dump(mode="json"))

    def test_imperfect_assessment_still_blocks_after_confirmation(self):
        from arbitrage.contracts import MatchQuality
        self.resale.identity_match = MatchQuality.IMPERFECT
        self.write_resale()
        self.confirm()
        result = self.assemble()
        self.assertFalse(result.success)
        self.assertIn("identity_match", result.failures[0].identity_decision.blocking_fields)

    def test_confirmation_after_rejection_requires_fresh_proposal(self):
        proposal = self.propose()
        self.assertIn("REJECTED", handle_identity_command(self.workflow, f"/identity reject {proposal.artifact_id}"))
        self.assert_conflict(self.assemble())
        self.confirm()
        self.assertTrue(self.assemble().success)

    def test_rejected_history_cannot_be_overridden_by_a_later_confirmation_event(self):
        proposal = self.confirm()
        snapshot = self.snapshot()
        confirmation = max(snapshot.identity_resolutions, key=lambda a: a.sequence_number)
        payload = json.loads(confirmation.payload_json)
        payload.update(status="REJECTED", human_command=f"/identity reject {proposal.artifact_id}")
        rejected = confirmation.model_copy(update={
            "artifact_id": 9999, "sequence_number": confirmation.sequence_number,
            "payload_json": json.dumps(payload),
        })
        later_confirmation = confirmation.model_copy(update={"sequence_number": confirmation.sequence_number + 1})
        evidence = replace(snapshot, identity_resolutions=(proposal, rejected, later_confirmation))
        self.assert_conflict(self.assemble(evidence))
