"""以白格滑坡原件和独立空间反例验证原生表格的保守拒绝。"""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re

import pytest

from docvortex.analyzers.native.pdf._table_recovery import (
    NativeTableInput,
    NativeTableRectangle,
    NativeTableRule,
    recover_native_pdf_table,
)
from docvortex.analyzers.native.pdf._table_recovery import engine
from docvortex.analyzers.native.pdf._table_recovery.text import build_native_table_text
from docvortex.analyzers.native.pdf._table_recovery.sparse_multiline import _split_rows_by_anchors
from docvortex.analyzers.native.pdf._table_recovery.vector import _physical_row_dense_baseline_pairs
from docvortex.analyzers.native.pdf._table_recovery.vector import _CanonicalTrack, _MergedRule
from docvortex.analyzers.native.pdf.models import _PageSource, _TableCandidate
from docvortex.analyzers.native.pdf.spatial_text import project_pdf_table_text
from docvortex.analyzers.native.pdf.table_materialization import _materialize_table_blocks
from test_native_pdf_table import _candidate, _char_items, _local_to_page_bbox


def _baige_input(table: int, mode: str) -> NativeTableInput:
    """加载带原件指纹的字符与绘图原语，覆盖两种产品实际区域。"""
    fixture = json.loads((Path(__file__).parents[1] / "fixtures/native_pdf_table_baige.json").read_text())
    assert fixture["source_sha256"] == "4836be200a9af299be2852a6b0d5263f8a054f21a01aa2d64ea2c8391a36adcd"
    record = fixture["tables"][table]
    return NativeTableInput(
        table_bbox=tuple(record["regions"][mode]),
        page_size=tuple(record["page_size"]),
        angle=0,
        chars=tuple(record["chars"]),
        drawing_lines=tuple(NativeTableRule(**rule) for rule in record["drawing_lines"]),
        rectangles=tuple(NativeTableRectangle(**rectangle) for rectangle in record["rectangles"]),
    )


@pytest.mark.parametrize("mode", ["flash", "standard"])
def test_baige_satellite_table_preserves_header_and_all_eight_records(mode: str) -> None:
    """原页两层表头和八行参数分别落格，日期的多行内容仍属于原单元格。"""
    table_input = _baige_input(0, mode)
    before = deepcopy(table_input.chars)
    result = recover_native_pdf_table(table_input)
    assert result is not None
    assert (result.rows, result.cols) == (10, 3)
    assert [(c.row, c.col, c.rowspan, c.colspan, c.content) for c in result.cells if c.row < 2] == [
        (0, 0, 2, 1, "参数"),
        (0, 1, 1, 2, "SAR传感器"),
        (1, 1, 1, 1, "ALOS PALSAR-1"),
        (1, 2, 1, 1, "Sentinel-1A"),
    ]
    expected = [
        ["轨道方向", "升轨", "升轨/降轨"],
        ["所处波段", "L", "C"],
        ["雷达波长/cm", "23.6", "5.6"],
        ["空间分辨率/m", "10", "15"],
        ["重访周期/d", "46", "12"],
        ["侧视角/(°)", "34", "39.5"],
        ["影像时间", "2007-12-2009-02", "2017-12-2018-12/2018-01-2018-12"],
        ["影像数量/景", "9", "29/25"],
    ]
    for row, values in enumerate(expected, start=2):
        cells = [c for c in result.cells if c.row == row]
        assert [(c.col, c.rowspan, c.colspan) for c in cells] == [(0, 1, 1), (1, 1, 1), (2, 1, 1)]
        assert [re.sub(r"\s+", "", c.content) for c in cells] == values
    indices = [index for cell in result.cells for index in cell.source_char_indices]
    assert len(indices) == len(set(indices)) == len(result.text.glyphs)
    assert table_input.chars == before


