import asyncio
import json
import sys
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID, uuid4

import typer

from assessment_workbench.application import (
    build_edited_exam_workflow,
    build_exam_workflow,
    latest_artifact_json,
    open_workspace,
    publish_question_bundle,
)
from assessment_workbench.benchmarking import (
    AttackKind,
    DeterministicBaseline,
    build_attack_dataset,
    calculate_benchmark_experiment_report,
    calculate_optimization_pressure,
    calculate_verifier_disagreement,
    calculate_verifier_metrics,
    generate_deterministic_baseline_observations,
    read_benchmark_cases,
    read_verifier_observations,
    validate_benchmark_dataset,
    write_benchmark_cases,
    write_verifier_observations,
)
from assessment_workbench.config import Settings
from assessment_workbench.document_workflow import latest_document_builds
from assessment_workbench.domain import (
    ArtifactRef,
    CalculatorPolicy,
    DifficultyBasis,
    DocumentBuildRunRecord,
    ExamBlueprint,
    ExamDocument,
    ExamQuestionBundle,
    HumanDecision,
    HumanDecisionType,
    MaterialKind,
    PhaseEvent,
    PhaseStatus,
    QuestionGenerationRequest,
    QuestionPlan,
    QuestionType,
    RunStatus,
    SubjectProfile,
    now_utc,
)
from assessment_workbench.exam_quality import (
    validate_bundle_for_plan,
    validate_question_plan_coverage,
    validate_question_plan_timing,
)
from assessment_workbench.ingestion import MaterialIngestionWorkflow
from assessment_workbench.models import OpenAICompatibleModel
from assessment_workbench.parsers import FixtureParser, MinerUApiParser, MinerUCliParser
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
from assessment_workbench.subject_research_workflow import parse_subject_research_records

app = typer.Typer(help="Course-grounded assessment workflows")
workspace_app = typer.Typer(help="Initialize and inspect workspaces")
materials_app = typer.Typer(help="Ingest course materials")
topics_app = typer.Typer(help="Browse course knowledge points")
runs_app = typer.Typer(help="Inspect workflow runs")
knowledge_app = typer.Typer(help="Search course knowledge")
questions_app = typer.Typer(help="Plan and generate questions")
exams_app = typer.Typer(help="Plan, generate, and export exams")
benchmark_app = typer.Typer(help="Build and evaluate verifier benchmarks")
app.add_typer(workspace_app, name="workspace")
app.add_typer(materials_app, name="materials")
app.add_typer(topics_app, name="topics")
app.add_typer(runs_app, name="runs")
app.add_typer(knowledge_app, name="knowledge")
app.add_typer(questions_app, name="questions")
app.add_typer(exams_app, name="exams")
app.add_typer(benchmark_app, name="benchmark")


def _workspace(path: Path | None) -> Workspace:
    return open_workspace(path)


def _console_safe(value: object, *, err: bool = False) -> str:
    text = str(value)
    stream = sys.stderr if err else sys.stdout
    encoding = getattr(stream, "encoding", None) or "utf-8"
    return text.encode(encoding, errors="replace").decode(encoding)


def _emit_json(payload: str, output: Path | None) -> None:
    if output is None:
        typer.echo(payload)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(f"{payload}\n", encoding="utf-8")
    typer.echo(output)


@benchmark_app.command("attack")
def generate_attack_benchmark(
    cases_path: Annotated[
        Path,
        typer.Option("--cases", exists=True, file_okay=True, dir_okay=False, readable=True),
    ],
    output: Annotated[
        Path,
        typer.Option("--output", help="Paired clean and attacked benchmark JSONL path"),
    ],
    attack_kinds: Annotated[
        list[AttackKind] | None,
        typer.Option(
            "--attack",
            help="Attack family; repeat as needed. Defaults to all families.",
        ),
    ] = None,
) -> None:
    try:
        dataset = build_attack_dataset(
            read_benchmark_cases(cases_path),
            attack_kinds=attack_kinds,
        )
        write_benchmark_cases(output, dataset)
    except (OSError, ValueError) as exc:
        typer.echo(_console_safe(exc, err=True), err=True)
        raise typer.Exit(1) from exc
    typer.echo(output)


@benchmark_app.command("attack-rubric")
def generate_rubric_loophole_benchmark(
    cases_path: Annotated[
        Path,
        typer.Option("--cases", exists=True, file_okay=True, dir_okay=False, readable=True),
    ],
    output: Annotated[
        Path,
        typer.Option("--output", help="Paired clean and attacked benchmark JSONL path"),
    ],
) -> None:
    try:
        clean_cases = read_benchmark_cases(cases_path)
        write_benchmark_cases(
            output,
            build_attack_dataset(
                clean_cases,
                attack_kinds=[AttackKind.RUBRIC_LOOPHOLE],
            ),
        )
    except (OSError, ValueError) as exc:
        typer.echo(_console_safe(exc, err=True), err=True)
        raise typer.Exit(1) from exc
    typer.echo(output)


@benchmark_app.command("validate")
def validate_benchmark(
    cases_path: Annotated[
        Path,
        typer.Option("--cases", exists=True, file_okay=True, dir_okay=False, readable=True),
    ],
    output: Annotated[Path | None, typer.Option(help="Optional JSON output path")] = None,
) -> None:
    try:
        summary = validate_benchmark_dataset(read_benchmark_cases(cases_path))
    except (OSError, ValueError) as exc:
        typer.echo(_console_safe(exc, err=True), err=True)
        raise typer.Exit(1) from exc
    _emit_json(summary.model_dump_json(indent=2), output)


@benchmark_app.command("observe-baseline")
def generate_baseline_observations(
    cases_path: Annotated[
        Path,
        typer.Option("--cases", exists=True, file_okay=True, dir_okay=False, readable=True),
    ],
    output: Annotated[
        Path,
        typer.Option("--output", help="Verifier observation JSONL output path"),
    ],
    baselines: Annotated[
        list[DeterministicBaseline] | None,
        typer.Option(
            "--baseline",
            help="Deterministic baseline; repeat as needed. Defaults to all baselines.",
        ),
    ] = None,
    trial: Annotated[int, typer.Option(min=1)] = 1,
) -> None:
    try:
        observations = generate_deterministic_baseline_observations(
            read_benchmark_cases(cases_path),
            baselines=baselines,
            trial=trial,
        )
        write_verifier_observations(output, observations)
    except (OSError, ValueError) as exc:
        typer.echo(_console_safe(exc, err=True), err=True)
        raise typer.Exit(1) from exc
    typer.echo(output)


