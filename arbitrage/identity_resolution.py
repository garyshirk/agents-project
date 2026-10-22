"""Explicit terminal-only human confirmation; no tools, model calls, or identity edits."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from arbitrage.contracts import (
    CandidateRecord, EvaluationArtifact, ProductIdentity,
    EvaluationArtifactType, EvaluationStatus, IdentityResolution, IdentityResolutionStatus,
    ResaleResult, SourceReference,
)
from arbitrage.identity_comparison import compare_product_identity
from arbitrage.persistence import EconomicEvidenceSnapshot, _IDENTITY_RESOLUTION_AUTHORITY

if TYPE_CHECKING:
    from arbitrage.candidate_workflow import CandidateWorkflow


PATH = "material_variant.colorway"
ORIGIN = "human_terminal_identity_command"


def identity_digest(identity: ProductIdentity) -> str:
    return hashlib.sha256(json.dumps(identity.model_dump(mode="json"), sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _records(evidence: EconomicEvidenceSnapshot) -> list[tuple[EvaluationArtifact, IdentityResolution]]:
    records = []
    for artifact in evidence.identity_resolutions:
        if artifact.artifact_type != EvaluationArtifactType.IDENTITY_RESOLUTION:
            continue
        try:
            record = IdentityResolution.model_validate_json(artifact.payload_json)
            context = json.loads(artifact.context_json or "{}")
        except (ValueError, TypeError):
            return []
        if (context != {"origin": ORIGIN} or artifact.evaluation_id != evidence.evaluation_id
                or record.evaluation_id != evidence.evaluation_id):
            return []
        records.append((artifact, record))
    return sorted(records, key=lambda pair: pair[0].sequence_number)


def _current_proposal(evidence: EconomicEvidenceSnapshot) -> tuple[EvaluationArtifact, IdentityResolution] | None:
    proposals = [(artifact, record) for artifact, record in _records(evidence)
                 if record.status == IdentityResolutionStatus.PROPOSED
                 and record.proposal_artifact_id is None and record.human_command == "/identity propose colorway"]
    return proposals[-1] if proposals else None


def _matches(
    candidate: CandidateRecord, evaluation_id: str,
    evidence: EconomicEvidenceSnapshot, record: IdentityResolution,
) -> bool:
    if evidence.resale is None:
        return False
    researched = ResaleResult.model_validate_json(evidence.resale.payload_json).researched_identity
    left = candidate.product_identity.material_variant
    right = researched.material_variant
    return (
        record.candidate_id == candidate.candidate_id and record.evaluation_id == evaluation_id
        and record.attribute_path == PATH and record.resale_artifact_id == evidence.resale.artifact_id
        and record.resale_sequence == evidence.resale.sequence_number
        and record.candidate_identity_sha256 == identity_digest(candidate.product_identity)
        and record.resale_identity_sha256 == identity_digest(researched)
        and left is not None and right is not None
        and record.candidate_value == left.colorway and record.resale_value == right.colorway
        and bool(record.supporting_evidence) and bool(record.explanation.strip())
    )


def confirmed_colorway_resolution(
    candidate: CandidateRecord, evaluation_id: str, evidence: EconomicEvidenceSnapshot,
) -> tuple[EvaluationArtifact, ...]:
    """Return exact proposal/confirmation artifacts, never a blanket match override."""
    proposal = _current_proposal(evidence)
    if proposal is None:
        return ()
    artifact, record = proposal
    if not _matches(candidate, evaluation_id, evidence, record):
        return ()
    events = [(a, r) for a, r in _records(evidence)
              if r.proposal_artifact_id == artifact.artifact_id and a.sequence_number > artifact.sequence_number]
    if not events or any(record.status == IdentityResolutionStatus.REJECTED for _, record in events):
        return ()
    event, confirmed = events[-1]
    if (confirmed.status != IdentityResolutionStatus.CONFIRMED
            or confirmed.human_command != f"/identity confirm {artifact.artifact_id}"
            or not _matches(candidate, evaluation_id, evidence, confirmed)):
        return ()
    expected = record.model_copy(update=dict(status=IdentityResolutionStatus.CONFIRMED,
                    proposal_artifact_id=artifact.artifact_id, human_command=confirmed.human_command))
    if confirmed != expected:
        return ()
    return (artifact, event)


def resolution_status(evidence: EconomicEvidenceSnapshot) -> str:
    """Describe the latest actual proposal, without implying that it is applicable."""
    proposal = _current_proposal(evidence)
    if proposal is None:
        return "No identity resolution proposal exists."
    artifact, record = proposal
    events = [r for a, r in _records(evidence)
              if r.proposal_artifact_id == artifact.artifact_id and a.sequence_number > artifact.sequence_number]
    status = events[-1].status if events else record.status
    return f"Latest proposal artifact {artifact.artifact_id}: {status.value}; applicability is checked against the current evidence."


def handle_identity_command(workflow: CandidateWorkflow, text: str) -> str | None:
    """Call ONLY from direct terminal input; never register this function as an agent tool."""
    from arbitrage.candidate_workflow import CandidateWorkflowError

    if not text.startswith("/identity"):
        return None
    try:
        candidate, evaluation = workflow._active_records()
        if evaluation.status not in {EvaluationStatus.IN_PROGRESS, EvaluationStatus.WAITING_FOR_INPUT}:
            raise ValueError("Identity clarification requires an active nonterminal Evaluation")
        evidence = workflow.repository.economic_evidence_snapshot(evaluation.evaluation_id)
        words = text.split()
        if text == "/identity propose colorway":
            if evidence.resale is None:
                raise ValueError("A persisted Resale result is required")
            researched = ResaleResult.model_validate_json(evidence.resale.payload_json)
            comparison = compare_product_identity(candidate.product_identity, researched.researched_identity)
            if PATH not in comparison.conflicting_fields:
                raise ValueError("No supported colorway conflict exists")
            references = [SourceReference(url=item.url, description=f"Resale evidence: {item.listing_title}")
                          for item in researched.evidence_items if item.url]
            if not references:
                raise ValueError("Proposal requires existing source references; none were supplied")
            record = IdentityResolution(
                candidate_id=candidate.candidate_id, evaluation_id=evaluation.evaluation_id,
                attribute_path=PATH, candidate_value=candidate.product_identity.material_variant.colorway,
                resale_value=researched.researched_identity.material_variant.colorway,
                candidate_identity_sha256=identity_digest(candidate.product_identity),
                resale_identity_sha256=identity_digest(researched.researched_identity),
                resale_artifact_id=evidence.resale.artifact_id, resale_sequence=evidence.resale.sequence_number,
                supporting_evidence=references,
                explanation=("Proposed equivalence for this target's colorway only, based on the attached Resale evidence. "
                             "Sources are not human confirmation or proof of full product equivalence; other attributes remain independently enforced."),
                proposed_by="APPLICATION_FROM_RESALE_EVIDENCE", status="PROPOSED",
                proposal_artifact_id=None, human_command=text,
            )
        elif len(words) == 3 and words[1] in {"confirm", "reject"}:
            if text != f"/identity {words[1]} {int(words[2])}":
                raise ValueError("Use the exact confirmation or rejection command displayed with the proposal")
            proposal = _current_proposal(evidence)
            if proposal is None or proposal[0].artifact_id != int(words[2]):
                raise ValueError("Proposal is missing or superseded for this active Evaluation")
            artifact, proposed = proposal
            if not _matches(candidate, evaluation.evaluation_id, evidence, proposed):
                raise ValueError("Proposal is stale or does not match the current identities and Resale artifact")
            events = [r for a, r in _records(evidence) if r.proposal_artifact_id == artifact.artifact_id]
            if events and (events[-1].status == IdentityResolutionStatus.REJECTED or words[1] == "confirm"):
                raise ValueError("Proposal was rejected or already decided; create a new proposal if appropriate")
            record = proposed.model_copy(update=dict(
                status=IdentityResolutionStatus.CONFIRMED if words[1] == "confirm" else IdentityResolutionStatus.REJECTED,
                proposal_artifact_id=artifact.artifact_id, human_command=text,
            ))
        else:
            raise ValueError("Use /identity propose colorway, /identity confirm <proposal-id>, or /identity reject <proposal-id>")
        stored = workflow.repository.append_evaluation_artifact(
            evaluation.evaluation_id, artifact_type=EvaluationArtifactType.IDENTITY_RESOLUTION,
            payload_json=record.model_dump_json(), context_json=json.dumps({"origin": ORIGIN}),
            _identity_authority=_IDENTITY_RESOLUTION_AUTHORITY,
        )
        return (f"Identity resolution {record.status.value}: artifact {stored.artifact_id}; "
                f"Candidate {candidate.candidate_id}; Evaluation {evaluation.evaluation_id}; "
                f"{PATH}: {record.candidate_value!r} versus {record.resale_value!r}. "
                + record.explanation + " Supporting sources: "
                + ", ".join(reference.url for reference in record.supporting_evidence) + ". "
                + (f"To explicitly approve this exact equivalence, type /identity confirm {stored.artifact_id}; "
                   f"to decline, type /identity reject {stored.artifact_id}. " if record.status == IdentityResolutionStatus.PROPOSED else "")
                + "No identities changed and no evaluation resumed or calculation started. A later explicit follow-up is required; restart restoration is not supported.")
    except (ValueError, LookupError, CandidateWorkflowError) as error:
        return f"Identity command rejected: {error}"
