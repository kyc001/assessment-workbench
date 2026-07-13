from typing import Any
from uuid import UUID

from assessment_workbench.capabilities import CapabilityCatalog
from assessment_workbench.domain import (
    ArtifactRef,
    ExamArbitrationAction,
    ExamArbitrationDecision,
    ExamBlueprint,
    ExamDocument,
    ExamReviewReport,
    ExamWorkflowState,
    FindingSeverity,
    QuestionPlan,
    QuestionPlanSetDraft,
    SubjectProfile,
)
from assessment_workbench.exam_quality import (
    deterministic_exam_review,
    merge_revised_question_plans,
    resolve_exam_targets,
    validate_question_plan_coverage,
)
from assessment_workbench.exam_review_workflow import (
    ExamReviewerExhaustedError,
    ExamReviewerPoolWorkflow,
)
from assessment_workbench.ports import StructuredModel
from assessment_workbench.prompting import complete_with_prompt, json_prompt
from assessment_workbench.storage import ArtifactStore, RunStore

QUESTION_PLANS_REVISING = "QUESTION_PLANS_REVISING"
QUESTIONS_GENERATING = "QUESTIONS_GENERATING"
EXAM_REVIEWS_GENERATING = "EXAM_REVIEWS_GENERATING"
EXAM_ARBITRATING = "EXAM_ARBITRATING"
EXAM_FINALIZING = "EXAM_FINALIZING"


