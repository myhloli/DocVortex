"""锁定宽行距论文中空白分隔及多位数字引用的完整上标。"""

from copy import deepcopy
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from docvortex.analyzers.native.pdf._script_geometry import classify_char_script_roles
from docvortex.analyzers.native.pdf.geometry import _rotate_bbox_from_upright
from docvortex.analyzers.native.pdf.inline.scripts import _script_line_char_roles
from docvortex.analyzers.pdf import prepare_text_evidence
from docvortex.api import parse
from docvortex.document.pdf import PDFDocument
from docvortex.render import render_html, render_markdown

SOURCE = Path(__file__).parent / "unittest/pdfs/numeric_citations_introduction.pdf"
SOURCE_SHA256 = "424512a5c2c3a58c542e05f6692cb0efe3b4009b308b787db7f56806e77a7e18"
CASES = [
    (0, "worldwide.", ["1,2"]),
    (1, "healthcaresystems.3", ["3"]),
    (1, "2040.", ["4"]),
    (1, "follow-up,", ["5,6"]),
    (1, "productivity,", ["7,8"]),
    (1, "fractures.", ["9,10"]),
    (1, "conditions.", ["11"]),
    (1, "billion.", ["12"]),
    (2, "forAMD,", ["13,14", "15"]),
    (2, "model,", ["16,17", "18,19"]),
]
EXPECTED = [value for _, _, values in CASES for value in values]


@pytest.fixture(scope="module")
def source_evidence():
    """从冻结的三页原件提取证据，检查样本身份且复用只读结果。"""
    assert hashlib.sha256(SOURCE.read_bytes()).hexdigest() == SOURCE_SHA256
    with PDFDocument(str(SOURCE)) as document:
        assert len(document) == 3
        return [prepare_text_evidence(document[index]) for index in range(3)]


@pytest.mark.parametrize("page,anchor,expected", CASES)
def test_real_citation_evidence_is_complete(source_evidence, page, anchor, expected):
    """逐个原文锚点验证所有引用及逗号，不以局部字符或总数代替正确性。"""
    matches = [line for line in source_evidence[page].scripts if anchor in line.text]
    assert len(matches) == 1
    line = matches[0]
    assert [(line.text[item.start : item.end], item.style) for item in line.script_ranges] == [
        (value, "superscript") for value in expected
    ]


def test_real_citations_survive_public_export():
    """公开 Model/Middle/HTML/Markdown 都保留十二组完整引用且不提升其他正文。"""
    result = parse(SOURCE, keep_model_json=True)
    for markup in (render_html(result.middle_json), render_markdown(result.middle_json)):
        for value in EXPECTED:
            assert markup.count(f"<sup>{value}</sup>") == 1
        assert markup.count("<sup>") == len(EXPECTED)
        assert "<sub>" not in markup
    for document in (result.model_json.to_dict(), result.middle_json.to_dict()):
        scripts = []

        def visit(value):
            """递归收集公共协议内的上下标，覆盖嵌套段落且不依赖块编号。"""
            if isinstance(value, dict):
                if set(value.get("styles", [])) & {"superscript", "subscript"}:
                    scripts.append((value["content"], value["styles"]))
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(document)
        assert scripts == [(value, ["superscript"]) for value in EXPECTED]


def _citation_fixture(reference="word.", citation="13,14", separator=" ", shift=-4.0):
    """建立有明确正文参照的双 bbox 字符，引用中的标点保持同一基线。"""
    text = reference + separator + citation + " next"
    start, end = len(reference + separator), len(reference + separator + citation)
    chars, tight, origins = [], {}, {}
    for index, char in enumerate(text):
        scripted = start <= index < end
        x = 20.0 + index * 5.0
        baseline = 60.0 + (shift if scripted else 0)
        height = 6.0 if scripted else 9.0
        box = (x, baseline - height - 3.0, x + 4.0, baseline + 2.0)
        chars.append({"char": char, "char_idx": index, "bbox": box, "font": {"name": "arbitrary"}})
        tight[index] = (x, baseline - height, x + 4.0, baseline)
        origins[index] = (x, baseline)
    return chars, tight, origins, start, end


