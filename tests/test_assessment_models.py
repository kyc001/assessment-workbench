from uuid import uuid4

import pytest
from pydantic import ValidationError

from assessment_workbench.domain import (
    ExamQuestionBundle,
    GenerationMetadata,
    QuestionType,
    QuestionVersion,
    RubricItem,
    RubricVersion,
    SolutionStep,
    SolutionVersion,
)


def build_question_bundle() -> ExamQuestionBundle:
    question = QuestionVersion(
        question_id=uuid4(),
        version=1,
        number=1,
        question_type=QuestionType.CALCULATION,
        topic_tags=["函数与导数"],
        score=12,
        statement="求函数的单调区间。",
        metadata=GenerationMetadata(role="fixture_question_writer"),
    )
    solution = SolutionVersion(
        solution_id=uuid4(),
        question_version_id=question.id,
        version=1,
        steps=[
            SolutionStep(id="s1", description="求导", expression="f'(x)"),
            SolutionStep(id="s2", description="判断导数符号", conclusion="得到单调区间"),
        ],
        final_answer="在指定区间单调递增。",
        metadata=GenerationMetadata(role="fixture_solver"),
    )
    rubric = RubricVersion(
        rubric_id=uuid4(),
        question_version_id=question.id,
        solution_version_id=solution.id,
        version=1,
        max_score=12,
        items=[
            RubricItem(id="r1", description="正确求导", score=4),
            RubricItem(id="r2", description="正确判断符号", score=4, depends_on=["r1"]),
            RubricItem(id="r3", description="写出单调区间", score=4, carry_forward=True),
        ],
        metadata=GenerationMetadata(role="fixture_rubric_builder"),
    )
    return ExamQuestionBundle(question=question, solution=solution, rubric=rubric)


def test_question_solution_and_rubric_are_linked() -> None:
    bundle = build_question_bundle()
    assert bundle.rubric.max_score == bundle.question.score
    assert bundle.solution.question_version_id == bundle.question.id


def test_rubric_rejects_score_mismatch() -> None:
    bundle = build_question_bundle()
    payload = bundle.rubric.model_dump()
    payload["max_score"] = 10
    with pytest.raises(ValidationError, match="sum to max_score"):
        RubricVersion.model_validate(payload)


def test_multiple_choice_requires_options() -> None:
    with pytest.raises(ValidationError, match="at least four options"):
        QuestionVersion(
            question_id=uuid4(),
            version=1,
            number=1,
            question_type=QuestionType.MULTIPLE_CHOICE,
            topic_tags=["集合"],
            score=5,
            statement="选择正确结论。",
            metadata=GenerationMetadata(role="fixture"),
        )
