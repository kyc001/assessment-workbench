from pathlib import Path

from assessment_workbench.demo_exam import GaokaoMathDemoWorkflow
from assessment_workbench.domain import RunStatus
from assessment_workbench.storage import ArtifactStore, RunStore, Workspace

PROJECT_ROOT = Path(__file__).parents[1]


async def test_gaokao_math_demo_builds_complete_exam(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "workspace")
    workspace.initialize()
    artifacts = ArtifactStore(workspace)
    run, state = await GaokaoMathDemoWorkflow(artifacts, RunStore(workspace)).execute(
        PROJECT_ROOT / "examples" / "gaokao-mathematics" / "blueprint.yaml",
        PROJECT_ROOT / "examples" / "gaokao-mathematics" / "questions.yaml",
    )

    assert run.status is RunStatus.SUCCEEDED
    exam = state["exam"]
    assert len(exam.questions) == 20
    assert exam.total_score == 150
    assert sum(bundle.rubric.max_score for bundle in exam.questions) == 150
    stored = artifacts.list(run.id)
    assert [item.logical_name for item in stored] == [
        "exam.json",
        "exam.md",
        "rubrics.md",
        "solutions.md",
    ]
    assert all(artifacts.verify(item.id) for item in stored)
