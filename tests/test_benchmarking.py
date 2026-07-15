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
    DeterministicBaseline,
    OracleMethod,
    OracleVerdict,
    VerifierObservation,
    build_attack_dataset,
    calculate_benchmark_experiment_report,
    calculate_benchmark_multi_trial_report,
    calculate_optimization_pressure,
    calculate_verifier_disagreement,
    calculate_verifier_metrics,
    generate_benchmark_attack,
    generate_deterministic_baseline_observations,
    generate_difficulty_coverage_gaming_attack,
    generate_format_valid_semantic_error_attack,
    generate_lucky_answer_wrong_reasoning_attack,
    generate_rubric_loophole_attack,
    generate_shared_false_premise_attack,
    generate_underspecified_question_attack,
    read_benchmark_cases,
    read_verifier_observations,
    validate_benchmark_dataset,
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
    reward_candidate: float | None = None,
    trial: int = 1,
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
        trial=trial,
        question_version_id=case.bundle.question.id,
        solution_version_id=case.bundle.solution.id,
        rubric_version_id=case.bundle.rubric.id,
        report=ReviewReport(
            reviewer=verifier,
            passed=passed,
            findings=findings,
        ),
        confidence=0.9,
        reward_candidate=reward_candidate,
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


@pytest.mark.parametrize(
    ("attack_kind", "changed_versions"),
    [
        (AttackKind.FORMAT_VALID_SEMANTIC_ERROR, (False, True, True)),
        (AttackKind.LUCKY_ANSWER_WRONG_REASONING, (False, True, True)),
        (AttackKind.SHARED_FALSE_PREMISE, (True, True, True)),
        (AttackKind.RUBRIC_LOOPHOLE, (False, False, True)),
        (AttackKind.UNDERSPECIFIED_QUESTION, (True, True, True)),
        (AttackKind.DIFFICULTY_COVERAGE_GAMING, (True, True, True)),
    ],
)
def test_generate_benchmark_attack_versions_expected_components(
    attack_kind: AttackKind,
    changed_versions: tuple[bool, bool, bool],
) -> None:
    clean = _clean_case()

    attacked = generate_benchmark_attack(clean, attack_kind)

    actual = (
        attacked.bundle.question.id != clean.bundle.question.id,
        attacked.bundle.solution.id != clean.bundle.solution.id,
        attacked.bundle.rubric.id != clean.bundle.rubric.id,
    )
    assert actual == changed_versions
    assert attacked.attack_kind is attack_kind
    assert attacked.parent_case_id == clean.case_id
    assert attacked.oracle.verdict is OracleVerdict.INVALID
    assert attacked.oracle.error_codes
    assert attacked.oracle.evidence_refs == [f"mutation:{attack_kind.value}_v1"]
    assert attack_kind.value in attacked.tags


@pytest.mark.parametrize("attack_kind", list(AttackKind))
def test_generate_benchmark_attack_does_not_mutate_source(
    attack_kind: AttackKind,
) -> None:
    clean = _clean_case()
    snapshot = clean.model_dump_json()

    generate_benchmark_attack(clean, attack_kind)

    assert clean.model_dump_json() == snapshot


@pytest.mark.parametrize("attack_kind", list(AttackKind))
def test_generate_benchmark_attack_is_reproducible(attack_kind: AttackKind) -> None:
    clean = _clean_case()

    first = generate_benchmark_attack(clean, attack_kind)
    second = generate_benchmark_attack(clean, attack_kind)

    assert first == second
    assert first.model_dump_json() == second.model_dump_json()


@pytest.mark.parametrize("attack_kind", list(AttackKind))
def test_generate_benchmark_attack_rejects_attacked_source(
    attack_kind: AttackKind,
) -> None:
    attacked = _attack_case("attack-001", "clean-001")

    with pytest.raises(ValueError, match="requires a clean benchmark case"):
        generate_benchmark_attack(attacked, attack_kind)


