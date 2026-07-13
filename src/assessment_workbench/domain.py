import re
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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


class MaterialStatus(StrEnum):
    REGISTERED = "registered"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"
    DELETED = "deleted"


class Material(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    course_id: str
    kind: MaterialKind
    source_path: Path
    original_name: str
    sha256: str = Field(min_length=64, max_length=64)
    mime_type: str
    size_bytes: int = Field(ge=0)
    semester: str | None = None
    year: int | None = Field(default=None, ge=1900, le=9999)
    language: str = "zh-CN"
    status: MaterialStatus = MaterialStatus.REGISTERED
    parser: str | None = None
    parsed_document_id: str | None = None
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)


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
    MULTIPLE_SELECT = "multiple_select"
    FILL_BLANK = "fill_blank"
    CONSTRUCTED_RESPONSE = "constructed_response"
    CALCULATION = "calculation"
    PROOF = "proof"
    EXPERIMENT = "experiment"


class ExamContentKind(StrEnum):
    TEXT = "text"
    INLINE_MATH = "inline_math"
    DISPLAY_MATH = "display_math"


class ExamContentBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: ExamContentKind
    content: str = Field(min_length=1)

    @model_validator(mode="after")
    def normalize_math_content(self) -> "ExamContentBlock":
        if self.kind is ExamContentKind.TEXT:
            if any(symbol in self.content for symbol in ("∠", "⊥", "∥")):
                raise ValueError(
                    "text content must place angle and relation symbols in math blocks"
                )
            return self
        content = self.content.strip()
        wrappers = ((r"\(", r"\)"), (r"\[", r"\]"))
        for opening, closing in wrappers:
            if content.startswith(opening) and content.endswith(closing):
                content = content[len(opening) : -len(closing)].strip()
                break
        if content and set(content) == {"_"}:
            content = r"\underline{\hspace{2cm}}"
        content = content.translate(
            {
                ord("，"): ",",
                ord("。"): ".",
                ord("；"): ";",
                ord("："): ":",
            }
        )
        forbidden = ("$", r"\(", r"\)", r"\[", r"\]")
        if any(token in content for token in forbidden):
            raise ValueError("math content must not include LaTeX math delimiters")
        self.content = content
        return self


def _normalize_content_blocks(value: object) -> object:
    if isinstance(value, str):
        return [{"kind": ExamContentKind.TEXT, "content": value}]
    if isinstance(value, list) and value and all(isinstance(item, str) for item in value):
        return [{"kind": ExamContentKind.TEXT, "content": item} for item in value]
    return value


def _normalize_content_options(value: object) -> object:
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return [[{"kind": ExamContentKind.TEXT, "content": item}] for item in value]
    return value


class QuestionPart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    label: str
    prompt: list[ExamContentBlock] = Field(min_length=1)
    score: int = Field(ge=1)

    @field_validator("prompt", mode="before")
    @classmethod
    def normalize_prompt(cls, value: object) -> object:
        return _normalize_content_blocks(value)


class SubjectProfile(BaseModel):
    id: str
    display_name: str
    supported_question_types: list[QuestionType] = Field(min_length=1)
    reviewers: list[str] = Field(min_length=1)
    tools: list[str] = Field(default_factory=list)
    latex_template: str
    difficulty_dimensions: list[str] = Field(min_length=1)
    conventions: list[str] = Field(default_factory=list)
    source_summary: str = ""
    version: str = "1"


class ExamSectionBlueprint(BaseModel):
    id: str
    title: str
    question_type: QuestionType
    count: int = Field(ge=1)
    score_each: int | None = Field(default=None, ge=1)
    question_scores: list[int] = Field(default_factory=list)
    topic_tags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_question_scores(self) -> "ExamSectionBlueprint":
        if self.score_each is None and not self.question_scores:
            raise ValueError("section requires score_each or question_scores")
        if self.score_each is not None and self.question_scores:
            raise ValueError("section cannot define both score_each and question_scores")
        if self.question_scores and len(self.question_scores) != self.count:
            raise ValueError("question_scores length must match section count")
        if any(score < 1 for score in self.question_scores):
            raise ValueError("question_scores must contain only positive values")
        return self

    @property
    def resolved_scores(self) -> list[int]:
        if self.question_scores:
            return list(self.question_scores)
        assert self.score_each is not None
        return [self.score_each] * self.count

    @property
    def total_score(self) -> int:
        return sum(self.resolved_scores)


class CoverageTarget(BaseModel):
    topic_tag: str
    target_score: int = Field(ge=1)


