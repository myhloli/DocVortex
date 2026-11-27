"""Flash 原生 PDF 提取使用的内部数据模型。"""

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
    from ....document.pdf.native_contracts import PDFImageInfo
    from .inline.types import PDFTextScriptLine
    from .form_expansion import FormExpansion

_MARKER_REGEX_REFERENCES = (re.match, re._compile)


class _SharedLineIndexSet(MutableSet[int]):
    """共享只读基底，仅为单个候选记录增删差集，避免复制大型行号集合。"""

    __slots__ = ("base", "added", "removed")

    def __init__(
        self,
        base: frozenset[int],
        values: Iterable[int] = (),
    ) -> None:
        """以共享基底和候选已有成员初始化集合。"""

        self.base = base
        self.removed: set[int] = set()
        if isinstance(values, _SharedLineIndexSet) and values.base is base:
            # 与共享基底求并集时，基底成员天然全部可见，只需保留额外成员。
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
        """以共享基底精确表示给定集合，而不是默认并入全部基底成员。"""

        visible = set(values)
        instance = cls.__new__(cls)
        instance.base = base
        instance.added = visible.difference(base)
        instance.removed = set(base.difference(visible))
        return instance

    def __contains__(self, value: object) -> bool:
        """按基底、删除集和新增集判断成员是否可见。"""

        if value in self.added:
            return True
        return value in self.base and value not in self.removed

    def __iter__(self) -> Iterator[int]:
        """迭代当前可见成员，不展开持久化副本。"""

        for value in self.base:
            if value not in self.removed:
                yield value
        yield from self.added

    def __len__(self) -> int:
        """返回当前可见成员数量。"""

        return len(self.base) - len(self.removed) + len(self.added)

    def add(self, value: int) -> None:
        """加入成员；基底已有成员只需撤销删除标记。"""

        if value in self.base:
            self.removed.discard(value)
            return
        self.added.add(value)

    def discard(self, value: int) -> None:
        """删除成员；基底成员以差集标记表示。"""

        if value in self.added:
            self.added.discard(value)
            return
        if value in self.base:
            self.removed.add(value)

    def update(self, values: Iterable[int]) -> None:
        """并入成员；同共享基底的候选直接合并差集。"""

        if isinstance(values, _SharedLineIndexSet) and values.base is self.base:
            self.removed.intersection_update(values.removed)
            for value in values.added:
                self.add(value)
            return
        for value in values:
            self.add(value)

    def difference_update(self, values: Iterable[int]) -> None:
        """移除给定成员，并保持共享基底不变。"""

        if isinstance(values, (set, frozenset)):
            self.added.difference_update(values)
            self.removed.update(self.base.intersection(values))
            return
        for value in values:
            self.discard(value)


@dataclass(slots=True)
class _LineItem:
    """保存单个可视文本行及其 source/ink/canonical 几何。"""

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
    # 释放原字符后保留少量汉字排版证据，混排 dominant font 改变仍可核验正文连续。
    native_cjk_style: tuple[str, int, float, float] | None = None
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
    # 保留换行两端的实际字体；同行混排时主导字体不能代表最后或最先出现的字形。
    edge_fonts: tuple[tuple[str, int] | None, tuple[str, int] | None] = field(compare=False, default=(None, None))
    native_title_label_left: float | None = field(compare=False, default=None)
    _marker_features: tuple | None = field(compare=False, repr=False, default=None)
    # 数字图形特征只绑定当前文字对象；几何、语义和认领状态每次重新判断。
    _graphic_numeric_features: tuple | None = field(compare=False, repr=False, default=None)
    layout_glyph_ranges: bytes = field(compare=False, repr=False, default=b"")

    def __post_init__(self) -> None:
        """为旧调用与合成测试补齐可选来源几何字段。"""

        if self.source_bbox is None:
            self.source_bbox = self.bbox
        # 数字 run 的角色在输入边界冻结，辅助空间分类只消费编号证据，不重新读取整行文字。
        cached = self._marker_features
        stable_regex = (re.match, re._compile) == _MARKER_REGEX_REFERENCES
        if (
            type(self.text) is str
            and stable_regex
            and type(cached) is tuple
            and len(cached) == 4
            and cached[0] is self.text
            and cached[1] is re.match
        ):
            self.note_marker_value, self.numbered_heading_start = cached[2:]
        else:
            marker = self.text.strip()
            self.note_marker_value = marker if marker.isdigit() and 1 <= len(marker) <= 3 else None
            # 编号标题证据绑定当前行的文字对象；复制只改几何时复用，文字或正则函数改变后重算。
            self.numbered_heading_start = re.match(r"^\d+(?:\.\d+)*\.?(?:\s|$)\S", marker) is not None
            self._marker_features = (
                (self.text, re.match, self.note_marker_value, self.numbered_heading_start)
                if type(self.text) is str and stable_regex
                else None
            )


