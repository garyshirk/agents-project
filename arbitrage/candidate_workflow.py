from dataclasses import dataclass

from agents import RunContextWrapper, function_tool

from arbitrage.contracts import (
    CandidateLifecycleStatus,
    CandidateWorkflowResult,
    EvaluationStatus,
    FinishCandidateEvaluationRequest,
    StartCandidateEvaluationRequest,
    UpdateCandidateEvaluationRequest,
    WorkflowEvaluationAction,
)
from arbitrage.persistence import (
    CandidateRepository,
    RecordNotFoundError,
)


class CandidateWorkflowError(RuntimeError):
    pass


@dataclass
class ActiveInvestigation:
    candidate_id: str | None = None
    evaluation_id: str | None = None

    @property
    def is_active(self) -> bool:
        return self.candidate_id is not None and self.evaluation_id is not None

    def clear(self) -> None:
        self.candidate_id = None
        self.evaluation_id = None


class CandidateWorkflow:
    def __init__(self, repository: CandidateRepository) -> None:
        self.repository = repository
        self.active = ActiveInvestigation()

    def _active_records(self):
        if not self.active.is_active:
            raise CandidateWorkflowError("No Candidate Evaluation is currently active")
        assert self.active.candidate_id is not None
        assert self.active.evaluation_id is not None
        candidate = self.repository.get_candidate(self.active.candidate_id)
        evaluation = self.repository.get_evaluation(self.active.evaluation_id)
        if candidate is None or evaluation is None:
            raise CandidateWorkflowError("Active investigation records are unavailable")
        if evaluation.candidate_id != candidate.candidate_id:
            raise CandidateWorkflowError("Active Candidate and Evaluation do not match")
        return candidate, evaluation

    def active_result(self) -> CandidateWorkflowResult:
        candidate, evaluation = self._active_records()
        return self._result(candidate, evaluation, active=True)

    def qualification_context(self) -> str:
        if not self.active.is_active:
            return "No durable Candidate or Evaluation is active."
        candidate, evaluation = self._active_records()
        return (
            "A durable Candidate is active. Treat application state as authoritative. "
            f"Product: {candidate.product_identity.model_dump_json()}. "
            f"Acquisition source: {candidate.acquisition_source.model_dump_json()}. "
            f"Resale destination: {candidate.resale_destination.model_dump_json()}. "
            f"Evaluation status: {evaluation.status.value}."
        )

    @staticmethod
    def _result(candidate, evaluation, *, active: bool) -> CandidateWorkflowResult:
        return CandidateWorkflowResult(
            success=True,
            candidate_id=candidate.candidate_id,
            evaluation_id=evaluation.evaluation_id,
            candidate_lifecycle=candidate.lifecycle_status,
            evaluation_status=evaluation.status,
            active=active,
            error=None,
        )

    def error_result(self, error: Exception) -> CandidateWorkflowResult:
        candidate_id = self.active.candidate_id
        evaluation_id = self.active.evaluation_id
        candidate_lifecycle = None
        evaluation_status = None
        if self.active.is_active:
            try:
                candidate, evaluation = self._active_records()
                candidate_lifecycle = candidate.lifecycle_status
                evaluation_status = evaluation.status
            except Exception:
                pass
        return CandidateWorkflowResult(
            success=False,
            candidate_id=candidate_id,
            evaluation_id=evaluation_id,
            candidate_lifecycle=candidate_lifecycle,
            evaluation_status=evaluation_status,
            active=self.active.is_active,
            error=str(error),
        )

    def start_candidate_evaluation(
        self, request: StartCandidateEvaluationRequest
    ) -> CandidateWorkflowResult:
        if self.active.is_active:
            _, evaluation = self._active_records()
            raise CandidateWorkflowError(
                "Finish the active Candidate Evaluation before starting a new durable Candidate; "
                f"the current Evaluation is {evaluation.status.value}"
            )
        candidate, evaluation = self.repository.create_candidate_with_evaluation(
            product_identity=request.product_identity,
            acquisition_source=request.acquisition_source,
            resale_destination=request.resale_destination,
            intake_origin=request.intake_origin,
            trigger=request.trigger,
            intake_snapshot=request.intake_snapshot,
            assumptions=request.assumptions,
            uncertainties=request.uncertainties,
            candidate_notes=request.candidate_notes,
            manager_notes=request.manager_notes,
        )
        self.active.candidate_id = candidate.candidate_id
        self.active.evaluation_id = evaluation.evaluation_id
        return self._result(candidate, evaluation, active=True)

    def update_candidate_evaluation(
        self, request: UpdateCandidateEvaluationRequest
    ) -> CandidateWorkflowResult:
        candidate, evaluation = self._active_records()
        target = (
            EvaluationStatus.AWAITING_HUMAN_INPUT
            if request.action == WorkflowEvaluationAction.AWAIT_HUMAN_INPUT
            else EvaluationStatus.IN_PROGRESS
        )
        evaluation = self.repository.update_evaluation_status(
            evaluation.evaluation_id, target
        )
        candidate = self.repository.get_candidate(candidate.candidate_id)
        if candidate is None:
            raise RecordNotFoundError("Candidate disappeared during workflow update")
        return self._result(candidate, evaluation, active=True)

    def finish_candidate_evaluation(
        self, request: FinishCandidateEvaluationRequest
    ) -> CandidateWorkflowResult:
        candidate, evaluation = self._active_records()
        try:
            evaluation = self.repository.complete_evaluation(
                evaluation.evaluation_id,
                status=request.status,
                intake_snapshot=request.intake_snapshot,
                sourcing_result=request.sourcing_result,
                resale_result=request.resale_result,
                profitability_result=request.profitability_result,
                assumptions=request.assumptions,
                uncertainties=request.uncertainties,
                manager_notes=request.manager_notes,
            )
            candidate = self.repository.get_candidate(candidate.candidate_id)
            if candidate is None:
                raise RecordNotFoundError("Candidate disappeared during completion")
            return self._result(candidate, evaluation, active=False)
        finally:
            if evaluation.status in {
                EvaluationStatus.COMPLETED,
                EvaluationStatus.INSUFFICIENT_EVIDENCE,
                EvaluationStatus.FAILED,
            }:
                self.active.clear()


