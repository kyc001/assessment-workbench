import asyncio
import json
import sys
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

import typer

from assessment_workbench.agents import ExamAgentWorkflow, ModelRouter
from assessment_workbench.compilers import TectonicCompiler
from assessment_workbench.config import Settings
from assessment_workbench.document_workflow import (
    DocumentBatchWorkflow,
    DocumentBuildWorkflow,
    latest_document_builds,
)
from assessment_workbench.domain import (
    ArtifactRef,
    DocumentBuildRunRecord,
    ExamBlueprint,
    ExamDocument,
    ExamQuestionBundle,
    HumanDecision,
    HumanDecisionType,
    MaterialKind,
    QuestionPlan,
    QuestionType,
    RunStatus,
    SubjectProfile,
)
from assessment_workbench.edited_exam_workflow import EditedExamAssemblyWorkflow
from assessment_workbench.ingestion import MaterialIngestionWorkflow
from assessment_workbench.models import OpenAICompatibleModel
from assessment_workbench.parsers import FixtureParser, MinerUApiParser, MinerUCliParser
from assessment_workbench.pdf_inspection import PopplerPdfInspector
from assessment_workbench.planning import QuestionSpecWorkflow
from assessment_workbench.ports import DocumentParser
from assessment_workbench.profiles import load_exam_blueprint, load_subject_profile
from assessment_workbench.storage import (
    ArtifactStore,
    LocalKnowledgeBackend,
    MaterialStore,
    RunStore,
    Workspace,
)

app = typer.Typer(help="Course-grounded assessment workflows")
workspace_app = typer.Typer(help="Initialize and inspect workspaces")
materials_app = typer.Typer(help="Ingest course materials")
topics_app = typer.Typer(help="Browse course knowledge points")
runs_app = typer.Typer(help="Inspect workflow runs")
knowledge_app = typer.Typer(help="Search course knowledge")
questions_app = typer.Typer(help="Plan and generate questions")
exams_app = typer.Typer(help="Plan, generate, and export exams")
app.add_typer(workspace_app, name="workspace")
app.add_typer(materials_app, name="materials")
app.add_typer(topics_app, name="topics")
app.add_typer(runs_app, name="runs")
app.add_typer(knowledge_app, name="knowledge")
app.add_typer(questions_app, name="questions")
app.add_typer(exams_app, name="exams")


def _workspace(path: Path | None) -> Workspace:
    root = path or Settings().workspace
    workspace = Workspace(root)
    workspace.require_initialized()
    RunStore(workspace).recover_orphaned()
    return workspace


def _console_safe(value: object, *, err: bool = False) -> str:
    text = str(value)
    stream = sys.stderr if err else sys.stdout
    encoding = getattr(stream, "encoding", None) or "utf-8"
    return text.encode(encoding, errors="replace").decode(encoding)


