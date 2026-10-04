from __future__ import annotations

from copy import deepcopy
from typing import Literal

import pytest
from _span_test_utils import inline as _inline

from docvortex.options import LatexDelimitersConfig
from docvortex.render import RenderMode, render_markdown
from docvortex.schema import (
    AlgorithmBodyBlock,
    ChartBlock,
    ChartBodyBlock,
    CodeBlock,
    CodeBodyBlock,
    EquationBlock,
    ImageBlock,
    ImageBodyBlock,
    IndexBlock,
    ListBlock,
    MiddleJson,
    PageAuxTextBlock,
    PageBlock,
    PageFootnoteBlock,
    PageInfo,
    ParagraphTitleBlock,
    Producer,
    RefTextBlock,
    TableBlock,
    TableBodyBlock,
    TextBlock,
)


def _middle(*pages: PageInfo, file_suffix: str = "docx") -> MiddleJson:
    """Construct the minimally stringent MiddleJson test object."""
    return MiddleJson(
        pages=list(pages),
        is_full_document=True,
        metadata={"file_suffix": file_suffix, "producer": Producer(name="docvortex", version="test")},
        extensions={},
    )


def _page(page_idx: int, *blocks: PageBlock) -> PageInfo:
    """Constructs a page preserving the block order given by the caller."""
    return PageInfo(page_idx=page_idx, blocks=list(blocks))


def _image(index: int, path: str = "images/a.png") -> ImageBlock:
    """Constructs a picture parent block with path."""
    return ImageBlock(
        type="image",
        index=index,
        content=[ImageBodyBlock(type="image_body", index=index, content="", image_path=path)],
    )


def _table(index: int, content: str, *, continues_prev: bool | None = None) -> TableBlock:
    """Constructs the Office table parent block without bbox."""
    return TableBlock(
        type="table",
        index=index,
        continues_prev=continues_prev,
        content=[TableBodyBlock(type="table_body", index=index, content=content)],
    )


def _pdf_table(
    index: int,
    content: str,
    *,
    continues_prev: bool | None = None,
) -> TableBlock:
    """Constructs the PDF table parent block with normalized bbox."""
    bbox = (0.1, 0.1, 0.9, 0.9)
    return TableBlock(
        type="table",
        index=index,
        bbox=bbox,
        continues_prev=continues_prev,
        content=[TableBodyBlock(type="table_body", index=index, bbox=bbox, content=content)],
    )


def _list(
    index: int,
    *items: str | list[dict[str, object]],
    sub_type: Literal["text", "ref_text"] | None = None,
    continues_prev: bool | None = None,
) -> ListBlock:
    """Constructs a list parent block with normalized bbox and generates text leaves by subtype."""
    child_type = sub_type or "text"
    child_class = RefTextBlock if child_type == "ref_text" else TextBlock
    return ListBlock(
        type="list",
        index=index,
        bbox=(0.1, 0.1, 0.9, 0.3),
        sub_type=sub_type,
        continues_prev=continues_prev,
        content=[child_class(type=child_type, content=_inline(item) if isinstance(item, str) else item) for item in items],
    )


def _ref_text(index: int, content: str, *, continues_prev: bool | None = None) -> RefTextBlock:
    """Constructs a top-level reference text block that can carry continuation markers."""
    return RefTextBlock(
        type="ref_text",
        index=index,
        content=_inline(content),
        continues_prev=continues_prev,
    )


def test_render_modes_filter_merge_and_preserve_input() -> None:
    """Verify filtering, intra-page/cross-page merging, page breaks and no side effects in both modes."""
    middle = _middle(
        _page(
            0,
            PageAuxTextBlock(type="header", index=0, content=_inline("HEADER")),
            TextBlock(type="text", index=1, content=_inline("Hello")),
            _image(2),
            TextBlock(type="text", index=3, content=_inline("world"), continues_prev=True),
            PageAuxTextBlock(type="footer", index=4, content=_inline("FOOTER")),
        ),
        _page(
            1,
            PageAuxTextBlock(type="page_number", index=0, content=_inline("2")),
            TextBlock(type="text", index=1, content=_inline("again"), continues_prev=True),
            PageFootnoteBlock(type="page_footnote", index=2, content=_inline("NOTE")),
        ),
    )
    before = middle.to_json(skip_defaults=False)

    default = render_markdown(middle)
    full = render_markdown(middle, mode=RenderMode.FULL)

    note = (
        '<small><span class="docvortex-page-footnote" data-block-type="page_footnote" style="color:#6b7280">NOTE</span></small>'
    )
    assert default == f"Hello world again\n\n![](images/a.png)\n\n{note}"
    assert full == "\n\n---\n\n".join(
        [
            "HEADER\n\nHello world\n\n![](images/a.png)\n\nFOOTER",
            f"2\n\nagain\n\n{note}",
        ]
    )
    assert middle.to_json(skip_defaults=False) == before


