import json
from pathlib import Path

import httpx
import pytest
import respx
from pydantic import BaseModel

from assessment_workbench.domain import ContextPack, ModelCall
from assessment_workbench.errors import RetryableWorkflowError
from assessment_workbench.model_contracts import canonical_json_sha256, strict_json_schema
from assessment_workbench.models import (
    OpenAICompatibleModel,
    _is_loopback_endpoint,
    _strict_schema,
)
from assessment_workbench.prompting import PromptBundle, complete_with_prompt, json_prompt
from assessment_workbench.storage import (
    ArtifactStore,
    LocalKnowledgeBackend,
    RunStore,
    Workspace,
)


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
                "id": "response-1",
                "choices": [
                    {
                        "message": {"content": '{"value":"ok"}'},
                        "finish_reason": "stop",
                    }
                ],
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
    call = _stored_model_call(workspace)
    assert call.request_sha256_sequence == [call.request_sha256]
    assert call.provider_request_id == "response-1"
    assert call.finish_reason == "stop"
    assert call.endpoint_origin == "https://model.test"


@respx.mock
async def test_schema_can_be_embedded_for_compatibility_proxy(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "workspace")
    workspace.initialize()
    route = respx.post("https://model.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "response-schema-prompt",
                "choices": [
                    {"message": {"content": '{"value":"ok"}'}, "finish_reason": "stop"}
                ],
                "usage": {},
            },
        )
    )
    model = OpenAICompatibleModel(
        base_url="https://model.test/v1",
        api_key="secret",
        model="test-model",
        audit_store=LocalKnowledgeBackend(workspace),
        schema_in_prompt=True,
    )

    result = await model.complete(
        role="test",
        system_prompt="system",
        user_prompt="user",
        response_model=Answer,
        prompt_version="v1",
    )

    assert result.value == "ok"
    request = json.loads(route.calls[0].request.content)
    system_prompt = request["messages"][0]["content"]
    assert "Response JSON Schema" in system_prompt
    assert '"value"' in system_prompt


@respx.mock
async def test_prompt_execution_writes_reproducible_context_pack(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "workspace")
    workspace.initialize()
    runs = RunStore(workspace)
    run = runs.create("context-pack")
    artifacts = ArtifactStore(workspace)
    source = artifacts.write_json(run.id, "source.json", {"topic": "algebra"})
    respx.post("https://model.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "response-context",
                "choices": [
                    {
                        "message": {"content": '{"value":"ok"}'},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {},
            },
        )
    )
    model = OpenAICompatibleModel(
        base_url="https://model.test/v1",
        api_key="do-not-persist",
        model="test-model",
        audit_store=LocalKnowledgeBackend(workspace),
    )
    user_prompt = json_prompt(question={"number": 1}, source="course material")

    result = await complete_with_prompt(
        model,
        prompt=PromptBundle(
            key="test_prompt",
            role="test_role",
            version="test-v1",
            system_prompt="Return a structured answer.",
        ),
        user_prompt=user_prompt,
        response_model=Answer,
        run_id=run.id,
        artifacts=artifacts,
        created_by_phase="TESTING",
        input_artifact_ids=[source.id],
    )

    assert result.value == "ok"
    context_ref = artifacts.latest(run.id, "model-context.json")
    assert context_ref is not None
    context = ContextPack.model_validate(artifacts.read_json(context_ref.id))
    assert json_prompt(**context.payload) == user_prompt
    assert context.input_artifacts[0].artifact_id == source.id
    assert context.input_artifacts[0].sha256 == source.sha256
    call = _stored_model_call(workspace)
    assert call.audit_context is not None
    assert call.audit_context.context_pack_id == context_ref.id
    assert call.audit_context.context_pack_sha256 == context_ref.sha256
    assert call.audit_context.response_schema_sha256 == canonical_json_sha256(
        strict_json_schema(Answer.model_json_schema())
    )
    persisted = artifacts.read_bytes(context_ref.id).decode("utf-8") + call.model_dump_json()
    assert "do-not-persist" not in persisted
    assert "Authorization" not in persisted


