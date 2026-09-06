# DocGale HTML v1

HTML is a semantic rendering of MiddleJson. Native HTML analysis first checks
for the canonical DocGale protocol, then uses ordinary webpage selection and DOM
projection when the protocol is absent or invalid. It does not read product
metadata such as `effort`, `parse_mode` or `mineru_version`.

## Markers and structure

```html
<article class="docgale-document docgale-document--default"
         data-docgale-html-version="1" data-render-mode="default">
  <div class="docgale-block" data-page-idx="0"
       data-block-type="text" data-block-index="0">
    <p class="docgale-text">Example</p>
  </div>
</article>
```

The decoder validates the version, root ownership, class sets, DOM hierarchy and
semantic attributes together. Renaming only the root class is insufficient.
`data-block-type` contains the raw BlockType value; optional metadata includes
`data-block-sub-type`, `data-guess-lang`, `data-anchor` and `data-level`.

DEFAULT is continuous reading output. FULL includes `section.docgale-page` and
`hr.docgale-page-break`, preserving page auxiliary blocks. HTML analysis still
produces one logical page; this is not an exact geometry or original-PDF round trip.

Formulas use `docgale-math`, `docgale-math--inline/block`,
`data-formula-display` and `data-docgale-latex`. The LaTeX attribute preserves the
formula payload independently of visible MathJax delimiters. Style and runtime
hooks for headings, lists, notes, images, tables, code and Mermaid all use
`docgale-*`. The bundled HTML CSS files are `docgale.css` and `docgale.min.css`.
Standalone HTML defaults to the title `DocGale Document` when neither an explicit
title nor a document title block is available.

The codec exports `DOCGALE_HTML_VERSION`, `WireDecodeResult` and
`decode_docgale_html_wire`. Invalid marked input retains the existing
`unsupported_version` or `non_canonical_wire` diagnostic. Ordinary input has no
exact result or fallback reason; a valid empty document has an empty exact result.

## Related outputs and migration

EPUB XHTML and CSS use DocGale classes and `EPUB/styles/docgale.css`; its default
title is also `DocGale Document`. EPUB uses its own native parser and does not gain
the HTML v1 envelope merely by sharing the namespace. Markdown's embedded
footnote and algorithm HTML also uses DocGale classes.

The former MinerU marker namespace and codec names are no longer recognized as
an exact HTML protocol, and no aliases or dual-namespace mode are provided.
Previously generated HTML remains readable through the ordinary webpage path,
without a guarantee of recovering its exact original semantic types.

Only implementation-owned markers change. User text, code, URLs, asset paths and
arbitrary embedded markup are not globally string-rewritten. Existing JSON and
bundles remain valid and are not modified on load; re-rendering a complete saved
result produces the new markers without opening the source PDF. JSON adapters,
non-HTML PDF/DOCX/LaTeX identifiers and host UI markers remain independent.
