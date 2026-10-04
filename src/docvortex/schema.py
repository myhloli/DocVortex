from __future__ import annotations

import json
import math
from copy import deepcopy
from enum import Enum
from typing import Annotated, Any, ClassVar, Literal, TypeAlias, TypeVar, Union, cast, get_args

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    SerializationInfo,
    SerializerFunctionWrapHandler,
    TypeAdapter,
    field_validator,
    model_serializer,
    model_validator,
)

from .foundation._hyperlink import OFFICE_EXTERNAL_HYPERLINK_SCHEMES, sanitize_hyperlink_target

# These strings are not exposed as Block.type discriminator and are only used in the raw stage or Block internal enumeration values.
RawBlockType: TypeAlias = Literal[
    "algorithm",
    "caption",
    "footnote",
    "formula_number",
    "phonetic",
]

RAW_ALGORITHM: RawBlockType = "algorithm"
RAW_CAPTION: RawBlockType = "caption"
RAW_FOOTNOTE: RawBlockType = "footnote"
RAW_FORMULA_NUMBER: RawBlockType = "formula_number"
RAW_PHONETIC: RawBlockType = "phonetic"

RAW_ONLY_BLOCK_TYPES = frozenset(
    {
        RAW_ALGORITHM,
        RAW_CAPTION,
        RAW_FOOTNOTE,
        RAW_FORMULA_NUMBER,
        RAW_PHONETIC,
    }
)

FileSuffix: TypeAlias = Literal[
    "pdf",
    "doc",
    "docx",
    "ppt",
    "pptx",
    "xls",
    "xlsx",
    "rtf",
    "csv",
    "tsv",
    "epub",
    "html",
    "mhtml",
    "ofd",
    "odt",
    "ods",
    "odp",
]
FILE_SUFFIXES: frozenset[FileSuffix] = frozenset(cast(tuple[FileSuffix, ...], get_args(FileSuffix)))


class BlockType(str, Enum):
    IMAGE = "image"
    IMAGE_BODY = "image_body"
    IMAGE_CAPTION = "image_caption"
    IMAGE_FOOTNOTE = "image_footnote"

    TABLE = "table"
    TABLE_BODY = "table_body"
    TABLE_CAPTION = "table_caption"
    TABLE_FOOTNOTE = "table_footnote"

    CHART = "chart"
    CHART_BODY = "chart_body"
    CHART_CAPTION = "chart_caption"
    CHART_FOOTNOTE = "chart_footnote"

    # Added in vlm 2.5
    CODE = "code"
    CODE_BODY = "code_body"
    ALGORITHM_BODY = "algorithm_body"
    CODE_CAPTION = "code_caption"
    CODE_FOOTNOTE = "code_footnote"

    TEXT = "text"
    EQUATION = "equation"  # Interline formula (stand-alone formula)
    LIST = "list"
    INDEX = "index"

    # Added in vlm 2.5
    REF_TEXT = "ref_text"
    HEADER = "header"
    FOOTER = "footer"
    PAGE_NUMBER = "page_number"
    ASIDE_TEXT = "aside_text"
    PAGE_FOOTNOTE = "page_footnote"

    # Added in pp_doclayout_v2
    DOC_TITLE = "doc_title"
    PARAGRAPH_TITLE = "paragraph_title"

    def __str__(self) -> str:
        return self.value


BlockTypes = Literal[
    BlockType.IMAGE,
    BlockType.TABLE,
    BlockType.CHART,
    BlockType.IMAGE_BODY,
    BlockType.TABLE_BODY,
    BlockType.CHART_BODY,
    BlockType.IMAGE_CAPTION,
    BlockType.TABLE_CAPTION,
    BlockType.CHART_CAPTION,
    BlockType.IMAGE_FOOTNOTE,
    BlockType.TABLE_FOOTNOTE,
    BlockType.CHART_FOOTNOTE,
    BlockType.TEXT,
    BlockType.EQUATION,
    BlockType.LIST,
    BlockType.INDEX,
    BlockType.CODE,
    BlockType.CODE_BODY,
    BlockType.ALGORITHM_BODY,
    BlockType.CODE_CAPTION,
    BlockType.CODE_FOOTNOTE,
    BlockType.REF_TEXT,
    BlockType.HEADER,
    BlockType.FOOTER,
    BlockType.PAGE_NUMBER,
    BlockType.ASIDE_TEXT,
    BlockType.PAGE_FOOTNOTE,
    BlockType.DOC_TITLE,
    BlockType.PARAGRAPH_TITLE,
]

PageBlockTypes = Literal[
    BlockType.IMAGE,
    BlockType.TABLE,
    BlockType.CHART,
    BlockType.TEXT,
    BlockType.EQUATION,
    BlockType.LIST,
    BlockType.INDEX,
    BlockType.CODE,
    BlockType.REF_TEXT,
    BlockType.HEADER,
    BlockType.FOOTER,
    BlockType.PAGE_NUMBER,
    BlockType.ASIDE_TEXT,
    BlockType.PAGE_FOOTNOTE,
    BlockType.DOC_TITLE,
    BlockType.PARAGRAPH_TITLE,
]

