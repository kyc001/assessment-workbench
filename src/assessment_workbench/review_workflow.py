from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from assessment_workbench.capabilities import CapabilityCatalog
from assessment_workbench.domain import (
    ArtifactRef,
    ExamQuestionBundle,
    QuestionGenerationRequest,
    QuestionReviewRequest,
    ReviewerRunRecord,
    ReviewReport,
    RunStatus,
    WorkflowRun,
)
from assessment_workbench.ports import StructuredModel
from assessment_workbench.prompting import (
    complete_with_prompt,
    context_artifact_ids,
    json_prompt,
)
from assessment_workbench.storage import ArtifactStore, RunStore
from assessment_workbench.workflow import WorkflowEngine

REVIEWS_GENERATING = "REVIEWS_GENERATING"
REVIEW_GENERATING = "REVIEW_GENERATING"


@dataclass(frozen=True)
class ReviewBatchOutcome:
    reports: list[ReviewReport]
    records: list[ReviewerRunRecord]
    manifest_artifact: ArtifactRef
    child_run_ids: list[UUID]


class ReviewerPoolWorkflow:
    def __init__(
        self,
        model: StructuredModel,
        artifacts: ArtifactStore,
        runs: RunStore,
        capabilities: CapabilityCatalog,
        *,
        max_attempts: int,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("reviewer max_attempts must be at least 1")
        self.model = model
        self.artifacts = artifacts
        self.runs = runs
        self.capabilities = capabilities
        self.prompts = capabilities.prompts
        self.max_attempts = max_attempts

    async def execute(
        self,
        question_run_id: UUID,
        request: QuestionGenerationRequest,
        bundle: ExamQuestionBundle,
        restored_records: object,
        *,
        input_artifact_ids: list[UUID],
    ) -> ReviewBatchOutcome:
        records = self._load_records(question_run_id, restored_records)
        manifest_lock = asyncio.Lock()
        latest_snapshot: ArtifactRef | None = None

        def write_manifest() -> ArtifactRef:
            nonlocal latest_snapshot
            payload = [record.model_dump(mode="json") for record in records]
            self.artifacts.write_editable_json(question_run_id, "review-runs.json", payload)
            latest_snapshot = self.artifacts.write_json(
                question_run_id,
                "review-runs.json",
                payload,
                created_by_phase=REVIEWS_GENERATING,
            )
            return latest_snapshot

        reports = self.load_matching_reports(records, bundle, request.profile.reviewers)
        pending = [name for name in request.profile.reviewers if name not in reports]
        while pending:
            jobs: list[asyncio.Task[None]] = []
            for reviewer in pending:
                attempt = 1 + max(
                    (
                        record.attempt
                        for record in records
                        if record.reviewer == reviewer and record_matches(record, bundle)
                    ),
                    default=0,
                )
                if attempt > self.max_attempts:
                    raise RuntimeError(f"reviewer exhausted retry budget: {reviewer}")
                review_request = QuestionReviewRequest(
                    reviewer=reviewer,
                    plan=request.plan,
                    bundle=bundle,
                    capability_context=request.capability_context,
                    parent_run_id=question_run_id,
                    attempt=attempt,
                    input_artifact_ids=input_artifact_ids,
                )

                async def run_reviewer(
                    current_request: QuestionReviewRequest = review_request,
                ) -> None:
                    created_record: ReviewerRunRecord | None = None

                    def on_created(run: WorkflowRun) -> None:
                        nonlocal created_record
                        created_record = ReviewerRunRecord(
                            reviewer=current_request.reviewer,
                            attempt=current_request.attempt,
                            run_id=run.id,
                            status=run.status,
                            question_version_id=bundle.question.id,
                            solution_version_id=bundle.solution.id,
                            rubric_version_id=bundle.rubric.id,
                        )
                        records.append(created_record)
                        write_manifest()

                    review_run, review_state = await self._execute_reviewer(
                        current_request,
                        on_run_created=on_created,
                    )
                    async with manifest_lock:
                        if created_record is None:
                            raise RuntimeError("reviewer run was not reported at creation")
                        report_artifact = review_state.get("report_artifact")
                        report_id = (
                            report_artifact.id if isinstance(report_artifact, ArtifactRef) else None
                        )
                        status = review_run.status
                        error = review_run.error
                        if status is RunStatus.SUCCEEDED and report_id is None:
                            status = RunStatus.FAILED
                            error = "reviewer run produced no report artifact"
                        replacement = ReviewerRunRecord(
                            **created_record.model_dump(
                                exclude={"status", "report_artifact_id", "error"}
                            ),
                            status=status,
                            report_artifact_id=report_id,
                            error=error,
                        )
                        index = next(
                            index
                            for index, record in enumerate(records)
                            if record.run_id == created_record.run_id
                        )
                        records[index] = replacement
                        write_manifest()

                jobs.append(asyncio.create_task(run_reviewer()))
            await asyncio.gather(*jobs)
            reports = self.load_matching_reports(records, bundle, request.profile.reviewers)
            pending = [name for name in request.profile.reviewers if name not in reports]

        if latest_snapshot is None:
            latest_snapshot = write_manifest()
        return ReviewBatchOutcome(
            reports=[reports[name] for name in request.profile.reviewers],
            records=records,
            manifest_artifact=latest_snapshot,
            child_run_ids=list(dict.fromkeys(record.run_id for record in records)),
        )

    def load_matching_reports(
        self,
        records: list[ReviewerRunRecord],
        bundle: ExamQuestionBundle,
        reviewer_names: list[str],
    ) -> dict[str, ReviewReport]:
        reports: dict[str, ReviewReport] = {}
        for reviewer in reviewer_names:
            candidates = sorted(
                (
                    record
                    for record in records
                    if record.reviewer == reviewer
                    and record.status is RunStatus.SUCCEEDED
                    and record.report_artifact_id is not None
                    and record_matches(record, bundle)
                ),
                key=lambda record: record.attempt,
                reverse=True,
            )
            for record in candidates:
                artifact_id = record.report_artifact_id
                if artifact_id is None:
                    continue
                try:
                    report = ReviewReport.model_validate(self.artifacts.read_json(artifact_id))
                except (KeyError, OSError, ValueError):
                    continue
                reports[reviewer] = report.model_copy(update={"reviewer": reviewer})
                break
        return reports

    async def _execute_reviewer(
        self,
        request: QuestionReviewRequest,
        *,
        on_run_created: Callable[[WorkflowRun], None],
    ) -> tuple[WorkflowRun, dict[str, Any]]:
        async def review(state: dict[str, Any]) -> dict[str, Any]:
            request_artifact = self.artifacts.write_json(
                state["run_id"],
                "review-request.json",
                request.model_dump(mode="json"),
                created_by_phase=REVIEW_GENERATING,
            )
            definition = self.capabilities.reviewers.require(request.reviewer)
            if definition.handler is not None:
                report = definition.handler(request.bundle)
            else:
                assert definition.prompt_key is not None
                prompt = self.prompts.require(definition.prompt_key)
                report = await complete_with_prompt(
                    self.model,
                    prompt=prompt,
                    user_prompt=json_prompt(
                        question_plan=request.plan.model_dump(mode="json"),
                        bundle=request.bundle.model_dump(mode="json"),
                        capability_context=request.capability_context,
                    ),
                    response_model=ReviewReport,
                    run_id=state["run_id"],
                    artifacts=self.artifacts,
                    created_by_phase=REVIEW_GENERATING,
                    input_artifact_ids=[
                        request_artifact.id,
                        *request.input_artifact_ids,
                        *context_artifact_ids(state),
                    ],
                )
                report = report.model_copy(update={"reviewer": request.reviewer})
            report_artifact = self.artifacts.write_json(
                state["run_id"],
                "review-report.json",
                report.model_dump(mode="json"),
                created_by_phase=REVIEW_GENERATING,
            )
            return {
                "report": report,
                "report_artifact": report_artifact,
                "output_artifact_ids": [request_artifact.id, report_artifact.id],
                "_checkpoint_artifacts": {
                    "request": request_artifact.id,
                    "report": report_artifact.id,
                },
            }

        return await WorkflowEngine(self.runs).execute(
            "question_review",
            [(REVIEW_GENERATING, review)],
            parent_run_id=request.parent_run_id,
            on_run_created=on_run_created,
        )

    def _load_records(
        self,
        question_run_id: UUID,
        restored_records: object,
    ) -> list[ReviewerRunRecord]:
        records = (
            list(restored_records)
            if isinstance(restored_records, list)
            and all(isinstance(item, ReviewerRunRecord) for item in restored_records)
            else []
        )
        latest = self.artifacts.latest(question_run_id, "review-runs.json")
        if latest is not None:
            artifact_records = parse_review_records(self.artifacts.read_json(latest.id))
            known = {record.run_id for record in records}
            records.extend(record for record in artifact_records if record.run_id not in known)
        return [self._refresh_record(record) for record in records]

    def _refresh_record(self, record: ReviewerRunRecord) -> ReviewerRunRecord:
        run = self.runs.get(record.run_id)
        if run is None or (run.status is record.status and record.report_artifact_id is not None):
            return record
        report_artifact = self.artifacts.latest(record.run_id, "review-report.json")
        error: str | None
        if run.status is RunStatus.SUCCEEDED and report_artifact is None:
            status = RunStatus.FAILED
            error = "succeeded reviewer run has no report artifact"
        else:
            status = run.status
            error = run.error
        return ReviewerRunRecord(
            **record.model_dump(exclude={"status", "report_artifact_id", "error"}),
            status=status,
            report_artifact_id=(
                report_artifact.id if report_artifact is not None else record.report_artifact_id
            ),
            error=error,
        )


def record_matches(record: ReviewerRunRecord, bundle: ExamQuestionBundle) -> bool:
    return (
        record.question_version_id == bundle.question.id
        and record.solution_version_id == bundle.solution.id
        and record.rubric_version_id == bundle.rubric.id
    )


def parse_review_records(payload: object) -> list[ReviewerRunRecord]:
    if not isinstance(payload, list):
        raise ValueError("reviewer run manifest is not a list")
    return [ReviewerRunRecord.model_validate(item) for item in payload]
