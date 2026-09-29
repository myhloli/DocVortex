"""连续原生组行与 Python 参考实现的契约差分。"""

from __future__ import annotations

import math
import random

import pytest

from docvortex.document.pdf.text import _get_lines_from_chars_python
from docvortex.document.pdf.text._contracts import Bbox


def _char(text, box, index, font=None, rotation=0.0):
    """构造含可选来源字段的独立字符。"""
    return {
        "char": text,
        "bbox": Bbox([float(value) for value in box]),
        "rotation": rotation,
        "font": font if font is not None else {"name": "test", "size": 10},
        "char_idx": index,
        "source_indices": (index, index + 1000),
        "text_object_id": 7,
        "text_is_visible": True,
    }


def _plain(value):
    """仅转换矩形用于结构比较，不抹去原始字段。"""
    if isinstance(value, Bbox):
        return value.bbox
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def _assert_equivalent(chars, threshold=0.7, distance=0.1):
    """比较全部字段并验证字符与字体引用未被重建。"""
    native = pytest.importorskip("docvortex._native")
    actual = native.group_text_lines(chars, threshold, distance)
    expected = _get_lines_from_chars_python(chars, threshold, distance)
    assert actual is not None, "普通浮点字符必须真正执行 Rust，不能通过参考回退掩盖差异"
    assert _plain(actual) == _plain(expected)
    offset = 0
    for line in actual:
        for span in line["spans"]:
            assert span["font"] is chars[offset]["font"]
            for char in span["chars"]:
                assert char is chars[offset]
                offset += 1
    assert offset == len(chars)
    return actual


@pytest.mark.parametrize("marker", ["2", "²", "٣", "Ⅳ", "⅓", "∑", "α", "𝟙", "中", "12", "²³", "", "\x1c", "\x0b"])
def test_unicode_script_equivalence(marker):
    """覆盖数码、数学符号、Python 特有空白和多字符上下标。"""
    chars = [
        _char("body", (0, 5, 30, 15), 0),
        _char(marker, (31, 0, 36, 6), 1, {"name": "small", "size": 6}),
        _char("tail", (37, 5, 65, 15), 2),
    ]
    _assert_equivalent(chars)


def test_source_reference_and_mutation_isolation():
    """片段框独立累加，返回字符保持原身份及附加元数据。"""
    font = {"name": "test", "size": 10, "flags": [1, 2]}
    chars = [_char("a", (0, 0, 10, 10), 5, font), _char("b", (10, 0, 20, 10), 99, dict(font))]
    actual = _assert_equivalent(chars)
    assert actual[0]["spans"][0]["char_start_idx"] == 5
    assert actual[0]["spans"][0]["char_end_idx"] == 99
    actual[0]["spans"][0]["bbox"].bbox[0] = -100
    assert chars[0]["bbox"].bbox[0] == 0


def test_breaks_empty_and_rotations():
    """覆盖换行、空输入、垂直文本及负尺度造成的半周旋转。"""
    _assert_equivalent([])
    for rotation in [0, math.pi / 4, math.pi / 2, math.pi, 1.5 * math.pi, -math.pi]:
        chars = [
            _char("a", (0, 0, 10, 10), 0),
            _char("b", (10, 0, 20, 10), 1, rotation=rotation),
            _char("\x02", (20, 0, 21, 10), 2, rotation=rotation),
            _char("next", (0, 12, 20, 22), 3),
        ]
        _assert_equivalent(chars)


def test_seeded_geometry_differential():
    """随机多字体和几何组合验证连续阶段保持全部分支行为。"""
    rng = random.Random(915)
    alphabet = ["a", " ", "²", "∑", "⅓", "\n", "\x02", "\u2009", "٣", "12"]
    fonts = [{"name": name, "size": size} for name in ["A", "B"] for size in [5, 10]]
    for _ in range(60):
        chars = []
        for i in range(rng.randrange(1, 100)):
            x, y = rng.randrange(0, 100), rng.randrange(0, 30)
            chars.append(
                _char(
                    rng.choice(alphabet),
                    (x, y, x + rng.randrange(1, 12), y + rng.randrange(1, 20)),
                    i * 2,
                    rng.choice(fonts),
                    rng.choice([0, 0, 0, math.pi, math.pi / 2]),
                )
            )
        _assert_equivalent(chars, rng.choice([0.7, 0.8]), rng.choice([0.05, 0.1, 0.2]))


