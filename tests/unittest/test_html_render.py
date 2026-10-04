from __future__ import annotations

from copy import deepcopy
from importlib import resources

import pytest
from _span_test_utils import inline as _inline
from bs4 import BeautifulSoup

from docvortex.render import RenderMode, render_html
from docvortex.schema import (
    AlgorithmBodyBlock,
    ChartAnnotationBlock,
    ChartBlock,
    ChartBodyBlock,
    CodeAnnotationBlock,
    CodeBlock,
    CodeBodyBlock,
    DocTitleBlock,
    EquationBlock,
    ImageBlock,
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
    TextSpan,
)

_PNG_URI = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2l9sAAAAASUVORK5CYII="


def _middle(*pages: PageInfo) -> MiddleJson:
    """Construction does not require the strictness of PDF bbox Office MiddleJson."""
    return MiddleJson(
        pages=list(pages),
        is_full_document=True,
        metadata={"file_suffix": "docx", "producer": Producer(name="docvortex", version="test")},
        extensions={},
    )


def _page(page_idx: int, *blocks: PageBlock) -> PageInfo:
    """Construct test pages in caller order."""
    return PageInfo(page_idx=page_idx, blocks=list(blocks))


def _list(index: int, *items: str, sub_type: str | None = None) -> ListBlock:
    """Construct a general or reference list."""
    child_class = RefTextBlock if sub_type == "ref_text" else TextBlock
    return ListBlock(
        type="list",
        index=index,
        sub_type=sub_type,
        content=[child_class(type=sub_type or "text", content=_inline(item)) for item in items],
    )


def _mermaid_fence(source: str, *, language: str = "mermaid") -> str:
    """Construct diagram fence with standard triple backticks to avoid test strings littering with fence details."""
    fence = chr(96) * 3
    return f"{fence}{language}\n{source}\n{fence}"


def _flowchart(content: str, *, with_raster: bool = True) -> ImageBlock:
    """Constructs a flowchart picture block with optional raster fallback."""
    body: dict[str, object] = {
        "type": "image_body",
        "index": 0,
        "content": content,
    }
    if with_raster:
        body["image_base64"] = _PNG_URI
    return ImageBlock.model_validate(
        {
            "type": "image",
            "index": 0,
            "sub_type": "flowchart",
            "content": [
                body,
                {"type": "image_caption", "index": 1, "content": _inline("Flowchart caption")},
            ],
        }
    )


def test_public_contract_fragment_standalone_title_and_input_immutability() -> None:
    """Verify strict public parameters, dual output morphology, title fallback, and input without side effects."""
    middle = _middle(
        _page(
            0,
            DocTitleBlock(
                type="doc_title",
                index=0,
                level=1,
                content=[
                    {"type": "text", "content": "Demo", "styles": ["bold"]},
                    {"type": "text", "content": " <unsafe>"},
                ],
            ),
            TextBlock(type="text", index=1, content=_inline("body")),
        )
    )
    original = deepcopy(middle)

    fragment = render_html(middle, standalone=False)
    standalone = render_html(middle)

    assert fragment.startswith('<article class="docvortex-document docvortex-document--default" ')
    assert 'data-docvortex-html-version="1" data-render-mode="default"' in fragment
    assert fragment in standalone
    assert '<html lang="und">' in standalone
    assert "<title>Demo &lt;unsafe&gt;</title>" in standalone
    assert '<body class="docvortex-html-body">' in standalone
    assert "<style>" in standalone
    assert "mathjax@" not in standalone
    assert "prismjs@" not in standalone
    assert middle == original

    explicit = render_html(middle, document_title="A </title><script>x</script>")
    assert "<title>A &lt;/title&gt;&lt;script&gt;x&lt;/script&gt;</title>" in explicit

    with pytest.raises(TypeError, match="MiddleJson"):
        render_html({})  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="RenderMode"):
        render_html(middle, mode="default")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="standalone"):
        render_html(middle, standalone=1)  # type: ignore[arg-type]


def test_default_and_full_modes_preserve_their_page_contracts() -> None:
    """Verify DEFAULT continuous reading with FULL empty pages, auxiliary blocks, and page delimiters."""
    middle = _middle(
        _page(
            0,
            PageAuxTextBlock(type="header", index=0, content=_inline("HEADER")),
            TextBlock(type="text", index=1, content=_inline("inter-")),
        ),
        _page(5),
        _page(
            9,
            TextBlock(type="text", index=0, content=_inline("national"), continues_prev=True),
            PageAuxTextBlock(type="footer", index=1, content=_inline("FOOTER")),
        ),
    )

    default = BeautifulSoup(render_html(middle, standalone=False), "html.parser")
    full = BeautifulSoup(render_html(middle, mode=RenderMode.FULL, standalone=False), "html.parser")

    assert default.select_one(".docvortex-text").get_text() == "international"
    assert default.select_one('[data-block-type="text"]')["data-page-idx"] == "0"
    assert not default.select(".docvortex-page")
    assert "HEADER" not in default.get_text()

    assert [section["data-page-idx"] for section in full.select(".docvortex-page")] == ["0", "5", "9"]
    assert len(full.select(".docvortex-page-break")) == 2
    assert [item.get_text() for item in full.select(".docvortex-text")] == ["inter-", "national"]
    assert "HEADER" in full.get_text() and "FOOTER" in full.get_text()


