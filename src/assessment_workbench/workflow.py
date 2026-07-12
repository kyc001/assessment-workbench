from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID, uuid4

from assessment_workbench.domain import (
    PhaseEvent,
    PhaseStatus,
    RunStatus,
    WorkflowRun,
    now_utc,
)
from assessment_workbench.storage import RunStore

Step = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


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
    ) -> tuple[WorkflowRun, dict[str, Any]]:
        run = self.store.create(workflow)
        state = dict(context or {})
        state["run_id"] = run.id
        run.status = RunStatus.RUNNING
        self.store.save(run)

        try:
            for phase, step in steps:
                run.current_phase = phase
                self.store.save(run)
                occurrence_id = uuid4()
                started_at = now_utc()
                self.store.append_event(
                    PhaseEvent(
                        run_id=run.id,
                        workflow=workflow,
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
                            workflow=workflow,
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
                        workflow=workflow,
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
        except Exception as exc:
            run.status = RunStatus.FAILED
            run.error = str(exc)
            self.store.save(run)
            return run, state

        run.status = RunStatus.SUCCEEDED
        run.current_phase = "DONE"
        self.store.save(run)
        return run, state


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
