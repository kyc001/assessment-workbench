import asyncio
from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

from assessment_workbench.capabilities import (
    CapabilityCatalog,
    SubjectCapability,
    load_default_capability_catalog,
)
from assessment_workbench.compilers import LatexCompiler
from assessment_workbench.domain import (
    ArtifactRef,
    BlueprintDraft,
    ExamBlueprint,
    ExamDocument,
    ExamGenerationRequest,
    ExamPlanningMode,
    ExamPlanningRecord,
    ExamQuestionBundle,
    QuestionGenerationRequest,
    QuestionPlan,
    QuestionPlanDraft,
    QuestionPlanSetDraft,
    QuestionSlot,
    RunStatus,
    SubjectProfile,
    SubjectProfileCandidate,
    WorkflowCheckpoint,
    WorkflowRun,
)
from assessment_workbench.latex_service import ExamLatexService
from assessment_workbench.prompting import complete_with_prompt, json_prompt
from assessment_workbench.question_workflow import ModelRouter, QuestionAgentWorkflow
from assessment_workbench.storage import ArtifactStore, RunStore
from assessment_workbench.workflow import Step, WorkflowEngine

__all__ = ["ExamAgentWorkflow", "ModelRouter"]


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
        max_reviewer_attempts: int = 2,
        max_parallel_questions: int = 1,
        compiler: LatexCompiler | None = None,
        capability_catalog: CapabilityCatalog | None = None,
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
        self.capabilities = capability_catalog or load_default_capability_catalog()
        self.prompts = self.capabilities.prompts
        self.question_workflow = QuestionAgentWorkflow(
            models,
            artifacts,
            runs,
            self.capabilities,
            max_question_attempts=max_question_attempts,
            max_total_question_rounds=max_total_question_rounds,
            max_draft_validation_attempts=max_draft_validation_attempts,
            max_reviewer_attempts=max_reviewer_attempts,
        )

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
        capability: SubjectCapability | None = None
        if subject_profile is None and blueprint is None:
            capability = self.capabilities.subjects.resolve(subject)
            if capability is not None:
                subject_profile = capability.profile
                blueprint = capability.blueprint
                if blueprint is None:
                    raise ValueError(f"subject capability has no locked blueprint: {capability.id}")
        request = ExamGenerationRequest(
            subject=subject,
            target_level=target_level,
            requirements=requirements,
            source_context=source_context,
            subject_profile=subject_profile,
            blueprint=blueprint,
            capability_id=capability.id if capability is not None else None,
            capability_version=capability.version if capability is not None else None,
            capability_context=capability.prompt_context if capability is not None else {},
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
        capability: SubjectCapability | None = None
        if request.capability_id is not None:
            assert request.capability_version is not None
            assert provided_profile is not None
            assert provided_blueprint is not None
            capability = self.capabilities.require_subject_binding(
                request.capability_id,
                request.capability_version,
                provided_profile,
                provided_blueprint,
                request.capability_context,
            )
        if capability is not None:
            planning_mode = ExamPlanningMode.CAPABILITY
        elif provided_profile is not None:
            planning_mode = ExamPlanningMode.PRESET
        else:
            planning_mode = ExamPlanningMode.AGENT
        capability_validators = capability.validators if capability is not None else []
        if provided_profile is not None and provided_blueprint is not None:
            self.capabilities.validate_profile(provided_profile, capability_validators)
            self.capabilities.validate_blueprint(
                provided_profile,
                provided_blueprint,
                capability_validators,
            )
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
                prompt = self.prompts.require("subject_researcher")
                validation_feedback: list[str] = []
                for validation_attempt in range(self.max_draft_validation_attempts):
                    candidate = await complete_with_prompt(
                        self.models.strong,
                        prompt=prompt,
                        user_prompt=json_prompt(
                            subject=subject,
                            target_level=target_level,
                            requirements=requirements,
                            source_context=source_context,
                            registered_reviewers=self.capabilities.reviewers.names(),
                            registered_tools=self.capabilities.tools.names(),
                            revision_feedback=validation_feedback,
                        ),
                        response_model=SubjectProfileCandidate,
                        run_id=state["run_id"],
                    )
                    profile = SubjectProfile(
                        id=candidate.subject_id,
                        display_name=candidate.display_name,
                        supported_question_types=candidate.supported_question_types,
                        reviewers=candidate.reviewers,
                        tools=candidate.tools,
                        latex_template="generic-v1",
                        difficulty_dimensions=candidate.difficulty_dimensions,
                        conventions=candidate.conventions,
                        source_summary=candidate.source_summary,
                    )
                    try:
                        self.capabilities.validate_profile(profile)
                        break
                    except ValueError as exc:
                        if validation_attempt == self.max_draft_validation_attempts - 1:
                            raise
                        validation_feedback = [
                            "The previous profile referenced unavailable capabilities. "
                            f"Choose only registered names. Error: {exc}"
                        ]
                else:
                    raise RuntimeError("subject profile validation loop exited unexpectedly")
            else:
                profile = provided_profile
            self.capabilities.validate_profile(profile, capability_validators)
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
                prompt = self.prompts.require("exam_blueprint_planner")
                validation_feedback: list[str] = []
                for validation_attempt in range(self.max_draft_validation_attempts):
                    draft = await complete_with_prompt(
                        self.models.strong,
                        prompt=prompt,
                        user_prompt=json_prompt(
                            subject_profile=profile.model_dump(mode="json"),
                            target_level=target_level,
                            requirements=requirements,
                            source_context=source_context,
                            capability_context=request.capability_context,
                            revision_feedback=validation_feedback,
                        ),
                        response_model=BlueprintDraft,
                        run_id=state["run_id"],
                    )
                    exam_blueprint = ExamBlueprint(
                        id=f"{profile.id}-{uuid4().hex[:12]}",
                        subject_profile=profile.id,
                        **draft.model_dump(),
                    )
                    try:
                        self.capabilities.validate_blueprint(profile, exam_blueprint)
                        break
                    except ValueError as exc:
                        if validation_attempt == self.max_draft_validation_attempts - 1:
                            raise
                        validation_feedback = [
                            "The previous blueprint failed registered capability validation. "
                            f"Return a complete corrected blueprint. Error: {exc}"
                        ]
                else:
                    raise RuntimeError("exam blueprint validation loop exited unexpectedly")
            else:
                exam_blueprint = provided_blueprint
            self.capabilities.validate_blueprint(
                profile,
                exam_blueprint,
                capability_validators,
            )
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
                capability_id=request.capability_id,
                capability_version=request.capability_version,
            )
            planning_artifact = self.artifacts.write_json(
                state["run_id"],
                "exam-planning.json",
                planning.model_dump(mode="json", exclude_none=True),
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
            if (
                not request.require_blueprint_approval
                or planning_mode is not ExamPlanningMode.AGENT
            ):
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
                prompt = self.prompts.require("question_set_planner")
                draft = await complete_with_prompt(
                    self.models.strong,
                    prompt=prompt,
                    user_prompt=json_prompt(
                        subject_profile=profile.model_dump(mode="json"),
                        blueprint=exam_blueprint.model_dump(mode="json"),
                        slots=[slot.model_dump(mode="json") for slot in slots],
                        requirements=requirements,
                        source_context=source_context,
                        revision_feedback=planning_feedback,
                        capability_context=request.capability_context,
                    ),
                    response_model=QuestionPlanSetDraft,
                    run_id=state["run_id"],
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

                    def child_created(created: WorkflowRun) -> None:
                        records_by_number[plan.number] = {
                            **records_by_number[plan.number],
                            "run_id": str(created.id),
                            "status": RunStatus.RUNNING,
                        }
                        write_live_manifest()
                        self.artifacts.write_json(
                            state["run_id"],
                            "question-runs.json",
                            ordered_records(),
                            created_by_phase="QUESTION_CHILD_CREATED",
                        )

                    child_run, child_state = await self.generate_question_run(
                        profile=profile,
                        blueprint=blueprint,
                        plan=plan,
                        source_context=source_context,
                        parent_run_id=state["run_id"],
                        capability_id=request.capability_id,
                        capability_version=request.capability_version,
                        capability_context=request.capability_context,
                        on_run_created=child_created,
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
        capability_id: str | None = None,
        capability_version: str | None = None,
        capability_context: dict[str, list[str]] | None = None,
        on_run_created: Callable[[WorkflowRun], None] | None = None,
    ) -> tuple[WorkflowRun, dict[str, Any]]:
        request = QuestionGenerationRequest(
            profile=profile,
            blueprint=blueprint,
            plan=plan,
            source_context=source_context,
            parent_run_id=parent_run_id,
            capability_id=capability_id,
            capability_version=capability_version,
            capability_context=capability_context or {},
        )
        return await self.question_workflow.execute(
            request,
            on_run_created=on_run_created,
        )

    async def resume_question_run(self, run_id: UUID) -> tuple[WorkflowRun, dict[str, Any]]:
        return await self.question_workflow.resume(run_id)


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
