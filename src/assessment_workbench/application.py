from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

from assessment_workbench.agents import ExamAgentWorkflow, ModelRouter
from assessment_workbench.compilers import TectonicCompiler
from assessment_workbench.config import Settings
from assessment_workbench.document_workflow import DocumentBatchWorkflow, DocumentBuildWorkflow
from assessment_workbench.domain import ArtifactRef, ExamQuestionBundle, QuestionPlan, WorkflowRun
from assessment_workbench.edited_exam_workflow import EditedExamAssemblyWorkflow
from assessment_workbench.models import OpenAICompatibleModel
from assessment_workbench.pdf_inspection import PopplerPdfInspector
from assessment_workbench.storage import (
    ArtifactStore,
    LocalKnowledgeBackend,
    RunStore,
    Workspace,
)


def open_workspace(path: Path | None, settings: Settings | None = None) -> Workspace:
    resolved_settings = settings or Settings()
    workspace = Workspace(path or resolved_settings.workspace)
    workspace.require_initialized()
    RunStore(workspace).recover_orphaned()
    return workspace


def build_exam_workflow(
    workspace: Workspace,
    settings: Settings,
    *,
    compile_pdf: bool,
) -> ExamAgentWorkflow:
    audit_store = LocalKnowledgeBackend(workspace)
    standard = OpenAICompatibleModel(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        audit_store=audit_store,
        timeout=settings.http_timeout,
        max_concurrency=settings.llm_request_concurrency,
    )
    strong = standard
    if settings.llm_strong_model != settings.llm_model:
        strong = OpenAICompatibleModel(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            model=settings.llm_strong_model,
            audit_store=audit_store,
            timeout=settings.http_timeout,
            max_concurrency=settings.llm_request_concurrency,
        )
    compiler = None
    inspector = None
    if compile_pdf:
        compiler = TectonicCompiler(
            settings.tectonic_command,
            timeout_seconds=settings.tectonic_timeout,
        )
        inspector = PopplerPdfInspector(
            pdfinfo_command=settings.pdfinfo_command,
            pdftotext_command=settings.pdftotext_command,
            pdftoppm_command=settings.pdftoppm_command,
            timeout_seconds=settings.pdf_inspection_timeout,
            raster_dpi=settings.pdf_raster_dpi,
        )
    return ExamAgentWorkflow(
        ModelRouter(standard=standard, strong=strong),
        ArtifactStore(workspace),
        RunStore(workspace),
        max_exam_reviewer_attempts=settings.exam_reviewer_attempts,
        max_exam_review_rounds=settings.exam_review_rounds,
        max_parallel_questions=settings.exam_question_concurrency,
        compiler=compiler,
        pdf_inspector=inspector,
    )


def build_edited_exam_workflow(
    workspace: Workspace,
    settings: Settings,
) -> EditedExamAssemblyWorkflow:
    artifacts = ArtifactStore(workspace)
    runs = RunStore(workspace)
    compiler = TectonicCompiler(
        settings.tectonic_command,
        timeout_seconds=settings.tectonic_timeout,
    )
    inspector = PopplerPdfInspector(
        pdfinfo_command=settings.pdfinfo_command,
        pdftotext_command=settings.pdftotext_command,
        pdftoppm_command=settings.pdftoppm_command,
        timeout_seconds=settings.pdf_inspection_timeout,
        raster_dpi=settings.pdf_raster_dpi,
    )
    documents = DocumentBatchWorkflow(
        DocumentBuildWorkflow(compiler, inspector, artifacts, runs),
        artifacts,
        runs,
    )
    return EditedExamAssemblyWorkflow(documents, artifacts, runs)


def latest_artifact_json(
    artifacts: ArtifactStore,
    run_id: UUID,
    logical_name: str,
) -> Any:
    latest = artifacts.latest(run_id, logical_name)
    if latest is None:
        raise KeyError(f"artifact not found for run {run_id}: {logical_name}")
    return artifacts.read_json(latest.id)


def publish_question_bundle(
    workspace: Workspace,
    *,
    parent_run_id: UUID,
    plan: QuestionPlan,
    child_run: WorkflowRun,
    bundle: ExamQuestionBundle,
    bundle_artifact: ArtifactRef,
) -> Path:
    if bundle.question.number != plan.number:
        raise ValueError("question bundle number does not match its plan")
    artifacts = ArtifactStore(workspace)
    live_manifest = workspace.root / "editable" / str(parent_run_id) / "question-runs.json"
    if live_manifest.is_file():
        records = artifacts.read_editable_json(parent_run_id, "question-runs.json")
    else:
        try:
            records = latest_artifact_json(artifacts, parent_run_id, "question-runs.json")
        except KeyError:
            records = []
    if not isinstance(records, list):
        raise RuntimeError("question-runs artifact is not a list")
    previous = next(
        (
            record
            for record in records
            if isinstance(record, dict) and record.get("question_number") == plan.number
        ),
        None,
    )
    history = [
        item
        for item in (previous.get("replacement_history", []) if previous else [])
        if isinstance(item, dict) and item.get("run_id") != str(child_run.id)
    ]
    if previous and previous.get("run_id") not in {None, str(child_run.id)}:
        previous_run_id = previous.get("run_id")
        history = [item for item in history if item.get("run_id") != previous_run_id]
        history.append(
            {
                "exam_round": previous.get("exam_round", 0),
                "run_id": previous_run_id,
                "bundle_artifact_id": previous.get("bundle_artifact_id"),
                "status": previous.get("status"),
            }
        )
    editable_path = artifacts.write_editable_json(
        parent_run_id,
        f"questions/{plan.number:02d}.json",
        bundle.model_dump(mode="json"),
    )
    records = [
        record
        for record in records
        if not isinstance(record, dict) or record.get("question_number") != plan.number
    ]
    records.append(
        {
            "question_number": plan.number,
            "plan_id": plan.id,
            "run_id": str(child_run.id),
            "status": child_run.status,
            "error": child_run.error,
            "bundle_artifact_id": str(bundle_artifact.id),
            "bundle_path": str(bundle_artifact.path),
            "editable_path": str(editable_path),
            "requires_human_review": False,
            "exam_round": previous.get("exam_round", 0) if previous else 0,
            "replacement_history": history,
        }
    )
    records.sort(
        key=lambda record: int(record.get("question_number", 0)) if isinstance(record, dict) else 0
    )
    artifacts.write_editable_json(parent_run_id, "question-runs.json", records)
    artifacts.write_json(
        parent_run_id,
        "question-runs.json",
        records,
        created_by_phase="QUESTION_REGENERATING",
    )
    return editable_path
