from pathlib import Path
from typing import Any

from assessment_workbench.domain import PhaseStatus, RunStatus
from assessment_workbench.storage import RunStore, Workspace
from assessment_workbench.workflow import WorkflowEngine


async def test_workflow_records_failure(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "workspace")
    workspace.initialize()
    store = RunStore(workspace)
    engine = WorkflowEngine(store)

    async def fail(_: dict[str, Any]) -> dict[str, Any]:
        raise ValueError("broken")

    run, _ = await engine.execute("test", [("FAIL", fail)])

    assert run.status is RunStatus.FAILED
    assert run.error == "broken"
    events = store.events(run.id)
    assert [event.status for event in events] == [PhaseStatus.RUNNING, PhaseStatus.FAILED]
    assert events[0].occurrence_id == events[1].occurrence_id
