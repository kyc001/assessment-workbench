from collections.abc import Iterator
from dataclasses import dataclass

from assessment_workbench.domain import ExamDocument, ExamView
from assessment_workbench.latex import GenericLatexRenderer


class LatexTemplateError(ValueError):
    pass


@dataclass(frozen=True)
class ExamLatexBuild:
    view: ExamView
    source: str
    compile_result: None = None


class ExamLatexService:
    def __init__(
        self,
        *,
        renderer: GenericLatexRenderer | None = None,
    ) -> None:
        self.renderer = renderer or GenericLatexRenderer()

    def render(self, exam: ExamDocument, view: ExamView) -> ExamLatexBuild:
        source = self.renderer.render(exam, view)
        validate_exam_latex(source)
        return ExamLatexBuild(view=view, source=source)

    def build(self, exam: ExamDocument) -> Iterator[ExamLatexBuild]:
        for view in ExamView:
            yield self.render(exam, view)


def validate_exam_latex(source: str) -> None:
    required = (
        r"\documentclass[12pt,a4paper]{ctexart}",
        r"\newcommand{\awsection}",
        r"\newcommand{\awquestion}",
        r"\newcommand{\awpart}",
        r"\begin{document}",
        r"\end{document}",
    )
    missing = [token for token in required if token not in source]
    if missing:
        raise LatexTemplateError(f"exam LaTeX is missing template contract: {missing}")
    if source.count(r"\begin{document}") != 1 or source.count(r"\end{document}") != 1:
        raise LatexTemplateError("exam LaTeX must contain exactly one document environment")
    forbidden = (r"\begin{samepage}", "{{BLOCK_", "{{INLINE_", "{{FIGURE_")
    found = [token for token in forbidden if token in source]
    if found:
        raise LatexTemplateError(f"exam LaTeX contains forbidden template content: {found}")
