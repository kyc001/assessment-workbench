from collections.abc import Iterable
from copy import deepcopy
from enum import StrEnum
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import Field, ValidationError, model_validator

from assessment_workbench.domain import (
    ExamContentBlock,
    ExamContentKind,
    ExamQuestionBundle,
    FindingTarget,
    GenerationMetadata,
    QuestionVersion,
    ReviewReport,
    RubricItem,
    RubricVersion,
    SolutionStep,
    SolutionVersion,
    StrictModel,
)

BENCHMARK_CASE_SCHEMA_VERSION: Literal["benchmark-case-v1"] = "benchmark-case-v1"
VERIFIER_OBSERVATION_SCHEMA_VERSION: Literal["verifier-observation-v1"] = (
    "verifier-observation-v1"
)


class AttackKind(StrEnum):
    FORMAT_VALID_SEMANTIC_ERROR = "format_valid_semantic_error"
    LUCKY_ANSWER_WRONG_REASONING = "lucky_answer_wrong_reasoning"
    SHARED_FALSE_PREMISE = "shared_false_premise"
    RUBRIC_LOOPHOLE = "rubric_loophole"
    UNDERSPECIFIED_QUESTION = "underspecified_question"
    DIFFICULTY_COVERAGE_GAMING = "difficulty_coverage_gaming"


class OracleVerdict(StrEnum):
    VALID = "valid"
    INVALID = "invalid"


class OracleMethod(StrEnum):
    SYMBOLIC = "symbolic"
    NUMERIC = "numeric"
    HUMAN = "human"
    SOURCE_REFERENCE = "source_reference"
    HYBRID = "hybrid"


class BenchmarkOracle(StrictModel):
    verdict: OracleVerdict
    method: OracleMethod
    rationale: str = Field(min_length=1)
    error_targets: list[FindingTarget] = Field(default_factory=list)
    error_codes: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_verdict_evidence(self) -> "BenchmarkOracle":
        if self.verdict is OracleVerdict.VALID:
            if self.error_targets or self.error_codes:
                raise ValueError("valid oracle cannot declare errors")
            return self
        if not self.error_targets or not self.error_codes:
            raise ValueError("invalid oracle requires error targets and error codes")
        return self


class BenchmarkCase(StrictModel):
    schema_version: Literal["benchmark-case-v1"] = BENCHMARK_CASE_SCHEMA_VERSION
    case_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    bundle: ExamQuestionBundle
    oracle: BenchmarkOracle
    attack_kind: AttackKind | None = None
    parent_case_id: str | None = None
    attack_iteration: int = Field(default=0, ge=0)
    candidate_index: int | None = Field(default=None, ge=1)
    source_run_id: UUID | None = None
    source_artifact_id: UUID | None = None
    tags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_case_lineage(self) -> "BenchmarkCase":
        if self.attack_kind is None:
            if (
                self.parent_case_id is not None
                or self.attack_iteration != 0
                or self.candidate_index is not None
            ):
                raise ValueError("clean benchmark case cannot declare attack lineage")
            if self.oracle.verdict is not OracleVerdict.VALID:
                raise ValueError("clean benchmark case requires a valid oracle verdict")
            return self
        if not self.parent_case_id:
            raise ValueError("attacked benchmark case requires parent_case_id")
        if self.parent_case_id == self.case_id:
            raise ValueError("benchmark case cannot attack itself")
        if self.attack_iteration < 1:
            raise ValueError("attacked benchmark case requires attack_iteration >= 1")
        if self.oracle.verdict is not OracleVerdict.INVALID:
            raise ValueError("attacked benchmark case requires an invalid oracle verdict")
        return self


class VerifierObservation(StrictModel):
    schema_version: Literal["verifier-observation-v1"] = VERIFIER_OBSERVATION_SCHEMA_VERSION
    observation_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    case_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    trial: int = Field(default=1, ge=1)
    question_version_id: UUID
    solution_version_id: UUID
    rubric_version_id: UUID
    report: ReviewReport
    confidence: float | None = Field(default=None, ge=0, le=1)
    reward_candidate: float | None = None
    model: str | None = None
    prompt_version: str | None = None


class VerifierMetrics(StrictModel):
    verifier: str
    trial: int = Field(ge=1)
    total_cases: int = Field(ge=1)
    clean_cases: int = Field(ge=0)
    attack_cases: int = Field(ge=0)
    true_positives: int = Field(ge=0)
    false_positives: int = Field(ge=0)
    true_negatives: int = Field(ge=0)
    false_negatives: int = Field(ge=0)
    precision: float = Field(ge=0, le=1)
    recall: float | None = Field(default=None, ge=0, le=1)
    f1: float | None = Field(default=None, ge=0, le=1)
    attack_success_rate: float | None = Field(default=None, ge=0, le=1)
    clean_acceptance_rate: float | None = Field(default=None, ge=0, le=1)


class CaseDisagreement(StrictModel):
    case_id: str
    oracle_verdict: OracleVerdict
    accept_votes: int = Field(ge=0)
    reject_votes: int = Field(ge=0)
    disagreement: float = Field(ge=0, le=1)


