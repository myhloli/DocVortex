"""验证公开 PDF 输出的清洗时机，保留原始字形证据及旧结果读取语义。"""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

from bs4 import BeautifulSoup
import pytest

import docvortex
from docvortex.api import postprocess
from docvortex.analyzers.native import PdfModel
from docvortex.analyzers.native.pdf.pipeline import _analyze_native_document
from docvortex.codecs.json import load_model
from docvortex.document.pdf import PDFDocument
from docvortex.schema import ModelJson

_SOURCE = Path(__file__).parents[1] / "demo/pdfs/中文论文4.pdf"
_FULLWIDTH = re.compile("[Ａ-Ｚａ-ｚ０-９：．／＼－＿％＋＝＠＃＆＊]")


def _raw_geometry(document: PDFDocument) -> str:
    """物化原始字符、字体和全部几何数值，避免矩形对象的身份比较掩盖数据变化。"""
    pages = [
        [{**char, "bbox": list(char["bbox"].bbox)} for char in document.get_page_chars_with_geometry(index).chars]
        for index in range(document.page_count)
    ]
    return json.dumps(pages, ensure_ascii=False, sort_keys=True)


def _text_spans(content: Any) -> list[str]:
    """只读取文字和链接显示文字，公式、代码及 URL 不属于普通文字断言。"""
    if not isinstance(content, list):
        return []
    output = []
    for span in content:
        if span.get("type") == "text":
            output.append(span["content"])
        elif span.get("type") == "hyperlink":
            output.extend(_text_spans(span["content"]))
    return output


def _assert_paper_text_is_normalized(pages: list[list[dict[str, Any]]]) -> None:
    """真实论文的普通文字及单元格不再包含目标全角字符，中文标点仍然存在。"""
    text = "".join(text for page in pages for block in page for text in _text_spans(block.get("content")))
    assert text and not _FULLWIDTH.search(text)
    assert "，" in text and "40" in text
    tables = [block["content"] for page in pages for block in page if block["type"] == "table"]
    assert len(tables) == 5
    for markup in tables:
        soup = BeautifulSoup(markup, "html.parser")
        assert soup.find_all("td")
        assert not _FULLWIDTH.search(soup.get_text())


def test_pdf_model_normalizes_after_geometry_and_inline_matching() -> None:
    """公开模型输出改变文字，完整原始字符、分类和输出几何保持一致。"""
    with PDFDocument(str(_SOURCE)) as document:
        evidence = _raw_geometry(document)
        classification = document.classify()
        raw = _analyze_native_document(document)
        actual = PdfModel().predict(document)
        assert _raw_geometry(document) == evidence
        assert document.classify() == classification == "txt"
    assert _FULLWIDTH.search(json.dumps(raw, ensure_ascii=False))
    _assert_paper_text_is_normalized(actual)
    assert len(raw) == len(actual)
    for original_page, normalized_page in zip(raw, actual):
        assert len(original_page) == len(normalized_page)
        for original, normalized in zip(original_page, normalized_page):
            assert {key: value for key, value in original.items() if key != "content"} == {
                key: value for key, value in normalized.items() if key != "content"
            }


def test_public_pdf_parse_exports_normalized_text_and_offline_bundle(tmp_path: Path) -> None:
    """API、HTML、Markdown 和离线结果包使用同一份已经清洗的 ModelJson。"""
    result = docvortex.parse(_SOURCE, keep_model_json=True)
    assert result.model_json is not None
    _assert_paper_text_is_normalized(result.model_json.pages)
    before = result.to_dict()
    for target in ("html", "markdown"):
        path = tmp_path / f"result.{target}"
        result.export(path, output_format=target)
        assert not _FULLWIDTH.search(path.read_text(encoding="utf-8"))
    result.save_bundle(tmp_path / "bundle")
    restored = docvortex.load_bundle(tmp_path / "bundle")
    assert restored.to_dict() == before
    assert restored.model_json is not None
    _assert_paper_text_is_normalized(restored.model_json.pages)
    assert result.to_dict() == before


@pytest.mark.parametrize(
    ("suffix", "source"),
    [
        ("html", "<html><body><p>Ａ１，。：．／</p></body></html>"),
        ("csv", "名称,值\nＡ１：．／,Ｂ２\n"),
    ],
)
def test_non_pdf_public_analysis_does_not_normalize(suffix: str, source: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """其他格式中的全角英数保留，不能在通用 API 边界无条件清洗。"""
    from docvortex import content

    def forbidden(_pages: list[list[dict[str, Any]]]) -> None:
        """禁止非 PDF 分析进入 PDF 专用清洗。"""
        raise AssertionError("Non-PDF normalization")

    monkeypatch.setattr(content, "normalize_pdf_model_text", forbidden)
    result = docvortex.analyze(source.encode(), file_suffix=suffix)
    assert "Ａ１" in result.model_json.to_json()


def test_model_loading_and_rendering_do_not_rewrite_old_pdf_results(tmp_path: Path) -> None:
    """旧 ModelJson、结果包和显式后处理不会因为升级包而隐式转换文本。"""
    model = ModelJson(
        metadata={"file_suffix": "pdf", "producer": {"name": "docvortex", "version": "0.2.0"}},
        page_index_map=[],
        pages=[
            [
                {
                    "type": "text",
                    "content": [{"type": "text", "content": "旧Ａ１"}],
                    "bbox": [0.1, 0.1, 0.9, 0.2],
                    "lines": [{"bbox": [0.1, 0.1, 0.9, 0.2]}],
                }
            ]
        ],
    )
    restored = load_model(json.loads(model.to_json()))
    assert restored.to_dict() == model.to_dict()
    result = postprocess(restored, keep_model_json=True)
    result.save_bundle(tmp_path / "old-bundle")
    loaded = docvortex.load_bundle(tmp_path / "old-bundle")
    loaded.export(tmp_path / "old.md", output_format="markdown")
    assert "旧Ａ１" in (tmp_path / "old.md").read_text(encoding="utf-8")