@pytest.mark.parametrize("mode", ["flash", "standard"])
def test_baige_landslide_table_rejects_both_merged_body_and_six_column_recovery(mode: str) -> None:
    """原页七列表不接受合并表体或高分六列候选，空间投影保留十条完整记录。"""
    table_input = _baige_input(1, mode)
    before = deepcopy(table_input.chars)
    assert recover_native_pdf_table(table_input) is None
    diagnostics = engine.diagnose_native_pdf_table(table_input)
    assert diagnostics["adopted"] is None
    line = next(attempt for attempt in diagnostics["vector_attempts"] if attempt["evidence"] == "line_grid")
    assert line["first_rejection_gate"] == "physical_row_undercount"
    assert line["grid"]["cols"] == 7
    assert any(c["cols"] == 6 and c["score"] == 1.0 for c in diagnostics["generated_candidates"])
    text = re.sub(r"\s+", "", project_pdf_table_text(table_input.chars, table_input.table_bbox))
    for number in (28, 29, 31, 34, 36, 37, 44, 46, 47, 48):
        assert text.count(f"H{number}") == 1
    assert text.count("古滑坡堆积体") == 2
    assert text.count("岩质滑坡") == 8
    for value in ("360641", "413967", "656833", "214908", "65097", "79742", "105973", "76569", "239179", "42587"):
        assert value in text
    assert table_input.chars == before


@pytest.mark.parametrize("angle", [0, 90, 180, 270])
@pytest.mark.parametrize("continuation_position", [0, 1, 2])
@pytest.mark.parametrize("continuation_columns", [(2,), (0, 1)])
def test_dense_record_evidence_survives_sparse_continuations(
    angle: int,
    continuation_position: int,
    continuation_columns: tuple[int, ...],
) -> None:
    """稀疏说明位于重复数据前、中、后时均不遮蔽漏行证据，旋转后结论相同。"""
    rows = [(0, 1, 2), (0, 1, 2)]
    rows.insert(continuation_position, continuation_columns)
    entries = [
        (str(col), (col * 40.0 + 8.0, row * 20.0 + 5.0, col * 40.0 + 13.0, row * 20.0 + 13.0))
        for row, cols in enumerate(rows)
        for col in cols
    ]
    width, height = (90.0, 120.0) if angle in {90, 270} else (120.0, 90.0)
    chars = _char_items(entries)
    rotated_chars = tuple(dict(c, bbox=_local_to_page_bbox(c["bbox"], width, height, angle)) for c in chars)
    text = build_native_table_text(NativeTableInput((0.0, 0.0, width, height), (width, height), angle, rotated_chars))
    assert text is not None
    pairs = _physical_row_dense_baseline_pairs(text, [0.0, 40.0, 80.0, 120.0], [0.0, 90.0])
    assert len(pairs) == 1
    assert pairs[0]["occupied_cols"] == [0, 1, 2]


def test_different_dense_column_sets_do_not_establish_repeated_records() -> None:
    """两条稠密行占用不同列时保持歧义，不能凭文本行数拒绝真实合并结构。"""
    text = build_native_table_text(
        NativeTableInput(
            (0.0, 0.0, 120.0, 90.0),
            (120.0, 90.0),
            0,
            _char_items(
                [
                    ("A", (8.0, 5.0, 13.0, 13.0)),
                    ("B", (48.0, 5.0, 53.0, 13.0)),
                    ("C", (48.0, 25.0, 53.0, 33.0)),
                    ("D", (88.0, 25.0, 93.0, 33.0)),
                ]
            ),
        )
    )
    assert _physical_row_dense_baseline_pairs(text, [0.0, 40.0, 80.0, 120.0], [0.0, 90.0]) == ()


def test_aligned_numeric_override_cannot_reintroduce_lost_physical_column(monkeypatch: pytest.MonkeyPatch) -> None:
    """最终数值候选替换同样遵守原页列数下界，避免高分末端旁路。"""
    monkeypatch.setattr(
        engine,
        "build_aligned_numeric_candidate",
        lambda *args: _candidate(
            source="text_grid",
            rows=11,
            cols=6,
            score=1.0,
        ),
    )
    assert recover_native_pdf_table(_baige_input(1, "flash")) is None