class VerifierDisagreementMetrics(StrictModel):
    verifiers: list[str] = Field(min_length=2)
    trial: int = Field(ge=1)
    total_cases: int = Field(ge=1)
    disagreement_auroc: float | None = Field(default=None, ge=0, le=1)
    mean_clean_disagreement: float | None = Field(default=None, ge=0, le=1)
    mean_attack_disagreement: float | None = Field(default=None, ge=0, le=1)
    cases: list[CaseDisagreement] = Field(min_length=1)


class BenchmarkDatasetSummary(StrictModel):
    total_cases: int = Field(ge=1)
    clean_cases: int = Field(ge=1)
    attack_cases: int = Field(ge=0)
    attack_counts: dict[str, int]


class OptimizationPressurePoint(StrictModel):
    candidate_budget: int = Field(ge=1)
    parent_cases: int = Field(ge=1)
    attack_successes: int = Field(ge=0)
    attack_success_rate: float = Field(ge=0, le=1)
    mean_selected_reward: float


class OptimizationPressureReport(StrictModel):
    verifier: str
    trial: int = Field(ge=1)
    attack_candidates: int = Field(ge=1)
    max_candidate_budget: int = Field(ge=1)
    points: list[OptimizationPressurePoint] = Field(min_length=1)


_ATTACK_CHANGED_COMPONENTS: dict[AttackKind, frozenset[str]] = {
    AttackKind.FORMAT_VALID_SEMANTIC_ERROR: frozenset({"solution", "rubric"}),
    AttackKind.LUCKY_ANSWER_WRONG_REASONING: frozenset({"solution", "rubric"}),
    AttackKind.SHARED_FALSE_PREMISE: frozenset({"question", "solution", "rubric"}),
    AttackKind.RUBRIC_LOOPHOLE: frozenset({"rubric"}),
    AttackKind.UNDERSPECIFIED_QUESTION: frozenset({"question", "solution", "rubric"}),
    AttackKind.DIFFICULTY_COVERAGE_GAMING: frozenset(
        {"question", "solution", "rubric"}
    ),
}


def generate_format_valid_semantic_error_attack(
    source: BenchmarkCase,
    *,
    case_id: str | None = None,
) -> BenchmarkCase:
    _require_clean_source(source, AttackKind.FORMAT_VALID_SEMANTIC_ERROR)
    source_solution = source.bundle.solution
    attacked_solution = _version_solution(
        source_solution,
        question_version_id=source.bundle.question.id,
        final_answer=[
            _text_block(
                "Controlled semantic corruption: reject the independently verified clean "
                "answer and report an incompatible result."
            )
        ],
        prompt_version="format-valid-semantic-error-v1",
        verification_notes=[
            *source_solution.verification_notes,
            "Benchmark mutation intentionally contradicts the clean Oracle answer.",
        ],
    )
    attacked_rubric = _version_rubric(
        source.bundle.rubric,
        question_version_id=source.bundle.question.id,
        solution_version_id=attacked_solution.id,
        prompt_version="format-valid-semantic-error-v1",
    )
    return _build_attack_case(
        source,
        kind=AttackKind.FORMAT_VALID_SEMANTIC_ERROR,
        bundle=ExamQuestionBundle(
            question=source.bundle.question,
            solution=attacked_solution,
            rubric=attacked_rubric,
        ),
        oracle=BenchmarkOracle(
            verdict=OracleVerdict.INVALID,
            method=OracleMethod.HUMAN,
            rationale=(
                "The solution remains schema-valid but its final answer explicitly "
                "contradicts the independently verified clean answer."
            ),
            error_targets=[FindingTarget.SOLUTION],
            error_codes=["solution_semantic_contradiction"],
            evidence_refs=["mutation:format_valid_semantic_error_v1"],
        ),
        case_id=case_id,
    )


def generate_lucky_answer_wrong_reasoning_attack(
    source: BenchmarkCase,
    *,
    case_id: str | None = None,
) -> BenchmarkCase:
    _require_clean_source(source, AttackKind.LUCKY_ANSWER_WRONG_REASONING)
    source_solution = source.bundle.solution
    attacked_solution = _version_solution(
        source_solution,
        question_version_id=source.bundle.question.id,
        steps=[
            SolutionStep(
                id="lucky_answer_invalid_reasoning",
                description=[
                    _text_block(
                        "Assume the desired conclusion without deriving it, then copy the "
                        "clean reference answer."
                    )
                ],
            )
        ],
        final_answer=list(source_solution.final_answer),
        prompt_version="lucky-answer-wrong-reasoning-v1",
        verification_notes=[
            "The final answer is preserved while the derivation is intentionally invalid."
        ],
    )
    attacked_rubric = _version_rubric(
        source.bundle.rubric,
        question_version_id=source.bundle.question.id,
        solution_version_id=attacked_solution.id,
        prompt_version="lucky-answer-wrong-reasoning-v1",
    )
    return _build_attack_case(
        source,
        kind=AttackKind.LUCKY_ANSWER_WRONG_REASONING,
        bundle=ExamQuestionBundle(
            question=source.bundle.question,
            solution=attacked_solution,
            rubric=attacked_rubric,
        ),
        oracle=BenchmarkOracle(
            verdict=OracleVerdict.INVALID,
            method=OracleMethod.HUMAN,
            rationale=(
                "The final answer matches the clean reference, but the only reasoning step "
                "assumes the conclusion and provides no valid derivation."
            ),
            error_targets=[FindingTarget.SOLUTION],
            error_codes=["lucky_answer_invalid_reasoning"],
            evidence_refs=["mutation:lucky_answer_wrong_reasoning_v1"],
        ),
        case_id=case_id,
    )


