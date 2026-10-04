from __future__ import annotations

import importlib
from unittest.mock import MagicMock

import pytest

from docvortex.analyzers.native import PdfModel
from docvortex.analyzers.native.pdf import pipeline
from docvortex.document.pdf._document import PDFDocument


def test_pdf_model_predict_returns_native_model_list_without_owning_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The validation model is passed the same PDFDocument and is not responsible for classifying or closing the caller document."""

    pdf_doc = MagicMock(spec=PDFDocument)
    expected_model_list = [[{"type": "text", "content": "native"}]]
    native_analyze = MagicMock(return_value=expected_model_list)
    monkeypatch.setattr(pipeline, "_analyze_native_document", native_analyze)

    result = PdfModel().predict(pdf_doc)

    assert result is expected_model_list
    native_analyze.assert_called_once_with(pdf_doc)
    pdf_doc.classify.assert_not_called()
    pdf_doc.close.assert_not_called()


def test_pdf_model_is_public_and_old_flash_model_is_removed() -> None:
    """Verify that PdfModel is publicly available, the old FlashModel name is no longer compatible."""

    flash_module = importlib.import_module("docvortex.analyzers.native")
    assert flash_module.PdfModel is PdfModel
    assert not hasattr(flash_module, "FlashModel")
