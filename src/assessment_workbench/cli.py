import asyncio
from pathlib import Path
from typing import Annotated
from uuid import UUID

import typer

from assessment_workbench.config import Settings
from assessment_workbench.domain import HumanDecision, HumanDecisionType, MaterialKind, QuestionType
from assessment_workbench.ingestion import MaterialIngestionWorkflow
from assessment_workbench.models import OpenAICompatibleModel
from assessment_workbench.parsers import FixtureParser, MinerUApiParser, MinerUCliParser
from assessment_workbench.planning import QuestionSpecWorkflow
from assessment_workbench.ports import DocumentParser
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


@runs_app.command("abort")
def abort_run(
    run_id: UUID,
    actor: Annotated[str, typer.Option()] = "cli-user",
    reason: Annotated[str, typer.Option()] = "",
    workspace_path: Annotated[Path | None, typer.Option("--workspace")] = None,
) -> None:
    _resolve_human(run_id, HumanDecisionType.ABORT, actor, reason, workspace_path)