def _exam_workflow(
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


def _edited_exam_workflow(
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


def _latest_artifact_json(
    artifacts: ArtifactStore,
    run_id: UUID,
    logical_name: str,
) -> Any:
    latest = artifacts.latest(run_id, logical_name)
    if latest is None:
        raise KeyError(f"artifact not found for run {run_id}: {logical_name}")
    return artifacts.read_json(latest.id)


def _artifact_display_path(
    workspace: Workspace,
    artifacts: ArtifactStore,
    artifact_id: UUID | None,
) -> str:
    if artifact_id is None:
        return "-"
    artifact = artifacts.get(artifact_id)
    return str(workspace.root / artifact.path) if artifact is not None else "<missing>"


def _echo_human_review(run_id: UUID, store: RunStore) -> None:
    request = store.pending_human_review(run_id)
    if request is None:
        return
    typer.echo(f"Review: {request.prompt}")
    typer.echo(f"Approve: assessment-workbench runs approve {run_id}")
    typer.echo(f"Retry: assessment-workbench runs retry {run_id}")
    typer.echo(f"Continue after decision: assessment-workbench runs resume {run_id}")


def _raise_if_interrupted(run_id: UUID, status: RunStatus, error: str | None) -> None:
    if status is not RunStatus.INTERRUPTED:
        return
    typer.echo(f"Retryable interruption: {error or 'workflow interrupted'}", err=True)
    typer.echo(f"Resume: assessment-workbench runs resume {run_id}", err=True)
    raise typer.Exit(1)


@workspace_app.command("init")
def initialize_workspace(path: Path) -> None:
    workspace = Workspace(path)
    workspace.initialize()
    typer.echo(f"Initialized workspace: {workspace.root}")


@materials_app.command("ingest")
def ingest_material(
    source: Path,
    course: Annotated[str, typer.Option(help="Stable course identifier")],
    kind: Annotated[MaterialKind, typer.Option(case_sensitive=False)],
    parser: Annotated[str, typer.Option(help="fixture, mineru-cli, or mineru-api")] = "fixture",
    semantic: Annotated[
        bool, typer.Option(help="Extract semantic course entities with the configured LLM")
    ] = False,
    semester: Annotated[str | None, typer.Option()] = None,
    year: Annotated[int | None, typer.Option(min=1900, max=9999)] = None,
    language: Annotated[str, typer.Option()] = "zh-CN",
    workspace_path: Annotated[Path | None, typer.Option("--workspace")] = None,
) -> None:
    workspace = _workspace(workspace_path)
    if not source.is_file():
        raise typer.BadParameter(f"source file does not exist: {source}")
    settings = Settings()
    document_parser: DocumentParser
    if parser == "fixture":
        document_parser = FixtureParser()
    elif parser == "mineru-cli":
        document_parser = MinerUCliParser(settings.mineru_command)
    elif parser == "mineru-api":
        document_parser = MinerUApiParser(settings.mineru_api_url, settings.http_timeout)
    else:
        raise typer.BadParameter("parser must be fixture, mineru-cli, or mineru-api")

    knowledge = LocalKnowledgeBackend(workspace)
    model = None
    if semantic:
        model = OpenAICompatibleModel(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            model=settings.llm_model,
            audit_store=knowledge,
            timeout=settings.http_timeout,
        )
    workflow = MaterialIngestionWorkflow(
        document_parser,
        knowledge,
        ArtifactStore(workspace),
        RunStore(workspace),
        MaterialStore(workspace),
        model,
    )
    run, state = asyncio.run(
        workflow.execute(
            source,
            course,
            kind,
            semester=semester,
            year=year,
            language=language,
        )
    )
    typer.echo(f"Run: {run.id}")
    typer.echo(f"Status: {run.status}")
    if run.error:
        typer.echo(f"Error: {run.error}", err=True)
        raise typer.Exit(1)
    typer.echo(f"Knowledge points: {len(state['points'])}")


@materials_app.command("list")
def list_materials(
    course: Annotated[str | None, typer.Option()] = None,
    workspace_path: Annotated[Path | None, typer.Option("--workspace")] = None,
) -> None:
    materials = MaterialStore(_workspace(workspace_path)).list(course)
    typer.echo("ID\tCOURSE\tKIND\tSTATUS\tNAME\tMIME\tSIZE")
    for material in materials:
        typer.echo(
            f"{material.id}\t{material.course_id}\t{material.kind}\t{material.status}\t"
            f"{material.original_name}\t{material.mime_type}\t{material.size_bytes}"
        )


@materials_app.command("show")
def show_material(
    material_id: UUID,
    workspace_path: Annotated[Path | None, typer.Option("--workspace")] = None,
) -> None:
    material = MaterialStore(_workspace(workspace_path)).get(material_id)
    if material is None:
        typer.echo(f"Material not found: {material_id}", err=True)
        raise typer.Exit(1)
    typer.echo(material.model_dump_json(indent=2))


@topics_app.command("list")
def list_topics(
    course: Annotated[str, typer.Option()],
    workspace_path: Annotated[Path | None, typer.Option("--workspace")] = None,
) -> None:
    points = LocalKnowledgeBackend(_workspace(workspace_path)).list_points(course)
    if not points:
        typer.echo("No knowledge points found.")
        return
    typer.echo("SLUG\tNAME\tSOURCE")
    for point in points:
        source = point.evidence[0] if point.evidence else None
        location = f"{source.document_id}:p{source.page}" if source else "-"
        typer.echo(f"{point.slug}\t{point.name}\t{location}")


@topics_app.command("show")
def show_topic(
    slug: str,
    course: Annotated[str, typer.Option()],
    workspace_path: Annotated[Path | None, typer.Option("--workspace")] = None,
) -> None:
    point = LocalKnowledgeBackend(_workspace(workspace_path)).get_point(course, slug)
    if point is None:
        typer.echo(f"Knowledge point not found: {slug}", err=True)
        raise typer.Exit(1)
    typer.echo(point.model_dump_json(indent=2))


@knowledge_app.command("search")
def search_knowledge(
    query: str,
    course: Annotated[str, typer.Option()],
    limit: Annotated[int, typer.Option(min=1, max=100)] = 10,
    workspace_path: Annotated[Path | None, typer.Option("--workspace")] = None,
) -> None:
    hits = LocalKnowledgeBackend(_workspace(workspace_path)).search(course, query, limit)
    if not hits:
        typer.echo("No matching knowledge points found.")
        return
    typer.echo("SCORE\tSLUG\tNAME\tREASONS")
    for hit in hits:
        typer.echo(f"{hit.score:.2f}\t{hit.point.slug}\t{hit.point.name}\t{','.join(hit.reasons)}")


@questions_app.command("plan")
def plan_question(
    topics: Annotated[list[str], typer.Option("--topic", help="Repeat for multiple topics")],
    course: Annotated[str, typer.Option()],
    question_type: Annotated[QuestionType, typer.Option("--type", case_sensitive=False)],
    score: Annotated[int, typer.Option(min=1)] = 10,
    difficulty: Annotated[int, typer.Option(min=1, max=10)] = 5,
    workspace_path: Annotated[Path | None, typer.Option("--workspace")] = None,
) -> None:
    workspace = _workspace(workspace_path)
    workflow = QuestionSpecWorkflow(
        LocalKnowledgeBackend(workspace), ArtifactStore(workspace), RunStore(workspace)
    )
    run, state = asyncio.run(
        workflow.execute(
            course_id=course,
            topic_slugs=topics,
            question_type=question_type,
            score=score,
            difficulty=difficulty,
        )
    )
    typer.echo(f"Run: {run.id}")
    typer.echo(f"Status: {run.status}")
    if run.error:
        typer.echo(f"Error: {run.error}", err=True)
        raise typer.Exit(1)
    typer.echo(state["spec"].model_dump_json(indent=2))


@runs_app.command("list")
def list_runs(
    workspace_path: Annotated[Path | None, typer.Option("--workspace")] = None,
) -> None:
    runs = RunStore(_workspace(workspace_path)).list_runs()
    typer.echo("ID\tWORKFLOW\tSTATUS\tPHASE")
    for run in runs:
        typer.echo(f"{run.id}\t{run.workflow}\t{run.status}\t{run.current_phase or '-'}")


@runs_app.command("inspect")
def inspect_run(
    run_id: UUID,
    workspace_path: Annotated[Path | None, typer.Option("--workspace")] = None,
) -> None:
    store = RunStore(_workspace(workspace_path))
    run = store.get(run_id)
    if run is None:
        typer.echo(f"Run not found: {run_id}", err=True)
        raise typer.Exit(1)
    typer.echo(run.model_dump_json(indent=2))
    typer.echo("\nEVENTS")
    for event in store.events(run_id):
        typer.echo(f"{event.status}\t{event.phase}\t{event.occurrence_id}")
    request = store.pending_human_review(run_id)
    if request:
        typer.echo("\nHUMAN REVIEW")
        typer.echo(request.model_dump_json(indent=2))


@runs_app.command("cancel")
def cancel_run(
    run_id: UUID,
    workspace_path: Annotated[Path | None, typer.Option("--workspace")] = None,
) -> None:
    store = RunStore(_workspace(workspace_path))
    try:
        run = store.request_cancel(run_id)
    except (KeyError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"Run: {run.id}")
    typer.echo(f"Status: {run.status}")


@runs_app.command("resume")
def resume_run(
    run_id: UUID,
    workspace_path: Annotated[Path | None, typer.Option("--workspace")] = None,
) -> None:
    workspace = _workspace(workspace_path)
    store = RunStore(workspace)
    run = store.get(run_id)
    if run is None:
        typer.echo(f"Run not found: {run_id}", err=True)
        raise typer.Exit(1)
    if run.status is RunStatus.WAITING_HUMAN:
        typer.echo(f"Run is waiting for a human decision: {run_id}", err=True)
        _echo_human_review(run_id, store)
        raise typer.Exit(1)

    workflow = _exam_workflow(workspace, Settings(), compile_pdf=True)
    try:
        if run.workflow == "exam_agent_generation":
            resumed, state = asyncio.run(workflow.resume(run_id))
        elif run.workflow == "exam_question_generation":
            resumed, state = asyncio.run(workflow.resume_question_run(run_id))
        elif run.workflow == "exam_edited_assembly":
            resumed, state = asyncio.run(
                _edited_exam_workflow(workspace, Settings()).resume(run_id)
            )
        else:
            raise ValueError(f"workflow does not have a registered resume handler: {run.workflow}")
    except (KeyError, OSError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc

    typer.echo(f"Run: {resumed.id}")
    typer.echo(f"Status: {resumed.status}")
    if resumed.status is RunStatus.WAITING_HUMAN:
        _echo_human_review(resumed.id, store)
        return
    _raise_if_interrupted(resumed.id, resumed.status, resumed.error)
    if resumed.status is not RunStatus.SUCCEEDED:
        typer.echo(f"Error: {resumed.error or 'workflow ended without success'}", err=True)
        raise typer.Exit(1)
    if resumed.workflow in {"exam_agent_generation", "exam_edited_assembly"}:
        exam = state.get("exam")
        if isinstance(exam, ExamDocument):
            typer.echo(f"Questions: {len(exam.questions)}")
            typer.echo(f"Total score: {exam.total_score}")
        for artifact in state.get("artifacts", []):
            if isinstance(artifact, ArtifactRef):
                typer.echo(f"Artifact: {workspace.root / artifact.path}")


def _resolve_human(
    run_id: UUID,
    decision_type: HumanDecisionType,
    actor: str,
    reason: str,
    workspace_path: Path | None,
) -> None:
    store = RunStore(_workspace(workspace_path))
    request = store.pending_human_review(run_id)
    if request is None:
        typer.echo(f"No pending human review for run: {run_id}", err=True)
        raise typer.Exit(1)
    run = store.resolve_human_review(
        HumanDecision(
            request_id=request.id,
            run_id=run_id,
            decision=decision_type,
            actor=actor,
            reason=reason,
            input_artifact_ids=request.artifact_ids,
        )
    )
    typer.echo(f"Run: {run.id}")
    typer.echo(f"Status: {run.status}")


@runs_app.command("approve")
def approve_run(
    run_id: UUID,
    actor: Annotated[str, typer.Option()] = "cli-user",
    reason: Annotated[str, typer.Option()] = "",
    workspace_path: Annotated[Path | None, typer.Option("--workspace")] = None,
) -> None:
    _resolve_human(run_id, HumanDecisionType.ACCEPT, actor, reason, workspace_path)


@runs_app.command("reject")
def reject_run(
    run_id: UUID,
    actor: Annotated[str, typer.Option()] = "cli-user",
    reason: Annotated[str, typer.Option()] = "",
    workspace_path: Annotated[Path | None, typer.Option("--workspace")] = None,
) -> None:
    _resolve_human(run_id, HumanDecisionType.REJECT, actor, reason, workspace_path)


@runs_app.command("retry")
def retry_run(
    run_id: UUID,
    actor: Annotated[str, typer.Option()] = "cli-user",
    reason: Annotated[str, typer.Option()] = "",
    workspace_path: Annotated[Path | None, typer.Option("--workspace")] = None,
) -> None:
    _resolve_human(run_id, HumanDecisionType.RETRY, actor, reason, workspace_path)


@runs_app.command("retry-failed")
def retry_failed_run(
    run_id: UUID,
    actor: Annotated[str, typer.Option()] = "cli-user",
    reason: Annotated[str, typer.Option()] = "",
    workspace_path: Annotated[Path | None, typer.Option("--workspace")] = None,
) -> None:
    try:
        run = RunStore(_workspace(workspace_path)).retry_failed(
            run_id,
            actor=actor,
            reason=reason,
        )
    except (KeyError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"Run: {run.id}")
    typer.echo(f"Status: {run.status}")
    typer.echo(f"Resume: assessment-workbench runs resume {run.id}")


@runs_app.command("abort")
def abort_run(
    run_id: UUID,
    actor: Annotated[str, typer.Option()] = "cli-user",
    reason: Annotated[str, typer.Option()] = "",
    workspace_path: Annotated[Path | None, typer.Option("--workspace")] = None,
) -> None:
    _resolve_human(run_id, HumanDecisionType.ABORT, actor, reason, workspace_path)


@exams_app.command("generate")
def generate_exam(
    subject: Annotated[str, typer.Option(help="Subject to research and assess")],
    target_level: Annotated[str, typer.Option(help="Learner or examination level")],
    requirements: Annotated[str, typer.Option(help="Exam requirements and constraints")],
    source: Annotated[
        Path | None,
        typer.Option(help="Optional UTF-8 source context prepared from course materials"),
    ] = None,
    subject_profile_path: Annotated[
        Path | None,
        typer.Option("--subject-profile", help="Optional locked subject profile YAML"),
    ] = None,
    blueprint_path: Annotated[
        Path | None,
        typer.Option("--blueprint", help="Optional locked exam blueprint YAML"),
    ] = None,
    human_gates: Annotated[
        bool,
        typer.Option(
            "--human-gates/--no-human-gates",
            help="Pause for generated-blueprint and final-exam approval",
        ),
    ] = True,
    workspace_path: Annotated[Path | None, typer.Option("--workspace")] = None,
) -> None:
    if source is not None and not source.is_file():
        raise typer.BadParameter(f"source file does not exist: {source}")
    if (subject_profile_path is None) != (blueprint_path is None):
        raise typer.BadParameter("--subject-profile and --blueprint must be provided together")
    subject_profile: SubjectProfile | None = None
    blueprint: ExamBlueprint | None = None
    if subject_profile_path is not None and blueprint_path is not None:
        if not subject_profile_path.is_file():
            raise typer.BadParameter(f"subject profile file does not exist: {subject_profile_path}")
        if not blueprint_path.is_file():
            raise typer.BadParameter(f"blueprint file does not exist: {blueprint_path}")
        try:
            subject_profile = load_subject_profile(subject_profile_path)
            blueprint = load_exam_blueprint(blueprint_path)
        except (OSError, ValueError) as exc:
            raise typer.BadParameter(f"invalid exam preset: {exc}") from exc

    workspace = _workspace(workspace_path)
    settings = Settings()
    workflow = _exam_workflow(workspace, settings, compile_pdf=True)
    run, state = asyncio.run(
        workflow.execute(
            subject=subject,
            target_level=target_level,
            requirements=requirements,
            source_context=source.read_text(encoding="utf-8") if source else "",
            subject_profile=subject_profile,
            blueprint=blueprint,
            require_blueprint_approval=human_gates and subject_profile is None,
            require_exam_approval=human_gates,
            on_run_created=lambda created: typer.echo(f"Run: {created.id}"),
        )
    )
    typer.echo(f"Status: {run.status}")
    if run.status is RunStatus.WAITING_HUMAN:
        _echo_human_review(run.id, RunStore(workspace))
        return
    _raise_if_interrupted(run.id, run.status, run.error)
    if run.status is not RunStatus.SUCCEEDED:
        typer.echo(f"Error: {run.error or 'workflow ended without success'}", err=True)
        raise typer.Exit(1)
    exam = state["exam"]
    typer.echo(f"Questions: {len(exam.questions)}")
    typer.echo(f"Total score: {exam.total_score}")
    for artifact in state["artifacts"]:
        typer.echo(f"Artifact: {workspace.root / artifact.path}")


@exams_app.command("question-status")
def show_exam_question_status(
    parent_run_id: Annotated[UUID, typer.Option("--parent-run")],
    workspace_path: Annotated[Path | None, typer.Option("--workspace")] = None,
) -> None:
    workspace = _workspace(workspace_path)
    runs = RunStore(workspace)
    if runs.get(parent_run_id) is None:
        raise typer.BadParameter(f"parent run does not exist: {parent_run_id}")
    live_path = workspace.root / "editable" / str(parent_run_id) / "question-runs.json"
    try:
        if live_path.is_file():
            records = json.loads(live_path.read_text(encoding="utf-8"))
        else:
            records = _latest_artifact_json(
                ArtifactStore(workspace), parent_run_id, "question-runs.json"
            )
    except (KeyError, OSError, ValueError) as exc:
        raise typer.BadParameter(f"question run manifest is unavailable: {exc}") from exc
    if not isinstance(records, list):
        raise typer.BadParameter("question run manifest is not a list")

    typer.echo("NUMBER\tSTATUS\tCHILD_RUN\tEDITABLE\tERROR")
    counts: dict[str, int] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        child_id = record.get("run_id")
        status = str(record.get("status") or "queued")
        if child_id:
            try:
                child_run = runs.get(UUID(str(child_id)))
            except ValueError:
                child_run = None
            if child_run is not None:
                status = child_run.status.value
        counts[status] = counts.get(status, 0) + 1
        editable = record.get("editable_path") or "-"
        if editable != "-":
            editable = str(workspace.root / str(editable))
        error = _console_safe(record.get("error") or "").replace("\n", " ")
        typer.echo(
            f"{record.get('question_number', '-')}\t{status}\t{child_id or '-'}\t"
            f"{editable}\t{error}"
        )
    summary = ", ".join(f"{key}={counts[key]}" for key in sorted(counts))
    typer.echo(f"Summary: {summary}")


@exams_app.command("document-status")
def show_exam_document_status(
    parent_run_id: Annotated[UUID, typer.Option("--parent-run")],
    workspace_path: Annotated[Path | None, typer.Option("--workspace")] = None,
) -> None:
    workspace = _workspace(workspace_path)
    artifacts = ArtifactStore(workspace)
    live_path = workspace.root / "editable" / str(parent_run_id) / "document-build-runs.json"
    try:
        if live_path.is_file():
            payload = json.loads(live_path.read_text(encoding="utf-8"))
        else:
            payload = _latest_artifact_json(artifacts, parent_run_id, "document-build-runs.json")
        if not isinstance(payload, list):
            raise ValueError("document build manifest is not a list")
        records = [DocumentBuildRunRecord.model_validate(item) for item in payload]
    except (KeyError, OSError, ValueError) as exc:
        raise typer.BadParameter(f"document build manifest is unavailable: {exc}") from exc
    latest = {record.view.value: record for record in latest_document_builds(records)}
    typer.echo("VIEW\tSTATUS\tATTEMPT\tCHILD_RUN\tPDF\tINSPECTION\tERROR")
    for view in ("questions", "solutions", "rubric"):
        latest_record = latest.get(view)
        if latest_record is None:
            typer.echo(f"{view}\tqueued\t-\t-\t-\t-\t-")
            continue
        pdf = _artifact_display_path(workspace, artifacts, latest_record.pdf_artifact_id)
        inspection = _artifact_display_path(
            workspace,
            artifacts,
            latest_record.inspection_artifact_id,
        )
        error = _console_safe(latest_record.error or "").replace("\n", " ")
        typer.echo(
            f"{view}\t{latest_record.status}\t{latest_record.attempt}\t"
            f"{latest_record.run_id}\t"
            f"{pdf}\t{inspection}\t{error}"
        )


@exams_app.command("generate-question")
def generate_exam_question(
    parent_run_id: Annotated[UUID, typer.Option("--parent-run")],
    number: Annotated[int, typer.Option(min=1)],
    workspace_path: Annotated[Path | None, typer.Option("--workspace")] = None,
) -> None:
    workspace = _workspace(workspace_path)
    runs = RunStore(workspace)
    if runs.get(parent_run_id) is None:
        raise typer.BadParameter(f"parent run does not exist: {parent_run_id}")
    artifacts = ArtifactStore(workspace)
    try:
        profile = SubjectProfile.model_validate(
            _latest_artifact_json(artifacts, parent_run_id, "subject-profile.json")
        )
        blueprint = ExamBlueprint.model_validate(
            _latest_artifact_json(artifacts, parent_run_id, "exam-blueprint.json")
        )
        plans = [
            QuestionPlan.model_validate(item)
            for item in _latest_artifact_json(artifacts, parent_run_id, "question-plans.json")
        ]
    except (KeyError, ValueError) as exc:
        raise typer.BadParameter(f"parent run is missing valid planning artifacts: {exc}") from exc
    plan = next((item for item in plans if item.number == number), None)
    if plan is None:
        raise typer.BadParameter(f"question number is not present in the plan: {number}")

    workflow = _exam_workflow(workspace, Settings(), compile_pdf=False)
    child_run, state = asyncio.run(
        workflow.generate_question_run(
            profile=profile,
            blueprint=blueprint,
            plan=plan,
            parent_run_id=parent_run_id,
            on_run_created=lambda created: typer.echo(f"Run: {created.id}"),
        )
    )
    typer.echo(f"Status: {child_run.status}")
    _raise_if_interrupted(child_run.id, child_run.status, child_run.error)
    if child_run.status is not RunStatus.SUCCEEDED:
        typer.echo(f"Error: {child_run.error or 'question generation failed'}", err=True)
        raise typer.Exit(1)

    bundle = state.get("bundle")
    bundle_artifact = state.get("bundle_artifact")
    if not isinstance(bundle, ExamQuestionBundle) or not isinstance(bundle_artifact, ArtifactRef):
        raise RuntimeError("question child run completed without a bundle artifact")
    editable_path = artifacts.write_editable_json(
        parent_run_id,
        f"questions/{number:02d}.json",
        bundle.model_dump(mode="json"),
    )
    try:
        records = _latest_artifact_json(artifacts, parent_run_id, "question-runs.json")
    except KeyError:
        records = []
    if not isinstance(records, list):
        raise RuntimeError("question-runs artifact is not a list")
    records = [
        record
        for record in records
        if not isinstance(record, dict) or record.get("question_number") != number
    ]
    records.append(
        {
            "question_number": number,
            "plan_id": plan.id,
            "run_id": str(child_run.id),
            "status": child_run.status,
            "error": child_run.error,
            "bundle_artifact_id": str(bundle_artifact.id),
            "bundle_path": str(bundle_artifact.path),
            "editable_path": str(editable_path),
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
    typer.echo(f"Editable: {workspace.root / editable_path}")


@exams_app.command("assemble-edited")
def assemble_edited_exam(
    parent_run_id: Annotated[UUID, typer.Option("--parent-run")],
    human_gates: Annotated[
        bool,
        typer.Option(
            "--human-gates/--no-human-gates",
            help="Pause for full-page visual approval before publishing",
        ),
    ] = False,
    workspace_path: Annotated[Path | None, typer.Option("--workspace")] = None,
) -> None:
    workspace = _workspace(workspace_path)
    artifacts = ArtifactStore(workspace)
    try:
        blueprint = ExamBlueprint.model_validate(
            _latest_artifact_json(artifacts, parent_run_id, "exam-blueprint.json")
        )
    except (KeyError, ValueError) as exc:
        raise typer.BadParameter(f"parent run has no valid blueprint: {exc}") from exc

    expected_count = sum(section.count for section in blueprint.sections)
    question_dir = workspace.root / "editable" / str(parent_run_id) / "questions"
    paths = [question_dir / f"{number:02d}.json" for number in range(1, expected_count + 1)]
    missing = [path.name for path in paths if not path.is_file()]
    if missing:
        raise typer.BadParameter(f"editable questions are missing: {', '.join(missing)}")
    try:
        bundles = [
            ExamQuestionBundle.model_validate_json(path.read_text(encoding="utf-8"))
            for path in paths
        ]
        exam = ExamDocument(
            blueprint_id=blueprint.id,
            title=blueprint.title,
            subject_profile=blueprint.subject_profile,
            duration_minutes=blueprint.duration_minutes,
            total_score=blueprint.total_score,
            language=blueprint.language,
            questions=bundles,
        )
    except (OSError, ValueError) as exc:
        raise typer.BadParameter(f"editable question validation failed: {exc}") from exc

    assembly_run, state = asyncio.run(
        _edited_exam_workflow(workspace, Settings()).execute(
            exam,
            source_parent_run_id=parent_run_id,
            require_document_approval=human_gates,
            on_run_created=lambda created: typer.echo(f"Run: {created.id}"),
        )
    )
    typer.echo(f"Status: {assembly_run.status}")
    if assembly_run.status is RunStatus.WAITING_HUMAN:
        _echo_human_review(assembly_run.id, RunStore(workspace))
        return
    _raise_if_interrupted(assembly_run.id, assembly_run.status, assembly_run.error)
    if assembly_run.status is not RunStatus.SUCCEEDED:
        typer.echo(f"Error: {assembly_run.error or 'edited assembly failed'}", err=True)
        raise typer.Exit(1)
    outputs = state["artifacts"]
    typer.echo(f"Questions: {len(exam.questions)}")
    typer.echo(f"Total score: {exam.total_score}")
    for artifact in outputs:
        typer.echo(f"Artifact: {workspace.root / artifact.path}")
