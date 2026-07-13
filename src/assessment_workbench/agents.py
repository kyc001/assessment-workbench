import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import httpx
from pydantic import ValidationError

from assessment_workbench.compilers import LatexCompiler
from assessment_workbench.domain import (
    ArbitrationAction,
    ArbitrationDecision,
    ArtifactRef,
    BlueprintDraft,
    ExamBlueprint,
    ExamDocument,
    ExamGenerationRequest,
    ExamPlanningMode,
    ExamPlanningRecord,
    ExamQuestionBundle,
    FindingSeverity,
    FindingTarget,
    GenerationMetadata,
    QuestionDraft,
    QuestionGenerationRequest,
    QuestionPlan,
    QuestionPlanDraft,
    QuestionPlanSetDraft,
    QuestionSlot,
    QuestionType,
    QuestionVersion,
    ReviewFinding,
    ReviewReport,
    RubricDraft,
    RubricVersion,
    RunStatus,
    SolutionDraft,
    SolutionVersion,
    SubjectProfile,
    SubjectProfileCandidate,
    WorkflowCheckpoint,
    WorkflowRun,
)
from assessment_workbench.latex_service import ExamLatexService
from assessment_workbench.ports import StructuredModel
from assessment_workbench.storage import ArtifactStore, RunStore
from assessment_workbench.workflow import Step, WorkflowEngine

SUPPORTED_REVIEWERS = frozenset(
    {"mathematical", "subject", "solvability", "rubric", "pedagogical", "structure"}
)


@dataclass(frozen=True)
class ModelRouter:
    standard: StructuredModel
    strong: StructuredModel


@dataclass(frozen=True)
class QuestionGenerationOutcome:
    bundle: ExamQuestionBundle
    requires_human_review: bool = False