def test_default_and_full_html_link_to_visible_page_footnote_anchor() -> None:
    """Verify that both default and full HTML output page footer targets and weakened style classes."""
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

    default = BeautifulSoup(render_html(middle, standalone=False), "html.parser")
    full = BeautifulSoup(render_html(middle, mode=RenderMode.FULL, standalone=False), "html.parser")

    assert default.select_one('a[href="#note-one"]') is not None
    for output in (default, full):
        target = output.select_one("#note-one.docvortex-page-footnote")
        assert target is not None
        assert target["data-block-type"] == "page_footnote"
        assert target.get_text() == "Footnote body."
    assert full.select_one('a[href="#note-one"]') is not None


def test_inline_html_escapes_plain_text_and_renders_styles_links_and_math() -> None:
    """Validates plain angle brackets, rich styles, safe links, and MathJax carrier."""
    content = [
        {"type": "text", "content": "p <0.05 <local_dir> "},
        {"type": "text", "content": " styled ", "styles": ["bold", "italic", "underline"]},
        {"type": "text", "content": " "},
        {"type": "hyperlink", "url": "https://example.test/a b", "content": _inline("safe")},
        {"type": "text", "content": " bad javascript:alert(1) "},
        {"type": "equation_inline", "content": "x < y & z"},
    ]
    result = render_html(_middle(_page(0, TextBlock(type="text", index=0, content=content))))
    soup = BeautifulSoup(result, "html.parser")

    paragraph = soup.select_one(".docvortex-text")
    assert "p <0.05 <local_dir>" in paragraph.get_text()
    assert paragraph.select_one("em strong u") is not None
    assert paragraph.select_one(".docvortex-preserve-whitespace") is not None
    assert paragraph.select_one('a[href="https://example.test/a%20b"]') is not None
    assert "bad" in paragraph.get_text()
    assert paragraph.select_one('a[href^="javascript:"]') is None
    assert paragraph.select_one(".docvortex-math").get_text() == r"\(x < y & z\)"
    assert "mathjax@4.1.2/tex-chtml.js" in result
    assert "loader: {load: ['ui/safe']}" in result
    assert "ignoreHtmlClass: 'docvortex-document'" in result
    assert "packages: {'[-]': ['require']}" in result


def test_plain_text_autolinks_mpe_style_urls_domains_and_email() -> None:
    """Verify that explicit URL, www, naked domain names, email addresses, brackets, and CJK sentence reads are converted into links in MPE style."""
    content = (
        "See https://example.com/a_(b), www.example.org/path?x=1, "
        "example.net/docs。Email user+tag@example.co.uk；"
        "local http://127.0.0.1:8080/a. "
        "pair https://one.example/ahttps://two.example/b"
    )
    soup = BeautifulSoup(
        render_html(_middle(_page(0, TextBlock(type="text", index=0, content=_inline(content)))), standalone=False),
        "html.parser",
    )

    links = soup.select(".docvortex-text a")
    assert [link["href"] for link in links] == [
        "https://example.com/a_%28b%29",
        "https://www.example.org/path?x=1",
        "https://example.net/docs",
        "mailto:user+tag@example.co.uk",
        "http://127.0.0.1:8080/a",
        "https://one.example/a",
        "https://two.example/b",
    ]
    assert [link.get_text() for link in links] == [
        "https://example.com/a_(b)",
        "www.example.org/path?x=1",
        "example.net/docs",
        "user+tag@example.co.uk",
        "http://127.0.0.1:8080/a",
        "https://one.example/a",
        "https://two.example/b",
    ]
    assert "docs。Email" in soup.select_one(".docvortex-text").get_text()


@pytest.mark.parametrize(
    "tld",
    [
        "ai",
        "app",
        "au",
        "biz",
        "br",
        "ca",
        "cloud",
        "cn",
        "com",
        "de",
        "dev",
        "edu",
        "eu",
        "fr",
        "gov",
        "hk",
        "in",
        "info",
        "io",
        "jp",
        "kr",
        "net",
        "nl",
        "nz",
        "online",
        "org",
        "ru",
        "sg",
        "shop",
        "site",
        "store",
        "tech",
        "tw",
        "uk",
        "xyz",
    ],
)
def test_common_engineering_bare_domain_suffixes_linkify(tld: str) -> None:
    """Verify that all naked domain name suffixes commonly used in the B project remain linkable."""
    content = f"Project.Example.{tld}/docs?x=1#intro"
    soup = BeautifulSoup(
        render_html(_middle(_page(0, TextBlock(type="text", index=0, content=_inline(content)))), standalone=False),
        "html.parser",
    )

    link = soup.select_one(".docvortex-text a")
    assert link["href"] == f"https://Project.Example.{tld}/docs?x=1#intro"
    assert link.get_text() == content


