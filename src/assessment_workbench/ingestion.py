import hashlib
import mimetypes
import re
from pathlib import Path
from typing import Any

from assessment_workbench.domain import (
    BlockKind,
    KnowledgeExtraction,
    KnowledgePoint,
    KnowledgeRelation,
    Material,
    MaterialKind,
    MaterialStatus,
    ParsedDocument,
    RelationKind,
    SourceReference,
    WorkflowRun,
)
from assessment_workbench.ports import DocumentParser, StructuredModel
from assessment_workbench.storage import (
    ArtifactStore,
    LocalKnowledgeBackend,
    MaterialStore,
    RunStore,
)
from assessment_workbench.workflow import WorkflowEngine


class MaterialIngestionWorkflow:
    def __init__(
        self,
        parser: DocumentParser,
        knowledge: LocalKnowledgeBackend,
        artifacts: ArtifactStore,
        runs: RunStore,
        materials: MaterialStore,
        model: StructuredModel | None = None,
    ) -> None:
        self.parser = parser
        self.knowledge = knowledge
        self.artifacts = artifacts
        self.engine = WorkflowEngine(runs)
        self.materials = materials
        self.model = model

    async def execute(
        self,
        source: Path,
        course_id: str,
        kind: MaterialKind,
        *,
        semester: str | None = None,
        year: int | None = None,
        language: str = "zh-CN",
    ) -> tuple[WorkflowRun, dict[str, Any]]:
        material = build_material(
            source,
            course_id,
            kind,
            semester=semester,
            year=year,
            language=language,
        )
        self.materials.create(material)

        async def parse(_: dict[str, Any]) -> dict[str, Any]:
            self.materials.transition(material, MaterialStatus.PROCESSING, parser=self.parser.name)
            return {"document": await self.parser.parse(source)}

        async def extract(state: dict[str, Any]) -> dict[str, Any]:
            document: ParsedDocument = state["document"]
            points, relations = extract_heading_graph(course_id, document)
            if self.model is not None:
                extraction = await self.model.complete(
                    role="knowledge_extractor",
                    system_prompt=KNOWLEDGE_SYSTEM_PROMPT,
                    user_prompt=build_knowledge_prompt(document),
                    response_model=KnowledgeExtraction,
                    prompt_version="knowledge-extraction-v1",
                )
                semantic_points, semantic_relations = materialize_extraction(
                    course_id, document, extraction
                )
                points.extend(semantic_points)
                relations.extend(semantic_relations)
            return {"points": points, "relations": relations}

        async def persist(state: dict[str, Any]) -> dict[str, Any]:
            document: ParsedDocument = state["document"]
            points: list[KnowledgePoint] = state["points"]
            relations: list[KnowledgeRelation] = state["relations"]
            self.knowledge.save_document(course_id, document, kind.value)
            self.knowledge.upsert_points(points)
            self.knowledge.upsert_relations(relations)
            document_artifact = self.artifacts.write_json(
                state["run_id"],
                "parsed-document.json",
                document.model_dump(mode="json"),
                created_by_phase="PERSISTING",
            )
            graph_artifact = self.artifacts.write_json(
                state["run_id"],
                "knowledge-graph.json",
                {
                    "points": [point.model_dump(mode="json") for point in points],
                    "relations": [relation.model_dump(mode="json") for relation in relations],
                },
                created_by_phase="PERSISTING",
            )
            self.materials.transition(
                material,
                MaterialStatus.READY,
                parser=self.parser.name,
                parsed_document_id=document.id,
            )
            return {
                "output_artifact_ids": [document_artifact.id, graph_artifact.id],
                "artifacts": [document_artifact, graph_artifact],
            }

        run, state = await self.engine.execute(
            "material_ingestion",
            [
                ("PARSING", parse),
                ("KNOWLEDGE_EXTRACTING", extract),
                ("PERSISTING", persist),
            ],
            {
                "course_id": course_id,
                "kind": kind.value,
                "material_id": str(material.id),
            },
        )
        if run.status == "failed":
            self.materials.transition(material, MaterialStatus.FAILED, parser=self.parser.name)
        return run, state


def build_material(
    source: Path,
    course_id: str,
    kind: MaterialKind,
    *,
    semester: str | None = None,
    year: int | None = None,
    language: str = "zh-CN",
) -> Material:
    resolved = source.resolve()
    digest = hashlib.sha256()
    with resolved.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return Material(
        course_id=course_id,
        kind=kind,
        source_path=resolved,
        original_name=resolved.name,
        sha256=digest.hexdigest(),
        mime_type=mimetypes.guess_type(resolved.name)[0] or "application/octet-stream",
        size_bytes=resolved.stat().st_size,
        semester=semester,
        year=year,
        language=language,
    )


