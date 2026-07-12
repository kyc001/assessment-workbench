from enum import StrEnum

from assessment_workbench.domain import ExamDocument, ExamQuestionBundle, QuestionType


class ExamView(StrEnum):
    QUESTIONS = "questions"
    SOLUTIONS = "solutions"
    RUBRIC = "rubric"


class GenericLatexRenderer:
    name = "generic-latex"
    template_version = "generic-v1"

    def render(self, exam: ExamDocument, view: ExamView) -> str:
        body = "\n\n".join(self._render_bundle(bundle, view) for bundle in exam.questions)
        title_suffix = {
            ExamView.QUESTIONS: "",
            ExamView.SOLUTIONS: " - Solutions",
            ExamView.RUBRIC: " - Rubric",
        }[view]
        return (
            "\\documentclass[12pt,a4paper]{ctexart}\n"
            "\\usepackage[margin=2.2cm]{geometry}\n"
            "\\usepackage{amsmath,amssymb}\n"
            "\\usepackage{enumitem}\n"
            "\\setlist{nosep}\n"
            "\\begin{document}\n"
            f"\\begin{{center}}\\Large\\textbf{{{escape_latex(exam.title + title_suffix)}}}"
            "\\end{center}\n"
            f"\\noindent Duration: {exam.duration_minutes} minutes\\hfill "
            f"Total: {exam.total_score} points\n\n"
            f"{body}\n"
            "\\end{document}\n"
        )

    def _render_bundle(self, bundle: ExamQuestionBundle, view: ExamView) -> str:
        question = bundle.question
        lines = [
            f"\\section*{{Question {question.number} ({question.score} points)}}",
            escape_latex(question.statement),
        ]
        if question.question_type is QuestionType.MULTIPLE_CHOICE:
            lines.append("\\begin{enumerate}[label=\\Alph*.]")
            lines.extend(f"\\item {escape_latex(option)}" for option in question.options)
            lines.append("\\end{enumerate}")
        if view is ExamView.SOLUTIONS:
            lines.extend(["\\subsection*{Solution}", "\\begin{enumerate}"])
            for step in bundle.solution.steps:
                lines.append(f"\\item {escape_latex(step.description)}")
                if step.expression:
                    lines.append(f"\\[{validate_math(step.expression)}\\]")
                if step.conclusion:
                    lines.append(escape_latex(step.conclusion))
            lines.extend(
                [
                    "\\end{enumerate}",
                    f"\\textbf{{Final answer:}} {escape_latex(bundle.solution.final_answer)}",
                ]
            )
        elif view is ExamView.RUBRIC:
            lines.extend(["\\subsection*{Scoring rubric}", "\\begin{enumerate}"])
            lines.extend(
                f"\\item {escape_latex(item.description)} ({item.score} points)"
                for item in bundle.rubric.items
            )
            lines.append("\\end{enumerate}")
        return "\n".join(lines)


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
    if "\x00" in expression or any(command in lowered for command in forbidden):
        raise ValueError("unsafe LaTeX command in mathematical expression")
    return expression
