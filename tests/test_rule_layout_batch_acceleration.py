"""新增布局批量入口须保留完整源对象、稳定顺序和精确边界。"""

from copy import deepcopy
from random import Random
from types import SimpleNamespace
import math

import pytest

from docvortex._compute_backend import get_native
from docvortex.analyzers.native.pdf.models import _AxisLine, _Fragment, _LineItem, _TableCandidate, _TextLane, _VisualRow


@pytest.mark.parametrize("seed", range(40))
def test_year_header_groups_keep_all_members_and_exact_tolerance(seed):
    """全部横线与行框逐项对照原谓词，覆盖容差两侧、重复框和稳定源顺序。"""
    native = get_native()
    if native is None:
        pytest.skip("native backend is not selected")
    rng = Random(seed)
    em = rng.choice([1.0, 3.0, 10.0])
    rules = [(0.0, 10.0, 100.0, 10.0), (0.0, 10.0, 100.0, 10.0)]
    rules += [(rng.uniform(-50, 50), rng.uniform(20, 60), 120.0, 60.0) for _ in range(20)]
    lines = []
    for _ in range(150):
        x = rng.choice([-0.3 * em, math.nextafter(-0.3 * em, -math.inf), 20.0])
        x2 = rng.choice([100.0 + 0.3 * em, math.nextafter(100.0 + 0.3 * em, math.inf), 50.0])
        y2 = rng.choice([10.0, 10.0 - 0.8 * em, math.nextafter(10.0 - 0.8 * em, -math.inf), 30.0])
        lines.append((x, y2 - em, x2, y2))
    expected = [
        [i for i, b in enumerate(lines) if r[0] - 0.3 * em <= b[0] and b[2] <= r[2] + 0.3 * em and 0 <= r[1] - b[3] <= 0.8 * em]
        for r in rules
    ]
    assert native.year_header_groups(lines, rules, em) == expected


def test_year_header_tables_preserve_candidates_and_input_changes(monkeypatch):
    """真实三行数值表完整对照候选，再修改文字与坐标确认不存在旧表头缓存。"""
    from docvortex.analyzers.native.pdf import table_detection as detection

    lines = [_LineItem("Metric", (0.0, 0.0, 20.0, 5.0), 0, 0, effective_height=5.0)]
    for i, x in enumerate([40.0, 70.0, 100.0]):
        lines.append(_LineItem(str(2020 + i), (x, 0.0, x + 15.0, 5.0), 0, len(lines), effective_height=5.0))
    for row, y in enumerate([10.0, 20.0, 30.0]):
        lines.append(_LineItem(f"item {row}", (0.0, y, 20.0, y + 5.0), 0, len(lines), effective_height=5.0))
        for x in [40.0, 70.0, 100.0]:
            lines.append(_LineItem("123", (x, y, x + 15.0, y + 5.0), 0, len(lines), effective_height=5.0))
    source = SimpleNamespace(lines=lines, drawing_lines=[_AxisLine((0.0, 6.0, 120.0, 6.0), 0.1, "horizontal")], page_size=(150.0, 100.0))
    adapter = detection._native_year_header_groups

    def reference_groups(*args):
        """禁用原生几何入口，保留完整年份规则作为差分参考。"""
        return None

    for mutation in [None, "text", "box"]:
        if mutation == "text":
            lines[2].text = "1990"
        elif mutation == "box":
            lines[2].text = "2021"
            lines[2].bbox = (70.0, 15.0, 85.0, 20.0)
        actual = detection._detect_year_header_numeric_tables(source, [])
        with monkeypatch.context() as context:
            context.setattr(detection, "_native_year_header_groups", reference_groups)
            expected = detection._detect_year_header_numeric_tables(source, [])
        assert actual == expected
        if mutation is None:
            assert len(actual) == 1
            if get_native() is not None:
                assert adapter(lines, source.drawing_lines, 5.0) is not None


def test_line_marker_features_reuse_only_identical_current_text_and_regex(monkeypatch):
    """几何复制复用文字特征，文字替换、后续修改和正则替换分别重新计算。"""
    from dataclasses import replace
    from docvortex.analyzers.native.pdf import models

    calls = []
    original = models.re.match

    def traced_match(pattern, text, *args, **kwargs):
        """记录真实正则调用，继续使用原字符和边界语义。"""
        calls.append(text)
        return original(pattern, text, *args, **kwargs)

    line = _LineItem("123", (0.0, 0.0, 10.0, 5.0), 0, 1)
    clone = replace(line, bbox=(0.0, 1.0, 10.0, 6.0))
    assert clone.note_marker_value == "123" and not clone.numbered_heading_start
    assert clone._marker_features is line._marker_features
    clone = replace(clone, text="1. Section")
    assert clone.note_marker_value is None and clone.numbered_heading_start
    assert clone._marker_features is not line._marker_features
    monkeypatch.setattr(models.re, "match", traced_match)
    clone.text = "25"
    clone.__post_init__()
    assert clone.note_marker_value == "25" and not clone.numbered_heading_start
    assert calls[-1] == "25"
    assert clone._marker_features is None
    monkeypatch.setattr(models.re, "match", original)
    clone.__post_init__()
    assert clone._marker_features[1] is original


@pytest.mark.parametrize("seed", range(50))
def test_math_word_owned_keeps_python_unicode_offsets_fonts_and_current_state(seed):
    """多字字符、Unicode 边界、字体角标与已有词逐项对照，修改字体后重新读取当前输入。"""
    from docvortex.analyzers.native.pdf import formulas

    rng = Random(seed)
    chars = []
    for _ in range(100):
        text = rng.choice(["中文 ", "abc", "ijk", "test", "word ", " ", "\n", "a", "b", "c", "d", "²", "_", None])
        chars.append({"char": text, "font": {"size": rng.choice([0.0, 8.5, 10.0]), "flags": rng.choice([0, 64, -1])}})
    line = _LineItem("body", (0.0, 0.0, 10.0, 5.0), 0, 1, chars=chars, native_math_words=frozenset(["existing"]))
    for mutation in [False, True]:
        if mutation:
            for char in chars:
                char["font"]["flags"] = 64
        assert formulas._native_math_word_fragments(line) == formulas._native_math_word_fragments_python(line)
        native = get_native()
        if native is not None:
            assert native.math_words_owned(line, _LineItem, formulas.re.finditer) is not None


@pytest.mark.parametrize("value", [1 << 62 | 64, "64", 64.0])
def test_math_words_unsupported_flags_use_complete_reference(value):
    """巨大整数与可转换字号标志不做近似，仍由 Python 保持完整 int 转换语义。"""
    from docvortex.analyzers.native.pdf import formulas

    line = _LineItem("abc", (0.0, 0.0, 10.0, 5.0), 0, 1, chars=[{"char": c, "font": {"size": 10.0, "flags": value}} for c in "abc"])
    assert formulas._native_math_word_fragments(line) == formulas._native_math_word_fragments_python(line)
    if get_native() is not None:
        assert get_native().math_words_owned(line, _LineItem, formulas.re.finditer) is None


