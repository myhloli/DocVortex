"""验证选页重写直接返回实际页号、故障回退和 PDF 文档所有权。"""

from __future__ import annotations

from io import BytesIO

import pypdfium2 as pdfium
import pytest
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen.canvas import Canvas

from docvortex.document.pdf import PDFDocument
from docvortex.document.pdf import pdfium as rewrite_module
from docvortex.document.source import prepare_source
from docvortex.errors import InvalidRequestError


@pytest.fixture
def payload() -> bytes:
    """创建六页可区分的正文、尺寸、链接和源属性，供实际选页核验。"""
    stream = BytesIO()
    canvas = Canvas(stream, invariant=1)
    canvas.setTitle("Selection source title")
    canvas.setAuthor("Source author")
    for index in range(6):
        canvas.setPageSize((220 + index * 10, 320 + index * 5))
        canvas.drawString(20, 200, f"Source page {index + 1}")
        canvas.linkURL(f"https://example.invalid/{index + 1}", (20, 190, 140, 215))
        canvas.showPage()
    canvas.save()
    return stream.getvalue()


def _page_states(data: bytes) -> list[dict]:
    """从实际输出提取每页文字、尺寸和链接，避免仅信任返回的页号列表。"""
    reader = PdfReader(BytesIO(data))
    return [
        {
            "text": page.extract_text(),
            "box": list(page.mediabox),
            "rotation": page.get("/Rotate", 0),
            "links": [annotation.get_object()["/A"]["/URI"] for annotation in page.get("/Annots", [])],
        }
        for page in reader.pages
    ]


def _track_documents(monkeypatch) -> list[tuple[object, object]]:
    """记录真实 PDFium 文档的构造输入，验证源文档打开次数与关闭结果。"""
    documents = []
    original = pdfium.PdfDocument.__init__

    def initialize(document, source, *args, **kwargs):
        """完成真实构造后保留句柄引用，避免析构掩盖遗漏的显式关闭。"""
        original(document, source, *args, **kwargs)
        documents.append((document, source))

    monkeypatch.setattr(pdfium.PdfDocument, "__init__", initialize)
    return documents


@pytest.mark.parametrize("arguments,expected", [
    ({}, [0, 1, 2, 3, 4, 5]),
    ({"start_page_id": 1, "end_page_id": 3}, [1, 2, 3]),
    ({"start_page_id": -2, "end_page_id": 2}, [0, 1, 2]),
    ({"start_page_id": 4, "end_page_id": 99}, [4, 5]),
    ({"start_page_id": 3, "end_page_id": -1}, [3, 4, 5]),
    ({"page_indices": [5, 1, 1, -1, 50, 0]}, [0, 1, 5]),
    ({"page_indices": [4, 2], "start_page_id": 0, "end_page_id": 0}, [2, 4]),
])
def test_rewrite_maps_actual_imported_pages_without_reopening(payload, arguments, expected, monkeypatch) -> None:
    """标准化只执行一次、原文只打开一次，实际输出内容与映射逐页对应。"""
    documents = _track_documents(monkeypatch)
    calls = []
    normalize = rewrite_module._normalize_rewrite_page_indices

    def record_normalization(*args, **kwargs):
        """记录实际标准化结果，禁止成功后为映射再次计算。"""
        result = normalize(*args, **kwargs)
        calls.append(result)
        return result

    monkeypatch.setattr(rewrite_module, "_normalize_rewrite_page_indices", record_normalization)
    result = rewrite_module.safe_rewrite_pdf_bytes_with_pdfium_result(payload, **arguments)
    assert result.retained_page_indices == expected
    assert not result.used_original and result.broken_page_indices == []
    assert calls == [expected]
    assert len(documents) == 2
    assert sum(isinstance(source, bytes) and source == payload for _, source in documents) == 1
    assert all(document.raw is None for document, _ in documents)
    assert _page_states(result.pdf_bytes) == [_page_states(payload)[index] for index in expected]


