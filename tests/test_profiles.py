from pathlib import Path

import pytest
from pydantic import ValidationError

from assessment_workbench.domain import ExamBlueprint
from assessment_workbench.profiles import load_exam_blueprint, load_subject_profile

PROJECT_ROOT = Path(__file__).parents[1]


def test_loads_gaokao_mathematics_profile_and_blueprint() -> None:
    profile = load_subject_profile(
        PROJECT_ROOT / "examples" / "subject-profiles" / "gaokao-mathematics.yaml"
    )
    blueprint = load_exam_blueprint(
        PROJECT_ROOT / "examples" / "gaokao-mathematics" / "blueprint.yaml"
    )

    assert profile.display_name == "高考数学"
    assert blueprint.subject_profile == profile.id
    assert blueprint.total_score == 150
    assert sum(section.total_score for section in blueprint.sections) == 150
    assert sum(section.count for section in blueprint.sections) == 20


def test_blueprint_rejects_score_mismatch() -> None:
    with pytest.raises(ValidationError, match="section scores total"):
        ExamBlueprint.model_validate(
            {
                "id": "invalid",
                "subject_profile": "math",
                "title": "invalid",
                "target_level": "高中",
                "duration_minutes": 120,
                "total_score": 100,
                "sections": [
                    {
                        "id": "a",
                        "title": "选择题",
                        "question_type": "multiple_choice",
                        "count": 10,
                        "score_each": 5,
                    }
                ],
                "difficulty_distribution": {"easy": 0.3, "medium": 0.5, "hard": 0.2},
            }
        )
