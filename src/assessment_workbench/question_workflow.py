from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from assessment_workbench.capabilities import CapabilityCatalog
from assessment_workbench.domain import (
    ArbitrationAction,
    ArbitrationDecision,
    ArtifactRef,
    ExamQuestionBundle,
    FindingSeverity,
    FindingTarget,
    GenerationMetadata,
    QuestionDraft,
    QuestionGenerationRequest,
    QuestionVersion,
    QuestionWorkflowState,
    ReviewReport,
    RubricDraft,
    RubricVersion,
    SolutionDraft,
    SolutionVersion,
    WorkflowCheckpoint,
    WorkflowRun,
)
from assessment_workbench.ports import StructuredModel
from assessment_workbench.prompting import (
    complete_with_prompt,
    context_artifact_ids,
    json_prompt,
)
from assessment_workbench.review_workflow import (
    ReviewerPoolWorkflow,
    parse_review_records,
)
from assessment_workbench.storage import ArtifactStore, RunStore
from assessment_workbench.workflow import Step, WorkflowEngine

QUESTION_INITIALIZING = "QUESTION_INITIALIZING"
PROBLEM_GENERATING = "PROBLEM_GENERATING"
SOLUTION_GENERATING = "SOLUTION_GENERATING"
RUBRIC_GENERATING = "RUBRIC_GENERATING"
REVIEWS_GENERATING = "REVIEWS_GENERATING"
ARBITRATING = "ARBITRATING"
QUESTION_FINALIZING = "QUESTION_FINALIZING"


class MissingReviewReportsError(ValueError):
    pass


@dataclass(frozen=True)
class ModelRouter:
    standard: StructuredModel
    strong: StructuredModel


