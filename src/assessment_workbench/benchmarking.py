from collections.abc import Iterable
from enum import StrEnum
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import Field, ValidationError, model_validator

from assessment_workbench.domain import ExamQuestionBundle, FindingTarget, StrictModel

BENCHMARK_CASE_SCHEMA_VERSION: Literal["benchmark-case-v1"] = "benchmark-case-v1"


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


def write_benchmark_cases(path: Path, cases: Iterable[BenchmarkCase]) -> Path:
    materialized = list(cases)
    _validate_case_ids(materialized)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            for case in materialized:
                handle.write(case.model_dump_json())
                handle.write("\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


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


def _validate_case_ids(cases: list[BenchmarkCase]) -> None:
    if not cases:
        raise ValueError("benchmark dataset requires at least one case")
    case_ids = [case.case_id for case in cases]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("benchmark case ids must be unique")
