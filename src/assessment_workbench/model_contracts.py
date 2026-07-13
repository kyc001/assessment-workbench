import hashlib
import json
from typing import Any


def canonical_json_sha256(payload: object) -> str:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def strict_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(schema)
    properties = normalized.get("properties")
    if isinstance(properties, dict):
        normalized["additionalProperties"] = False
        normalized["required"] = list(properties)
        normalized["properties"] = {
            key: strict_json_schema(value) if isinstance(value, dict) else value
            for key, value in properties.items()
        }
    for key in ("$defs", "definitions"):
        definitions = normalized.get(key)
        if isinstance(definitions, dict):
            normalized[key] = {
                name: strict_json_schema(value) if isinstance(value, dict) else value
                for name, value in definitions.items()
            }
    items = normalized.get("items")
    if isinstance(items, dict):
        normalized["items"] = strict_json_schema(items)
    for key in ("anyOf", "oneOf", "allOf"):
        variants = normalized.get(key)
        if isinstance(variants, list):
            normalized[key] = [
                strict_json_schema(value) if isinstance(value, dict) else value
                for value in variants
            ]
    return normalized
