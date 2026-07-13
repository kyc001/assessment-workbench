import asyncio
import json
from collections import defaultdict
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from assessment_workbench.agents import ExamAgentWorkflow, ModelRouter
from assessment_workbench.domain import (
    ArbitrationAction,
    ArbitrationDecision,
    BlueprintDraft,
    CoverageTarget,
    DifficultyDistribution,
    ExamArbitrationAction,
    ExamArbitrationDecision,
    ExamBlueprint,
    ExamDocument,
    ExamPlanningMode,
    ExamReviewReport,
    ExamSectionBlueprint,
    HumanDecision,
    HumanDecisionType,
    QuestionDraft,
    QuestionPart,
    QuestionPlan,
    QuestionPlanDraft,
    QuestionPlanSetDraft,
    QuestionType,
    ReviewerName,
    ReviewerRunRecord,
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
    def __init__(self, responses: dict[str, list[BaseModel | BaseException]]) -> None:
        self.responses = responses
        self.calls: dict[str, int] = defaultdict(int)

    async def complete(self, **kwargs: Any) -> BaseModel:
        role = str(kwargs["role"])
        index = self.calls[role]
        self.calls[role] += 1
        if role not in self.responses:
            response_model = kwargs["response_model"]
            if response_model is ExamReviewReport:
                return ExamReviewReport(reviewer=role, passed=True)
            if response_model is ExamArbitrationDecision:
                return ExamArbitrationDecision(
                    action=ExamArbitrationAction.PASS,
                    rationale="Fixture whole-exam reviews pass.",
                )
            raise KeyError(role)
        response = self.responses[role][index]
        if isinstance(response, BaseException):
            raise response
        return response


def single_question_context(
    reviewers: list[str],
) -> tuple[
    SubjectProfile,
    ExamBlueprint,
    QuestionPlan,
    QuestionDraft,
    SolutionDraft,
    RubricDraft,
]:
    profile = SubjectProfile(
        id="mathematics",
        display_name="Mathematics",
        supported_question_types=[QuestionType.CALCULATION],
        reviewers=reviewers,
        latex_template="generic-v1",
        difficulty_dimensions=["reasoning"],
    )
    blueprint = ExamBlueprint(
        id="single-question",
        subject_profile=profile.id,
        title="Single question",
        target_level="Grade 12",
        duration_minutes=20,
        total_score=10,
        sections=[
            ExamSectionBlueprint(
                id="calculation",
                title="Calculation",
                question_type=QuestionType.CALCULATION,
                count=1,
                score_each=10,
            )
        ],
        coverage=[CoverageTarget(topic_tag="algebra", target_score=10)],
        difficulty_distribution=DifficultyDistribution(easy=0, medium=1, hard=0),
    )
    plan = QuestionPlan(
        id="single-question:q01",
        number=1,
        question_type=QuestionType.CALCULATION,
        score=10,
        section_id="calculation",
        section_title="Calculation",
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
        originality_constraints=["Use original coefficients"],
    )
    question = QuestionDraft(statement="Solve x + 2 = 5.", topic_tags=["algebra"])
    solution = SolutionDraft(
        steps=[SolutionStep(id="s1", description="Subtract two from both sides")],
        final_answer="x = 3",
    )
    rubric = RubricDraft(
        items=[
            RubricItem(id="r1", description="Sets up the equation", score=4),
            RubricItem(id="r2", description="Obtains the answer", score=6),
        ]
    )
    return profile, blueprint, plan, question, solution, rubric


class StopAtQuestionPlanningModel:
    def __init__(self) -> None:
        self.roles: list[str] = []

    async def complete(self, **kwargs: Any) -> BaseModel:
        role = str(kwargs["role"])
        self.roles.append(role)
        raise RuntimeError("stop after capability planning")


class BlockingFixtureModel(FixtureModel):
    def __init__(self, responses: dict[str, list[BaseModel | BaseException]]) -> None:
        super().__init__(responses)
        self.writer_started = asyncio.Event()
        self.release_writer = asyncio.Event()

    async def complete(self, **kwargs: Any) -> BaseModel:
        if kwargs["role"] == "question_writer":
            self.writer_started.set()
            await self.release_writer.wait()
        return await super().complete(**kwargs)


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


async def test_question_resume_reuses_completed_problem_stage(tmp_path: Path) -> None:
    profile, blueprint, plan, question, solution, rubric = single_question_context(
        ["solvability", "structure"]
    )
    passed_review = ReviewReport(reviewer="solvability", passed=True)
    passed = ArbitrationDecision(action=ArbitrationAction.PASS, rationale="All checks pass.")
    standard = FixtureModel(
        {
            "question_writer": [question],
            "rubric_builder": [rubric],
            "solvability_reviewer": [passed_review],
        }
    )
    strong = FixtureModel(
        {
            "independent_solver": [KeyboardInterrupt(), solution],
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

    run, _ = await workflow.generate_question_run(
        profile=profile,
        blueprint=blueprint,
        plan=plan,
    )

    assert run.status is RunStatus.INTERRUPTED
    assert artifacts.latest(run.id, "questions/01/question.json") is not None
    assert artifacts.latest(run.id, "questions/01/solution.json") is None

    resumed, state = await workflow.resume_question_run(run.id)

    assert resumed.status is RunStatus.SUCCEEDED
    assert state["bundle"].solution.final_answer[0].content == "x = 3"
    assert standard.calls["question_writer"] == 1
    assert strong.calls["independent_solver"] == 2
    problem_rounds = [
        event.round
        for event in runs.events(run.id)
        if event.phase == "PROBLEM_GENERATING" and event.status.value == "completed"
    ]
    solution_running_rounds = [
        event.round
        for event in runs.events(run.id)
        if event.phase == "SOLUTION_GENERATING" and event.status.value == "running"
    ]
    assert problem_rounds == [1]
    assert solution_running_rounds == [1, 2]


async def test_reviewer_failure_retries_only_failed_reviewer(tmp_path: Path) -> None:
    profile, blueprint, plan, question, solution, rubric = single_question_context(
        ["solvability", "pedagogical", "structure"]
    )
    solvability_passed = ReviewReport(reviewer="solvability", passed=True)
    pedagogical_passed = ReviewReport(reviewer="pedagogical", passed=True)
    passed = ArbitrationDecision(action=ArbitrationAction.PASS, rationale="All checks pass.")
    standard = FixtureModel(
        {
            "question_writer": [question],
            "rubric_builder": [rubric],
            "solvability_reviewer": [RuntimeError("temporary failure"), solvability_passed],
            "pedagogical_reviewer": [pedagogical_passed],
        }
    )
    strong = FixtureModel(
        {
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

    run, state = await workflow.generate_question_run(
        profile=profile,
        blueprint=blueprint,
        plan=plan,
    )

    assert run.status is RunStatus.SUCCEEDED
    assert standard.calls["solvability_reviewer"] == 2
    assert standard.calls["pedagogical_reviewer"] == 1
    manifest = artifacts.latest(run.id, "review-runs.json")
    assert manifest is not None
    payload = artifacts.read_json(manifest.id)
    assert isinstance(payload, list)
    records = [ReviewerRunRecord.model_validate(item) for item in payload]
    assert [record.status for record in records if record.reviewer == "solvability"] == [
        RunStatus.FAILED,
        RunStatus.SUCCEEDED,
    ]
    assert len([record for record in records if record.reviewer == "pedagogical"]) == 1
    assert len([record for record in records if record.reviewer == "structure"]) == 1
    bundle = state["bundle"]
    assert all(
        record.question_version_id == bundle.question.id
        and record.solution_version_id == bundle.solution.id
        and record.rubric_version_id == bundle.rubric.id
        for record in records
    )


async def test_parent_manifest_exposes_child_run_before_writer_finishes(tmp_path: Path) -> None:
    profile, blueprint, plan, question, solution, rubric = single_question_context(
        ["solvability", "structure"]
    )
    plan_set = QuestionPlanSetDraft(
        plans=[
            QuestionPlanDraft(
                section_id=plan.section_id,
                slot=plan.slot,
                topic_tags=plan.topic_tags,
                primary_skill=plan.primary_skill,
                design_brief=plan.design_brief,
                difficulty=plan.difficulty,
                estimated_minutes=plan.estimated_minutes,
                answer_form=plan.answer_form,
                solution_outline=plan.solution_outline,
                rubric_focus=plan.rubric_focus,
                verification_methods=plan.verification_methods,
                originality_constraints=plan.originality_constraints,
            )
        ]
    )
    passed_review = ReviewReport(reviewer="solvability", passed=True)
    passed = ArbitrationDecision(action=ArbitrationAction.PASS, rationale="All checks pass.")
    standard = BlockingFixtureModel(
        {
            "question_writer": [question],
            "rubric_builder": [rubric],
            "solvability_reviewer": [passed_review],
        }
    )
    strong = FixtureModel(
        {
            "question_set_planner": [plan_set],
            "independent_solver": [solution],
            "question_arbiter": [passed],
        }
    )
    workspace = Workspace(tmp_path / "workspace")
    workspace.initialize()
    artifacts = ArtifactStore(workspace)
    workflow = ExamAgentWorkflow(
        ModelRouter(standard=standard, strong=strong),  # type: ignore[arg-type]
        artifacts,
        RunStore(workspace),
    )
    parent_ids: list[str] = []

    execution = asyncio.create_task(
        workflow.execute(
            subject="mathematics",
            target_level="Grade 12",
            requirements="One question",
            subject_profile=profile,
            blueprint=blueprint,
            on_run_created=lambda run: parent_ids.append(str(run.id)),
        )
    )
    await asyncio.wait_for(standard.writer_started.wait(), timeout=5)

    assert len(parent_ids) == 1
    manifest = artifacts.read_editable_json(UUID(parent_ids[0]), "question-runs.json")
    assert isinstance(manifest, list)
    assert manifest[0]["run_id"] is not None
    assert manifest[0]["status"] == RunStatus.RUNNING

    standard.release_writer.set()
    run, _ = await execution
    assert run.status is RunStatus.SUCCEEDED


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


async def test_exam_arbitration_regenerates_only_target_section(tmp_path: Path) -> None:
    profile = SubjectProfile(
        id="local-replacement-math",
        display_name="Mathematics",
        supported_question_types=[QuestionType.CALCULATION],
        reviewers=["structure"],
        latex_template="generic-v1",
        difficulty_dimensions=["reasoning"],
    )
    blueprint = ExamBlueprint(
        id="local-replacement-exam",
        subject_profile=profile.id,
        title="Local replacement exam",
        target_level="Grade 12",
        duration_minutes=30,
        total_score=20,
        sections=[
            ExamSectionBlueprint(
                id="algebra",
                title="Algebra",
                question_type=QuestionType.CALCULATION,
                count=1,
                score_each=10,
            ),
            ExamSectionBlueprint(
                id="geometry",
                title="Geometry",
                question_type=QuestionType.CALCULATION,
                count=1,
                score_each=10,
            ),
        ],
        coverage=[
            CoverageTarget(topic_tag="algebra", target_score=10),
            CoverageTarget(topic_tag="geometry", target_score=10),
        ],
        difficulty_distribution=DifficultyDistribution(easy=0, medium=1, hard=0),
    )
    plan_drafts = [
        QuestionPlanDraft(
            section_id="algebra",
            slot=1,
            topic_tags=["algebra"],
            primary_skill="Solve a linear equation",
            design_brief="Use one variable.",
            difficulty="medium",
            estimated_minutes=10,
            answer_form="A value",
            solution_outline=["Solve"],
            rubric_focus=["Method", "Answer"],
            verification_methods=["Substitute"],
            originality_constraints=["Use original values"],
        ),
        QuestionPlanDraft(
            section_id="geometry",
            slot=1,
            topic_tags=["geometry"],
            primary_skill="Compute a length",
            design_brief="Use a right triangle.",
            difficulty="medium",
            estimated_minutes=10,
            answer_form="A value",
            solution_outline=["Apply the theorem"],
            rubric_focus=["Method", "Answer"],
            verification_methods=["Check units"],
            originality_constraints=["Use original values"],
        ),
    ]
    questions = [
        QuestionDraft(statement="Solve x + 2 = 5.", topic_tags=["algebra"]),
        QuestionDraft(statement="Find the missing side of the triangle.", topic_tags=["geometry"]),
        QuestionDraft(statement="Solve 2x + 1 = 7.", topic_tags=["algebra"]),
    ]
    solutions = [
        SolutionDraft(
            steps=[SolutionStep(id="s1", description="Subtract two.")],
            final_answer="x = 3",
        ),
        SolutionDraft(
            steps=[SolutionStep(id="s1", description="Apply the theorem.")],
            final_answer="5",
        ),
        SolutionDraft(
            steps=[SolutionStep(id="s1", description="Subtract one and divide.")],
            final_answer="x = 3",
        ),
    ]
    rubric = RubricDraft(
        items=[
            RubricItem(id="r1", description="Uses a valid method", score=6),
            RubricItem(id="r2", description="Gets the answer", score=4),
        ]
    )
    question_pass = ArbitrationDecision(
        action=ArbitrationAction.PASS,
        rationale="Question is valid.",
    )
    standard = FixtureModel(
        {
            "question_writer": questions,
            "rubric_builder": [rubric, rubric, rubric],
        }
    )
    strong = FixtureModel(
        {
            "question_set_planner": [QuestionPlanSetDraft(plans=plan_drafts)],
            "independent_solver": solutions,
            "question_arbiter": [question_pass, question_pass, question_pass],
            "exam_arbiter": [
                ExamArbitrationDecision(
                    action=ExamArbitrationAction.REGENERATE_SECTION,
                    rationale="Replace only the algebra section.",
                    section_ids=["algebra"],
                    question_feedback=["Use a distinct construction."],
                ),
                ExamArbitrationDecision(
                    action=ExamArbitrationAction.PASS,
                    rationale="The replacement resolves the finding.",
                ),
            ],
        }
    )
    workspace = Workspace(tmp_path / "workspace")
    workspace.initialize()
    artifacts = ArtifactStore(workspace)
    workflow = ExamAgentWorkflow(
        ModelRouter(standard=standard, strong=strong),  # type: ignore[arg-type]
        artifacts,
        RunStore(workspace),
    )

    run, state = await workflow.execute(
        subject="mathematics",
        target_level="Grade 12",
        requirements="Two questions",
        subject_profile=profile,
        blueprint=blueprint,
    )

    assert run.status is RunStatus.SUCCEEDED
    exam_artifacts = [
        artifact for artifact in artifacts.list(run.id) if artifact.logical_name == "exam.json"
    ]
    assert len(exam_artifacts) == 2
    first_exam = ExamDocument.model_validate(artifacts.read_json(exam_artifacts[0].id))
    final_exam = state["exam"]
    first_by_number = {bundle.question.number: bundle for bundle in first_exam.questions}
    final_by_number = {bundle.question.number: bundle for bundle in final_exam.questions}
    assert final_by_number[1].question.id != first_by_number[1].question.id
    assert final_by_number[2] == first_by_number[2]
    assert standard.calls["question_writer"] == 3
    assert strong.calls["independent_solver"] == 3
    records = state["question_runs"]
    assert len(records[0]["replacement_history"]) == 1
    assert records[1].get("replacement_history", []) == []
