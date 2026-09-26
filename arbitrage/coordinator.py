from collections.abc import Callable
from dataclasses import dataclass

from arbitrage.candidate_workflow import CandidateWorkflow
from arbitrage.contracts import (
    CandidateRelation,
    CandidateWorkflowResult,
    LeadDecision,
    LeadDisposition,
)


@dataclass
class CoordinationResult:
    proceeded: bool
    response: str
    workflow_result: CandidateWorkflowResult | None


class ArbitrageCoordinator:
    def __init__(self, candidate_workflow: CandidateWorkflow) -> None:
        self.candidate_workflow = candidate_workflow

    def qualification_context(self) -> str:
        return self.candidate_workflow.qualification_context()

    def coordinate(
        self,
        decision: LeadDecision,
        substantive_evaluation: Callable[[CandidateWorkflowResult], str],
    ) -> CoordinationResult:
        if decision.disposition == LeadDisposition.NEEDS_MORE_INFO:
            return CoordinationResult(False, decision.clarification_question or "", None)

        if decision.disposition == LeadDisposition.AMBIGUOUS_BOUNDARY:
            return CoordinationResult(False, decision.clarification_question or "", None)

        if decision.disposition == LeadDisposition.STOP:
            return CoordinationResult(False, decision.user_message or decision.reasoning, None)

        if decision.disposition == LeadDisposition.EXISTING_CANDIDATE:
            if not self.candidate_workflow.active.is_active:
                return CoordinationResult(
                    False,
                    "No active Candidate exists for this follow-up, so substantive evaluation did not start.",
                    None,
                )
            workflow_result = self.candidate_workflow.active_result()
            print("[debug] Existing Candidate reused")
            return CoordinationResult(
                True,
                substantive_evaluation(workflow_result),
                workflow_result,
            )

        if decision.relation_to_active == CandidateRelation.CLEARLY_NEW and (
            self.candidate_workflow.active.is_active
        ):
            return CoordinationResult(
                False,
                "A different Candidate is already active. Finish that investigation before starting this Lead.",
                None,
            )

        if self.candidate_workflow.active.is_active:
            if decision.relation_to_active == CandidateRelation.SAME_AS_ACTIVE:
                workflow_result = self.candidate_workflow.active_result()
                print("[debug] Existing Candidate reused")
                return CoordinationResult(
                    True,
                    substantive_evaluation(workflow_result),
                    workflow_result,
                )
            return CoordinationResult(
                False,
                "Candidate readiness conflicts with the active investigation; substantive evaluation did not start.",
                None,
            )

        if decision.relation_to_active == CandidateRelation.SAME_AS_ACTIVE:
            return CoordinationResult(
                False,
                "No active Candidate exists for the claimed follow-up, so substantive evaluation did not start.",
                None,
            )

        assert decision.candidate_request is not None
        print("[debug] Candidate creation started")
        try:
            workflow_result = self.candidate_workflow.start_candidate_evaluation(
                decision.candidate_request
            )
        except Exception as error:
            return CoordinationResult(
                False,
                f"Candidate persistence failed; substantive evaluation did not start. {error}",
                None,
            )
        print(
            "[debug] Candidate persisted: "
            f"{workflow_result.candidate_id} / {workflow_result.evaluation_id}"
        )
        return CoordinationResult(
            True,
            substantive_evaluation(workflow_result),
            workflow_result,
        )
