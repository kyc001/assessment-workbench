from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import yaml
from pydantic import BaseModel, Field

from assessment_workbench.domain import (
    ExamBlueprint,
    ExamDocument,
    ExamQuestionBundle,
    GenerationMetadata,
    QuestionType,
    QuestionVersion,
    RubricItem,
    RubricVersion,
    SolutionStep,
    SolutionVersion,
    WorkflowRun,
)
from assessment_workbench.profiles import load_exam_blueprint
from assessment_workbench.storage import ArtifactStore, RunStore
from assessment_workbench.workflow import WorkflowEngine


class DemoRubricSeed(BaseModel):
    description: str
    score: int = Field(ge=1)


class DemoQuestionSeed(BaseModel):
    number: int = Field(ge=1)
    section_id: str
    topic_tags: list[str] = Field(min_length=1)
    statement: str
    options: list[str] = Field(default_factory=list)
    final_answer: str
    solution_steps: list[str] = Field(min_length=1)
    rubric: list[DemoRubricSeed] = Field(min_length=1)


class GaokaoMathDemoWorkflow:
    def __init__(self, artifacts: ArtifactStore, runs: RunStore) -> None:
        self.artifacts = artifacts
        self.engine = WorkflowEngine(runs)

    async def execute(
        self, blueprint_path: Path, questions_path: Path
    ) -> tuple[WorkflowRun, dict[str, Any]]:
        async def load_blueprint(_: dict[str, Any]) -> dict[str, Any]:
            return {"blueprint": load_exam_blueprint(blueprint_path)}

        async def load_questions(_: dict[str, Any]) -> dict[str, Any]:
            payload = yaml.safe_load(questions_path.read_text(encoding="utf-8"))
            if not isinstance(payload, list):
                raise ValueError("demo question file must contain a list")
            return {"seeds": [DemoQuestionSeed.model_validate(item) for item in payload]}

        async def assemble(state: dict[str, Any]) -> dict[str, Any]:
            blueprint: ExamBlueprint = state["blueprint"]
            seeds: list[DemoQuestionSeed] = state["seeds"]
            return {"exam": build_demo_exam(blueprint, seeds)}

        async def export(state: dict[str, Any]) -> dict[str, Any]:
            exam: ExamDocument = state["exam"]
            run_id = state["run_id"]
            outputs = [
                self.artifacts.write_json(
                    run_id,
                    "exam.json",
                    exam.model_dump(mode="json"),
                    created_by_phase="EXPORTING",
                ),
                self.artifacts.write_bytes(
                    run_id,
                    "exam.md",
                    render_exam_markdown(exam).encode("utf-8"),
                    media_type="text/markdown",
                    created_by_phase="EXPORTING",
                ),
                self.artifacts.write_bytes(
                    run_id,
                    "solutions.md",
                    render_solutions_markdown(exam).encode("utf-8"),
                    media_type="text/markdown",
                    created_by_phase="EXPORTING",
                ),
                self.artifacts.write_bytes(
                    run_id,
                    "rubrics.md",
                    render_rubrics_markdown(exam).encode("utf-8"),
                    media_type="text/markdown",
                    created_by_phase="EXPORTING",
                ),
            ]
            return {"artifacts": outputs, "output_artifact_ids": [item.id for item in outputs]}

        return await self.engine.execute(
            "gaokao_mathematics_demo",
            [
                ("BLUEPRINT_LOADING", load_blueprint),
                ("DEMO_CONTENT_LOADING", load_questions),
                ("EXAM_ASSEMBLING", assemble),
                ("EXPORTING", export),
            ],
        )


