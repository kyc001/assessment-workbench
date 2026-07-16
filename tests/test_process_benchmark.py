import asyncio
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from assessment_workbench.process_benchmark import (
    ProcessBenchmarkCase,
    ProcessSamplingStrategy,
    ProcessVerifierObservation,
    calculate_process_benchmark_report,
    import_processbench_cases,
    run_process_verifier,
)
from assessment_workbench.prompting import load_default_prompt_registry


def _source_records() -> list[dict[str, object]]:
    return [
        {
            "id": "error-0",
            "generator": "fixture",
            "problem": "What is 2 + 2?",
            "steps": ["Add the numbers.", "2 + 2 = 5."],
            "final_answer_correct": False,
            "label": 1,
        },
        {
            "id": "trap-0",
            "generator": "fixture",
            "problem": "What is 3 + 4?",
            "steps": ["Claim 3 + 4 = 8.", "Correct the answer to 7 without justification."],
            "final_answer_correct": True,
            "label": 0,
        },
        {
            "id": "clean-0",
            "generator": "fixture",
            "problem": "What is 5 + 6?",
            "steps": ["5 + 6 = 11."],
            "final_answer_correct": True,
            "label": -1,
        },
        {
            "id": "clean-1",
            "generator": "fixture",
            "problem": "What is 8 + 1?",
            "steps": ["8 + 1 = 9."],
            "final_answer_correct": True,
            "label": -1,
        },
    ]


def _write_source(path: Path) -> Path:
    path.write_text(json.dumps(_source_records()), encoding="utf-8")
    return path


def test_processbench_import_preserves_step_oracle_and_attribution(tmp_path: Path) -> None:
    cases = import_processbench_cases(
        _write_source(tmp_path / "source.json"),
        split="gsm8k",
    )

    assert [case.first_error_step for case in cases] == [1, 0, -1, -1]
    assert all(case.source_dataset == "Qwen/ProcessBench" for case in cases)
    assert all(case.source_license == "Apache-2.0" for case in cases)
    assert cases[1].final_answer_correct is True


def test_diagnostic_sampling_includes_final_answer_trap_and_clean_case(
    tmp_path: Path,
) -> None:
    cases = import_processbench_cases(
        _write_source(tmp_path / "source.json"),
        split="gsm8k",
        limit=3,
        sampling=ProcessSamplingStrategy.DIAGNOSTIC,
    )

    assert len(cases) == 3
    assert any(case.first_error_step >= 0 and case.final_answer_correct for case in cases)
    assert any(case.first_error_step == -1 for case in cases)


class _FixtureProcessVerifier:
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.prompts: list[dict[str, Any]] = []

    async def complete(self, **kwargs: Any) -> BaseModel:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.01)
            payload = json.loads(kwargs["user_prompt"])
            self.prompts.append(payload)
            combined = " ".join(step["content"] for step in payload["steps"])
            if "= 5" in combined:
                prediction = 1
            elif "= 8" in combined:
                prediction = 0
            else:
                prediction = -1
            return kwargs["response_model"](
                first_error_step=prediction,
                confidence=0.9,
                rationale="Fixture process judgment.",
                error_summary=("Arithmetic error." if prediction >= 0 else None),
            )
        finally:
            self.active -= 1


async def test_process_verifier_runner_is_oracle_blind_and_bounded(tmp_path: Path) -> None:
    cases = import_processbench_cases(
        _write_source(tmp_path / "source.json"),
        split="gsm8k",
    )
    model = _FixtureProcessVerifier()

    result = await run_process_verifier(
        cases,
        model,  # type: ignore[arg-type]
        prompt=load_default_prompt_registry().require("process_verifier"),
        verifier="fixture_process",
        model_name="fixture-model",
        concurrency=2,
    )

    assert not result.failures
    assert model.max_active == 2
    assert len(result.observations) == len(cases)
    assert all(set(prompt) == {"problem", "steps", "output_contract"} for prompt in model.prompts)
    assert all("label" not in prompt for prompt in model.prompts)


async def test_process_verifier_runner_limits_new_cases_for_batched_resume(
    tmp_path: Path,
) -> None:
    cases = import_processbench_cases(
        _write_source(tmp_path / "source.json"),
        split="gsm8k",
    )
    model = _FixtureProcessVerifier()

    result = await run_process_verifier(
        cases,
        model,  # type: ignore[arg-type]
        prompt=load_default_prompt_registry().require("process_verifier"),
        verifier="fixture_process",
        model_name="fixture-model",
        max_new_cases=2,
        completed_case_ids=[cases[0].case_id],
    )

    assert [observation.case_id for observation in result.observations] == [
        cases[1].case_id,
        cases[2].case_id,
    ]
    assert len(model.prompts) == 2


def test_process_report_scores_localization_and_final_answer_traps() -> None:
    cases = [
        ProcessBenchmarkCase(
            case_id="processbench.gsm8k.error",
            source_dataset="Qwen/ProcessBench",
            source_split="gsm8k",
            source_id="error",
            source_url="https://example.test",
            source_license="Apache-2.0",
            generator="fixture",
            problem="Problem",
            steps=["Wrong step", "Later step"],
            first_error_step=0,
            final_answer_correct=True,
        ),
        ProcessBenchmarkCase(
            case_id="processbench.gsm8k.clean",
            source_dataset="Qwen/ProcessBench",
            source_split="gsm8k",
            source_id="clean",
            source_url="https://example.test",
            source_license="Apache-2.0",
            generator="fixture",
            problem="Problem",
            steps=["Correct step"],
            first_error_step=-1,
            final_answer_correct=True,
        ),
    ]
    observations = [
        ProcessVerifierObservation(
            observation_id="process:fixture:trial-1:processbench.gsm8k.error",
            case_id=cases[0].case_id,
            verifier="fixture",
            predicted_first_error_step=0,
            confidence=1,
            rationale="Detected the first error.",
            error_summary="Invalid first step.",
            model="fixture",
            prompt_version="fixture-v1",
        ),
        ProcessVerifierObservation(
            observation_id="process:fixture:trial-1:processbench.gsm8k.clean",
            case_id=cases[1].case_id,
            verifier="fixture",
            predicted_first_error_step=-1,
            confidence=1,
            rationale="All steps are correct.",
            model="fixture",
            prompt_version="fixture-v1",
        ),
    ]

    report = calculate_process_benchmark_report(
        cases,
        observations,
        verifier="fixture",
    )

    assert report.dataset.final_answer_traps == 1
    assert report.metrics.exact_localization_accuracy == 1
    assert report.metrics.error_detection_recall == 1
    assert report.metrics.correct_process_acceptance_rate == 1
    assert report.metrics.final_answer_trap_localization_accuracy == 1
