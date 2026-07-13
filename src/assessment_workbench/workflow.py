import os
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID, uuid4

from assessment_workbench.domain import (
    HumanReviewRequest,
    PhaseEvent,
    PhaseStatus,
    RunStatus,
    WorkflowCheckpoint,
    WorkflowRun,
    now_utc,
)
from assessment_workbench.storage import RunStore

Step = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
RunCreatedCallback = Callable[[WorkflowRun], None]


class WorkflowEngine:
    def __init__(self, store: RunStore) -> None:
        self.store = store

    async def execute(
        self,
        workflow: str,
        steps: list[tuple[str, Step]],
        context: dict[str, Any] | None = None,
        *,
        parent_run_id: UUID | None = None,
        parent_event_id: UUID | None = None,
        on_run_created: RunCreatedCallback | None = None,
    ) -> tuple[WorkflowRun, dict[str, Any]]:
        run = self.store.create(workflow)
        if on_run_created is not None:
            on_run_created(run)
        state = dict(context or {})
        state["run_id"] = run.id
        self.store.transition(run, RunStatus.RUNNING)
        self.store.claim(run, os.getpid())
        return await self._execute_steps(run, steps, state, 0, parent_run_id, parent_event_id)

    async def resume(
        self,
        run_id: UUID,
        workflow: str,
        steps: list[tuple[str, Step]],
        *,
        parent_run_id: UUID | None = None,
        parent_event_id: UUID | None = None,
    ) -> tuple[WorkflowRun, dict[str, Any]]:
        run = self.store.get(run_id)
        if run is None:
            raise KeyError(f"run not found: {run_id}")
        if run.workflow != workflow:
            raise ValueError(f"workflow mismatch: expected {run.workflow}, got {workflow}")
        if run.status is not RunStatus.INTERRUPTED:
            raise ValueError(f"run is not resumable from status: {run.status}")
        checkpoint = self.store.get_checkpoint(run_id)
        if checkpoint is None:
            raise ValueError(f"run has no checkpoint: {run_id}")
        state: dict[str, Any] = dict(checkpoint.context)
        state["run_id"] = run.id
        self.store.transition(run, RunStatus.RUNNING)
        self.store.claim(run, os.getpid())
        return await self._execute_steps(
            run,
            steps,
            state,
            checkpoint.next_step_index,
            parent_run_id,
            parent_event_id,
        )

    async def _execute_steps(
        self,
        run: WorkflowRun,
        steps: list[tuple[str, Step]],
        state: dict[str, Any],
        start_index: int,
        parent_run_id: UUID | None,
        parent_event_id: UUID | None,
    ) -> tuple[WorkflowRun, dict[str, Any]]:

        try:
            for step_index, (phase, step) in enumerate(steps[start_index:], start=start_index):
                if self._cancel_if_requested(run):
                    return run, state
                run.current_phase = phase
                self.store.save(run)
                occurrence_id = uuid4()
                started_at = now_utc()
                self.store.append_event(
                    PhaseEvent(
                        run_id=run.id,
                        workflow=run.workflow,
                        phase=phase,
                        status=PhaseStatus.RUNNING,
                        occurrence_id=occurrence_id,
                        parent_run_id=parent_run_id,
                        parent_event_id=parent_event_id,
                        input_artifact_ids=_artifact_ids(state.get("input_artifact_ids")),
                        started_at=started_at,
                    )
                )
                try:
                    updates = await step(state)
                    state.update(updates)
                except Exception as exc:
                    self.store.append_event(
                        PhaseEvent(
                            run_id=run.id,
                            workflow=run.workflow,
                            phase=phase,
                            status=PhaseStatus.FAILED,
                            occurrence_id=occurrence_id,
                            parent_run_id=parent_run_id,
                            parent_event_id=parent_event_id,
                            input_artifact_ids=_artifact_ids(state.get("input_artifact_ids")),
                            output_artifact_ids=_artifact_ids(state.get("output_artifact_ids")),
                            started_at=started_at,
                            completed_at=now_utc(),
                            error_code=type(exc).__name__,
                            error=str(exc),
                        )
                    )
                    raise
                self.store.append_event(
                    PhaseEvent(
                        run_id=run.id,
                        workflow=run.workflow,
                        phase=phase,
                        status=PhaseStatus.COMPLETED,
                        occurrence_id=occurrence_id,
                        parent_run_id=parent_run_id,
                        parent_event_id=parent_event_id,
                        input_artifact_ids=_artifact_ids(state.get("input_artifact_ids")),
                        output_artifact_ids=_artifact_ids(state.get("output_artifact_ids")),
                        started_at=started_at,
                        completed_at=now_utc(),
                    )
                )
                if self._cancel_if_requested(run):
                    return run, state
                self.store.save_checkpoint(
                    WorkflowCheckpoint(
                        run_id=run.id,
                        workflow=run.workflow,
                        next_step_index=step_index + 1,
                        context=_checkpoint_context(state),
                    )
                )
                human_review = state.pop("_human_review", None)
                if isinstance(human_review, dict):
                    request = HumanReviewRequest(
                        run_id=run.id,
                        phase=phase,
                        prompt=str(human_review.get("prompt", "Review required")),
                        artifact_ids=_artifact_ids(human_review.get("artifact_ids")),
                    )
                    self.store.create_human_review(request)
                    self.store.transition(run, RunStatus.WAITING_HUMAN)
                    state["human_review_request_id"] = str(request.id)
                    return run, state
        except (KeyboardInterrupt, SystemExit):
            self.store.transition(run, RunStatus.INTERRUPTED)
            self.store.release(run)
            return run, state
        except Exception as exc:
            self.store.transition(run, RunStatus.FAILED, error=str(exc))
            self.store.release(run)
            return run, state

        self.store.transition(run, RunStatus.SUCCEEDED, current_phase="DONE")
        self.store.release(run)
        self.store.clear_checkpoint(run.id)
        return run, state

    def _cancel_if_requested(self, run: WorkflowRun) -> bool:
        stored = self.store.get(run.id)
        if stored is None or stored.status is not RunStatus.CANCELLING:
            return False
        run.status = stored.status
        self.store.transition(run, RunStatus.CANCELLED)
        self.store.release(run)
        return True


def _artifact_ids(value: object) -> list[UUID]:
    if not isinstance(value, list):
        return []
    result: list[UUID] = []
    for item in value:
        if isinstance(item, UUID):
            result.append(item)
        elif isinstance(item, str):
            result.append(UUID(item))
    return result


def _checkpoint_context(
    state: dict[str, Any],
) -> dict[str, str | int | float | bool | None | list[str]]:
    context: dict[str, str | int | float | bool | None | list[str]] = {}
    for key, value in state.items():
        if key == "run_id":
            continue
        if value is None or isinstance(value, (str, int, float, bool)):
            context[key] = value
        elif isinstance(value, UUID):
            context[key] = str(value)
        elif isinstance(value, list) and all(isinstance(item, str) for item in value):
            context[key] = value
    return context
