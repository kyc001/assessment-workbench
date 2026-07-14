from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from importlib.resources import files

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from assessment_workbench.domain import (
    ExamBlueprint,
    ExamQuestionBundle,
    FindingSeverity,
    FindingTarget,
    QuestionType,
    ReviewFinding,
    ReviewReport,
    SubjectProfile,
)
from assessment_workbench.prompting import PromptRegistry, load_default_prompt_registry

ReviewHandler = Callable[[ExamQuestionBundle], ReviewReport]


class ValidatorTarget(StrEnum):
    PROFILE = "profile"
    BLUEPRINT = "blueprint"
    BUNDLE = "bundle"


@dataclass(frozen=True)
class ValidationContext:
    profile: SubjectProfile | None = None
    blueprint: ExamBlueprint | None = None
    bundle: ExamQuestionBundle | None = None


ValidationHandler = Callable[[ValidationContext], None]


@dataclass(frozen=True)
class ReviewerDefinition:
    name: str
    prompt_key: str | None = None
    handler: ReviewHandler | None = None

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("reviewer name cannot be empty")
        if (self.prompt_key is None) == (self.handler is None):
            raise ValueError("reviewer must define exactly one of prompt_key or handler")

    @property
    def deterministic(self) -> bool:
        return self.handler is not None


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str

    def __post_init__(self) -> None:
        if not self.name or not self.description:
            raise ValueError("tool name and description cannot be empty")


@dataclass(frozen=True)
class ValidatorDefinition:
    name: str
    target: ValidatorTarget
    handler: ValidationHandler

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("validator name cannot be empty")


class ReviewerRegistry:
    def __init__(self, definitions: Iterable[ReviewerDefinition] = ()) -> None:
        self._definitions: dict[str, ReviewerDefinition] = {}
        for definition in definitions:
            self.register(definition)

    def register(self, definition: ReviewerDefinition) -> None:
        if definition.name in self._definitions:
            raise ValueError(f"reviewer is already registered: {definition.name}")
        self._definitions[definition.name] = definition

    def require(self, name: str) -> ReviewerDefinition:
        try:
            return self._definitions[name]
        except KeyError as exc:
            raise ValueError(f"reviewer is not registered: {name}") from exc

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._definitions))


@dataclass(frozen=True)
class ExamReviewerDefinition:
    name: str
    prompt_key: str

    def __post_init__(self) -> None:
        if not self.name or not self.prompt_key:
            raise ValueError("exam reviewer name and prompt key cannot be empty")


class ExamReviewerRegistry:
    def __init__(self, definitions: Iterable[ExamReviewerDefinition] = ()) -> None:
        self._definitions: dict[str, ExamReviewerDefinition] = {}
        for definition in definitions:
            self.register(definition)

    def register(self, definition: ExamReviewerDefinition) -> None:
        if definition.name in self._definitions:
            raise ValueError(f"exam reviewer is already registered: {definition.name}")
        self._definitions[definition.name] = definition

    def require(self, name: str) -> ExamReviewerDefinition:
        try:
            return self._definitions[name]
        except KeyError as exc:
            raise ValueError(f"exam reviewer is not registered: {name}") from exc

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._definitions))


@dataclass(frozen=True)
class SubjectResearchDefinition:
    name: str
    prompt_key: str

    def __post_init__(self) -> None:
        if not self.name or not self.prompt_key:
            raise ValueError("subject research name and prompt key cannot be empty")


class SubjectResearchRegistry:
    def __init__(self, definitions: Iterable[SubjectResearchDefinition] = ()) -> None:
        self._definitions: dict[str, SubjectResearchDefinition] = {}
        for definition in definitions:
            self.register(definition)

    def register(self, definition: SubjectResearchDefinition) -> None:
        if definition.name in self._definitions:
            raise ValueError(f"subject research role is already registered: {definition.name}")
        self._definitions[definition.name] = definition

    def require(self, name: str) -> SubjectResearchDefinition:
        try:
            return self._definitions[name]
        except KeyError as exc:
            raise ValueError(f"subject research role is not registered: {name}") from exc

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._definitions))


