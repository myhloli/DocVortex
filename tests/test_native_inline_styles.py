"""自有快照样式阶段与 Python 参考逐字段差分，覆盖排序、装饰线及能力边界。"""
from io import BytesIO
import random

import pytest
from reportlab.pdfgen.canvas import Canvas

from docvortex._compute_backend import get_native
from docvortex.document.pdf import PDFDocument
from docvortex.analyzers.native.pdf.models import _AxisLine, _LineItem
from docvortex.analyzers.native.pdf.inline import detection, owned_styles


@pytest.fixture(scope="module")
def owned_page():
    """从真实 PDF 获得独立快照，全部测试均在文档关闭后消费字符与字体证据。"""
    if get_native() is None:
        pytest.skip("Python reference backend")
    output = BytesIO()
    canvas = Canvas(output, pagesize=(300, 300))
    for row, font in enumerate(["Helvetica", "Helvetica-Bold", "Times-Bold", "Courier", "Helvetica-Bold"]):
        canvas.setFont(font, 10 + row)
        canvas.drawString(20, 270 - row * 35, "A bc DE 12 - bullet text")
    canvas.save()
    with PDFDocument(output.getvalue()) as document:
        owner = document._extract_native_page(0).native_text
        assert owner is not None
        geometry = owner.materialize_geometry()
    return owner, geometry.chars, {id(char): i for i, char in enumerate(geometry.chars)}


@pytest.mark.parametrize("seed", range(100))
def test_owned_style_geometry_matches_reference(owned_page, seed):
    """随机组合乱序/重复成员、并列来源、旋转行和临界绘图线，完整比对区间及稳定顺序。"""
    owner, chars, identities = owned_page
    rng = random.Random(seed)
    lines = []
    drawings = []
    for index in range(rng.randrange(2, 12)):
        start = rng.randrange(len(chars) - 12)
        members = list(chars[start : start + rng.randrange(1, 13)])
        if rng.random() < 0.4:
            members.append(members[0])
        rng.shuffle(members)
        boxes = [tuple(char["bbox"].bbox) for char in members]
        box = (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))
        lines.append(_LineItem("unused", box, rng.choice([0, 0, 0, 90, 270]), rng.choice([0, 1, index]), chars=members))
        b = rng.choice(boxes)
        height = b[3] - b[1]
        y = rng.choice([b[3], (b[1] + b[3]) / 2, b[3] + height * 0.2, b[3] - height * 0.2])
        drawings.append(_AxisLine((box[0], y - 0.01, box[2], y + 0.01), height * rng.choice([0.0, 0.1, 0.2, 0.21]), rng.choice(["horizontal", "horizontal", "vertical"])))
    result = owned_styles.detect_owned_style_lines(owner, lines, drawings, identities)
    assert result is not None
    assert result == detection.detect_pdf_text_style_lines(lines, drawings)


def test_owned_style_missing_identity_uses_reference(owned_page):
    """调用方复制或替换字符时不能继续使用原快照的几何与字体。"""
    owner, chars, identities = owned_page
    line = _LineItem("A", (0.0, 0.0, 100.0, 30.0), 0, 0, chars=[dict(chars[0])])
    assert owned_styles.detect_owned_style_lines(owner, [line], [], identities) is None


def test_owned_style_invalid_index_propagates(owned_page):
    """身份映射损坏必须明确报错，不吞掉原生越界错误。"""
    owner, chars, _ = owned_page
    line = _LineItem("A", (0.0, 0.0, 100.0, 30.0), 0, 0, chars=[chars[0]])
    with pytest.raises(ValueError, match="member index"):
        owned_styles.detect_owned_style_lines(owner, [line], [], {id(chars[0]): len(chars)})


def test_owned_style_extreme_grid_uses_reference(owned_page):
    """超出有界网格的有限坐标明确返回能力不适用，不截断候选覆盖。"""
    owner, chars, identities = owned_page
    line = _LineItem("A", (0.0, 1e90, 100.0, 2e90), 0, 0, chars=chars[:3])
    rule = _AxisLine((0.0, 0.0, 100.0, 0.1), 0.1, "horizontal")
    assert owned_styles.detect_owned_style_lines(owner, [line], [rule], identities) is None


@pytest.mark.parametrize("text", ["ﬃ", "\x02", "\u200b", "\u00ad", "\u00a0", "😀", "•", "汉字", "\u0378", "A\tB"])
def test_snapshot_style_unicode_properties_match_host(text):
    """连字、控制符、补充平面和项目符号仍由宿主 Python 的 Unicode 规则定义。"""
    fragment, visible, space, marker = owned_styles._snapshot_text_properties(text)
    assert fragment == detection._normalize_match_fragment(text)
    assert visible == (text.isprintable() and not text.isspace())
    assert space == text.isspace()
    assert marker == (bool(fragment) and all(c in detection._PDF_LIST_MARKER_CHARS for c in fragment))


def test_public_style_input_mutation_does_not_change_snapshot(owned_page):
    """公开可变字符仍按当前字体值检测，修改不会反向写入 Rust 快照。"""
    owner, _, _ = owned_page
    chars = [char for char in owner.materialize_geometry().chars if detection._normalize_match_fragment(char["char"])][:2]
    boxes = [char["bbox"].bbox for char in chars]
    box = (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))
    line = _LineItem("A", box, 0, 0, chars=chars)
    assert detection.detect_pdf_text_style_lines([line], []) == []
    chars[0]["font"]["weight"] = 900
    assert detection.detect_pdf_text_style_lines([line], [])[0].style_ranges[0].styles == ("bold",)
    assert owner.materialize_geometry().chars[0]["font"]["weight"] != 900
