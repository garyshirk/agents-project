import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ValidationError

from arbitrage.contracts import (
    CandidateDestination,
    CandidateEvaluation,
    CandidateIntake,
    CandidateIntakeSource,
    CandidateLifecycleStatus,
    CandidateRecord,
    CandidateSource,
    EvaluationArtifact,
    EvaluationArtifactType,
    EvaluationStatus,
    EvaluationTrigger,
    ProductIdentity,
    ProfitabilityResult,
    ProfitabilityToolResult,
    ResaleResult,
    SourcingResult,
    SourcingTextReport,
)


SCHEMA_VERSION = 2
DEFAULT_DATABASE_PATH = Path(__file__).resolve().parent.parent / "arbitrage.db"
TERMINAL_EVALUATION_STATUSES = {
    EvaluationStatus.COMPLETED,
    EvaluationStatus.FAILED,
    EvaluationStatus.CANCELLED,
}


class PersistenceError(RuntimeError):
    """Base error for deterministic candidate persistence operations."""


class UnsupportedSchemaVersionError(PersistenceError):
    pass


class RecordNotFoundError(PersistenceError):
    pass


class InvalidStateTransitionError(PersistenceError):
    pass


class SubstantiveCompletionPrerequisiteError(InvalidStateTransitionError):
    pass


class ViableFinalizationPrerequisiteError(SubstantiveCompletionPrerequisiteError):
    pass


class ProfitabilityCorrectionRequiredError(SubstantiveCompletionPrerequisiteError):
    pass


class PersistenceDataError(PersistenceError):
    pass


class ProfitabilityArtifactState(str, Enum):
    NOT_ATTEMPTED = "NOT_ATTEMPTED"
    ONLY_FAILED = "ONLY_FAILED"
    SUCCESS_EXISTS = "SUCCESS_EXISTS"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.isoformat()


