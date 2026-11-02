"""Bounded offline batches over V2 artifacts. No agents, implicit retries, or business judgments."""

import sqlite3
from hashlib import sha256
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, ValidationError

from arbitrage.v2.contracts import (
    ArtifactRef, ArtifactType, AssessmentDraft, AssumptionRecord, ComparableDraft,
    Contract, FailureRecord, OpportunitySubmission, Producer, RawResearchReport,
    ScenarioDraft, StepInputManifest, SummaryDraft, Text, WorkflowLimits, WorkflowStep,
)
from arbitrage.v2.persistence import V2PersistenceError, V2Repository


class ResearchOutput(Contract):
    role: Literal["RESEARCH"]
    report: RawResearchReport


class ComparableOutput(Contract):
    role: Literal["COMPARABLE"]
    draft: ComparableDraft


class AssessmentOutput(Contract):
    role: Literal["ASSESSMENT"]
    comparable: ArtifactRef
    draft: AssessmentDraft


class AssumptionOutput(Contract):
    role: Literal["ASSUMPTION"]
    record: AssumptionRecord


class ScenarioOutput(Contract):
    role: Literal["SCENARIO"]
    draft: ScenarioDraft


class SummaryOutput(Contract):
    role: Literal["SUMMARY"]
    draft: SummaryDraft


class SpecialistFailure(Contract):
    role: Literal["FAILURE"]
    failure: FailureRecord


class OfflineResponse(Contract):
    opportunity_id: Text
    evaluation_id: Text
    output: Annotated[ResearchOutput | ComparableOutput | AssessmentOutput | AssumptionOutput
                      | ScenarioOutput | SummaryOutput | SpecialistFailure, Field(discriminator="role")]


class RecoveryRequired(V2PersistenceError):
    """Ambiguous started run: caller must explicitly reconcile or abandon it."""


class WorkflowPersistenceFailure(RuntimeError):
    """Recording failed; durable state must be inspected before continuing."""


def output_operation_key(attempt):
    return "workflow-output:" + attempt.payload.attempt_id


def references(value):
    """Walk typed references only; never parse identity, prices, or prose."""
    if isinstance(value, ArtifactRef):
        return (value,)
    if isinstance(value, BaseModel):
        return tuple(ref for name in type(value).model_fields for ref in references(getattr(value,name)))
    if isinstance(value, (tuple,list)):
        return tuple(ref for item in value for ref in references(item))
    return ()


def invoke_offline_fixture(manifest: StepInputManifest) -> OfflineResponse:
    """Data-only adapter. Receives no repository, human service, callable, or external tools."""
    manifest = StepInputManifest.model_validate(manifest)
    if manifest.fixture is None:
        raise ValueError("No supplied fixture")
    return OfflineResponse.model_validate_json(manifest.fixture.response_json)


