import asyncio
import hashlib
import ipaddress
import json
from datetime import UTC, datetime
from typing import Any, TypeVar, cast
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from pydantic import BaseModel, ValidationError

from assessment_workbench.domain import ModelAuditContext, ModelCall, ModelUsage
from assessment_workbench.errors import RetryableWorkflowError, is_retryable_http_status
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
        schema_in_prompt: bool = False,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.audit_store = audit_store
        self.timeout = timeout
        self.schema_in_prompt = schema_in_prompt
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
        response_schema = _strict_schema(response_model.model_json_schema())
        schema_json = json.dumps(response_schema, ensure_ascii=False, separators=(",", ":"))
        request_system_prompt = system_prompt
        if self.schema_in_prompt:
            request_system_prompt = (
                f"{system_prompt}\n\n"
                "Return one JSON object only that matches this response schema exactly. "
                f"Response JSON Schema: {schema_json}"
            )
        request = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": request_system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": response_model.__name__,
                    "strict": True,
                    "schema": response_schema,
                },
            },
            "stream": True,
            "stream_options": {"include_usage": True},
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
                            f"{json.dumps(exc.errors(include_url=False), default=str)}. "
                            "Expected JSON Schema: "
                            f"{schema_json}"
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
            httpx.AsyncClient(
                timeout=self.timeout,
                trust_env=not _is_loopback_endpoint(self.base_url),
            ) as client,
        ):
            try:
                async with asyncio.timeout(self.timeout):
                    for attempt in range(3):
                        try:
                            async with client.stream(
                                "POST",
                                f"{self.base_url}/chat/completions",
                                headers={
                                    "Accept": "text/event-stream",
                                    "Authorization": f"Bearer {self.api_key}",
                                },
                                json=request,
                            ) as response:
                                response.raise_for_status()
                                return await _read_response_payload(response)
                        except (
                            httpx.TimeoutException,
                            httpx.NetworkError,
                            httpx.RemoteProtocolError,
                        ) as exc:
                            if attempt == 2:
                                raise RetryableWorkflowError(str(exc)) from exc
                        except httpx.HTTPStatusError as exc:
                            if not is_retryable_http_status(exc.response.status_code):
                                raise
                            if attempt == 2:
                                raise RetryableWorkflowError(str(exc)) from exc
                        except ValueError as exc:
                            if str(exc) != "model stream completed without response content":
                                raise
                            if attempt == 2:
                                raise RetryableWorkflowError(str(exc)) from exc
                        await asyncio.sleep(0.5 * (2**attempt))
            except TimeoutError as exc:
                raise RetryableWorkflowError(
                    f"model request exceeded total timeout of {self.timeout:g} seconds"
                ) from exc
        raise RuntimeError("model request retry loop exited unexpectedly")


async def _read_response_payload(response: httpx.Response) -> dict[str, Any]:
    content_type = response.headers.get("content-type", "").casefold()
    if "text/event-stream" not in content_type:
        await response.aread()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("model response payload is not an object")
        return payload

    content_parts: list[str] = []
    usage: dict[str, Any] = {}
    provider_request_id: str | None = None
    finish_reason: str | None = None
    async for line in response.aiter_lines():
        if not line.startswith("data:"):
            continue
        data = line.removeprefix("data:").strip()
        if not data or data == "[DONE]":
            continue
        event = json.loads(data)
        if not isinstance(event, dict):
            raise ValueError("model stream event is not an object")
        event_id = event.get("id")
        if provider_request_id is None and isinstance(event_id, str):
            provider_request_id = event_id
        event_usage = event.get("usage")
        if isinstance(event_usage, dict):
            usage = event_usage
        choices = event.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            continue
        choice = choices[0]
        choice_finish_reason = choice.get("finish_reason")
        if isinstance(choice_finish_reason, str):
            finish_reason = choice_finish_reason
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            continue
        content = delta.get("content")
        if isinstance(content, str):
            content_parts.append(content)

    if not content_parts:
        raise ValueError("model stream completed without response content")
    return {
        "id": provider_request_id,
        "choices": [
            {
                "message": {"content": "".join(content_parts)},
                "finish_reason": finish_reason,
            }
        ],
        "usage": usage,
    }


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


def _is_loopback_endpoint(base_url: str) -> bool:
    hostname = urlsplit(base_url).hostname
    if hostname is None:
        return False
    if hostname.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False