@pytest.mark.parametrize(
    "content",
    [
        "machine.Aborted",
        "HostID.lic",
        "requirements.txt",
        "600104.SH",
        "000868.SZ",
        "libcudart.so",
        "TABLEFORMER.md",
        "cs.CL",
        "TSLA.US",
        "A.Balakrishnan",
        "R.Kohli",
    ],
)
def test_non_common_bare_domain_suffixes_remain_plain_text(content: str) -> None:
    """Verifying filenames, tickers, author names, and non-whitelisted suffixes no longer generate links by mistake."""
    soup = BeautifulSoup(
        render_html(_middle(_page(0, TextBlock(type="text", index=0, content=_inline(content)))), standalone=False),
        "html.parser",
    )

    assert soup.select_one(".docvortex-text a") is None
    assert soup.select_one(".docvortex-text").get_text() == content


def test_strong_link_syntax_bypasses_bare_domain_suffix_allowlist() -> None:
    """Verify that explicit HTTP, www, and mailboxes are not whitelisted by the naked domain name B."""
    content = "https://example.ch www.example.ch user@example.ua https://machine.Aborted"
    soup = BeautifulSoup(
        render_html(_middle(_page(0, TextBlock(type="text", index=0, content=_inline(content)))), standalone=False),
        "html.parser",
    )

    assert [link["href"] for link in soup.select(".docvortex-text a")] == [
        "https://example.ch",
        "https://www.example.ch",
        "mailto:user@example.ua",
        "https://machine.Aborted",
    ]


def test_autolink_excludes_existing_links_code_math_algorithm_and_raw_html() -> None:
    """Verify that linkify does not nest explicit links or enter dangerous protocols, codes, formulas, algorithms and raw HTML."""
    text = TextBlock(
        type="text",
        index=0,
        content=[
            {
                "type": "text",
                "content": (
                    "ftp://bad.example.com /relative.example.com 1.2.3.4 "
                    "javascript:evil.example.com data:text/html,data.example.com bad..mail@example.com "
                ),
            },
            {"type": "hyperlink", "url": "https://target.test", "content": _inline("example.com")},
            {"type": "text", "content": " "},
            {"type": "equation_inline", "content": "math.example.com"},
        ],
    )
    code = CodeBlock(
        type="code",
        index=1,
        sub_type="code",
        guess_lang="txt",
        content=[CodeBodyBlock(type="code_body", index=1, content="https://code.example.com")],
    )
    algorithm = CodeBlock(
        type="code",
        index=2,
        sub_type="algorithm",
        content=[AlgorithmBodyBlock(type="algorithm_body", index=2, content=_inline("visit algorithm.example.com"))],
    )
    raw_html = ChartBlock(
        type="chart",
        index=3,
        content=[ChartBodyBlock(type="chart_body", index=3, content="<p>raw.example.com</p>")],
    )
    soup = BeautifulSoup(
        render_html(_middle(_page(0, text, code, algorithm, raw_html)), standalone=False),
        "html.parser",
    )

    links = soup.select("a")
    assert len(links) == 1
    assert links[0]["href"] == "https://target.test"
    assert links[0].get_text() == "example.com"
    assert soup.select_one(".docvortex-math").get_text() == r"\(math.example.com\)"
    assert soup.select_one(".docvortex-code").get_text() == "https://code.example.com"
    assert soup.select_one(".docvortex-algorithm").get_text() == "visit algorithm.example.com"
    assert soup.select_one(".docvortex-figure--chart p").get_text() == "raw.example.com"


def test_formula_body_closing_delimiters_are_neutralized_before_mathjax_scanning() -> None:
    """Verify that the end delimiter in the body of the formula is rewritten to be equivalent to TeX, and carrier will not be closed prematurely."""
    middle = _middle(
        _page(
            0,
            TextBlock(
                type="text",
                index=0,
                content=[{"type": "text", "content": "inline "}, {"type": "equation_inline", "content": r"x\)y"}],
            ),
            EquationBlock(type="equation", index=1, content=r"x\]y"),
        )
    )
    soup = BeautifulSoup(render_html(middle, standalone=False), "html.parser")

    assert soup.select_one(".docvortex-math--inline").get_text() == r"\(x\mathclose{)}y\)"
    assert soup.select_one(".docvortex-math--block").get_text().replace("\n", "") == r"\[x\mathclose{]}y\]"


