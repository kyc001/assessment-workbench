import asyncio
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from assessment_workbench.capabilities import (
    CapabilityCatalog,
    SubjectCapability,
    load_default_capability_catalog,
)
from assessment_workbench.compilers import LatexCompiler
from assessment_workbench.document_workflow import (
    DOCUMENTS_BUILDING,
    DocumentBatchWorkflow,
    DocumentBuildWorkflow,
    document_artifact_ids,
    latest_document_builds,
    parse_document_build_records,
    successful_document_builds,
)
from assessment_workbench.domain import (
    ArtifactRef,
    ExamArbitrationDecision,
    ExamBlueprint,
    ExamDocument,
    ExamGenerationRequest,
    ExamPlanningMode,
    ExamPlanningRecord,
    ExamQuestionBundle,
    ExamReviewReport,
    ExamWorkflowState,
    QuestionGenerationRequest,
    QuestionPlan,
    QuestionPlanDraft,
    QuestionPlanningProgress,
    QuestionPlanSetDraft,
    QuestionSlot,
    RunStatus,
    SubjectProfile,
    SubjectProfileCandidate,
    SubjectResearchReport,
    SubjectResearchRunRecord,
    SubjectResearchSynthesis,
    WorkflowCheckpoint,
    WorkflowRun,
)
from assessment_workbench.errors import RetryableWorkflowError
from assessment_workbench.exam_quality import (
    allocate_question_slot_coverage,
    resolve_exam_targets,
    validate_bundle_for_plan,
    validate_question_plan_coverage,
    validate_question_plan_timing,
)
from assessment_workbench.exam_review_workflow import parse_exam_review_records
from assessment_workbench.exam_workflow import ExamQualityWorkflow
from assessment_workbench.pdf_inspection import PdfInspector
from assessment_workbench.prompting import (
    complete_with_prompt,
    context_artifact_ids,
    json_prompt,
)
from assessment_workbench.question_workflow import ModelRouter, QuestionAgentWorkflow
from assessment_workbench.release import DOCUMENT_APPROVAL, RELEASE_BUNDLING, ReleaseBundleBuilder
from assessment_workbench.storage import ArtifactStore, RunStore
from assessment_workbench.subject_research_workflow import (
    SubjectResearchPoolWorkflow,
    parse_subject_research_records,
)
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
        max_exam_reviewer_attempts: int = 3,
        max_exam_review_rounds: int = 3,
        max_parallel_questions: int = 1,
        compiler: LatexCompiler | None = None,
        pdf_inspector: PdfInspector | None = None,
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
        self.pdf_inspector = pdf_inspector
        self.capabilities = capability_catalog or load_default_capability_catalog()
        self.prompts = self.capabilities.prompts
        self.subject_research = SubjectResearchPoolWorkflow(
            models.strong,
            artifacts,
            runs,
            self.capabilities,
            max_attempts=max_draft_validation_attempts,
        )
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
        self.exam_workflow = ExamQualityWorkflow(
            models.standard,
            models.strong,
            artifacts,
            runs,
            self.capabilities,
            max_reviewer_attempts=max_exam_reviewer_attempts,
            max_review_rounds=max_exam_review_rounds,
            max_draft_validation_attempts=max_draft_validation_attempts,
        )
        self.document_batch = (
            DocumentBatchWorkflow(
                DocumentBuildWorkflow(compiler, pdf_inspector, artifacts, runs),
                artifacts,
                runs,
            )
            if compiler is not None and pdf_inspector is not None
            else None
        )
        self.release_builder = ReleaseBundleBuilder(artifacts, runs)

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
            if provided_profile is not None:
                self.capabilities.validate_profile(provided_profile, capability_validators)
                artifact = self.artifacts.write_json(
                    state["run_id"],
                    "subject-profile.json",
                    provided_profile.model_dump(mode="json"),
                    created_by_phase="SUBJECT_RESEARCHING",
                )
                return {
                    "profile": provided_profile,
                    "output_artifact_ids": [request_artifact.id, artifact.id],
                    "_checkpoint_artifacts": {
                        "request": request_artifact.id,
                        "profile": artifact.id,
                    },
                }
            restored_records = state.get("subject_research_records")
            outcome = await self.subject_research.execute(
                state["run_id"],
                subject=subject,
                target_level=target_level,
                requirements=requirements,
                source_context=source_context,
                restored_records=restored_records,
                input_artifact_ids=[request_artifact.id, *context_artifact_ids(state)],
            )
            reports_artifact = self.artifacts.write_json(
                state["run_id"],
                "subject-research-reports.json",
                [report.model_dump(mode="json") for report in outcome.reports.values()],
                created_by_phase="SUBJECT_RESEARCHING",
            )
            if len(outcome.reports) < self.subject_research.quorum:
                message = (
                    "subject research quorum was not reached: "
                    f"{len(outcome.reports)}/{self.subject_research.quorum}"
                )
                if any(record.status is RunStatus.INTERRUPTED for record in outcome.records):
                    raise RetryableWorkflowError(message)
                raise RuntimeError(message)
            return {
                "subject_research_reports": list(outcome.reports.values()),
                "subject_research_records": outcome.records,
                "subject_research_manifest_artifact": outcome.manifest_artifact,
                "output_artifact_ids": [
                    request_artifact.id,
                    outcome.manifest_artifact.id,
                    reports_artifact.id,
                ],
                "_checkpoint_artifacts": {
                    "request": request_artifact.id,
                    "subject_research_manifest": outcome.manifest_artifact.id,
                    "subject_research_reports": reports_artifact.id,
                },
                "_checkpoint_child_run_ids": outcome.child_run_ids,
            }

        async def synthesize_subject(state: dict[str, Any]) -> dict[str, Any]:
            if provided_profile is not None:
                return {}
            reports = state.get("subject_research_reports")
            records = state.get("subject_research_records")
            if not isinstance(reports, list) or not all(
                isinstance(report, SubjectResearchReport) for report in reports
            ):
                raise ValueError("subject synthesis requires valid research reports")
            if not isinstance(records, list) or not all(
                isinstance(record, SubjectResearchRunRecord) for record in records
            ):
                raise ValueError("subject synthesis requires research run records")
            successful_ids = [
                record.run_id for record in records if record.status is RunStatus.SUCCEEDED
            ]
            failed_ids = [
                record.run_id for record in records if record.status is not RunStatus.SUCCEEDED
            ]
            prompt = self.prompts.require("subject_research_synthesizer")
            validation_feedback: list[str] = []
            for attempt in range(self.max_draft_validation_attempts):
                synthesis = await complete_with_prompt(
                    self.models.strong,
                    prompt=prompt,
                    user_prompt=json_prompt(
                        subject=subject,
                        target_level=target_level,
                        requirements=requirements,
                        source_context=source_context,
                        research_reports=[report.model_dump(mode="json") for report in reports],
                        registered_reviewers=self.capabilities.reviewers.names(),
                        registered_tools=self.capabilities.tools.names(),
                        revision_feedback=validation_feedback,
                    ),
                    response_model=SubjectResearchSynthesis,
                    run_id=state["run_id"],
                    artifacts=self.artifacts,
                    created_by_phase="SUBJECT_SYNTHESIZING",
                    input_artifact_ids=context_artifact_ids(state),
                )
                synthesis = synthesis.model_copy(
                    update={
                        "successful_research_run_ids": successful_ids,
                        "failed_research_run_ids": failed_ids,
                    }
                )
                try:
                    _validate_subject_research_synthesis(synthesis, reports)
                    profile = _materialize_subject_profile(synthesis.profile)
                    blueprint = ExamBlueprint(
                        id=f"{profile.id}-{uuid4().hex[:12]}",
                        subject_profile=profile.id,
                        **synthesis.blueprint.model_dump(),
                    )
                    self.capabilities.validate_profile(profile)
                    self.capabilities.validate_blueprint(profile, blueprint)
                    _blueprint_slots(blueprint)
                    break
                except ValueError as exc:
                    if attempt == self.max_draft_validation_attempts - 1:
                        raise
                    validation_feedback = [
                        "The previous synthesis was not traceable or executable. "
                        f"Return a corrected synthesis. Error: {exc}"
                    ]
            else:
                raise RuntimeError("subject synthesis retry loop exited unexpectedly")
            synthesis_artifact = self.artifacts.write_json(
                state["run_id"],
                "subject-research-synthesis.json",
                synthesis.model_dump(mode="json"),
                created_by_phase="SUBJECT_SYNTHESIZING",
            )
            profile_artifact = self.artifacts.write_json(
                state["run_id"],
                "subject-profile.json",
                profile.model_dump(mode="json"),
                created_by_phase="SUBJECT_SYNTHESIZING",
            )
            research_blueprint_artifact = self.artifacts.write_json(
                state["run_id"],
                "subject-research-blueprint.json",
                blueprint.model_dump(mode="json"),
                created_by_phase="SUBJECT_SYNTHESIZING",
            )
            return {
                "profile": profile,
                "blueprint": blueprint,
                "subject_research_synthesis": synthesis,
                "subject_research_synthesis_artifact": synthesis_artifact,
                "output_artifact_ids": [
                    synthesis_artifact.id,
                    profile_artifact.id,
                    research_blueprint_artifact.id,
                ],
                "_checkpoint_artifacts": {
                    "subject_research_synthesis": synthesis_artifact.id,
                    "profile": profile_artifact.id,
                    "research_blueprint": research_blueprint_artifact.id,
                },
            }

        async def plan_exam(state: dict[str, Any]) -> dict[str, Any]:
            profile: SubjectProfile = state["profile"]
            if provided_blueprint is None:
                exam_blueprint = state["blueprint"]
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
                research_synthesis_artifact_id=(
                    state["subject_research_synthesis_artifact"].id
                    if isinstance(state.get("subject_research_synthesis_artifact"), ArtifactRef)
                    else None
                ),
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
                    "artifact_ids": [
                        bindings[key]
                        for key in (
                            "subject_research_manifest",
                            "subject_research_reports",
                            "subject_research_synthesis",
                            "profile",
                            "blueprint",
                            "planning",
                        )
                        if key in bindings
                    ],
                    "retry_phase": "EXAM_PLANNING",
                }
            }

        async def plan_questions(state: dict[str, Any]) -> dict[str, Any]:
            profile: SubjectProfile = state["profile"]
            exam_blueprint: ExamBlueprint = state["blueprint"]
            slots = _blueprint_slots(exam_blueprint)
            planning_feedback: list[str] = []
            draft_artifact_ids: list[UUID] = []
            validation_artifact_ids: list[UUID] = []
            next_attempt = 1
            progress_artifact = self.artifacts.latest(
                state["run_id"], "question-planning-progress.json"
            )
            if progress_artifact is not None:
                progress = QuestionPlanningProgress.model_validate(
                    self.artifacts.read_json(progress_artifact.id)
                )
                if (
                    progress.blueprint_id == exam_blueprint.id
                    and progress.blueprint_version == exam_blueprint.version
                ):
                    next_attempt = progress.next_attempt
                    planning_feedback = progress.validation_feedback
                    draft_artifact_ids = progress.draft_artifact_ids
                    validation_artifact_ids = progress.validation_artifact_ids
                else:
                    progress_artifact = None
            for planning_attempt in range(next_attempt, 4):
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
                    artifacts=self.artifacts,
                    created_by_phase="QUESTION_PLANNING",
                    input_artifact_ids=[
                        *context_artifact_ids(state),
                        *draft_artifact_ids,
                        *validation_artifact_ids,
                        *([progress_artifact.id] if progress_artifact is not None else []),
                    ],
                )
                draft_artifact = self.artifacts.write_json(
                    state["run_id"],
                    "question-plan-draft.json",
                    draft.model_dump(mode="json"),
                    created_by_phase="QUESTION_PLANNING",
                )
                draft_artifact_ids.append(draft_artifact.id)
                try:
                    question_plans = _materialize_question_plans(exam_blueprint, draft.plans)
                    break
                except ValueError as exc:
                    planning_feedback = [
                        "The previous plan set failed deterministic slot validation. Return "
                        f"exactly the supplied slots and no others. Error: {exc}"
                    ]
                    validation_artifact = self.artifacts.write_json(
                        state["run_id"],
                        "question-plan-validation.json",
                        {
                            "blueprint_id": exam_blueprint.id,
                            "blueprint_version": exam_blueprint.version,
                            "attempt": planning_attempt,
                            "draft_artifact_id": str(draft_artifact.id),
                            "error": str(exc),
                        },
                        created_by_phase="QUESTION_PLANNING",
                    )
                    validation_artifact_ids.append(validation_artifact.id)
                    progress = QuestionPlanningProgress(
                        blueprint_id=exam_blueprint.id,
                        blueprint_version=exam_blueprint.version,
                        next_attempt=planning_attempt + 1,
                        validation_feedback=planning_feedback,
                        draft_artifact_ids=draft_artifact_ids,
                        validation_artifact_ids=validation_artifact_ids,
                    )
                    progress_artifact = self.artifacts.write_json(
                        state["run_id"],
                        "question-planning-progress.json",
                        progress.model_dump(mode="json"),
                        created_by_phase="QUESTION_PLANNING",
                    )
                    state["output_artifact_ids"] = [
                        *draft_artifact_ids,
                        *validation_artifact_ids,
                        progress_artifact.id,
                    ]
                    if planning_attempt == 3:
                        raise
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
                "output_artifact_ids": [artifact.id, *draft_artifact_ids],
                "_checkpoint_artifacts": {"question_plans": artifact.id},
            }

        async def revise_question_plans(state: dict[str, Any]) -> dict[str, Any]:
            return await self.exam_workflow.revise_plans(
                state["run_id"],
                profile=state["profile"],
                blueprint=state["blueprint"],
                current=state["question_plans"],
                exam_state=state.get("exam_workflow_state"),
                reports=state.get("exam_reports"),
                capability_context=request.capability_context,
                input_artifact_ids=context_artifact_ids(state),
            )

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
                existing_records.extend(restored_records)
            try:
                editable_records = self.artifacts.read_editable_json(
                    state["run_id"], "question-runs.json"
                )
            except (FileNotFoundError, OSError, ValueError):
                pass
            else:
                if isinstance(editable_records, list):
                    existing_records.extend(editable_records)
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

            exam_state = state.get("exam_workflow_state")
            target_numbers = (
                set(exam_state.replacement_question_numbers)
                if isinstance(exam_state, ExamWorkflowState)
                else set()
            )
            exam_round = exam_state.round if isinstance(exam_state, ExamWorkflowState) else 0
            for number, record in list(records_by_number.items()):
                if (
                    number in target_numbers
                    or record.get("exam_round") != exam_round
                    or record.get("status") == RunStatus.SUCCEEDED
                ):
                    continue
                history = record.get("replacement_history", [])
                if not isinstance(history, list):
                    continue
                previous = next(
                    (
                        item
                        for item in reversed(history)
                        if isinstance(item, dict)
                        and item.get("status") in {RunStatus.SUCCEEDED, RunStatus.SUCCEEDED.value}
                        and item.get("bundle_artifact_id")
                    ),
                    None,
                )
                if previous is None:
                    continue
                bundle_artifact = self.artifacts.get(UUID(str(previous["bundle_artifact_id"])))
                if bundle_artifact is None:
                    continue
                records_by_number[number] = {
                    **record,
                    "run_id": previous.get("run_id"),
                    "status": RunStatus.SUCCEEDED,
                    "error": None,
                    "bundle_artifact_id": str(bundle_artifact.id),
                    "bundle_path": str(bundle_artifact.path),
                    "editable_path": str(
                        Path("editable") / str(state["run_id"]) / "questions" / f"{number:02d}.json"
                    ),
                    "exam_round": previous.get("exam_round", 0),
                }
            for number in target_numbers:
                if number not in records_by_number:
                    raise ValueError(f"replacement target has no question plan: {number}")
                record = records_by_number[number]
                if record.get("exam_round") == exam_round:
                    continue
                history = list(record.get("replacement_history", []))
                if record.get("run_id") is not None:
                    history.append(
                        {
                            "exam_round": record.get("exam_round", 0),
                            "run_id": record.get("run_id"),
                            "bundle_artifact_id": record.get("bundle_artifact_id"),
                            "status": record.get("status"),
                        }
                    )
                records_by_number[number] = {
                    **record,
                    "run_id": None,
                    "status": RunStatus.QUEUED,
                    "error": None,
                    "bundle_artifact_id": None,
                    "bundle_path": None,
                    "editable_path": None,
                    "requires_human_review": False,
                    "exam_round": exam_round,
                    "replacement_history": history,
                }

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
                        validate_bundle_for_plan(reused_bundle, plan)
                        self.capabilities.validate_bundle(
                            profile, reused_bundle, capability_validators
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
                    generation_feedback = (
                        _question_feedback_for_number(
                            exam_state.question_feedback,
                            plan.number,
                        )
                        if isinstance(exam_state, ExamWorkflowState)
                        and plan.number in target_numbers
                        else []
                    )
                    existing_run: WorkflowRun | None = None
                    existing_run_id = records_by_number[plan.number].get("run_id")
                    if existing_run_id is not None:
                        try:
                            existing_run = self.runs.get(UUID(str(existing_run_id)))
                        except ValueError:
                            existing_run = None
                    await update_record(
                        plan.number,
                        {
                            **records_by_number[plan.number],
                            "status": "running",
                            "error": None,
                        },
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

                    resume_existing = False
                    if existing_run is not None and existing_run.status is RunStatus.INTERRUPTED:
                        previous_request = self.artifacts.latest(
                            existing_run.id,
                            "question-request.json",
                        )
                        if previous_request is not None:
                            try:
                                parsed_request = QuestionGenerationRequest.model_validate(
                                    self.artifacts.read_json(previous_request.id)
                                )
                            except (KeyError, OSError, ValueError):
                                pass
                            else:
                                resume_existing = (
                                    parsed_request.plan.id == plan.id
                                    and parsed_request.generation_feedback == generation_feedback
                                )
                    if resume_existing:
                        assert existing_run is not None
                        child_run, child_state = await self.resume_question_run(existing_run.id)
                    else:
                        child_run, child_state = await self.generate_question_run(
                            profile=profile,
                            blueprint=blueprint,
                            plan=plan,
                            source_context=source_context,
                            parent_run_id=state["run_id"],
                            capability_id=request.capability_id,
                            capability_version=request.capability_version,
                            capability_context=request.capability_context,
                            generation_feedback=generation_feedback,
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
                            "exam_round": records_by_number[plan.number].get("exam_round", 0),
                            "replacement_history": records_by_number[plan.number].get(
                                "replacement_history", []
                            ),
                        },
                        immutable_snapshot=True,
                    )
                    return plan, child_run, child_state

            child_results = await asyncio.gather(*(generate(plan) for plan in pending_plans))
            bundles: list[ExamQuestionBundle] = [item[0] for item in reused_results.values()]
            interrupted_numbers: list[int] = []
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
                elif child_run.status is RunStatus.INTERRUPTED:
                    interrupted_numbers.append(plan.number)
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
            if interrupted_numbers:
                numbers = ", ".join(str(number) for number in interrupted_numbers)
                raise RetryableWorkflowError(f"question child runs interrupted: {numbers}")
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
                calculator_policy=blueprint.calculator_policy,
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

        async def generate_exam_reviews(state: dict[str, Any]) -> dict[str, Any]:
            return await self.exam_workflow.review(
                state["run_id"],
                profile=state["profile"],
                blueprint=state["blueprint"],
                plans=state["question_plans"],
                exam=state["exam"],
                capability_context=request.capability_context,
                current_state=state.get("exam_workflow_state"),
                restored_records=state.get("exam_review_records"),
                input_artifact_ids=context_artifact_ids(state),
            )

        async def arbitrate_exam(state: dict[str, Any]) -> dict[str, Any]:
            return await self.exam_workflow.arbitrate(
                state["run_id"],
                profile=state["profile"],
                blueprint=state["blueprint"],
                plans=state["question_plans"],
                exam=state["exam"],
                reports=state.get("exam_reports"),
                current_state=state.get("exam_workflow_state"),
                capability_context=request.capability_context,
                input_artifact_ids=context_artifact_ids(state),
            )

        async def finalize_exam(state: dict[str, Any]) -> dict[str, Any]:
            return self.exam_workflow.finalize(state["run_id"], state.get("exam_workflow_state"))

        async def approve_exam(state: dict[str, Any]) -> dict[str, Any]:
            question_runs = state.get("question_runs", [])
            escalated = any(
                isinstance(record, dict) and bool(record.get("requires_human_review"))
                for record in question_runs
            )
            exam_state = state.get("exam_workflow_state")
            escalated = escalated or (
                isinstance(exam_state, ExamWorkflowState) and exam_state.requires_human_review
            )
            if not request.require_exam_approval and not escalated:
                return {}
            bindings = state.get("_checkpoint_artifacts", {})
            prompt = "Approve the assembled exam before document generation."
            if escalated:
                prompt += " Question-level or whole-exam arbitration requires human review."
            return {
                "_human_review": {
                    "prompt": prompt,
                    "artifact_ids": [
                        bindings[key]
                        for key in (
                            "exam",
                            "question_runs",
                            "exam_reviews",
                            "exam_decision",
                            "exam_workflow_state",
                        )
                        if key in bindings
                    ],
                    "retry_phase": "EXAM_REVIEWS_GENERATING",
                }
            }

        async def build_documents(state: dict[str, Any]) -> dict[str, Any]:
            if self.document_batch is None:
                return {"document_builds": [], "document_build_records": []}
            exam: ExamDocument = state["exam"]
            restored_records = state.get("document_build_records")
            if restored_records is None:
                latest = self.artifacts.latest(state["run_id"], "document-build-runs.json")
                if latest is not None:
                    restored_records = self.artifacts.read_json(latest.id)
            outcome = await self.document_batch.execute(
                state["run_id"],
                exam,
                restored_records,
                input_artifact_ids=context_artifact_ids(state),
            )
            output_ids = [
                outcome.manifest_artifact.id,
                *(
                    artifact_id
                    for record in outcome.current
                    for artifact_id in document_artifact_ids(record)
                ),
            ]
            updates: dict[str, Any] = {
                "document_build_records": outcome.records,
                "document_builds": outcome.current,
                "document_manifest_artifact": outcome.manifest_artifact,
                "output_artifact_ids": output_ids,
                "_checkpoint_artifacts": {
                    "document_manifest": outcome.manifest_artifact.id,
                },
                "_checkpoint_child_run_ids": outcome.child_run_ids,
            }
            if not outcome.succeeded:
                failed = [
                    record.view.value
                    for record in outcome.current
                    if record.status is not RunStatus.SUCCEEDED
                ]
                updates["_human_review"] = {
                    "prompt": (
                        "Document views failed machine gates. Inspect the manifest and retry "
                        f"only the failed views: {', '.join(failed)}."
                    ),
                    "artifact_ids": output_ids,
                    "allowed_decisions": ["retry", "reject"],
                    "resume_phase": DOCUMENTS_BUILDING,
                    "retry_phase": DOCUMENTS_BUILDING,
                }
            return updates

        async def approve_documents(state: dict[str, Any]) -> dict[str, Any]:
            if self.document_batch is None:
                return {}
            if not request.require_exam_approval:
                return {}
            manifest = state.get("document_manifest_artifact")
            builds = state.get("document_builds", [])
            if not isinstance(manifest, ArtifactRef) or not successful_document_builds(builds):
                raise ValueError("document approval requires a successful build manifest")
            return {
                "_human_review": {
                    "prompt": (
                        "Review every rendered page for clipping, overlap, labels, Chinese text, "
                        "mathematical notation, question content, solutions, and rubric scoring."
                    ),
                    "artifact_ids": [
                        manifest.id,
                        *(
                            artifact_id
                            for record in builds
                            for artifact_id in document_artifact_ids(record)
                        ),
                    ],
                    "retry_phase": DOCUMENTS_BUILDING,
                }
            }

        async def release_documents(state: dict[str, Any]) -> dict[str, Any]:
            if self.document_batch is None:
                artifacts = [
                    artifact
                    for artifact in state.get("artifacts", [])
                    if isinstance(artifact, ArtifactRef)
                ]
                return {
                    "artifacts": artifacts,
                    "output_artifact_ids": [artifact.id for artifact in artifacts],
                }
            exam: ExamDocument = state["exam"]
            builds = state.get("document_builds", [])
            manifest = state.get("document_manifest_artifact")
            if not isinstance(manifest, ArtifactRef) or not successful_document_builds(builds):
                raise ValueError("release requires successful document builds")
            acceptance: ArtifactRef | None = None
            if request.require_exam_approval:
                acceptance = self.release_builder.write_acceptance(
                    state["run_id"],
                    manifest_artifact_id=manifest.id,
                    document_builds=builds,
                )
            bindings = state.get("_checkpoint_artifacts", {})
            exam_artifact_id = bindings.get("exam")
            if not isinstance(exam_artifact_id, UUID):
                raise ValueError("release requires the ExamDocument artifact binding")
            question_bundle_artifact_ids = _question_bundle_artifact_ids(
                state.get("question_runs"),
                expected=len(exam.questions),
            )
            bundle, bundle_artifact = self.release_builder.build(
                state["run_id"],
                exam,
                exam_artifact_id=exam_artifact_id,
                question_bundle_artifact_ids=question_bundle_artifact_ids,
                document_builds=builds,
                acceptance_artifact_id=acceptance.id if acceptance is not None else None,
            )
            artifact_ids = [
                exam_artifact_id,
                manifest.id,
                *(
                    artifact_id
                    for record in builds
                    for artifact_id in document_artifact_ids(record)
                ),
                acceptance.id if acceptance is not None else None,
                bundle_artifact.id,
            ]
            output_artifacts = [
                artifact
                for artifact_id in artifact_ids
                if artifact_id is not None
                for artifact in [self.artifacts.get(artifact_id)]
                if artifact is not None
            ]
            checkpoint_updates = {"release_bundle": bundle_artifact.id}
            if acceptance is not None:
                checkpoint_updates["document_acceptance"] = acceptance.id
            return {
                "release_bundle": bundle,
                "release_bundle_artifact": bundle_artifact,
                "artifacts": output_artifacts,
                "output_artifact_ids": [artifact.id for artifact in output_artifacts],
                "_checkpoint_artifacts": checkpoint_updates,
            }

        steps: list[tuple[str, Step]] = [
            ("SUBJECT_RESEARCHING", research_subject),
            ("SUBJECT_SYNTHESIZING", synthesize_subject),
            ("EXAM_PLANNING", plan_exam),
            ("BLUEPRINT_APPROVAL", approve_blueprint),
            ("QUESTION_PLANNING", plan_questions),
            ("QUESTION_PLANS_REVISING", revise_question_plans),
            ("QUESTIONS_GENERATING", generate_questions),
            ("EXAM_ASSEMBLING", assemble),
            ("EXAM_REVIEWS_GENERATING", generate_exam_reviews),
            ("EXAM_ARBITRATING", arbitrate_exam),
            ("EXAM_FINALIZING", finalize_exam),
            ("EXAM_APPROVAL", approve_exam),
            (DOCUMENTS_BUILDING, build_documents),
            (DOCUMENT_APPROVAL, approve_documents),
            (RELEASE_BUNDLING, release_documents),
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
        research_manifest = payload("subject_research_manifest")
        if research_manifest is not None:
            state["subject_research_records"] = parse_subject_research_records(research_manifest)
            manifest_id = checkpoint.artifact_bindings["subject_research_manifest"]
            manifest_artifact = self.artifacts.get(manifest_id)
            if manifest_artifact is None:
                raise ValueError(f"subject research manifest is missing: {manifest_id}")
            state["subject_research_manifest_artifact"] = manifest_artifact
        research_reports = payload("subject_research_reports")
        if research_reports is not None:
            if not isinstance(research_reports, list):
                raise ValueError("subject research reports artifact is not a list")
            state["subject_research_reports"] = [
                SubjectResearchReport.model_validate(item) for item in research_reports
            ]
        synthesis_payload = payload("subject_research_synthesis")
        if synthesis_payload is not None:
            state["subject_research_synthesis"] = SubjectResearchSynthesis.model_validate(
                synthesis_payload
            )
            synthesis_id = checkpoint.artifact_bindings["subject_research_synthesis"]
            synthesis_artifact = self.artifacts.get(synthesis_id)
            if synthesis_artifact is None:
                raise ValueError(f"subject research synthesis is missing: {synthesis_id}")
            state["subject_research_synthesis_artifact"] = synthesis_artifact
        research_blueprint_payload = payload("research_blueprint")
        if research_blueprint_payload is not None:
            state["blueprint"] = ExamBlueprint.model_validate(research_blueprint_payload)
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
        document_manifest = payload("document_manifest")
        if document_manifest is not None:
            records = parse_document_build_records(document_manifest)
            state["document_build_records"] = records
            state["document_builds"] = latest_document_builds(records)
            manifest_id = checkpoint.artifact_bindings["document_manifest"]
            manifest_artifact = self.artifacts.get(manifest_id)
            if manifest_artifact is None:
                raise ValueError(f"document manifest metadata is missing: {manifest_id}")
            state["document_manifest_artifact"] = manifest_artifact
        exam_state_payload = payload("exam_workflow_state")
        if exam_state_payload is not None:
            state["exam_workflow_state"] = ExamWorkflowState.model_validate(exam_state_payload)
        exam_review_manifest = payload("exam_review_manifest")
        if exam_review_manifest is not None:
            state["exam_review_records"] = parse_exam_review_records(exam_review_manifest)
        exam_reviews_payload = payload("exam_reviews")
        if exam_reviews_payload is not None:
            if not isinstance(exam_reviews_payload, list):
                raise ValueError("exam reviews artifact is not a list")
            state["exam_reports"] = [
                ExamReviewReport.model_validate(item) for item in exam_reviews_payload
            ]
        exam_decision_payload = payload("exam_decision")
        if exam_decision_payload is not None:
            state["exam_decision"] = ExamArbitrationDecision.model_validate(exam_decision_payload)
        exam_state = state.get("exam_workflow_state")
        decision = state.get("exam_decision")
        exam = state.get("exam")
        blueprint = state.get("blueprint")
        if (
            isinstance(exam_state, ExamWorkflowState)
            and isinstance(decision, ExamArbitrationDecision)
            and isinstance(exam, ExamDocument)
            and isinstance(blueprint, ExamBlueprint)
            and (decision.question_ids or decision.section_ids)
        ):
            state["exam_workflow_state"] = exam_state.model_copy(
                update={
                    "replacement_question_numbers": resolve_exam_targets(
                        decision,
                        exam,
                        blueprint,
                    )
                }
            )
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
        generation_feedback: list[str] | None = None,
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
            generation_feedback=generation_feedback or [],
        )
        return await self.question_workflow.execute(
            request,
            on_run_created=on_run_created,
        )

    async def resume_question_run(self, run_id: UUID) -> tuple[WorkflowRun, dict[str, Any]]:
        return await self.question_workflow.resume(run_id)


def _question_bundle_artifact_ids(payload: object, *, expected: int) -> list[UUID]:
    if not isinstance(payload, list):
        raise ValueError("release requires the question run manifest")
    by_number: dict[int, UUID] = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        try:
            number = int(item["question_number"])
            artifact_id = UUID(str(item["bundle_artifact_id"]))
        except (KeyError, TypeError, ValueError):
            continue
        if item.get("status") in {RunStatus.SUCCEEDED, RunStatus.SUCCEEDED.value}:
            by_number[number] = artifact_id
    if sorted(by_number) != list(range(1, expected + 1)):
        raise ValueError("release requires one successful Bundle artifact per question")
    return [by_number[number] for number in range(1, expected + 1)]


def _question_feedback_for_number(feedback: list[str], number: int) -> list[str]:
    selected: list[str] = []
    for message in feedback:
        match = re.match(r"\s*Q(\d+)\b", message, flags=re.IGNORECASE)
        if match is None or int(match.group(1)) == number:
            selected.append(message)
    return selected


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
    return allocate_question_slot_coverage(blueprint, slots)


def _materialize_subject_profile(candidate: SubjectProfileCandidate) -> SubjectProfile:
    return SubjectProfile(
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


def _validate_subject_research_synthesis(
    synthesis: SubjectResearchSynthesis,
    reports: list[SubjectResearchReport],
) -> None:
    successful_ids = set(synthesis.successful_research_run_ids)
    if len(successful_ids) < 2:
        raise ValueError("subject research synthesis requires at least two successful runs")
    claim_ids = {
        f"{report.research_role}:{claim.id}" for report in reports for claim in report.claims
    }
    evidence_ids = {
        f"{report.research_role}:{evidence.id}"
        for report in reports
        for evidence in report.evidence
    }
    if not set(synthesis.adopted_claim_ids) <= claim_ids:
        raise ValueError("subject research synthesis adopts unknown claims")
    if not set(synthesis.rejected_claim_ids) <= claim_ids:
        raise ValueError("subject research synthesis rejects unknown claims")
    required_paths = {
        "profile.display_name",
        "profile.supported_question_types",
        "blueprint.duration_minutes",
        "blueprint.total_score",
        "blueprint.sections",
        "blueprint.coverage",
        "blueprint.difficulty_distribution",
    }
    trace_paths = {trace.target_path for trace in synthesis.field_traces}
    missing_paths = required_paths - trace_paths
    if missing_paths:
        raise ValueError(f"subject research synthesis is missing field traces: {missing_paths}")
    unclaimed_extra_decision_types = {
        "assumption",
        "default",
        "human_override",
        "system_default",
    }
    for trace in synthesis.field_traces:
        if not set(trace.claim_ids) <= claim_ids:
            raise ValueError(f"field trace {trace.target_path} references unknown claims")
        if not set(trace.evidence_ids) <= evidence_ids:
            raise ValueError(f"field trace {trace.target_path} references unknown evidence")
        if trace.target_path in required_paths and not trace.claim_ids:
            if trace.decision_type == "assumption":
                continue
            raise ValueError(
                f"field trace {trace.target_path} needs claims or decision_type=assumption"
            )
        if (
            trace.target_path not in required_paths
            and not trace.claim_ids
            and trace.decision_type not in unclaimed_extra_decision_types
        ):
            raise ValueError(
                f"unclaimed field trace {trace.target_path} has unsupported decision type "
                f"{trace.decision_type}"
            )


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
                **draft.model_dump(exclude={"coverage_tag"}),
                coverage_tag=slot.coverage_tag,
            )
        )
    validate_question_plan_coverage(blueprint, plans)
    validate_question_plan_timing(blueprint, plans)
    return plans
