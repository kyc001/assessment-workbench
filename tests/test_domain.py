import pytest
from pydantic import ValidationError

from assessment_workbench.domain import (
    KnowledgeRelation,
    RelationKind,
    RunStatus,
    validate_run_transition,
)


def test_relation_rejects_self_reference() -> None:
    with pytest.raises(ValidationError):
        KnowledgeRelation(source_id="same", target_id="same", kind=RelationKind.RELATED_TO)


def test_run_status_rejects_illegal_transition() -> None:
    validate_run_transition(RunStatus.QUEUED, RunStatus.RUNNING)
    with pytest.raises(ValueError, match="invalid run status transition"):
        validate_run_transition(RunStatus.SUCCEEDED, RunStatus.RUNNING)
