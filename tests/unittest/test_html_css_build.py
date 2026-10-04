from __future__ import annotations

from pathlib import Path

import pytest

from tools import build_html_css


def test_committed_minified_css_matches_readable_source() -> None:
    """Verify that min products submitted by the repository are always deterministically generated from current readable source code."""
    project_root = Path(__file__).resolve().parents[2]
    resource_root = project_root / "src" / "docvortex" / "resources" / "html"
    source = resource_root.joinpath("docvortex.css").read_text(encoding="utf-8")
    minified = resource_root.joinpath("docvortex.min.css").read_text(encoding="utf-8")

    assert minified == build_html_css.minify_css(source)


def test_visual_bodies_captions_and_footnotes_align_left() -> None:
    """Verify that the visual body and description are posted on the left side of the main text, and long descriptions are also left aligned."""
    project_root = Path(__file__).resolve().parents[2]
    css_path = project_root.joinpath("src", "docvortex", "resources", "html", "docvortex.css")
    source = css_path.read_text(encoding="utf-8")

    assert "width: fit-content" not in source
    assert (
        ".docvortex-document .docvortex-figure > img,\n"
        ".docvortex-document .docvortex-visual-body > img {\n  display: block;\n  margin-inline: 0;\n}"
    ) in source
    assert ".docvortex-document .docvortex-flowchart {\n  margin: 1rem 0 0.5rem;" in source
    assert ".docvortex-document .docvortex-flowchart-canvas {\n  display: none;\n  min-width: 0;\n  text-align: left;" in source
    assert (
        '.docvortex-document .docvortex-flowchart-source[hidden],\n'
        '.docvortex-document .docvortex-flowchart[data-mermaid-state="rendered"] + .docvortex-flowchart-source {\n'
        '  display: none;\n}'
    ) in source
    assert (
        ".docvortex-document .docvortex-caption {\n"
        "  color: var(--docvortex-muted);\n"
        "  font-size: 0.9em;\n"
        "  margin-top: 0.5rem;\n"
        "  text-align: left;\n"
        "}"
    ) in source
    assert (
        ".docvortex-document .docvortex-footnote,\n"
        ".docvortex-document .docvortex-page-footnote {\n"
        "  color: var(--docvortex-muted);\n"
        "  font-size: 0.875em;\n"
        "  margin-top: 0.4rem;\n"
        "  text-align: left;\n"
        "}"
    ) in source


def test_minify_css_preserves_strings_escapes_and_calc_spacing() -> None:
    """Verify that the compression process removes only safe whitespace and does not break strings, escapes, and calc operators."""
    source = r"""
/* removable */
@media screen and (max-width: 40rem) {
  .demo::before {
    content: "a /* not comment */ b";
    font-family: "Courier New";
    width: calc(100% - 2rem);
  }
  .escaped\ class {
    --label: 'x\' y';
  }
}
"""

    expected = (
        '@media screen and (max-width:40rem){.demo::before{content:"a /* not comment */ b";'
        'font-family:"Courier New";width:calc(100% - 2rem)}'
        ".escaped\\ class{--label:'x\\' y'}}"
    )
    assert build_html_css.minify_css(source) == expected


def test_minify_css_preserves_descendant_combinator_before_pseudo_class() -> None:
    """Verify that descending spaces before pseudo-classes are not mistaken for redundant spaces next to declaration colons."""
    source = ".root :is(h1, h2) { color: red; } .root:hover { color: blue; }"

    assert build_html_css.minify_css(source) == (".root :is(h1,h2){color:red}.root:hover{color:blue}")


@pytest.mark.parametrize(
    "source",
    [
        "a { color: red;",
        'a { content: "unterminated; }',
        "a { color: red; /* unterminated",
        "a { color: red; }}",
        "a { content: \\",
    ],
)
def test_minify_css_rejects_unterminated_or_unbalanced_input(source: str) -> None:
    """The validation generator rejects unclosed strings, comments, escapes, and rule blocks."""
    with pytest.raises(ValueError):
        build_html_css.minify_css(source)


def test_build_html_css_check_detects_and_repairs_stale_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that check mode reports expired products without writing, and normal mode repairs them atomically."""
    source_path = tmp_path / "docvortex.css"
    output_path = tmp_path / "docvortex.min.css"
    source_path.write_text(".demo { color: red; }\n", encoding="utf-8")
    output_path.write_text("stale", encoding="utf-8")
    monkeypatch.setattr(build_html_css, "_SOURCE_PATH", source_path)
    monkeypatch.setattr(build_html_css, "_OUTPUT_PATH", output_path)

    assert build_html_css.build_html_css(check=True) is False
    assert output_path.read_text(encoding="utf-8") == "stale"
    assert build_html_css.build_html_css(check=False) is True
    assert output_path.read_text(encoding="utf-8") == ".demo{color:red}"
    assert build_html_css.build_html_css(check=True) is True
