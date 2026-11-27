"""验证有定位依据的严重边界故障才会降低原生版面伪置信度。"""

import pytest

from docvortex.analyzers.native.pdf.layout_risk_scoring import score_layout_pages
from docvortex.analyzers.native.pdf.layout_vector_risk import vector_conflict
from docvortex.document.pdf.native_contracts import PDFDrawingLine, PDFPageVectorGeometry, PDFPathInfo


def _block(role, text, bbox):
    """构造带一条原始行框的独立块。"""
    return {"type": role, "content": text, "bbox": bbox, "lines": [{"bbox": bbox}]}


def test_prose_and_code_split_into_text_only():
    """正文连续行和真正程序行应当可在全为文本时触发，完整段与编号清单则放行。"""
    prose = [
        _block("text", "a continuous line of the same passage", (0.12, y, 0.68, y + 0.018))
        for y in (0.12, 0.14, 0.16, 0.18, 0.20)
    ]
    code = [_block("text", f"data[{i}] = value;", b["bbox"]) for i, b in enumerate(prose)]
    items = [{**b, "content": f"{i + 1}. a separate item in a list"} for i, b in enumerate(prose)]
    scores = score_layout_pages([prose, code, items, [{**prose[0], "lines": [b["lines"][0] for b in prose]}]])
    assert scores == (0.40, 0.40, 1.0, 1.0)


def test_one_display_equation_is_fragmented():
    """三块横向连成的公式含真实重叠；仅两块重叠或分属上下两行时不能据此接管。"""
    a, b, c = [
        _block("equation", "", r) for r in [(0.12, 0.21, 0.26, 0.25), (0.21, 0.212, 0.26, 0.245), (0.266, 0.218, 0.41, 0.25)]
    ]
    scores = score_layout_pages([[a, b, c], [a, b], [a, b, {**c, "bbox": (0.26, 0.30, 0.41, 0.35)}]])
    assert scores == (0.40, 1.0, 1.0)


def test_true_figure_paths_and_reference_coverage():
    """真实轴与两条走势共同提供源证据；已有图框和单独装饰线都能排除。"""
    size = (200, 200)
    paths = [
        PDFPathInfo(box, segments, False, True, 0, i)
        for i, (box, segments) in enumerate([((20, 30, 180, 150), 5), ((30, 50, 170, 110), 22), ((25, 45, 162, 116), 17)])
    ]
    lines = [
        PDFDrawingLine((20, 30), (20, 150), (20, 30, 20, 150), 1, "vertical"),
        PDFDrawingLine((20, 150), (180, 150), (20, 150, 180, 150), 1, "horizontal"),
    ]
    geom = PDFPageVectorGeometry(tuple(lines), tuple(paths))
    assert vector_conflict([], size, geom)[0] == "missing_chart"
    assert vector_conflict([_block("chart", "", (0.09, 0.145, 0.905, 0.755))], size, geom) is None
    assert vector_conflict([], size, PDFPageVectorGeometry(tuple(lines), tuple(paths[:2]))) is None


def test_separate_tables_merged_in_html():
    """跨列标题后重启列名和两条数值行才认作另一张表。"""
    cells = "<tr><td>Trial A</td><td>12</td><td>15</td></tr><tr><td>Trial B</td><td>31</td><td>22</td></tr>"
    body = f'<table>{cells}<tr><td colspan="3">Table 3: Validation</td></tr><tr><th>Test</th><th>Cold</th><th>Warm</th></tr>{cells}</table>'
    t = _block("table", body, (0.13, 0.2, 0.82, 0.65))
    scores = score_layout_pages(
        [[t], [{**t, "content": body.replace("Table 3: Validation", "Table 3 shows comparable values")}]]
    )
    assert scores == (0.40, 1.0)


