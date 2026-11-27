from copy import deepcopy

from docvortex.analyzers.native.pdf.layout_risk import extract_layout_risk


def _block(kind, bbox, content="", *, lines=(), angle=0):
    """构造只保留风险提取所用字段的归一化块。"""
    return {
        "type": kind,
        "bbox": list(bbox),
        "content": content,
        "lines": [{"bbox": list(line)} for line in lines],
        "angle": angle,
    }


def _prose(x0, y0, x1, y1):
    """构造具有首尾两条印刷行框的正文段。"""
    return _block("text", (x0, y0, x1, y1), "正文内容。", lines=((x0, y0, x1, y0 + 0.014), (x0, y1 - 0.014, x1, y1)))


def test_code_overlap_uses_normalized_or_serialized_marginal_text():
    """页边代码首行与代码框冲突在内存模型和序列化模型中得到相同证据。"""
    header = _block("header", (0.13, 0.06, 0.32, 0.074), "example log line")
    code = _block("code", (0.12, 0.058, 0.88, 0.26), "second line")
    blocks = [header, code, _prose(0.13, 0.30, 0.84, 0.42)]
    original = deepcopy(blocks)
    risk = extract_layout_risk(blocks)
    assert risk["marginal_role"][0] == 60
    assert "code" in risk["marginal_role"][1]
    assert blocks == original

    header["content"] = [{"type": "text", "content": "example log line"}]
    assert extract_layout_risk(blocks) == risk
    header["bbox"] = [0.13, 0.02, 0.32, 0.034]
    assert "marginal_role" not in extract_layout_risk(blocks)


def test_marginal_clusters_require_continuity_with_the_body():
    """数个同一物理行的页边碎块紧贴正文时才算一组强证据。"""
    fragments = [
        _block("header", (0.14, 0.125, 0.20, 0.140), "令"),
        _block("header", (0.21, 0.124, 0.25, 0.141), "x = 1"),
        _block("header", (0.26, 0.11, 0.32, 0.163), "a = 2"),
        _block("page_number", (0.33, 0.128, 0.35, 0.142), "3"),
    ]
    close = _prose(0.14, 0.17, 0.82, 0.25)
    assert extract_layout_risk([*fragments, close])["marginal_role"][0] == 60
    separated = _prose(0.14, 0.22, 0.82, 0.32)
    assert "marginal_role" not in extract_layout_risk([*fragments, separated])


def test_sequential_table_notes_are_flagged_but_running_footer_is_kept():
    """表体和正文之间的页脚冲突可定位，正文之后留有行距的页脚正常。"""
    table = _block("table", (0.16, 0.07, 0.85, 0.81), "<table></table>")
    note = _block("footer", (0.10, 0.82, 0.91, 0.833), "description")
    prose = _prose(0.10, 0.836, 0.91, 0.89)
    footer = _block("footer", (0.10, 0.95, 0.32, 0.963), "running footer")
    assert extract_layout_risk([table, prose, note, footer])["marginal_role"][0] == 60
    assert "marginal_role" not in extract_layout_risk([table, prose, footer])


def test_table_number_and_large_header_have_distinct_strength():
    """表编号和近邻表体给出强分，只有大字号的页眉宽框给出弱分。"""
    caption = _block("header", (0.43, 0.045, 0.54, 0.061), "Table 4")
    table = _block("table", (0.11, 0.084, 0.89, 0.30), "<table></table>")
    assert extract_layout_risk([caption, table])["marginal_role"][0] == 60
    caption["content"] = "Table 4 was updated by staff."
    assert "marginal_role" not in extract_layout_risk([caption, table])

    wide = _block("header", (0.08, 0.045, 0.71, 0.087), "Topic of this page")
    prose = _prose(0.08, 0.13, 0.88, 0.24)
    assert extract_layout_risk([wide, prose])["marginal_role"][0] == 30


