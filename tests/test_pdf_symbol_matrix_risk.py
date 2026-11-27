"""验证符号矩阵被横向切片且中间数据行无人承接时的页级扣分。"""

import pytest

from docvortex.analyzers.native.pdf.layout_risk_scoring import score_layout_pages
from docvortex.document.pdf.native_contracts import PDFPageVectorGeometry, PDFPathInfo


def _matrix_page(*, size=(500, 600), offset=(0.14, 0.28), scattered=False, single_color=False, middle=True, columns=6):
    """构造重复符号的物理绘图和仅覆盖首尾数据行的原生块，可变换位置、页幅和符号分布。"""
    width, height = size
    left, top = width * offset[0], height * offset[1]
    step = 0.054 * width
    diameter = 0.014 * width
    paths = []
    for row in range(5):
        if not middle and row in (1, 2, 3):
            continue
        for column in range(columns):
            x = left + column * step + (row * diameter * 0.55 if scattered else 0)
            y = top + row * diameter * 1.65
            box = (x, y, x + diameter, y + diameter)
            color = (20, 60, 110, 255) if single_color or (column + row) % 2 else (150, 105, 115, 255)
            paths.append(PDFPathInfo(box, 13, True, False, 0, len(paths), color))
    blocks = [
        {
            "type": kind,
            "bbox": ((left - 1) / width, y / height, (left + 5 * step + diameter + 1) / width, (y + diameter) / height),
        }
        for kind, y in (("table", top), ("equation", top + 4 * diameter * 1.65))
    ]
    return blocks, PDFPageVectorGeometry((), tuple(paths))


@pytest.mark.parametrize("size,offset", [((500, 600), (0.14, 0.28)), ((820, 580), (0.40, 0.55))])
def test_unowned_rows_between_matrix_fragments_lower_confidence(size, offset):
    """同列符号延续经过多个未交付的数据行，换页幅和位置仍应判为严重。"""
    blocks, geometry = _matrix_page(size=size, offset=offset)
    assert score_layout_pages([blocks], vector_geometry=lambda _: (size, geometry)) == (0.40,)


@pytest.mark.parametrize("owner", ["chart", "image", "table"])
def test_delivered_matrix_preserves_confidence(owner):
    """有完整的图表框时，内部的标题、公式裁片或局部表框不能单独降分。"""
    blocks, geometry = _matrix_page()
    blocks.append({"type": owner, "bbox": (0.12, 0.25, 0.45, 0.39)})
    assert score_layout_pages([blocks], vector_geometry=lambda _: ((500, 600), geometry)) == (1.0,)


@pytest.mark.parametrize("options", [{"scattered": True}, {"single_color": True}, {"middle": False}, {"columns": 2}])
def test_other_marks_do_not_establish_a_fragmented_matrix(options):
    """散点、单色装饰、两个独立图带以及列表项的点阵均不能证明跨区矩阵。"""
    blocks, geometry = _matrix_page(**options)
    assert score_layout_pages([blocks], vector_geometry=lambda _: ((500, 600), geometry)) == (1.0,)


def test_unrelated_strips_and_independently_delivered_panels_preserve_confidence():
    """页面另一侧的点阵，或已由两个相邻图框完整交付的点阵，不应由局部窄框触发扣分。"""
    blocks, geometry = _matrix_page()
    elsewhere = [{**block, "bbox": (0.57, block["bbox"][1], 0.91, block["bbox"][3])} for block in blocks]
    panels = [{"type": "image", "bbox": (left, 0.25, right, 0.39)} for left, right in [(0.12, 0.29), (0.29, 0.45)]]
    assert score_layout_pages([elsewhere, blocks + panels], vector_geometry=lambda _: ((500, 600), geometry)) == (1.0, 1.0)
