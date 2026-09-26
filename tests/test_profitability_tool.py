import asyncio
import contextlib
import io
import json
import unittest

from agents.items import ItemHelpers
from agents.tool_context import ToolContext
from openai.types.responses import ResponseFunctionToolCall

from arbitrage.contracts import (
    AcquisitionScenario,
    CostComponent,
    CostType,
    InputBasis,
    ProductCondition,
    ProductIdentity,
    ProfitabilityRequest,
    ProfitabilityStatus,
    ProfitabilityToolResult,
    SaleScenario,
    SourceType,
    UnknownInput,
    UnknownMateriality,
)
from arbitrage.tools.profitability import calculate_profitability


class ProfitabilityFunctionToolTests(unittest.TestCase):
    @staticmethod
    def request() -> ProfitabilityRequest:
        identity = ProductIdentity(
            brand="Nike",
            product_name="Pegasus 41",
            model_number="FD2722-001",
            upc_gtin=None,
            variant="Men's size 10",
            condition=ProductCondition.NEW,
            package_quantity=1,
        )
        return ProfitabilityRequest(
            acquisition=AcquisitionScenario(
                product_identity=identity,
                source_type=SourceType.PHYSICAL_RETAILER,
                source_name="Ross",
                source_references=[],
                condition=ProductCondition.NEW,
                currency="USD",
                unit_purchase_price="39.99",
                purchase_price_basis=InputBasis.VERIFIED,
                quantity=1,
                additional_costs=[],
                quantity_available=None,
                quantity_available_basis=None,
                purchase_limit=None,
                purchase_requirements=[],
                assumptions=[],
                unknowns=[],
                notes=None,
            ),
            sale=SaleScenario(
                marketplace_name="eBay",
                marketplace_references=[],
                currency="USD",
                target_condition=ProductCondition.NEW,
                resale_price_low="80",
                resale_price_expected="90",
                resale_price_high="100",
                resale_price_basis=InputBasis.ESTIMATED,
                selling_costs=[],
                quantity=1,
                assumptions=[],
                unknowns=[],
                notes=None,
            ),
            sensitivity_inputs=[],
        )

    @staticmethod
    def invoke(arguments: str) -> ProfitabilityToolResult:
        tool_call = ResponseFunctionToolCall(
            arguments=arguments,
            call_id="offline-profitability-call",
            name="calculate_profitability",
            type="function_call",
        )
        context = ToolContext(
            None,
            tool_name=calculate_profitability.name,
            tool_call_id=tool_call.call_id,
            tool_arguments=arguments,
            tool_call=tool_call,
        )
        output = asyncio.run(calculate_profitability.on_invoke_tool(context, arguments))
        raw_item = ItemHelpers.tool_call_output_item(
            tool_call,
            output,
            output_json_schema=calculate_profitability.output_json_schema,
            output_type_adapter=calculate_profitability._output_type_adapter,
        )
        return ProfitabilityToolResult.model_validate_json(raw_item["output"])

    @classmethod
    def nike_regression_request(cls) -> ProfitabilityRequest:
        values = cls.request().model_dump(mode="json")
        values["acquisition"]["additional_costs"] = [
            CostComponent(
                cost_id="purchase_tax",
                name="Purchase tax",
                cost_type=CostType.PERCENT_OF_UNIT_PRICE,
                value="8",
                basis=InputBasis.ESTIMATED,
                notes=None,
            ).model_dump(mode="json")
        ]
        values["sale"].update(
            resale_price_low="84",
            resale_price_expected="89.99",
            resale_price_high="109.97",
            selling_costs=[
                CostComponent(
                    cost_id="ebay_fee",
                    name="eBay selling fee",
                    cost_type=CostType.PERCENT_OF_UNIT_PRICE,
                    value="13.6",
                    basis=InputBasis.ESTIMATED,
                    notes=None,
                ).model_dump(mode="json"),
                CostComponent(
                    cost_id="outbound_shipping",
                    name="Outbound shipping",
                    cost_type=CostType.FIXED_PER_UNIT,
                    value="8",
                    basis=InputBasis.ESTIMATED,
                    notes=None,
                ).model_dump(mode="json"),
                CostComponent(
                    cost_id="packaging",
                    name="Packaging",
                    cost_type=CostType.FIXED_PER_UNIT,
                    value="1",
                    basis=InputBasis.ESTIMATED,
                    notes=None,
                ).model_dump(mode="json"),
            ],
        )
        return ProfitabilityRequest.model_validate(values)

    def test_successful_function_tool_call_preserves_calculation(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = self.invoke(
                json.dumps({"request": self.request().model_dump(mode="json")})
            )

        self.assertTrue(result.success)
        self.assertIsNone(result.error)
        self.assertEqual(result.result.status, ProfitabilityStatus.COMPLETE)
        self.assertEqual(str(result.result.expected_case.net_profit), "50.01")
        debug_output = output.getvalue()
        self.assertIn("[debug] Profitability validated request:", debug_output)
        self.assertIn('"unit_purchase_price":"39.99"', debug_output)
        self.assertIn('"resale_price_expected":"90"', debug_output)
        self.assertIn("[debug] Profitability deterministic result:", debug_output)
        self.assertIn('"net_profit":"50.01"', debug_output)

    def test_debug_logging_preserves_known_nike_regression(self):
        output = io.StringIO()
        request = self.nike_regression_request()
        with contextlib.redirect_stdout(output):
            result = self.invoke(
                json.dumps({"request": request.model_dump(mode="json")})
            )

        self.assertTrue(result.success)
        self.assertEqual(str(result.result.low_case.net_profit), "20.39")
        self.assertEqual(str(result.result.low_case.roi_percent), "47.20")
        self.assertEqual(str(result.result.expected_case.net_profit), "25.56")
        self.assertEqual(str(result.result.expected_case.roi_percent), "59.19")
        self.assertEqual(str(result.result.high_case.net_profit), "42.82")
        self.assertEqual(str(result.result.high_case.roi_percent), "99.16")
        debug_output = output.getvalue()
        self.assertIn('"value":"13.6"', debug_output)
        self.assertIn('"value":"8"', debug_output)
        self.assertIn('"effective_acquisition_cost_per_unit":"43.19"', debug_output)

    def test_human_observed_price_provenance_is_preserved(self):
        values = self.request().model_dump(mode="json")
        values["acquisition"]["purchase_price_basis"] = "HUMAN_OBSERVED"
        result = self.invoke(json.dumps({"request": values}))

        self.assertTrue(result.success)
        self.assertEqual(
            result.result.input_quality.human_observed_inputs,
            ["acquisition.unit_purchase_price"],
        )
        self.assertNotIn(
            "acquisition.unit_purchase_price",
            result.result.input_quality.verified_inputs,
        )
        self.assertNotIn(
            "acquisition.unit_purchase_price",
            result.result.input_quality.assumed_inputs,
        )

    def test_material_unknown_remains_insufficient_without_fake_zero_cost(self):
        values = self.request().model_dump(mode="json")
        values["sale"]["unknowns"] = [
            UnknownInput(
                name="outbound shipping",
                materiality=UnknownMateriality.MATERIAL,
                notes="Actual fulfillment cost is unresolved.",
            ).model_dump(mode="json")
        ]
        result = self.invoke(json.dumps({"request": values}))

        self.assertTrue(result.success)
        self.assertEqual(result.result.status, ProfitabilityStatus.INSUFFICIENT_INPUTS)
        self.assertIsNone(result.result.low_case)
        self.assertEqual(values["sale"]["selling_costs"], [])
        self.assertIn(
            "sale.unknowns[outbound shipping]",
            result.result.input_quality.unknown_inputs[0],
        )

    def test_known_zero_cost_remains_valid(self):
        values = self.request().model_dump(mode="json")
        values["sale"]["selling_costs"] = [
            CostComponent(
                cost_id="known_listing_fee",
                name="Known listing fee",
                cost_type=CostType.FIXED_PER_UNIT,
                value="0",
                basis=InputBasis.VERIFIED,
                notes="The applicable listing has no insertion fee.",
            ).model_dump(mode="json")
        ]
        result = self.invoke(json.dumps({"request": values}))

        self.assertTrue(result.success)
        self.assertEqual(result.result.status, ProfitabilityStatus.COMPLETE)
        self.assertEqual(str(result.result.expected_case.total_selling_cost), "0.00")

    def test_unknown_cost_cannot_also_be_an_assumed_zero_placeholder(self):
        values = self.request().model_dump(mode="json")
        values["sale"]["selling_costs"] = [
            CostComponent(
                cost_id="outbound_shipping",
                name="Shipping to buyer",
                cost_type=CostType.FIXED_PER_UNIT,
                value="0",
                basis=InputBasis.ASSUMED,
                notes="Placeholder",
            ).model_dump(mode="json")
        ]
        values["sale"]["unknowns"] = [
            UnknownInput(
                name="outbound shipping and packaging",
                materiality=UnknownMateriality.MATERIAL,
                notes="Actual fulfillment cost is unresolved.",
                related_cost_ids=["outbound_shipping"],
            ).model_dump(mode="json")
        ]

        result = self.invoke(json.dumps({"request": values}))

        self.assertFalse(result.success)
        self.assertIsNone(result.result)
        self.assertIn("both unresolved and an assumed zero-valued cost", result.error)
        self.assertIn("preserve the unknown", result.error)

    def test_purposeful_assumed_zero_without_contradictory_unknown_is_valid(self):
        values = self.request().model_dump(mode="json")
        values["sale"]["selling_costs"] = [
            CostComponent(
                cost_id="outbound_shipping",
                name="Shipping to buyer",
                cost_type=CostType.FIXED_PER_UNIT,
                value="0",
                basis=InputBasis.ASSUMED,
                notes="Purposeful conditional zero-shipping scenario.",
            ).model_dump(mode="json")
        ]

        result = self.invoke(json.dumps({"request": values}))

        self.assertTrue(result.success)
        self.assertEqual(result.result.status, ProfitabilityStatus.COMPLETE)

    def test_distinct_zero_packaging_and_unknown_shipping_are_valid(self):
        values = self.request().model_dump(mode="json")
        values["sale"]["selling_costs"] = [
            CostComponent(
                cost_id="packaging",
                name="Packaging",
                cost_type=CostType.FIXED_PER_UNIT,
                value="0",
                basis=InputBasis.HUMAN_OBSERVED,
                notes="Reusable packaging is already available.",
            ).model_dump(mode="json")
        ]
        values["sale"]["unknowns"] = [
            UnknownInput(
                name="outbound shipping",
                materiality=UnknownMateriality.MATERIAL,
                notes="Carrier cost is unresolved.",
                related_cost_ids=["outbound_shipping"],
            ).model_dump(mode="json")
        ]

        result = self.invoke(json.dumps({"request": values}))

        self.assertTrue(result.success)
        self.assertEqual(result.result.status, ProfitabilityStatus.INSUFFICIENT_INPUTS)

    def test_nonzero_conditional_fee_can_coexist_with_unknown_actual_fee(self):
        values = self.request().model_dump(mode="json")
        values["sale"]["selling_costs"] = [
            CostComponent(
                cost_id="ebay_fee",
                name="Illustrative eBay fee",
                cost_type=CostType.PERCENT_OF_UNIT_PRICE,
                value="13.6",
                basis=InputBasis.ASSUMED,
                notes="Conditional scenario assumption.",
            ).model_dump(mode="json")
        ]
        values["sale"]["unknowns"] = [
            UnknownInput(
                name="actual seller-specific eBay fee",
                materiality=UnknownMateriality.MATERIAL,
                notes="The applicable fee schedule is unresolved.",
                related_cost_ids=["ebay_fee"],
            ).model_dump(mode="json")
        ]

        result = self.invoke(json.dumps({"request": values}))

        self.assertTrue(result.success)
        self.assertEqual(result.result.status, ProfitabilityStatus.INSUFFICIENT_INPUTS)

    def test_acquisition_unknown_cannot_also_be_assumed_zero(self):
        values = self.request().model_dump(mode="json")
        values["acquisition"]["additional_costs"] = [
            CostComponent(
                cost_id="purchase_tax",
                name="Purchase sales tax",
                cost_type=CostType.PERCENT_OF_UNIT_PRICE,
                value="0",
                basis=InputBasis.ASSUMED,
                notes="Placeholder",
            ).model_dump(mode="json")
        ]
        values["acquisition"]["unknowns"] = [
            UnknownInput(
                name="purchase sales tax",
                materiality=UnknownMateriality.MATERIAL,
                notes="Actual purchase tax is unresolved.",
                related_cost_ids=["purchase_tax"],
            ).model_dump(mode="json")
        ]

        result = self.invoke(json.dumps({"request": values}))

        self.assertFalse(result.success)
        self.assertIsNone(result.result)
        self.assertIn("both unresolved and an assumed zero-valued cost", result.error)

    def test_invalid_function_tool_call_returns_schema_compatible_failure(self):
        arguments = {"request": self.request().model_dump(mode="json")}
        arguments["request"]["acquisition"]["quantity"] = 0

        result = self.invoke(json.dumps(arguments))

        self.assertFalse(result.success)
        self.assertIsNone(result.result)
        self.assertIn("Invalid JSON input", result.error)
        self.assertIn("do not invent missing economic inputs", result.error)


if __name__ == "__main__":
    unittest.main()