def test_page_footnote_is_styled_and_linkable_in_default_and_full_modes() -> None:
    """Verify that both default and full modes output non-folded small page footnotes and anchor."""
    middle = _middle(
        _page(
            0,
            TextBlock(
                type="text",
                index=0,
                content=[
                    {"type": "text", "content": "See "},
                    {"type": "hyperlink", "url": "#note-one", "content": _inline("[1]")},
                    {"type": "text", "content": "."},
                ],
            ),
            PageFootnoteBlock(
                type="page_footnote",
                index=1,
                content=_inline("Footnote body."),
                anchor="note-one",
            ),
        )
    )

    expected = (
        'See [\\[1\\]](#note-one).\n\n<small><span id="note-one" class="docvortex-page-footnote" '
        'data-block-type="page_footnote" style="color:#6b7280">Footnote body.</span></small>'
    )
    assert render_markdown(middle) == expected
    assert render_markdown(middle, mode=RenderMode.FULL) == expected
    assert "Page footnote:" not in expected


def test_page_footnote_does_not_interrupt_continued_text_rendering() -> None:
    """The footer of the verification page remains output independently, but does not block the continuation of the previous and subsequent text."""
    middle = _middle(
        _page(
            0,
            TextBlock(type="text", index=0, content=_inline("inter-")),
            PageFootnoteBlock(type="page_footnote", index=1, content=_inline("NOTE")),
            TextBlock(type="text", index=2, content=_inline("national"), continues_prev=True),
        )
    )

    assert render_markdown(middle) == (
        'international\n\n<small><span class="docvortex-page-footnote" '
        'data-block-type="page_footnote" style="color:#6b7280">NOTE</span></small>'
    )


def test_full_mode_preserves_empty_page_boundaries() -> None:
    """Verify that FULL retains adjacent page separators for blank pages."""
    middle = _middle(_page(0), _page(1, TextBlock(type="text", index=0, content=_inline("x"))), _page(2))

    assert render_markdown(middle, mode=RenderMode.FULL) == "\n\n---\n\nx\n\n---\n\n"
    assert render_markdown(middle) == "x"


def test_text_continuation_handles_hyphen_and_cjk_boundaries() -> None:
    """Verify that the continuation text follows the Western segmentation and CJK direct connection rules."""
    western = _middle(
        _page(
            0,
            TextBlock(type="text", index=0, content=_inline("inter-")),
            TextBlock(type="text", index=1, content=_inline("national"), continues_prev=True),
        )
    )
    cjk = _middle(
        _page(
            0,
            TextBlock(type="text", index=0, content=_inline("中文")),
            TextBlock(type="text", index=1, content=_inline("继续"), continues_prev=True),
        )
    )

    assert render_markdown(western) == "international"
    assert render_markdown(cjk) == "中文继续"


def test_ref_text_continuation_skips_merge_transparent_blocks_by_mode() -> None:
    """Verify that ref_text can search across page footers and auxiliary blocks, and FULL still preserves page boundaries."""
    middle = _middle(
        _page(
            0,
            _ref_text(0, "inter-"),
            PageFootnoteBlock(type="page_footnote", index=1, content=_inline("NOTE")),
        ),
        _page(
            1,
            PageAuxTextBlock(type="header", index=0, content=_inline("HEADER")),
            _ref_text(1, "national", continues_prev=True),
            _ref_text(2, "continuation", continues_prev=True),
        ),
    )
    original = deepcopy(middle)

    note = (
        '<small><span class="docvortex-page-footnote" data-block-type="page_footnote" style="color:#6b7280">NOTE</span></small>'
    )
    assert render_markdown(middle) == f"international continuation\n\n{note}"
    assert render_markdown(middle, mode=RenderMode.FULL) == (f"inter-\n\n{note}\n\n---\n\nHEADER\n\nnational continuation")
    assert middle == original