def _connect(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


@contextmanager
def _connection(database_path: Path):
    connection = _connect(database_path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def initialize_database(database_path: str | Path = DEFAULT_DATABASE_PATH) -> None:
    path = Path(database_path)
    with _connection(path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        row = connection.execute(
            "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
        ).fetchone()
        existing_application_tables = {
            item["name"]
            for item in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name IN ('candidates', 'candidate_evaluations')
                """
            ).fetchall()
        }
        if row is None and existing_application_tables:
            raise PersistenceDataError(
                "Candidate persistence tables exist without a schema version"
            )
        if row is not None:
            try:
                stored_version = int(row["value"])
            except (TypeError, ValueError) as exc:
                raise UnsupportedSchemaVersionError(
                    f"Invalid stored schema version: {row['value']!r}"
                ) from exc
            if stored_version not in {1, SCHEMA_VERSION}:
                raise UnsupportedSchemaVersionError(
                    f"Unsupported schema version {stored_version}; expected 1 or {SCHEMA_VERSION}"
                )

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS candidates (
                candidate_id TEXT PRIMARY KEY,
                product_identity_json TEXT NOT NULL,
                acquisition_source_json TEXT NOT NULL,
                resale_destination_json TEXT NOT NULL,
                intake_origin TEXT NOT NULL,
                lifecycle_status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                latest_evaluation_id TEXT,
                notes TEXT,
                FOREIGN KEY (latest_evaluation_id)
                    REFERENCES candidate_evaluations(evaluation_id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS candidate_evaluations (
                evaluation_id TEXT PRIMARY KEY,
                candidate_id TEXT NOT NULL,
                trigger TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                intake_snapshot_json TEXT,
                sourcing_result_json TEXT,
                resale_result_json TEXT,
                profitability_result_json TEXT,
                assumptions_json TEXT NOT NULL,
                uncertainties_json TEXT NOT NULL,
                manager_notes TEXT,
                FOREIGN KEY (candidate_id) REFERENCES candidates(candidate_id)
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_candidates_updated_at
            ON candidates(updated_at DESC)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_evaluations_candidate_started
            ON candidate_evaluations(candidate_id, started_at, evaluation_id)
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS evaluation_artifacts (
                artifact_id INTEGER PRIMARY KEY AUTOINCREMENT,
                evaluation_id TEXT NOT NULL,
                sequence_number INTEGER NOT NULL,
                artifact_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                context_json TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(evaluation_id, sequence_number),
                FOREIGN KEY (evaluation_id) REFERENCES candidate_evaluations(evaluation_id)
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_artifacts_evaluation_sequence
            ON evaluation_artifacts(evaluation_id, sequence_number)
            """
        )
        if row is None:
            connection.execute(
                "INSERT INTO schema_metadata(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
        elif stored_version == 1:
            connection.execute(
                """
                UPDATE candidate_evaluations
                SET status = CASE status
                    WHEN 'AWAITING_HUMAN_INPUT' THEN 'WAITING_FOR_INPUT'
                    WHEN 'INSUFFICIENT_EVIDENCE' THEN 'COMPLETED'
                    ELSE status END
                """
            )
            connection.execute(
                """
                UPDATE candidates
                SET lifecycle_status = CASE lifecycle_status
                    WHEN 'AWAITING_HUMAN_INPUT' THEN 'INVESTIGATING'
                    WHEN 'EVALUATED' THEN 'INVESTIGATING'
                    ELSE lifecycle_status END
                """
            )
            connection.execute(
                "UPDATE schema_metadata SET value = ? WHERE key = 'schema_version'",
                (str(SCHEMA_VERSION),),
            )


def _model_json(value: BaseModel | None) -> str | None:
    return None if value is None else value.model_dump_json()


def _parse_model(
    model_type: type[BaseModel], value: str | None, field_name: str
) -> BaseModel | None:
    if value is None:
        return None
    try:
        return model_type.model_validate_json(value)
    except (ValidationError, ValueError, TypeError) as exc:
        raise PersistenceDataError(f"Invalid stored {field_name}: {exc}") from exc


def _parse_list(value: str, field_name: str) -> list[str]:
    try:
        parsed = json.loads(value)
        if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
            raise ValueError("expected a list of strings")
        return parsed
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        raise PersistenceDataError(f"Invalid stored {field_name}: {exc}") from exc


def _candidate_from_row(row: sqlite3.Row) -> CandidateRecord:
    try:
        return CandidateRecord(
            candidate_id=row["candidate_id"],
            product_identity=_parse_model(
                ProductIdentity, row["product_identity_json"], "product identity"
            ),
            acquisition_source=_parse_model(
                CandidateSource, row["acquisition_source_json"], "acquisition source"
            ),
            resale_destination=_parse_model(
                CandidateDestination,
                row["resale_destination_json"],
                "resale destination",
            ),
            intake_origin=row["intake_origin"],
            lifecycle_status=row["lifecycle_status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            latest_evaluation_id=row["latest_evaluation_id"],
            notes=row["notes"],
        )
    except (ValidationError, ValueError, TypeError) as exc:
        raise PersistenceDataError(
            f"Invalid stored Candidate {row['candidate_id']}: {exc}"
        ) from exc


def _evaluation_from_row(row: sqlite3.Row) -> CandidateEvaluation:
    try:
        return CandidateEvaluation(
            evaluation_id=row["evaluation_id"],
            candidate_id=row["candidate_id"],
            trigger=row["trigger"],
            status=row["status"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            intake_snapshot=_parse_model(
                CandidateIntake, row["intake_snapshot_json"], "intake snapshot"
            ),
            sourcing_result=_parse_model(
                SourcingResult, row["sourcing_result_json"], "sourcing result"
            ),
            resale_result=_parse_model(
                ResaleResult, row["resale_result_json"], "resale result"
            ),
            profitability_result=_parse_model(
                ProfitabilityResult,
                row["profitability_result_json"],
                "profitability result",
            ),
            assumptions=_parse_list(row["assumptions_json"], "assumptions"),
            uncertainties=_parse_list(row["uncertainties_json"], "uncertainties"),
            manager_notes=row["manager_notes"],
        )
    except (ValidationError, ValueError, TypeError) as exc:
        raise PersistenceDataError(
            f"Invalid stored CandidateEvaluation {row['evaluation_id']}: {exc}"
        ) from exc


def _artifact_from_row(row: sqlite3.Row) -> EvaluationArtifact:
    try:
        return EvaluationArtifact(
            artifact_id=row["artifact_id"],
            evaluation_id=row["evaluation_id"],
            sequence_number=row["sequence_number"],
            artifact_type=row["artifact_type"],
            payload_json=row["payload_json"],
            context_json=row["context_json"],
            created_at=row["created_at"],
        )
    except (ValidationError, ValueError, TypeError) as exc:
        raise PersistenceDataError(
            f"Invalid stored EvaluationArtifact {row['artifact_id']}: {exc}"
        ) from exc


class CandidateRepository:
    def __init__(self, database_path: str | Path = DEFAULT_DATABASE_PATH) -> None:
        self.database_path = Path(database_path)
        initialize_database(self.database_path)

    def create_candidate(
        self,
        *,
        product_identity: ProductIdentity,
        acquisition_source: CandidateSource,
        resale_destination: CandidateDestination,
        intake_origin: CandidateIntakeSource,
        notes: str | None = None,
    ) -> CandidateRecord:
        now = _utc_now()
        candidate = CandidateRecord(
            candidate_id=str(uuid4()),
            product_identity=product_identity,
            acquisition_source=acquisition_source,
            resale_destination=resale_destination,
            intake_origin=intake_origin,
            lifecycle_status=CandidateLifecycleStatus.INVESTIGATING,
            created_at=now,
            updated_at=now,
            latest_evaluation_id=None,
            notes=notes,
        )
        with _connection(self.database_path) as connection:
            connection.execute(
                """
                INSERT INTO candidates VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate.candidate_id,
                    candidate.product_identity.model_dump_json(),
                    candidate.acquisition_source.model_dump_json(),
                    candidate.resale_destination.model_dump_json(),
                    candidate.intake_origin.value,
                    candidate.lifecycle_status.value,
                    _timestamp(candidate.created_at),
                    _timestamp(candidate.updated_at),
                    None,
                    candidate.notes,
                ),
            )
        return candidate

    def get_candidate(self, candidate_id: str) -> CandidateRecord | None:
        with _connection(self.database_path) as connection:
            row = connection.execute(
                "SELECT * FROM candidates WHERE candidate_id = ?", (candidate_id,)
            ).fetchone()
        return None if row is None else _candidate_from_row(row)

    def list_candidates(
        self,
        *,
        lifecycle_status: CandidateLifecycleStatus | None = None,
        intake_origin: CandidateIntakeSource | None = None,
        limit: int | None = None,
    ) -> list[CandidateRecord]:
        if limit is not None and limit < 1:
            raise ValueError("limit must be at least 1")
        conditions: list[str] = []
        values: list[Any] = []
        if lifecycle_status is not None:
            conditions.append("lifecycle_status = ?")
            values.append(lifecycle_status.value)
        if intake_origin is not None:
            conditions.append("intake_origin = ?")
            values.append(intake_origin.value)
        query = "SELECT * FROM candidates"
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY updated_at DESC, candidate_id ASC"
        if limit is not None:
            query += " LIMIT ?"
            values.append(limit)
        with _connection(self.database_path) as connection:
            rows = connection.execute(query, values).fetchall()
        return [_candidate_from_row(row) for row in rows]

    def create_evaluation(
        self,
        candidate_id: str,
        *,
        trigger: EvaluationTrigger,
        intake_snapshot: CandidateIntake | None = None,
        assumptions: list[str] | None = None,
        uncertainties: list[str] | None = None,
        manager_notes: str | None = None,
    ) -> CandidateEvaluation:
        now = _utc_now()
        evaluation = CandidateEvaluation(
            evaluation_id=str(uuid4()),
            candidate_id=candidate_id,
            trigger=trigger,
            status=EvaluationStatus.IN_PROGRESS,
            started_at=now,
            completed_at=None,
            intake_snapshot=intake_snapshot,
            sourcing_result=None,
            resale_result=None,
            profitability_result=None,
            assumptions=list(assumptions or []),
            uncertainties=list(uncertainties or []),
            manager_notes=manager_notes,
        )
        with _connection(self.database_path) as connection:
            if connection.execute(
                "SELECT 1 FROM candidates WHERE candidate_id = ?", (candidate_id,)
            ).fetchone() is None:
                raise RecordNotFoundError(f"Candidate not found: {candidate_id}")
            connection.execute(
                """
                INSERT INTO candidate_evaluations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evaluation.evaluation_id,
                    candidate_id,
                    evaluation.trigger.value,
                    evaluation.status.value,
                    _timestamp(evaluation.started_at),
                    None,
                    _model_json(evaluation.intake_snapshot),
                    None,
                    None,
                    None,
                    json.dumps(evaluation.assumptions),
                    json.dumps(evaluation.uncertainties),
                    evaluation.manager_notes,
                ),
            )
            connection.execute(
                """
                UPDATE candidates
                SET lifecycle_status = ?, updated_at = ?
                WHERE candidate_id = ?
                """,
                (
                    CandidateLifecycleStatus.INVESTIGATING.value,
                    _timestamp(now),
                    candidate_id,
                ),
            )
        return evaluation

    def create_candidate_with_evaluation(
        self,
        *,
        product_identity: ProductIdentity,
        acquisition_source: CandidateSource,
        resale_destination: CandidateDestination,
        intake_origin: CandidateIntakeSource,
        trigger: EvaluationTrigger,
        intake_snapshot: CandidateIntake | None = None,
        assumptions: list[str] | None = None,
        uncertainties: list[str] | None = None,
        candidate_notes: str | None = None,
        manager_notes: str | None = None,
    ) -> tuple[CandidateRecord, CandidateEvaluation]:
        now = _utc_now()
        candidate = CandidateRecord(
            candidate_id=str(uuid4()),
            product_identity=product_identity,
            acquisition_source=acquisition_source,
            resale_destination=resale_destination,
            intake_origin=intake_origin,
            lifecycle_status=CandidateLifecycleStatus.INVESTIGATING,
            created_at=now,
            updated_at=now,
            latest_evaluation_id=None,
            notes=candidate_notes,
        )
        evaluation = CandidateEvaluation(
            evaluation_id=str(uuid4()),
            candidate_id=candidate.candidate_id,
            trigger=trigger,
            status=EvaluationStatus.IN_PROGRESS,
            started_at=now,
            completed_at=None,
            intake_snapshot=intake_snapshot,
            sourcing_result=None,
            resale_result=None,
            profitability_result=None,
            assumptions=list(assumptions or []),
            uncertainties=list(uncertainties or []),
            manager_notes=manager_notes,
        )
        with _connection(self.database_path) as connection:
            connection.execute(
                "INSERT INTO candidates VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    candidate.candidate_id,
                    candidate.product_identity.model_dump_json(),
                    candidate.acquisition_source.model_dump_json(),
                    candidate.resale_destination.model_dump_json(),
                    candidate.intake_origin.value,
                    candidate.lifecycle_status.value,
                    _timestamp(candidate.created_at),
                    _timestamp(candidate.updated_at),
                    None,
                    candidate.notes,
                ),
            )
            connection.execute(
                """
                INSERT INTO candidate_evaluations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evaluation.evaluation_id,
                    candidate.candidate_id,
                    evaluation.trigger.value,
                    evaluation.status.value,
                    _timestamp(evaluation.started_at),
                    None,
                    _model_json(evaluation.intake_snapshot),
                    None,
                    None,
                    None,
                    json.dumps(evaluation.assumptions),
                    json.dumps(evaluation.uncertainties),
                    evaluation.manager_notes,
                ),
            )
        return candidate, evaluation

    def get_evaluation(self, evaluation_id: str) -> CandidateEvaluation | None:
        with _connection(self.database_path) as connection:
            row = connection.execute(
                "SELECT * FROM candidate_evaluations WHERE evaluation_id = ?",
                (evaluation_id,),
            ).fetchone()
        return None if row is None else _evaluation_from_row(row)

    def list_evaluations(self, candidate_id: str) -> list[CandidateEvaluation]:
        with _connection(self.database_path) as connection:
            rows = connection.execute(
                """
                SELECT * FROM candidate_evaluations
                WHERE candidate_id = ?
                ORDER BY started_at ASC, evaluation_id ASC
                """,
                (candidate_id,),
            ).fetchall()
        return [_evaluation_from_row(row) for row in rows]

    def append_evaluation_artifact(
        self,
        evaluation_id: str,
        *,
        artifact_type: EvaluationArtifactType,
        payload_json: str,
        context_json: str | None = None,
    ) -> EvaluationArtifact:
        try:
            json.loads(payload_json)
            if context_json is not None:
                json.loads(context_json)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError("artifact payload and context must be valid JSON") from exc

        latest_column: str | None = None
        latest_value: str | None = None
        if artifact_type == EvaluationArtifactType.SOURCING:
            SourcingResult.model_validate_json(payload_json)
            latest_column = "sourcing_result_json"
            latest_value = payload_json
        elif artifact_type == EvaluationArtifactType.SOURCING_REPORT:
            SourcingTextReport.model_validate_json(payload_json)
        elif artifact_type == EvaluationArtifactType.RESALE:
            ResaleResult.model_validate_json(payload_json)
            latest_column = "resale_result_json"
            latest_value = payload_json
        elif artifact_type == EvaluationArtifactType.PROFITABILITY:
            envelope = ProfitabilityToolResult.model_validate_json(payload_json)
            if envelope.success and envelope.result is not None:
                latest_column = "profitability_result_json"
                latest_value = envelope.result.model_dump_json()

        now = _utc_now()
        with _connection(self.database_path) as connection:
            evaluation = connection.execute(
                "SELECT status FROM candidate_evaluations WHERE evaluation_id = ?",
                (evaluation_id,),
            ).fetchone()
            if evaluation is None:
                raise RecordNotFoundError(f"Evaluation not found: {evaluation_id}")
            if EvaluationStatus(evaluation["status"]) in TERMINAL_EVALUATION_STATUSES:
                raise InvalidStateTransitionError(
                    "Cannot capture artifacts for a terminal Evaluation"
                )
            sequence = connection.execute(
                """
                SELECT COALESCE(MAX(sequence_number), 0) + 1
                FROM evaluation_artifacts WHERE evaluation_id = ?
                """,
                (evaluation_id,),
            ).fetchone()[0]
            cursor = connection.execute(
                """
                INSERT INTO evaluation_artifacts(
                    evaluation_id, sequence_number, artifact_type,
                    payload_json, context_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    evaluation_id,
                    sequence,
                    artifact_type.value,
                    payload_json,
                    context_json,
                    _timestamp(now),
                ),
            )
            if latest_column is not None:
                connection.execute(
                    f"UPDATE candidate_evaluations SET {latest_column} = ? WHERE evaluation_id = ?",
                    (latest_value, evaluation_id),
                )
            artifact_id = cursor.lastrowid
        assert artifact_id is not None
        with _connection(self.database_path) as connection:
            row = connection.execute(
                "SELECT * FROM evaluation_artifacts WHERE artifact_id = ?",
                (artifact_id,),
            ).fetchone()
        assert row is not None
        return _artifact_from_row(row)

    def list_evaluation_artifacts(
        self, evaluation_id: str
    ) -> list[EvaluationArtifact]:
        with _connection(self.database_path) as connection:
            rows = connection.execute(
                """
                SELECT * FROM evaluation_artifacts
                WHERE evaluation_id = ?
                ORDER BY sequence_number ASC
                """,
                (evaluation_id,),
            ).fetchall()
        return [_artifact_from_row(row) for row in rows]

    def profitability_artifact_state(
        self, evaluation_id: str
    ) -> ProfitabilityArtifactState:
        with _connection(self.database_path) as connection:
            if connection.execute(
                "SELECT 1 FROM candidate_evaluations WHERE evaluation_id = ?",
                (evaluation_id,),
            ).fetchone() is None:
                raise RecordNotFoundError(f"Evaluation not found: {evaluation_id}")
            return self._profitability_artifact_state(
                connection, evaluation_id
            )

    @staticmethod
    def _profitability_artifact_state(
        connection: sqlite3.Connection, evaluation_id: str
    ) -> ProfitabilityArtifactState:
        rows = connection.execute(
            """
            SELECT payload_json FROM evaluation_artifacts
            WHERE evaluation_id = ? AND artifact_type = ?
            ORDER BY sequence_number ASC
            """,
            (evaluation_id, EvaluationArtifactType.PROFITABILITY.value),
        ).fetchall()
        if not rows:
            return ProfitabilityArtifactState.NOT_ATTEMPTED
        for row in rows:
            try:
                result = ProfitabilityToolResult.model_validate_json(row["payload_json"])
            except (ValidationError, ValueError, TypeError) as exc:
                raise PersistenceDataError(
                    f"Invalid stored Profitability artifact for Evaluation {evaluation_id}: {exc}"
                ) from exc
            if result.success and result.result is not None:
                return ProfitabilityArtifactState.SUCCESS_EXISTS
        return ProfitabilityArtifactState.ONLY_FAILED

    def update_evaluation_status(
        self, evaluation_id: str, status: EvaluationStatus
    ) -> CandidateEvaluation:
        if status not in {
            EvaluationStatus.IN_PROGRESS,
            EvaluationStatus.WAITING_FOR_INPUT,
        }:
            raise InvalidStateTransitionError(
                "update_evaluation_status accepts only nonterminal statuses"
            )
        now = _utc_now()
        with _connection(self.database_path) as connection:
            row = connection.execute(
                "SELECT * FROM candidate_evaluations WHERE evaluation_id = ?",
                (evaluation_id,),
            ).fetchone()
            if row is None:
                raise RecordNotFoundError(f"Evaluation not found: {evaluation_id}")
            current = _evaluation_from_row(row)
            if current.status in TERMINAL_EVALUATION_STATUSES:
                raise InvalidStateTransitionError("Terminal evaluations are immutable")
            allowed = {
                EvaluationStatus.IN_PROGRESS: EvaluationStatus.WAITING_FOR_INPUT,
                EvaluationStatus.WAITING_FOR_INPUT: EvaluationStatus.IN_PROGRESS,
            }
            if allowed[current.status] != status:
                raise InvalidStateTransitionError(
                    f"Invalid transition from {current.status.value} to {status.value}"
                )
            candidate_status = CandidateLifecycleStatus.INVESTIGATING
            connection.execute(
                "UPDATE candidate_evaluations SET status = ? WHERE evaluation_id = ?",
                (status.value, evaluation_id),
            )
            connection.execute(
                """
                UPDATE candidates SET lifecycle_status = ?, updated_at = ?
                WHERE candidate_id = ?
                """,
                (candidate_status.value, _timestamp(now), current.candidate_id),
            )
        result = self.get_evaluation(evaluation_id)
        assert result is not None
        return result

    def complete_evaluation(
        self,
        evaluation_id: str,
        *,
        status: EvaluationStatus,
        intake_snapshot: CandidateIntake | None = None,
        sourcing_result: SourcingResult | None = None,
        resale_result: ResaleResult | None = None,
        profitability_result: ProfitabilityResult | None = None,
        candidate_status: CandidateLifecycleStatus | None = None,
        assumptions: list[str] | None = None,
        uncertainties: list[str] | None = None,
        manager_notes: str | None = None,
    ) -> CandidateEvaluation:
        if status not in TERMINAL_EVALUATION_STATUSES:
            raise InvalidStateTransitionError("Completion requires a terminal status")
        now = _utc_now()
        with _connection(self.database_path) as connection:
            row = connection.execute(
                "SELECT * FROM candidate_evaluations WHERE evaluation_id = ?",
                (evaluation_id,),
            ).fetchone()
            if row is None:
                raise RecordNotFoundError(f"Evaluation not found: {evaluation_id}")
            current = _evaluation_from_row(row)
            if current.status in TERMINAL_EVALUATION_STATUSES:
                raise InvalidStateTransitionError("Terminal evaluations are immutable")
            if current.status == EvaluationStatus.WAITING_FOR_INPUT and (
                status == EvaluationStatus.CANCELLED
            ):
                pass
            elif current.status != EvaluationStatus.IN_PROGRESS:
                raise InvalidStateTransitionError(
                    "Only an IN_PROGRESS Evaluation may finish, except that a "
                    "WAITING_FOR_INPUT Evaluation may be CANCELLED"
                )
            allowed_candidate_statuses = {
                EvaluationStatus.COMPLETED: {
                    CandidateLifecycleStatus.VIABLE,
                    CandidateLifecycleStatus.REJECTED,
                    CandidateLifecycleStatus.INVESTIGATING,
                },
                EvaluationStatus.FAILED: {CandidateLifecycleStatus.INVESTIGATING},
                EvaluationStatus.CANCELLED: {
                    CandidateLifecycleStatus.INVESTIGATING,
                    CandidateLifecycleStatus.CLOSED,
                },
            }
            final_candidate_status = candidate_status or CandidateLifecycleStatus.INVESTIGATING
            if final_candidate_status not in allowed_candidate_statuses[status]:
                raise InvalidStateTransitionError(
                    f"{status.value} cannot produce Candidate {final_candidate_status.value}"
                )
            if status == EvaluationStatus.COMPLETED:
                profitability_state = self._profitability_artifact_state(
                    connection, evaluation_id
                )
                if profitability_state == ProfitabilityArtifactState.ONLY_FAILED:
                    raise ProfitabilityCorrectionRequiredError(
                        "Profitability was attempted but no invocation completed "
                        "successfully for the current Evaluation"
                    )
                if (
                    final_candidate_status == CandidateLifecycleStatus.VIABLE
                    and profitability_state
                    != ProfitabilityArtifactState.SUCCESS_EXISTS
                ):
                    raise ViableFinalizationPrerequisiteError(
                        "VIABLE requires at least one successful Profitability result "
                        "for the current Evaluation"
                    )
            final_assumptions = current.assumptions if assumptions is None else list(assumptions)
            final_uncertainties = (
                current.uncertainties if uncertainties is None else list(uncertainties)
            )
            final_notes = current.manager_notes if manager_notes is None else manager_notes
            final_intake = (
                current.intake_snapshot if intake_snapshot is None else intake_snapshot
            )
            connection.execute(
                """
                UPDATE candidate_evaluations
                SET status = ?, completed_at = ?, intake_snapshot_json = ?, sourcing_result_json = ?,
                    resale_result_json = ?, profitability_result_json = ?,
                    assumptions_json = ?, uncertainties_json = ?, manager_notes = ?
                WHERE evaluation_id = ?
                """,
                (
                    status.value,
                    _timestamp(now),
                    _model_json(final_intake),
                    _model_json(sourcing_result or current.sourcing_result),
                    _model_json(resale_result or current.resale_result),
                    _model_json(profitability_result or current.profitability_result),
                    json.dumps(final_assumptions),
                    json.dumps(final_uncertainties),
                    final_notes,
                    evaluation_id,
                ),
            )
            if status == EvaluationStatus.COMPLETED:
                connection.execute(
                    """
                    UPDATE candidates
                    SET lifecycle_status = ?, updated_at = ?, latest_evaluation_id = ?
                    WHERE candidate_id = ?
                    """,
                    (
                        final_candidate_status.value,
                        _timestamp(now),
                        evaluation_id,
                        current.candidate_id,
                    ),
                )
            else:
                connection.execute(
                    """
                    UPDATE candidates SET lifecycle_status = ?, updated_at = ?
                    WHERE candidate_id = ?
                    """,
                    (
                        final_candidate_status.value,
                        _timestamp(now),
                        current.candidate_id,
                    ),
                )
        result = self.get_evaluation(evaluation_id)
        assert result is not None
        return result

    def update_candidate_status(
        self,
        candidate_id: str,
        status: CandidateLifecycleStatus,
        *,
        notes: str | None = None,
    ) -> CandidateRecord:
        now = _utc_now()
        with _connection(self.database_path) as connection:
            row = connection.execute(
                "SELECT * FROM candidates WHERE candidate_id = ?", (candidate_id,)
            ).fetchone()
            if row is None:
                raise RecordNotFoundError(f"Candidate not found: {candidate_id}")
            candidate = _candidate_from_row(row)
            if status in {
                CandidateLifecycleStatus.VIABLE,
                CandidateLifecycleStatus.REJECTED,
            } and candidate.latest_evaluation_id is None:
                raise InvalidStateTransitionError(
                    f"{status.value} requires a completed Evaluation"
                )
            connection.execute(
                """
                UPDATE candidates SET lifecycle_status = ?, updated_at = ?, notes = ?
                WHERE candidate_id = ?
                """,
                (status.value, _timestamp(now), notes, candidate_id),
            )
        result = self.get_candidate(candidate_id)
        assert result is not None
        return result