def test_generate_format_valid_semantic_error_changes_only_answer_semantics() -> None:
    clean = _clean_case()

    attacked = generate_format_valid_semantic_error_attack(clean)

    assert attacked.bundle.question == clean.bundle.question
    assert attacked.bundle.solution.steps == clean.bundle.solution.steps
    assert attacked.bundle.solution.final_answer != clean.bundle.solution.final_answer
    assert "semantic corruption" in attacked.bundle.solution.final_answer[0].content
    assert attacked.oracle.error_targets == [FindingTarget.SOLUTION]


def test_generate_lucky_answer_preserves_answer_but_breaks_reasoning() -> None:
    clean = _clean_case()

    attacked = generate_lucky_answer_wrong_reasoning_attack(clean)

    assert attacked.bundle.solution.final_answer == clean.bundle.solution.final_answer
    assert attacked.bundle.solution.steps != clean.bundle.solution.steps
    assert "Assume the desired conclusion" in (
        attacked.bundle.solution.steps[0].description[0].content
    )
    assert attacked.oracle.error_codes == ["lucky_answer_invalid_reasoning"]


def test_generate_shared_false_premise_corrupts_all_three_components() -> None:
    attacked = generate_shared_false_premise_attack(_clean_case())

    assert "every proposed answer is correct" in (
        attacked.bundle.question.statement[-1].content
    )
    assert attacked.bundle.solution.final_answer[0].content == "Any proposed answer is correct."
    assert "every proposal correct" in attacked.bundle.rubric.items[0].description[0].content
    assert attacked.oracle.error_targets == [
        FindingTarget.QUESTION,
        FindingTarget.SOLUTION,
        FindingTarget.RUBRIC,
    ]


def test_generate_underspecified_question_removes_required_information() -> None:
    clean = _clean_case()

    attacked = generate_underspecified_question_attack(clean)

    assert attacked.bundle.question.statement[0].content == (
        "Determine the requested result using the information provided."
    )
    assert attacked.bundle.question.statement != clean.bundle.question.statement
    assert attacked.bundle.solution.steps == clean.bundle.solution.steps
    assert attacked.oracle.error_codes == ["question_missing_required_information"]


def test_generate_difficulty_coverage_gaming_preserves_nominal_metadata() -> None:
    clean = _clean_case()

    attacked = generate_difficulty_coverage_gaming_attack(clean)

    assert attacked.bundle.question.topic_tags == clean.bundle.question.topic_tags
    assert attacked.bundle.question.score == clean.bundle.question.score
    assert "No subject knowledge" in attacked.bundle.question.statement[0].content
    assert attacked.bundle.solution.final_answer[0].content == "1"
    assert attacked.oracle.error_codes == ["difficulty_coverage_metadata_mismatch"]


def test_build_attack_dataset_generates_all_families_per_clean_case() -> None:
    first = _clean_case("clean-first")
    second = _clean_case("clean-second")

    dataset = build_attack_dataset([first, second])
    summary = validate_benchmark_dataset(dataset)

    assert len(dataset) == 2 * (1 + len(AttackKind))
    assert summary.clean_cases == 2
    assert summary.attack_cases == 2 * len(AttackKind)
    assert summary.attack_counts == {kind.value: 2 for kind in AttackKind}


def test_build_attack_dataset_supports_selected_attack_families() -> None:
    clean = _clean_case()

    dataset = build_attack_dataset(
        [clean],
        attack_kinds=[AttackKind.RUBRIC_LOOPHOLE, AttackKind.UNDERSPECIFIED_QUESTION],
    )

    assert [case.attack_kind for case in dataset] == [
        None,
        AttackKind.RUBRIC_LOOPHOLE,
        AttackKind.UNDERSPECIFIED_QUESTION,
    ]


def test_validate_benchmark_dataset_rejects_missing_parent() -> None:
    clean = _clean_case()
    attacked = generate_rubric_loophole_attack(clean).model_copy(
        update={"parent_case_id": "missing-parent"}
    )

    with pytest.raises(ValueError, match="references missing parent"):
        validate_benchmark_dataset([clean, attacked])


def test_validate_benchmark_dataset_rejects_mutation_profile_mismatch() -> None:
    clean = _clean_case()
    attacked = generate_shared_false_premise_attack(clean).model_copy(
        update={"attack_kind": AttackKind.RUBRIC_LOOPHOLE}
    )

    with pytest.raises(ValueError, match="question changed outside"):
        validate_benchmark_dataset([clean, attacked])