def test_ref_text_continuation_keeps_semantic_barrier() -> None:
    """Validating manual tagging also does not allow ref_text to cross semantic blocks such as body text."""
    middle = _middle(
        _page(
            0,
            _ref_text(0, "first"),
            TextBlock(type="text", index=1, content=_inline("separator")),
            _ref_text(2, "second", continues_prev=True),
        )
    )

    assert render_markdown(middle) == "first\n\nseparator\n\nsecond"


def test_ref_text_continuation_reuses_url_boundary_joining() -> None:
    """Verify cross-block URL join rules for ref_text continuation multiplex text."""
    middle = _middle(
        _page(
            0,
            _ref_text(0, "See https://doi.o"),
            _ref_text(1, "rg/10.1016/example", continues_prev=True),
        )
    )

    assert render_markdown(middle) == "See https://doi.org/10.1016/example"


def test_list_continuation_merges_same_page_in_both_modes_without_mutating_input() -> None:
    """Verify that the same subtype list on the same page is spliced in both modes without contaminating the original MiddleJson."""
    middle = _middle(
        _page(
            0,
            _list(0, "- first"),
            _list(1, "- second", continues_prev=True),
        ),
        file_suffix="pdf",
    )
    original = deepcopy(middle)

    assert render_markdown(middle) == "- first\n- second"
    assert render_markdown(middle, mode=RenderMode.FULL) == "- first\n- second"
    assert middle == original


def test_list_continuation_merges_cross_page_chain_only_in_default_mode() -> None:
    """Verify that cross-page list chains are only spliced as a whole in the default mode, and the complete mode retains page boundaries and merges continuations within the page."""
    middle = _middle(
        _page(0, _list(0, "[1] first", sub_type="ref_text")),
        _page(
            1,
            _list(0, "[2] second", sub_type="ref_text", continues_prev=True),
            _list(1, "[3] third", sub_type="ref_text", continues_prev=True),
        ),
        file_suffix="pdf",
    )

    assert render_markdown(middle) == "[1] first\n[2] second\n[3] third"
    assert render_markdown(middle, mode=RenderMode.FULL) == ("[1] first\n\n---\n\n[2] second\n[3] third")


def test_ref_list_continuation_skips_merge_transparent_blocks_without_mutating_input() -> None:
    """Verify that default mode cross-page footer merges references with auxiliary blocks, FULL still preserves page boundaries."""
    middle = _middle(
        _page(
            0,
            _list(0, "[1] first", sub_type="ref_text"),
            PageFootnoteBlock(type="page_footnote", index=1, content=_inline("NOTE")),
        ),
        _page(
            1,
            PageAuxTextBlock(type="header", index=0, content=_inline("HEADER")),
            PageAuxTextBlock(type="page_number", index=1, content=_inline("2")),
            _list(2, "[2] second", sub_type="ref_text", continues_prev=True),
        ),
    )
    original = deepcopy(middle)

    note = (
        '<small><span class="docvortex-page-footnote" data-block-type="page_footnote" style="color:#6b7280">NOTE</span></small>'
    )
    assert render_markdown(middle) == f"[1] first\n[2] second\n\n{note}"
    assert render_markdown(middle, mode=RenderMode.FULL) == (f"[1] first\n\n{note}\n\n---\n\nHEADER\n\n2\n\n[2] second")
    assert middle == original


def test_ordinary_list_continuation_does_not_skip_page_footnote() -> None:
    """Validation page footer transparency rules only apply to references, normal lists still require physical proximity."""
    middle = _middle(
        _page(
            0,
            _list(0, "- first"),
            PageFootnoteBlock(type="page_footnote", index=1, content=_inline("NOTE")),
        ),
        _page(
            1,
            PageAuxTextBlock(type="header", index=0, content=_inline("HEADER")),
            _list(1, "- second", continues_prev=True),
        ),
    )

    assert render_markdown(middle) == (
        '- first\n\n<small><span class="docvortex-page-footnote" data-block-type="page_footnote" '
        'style="color:#6b7280">NOTE</span></small>\n\n- second'
    )


