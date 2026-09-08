from __future__ import annotations

from io import BytesIO
from urllib.parse import unquote
from zipfile import ZipFile

import pytest
from bs4 import BeautifulSoup

from _epub_test_utils import _PNG_BYTES, build_epub_notes_fixture
from _native_test_utils import analyze_native_test_document
from _span_test_utils import inline_text, inline_urls
from docvortex.render import render_epub, render_html, render_markdown
from docvortex.schema import BlockType, MiddleJson, PageFootnoteBlock, TextBlock, PageBlock


def build_backlinks_epub(*, notes_first: bool = False) -> bytes:
    """构造同段别名、图片分段、跨章节返回和无效目标的真实 EPUB 包。"""
    chapter = """<html xmlns="http://www.w3.org/1999/xhtml"
        xmlns:epub="http://www.idpf.org/2007/ops"><body>
      <h1 id="chapter">Backlink chapter</h1>
      <p id="paragraph">Before <a id="ref" href="notes.xhtml#note">[1]</a>
        <span xml:id="alias">alias</span><img src="pixel.png"/>
        After <a id="after" href="notes.xhtml#note"><sup>[2]</sup></a>.</p>
      <p id="second">Second <a id="ref-two" href="notes.xhtml#note">[3]</a>.</p>
      <p id="ref">Duplicate ID must not replace first.</p>
      <p id="编码">Encoded target <a href="notes.xhtml#note">[4]</a>.</p>
      <p>Visible <span id="hidden" hidden="hidden">hidden</span>
        <span id="empty"> </span><a href="notes.xhtml#missing">missing</a>.</p>
      <p id="invisible" style="visibility:hidden">Invisible.</p>
      <div id="container"><p>Container first.</p><p>Container second.</p></div>
      <p><span style="display:none" id="discard">discard</span>Tail.</p>
    </body></html>"""
    notes = """<html xmlns="http://www.w3.org/1999/xhtml"
        xmlns:epub="http://www.idpf.org/2007/ops"><body>
      <h1>Notes</h1>
      <p id="ref">Other chapter target.</p>
      <aside epub:type="footnote" id="note"><p>Note
        <a href="chapter.xhtml#ref">back-one</a>
        <a href="chapter.xhtml#alias">back-alias</a>
        <a href="chapter.xhtml#paragraph">back-paragraph</a>
        <a href="chapter.xhtml#after">back-after</a>
        <a href="chapter.xhtml#ref-two">back-two</a>
        <a href="chapter.xhtml#second">back-second</a>
        <a href="chapter.xhtml#%E7%BC%96%E7%A0%81">back-encoded</a>
        <a href="#ref">back-local</a>
        <a href="chapter.xhtml#container">back-container</a>
        <a href="chapter.xhtml#hidden"><em>hidden-label</em></a>
        <a href="chapter.xhtml#empty">empty-label</a>
        <a href="chapter.xhtml#invisible">invisible-label</a>
        <a href="chapter.xhtml#discard">discard-label</a>
        <a href="chapter.xhtml#missing">missing-label</a>
      </p><p>Note continuation without return.</p></aside>
      <table><tr><td><a href="chapter.xhtml#after">table-back</a>
        <a href="chapter.xhtml#missing">table-missing</a></td></tr></table>
    </body></html>"""
    output = BytesIO()
    with ZipFile(BytesIO(build_epub_notes_fixture())) as source, ZipFile(output, "w") as target:
        for item in source.infolist():
            data = source.read(item.filename)
            if item.filename == "EPUB/chapter.xhtml":
                data = chapter.encode()
            elif item.filename == "EPUB/notes.xhtml":
                data = notes.encode()
            elif item.filename == "EPUB/package.opf" and notes_first:
                data = data.replace(
                    b'<itemref idref="chapter"/><itemref idref="notes"/>',
                    b'<itemref idref="notes"/><itemref idref="chapter"/>',
                )
            target.writestr(item, data)
        target.writestr("EPUB/pixel.png", _PNG_BYTES)
    return output.getvalue()


def _blocks(middle: MiddleJson) -> list[PageBlock]:
    """按源顺序展平页面，便于断言真实物化目标。"""
    return [block for page in middle.pages for block in page.blocks]


def _links(block: TextBlock | PageFootnoteBlock) -> dict[str, str]:
    """以保留的链接文字建立目标映射。"""
    return {inline_text(span.content): span.url for span in block.content if span.type == "hyperlink"}


