"""显式全零纵向步进的来源证据、条款顺序和裁剪前保护回归。"""

from dataclasses import replace
from io import BytesIO
import json
from zipfile import ZIP_DEFLATED, ZipFile

from lxml import etree
import pytest

from _ofd_test_utils import build_ofd_package, page_xml
from _span_test_utils import inline_text
from test_ofd_inline_layout import _repro_payload
from docvortex import parse
from docvortex.api import render
from docvortex.analyzers.native.ofd.geometry import Affine
from docvortex.analyzers.native.ofd.models import PageBuildContext
from docvortex.analyzers.native.ofd.package import OfdPackage
from docvortex.analyzers.native.ofd.scene import OfdSceneBuilder


def _with_body_delta(payload: bytes, delta_y: str) -> bytes:
    """仅给原条款正文添加纵向步进，保留反序、旋转和拆分条号夹具。"""
    output = BytesIO()
    with ZipFile(BytesIO(payload)) as source, ZipFile(output, "w", ZIP_DEFLATED) as target:
        for name in source.namelist():
            data = source.read(name)
            if name.endswith("Content.xml"):
                root = etree.fromstring(data)
                for code in root.xpath('//*[local-name()="TextObject" and @ID="22"]/*[local-name()="TextCode"]'):
                    code.set("DeltaY", delta_y)
                data = etree.tostring(root)
            target.writestr(name, data)
    return output.getvalue()


@pytest.mark.parametrize("delta_y", ["", " \t ", "0", "0 -0.0 0.0", "g 3 0"])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("angle", [0, 90, 180, 270])
@pytest.mark.parametrize("split_marker", [False, True])
def test_zero_delta_y_preserves_complete_article(delta_y, reverse, angle, split_marker) -> None:
    """零步进与省略属性的完整公开结果相同，条号、正文及续行组成同一条款。"""
    payload = _repro_payload(reverse=reverse, angle=angle, source_width=24, split_marker=split_marker)
    expected = parse(payload, file_suffix="ofd", keep_model_json=True)
    actual = parse(_with_body_delta(payload, delta_y), file_suffix="ofd", keep_model_json=True)
    assert actual.model_json.to_dict() == expected.model_json.to_dict()
    assert actual.to_dict() == expected.to_dict()
    blocks = actual.model_json.pages[0]
    assert [len(block["lines"]) for block in blocks] == [1, 2]
    assert inline_text(blocks[1]["content"]).startswith("第一条这是一段")
    for fmt in ("markdown", "html", "structured_content"):
        assert render(actual.middle_json, fmt).content == render(expected.middle_json, fmt).content
    public = json.dumps([actual.model_json.to_dict(), actual.to_dict()], ensure_ascii=False)
    assert "has_nonzero_delta_y" not in public


@pytest.mark.parametrize(
    "delta_y,has_nonzero,source_allowed,token_count",
    [
        (None, False, True, 2),
        ("  ", False, True, 2),
        ("0 -0", False, True, 4),
        ("g 3 0", False, True, 5),
        ("2 0", True, False, 4),
        ("2 -2", True, False, 4),
        ("20 0", True, False, 4),
    ],
)
def test_delta_evidence_covers_unclipped_positions(delta_y, has_nonzero, source_allowed, token_count) -> None:
    """裁剪前的非零步进持续阻止来源证明，压缩步进只在字形构建时计费一次。"""
    attribute = "" if delta_y is None else f' DeltaY="{delta_y}"'
    content = (
        '<ofd:TextObject ID="1" Boundary="10 10 30 6" Font="1" Size="5">'
        f'<ofd:TextCode X="1" Y="5" DeltaX="5 5"{attribute}>甲乙丙</ofd:TextCode></ofd:TextObject>'
    )
    with OfdPackage(build_ofd_package([("Pages/Page_0/Content.xml", page_xml(content))])) as package:
        builder = OfdSceneBuilder(package)
        line = builder.build()[0].text_lines[0]
        assert line.has_nonzero_delta_y is has_nonzero
        assert (line.source_bbox is not None) is source_allowed
        assert (line.source_order is not None) is source_allowed
        assert builder.text_budget.delta_token_count == token_count
        if delta_y == "20 0":
            assert line.text == "甲"
        if source_allowed and delta_y and delta_y.strip():
            # 未经当前文字构建器确认的显式步进不能仅凭可见字形获得来源信息。
            unknown = replace(line, source_bbox=None, source_order=None, has_nonzero_delta_y=None)
            element = etree.fromstring(page_xml(content).encode()).xpath('//*[local-name()="TextObject"]')[0]
            builder._attach_text_source_bbox(element, PageBuildContext(Affine(), (0, 0, 100, 100), "Body", None), [unknown])
            assert unknown.source_bbox is None
