from pathlib import Path

from assessment_workbench.domain import MaterialKind, QuestionType, RunStatus
from assessment_workbench.ingestion import MaterialIngestionWorkflow
from assessment_workbench.parsers import FixtureParser
from assessment_workbench.planning import QuestionSpecWorkflow
from assessment_workbench.storage import (
    ArtifactStore,
    LocalKnowledgeBackend,
    RunStore,
    Workspace,
)


async def test_plans_question_spec_from_topic(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "workspace")
    workspace.initialize()
    knowledge = LocalKnowledgeBackend(workspace)
    artifacts = ArtifactStore(workspace)
    runs = RunStore(workspace)
    source = Path(__file__).parent / "fixtures" / "sample_course.json"
    await MaterialIngestionWorkflow(FixtureParser(), knowledge, artifacts, runs).execute(
        source, "demo-physics", MaterialKind.LECTURE
    )

    run, state = await QuestionSpecWorkflow(knowledge, artifacts, runs).execute(
        course_id="demo-physics",
        topic_slugs=["电磁学.静电场.高斯定律"],
        question_type=QuestionType.CALCULATION,
        score=20,
        difficulty=7,
    )

    assert run.status is RunStatus.SUCCEEDED
    spec = state["spec"]
    assert spec.score == 20
    assert spec.difficulty.overall == 7
    assert spec.required_context
    assert (workspace.artifacts / str(run.id) / "question-spec.json").exists()
