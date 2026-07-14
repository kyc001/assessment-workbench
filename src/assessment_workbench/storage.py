from __future__ import annotations

import builtins
import hashlib
import json
import os
import re
import socket
import sqlite3
import tempfile
import time
from pathlib import Path
from uuid import UUID, uuid4

from assessment_workbench.domain import (
    ArtifactRef,
    HumanDecision,
    HumanDecisionType,
    HumanReviewRequest,
    KnowledgePoint,
    KnowledgeRelation,
    Material,
    MaterialStatus,
    ModelCall,
    ParsedDocument,
    PhaseEvent,
    PhaseStatus,
    RetrievalHit,
    RunStatus,
    WorkflowCheckpoint,
    WorkflowRun,
    now_utc,
    validate_run_transition,
)
from assessment_workbench.errors import is_retryable_failure

_EDITABLE_REPLACE_ATTEMPTS = 7
_EDITABLE_REPLACE_BASE_DELAY_SECONDS = 0.01


class Workspace:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.db_path = self.root / "workbench.db"
        self.artifacts = self.root / "artifacts"

    def initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.artifacts.mkdir(exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            _migrate_phase_events(connection)
            _migrate_runs(connection)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def require_initialized(self) -> None:
        if not self.db_path.exists():
            raise RuntimeError(f"workspace is not initialized: {self.root}")


class RunStore:
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def create(self, workflow: str) -> WorkflowRun:
        run = WorkflowRun(workflow=workflow)
        with self.workspace.connect() as connection:
            connection.execute(
                """INSERT INTO runs
                (id, workflow, status, current_phase, created_at, updated_at, error,
                 runner_host, runner_pid)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                _run_values(run),
            )
        return run

    def save(self, run: WorkflowRun) -> None:
        run.updated_at = now_utc()
        with self.workspace.connect() as connection:
            connection.execute(
                """UPDATE runs SET status=?, current_phase=?, updated_at=?, error=?,
                runner_host=?, runner_pid=? WHERE id=?""",
                (
                    run.status,
                    run.current_phase,
                    run.updated_at.isoformat(),
                    run.error,
                    run.runner_host,
                    run.runner_pid,
                    str(run.id),
                ),
            )

    def transition(
        self,
        run: WorkflowRun,
        target: RunStatus,
        *,
        current_phase: str | None = None,
        error: str | None = None,
    ) -> WorkflowRun:
        validate_run_transition(run.status, target)
        run.status = target
        if current_phase is not None:
            run.current_phase = current_phase
        run.error = error
        self.save(run)
        return run

    def request_cancel(self, run_id: UUID) -> WorkflowRun:
        run = self.get(run_id)
        if run is None:
            raise KeyError(f"run not found: {run_id}")
        if run.status is RunStatus.QUEUED:
            return self.transition(run, RunStatus.CANCELLED)
        return self.transition(run, RunStatus.CANCELLING)

    def claim(self, run: WorkflowRun, pid: int) -> WorkflowRun:
        run.runner_host = socket.gethostname()
        run.runner_pid = pid
        self.save(run)
        return run

    def release(self, run: WorkflowRun) -> WorkflowRun:
        run.runner_host = None
        run.runner_pid = None
        self.save(run)
        return run

    def retry_failed(self, run_id: UUID, *, actor: str, reason: str) -> WorkflowRun:
        if not actor.strip():
            raise ValueError("failed run retry requires an actor")
        if not reason.strip():
            raise ValueError("failed run retry requires a reason")
        run = self.get(run_id)
        if run is None:
            raise KeyError(f"run not found: {run_id}")
        if run.status is not RunStatus.FAILED:
            raise ValueError(f"run is not failed: {run.status}")
        checkpoint = self.get_checkpoint(run_id)
        if checkpoint is None:
            raise ValueError(f"failed run has no recovery checkpoint: {run_id}")
        failed_events = [
            event for event in self.events(run_id) if event.status is PhaseStatus.FAILED
        ]
        if not failed_events:
            raise ValueError(f"failed run has no failed phase event: {run_id}")
        failed = failed_events[-1]
        retryable_child_run_ids: list[UUID] = []
        retryable = is_retryable_failure(failed.error_code, failed.error)
        recovery_kind = "transient_failure" if retryable else ""
        if not retryable and _is_subject_synthesis_validation_recoverable(failed, checkpoint):
            retryable = True
            recovery_kind = "subject_synthesis_validation"
        if not retryable and _is_editable_projection_replace_recoverable(failed, checkpoint):
            retryable = True
            recovery_kind = "editable_projection_replace"
        if not retryable and failed.phase == "QUESTIONS_GENERATING":
            child_runs = [
                child
                for child_id in self.child_run_ids(run.id)
                if (child := self.get(child_id)) is not None
            ]
            retryable_child_run_ids = [
                child.id
                for child in child_runs
                if child.status in {RunStatus.FAILED, RunStatus.INTERRUPTED}
                and self._run_has_retryable_failure(child.id)
            ]
            nonrecoverable_children = [
                child
                for child in child_runs
                if child.status is not RunStatus.SUCCEEDED
                and child.id not in retryable_child_run_ids
            ]
            retryable = bool(retryable_child_run_ids) and not nonrecoverable_children
            if retryable:
                recovery_kind = "retryable_question_children"
        if not retryable:
            raise ValueError(f"failed run error is not retryable: {failed.error_code or 'unknown'}")
        validate_run_transition(run.status, RunStatus.INTERRUPTED)
        timestamp = now_utc()
        recovery_event = PhaseEvent(
            run_id=run.id,
            workflow=run.workflow,
            phase="RUN_RECOVERY",
            status=PhaseStatus.COMPLETED,
            occurrence_id=uuid4(),
            round=1 + sum(event.phase == "RUN_RECOVERY" for event in self.events(run.id)),
            started_at=timestamp,
            completed_at=timestamp,
            summary=reason.strip(),
            error_details={
                "actor": actor.strip(),
                "failed_phase": failed.phase,
                "failed_error_code": failed.error_code or "unknown",
                "failed_error": failed.error or "",
                "recovery_kind": recovery_kind,
                "retryable_child_run_ids": ",".join(
                    str(child_run_id) for child_run_id in retryable_child_run_ids
                ),
            },
        )
        run.status = RunStatus.INTERRUPTED
        run.error = f"retry requested by {actor.strip()}: {reason.strip()}"
        run.runner_host = None
        run.runner_pid = None
        run.updated_at = timestamp
        with self.workspace.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._insert_phase_event(connection, recovery_event)
            connection.execute(
                """UPDATE runs SET status=?, updated_at=?, error=?, runner_host=NULL,
                runner_pid=NULL WHERE id=?""",
                (run.status, run.updated_at.isoformat(), run.error, str(run.id)),
            )
            connection.commit()
        return run

    def recover_orphaned(self, *, host: str | None = None) -> list[WorkflowRun]:
        current_host = host or socket.gethostname()
        recovered: list[WorkflowRun] = []
        for run in self.list_runs():
            if run.status not in {RunStatus.RUNNING, RunStatus.CANCELLING}:
                continue
            if run.runner_host != current_host or run.runner_pid is None:
                continue
            if _pid_exists(run.runner_pid):
                continue
            self.transition(run, RunStatus.INTERRUPTED, error="runner process no longer exists")
            self.release(run)
            recovered.append(run)
        return recovered

    def save_checkpoint(self, checkpoint: WorkflowCheckpoint) -> None:
        with self.workspace.connect() as connection:
            self._upsert_checkpoint(connection, checkpoint)

    def commit_phase(self, event: PhaseEvent, checkpoint: WorkflowCheckpoint) -> None:
        if event.status is not PhaseStatus.COMPLETED:
            raise ValueError("only completed phase events can be committed with a checkpoint")
        if event.run_id != checkpoint.run_id or event.workflow != checkpoint.workflow:
            raise ValueError("phase event and checkpoint must belong to the same workflow run")
        with self.workspace.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._insert_phase_event(connection, event)
            self._upsert_checkpoint(connection, checkpoint)
            connection.commit()

    def commit_checkpoint_override(
        self,
        event: PhaseEvent,
        checkpoint: WorkflowCheckpoint,
        *,
        binding_key: str,
        expected_artifact_id: UUID,
    ) -> None:
        if event.status is not PhaseStatus.COMPLETED:
            raise ValueError("checkpoint override requires a completed phase event")
        if event.run_id != checkpoint.run_id or event.workflow != checkpoint.workflow:
            raise ValueError("phase event and checkpoint must belong to the same workflow run")
        with self.workspace.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM workflow_checkpoints WHERE run_id=?",
                (str(checkpoint.run_id),),
            ).fetchone()
            if row is None:
                raise ValueError(f"run has no checkpoint: {checkpoint.run_id}")
            current = WorkflowCheckpoint.model_validate_json(row["payload"])
            if current.artifact_bindings.get(binding_key) != expected_artifact_id:
                raise ValueError(f"checkpoint binding changed during override: {binding_key}")
            self._insert_phase_event(connection, event)
            self._upsert_checkpoint(connection, checkpoint)
            connection.commit()

    def get_checkpoint(self, run_id: UUID) -> WorkflowCheckpoint | None:
        with self.workspace.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM workflow_checkpoints WHERE run_id=?", (str(run_id),)
            ).fetchone()
        return WorkflowCheckpoint.model_validate_json(row["payload"]) if row else None

    def clear_checkpoint(self, run_id: UUID) -> None:
        with self.workspace.connect() as connection:
            connection.execute("DELETE FROM workflow_checkpoints WHERE run_id=?", (str(run_id),))

    def create_human_review(self, request: HumanReviewRequest) -> None:
        with self.workspace.connect() as connection:
            connection.execute(
                """INSERT INTO human_review_requests
                (id, run_id, phase, created_at, resolved_at, payload)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    str(request.id),
                    str(request.run_id),
                    request.phase,
                    request.created_at.isoformat(),
                    None,
                    request.model_dump_json(),
                ),
            )

    def pending_human_review(self, run_id: UUID) -> HumanReviewRequest | None:
        with self.workspace.connect() as connection:
            row = connection.execute(
                """SELECT payload FROM human_review_requests
                WHERE run_id=? AND resolved_at IS NULL ORDER BY created_at DESC LIMIT 1""",
                (str(run_id),),
            ).fetchone()
        return HumanReviewRequest.model_validate_json(row["payload"]) if row else None

    def resolve_human_review(self, decision: HumanDecision) -> WorkflowRun:
        request = self.pending_human_review(decision.run_id)
        if request is None or request.id != decision.request_id:
            raise ValueError(f"no matching pending human review for run: {decision.run_id}")
        run = self.get(decision.run_id)
        if run is None:
            raise KeyError(f"run not found: {decision.run_id}")
        if run.status is not RunStatus.WAITING_HUMAN:
            raise ValueError(f"run is not waiting for human review: {run.status}")
        if decision.decision not in request.allowed_decisions:
            raise ValueError(
                f"human decision {decision.decision.value!r} is not allowed for phase "
                f"{request.phase!r}"
            )
        checkpoint: WorkflowCheckpoint | None = None
        if decision.decision in {
            HumanDecisionType.ACCEPT,
            HumanDecisionType.EDIT_ACCEPT,
            HumanDecisionType.RETRY,
        }:
            checkpoint = self.get_checkpoint(decision.run_id)
            if checkpoint is None:
                raise ValueError(f"run has no checkpoint for human decision: {decision.run_id}")
        request.resolved_at = now_utc()
        with self.workspace.connect() as connection:
            connection.execute(
                """UPDATE human_review_requests SET resolved_at=?, payload=? WHERE id=?""",
                (request.resolved_at.isoformat(), request.model_dump_json(), str(request.id)),
            )
            connection.execute(
                """INSERT INTO human_decisions
                (id, request_id, run_id, decision, actor, created_at, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(decision.id),
                    str(decision.request_id),
                    str(decision.run_id),
                    decision.decision,
                    decision.actor,
                    decision.created_at.isoformat(),
                    decision.model_dump_json(),
                ),
            )
        if decision.decision in {
            HumanDecisionType.ACCEPT,
            HumanDecisionType.EDIT_ACCEPT,
            HumanDecisionType.RETRY,
        }:
            assert checkpoint is not None
            legacy_request = request.resume_step_index == request.retry_step_index == 0
            if decision.decision is HumanDecisionType.RETRY:
                checkpoint.next_step_index = (
                    max(0, checkpoint.next_step_index - 1)
                    if legacy_request
                    else request.retry_step_index
                )
            elif not legacy_request:
                checkpoint.next_step_index = request.resume_step_index
            checkpoint.human_decision_id = decision.id
            checkpoint.created_at = now_utc()
            self.save_checkpoint(checkpoint)
            return self.transition(run, RunStatus.INTERRUPTED)
        if decision.decision is HumanDecisionType.REJECT:
            return self.transition(run, RunStatus.FAILED, error=decision.reason or "human rejected")
        return self.transition(run, RunStatus.CANCELLED)

    def append_event(self, event: PhaseEvent) -> None:
        with self.workspace.connect() as connection:
            self._insert_phase_event(connection, event)

    @staticmethod
    def _upsert_checkpoint(
        connection: sqlite3.Connection,
        checkpoint: WorkflowCheckpoint,
    ) -> None:
        connection.execute(
            """INSERT OR REPLACE INTO workflow_checkpoints
            (run_id, workflow, next_step_index, created_at, payload)
            VALUES (?, ?, ?, ?, ?)""",
            (
                str(checkpoint.run_id),
                checkpoint.workflow,
                checkpoint.next_step_index,
                checkpoint.created_at.isoformat(),
                checkpoint.model_dump_json(),
            ),
        )

    @staticmethod
    def _insert_phase_event(connection: sqlite3.Connection, event: PhaseEvent) -> None:
        connection.execute(
            """INSERT INTO phase_events
            (id, run_id, workflow, phase, status, occurrence_id, round,
             parent_run_id, parent_event_id, entity_type, entity_id,
             input_artifact_ids, output_artifact_ids, started_at, completed_at,
             summary, warnings, error_code, error_details, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(event.id),
                str(event.run_id),
                event.workflow,
                event.phase,
                event.status,
                str(event.occurrence_id),
                event.round,
                str(event.parent_run_id) if event.parent_run_id else None,
                str(event.parent_event_id) if event.parent_event_id else None,
                event.entity_type,
                event.entity_id,
                json.dumps([str(value) for value in event.input_artifact_ids]),
                json.dumps([str(value) for value in event.output_artifact_ids]),
                event.started_at.isoformat(),
                event.completed_at.isoformat() if event.completed_at else None,
                event.summary,
                json.dumps(event.warnings, ensure_ascii=False),
                event.error_code,
                json.dumps(event.error_details, ensure_ascii=False),
                event.error,
            ),
        )

    def list_runs(self) -> list[WorkflowRun]:
        with self.workspace.connect() as connection:
            rows = connection.execute("SELECT * FROM runs ORDER BY created_at DESC").fetchall()
        return [_run_from_row(row) for row in rows]

    def get(self, run_id: UUID) -> WorkflowRun | None:
        with self.workspace.connect() as connection:
            row = connection.execute("SELECT * FROM runs WHERE id=?", (str(run_id),)).fetchone()
        return _run_from_row(row) if row else None

    def get_many(self, run_ids: list[UUID]) -> list[WorkflowRun]:
        if not run_ids:
            return []
        placeholders = ",".join("?" for _ in run_ids)
        with self.workspace.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM runs WHERE id IN ({placeholders})",  # noqa: S608
                tuple(str(run_id) for run_id in run_ids),
            ).fetchall()
        parsed = [_run_from_row(row) for row in rows]
        by_id = {run.id: run for run in parsed}
        return [by_id[run_id] for run_id in run_ids if run_id in by_id]

    def events(self, run_id: UUID) -> list[PhaseEvent]:
        with self.workspace.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM phase_events WHERE run_id=? ORDER BY started_at, rowid",
                (str(run_id),),
            ).fetchall()
        return [_event_from_row(row) for row in rows]

    def child_run_ids(self, parent_run_id: UUID) -> list[UUID]:
        with self.workspace.connect() as connection:
            rows = connection.execute(
                """SELECT DISTINCT run_id FROM phase_events
                WHERE parent_run_id=? ORDER BY run_id""",
                (str(parent_run_id),),
            ).fetchall()
        return [UUID(str(row["run_id"])) for row in rows]

    def parent_run_id(self, run_id: UUID) -> UUID | None:
        with self.workspace.connect() as connection:
            row = connection.execute(
                """SELECT parent_run_id FROM phase_events
                WHERE run_id=? AND parent_run_id IS NOT NULL
                ORDER BY started_at, rowid LIMIT 1""",
                (str(run_id),),
            ).fetchone()
        return UUID(str(row["parent_run_id"])) if row is not None else None

    def run_relationships(self) -> tuple[dict[UUID, UUID], dict[UUID, int]]:
        with self.workspace.connect() as connection:
            rows = connection.execute(
                """SELECT run_id, parent_run_id FROM phase_events
                WHERE parent_run_id IS NOT NULL
                GROUP BY run_id, parent_run_id"""
            ).fetchall()
        parent_by_child: dict[UUID, UUID] = {}
        children_by_parent: dict[UUID, set[UUID]] = {}
        for row in rows:
            child_id = UUID(str(row["run_id"]))
            parent_id = UUID(str(row["parent_run_id"]))
            parent_by_child.setdefault(child_id, parent_id)
            children_by_parent.setdefault(parent_id, set()).add(child_id)
        return parent_by_child, {
            parent_id: len(child_ids) for parent_id, child_ids in children_by_parent.items()
        }

    def _run_has_retryable_failure(self, run_id: UUID) -> bool:
        failed_events = [
            event for event in self.events(run_id) if event.status is PhaseStatus.FAILED
        ]
        if not failed_events:
            return False
        failed = failed_events[-1]
        return is_retryable_failure(failed.error_code, failed.error)

    def descendant_run_ids(self, root_run_id: UUID) -> list[UUID]:
        discovered: list[UUID] = []
        seen = {root_run_id}
        frontier = [root_run_id]
        with self.workspace.connect() as connection:
            while frontier:
                placeholders = ",".join("?" for _ in frontier)
                rows = connection.execute(
                    f"""SELECT DISTINCT run_id FROM phase_events
                    WHERE parent_run_id IN ({placeholders}) ORDER BY run_id""",  # noqa: S608
                    tuple(str(run_id) for run_id in frontier),
                ).fetchall()
                next_frontier: list[UUID] = []
                for row in rows:
                    run_id = UUID(str(row["run_id"]))
                    if run_id in seen:
                        continue
                    seen.add(run_id)
                    discovered.append(run_id)
                    next_frontier.append(run_id)
                frontier = next_frontier
        return discovered

    def model_calls(self, run_ids: list[UUID]) -> list[ModelCall]:
        if not run_ids:
            return []
        placeholders = ",".join("?" for _ in run_ids)
        with self.workspace.connect() as connection:
            rows = connection.execute(
                f"""SELECT payload FROM model_calls
                WHERE run_id IN ({placeholders}) ORDER BY started_at, id""",  # noqa: S608
                tuple(str(run_id) for run_id in run_ids),
            ).fetchall()
        return [ModelCall.model_validate_json(row["payload"]) for row in rows]

    def latest_human_decision(self, run_id: UUID) -> HumanDecision | None:
        with self.workspace.connect() as connection:
            row = connection.execute(
                """SELECT payload FROM human_decisions
                WHERE run_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1""",
                (str(run_id),),
            ).fetchone()
        return HumanDecision.model_validate_json(row["payload"]) if row else None