@pytest.mark.parametrize("citation", ["3", "11", "13,14", "18–20"])
@pytest.mark.parametrize("separator", ["", " ", "\u00a0"])
@pytest.mark.parametrize("angle", [0, 90, 180, 270])
@pytest.mark.parametrize("scale,translation", [(0.75, 0), (1.0, 75), (1.6, 0)])
def test_numeric_citations_generalize(citation, separator, angle, scale, translation):
    """有无空白、引用长度、字号、平移和旋转均不改变正文及引用角色。"""
    chars, tight, origins, start, end = _citation_fixture(citation=citation, separator=separator)
    size = (800.0, 1000.0)

    def transform(box):
        """同步变换两类 bbox，保持样式判断不依赖绝对位置。"""
        return _rotate_bbox_from_upright(tuple(v * scale + translation for v in box), size, angle)

    for char in chars:
        char["bbox"] = transform(char["bbox"])
        char["font"]["name"] = "different-subset" if char["char_idx"] % 2 else "another-font"
    tight = {index: transform(box) for index, box in tight.items()}
    for index, (x, y) in origins.items():
        x, y = x * scale + translation, y * scale + translation
        origins[index] = {0: (x, y), 90: (size[0] - y, x), 180: (size[0] - x, size[1] - y), 270: (y, size[1] - x)}[angle]
    before = deepcopy((chars, tight, origins))
    _, roles, _, _ = _script_line_char_roles(
        SimpleNamespace(chars=chars, angle=angle, inline_math_regions=[]), size, tight, origins, set()
    )
    assert roles == ["sup" if start <= index < end else "body" for index in range(len(chars))]
    assert (chars, tight, origins) == before


@pytest.mark.parametrize("mode", ["same-baseline", "weak", "newline", "column", "row", "missing", "no-anchor"])
def test_numeric_citation_requires_local_geometry(mode):
    """普通数字、弱偏移、跨行跨栏、缺失证据和孤立数字不可借用正文锚点。"""
    chars, tight, origins, start, end = _citation_fixture(
        reference="1." if mode == "no-anchor" else "word.",
        citation="2021",
        separator="\n" if mode == "newline" else " ",
        shift=0 if mode == "same-baseline" else -0.5 if mode == "weak" else -25 if mode == "row" else -4,
    )
    for index in range(start, end):
        if mode == "missing":
            origins.pop(index)
        elif mode == "column":
            box = chars[index]["bbox"]
            chars[index]["bbox"] = (box[0] + 100, box[1], box[2] + 100, box[3])
            box = tight[index]
            tight[index] = (box[0] + 100, box[1], box[2] + 100, box[3])
            origins[index] = (origins[index][0] + 100, origins[index][1])
    roles = classify_char_script_roles(chars, tight_bboxes=tight, origins=origins)
    assert roles[start:end] == ["body"] * (end - start)


def test_inline_math_retains_its_body_base():
    """数字幂的基数留在正文，不将整个数学 token 强制保护为引用。"""
    chars, tight, origins, start, end = _citation_fixture(reference="10", citation="12", separator="")
    _, roles, _, _ = _script_line_char_roles(
        SimpleNamespace(chars=chars, angle=0, inline_math_regions=[]), (800, 1000), tight, origins, set()
    )
    assert roles[:end] == ["body"] * start + ["sup"] * (end - start)


@pytest.mark.parametrize("scaled_loose", [True, False])
def test_lowercase_body_requires_complementary_loose_evidence(scaled_loose):
    """正文 x-height 接近数字时，须同时具备强位移及较小 loose 框才能补判。"""
    chars, tight, origins, start, end = _citation_fixture(reference="losses,", citation="18,19")
    for index in range(start):
        box = tight[index]
        tight[index] = (box[0], box[3] - 6, box[2], box[3])
    if not scaled_loose:
        for index in range(start, end):
            x0, _, x1, y1 = chars[index]["bbox"]
            chars[index]["bbox"] = (x0, y1 - 14, x1, y1)
    roles = classify_char_script_roles(chars, tight_bboxes=tight, origins=origins)
    assert roles[start:end] == ["sup" if scaled_loose else "body"] * (end - start)


@pytest.mark.parametrize("reference", ["SK", "σT", "word. "])
def test_numeric_protection_does_not_bypass_fraction_filter(reference):
    """分式指数仍由原数学过滤处理，不因保护数字而只留下分子上标。"""
    chars, tight, origins, start, end = _citation_fixture(reference=reference, citation="1/2", separator="")
    _, roles, _, _ = _script_line_char_roles(
        SimpleNamespace(chars=chars, angle=0, inline_math_regions=[]), (800, 1000), tight, origins, set()
    )
    assert roles[start:end] == ["body"] * (end - start)


def test_reviewed_klee_footnote_survives_public_export():
    """锁定原页已确认的 KLEE 数字脚注改善，保持后续句号为正文。"""
    source = Path(__file__).parents[1] / "demo/pdfs/mixed_elements_pages_07_10.pdf"
    assert hashlib.sha256(source.read_bytes()).hexdigest() == "89076ee9cce2168ab894ee7e280872f8a3c09053b9dcdeda9c1da76d111adc1e"
    result = parse(source)
    assert "KLEE <sup>1</sup> . KLEE" in render_html(result.middle_json)