BLOCK_TYPES = {
    BlockType.IMAGE,
    BlockType.TABLE,
    BlockType.CHART,
    BlockType.IMAGE_BODY,
    BlockType.TABLE_BODY,
    BlockType.CHART_BODY,
    BlockType.IMAGE_CAPTION,
    BlockType.TABLE_CAPTION,
    BlockType.CHART_CAPTION,
    BlockType.IMAGE_FOOTNOTE,
    BlockType.TABLE_FOOTNOTE,
    BlockType.CHART_FOOTNOTE,
    BlockType.TEXT,
    BlockType.EQUATION,
    BlockType.LIST,
    BlockType.INDEX,
    BlockType.CODE,
    BlockType.CODE_BODY,
    BlockType.ALGORITHM_BODY,
    BlockType.CODE_CAPTION,
    BlockType.CODE_FOOTNOTE,
    BlockType.REF_TEXT,
    BlockType.HEADER,
    BlockType.FOOTER,
    BlockType.PAGE_NUMBER,
    BlockType.ASIDE_TEXT,
    BlockType.PAGE_FOOTNOTE,
    BlockType.DOC_TITLE,
    BlockType.PARAGRAPH_TITLE,
}

PAGE_BLOCK_TYPES = {
    BlockType.IMAGE,
    BlockType.TABLE,
    BlockType.CHART,
    BlockType.TEXT,
    BlockType.EQUATION,
    BlockType.LIST,
    BlockType.INDEX,
    BlockType.CODE,
    BlockType.REF_TEXT,
    BlockType.HEADER,
    BlockType.FOOTER,
    BlockType.PAGE_NUMBER,
    BlockType.ASIDE_TEXT,
    BlockType.PAGE_FOOTNOTE,
    BlockType.DOC_TITLE,
    BlockType.PARAGRAPH_TITLE,
}

# Page decoration and auxiliary text do not participate in the judgment of semantic boundaries between text, lists, and visual objects.
PAGE_AUXILIARY_BLOCK_TYPES = {
    BlockType.HEADER,
    BlockType.FOOTER,
    BlockType.PAGE_NUMBER,
    BlockType.ASIDE_TEXT,
}
# Page footers need to participate in the output, but will not block the judgment of relationships between text, lists, continuation tables, or visual objects.
MERGE_TRANSPARENT_BLOCK_TYPES = {
    *PAGE_AUXILIARY_BLOCK_TYPES,
    BlockType.PAGE_FOOTNOTE,
}
VISUAL_RELATION_IGNORED_TYPES = MERGE_TRANSPARENT_BLOCK_TYPES
VISUAL_MAIN_TYPES = {
    BlockType.IMAGE_BODY: BlockType.IMAGE,
    BlockType.TABLE_BODY: BlockType.TABLE,
    BlockType.CHART_BODY: BlockType.CHART,
    BlockType.CODE_BODY: BlockType.CODE,
}
VISUAL_TYPE_MAPPING = {
    BlockType.IMAGE: {
        "body": BlockType.IMAGE_BODY,
        "caption": BlockType.IMAGE_CAPTION,
        "footnote": BlockType.IMAGE_FOOTNOTE,
    },
    BlockType.TABLE: {
        "body": BlockType.TABLE_BODY,
        "caption": BlockType.TABLE_CAPTION,
        "footnote": BlockType.TABLE_FOOTNOTE,
    },
    BlockType.CHART: {
        "body": BlockType.CHART_BODY,
        "caption": BlockType.CHART_CAPTION,
        "footnote": BlockType.CHART_FOOTNOTE,
    },
    BlockType.CODE: {
        "body": BlockType.CODE_BODY,
        "caption": BlockType.CODE_CAPTION,
        "footnote": BlockType.CODE_FOOTNOTE,
    },
}
# ── model types ─────────────────────────────────────────────────────

BBox: TypeAlias = tuple[float, float, float, float]
IntBBox: TypeAlias = tuple[int, int, int, int]


def _remove_block_fields(value: Any, excluded_fields: set[str]) -> Any:
    """Recursively removes the specified block field from the serialization result, covering containers of arbitrary depth."""
    if isinstance(value, list):
        return [_remove_block_fields(item, excluded_fields) for item in value]
    if not isinstance(value, dict):
        return value

    result = {
        key: _remove_block_fields(item, excluded_fields)
        for key, item in value.items()
        if not ("type" in value and key in excluded_fields)
    }
    return result


class _StrictMiddleModel(BaseModel):
    """Model/Middle JSON Strict model base class, providing a unified serialization entry without side effects."""

    model_config = ConfigDict(extra="forbid", strict=True, validate_assignment=True)

    def to_dict(
        self,
        *,
        skip_defaults: bool = True,
        exclude_none: bool = False,
        exclude_block_fields: set[str] | None = None,
    ) -> dict[str, Any]:
        """Serialize the object and recursively exclude block fields at any level by field name."""
        payload = self.model_dump(
            mode="json",
            exclude_defaults=skip_defaults,
            exclude_none=exclude_none,
        )
        if exclude_block_fields:
            payload = _remove_block_fields(payload, set(exclude_block_fields))
        return payload

    def to_json(
        self,
        *,
        skip_defaults: bool = True,
        exclude_none: bool = False,
        exclude_block_fields: set[str] | None = None,
        indent: int | None = 4,
    ) -> str:
        """Encode the object into a UTF-8-friendly JSON string, without performing image file writing."""
        return json.dumps(
            self.to_dict(
                skip_defaults=skip_defaults,
                exclude_none=exclude_none,
                exclude_block_fields=exclude_block_fields,
            ),
            ensure_ascii=False,
            indent=indent,
        )


