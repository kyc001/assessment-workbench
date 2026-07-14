import re
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
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


_STRUCTURED_MATH_ENVIRONMENT = re.compile(
    r"\\begin\{(?:aligned|alignedat|array|matrix|pmatrix|bmatrix|vmatrix|Vmatrix|"
    r"smallmatrix|cases|gathered|split)\*?\}"
)
_DOUBLE_ESCAPED_LATEX_COMMAND = re.compile(r"\\\\(?=[A-Za-z])")


def _normalize_latex_command_escapes(content: str) -> str:
    if _STRUCTURED_MATH_ENVIRONMENT.search(content):
        return content
    return _DOUBLE_ESCAPED_LATEX_COMMAND.sub(lambda _: "\\", content)


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
        content = _normalize_latex_command_escapes(content)
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


class CalculatorPolicy(StrEnum):
    UNSPECIFIED = "unspecified"
    PROHIBITED = "prohibited"
    SCIENTIFIC_ALLOWED = "scientific_allowed"


class DifficultyBasis(StrEnum):
    UNSPECIFIED = "unspecified"
    QUESTION_COUNT = "question_count"
    SCORE = "score"
    ESTIMATED_TIME = "estimated_time"


class ExamBlueprint(BaseModel):
    id: str
    version: str = "1"
    subject_profile: str
    title: str
    target_level: str
    duration_minutes: int = Field(ge=1)
    total_score: int = Field(ge=1)
    language: str = "zh-CN"
    calculator_policy: CalculatorPolicy = CalculatorPolicy.UNSPECIFIED
    difficulty_basis: DifficultyBasis = DifficultyBasis.UNSPECIFIED
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
    research_synthesis_artifact_id: UUID | None = None

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
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(item_id: str) -> None:
            if item_id in visiting:
                raise ValueError("rubric item dependencies must be acyclic")
            if item_id in visited:
                return
            visiting.add(item_id)
            item = next(candidate for candidate in self.items if candidate.id == item_id)
            for dependency in item.depends_on:
                visit(dependency)
            visiting.remove(item_id)
            visited.add(item_id)

        for item_id in item_ids:
            visit(item_id)
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
    calculator_policy: CalculatorPolicy = CalculatorPolicy.UNSPECIFIED
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


class ExamView(StrEnum):
    QUESTIONS = "questions"
    SOLUTIONS = "solutions"
    RUBRIC = "rubric"


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


class ResearchEvidence(StrictModel):
    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    publisher: str = Field(min_length=1)
    url_or_document_id: str = Field(min_length=1)
    locator: str = ""
    excerpt: str = Field(min_length=1)
    authority: str = Field(min_length=1)
    directness: str = Field(min_length=1)
    retrieved_at: str = Field(min_length=1)


class ResearchClaim(StrictModel):
    id: str = Field(min_length=1)
    category: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    proposed_value: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)
    assumption: bool = False
    confidence: str = Field(min_length=1)
    confidence_rationale: str = Field(min_length=1)


class ResearchTopic(StrictModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    prerequisites: list[str] = Field(default_factory=list)
    included: bool
    exclusion_reason: str | None = None


class AssessmentDesignCandidate(StrictModel):
    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    audience: str = Field(min_length=1)
    assessment_purpose: str = Field(min_length=1)
    duration_minutes: int = Field(ge=1)
    total_score: int = Field(ge=1)
    sections: list[ExamSectionBlueprint] = Field(min_length=1)
    coverage: list[CoverageTarget] = Field(min_length=1)
    difficulty_distribution: DifficultyDistribution
    constraints: list[str] = Field(default_factory=list)
    claim_ids: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)


class ResearchConflict(StrictModel):
    id: str = Field(min_length=1)
    field_path: str = Field(min_length=1)
    competing_claim_ids: list[str] = Field(min_length=2)
    rationale: str = Field(min_length=1)
    requires_human: bool = True


