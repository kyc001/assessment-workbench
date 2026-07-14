from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from assessment_workbench.agents import ExamAgentWorkflow
from assessment_workbench.application import (
    build_edited_exam_workflow,
    build_exam_workflow,
    latest_artifact_json,
    publish_question_bundle,
)
from assessment_workbench.config import Settings
from assessment_workbench.document_workflow import (
    latest_document_builds,
    parse_document_build_records,
)
from assessment_workbench.domain import (
    ArtifactRef,
    ExamBlueprint,
    ExamDocument,
    ExamQuestionBundle,
    HumanDecision,
    HumanDecisionType,
    QuestionGenerationRequest,
    QuestionPlan,
    RunStatus,
    SubjectProfile,
    WorkflowRun,
)
from assessment_workbench.exam_quality import validate_bundle_for_plan
from assessment_workbench.storage import ArtifactStore, RunStore, Workspace
from assessment_workbench.subject_research_workflow import parse_subject_research_records
from assessment_workbench.web_models import (
    ArtifactContent,
    EditableQuestion,
    ExamCreateRequest,
    RunDetail,
    RunSummary,
    WorkspaceInfo,
)


class WorkbenchServiceError(RuntimeError):
    def __init__(
        self,
        code: str,
        detail: str,
        *,
        status_code: int = 400,
        fields: dict[str, str] | None = None,
    ) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status_code = status_code
        self.fields = fields or {}