InlineStyle: TypeAlias = Literal[
    "bold",
    "italic",
    "underline",
    "emphasis",
    "strikethrough",
    "superscript",
    "subscript",
]

INLINE_STYLE_ORDER: tuple[InlineStyle, ...] = (
    "bold",
    "italic",
    "underline",
    "emphasis",
    "strikethrough",
    "superscript",
    "subscript",
)


class TextSpan(_StrictMiddleModel):
    """Saves normal inline text and its visible font style."""

    type: Literal["text"]
    content: str = Field(min_length=1)
    styles: list[InlineStyle] = Field(default_factory=list)

    @field_validator("styles")
    @classmethod
    def _normalize_styles(cls, value: list[InlineStyle]) -> list[InlineStyle]:
        """Deduplicate styles in a fixed public order, and prohibit simultaneous declaration of superscripts and subscripts."""
        unique = set(value)
        if "superscript" in unique and "subscript" in unique:
            raise ValueError("text span cannot be both superscript and subscript")
        return [style for style in INLINE_STYLE_ORDER if style in unique]


class EquationInlineSpan(_StrictMiddleModel):
    """Save inline LaTeX without outer delimiter."""

    type: Literal["equation_inline"]
    content: str = Field(min_length=1)

    @field_validator("content")
    @classmethod
    def _validate_content(cls, value: str) -> str:
        """Reject inline formulas that contain only whitespace while preserving the original whitespace of the formula."""
        if not value.strip():
            raise ValueError("inline equation content must not be blank")
        return value


class CodeInlineSpan(_StrictMiddleModel):
    """Save inline code that needs to be displayed literally."""

    type: Literal["code_inline"]
    content: str = Field(min_length=1)


NonLinkInlineSpan: TypeAlias = Annotated[
    Union[TextSpan, EquationInlineSpan, CodeInlineSpan],
    Field(discriminator="type"),
]


class HyperlinkSpan(_StrictMiddleModel):
    """Saves safe hyperlink targets and their non-linked inline child nodes."""

    type: Literal["hyperlink"]
    url: str = Field(min_length=1)
    content: list[NonLinkInlineSpan] = Field(min_length=1)

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        """Reuse unified policies to deny dangerous protocols, local paths, malformed URL, and control characters."""
        normalized = sanitize_hyperlink_target(
            value,
            allowed_schemes=OFFICE_EXTERNAL_HYPERLINK_SCHEMES,
            allow_relative=True,
            allow_fragment=True,
        )
        if normalized is None:
            raise ValueError("hyperlink span url is unsafe or malformed")
        return normalized


InlineSpan: TypeAlias = Annotated[
    Union[TextSpan, EquationInlineSpan, CodeInlineSpan, HyperlinkSpan],
    Field(discriminator="type"),
]

INLINE_SPAN_ADAPTER = TypeAdapter(InlineSpan)
INLINE_SPAN_LIST_ADAPTER = TypeAdapter(list[InlineSpan])


def _normalize_typed_inline_spans(spans: list[InlineSpan]) -> list[InlineSpan]:
    """Recursively merge adjacent text of the same style and adjacent links with the same target."""
    normalized: list[InlineSpan] = []
    for span in spans:
        current: InlineSpan
        if isinstance(span, HyperlinkSpan):
            children = _normalize_typed_inline_spans(list(span.content))
            non_link_children = [child for child in children if not isinstance(child, HyperlinkSpan)]
            if not non_link_children:
                continue
            current = span.model_copy(update={"content": non_link_children}, deep=True)
        else:
            current = span.model_copy(deep=True)
        if (
            normalized
            and isinstance(normalized[-1], TextSpan)
            and isinstance(current, TextSpan)
            and normalized[-1].styles == current.styles
        ):
            previous = normalized[-1]
            normalized[-1] = previous.model_copy(update={"content": f"{previous.content}{current.content}"})
            continue
        if (
            normalized
            and isinstance(normalized[-1], HyperlinkSpan)
            and isinstance(current, HyperlinkSpan)
            and normalized[-1].url == current.url
        ):
            previous_link = normalized[-1]
            merged_children = _normalize_typed_inline_spans([*previous_link.content, *current.content])
            normalized[-1] = previous_link.model_copy(update={"content": merged_children})
            continue
        normalized.append(current)
    return normalized


def parse_inline_span(value: Any) -> InlineSpan:
    """Resolve a dictionary or existing model strictly as a public inline Span."""
    return INLINE_SPAN_ADAPTER.validate_python(value)


def parse_inline_spans(value: Any) -> list[InlineSpan]:
    """Strictly parse and normalize the complete inline Span list."""
    return _normalize_typed_inline_spans(INLINE_SPAN_LIST_ADAPTER.validate_python(value))