class SubjectResearchReport(StrictModel):
    research_role: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    audience_candidates: list[str] = Field(min_length=1)
    course_variant_candidates: list[str] = Field(min_length=1)
    prerequisites: list[str] = Field(default_factory=list)
    topics: list[ResearchTopic] = Field(default_factory=list)
    claims: list[ResearchClaim] = Field(min_length=1)
    evidence: list[ResearchEvidence] = Field(min_length=1)
    assessment_design_candidates: list[AssessmentDesignCandidate] = Field(default_factory=list)
    quality_rules: list[str] = Field(default_factory=list)
    conflicts: list[ResearchConflict] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_references(self) -> "SubjectResearchReport":
        evidence_ids = {item.id for item in self.evidence}
        claim_ids = {item.id for item in self.claims}
        if len(evidence_ids) != len(self.evidence) or len(claim_ids) != len(self.claims):
            raise ValueError("research evidence and claim ids must be unique")
        for claim in self.claims:
            if not set(claim.evidence_ids) <= evidence_ids:
                raise ValueError(f"research claim {claim.id} references unknown evidence")
            if not claim.evidence_ids and not claim.assumption:
                raise ValueError(f"research claim {claim.id} requires evidence or assumption=true")
        for conflict in self.conflicts:
            if not set(conflict.competing_claim_ids) <= claim_ids:
                raise ValueError(f"research conflict {conflict.id} references unknown claims")
        return self


class ResearchFieldTrace(StrictModel):
    target_path: str = Field(min_length=1)
    claim_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    decision_type: str = Field(min_length=1)
    decision_rationale: str = Field(min_length=1)


class SubjectResearchSynthesis(StrictModel):
    selected_course_variant: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    successful_research_run_ids: list[UUID] = Field(default_factory=list)
    failed_research_run_ids: list[UUID] = Field(default_factory=list)
    adopted_claim_ids: list[str] = Field(min_length=1)
    rejected_claim_ids: list[str] = Field(default_factory=list)
    unresolved_conflict_ids: list[str] = Field(default_factory=list)
    field_traces: list[ResearchFieldTrace] = Field(min_length=1)
    profile: SubjectProfileCandidate
    blueprint: BlueprintDraft
    human_confirmation_required: bool = True


class SubjectResearchRequest(StrictModel):
    research_role: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    target_level: str = Field(min_length=1)
    requirements: str
    source_context: str = ""
    parent_run_id: UUID
    attempt: int = Field(ge=1)
    input_signature: str = Field(min_length=64, max_length=64)
    input_artifact_ids: list[UUID] = Field(default_factory=list)


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
    coverage_tag: str | None = None
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
    coverage_tag: str | None = None


class QuestionPlanningProgress(StrictModel):
    blueprint_id: str = Field(min_length=1)
    blueprint_version: str = Field(min_length=1)
    next_attempt: int = Field(ge=1)
    validation_feedback: list[str] = Field(default_factory=list)
    draft_artifact_ids: list[UUID] = Field(default_factory=list)
    validation_artifact_ids: list[UUID] = Field(default_factory=list)


class QuestionPlan(BaseModel):
    id: str
    number: int = Field(ge=1)
    question_type: QuestionType
    score: int = Field(ge=1)
    section_id: str
    section_title: str
    slot: int = Field(ge=1)
    topic_tags: list[str] = Field(min_length=1)
    coverage_tag: str | None = None
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

    @model_validator(mode="after")
    def validate_finding_consistency(self) -> "ReviewReport":
        blocking = [
            finding
            for finding in self.findings
            if finding.severity in {FindingSeverity.ERROR, FindingSeverity.FATAL}
        ]
        if self.passed and blocking:
            raise ValueError("passed review report cannot contain error or fatal findings")
        if not self.passed and not blocking:
            raise ValueError("failed review report requires an error or fatal finding")
        for finding in blocking:
            message = finding.message.casefold()
            action = finding.suggested_action.strip().casefold()
            if finding.severity is FindingSeverity.FATAL and (
                "no fatal" in message
                or action in {"no action", "no action needed", "none"}
                or action.startswith("no action needed")
            ):
                raise ValueError(
                    f"fatal review finding {finding.code} contradicts its message or action"
                )
        return self


class ExamReviewTarget(StrEnum):
    EXAM = "exam"
    QUESTION = "question"
    SECTION = "section"
    PLAN = "plan"
    LAYOUT = "layout"


class ExamReviewFinding(StrictModel):
    code: str = Field(min_length=1)
    severity: FindingSeverity
    target: ExamReviewTarget
    message: str = Field(min_length=1)
    question_ids: list[UUID] = Field(default_factory=list)
    section_ids: list[str] = Field(default_factory=list)
    suggested_action: str = ""


class ExamReviewReport(StrictModel):
    reviewer: str = Field(min_length=1)
    passed: bool
    findings: list[ExamReviewFinding] = Field(default_factory=list)
    summary: str = ""


