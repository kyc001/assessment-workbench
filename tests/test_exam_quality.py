from collections import defaultdict
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import BaseModel

from assessment_workbench.capabilities import load_default_capability_catalog
from assessment_workbench.domain import (
    CoverageTarget,
    DifficultyDistribution,
    ExamArbitrationAction,
    ExamArbitrationDecision,
    ExamBlueprint,
    ExamDocument,
    ExamQuestionBundle,
    ExamReviewReport,
    ExamSectionBlueprint,
    GenerationMetadata,
    QuestionPlan,
    QuestionPlanDraft,
    QuestionType,
    QuestionVersion,
    RubricItem,
    RubricVersion,
    RunStatus,
    SolutionStep,
    SolutionVersion,
    SubjectProfile,
)
from assessment_workbench.exam_quality import (
    deterministic_exam_review,
    merge_revised_question_plans,
    resolve_exam_targets,
)
from assessment_workbench.exam_review_workflow import ExamReviewerPoolWorkflow
from assessment_workbench.storage import ArtifactStore, RunStore, Workspace


class _ReviewFixtureModel:
    def __init__(self) -> None:
        self.calls: dict[str, int] = defaultdict(int)

    async def complete(self, **kwargs: Any) -> BaseModel:
        role = str(kwargs["role"])
        attempt = self.calls[role]
        self.calls[role] += 1
        if role == "exam_duplication_reviewer" and attempt == 0:
            raise RuntimeError("transient reviewer failure")
        return ExamReviewReport(reviewer=role, passed=True)


def _context() -> tuple[
    SubjectProfile,
    ExamBlueprint,
    list[QuestionPlan],
    ExamDocument,
]:
    profile = SubjectProfile(
        id="math",
        display_name="Mathematics",
        supported_question_types=[QuestionType.CALCULATION],
        reviewers=["structure"],
        latex_template="generic-v1",
        difficulty_dimensions=["reasoning"],
    )
    blueprint = ExamBlueprint(
        id="two-question-exam",
        subject_profile=profile.id,
        title="Two questions",
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
    plans = [
        _plan("two-question-exam:q01", 1, "algebra", "algebra"),
        _plan("two-question-exam:q02", 2, "geometry", "geometry"),
    ]
    bundles = [_bundle(plan) for plan in plans]
    exam = ExamDocument(
        blueprint_id=blueprint.id,
        title=blueprint.title,
        subject_profile=profile.id,
        duration_minutes=blueprint.duration_minutes,
        total_score=blueprint.total_score,
        questions=bundles,
    )
    return profile, blueprint, plans, exam


def _plan(plan_id: str, number: int, section_id: str, topic: str) -> QuestionPlan:
    return QuestionPlan(
        id=plan_id,
        number=number,
        question_type=QuestionType.CALCULATION,
        score=10,
        section_id=section_id,
        section_title=section_id.title(),
        slot=1,
        topic_tags=[topic],
        primary_skill=f"Use {topic}",
        design_brief=f"Assess {topic}.",
        difficulty="medium",
        estimated_minutes=10,
        answer_form="A value",
        solution_outline=["Solve"],
        rubric_focus=["Method", "Answer"],
        verification_methods=["Check"],
        originality_constraints=["Use original data"],
    )


def _bundle(plan: QuestionPlan) -> ExamQuestionBundle:
    question = QuestionVersion(
        question_id=uuid4(),
        version=1,
        number=plan.number,
        section_id=plan.section_id,
        section_title=plan.section_title,
        question_type=plan.question_type,
        topic_tags=plan.topic_tags,
        score=plan.score,
        statement=f"Solve question {plan.number}.",
        metadata=GenerationMetadata(role="writer", plan_id=plan.id),
    )
    solution = SolutionVersion(
        solution_id=uuid4(),
        question_version_id=question.id,
        version=1,
        steps=[SolutionStep(id="s1", description="Solve it.")],
        final_answer="1",
        metadata=GenerationMetadata(role="solver", plan_id=plan.id),
    )
    rubric = RubricVersion(
        rubric_id=uuid4(),
        question_version_id=question.id,
        solution_version_id=solution.id,
        version=1,
        max_score=plan.score,
        items=[RubricItem(id="r1", description="Correct answer", score=plan.score)],
        metadata=GenerationMetadata(role="rubric", plan_id=plan.id),
    )
    return ExamQuestionBundle(question=question, solution=solution, rubric=rubric)


def test_exam_quality_reports_only_provable_plan_mismatch() -> None:
    profile, blueprint, plans, exam = _context()
    invalid_plans = [
        plans[0].model_copy(update={"topic_tags": ["geometry"]}),
        plans[1],
    ]

    report = deterministic_exam_review(profile, blueprint, invalid_plans, exam)

    assert not report.passed
    assert {finding.code for finding in report.findings} == {"coverage_score_mismatch"}


def test_exam_arbitration_targets_are_resolved_against_current_exam() -> None:
    _, blueprint, _, exam = _context()
    decision = ExamArbitrationDecision(
        action=ExamArbitrationAction.REGENERATE_SECTION,
        rationale="Replace only algebra.",
        section_ids=["algebra"],
    )
    assert resolve_exam_targets(decision, exam, blueprint) == [1]

    stale = ExamArbitrationDecision(
        action=ExamArbitrationAction.REPLACE_QUESTIONS,
        rationale="Stale model output.",
        question_ids=[uuid4()],
    )
    with pytest.raises(ValueError, match="unknown question ids"):
        resolve_exam_targets(stale, exam, blueprint)


def test_local_plan_revision_cannot_change_healthy_plan() -> None:
    _, _, plans, _ = _context()
    target = plans[0]
    revision = QuestionPlanDraft(
        section_id=target.section_id,
        slot=target.slot,
        topic_tags=["algebra"],
        primary_skill="Solve a quadratic equation",
        design_brief="Use a quadratic with integer roots.",
        difficulty="hard",
        estimated_minutes=15,
        answer_form=target.answer_form,
        solution_outline=target.solution_outline,
        rubric_focus=target.rubric_focus,
        verification_methods=target.verification_methods,
        originality_constraints=target.originality_constraints,
    )

    merged = merge_revised_question_plans(plans, [target.id], [revision])

    assert merged[0].difficulty == "hard"
    assert merged[0].id == target.id
    assert merged[1] == plans[1]


async def test_exam_reviewer_failure_retries_only_that_reviewer(tmp_path: Path) -> None:
    profile, blueprint, plans, exam = _context()
    workspace = Workspace(tmp_path / "workspace")
    workspace.initialize()
    artifacts = ArtifactStore(workspace)
    runs = RunStore(workspace)
    parent = runs.create("exam_parent")
    model = _ReviewFixtureModel()
    workflow = ExamReviewerPoolWorkflow(
        model,  # type: ignore[arg-type]
        artifacts,
        runs,
        load_default_capability_catalog(),
        max_attempts=2,
    )

    outcome = await workflow.execute(
        parent.id,
        profile=profile,
        blueprint=blueprint,
        plans=plans,
        exam=exam,
        capability_context={},
        restored_records=None,
        input_artifact_ids=[],
    )

    assert model.calls["exam_duplication_reviewer"] == 2
    assert model.calls["exam_consistency_reviewer"] == 1
    assert model.calls["exam_leakage_reviewer"] == 1
    assert model.calls["exam_risk_reviewer"] == 1
    duplication_records = [record for record in outcome.records if record.reviewer == "duplication"]
    assert [record.status for record in duplication_records] == [
        RunStatus.FAILED,
        RunStatus.SUCCEEDED,
    ]
