from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from assessment_workbench.compilers import LatexCompileError, LatexCompiler
from assessment_workbench.domain import (
    ArtifactRef,
    DocumentBuildInputSignature,
    DocumentBuildRunRecord,
    ExamDocument,
    ExamView,
    PdfInspectionReport,
    PdfPageInspection,
    RunStatus,
    WorkflowCheckpoint,
    WorkflowRun,
)
from assessment_workbench.exam_quality import exam_bundle_signature
from assessment_workbench.latex_service import ExamLatexService
from assessment_workbench.pdf_inspection import PdfInspector
from assessment_workbench.storage import ArtifactStore, RunStore
from assessment_workbench.workflow import Step, WorkflowEngine

DOCUMENT_RENDERING = "DOCUMENT_RENDERING"
PDF_COMPILING = "PDF_COMPILING"
PDF_INSPECTING = "PDF_INSPECTING"
DOCUMENTS_BUILDING = "DOCUMENTS_BUILDING"

DocumentProgressCallback = Callable[[DocumentBuildRunRecord], None]


class PdfGateError(RuntimeError):
    pass


@dataclass(frozen=True)
class DocumentBatchOutcome:
    records: list[DocumentBuildRunRecord]
    current: list[DocumentBuildRunRecord]
    manifest_artifact: ArtifactRef
    child_run_ids: list[UUID]

    @property
    def succeeded(self) -> bool:
        return len(self.current) == len(ExamView) and all(
            record.status is RunStatus.SUCCEEDED for record in self.current
        )