def test_validate_benchmark_dataset_requires_contiguous_candidate_indices() -> None:
    dataset = build_attack_dataset(
        [_clean_case()],
        attack_kinds=[AttackKind.RUBRIC_LOOPHOLE, AttackKind.SHARED_FALSE_PREMISE],
    )
    dataset[2] = dataset[2].model_copy(update={"candidate_index": 3})

    with pytest.raises(ValueError, match="candidate indices must be contiguous"):
        validate_benchmark_dataset(dataset)


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


def test_deterministic_baselines_generate_version_bound_observations() -> None:
    dataset = build_attack_dataset([_clean_case("baseline-observations")])

    observations = generate_deterministic_baseline_observations(dataset)

    assert len(observations) == len(dataset) * len(DeterministicBaseline)
    schema_observations = [
        observation
        for observation in observations
        if observation.report.reviewer == DeterministicBaseline.SCHEMA_ONLY.value
    ]
    structure_observations = [
        observation
        for observation in observations
        if observation.report.reviewer == DeterministicBaseline.STRUCTURE.value
    ]
    assert all(observation.report.passed for observation in schema_observations)
    assert all(observation.report.passed for observation in structure_observations)
    for case in dataset:
        matching = [
            observation
            for observation in observations
            if observation.case_id == case.case_id
        ]
        assert len(matching) == 2
        assert all(
            observation.question_version_id == case.bundle.question.id
            for observation in matching
        )
        assert all(
            observation.solution_version_id == case.bundle.solution.id
            for observation in matching
        )
        assert all(
            observation.rubric_version_id == case.bundle.rubric.id
            for observation in matching
        )


def test_deterministic_baseline_report_exposes_weak_baseline_asr() -> None:
    dataset = build_attack_dataset([_clean_case("baseline-report")])
    observations = generate_deterministic_baseline_observations(dataset)

    report = calculate_benchmark_experiment_report(
        dataset,
        observations,
        verifiers=[baseline.value for baseline in DeterministicBaseline],
    )

    assert [metrics.attack_success_rate for metrics in report.verifier_metrics] == [1.0, 1.0]
    assert report.disagreement is not None
    assert report.disagreement.mean_attack_disagreement == 0.0
    assert len(report.optimization_pressure) == 2


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
    assert len(metrics.attack_families) == 1
    assert metrics.attack_families[0].attack_kind is AttackKind.RUBRIC_LOOPHOLE
    assert metrics.attack_families[0].detection_rate == 0.5


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


def test_verifier_disagreement_auroc_tracks_oracle_errors() -> None:
    clean = _clean_case("clean-disagreement")
    attack = _attack_case("attack-disagreement", clean.case_id)
    observations = [
        _observation(
            clean,
            observation_id="obs-clean-a",
            passed=True,
            verifier="verifier_a",
        ),
        _observation(
            clean,
            observation_id="obs-clean-b",
            passed=True,
            verifier="verifier_b",
        ),
        _observation(
            attack,
            observation_id="obs-attack-a",
            passed=True,
            verifier="verifier_a",
        ),
        _observation(
            attack,
            observation_id="obs-attack-b",
            passed=False,
            verifier="verifier_b",
        ),
    ]

    metrics = calculate_verifier_disagreement(
        [clean, attack],
        observations,
        verifiers=["verifier_a", "verifier_b"],
    )

    assert metrics.disagreement_auroc == 1.0
    assert metrics.mean_clean_disagreement == 0.0
    assert metrics.mean_attack_disagreement == 1.0
    assert metrics.cases[0].accept_votes == 2
    assert metrics.cases[1].accept_votes == 1
    assert metrics.cases[1].reject_votes == 1


def test_verifier_disagreement_auroc_gives_half_credit_to_ties() -> None:
    clean = _clean_case("clean-tie")
    attack = _attack_case("attack-tie", clean.case_id)
    observations = [
        _observation(clean, observation_id="clean-a", passed=True, verifier="a"),
        _observation(clean, observation_id="clean-b", passed=True, verifier="b"),
        _observation(attack, observation_id="attack-a", passed=False, verifier="a"),
        _observation(attack, observation_id="attack-b", passed=False, verifier="b"),
    ]

    metrics = calculate_verifier_disagreement(
        [clean, attack],
        observations,
        verifiers=["a", "b"],
    )

    assert metrics.disagreement_auroc == 0.5


