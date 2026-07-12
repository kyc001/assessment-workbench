from pathlib import Path
from typing import Any
from uuid import uuid4

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
    assert events[0].workflow == "test"
    assert events[1].error_code == "ValueError"


async def test_workflow_records_parent_and_artifact_links(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "workspace")
    workspace.initialize()
    store = RunStore(workspace)
    engine = WorkflowEngine(store)
    parent_run_id = uuid4()
    parent_event_id = uuid4()
    input_id = uuid4()
    output_id = uuid4()

    async def complete(state: dict[str, Any]) -> dict[str, Any]:
        return {"output_artifact_ids": [str(output_id)]}

    run, _ = await engine.execute(
        "child",
        [("WORK", complete)],
        {"input_artifact_ids": [str(input_id)]},
        parent_run_id=parent_run_id,
        parent_event_id=parent_event_id,
    )

    events = store.events(run.id)
    assert events[0].parent_run_id == parent_run_id
    assert events[0].parent_event_id == parent_event_id
    assert events[0].input_artifact_ids == [input_id]
    assert events[1].output_artifact_ids == [output_id]


def test_cancel_request_uses_cancelling_state(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "workspace")
    workspace.initialize()
    store = RunStore(workspace)
    queued = store.create("queued")
    running = store.create("running")
    store.transition(running, RunStatus.RUNNING)

    assert store.request_cancel(queued.id).status is RunStatus.CANCELLED
    assert store.request_cancel(running.id).status is RunStatus.CANCELLING
