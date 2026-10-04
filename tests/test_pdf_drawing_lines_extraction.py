"""On-demand construction and cluster merging equivalence of plot line extraction (#18 regression).

`get_page_drawing_lines()` must not build `PDFPathInfo` which is then discarded;
`_merge_orientation_lines()` uses the coordinates of the first element of the cluster to replace the entire cluster and rescan.
Both outputs must be bit-by-bit consistent with the fully built, naive rescan implementation.
"""

from __future__ import annotations

import random
import ctypes
from importlib import import_module

import pytest

from docvortex.document.pdf import PDFDocument, native_objects
from docvortex.document.pdf.native_contracts import PDFDrawingLine

native_objects_module = import_module("docvortex.document.pdf.native_objects")


def _pdf(objects: list[bytes]) -> bytes:
    """Write the smallest legal PDF with the xref table in object number order (object 1 is catalog)."""
    out, offsets = bytearray(b"%PDF-1.7\n"), []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


def _vector_page_pdf(paths: int) -> bytes:
    """A4 Short stroke lines alternating horizontally and vertically on the page, simulating the path density exported by CAD."""
    directions = ((40, 0), (0, 40))
    segments = [b"0.4 w"]
    for index in range(paths):
        column, row = index % 40, index // 40
        x, y = 20.0 + column * 14.0, 30.0 + row * 16.0
        dx, dy = directions[index % 2]
        segments.append(b"%.2f %.2f m %.2f %.2f l S" % (x, y, x + dx, y + dy))
    content = b"\n".join(segments)
    return _pdf(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << >> /Contents 4 0 R >>",
            b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
        ]
    )


def _reference_merge_orientation_lines(lines, page_size):
    """Naive implementation before repair: each line recalculates the minimum axis coordinate of the current cluster as a whole."""
    tolerance = native_objects.DRAWING_LINE_MERGE_TOLERANCE
    if not lines:
        return []
    coordinate_clusters: list[list[PDFDrawingLine]] = []
    for line in sorted(
        lines, key=lambda item: (native_objects._line_axis_coordinate(item), native_objects._line_main_interval(item)[0])
    ):
        if (
            not coordinate_clusters
            or native_objects._line_axis_coordinate(line)
            - min(native_objects._line_axis_coordinate(item) for item in coordinate_clusters[-1])
            > tolerance
        ):
            coordinate_clusters.append([line])
        else:
            coordinate_clusters[-1].append(line)

    merged: list[PDFDrawingLine] = []
    for cluster in coordinate_clusters:
        interval_group: list[PDFDrawingLine] = []
        interval_end = float("-inf")
        for line in sorted(cluster, key=lambda item: native_objects._line_main_interval(item)[0]):
            line_start, line_end = native_objects._line_main_interval(line)
            if interval_group and line_start > interval_end + tolerance:
                combined = native_objects._combine_collinear_line_group(interval_group, page_size)
                if combined is not None:
                    merged.append(combined)
                interval_group = []
                interval_end = float("-inf")
            interval_group.append(line)
            interval_end = max(interval_end, line_end)
        if interval_group:
            combined = native_objects._combine_collinear_line_group(interval_group, page_size)
            if combined is not None:
                merged.append(combined)
    return merged


def _random_axis_lines(rng: random.Random, count: int, page_size) -> list[PDFDrawingLine]:
    """Generate on-axis/cross-axis line segments close to and across merge tolerances to cover cluster boundary shapes."""
    lines = []
    for _ in range(count):
        horizontal = rng.random() < 0.5
        coordinate = rng.uniform(10.0, page_size[1] - 10.0) if horizontal else rng.uniform(10.0, page_size[0] - 10.0)
        coordinate += rng.choice((0.0, 0.0, 1.0, 1.9, 2.1, 4.0))
        start = rng.uniform(10.0, page_size[0] * 0.6 if horizontal else page_size[1] * 0.6)
        length = rng.choice((3.0, 20.0, 60.0, 200.0))
        if horizontal:
            candidate = native_objects._make_axis_drawing_line(
                (start, coordinate), (start + length, coordinate), 0.4, page_size
            )
        else:
            candidate = native_objects._make_axis_drawing_line(
                (coordinate, start), (coordinate, start + length), 0.4, page_size
            )
        if candidate is not None:
            lines.append(candidate)
    return lines


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_cluster_start_merge_matches_full_rescan(seed: int) -> None:
    """The merging result of the cluster starting point is consistent bit by bit with the naive implementation of the whole cluster rescan."""
    rng = random.Random(seed)
    page_size = (595.0, 842.0)
    lines = _random_axis_lines(rng, 260, page_size)

    assert native_objects._merge_orientation_lines(lines, page_size) == _reference_merge_orientation_lines(lines, page_size)


