import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from assessment_workbench.benchmark_export import (
    RLVREpisode,
    RLVRPreferenceRecord,
    build_rlvr_episodes,
    build_rlvr_preferences,
    write_rlvr_episodes,
    write_rlvr_preferences,
)
from assessment_workbench.benchmarking import (
    DeterministicBaseline,
    calculate_benchmark_experiment_report,
    calculate_benchmark_multi_trial_report,
    generate_deterministic_baseline_observations,
    read_benchmark_cases,
    read_verifier_observations,
    write_verifier_observations,
)
from assessment_workbench.cli import app


def _fixture_cases() -> list:
    root = Path(__file__).parents[1] / "examples" / "verifier-benchmark"
    return read_benchmark_cases(root / "cases.jsonl")


def test_build_rlvr_episodes_maps_environment_contracts() -> None:
    cases = _fixture_cases()
    observations = generate_deterministic_baseline_observations(
        cases,
        baselines=[DeterministicBaseline.STRUCTURE],
    )

    episodes = build_rlvr_episodes(cases, observations)

    assert len(episodes) == len(cases)
    assert episodes[0].environment_valid is True
    assert episodes[0].attack_kind is None
    assert episodes[1].environment_valid is False
    assert episodes[1].parent_episode_id == episodes[0].episode_id
    assert episodes[1].candidate_index == 1
    assert episodes[0].task == cases[0].bundle.question
    assert episodes[0].reference == cases[0].bundle.solution
    assert episodes[0].reward_spec == cases[0].bundle.rubric
    assert episodes[0].reward_vector == {"structure:trial-1": 1.0}


def test_build_rlvr_preferences_records_reward_hacking_signals() -> None:
    cases = _fixture_cases()
    observations = generate_deterministic_baseline_observations(
        cases,
        baselines=[DeterministicBaseline.SCHEMA_ONLY],
    )

    preferences = build_rlvr_preferences(
        cases,
        observations,
        verifiers=[DeterministicBaseline.SCHEMA_ONLY.value],
    )

    assert len(preferences) == 6
    assert all(record.chosen_case_id == cases[0].case_id for record in preferences)
    assert all(record.oracle_preference == "chosen" for record in preferences)
    assert all(record.verifier_signals[0].reward_margin == 0.0 for record in preferences)
    assert all(record.verifier_signals[0].verifier_prefers_rejected for record in preferences)
    assert all(record.verifier_signals[0].rejected_accepted for record in preferences)


def test_build_rlvr_preferences_requires_clean_and_attack_rewards() -> None:
    cases = _fixture_cases()
    observations = generate_deterministic_baseline_observations(
        cases,
        baselines=[DeterministicBaseline.SCHEMA_ONLY],
    )
    observations[0] = observations[0].model_copy(update={"reward_candidate": None})

    with pytest.raises(ValueError, match="requires reward candidates"):
        build_rlvr_preferences(cases, observations)


def test_rlvr_export_writers_round_trip_jsonl(tmp_path: Path) -> None:
    cases = _fixture_cases()
    observations = generate_deterministic_baseline_observations(cases)
    episodes = build_rlvr_episodes(cases, observations)
    preferences = build_rlvr_preferences(cases, observations)

    episode_path = write_rlvr_episodes(tmp_path / "episodes.jsonl", episodes)
    preference_path = write_rlvr_preferences(
        tmp_path / "preferences.jsonl",
        preferences,
    )

    restored_episodes = [
        RLVREpisode.model_validate_json(line)
        for line in episode_path.read_text(encoding="utf-8").splitlines()
    ]
    restored_preferences = [
        RLVRPreferenceRecord.model_validate_json(line)
        for line in preference_path.read_text(encoding="utf-8").splitlines()
    ]
    assert restored_episodes == episodes
    assert restored_preferences == preferences


