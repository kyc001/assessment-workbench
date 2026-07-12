from pathlib import Path

from assessment_workbench.domain import BlockKind
from assessment_workbench.parsers import FixtureParser, normalize_mineru_payload


async def test_fixture_parser_loads_document() -> None:
    source = Path(__file__).parent / "fixtures" / "sample_course.json"
    document = await FixtureParser().parse(source)
    assert document.id == "doc-demo-physics-01"
    assert any(block.kind is BlockKind.EQUATION for block in document.blocks)


def test_normalizes_wrapped_mineru_content_list(tmp_path: Path) -> None:
    source = tmp_path / "lecture.pdf"
    source.write_bytes(b"fake-pdf")
    payload = {
        "data": {
            "content_list": [
                {"type": "title", "text": "Mechanics", "page_idx": 0, "level": 1},
                {"type": "equation", "latex": "F=ma", "page_idx": 1},
            ]
        }
    }
    document = normalize_mineru_payload(source, payload, "mineru-api")
    assert document.blocks[0].page == 1
    assert document.blocks[0].kind is BlockKind.HEADING
    assert document.blocks[1].kind is BlockKind.EQUATION
