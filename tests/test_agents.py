import json
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
    ExamBlueprint,
    ExamPlanningMode,
    ExamSectionBlueprint,
    HumanDecision,
    HumanDecisionType,
    QuestionDraft,
    QuestionPart,
    QuestionPlanDraft,
    QuestionPlanSetDraft,
    QuestionType,
    ReviewerName,
    ReviewFinding,
    ReviewReport,
    RubricDraft,
    RubricItem,
    RunStatus,
    SolutionDraft,
    SolutionStep,
    SubjectProfile,
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


class StopAtQuestionPlanningModel:
    def __init__(self) -> None:
        self.roles: list[str] = []

    async def complete(self, **kwargs: Any) -> BaseModel:
        role = str(kwargs["role"])
        self.roles.append(role)
        raise RuntimeError("stop after capability planning")


async def test_gaokao_request_uses_locked_capability_before_model_planning(
    tmp_path: Path,
) -> None:
    model = StopAtQuestionPlanningModel()
    workspace = Workspace(tmp_path / "workspace")
    workspace.initialize()
    workflow = ExamAgentWorkflow(
        ModelRouter(standard=model, strong=model),  # type: ignore[arg-type]
        ArtifactStore(workspace),
        RunStore(workspace),
    )

    run, state = await workflow.execute(
        subject="高考数学",
        target_level="高中毕业年级",
        requirements="生成一份完整模拟卷",
        require_blueprint_approval=True,
    )

    assert run.status is RunStatus.FAILED
    assert model.roles == ["question_set_planner"]
    assert state["planning"].mode is ExamPlanningMode.CAPABILITY
    assert state["planning"].capability_id == "gaokao-mathematics"
    assert state["blueprint"].total_score == 150
    assert sum(section.count for section in state["blueprint"].sections) == 19


async def test_exam_agents_retry_only_invalid_dependency(tmp_path: Path) -> None:
    profile = SubjectProfileCandidate(
        subject_id="mathematics",
        display_name="Mathematics",
        supported_question_types=[QuestionType.CALCULATION],
        reviewers=[ReviewerName.SOLVABILITY, ReviewerName.STRUCTURE],
        difficulty_dimensions=["reasoning"],
        conventions=["Use standard algebraic notation"],
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
    question_plans = QuestionPlanSetDraft(
        plans=[
            QuestionPlanDraft(
                section_id="calculation",
                slot=1,
                topic_tags=["algebra"],
                primary_skill="Solve a linear equation",
                design_brief="Use a direct one-variable equation.",
                difficulty="medium",
                estimated_minutes=10,
                answer_form="A single real value",
                solution_outline=["Isolate the variable"],
                rubric_focus=["Correct transformation", "Correct result"],
                verification_methods=["Substitute the result"],
                originality_constraints=["Do not reuse another slot"],
            )
        ]
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
            "question_set_planner": [question_plans],
            "independent_solver": [wrong_solution, correct_solution],
            "question_arbiter": [retry, passed],
        }
    )
    workspace = Workspace(tmp_path / "workspace")
    workspace.initialize()
    runs = RunStore(workspace)
    workflow = ExamAgentWorkflow(
        ModelRouter(standard=standard, strong=strong),  # type: ignore[arg-type]
        ArtifactStore(workspace),
        runs,
    )
    run, state = await workflow.execute(
        subject="mathematics",
        target_level="Grade 12",
        requirements="One question",
        require_blueprint_approval=True,
    )

    assert run.status is RunStatus.WAITING_HUMAN
    assert strong.calls["subject_researcher"] == 1
    assert strong.calls["exam_blueprint_planner"] == 1
    assert strong.calls["question_set_planner"] == 0
    request = runs.pending_human_review(run.id)
    assert request is not None
    runs.resolve_human_review(
        HumanDecision(
            request_id=request.id,
            run_id=run.id,
            decision=HumanDecisionType.ACCEPT,
            actor="tester",
        )
    )
    run, state = await workflow.resume(run.id)

    assert run.status is RunStatus.SUCCEEDED
    assert state["planning"].mode is ExamPlanningMode.AGENT
    assert state["profile"].conventions == ["Use standard algebraic notation"]
    assert state["profile"].source_summary == "User requirements"
    assert strong.calls["subject_researcher"] == 1
    assert strong.calls["exam_blueprint_planner"] == 1
    assert strong.calls["question_set_planner"] == 1
    assert state["exam"].questions[0].solution.final_answer[0].content == "x = 3"
    assert standard.calls["question_writer"] == 1
    assert strong.calls["independent_solver"] == 2
    assert standard.calls["rubric_builder"] == 2


