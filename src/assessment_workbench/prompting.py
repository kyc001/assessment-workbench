from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from importlib.resources import files
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml
from pydantic import BaseModel, ConfigDict, Field

from assessment_workbench.domain import (
    ContextArtifactBinding,
    ContextPack,
    ModelAuditContext,
)
from assessment_workbench.model_contracts import canonical_json_sha256, strict_json_schema
from assessment_workbench.ports import StructuredModel
from assessment_workbench.storage import ArtifactStore


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
    artifacts: ArtifactStore,
    created_by_phase: str,
    input_artifact_ids: Iterable[UUID] = (),
) -> ResponseT:
    try:
        payload = json.loads(user_prompt)
    except json.JSONDecodeError as exc:
        raise ValueError("model user prompt must be a JSON object") from exc
    if not isinstance(payload, dict):
        raise ValueError("model user prompt must be a JSON object")
    bindings = _context_artifact_bindings(artifacts, input_artifact_ids)
    context_pack = ContextPack(
        prompt_key=prompt.key,
        role=prompt.role,
        prompt_version=prompt.version,
        response_model=response_model.__name__,
        user_prompt_sha256=_text_sha256(user_prompt),
        payload=payload,
        input_artifacts=bindings,
    )
    context_artifact = artifacts.write_json(
        run_id,
        "model-context.json",
        context_pack.model_dump(mode="json"),
        created_by_phase=created_by_phase,
    )
    audit_context = ModelAuditContext(
        context_pack_id=context_artifact.id,
        context_pack_sha256=context_artifact.sha256,
        system_prompt_sha256=_text_sha256(prompt.system_prompt),
        response_schema_sha256=canonical_json_sha256(
            strict_json_schema(response_model.model_json_schema())
        ),
    )
    return await model.complete(
        role=prompt.role,
        system_prompt=prompt.system_prompt,
        user_prompt=user_prompt,
        response_model=response_model,
        prompt_version=prompt.version,
        run_id=str(run_id),
        audit_context=audit_context,
    )


def json_prompt(**payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def context_artifact_ids(state: dict[str, Any]) -> list[UUID]:
    bindings = state.get("_checkpoint_artifacts")
    if not isinstance(bindings, dict):
        return []
    artifact_ids: list[UUID] = []
    for value in bindings.values():
        if isinstance(value, UUID):
            artifact_ids.append(value)
        elif isinstance(value, str):
            artifact_ids.append(UUID(value))
        else:
            raise TypeError("checkpoint artifact binding must contain UUID values")
    return list(dict.fromkeys(artifact_ids))


def _context_artifact_bindings(
    artifacts: ArtifactStore,
    artifact_ids: Iterable[UUID],
) -> list[ContextArtifactBinding]:
    bindings: list[ContextArtifactBinding] = []
    for artifact_id in dict.fromkeys(artifact_ids):
        artifact = artifacts.get(artifact_id)
        if artifact is None or not artifacts.verify(artifact_id):
            raise ValueError(f"model context input artifact is missing or invalid: {artifact_id}")
        bindings.append(
            ContextArtifactBinding(
                artifact_id=artifact.id,
                run_id=artifact.run_id,
                logical_name=artifact.logical_name,
                version=artifact.version,
                sha256=artifact.sha256,
            )
        )
    return bindings


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


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