def test_sparse_unboxed_table_needs_actual_rails_and_columns():
    """两条相交的表线必须有真实框外数值行配合，完整表格内部可按正常粒度输出。"""
    blocks = [
        _block("text", str(10 * i + j), (x, y, x + 0.03, y + 0.01))
        for i, y in enumerate((0.50, 0.525))
        for j, x in enumerate((0.32, 0.44, 0.61))
    ]
    lines = (
        PDFDrawingLine((40, 96), (145, 96), (40, 96, 145, 96), 1, "horizontal"),
        PDFDrawingLine((80, 90), (80, 114), (80, 90, 80, 114), 1, "vertical"),
    )
    paths = tuple(
        PDFPathInfo(b, 2, False, True, 0, i) for i, b in enumerate(((40, 96, 145, 96), (80, 90, 80, 114), (20, 90, 20, 114)))
    )
    g = PDFPageVectorGeometry(lines, paths)
    assert vector_conflict(blocks, (200, 200), g)[0] == "table_structure"
    table = _block("table", "<table/>", (0.2, 0.45, 0.73, 0.57))
    assert vector_conflict(blocks + [table], (200, 200), g) is None


def test_wide_repeated_crops_require_prose_collision():
    """重复通栏裁图切进至少两行真正文时才接管，同宽但独立的方程不触发。"""
    formulas = [_block("equation", "", (0.02, y, 0.87, y + 0.04)) for y in (0.1, 0.22, 0.34, 0.47)]
    text = [_block("text", "a regular printed sentence with multiple words", (0.14, y, 0.76, y + 0.012)) for y in (0.23, 0.35)]
    scores = score_layout_pages([formulas, formulas + text])
    assert scores == (1.0, 0.40)


def _sparse_table_page(angle, *, owning_table=False):
    """生成有两条表头边线、四列文字和三行碎块的页面，整体转到指定阅读方向。"""
    from docvortex.analyzers.native.pdf.geometry import _rotate_bbox_from_upright

    size = (600, 800) if angle in (90, 270) else (800, 600)
    blocks = []
    for y, names in [(86, ["Name", "Material", "Condition", "Reference"])] + [
        (y, ["Sample", "Polymer", "Concentration", "Authors"]) for y in (108, 149, 190)
    ]:
        for x, name in zip((65, 225, 405, 625), names):
            box = _rotate_bbox_from_upright((x, y, x + 68, y + 10), size, angle)
            blocks.append({**_block("text", name, tuple(v / scale for v, scale in zip(box, size * 2))), "angle": angle})
    if owning_table:
        box = _rotate_bbox_from_upright((39, 80, 755, 201), size, angle)
        blocks.append({**_block("table", "", tuple(v / scale for v, scale in zip(box, size * 2))), "angle": angle})
    lines = []
    paths = []
    for i, y in enumerate((81, 103)):
        box = _rotate_bbox_from_upright((40, y, 754, y + 0.5), size, angle)
        orientation = "vertical" if angle in (90, 270) else "horizontal"
        lines.append(PDFDrawingLine(box[:2], box[2:], box, 0.5, orientation))
        paths.append(PDFPathInfo(box, 5, True, False, 0, i))
    return blocks, size, PDFPageVectorGeometry(tuple(lines), tuple(paths))


def test_rotated_sparse_table_uses_real_cell_tracks():
    """各阅读方向应作同样决定，完整母表、单列竖排、无表线或只有表头均不触发。"""
    for angle in (0, 90, 180, 270):
        blocks, size, geometry = _sparse_table_page(angle)
        proof = vector_conflict(blocks, size, geometry)
        assert proof and proof[0] == "table_structure" and "文字列" in proof[1]
        assert vector_conflict([{**b, "content": "X"} for b in blocks], size, geometry) is None
        assert vector_conflict(blocks[:4], size, geometry) is None
        assert vector_conflict(blocks[::4], size, geometry) is None
        assert vector_conflict(blocks, size, PDFPageVectorGeometry()) is None
        complete, _, _ = _sparse_table_page(angle, owning_table=True)
        assert vector_conflict(complete, size, geometry) is None


