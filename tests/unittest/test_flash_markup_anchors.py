from __future__ import annotations

from dataclasses import dataclass

from lxml import etree

from docvortex.analyzers.native._shared.markup import (
    AnchorVisibilityScope,
    MarkupAnchorDocument,
    MarkupAnchorRegistry,
    MarkupStylesheet,
    canonical_anchor,
    element_id,
    visible_element_text,
)


@dataclass(frozen=True, slots=True)
class _TestAnchorPolicy:
    """Provides switchable footnote semantics strategy for shared anchor unit tests."""

    anchor_prefix: str = "test"
    register_document_start: bool = True
    note_marker: str = "primary"

    @staticmethod
    def heading_identity(element: etree._Element, ordinal: int) -> str:
        """Generate the test title identity according to the source ID and the serial number in the document."""
        return f"heading-{element_id(element) or 'anonymous'}-{ordinal}"

    def is_materializable_note(self, element: etree._Element, document: MarkupAnchorDocument) -> bool:
        """Only test footnotes that match the current policy and have final visible text are registered."""
        return element.get("data-note") == self.note_marker and bool(visible_element_text(element, document))

    @staticmethod
    def note_identity(element: etree._Element, ordinal: int) -> str:
        """Generate the test footnote identity according to the source ID and the serial number in the document."""
        return f"note-{element_id(element) or 'anonymous'}-{ordinal}"


def _document(
    key: str,
    root: etree._Element,
    *,
    visibility_scope: AnchorVisibilityScope = "all_ancestors",
) -> MarkupAnchorDocument:
    """Construct a shared anchor test document with an empty stylesheet."""
    return MarkupAnchorDocument(
        key=key,
        root=root,
        stylesheet=MarkupStylesheet(),
        visibility_scope=visibility_scope,
    )


def test_markup_anchor_element_id_and_canonical_digest_are_stable() -> None:
    """Verify id/xml:id priority, blank cleaning and twenty-digit SHA-256 anchor contract."""
    html_id = etree.fromstring(b'<p id=" html-id " xml:id="xml-id"/>')
    xml_id = etree.fromstring(b'<p xml:id="xml-only"/>')
    anonymous = etree.fromstring(b"<p/>")

    assert element_id(html_id) == "html-id"
    assert element_id(xml_id) == "xml-only"
    assert element_id(anonymous) is None
    assert canonical_anchor("epub", "EPUB/text/ch1.xhtml", "chapter-one-0") == "epub-1daacccca5bf43833643"
    assert canonical_anchor("html", "html", "heading-top-0") == "html-39fc7010518f54fa3fa9"


def test_markup_anchor_registry_filters_hidden_targets_and_keeps_visible_descendants() -> None:
    """Verify that hidden captions/footnotes are not registered while visibility:visible descendants still provide landing text."""
    root = etree.fromstring(
        b"""<html><body>
        <h1 id="visible">Visible title</h1>
        <h2 id="hidden" hidden="hidden">Hidden title</h2>
        <h3 id="recovered" style="visibility:hidden">Hidden <span style="visibility:visible">Recovered title</span></h3>
        <aside id="visible-note" data-note="primary">Visible note</aside>
        <aside id="hidden-note" data-note="primary" style="display:none">Hidden note</aside>
        <aside data-note="primary" style="visibility:hidden"><span style="visibility:visible">Recovered note</span></aside>
        </body></html>"""
    )
    registry = MarkupAnchorRegistry([_document("doc", root)], _TestAnchorPolicy())
    elements = {element_id(element): element for element in root.iter() if element_id(element) is not None}
    anonymous_note = next(element for element in root.iter("aside") if element_id(element) is None)

    visible_anchor = registry.heading_anchor(elements["visible"])
    recovered_anchor = registry.heading_anchor(elements["recovered"])
    assert visible_anchor is not None
    assert recovered_anchor is not None
    assert registry.heading_label(visible_anchor) == "Visible title"
    assert registry.heading_label(recovered_anchor) == "Recovered title"
    assert registry.heading_anchor(elements["hidden"]) is None
    assert registry.resolve_target("doc", "hidden") is None
    assert registry.note_anchor(elements["visible-note"]) is not None
    assert registry.note_anchor(elements["hidden-note"]) is None
    assert registry.note_anchor(anonymous_note) is not None