class DifficultyDistribution(BaseModel):
    easy: float = Field(ge=0, le=1)
    medium: float = Field(ge=0, le=1)
    hard: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_total(self) -> "DifficultyDistribution":
        if abs(self.easy + self.medium + self.hard - 1.0) > 1e-6:
            raise ValueError("difficulty distribution must sum to 1")
        return self


class ExamBlueprint(BaseModel):
    id: str
    version: str = "1"
    subject_profile: str
    title: str
    target_level: str
    duration_minutes: int = Field(ge=1)
    total_score: int = Field(ge=1)
    language: str = "zh-CN"
    sections: list[ExamSectionBlueprint] = Field(min_length=1)
    coverage: list[CoverageTarget] = Field(default_factory=list)
    difficulty_distribution: DifficultyDistribution
    constraints: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_scores(self) -> "ExamBlueprint":
        section_score = sum(section.total_score for section in self.sections)
        if section_score != self.total_score:
            raise ValueError(f"section scores total {section_score}, expected {self.total_score}")
        coverage_score = sum(item.target_score for item in self.coverage)
        if self.coverage and coverage_score != self.total_score:
            raise ValueError(f"coverage scores total {coverage_score}, expected {self.total_score}")
        section_ids = [section.id for section in self.sections]
        if len(section_ids) != len(set(section_ids)):
            raise ValueError("exam section ids must be unique")
        return self


class ExamPlanningMode(StrEnum):
    AGENT = "agent"
    CAPABILITY = "capability"
    PRESET = "preset"


class ExamPlanningRecord(BaseModel):
    mode: ExamPlanningMode
    subject_profile_id: str
    subject_profile_version: str
    blueprint_id: str
    blueprint_version: str
    capability_id: str | None = None
    capability_version: str | None = None

    @model_validator(mode="after")
    def validate_capability_pair(self) -> "ExamPlanningRecord":
        if (self.capability_id is None) != (self.capability_version is None):
            raise ValueError("capability_id and capability_version must be provided together")
        return self


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


class GenerationMetadata(BaseModel):
    role: str
    model: str = "fixture"
    prompt_version: str = "fixture-v1"
    source_refs: list[SourceReference] = Field(default_factory=list)
    plan_id: str | None = None