class ToolRegistry:
    def __init__(self, definitions: Iterable[ToolDefinition] = ()) -> None:
        self._definitions: dict[str, ToolDefinition] = {}
        for definition in definitions:
            self.register(definition)

    def register(self, definition: ToolDefinition) -> None:
        if definition.name in self._definitions:
            raise ValueError(f"tool is already registered: {definition.name}")
        self._definitions[definition.name] = definition

    def require(self, name: str) -> ToolDefinition:
        try:
            return self._definitions[name]
        except KeyError as exc:
            raise ValueError(f"tool is not registered: {name}") from exc

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._definitions))


class ValidatorRegistry:
    def __init__(self, definitions: Iterable[ValidatorDefinition] = ()) -> None:
        self._definitions: dict[str, ValidatorDefinition] = {}
        for definition in definitions:
            self.register(definition)

    def register(self, definition: ValidatorDefinition) -> None:
        if definition.name in self._definitions:
            raise ValueError(f"validator is already registered: {definition.name}")
        self._definitions[definition.name] = definition

    def require(self, name: str) -> ValidatorDefinition:
        try:
            return self._definitions[name]
        except KeyError as exc:
            raise ValueError(f"validator is not registered: {name}") from exc

    def validate(
        self,
        names: Iterable[str],
        target: ValidatorTarget,
        context: ValidationContext,
    ) -> None:
        for name in names:
            definition = self.require(name)
            if definition.target is target:
                definition.handler(context)


