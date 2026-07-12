from pathlib import Path
from typing import Any
from uuid import uuid4

from assessment_workbench.domain import HumanDecision, HumanDecisionType, RunStatus
from assessment_workbench.storage import ArtifactStore, RunStore, Workspace
from assessment_workbench.workflow import WorkflowEngine


async def test_parent_child_human_review_and_resume_lifecycle(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "workspace")
    workspace.initialize()
    store = RunStore(workspace)
    artifacts = ArtifactStore(workspace)
    engine = WorkflowEngine(store)
    parent_run = store.create("parent")
    parent_event_id = uuid4()
    calls: list[str] = []

    async def produce(state: dict[str, Any]) -> dict[str, Any]:
        calls.append("produce")
        artifact = artifacts.write_json(
            state["run_id"], "candidate.json", {"value": 1}, created_by_phase="PRODUCE"
        )
        return {
            "candidate_artifact_id": str(artifact.id),
            "output_artifact_ids": [str(artifact.id)],
        }

    async def review(state: dict[str, Any]) -> dict[str, Any]:
        calls.append("review")
        return {
            "_human_review": {
                "prompt": "Approve candidate",
                "artifact_ids": [state["candidate_artifact_id"]],
            }
        }

    async def finish(_: dict[str, Any]) -> dict[str, Any]:
        calls.append("finish")
        return {"finished": True}

    steps = [("PRODUCE", produce), ("REVIEW", review), ("FINISH", finish)]
    run, _ = await engine.execute(
        "child", steps, parent_run_id=parent_run.id, parent_event_id=parent_event_id
    )

    assert run.status is RunStatus.WAITING_HUMAN
    assert calls == ["produce", "review"]
    request = store.pending_human_review(run.id)
    assert request is not None
    store.resolve_human_review(
        HumanDecision(
            request_id=request.id,
            run_id=run.id,
            decision=HumanDecisionType.ACCEPT,
            actor="reviewer",
        )
    )

    resumed, state = await engine.resume(
        run.id, "child", steps, parent_run_id=parent_run.id, parent_event_id=parent_event_id
    )

    assert resumed.status is RunStatus.SUCCEEDED
    assert state["finished"] is True
    assert calls == ["produce", "review", "finish"]
    assert all(event.parent_run_id == parent_run.id for event in store.events(run.id))
    assert artifacts.verify(request.artifact_ids[0])
