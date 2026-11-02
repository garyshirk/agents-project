"""Trusted human-facing service foundation; deliberately not an agent tool or CLI."""

from arbitrage.v2.contracts import ArtifactRef, HumanActor, ReviewDecision
from arbitrage.v2.persistence import V2Repository


class HumanReviewService:
    """Caller supplies actual human provenance; this is not authentication.

    Review is assessment only. Follow-up remains recorded text, never execution.
    No generic artifact-writing operation is exposed by this service.
    """

    def __init__(self, repository: V2Repository):
        self.repository = repository

    def list_queue(self, *, limit=25, offset=0):
        return self.repository.list_review_queue(limit=limit, offset=offset)

    def get_evaluation(self, evaluation_id):
        return self.repository.get_evaluation(evaluation_id)

    def get_artifact_history(self, evaluation_id):
        return self.repository.list_artifacts(evaluation_id)

    def get_current_summary(self, evaluation_id):
        """Latest published summary; consult review_status for new-information gaps."""
        return self.repository.get_current_summary(evaluation_id)

    def get_review_history(self, evaluation_id):
        return self.repository.list_reviews(evaluation_id)

    def get_evidence(self, evaluation_id):
        """All evidence, failures, and historical judgments; no category filtering."""
        return self.repository.get_review_evidence(evaluation_id)

    def review_status(self, evaluation_id):
        return self.repository.review_status(evaluation_id)

    def record_decision(self, evaluation_id: str, subject: ArtifactRef,
                        decision: ReviewDecision, rationale: str, *, human: HumanActor,
                        operation_key: str, concerns=(), requested_follow_up=(), supersedes_review_id=None):
        return self.repository.record_review(
            evaluation_id, subject, decision, rationale, human=human, operation_key=operation_key,
            concerns=concerns, requested_follow_up=requested_follow_up, supersedes_review_id=supersedes_review_id,
        )