async def test_exam_agents_use_locked_preset_without_replanning(tmp_path: Path) -> None:
    profile = SubjectProfile(
        id="preset-mathematics",
        display_name="Preset Mathematics",
        supported_question_types=[QuestionType.CONSTRUCTED_RESPONSE],
        reviewers=["structure"],
        latex_template="generic-v1",
        difficulty_dimensions=["reasoning"],
    )
    blueprint = ExamBlueprint(
        id="preset-blueprint",
        version="2",
        subject_profile=profile.id,
        title="Preset assessment",
        target_level="Grade 12",
        duration_minutes=30,
        total_score=10,
        sections=[
            ExamSectionBlueprint(
                id="response",
                title="Constructed response",
                question_type=QuestionType.CONSTRUCTED_RESPONSE,
                count=1,
                question_scores=[10],
                topic_tags=["algebra"],
            )
        ],
        coverage=[CoverageTarget(topic_tag="algebra", target_score=10)],
        difficulty_distribution=DifficultyDistribution(easy=0, medium=1, hard=0),
    )
    question_plans = QuestionPlanSetDraft(
        plans=[
            QuestionPlanDraft(
                section_id="response",
                slot=1,
                topic_tags=["algebra"],
                primary_skill="Solve a linear equation",
                design_brief="Use a direct one-variable equation.",
                difficulty="medium",
                estimated_minutes=10,
                answer_form="A single real value",
                solution_outline=["Isolate the variable"],
                rubric_focus=["Correct transformation", "Correct result"],
                verification_methods=["Substitute the result"],
                originality_constraints=["Do not reuse another slot"],
            )
        ]
    )
    question = QuestionDraft(
        statement="Solve the following equation.",
        parts=[QuestionPart(id="p1", label="(1)", prompt="Solve x + 2 = 5.", score=10)],
        topic_tags=["algebra"],
    )
    solution = SolutionDraft(
        steps=[SolutionStep(id="s1", description="Subtract two from both sides")],
        final_answer="x = 3",
    )
    rubric = RubricDraft(items=[RubricItem(id="r1", description="Obtains the answer", score=10)])
    passed = ArbitrationDecision(action=ArbitrationAction.PASS, rationale="All checks pass.")
    standard = FixtureModel(
        {
            "question_writer": [question],
            "rubric_builder": [rubric],
        }
    )
    strong = FixtureModel(
        {
            "question_set_planner": [question_plans],
            "independent_solver": [solution],
            "question_arbiter": [passed],
        }
    )
    workspace = Workspace(tmp_path / "workspace")
    workspace.initialize()
    artifacts = ArtifactStore(workspace)
    runs = RunStore(workspace)
    workflow = ExamAgentWorkflow(
        ModelRouter(standard=standard, strong=strong),  # type: ignore[arg-type]
        artifacts,
        runs,
    )
    run, state = await workflow.execute(
        subject="mathematics",
        target_level="Grade 12",
        requirements="One question",
        subject_profile=profile,
        blueprint=blueprint,
        require_exam_approval=True,
    )

    assert run.status is RunStatus.WAITING_HUMAN
    assert state["planning"].mode is ExamPlanningMode.PRESET
    assert strong.calls["subject_researcher"] == 0
    assert strong.calls["exam_blueprint_planner"] == 0
    assert strong.calls["question_set_planner"] == 1
    assert state["exam"].questions[0].question.score == 10
    request = runs.pending_human_review(run.id)
    assert request is not None
    runs.resolve_human_review(
        HumanDecision(
            request_id=request.id,
            run_id=run.id,
            decision=HumanDecisionType.ACCEPT,
            actor="tester",
        )
    )
    run, state = await workflow.resume(run.id)

    assert run.status is RunStatus.SUCCEEDED
    assert standard.calls["question_writer"] == 1
    assert strong.calls["independent_solver"] == 1
    assert standard.calls["rubric_builder"] == 1
    planning_artifact = next(
        artifact
        for artifact in artifacts.list(run.id)
        if artifact.logical_name == "exam-planning.json"
    )
    planning_payload = json.loads(artifacts.read_bytes(planning_artifact.id))
    assert planning_payload == {
        "mode": "preset",
        "subject_profile_id": profile.id,
        "subject_profile_version": "1",
        "blueprint_id": blueprint.id,
        "blueprint_version": "2",
    }
