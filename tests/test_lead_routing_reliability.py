import asyncio
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from agents import RunConfig, RunContextWrapper, Runner, Usage
from agents.exceptions import MaxTurnsExceeded
from agents.items import ModelResponse
from agents.models.interface import Model
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText

from arbitrage import application, orchestration
from arbitrage.candidate_workflow import ApplicationContext, CandidateWorkflow
from arbitrage.lead_routing import LEAD_ROUTING_TOOLS
from arbitrage.lead_routing_completion import LeadRoutingDiagnostics, finish_lead_on_routing
from arbitrage.lead_routing_state import LeadRoutingError
from arbitrage.persistence import CandidateRepository
from arbitrage.specialists.sourcing import sourcing_agent
from tests.test_lead_decision_contract import candidate_request, intake_assessment


def text(value):
    return ResponseOutputMessage(id="offline-message", role="assistant", status="completed",
                                 type="message", content=[ResponseOutputText(
                                     text=value, annotations=[], type="output_text")])


def call(name, arguments):
    return ResponseFunctionToolCall(name=name, arguments=arguments, call_id="offline-call",
                                   type="function_call")


def route():
    return call("route_needs_more_info", json.dumps({
        "reasoning": "Missing observable identity", "clarification_question": "Which variant?",
        "unresolved_uncertainties": [],
    }))


class ScriptedModel(Model):
    def __init__(self, outputs, *, repeat=False, state=None):
        self.outputs = outputs
        self.repeat = repeat
        self.state = state
        self.calls = 0
        self.settings = []
        self.decision_counts = []

    async def get_response(self, system_instructions, input, model_settings, *args, **kwargs):
        index = self.calls
        self.calls += 1
        self.settings.append(model_settings)
        if self.state is not None:
            self.decision_counts.append(len(self.state.decisions))
        if index >= len(self.outputs) and not self.repeat:
            raise AssertionError("Unexpected additional model turn")
        output = self.outputs[min(index, len(self.outputs) - 1)]
        output = [item.model_copy(update={"call_id": f"offline-{index}-{position}"})
                  if isinstance(item, ResponseFunctionToolCall) else item
                  for position, item in enumerate(output)]
        return ModelResponse(output=output, usage=Usage(), response_id=f"offline-response-{index}")

    def stream_response(self, *args, **kwargs):
        raise AssertionError("Streaming not used")


class LeadRoutingReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = Path(self.directory.name) / "offline.db"
        self.repository = CandidateRepository(self.database)
        self.context = ApplicationContext(candidate_workflow=CandidateWorkflow(self.repository))
        self.context.lead_routing.begin()
        self.config = RunConfig(tracing_disabled=True)

    def tearDown(self):
        self.directory.cleanup()

    def lead(self, outputs, *, source=False, repeat=False):
        model = ScriptedModel(outputs, repeat=repeat, state=self.context.lead_routing)
        tools = list(LEAD_ROUTING_TOOLS)
        self.worker_model = None
        if source:
            self.worker_model = ScriptedModel([[text("Offline sourcing report")]], repeat=True)
            worker = sourcing_agent.clone(model=self.worker_model, tools=[])
            tools.insert(0, worker.as_tool(
                tool_name="consult_sourcing_agent", tool_description="Offline only",
                input_builder=orchestration.sourcing_tool_input,
                custom_output_extractor=orchestration.sourcing_tool_output,
            ))
        return orchestration.lead_qualifier.clone(model=model, tools=tools), model

    def run_lead(self, lead, hooks=None):
        with patch("socket.create_connection", side_effect=AssertionError("network")), \
             patch("socket.getaddrinfo", side_effect=AssertionError("DNS")):
            return Runner.run_sync(lead, "Offline test", context=self.context,
                                   run_config=self.config, hooks=hooks)

    def business_counts(self):
        with sqlite3.connect(self.database) as connection:
            return tuple(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                         for table in ("candidates", "candidate_evaluations", "evaluation_artifacts"))

    def test_sourcing_then_routing_completes_once_with_required_choice_preserved(self):
        lead, model = self.lead([[call("consult_sourcing_agent", '{"input":"Research identity"}')],
                                 [route()]], source=True)
        result = self.run_lead(lead)
        self.assertEqual(model.calls, 2)
        self.assertEqual(self.worker_model.calls, 1)
        self.assertEqual(model.decision_counts, [0, 0])
        self.assertEqual(len(self.context.lead_routing.decisions), 1)
        self.assertEqual(self.context.lead_routing.complete().disposition.value, "NEEDS_MORE_INFO")
        self.assertEqual([setting.tool_choice for setting in model.settings], ["required", "required"])
        self.assertEqual([setting.parallel_tool_calls for setting in model.settings], [False, False])
        self.assertIsInstance(result.final_output, str)

    def test_successful_route_stops_before_another_model_turn(self):
        lead, model = self.lead([[route()]])
        self.run_lead(lead)
        self.assertEqual(model.calls, 1)
        self.assertEqual(len(self.context.lead_routing.decisions), 1)

    def test_sourcing_result_does_not_count_as_terminal(self):
        result = finish_lead_on_routing(RunContextWrapper(self.context), [SimpleNamespace(
            tool=SimpleNamespace(name="consult_sourcing_agent"), output="report")])
        self.assertFalse(result.is_final_output)
        self.assertEqual(self.context.lead_routing.decisions, [])

    def test_invalid_arguments_can_be_corrected_within_same_run(self):
        for invalid in ("{}", "{bad"):
            with self.subTest(invalid=invalid):
                self.context.lead_routing.begin()
                hooks = LeadRoutingDiagnostics()
                lead, model = self.lead([[call("route_candidate_ready", invalid)], [route()]])
                self.run_lead(lead, hooks)
                self.assertEqual(model.calls, 2)
                self.assertEqual(model.decision_counts, [0, 0])
                self.assertEqual(len(self.context.lead_routing.decisions), 1)
                self.assertEqual(hooks.events[0]["outcome"], "argument_error_reported")
                self.assertEqual(hooks.events[1]["outcome"], "routing_recorded")

    def test_valid_candidate_ready_records_without_persisting_until_coordinator(self):
        self.context.lead_routing.begin("Offline test")
        arguments = {"candidate_request": candidate_request().model_dump(mode="json"),
                     "intake_assessment": intake_assessment().model_dump(mode="json"),
                     "reasoning": "Sufficient identity", "unresolved_uncertainties": []}
        lead, model = self.lead([[call("route_candidate_ready", json.dumps(arguments))]])
        self.run_lead(lead)
        self.assertEqual(self.context.lead_routing.complete().disposition.value, "CANDIDATE_READY")
        self.assertEqual(model.calls, 1)
        self.assertEqual(self.business_counts(), (0, 0, 0))

    def test_multiple_terminal_decisions_rejected_even_if_provider_ignores_parallel_setting(self):
        lead, model = self.lead([[route(), route()]])
        with self.assertRaisesRegex(LeadRoutingError, "multiple terminal"):
            self.run_lead(lead)
        self.assertEqual(len(self.context.lead_routing.decisions), 2)
        self.assertEqual(self.business_counts(), (0, 0, 0))

    def test_repeated_sourcing_is_bounded_by_existing_ten_turn_limit(self):
        lead, model = self.lead([[call("consult_sourcing_agent", '{"input":"Offline identity"}')]],
                                source=True, repeat=True)
        with self.assertRaises(MaxTurnsExceeded):
            self.run_lead(lead)
        self.assertEqual(model.calls, 10)
        self.assertEqual(self.context.lead_routing.decisions, [])
        self.assertEqual(self.business_counts(), (0, 0, 0))

    def run_application_failure(self, lead, expected_error):
        session = SimpleNamespace(get_items=AsyncMock(return_value=[]), close=Mock())
        coordinator = Mock()
        coordinator.qualification_context.return_value = "No active Candidate"
        with patch.object(orchestration, "lead_qualifier", lead), \
             patch.object(application, "load_dotenv"), \
             patch.object(application, "SQLiteSession", return_value=session), \
             patch.object(application, "CandidateRepository", return_value=self.repository), \
             patch.object(application, "ArbitrageCoordinator", return_value=coordinator), \
             patch.object(application, "RunConfig", return_value=self.config), \
             patch("builtins.input", return_value="Offline test"), \
             patch("socket.create_connection", side_effect=AssertionError("network")), \
             patch("socket.getaddrinfo", side_effect=AssertionError("DNS")), \
             redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(expected_error):
                application.main()
        coordinator.coordinate.assert_not_called()
        session.close.assert_called_once()
        self.assertEqual(self.business_counts(), (0, 0, 0))
        return output.getvalue()

    def test_plain_prose_cannot_successfully_route_application_and_is_redacted(self):
        secret = "sk-secret-credential private user and research details"
        lead, model = self.lead([[text(secret)]])
        output = self.run_application_failure(lead, LeadRoutingError)
        self.assertNotIn(secret, output)
        self.assertNotIn("sk-secret", output)
        self.assertIn('"runner_status": "returned"', output)
        self.assertIn('"terminal_decision_count": 0', output)
        self.assertIn('"final_output_type": "str"', output)
        self.assertIn("text omitted", output)

    def test_turn_limit_is_explicit_application_failure_without_retry_or_records(self):
        lead, model = self.lead([[call("consult_sourcing_agent", '{"input":"Offline"}')]],
                                source=True, repeat=True)
        output = self.run_application_failure(lead, MaxTurnsExceeded)
        self.assertEqual(model.calls, 10)
        self.assertIn('"runner_status": "raised"', output)
        self.assertIn('"sdk_error_class": "MaxTurnsExceeded"', output)

    def test_failure_diagnostics_are_bounded_and_never_log_payloads(self):
        hooks = LeadRoutingDiagnostics()
        secret = "sk-secret-password"
        context = RunContextWrapper(self.context)
        tool = SimpleNamespace(name=secret)
        for _ in range(25):
            asyncio.run(hooks.on_tool_start(context, None, tool))
            asyncio.run(hooks.on_tool_end(context, None, tool, secret))
        with redirect_stdout(io.StringIO()) as output:
            hooks.report_failure(self.context, runner_returned=True,
                                 result=SimpleNamespace(final_output=secret * 10000),
                                 error=LeadRoutingError(secret))
        self.assertLess(len(output.getvalue()), 3000)
        self.assertNotIn(secret, output.getvalue())
        self.assertTrue(hooks.events_truncated)
        self.assertEqual(len(hooks.events), 20)
        self.assertEqual(self.business_counts(), (0, 0, 0))

    def test_production_configuration_uses_state_callback(self):
        lead = orchestration.lead_qualifier
        self.assertEqual(lead.model_settings.tool_choice, "required")
        self.assertFalse(lead.model_settings.parallel_tool_calls)
        self.assertFalse(lead.reset_tool_choice)
        self.assertIs(lead.tool_use_behavior, finish_lead_on_routing)


if __name__ == "__main__":
    unittest.main()
