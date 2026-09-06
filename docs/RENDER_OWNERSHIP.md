# Rendering ownership

DocVortex provides seven general output formats: Markdown, HTML, LaTeX, DOCX,
EPUB, PDF and Structured Content. Its `RenderFormat`, API, CLI and result export
accept only these seven targets. Content List V1/V2 are implemented in MinerU;
there are no DocVortex forwarding modules or host imports.

MinerU keeps its existing nine-value `RenderFormat`, Content List options,
`render_content_list()` and `render_content_list_v2()` entrypoints. The first
returns a flat list; the second returns one list per page. Their field values,
geometry, ordering, resource precedence and configuration behavior are unchanged.
The `ContentType` and `ContentTypeV2` product constants now belong to `mineru.types`.

The two projects' `RenderFormat` classes are distinct. Import the enum belonging
to the API being called. Common options, `RenderMode`, asset interfaces and
document types remain shared DocVortex types; only product-specific contracts
are owned by MinerU.

## Shared fragment API

`docvortex.render.fragments` supplies the operations required by host renderers:

| Operation | Purpose |
| --- | --- |
| `render_inline_content` | Inline Markdown with explicit formula delimiters |
| `render_internal_link` | Escaped internal Markdown links |
| `resolve_image_source` | Existing image source precedence |
| `normalize_image_source` | Shared image path/URL normalization |
| `format_embedded_html` | Image prefixes and inline formulas in embedded HTML |
| `strip_index_page_tail` | Remove recognized index page-number tails |
| `parse_list_item_marker` | Parse list markers without losing inline styling |

`ListItem`, `ListItemKind` and `OrderedListStyle` describe list-parser results.
These functions delegate to the existing shared implementation rather than
maintaining copies. The public module is lightweight; renderer dependencies are
loaded on use. MinerU's Content List code imports only public DocVortex interfaces.

Restored DocVortex bundles can still be exported to the seven general formats.
For Content List output, pass the shared `MiddleJson` to MinerU and supply the
appropriate `asset_base_url`, as with its existing public API. JSON document
schemas, source parsing, fonts, normalization and saved bundle structure do not
change as part of this ownership split.