class DocumentBuildWorkflow:
    def __init__(
        self,
        compiler: LatexCompiler,
        inspector: PdfInspector,
        artifacts: ArtifactStore,
        runs: RunStore,
        *,
        latex: ExamLatexService | None = None,
    ) -> None:
        self.compiler = compiler
        self.inspector = inspector
        self.artifacts = artifacts
        self.runs = runs
        self.latex = latex or ExamLatexService()
        self.engine = WorkflowEngine(runs)

    def input_signature(self, exam: ExamDocument) -> DocumentBuildInputSignature:
        renderer = self.latex.renderer
        return DocumentBuildInputSignature(
            exam_id=exam.id,
            bundle_versions=exam_bundle_signature(exam),
            renderer=renderer.name,
            renderer_version=renderer.template_version,
            compiler=_component_identity(self.compiler),
            inspector=self.inspector.name,
            inspector_version=self.inspector.version,
        )

    async def execute(
        self,
        exam: ExamDocument,
        view: ExamView,
        *,
        attempt: int,
        parent_run_id: UUID,
        input_artifact_ids: list[UUID],
        on_run_created: Callable[[WorkflowRun], None] | None = None,
        on_progress: DocumentProgressCallback | None = None,
    ) -> tuple[WorkflowRun, dict[str, Any]]:
        signature = self.input_signature(exam)
        return await self._run(
            exam,
            view,
            signature,
            attempt=attempt,
            parent_run_id=parent_run_id,
            input_artifact_ids=input_artifact_ids,
            on_run_created=on_run_created,
            on_progress=on_progress,
        )

    async def resume(
        self,
        run_id: UUID,
        exam: ExamDocument,
        view: ExamView,
        *,
        attempt: int,
        parent_run_id: UUID,
        input_artifact_ids: list[UUID],
        on_progress: DocumentProgressCallback | None = None,
    ) -> tuple[WorkflowRun, dict[str, Any]]:
        checkpoint = self.runs.get_checkpoint(run_id)
        if checkpoint is None:
            raise ValueError(f"document build run has no checkpoint: {run_id}")
        signature = self.input_signature(exam)
        restored = self._restore(checkpoint)
        return await self._run(
            exam,
            view,
            signature,
            attempt=attempt,
            parent_run_id=parent_run_id,
            input_artifact_ids=input_artifact_ids,
            resume_run_id=run_id,
            restored_state=restored,
            on_progress=on_progress,
        )

    async def _run(
        self,
        exam: ExamDocument,
        view: ExamView,
        signature: DocumentBuildInputSignature,
        *,
        attempt: int,
        parent_run_id: UUID,
        input_artifact_ids: list[UUID],
        resume_run_id: UUID | None = None,
        restored_state: dict[str, Any] | None = None,
        on_run_created: Callable[[WorkflowRun], None] | None = None,
        on_progress: DocumentProgressCallback | None = None,
    ) -> tuple[WorkflowRun, dict[str, Any]]:
        signature_sha256 = document_signature_sha256(signature, view)
        job_name = f"exam-{view.value}"

        def report_progress(state: dict[str, Any], *, status: RunStatus) -> None:
            if on_progress is None:
                return
            on_progress(
                _record_from_state(
                    state,
                    view=view,
                    attempt=attempt,
                    signature=signature,
                    signature_sha256=signature_sha256,
                    status=status,
                )
            )

        async def render(state: dict[str, Any]) -> dict[str, Any]:
            document = self.latex.render(exam, view)
            source_artifact = self.artifacts.write_bytes(
                state["run_id"],
                f"{job_name}.tex",
                document.source.encode("utf-8"),
                media_type="application/x-tex",
                created_by_phase=DOCUMENT_RENDERING,
            )
            updates = {
                "source": document.source,
                "source_artifact": source_artifact,
                "output_artifact_ids": [source_artifact.id],
                "_checkpoint_artifacts": {"source": source_artifact.id},
            }
            state.update(updates)
            report_progress(state, status=RunStatus.RUNNING)
            return updates

        async def compile_pdf(state: dict[str, Any]) -> dict[str, Any]:
            source = str(state["source"])
            try:
                result = await asyncio.to_thread(
                    self.compiler.compile,
                    source,
                    job_name=job_name,
                )
            except LatexCompileError as exc:
                log_content = exc.log or str(exc)
                log_artifact = self.artifacts.write_bytes(
                    state["run_id"],
                    f"{job_name}.tectonic.log",
                    log_content.encode("utf-8"),
                    media_type="text/plain",
                    created_by_phase=PDF_COMPILING,
                )
                state["log_artifact"] = log_artifact
                state["output_artifact_ids"] = [
                    _artifact(state, "source_artifact").id,
                    log_artifact.id,
                ]
                report_progress(state, status=RunStatus.RUNNING)
                raise

            log_artifact = self.artifacts.write_bytes(
                state["run_id"],
                f"{job_name}.tectonic.log",
                result.log.encode("utf-8"),
                media_type="text/plain",
                created_by_phase=PDF_COMPILING,
            )
            state["log_artifact"] = log_artifact
            report_progress(state, status=RunStatus.RUNNING)
            pdf_artifact = self.artifacts.write_bytes(
                state["run_id"],
                f"{job_name}.pdf",
                result.pdf,
                media_type="application/pdf",
                created_by_phase=PDF_COMPILING,
            )
            updates = {
                "pdf": result.pdf,
                "pdf_artifact": pdf_artifact,
                "log_artifact": log_artifact,
                "output_artifact_ids": [
                    _artifact(state, "source_artifact").id,
                    log_artifact.id,
                    pdf_artifact.id,
                ],
                "_checkpoint_artifacts": {
                    "pdf": pdf_artifact.id,
                    "log": log_artifact.id,
                },
            }
            state.update(updates)
            report_progress(state, status=RunStatus.RUNNING)
            return updates

        async def inspect_pdf(state: dict[str, Any]) -> dict[str, Any]:
            pdf_artifact = _artifact(state, "pdf_artifact")
            result = await asyncio.to_thread(
                self.inspector.inspect,
                bytes(state["pdf"]),
                exam=exam,
                view=view,
                job_name=job_name,
            )
            page_artifacts: list[ArtifactRef] = []
            page_records: list[PdfPageInspection] = []
            for page in result.pages:
                artifact = self.artifacts.write_bytes(
                    state["run_id"],
                    f"{job_name}.page-{page.page_number:03d}.png",
                    page.png,
                    media_type="image/png",
                    created_by_phase=PDF_INSPECTING,
                )
                page_artifacts.append(artifact)
                page_records.append(
                    PdfPageInspection(
                        page_number=page.page_number,
                        width_points=page.width_points,
                        height_points=page.height_points,
                        text_characters=page.text_characters,
                        ink_ratio=page.ink_ratio,
                        edge_ink_ratio=page.edge_ink_ratio,
                        image_artifact_id=artifact.id,
                    )
                )
            report = PdfInspectionReport(
                view=view,
                pdf_artifact_id=pdf_artifact.id,
                page_count=result.page_count,
                extracted_text_sha256=result.extracted_text_sha256,
                pages=page_records,
                blocking_findings=list(result.blocking_findings),
                warnings=list(result.warnings),
                manual_checks_required=list(result.manual_checks_required),
                passed=not result.blocking_findings,
            )
            inspection_artifact = self.artifacts.write_json(
                state["run_id"],
                f"{job_name}.inspection.json",
                report.model_dump(mode="json"),
                created_by_phase=PDF_INSPECTING,
            )
            updates = {
                "page_artifacts": page_artifacts,
                "inspection": report,
                "inspection_artifact": inspection_artifact,
                "output_artifact_ids": [
                    _artifact(state, "source_artifact").id,
                    _artifact(state, "log_artifact").id,
                    pdf_artifact.id,
                    *(artifact.id for artifact in page_artifacts),
                    inspection_artifact.id,
                ],
                "_checkpoint_artifacts": {"inspection": inspection_artifact.id},
            }
            state.update(updates)
            report_progress(state, status=RunStatus.RUNNING)
            if report.blocking_findings:
                raise PdfGateError("; ".join(report.blocking_findings))
            return updates

        steps: list[tuple[str, Step]] = [
            (DOCUMENT_RENDERING, render),
            (PDF_COMPILING, compile_pdf),
            (PDF_INSPECTING, inspect_pdf),
        ]
        context = dict(restored_state or {})
        context["input_artifact_ids"] = list(input_artifact_ids)
        if resume_run_id is not None:
            return await self.engine.resume(
                resume_run_id,
                "exam_document_build",
                steps,
                context=context,
                parent_run_id=parent_run_id,
            )
        return await self.engine.execute(
            "exam_document_build",
            steps,
            context=context,
            parent_run_id=parent_run_id,
            on_run_created=on_run_created,
        )

    def _restore(self, checkpoint: WorkflowCheckpoint) -> dict[str, Any]:
        state: dict[str, Any] = {
            "_checkpoint_artifacts": dict(checkpoint.artifact_bindings),
            "_checkpoint_child_run_ids": list(checkpoint.child_run_ids),
        }
        source_id = checkpoint.artifact_bindings.get("source")
        if source_id is not None:
            source_artifact = self.artifacts.get(source_id)
            if source_artifact is None:
                raise ValueError("document source artifact metadata is missing")
            state["source_artifact"] = source_artifact
            state["source"] = self.artifacts.read_bytes(source_id).decode("utf-8")
        pdf_id = checkpoint.artifact_bindings.get("pdf")
        if pdf_id is not None:
            pdf_artifact = self.artifacts.get(pdf_id)
            if pdf_artifact is None:
                raise ValueError("document PDF artifact metadata is missing")
            state["pdf_artifact"] = pdf_artifact
            state["pdf"] = self.artifacts.read_bytes(pdf_id)
        log_id = checkpoint.artifact_bindings.get("log")
        if log_id is not None:
            log_artifact = self.artifacts.get(log_id)
            if log_artifact is None:
                raise ValueError("document log artifact metadata is missing")
            self.artifacts.read_bytes(log_id)
            state["log_artifact"] = log_artifact
        return state


