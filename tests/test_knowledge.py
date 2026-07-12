from pathlib import Path

from assessment_workbench.domain import KnowledgePoint, SourceReference
from assessment_workbench.storage import LocalKnowledgeBackend, Workspace


def test_upsert_merges_evidence_and_searches(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "workspace")
    workspace.initialize()
    backend = LocalKnowledgeBackend(workspace)
    first = KnowledgePoint(
        id="physics:gauss",
        course_id="physics",
        name="高斯定律",
        slug="电磁学.高斯定律",
        description="描述电通量与包围电荷的关系",
        evidence=[SourceReference(document_id="d1", block_id="b1", page=3)],
    )
    second = first.model_copy(
        update={
            "tags": ["kind:law"],
            "evidence": [SourceReference(document_id="d2", block_id="b2", page=8)],
        }
    )
    backend.upsert_points([first, second])

    stored = backend.get_point("physics", "电磁学.高斯定律")
    assert stored is not None
    assert len(stored.evidence) == 2
    assert stored.tags == ["kind:law"]
    hits = backend.search("physics", "高斯定律")
    assert hits[0].point.slug == "电磁学.高斯定律"
