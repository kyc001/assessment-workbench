import asyncio
import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from assessment_workbench.compilers import TectonicCompiler
from assessment_workbench.domain import (
    ArbitrationAction,
    ArbitrationDecision,
    BlueprintDraft,
    ExamBlueprint,
    ExamDocument,
    ExamQuestionBundle,
    FindingSeverity,
    FindingTarget,
    GenerationMetadata,
    QuestionDraft,
    QuestionType,
    QuestionVersion,
    ReviewFinding,
    ReviewReport,
    RubricDraft,
    RubricVersion,
    SolutionDraft,
    SolutionVersion,
    SubjectProfile,
    SubjectProfileCandidate,
    WorkflowRun,
)
from assessment_workbench.latex import ExamView, GenericLatexRenderer
from assessment_workbench.ports import StructuredModel
from assessment_workbench.storage import ArtifactStore, RunStore
from assessment_workbench.workflow import WorkflowEngine

SUPPORTED_REVIEWERS = frozenset(
    {"mathematical", "subject", "solvability", "rubric", "pedagogical", "structure"}
)


@dataclass(frozen=True)
class ModelRouter:
    standard: StructuredModel
    strong: StructuredModel


class ExamAgentWorkflow:
    def __init__(
        self,
        models: ModelRouter,
        artifacts: ArtifactStore,
        runs: RunStore,
        *,
        max_question_attempts: int = 3,
        compiler: TectonicCompiler | None = None,
    ) -> None:
        self.models = models
        self.artifacts = artifacts
        self.engine = WorkflowEngine(runs)
        self.max_question_attempts = max_question_attempts
        self.compiler = compiler

    async def execute(
        self,
        *,
        subject: str,
        target_level: str,
        requirements: str,
        source_context: str = "",
    ) -> tuple[WorkflowRun, dict[str, Any]]:
        async def research_subject(state: dict[str, Any]) -> dict[str, Any]:
            candidate = await self.models.strong.complete(
                role="subject_researcher",
                system_prompt=(
                    "Research a subject profile for exam generation. Use only the supplied "
                    "context and "
                    "requirements. Choose reviewers only from: mathematical, subject, solvability, "
                    "rubric, pedagogical, structure. Do not invent source citations."
                ),
                user_prompt=_json_prompt(
                    subject=subject,
                    target_level=target_level,
                    requirements=requirements,
                    source_context=source_context,
                ),
                response_model=SubjectProfileCandidate,
                prompt_version="subject-research-v1",
                run_id=str(state["run_id"]),
            )
            unknown = set(candidate.reviewers) - SUPPORTED_REVIEWERS
            if unknown:
                raise ValueError(f"subject profile proposed unknown reviewers: {sorted(unknown)}")
            profile = SubjectProfile(
                id=candidate.subject_id,
                display_name=candidate.display_name,
                supported_question_types=candidate.supported_question_types,
                reviewers=candidate.reviewers,
                tools=candidate.tools,
                latex_template="generic-v1",
                difficulty_dimensions=candidate.difficulty_dimensions,
            )
            artifact = self.artifacts.write_json(
                state["run_id"],
                "subject-profile.json",
                profile.model_dump(mode="json"),
                created_by_phase="SUBJECT_RESEARCHING",
            )
            return {"profile": profile, "output_artifact_ids": [artifact.id]}

        async def plan_exam(state: dict[str, Any]) -> dict[str, Any]:
            profile: SubjectProfile = state["profile"]
            draft = await self.models.strong.complete(
                role="exam_blueprint_planner",
                system_prompt=(
                    "Create a complete exam blueprint. Section scores and coverage scores must "
                    "each sum exactly to total_score. Use only question types supported by the "
                    "subject profile."
                ),
                user_prompt=_json_prompt(
                    subject_profile=profile.model_dump(mode="json"),
                    target_level=target_level,
                    requirements=requirements,
                    source_context=source_context,
                ),
                response_model=BlueprintDraft,
                prompt_version="exam-blueprint-v1",
                run_id=str(state["run_id"]),
            )
            unsupported = {section.question_type for section in draft.sections} - set(
                profile.supported_question_types
            )
            if unsupported:
                names = sorted(item.value for item in unsupported)
                raise ValueError(f"blueprint uses unsupported question types: {names}")
            blueprint = ExamBlueprint(
                id=f"{profile.id}-{uuid4().hex[:12]}",
                subject_profile=profile.id,
                **draft.model_dump(),
            )
            artifact = self.artifacts.write_json(
                state["run_id"],
                "exam-blueprint.json",
                blueprint.model_dump(mode="json"),
                created_by_phase="EXAM_PLANNING",
            )
            return {"blueprint": blueprint, "output_artifact_ids": [artifact.id]}

        async def generate_questions(state: dict[str, Any]) -> dict[str, Any]:
            profile: SubjectProfile = state["profile"]
            blueprint: ExamBlueprint = state["blueprint"]
            bundles: list[ExamQuestionBundle] = []
            number = 1
            for section in blueprint.sections:
                for slot in range(section.count):
                    bundles.append(
                        await self._generate_question(
                            run_id=state["run_id"],
                            profile=profile,
                            blueprint=blueprint,
                            number=number,
                            section_id=section.id,
                            slot=slot + 1,
                            question_type=section.question_type.value,
                            score=section.score_each,
                            topic_tags=section.topic_tags,
                            source_context=source_context,
                        )
                    )
                    number += 1
            artifact = self.artifacts.write_json(
                state["run_id"],
                "question-bundles.json",
                [bundle.model_dump(mode="json") for bundle in bundles],
                created_by_phase="QUESTIONS_GENERATING",
            )
            return {"bundles": bundles, "output_artifact_ids": [artifact.id]}

        async def assemble(state: dict[str, Any]) -> dict[str, Any]:
            blueprint: ExamBlueprint = state["blueprint"]
            exam = ExamDocument(
                blueprint_id=blueprint.id,
                title=blueprint.title,
                subject_profile=blueprint.subject_profile,
                duration_minutes=blueprint.duration_minutes,
                total_score=blueprint.total_score,
                questions=state["bundles"],
            )
            artifact = self.artifacts.write_json(
                state["run_id"],
                "exam.json",
                exam.model_dump(mode="json"),
                created_by_phase="EXAM_ASSEMBLING",
            )
            return {"exam": exam, "artifacts": [artifact], "output_artifact_ids": [artifact.id]}

        async def export(state: dict[str, Any]) -> dict[str, Any]:
            exam: ExamDocument = state["exam"]
            renderer = GenericLatexRenderer()
            outputs = list(state["artifacts"])
            for view in ExamView:
                source = renderer.render(exam, view)
                outputs.append(
                    self.artifacts.write_bytes(
                        state["run_id"],
                        f"exam-{view.value}.tex",
                        source.encode("utf-8"),
                        media_type="application/x-tex",
                        created_by_phase="LATEX_FORMATTING",
                    )
                )
                if self.compiler is not None:
                    result = self.compiler.compile(source, job_name=f"exam-{view.value}")
                    outputs.append(
                        self.artifacts.write_bytes(
                            state["run_id"],
                            f"exam-{view.value}.pdf",
                            result.pdf,
                            media_type="application/pdf",
                            created_by_phase="PDF_COMPILING",
                        )
                    )
                    outputs.append(
                        self.artifacts.write_bytes(
                            state["run_id"],
                            f"exam-{view.value}.tectonic.log",
                            result.log.encode("utf-8"),
                            media_type="text/plain",
                            created_by_phase="PDF_COMPILING",
                        )
                    )
            return {"artifacts": outputs, "output_artifact_ids": [item.id for item in outputs]}

        return await self.engine.execute(
            "exam_agent_generation",
            [
                ("SUBJECT_RESEARCHING", research_subject),
                ("EXAM_PLANNING", plan_exam),
                ("QUESTIONS_GENERATING", generate_questions),
                ("EXAM_ASSEMBLING", assemble),
                ("LATEX_FORMATTING", export),
            ],
        )

    async def _generate_question(
        self,
        *,
        run_id: UUID,
        profile: SubjectProfile,
        blueprint: ExamBlueprint,
        number: int,
        section_id: str,
        slot: int,
        question_type: str,
        score: int,
        topic_tags: list[str],
        source_context: str,
    ) -> ExamQuestionBundle:
        question_id = uuid4()
        solution_id = uuid4()
        rubric_id = uuid4()
        question: QuestionVersion | None = None
        solution: SolutionVersion | None = None
        rubric: RubricVersion | None = None
        feedback: dict[str, list[str]] = {"writer": [], "solver": [], "rubric": []}
        retry_target = ArbitrationAction.RETRY_ALL
        for attempt in range(1, self.max_question_attempts + 1):
            rewrite_question = retry_target in {
                ArbitrationAction.RETRY_PROBLEM,
                ArbitrationAction.RETRY_ALL,
            }
            rewrite_solution = rewrite_question or retry_target is ArbitrationAction.RETRY_SOLUTION
            rewrite_rubric = rewrite_solution or retry_target is ArbitrationAction.RETRY_RUBRIC
            if rewrite_question:
                question_draft = await self.models.standard.complete(
                    role="question_writer",
                    system_prompt=(
                        "Write one original, self-contained, solvable exam question. Do not "
                        "provide its answer. Multiple-choice questions require at least four "
                        "options; other types require none."
                    ),
                    user_prompt=_json_prompt(
                        profile=profile.model_dump(mode="json"),
                        blueprint=blueprint.model_dump(mode="json"),
                        section_id=section_id,
                        slot=slot,
                        number=number,
                        question_type=question_type,
                        score=score,
                        topic_tags=topic_tags,
                        source_context=source_context,
                        revision_feedback=feedback["writer"],
                    ),
                    response_model=QuestionDraft,
                    prompt_version="question-writer-v1",
                    run_id=str(run_id),
                )
                previous_question = question
                question = QuestionVersion(
                    question_id=question_id,
                    version=(previous_question.version + 1) if previous_question else 1,
                    parent_version_id=previous_question.id if previous_question else None,
                    number=number,
                    question_type=QuestionType(question_type),
                    score=score,
                    metadata=GenerationMetadata(
                        role="question_writer",
                        model="routed",
                        prompt_version="question-writer-v1",
                    ),
                    **question_draft.model_dump(),
                )
            assert question is not None
            if rewrite_solution:
                solution_draft = await self.models.strong.complete(
                    role="independent_solver",
                    system_prompt=(
                        "Solve the supplied question independently. Check every step and do not "
                        "assume an intended answer. Return a rigorous solution."
                    ),
                    user_prompt=_json_prompt(
                        question=question.model_dump(mode="json"),
                        source_context=source_context,
                        revision_feedback=feedback["solver"],
                    ),
                    response_model=SolutionDraft,
                    prompt_version="independent-solver-v1",
                    run_id=str(run_id),
                )
                previous_solution = solution
                solution = SolutionVersion(
                    solution_id=solution_id,
                    question_version_id=question.id,
                    version=(previous_solution.version + 1) if previous_solution else 1,
                    parent_version_id=previous_solution.id if previous_solution else None,
                    metadata=GenerationMetadata(
                        role="independent_solver",
                        model="routed",
                        prompt_version="independent-solver-v1",
                    ),
                    **solution_draft.model_dump(),
                )
            assert solution is not None
            if rewrite_rubric:
                rubric_draft = await self.models.standard.complete(
                    role="rubric_builder",
                    system_prompt=(
                        "Build a non-overlapping analytic rubric from the question and independent "
                        "solution. Rubric item scores must sum exactly to the question score."
                    ),
                    user_prompt=_json_prompt(
                        question=question.model_dump(mode="json"),
                        solution=solution.model_dump(mode="json"),
                        score=score,
                        revision_feedback=feedback["rubric"],
                    ),
                    response_model=RubricDraft,
                    prompt_version="rubric-builder-v1",
                    run_id=str(run_id),
                )
                previous_rubric = rubric
                rubric = RubricVersion(
                    rubric_id=rubric_id,
                    question_version_id=question.id,
                    solution_version_id=solution.id,
                    version=(previous_rubric.version + 1) if previous_rubric else 1,
                    parent_version_id=previous_rubric.id if previous_rubric else None,
                    max_score=score,
                    metadata=GenerationMetadata(
                        role="rubric_builder",
                        model="routed",
                        prompt_version="rubric-builder-v1",
                    ),
                    **rubric_draft.model_dump(),
                )
            assert rubric is not None
            bundle = ExamQuestionBundle(question=question, solution=solution, rubric=rubric)
            reports = await self._review(run_id, profile, bundle)
            decision = await self._arbitrate(run_id, bundle, reports)
            self._persist_attempt(run_id, number, attempt, bundle, reports, decision)
            if decision.action in {ArbitrationAction.PASS, ArbitrationAction.PASS_WITH_WARNINGS}:
                return bundle
            if decision.action in {ArbitrationAction.ABORT, ArbitrationAction.ESCALATE_HUMAN}:
                raise RuntimeError(f"question {number} arbitration: {decision.action}")
            feedback = {
                "writer": decision.writer_feedback,
                "solver": decision.solver_feedback,
                "rubric": decision.rubric_feedback,
            }
            retry_target = decision.action
        raise RuntimeError(f"question {number} exhausted retry budget")

    async def _review(
        self, run_id: UUID, profile: SubjectProfile, bundle: ExamQuestionBundle
    ) -> list[ReviewReport]:
        async def review(name: str) -> ReviewReport:
            if name == "structure":
                return _structure_review(bundle)
            return await self.models.standard.complete(
                role=f"{name}_reviewer",
                system_prompt=(
                    f"Act as the {name} reviewer. Independently inspect the question, "
                    "solution, and "
                    "rubric. Mark passed false for any error or fatal finding."
                ),
                user_prompt=_json_prompt(bundle=bundle.model_dump(mode="json")),
                response_model=ReviewReport,
                prompt_version="question-review-v1",
                run_id=str(run_id),
            )

        return list(await asyncio.gather(*(review(name) for name in profile.reviewers)))

    async def _arbitrate(
        self, run_id: UUID, bundle: ExamQuestionBundle, reports: list[ReviewReport]
    ) -> ArbitrationDecision:
        decision = await self.models.strong.complete(
            role="question_arbiter",
            system_prompt=(
                "Arbitrate independent review reports. Retry the earliest invalid dependency: "
                "a question problem invalidates solution and rubric; a solution problem "
                "invalidates rubric. Never pass an error or fatal finding."
            ),
            user_prompt=_json_prompt(
                bundle=bundle.model_dump(mode="json"),
                reports=[report.model_dump(mode="json") for report in reports],
            ),
            response_model=ArbitrationDecision,
            prompt_version="question-arbiter-v1",
            run_id=str(run_id),
        )
        severe = [
            finding
            for report in reports
            for finding in report.findings
            if finding.severity in {FindingSeverity.ERROR, FindingSeverity.FATAL}
        ]
        if severe and decision.action in {
            ArbitrationAction.PASS,
            ArbitrationAction.PASS_WITH_WARNINGS,
        }:
            raise ValueError("arbiter attempted to pass unresolved error findings")
        return decision

    def _persist_attempt(
        self,
        run_id: UUID,
        number: int,
        attempt: int,
        bundle: ExamQuestionBundle,
        reports: list[ReviewReport],
        decision: ArbitrationDecision,
    ) -> None:
        prefix = f"questions/{number:02d}"
        for name, payload in (
            ("question.json", bundle.question.model_dump(mode="json")),
            ("solution.json", bundle.solution.model_dump(mode="json")),
            ("rubric.json", bundle.rubric.model_dump(mode="json")),
            ("reviews.json", [report.model_dump(mode="json") for report in reports]),
            ("arbitration.json", decision.model_dump(mode="json")),
        ):
            self.artifacts.write_json(
                run_id,
                f"{prefix}/{name}",
                payload,
                created_by_phase=f"QUESTION_ATTEMPT_{attempt}",
            )


def _json_prompt(**payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def _structure_review(bundle: ExamQuestionBundle) -> ReviewReport:
    findings: list[ReviewFinding] = []
    if (
        bundle.question.question_type is QuestionType.MULTIPLE_CHOICE
        and len(bundle.question.options) < 4
    ):
        findings.append(
            ReviewFinding(
                code="choice_options",
                severity=FindingSeverity.ERROR,
                target=FindingTarget.QUESTION,
                message="Multiple-choice question has fewer than four options.",
            )
        )
    if sum(item.score for item in bundle.rubric.items) != bundle.question.score:
        findings.append(
            ReviewFinding(
                code="rubric_score_total",
                severity=FindingSeverity.FATAL,
                target=FindingTarget.RUBRIC,
                message="Rubric scores do not sum to the question score.",
            )
        )
    return ReviewReport(
        reviewer="structure",
        passed=not findings,
        findings=findings,
        summary="Deterministic domain and scoring checks.",
    )