def build_demo_exam(blueprint: ExamBlueprint, seeds: list[DemoQuestionSeed]) -> ExamDocument:
    section_by_id = {section.id: section for section in blueprint.sections}
    actual_counts: dict[str, int] = {}
    bundles: list[ExamQuestionBundle] = []
    for seed in sorted(seeds, key=lambda item: item.number):
        section = section_by_id.get(seed.section_id)
        if section is None:
            raise ValueError(f"unknown section id: {seed.section_id}")
        actual_counts[seed.section_id] = actual_counts.get(seed.section_id, 0) + 1
        stable_prefix = f"{blueprint.id}:q{seed.number}"
        question = QuestionVersion(
            id=uuid5(NAMESPACE_URL, f"{stable_prefix}:question:v1"),
            question_id=uuid5(NAMESPACE_URL, f"{stable_prefix}:question"),
            version=1,
            number=seed.number,
            question_type=section.question_type,
            topic_tags=seed.topic_tags,
            score=section.score_each,
            statement=seed.statement,
            options=seed.options,
            metadata=GenerationMetadata(role="gaokao_math_demo_question_writer"),
        )
        solution = SolutionVersion(
            id=uuid5(NAMESPACE_URL, f"{stable_prefix}:solution:v1"),
            solution_id=uuid5(NAMESPACE_URL, f"{stable_prefix}:solution"),
            question_version_id=question.id,
            version=1,
            steps=[
                SolutionStep(id=f"s{index}", description=description)
                for index, description in enumerate(seed.solution_steps, start=1)
            ],
            final_answer=seed.final_answer,
            metadata=GenerationMetadata(role="gaokao_math_demo_solver"),
        )
        rubric = RubricVersion(
            id=uuid5(NAMESPACE_URL, f"{stable_prefix}:rubric:v1"),
            rubric_id=uuid5(NAMESPACE_URL, f"{stable_prefix}:rubric"),
            question_version_id=question.id,
            solution_version_id=solution.id,
            version=1,
            max_score=question.score,
            items=[
                RubricItem(
                    id=f"r{index}",
                    description=item.description,
                    score=item.score,
                    carry_forward=section.question_type
                    in {QuestionType.CALCULATION, QuestionType.PROOF},
                )
                for index, item in enumerate(seed.rubric, start=1)
            ],
            metadata=GenerationMetadata(role="gaokao_math_demo_rubric_builder"),
        )
        bundles.append(ExamQuestionBundle(question=question, solution=solution, rubric=rubric))

    expected_counts = {section.id: section.count for section in blueprint.sections}
    if actual_counts != expected_counts:
        raise ValueError(f"section question counts {actual_counts}, expected {expected_counts}")
    return ExamDocument(
        id=uuid5(NAMESPACE_URL, f"{blueprint.id}:exam"),
        blueprint_id=blueprint.id,
        title=blueprint.title,
        subject_profile=blueprint.subject_profile,
        duration_minutes=blueprint.duration_minutes,
        total_score=blueprint.total_score,
        questions=bundles,
    )


def render_exam_markdown(exam: ExamDocument) -> str:
    lines = [f"# {exam.title}", "", f"考试时间：{exam.duration_minutes} 分钟", ""]
    for bundle in exam.questions:
        question = bundle.question
        lines.extend([f"## {question.number}. （{question.score} 分）", "", question.statement, ""])
        lines.extend(f"- {option}" for option in question.options)
        if question.options:
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_solutions_markdown(exam: ExamDocument) -> str:
    lines = [f"# {exam.title}参考答案", ""]
    for bundle in exam.questions:
        lines.extend([f"## {bundle.question.number}. {bundle.solution.final_answer}", ""])
        lines.extend(
            f"{index}. {step.description}"
            for index, step in enumerate(bundle.solution.steps, start=1)
        )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_rubrics_markdown(exam: ExamDocument) -> str:
    lines = [f"# {exam.title}评分标准", ""]
    for bundle in exam.questions:
        lines.append(f"## 第 {bundle.question.number} 题（{bundle.rubric.max_score} 分）")
        lines.append("")
        lines.extend(f"- {item.description}：{item.score} 分" for item in bundle.rubric.items)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
