"""Offline diagnostic dimensions, not an economic truth oracle or model call harness."""

from arbitrage.v2.contracts import Contract, Producer, Provenance, Text
from arbitrage.v2.manager import _walk, parse_proposal, read_context


class JudgmentRubric(Contract):
    case_name: Text
    human_authored_expectations: tuple[Text, ...]
    consequential_uncertainties: tuple[Text, ...]


def research_judgment_rubric(case_name, *, expectations, uncertainties):
    """Human review prompts, not automatic scoring or role/scope acceptance rules."""
    return JudgmentRubric(case_name=case_name,human_authored_expectations=(
        'Assess role appropriateness against the primary objective; no keyword-based correct answer.',
        'Assess scope coherence and whether the invocation budget is realistic.',
        'Assess research prioritization by expected economic value, not exhaustive fact collection.',
        'Distinguish public research, necessary human observations and tolerable uncertainty.',
        'Assess incomplete identity and comparable differences without an automatic exact-match gate.',
        'Assess comparable quality and completion/realized-price support separately from asking prices.',
        *expectations),consequential_uncertainties=tuple(uncertainties))


class EvaluationReport(Contract):
    case_name: str
    structural_validity: bool
    provenance_scope_validity: bool | None
    application_reference_integrity: bool | None
    judgment_quality: str = "HUMAN_REVIEW_REQUIRED"
    rubric: JudgmentRubric
    diagnostic: str | None = None


class _RollbackProbe(Exception):
    pass


def evaluate_proposal(repository, context, encoded, rubric: JudgmentRubric) -> EvaluationReport:
    """Use exact existing validators in a rolled-back transaction over a caller's isolated fixture DB."""
    try:
        proposal=parse_proposal(encoded)
        read_context(context)
    except ValueError:
        return EvaluationReport(case_name=rubric.case_name,structural_validity=False,
            provenance_scope_validity=None,application_reference_integrity=None,rubric=rubric,diagnostic="Schema/syntax rejection")
    provenance_ok=True
    with repository._connection() as connection:
        try:
            for provenance in _walk(proposal,Provenance):
                repository._validate_provenance(connection,provenance,context.reference.evaluation_id)
        except ValueError:
            provenance_ok=False
    accepted=False
    diagnostic=None
    try:
        with repository._connection(write=True) as connection:
            token=repository._manager_transaction.set(connection)
            try:
                repository.record_manager_decision(context.reference,encoded,
                    producer=Producer(kind="APPLICATION",name="Synthetic offline evaluation probe"),operation_key="evaluation-probe")
                accepted=True
                raise _RollbackProbe()
            finally:
                repository._manager_transaction.reset(token)
    except _RollbackProbe:
        pass
    except ValueError as error:
        diagnostic=type(error).__name__ + ": authoritative application validation rejected proposal"
    return EvaluationReport(case_name=rubric.case_name,structural_validity=True,
        provenance_scope_validity=provenance_ok,application_reference_integrity=accepted,
        rubric=rubric,diagnostic=diagnostic)
