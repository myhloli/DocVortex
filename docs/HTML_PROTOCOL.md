# DocVortex HTML v1

HTML is a semantic rendering of MiddleJson. Native HTML analysis first checks
for the canonical DocVortex protocol, then uses ordinary webpage selection and DOM
projection when the protocol is absent or invalid. It does not read product
metadata such as `effort`, `parse_mode` or `mineru_version`.

## Markers and structure

```html
<article class="docvortex-document docvortex-document--default"
         data-docvortex-html-version="1" data-render-mode="default">
  <div class="docvortex-block" data-page-idx="0"
       data-block-type="text" data-block-index="0">
    <p class="docvortex-text">Example</p>
  </div>
</article>
```

The decoder validates the version, root ownership, class sets, DOM hierarchy and
semantic attributes together. Renaming only the root class is insufficient.
`data-block-type` contains the raw BlockType value; optional metadata includes
`data-block-sub-type`, `data-guess-lang`, `data-anchor` and `data-level`.

DEFAULT is continuous reading output. FULL includes `section.docvortex-page` and
`hr.docvortex-page-break`, preserving page auxiliary blocks. HTML analysis still
produces one logical page; this is not an exact geometry or original-PDF round trip.

Formulas use `docvortex-math`, `docvortex-math--inline/block`,
`data-formula-display` and `data-docvortex-latex`. The LaTeX attribute preserves the
formula payload independently of visible MathJax delimiters. Style and runtime
hooks for headings, lists, notes, images, tables, code and Mermaid all use
`docvortex-*`. The bundled HTML CSS files are `docvortex.css` and `docvortex.min.css`.
Standalone HTML defaults to the title `DocVortex Document` when neither an explicit
title nor a document title block is available.

The codec exports `DOCVORTEX_HTML_VERSION`, `WireDecodeResult` and
`decode_docvortex_html_wire`. Invalid marked input retains the existing
`unsupported_version` or `non_canonical_wire` diagnostic. Ordinary input has no
exact result or fallback reason; a valid empty document has an empty exact result.

## Related outputs and migration

Visual caption and footnote paragraphs may carry `data-anchor` with the original
MiddleJson bookmark and an `id` allocated by the document anchor registry. This
applies to image, table, chart and code annotations. Internal inline and index
links use that `id`; the exact HTML codec restores the original bookmark on both
the annotation and its links. Empty descriptions do not emit an `id`, and a
duplicate bookmark links to the first visible target. This optional addition
keeps HTML protocol version 1 and does not add an anchor to the visual parent or
body carrier.

EPUB XHTML and CSS use DocVortex classes and `EPUB/styles/docvortex.css`; its default
title is also `DocVortex Document`. EPUB uses its own native parser and does not gain
the HTML v1 envelope merely by sharing the namespace. Markdown's embedded
footnote and algorithm HTML also uses DocVortex classes.

The former DocGale and MinerU marker namespaces and codec names are no longer recognized as
an exact HTML protocol, and no aliases or dual-namespace mode are provided.
Previously generated HTML remains readable through the ordinary webpage path,
without a guarantee of recovering its exact original semantic types.

Only implementation-owned markers change. User text, code, URLs, asset paths and
arbitrary embedded markup are not globally string-rewritten. DocVortex JSON and bundles are not modified on load. Old DocGale native JSON and
bundles are rejected because their schema identities differ; no implicit migration
is performed. MinerU JSON adapters continue to read their existing supported
formats. PDF/DOCX/LaTeX generated brand identifiers also use DocVortex, while
MinerU host UI markers remain independent.


## 可选物理来源映射

`render_html(..., include_source_map=True)` 或 `HtmlRenderOptions(include_source_map=True)` 为具有几何信息的可见节点输出来源属性，默认值为 `False`。

- `data-docvortex-trace-id` 是文档内唯一的目标标识，由原始页号、顶层块号和子块路径生成。
- `data-docvortex-sources` 是 JSON 数组。每条来源包含 `page_idx`、顶层 `block_index`、从该顶层块开始的 `child_path` 和 `[0, 1]` 归一化 `bbox`。
- 坐标以原始页面显示方向的可见页框左上角为原点。抽页后的查看器通过宿主提供的页映射定位原文页号。
- 续段、续列表和成功合并的续表累计多个来源。子节点使用合并前的原始路径，表格脚注按实际保留的页记录来源。
- 具有独立几何信息的节点生成独立目标，宿主可将其余子内容的点击委托给可定位祖先。

来源属性属于当前渲染的派生信息，HTML codec 按正文语义完成回读。宿主负责消息通信、滚动、区域绘制和文档生命周期。
