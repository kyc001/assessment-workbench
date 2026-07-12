from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from uuid import UUID

from assessment_workbench.domain import (
    KnowledgePoint,
    KnowledgeRelation,
    ModelCall,
    ParsedDocument,
    PhaseEvent,
    RetrievalHit,
    WorkflowRun,
    now_utc,
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

    def write_json(self, run_id: UUID, name: str, payload: object) -> Path:
        run_dir = self.workspace.artifacts / str(run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        destination = run_dir / name
        serialized = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        destination.write_text(serialized + "\n", encoding="utf-8")
        return destination

    @staticmethod
    def sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()


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
"""
