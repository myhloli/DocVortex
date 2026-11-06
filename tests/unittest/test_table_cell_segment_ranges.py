"""验证公开表格片段保留原始跨行范围及虚拟占位边界。"""

from bs4 import BeautifulSoup

from docvortex.content.table import RenderedCellSegment, build_row_rendered_cell_segments, build_table_state_from_html


def test_inherited_segment_preserves_original_row_range() -> None:
    """继承自更早行的跨行跨列单元格返回原始范围，并保留图片存在性。"""
    state = build_table_state_from_html(
        '<table><tr><td rowspan="3" colspan="2">Group<img src="image.png"/></td><td>A</td></tr>'
        "<tr><td>B</td></tr><tr><td>C</td></tr></table>"
    )
    assert state is not None
    segments = build_row_rendered_cell_segments(state.rows, -1)
    assert [(s.text, s.start_col, s.end_col, s.start_row, s.end_row) for s in segments] == [
        ("Group", 0, 2, 0, 3),
        ("C", 2, 3, 2, 3),
    ]
    assert segments[0].has_non_text_content is True
    assert segments[1].has_non_text_content is False


def test_virtual_segment_has_no_invented_row_range() -> None:
    """外部继承占位没有真实源单元格，应返回未知行范围。"""
    rows = BeautifulSoup("<table><tr><td>A</td></tr><tr><td>B</td></tr></table>", "html.parser").find_all("tr")
    segments = build_row_rendered_cell_segments(rows, 1, initial_occupied={0: {0}, 1: {0}})
    assert [(s.text, s.start_row, s.end_row) for s in segments] == [("", None, None), ("B", 1, 2)]


def test_segment_existing_constructor_keeps_unknown_range_defaults() -> None:
    """既有片段构造参数继续有效，未提供的行范围保持未知。"""
    segment = RenderedCellSegment("A", 0, 1, True)
    assert segment.text == "A" and segment.has_non_text_content is True
    assert segment.start_row is None and segment.end_row is None
