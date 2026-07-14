from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from assessment_workbench.config import Settings
from assessment_workbench.domain import HumanDecisionType
from assessment_workbench.storage import Workspace
from assessment_workbench.web_models import (
    ApiErrorResponse,
    ArtifactContent,
    BackgroundRunResponse,
    EditableQuestion,
    ExamCreateRequest,
    QuestionEditRequest,
    QuestionPublishRequest,
    QuestionRerunRequest,
    RunActionRequest,
    RunDetail,
    RunSnapshot,
    RunSummary,
    WorkspaceInfo,
)
from assessment_workbench.workbench_service import (
    WorkbenchApplicationService,
    WorkbenchServiceError,
)


def create_gui_app(workspace: Workspace, settings: Settings) -> FastAPI:
    service = WorkbenchApplicationService(workspace, settings)
    app = FastAPI(title="Assessment Workbench GUI", version="0.1.0")
    app.state.workbench = service

    @app.exception_handler(WorkbenchServiceError)
    async def handle_service_error(_: Request, exc: WorkbenchServiceError) -> JSONResponse:
        payload = ApiErrorResponse(
            code=exc.code,
            detail=exc.detail,
            fields=exc.fields,
        )
        return JSONResponse(status_code=exc.status_code, content=payload.model_dump(mode="json"))

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        fields: dict[str, str] = {}
        for error in exc.errors():
            path = [
                str(part)
                for part in error["loc"]
                if part not in {"body", "path", "query"} and not isinstance(part, int)
            ]
            fields[".".join(path) or "_request"] = error["msg"]
        payload = ApiErrorResponse(
            code="invalid_request",
            detail="request validation failed",
            fields=fields,
        )
        return JSONResponse(status_code=422, content=payload.model_dump(mode="json"))

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "assessment-workbench-gui"}

    @app.get("/api/workspace", response_model=WorkspaceInfo)
    def workspace_info() -> WorkspaceInfo:
        return service.workspace_info()

    @app.get("/api/runs", response_model=list[RunSummary])
    def list_runs() -> list[RunSummary]:
        return service.list_runs()

    @app.get("/api/runs/{run_id}", response_model=RunDetail)
    def run_detail(run_id: UUID) -> RunDetail:
        return service.run_detail(run_id)

    @app.get("/api/runs/{run_id}/snapshot", response_model=RunSnapshot)
    def run_snapshot(run_id: UUID) -> RunSnapshot:
        return _snapshot(service, run_id)

    @app.get("/api/runs/{run_id}/stream")
    async def run_stream(run_id: UUID, request: Request) -> StreamingResponse:
        initial = await asyncio.to_thread(_snapshot, service, run_id)

        async def events() -> AsyncIterator[str]:
            previous = ""
            snapshot = initial
            while not await request.is_disconnected():
                data = snapshot.model_dump_json()
                digest = hashlib.sha256(data.encode()).hexdigest()
                if digest != previous:
                    yield f"event: snapshot\ndata: {data}\n\n"
                    previous = digest
                await asyncio.sleep(1.25)
                snapshot = await asyncio.to_thread(_snapshot, service, run_id)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/exams", response_model=BackgroundRunResponse, status_code=202)
    async def create_exam(payload: ExamCreateRequest) -> BackgroundRunResponse:
        return BackgroundRunResponse(run=await service.launch_exam(payload))

    @app.post("/api/runs/{run_id}/resume", response_model=BackgroundRunResponse, status_code=202)
    async def resume_run(run_id: UUID) -> BackgroundRunResponse:
        return BackgroundRunResponse(run=await service.launch_resume(run_id))

    @app.post("/api/runs/{run_id}/approve", response_model=BackgroundRunResponse)
    def approve_run(run_id: UUID, payload: RunActionRequest) -> BackgroundRunResponse:
        return BackgroundRunResponse(
            run=service.resolve_human(
                run_id,
                HumanDecisionType.ACCEPT,
                actor=payload.actor,
                reason=payload.reason,
            )
        )

    @app.post("/api/runs/{run_id}/reject", response_model=BackgroundRunResponse)
    def reject_run(run_id: UUID, payload: RunActionRequest) -> BackgroundRunResponse:
        return BackgroundRunResponse(
            run=service.resolve_human(
                run_id,
                HumanDecisionType.REJECT,
                actor=payload.actor,
                reason=payload.reason,
            )
        )

    @app.post("/api/runs/{run_id}/retry", response_model=BackgroundRunResponse)
    def retry_run(run_id: UUID, payload: RunActionRequest) -> BackgroundRunResponse:
        return BackgroundRunResponse(
            run=service.resolve_human(
                run_id,
                HumanDecisionType.RETRY,
                actor=payload.actor,
                reason=payload.reason,
            )
        )

    @app.post("/api/runs/{run_id}/abort", response_model=BackgroundRunResponse)
    def abort_run(run_id: UUID, payload: RunActionRequest) -> BackgroundRunResponse:
        return BackgroundRunResponse(
            run=service.resolve_human(
                run_id,
                HumanDecisionType.ABORT,
                actor=payload.actor,
                reason=payload.reason,
            )
        )

    @app.post("/api/runs/{run_id}/cancel", response_model=BackgroundRunResponse)
    def cancel_run(run_id: UUID) -> BackgroundRunResponse:
        return BackgroundRunResponse(run=service.request_cancel(run_id))

    @app.get("/api/exams/{parent_run_id}/research")
    def research_status(parent_run_id: UUID) -> list[dict[str, object]]:
        return service.research_status(parent_run_id)

    @app.get("/api/exams/{parent_run_id}/questions")
    def question_status(parent_run_id: UUID) -> list[dict[str, object]]:
        return service.question_status(parent_run_id)

    @app.get(
        "/api/exams/{parent_run_id}/questions/{number}",
        response_model=EditableQuestion,
    )
    def editable_question(parent_run_id: UUID, number: int) -> EditableQuestion:
        return service.editable_question(parent_run_id, number)

    @app.put(
        "/api/exams/{parent_run_id}/questions/{number}",
        response_model=EditableQuestion,
    )
    def save_editable_question(
        parent_run_id: UUID,
        number: int,
        payload: QuestionEditRequest,
    ) -> EditableQuestion:
        return service.save_editable_question(
            parent_run_id,
            number,
            expected_sha256=payload.expected_sha256,
            payload=payload.bundle,
        )

    @app.post(
        "/api/exams/{parent_run_id}/questions/{number}/rerun",
        response_model=BackgroundRunResponse,
        status_code=202,
    )
    async def rerun_question(
        parent_run_id: UUID,
        number: int,
        payload: QuestionRerunRequest,
    ) -> BackgroundRunResponse:
        return BackgroundRunResponse(
            run=await service.launch_question_rerun(parent_run_id, number, payload.feedback)
        )

    @app.post(
        "/api/exams/{parent_run_id}/questions/{number}/publish",
        response_model=EditableQuestion,
    )
    def publish_question(
        parent_run_id: UUID,
        number: int,
        payload: QuestionPublishRequest,
    ) -> EditableQuestion:
        return service.publish_question_run(parent_run_id, number, payload.child_run_id)

    @app.post(
        "/api/exams/{parent_run_id}/assemble-edited",
        response_model=BackgroundRunResponse,
        status_code=202,
    )
    async def assemble_edited(
        parent_run_id: UUID, human_gates: bool = False
    ) -> BackgroundRunResponse:
        return BackgroundRunResponse(
            run=await service.launch_edited_assembly(parent_run_id, human_gates=human_gates)
        )

    @app.get("/api/exams/{parent_run_id}/documents")
    def document_status(parent_run_id: UUID) -> list[dict[str, object]]:
        return service.document_status(parent_run_id)

    @app.get("/api/artifacts/{artifact_id}", response_model=ArtifactContent)
    def artifact_content(artifact_id: UUID) -> ArtifactContent:
        return service.artifact_content(artifact_id)

    @app.get("/api/artifacts/{artifact_id}/download")
    def artifact_download(artifact_id: UUID) -> Response:
        artifact, content = service.artifact_bytes(artifact_id)
        filename = Path(artifact.path).name
        return Response(
            content=content,
            media_type=artifact.media_type,
            headers={"Content-Disposition": f'inline; filename="{filename}"'},
        )

    static_dir = Path(__file__).with_name("web_static")
    if static_dir.is_dir():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="gui")
    else:

        @app.get("/", response_class=HTMLResponse)
        async def missing_frontend() -> str:
            return (
                "<main style='font-family:system-ui;padding:2rem'>"
                "<h1>GUI assets are not built</h1>"
                "<p>Run <code>npm --prefix frontend run build</code>.</p>"
                "</main>"
            )

    return app


def _snapshot(service: WorkbenchApplicationService, run_id: UUID) -> RunSnapshot:
    detail = service.run_detail(run_id)
    root_run_id = detail.parent_run_id or run_id
    return RunSnapshot(
        detail=detail,
        research=service.research_status(root_run_id),
        questions=service.question_status(root_run_id),
        documents=service.document_status(root_run_id),
    )
