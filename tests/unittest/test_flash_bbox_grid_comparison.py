"""Overwrite History bbox Freezes all ruling paths for the Tolerance Comparison Assistant."""

from __future__ import annotations

import pytest

from _flash_pdf_test_utils import (
    _assert_history_page,
    _assert_page_bboxes_within,
    _bbox_grid_steps,
    _page_bbox_fingerprint,
    _page_fingerprint,
)


def _block(bbox: tuple[float, float, float, float], content: str = "sample") -> dict:
    """Constructs a public output block consistent with the historical assertion path."""

    return {"type": "text", "bbox": list(bbox), "content": content}


def test_exact_match_passes_without_allowance() -> None:
    """Without any relaxation, bbox passes exactly as the reference."""

    page = [_block((0.1, 0.2, 0.3, 0.4)), _block((0.5, 0.6, 0.7, 0.8))]
    _assert_page_bboxes_within(page, [(0.1, 0.2, 0.3, 0.4), (0.5, 0.6, 0.7, 0.8)], {}, ("doc", 1))


def test_delta_at_allowed_boundary_passes() -> None:
    """It passes when the scale difference is exactly equal to the allowed value, and does not fail due to boundary values."""

    page = [_block((0.1, 0.2, 0.302, 0.4))]
    _assert_page_bboxes_within(page, [(0.1, 0.2, 0.3, 0.4)], {"0": {"x1": 2}}, ("doc", 1))
    page = [_block((0.1, 0.199, 0.3, 0.4))]
    _assert_page_bboxes_within(page, [(0.1, 0.2, 0.3, 0.4)], {"0": {"y0": 1}}, ("doc", 1))


def test_delta_one_step_beyond_allowance_fails() -> None:
    """Fails if one tick exceeds the allowed value, reporting the block index, coordinates, both values and the difference."""

    page = [_block((0.1, 0.2, 0.303, 0.4))]
    with pytest.raises(AssertionError, match=r"'block', 0, 'x1'.*'delta', 3, 'allowed', 2"):
        _assert_page_bboxes_within(page, [(0.1, 0.2, 0.3, 0.4)], {"0": {"x1": 2}}, ("doc", 1))


def test_unconfigured_coordinate_change_fails() -> None:
    """Coordinates without configured tolerance will fail if there is a scale change."""

    page = [_block((0.1, 0.2, 0.3, 0.401))]
    with pytest.raises(AssertionError, match=r"'block', 0, 'y1'"):
        _assert_page_bboxes_within(page, [(0.1, 0.2, 0.3, 0.4)], {"0": {"x1": 2}}, ("doc", 1))


def test_block_count_mismatch_fails() -> None:
    """Fails if the number of blocks does not match the reference, partial comparison is not allowed."""

    page = [_block((0.1, 0.2, 0.3, 0.4)), _block((0.5, 0.6, 0.7, 0.8))]
    with pytest.raises(AssertionError, match="block count"):
        _assert_page_bboxes_within(page, [(0.1, 0.2, 0.3, 0.4)], {}, ("doc", 1))


@pytest.mark.parametrize("bbox", [(0.1, 0.2, 0.3), (0.1, 0.2, 0.3, float("nan")), None])
def test_illegal_bbox_shape_or_value_fails(bbox) -> None:
    """Fails when the number of coordinates is not four or contains a non-finite value."""

    page = [{"type": "text", "bbox": bbox, "content": "sample"}]
    with pytest.raises(AssertionError):
        _assert_page_bboxes_within(page, [(0.1, 0.2, 0.3, 0.4)], {}, ("doc", 1))


def test_grid_steps_ignore_float_representation_noise() -> None:
    """Quantized grid integer scale is not affected by the 0.001 floating point representation difference."""

    assert _bbox_grid_steps((0.117, 0.2, 0.3, 0.4)) == _bbox_grid_steps((0.117 + 1e-12, 0.2, 0.3, 0.4))
    assert _bbox_grid_steps((0.117, 0.2, 0.3, 0.4)) == [117, 200, 300, 400]


def test_history_page_assertion_rejects_content_or_order_change() -> None:
    """Content or sequence changes always fail by content fingerprint and do not enter the tolerance path."""

    reference_page = [_block((0.1, 0.2, 0.3, 0.4), "first"), _block((0.5, 0.6, 0.7, 0.8), "second")]
    expected = {
        "fingerprint": _page_fingerprint(reference_page),
        "bbox_fingerprint": _page_bbox_fingerprint(reference_page),
        "bbox_tolerance": {"platforms": ["linux"], "reference_blocks": [(0.1, 0.2, 0.3, 0.4), (0.5, 0.6, 0.7, 0.8)], "allowances": {}},
    }
    reordered = [reference_page[1], reference_page[0]]
    with pytest.raises(AssertionError, match="content"):
        _assert_history_page(reordered, expected, ("doc", 1), "linux")


def test_history_page_assertion_branches_on_platform() -> None:
    """The tolerance only takes effect on the platform where the configuration hits, and the remaining platforms maintain the precise bbox fingerprint."""

    reference_page = [_block((0.1, 0.2, 0.3, 0.4))]
    expected = {
        "fingerprint": _page_fingerprint(reference_page),
        "bbox_fingerprint": _page_bbox_fingerprint(reference_page),
        "bbox_tolerance": {"platforms": ["linux"], "reference_blocks": [(0.1, 0.2, 0.3, 0.4)], "allowances": {"0": {"x1": 2}}},
    }
    shifted = [_block((0.1, 0.2, 0.302, 0.4))]
    _assert_history_page(shifted, expected, ("doc", 1), "linux")
    with pytest.raises(AssertionError, match="bbox"):
        _assert_history_page(shifted, expected, ("doc", 1), "darwin")
    _assert_history_page(reference_page, expected, ("doc", 1), "darwin")