@dataclass(slots=True)
class _Fragment:
    """保存表格规则使用的单元文本片段。"""

    text: str
    bbox: BBox
    local_bbox: BBox
    line_index: int
    visual_row_id: int | None = None


@dataclass(slots=True)
class _VisualRow:
    """保存同一局部水平带内的表格片段。"""

    fragments: list[_Fragment]
    center_y: float
    bbox: BBox
    visual_row_id: int | None = None


@dataclass(slots=True)
class _AxisLine:
    """保存 PDF 路径中的横竖线。"""

    bbox: BBox
    width: float
    orientation: Literal["horizontal", "vertical"]


@dataclass(slots=True)
class _LocalAxisLine:
    """保存转入当前文本方向后的横竖线。"""

    bbox: BBox
    original_bbox: BBox
    orientation: Literal["horizontal", "vertical"]
    width: float


@dataclass(slots=True)
class _TableAnnotation:
    """保存表格候选中已识别注释的类型、紧致边界和原始行身份。"""

    kind: Literal["caption", "footnote"]
    bbox: BBox
    line_indices: set[int] = field(default_factory=set)
    line_bboxes: dict[int, BBox] = field(default_factory=dict)


@dataclass(slots=True)
class _TableCandidate:
    """保存已通过相邻横线边界与文本分布校验的表格候选。"""

    bbox: BBox
    local_bbox: BBox
    angle: int
    score: float
    core_bbox: BBox | None = None
    line_indices: MutableSet[int] = field(default_factory=set)
    annotations: list[_TableAnnotation] = field(default_factory=list)
    inferred_grid: list[_AxisLine] = field(default_factory=list)
    # 新恢复的文字网格按原生字符保留强调样式，已有表格维持原物化契约。
    preserve_inline_font_styles: bool = False
    # 高置信裁切空白表单已重建规范网格，不能再混入填色矩形的文字行带边界。
    inferred_grid_authoritative: bool = False


@dataclass(slots=True)
class _VerifiedTableCandidate(_TableCandidate):
    """全页新提名附上独立边界证据，旧候选的值表示不增加字段。"""

    # 重复行带提出的候选必须恢复预期行数并保全字符，失败时原有内容所有权不变。
    verified_region_rows: int = 0
    # 独立闭合笔画给出精确列数，也允许从较大的误判图形组件中移走整幅表。
    verified_region_columns: int = 0
    # 重复底纹正文独立证实的公共列沟，用于校验欠分割的网格。
    verified_region_gutters: tuple[tuple[float, float], ...] = ()
    # 实体竖轨只需贯穿有底纹证明的正文，接回的框外表头不稀释此条件。
    verified_region_body_band: tuple[float, float] | None = None
    # 年份列确认精确列数，同时允许跨列总表头和没有金额的小节。
    verified_period_columns: int = 0
    # 用较小新表替换跨正文旧表时，其余已识别的金额行必须先由前面的独立表成功输出。
    verified_period_dependencies: tuple[BBox, ...] = ()
    # 分开绘制的完整外框和内轨限定每一个物理格，不用平均行高推算横线位置。
    verified_grid_tracks: tuple[tuple[float, ...], tuple[float, ...]] | None = None


