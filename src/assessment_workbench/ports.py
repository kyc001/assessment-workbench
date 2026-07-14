from pathlib import Path
from typing import Protocol, TypeVar

from pydantic import BaseModel

from assessment_workbench.domain import (
    KnowledgePoint,
    KnowledgeRelation,
    ModelAuditContext,
    ModelCall,
    ParsedDocument,
    RetrievalHit,
)

ResponseT = TypeVar("ResponseT", bound=BaseModel)


class DocumentParser(Protocol):
    name: str

    async def parse(self, source: Path) -> ParsedDocument: ...


class KnowledgeBackend(Protocol):
    def save_document(self, course_id: str, document: ParsedDocument, kind: str) -> None: ...

    def upsert_points(self, points: list[KnowledgePoint]) -> None: ...

    def upsert_relations(self, relations: list[KnowledgeRelation]) -> None: ...

    def list_points(self, course_id: str) -> list[KnowledgePoint]: ...

    def get_point(self, course_id: str, slug: str) -> KnowledgePoint | None: ...

    def search(self, course_id: str, query: str, limit: int = 10) -> list[RetrievalHit]: ...

    def expand(self, course_id: str, slugs: list[str], depth: int = 1) -> list[KnowledgePoint]: ...


class StructuredModel(Protocol):
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
    ) -> ResponseT: ...


class ModelAuditStore(Protocol):
    def save_model_call(self, call: ModelCall) -> None: ...
