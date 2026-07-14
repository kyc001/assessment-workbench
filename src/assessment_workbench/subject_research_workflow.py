from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from assessment_workbench.capabilities import CapabilityCatalog
from assessment_workbench.domain import (
    ArtifactRef,
    RunStatus,
    SubjectResearchReport,
    SubjectResearchRequest,
    SubjectResearchRunRecord,
    WorkflowRun,
)
from assessment_workbench.model_contracts import canonical_json_sha256
from assessment_workbench.ports import StructuredModel
from assessment_workbench.prompting import complete_with_prompt, json_prompt
from assessment_workbench.storage import ArtifactStore, RunStore
from assessment_workbench.workflow import WorkflowEngine

SUBJECT_RESEARCHING = "SUBJECT_RESEARCHING"
SUBJECT_RESEARCH_ROLE_RUNNING = "SUBJECT_RESEARCH_ROLE_RUNNING"


@dataclass(frozen=True)
class SubjectResearchBatchOutcome:
    reports: dict[str, SubjectResearchReport]
    records: list[SubjectResearchRunRecord]
    manifest_artifact: ArtifactRef
    child_run_ids: list[UUID]

    @property
    def successful_run_ids(self) -> list[UUID]:
        return [
            record.run_id
            for record in self.records
            if record.status is RunStatus.SUCCEEDED and record.report_artifact_id is not None
        ]

    @property
    def failed_run_ids(self) -> list[UUID]:
        return [
            record.run_id for record in self.records if record.status is not RunStatus.SUCCEEDED
        ]


