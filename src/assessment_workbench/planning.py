from typing import Any

from assessment_workbench.domain import (
    DifficultyProfile,
    QuestionSpec,
    QuestionType,
    WorkflowRun,
)
from assessment_workbench.storage import ArtifactStore, LocalKnowledgeBackend, RunStore
from assessment_workbench.workflow import WorkflowEngine


class QuestionSpecWorkflow:
    def __init__(
        self,
        knowledge: LocalKnowledgeBackend,
        artifacts: ArtifactStore,
        runs: RunStore,
    ) -> None:
        self.knowledge = knowledge
        self.artifacts = artifacts
        self.engine = WorkflowEngine(runs)

    async def execute(
        self,
        *,
        course_id: str,
        topic_slugs: list[str],
        question_type: QuestionType,
        score: int,
        difficulty: int,
    ) -> tuple[WorkflowRun, dict[str, Any]]:
        async def resolve(_: dict[str, Any]) -> dict[str, Any]:
            missing = [
                slug for slug in topic_slugs if self.knowledge.get_point(course_id, slug) is None
            ]
            if missing:
                raise ValueError(f"unknown topic slugs: {', '.join(missing)}")
            points = self.knowledge.expand(course_id, topic_slugs, depth=1)
            return {"points": points}

        async def plan(state: dict[str, Any]) -> dict[str, Any]:
            points = state["points"]
            references = []
            for point in points:
                references.extend(point.evidence)
            unique_references = {f"{ref.document_id}:{ref.block_id}": ref for ref in references}
            profile = DifficultyProfile(
                conceptual=difficulty,
                reasoning=difficulty,
                calculation=difficulty,
                overall=difficulty,
            )
            spec = QuestionSpec(
                course_id=course_id,
                question_type=question_type,
                topic_slugs=topic_slugs,
                score=score,
                difficulty=profile,
                learning_objectives=[f"Assess understanding of {point.name}" for point in points],
                required_context=list(unique_references.values()),
                constraints=[
                    "Use only claims supported by the required context",
                    "Provide an independently verifiable solution",
                    "Provide rubric items whose scores sum to the question score",
                ],
            )
            return {"spec": spec}

        run, state = await self.engine.execute(
            "question_spec_planning",
            [("TOPIC_RESOLVING", resolve), ("SPEC_PLANNING", plan)],
        )
        if run.status == "succeeded":
            self.artifacts.write_json(
                run.id, "question-spec.json", state["spec"].model_dump(mode="json")
            )
        return run, state
