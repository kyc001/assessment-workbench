from argparse import ArgumentParser
from pathlib import Path

from assessment_workbench.benchmarking import (
    AttackKind,
    VerifierObservation,
    read_benchmark_cases,
    write_verifier_observations,
)
from assessment_workbench.domain import (
    FindingSeverity,
    FindingTarget,
    ReviewFinding,
    ReviewReport,
)

SYNTHETIC_OUTCOMES: dict[str, dict[AttackKind, tuple[bool, float]]] = {
    "surface_checker": {
        AttackKind.FORMAT_VALID_SEMANTIC_ERROR: (True, 0.82),
        AttackKind.LUCKY_ANSWER_WRONG_REASONING: (True, 0.91),
        AttackKind.SHARED_FALSE_PREMISE: (True, 0.88),
        AttackKind.RUBRIC_LOOPHOLE: (True, 0.86),
        AttackKind.UNDERSPECIFIED_QUESTION: (False, 0.24),
        AttackKind.DIFFICULTY_COVERAGE_GAMING: (False, 0.18),
    },
    "specialized_ensemble": {
        AttackKind.FORMAT_VALID_SEMANTIC_ERROR: (False, 0.15),
        AttackKind.LUCKY_ANSWER_WRONG_REASONING: (True, 0.72),
        AttackKind.SHARED_FALSE_PREMISE: (False, 0.12),
        AttackKind.RUBRIC_LOOPHOLE: (False, 0.20),
        AttackKind.UNDERSPECIFIED_QUESTION: (False, 0.08),
        AttackKind.DIFFICULTY_COVERAGE_GAMING: (False, 0.05),
    },
}


def build_observations(cases_path: Path) -> list[VerifierObservation]:
    observations: list[VerifierObservation] = []
    for case in read_benchmark_cases(cases_path):
        for verifier, outcomes in SYNTHETIC_OUTCOMES.items():
            passed = True
            reward_candidate = None
            findings: list[ReviewFinding] = []
            if case.attack_kind is not None:
                passed, reward_candidate = outcomes[case.attack_kind]
                if not passed:
                    findings = [
                        ReviewFinding(
                            code=f"synthetic_detected_{case.attack_kind.value}",
                            severity=FindingSeverity.ERROR,
                            target=FindingTarget.BUNDLE,
                            message=(
                                "Synthetic fixture rule rejects this controlled attack. "
                                "This is not a real model judgment."
                            ),
                        )
                    ]
            observations.append(
                VerifierObservation(
                    observation_id=f"synthetic:{verifier}:{case.case_id}",
                    case_id=case.case_id,
                    question_version_id=case.bundle.question.id,
                    solution_version_id=case.bundle.solution.id,
                    rubric_version_id=case.bundle.rubric.id,
                    report=ReviewReport(
                        reviewer=verifier,
                        passed=passed,
                        findings=findings,
                        summary="Deterministic synthetic fixture outcome.",
                    ),
                    confidence=1.0,
                    reward_candidate=reward_candidate,
                    model="synthetic-fixture",
                    prompt_version="synthetic-rules-v1",
                )
            )
    return observations


def main() -> None:
    parser = ArgumentParser()
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    write_verifier_observations(args.output, build_observations(args.cases))
    print(args.output)


if __name__ == "__main__":
    main()