def test_lists_cover_native_explicit_reference_nested_and_orphan_shapes() -> None:
    """Verify list classification, non-consecutive numbering, explicit marker, references, and nested attributions."""
    ordered = _list(0, "1. first", "3. third")
    alpha = _list(1, "a. alpha", "b. beta")
    explicit = _list(2, "(1) one", "[x] done", "plain")
    mixed = _list(6, "1. one", "- two", "plain")
    reference = _list(3, "[1] first", "Author A", sub_type="ref_text")
    nested = ListBlock(
        type="list",
        index=4,
        content=[
            TextBlock(type="text", content=_inline("- parent")),
            ListBlock(type="list", content=[TextBlock(type="text", content=_inline("- child"))]),
        ],
    )
    orphan = ListBlock(
        type="list",
        index=5,
        content=[ListBlock(type="list", content=[TextBlock(type="text", content=_inline("- orphan"))])],
    )
    markerless_owner = ListBlock(
        type="list",
        index=7,
        content=[
            TextBlock(type="text", content=_inline("1. visible")),
            TextBlock(type="text", content=[]),
            ListBlock(type="list", content=[TextBlock(type="text", content=_inline("- nested owner"))]),
        ],
    )
    soup = BeautifulSoup(
        render_html(
            _middle(_page(0, ordered, alpha, explicit, reference, nested, orphan, mixed, markerless_owner)),
            standalone=False,
        ),
        "html.parser",
    )

    ordered_html = soup.select('[data-block-index="0"] ol')[0]
    assert ordered_html.find_all("li", recursive=False)[1]["value"] == "3"
    assert ordered_html.get_text(" ", strip=True) == "first third"
    assert soup.select_one('[data-block-index="1"] ol')["type"] == "a"
    assert [marker.get_text() for marker in soup.select('[data-block-index="2"] .docvortex-list-marker')] == [
        "(1)",
        "[x]",
        "",
    ]
    assert "[1] first" in soup.select_one('[data-block-index="3"] li').get_text()
    parent_item = soup.select_one('[data-block-index="4"] > .docvortex-list > li')
    assert parent_item.find("ul", recursive=False) is not None
    assert soup.select_one('[data-block-index="5"] .docvortex-list-item--orphan') is not None
    assert [marker.get_text() for marker in soup.select('[data-block-index="6"] .docvortex-list-marker')] == [
        "1.",
        "-",
        "",
    ]
    assert soup.select_one('[data-block-index="7"] .docvortex-list-item--markerless > ul') is not None


def test_index_uses_real_forward_anchor_and_omits_duplicate_ids() -> None:
    """Verify that directory forward links, page number tail cleaning, nested directories, and duplicate anchor first items take effect."""
    index = IndexBlock(
        type="index",
        index=0,
        content=[
            ParagraphTitleBlock(type="paragraph_title", level=2, anchor="sec 1", content=_inline("Section\t3")),
            IndexBlock(
                type="index",
                content=[
                    ParagraphTitleBlock(type="paragraph_title", level=3, anchor="missing", content=_inline("Missing\tiv"))
                ],
            ),
        ],
    )
    first = ParagraphTitleBlock(type="paragraph_title", index=1, level=2, anchor="sec 1", content=_inline("Section"))
    duplicate = ParagraphTitleBlock(type="paragraph_title", index=2, level=6, anchor="sec 1", content=_inline("Duplicate"))
    soup = BeautifulSoup(render_html(_middle(_page(0, index, first, duplicate)), standalone=False), "html.parser")

    assert soup.select_one('.docvortex-index a[href="#sec-1"]').get_text() == "Section"
    assert "3" not in soup.select_one(".docvortex-index").get_text()
    assert soup.select_one(".docvortex-index li ul") is not None
    assert soup.select_one(".docvortex-index li ul a") is None
    assert len(soup.select('[id="sec-1"]')) == 1
    assert soup.find("h6", attrs={"data-heading-level": "6"}).get_text() == "Duplicate"


def test_empty_title_does_not_create_a_broken_index_target_and_anchor_controls_are_normalized() -> None:
    """Verify that empty text titles do not become link targets and that control characters are consistently normalized on both sides of id/href."""
    index = IndexBlock(
        type="index",
        index=0,
        content=[
            ParagraphTitleBlock(type="paragraph_title", level=2, anchor="empty", content=_inline("Empty")),
            ParagraphTitleBlock(type="paragraph_title", level=2, anchor="bad\x00id", content=_inline("Good")),
            ParagraphTitleBlock(type="paragraph_title", level=2, anchor="bad\ufffdid", content=_inline("Collision")),
        ],
    )
    empty = ParagraphTitleBlock(type="paragraph_title", index=1, level=2, anchor="empty", content=[])
    good = ParagraphTitleBlock(type="paragraph_title", index=2, level=2, anchor="bad\x00id", content=_inline("Good"))
    collision = ParagraphTitleBlock(
        type="paragraph_title",
        index=3,
        level=2,
        anchor="bad\ufffdid",
        content=_inline("Collision"),
    )
    soup = BeautifulSoup(
        render_html(_middle(_page(0, index, empty, good, collision)), standalone=False),
        "html.parser",
    )

    links = soup.select(".docvortex-index a")
    assert len(links) == 2
    assert links[0]["href"] == "#bad%EF%BF%BDid"
    assert links[1]["href"] == "#bad%EF%BF%BDid-2"
    assert soup.find(id="bad\ufffdid") is not None
    assert soup.find(id="bad\ufffdid-2") is not None


