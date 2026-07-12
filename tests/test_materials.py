from pathlib import Path

from assessment_workbench.domain import MaterialKind, MaterialStatus
from assessment_workbench.ingestion import build_material
from assessment_workbench.storage import MaterialStore, Workspace


def test_material_captures_file_metadata_and_persists(tmp_path: Path) -> None:
    source = tmp_path / "lecture.pdf"
    source.write_bytes(b"%PDF-example")
    material = build_material(
        source,
        "physics-2026",
        MaterialKind.LECTURE,
        semester="spring",
        year=2026,
        language="zh-CN",
    )

    assert material.original_name == "lecture.pdf"
    assert material.mime_type == "application/pdf"
    assert material.size_bytes == len(b"%PDF-example")
    assert len(material.sha256) == 64
    assert material.status is MaterialStatus.REGISTERED

    workspace = Workspace(tmp_path / "workspace")
    workspace.initialize()
    store = MaterialStore(workspace)
    store.create(material)
    stored = store.get(material.id)

    assert stored == material
    assert store.list("physics-2026") == [material]