def _line_records(items):
    """展开视觉行全部字段，避免只比较文本掩盖排版差异。"""
    from dataclasses import fields

    return [{field.name: _plain(getattr(item, field.name)) for field in fields(item) if field.compare} for item in items]


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_fused_visual_lines(rotation):
    """对比融合管线与先物化粗行路径，包括公式及多个视觉 run。"""
    pytest.importorskip("docvortex._native")
    from docvortex.analyzers.native.pdf.native_text import (
        _build_native_line_items,
        _build_native_line_items_from_chars,
    )

    chars = []
    for row, angle in enumerate([0.0, math.pi, math.pi / 2, -math.pi / 2, 0.12]):
        for i, text in enumerate(["A", "b", "c", "d", " ", "∑", "²", "\n"]):
            x = i * 8 + (80 if i > 4 else 0)
            y = 20 + row * 30
            chars.append(_char(text, (x, y, x + 6, y + 10), len(chars), rotation=angle))
    expected = _build_native_line_items(_get_lines_from_chars_python(chars), (300, 300), page_rotation=rotation)
    actual = _build_native_line_items_from_chars(chars, (300, 300), page_rotation=rotation)
    assert _line_records(actual) == _line_records(expected)
    originals = {id(char) for char in chars}
    assert all(id(char) in originals for item in actual for char in item.chars)


def test_fused_seeded_differential():
    """覆盖多字体、字符方向与分隔几何的完整视觉行差分。"""
    pytest.importorskip("docvortex._native")
    from docvortex.analyzers.native.pdf.native_text import (
        _build_native_line_items,
        _build_native_line_items_from_chars,
    )

    rng = random.Random(918)
    fonts = [{"name": "Helvetica", "size": 10}, {"name": "Times", "size": 6}]
    for _ in range(30):
        chars = []
        for row in range(6):
            angle = rng.choice([0.0, 0.0, math.pi / 2, math.pi, 0.12])
            for i in range(20):
                x = i * 7 + (40 if i > 10 else 0)
                y = row * 30 + rng.choice([0, 0, 0, 3])
                chars.append(
                    _char(
                        rng.choice(["a", "2", " ", "中", "⅓", "∑", "\x02", "\n"]),
                        (x, y, x + 6, y + 10),
                        len(chars),
                        rng.choice(fonts),
                        angle,
                    )
                )
        expected = _build_native_line_items(_get_lines_from_chars_python(chars), (300, 300))
        actual = _build_native_line_items_from_chars(chars, (300, 300))
        assert _line_records(actual) == _line_records(expected)


def test_explicit_special_object_fallback():
    """自定义映射与框在进入原生算法前明确返回参考路径。"""
    native = pytest.importorskip("docvortex._native")
    from docvortex.document.pdf.text import get_lines_from_chars

    class CustomChar(dict):
        """代表调用方自定义的字符映射。"""

    class CustomBbox(Bbox):
        """代表调用方自定义的几何对象。"""

    original = _char("a", (0, 0, 10, 10), 0)
    for char in [CustomChar(original), {**original, "bbox": CustomBbox([0, 0, 10, 10])}, {**original, "char": "\ud800"}]:
        assert native.group_text_lines([char], 0.7, 0.1) is None
        assert _plain(get_lines_from_chars([char])) == _plain(_get_lines_from_chars_python([char]))