class SubjectResearchPoolWorkflow:
    def __init__(
        self,
        model: StructuredModel,
        artifacts: ArtifactStore,
        runs: RunStore,
        capabilities: CapabilityCatalog,
        *,
        max_attempts: int,
        quorum: int = 2,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("subject research max_attempts must be at least 1")
        role_count = len(capabilities.subject_researchers.names())
        if quorum < 1 or quorum > role_count:
            raise ValueError("subject research quorum must fit the registered role count")
        self.model = model
        self.artifacts = artifacts
        self.runs = runs
        self.capabilities = capabilities
        self.max_attempts = max_attempts
        self.quorum = quorum

    async def execute(
        self,
        parent_run_id: UUID,
        *,
        subject: str,
        target_level: str,
        requirements: str,
        source_context: str,
        restored_records: object,
        input_artifact_ids: list[UUID],
    ) -> SubjectResearchBatchOutcome:
        input_signature = canonical_json_sha256(
            {
                "subject": subject,
                "target_level": target_level,
                "requirements": requirements,
                "source_context": source_context,
            }
        )
        records = self._load_records(parent_run_id, restored_records)
        manifest_lock = asyncio.Lock()
        latest_snapshot: ArtifactRef | None = None

        def write_manifest() -> ArtifactRef:
            nonlocal latest_snapshot
            payload = [record.model_dump(mode="json") for record in records]
            self.artifacts.write_editable_json(
                parent_run_id,
                "subject-research-runs.json",
                payload,
            )
            latest_snapshot = self.artifacts.write_json(
                parent_run_id,
                "subject-research-runs.json",
                payload,
                created_by_phase=SUBJECT_RESEARCHING,
            )
            return latest_snapshot

        reports = self._load_matching_reports(records, input_signature)
        roles = list(self.capabilities.subject_researchers.names())
        pending = [role for role in roles if role not in reports]
        while pending:
            jobs: list[asyncio.Task[None]] = []
            attempted_role = False
            for role in pending:
                attempt = 1 + max(
                    (
                        record.attempt
                        for record in records
                        if record.research_role == role
                        and record.input_signature == input_signature
                    ),
                    default=0,
                )
                if attempt > self.max_attempts:
                    continue
                attempted_role = True
                request = SubjectResearchRequest(
                    research_role=role,
                    subject=subject,
                    target_level=target_level,
                    requirements=requirements,
                    source_context=source_context,
                    parent_run_id=parent_run_id,
                    attempt=attempt,
                    input_signature=input_signature,
                    input_artifact_ids=input_artifact_ids,
                )

                async def run_role(current: SubjectResearchRequest = request) -> None:
                    created_record: SubjectResearchRunRecord | None = None

                    def on_created(run: WorkflowRun) -> None:
                        nonlocal created_record
                        created_record = SubjectResearchRunRecord(
                            research_role=current.research_role,
                            attempt=current.attempt,
                            run_id=run.id,
                            status=run.status,
                            input_signature=current.input_signature,
                        )
                        records.append(created_record)
                        write_manifest()

                    run, state = await self._execute_role(current, on_run_created=on_created)
                    async with manifest_lock:
                        if created_record is None:
                            raise RuntimeError(
                                "subject research child was not reported at creation"
                            )
                        report_artifact = state.get("report_artifact")
                        report_id = (
                            report_artifact.id if isinstance(report_artifact, ArtifactRef) else None
                        )
                        status = run.status
                        error = run.error
                        if status is RunStatus.SUCCEEDED and report_id is None:
                            status = RunStatus.FAILED
                            error = "subject research child produced no report artifact"
                        replacement = SubjectResearchRunRecord(
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

                jobs.append(asyncio.create_task(run_role()))
            if not attempted_role:
                break
            await asyncio.gather(*jobs)
            reports = self._load_matching_reports(records, input_signature)
            if len(reports) >= self.quorum:
                break
            pending = [role for role in roles if role not in reports]

        if latest_snapshot is None:
            latest_snapshot = write_manifest()
        return SubjectResearchBatchOutcome(
            reports=reports,
            records=records,
            manifest_artifact=latest_snapshot,
            child_run_ids=list(dict.fromkeys(record.run_id for record in records)),
        )

    async def _execute_role(
        self,
        request: SubjectResearchRequest,
        *,
        on_run_created: Callable[[WorkflowRun], None],
    ) -> tuple[WorkflowRun, dict[str, Any]]:
        async def research(state: dict[str, Any]) -> dict[str, Any]:
            request_artifact = self.artifacts.write_json(
                state["run_id"],
                "subject-research-request.json",
                request.model_dump(mode="json"),
                created_by_phase=SUBJECT_RESEARCH_ROLE_RUNNING,
            )
            definition = self.capabilities.subject_researchers.require(request.research_role)
            prompt = self.capabilities.prompts.require(definition.prompt_key)
            report = await complete_with_prompt(
                self.model,
                prompt=prompt,
                user_prompt=json_prompt(
                    research_role=request.research_role,
                    subject=request.subject,
                    target_level=request.target_level,
                    requirements=request.requirements,
                    source_context=request.source_context,
                ),
                response_model=SubjectResearchReport,
                run_id=state["run_id"],
                artifacts=self.artifacts,
                created_by_phase=SUBJECT_RESEARCH_ROLE_RUNNING,
                input_artifact_ids=[request_artifact.id, *request.input_artifact_ids],
            )
            report = report.model_copy(update={"research_role": request.research_role})
            report_artifact = self.artifacts.write_json(
                state["run_id"],
                "subject-research-report.json",
                report.model_dump(mode="json"),
                created_by_phase=SUBJECT_RESEARCH_ROLE_RUNNING,
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
            "subject_research",
            [(SUBJECT_RESEARCH_ROLE_RUNNING, research)],
            parent_run_id=request.parent_run_id,
            on_run_created=on_run_created,
        )

    def _load_matching_reports(
        self,
        records: list[SubjectResearchRunRecord],
        input_signature: str,
    ) -> dict[str, SubjectResearchReport]:
        reports: dict[str, SubjectResearchReport] = {}
        for role in self.capabilities.subject_researchers.names():
            candidates = sorted(
                (
                    record
                    for record in records
                    if record.research_role == role
                    and record.input_signature == input_signature
                    and record.status is RunStatus.SUCCEEDED
                    and record.report_artifact_id is not None
                ),
                key=lambda record: record.attempt,
                reverse=True,
            )
            for record in candidates:
                assert record.report_artifact_id is not None
                try:
                    report = SubjectResearchReport.model_validate(
                        self.artifacts.read_json(record.report_artifact_id)
                    )
                except (KeyError, OSError, ValueError):
                    continue
                reports[role] = report.model_copy(update={"research_role": role})
                break
        return reports

    def _load_records(
        self,
        parent_run_id: UUID,
        restored_records: object,
    ) -> list[SubjectResearchRunRecord]:
        records = (
            list(restored_records)
            if isinstance(restored_records, list)
            and all(isinstance(item, SubjectResearchRunRecord) for item in restored_records)
            else []
        )
        latest = self.artifacts.latest(parent_run_id, "subject-research-runs.json")
        if latest is not None:
            known = {record.run_id for record in records}
            records.extend(
                record
                for record in parse_subject_research_records(self.artifacts.read_json(latest.id))
                if record.run_id not in known
            )
        return [self._refresh_record(record) for record in records]

    def _refresh_record(self, record: SubjectResearchRunRecord) -> SubjectResearchRunRecord:
        run = self.runs.get(record.run_id)
        if run is None or (run.status is record.status and record.report_artifact_id is not None):
            return record
        report = self.artifacts.latest(record.run_id, "subject-research-report.json")
        status = run.status
        error = run.error
        if status is RunStatus.SUCCEEDED and report is None:
            status = RunStatus.FAILED
            error = "succeeded subject research child has no report artifact"
        return SubjectResearchRunRecord(
            **record.model_dump(exclude={"status", "report_artifact_id", "error"}),
            status=status,
            report_artifact_id=report.id if report is not None else record.report_artifact_id,
            error=error,
        )


def parse_subject_research_records(payload: object) -> list[SubjectResearchRunRecord]:
    if not isinstance(payload, list):
        raise ValueError("subject research run manifest is not a list")
    return [SubjectResearchRunRecord.model_validate(item) for item in payload]
