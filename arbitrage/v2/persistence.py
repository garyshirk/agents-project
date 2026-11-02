"""V2-only SQLite records. Explicit initialization; no V1C workflow calls."""

import json
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from arbitrage.database_config import isolated_database_paths
from arbitrage.v2.contracts import (
    AcceptedEvaluation, Artifact, ArtifactRef, ArtifactType, AssumptionRecord,
    EconomicAssessmentSummary, Evaluation, ExecutionState, FailureRecord,
    HumanActor, HumanReviewDecision, KnowledgeSource, Opportunity,
    OpportunitySubmission, PartialReviewSubject, Producer, ReviewDecision,
    ReviewQueueEntry, ReviewQueuePage, SummaryDraft,
    AssessmentDraft, AssessmentRevisionStatus, ComparableAssessment, ComparableDraft,
    RawResearchReport, ResaleComparable, ReviewEvidenceSnapshot,
    AssessmentState, CalculationRecord, ComputationStatus, EconomicScenario, ScenarioDraft,
)
from arbitrage.v2.economics import ADAPTER_VERSION, ENGINE_VERSION, calculate_scenario


class V2PersistenceError(ValueError):
    pass


class IdempotencyConflict(V2PersistenceError):
    pass


APPLICATION = Producer(kind="APPLICATION", name="v2-foundation")


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _now():
    return datetime.now(timezone.utc)


