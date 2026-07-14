from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

import assessment_workbench.storage as storage_module
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


def test_editable_publish_retries_transient_permission_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = Workspace(tmp_path / "workspace")
    workspace.initialize()
    run = RunStore(workspace).create("editable-retry")
    store = ArtifactStore(workspace)
    original_replace = storage_module.os.replace
    attempts = 0

    def replace_after_contention(source: Path, destination: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError(5, "destination is temporarily busy", str(destination))
        original_replace(source, destination)

    monkeypatch.setattr(storage_module.os, "replace", replace_after_contention)
    monkeypatch.setattr(storage_module.time, "sleep", lambda _: None)

    path = store.write_editable_json(run.id, "review-runs.json", [{"status": "running"}])

    assert attempts == 3
    assert store.read_editable_json(run.id, "review-runs.json") == [{"status": "running"}]
    assert list((workspace.root / path).parent.glob(".editable-*")) == []


def test_editable_publish_cleans_temp_after_permission_retry_exhaustion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = Workspace(tmp_path / "workspace")
    workspace.initialize()
    run = RunStore(workspace).create("editable-retry-exhausted")
    store = ArtifactStore(workspace)
    attempts = 0

    def always_busy(_: Path, destination: Path) -> None:
        nonlocal attempts
        attempts += 1
        raise PermissionError(5, "destination remains busy", str(destination))

    monkeypatch.setattr(storage_module.os, "replace", always_busy)
    monkeypatch.setattr(storage_module.time, "sleep", lambda _: None)

    with pytest.raises(PermissionError, match="destination remains busy"):
        store.write_editable_json(run.id, "review-runs.json", [])

    editable_dir = workspace.root / "editable" / str(run.id)
    assert attempts == storage_module._EDITABLE_REPLACE_ATTEMPTS
    assert list(editable_dir.glob(".editable-*")) == []
