"""验证续表前驱约定，以及已确认关系与内容安全拼接的职责边界。"""

from copy import deepcopy

import pytest
from bs4 import BeautifulSoup

from docvortex.content.table import merge_table_content
from docvortex.postprocess.document import model_json_to_middle_json
from docvortex.render import RenderMode, render_html, render_structured_content
from docvortex.render._internal.common.planner import build_render_plan
from docvortex.schema import MiddleJson, ModelJson, TableBlock


def _table(label: str, *, identity: str | None = None, width: float = 0.8, html: str | None = None) -> dict:
    """构造可带结构身份的原始表格，各片段数据不同以隔离表头判定。"""
    block = {
        "type": "table",
        "bbox": [0.05, 0.2, 0.05 + width, 0.8],
        "content": html or f"<table><tr><th>H</th></tr><tr><td>{label}</td></tr></table>",
    }
    if identity is not None:
        block["_pdf_structure"] = {"node_id": identity, "logical_id": identity}
    return block


def _text(label: str) -> dict:
    """构造不应改变可信表格前驱的普通正文。"""
    return {"type": "text", "bbox": [0.1, 0.85, 0.9, 0.9], "content": [{"type": "text", "content": label}]}


def _document(pages: list[list[dict]], page_indices: list[int] | None = None) -> MiddleJson:
    """沿真实后处理生成严格文档，保留结构来源关联供断言。"""
    return model_json_to_middle_json(
        ModelJson(
            pages=pages,
            page_index_map=page_indices or [],
            metadata={"file_suffix": "pdf", "producer": {"name": "test", "version": "1"}},
            extensions={"docvortex_pdf_structure": {}},
        )
    )


def _rows(table: TableBlock) -> list[list[str]]:
    """读取实际表体单元格，避免仅凭表格数量判定成功。"""
    html = next(child.content for child in table.content if child.type == "table_body")
    return [
        [cell.get_text() for cell in row.find_all(["td", "th"])] for row in BeautifulSoup(html, "html.parser").find_all("tr")
    ]


def _visible_tables(document: MiddleJson, mode: RenderMode = RenderMode.DEFAULT) -> list[TableBlock]:
    """运行真实共享规划器并验证输入保持不变。"""
    original = document.model_dump()
    plan = build_render_plan(document, mode)
    assert document.model_dump() == original
    return [item.block for page in plan for item in page if isinstance(item.block, TableBlock) and not item.removed]


@pytest.mark.parametrize("location", ["previous_page", "current_page"])
@pytest.mark.parametrize("tagged_interloper", [False, True])
def test_structure_does_not_mark_interleaved_table(location: str, tagged_interloper: bool) -> None:
    """同源前片段不是最近顶层表格时不打标，普通表格同样阻断交错关系。"""
    pages = [[_table("A1", identity="a")], [_table("A2", identity="a")]]
    other = _table("B", identity="b" if tagged_interloper else None)
    if location == "previous_page":
        pages[0].append(other)
    else:
        pages[1].insert(0, other)
    document = _document(pages)
    continuation = document.pages[1].blocks[-1]

    assert continuation.continues_prev is None
    tables = _visible_tables(document)
    assert [_rows(table)[-1] for table in tables] == [["A1"], ["B"], ["A2"]]
    links = document.extensions["docvortex_pdf_structure"]["blocks"]["a"]
    assert [link["page_idx"] for link in links] == [0, 1]
    assert links[1]["block_path"] == [len(document.pages[1].blocks) - 1]


def test_structure_marks_the_nearest_matching_table() -> None:
    """前页有多表时仅续接最近且同源的 B，A 保持独立。"""
    document = _document([[_table("A", identity="a"), _table("B1", identity="b")], [_table("B2", identity="b")]])
    assert document.pages[1].blocks[0].continues_prev is True
    assert [_rows(table) for table in _visible_tables(document)] == [[["H"], ["A"]], [["H"], ["B1"], ["B2"]]]


def test_structure_predecessor_ignores_non_table_blocks() -> None:
    """可信身份可跨正文定位最近表格，不把普通页边界启发式加入结构判断。"""
    document = _document([[_table("A1", identity="a"), _text("tail")], [_text("head"), _table("A2", identity="a")]])
    assert document.pages[1].blocks[-1].continues_prev is True
    assert [_rows(table) for table in _visible_tables(document)] == [[["H"], ["A1"], ["A2"]]]


def test_structure_does_not_mark_across_missing_pages() -> None:
    """即使结构身份相同，抽页缺口也保留独立片段。"""
    document = _document([[_table("A1", identity="a")], [_table("A3", identity="a")]], [0, 2])
    assert document.pages[1].blocks[0].continues_prev is None
    assert len(_visible_tables(document)) == 2


