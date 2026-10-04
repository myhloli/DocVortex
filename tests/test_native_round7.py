"""Seventh-round stage caching, independent differentiation of common character source geometry and materialized multiplexing."""

from copy import deepcopy
import math
import random

import pytest

from docvortex.analyzers.native.pdf.models import _LineItem, _TextLane
from docvortex.analyzers.native.pdf.title_analysis import body_profile, page_titles
from docvortex.analyzers.native.pdf import char_geometry
from docvortex._compute_backend import get_native
from docvortex.document.pdf.native_contracts import PDFPageTextGeometry
from docvortex.document.pdf.text import dedup
from docvortex.document.pdf.text._contracts import Bbox
from docvortex.analyzers.native.pdf.inline import detection, matching, scripts


def _profile_lane(seed):
    """Construct column members with variable ordering, font size, font layout, and mixed semantics."""
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
    """Font hits before and after the first sorting cannot be mixed, and semantic changes must be recalculated."""
    lane = _profile_lane(7)
    expected_lane = deepcopy(lane)
    original = body_profile._infer_lane_body_profile
    calls = []

    def counted(value):
        """Logging real reference statistics calls, check cache does not scan stable bar repeatedly."""
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
    """The new analysis phase does not inherit the cache of old geometry, fonts, members, column boundaries, or ordering."""
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
    """The exception scale cannot introduce additional exceptions or change the reference path due to cache pre-checking."""
    lane = _profile_lane(2)
    lane.lines[0][0].effective_height = invalid
    assert not body_profile._LaneProfileContext([lane]).plain


@pytest.mark.parametrize("seed", range(20))
@pytest.mark.parametrize("operation", ["centered", "emphasized", "demote"])
def test_title_decisions_match_uncached_stage(seed, operation, monkeypatch):
    """When original candidate traversal and semantic writing occur continuously, the cache remains consistent with the successive statistics."""
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
    """Compare the original character-by-character source frame paths, covering rotation, cropping, invalid frames and character filtering."""
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
    """Exceptions or non-floating point coordinate returns are explicitly not supported and cannot swallow exceptions for reference paths."""
    native = get_native()
    if native is None:
        pytest.skip("native backend is not selected")
    assert (
        native.source_rows_plain([(0, (0.0, value, 2.0, 3.0), None, (0.0, 1.0, 2.0, 3.0), (0.0, 3.0), 0.0)], (10.0, 10.0), 0)
        is None
    )


def test_plain_source_keeps_zero_bits_and_coordinate_identity():
    """Extreme values and unrotated results retain the original floating point source and positive and negative zero bits."""
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
    """The identity strong reference is bounded, and the special field callback cannot hit stale metadata when changing other fonts."""
    cache = char_geometry._ReadOnlyFontCache()
    for index in range(4100):
        cache.metadata({"name": str(index), "size": 10.0})
    assert len(cache.values) == 4096
    font = {"name": "A", "size": 10.0}
    cache.metadata(font)

    class ChangingSize:
        """Simulate the writing of special font conversion to ordinary fonts."""

        def __float__(self):
            """Keep the original conversion callback and modify the prepared font object."""
            font["size"] = 12.0
            return 8.0

    cache.metadata({"name": "B", "size": ChangingSize()})
    assert cache.metadata(font)[1] == 12.0


