import re

from assessment_workbench.domain import (
    CalculatorPolicy,
    ExamContentBlock,
    ExamContentKind,
    ExamDocument,
    ExamQuestionBundle,
    ExamView,
    QuestionType,
)

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

GENERIC_LATEX_REVIEW_CONTEXT = {
    "renderer": "generic-latex",
    "template_version": "generic-v3",
    "output_target": "A4 PDF rendered by ctexart and Tectonic",
    "choice_labels": (
        "The renderer automatically labels ordered option arrays as A, B, C, D. "
        "Option content must not repeat those labels."
    ),
    "math_support": (
        "amsmath and amssymb are loaded; aligned is supported inside display math. "
        "Choice-option math is rendered inline to keep each generated label attached."
    ),
}


class GenericLatexRenderer:
    name = "generic-latex"
    template_version = "generic-v3"

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
        calculator_notice = _calculator_notice(exam.calculator_policy, chinese=chinese)
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
            f"{calculator_notice}"
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


def _calculator_notice(policy: CalculatorPolicy, *, chinese: bool) -> str:
    if policy is CalculatorPolicy.UNSPECIFIED:
        return ""
    if policy is CalculatorPolicy.PROHIBITED:
        text = (
            "计算器：不允许使用；除题目明确要求外，答案应保留精确形式。"
            if chinese
            else "Calculator: not permitted; keep answers exact unless instructed otherwise."
        )
    else:
        text = (
            "计算器：允许使用不具备编程、符号代数或联网功能的普通科学计算器。"
            if chinese
            else (
                "Calculator: a standard scientific calculator without programming, "
                "symbolic algebra, or network access is permitted."
            )
        )
    return f"\\noindent\\textbf{{{escape_latex(text)}}}\\par\n\n"


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
    if _is_short_embedded_math(blocks, index):
        return False
    if index + 1 >= len(blocks):
        return True
    following = blocks[index + 1]
    if following.kind is not ExamContentKind.TEXT:
        return True
    return not following.content.lstrip().startswith(("，", "。", "；", "：", ",", ".", ";", ":"))


def _is_short_embedded_math(blocks: list[ExamContentBlock], index: int) -> bool:
    if index == 0 or index + 1 >= len(blocks):
        return False
    previous = blocks[index - 1]
    following = blocks[index + 1]
    if previous.kind is not ExamContentKind.TEXT or following.kind is not ExamContentKind.TEXT:
        return False
    content = blocks[index].content.strip()
    return "\n" not in content and len(content) <= 32


def _render_text(text: str) -> str:
    rendered = escape_latex(text)
    rendered = re.sub(
        r"\b(?P<unit>mm|cm|m)\\textasciicircum\{\}(?P<power>[23])\b",
        lambda match: (
            rf"\ensuremath{{\mathrm{{{match.group('unit')}}}^{{{match.group('power')}}}}}"
        ),
        rendered,
    )
    rendered = re.sub(
        r"(?<![A-Za-z0-9])(?P<name>[A-Za-z])\\_(?P<index>[0-9]+)(?![A-Za-z0-9])",
        lambda match: rf"\ensuremath{{{match.group('name')}_{match.group('index')}}}",
        rendered,
    )
    rendered = re.sub(
        r"(?<![A-Za-z0-9])x0(?![A-Za-z0-9])",
        lambda _match: r"\ensuremath{x_0}",
        rendered,
    )
    circled_numbers = {chr(0x2460 + index): rf"\textcircled{{{index + 1}}}" for index in range(10)}
    for symbol, replacement in circled_numbers.items():
        rendered = rendered.replace(symbol, replacement)
    math_symbols = {
        "∠": r"$\angle$",
        "⊥": r"$\perp$",
        "∥": r"$\parallel$",
        "≤": r"$\leq$",
        "≥": r"$\geq$",
        "≠": r"$\ne$",
        "≈": r"$\approx$",
        "∞": r"$\infty$",
        "∈": r"$\in$",
        "∉": r"$\notin$",
        "∪": r"$\cup$",
        "∩": r"$\cap$",
        "±": r"$\pm$",
        "×": r"$\times$",
        "→": r"$\to$",
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
    return _normalize_math(expression)


def _normalize_math(expression: str) -> str:
    normalized = expression
    normalized = re.sub(r"(?<!\\)\bmax\b", r"\\max", normalized)
    normalized = re.sub(r"(?<!\\)\bmin\b", r"\\min", normalized)
    normalized = re.sub(r"\s+at\s+", r" \\text{ at } ", normalized)
    normalized = re.sub(r"\s+and\s+", r" \\text{ and } ", normalized)
    normalized = re.sub(r"\s*<=\s*", r" \\leq ", normalized)
    normalized = re.sub(r"\s*>=\s*", r" \\geq ", normalized)
    sqrt_pattern = re.compile(r"(?<![\\A-Za-z])sqrt\(([^()]*)\)")
    while sqrt_pattern.search(normalized):
        normalized = sqrt_pattern.sub(lambda match: rf"\sqrt{{{match.group(1)}}}", normalized)
    normalized = re.sub(
        r"(?P<value>(?:\d+(?:\.\d+)?|[A-Za-z]))\s*degrees\b",
        lambda match: rf"{match.group('value')}^\circ",
        normalized,
    )
    normalized = re.sub(
        r"(?<![\\A-Za-z])(?P<name>sin|cos|tan|ln|log)(?=(?:\\?[A-Za-z]|\())",
        lambda match: rf"\{match.group('name')} ",
        normalized,
    )
    normalized = re.sub(
        r"(?<!\\)\b(?P<name>sin|cos|tan|ln|log|det)\b",
        lambda match: rf"\{match.group('name')}",
        normalized,
    )
    normalized = re.sub(r"(?<!\\)\bpi\b", r"\\pi", normalized)
    normalized = re.sub(r"(?<!\\)\binfinity\b", r"\\infty", normalized)
    normalized = re.sub(r"(?<![\\A-Za-z])sum(?=_)", r"\\sum", normalized)
    normalized = re.sub(r"(?<![\\A-Za-z])lim(?=[_(])", r"\\lim", normalized)
    normalized = re.sub(r"(?<![\\A-Za-z])in(?=\s)", r"\\in", normalized)
    normalized = re.sub(
        r"(?P<operator>\\in\s*)\{(?P<members>[^{}]*)\}",
        lambda match: rf"{match.group('operator')}\{{{match.group('members')}\}}",
        normalized,
    )
    normalized = re.sub(
        r"(?<=[0-9A-Za-z)\]}])\s*\*\s*(?=[0-9A-Za-z(\\])",
        r" \\cdot ",
        normalized,
    )
    normalized = re.sub(
        r"(?P<prefix>[A-Za-z])_triangle(?P<vertices>[A-Za-z]+)",
        lambda match: rf"{match.group('prefix')}_{{\triangle {match.group('vertices')}}}",
        normalized,
    )
    normalized = re.sub(r"(?<!\\)\bangle\s+", r"\\angle ", normalized)
    return normalized
