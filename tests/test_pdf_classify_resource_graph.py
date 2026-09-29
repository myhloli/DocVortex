"""共享 /Resources 的 Form 资源图只做可达性遍历，不得枚举简单路径。

回归 #17：多个 Form XObject 共用一个列出它们全部的 /Resources 字典时，
`_resource_graph_has_cid_without_to_unicode()` 的结果与路径无关，每个 Form
按对象身份只允许访问一次；路径局部防环会退化成 e·N! 次调用。
"""

from io import BytesIO
from importlib import import_module
import multiprocessing
import os
import queue
import time

import pytest
from pypdf import PdfReader
from pypdf.generic import DictionaryObject

from docvortex.document.pdf import PDFDocument

classify = import_module("docvortex.document.pdf.classify")


def _pdf(objects: list[bytes]) -> bytes:
    """按对象号顺序写出带 xref 表的最小合法 PDF（对象 1 是 catalog）。"""
    out, offsets = bytearray(b"%PDF-1.7\n"), []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


def _forms_pdf(forms: int, *, shared: bool) -> bytes:
    """一页文本绘制 `forms` 个空 Form。

    shared=True 时每个 Form 的 /Resources 都是被页引用、且列出全部 Form 的对象 4，
    任意两个 Form 互相可达；否则每个 Form 携带自己的空 /Resources。
    """
    xobjects = b" ".join(b"/X%d %d 0 R" % (i, 7 + i) for i in range(forms))
    text = b"BT /F1 12 Tf 72 720 Td (" + b"Plain text on a page with form XObjects. " * 3 + b") Tj ET\n"
    draws = b"".join(b"q /X%d Do Q\n" % i for i in range(forms))
    form_resources = b"4 0 R" if shared else b"<< >>"
    return _pdf(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources 4 0 R /Contents 5 0 R >>",
            b"<< /Font << /F1 6 0 R >> /XObject << %s >> >>" % xobjects,
            b"<< /Length %d >>\nstream\n%s\nendstream" % (len(text + draws), text + draws),
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            *[
                b"<< /Type /XObject /Subtype /Form /BBox [0 0 10 10] /Resources %s /Length 3 >>\nstream\nq Q\nendstream"
                % form_resources
                for _ in range(forms)
            ],
        ]
    )


def _form_chain_cid_pdf(forms: int) -> bytes:
    """CID 字体只经由 Form 链可达：页 /Resources 只列 Form，共享的对象 7 才列字体与全部 Form。"""
    form_refs = b" ".join(b"/X%d %d 0 R" % (i, 9 + i) for i in range(forms))
    text = b"BT /F1 12 Tf 72 720 Td (" + b"Plain text on a page with form XObjects. " * 3 + b") Tj ET\n"
    draws = b"".join(b"q /X%d Do Q\n" % i for i in range(forms))
    return _pdf(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources 4 0 R /Contents 5 0 R >>",
            b"<< /Font << /F1 6 0 R >> /XObject << %s >> >>" % form_refs,
            b"<< /Length %d >>\nstream\n%s\nendstream" % (len(text + draws), text + draws),
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            b"<< /Font << /FC 8 0 R >> /XObject << %s >> >>" % form_refs,
            b"<< /Type /Font /Subtype /Type0 /BaseFont /CIDNoMap /Encoding /Identity-H /DescendantFonts [8 0 R] >>",
            *[
                b"<< /Type /XObject /Subtype /Form /BBox [0 0 10 10] /Resources 7 0 R /Length 3 >>\nstream\nq Q\nendstream"
                for _ in range(forms)
            ],
        ]
    )


def _count_resource_graph_calls(monkeypatch):
    """统计资源图函数的进入次数，递归也经过被替换的模块全局名。"""
    original = classify._resource_graph_has_cid_without_to_unicode
    counter = {"calls": 0}

    def counted(resources, font_analysis_cache, visited_form_keys=None, visited_resource_keys=None):
        """记录 Form 递归进入次数，保留资源图函数的原始参数语义。"""
        counter["calls"] += 1
        return original(resources, font_analysis_cache, visited_form_keys, visited_resource_keys)

    monkeypatch.setattr(classify, "_resource_graph_has_cid_without_to_unicode", counted)
    return counter