class DocumentBatchWorkflow:
    def __init__(
        self,
        documents: DocumentBuildWorkflow,
        artifacts: ArtifactStore,
        runs: RunStore,
    ) -> None:
        self.documents = documents
        self.artifacts = artifacts
        self.runs = runs

    async def execute(
        self,
        parent_run_id: UUID,
        exam: ExamDocument,
        restored_records: object,
        *,
        input_artifact_ids: list[UUID],
    ) -> DocumentBatchOutcome:
        records = parse_document_build_records(restored_records)
        signature = self.documents.input_signature(exam)
        latest_snapshot: ArtifactRef | None = None

        def write_manifest() -> ArtifactRef:
            nonlocal latest_snapshot
            payload = [record.model_dump(mode="json") for record in records]
            self.artifacts.write_editable_json(parent_run_id, "document-build-runs.json", payload)
            latest_snapshot = self.artifacts.write_json(
                parent_run_id,
                "document-build-runs.json",
                payload,
                created_by_phase=DOCUMENTS_BUILDING,
            )
            return latest_snapshot

        def replace_record(updated: DocumentBuildRunRecord) -> None:
            for index, record in enumerate(records):
                if record.run_id == updated.run_id:
                    records[index] = updated
                    write_manifest()
                    return
            records.append(updated)
            write_manifest()

        reusable = {
            view: _latest_reusable_record(records, view, signature, self.artifacts, self.runs)
            for view in ExamView
        }
        jobs: list[asyncio.Task[None]] = []
        for view in ExamView:
            if reusable[view] is not None:
                continue
            previous = _latest_record(records, view, signature)
            attempt = 1 + max(
                (record.attempt for record in records if record.view is view),
                default=0,
            )

            async def run_view(
                current_view: ExamView = view,
                current_previous: DocumentBuildRunRecord | None = previous,
                current_attempt: int = attempt,
            ) -> None:
                created: DocumentBuildRunRecord | None = None

                def on_created(run: WorkflowRun) -> None:
                    nonlocal created
                    created = DocumentBuildRunRecord(
                        view=current_view,
                        attempt=current_attempt,
                        input_signature=signature,
                        input_signature_sha256=document_signature_sha256(signature, current_view),
                        run_id=run.id,
                        status=run.status,
                    )
                    records.append(created)
                    write_manifest()

                try:
                    previous_run = (
                        self.runs.get(current_previous.run_id)
                        if current_previous is not None
                        else None
                    )
                    if previous_run is not None and previous_run.status is RunStatus.INTERRUPTED:
                        assert current_previous is not None
                        created = current_previous
                        run, state = await self.documents.resume(
                            current_previous.run_id,
                            exam,
                            current_view,
                            attempt=current_previous.attempt,
                            parent_run_id=parent_run_id,
                            input_artifact_ids=input_artifact_ids,
                            on_progress=replace_record,
                        )
                    else:
                        run, state = await self.documents.execute(
                            exam,
                            current_view,
                            attempt=current_attempt,
                            parent_run_id=parent_run_id,
                            input_artifact_ids=input_artifact_ids,
                            on_run_created=on_created,
                            on_progress=replace_record,
                        )
                    if created is None:
                        raise RuntimeError("document child run was not reported at creation")
                    replace_record(
                        _record_from_state(
                            state,
                            view=current_view,
                            attempt=created.attempt,
                            signature=signature,
                            signature_sha256=document_signature_sha256(signature, current_view),
                            status=run.status,
                            error=run.error,
                        )
                    )
                except Exception as exc:
                    if created is not None:
                        replace_record(
                            created.model_copy(
                                update={"status": RunStatus.FAILED, "error": str(exc)}
                            )
                        )

            jobs.append(asyncio.create_task(run_view()))
        if jobs:
            await asyncio.gather(*jobs)
        if latest_snapshot is None:
            latest_snapshot = write_manifest()
        current = [
            _latest_reusable_record(records, view, signature, self.artifacts, self.runs)
            or _latest_record(records, view, signature)
            for view in ExamView
        ]
        return DocumentBatchOutcome(
            records=records,
            current=[record for record in current if record is not None],
            manifest_artifact=latest_snapshot,
            child_run_ids=list(dict.fromkeys(record.run_id for record in records)),
        )