@pytest.mark.parametrize("seed", range(40))
def test_prepared_rule_segments_keep_stable_centers_and_gap_boundaries(seed):
    """完整分段对照 Python 稳定排序，覆盖相同中心、零行高及三倍行距端点。"""
    native = get_native()
    if native is None:
        pytest.skip("native backend is not selected")
    from docvortex.analyzers.native.pdf import table_rules

    rng = Random(seed)
    rows = []
    for i in range(100):
        center = rng.choice([0.0, -0.0, 20.0, 30.0, rng.uniform(0.0, 60.0)])
        y = rng.choice([0.0, 3.0, math.nextafter(3.0, math.inf), 10.0, 30.0])
        rows.append(_VisualRow([], center, (0.0, y, 10.0, y + rng.choice([0.0, 1.0, 5.0]))))
    owner = native.PreparedRuleCandidates([0.0, 20.0, 40.0, 60.0], [(row.center_y, 0, row.bbox, [i]) for i, row in enumerate(rows)])
    for start, end, height in [(0, 100, 0.0), (3, 50, 1.0), (8, 40, 3.0), (10, 10, 4.0)]:
        groups, accepted, segments, short = owner.partition_with_segments(start, end, 0, 4, height)
        assert short == all(b - a <= 6.0 * height for a, b in zip([0.0, 20.0, 40.0], [20.0, 40.0, 60.0]))
        assert (groups, accepted) == owner.partition(start, end, 0, 4, height)
        expected = table_rules._continuous_table_row_segments(rows[start:end], height)
        assert [[id(rows[i]) for i in group] for group in segments] == [[id(row) for row in group] for group in expected]


@pytest.mark.parametrize("seed", range(30))
def test_marker_owned_sources_and_glyphs_match_full_unicode_and_current_boxes(seed):
    """普通来源字形完整对照原路径，保留负框、方向、特殊字符与大来源整数。"""
    from docvortex.analyzers.native.pdf import table_annotations as annotations
    from docvortex.document.pdf.text._contracts import Bbox

    rng = Random(seed)
    chars = []
    for _ in range(80):
        box = [rng.uniform(-10, 10), rng.uniform(-10, 10), rng.uniform(-10, 10), rng.uniform(-10, 10)]
        chars.append({"char": rng.choice(["A", "２", "a\n", " ", "é", "fi", "*", None]), "bbox": Bbox(box) if rng.choice([False, True]) else box})
    line = _LineItem("２ café *", (0.0, 0.0, 100.0, 10.0), 0, 1 << 100, chars=chars)
    assert annotations._prepare_marker_line_context([line, line]) == annotations._prepare_marker_line_context_python([line, line])
    for angle in [0, 90, 180, 270]:
        actual = annotations._prepare_marker_line(line, (100.0, 200.0), angle)
        expected = annotations._prepare_marker_line_python(line, (100.0, 200.0), angle)
        assert actual == expected
        for (_, a), (_, b) in zip(actual[0], expected[0]):
            assert [v.hex() for v in a] == [v.hex() for v in b]
    chars[0]["char"] = "changed"
    chars[0]["bbox"] = [1.0, 2.0, 5.0, 8.0]
    assert annotations._prepare_marker_line(line, (100.0, 200.0), 0) == annotations._prepare_marker_line_python(line, (100.0, 200.0), 0)


@pytest.mark.parametrize("seed", range(30))
def test_owned_column_cache_keeps_prefix_order_tolerance_and_epoch(seed):
    """随机锚点按增长、缩短与换起点顺序逐次对照，覆盖补偿求和及复用前缀清理。"""
    native = get_native()
    if native is None:
        pytest.skip("native backend is not selected")
    from docvortex.analyzers.native.pdf import table_rules as rules

    rng = Random(seed)
    rows = []
    for i in range(70):
        fragments = []
        for _ in range(rng.randrange(1, 8)):
            left = rng.choice([0.0, -0.0, 3.0, math.nextafter(3.0, math.inf), rng.uniform(-100, 100)])
            box = (left, float(i), left + rng.uniform(1, 30), float(i + 1))
            fragments.append(_Fragment("value", box, box, i))
        rows.append(_VisualRow(fragments, float(i), (0.0, float(i), 100.0, float(i + 1))))
    for compensated in [False, True]:
        owner = native.NativeColumnCache(compensated)
        for start, end in [(0, 10), (0, 30), (0, 20), (0, 50), (1, 40), (0, 60), (0, 30), (0, 0)]:
            actual = owner.count(rows[start:end], 4.0, True, _VisualRow, _Fragment)
            reference = native.StableColumnClusters(compensated)
            packed = [[(f.local_bbox[0], f.local_bbox[2]) for f in row.fragments] for row in rows[start:end]]
            assert actual == reference.extend(packed, 3.0)
        owner.clear_prefixes()
        packed = [[(f.local_bbox[0], f.local_bbox[2]) for f in row.fragments] for row in rows[:40]]
        expected = native.StableColumnClusters(compensated).extend(packed, 3.0)
        assert owner.count(rows[:40], 4.0, True, _VisualRow, _Fragment) == expected
        if compensated:
            assert expected == rules._count_stable_columns_python(rows[:40], 4.0)