def test_column_reentry_detects_local_shape_on_three_column_page():
    """在有第三栏的页面保留真实输入顺序，并辨别已经顺栏的同一组块。"""
    left_top = _prose(0.09, 0.34, 0.30, 0.59)
    middle = _prose(0.36, 0.56, 0.61, 0.76)
    left_tail = _prose(0.09, 0.61, 0.31, 0.81)
    right = _prose(0.67, 0.38, 0.90, 0.87)
    misordered = [left_top, middle, left_tail, right]
    risk = extract_layout_risk(misordered)["column_reentry"]
    assert risk[0] == 60
    assert "左→右→左" in risk[1]
    assert "column_reentry" not in extract_layout_risk([left_top, left_tail, middle, right])


def test_wide_section_divider_and_narrow_sidebar_do_not_form_column_reentry():
    """跨栏标题和栏目宽度悬殊的侧栏可有正常的上下阅读顺序。"""
    top = _prose(0.09, 0.34, 0.31, 0.59)
    other = _prose(0.36, 0.56, 0.61, 0.74)
    tail = _prose(0.09, 0.62, 0.31, 0.81)
    divider = _block("paragraph_title", (0.09, 0.60, 0.70, 0.615), "New section")
    assert "column_reentry" not in extract_layout_risk([top, other, divider, tail])

    narrow_top = _prose(0.18, 0.34, 0.31, 0.59)
    narrow_tail = _prose(0.18, 0.62, 0.31, 0.81)
    wider_other = _prose(0.36, 0.56, 0.87, 0.74)
    assert "column_reentry" not in extract_layout_risk([narrow_top, wider_other, narrow_tail])


def test_equation_overlap_scores_independent_assets():
    """两个独立公式的显著交叠得强分，紧挨但分离的公式正常。"""
    first = _block("equation", (0.39, 0.61, 0.55, 0.68))
    second = _block("equation", (0.44, 0.64, 0.56, 0.66))
    risk = extract_layout_risk([first, second])
    assert risk["asset_fragmentation"][0] == 60
    assert "交叠" in risk["asset_fragmentation"][1]
    second["bbox"] = [0.44, 0.68, 0.56, 0.70]
    assert "asset_fragmentation" not in extract_layout_risk([first, second])


def test_image_strips_require_consecutive_geometry():
    """累计相邻图条区分三条线索和四条强证据，分散图条不能累加。"""
    strips = [_block("image", (0.34, 0.43 + index * 0.012, 0.83, 0.44 + index * 0.012)) for index in range(4)]
    assert extract_layout_risk(strips[:3])["asset_fragmentation"][0] == 30
    assert extract_layout_risk(strips)["asset_fragmentation"][0] == 60
    strips[2]["bbox"] = [0.15, 0.45, 0.44, 0.46]
    assert "asset_fragmentation" not in extract_layout_risk(strips)


def test_large_image_panels_join_on_both_axes_with_different_widths():
    """三块不同宽度的大图近拼给出弱分，交换横纵轴和改变版面比例后结论一致。"""
    boxes = [(0.12, 0.10, 0.88, 0.24), (0.15, 0.246, 0.68, 0.47), (0.28, 0.476, 0.83, 0.66)]
    images = [_block("image", box) for box in boxes]
    assert extract_layout_risk(images)["asset_fragmentation"][0] == 30
    transposed = [_block("image", (y0, x0 * 0.7, y1, x1 * 0.7)) for x0, y0, x1, y1 in boxes]
    assert extract_layout_risk(transposed)["asset_fragmentation"][0] == 30
    assert "asset_fragmentation" not in extract_layout_risk(images[:2])


def test_body_between_image_panels_stops_a_continuous_join():
    """正文基线在接边上时应断开连续图面，接边外的段落不影响判定。"""
    images = [
        _block("image", (0.12, 0.10, 0.88, 0.24)),
        _block("image", (0.15, 0.246, 0.68, 0.47)),
        _block("image", (0.28, 0.476, 0.83, 0.66)),
    ]
    separator = _block("text", (0.24, 0.238, 0.64, 0.249), "An independent explanatory paragraph.")
    assert "asset_fragmentation" not in extract_layout_risk([*images, separator])
    separator["bbox"] = [0.24, 0.70, 0.64, 0.72]
    assert extract_layout_risk([*images, separator])["asset_fragmentation"][0] == 30


