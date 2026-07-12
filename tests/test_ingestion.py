from pathlib import Path

from assessment_workbench.domain import MaterialKind, PhaseStatus, RunStatus
from assessment_workbench.ingestion import MaterialIngestionWorkflow
from assessment_workbench.parsers import FixtureParser
from assessment_workbench.storage import (
    ArtifactStore,
    LocalKnowledgeBackend,
    RunStore,
    Workspace,
)


async def test_ingestion_persists_graph_and_paired_events(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "workspace")
    workspace.initialize()
    runs = RunStore(workspace)
    knowledge = LocalKnowledgeBackend(workspace)
    workflow = MaterialIngestionWorkflow(FixtureParser(), knowledge, ArtifactStore(workspace), runs)
    source = Path(__file__).parent / "fixtures" / "sample_course.json"

    run, state = await workflow.execute(source, "demo-physics", MaterialKind.LECTURE)

    assert run.status is RunStatus.SUCCEEDED
    assert len(state["points"]) == 5
    point = knowledge.get_point("demo-physics", "电磁学.静电场.高斯定律")
    assert point is not None
    assert point.evidence[0].page == 5
    events = runs.events(run.id)
    assert [event.status for event in events] == [
        PhaseStatus.RUNNING,
        PhaseStatus.COMPLETED,
        PhaseStatus.RUNNING,
        PhaseStatus.COMPLETED,
        PhaseStatus.RUNNING,
        PhaseStatus.COMPLETED,
    ]
    assert events[0].occurrence_id == events[1].occurrence_id
    artifacts = ArtifactStore(workspace).list(run.id)
    assert [artifact.logical_name for artifact in artifacts] == [
        "knowledge-graph.json",
        "parsed-document.json",
    ]
    assert all(ArtifactStore(workspace).verify(artifact.id) for artifact in artifacts)
    assert events[-1].output_artifact_ids