@pytest.mark.parametrize("entrypoint", ["rewrite_pdf_bytes_with_pdfium", "safe_rewrite_pdf_bytes_with_pdfium"])
def test_byte_helpers_keep_return_type_and_actual_selected_content(payload, entrypoint) -> None:
    """保留两个字节辅助入口的签名与实际选页行为，不要求随机 PDF ID 相同。"""
    result = getattr(rewrite_module, entrypoint)(payload, page_indices=[4, 0, 4])
    assert isinstance(result, bytes)
    assert _page_states(result) == [_page_states(payload)[index] for index in [0, 4]]


@pytest.mark.parametrize("arguments", [
    {"page_indices": []}, {"page_indices": [-1, 99]}, {"start_page_id": 5, "end_page_id": 2},
    {"start_page_id": 99},
])
def test_empty_selection_keeps_raw_empty_and_safe_original(payload, arguments, monkeypatch) -> None:
    """低层空选择仍返回空字节，安全入口仍回退原文且不提供虚假页号。"""
    documents = _track_documents(monkeypatch)
    assert rewrite_module.rewrite_pdf_bytes_with_pdfium(payload, **arguments) == b""
    result = rewrite_module.safe_rewrite_pdf_bytes_with_pdfium_result(payload, **arguments)
    assert result.pdf_bytes is payload
    assert result.used_original and result.retained_page_indices is None and result.broken_page_indices == []
    assert all(document.raw is None for document, _ in documents)


def test_zero_page_document_keeps_existing_load_error_and_safe_fallback(monkeypatch) -> None:
    """零页 PDF 仍由底层拒绝打开，安全入口保留返回原文的既有合同。"""
    stream = BytesIO()
    PdfWriter().write(stream)
    data = stream.getvalue()
    documents = _track_documents(monkeypatch)
    with pytest.raises(pdfium.PdfiumError, match="Failed to load document"):
        rewrite_module.rewrite_pdf_bytes_with_pdfium(data)
    result = rewrite_module.safe_rewrite_pdf_bytes_with_pdfium_result(data)
    assert result.pdf_bytes is data and result.used_original
    assert result.retained_page_indices is None and result.broken_page_indices == []
    assert all(document.raw is None for document, _ in documents)


@pytest.mark.parametrize("stage", ["normalize", "new", "import", "save"])
def test_raw_rewrite_failure_closes_every_created_document(payload, stage, monkeypatch) -> None:
    """标准化、新建输出、导入与保存异常均释放本次已创建的真实文档。"""
    documents = _track_documents(monkeypatch)

    def fail(*args, **kwargs):
        """在指定阶段注入异常，不替换其他 PDFium 资源操作。"""
        raise RuntimeError(f"failure at {stage}")

    if stage == "normalize":
        monkeypatch.setattr(rewrite_module, "_normalize_rewrite_page_indices", fail)
    else:
        monkeypatch.setattr(pdfium.PdfDocument, {"new": "new", "import": "import_pages", "save": "save"}[stage], fail)
    with pytest.raises(RuntimeError, match=f"failure at {stage}"):
        rewrite_module.rewrite_pdf_bytes_with_pdfium(payload, page_indices=[0, 2])
    assert len(documents) == (1 if stage in {"normalize", "new"} else 2)
    assert all(document.raw is None for document, _ in documents)


