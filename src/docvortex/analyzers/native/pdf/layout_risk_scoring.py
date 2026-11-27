"""按物理页将 Flash 原生版面的四类正交结构风险换算成伪置信度与归因。"""

from __future__ import annotations

from dataclasses import dataclass

from ....schema import BBox
from . import TEXT_OWNERSHIP, VISUAL_ROLE_CONFLICT
from .geometry import _bbox_area
from .layout_fragment_risk import fragmented_region
from .layout_grouping_risk import displaced_note_points, grouping_loss_points
from .layout_native_conflicts import nested_block_risk, table_container_risk
from .layout_overlap_risk import overlap_risk
from .layout_program_risk import program_equation_conflict
from .layout_risk import extract_scored_layout_risk
from .layout_spatial_confidence import spatial_fragmentation
from .layout_vector_risk import vector_conflict, wants_vectors

_DOMAIN_ORDER = ("container_integrity", "fragmentation", "spatial_collision", "grouping_and_order")
_VECTOR_FRAGMENTATION_KINDS = frozenset({"fragmented_symbol_matrix"})


@dataclass(frozen=True, slots=True)
class PageLayoutAssessment:
    """保存单物理页的伪置信度、四域风险分及可解释归因。"""

    confidence: float
    risk_points: int
    domain_points: dict[str, int]
    primary_domain: str | None
    reasons: tuple[str, ...]


def _record_domain(
    domains: dict[str, tuple[int, str | None]],
    domain: str,
    points: int,
    reason: str,
) -> None:
    """同一正交风险域内只保留最重证据，避免同源冲突重复叠加。"""
    if points > domains[domain][0]:
        domains[domain] = (points, reason)