def test_drawing_lines_skip_path_info_construction(monkeypatch) -> None:
    """get_page_drawing_lines no longer builds and discards PDFPathInfo, the results are consistent with full extraction."""
    counter = {"calls": 0}
    original = native_objects._path_info_from_object

    def counting(*args, **kwargs):
        """Count the number of times the special entry for drawing lines accidentally builds path information."""
        counter["calls"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(native_objects, "_path_info_from_object", counting)

    data = _vector_page_pdf(600)
    with PDFDocument(data) as document:
        lines_only = document.get_page_drawing_lines(0)
    assert counter["calls"] == 0
    assert lines_only

    monkeypatch.setattr(native_objects, "_path_info_from_object", original)
    with PDFDocument(data) as document:
        with document._open_page(0) as page:
            from docvortex.document.pdf.native_coordinates import _normalize_pdf_page_bbox

            bbox = _normalize_pdf_page_bbox(page.get_bbox())
            full_lines, path_infos = native_objects._extract_page_paths_and_lines(page, bbox, 0)
            infos_only = native_objects._extract_page_paths_and_lines(page, bbox, 0, want_lines=False)[1]

    assert lines_only == full_lines
    assert path_infos == infos_only
    assert path_infos


def test_path_dual_consumption_reads_draw_state_once(monkeypatch) -> None:
    """When the same Path produces lines and summaries at the same time, the drawing status is only allowed to be read once."""
    from docvortex.document.pdf import _object_bridge

    original = native_objects._get_path_visibility
    calls = 0

    def counted(*args, **kwargs):
        """Wrapper reference implementation used to confirm that joint consumption does not read attributes repeatedly."""
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(native_objects, "_get_path_visibility", counted)
    # Union attribute reads are constrained by the Python reference path; the native batch path is additionally covered by the bridge hit test.
    monkeypatch.setattr(_object_bridge, "read_path_evidence", lambda *args, **kwargs: None)
    with PDFDocument(_vector_page_pdf(5)) as document:
        vector = document[0].get_vector_geometry()
        lines, path_infos = vector.drawing_lines, vector.path_infos
    assert lines
    assert len(path_infos) == 5
    assert calls == 5


def test_native_path_evidence_matches_python_reference(monkeypatch) -> None:
    """Batch native Path evidence is field-by-field and sequentially consistent with the per-object Python reference output."""
    from docvortex.document.pdf import _object_bridge
    from docvortex._compute_backend import backend_info, get_native
    from docvortex.document.pdf.native_coordinates import _normalize_pdf_page_bbox

    original_reader = _object_bridge.read_path_evidence
    with PDFDocument(_vector_page_pdf(80)) as document:
        with document._open_page(0) as page:
            bbox = _normalize_pdf_page_bbox(page.get_bbox())
            native_lines, native_infos = native_objects._extract_page_paths_and_lines(page, bbox, 0)
            monkeypatch.setattr(_object_bridge, "read_path_evidence", lambda *args, **kwargs: None)
            reference_lines, reference_infos = native_objects._extract_page_paths_and_lines(page, bbox, 0)

    monkeypatch.setattr(_object_bridge, "read_path_evidence", original_reader)
    assert native_lines == reference_lines
    assert native_infos == reference_infos
    assert len(native_infos) == 80
    if get_native() is not None:
        assert backend_info()["pdfium_path_evidence_bridge_calls"] >= 1


def _open_filled_path_pdf(path_commands: bytes) -> bytes:
    """Constructs a minimal PDF containing only an open-filled Path for native/reference differential."""
    content = b"0 0 0 rg\n" + path_commands
    return _pdf(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 50] /Resources << >> /Contents 4 0 R >>",
            b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
        ]
    )