def test_custom_supported_sequence_and_empty_input():
    """空页、自定义方向序列和空白字符遵守参考入口行为。"""
    pytest.importorskip("docvortex._native")
    from docvortex.analyzers.native.pdf.native_text import (
        _build_native_line_items,
        _build_native_line_items_from_chars,
    )

    for chars in [[], [_char(" ", (0, 0, 10, 10), 0)], [_char("A", (0, 0, 10, 10), 0)]]:
        for supported in [(0, 90, 270), range(0, 360, 90), (45, 135)]:
            expected = _build_native_line_items(_get_lines_from_chars_python(chars), (100, 100), supported_angles=supported)
            actual = _build_native_line_items_from_chars(chars, (100, 100), supported_angles=supported)
            assert _line_records(actual) == _line_records(expected)


@pytest.mark.parametrize("separator", ["\t", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x1f", " "])
def test_python_ascii_whitespace_soft_gap(separator):
    """包括 Rust 标准空白表缺少的 VT，验证 Python 空白触发软间隙拆分。"""
    pytest.importorskip("docvortex._native")
    from docvortex.analyzers.native.pdf.native_text import (
        _build_native_line_items,
        _build_native_line_items_from_chars,
    )

    chars = [_char("A", (0, 0, 1, 10), 0), _char(separator, (2, 0, 3, 10), 1), _char("B", (10, 0, 11, 10), 2)]
    expected = _build_native_line_items(_get_lines_from_chars_python(chars), (100, 100))
    actual = _build_native_line_items_from_chars(chars, (100, 100))
    assert len(expected) == 2
    assert _line_records(actual) == _line_records(expected)


@pytest.mark.parametrize("rotation", [False, True, 0, 1, -1, 0.0, -0.0, 0.125])
def test_rotation_type_and_source_identity(rotation):
    """原生输出复用源 rotation，保留 bool/int/float 及其序列化类型。"""
    import json

    chars = [_char("a", (0, 0, 10, 10), 0, rotation=rotation), _char("b", (10, 0, 20, 10), 1, rotation=rotation)]
    actual = _assert_equivalent(chars)
    expected = _get_lines_from_chars_python(chars)
    assert actual[0]["rotation"] is rotation
    assert actual[0]["spans"][0]["rotation"] is rotation
    assert json.dumps(_plain(actual)) == json.dumps(_plain(expected))


@pytest.mark.parametrize(
    "coordinates", [[0, 0, 10, 10], [False, False, True, True], [0.0, 0, 10.0, 10.0], [0, 0, 10**400, 10**400]]
)
def test_nonfloat_geometry_explicit_fallback(coordinates):
    """整数、布尔与超大整数框保持参考类型，不能先转换双精度再回退。"""
    import json
    from docvortex.document.pdf.text import get_lines_from_chars

    native = pytest.importorskip("docvortex._native")
    char = _char("a", (0, 0, 10, 10), 0)
    char["bbox"] = Bbox(coordinates)
    assert native.group_text_lines([char], 0.7, 0.1) is None
    actual = get_lines_from_chars([char])
    expected = _get_lines_from_chars_python([char])
    assert json.dumps(_plain(actual)) == json.dumps(_plain(expected))
    assert [type(value) for value in actual[0]["bbox"].bbox] == [type(value) for value in coordinates]


def test_large_rotation_explicit_fallback():
    """单字符超大方向值无需算术，原入口必须保留而非提前浮点溢出。"""
    from docvortex.document.pdf.text import get_lines_from_chars

    native = pytest.importorskip("docvortex._native")
    char = _char("a", (0, 0, 10, 10), 0, rotation=10**400)
    assert native.group_text_lines([char], 0.7, 0.1) is None
    assert get_lines_from_chars([char])[0]["rotation"] is char["rotation"]


def test_threshold_objects_do_not_trigger_eager_native_conversion(monkeypatch):
    """自定义阈值在参考计算真正需要它时才求值，空输入不触发类型转换。"""
    from docvortex.document.pdf.text import get_lines_from_chars

    class NativeMustNotRun:
        """拒绝被自定义数值路径调用，防止回退测试误用原生转换。"""

        def group_text_lines(self, *args):
            """若包装器提前调用原生入口，则立即暴露兼容性错误。"""
            raise AssertionError("custom threshold reached native conversion")

    class Threshold(float):
        """允许原有浮点运算，但禁止新的显式 float 转换。"""

        def __float__(self):
            """拒绝新增的提前转换。"""
            raise AssertionError("eager conversion")

    monkeypatch.setattr("docvortex._compute_backend.get_native", lambda: NativeMustNotRun())
    for height, distance in [(Threshold(0.7), 0.1), (0.7, Threshold(0.1)), (10**400, 0.1), (None, None)]:
        assert get_lines_from_chars([], height, distance) == []
    chars = [_char("a", (0, 0, 10, 10), 0), _char("b", (10, 0, 20, 10), 1, {"name": "other", "size": 10})]
    for height, distance in [(Threshold(0.7), 0.1), (0.7, Threshold(0.1))]:
        assert _plain(get_lines_from_chars(chars, height, distance)) == _plain(
            _get_lines_from_chars_python(chars, height, distance)
        )
    for height, distance in [(None, 0.1), (0.7, None)]:
        errors = []
        for function in [get_lines_from_chars, _get_lines_from_chars_python]:
            try:
                function(chars, height, distance)
            except Exception as error:
                errors.append((type(error), str(error)))
        assert len(errors) == 2 and errors[0] == errors[1]


def test_real_pdf_still_uses_native_float_path():
    """真实 PDF 提取字符必须直接命中 Rust，防止保守类型边界导致全页回退。"""
    from pathlib import Path
    from docvortex.document.pdf import PDFDocument

    native = pytest.importorskip("docvortex._native")
    source = Path(__file__).parents[1] / "demo/pdfs/demo1.pdf"
    with PDFDocument(str(source)) as document:
        for page in range(document.page_count):
            chars = document.get_page_chars(page)
            assert native.group_text_lines(chars, 0.7, 0.1) is not None
            assert (
                native.prepare_visual_lines(chars, document.page_size(page), document.page_rotation(page), [0.0, 90.0, 270.0])
                is not None
            )


def test_line_rotation_comes_from_first_span_when_equal_values_have_different_types():
    """同值 bool/float 可合行，但各片段及行保留各自首字符的原类型。"""
    import json

    chars = [
        _char("a", (0, 0, 10, 10), 0, rotation=True),
        _char("b", (10, 0, 20, 10), 1, {"name": "other", "size": 10}, rotation=1.0),
    ]
    actual = _assert_equivalent(chars)
    assert len(actual) == 1 and len(actual[0]["spans"]) == 2
    assert actual[0]["rotation"] is True
    assert actual[0]["spans"][0]["rotation"] is True
    assert actual[0]["spans"][1]["rotation"] is chars[1]["rotation"]
    assert json.dumps(_plain(actual)) == json.dumps(_plain(_get_lines_from_chars_python(chars)))


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "\r\n",
        "\x00\x02\x7f\x9f",
        " \t\v\f ",
        "  Alpha  beta  ",
        "A\r\nB\rC\nD",
        "A\u0085B\u2028C\u2029D",
        "A\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u202f\u205f\u3000B",
        "A\u200b\u2060\ufeffB",
        "word\xad",
        "word\xad \t\nnext",
        "word\x02\t\nnext",
        "1\xad\nnext",
        "中\x02\nnext",
        "word\xad\v\nnext",
        "word\xad\x00\nnext",
        "A\u200b\xad\u00a0\nB",
        "a \x00  b",
        "a\xad\xad",
        "a\x02\xad",
        "A.\u200b",
        "A!）】》”’'\"",
        "\u001cA\u001f",
        "A\x02\r\nB",
    ],
)
def test_run_normalization_matches_reference_order(raw):
    """覆盖清洗步骤先后依赖、Unicode 空白和软断词的上下文断言。"""
    native = pytest.importorskip("docvortex._native")
    from docvortex.analyzers.native.pdf.native_text import (
        _build_native_line_items,
        _build_native_line_items_from_chars,
        _normalize_native_run_text,
    )

    chars = [_char(raw, (0, 0, 100, 10), 0)]
    records = native.prepare_visual_lines(chars, (200.0, 200.0), 0, [0.0, 90.0, 270.0])
    assert records is not None
    text = _normalize_native_run_text(raw)
    assert [record[0] for record in records] == ([text] if text else [])
    if raw == "A.\u200b":
        assert records[0][8] is True and records[0][10] is False
    expected = _build_native_line_items(_get_lines_from_chars_python(chars), (200, 200))
    actual = _build_native_line_items_from_chars(chars, (200, 200))
    assert _line_records(actual) == _line_records(expected)


