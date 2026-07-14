from pathlib import Path
from uuid import uuid4

import pytest

from assessment_workbench.compilers import LatexCompileError, TectonicCompiler, validate_latex_log
from assessment_workbench.domain import (
    ExamContentBlock,
    ExamContentKind,
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
from assessment_workbench.latex import (
    ExamView,
    GenericLatexRenderer,
    escape_latex,
    render_content,
    validate_math,
)
from assessment_workbench.latex_service import (
    ExamLatexService,
    LatexTemplateError,
    validate_exam_latex,
)


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

    assert "最终答案" not in questions
    assert "最终答案" in solutions
    assert "评分标准" in rubric
    assert r"\ensuremath{x_1} \& \ensuremath{x_2}" in questions
    assert r"\awquestion{第1题（10分）}" in questions
    assert renderer.render(exam, ExamView.QUESTIONS) == questions


def test_latex_safety_checks() -> None:
    assert escape_latex("50%") == r"50\%"
    with pytest.raises(ValueError, match="unsafe"):
        validate_math(r"\input{secret}")
    with pytest.raises(ValueError, match="math blocks"):
        ExamContentBlock(kind=ExamContentKind.TEXT, content="AB⊥平面ABC")

    with pytest.raises(LatexCompileError, match="blocking diagnostic"):
        validate_latex_log(r"warning: Overfull \vbox (12.0pt too high)")
    validate_latex_log(r"warning: Underfull \hbox (badness 2941)")

    with pytest.raises(LatexTemplateError, match="template contract"):
        validate_exam_latex(r"\documentclass{article}\begin{document}\end{document}")


def test_math_blocks_normalize_double_escaped_commands_but_preserve_rows() -> None:
    block = ExamContentBlock(
        kind=ExamContentKind.DISPLAY_MATH,
        content=r"(x \\lor y) \\land (x \\lor \\neg y)",
    )
    aligned = ExamContentBlock(
        kind=ExamContentKind.DISPLAY_MATH,
        content=r"\begin{aligned}a_3&=3,\\a_4&=4\end{aligned}",
    )

    assert block.content == r"(x \lor y) \land (x \lor \neg y)"
    assert aligned.content == r"\begin{aligned}a_3&=3,\\a_4&=4\end{aligned}"


def test_renderer_preserves_structured_mathematics() -> None:
    exam = build_exam()
    exam.questions[0].question.statement = [
        ExamContentBlock(kind=ExamContentKind.TEXT, content="已知 "),
        ExamContentBlock(kind=ExamContentKind.INLINE_MATH, content=r"x_1+x_2=3"),
        ExamContentBlock(kind=ExamContentKind.TEXT, content="，求"),
        ExamContentBlock(kind=ExamContentKind.DISPLAY_MATH, content=r"x_1^2+x_2^2"),
    ]

    rendered = GenericLatexRenderer().render(exam, ExamView.QUESTIONS)

    assert r"$x_1+x_2=3$" in rendered
    assert r"\[x_1^2+x_2^2\]" in rendered


def test_renderer_normalizes_real_document_regressions() -> None:
    text = [
        ExamContentBlock(
            kind=ExamContentKind.TEXT,
            content="¬ 表示取反，∧ 表示与，∨ 表示或。",
        )
    ]
    modulo = ExamContentBlock(
        kind=ExamContentKind.DISPLAY_MATH,
        content="xRy iff x % 3 = y % 3",
    )
    aligned = ExamContentBlock(
        kind=ExamContentKind.DISPLAY_MATH,
        content=(
            r"\begin{aligned}[0]_R&=\{0,3,6,9\},\\"
            r"[1]_R&=\{1,4,7,10\}\end{aligned}"
        ),
    )

    assert render_content(text) == r"$\neg$ 表示取反，$\land$ 表示与，$\lor$ 表示或。"
    assert validate_math(modulo.content) == (r"xRy \Longleftrightarrow x \bmod 3 = y \bmod 3")
    normalized_aligned = validate_math(aligned.content)
    assert normalized_aligned.startswith(r"\begin{aligned}\lbrack 0\rbrack_R")
    assert r"\\\lbrack 1\rbrack_R" in normalized_aligned
    assert validate_math("delta(S) 中最轻边为 e") == (r"\delta(S) \text{中最轻边为} e")


def test_renderer_wraps_long_top_level_comma_displays() -> None:
    expression = ", ".join(f"w(e_{index})={index}" for index in range(1, 16))
    rendered = render_content(
        [ExamContentBlock(kind=ExamContentKind.DISPLAY_MATH, content=expression)]
    )

    assert r"\begin{aligned}" in rendered
    assert r" \\ " in rendered


def test_renderer_keeps_choice_math_inline_and_reserves_question_space() -> None:
    sentence = [
        ExamContentBlock(kind=ExamContentKind.TEXT, content="数据为"),
        ExamContentBlock(kind=ExamContentKind.DISPLAY_MATH, content="x_1=1,x_2=2"),
        ExamContentBlock(kind=ExamContentKind.TEXT, content="，求平均数"),
    ]
    option = [
        ExamContentBlock(kind=ExamContentKind.DISPLAY_MATH, content=r"\frac{3}{5}"),
    ]

    assert render_content(sentence) == r"数据为$x_1=1,x_2=2$，求平均数"
    assert render_content(option, allow_display_math=False) == r"$\frac{3}{5}$"

    exam = build_exam()
    exam.questions[0].question.question_type = QuestionType.MULTIPLE_CHOICE
    exam.questions[0].question.options = [option, option]
    rendered = GenericLatexRenderer().render(exam, ExamView.QUESTIONS)
    assert r"\newcommand{\awquestion}" in rendered
    assert r"\item $\frac{3}{5}$" in rendered

    builds = list(ExamLatexService().build(exam))
    assert [build.view for build in builds] == list(ExamView)
    assert all(build.compile_result is None for build in builds)


def test_tectonic_compiles_document_when_available() -> None:
    executable = Path.home() / ".local" / "bin" / "tectonic.cmd"
    if not executable.is_file():
        pytest.skip("Tectonic is not installed")
    result = TectonicCompiler(str(executable)).compile(
        GenericLatexRenderer().render(build_exam(), ExamView.QUESTIONS),
        job_name="renderer-integration",
    )
    assert result.pdf.startswith(b"%PDF-")
