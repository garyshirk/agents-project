import unittest

from arbitrage.orchestration import agent, lead_qualifier


class OrchestrationPolicyTests(unittest.TestCase):
    def test_candidate_persistence_is_required_at_the_threshold(self):
        instructions = agent.instructions

        self.assertIsInstance(instructions, str)
        self.assertIn("application establishes and binds", instructions)
        self.assertIn("Candidate creation is not one of your tools", instructions)
        self.assertIn("Sourcing research is not a prerequisite", instructions)
        self.assertIn("Profitability readiness is not required", instructions)
        self.assertIn("Sourcing may precede Candidate creation", instructions)
        self.assertIn(
            "Missing UPC, unverified authenticity, uncertain official colorway naming",
            instructions,
        )

    def test_manager_tool_list_remains_v1b_scope(self):
        self.assertEqual(
            [tool.name for tool in agent.tools],
            [
                "consult_sourcing_agent",
                "consult_resale_agent",
                "calculate_profitability",
                "update_candidate_evaluation",
                "finish_candidate_evaluation",
            ],
        )

    def test_lead_qualifier_has_only_limited_sourcing(self):
        self.assertEqual(
            [tool.name for tool in lead_qualifier.tools],
            ["consult_sourcing_agent"],
        )

    def test_profitability_request_construction_semantics(self):
        instructions = agent.instructions

        self.assertIn("encode 13% as 13", instructions)
        self.assertIn("13.6% as 13.6", instructions)
        self.assertIn("never encode those as 0.13, 0.136, or 0.08", instructions)
        self.assertIn("a 12%-15% range is 12 to 15, not 0.12 to 0.15", instructions)
        self.assertIn("zero-valued cost as a placeholder", instructions)
        self.assertIn("zero is actually known to be correct", instructions)
        self.assertIn("related_cost_ids with the exact cost_id", instructions)
        self.assertIn("do not delete the UnknownInput or invent a value", instructions)
        self.assertIn("HUMAN_OBSERVED basis", instructions)
        self.assertIn("do not downgrade it to ASSUMED", instructions)
        self.assertIn("must not be promoted to independently verified condition", instructions)
        self.assertIn("If Profitability returns no scenario cases", instructions)


if __name__ == "__main__":
    unittest.main()