class ExamBundleVersionSignature(StrictModel):
    question_version_ids: list[UUID] = Field(min_length=1)
    solution_version_ids: list[UUID] = Field(min_length=1)
    rubric_version_ids: list[UUID] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_lengths(self) -> "ExamBundleVersionSignature":
        lengths = {
            len(self.question_version_ids),
            len(self.solution_version_ids),
            len(self.rubric_version_ids),
        }
        if len(lengths) != 1:
            raise ValueError("exam bundle version signature lengths must match")
        return self


class ExamReviewRequest(StrictModel):
    reviewer: str = Field(min_length=1)
    profile: SubjectProfile
    blueprint: ExamBlueprint
    plans: list[QuestionPlan] = Field(min_length=1)
    exam: ExamDocument
    parent_run_id: UUID
    attempt: int = Field(ge=1)
    capability_context: dict[str, list[str]] = Field(default_factory=dict)
    rendering_context: dict[str, str] = Field(default_factory=dict)
    input_artifact_ids: list[UUID] = Field(default_factory=list)


class ExamArbitrationAction(StrEnum):
    PASS = "pass"
    PASS_WITH_WARNINGS = "pass_with_warnings"
    REPLACE_QUESTIONS = "replace_questions"
    REBALANCE_DIFFICULTY = "rebalance_difficulty"
    REBALANCE_COVERAGE = "rebalance_coverage"
    REGENERATE_SECTION = "regenerate_section"
    ESCALATE_HUMAN = "escalate_human"
    ABORT = "abort"


class ExamArbitrationDecision(StrictModel):
    action: ExamArbitrationAction
    rationale: str = Field(min_length=1)
    finding_codes: list[str] = Field(default_factory=list)
    question_ids: list[UUID] = Field(default_factory=list)
    section_ids: list[str] = Field(default_factory=list)
    plan_feedback: list[str] = Field(default_factory=list)
    question_feedback: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_targets(self) -> "ExamArbitrationDecision":
        if self.action in {
            ExamArbitrationAction.PASS,
            ExamArbitrationAction.PASS_WITH_WARNINGS,
            ExamArbitrationAction.ESCALATE_HUMAN,
            ExamArbitrationAction.ABORT,
        }:
            if self.question_ids or self.section_ids:
                raise ValueError(f"{self.action.value} cannot target questions or sections")
            return self
        if self.action is ExamArbitrationAction.REPLACE_QUESTIONS and not self.question_ids:
            raise ValueError("replace_questions requires question_ids")
        if self.action is ExamArbitrationAction.REGENERATE_SECTION and not self.section_ids:
            raise ValueError("regenerate_section requires section_ids")
        if not self.question_ids and not self.section_ids:
            raise ValueError(f"{self.action.value} requires question_ids or section_ids")
        return self


class ExamWorkflowState(StrictModel):
    round: int = Field(default=1, ge=1)
    replacement_rounds: int = Field(default=0, ge=0)
    rebalance_rounds: int = Field(default=0, ge=0)
    replacement_question_numbers: list[int] = Field(default_factory=list)
    revision_plan_ids: list[str] = Field(default_factory=list)
    plan_feedback: list[str] = Field(default_factory=list)
    question_feedback: list[str] = Field(default_factory=list)
    last_action: ExamArbitrationAction | None = None
    requires_human_review: bool = False


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


class QuestionWorkflowState(StrictModel):
    question_id: UUID = Field(default_factory=uuid4)
    solution_id: UUID = Field(default_factory=uuid4)
    rubric_id: UUID = Field(default_factory=uuid4)
    round: int = Field(default=1, ge=1)
    problem_retries: int = Field(default=0, ge=0)
    solution_retries: int = Field(default=0, ge=0)
    rubric_retries: int = Field(default=0, ge=0)
    writer_feedback: list[str] = Field(default_factory=list)
    solver_feedback: list[str] = Field(default_factory=list)
    rubric_feedback: list[str] = Field(default_factory=list)
    last_action: ArbitrationAction | None = None
    requires_human_review: bool = False


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
    generation_feedback: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_capability_pair(self) -> "QuestionGenerationRequest":
        if (self.capability_id is None) != (self.capability_version is None):
            raise ValueError("capability_id and capability_version must be provided together")
        return self


class QuestionReviewRequest(StrictModel):
    reviewer: str = Field(min_length=1)
    plan: QuestionPlan
    bundle: ExamQuestionBundle
    capability_context: dict[str, list[str]] = Field(default_factory=dict)
    parent_run_id: UUID
    attempt: int = Field(ge=1)
    input_artifact_ids: list[UUID] = Field(default_factory=list)


