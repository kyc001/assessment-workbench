from collections.abc import Iterator
from dataclasses import dataclass

from assessment_workbench.compilers import CompileResult, LatexCompiler
from assessment_workbench.domain import ExamDocument
from assessment_workbench.latex import ExamView, GenericLatexRenderer


class LatexTemplateError(ValueError):
    pass


@dataclass(frozen=True)
class ExamLatexBuild:
    view: ExamView
    source: str
    compile_result: CompileResult | None


class ExamLatexService:
    def __init__(
        self,
        *,
        renderer: GenericLatexRenderer | None = None,
        compiler: LatexCompiler | None = None,
    ) -> None:
        self.renderer = renderer or GenericLatexRenderer()
        self.compiler = compiler

    def build(self, exam: ExamDocument) -> Iterator[ExamLatexBuild]:
        for view in ExamView:
            source = self.renderer.render(exam, view)
            validate_exam_latex(source)
            result = (
                self.compiler.compile(source, job_name=f"exam-{view.value}")
                if self.compiler is not None
                else None
            )
            yield ExamLatexBuild(view=view, source=source, compile_result=result)


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
