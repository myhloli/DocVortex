"""第七轮阶段缓存、普通字符源几何与物化复用的独立差分。"""

from copy import deepcopy
import math
import random

import pytest

from docvortex.analyzers.native.pdf.models import _LineItem, _TextLane
from docvortex.analyzers.native.pdf.title_analysis import body_profile, page_titles
from docvortex.analyzers.native.pdf import char_geometry
from docvortex._compute_backend import get_native
from docvortex.document.pdf.native_contracts import PDFPageTextGeometry


def _profile_lane(seed):
    """构造排序、字号、字体平局及混合语义均可变化的栏成员。"""
    rng = random.Random(seed)
    rows = []
    for index in range(40):
        left = rng.choice([0.0, 30.0, 60.0])
        box = (left, float(index * 15), left + rng.choice([40.0, 80.0, 160.0]), float(index * 15 + 10))
        line = _LineItem("Section" if index % 3 == 0 else "Body text.", box, 0, index)
        line.effective_height = rng.choice([8.0, 10.0, 12.0])
        line.font_signature = (rng.choice(["A", "B"]), rng.choice([0, 64]))
        line.font_coverage = 1.0
        line.dominant_font_weight = rng.choice([400.0, 700.0])
        rows.append((line, box))
    rng.shuffle(rows)
    return _TextLane(0.0, 220.0, rows)


def test_profile_cache_preserves_first_sort_and_invalidates_semantics(monkeypatch):
    """第一次排序前后的字体首命中不可混用，语义变化须重算。"""
    lane = _profile_lane(7)
    expected_lane = deepcopy(lane)
    original = body_profile._infer_lane_body_profile
    calls = []

    def counted(value):
        """记录真实参考统计调用，检查缓存不会重复扫描稳定栏。"""
        calls.append(1)
        return original(value)

    monkeypatch.setattr(body_profile, "_infer_lane_body_profile", counted)
    context = body_profile._LaneProfileContext([lane])
    assert context.profile(lane) == original(expected_lane)
    assert [row[0].source_index for row in lane.lines] == [row[0].source_index for row in expected_lane.lines]
    assert context.profile(lane) == original(expected_lane)
    assert context.profile(lane) == original(expected_lane)
    assert len(calls) == 2
    lane.lines[0][0].semantic_type = "paragraph_title"
    expected_lane.lines[0][0].semantic_type = "paragraph_title"
    context.invalidate(lane.lines[0][0])
    assert context.profile(lane) == original(expected_lane)
    assert len(calls) == 3


@pytest.mark.parametrize("change", ["geometry", "font", "order", "members", "boundary"])
def test_profile_new_stage_rebuilds_mutated_state(change):
    """新分析阶段不沿用旧几何、字体、成员、栏界或顺序的缓存。"""
    lane = _profile_lane(3)
    old = body_profile._LaneProfileContext([lane])
    old.profile(lane)
    if change == "geometry":
        lane.lines[0][0].effective_height = 30.0
    elif change == "font":
        lane.lines[0][0].font_signature = ("C", 0)
    elif change == "order":
        lane.lines.reverse()
    elif change == "members":
        lane.lines.pop()
    else:
        lane.right = 400.0
    expected = deepcopy(lane)
    context = body_profile._LaneProfileContext([lane])
    assert context.profile(lane) == body_profile._infer_lane_body_profile(expected)
    assert [row[0].source_index for row in lane.lines] == [row[0].source_index for row in expected.lines]


@pytest.mark.parametrize("invalid", [math.nan, math.inf, 10**400])
def test_profile_abnormal_numeric_uses_reference(invalid):
    """异常尺度不能因缓存预检查引入额外异常或改变参考路径。"""
    lane = _profile_lane(2)
    lane.lines[0][0].effective_height = invalid
    assert not body_profile._LaneProfileContext([lane]).plain