def test_separate_product_photos_have_gutters_or_distinct_labels():
    """有正常留白或各自拥有标题的商品组保留为独立图片。"""
    spaced = [(0.07, 0.19, 0.33, 0.44), (0.36, 0.19, 0.62, 0.44), (0.65, 0.19, 0.91, 0.44)]
    assert "asset_fragmentation" not in extract_layout_risk([_block("image", box) for box in spaced])

    adjacent = [(0.07, 0.19, 0.34, 0.44), (0.346, 0.19, 0.63, 0.44), (0.636, 0.19, 0.91, 0.44)]
    photos = [_block("image", box) for box in adjacent]
    captions = [
        _block("image_caption", (x0 + 0.05, 0.452, x1 - 0.04, 0.47), f"Product {index}")
        for index, (x0, _, x1, _) in enumerate(adjacent)
    ]
    assert "asset_fragmentation" not in extract_layout_risk([*photos, *captions])


def test_overlap_between_visible_images_needs_no_distinct_inset_caption():
    """覆盖多数面积的大图副块给出弱分，有独立题注的插图与小徽标保留。"""
    outer = _block("image", (0.10, 0.16, 0.78, 0.78))
    inner = _block("image", (0.33, 0.36, 0.66, 0.63))
    logo = _block("image", (0.30, 0.24, 0.38, 0.31))
    assert extract_layout_risk([outer, inner])["asset_fragmentation"][0] == 30
    assert "asset_fragmentation" not in extract_layout_risk([outer, logo])

    caption = _block("image_caption", (0.37, 0.639, 0.62, 0.658), "Detail of the photo")
    assert "asset_fragmentation" not in extract_layout_risk([outer, inner, caption])
    caption["bbox"] = [0.36, 0.57, 0.62, 0.59]
    caption["_native_annotation_parent_bbox"] = inner["bbox"]
    assert "asset_fragmentation" not in extract_layout_risk([outer, inner, caption])


def test_full_page_crop_with_editable_body_allows_independent_figure():
    """有正文的整页背景允许单独插图，纯重复大裁片给出风险。"""
    background = _block("image", (0, 0, 1, 1))
    crop = _block("image", (0.40, 0.50, 0.88, 0.77))
    assert extract_layout_risk([background, crop])["asset_fragmentation"][0] == 60
    assert "asset_fragmentation" not in extract_layout_risk([background, crop, _prose(0.08, 0.15, 0.8, 0.31)])


def test_body_grid_requires_repeated_rows_and_no_recognized_table():
    """短数值块按多条列轨重复排列才命中，已解析的表体不重复计分。"""
    rows = [
        [
            _block("text", (edge - 0.015 - index * 0.005, y, edge, y + 0.016), str(index * 11 + col + 1))
            for col, edge in enumerate((0.26, 0.40, 0.53, 0.68, 0.77))
        ]
        for index, y in enumerate((0.27, 0.294, 0.317, 0.341))
    ]
    cells = [cell for row in rows for cell in row]
    assert extract_layout_risk(cells)["grid_fragmentation"][0] == 60
    assert "grid_fragmentation" not in extract_layout_risk(cells[:-5])

    table = _block("table", (0.18, 0.25, 0.81, 0.38), "<table></table>")
    assert "grid_fragmentation" not in extract_layout_risk([table, *cells])

    for cell in rows[-1][2:]:
        cell["bbox"][0] += 0.030
        cell["bbox"][2] += 0.030
    assert "grid_fragmentation" not in extract_layout_risk(cells)


