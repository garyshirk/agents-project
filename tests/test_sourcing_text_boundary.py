import asyncio
import inspect
import io
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace

from arbitrage.contracts import (
    ManagerEvaluationJudgment,
    ResaleRequest,
    ResaleResult,
    SourcingRequest,
)
from arbitrage.orchestration import (
    agent,
    lead_qualifier,
    resale_agent_tool,
    sourcing_agent_tool,
    sourcing_tool_input,
    sourcing_tool_output,
)
from arbitrage.specialists.resale import resale_agent
from arbitrage.specialists.sourcing import sourcing_agent


EXPECTED_TEXT_INPUT_SCHEMA = {
    "description": "Default input schema for agent-as-tool calls.",
    "properties": {"input": {"title": "Input", "type": "string"}},
    "required": ["input"],
    "title": "AgentAsToolInput",
    "type": "object",
    "additionalProperties": False,
}


class SourcingTextBoundaryTests(unittest.TestCase):
    def test_sourcing_uses_plain_text_output_and_default_text_input(self):
        self.assertIsNone(sourcing_agent.output_type)
        self.assertEqual(sourcing_agent_tool.params_json_schema, EXPECTED_TEXT_INPUT_SCHEMA)
        self.assertNotEqual(
            sourcing_agent_tool.params_json_schema["title"], SourcingRequest.__name__
        )
        self.assertEqual([tool.name for tool in sourcing_agent.tools], ["web_search"])
        self.assertTrue(sourcing_agent.tools[0].external_web_access)

    def test_sourcing_instructions_require_natural_language_not_json(self):
        instructions = sourcing_agent.instructions
        self.assertIsInstance(instructions, str)
        self.assertIn("natural-language research report", instructions)
        self.assertIn("do not emit JSON or imitate a data schema", instructions)
        for requirement in (
            "exact product, model, and variant",
            "identity uncertainty",
            "source distinct from the seller",
            "direct source URL",
            "shipping and other acquisition costs",
            "availability",
            "human-observed facts",
            "unresolved issues",
        ):
            self.assertIn(requirement, instructions)

    def test_sourcing_builder_and_extractor_pass_plain_text(self):
        with redirect_stdout(io.StringIO()) as output:
            delegated = sourcing_tool_input(
                {"params": {"input": "Research this exact product."}}
            )
        self.assertEqual(delegated, "Research this exact product.")
        self.assertIn("Sourcing Agent tool entered", output.getvalue())

        with redirect_stdout(io.StringIO()) as output:
            result = asyncio.run(
                sourcing_tool_output(SimpleNamespace(final_output="Evidence report"))
            )
        self.assertEqual(result, "Evidence report")
        self.assertIn("Sourcing Agent called as tool", output.getvalue())

    def test_other_structured_boundaries_remain_unchanged(self):
        self.assertIs(resale_agent.output_type, ResaleResult)
        self.assertEqual(resale_agent_tool.params_json_schema["title"], "ResaleRequest")
        self.assertEqual(
            set(resale_agent_tool.params_json_schema["properties"]),
            set(ResaleRequest.model_fields),
        )
        self.assertIsNone(lead_qualifier.output_type)
        self.assertIs(agent.output_type, ManagerEvaluationJudgment)
        self.assertEqual(
            [tool.name for tool in agent.tools],
            [
                "consult_sourcing_agent",
                "consult_resale_agent",
                "calculate_profitability",
                "record_acquisition_cost_findings",
                "record_selling_cost_findings",
            ],
        )

    def test_no_retry_repair_regex_or_fallback_parser_was_added(self):
        self.assertIsNone(sourcing_agent.model_settings.retry)
        production_source = "\n".join(
            inspect.getsource(item)
            for item in (
                sourcing_tool_input,
                sourcing_tool_output,
            )
        ).lower()
        for forbidden in ("retry", "repair", "regex", "fallback", "model_validate_json"):
            self.assertNotIn(forbidden, production_source)


if __name__ == "__main__":
    unittest.main()
