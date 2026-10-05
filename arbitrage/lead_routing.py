from agents import RunContextWrapper, function_tool

from arbitrage.candidate_workflow import ApplicationContext
from arbitrage.contracts import (
    CandidateRelation,
    LeadDecision,
    LeadDisposition,
    StartCandidateEvaluationRequest,
)


ROUTING_RECORDED = (
    "Terminal Lead routing action recorded. Do not call another routing action."
)


@function_tool
def route_candidate_ready(
    context: RunContextWrapper[ApplicationContext],
    candidate_request: StartCandidateEvaluationRequest,
    reasoning: str,
    unresolved_uncertainties: list[str],
) -> str:
    """Route a sufficiently specific new Lead into substantive Candidate evaluation."""
    relation = (
        CandidateRelation.CLEARLY_NEW
        if context.context.candidate_workflow.active.is_active
        else CandidateRelation.NO_ACTIVE_CANDIDATE
    )
    context.context.lead_routing.select(
        LeadDecision(
            disposition=LeadDisposition.CANDIDATE_READY,
            relation_to_active=relation,
            reasoning=reasoning,
            continue_substantive_evaluation=True,
            candidate_request=candidate_request,
            clarification_question=None,
            user_message=None,
            unresolved_uncertainties=unresolved_uncertainties,
        )
    )
    return ROUTING_RECORDED


@function_tool
def route_existing_candidate(
    context: RunContextWrapper[ApplicationContext],
    reasoning: str,
    unresolved_uncertainties: list[str],
) -> str:
    """Route a clear follow-up into the currently active Candidate evaluation."""
    context.context.lead_routing.select(
        LeadDecision(
            disposition=LeadDisposition.EXISTING_CANDIDATE,
            relation_to_active=CandidateRelation.SAME_AS_ACTIVE,
            reasoning=reasoning,
            continue_substantive_evaluation=True,
            candidate_request=None,
            clarification_question=None,
            user_message=None,
            unresolved_uncertainties=unresolved_uncertainties,
        )
    )
    return ROUTING_RECORDED


@function_tool
def route_needs_more_info(
    context: RunContextWrapper[ApplicationContext],
    clarification_question: str,
    reasoning: str,
    unresolved_uncertainties: list[str],
) -> str:
    """Ask one targeted question when useful human information could qualify the Lead."""
    relation = (
        CandidateRelation.SAME_AS_ACTIVE
        if context.context.candidate_workflow.active.is_active
        else CandidateRelation.NO_ACTIVE_CANDIDATE
    )
    context.context.lead_routing.select(
        LeadDecision(
            disposition=LeadDisposition.NEEDS_MORE_INFO,
            relation_to_active=relation,
            reasoning=reasoning,
            continue_substantive_evaluation=False,
            candidate_request=None,
            clarification_question=clarification_question,
            user_message=None,
            unresolved_uncertainties=unresolved_uncertainties,
        )
    )
    return ROUTING_RECORDED


@function_tool
def route_ambiguous_boundary(
    context: RunContextWrapper[ApplicationContext],
    clarification_question: str,
    reasoning: str,
    unresolved_uncertainties: list[str],
) -> str:
    """Ask whether an ambiguous request concerns the active Candidate or a new Lead."""
    context.context.lead_routing.select(
        LeadDecision(
            disposition=LeadDisposition.AMBIGUOUS_BOUNDARY,
            relation_to_active=CandidateRelation.AMBIGUOUS,
            reasoning=reasoning,
            continue_substantive_evaluation=False,
            candidate_request=None,
            clarification_question=clarification_question,
            user_message=None,
            unresolved_uncertainties=unresolved_uncertainties,
        )
    )
    return ROUTING_RECORDED


@function_tool
def route_stop(
    context: RunContextWrapper[ApplicationContext],
    user_message: str,
    reasoning: str,
    unresolved_uncertainties: list[str],
) -> str:
    """Stop qualification when no defensible path to Candidate evaluation remains."""
    relation = (
        CandidateRelation.SAME_AS_ACTIVE
        if context.context.candidate_workflow.active.is_active
        else CandidateRelation.NO_ACTIVE_CANDIDATE
    )
    context.context.lead_routing.select(
        LeadDecision(
            disposition=LeadDisposition.STOP,
            relation_to_active=relation,
            reasoning=reasoning,
            continue_substantive_evaluation=False,
            candidate_request=None,
            clarification_question=None,
            user_message=user_message,
            unresolved_uncertainties=unresolved_uncertainties,
        )
    )
    return ROUTING_RECORDED


LEAD_ROUTING_TOOLS = [
    route_candidate_ready,
    route_existing_candidate,
    route_needs_more_info,
    route_ambiguous_boundary,
    route_stop,
]
