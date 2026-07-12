from collections import defaultdict
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from assessment_workbench.agents import ExamAgentWorkflow, ModelRouter
from assessment_workbench.domain import (
    ArbitrationAction,
    ArbitrationDecision,
    BlueprintDraft,
    CoverageTarget,
    DifficultyDistribution,
    ExamSectionBlueprint,
    QuestionDraft,
    QuestionType,
    ReviewFinding,
    ReviewReport,
    RubricDraft,
    RubricItem,
    RunStatus,
    SolutionDraft,
    SolutionStep,
    SubjectProfileCandidate,
)
from assessment_workbench.storage import ArtifactStore, RunStore, Workspace


class FixtureModel:
    def __init__(self, responses: dict[str, list[BaseModel]]) -> None:
        self.responses = responses
        self.calls: dict[str, int] = defaultdict(int)

    async def complete(self, **kwargs: Any) -> BaseModel:
        role = str(kwargs["role"])
        index = self.calls[role]
        self.calls[role] += 1
        return self.responses[role][index]


async def test_exam_agents_retry_only_invalid_dependency(tmp_path: Path) -> None:
    profile = SubjectProfileCandidate(
        subject_id="mathematics",
        display_name="Mathematics",
        supported_question_types=[QuestionType.CALCULATION],
        reviewers=["solvability", "structure"],
        difficulty_dimensions=["reasoning"],
        source_summary="User requirements",
    )
    blueprint = BlueprintDraft(
        title="Generated assessment",
        target_level="Grade 12",
        duration_minutes=30,
        total_score=10,
        sections=[
            ExamSectionBlueprint(
                id="calculation",
                title="Calculation",
                question_type=QuestionType.CALCULATION,
                count=1,
                score_each=10,
                topic_tags=["algebra"],
            )
        ],
        coverage=[CoverageTarget(topic_tag="algebra", target_score=10)],
        difficulty_distribution=DifficultyDistribution(easy=0, medium=1, hard=0),
    )
    question = QuestionDraft(statement="Solve x + 2 = 5.", topic_tags=["algebra"])
    wrong_solution = SolutionDraft(
        steps=[SolutionStep(id="s1", description="Subtract two")],
        final_answer="x = 4",
    )
    correct_solution = SolutionDraft(
        steps=[SolutionStep(id="s1", description="Subtract two from both sides")],
        final_answer="x = 3",
    )
    rubric = RubricDraft(
        items=[
            RubricItem(id="r1", description="Sets up the equation", score=4),
            RubricItem(id="r2", description="Obtains the answer", score=6),
        ]
    )
    failed_review = ReviewReport(
        reviewer="solvability",
        passed=False,
        findings=[
            ReviewFinding(
                code="wrong_answer",
                severity="error",
                target="solution",
                message="The final answer is incorrect.",
            )
        ],
    )
    passed_review = ReviewReport(reviewer="solvability", passed=True)
    retry = ArbitrationDecision(
        action=ArbitrationAction.RETRY_SOLUTION,
        rationale="The question is valid but the solution is wrong.",
        finding_codes=["wrong_answer"],
        solver_feedback=["Recompute the final value."],
    )
    passed = ArbitrationDecision(action=ArbitrationAction.PASS, rationale="All checks pass.")
    standard = FixtureModel(
        {
            "question_writer": [question],
            "rubric_builder": [rubric, rubric],
            "solvability_reviewer": [failed_review, passed_review],
        }
    )
    strong = FixtureModel(
        {
            "subject_researcher": [profile],
            "exam_blueprint_planner": [blueprint],
            "independent_solver": [wrong_solution, correct_solution],
            "question_arbiter": [retry, passed],
        }
    )
    workspace = Workspace(tmp_path / "workspace")
    workspace.initialize()
    run, state = await ExamAgentWorkflow(
        ModelRouter(standard=standard, strong=strong),  # type: ignore[arg-type]
        ArtifactStore(workspace),
        RunStore(workspace),
    ).execute(subject="mathematics", target_level="Grade 12", requirements="One question")

    assert run.status is RunStatus.SUCCEEDED
    assert state["exam"].questions[0].solution.final_answer == "x = 3"
    assert standard.calls["question_writer"] == 1
    assert strong.calls["independent_solver"] == 2
    assert standard.calls["rubric_builder"] == 2