class QuestionAgentWorkflow:
    def __init__(
        self,
        models: ModelRouter,
        artifacts: ArtifactStore,
        runs: RunStore,
        capabilities: CapabilityCatalog,
        *,
        max_question_attempts: int,
        max_total_question_rounds: int,
        max_draft_validation_attempts: int,
        max_reviewer_attempts: int = 2,
    ) -> None:
        if max_reviewer_attempts < 1:
            raise ValueError("max_reviewer_attempts must be at least 1")
        self.models = models
        self.artifacts = artifacts
        self.runs = runs
        self.capabilities = capabilities
        self.prompts = capabilities.prompts
        self.max_question_attempts = max_question_attempts
        self.max_total_question_rounds = max_total_question_rounds
        self.max_draft_validation_attempts = max_draft_validation_attempts
        self.reviewer_workflow = ReviewerPoolWorkflow(
            models.strong,
            artifacts,
            runs,
            capabilities,
            max_attempts=max_reviewer_attempts,
        )

    async def execute(
        self,
        request: QuestionGenerationRequest,
        *,
        on_run_created: Callable[[WorkflowRun], None] | None = None,
    ) -> tuple[WorkflowRun, dict[str, Any]]:
        return await self._run_request(request, on_run_created=on_run_created)

    async def resume(self, run_id: UUID) -> tuple[WorkflowRun, dict[str, Any]]:
        checkpoint = self.runs.get_checkpoint(run_id)
        if checkpoint is None:
            raise ValueError(f"run has no checkpoint: {run_id}")
        if not checkpoint.artifact_bindings:
            raise ValueError(f"run uses a legacy checkpoint without artifact bindings: {run_id}")
        request_artifact_id = checkpoint.artifact_bindings.get("request")
        if request_artifact_id is None:
            artifact = self.artifacts.latest(run_id, "question-request.json")
            if artifact is None:
                raise ValueError(f"run has no question request artifact: {run_id}")
            request_artifact_id = artifact.id
        request = QuestionGenerationRequest.model_validate(
            self.artifacts.read_json(request_artifact_id)
        )
        return await self._run_request(
            request,
            resume_run_id=run_id,
            restored_state=self._restore_state(checkpoint),
        )

    async def _run_request(
        self,
        request: QuestionGenerationRequest,
        *,
        resume_run_id: UUID | None = None,
        restored_state: dict[str, Any] | None = None,
        on_run_created: Callable[[WorkflowRun], None] | None = None,
    ) -> tuple[WorkflowRun, dict[str, Any]]:
        capability_validators = self._validate_request(request)

        async def initialize(state: dict[str, Any]) -> dict[str, Any]:
            request_artifact = self.artifacts.write_json(
                state["run_id"],
                "question-request.json",
                request.model_dump(mode="json"),
                created_by_phase=QUESTION_INITIALIZING,
            )
            plan_artifact = self.artifacts.write_json(
                state["run_id"],
                "question-plan.json",
                request.plan.model_dump(mode="json"),
                created_by_phase=QUESTION_INITIALIZING,
            )
            question_state = QuestionWorkflowState(writer_feedback=request.generation_feedback)
            state_artifact = self._write_state(
                state["run_id"], question_state, QUESTION_INITIALIZING
            )
            return {
                "question_state": question_state,
                "output_artifact_ids": [
                    request_artifact.id,
                    plan_artifact.id,
                    state_artifact.id,
                ],
                "_checkpoint_artifacts": {
                    "request": request_artifact.id,
                    "plan": plan_artifact.id,
                    "question_state": state_artifact.id,
                },
            }

        async def generate_problem(state: dict[str, Any]) -> dict[str, Any]:
            question_state: QuestionWorkflowState = state["question_state"]
            previous = state.get("question")
            previous_question = previous if isinstance(previous, QuestionVersion) else None
            prompt = self.prompts.require("question_writer")
            feedback = list(question_state.writer_feedback)
            for validation_attempt in range(self.max_draft_validation_attempts):
                draft = await complete_with_prompt(
                    self.models.standard,
                    prompt=prompt,
                    user_prompt=json_prompt(
                        profile=request.profile.model_dump(mode="json"),
                        blueprint_id=request.blueprint.id,
                        question_plan=request.plan.model_dump(mode="json"),
                        source_context=request.source_context,
                        revision_feedback=feedback,
                        capability_context=request.capability_context,
                    ),
                    response_model=QuestionDraft,
                    run_id=state["run_id"],
                    artifacts=self.artifacts,
                    created_by_phase=PROBLEM_GENERATING,
                    input_artifact_ids=context_artifact_ids(state),
                )
                try:
                    question = QuestionVersion(
                        question_id=question_state.question_id,
                        version=(previous_question.version + 1) if previous_question else 1,
                        parent_version_id=(previous_question.id if previous_question else None),
                        number=request.plan.number,
                        section_id=request.plan.section_id,
                        section_title=request.plan.section_title,
                        question_type=request.plan.question_type,
                        score=request.plan.score,
                        metadata=GenerationMetadata(
                            role=prompt.role,
                            model="routed",
                            prompt_version=prompt.version,
                            plan_id=request.plan.id,
                        ),
                        **draft.model_dump(),
                    )
                    break
                except ValidationError as exc:
                    if validation_attempt == self.max_draft_validation_attempts - 1:
                        raise
                    feedback = [_domain_validation_feedback("question", exc)]
            else:
                raise RuntimeError("question validation loop exited unexpectedly")
            artifact = self.artifacts.write_json(
                state["run_id"],
                f"questions/{request.plan.number:02d}/question.json",
                question.model_dump(mode="json"),
                created_by_phase=PROBLEM_GENERATING,
            )
            return {
                "question": question,
                "output_artifact_ids": [artifact.id],
                "_checkpoint_artifacts": {"question": artifact.id},
            }

        async def generate_solution(state: dict[str, Any]) -> dict[str, Any]:
            question_state: QuestionWorkflowState = state["question_state"]
            question: QuestionVersion = state["question"]
            previous = state.get("solution")
            previous_solution = previous if isinstance(previous, SolutionVersion) else None
            prompt = self.prompts.require("independent_solver")
            feedback = list(question_state.solver_feedback)
            for validation_attempt in range(self.max_draft_validation_attempts):
                draft = await complete_with_prompt(
                    self.models.strong,
                    prompt=prompt,
                    user_prompt=json_prompt(
                        question=question.model_dump(mode="json"),
                        question_plan=request.plan.model_dump(mode="json"),
                        source_context=request.source_context,
                        revision_feedback=feedback,
                        capability_context=request.capability_context,
                    ),
                    response_model=SolutionDraft,
                    run_id=state["run_id"],
                    artifacts=self.artifacts,
                    created_by_phase=SOLUTION_GENERATING,
                    input_artifact_ids=context_artifact_ids(state),
                )
                try:
                    solution = SolutionVersion(
                        solution_id=question_state.solution_id,
                        question_version_id=question.id,
                        version=(previous_solution.version + 1) if previous_solution else 1,
                        parent_version_id=(previous_solution.id if previous_solution else None),
                        metadata=GenerationMetadata(
                            role=prompt.role,
                            model="routed",
                            prompt_version=prompt.version,
                            plan_id=request.plan.id,
                        ),
                        **draft.model_dump(),
                    )
                    break
                except ValidationError as exc:
                    if validation_attempt == self.max_draft_validation_attempts - 1:
                        raise
                    feedback = [_domain_validation_feedback("solution", exc)]
            else:
                raise RuntimeError("solution validation loop exited unexpectedly")
            artifact = self.artifacts.write_json(
                state["run_id"],
                f"questions/{request.plan.number:02d}/solution.json",
                solution.model_dump(mode="json"),
                created_by_phase=SOLUTION_GENERATING,
            )
            return {
                "solution": solution,
                "output_artifact_ids": [artifact.id],
                "_checkpoint_artifacts": {"solution": artifact.id},
            }

        async def generate_rubric(state: dict[str, Any]) -> dict[str, Any]:
            question_state: QuestionWorkflowState = state["question_state"]
            question: QuestionVersion = state["question"]
            solution: SolutionVersion = state["solution"]
            previous = state.get("rubric")
            previous_rubric = previous if isinstance(previous, RubricVersion) else None
            prompt = self.prompts.require("rubric_builder")
            feedback = list(question_state.rubric_feedback)
            for validation_attempt in range(self.max_draft_validation_attempts):
                draft = await complete_with_prompt(
                    self.models.standard,
                    prompt=prompt,
                    user_prompt=json_prompt(
                        question=question.model_dump(mode="json"),
                        solution=solution.model_dump(mode="json"),
                        question_plan=request.plan.model_dump(mode="json"),
                        score=request.plan.score,
                        revision_feedback=feedback,
                        capability_context=request.capability_context,
                    ),
                    response_model=RubricDraft,
                    run_id=state["run_id"],
                    artifacts=self.artifacts,
                    created_by_phase=RUBRIC_GENERATING,
                    input_artifact_ids=context_artifact_ids(state),
                )
                try:
                    rubric = RubricVersion(
                        rubric_id=question_state.rubric_id,
                        question_version_id=question.id,
                        solution_version_id=solution.id,
                        version=(previous_rubric.version + 1) if previous_rubric else 1,
                        parent_version_id=(previous_rubric.id if previous_rubric else None),
                        max_score=request.plan.score,
                        metadata=GenerationMetadata(
                            role=prompt.role,
                            model="routed",
                            prompt_version=prompt.version,
                            plan_id=request.plan.id,
                        ),
                        **draft.model_dump(),
                    )
                    break
                except ValidationError as exc:
                    if validation_attempt == self.max_draft_validation_attempts - 1:
                        raise
                    feedback = [_domain_validation_feedback("rubric", exc)]
            else:
                raise RuntimeError("rubric validation loop exited unexpectedly")
            artifact = self.artifacts.write_json(
                state["run_id"],
                f"questions/{request.plan.number:02d}/rubric.json",
                rubric.model_dump(mode="json"),
                created_by_phase=RUBRIC_GENERATING,
            )
            return {
                "rubric": rubric,
                "output_artifact_ids": [artifact.id],
                "_checkpoint_artifacts": {"rubric": artifact.id},
            }

        async def generate_reviews(state: dict[str, Any]) -> dict[str, Any]:
            bundle = self._bundle_from_state(state, request, capability_validators)
            outcome = await self.reviewer_workflow.execute(
                state["run_id"],
                request,
                bundle,
                state.get("review_records"),
                input_artifact_ids=context_artifact_ids(state),
            )
            reports_artifact = self.artifacts.write_json(
                state["run_id"],
                f"questions/{request.plan.number:02d}/reviews-pre-arbitration.json",
                [report.model_dump(mode="json") for report in outcome.reports],
                created_by_phase=REVIEWS_GENERATING,
            )
            compatibility_artifact = self.artifacts.write_json(
                state["run_id"],
                f"questions/{request.plan.number:02d}/reviews.json",
                [report.model_dump(mode="json") for report in outcome.reports],
                created_by_phase=REVIEWS_GENERATING,
            )
            return {
                "reports": outcome.reports,
                "review_records": outcome.records,
                "output_artifact_ids": [
                    outcome.manifest_artifact.id,
                    reports_artifact.id,
                    compatibility_artifact.id,
                ],
                "_checkpoint_artifacts": {
                    "review_manifest": outcome.manifest_artifact.id,
                    "reports": reports_artifact.id,
                },
                "_checkpoint_child_run_ids": outcome.child_run_ids,
            }

        async def arbitrate(state: dict[str, Any]) -> dict[str, Any]:
            bundle = self._bundle_from_state(state, request, capability_validators)
            try:
                reports = self._reports_from_state(
                    state,
                    bundle,
                    request.profile.reviewers,
                )
            except MissingReviewReportsError:
                return {"_next_phase": REVIEWS_GENERATING}
            decision = await self._arbitrate(
                state["run_id"],
                request,
                bundle,
                reports,
                context_artifact_ids(state),
            )
            decision_artifact = self.artifacts.write_json(
                state["run_id"],
                f"questions/{request.plan.number:02d}/arbitration.json",
                decision.model_dump(mode="json"),
                created_by_phase=ARBITRATING,
            )
            question_state: QuestionWorkflowState = state["question_state"]
            question_state, next_phase = self._route_decision(question_state, decision)
            state_artifact = self._write_state(state["run_id"], question_state, ARBITRATING)
            return {
                "decision": decision,
                "question_state": question_state,
                "output_artifact_ids": [decision_artifact.id, state_artifact.id],
                "_checkpoint_artifacts": {
                    "decision": decision_artifact.id,
                    "question_state": state_artifact.id,
                },
                "_next_phase": next_phase,
            }

        async def finalize(state: dict[str, Any]) -> dict[str, Any]:
            bundle = self._bundle_from_state(state, request, capability_validators)
            artifact = self.artifacts.write_json(
                state["run_id"],
                "question-bundle.json",
                bundle.model_dump(mode="json"),
                created_by_phase=QUESTION_FINALIZING,
            )
            question_state: QuestionWorkflowState = state["question_state"]
            return {
                "bundle": bundle,
                "bundle_artifact": artifact,
                "requires_human_review": question_state.requires_human_review,
                "output_artifact_ids": [artifact.id],
                "_checkpoint_artifacts": {"bundle": artifact.id},
            }

        steps: list[tuple[str, Step]] = [
            (QUESTION_INITIALIZING, initialize),
            (PROBLEM_GENERATING, generate_problem),
            (SOLUTION_GENERATING, generate_solution),
            (RUBRIC_GENERATING, generate_rubric),
            (REVIEWS_GENERATING, generate_reviews),
            (ARBITRATING, arbitrate),
            (QUESTION_FINALIZING, finalize),
        ]
        engine = WorkflowEngine(self.runs)
        if resume_run_id is not None:
            return await engine.resume(
                resume_run_id,
                "exam_question_generation",
                steps,
                context=restored_state,
                parent_run_id=request.parent_run_id,
            )
        return await engine.execute(
            "exam_question_generation",
            steps,
            parent_run_id=request.parent_run_id,
            on_run_created=on_run_created,
        )

    def _validate_request(self, request: QuestionGenerationRequest) -> list[str]:
        capability_validators: list[str] = []
        if request.capability_id is not None:
            assert request.capability_version is not None
            capability = self.capabilities.require_subject_binding(
                request.capability_id,
                request.capability_version,
                request.profile,
                request.blueprint,
                request.capability_context,
            )
            capability_validators = capability.validators
        self.capabilities.validate_profile(request.profile, capability_validators)
        self.capabilities.validate_blueprint(
            request.profile,
            request.blueprint,
            capability_validators,
        )
        return capability_validators

    def _restore_state(self, checkpoint: WorkflowCheckpoint) -> dict[str, Any]:
        bindings = checkpoint.artifact_bindings
        state: dict[str, Any] = {
            "_checkpoint_artifacts": dict(bindings),
            "_checkpoint_child_run_ids": list(checkpoint.child_run_ids),
            "input_artifact_ids": list(bindings.values()),
        }

        def payload(key: str) -> object | None:
            artifact_id = bindings.get(key)
            return self.artifacts.read_json(artifact_id) if artifact_id is not None else None

        question_state_payload = payload("question_state")
        if question_state_payload is None:
            raise ValueError("question run uses a legacy checkpoint without stage state")
        state["question_state"] = QuestionWorkflowState.model_validate(question_state_payload)
        question_payload = payload("question")
        if question_payload is not None:
            state["question"] = QuestionVersion.model_validate(question_payload)
        solution_payload = payload("solution")
        if solution_payload is not None:
            state["solution"] = SolutionVersion.model_validate(solution_payload)
        rubric_payload = payload("rubric")
        if rubric_payload is not None:
            state["rubric"] = RubricVersion.model_validate(rubric_payload)
        manifest_payload = payload("review_manifest")
        if manifest_payload is not None:
            state["review_records"] = parse_review_records(manifest_payload)
        reports_payload = payload("reports")
        if reports_payload is not None:
            if not isinstance(reports_payload, list):
                raise ValueError("review report artifact is not a list")
            state["reports"] = [ReviewReport.model_validate(item) for item in reports_payload]
        decision_payload = payload("decision")
        if decision_payload is not None:
            state["decision"] = ArbitrationDecision.model_validate(decision_payload)
        bundle_payload = payload("bundle")
        if bundle_payload is not None:
            bundle = ExamQuestionBundle.model_validate(bundle_payload)
            artifact = self.artifacts.get(bindings["bundle"])
            if artifact is None:
                raise ValueError("question bundle artifact metadata is missing")
            state["bundle"] = bundle
            state["bundle_artifact"] = artifact
        return state

    def _reports_from_state(
        self,
        state: dict[str, Any],
        bundle: ExamQuestionBundle,
        reviewer_names: list[str],
    ) -> list[ReviewReport]:
        records = state.get("review_records")
        if not isinstance(records, list):
            raise MissingReviewReportsError("question state has no reviewer manifest")
        loaded = self.reviewer_workflow.load_matching_reports(
            records,
            bundle,
            reviewer_names,
        )
        missing = [name for name in reviewer_names if name not in loaded]
        if missing:
            raise MissingReviewReportsError(f"question state is missing review reports: {missing}")
        return [loaded[name] for name in reviewer_names]

    async def _arbitrate(
        self,
        run_id: UUID,
        request: QuestionGenerationRequest,
        bundle: ExamQuestionBundle,
        reports: list[ReviewReport],
        input_artifact_ids: list[UUID],
    ) -> ArbitrationDecision:
        prompt = self.prompts.require("question_arbiter")
        decision = await complete_with_prompt(
            self.models.strong,
            prompt=prompt,
            user_prompt=json_prompt(
                question_plan=request.plan.model_dump(mode="json"),
                bundle=bundle.model_dump(mode="json"),
                reports=[report.model_dump(mode="json") for report in reports],
                capability_context=request.capability_context,
            ),
            response_model=ArbitrationDecision,
            run_id=run_id,
            artifacts=self.artifacts,
            created_by_phase=ARBITRATING,
            input_artifact_ids=input_artifact_ids,
        )
        fatal_findings = [
            finding
            for report in reports
            for finding in report.findings
            if finding.severity is FindingSeverity.FATAL
        ]
        if fatal_findings and decision.action in {
            ArbitrationAction.PASS,
            ArbitrationAction.PASS_WITH_WARNINGS,
        }:
            if any(
                finding.target in {FindingTarget.QUESTION, FindingTarget.BUNDLE}
                for finding in fatal_findings
            ):
                action = ArbitrationAction.RETRY_PROBLEM
            elif any(finding.target is FindingTarget.SOLUTION for finding in fatal_findings):
                action = ArbitrationAction.RETRY_SOLUTION
            else:
                action = ArbitrationAction.RETRY_RUBRIC
            return ArbitrationDecision(
                action=action,
                rationale=(
                    "Deterministic review gate overrode an invalid PASS decision because "
                    "fatal findings remained unresolved."
                ),
                finding_codes=[finding.code for finding in fatal_findings],
                writer_feedback=[
                    finding.message
                    for finding in fatal_findings
                    if finding.target in {FindingTarget.QUESTION, FindingTarget.BUNDLE}
                ],
                solver_feedback=[
                    finding.message
                    for finding in fatal_findings
                    if finding.target is FindingTarget.SOLUTION
                ],
                rubric_feedback=[
                    finding.message
                    for finding in fatal_findings
                    if finding.target is FindingTarget.RUBRIC
                ],
            )
        return decision

    def _route_decision(
        self,
        state: QuestionWorkflowState,
        decision: ArbitrationDecision,
    ) -> tuple[QuestionWorkflowState, str]:
        values = state.model_dump()
        values.update(
            last_action=decision.action,
            writer_feedback=decision.writer_feedback,
            solver_feedback=decision.solver_feedback,
            rubric_feedback=decision.rubric_feedback,
        )
        if decision.action in {ArbitrationAction.PASS, ArbitrationAction.PASS_WITH_WARNINGS}:
            if decision.action is ArbitrationAction.PASS_WITH_WARNINGS:
                values["requires_human_review"] = True
            return QuestionWorkflowState.model_validate(values), QUESTION_FINALIZING
        if decision.action is ArbitrationAction.ESCALATE_HUMAN:
            values["requires_human_review"] = True
            return QuestionWorkflowState.model_validate(values), QUESTION_FINALIZING
        if decision.action is ArbitrationAction.ABORT:
            raise RuntimeError(f"question arbitration aborted: {decision.rationale}")

        retry_field: str
        next_phase: str
        if decision.action in {ArbitrationAction.RETRY_PROBLEM, ArbitrationAction.RETRY_ALL}:
            retry_field = "problem_retries"
            next_phase = PROBLEM_GENERATING
        elif decision.action is ArbitrationAction.RETRY_SOLUTION:
            retry_field = "solution_retries"
            next_phase = SOLUTION_GENERATING
        else:
            retry_field = "rubric_retries"
            next_phase = RUBRIC_GENERATING
        values[retry_field] = int(values[retry_field]) + 1
        exhausted = (
            int(values[retry_field]) >= self.max_question_attempts
            or state.round >= self.max_total_question_rounds
        )
        if exhausted:
            values["requires_human_review"] = True
            return QuestionWorkflowState.model_validate(values), QUESTION_FINALIZING
        values["round"] = state.round + 1
        return QuestionWorkflowState.model_validate(values), next_phase

    def _bundle_from_state(
        self,
        state: dict[str, Any],
        request: QuestionGenerationRequest,
        capability_validators: list[str],
    ) -> ExamQuestionBundle:
        bundle = ExamQuestionBundle(
            question=state["question"],
            solution=state["solution"],
            rubric=state["rubric"],
        )
        self.capabilities.validate_bundle(request.profile, bundle, capability_validators)
        return bundle

    def _write_state(
        self,
        run_id: UUID,
        state: QuestionWorkflowState,
        phase: str,
    ) -> ArtifactRef:
        return self.artifacts.write_json(
            run_id,
            "question-state.json",
            state.model_dump(mode="json"),
            created_by_phase=phase,
        )


def _domain_validation_feedback(stage: str, error: ValidationError) -> str:
    details = json.dumps(error.errors(include_url=False), ensure_ascii=False, default=str)
    return (
        f"The {stage} draft failed deterministic domain validation. Correct the draft and "
        f"return a complete replacement. Validation errors: {details}"
    )