class BlockBase(_StrictMiddleModel):
    """Minimum common field for all public Middle JSON block."""

    type: BlockTypes
    index: int | None = Field(default=None, ge=0)
    bbox: BBox | None = None

    @field_validator("bbox", mode="before")
    @classmethod
    def _validate_bbox(cls, value: Any) -> BBox | None:
        """Accept bbox in the form of JSON array, and strictly verify the normalized coordinates."""
        if value is None:
            return None
        if not isinstance(value, (list, tuple)) or len(value) != 4:
            raise ValueError("bbox must contain exactly four numbers")
        if any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in value):
            raise ValueError("bbox values must be numbers")
        bbox = tuple(float(item) for item in value)
        if not all(math.isfinite(item) and 0.0 <= item <= 1.0 for item in bbox):
            raise ValueError("bbox values must be finite normalized coordinates")
        if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
            raise ValueError("bbox must satisfy x1 > x0 and y1 > y0")
        return bbox  # type: ignore[return-value]


class StringContentBlock(BlockBase):
    """A shared structure for all string contents block."""

    content: str


class InlineContentBlock(BlockBase):
    """Shared structure for all structured inline content block."""

    content: list[InlineSpan]

    @field_validator("content")
    @classmethod
    def _normalize_content(cls, value: list[InlineSpan]) -> list[InlineSpan]:
        """Merging adjacent synonyms at strict object boundaries Span."""
        return _normalize_typed_inline_spans(value)


class ContinuableTextBlockBase(InlineContentBlock):
    """A cross-block continuation structure shared between text and references."""

    continues_prev: bool | None = None


class TextBlock(ContinuableTextBlockBase):
    type: Literal[BlockType.TEXT]  # type: ignore[reportIncompatibleVariableOverride]
    anchor: str | None = None


class RefTextBlock(ContinuableTextBlockBase):
    type: Literal[BlockType.REF_TEXT]  # type: ignore[reportIncompatibleVariableOverride]


class TitleBlockBase(InlineContentBlock):
    """A global hierarchical common structure for document titles and paragraph headings."""

    anchor: str | None = None
    level: int


class DocTitleBlock(TitleBlockBase):
    type: Literal[BlockType.DOC_TITLE]  # type: ignore[reportIncompatibleVariableOverride]
    level: int = Field(ge=1, le=1)


class ParagraphTitleBlock(TitleBlockBase):
    type: Literal[BlockType.PARAGRAPH_TITLE]  # type: ignore[reportIncompatibleVariableOverride]
    level: int = Field(ge=2, le=6)


class PageAuxTextBlock(InlineContentBlock):
    """Shared text structure for headers, footers, page numbers, and sidebars."""

    type: Literal[  # type: ignore[reportIncompatibleVariableOverride]
        BlockType.HEADER,
        BlockType.FOOTER,
        BlockType.PAGE_NUMBER,
        BlockType.ASIDE_TEXT,
    ]


class PageFootnoteBlock(InlineContentBlock):
    """Save page footnotes that need to participate in the default output and can be referenced by links within the document."""

    type: Literal[BlockType.PAGE_FOOTNOTE]  # type: ignore[reportIncompatibleVariableOverride]
    anchor: str | None = None


class ImagePayloadBlock(BlockBase):
    """The block base class uniformly carries the pictures of sidecar, data, URI or remote URL."""

    image_base64: str | None = Field(default=None, repr=False)
    image_path: str | None = None
    image_url: str | None = None

    @field_validator("image_path")
    @classmethod
    def _validate_image_path(cls, value: str | None) -> str | None:
        """Verify that the recorded image path can only be a safe POSIX relative path."""
        if value is None:
            return None
        from .foundation._image_payload import validate_image_sidecar_path

        return validate_image_sidecar_path(value)

    @field_validator("image_url")
    @classmethod
    def _validate_image_url(cls, value: str | None) -> str | None:
        """Verify remote image URL, prohibit active protocols, relative addresses and embedded credentials."""
        if value is None:
            return None
        from .foundation._image_payload import validate_remote_image_url

        return validate_remote_image_url(value)


class ImagePayloadContentBlock(ImagePayloadBlock):
    """block structure that uniformly carries string content and image payload."""

    content: str


class EquationBlock(ImagePayloadContentBlock):
    type: Literal[BlockType.EQUATION]  # type: ignore[reportIncompatibleVariableOverride]


class ImageBodyBlock(ImagePayloadContentBlock):
    type: Literal[BlockType.IMAGE_BODY]  # type: ignore[reportIncompatibleVariableOverride]


class TableBodyBlock(ImagePayloadContentBlock):
    type: Literal[BlockType.TABLE_BODY]  # type: ignore[reportIncompatibleVariableOverride]


class ChartBodyBlock(ImagePayloadContentBlock):
    type: Literal[BlockType.CHART_BODY]  # type: ignore[reportIncompatibleVariableOverride]


class CodeBodyBlock(StringContentBlock):
    type: Literal[BlockType.CODE_BODY]  # type: ignore[reportIncompatibleVariableOverride]


class AlgorithmBodyBlock(InlineContentBlock):
    """Save preformatted algorithm text and inline formulas Span."""

    type: Literal[BlockType.ALGORITHM_BODY]  # type: ignore[reportIncompatibleVariableOverride]