class WorkbenchApplicationService:
    def __init__(self, workspace: Workspace, settings: Settings) -> None:
        self.workspace = workspace
        self.settings = settings
        self.runs = RunStore(workspace)
        self.artifacts = ArtifactStore(workspace)
        self._background_tasks: set[asyncio.Task[Any]] = set()

    def workspace_info(self) -> WorkspaceInfo:
        runs = self.runs.list_runs()
        return WorkspaceInfo(
            root=str(self.workspace.root),
            database=str(self.workspace.db_path),
            run_count=len(runs),
        )

    def list_runs(self) -> list[RunSummary]:
        runs = self.runs.list_runs()
        parent_by_child, child_counts = self.runs.run_relationships()
        return [
            RunSummary(
                run=run,
                parent_run_id=parent_by_child.get(run.id),
                child_count=child_counts.get(run.id, 0),
            )
            for run in runs
        ]

    def run_detail(self, run_id: UUID) -> RunDetail:
        run = self._require_run(run_id)
        child_ids = self.runs.child_run_ids(run_id)
        children = self.runs.get_many(child_ids)
        return RunDetail(
            run=run,
            parent_run_id=self.runs.parent_run_id(run_id),
            events=self.runs.events(run_id),
            children=children,
            human_review=self.runs.pending_human_review(run_id),
            artifacts=self.artifacts.list(run_id),
        )

    def research_status(self, parent_run_id: UUID) -> list[dict[str, Any]]:
        payload = self._manifest_payload(parent_run_id, "subject-research-runs.json")
        if payload is None:
            return []
        try:
            records = parse_subject_research_records(payload)
        except ValueError as exc:
            raise WorkbenchServiceError(
                "invalid_manifest",
                f"subject research manifest is invalid: {exc}",
                status_code=422,
            ) from exc
        return [record.model_dump(mode="json") for record in records]

    def question_status(self, parent_run_id: UUID) -> list[dict[str, Any]]:
        payload = self._manifest_payload(parent_run_id, "question-runs.json")
        if payload is None:
            return []
        if not isinstance(payload, list):
            raise WorkbenchServiceError(
                "invalid_manifest",
                "question run manifest is not a list",
                status_code=422,
            )
        return [item for item in payload if isinstance(item, dict)]

    def document_status(self, parent_run_id: UUID) -> list[dict[str, Any]]:
        payload = self._manifest_payload(parent_run_id, "document-build-runs.json")
        if payload is None:
            return []
        try:
            records = latest_document_builds(parse_document_build_records(payload))
        except ValueError as exc:
            raise WorkbenchServiceError(
                "invalid_manifest",
                f"document build manifest is invalid: {exc}",
                status_code=422,
            ) from exc
        return [record.model_dump(mode="json") for record in records]

    def artifact_content(self, artifact_id: UUID) -> ArtifactContent:
        artifact = self._require_artifact(artifact_id)
        try:
            if artifact.media_type == "application/json":
                return ArtifactContent(
                    artifact=artifact,
                    kind="json",
                    content=self.artifacts.read_json(artifact.id),
                )
            if artifact.media_type.startswith("text/") or artifact.path.suffix.lower() in {
                ".tex",
                ".md",
                ".log",
                ".txt",
            }:
                content = self.artifacts.read_bytes(artifact.id).decode("utf-8", errors="replace")
                return ArtifactContent(artifact=artifact, kind="text", content=content)
        except (FileNotFoundError, KeyError, ValueError) as exc:
            raise WorkbenchServiceError(
                "artifact_unavailable",
                f"artifact cannot be read or verified: {artifact_id}",
                status_code=409,
            ) from exc
        return ArtifactContent(artifact=artifact, kind="binary")

    def artifact_bytes(self, artifact_id: UUID) -> tuple[ArtifactRef, bytes]:
        artifact = self._require_artifact(artifact_id)
        try:
            return artifact, self.artifacts.read_bytes(artifact.id)
        except (FileNotFoundError, KeyError, ValueError) as exc:
            raise WorkbenchServiceError(
                "artifact_unavailable",
                f"artifact cannot be read or verified: {artifact_id}",
                status_code=409,
            ) from exc

    async def launch_exam(self, request: ExamCreateRequest) -> WorkflowRun:
        workflow = build_exam_workflow(
            self.workspace,
            self.settings,
            compile_pdf=request.compile_pdf,
        )

        async def execute(
            on_created: Callable[[WorkflowRun], None],
        ) -> tuple[WorkflowRun, dict[str, Any]]:
            return await workflow.execute(
                subject=request.subject.strip(),
                target_level=request.target_level.strip(),
                requirements=request.requirements.strip(),
                source_context=request.source_context,
                require_blueprint_approval=request.human_gates,
                require_exam_approval=request.human_gates,
                on_run_created=on_created,
            )

        return await self._launch_with_created(execute)

    async def launch_resume(self, run_id: UUID) -> WorkflowRun:
        run = self._require_run(run_id)
        if run.status is RunStatus.WAITING_HUMAN:
            raise WorkbenchServiceError(
                "human_decision_required",
                "run is waiting for a human decision before it can resume",
                status_code=409,
            )
        workflow = build_exam_workflow(self.workspace, self.settings, compile_pdf=True)
        if run.workflow == "exam_agent_generation":
            task = asyncio.create_task(workflow.resume(run_id))
        elif run.workflow == "exam_question_generation":
            task = asyncio.create_task(self._resume_question(workflow, run_id))
        elif run.workflow == "exam_edited_assembly":
            task = asyncio.create_task(
                build_edited_exam_workflow(self.workspace, self.settings).resume(run_id)
            )
        else:
            raise WorkbenchServiceError(
                "resume_not_supported",
                f"workflow does not have a resume handler: {run.workflow}",
                status_code=409,
            )
        self._track(task)
        return run

    def resolve_human(
        self,
        run_id: UUID,
        decision: HumanDecisionType,
        *,
        actor: str,
        reason: str,
    ) -> WorkflowRun:
        request = self.runs.pending_human_review(run_id)
        if request is None:
            raise WorkbenchServiceError(
                "human_review_missing",
                "run has no pending human review",
                status_code=409,
            )
        try:
            return self.runs.resolve_human_review(
                HumanDecision(
                    request_id=request.id,
                    run_id=run_id,
                    decision=decision,
                    actor=actor.strip() or "gui-user",
                    reason=reason.strip(),
                    input_artifact_ids=request.artifact_ids,
                )
            )
        except (KeyError, ValueError) as exc:
            raise WorkbenchServiceError(
                "invalid_human_decision", str(exc), status_code=409
            ) from exc

    def request_cancel(self, run_id: UUID) -> WorkflowRun:
        try:
            return self.runs.request_cancel(run_id)
        except (KeyError, ValueError) as exc:
            raise WorkbenchServiceError("cancel_not_allowed", str(exc), status_code=409) from exc

    def editable_question(self, parent_run_id: UUID, number: int) -> EditableQuestion:
        path = self._question_path(parent_run_id, number)
        if not path.is_file():
            raise WorkbenchServiceError(
                "question_not_found",
                f"editable question {number} does not exist",
                status_code=404,
            )
        try:
            raw = path.read_bytes()
            bundle = ExamQuestionBundle.model_validate_json(raw)
        except (FileNotFoundError, OSError, ValueError) as exc:
            raise WorkbenchServiceError("invalid_question", str(exc), status_code=422) from exc
        return EditableQuestion(
            question_number=number,
            sha256=hashlib.sha256(raw).hexdigest(),
            bundle=bundle.model_dump(mode="json"),
        )

    def save_editable_question(
        self,
        parent_run_id: UUID,
        number: int,
        *,
        expected_sha256: str,
        payload: dict[str, Any],
    ) -> EditableQuestion:
        current = self.editable_question(parent_run_id, number)
        if current.sha256 != expected_sha256:
            raise WorkbenchServiceError(
                "question_version_conflict",
                "editable question changed after it was loaded",
                status_code=409,
            )
        try:
            bundle = ExamQuestionBundle.model_validate(payload)
            if bundle.question.number != number:
                raise ValueError("question number does not match the edited slot")
            plan = self._question_plan(parent_run_id, number)
            validate_bundle_for_plan(bundle, plan)
        except (FileNotFoundError, KeyError, ValueError) as exc:
            raise WorkbenchServiceError("invalid_question", str(exc), status_code=422) from exc
        self.artifacts.write_editable_json(
            parent_run_id,
            f"questions/{number:02d}.json",
            bundle.model_dump(mode="json"),
        )
        return self.editable_question(parent_run_id, number)

    def publish_question_run(
        self,
        parent_run_id: UUID,
        number: int,
        child_run_id: UUID,
    ) -> EditableQuestion:
        self._require_run(parent_run_id)
        child_run = self._require_run(child_run_id)
        if child_run.workflow != "exam_question_generation":
            raise WorkbenchServiceError(
                "question_run_required",
                "selected run is not a question generation run",
                status_code=409,
            )
        if child_run.status is not RunStatus.SUCCEEDED:
            raise WorkbenchServiceError(
                "question_run_incomplete",
                "only a succeeded question run can be published",
                status_code=409,
            )
        try:
            request = QuestionGenerationRequest.model_validate(
                latest_artifact_json(self.artifacts, child_run_id, "question-request.json")
            )
            if request.parent_run_id != parent_run_id or request.plan.number != number:
                raise ValueError("question run does not belong to the requested parent and slot")
            plan = self._question_plan(parent_run_id, number)
            bundle = ExamQuestionBundle.model_validate(
                latest_artifact_json(self.artifacts, child_run_id, "question-bundle.json")
            )
            validate_bundle_for_plan(bundle, plan)
            bundle_artifact = self.artifacts.latest(child_run_id, "question-bundle.json")
            if bundle_artifact is None:
                raise ValueError("question run has no bundle artifact")
            publish_question_bundle(
                self.workspace,
                parent_run_id=parent_run_id,
                plan=plan,
                child_run=child_run,
                bundle=bundle,
                bundle_artifact=bundle_artifact,
            )
        except (FileNotFoundError, KeyError, ValueError) as exc:
            raise WorkbenchServiceError(
                "question_run_invalid",
                f"question run cannot be published: {exc}",
                status_code=422,
            ) from exc
        return self.editable_question(parent_run_id, number)

    async def launch_question_rerun(
        self,
        parent_run_id: UUID,
        number: int,
        feedback: list[str],
    ) -> WorkflowRun:
        profile, blueprint, plan = self._question_context(parent_run_id, number)
        workflow = build_exam_workflow(self.workspace, self.settings, compile_pdf=False)

        async def execute(
            on_created: Callable[[WorkflowRun], None],
        ) -> tuple[WorkflowRun, dict[str, Any]]:
            child_run, state = await workflow.generate_question_run(
                profile=profile,
                blueprint=blueprint,
                plan=plan,
                parent_run_id=parent_run_id,
                generation_feedback=feedback,
                on_run_created=on_created,
            )
            bundle = state.get("bundle")
            bundle_artifact = state.get("bundle_artifact")
            if (
                child_run.status is RunStatus.SUCCEEDED
                and isinstance(bundle, ExamQuestionBundle)
                and isinstance(bundle_artifact, ArtifactRef)
            ):
                publish_question_bundle(
                    self.workspace,
                    parent_run_id=parent_run_id,
                    plan=plan,
                    child_run=child_run,
                    bundle=bundle,
                    bundle_artifact=bundle_artifact,
                )
            return child_run, state

        return await self._launch_with_created(execute)

    async def launch_edited_assembly(
        self,
        parent_run_id: UUID,
        *,
        human_gates: bool,
    ) -> WorkflowRun:
        self._require_run(parent_run_id)
        try:
            blueprint = ExamBlueprint.model_validate(
                latest_artifact_json(self.artifacts, parent_run_id, "exam-blueprint.json")
            )
            count = sum(section.count for section in blueprint.sections)
            missing = [
                number
                for number in range(1, count + 1)
                if not self._question_path(parent_run_id, number).is_file()
            ]
            if missing:
                rendered = ", ".join(str(number) for number in missing)
                raise WorkbenchServiceError(
                    "editable_questions_incomplete",
                    f"cannot assemble because editable questions are missing: {rendered}",
                    status_code=409,
                    fields={"missing_questions": rendered},
                )
            bundles = [
                ExamQuestionBundle.model_validate_json(
                    self._question_path(parent_run_id, number).read_bytes()
                )
                for number in range(1, count + 1)
            ]
            exam = ExamDocument(
                blueprint_id=blueprint.id,
                title=blueprint.title,
                subject_profile=blueprint.subject_profile,
                duration_minutes=blueprint.duration_minutes,
                total_score=blueprint.total_score,
                language=blueprint.language,
                calculator_policy=blueprint.calculator_policy,
                questions=bundles,
            )
        except WorkbenchServiceError:
            raise
        except (FileNotFoundError, KeyError, ValueError) as exc:
            raise WorkbenchServiceError(
                "edited_assembly_invalid",
                f"editable exam cannot be assembled: {exc}",
                status_code=422,
            ) from exc
        workflow = build_edited_exam_workflow(self.workspace, self.settings)

        async def execute(
            on_created: Callable[[WorkflowRun], None],
        ) -> tuple[WorkflowRun, dict[str, Any]]:
            return await workflow.execute(
                exam,
                source_parent_run_id=parent_run_id,
                require_document_approval=human_gates,
                on_run_created=on_created,
            )

        return await self._launch_with_created(execute)

    async def _resume_question(
        self,
        workflow: ExamAgentWorkflow,
        run_id: UUID,
    ) -> tuple[WorkflowRun, dict[str, Any]]:
        resumed, state = await workflow.resume_question_run(run_id)
        if resumed.status is not RunStatus.SUCCEEDED:
            return resumed, state
        request = QuestionGenerationRequest.model_validate(
            latest_artifact_json(self.artifacts, resumed.id, "question-request.json")
        )
        bundle = state.get("bundle")
        bundle_artifact = state.get("bundle_artifact")
        if (
            request.parent_run_id is not None
            and isinstance(bundle, ExamQuestionBundle)
            and isinstance(bundle_artifact, ArtifactRef)
        ):
            publish_question_bundle(
                self.workspace,
                parent_run_id=request.parent_run_id,
                plan=request.plan,
                child_run=resumed,
                bundle=bundle,
                bundle_artifact=bundle_artifact,
            )
        return resumed, state

    async def _launch_with_created(
        self,
        operation: Callable[
            [Callable[[WorkflowRun], None]],
            Coroutine[Any, Any, tuple[WorkflowRun, dict[str, Any]]],
        ],
    ) -> WorkflowRun:
        loop = asyncio.get_running_loop()
        created: asyncio.Future[WorkflowRun] = loop.create_future()

        def on_created(run: WorkflowRun) -> None:
            if not created.done():
                created.set_result(run)

        task: asyncio.Task[tuple[WorkflowRun, dict[str, Any]]] = asyncio.create_task(
            operation(on_created)
        )
        self._track(task)
        waiters = {
            cast(asyncio.Future[Any], created),
            cast(asyncio.Future[Any], task),
        }
        done, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        if created in done:
            return created.result()
        try:
            task.result()
        except WorkbenchServiceError:
            raise
        except Exception as exc:
            raise WorkbenchServiceError(
                "run_launch_failed",
                f"workflow failed before creating a run: {exc}",
                status_code=500,
            ) from exc
        raise WorkbenchServiceError("run_not_created", "workflow finished without creating a run")

    def _track(self, task: asyncio.Task[Any]) -> None:
        self._background_tasks.add(task)

        def finished(completed: asyncio.Task[Any]) -> None:
            self._background_tasks.discard(completed)
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(finished)

    def _question_context(
        self,
        parent_run_id: UUID,
        number: int,
    ) -> tuple[SubjectProfile, ExamBlueprint, QuestionPlan]:
        try:
            profile = SubjectProfile.model_validate(
                latest_artifact_json(self.artifacts, parent_run_id, "subject-profile.json")
            )
            blueprint = ExamBlueprint.model_validate(
                latest_artifact_json(self.artifacts, parent_run_id, "exam-blueprint.json")
            )
            plan = self._question_plan(parent_run_id, number)
        except (FileNotFoundError, KeyError, ValueError) as exc:
            raise WorkbenchServiceError(
                "planning_artifacts_missing",
                f"parent run has no valid planning artifacts: {exc}",
                status_code=409,
            ) from exc
        return profile, blueprint, plan

    def _question_plan(self, parent_run_id: UUID, number: int) -> QuestionPlan:
        payload = latest_artifact_json(self.artifacts, parent_run_id, "question-plans.json")
        if not isinstance(payload, list):
            raise ValueError("question plan artifact is not a list")
        plans = [QuestionPlan.model_validate(item) for item in payload]
        plan = next((item for item in plans if item.number == number), None)
        if plan is None:
            raise ValueError(f"question number is not present in the plan: {number}")
        return plan

    def _manifest_payload(self, parent_run_id: UUID, name: str) -> object | None:
        editable = self.workspace.root / "editable" / str(parent_run_id) / name
        try:
            if editable.is_file():
                return self.artifacts.read_editable_json(parent_run_id, name)
            latest = self.artifacts.latest(parent_run_id, name)
            return self.artifacts.read_json(latest.id) if latest is not None else None
        except (FileNotFoundError, KeyError, ValueError) as exc:
            raise WorkbenchServiceError(
                "invalid_manifest",
                f"manifest cannot be read or verified: {name}",
                status_code=422,
            ) from exc

    def _question_path(self, parent_run_id: UUID, number: int) -> Path:
        return (
            self.workspace.root
            / "editable"
            / str(parent_run_id)
            / "questions"
            / f"{number:02d}.json"
        )

    def _require_run(self, run_id: UUID) -> WorkflowRun:
        run = self.runs.get(run_id)
        if run is None:
            raise WorkbenchServiceError(
                "run_not_found", f"run does not exist: {run_id}", status_code=404
            )
        return run

    def _require_artifact(self, artifact_id: UUID) -> ArtifactRef:
        artifact = self.artifacts.get(artifact_id)
        if artifact is None:
            raise WorkbenchServiceError(
                "artifact_not_found",
                f"artifact does not exist: {artifact_id}",
                status_code=404,
            )
        return artifact