@dataclass(slots=True)
class _GraphicCandidate:
    """保存由紧凑绘图线组件形成的图形文本容器候选。"""

    core_bbox: BBox
    lane_index: int
    label_margin_scale: float = 2.5
    line_indices: set[int] = field(default_factory=set)
    # 刻度证明的栅格图已包含完整轴标签，只允许核心内成员，避免再吸入说明续行或图间小框。
    strict_core_members: bool = False


@dataclass(slots=True)
class _DrawingComponentSummary:
    """缓存单页绘图线分量及两处图形检测共用的几何统计。"""

    lines: list[_AxisLine]
    bbox: BBox
    horizontal_count: int
    vertical_count: int


@dataclass(slots=True)
class _CodeCandidate:
    """保存由填充背景或成对横线与稳定文本节奏确认的代码区域。"""

    bbox: BBox
    angle: int
    line_indices: set[int] = field(default_factory=set)


@dataclass(slots=True)
class _TextLane:
    """保存同一文本方向下的局部栏带与已归属文本行。"""

    left: float
    right: float
    lines: list[tuple[_LineItem, BBox]] = field(default_factory=list)
    is_span: bool = False


@dataclass(slots=True)
class _FormulaAnchor:
    """保存公式右缘锚点及其相对正文密集区的上下位置。"""

    line: _LineItem
    bbox: BBox
    detached_below_body: bool = False
    detached_above_body: bool = False
    repeated_number_band: bool = False


@dataclass(frozen=True, slots=True)
class _TableContinuationGrid:
    """仅保留前一物理页末表的方向、归一化列轨和字号容差，不延长字符生命周期。"""

    page_index: int
    angle: int
    positions: tuple[float, ...]
    tolerance: float


@dataclass(slots=True)
class _PageSource:
    """保存单页原生文本分析所需的文本、字符、绘图线和视觉容器。"""

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
    # 整页装饰只按实际 Form 调用身份排除，子位图已由同一页面的源过滤证明。
    discarded_raster_forms: frozenset[int] = frozenset()
    form_member_sources: dict[BBox, frozenset[int]] = field(default_factory=dict)
    form_path_sources: dict[BBox, frozenset[int]] = field(default_factory=dict)
    retained_page_forms: set[BBox] = field(default_factory=set)
    # 原字符存在未决遮挡时，不用它们替换已有图框；该信号不触发PP。
    text_visibility_uncertain: bool = False
    compound_baseline_cache: dict = field(default_factory=dict)
    isolated_path_cache: dict = field(default_factory=dict)
    previous_table_grid: _TableContinuationGrid | None = None
    # 部分结构可用时，残余表仍需避开已认领的近邻题名虚边。
    trusted_text_bboxes: tuple[BBox, ...] = ()
    # 已证实由本页点阵图、原生引线及图外标签共同组成的占位。
    raster_callout_bboxes: dict[BBox, BBox] = field(default_factory=dict)
    # 记录重复位图的唯一邻接图题，以便语义组装时沿用同一父图证据。
    raster_caption_rows: dict[BBox, tuple[BBox, str]] = field(default_factory=dict)
    # 保留局部位图合成证据，待竞争容器筛选结束后裁定。
    raster_composite_infos: tuple[PDFImageInfo, ...] = ()
    collect_layout_diagnostics: bool = False
    layout_raster_fingerprints: dict[BBox, str] | None = None
    # 有独立原始位图身份的大幅内容图，供少数带标注内嵌图的页面核对整体归属。
    inset_base_bboxes: frozenset[BBox] = frozenset()


