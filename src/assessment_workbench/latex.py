from enum import StrEnum

from assessment_workbench.domain import (
    ExamContentBlock,
    ExamContentKind,
    ExamDocument,
    ExamQuestionBundle,
    QuestionType,
)


class ExamView(StrEnum):
    QUESTIONS = "questions"
    SOLUTIONS = "solutions"
    RUBRIC = "rubric"


_EXAM_LAYOUT_COMMANDS = r"""
\newcommand{\awsection}[1]{%
  \Needspace{12\baselineskip}%
  \section*{#1}%
}
\newcommand{\awquestion}[1]{%
  \Needspace{8\baselineskip}%
  \subsection*{#1}%
}
\newcommand{\awpart}[2]{%
  \item \parbox[t]{\dimexpr\linewidth-4.5em\relax}{#1}%
  \hfill\makebox[4em][r]{(#2)}%
}
""".strip()


class GenericLatexRenderer:
    name = "generic-latex"
    template_version = "generic-v1"

    def render(self, exam: ExamDocument, view: ExamView) -> str:
        chinese = exam.language.startswith("zh")
        body_parts: list[str] = []
        previous_section = None
        for bundle in exam.questions:
            section_title = bundle.question.section_title
            if section_title and section_title != previous_section:
                body_parts.append(f"\\awsection{{{escape_latex(section_title)}}}")
                previous_section = section_title
            body_parts.append(self._render_bundle(bundle, view, chinese=chinese))
        body = "\n\n".join(body_parts)
        title_suffix = _view_title_suffix(view, chinese)
        duration_label = "考试时间" if chinese else "Duration"
        total_label = "满分" if chinese else "Total"
        duration_unit = "分钟" if chinese else "minutes"
        score_unit = "分" if chinese else "points"
        return (
            "\\documentclass[12pt,a4paper]{ctexart}\n"
            "\\usepackage[margin=2.2cm]{geometry}\n"
            "\\usepackage{amsmath,amssymb}\n"
            "\\usepackage{enumitem}\n"
            "\\usepackage{needspace}\n"
            "\\allowdisplaybreaks\n"
            "\\setlength{\\parindent}{0pt}\n"
            "\\setlist{nosep}\n"
            f"{_EXAM_LAYOUT_COMMANDS}\n"
            "\\begin{document}\n"
            f"\\begin{{center}}\\Large\\textbf{{{escape_latex(exam.title + title_suffix)}}}"
            "\\end{center}\n"
            f"\\noindent {duration_label}: {exam.duration_minutes} {duration_unit}\\hfill "
            f"{total_label}: {exam.total_score} {score_unit}\n\n"
            f"{body}\n"
            "\\end{document}\n"
        )

    def _render_bundle(self, bundle: ExamQuestionBundle, view: ExamView, *, chinese: bool) -> str:
        question = bundle.question
        score_unit = "分" if chinese else "points"
        question_title = (
            f"第{question.number}题（{question.score}{score_unit}）"
            if chinese
            else f"Question {question.number} ({question.score} {score_unit})"
        )
        solution_title = "解答" if chinese else "Solution"
        rubric_title = "评分标准" if chinese else "Scoring rubric"
        final_answer_title = "最终答案" if chinese else "Final answer"
        lines = [
            f"\\awquestion{{{question_title}}}",
            render_content(question.statement),
        ]
        if question.question_type in {
            QuestionType.MULTIPLE_CHOICE,
            QuestionType.MULTIPLE_SELECT,
        }:
            lines.append("\\begin{enumerate}[label=\\Alph*.]")
            lines.extend(
                f"\\item {render_content(option, allow_display_math=False)}"
                for option in question.options
            )
            lines.append("\\end{enumerate}")
        if question.parts:
            lines.append("\\begin{enumerate}[label=(\\arabic*)]")
            lines.extend(
                f"\\awpart{{{render_content(part.prompt)}}}{{{part.score}{score_unit}}}"
                for part in question.parts
            )
            lines.append("\\end{enumerate}")
        if view is ExamView.SOLUTIONS:
            lines.extend([f"\\subsubsection*{{{solution_title}}}", "\\begin{enumerate}"])
            for step in bundle.solution.steps:
                lines.append(f"\\item {render_content(step.description)}")
                if step.expression:
                    lines.append(f"\\[{validate_math(step.expression)}\\]")
                if step.conclusion:
                    lines.append(render_content(step.conclusion))
            lines.extend(
                [
                    "\\end{enumerate}",
                    f"\\textbf{{{final_answer_title}：}} "
                    f"{render_content(bundle.solution.final_answer)}",
                ]
            )
        elif view is ExamView.RUBRIC:
            lines.extend([f"\\subsubsection*{{{rubric_title}}}", "\\begin{enumerate}"])
            lines.extend(
                f"\\item {render_content(item.description)} ({item.score}{score_unit})"
                for item in bundle.rubric.items
            )
            lines.append("\\end{enumerate}")
        return "\n".join(lines)