class ImageAnnotationBlock(InlineContentBlock):
    """A shared structure for image titles and image footers."""

    type: Literal[BlockType.IMAGE_CAPTION, BlockType.IMAGE_FOOTNOTE]  # type: ignore[reportIncompatibleVariableOverride]


class TableAnnotationBlock(InlineContentBlock):
    """Shared structure for table titles and table footers."""

    type: Literal[BlockType.TABLE_CAPTION, BlockType.TABLE_FOOTNOTE]  # type: ignore[reportIncompatibleVariableOverride]


class ChartAnnotationBlock(InlineContentBlock):
    """Shared structure for chart titles and chart footers."""

    type: Literal[BlockType.CHART_CAPTION, BlockType.CHART_FOOTNOTE]  # type: ignore[reportIncompatibleVariableOverride]


class CodeAnnotationBlock(InlineContentBlock):
    """Shared structure of code titles and code footnotes."""

    type: Literal[BlockType.CODE_CAPTION, BlockType.CODE_FOOTNOTE]  # type: ignore[reportIncompatibleVariableOverride]


ListChildBlock: TypeAlias = Annotated[
    Union[TextBlock, RefTextBlock, "ListBlock"],
    Field(discriminator="type"),
]


class ListBlock(BlockBase):
    type: Literal[BlockType.LIST]  # type: ignore[reportIncompatibleVariableOverride]
    content: list[ListChildBlock]
    sub_type: Literal[BlockType.TEXT, BlockType.REF_TEXT] | None = None
    continues_prev: bool | None = None


IndexChildBlock: TypeAlias = Annotated[
    Union[TextBlock, DocTitleBlock, ParagraphTitleBlock, "IndexBlock"],
    Field(discriminator="type"),
]


class IndexBlock(BlockBase):
    type: Literal[BlockType.INDEX]  # type: ignore[reportIncompatibleVariableOverride]
    content: list[IndexChildBlock]


class _VisualBlockBase(BlockBase):
    """Shared structural constraints for the visual parent block."""

    _body_types: ClassVar[tuple[str, ...]]

    @model_validator(mode="after")
    def _validate_visual_children(self) -> _VisualBlockBase:
        """Verify that the visual parent block has only one body, and the parent-child positioning fields are consistent."""
        children = getattr(self, "content", [])
        bodies = [child for child in children if child.type in self._body_types]
        if len(bodies) != 1:
            expected = "/".join(str(item) for item in self._body_types)
            raise ValueError(f"{self.type} must contain exactly one {expected}")
        body = bodies[0]
        if self.index is not None and body.index != self.index:
            raise ValueError(f"{self.type} body index must equal parent index")
        if self.bbox is not None and body.bbox is not None and body.bbox != self.bbox:
            raise ValueError(f"{self.type} body bbox must equal parent bbox")
        return self


ImageChildBlock: TypeAlias = Annotated[
    Union[ImageBodyBlock, ImageAnnotationBlock],
    Field(discriminator="type"),
]


class ImageBlock(_VisualBlockBase):
    type: Literal[BlockType.IMAGE]  # type: ignore[reportIncompatibleVariableOverride]
    content: list[ImageChildBlock]
    sub_type: str | None = None
    _body_types: ClassVar[tuple[str, ...]] = (BlockType.IMAGE_BODY,)


TableChildBlock: TypeAlias = Annotated[
    Union[TableBodyBlock, TableAnnotationBlock],
    Field(discriminator="type"),
]


class TableBlock(_VisualBlockBase):
    type: Literal[BlockType.TABLE]  # type: ignore[reportIncompatibleVariableOverride]
    content: list[TableChildBlock]
    continues_prev: bool | None = None
    cell_merge: list[Literal[0, 1]] | None = None
    _body_types: ClassVar[tuple[str, ...]] = (BlockType.TABLE_BODY,)


ChartChildBlock: TypeAlias = Annotated[
    Union[ChartBodyBlock, ChartAnnotationBlock],
    Field(discriminator="type"),
]


class ChartBlock(_VisualBlockBase):
    type: Literal[BlockType.CHART]  # type: ignore[reportIncompatibleVariableOverride]
    content: list[ChartChildBlock]
    sub_type: str | None = None
    _body_types: ClassVar[tuple[str, ...]] = (BlockType.CHART_BODY,)


CodeChildBlock: TypeAlias = Annotated[
    Union[CodeBodyBlock, AlgorithmBodyBlock, CodeAnnotationBlock],
    Field(discriminator="type"),
]


class CodeBlock(_VisualBlockBase):
    type: Literal[BlockType.CODE]  # type: ignore[reportIncompatibleVariableOverride]
    content: list[CodeChildBlock]
    sub_type: Literal[BlockType.CODE, RAW_ALGORITHM]
    guess_lang: str | None = None
    _body_types: ClassVar[tuple[str, ...]] = (BlockType.CODE_BODY, BlockType.ALGORITHM_BODY)

    @model_validator(mode="after")
    def _validate_language(self) -> CodeBlock:
        """Code blocks require language, and algorithm blocks are prohibited from carrying code language guess results."""
        body = next(child for child in self.content if child.type in self._body_types)
        if self.sub_type == BlockType.CODE:
            if body.type != BlockType.CODE_BODY:
                raise ValueError("code block must contain code_body")
            if not isinstance(self.guess_lang, str) or not self.guess_lang.strip():
                raise ValueError("code block must contain a non-empty guess_lang")
        else:
            if body.type != BlockType.ALGORITHM_BODY:
                raise ValueError("algorithm block must contain algorithm_body")
            if self.guess_lang is not None:
                raise ValueError("algorithm block must not contain guess_lang")
        return self


