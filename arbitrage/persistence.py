import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
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
    EvaluationStatus,
    EvaluationTrigger,
    ProductIdentity,
    ProfitabilityResult,
    ResaleResult,
    SourcingResult,
)


SCHEMA_VERSION = 1
DEFAULT_DATABASE_PATH = Path(__file__).resolve().parent.parent / "arbitrage.db"
TERMINAL_EVALUATION_STATUSES = {
    EvaluationStatus.COMPLETED,
    EvaluationStatus.INSUFFICIENT_EVIDENCE,
    EvaluationStatus.FAILED,
}


class PersistenceError(RuntimeError):
    """Base error for deterministic candidate persistence operations."""


class UnsupportedSchemaVersionError(PersistenceError):
    pass


class RecordNotFoundError(PersistenceError):
    pass


class InvalidStateTransitionError(PersistenceError):
    pass


class PersistenceDataError(PersistenceError):
    pass


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
            if stored_version != SCHEMA_VERSION:
                raise UnsupportedSchemaVersionError(
                    f"Unsupported schema version {stored_version}; expected {SCHEMA_VERSION}"
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
        if row is None:
            connection.execute(
                "INSERT INTO schema_metadata(key, value) VALUES ('schema_version', ?)",
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

    def update_evaluation_status(
        self, evaluation_id: str, status: EvaluationStatus
    ) -> CandidateEvaluation:
        if status not in {
            EvaluationStatus.IN_PROGRESS,
            EvaluationStatus.AWAITING_HUMAN_INPUT,
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
                EvaluationStatus.IN_PROGRESS: EvaluationStatus.AWAITING_HUMAN_INPUT,
                EvaluationStatus.AWAITING_HUMAN_INPUT: EvaluationStatus.IN_PROGRESS,
            }
            if allowed[current.status] != status:
                raise InvalidStateTransitionError(
                    f"Invalid transition from {current.status.value} to {status.value}"
                )
            candidate_status = (
                CandidateLifecycleStatus.AWAITING_HUMAN_INPUT
                if status == EvaluationStatus.AWAITING_HUMAN_INPUT
                else CandidateLifecycleStatus.INVESTIGATING
            )
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
                    _model_json(sourcing_result),
                    _model_json(resale_result),
                    _model_json(profitability_result),
                    json.dumps(final_assumptions),
                    json.dumps(final_uncertainties),
                    final_notes,
                    evaluation_id,
                ),
            )
            if status in {
                EvaluationStatus.COMPLETED,
                EvaluationStatus.INSUFFICIENT_EVIDENCE,
            }:
                connection.execute(
                    """
                    UPDATE candidates
                    SET lifecycle_status = ?, updated_at = ?, latest_evaluation_id = ?
                    WHERE candidate_id = ?
                    """,
                    (
                        CandidateLifecycleStatus.EVALUATED.value,
                        _timestamp(now),
                        evaluation_id,
                        current.candidate_id,
                    ),
                )
            else:
                candidate = connection.execute(
                    "SELECT latest_evaluation_id FROM candidates WHERE candidate_id = ?",
                    (current.candidate_id,),
                ).fetchone()
                candidate_status = (
                    CandidateLifecycleStatus.EVALUATED
                    if candidate["latest_evaluation_id"] is not None
                    else CandidateLifecycleStatus.INVESTIGATING
                )
                connection.execute(
                    """
                    UPDATE candidates SET lifecycle_status = ?, updated_at = ?
                    WHERE candidate_id = ?
                    """,
                    (
                        candidate_status.value,
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
            if status == CandidateLifecycleStatus.EVALUATED and (
                candidate.latest_evaluation_id is None
            ):
                raise InvalidStateTransitionError(
                    "EVALUATED requires a completed or insufficient-evidence evaluation"
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
