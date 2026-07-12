import hashlib
import json
from datetime import UTC, datetime
from typing import Any, TypeVar
from uuid import UUID

import httpx
from pydantic import BaseModel

from assessment_workbench.domain import ModelCall, ModelUsage
from assessment_workbench.ports import ModelAuditStore

ResponseT = TypeVar("ResponseT", bound=BaseModel)


class OpenAICompatibleModel:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        audit_store: ModelAuditStore,
        timeout: float = 300,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.audit_store = audit_store
        self.timeout = timeout

    async def complete(
        self,
        *,
        role: str,
        system_prompt: str,
        user_prompt: str,
        response_model: type[ResponseT],
        prompt_version: str,
        run_id: str | None = None,
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
                    "schema": response_model.model_json_schema(),
                },
            },
            "temperature": 0,
        }
        call = ModelCall(
            run_id=UUID(run_id) if run_id else None,
            role=role,
            model=self.model,
            prompt_version=prompt_version,
            request_sha256=_sha(request),
            status="running",
        )
        self.audit_store.save_model_call(call)
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=request,
                )
                response.raise_for_status()
                payload = response.json()
            content = payload["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise ValueError("model response content is not text")
            result = response_model.model_validate_json(content)
            usage = payload.get("usage", {})
            call.status = "succeeded"
            call.response_sha256 = hashlib.sha256(content.encode()).hexdigest()
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


def _sha(payload: dict[str, Any]) -> str:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode()).hexdigest()