def _terminal_chars(text, reference_count, reference_height, angle):
    """构造保持字符记录粒度的水平或竖排句尾引用样本。"""
    chars = []
    for index, ch in enumerate(text):
        height = reference_height if reference_count and index >= len(text) - reference_count else 10.0
        box = (index * 6.0, 10.0, index * 6.0 + 5.0, 10.0 + height)
        if angle in {90, 270}:
            box = (10.0, index * 6.0, 10.0 + height, index * 6.0 + 5.0)
        chars.append(_char(ch, box, index, rotation=math.radians(angle)))
    return chars


@pytest.mark.parametrize(
    "text,count,height,terminal",
    [
        ("End.", 0, 10.0, True),
        ("End!）】》”’'\"", 0, 10.0, True),
        ("End?]})", 0, 10.0, True),
        ("正文。", 0, 10.0, True),
        ("Not terminal", 0, 10.0, False),
        ("End.12", 2, 8.0, True),
        ("End.12", 2, 8.000001, False),
        ("End.12", 2, 10.0, False),
        ("End.1,2–3—4-", 8, 6.0, True),
        ("End.-12", 2, 6.0, False),
        ("End.١٢", 2, 6.0, True),
        ("End.𝟙𝟚", 2, 6.0, True),
        ("End.²³", 2, 6.0, False),
        ("End.Ⅳ", 1, 6.0, False),
        ("End.1)", 2, 6.0, False),
        ("Value 1.23", 2, 10.0, False),
    ],
)
@pytest.mark.parametrize("angle", [0, 90, 270])
def test_native_sentence_terminal_parity(text, count, height, terminal, angle):
    """保留十进制引用、字高阈值、闭合标点和竖排轴选择的精确判断。"""
    native = pytest.importorskip("docvortex._native")
    from docvortex.analyzers.native.pdf.native_text import (
        _build_native_line_items,
        _build_native_line_items_from_chars,
    )

    chars = _terminal_chars(text, count, height, angle)
    records = native.prepare_visual_lines(chars, (1000.0, 1000.0), 0, [0.0, 90.0, 270.0])
    assert records is not None and len(records) == 1
    assert records[0][10] is terminal
    expected = _build_native_line_items(_get_lines_from_chars_python(chars), (1000, 1000))
    actual = _build_native_line_items_from_chars(chars, (1000, 1000))
    assert _line_records(actual) == _line_records(expected)