ListBlock.model_rebuild()
IndexBlock.model_rebuild()


PageBlock: TypeAlias = Annotated[
    Union[
        TextBlock,
        RefTextBlock,
        DocTitleBlock,
        ParagraphTitleBlock,
        PageAuxTextBlock,
        PageFootnoteBlock,
        EquationBlock,
        ListBlock,
        IndexBlock,
        ImageBlock,
        TableBlock,
        ChartBlock,
        CodeBlock,
    ],
    Field(discriminator="type"),
]


Block: TypeAlias = Annotated[
    Union[
        TextBlock,
        RefTextBlock,
        DocTitleBlock,
        ParagraphTitleBlock,
        PageAuxTextBlock,
        PageFootnoteBlock,
        EquationBlock,
        ListBlock,
        IndexBlock,
        ImageBodyBlock,
        ImageAnnotationBlock,
        ImageBlock,
        TableBodyBlock,
        TableAnnotationBlock,
        TableBlock,
        ChartBodyBlock,
        ChartAnnotationBlock,
        ChartBlock,
        CodeBodyBlock,
        AlgorithmBodyBlock,
        CodeAnnotationBlock,
        CodeBlock,
    ],
    Field(discriminator="type"),
]

BLOCK_ADAPTER = TypeAdapter(Block)


def parse_block(value: Any) -> Block:
    """Strictly parse the dictionary or existing model into the corresponding specific Block type."""
    return BLOCK_ADAPTER.validate_python(value)


def _iter_child_blocks(block: BlockBase) -> list[BlockBase]:
    """Returns the immediate child chunks of container block, leaves block returns an empty list."""
    content = getattr(block, "content", None)
    if not isinstance(content, list):
        return []
    return [child for child in content if isinstance(child, BlockBase)]


_RAW_INLINE_CONTENT_TYPES = {
    BlockType.TEXT,
    BlockType.REF_TEXT,
    BlockType.DOC_TITLE,
    BlockType.PARAGRAPH_TITLE,
    BlockType.HEADER,
    BlockType.FOOTER,
    BlockType.PAGE_NUMBER,
    BlockType.ASIDE_TEXT,
    BlockType.PAGE_FOOTNOTE,
    BlockType.IMAGE_CAPTION,
    BlockType.IMAGE_FOOTNOTE,
    BlockType.TABLE_CAPTION,
    BlockType.TABLE_FOOTNOTE,
    BlockType.CHART_CAPTION,
    BlockType.CHART_FOOTNOTE,
    BlockType.CODE_CAPTION,
    BlockType.CODE_FOOTNOTE,
    RAW_ALGORITHM,
    RAW_CAPTION,
    RAW_FOOTNOTE,
    RAW_PHONETIC,
}


def _looks_like_raw_inline_span_list(content: list[Any]) -> bool:
    """Distinguish Span payloads of PDF flattened LIST/INDEX from treed text sub-blocks."""
    if not content:
        return False
    for item in content:
        if not isinstance(item, dict):
            return False
        span_type = item.get("type")
        span_content = item.get("content")
        if span_type in {"text", "equation_inline", "code_inline"} and isinstance(span_content, str):
            continue
        if span_type == "hyperlink" and isinstance(span_content, list) and isinstance(item.get("url"), str):
            continue
        return False
    return True


def _validate_raw_block_inline_content(block: dict[str, Any], *, location: str) -> None:
    """Recursive check raw Natural language content for block has been switched to a list of Span."""
    block_type = block.get("type")
    content = block.get("content")
    if block_type in _RAW_INLINE_CONTENT_TYPES:
        if not isinstance(content, list):
            raise ValueError(f"ModelJson inline content must be a span list: {location}, type={block_type}")
        try:
            parse_inline_spans(content)
        except ValueError as exc:
            raise ValueError(f"Invalid ModelJson inline spans: {location}, type={block_type}: {exc}") from exc
        return
    if block_type not in {BlockType.LIST, BlockType.INDEX} or not isinstance(content, list):
        return
    if _looks_like_raw_inline_span_list(content):
        try:
            parse_inline_spans(content)
        except ValueError as exc:
            raise ValueError(f"Invalid ModelJson inline spans: {location}, type={block_type}: {exc}") from exc
        return
    for child_index, child in enumerate(content):
        if isinstance(child, dict):
            _validate_raw_block_inline_content(child, location=f"{location}.content[{child_index}]")


class Producer(_StrictMiddleModel):
    """Document language-agnostic document producers to avoid binding host product metadata."""

    name: str = Field(min_length=1)
    version: str = Field(min_length=1)