class ContextArtifactBinding(StrictModel):
    artifact_id: UUID
    run_id: UUID
    logical_name: str = Field(min_length=1)
    version: int = Field(ge=1)
    sha256: str = Field(min_length=64, max_length=64)


class ContextPack(StrictModel):
    format_version: str = "1"
    prompt_key: str = Field(min_length=1)
    role: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    response_model: str = Field(min_length=1)
    user_prompt_sha256: str = Field(min_length=64, max_length=64)
    payload: dict[str, Any]
    input_artifacts: list[ContextArtifactBinding] = Field(default_factory=list)


class ModelAuditContext(StrictModel):
    context_pack_id: UUID
    context_pack_sha256: str = Field(min_length=64, max_length=64)
    system_prompt_sha256: str = Field(min_length=64, max_length=64)
    response_schema_sha256: str = Field(min_length=64, max_length=64)


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
    request_sha256_sequence: list[str] = Field(default_factory=list)
    response_sha256: str | None = None
    audit_context: ModelAuditContext | None = None
    repair_count: int = Field(default=0, ge=0)
    provider_request_id: str | None = None
    finish_reason: str | None = None
    endpoint_origin: str | None = None
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


class SubjectResearchRunRecord(StrictModel):
    research_role: str = Field(min_length=1)
    attempt: int = Field(ge=1)
    run_id: UUID
    status: RunStatus
    input_signature: str = Field(min_length=64, max_length=64)
    report_artifact_id: UUID | None = None
    error: str | None = None

    @model_validator(mode="after")
    def validate_report_binding(self) -> "SubjectResearchRunRecord":
        if self.status is RunStatus.SUCCEEDED and self.report_artifact_id is None:
            raise ValueError("succeeded subject research run requires a report artifact")
        return self


class ReviewerRunRecord(StrictModel):
    reviewer: str = Field(min_length=1)
    attempt: int = Field(ge=1)
    run_id: UUID
    status: RunStatus
    question_version_id: UUID
    solution_version_id: UUID
    rubric_version_id: UUID
    report_artifact_id: UUID | None = None
    error: str | None = None

    @model_validator(mode="after")
    def validate_report_binding(self) -> "ReviewerRunRecord":
        if self.status is RunStatus.SUCCEEDED and self.report_artifact_id is None:
            raise ValueError("succeeded reviewer run requires a report artifact")
        return self


class ExamReviewerRunRecord(StrictModel):
    reviewer: str = Field(min_length=1)
    attempt: int = Field(ge=1)
    run_id: UUID
    status: RunStatus
    exam_id: UUID
    signature: ExamBundleVersionSignature
    report_artifact_id: UUID | None = None
    error: str | None = None

    @model_validator(mode="after")
    def validate_report_binding(self) -> "ExamReviewerRunRecord":
        if self.status is RunStatus.SUCCEEDED and self.report_artifact_id is None:
            raise ValueError("succeeded exam reviewer run requires a report artifact")
        return self


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
    RunStatus.FAILED: frozenset({RunStatus.INTERRUPTED}),
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


class DocumentBuildInputSignature(StrictModel):
    exam_id: UUID
    bundle_versions: ExamBundleVersionSignature
    renderer: str = Field(min_length=1)
    renderer_version: str = Field(min_length=1)
    compiler: str = Field(min_length=1)
    inspector: str = Field(min_length=1)
    inspector_version: str = Field(min_length=1)


class PdfPageInspection(StrictModel):
    page_number: int = Field(ge=1)
    width_points: float = Field(gt=0)
    height_points: float = Field(gt=0)
    text_characters: int = Field(ge=0)
    ink_ratio: float = Field(ge=0, le=1)
    edge_ink_ratio: float = Field(ge=0, le=1)
    image_artifact_id: UUID


class PdfInspectionReport(StrictModel):
    view: ExamView
    pdf_artifact_id: UUID
    page_count: int = Field(ge=1)
    extracted_text_sha256: str = Field(min_length=64, max_length=64)
    pages: list[PdfPageInspection] = Field(min_length=1)
    blocking_findings: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    manual_checks_required: list[str] = Field(min_length=1)
    passed: bool

    @model_validator(mode="after")
    def validate_result(self) -> "PdfInspectionReport":
        if len(self.pages) != self.page_count:
            raise ValueError("PDF inspection page count does not match page records")
        if self.passed == bool(self.blocking_findings):
            raise ValueError("PDF inspection passed flag does not match blocking findings")
        return self


