"""Guards linear continuation planning, inline merging, and object ownership within a single call."""

from __future__ import annotations

from copy import deepcopy

import pytest

from docvortex import api
from docvortex.content.inline import join_inline_spans
from docvortex.render._internal.common.planner import build_render_plan
from docvortex.render.contracts import RenderMode, EpubRenderOptions, MarkdownRenderOptions
from docvortex.schema import MiddleJson, PageInfo, TextBlock, TextSpan, ListBlock, PageAuxTextBlock, RefTextBlock


def text_block(index: int, text: str, **kwargs: object) -> TextBlock:
    """Construct text blocks with stable indexes."""
    return TextBlock(type="text", index=index, content=[TextSpan(type="text", content=text)], **kwargs)


def test_reference_barriers_anchors_and_page_modes() -> None:
    """Ordinary text can be continued across transparent blocks, references cannot cross text barriers, and anchor points are retained independently."""
    middle = MiddleJson(
        pages=[
            PageInfo(page_idx=0, blocks=[text_block(0, "A"), text_block(1, "B", continues_prev=True, anchor="b")]),
            PageInfo(
                page_idx=1,
                blocks=[
                    text_block(0, "C", continues_prev=True),
                    RefTextBlock(type="ref_text", index=1, content=[TextSpan(type="text", content="R1")]),
                    PageAuxTextBlock(type="header", index=2, content=[TextSpan(type="text", content="header")]),
                    RefTextBlock(type="ref_text", index=3, content=[TextSpan(type="text", content="R2")], continues_prev=True),
                    text_block(4, "barrier"),
                    RefTextBlock(type="ref_text", index=5, content=[TextSpan(type="text", content="R3")], continues_prev=True),
                ],
            ),
        ],
        metadata={"file_suffix": "html", "producer": {"name": "docvortex", "version": "0.2.0"}},
        is_full_document=True,
    )
    before = middle.model_dump()
    default = build_render_plan(middle)
    full = build_render_plan(middle, RenderMode.FULL)
    assert not default[0][1].removed
    assert default[1][0].removed and not full[1][0].removed
    assert default[1][3].removed and full[1][3].removed
    assert not default[1][5].removed
    assert middle.model_dump() == before


def test_merged_lists_do_not_change_epub_identity_or_input() -> None:
    """List merging during internal reuse must not change the EPUB identifier calculated from the original document."""
    from datetime import datetime, timezone
    from docvortex.render import render, RenderFormat

    middle = MiddleJson(
        pages=[
            PageInfo(
                page_idx=i,
                blocks=[
                    ListBlock(type="list", index=0, sub_type="text", continues_prev=i > 0, content=[text_block(0, str(i))])
                ],
            )
            for i in range(3)
        ],
        metadata={"file_suffix": "html", "producer": {"name": "docvortex", "version": "0.2.0"}},
        is_full_document=True,
    )
    before = middle.model_dump()
    options = EpubRenderOptions(modified_at=datetime(2000, 1, 1, tzinfo=timezone.utc))
    expected = render(middle, RenderFormat.EPUB, options=options)
    assert api.render(middle, "epub", options=options).content == expected
    assert middle.model_dump() == before


def test_owned_context_is_consumed_and_recovers_after_exception() -> None:
    """Defensive replication is restored for both reentrancy planning and new calls after exceptions to the same document."""
    from docvortex.render._internal.common.context import owned_render_document

    middle = MiddleJson(
        pages=[PageInfo(page_idx=0, blocks=[text_block(0, "original")])],
        metadata={"file_suffix": "html", "producer": {"name": "docvortex", "version": "0.2.0"}},
        is_full_document=True,
    )
    with pytest.raises(RuntimeError), owned_render_document(middle):
        first = build_render_plan(middle)
        second = build_render_plan(middle)
        assert first[0][0].block is middle.pages[0].blocks[0]
        assert second[0][0].block is not middle.pages[0].blocks[0]
        raise RuntimeError("callback failure")
    assert build_render_plan(middle)[0][0].block is not middle.pages[0].blocks[0]


def test_inline_join_preserves_links_and_empty_leaves() -> None:
    """Character rules are used on a boundary-by-boundary basis, and semantic boundaries for whitespace, same-target links, and formulas can be reviewed."""
    from docvortex.content import inline
    from docvortex.schema import HyperlinkSpan, EquationInlineSpan

    contents = [
        [TextSpan(type="text", content="alpha ", styles=["bold"])],
        [HyperlinkSpan(type="hyperlink", url="https://example.com", content=[TextSpan(type="text", content=" beta ")])],
        [EquationInlineSpan(type="equation_inline", content="x^2"), TextSpan(type="text", content=" ")],
        [TextSpan(type="text", content="gamma")],
    ]
    before = deepcopy(contents)
    joined = join_inline_spans(contents)
    assert inline.inline_plain_text(joined) == "alpha beta x^2 gamma"
    assert contents == before


def test_callback_failure_and_repeated_exports_leave_source_untouched() -> None:
    """Modifications and failures of internal blocks by image callbacks are not leaked to the source document owned by the caller."""
    from docvortex.schema import ImageBlock, ImageBodyBlock

    body = ImageBodyBlock(type="image_body", content="", index=0, image_path="images/a.png")
    middle = MiddleJson(
        pages=[PageInfo(page_idx=0, blocks=[ImageBlock(type="image", index=0, content=[body])])],
        metadata={"file_suffix": "html", "producer": {"name": "docvortex", "version": "0.2.0"}},
        is_full_document=True,
    )
    before = middle.model_dump()

    def callback(block: object) -> str:
        """Fails after modifying the received copy, simulating a side-effecting host extension."""
        block.content.clear()
        raise RuntimeError("image failure")

    with pytest.raises(RuntimeError, match="image failure"):
        api.render(middle, "markdown", options=MarkdownRenderOptions(image_renderer=callback))
    assert middle.model_dump() == before
    assert api.render(middle, "markdown").content == api.render(middle, "markdown").content