class DocumentProperties(_StrictMiddleModel):
    """Saves the properties declared by the source file; the count belongs to the complete source file rather than the current parsed selection."""

    title: str | None = None
    authors: list[str] = Field(default_factory=list)
    subject: str | None = None
    keywords: list[str] = Field(default_factory=list)
    description: str | None = None
    languages: list[str] = Field(default_factory=list)
    identifiers: list[str] = Field(default_factory=list)
    publisher: str | None = None
    created_at: str | None = None
    modified_at: str | None = None
    creator_application: str | None = None
    producer_application: str | None = None
    page_count: int | None = Field(default=None, ge=0)
    page_count_kind: Literal["physical", "declared", "slide", "sheet", "spine", "logical"] | None = None

    @field_validator("authors", "keywords", "languages", "identifiers")
    @classmethod
    def _normalize_values(cls, values: list[str]) -> list[str]:
        """Remove whitespace items and robust deduplication without guessing delimiters in individual strings."""
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))


class DocumentMetadata(_StrictMiddleModel):
    """It carries the document format and real producer, and does not modify the source information when reading."""

    file_suffix: FileSuffix
    producer: Producer
    document: DocumentProperties | None = None


def _require_document_wire_identity(schema: dict[str, Any]) -> None:
    """JSON documents must explicitly carry the protocol identity; the constructor's constant default value only facilitates Python calls."""
    identity = "schema" if "schema" in schema["properties"] else "schema_id"
    schema["required"] = list(dict.fromkeys([identity, "schema_version", *schema.get("required", [])]))


_DocumentT = TypeVar("_DocumentT", bound="DocumentModel")


class DocumentModel(_StrictMiddleModel):
    """Public document encapsulation, only holds producer and serializable extension information."""

    model_config = ConfigDict(serialize_by_alias=True, json_schema_extra=_require_document_wire_identity)

    metadata: DocumentMetadata
    extensions: dict[str, JsonValue] = Field(default_factory=dict)
    schema_version: Literal["2.0"] = "2.0"
    schema_id: str = Field(alias="schema")

    def to_dict(
        self,
        *,
        skip_defaults: bool = True,
        exclude_none: bool = False,
        exclude_block_fields: set[str] | None = None,
    ) -> dict[str, Any]:
        """Omit block fields only for page trees, protecting sources and application extensions with keys of the same name."""
        payload = super().to_dict(skip_defaults=skip_defaults, exclude_none=exclude_none)
        if exclude_block_fields and "pages" in payload:
            payload["pages"] = _remove_block_fields(payload["pages"], exclude_block_fields)
        return payload

    @model_serializer(mode="wrap")
    def _serialize_document(self, handler: SerializerFunctionWrapHandler, info: SerializationInfo):
        """Preserves declared protocol default fields while respecting the caller's explicit field filtering."""
        # Do not declare the generic dict return type to avoid Pydantic reducing the serialized Schema to an arbitrary object.
        payload = handler(self)
        use_alias = info.by_alias is not False
        for field_name in ("schema_id", "schema_version", "extensions"):
            if info.exclude and field_name in info.exclude:
                continue
            if info.include is not None and field_name not in info.include:
                continue
            key = "schema" if field_name == "schema_id" and use_alias else field_name
            if key not in payload:
                payload[key] = deepcopy(getattr(self, field_name))
        return payload

    @classmethod
    def from_dict(cls: type[_DocumentT], value: dict[str, Any]) -> _DocumentT:
        """The document is read after jointly verifying the protocol identity and version, without guessing or migrating historical formats."""
        expected_schema = cls.model_fields["schema_id"].default
        expected_version = cls.model_fields["schema_version"].default
        if (
            not isinstance(value, dict)
            or value.get("schema") != expected_schema
            or value.get("schema_version") != expected_version
        ):
            raise ValueError(f"Expected {expected_schema} schema version {expected_version}; reparse the source document")
        return cls.model_validate(value)

    @classmethod
    def from_json(cls: type[_DocumentT], value: str | bytes) -> _DocumentT:
        """Recover shared documents from JSON text and reuse unique protocol check entries."""
        return cls.from_dict(json.loads(value))


class ModelJson(DocumentModel):
    """Analyze Returns the complete strict Model JSON object."""

    schema_id: Literal["docvortex.model"] = Field(default="docvortex.model", alias="schema")
    pages: list[list[dict[str, Any]]]
    page_index_map: list[int]

    @model_validator(mode="after")
    def _validate_page_index_map(self) -> ModelJson:
        """Verify explicit page mapping and Span contracts for each raw text block."""
        if self.page_index_map:
            if len(self.page_index_map) != len(self.pages):
                raise ValueError(f"page_index_map length mismatch: pages={len(self.pages)}, mapping={len(self.page_index_map)}")
            if any(page_idx < 0 for page_idx in self.page_index_map):
                raise ValueError("page_index_map values must be non-negative integers")
            if len(self.page_index_map) != len(set(self.page_index_map)):
                raise ValueError("page_index_map values must be unique")
            if any(current <= previous for previous, current in zip(self.page_index_map, self.page_index_map[1:])):
                raise ValueError("page_index_map values must preserve strictly increasing order")
        for page_index, page in enumerate(self.pages):
            for block_index, block in enumerate(page):
                if isinstance(block, dict):
                    _validate_raw_block_inline_content(block, location=f"pages[{page_index}][{block_index}]")
        return self

    @property
    def is_full_document(self) -> bool:
        """Returns the current Model. JSON indicates whether the entire document is parsed."""
        return not self.page_index_map

    @property
    def resolved_page_indices(self) -> list[int]:
        """Returns an explicit page map or a sequential page number copy of the entire document."""
        if self.is_full_document:
            return list(range(len(self.pages)))
        return list(self.page_index_map)