@pytest.mark.parametrize(
    ("path_commands", "expected_line_count"),
    [
        # The reference implementation does not accept the open path as a thin rectangle when the upper left corner is missing and the lower right corner is repeated.
        (b"20 20 m 30 20 l 30 21 l 30 20 l f", 0),
        # The object matrix will convert the two local hypotenuses into axis-aligned edges, which should be judged as converted line segments.
        (b"q 1 0 -2 1 40 0 cm 20 20 m 30 20 l 32 21 l 22 21 l f Q", 1),
        # Python round(x, 3) differs from Rust by multiplying by thousands and then rounding at this corner point.
        (b"20.0625 20 m 70.0625 20 l 70.0625 21 l 20.062498 21 l f", 1),
    ],
)
def test_native_open_filled_path_edges_match_reference(monkeypatch, path_commands, expected_line_count) -> None:
    """Covering the corner integrity of the open thin rectangle with the matrix rear axis aligning the two back end divergence behaviors."""
    from docvortex.document.pdf import _object_bridge
    from docvortex._compute_backend import backend_info, get_native
    from docvortex.document.pdf.native_coordinates import _normalize_pdf_page_bbox

    original_reader = _object_bridge.read_path_evidence
    calls_before = backend_info()["pdfium_path_evidence_bridge_calls"]
    with PDFDocument(_open_filled_path_pdf(path_commands)) as document:
        with document._open_page(0) as page:
            bbox = _normalize_pdf_page_bbox(page.get_bbox())
            native_lines, native_infos = native_objects._extract_page_paths_and_lines(page, bbox, 0)
            monkeypatch.setattr(_object_bridge, "read_path_evidence", lambda *args, **kwargs: None)
            reference_lines, reference_infos = native_objects._extract_page_paths_and_lines(page, bbox, 0)

    monkeypatch.setattr(_object_bridge, "read_path_evidence", original_reader)
    assert native_lines == reference_lines
    assert native_infos == reference_infos
    assert len(native_lines) == expected_line_count
    with PDFDocument(_open_filled_path_pdf(path_commands)) as document:
        native_page = document.get_page_vector_geometry(0)
    with monkeypatch.context() as reference:
        reference.setattr(_object_bridge, "read_path_evidence", lambda *args, **kwargs: None)
        with PDFDocument(_open_filled_path_pdf(path_commands)) as document:
            reference_page = document.get_page_vector_geometry(0)
    assert native_page == reference_page
    if get_native() is not None and hasattr(get_native(), "read_pdfium_path_evidence"):
        assert backend_info()["pdfium_path_evidence_bridge_calls"] > calls_before


def test_native_path_failed_fill_color_keeps_unknown_rgba(monkeypatch) -> None:
    """Color query failure is still visible as opacity, but Path summary cannot fake black fill."""
    import pypdfium2.raw as raw

    from docvortex._compute_backend import get_native
    from docvortex.analyzers.native.pdf.code_blocks import _path_has_visible_nonwhite_fill
    from docvortex.document.pdf import _object_bridge
    from docvortex.document.pdf.native_coordinates import _normalize_pdf_page_bbox

    if get_native() is None:
        pytest.skip("需要 Rust Path bridge 进行失败颜色差分")
    original = raw.FPDFPageObj_GetFillColor
    callback_type = ctypes.WINFUNCTYPE if hasattr(ctypes, "WINFUNCTYPE") else ctypes.CFUNCTYPE
    failed_color = callback_type(original.restype, *original.argtypes)(lambda *_args: 0)
    monkeypatch.setattr(raw, "FPDFPageObj_GetFillColor", failed_color)
    payload = _open_filled_path_pdf(b"20 20 m 70 20 l 70 21 l 20 21 l f")
    with PDFDocument(payload) as document:
        with document._open_page(0) as page:
            bbox = _normalize_pdf_page_bbox(page.get_bbox())
            native_lines, native_infos = native_objects._extract_page_paths_and_lines(page, bbox, 0)
            with monkeypatch.context() as reference:
                reference.setattr(_object_bridge, "read_path_evidence", lambda *args, **kwargs: None)
                reference_lines, reference_infos = native_objects._extract_page_paths_and_lines(page, bbox, 0)
    assert native_lines == reference_lines
    assert native_infos == reference_infos
    assert len(native_infos) == 1 and native_infos[0].fill_rgba is None
    assert not _path_has_visible_nonwhite_fill(native_infos[0])


