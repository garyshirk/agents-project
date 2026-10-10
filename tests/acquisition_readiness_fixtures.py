from arbitrage.contracts import (
    CostType,
    EconomicCostFinding,
    EconomicCostFindingsResult,
    EvaluationArtifactType,
    InputBasis,
    ResaleResult,
    SourcingTextReport,
)
from arbitrage.persistence import CandidateRepository


def append_sourcing_report(
    repository: CandidateRepository,
    evaluation_id: str,
    report_text: str = "Substantive sourcing evidence.",
):
    return repository.append_evaluation_artifact(
        evaluation_id,
        artifact_type=EvaluationArtifactType.SOURCING_REPORT,
        payload_json=SourcingTextReport(report_text=report_text).model_dump_json(),
    )


def append_acquisition_capture(
    repository: CandidateRepository,
    evaluation_id: str,
):
    result = EconomicCostFindingsResult(
        findings=[
            EconomicCostFinding(
                cost_id="purchase_price",
                name="Purchase price",
                cost_type=CostType.FIXED_PER_UNIT,
                value="39.99",
                estimated_low=None,
                estimated_high=None,
                currency="USD",
                basis=InputBasis.HUMAN_OBSERVED,
                modeled_value=None,
                modeled_value_basis=None,
                modeled_value_is_conservative=False,
                unresolved_materiality=None,
                source_references=[],
                limitations=["Test fixture evidence."],
                notes=None,
            )
        ]
    )
    return repository.append_evaluation_artifact(
        evaluation_id,
        artifact_type=EvaluationArtifactType.ACQUISITION_COST_FINDINGS,
        payload_json=result.model_dump_json(),
    )


def append_acquisition_readiness(
    repository: CandidateRepository,
    evaluation_id: str,
) -> None:
    append_sourcing_report(repository, evaluation_id)
    append_acquisition_capture(repository, evaluation_id)


def append_resale(repository: CandidateRepository, evaluation_id: str):
    from tests.test_candidate_persistence import CandidatePersistenceTests

    resale: ResaleResult = CandidatePersistenceTests().resale_result()
    return repository.append_evaluation_artifact(
        evaluation_id,
        artifact_type=EvaluationArtifactType.RESALE,
        payload_json=resale.model_dump_json(),
    )


def append_selling_capture(repository: CandidateRepository, evaluation_id: str):
    result = EconomicCostFindingsResult(
        findings=[
            EconomicCostFinding(
                cost_id="marketplace_fee",
                name="Marketplace fee",
                cost_type=CostType.PERCENT_OF_UNIT_PRICE,
                value=None,
                estimated_low=None,
                estimated_high=None,
                currency=None,
                basis=None,
                modeled_value=None,
                modeled_value_basis=None,
                modeled_value_is_conservative=False,
                unresolved_materiality="MATERIAL",
                source_references=[],
                limitations=["Test fixture evidence."],
                notes=None,
            )
        ]
    )
    return repository.append_evaluation_artifact(
        evaluation_id,
        artifact_type=EvaluationArtifactType.SELLING_COST_FINDINGS,
        payload_json=result.model_dump_json(),
    )


def append_selling_readiness(
    repository: CandidateRepository, evaluation_id: str
) -> None:
    append_resale(repository, evaluation_id)
    append_selling_capture(repository, evaluation_id)


def append_economic_readiness(
    repository: CandidateRepository, evaluation_id: str
) -> None:
    append_acquisition_readiness(repository, evaluation_id)
    append_selling_readiness(repository, evaluation_id)
