"""Explicit fictional offline responses. No live or historical business evidence."""

from arbitrage.v2.contracts import OfflineFixture, WorkflowStep
from arbitrage.v2.workflow import OfflineResponse


def fixture_step(accepted, step_id, role, output, *, refs=(), retry_of=None):
    response = OfflineResponse(opportunity_id=accepted.opportunity.opportunity_id,
        evaluation_id=accepted.evaluation.evaluation_id,output=output)
    return WorkflowStep(step_id=step_id,role=role,context_references=refs,
        context_text="Explicit synthetic offline input; no actual product research.",
        fixture=OfflineFixture(fixture_id="synthetic-" + step_id,response_json=response.model_dump_json()),retry_of=retry_of)