class ExamQualityWorkflow:
    def __init__(
        self,
        standard_model: StructuredModel,
        strong_model: StructuredModel,
        artifacts: ArtifactStore,
        runs: RunStore,
        capabilities: CapabilityCatalog,
        *,
        max_reviewer_attempts: int,
        max_review_rounds: int,
        max_draft_validation_attempts: int,
    ) -> None:
        if max_review_rounds < 1:
            raise ValueError("max_review_rounds must be at least 1")
        self.strong_model = strong_model
        self.artifacts = artifacts
        self.capabilities = capabilities
        self.prompts = capabilities.prompts
        self.max_review_rounds = max_review_rounds
        self.max_draft_validation_attempts = max_draft_validation_attempts
        self.reviewers = ExamReviewerPoolWorkflow(
            standard_model,
            artifacts,
            runs,
            capabilities,
            max_attempts=max_reviewer_attempts,
        )

    async def revise_plans(
        self,
        run_id: UUID,
        *,
        profile: SubjectProfile,
        blueprint: ExamBlueprint,
        current: list[QuestionPlan],
        exam_state: object,
        reports: object,
        capability_context: dict[str, list[str]],
    ) -> dict[str, Any]:
        if not isinstance(exam_state, ExamWorkflowState) or not exam_state.revision_plan_ids:
            return {}
        target_ids = set(exam_state.revision_plan_ids)
        target_plans = [plan for plan in current if plan.id in target_ids]
        unchanged_plans = [plan for plan in current if plan.id not in target_ids]
        prompt = self.prompts.require("question_plan_reviser")
        validation_feedback = list(exam_state.plan_feedback)
        review_reports = (
            [report for report in reports if isinstance(report, ExamReviewReport)]
            if isinstance(reports, list)
            else []
        )
        for validation_attempt in range(self.max_draft_validation_attempts):
            draft = await complete_with_prompt(
                self.strong_model,
                prompt=prompt,
                user_prompt=json_prompt(
                    subject_profile=profile.model_dump(mode="json"),
                    blueprint=blueprint.model_dump(mode="json"),
                    target_plans=[plan.model_dump(mode="json") for plan in target_plans],
                    unchanged_plans=[plan.model_dump(mode="json") for plan in unchanged_plans],
                    review_reports=[report.model_dump(mode="json") for report in review_reports],
                    revision_feedback=validation_feedback,
                    capability_context=capability_context,
                ),
                response_model=QuestionPlanSetDraft,
                run_id=run_id,
            )
            try:
                revised = merge_revised_question_plans(current, target_ids, draft.plans)
                validate_question_plan_coverage(blueprint, revised)
                break
            except ValueError as exc:
                if validation_attempt == self.max_draft_validation_attempts - 1:
                    raise
                validation_feedback = [
                    "The previous local revision changed the target set or slot identity. "
                    f"Return exactly the supplied target slots. Error: {exc}"
                ]
        else:
            raise RuntimeError("question plan revision loop exited unexpectedly")
        artifact = self.artifacts.write_json(
            run_id,
            "question-plans.json",
            [plan.model_dump(mode="json") for plan in revised],
            created_by_phase=QUESTION_PLANS_REVISING,
        )
        return {
            "question_plans": revised,
            "output_artifact_ids": [artifact.id],
            "_checkpoint_artifacts": {"question_plans": artifact.id},
        }

    async def review(
        self,
        run_id: UUID,
        *,
        profile: SubjectProfile,
        blueprint: ExamBlueprint,
        plans: list[QuestionPlan],
        exam: ExamDocument,
        capability_context: dict[str, list[str]],
        current_state: object,
        restored_records: object,
    ) -> dict[str, Any]:
        exam_state = (
            current_state if isinstance(current_state, ExamWorkflowState) else ExamWorkflowState()
        )
        deterministic = deterministic_exam_review(profile, blueprint, plans, exam)
        deterministic_artifact = self.artifacts.write_json(
            run_id,
            "exam-deterministic-review.json",
            deterministic.model_dump(mode="json"),
            created_by_phase=EXAM_REVIEWS_GENERATING,
        )
        try:
            outcome = await self.reviewers.execute(
                run_id,
                profile=profile,
                blueprint=blueprint,
                plans=plans,
                exam=exam,
                capability_context=capability_context,
                restored_records=restored_records,
            )
        except ExamReviewerExhaustedError as exc:
            exhausted_state = exam_state.model_copy(
                update={"requires_human_review": True, "question_feedback": [str(exc)]}
            )
            state_artifact = self.write_state(run_id, exhausted_state, EXAM_REVIEWS_GENERATING)
            latest_manifest = self.artifacts.latest(run_id, "exam-review-runs.json")
            bindings = {
                "exam_deterministic_review": deterministic_artifact.id,
                "exam_workflow_state": state_artifact.id,
            }
            artifact_ids = [deterministic_artifact.id, state_artifact.id]
            if latest_manifest is not None:
                bindings["exam_review_manifest"] = latest_manifest.id
                artifact_ids.append(latest_manifest.id)
            return {
                "exam_workflow_state": exhausted_state,
                "exam_reports": [deterministic],
                "output_artifact_ids": artifact_ids,
                "_checkpoint_artifacts": bindings,
                "_next_phase": EXAM_FINALIZING,
            }
        reports = [deterministic, *outcome.reports]
        reports_artifact = self.artifacts.write_json(
            run_id,
            "exam-reviews.json",
            [report.model_dump(mode="json") for report in reports],
            created_by_phase=EXAM_REVIEWS_GENERATING,
        )
        state_artifact = self.write_state(run_id, exam_state, EXAM_REVIEWS_GENERATING)
        return {
            "exam_workflow_state": exam_state,
            "exam_reports": reports,
            "exam_review_records": outcome.records,
            "output_artifact_ids": [
                deterministic_artifact.id,
                outcome.manifest_artifact.id,
                reports_artifact.id,
                state_artifact.id,
            ],
            "_checkpoint_artifacts": {
                "exam_deterministic_review": deterministic_artifact.id,
                "exam_review_manifest": outcome.manifest_artifact.id,
                "exam_reviews": reports_artifact.id,
                "exam_workflow_state": state_artifact.id,
            },
            "_checkpoint_child_run_ids": outcome.child_run_ids,
        }

    async def arbitrate(
        self,
        run_id: UUID,
        *,
        profile: SubjectProfile,
        blueprint: ExamBlueprint,
        plans: list[QuestionPlan],
        exam: ExamDocument,
        reports: object,
        current_state: object,
        capability_context: dict[str, list[str]],
    ) -> dict[str, Any]:
        if not isinstance(reports, list) or not all(
            isinstance(report, ExamReviewReport) for report in reports
        ):
            raise ValueError("exam state has no complete review reports")
        prompt = self.prompts.require("exam_arbiter")
        decision = await complete_with_prompt(
            self.strong_model,
            prompt=prompt,
            user_prompt=json_prompt(
                subject_profile=profile.model_dump(mode="json"),
                blueprint=blueprint.model_dump(mode="json"),
                question_plans=[plan.model_dump(mode="json") for plan in plans],
                exam=exam.model_dump(mode="json"),
                reports=[report.model_dump(mode="json") for report in reports],
                capability_context=capability_context,
            ),
            response_model=ExamArbitrationDecision,
            run_id=run_id,
        )
        decision = self.enforce_review_gate(decision, reports)
        decision_artifact = self.artifacts.write_json(
            run_id,
            "exam-arbitration.json",
            decision.model_dump(mode="json"),
            created_by_phase=EXAM_ARBITRATING,
        )
        exam_state = (
            current_state if isinstance(current_state, ExamWorkflowState) else ExamWorkflowState()
        )
        next_state, next_phase = self.route_decision(exam_state, decision, exam, blueprint, plans)
        state_artifact = self.write_state(run_id, next_state, EXAM_ARBITRATING)
        return {
            "exam_decision": decision,
            "exam_workflow_state": next_state,
            "output_artifact_ids": [decision_artifact.id, state_artifact.id],
            "_checkpoint_artifacts": {
                "exam_decision": decision_artifact.id,
                "exam_workflow_state": state_artifact.id,
            },
            "_next_phase": next_phase,
        }

    def finalize(self, run_id: UUID, current_state: object) -> dict[str, Any]:
        exam_state = (
            current_state
            if isinstance(current_state, ExamWorkflowState)
            else ExamWorkflowState(requires_human_review=True)
        )
        artifact = self.write_state(run_id, exam_state, EXAM_FINALIZING)
        return {
            "exam_workflow_state": exam_state,
            "output_artifact_ids": [artifact.id],
            "_checkpoint_artifacts": {"exam_workflow_state": artifact.id},
        }

    def write_state(
        self,
        run_id: UUID,
        state: ExamWorkflowState,
        phase: str,
    ) -> ArtifactRef:
        return self.artifacts.write_json(
            run_id,
            "exam-workflow-state.json",
            state.model_dump(mode="json"),
            created_by_phase=phase,
        )

    def enforce_review_gate(
        self,
        decision: ExamArbitrationDecision,
        reports: list[ExamReviewReport],
    ) -> ExamArbitrationDecision:
        if decision.action not in {
            ExamArbitrationAction.PASS,
            ExamArbitrationAction.PASS_WITH_WARNINGS,
        }:
            return decision
        blocking = [
            finding
            for report in reports
            for finding in report.findings
            if finding.severity in {FindingSeverity.ERROR, FindingSeverity.FATAL}
        ]
        if not blocking:
            return decision
        question_ids = list(
            dict.fromkeys(
                question_id for finding in blocking for question_id in finding.question_ids
            )
        )
        section_ids = list(
            dict.fromkeys(section_id for finding in blocking for section_id in finding.section_ids)
        )
        codes = [finding.code for finding in blocking]
        messages = [finding.message for finding in blocking]
        if any(code.startswith("coverage_") for code in codes) and (question_ids or section_ids):
            action = ExamArbitrationAction.REBALANCE_COVERAGE
        elif any(code.startswith("difficulty_") for code in codes) and (
            question_ids or section_ids
        ):
            action = ExamArbitrationAction.REBALANCE_DIFFICULTY
        elif question_ids:
            action = ExamArbitrationAction.REPLACE_QUESTIONS
        elif section_ids:
            action = ExamArbitrationAction.REGENERATE_SECTION
        else:
            return ExamArbitrationDecision(
                action=ExamArbitrationAction.ESCALATE_HUMAN,
                rationale=(
                    "Deterministic exam review gate rejected PASS, but the blocking findings "
                    "did not identify a safe local repair target."
                ),
                finding_codes=codes,
                question_feedback=messages,
            )
        return ExamArbitrationDecision(
            action=action,
            rationale=(
                "Deterministic exam review gate rejected PASS while blocking findings remain."
            ),
            finding_codes=codes,
            question_ids=question_ids,
            section_ids=section_ids,
            plan_feedback=messages,
            question_feedback=messages,
        )

    def route_decision(
        self,
        state: ExamWorkflowState,
        decision: ExamArbitrationDecision,
        exam: ExamDocument,
        blueprint: ExamBlueprint,
        plans: list[QuestionPlan],
    ) -> tuple[ExamWorkflowState, str]:
        values = state.model_dump()
        values.update(
            last_action=decision.action,
            plan_feedback=decision.plan_feedback,
            question_feedback=decision.question_feedback,
        )
        if decision.action in {
            ExamArbitrationAction.PASS,
            ExamArbitrationAction.PASS_WITH_WARNINGS,
        }:
            values["replacement_question_numbers"] = []
            values["revision_plan_ids"] = []
            values["requires_human_review"] = (
                decision.action is ExamArbitrationAction.PASS_WITH_WARNINGS
            )
            return ExamWorkflowState.model_validate(values), EXAM_FINALIZING
        if decision.action is ExamArbitrationAction.ESCALATE_HUMAN:
            values["requires_human_review"] = True
            return ExamWorkflowState.model_validate(values), EXAM_FINALIZING
        if decision.action is ExamArbitrationAction.ABORT:
            raise RuntimeError(f"exam arbitration aborted: {decision.rationale}")

        numbers = resolve_exam_targets(decision, exam, blueprint)
        plan_by_number = {plan.number: plan for plan in plans}
        missing_plans = sorted(set(numbers) - set(plan_by_number))
        if missing_plans:
            raise ValueError(f"exam arbitration targets questions without plans: {missing_plans}")
        values["replacement_question_numbers"] = numbers
        values["requires_human_review"] = False
        is_rebalance = decision.action in {
            ExamArbitrationAction.REBALANCE_DIFFICULTY,
            ExamArbitrationAction.REBALANCE_COVERAGE,
        }
        retry_field = "rebalance_rounds" if is_rebalance else "replacement_rounds"
        values[retry_field] = int(values[retry_field]) + 1
        exhausted = (
            int(values[retry_field]) >= self.max_review_rounds
            or state.round >= self.max_review_rounds
        )
        if exhausted:
            values["requires_human_review"] = True
            return ExamWorkflowState.model_validate(values), EXAM_FINALIZING
        values["round"] = state.round + 1
        if is_rebalance:
            values["revision_plan_ids"] = [plan_by_number[number].id for number in numbers]
            next_phase = QUESTION_PLANS_REVISING
        else:
            values["revision_plan_ids"] = []
            next_phase = QUESTIONS_GENERATING
        return ExamWorkflowState.model_validate(values), next_phase
