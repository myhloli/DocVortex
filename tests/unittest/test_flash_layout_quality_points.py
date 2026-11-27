"""检查可验证的成员归组缺失与实际页级分数的关系。"""

from docvortex.analyzers.native.pdf.layout_risk_scoring import score_layout_pages


def _block(kind, text, bounds):
    """构造已交付的块，正文保留一行视觉边界。"""
    return {"type": kind, "content": text, "bbox": bounds, "lines": [{"bbox": bounds}]}


def test_tagged_flattened_bullets_require_source_rows_and_missing_boundaries():
    """页上单一长段仅在原文有同左缘项目且输出确实压平时扣分；行内符号和真实列表均保留。"""
    bounds = (0.12, 0.08, 0.84, 0.83)
    values = ["• Read the instructions", "• Inspect the specimen", "• Record the results"]
    raw = [dict(bbox=(25, 40 + 18 * i, 150, 50 + 18 * i), spans=[{"text": text}]) for i, text in enumerate(values)]
    flat = dict(_block("text", "Introduction. " + " ".join(values) + " Discussion.", bounds), _pdf_structure={"node_id": "7:0"})
    broken = [flat]
    assert 0.8 < score_layout_pages([broken], source_lines=lambda _: raw)[0] < 1
    assert score_layout_pages([broken], source_lines=lambda _: [{"spans": [{"text": "Signs " + " ".join(values)}]}]) == (1.0,)
    assert score_layout_pages([[dict(flat, type="list")]], source_lines=lambda _: raw) == (1.0,)
    assert score_layout_pages([[dict(flat, content="\n".join(values))]], source_lines=lambda _: raw) == (1.0,)


def test_tagged_page_with_a_source_confirmed_section_and_list_loses_more_points():
    """段中出现粗体编号字串若非独立印刷标题，不应按整页层级损失扣分。"""
    texts = ["• First item here", "• Second item here", "4.2 Actual section"]
    source = [dict(spans=[{"text": t}], bbox=(45, 100 + i * 30, 140, 110 + i * 30)) for i, t in enumerate(texts)]
    block = _block("text", "", (0.1, 0.09, 0.8, 0.85))
    block.update(
        _pdf_structure={"node_id": "9"},
        content=[dict(type="text", content=" ".join(texts[:2]) + " "), dict(type="text", content=texts[-1], styles=["bold"])],
    )
    assert score_layout_pages([[block]], source_lines=lambda _: source) == (0.70,)
    source[-1]["spans"][0]["text"] += " appears inside a sentence."
    assert score_layout_pages([[block]], source_lines=lambda _: source) == (0.97,)


def test_code_heading_with_a_detached_closer_is_not_a_full_score():
    """错误标题和同级闭括号共同构成反证，已归入代码或隔栏的括号不扣分。"""
    title = _block("paragraph_title", "fn transform(value) {return value * 2;", (0.1, 0.15, 0.65, 0.19))
    close = _block("text", "}", (0.1, 0.195, 0.11, 0.212))
    assert 0.8 < score_layout_pages([[title, close]])[0] < 1
    assert score_layout_pages([[dict(title, type="code"), close], [title, dict(close, bbox=(0.76, 0.195, 0.77, 0.212))]]) == (
        1,
        1,
    )


def test_orphaned_cell_rows_need_repeat_tracks_and_adjacent_table_caption():
    """中英表题旁混合数据的连续行应退出满分；已归表、没有表题和远处引用保留。"""
    cells = []
    for i in range(4):
        y = 0.30 + 0.018 * i
        for left, value in zip((0.15, 0.24, 0.35, 0.43), ("Group", "Method", str(i + 1), f"{i}.5"), strict=True):
            cells.append(_block("text", value, (left, y, left + 0.035, y + 0.013)))
    caption = _block("text", "Table 2. Independent estimates.", (0.14, 0.378, 0.55, 0.398))
    score = score_layout_pages([[*cells, caption]])[0]
    assert score == 0.70
    assert score_layout_pages([cells, [*cells, dict(caption, bbox=(0.14, 0.68, 0.55, 0.7))]]) == (1, 1)
    table = _block("table", "<table><tr><td>Group</td></tr></table>", (0.14, 0.298, 0.54, 0.372))
    assert score_layout_pages([[table, caption]]) == (1.0,)
    assert score_layout_pages([[*cells, dict(caption, content="Table 2 shows the findings near the chart.")]]) == (1.0,)


def test_distant_note_interrupting_same_printed_line_leaves_perfect_score():
    """编号、真实上标、同源段和回到同一物理行须同时成立，合法的注后新段和旁注不扣分。"""
    first = dict(
        _block(
            "text",
            [{"type": "text", "content": "The population"}, {"type": "text", "content": "4", "styles": ["superscript"]}],
            (0.11, 0.3, 0.57, 0.33),
        ),
        _pdf_structure={"node_id": "body"},
    )
    rest = dict(_block("text", "in these regions is growing.", (0.11, 0.32, 0.89, 0.45)), _pdf_structure={"node_id": "body"})
    note = dict(
        _block("text", "4 See the appendix for definitions.", (0.11, 0.85, 0.89, 0.91)), _pdf_structure={"node_id": "note"}
    )
    pages = [
        [first, note, rest],
        [first, note, dict(rest, _pdf_structure={"node_id": "other"})],
        [first, dict(note, bbox=(0.91, 0.33, 0.98, 0.39)), rest],
        [first, dict(note, content="5 A different note."), rest],
        [dict(first, content="The population4"), note, rest],
        [first, rest, note],
    ]
    assert score_layout_pages(pages) == (0.97, 1, 1, 1, 1, 1)


def test_grouping_loss_and_spatial_collision_combine_across_orthogonal_domains():
    """归组缺失与顶层重叠分属正交域时，跨域按 Noisy-OR 联合扣分并可跨过 0.80 门槛。"""
    title = _block("paragraph_title", "fn transform(value) {return value * 2;", (0.1, 0.15, 0.65, 0.19))
    close = _block("text", "}", (0.1, 0.195, 0.11, 0.212))
    equation = _block("equation", "", (0.1, 0.70, 0.5, 0.90))
    clause = _block("text", "otherwise", (0.32, 0.80, 0.49, 0.82))
    only_grouping, only_overlap, combined = score_layout_pages([[title, close], [equation, clause], [title, close, equation, clause]])
    assert only_grouping > 0.80 and only_overlap == 0.82
    assert combined <= 0.80 and combined < min(only_grouping, only_overlap)
