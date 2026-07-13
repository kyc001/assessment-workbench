from collections.abc import Iterable
from uuid import UUID

from assessment_workbench.domain import (
    DifficultyDistribution,
    ExamArbitrationDecision,
    ExamBlueprint,
    ExamBundleVersionSignature,
    ExamDocument,
    ExamQuestionBundle,
    ExamReviewFinding,
    ExamReviewReport,
    ExamReviewTarget,
    FindingSeverity,
    QuestionPlan,
    QuestionPlanDraft,
    SubjectProfile,
)


def exam_bundle_signature(exam: ExamDocument) -> ExamBundleVersionSignature:
    bundles = sorted(exam.questions, key=lambda item: item.question.number)
    return ExamBundleVersionSignature(
        question_version_ids=[bundle.question.id for bundle in bundles],
        solution_version_ids=[bundle.solution.id for bundle in bundles],
        rubric_version_ids=[bundle.rubric.id for bundle in bundles],
    )


def validate_bundle_for_plan(bundle: ExamQuestionBundle, plan: QuestionPlan) -> None:
    question = bundle.question
    mismatches: list[str] = []
    if question.metadata.plan_id != plan.id:
        mismatches.append("plan id")
    if question.number != plan.number:
        mismatches.append("question number")
    if question.section_id != plan.section_id:
        mismatches.append("section")
    if question.question_type != plan.question_type:
        mismatches.append("question type")
    if question.score != plan.score:
        mismatches.append("score")
    if mismatches:
        raise ValueError(f"question bundle does not match plan {plan.id}: {', '.join(mismatches)}")


def validate_question_plan_coverage(
    blueprint: ExamBlueprint,
    plans: list[QuestionPlan],
) -> None:
    if not blueprint.coverage:
        return
    targets = {target.topic_tag: target.target_score for target in blueprint.coverage}
    actual = dict.fromkeys(targets, 0)
    for plan in plans:
        if plan.coverage_tag not in targets:
            raise ValueError(
                f"question plan {plan.id} has invalid coverage_tag: {plan.coverage_tag!r}"
            )
        actual[plan.coverage_tag] += plan.score
    mismatched = {
        tag: {"actual": actual[tag], "expected": expected}
        for tag, expected in targets.items()
        if actual[tag] != expected
    }
    if mismatched:
        raise ValueError(f"question plan coverage scores do not match blueprint: {mismatched}")


def deterministic_exam_review(
    profile: SubjectProfile,
    blueprint: ExamBlueprint,
    plans: list[QuestionPlan],
    exam: ExamDocument,
) -> ExamReviewReport:
    findings: list[ExamReviewFinding] = []
    bundles_by_number = {bundle.question.number: bundle for bundle in exam.questions}
    plans_by_number = {plan.number: plan for plan in plans}
    if blueprint.subject_profile != profile.id or exam.subject_profile != profile.id:
        findings.append(
            _finding(
                "subject_profile_mismatch",
                FindingSeverity.FATAL,
                ExamReviewTarget.EXAM,
                "Exam and blueprint must reference the active subject profile.",
            )
        )
    expected_numbers = set(range(1, sum(section.count for section in blueprint.sections) + 1))
    actual_numbers = set(bundles_by_number)
    plan_numbers = set(plans_by_number)
    if actual_numbers != expected_numbers or plan_numbers != expected_numbers:
        findings.append(
            _finding(
                "exam_slot_mismatch",
                FindingSeverity.FATAL,
                ExamReviewTarget.EXAM,
                "Exam bundles and question plans must cover every blueprint slot exactly once.",
            )
        )

    section_by_id = {section.id: section for section in blueprint.sections}
    for number in sorted(actual_numbers & plan_numbers):
        plan = plans_by_number[number]
        bundle = bundles_by_number[number]
        question = bundle.question
        errors: list[str] = []
        if question.metadata.plan_id != plan.id:
            errors.append("plan binding")
        if question.section_id != plan.section_id:
            errors.append("section")
        if question.question_type != plan.question_type:
            errors.append("question type")
        if question.score != plan.score:
            errors.append("score")
        section = section_by_id.get(question.section_id)
        if section is None:
            errors.append("blueprint section")
        if errors:
            findings.append(
                _finding(
                    "question_plan_mismatch",
                    FindingSeverity.FATAL,
                    ExamReviewTarget.QUESTION,
                    f"Question {number} disagrees with its plan or blueprint: {', '.join(errors)}.",
                    question_ids=[question.id],
                    section_ids=[question.section_id] if question.section_id else [],
                )
            )

    findings.extend(_coverage_findings(blueprint, plans, bundles_by_number))
    findings.extend(
        _difficulty_findings(blueprint.difficulty_distribution, plans, bundles_by_number)
    )

    estimated_minutes = sum(plan.estimated_minutes for plan in plans)
    if estimated_minutes > blueprint.duration_minutes:
        findings.append(
            _finding(
                "exam_duration_overrun",
                FindingSeverity.ERROR,
                ExamReviewTarget.PLAN,
                f"Planned time is {estimated_minutes} minutes, above the "
                f"{blueprint.duration_minutes}-minute limit.",
                question_ids=[bundle.question.id for bundle in exam.questions],
            )
        )
    elif estimated_minutes < blueprint.duration_minutes * 0.6:
        findings.append(
            _finding(
                "exam_duration_underfill",
                FindingSeverity.WARNING,
                ExamReviewTarget.PLAN,
                f"Planned time is only {estimated_minutes} minutes for a "
                f"{blueprint.duration_minutes}-minute exam.",
                question_ids=[bundle.question.id for bundle in exam.questions],
            )
        )

    blocking = {FindingSeverity.ERROR, FindingSeverity.FATAL}
    return ExamReviewReport(
        reviewer="deterministic",
        passed=not any(finding.severity in blocking for finding in findings),
        findings=findings,
        summary=f"Checked {len(exam.questions)} questions against the blueprint and plans.",
    )


