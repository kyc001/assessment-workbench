from __future__ import annotations

from uuid import UUID

from assessment_workbench.domain import (
    ArtifactRef,
    DocumentAcceptanceRecord,
    DocumentBuildRunRecord,
    ExamDocument,
    ExamReleaseBundle,
    HumanDecisionType,
    ReleaseArtifactBinding,
    ReleaseLevel,
    RunStatus,
)
from assessment_workbench.exam_quality import exam_bundle_signature
from assessment_workbench.storage import ArtifactStore, RunStore

RELEASE_BUNDLING = "RELEASE_BUNDLING"
DOCUMENT_APPROVAL = "DOCUMENT_APPROVAL"


class ReleaseBundleBuilder:
    def __init__(self, artifacts: ArtifactStore, runs: RunStore) -> None:
        self.artifacts = artifacts
        self.runs = runs

    def write_acceptance(
        self,
        root_run_id: UUID,
        *,
        manifest_artifact_id: UUID,
        document_builds: list[DocumentBuildRunRecord],
    ) -> ArtifactRef:
        decision = self.runs.latest_human_decision(root_run_id)
        if decision is None or decision.decision not in {
            HumanDecisionType.ACCEPT,
            HumanDecisionType.EDIT_ACCEPT,
        }:
            raise ValueError("document acceptance requires an approving human decision")
        page_artifact_ids = [
            artifact_id for record in document_builds for artifact_id in record.page_artifact_ids
        ]
        acceptance = DocumentAcceptanceRecord(
            decision_id=decision.id,
            actor=decision.actor,
            reason=decision.reason,
            manifest_artifact_id=manifest_artifact_id,
            page_artifact_ids=page_artifact_ids,
        )
        return self.artifacts.write_json(
            root_run_id,
            "document-acceptance.json",
            acceptance.model_dump(mode="json"),
            created_by_phase=DOCUMENT_APPROVAL,
        )

    def build(
        self,
        root_run_id: UUID,
        exam: ExamDocument,
        *,
        exam_artifact_id: UUID,
        question_bundle_artifact_ids: list[UUID],
        document_builds: list[DocumentBuildRunRecord],
        acceptance_artifact_id: UUID | None,
    ) -> tuple[ExamReleaseBundle, ArtifactRef]:
        if len(question_bundle_artifact_ids) != len(exam.questions):
            raise ValueError("release requires one question Bundle artifact per exam question")
        if any(record.status is not RunStatus.SUCCEEDED for record in document_builds):
            raise ValueError("release requires successful document builds")

        run_ids = [root_run_id, *self.runs.descendant_run_ids(root_run_id)]
        artifact_refs = [artifact for run_id in run_ids for artifact in self.artifacts.list(run_id)]
        bindings: list[ReleaseArtifactBinding] = []
        for artifact in artifact_refs:
            self.artifacts.read_bytes(artifact.id)
            bindings.append(_release_binding(artifact))
        artifact_ids = {binding.artifact_id for binding in bindings}
        required = {exam_artifact_id, *question_bundle_artifact_ids}
        if acceptance_artifact_id is not None:
            required.add(acceptance_artifact_id)
        if not required <= artifact_ids:
            missing = sorted(str(value) for value in required - artifact_ids)
            raise ValueError(f"release references artifacts outside the run tree: {missing}")

        review_ids = [
            artifact.id
            for artifact in artifact_refs
            if "review" in artifact.logical_name.casefold()
        ]
        arbitration_ids = [
            artifact.id
            for artifact in artifact_refs
            if any(
                marker in artifact.logical_name.casefold()
                for marker in ("arbitration", "arbiter", "decision")
            )
        ]
        context_ids = [
            artifact.id
            for artifact in artifact_refs
            if artifact.logical_name == "model-context.json"
        ]
        bundle = ExamReleaseBundle(
            root_run_id=root_run_id,
            exam_id=exam.id,
            exam_signature=exam_bundle_signature(exam),
            release_level=(
                ReleaseLevel.HUMAN_VERIFIED
                if acceptance_artifact_id is not None
                else ReleaseLevel.MACHINE_VERIFIED
            ),
            run_ids=run_ids,
            model_call_ids=[call.id for call in self.runs.model_calls(run_ids)],
            exam_artifact_id=exam_artifact_id,
            question_bundle_artifact_ids=question_bundle_artifact_ids,
            review_artifact_ids=review_ids,
            arbitration_artifact_ids=arbitration_ids,
            context_pack_artifact_ids=context_ids,
            document_builds=document_builds,
            acceptance_artifact_id=acceptance_artifact_id,
            artifacts=bindings,
        )
        artifact = self.artifacts.write_json(
            root_run_id,
            "exam-release-bundle.json",
            bundle.model_dump(mode="json"),
            created_by_phase=RELEASE_BUNDLING,
        )
        return bundle, artifact


def _release_binding(artifact: ArtifactRef) -> ReleaseArtifactBinding:
    return ReleaseArtifactBinding(
        artifact_id=artifact.id,
        run_id=artifact.run_id,
        logical_name=artifact.logical_name,
        version=artifact.version,
        media_type=artifact.media_type,
        sha256=artifact.sha256,
        size_bytes=artifact.size_bytes,
    )