def test_empty_index_leaf_owns_its_following_nested_index() -> None:
    """Verify that nested ul for empty directory leaves does not incorrectly hang on earlier visible directory entries."""
    index = IndexBlock(
        type="index",
        index=0,
        content=[
            TextBlock(type="text", content=_inline("visible")),
            TextBlock(type="text", content=[]),
            IndexBlock(type="index", content=[TextBlock(type="text", content=_inline("nested"))]),
        ],
    )
    soup = BeautifulSoup(render_html(_middle(_page(0, index)), standalone=False), "html.parser")
    top_items = soup.select(".docvortex-index > ul > li")

    assert len(top_items) == 2
    assert top_items[0].find("ul", recursive=False) is None
    assert top_items[1].find("ul", recursive=False).get_text(strip=True) == "nested"


def test_visual_child_order_image_details_and_asset_precedence() -> None:
    """Verify visual description sequence, image path priority, URL encoding and identification content details."""
    image = ImageBlock.model_validate(
        {
            "type": "image",
            "index": 0,
            "sub_type": "diagram",
            "content": [
                {"type": "image_caption", "content": _inline("before")},
                {
                    "type": "image_body",
                    "index": 0,
                    "content": "description <eq>x</eq>",
                    "image_path": "images/a b.png",
                    "image_base64": _PNG_URI,
                },
                {"type": "image_footnote", "content": _inline("after")},
                {"type": "image_caption", "content": _inline("again")},
            ],
        }
    )
    rendered = render_html(
        _middle(_page(0, image)),
        asset_base_url="https://cdn.example/doc",
        standalone=False,
    )
    soup = BeautifulSoup(rendered, "html.parser")

    assert rendered.index("before") < rendered.index("images/a%20b.png") < rendered.index("after") < rendered.index("again")
    assert soup.select_one("img")["src"] == "https://cdn.example/doc/images/a%20b.png"
    assert "data:image" not in rendered
    assert soup.select_one("details .docvortex-math") is not None
    assert len(soup.select(".docvortex-caption")) == 2


def test_mermaid_flowchart_keeps_raster_primary_and_lazy_details_render() -> None:
    """Verify that the original image is the main view, and only the image is displayed in the folded area and the source code is hidden for on-demand rendering."""
    source_text = '%% comment\ngraph LR\n  A["<script>alert(1)</script>"] --> B'
    middle = _middle(_page(0, _flowchart(_mermaid_fence(source_text))))
    fragment = render_html(middle, standalone=False)
    standalone = render_html(middle)
    soup = BeautifulSoup(fragment, "html.parser")

    image = soup.select_one("img.docvortex-image")
    assert image is not None and image["src"].startswith("data:image/png")
    assert image.find_parent("details") is None
    details = soup.select_one("details.docvortex-flowchart-details")
    assert "open" not in details.attrs
    assert details.summary.get_text(strip=True) == "flowchart"
    assert [child.name for child in details.find_all(recursive=False)] == ["summary", "div", "pre"]
    host = details.select_one(".docvortex-flowchart")
    assert host["data-mermaid-state"] == "pending"
    assert host.select_one('.docvortex-flowchart-canvas[role="img"]') is not None
    hidden_source = details.select_one(".docvortex-flowchart-source")
    assert hidden_source.has_attr("hidden")
    assert hidden_source.code.get_text() == source_text
    assert soup.find("script") is None
    assert "mermaid@" not in fragment
    assert "mermaid@11.16.1/dist/mermaid.min.js" in standalone
    assert "sha384-aBQXj4hK6Jm05i7aQAsUV3bLdSUrHX1BGYfMB0166TtWt/RRaw+h0Eelme9OCOvy" in standalone
    assert "securityLevel: 'strict'" in standalone
    assert "suppressErrorRendering: true" in standalone
    assert "maxTextSize: 50000" in standalone and "maxEdges: 500" in standalone
    assert "flowchart: {htmlLabels: false, useMaxWidth: true}" in standalone
    assert "bindFunctions" not in standalone
    assert "addEventListener('toggle'" in standalone
    assert "closest('details')" in standalone
    assert "Flowchart unavailable" in standalone