def resolve_exam_targets(
    decision: ExamArbitrationDecision,
    exam: ExamDocument,
    blueprint: ExamBlueprint,
) -> list[int]:
    question_number_by_id = {
        bundle.question.id: bundle.question.number for bundle in exam.questions
    }
    unknown_question_ids = sorted(set(decision.question_ids) - set(question_number_by_id), key=str)
    if unknown_question_ids:
        raise ValueError(
            f"exam arbitration references unknown question ids: {unknown_question_ids}"
        )

    section_ids = {section.id for section in blueprint.sections}
    unknown_section_ids = sorted(set(decision.section_ids) - section_ids)
    if unknown_section_ids:
        raise ValueError(f"exam arbitration references unknown section ids: {unknown_section_ids}")

    numbers = {question_number_by_id[question_id] for question_id in decision.question_ids}
    numbers.update(
        bundle.question.number
        for bundle in exam.questions
        if bundle.question.section_id in decision.section_ids
    )
    if not numbers:
        raise ValueError(f"exam arbitration action {decision.action.value} resolved no questions")
    return sorted(numbers)


def validate_exam_review_report(
    report: ExamReviewReport,
    exam: ExamDocument,
    blueprint: ExamBlueprint,
) -> None:
    question_ids = {bundle.question.id for bundle in exam.questions}
    section_ids = {section.id for section in blueprint.sections}
    has_blocking = False
    for finding in report.findings:
        unknown_questions = sorted(set(finding.question_ids) - question_ids, key=str)
        if unknown_questions:
            raise ValueError(f"exam review references unknown question ids: {unknown_questions}")
        unknown_sections = sorted(set(finding.section_ids) - section_ids)
        if unknown_sections:
            raise ValueError(f"exam review references unknown section ids: {unknown_sections}")
        if finding.severity in {FindingSeverity.ERROR, FindingSeverity.FATAL} and not (
            finding.question_ids or finding.section_ids
        ):
            raise ValueError(
                f"blocking exam review finding {finding.code!r} requires a local target"
            )
        has_blocking = has_blocking or finding.severity in {
            FindingSeverity.ERROR,
            FindingSeverity.FATAL,
        }
    if report.passed == has_blocking:
        raise ValueError("exam review passed flag must equal the absence of blocking findings")


def merge_revised_question_plans(
    current: list[QuestionPlan],
    target_plan_ids: Iterable[str],
    drafts: list[QuestionPlanDraft],
) -> list[QuestionPlan]:
    target_ids = set(target_plan_ids)
    current_by_key = {(plan.section_id, plan.slot): plan for plan in current}
    target_by_key = {
        (plan.section_id, plan.slot): plan for plan in current if plan.id in target_ids
    }
    if {plan.id for plan in target_by_key.values()} != target_ids:
        missing = sorted(target_ids - {plan.id for plan in target_by_key.values()})
        raise ValueError(f"unknown target question plan ids: {missing}")

    drafts_by_key: dict[tuple[str, int], QuestionPlanDraft] = {}
    for draft in drafts:
        key = (draft.section_id, draft.slot)
        if key in drafts_by_key:
            raise ValueError(f"question plan reviser returned duplicate slot: {key}")
        drafts_by_key[key] = draft
    if set(drafts_by_key) != set(target_by_key):
        missing_keys = sorted(set(target_by_key) - set(drafts_by_key))
        unexpected_keys = sorted(set(drafts_by_key) - set(target_by_key))
        raise ValueError(
            "question plan revision target mismatch: "
            f"missing={missing_keys}, unexpected={unexpected_keys}"
        )

    merged: list[QuestionPlan] = []
    for key, plan in current_by_key.items():
        revision = drafts_by_key.get(key)
        if revision is None:
            merged.append(plan)
            continue
        merged.append(
            QuestionPlan(
                id=plan.id,
                number=plan.number,
                question_type=plan.question_type,
                score=plan.score,
                section_id=plan.section_id,
                section_title=plan.section_title,
                slot=plan.slot,
                topic_tags=revision.topic_tags,
                coverage_tag=revision.coverage_tag,
                primary_skill=revision.primary_skill,
                design_brief=revision.design_brief,
                difficulty=revision.difficulty,
                estimated_minutes=revision.estimated_minutes,
                answer_form=revision.answer_form,
                solution_outline=revision.solution_outline,
                rubric_focus=revision.rubric_focus,
                verification_methods=revision.verification_methods,
                originality_constraints=revision.originality_constraints,
            )
        )
    return sorted(merged, key=lambda plan: plan.number)