@pytest.mark.parametrize("seed", range(40))
def test_fragment_groups_preserve_runtime_means_and_final_coordinates(seed):
    """随机原生行组、同高平局和容差端点逐字段比较；最终均值不得改成近似求和。"""
    from docvortex.analyzers.native.pdf import table_rules as rules

    rng = Random(seed)
    fragments = []
    for i in range(180):
        y = rng.choice([0.0, -0.0, 2.0, math.nextafter(2.0, math.inf), rng.uniform(-100, 500)])
        x = rng.choice([0.0, 10.0, rng.uniform(-20, 100)])
        box = (x, y, x + rng.choice([0.0, 3.0, 20.0]), y + rng.choice([0.0, 1.0, 10.0]))
        fragments.append(_Fragment(str(i), box, box, i, rng.choice([None, None, i // 3, i // 7])))
    for height in (0.0, 4.0, math.nextafter(4.0, math.inf), 10.0):
        assert rules._cluster_fragment_rows(fragments, height) == rules._cluster_fragment_rows_python(fragments, height)


@pytest.mark.parametrize("seed", range(30))
def test_prose_draft_filter_preserves_all_conflicts_and_source_members(seed):
    """逐个弱横线候选对照原循环，覆盖多网格、字重、语义和三行正文的严格端点。"""
    from docvortex.analyzers.native.pdf import table_detection as detection
    from docvortex.analyzers.native.pdf.table_rules import _RuleCandidateDraft

    rng = Random(seed)
    em = 2.0
    lines = []
    for i in range(25):
        y = rng.choice([1.0, math.nextafter(1.0, math.inf), 5.0, 10.0, 15.0, 28.0, 30.0])
        box = (rng.choice([0.0, 5.0, 80.0]), y, 100.0, y + 1.0)
        line = _LineItem("alpha beta gamma delta epsilon zeta", box, 0, i, dominant_font_weight=rng.choice([400, 599, 600]))
        line.semantic_type = rng.choice([None, None, "page_footnote"])
        lines.append(line)
    grids = [_TableCandidate((0.0, 30.0, 100.0, 60.0), (0.0, 30.0, 100.0, 60.0), 0, 1.0)]
    drawing = [_AxisLine((x, 30.0, x, 60.0), 1, "vertical") for x in (0.0, 50.0, 100.0)]
    drawing += [_AxisLine((0.0, y, 100.0, y), 1, "horizontal") for y in (30.0, 60.0)]
    source = SimpleNamespace(lines=lines, drawing_lines=drawing)
    drafts = []
    for _ in range(25):
        members = rng.sample(range(len(lines)), rng.randrange(1, 26))
        fragments = [_Fragment(lines[i].text, lines[i].bbox, lines[i].bbox, i) for i in members]
        first, last = rng.choice([0.0, 1.0, 15.0, 20.0]), rng.choice([28.0, 30.0, 60.0, 62.0])
        boundaries = [_AxisLine((0.0, y, 100.0, y), 1, "horizontal") for y in (first, last)]
        row = _VisualRow(fragments, 0.0, (0, 0, 100, 30))
        drafts.append(_RuleCandidateDraft(SimpleNamespace(angle=0, median_height=em), boundaries, [row], None, [], False, 1.0))
    candidates = drafts + grids
    expected = detection._exclude_prose_spanning_rule_drafts_python(source, candidates)
    actual = detection._exclude_prose_spanning_rule_drafts(source, candidates)
    assert [id(item) for item in actual] == [id(item) for item in expected]
    native = get_native()
    if native is not None:
        assert detection._prose_rule_native_flags(source, candidates) is not None


@pytest.mark.parametrize("mutation", ["duplicate", "index", "box", "font", "semantic"])
def test_owned_tail_rejects_unsupported_states_before_moving(mutation):
    """行身份或字段改变后完整回退，拒绝部分消费或在验证期间移动成员。"""
    native = get_native()
    if native is None:
        pytest.skip("native backend is not selected")
    line = _LineItem("body", (0.0, 0.0, 80.0, 5.0), 0, 0, effective_height=5.0)
    lanes = [_TextLane(0.0, 100.0, [(line, line.bbox)]), _TextLane(100.0, 200.0)]
    if mutation == "duplicate":
        lanes[1].lines.append((line, line.bbox))
    elif mutation == "index":
        line.source_index = True
    elif mutation == "box":
        lanes[0].lines[0] = (line, (0.0, math.nan, 80.0, 5.0))
    elif mutation == "font":
        line.font_signature = ["body", 0]
    else:
        line.semantic_type = object()
    before = [(list(lane.lines), lane.left, lane.right) for lane in lanes]
    assert native.short_tail_lane_events(lanes, 5.0, _TextLane, _LineItem) is None
    assert [(list(lane.lines), lane.left, lane.right) for lane in lanes] == before


@pytest.mark.parametrize("kind", ["no_candidate", "shrunken", "spanning"])
def test_unruled_notes_read_references_only_when_required(monkeypatch, kind):
    """无线注释已有字号证据时不读上标，跨栏候选仍读取全页完整编号。"""
    from docvortex.analyzers.native.pdf import auxiliary_text as auxiliary

    rows = []
    for i, y in enumerate((50.0, 70.0, 90.0)):
        box = (20.0, y, 190.0, y + 10.0)
        rows.append((_LineItem("ordinary text", box, 0, i, effective_height=10.0), box))
    text = "ordinary explanation" if kind == "no_candidate" else "1. explanation"
    h = 8.0 if kind == "shrunken" else 10.0
    box = (0.0, 160.0, 190.0, 160.0 + h)
    rows.append((_LineItem(text, box, 0, 3, effective_height=h), box))
    calls = []

    def references(lines):
        """模拟完整编号读取，记录是否在足够几何证据之后执行。"""
        calls.append(list(lines))
        return {"1"}

    monkeypatch.setattr(auxiliary, "_native_note_reference_values", references)
    groups = auxiliary._unruled_numbered_footnote_groups(rows, (200.0, 200.0))
    assert groups == ([] if kind == "no_candidate" else [{3}])
    assert len(calls) == (1 if kind == "spanning" else 0)


@pytest.mark.parametrize("anchors_only", [False, True])
def test_owned_character_packing_preserves_unicode_and_changed_geometry(monkeypatch, anchors_only):
    """完整对照 Unicode 分类、原框身份和状态更新；当前调用缓存不复用上一轮字符或侧表。"""
    import docvortex._compute_backend as backend
    from docvortex.analyzers.native.pdf import char_geometry as geometry
    from docvortex.document.pdf import PDFPageTextGeometry
    from docvortex.document.pdf.text import Bbox

    native = get_native()
    if native is None:
        pytest.skip("native backend is not selected")
    alphabet = ["A", "A", "中", "１", "Ⅻ", "²", "α", "=", " ", "\u200b", "\n", "😀", "a\u0301", "abc"]
    chars = [
        {"char": text, "char_idx": i, "rotation": 0.0, "bbox": Bbox([float(i), 0.0, float(i + 1), 2.0])}
        for i, text in enumerate(alphabet)
    ]
    line = _LineItem("".join(alphabet), (0.0, 0.0, 50.0, 2.0), 0, 0, chars=chars)
    side = PDFPageTextGeometry({}, {}, {})
    for round_index in range(2):
        if round_index:
            chars[0]["char"] = "="
            chars[1]["rotation"] = 90.0
            side.loose_bboxes[1] = (0.0, 0.0, 5.0, 3.0)
            side.tight_bboxes[2] = (2.0, 0.0, 4.0, 2.0)
            side.origins[2] = (2.0, 2.0)
        actual = geometry._plain_source_records(line, side, anchors_only)
        with monkeypatch.context() as scoped:
            scoped.setattr(backend, "get_native", lambda: None)
            expected = geometry._plain_source_records(line, side, anchors_only)
        assert actual == expected
        assert all(row[1] is chars[row[0]]["bbox"].bbox for row in actual)


@pytest.mark.parametrize("seed", range(50))
def test_lane_interval_and_assignment_batches_preserve_thresholds_and_ties(seed):
    """完整比较锚点中位数、支持率、同边缘平局以及嵌套栏的所有成员目标。"""
    from docvortex.analyzers.native.pdf import line_layout as layout

    native = get_native()
    if native is None:
        pytest.skip("native backend is not selected")
    rng = Random(seed)
    rows = []
    for i in range(180):
        x = rng.choice([0.0, -0.0, 10.0, 110.0, 200.0, rng.uniform(-10.0, 300.0)])
        y = rng.choice([0.0, 20.0, 100.0, 120.0])
        box = (x, y, x + rng.choice([0.0, 20.0, 80.0, 190.0]), y + rng.choice([0.0, 10.0]))
        line = _LineItem(
            "body",
            box,
            0,
            i,
            effective_height=rng.choice([0.0, 10.0]),
            em_height=8.0,
            style_scale_repaired=bool(rng.randrange(2)),
        )
        line.semantic_type = rng.choice([None, None, "header", "page_footnote", "aside_text"])
        rows.append((line, box))
    intervals, anchors = native.supported_lane_intervals_owned(rows, 320.0, 10.0, _LineItem)
    anchor_rows, expected = layout._supported_lane_intervals_python(rows, 320.0, 10.0)
    assert intervals == expected
    assert [rows[i] for i in anchors] == anchor_rows
    assert [[a.hex(), b.hex()] for a, b, _ in intervals] == [[float(a).hex(), float(b).hex()] for a, b, _ in expected]
    lanes = [_TextLane(0.0, 100.0), _TextLane(110.0, 210.0), _TextLane(110.0, 210.0)]
    ordered = sorted(lanes, key=lambda lane: lane.left)
    for nested in (None, (20.0, 100.0)):
        actual = native.lane_assignments_owned(rows, lanes, 7.5, nested, _LineItem, _TextLane)
        expected = []
        for line, bbox in rows:
            if nested is not None and (
                line.semantic_type in {"header", "footer", "page_number", "page_footnote", "aside_text"}
                or not nested[0] <= (bbox[1] + bbox[3]) / 2 <= nested[1]
            ):
                expected.append(len(lanes))
                continue
            best, coverage, second = layout._best_lane_coverage(bbox, lanes)
            fit = layout._fits_only_one_lane_ordered(bbox, best, ordered, 7.5)
            expected.append(
                None
                if second >= 0.2 and not fit
                else next(i for i, lane in enumerate(lanes) if lane is best)
                if fit or (coverage >= 0.5 if nested is not None else len(lanes) == 1)
                else None
            )
        assert actual == expected


@pytest.mark.parametrize("seed", range(30))
def test_complete_lane_inference_preserves_members_and_expansion(monkeypatch, seed):
    """在完整嵌套栏、扩张和短尾路径中对照参考入口，不能仅比较栏数量。"""
    from docvortex.analyzers.native.pdf import line_layout as layout

    rng = Random(seed)
    rows = []
    for i in range(90):
        x, y = rng.choice([0.0, 10.0, 120.0]), float(rng.randrange(30) * 6)
        box = (x, y, x + rng.choice([30.0, 90.0, 220.0]), y + 5.0)
        line = _LineItem("ordinary body text", box, 0, i, effective_height=5.0)
        line.semantic_type = rng.choice([None, None, None, "header", "page_footnote"])
        rows.append((line, box))
    for recalculate in (False, True):
        actual = layout._infer_text_lanes(deepcopy(rows), 240.0, 5.0, recalculate_intervals=recalculate)
        with monkeypatch.context() as scoped:
            scoped.setattr(layout, "_lane_batch_native", lambda: None)
            expected = layout._infer_text_lanes(deepcopy(rows), 240.0, 5.0, recalculate_intervals=recalculate)
        assert actual == expected


@pytest.mark.parametrize("layout", [False, True])
@pytest.mark.parametrize("angle", [0, 90, -180])
def test_owned_font_encoding_preserves_run_order_unicode_and_current_fields(layout, angle):
    """以原逐字符编码作独立参考，比较全部 run 键、字号、文字、编号和原始顺序。"""
    from docvortex.analyzers.native.pdf import char_geometry as geometry

    native = get_native()
    if native is None:
        pytest.skip("native backend is not selected")
    fonts = [
        None,
        {},
        {"name": "ABCDEF+Regular", "size": 8.0, "flags": 0, "weight": 399.0},
        {"name": "斜体", "size": "12.25", "flags": "64", "weight": "450"},
    ]
    alphabet = ["A", "中", "α", "１", "²", "Ⅻ", "=", " ", "abc", "😀", "A", "A"]
    chars = [{"char": text, "char_idx": i, "font": fonts[i % len(fonts)]} for i, text in enumerate(alphabet)]
    positions = [3, 0, 1, 5, 2, 0, 10, 11, 6, 8, 4, 9, 7]
    for revision in range(2):
        if revision:
            fonts[2]["size"] = 14.0
            fonts[2]["weight"] = 600
            chars[1]["char"] = "B"
        actual_ids, actual_keys = {}, []
        actual = native.owned_font_run_metadata(
            chars,
            positions,
            angle,
            actual_ids,
            actual_keys,
            geometry._font_run_metadata,
            geometry._script_group,
            geometry._cached_run_key,
            geometry._is_anchor_text,
            layout,
        )
        expected_ids, expected_keys, expected = {}, [], []
        cache = geometry._ReadOnlyFontCache(native_only=True)
        for i in positions:
            char = chars[i]
            key, size = geometry._font_run_key(char, angle, char["char"], cache)
            if key not in expected_ids:
                expected_ids[key] = len(expected_keys)
                expected_keys.append(key)
            encoded = (expected_ids[key], size, geometry._is_anchor_text(char["char"]))
            expected.append((*encoded, char["char_idx"], char["char"]) if layout else encoded)
        assert actual == expected
        assert actual_ids == expected_ids and actual_keys == expected_keys


def test_formula_text_cache_is_page_local_and_keeps_input_changes():
    """同页重放复用纯文字，跨页、改变文字和异常离开后均不泄漏特征状态。"""
    from docvortex.analyzers.native.pdf import formulas

    calls = []

    @formulas._formula_text_feature
    def feature(text):
        """模拟仅依赖不可变字符串的纯特征。"""
        calls.append(text)
        return text == "a"

    @formulas._formula_page_scope
    def replay(text):
        """独立调用可建立上下文，在页面入口内部则复用该页上下文。"""
        return feature(text)

    @formulas._formula_fresh_page_scope
    def page():
        """连续两次重放同一文字，再改变输入文字。"""
        return replay("a"), replay("a"), replay("b")

    assert page() == (True, True, False)
    assert calls == ["a", "b"]
    assert page() == (True, True, False)
    assert calls == ["a", "b", "a", "b"]
    assert formulas._FORMULA_TEXT_FEATURES.get() is None


@pytest.mark.parametrize("seed", range(50))
def test_owned_content_projection_preserves_every_unicode_offset_and_formula_gap(monkeypatch, seed):
    """对照原逐字符投影的全部字段，覆盖连字、非 BMP、控制字符和未闭合公式标记。"""
    import docvortex._compute_backend as backend
    from docvortex.analyzers.native.pdf.inline import matching

    rng = Random(seed)
    parts = [
        "abc",
        "\ufb01",
        "中",
        "😀",
        "\x02",
        "\x01",
        "\u200b",
        "\u00ad",
        "\u3000",
        r"\(x\)",
        r"\(a \(b\)c\)",
        r"\(missing",
        r"\)",
        r"\(\)",
    ]
    content = "".join(rng.choice(parts) for _ in range(90))
    actual = matching._project_content_chars(content)
    with monkeypatch.context() as scoped:
        scoped.setattr(backend, "get_native", lambda: None)
        expected = matching._project_content_chars(content)
    assert actual == expected
    assert [type(char) for char in actual] == [type(char) for char in expected]
    if actual:
        with pytest.raises(AttributeError):
            actual[0].value = "changed"


def test_owned_projection_keeps_surrogate_and_replaced_constructor_fallback(monkeypatch):
    """无法安全传输的字符串保留参考路径，替换构造函数后不绕过调用方行为。"""
    from docvortex.analyzers.native.pdf.inline import matching

    assert [char.value for char in matching._project_content_chars("a\ud800b")] == ["a", "\ud800", "b"]
    calls = []
    original = matching._ProjectedChar.__init__

    def init(self, *args, **kwargs):
        """记录原有构造函数是否逐字符执行。"""
        calls.append(True)
        original(self, *args, **kwargs)

    monkeypatch.setattr(matching._ProjectedChar, "__init__", init)
    assert "".join(char.value for char in matching._project_content_chars("ab")) == "ab"
    assert calls == [True, True]


def test_owned_table_rows_keep_coordinate_types_signed_zeros_and_current_members():
    """单片段保留原框，多片段保留整数类型和零的先后顺序，输入变更后重新读取。"""
    from docvortex.analyzers.native.pdf import table_rules as rules

    first = [0, -0.0, 2, 2.0]
    second = (-0.0, 0.0, 2.0, 2)
    fragments = [_Fragment("a", first, first, 0), _Fragment("b", second, second, 1)]
    for selected in ([fragments[0]], fragments, list(reversed(fragments))):
        actual = rules._cluster_fragment_rows(selected, 2.0)
        expected = rules._cluster_fragment_rows_python(selected, 2.0)
        assert actual == expected
        for a, b in zip(actual, expected):
            assert [type(v) for v in a.bbox] == [type(v) for v in b.bbox]
            assert [math.copysign(1, v) for v in a.bbox] == [math.copysign(1, v) for v in b.bbox]
            assert [id(fragment) for fragment in a.fragments] == [id(fragment) for fragment in b.fragments]
    fragments[0].local_bbox = (2, 30, 4, 32)
    fragments[1].visual_row_id = 0
    assert rules._cluster_fragment_rows(fragments, 2.0) == rules._cluster_fragment_rows_python(fragments, 2.0)


@pytest.mark.parametrize("mutation", [None, "semantic", "box", "height", "font", "unknown", "large"])
def test_owned_title_context_preserves_types_memberships_and_fallback(monkeypatch, mutation):
    """逐字段比较上下文快照，覆盖重复成员、语义变化、整数尺度及未知状态回退。"""
    import docvortex._compute_backend as backend
    from docvortex.analyzers.native.pdf.title_analysis.body_profile import _LaneProfileContext

    line = _LineItem("body", (0, 0, 80, 5), 0, 0, effective_height=5)
    box = (0, 0, 80, 5)
    lanes = [_TextLane(0, 100, [(line, box)]), _TextLane(100, 200, [(line, box)])]
    if mutation == "semantic":
        line.semantic_type = "header"
    elif mutation == "box":
        box = [0, -0.0, 80, 8]
        lanes[0].lines[0] = (line, box)
    elif mutation == "height":
        line.effective_height = 0
    elif mutation == "font":
        line.font_signature = ("font", 2**80)
    elif mutation == "unknown":
        line.font_coverage = object()
    elif mutation == "large":
        line.em_height = 2**80
    actual = _LaneProfileContext(lanes, [(line, box)])
    with monkeypatch.context() as scoped:
        scoped.setattr(backend, "get_native", lambda: None)
        expected = _LaneProfileContext(lanes, [(line, box)])
    assert actual.plain == expected.plain
    assert actual.heights == expected.heights
    assert {key: type(value) for key, value in actual.heights.items()} == {
        key: type(value) for key, value in expected.heights.items()
    }
    # 两次调用创建的临时补充栏身份不同；正式栏及重复成员归属必须保持一致。
    for context in (actual, expected):
        if context.plain:
            assert {id(lane) for lane in lanes} <= context.memberships[id(line)]
            assert len(context.memberships[id(line)]) == 3


@pytest.mark.parametrize("seed", range(100))
def test_exact_rule_mean_preserves_python_fraction_rounding(seed):
    """精确按位对照均值，包含抵消、不同二进制精度、次正规数及溢出安全回退。"""
    import statistics
    import struct
    from docvortex.analyzers.native.pdf.table_rules import _mean_rule_positions

    rng = Random(seed)
    power = rng.randrange(-1070, 950)
    numbers = [math.ldexp(rng.uniform(-10, 10), power) for _ in range(rng.randrange(1, 80))]
    cases = [
        numbers,
        [-0.0],
        [1e308, 1e308],
        [1e308, 1e-308],
        [1.0, -1.0, math.ulp(1.0)],
        [math.ulp(0.0), math.ulp(0.0), 2 * math.ulp(0.0)],
    ]
    for values in cases:
        assert struct.pack(">d", _mean_rule_positions(values)) == struct.pack(">d", statistics.mean(values))


def test_exact_rule_mean_keeps_numeric_types_and_replaced_mean(monkeypatch):
    """普通整数均值保留原返回类型，统计函数被替换时逐次调用完整参考实现。"""
    import statistics
    from docvortex.analyzers.native.pdf.table_rules import _mean_rule_positions

    assert type(_mean_rule_positions([2, 4])) is int
    assert type(_mean_rule_positions([1, 2])) is float
    calls = []

    def mean(values):
        """记录替换函数实际收到的完整列表。"""
        calls.append(values)
        return 123

    monkeypatch.setattr(statistics, "mean", mean)
    assert _mean_rule_positions([1.0, 2.0]) == 123
    assert calls == [[1.0, 2.0]]


@pytest.mark.parametrize("seed", range(40))
def test_code_rule_windows_preserve_all_candidates_and_geometric_endpoints(seed, monkeypatch):
    """随机横竖轨、包含端点、遮挡区域及同坐标平局对照完整代码候选，不能截断横线对。"""
    from docvortex.analyzers.native.pdf import code_blocks

    rng = Random(seed)
    lines = [
        _LineItem(
            "if condition return value", (20.0, float(y), 150.0, float(y + 2)), 0, i, effective_height=2.0, visual_row_id=i
        )
        for i, y in enumerate(range(5, 100, 5))
    ]
    ys = sorted(rng.sample(range(0, 105, 5), 7))
    rules = [
        _AxisLine((rng.choice([0.0, 1.5, math.nextafter(1.5, math.inf)]), float(y), 180.0, float(y + 0.1)), 0.1, "horizontal")
        for y in ys
    ]
    rules += [
        _AxisLine((float(x), 0.0, float(x + 0.1), rng.choice([0.0, 30.0, 120.0])), 0.1, "vertical")
        for x in rng.sample(range(0, 190, 10), 3)
    ]
    source = SimpleNamespace(page_size=(200.0, 200.0), lines=lines, drawing_lines=rules)
    excluded = [] if seed % 2 else [(0.0, 20.0, 200.0, 30.0)]
    claimed = {rng.randrange(len(lines))}
    assert code_blocks._detect_rule_delimited_code_candidates(source, excluded, claimed) == (
        code_blocks._detect_rule_delimited_code_candidates_python(source, excluded, claimed)
    )

    # 独立冻结文字裁决为通过，比较全部几何窗口，避免空候选掩盖几何分支错误。
    def accept(members, bbox, height):
        """仅在此差分用例中冻结文字裁决，几何及认领保持完整执行。"""
        return True

    monkeypatch.setattr(code_blocks, "_rule_delimited_code_members_are_structured", accept)
    assert code_blocks._detect_rule_delimited_code_candidates(source, excluded, claimed) == (
        code_blocks._detect_rule_delimited_code_candidates_python(source, excluded, claimed)
    )


def test_bold_font_batch_reloads_mutated_fields_and_keeps_all_members():
    """字体共享、子集名前缀和字重改变后逐字符核对，字体状态不在调用间复用。"""
    from docvortex.analyzers.native.pdf.inline.detection import _pdf_font_metadata

    native = get_native()
    if native is None:
        pytest.skip("Python reference backend")
    font = {"name": "ABCDEF+Helvetica", "flags": 0, "weight": 0}
    other = {"name": "Helvetica", "flags": 0, "weight": 400}
    lines = [_LineItem("ab", (0, 0, 10, 10), 0, 0, chars=[{"char": "a", "font": font}, {"char": "b", "font": other}])]
    bold = {("Helvetica", 0, None)}
    for _ in range(2):
        expected = [
            (i, [j for j, char in enumerate(line.chars) if _pdf_font_metadata(char) in bold]) for i, line in enumerate(lines)
        ]
        assert native.glyph_bold_indices_owned(lines, bold, _pdf_font_metadata, _LineItem) == [
            item for item in expected if item[1]
        ]
        font["weight"] = 400
        other["weight"] = 0


@pytest.mark.parametrize("seed", range(40))
def test_overlap_groups_preserve_every_member_and_ratio_boundary(seed):
    """保留重复框、退化框、整数类型回退和覆盖率端点的全部源索引。"""
    from docvortex.analyzers.native.pdf import table_detection as detection

    rng = Random(seed)
    boxes = [(0.0, 0.0, 100.0, 100.0), (0.0, 0.0, 0.0, 100.0)]
    boxes += [(rng.uniform(-100, 50), rng.uniform(-100, 50), rng.uniform(50, 200), rng.uniform(50, 200)) for _ in range(60)]
    boxes += boxes[:5]
    regions = [
        (0.0, 0.0, 80.0, 100.0),
        (0.0, 0.0, math.nextafter(80.0, -math.inf), 100.0),
        (0.0, 0.0, 90.0, 100.0),
        (0.0, 0.0, math.nextafter(90.0, math.inf), 100.0),
    ]
    for threshold in (0.8, 0.9):
        actual = detection._native_overlap_member_groups(boxes, regions, threshold)
        if get_native() is not None:
            assert actual == [
                [i for i, box in enumerate(boxes) if detection._bbox_overlap_in_first(box, region) >= threshold]
                for region in regions
            ]
    assert detection._native_overlap_member_groups([(0, 0, 2**30, 2**30)], regions, 0.8) is None


@pytest.mark.parametrize("seed", range(25))
def test_shaded_tables_match_complete_reference_after_geometry_changes(monkeypatch, seed):
    """完整阴影表候选对照 Python，保持列边界、成员、重复底色及图像表头的全部判断。"""
    import docvortex._compute_backend as backend
    from docvortex.analyzers.native.pdf import table_detection as detection

    rng = Random(seed)
    lines = []
    for row in range(6):
        for col in range(3):
            x, y = 10.0 + col * 70, 20.0 + row * 10
            lines.append(
                _LineItem(
                    "header" if row == 0 else str(rng.randrange(100)),
                    (x, y, x + 40, y + 5),
                    0,
                    len(lines),
                    effective_height=5.0,
                )
            )
    cells = [
        SimpleNamespace(
            bbox=(float(col * 70), 18.0, float((col + 1) * 70), 26.0),
            fill_visible=True,
            segment_count=5,
            fill_rgba=(220, 220, 220, 255),
        )
        for col in range(3)
    ]
    if seed % 2:
        cells.extend(cells[:1])
    source = SimpleNamespace(page_size=(220.0, 200.0), lines=lines, path_infos=cells, drawing_lines=[], image_bboxes=[])
    for _ in range(2):
        actual = detection._detect_shaded_header_tables(source, [])
        with monkeypatch.context() as scoped:
            scoped.setattr(backend, "get_native", lambda: None)
            expected = detection._detect_shaded_header_tables(source, [])
        assert actual == expected
        lines[-1].bbox = (0.0, 150.0, 210.0, 155.0)


@pytest.mark.parametrize("seed", range(50))
def test_fraction_batch_preserves_complete_members_and_thresholds(seed):
    """比较完整字符集合，覆盖 Unicode、重复索引、四方向、退化框与容差浮点邻居。"""
    from docvortex.analyzers.native.pdf.inline import scripts

    rng = Random(seed)
    page = (300.0, 400.0)
    for angle in (0, 90, 180, 270, 17):
        chars = [
            {
                "char_idx": i // 2 if i % 7 == 0 else i,
                "char": rng.choice(["a", "β", "中", "𝟙", "1", " ", "\u200b", "\ud800", "+", "ab"]),
            }
            for i in range(60)
        ]
        # 孤立代理字符测试参考回退，常规 Unicode 则测试实际内核。
        if seed % 2 == 0:
            chars = [dict(c, char="x") if c["char"] == "\ud800" else c for c in chars]
        boxes = {
            c["char_idx"]: (
                float(i % 15 * 10),
                float(i // 15 * 8),
                float(i % 15 * 10 + 4),
                float(i // 15 * 8 + rng.choice([0, 4, 8])),
            )
            for i, c in enumerate(chars)
        }
        rules = [
            (rng.uniform(0, 80), rng.choice([4.0, 8.0, math.nextafter(8.0, math.inf)]), rng.uniform(80, 160), 9.0)
            for _ in range(8)
        ]
        rules.append(None)
        actual = scripts._fraction_member_indices(page, chars, boxes, [None] * len(rules), angle, rules)
        expected = scripts._fraction_member_indices_python(page, chars, boxes, [None] * len(rules), angle, rules)
        assert actual == expected


def test_fraction_batch_positive_members_and_state_invalidation(monkeypatch):
    """真实上下叠字必须完整认领，框与文字变化立即生效，替换判定函数时回退原路径。"""
    from docvortex.analyzers.native.pdf.inline import scripts

    chars = [{"char_idx": 1, "char": "a"}, {"char_idx": 2, "char": "β"}]
    boxes = {1: (2.0, 0.0, 6.0, 4.0), 2: (2.0, 6.0, 6.0, 10.0)}
    args = [(100.0, 100.0), chars, boxes, [None], 0, [(1.0, 4.5, 7.0, 5.5)]]
    assert scripts._fraction_member_indices(*args) == {1, 2}
    chars[1]["char"] = "+"
    assert scripts._fraction_member_indices(*args) == set()
    chars[1]["char"] = "β"
    boxes[2] = (70.0, 6.0, 75.0, 10.0)
    assert scripts._fraction_member_indices(*args) == set()
    monkeypatch.setattr(scripts, "_bbox_axis_overlap", lambda *_args, **_kwargs: 99.0)
    assert scripts._fraction_member_indices(*args) == scripts._fraction_member_indices_python(*args) == {1, 2}


def test_fraction_batch_keeps_reference_empty_height_exception():
    """字符全为零高度时保留原 StatisticsError，不能将异常静默改为无候选。"""
    from docvortex.analyzers.native.pdf.inline import scripts
    import statistics

    with pytest.raises(statistics.StatisticsError):
        scripts._fraction_member_indices(
            (100.0, 100.0), [{"char_idx": 1, "char": "x"}], {1: (0.0, 0.0, 5.0, 0.0)}, [None], 0, [None]
        )


@pytest.mark.parametrize("seed", range(50))
def test_pending_glyph_batch_keeps_frozen_records_and_whitespace_state(seed):
    """完整比较表格字符、连字、空白状态、逆向框、重复索引、裁剪和四方向输出。"""
    from docvortex.analyzers.native.pdf._table_recovery import text
    from docvortex.analyzers.native.pdf._table_recovery.contracts import NativeTableInput
    from docvortex.document.pdf.text._contracts import Bbox

    rng = Random(seed)
    chars = []
    for i in range(120):
        x, y = rng.uniform(-10, 110), rng.uniform(-10, 110)
        raw = [x, y, x + rng.choice([-3.0, 0.0, 2.0, 9.0]), y + rng.choice([-4.0, 0.0, 0.4, 8.0])]
        chars.append(
            {
                "char_idx": rng.randrange(-20, 120),
                "char": rng.choice(["a", "中", "ﬁ", "-", "\x02", " ", "\n", "\r", "\u200b", "\u00a0", "ab", "", None]),
                "bbox": Bbox(raw) if i % 2 else raw,
            }
        )
    for angle in (0, 90, 180, 270):
        table = NativeTableInput((0.0, 0.0, 100.0, 100.0), (100.0, 100.0), angle, tuple(chars), (), ())
        assert text._select_pending_glyphs(table) == text._select_pending_glyphs_adapter(table)
        assert text._select_pending_glyphs(table) == text._select_pending_glyphs_python(table)


def test_pending_glyph_batch_observes_input_changes_and_constructor(monkeypatch):
    """字符、框和冻结对象构造器的改变即刻生效，自定义初始化必须执行参考构造路径。"""
    from docvortex.analyzers.native.pdf._table_recovery import text
    from docvortex.analyzers.native.pdf._table_recovery.contracts import NativeTableInput
    from dataclasses import FrozenInstanceError

    char = {"char_idx": 500, "char": "ﬁ", "bbox": [2.0, 2.0, 5.0, 10.0]}
    table = NativeTableInput((0.0, 0.0, 20.0, 20.0), (20.0, 20.0), 0, (char,), (), ())
    result = text._select_pending_glyphs(table)
    assert result[0].text == "fi"
    with pytest.raises(FrozenInstanceError):
        result[0].text = "x"
    char["char"] = "X"
    assert text._select_pending_glyphs(table)[0].text == "X"
    char["bbox"] = [30.0, 0.0, 40.0, 10.0]
    assert text._select_pending_glyphs(table) == []
    char["bbox"] = [2.0, 2.0, 5.0, 10.0]
    original = text._PendingGlyph.__init__
    calls = []

    def custom_init(self, *args, **kwargs):
        """记录原构造器调用，确保 native 不绕过测试或调用方提供的构造行为。"""
        calls.append(args)
        original(self, *args, **kwargs)

    monkeypatch.setattr(text._PendingGlyph, "__init__", custom_init)
    assert text._select_pending_glyphs(table)[0].text == "X"
    assert len(calls) == 1


@pytest.mark.parametrize("seed", range(50))
def test_page_fraction_batch_matches_unprepared_drawings(seed):
    """整页原始横线按相同有限框校验与方向变换处理，不需 Python 重建全部字符几何。"""
    from docvortex.analyzers.native.pdf.inline import scripts

    rng = Random(seed)
    chars = [{"char_idx": i, "char": rng.choice(["a", "1", "中", "𝟙", "+", " "])} for i in range(200)]
    boxes = {i: (float(i % 20 * 5), float(i // 20 * 7), float(i % 20 * 5 + 3), float(i // 20 * 7 + 5)) for i in range(200)}
    drawings = [
        _AxisLine((rng.uniform(-5, 80), rng.uniform(0, 65), rng.uniform(80, 110), rng.uniform(65, 80)), 0.5, "horizontal")
        for _ in range(25)
    ]
    drawings.extend([_AxisLine((0.0, 0.0, 15.0, 0.0), 0.1, "horizontal"), _AxisLine((7.0, 8.0, 3.0, 2.0), 0.1, "horizontal")])
    for angle in (0, 90, 180, 270, 17):
        args = ((100.0, 100.0), chars, boxes, drawings, angle)
        assert scripts._fraction_member_indices(*args) == scripts._fraction_member_indices_python(*args)
        native = get_native()
        if native is not None:
            assert native.fraction_members_owned(chars, boxes, (100.0, 100.0), angle, drawings, _AxisLine) is not None


@pytest.mark.parametrize("seed", range(60))
def test_owned_typography_matches_reference_and_recomputes_mutated_fields(monkeypatch, seed):
    """完整排版字段逐位比较，覆盖方向、Unicode、裁剪、字重平局及输入字段修改。"""
    from dataclasses import asdict, replace
    from docvortex.analyzers.native.pdf import native_text

    rng = Random(seed)
    fonts = [
        {"name": "ABCDEF+Arial-Bold", "flags": 0, "weight": 700.0},
        {"name": "Arial", "flags": 4, "weight": 400},
        {"name": "Times-Roman", "flags": 0, "weight": None},
        {"name": None},
    ]
    chars = []
    for i in range(45):
        x, y = rng.uniform(-30, 300), rng.uniform(-20, 400)
        chars.append({"char": rng.choice(["a", "中", "β", "", None, " ", "\n", "\u200b", "ab", "\u00a0"]),
                      "bbox": (x, y, x + rng.choice([0, 2.0, -2.0, 5.0]), y + rng.choice([0, 10.0, -10.0])),
                      "font": rng.choice(fonts)})
    line = _LineItem("text.", (0.0, 0.0, 200.0, 20.0), rng.choice([0, 90, 180, 270]), 0,
                     chars=chars, em_height=rng.choice([0.0, 12.0]))
    for _ in range(2):
        actual, expected = replace(line), replace(line)
        native_text._fill_native_typography(actual, (300.0, 400.0))
        with monkeypatch.context() as scoped:
            scoped.setattr(native_text, "_owned_typography_metrics", lambda *args: None)
            native_text._fill_native_typography(expected, (300.0, 400.0))
        actual, expected = asdict(actual), asdict(expected)
        for key in ("effective_height", "em_height", "median_glyph_width", "font_coverage", "dominant_font_weight",
                    "leading_emphasis_width", "leading_typography_width"):
            a, b = actual[key], expected[key]
            assert (a.hex() if type(a) is float else a) == (b.hex() if type(b) is float else b), key
        assert actual == expected
        chars[0]["char"] = "中"
        chars[0]["bbox"] = (10.0, 10.0, 100.0, 40.0)
        fonts[0]["name"] = "ChangedFamily"
        fonts[0]["weight"] = 450.0


@pytest.mark.parametrize("value", ["0", 1 << 100, 0.1, float("nan")])
def test_owned_typography_unsupported_font_conversion_keeps_complete_fallback(monkeypatch, value):
    """非标准标志交由原 int 转换与字体处理，不能部分采用快路径结果。"""
    from dataclasses import replace
    from docvortex.analyzers.native.pdf import native_text

    line = _LineItem("abcd", (0.0, 0.0, 40.0, 10.0), 0, 0,
                     chars=[{"char": "a", "bbox": (0.0, 0.0, 10.0, 10.0),
                             "font": {"name": "Arial", "flags": value, "weight": 400}}] * 4)
    actual, expected = replace(line), replace(line)
    native_text._fill_native_typography(actual, (300.0, 400.0))
    with monkeypatch.context() as scoped:
        scoped.setattr(native_text, "_owned_typography_metrics", lambda *args: None)
        native_text._fill_native_typography(expected, (300.0, 400.0))
    assert actual == expected


@pytest.mark.parametrize("seed", range(80))
def test_cell_visual_geometry_retains_complete_text_parts_and_source_refs(monkeypatch, seed):
    """单行、双行、水印、上下标与阈值邻居均比较完整片段，不能丢失或重排任何源字形。"""
    from docvortex.analyzers.native.pdf._table_recovery import text
    from docvortex.analyzers.native.pdf._table_recovery.contracts import NativeTableGlyph

    rng = Random(seed)
    glyphs = []
    for i in range(rng.randrange(1, 50)):
        x, y = float(rng.randrange(15) * 5), float(rng.randrange(6) * 4)
        height = rng.choice([0.0, 4.25, math.nextafter(4.25, -math.inf), 5.0, 6.25, math.nextafter(6.25, math.inf)])
        glyphs.append(NativeTableGlyph(i // 2, i, rng.choice(["中", "a", "1", "", "ab", " ", "α"]),
                                     (x, y, x + rng.choice([2.0, 4.0]), y + height), rng.randrange(3),
                                     rng.choice([False, True]), rng.choice([False, True])))
    actual = text.build_cell_text_parts(glyphs, 5.0)
    with monkeypatch.context() as scoped:
        scoped.setattr(text, "_native_cell_visual_groups", lambda *args: None)
        expected = text.build_cell_text_parts(glyphs, 5.0)
    assert actual == expected
    grouped = text._native_cell_visual_groups(glyphs, 5.0)
    if grouped is not None:
        reference = text._python_cell_visual_groups(glyphs, 5.0)
        assert [[id(g) for g in row] for row in grouped] == [[id(g) for g in row] for row in reference]
@pytest.mark.parametrize("seed", range(48))
def test_unmapped_formula_ink_current_state_and_signed_zero(seed):
    """批量筛选逐项比较文字、透明模式、字号及翻转框，改变输入后必须重新判定。"""
    from random import Random
    from docvortex.analyzers.native.pdf import formulas

    rng = Random(seed)
    chars = [
        {"char": rng.choice(["A", "中", "\x00", " ", "\t", "", "\u200b", "ab", None]),
         "tight_bbox": rng.choice([(-0.0, 0.0, 5.0, 40.0), (10.0, 40.0, 0.0, 0.0), None, (0.0, 0.0, float("nan"), 40.0)]),
         "font": {"size": rng.choice([0.0, 10.0, float("nan"), float("inf"), None])},
         "text_object_id": rng.choice([None, 1]), "text_render_mode": rng.choice([0, 3, 7, None, "3", 3.0])}
        for _ in range(36)
    ]
    for iteration in range(2):
        actual = formulas._unmapped_formula_ink_bboxes(chars)
        expected = formulas._unmapped_formula_ink_bboxes_python(chars)
        assert [[v.hex() for v in box] for box in actual] == [[v.hex() for v in box] for box in expected]
        for char in chars:
            char["char"] = "\x00"
            char["font"]["size"] = 10.0
            char["text_render_mode"] = 0
            char["text_object_id"] = 1


def test_unmapped_formula_ink_custom_conversion_and_errors(monkeypatch):
    """特殊转换只在完整回退执行一次，异常字号及替换框函数保持既有行为。"""
    from docvortex.analyzers.native.pdf import formulas

    calls = []

    class Size:
        """记录非普通字号对象的转换副作用。"""

        def __float__(self):
            """执行一次原字号转换并保留调用证据。"""
            calls.append(True)
            return 10.0

    char = {"char": "\x00", "tight_bbox": (-0.0, 0.0, 5.0, 40.0), "font": {"size": Size()},
            "text_object_id": 1, "text_render_mode": 0}
    assert formulas._unmapped_formula_ink_bboxes([char]) == [(-0.0, 0.0, 5.0, 40.0)]
    assert calls == [True]
    char["font"]["size"] = "invalid"
    with pytest.raises(ValueError):
        formulas._unmapped_formula_ink_bboxes([char])
    char["font"]["size"] = 10.0

    char["tight_bbox"] = (0.0, 0.0, 2**10000)
    with pytest.raises(OverflowError):
        formulas._unmapped_formula_ink_bboxes([char])
    char["tight_bbox"] = (-0.0, 0.0, 5.0, 40.0)

    def replaced_bbox(value):
        """替换规范化函数必须改变当前筛选结果。"""
        return None

    monkeypatch.setattr(formulas, "_coerce_bbox", replaced_bbox)
    assert formulas._unmapped_formula_ink_bboxes([char]) == []


def test_font_family_page_cache_state_nesting_and_exception(monkeypatch):
    """字体变更取新文字键，页面、嵌套及异常退出不得共享旧缓存。"""
    from docvortex.analyzers.native.pdf import typography

    observed = []

    @typography._font_family_page_scope
    def nested():
        """嵌套页面使用独立缓存，退出后恢复外层对象。"""
        assert typography._FONT_FAMILY_CACHE.get() == {}
        assert typography._normalized_font_family(("ABCDEF+New_Font", 0)) == "newfont"

    @typography._font_family_page_scope
    def page(fail=False):
        """本页只保存普通字体的纯归一化结果，并验证原签名修改。"""
        cache = typography._FONT_FAMILY_CACHE.get()
        assert cache == {}
        signature = ["ABCDEF+Fixture_Font", 0]
        assert typography._normalized_font_family(signature) == "fixturefont"
        assert typography._normalized_font_family(signature) == "fixturefont"
        assert cache == {"ABCDEF+Fixture_Font": "fixturefont"}
        nested()
        assert typography._FONT_FAMILY_CACHE.get() is cache
        signature[0] = "GHIJKL+Changed Font"
        assert typography._normalized_font_family(signature) == "changedfont"
        observed.append(cache)
        if fail:
            raise RuntimeError("fixture")

    page()
    page()
    assert observed[0] is not observed[1]
    with pytest.raises(RuntimeError):
        page(True)
    assert typography._FONT_FAMILY_CACHE.get() is None

    original_sub = typography.re.sub

    def changed_sub(pattern, repl, value):
        """替换正则实现后必须禁用缓存并执行当前函数。"""
        return "Different" if pattern == r"[\s_-]+" else original_sub(pattern, repl, value)

    @typography._font_family_page_scope
    def replacement():
        """已经缓存的相同文字也必须尊重正则实现变更。"""
        assert typography._normalized_font_family(("Fixture", 0)) == "fixture"
        monkeypatch.setattr(typography.re, "sub", changed_sub)
        assert typography._normalized_font_family(("Fixture", 0)) == "different"

    replacement()
