from pydantic import BaseModel, ConfigDict

from arbitrage.contracts import (
    CostComponent,
    CostType,
    EconomicCostFinding,
    EconomicCostFindingsResult,
    InputBasis,
    NonNegativeDecimalString,
    ScenarioSide,
    SensitivityInput,
    SourceReference,
    UnknownInput,
)


PURCHASE_PRICE_COST_ID = "acquisition_purchase_price"


class ProjectedFindingEvidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    scenario_side: ScenarioSide
    cost_id: str
    source_references: list[SourceReference]
    limitations: list[str]
    notes: str | None


class EconomicFindingProjection(BaseModel):
    model_config = ConfigDict(frozen=True)

    valid: bool
    failure_reasons: list[str]
    currency: str
    unit_purchase_price: NonNegativeDecimalString | None
    purchase_price_basis: InputBasis | None
    additional_acquisition_costs: list[CostComponent]
    selling_costs: list[CostComponent]
    acquisition_unknowns: list[UnknownInput]
    selling_unknowns: list[UnknownInput]
    sensitivity_inputs: list[SensitivityInput]
    provenance: list[ProjectedFindingEvidence]
    assumptions: list[str]


def _failure(currency: str, reasons: list[str]) -> EconomicFindingProjection:
    return EconomicFindingProjection(
        valid=False,
        failure_reasons=list(dict.fromkeys(reasons)),
        currency=currency,
        unit_purchase_price=None,
        purchase_price_basis=None,
        additional_acquisition_costs=[],
        selling_costs=[],
        acquisition_unknowns=[],
        selling_unknowns=[],
        sensitivity_inputs=[],
        provenance=[],
        assumptions=[],
    )


def _duplicate_ids(findings: list[EconomicCostFinding]) -> list[str]:
    seen: set[str] = set()
    duplicates: list[str] = []
    for finding in findings:
        if finding.cost_id in seen and finding.cost_id not in duplicates:
            duplicates.append(finding.cost_id)
        seen.add(finding.cost_id)
    return duplicates


def _currency_error(finding: EconomicCostFinding, currency: str) -> str | None:
    if finding.cost_type == CostType.PERCENT_OF_UNIT_PRICE:
        if finding.currency not in (None, currency):
            return (
                f"{finding.cost_id} has currency {finding.currency}; percentage costs "
                f"must be dimensionless or use {currency}"
            )
        return None
    if finding.currency != currency:
        return (
            f"{finding.cost_id} requires currency {currency}, not "
            f"{finding.currency or 'None'}"
        )
    return None


def _unknown(finding: EconomicCostFinding) -> UnknownInput:
    detail = [*finding.limitations]
    if finding.notes:
        detail.append(finding.notes)
    return UnknownInput(
        name=finding.name,
        materiality=finding.unresolved_materiality,
        notes=" ".join(detail) or None,
        related_cost_ids=[finding.cost_id],
    )


def _project_side(
    findings: list[EconomicCostFinding],
    side: ScenarioSide,
    currency: str,
) -> tuple[
    list[CostComponent],
    list[UnknownInput],
    list[SensitivityInput],
    list[ProjectedFindingEvidence],
    list[str],
    list[str],
]:
    costs: list[CostComponent] = []
    unknowns: list[UnknownInput] = []
    sensitivities: list[SensitivityInput] = []
    provenance: list[ProjectedFindingEvidence] = []
    assumptions: list[str] = []
    failures: list[str] = []

    for finding in findings:
        currency_error = _currency_error(finding, currency)
        if currency_error:
            failures.append(currency_error)
            continue
        if finding.value is not None and finding.modeled_value is not None:
            failures.append(
                f"{finding.cost_id} has both an exact value and a modeled value"
            )
            continue
        if finding.value is not None and finding.unresolved_materiality is not None:
            failures.append(
                f"{finding.cost_id} has both an exact value and unresolved materiality"
            )
            continue

        baseline = None
        baseline_basis = None
        if finding.value is not None:
            baseline = finding.value
            baseline_basis = finding.basis
        elif finding.estimated_low is not None:
            if finding.modeled_value is not None:
                baseline = finding.modeled_value
                baseline_basis = finding.modeled_value_basis
                assumptions.append(
                    f"{side.value}/{finding.cost_id} uses the explicit modeled value "
                    "as its ASSUMED baseline."
                )
            else:
                baseline = finding.estimated_high
                baseline_basis = InputBasis.ESTIMATED
                assumptions.append(
                    f"{side.value}/{finding.cost_id} uses the high endpoint of the "
                    "estimated cost range as its conservative baseline."
                )
            sensitivities.append(
                SensitivityInput(
                    cost_id=finding.cost_id,
                    scenario_side=side,
                    low_value=finding.estimated_low,
                    high_value=finding.estimated_high,
                )
            )
        elif finding.modeled_value is not None:
            baseline = finding.modeled_value
            baseline_basis = finding.modeled_value_basis
            assumptions.append(
                f"{side.value}/{finding.cost_id} uses an explicit ASSUMED modeled value."
            )

        if baseline is not None:
            costs.append(
                CostComponent(
                    cost_id=finding.cost_id,
                    name=finding.name,
                    cost_type=finding.cost_type,
                    value=baseline,
                    basis=baseline_basis,
                    notes=finding.notes,
                )
            )
        if finding.unresolved_materiality is not None:
            unknowns.append(_unknown(finding))
        provenance.append(
            ProjectedFindingEvidence(
                scenario_side=side,
                cost_id=finding.cost_id,
                source_references=finding.source_references,
                limitations=finding.limitations,
                notes=finding.notes,
            )
        )
    return costs, unknowns, sensitivities, provenance, assumptions, failures


