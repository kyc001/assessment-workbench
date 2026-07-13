from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

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


def test_concurrent_artifact_writers_receive_unique_versions(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "workspace")
    workspace.initialize()
    run = RunStore(workspace).create("artifact-concurrency")
    store = ArtifactStore(workspace)

    def write(index: int) -> int:
        return store.write_json(run.id, "shared.json", {"index": index}).version

    with ThreadPoolExecutor(max_workers=6) as executor:
        versions = list(executor.map(write, range(12)))

    assert sorted(versions) == list(range(1, 13))
    assert all(store.verify(artifact.id) for artifact in store.list(run.id))


def test_artifact_metadata_failure_removes_published_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = Workspace(tmp_path / "workspace")
    workspace.initialize()
    run = RunStore(workspace).create("artifact-failure")
    store = ArtifactStore(workspace)

    def fail_insert(*_: Any) -> None:
        raise RuntimeError("injected metadata failure")

    monkeypatch.setattr(store, "_insert_artifact", fail_insert)
    with pytest.raises(RuntimeError, match="injected metadata failure"):
        store.write_json(run.id, "result.json", {"value": 1})

    assert store.list(run.id) == []
    assert list((workspace.artifacts / str(run.id)).iterdir()) == []


def test_artifact_reconcile_removes_only_recognized_orphans(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "workspace")
    workspace.initialize()
    run = RunStore(workspace).create("artifact-reconcile")
    store = ArtifactStore(workspace)
    kept = store.write_json(run.id, "result.json", {"value": 1})
    run_dir = workspace.artifacts / str(run.id)
    orphan = run_dir / "result.v2.json"
    temporary = run_dir / ".artifact-crashed"
    unknown = run_dir / "notes.txt"
    orphan.write_text("orphan", encoding="utf-8")
    temporary.write_text("temporary", encoding="utf-8")
    unknown.write_text("keep", encoding="utf-8")

    removed = store.reconcile()

    assert removed == [
        temporary.relative_to(workspace.root),
        orphan.relative_to(workspace.root),
    ]
    assert (workspace.root / kept.path).exists()
    assert unknown.exists()
