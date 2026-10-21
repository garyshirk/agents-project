"""Human-interface channel authority; observation claims remain attributed reports."""

from arbitrage.contracts import (
    CandidateIntake, CandidateIntakeSource, IdentityConfidence, IntakeChannel,
    IntakeProvenance, StartCandidateEvaluationRequest,
)


def bind_human_intake(request: StartCandidateEvaluationRequest, human_input: str):
    """Copy an agent proposal; never infer observation from channel or field presence."""
    result = request.model_copy(deep=True)
    intake = result.intake_snapshot
    proposed = intake.provenance if intake else None
    excerpts = proposed.supporting_user_excerpts if proposed else []
    # Validate literal support, not linguistic meaning. Status remains an agent-attributed
    # interpretation of explicit statements, never independent verification.
    supported = bool(excerpts) and all(excerpt.strip() and excerpt in human_input for excerpt in excerpts)
    provenance = IntakeProvenance(
        channel=IntakeChannel.HUMAN_SUBMISSION,
        opportunity_status=proposed.opportunity_status if proposed and supported else None,
        supporting_user_excerpts=list(excerpts) if supported else [],
    )
    if intake is None:
        intake = CandidateIntake(
            intake_source=CandidateIntakeSource.HUMAN_SUBMISSION,
            product_identity=None, identity_confidence=IdentityConfidence.AMBIGUOUS,
            identity_basis=None, acquisition=None, resale=None, notes=None,
        )
    intake.intake_source = CandidateIntakeSource.HUMAN_SUBMISSION
    intake.provenance = provenance
    result.intake_origin = CandidateIntakeSource.HUMAN_SUBMISSION
    result.intake_snapshot = intake
    return result