def project_economic_findings(
    acquisition_findings: EconomicCostFindingsResult,
    selling_findings: EconomicCostFindingsResult,
    *,
    currency: str,
) -> EconomicFindingProjection:
    """Project validated evidence into Profitability-compatible economic inputs."""
    if not currency.strip():
        return _failure(currency, ["currency must not be blank"])

    acquisition = list(acquisition_findings.findings)
    selling = list(selling_findings.findings)
    failures: list[str] = []
    for side_name, findings in (("acquisition", acquisition), ("selling", selling)):
        duplicates = _duplicate_ids(findings)
        if duplicates:
            failures.append(
                f"{side_name} findings contain duplicate cost_id values: {duplicates}"
            )

    purchase_matches = [
        finding for finding in acquisition if finding.cost_id == PURCHASE_PRICE_COST_ID
    ]
    if not purchase_matches:
        failures.append(
            f"acquisition findings require exactly one {PURCHASE_PRICE_COST_ID} finding"
        )
    elif len(purchase_matches) > 1:
        failures.append(
            f"acquisition findings contain multiple {PURCHASE_PRICE_COST_ID} findings"
        )
    if failures:
        return _failure(currency, failures)

    purchase = purchase_matches[0]
    if (
        purchase.cost_type != CostType.FIXED_PER_UNIT
        or purchase.value is None
        or purchase.basis is None
        or purchase.estimated_low is not None
        or purchase.modeled_value is not None
        or purchase.unresolved_materiality is not None
    ):
        failures.append(
            f"{PURCHASE_PRICE_COST_ID} must be one resolved FIXED_PER_UNIT exact value"
        )
    purchase_currency_error = _currency_error(purchase, currency)
    if purchase_currency_error:
        failures.append(purchase_currency_error)
    if failures:
        return _failure(currency, failures)

    additional_acquisition = [
        finding for finding in acquisition if finding.cost_id != PURCHASE_PRICE_COST_ID
    ]
    (
        acquisition_costs,
        acquisition_unknowns,
        acquisition_sensitivities,
        acquisition_provenance,
        acquisition_assumptions,
        acquisition_failures,
    ) = _project_side(additional_acquisition, ScenarioSide.ACQUISITION, currency)
    (
        selling_costs,
        selling_unknowns,
        selling_sensitivities,
        selling_provenance,
        selling_assumptions,
        selling_failures,
    ) = _project_side(selling, ScenarioSide.SALE, currency)
    failures.extend(acquisition_failures)
    failures.extend(selling_failures)
    if failures:
        return _failure(currency, failures)

    purchase_provenance = ProjectedFindingEvidence(
        scenario_side=ScenarioSide.ACQUISITION,
        cost_id=purchase.cost_id,
        source_references=purchase.source_references,
        limitations=purchase.limitations,
        notes=purchase.notes,
    )
    return EconomicFindingProjection(
        valid=True,
        failure_reasons=[],
        currency=currency,
        unit_purchase_price=purchase.value,
        purchase_price_basis=purchase.basis,
        additional_acquisition_costs=acquisition_costs,
        selling_costs=selling_costs,
        acquisition_unknowns=acquisition_unknowns,
        selling_unknowns=selling_unknowns,
        sensitivity_inputs=[
            *acquisition_sensitivities,
            *selling_sensitivities,
        ],
        provenance=[
            purchase_provenance,
            *acquisition_provenance,
            *selling_provenance,
        ],
        assumptions=[*acquisition_assumptions, *selling_assumptions],
    )
