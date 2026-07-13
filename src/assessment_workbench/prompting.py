from __future__ import annotations

import json
from collections.abc import Iterable
from importlib.resources import files
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml
from pydantic import BaseModel, ConfigDict, Field

from assessment_workbench.ports import StructuredModel


class PromptBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(min_length=1)
    role: str = Field(min_length=1)
    version: str = Field(min_length=1)
    system_prompt: str = Field(min_length=1)


class PromptRegistry:
    def __init__(self, bundles: Iterable[PromptBundle] = ()) -> None:
        self._bundles: dict[str, PromptBundle] = {}
        for bundle in bundles:
            self.register(bundle)

    def register(self, bundle: PromptBundle) -> None:
        if bundle.key in self._bundles:
            raise ValueError(f"prompt is already registered: {bundle.key}")
        self._bundles[bundle.key] = bundle

    def require(self, key: str) -> PromptBundle:
        try:
            return self._bundles[key]
        except KeyError as exc:
            raise ValueError(f"prompt is not registered: {key}") from exc

    def keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._bundles))

    @classmethod
    def from_path(cls, path: Path) -> PromptRegistry:
        return cls(_parse_prompt_payload(_load_yaml(path.read_text(encoding="utf-8"), path)))


def load_default_prompt_registry() -> PromptRegistry:
    resource = files("assessment_workbench").joinpath("resources", "prompts.yaml")
    payload = _load_yaml(resource.read_text(encoding="utf-8"), Path(str(resource)))
    return PromptRegistry(_parse_prompt_payload(payload))


async def complete_with_prompt[ResponseT: BaseModel](
    model: StructuredModel,
    *,
    prompt: PromptBundle,
    user_prompt: str,
    response_model: type[ResponseT],
    run_id: UUID,
) -> ResponseT:
    return await model.complete(
        role=prompt.role,
        system_prompt=prompt.system_prompt,
        user_prompt=user_prompt,
        response_model=response_model,
        prompt_version=prompt.version,
        run_id=str(run_id),
    )


def json_prompt(**payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def _load_yaml(content: str, source: Path) -> dict[str, Any]:
    payload = yaml.safe_load(content)
    if not isinstance(payload, dict):
        raise ValueError(f"expected a mapping in YAML file: {source}")
    return payload


def _parse_prompt_payload(payload: dict[str, Any]) -> list[PromptBundle]:
    raw_prompts = payload.get("prompts")
    if not isinstance(raw_prompts, list):
        raise ValueError("prompt registry YAML must define a prompts list")
    return [PromptBundle.model_validate(item) for item in raw_prompts]