@pytest.mark.parametrize("broken,expected", [({1, 2}, [0, 4, 5]), ({2}, [0, 1, 4, 5]), ({0, 1, 2, 4, 5}, [])])
def test_fallback_filters_broken_and_unrequested_pages(payload, broken, expected, monkeypatch) -> None:
    """首次导入失败后实际探页，仅报告请求集合内的坏页并映射实际保留页。"""
    documents = _track_documents(monkeypatch)
    original_import = pdfium.PdfDocument.import_pages
    original_getitem = pdfium.PdfDocument.__getitem__
    imports = []
    loaded_pages = []

    def import_pages(document, source, indices, *args, **kwargs):
        """仅阻断第一次常规导入，回退使用真实 PDFium 生成剩余页面。"""
        imports.append(list(indices))
        if len(imports) == 1:
            raise RuntimeError("initial import failed")
        return original_import(document, source, indices, *args, **kwargs)

    def get_page(document, index):
        """模拟选中及未选中的损坏页，并记录成功加载页面的关闭状态。"""
        if index in broken:
            raise RuntimeError("broken page")
        page = original_getitem(document, index)
        loaded_pages.append(page)
        return page

    monkeypatch.setattr(pdfium.PdfDocument, "import_pages", import_pages)
    monkeypatch.setattr(pdfium.PdfDocument, "__getitem__", get_page)
    result = rewrite_module.safe_rewrite_pdf_bytes_with_pdfium_result(payload, page_indices=[5, 0, 4, 1, 1])
    assert result.broken_page_indices == sorted(broken & {0, 1, 4, 5})
    assert result.used_original == (not expected)
    assert result.retained_page_indices == (expected or None)
    if expected:
        assert imports == [[0, 1, 4, 5], expected]
        assert _page_states(result.pdf_bytes) == [_page_states(payload)[index] for index in expected]
    else:
        assert result.pdf_bytes is payload
        assert imports == [[0, 1, 4, 5]]
    assert all(document.raw is None for document, _ in documents)
    assert all(page.raw is None for page in loaded_pages)


@pytest.mark.parametrize("failure", ["error", "empty"])
def test_failed_fallback_keeps_original_and_existing_empty_diagnostics(payload, failure, monkeypatch) -> None:
    """两次重写失败或为空时保持最终原文回退，不凭空新增坏页诊断。"""
    documents = _track_documents(monkeypatch)
    saves = []

    def save(document, buffer, *args, **kwargs):
        """模拟保存失败或未写出字节，两条路径均必须释放已打开句柄。"""
        saves.append(document)
        if failure == "error":
            raise RuntimeError("save failed")

    monkeypatch.setattr(pdfium.PdfDocument, "save", save)
    result = rewrite_module.safe_rewrite_pdf_bytes_with_pdfium_result(payload, page_indices=[0, 2])
    assert len(saves) == 2
    assert result.pdf_bytes is payload and result.used_original
    assert result.retained_page_indices is None and result.broken_page_indices == []
    assert all(document.raw is None for document, _ in documents)


@pytest.mark.parametrize("failing_attempt", [1, 2])
def test_font_error_still_propagates_after_cleanup(payload, failing_attempt, monkeypatch) -> None:
    """字体运行时错误在正常与回退分支都继续向上传播，不能伪装成原文成功。"""
    documents = _track_documents(monkeypatch)
    attempts = []

    def fail_import(*args, **kwargs):
        """必要时先触发普通回退，再在指定导入次数触发字体错误。"""
        attempts.append(None)
        if len(attempts) == failing_attempt:
            raise rewrite_module.PdfiumFontError("font failure")
        raise RuntimeError("initial import failure")

    monkeypatch.setattr(pdfium.PdfDocument, "import_pages", fail_import)
    with pytest.raises(rewrite_module.PdfiumFontError, match="font failure"):
        rewrite_module.safe_rewrite_pdf_bytes_with_pdfium_result(payload, page_indices=[0, 2])
    assert len(attempts) == failing_attempt
    assert all(document.raw is None for document, _ in documents)