def _count_shared_resource_work(monkeypatch, resources):
    """分别统计共享资源展开次数和枚举的 XObject 条目数。"""
    resolved = classify._resolve_pdf_object(resources)
    xobjects = classify._resolve_pdf_object(resolved.get("/XObject"))
    counts = {"resource_expansions": 0, "xobject_entries": 0}
    original_get = DictionaryObject.get
    original_values = DictionaryObject.values

    def counted_get(self, key, *args, **kwargs):
        """用读取 /XObject 作为资源字典实际展开的计数点。"""
        if self is resolved and key == "/XObject":
            counts["resource_expansions"] += 1
        return original_get(self, key, *args, **kwargs)

    def counted_values(self):
        """只统计目标共享 XObject 字典的条目枚举。"""
        if self is xobjects:
            counts["xobject_entries"] += len(self)
        return original_values(self)

    monkeypatch.setattr(DictionaryObject, "get", counted_get)
    monkeypatch.setattr(DictionaryObject, "values", counted_values)
    return counts


def _classify_shared_forms_worker(data: bytes, result_queue, ready_event, start_event) -> None:
    """预热独立进程后同步启动分类，避免把进程导入耗时计入页面期限。"""
    ready_event.set()
    if not start_event.wait(timeout=20.0):
        return
    started = time.perf_counter()
    with PDFDocument(data) as document:
        result = document.classify()
    result_queue.put((result, time.perf_counter() - started))


@pytest.mark.parametrize("shared", [False, True])
def test_form_resource_graph_visits_each_form_once(monkeypatch, shared):
    """共享与独占 /Resources 两种形态的调用次数都被 2N+1 约束，而不是阶乘级枚举。"""
    forms = 8
    counter = _count_resource_graph_calls(monkeypatch)
    reader = PdfReader(BytesIO(_forms_pdf(forms, shared=shared)))
    resources = reader.pages[0]["/Resources"]

    assert classify._resource_graph_has_cid_without_to_unicode(resources, {}) is False
    assert counter["calls"] <= 2 * forms + 1


def test_classify_returns_txt_for_shared_resource_forms(monkeypatch):
    """端到端：页与 10 个 Form 共享 /Resources 时 classify 正常返回 txt 且访问次数有界。"""
    forms = 10
    counter = _count_resource_graph_calls(monkeypatch)

    with PDFDocument(_forms_pdf(forms, shared=True)) as document:
        assert document.classify() == "txt"
    assert counter["calls"] <= 2 * forms + 1


@pytest.mark.parametrize("forms", [12, 115])
def test_shared_resource_expansion_is_linear(monkeypatch, forms):
    """共享资源的 Form 递归、资源展开和 XObject 枚举分别受线性上界约束。"""
    reader = PdfReader(BytesIO(_forms_pdf(forms, shared=True)))
    resources = reader.pages[0]["/Resources"]
    graph_calls = _count_resource_graph_calls(monkeypatch)
    work = _count_shared_resource_work(monkeypatch, resources)

    assert classify._resource_graph_has_cid_without_to_unicode(resources, {}) is False
    assert graph_calls["calls"] <= forms + 2
    assert work["resource_expansions"] <= 2
    assert work["xobject_entries"] <= 2 * forms


@pytest.mark.parametrize(("forms", "seconds_limit"), [(12, 2.0), (115, 5.0)])
@pytest.mark.skipif(bool(os.getenv("CI")), reason="性能时限在独立基准机运行，CI 仅验证操作次数和结果")
def test_shared_forms_classification_deadline(forms: int, seconds_limit: float) -> None:
    """共享资源 Form 分类在明确时限内返回已知的文本模式。"""
    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue()
    ready_event = context.Event()
    start_event = context.Event()
    process = context.Process(
        target=_classify_shared_forms_worker,
        args=(_forms_pdf(forms, shared=True), result_queue, ready_event, start_event),
    )
    process.start()
    try:
        assert ready_event.wait(timeout=20.0), "classifier worker failed to start"
        start_event.set()
        mode, elapsed = result_queue.get(timeout=seconds_limit)
        assert mode == "txt"
        assert elapsed < seconds_limit
    except queue.Empty:
        pytest.fail(f"{forms} shared Form classification did not finish within {seconds_limit} seconds")
    finally:
        if process.is_alive():
            process.kill()
        process.join(timeout=3.0)
        result_queue.close()


def test_form_chain_still_detects_cid_without_to_unicode():
    """仅经 Form 链可达的 Identity CID 缺 ToUnicode 字体仍要触发信号。"""
    reader = PdfReader(BytesIO(_form_chain_cid_pdf(6)))
    resources = reader.pages[0]["/Resources"]

    assert classify._resource_graph_has_cid_without_to_unicode(resources, {}) is True
