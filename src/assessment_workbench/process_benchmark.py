import asyncio
import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError, model_validator

from assessment_workbench.domain import StrictModel
from assessment_workbench.ports import StructuredModel
from assessment_workbench.prompting import PromptBundle

PROCESS_CASE_SCHEMA_VERSION: Literal["process-benchmark-case-v1"] = (
    "process-benchmark-case-v1"
)
PROCESS_OBSERVATION_SCHEMA_VERSION: Literal["process-verifier-observation-v1"] = (
    "process-verifier-observation-v1"
)
PROCESS_REPORT_SCHEMA_VERSION: Literal["process-benchmark-report-v1"] = (
    "process-benchmark-report-v1"
)
PROCESSBENCH_DATASET_URL = "https://huggingface.co/datasets/Qwen/ProcessBench"
PROCESSBENCH_LICENSE = "Apache-2.0"


class ProcessSamplingStrategy(StrEnum):
    HEAD = "head"
    BALANCED = "balanced"
    DIAGNOSTIC = "diagnostic"


class ProcessBenchSourceRecord(StrictModel):
    id: str = Field(min_length=1)
    generator: str = Field(min_length=1)
    problem: str = Field(min_length=1)
    steps: list[str] = Field(min_length=1)
    final_answer_correct: bool
    label: int = Field(ge=-1)

    @model_validator(mode="after")
    def validate_first_error_step(self) -> "ProcessBenchSourceRecord":
        if self.label >= len(self.steps):
            raise ValueError("ProcessBench label must reference an existing step or be -1")
        return self


class ProcessBenchmarkCase(StrictModel):
    schema_version: Literal["process-benchmark-case-v1"] = PROCESS_CASE_SCHEMA_VERSION
    case_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    source_dataset: str = Field(min_length=1)
    source_split: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    source_url: str = Field(min_length=1)
    source_license: str = Field(min_length=1)
    generator: str = Field(min_length=1)
    problem: str = Field(min_length=1)
    steps: list[str] = Field(min_length=1)
    first_error_step: int = Field(ge=-1)
    final_answer_correct: bool

    @model_validator(mode="after")
    def validate_first_error_step(self) -> "ProcessBenchmarkCase":
        if self.first_error_step >= len(self.steps):
            raise ValueError("first_error_step must reference an existing step or be -1")
        return self


class ProcessVerifierJudgment(StrictModel):
    first_error_step: int = Field(ge=-1)
    confidence: float = Field(ge=0, le=1)
    rationale: str = Field(min_length=1)
    error_summary: str | None = None

    @model_validator(mode="after")
    def validate_error_summary(self) -> "ProcessVerifierJudgment":
        if self.first_error_step == -1 and self.error_summary is not None:
            raise ValueError("all-correct judgment cannot include an error summary")
        if self.first_error_step >= 0 and not self.error_summary:
            raise ValueError("error judgment requires an error summary")
        return self


class ProcessVerifierObservation(StrictModel):
    schema_version: Literal["process-verifier-observation-v1"] = (
        PROCESS_OBSERVATION_SCHEMA_VERSION
    )
    observation_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    case_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    verifier: str = Field(min_length=1)
    trial: int = Field(default=1, ge=1)
    predicted_first_error_step: int = Field(ge=-1)
    confidence: float = Field(ge=0, le=1)
    rationale: str = Field(min_length=1)
    error_summary: str | None = None
    model: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)


class ProcessBenchmarkDatasetSummary(StrictModel):
    total_cases: int = Field(ge=1)
    correct_processes: int = Field(ge=0)
    erroneous_processes: int = Field(ge=0)
    final_answer_traps: int = Field(ge=0)


class ProcessVerifierMetrics(StrictModel):
    verifier: str
    trial: int = Field(ge=1)
    exact_localization_accuracy: float = Field(ge=0, le=1)
    error_detection_recall: float | None = Field(default=None, ge=0, le=1)
    correct_process_acceptance_rate: float | None = Field(default=None, ge=0, le=1)
    error_localization_accuracy: float | None = Field(default=None, ge=0, le=1)
    final_answer_trap_localization_accuracy: float | None = Field(default=None, ge=0, le=1)
    mean_absolute_step_error_on_detected: float | None = Field(default=None, ge=0)


