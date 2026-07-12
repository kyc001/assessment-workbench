from pathlib import Path
from uuid import uuid4

from assessment_workbench.storage import ArtifactStore, RunStore, Workspace


def test_artifacts_are_atomic_versioned_and_verified(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "workspace")
    workspace.initialize()
    run = RunStore(workspace).create("artifact-test")
    store = ArtifactStore(workspace)

    first = store.write_json(run.id, "result.json", {"version": 1}, created_by_phase="WRITE")
    second = store.write_json(run.id, "result.json", {"version": 2}, created_by_phase="WRITE")

    assert first.version == 1
    assert second.version == 2
    assert first.path != second.path
    assert store.verify(first.id)
    assert store.verify(second.id)
    assert b'"version": 1' in store.read_bytes(first.id)
    assert [artifact.version for artifact in store.list(run.id)] == [1, 2]


def test_artifact_integrity_detects_tampering(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "workspace")
    workspace.initialize()
    run = RunStore(workspace).create("artifact-test")
    store = ArtifactStore(workspace)
    artifact = store.write_bytes(run.id, "answer.txt", b"original", media_type="text/plain")
    (workspace.root / artifact.path).write_bytes(b"changed")

    assert not store.verify(artifact.id)
    assert not store.verify(uuid4())
