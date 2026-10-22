"""Human-interface channel authority; observation claims remain attributed reports."""

from arbitrage.contracts import (
    CandidateIntake, CandidateIntakeSource, IdentityConfidence, IntakeChannel,
    IntakeProvenance, StartCandidateEvaluationRequest,
    LeadIntakeAssessment, CandidateSourceKind,
)


def validate_intake_assessment(request, human_input, assessment):
    if assessment is None or not isinstance(human_input, str):
        raise ValueError("New Candidate requires an explicit intake assessment and current human input")
    assessment = LeadIntakeAssessment.model_validate(assessment.model_dump())
    if any(excerpt not in human_input for excerpt in assessment.supporting_user_excerpts):
        raise ValueError("Intake assessment excerpts must occur literally in the current human message")
    physical = request.acquisition_source.source_kind == CandidateSourceKind.PHYSICAL_STORE
    if ((assessment.physical_acquisition_proposed is True and not physical)
            or (physical and assessment.physical_acquisition_proposed is not True)):
        raise ValueError("Physical acquisition declaration must agree with PHYSICAL_STORE; unknown location does not imply OTHER")
    return assessment


def bind_human_intake(request: StartCandidateEvaluationRequest, human_input: str, assessment=None):
    """Copy an agent proposal; never infer observation from channel or field presence."""
    assessment = validate_intake_assessment(request, human_input, assessment)
    result = request.model_copy(deep=True)
    intake = result.intake_snapshot
    provenance = IntakeProvenance(
        channel=IntakeChannel.HUMAN_SUBMISSION,
        opportunity_status=assessment.status,
        supporting_user_excerpts=list(assessment.supporting_user_excerpts),
        unspecified_reason=assessment.unspecified_reason,
        physical_acquisition_proposed=assessment.physical_acquisition_proposed,
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
