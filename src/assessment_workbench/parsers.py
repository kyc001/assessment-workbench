import asyncio
import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

import httpx

from assessment_workbench.domain import BlockKind, ContentBlock, ParsedDocument


class FixtureParser:
    name = "fixture"

    async def parse(self, source: Path) -> ParsedDocument:
        payload = json.loads(source.read_text(encoding="utf-8"))
        document_payload = payload.get("document", payload)
        return ParsedDocument.model_validate(document_payload)


class MinerUApiParser:
    name = "mineru-api"

    def __init__(self, base_url: str, timeout: float = 300.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def parse(self, source: Path) -> ParsedDocument:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            with source.open("rb") as stream:
                response = await client.post(
                    f"{self.base_url}/file_parse",
                    files={"files": (source.name, stream, "application/octet-stream")},
                    data={"return_content_list": "true", "return_middle_json": "true"},
                )
            response.raise_for_status()
            payload = response.json()
        return normalize_mineru_payload(source, payload, self.name)


class MinerUCliParser:
    name = "mineru-cli"

    def __init__(self, command: str = "mineru") -> None:
        self.command = command

    async def parse(self, source: Path) -> ParsedDocument:
        executable = shutil.which(self.command)
        if executable is None:
            raise RuntimeError(
                f"MinerU command not found: {self.command}. Install MinerU or use mineru-api."
            )
        with tempfile.TemporaryDirectory(prefix="awb-mineru-") as temporary:
            output = Path(temporary)
            process = await asyncio.create_subprocess_exec(
                executable,
                "-p",
                str(source.resolve()),
                "-o",
                str(output),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()
            if process.returncode != 0:
                detail = stderr.decode(errors="replace") or stdout.decode(errors="replace")
                raise RuntimeError(f"MinerU failed with exit code {process.returncode}: {detail}")
            candidates = list(output.rglob("*content_list.json"))
            if not candidates:
                raise RuntimeError("MinerU did not produce a content_list JSON file")
            payload = json.loads(candidates[0].read_text(encoding="utf-8"))
        return normalize_mineru_payload(source, payload, self.name)


def normalize_mineru_payload(source: Path, payload: Any, parser: str) -> ParsedDocument:
    content_list = _find_content_list(payload)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
    blocks: list[ContentBlock] = []
    heading_path: list[str] = []
    for index, item in enumerate(content_list):
        item_type = str(item.get("type", "text"))
        content = _content_from_item(item)
        if not content:
            continue
        kind = _block_kind(item_type)
        if kind is BlockKind.HEADING:
            level = int(item.get("level", 1))
            heading_path = heading_path[: max(level - 1, 0)] + [content]
        blocks.append(
            ContentBlock(
                id=f"{digest}-b{index:05d}",
                kind=kind,
                page=int(item.get("page_idx", item.get("page", 0))) + 1,
                content=content,
                heading_path=list(heading_path),
                metadata={"mineru_type": item_type},
            )
        )
    return ParsedDocument(
        id=f"doc-{digest}",
        source_path=str(source.resolve()),
        title=source.stem,
        blocks=blocks,
        parser=parser,
    )


def _find_content_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        raise ValueError("unsupported MinerU response")
    for key in ("content_list", "content", "result", "data"):
        candidate = payload.get(key)
        if isinstance(candidate, list):
            return [item for item in candidate if isinstance(item, dict)]
        if isinstance(candidate, dict):
            try:
                return _find_content_list(candidate)
            except ValueError:
                pass
    for candidate in payload.values():
        if isinstance(candidate, (dict, list)):
            try:
                return _find_content_list(candidate)
            except ValueError:
                pass
    raise ValueError("MinerU response does not contain a content list")


def _content_from_item(item: dict[str, Any]) -> str:
    for key in ("text", "latex", "table_body", "content"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    captions = item.get("image_caption") or item.get("table_caption")
    if isinstance(captions, list):
        return "\n".join(str(value) for value in captions if value)
    return ""


def _block_kind(item_type: str) -> BlockKind:
    normalized = item_type.lower()
    if normalized in {"title", "heading", "header"}:
        return BlockKind.HEADING
    if normalized in {"equation", "formula", "interline_equation"}:
        return BlockKind.EQUATION
    if normalized == "table":
        return BlockKind.TABLE
    if normalized in {"image", "figure"}:
        return BlockKind.IMAGE
    return BlockKind.TEXT
