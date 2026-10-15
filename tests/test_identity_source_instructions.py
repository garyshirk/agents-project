import ast
import asyncio
import inspect
import io
import json
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from agents import RunContextWrapper, Runner
from agents.agent_output import AgentOutputSchema

from arbitrage import application, orchestration
from arbitrage.contracts import (
    ProductAudience, ProductIdentity, ProductPackaging, ProductSizeSystem,
    ResaleEvidenceItem, ResaleEvidenceSourceKind, ResaleRequest, ResaleResult,
)
from arbitrage.lead_routing import LEAD_ROUTING_TOOLS, route_candidate_ready
from arbitrage.specialists.resale import resale_agent
from tests import test_candidate_persistence as fixtures


class IdentitySourceInstructionTests(unittest.TestCase):
    def assert_guidance(self, instructions, requirements):
        text = " ".join(instructions.split()).casefold()
        for requirement in requirements:
            with self.subTest(requirement=requirement):
                self.assertIn(requirement.casefold(), text)

    def test_lead_structured_fields_and_raw_variant_preservation(self):
        self.assert_guidance(orchestration.lead_qualifier.instructions, (
            "preserve the user's original variant description", "raw variant field",
            "populate material_variant", "explicit user information",
            "colorway, audience, size_value, size_system, width, and packaging",
            "leave unknown attributes null", "do not require all six attributes",
        ))

    def test_lead_identifiers_and_sizing_cannot_be_invented(self):
        self.assert_guidance(orchestration.lead_qualifier.instructions, (
            "preserve explicit model_number and upc_gtin", "never manufacture model numbers or UPC/GTIN",
            "never infer a sizing system from a number", "solely from a product name",
            "preserve size_value and leave size_system null",
            "do not turn an ambiguous variant into a confidently identified product",
        ))

    def test_lead_existing_clarification_and_authority_preserved(self):
        self.assert_guidance(orchestration.lead_qualifier.instructions, (
            "existing targeted clarification workflow", "materially missing identity",
            "optional unknowns alone do not require clarification",
            "Candidate authority and persistence remain application-owned",
            "route_needs_more_info", "route_existing_candidate", "select exactly one",
            "distinguish packaging from condition",
        ))

    def test_resale_researched_identity_is_grounded_separately_from_candidate(self):
        self.assert_guidance(resale_agent.instructions, (
            "ResaleResult.researched_identity", "original raw variant description",
            "populate material_variant using research evidence",
            "Candidate identity is a search target, not independent research verification",
            "do not copy Candidate attributes", "as if independently verified",
            "never fabricate matching identifiers", "silently reconcile a researched variant",
            "report actual researched condition and package_quantity",
            "distinguish condition from packaging", "Similar product names alone do not establish",
        ))

    def test_resale_nullable_schema_and_deterministic_policy_guidance(self):
        self.assert_guidance(resale_agent.instructions, (
            "explicit nulls for unknown attributes", "do not omit required-but-nullable fields",
            "without requiring all six attributes", "numeric size alone does not establish",
            "do not infer audience, size system, colorway, width, or packaging without evidence",
            "deterministic comparator and acceptance policy, not your confidence alone",
            "identity_match and evidence-item match_quality semantics",
        ))

    def test_exact_material_variant_enum_values_are_in_effective_instructions(self):
        for instructions in (orchestration.lead_qualifier.instructions, resale_agent.instructions):
            for enum in (ProductAudience, ProductSizeSystem, ProductPackaging):
                self.assert_guidance(instructions, [member.value for member in enum])

    def test_resale_source_identity_and_shoe_palace_example(self):
        self.assert_guidance(resale_agent.instructions, (
            "source_kind and source_name", "where the evidence was actually found",
            "distinct from the intended resale destination", "evidence_type, and the source URL",
            "Shoe Palace is RETAILER with source_name 'Shoe Palace'",
            "eBay is DESTINATION_MARKETPLACE", "StockX is OTHER_RESALE_MARKETPLACE",
            "Nike's own website is MANUFACTURER",
            "Never label Shoe Palace as eBay merely because",
            "RETAIL_REFERENCE remains distinct from destination resale evidence",
        ))
        self.assert_guidance(resale_agent.instructions,
                             [member.value for member in ResaleEvidenceSourceKind])

    def test_resale_prices_sources_and_uncertainty_remain_limited_by_evidence(self):
        self.assert_guidance(resale_agent.instructions, (
            "retail asking price is not an achieved resale transaction price",
            "active listing is not a completed sale", "Never invent source URLs",
            "never invent a verification basis", "realized transaction price are established",
            "infer authenticity solely from a plausible-looking hostname",
            "Agent-authored source labels are not independently verified",
            "Use null or UNKNOWN", "source_name null when unknown",
            "Preserve contradictions, limitations, and uncertainty",
            "individual evidence URL directly supports an aggregate",
            "current contract does not establish that linkage",
        ))

    def test_application_wires_constructed_lead_and_existing_routing_tools(self):
        tree = ast.parse(inspect.getsource(application.main))
        imports = [node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        self.assertTrue(any(node.module == "arbitrage.orchestration" and
                            "lead_qualifier" in [name.name for name in node.names] for node in imports))
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute) and node.func.attr == "run_sync"]
        self.assertTrue(any(call.args and isinstance(call.args[0], ast.Name)
                            and call.args[0].id == "lead_qualifier" for call in calls))
        self.assertIsNone(orchestration.lead_qualifier.output_type)
        self.assertEqual(orchestration.lead_qualifier.tools[1:], LEAD_ROUTING_TOOLS)

    def test_agent_tool_uses_actual_constructed_resale_agent_with_mock_runner(self):
        request = ResaleRequest(
            product_identity=fixtures.CandidatePersistenceTests.identity(),
            identity_confidence="STRONG", identity_uncertainty=None,
            research_constraints=[], research_objectives=[], sourcing_context=None,
            additional_context=None,
        )
        result = fixtures.CandidatePersistenceTests().resale_result()
        run_result = SimpleNamespace(
            final_output=result, final_output_as=Mock(return_value=result), interruptions=[]
        )
        with patch.object(Runner, "run", new_callable=AsyncMock, return_value=run_result) as run:
            candidate = SimpleNamespace(candidate_id="candidate", product_identity=request.product_identity)
            from arbitrage.contracts import EvaluationStatus
            evaluation = SimpleNamespace(evaluation_id="evaluation", status=EvaluationStatus.IN_PROGRESS)
            workflow = SimpleNamespace(_active_records=Mock(return_value=(candidate, evaluation)))
            intent = request.model_dump(mode="json", exclude={"product_identity"})
            with redirect_stdout(io.StringIO()):
                asyncio.run(orchestration.resale_agent_tool.on_invoke_tool(
                    RunContextWrapper(context=SimpleNamespace(candidate_workflow=workflow)), json.dumps(intent)
                ))
        run.assert_awaited_once()
        self.assertIs(run.call_args.kwargs["starting_agent"], resale_agent)
        self.assertIs(orchestration.resale_agent, resale_agent)
        self.assertIn(orchestration.resale_agent_tool, orchestration.agent.tools)

    def test_actual_strict_lead_tool_and_resale_output_schemas(self):
        self.assertTrue(route_candidate_ready.strict_json_schema)
        lead_schema = route_candidate_ready.params_json_schema
        self.assertIn("material_variant", lead_schema["$defs"]["ProductIdentity"]["required"])
        output_schema = AgentOutputSchema(resale_agent.output_type)
        self.assertTrue(output_schema.is_strict_json_schema())
        document = output_schema.json_schema()
        self.assertIs(resale_agent.output_type, ResaleResult)
        for field in ("source_kind", "source_name"):
            evidence = document["$defs"]["ResaleEvidenceItem"]
            self.assertIn(field, evidence["required"])
            self.assertIn({"type": "null"}, evidence["properties"][field]["anyOf"])

    def test_historical_readability_and_no_external_access(self):
        identity = fixtures.CandidatePersistenceTests.identity().model_dump(mode="json")
        identity.pop("material_variant")
        from tests.test_resale_evidence_source_contract import historical_item
        with patch("socket.socket", side_effect=AssertionError("network")), \
             patch("sqlite3.connect", side_effect=AssertionError("database")), \
             patch.object(Runner, "run", side_effect=AssertionError("model")), \
             patch.object(Runner, "run_sync", side_effect=AssertionError("model")):
            self.assertIsNone(ProductIdentity.model_validate(identity).material_variant)
            item = ResaleEvidenceItem.model_validate(historical_item())
            self.assertIsNone(item.source_kind)
            self.assertIsNone(item.source_name)
            AgentOutputSchema(resale_agent.output_type).json_schema()


if __name__ == "__main__":
    unittest.main()
