import asyncio
import io
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

from diagnostics import plain_text_agent_workflow as diagnostic


class PlainTextAgentWorkflowTests(unittest.TestCase):
    def test_every_diagnostic_agent_has_plain_text_output(self):
        diagnostic.assert_plain_text_experiment()
        self.assertTrue(
            all(agent.output_type is None for agent in diagnostic.DIAGNOSTIC_AGENTS)
        )
        self.assertTrue(
            diagnostic.PRODUCTION_OUTPUT_CONTRACT_NAMES.isdisjoint(
                {
                    getattr(agent.output_type, "__name__", None)
                    for agent in diagnostic.DIAGNOSTIC_AGENTS
                }
            )
        )

    def test_specialist_tools_use_only_default_text_input_schema(self):
        expected = {
            "description": "Default input schema for agent-as-tool calls.",
            "properties": {"input": {"title": "Input", "type": "string"}},
            "required": ["input"],
            "title": "AgentAsToolInput",
            "type": "object",
            "additionalProperties": False,
        }
        self.assertEqual(diagnostic.sourcing_tool.params_json_schema, expected)
        self.assertEqual(diagnostic.resale_tool.params_json_schema, expected)

    def test_manager_has_only_the_two_diagnostic_specialist_tools(self):
        self.assertEqual(
            [tool.name for tool in diagnostic.manager_agent.tools],
            [
                "consult_diagnostic_sourcing_agent",
                "consult_diagnostic_resale_agent",
            ],
        )

    def test_specialists_retain_web_search(self):
        for agent in (diagnostic.sourcing_agent, diagnostic.resale_agent):
            self.assertEqual([tool.name for tool in agent.tools], ["web_search"])

    def test_diagnostic_has_no_session_or_persistence(self):
        summary = diagnostic.diagnostic_summary()
        self.assertIsNone(summary["session"])
        self.assertIsNone(summary["persistence"])
        self.assertTrue(summary["lead_qualifier_bypassed"])
        self.assertTrue(summary["production_manager_bypassed"])
        self.assertTrue(summary["candidate_workflow_bypassed"])

    def test_dry_run_does_not_invoke_runner(self):
        with patch.object(diagnostic, "load_dotenv") as load_dotenv:
            with patch.object(diagnostic.Runner, "run_sync") as run_sync:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(diagnostic.main([]), 0)
        load_dotenv.assert_called_once_with()
        run_sync.assert_not_called()

    def test_environment_is_loaded_before_live_runner(self):
        calls = []
        result = SimpleNamespace(final_output="Plain-text evaluation")
        with patch.object(
            diagnostic,
            "load_dotenv",
            side_effect=lambda: calls.append("load_dotenv"),
        ):
            with patch.object(
                diagnostic.Runner,
                "run_sync",
                side_effect=lambda *_args: (
                    calls.append("run_sync"),
                    result,
                )[1],
            ):
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(diagnostic.main(["--run"]), 0)
        self.assertEqual(calls, ["load_dotenv", "run_sync"])

    def test_run_path_invokes_expected_manager_and_prompt(self):
        result = SimpleNamespace(final_output="Plain-text evaluation")
        with patch.object(diagnostic, "load_dotenv"):
            with patch.object(
                diagnostic.Runner, "run_sync", return_value=result
            ) as run_sync:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(diagnostic.main(["--run"]), 0)
        run_sync.assert_called_once_with(
            diagnostic.manager_agent,
            diagnostic.ROSS_PROMPT,
        )

    def test_specialist_input_builder_passes_one_text_string(self):
        builder = diagnostic._text_input_builder("Sourcing")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(
                builder({"params": {"input": "Research this exact shoe."}}),
                "Research this exact shoe.",
            )

    def test_specialist_output_extractor_returns_plain_text(self):
        result = SimpleNamespace(final_output="Labeled natural-language report")
        with redirect_stdout(io.StringIO()):
            output = asyncio.run(diagnostic.sourcing_text_output(result))
        self.assertEqual(output, "Labeled natural-language report")

    def test_no_retry_or_repair_configuration_is_present(self):
        for agent in diagnostic.DIAGNOSTIC_AGENTS:
            self.assertIsNone(agent.model_settings.retry)
        source = diagnostic.__file__
        with open(source, encoding="utf-8") as diagnostic_file:
            text = diagnostic_file.read().lower()
        self.assertNotIn("json repair", text)
        self.assertNotIn("fallback parsing", text)


if __name__ == "__main__":
    unittest.main()