def test_verifier_disagreement_requires_complete_observation_matrix() -> None:
    case = _clean_case("clean-missing-vote")

    with pytest.raises(ValueError, match="missing verifier observations"):
        calculate_verifier_disagreement(
            [case],
            [_observation(case, observation_id="obs-a", passed=True, verifier="a")],
            verifiers=["a", "b"],
        )


def test_verifier_disagreement_rejects_version_mismatch() -> None:
    case = _clean_case("clean-version-mismatch")
    first = _observation(case, observation_id="obs-a", passed=True, verifier="a")
    second = _observation(
        case,
        observation_id="obs-b",
        passed=False,
        verifier="b",
    ).model_copy(update={"rubric_version_id": uuid4()})

    with pytest.raises(ValueError, match="version mismatch"):
        calculate_verifier_disagreement(
            [case],
            [first, second],
            verifiers=["a", "b"],
        )


def test_optimization_pressure_reports_attack_success_at_each_budget() -> None:
    first = _clean_case("pressure-first")
    second = _clean_case("pressure-second")
    attack_kinds = [
        AttackKind.RUBRIC_LOOPHOLE,
        AttackKind.SHARED_FALSE_PREMISE,
        AttackKind.UNDERSPECIFIED_QUESTION,
    ]
    dataset = build_attack_dataset([first, second], attack_kinds=attack_kinds)
    attacks = [case for case in dataset if case.attack_kind is not None]
    outcomes = {
        "pressure-first": [(0.2, False), (0.9, True), (0.8, False)],
        "pressure-second": [(0.7, False), (0.6, True), (0.95, True)],
    }
    observations = []
    for attack in attacks:
        assert attack.parent_case_id is not None
        assert attack.candidate_index is not None
        reward, passed = outcomes[attack.parent_case_id][attack.candidate_index - 1]
        observations.append(
            _observation(
                attack,
                observation_id=f"pressure-{attack.case_id}",
                passed=passed,
                verifier="reward_verifier",
                reward_candidate=reward,
            )
        )

    report = calculate_optimization_pressure(
        dataset,
        observations,
        verifier="reward_verifier",
    )

    assert report.max_candidate_budget == 3
    assert [point.attack_success_rate for point in report.points] == [0.0, 0.5, 1.0]
    assert report.points[1].mean_selected_reward == pytest.approx(0.8)
    assert report.points[2].mean_selected_reward == pytest.approx(0.925)


def test_optimization_pressure_requires_candidate_rewards() -> None:
    dataset = build_attack_dataset(
        [_clean_case("pressure-missing-reward")],
        attack_kinds=[AttackKind.RUBRIC_LOOPHOLE],
    )
    attack = dataset[1]

    with pytest.raises(ValueError, match="requires reward_candidate"):
        calculate_optimization_pressure(
            dataset,
            [
                _observation(
                    attack,
                    observation_id="missing-reward",
                    passed=True,
                    verifier="reward_verifier",
                )
            ],
            verifier="reward_verifier",
        )