@pytest.mark.parametrize("caller_owned", [False, True])
@pytest.mark.parametrize("selection,expected", [
    ("", None), ("all", None), ("1,3,3,r1", [0, 2, 5]), ("2-4", [1, 2, 3]),
    ("4-99", [3, 4, 5]), ("r3-r1", [3, 4, 5]), ("99", "error"), ("3-1", "error"), ("r99", "error"),
])
def test_prepare_source_mapping_metadata_and_ownership(payload, caller_owned, selection, expected, monkeypatch) -> None:
    """上层选页保留源属性与原页号，成功和失败均不得关闭调用方借出的文档。"""
    documents = _track_documents(monkeypatch)
    caller = PDFDocument(payload) if caller_owned else None
    caller_handle = caller._pdf_doc if caller is not None else None
    prepared = None
    try:
        if expected == "error":
            with pytest.raises(InvalidRequestError) as error:
                prepare_source(caller if caller_owned else payload, file_suffix="pdf", page_range=selection)
            assert error.value.code == "page_range_invalid"
        else:
            prepared = prepare_source(caller if caller_owned else payload, file_suffix="pdf", page_range=selection)
            assert prepared.page_index_map == expected
            assert prepared.broken_page_indices == ()
            assert prepared.owns_document == (not caller_owned or expected is not None)
            assert prepared.document.page_count == (len(expected) if expected is not None else 6)
            assert prepared.source_properties.title == "Selection source title"
            assert prepared.source_properties.page_count == 6
            assert prepared.document is caller if caller_owned and expected is None else prepared.document is not caller
        if caller is not None:
            assert caller._pdf_doc_opened is caller_handle and caller_handle.raw
    finally:
        if prepared is not None and prepared.owns_document:
            prepared.document.close()
        if caller is not None:
            caller.close()
    assert all(document.raw is None for document, _ in documents)


def test_prepare_source_deletes_only_mapping_reopen(payload, monkeypatch) -> None:
    """强制打开选页结果后总共四次构造，其中原输入两次，排除仅重算映射的第五次。"""
    documents = _track_documents(monkeypatch)
    prepared = prepare_source(payload, file_suffix="pdf", page_range="1,3")
    try:
        assert prepared.document.page_count == 2
        assert prepared.page_index_map == [0, 2]
        assert len(documents) == 4
        assert sum(isinstance(source, bytes) and source == payload for _, source in documents) == 2
    finally:
        prepared.document.close()
    assert all(document.raw is None for document, _ in documents)


@pytest.mark.parametrize("caller_owned", [False, True])
def test_prepare_source_original_fallback_does_not_keep_requested_mapping(payload, caller_owned, monkeypatch) -> None:
    """保存失败并回退原文后，准备层恢复全页输入且不误用请求的两页映射。"""
    documents = _track_documents(monkeypatch)
    caller = PDFDocument(payload) if caller_owned else None
    handle = caller._pdf_doc if caller is not None else None

    def fail_save(*args, **kwargs):
        """正常及回退保存均失败，让真实安全重写返回原输入。"""
        raise RuntimeError("cannot save")

    monkeypatch.setattr(pdfium.PdfDocument, "save", fail_save)
    prepared = prepare_source(caller if caller_owned else payload, file_suffix="pdf", page_range="1,3")
    try:
        assert prepared.data is payload
        assert prepared.page_index_map is None and prepared.broken_page_indices == ()
        assert prepared.document.page_count == 6 and prepared.owns_document
        if caller is not None:
            assert caller._pdf_doc_opened is handle and handle.raw
    finally:
        prepared.document.close()
        if caller is not None:
            caller.close()
    assert all(document.raw is None for document, _ in documents)


@pytest.mark.parametrize("caller_owned", [False, True])
def test_prepared_output_construction_failure_keeps_caller_handle(payload, caller_owned, monkeypatch) -> None:
    """重写成功但新包装构造失败时仍清理内部句柄，同时保留借用原文档。"""
    documents = _track_documents(monkeypatch)
    caller = PDFDocument(payload) if caller_owned else None
    handle = caller._pdf_doc if caller is not None else None
    initialize = PDFDocument.__init__

    def fail_output(document, source, *args, **kwargs):
        """只在重写字节创建新包装时失败，原文包装保持真实行为。"""
        if source != payload:
            raise RuntimeError("output construction failed")
        initialize(document, source, *args, **kwargs)

    monkeypatch.setattr(PDFDocument, "__init__", fail_output)
    try:
        with pytest.raises(RuntimeError, match="output construction failed"):
            prepare_source(caller if caller_owned else payload, file_suffix="pdf", page_range="1,3")
        if caller is not None:
            assert caller._pdf_doc_opened is handle and handle.raw
    finally:
        if caller is not None:
            caller.close()
    assert all(document.raw is None for document, _ in documents)