def test_two_column_index_page_numbers_do_not_form_a_grid():
    """正文分栏或每个对齐目录条目带页码也只形成两条数值轨。"""
    blocks = []
    for i in range(8):
        y = 0.12 + i * 0.028
        for left, right in ((0.08, 0.46), (0.53, 0.91)):
            blocks.append(_block("text", (left, y, right - 0.06, y + 0.016), f"{i + 1}. A directory entry"))
            blocks.append(_block("text", (right - 0.025, y, right, y + 0.016), str(i + 12)))
    assert "grid_fragmentation" not in extract_layout_risk(blocks)


def test_each_family_returns_only_its_best_score():
    """同时存在多个机制时各自保留最强证据，同族重复不增加分数。"""
    wide = _block("header", (0.08, 0.045, 0.71, 0.087), "Large header")
    prose = _prose(0.08, 0.13, 0.8, 0.31)
    code = _block("code", (0.08, 0.70, 0.86, 0.91), "code")
    footer = _block("footer", (0.10, 0.88, 0.15, 0.89), "last line")
    formula = _block("equation", (0.20, 0.48, 0.52, 0.52))
    cropped_formula = _block("equation", (0.27, 0.49, 0.40, 0.51))
    risk = extract_layout_risk([wide, prose, formula, cropped_formula, code, footer])
    assert {family: score for family, (score, _) in risk.items()} == {"marginal_role": 60, "asset_fragmentation": 60}


def test_detached_image_descriptions_return_to_an_earlier_card():
    """两幅并列图各有自己的说明时，发完图再回头发说明会打断局部图文顺序。"""
    left = _block("image", (0.06, 0.14, 0.42, 0.34))
    right = _block("image", (0.54, 0.14, 0.90, 0.34))
    name = _block("paragraph_title", (0.07, 0.35, 0.35, 0.37), "第一个操作步骤")
    step = _block("text", (0.55, 0.35, 0.85, 0.38), "Confirm the account")
    assert "图文" in extract_layout_risk([left, right, name, step])["column_reentry"][1]
    assert not extract_layout_risk([left, name, right, step])
    assert not extract_layout_risk([left, right, _block("text", (0.05, 0.35, 0.91, 0.38), "Shared caption")])
    assert not extract_layout_risk([left, right, {**name, "content": "(a)"}, {**step, "content": "(b)"}])
    assert not extract_layout_risk([left, right, {**name, "type": "caption"}, {**step, "type": "caption"}])


def test_display_formula_in_footer_stays_routed_after_paragraph_repair():
    """正文引出算式而实际算式只在页脚中时，修好上方分段仍应交给 PP。"""
    para = _block("text", (0.10, 0.81, 0.86, 0.90), "This gives the equation for the coefficient:")
    foot = _block(
        "footer",
        (0.24, 0.911, 0.78, 0.929),
        [
            {"type": "text", "content": "R"},
            {"type": "text", "content": "k", "styles": ["subscript"]},
            {"type": "text", "content": " = a/b"},
        ],
    )
    assert extract_layout_risk([para, foot])["marginal_role"][0] == 60
    assert "marginal_role" not in extract_layout_risk([para, {**foot, "content": "Journal / issue 2024"}])
    assert "marginal_role" not in extract_layout_risk([{**para, "content": "An independent paragraph."}, foot])


def test_running_header_cannot_own_a_function_closing_brace():
    """只有同一缩进的函数和紧邻末句支持时才转交孤立闭括号。"""
    start = _block("text", (0.14, 0.09, 0.46, 0.10), "void report(int size) {")
    stmt = _block("text", (0.16, 0.13, 0.32, 0.14), "return size;")
    end = _block("header", (0.14, 0.142, 0.15, 0.15), "}")
    assert extract_layout_risk([end, start, stmt])["marginal_role"][0] == 30
    assert "marginal_role" not in extract_layout_risk([end, start])
    assert "marginal_role" not in extract_layout_risk([end, stmt, {**start, "bbox": (0.61, 0.09, 0.87, 0.10)}])