@pytest.mark.parametrize("seed", range(24))
def test_stream_mapping_matches_exhaustive_sources(seed):
    """The normal mapping stream compares the original complete grouping, including ligatures, duplicate sources and abnormal Chinese character mappings."""
    rng = random.Random(seed)
    font = {"name": "A", "size": 10.0, "flags": 0, "weight": 400.0}
    chars = []
    for index in range(80):
        left = float(index // 3 * 5)
        chars.append(
            {
                "char": rng.choice("AAff 人人⼈1 "),
                "char_idx": index,
                "source_indices": (index,),
                "bbox": Bbox([left, 0.0, left + 4.0, 10.0]),
                "origin": (left, 9.0),
                "font": font,
                "rotation": 0.0,
                "writing_angle": 0.0,
                "text_object_id": index // 7,
            }
        )
    before = deepcopy([{**char, "bbox": tuple(char["bbox"].bbox)} for char in chars])
    assert dedup._mapping_groups(chars) == dedup._mapping_groups_reference(chars)
    assert [{**char, "bbox": tuple(char["bbox"].bbox)} for char in chars] == before
    for glyph in dedup._mapping_groups(chars):
        if len(glyph.chars) == 1 and glyph.chars[0] == chars[glyph.chars[0]["char_idx"]]:
            assert glyph.chars[0] is chars[glyph.chars[0]["char_idx"]]


def test_mapping_special_object_preserves_error_order():
    """Special sources go to the original group first, and cannot materialize the frame or swallow the original abnormality in advance."""
    chars = [{"char": "A", "char_idx": 0, "bbox": None, "font": {}, "rotation": None}]
    with pytest.raises(TypeError):
        dedup._mapping_groups(chars)
    with pytest.raises(TypeError):
        dedup._mapping_groups_reference(chars)


@pytest.mark.parametrize("seed", range(12))
def test_style_preparation_matches_original_materialization(seed, monkeypatch):
    """The normal font classification fast path without plot lines is the same as the original geometry, bold filtered full output."""
    rng = random.Random(seed)
    lines = []
    for number in range(20):
        chars = []
        for index, text in enumerate("A1 • 中文f"):
            left = float(index * 5)
            chars.append(
                {
                    "char": text,
                    "char_idx": index,
                    "bbox": (left, 0.0, left + 4.0, 10.0),
                    "font": {"name": "A", "flags": 0, "weight": rng.choice([400.0, 700.0])},
                }
            )
        lines.append(_LineItem("text", (0.0, 0.0, 50.0, 10.0), 0, number, chars=chars))
    before = deepcopy(lines)
    actual = detection.detect_pdf_text_style_lines(lines, [])
    with monkeypatch.context() as patch:
        patch.setattr(detection, "_prepare_plain_style_line", lambda *_args: None)
        expected = detection.detect_pdf_text_style_lines(lines, [])
    assert actual == expected
    assert lines == before


def test_projection_cache_tracks_content_and_bounds():
    """Precise content changes, duplicate identities, formulas, and very long text remain in the original projection and bounded capacity."""
    cache = matching._ContentProjectionCache()
    block = {"content": "A\\(x\\)B"}
    first = cache.project(block, block["content"])
    assert first == matching._project_content_chars(block["content"])
    assert cache.project(block, block["content"]) is first
    block["content"] = "<b>A</b>1"
    assert cache.project(block, block["content"]) == matching._project_content_chars(block["content"])
    for index in range(300):
        item = {"content": str(index) * 40}
        assert cache.project(item, item["content"]) == matching._project_content_chars(item["content"])
        assert len(cache.values) <= 256 and cache.characters <= 8192
    long = {"content": "A" * 8193}
    count = len(cache.values)
    assert cache.project(long, long["content"]) == matching._project_content_chars(long["content"])
    assert len(cache.values) == count and id(long) not in cache.values


def test_native_mapping_geometry_keeps_boundaries_and_float_sources():
    """The fusion preparation uses the original adjacent members to compare the shared threshold, and the extreme value box reuses the first character floating point source."""
    native = get_native()
    if native is None:
        pytest.skip("native backend is not selected")
    chars = []
    font = {"name": "A", "size": 10.0, "flags": 0}
    for index, x in enumerate([-0.0, 0.001, 0.0021, 0.0031]):
        chars.append(
            {
                "char": "A",
                "char_idx": index,
                "source_indices": (index,),
                "font": font,
                "bbox": Bbox([x, 0.0, x + 4.0, 10.0]),
                "origin": (x, 9.0),
                "rotation": 0.0,
                "writing_angle": 0.0,
                "text_object_id": 1,
            }
        )
    rows = native.mapping_glyph_rows(chars, Bbox)
    expected_ranges = []
    cursor = 0
    for group in dedup._mapping_char_groups_python(chars):
        expected_ranges.append((cursor, cursor + len(group)))
        cursor += len(group)
    assert [(first, last) for first, last, _box, _angle in rows] == expected_ranges
    assert rows[0][2][0] is chars[0]["bbox"].bbox[0]
    assert dedup._mapping_groups(chars) == dedup._mapping_groups_reference(chars)


def test_stage_profile_special_neighbors_disable_cache():
    """External special containers, scalars, and non-ordinary rows may not be able to reuse ordinary row states when they have callbacks."""
    lane = _profile_lane(0)
    assert not body_profile._stage_profile_context([lane], lane.lines, [(object(), 0.0, 1.0, 2.0)]).plain
    assert not body_profile._stage_profile_context([lane], lane.lines, scalars=(math.nan,)).plain
    assert not body_profile._stage_profile_context([lane], object()).plain


def test_empty_script_analysis_preserves_backend_and_truth_callbacks(monkeypatch):
    """Empty pages eliminate the need for classification preparation, but original parameter true value callbacks and forced backend errors still need to occur."""
    events = []

    class Chars:
        """Mocks an observable external character container."""

        def __bool__(self):
            """Keep the original calling order."""
            events.append("chars")
            return False

    def backend():
        """Simulates incompatible forced extension loading errors."""
        events.append("backend")
        raise ImportError("incompatible backend")

    monkeypatch.setattr("docvortex._compute_backend.get_native", backend)
    with pytest.raises(ImportError):
        scripts.detect_pdf_text_script_lines([], (100.0, 100.0), {}, {}, all_chars=Chars())
    assert events == ["chars", "backend"]


def test_native_mapping_keeps_mixed_glyph_groups():
    """The heterogeneous character protection group does not trigger full-page secondary preparation, and Unicode and its source are still consistent with the exhaustive path."""
    native = get_native()
    if native is None:
        pytest.skip("native backend is not selected")
    chars = [
        {
            "char": text,
            "char_idx": index,
            "source_indices": (index,),
            "font": {"name": "A"},
            "bbox": Bbox([0.0, 0.0, 4.0, 10.0]),
            "origin": (0.0, 9.0),
            "rotation": 0.0,
            "text_object_id": 1,
        }
        for index, text in enumerate("⼈人")
    ]
    assert native.mapping_glyph_rows(chars, Bbox) is not None
    assert dedup._mapping_groups(chars) == dedup._mapping_groups_reference(chars)
    assert dedup._mapping_groups(chars)[0].chars[0]["source_indices"] == (0, 1)