@dataclass
class ApplicationContext:
    candidate_workflow: CandidateWorkflow


def workflow_tool_error(
    context: RunContextWrapper[ApplicationContext], error: Exception
) -> str:
    message = str(error)
    if getattr(context, "tool_name", None) == "start_candidate_evaluation" and message.startswith(
        "Invalid JSON input"
    ):
        message += (
            ". For a PHYSICAL_STORE, physical_location must be a truthful nonblank description; "
            "when the exact address is unavailable, state that it is the human's current store "
            "and that the exact location was not provided"
        )
    return context.context.candidate_workflow.error_result(
        ValueError(message)
    ).model_dump_json()


@function_tool(
    output_type=CandidateWorkflowResult,
    failure_error_function=workflow_tool_error,
)
def start_candidate_evaluation(
    context: RunContextWrapper[ApplicationContext],
    request: StartCandidateEvaluationRequest,
) -> CandidateWorkflowResult:
    """Create a durable Candidate and its first Evaluation once identity is sufficient."""
    return context.context.candidate_workflow.start_candidate_evaluation(request)


@function_tool(
    output_type=CandidateWorkflowResult,
    failure_error_function=workflow_tool_error,
)
def update_candidate_evaluation(
    context: RunContextWrapper[ApplicationContext],
    request: UpdateCandidateEvaluationRequest,
) -> CandidateWorkflowResult:
    """Pause the active Evaluation for human input or resume that same Evaluation."""
    return context.context.candidate_workflow.update_candidate_evaluation(request)


@function_tool(
    output_type=CandidateWorkflowResult,
    failure_error_function=workflow_tool_error,
)
def finish_candidate_evaluation(
    context: RunContextWrapper[ApplicationContext],
    request: FinishCandidateEvaluationRequest,
) -> CandidateWorkflowResult:
    """Finish the active Evaluation and persist its available evidence and conclusion."""
    return context.context.candidate_workflow.finish_candidate_evaluation(request)
