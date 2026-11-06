"""验证渲染合并后的物理来源及子块身份。"""

import json
from copy import deepcopy

import pytest
from bs4 import BeautifulSoup

from docvortex.render import HtmlRenderOptions, RenderFormat, RenderMode, render, render_html
from docvortex.schema import MiddleJson


BOX = [0.1, 0.2, 0.9, 0.4]


def _text(value: str, **kwargs: object) -> dict:
    """构造带原始几何信息的正文块。"""
    return dict(type="text", index=0, bbox=BOX, content=[dict(type="text", content=value)], **kwargs)


def _middle(*pages: list[dict]) -> MiddleJson:
    """使用非连续原始页号验证抽页及跨页身份。"""
    return MiddleJson.model_validate(
        dict(
            pages=[dict(page_idx=index * 3 + 2, blocks=blocks) for index, blocks in enumerate(pages)],
            is_full_document=False,
            metadata=dict(file_suffix="pdf", producer=dict(name="test", version="1")),
        )
    )


def _targets(middle: MiddleJson, mode: RenderMode = RenderMode.DEFAULT) -> list:
    """从真实公共入口输出提取全部来源节点。"""
    before = middle.model_dump()
    output = render(middle, RenderFormat.HTML, options=HtmlRenderOptions(include_source_map=True, mode=mode))
    assert middle.model_dump() == before
    soup = BeautifulSoup(output, "html.parser")
    nodes = soup.select("[data-docvortex-trace-id]")
    assert len({node["data-docvortex-trace-id"] for node in nodes}) == len(nodes)
    return [(node, json.loads(node["data-docvortex-sources"])) for node in nodes]


def test_continued_text_and_full_mode() -> None:
    """默认续段累计来源，FULL 模式按实际分段分别提供来源。"""
    middle = _middle([_text("first")], [_text("second", continues_prev=True)])
    nodes = _targets(middle)
    assert len(nodes) == 1
    assert [ref["page_idx"] for ref in nodes[0][1]] == [2, 5]
    assert len(_targets(middle, RenderMode.FULL)) == 2
    assert "data-docvortex-sources" not in render_html(middle)
    with pytest.raises(TypeError):
        render_html(middle, include_source_map=1)


def test_list_children_and_tail_continuation() -> None:
    """跨页重复子块索引用原始子路径区分，正文续文定位到列表末项。"""
    first = dict(type="list", index=0, bbox=BOX, sub_type="text", content=[_text("1. first")])
    second = dict(type="list", index=0, bbox=BOX, sub_type="text", continues_prev=True, content=[_text("2. second")])
    middle = _middle([first], [second], [_text("tail", continues_prev=True)])
    nodes = _targets(middle)
    leaves = [(node, refs) for node, refs in nodes if node.name == "li"]
    assert len(leaves) == 2
    assert leaves[0][1][0]["page_idx"] == 2
    assert [ref["page_idx"] for ref in leaves[1][1]] == [5, 8]
    assert leaves[0][1][0]["child_path"] == [0]
    assert "tail" in leaves[1][0].get_text()


def test_table_body_and_replaced_footnote_sources() -> None:
    """续表保留首页图注，正文累计所有页，末页脚注保留其原始路径。"""
    tables = []
    for index in range(3):
        tables.append(
            dict(
                type="table",
                index=0,
                bbox=BOX,
                continues_prev=index > 0,
                content=[
                    dict(
                        type="table_caption",
                        content=[dict(type="text", content=f"Table {index} (continued)")],
                        bbox=[0.1, 0.1, 0.9, 0.18],
                    ),
                    dict(type="table_body", index=0, bbox=BOX, content=f"<table><tr><td>{index}</td></tr></table>"),
                    dict(
                        type="table_footnote", content=[dict(type="text", content=f"note {index}")], bbox=[0.1, 0.41, 0.9, 0.5]
                    ),
                ],
            )
        )
    nodes = _targets(_middle(*[[table] for table in tables]))
    body = next(refs for node, refs in nodes if node.get("data-block-type") == "table_body")
    assert [ref["page_idx"] for ref in body] == [2, 5, 8]
    caption = next(refs for node, refs in nodes if node.get("data-block-type") == "table_caption")
    note = next(refs for node, refs in nodes if node.get("data-block-type") == "table_footnote")
    assert caption[0]["page_idx"] == 2
    assert note[0]["page_idx"] == 8 and note[0]["child_path"] == [2]


def test_missing_child_geometry_and_hidden_auxiliary() -> None:
    """缺少子坐标时由父节点响应，过滤的页眉不产生目标。"""
    child = _text("child")
    child.pop("bbox")
    parent = dict(type="list", sub_type="text", index=0, bbox=BOX, content=[child])
    header = dict(type="header", index=1, bbox=BOX, content=[dict(type="text", content="header")])
    nodes = _targets(_middle([parent, header]))
    assert len(nodes) == 1
    assert nodes[0][1][0]["child_path"] == []


def test_same_page_merge_and_failed_table_merge() -> None:
    """同页续段保留每个块号，失败的续表仍拥有独立目标。"""
    second = _text("tail", continues_prev=True)
    second["index"] = 1
    refs = _targets(_middle([_text("first"), second]))[0][1]
    assert [ref["block_index"] for ref in refs] == [0, 1]
    table = dict(type="table", index=0, bbox=BOX, content=[dict(type="table_body", index=0, bbox=BOX, content="plain")])
    continued = deepcopy(table)
    continued["continues_prev"] = True
    nodes = _targets(_middle([table], [continued]))
    bodies = [refs for node, refs in nodes if node.get("data-block-type") == "table_body"]
    assert len(bodies) == 2 and all(len(refs) == 1 for refs in bodies)


@pytest.mark.parametrize("standalone", [False, True])
def test_source_attributes_preserve_wire_roundtrip(standalone: bool) -> None:
    """附加来源属性后仍通过当前 HTML wire 精确读取语义。"""
    from docvortex.analyzers.native.html.document import parse_html_document
    from docvortex.analyzers.native.html.resources import HtmlResourceContext
    from docvortex.codecs.html import decode_docvortex_html_wire

    middle = _middle([_text("first")], [_text("second", continues_prev=True)])
    traced = render_html(middle, include_source_map=True, standalone=standalone)
    plain = render_html(middle, standalone=standalone)
    traced_document = parse_html_document(traced.encode())
    plain_document = parse_html_document(plain.encode())
    traced_result = decode_docvortex_html_wire(traced_document.body, HtmlResourceContext(traced_document.source_context))
    plain_result = decode_docvortex_html_wire(plain_document.body, HtmlResourceContext(plain_document.source_context))
    assert traced_result.fallback_reason is None
    assert traced_result.blocks == plain_result.blocks


def test_nested_list_paths_are_original_and_unique() -> None:
    """嵌套列表中重复的局部索引用完整子路径区分。"""
    nested = dict(type="list", index=0, bbox=BOX, sub_type="text", content=[_text("nested")])
    parent = dict(type="list", index=0, bbox=BOX, sub_type="text", content=[_text("outer"), nested])
    nodes = _targets(_middle([parent]))
    leaves = [refs[0]["child_path"] for node, refs in nodes if node.name == "li"]
    assert sorted(leaves) == [[0], [1, 0]]
