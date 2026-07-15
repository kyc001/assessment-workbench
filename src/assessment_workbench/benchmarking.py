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
