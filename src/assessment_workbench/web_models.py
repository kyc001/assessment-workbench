from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, Field, StringConstraints

from assessment_workbench.domain import (
    ArtifactRef,
    HumanReviewRequest,
    PhaseEvent,
    WorkflowRun,
)


class ApiErrorResponse(BaseModel):
    code: str
    detail: str
    fields: dict[str, str] = Field(default_factory=dict)


class WorkspaceInfo(BaseModel):
    root: str
    database: str
    run_count: int


class RunSummary(BaseModel):
    run: WorkflowRun
    parent_run_id: UUID | None = None
    child_count: int = 0


class RunDetail(BaseModel):
    run: WorkflowRun
    parent_run_id: UUID | None = None
    events: list[PhaseEvent]
    children: list[WorkflowRun]
    human_review: HumanReviewRequest | None = None
    artifacts: list[ArtifactRef]


class RunSnapshot(BaseModel):
    detail: RunDetail
    research: list[dict[str, Any]] = Field(default_factory=list)
    questions: list[dict[str, Any]] = Field(default_factory=list)
    documents: list[dict[str, Any]] = Field(default_factory=list)


NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Sha256Text = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class ExamCreateRequest(BaseModel):
    subject: NonEmptyText
    target_level: NonEmptyText
    requirements: NonEmptyText
    source_context: str = ""
    human_gates: bool = True
    compile_pdf: bool = True


class RunActionRequest(BaseModel):
    actor: str = "gui-user"
    reason: str = ""


class QuestionRerunRequest(BaseModel):
    feedback: list[str] = Field(default_factory=list)


class QuestionPublishRequest(BaseModel):
    child_run_id: UUID


class QuestionEditRequest(BaseModel):
    expected_sha256: Sha256Text
    bundle: dict[str, Any]


class EditableQuestion(BaseModel):
    question_number: int
    sha256: str
    bundle: dict[str, Any]


class BackgroundRunResponse(BaseModel):
    run: WorkflowRun


class ArtifactContent(BaseModel):
    artifact: ArtifactRef
    kind: str
    content: Any = None