class OfflineWorkflow:
    def __init__(self, repository: V2Repository):
        self.repository = repository

    def accept(self, submission: OpportunitySubmission, limits: WorkflowLimits, *, operation_key):
        return self.repository.accept_workflow(submission,limits,operation_key=operation_key)

    def _validate_response(self, manifest, response):
        if response.evaluation_id != manifest.evaluation_id or response.opportunity_id != manifest.opportunity_id:
            raise ValueError("Specialist output attempts to substitute Evaluation/Opportunity identity")
        if response.output.role not in {manifest.role,"FAILURE"}:
            raise ValueError("Specialist returned a different output role")
        if any(ref not in manifest.context_references for ref in references(response.output)):
            raise ValueError("Output references evidence not supplied in the exact input manifest")
        if isinstance(response.output,ComparableOutput) and response.output.draft.source.capture_method != "SYNTHETIC_FIXTURE":
            raise ValueError("Offline comparable must be attributed as synthetic")

    def _persist_output(self, attempt, response):
        """One accepted output through its existing validated writer, never a generic SQL payload."""
        manifest = attempt.payload.input_manifest
        eid, key = manifest.evaluation_id, output_operation_key(attempt)
        if manifest.role == "CALCULATION":
            return self.repository.calculate_scenario(manifest.scenario,producer=Producer(
                kind="APPLICATION",name="V2 offline deterministic calculation"),operation_key=key)
        output = response.output
        producer = Producer(kind="APPLICATION",name="Synthetic offline fixture: " + manifest.fixture.fixture_id)
        options = dict(producer=producer,operation_key=key)
        if isinstance(output,ResearchOutput):
            return self.repository.record_artifact(eid,output.report,parents=manifest.context_references,**options)
        if isinstance(output,ComparableOutput):
            return self.repository.record_comparable(eid,output.draft,parents=manifest.context_references,**options)
        if isinstance(output,AssessmentOutput):
            return self.repository.record_assessment(eid,output.comparable,output.draft,parents=manifest.context_references,**options)
        if isinstance(output,AssumptionOutput):
            return self.repository.record_artifact(eid,output.record,parents=manifest.context_references,**options)
        if isinstance(output,ScenarioOutput):
            return self.repository.record_scenario(eid,output.draft,**options)
        if isinstance(output,SummaryOutput):
            return self.repository.record_summary(eid,output.draft,**options)
        raise ValueError("Not an accepted output")

    @staticmethod
    def _output_status(artifact):
        if artifact.artifact_type != ArtifactType.CALCULATION:
            return "SUCCEEDED"
        return {"CALCULATED":"SUCCEEDED","INSUFFICIENT_INPUTS":"CALCULATION_INSUFFICIENT",
                "INVALID_INPUTS":"CALCULATION_INVALID","ERROR":"CALCULATION_ERROR"}[artifact.payload.status]

    def _complete(self, start, status, *, artifact=None, failure=None):
        return self.repository.complete_workflow_attempt(start.reference,status,
            outputs=(artifact.reference,) if artifact else (),failure=failure,
            operation_key="workflow-end:" + start.payload.attempt_id)

    def _execute(self, attempt):
        manifest = attempt.payload.input_manifest
        response = None
        if manifest.role != "CALCULATION":
            try:
                response = invoke_offline_fixture(manifest)
                self._validate_response(manifest,response)
            except (ValidationError,ValueError) as error:
                return self._complete(attempt,"INVALID_OUTPUT",failure=FailureRecord(
                    stage="specialist_validation",category=type(error).__name__,message=str(error)))
            except Exception as error:
                return self._complete(attempt,"COORDINATOR_ERROR",failure=FailureRecord(
                    stage="specialist_boundary",category=type(error).__name__,message=str(error) or repr(error)))
            if isinstance(response.output,SpecialistFailure):
                return self._complete(attempt,"SPECIALIST_FAILURE",failure=response.output.failure)
        try:
            artifact = self._persist_output(attempt,response)
        except (ValidationError,V2PersistenceError,ValueError) as error:
            return self._complete(attempt,"INVALID_OUTPUT",failure=FailureRecord(
                stage="validated_output_persistence",category=type(error).__name__,message=str(error)))
        except sqlite3.Error as error:
            # Best effort only: if this write also fails, the original STARTED remains ambiguous.
            try:
                self._complete(attempt,"PERSISTENCE_FAILURE",failure=FailureRecord(
                    stage="output_persistence",category=type(error).__name__,message=str(error)))
            except Exception as recording_error:
                raise WorkflowPersistenceFailure("Output and failure recording failed; inspect durable state") from recording_error
            raise WorkflowPersistenceFailure("Output persistence failed; failure recorded, no output success claimed") from error
        except Exception as error:
            return self._complete(attempt,"COORDINATOR_ERROR",failure=FailureRecord(
                stage="coordinator_output",category=type(error).__name__,message=str(error) or repr(error)))
        return self._complete(attempt,self._output_status(artifact),artifact=artifact)

    def run(self, evaluation_id, steps: tuple[WorkflowStep, ...], *, operation_key):
        """Execute exactly the caller's batch. Other-Evaluation/latest context is never selected."""
        token = str(uuid4())
        run = self.repository.begin_workflow_run(evaluation_id,steps,owner_token=token,operation_key=operation_key)
        snapshot = self.repository.get_workflow(evaluation_id)
        closed = [e for e in snapshot.events if e.payload.run == run.reference and e.payload.event in {"RUN_ENDED","RECOVERED"}]
        if closed:
            return closed[-1]
        if run.payload.owner_token != token:
            raise RecoveryRequired("Existing run is not complete; explicitly recover it before any continuation")
        try:
            for step in run.payload.steps:
                start = self.repository.begin_workflow_attempt(run.reference,step.step_id,owner_token=token,
                    operation_key="workflow-start:" + run.reference.artifact_id + ":" + sha256(step.step_id.encode()).hexdigest())
                self._execute(start)
            return self.repository.end_workflow_run(run.reference,reason="Supplied offline batch ended; no economic state inferred.",
                operation_key="workflow-run-end:" + run.reference.artifact_id)
        except BaseException as error:
            if isinstance(error,(KeyboardInterrupt,SystemExit)):
                category = "INTERRUPTED"
            elif isinstance(error,(sqlite3.Error,WorkflowPersistenceFailure)):
                category = "PERSISTENCE_FAILURE"
            else:
                category = "COORDINATOR_ERROR"
            try:
                self.repository.report_workflow_interruption(run.reference,FailureRecord(stage="workflow_run",
                    category=category,message=str(error) or type(error).__name__),
                    operation_key="workflow-interruption:" + run.reference.artifact_id)
            except Exception:
                # Storage may be unavailable. Preserve the original error; claim no durable report.
                pass
            raise

    def recover(self, run: ArtifactRef, *, decision: Literal["RECONCILE", "ABANDON"], operation_key):
        """Explicit stopped-process recovery. Never invokes specialists or recalculates."""
        if decision not in {"RECONCILE","ABANDON"}:
            raise ValueError("Explicit RECONCILE or ABANDON decision required")
        event = self.repository.get_artifact(run)
        if event.artifact_type != ArtifactType.WORKFLOW_EVENT or event.payload.event != "RUN_STARTED":
            raise ValueError("Recovery requires the exact RUN_STARTED artifact")
        request = self.repository.begin_workflow_recovery(run,decision,operation_key=operation_key)
        end_key = "workflow-recovery-end:" + request.reference.artifact_id
        snapshot = self.repository.get_workflow(run.evaluation_id)
        closed = [e for e in snapshot.events if e.payload.run == run and e.payload.event in {"RUN_ENDED","RECOVERED"}]
        if closed:
            # Preserve request conflict checking via the repository's operation receipt.
            if closed[-1].payload.event == "RECOVERED":
                return self.repository.end_workflow_run(run,recovered=True,reason="Explicit " + decision + " recovery.",operation_key=end_key)
            raise ValueError("Run already completed normally")
        ends = {a.payload.start for a in snapshot.attempts if a.payload.start}
        for start in snapshot.attempts:
            if start.payload.run != run or start.payload.status != "STARTED" or start.reference in ends:
                continue
            artifact = self.repository.get_operation_artifact(output_operation_key(start))
            if artifact is not None:
                # Even ABANDON cannot pretend an already committed output did not happen.
                self._complete(start,self._output_status(artifact),artifact=artifact)
            else:
                self._complete(start,"INTERRUPTED",failure=FailureRecord(stage="workflow_recovery",
                    category="INTERRUPTED",message="Explicit " + decision + "; invocation outcome unrecorded; no automatic replay."))
        return self.repository.end_workflow_run(run,recovered=True,reason="Explicit " + decision + " recovery.",operation_key=end_key)

    def finish(self, evaluation_id, state, *, reason, operation_key):
        return self.repository.finish_workflow(evaluation_id,state,reason=reason,operation_key=operation_key)