def test_mermaid_flowchart_without_raster_opens_diagram() -> None:
    """When verifying that there is no original image, the source code will fall back as a rendering failure, and will be hidden by CSS after successful rendering."""
    soup = BeautifulSoup(
        render_html(
            _middle(_page(0, _flowchart(_mermaid_fence("flowchart TD\n  A --> B"), with_raster=False))),
            standalone=False,
        ),
        "html.parser",
    )

    host = soup.select_one(".docvortex-flowchart")
    details = host.find_parent("details")
    assert details is not None and "docvortex-flowchart-details" in details.get("class", [])
    assert details.summary.get_text(strip=True) == "flowchart"
    assert not details.select_one(".docvortex-flowchart-source").has_attr("hidden")
    assert soup.select_one("img") is None
    assert "open" in details.attrs


@pytest.mark.parametrize(
    "content",
    [
        _mermaid_fence("sequenceDiagram\n  A->>B: example.com"),
        _mermaid_fence('%%{init: {"securityLevel": "loose"}}%%\ngraph LR\n  A --> B'),
        _mermaid_fence("---\ntheme: dark\n---\ngraph LR\n  A --> B"),
        _mermaid_fence("graph LR\n  A --> B", language="python"),
        _mermaid_fence(f"graph LR\n  A[{'x' * 50_001}]"),
    ],
    # Use short ID to avoid pytest from writing long source code into the Windows environment variable.
    ids=["unsupported-diagram", "init-directive", "frontmatter", "wrong-language", "oversized-source"],
)
def test_invalid_or_out_of_scope_mermaid_keeps_existing_image_path(content: str) -> None:
    """Verify that non-flowchart, configuration injection, error fence and over-limit source code do not trigger Mermaid dependencies."""
    rendered = render_html(_middle(_page(0, _flowchart(content))))
    soup = BeautifulSoup(rendered, "html.parser")

    assert soup.select_one(".docvortex-flowchart") is None
    assert soup.select_one(".docvortex-image") is not None
    assert soup.select_one(".docvortex-details") is not None
    assert soup.select_one(".docvortex-details a") is None
    assert "mermaid@" not in rendered


def test_table_keeps_safe_html_and_spatial_or_image_fallbacks() -> None:
    """Verify native tables, formulas, dangerous attribute cleaning, space text and unavailable table image fallback."""
    html_table = TableBlock(
        type="table",
        index=0,
        content=[
            TableBodyBlock(
                type="table_body",
                index=0,
                content=(
                    '<table border="1"><tr><td rowspan="2" onclick="bad()">'
                    "<eq>x&lt;y</eq><table><tr><td>N</td></tr></table></td></tr></table>"
                ),
            )
        ],
    )
    spatial = TableBlock(
        type="table",
        index=1,
        content=[TableBodyBlock(type="table_body", index=1, content="A < B\nC   D")],
    )
    invalid = TableBlock(
        type="table",
        index=2,
        content=[TableBodyBlock(type="table_body", index=2, content="<table></table>", image_base64=_PNG_URI)],
    )
    soup = BeautifulSoup(render_html(_middle(_page(0, html_table, spatial, invalid)), standalone=False), "html.parser")

    assert len(soup.select('[data-block-index="0"] table')) == 2
    cell = soup.select_one('[data-block-index="0"] td')
    assert cell["rowspan"] == "2"
    assert "onclick" not in cell.attrs and "border" not in soup.select_one("table").attrs
    assert cell.select_one(".docvortex-math") is not None
    assert soup.select_one('[data-block-index="1"] .docvortex-table-text').get_text() == "A < B\nC   D"
    assert soup.select_one('[data-block-index="2"] img')["src"].startswith("data:image/png")


def test_table_preserves_standard_inline_style_tags() -> None:
    """Validate table security HTML Preserves the standard inline style tags supported by renderer."""
    table = TableBlock(
        type="table",
        index=0,
        content=[
            TableBodyBlock(
                type="table_body",
                index=0,
                content=(
                    "<table><tr><td><strong>bold</strong><em>italic</em><u>under</u>"
                    "<s>strike</s><sup>sup</sup><sub>sub</sub></td></tr></table>"
                ),
            )
        ],
    )

    soup = BeautifulSoup(render_html(_middle(_page(0, table)), standalone=False), "html.parser")
    cell = soup.select_one("td")

    assert [cell.select_one(tag).get_text() for tag in ("strong", "em", "u", "s", "sup", "sub")] == [
        "bold",
        "italic",
        "under",
        "strike",
        "sup",
        "sub",
    ]


def test_discarded_invalid_table_math_does_not_load_mathjax() -> None:
    """After verifying that there is no cell table fallback image, the discarded eq will not trigger MathJax accidentally."""
    table = TableBlock(
        type="table",
        index=0,
        content=[
            TableBodyBlock(
                type="table_body",
                index=0,
                content="<table><eq>x</eq></table>",
                image_base64=_PNG_URI,
            )
        ],
    )
    rendered = render_html(_middle(_page(0, table)))

    assert BeautifulSoup(rendered, "html.parser").select_one("article .docvortex-math") is None
    assert "mathjax@" not in rendered
    assert "data:image/png" in rendered