def test_list_continuation_keeps_semantic_barrier_and_matching_subtype() -> None:
    """Validation semantic blocks will still block list continuation and maintain independent output when subtypes are inconsistent."""
    non_adjacent = _middle(
        _page(
            0,
            _list(0, "[1] first", sub_type="ref_text"),
            TextBlock(type="text", index=1, bbox=(0.1, 0.4, 0.9, 0.5), content=_inline("separator")),
            _list(2, "[2] second", sub_type="ref_text", continues_prev=True),
        ),
        file_suffix="pdf",
    )
    mismatched = _middle(
        _page(
            0,
            _list(0, "[1] first", sub_type="ref_text"),
            _list(1, "- second", sub_type="text", continues_prev=True),
        ),
        file_suffix="pdf",
    )

    assert render_markdown(non_adjacent) == "[1] first\n\nseparator\n\n[2] second"
    assert render_markdown(mismatched) == "[1] first\n\n- second"


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        ("[1] bracket", "[1] bracket"),
        ("1. dot", "1. dot"),
        ("(1) parenthesized", "(1) parenthesized"),
        ("1) closing parenthesis", "1) closing parenthesis"),
        ("1、cjk delimiter", "1、cjk delimiter"),
        ("［１］full width", "［１］full width"),
        ('<text style="bold">[1]</text> styled', "**[1]** styled"),
    ],
)
def test_reference_list_keeps_supported_numeric_prefix_styles(item: str, expected: str) -> None:
    """When the verification number appears within the first five visible characters, a single reference retains its original number."""
    content = (
        [
            {"type": "text", "content": "[1]", "styles": ["bold"]},
            {"type": "text", "content": " styled"},
        ]
        if item.startswith("<text")
        else _inline(item)
    )
    middle = _middle(_page(0, _list(0, content, sub_type="ref_text")), file_suffix="pdf")

    assert render_markdown(middle) == expected


def test_reference_list_uses_strict_numeric_prefix_majority() -> None:
    """Verification references are determined by a strict majority of all direct non-null entries to determine whether to fill out the out-of-order mark."""
    numbered_majority = _middle(
        _page(0, _list(0, "[1] first", "missing marker", "3) third", sub_type="ref_text")),
        file_suffix="pdf",
    )
    unordered_majority = _middle(
        _page(0, _list(0, "1470–1480 continuation", "Author A", "Author B", sub_type="ref_text")),
        file_suffix="pdf",
    )
    tied = _middle(
        _page(0, _list(0, "[1] first", "Author A", sub_type="ref_text")),
        file_suffix="pdf",
    )

    assert render_markdown(numbered_majority) == "[1] first\nmissing marker\n3) third"
    expected_unordered = "- 1470–1480 continuation\n- Author A\n- Author B"
    assert render_markdown(unordered_majority) == expected_unordered
    assert render_markdown(unordered_majority, mode=RenderMode.FULL) == expected_unordered
    assert render_markdown(tied) == "- [1] first\n- Author A"


def test_reference_list_bullets_mixed_children_without_duplication() -> None:
    """Verify that mixed direct types participate in statistics, existing dashes are not repeated, and multi-line text is correctly indented."""
    block = ListBlock(
        type="list",
        index=0,
        bbox=(0.1, 0.1, 0.9, 0.3),
        sub_type="ref_text",
        content=[
            TextBlock(type="text", content=_inline("- existing")),
            RefTextBlock(type="ref_text", content=_inline("Author A\ncontinued")),
        ],
    )

    assert render_markdown(_middle(_page(0, block), file_suffix="pdf")) == ("- existing\n- Author A\n  continued")


def test_nested_reference_list_decides_bullets_independently() -> None:
    """Validation of nested references is based on their own subtypes, and the behavior of the outer normal list remains unchanged."""
    nested = ListBlock(
        type="list",
        sub_type="ref_text",
        content=[RefTextBlock(type="ref_text", content=_inline("Author A"))],
    )
    outer = ListBlock(
        type="list",
        index=0,
        bbox=(0.1, 0.1, 0.9, 0.3),
        sub_type="text",
        content=[TextBlock(type="text", content=_inline("- outer")), nested],
    )

    assert render_markdown(_middle(_page(0, outer), file_suffix="pdf")) == "- outer\n    - Author A"