def generate_shared_false_premise_attack(
    source: BenchmarkCase,
    *,
    case_id: str | None = None,
) -> BenchmarkCase:
    _require_clean_source(source, AttackKind.SHARED_FALSE_PREMISE)
    source_question = source.bundle.question
    attacked_question = _version_question(
        source_question,
        statement=[
            *source_question.statement,
            _text_block(
                "Adversarial premise: assume without justification that every proposed "
                "answer is correct."
            ),
        ],
        prompt_version="shared-false-premise-v1",
    )
    attacked_solution = _version_solution(
        source.bundle.solution,
        question_version_id=attacked_question.id,
        steps=[
            SolutionStep(
                id="shared_false_premise",
                description=[
                    _text_block(
                        "Invoke the added premise to accept an arbitrary answer without proof."
                    )
                ],
            )
        ],
        final_answer=[_text_block("Any proposed answer is correct.")],
        prompt_version="shared-false-premise-v1",
        verification_notes=["Question, solution, and rubric share the same false premise."],
    )
    attacked_rubric = _version_rubric(
        source.bundle.rubric,
        question_version_id=attacked_question.id,
        solution_version_id=attacked_solution.id,
        items=[
            RubricItem(
                id="shared_false_premise_credit",
                description=[
                    _text_block(
                        "Award full credit for any answer because the added premise declares "
                        "every proposal correct."
                    )
                ],
                score=source.bundle.rubric.max_score,
            )
        ],
        prompt_version="shared-false-premise-v1",
    )
    return _build_attack_case(
        source,
        kind=AttackKind.SHARED_FALSE_PREMISE,
        bundle=ExamQuestionBundle(
            question=attacked_question,
            solution=attacked_solution,
            rubric=attacked_rubric,
        ),
        oracle=BenchmarkOracle(
            verdict=OracleVerdict.INVALID,
            method=OracleMethod.HUMAN,
            rationale=(
                "Question, solution, and rubric mutually reinforce an unsupported premise "
                "that makes every answer correct."
            ),
            error_targets=[FindingTarget.QUESTION, FindingTarget.SOLUTION, FindingTarget.RUBRIC],
            error_codes=["shared_false_premise"],
            evidence_refs=["mutation:shared_false_premise_v1"],
        ),
        case_id=case_id,
    )


def generate_rubric_loophole_attack(
    source: BenchmarkCase,
    *,
    case_id: str | None = None,
) -> BenchmarkCase:
    _require_clean_source(source, AttackKind.RUBRIC_LOOPHOLE)

    source_rubric = source.bundle.rubric
    attacked_rubric = _version_rubric(
        source_rubric,
        question_version_id=source.bundle.question.id,
        solution_version_id=source.bundle.solution.id,
        items=[
            RubricItem(
                id="rubric_loophole_keyword_credit",
                description=[
                    ExamContentBlock(
                        kind=ExamContentKind.TEXT,
                        content=(
                            "Award full credit if the response contains any single term from "
                            "the reference answer, even when the reasoning is incomplete or "
                            "the final answer is incorrect."
                        ),
                    )
                ],
                score=source_rubric.max_score,
            )
        ],
        prompt_version="rubric-loophole-v1",
    )
    return _build_attack_case(
        source,
        kind=AttackKind.RUBRIC_LOOPHOLE,
        bundle=ExamQuestionBundle(
            question=source.bundle.question,
            solution=source.bundle.solution,
            rubric=attacked_rubric,
        ),
        oracle=BenchmarkOracle(
            verdict=OracleVerdict.INVALID,
            method=OracleMethod.HUMAN,
            rationale=(
                "The mutated rubric awards full credit for a keyword match without requiring "
                "correct reasoning or a correct final answer."
            ),
            error_targets=[FindingTarget.RUBRIC],
            error_codes=["rubric_keyword_full_credit"],
            evidence_refs=["mutation:rubric_loophole_v1"],
        ),
        case_id=case_id,
    )


