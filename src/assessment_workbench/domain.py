from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, model_validator


def now_utc() -> datetime:
    return datetime.now(UTC)


class MaterialKind(StrEnum):
    LECTURE = "lecture"
    TEXTBOOK = "textbook"
    PAST_EXAM = "past_exam"
    PAST_SOLUTION = "past_solution"
    EXERCISE_SET = "exercise_set"
    SYLLABUS = "syllabus"
    OTHER = "other"


class BlockKind(StrEnum):
    TEXT = "text"
    EQUATION = "equation"
    TABLE = "table"
    IMAGE = "image"
    HEADING = "heading"


class SourceReference(BaseModel):
    document_id: str
    block_id: str
    page: int = Field(ge=1)
    excerpt: str = ""


class ContentBlock(BaseModel):
    id: str
    kind: BlockKind
    page: int = Field(ge=1)
    content: str
    heading_path: list[str] = Field(default_factory=list)
    metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class ParsedDocument(BaseModel):
    id: str
    source_path: str
    title: str
    blocks: list[ContentBlock]
    parser: str
    parser_version: str | None = None
    metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class KnowledgePoint(BaseModel):
    id: str
    course_id: str
    name: str
    slug: str
    description: str = ""
    parent_id: str | None = None
    tags: list[str] = Field(default_factory=list)
    evidence: list[SourceReference] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0, le=1)


class KnowledgeNodeKind(StrEnum):
    MODULE = "module"
    KNOWLEDGE_POINT = "knowledge_point"
    CONCEPT = "concept"
    DEFINITION = "definition"
    THEOREM = "theorem"
    LAW = "law"
    FORMULA = "formula"
    EXPERIMENT = "experiment"
    PROBLEM_PATTERN = "problem_pattern"


class KnowledgeNode(BaseModel):
    key: str
    kind: KnowledgeNodeKind
    name: str
    description: str = ""
    aliases: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    source_block_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0, le=1)


class ExtractedRelation(BaseModel):
    source_key: str
    target_key: str
    kind: "RelationKind"
    source_block_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0, le=1)


class KnowledgeExtraction(BaseModel):
    nodes: list[KnowledgeNode] = Field(default_factory=list)
    relations: list[ExtractedRelation] = Field(default_factory=list)


class RelationKind(StrEnum):
    CONTAINS = "contains"
    PREREQUISITE_OF = "prerequisite_of"
    DERIVES_FROM = "derives_from"
    DEPENDS_ON = "depends_on"
    APPLIES_TO = "applies_to"
    ASSESSED_BY = "assessed_by"
    RELATED_TO = "related_to"


class KnowledgeRelation(BaseModel):
    source_id: str
    target_id: str
    kind: RelationKind
    evidence: list[SourceReference] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0, le=1)

    @model_validator(mode="after")
    def reject_self_relation(self) -> "KnowledgeRelation":
        if self.source_id == self.target_id:
            raise ValueError("knowledge relation cannot point to itself")
        return self


class RetrievalHit(BaseModel):
    point: KnowledgePoint
    score: float = Field(ge=0)
    reasons: list[str] = Field(default_factory=list)


class QuestionType(StrEnum):
    MULTIPLE_CHOICE = "multiple_choice"
    FILL_BLANK = "fill_blank"
    CALCULATION = "calculation"
    PROOF = "proof"
    EXPERIMENT = "experiment"


class DifficultyProfile(BaseModel):
    conceptual: int = Field(default=5, ge=1, le=10)
    reasoning: int = Field(default=5, ge=1, le=10)
    calculation: int = Field(default=5, ge=1, le=10)
    overall: int = Field(default=5, ge=1, le=10)


class QuestionSpec(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    course_id: str
    question_type: QuestionType
    topic_slugs: list[str] = Field(min_length=1)
    score: int = Field(default=10, ge=1)
    difficulty: DifficultyProfile = Field(default_factory=DifficultyProfile)
    learning_objectives: list[str] = Field(default_factory=list)
    required_context: list[SourceReference] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)


class ModelUsage(BaseModel):
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


class ModelCall(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    run_id: UUID | None = None
    role: str
    model: str
    prompt_version: str
    request_sha256: str
    response_sha256: str | None = None
    status: str
    started_at: datetime = Field(default_factory=now_utc)
    completed_at: datetime | None = None
    usage: ModelUsage = Field(default_factory=ModelUsage)
    error: str | None = None


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_HUMAN = "waiting_human"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class PhaseStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class WorkflowRun(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    workflow: str
    status: RunStatus = RunStatus.QUEUED
    current_phase: str | None = None
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)
    error: str | None = None


class PhaseEvent(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    workflow: str
    phase: str
    status: PhaseStatus
    occurrence_id: UUID
    round: int = Field(default=1, ge=1)
    parent_run_id: UUID | None = None
    parent_event_id: UUID | None = None
    entity_type: str | None = None
    entity_id: str | None = None
    input_artifact_ids: list[UUID] = Field(default_factory=list)
    output_artifact_ids: list[UUID] = Field(default_factory=list)
    started_at: datetime
    completed_at: datetime | None = None
    summary: str | None = None
    warnings: list[str] = Field(default_factory=list)
    error_code: str | None = None
    error_details: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    error: str | None = None


class ArtifactRef(BaseModel):
    name: str
    path: Path
    media_type: str
    sha256: str
