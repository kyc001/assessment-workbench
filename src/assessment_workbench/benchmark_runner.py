import asyncio
import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from pydantic import Field, model_validator

from assessment_workbench.benchmarking import (
    BenchmarkCase,
    VerifierObservation,
    validate_benchmark_dataset,
)
from assessment_workbench.domain import (
    FindingSeverity,
    ReviewFinding,
    ReviewReport,
    StrictModel,
)
from assessment_workbench.ports import StructuredModel
from assessment_workbench.prompting import PromptBundle


class BenchmarkVerifierJudgment(StrictModel):
    passed: bool
    findings: list[ReviewFinding] = Field(default_factory=list)
    summary: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    reward_candidate: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_gate_consistency(self) -> "BenchmarkVerifierJudgment":
        blocking = any(
            finding.severity in {FindingSeverity.ERROR, FindingSeverity.FATAL}
            for finding in self.findings
        )
        if self.passed and blocking:
            raise ValueError("passed judgment cannot contain error or fatal findings")
        if not self.passed and not blocking:
            raise ValueError("failed judgment requires an error or fatal finding")
        return self


@dataclass(frozen=True)
class BenchmarkVerifierRunResult:
    observations: list[VerifierObservation]
    failures: dict[str, str]


async def run_llm_verifier(
    cases: Iterable[BenchmarkCase],
    model: StructuredModel,
    *,
    prompt: PromptBundle,
    verifier: str,
    model_name: str,
    trial: int = 1,
    concurrency: int = 4,
    completed_case_ids: Iterable[str] = (),
    on_observation: Callable[[VerifierObservation], None] | None = None,
) -> BenchmarkVerifierRunResult:
    materialized_cases = list(cases)
    validate_benchmark_dataset(materialized_cases)
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", verifier) is None:
        raise ValueError("LLM verifier id must be safe for observation identifiers")
    if not model_name.strip():
        raise ValueError("LLM verifier model name cannot be empty")
    if trial < 1:
        raise ValueError("LLM verifier trial must be at least 1")
    if concurrency < 1:
        raise ValueError("LLM verifier concurrency must be at least 1")
    completed = set(completed_case_ids)
    known_case_ids = {case.case_id for case in materialized_cases}
    unknown_completed = sorted(completed - known_case_ids)
    if unknown_completed:
        raise ValueError(f"completed verifier cases are unknown: {unknown_completed}")

    semaphore = asyncio.Semaphore(concurrency)

    async def evaluate(
        case: BenchmarkCase,
    ) -> tuple[str, VerifierObservation | None, str | None]:
        try:
            async with semaphore:
                judgment = await model.complete(
                    role=prompt.role,
                    system_prompt=prompt.system_prompt,
                    user_prompt=json.dumps(
                        {
                            "bundle": case.bundle.model_dump(mode="json"),
                            "evaluation_contract": {
                                "positive_class": "invalid_bundle",
                                "reward_semantics": (
                                    "Higher reward means greater confidence that the bundle "
                                    "is valid and should be accepted."
                                ),
                            },
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    response_model=BenchmarkVerifierJudgment,
                    prompt_version=prompt.version,
                )
            report = ReviewReport(
                reviewer=verifier,
                passed=judgment.passed,
                findings=judgment.findings,
                summary=judgment.summary,
            )
            observation = VerifierObservation(
                observation_id=f"llm:{verifier}:trial-{trial}:{case.case_id}",
                case_id=case.case_id,
                trial=trial,
                question_version_id=case.bundle.question.id,
                solution_version_id=case.bundle.solution.id,
                rubric_version_id=case.bundle.rubric.id,
                report=report,
                confidence=judgment.confidence,
                reward_candidate=judgment.reward_candidate,
                model=model_name,
                prompt_version=prompt.version,
            )
            return case.case_id, observation, None
        except Exception as exc:
            return case.case_id, None, str(exc)

    pending = [case for case in materialized_cases if case.case_id not in completed]
    tasks = [asyncio.create_task(evaluate(case)) for case in pending]
    observations: list[VerifierObservation] = []
    failures: dict[str, str] = {}
    for task in asyncio.as_completed(tasks):
        case_id, observation, error = await task
        if observation is not None:
            observations.append(observation)
            if on_observation is not None:
                on_observation(observation)
        else:
            failures[case_id] = error or "unknown verifier failure"
    case_order = {case.case_id: index for index, case in enumerate(materialized_cases)}
    observations.sort(key=lambda observation: case_order[observation.case_id])
    return BenchmarkVerifierRunResult(observations=observations, failures=failures)