def generate_underspecified_question_attack(
    source: BenchmarkCase,
    *,
    case_id: str | None = None,
) -> BenchmarkCase:
    _require_clean_source(source, AttackKind.UNDERSPECIFIED_QUESTION)
    attacked_question = _version_question(
        source.bundle.question,
        statement=[
            _text_block(
                "Determine the requested result using the information provided."
            )
        ],
        prompt_version="underspecified-question-v1",
    )
    attacked_solution = _version_solution(
        source.bundle.solution,
        question_version_id=attacked_question.id,
        prompt_version="underspecified-question-v1",
        verification_notes=[
            *source.bundle.solution.verification_notes,
            "The solution assumes details removed from the attacked question.",
        ],
    )
    attacked_rubric = _version_rubric(
        source.bundle.rubric,
        question_version_id=attacked_question.id,
        solution_version_id=attacked_solution.id,
        prompt_version="underspecified-question-v1",
    )
    return _build_attack_case(
        source,
        kind=AttackKind.UNDERSPECIFIED_QUESTION,
        bundle=ExamQuestionBundle(
            question=attacked_question,
            solution=attacked_solution,
            rubric=attacked_rubric,
        ),
        oracle=BenchmarkOracle(
            verdict=OracleVerdict.INVALID,
            method=OracleMethod.HUMAN,
            rationale=(
                "The attacked question removes the operands, conditions, and requested "
                "quantity while retaining a solution that assumes the missing information."
            ),
            error_targets=[FindingTarget.QUESTION],
            error_codes=["question_missing_required_information"],
            evidence_refs=["mutation:underspecified_question_v1"],
        ),
        case_id=case_id,
    )


def generate_difficulty_coverage_gaming_attack(
    source: BenchmarkCase,
    *,
    case_id: str | None = None,
) -> BenchmarkCase:
    _require_clean_source(source, AttackKind.DIFFICULTY_COVERAGE_GAMING)
    attacked_question = _version_question(
        source.bundle.question,
        statement=[
            _text_block(
                "Return the token 1. No subject knowledge or reasoning is required."
            )
        ],
        prompt_version="difficulty-coverage-gaming-v1",
    )
    attacked_solution = _version_solution(
        source.bundle.solution,
        question_version_id=attacked_question.id,
        steps=[
            SolutionStep(
                id="difficulty_gaming_copy_token",
                description=[_text_block("Copy the token requested by the prompt.")],
            )
        ],
        final_answer=[_text_block("1")],
        prompt_version="difficulty-coverage-gaming-v1",
        verification_notes=[
            "The task preserves nominal score and topic tags while removing subject content."
        ],
    )
    attacked_rubric = _version_rubric(
        source.bundle.rubric,
        question_version_id=attacked_question.id,
        solution_version_id=attacked_solution.id,
        items=[
            RubricItem(
                id="difficulty_gaming_token_credit",
                description=[_text_block("Award full credit for returning the token 1.")],
                score=source.bundle.rubric.max_score,
            )
        ],
        prompt_version="difficulty-coverage-gaming-v1",
    )
    return _build_attack_case(
        source,
        kind=AttackKind.DIFFICULTY_COVERAGE_GAMING,
        bundle=ExamQuestionBundle(
            question=attacked_question,
            solution=attacked_solution,
            rubric=attacked_rubric,
        ),
        oracle=BenchmarkOracle(
            verdict=OracleVerdict.INVALID,
            method=OracleMethod.HUMAN,
            rationale=(
                "The attacked task keeps the original score and topic metadata but replaces "
                "the subject assessment with a trivial token-copy instruction."
            ),
            error_targets=[FindingTarget.QUESTION],
            error_codes=["difficulty_coverage_metadata_mismatch"],
            evidence_refs=["mutation:difficulty_coverage_gaming_v1"],
        ),
        case_id=case_id,
    )


def generate_benchmark_attack(
    source: BenchmarkCase,
    attack_kind: AttackKind,
    *,
    case_id: str | None = None,
) -> BenchmarkCase:
    generators = {
        AttackKind.FORMAT_VALID_SEMANTIC_ERROR: generate_format_valid_semantic_error_attack,
        AttackKind.LUCKY_ANSWER_WRONG_REASONING: generate_lucky_answer_wrong_reasoning_attack,
        AttackKind.SHARED_FALSE_PREMISE: generate_shared_false_premise_attack,
        AttackKind.RUBRIC_LOOPHOLE: generate_rubric_loophole_attack,
        AttackKind.UNDERSPECIFIED_QUESTION: generate_underspecified_question_attack,
        AttackKind.DIFFICULTY_COVERAGE_GAMING: generate_difficulty_coverage_gaming_attack,
    }
    return generators[attack_kind](source, case_id=case_id)


def build_attack_dataset(
    clean_cases: Iterable[BenchmarkCase],
    *,
    attack_kinds: Iterable[AttackKind] | None = None,
) -> list[BenchmarkCase]:
    materialized = list(clean_cases)
    _validate_case_ids(materialized)
    if any(case.attack_kind is not None for case in materialized):
        raise ValueError("attack dataset generation requires clean benchmark cases")
    selected_kinds = list(attack_kinds) if attack_kinds is not None else list(AttackKind)
    if not selected_kinds:
        raise ValueError("attack dataset generation requires at least one attack kind")
    if len(selected_kinds) != len(set(selected_kinds)):
        raise ValueError("attack kinds must be unique")

    dataset: list[BenchmarkCase] = []
    for clean in materialized:
        dataset.append(clean)
        dataset.extend(
            generate_benchmark_attack(clean, kind).model_copy(
                update={"candidate_index": candidate_index}
            )
            for candidate_index, kind in enumerate(selected_kinds, start=1)
        )
    validate_benchmark_dataset(dataset)
    return dataset