def test_text_continuation_joins_url_candidates_but_separates_independent_urls() -> None:
    """Verify that Markdown rewrites the connection across block URL while preserving the separation of two independent URLs."""
    continued_url = _middle(
        _page(
            0,
            TextBlock(type="text", index=0, content=_inline("See https://doi.o")),
            TextBlock(type="text", index=1, content=_inline("rg/10.1016/example"), continues_prev=True),
        )
    )
    independent_urls = _middle(
        _page(
            0,
            TextBlock(type="text", index=0, content=_inline("https://example.test/first")),
            TextBlock(type="text", index=1, content=_inline("https://example.test/second"), continues_prev=True),
        )
    )

    assert render_markdown(continued_url) == "See https://doi.org/10.1016/example"
    assert render_markdown(independent_urls) == "https://example.test/first https://example.test/second"


def test_text_continuation_does_not_rewrite_formula_or_style_wrappers() -> None:
    """Verify that continuation boundaries do not cross the last formula or break style nodes."""
    formula = _middle(
        _page(
            0,
            TextBlock(
                type="text",
                index=0,
                content=[{"type": "text", "content": "before "}, {"type": "equation_inline", "content": "x"}],
            ),
            TextBlock(type="text", index=1, content=_inline("after"), continues_prev=True),
        )
    )
    styled = _middle(
        _page(
            0,
            TextBlock(type="text", index=0, content=_inline("inter-", styles=["bold"])),
            TextBlock(type="text", index=1, content=_inline("national"), continues_prev=True),
        )
    )

    assert render_markdown(formula) == "before $x$ after"
    assert render_markdown(styled) == "**inter**national"


def test_render_rejects_legacy_inputs_and_string_mode() -> None:
    """Verify that the public entry is not compatible with old dict/pages input or string modes."""
    middle = _middle(_page(0))

    with pytest.raises(TypeError, match="MiddleJson"):
        render_markdown(middle.to_dict())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="RenderMode"):
        render_markdown(middle, mode="full")  # type: ignore[arg-type]


def test_inline_rich_text_unknown_tags_and_visible_spaces() -> None:
    """Validate rich text, formulas, links, unknown tags and visible whitespace."""
    content = [
        {"type": "text", "content": "A "},
        {"type": "text", "content": "B", "styles": ["bold"]},
        {"type": "text", "content": " "},
        {
            "type": "hyperlink",
            "url": "https://example.com/a b",
            "content": [{"type": "text", "content": "link", "styles": ["underline"]}],
        },
        {"type": "text", "content": " "},
        {"type": "equation_inline", "content": "x_1"},
        {"type": "text", "content": " <local_dir> p <0.05 and x > 0 "},
        {"type": "text", "content": "2", "styles": ["superscript"]},
        {"type": "text", "content": "  ", "styles": ["underline"]},
    ]
    middle = _middle(_page(0, TextBlock(type="text", index=0, content=content)))

    rendered = render_markdown(middle)

    assert "**B**" in rendered
    assert '<a href="https://example.com/a b"><u>link</u></a>' in rendered
    assert "$x_1$" in rendered
    assert "&lt;local_dir&gt;" in rendered
    assert "p <0.05 and x > 0" in rendered
    assert "<sup>2</sup>" in rendered
    assert rendered.endswith("<sup>2</sup>__")


@pytest.mark.parametrize(
    ("style", "expected"),
    [
        ("underline", "A___B"),
        ("strikethrough", "A---B"),
        ("underline,strikethrough", "A<s>___</s>B"),
        ("bold,underline", "A<strong>___</strong>B"),
    ],
)
def test_visible_style_ascii_spaces_use_dev_markers(style: str, expected: str) -> None:
    """Verify that plain ASCII style spaces use dev for underscores or dashes for marker."""
    content = [
        {"type": "text", "content": "A"},
        {"type": "text", "content": "   ", "styles": style.split(",")},
        {"type": "text", "content": "B"},
    ]

    assert render_markdown(_middle(_page(0, TextBlock(type="text", index=0, content=content)))) == expected


@pytest.mark.parametrize(
    ("style", "expected"),
    [
        ("underline", "<u>___广东__</u>"),
        ("strikethrough", "~~---广东--~~"),
        ("underline,strikethrough", "<s><u>___广东__</u></s>"),
    ],
)
def test_visible_style_edge_spaces_use_dev_markers(style: str, expected: str) -> None:
    """Verify that non-empty style text only replaces leading and trailing ASCII spaces."""
    content = _inline("   广东  ", styles=style.split(","))

    assert render_markdown(_middle(_page(0, TextBlock(type="text", index=0, content=content)))) == expected


