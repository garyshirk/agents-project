import asyncio
import inspect
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from agents.tool_context import ToolContext
from openai.types.responses import ResponseFunctionToolCall

from arbitrage import application
from arbitrage.candidate_workflow import ApplicationContext, CandidateWorkflow
from arbitrage.contracts import (
    CandidateRelation,
    LeadDisposition,
    ManagerEvaluationJudgment,
    ResaleRequest,
    ResaleResult,
)
from arbitrage.lead_routing import (
    LEAD_ROUTING_TOOLS,
    route_ambiguous_boundary,
    route_candidate_ready,
    route_existing_candidate,
    route_needs_more_info,
    route_stop,
)
from arbitrage.lead_routing_state import LeadRoutingError
from arbitrage.orchestration import (
    agent,
    lead_qualifier,
    resale_agent_tool,
    sourcing_agent_tool,
)
from arbitrage.persistence import CandidateRepository
from arbitrage.specialists.resale import resale_agent
from arbitrage.specialists.sourcing import sourcing_agent
from tests.test_lead_decision_contract import candidate_request


class LeadRoutingActionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        repository = CandidateRepository(
            Path(self.temporary_directory.name) / "lead-routing.db"
        )
        self.context = ApplicationContext(
            candidate_workflow=CandidateWorkflow(repository)
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def invoke(self, tool, arguments: dict) -> str:
        encoded = json.dumps(arguments)
        call = ResponseFunctionToolCall(
            arguments=encoded,
            call_id="offline-lead-call",
            name=tool.name,
            type="function_call",
        )
        context = ToolContext(
            self.context,
            tool_name=tool.name,
            tool_call_id=call.call_id,
            tool_arguments=encoded,
            tool_call=call,
        )
        with redirect_stdout(StringIO()):
            return asyncio.run(tool.on_invoke_tool(context, encoded))

    @staticmethod
    def common_arguments() -> dict:
        return {
            "reasoning": "Deterministic routing test.",
            "unresolved_uncertainties": ["Test uncertainty"],
        }

    def test_lead_agent_uses_terminal_tools_and_plain_text_output(self):
        self.assertIsNone(lead_qualifier.output_type)
        self.assertEqual(
            [tool.name for tool in LEAD_ROUTING_TOOLS],
            [
                "route_candidate_ready",
                "route_existing_candidate",
                "route_needs_more_info",
                "route_ambiguous_boundary",
                "route_stop",
            ],
        )
        self.assertEqual(
            [tool.name for tool in lead_qualifier.tools],
            ["consult_sourcing_agent", *[tool.name for tool in LEAD_ROUTING_TOOLS]],
        )

    def test_candidate_ready_action_constructs_internal_decision(self):
        arguments = self.common_arguments()
        arguments["candidate_request"] = candidate_request().model_dump(mode="json")
        self.context.lead_routing.begin()
        self.invoke(route_candidate_ready, arguments)
        decision = self.context.lead_routing.complete()
        self.assertEqual(decision.disposition, LeadDisposition.CANDIDATE_READY)
        self.assertEqual(
            decision.relation_to_active, CandidateRelation.NO_ACTIVE_CANDIDATE
        )
        self.assertTrue(decision.continue_substantive_evaluation)
        self.assertEqual(decision.candidate_request, candidate_request())

    def test_every_non_candidate_terminal_action_constructs_expected_decision(self):
        cases = (
            (
                route_existing_candidate,
                {},
                LeadDisposition.EXISTING_CANDIDATE,
                CandidateRelation.SAME_AS_ACTIVE,
                True,
            ),
            (
                route_needs_more_info,
                {"clarification_question": "What is the style number?"},
                LeadDisposition.NEEDS_MORE_INFO,
                CandidateRelation.NO_ACTIVE_CANDIDATE,
                False,
            ),
            (
                route_ambiguous_boundary,
                {"clarification_question": "Is this the current item or a new one?"},
                LeadDisposition.AMBIGUOUS_BOUNDARY,
                CandidateRelation.AMBIGUOUS,
                False,
            ),
            (
                route_stop,
                {"user_message": "There is no defensible evaluation path."},
                LeadDisposition.STOP,
                CandidateRelation.NO_ACTIVE_CANDIDATE,
                False,
            ),
        )
        for tool, additions, disposition, relation, continues in cases:
            with self.subTest(tool=tool.name):
                self.context.lead_routing.begin()
                self.invoke(tool, {**self.common_arguments(), **additions})
                decision = self.context.lead_routing.complete()
                self.assertEqual(decision.disposition, disposition)
                self.assertEqual(decision.relation_to_active, relation)
                self.assertEqual(decision.continue_substantive_evaluation, continues)

    def test_missing_terminal_action_fails_clearly(self):
        self.context.lead_routing.begin()
        with self.assertRaisesRegex(LeadRoutingError, "without selecting"):
            self.context.lead_routing.complete()

    def test_multiple_terminal_actions_fail_clearly(self):
        self.context.lead_routing.begin()
        self.invoke(
            route_needs_more_info,
            {
                **self.common_arguments(),
                "clarification_question": "What is the model?",
            },
        )
        second_output = self.invoke(
            route_stop,
            {
                **self.common_arguments(),
                "user_message": "Stop.",
            },
        )
        self.assertIn("multiple terminal routing actions", second_output)
        with self.assertRaisesRegex(LeadRoutingError, "multiple terminal"):
            self.context.lead_routing.complete()

    def test_action_schemas_are_semantic_and_smaller_at_the_top_level(self):
        self.assertEqual(
            set(route_candidate_ready.params_json_schema["properties"]),
            {"candidate_request", "reasoning", "unresolved_uncertainties"},
        )
        self.assertEqual(
            set(route_existing_candidate.params_json_schema["properties"]),
            {"reasoning", "unresolved_uncertainties"},
        )
        self.assertEqual(
            set(route_needs_more_info.params_json_schema["properties"]),
            {"clarification_question", "reasoning", "unresolved_uncertainties"},
        )
        self.assertEqual(
            set(route_ambiguous_boundary.params_json_schema["properties"]),
            {"clarification_question", "reasoning", "unresolved_uncertainties"},
        )
        self.assertEqual(
            set(route_stop.params_json_schema["properties"]),
            {"user_message", "reasoning", "unresolved_uncertainties"},
        )

    def test_final_prose_is_not_parsed_and_no_repair_or_retry_was_added(self):
        source = "\n".join(
            (
                inspect.getsource(application),
                inspect.getsource(__import__("arbitrage.lead_routing", fromlist=["*"])),
                inspect.getsource(
                    __import__("arbitrage.lead_routing_state", fromlist=["*"])
                ),
            )
        ).lower()
        application_source = inspect.getsource(application)
        self.assertNotIn("qualification.final_output", application_source)
        self.assertIn("result.final_output_as", application_source)
        for forbidden in ("regex", "json repair", "fallback parser", "retry loop"):
            self.assertNotIn(forbidden, source)

    def test_other_architectural_boundaries_remain_unchanged(self):
        self.assertIsNone(sourcing_agent.output_type)
        self.assertEqual(sourcing_agent_tool.params_json_schema["title"], "AgentAsToolInput")
        self.assertEqual([tool.name for tool in sourcing_agent.tools], ["web_search"])
        self.assertIs(resale_agent.output_type, ResaleResult)
        self.assertEqual(resale_agent_tool.params_json_schema["title"], "ResaleRequest")
        self.assertEqual(
            set(resale_agent_tool.params_json_schema["properties"]),
            set(ResaleRequest.model_fields),
        )
        self.assertIs(agent.output_type, ManagerEvaluationJudgment)
        self.assertIn("calculate_profitability", [tool.name for tool in agent.tools])


if __name__ == "__main__":
    unittest.main()
