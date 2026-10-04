from typing import Any

from _span_test_utils import inline

from docvortex.postprocess.pages import model_json_to_pages
from docvortex.schema import BlockType, ImageBlock, ModelJson, Producer


def _model_json(pages: list[list[dict[str, Any]]]) -> ModelJson:
    """Construct minimally stringent ModelJson for raw block objectified regression."""
    return ModelJson(
        pages=pages,
        page_index_map=[],
        metadata={"file_suffix": "pdf", "producer": Producer(name="docvortex", version="test")},
        extensions={},
    )


def test_postprocess_groups_pdf_visual_blocks_without_dict_access() -> None:
    """Verification PDF raw Vision blocks are grouped at strict object boundaries."""
    page = model_json_to_pages(
        _model_json(
            [
                [
                    {
                        "type": BlockType.IMAGE_CAPTION,
                        "bbox": [0.05, 0.05, 0.6, 0.12],
                        "content": inline("Figure 1"),
                    },
                    {
                        "type": BlockType.IMAGE,
                        "bbox": [0.05, 0.15, 0.6, 0.45],
                    },
                ]
            ]
        )
    )[0]

    assert len(page.blocks) == 1
    assert isinstance(page.blocks[0], ImageBlock)
    assert {block.type for block in page.blocks[0].content} == {
        BlockType.IMAGE_BODY,
        BlockType.IMAGE_CAPTION,
    }


def test_postprocess_groups_raw_vision_footnote_as_image_footnote() -> None:
    """Verify that the generic raw footnote is relegated to the adjacent image instead of the footer."""
    page = model_json_to_pages(
        _model_json(
            [
                [
                    {"type": BlockType.IMAGE, "bbox": [0.1, 0.1, 0.6, 0.45]},
                    {"type": "footnote", "bbox": [0.12, 0.47, 0.6, 0.6], "content": inline("Figure note")},
                ]
            ]
        )
    )[0]

    assert len(page.blocks) == 1
    assert isinstance(page.blocks[0], ImageBlock)
    assert {block.type for block in page.blocks[0].content} == {
        BlockType.IMAGE_BODY,
        BlockType.IMAGE_FOOTNOTE,
    }