class SubjectCapability(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    profile: SubjectProfile
    blueprint: ExamBlueprint | None = None
    prompt_context: dict[str, list[str]] = Field(default_factory=dict)
    validators: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_links(self) -> SubjectCapability:
        if self.profile.id != self.id:
            raise ValueError("capability id must match its subject profile id")
        if self.blueprint is not None and self.blueprint.subject_profile != self.profile.id:
            raise ValueError("capability blueprint must reference its subject profile")
        return self


class SubjectCapabilityRegistry:
    def __init__(self) -> None:
        self._capabilities: dict[str, SubjectCapability] = {}
        self._aliases: dict[str, str] = {}

    def register(self, capability: SubjectCapability) -> None:
        if capability.id in self._capabilities:
            raise ValueError(f"subject capability is already registered: {capability.id}")
        aliases = {_normalize_subject_name(capability.id)}
        aliases.update(_normalize_subject_name(alias) for alias in capability.aliases)
        conflicts = sorted(alias for alias in aliases if alias in self._aliases)
        if conflicts:
            raise ValueError(f"subject capability aliases are already registered: {conflicts}")
        self._capabilities[capability.id] = capability
        for alias in aliases:
            self._aliases[alias] = capability.id

    def resolve(self, subject: str) -> SubjectCapability | None:
        capability_id = self._aliases.get(_normalize_subject_name(subject))
        return self._capabilities.get(capability_id) if capability_id is not None else None

    def require(self, capability_id: str) -> SubjectCapability:
        try:
            return self._capabilities[capability_id]
        except KeyError as exc:
            raise ValueError(f"subject capability is not registered: {capability_id}") from exc


@dataclass(frozen=True)
class CapabilityCatalog:
    prompts: PromptRegistry
    reviewers: ReviewerRegistry
    exam_reviewers: ExamReviewerRegistry
    subject_researchers: SubjectResearchRegistry
    tools: ToolRegistry
    validators: ValidatorRegistry
    subjects: SubjectCapabilityRegistry

    def require_subject_binding(
        self,
        capability_id: str,
        capability_version: str,
        profile: SubjectProfile,
        blueprint: ExamBlueprint,
        prompt_context: dict[str, list[str]],
    ) -> SubjectCapability:
        capability = self.subjects.require(capability_id)
        if capability.version != capability_version:
            raise ValueError("subject capability version does not match the persisted request")
        if (
            capability.profile != profile
            or capability.blueprint != blueprint
            or capability.prompt_context != prompt_context
        ):
            raise ValueError(
                "persisted capability content does not match the subject capability version"
            )
        return capability

    def validate_profile(
        self,
        profile: SubjectProfile,
        validator_names: Iterable[str] = (),
    ) -> None:
        for reviewer_name in profile.reviewers:
            reviewer = self.reviewers.require(reviewer_name)
            if reviewer.prompt_key is not None:
                self.prompts.require(reviewer.prompt_key)
        for tool_name in profile.tools:
            self.tools.require(tool_name)
        self.validators.validate(
            ("profile_core", *validator_names),
            ValidatorTarget.PROFILE,
            ValidationContext(profile=profile),
        )

    def validate_blueprint(
        self,
        profile: SubjectProfile,
        blueprint: ExamBlueprint,
        validator_names: Iterable[str] = (),
    ) -> None:
        self.validators.validate(
            ("blueprint_core", *validator_names),
            ValidatorTarget.BLUEPRINT,
            ValidationContext(profile=profile, blueprint=blueprint),
        )

    def validate_bundle(
        self,
        profile: SubjectProfile,
        bundle: ExamQuestionBundle,
        validator_names: Iterable[str] = (),
    ) -> None:
        self.validators.validate(
            ("bundle_core", *validator_names),
            ValidatorTarget.BUNDLE,
            ValidationContext(profile=profile, bundle=bundle),
        )

    def register_subject(self, capability: SubjectCapability) -> None:
        for validator_name in capability.validators:
            self.validators.require(validator_name)
        self.validate_profile(capability.profile, capability.validators)
        if capability.blueprint is not None:
            self.validate_blueprint(
                capability.profile,
                capability.blueprint,
                capability.validators,
            )
        self.subjects.register(capability)


def load_default_capability_catalog(
    prompts: PromptRegistry | None = None,
) -> CapabilityCatalog:
    prompt_registry = prompts or load_default_prompt_registry()
    reviewers = ReviewerRegistry(
        [
            ReviewerDefinition("mathematical", prompt_key="reviewer_mathematical"),
            ReviewerDefinition("subject", prompt_key="reviewer_subject"),
            ReviewerDefinition("solvability", prompt_key="reviewer_solvability"),
            ReviewerDefinition("rubric", prompt_key="reviewer_rubric"),
            ReviewerDefinition("pedagogical", prompt_key="reviewer_pedagogical"),
            ReviewerDefinition("structure", handler=_structure_review),
        ]
    )
    exam_reviewers = ExamReviewerRegistry(
        [
            ExamReviewerDefinition("duplication", "exam_reviewer_duplication"),
            ExamReviewerDefinition("consistency", "exam_reviewer_consistency"),
            ExamReviewerDefinition("leakage", "exam_reviewer_leakage"),
            ExamReviewerDefinition("risk", "exam_reviewer_risk"),
        ]
    )
    for reviewer_name in exam_reviewers.names():
        prompt_registry.require(exam_reviewers.require(reviewer_name).prompt_key)
    subject_researchers = SubjectResearchRegistry(
        [
            SubjectResearchDefinition("assessment_design", "subject_research_assessment"),
            SubjectResearchDefinition("curriculum_scope", "subject_research_curriculum"),
            SubjectResearchDefinition("quality_policy", "subject_research_quality"),
        ]
    )
    for research_name in subject_researchers.names():
        prompt_registry.require(subject_researchers.require(research_name).prompt_key)
    prompt_registry.require("subject_research_synthesizer")
    prompt_registry.require("question_plan_reviser")
    prompt_registry.require("exam_arbiter")
    tools = ToolRegistry(
        [
            ToolDefinition("sympy", "Symbolic mathematics verification capability"),
            ToolDefinition("numerical_sampler", "Numerical and boundary sampling capability"),
        ]
    )
    validators = ValidatorRegistry(
        [
            ValidatorDefinition("profile_core", ValidatorTarget.PROFILE, _validate_profile_core),
            ValidatorDefinition(
                "blueprint_core", ValidatorTarget.BLUEPRINT, _validate_blueprint_core
            ),
            ValidatorDefinition("bundle_core", ValidatorTarget.BUNDLE, _validate_bundle_core),
            ValidatorDefinition(
                "gaokao_mathematics_blueprint",
                ValidatorTarget.BLUEPRINT,
                _validate_gaokao_mathematics_blueprint,
            ),
        ]
    )
    catalog = CapabilityCatalog(
        prompts=prompt_registry,
        reviewers=reviewers,
        exam_reviewers=exam_reviewers,
        subject_researchers=subject_researchers,
        tools=tools,
        validators=validators,
        subjects=SubjectCapabilityRegistry(),
    )
    for capability in _load_bundled_subject_capabilities():
        catalog.register_subject(capability)
    return catalog


def _load_bundled_subject_capabilities() -> list[SubjectCapability]:
    resource = files("assessment_workbench").joinpath("resources", "subject-capabilities.yaml")
    payload = yaml.safe_load(resource.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("capabilities"), list):
        raise ValueError("subject capability YAML must define a capabilities list")
    raw_capabilities = payload["capabilities"]
    return [SubjectCapability.model_validate(item) for item in raw_capabilities]


def _normalize_subject_name(value: str) -> str:
    normalized = " ".join(value.strip().casefold().split())
    if not normalized:
        raise ValueError("subject capability alias cannot be empty")
    return normalized


def _validate_profile_core(context: ValidationContext) -> None:
    if context.profile is None:
        raise TypeError("profile validator requires a subject profile")
    if len(context.profile.reviewers) != len(set(context.profile.reviewers)):
        raise ValueError("subject profile reviewers must be unique")
    if len(context.profile.tools) != len(set(context.profile.tools)):
        raise ValueError("subject profile tools must be unique")


def _validate_blueprint_core(context: ValidationContext) -> None:
    profile = context.profile
    blueprint = context.blueprint
    if profile is None or blueprint is None:
        raise TypeError("blueprint validator requires a profile and blueprint")
    if blueprint.subject_profile != profile.id:
        raise ValueError("blueprint subject_profile does not match the subject profile id")
    unsupported = {section.question_type for section in blueprint.sections} - set(
        profile.supported_question_types
    )
    if unsupported:
        names = sorted(item.value for item in unsupported)
        raise ValueError(f"blueprint uses unsupported question types: {names}")


def _validate_gaokao_mathematics_blueprint(context: ValidationContext) -> None:
    blueprint = context.blueprint
    if blueprint is None:
        raise TypeError("gaokao mathematics validator requires a blueprint")
    expected_types = [
        QuestionType.MULTIPLE_CHOICE,
        QuestionType.MULTIPLE_SELECT,
        QuestionType.FILL_BLANK,
        QuestionType.CONSTRUCTED_RESPONSE,
    ]
    if blueprint.total_score != 150 or blueprint.duration_minutes != 120:
        raise ValueError("gaokao mathematics blueprint must be 120 minutes and 150 points")
    if [section.count for section in blueprint.sections] != [8, 3, 3, 5]:
        raise ValueError("gaokao mathematics blueprint must use the 8/3/3/5 structure")
    if [section.question_type for section in blueprint.sections] != expected_types:
        raise ValueError("gaokao mathematics blueprint section types are invalid")
    if [section.total_score for section in blueprint.sections] != [40, 18, 15, 77]:
        raise ValueError("gaokao mathematics blueprint section scores are invalid")


def _validate_bundle_core(context: ValidationContext) -> None:
    bundle = context.bundle
    if bundle is None:
        raise TypeError("bundle validator requires an exam question bundle")
    if bundle.solution.question_version_id != bundle.question.id:
        raise ValueError("solution does not reference the current question version")
    if bundle.rubric.solution_version_id != bundle.solution.id:
        raise ValueError("rubric does not reference the current solution version")
    if bundle.rubric.max_score != bundle.question.score:
        raise ValueError("rubric max_score does not match the question score")


def _structure_review(bundle: ExamQuestionBundle) -> ReviewReport:
    findings: list[ReviewFinding] = []
    if (
        bundle.question.question_type
        in {
            QuestionType.MULTIPLE_CHOICE,
            QuestionType.MULTIPLE_SELECT,
        }
        and len(bundle.question.options) < 4
    ):
        findings.append(
            ReviewFinding(
                code="choice_options",
                severity=FindingSeverity.ERROR,
                target=FindingTarget.QUESTION,
                message="Choice question has fewer than four options.",
            )
        )
    if sum(item.score for item in bundle.rubric.items) != bundle.question.score:
        findings.append(
            ReviewFinding(
                code="rubric_score_total",
                severity=FindingSeverity.FATAL,
                target=FindingTarget.RUBRIC,
                message="Rubric scores do not sum to the question score.",
            )
        )
    return ReviewReport(
        reviewer="structure",
        passed=not findings,
        findings=findings,
        summary="Deterministic domain and scoring checks.",
    )
