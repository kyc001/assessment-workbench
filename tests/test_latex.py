from pathlib import Path
from uuid import uuid4

import pytest

from assessment_workbench.compilers import TectonicCompiler
from assessment_workbench.domain import (
    ExamDocument,
    ExamQuestionBundle,
    GenerationMetadata,
    QuestionType,
    QuestionVersion,
    RubricItem,
    RubricVersion,
    SolutionStep,
    SolutionVersion,
)
from assessment_workbench.latex import ExamView, GenericLatexRenderer, escape_latex, validate_math


def build_exam() -> ExamDocument:
    question = QuestionVersion(
        question_id=uuid4(),
        version=1,
        number=1,
        question_type=QuestionType.CALCULATION,
        topic_tags=["algebra"],
        score=10,
        statement="求 x_1 & x_2。",
        metadata=GenerationMetadata(role="writer"),
    )
    solution = SolutionVersion(
        solution_id=uuid4(),
        question_version_id=question.id,
        version=1,
        steps=[SolutionStep(id="s1", description="移项", expression=r"x=3")],
        final_answer="x = 3",
        metadata=GenerationMetadata(role="solver"),
    )
    rubric = RubricVersion(
        rubric_id=uuid4(),
        question_version_id=question.id,
        solution_version_id=solution.id,
        version=1,
        max_score=10,
        items=[
            RubricItem(id="r1", description="列式", score=4),
            RubricItem(id="r2", description="求解", score=6),
        ],
        metadata=GenerationMetadata(role="rubric"),
    )
    return ExamDocument(
        blueprint_id="blueprint",
        title="数学测试",
        subject_profile="math",
        duration_minutes=30,
        total_score=10,
        questions=[ExamQuestionBundle(question=question, solution=solution, rubric=rubric)],
    )


def test_renderer_separates_views_and_escapes_text() -> None:
    renderer = GenericLatexRenderer()
    exam = build_exam()
    questions = renderer.render(exam, ExamView.QUESTIONS)
    solutions = renderer.render(exam, ExamView.SOLUTIONS)
    rubric = renderer.render(exam, ExamView.RUBRIC)

    assert "Final answer" not in questions
    assert "Final answer" in solutions
    assert "Scoring rubric" in rubric
    assert r"x\_1 \& x\_2" in questions
    assert renderer.render(exam, ExamView.QUESTIONS) == questions


def test_latex_safety_checks() -> None:
    assert escape_latex("50%") == r"50\%"
    with pytest.raises(ValueError, match="unsafe"):
        validate_math(r"\input{secret}")


def test_tectonic_compiles_document_when_available() -> None:
    executable = Path.home() / ".local" / "bin" / "tectonic.cmd"
    if not executable.is_file():
        pytest.skip("Tectonic is not installed")
    result = TectonicCompiler(str(executable)).compile(
        GenericLatexRenderer().render(build_exam(), ExamView.QUESTIONS),
        job_name="renderer-integration",
    )
    assert result.pdf.startswith(b"%PDF-")
