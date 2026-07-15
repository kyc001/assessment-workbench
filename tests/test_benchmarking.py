import json
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from assessment_workbench.benchmarking import (
    AttackKind,
    BenchmarkCase,
    BenchmarkOracle,
    OracleMethod,
    OracleVerdict,
    VerifierObservation,
    calculate_verifier_metrics,
    generate_rubric_loophole_attack,
    read_benchmark_cases,
    read_verifier_observations,
    write_benchmark_cases,
    write_verifier_observations,
)
from assessment_workbench.cli import app
from assessment_workbench.domain import (
    ExamQuestionBundle,
    FindingSeverity,
    FindingTarget,
    GenerationMetadata,
    QuestionType,
    QuestionVersion,
    ReviewFinding,
    ReviewReport,
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


def _attack_case(case_id: str, parent_case_id: str) -> BenchmarkCase:
    return BenchmarkCase(
        case_id=case_id,
        bundle=_bundle(),
        oracle=BenchmarkOracle(
            verdict=OracleVerdict.INVALID,
            method=OracleMethod.HYBRID,
            rationale="Symbolic checking and human review identify a rubric loophole.",
            error_targets=[FindingTarget.RUBRIC],
            error_codes=["incomplete_full_credit"],
        ),
        attack_kind=AttackKind.RUBRIC_LOOPHOLE,
        parent_case_id=parent_case_id,
        attack_iteration=1,
        tags=["algebra", "attack"],
    )


def _observation(
    case: BenchmarkCase,
    *,
    observation_id: str,
    passed: bool,
    verifier: str = "specialized_ensemble",
) -> VerifierObservation:
    findings = []
    if not passed:
        findings = [
            ReviewFinding(
                code="detected_invalid_bundle",
                severity=FindingSeverity.ERROR,
                target=FindingTarget.BUNDLE,
                message="The bundle violates the independent oracle contract.",
            )
        ]
    return VerifierObservation(
        observation_id=observation_id,
        case_id=case.case_id,
        question_version_id=case.bundle.question.id,
        solution_version_id=case.bundle.solution.id,
        rubric_version_id=case.bundle.rubric.id,
        report=ReviewReport(
            reviewer=verifier,
            passed=passed,
            findings=findings,
        ),
        confidence=0.9,
        model="fixture-verifier",
        prompt_version="fixture-v1",
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


def test_generate_rubric_loophole_attack_versions_only_the_rubric() -> None:
    clean = _clean_case()

    attacked = generate_rubric_loophole_attack(clean)

    assert attacked.bundle.question == clean.bundle.question
    assert attacked.bundle.solution == clean.bundle.solution
    assert attacked.bundle.rubric.id != clean.bundle.rubric.id
    assert attacked.bundle.rubric.rubric_id == clean.bundle.rubric.rubric_id
    assert attacked.bundle.rubric.version == clean.bundle.rubric.version + 1
    assert attacked.bundle.rubric.parent_version_id == clean.bundle.rubric.id


def test_generate_rubric_loophole_attack_creates_full_credit_keyword_rule() -> None:
    attacked = generate_rubric_loophole_attack(_clean_case())

    assert attacked.bundle.rubric.max_score == attacked.bundle.question.score
    assert len(attacked.bundle.rubric.items) == 1
    loophole = attacked.bundle.rubric.items[0]
    assert loophole.score == attacked.bundle.rubric.max_score
    assert "any single term" in loophole.description[0].content
    assert "final answer is incorrect" in loophole.description[0].content


def test_generate_rubric_loophole_attack_records_oracle_and_lineage() -> None:
    clean = _clean_case()

    attacked = generate_rubric_loophole_attack(clean, case_id="algebra-001-loophole")

    assert attacked.case_id == "algebra-001-loophole"
    assert attacked.attack_kind is AttackKind.RUBRIC_LOOPHOLE
    assert attacked.parent_case_id == clean.case_id
    assert attacked.attack_iteration == 1
    assert attacked.oracle.verdict is OracleVerdict.INVALID
    assert attacked.oracle.method is OracleMethod.HUMAN
    assert attacked.oracle.error_targets == [FindingTarget.RUBRIC]
    assert attacked.oracle.error_codes == ["rubric_keyword_full_credit"]
    assert attacked.oracle.evidence_refs == ["mutation:rubric_loophole_v1"]
    assert "rubric_loophole" in attacked.tags


def test_generate_rubric_loophole_attack_does_not_mutate_source() -> None:
    clean = _clean_case()
    snapshot = clean.model_dump_json()

    generate_rubric_loophole_attack(clean)

    assert clean.model_dump_json() == snapshot


def test_generate_rubric_loophole_attack_rejects_attacked_source() -> None:
    attacked = _attack_case("attack-001", "clean-001")

    with pytest.raises(ValueError, match="requires a clean benchmark case"):
        generate_rubric_loophole_attack(attacked)


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


def test_verifier_observation_jsonl_round_trip(tmp_path: Path) -> None:
    case = _clean_case()
    observation = _observation(case, observation_id="obs-001", passed=True)
    path = write_verifier_observations(tmp_path / "observations.jsonl", [observation])

    restored = read_verifier_observations(path)

    assert restored == [observation]


def test_verifier_metrics_measure_detection_and_attack_success() -> None:
    clean_pass = _clean_case("clean-pass")
    clean_reject = _clean_case("clean-reject")
    attack_reject = _attack_case("attack-reject", clean_pass.case_id)
    attack_pass = _attack_case("attack-pass", clean_reject.case_id)
    cases = [clean_pass, clean_reject, attack_reject, attack_pass]
    observations = [
        _observation(clean_pass, observation_id="obs-clean-pass", passed=True),
        _observation(clean_reject, observation_id="obs-clean-reject", passed=False),
        _observation(attack_reject, observation_id="obs-attack-reject", passed=False),
        _observation(attack_pass, observation_id="obs-attack-pass", passed=True),
    ]

    metrics = calculate_verifier_metrics(
        cases,
        observations,
        verifier="specialized_ensemble",
    )

    assert metrics.true_positives == 1
    assert metrics.false_positives == 1
    assert metrics.true_negatives == 1
    assert metrics.false_negatives == 1
    assert metrics.precision == 0.5
    assert metrics.recall == 0.5
    assert metrics.f1 == 0.5
    assert metrics.attack_success_rate == 0.5
    assert metrics.clean_acceptance_rate == 0.5


def test_verifier_metrics_reject_version_mismatch() -> None:
    case = _clean_case()
    observation = _observation(case, observation_id="obs-001", passed=True).model_copy(
        update={"question_version_id": uuid4()}
    )

    with pytest.raises(ValueError, match="version mismatch"):
        calculate_verifier_metrics(
            [case],
            [observation],
            verifier="specialized_ensemble",
        )


def test_verifier_metrics_require_one_observation_per_case() -> None:
    first = _clean_case("clean-001")
    second = _clean_case("clean-002")

    with pytest.raises(ValueError, match="missing verifier observations"):
        calculate_verifier_metrics(
            [first, second],
            [_observation(first, observation_id="obs-001", passed=True)],
            verifier="specialized_ensemble",
        )


def test_benchmark_evaluate_cli_outputs_json_metrics(tmp_path: Path) -> None:
    clean = _clean_case("clean-cli")
    attack = _attack_case("attack-cli", clean.case_id)
    cases_path = write_benchmark_cases(tmp_path / "cases.jsonl", [clean, attack])
    observations_path = write_verifier_observations(
        tmp_path / "observations.jsonl",
        [
            _observation(clean, observation_id="obs-clean-cli", passed=True),
            _observation(attack, observation_id="obs-attack-cli", passed=False),
        ],
    )

    result = CliRunner().invoke(
        app,
        [
            "benchmark",
            "evaluate",
            "--cases",
            str(cases_path),
            "--observations",
            str(observations_path),
            "--verifier",
            "specialized_ensemble",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["precision"] == 1.0
    assert payload["recall"] == 1.0
    assert payload["attack_success_rate"] == 0.0