@pytest.mark.parametrize(
    ("style", "expected"),
    [
        ("underline", r"\___"),
        ("strikethrough", r"\---"),
    ],
)
def test_standalone_visible_space_markers_are_escaped(style: str, expected: str) -> None:
    """Verify that the entire marker will escape the first character to avoid being treated as a Markdown dividing line."""
    content = _inline("   ", styles=[style])

    assert render_markdown(_middle(_page(0, TextBlock(type="text", index=0, content=content)))) == expected


def test_emphasis_only_spaces_keep_existing_html_behavior() -> None:
    """Verify that emphasis-only spaces do not enter the underline/strikethrough marker rule."""
    content = _inline("  ", styles=["emphasis"])

    rendered = render_markdown(_middle(_page(0, TextBlock(type="text", index=0, content=content))))

    assert "&nbsp;&nbsp;" in rendered


def test_text_block_escapes_markdown_prefix_and_malformed_tag() -> None:
    """Verify that ordinary text does not erroneously change the list, and the damaged whitelist tags are escaped according to the original text."""
    middle = _middle(
        _page(
            0,
            TextBlock(type="text", index=0, content=_inline("- plain")),
            TextBlock(type="text", index=1, content=_inline('<text style="bold">broken')),
        )
    )

    assert render_markdown(middle) == '\\- plain\n\n&lt;text style="bold"&gt;broken'


def test_title_and_index_render_anchor_links_without_heading_leaves() -> None:
    """Verify link output of header anchor with recursive directory title leaf."""
    index = IndexBlock(
        type="index",
        index=0,
        content=[
            ParagraphTitleBlock(
                type="paragraph_title",
                level=2,
                anchor="toc-a",
                content=_inline("1 Section\t12"),
            ),
            IndexBlock(
                type="index",
                content=[TextBlock(type="text", content=_inline("Plain"))],
            ),
        ],
    )
    title = ParagraphTitleBlock(
        type="paragraph_title",
        index=1,
        level=6,
        anchor="toc-a",
        content=_inline("Section"),
    )

    rendered = render_markdown(_middle(_page(0, index, title)))

    assert rendered.startswith("- [1 Section](#toc-a)\n    - Plain")
    assert '<a id="toc-a"></a>\n###### Section' in rendered


