"""第三轮候选流、批量几何和原生读取的独立参考差分。"""

import math
import random
from copy import deepcopy

import pytest

from docvortex.analyzers.native.pdf import _interval_candidates as intervals


@pytest.mark.parametrize("seed", range(12))
@pytest.mark.parametrize("python_only", (False, True))
def test_interval_tree_matches_complete_pairs(monkeypatch, seed, python_only):
    """逐行比较完整区间交集，覆盖端点相等、重复键和跨组排除。"""
    if python_only:
        monkeypatch.setattr(intervals, "get_native", lambda: None)
    rng = random.Random(seed)
    bounds, groups, ids = [], {}, []
    for index in range(150):
        low = rng.choice([0.0, -0.0, 1.0, math.nextafter(1.0, math.inf), rng.uniform(-10, 20)])
        bounds.append((low, low + rng.choice([0.0, 1.0, 20.0])))
        group = rng.randrange(4)
        groups.setdefault(group, []).append(index)
        ids.append(group)
    original = list(bounds)
    index = intervals.IntervalCandidates(bounds, groups)
    for left in [*range(len(bounds)), 80, 0, 149]:
        expected = [
            right
            for right in range(left + 1, len(bounds))
            if ids[left] == ids[right] and bounds[right][0] <= bounds[left][1] and bounds[right][1] >= bounds[left][0]
        ]
        assert index[left] == expected
    assert bounds == original
    with pytest.raises(IndexError):
        index[len(bounds)]


def test_dense_interval_storage_is_linear():
    """复现新增密集页面规模，只允许线性索引及一批候选存活。"""
    count = 6555
    index = intervals.IntervalCandidates([(0.0, 10.0)] * count, {0: list(range(count))})
    assert index[0] == list(range(1, count))
    assert sum(map(len, index.cached_rows)) <= count + 8192
    assert index[count - 1] == []
    assert len(index) == count


def test_native_interval_rejects_invalid_records():
    """直接绑定也必须拒绝非法组号和非有限区间，不能越界或排序崩溃。"""
    native = intervals.get_native()
    if native is None:
        pytest.skip("native backend is not selected")
    for bounds, groups in [([(0, 1)], []), ([(0, 1)], [1]), ([(math.nan, 1)], [0]), ([(2, 1)], [0])]:
        with pytest.raises(ValueError):
            native.BaselineCandidates(bounds, groups)


@pytest.mark.parametrize("seed", range(12))
def test_table_note_query_matches_reference(seed):
    """比较重复来源、上下边界和核心成员排除，保留最高四分位奇偶中位数。"""
    from docvortex.analyzers.native.pdf import table_annotations as notes

    native = intervals.get_native()
    if native is None:
        pytest.skip("native backend is not selected")
    rng = random.Random(seed)
    items = sorted(
        [
            (rng.randrange(80), rng.choice([0.0, 20.0, 50.0, rng.uniform(-50, 100)]), rng.choice([0.1, 5.0, 10.0, 20.0]))
            for _ in range(150)
        ],
        key=lambda item: item[1],
    )
    prepared = notes._PreparedTableNoteBodyMetrics(tuple(items), tuple(item[1] for item in items))
    accelerated = notes._PreparedTableNoteBodyMetrics(prepared.items, prepared.centers, native.TableNoteMetrics(items))
    for _ in range(30):
        top, bottom = sorted((rng.uniform(-20, 100), rng.uniform(-20, 100)))
        box = (0.0, top, 100.0, bottom)
        core = {rng.randrange(90) for _ in range(20)}
        expected = notes._table_note_body_reference_height([], box, 5.0, core, (100, 200), 0, prepared)
        assert notes._table_note_body_reference_height([], box, 5.0, core, (100, 200), 0, accelerated) == expected


