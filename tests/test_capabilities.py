from pathlib import Path

import pytest

from assessment_workbench.capabilities import load_default_capability_catalog
from assessment_workbench.domain import QuestionType
from assessment_workbench.prompting import PromptBundle, PromptRegistry

PROJECT_ROOT = Path(__file__).parents[1]


def test_default_prompt_registry_owns_role_and_version() -> None:
    catalog = load_default_capability_catalog()
    prompt = catalog.prompts.require("question_writer")

    assert prompt.role == "question_writer"
    assert prompt.version == "question-writer-v2"
    assert "original" in prompt.system_prompt

    with pytest.raises(ValueError, match="not registered"):
        catalog.prompts.require("missing")


def test_prompt_registry_rejects_duplicate_keys() -> None:
    bundle = PromptBundle(key="writer", role="writer", version="v1", system_prompt="Write.")

    with pytest.raises(ValueError, match="already registered"):
        PromptRegistry([bundle, bundle])


def test_gaokao_mathematics_capability_locks_structure_without_broad_alias() -> None:
    catalog = load_default_capability_catalog()
    capability = catalog.subjects.resolve("高考数学")

    assert capability is not None
    assert catalog.subjects.resolve("高中数学") is None
    assert capability.blueprint is not None
    assert capability.blueprint.total_score == 150
    assert [section.count for section in capability.blueprint.sections] == [8, 3, 3, 5]
    assert [section.question_type for section in capability.blueprint.sections] == [
        QuestionType.MULTIPLE_CHOICE,
        QuestionType.MULTIPLE_SELECT,
        QuestionType.FILL_BLANK,
        QuestionType.CONSTRUCTED_RESPONSE,
    ]


def test_catalog_rejects_unknown_profile_references() -> None:
    catalog = load_default_capability_catalog()
    capability = catalog.subjects.require("gaokao-mathematics")

    unknown_reviewer = capability.profile.model_copy(update={"reviewers": ["missing"]})
    with pytest.raises(ValueError, match="reviewer is not registered"):
        catalog.validate_profile(unknown_reviewer)

    unknown_tool = capability.profile.model_copy(update={"tools": ["missing"]})
    with pytest.raises(ValueError, match="tool is not registered"):
        catalog.validate_profile(unknown_tool)

    assert capability.blueprint is not None
    changed_blueprint = capability.blueprint.model_copy(update={"id": "changed"})
    with pytest.raises(ValueError, match="does not match"):
        catalog.require_subject_binding(
            capability.id,
            capability.version,
            capability.profile,
            changed_blueprint,
            capability.prompt_context,
        )


def test_bundled_capability_has_no_static_assessment_content() -> None:
    content = (
        PROJECT_ROOT / "src" / "assessment_workbench" / "resources" / "subject-capabilities.yaml"
    ).read_text(encoding="utf-8")

    for forbidden_field in ("statement:", "final_answer:", "solution_steps:", "rubric_items:"):
        assert forbidden_field not in content
