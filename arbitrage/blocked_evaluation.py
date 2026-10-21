"""Read-only diagnostics for blocked finalization; no speculative recovery."""

import json

from arbitrage.contracts import EvaluationArtifactType, ProfitabilityToolResult
from arbitrage.persistence import (
    AcquisitionCapturePrerequisiteError, SellingCapturePrerequisiteError,
    ViableFinalizationPrerequisiteError,
)
from arbitrage.profitability_request_assembly import AssemblyFailureCode


def blocked_evaluation_details(workflow, error):
    state = workflow.active_result()
    evidence = workflow.repository.economic_evidence_snapshot(state.evaluation_id)
    sources = [item for item in (evidence.sourcing_report, evidence.acquisition_cost_findings,
                               evidence.resale, evidence.selling_cost_findings) if item is not None]
    attempts = workflow.repository.list_evaluation_artifacts(state.evaluation_id)
    failed = None
    for artifact in reversed(attempts):
        if artifact.artifact_type != EvaluationArtifactType.PROFITABILITY:
            continue
        try:
            result = ProfitabilityToolResult.model_validate_json(artifact.payload_json)
        except ValueError:
            continue
        if not result.success:
            failed = artifact
            break
    categories = []
    if isinstance(error, AcquisitionCapturePrerequisiteError):
        categories.append("MISSING_RESEARCH" if evidence.sourcing_report is None else "MISSING_OR_STALE_ACQUISITION_CAPTURE")
    elif isinstance(error, SellingCapturePrerequisiteError):
        categories.append("MISSING_RESEARCH" if evidence.resale is None else "MISSING_OR_STALE_SELLING_CAPTURE")
    elif isinstance(error, ViableFinalizationPrerequisiteError):
        categories.append("NO_SUCCESSFUL_PROFITABILITY")
    if failed is not None:
        try:
            metadata = json.loads(failed.context_json or "{}")
            if not isinstance(metadata, dict):
                metadata = {}
            known_codes = {code.value for code in AssemblyFailureCode}
            failures = metadata.get("assembly_failures", [])
            if isinstance(failures, list):
                for failure in failures:
                    code = failure.get("code") if isinstance(failure, dict) else None
                    categories.append(code if isinstance(code, str) and code in known_codes else "UNCLASSIFIED_FAILURE")
            execution = metadata.get("error_type")
            mapped = {"AcquisitionCapturePrerequisiteError": "MISSING_OR_STALE_ACQUISITION_CAPTURE",
                      "SellingCapturePrerequisiteError": "MISSING_OR_STALE_SELLING_CAPTURE"}
            if execution is not None:
                categories.append(mapped.get(execution, "UNCLASSIFIED_FAILURE") if isinstance(execution, str)
                                  else "UNCLASSIFIED_FAILURE")
        except (ValueError, TypeError):
            categories.append("UNCLASSIFIED_FAILURE")
    categories = list(dict.fromkeys(categories)) or ["UNCLASSIFIED_FAILURE"]
    correction = "Provide explicit corrected evidence or clarification for review; no automatic research retry is authorized."
    if any(code in categories for code in ("IDENTITY_CONFLICT", "CONDITION_CONFLICT", "INSUFFICIENT_IDENTITY_EVIDENCE", "RESALE_IDENTITY_IMPERFECT")):
        correction = "Resolve or document the target/researched identity discrepancy with supported evidence; do not force agreement."
    elif "SHIPPING_COVERAGE" in categories:
        correction = "Provide revised selling capture with explicit supported or MATERIAL-unresolved outbound shipping, or supported nonapplicability."
    elif any(code in categories for code in ("MISSING_RESEARCH", "MISSING_EVIDENCE", "STALE_EVIDENCE", "MISSING_OR_STALE_ACQUISITION_CAPTURE", "MISSING_OR_STALE_SELLING_CAPTURE")):
        correction = "Provide the missing research/capture pair or a corrected capture following its research; existing freshness checks still apply."
    return {
        "candidate_id": state.candidate_id, "evaluation_id": state.evaluation_id,
        "categories": categories,
        "failed_profitability_artifact_id": failed.artifact_id if failed else None,
        "source_artifacts": [{"artifact_id": item.artifact_id, "sequence": item.sequence_number,
                              "type": item.artifact_type.value} for item in sources],
        "source_reference_scope": "Current same-Evaluation evidence snapshot; not proof of changed evidence or the historical failed attempt's exact inputs.",
        "required_correction": correction,
        "automatic_continuations": 0,
        "resumption": "Later explicit follow-up in the same active process is required. Active state is not automatically restored after restart; no historical evaluation is resumed automatically.",
    }