@pytest.mark.parametrize("seed", range(30))
def test_lane_predecessor_events_match_reference(seed):
    """覆盖同高行、跨栏迁移和字体冲突，比较完整成员顺序及对象身份守恒。"""
    from docvortex.analyzers.native.pdf import line_layout as layout
    from docvortex.analyzers.native.pdf.models import _LineItem, _TextLane

    rng = random.Random(seed)
    lanes = [_TextLane(float(i * 100), float(i * 100 + 100)) for i in range(3)]
    for index in range(90):
        owner = rng.randrange(3)
        left = float(rng.randrange(3) * 100 + rng.choice((0, 0, 5, 15)))
        top = float(rng.randrange(25) * 6)
        box = (left, top, left + rng.choice((20.0, 65.0, 95.0)), top + 5.0)
        line = _LineItem(
            "body",
            box,
            0,
            index,
            effective_height=5.0,
            font_signature=(rng.choice(("A", "A", "B")), 0),
            font_coverage=1.0,
            semantic_type=rng.choice((None, None, None, "text")),
            visual_row_id=rng.choice((None, int(top / 6))),
            baseline=rng.choice((None, top + 4.0)),
        )
        lanes[owner].lines.append((line, box))
    expected = deepcopy(lanes)
    identities = {id(line) for lane in lanes for line, _box in lane.lines}
    layout._reattach_cross_lane_short_tails_python(expected, 5.0)
    layout._reattach_cross_lane_short_tails(lanes, 5.0)
    assert lanes == expected
    assert {id(line) for lane in lanes for line, _box in lane.lines} == identities


def test_font_snapshot_tracks_mutation_and_conversion():
    """相同字体字典字段变化后必须重新转换，缓存不能改变数值和异常语义。"""
    from docvortex.analyzers.native.pdf.char_geometry import _font_run_key

    font = {"name": "ABCDEF+Example", "size": 12.0, "flags": 0, "weight": 400}
    cache = {}
    for size, weight in ((12.0, 400), (14.0, 700), ("16", "500"), (None, None), (9.0, float("nan"))):
        font.update(size=size, weight=weight)
        for text in ("A", "中", "1", "+"):
            assert _font_run_key({"font": font}, 0, text, cache) == _font_run_key({"font": font}, 0, text)