def test_sentence_reference_length_counts_records_not_text_codepoints():
    """多码点字符记录不得被错误拆成多个引用字形。"""
    native = pytest.importorskip("docvortex._native")
    from docvortex.analyzers.native.pdf.native_text import (
        _build_native_line_items,
        _build_native_line_items_from_chars,
    )

    chars = [_char("A.", (0, 0, 12, 10), 0), _char("12", (12, 0, 24, 6), 1)]
    records = native.prepare_visual_lines(chars, (100.0, 100.0), 0, [0.0, 90.0, 270.0])
    assert records is not None and len(records) == 1 and records[0][10] is False
    expected = _build_native_line_items(_get_lines_from_chars_python(chars), (100, 100))
    actual = _build_native_line_items_from_chars(chars, (100, 100))
    assert _line_records(actual) == _line_records(expected)


def test_initial_native_run_finalization_does_not_call_python_reference(monkeypatch):
    """原生初次物化无需再调用 Python 清洗和句尾规则，后继合并仍保留原入口。"""
    native = pytest.importorskip("docvortex._native")
    monkeypatch.setattr("docvortex._compute_backend.get_native", lambda: native)
    from docvortex.analyzers.native.pdf import native_text

    chars = _terminal_chars("End.12", 2, 6.0, 0)
    expected = native_text._build_native_line_items(_get_lines_from_chars_python(chars), (100, 100))

    def forbidden(*args, **kwargs):
        """捕获本应在 Rust 完成的初次 run 规则回调。"""
        raise AssertionError("initial native finalization called Python reference")

    monkeypatch.setattr(native_text, "_normalize_native_run_text", forbidden)
    monkeypatch.setattr(native_text, "_native_sentence_terminal", forbidden)
    actual = native_text._build_native_line_items_from_chars(chars, (100, 100))
    assert _line_records(actual) == _line_records(expected)