class V2Repository:
    def __init__(self, database_path: str | Path, *, session_database_path: str | Path):
        self.paths = isolated_database_paths(database_path, session_database_path)
        self.database_path = self.paths.business

    @contextmanager
    def _connection(self, *, write=False, create=False):
        # Recheck existing aliases before every open. Never initialize sessions.
        isolated_database_paths(self.database_path, self.paths.sessions)
        mode = "rwc" if create else "rw" if write else "ro"
        with closing(sqlite3.connect(self.database_path.as_uri() + f"?mode={mode}", uri=True, timeout=2)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            if not write:
                connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                if not create:
                    version = connection.execute("SELECT value FROM v2_schema_metadata WHERE key='schema_version'").fetchone()
                    if version is None or version[0] != "1":
                        raise V2PersistenceError("Unsupported or missing V2 schema version")
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def initialize_schema(self):
        """Create only V2 tables; no automatic schema migration or history import."""
        with self._connection(write=True, create=True) as c:
            existing = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name GLOB 'v2_*'")}
            c.execute("CREATE TABLE IF NOT EXISTS v2_schema_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            row = c.execute("SELECT value FROM v2_schema_metadata WHERE key='schema_version'").fetchone()
            if row is None and existing - {"v2_schema_metadata"}:
                raise V2PersistenceError("Unversioned V2 tables require explicit investigation")
            if row is not None and row[0] != "1":
                raise V2PersistenceError("Unsupported V2 schema version; no migration performed")
            statements = (
                """CREATE TABLE IF NOT EXISTS v2_opportunities (
                    opportunity_id TEXT PRIMARY KEY, submission_json TEXT NOT NULL, created_at TEXT NOT NULL)""",
                """CREATE TABLE IF NOT EXISTS v2_evaluations (
                    evaluation_id TEXT PRIMARY KEY,
                    opportunity_id TEXT NOT NULL REFERENCES v2_opportunities(opportunity_id),
                    execution_state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    predecessor_id TEXT REFERENCES v2_evaluations(evaluation_id),
                    current_summary_id TEXT, current_subject_id TEXT NOT NULL,
                    FOREIGN KEY(evaluation_id,current_summary_id) REFERENCES v2_artifacts(evaluation_id,artifact_id) DEFERRABLE INITIALLY DEFERRED,
                    FOREIGN KEY(evaluation_id,current_subject_id) REFERENCES v2_artifacts(evaluation_id,artifact_id) DEFERRABLE INITIALLY DEFERRED)""",
                """CREATE TABLE IF NOT EXISTS v2_artifacts (
                    artifact_id TEXT PRIMARY KEY, evaluation_id TEXT NOT NULL REFERENCES v2_evaluations(evaluation_id),
                    sequence INTEGER NOT NULL CHECK(sequence>0), artifact_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL, producer_json TEXT NOT NULL, parents_json TEXT NOT NULL,
                    created_at TEXT NOT NULL, UNIQUE(evaluation_id,sequence), UNIQUE(evaluation_id,artifact_id))""",
                """CREATE TABLE IF NOT EXISTS v2_reviews (
                    review_id TEXT PRIMARY KEY, evaluation_id TEXT NOT NULL REFERENCES v2_evaluations(evaluation_id),
                    sequence INTEGER NOT NULL CHECK(sequence>0), subject_id TEXT NOT NULL,
                    supersedes_id TEXT, record_json TEXT NOT NULL,
                    UNIQUE(evaluation_id,sequence), UNIQUE(evaluation_id,review_id),
                    FOREIGN KEY(evaluation_id,subject_id) REFERENCES v2_artifacts(evaluation_id,artifact_id),
                    FOREIGN KEY(evaluation_id,supersedes_id) REFERENCES v2_reviews(evaluation_id,review_id))""",
                """CREATE TABLE IF NOT EXISTS v2_operations (
                    operation_key TEXT PRIMARY KEY, operation TEXT NOT NULL,
                    request_json TEXT NOT NULL, result_json TEXT NOT NULL)""",
                "CREATE INDEX IF NOT EXISTS v2_evaluation_order ON v2_evaluations(created_at,evaluation_id)",
            )
            for sql in statements:
                c.execute(sql)
            for table in ("v2_artifacts", "v2_reviews", "v2_operations"):
                for action in ("UPDATE", "DELETE"):
                    c.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'V2 history is append-only'); END")
            c.execute("INSERT OR IGNORE INTO v2_schema_metadata VALUES ('schema_version','1')")

    def _once(self, key, operation, request, result_type, perform):
        if not isinstance(key, str) or not key.strip() or len(key) > 200:
            raise V2PersistenceError("A nonblank operation key of at most 200 characters is required")
        encoded = _json(request)
        with self._connection(write=True) as c:
            old = c.execute("SELECT * FROM v2_operations WHERE operation_key=?", (key,)).fetchone()
            if old:
                if old["operation"] != operation or old["request_json"] != encoded:
                    raise IdempotencyConflict("Operation key already records different content or operation")
                return result_type.model_validate_json(old["result_json"])
            result = perform(c)
            c.execute("INSERT INTO v2_operations VALUES (?,?,?,?)", (key, operation, encoded, result.model_dump_json()))
            return result

    @staticmethod
    def _row(c, table, key, value):
        row = c.execute(f"SELECT * FROM {table} WHERE {key}=?", (value,)).fetchone()
        if row is None:
            raise V2PersistenceError(f"Required {table} record does not exist")
        return row

    def _opportunity(self, c, identifier):
        row = self._row(c, "v2_opportunities", "opportunity_id", identifier)
        return Opportunity(opportunity_id=identifier,
            submission=OpportunitySubmission.model_validate_json(row["submission_json"]), created_at=row["created_at"])

    def _artifact(self, c, identifier):
        row = self._row(c, "v2_artifacts", "artifact_id", identifier)
        evaluation = self._row(c, "v2_evaluations", "evaluation_id", row["evaluation_id"])
        return Artifact(reference=ArtifactRef(opportunity_id=evaluation["opportunity_id"],
            evaluation_id=row["evaluation_id"], artifact_id=identifier, sequence=row["sequence"]),
            artifact_type=row["artifact_type"], payload=json.loads(row["payload_json"]),
            producer=Producer.model_validate_json(row["producer_json"]),
            parent_references=json.loads(row["parents_json"]), created_at=row["created_at"])

    def _reference(self, c, reference, evaluation_id=None):
        reference = ArtifactRef.model_validate(reference)
        actual = self._artifact(c, reference.artifact_id)
        if actual.reference != reference or (evaluation_id and reference.evaluation_id != evaluation_id):
            raise V2PersistenceError("Artifact reference ownership or sequence mismatch")
        return actual

    def _evaluation(self, c, identifier):
        row = self._row(c, "v2_evaluations", "evaluation_id", identifier)
        subject = self._artifact(c, row["current_subject_id"])
        summary = self._artifact(c, row["current_summary_id"]) if row["current_summary_id"] else None
        if subject.artifact_type not in {ArtifactType.REVIEW_SUBJECT, ArtifactType.EVALUATION_SUMMARY}:
            raise V2PersistenceError("Invalid review subject type")
        if summary and summary.artifact_type != ArtifactType.EVALUATION_SUMMARY:
            raise V2PersistenceError("Invalid current summary type")
        return Evaluation(evaluation_id=identifier, opportunity_id=row["opportunity_id"],
            execution_state=row["execution_state"], created_at=row["created_at"], updated_at=row["updated_at"],
            predecessor_evaluation_id=row["predecessor_id"], current_summary=summary.reference if summary else None,
            current_review_subject=subject.reference)

    def _validate_provenance(self, c, provenance, evaluation_id):
        for support in provenance.supporting_records:
            artifact = self._reference(c, support.artifact)
            if (artifact.artifact_type == ArtifactType.SCENARIO_ASSUMPTION
                    and provenance.information_kind.value in {"OBSERVED_COST", "ACTUAL_TRANSACTION_OUTCOME"}):
                raise V2PersistenceError("A persisted assumption cannot establish an observed cost or transaction outcome")
            historical = support.source == KnowledgeSource.HISTORICAL_EVALUATIONS
            if historical == (support.artifact.evaluation_id == evaluation_id):
                raise V2PersistenceError("Historical references require another Evaluation; current/human evidence requires this Evaluation")

    def _insert_artifact(self, c, evaluation_id, kind, payload, producer, parents=(), identifier=None, created_at=None):
        row = self._row(c, "v2_evaluations", "evaluation_id", evaluation_id)
        if len(parents) != len(set(parents)):
            raise V2PersistenceError("Duplicate parent references")
        for parent in parents:
            self._reference(c, parent, evaluation_id)
        sequence = c.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM v2_artifacts WHERE evaluation_id=?", (evaluation_id,)).fetchone()[0]
        artifact = Artifact(reference=ArtifactRef(opportunity_id=row["opportunity_id"],
            evaluation_id=evaluation_id, artifact_id=identifier or str(uuid4()), sequence=sequence),
            artifact_type=kind, payload=payload, producer=producer, parent_references=parents, created_at=created_at or _now())
        c.execute("INSERT INTO v2_artifacts VALUES (?,?,?,?,?,?,?,?)", (
            artifact.reference.artifact_id, evaluation_id, sequence, kind.value, payload.model_dump_json(),
            producer.model_dump_json(), _json([p.model_dump(mode="json") for p in parents]), artifact.created_at.isoformat()))
        return artifact

    def _partial_subject(self, c, evaluation_id, reason, parents=(), identifier=None):
        row = self._row(c, "v2_evaluations", "evaluation_id", evaluation_id)
        previous = c.execute("SELECT artifact_id FROM v2_artifacts WHERE artifact_id=?", (row["current_subject_id"],)).fetchone()
        if previous is None and (identifier is None or identifier != row["current_subject_id"]):
            raise V2PersistenceError("Missing persisted review subject; no implicit repair")
        prior = self._artifact(c, previous[0]).reference if previous else None
        sequence = c.execute("SELECT COALESCE(MAX(sequence),0) FROM v2_artifacts WHERE evaluation_id=?", (evaluation_id,)).fetchone()[0]
        subject = self._insert_artifact(c, evaluation_id, ArtifactType.REVIEW_SUBJECT,
            PartialReviewSubject(reason=reason, execution_state=row["execution_state"],
                covered_through_sequence=sequence, supersedes_subject=prior), APPLICATION,
            tuple(dict.fromkeys((*parents, *((prior,) if prior else ())))), identifier)
        c.execute("UPDATE v2_evaluations SET current_subject_id=?,updated_at=? WHERE evaluation_id=?",
                  (subject.reference.artifact_id, subject.created_at.isoformat(), evaluation_id))
        return subject

    def _create_evaluation(self, c, opportunity_id, predecessor_id=None):
        self._opportunity(c, opportunity_id)
        if predecessor_id and self._evaluation(c, predecessor_id).opportunity_id != opportunity_id:
            raise V2PersistenceError("Predecessor must belong to the same Opportunity")
        identifier, subject_id, now = str(uuid4()), str(uuid4()), _now().isoformat()
        c.execute("INSERT INTO v2_evaluations VALUES (?,?,?,?,?,?,?,?)",
                  (identifier, opportunity_id, ExecutionState.RUNNING.value, now, now, predecessor_id, None, subject_id))
        self._partial_subject(c, identifier, "Accepted Evaluation; no completed economic summary exists.", identifier=subject_id)
        return self._evaluation(c, identifier)

    def accept_opportunity(self, submission: OpportunitySubmission, *, operation_key: str) -> AcceptedEvaluation:
        submission = OpportunitySubmission.model_validate(submission)
        def perform(c):
            identifier, now = str(uuid4()), _now()
            c.execute("INSERT INTO v2_opportunities VALUES (?,?,?)", (identifier, submission.model_dump_json(), now.isoformat()))
            return AcceptedEvaluation(opportunity=self._opportunity(c, identifier), evaluation=self._create_evaluation(c, identifier))
        return self._once(operation_key, "accept", submission.model_dump(mode="json"), AcceptedEvaluation, perform)

    def create_evaluation(self, opportunity_id: str, *, predecessor_evaluation_id=None, operation_key: str) -> Evaluation:
        return self._once(operation_key, "evaluate", {"opportunity_id": opportunity_id, "predecessor": predecessor_evaluation_id},
            Evaluation, lambda c: self._create_evaluation(c, opportunity_id, predecessor_evaluation_id))

    def set_execution_state(self, evaluation_id: str, state: ExecutionState, *, operation_key: str) -> Evaluation:
        state = ExecutionState(state)
        def perform(c):
            current = self._evaluation(c, evaluation_id)
            if state != current.execution_state:
                if current.execution_state != ExecutionState.RUNNING:
                    raise V2PersistenceError("Terminal execution state cannot be resumed; create a linked Evaluation")
                c.execute("UPDATE v2_evaluations SET execution_state=? WHERE evaluation_id=?", (state.value, evaluation_id))
                if current.current_summary == current.current_review_subject:
                    # Execution state alone does not revise unchanged economic content.
                    c.execute("UPDATE v2_evaluations SET updated_at=? WHERE evaluation_id=?", (_now().isoformat(), evaluation_id))
                else:
                    self._partial_subject(c, evaluation_id, "Execution state changed; review includes this update.")
            return self._evaluation(c, evaluation_id)
        return self._once(operation_key, "state", {"evaluation_id": evaluation_id, "state": state.value}, Evaluation, perform)

    def record_artifact(self, evaluation_id: str, payload: FailureRecord | AssumptionRecord | RawResearchReport, *,
                        producer: Producer, parents: tuple[ArtifactRef, ...] = (), operation_key: str) -> Artifact:
        if not isinstance(payload, (FailureRecord, AssumptionRecord, RawResearchReport)):
            raise V2PersistenceError("Only validated failure, assumption, or raw-report payloads are supported here")
        payload = type(payload).model_validate(payload)
        producer = Producer.model_validate(producer)
        parents = tuple(ArtifactRef.model_validate(p) for p in parents)
        kind = (ArtifactType.FAILURE if isinstance(payload, FailureRecord) else
                ArtifactType.SCENARIO_ASSUMPTION if isinstance(payload, AssumptionRecord) else ArtifactType.RAW_RESEARCH_REPORT)
        def perform(c):
            if isinstance(payload, AssumptionRecord):
                self._validate_provenance(c, payload.assumption.provenance, evaluation_id)
            artifact = self._insert_artifact(c, evaluation_id, kind, payload, producer, parents)
            self._partial_subject(c, evaluation_id, "New persisted information; any prior summary/review is historical.", (artifact.reference,))
            return artifact
        return self._once(operation_key, "artifact", {"evaluation_id": evaluation_id, "payload": payload.model_dump(mode="json"),
            "producer": producer.model_dump(mode="json"), "parents": [p.model_dump(mode="json") for p in parents]}, Artifact, perform)

    def record_summary(self, evaluation_id: str, draft: SummaryDraft, *, producer: Producer, operation_key: str) -> Artifact:
        draft, producer = SummaryDraft.model_validate(draft), Producer.model_validate(producer)
        def perform(c):
            evaluation = self._evaluation(c, evaluation_id)
            calculations = [self._typed_reference(c, ref, evaluation_id, {ArtifactType.CALCULATION})
                            for ref in draft.calculation_references]
            for calculation in calculations:
                scenario = self._typed_reference(c, calculation.payload.scenario, evaluation_id, {ArtifactType.ECONOMIC_SCENARIO})
                if scenario.payload != calculation.payload.scenario_manifest:
                    raise V2PersistenceError("Calculation manifest does not match its exact scenario")
            if draft.numerical_estimate:
                if draft.assessment_state == AssessmentState.INSUFFICIENT_INFORMATION or not calculations:
                    raise V2PersistenceError("Numerical estimate requires an estimate summary and actual calculation")
            if draft.assessment_state != AssessmentState.INSUFFICIENT_INFORMATION and any(
                    a.payload.status != ComputationStatus.CALCULATED for a in calculations):
                raise V2PersistenceError("An estimate cannot cite an incomplete calculation as completed arithmetic")
            for ref in draft.evidence_references:
                self._reference(c, ref, evaluation_id)
            for item in (*draft.known_economic_facts, *draft.assumptions):
                self._validate_provenance(c, item.provenance, evaluation_id)
            old = self._artifact(c, evaluation.current_summary.artifact_id) if evaluation.current_summary else None
            summary = EconomicAssessmentSummary(**draft.model_dump(), revision=old.payload.revision + 1 if old else 1,
                supersedes=old.reference if old else None)
            parents = tuple(dict.fromkeys((*draft.evidence_references, *draft.calculation_references,
                                          *((old.reference,) if old else ()))))
            artifact = self._insert_artifact(c, evaluation_id, ArtifactType.EVALUATION_SUMMARY, summary, producer, parents)
            c.execute("UPDATE v2_evaluations SET current_summary_id=?,current_subject_id=?,updated_at=? WHERE evaluation_id=?",
                (artifact.reference.artifact_id, artifact.reference.artifact_id, artifact.created_at.isoformat(), evaluation_id))
            return artifact
        # Preserve the canonical request of pre-Checkpoint-3 qualitative summary receipts.
        encoded_draft = draft.model_dump(mode="json", exclude={"numerical_estimate"} if not draft.numerical_estimate else set())
        return self._once(operation_key, "summary", {"evaluation_id": evaluation_id, "draft": encoded_draft,
            "producer": producer.model_dump(mode="json")}, Artifact, perform)

    def record_review(self, evaluation_id: str, subject: ArtifactRef, decision: ReviewDecision, rationale: str, *,
                      human: HumanActor, concerns=(), requested_follow_up=(), supersedes_review_id=None,
                      operation_key: str) -> HumanReviewDecision:
        subject, decision, human = ArtifactRef.model_validate(subject), ReviewDecision(decision), HumanActor.model_validate(human)
        # Validate human data before a write; IDs/timestamps are assigned below.
        proposed = HumanReviewDecision(review_id="pending", evaluation_id=evaluation_id, sequence=1,
            reviewed_subject=subject, decision=decision, rationale=rationale, human=human, recorded_at=_now(),
            concerns=concerns, requested_follow_up=requested_follow_up, supersedes_review_id=supersedes_review_id)
        def perform(c):
            actual = self._reference(c, subject, evaluation_id)
            if actual.artifact_type not in {ArtifactType.EVALUATION_SUMMARY, ArtifactType.REVIEW_SUBJECT}:
                raise V2PersistenceError("Human reviews require an exact persisted summary or partial review subject")
            if supersedes_review_id:
                prior = self._review(c, supersedes_review_id)
                latest = c.execute("SELECT review_id FROM v2_reviews WHERE evaluation_id=? AND subject_id=? ORDER BY sequence DESC LIMIT 1",
                                   (evaluation_id, subject.artifact_id)).fetchone()
                if (prior.evaluation_id != evaluation_id or prior.reviewed_subject != subject
                        or not latest or latest[0] != supersedes_review_id):
                    raise V2PersistenceError("Supersession requires the latest review of this exact subject")
            sequence = c.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM v2_reviews WHERE evaluation_id=?", (evaluation_id,)).fetchone()[0]
            record = HumanReviewDecision(**proposed.model_dump(exclude={"review_id", "sequence", "recorded_at"}),
                                         review_id=str(uuid4()), sequence=sequence, recorded_at=_now())
            c.execute("INSERT INTO v2_reviews VALUES (?,?,?,?,?,?)", (record.review_id, evaluation_id, sequence,
                subject.artifact_id, supersedes_review_id, record.model_dump_json()))
            return record
        request = proposed.model_dump(mode="json", exclude={"review_id", "sequence", "recorded_at"})
        return self._once(operation_key, "review", request, HumanReviewDecision, perform)

    def _review(self, c, identifier):
        row = self._row(c, "v2_reviews", "review_id", identifier)
        record = HumanReviewDecision.model_validate_json(row["record_json"])
        if (record.review_id != row["review_id"] or record.evaluation_id != row["evaluation_id"]
                or record.sequence != row["sequence"] or record.reviewed_subject.artifact_id != row["subject_id"]
                or record.supersedes_review_id != row["supersedes_id"]):
            raise V2PersistenceError("Stored review columns and payload disagree")
        self._reference(c, record.reviewed_subject, record.evaluation_id)
        return record

    def _queue_entry(self, c, evaluation_id):
        evaluation = self._evaluation(c, evaluation_id)
        row = c.execute("SELECT review_id FROM v2_reviews WHERE evaluation_id=? AND subject_id=? ORDER BY sequence DESC LIMIT 1",
                        (evaluation_id, evaluation.current_review_subject.artifact_id)).fetchone()
        review = self._review(c, row[0]) if row else None
        current = evaluation.current_summary == evaluation.current_review_subject
        summary = self._artifact(c, evaluation.current_summary.artifact_id) if evaluation.current_summary else None
        return ReviewQueueEntry(evaluation=evaluation, review_state=review.decision if review else "PENDING",
            current_review=review, assessment_state=summary.payload.assessment_state if summary and current else None,
            summary_matches_current_subject=current)

    def get_opportunity(self, opportunity_id):
        with self._connection() as c:
            return self._opportunity(c, opportunity_id)

    def get_evaluation(self, evaluation_id):
        with self._connection() as c:
            return self._evaluation(c, evaluation_id)

    def get_artifact(self, reference: ArtifactRef):
        with self._connection() as c:
            return self._reference(c, reference)

    def list_artifacts(self, evaluation_id):
        with self._connection() as c:
            self._evaluation(c, evaluation_id)
            rows = c.execute("SELECT artifact_id FROM v2_artifacts WHERE evaluation_id=? ORDER BY sequence", (evaluation_id,)).fetchall()
            return tuple(self._artifact(c, row[0]) for row in rows)

    def get_current_summary(self, evaluation_id):
        with self._connection() as c:
            evaluation = self._evaluation(c, evaluation_id)
            return self._artifact(c, evaluation.current_summary.artifact_id) if evaluation.current_summary else None

    def list_reviews(self, evaluation_id):
        with self._connection() as c:
            self._evaluation(c, evaluation_id)
            rows = c.execute("SELECT review_id FROM v2_reviews WHERE evaluation_id=? ORDER BY sequence", (evaluation_id,)).fetchall()
            return tuple(self._review(c, row[0]) for row in rows)

    def review_status(self, evaluation_id):
        with self._connection() as c:
            return self._queue_entry(c, evaluation_id)

    def review_is_current(self, review_id):
        with self._connection() as c:
            review = self._review(c, review_id)
            current = self._queue_entry(c, review.evaluation_id).current_review
            return current is not None and current.review_id == review_id

    def review_subject_is_current(self, review_id):
        """Separate exact revision applicability from being the latest decision."""
        with self._connection() as c:
            review = self._review(c, review_id)
            return review.reviewed_subject == self._evaluation(c, review.evaluation_id).current_review_subject

    def list_review_queue(self, *, limit=25, offset=0):
        if type(limit) is not int or type(offset) is not int or not 1 <= limit <= 100 or offset < 0:
            raise V2PersistenceError("Queue pagination requires limit 1..100 and nonnegative offset")
        with self._connection() as c:
            total = c.execute("SELECT COUNT(*) FROM v2_evaluations").fetchone()[0]
            rows = c.execute("SELECT evaluation_id FROM v2_evaluations ORDER BY created_at,evaluation_id LIMIT ? OFFSET ?", (limit, offset)).fetchall()
            return ReviewQueuePage(entries=tuple(self._queue_entry(c, row[0]) for row in rows), total=total, limit=limit, offset=offset)

    def _typed_reference(self, c, reference, evaluation_id, kinds):
        artifact = self._reference(c, reference, evaluation_id)
        if artifact.artifact_type not in kinds:
            raise V2PersistenceError("Referenced artifact has an incorrect evidence type")
        return artifact

    def _evidence_history(self, c, evaluation_id, kind):
        self._evaluation(c, evaluation_id)
        rows = c.execute("SELECT artifact_id FROM v2_artifacts WHERE evaluation_id=? AND artifact_type=? ORDER BY sequence",
                         (evaluation_id, kind.value)).fetchall()
        return tuple(self._artifact(c, row[0]) for row in rows)

    def _current_lineage(self, c, evaluation_id, identifier, kind, identity_field):
        history = self._evidence_history(c, evaluation_id, kind)
        matches = [a for a in history if getattr(a.payload, identity_field) == identifier]
        if not matches:
            raise V2PersistenceError("Evidence lineage does not exist in this Evaluation")
        return matches[-1]

    def record_comparable(self, evaluation_id, draft: ComparableDraft, *, producer: Producer,
                          operation_key: str, parents=(), predecessor: ArtifactRef | None = None):
        """Initial identity and later immutable revisions share this atomic writer."""
        draft, producer = ComparableDraft.model_validate(draft), Producer.model_validate(producer)
        parents = tuple(ArtifactRef.model_validate(p) for p in parents)
        predecessor = ArtifactRef.model_validate(predecessor) if predecessor else None
        def perform(c):
            evaluation = self._evaluation(c, evaluation_id)
            old = self._typed_reference(c, predecessor, evaluation_id, {ArtifactType.RESALE_COMPARABLE}) if predecessor else None
            if old and self._current_lineage(c, evaluation_id, old.payload.comparable_id,
                    ArtifactType.RESALE_COMPARABLE, "comparable_id").reference != predecessor:
                raise V2PersistenceError("Comparable revision requires the latest predecessor; branching is refused")
            for raw in draft.source.raw_artifact_references:
                self._typed_reference(c, raw, evaluation_id, {ArtifactType.RAW_RESEARCH_REPORT})
            now = _now()
            payload = ResaleComparable(**draft.model_dump(), comparable_id=old.payload.comparable_id if old else str(uuid4()),
                opportunity_id=evaluation.opportunity_id, evaluation_id=evaluation_id, revision=old.payload.revision+1 if old else 1,
                supersedes=predecessor, captured_at=now)
            # Explicit caller parents are still validated (including duplicates).
            if len(parents) != len(set(parents)):
                raise V2PersistenceError("Duplicate parent references")
            references = tuple(dict.fromkeys((*parents, *draft.source.raw_artifact_references,
                                             *((predecessor,) if predecessor else ()))))
            artifact = self._insert_artifact(c, evaluation_id, ArtifactType.RESALE_COMPARABLE, payload, producer,
                                             references, created_at=now)
            self._partial_subject(c, evaluation_id, "New comparable evidence; prior reviews do not cover this revision.", (artifact.reference,))
            return artifact
        return self._once(operation_key, "comparable", {"evaluation_id":evaluation_id, "draft":draft.model_dump(mode="json"),
            "producer":producer.model_dump(mode="json"), "parents":[p.model_dump(mode="json") for p in parents],
            "predecessor":predecessor.model_dump(mode="json") if predecessor else None}, Artifact, perform)

    def record_comparable_revision(self, previous: ArtifactRef, draft: ComparableDraft, **options):
        previous = ArtifactRef.model_validate(previous)
        return self.record_comparable(previous.evaluation_id, draft, predecessor=previous, **options)

    def record_assessment(self, evaluation_id, comparable: ArtifactRef, draft: AssessmentDraft, *,
                          producer: Producer, operation_key: str, parents=(), predecessor: ArtifactRef | None = None):
        comparable, draft, producer = ArtifactRef.model_validate(comparable), AssessmentDraft.model_validate(draft), Producer.model_validate(producer)
        parents = tuple(ArtifactRef.model_validate(p) for p in parents)
        predecessor = ArtifactRef.model_validate(predecessor) if predecessor else None
        def perform(c):
            target = self._typed_reference(c, comparable, evaluation_id, {ArtifactType.RESALE_COMPARABLE})
            self._typed_reference(c, draft.candidate_context, evaluation_id, {ArtifactType.REVIEW_SUBJECT, ArtifactType.EVALUATION_SUMMARY})
            for ref in draft.evidence_references:
                self._typed_reference(c, ref, evaluation_id, {ArtifactType.RAW_RESEARCH_REPORT, ArtifactType.RESALE_COMPARABLE})
            for support in draft.provenance.supporting_records:
                self._reference(c, support.artifact, evaluation_id)
            self._validate_provenance(c, draft.provenance, evaluation_id)
            old = self._typed_reference(c, predecessor, evaluation_id, {ArtifactType.COMPARABLE_ASSESSMENT}) if predecessor else None
            if old:
                old_target = self._typed_reference(c, old.payload.comparable, evaluation_id, {ArtifactType.RESALE_COMPARABLE})
                if old_target.payload.comparable_id != target.payload.comparable_id:
                    raise V2PersistenceError("An assessment lineage cannot change comparable identity")
                if self._current_lineage(c, evaluation_id, old.payload.assessment_id,
                        ArtifactType.COMPARABLE_ASSESSMENT, "assessment_id").reference != predecessor:
                    raise V2PersistenceError("Assessment revision requires the latest predecessor")
            now = _now()
            payload = ComparableAssessment(**draft.model_dump(), assessment_id=old.payload.assessment_id if old else str(uuid4()),
                comparable=comparable, opportunity_id=target.reference.opportunity_id, evaluation_id=evaluation_id,
                revision=old.payload.revision+1 if old else 1, supersedes=predecessor, assessed_at=now)
            if len(parents) != len(set(parents)):
                raise V2PersistenceError("Duplicate parent references")
            refs = tuple(dict.fromkeys((*parents, comparable, draft.candidate_context, *draft.evidence_references,
                                       *(s.artifact for s in draft.provenance.supporting_records), *((predecessor,) if predecessor else ()))))
            artifact = self._insert_artifact(c, evaluation_id, ArtifactType.COMPARABLE_ASSESSMENT, payload, producer, refs, created_at=now)
            self._partial_subject(c, evaluation_id, "New attributed comparable assessment; no economic state inferred.", (artifact.reference,))
            return artifact
        return self._once(operation_key, "assessment", {"evaluation_id":evaluation_id,"comparable":comparable.model_dump(mode="json"),
            "draft":draft.model_dump(mode="json"),"producer":producer.model_dump(mode="json"),
            "parents":[p.model_dump(mode="json") for p in parents], "predecessor":predecessor.model_dump(mode="json") if predecessor else None}, Artifact, perform)

    def record_assessment_revision(self, previous: ArtifactRef, comparable: ArtifactRef, draft: AssessmentDraft, **options):
        previous = ArtifactRef.model_validate(previous)
        return self.record_assessment(previous.evaluation_id, comparable, draft, predecessor=previous, **options)

    def list_comparable_history(self, evaluation_id):
        with self._connection() as c:
            return self._evidence_history(c, evaluation_id, ArtifactType.RESALE_COMPARABLE)

    def get_comparable(self, reference: ArtifactRef):
        reference = ArtifactRef.model_validate(reference)
        with self._connection() as c:
            return self._typed_reference(c, reference, reference.evaluation_id, {ArtifactType.RESALE_COMPARABLE})

    def get_current_comparable(self, evaluation_id, comparable_id):
        with self._connection() as c:
            return self._current_lineage(c, evaluation_id, comparable_id, ArtifactType.RESALE_COMPARABLE, "comparable_id")

    def _assessment_history(self, c, evaluation_id, comparable_id):
        current = self._current_lineage(c, evaluation_id, comparable_id, ArtifactType.RESALE_COMPARABLE, "comparable_id")
        assessments = self._evidence_history(c, evaluation_id, ArtifactType.COMPARABLE_ASSESSMENT)
        relevant = [a for a in assessments if self._typed_reference(c, a.payload.comparable, evaluation_id,
                    {ArtifactType.RESALE_COMPARABLE}).payload.comparable_id == comparable_id]
        latest = {a.payload.assessment_id:a.reference for a in relevant}
        return tuple(AssessmentRevisionStatus(artifact=a,
            latest_assessment_revision=latest[a.payload.assessment_id] == a.reference,
            current_comparable_revision=a.payload.comparable == current.reference,
            is_current=latest[a.payload.assessment_id] == a.reference and a.payload.comparable == current.reference) for a in relevant)

    def list_assessment_history(self, evaluation_id, comparable_id):
        with self._connection() as c:
            return self._assessment_history(c, evaluation_id, comparable_id)

    def get_current_assessments(self, evaluation_id, comparable_id):
        with self._connection() as c:
            return tuple(a for a in self._assessment_history(c, evaluation_id, comparable_id) if a.is_current)

    def get_review_evidence(self, evaluation_id):
        with self._connection() as c:
            evaluation = self._evaluation(c, evaluation_id)
            rows = c.execute("SELECT artifact_id FROM v2_artifacts WHERE evaluation_id=? ORDER BY sequence", (evaluation_id,)).fetchall()
            return ReviewEvidenceSnapshot(evaluation=evaluation, artifacts=tuple(self._artifact(c, r[0]) for r in rows))

    def _scenario_references(self, c, evaluation_id, draft):
        """Validate identities, not truth or comparability. Never select evidence implicitly."""
        refs = list(draft.evidence_references)
        for ref in draft.evidence_references:
            self._reference(c, ref, evaluation_id)
        for ref in draft.comparable_references:
            self._typed_reference(c, ref, evaluation_id, {ArtifactType.RESALE_COMPARABLE})
            refs.append(ref)
        for ref in draft.assessment_references:
            assessment = self._typed_reference(c, ref, evaluation_id, {ArtifactType.COMPARABLE_ASSESSMENT})
            if assessment.payload.comparable not in draft.comparable_references:
                raise V2PersistenceError("Cited assessment must bind an explicitly cited exact comparable revision")
            refs.append(ref)
        for item in draft.inputs:
            self._validate_provenance(c, item.provenance, evaluation_id)
            for category in (KnowledgeSource.CURRENT_EVALUATION_EVIDENCE, KnowledgeSource.HISTORICAL_EVALUATIONS):
                if category in item.provenance.sources and not any(s.source == category for s in item.provenance.supporting_records):
                    raise V2PersistenceError("Economic evidence-based provenance requires exact supporting records")
            for support in item.provenance.supporting_records:
                if support.artifact.evaluation_id == evaluation_id:
                    refs.append(support.artifact)
            for ref in item.evidence_references:
                artifact = self._reference(c, ref, evaluation_id)
                if item.disposition == "EVIDENCE" and artifact.artifact_type in {
                        ArtifactType.SCENARIO_ASSUMPTION, ArtifactType.ECONOMIC_SCENARIO, ArtifactType.CALCULATION}:
                    raise V2PersistenceError("Assumptions/scenarios/calculations are not direct observed price/cost evidence")
                refs.append(ref)
            if item.assumption:
                assumption = self._typed_reference(c, item.assumption, evaluation_id, {ArtifactType.SCENARIO_ASSUMPTION}).payload.assumption
                if (assumption.name != item.input_id or assumption.amount != item.amount
                        or assumption.unit != f"{item.currency}/{item.cost_type.value}"
                        or assumption.provenance != item.provenance):
                    raise V2PersistenceError("Scenario assumption must exactly preserve named amount, unit/currency, and provenance")
                refs.append(item.assumption)
            if item.category == "RESALE_PRICE" and item.disposition == "EVIDENCE":
                matches = [self._typed_reference(c, ref, evaluation_id, {ArtifactType.RESALE_COMPARABLE})
                           for ref in item.evidence_references if ref in draft.comparable_references]
                if len(matches) != 1:
                    raise V2PersistenceError("Direct resale evidence must cite one exact comparable; combinations require an explicit assumption")
                price = matches[0].payload.price_evidence
                if price.amount != item.amount or price.currency != item.currency:
                    raise V2PersistenceError("Direct resale value must preserve the cited price; no averaging or inference")
        return tuple(dict.fromkeys(refs))

    def record_scenario(self, evaluation_id, draft: ScenarioDraft, *, producer: Producer,
                        operation_key: str, predecessor: ArtifactRef | None = None):
        draft, producer = ScenarioDraft.model_validate(draft), Producer.model_validate(producer)
        predecessor = ArtifactRef.model_validate(predecessor) if predecessor else None
        def perform(c):
            evaluation = self._evaluation(c, evaluation_id)
            old = self._typed_reference(c, predecessor, evaluation_id, {ArtifactType.ECONOMIC_SCENARIO}) if predecessor else None
            if old and self._current_lineage(c, evaluation_id, old.payload.scenario_id,
                    ArtifactType.ECONOMIC_SCENARIO, "scenario_id").reference != predecessor:
                raise V2PersistenceError("Scenario revision requires latest exact predecessor")
            refs = self._scenario_references(c, evaluation_id, draft)
            now = _now()
            payload = EconomicScenario(**draft.model_dump(), scenario_id=old.payload.scenario_id if old else str(uuid4()),
                opportunity_id=evaluation.opportunity_id, evaluation_id=evaluation_id,
                revision=old.payload.revision + 1 if old else 1, supersedes=predecessor, created_at=now)
            parents = tuple(dict.fromkeys((*refs, *((predecessor,) if predecessor else ()))))
            artifact = self._insert_artifact(c, evaluation_id, ArtifactType.ECONOMIC_SCENARIO, payload, producer,
                                             parents, created_at=now)
            self._partial_subject(c, evaluation_id, "New economic scenario; no assessment state inferred.", (artifact.reference,))
            return artifact
        return self._once(operation_key, "economic_scenario", {"evaluation_id": evaluation_id,
            "draft": draft.model_dump(mode="json"), "producer": producer.model_dump(mode="json"),
            "predecessor": predecessor.model_dump(mode="json") if predecessor else None}, Artifact, perform)

    def record_scenario_revision(self, previous: ArtifactRef, draft: ScenarioDraft, **options):
        previous = ArtifactRef.model_validate(previous)
        return self.record_scenario(previous.evaluation_id, draft, predecessor=previous, **options)

    def calculate_scenario(self, scenario: ArtifactRef, *, producer: Producer, operation_key: str):
        """Compute from the exact persisted revision and atomically record outcome + review subject."""
        scenario, producer = ArtifactRef.model_validate(scenario), Producer.model_validate(producer)
        def perform(c):
            artifact = self._typed_reference(c, scenario, scenario.evaluation_id, {ArtifactType.ECONOMIC_SCENARIO})
            manifest = artifact.payload
            draft = ScenarioDraft.model_validate(manifest.model_dump(include=set(ScenarioDraft.model_fields)))
            refs = self._scenario_references(c, scenario.evaluation_id, draft)
            assumptions = tuple(dict.fromkeys(i.assumption for i in draft.inputs if i.assumption))
            outcome = calculate_scenario(draft)
            now = _now()
            # Historical support remains an exact manifest reference, never a same-Evaluation parent.
            evidence_refs = tuple(dict.fromkeys((*refs, *(s.artifact for i in draft.inputs
                for s in i.provenance.supporting_records))))
            payload = CalculationRecord(**outcome.model_dump(), scenario=scenario, scenario_manifest=manifest,
                evidence_references=tuple(r for r in evidence_refs if r not in assumptions), assumption_references=assumptions,
                adapter_version=ADAPTER_VERSION, engine_version=ENGINE_VERSION, created_at=now)
            result = self._insert_artifact(c, scenario.evaluation_id, ArtifactType.CALCULATION, payload, producer,
                tuple(dict.fromkeys((scenario, *refs))), created_at=now)
            self._partial_subject(c, scenario.evaluation_id, "New calculation outcome; assessment remains author-selected.", (result.reference,))
            return result
        return self._once(operation_key, "calculate_scenario", {"scenario": scenario.model_dump(mode="json"),
            "producer": producer.model_dump(mode="json")}, Artifact, perform)

    def list_scenario_history(self, evaluation_id):
        with self._connection() as c:
            return self._evidence_history(c, evaluation_id, ArtifactType.ECONOMIC_SCENARIO)

    def get_scenario(self, reference: ArtifactRef):
        reference = ArtifactRef.model_validate(reference)
        with self._connection() as c:
            return self._typed_reference(c, reference, reference.evaluation_id, {ArtifactType.ECONOMIC_SCENARIO})

    def get_current_scenario(self, evaluation_id, scenario_id):
        with self._connection() as c:
            return self._current_lineage(c, evaluation_id, scenario_id, ArtifactType.ECONOMIC_SCENARIO, "scenario_id")

    def list_current_scenarios(self, evaluation_id):
        with self._connection() as c:
            history = self._evidence_history(c, evaluation_id, ArtifactType.ECONOMIC_SCENARIO)
            latest = {a.payload.scenario_id: a for a in history}
            return tuple(sorted(latest.values(), key=lambda a: a.reference.sequence))

    def list_calculation_history(self, evaluation_id, *, scenario: ArtifactRef | None = None):
        with self._connection() as c:
            if scenario is not None:
                scenario = ArtifactRef.model_validate(scenario)
                self._typed_reference(c, scenario, evaluation_id, {ArtifactType.ECONOMIC_SCENARIO})
            return tuple(a for a in self._evidence_history(c, evaluation_id, ArtifactType.CALCULATION)
                         if scenario is None or a.payload.scenario == scenario)

    def get_latest_calculation(self, scenario: ArtifactRef):
        scenario = ArtifactRef.model_validate(scenario)
        history = self.list_calculation_history(scenario.evaluation_id, scenario=scenario)
        return history[-1] if history else None