@pytest.mark.parametrize("above", [False, True], ids=["below", "above"])
def test_partial_ruled_table_requires_repeated_detached_rows(above):
    """表框外要有多行独立数值沿同一组列轨延续，合计、邻表、正文及错位数列都应放行。"""
    size = (600, 800)
    table = _block("table", "Metric    Left    Middle    Right", (0.12, 0.14, 0.84, 0.30))
    rows = [
        [_block("text", str(i * 5 + j), (x - 0.025, y, x, y + 0.012)) for j, x in enumerate((0.35, 0.55, 0.79))]
        for i, y in enumerate((0.314, 0.342, 0.38, 0.42))
    ]
    cells = [cell for row in rows for cell in row]

    def oriented(blocks):
        """表顶用例上下镜像，保持文字正向并同步每个块的行框。"""
        if not above:
            return blocks
        return [
            _block(b["type"], b["content"], (x, 1 - bottom, end, 1 - y)) for b in blocks for x, y, end, bottom in [b["bbox"]]
        ]

    def geometry(rails):
        """制作只含实际同宽横线的源几何。"""
        heights = [(1 - y if above else y) * size[1] for y in rails]
        boxes = [(72, y - 0.2, 504, y + 0.2) for y in heights]
        return PDFPageVectorGeometry(tuple(PDFDrawingLine(b[:2], b[2:], b, 0.4, "horizontal") for b in boxes))

    rails = (0.14, 0.23, 0.30, 0.368, 0.408, 0.447)

    def conflict(blocks, positions=rails):
        """用对应方向的块与绘线验证同一组聚集或屏障条件。"""
        return vector_conflict(oriented(blocks), size, geometry(positions))

    scores = score_layout_pages([oriented([table, *cells])], vector_geometry=lambda _: (size, geometry(rails)))
    assert scores == (0.40,)
    kind, detail = conflict([table, *cells], rails)
    assert kind == "table_structure" and ("表顶" if above else "表底") in detail and "列" in detail
    # 源横线和短行标题连续也不能把远处的数据借作表体；两种方向都按纵向表边算距离。
    distant = [_block("text", b["content"], (x, y + 0.12, end, base + 0.12)) for b in cells for x, y, end, base in [b["bbox"]]]
    labels = [_block("text", "Group", (0.14, y, 0.23, y + 0.012)) for y in (0.32, 0.37, 0.40)]
    assert conflict([table, *labels, *distant], (0.14, 0.23, 0.30, 0.35, 0.39, 0.42, 0.488, 0.528, 0.567)) is None
    assert conflict([table, *rows[0]]) is None
    assert conflict([table, *cells[:-3]]) is None
    assert conflict([table, *cells], (0.14, 0.23, 0.30, 0.31, 0.34, 0.60)) is None
    assert conflict([table, *[{**b, "content": "word"} for b in cells]]) is None
    duplicates = [b for row in rows for b in (row[0], row[0], row[2])]
    assert conflict([table, *duplicates]) is None
    after_blank = [{**b, "bbox": (x, y + 0.05, z, v + 0.05)} for b in cells for x, y, z, v in [b["bbox"]]]
    assert conflict([table, *after_blank], (0.14, 0.3, 0.35, 0.405, 0.463, 0.495)) is None
    assert conflict([table, *cells], (0.30, 0.368, 0.408, 0.447)) is None
    for barrier in (
        _block("caption", "Table 2", (0.14, 0.301, 0.22, 0.313)),
        _block("text", "Table 2. Measurements", (0.14, 0.301, 0.43, 0.313)),
        _block("text", "An independent paragraph introduces the following values.", (0.12, 0.301, 0.82, 0.313)),
        _block("table", "<table/>", (0.12, 0.301, 0.84, 0.445)),
    ):
        assert conflict([table, barrier, *cells]) is None
    staggered = [
        {**b, "bbox": (x + 0.017 * i, y, z + 0.017 * i, v)}
        for i, row in enumerate(rows)
        for b in row
        for x, y, z, v in [b["bbox"]]
    ]
    assert conflict([table, *staggered]) is None
