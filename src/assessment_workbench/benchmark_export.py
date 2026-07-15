from collections.abc import Iterable
from pathlib import Path
from typing import Literal

from pydantic import Field

from assessment_workbench.benchmarking import (
    AttackKind,
    BenchmarkCase,
    BenchmarkOracle,
    VerifierObservation,
    validate_benchmark_dataset,
)
from assessment_workbench.domain import (
    ExamQuestionBundle,
    QuestionVersion,
    RubricVersion,
    SolutionVersion,
    StrictModel,
)

RLVR_EPISODE_SCHEMA_VERSION: Literal["rlvr-episode-v1"] = "rlvr-episode-v1"
RLVR_PREFERENCE_SCHEMA_VERSION: Literal["rlvr-preference-v1"] = "rlvr-preference-v1"


class RLVREpisode(StrictModel):
    schema_version: Literal["rlvr-episode-v1"] = RLVR_EPISODE_SCHEMA_VERSION
    episode_id: str = Field(min_length=1)
    task: QuestionVersion
    reference: SolutionVersion
    reward_spec: RubricVersion
    oracle: BenchmarkOracle
    environment_valid: bool
    attack_kind: AttackKind | None = None
    parent_episode_id: str | None = None
    candidate_index: int | None = Field(default=None, ge=1)
    verifier_observations: list[VerifierObservation] = Field(default_factory=list)
    reward_vector: dict[str, float] = Field(default_factory=dict)
    source_run_id: str | None = None
    source_artifact_id: str | None = None


class VerifierPreferenceSignal(StrictModel):
    verifier: str
    trial: int = Field(ge=1)
    chosen_reward: float
    rejected_reward: float
    reward_margin: float
    verifier_prefers_rejected: bool
    rejected_accepted: bool


class RLVRPreferenceRecord(StrictModel):
    schema_version: Literal["rlvr-preference-v1"] = RLVR_PREFERENCE_SCHEMA_VERSION
    preference_id: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    chosen_case_id: str = Field(min_length=1)
    rejected_case_id: str = Field(min_length=1)
    chosen: ExamQuestionBundle
    rejected: ExamQuestionBundle
    attack_kind: AttackKind
    oracle_preference: Literal["chosen"] = "chosen"
    oracle_rationale: str = Field(min_length=1)
    verifier_signals: list[VerifierPreferenceSignal] = Field(min_length=1)


def build_rlvr_episodes(
    cases: Iterable[BenchmarkCase],
    observations: Iterable[VerifierObservation] = (),
) -> list[RLVREpisode]:
    materialized_cases = list(cases)
    validate_benchmark_dataset(materialized_cases)
    observation_map = _bind_observations(materialized_cases, observations)
    episodes: list[RLVREpisode] = []
    for case in materialized_cases:
        case_observations = sorted(
            (
                observation
                for (case_id, _, _), observation in observation_map.items()
                if case_id == case.case_id
            ),
            key=lambda observation: (observation.report.reviewer, observation.trial),
        )
        reward_vector = {
            f"{observation.report.reviewer}:trial-{observation.trial}": (
                observation.reward_candidate
            )
            for observation in case_observations
            if observation.reward_candidate is not None
        }
        episodes.append(
            RLVREpisode(
                episode_id=case.case_id,
                task=case.bundle.question,
                reference=case.bundle.solution,
                reward_spec=case.bundle.rubric,
                oracle=case.oracle,
                environment_valid=case.attack_kind is None,
                attack_kind=case.attack_kind,
                parent_episode_id=case.parent_case_id,
                candidate_index=case.candidate_index,
                verifier_observations=case_observations,
                reward_vector=reward_vector,
                source_run_id=str(case.source_run_id) if case.source_run_id else None,
                source_artifact_id=(
                    str(case.source_artifact_id) if case.source_artifact_id else None
                ),
            )
        )
    return episodes


