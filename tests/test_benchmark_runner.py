import asyncio
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import BaseModel, ValidationError
from typer.testing import CliRunner

import assessment_workbench.cli as cli_module
from assessment_workbench.benchmark_runner import (
    BenchmarkVerifierJudgment,
    run_llm_verifier,
)
from assessment_workbench.benchmarking import (
    AttackKind,
    BenchmarkCase,
    BenchmarkOracle,
    OracleMethod,
    OracleVerdict,
    build_attack_dataset,
    read_verifier_observations,
    write_benchmark_cases,
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
    RubricItem,
    RubricVersion,
    SolutionStep,
    SolutionVersion,
)
from assessment_workbench.prompting import load_default_prompt_registry
from assessment_workbench.storage import Workspace


def _clean_case(case_id: str = "runner-clean") -> BenchmarkCase:
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
        items=[RubricItem(id="r1", description="Uses a valid method.", score=10)],
        metadata=GenerationMetadata(role="fixture_rubric"),
    )
    return BenchmarkCase(
        case_id=case_id,
        bundle=ExamQuestionBundle(question=question, solution=solution, rubric=rubric),
        oracle=BenchmarkOracle(
            verdict=OracleVerdict.VALID,
            method=OracleMethod.SYMBOLIC,
            rationale="Substitution verifies the clean answer.",
        ),
    )


class _FixtureVerifierModel:
    def __init__(self, *, fail_on_underspecified: bool = False) -> None:
        self.fail_on_underspecified = fail_on_underspecified
        self.calls: list[dict[str, Any]] = []
        self.active = 0
        self.max_active = 0

    async def complete(self, **kwargs: Any) -> BaseModel:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.01)
            payload = json.loads(kwargs["user_prompt"])
            self.calls.append(payload)
            assert set(payload) == {"bundle", "evaluation_contract"}
            assert "oracle" not in payload
            statement = payload["bundle"]["question"]["statement"][0]["content"]
            if self.fail_on_underspecified and statement.startswith("Determine the requested"):
                raise RuntimeError("fixture verifier failure")
            rubric_text = payload["bundle"]["rubric"]["items"][0]["description"][0][
                "content"
            ]
            rejected = "any single term" in rubric_text
            findings = []
            if rejected:
                findings = [
                    ReviewFinding(
                        code="keyword_rubric",
                        severity=FindingSeverity.ERROR,
                        target=FindingTarget.RUBRIC,
                        message="The rubric awards full credit for a keyword match.",
                    )
                ]
            response_model = kwargs["response_model"]
            return response_model(
                passed=not rejected,
                findings=findings,
                summary="Fixture semantic verifier judgment.",
                confidence=0.9,
                reward_candidate=0.1 if rejected else 0.9,
            )
        finally:
            self.active -= 1


def test_benchmark_verifier_judgment_enforces_blocking_gate() -> None:
    with pytest.raises(ValidationError, match="cannot contain error or fatal"):
        BenchmarkVerifierJudgment(
            passed=True,
            findings=[
                ReviewFinding(
                    code="invalid",
                    severity=FindingSeverity.ERROR,
                    target=FindingTarget.BUNDLE,
                    message="Invalid bundle.",
                )
            ],
            summary="Contradictory judgment.",
            confidence=0.9,
            reward_candidate=0.9,
        )


async def test_llm_verifier_runner_is_oracle_blind_and_bounded() -> None:
    dataset = build_attack_dataset(
        [_clean_case()],
        attack_kinds=[AttackKind.RUBRIC_LOOPHOLE, AttackKind.UNDERSPECIFIED_QUESTION],
    )
    model = _FixtureVerifierModel()
    prompt = load_default_prompt_registry().require("benchmark_verifier")

    result = await run_llm_verifier(
        dataset,
        model,  # type: ignore[arg-type]
        prompt=prompt,
        verifier="fixture_llm",
        model_name="fixture-model",
        concurrency=2,
    )

    assert not result.failures
    assert len(result.observations) == 3
    assert model.max_active == 2
    assert [observation.report.passed for observation in result.observations] == [
        True,
        False,
        True,
    ]
    assert all(observation.model == "fixture-model" for observation in result.observations)


async def test_llm_verifier_runner_resumes_and_isolates_failures() -> None:
    dataset = build_attack_dataset(
        [_clean_case("runner-resume")],
        attack_kinds=[AttackKind.RUBRIC_LOOPHOLE, AttackKind.UNDERSPECIFIED_QUESTION],
    )
    model = _FixtureVerifierModel(fail_on_underspecified=True)
    completed: list[str] = []

    result = await run_llm_verifier(
        dataset,
        model,  # type: ignore[arg-type]
        prompt=load_default_prompt_registry().require("benchmark_verifier"),
        verifier="fixture_llm",
        model_name="fixture-model",
        completed_case_ids=[dataset[0].case_id],
        on_observation=lambda observation: completed.append(observation.case_id),
    )

    assert len(model.calls) == 2
    assert [observation.case_id for observation in result.observations] == [dataset[1].case_id]
    assert completed == [dataset[1].case_id]
    assert result.failures == {dataset[2].case_id: "fixture verifier failure"}


def test_benchmark_observe_llm_cli_resumes_existing_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset = build_attack_dataset(
        [_clean_case("runner-cli")],
        attack_kinds=[AttackKind.RUBRIC_LOOPHOLE],
    )
    cases_path = write_benchmark_cases(tmp_path / "cases.jsonl", dataset)
    output_path = tmp_path / "observations.jsonl"
    workspace = Workspace(tmp_path / "workspace")
    workspace.initialize()
    model = _FixtureVerifierModel()
    monkeypatch.setattr(cli_module, "OpenAICompatibleModel", lambda **_: model)
    arguments = [
        "benchmark",
        "observe-llm",
        "--cases",
        str(cases_path),
        "--output",
        str(output_path),
        "--verifier",
        "fixture_llm",
        "--workspace",
        str(workspace.root),
    ]

    first = CliRunner().invoke(app, arguments)
    first_call_count = len(model.calls)
    second = CliRunner().invoke(app, arguments)

    assert first.exit_code == 0
    assert second.exit_code == 0
    assert first_call_count == len(dataset)
    assert len(model.calls) == first_call_count
    observations = read_verifier_observations(output_path)
    assert len(observations) == len(dataset)