def test_seeded_native_run_normalization_differential():
    """组合随机控制字符验证删除、替换与空白折叠的顺序不变。"""
    native = pytest.importorskip("docvortex._native")
    from docvortex.analyzers.native.pdf.native_text import _normalize_native_run_text

    rng = random.Random(927)
    alphabet = [
        "A",
        "z",
        "1",
        "中",
        ".",
        " ",
        "\t",
        "\n",
        "\r",
        "\v",
        "\f",
        "\x00",
        "\x02",
        "\u0085",
        "\u00ad",
        "\u00a0",
        "\u200b",
        "\u2028",
        "\u2029",
        "\u2060",
        "\ufeff",
    ]
    for _ in range(250):
        raw = "".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 60)))
        chars = [_char(raw, (0, 0, 100, 10), 0)]
        records = native.prepare_visual_lines(chars, (200.0, 200.0), 0, [0.0, 90.0, 270.0])
        assert records is not None
        expected = _normalize_native_run_text(raw)
        assert [record[0] for record in records] == ([expected] if expected else [])


def test_empty_coarse_run_filter_preserves_source_row_ids():
    """粗行清洗后为空时只过滤该行，保留后续来源行号与 coarse 句尾默认值。"""
    native = pytest.importorskip("docvortex._native")
    from docvortex.analyzers.native.pdf.native_text import (
        _build_native_line_items,
        _build_native_line_items_from_chars,
    )

    raw_rows = [" \n", "A.\n", "\u200b\n", "B!\n"]
    chars = [
        _char(text, (0, index * 20, 30, index * 20 + 10), index, {"name": f"row-{index}", "size": 10})
        for index, text in enumerate(raw_rows)
    ]
    records = native.prepare_visual_lines(chars, (200.0, 200.0), 0, [0.0, 90.0, 270.0])
    assert records is not None
    assert all(len(record) == 11 for record in records)
    assert [(record[0], record[4], record[5], record[8], record[10]) for record in records] == [
        ("A.", 1, 0, True, False),
        ("B!", 3, 0, True, False),
    ]
    expected = _build_native_line_items(_get_lines_from_chars_python(chars), (200, 200))
    actual = _build_native_line_items_from_chars(chars, (200, 200))
    assert _line_records(actual) == _line_records(expected)


def test_empty_split_run_filter_preserves_run_index_and_split_flag():
    """已产生视觉分段后过滤空文本，不能重编号或抹去原拆分标记。"""
    native = pytest.importorskip("docvortex._native")
    from docvortex.analyzers.native.pdf.native_text import (
        _build_native_line_items,
        _build_native_line_items_from_chars,
    )

    chars = [_char("", (0, 0, 5, 10), 0), _char("A", (100, 0, 105, 10), 1)]
    records = native.prepare_visual_lines(chars, (200.0, 200.0), 0, [0.0, 90.0, 270.0])
    assert records is not None and len(records) == 1 and len(records[0]) == 11
    record = records[0]
    assert (record[0], record[4], record[5], record[6], record[8], record[10]) == ("A", 0, 1, True, False, False)
    assert record[3][0] is chars[1]
    expected = _build_native_line_items(_get_lines_from_chars_python(chars), (200, 200))
    actual = _build_native_line_items_from_chars(chars, (200, 200))
    assert _line_records(actual) == _line_records(expected)