def extract_heading_graph(
    course_id: str, document: ParsedDocument
) -> tuple[list[KnowledgePoint], list[KnowledgeRelation]]:
    points: list[KnowledgePoint] = []
    relations: list[KnowledgeRelation] = []
    path_ids: list[str] = []
    seen: set[str] = set()

    for block in document.blocks:
        if block.kind is not BlockKind.HEADING:
            continue
        slug_parts = [_slug(value) for value in block.heading_path if value]
        slug = ".".join(part for part in slug_parts if part)
        if not slug or slug in seen:
            continue
        point_id = f"{course_id}:{slug}"
        level = max(len(block.heading_path), 1)
        path_ids = path_ids[: level - 1]
        parent_id = path_ids[-1] if path_ids else None
        evidence = SourceReference(
            document_id=document.id,
            block_id=block.id,
            page=block.page,
            excerpt=block.content[:200],
        )
        points.append(
            KnowledgePoint(
                id=point_id,
                course_id=course_id,
                name=block.content,
                slug=slug,
                parent_id=parent_id,
                tags=[f"level:{level}"],
                evidence=[evidence],
            )
        )
        if parent_id:
            relations.append(
                KnowledgeRelation(
                    source_id=parent_id,
                    target_id=point_id,
                    kind=RelationKind.CONTAINS,
                    evidence=[evidence],
                )
            )
        path_ids.append(point_id)
        seen.add(slug)
    return points, relations


def _slug(value: str) -> str:
    normalized = re.sub(r"[^\w\u4e00-\u9fff]+", "-", value.strip().lower())
    return normalized.strip("-")


def build_knowledge_prompt(document: ParsedDocument) -> str:
    blocks = [
        {
            "id": block.id,
            "page": block.page,
            "kind": block.kind,
            "heading_path": block.heading_path,
            "content": block.content[:2000],
        }
        for block in document.blocks
        if block.content.strip()
    ]
    import json

    return json.dumps(
        {"document_id": document.id, "title": document.title, "blocks": blocks},
        ensure_ascii=False,
    )


def materialize_extraction(
    course_id: str, document: ParsedDocument, extraction: KnowledgeExtraction
) -> tuple[list[KnowledgePoint], list[KnowledgeRelation]]:
    blocks = {block.id: block for block in document.blocks}
    points: list[KnowledgePoint] = []
    key_to_id: dict[str, str] = {}
    for node in extraction.nodes:
        slug = f"semantic.{node.kind}.{_slug(node.name)}"
        point_id = f"{course_id}:{slug}"
        key_to_id[node.key] = point_id
        evidence = [
            SourceReference(
                document_id=document.id,
                block_id=block.id,
                page=block.page,
                excerpt=block.content[:200],
            )
            for block_id in node.source_block_ids
            if (block := blocks.get(block_id)) is not None
        ]
        if not evidence:
            continue
        points.append(
            KnowledgePoint(
                id=point_id,
                course_id=course_id,
                name=node.name,
                slug=slug,
                description=node.description,
                tags=[f"kind:{node.kind}", *node.tags, *[f"alias:{a}" for a in node.aliases]],
                evidence=evidence,
                confidence=node.confidence,
            )
        )

    relations: list[KnowledgeRelation] = []
    for relation in extraction.relations:
        source_id = key_to_id.get(relation.source_key)
        target_id = key_to_id.get(relation.target_key)
        if source_id is None or target_id is None or source_id == target_id:
            continue
        evidence = [
            SourceReference(
                document_id=document.id,
                block_id=block.id,
                page=block.page,
                excerpt=block.content[:200],
            )
            for block_id in relation.source_block_ids
            if (block := blocks.get(block_id)) is not None
        ]
        if not evidence:
            continue
        relations.append(
            KnowledgeRelation(
                source_id=source_id,
                target_id=target_id,
                kind=relation.kind,
                evidence=evidence,
                confidence=relation.confidence,
            )
        )
    return points, relations


KNOWLEDGE_SYSTEM_PROMPT = """You extract auditable course knowledge from parsed documents.
Return only entities explicitly supported by the supplied blocks. Use stable short keys that are
unique within this response. Every node and relation must cite one or more source_block_ids. Do not
infer a prerequisite, derivation, or application relation unless the text supports it. Prefer
educational entities such as concepts, definitions, theorems, laws, formulas, experiments, and
problem patterns. Do not repeat headings that merely organize the document."""
