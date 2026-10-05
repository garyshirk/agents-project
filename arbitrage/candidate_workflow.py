import json
from dataclasses import dataclass, field

from agents import RunContextWrapper, function_tool

from arbitrage.contracts import (
    CandidateLifecycleStatus,
    CandidateConclusion,
    CandidateWorkflowResult,
    EvaluationArtifact,
    EvaluationArtifactType,
    EvaluationStatus,
    EvaluationTrigger,
    FinishCandidateEvaluationRequest,
    ManagerEvaluationJudgment,
    ManagerEvaluationOutcome,
    StartCandidateEvaluationRequest,
    UpdateCandidateEvaluationRequest,
    WorkflowEvaluationAction,
)
from arbitrage.persistence import (
    CandidateRepository,
    RecordNotFoundError,
)
from arbitrage.lead_routing_state import LeadRoutingState


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
            EvaluationStatus.WAITING_FOR_INPUT
            if request.action == WorkflowEvaluationAction.WAIT_FOR_INPUT
            else EvaluationStatus.IN_PROGRESS
        )
        evaluation = self.repository.update_evaluation_status(
            evaluation.evaluation_id, target
        )
        candidate = self.repository.get_candidate(candidate.candidate_id)
        if candidate is None:
            raise RecordNotFoundError("Candidate disappeared during workflow update")
        return self._result(candidate, evaluation, active=True)

    def start_existing_candidate_evaluation(
        self,
        candidate_id: str,
        *,
        trigger: EvaluationTrigger,
    ) -> CandidateWorkflowResult:
        if self.active.is_active:
            raise CandidateWorkflowError("An Evaluation is already active")
        evaluation = self.repository.create_evaluation(candidate_id, trigger=trigger)
        candidate = self.repository.get_candidate(candidate_id)
        if candidate is None:
            raise RecordNotFoundError("Candidate disappeared during reevaluation")
        self.active.candidate_id = candidate_id
        self.active.evaluation_id = evaluation.evaluation_id
        return self._result(candidate, evaluation, active=True)

    def record_human_input(self, text: str) -> EvaluationArtifact:
        if not text.strip():
            raise ValueError("human input must not be blank")
        _, evaluation = self._active_records()
        return self.repository.append_evaluation_artifact(
            evaluation.evaluation_id,
            artifact_type=EvaluationArtifactType.HUMAN_INPUT,
            payload_json=json.dumps({"text": text}),
        )

    def capture_specialist_result(
        self,
        artifact_type: EvaluationArtifactType,
        payload_json: str,
        *,
        context_json: str | None = None,
    ) -> EvaluationArtifact:
        _, evaluation = self._active_records()
        artifact = self.repository.append_evaluation_artifact(
            evaluation.evaluation_id,
            artifact_type=artifact_type,
            payload_json=payload_json,
            context_json=context_json,
        )
        call_number = sum(
            item.artifact_type == artifact_type
            for item in self.repository.list_evaluation_artifacts(
                evaluation.evaluation_id
            )
        )
        print(
            "[debug] Specialist result captured: "
            f"{artifact_type.value.lower()} / {evaluation.evaluation_id} / "
            f"call {call_number}"
        )
        return artifact

    def apply_manager_judgment(
        self, judgment: ManagerEvaluationJudgment
    ) -> CandidateWorkflowResult:
        print(
            "[debug] Manager evaluation judgment: "
            f"{judgment.evaluation_outcome.value} / {judgment.candidate_conclusion.value}"
        )
        if judgment.evaluation_outcome == ManagerEvaluationOutcome.WAITING_FOR_INPUT:
            result = self.update_candidate_evaluation(
                UpdateCandidateEvaluationRequest(
                    action=WorkflowEvaluationAction.WAIT_FOR_INPUT
                )
            )
            return result

        status = (
            EvaluationStatus.COMPLETED
            if judgment.evaluation_outcome == ManagerEvaluationOutcome.COMPLETED
            else EvaluationStatus.CANCELLED
        )
        candidate_status = {
            CandidateConclusion.VIABLE: CandidateLifecycleStatus.VIABLE,
            CandidateConclusion.REJECTED: CandidateLifecycleStatus.REJECTED,
            CandidateConclusion.INCONCLUSIVE: CandidateLifecycleStatus.INVESTIGATING,
            CandidateConclusion.CLOSED: CandidateLifecycleStatus.CLOSED,
        }[judgment.candidate_conclusion]
        candidate, evaluation = self._active_records()
        try:
            evaluation = self.repository.complete_evaluation(
                evaluation.evaluation_id,
                status=status,
                candidate_status=candidate_status,
                assumptions=judgment.assumptions,
                uncertainties=judgment.uncertainties,
                manager_notes=judgment.manager_notes,
            )
            candidate = self.repository.get_candidate(candidate.candidate_id)
            if candidate is None:
                raise RecordNotFoundError("Candidate disappeared during finalization")
            print(
                f"[debug] Evaluation finalized: {evaluation.evaluation_id} / {status.value}"
            )
            print(
                f"[debug] Candidate lifecycle: {candidate.candidate_id} / "
                f"{candidate.lifecycle_status.value}"
            )
            return self._result(candidate, evaluation, active=False)
        finally:
            if evaluation.status in {
                EvaluationStatus.COMPLETED,
                EvaluationStatus.FAILED,
                EvaluationStatus.CANCELLED,
            }:
                self.active.clear()

    def fail_active_evaluation(self, reason: str) -> CandidateWorkflowResult:
        candidate, evaluation = self._active_records()
        safe_reason = reason.strip() or "Substantive evaluation failed."
        try:
            evaluation = self.repository.complete_evaluation(
                evaluation.evaluation_id,
                status=EvaluationStatus.FAILED,
                candidate_status=CandidateLifecycleStatus.INVESTIGATING,
                manager_notes=safe_reason,
            )
            candidate = self.repository.get_candidate(candidate.candidate_id)
            if candidate is None:
                raise RecordNotFoundError("Candidate disappeared during failure handling")
            return self._result(candidate, evaluation, active=False)
        finally:
            if evaluation.status == EvaluationStatus.FAILED:
                self.active.clear()

    def finish_candidate_evaluation(
        self, request: FinishCandidateEvaluationRequest
    ) -> CandidateWorkflowResult:
        candidate, evaluation = self._active_records()
        try:
            evaluation = self.repository.complete_evaluation(
                evaluation.evaluation_id,
                status=request.status,
                candidate_status=CandidateLifecycleStatus.INVESTIGATING,
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
                EvaluationStatus.FAILED,
                EvaluationStatus.CANCELLED,
            }:
                self.active.clear()


@dataclass
class ApplicationContext:
    candidate_workflow: CandidateWorkflow
    lead_routing: LeadRoutingState = field(default_factory=LeadRoutingState)


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
