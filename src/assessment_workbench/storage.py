from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
from pathlib import Path
from uuid import UUID

from assessment_workbench.domain import (
    ArtifactRef,
    KnowledgePoint,
    KnowledgeRelation,
    ModelCall,
    ParsedDocument,
    PhaseEvent,
    RetrievalHit,
    RunStatus,
    WorkflowCheckpoint,
    WorkflowRun,
    now_utc,
    validate_run_transition,
)


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

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
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
                "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?)",
                _run_values(run),
            )
        return run

    def save(self, run: WorkflowRun) -> None:
        run.updated_at = now_utc()
        with self.workspace.connect() as connection:
            connection.execute(
                """UPDATE runs SET status=?, current_phase=?, updated_at=?, error=? WHERE id=?""",
                (run.status, run.current_phase, run.updated_at.isoformat(), run.error, str(run.id)),
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

    def save_checkpoint(self, checkpoint: WorkflowCheckpoint) -> None:
        with self.workspace.connect() as connection:
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

    def get_checkpoint(self, run_id: UUID) -> WorkflowCheckpoint | None:
        with self.workspace.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM workflow_checkpoints WHERE run_id=?", (str(run_id),)
            ).fetchone()
        return WorkflowCheckpoint.model_validate_json(row["payload"]) if row else None

    def clear_checkpoint(self, run_id: UUID) -> None:
        with self.workspace.connect() as connection:
            connection.execute("DELETE FROM workflow_checkpoints WHERE run_id=?", (str(run_id),))

    def append_event(self, event: PhaseEvent) -> None:
        with self.workspace.connect() as connection:
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

    def events(self, run_id: UUID) -> list[PhaseEvent]:
        with self.workspace.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM phase_events WHERE run_id=? ORDER BY started_at, rowid",
                (str(run_id),),
            ).fetchall()
        return [_event_from_row(row) for row in rows]


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
        version = self._next_version(run_id, logical_name)
        destination = run_dir / _versioned_name(logical_name, version)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".artifact-", dir=run_dir)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, destination)
        except Exception:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)
            raise
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
        self._save(artifact)
        return artifact

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

    def read_bytes(self, artifact_id: UUID) -> bytes:
        artifact = self.get(artifact_id)
        if artifact is None:
            raise KeyError(f"artifact not found: {artifact_id}")
        path = self.workspace.root / artifact.path
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != artifact.sha256:
            raise ValueError(f"artifact integrity check failed: {artifact_id}")
        return content

    def verify(self, artifact_id: UUID) -> bool:
        try:
            self.read_bytes(artifact_id)
        except (FileNotFoundError, KeyError, ValueError):
            return False
        return True

    def _next_version(self, run_id: UUID, logical_name: str) -> int:
        with self.workspace.connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) AS version FROM artifacts "
                "WHERE run_id=? AND logical_name=?",
                (str(run_id), logical_name),
            ).fetchone()
        return int(row["version"]) + 1

    def _save(self, artifact: ArtifactRef) -> None:
        with self.workspace.connect() as connection:
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


def _versioned_name(logical_name: str, version: int) -> str:
    path = Path(logical_name)
    suffix = "".join(path.suffixes)
    stem = path.name[: -len(suffix)] if suffix else path.name
    return f"{stem}.v{version}{suffix}"


def _run_values(run: WorkflowRun) -> tuple[str, str, str, str | None, str, str, str | None]:
    return (
        str(run.id),
        run.workflow,
        run.status,
        run.current_phase,
        run.created_at.isoformat(),
        run.updated_at.isoformat(),
        run.error,
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
    error TEXT
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
"""