@pytest.mark.parametrize("layout", ["first", "same_page", "gap", "same_page_after_merge", "gap_after_merge"])
def test_renderer_requires_an_adjacent_original_predecessor(layout: str) -> None:
    """错误或过期标记不能跨缺页或同页合并，已消费片段也必须推进物理前驱。"""
    if layout == "first":
        document = _document([[_table("A")]])
        expected = [[["H"], ["A"]]]
    elif layout == "same_page":
        document = _document([[_table("A"), _table("B")]])
        expected = [[["H"], ["A"]], [["H"], ["B"]]]
    elif layout == "gap":
        document = _document([[_table("A")], [_table("B")]], [0, 2])
        expected = [[["H"], ["A"]], [["H"], ["B"]]]
    elif layout == "same_page_after_merge":
        document = _document([[_table("A")], [_table("B"), _table("C")]])
        expected = [[["H"], ["A"], ["B"]], [["H"], ["C"]]]
    else:
        document = _document([[_table("A")], [_table("B")], [_table("C")]], [0, 1, 3])
        expected = [[["H"], ["A"], ["B"]], [["H"], ["C"]]]
    for page in document.pages:
        for block in page.blocks:
            block.continues_prev = True
    assert [_rows(table) for table in _visible_tables(document)] == expected


def test_renderer_keeps_gradual_width_continuation_chain() -> None:
    """逐页宽度均相容时不能拿累计表首页宽度否决第三个片段。"""
    document = _document([[_table(label, width=width)] for label, width in zip("ABC", [0.8, 0.76, 0.72], strict=True)])
    assert [page.blocks[0].continues_prev for page in document.pages] == [None, True, True]
    assert [_rows(table) for table in _visible_tables(document)] == [[["H"], ["A"], ["B"], ["C"]]]
    assert len(_visible_tables(document, RenderMode.FULL)) == 3
    assert [len(page["blocks"]) for page in render_structured_content(document)["pages"]] == [1, 1, 1]


def test_confirmed_structure_can_merge_different_physical_widths() -> None:
    """可信身份绕过宽度启发式，但公开任意两表入口即使收到标记仍保持保护。"""
    document = _document([[_table("A", identity="a", width=0.8)], [_table("B", identity="a", width=0.5)]])
    assert document.pages[1].blocks[0].continues_prev is True
    raw = [page.blocks[0].model_dump(mode="python", exclude_none=True) for page in document.pages]
    original = deepcopy(raw)
    assert merge_table_content(*raw) is None
    assert raw == original
    assert [_rows(table) for table in _visible_tables(document)] == [[["H"], ["A"], ["B"]]]


@pytest.mark.parametrize("reason", ["caption", "footnotes"])
def test_confirmed_structure_does_not_repeat_annotation_heuristics(reason: str) -> None:
    """结构已确认关系后不因普通标题或脚注数量再次否决，公开入口仍检查。"""
    pages = [[_table("A", identity="a")], [_table("B", identity="a")]]
    for index in range(1 if reason == "caption" else 2):
        pages[1 if reason == "caption" else 0].append(
            {
                "type": "table_caption" if reason == "caption" else "table_footnote",
                "bbox": [0.1, 0.1, 0.9, 0.15] if reason == "caption" else [0.1, 0.85, 0.9, 0.9],
                "content": [{"type": "text", "content": f"annotation {index}"}],
                "_pdf_structure": {"node_id": f"note-{index}", "visual_parent": "a"},
            }
        )
    document = _document(pages)
    assert document.pages[1].blocks[0].continues_prev is True
    raw = [page.blocks[0].model_dump(mode="python", exclude_none=True) for page in document.pages]
    assert merge_table_content(*raw) is None
    assert [_rows(table) for table in _visible_tables(document)] == [[["H"], ["A"], ["B"]]]


@pytest.mark.parametrize(
    "unsafe_html",
    [
        "<table></table>",
        "<table><tr><td colspan='bad'>B</td></tr></table>",
        "<table><tr><td colspan='2'>B</td></tr><tr><td>C</td></tr></table>",
        "<table><tr><td><table><tr><td>B</td></tr></table></td></tr></table>",
    ],
)
def test_confirmed_relation_still_requires_safe_table_content(unsafe_html: str) -> None:
    """标记不能绕过空表、非法跨度、缺格和嵌套网格的安全检查。"""
    document = _document([[_table("A", identity="a")], [_table("B", identity="a", html=unsafe_html)]])
    assert document.pages[1].blocks[0].continues_prev is True
    assert len(_visible_tables(document)) == 2


def test_failed_merge_starts_a_new_chain_and_retains_sources() -> None:
    """两列与三列不能安全拼接时，后续三列表格只与最近原片段续接。"""
    document = _document(
        [
            [_table("A", identity="a", html="<table><tr><td>A1</td><td>A2</td></tr></table>")],
            [_table("B", identity="a", html="<table><tr><td>B1</td><td>B2</td><td>B3</td></tr></table>")],
            [_table("C", identity="a", html="<table><tr><td>C1</td><td>C2</td><td>C3</td></tr></table>")],
        ]
    )
    assert [_rows(table) for table in _visible_tables(document)] == [
        [["A1", "A2"]],
        [["B1", "B2", "B3"], ["C1", "C2", "C3"]],
    ]
    plan = build_render_plan(document, include_source_map=True)
    assert [ref.page_idx for ref in plan[1][0].sources.refs] == [1, 2]
    assert plan[0][0].sources.refs[0].page_idx == 0
    assert plan[2][0].removed
    html = render_html(document, include_source_map=True)
    assert all(label in html for label in ("A1", "A2", "B1", "B2", "B3", "C1", "C2", "C3"))
