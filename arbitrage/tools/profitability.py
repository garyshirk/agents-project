from decimal import ROUND_HALF_EVEN, Decimal

from agents import RunContextWrapper, function_tool

from arbitrage.contracts import (
    AcquisitionScenario,
    CalculatedCost,
    CostComponent,
    CostType,
    InputBasis,
    InputQualitySummary,
    ProfitabilityRequest,
    ProfitabilityResult,
    ProfitabilityStatus,
    ProfitabilityToolResult,
    ProfitScenarioResult,
    SaleScenario,
    ScenarioSide,
    SensitivityResult,
    UnknownInput,
    UnknownMateriality,
)


MONEY_QUANTUM = Decimal("0.01")
PERCENT_QUANTUM = Decimal("0.01")
ONE_HUNDRED = Decimal("100")


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)


def _percent(value: Decimal) -> Decimal:
    return value.quantize(PERCENT_QUANTUM, rounding=ROUND_HALF_EVEN)


def _deduplicate(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _calculate_cost(
    cost: CostComponent,
    quantity: int,
    unit_price: Decimal,
) -> Decimal:
    if cost.cost_type == CostType.FIXED_PER_UNIT:
        return cost.value * quantity
    if cost.cost_type == CostType.FIXED_PER_BATCH:
        return cost.value
    return unit_price * quantity * (cost.value / ONE_HUNDRED)


def _rounded_breakdown(
    costs_and_amounts: list[tuple[CostComponent, Decimal]],
    rounded_total: Decimal,
) -> list[CalculatedCost]:
    breakdown = [
        CalculatedCost(
            cost_id=cost.cost_id,
            name=cost.name,
            cost_type=cost.cost_type,
            input_value=cost.value,
            calculated_amount=_money(amount),
        )
        for cost, amount in costs_and_amounts
    ]
    if breakdown:
        # Reconcile any aggregate rounding residual without using rounded rows in arithmetic.
        residual = rounded_total - sum(
            (item.calculated_amount for item in breakdown), Decimal("0")
        )
        breakdown[-1].calculated_amount += residual
    return breakdown


def _unrounded_net_profit(
    acquisition: AcquisitionScenario,
    sale: SaleScenario,
    resale_price: Decimal,
) -> Decimal:
    quantity = acquisition.quantity
    total_acquisition_cost = acquisition.unit_purchase_price * quantity + sum(
        (
            _calculate_cost(cost, quantity, acquisition.unit_purchase_price)
            for cost in acquisition.additional_costs
        ),
        Decimal("0"),
    )
    total_selling_cost = sum(
        (_calculate_cost(cost, quantity, resale_price) for cost in sale.selling_costs),
        Decimal("0"),
    )
    return resale_price * quantity - total_selling_cost - total_acquisition_cost


def _calculate_scenario(
    acquisition: AcquisitionScenario,
    sale: SaleScenario,
    resale_price: Decimal,
    warnings: list[str],
) -> ProfitScenarioResult:
    quantity = acquisition.quantity
    gross_purchase_cost = acquisition.unit_purchase_price * quantity
    acquisition_amounts = [
        (cost, _calculate_cost(cost, quantity, acquisition.unit_purchase_price))
        for cost in acquisition.additional_costs
    ]
    additional_acquisition_cost = sum(
        (amount for _, amount in acquisition_amounts), Decimal("0")
    )
    total_acquisition_cost = gross_purchase_cost + additional_acquisition_cost
    effective_acquisition_cost_per_unit = total_acquisition_cost / quantity

    gross_revenue = resale_price * quantity
    selling_amounts = [
        (cost, _calculate_cost(cost, quantity, resale_price))
        for cost in sale.selling_costs
    ]
    total_selling_cost = sum(
        (amount for _, amount in selling_amounts), Decimal("0")
    )
    net_sale_proceeds = gross_revenue - total_selling_cost
    net_profit = net_sale_proceeds - total_acquisition_cost
    profit_per_unit = net_profit / quantity

    if total_acquisition_cost == 0:
        roi_percent = None
        warnings.append("ROI is unavailable because total acquisition cost is zero.")
    else:
        roi_percent = net_profit / total_acquisition_cost * ONE_HUNDRED

    if gross_revenue == 0:
        profit_margin_percent = None
        warnings.append("Profit margin is unavailable because gross revenue is zero.")
    else:
        profit_margin_percent = net_profit / gross_revenue * ONE_HUNDRED

    # Aggregate using full precision and quantize only the public result fields.
    return ProfitScenarioResult(
        resale_price_per_unit=_money(resale_price),
        gross_revenue=_money(gross_revenue),
        gross_purchase_cost=_money(gross_purchase_cost),
        acquisition_cost_breakdown=_rounded_breakdown(
            acquisition_amounts, _money(additional_acquisition_cost)
        ),
        additional_acquisition_cost=_money(additional_acquisition_cost),
        total_acquisition_cost=_money(total_acquisition_cost),
        effective_acquisition_cost_per_unit=_money(effective_acquisition_cost_per_unit),
        selling_cost_breakdown=_rounded_breakdown(
            selling_amounts, _money(total_selling_cost)
        ),
        total_selling_cost=_money(total_selling_cost),
        net_sale_proceeds=_money(net_sale_proceeds),
        net_profit=_money(net_profit),
        profit_per_unit=_money(profit_per_unit),
        roi_percent=None if roi_percent is None else _percent(roi_percent),
        profit_margin_percent=(
            None if profit_margin_percent is None else _percent(profit_margin_percent)
        ),
    )


def _quality_label(side: str, unknown: UnknownInput) -> str:
    label = f"{side}.unknowns[{unknown.name}]"
    return f"{label}: {unknown.notes}" if unknown.notes else label


def _build_input_quality(request: ProfitabilityRequest) -> InputQualitySummary:
    classified: dict[InputBasis, list[str]] = {basis: [] for basis in InputBasis}

    classified[request.acquisition.purchase_price_basis].append(
        "acquisition.unit_purchase_price"
    )
    for cost in request.acquisition.additional_costs:
        classified[cost.basis].append(
            f"acquisition.additional_costs[{cost.cost_id}]"
        )
    classified[request.sale.resale_price_basis].append("sale.resale_price_range")
    for cost in request.sale.selling_costs:
        classified[cost.basis].append(f"sale.selling_costs[{cost.cost_id}]")
    if request.acquisition.quantity_available is not None:
        assert request.acquisition.quantity_available_basis is not None
        classified[request.acquisition.quantity_available_basis].append(
            "acquisition.quantity_available"
        )

    assumed_inputs = classified[InputBasis.ASSUMED] + [
        *(f"acquisition.assumptions: {item}" for item in request.acquisition.assumptions),
        *(f"sale.assumptions: {item}" for item in request.sale.assumptions),
    ]
    unknown_inputs = [
        *(
            _quality_label("acquisition", unknown)
            for unknown in request.acquisition.unknowns
        ),
        *(_quality_label("sale", unknown) for unknown in request.sale.unknowns),
    ]

    return InputQualitySummary(
        human_observed_inputs=_deduplicate(classified[InputBasis.HUMAN_OBSERVED]),
        verified_inputs=_deduplicate(classified[InputBasis.VERIFIED]),
        calculated_inputs=_deduplicate(classified[InputBasis.CALCULATED]),
        estimated_inputs=_deduplicate(classified[InputBasis.ESTIMATED]),
        assumed_inputs=_deduplicate(assumed_inputs),
        unknown_inputs=_deduplicate(unknown_inputs),
    )


def _unknown_warning(side: str, unknown: UnknownInput, material: bool) -> str:
    detail = f" ({unknown.notes})" if unknown.notes else ""
    if material:
        return f"Material economic input is unknown: {side}.{unknown.name}{detail}."
    return f"Non-material economic input was omitted: {side}.{unknown.name}{detail}."


def _all_unknowns(request: ProfitabilityRequest) -> list[tuple[str, UnknownInput]]:
    return [
        *(("acquisition", unknown) for unknown in request.acquisition.unknowns),
        *(("sale", unknown) for unknown in request.sale.unknowns),
    ]


def _replace_cost_value(
    request: ProfitabilityRequest,
    side: ScenarioSide,
    cost_id: str,
    value: Decimal,
) -> tuple[AcquisitionScenario, SaleScenario]:
    acquisition = request.acquisition.model_copy(deep=True)
    sale = request.sale.model_copy(deep=True)
    costs = (
        acquisition.additional_costs
        if side == ScenarioSide.ACQUISITION
        else sale.selling_costs
    )
    for index, cost in enumerate(costs):
        if cost.cost_id == cost_id:
            costs[index] = cost.model_copy(update={"value": value})
            break
    return acquisition, sale


def _calculate_profitability(request: ProfitabilityRequest) -> ProfitabilityResult:
    warnings: list[str] = []
    unknowns = _all_unknowns(request)
    material_unknowns = [
        item for item in unknowns if item[1].materiality == UnknownMateriality.MATERIAL
    ]

    if material_unknowns:
        warnings.extend(
            _unknown_warning(side, unknown, material=True)
            for side, unknown in material_unknowns
        )
        return ProfitabilityResult(
            status=ProfitabilityStatus.INSUFFICIENT_INPUTS,
            currency=request.acquisition.currency,
            quantity=request.acquisition.quantity,
            low_case=None,
            expected_case=None,
            high_case=None,
            input_quality=_build_input_quality(request),
            sensitivity_results=[],
            warnings=_deduplicate(warnings),
            notes=None,
        )

    status = ProfitabilityStatus.PARTIAL if unknowns else ProfitabilityStatus.COMPLETE
    warnings.extend(
        _unknown_warning(side, unknown, material=False) for side, unknown in unknowns
    )

    low_case = _calculate_scenario(
        request.acquisition, request.sale, request.sale.resale_price_low, warnings
    )
    expected_case = (
        None
        if request.sale.resale_price_expected is None
        else _calculate_scenario(
            request.acquisition,
            request.sale,
            request.sale.resale_price_expected,
            warnings,
        )
    )
    high_case = _calculate_scenario(
        request.acquisition, request.sale, request.sale.resale_price_high, warnings
    )

    sensitivity_results: list[SensitivityResult] = []
    if request.sensitivity_inputs and expected_case is None:
        warnings.append(
            "Sensitivity analysis was skipped because no expected resale price was supplied."
        )
    elif expected_case is not None:
        assert request.sale.resale_price_expected is not None
        for sensitivity in request.sensitivity_inputs:
            source_costs = (
                request.acquisition.additional_costs
                if sensitivity.scenario_side == ScenarioSide.ACQUISITION
                else request.sale.selling_costs
            )
            source_cost = next(
                cost for cost in source_costs if cost.cost_id == sensitivity.cost_id
            )
            low_acquisition, low_sale = _replace_cost_value(
                request,
                sensitivity.scenario_side,
                sensitivity.cost_id,
                sensitivity.low_value,
            )
            high_acquisition, high_sale = _replace_cost_value(
                request,
                sensitivity.scenario_side,
                sensitivity.cost_id,
                sensitivity.high_value,
            )
            low_value_case = _calculate_scenario(
                low_acquisition,
                low_sale,
                request.sale.resale_price_expected,
                warnings,
            )
            high_value_case = _calculate_scenario(
                high_acquisition,
                high_sale,
                request.sale.resale_price_expected,
                warnings,
            )
            original_net_profit = _unrounded_net_profit(
                request.acquisition,
                request.sale,
                request.sale.resale_price_expected,
            )
            low_net_profit = _unrounded_net_profit(
                low_acquisition,
                low_sale,
                request.sale.resale_price_expected,
            )
            high_net_profit = _unrounded_net_profit(
                high_acquisition,
                high_sale,
                request.sale.resale_price_expected,
            )
            sensitivity_results.append(
                SensitivityResult(
                    cost_id=source_cost.cost_id,
                    cost_name=source_cost.name,
                    scenario_side=sensitivity.scenario_side,
                    original_value=source_cost.value,
                    low_value=sensitivity.low_value,
                    high_value=sensitivity.high_value,
                    low_value_case=low_value_case,
                    high_value_case=high_value_case,
                    net_profit_change_low=_money(low_net_profit - original_net_profit),
                    net_profit_change_high=_money(high_net_profit - original_net_profit),
                )
            )

    return ProfitabilityResult(
        status=status,
        currency=request.acquisition.currency,
        quantity=request.acquisition.quantity,
        low_case=low_case,
        expected_case=expected_case,
        high_case=high_case,
        input_quality=_build_input_quality(request),
        sensitivity_results=sensitivity_results,
        warnings=_deduplicate(warnings),
        notes=None,
    )


def profitability_tool_error(
    _context: RunContextWrapper[None], error: Exception
) -> str:
    return ProfitabilityToolResult(
        success=False,
        result=None,
        error=(
            f"{error}. Correct the Profitability request and retry when appropriate; "
            "do not invent missing economic inputs. If one economic input is both "
            "unresolved and an assumed zero-valued cost, remove the zero placeholder "
            "and preserve the unknown."
        ),
    ).model_dump_json()


@function_tool(
    output_type=ProfitabilityToolResult,
    failure_error_function=profitability_tool_error,
)
def calculate_profitability(request: ProfitabilityRequest) -> ProfitabilityToolResult:
    """Calculate deterministic economics for one acquisition and sale scenario pairing."""
    print("[debug] Profitability Tool called")
    print(f"[debug] Profitability validated request: {request.model_dump_json()}")
    result = _calculate_profitability(request)
    print(f"[debug] Profitability deterministic result: {result.model_dump_json()}")
    return ProfitabilityToolResult(
        success=True,
        result=result,
        error=None,
    )