def document_signature_sha256(
    signature: DocumentBuildInputSignature,
    view: ExamView,
) -> str:
    payload = json.dumps(
        {"view": view.value, "signature": signature.model_dump(mode="json")},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def parse_document_build_records(payload: object) -> list[DocumentBuildRunRecord]:
    if payload is None:
        return []
    if not isinstance(payload, list):
        raise ValueError("document build run manifest is not a list")
    return [DocumentBuildRunRecord.model_validate(item) for item in payload]


def latest_document_builds(
    records: list[DocumentBuildRunRecord],
) -> list[DocumentBuildRunRecord]:
    latest: dict[ExamView, DocumentBuildRunRecord] = {}
    for record in records:
        current = latest.get(record.view)
        if current is None or record.attempt > current.attempt:
            latest[record.view] = record
    return [latest[view] for view in ExamView if view in latest]


def successful_document_builds(value: object) -> bool:
    return (
        isinstance(value, list)
        and len(value) == len(ExamView)
        and all(
            isinstance(record, DocumentBuildRunRecord) and record.status is RunStatus.SUCCEEDED
            for record in value
        )
    )


def document_artifact_ids(record: DocumentBuildRunRecord) -> list[UUID]:
    return [
        artifact_id
        for artifact_id in (
            record.source_artifact_id,
            record.log_artifact_id,
            record.pdf_artifact_id,
            *record.page_artifact_ids,
            record.inspection_artifact_id,
        )
        if artifact_id is not None
    ]


def _record_from_state(
    state: dict[str, Any],
    *,
    view: ExamView,
    attempt: int,
    signature: DocumentBuildInputSignature,
    signature_sha256: str,
    status: RunStatus,
    error: str | None = None,
) -> DocumentBuildRunRecord:
    source = state.get("source_artifact")
    pdf = state.get("pdf_artifact")
    log = state.get("log_artifact")
    inspection = state.get("inspection_artifact")
    pages = state.get("page_artifacts", [])
    return DocumentBuildRunRecord(
        view=view,
        attempt=attempt,
        input_signature=signature,
        input_signature_sha256=signature_sha256,
        run_id=UUID(str(state["run_id"])),
        status=status,
        source_artifact_id=source.id if isinstance(source, ArtifactRef) else None,
        pdf_artifact_id=pdf.id if isinstance(pdf, ArtifactRef) else None,
        log_artifact_id=log.id if isinstance(log, ArtifactRef) else None,
        inspection_artifact_id=inspection.id if isinstance(inspection, ArtifactRef) else None,
        page_artifact_ids=[item.id for item in pages if isinstance(item, ArtifactRef)],
        error=error,
    )


def _latest_record(
    records: list[DocumentBuildRunRecord],
    view: ExamView,
    signature: DocumentBuildInputSignature,
) -> DocumentBuildRunRecord | None:
    expected = document_signature_sha256(signature, view)
    candidates = [
        record
        for record in records
        if record.view is view and record.input_signature_sha256 == expected
    ]
    return max(candidates, key=lambda item: item.attempt, default=None)


def _latest_reusable_record(
    records: list[DocumentBuildRunRecord],
    view: ExamView,
    signature: DocumentBuildInputSignature,
    artifacts: ArtifactStore,
    runs: RunStore,
) -> DocumentBuildRunRecord | None:
    expected = document_signature_sha256(signature, view)
    candidates = sorted(
        (
            record
            for record in records
            if record.view is view
            and record.input_signature_sha256 == expected
            and record.status is RunStatus.SUCCEEDED
        ),
        key=lambda item: item.attempt,
        reverse=True,
    )
    for record in candidates:
        run = runs.get(record.run_id)
        if run is None or run.status is not RunStatus.SUCCEEDED:
            continue
        artifact_ids = [
            record.source_artifact_id,
            record.pdf_artifact_id,
            record.log_artifact_id,
            record.inspection_artifact_id,
            *record.page_artifact_ids,
        ]
        if any(artifact_id is None for artifact_id in artifact_ids):
            continue
        try:
            for artifact_id in artifact_ids:
                assert artifact_id is not None
                artifacts.read_bytes(artifact_id)
            assert record.inspection_artifact_id is not None
            report = PdfInspectionReport.model_validate(
                artifacts.read_json(record.inspection_artifact_id)
            )
        except (KeyError, OSError, ValueError):
            continue
        if (
            report.passed
            and report.view is view
            and report.pdf_artifact_id == record.pdf_artifact_id
        ):
            return record
    return None


def _component_identity(component: object) -> str:
    component_type = type(component)
    name = getattr(component, "name", component_type.__qualname__)
    version = getattr(component, "version", "unversioned")
    executable = getattr(component, "executable", None)
    suffix = f":{executable}" if isinstance(executable, str) else ""
    return f"{component_type.__module__}.{name}:{version}{suffix}"


def _artifact(state: dict[str, Any], key: str) -> ArtifactRef:
    value = state.get(key)
    if not isinstance(value, ArtifactRef):
        raise RuntimeError(f"document workflow state is missing artifact: {key}")
    return value