class QuestionVersion(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    question_id: UUID
    version: int = Field(ge=1)
    parent_version_id: UUID | None = None
    number: int = Field(ge=1)
    section_id: str = ""
    section_title: str = ""
    question_type: QuestionType
    topic_tags: list[str] = Field(min_length=1)
    score: int = Field(ge=1)
    statement: list[ExamContentBlock] = Field(min_length=1)
    options: list[list[ExamContentBlock]] = Field(default_factory=list)
    parts: list[QuestionPart] = Field(default_factory=list)
    answer_format: str = "show_work"
    metadata: GenerationMetadata

    @field_validator("statement", mode="before")
    @classmethod
    def normalize_statement(cls, value: object) -> object:
        return _normalize_content_blocks(value)

    @field_validator("options", mode="before")
    @classmethod
    def normalize_options(cls, value: object) -> object:
        return _normalize_content_options(value)

    @model_validator(mode="after")
    def validate_options(self) -> "QuestionVersion":
        choice_types = {QuestionType.MULTIPLE_CHOICE, QuestionType.MULTIPLE_SELECT}
        if self.question_type in choice_types and len(self.options) < 4:
            raise ValueError("choice questions require at least four options")
        if self.question_type not in choice_types and self.options:
            raise ValueError("only choice questions may define options")
        for index, option in enumerate(self.options):
            if not option or option[0].kind is not ExamContentKind.TEXT:
                continue
            label = chr(ord("A") + index)
            option[0].content = re.sub(
                rf"^\s*{label}[.\uff0e\u3001:\uff1a]\s*",
                "",
                option[0].content,
            )
            if not option[0].content:
                option.pop(0)
            if not option:
                raise ValueError("multiple-choice options cannot contain only a label")
        if self.question_type is QuestionType.CONSTRUCTED_RESPONSE:
            if not self.parts:
                raise ValueError("constructed-response questions require explicit parts")
            if sum(part.score for part in self.parts) != self.score:
                raise ValueError("question part scores must sum to the question score")
        elif self.parts:
            raise ValueError("only constructed-response questions may define parts")
        return self


class SolutionStep(BaseModel):
    id: str
    description: list[ExamContentBlock] = Field(min_length=1)
    expression: str | None = None
    conclusion: list[ExamContentBlock] | None = None
    required: bool = True

    @field_validator("description", mode="before")
    @classmethod
    def normalize_description(cls, value: object) -> object:
        return _normalize_content_blocks(value)

    @field_validator("conclusion", mode="before")
    @classmethod
    def normalize_conclusion(cls, value: object) -> object:
        if value is None:
            return None
        return _normalize_content_blocks(value)


class SolutionVersion(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    solution_id: UUID
    question_version_id: UUID
    version: int = Field(ge=1)
    parent_version_id: UUID | None = None
    steps: list[SolutionStep] = Field(min_length=1)
    final_answer: list[ExamContentBlock] = Field(min_length=1)
    alternative_solutions: list[list[SolutionStep]] = Field(default_factory=list)
    verification_notes: list[str] = Field(default_factory=list)
    metadata: GenerationMetadata

    @field_validator("final_answer", mode="before")
    @classmethod
    def normalize_final_answer(cls, value: object) -> object:
        return _normalize_content_blocks(value)


class PartialCreditLevel(BaseModel):
    score: int = Field(ge=0)
    condition: str = Field(min_length=1)


class RubricItem(BaseModel):
    id: str
    description: list[ExamContentBlock] = Field(min_length=1)
    score: int = Field(ge=1)
    depends_on: list[str] = Field(default_factory=list)
    equivalent_expressions: list[str] = Field(default_factory=list)
    partial_credit: list[PartialCreditLevel] = Field(default_factory=list)
    carry_forward: bool = False

    @field_validator("description", mode="before")
    @classmethod
    def normalize_description(cls, value: object) -> object:
        return _normalize_content_blocks(value)

    @model_validator(mode="after")
    def validate_partial_credit(self) -> "RubricItem":
        if any(level.score >= self.score for level in self.partial_credit):
            raise ValueError("partial credit must be less than the rubric item score")
        return self


class RubricVersion(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    rubric_id: UUID
    question_version_id: UUID
    solution_version_id: UUID
    version: int = Field(ge=1)
    parent_version_id: UUID | None = None
    max_score: int = Field(ge=1)
    items: list[RubricItem] = Field(min_length=1)
    alternative_solution_policy: str = "award_equivalent_method_credit"
    metadata: GenerationMetadata

    @model_validator(mode="after")
    def validate_items(self) -> "RubricVersion":
        item_ids = [item.id for item in self.items]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("rubric item ids must be unique")
        if sum(item.score for item in self.items) != self.max_score:
            raise ValueError("rubric item scores must sum to max_score")
        known = set(item_ids)
        for item in self.items:
            if not set(item.depends_on) <= known:
                raise ValueError(f"rubric item {item.id} depends on an unknown item")
            if item.id in item.depends_on:
                raise ValueError(f"rubric item {item.id} cannot depend on itself")
        return self


class ExamQuestionBundle(BaseModel):
    question: QuestionVersion
    solution: SolutionVersion
    rubric: RubricVersion

    @model_validator(mode="after")
    def validate_links(self) -> "ExamQuestionBundle":
        if self.solution.question_version_id != self.question.id:
            raise ValueError("solution does not reference the bundled question version")
        if self.rubric.question_version_id != self.question.id:
            raise ValueError("rubric does not reference the bundled question version")
        if self.rubric.solution_version_id != self.solution.id:
            raise ValueError("rubric does not reference the bundled solution version")
        if self.rubric.max_score != self.question.score:
            raise ValueError("rubric max_score must match the question score")
        return self


class ExamDocument(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    blueprint_id: str
    title: str
    subject_profile: str
    duration_minutes: int = Field(ge=1)
    total_score: int = Field(ge=1)
    language: str = "zh-CN"
    questions: list[ExamQuestionBundle] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_exam(self) -> "ExamDocument":
        numbers = [bundle.question.number for bundle in self.questions]
        if numbers != list(range(1, len(numbers) + 1)):
            raise ValueError("exam question numbers must be consecutive starting at 1")
        score = sum(bundle.question.score for bundle in self.questions)
        if score != self.total_score:
            raise ValueError(f"exam question scores total {score}, expected {self.total_score}")
        return self


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReviewerName(StrEnum):
    MATHEMATICAL = "mathematical"
    SUBJECT = "subject"
    SOLVABILITY = "solvability"
    RUBRIC = "rubric"
    PEDAGOGICAL = "pedagogical"
    STRUCTURE = "structure"


class SubjectProfileCandidate(StrictModel):
    subject_id: str
    display_name: str
    supported_question_types: list[QuestionType] = Field(min_length=1)
    reviewers: list[str] = Field(min_length=1)
    tools: list[str] = Field(default_factory=list)
    difficulty_dimensions: list[str] = Field(min_length=1)
    conventions: list[str] = Field(default_factory=list)
    source_summary: str = Field(min_length=1)


class BlueprintDraft(StrictModel):
    title: str
    target_level: str
    duration_minutes: int = Field(ge=1)
    total_score: int = Field(ge=1)
    language: str = "zh-CN"
    sections: list[ExamSectionBlueprint] = Field(min_length=1)
    coverage: list[CoverageTarget] = Field(default_factory=list)
    difficulty_distribution: DifficultyDistribution
    constraints: list[str] = Field(default_factory=list)


class QuestionDraft(StrictModel):
    statement: list[ExamContentBlock] = Field(min_length=1)
    options: list[list[ExamContentBlock]] = Field(default_factory=list)
    parts: list[QuestionPart] = Field(default_factory=list)
    answer_format: str = "show_work"
    topic_tags: list[str] = Field(min_length=1)

    @field_validator("statement", mode="before")
    @classmethod
    def normalize_statement(cls, value: object) -> object:
        return _normalize_content_blocks(value)

    @field_validator("options", mode="before")
    @classmethod
    def normalize_options(cls, value: object) -> object:
        return _normalize_content_options(value)


class SolutionDraft(StrictModel):
    steps: list[SolutionStep] = Field(min_length=1)
    final_answer: list[ExamContentBlock] = Field(min_length=1)
    alternative_solutions: list[list[SolutionStep]] = Field(default_factory=list)
    verification_notes: list[str] = Field(default_factory=list)

    @field_validator("final_answer", mode="before")
    @classmethod
    def normalize_final_answer(cls, value: object) -> object:
        return _normalize_content_blocks(value)


class RubricDraft(StrictModel):
    items: list[RubricItem] = Field(min_length=1)
    alternative_solution_policy: str = "award_equivalent_method_credit"


class QuestionPlanDraft(StrictModel):
    section_id: str
    slot: int = Field(ge=1)
    topic_tags: list[str] = Field(min_length=1)
    primary_skill: str = Field(min_length=1)
    design_brief: str = Field(min_length=1)
    difficulty: str = Field(min_length=1)
    estimated_minutes: int = Field(ge=1)
    answer_form: str = Field(min_length=1)
    solution_outline: list[str] = Field(min_length=1)
    rubric_focus: list[str] = Field(min_length=1)
    verification_methods: list[str] = Field(min_length=1)
    originality_constraints: list[str] = Field(min_length=1)


class QuestionPlanSetDraft(StrictModel):
    plans: list[QuestionPlanDraft] = Field(min_length=1)


class QuestionSlot(BaseModel):
    section_id: str
    section_title: str
    slot: int = Field(ge=1)
    number: int = Field(ge=1)
    question_type: QuestionType
    score: int = Field(ge=1)
    topic_tags: list[str] = Field(default_factory=list)


class QuestionPlan(BaseModel):
    id: str
    number: int = Field(ge=1)
    question_type: QuestionType
    score: int = Field(ge=1)
    section_id: str
    section_title: str
    slot: int = Field(ge=1)
    topic_tags: list[str] = Field(min_length=1)
    primary_skill: str = Field(min_length=1)
    design_brief: str = Field(min_length=1)
    difficulty: str = Field(min_length=1)
    estimated_minutes: int = Field(ge=1)
    answer_form: str = Field(min_length=1)
    solution_outline: list[str] = Field(min_length=1)
    rubric_focus: list[str] = Field(min_length=1)
    verification_methods: list[str] = Field(min_length=1)
    originality_constraints: list[str] = Field(min_length=1)


class FindingSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    FATAL = "fatal"


class FindingTarget(StrEnum):
    QUESTION = "question"
    SOLUTION = "solution"
    RUBRIC = "rubric"
    BUNDLE = "bundle"


class ReviewFinding(StrictModel):
    code: str
    severity: FindingSeverity
    target: FindingTarget
    message: str
    suggested_action: str = ""


class ReviewReport(StrictModel):
    reviewer: str
    passed: bool
    findings: list[ReviewFinding] = Field(default_factory=list)
    summary: str = ""


class ArbitrationAction(StrEnum):
    PASS = "pass"
    PASS_WITH_WARNINGS = "pass_with_warnings"
    RETRY_PROBLEM = "retry_problem"
    RETRY_SOLUTION = "retry_solution"
    RETRY_RUBRIC = "retry_rubric"
    RETRY_ALL = "retry_all"
    ESCALATE_HUMAN = "escalate_human"
    ABORT = "abort"


class ArbitrationDecision(StrictModel):
    action: ArbitrationAction
    rationale: str
    finding_codes: list[str] = Field(default_factory=list)
    writer_feedback: list[str] = Field(default_factory=list)
    solver_feedback: list[str] = Field(default_factory=list)
    rubric_feedback: list[str] = Field(default_factory=list)


class ExamGenerationRequest(BaseModel):
    subject: str
    target_level: str
    requirements: str
    source_context: str = ""
    subject_profile: SubjectProfile | None = None
    blueprint: ExamBlueprint | None = None
    capability_id: str | None = None
    capability_version: str | None = None
    capability_context: dict[str, list[str]] = Field(default_factory=dict)
    require_blueprint_approval: bool = False
    require_exam_approval: bool = False

    @model_validator(mode="after")
    def validate_preset_pair(self) -> "ExamGenerationRequest":
        if (self.subject_profile is None) != (self.blueprint is None):
            raise ValueError("subject_profile and blueprint must be provided together")
        if (self.capability_id is None) != (self.capability_version is None):
            raise ValueError("capability_id and capability_version must be provided together")
        if self.capability_id is not None and self.subject_profile is None:
            raise ValueError("capability requests require a subject_profile and blueprint")
        return self


class QuestionGenerationRequest(BaseModel):
    profile: SubjectProfile
    blueprint: ExamBlueprint
    plan: QuestionPlan
    source_context: str = ""
    parent_run_id: UUID | None = None
    capability_id: str | None = None
    capability_version: str | None = None
    capability_context: dict[str, list[str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_capability_pair(self) -> "QuestionGenerationRequest":
        if (self.capability_id is None) != (self.capability_version is None):
            raise ValueError("capability_id and capability_version must be provided together")
        return self


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
    CANCELLING = "cancelling"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


ALLOWED_RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.QUEUED: frozenset({RunStatus.RUNNING, RunStatus.CANCELLED}),
    RunStatus.RUNNING: frozenset(
        {
            RunStatus.WAITING_HUMAN,
            RunStatus.CANCELLING,
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.INTERRUPTED,
        }
    ),
    RunStatus.WAITING_HUMAN: frozenset(
        {
            RunStatus.RUNNING,
            RunStatus.INTERRUPTED,
            RunStatus.CANCELLING,
            RunStatus.CANCELLED,
            RunStatus.FAILED,
        }
    ),
    RunStatus.CANCELLING: frozenset({RunStatus.CANCELLED, RunStatus.FAILED, RunStatus.INTERRUPTED}),
    RunStatus.INTERRUPTED: frozenset(
        {RunStatus.RUNNING, RunStatus.CANCELLING, RunStatus.CANCELLED, RunStatus.FAILED}
    ),
    RunStatus.SUCCEEDED: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.CANCELLED: frozenset(),
}


def validate_run_transition(current: RunStatus, target: RunStatus) -> None:
    if target not in ALLOWED_RUN_TRANSITIONS[current]:
        raise ValueError(f"invalid run status transition: {current} -> {target}")


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
    runner_host: str | None = None
    runner_pid: int | None = Field(default=None, ge=1)


class WorkflowCheckpoint(BaseModel):
    run_id: UUID
    workflow: str
    next_step_index: int = Field(ge=0)
    context: dict[str, str | int | float | bool | None | list[str]] = Field(default_factory=dict)
    artifact_bindings: dict[str, UUID] = Field(default_factory=dict)
    child_run_ids: list[UUID] = Field(default_factory=list)
    human_decision_id: UUID | None = None
    created_at: datetime = Field(default_factory=now_utc)


class HumanDecisionType(StrEnum):
    ACCEPT = "accept"
    REJECT = "reject"
    EDIT_ACCEPT = "edit_accept"
    RETRY = "retry"
    ABORT = "abort"


class HumanReviewRequest(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    phase: str
    prompt: str
    artifact_ids: list[UUID] = Field(default_factory=list)
    resume_step_index: int = Field(default=0, ge=0)
    retry_step_index: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=now_utc)
    resolved_at: datetime | None = None


class HumanDecision(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    request_id: UUID
    run_id: UUID
    decision: HumanDecisionType
    actor: str
    reason: str = ""
    input_artifact_ids: list[UUID] = Field(default_factory=list)
    output_artifact_ids: list[UUID] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=now_utc)


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
    id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    logical_name: str
    version: int = Field(ge=1)
    path: Path
    media_type: str
    sha256: str
    size_bytes: int = Field(ge=0)
    created_by_phase: str | None = None
    created_at: datetime = Field(default_factory=now_utc)
