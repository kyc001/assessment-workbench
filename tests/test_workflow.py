from pathlib import Path
from typing import Any
from uuid import uuid4

from assessment_workbench.domain import (
    HumanDecision,
    HumanDecisionType,
    PhaseStatus,
    RunStatus,
)
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


async def test_resume_starts_after_last_checkpoint(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "workspace")
    workspace.initialize()
    store = RunStore(workspace)
    engine = WorkflowEngine(store)
    calls: list[str] = []

    async def first(_: dict[str, Any]) -> dict[str, Any]:
        calls.append("first")
        return {"stable": "saved"}

    async def interrupt(_: dict[str, Any]) -> dict[str, Any]:
        calls.append("interrupt")
        raise KeyboardInterrupt

    run, _ = await engine.execute("resumable", [("FIRST", first), ("SECOND", interrupt)])
    assert run.status is RunStatus.INTERRUPTED
    checkpoint = store.get_checkpoint(run.id)
    assert checkpoint is not None
    assert checkpoint.next_step_index == 1

    async def second(state: dict[str, Any]) -> dict[str, Any]:
        calls.append("second")
        assert state["stable"] == "saved"
        return {"done": True}

    resumed, state = await engine.resume(
        run.id, "resumable", [("FIRST", first), ("SECOND", second)]
    )

    assert resumed.status is RunStatus.SUCCEEDED
    assert state["done"] is True
    assert calls == ["first", "interrupt", "second"]
    assert store.get_checkpoint(run.id) is None


async def test_human_review_pauses_and_approval_makes_run_resumable(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "workspace")
    workspace.initialize()
    store = RunStore(workspace)
    engine = WorkflowEngine(store)

    async def request_review(_: dict[str, Any]) -> dict[str, Any]:
        return {"_human_review": {"prompt": "Approve blueprint"}}

    run, _ = await engine.execute("reviewable", [("REVIEW", request_review)])
    assert run.status is RunStatus.WAITING_HUMAN
    request = store.pending_human_review(run.id)
    assert request is not None
    assert request.prompt == "Approve blueprint"

    resolved = store.resolve_human_review(
        HumanDecision(
            request_id=request.id,
            run_id=run.id,
            decision=HumanDecisionType.ACCEPT,
            actor="tester",
            reason="looks good",
        )
    )

    assert resolved.status is RunStatus.INTERRUPTED
    assert store.pending_human_review(run.id) is None


async def test_human_rejection_fails_run(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "workspace")
    workspace.initialize()
    store = RunStore(workspace)
    engine = WorkflowEngine(store)

    async def request_review(_: dict[str, Any]) -> dict[str, Any]:
        return {"_human_review": {"prompt": "Review"}}

    run, _ = await engine.execute("reviewable", [("REVIEW", request_review)])
    request = store.pending_human_review(run.id)
    assert request is not None
    resolved = store.resolve_human_review(
        HumanDecision(
            request_id=request.id,
            run_id=run.id,
            decision=HumanDecisionType.REJECT,
            actor="tester",
            reason="incorrect",
        )
    )

    assert resolved.status is RunStatus.FAILED
    assert resolved.error == "incorrect"
