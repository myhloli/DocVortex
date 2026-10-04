"""Flash The internal data model used by native PDF extraction."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, MutableSet
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal
import re

from ....document.pdf._document import PDFPathInfo
from ....document.pdf.text._contracts import Char
from ....schema import BBox

if TYPE_CHECKING:
    from ....document.pdf.form_structure import _PDFFormInfo
    from .inline.types import PDFTextScriptLine


class _SharedLineIndexSet(MutableSet[int]):
    """A shared read-only base is used to only add and delete a single candidate record to avoid copying large row number sets."""

    __slots__ = ("base", "added", "removed")

    def __init__(
        self,
        base: frozenset[int],
        values: Iterable[int] = (),
    ) -> None:
        """Initialize the collection with a shared base and candidate existing members."""

        self.base = base
        self.removed: set[int] = set()
        if isinstance(values, _SharedLineIndexSet) and values.base is base:
            # When performing a union with a shared base, all base members are naturally visible and only the extra members need to be retained.
            self.added = set(values.added)
        elif isinstance(values, (set, frozenset)):
            self.added = values.difference(base)
        else:
            self.added = {value for value in values if value not in base}

    @classmethod
    def from_exact_values(
        cls,
        base: frozenset[int],
        values: Iterable[int],
    ) -> _SharedLineIndexSet:
        """Exactly represent the given set in terms of a shared base, rather than merging all base members by default."""

        visible = set(values)
        instance = cls.__new__(cls)
        instance.base = base
        instance.added = visible.difference(base)
        instance.removed = set(base.difference(visible))
        return instance

    def __contains__(self, value: object) -> bool:
        """Determine whether members are visible based on the base, deleted set and new set."""

        if value in self.added:
            return True
        return value in self.base and value not in self.removed

    def __iter__(self) -> Iterator[int]:
        """Iterates over currently visible members without expanding persistent copies."""

        for value in self.base:
            if value not in self.removed:
                yield value
        yield from self.added

    def __len__(self) -> int:
        """Returns the current number of visible members."""

        return len(self.base) - len(self.removed) + len(self.added)

    def add(self, value: int) -> None:
        """Add members; existing members only need to unmark the deletion mark."""

        if value in self.base:
            self.removed.discard(value)
            return
        self.added.add(value)

    def discard(self, value: int) -> None:
        """Remove members; base members are represented by difference markers."""

        if value in self.added:
            self.added.discard(value)
            return
        if value in self.base:
            self.removed.add(value)

    def update(self, values: Iterable[int]) -> None:
        """Merge members; directly merge difference sets with candidates that share a base."""

        if isinstance(values, _SharedLineIndexSet) and values.base is self.base:
            self.removed.intersection_update(values.removed)
            for value in values.added:
                self.add(value)
            return
        for value in values:
            self.add(value)

    def difference_update(self, values: Iterable[int]) -> None:
        """Removes the given member, leaving the shared base unchanged."""

        if isinstance(values, (set, frozenset)):
            self.added.difference_update(values)
            self.removed.update(self.base.intersection(values))
            return
        for value in values:
            self.discard(value)


@dataclass(slots=True)
class _LineItem:
    """Saves a single visual text line and its source/ink/canonical geometry."""

    text: str
    bbox: BBox
    angle: int
    source_index: int
    source_bbox: BBox | None = None
    ink_bbox: BBox | None = None
    baseline: float | None = None
    em_height: float = 0.0
    geometry_state: Literal["healthy", "repair_x", "trim_y", "repair_xy", "uncertain"] = "healthy"
    geometry_confidence: float = 1.0
    split_y_candidate: bool = False
    chars: list[Char] = field(default_factory=list)
    visual_row_id: int | None = None
    run_index: int = 0
    effective_height: float = 0.0
    font_signature: tuple[str, int] | None = None
    font_coverage: float = 0.0
    dominant_font_weight: float | None = None
    median_glyph_width: float | None = None
    leading_emphasis_width: float | None = None
    leading_typography_width: float | None = None
    split_from_row: bool = False
    preserve_split_boundary: bool = False
    semantic_type: str | None = None
    restored_inline_cluster: bool = False
    compact_formula_cluster: bool = False
    formula_candidate_only: bool = False
    paragraph_formula_context: bool = False
    structural_title: bool = False
    explicit_section_title: bool = False
    title_suppressed: bool = False
    style_scale_repaired: bool = False
    inline_math_regions: list[BBox] = field(default_factory=list)
    paragraph_group: int | None = None
    paragraph_terminal: bool = False
    native_typographic_scale: float | None = field(compare=False, default=None)
    reference_start: bool | None = None
    title_band_id: int | None = None
    caption_start: bool = False
    footnote_marker_start: bool = False
    note_marker_value: str | None = None
    numbered_heading_start: bool = False
    native_math_words: frozenset[str] = field(default_factory=frozenset)
    wrapped_title_parts: tuple[_LineItem, _LineItem] | None = field(compare=False, repr=False, default=None)
    native_title_label_left: float | None = field(compare=False, default=None)

    def __post_init__(self) -> None:
        """Complete optional source geometry fields for legacy calls and synthetic tests."""

        if self.source_bbox is None:
            self.source_bbox = self.bbox
        # The role of the number run is frozen at the input boundary, and the auxiliary spatial classification only consumes the numbered evidence without rereading the entire line of text.
        marker = self.text.strip()
        self.note_marker_value = marker if marker.isdigit() and 1 <= len(marker) <= 3 else None
        # Numbered title evidence is frozen at input boundaries, and auxiliary spatial classification does not revisit the full text.
        self.numbered_heading_start = re.match(r"^\d+(?:\.\d+)*\.?(?:\s|$)\S", marker) is not None


@dataclass(slots=True)
class _Fragment:
    """Saves the cell text fragment used by table rules."""

    text: str
    bbox: BBox
    local_bbox: BBox
    line_index: int
    visual_row_id: int | None = None


@dataclass(slots=True)
class _VisualRow:
    """Save table fragments within the same local horizontal band."""

    fragments: list[_Fragment]
    center_y: float
    bbox: BBox
    visual_row_id: int | None = None


@dataclass(slots=True)
class _AxisLine:
    """Save the horizontal and vertical lines in the PDF path."""

    bbox: BBox
    width: float
    orientation: Literal["horizontal", "vertical"]


@dataclass(slots=True)
class _LocalAxisLine:
    """Save the horizontal and vertical lines after switching to the current text direction."""

    bbox: BBox
    original_bbox: BBox
    orientation: Literal["horizontal", "vertical"]
    width: float


@dataclass(slots=True)
class _TableAnnotation:
    """Saves the type, compact boundaries, and original row identity of identified annotations in table candidates."""

    kind: Literal["caption", "footnote"]
    bbox: BBox
    line_indices: set[int] = field(default_factory=set)
    line_bboxes: dict[int, BBox] = field(default_factory=dict)


@dataclass(slots=True)
class _TableCandidate:
    """Save table candidates that have passed adjacent horizontal line boundary and text distribution verification."""

    bbox: BBox
    local_bbox: BBox
    angle: int
    score: float
    core_bbox: BBox | None = None
    line_indices: MutableSet[int] = field(default_factory=set)
    annotations: list[_TableAnnotation] = field(default_factory=list)
    inferred_grid: list[_AxisLine] = field(default_factory=list)
    # The newly restored text grid retains the emphasis style according to the native characters, and the existing tables maintain the original materialization contract.
    preserve_inline_font_styles: bool = False
    # High-confidence cropping of blank forms has rebuilt the canonical grid and can no longer blend text lines with borders into filled rectangles.
    inferred_grid_authoritative: bool = False


@dataclass(slots=True)
class _GraphicCandidate:
    """Saves a graphic text container candidate formed from compact drawing line components."""

    core_bbox: BBox
    lane_index: int
    label_margin_scale: float = 2.5
    line_indices: set[int] = field(default_factory=set)
    # The scale-proven raster plot already contains complete axis labels, allowing only members within the core to avoid drawing in description continuation lines or small boxes between figures.
    strict_core_members: bool = False


@dataclass(slots=True)
class _DrawingComponentSummary:
    """Cache single-page drawing line components and geometric statistics shared by two graphics detections."""

    lines: list[_AxisLine]
    bbox: BBox
    horizontal_count: int
    vertical_count: int


@dataclass(slots=True)
class _CodeCandidate:
    """Save areas of code identified by filled backgrounds or pairs of horizontal lines with a steady rhythm of text."""

    bbox: BBox
    angle: int
    line_indices: set[int] = field(default_factory=set)


@dataclass(slots=True)
class _TextLane:
    """Save local columns and owned text lines in the same text direction."""

    left: float
    right: float
    lines: list[tuple[_LineItem, BBox]] = field(default_factory=list)
    is_span: bool = False


@dataclass(slots=True)
class _FormulaAnchor:
    """Save the right edge anchor point of the formula and its upper and lower position relative to the dense text area."""

    line: _LineItem
    bbox: BBox
    detached_below_body: bool = False
    detached_above_body: bool = False
    repeated_number_band: bool = False


@dataclass(slots=True)
class _PageSource:
    """Save text, characters, drawing lines, and visual containers required for single-page native text analysis."""

    page_size: tuple[float, float]
    lines: list[_LineItem]
    chars: list[Char]
    drawing_lines: list[_AxisLine]
    image_bboxes: list[BBox] = field(default_factory=list)
    signature_bboxes: list[BBox] = field(default_factory=list)
    form_bboxes: list[BBox] = field(default_factory=list)
    path_infos: list[PDFPathInfo] = field(default_factory=list)
    page_index: int | None = None
    publication_bboxes: list[BBox] = field(default_factory=list)
    drawing_component_cache: list[tuple[list[_AxisLine], float, list[_DrawingComponentSummary]]] = field(default_factory=list)
    form_infos: tuple[_PDFFormInfo, ...] = ()
    form_member_sources: dict[BBox, frozenset[int]] = field(default_factory=dict)
    form_path_sources: dict[BBox, frozenset[int]] = field(default_factory=dict)
    retained_page_forms: set[BBox] = field(default_factory=set)


@dataclass(slots=True)
class _PreparedPage:
    """Save the lightweight page that is waiting for cross-page and text type determination after the container claim is completed."""

    page_size: tuple[float, float]
    remaining_lines: list[_LineItem]
    table_bboxes: list[BBox]
    drawing_lines: list[_AxisLine]
    fixed_blocks: list[dict[str, Any]]
    # Only the lightweight path evidence of small dot candidates is retained for restoring the local list after completing text aggregation.
    bullet_paths: tuple[PDFPathInfo, ...] = ()
    canonical_formula_geometry: bool = False
    canonical_formula_source_lines: list[_LineItem] = field(default_factory=list)
    page_footnote_groups: list[set[int]] = field(default_factory=list)
    reference_regions: list[BBox] = field(default_factory=list)
    numbered_references: bool = False
    script_lines: list[PDFTextScriptLine] = field(default_factory=list)
    formula_candidate_lines: list[_LineItem] = field(default_factory=list)
    formula_ink_bboxes: list[BBox] = field(default_factory=list)
    # The sparse table page retains the native row style without character load to prevent reverse statistics of the text using only the large title after the table body is claimed.
    table_body_profile_lines: list[_LineItem] = field(default_factory=list)
    local_axis_table_cache: dict[tuple[int, int, int], tuple[list[_LocalAxisLine], list[_LocalAxisLine]]] = field(
        default_factory=dict
    )


@dataclass(slots=True)
class _MarginalCandidate:
    """Save a single row candidate in the header and footer band and its forward normalized geometry."""

    page_index: int
    line: _LineItem
    local_bbox: BBox
    local_page_size: tuple[float, float]
    region: Literal["header", "footer", "side"]


@dataclass(slots=True)
class _LaneBodyProfile:
    """Save the column and text layout baseline used for title determination, excluding text content characteristics."""

    body_height: float
    body_font: tuple[str, int] | None
    body_weight: float | None
    regular_gap: float
    style_support: dict[tuple[str, int], float]
    body_row_count: int = 0


@dataclass(frozen=True, slots=True)
class _DocumentBodyProfile:
    """Save cross-page text line heights, regular font weights, and recurring regular fonts."""

    body_height: float
    body_weight: float | None
    regular_fonts: frozenset[tuple[str, int]]
    has_style_scale_repairs: bool = False


@dataclass(frozen=True, slots=True)
class _TitleStylePrototype:
    """Saves the font, size, weight, and column alignment characteristics of a cross-page title prototype."""

    font_family: str
    font_flags: int
    height_ratio: float
    weight: float | None
    alignment: Literal["left", "center"]
    anchor_offset: float
    support_count: int
    support_pages: int


@dataclass(frozen=True, slots=True)
class _DocumentTitleProfile:
    """Save a collection of typographic prototypes formed by repeating high-confidence headlines throughout the text."""

    prototypes: tuple[_TitleStylePrototype, ...]