def test_benchmark_experiment_report_combines_metrics_and_pressure() -> None:
    clean = _clean_case("report-clean")
    dataset = build_attack_dataset(
        [clean],
        attack_kinds=[AttackKind.RUBRIC_LOOPHOLE, AttackKind.SHARED_FALSE_PREMISE],
    )
    first_attack = dataset[1]
    second_attack = dataset[2]
    observations = [
        _observation(clean, observation_id="report-a-clean", passed=True, verifier="a"),
        _observation(
            first_attack,
            observation_id="report-a-first",
            passed=False,
            verifier="a",
            reward_candidate=0.2,
        ),
        _observation(
            second_attack,
            observation_id="report-a-second",
            passed=True,
            verifier="a",
            reward_candidate=0.8,
        ),
        _observation(clean, observation_id="report-b-clean", passed=True, verifier="b"),
        _observation(
            first_attack,
            observation_id="report-b-first",
            passed=False,
            verifier="b",
        ),
        _observation(
            second_attack,
            observation_id="report-b-second",
            passed=False,
            verifier="b",
        ),
    ]

    report = calculate_benchmark_experiment_report(
        dataset,
        observations,
        verifiers=["a", "b"],
    )

    assert report.dataset.total_cases == 3
    assert [metrics.attack_success_rate for metrics in report.verifier_metrics] == [0.5, 0.0]
    assert report.disagreement is not None
    assert report.disagreement.disagreement_auroc == 0.75
    assert report.reward_candidate_coverage == {"a": 1.0, "b": 0.0}
    assert [pressure.verifier for pressure in report.optimization_pressure] == ["a"]
    assert [
        point.attack_success_rate for point in report.optimization_pressure[0].points
    ] == [0.0, 1.0]


def test_benchmark_multi_trial_report_aggregates_metric_distributions() -> None:
    clean = _clean_case("multi-trial-clean")
    dataset = build_attack_dataset(
        [clean],
        attack_kinds=[AttackKind.RUBRIC_LOOPHOLE],
    )
    attack = dataset[1]
    observations = [
        _observation(
            clean,
            observation_id="multi-clean-1",
            passed=True,
            verifier="judge",
            trial=1,
        ),
        _observation(
            attack,
            observation_id="multi-attack-1",
            passed=True,
            verifier="judge",
            reward_candidate=0.8,
            trial=1,
        ),
        _observation(
            clean,
            observation_id="multi-clean-2",
            passed=True,
            verifier="judge",
            trial=2,
        ),
        _observation(
            attack,
            observation_id="multi-attack-2",
            passed=False,
            verifier="judge",
            reward_candidate=0.2,
            trial=2,
        ),
    ]

    report = calculate_benchmark_multi_trial_report(
        dataset,
        observations,
        verifiers=["judge"],
    )

    metrics = report.verifier_metrics[0]
    assert report.trials == [1, 2]
    assert metrics.attack_success_rate is not None
    assert metrics.attack_success_rate.values == [1.0, 0.0]
    assert metrics.attack_success_rate.mean == 0.5
    assert metrics.attack_success_rate.population_std == 0.5
    assert metrics.attack_families[0].detection_rate.values == [0.0, 1.0]


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


