import asyncio
import hashlib
import json
from datetime import UTC, datetime
from typing import Any, TypeVar, cast
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from pydantic import BaseModel, ValidationError

from assessment_workbench.domain import ModelAuditContext, ModelCall, ModelUsage
from assessment_workbench.model_contracts import canonical_json_sha256, strict_json_schema
from assessment_workbench.ports import ModelAuditStore

ResponseT = TypeVar("ResponseT", bound=BaseModel)
_strict_schema = strict_json_schema


class OpenAICompatibleModel:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        audit_store: ModelAuditStore,
        timeout: float = 300,
        max_concurrency: int = 6,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.audit_store = audit_store
        self.timeout = timeout
        self._request_semaphore = asyncio.Semaphore(max_concurrency)

    async def complete(
        self,
        *,
        role: str,
        system_prompt: str,
        user_prompt: str,
        response_model: type[ResponseT],
        prompt_version: str,
        run_id: str | None = None,
        audit_context: ModelAuditContext | None = None,
    ) -> ResponseT:
        if not self.api_key:
            raise RuntimeError("AW_LLM_API_KEY is required for LLM knowledge extraction")
        request = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": response_model.__name__,
                    "strict": True,
                    "schema": _strict_schema(response_model.model_json_schema()),
                },
            },
            "temperature": 0,
        }
        request_sha256 = _sha(request)
        call = ModelCall(
            run_id=UUID(run_id) if run_id else None,
            role=role,
            model=self.model,
            prompt_version=prompt_version,
            request_sha256=request_sha256,
            request_sha256_sequence=[request_sha256],
            audit_context=audit_context,
            endpoint_origin=_endpoint_origin(self.base_url),
            status="running",
        )
        self.audit_store.save_model_call(call)
        try:
            payload = await self._post(request)
            content = _response_content(payload)
            try:
                result = response_model.model_validate_json(content)
            except ValidationError as exc:
                repair_request = dict(request)
                messages = list(cast(list[dict[str, str]], request["messages"]))
                repair_request["messages"] = [
                    *messages,
                    {"role": "assistant", "content": content},
                    {
                        "role": "user",
                        "content": (
                            "The previous JSON failed schema validation. Return a corrected JSON "
                            "object only, preserving valid content. Validation errors: "
                            f"{json.dumps(exc.errors(include_url=False), default=str)}"
                        ),
                    },
                ]
                call.repair_count += 1
                call.request_sha256_sequence.append(_sha(repair_request))
                payload = await self._post(repair_request)
                content = _response_content(payload)
                result = response_model.model_validate_json(content)
            usage = payload.get("usage", {})
            call.status = "succeeded"
            call.response_sha256 = hashlib.sha256(content.encode()).hexdigest()
            provider_request_id = payload.get("id")
            call.provider_request_id = (
                provider_request_id if isinstance(provider_request_id, str) else None
            )
            call.finish_reason = _finish_reason(payload)
            call.usage = ModelUsage.model_validate(usage)
            call.completed_at = datetime.now(UTC)
            self.audit_store.save_model_call(call)
            return result
        except Exception as exc:
            call.status = "failed"
            call.error = str(exc)
            call.completed_at = datetime.now(UTC)
            self.audit_store.save_model_call(call)
            raise

    async def _post(self, request: dict[str, Any]) -> dict[str, Any]:
        async with (
            self._request_semaphore,
            httpx.AsyncClient(timeout=self.timeout) as client,
        ):
            for attempt in range(3):
                try:
                    response = await client.post(
                        f"{self.base_url}/chat/completions",
                        headers={"Authorization": f"Bearer {self.api_key}"},
                        json=request,
                    )
                    response.raise_for_status()
                    payload = response.json()
                    if not isinstance(payload, dict):
                        raise ValueError("model response payload is not an object")
                    return payload
                except (httpx.TimeoutException, httpx.NetworkError):
                    if attempt == 2:
                        raise
                except httpx.HTTPStatusError as exc:
                    if attempt == 2 or exc.response.status_code not in {
                        429,
                        502,
                        503,
                        504,
                        524,
                    }:
                        raise
                await asyncio.sleep(0.5 * (2**attempt))
        raise RuntimeError("model request retry loop exited unexpectedly")


def _sha(payload: dict[str, Any]) -> str:
    return canonical_json_sha256(payload)


def _response_content(payload: dict[str, Any]) -> str:
    content = payload["choices"][0]["message"]["content"]
    if not isinstance(content, str):
        raise ValueError("model response content is not text")
    return content


def _finish_reason(payload: dict[str, Any]) -> str | None:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None
    finish_reason = choices[0].get("finish_reason")
    return finish_reason if isinstance(finish_reason, str) else None


def _endpoint_origin(base_url: str) -> str:
    parsed = urlsplit(base_url)
    if not parsed.scheme or not parsed.netloc:
        return base_url
    return f"{parsed.scheme}://{parsed.netloc}"