def bundles_for_numbers(exam: ExamDocument, numbers: Iterable[int]) -> list[ExamQuestionBundle]:
    targets = set(numbers)
    return [bundle for bundle in exam.questions if bundle.question.number in targets]


def _coverage_findings(
    blueprint: ExamBlueprint,
    plans: list[QuestionPlan],
    bundles_by_number: dict[int, ExamQuestionBundle],
) -> list[ExamReviewFinding]:
    findings: list[ExamReviewFinding] = []
    for target in blueprint.coverage:
        matching = [plan for plan in plans if plan.coverage_tag == target.topic_tag]
        if not matching:
            matching = [
                plan
                for plan in plans
                if plan.coverage_tag is None and target.topic_tag in plan.topic_tags
            ]
        actual_score = sum(plan.score for plan in matching)
        if actual_score == target.target_score:
            continue
        findings.append(
            _finding(
                "coverage_score_mismatch",
                FindingSeverity.ERROR,
                ExamReviewTarget.PLAN,
                f"Coverage for {target.topic_tag!r} is {actual_score} points, expected "
                f"{target.target_score}.",
                question_ids=[
                    bundles_by_number[plan.number].question.id
                    for plan in matching
                    if plan.number in bundles_by_number
                ],
                section_ids=sorted({plan.section_id for plan in matching}),
            )
        )
    return findings


def _difficulty_findings(
    target: DifficultyDistribution,
    plans: list[QuestionPlan],
    bundles_by_number: dict[int, ExamQuestionBundle],
) -> list[ExamReviewFinding]:
    total_score = sum(plan.score for plan in plans)
    if total_score == 0:
        return []
    actual_scores = {"easy": 0, "medium": 0, "hard": 0}
    unknown: list[QuestionPlan] = []
    for plan in plans:
        difficulty = plan.difficulty.strip().casefold()
        if difficulty not in actual_scores:
            unknown.append(plan)
            continue
        actual_scores[difficulty] += plan.score
    findings: list[ExamReviewFinding] = []
    if unknown:
        findings.append(
            _finding(
                "unknown_difficulty_label",
                FindingSeverity.ERROR,
                ExamReviewTarget.PLAN,
                "Question plans must use easy, medium, or hard difficulty labels.",
                question_ids=[
                    bundles_by_number[plan.number].question.id
                    for plan in unknown
                    if plan.number in bundles_by_number
                ],
                section_ids=sorted({plan.section_id for plan in unknown}),
            )
        )
    tolerance = max(plan.score for plan in plans) / total_score
    target_values = target.model_dump()
    mismatched = [
        name
        for name, target_value in target_values.items()
        if abs(actual_scores[name] / total_score - target_value) > tolerance + 1e-6
    ]
    if mismatched:
        findings.append(
            _finding(
                "difficulty_distribution_mismatch",
                FindingSeverity.ERROR,
                ExamReviewTarget.PLAN,
                "Score-weighted difficulty distribution is outside one-question tolerance for: "
                + ", ".join(mismatched)
                + ".",
                question_ids=[bundle.question.id for bundle in bundles_by_number.values()],
                section_ids=sorted({plan.section_id for plan in plans}),
            )
        )
    return findings


def _finding(
    code: str,
    severity: FindingSeverity,
    target: ExamReviewTarget,
    message: str,
    *,
    question_ids: list[UUID] | None = None,
    section_ids: list[str] | None = None,
) -> ExamReviewFinding:
    return ExamReviewFinding(
        code=code,
        severity=severity,
        target=target,
        message=message,
        question_ids=question_ids or [],
        section_ids=section_ids or [],
    )
