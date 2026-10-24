"""Selection commands from direct terminal input only; never an agent tool."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from arbitrage.contracts import EvaluationSelectionResult
from arbitrage.persistence import _EVALUATION_SELECTION_AUTHORITY

if TYPE_CHECKING:
    from arbitrage.candidate_workflow import CandidateWorkflow


def handle_evaluation_command(
    workflow: CandidateWorkflow, text: str, *, session_id: str | None = None,
) -> EvaluationSelectionResult | None:
    # Reserve malformed command attempts too, but never strip them into validity.
    if not text.lstrip().startswith("/evaluation"):
        return None
    command = re.fullmatch(r"/evaluation select ([^\s]+)", text)
    if command is None:
        return EvaluationSelectionResult(
            success=False, error_code="INVALID_COMMAND",
            message="Use exactly /evaluation select <evaluation-id>, with one canonical UUID and no extra arguments.",
        )
    if session_id is not None and (not isinstance(session_id, str) or not session_id.strip()):
        return EvaluationSelectionResult(
            success=False, error_code="INVALID_SESSION_ID",
            message="Session provenance must be an actual nonblank identifier or absent.",
        )
    return workflow.select_existing_evaluation(
        command[1], human_command=text, session_id=session_id,
        _authority=_EVALUATION_SELECTION_AUTHORITY,
    )
