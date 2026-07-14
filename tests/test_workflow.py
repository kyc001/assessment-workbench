import socket
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from assessment_workbench.domain import (
    HumanDecision,
    HumanDecisionType,
    PhaseEvent,
    PhaseStatus,
    RunStatus,
    WorkflowCheckpoint,
    now_utc,
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


def test_phase_event_and_checkpoint_rollback_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = Workspace(tmp_path / "workspace")
    workspace.initialize()
    store = RunStore(workspace)
    run = store.create("phase-transaction")
    event = PhaseEvent(
        run_id=run.id,
        workflow=run.workflow,
        phase="WRITE",
        status=PhaseStatus.COMPLETED,
        occurrence_id=uuid4(),
        started_at=now_utc(),
        completed_at=now_utc(),
    )
    checkpoint = WorkflowCheckpoint(
        run_id=run.id,
        workflow=run.workflow,
        next_step_index=1,
    )

    def fail_checkpoint(*_: Any) -> None:
        raise RuntimeError("injected checkpoint failure")

    monkeypatch.setattr(store, "_upsert_checkpoint", fail_checkpoint)
    with pytest.raises(RuntimeError, match="injected checkpoint failure"):
        store.commit_phase(event, checkpoint)

    assert store.events(run.id) == []
    assert store.get_checkpoint(run.id) is None


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


async def test_named_phase_jump_records_rounds_and_preserves_order(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "workspace")
    workspace.initialize()
    store = RunStore(workspace)
    engine = WorkflowEngine(store)
    calls: list[str] = []

    async def repeat(state: dict[str, Any]) -> dict[str, Any]:
        calls.append("repeat")
        count = int(state.get("count", 0)) + 1
        if count == 1:
            return {"count": count, "_next_phase": "REPEAT"}
        return {"count": count}

    async def finish(_: dict[str, Any]) -> dict[str, Any]:
        calls.append("finish")
        return {}

    run, _ = await engine.execute("jump", [("REPEAT", repeat), ("FINISH", finish)])

    assert run.status is RunStatus.SUCCEEDED
    assert calls == ["repeat", "repeat", "finish"]
    repeat_events = [event for event in store.events(run.id) if event.phase == "REPEAT"]
    assert [event.round for event in repeat_events] == [1, 1, 2, 2]


async def test_unknown_phase_jump_fails_current_phase(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "workspace")
    workspace.initialize()
    store = RunStore(workspace)
    engine = WorkflowEngine(store)

    async def invalid(_: dict[str, Any]) -> dict[str, Any]:
        return {"_next_phase": "MISSING"}

    run, _ = await engine.execute("jump", [("ONLY", invalid)])

    assert run.status is RunStatus.FAILED
    assert "does not exist" in (run.error or "")
    assert [event.status for event in store.events(run.id)] == [
        PhaseStatus.RUNNING,
        PhaseStatus.FAILED,
    ]


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


async def test_human_retry_returns_to_named_phase(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "workspace")
    workspace.initialize()
    store = RunStore(workspace)
    engine = WorkflowEngine(store)
    calls: list[str] = []

    async def plan(_: dict[str, Any]) -> dict[str, Any]:
        calls.append("plan")
        return {}

    async def review(_: dict[str, Any]) -> dict[str, Any]:
        calls.append("review")
        return {
            "_human_review": {
                "prompt": "Approve",
                "retry_phase": "PLAN",
            }
        }

    steps = [("PLAN", plan), ("REVIEW", review)]
    run, _ = await engine.execute("reviewable", steps)
    request = store.pending_human_review(run.id)
    assert request is not None
    store.resolve_human_review(
        HumanDecision(
            request_id=request.id,
            run_id=run.id,
            decision=HumanDecisionType.RETRY,
            actor="tester",
        )
    )

    resumed, _ = await engine.resume(run.id, "reviewable", steps)

    assert resumed.status is RunStatus.WAITING_HUMAN
    assert calls == ["plan", "review", "plan", "review"]


async def test_cooperative_cancel_stops_before_next_step(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "workspace")
    workspace.initialize()
    store = RunStore(workspace)
    engine = WorkflowEngine(store)
    calls: list[str] = []

    async def first(state: dict[str, Any]) -> dict[str, Any]:
        calls.append("first")
        store.request_cancel(state["run_id"])
        return {"saved": "yes"}

    async def second(_: dict[str, Any]) -> dict[str, Any]:
        calls.append("second")
        return {}

    run, _ = await engine.execute("cancel", [("FIRST", first), ("SECOND", second)])

    assert run.status is RunStatus.CANCELLED
    assert calls == ["first"]
    assert run.runner_host is None
    assert store.get_checkpoint(run.id) is None


def test_orphaned_local_runner_becomes_interrupted(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "workspace")
    workspace.initialize()
    store = RunStore(workspace)
    run = store.create("orphan")
    store.transition(run, RunStatus.RUNNING)
    run.runner_host = socket.gethostname()
    run.runner_pid = 2147483647
    store.save(run)

    recovered = store.recover_orphaned()

    assert [item.id for item in recovered] == [run.id]
    stored = store.get(run.id)
    assert stored is not None
    assert stored.status is RunStatus.INTERRUPTED
    assert stored.runner_host is None
    assert stored.runner_pid is None


def test_live_runner_is_not_recovered(tmp_path: Path) -> None:
    import os

    workspace = Workspace(tmp_path / "workspace")
    workspace.initialize()
    store = RunStore(workspace)
    run = store.create("live")
    store.transition(run, RunStatus.RUNNING)
    store.claim(run, os.getpid())

    assert store.recover_orphaned() == []
    stored = store.get(run.id)
    assert stored is not None
    assert stored.status is RunStatus.RUNNING


@pytest.mark.parametrize("error_code", ["ProtocolError", "RemoteProtocolError"])
def test_failed_protocol_error_run_is_audited_before_retry(tmp_path: Path, error_code: str) -> None:
    workspace = Workspace(tmp_path / "workspace")
    workspace.initialize()
    store = RunStore(workspace)
    run = store.create("protocol-recovery")
    store.transition(run, RunStatus.RUNNING, current_phase="SUBJECT_SYNTHESIZING")
    store.save_checkpoint(
        WorkflowCheckpoint(run_id=run.id, workflow=run.workflow, next_step_index=1)
    )
    store.append_event(
        PhaseEvent(
            run_id=run.id,
            workflow=run.workflow,
            phase="SUBJECT_SYNTHESIZING",
            status=PhaseStatus.FAILED,
            occurrence_id=uuid4(),
            started_at=now_utc(),
            completed_at=now_utc(),
            error_code=error_code,
            error="Server disconnected without sending a response.",
        )
    )
    store.transition(run, RunStatus.FAILED, error="Server disconnected")

    recovered = store.retry_failed(run.id, actor="test", reason="retry protocol disconnect")

    assert recovered.status is RunStatus.INTERRUPTED
    recovery_event = store.events(run.id)[-1]
    assert recovery_event.phase == "RUN_RECOVERY"
    assert recovery_event.error_details["failed_error_code"] == error_code


def test_failed_value_error_run_remains_non_retryable(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "workspace")
    workspace.initialize()
    store = RunStore(workspace)
    run = store.create("permanent-failure")
    store.transition(run, RunStatus.RUNNING, current_phase="SUBJECT_SYNTHESIZING")
    store.save_checkpoint(
        WorkflowCheckpoint(run_id=run.id, workflow=run.workflow, next_step_index=1)
    )
    store.append_event(
        PhaseEvent(
            run_id=run.id,
            workflow=run.workflow,
            phase="SUBJECT_SYNTHESIZING",
            status=PhaseStatus.FAILED,
            occurrence_id=uuid4(),
            started_at=now_utc(),
            completed_at=now_utc(),
            error_code="ValueError",
            error="invalid synthesis",
        )
    )
    store.transition(run, RunStatus.FAILED, error="invalid synthesis")

    with pytest.raises(ValueError, match="not retryable"):
        store.retry_failed(run.id, actor="test", reason="should remain failed")