def test_drawing_lines_match_vector_and_snapshot_entries() -> None:
    """Drawing lines for the independent line interface, vector geometry, and page snapshots are field-by-field and sequentially consistent."""
    data = _vector_page_pdf(600)
    with PDFDocument(data) as document:
        direct = document.get_page_drawing_lines(0)
        vector = document.get_page_vector_geometry(0).drawing_lines
        snapshot = document.get_page_snapshot(0).vector_geometry.drawing_lines
        native_page = document._extract_native_page(0)

    assert direct == list(vector)
    assert direct == list(snapshot)
    assert direct == native_page.drawing_lines


def test_native_drawing_line_bridge_reports_execution_or_fallback() -> None:
    """Backend reporting must distinguish between the actual execution of new plot line entry and the Python reference fallback."""
    from docvortex._compute_backend import backend_info, get_native

    native = get_native()
    with PDFDocument(_vector_page_pdf(60)) as document:
        document.get_page_drawing_lines(0)
    info = backend_info()
    if native is not None and hasattr(native, "read_pdfium_drawing_lines"):
        assert info["pdfium_drawing_line_bridge_calls"] >= 1
        assert info["pdfium_drawing_line_bridge_unavailable_reason"] is None
    else:
        assert info["pdfium_drawing_line_bridge_calls"] == 0
        assert info["pdfium_drawing_line_bridge_unavailable_reason"] is not None


def test_filled_path_and_transformed_form_use_reference_lines() -> None:
    """Filled Rectangle and Transform Form Preserves the geometry and order of the reference path."""

    content = b"0 0 0 rg 10 10 40 2 re f q 2 0 0 2 0 0 cm /F1 Do Q"
    form_content = b"0.5 w 30 30 m 90 30 l S"
    data = _pdf(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /XObject << /F1 5 0 R >> >> /Contents 4 0 R >>",
            b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
            b"<< /Type /XObject /Subtype /Form /BBox [0 0 100 100] /Resources << >> /Length %d >>\nstream\n%s\nendstream"
            % (len(form_content), form_content),
        ]
    )
    with PDFDocument(data) as document:
        direct = document.get_page_drawing_lines(0)
        vector = document.get_page_vector_geometry(0).drawing_lines
        snapshot = document.get_page_snapshot(0).vector_geometry.drawing_lines
    assert direct
    assert direct == list(vector) == list(snapshot)


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_rust_lines_match_reference_for_page_rotation_and_offset(rotation: int, monkeypatch) -> None:
    """Under four page rotations and a non-zero page origin, the new entry is field-by-field consistent with the Python reference path."""

    content = b"0.4 w 20 30 m 100 30 l S 150 60 m 150 200 l S"
    data = _pdf(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [10 20 300 400] /Rotate %d /Resources << >> /Contents 4 0 R >>" % rotation,
            b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
        ]
    )
    with PDFDocument(data) as document:
        accelerated = document.get_page_drawing_lines(0)

    from docvortex.document.pdf import _object_bridge

    def no_bridge(*_args):
        """Forces the second query to go through the Python reference path."""
        return None

    monkeypatch.setattr(_object_bridge, "read_drawing_lines", no_bridge)
    with PDFDocument(data) as document:
        reference = document.get_page_drawing_lines(0)
    assert accelerated == reference