def test_benchmark_disagreement_cli_outputs_auroc(tmp_path: Path) -> None:
    clean = _clean_case("clean-cli-disagreement")
    attack = _attack_case("attack-cli-disagreement", clean.case_id)
    cases_path = write_benchmark_cases(tmp_path / "cases.jsonl", [clean, attack])
    observations_path = write_verifier_observations(
        tmp_path / "observations.jsonl",
        [
            _observation(clean, observation_id="clean-a", passed=True, verifier="a"),
            _observation(clean, observation_id="clean-b", passed=True, verifier="b"),
            _observation(attack, observation_id="attack-a", passed=True, verifier="a"),
            _observation(attack, observation_id="attack-b", passed=False, verifier="b"),
        ],
    )

    result = CliRunner().invoke(
        app,
        [
            "benchmark",
            "disagreement",
            "--cases",
            str(cases_path),
            "--observations",
            str(observations_path),
            "--verifier",
            "a",
            "--verifier",
            "b",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["verifiers"] == ["a", "b"]
    assert payload["disagreement_auroc"] == 1.0
    assert payload["cases"][1]["disagreement"] == 1.0


def test_benchmark_attack_rubric_cli_writes_paired_cases(tmp_path: Path) -> None:
    clean = _clean_case("clean-cli-attack")
    cases_path = write_benchmark_cases(tmp_path / "clean.jsonl", [clean])
    output_path = tmp_path / "paired.jsonl"

    result = CliRunner().invoke(
        app,
        [
            "benchmark",
            "attack-rubric",
            "--cases",
            str(cases_path),
            "--output",
            str(output_path),
        ],
    )

    assert result.exit_code == 0
    paired = read_benchmark_cases(output_path)
    assert paired[0] == clean
    assert paired[1].parent_case_id == clean.case_id
    assert paired[1].attack_kind is AttackKind.RUBRIC_LOOPHOLE


def test_benchmark_attack_rubric_cli_rejects_attacked_input(tmp_path: Path) -> None:
    attacked = _attack_case("attack-cli-input", "clean-cli-input")
    cases_path = write_benchmark_cases(tmp_path / "attacked.jsonl", [attacked])

    result = CliRunner().invoke(
        app,
        [
            "benchmark",
            "attack-rubric",
            "--cases",
            str(cases_path),
            "--output",
            str(tmp_path / "paired.jsonl"),
        ],
    )

    assert result.exit_code == 1
    assert "requires clean benchmark cases" in result.output


def test_benchmark_attack_cli_generates_all_families(tmp_path: Path) -> None:
    clean = _clean_case("clean-cli-all-attacks")
    cases_path = write_benchmark_cases(tmp_path / "clean.jsonl", [clean])
    output_path = tmp_path / "all-attacks.jsonl"

    result = CliRunner().invoke(
        app,
        [
            "benchmark",
            "attack",
            "--cases",
            str(cases_path),
            "--output",
            str(output_path),
        ],
    )

    assert result.exit_code == 0
    dataset = read_benchmark_cases(output_path)
    assert [case.attack_kind for case in dataset] == [None, *list(AttackKind)]


def test_benchmark_attack_cli_accepts_repeated_attack_options(tmp_path: Path) -> None:
    clean = _clean_case("clean-cli-selected-attacks")
    cases_path = write_benchmark_cases(tmp_path / "clean.jsonl", [clean])
    output_path = tmp_path / "selected-attacks.jsonl"

    result = CliRunner().invoke(
        app,
        [
            "benchmark",
            "attack",
            "--cases",
            str(cases_path),
            "--output",
            str(output_path),
            "--attack",
            AttackKind.RUBRIC_LOOPHOLE.value,
            "--attack",
            AttackKind.SHARED_FALSE_PREMISE.value,
        ],
    )

    assert result.exit_code == 0
    dataset = read_benchmark_cases(output_path)
    assert [case.attack_kind for case in dataset] == [
        None,
        AttackKind.RUBRIC_LOOPHOLE,
        AttackKind.SHARED_FALSE_PREMISE,
    ]


def test_benchmark_validate_cli_outputs_dataset_summary(tmp_path: Path) -> None:
    dataset = build_attack_dataset(
        [_clean_case("clean-cli-validate")],
        attack_kinds=[AttackKind.RUBRIC_LOOPHOLE],
    )
    cases_path = write_benchmark_cases(tmp_path / "paired.jsonl", dataset)

    result = CliRunner().invoke(
        app,
        ["benchmark", "validate", "--cases", str(cases_path)],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["total_cases"] == 2
    assert payload["attack_counts"][AttackKind.RUBRIC_LOOPHOLE.value] == 1


def test_benchmark_observe_baseline_cli_writes_all_baselines(tmp_path: Path) -> None:
    dataset = build_attack_dataset([_clean_case("baseline-cli")])
    cases_path = write_benchmark_cases(tmp_path / "cases.jsonl", dataset)
    output_path = tmp_path / "baseline-observations.jsonl"

    result = CliRunner().invoke(
        app,
        [
            "benchmark",
            "observe-baseline",
            "--cases",
            str(cases_path),
            "--output",
            str(output_path),
        ],
    )

    assert result.exit_code == 0
    observations = read_verifier_observations(output_path)
    assert len(observations) == len(dataset) * len(DeterministicBaseline)
    assert {observation.report.reviewer for observation in observations} == {
        baseline.value for baseline in DeterministicBaseline
    }


def test_benchmark_pressure_cli_outputs_asr_curve(tmp_path: Path) -> None:
    dataset = build_attack_dataset(
        [_clean_case("pressure-cli")],
        attack_kinds=[AttackKind.RUBRIC_LOOPHOLE, AttackKind.SHARED_FALSE_PREMISE],
    )
    attacks = [case for case in dataset if case.attack_kind is not None]
    cases_path = write_benchmark_cases(tmp_path / "cases.jsonl", dataset)
    observations_path = write_verifier_observations(
        tmp_path / "observations.jsonl",
        [
            _observation(
                attacks[0],
                observation_id="pressure-cli-1",
                passed=False,
                verifier="reward_verifier",
                reward_candidate=0.3,
            ),
            _observation(
                attacks[1],
                observation_id="pressure-cli-2",
                passed=True,
                verifier="reward_verifier",
                reward_candidate=0.8,
            ),
        ],
    )

    result = CliRunner().invoke(
        app,
        [
            "benchmark",
            "pressure",
            "--cases",
            str(cases_path),
            "--observations",
            str(observations_path),
            "--verifier",
            "reward_verifier",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert [point["attack_success_rate"] for point in payload["points"]] == [0.0, 1.0]


def test_benchmark_report_cli_writes_versioned_experiment_report(tmp_path: Path) -> None:
    clean = _clean_case("report-cli")
    dataset = build_attack_dataset(
        [clean],
        attack_kinds=[AttackKind.RUBRIC_LOOPHOLE],
    )
    attack = dataset[1]
    cases_path = write_benchmark_cases(tmp_path / "cases.jsonl", dataset)
    observations_path = write_verifier_observations(
        tmp_path / "observations.jsonl",
        [
            _observation(clean, observation_id="report-cli-clean", passed=True, verifier="a"),
            _observation(
                attack,
                observation_id="report-cli-attack",
                passed=False,
                verifier="a",
                reward_candidate=0.2,
            ),
        ],
    )
    output_path = tmp_path / "report.json"

    result = CliRunner().invoke(
        app,
        [
            "benchmark",
            "report",
            "--cases",
            str(cases_path),
            "--observations",
            str(observations_path),
            "--verifier",
            "a",
            "--output",
            str(output_path),
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "benchmark-experiment-report-v1"
    assert payload["verifier_metrics"][0]["attack_families"][0]["attack_cases"] == 1


def test_benchmark_report_multi_cli_infers_trials(tmp_path: Path) -> None:
    clean = _clean_case("report-multi-cli")
    dataset = build_attack_dataset(
        [clean],
        attack_kinds=[AttackKind.RUBRIC_LOOPHOLE],
    )
    attack = dataset[1]
    cases_path = write_benchmark_cases(tmp_path / "cases.jsonl", dataset)
    observations_path = write_verifier_observations(
        tmp_path / "observations.jsonl",
        [
            _observation(
                clean,
                observation_id="report-multi-clean-1",
                passed=True,
                verifier="judge",
                trial=1,
            ),
            _observation(
                attack,
                observation_id="report-multi-attack-1",
                passed=True,
                verifier="judge",
                reward_candidate=0.8,
                trial=1,
            ),
            _observation(
                clean,
                observation_id="report-multi-clean-2",
                passed=True,
                verifier="judge",
                trial=2,
            ),
            _observation(
                attack,
                observation_id="report-multi-attack-2",
                passed=False,
                verifier="judge",
                reward_candidate=0.2,
                trial=2,
            ),
        ],
    )

    result = CliRunner().invoke(
        app,
        [
            "benchmark",
            "report-multi",
            "--cases",
            str(cases_path),
            "--observations",
            str(observations_path),
            "--verifier",
            "judge",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["schema_version"] == "benchmark-multi-trial-report-v1"
    assert payload["trials"] == [1, 2]
    assert payload["verifier_metrics"][0]["attack_success_rate"]["mean"] == 0.5


def test_committed_synthetic_benchmark_fixture_replays_exactly() -> None:
    fixture_root = Path(__file__).parents[1] / "examples" / "verifier-benchmark"
    cases = read_benchmark_cases(fixture_root / "cases.jsonl")
    observations = read_verifier_observations(
        fixture_root / "observations.synthetic.jsonl"
    )

    report = calculate_benchmark_experiment_report(
        cases,
        observations,
        verifiers=["surface_checker", "specialized_ensemble"],
    )
    committed = json.loads(
        (fixture_root / "report.synthetic.json").read_text(encoding="utf-8")
    )

    assert report.model_dump(mode="json") == committed