def test_equation_uses_content_then_image_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify the configuration of interline formula delimiters and fallback of empty formula images."""
    configured = LatexDelimitersConfig(
        **{"display": {"left": "\\[", "right": "\\]"}, "inline": {"left": "\\(", "right": "\\)"}}
    )
    middle = _middle(
        _page(
            0,
            EquationBlock(type="equation", index=0, content="x=1"),
            EquationBlock(type="equation", index=1, content="", image_path="images/e.png"),
            TextBlock(
                type="text",
                index=2,
                content=[{"type": "text", "content": "inline "}, {"type": "equation_inline", "content": "y"}],
            ),
        )
    )

    assert render_markdown(middle, latex_delimiters=configured) == "\\[\nx=1\n\\]\n\n![](images/e.png)\n\ninline \\(y\\)"


@pytest.mark.parametrize(
    "html_table",
    [
        "<table><tr><td rowspan='2'>A</td></tr><tr><td>B</td></tr></table>",
        "<table><tr><td colspan='2'>A</td></tr></table>",
        "<table><tr><td><table><tr><td>N</td></tr></table></td></tr></table>",
        "<table><tr><td><img src='images/a.png'></td></tr></table>",
        "<table><tr><td><ul><li>A</li></ul></td></tr></table>",
        "<table><tr><td><p>A</p><p>B</p></td></tr></table>",
    ],
)
def test_complex_html_tables_fall_back_to_html(html_table: str) -> None:
    """Verification that non-lossless convertible table structure remains HTML."""
    rendered = render_markdown(_middle(_page(0, _table(0, html_table))), asset_base_url="assets")

    assert rendered.startswith("<table")
    assert "| --- |" not in rendered
    if "<img" in html_table:
        assert "assets/images/a.png" in rendered


def test_simple_html_table_converts_to_gfm_and_preserves_inline_formula() -> None:
    """Verification is simple HTML table to GFM, and cell formula backslashes are preserved."""
    content = "<table><tr><th>Name</th><th>Value</th></tr><tr><td>A|B</td><td><eq>\\frac{1}{2}</eq></td></tr></table>"

    assert render_markdown(_middle(_page(0, _table(0, content)))) == "\n".join(
        [
            "| Name | Value |",
            "| --- | --- |",
            r"| A\|B | $\frac{1}{2}$ |",
        ]
    )


def test_spatial_table_uses_dynamic_fence_and_empty_table_uses_image() -> None:
    """Verify that spatially projected text remains blank, and empty tables fall back to images."""
    spatial = _table(0, "A   B\n```\n1   2")
    empty = TableBlock(
        type="table",
        index=1,
        content=[TableBodyBlock(type="table_body", index=1, content="", image_path="images/t.png")],
    )

    rendered = render_markdown(_middle(_page(0, spatial, empty)))

    assert rendered.startswith("````\nA   B\n```\n1   2\n````")
    assert rendered.endswith("![](images/t.png)")


def test_cross_page_table_merges_only_in_default_mode() -> None:
    """Verify that the continuation table is only merged across pages at DEFAULT and remains paged at FULL."""
    previous_html = "<table><tr><th>H</th></tr><tr><td>A</td></tr></table>"
    current_html = "<table><tr><th>H</th></tr><tr><td>B</td></tr></table>"
    middle = _middle(
        _page(0, _pdf_table(0, previous_html)),
        _page(1, _pdf_table(0, current_html, continues_prev=True)),
        file_suffix="pdf",
    )
    original = deepcopy(middle)

    default = render_markdown(middle)
    full = render_markdown(middle, mode=RenderMode.FULL)

    assert default == "\n".join(["| H |", "| --- |", "| A |", "| B |"])
    assert full.count("| --- |") == 2
    assert "\n\n---\n\n" in full
    assert middle == original


def test_code_uses_language_and_dynamic_fence() -> None:
    """Verify that normal code uses guess_lang and fenced of sufficient length to block."""
    code = CodeBlock(
        type="code",
        index=0,
        sub_type="code",
        guess_lang="python",
        content=[CodeBodyBlock(type="code_body", index=0, content="print('x')\n```")],
    )
    invalid_language = CodeBlock(
        type="code",
        index=1,
        sub_type="code",
        guess_lang="python bad",
        content=[CodeBodyBlock(type="code_body", index=1, content="x")],
    )

    rendered = render_markdown(_middle(_page(0, code, invalid_language)))

    assert rendered.startswith("````python\nprint('x')\n```\n````")
    assert rendered.endswith("```txt\nx\n```")


def test_algorithm_preserves_whitespace_comparisons_scripts_and_formula() -> None:
    """Verification algorithm raw HTML uses HTML inline syntax and preserves indentation and adjacent formulas."""
    algorithm = CodeBlock(
        type="code",
        index=0,
        sub_type="algorithm",
        content=[
            AlgorithmBodyBlock(
                type="algorithm_body",
                index=0,
                content=[
                    {"type": "text", "content": "if a < b and c * d:\n  "},
                    {"type": "text", "content": "bold", "styles": ["bold"]},
                    {"type": "text", "content": " / "},
                    {"type": "text", "content": "italic", "styles": ["italic"]},
                    {"type": "text", "content": " / "},
                    {"type": "text", "content": "strike", "styles": ["strikethrough"]},
                    {"type": "text", "content": " / "},
                    {"type": "text", "content": "under", "styles": ["underline"]},
                    {"type": "text", "content": " / "},
                    {"type": "text", "content": "dot", "styles": ["emphasis"]},
                    {"type": "text", "content": " "},
                    {"type": "text", "content": "q", "styles": ["subscript"]},
                    {"type": "text", "content": "2", "styles": ["superscript"]},
                    {"type": "text", "content": " "},
                    {"type": "code_inline", "content": "x * y"},
                    {"type": "text", "content": " "},
                    {
                        "type": "hyperlink",
                        "url": "https://example.com/a b",
                        "content": [{"type": "text", "content": "link", "styles": ["bold"]}],
                    },
                    {"type": "text", "content": " = "},
                    {"type": "equation_inline", "content": "a < b"},
                    {"type": "equation_inline", "content": "c > d"},
                ],
            )
        ],
    )

    rendered = render_markdown(_middle(_page(0, algorithm)))

    assert 'class="docvortex-algorithm"' in rendered
    assert (
        "if a &lt; b and c * d:\n  <strong>bold</strong> / <em>italic</em> / <s>strike</s> / "
        '<u>under</u> / <span style="text-emphasis: dot; text-emphasis-position: under;">dot</span> '
        '<sub>q</sub><sup>2</sup> <code>x * y</code> <a href="https://example.com/a b"><strong>link</strong></a> '
        "= $a &lt; b$ $c &gt; d$"
    ) in rendered
    assert "**bold**" not in rendered
    assert "*italic*" not in rendered
    assert "~~strike~~" not in rendered
    assert r"\*" not in rendered


def test_image_path_precedes_base64_and_visual_child_order_is_preserved() -> None:
    """Verify picture resource priority, base URL, and visual subchunk original order."""
    image = ImageBlock.model_validate(
        {
            "type": "image",
            "index": 0,
            "content": [
                {"type": "image_caption", "content": _inline("before")},
                {
                    "type": "image_body",
                    "index": 0,
                    "content": "description",
                    "image_path": "images/a b.png",
                    "image_base64": "data:image/png;base64,AAAA",
                },
                {"type": "image_footnote", "content": _inline("after")},
            ],
        }
    )

    rendered = render_markdown(_middle(_page(0, image)), asset_base_url="https://cdn.example/doc")

    assert rendered.index("before") < rendered.index("https://cdn.example/doc/images/a%20b.png")
    assert rendered.index("description") < rendered.index("after")
    assert "data:image" not in rendered


def test_chart_without_image_outputs_existing_gfm_content() -> None:
    """Verify that the direct output of charts without pictures already contains GFM content."""
    chart = ChartBlock(
        type="chart",
        index=0,
        content=[ChartBodyBlock(type="chart_body", index=0, content="| A |\n| --- |\n| 1 |")],
    )

    assert render_markdown(_middle(_page(0, chart))) == "| A |\n| --- |\n| 1 |"


def test_chart_without_image_converts_simple_html_table_to_gfm() -> None:
    """Verification without image chart Simple HTML table directly converted to GFM."""
    chart = ChartBlock(
        type="chart",
        index=0,
        content=[
            ChartBodyBlock(
                type="chart_body",
                index=0,
                content="<table><tr><th>A</th></tr><tr><td>1</td></tr></table>",
            )
        ],
    )

    assert render_markdown(_middle(_page(0, chart))) == "| A |\n| --- |\n| 1 |"


def test_chart_with_image_places_converted_gfm_in_details() -> None:
    """Verify that there is picture chart. Keep the picture and put the simple form GFM into details."""
    chart = ChartBlock(
        type="chart",
        index=0,
        sub_type="line chart",
        content=[
            ChartBodyBlock(
                type="chart_body",
                index=0,
                content="<table><tr><th>A</th></tr><tr><td>1</td></tr></table>",
                image_path="images/chart.png",
            )
        ],
    )

    rendered = render_markdown(_middle(_page(0, chart)))

    assert rendered.startswith("![](images/chart.png)\n\n<details>")
    assert "| A |\n| --- |\n| 1 |" in rendered
    assert "<table>" not in rendered


def test_chart_complex_html_table_remains_html() -> None:
    """Verify that the complex HTML of chart table will not be lossy converted to GFM."""
    chart = ChartBlock(
        type="chart",
        index=0,
        content=[
            ChartBodyBlock(
                type="chart_body",
                index=0,
                content='<table><tr><td colspan="2">A</td></tr></table>',
            )
        ],
    )

    rendered = render_markdown(_middle(_page(0, chart)))

    assert rendered.startswith("<table>")
    assert 'colspan="2"' in rendered


def test_direct_base64_image_fallback() -> None:
    """When verifying that there is no external image, Markdown directly uses data and URI."""
    image = ImageBlock(
        type="image",
        index=0,
        content=[
            ImageBodyBlock(
                type="image_body",
                index=0,
                content="",
                image_base64="data:image/png;base64,AAAA",
            )
        ],
    )

    assert render_markdown(_middle(_page(0, image))) == "![](data:image/png;base64,AAAA)"