@pytest.mark.parametrize("seed", range(15))
def test_mapping_ranges_preserve_char_groups(seed):
    """批量映射保持连字组、重复来源和相等阈值，同时保留原始字符对象。"""
    from docvortex.document.pdf.text import dedup
    from docvortex.document.pdf.text._contracts import Bbox

    native = intervals.get_native()
    if native is None:
        pytest.skip("native backend is not selected")
    rng = random.Random(seed)
    fonts = [{"name": name, "size": 10.0, "flags": 0, "weight": 400} for name in ("a", "b")]
    chars = []
    for index in range(100):
        x = float(index // 3)
        chars.append(
            {
                "char": rng.choice("AA 中文 "),
                "char_idx": index,
                "source_indices": (index,),
                "font": rng.choice(fonts),
                "rotation": 0.0,
                "text_object_id": rng.choice((1, 1, 1, None)),
                "bbox": Bbox([x, 0.0, x + 4.0, 10.0]),
                "origin": (x + rng.choice((0.0, 0.001, 0.002)), 10.0),
            }
        )
    expected = dedup._mapping_char_groups_python(chars)
    ranges = dedup._native_mapping_ranges(chars, native)
    assert ranges is not None
    assert [[id(c) for c in chars[start:end]] for start, end in ranges] == [[id(c) for c in group] for group in expected]


def test_zero_rotation_script_snapshot_preserves_inputs(monkeypatch):
    """直接借用普通几何与原物化路径比较全部返回字段，源对象不得被修改。"""
    import pickle
    from docvortex.analyzers.native.pdf.inline import scripts
    from docvortex.analyzers.native.pdf.models import _LineItem
    from docvortex.document.pdf.text._contracts import Bbox

    chars = [
        {"char": c, "char_idx": i, "bbox": Bbox([float(i * 5), 0.0, float(i * 5 + 4), 10.0])} for i, c in enumerate("Ab12")
    ]
    line = _LineItem("Ab12", (0.0, 0.0, 20.0, 10.0), 0, 0, chars=chars)
    tight = {i: (float(i * 5), 1.0, float(i * 5 + 4), 9.0) for i in range(4)}
    origins = {i: (float(i * 5), 9.0) for i in range(4)}
    before = pickle.dumps((line, tight, origins))
    actual = scripts._script_line_char_roles(line, (100.0, 100.0), tight, origins, set())
    with monkeypatch.context() as context:
        context.setattr(scripts, "_plain_script_geometry", lambda *_args: False)
        context.setattr(scripts, "_prepare_plain_script_input", lambda *_args: None)
        expected = scripts._script_line_char_roles(line, (100.0, 100.0), tight, origins, set())
    assert actual == expected
    assert pickle.dumps((line, tight, origins)) == before


@pytest.mark.parametrize("seed", range(12))
def test_title_gaps_match_reference(seed):
    """重复来源键、共享行对象和视觉行相等时仍保持全部物理净空一致。"""
    from docvortex.analyzers.native.pdf.title_analysis import common
    from docvortex.analyzers.native.pdf.models import _LineItem

    rng = random.Random(seed)
    rows = []
    for index in range(50):
        x, y = float(rng.randrange(5) * 20), float(rng.randrange(20) * 5)
        box = (x, y, x + rng.choice((0.0, 20.0, 60.0)), y + rng.choice((5.0, 10.0)))
        line = _LineItem(
            "text",
            box,
            0,
            rng.randrange(45),
            effective_height=5.0,
            visual_row_id=rng.choice((None, index // 4)),
            restored_inline_cluster=bool(index % 2),
        )
        rows.append((line, box))
    rows.append(rows[0])
    assert common._build_physical_title_gap_map(rows) == common._build_physical_title_gap_map_python(rows)


@pytest.mark.parametrize("seed", range(12))
@pytest.mark.parametrize("positive", (False, True))
def test_anchor_pair_statistics_match_reference(seed, positive):
    """分别验证风险与完整样本的宽度准入，包含相等阈值和顺序相关相邻对。"""
    from docvortex.analyzers.native.pdf import char_geometry as geometry

    rng = random.Random(seed)
    rows = []
    for i in range(80):
        x = float(i) * rng.choice((0.1, 1.0, 5.0))
        y = rng.choice((0.0, 0.5, 0.75, 2.5))
        box = (x, y, x + rng.choice((0.0, 3.0, 12.0)), y + 10.0)
        rows.append((box, (x, y + 1.0, x + 3.0, y + 9.0), (x, y + 8.0), (rng.choice(("a", "b")),), 10.0))
    assert geometry._anchor_pair_statistics(rows, positive) == geometry._anchor_pair_statistics_python(rows, positive)


def test_neighbor_indices_match_original_geometry():
    """最近基线的平局保留原索引，并排除同一对象的重复记录。"""
    from types import SimpleNamespace
    from docvortex.analyzers.native.pdf import char_geometry as geometry

    native = intervals.get_native()
    if native is None:
        pytest.skip("native backend is not selected")
    rng = random.Random(335)
    records = [
        (i, (float(i % 3 * 50), 0.0, float(i % 3 * 50 + 60), 10.0), 10.0, float(rng.randrange(20) * 5)) for i in range(60)
    ]
    records.append(records[0])
    objects = [SimpleNamespace(local_source_bbox=box, tight_core=(0.0, 0.0, 10.0, height)) for _, box, height, _ in records]
    expected = []
    for i, a in enumerate(records):
        candidates = [
            j
            for j, b in enumerate(records)
            if a[0] != b[0] and geometry._same_lane(objects[i], objects[j]) and abs(b[3] - a[3]) >= max(2.0, 0.6 * a[2])
        ]
        above = [j for j in candidates if records[j][3] < a[3]]
        below = [j for j in candidates if records[j][3] > a[3]]
        expected.append(
            (
                max(above, key=lambda j: records[j][3]) if above else None,
                min(below, key=lambda j: records[j][3]) if below else None,
            )
        )
    assert native.line_neighbors(records) == expected
