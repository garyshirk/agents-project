"""Bind specialist research to an application-owned, persisted target."""

from dataclasses import replace
from pydantic import ConfigDict, create_model

from arbitrage.contracts import EvaluationStatus, ResaleRequest


# Reuse the existing research-intent fields, without model control of identity.
ResaleResearchIntent = create_model(
    "ResaleResearchIntent",
    __config__=ConfigDict(extra="forbid"),
    **{name: (field.annotation, field) for name, field in ResaleRequest.model_fields.items()
       if name != "product_identity"},
)


def bind_resale_target(inner_tool):
    from agents.agent_output import AgentOutputSchema

    async def invoke(context, arguments):
        try:
            intent = ResaleResearchIntent.model_validate_json(arguments)
            workflow = context.context.candidate_workflow
            candidate, evaluation = workflow._active_records()
            if evaluation.status != EvaluationStatus.IN_PROGRESS:
                raise ValueError("Resale requires an IN_PROGRESS Evaluation")
        except Exception:
            # Do not return repository contents, payloads, or configuration in errors.
            return "Resale delegation rejected: invalid research intent or active Candidate/Evaluation."
        target = candidate.product_identity.model_copy(deep=True)
        request = ResaleRequest(product_identity=target, **intent.model_dump())
        provenance = {
            "origin": "active_persisted_candidate",
            "candidate_id": candidate.candidate_id,
            "evaluation_id": evaluation.evaluation_id,
            "product_identity": target.model_dump(mode="json"),
        }
        context.resale_target_provenance = provenance
        return await inner_tool.on_invoke_tool(context, request.model_dump_json())

    return replace(
        inner_tool,
        params_json_schema=AgentOutputSchema(ResaleResearchIntent).json_schema(),
        on_invoke_tool=invoke,
        description="Research resale evidence for the active Candidate; specify research intent only. "
                    "The application supplies the authoritative target identity.",
    )
