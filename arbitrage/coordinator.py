from collections.abc import Callable
from dataclasses import dataclass

from arbitrage.candidate_workflow import CandidateWorkflow
from arbitrage.contracts import (
    CandidateRelation,
    CandidateWorkflowResult,
    EvaluationStatus,
    LeadDecision,
    LeadDisposition,
    ManagerEvaluationJudgment,
    UpdateCandidateEvaluationRequest,
    WorkflowEvaluationAction,
)
from arbitrage.persistence import SubstantiveCompletionPrerequisiteError


SubstantiveEvaluation = Callable[
    [CandidateWorkflowResult, str | None], ManagerEvaluationJudgment
]


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
        human_input: str,
        substantive_evaluation: SubstantiveEvaluation,
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
            return self._evaluate_active(human_input, substantive_evaluation)

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
                return self._evaluate_active(human_input, substantive_evaluation)
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
        return self._evaluate_active(
            human_input,
            substantive_evaluation,
            workflow_result=workflow_result,
            reused=False,
        )

    def _evaluate_active(
        self,
        human_input: str,
        substantive_evaluation: SubstantiveEvaluation,
        *,
        workflow_result: CandidateWorkflowResult | None = None,
        reused: bool = True,
    ) -> CoordinationResult:
        if workflow_result is None:
            workflow_result = self.candidate_workflow.active_result()
        if reused:
            print("[debug] Existing Candidate reused")
        if workflow_result.evaluation_status == EvaluationStatus.WAITING_FOR_INPUT:
            workflow_result = self.candidate_workflow.update_candidate_evaluation(
                UpdateCandidateEvaluationRequest(action=WorkflowEvaluationAction.RESUME)
            )
            print("[debug] Evaluation resumed")
        self.candidate_workflow.record_human_input(human_input)
        try:
            judgment = substantive_evaluation(workflow_result, None)
            try:
                final_result = self.candidate_workflow.apply_manager_judgment(judgment)
            except SubstantiveCompletionPrerequisiteError as error:
                assert workflow_result.evaluation_id is not None
                print(
                    "[debug] Substantive completion blocked: "
                    f"{error} / {workflow_result.evaluation_id}"
                )
                print(
                    "[debug] Manager continuation requested after finalization guard"
                )
                continuation = (
                    "Deterministic finalization rejected the prior COMPLETED judgment: "
                    f"{error}. A failed calculate_profitability invocation is not a "
                    "substantive result and is not equivalent to a successful result "
                    "whose status is INSUFFICIENT_INPUTS. "
                    "Inspect its error, correct and retry the Profitability request when "
                    "possible, then return a new structured judgment. Do not declare "
                    "VIABLE based only on gross spread."
                )
                judgment = substantive_evaluation(
                    self.candidate_workflow.active_result(), continuation
                )
                try:
                    final_result = self.candidate_workflow.apply_manager_judgment(
                        judgment
                    )
                except SubstantiveCompletionPrerequisiteError:
                    return CoordinationResult(
                        False,
                        "Substantive completion was blocked because Profitability did "
                        "not execute successfully. The Evaluation remains active and all "
                        "captured attempts were preserved.",
                        self.candidate_workflow.active_result(),
                    )
        except Exception as error:
            failure = self.candidate_workflow.fail_active_evaluation(
                f"Substantive evaluation failed ({type(error).__name__})."
            )
            return CoordinationResult(
                False,
                "The evaluation could not be completed. Captured evidence was preserved for review.",
                failure,
            )
        return CoordinationResult(True, judgment.user_response, final_result)