def build_rlvr_preferences(
    cases: Iterable[BenchmarkCase],
    observations: Iterable[VerifierObservation],
    *,
    verifiers: Iterable[str] | None = None,
    trial: int = 1,
) -> list[RLVRPreferenceRecord]:
    materialized_cases = list(cases)
    validate_benchmark_dataset(materialized_cases)
    observation_map = _bind_observations(materialized_cases, observations)
    case_by_id = {case.case_id: case for case in materialized_cases}
    selected_verifiers = (
        list(verifiers)
        if verifiers is not None
        else sorted(
            {
                verifier
                for _, verifier, observation_trial in observation_map
                if observation_trial == trial
            }
        )
    )
    if not selected_verifiers:
        raise ValueError("RLVR preference export requires at least one verifier")
    if len(selected_verifiers) != len(set(selected_verifiers)):
        raise ValueError("RLVR preference verifier ids must be unique")

    preferences: list[RLVRPreferenceRecord] = []
    for attacked in materialized_cases:
        if attacked.attack_kind is None:
            continue
        assert attacked.parent_case_id is not None
        clean = case_by_id[attacked.parent_case_id]
        signals: list[VerifierPreferenceSignal] = []
        for verifier in selected_verifiers:
            clean_observation = observation_map.get((clean.case_id, verifier, trial))
            attacked_observation = observation_map.get((attacked.case_id, verifier, trial))
            if clean_observation is None or attacked_observation is None:
                raise ValueError(
                    f"RLVR preference export is missing {verifier} observations for "
                    f"{clean.case_id} -> {attacked.case_id}"
                )
            if (
                clean_observation.reward_candidate is None
                or attacked_observation.reward_candidate is None
            ):
                raise ValueError(
                    f"RLVR preference export requires reward candidates for {verifier}: "
                    f"{attacked.case_id}"
                )
            chosen_reward = clean_observation.reward_candidate
            rejected_reward = attacked_observation.reward_candidate
            signals.append(
                VerifierPreferenceSignal(
                    verifier=verifier,
                    trial=trial,
                    chosen_reward=chosen_reward,
                    rejected_reward=rejected_reward,
                    reward_margin=chosen_reward - rejected_reward,
                    verifier_prefers_rejected=rejected_reward >= chosen_reward,
                    rejected_accepted=attacked_observation.report.passed,
                )
            )
        preferences.append(
            RLVRPreferenceRecord(
                preference_id=f"preference:{clean.case_id}:{attacked.case_id}",
                prompt=(
                    "Choose the semantically valid Question-Solution-Rubric environment."
                ),
                chosen_case_id=clean.case_id,
                rejected_case_id=attacked.case_id,
                chosen=clean.bundle,
                rejected=attacked.bundle,
                attack_kind=attacked.attack_kind,
                oracle_rationale=attacked.oracle.rationale,
                verifier_signals=signals,
            )
        )
    return preferences


def write_rlvr_episodes(path: Path, episodes: Iterable[RLVREpisode]) -> Path:
    return _write_records(path, episodes, "RLVR episode export")


def write_rlvr_preferences(
    path: Path,
    preferences: Iterable[RLVRPreferenceRecord],
) -> Path:
    return _write_records(path, preferences, "RLVR preference export")


def _bind_observations(
    cases: list[BenchmarkCase],
    observations: Iterable[VerifierObservation],
) -> dict[tuple[str, str, int], VerifierObservation]:
    case_by_id = {case.case_id: case for case in cases}
    bound: dict[tuple[str, str, int], VerifierObservation] = {}
    observation_ids: set[str] = set()
    for observation in observations:
        if observation.observation_id in observation_ids:
            raise ValueError("RLVR export observation ids must be unique")
        observation_ids.add(observation.observation_id)
        case = case_by_id.get(observation.case_id)
        if case is None:
            raise ValueError(
                f"RLVR export observation references unknown case: {observation.case_id}"
            )
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
            raise ValueError(
                f"RLVR export observation version mismatch: {observation.case_id}"
            )
        key = (observation.case_id, observation.report.reviewer, observation.trial)
        if key in bound:
            raise ValueError(
                f"RLVR export has duplicate case-verifier-trial observation: {key}"
            )
        bound[key] = observation
    return bound


def _write_records(
    path: Path,
    records: Iterable[StrictModel],
    label: str,
) -> Path:
    materialized = list(records)
    if not materialized:
        raise ValueError(f"{label} requires at least one record")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            for record in materialized:
                handle.write(record.model_dump_json())
                handle.write("\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return path