def test_chart_gfm_details_code_prism_and_algorithm_html() -> None:
    """Verification strict chart GFM, image details, Prism Autoloader and algorithmic escapes."""
    chart = ChartBlock(
        type="chart",
        index=0,
        sub_type="line chart",
        content=[
            ChartAnnotationBlock(type="chart_caption", content=_inline("chart before")),
            ChartBodyBlock(
                type="chart_body",
                index=0,
                image_path="images/chart.png",
                content="| A | B |\n| :--- | ---: |\n| 1 | 2 |",
            ),
        ],
    )
    code = CodeBlock(
        type="code",
        index=1,
        sub_type="code",
        guess_lang="shell",
        content=[
            CodeBodyBlock(type="code_body", index=1, content='echo "<x>"\n'),
            CodeAnnotationBlock(type="code_footnote", content=_inline("code after")),
        ],
    )
    algorithm = CodeBlock(
        type="code",
        index=2,
        sub_type="algorithm",
        content=[
            AlgorithmBodyBlock(
                type="algorithm_body",
                index=2,
                content=[
                    {"type": "text", "content": "if a < b:\n  T"},
                    {"type": "text", "content": "bold", "styles": ["bold"]},
                    {"type": "text", "content": "italic", "styles": ["italic"]},
                    {"type": "text", "content": "strike", "styles": ["strikethrough"]},
                    {"type": "text", "content": "under", "styles": ["underline"]},
                    {"type": "text", "content": "dot", "styles": ["emphasis"]},
                    {"type": "text", "content": "q", "styles": ["subscript"]},
                    {"type": "text", "content": " = "},
                    {"type": "equation_inline", "content": "x"},
                    {"type": "equation_inline", "content": "y"},
                ],
            )
        ],
    )
    result = render_html(_middle(_page(0, chart, code, algorithm)))
    soup = BeautifulSoup(result, "html.parser")

    assert soup.select_one("details .docvortex-chart-table") is not None
    assert soup.select_one("th.docvortex-align-left") is not None
    assert soup.select_one("th.docvortex-align-right") is not None
    assert soup.select_one("pre.language-bash code.language-bash").get_text() == 'echo "<x>"\n'
    assert "prismjs@1.30.0/components/prism-core.min.js" in result
    assert "prismjs@1.30.0/plugins/autoloader/prism-autoloader.min.js" in result
    assert "Prism.highlightAllUnder(root)" in result
    assert "sha384-zLRFO4dw" in result and "sha384-Uq05+JLk" in result
    algorithm_html = soup.select_one(".docvortex-algorithm")
    assert "if a < b:" in algorithm_html.get_text()
    assert algorithm_html.select_one("strong").get_text() == "bold"
    assert algorithm_html.select_one("em").get_text() == "italic"
    assert algorithm_html.select_one("s").get_text() == "strike"
    assert algorithm_html.select_one("u").get_text() == "under"
    assert algorithm_html.select_one(".docvortex-text-emphasis").get_text() == "dot"
    assert algorithm_html.select_one("sub").get_text() == "q"
    assert len(algorithm_html.select(".docvortex-math")) == 2
    assert r"\(x\) \(y\)" in algorithm_html.get_text()
    assert "<br" not in str(algorithm_html)
    assert "if a &lt; b:\n  " in str(algorithm_html)
    assert "**bold**" not in str(algorithm_html)
    assert "*italic*" not in str(algorithm_html)
    assert "~~strike~~" not in str(algorithm_html)


@pytest.mark.parametrize("guess_lang", ["python bad/../../x", "constructor", "prototype", "__proto__"])
def test_invalid_code_language_remains_visible_without_loading_prism(guess_lang: str) -> None:
    """Verify that illegal language names do not enter the class or CDN component paths."""
    code = CodeBlock(
        type="code",
        index=0,
        sub_type="code",
        guess_lang=guess_lang,
        content=[CodeBodyBlock(type="code_body", index=0, content="<script>x</script>")],
    )
    result = render_html(_middle(_page(0, code)))
    soup = BeautifulSoup(result, "html.parser")

    assert soup.select_one("code").get_text() == "<script>x</script>"
    assert not soup.select_one("code").get("class")
    assert "prismjs@" not in result
    assert "<script>x</script>" not in result


def test_empty_code_and_literal_class_text_do_not_load_external_runtimes() -> None:
    """Verify that empty codes and the words class in plain text do not accidentally trigger Prism or MathJax."""
    middle = _middle(
        _page(
            0,
            TextBlock(type="text", index=0, content=_inline('class="docvortex-math fake" class="language-python"')),
            CodeBlock(
                type="code",
                index=1,
                sub_type="code",
                guess_lang="python",
                content=[CodeBodyBlock(type="code_body", index=1, content="")],
            ),
        )
    )
    rendered = render_html(middle)

    assert "mathjax@" not in rendered
    assert "prismjs@" not in rendered
    assert '<pre class="docvortex-code"><code></code></pre>' in rendered