def _view_title_suffix(view: ExamView, chinese: bool) -> str:
    if not chinese:
        suffixes = {
            ExamView.QUESTIONS: "",
            ExamView.SOLUTIONS: " - Solutions",
            ExamView.RUBRIC: " - Rubric",
        }
    else:
        suffixes = {
            ExamView.QUESTIONS: "",
            ExamView.SOLUTIONS: "（答案）",
            ExamView.RUBRIC: "（评分标准）",
        }
    return suffixes[view]


def render_content(blocks: list[ExamContentBlock], *, allow_display_math: bool = True) -> str:
    rendered: list[str] = []
    for index, block in enumerate(blocks):
        if block.kind is ExamContentKind.TEXT:
            rendered.append(_render_text(block.content).replace("\n\n", "\n\n\\par\n"))
        elif block.kind is ExamContentKind.INLINE_MATH:
            rendered.append(f"${validate_math(block.content)}$")
        elif allow_display_math and _is_standalone_display(blocks, index):
            rendered.append(f"\n\\[{validate_math(block.content)}\\]\n")
        else:
            rendered.append(f"${validate_math(block.content)}$")
    return "".join(rendered)


def _is_standalone_display(blocks: list[ExamContentBlock], index: int) -> bool:
    if index + 1 >= len(blocks):
        return True
    following = blocks[index + 1]
    if following.kind is not ExamContentKind.TEXT:
        return True
    return not following.content.lstrip().startswith(("，", "。", "；", "：", ",", ".", ";", ":"))


def _render_text(text: str) -> str:
    rendered = escape_latex(text)
    circled_numbers = {chr(0x2460 + index): rf"\textcircled{{{index + 1}}}" for index in range(10)}
    for symbol, replacement in circled_numbers.items():
        rendered = rendered.replace(symbol, replacement)
    math_symbols = {
        "∠": r"$\angle$",
        "⊥": r"$\perp$",
        "∥": r"$\parallel$",
    }
    for symbol, replacement in math_symbols.items():
        rendered = rendered.replace(symbol, replacement)
    return rendered


def escape_latex(text: str) -> str:
    mapping = {
        "\\": r"\textbackslash{}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "^": r"\textasciicircum{}",
        "~": r"\textasciitilde{}",
        "{": r"\{",
        "}": r"\}",
    }
    return "".join(mapping.get(character, character) for character in text)


def validate_math(expression: str) -> str:
    lowered = expression.lower()
    forbidden = (
        "\\input",
        "\\include",
        "\\openin",
        "\\openout",
        "\\write",
        "\\read",
        "\\usepackage",
        "\\documentclass",
        "\\begin{document}",
        "\\end{document}",
    )
    invalid_controls = "\x00\t\b\f\r"
    if any(character in expression for character in invalid_controls) or any(
        command in lowered for command in forbidden
    ):
        raise ValueError("unsafe LaTeX command in mathematical expression")
    return expression
