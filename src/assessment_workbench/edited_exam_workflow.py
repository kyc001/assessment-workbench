from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

from assessment_workbench.document_workflow import (
    DOCUMENTS_BUILDING,
    DocumentBatchWorkflow,
    document_artifact_ids,
    latest_document_builds,
    parse_document_build_records,
    successful_document_builds,
)
from assessment_workbench.domain import (
    ArtifactRef,
    DocumentBuildRunRecord,
    ExamDocument,
    RunStatus,
    WorkflowCheckpoint,
    WorkflowRun,
)
from assessment_workbench.release import DOCUMENT_APPROVAL, RELEASE_BUNDLING, ReleaseBundleBuilder
from assessment_workbench.storage import ArtifactStore, RunStore
from assessment_workbench.workflow import Step, WorkflowEngine

EDITED_EXAM_ASSEMBLING = "EDITED_EXAM_ASSEMBLING"


class EditedExamAssemblyWorkflow:
    def __init__(
        self,
        documents: DocumentBatchWorkflow,
        artifacts: ArtifactStore,
        runs: RunStore,
    ) -> None:
        self.documents = documents
        self.artifacts = artifacts
        self.runs = runs
        self.engine = WorkflowEngine(runs)
        self.releases = ReleaseBundleBuilder(artifacts, runs)

    async def execute(
        self,
        exam: ExamDocument,
        *,
        source_parent_run_id: UUID,
        require_document_approval: bool = False,
        on_run_created: Callable[[WorkflowRun], None] | None = None,
    ) -> tuple[WorkflowRun, dict[str, Any]]:
        return await self._run(
            exam,
            source_parent_run_id=source_parent_run_id,
            require_document_approval=require_document_approval,
            on_run_created=on_run_created,
        )

    async def resume(self, run_id: UUID) -> tuple[WorkflowRun, dict[str, Any]]:
        checkpoint = self.runs.get_checkpoint(run_id)
        if checkpoint is None:
            raise ValueError(f"edited exam assembly has no checkpoint: {run_id}")
        restored = self._restore(checkpoint)
        exam = restored.get("exam")
        source_parent_run_id = restored.get("source_parent_run_id")
        require_document_approval = restored.get("require_document_approval")
        if not isinstance(exam, ExamDocument) or not isinstance(source_parent_run_id, UUID):
            raise ValueError("edited exam assembly checkpoint is missing its input contract")
        if not isinstance(require_document_approval, bool):
            raise ValueError("edited exam assembly checkpoint is missing its approval mode")
        return await self._run(
            exam,
            source_parent_run_id=source_parent_run_id,
            require_document_approval=require_document_approval,
            resume_run_id=run_id,
            restored_state=restored,
        )

    async def _run(
        self,
        exam: ExamDocument,
        *,
        source_parent_run_id: UUID,
        require_document_approval: bool,
        resume_run_id: UUID | None = None,
        restored_state: dict[str, Any] | None = None,
        on_run_created: Callable[[WorkflowRun], None] | None = None,
    ) -> tuple[WorkflowRun, dict[str, Any]]:
        async def assemble(state: dict[str, Any]) -> dict[str, Any]:
            exam_artifact = self.artifacts.write_json(
                state["run_id"],
                "exam.json",
                exam.model_dump(mode="json"),
                created_by_phase=EDITED_EXAM_ASSEMBLING,
            )
            bundle_artifacts = [
                self.artifacts.write_json(
                    state["run_id"],
                    f"question-{bundle.question.number:02d}-bundle.json",
                    bundle.model_dump(mode="json"),
                    created_by_phase=EDITED_EXAM_ASSEMBLING,
                )
                for bundle in exam.questions
            ]
            input_artifact = self.artifacts.write_json(
                state["run_id"],
                "edited-exam-input.json",
                {
                    "source_parent_run_id": str(source_parent_run_id),
                    "require_document_approval": require_document_approval,
                    "question_bundle_artifact_ids": [
                        str(artifact.id) for artifact in bundle_artifacts
                    ],
                },
                created_by_phase=EDITED_EXAM_ASSEMBLING,
            )
            return {
                "exam": exam,
                "exam_artifact": exam_artifact,
                "question_bundle_artifact_ids": [artifact.id for artifact in bundle_artifacts],
                "source_parent_run_id": str(source_parent_run_id),
                "require_document_approval": require_document_approval,
                "artifacts": [exam_artifact, *bundle_artifacts, input_artifact],
                "output_artifact_ids": [
                    exam_artifact.id,
                    *(artifact.id for artifact in bundle_artifacts),
                    input_artifact.id,
                ],
                "_checkpoint_artifacts": {
                    "exam": exam_artifact.id,
                    "edited_input": input_artifact.id,
                },
            }

        async def build_documents(state: dict[str, Any]) -> dict[str, Any]:
            restored_records = state.get("document_build_records")
            if restored_records is None:
                latest = self.artifacts.latest(state["run_id"], "document-build-runs.json")
                if latest is not None:
                    restored_records = self.artifacts.read_json(latest.id)
            bindings = state.get("_checkpoint_artifacts", {})
            outcome = await self.documents.execute(
                state["run_id"],
                state["exam"],
                restored_records,
                input_artifact_ids=list(bindings.values()),
            )
            output_ids = [
                outcome.manifest_artifact.id,
                *(
                    artifact_id
                    for record in outcome.current
                    for artifact_id in document_artifact_ids(record)
                ),
            ]
            updates: dict[str, Any] = {
                "document_build_records": outcome.records,
                "document_builds": outcome.current,
                "document_manifest_artifact": outcome.manifest_artifact,
                "output_artifact_ids": output_ids,
                "_checkpoint_artifacts": {
                    "document_manifest": outcome.manifest_artifact.id,
                },
                "_checkpoint_child_run_ids": outcome.child_run_ids,
            }
            if not outcome.succeeded:
                failed = [
                    record.view.value
                    for record in outcome.current
                    if record.status is not RunStatus.SUCCEEDED
                ]
                updates["_human_review"] = {
                    "prompt": (
                        "Edited exam document views failed. Inspect artifacts and retry only: "
                        f"{', '.join(failed)}."
                    ),
                    "artifact_ids": output_ids,
                    "resume_phase": DOCUMENTS_BUILDING,
                    "retry_phase": DOCUMENTS_BUILDING,
                }
            return updates

        async def approve_documents(state: dict[str, Any]) -> dict[str, Any]:
            if not require_document_approval:
                return {}
            manifest = state.get("document_manifest_artifact")
            builds = state.get("document_builds")
            if (
                not isinstance(manifest, ArtifactRef)
                or not isinstance(builds, list)
                or not successful_document_builds(builds)
            ):
                raise ValueError("edited document approval requires successful builds")
            return {
                "_human_review": {
                    "prompt": (
                        "Review every rendered edited-exam page for clipping, overlap, labels, "
                        "Chinese text, mathematical notation, content, solutions, and rubric."
                    ),
                    "artifact_ids": [
                        manifest.id,
                        *(
                            artifact_id
                            for record in builds
                            if isinstance(record, DocumentBuildRunRecord)
                            for artifact_id in document_artifact_ids(record)
                        ),
                    ],
                    "retry_phase": DOCUMENTS_BUILDING,
                }
            }

        async def release(state: dict[str, Any]) -> dict[str, Any]:
            builds = state.get("document_builds")
            if not isinstance(builds, list) or not successful_document_builds(builds):
                raise ValueError("edited exam release requires successful document builds")
            document_builds = [
                record for record in builds if isinstance(record, DocumentBuildRunRecord)
            ]
            exam_artifact = state.get("exam_artifact")
            bundle_ids = state.get("question_bundle_artifact_ids")
            if not isinstance(exam_artifact, ArtifactRef) or not isinstance(bundle_ids, list):
                raise ValueError("edited exam release is missing content artifacts")
            acceptance: ArtifactRef | None = None
            if require_document_approval:
                manifest = state.get("document_manifest_artifact")
                if not isinstance(manifest, ArtifactRef):
                    raise ValueError("edited exam release is missing its approved manifest")
                acceptance = self.releases.write_acceptance(
                    state["run_id"],
                    manifest_artifact_id=manifest.id,
                    document_builds=document_builds,
                )
            bundle, release_artifact = self.releases.build(
                state["run_id"],
                state["exam"],
                exam_artifact_id=exam_artifact.id,
                question_bundle_artifact_ids=bundle_ids,
                document_builds=document_builds,
                acceptance_artifact_id=acceptance.id if acceptance is not None else None,
            )
            output_ids = [
                exam_artifact.id,
                *bundle_ids,
                *(
                    artifact_id
                    for record in document_builds
                    for artifact_id in document_artifact_ids(record)
                ),
                acceptance.id if acceptance is not None else None,
                release_artifact.id,
            ]
            output_artifacts = [
                artifact
                for artifact_id in output_ids
                for artifact in [self.artifacts.get(artifact_id)]
                if artifact is not None
            ]
            checkpoint_updates = {"release_bundle": release_artifact.id}
            if acceptance is not None:
                checkpoint_updates["document_acceptance"] = acceptance.id
            return {
                "release_bundle": bundle,
                "release_bundle_artifact": release_artifact,
                "artifacts": output_artifacts,
                "output_artifact_ids": output_ids,
                "_checkpoint_artifacts": checkpoint_updates,
            }

        steps: list[tuple[str, Step]] = [
            (EDITED_EXAM_ASSEMBLING, assemble),
            (DOCUMENTS_BUILDING, build_documents),
            (DOCUMENT_APPROVAL, approve_documents),
            (RELEASE_BUNDLING, release),
        ]
        if resume_run_id is not None:
            return await self.engine.resume(
                resume_run_id,
                "exam_edited_assembly",
                steps,
                context=restored_state,
                parent_run_id=source_parent_run_id,
            )
        return await self.engine.execute(
            "exam_edited_assembly",
            steps,
            parent_run_id=source_parent_run_id,
            on_run_created=on_run_created,
        )

    def _restore(self, checkpoint: WorkflowCheckpoint) -> dict[str, Any]:
        exam_id = checkpoint.artifact_bindings.get("exam")
        input_id = checkpoint.artifact_bindings.get("edited_input")
        if exam_id is None or input_id is None:
            raise ValueError("edited exam assembly uses a legacy checkpoint")
        exam = ExamDocument.model_validate(self.artifacts.read_json(exam_id))
        input_payload = self.artifacts.read_json(input_id)
        if not isinstance(input_payload, dict):
            raise ValueError("edited exam input artifact is not an object")
        approval_mode = input_payload.get("require_document_approval")
        if not isinstance(approval_mode, bool):
            raise ValueError("edited exam input artifact has an invalid approval mode")
        bundle_ids = [UUID(str(value)) for value in input_payload["question_bundle_artifact_ids"]]
        exam_artifact = self.artifacts.get(exam_id)
        if exam_artifact is None:
            raise ValueError("edited exam artifact metadata is missing")
        state: dict[str, Any] = {
            "exam": exam,
            "exam_artifact": exam_artifact,
            "question_bundle_artifact_ids": bundle_ids,
            "source_parent_run_id": UUID(str(input_payload["source_parent_run_id"])),
            "require_document_approval": approval_mode,
            "_checkpoint_artifacts": dict(checkpoint.artifact_bindings),
            "_checkpoint_child_run_ids": list(checkpoint.child_run_ids),
            "input_artifact_ids": list(checkpoint.artifact_bindings.values()),
            "artifacts": [exam_artifact],
        }
        manifest_id = checkpoint.artifact_bindings.get("document_manifest")
        if manifest_id is not None:
            records = parse_document_build_records(self.artifacts.read_json(manifest_id))
            state["document_build_records"] = records
            state["document_builds"] = latest_document_builds(records)
            manifest = self.artifacts.get(manifest_id)
            if manifest is None:
                raise ValueError("document manifest artifact metadata is missing")
            state["document_manifest_artifact"] = manifest
        return state