@benchmark_app.command("pressure")
def evaluate_optimization_pressure(
    cases_path: Annotated[
        Path,
        typer.Option("--cases", exists=True, file_okay=True, dir_okay=False, readable=True),
    ],
    observations_path: Annotated[
        Path,
        typer.Option(
            "--observations",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
        ),
    ],
    verifier: Annotated[str, typer.Option(help="Verifier ID used as the reward source")],
    trial: Annotated[int, typer.Option(min=1)] = 1,
    budgets: Annotated[
        list[int] | None,
        typer.Option("--budget", min=1, help="Candidate budget N; repeat as needed"),
    ] = None,
    output: Annotated[Path | None, typer.Option(help="Optional JSON output path")] = None,
) -> None:
    try:
        report = calculate_optimization_pressure(
            read_benchmark_cases(cases_path),
            read_verifier_observations(observations_path),
            verifier=verifier,
            trial=trial,
            candidate_budgets=budgets,
        )
    except (OSError, ValueError) as exc:
        typer.echo(_console_safe(exc, err=True), err=True)
        raise typer.Exit(1) from exc
    _emit_json(report.model_dump_json(indent=2), output)


@benchmark_app.command("report")
def build_benchmark_experiment_report(
    cases_path: Annotated[
        Path,
        typer.Option("--cases", exists=True, file_okay=True, dir_okay=False, readable=True),
    ],
    observations_path: Annotated[
        Path,
        typer.Option(
            "--observations",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
        ),
    ],
    verifiers: Annotated[
        list[str],
        typer.Option("--verifier", help="Verifier ID; repeat for each experiment arm"),
    ],
    trial: Annotated[int, typer.Option(min=1)] = 1,
    output: Annotated[Path | None, typer.Option(help="Optional JSON output path")] = None,
) -> None:
    try:
        report = calculate_benchmark_experiment_report(
            read_benchmark_cases(cases_path),
            read_verifier_observations(observations_path),
            verifiers=verifiers,
            trial=trial,
        )
    except (OSError, ValueError) as exc:
        typer.echo(_console_safe(exc, err=True), err=True)
        raise typer.Exit(1) from exc
    _emit_json(report.model_dump_json(indent=2), output)


@benchmark_app.command("disagreement")
def evaluate_verifier_disagreement(
    cases_path: Annotated[
        Path,
        typer.Option("--cases", exists=True, file_okay=True, dir_okay=False, readable=True),
    ],
    observations_path: Annotated[
        Path,
        typer.Option(
            "--observations",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
        ),
    ],
    verifiers: Annotated[
        list[str],
        typer.Option("--verifier", help="Verifier ID; repeat for each ensemble member"),
    ],
    trial: Annotated[int, typer.Option(min=1)] = 1,
    output: Annotated[Path | None, typer.Option(help="Optional JSON output path")] = None,
) -> None:
    try:
        metrics = calculate_verifier_disagreement(
            read_benchmark_cases(cases_path),
            read_verifier_observations(observations_path),
            verifiers=verifiers,
            trial=trial,
        )
    except (OSError, ValueError) as exc:
        typer.echo(_console_safe(exc, err=True), err=True)
        raise typer.Exit(1) from exc
    _emit_json(metrics.model_dump_json(indent=2), output)


@benchmark_app.command("evaluate")
def evaluate_benchmark(
    cases_path: Annotated[
        Path,
        typer.Option("--cases", exists=True, file_okay=True, dir_okay=False, readable=True),
    ],
    observations_path: Annotated[
        Path,
        typer.Option(
            "--observations",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
        ),
    ],
    verifier: Annotated[str, typer.Option(help="Verifier ID stored in ReviewReport.reviewer")],
    trial: Annotated[int, typer.Option(min=1)] = 1,
    output: Annotated[Path | None, typer.Option(help="Optional JSON output path")] = None,
) -> None:
    try:
        metrics = calculate_verifier_metrics(
            read_benchmark_cases(cases_path),
            read_verifier_observations(observations_path),
            verifier=verifier,
            trial=trial,
        )
    except (OSError, ValueError) as exc:
        typer.echo(_console_safe(exc, err=True), err=True)
        raise typer.Exit(1) from exc
    _emit_json(metrics.model_dump_json(indent=2), output)


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