class DocumentBuildRunRecord(StrictModel):
    view: ExamView
    attempt: int = Field(ge=1)
    input_signature: DocumentBuildInputSignature
    input_signature_sha256: str = Field(min_length=64, max_length=64)
    run_id: UUID
    status: RunStatus
    source_artifact_id: UUID | None = None
    pdf_artifact_id: UUID | None = None
    log_artifact_id: UUID | None = None
    inspection_artifact_id: UUID | None = None
    page_artifact_ids: list[UUID] = Field(default_factory=list)
    error: str | None = None

    @model_validator(mode="after")
    def validate_outputs(self) -> "DocumentBuildRunRecord":
        if self.status is RunStatus.SUCCEEDED:
            required = (
                self.source_artifact_id,
                self.pdf_artifact_id,
                self.log_artifact_id,
                self.inspection_artifact_id,
            )
            if any(value is None for value in required) or not self.page_artifact_ids:
                raise ValueError("succeeded document build requires all output artifacts")
        return self


class DocumentAcceptanceRecord(StrictModel):
    decision_id: UUID
    actor: str = Field(min_length=1)
    reason: str = ""
    manifest_artifact_id: UUID
    page_artifact_ids: list[UUID] = Field(min_length=1)
    created_at: datetime = Field(default_factory=now_utc)


class ReleaseArtifactBinding(StrictModel):
    artifact_id: UUID
    run_id: UUID
    logical_name: str = Field(min_length=1)
    version: int = Field(ge=1)
    media_type: str = Field(min_length=1)
    sha256: str = Field(min_length=64, max_length=64)
    size_bytes: int = Field(ge=0)


class ReleaseLevel(StrEnum):
    MACHINE_VERIFIED = "machine_verified"
    HUMAN_VERIFIED = "human_verified"


class ExamReleaseBundle(StrictModel):
    format_version: str = "1"
    root_run_id: UUID
    exam_id: UUID
    exam_signature: ExamBundleVersionSignature
    release_level: ReleaseLevel
    run_ids: list[UUID] = Field(min_length=1)
    model_call_ids: list[UUID] = Field(default_factory=list)
    exam_artifact_id: UUID
    question_bundle_artifact_ids: list[UUID] = Field(min_length=1)
    review_artifact_ids: list[UUID] = Field(default_factory=list)
    arbitration_artifact_ids: list[UUID] = Field(default_factory=list)
    context_pack_artifact_ids: list[UUID] = Field(default_factory=list)
    document_builds: list[DocumentBuildRunRecord] = Field(min_length=3, max_length=3)
    acceptance_artifact_id: UUID | None = None
    artifacts: list[ReleaseArtifactBinding] = Field(min_length=1)
    created_at: datetime = Field(default_factory=now_utc)

    @model_validator(mode="after")
    def validate_release(self) -> "ExamReleaseBundle":
        views = [record.view for record in self.document_builds]
        if len(set(views)) != len(ExamView) or set(views) != set(ExamView):
            raise ValueError("release bundle requires one successful build for every exam view")
        if any(record.status is not RunStatus.SUCCEEDED for record in self.document_builds):
            raise ValueError("release bundle cannot contain failed document builds")
        if self.release_level is ReleaseLevel.HUMAN_VERIFIED:
            if self.acceptance_artifact_id is None:
                raise ValueError("human-verified release requires an acceptance artifact")
        elif self.acceptance_artifact_id is not None:
            raise ValueError("machine-verified release cannot claim a human acceptance artifact")
        artifact_ids = {binding.artifact_id for binding in self.artifacts}
        required_ids = {
            self.exam_artifact_id,
            *self.question_bundle_artifact_ids,
            *self.review_artifact_ids,
            *self.arbitration_artifact_ids,
            *self.context_pack_artifact_ids,
            *(
                artifact_id
                for record in self.document_builds
                for artifact_id in (
                    record.source_artifact_id,
                    record.log_artifact_id,
                    record.pdf_artifact_id,
                    *record.page_artifact_ids,
                    record.inspection_artifact_id,
                )
                if artifact_id is not None
            ),
        }
        if self.acceptance_artifact_id is not None:
            required_ids.add(self.acceptance_artifact_id)
        if not required_ids <= artifact_ids:
            raise ValueError("release bundle contains artifact references outside its index")
        return self