class ExamAgentWorkflow:
    def __init__(
        self,
        models: ModelRouter,
        artifacts: ArtifactStore,
        runs: RunStore,
        *,
        max_question_attempts: int = 3,
        max_total_question_rounds: int = 7,
        max_draft_validation_attempts: int = 3,
        max_parallel_questions: int = 1,
        compiler: LatexCompiler | None = None,
    ) -> None:
        if max_question_attempts < 1:
            raise ValueError("max_question_attempts must be at least 1")
        if max_total_question_rounds < 1:
            raise ValueError("max_total_question_rounds must be at least 1")
        if max_draft_validation_attempts < 1:
            raise ValueError("max_draft_validation_attempts must be at least 1")
        if max_parallel_questions < 1:
            raise ValueError("max_parallel_questions must be at least 1")
        self.models = models
        self.artifacts = artifacts
        self.runs = runs
        self.engine = WorkflowEngine(runs)
        self.max_question_attempts = max_question_attempts
        self.max_total_question_rounds = max_total_question_rounds
        self.max_draft_validation_attempts = max_draft_validation_attempts
        self.max_parallel_questions = max_parallel_questions
        self.compiler = compiler

    async def execute(
        self,
        *,
        subject: str,
        target_level: str,
        requirements: str,
        source_context: str = "",
        subject_profile: SubjectProfile | None = None,
        blueprint: ExamBlueprint | None = None,
        require_blueprint_approval: bool = False,
        require_exam_approval: bool = False,
        on_run_created: Callable[[WorkflowRun], None] | None = None,
    ) -> tuple[WorkflowRun, dict[str, Any]]:
        request = ExamGenerationRequest(
            subject=subject,
            target_level=target_level,
            requirements=requirements,
            source_context=source_context,
            subject_profile=subject_profile,
            blueprint=blueprint,
            require_blueprint_approval=require_blueprint_approval,
            require_exam_approval=require_exam_approval,
        )
        return await self._run_exam_request(request, on_run_created=on_run_created)

    async def resume(self, run_id: UUID) -> tuple[WorkflowRun, dict[str, Any]]:
        checkpoint = self.runs.get_checkpoint(run_id)
        if checkpoint is None:
            raise ValueError(f"run has no checkpoint: {run_id}")
        if not checkpoint.artifact_bindings:
            raise ValueError(f"run uses a legacy checkpoint without artifact bindings: {run_id}")
        request_artifact_id = checkpoint.artifact_bindings.get("request")
        if request_artifact_id is None:
            artifact = self.artifacts.latest(run_id, "exam-request.json")
            if artifact is None:
                raise ValueError(f"run has no exam request artifact: {run_id}")
            request_artifact_id = artifact.id
        request = ExamGenerationRequest.model_validate(
            self.artifacts.read_json(request_artifact_id)
        )
        restored_state = self._restore_exam_state(checkpoint)
        return await self._run_exam_request(
            request,
            resume_run_id=run_id,
            restored_state=restored_state,
        )

    async def _run_exam_request(
        self,
        request: ExamGenerationRequest,
        *,
        resume_run_id: UUID | None = None,
        restored_state: dict[str, Any] | None = None,
        on_run_created: Callable[[WorkflowRun], None] | None = None,
    ) -> tuple[WorkflowRun, dict[str, Any]]:
        subject = request.subject
        target_level = request.target_level
        requirements = request.requirements
        source_context = request.source_context
        provided_profile = request.subject_profile
        provided_blueprint = request.blueprint
        planning_mode = (
            ExamPlanningMode.PRESET if provided_profile is not None else ExamPlanningMode.AGENT
        )
        if provided_profile is not None and provided_blueprint is not None:
            _validate_subject_profile(provided_profile)
            _validate_blueprint(provided_profile, provided_blueprint)
            if provided_blueprint.target_level != target_level:
                raise ValueError(
                    "preset blueprint target_level does not match the requested target_level"
                )

        async def research_subject(state: dict[str, Any]) -> dict[str, Any]:
            request_artifact = self.artifacts.write_json(
                state["run_id"],
                "exam-request.json",
                request.model_dump(mode="json"),
                created_by_phase="SUBJECT_RESEARCHING",
            )
            if provided_profile is None:
                candidate = await self.models.strong.complete(
                    role="subject_researcher",
                    system_prompt=(
                        "Research a subject profile for exam generation. Use only the supplied "
                        "context and requirements. Choose reviewers only from: mathematical, "
                        "subject, solvability, rubric, pedagogical, structure. Do not invent "
                        "source citations."
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
                profile = SubjectProfile(
                    id=candidate.subject_id,
                    display_name=candidate.display_name,
                    supported_question_types=candidate.supported_question_types,
                    reviewers=[reviewer.value for reviewer in candidate.reviewers],
                    tools=candidate.tools,
                    latex_template="generic-v1",
                    difficulty_dimensions=candidate.difficulty_dimensions,
                )
            else:
                profile = provided_profile
            _validate_subject_profile(profile)
            artifact = self.artifacts.write_json(
                state["run_id"],
                "subject-profile.json",
                profile.model_dump(mode="json"),
                created_by_phase="SUBJECT_RESEARCHING",
            )
            return {
                "profile": profile,
                "output_artifact_ids": [request_artifact.id, artifact.id],
                "_checkpoint_artifacts": {
                    "request": request_artifact.id,
                    "profile": artifact.id,
                },
            }

        async def plan_exam(state: dict[str, Any]) -> dict[str, Any]:
            profile: SubjectProfile = state["profile"]
            if provided_blueprint is None:
                draft = await self.models.strong.complete(
                    role="exam_blueprint_planner",
                    system_prompt=(
                        "Create a complete exam blueprint. Section scores and coverage scores "
                        "must each sum exactly to total_score. Use only question types supported "
                        "by the subject profile. For each section, use score_each for uniform "
                        "scores or question_scores for per-question scores, never both."
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
                exam_blueprint = ExamBlueprint(
                    id=f"{profile.id}-{uuid4().hex[:12]}",
                    subject_profile=profile.id,
                    **draft.model_dump(),
                )
            else:
                exam_blueprint = provided_blueprint
            _validate_blueprint(profile, exam_blueprint)
            blueprint_artifact = self.artifacts.write_json(
                state["run_id"],
                "exam-blueprint.json",
                exam_blueprint.model_dump(mode="json"),
                created_by_phase="EXAM_PLANNING",
            )
            planning = ExamPlanningRecord(
                mode=planning_mode,
                subject_profile_id=profile.id,
                subject_profile_version=profile.version,
                blueprint_id=exam_blueprint.id,
                blueprint_version=exam_blueprint.version,
            )
            planning_artifact = self.artifacts.write_json(
                state["run_id"],
                "exam-planning.json",
                planning.model_dump(mode="json"),
                created_by_phase="EXAM_PLANNING",
            )
            return {
                "blueprint": exam_blueprint,
                "planning": planning,
                "output_artifact_ids": [blueprint_artifact.id, planning_artifact.id],
                "_checkpoint_artifacts": {
                    "blueprint": blueprint_artifact.id,
                    "planning": planning_artifact.id,
                },
            }

        async def approve_blueprint(state: dict[str, Any]) -> dict[str, Any]:
            if not request.require_blueprint_approval or planning_mode is ExamPlanningMode.PRESET:
                return {}
            bindings = state.get("_checkpoint_artifacts", {})
            return {
                "_human_review": {
                    "prompt": "Approve the generated exam blueprint before question planning.",
                    "artifact_ids": [bindings["blueprint"], bindings["planning"]],
                    "retry_phase": "EXAM_PLANNING",
                }
            }

        async def plan_questions(state: dict[str, Any]) -> dict[str, Any]:
            profile: SubjectProfile = state["profile"]
            exam_blueprint: ExamBlueprint = state["blueprint"]
            slots = _blueprint_slots(exam_blueprint)
            planning_feedback: list[str] = []
            for planning_attempt in range(3):
                draft = await self.models.strong.complete(
                    role="question_set_planner",
                    system_prompt=(
                        "Plan every question in a complete exam before any question is written. "
                        "Produce exactly one executable plan for each supplied slot and no other "
                        "plans. Each plan must define a concrete construction, exact assessed "
                        "skill, difficulty, expected answer form, solution outline, rubric focus, "
                        "verification methods, estimated time, and constraints that prevent "
                        "overlap with the other questions. Do not write the final question or its "
                        "full solution. Keep all structural fields aligned with the supplied slot "
                        "list."
                    ),
                    user_prompt=_json_prompt(
                        subject_profile=profile.model_dump(mode="json"),
                        blueprint=exam_blueprint.model_dump(mode="json"),
                        slots=[slot.model_dump(mode="json") for slot in slots],
                        requirements=requirements,
                        source_context=source_context,
                        revision_feedback=planning_feedback,
                    ),
                    response_model=QuestionPlanSetDraft,
                    prompt_version="question-set-planner-v1",
                    run_id=str(state["run_id"]),
                )
                try:
                    question_plans = _materialize_question_plans(exam_blueprint, draft.plans)
                    break
                except ValueError as exc:
                    if planning_attempt == 2:
                        raise
                    planning_feedback = [
                        "The previous plan set failed deterministic slot validation. Return "
                        f"exactly the supplied slots and no others. Error: {exc}"
                    ]
            else:
                raise RuntimeError("question planning retry loop exited unexpectedly")
            artifact = self.artifacts.write_json(
                state["run_id"],
                "question-plans.json",
                [plan.model_dump(mode="json") for plan in question_plans],
                created_by_phase="QUESTION_PLANNING",
            )
            return {
                "question_plans": question_plans,
                "output_artifact_ids": [artifact.id],
                "_checkpoint_artifacts": {"question_plans": artifact.id},
            }

        async def generate_questions(state: dict[str, Any]) -> dict[str, Any]:
            profile: SubjectProfile = state["profile"]
            blueprint: ExamBlueprint = state["blueprint"]
            question_plans: list[QuestionPlan] = state["question_plans"]
            semaphore = asyncio.Semaphore(self.max_parallel_questions)
            manifest_lock = asyncio.Lock()
            records_by_number: dict[int, dict[str, Any]] = {
                plan.number: {
                    "question_number": plan.number,
                    "plan_id": plan.id,
                    "run_id": None,
                    "status": "queued",
                    "error": None,
                    "bundle_artifact_id": None,
                    "bundle_path": None,
                    "editable_path": None,
                    "requires_human_review": False,
                }
                for plan in question_plans
            }
            existing_records: list[object] = []
            restored_records = state.get("question_runs")
            if isinstance(restored_records, list):
                existing_records = restored_records
            else:
                try:
                    editable_records = self.artifacts.read_editable_json(
                        state["run_id"], "question-runs.json"
                    )
                except (FileNotFoundError, OSError, ValueError):
                    pass
                else:
                    if isinstance(editable_records, list):
                        existing_records = editable_records
            for record in existing_records:
                if not isinstance(record, dict):
                    continue
                try:
                    number = int(record["question_number"])
                except (KeyError, TypeError, ValueError):
                    continue
                plan = next((item for item in question_plans if item.number == number), None)
                if plan is None or record.get("plan_id") != plan.id:
                    continue
                records_by_number[number] = {**records_by_number[number], **record}

            def ordered_records() -> list[dict[str, Any]]:
                return [records_by_number[number] for number in sorted(records_by_number)]

            def write_live_manifest() -> None:
                self.artifacts.write_editable_json(
                    state["run_id"],
                    "question-runs.json",
                    ordered_records(),
                )

            async def update_record(
                number: int,
                record: dict[str, Any],
                *,
                immutable_snapshot: bool,
            ) -> ArtifactRef | None:
                async with manifest_lock:
                    records_by_number[number] = record
                    write_live_manifest()
                    if not immutable_snapshot:
                        return None
                    return self.artifacts.write_json(
                        state["run_id"],
                        "question-runs.json",
                        ordered_records(),
                        created_by_phase="QUESTION_CHILD_COMPLETED",
                    )

            write_live_manifest()
            self.artifacts.write_json(
                state["run_id"],
                "question-runs.json",
                ordered_records(),
                created_by_phase="QUESTIONS_DISPATCHING",
            )

            reused_results: dict[int, tuple[ExamQuestionBundle, ArtifactRef]] = {}
            pending_plans: list[QuestionPlan] = []
            for plan in question_plans:
                record = records_by_number[plan.number]
                artifact_id = record.get("bundle_artifact_id")
                if record.get("status") == RunStatus.SUCCEEDED and artifact_id:
                    try:
                        bundle_artifact = self.artifacts.get(UUID(str(artifact_id)))
                        if bundle_artifact is None:
                            raise KeyError(artifact_id)
                        reused_bundle = ExamQuestionBundle.model_validate(
                            self.artifacts.read_json(bundle_artifact.id)
                        )
                    except (KeyError, OSError, ValueError):
                        pending_plans.append(plan)
                    else:
                        reused_results[plan.number] = (reused_bundle, bundle_artifact)
                    continue
                pending_plans.append(plan)

            async def generate(
                plan: QuestionPlan,
            ) -> tuple[QuestionPlan, WorkflowRun, dict[str, Any]]:
                async with semaphore:
                    await update_record(
                        plan.number,
                        {**records_by_number[plan.number], "status": "running"},
                        immutable_snapshot=False,
                    )
                    child_run, child_state = await self.generate_question_run(
                        profile=profile,
                        blueprint=blueprint,
                        plan=plan,
                        source_context=source_context,
                        parent_run_id=state["run_id"],
                    )
                    bundle = child_state.get("bundle")
                    bundle_artifact = child_state.get("bundle_artifact")
                    if (
                        child_run.status is RunStatus.SUCCEEDED
                        and isinstance(bundle, ExamQuestionBundle)
                        and isinstance(bundle_artifact, ArtifactRef)
                    ):
                        editable_path = self.artifacts.write_editable_json(
                            state["run_id"],
                            f"questions/{plan.number:02d}.json",
                            bundle.model_dump(mode="json"),
                        )
                    else:
                        editable_path = None
                    await update_record(
                        plan.number,
                        {
                            "question_number": plan.number,
                            "plan_id": plan.id,
                            "run_id": str(child_run.id),
                            "status": child_run.status,
                            "error": child_run.error,
                            "bundle_artifact_id": (
                                str(bundle_artifact.id)
                                if isinstance(bundle_artifact, ArtifactRef)
                                else None
                            ),
                            "bundle_path": (
                                str(bundle_artifact.path)
                                if isinstance(bundle_artifact, ArtifactRef)
                                else None
                            ),
                            "editable_path": (
                                str(editable_path) if editable_path is not None else None
                            ),
                            "requires_human_review": bool(
                                child_state.get("requires_human_review", False)
                            ),
                        },
                        immutable_snapshot=True,
                    )
                    return plan, child_run, child_state

            child_results = await asyncio.gather(*(generate(plan) for plan in pending_plans))
            bundles: list[ExamQuestionBundle] = [item[0] for item in reused_results.values()]
            failed_numbers: list[int] = []
            child_artifact_ids: list[UUID] = [item[1].id for item in reused_results.values()]
            for plan, child_run, child_state in child_results:
                child_bundle = child_state.get("bundle")
                bundle_artifact = child_state.get("bundle_artifact")
                if (
                    child_run.status is RunStatus.SUCCEEDED
                    and isinstance(child_bundle, ExamQuestionBundle)
                    and isinstance(bundle_artifact, ArtifactRef)
                ):
                    bundles.append(child_bundle)
                    child_artifact_ids.append(bundle_artifact.id)
                else:
                    failed_numbers.append(plan.number)

            bundles.sort(key=lambda item: item.question.number)
            child_records = ordered_records()
            runs_artifact = self.artifacts.write_json(
                state["run_id"],
                "question-runs.json",
                child_records,
                created_by_phase="QUESTIONS_GENERATING",
            )
            if failed_numbers:
                numbers = ", ".join(str(number) for number in failed_numbers)
                raise RuntimeError(f"question child runs failed: {numbers}")
            artifact = self.artifacts.write_json(
                state["run_id"],
                "question-bundles.json",
                [bundle.model_dump(mode="json") for bundle in bundles],
                created_by_phase="QUESTIONS_GENERATING",
            )
            return {
                "bundles": bundles,
                "question_runs": child_records,
                "output_artifact_ids": [
                    runs_artifact.id,
                    artifact.id,
                    *child_artifact_ids,
                ],
                "_checkpoint_artifacts": {
                    "question_runs": runs_artifact.id,
                    "bundles": artifact.id,
                },
                "_checkpoint_child_run_ids": [
                    UUID(str(record["run_id"])) for record in child_records if record.get("run_id")
                ],
            }

        async def assemble(state: dict[str, Any]) -> dict[str, Any]:
            blueprint: ExamBlueprint = state["blueprint"]
            exam = ExamDocument(
                blueprint_id=blueprint.id,
                title=blueprint.title,
                subject_profile=blueprint.subject_profile,
                duration_minutes=blueprint.duration_minutes,
                total_score=blueprint.total_score,
                language=blueprint.language,
                questions=state["bundles"],
            )
            artifact = self.artifacts.write_json(
                state["run_id"],
                "exam.json",
                exam.model_dump(mode="json"),
                created_by_phase="EXAM_ASSEMBLING",
            )
            return {
                "exam": exam,
                "artifacts": [artifact],
                "output_artifact_ids": [artifact.id],
                "_checkpoint_artifacts": {"exam": artifact.id},
            }

        async def approve_exam(state: dict[str, Any]) -> dict[str, Any]:
            question_runs = state.get("question_runs", [])
            escalated = any(
                isinstance(record, dict) and bool(record.get("requires_human_review"))
                for record in question_runs
            )
            if not request.require_exam_approval and not escalated:
                return {}
            bindings = state.get("_checkpoint_artifacts", {})
            prompt = "Approve the assembled exam before document generation."
            if escalated:
                prompt += " One or more questions were explicitly escalated by arbitration."
            return {
                "_human_review": {
                    "prompt": prompt,
                    "artifact_ids": [bindings["exam"], bindings["question_runs"]],
                    "retry_phase": "EXAM_ASSEMBLING",
                }
            }

        async def export(state: dict[str, Any]) -> dict[str, Any]:
            exam: ExamDocument = state["exam"]
            latex_service = ExamLatexService(compiler=self.compiler)
            outputs = list(state["artifacts"])
            for document in latex_service.build(exam):
                view = document.view
                source = document.source
                outputs.append(
                    self.artifacts.write_bytes(
                        state["run_id"],
                        f"exam-{view.value}.tex",
                        source.encode("utf-8"),
                        media_type="application/x-tex",
                        created_by_phase="LATEX_FORMATTING",
                    )
                )
                if document.compile_result is not None:
                    result = document.compile_result
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

        steps: list[tuple[str, Step]] = [
            ("SUBJECT_RESEARCHING", research_subject),
            ("EXAM_PLANNING", plan_exam),
            ("BLUEPRINT_APPROVAL", approve_blueprint),
            ("QUESTION_PLANNING", plan_questions),
            ("QUESTIONS_GENERATING", generate_questions),
            ("EXAM_ASSEMBLING", assemble),
            ("EXAM_APPROVAL", approve_exam),
            ("LATEX_FORMATTING", export),
        ]
        if resume_run_id is not None:
            return await self.engine.resume(
                resume_run_id,
                "exam_agent_generation",
                steps,
                context=restored_state,
            )
        return await self.engine.execute(
            "exam_agent_generation",
            steps,
            on_run_created=on_run_created,
        )

    def _restore_exam_state(self, checkpoint: WorkflowCheckpoint) -> dict[str, Any]:
        state: dict[str, Any] = {
            "_checkpoint_artifacts": dict(checkpoint.artifact_bindings),
            "_checkpoint_child_run_ids": list(checkpoint.child_run_ids),
            "input_artifact_ids": list(checkpoint.artifact_bindings.values()),
        }

        def payload(key: str) -> object | None:
            artifact_id = checkpoint.artifact_bindings.get(key)
            return self.artifacts.read_json(artifact_id) if artifact_id is not None else None

        profile_payload = payload("profile")
        if profile_payload is not None:
            state["profile"] = SubjectProfile.model_validate(profile_payload)
        blueprint_payload = payload("blueprint")
        if blueprint_payload is not None:
            state["blueprint"] = ExamBlueprint.model_validate(blueprint_payload)
        planning_payload = payload("planning")
        if planning_payload is not None:
            state["planning"] = ExamPlanningRecord.model_validate(planning_payload)
        plans_payload = payload("question_plans")
        if plans_payload is not None:
            if not isinstance(plans_payload, list):
                raise ValueError("question plan artifact is not a list")
            state["question_plans"] = [QuestionPlan.model_validate(item) for item in plans_payload]
        runs_payload = payload("question_runs")
        if runs_payload is not None:
            if not isinstance(runs_payload, list):
                raise ValueError("question run artifact is not a list")
            state["question_runs"] = runs_payload
        bundles_payload = payload("bundles")
        if bundles_payload is not None:
            if not isinstance(bundles_payload, list):
                raise ValueError("question bundle artifact is not a list")
            state["bundles"] = [ExamQuestionBundle.model_validate(item) for item in bundles_payload]
        exam_payload = payload("exam")
        if exam_payload is not None:
            state["exam"] = ExamDocument.model_validate(exam_payload)
            exam_artifact_id = checkpoint.artifact_bindings["exam"]
            exam_artifact = self.artifacts.get(exam_artifact_id)
            if exam_artifact is None:
                raise ValueError(f"exam artifact metadata is missing: {exam_artifact_id}")
            state["artifacts"] = [exam_artifact]
        return state

    async def generate_question_run(
        self,
        *,
        profile: SubjectProfile,
        blueprint: ExamBlueprint,
        plan: QuestionPlan,
        source_context: str = "",
        parent_run_id: UUID | None = None,
        on_run_created: Callable[[WorkflowRun], None] | None = None,
    ) -> tuple[WorkflowRun, dict[str, Any]]:
        request = QuestionGenerationRequest(
            profile=profile,
            blueprint=blueprint,
            plan=plan,
            source_context=source_context,
            parent_run_id=parent_run_id,
        )
        return await self._run_question_request(request, on_run_created=on_run_created)

    async def resume_question_run(self, run_id: UUID) -> tuple[WorkflowRun, dict[str, Any]]:
        checkpoint = self.runs.get_checkpoint(run_id)
        if checkpoint is None:
            raise ValueError(f"run has no checkpoint: {run_id}")
        if not checkpoint.artifact_bindings:
            raise ValueError(f"run uses a legacy checkpoint without artifact bindings: {run_id}")
        request_artifact_id = checkpoint.artifact_bindings.get("request")
        if request_artifact_id is None:
            artifact = self.artifacts.latest(run_id, "question-request.json")
            if artifact is None:
                raise ValueError(f"run has no question request artifact: {run_id}")
            request_artifact_id = artifact.id
        request = QuestionGenerationRequest.model_validate(
            self.artifacts.read_json(request_artifact_id)
        )
        restored_state = self._restore_question_state(checkpoint)
        return await self._run_question_request(
            request,
            resume_run_id=run_id,
            restored_state=restored_state,
        )

    async def _run_question_request(
        self,
        request: QuestionGenerationRequest,
        *,
        resume_run_id: UUID | None = None,
        restored_state: dict[str, Any] | None = None,
        on_run_created: Callable[[WorkflowRun], None] | None = None,
    ) -> tuple[WorkflowRun, dict[str, Any]]:
        async def generate_child(state: dict[str, Any]) -> dict[str, Any]:
            request_artifact = self.artifacts.write_json(
                state["run_id"],
                "question-request.json",
                request.model_dump(mode="json"),
                created_by_phase="QUESTION_GENERATING",
            )
            plan_artifact = self.artifacts.write_json(
                state["run_id"],
                "question-plan.json",
                request.plan.model_dump(mode="json"),
                created_by_phase="QUESTION_GENERATING",
            )
            for transport_attempt in range(2):
                try:
                    outcome = await self._generate_question(
                        run_id=state["run_id"],
                        profile=request.profile,
                        blueprint=request.blueprint,
                        plan=request.plan,
                        source_context=request.source_context,
                    )
                    break
                except httpx.HTTPError:
                    if transport_attempt == 1:
                        raise
                    await asyncio.sleep(2)
            else:
                raise RuntimeError("question transport retry loop exited unexpectedly")
            bundle_artifact = self.artifacts.write_json(
                state["run_id"],
                "question-bundle.json",
                outcome.bundle.model_dump(mode="json"),
                created_by_phase="QUESTION_GENERATING",
            )
            return {
                "bundle": outcome.bundle,
                "bundle_artifact": bundle_artifact,
                "requires_human_review": outcome.requires_human_review,
                "output_artifact_ids": [
                    request_artifact.id,
                    plan_artifact.id,
                    bundle_artifact.id,
                ],
                "_checkpoint_artifacts": {
                    "request": request_artifact.id,
                    "plan": plan_artifact.id,
                    "bundle": bundle_artifact.id,
                },
            }

        steps: list[tuple[str, Step]] = [("QUESTION_GENERATING", generate_child)]
        engine = WorkflowEngine(self.runs)
        if resume_run_id is not None:
            return await engine.resume(
                resume_run_id,
                "exam_question_generation",
                steps,
                context=restored_state,
                parent_run_id=request.parent_run_id,
            )
        return await engine.execute(
            "exam_question_generation",
            steps,
            parent_run_id=request.parent_run_id,
            on_run_created=on_run_created,
        )

    def _restore_question_state(self, checkpoint: WorkflowCheckpoint) -> dict[str, Any]:
        state: dict[str, Any] = {
            "_checkpoint_artifacts": dict(checkpoint.artifact_bindings),
            "input_artifact_ids": list(checkpoint.artifact_bindings.values()),
        }
        bundle_artifact_id = checkpoint.artifact_bindings.get("bundle")
        if bundle_artifact_id is not None:
            state["bundle"] = ExamQuestionBundle.model_validate(
                self.artifacts.read_json(bundle_artifact_id)
            )
            bundle_artifact = self.artifacts.get(bundle_artifact_id)
            if bundle_artifact is None:
                raise ValueError(f"bundle artifact metadata is missing: {bundle_artifact_id}")
            state["bundle_artifact"] = bundle_artifact
        return state

    async def _generate_question(
        self,
        *,
        run_id: UUID,
        profile: SubjectProfile,
        blueprint: ExamBlueprint,
        plan: QuestionPlan,
        source_context: str,
    ) -> QuestionGenerationOutcome:
        question_id = uuid4()
        solution_id = uuid4()
        rubric_id = uuid4()
        question: QuestionVersion | None = None
        solution: SolutionVersion | None = None
        rubric: RubricVersion | None = None
        feedback: dict[str, list[str]] = {"writer": [], "solver": [], "rubric": []}
        retry_target = ArbitrationAction.RETRY_ALL
        retry_counts = {"problem": 0, "solution": 0, "rubric": 0}
        for attempt in range(1, self.max_total_question_rounds + 1):
            rewrite_question = retry_target in {
                ArbitrationAction.RETRY_PROBLEM,
                ArbitrationAction.RETRY_ALL,
            }
            rewrite_solution = rewrite_question or retry_target is ArbitrationAction.RETRY_SOLUTION
            rewrite_rubric = rewrite_solution or retry_target is ArbitrationAction.RETRY_RUBRIC
            if rewrite_question:
                previous_question = question
                for validation_attempt in range(self.max_draft_validation_attempts):
                    question_draft = await self.models.standard.complete(
                        role="question_writer",
                        system_prompt=(
                            "Write one original, self-contained, solvable exam question by "
                            "executing the supplied question plan exactly. Do not provide its "
                            "answer. Return the statement and each option as ordered content "
                            "blocks: text for prose, inline_math for short formulas, and "
                            "display_math for standalone formulas. Do not place LaTeX inside text "
                            "blocks. Math block content must be a bare expression without dollar "
                            "signs, \\( ... \\), or \\[ ... \\] delimiters, and must use ASCII "
                            "punctuation. Geometry notation such as angle names, perpendicular or "
                            "parallel relations, coordinates, equations, and degree measures must "
                            "be complete inline_math blocks; never place Unicode ∠, ⊥, or ∥ in "
                            "text blocks. Do not emit Markdown tables in text blocks; express "
                            "small data tables as clear prose. Multiple-choice questions require "
                            "at least four options; do not prefix option content with A, B, C, or "
                            "D because the renderer adds labels. "
                            "other types require none. For a constructed-response question, "
                            "statement contains only the shared stem and every requested "
                            "subproblem must appear in parts with an explicit score; part scores "
                            "must sum exactly to the question score. answer_format describes only "
                            "the response format and must never contain hidden subproblems."
                        ),
                        user_prompt=_json_prompt(
                            profile=profile.model_dump(mode="json"),
                            blueprint_id=blueprint.id,
                            question_plan=plan.model_dump(mode="json"),
                            source_context=source_context,
                            revision_feedback=feedback["writer"],
                        ),
                        response_model=QuestionDraft,
                        prompt_version="question-writer-v1",
                        run_id=str(run_id),
                    )
                    try:
                        question = QuestionVersion(
                            question_id=question_id,
                            version=(previous_question.version + 1) if previous_question else 1,
                            parent_version_id=(previous_question.id if previous_question else None),
                            number=plan.number,
                            section_id=plan.section_id,
                            section_title=plan.section_title,
                            question_type=plan.question_type,
                            score=plan.score,
                            metadata=GenerationMetadata(
                                role="question_writer",
                                model="routed",
                                prompt_version="question-writer-v1",
                                plan_id=plan.id,
                            ),
                            **question_draft.model_dump(),
                        )
                        break
                    except ValidationError as exc:
                        if validation_attempt == self.max_draft_validation_attempts - 1:
                            raise
                        feedback["writer"] = [_domain_validation_feedback("question", exc)]
            assert question is not None
            if rewrite_solution:
                previous_solution = solution
                for validation_attempt in range(self.max_draft_validation_attempts):
                    solution_draft = await self.models.strong.complete(
                        role="independent_solver",
                        system_prompt=(
                            "Solve the supplied question independently. Check every step and do "
                            "not assume an intended answer. Return a rigorous solution. Return "
                            "final_answer as ordered text, inline_math, or display_math content "
                            "blocks; do not place LaTeX inside text blocks. Math block content is "
                            "a "
                            "bare expression without dollar signs or math delimiters and uses "
                            "ASCII punctuation. Each step description "
                            "and conclusion also uses content blocks, while expression contains "
                            "only pure mathematical LaTeX. Keep final_answer concise and do not "
                            "repeat the full derivation already present in steps."
                        ),
                        user_prompt=_json_prompt(
                            question=question.model_dump(mode="json"),
                            question_plan=plan.model_dump(mode="json"),
                            source_context=source_context,
                            revision_feedback=feedback["solver"],
                        ),
                        response_model=SolutionDraft,
                        prompt_version="independent-solver-v1",
                        run_id=str(run_id),
                    )
                    try:
                        solution = SolutionVersion(
                            solution_id=solution_id,
                            question_version_id=question.id,
                            version=(previous_solution.version + 1) if previous_solution else 1,
                            parent_version_id=(previous_solution.id if previous_solution else None),
                            metadata=GenerationMetadata(
                                role="independent_solver",
                                model="routed",
                                prompt_version="independent-solver-v1",
                                plan_id=plan.id,
                            ),
                            **solution_draft.model_dump(),
                        )
                        break
                    except ValidationError as exc:
                        if validation_attempt == self.max_draft_validation_attempts - 1:
                            raise
                        feedback["solver"] = [_domain_validation_feedback("solution", exc)]
            assert solution is not None
            if rewrite_rubric:
                previous_rubric = rubric
                for validation_attempt in range(self.max_draft_validation_attempts):
                    rubric_draft = await self.models.standard.complete(
                        role="rubric_builder",
                        system_prompt=(
                            "Build a non-overlapping analytic rubric from the question and "
                            "independent solution. Rubric item scores must sum exactly to the "
                            "question score. Return each rubric description as ordered text, "
                            "inline_math, or display_math content blocks, with no LaTeX embedded "
                            "in text blocks. Math block content is a bare expression without "
                            "dollar "
                            "signs or math delimiters and uses ASCII punctuation."
                        ),
                        user_prompt=_json_prompt(
                            question=question.model_dump(mode="json"),
                            solution=solution.model_dump(mode="json"),
                            question_plan=plan.model_dump(mode="json"),
                            score=plan.score,
                            revision_feedback=feedback["rubric"],
                        ),
                        response_model=RubricDraft,
                        prompt_version="rubric-builder-v1",
                        run_id=str(run_id),
                    )
                    try:
                        rubric = RubricVersion(
                            rubric_id=rubric_id,
                            question_version_id=question.id,
                            solution_version_id=solution.id,
                            version=(previous_rubric.version + 1) if previous_rubric else 1,
                            parent_version_id=(previous_rubric.id if previous_rubric else None),
                            max_score=plan.score,
                            metadata=GenerationMetadata(
                                role="rubric_builder",
                                model="routed",
                                prompt_version="rubric-builder-v1",
                                plan_id=plan.id,
                            ),
                            **rubric_draft.model_dump(),
                        )
                        break
                    except ValidationError as exc:
                        if validation_attempt == self.max_draft_validation_attempts - 1:
                            raise
                        feedback["rubric"] = [_domain_validation_feedback("rubric", exc)]
            assert rubric is not None
            bundle = ExamQuestionBundle(question=question, solution=solution, rubric=rubric)
            reports = await self._review(run_id, profile, plan, bundle)
            self.artifacts.write_json(
                run_id,
                f"questions/{plan.number:02d}/reviews-pre-arbitration.json",
                [report.model_dump(mode="json") for report in reports],
                created_by_phase=f"QUESTION_ATTEMPT_{attempt}",
            )
            decision = await self._arbitrate(run_id, plan, bundle, reports)
            self._persist_attempt(run_id, plan.number, attempt, bundle, reports, decision)
            if decision.action in {ArbitrationAction.PASS, ArbitrationAction.PASS_WITH_WARNINGS}:
                return QuestionGenerationOutcome(
                    bundle=bundle,
                    requires_human_review=(decision.action is ArbitrationAction.PASS_WITH_WARNINGS),
                )
            if decision.action is ArbitrationAction.ESCALATE_HUMAN:
                return QuestionGenerationOutcome(bundle=bundle, requires_human_review=True)
            if decision.action is ArbitrationAction.ABORT:
                raise RuntimeError(f"question {plan.number} arbitration: {decision.action}")
            feedback = {
                "writer": decision.writer_feedback,
                "solver": decision.solver_feedback,
                "rubric": decision.rubric_feedback,
            }
            retry_target = decision.action
            if retry_target in {
                ArbitrationAction.RETRY_PROBLEM,
                ArbitrationAction.RETRY_ALL,
            }:
                retry_bucket = "problem"
            elif retry_target is ArbitrationAction.RETRY_SOLUTION:
                retry_bucket = "solution"
            else:
                retry_bucket = "rubric"
            retry_counts[retry_bucket] += 1
            if retry_counts[retry_bucket] >= self.max_question_attempts:
                raise RuntimeError(f"question {plan.number} exhausted {retry_bucket} retry budget")
        raise RuntimeError(f"question {plan.number} exhausted total review round budget")

    async def _review(
        self,
        run_id: UUID,
        profile: SubjectProfile,
        plan: QuestionPlan,
        bundle: ExamQuestionBundle,
    ) -> list[ReviewReport]:
        async def review(name: str) -> ReviewReport:
            if name == "structure":
                return _structure_review(bundle)
            return await self.models.standard.complete(
                role=f"{name}_reviewer",
                system_prompt=(
                    f"Act as the {name} reviewer. Independently inspect the question, "
                    "solution, rubric, and adherence to the supplied question plan. Mark passed "
                    "false for any error or fatal finding. Use info or warning for style, "
                    "difficulty calibration, distractor quality, or non-blocking plan alignment "
                    "when the bundle remains mathematically valid and scorable. Use error only "
                    "for an objective defect that prevents reliable use, and fatal only for a "
                    "wrong or missing answer, an invalid question, or a broken scoring contract."
                ),
                user_prompt=_json_prompt(
                    question_plan=plan.model_dump(mode="json"),
                    bundle=bundle.model_dump(mode="json"),
                ),
                response_model=ReviewReport,
                prompt_version="question-review-v1",
                run_id=str(run_id),
            )

        return list(await asyncio.gather(*(review(name) for name in profile.reviewers)))

    async def _arbitrate(
        self,
        run_id: UUID,
        plan: QuestionPlan,
        bundle: ExamQuestionBundle,
        reports: list[ReviewReport],
    ) -> ArbitrationDecision:
        decision = await self.models.strong.complete(
            role="question_arbiter",
            system_prompt=(
                "Arbitrate independent review reports. Retry the earliest invalid dependency: "
                "a question problem invalidates solution and rubric; a solution problem "
                "invalidates rubric. Never pass a fatal finding. You may reject an error finding "
                "as a reviewer severity mistake only when the bundle is objectively correct, "
                "internally consistent, usable, and scorable; explain that judgment explicitly."
            ),
            user_prompt=_json_prompt(
                question_plan=plan.model_dump(mode="json"),
                bundle=bundle.model_dump(mode="json"),
                reports=[report.model_dump(mode="json") for report in reports],
            ),
            response_model=ArbitrationDecision,
            prompt_version="question-arbiter-v1",
            run_id=str(run_id),
        )
        fatal_findings = [
            finding
            for report in reports
            for finding in report.findings
            if finding.severity is FindingSeverity.FATAL
        ]
        if fatal_findings and decision.action in {
            ArbitrationAction.PASS,
            ArbitrationAction.PASS_WITH_WARNINGS,
        }:
            if any(
                finding.target in {FindingTarget.QUESTION, FindingTarget.BUNDLE}
                for finding in fatal_findings
            ):
                action = ArbitrationAction.RETRY_PROBLEM
            elif any(finding.target is FindingTarget.SOLUTION for finding in fatal_findings):
                action = ArbitrationAction.RETRY_SOLUTION
            else:
                action = ArbitrationAction.RETRY_RUBRIC
            return ArbitrationDecision(
                action=action,
                rationale=(
                    "Deterministic review gate overrode an invalid PASS decision because "
                    "fatal findings remained unresolved."
                ),
                finding_codes=[finding.code for finding in fatal_findings],
                writer_feedback=[
                    finding.message
                    for finding in fatal_findings
                    if finding.target in {FindingTarget.QUESTION, FindingTarget.BUNDLE}
                ],
                solver_feedback=[
                    finding.message
                    for finding in fatal_findings
                    if finding.target is FindingTarget.SOLUTION
                ],
                rubric_feedback=[
                    finding.message
                    for finding in fatal_findings
                    if finding.target is FindingTarget.RUBRIC
                ],
            )
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


def _validate_subject_profile(profile: SubjectProfile) -> None:
    unknown = set(profile.reviewers) - SUPPORTED_REVIEWERS
    if unknown:
        raise ValueError(f"subject profile proposed unknown reviewers: {sorted(unknown)}")


def _blueprint_slots(blueprint: ExamBlueprint) -> list[QuestionSlot]:
    slots: list[QuestionSlot] = []
    number = 1
    for section in blueprint.sections:
        for slot, score in enumerate(section.resolved_scores, start=1):
            slots.append(
                QuestionSlot(
                    section_id=section.id,
                    section_title=section.title,
                    slot=slot,
                    number=number,
                    question_type=section.question_type,
                    score=score,
                    topic_tags=section.topic_tags,
                )
            )
            number += 1
    return slots


def _materialize_question_plans(
    blueprint: ExamBlueprint, drafts: list[QuestionPlanDraft]
) -> list[QuestionPlan]:
    expected_slots = _blueprint_slots(blueprint)
    expected = {(slot.section_id, slot.slot): slot for slot in expected_slots}
    received: dict[tuple[str, int], QuestionPlanDraft] = {}
    for draft in drafts:
        key = (draft.section_id, draft.slot)
        if key in received:
            raise ValueError(f"question planner returned duplicate slot: {key}")
        received[key] = draft

    missing = sorted(set(expected) - set(received))
    unexpected = sorted(set(received) - set(expected))
    if missing or unexpected:
        raise ValueError(
            f"question planner slot mismatch: missing={missing}, unexpected={unexpected}"
        )

    plans: list[QuestionPlan] = []
    for slot in expected_slots:
        draft = received[(slot.section_id, slot.slot)]
        plans.append(
            QuestionPlan(
                id=f"{blueprint.id}:q{slot.number:02d}",
                number=slot.number,
                question_type=slot.question_type,
                score=slot.score,
                section_title=slot.section_title,
                **draft.model_dump(),
            )
        )
    return plans


def _validate_blueprint(profile: SubjectProfile, blueprint: ExamBlueprint) -> None:
    if blueprint.subject_profile != profile.id:
        raise ValueError("blueprint subject_profile does not match the subject profile id")
    unsupported = {section.question_type for section in blueprint.sections} - set(
        profile.supported_question_types
    )
    if unsupported:
        names = sorted(item.value for item in unsupported)
        raise ValueError(f"blueprint uses unsupported question types: {names}")


def _domain_validation_feedback(stage: str, error: ValidationError) -> str:
    details = json.dumps(error.errors(include_url=False), ensure_ascii=False, default=str)
    return (
        f"The {stage} draft failed deterministic domain validation. Correct the draft and "
        f"return a complete replacement. Validation errors: {details}"
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
