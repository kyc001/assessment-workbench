import pytest
from pydantic import ValidationError

from assessment_workbench.domain import KnowledgeRelation, RelationKind


def test_relation_rejects_self_reference() -> None:
    with pytest.raises(ValidationError):
        KnowledgeRelation(source_id="same", target_id="same", kind=RelationKind.RELATED_TO)
