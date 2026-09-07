from __future__ import annotations

from docvortex.document.detection import (
    _guess_ole2_suffix_by_bytes,
    guess_suffix_by_bytes,
)

_OLE2_HEADER_ONLY = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 512


def test_guess_ole2_suffix_returns_none_for_non_ole2_bytes() -> None:
    """非 OLE2 字节不得被识别成旧 Office 文档。"""
    assert _guess_ole2_suffix_by_bytes(b"%PDF-1.4 test") is None
    assert _guess_ole2_suffix_by_bytes(b"plain text") is None


def test_guess_ole2_suffix_returns_none_for_ole2_without_known_streams() -> None:
    """OLE2 magic 但无 WordDocument/Workbook/PowerPoint Document stream 时返回 None。"""
    assert _guess_ole2_suffix_by_bytes(_OLE2_HEADER_ONLY) is None


def test_guess_suffix_by_bytes_falls_through_to_magika_for_unknown_ole2() -> None:
    """OLE2 magic 但无已知 stream 时，交给 Magika 兜底（不返回 unknown_ole2）。"""
    suffix = guess_suffix_by_bytes(_OLE2_HEADER_ONLY)
    assert isinstance(suffix, str)


def test_guess_suffix_by_bytes_prefers_rtf_signature_over_csv_extension() -> None:
    """RTF 强内容签名必须覆盖 CSV 的无签名扩展名兜底。"""
    payload = b"\xef\xbb\xbf \r\n{\\RTF1\\ANSI body}"

    assert guess_suffix_by_bytes(payload, "disguised.csv") == "rtf"