def assess_layout_pages(
    pages: list[list[dict]],
    *,
    program_controls: dict[int, tuple[BBox, ...]] | None = None,
    vector_geometry=None,
    source_lines=None,
    diagnostics=None,
    major_missing_visuals: dict[int, str] | None = None,
    skip_vector_conflict: set[int] | frozenset[int] | None = None,
) -> tuple[PageLayoutAssessment, ...]:
    """每页按四类正交风险域取域内最大值，跨域以 Noisy-OR 合成伪置信度。"""
    diagnostics_by_page: dict[int, list[tuple[str, str]]] = {}
    if diagnostics:
        for item in diagnostics:
            page_index = getattr(item, "page_index", None)
            code = getattr(item, "code", None)
            if page_index is not None and code in {TEXT_OWNERSHIP, VISUAL_ROLE_CONFLICT}:
                diagnostics_by_page.setdefault(page_index, []).append((code, getattr(item, "message", "")))

    assessments: list[PageLayoutAssessment] = []
    for index, blocks in enumerate(pages):
        domains: dict[str, tuple[int, str | None]] = {name: (0, None) for name in _DOMAIN_ORDER}
        cached_vector = None
        cached_lines = None

        def read_vector():
            nonlocal cached_vector
            if cached_vector is None:
                cached_vector = vector_geometry(index)
            return cached_vector

        def read_lines():
            nonlocal cached_lines
            if cached_lines is None:
                cached_lines = source_lines(index)
            return cached_lines

        for code, message in diagnostics_by_page.get(index, ()):
            if code == TEXT_OWNERSHIP:
                _record_domain(domains, "container_integrity", 60, message)
            elif code == VISUAL_ROLE_CONFLICT:
                _record_domain(domains, "container_integrity", 40, message)
        if major_missing_visuals and (visual_reason := major_missing_visuals.get(index)):
            _record_domain(domains, "container_integrity", 40, visual_reason)

        factors = extract_scored_layout_risk(blocks)
        if risk := factors.get("table_structure"):
            _record_domain(domains, "container_integrity", risk[0], risk[1])
        if risk := factors.get("formula_ownership"):
            _record_domain(domains, "fragmentation", risk[0], risk[1])
        if risk := factors.get("grid_fragmentation"):
            _record_domain(domains, "fragmentation", risk[0], risk[1])

        if program_controls and (controls := program_controls.get(index)):
            if reason := program_equation_conflict(blocks, controls):
                _record_domain(domains, "fragmentation", 30, reason)

        if frag := fragmented_region(blocks):
            _record_domain(domains, "fragmentation", 60, frag[1])

        if spatial := spatial_fragmentation(blocks):
            _record_domain(domains, "fragmentation", spatial[0], spatial[1])

        # 仅在容器与碎裂域都尚未触及 60 分上限且存在有效矢量候选时读取矢量几何。
        if (
            vector_geometry is not None
            and max(domains["container_integrity"][0], domains["fragmentation"][0]) < 60
            and (not skip_vector_conflict or index not in skip_vector_conflict)
            and wants_vectors(blocks)
        ):
            size, geometry = read_vector()
            lazy_lines = read_lines if source_lines is not None else None
            if conflict := vector_conflict(blocks, size, geometry, source_lines=lazy_lines):
                kind, reason = conflict
                target = "fragmentation" if kind in _VECTOR_FRAGMENTATION_KINDS else "container_integrity"
                _record_domain(domains, target, 60, reason)

        if (
            source_lines is not None
            and vector_geometry is not None
            and domains["container_integrity"][0] < 60
            and any(
                block.get("type") == "table" and not block.get("angle") and _bbox_area(block["bbox"]) >= 0.08
                for block in blocks
            )
        ):
            size, geometry = read_vector()
            if table := table_container_risk(blocks, size, read_lines(), geometry):
                _record_domain(domains, "container_integrity", table[0], table[1])

        if nested := nested_block_risk(blocks):
            _record_domain(domains, "spatial_collision", nested[0], nested[1])
        if overlap := overlap_risk(blocks):
            _record_domain(domains, "spatial_collision", overlap[0], overlap[1])

        lazy_grouping_lines = read_lines if source_lines is not None else None
        if grouping := grouping_loss_points(blocks, lazy_grouping_lines):
            _record_domain(domains, "grouping_and_order", grouping, f"存在未恢复的段落、代码或表格归组缺失（{grouping} 分）")
        if domains["grouping_and_order"][0] < 9:
            if note := displaced_note_points(blocks):
                _record_domain(domains, "grouping_and_order", note, f"远处编号脚注打断同源正文印刷行（{note} 分）")

        domain_points = {name: domains[name][0] for name in _DOMAIN_ORDER}
        reasons = tuple(
            f"{name}: {domains[name][1]}"
            for name in sorted(_DOMAIN_ORDER, key=lambda d: (-domain_points[d], _DOMAIN_ORDER.index(d)))
            if domain_points[name] > 0 and domains[name][1] is not None
        )
        if not any(domain_points.values()):
            points = 0
            primary_domain = None
        else:
            survived = 1.0
            for value in domain_points.values():
                if value > 0:
                    survived *= 1.0 - min(100, value) / 100.0
            points = max(1, min(100, round(100.0 * (1.0 - survived))))
            primary_domain = max(_DOMAIN_ORDER, key=lambda name: (domain_points[name], -_DOMAIN_ORDER.index(name)))

        # 先在整数尺度取补值，让每个百分位都能与相同的十进制配置精确比较。
        assessments.append(
            PageLayoutAssessment(
                confidence=(100 - points) / 100,
                risk_points=points,
                domain_points=domain_points,
                primary_domain=primary_domain,
                reasons=reasons,
            )
        )
    return tuple(assessments)


def score_layout_pages(
    pages: list[list[dict]],
    *,
    program_controls: dict[int, tuple[BBox, ...]] | None = None,
    vector_geometry=None,
    source_lines=None,
    diagnostics=None,
    major_missing_visuals: dict[int, str] | None = None,
    skip_vector_conflict: set[int] | frozenset[int] | None = None,
) -> tuple[float, ...]:
    """按原页序返回 0 至 1 的页级版面伪置信度。"""
    return tuple(
        item.confidence
        for item in assess_layout_pages(
            pages,
            program_controls=program_controls,
            vector_geometry=vector_geometry,
            source_lines=source_lines,
            diagnostics=diagnostics,
            major_missing_visuals=major_missing_visuals,
            skip_vector_conflict=skip_vector_conflict,
        )
    )