class ProcessBenchmarkReport(StrictModel):
    schema_version: Literal["process-benchmark-report-v1"] = PROCESS_REPORT_SCHEMA_VERSION
    dataset: ProcessBenchmarkDatasetSummary
    metrics: ProcessVerifierMetrics


@dataclass(frozen=True)
class ProcessVerifierRunResult:
    observations: list[ProcessVerifierObservation]
    failures: dict[str, str]


def import_processbench_cases(
    source: Path,
    *,
    split: str,
    limit: int | None = None,
    sampling: ProcessSamplingStrategy = ProcessSamplingStrategy.HEAD,
) -> list[ProcessBenchmarkCase]:
    if not split.strip():
        raise ValueError("ProcessBench split cannot be empty")
    if limit is not None and limit < 1:
        raise ValueError("ProcessBench import limit must be at least 1")
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid ProcessBench JSON: {exc}") from exc
    if not isinstance(payload, list) or not payload:
        raise ValueError("ProcessBench source must contain a non-empty JSON array")
    records: list[ProcessBenchSourceRecord] = []
    for index, item in enumerate(payload):
        try:
            records.append(ProcessBenchSourceRecord.model_validate(item))
        except ValidationError as exc:
            raise ValueError(f"invalid ProcessBench record at index {index}: {exc}") from exc
    selected = _sample_processbench_records(records, limit=limit, strategy=sampling)
    cases = [
        ProcessBenchmarkCase(
            case_id=f"processbench.{split}.{record.id}",
            source_dataset="Qwen/ProcessBench",
            source_split=split,
            source_id=record.id,
            source_url=PROCESSBENCH_DATASET_URL,
            source_license=PROCESSBENCH_LICENSE,
            generator=record.generator,
            problem=record.problem,
            steps=record.steps,
            first_error_step=record.label,
            final_answer_correct=record.final_answer_correct,
        )
        for record in selected
    ]
    _validate_case_ids(cases)
    return cases


def write_process_cases(path: Path, cases: Iterable[ProcessBenchmarkCase]) -> Path:
    materialized = list(cases)
    _validate_case_ids(materialized)
    return _write_jsonl(path, materialized)


def read_process_cases(path: Path) -> list[ProcessBenchmarkCase]:
    cases = _read_jsonl(path, ProcessBenchmarkCase, "process benchmark case")
    _validate_case_ids(cases)
    return cases


def write_process_observations(
    path: Path,
    observations: Iterable[ProcessVerifierObservation],
) -> Path:
    materialized = list(observations)
    _validate_observation_ids(materialized)
    return _write_jsonl(path, materialized)


def read_process_observations(path: Path) -> list[ProcessVerifierObservation]:
    observations = _read_jsonl(path, ProcessVerifierObservation, "process observation")
    _validate_observation_ids(observations)
    return observations