@app.command("gui")
def launch_gui(
    host: Annotated[
        str, typer.Option(help="Host interface for the local GUI service")
    ] = "127.0.0.1",
    port: Annotated[int, typer.Option(min=1, max=65535)] = 8765,
    open_browser: Annotated[
        bool,
        typer.Option("--open/--no-open", help="Open the GUI in the default browser"),
    ] = True,
    workspace_path: Annotated[Path | None, typer.Option("--workspace")] = None,
) -> None:
    import threading
    import webbrowser

    import uvicorn

    from assessment_workbench.web_api import create_gui_app

    workspace = _workspace(workspace_path)
    if host not in {"127.0.0.1", "localhost", "::1"}:
        typer.echo(
            "Warning: GUI is running without authentication on a non-loopback interface.",
            err=True,
        )
    browser_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    url = f"http://{browser_host}:{port}"
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    typer.echo(f"Assessment Workbench GUI: {url}")
    typer.echo(f"Workspace: {workspace.root}")
    uvicorn.run(create_gui_app(workspace, Settings()), host=host, port=port, log_level="info")


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
            schema_in_prompt=settings.llm_schema_in_prompt,
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

    workflow = build_exam_workflow(workspace, Settings(), compile_pdf=True)
    try:
        if run.workflow == "exam_agent_generation":
            resumed, state = asyncio.run(workflow.resume(run_id))
        elif run.workflow == "exam_question_generation":
            resumed, state = asyncio.run(workflow.resume_question_run(run_id))
        elif run.workflow == "exam_edited_assembly":
            resumed, state = asyncio.run(
                build_edited_exam_workflow(workspace, Settings()).resume(run_id)
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
    if resumed.workflow == "exam_question_generation":
        request = QuestionGenerationRequest.model_validate(
            latest_artifact_json(ArtifactStore(workspace), resumed.id, "question-request.json")
        )
        bundle = state.get("bundle")
        bundle_artifact = state.get("bundle_artifact")
        if (
            request.parent_run_id is not None
            and isinstance(bundle, ExamQuestionBundle)
            and isinstance(bundle_artifact, ArtifactRef)
        ):
            editable_path = publish_question_bundle(
                workspace,
                parent_run_id=request.parent_run_id,
                plan=request.plan,
                child_run=resumed,
                bundle=bundle,
                bundle_artifact=bundle_artifact,
            )
            typer.echo(f"Editable: {workspace.root / editable_path}")
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
    workflow = build_exam_workflow(workspace, settings, compile_pdf=True)
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
            records = latest_artifact_json(
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


@exams_app.command("research-status")
def show_exam_research_status(
    parent_run_id: Annotated[UUID, typer.Option("--parent-run")],
    workspace_path: Annotated[Path | None, typer.Option("--workspace")] = None,
) -> None:
    workspace = _workspace(workspace_path)
    artifacts = ArtifactStore(workspace)
    runs = RunStore(workspace)
    live_path = workspace.root / "editable" / str(parent_run_id) / "subject-research-runs.json"
    try:
        payload = (
            json.loads(live_path.read_text(encoding="utf-8"))
            if live_path.is_file()
            else latest_artifact_json(artifacts, parent_run_id, "subject-research-runs.json")
        )
        records = parse_subject_research_records(payload)
    except (KeyError, OSError, ValueError) as exc:
        raise typer.BadParameter(f"subject research manifest is unavailable: {exc}") from exc
    typer.echo("ROLE\tSTATUS\tATTEMPT\tCHILD_RUN\tREPORT\tERROR")
    for record in records:
        child = runs.get(record.run_id)
        status = child.status if child is not None else record.status
        report = _artifact_display_path(workspace, artifacts, record.report_artifact_id)
        error = _console_safe((child.error if child is not None else record.error) or "").replace(
            "\n", " "
        )
        typer.echo(
            f"{record.research_role}\t{status}\t{record.attempt}\t{record.run_id}\t"
            f"{report}\t{error}"
        )


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
            payload = latest_artifact_json(artifacts, parent_run_id, "document-build-runs.json")
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
    feedback: Annotated[
        list[str] | None,
        typer.Option(
            "--feedback",
            help="Writer feedback for this independent question rerun; repeat as needed",
        ),
    ] = None,
    workspace_path: Annotated[Path | None, typer.Option("--workspace")] = None,
) -> None:
    workspace = _workspace(workspace_path)
    runs = RunStore(workspace)
    if runs.get(parent_run_id) is None:
        raise typer.BadParameter(f"parent run does not exist: {parent_run_id}")
    artifacts = ArtifactStore(workspace)
    try:
        profile = SubjectProfile.model_validate(
            latest_artifact_json(artifacts, parent_run_id, "subject-profile.json")
        )
        blueprint = ExamBlueprint.model_validate(
            latest_artifact_json(artifacts, parent_run_id, "exam-blueprint.json")
        )
        plans = [
            QuestionPlan.model_validate(item)
            for item in latest_artifact_json(artifacts, parent_run_id, "question-plans.json")
        ]
    except (KeyError, ValueError) as exc:
        raise typer.BadParameter(f"parent run is missing valid planning artifacts: {exc}") from exc
    plan = next((item for item in plans if item.number == number), None)
    if plan is None:
        raise typer.BadParameter(f"question number is not present in the plan: {number}")

    workflow = build_exam_workflow(workspace, Settings(), compile_pdf=False)
    child_run, state = asyncio.run(
        workflow.generate_question_run(
            profile=profile,
            blueprint=blueprint,
            plan=plan,
            parent_run_id=parent_run_id,
            generation_feedback=feedback or [],
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
    editable_path = publish_question_bundle(
        workspace,
        parent_run_id=parent_run_id,
        plan=plan,
        child_run=child_run,
        bundle=bundle,
        bundle_artifact=bundle_artifact,
    )
    typer.echo(f"Editable: {workspace.root / editable_path}")


@exams_app.command("set-plan-minutes")
def set_exam_question_plan_minutes(
    parent_run_id: Annotated[UUID, typer.Option("--parent-run")],
    number: Annotated[int, typer.Option(min=1)],
    minutes: Annotated[int, typer.Option(min=1)],
    actor: Annotated[str, typer.Option()] = "cli-user",
    reason: Annotated[str, typer.Option()] = "",
    workspace_path: Annotated[Path | None, typer.Option("--workspace")] = None,
) -> None:
    if not actor.strip() or not reason.strip():
        raise typer.BadParameter("plan minute override requires actor and reason")
    workspace = _workspace(workspace_path)
    runs = RunStore(workspace)
    run = runs.get(parent_run_id)
    if run is None:
        raise typer.BadParameter(f"parent run does not exist: {parent_run_id}")
    if run.status is not RunStatus.WAITING_HUMAN:
        raise typer.BadParameter("plan minute override requires a waiting_human parent run")
    checkpoint = runs.get_checkpoint(parent_run_id)
    if checkpoint is None:
        raise typer.BadParameter("parent run has no recovery checkpoint")
    plans_artifact_id = checkpoint.artifact_bindings.get("question_plans")
    blueprint_artifact_id = checkpoint.artifact_bindings.get("blueprint")
    if plans_artifact_id is None or blueprint_artifact_id is None:
        raise typer.BadParameter("parent checkpoint is missing plans or blueprint")
    artifacts = ArtifactStore(workspace)
    plans_payload = artifacts.read_json(plans_artifact_id)
    if not isinstance(plans_payload, list):
        raise typer.BadParameter("question plan artifact is not a list")
    plans = [QuestionPlan.model_validate(item) for item in plans_payload]
    current = next((plan for plan in plans if plan.number == number), None)
    if current is None:
        raise typer.BadParameter(f"question number is not present in the plan: {number}")
    updated = [
        plan.model_copy(update={"estimated_minutes": minutes}) if plan.number == number else plan
        for plan in plans
    ]
    blueprint = ExamBlueprint.model_validate(artifacts.read_json(blueprint_artifact_id))
    validate_question_plan_coverage(blueprint, updated)
    validate_question_plan_timing(blueprint, updated)
    updated_artifact = artifacts.write_json(
        parent_run_id,
        "question-plans.json",
        [plan.model_dump(mode="json") for plan in updated],
        created_by_phase="QUESTION_PLAN_HUMAN_OVERRIDE",
    )
    checkpoint.artifact_bindings["question_plans"] = updated_artifact.id
    checkpoint.created_at = now_utc()
    timestamp = checkpoint.created_at
    runs.commit_checkpoint_override(
        PhaseEvent(
            run_id=run.id,
            workflow=run.workflow,
            phase="QUESTION_PLAN_HUMAN_OVERRIDE",
            status=PhaseStatus.COMPLETED,
            occurrence_id=uuid4(),
            round=1
            + sum(event.phase == "QUESTION_PLAN_HUMAN_OVERRIDE" for event in runs.events(run.id)),
            input_artifact_ids=[plans_artifact_id],
            output_artifact_ids=[updated_artifact.id],
            started_at=timestamp,
            completed_at=timestamp,
            summary=reason.strip(),
            error_details={
                "actor": actor.strip(),
                "question_number": str(number),
                "old_minutes": str(current.estimated_minutes),
                "new_minutes": str(minutes),
                "total_minutes": str(sum(plan.estimated_minutes for plan in updated)),
            },
        ),
        checkpoint,
        binding_key="question_plans",
        expected_artifact_id=plans_artifact_id,
    )
    typer.echo(f"Question: {number}")
    typer.echo(f"Minutes: {current.estimated_minutes} -> {minutes}")
    typer.echo(f"Total minutes: {sum(plan.estimated_minutes for plan in updated)}")
    typer.echo(f"Artifact: {workspace.root / updated_artifact.path}")


@exams_app.command("set-title")
def set_exam_title(
    parent_run_id: Annotated[UUID, typer.Option("--parent-run")],
    title: Annotated[str, typer.Option("--title")],
    actor: Annotated[str, typer.Option()] = "cli-user",
    reason: Annotated[str, typer.Option()] = "",
    workspace_path: Annotated[Path | None, typer.Option("--workspace")] = None,
) -> None:
    if not title.strip() or not actor.strip() or not reason.strip():
        raise typer.BadParameter("title revision requires title, actor, and reason")
    workspace = _workspace(workspace_path)
    runs = RunStore(workspace)
    run = runs.get(parent_run_id)
    if run is None:
        raise typer.BadParameter(f"parent run does not exist: {parent_run_id}")
    if run.status is not RunStatus.WAITING_HUMAN:
        raise typer.BadParameter("title revision requires a waiting_human parent run")
    checkpoint = runs.get_checkpoint(parent_run_id)
    if checkpoint is None:
        raise typer.BadParameter("parent run has no recovery checkpoint")
    blueprint_artifact_id = checkpoint.artifact_bindings.get("blueprint")
    if blueprint_artifact_id is None:
        raise typer.BadParameter("parent checkpoint is missing the blueprint")
    artifacts = ArtifactStore(workspace)
    blueprint = ExamBlueprint.model_validate(artifacts.read_json(blueprint_artifact_id))
    updated = blueprint.model_copy(update={"title": title.strip()})
    updated_artifact = artifacts.write_json(
        parent_run_id,
        "exam-blueprint.json",
        updated.model_dump(mode="json"),
        created_by_phase="BLUEPRINT_HUMAN_OVERRIDE",
    )
    checkpoint.artifact_bindings["blueprint"] = updated_artifact.id
    checkpoint.created_at = now_utc()
    timestamp = checkpoint.created_at
    runs.commit_checkpoint_override(
        PhaseEvent(
            run_id=run.id,
            workflow=run.workflow,
            phase="BLUEPRINT_HUMAN_OVERRIDE",
            status=PhaseStatus.COMPLETED,
            occurrence_id=uuid4(),
            round=1
            + sum(event.phase == "BLUEPRINT_HUMAN_OVERRIDE" for event in runs.events(run.id)),
            input_artifact_ids=[blueprint_artifact_id],
            output_artifact_ids=[updated_artifact.id],
            started_at=timestamp,
            completed_at=timestamp,
            summary=reason.strip(),
            error_details={
                "actor": actor.strip(),
                "old_title": blueprint.title,
                "new_title": updated.title,
            },
        ),
        checkpoint,
        binding_key="blueprint",
        expected_artifact_id=blueprint_artifact_id,
    )
    typer.echo(f"Title: {blueprint.title} -> {updated.title}")
    typer.echo(f"Artifact: {workspace.root / updated_artifact.path}")


@exams_app.command("set-calculator-policy")
def set_exam_calculator_policy(
    parent_run_id: Annotated[UUID, typer.Option("--parent-run")],
    policy: Annotated[CalculatorPolicy, typer.Option("--policy")],
    actor: Annotated[str, typer.Option()] = "cli-user",
    reason: Annotated[str, typer.Option()] = "",
    workspace_path: Annotated[Path | None, typer.Option("--workspace")] = None,
) -> None:
    if not actor.strip() or not reason.strip():
        raise typer.BadParameter("calculator policy revision requires actor and reason")
    workspace = _workspace(workspace_path)
    runs = RunStore(workspace)
    run = runs.get(parent_run_id)
    if run is None:
        raise typer.BadParameter(f"parent run does not exist: {parent_run_id}")
    if run.status not in {RunStatus.WAITING_HUMAN, RunStatus.INTERRUPTED}:
        raise typer.BadParameter(
            "calculator policy revision requires a waiting_human or interrupted parent run"
        )
    checkpoint = runs.get_checkpoint(parent_run_id)
    if checkpoint is None:
        raise typer.BadParameter("parent run has no recovery checkpoint")
    blueprint_artifact_id = checkpoint.artifact_bindings.get("blueprint")
    if blueprint_artifact_id is None:
        raise typer.BadParameter("parent checkpoint is missing the blueprint")
    artifacts = ArtifactStore(workspace)
    blueprint = ExamBlueprint.model_validate(artifacts.read_json(blueprint_artifact_id))
    retained_constraints = [
        constraint
        for constraint in blueprint.constraints
        if "计算器" not in constraint and "calculator" not in constraint.lower()
    ]
    policy_constraint = {
        CalculatorPolicy.UNSPECIFIED: "计算器及其他计算辅助工具政策尚未锁定，需在命题前人工确认。",
        CalculatorPolicy.PROHIBITED: (
            "闭卷考试不允许使用计算器；除题目明确要求外，数值结果保留精确形式。"
        ),
        CalculatorPolicy.SCIENTIFIC_ALLOWED: (
            "允许使用不具备编程、符号代数或联网功能的普通科学计算器。"
        ),
    }[policy]
    updated = blueprint.model_copy(
        update={
            "calculator_policy": policy,
            "constraints": [policy_constraint, *retained_constraints],
        }
    )
    updated_artifact = artifacts.write_json(
        parent_run_id,
        "exam-blueprint.json",
        updated.model_dump(mode="json"),
        created_by_phase="BLUEPRINT_HUMAN_OVERRIDE",
    )
    checkpoint.artifact_bindings["blueprint"] = updated_artifact.id
    checkpoint.created_at = now_utc()
    timestamp = checkpoint.created_at
    runs.commit_checkpoint_override(
        PhaseEvent(
            run_id=run.id,
            workflow=run.workflow,
            phase="BLUEPRINT_HUMAN_OVERRIDE",
            status=PhaseStatus.COMPLETED,
            occurrence_id=uuid4(),
            round=1
            + sum(event.phase == "BLUEPRINT_HUMAN_OVERRIDE" for event in runs.events(run.id)),
            input_artifact_ids=[blueprint_artifact_id],
            output_artifact_ids=[updated_artifact.id],
            started_at=timestamp,
            completed_at=timestamp,
            summary=reason.strip(),
            error_details={
                "actor": actor.strip(),
                "old_calculator_policy": blueprint.calculator_policy.value,
                "new_calculator_policy": updated.calculator_policy.value,
            },
        ),
        checkpoint,
        binding_key="blueprint",
        expected_artifact_id=blueprint_artifact_id,
    )
    typer.echo(
        f"Calculator policy: {blueprint.calculator_policy.value} -> "
        f"{updated.calculator_policy.value}"
    )
    typer.echo(f"Artifact: {workspace.root / updated_artifact.path}")


@exams_app.command("set-difficulty-basis")
def set_exam_difficulty_basis(
    parent_run_id: Annotated[UUID, typer.Option("--parent-run")],
    basis: Annotated[DifficultyBasis, typer.Option("--basis")],
    actor: Annotated[str, typer.Option()] = "cli-user",
    reason: Annotated[str, typer.Option()] = "",
    workspace_path: Annotated[Path | None, typer.Option("--workspace")] = None,
) -> None:
    if not actor.strip() or not reason.strip():
        raise typer.BadParameter("difficulty basis revision requires actor and reason")
    workspace = _workspace(workspace_path)
    runs = RunStore(workspace)
    run = runs.get(parent_run_id)
    if run is None:
        raise typer.BadParameter(f"parent run does not exist: {parent_run_id}")
    if run.status not in {RunStatus.WAITING_HUMAN, RunStatus.INTERRUPTED}:
        raise typer.BadParameter(
            "difficulty basis revision requires a waiting_human or interrupted parent run"
        )
    checkpoint = runs.get_checkpoint(parent_run_id)
    if checkpoint is None:
        raise typer.BadParameter("parent run has no recovery checkpoint")
    blueprint_artifact_id = checkpoint.artifact_bindings.get("blueprint")
    if blueprint_artifact_id is None:
        raise typer.BadParameter("parent checkpoint is missing the blueprint")
    artifacts = ArtifactStore(workspace)
    blueprint = ExamBlueprint.model_validate(artifacts.read_json(blueprint_artifact_id))
    retained_constraints = [
        constraint for constraint in blueprint.constraints if "难度比例" not in constraint
    ]
    basis_constraint = {
        DifficultyBasis.UNSPECIFIED: "难度比例的统计口径尚未锁定，需在命题前人工确认。",
        DifficultyBasis.QUESTION_COUNT: (
            "难度比例按题数统计；18题下采用易6题、中9题、难3题，作为30%/50%/20%的最近整数分配。"
        ),
        DifficultyBasis.SCORE: "难度比例按题目分值统计。",
        DifficultyBasis.ESTIMATED_TIME: "难度比例按预计作答时间统计。",
    }[basis]
    updated = blueprint.model_copy(
        update={
            "difficulty_basis": basis,
            "constraints": [basis_constraint, *retained_constraints],
        }
    )
    updated_artifact = artifacts.write_json(
        parent_run_id,
        "exam-blueprint.json",
        updated.model_dump(mode="json"),
        created_by_phase="BLUEPRINT_HUMAN_OVERRIDE",
    )
    checkpoint.artifact_bindings["blueprint"] = updated_artifact.id
    checkpoint.created_at = now_utc()
    timestamp = checkpoint.created_at
    runs.commit_checkpoint_override(
        PhaseEvent(
            run_id=run.id,
            workflow=run.workflow,
            phase="BLUEPRINT_HUMAN_OVERRIDE",
            status=PhaseStatus.COMPLETED,
            occurrence_id=uuid4(),
            round=1
            + sum(event.phase == "BLUEPRINT_HUMAN_OVERRIDE" for event in runs.events(run.id)),
            input_artifact_ids=[blueprint_artifact_id],
            output_artifact_ids=[updated_artifact.id],
            started_at=timestamp,
            completed_at=timestamp,
            summary=reason.strip(),
            error_details={
                "actor": actor.strip(),
                "old_difficulty_basis": blueprint.difficulty_basis.value,
                "new_difficulty_basis": updated.difficulty_basis.value,
            },
        ),
        checkpoint,
        binding_key="blueprint",
        expected_artifact_id=blueprint_artifact_id,
    )
    typer.echo(
        f"Difficulty basis: {blueprint.difficulty_basis.value} -> {updated.difficulty_basis.value}"
    )
    typer.echo(f"Artifact: {workspace.root / updated_artifact.path}")


@exams_app.command("revise-plan")
def revise_exam_question_plan(
    parent_run_id: Annotated[UUID, typer.Option("--parent-run")],
    number: Annotated[int, typer.Option(min=1)],
    topic_tags: Annotated[list[str] | None, typer.Option("--topic-tag")] = None,
    primary_skill: Annotated[str | None, typer.Option("--primary-skill")] = None,
    design_brief: Annotated[str | None, typer.Option("--design-brief")] = None,
    answer_form: Annotated[str | None, typer.Option("--answer-form")] = None,
    solution_outline: Annotated[list[str] | None, typer.Option("--solution-outline")] = None,
    rubric_focus: Annotated[list[str] | None, typer.Option("--rubric-focus")] = None,
    verification_methods: Annotated[list[str] | None, typer.Option("--verification-method")] = None,
    originality_constraints: Annotated[
        list[str] | None, typer.Option("--originality-constraint")
    ] = None,
    actor: Annotated[str, typer.Option()] = "cli-user",
    reason: Annotated[str, typer.Option()] = "",
    workspace_path: Annotated[Path | None, typer.Option("--workspace")] = None,
) -> None:
    if not actor.strip() or not reason.strip():
        raise typer.BadParameter("plan revision requires actor and reason")
    updates = {
        key: value
        for key, value in {
            "topic_tags": topic_tags,
            "primary_skill": primary_skill,
            "design_brief": design_brief,
            "answer_form": answer_form,
            "solution_outline": solution_outline,
            "rubric_focus": rubric_focus,
            "verification_methods": verification_methods,
            "originality_constraints": originality_constraints,
        }.items()
        if value is not None
    }
    if not updates:
        raise typer.BadParameter("plan revision requires at least one changed field")
    workspace = _workspace(workspace_path)
    runs = RunStore(workspace)
    run = runs.get(parent_run_id)
    if run is None:
        raise typer.BadParameter(f"parent run does not exist: {parent_run_id}")
    if run.status is not RunStatus.WAITING_HUMAN:
        raise typer.BadParameter("plan revision requires a waiting_human parent run")
    checkpoint = runs.get_checkpoint(parent_run_id)
    if checkpoint is None:
        raise typer.BadParameter("parent run has no recovery checkpoint")
    plans_artifact_id = checkpoint.artifact_bindings.get("question_plans")
    blueprint_artifact_id = checkpoint.artifact_bindings.get("blueprint")
    if plans_artifact_id is None or blueprint_artifact_id is None:
        raise typer.BadParameter("parent checkpoint is missing plans or blueprint")
    artifacts = ArtifactStore(workspace)
    plans_payload = artifacts.read_json(plans_artifact_id)
    if not isinstance(plans_payload, list):
        raise typer.BadParameter("question plan artifact is not a list")
    plans = [QuestionPlan.model_validate(item) for item in plans_payload]
    current = next((plan for plan in plans if plan.number == number), None)
    if current is None:
        raise typer.BadParameter(f"question number is not present in the plan: {number}")
    updated = [
        QuestionPlan.model_validate({**plan.model_dump(mode="python"), **updates})
        if plan.number == number
        else plan
        for plan in plans
    ]
    blueprint = ExamBlueprint.model_validate(artifacts.read_json(blueprint_artifact_id))
    validate_question_plan_coverage(blueprint, updated)
    validate_question_plan_timing(blueprint, updated)
    updated_artifact = artifacts.write_json(
        parent_run_id,
        "question-plans.json",
        [plan.model_dump(mode="json") for plan in updated],
        created_by_phase="QUESTION_PLAN_HUMAN_OVERRIDE",
    )
    checkpoint.artifact_bindings["question_plans"] = updated_artifact.id
    checkpoint.created_at = now_utc()
    timestamp = checkpoint.created_at
    runs.commit_checkpoint_override(
        PhaseEvent(
            run_id=run.id,
            workflow=run.workflow,
            phase="QUESTION_PLAN_HUMAN_OVERRIDE",
            status=PhaseStatus.COMPLETED,
            occurrence_id=uuid4(),
            round=1
            + sum(event.phase == "QUESTION_PLAN_HUMAN_OVERRIDE" for event in runs.events(run.id)),
            input_artifact_ids=[plans_artifact_id],
            output_artifact_ids=[updated_artifact.id],
            started_at=timestamp,
            completed_at=timestamp,
            summary=reason.strip(),
            error_details={
                "actor": actor.strip(),
                "question_number": str(number),
                "changed_fields": ",".join(sorted(updates)),
            },
        ),
        checkpoint,
        binding_key="question_plans",
        expected_artifact_id=plans_artifact_id,
    )
    typer.echo(f"Question: {number}")
    typer.echo(f"Changed fields: {', '.join(sorted(updates))}")
    typer.echo(f"Artifact: {workspace.root / updated_artifact.path}")


@exams_app.command("restore-plan-version")
def restore_exam_question_plan_version(
    parent_run_id: Annotated[UUID, typer.Option("--parent-run")],
    version: Annotated[int, typer.Option(min=1)],
    actor: Annotated[str, typer.Option()] = "cli-user",
    reason: Annotated[str, typer.Option()] = "",
    workspace_path: Annotated[Path | None, typer.Option("--workspace")] = None,
) -> None:
    if not actor.strip() or not reason.strip():
        raise typer.BadParameter("plan rollback requires actor and reason")
    workspace = _workspace(workspace_path)
    runs = RunStore(workspace)
    run = runs.get(parent_run_id)
    if run is None:
        raise typer.BadParameter(f"parent run does not exist: {parent_run_id}")
    if run.status is not RunStatus.WAITING_HUMAN:
        raise typer.BadParameter("plan rollback requires a waiting_human parent run")
    checkpoint = runs.get_checkpoint(parent_run_id)
    if checkpoint is None:
        raise typer.BadParameter("parent run has no recovery checkpoint")
    current_artifact_id = checkpoint.artifact_bindings.get("question_plans")
    blueprint_artifact_id = checkpoint.artifact_bindings.get("blueprint")
    if current_artifact_id is None or blueprint_artifact_id is None:
        raise typer.BadParameter("parent checkpoint is missing plans or blueprint")
    artifacts = ArtifactStore(workspace)
    selected = next(
        (
            artifact
            for artifact in artifacts.list(parent_run_id)
            if artifact.logical_name == "question-plans.json" and artifact.version == version
        ),
        None,
    )
    if selected is None:
        raise typer.BadParameter(f"question plan artifact version does not exist: {version}")
    payload = artifacts.read_json(selected.id)
    if not isinstance(payload, list):
        raise typer.BadParameter("selected question plan artifact is not a list")
    plans = [QuestionPlan.model_validate(item) for item in payload]
    blueprint = ExamBlueprint.model_validate(artifacts.read_json(blueprint_artifact_id))
    validate_question_plan_coverage(blueprint, plans)
    validate_question_plan_timing(blueprint, plans)
    checkpoint.artifact_bindings["question_plans"] = selected.id
    checkpoint.created_at = now_utc()
    timestamp = checkpoint.created_at
    runs.commit_checkpoint_override(
        PhaseEvent(
            run_id=run.id,
            workflow=run.workflow,
            phase="QUESTION_PLAN_ROLLBACK",
            status=PhaseStatus.COMPLETED,
            occurrence_id=uuid4(),
            round=1 + sum(event.phase == "QUESTION_PLAN_ROLLBACK" for event in runs.events(run.id)),
            input_artifact_ids=[current_artifact_id],
            output_artifact_ids=[selected.id],
            started_at=timestamp,
            completed_at=timestamp,
            summary=reason.strip(),
            error_details={
                "actor": actor.strip(),
                "restored_version": str(version),
            },
        ),
        checkpoint,
        binding_key="question_plans",
        expected_artifact_id=current_artifact_id,
    )
    typer.echo(f"Restored version: {version}")
    typer.echo(f"Artifact: {workspace.root / selected.path}")


@exams_app.command("publish-question-run")
def publish_exam_question_run(
    child_run_id: Annotated[UUID, typer.Option("--child-run")],
    workspace_path: Annotated[Path | None, typer.Option("--workspace")] = None,
) -> None:
    workspace = _workspace(workspace_path)
    runs = RunStore(workspace)
    child_run = runs.get(child_run_id)
    if child_run is None:
        raise typer.BadParameter(f"question child run does not exist: {child_run_id}")
    if (
        child_run.workflow != "exam_question_generation"
        or child_run.status is not RunStatus.SUCCEEDED
    ):
        raise typer.BadParameter("question child run must be a succeeded exam_question_generation")
    artifacts = ArtifactStore(workspace)
    try:
        request = QuestionGenerationRequest.model_validate(
            latest_artifact_json(artifacts, child_run.id, "question-request.json")
        )
        bundle = ExamQuestionBundle.model_validate(
            latest_artifact_json(artifacts, child_run.id, "question-bundle.json")
        )
        bundle_artifact = artifacts.latest(child_run.id, "question-bundle.json")
    except (KeyError, ValueError) as exc:
        raise typer.BadParameter(f"question child artifacts are invalid: {exc}") from exc
    if request.parent_run_id is None or bundle_artifact is None:
        raise typer.BadParameter("question child run has no parent or bundle artifact")
    editable_path = publish_question_bundle(
        workspace,
        parent_run_id=request.parent_run_id,
        plan=request.plan,
        child_run=child_run,
        bundle=bundle,
        bundle_artifact=bundle_artifact,
    )
    typer.echo(f"Parent run: {request.parent_run_id}")
    typer.echo(f"Editable: {workspace.root / editable_path}")


@exams_app.command("accept-edited-questions")
def accept_edited_exam_questions(
    parent_run_id: Annotated[UUID, typer.Option("--parent-run")],
    actor: Annotated[str, typer.Option()] = "cli-user",
    reason: Annotated[str, typer.Option()] = "",
    workspace_path: Annotated[Path | None, typer.Option("--workspace")] = None,
) -> None:
    if not actor.strip() or not reason.strip():
        raise typer.BadParameter("accepting edited questions requires actor and reason")
    workspace = _workspace(workspace_path)
    runs = RunStore(workspace)
    run = runs.get(parent_run_id)
    if run is None:
        raise typer.BadParameter(f"parent run does not exist: {parent_run_id}")
    accepting_interrupted_questions = (
        run.status is RunStatus.INTERRUPTED and run.current_phase == "QUESTIONS_GENERATING"
    )
    if run.status is not RunStatus.WAITING_HUMAN and not accepting_interrupted_questions:
        raise typer.BadParameter(
            "accepting edited questions requires a waiting_human parent run or an "
            "interrupted QUESTIONS_GENERATING parent run"
        )
    review_request = (
        runs.pending_human_review(parent_run_id) if run.status is RunStatus.WAITING_HUMAN else None
    )
    checkpoint = runs.get_checkpoint(parent_run_id)
    if checkpoint is None:
        raise typer.BadParameter("parent run has no recovery checkpoint")
    if run.status is RunStatus.WAITING_HUMAN and review_request is None:
        raise typer.BadParameter("parent run has no pending review")
    plans_artifact_id = checkpoint.artifact_bindings.get("question_plans")
    blueprint_artifact_id = checkpoint.artifact_bindings.get("blueprint")
    previous_runs_artifact_id = checkpoint.artifact_bindings.get("question_runs")
    if (
        plans_artifact_id is None
        or blueprint_artifact_id is None
        or previous_runs_artifact_id is None
    ):
        raise typer.BadParameter("parent checkpoint is missing plans, blueprint, or question runs")
    artifacts = ArtifactStore(workspace)
    plans_payload = artifacts.read_json(plans_artifact_id)
    if not isinstance(plans_payload, list):
        raise typer.BadParameter("question plan artifact is not a list")
    plans = [QuestionPlan.model_validate(item) for item in plans_payload]
    blueprint = ExamBlueprint.model_validate(artifacts.read_json(blueprint_artifact_id))
    validate_question_plan_coverage(blueprint, plans)
    validate_question_plan_timing(blueprint, plans)
    plan_by_number = {plan.number: plan for plan in plans}
    question_dir = workspace.root / "editable" / str(parent_run_id) / "questions"
    expected_numbers = list(range(1, sum(section.count for section in blueprint.sections) + 1))
    bundles: list[ExamQuestionBundle] = []
    for number in expected_numbers:
        path = question_dir / f"{number:02d}.json"
        if not path.is_file():
            raise typer.BadParameter(f"editable question is missing: {path.name}")
        bundle = ExamQuestionBundle.model_validate_json(path.read_text(encoding="utf-8"))
        validate_bundle_for_plan(bundle, plan_by_number[number])
        bundles.append(bundle)

    existing_payload = artifacts.read_json(previous_runs_artifact_id)
    if not isinstance(existing_payload, list):
        raise typer.BadParameter("question run artifact is not a list")
    existing_by_number = {
        int(record["question_number"]): record
        for record in existing_payload
        if isinstance(record, dict) and "question_number" in record
    }
    bundle_artifacts: list[ArtifactRef] = []
    records: list[dict[str, Any]] = []
    for bundle in bundles:
        number = bundle.question.number
        bundle_artifact = artifacts.write_json(
            parent_run_id,
            f"edited-question-{number:02d}-bundle.json",
            bundle.model_dump(mode="json"),
            created_by_phase="EDITED_QUESTIONS_ACCEPTED",
        )
        bundle_artifacts.append(bundle_artifact)
        existing = existing_by_number.get(number, {})
        records.append(
            {
                **existing,
                "question_number": number,
                "plan_id": plan_by_number[number].id,
                "status": RunStatus.SUCCEEDED,
                "error": None,
                "bundle_artifact_id": str(bundle_artifact.id),
                "bundle_path": str(bundle_artifact.path),
                "editable_path": str(
                    Path("editable") / str(parent_run_id) / "questions" / f"{number:02d}.json"
                ),
                "requires_human_review": False,
            }
        )
    bundles_artifact = artifacts.write_json(
        parent_run_id,
        "question-bundles.json",
        [bundle.model_dump(mode="json") for bundle in bundles],
        created_by_phase="EDITED_QUESTIONS_ACCEPTED",
    )
    runs_artifact = artifacts.write_json(
        parent_run_id,
        "question-runs.json",
        records,
        created_by_phase="EDITED_QUESTIONS_ACCEPTED",
    )
    artifacts.write_editable_json(parent_run_id, "question-runs.json", records)

    resolved = run
    if review_request is not None:
        resolved = runs.resolve_human_review(
            HumanDecision(
                request_id=review_request.id,
                run_id=parent_run_id,
                decision=HumanDecisionType.EDIT_ACCEPT,
                actor=actor.strip(),
                reason=reason.strip(),
                input_artifact_ids=[
                    *review_request.artifact_ids,
                    plans_artifact_id,
                    *[artifact.id for artifact in bundle_artifacts],
                ],
            )
        )
    checkpoint = runs.get_checkpoint(parent_run_id)
    if checkpoint is None:
        raise RuntimeError("parent checkpoint disappeared after edit acceptance")
    current_runs_artifact_id = checkpoint.artifact_bindings.get("question_runs")
    if current_runs_artifact_id is None:
        raise RuntimeError("parent checkpoint lost its question run binding")
    checkpoint.artifact_bindings["question_runs"] = runs_artifact.id
    checkpoint.artifact_bindings["bundles"] = bundles_artifact.id
    for key in (
        "exam",
        "exam_review_manifest",
        "exam_reviews",
        "exam_decision",
        "exam_workflow_state",
        "document_manifest",
        "document_acceptance",
        "release_bundle",
    ):
        checkpoint.artifact_bindings.pop(key, None)
    checkpoint.next_step_index = 7
    checkpoint.created_at = now_utc()
    timestamp = checkpoint.created_at
    runs.commit_checkpoint_override(
        PhaseEvent(
            run_id=run.id,
            workflow=run.workflow,
            phase="EDITED_QUESTIONS_ACCEPTED",
            status=PhaseStatus.COMPLETED,
            occurrence_id=uuid4(),
            round=1
            + sum(event.phase == "EDITED_QUESTIONS_ACCEPTED" for event in runs.events(run.id)),
            input_artifact_ids=[previous_runs_artifact_id, plans_artifact_id],
            output_artifact_ids=[
                runs_artifact.id,
                bundles_artifact.id,
                *[artifact.id for artifact in bundle_artifacts],
            ],
            started_at=timestamp,
            completed_at=timestamp,
            summary=reason.strip(),
            error_details={
                "actor": actor.strip(),
                "question_count": str(len(bundles)),
                "resume_phase": "EXAM_ASSEMBLING",
            },
        ),
        checkpoint,
        binding_key="question_runs",
        expected_artifact_id=current_runs_artifact_id,
    )
    typer.echo(f"Run: {resolved.id}")
    typer.echo(f"Status: {resolved.status}")
    typer.echo(f"Questions accepted: {len(bundles)}")
    typer.echo("Resume phase: EXAM_ASSEMBLING")


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
            latest_artifact_json(artifacts, parent_run_id, "exam-blueprint.json")
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
            calculator_policy=blueprint.calculator_policy,
            questions=bundles,
        )
    except (OSError, ValueError) as exc:
        raise typer.BadParameter(f"editable question validation failed: {exc}") from exc

    assembly_run, state = asyncio.run(
        build_edited_exam_workflow(workspace, Settings()).execute(
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
