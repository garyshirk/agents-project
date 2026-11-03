"""Offline diagnostic dimensions, not an economic truth oracle or model call harness."""

from arbitrage.v2.contracts import Contract, Producer, Provenance, Text
from arbitrage.v2.manager import _walk, parse_proposal, read_context


class JudgmentRubric(Contract):
    case_name: Text
    human_authored_expectations: tuple[Text, ...]
    consequential_uncertainties: tuple[Text, ...]


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
