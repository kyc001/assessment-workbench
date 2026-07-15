from collections.abc import Iterable
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
    ReviewReport,
    RubricItem,
    RubricVersion,
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
    source_run_id: UUID | None = None
    source_artifact_id: UUID | None = None
    tags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_case_lineage(self) -> "BenchmarkCase":
        if self.attack_kind is None:
            if self.parent_case_id is not None or self.attack_iteration != 0:
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


def generate_rubric_loophole_attack(
    source: BenchmarkCase,
    *,
    case_id: str | None = None,
) -> BenchmarkCase:
    if source.attack_kind is not None:
        raise ValueError("rubric loophole attack requires a clean benchmark case")

    attack_iteration = 1
    attacked_case_id = case_id or (
        f"{source.case_id}:rubric-loophole:{attack_iteration}"
    )
    source_rubric = source.bundle.rubric
    attacked_rubric = RubricVersion(
        rubric_id=source_rubric.rubric_id,
        question_version_id=source.bundle.question.id,
        solution_version_id=source.bundle.solution.id,
        version=source_rubric.version + 1,
        parent_version_id=source_rubric.id,
        max_score=source_rubric.max_score,
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
        alternative_solution_policy=source_rubric.alternative_solution_policy,
        metadata=GenerationMetadata(
            role="benchmark_attack_generator",
            model="deterministic",
            prompt_version="rubric-loophole-v1",
            source_refs=list(source_rubric.metadata.source_refs),
            plan_id=source_rubric.metadata.plan_id,
        ),
    )
    return BenchmarkCase(
        case_id=attacked_case_id,
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
        attack_kind=AttackKind.RUBRIC_LOOPHOLE,
        parent_case_id=source.case_id,
        attack_iteration=attack_iteration,
        source_run_id=source.source_run_id,
        source_artifact_id=source.source_artifact_id,
        tags=list(dict.fromkeys([*source.tags, "attack", "rubric_loophole"])),
    )


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