class PageInfo(_StrictMiddleModel):
    """One page of strictly Middle JSON content."""

    page_idx: int = Field(ge=0)
    blocks: list[PageBlock] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_page_tree(self) -> PageInfo:
        """Verify top-level index sequence and disable nested blocks from carrying cross-block continuation markers."""
        indices: list[int] = []
        for block in self.blocks:
            if block.index is None:
                raise ValueError("top-level block index is required")
            indices.append(block.index)
        if len(indices) != len(set(indices)):
            raise ValueError("top-level block indices must be unique")
        if any(current <= previous for previous, current in zip(indices, indices[1:])):
            raise ValueError("top-level block indices must be strictly increasing")

        pending = [child for block in self.blocks for child in _iter_child_blocks(block)]
        while pending:
            child = pending.pop()
            if "continues_prev" in child.model_fields_set:
                raise ValueError("nested blocks must not contain continues_prev")
            pending.extend(_iter_child_blocks(child))
        return self


class MiddleJson(DocumentModel):
    """Analyze Returns the complete strict Middle JSON object."""

    schema_id: Literal["docvortex.middle"] = Field(default="docvortex.middle", alias="schema")
    pages: list[PageInfo]
    is_full_document: bool

    @model_validator(mode="after")
    def _validate_document(self) -> MiddleJson:
        """Verify that page numbers are uniquely ordered and require that the top-level block of a fixed-layout document all have bbox."""
        page_indices = [page.page_idx for page in self.pages]
        if len(page_indices) != len(set(page_indices)):
            raise ValueError("page_idx values must be unique")
        if any(current <= previous for previous, current in zip(page_indices, page_indices[1:])):
            raise ValueError("page_idx values must be strictly increasing")
        if self.metadata.file_suffix in {"pdf", "ofd"}:
            for page in self.pages:
                for block in page.blocks:
                    if block.bbox is None:
                        raise ValueError(
                            f"Fixed-layout top-level block requires bbox: "
                            f"file_suffix={self.metadata.file_suffix}, page_idx={page.page_idx}, index={block.index}"
                        )
        return self


__all__ = [
    "DocumentMetadata",
    "DocumentProperties",
    "RawBlockType",
    "RAW_ALGORITHM",
    "RAW_CAPTION",
    "RAW_FOOTNOTE",
    "RAW_FORMULA_NUMBER",
    "RAW_PHONETIC",
    "RAW_ONLY_BLOCK_TYPES",
    "FileSuffix",
    "FILE_SUFFIXES",
    "BlockType",
    "BlockTypes",
    "PageBlockTypes",
    "BLOCK_TYPES",
    "PAGE_BLOCK_TYPES",
    "PAGE_AUXILIARY_BLOCK_TYPES",
    "MERGE_TRANSPARENT_BLOCK_TYPES",
    "VISUAL_RELATION_IGNORED_TYPES",
    "VISUAL_MAIN_TYPES",
    "VISUAL_TYPE_MAPPING",
    "BBox",
    "IntBBox",
    "InlineStyle",
    "INLINE_STYLE_ORDER",
    "TextSpan",
    "EquationInlineSpan",
    "CodeInlineSpan",
    "NonLinkInlineSpan",
    "HyperlinkSpan",
    "InlineSpan",
    "INLINE_SPAN_ADAPTER",
    "INLINE_SPAN_LIST_ADAPTER",
    "parse_inline_span",
    "parse_inline_spans",
    "BlockBase",
    "StringContentBlock",
    "InlineContentBlock",
    "ContinuableTextBlockBase",
    "TextBlock",
    "RefTextBlock",
    "TitleBlockBase",
    "DocTitleBlock",
    "ParagraphTitleBlock",
    "PageAuxTextBlock",
    "PageFootnoteBlock",
    "ImagePayloadBlock",
    "ImagePayloadContentBlock",
    "EquationBlock",
    "ImageBodyBlock",
    "TableBodyBlock",
    "ChartBodyBlock",
    "CodeBodyBlock",
    "AlgorithmBodyBlock",
    "ImageAnnotationBlock",
    "TableAnnotationBlock",
    "ChartAnnotationBlock",
    "CodeAnnotationBlock",
    "ListChildBlock",
    "ListBlock",
    "IndexChildBlock",
    "IndexBlock",
    "ImageChildBlock",
    "ImageBlock",
    "TableChildBlock",
    "TableBlock",
    "ChartChildBlock",
    "ChartBlock",
    "CodeChildBlock",
    "CodeBlock",
    "PageBlock",
    "Block",
    "BLOCK_ADAPTER",
    "parse_block",
    "Producer",
    "DocumentModel",
    "ModelJson",
    "PageInfo",
    "MiddleJson",
]
