from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from assessment_workbench.capabilities import CapabilityCatalog
from assessment_workbench.domain import (
    ArtifactRef,
    ExamBlueprint,
    ExamBundleVersionSignature,
    ExamDocument,
    ExamReviewerRunRecord,
    ExamReviewReport,
    ExamReviewRequest,
    QuestionPlan,
    RunStatus,
    SubjectProfile,
    WorkflowRun,
)
from assessment_workbench.exam_quality import exam_bundle_signature, validate_exam_review_report
from assessment_workbench.ports import StructuredModel
from assessment_workbench.prompting import complete_with_prompt, json_prompt
from assessment_workbench.storage import ArtifactStore, RunStore
from assessment_workbench.workflow import WorkflowEngine

EXAM_REVIEWS_GENERATING = "EXAM_REVIEWS_GENERATING"
EXAM_REVIEW_GENERATING = "EXAM_REVIEW_GENERATING"


class ExamReviewerExhaustedError(RuntimeError):
    pass


@dataclass(frozen=True)
class ExamReviewBatchOutcome:
    reports: list[ExamReviewReport]
    records: list[ExamReviewerRunRecord]
    manifest_artifact: ArtifactRef
    child_run_ids: list[UUID]


class ExamReviewerPoolWorkflow:
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
            raise ValueError("exam reviewer max_attempts must be at least 1")
        self.model = model
        self.artifacts = artifacts
        self.runs = runs
        self.capabilities = capabilities
        self.prompts = capabilities.prompts
        self.max_attempts = max_attempts

    async def execute(
        self,
        parent_run_id: UUID,
        *,
        profile: SubjectProfile,
        blueprint: ExamBlueprint,
        plans: list[QuestionPlan],
        exam: ExamDocument,
        capability_context: dict[str, list[str]],
        restored_records: object,
    ) -> ExamReviewBatchOutcome:
        records = self._load_records(parent_run_id, restored_records)
        signature = exam_bundle_signature(exam)
        reviewer_names = list(self.capabilities.exam_reviewers.names())
        manifest_lock = asyncio.Lock()
        latest_snapshot: ArtifactRef | None = None

        def write_manifest() -> ArtifactRef:
            nonlocal latest_snapshot
            payload = [record.model_dump(mode="json") for record in records]
            self.artifacts.write_editable_json(parent_run_id, "exam-review-runs.json", payload)
            latest_snapshot = self.artifacts.write_json(
                parent_run_id,
                "exam-review-runs.json",
                payload,
                created_by_phase=EXAM_REVIEWS_GENERATING,
            )
            return latest_snapshot

        reports = self.load_matching_reports(records, exam, reviewer_names)
        pending = [name for name in reviewer_names if name not in reports]
        while pending:
            jobs: list[asyncio.Task[None]] = []
            for reviewer in pending:
                attempt = 1 + max(
                    (
                        record.attempt
                        for record in records
                        if record.reviewer == reviewer
                        and exam_record_matches(record, exam, signature)
                    ),
                    default=0,
                )
                if attempt > self.max_attempts:
                    raise ExamReviewerExhaustedError(
                        f"exam reviewer exhausted retry budget: {reviewer}"
                    )
                review_request = ExamReviewRequest(
                    reviewer=reviewer,
                    profile=profile,
                    blueprint=blueprint,
                    plans=plans,
                    exam=exam,
                    capability_context=capability_context,
                    parent_run_id=parent_run_id,
                    attempt=attempt,
                )

                async def run_reviewer(current_request: ExamReviewRequest = review_request) -> None:
                    created_record: ExamReviewerRunRecord | None = None

                    def on_created(run: WorkflowRun) -> None:
                        nonlocal created_record
                        created_record = ExamReviewerRunRecord(
                            reviewer=current_request.reviewer,
                            attempt=current_request.attempt,
                            run_id=run.id,
                            status=run.status,
                            exam_id=exam.id,
                            signature=signature,
                        )
                        records.append(created_record)
                        write_manifest()

                    review_run, review_state = await self._execute_reviewer(
                        current_request,
                        on_run_created=on_created,
                    )
                    async with manifest_lock:
                        if created_record is None:
                            raise RuntimeError("exam reviewer run was not reported at creation")
                        report_artifact = review_state.get("report_artifact")
                        report_id = (
                            report_artifact.id if isinstance(report_artifact, ArtifactRef) else None
                        )
                        status = review_run.status
                        error = review_run.error
                        if status is RunStatus.SUCCEEDED and report_id is None:
                            status = RunStatus.FAILED
                            error = "exam reviewer run produced no report artifact"
                        replacement = ExamReviewerRunRecord(
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
            reports = self.load_matching_reports(records, exam, reviewer_names)
            pending = [name for name in reviewer_names if name not in reports]

        if latest_snapshot is None:
            latest_snapshot = write_manifest()
        return ExamReviewBatchOutcome(
            reports=[reports[name] for name in reviewer_names],
            records=records,
            manifest_artifact=latest_snapshot,
            child_run_ids=list(dict.fromkeys(record.run_id for record in records)),
        )

    def load_matching_reports(
        self,
        records: list[ExamReviewerRunRecord],
        exam: ExamDocument,
        reviewer_names: list[str],
    ) -> dict[str, ExamReviewReport]:
        signature = exam_bundle_signature(exam)
        reports: dict[str, ExamReviewReport] = {}
        for reviewer in reviewer_names:
            candidates = sorted(
                (
                    record
                    for record in records
                    if record.reviewer == reviewer
                    and record.status is RunStatus.SUCCEEDED
                    and record.report_artifact_id is not None
                    and exam_record_matches(record, exam, signature)
                ),
                key=lambda record: record.attempt,
                reverse=True,
            )
            for record in candidates:
                artifact_id = record.report_artifact_id
                if artifact_id is None:
                    continue
                try:
                    report = ExamReviewReport.model_validate(self.artifacts.read_json(artifact_id))
                except (KeyError, OSError, ValueError):
                    continue
                reports[reviewer] = report.model_copy(update={"reviewer": reviewer})
                break
        return reports

    async def _execute_reviewer(
        self,
        request: ExamReviewRequest,
        *,
        on_run_created: Callable[[WorkflowRun], None],
    ) -> tuple[WorkflowRun, dict[str, Any]]:
        async def review(state: dict[str, Any]) -> dict[str, Any]:
            request_artifact = self.artifacts.write_json(
                state["run_id"],
                "exam-review-request.json",
                request.model_dump(mode="json"),
                created_by_phase=EXAM_REVIEW_GENERATING,
            )
            definition = self.capabilities.exam_reviewers.require(request.reviewer)
            prompt = self.prompts.require(definition.prompt_key)
            report = await complete_with_prompt(
                self.model,
                prompt=prompt,
                user_prompt=json_prompt(
                    subject_profile=request.profile.model_dump(mode="json"),
                    blueprint=request.blueprint.model_dump(mode="json"),
                    question_plans=[plan.model_dump(mode="json") for plan in request.plans],
                    exam=request.exam.model_dump(mode="json"),
                    capability_context=request.capability_context,
                ),
                response_model=ExamReviewReport,
                run_id=state["run_id"],
            )
            report = report.model_copy(update={"reviewer": request.reviewer})
            validate_exam_review_report(report, request.exam, request.blueprint)
            report_artifact = self.artifacts.write_json(
                state["run_id"],
                "exam-review-report.json",
                report.model_dump(mode="json"),
                created_by_phase=EXAM_REVIEW_GENERATING,
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
            "exam_review",
            [(EXAM_REVIEW_GENERATING, review)],
            parent_run_id=request.parent_run_id,
            on_run_created=on_run_created,
        )

    def _load_records(
        self,
        parent_run_id: UUID,
        restored_records: object,
    ) -> list[ExamReviewerRunRecord]:
        records = (
            list(restored_records)
            if isinstance(restored_records, list)
            and all(isinstance(item, ExamReviewerRunRecord) for item in restored_records)
            else []
        )
        latest = self.artifacts.latest(parent_run_id, "exam-review-runs.json")
        if latest is not None:
            artifact_records = parse_exam_review_records(self.artifacts.read_json(latest.id))
            known = {record.run_id for record in records}
            records.extend(record for record in artifact_records if record.run_id not in known)
        return [self._refresh_record(record) for record in records]

    def _refresh_record(self, record: ExamReviewerRunRecord) -> ExamReviewerRunRecord:
        run = self.runs.get(record.run_id)
        if run is None or (run.status is record.status and record.report_artifact_id is not None):
            return record
        report_artifact = self.artifacts.latest(record.run_id, "exam-review-report.json")
        error: str | None
        if run.status is RunStatus.SUCCEEDED and report_artifact is None:
            status = RunStatus.FAILED
            error = "succeeded exam reviewer run has no report artifact"
        else:
            status = run.status
            error = run.error
        return ExamReviewerRunRecord(
            **record.model_dump(exclude={"status", "report_artifact_id", "error"}),
            status=status,
            report_artifact_id=(
                report_artifact.id if report_artifact is not None else record.report_artifact_id
            ),
            error=error,
        )


def exam_record_matches(
    record: ExamReviewerRunRecord,
    exam: ExamDocument,
    signature: ExamBundleVersionSignature | None = None,
) -> bool:
    expected = signature or exam_bundle_signature(exam)
    return record.exam_id == exam.id and record.signature == expected


def parse_exam_review_records(payload: object) -> list[ExamReviewerRunRecord]:
    if not isinstance(payload, list):
        raise ValueError("exam reviewer run manifest is not a list")
    return [ExamReviewerRunRecord.model_validate(item) for item in payload]
