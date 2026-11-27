"""已证实卡面组与新增图层研究同时命中时，保留原有组归属。"""

from types import SimpleNamespace

import pytest

from docvortex.document.pdf import raster_composites, raster_occlusion
from docvortex.document.pdf.native_contracts import PDFImageInfo

_FRAME = (25.0, 25.0, 170.0, 130.0)
_PANEL = (44.0, 40.0, 151.0, 111.0)
_RIM = (39.0, 36.0, 155.0, 115.0)
_LATER = (33.0, 31.0, 162.0, 121.0)
_LEAF = SimpleNamespace(matrix=(1.0, 0.0, 0.0, 1.0, 0.0, 0.0), depth=0)


@pytest.mark.parametrize("same_owner", [False, True])
def test_later_frame_respects_previously_verified_same_card(monkeypatch, same_owner):
    """同一既有组内的两层不能再次配对，独立的透明开孔外框仍可接管其内图。"""
    grouped = (_PANEL, _RIM) if same_owner else (_PANEL,)
    members = [
        PDFImageInfo(_FRAME, None, composite_inner_bboxes=grouped),
        PDFImageInfo(_PANEL, None),
        PDFImageInfo(_RIM, None),
    ]
    monkeypatch.setattr(raster_composites, "read_frame_opening", lambda *_: _PANEL)
    monkeypatch.setattr(raster_composites, "screen_fits_opening", lambda *_: True)
    monkeypatch.setattr(raster_composites, "_opaque_foreground", lambda *_: True)
    raster_composites._attach_frame_crops(
        None, _FRAME, (200.0, 200.0), [_LEAF] * 3, members, {0, 1, 2}, {_FRAME: 1, _PANEL: 1, _RIM: 1}
    )
    assert members[2].raster_pair == (None if same_owner else (_PANEL, _RIM))


def test_same_card_veto_still_allows_two_layers_to_have_distinct_outer_cover():
    """只跳过同一旧卡内的候选对，外部新图对多个内层的确证仍可继续。"""
    members = [
        PDFImageInfo(_FRAME, None, composite_inner_bboxes=(_PANEL, _RIM)),
        PDFImageInfo(_PANEL, None),
        PDFImageInfo(_RIM, None),
        PDFImageInfo(_LATER, None),
    ]
    assert set(raster_occlusion._geometry_nominees(members, [_LEAF] * 4)) == {(1, 3), (2, 3)}