async def run_process_verifier(
    cases: Iterable[ProcessBenchmarkCase],
    model: StructuredModel,
    *,
    prompt: PromptBundle,
    verifier: str,
    model_name: str,
    trial: int = 1,
    concurrency: int = 4,
    max_new_cases: int | None = None,
    completed_case_ids: Iterable[str] = (),
    on_observation: Callable[[ProcessVerifierObservation], None] | None = None,
) -> ProcessVerifierRunResult:
    materialized_cases = list(cases)
    _validate_case_ids(materialized_cases)
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", verifier) is None:
        raise ValueError("process verifier id must be safe for observation identifiers")
    if not model_name.strip():
        raise ValueError("process verifier model name cannot be empty")
    if trial < 1:
        raise ValueError("process verifier trial must be at least 1")
    if concurrency < 1:
        raise ValueError("process verifier concurrency must be at least 1")
    if max_new_cases is not None and max_new_cases < 1:
        raise ValueError("max_new_cases must be at least 1")
    completed = set(completed_case_ids)
    known_case_ids = {case.case_id for case in materialized_cases}
    unknown_completed = sorted(completed - known_case_ids)
    if unknown_completed:
        raise ValueError(f"completed process verifier cases are unknown: {unknown_completed}")

    semaphore = asyncio.Semaphore(concurrency)

    async def evaluate(
        case: ProcessBenchmarkCase,
    ) -> tuple[str, ProcessVerifierObservation | None, str | None]:
        try:
            async with semaphore:
                judgment = await model.complete(
                    role=prompt.role,
                    system_prompt=prompt.system_prompt,
                    user_prompt=json.dumps(
                        {
                            "problem": case.problem,
                            "steps": [
                                {"index": index, "content": step}
                                for index, step in enumerate(case.steps)
                            ],
                            "output_contract": {
                                "first_error_step": (
                                    "Zero-based index of the earliest incorrect step, or -1 if "
                                    "every step is correct."
                                )
                            },
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    response_model=ProcessVerifierJudgment,
                    prompt_version=prompt.version,
                )
            if judgment.first_error_step >= len(case.steps):
                raise ValueError(
                    "predicted first error step is outside the supplied solution: "
                    f"{judgment.first_error_step}"
                )
            observation = ProcessVerifierObservation(
                observation_id=f"process:{verifier}:trial-{trial}:{case.case_id}",
                case_id=case.case_id,
                verifier=verifier,
                trial=trial,
                predicted_first_error_step=judgment.first_error_step,
                confidence=judgment.confidence,
                rationale=judgment.rationale,
                error_summary=judgment.error_summary,
                model=model_name,
                prompt_version=prompt.version,
            )
            return case.case_id, observation, None
        except Exception as exc:
            return case.case_id, None, str(exc)

    pending = [case for case in materialized_cases if case.case_id not in completed]
    if max_new_cases is not None:
        pending = pending[:max_new_cases]
    tasks = [asyncio.create_task(evaluate(case)) for case in pending]
    observations: list[ProcessVerifierObservation] = []
    failures: dict[str, str] = {}
    for task in asyncio.as_completed(tasks):
        case_id, observation, error = await task
        if observation is None:
            failures[case_id] = error or "unknown process verifier failure"
            continue
        observations.append(observation)
        if on_observation is not None:
            on_observation(observation)
    case_order = {case.case_id: index for index, case in enumerate(materialized_cases)}
    observations.sort(key=lambda observation: case_order[observation.case_id])
    return ProcessVerifierRunResult(observations=observations, failures=failures)


def calculate_process_benchmark_report(
    cases: Iterable[ProcessBenchmarkCase],
    observations: Iterable[ProcessVerifierObservation],
    *,
    verifier: str,
    trial: int = 1,
) -> ProcessBenchmarkReport:
    materialized_cases = list(cases)
    _validate_case_ids(materialized_cases)
    case_by_id = {case.case_id: case for case in materialized_cases}
    selected = [
        observation
        for observation in observations
        if observation.verifier == verifier and observation.trial == trial
    ]
    if len(selected) != len(materialized_cases):
        raise ValueError(
            "process report requires exactly one selected observation per benchmark case"
        )
    observation_by_case: dict[str, ProcessVerifierObservation] = {}
    for observation in selected:
        if observation.case_id not in case_by_id:
            raise ValueError(
                f"process observation references unknown case: {observation.case_id}"
            )
        if observation.case_id in observation_by_case:
            raise ValueError(
                f"duplicate selected process observation for case: {observation.case_id}"
            )
        observation_by_case[observation.case_id] = observation

    correct_cases = [case for case in materialized_cases if case.first_error_step == -1]
    error_cases = [case for case in materialized_cases if case.first_error_step >= 0]
    trap_cases = [case for case in error_cases if case.final_answer_correct]
    exact = 0
    detected_errors = 0
    accepted_correct = 0
    localized_errors = 0
    localized_traps = 0
    absolute_step_errors: list[int] = []
    for case in materialized_cases:
        prediction = observation_by_case[case.case_id].predicted_first_error_step
        if prediction == case.first_error_step:
            exact += 1
        if case.first_error_step == -1:
            if prediction == -1:
                accepted_correct += 1
            continue
        if prediction >= 0:
            detected_errors += 1
            absolute_step_errors.append(abs(prediction - case.first_error_step))
        if prediction == case.first_error_step:
            localized_errors += 1
            if case.final_answer_correct:
                localized_traps += 1

    return ProcessBenchmarkReport(
        dataset=ProcessBenchmarkDatasetSummary(
            total_cases=len(materialized_cases),
            correct_processes=len(correct_cases),
            erroneous_processes=len(error_cases),
            final_answer_traps=len(trap_cases),
        ),
        metrics=ProcessVerifierMetrics(
            verifier=verifier,
            trial=trial,
            exact_localization_accuracy=exact / len(materialized_cases),
            error_detection_recall=(
                detected_errors / len(error_cases) if error_cases else None
            ),
            correct_process_acceptance_rate=(
                accepted_correct / len(correct_cases) if correct_cases else None
            ),
            error_localization_accuracy=(
                localized_errors / len(error_cases) if error_cases else None
            ),
            final_answer_trap_localization_accuracy=(
                localized_traps / len(trap_cases) if trap_cases else None
            ),
            mean_absolute_step_error_on_detected=(
                sum(absolute_step_errors) / len(absolute_step_errors)
                if absolute_step_errors
                else None
            ),
        ),
    )


def _sample_processbench_records(
    records: list[ProcessBenchSourceRecord],
    *,
    limit: int | None,
    strategy: ProcessSamplingStrategy,
) -> list[ProcessBenchSourceRecord]:
    if limit is None or limit >= len(records):
        return records
    if strategy is ProcessSamplingStrategy.HEAD:
        return records[:limit]
    correct = [record for record in records if record.label == -1]
    errors = [record for record in records if record.label >= 0]
    if strategy is ProcessSamplingStrategy.BALANCED:
        error_target = limit // 2
        correct_target = limit - error_target
        return _interleave(errors[:error_target], correct[:correct_target])

    traps = [record for record in errors if record.final_answer_correct]
    ordinary_errors = [record for record in errors if not record.final_answer_correct]
    trap_target = min(len(traps), max(1, limit // 4))
    remaining = limit - trap_target
    error_target = remaining // 2
    correct_target = remaining - error_target
    selected = [
        *traps[:trap_target],
        *ordinary_errors[:error_target],
        *correct[:correct_target],
    ]
    if len(selected) < limit:
        selected_ids = {record.id for record in selected}
        selected.extend(record for record in records if record.id not in selected_ids)
    return selected[:limit]


def _interleave(
    left: list[ProcessBenchSourceRecord],
    right: list[ProcessBenchSourceRecord],
) -> list[ProcessBenchSourceRecord]:
    result: list[ProcessBenchSourceRecord] = []
    for index in range(max(len(left), len(right))):
        if index < len(left):
            result.append(left[index])
        if index < len(right):
            result.append(right[index])
    return result


def _read_jsonl[ModelT: StrictModel](
    path: Path,
    model: type[ModelT],
    label: str,
) -> list[ModelT]:
    records: list[ModelT] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            records.append(model.model_validate_json(line))
        except ValidationError as exc:
            raise ValueError(f"invalid {label} at line {line_number}: {exc}") from exc
    if not records:
        raise ValueError(f"{label} dataset requires at least one record")
    return records


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


def _validate_case_ids(cases: list[ProcessBenchmarkCase]) -> None:
    if not cases:
        raise ValueError("process benchmark requires at least one case")
    case_ids = [case.case_id for case in cases]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("process benchmark case ids must be unique")


def _validate_observation_ids(observations: list[ProcessVerifierObservation]) -> None:
    if not observations:
        raise ValueError("process observation dataset requires at least one observation")
    observation_ids = [observation.observation_id for observation in observations]
    if len(observation_ids) != len(set(observation_ids)):
        raise ValueError("process observation ids must be unique")