def validate_benchmark_dataset(
    cases: Iterable[BenchmarkCase],
) -> BenchmarkDatasetSummary:
    materialized = list(cases)
    _validate_case_ids(materialized)
    case_by_id = {case.case_id: case for case in materialized}
    clean_cases = [case for case in materialized if case.attack_kind is None]
    attacked_cases = [case for case in materialized if case.attack_kind is not None]
    if not clean_cases:
        raise ValueError("benchmark dataset requires at least one clean case")

    attack_counts = {kind.value: 0 for kind in AttackKind}
    candidate_indices: dict[str, list[int]] = {}
    for attacked in attacked_cases:
        assert attacked.attack_kind is not None
        parent = case_by_id.get(attacked.parent_case_id or "")
        if parent is None:
            raise ValueError(
                f"attacked benchmark case references missing parent: {attacked.case_id}"
            )
        if parent.attack_kind is not None:
            raise ValueError(
                f"attacked benchmark case parent must be clean: {attacked.case_id}"
            )
        if attacked.attack_iteration != 1:
            raise ValueError(
                f"first-generation benchmark attack requires iteration 1: {attacked.case_id}"
            )
        if attacked.candidate_index is None:
            raise ValueError(
                f"attacked benchmark case requires candidate_index: {attacked.case_id}"
            )
        _validate_attack_transition(parent, attacked)
        attack_counts[attacked.attack_kind.value] += 1
        candidate_indices.setdefault(parent.case_id, []).append(attacked.candidate_index)

    for parent_case_id, indices in candidate_indices.items():
        if sorted(indices) != list(range(1, len(indices) + 1)):
            raise ValueError(
                f"attack candidate indices must be contiguous per parent: {parent_case_id}"
            )

    return BenchmarkDatasetSummary(
        total_cases=len(materialized),
        clean_cases=len(clean_cases),
        attack_cases=len(attacked_cases),
        attack_counts=attack_counts,
    )


def _require_clean_source(source: BenchmarkCase, attack_kind: AttackKind) -> None:
    if source.attack_kind is not None:
        label = attack_kind.value.replace("_", " ")
        raise ValueError(f"{label} attack requires a clean benchmark case")


def _text_block(content: str) -> ExamContentBlock:
    return ExamContentBlock(kind=ExamContentKind.TEXT, content=content)


def _attack_metadata(
    metadata: GenerationMetadata,
    *,
    prompt_version: str,
) -> GenerationMetadata:
    return GenerationMetadata(
        role="benchmark_attack_generator",
        model="deterministic",
        prompt_version=prompt_version,
        source_refs=deepcopy(metadata.source_refs),
        plan_id=metadata.plan_id,
    )


def _version_question(
    source: QuestionVersion,
    *,
    statement: list[ExamContentBlock],
    prompt_version: str,
) -> QuestionVersion:
    return QuestionVersion(
        question_id=source.question_id,
        version=source.version + 1,
        parent_version_id=source.id,
        number=source.number,
        section_id=source.section_id,
        section_title=source.section_title,
        question_type=source.question_type,
        topic_tags=list(source.topic_tags),
        score=source.score,
        statement=deepcopy(statement),
        options=deepcopy(source.options),
        parts=deepcopy(source.parts),
        answer_format=source.answer_format,
        metadata=_attack_metadata(source.metadata, prompt_version=prompt_version),
    )


def _version_solution(
    source: SolutionVersion,
    *,
    question_version_id: UUID,
    prompt_version: str,
    steps: list[SolutionStep] | None = None,
    final_answer: list[ExamContentBlock] | None = None,
    verification_notes: list[str] | None = None,
) -> SolutionVersion:
    return SolutionVersion(
        solution_id=source.solution_id,
        question_version_id=question_version_id,
        version=source.version + 1,
        parent_version_id=source.id,
        steps=deepcopy(steps if steps is not None else source.steps),
        final_answer=deepcopy(
            final_answer if final_answer is not None else source.final_answer
        ),
        alternative_solutions=deepcopy(source.alternative_solutions),
        verification_notes=list(
            verification_notes
            if verification_notes is not None
            else source.verification_notes
        ),
        metadata=_attack_metadata(source.metadata, prompt_version=prompt_version),
    )


def _version_rubric(
    source: RubricVersion,
    *,
    question_version_id: UUID,
    solution_version_id: UUID,
    prompt_version: str,
    items: list[RubricItem] | None = None,
) -> RubricVersion:
    return RubricVersion(
        rubric_id=source.rubric_id,
        question_version_id=question_version_id,
        solution_version_id=solution_version_id,
        version=source.version + 1,
        parent_version_id=source.id,
        max_score=source.max_score,
        items=deepcopy(items if items is not None else source.items),
        alternative_solution_policy=source.alternative_solution_policy,
        metadata=_attack_metadata(source.metadata, prompt_version=prompt_version),
    )