@dataclass(frozen=True, slots=True)
class _DeferredFilledRegion:
    """保存已由分面几何验证的图核及候选源行，供块装配后整组认领。"""

    bbox: BBox
    members: tuple[_LineItem, ...]


@dataclass(slots=True)
class _PreparedPage:
    """保存容器认领完成后、等待跨页与文本类型判定的轻量页面。"""

    page_size: tuple[float, float]
    remaining_lines: list[_LineItem]
    table_bboxes: list[BBox]
    drawing_lines: list[_AxisLine]
    fixed_blocks: list[dict[str, Any]]
    deferred_filled_regions: list[_DeferredFilledRegion] = field(default_factory=list)
    # 已确认的裸位图及其唯一源题名，正文组装完成后才进入排序。
    deferred_raster_captions: dict[BBox, tuple[BBox, str]] = field(default_factory=dict)
    # 只保留小圆点候选的轻量路径证据，供完成文本聚合后恢复局部列表。
    bullet_paths: tuple[PDFPathInfo, ...] = ()
    canonical_formula_geometry: bool = False
    canonical_formula_source_lines: list[_LineItem] = field(default_factory=list)
    page_footnote_groups: list[set[int]] = field(default_factory=list)
    reference_regions: list[BBox] = field(default_factory=list)
    numbered_references: bool = False
    # 方括号文献区中的年代/卷页续行不能被小数点编号误作新条目。
    bracketed_reference_numbers: bool = False
    script_lines: list[PDFTextScriptLine] = field(default_factory=list)
    formula_candidate_lines: list[_LineItem] = field(default_factory=list)
    formula_ink_bboxes: list[BBox] = field(default_factory=list)
    # 仅有异常宽松定界符的页面保留行墨迹，用于公式输出框的邻行核验。
    formula_crop_ink: dict[int, tuple[str, BBox, bool]] | None = None
    # 稀疏表格页保留无字符载荷的原生行样式，防止表体认领后只用大题名反向统计正文。
    table_body_profile_lines: list[_LineItem] = field(default_factory=list)
    local_axis_table_cache: dict[tuple[int, int, int], tuple[list[_LocalAxisLine], list[_LocalAxisLine]]] = field(
        default_factory=dict
    )
    # 被结构树作为 Artifact 隔离的书目运行页眉只用于确认上下文，不向正文回填。
    reference_running_heading: bool = False
    # 页内候选不参与整件样式学习，选定后同步其行内材料与诊断。
    form_expansion: FormExpansion | None = None
    # 残余水印排除后的页面应使用已确认的公式、目录及题项证据约束章节晋升。
    preserve_non_title_roles: bool = False


@dataclass(slots=True)
class _MarginalCandidate:
    """保存页眉页脚带中的单行候选及其正向归一化几何。"""

    page_index: int
    line: _LineItem
    local_bbox: BBox
    local_page_size: tuple[float, float]
    region: Literal["header", "footer", "side"]


@dataclass(slots=True)
class _LaneBodyProfile:
    """保存标题判定使用的栏带正文排版基线，不包含文本内容特征。"""

    body_height: float
    body_font: tuple[str, int] | None
    body_weight: float | None
    regular_gap: float
    style_support: dict[tuple[str, int], float]
    body_row_count: int = 0


@dataclass(frozen=True, slots=True)
class _DocumentBodyProfile:
    """保存跨页正文行高、常规字重与反复出现的常规字体。"""

    body_height: float
    body_weight: float | None
    regular_fonts: frozenset[tuple[str, int]]
    has_style_scale_repairs: bool = False


@dataclass(frozen=True, slots=True)
class _TitleStylePrototype:
    """保存跨页标题原型的字体、尺度、字重和栏内对齐特征。"""

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
    """保存全文中由重复高置信标题形成的排版原型集合。"""

    prototypes: tuple[_TitleStylePrototype, ...]