def test_markup_anchor_registry_resolves_direct_ancestor_descendant_and_duplicate_fragments() -> None:
    """Verify that fragment maps to the direct, nearest ancestor, and first descendant targets in that order, with duplicate ID first mapping first."""
    first_root = etree.fromstring(
        b"""<body>
        <h1 id="direct">Direct</h1>
        <h2>Ancestor target <span id="inside-heading">inside</span></h2>
        <section id="container"><h3>Descendant target</h3></section>
        <section id="duplicate"><h4>First duplicate</h4></section>
        <h5 id="duplicate">Second duplicate</h5>
        </body>"""
    )
    second_root = etree.fromstring(b'<body><h1 id="direct">Other document</h1></body>')
    first_document = _document("doc-a", first_root)
    second_document = _document("doc-b", second_root)
    registry = MarkupAnchorRegistry([first_document, second_document], _TestAnchorPolicy())
    headings = list(first_root.iter("h1", "h2", "h3", "h4", "h5"))
    second_heading = next(second_root.iter("h1"))

    assert registry.resolve_target("doc-a", "direct") == registry.heading_anchor(headings[0])
    assert registry.resolve_target("doc-a", "inside-heading") == registry.heading_anchor(headings[1])
    assert registry.resolve_target("doc-a", "container") == registry.heading_anchor(headings[2])
    assert registry.resolve_target("doc-a", "duplicate") == registry.heading_anchor(headings[3])
    assert registry.resolve_target("doc-a", "duplicate") != registry.heading_anchor(headings[4])
    assert registry.resolve_target("doc-b", "direct") == registry.heading_anchor(second_heading)
    assert registry.resolve_target("doc-a", None) == registry.heading_anchor(headings[0])
    assert registry.resolve_target("doc-b", None) == registry.heading_anchor(second_heading)


def test_markup_anchor_document_start_visibility_scope_and_policy_do_not_leak() -> None:
    """Verify that optional document origins, ancestor ranges, and different footnotes policy are isolated from each other."""
    root = etree.fromstring(
        b"""<html hidden="hidden"><body><h1 id="title">Visible in body scope</h1>
        <aside id="primary" data-note="primary">Primary note</aside>
        <aside id="secondary" data-note="secondary">Secondary note</aside>
        </body></html>"""
    )
    all_ancestors = _document("all", root)
    body_scope = _document("body", root, visibility_scope="nearest_body")
    assert visible_element_text(next(root.iter("h1")), all_ancestors) == ""
    assert visible_element_text(next(root.iter("h1")), body_scope) == "Visible in body scope"

    primary = MarkupAnchorRegistry(
        [body_scope],
        _TestAnchorPolicy(anchor_prefix="primary", register_document_start=False, note_marker="primary"),
    )
    secondary = MarkupAnchorRegistry(
        [body_scope],
        _TestAnchorPolicy(anchor_prefix="secondary", register_document_start=True, note_marker="secondary"),
    )
    elements = {element_id(element): element for element in root.iter() if element_id(element) is not None}

    assert primary.resolve_target("body", None) is None
    assert secondary.resolve_target("body", None) == secondary.heading_anchor(elements["title"])
    assert primary.note_anchor(elements["primary"]) is not None
    assert primary.note_anchor(elements["secondary"]) is None
    assert secondary.note_anchor(elements["primary"]) is None
    assert secondary.note_anchor(elements["secondary"]) is not None
    assert primary.note_anchor(elements["primary"]).startswith("primary-")  # type: ignore[union-attr]
    assert secondary.note_anchor(elements["secondary"]).startswith("secondary-")  # type: ignore[union-attr]