@pytest.mark.parametrize("centered", [True, False])
def test_leading_multiline_cell_needs_symmetric_missing_column_evidence(centered: bool) -> None:
    """只有同一缺失列的上下基线对称包围关键行时，前置文本才转归下一记录。"""
    from docvortex.analyzers.native.pdf._table_recovery.contracts import NativeTableTextRow

    centers = [10.0, 24.0, 30.0, 36.0 if centered else 44.0]
    rows = [NativeTableTextRow(i, (0.0, y - 2.0, 100.0, y + 2.0), (), ()) for i, y in enumerate(centers)]
    groups = _split_rows_by_anchors(rows, 0, {0: {0, 1, 2}, 1: {2}, 2: {0, 1}, 3: {2}}, group_short_key_runs=False)
    assert groups == ([(0,), (1, 2, 3)] if centered else [(0, 1), (2, 3)])


@pytest.mark.parametrize("column_frames", [False, True])
def test_partial_continuations_do_not_override_real_multiline_column_frames(column_frames: bool) -> None:
    """忽略续行的新入口只拒绝缺少列隔断的巨型合并行，真实分列多行单元格保持有效。"""
    text = build_native_table_text(
        NativeTableInput(
            (0.0, 0.0, 120.0, 90.0),
            (120.0, 90.0),
            0,
            _char_items(
                [
                    ("A", (8.0, 5.0, 13.0, 13.0)),
                    ("B", (48.0, 5.0, 53.0, 13.0)),
                    ("C", (88.0, 5.0, 93.0, 13.0)),
                    ("D", (8.0, 25.0, 13.0, 33.0)),
                    ("E", (48.0, 25.0, 53.0, 33.0)),
                    ("F", (88.0, 25.0, 93.0, 33.0)),
                    ("detail", (88.0, 45.0, 112.0, 53.0)),
                ]
            ),
        )
    )
    x_tracks = [0.0, 40.0, 80.0, 120.0]
    rules = [_MergedRule("vertical", x, 0.0, 90.0) for x in x_tracks[1:-1]] if column_frames else []
    pairs = _physical_row_dense_baseline_pairs(
        text,
        x_tracks,
        [0.0, 90.0],
        rules=rules,
        canonical_x_tracks=[_CanonicalTrack(x, (x,)) for x in x_tracks],
        snap_tolerance=1.0,
    )
    assert len(pairs) == (0 if column_frames else 1)


def test_revoked_line_hypothesis_does_not_constrain_valid_fallback_columns() -> None:
    """已撤销的轨道假设及未通过规范化的网格不能把列数限制传给后备候选。"""
    rejected = {
        "evidence": "line_grid",
        "first_rejection_gate": "physical_row_undercount",
        "grid": {"cols": 7},
        "canonical_tracks": [{}] * 8,
    }
    assert engine._minimum_columns_after_row_undercount((rejected,)) == 7
    assert (
        engine._minimum_columns_after_row_undercount(
            (
                {
                    "evidence": "line_grid",
                    "first_rejection_gate": None,
                    "track_hypotheses": [rejected],
                },
            )
        )
        == 0
    )
    assert engine._minimum_columns_after_row_undercount(({**rejected, "canonical_tracks": []},)) == 0


def test_flash_materializes_rejected_baige_table_with_spatial_projection() -> None:
    """真实七列表拒绝后沿 Flash 物化入口输出投影，完整字符和成员归属随之保留。"""
    table_input = _baige_input(1, "flash")
    source = _PageSource(
        page_size=table_input.page_size, chars=list(table_input.chars), lines=[], drawing_lines=list(table_input.drawing_lines)
    )
    candidate = _TableCandidate(
        bbox=table_input.table_bbox,
        local_bbox=table_input.table_bbox,
        angle=0,
        score=1.0,
        line_indices={7},
        core_bbox=table_input.table_bbox,
    )
    blocks, annotations, claimed = _materialize_table_blocks(source, [candidate])
    assert len(blocks) == 1
    assert annotations == []
    assert claimed == {7}
    assert blocks[0]["content"] == project_pdf_table_text(table_input.chars, table_input.table_bbox)
    assert "<table>" not in blocks[0]["content"]