class MaterialStore:
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def create(self, material: Material) -> Material:
        with self.workspace.connect() as connection:
            connection.execute(
                """INSERT INTO materials
                (id, course_id, kind, original_name, sha256, mime_type, size_bytes,
                 status, created_at, updated_at, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(material.id),
                    material.course_id,
                    material.kind,
                    material.original_name,
                    material.sha256,
                    material.mime_type,
                    material.size_bytes,
                    material.status,
                    material.created_at.isoformat(),
                    material.updated_at.isoformat(),
                    material.model_dump_json(),
                ),
            )
        return material

    def save(self, material: Material) -> Material:
        material.updated_at = now_utc()
        with self.workspace.connect() as connection:
            connection.execute(
                "UPDATE materials SET status=?, updated_at=?, payload=? WHERE id=?",
                (
                    material.status,
                    material.updated_at.isoformat(),
                    material.model_dump_json(),
                    str(material.id),
                ),
            )
        return material

    def transition(
        self,
        material: Material,
        status: MaterialStatus,
        *,
        parser: str | None = None,
        parsed_document_id: str | None = None,
    ) -> Material:
        material.status = status
        if parser is not None:
            material.parser = parser
        if parsed_document_id is not None:
            material.parsed_document_id = parsed_document_id
        return self.save(material)

    def get(self, material_id: UUID) -> Material | None:
        with self.workspace.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM materials WHERE id=?", (str(material_id),)
            ).fetchone()
        return Material.model_validate_json(row["payload"]) if row else None

    def list(self, course_id: str | None = None) -> list[Material]:
        with self.workspace.connect() as connection:
            if course_id is None:
                rows = connection.execute(
                    "SELECT payload FROM materials ORDER BY created_at DESC"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT payload FROM materials WHERE course_id=? ORDER BY created_at DESC",
                    (course_id,),
                ).fetchall()
        return [Material.model_validate_json(row["payload"]) for row in rows]


class LocalKnowledgeBackend:
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def save_document(self, course_id: str, document: ParsedDocument, kind: str) -> None:
        payload = document.model_dump_json()
        with self.workspace.connect() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO documents
                (id, course_id, kind, title, source_path, payload)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (document.id, course_id, kind, document.title, document.source_path, payload),
            )

    def upsert_points(self, points: list[KnowledgePoint]) -> None:
        with self.workspace.connect() as connection:
            for point in points:
                existing = connection.execute(
                    "SELECT payload FROM knowledge_points WHERE course_id=? AND slug=?",
                    (point.course_id, point.slug),
                ).fetchone()
                merged = (
                    _merge_point(KnowledgePoint.model_validate_json(existing["payload"]), point)
                    if existing
                    else point
                )
                connection.execute(
                    """INSERT OR REPLACE INTO knowledge_points
                (id, course_id, slug, name, parent_id, payload) VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        merged.id,
                        merged.course_id,
                        merged.slug,
                        merged.name,
                        merged.parent_id,
                        merged.model_dump_json(),
                    ),
                )

    def upsert_relations(self, relations: list[KnowledgeRelation]) -> None:
        with self.workspace.connect() as connection:
            connection.executemany(
                """INSERT OR REPLACE INTO knowledge_relations
                (source_id, target_id, kind, payload) VALUES (?, ?, ?, ?)""",
                [(r.source_id, r.target_id, r.kind, r.model_dump_json()) for r in relations],
            )

    def list_points(self, course_id: str) -> list[KnowledgePoint]:
        with self.workspace.connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM knowledge_points WHERE course_id=? ORDER BY slug",
                (course_id,),
            ).fetchall()
        return [KnowledgePoint.model_validate_json(row["payload"]) for row in rows]

    def get_point(self, course_id: str, slug: str) -> KnowledgePoint | None:
        with self.workspace.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM knowledge_points WHERE course_id=? AND slug=?",
                (course_id, slug),
            ).fetchone()
        return KnowledgePoint.model_validate_json(row["payload"]) if row else None

    def search(self, course_id: str, query: str, limit: int = 10) -> list[RetrievalHit]:
        terms = _terms(query)
        hits: list[RetrievalHit] = []
        for point in self.list_points(course_id):
            searchable = " ".join([point.name, point.slug, point.description, *point.tags]).lower()
            matched = [term for term in terms if term in searchable]
            if not matched:
                continue
            exact_bonus = 2.0 if query.lower() in searchable else 0.0
            score = exact_bonus + len(matched) / max(len(terms), 1)
            hits.append(
                RetrievalHit(
                    point=point,
                    score=score,
                    reasons=[f"matched:{term}" for term in matched],
                )
            )
        return sorted(hits, key=lambda hit: (-hit.score, hit.point.slug))[:limit]

    def expand(self, course_id: str, slugs: list[str], depth: int = 1) -> list[KnowledgePoint]:
        points = {point.id: point for point in self.list_points(course_id)}
        selected = {point.id for point in points.values() if point.slug in slugs}
        frontier = set(selected)
        with self.workspace.connect() as connection:
            rows = connection.execute("SELECT payload FROM knowledge_relations").fetchall()
        relations = [KnowledgeRelation.model_validate_json(row["payload"]) for row in rows]
        for _ in range(depth):
            next_frontier: set[str] = set()
            for relation in relations:
                if relation.source_id in frontier:
                    next_frontier.add(relation.target_id)
                if relation.target_id in frontier:
                    next_frontier.add(relation.source_id)
            next_frontier &= points.keys()
            next_frontier -= selected
            selected |= next_frontier
            frontier = next_frontier
        return sorted((points[point_id] for point_id in selected), key=lambda point: point.slug)

    def save_model_call(self, call: ModelCall) -> None:
        with self.workspace.connect() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO model_calls
                (id, run_id, role, model, prompt_version, status, started_at, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(call.id),
                    str(call.run_id) if call.run_id else None,
                    call.role,
                    call.model,
                    call.prompt_version,
                    call.status,
                    call.started_at.isoformat(),
                    call.model_dump_json(),
                ),
            )