@pytest.mark.parametrize("notes_first", [False, True])
def test_epub_backlinks_bind_actual_text_segments(notes_first: bool) -> None:
    """验证跨章节解析顺序不影响正文、别名、分段和无效目标的链接语义。"""
    middle, model = analyze_native_test_document(build_backlinks_epub(notes_first=notes_first), file_suffix="epub")
    blocks = _blocks(middle)
    texts = [block for block in blocks if isinstance(block, TextBlock)]
    before = next(block for block in texts if inline_text(block.content).startswith("Before"))
    after = next(block for block in texts if inline_text(block.content).startswith("After"))
    second = next(block for block in texts if inline_text(block.content).startswith("Second "))
    encoded = next(block for block in texts if inline_text(block.content).startswith("Encoded "))
    local = next(block for block in texts if inline_text(block.content) == "Other chapter target.")
    container = next(block for block in texts if inline_text(block.content) == "Container first.")
    note = next(block for block in blocks if isinstance(block, PageFootnoteBlock) and block.anchor)
    links = _links(note)
    assert before.anchor and after.anchor and before.anchor != after.anchor
    assert links == {
        "back-one": f"#{before.anchor}",
        "back-alias": f"#{before.anchor}",
        "back-paragraph": f"#{before.anchor}",
        "back-after": f"#{after.anchor}",
        "back-two": f"#{second.anchor}",
        "back-second": f"#{second.anchor}",
        "back-encoded": f"#{encoded.anchor}",
        "back-local": f"#{local.anchor}",
        "back-container": f"#{container.anchor}",
    }
    assert local.anchor != before.anchor
    for block in [before, after, second, encoded]:
        assert inline_urls(block.content) == [f"#{note.anchor}"]
    assert next(block for block in texts if inline_text(block.content).startswith("Duplicate ID")).anchor is None
    continuation = next(block for block in blocks if isinstance(block, PageFootnoteBlock) and not block.anchor)
    assert not inline_urls(continuation.content)
    assert "hidden-label" in inline_text(note.content)
    assert any(span.type == "text" and "italic" in span.styles and "hidden-label" in span.content for span in note.content)
    assert "epub-pending-" not in middle.model_dump_json()
    assert "epub-pending-" not in model.model_dump_json()
    table = next(block for block in blocks if block.type == BlockType.TABLE)
    assert f"#{after.anchor}" in str(table)
    assert "table-missing" in str(table)


def _assert_fragment_targets(document: str) -> None:
    """验证所有输出内部链接都命中文档中唯一的实际 DOM 目标。"""
    soup = BeautifulSoup(document, "html.parser")
    for link in soup.select('a[href^="#"]'):
        target = unquote(link["href"][1:])
        assert len(soup.find_all(id=target)) == 1, (link, target)


def test_epub_backlinks_survive_render_and_protocol_roundtrips() -> None:
    """验证 HTML、Markdown、EPUB 输出及 JSON/HTML/EPUB 重读都保留双向链接。"""
    middle, _ = analyze_native_test_document(build_backlinks_epub(), file_suffix="epub")
    original = [
        (" ".join(inline_text(block.content).split()), _links(block))
        for block in _blocks(middle)
        if isinstance(block, (TextBlock, PageFootnoteBlock))
    ]
    restored = MiddleJson.model_validate_json(middle.model_dump_json())
    assert restored == middle
    html = render_html(middle)
    _assert_fragment_targets(html)
    decoded, _ = analyze_native_test_document(html.encode(), file_suffix="html")
    assert [
        (" ".join(inline_text(block.content).split()), _links(block))
        for block in _blocks(decoded)
        if isinstance(block, (TextBlock, PageFootnoteBlock))
    ] == original
    markdown = render_markdown(middle)
    for block in _blocks(middle):
        if isinstance(block, (TextBlock, PageFootnoteBlock)):
            for url in inline_urls(block.content):
                assert url in markdown
                assert f'id="{url[1:]}"' in markdown
    epub = render_epub(middle)
    with ZipFile(BytesIO(epub)) as archive:
        content = next(archive.read(name).decode() for name in archive.namelist() if name.endswith("content.xhtml"))
    _assert_fragment_targets(content)
    reloaded, _ = analyze_native_test_document(epub, file_suffix="epub")
    reloaded_note = next(block for block in _blocks(reloaded) if isinstance(block, PageFootnoteBlock) and block.anchor)
    assert set(_links(reloaded_note)) == set(
        _links(next(block for block in _blocks(middle) if isinstance(block, PageFootnoteBlock) and block.anchor))
    )
    targets = {
        f"#{block.anchor}": block
        for block in _blocks(reloaded)
        if isinstance(block, (TextBlock, PageFootnoteBlock)) and block.anchor
    }
    for label, url in _links(reloaded_note).items():
        assert isinstance(targets[url], TextBlock), label
    _assert_fragment_targets(render_html(reloaded))


def test_html_hyperlinks_use_allocated_ids_without_mutating_semantics() -> None:
    """验证空白编码及 ID 碰撞时使用最终分配目标，渲染不改写原始语义树。"""
    middle, _ = analyze_native_test_document(build_backlinks_epub(), file_suffix="epub")

    def text(value: str) -> list[dict[str, str]]:
        """构造不带额外样式的最小行内文字。"""
        return [{"type": "text", "content": value}]

    middle.pages[0].blocks = [
        TextBlock(
            type="text",
            index=0,
            anchor="body target",
            content=[{"type": "hyperlink", "url": "#note%20target", "content": text("go")}],
        ),
        TextBlock(type="text", index=1, anchor="body-target", content=text("collision")),
        PageFootnoteBlock(
            type="page_footnote",
            index=2,
            anchor="note target",
            content=[
                {"type": "hyperlink", "url": "#body%20target", "content": text("back")},
                {"type": "hyperlink", "url": "#body-target", "content": text("other")},
                {"type": "hyperlink", "url": "#missing", "content": text("missing")},
            ],
        ),
    ]
    middle.pages = middle.pages[:1]
    before = middle.model_dump_json()
    output = render_html(middle)
    _assert_fragment_targets(output)
    soup = BeautifulSoup(output, "html.parser")
    assert soup.find("a", string="back")["href"] == "#body-target"
    assert soup.find("a", string="other")["href"] == "#body-target-2"
    assert soup.find("a", string="missing") is None
    assert middle.model_dump_json() == before