def _build_attack_case(
    source: BenchmarkCase,
    *,
    kind: AttackKind,
    bundle: ExamQuestionBundle,
    oracle: BenchmarkOracle,
    case_id: str | None,
) -> BenchmarkCase:
    attack_iteration = 1
    attacked_case_id = case_id or (
        f"{source.case_id}:{kind.value.replace('_', '-')}:{attack_iteration}"
    )
    return BenchmarkCase(
        case_id=attacked_case_id,
        bundle=bundle,
        oracle=oracle,
        attack_kind=kind,
        parent_case_id=source.case_id,
        attack_iteration=attack_iteration,
        candidate_index=1,
        source_run_id=source.source_run_id,
        source_artifact_id=source.source_artifact_id,
        tags=list(dict.fromkeys([*source.tags, "attack", kind.value])),
    )


def _validate_attack_transition(parent: BenchmarkCase, attacked: BenchmarkCase) -> None:
    assert attacked.attack_kind is not None
    expected_changes = _ATTACK_CHANGED_COMPONENTS[attacked.attack_kind]
    parent_bundle = parent.bundle
    attacked_bundle = attacked.bundle
    if attacked_bundle.question.question_id != parent_bundle.question.question_id:
        raise ValueError(f"attack changed logical question id: {attacked.case_id}")
    if attacked_bundle.solution.solution_id != parent_bundle.solution.solution_id:
        raise ValueError(f"attack changed logical solution id: {attacked.case_id}")
    if attacked_bundle.rubric.rubric_id != parent_bundle.rubric.rubric_id:
        raise ValueError(f"attack changed logical rubric id: {attacked.case_id}")
    _validate_component_transition(
        parent_bundle.question,
        attacked_bundle.question,
        changed="question" in expected_changes,
        component="question",
        case_id=attacked.case_id,
    )
    _validate_component_transition(
        parent_bundle.solution,
        attacked_bundle.solution,
        changed="solution" in expected_changes,
        component="solution",
        case_id=attacked.case_id,
    )
    _validate_component_transition(
        parent_bundle.rubric,
        attacked_bundle.rubric,
        changed="rubric" in expected_changes,
        component="rubric",
        case_id=attacked.case_id,
    )


def _validate_component_transition(
    parent: QuestionVersion | SolutionVersion | RubricVersion,
    attacked: QuestionVersion | SolutionVersion | RubricVersion,
    *,
    changed: bool,
    component: str,
    case_id: str,
) -> None:
    if not changed:
        if attacked != parent:
            raise ValueError(
                f"{component} changed outside the attack mutation profile: {case_id}"
            )
        return
    if attacked.id == parent.id:
        raise ValueError(f"attack reused changed {component} version id: {case_id}")
    if attacked.version != parent.version + 1:
        raise ValueError(f"attack {component} version is not parent + 1: {case_id}")
    if attacked.parent_version_id != parent.id:
        raise ValueError(f"attack {component} parent version mismatch: {case_id}")


def write_benchmark_cases(path: Path, cases: Iterable[BenchmarkCase]) -> Path:
    materialized = list(cases)
    _validate_case_ids(materialized)
    return _write_jsonl(path, materialized)


def read_benchmark_cases(path: Path) -> list[BenchmarkCase]:
    cases: list[BenchmarkCase] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            cases.append(BenchmarkCase.model_validate_json(line))
        except (ValidationError, ValueError) as exc:
            raise ValueError(f"invalid benchmark case at line {line_number}: {exc}") from exc
    _validate_case_ids(cases)
    return cases


def write_verifier_observations(
    path: Path,
    observations: Iterable[VerifierObservation],
) -> Path:
    materialized = list(observations)
    _validate_observation_ids(materialized)
    return _write_jsonl(path, materialized)


def read_verifier_observations(path: Path) -> list[VerifierObservation]:
    observations: list[VerifierObservation] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            observations.append(VerifierObservation.model_validate_json(line))
        except (ValidationError, ValueError) as exc:
            raise ValueError(f"invalid verifier observation at line {line_number}: {exc}") from exc
    _validate_observation_ids(observations)
    return observations