@respx.mock
async def test_model_repair_is_a_single_audited_call(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "workspace")
    workspace.initialize()
    responses = [
        httpx.Response(
            200,
            json={
                "id": "invalid-response",
                "choices": [{"message": {"content": '{"wrong":1}'}, "finish_reason": "stop"}],
                "usage": {},
            },
        ),
        httpx.Response(
            200,
            json={
                "id": "repaired-response",
                "choices": [{"message": {"content": '{"value":"fixed"}'}, "finish_reason": "stop"}],
                "usage": {},
            },
        ),
    ]
    route = respx.post("https://model.test/v1/chat/completions").mock(side_effect=responses)
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

    assert result.value == "fixed"
    call = _stored_model_call(workspace)
    assert call.repair_count == 1
    assert len(call.request_sha256_sequence) == 2
    assert call.request_sha256_sequence[0] == call.request_sha256
    assert call.provider_request_id == "repaired-response"
    repair_request = json.loads(route.calls[1].request.content)
    repair_prompt = repair_request["messages"][-1]["content"]
    assert "Expected JSON Schema" in repair_prompt
    assert '"value"' in repair_prompt


@respx.mock
async def test_empty_stream_retries_then_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = Workspace(tmp_path / "workspace")
    workspace.initialize()
    route = respx.post("https://model.test/v1/chat/completions").mock(
        side_effect=[
            httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text="data: [DONE]\n\n",
            ),
            httpx.Response(
                200,
                json={
                    "id": "response-after-empty-stream",
                    "choices": [
                        {"message": {"content": '{"value":"ok"}'}, "finish_reason": "stop"}
                    ],
                    "usage": {},
                },
            ),
        ]
    )
    model = OpenAICompatibleModel(
        base_url="https://model.test/v1",
        api_key="secret",
        model="test-model",
        audit_store=LocalKnowledgeBackend(workspace),
    )

    async def skip_backoff(_: float) -> None:
        return None

    monkeypatch.setattr("assessment_workbench.models.asyncio.sleep", skip_backoff)

    result = await model.complete(
        role="test",
        system_prompt="system",
        user_prompt="user",
        response_model=Answer,
        prompt_version="v1",
    )

    assert result.value == "ok"
    assert route.call_count == 2


@respx.mock
async def test_remote_protocol_error_retries_then_interrupts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = Workspace(tmp_path / "workspace")
    workspace.initialize()
    route = respx.post("https://model.test/v1/chat/completions").mock(
        side_effect=httpx.RemoteProtocolError("Server disconnected without sending a response.")
    )
    model = OpenAICompatibleModel(
        base_url="https://model.test/v1",
        api_key="secret",
        model="test-model",
        audit_store=LocalKnowledgeBackend(workspace),
    )

    async def skip_backoff(_: float) -> None:
        return None

    monkeypatch.setattr("assessment_workbench.models.asyncio.sleep", skip_backoff)

    with pytest.raises(RetryableWorkflowError, match="Server disconnected"):
        await model.complete(
            role="test",
            system_prompt="system",
            user_prompt="user",
            response_model=Answer,
            prompt_version="v1",
        )

    assert route.call_count == 3
    call = _stored_model_call(workspace)
    assert call.status == "failed"
    assert call.error == "Server disconnected without sending a response."


@respx.mock
async def test_local_protocol_error_is_not_retried(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "workspace")
    workspace.initialize()
    route = respx.post("https://model.test/v1/chat/completions").mock(
        side_effect=httpx.LocalProtocolError("invalid client protocol state")
    )
    model = OpenAICompatibleModel(
        base_url="https://model.test/v1",
        api_key="secret",
        model="test-model",
        audit_store=LocalKnowledgeBackend(workspace),
    )

    with pytest.raises(httpx.LocalProtocolError, match="invalid client protocol state"):
        await model.complete(
            role="test",
            system_prompt="system",
            user_prompt="user",
            response_model=Answer,
            prompt_version="v1",
        )

    assert route.call_count == 1


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


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("http://127.0.0.1:8081/v1", True),
        ("http://localhost:8081/v1", True),
        ("http://[::1]:8081/v1", True),
        ("https://model.test/v1", False),
    ],
)
def test_loopback_endpoint_detection(base_url: str, expected: bool) -> None:
    assert _is_loopback_endpoint(base_url) is expected


def _stored_model_call(workspace: Workspace) -> ModelCall:
    with workspace.connect() as connection:
        row = connection.execute("SELECT payload FROM model_calls").fetchone()
    assert row is not None
    return ModelCall.model_validate_json(row["payload"])
