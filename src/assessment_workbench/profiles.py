from pathlib import Path
from typing import Any

import yaml

from assessment_workbench.domain import ExamBlueprint, SubjectProfile


def load_subject_profile(path: Path) -> SubjectProfile:
    return SubjectProfile.model_validate(_load_yaml(path))


def load_exam_blueprint(path: Path) -> ExamBlueprint:
    return ExamBlueprint.model_validate(_load_yaml(path))


def _load_yaml(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected a mapping in YAML file: {path}")
    return payload