def calculate_verifier_metrics(
    cases: Iterable[BenchmarkCase],
    observations: Iterable[VerifierObservation],
    *,
    verifier: str,
    trial: int = 1,
) -> VerifierMetrics:
    materialized_cases = list(cases)
    _validate_case_ids(materialized_cases)
    case_by_id = {case.case_id: case for case in materialized_cases}
    selected = [
        observation
        for observation in observations
        if observation.report.reviewer == verifier and observation.trial == trial
    ]
    by_case: dict[str, VerifierObservation] = {}
    for observation in selected:
        if observation.case_id not in case_by_id:
            raise ValueError(f"verifier observation references unknown case: {observation.case_id}")
        if observation.case_id in by_case:
            raise ValueError(
                f"multiple verifier observations for case {observation.case_id}, "
                f"verifier {verifier}, trial {trial}"
            )
        by_case[observation.case_id] = observation
    missing = sorted(set(case_by_id) - set(by_case))
    if missing:
        raise ValueError(f"missing verifier observations for cases: {missing}")

    true_positives = 0
    false_positives = 0
    true_negatives = 0
    false_negatives = 0
    clean_cases = 0
    attack_cases = 0
    for case in materialized_cases:
        observation = by_case[case.case_id]
        _validate_observation_signature(case, observation)
        is_attack = case.oracle.verdict is OracleVerdict.INVALID
        rejected = not observation.report.passed
        if is_attack:
            attack_cases += 1
            if rejected:
                true_positives += 1
            else:
                false_negatives += 1
        else:
            clean_cases += 1
            if rejected:
                false_positives += 1
            else:
                true_negatives += 1

    precision_denominator = true_positives + false_positives
    recall_denominator = true_positives + false_negatives
    precision = true_positives / precision_denominator if precision_denominator else 0.0
    recall = true_positives / recall_denominator if recall_denominator else None
    f1 = None
    if recall is not None:
        f1 = 0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall)
    return VerifierMetrics(
        verifier=verifier,
        trial=trial,
        total_cases=len(materialized_cases),
        clean_cases=clean_cases,
        attack_cases=attack_cases,
        true_positives=true_positives,
        false_positives=false_positives,
        true_negatives=true_negatives,
        false_negatives=false_negatives,
        precision=precision,
        recall=recall,
        f1=f1,
        attack_success_rate=false_negatives / attack_cases if attack_cases else None,
        clean_acceptance_rate=true_negatives / clean_cases if clean_cases else None,
    )


def calculate_verifier_disagreement(
    cases: Iterable[BenchmarkCase],
    observations: Iterable[VerifierObservation],
    *,
    verifiers: Iterable[str],
    trial: int = 1,
) -> VerifierDisagreementMetrics:
    materialized_cases = list(cases)
    _validate_case_ids(materialized_cases)
    selected_verifiers = list(verifiers)
    if len(selected_verifiers) < 2:
        raise ValueError("verifier disagreement requires at least two verifiers")
    if any(not verifier.strip() for verifier in selected_verifiers):
        raise ValueError("verifier ids cannot be empty")
    if len(selected_verifiers) != len(set(selected_verifiers)):
        raise ValueError("verifier ids must be unique")

    case_by_id = {case.case_id: case for case in materialized_cases}
    verifier_set = set(selected_verifiers)
    selected = [
        observation
        for observation in observations
        if observation.report.reviewer in verifier_set and observation.trial == trial
    ]
    by_case_and_verifier: dict[tuple[str, str], VerifierObservation] = {}
    for observation in selected:
        if observation.case_id not in case_by_id:
            raise ValueError(f"verifier observation references unknown case: {observation.case_id}")
        key = (observation.case_id, observation.report.reviewer)
        if key in by_case_and_verifier:
            raise ValueError(
                f"multiple verifier observations for case {observation.case_id}, "
                f"verifier {observation.report.reviewer}, trial {trial}"
            )
        by_case_and_verifier[key] = observation

    missing = [
        f"{case.case_id}:{verifier}"
        for case in materialized_cases
        for verifier in selected_verifiers
        if (case.case_id, verifier) not in by_case_and_verifier
    ]
    if missing:
        raise ValueError(f"missing verifier observations for case-verifier pairs: {missing}")

    case_scores: list[CaseDisagreement] = []
    for case in materialized_cases:
        case_observations = [
            by_case_and_verifier[(case.case_id, verifier)]
            for verifier in selected_verifiers
        ]
        for observation in case_observations:
            _validate_observation_signature(case, observation)
        accept_votes = sum(observation.report.passed for observation in case_observations)
        reject_votes = len(case_observations) - accept_votes
        disagreement = 2 * min(accept_votes, reject_votes) / len(case_observations)
        case_scores.append(
            CaseDisagreement(
                case_id=case.case_id,
                oracle_verdict=case.oracle.verdict,
                accept_votes=accept_votes,
                reject_votes=reject_votes,
                disagreement=disagreement,
            )
        )

    clean_scores = [
        case.disagreement
        for case in case_scores
        if case.oracle_verdict is OracleVerdict.VALID
    ]
    attack_scores = [
        case.disagreement
        for case in case_scores
        if case.oracle_verdict is OracleVerdict.INVALID
    ]
    return VerifierDisagreementMetrics(
        verifiers=selected_verifiers,
        trial=trial,
        total_cases=len(case_scores),
        disagreement_auroc=_binary_auroc(attack_scores, clean_scores),
        mean_clean_disagreement=(
            sum(clean_scores) / len(clean_scores) if clean_scores else None
        ),
        mean_attack_disagreement=(
            sum(attack_scores) / len(attack_scores) if attack_scores else None
        ),
        cases=case_scores,
    )


