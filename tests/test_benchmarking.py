from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from assessment_workbench.benchmarking import (
    AttackKind,
    BenchmarkCase,
    BenchmarkOracle,
    OracleMethod,
    OracleVerdict,
    read_benchmark_cases,
    write_benchmark_cases,
)
from assessment_workbench.domain import (
    ExamQuestionBundle,
    FindingTarget,
    GenerationMetadata,
    QuestionType,
    QuestionVersion,
    RubricItem,
    RubricVersion,
    SolutionStep,
    SolutionVersion,
)


def _bundle() -> ExamQuestionBundle:
    question = QuestionVersion(
        question_id=uuid4(),
        version=1,
        number=1,
        question_type=QuestionType.CALCULATION,
        topic_tags=["algebra"],
        score=10,
        statement="Solve x + 2 = 5.",
        metadata=GenerationMetadata(role="fixture_writer"),
    )
    solution = SolutionVersion(
        solution_id=uuid4(),
        question_version_id=question.id,
        version=1,
        steps=[SolutionStep(id="s1", description="Subtract two from both sides.")],
        final_answer="x = 3",
        metadata=GenerationMetadata(role="fixture_solver"),
    )
    rubric = RubricVersion(
        rubric_id=uuid4(),
        question_version_id=question.id,
        solution_version_id=solution.id,
        version=1,
        max_score=10,
        items=[
            RubricItem(id="r1", description="Uses a valid transformation.", score=6),
            RubricItem(id="r2", description="States x = 3.", score=4, depends_on=["r1"]),
        ],
        metadata=GenerationMetadata(role="fixture_rubric"),
    )
    return ExamQuestionBundle(question=question, solution=solution, rubric=rubric)


def _clean_case(case_id: str = "algebra-001") -> BenchmarkCase:
    return BenchmarkCase(
        case_id=case_id,
        bundle=_bundle(),
        oracle=BenchmarkOracle(
            verdict=OracleVerdict.VALID,
            method=OracleMethod.SYMBOLIC,
            rationale="Substitution verifies the unique solution and the rubric totals ten points.",
            evidence_refs=["sympy:solve-and-substitute"],
        ),
        tags=["algebra", "clean"],
    )


def test_clean_case_requires_independently_valid_oracle() -> None:
    case = _clean_case()
    assert case.attack_kind is None
    assert case.oracle.verdict is OracleVerdict.VALID


def test_invalid_oracle_requires_targeted_error_evidence() -> None:
    with pytest.raises(ValidationError, match="requires error targets and error codes"):
        BenchmarkOracle(
            verdict=OracleVerdict.INVALID,
            method=OracleMethod.HUMAN,
            rationale="The rubric can award full credit for an incomplete answer.",
        )


def test_attacked_case_requires_lineage_and_invalid_oracle() -> None:
    oracle = BenchmarkOracle(
        verdict=OracleVerdict.INVALID,
        method=OracleMethod.HUMAN,
        rationale="The mutated rubric rewards either root instead of the complete solution set.",
        error_targets=[FindingTarget.RUBRIC],
        error_codes=["incomplete_full_credit"],
    )
    with pytest.raises(ValidationError, match="requires parent_case_id"):
        BenchmarkCase(
            case_id="algebra-001-attack-01",
            bundle=_bundle(),
            oracle=oracle,
            attack_kind=AttackKind.RUBRIC_LOOPHOLE,
            attack_iteration=1,
        )


def test_benchmark_jsonl_round_trip(tmp_path: Path) -> None:
    clean = _clean_case()
    attacked = BenchmarkCase(
        case_id="algebra-001-attack-01",
        bundle=_bundle(),
        oracle=BenchmarkOracle(
            verdict=OracleVerdict.INVALID,
            method=OracleMethod.HYBRID,
            rationale="Symbolic checking and human review identify a shared false premise.",
            error_targets=[FindingTarget.QUESTION, FindingTarget.SOLUTION, FindingTarget.RUBRIC],
            error_codes=["shared_false_premise"],
            evidence_refs=["oracle-note:001"],
        ),
        attack_kind=AttackKind.SHARED_FALSE_PREMISE,
        parent_case_id=clean.case_id,
        attack_iteration=1,
        tags=["algebra", "attack"],
    )
    path = write_benchmark_cases(tmp_path / "benchmark.jsonl", [clean, attacked])

    restored = read_benchmark_cases(path)

    assert [case.case_id for case in restored] == [clean.case_id, attacked.case_id]
    assert restored[1].attack_kind is AttackKind.SHARED_FALSE_PREMISE


def test_benchmark_jsonl_rejects_duplicate_case_ids(tmp_path: Path) -> None:
    duplicate = _clean_case()
    with pytest.raises(ValueError, match="case ids must be unique"):
        write_benchmark_cases(tmp_path / "benchmark.jsonl", [duplicate, duplicate])