@pytest.mark.parametrize("seed", range(20))
@pytest.mark.parametrize("operation", ["centered", "emphasized", "demote"])
def test_title_decisions_match_uncached_stage(seed, operation, monkeypatch):
    """原候选遍历与语义写入连续发生时，缓存仍与逐次统计一致。"""
    lane = _profile_lane(seed)
    lane.lines.sort(key=lambda row: row[0].source_index)
    if operation == "demote":
        for line, _box in lane.lines[::3]:
            line.semantic_type = "paragraph_title"
    expected = deepcopy(lane)
    function = {
        "centered": page_titles._classify_cross_lane_centered_section_titles,
        "emphasized": page_titles._classify_cross_lane_emphasized_section_titles,
        "demote": page_titles._demote_cross_lane_body_continuation_titles,
    }[operation]
    args = [] if operation == "demote" else [220.0, 650.0, []]
    kwargs = {} if operation == "demote" else {"document_title_bottom": None}
    if operation == "centered":
        kwargs["page_index"] = 1
    elif operation == "emphasized":
        kwargs["document_body_profile"] = None
    function(list(lane.lines), [lane], *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(
            body_profile._LaneProfileContext, "profile", lambda self, value: body_profile._infer_lane_body_profile(value)
        )
        function(list(expected.lines), [expected], *args, **kwargs)
    assert lane == expected


@pytest.mark.parametrize("angle", [0, 90, 180, 270])
@pytest.mark.parametrize("seed", range(16))
def test_plain_source_geometry_matches_reference(angle, seed, monkeypatch):
    """比较原逐字符源框路径，覆盖旋转、裁剪、无效框和字符筛选。"""
    if get_native() is None:
        pytest.skip("native backend is not selected")
    rng = random.Random(seed)
    chars, tight, origins, loose = [], {}, {}, {}
    for index in range(40):
        x, y = float(rng.randrange(-10, 110)), float(rng.randrange(-10, 110))
        box = (x, y, x + rng.choice([0.0, 3.0, 20.0]), y + 9.0)
        chars.append({"char": rng.choice("A1中 α²\t"), "char_idx": index, "bbox": box, "rotation": rng.choice([0.0, 1.0])})
        if index % 5:
            tight[index] = (x, y + 1.0, x + 3.0, y + 8.0)
        if index % 7:
            origins[index] = (x, y + 8.0)
        loose[index] = (x, y, x + 5.0, y + 9.0)
    geometry = PDFPageTextGeometry(chars, tight, origins, loose)
    line = _LineItem("test", (0.0, 0.0, 100.0, 100.0), angle, 0, chars=chars)
    original = deepcopy((line, geometry))
    for anchors in (False, True):
        actual = list(char_geometry._prepared_line_geometry(line, geometry, (100.0, 100.0), anchors_only=anchors))
        with monkeypatch.context() as patch:
            patch.setattr("docvortex._compute_backend.get_native", lambda: None)
            expected = list(char_geometry._prepared_line_geometry(line, geometry, (100.0, 100.0), anchors_only=anchors))
        assert actual == expected
    assert (line, geometry) == original


@pytest.mark.parametrize("value", [1, math.nan, math.inf, 10**400])
def test_plain_source_rejects_unsupported_coordinates(value):
    """异常或非浮点坐标返回显式不支持，不能吞掉参考路径的异常。"""
    native = get_native()
    if native is None:
        pytest.skip("native backend is not selected")
    assert (
        native.source_rows_plain([(0, (0.0, value, 2.0, 3.0), None, (0.0, 1.0, 2.0, 3.0), (0.0, 3.0), 0.0)], (10.0, 10.0), 0)
        is None
    )


def test_plain_source_keeps_zero_bits_and_coordinate_identity():
    """极值与未旋转结果保持原浮点来源及正负零位。"""
    native = get_native()
    if native is None:
        pytest.skip("native backend is not selected")
    import struct

    box = (-0.0, 0.0, 2.0, 3.0)
    origin = (-0.0, 3.0)
    fast = native.source_rows_plain([(7, box, None, box, origin, 0.0)], (10.0, 10.0), 0)[0][1]
    old = native.source_rows([box], [None], [box], [origin], [0.0], (10.0, 10.0), 0, lambda value: value)[0]
    assert [struct.pack("!d", number) for row in fast for number in row] == [
        struct.pack("!d", number) for row in old for number in row
    ]
    assert fast[0] is fast[3] and fast[1] is fast[4] and fast[2] is fast[5]
    assert fast[0][2] is box[2]


def test_font_cache_is_bounded_and_clears_before_special_conversion():
    """身份强引用有界，特殊字段回调改变其他字体时不能命中陈旧元数据。"""
    cache = char_geometry._ReadOnlyFontCache()
    for index in range(4100):
        cache.metadata({"name": str(index), "size": 10.0})
    assert len(cache.values) == 4096
    font = {"name": "A", "size": 10.0}
    cache.metadata(font)

    class ChangingSize:
        """模拟特殊字体转换对普通字体的写入。"""

        def __float__(self):
            """保留原转换回调并修改已经准备的字体对象。"""
            font["size"] = 12.0
            return 8.0

    cache.metadata({"name": "B", "size": ChangingSize()})
    assert cache.metadata(font)[1] == 12.0