def test_crlf_and_cr_are_normalized_to_visible_line_breaks() -> None:
    """Verify that CRLF and CR are unified into two visible HTML line breaks."""
    rendered = render_html(
        _middle(_page(0, TextBlock(type="text", index=0, content=_inline("one\r\ntwo\rthree")))),
        standalone=False,
    )
    paragraph = BeautifulSoup(rendered, "html.parser").select_one(".docvortex-text")

    assert len(paragraph.find_all("br")) == 2
    assert paragraph.get_text("|", strip=True) == "one|two|three"


def test_invalid_html_characters_are_replaced_and_output_remains_utf8_encodable() -> None:
    """Verify control characters in plain text, code, anchor, and rich HTML with visible degradation of surrogate."""
    middle = _middle(
        _page(
            0,
            ParagraphTitleBlock(type="paragraph_title", index=0, level=2, anchor="a\ud800", content=_inline("T\x01")),
            TextBlock.model_construct(
                type="text",
                index=1,
                content=[TextSpan.model_construct(type="text", content="body\udfff", styles=[])],
            ),
            CodeBlock(
                type="code",
                index=2,
                sub_type="code",
                guess_lang="txt",
                content=[CodeBodyBlock(type="code_body", index=2, content="code\ud800\x02")],
            ),
            ChartBlock(
                type="chart",
                index=3,
                content=[ChartBodyBlock(type="chart_body", index=3, content="<p>chart\udfff\x03</p>")],
            ),
            TableBlock(
                type="table",
                index=4,
                content=[TableBodyBlock(type="table_body", index=4, content="table\ud800\x04")],
            ),
        )
    )
    rendered = render_html(middle)

    rendered.encode("utf-8")
    assert "\ud800" not in rendered and "\udfff" not in rendered
    assert rendered.count("\ufffd") >= 6


def test_setext_heading_shape_is_not_misclassified_as_a_gfm_chart_table() -> None:
    """Verify that the Setext header form missing pipe is output as normal chart text."""
    chart = ChartBlock(
        type="chart",
        index=0,
        content=[ChartBodyBlock(type="chart_body", index=0, content="Title\n---\nbody")],
    )
    soup = BeautifulSoup(render_html(_middle(_page(0, chart)), standalone=False), "html.parser")

    assert soup.select_one(".docvortex-chart-table") is None
    assert soup.select_one(".docvortex-figure--chart").get_text(" ", strip=True) == "Title --- body"


@pytest.mark.parametrize(
    ("slash_count", "expected"),
    [(1, "|"), (2, "|"), (3, r"\|"), (4, r"\|"), (5, r"\\|")],
)
def test_chart_gfm_pipe_matches_markdown_it_backslash_decoding(slash_count: int, expected: str) -> None:
    """Verify that any adjacent backslashes protect pipe within the column and are consistent with the decoding result of markdown-it."""
    chart = ChartBlock(
        type="chart",
        index=0,
        content=[
            ChartBodyBlock(
                type="chart_body",
                index=0,
                content="| Formula |\n| --- |\n| " + "\\" * slash_count + "| |",
            )
        ],
    )
    soup = BeautifulSoup(render_html(_middle(_page(0, chart)), standalone=False), "html.parser")

    assert soup.select_one(".docvortex-chart-table td").get_text() == expected


def test_mineru_styles_are_minified_scoped_and_inlined_byte_exact() -> None:
    """Validate independent style artifact volume, scope, and verbatim inline contract for standalone."""
    root = resources.files("docvortex").joinpath("resources", "html")
    source = root.joinpath("docvortex.css").read_text(encoding="utf-8")
    minified = root.joinpath("docvortex.min.css").read_text(encoding="utf-8")
    standalone = render_html(_middle())
    style = BeautifulSoup(standalone, "html.parser").style

    assert len(minified.encode("utf-8")) <= 10 * 1024
    assert "\n" not in minified and "/*" not in minified
    assert ".docvortex-document" in source and ".docvortex-html-body" in source
    assert style is not None and style.string == minified
    assert f"<style>{minified}</style>" in standalone
    assert not root.joinpath("crossnote").is_dir()


def test_empty_document_and_equation_image_do_not_load_external_scripts() -> None:
    """Verify that empty documents and pure formula images do not unnecessarily load MathJax or Prism."""
    empty = render_html(_middle())
    image_equation = render_html(_middle(_page(0, EquationBlock(type="equation", index=0, content="", image_base64=_PNG_URI))))

    assert (
        '<article class="docvortex-document docvortex-document--default" '
        'data-docvortex-html-version="1" data-render-mode="default">\n\n</article>'
    ) in empty
    assert "mathjax@" not in empty and "prismjs@" not in empty and "mermaid@" not in empty
    assert "mathjax@" not in image_equation and "prismjs@" not in image_equation and "mermaid@" not in image_equation
    assert "data:image/png" in image_equation