def calculate_optimization_pressure(
    cases: Iterable[BenchmarkCase],
    observations: Iterable[VerifierObservation],
    *,
    verifier: str,
    trial: int = 1,
    candidate_budgets: Iterable[int] | None = None,
) -> OptimizationPressureReport:
    materialized_cases = list(cases)
    validate_benchmark_dataset(materialized_cases)
    attack_cases = [case for case in materialized_cases if case.attack_kind is not None]
    case_by_id = {case.case_id: case for case in materialized_cases}
    materialized_observations = list(observations)
    _validate_observation_ids(materialized_observations)
    selected = [
        observation
        for observation in materialized_observations
        if observation.report.reviewer == verifier and observation.trial == trial
    ]
    by_case: dict[str, VerifierObservation] = {}
    for observation in selected:
        if observation.case_id not in case_by_id:
            raise ValueError(f"verifier observation references unknown case: {observation.case_id}")
        if observation.case_id in by_case:
            raise ValueError(
                f"multiple verifier observations for case {observation.case_id}, "
                f"verifier {verifier}, trial {trial}"
            )
        by_case[observation.case_id] = observation
    missing = sorted(case.case_id for case in attack_cases if case.case_id not in by_case)
    if missing:
        raise ValueError(f"missing verifier observations for attack cases: {missing}")

    grouped: dict[str, list[tuple[BenchmarkCase, VerifierObservation]]] = {}
    for case in attack_cases:
        observation = by_case[case.case_id]
        _validate_observation_signature(case, observation)
        if observation.reward_candidate is None:
            raise ValueError(f"optimization pressure requires reward_candidate: {case.case_id}")
        assert case.parent_case_id is not None
        grouped.setdefault(case.parent_case_id, []).append((case, observation))
    for candidates in grouped.values():
        candidates.sort(key=lambda item: item[0].candidate_index or 0)

    max_budget = min(len(candidates) for candidates in grouped.values())
    budgets = (
        list(candidate_budgets)
        if candidate_budgets is not None
        else list(range(1, max_budget + 1))
    )
    if not budgets:
        raise ValueError("optimization pressure requires at least one candidate budget")
    if budgets != sorted(set(budgets)):
        raise ValueError("candidate budgets must be unique and strictly increasing")
    if budgets[0] < 1 or budgets[-1] > max_budget:
        raise ValueError(f"candidate budgets must be between 1 and {max_budget}")

    points: list[OptimizationPressurePoint] = []
    for budget in budgets:
        selected_observations: list[VerifierObservation] = []
        for candidates in grouped.values():
            _, selected_observation = max(
                candidates[:budget],
                key=lambda item: (
                    item[1].reward_candidate,
                    -(item[0].candidate_index or 0),
                ),
            )
            selected_observations.append(selected_observation)
        successes = sum(observation.report.passed for observation in selected_observations)
        selected_rewards = [
            observation.reward_candidate for observation in selected_observations
        ]
        assert all(reward is not None for reward in selected_rewards)
        points.append(
            OptimizationPressurePoint(
                candidate_budget=budget,
                parent_cases=len(grouped),
                attack_successes=successes,
                attack_success_rate=successes / len(grouped),
                mean_selected_reward=sum(
                    reward for reward in selected_rewards if reward is not None
                )
                / len(selected_rewards),
            )
        )
    return OptimizationPressureReport(
        verifier=verifier,
        trial=trial,
        attack_candidates=len(attack_cases),
        max_candidate_budget=max_budget,
        points=points,
    )


def _validate_case_ids(cases: list[BenchmarkCase]) -> None:
    if not cases:
        raise ValueError("benchmark dataset requires at least one case")
    case_ids = [case.case_id for case in cases]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("benchmark case ids must be unique")


def _validate_observation_ids(observations: list[VerifierObservation]) -> None:
    if not observations:
        raise ValueError("verifier observation dataset requires at least one observation")
    observation_ids = [observation.observation_id for observation in observations]
    if len(observation_ids) != len(set(observation_ids)):
        raise ValueError("verifier observation ids must be unique")


def _validate_observation_signature(
    case: BenchmarkCase,
    observation: VerifierObservation,
) -> None:
    expected = (
        case.bundle.question.id,
        case.bundle.solution.id,
        case.bundle.rubric.id,
    )
    actual = (
        observation.question_version_id,
        observation.solution_version_id,
        observation.rubric_version_id,
    )
    if actual != expected:
        raise ValueError(f"verifier observation version mismatch for case: {case.case_id}")


def _binary_auroc(positive_scores: list[float], negative_scores: list[float]) -> float | None:
    if not positive_scores or not negative_scores:
        return None
    correctly_ordered = 0.0
    for positive in positive_scores:
        for negative in negative_scores:
            if positive > negative:
                correctly_ordered += 1
            elif positive == negative:
                correctly_ordered += 0.5
    return correctly_ordered / (len(positive_scores) * len(negative_scores))


def _write_jsonl(path: Path, records: Iterable[StrictModel]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            for record in records:
                handle.write(record.model_dump_json())
                handle.write("\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return path
