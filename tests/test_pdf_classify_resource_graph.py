"""The Form resource graph that shares /Resources only performs reachability traversal and is not allowed to enumerate simple paths.

Regression #17: When multiple Form XObject share a /Resources dictionary listing them all,
The results of `_resource_graph_has_cid_without_to_unicode()` are path independent, each Form
Only one access is allowed based on the object identity; the path local loop prevention will degrade to e·N! calls.
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


def _forms_pdf(forms: int, *, shared: bool) -> bytes:
    """One page of text draws `forms` empty Form.

    When shared=True, /Resources of each Form is the object 4 referenced by the page and lists all Form.
    Any two Forms are reachable to each other; otherwise each Form carries its own empty /Resources.
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
    """The CID font is only reachable via the Form link: page /Resources only lists Form, shared object 7 only lists the font with all Form."""
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
    """The number of entries of the resource graph function is counted, and the recursion also passes through the replaced module global name."""
    original = classify._resource_graph_has_cid_without_to_unicode
    counter = {"calls": 0}

    def counted(resources, font_analysis_cache, visited_form_keys=None, visited_resource_keys=None):
        """Record the number of recursive entries of Form, retaining the original parameter semantics of the resource graph function."""
        counter["calls"] += 1
        return original(resources, font_analysis_cache, visited_form_keys, visited_resource_keys)

    monkeypatch.setattr(classify, "_resource_graph_has_cid_without_to_unicode", counted)
    return counter


def _count_shared_resource_work(monkeypatch, resources):
    """Count the number of shared resource expansions and the number of enumerated XObject entries respectively."""
    resolved = classify._resolve_pdf_object(resources)
    xobjects = classify._resolve_pdf_object(resolved.get("/XObject"))
    counts = {"resource_expansions": 0, "xobject_entries": 0}
    original_get = DictionaryObject.get
    original_values = DictionaryObject.values

    def counted_get(self, key, *args, **kwargs):
        """Use read /XObject as the counting point at which the resource dictionary is actually expanded."""
        if self is resolved and key == "/XObject":
            counts["resource_expansions"] += 1
        return original_get(self, key, *args, **kwargs)

    def counted_values(self):
        """Only the entry enumeration of the target shared XObject dictionary is counted."""
        if self is xobjects:
            counts["xobject_entries"] += len(self)
        return original_values(self)

    monkeypatch.setattr(DictionaryObject, "get", counted_get)
    monkeypatch.setattr(DictionaryObject, "values", counted_values)
    return counts


def _classify_shared_forms_worker(data: bytes, result_queue, ready_event, start_event) -> None:
    """Start the classification synchronously after preheating the independent process to avoid counting the process import time into the page deadline."""
    ready_event.set()
    if not start_event.wait(timeout=20.0):
        return
    started = time.perf_counter()
    with PDFDocument(data) as document:
        result = document.classify()
    result_queue.put((result, time.perf_counter() - started))


@pytest.mark.parametrize("shared", [False, True])
def test_form_resource_graph_visits_each_form_once(monkeypatch, shared):
    """The number of calls in both forms of shared and exclusive /Resources is constrained by 2N+1, rather than factorial-level enumeration."""
    forms = 8
    counter = _count_resource_graph_calls(monkeypatch)
    reader = PdfReader(BytesIO(_forms_pdf(forms, shared=shared)))
    resources = reader.pages[0]["/Resources"]

    assert classify._resource_graph_has_cid_without_to_unicode(resources, {}) is False
    assert counter["calls"] <= 2 * forms + 1


def test_classify_returns_txt_for_shared_resource_forms(monkeypatch):
    """End-to-end: When the page shares /Resources with 10 Form, classify returns txt normally and the number of accesses is bounded."""
    forms = 10
    counter = _count_resource_graph_calls(monkeypatch)

    with PDFDocument(_forms_pdf(forms, shared=True)) as document:
        assert document.classify() == "txt"
    assert counter["calls"] <= 2 * forms + 1


@pytest.mark.parametrize("forms", [12, 115])
def test_shared_resource_expansion_is_linear(monkeypatch, forms):
    """Form recursion, resource expansion, and XObject enumeration of shared resources are each subject to linear upper bounds."""
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
    """Shared resource Form classification returns known text patterns within a defined time limit."""
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
    """Identity CID that is only reachable via the Form chain still triggers the signal if the ToUnicode font is missing."""
    reader = PdfReader(BytesIO(_form_chain_cid_pdf(6)))
    resources = reader.pages[0]["/Resources"]

    assert classify._resource_graph_has_cid_without_to_unicode(resources, {}) is True