class ArtifactStore:
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def write_json(
        self,
        run_id: UUID,
        logical_name: str,
        payload: object,
        *,
        created_by_phase: str | None = None,
    ) -> ArtifactRef:
        serialized = json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n"
        return self.write_bytes(
            run_id,
            logical_name,
            serialized.encode("utf-8"),
            media_type="application/json",
            created_by_phase=created_by_phase,
        )

    def write_bytes(
        self,
        run_id: UUID,
        logical_name: str,
        content: bytes,
        *,
        media_type: str,
        created_by_phase: str | None = None,
    ) -> ArtifactRef:
        run_dir = self.workspace.artifacts / str(run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        destination: Path | None = None
        temporary_name: str | None = None
        published = False
        with self.workspace.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                version = self._next_version(connection, run_id, logical_name)
                destination = run_dir / _versioned_name(logical_name, version)
                descriptor, temporary_name = tempfile.mkstemp(prefix=".artifact-", dir=run_dir)
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary_name, destination)
                temporary_name = None
                published = True
                artifact = ArtifactRef(
                    run_id=run_id,
                    logical_name=logical_name,
                    version=version,
                    path=destination.relative_to(self.workspace.root),
                    media_type=media_type,
                    sha256=hashlib.sha256(content).hexdigest(),
                    size_bytes=len(content),
                    created_by_phase=created_by_phase,
                )
                self._insert_artifact(connection, artifact)
                connection.commit()
                return artifact
            except Exception:
                connection.rollback()
                if temporary_name is not None and os.path.exists(temporary_name):
                    os.unlink(temporary_name)
                if published and destination is not None and destination.exists():
                    destination.unlink()
                raise

    def write_editable_json(
        self,
        parent_run_id: UUID,
        relative_name: str,
        payload: object,
    ) -> Path:
        root = (self.workspace.root / "editable" / str(parent_run_id)).resolve()
        destination = (root / relative_name).resolve()
        if root != destination and root not in destination.parents:
            raise ValueError("editable path escapes the parent run directory")
        destination.parent.mkdir(parents=True, exist_ok=True)
        serialized = json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n"
        descriptor, temporary_name = tempfile.mkstemp(prefix=".editable-", dir=destination.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(serialized)
                stream.flush()
                os.fsync(stream.fileno())
            _replace_editable_file(Path(temporary_name), destination)
        except Exception:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)
            raise
        return destination.relative_to(self.workspace.root)

    def list(self, run_id: UUID) -> list[ArtifactRef]:
        with self.workspace.connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM artifacts WHERE run_id=? ORDER BY logical_name, version",
                (str(run_id),),
            ).fetchall()
        return [ArtifactRef.model_validate_json(row["payload"]) for row in rows]

    def get(self, artifact_id: UUID) -> ArtifactRef | None:
        with self.workspace.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM artifacts WHERE id=?", (str(artifact_id),)
            ).fetchone()
        return ArtifactRef.model_validate_json(row["payload"]) if row else None

    def latest(self, run_id: UUID, logical_name: str) -> ArtifactRef | None:
        with self.workspace.connect() as connection:
            row = connection.execute(
                """SELECT payload FROM artifacts
                WHERE run_id=? AND logical_name=? ORDER BY version DESC LIMIT 1""",
                (str(run_id), logical_name),
            ).fetchone()
        return ArtifactRef.model_validate_json(row["payload"]) if row else None

    def read_bytes(self, artifact_id: UUID) -> bytes:
        artifact = self.get(artifact_id)
        if artifact is None:
            raise KeyError(f"artifact not found: {artifact_id}")
        path = self.workspace.root / artifact.path
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != artifact.sha256:
            raise ValueError(f"artifact integrity check failed: {artifact_id}")
        return content

    def read_json(self, artifact_id: UUID) -> object:
        return json.loads(self.read_bytes(artifact_id))

    def read_editable_json(self, parent_run_id: UUID, relative_name: str) -> object:
        root = (self.workspace.root / "editable" / str(parent_run_id)).resolve()
        source = (root / relative_name).resolve()
        if root != source and root not in source.parents:
            raise ValueError("editable path escapes the parent run directory")
        return json.loads(source.read_text(encoding="utf-8"))

    def verify(self, artifact_id: UUID) -> bool:
        try:
            self.read_bytes(artifact_id)
        except (FileNotFoundError, KeyError, ValueError):
            return False
        return True

    def reconcile(self) -> builtins.list[Path]:
        removed: builtins.list[Path] = []
        with self.workspace.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute("SELECT path FROM artifacts").fetchall()
            referenced = {(self.workspace.root / Path(str(row["path"]))).resolve() for row in rows}
            for path in self.workspace.artifacts.rglob("*"):
                if not path.is_file():
                    continue
                recognized = path.name.startswith(".artifact-") or re.fullmatch(
                    r".+\.v[1-9]\d*(?:\.[^.]+)*", path.name
                )
                if not recognized or path.resolve() in referenced:
                    continue
                path.unlink()
                removed.append(path.relative_to(self.workspace.root))
            connection.commit()
        return sorted(removed, key=str)

    @staticmethod
    def _next_version(
        connection: sqlite3.Connection,
        run_id: UUID,
        logical_name: str,
    ) -> int:
        row = connection.execute(
            "SELECT COALESCE(MAX(version), 0) AS version FROM artifacts "
            "WHERE run_id=? AND logical_name=?",
            (str(run_id), logical_name),
        ).fetchone()
        return int(row["version"]) + 1

    @staticmethod
    def _insert_artifact(
        connection: sqlite3.Connection,
        artifact: ArtifactRef,
    ) -> None:
        connection.execute(
            """INSERT INTO artifacts
            (id, run_id, logical_name, version, path, media_type, sha256,
             size_bytes, created_by_phase, created_at, payload)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(artifact.id),
                str(artifact.run_id),
                artifact.logical_name,
                artifact.version,
                str(artifact.path),
                artifact.media_type,
                artifact.sha256,
                artifact.size_bytes,
                artifact.created_by_phase,
                artifact.created_at.isoformat(),
                artifact.model_dump_json(),
            ),
        )

    @staticmethod
    def sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()


def _is_subject_synthesis_validation_recoverable(
    failed: PhaseEvent,
    checkpoint: WorkflowCheckpoint,
) -> bool:
    if (
        checkpoint.workflow != "exam_agent_generation"
        or failed.phase != "SUBJECT_SYNTHESIZING"
        or failed.error_code != "ValueError"
    ):
        return False
    required_bindings = {
        "request",
        "subject_research_manifest",
        "subject_research_reports",
    }
    if not required_bindings <= checkpoint.artifact_bindings.keys():
        return False
    error = failed.error or ""
    return error.startswith(
        (
            "subject research synthesis is missing field traces:",
            "field trace ",
            "unclaimed field trace ",
        )
    )


def _is_editable_projection_replace_recoverable(
    failed: PhaseEvent,
    checkpoint: WorkflowCheckpoint,
) -> bool:
    if (
        checkpoint.workflow != "exam_question_generation"
        or failed.phase != "REVIEWS_GENERATING"
        or failed.error_code != "PermissionError"
    ):
        return False
    required_bindings = {"request", "plan", "question_state", "question", "solution", "rubric"}
    if not required_bindings <= checkpoint.artifact_bindings.keys():
        return False
    error = failed.error or ""
    return ".editable-" in error and "review-runs.json" in error


def _versioned_name(logical_name: str, version: int) -> str:
    path = Path(logical_name)
    suffix = "".join(path.suffixes)
    stem = path.name[: -len(suffix)] if suffix else path.name
    return f"{stem}.v{version}{suffix}"


def _replace_editable_file(source: Path, destination: Path) -> None:
    for attempt in range(_EDITABLE_REPLACE_ATTEMPTS):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt == _EDITABLE_REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(_EDITABLE_REPLACE_BASE_DELAY_SECONDS * (2**attempt))


def _run_values(
    run: WorkflowRun,
) -> tuple[str, str, str, str | None, str, str, str | None, str | None, int | None]:
    return (
        str(run.id),
        run.workflow,
        run.status,
        run.current_phase,
        run.created_at.isoformat(),
        run.updated_at.isoformat(),
        run.error,
        run.runner_host,
        run.runner_pid,
    )


def _run_from_row(row: sqlite3.Row) -> WorkflowRun:
    return WorkflowRun.model_validate(dict(row))


def _event_from_row(row: sqlite3.Row) -> PhaseEvent:
    payload = dict(row)
    payload["input_artifact_ids"] = json.loads(payload.get("input_artifact_ids") or "[]")
    payload["output_artifact_ids"] = json.loads(payload.get("output_artifact_ids") or "[]")
    payload["warnings"] = json.loads(payload.get("warnings") or "[]")
    payload["error_details"] = json.loads(payload.get("error_details") or "{}")
    return PhaseEvent.model_validate(payload)


def _migrate_phase_events(connection: sqlite3.Connection) -> None:
    existing = {
        row["name"] for row in connection.execute("PRAGMA table_info(phase_events)").fetchall()
    }
    columns = {
        "workflow": "TEXT NOT NULL DEFAULT ''",
        "parent_run_id": "TEXT",
        "parent_event_id": "TEXT",
        "input_artifact_ids": "TEXT NOT NULL DEFAULT '[]'",
        "output_artifact_ids": "TEXT NOT NULL DEFAULT '[]'",
        "warnings": "TEXT NOT NULL DEFAULT '[]'",
        "error_code": "TEXT",
        "error_details": "TEXT NOT NULL DEFAULT '{}'",
    }
    for name, definition in columns.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE phase_events ADD COLUMN {name} {definition}")


def _migrate_runs(connection: sqlite3.Connection) -> None:
    existing = {row["name"] for row in connection.execute("PRAGMA table_info(runs)").fetchall()}
    columns = {"runner_host": "TEXT", "runner_pid": "INTEGER"}
    for name, definition in columns.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE runs ADD COLUMN {name} {definition}")


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        return _windows_pid_exists(pid)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _windows_pid_exists(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    win_dll = getattr(ctypes, "WinDLL", None)
    if win_dll is None:
        return False
    kernel32 = win_dll("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return False
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def _merge_point(existing: KnowledgePoint, incoming: KnowledgePoint) -> KnowledgePoint:
    evidence = {f"{item.document_id}:{item.block_id}": item for item in existing.evidence}
    evidence.update({f"{item.document_id}:{item.block_id}": item for item in incoming.evidence})
    return existing.model_copy(
        update={
            "name": incoming.name or existing.name,
            "description": incoming.description or existing.description,
            "parent_id": incoming.parent_id or existing.parent_id,
            "tags": sorted(set(existing.tags) | set(incoming.tags)),
            "evidence": list(evidence.values()),
            "confidence": max(existing.confidence, incoming.confidence),
        }
    )


def _terms(query: str) -> list[str]:
    latin = re.findall(r"[a-zA-Z0-9_]+", query.lower())
    chinese = re.findall(r"[\u4e00-\u9fff]+", query)
    terms = latin + chinese
    return terms or [query.lower()]


SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    workflow TEXT NOT NULL,
    status TEXT NOT NULL,
    current_phase TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    error TEXT,
    runner_host TEXT,
    runner_pid INTEGER
);
CREATE TABLE IF NOT EXISTS phase_events (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id),
    workflow TEXT NOT NULL DEFAULT '',
    phase TEXT NOT NULL,
    status TEXT NOT NULL,
    occurrence_id TEXT NOT NULL,
    round INTEGER NOT NULL,
    parent_run_id TEXT,
    parent_event_id TEXT,
    entity_type TEXT,
    entity_id TEXT,
    input_artifact_ids TEXT NOT NULL DEFAULT '[]',
    output_artifact_ids TEXT NOT NULL DEFAULT '[]',
    started_at TEXT NOT NULL,
    completed_at TEXT,
    summary TEXT,
    warnings TEXT NOT NULL DEFAULT '[]',
    error_code TEXT,
    error_details TEXT NOT NULL DEFAULT '{}',
    error TEXT
);
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    course_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    source_path TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS materials (
    id TEXT PRIMARY KEY,
    course_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    original_name TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS materials_course_id_idx ON materials(course_id);
CREATE INDEX IF NOT EXISTS materials_sha256_idx ON materials(sha256);
CREATE TABLE IF NOT EXISTS knowledge_points (
    id TEXT PRIMARY KEY,
    course_id TEXT NOT NULL,
    slug TEXT NOT NULL,
    name TEXT NOT NULL,
    parent_id TEXT,
    payload TEXT NOT NULL,
    UNIQUE(course_id, slug)
);
CREATE TABLE IF NOT EXISTS knowledge_relations (
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY(source_id, target_id, kind)
);
CREATE TABLE IF NOT EXISTS model_calls (
    id TEXT PRIMARY KEY,
    run_id TEXT,
    role TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS model_calls_run_id_idx ON model_calls(run_id, started_at);
CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id),
    logical_name TEXT NOT NULL,
    version INTEGER NOT NULL,
    path TEXT NOT NULL,
    media_type TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    created_by_phase TEXT,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    UNIQUE(run_id, logical_name, version)
);
CREATE TABLE IF NOT EXISTS workflow_checkpoints (
    run_id TEXT PRIMARY KEY REFERENCES runs(id),
    workflow TEXT NOT NULL,
    next_step_index INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS human_review_requests (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id),
    phase TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS human_decisions (
    id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL REFERENCES human_review_requests(id),
    run_id TEXT NOT NULL REFERENCES runs(id),
    decision TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
"""
