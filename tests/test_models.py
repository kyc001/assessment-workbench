from pathlib import Path

import httpx
import respx
from pydantic import BaseModel

from assessment_workbench.models import OpenAICompatibleModel, _strict_schema
from assessment_workbench.storage import LocalKnowledgeBackend, Workspace


class Answer(BaseModel):
    value: str


@respx.mock
async def test_openai_compatible_structured_response(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "workspace")
    workspace.initialize()
    route = respx.post("https://model.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"value":"ok"}'}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            },
        )
    )
    model = OpenAICompatibleModel(
        base_url="https://model.test/v1",
        api_key="secret",
        model="test-model",
        audit_store=LocalKnowledgeBackend(workspace),
    )

    result = await model.complete(
        role="test",
        system_prompt="system",
        user_prompt="user",
        response_model=Answer,
        prompt_version="v1",
    )

    assert result.value == "ok"
    request = route.calls[0].request
    assert request.headers["authorization"] == "Bearer secret"


def test_strict_schema_requires_defaulted_properties_recursively() -> None:
    schema = _strict_schema(
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "nested": {
                    "type": "object",
                    "properties": {"items": {"type": "array", "items": {"type": "string"}}},
                },
            },
        }
    )
    assert schema["required"] == ["name", "nested"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["nested"]["required"] == ["items"]
    assert schema["properties"]["nested"]["additionalProperties"] is False