def test_benchmark_export_cli_writes_episodes_and_preferences(tmp_path: Path) -> None:
    fixture_root = Path(__file__).parents[1] / "examples" / "verifier-benchmark"
    cases_path = fixture_root / "cases.jsonl"
    cases = read_benchmark_cases(cases_path)
    observations_path = write_verifier_observations(
        tmp_path / "observations.jsonl",
        generate_deterministic_baseline_observations(
            cases,
            baselines=[DeterministicBaseline.STRUCTURE],
        ),
    )
    episodes_path = tmp_path / "episodes.jsonl"
    preferences_path = tmp_path / "preferences.jsonl"

    episode_result = CliRunner().invoke(
        app,
        [
            "benchmark",
            "export-episodes",
            "--cases",
            str(cases_path),
            "--observations",
            str(observations_path),
            "--output",
            str(episodes_path),
        ],
    )
    preference_result = CliRunner().invoke(
        app,
        [
            "benchmark",
            "export-preferences",
            "--cases",
            str(cases_path),
            "--observations",
            str(observations_path),
            "--verifier",
            "structure",
            "--output",
            str(preferences_path),
        ],
    )

    assert episode_result.exit_code == 0
    assert preference_result.exit_code == 0
    assert len(episodes_path.read_text(encoding="utf-8").splitlines()) == 7
    assert len(preferences_path.read_text(encoding="utf-8").splitlines()) == 6
    first_preference = json.loads(
        preferences_path.read_text(encoding="utf-8").splitlines()[0]
    )
    assert first_preference["schema_version"] == "rlvr-preference-v1"


def test_committed_baseline_artifacts_replay_exactly() -> None:
    fixture_root = Path(__file__).parents[1] / "examples" / "verifier-benchmark"
    cases = read_benchmark_cases(fixture_root / "cases.jsonl")
    observations = generate_deterministic_baseline_observations(cases)
    episodes = build_rlvr_episodes(cases, observations)
    preferences = build_rlvr_preferences(cases, observations)
    report = calculate_benchmark_experiment_report(
        cases,
        observations,
        verifiers=[baseline.value for baseline in DeterministicBaseline],
    )

    committed_observations = [
        json.loads(line)
        for line in (fixture_root / "observations.baseline.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    committed_episodes = [
        json.loads(line)
        for line in (fixture_root / "episodes.baseline.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    committed_preferences = [
        json.loads(line)
        for line in (fixture_root / "preferences.baseline.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    committed_report = json.loads(
        (fixture_root / "report.baseline.json").read_text(encoding="utf-8")
    )

    assert [item.model_dump(mode="json") for item in observations] == committed_observations
    assert [item.model_dump(mode="json") for item in episodes] == committed_episodes
    assert [item.model_dump(mode="json") for item in preferences] == committed_preferences
    assert report.model_dump(mode="json") == committed_report


def test_committed_gemini_flash_artifacts_replay_exactly() -> None:
    fixture_root = Path(__file__).parents[1] / "examples" / "verifier-benchmark"
    cases = read_benchmark_cases(fixture_root / "cases.jsonl")
    observations = read_verifier_observations(
        fixture_root / "observations.gemini-flash.jsonl"
    )

    assert len(observations) == 21
    multi_trial_report = calculate_benchmark_multi_trial_report(
        cases,
        observations,
        verifiers=["gemini_flash"],
    )
    committed_multi_trial = json.loads(
        (fixture_root / "report.gemini-flash.multi-trial.json").read_text(
            encoding="utf-8"
        )
    )
    assert multi_trial_report.model_dump(mode="json") == committed_multi_trial

    episodes = build_rlvr_episodes(cases, observations)
    committed_episodes = [
        json.loads(line)
        for line in (fixture_root / "episodes.gemini-flash.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [item.model_dump(mode="json") for item in episodes] == committed_episodes

    for trial in (1, 2, 3):
        report = calculate_benchmark_experiment_report(
            cases,
            observations,
            verifiers=["gemini_flash"],
            trial=trial,
        )
        committed_report = json.loads(
            (fixture_root / f"report.gemini-flash.trial-{trial}.json").read_text(
                encoding="utf-8"
            )
        )
        assert report.model_dump(mode="json") == committed_report

        preferences = build_rlvr_preferences(
            cases,
            observations,
            verifiers=["gemini_flash"],
            trial=trial,
        )
        committed_preferences = [
            json.loads(line)
            for line in (
                fixture_root / f"preferences.gemini-flash.trial-{trial}.jsonl"
            )
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        assert [
            item.model_dump(mode="json") for item in preferences
        ] == committed_preferences
