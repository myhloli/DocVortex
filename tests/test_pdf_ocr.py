"""覆盖 OCR 下载故障、模型坐标与公开入口的离线回归。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import base64
from hashlib import sha256
from io import BytesIO
from http.client import IncompleteRead
import multiprocessing
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.error import URLError

import numpy as np
import pytest
from click.testing import CliRunner
from PIL import Image
from reportlab.pdfgen.canvas import Canvas

import docvortex
from docvortex.analyzers.ocr import download, pdf, runtime
from docvortex.analyzers.ocr.layout import LayoutModel, LayoutRegion
from docvortex.analyzers.ocr.text import TextLine, TextModel, crop_line, decode_ctc
from docvortex.cli import main
from docvortex.document.pdf import PDFDocument
from docvortex.errors import DocumentError


def _pdf_bytes(pages: int = 1) -> bytes:
    """在内存构造文本 PDF，用于页映射和文档资源所有权验证。"""
    output = BytesIO()
    canvas = Canvas(output)
    for page in range(pages):
        for row in range(30):
            canvas.drawString(30, 800 - row * 20, f"Page {page + 1}, native document line {row}.")
        canvas.showPage()
    canvas.save()
    return output.getvalue()


@pytest.fixture
def small_models(monkeypatch):
    """替换权重清单为小资源，下载测试无需联网或传输真实模型。"""
    payloads = {"Layout/a.onnx": b"layout", "OCR/b.yml": b"config"}
    assets = tuple(download.ModelFile(name, len(value), sha256(value).hexdigest()) for name, value in payloads.items())
    monkeypatch.setattr(download, "MODEL_FILES", assets)
    return payloads


@pytest.mark.parametrize("failure", ["network", "truncated", "corrupted"])
def test_download_falls_back_atomically_and_reuses_offline_cache(tmp_path, monkeypatch, small_models, failure):
    """首源故障或无效响应切换次源，有效缓存重复调用时完全不联网。"""
    calls = []

    def fetch(request, timeout):
        """模拟 HF 三种失败和 ModelScope 有效响应。"""
        calls.append(request.full_url)
        asset = next(asset for asset in download.MODEL_FILES if asset.path.split("/")[-1] in request.full_url)
        payload = small_models[asset.path]
        if "huggingface.co" in request.full_url:
            if failure == "network":
                raise URLError("offline")
            payload = payload[:-1] if failure == "truncated" else b"x" * len(payload)
        return BytesIO(payload)

    monkeypatch.setattr(download, "urlopen", fetch)
    assert download.ensure_models(tmp_path) == tmp_path.resolve()
    assert len(calls) == 3
    assert "huggingface.co" in calls[0] and "modelscope.cn" in calls[1]
    assert not list(tmp_path.rglob(".download-*"))
    monkeypatch.setattr(download, "urlopen", Mock(side_effect=AssertionError("network forbidden")))
    assert download.ensure_models(tmp_path) == tmp_path.resolve()


def test_download_repairs_corruption_and_serializes_callers(tmp_path, monkeypatch, small_models):
    """并发首次下载只获取每个文件一次，缓存同大小损坏后仍能校验发现。"""
    calls = []

    def fetch(request, timeout):
        """记录实际请求并返回固定内容。"""
        asset = next(asset for asset in download.MODEL_FILES if request.full_url.endswith(asset.path))
        calls.append(asset.path)
        return BytesIO(small_models[asset.path])

    monkeypatch.setattr(download, "urlopen", fetch)
    with ThreadPoolExecutor(max_workers=3) as pool:
        assert list(pool.map(download.ensure_models, [tmp_path] * 3)) == [tmp_path.resolve()] * 3
    assert sorted(calls) == sorted(small_models)
    asset = download.MODEL_FILES[0]
    (tmp_path / asset.path).write_bytes(b"!" * asset.size)
    download.ensure_models(tmp_path)
    assert calls.count(asset.path) == 2


def test_download_failure_keeps_existing_file_and_cleans_partial(tmp_path, monkeypatch, small_models):
    """双源失败时错误包含文件及来源，旧文件不被临时响应覆盖。"""
    destination = tmp_path / download.MODEL_FILES[0].path
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"old")
    monkeypatch.setattr(download, "urlopen", Mock(side_effect=URLError("unreachable")))
    with pytest.raises(DocumentError, match="Layout/a.onnx.*huggingface.*modelscope") as captured:
        download.ensure_models(tmp_path)
    assert captured.value.code == "ocr_model_download_failed"
    assert destination.read_bytes() == b"old"
    assert not list(tmp_path.rglob(".download-*"))


def _download_worker(root):
    """在独立子进程配置相同小资源，通过共享请求日志核验跨进程下载锁。"""
    payload = b"model"
    asset = download.ModelFile("OCR/a.onnx", len(payload), sha256(payload).hexdigest())
    download.MODEL_FILES = (asset,)

    def fetch(request, timeout):
        """只在实际请求时追加日志，返回独立响应缓冲。"""
        with (root / "requests.txt").open("a") as handle:
            handle.write("request\n")
        return BytesIO(payload)

    download.urlopen = fetch
    download.ensure_models(root)


def test_download_lock_across_processes(tmp_path):
    """三个 spawn 进程同时获取空缓存，网络获取只能发生一次。"""
    context = multiprocessing.get_context("spawn")
    processes = [context.Process(target=_download_worker, args=(tmp_path,)) for _ in range(3)]
    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=30)
            assert process.exitcode == 0
        assert (tmp_path / "requests.txt").read_text() == "request\n"
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)


def test_interrupted_response_falls_back(tmp_path, monkeypatch, small_models):
    """HTTP 响应中途断开仍能切换来源，并且不会留下半文件。"""
    calls = []

    class Interrupted(BytesIO):
        """模拟已连接但读取期间被截断的响应。"""

        def read(self, size):
            """模拟标准库的响应不完整异常。"""
            raise IncompleteRead(b"partial", 100)

    def fetch(request, timeout):
        """首源响应中断，后续资源走可用的次源。"""
        calls.append(request.full_url)
        asset = next(asset for asset in download.MODEL_FILES if asset.path.split("/")[-1] in request.full_url)
        return Interrupted() if "huggingface.co" in request.full_url else BytesIO(small_models[asset.path])

    monkeypatch.setattr(download, "urlopen", fetch)
    download.ensure_models(tmp_path)
    assert len(calls) == 3 and not list(tmp_path.rglob(".download-*"))


def test_layout_uses_rgb_and_model_order_and_clips_boxes():
    """图内顺序优先于几何位置，框使用原图尺度，并核验 RGB 通道没有反转。"""
    model = object.__new__(LayoutModel)
    model.labels, model.height, model.width, model.interpolation = ["text", "table"], 800, 800, "cubic"
    boxes = np.array(
        [[0, 0.9, -5, 50, 20, 150, 2, 0], [1, 0.8, 30, 10, 110, 40, 1, 0], [0, 0.2, 0, 0, 10, 10, 0, 0]], dtype=np.float32
    )
    model.session = Mock()
    model.session.run.return_value = [boxes, np.array([3], dtype=np.int32)]
    rgb = np.full((100, 100, 3), [255, 0, 0], dtype=np.uint8)
    regions = model.predict(rgb)
    assert [item.label for item in regions] == ["table", "text"]
    assert regions[0].bbox == (30, 10, 100, 40)
    assert regions[1].bbox == (0, 50, 20, 100)
    feed = model.session.run.call_args.args[1]
    np.testing.assert_array_equal(feed["image"][0, :, 0, 0], [1, 0, 0])
    np.testing.assert_array_equal(feed["scale_factor"], [[8, 8]])
    model.session.run.return_value[1] = np.array([2], dtype=np.int32)
    with pytest.raises(ValueError, match="count"):
        model.predict(rgb)


def test_ctc_blank_repeat_space_and_empty():
    """空白将重复字符隔开，连续重复仅保留一次，空格保留且全 blank 得到空串。"""
    prediction = np.eye(4, dtype=np.float32)[[1, 1, 0, 1, 3, 2, 2]]
    assert decode_ctc(prediction, ["", "A", "B", " "]) == ("AA B", 1.0)
    assert decode_ctc(np.eye(4)[[0, 0]], ["", "A", "B", " "]) == ("", 0)
    with pytest.raises(ValueError, match="dictionary"):
        decode_ctc(prediction, ["", "A"])


def test_text_detector_maps_boxes_and_recognition_uses_bgr():
    """已知概率区域回映射到裁图像素，识别归一化保留 BGR 契约。"""
    model = object.__new__(TextModel)
    model.det_config = {"thresh": 0.3, "box_thresh": 0.5, "unclip_ratio": 1.5, "max_candidates": 1000}
    probability = np.zeros((64, 128), dtype=np.float32)
    probability[20:35, 30:70] = 0.9
    boxes = model._boxes(probability, (256, 128))
    assert len(boxes) == 1
    assert boxes[0][:, 0].min() < 60 and boxes[0][:, 0].max() > 138
    assert boxes[0][:, 1].min() < 40 and boxes[0][:, 1].max() > 68
    model.rec_config = {"image_shape": [3, 48, 320], "rec_batch_num": 6, "max_width": 2560, "min_width": 16}
    model.characters = ["", "A"]
    model.recognizer = Mock()
    model.recognizer.run.return_value = [np.array([[[0.0, 1.0]]], dtype=np.float32)]
    red_bgr = np.full((24, 100, 3), [0, 0, 255], dtype=np.uint8)
    assert model._recognize([red_bgr]) == [("A", 1.0)]
    pixels = model.recognizer.run.call_args.args[1]["x"]
    np.testing.assert_array_equal(pixels[0, :, 0, 0], [-1, -1, 1])
    assert np.all(pixels[:, :, :, 200:] == 0)
    crop = crop_line(red_bgr, np.array([[0, 0], [99, 0], [99, 23], [0, 23]], dtype=np.float32))
    assert crop.shape == (23, 99, 3)
    np.testing.assert_array_equal(crop[10, 10], [0, 0, 255])


def test_table_ownership_formula_screenshot_and_typed_output(tmp_path):
    """表格内部块只输出一次，公式和编号合并截图，正文沿用 Span 及既有导出契约。"""
    rgb = np.full((200, 200, 3), 240, dtype=np.uint8)
    rgb[5:15, 5:15] = 0
    regions = [
        LayoutRegion("doc_title", (0, 0, 200, 20), 0.9),
        LayoutRegion("table", (0, 25, 200, 100), 0.9),
        LayoutRegion("text", (10, 30, 100, 50), 0.9),
        LayoutRegion("image", (110, 30, 180, 80), 0.9),
        LayoutRegion("display_formula", (20, 110, 150, 140), 0.9),
        LayoutRegion("formula_number", (153, 115, 168, 135), 0.9),
        LayoutRegion("inline_formula", (5, 5, 10, 10), 0.9),
    ]
    text_model = Mock()
    text_model.predict.return_value = [TextLine(np.array([[3, 3], [40, 3], [40, 15], [3, 15]]), "Hello", 0.9)]
    blocks, diagnostics = pdf._analyze_page(rgb, regions, text_model, 4)
    assert not diagnostics
    assert [block["type"] for block in blocks] == ["doc_title", "table", "equation"]
    assert text_model.predict.call_count == 2
    assert text_model.predict.call_args.kwargs == {"table": True}
    assert blocks[0]["content"] == [{"type": "text", "content": "Hello"}]
    assert "Hello" in blocks[1]["content"]
    assert blocks[2]["content"] == "" and blocks[2]["bbox"][2] == 168 / 200
    from docvortex.schema import ModelJson

    result = docvortex.postprocess_document(
        ModelJson(
            pages=[blocks], page_index_map=[4], metadata={"file_suffix": "pdf", "producer": {"name": "test", "version": "1"}}
        ),
        keep_model_json=True,
    )
    assert result.assets
    assert result.middle_json.pages[0].page_idx == 4
    assert "Hello" in docvortex.render_artifact(result.middle_json, "markdown", assets=result.assets).content.decode()
    result.save_bundle(tmp_path / "bundle")
    restored = docvortex.load_bundle(tmp_path / "bundle")
    assert restored.to_dict() == result.to_dict() and restored.model_json is not None
    assert all(restored.assets[name] == payload for name, payload in result.assets.items())


def test_real_layout_duplicate_title_is_emitted_once():
    """真实数学样本的同框 doc_title/paragraph_title 不应生成重复标题。"""
    regions = [
        LayoutRegion("doc_title", (699, 298, 1037, 344), 0.9),
        LayoutRegion("paragraph_title", (699, 298, 1037, 344), 0.95),
    ]
    assert [region.label for region in pdf._prepare_regions(regions)] == ["doc_title"]


@pytest.mark.parametrize("number_first", [False, True])
def test_adjacent_formula_number_merges_into_complete_screenshot(number_first):
    """相邻编号不受横向间距或垂直重叠限制，公式截图同时包含公式和编号像素。"""
    rgb = np.full((140, 200, 3), 255, dtype=np.uint8)
    rgb[65:85, 45:85] = [220, 0, 0]
    rgb[55:75, 170:195] = [0, 0, 220]
    equation = LayoutRegion("display_formula", (45, 65, 85, 85), 0.9)
    number = LayoutRegion("formula_number", (170, 55, 195, 75), 0.9)
    regions = [number, equation] if number_first else [equation, number]
    text_model = Mock(side_effect=AssertionError("Formula numbers must not be recognized separately"))
    blocks, diagnostics = pdf._analyze_page(rgb, regions, text_model, 0)
    assert not diagnostics and [block["type"] for block in blocks] == ["equation"]
    assert blocks[0]["content"] == "" and blocks[0]["bbox"] == (45 / 200, 55 / 140, 195 / 200, 85 / 140)
    text_model.predict.assert_not_called()
    with Image.open(BytesIO(base64.b64decode(blocks[0]["image_base64"].split(",", 1)[1]))) as crop:
        assert crop.size == (150, 30)
        red, blue = crop.getpixel((20, 20)), crop.getpixel((140, 10))
        assert red[0] > 150 and red[2] < 50
        assert blue[2] > 150 and blue[0] < 50


def test_formula_pairing_preserves_order_and_prefers_trailing_number():
    """连续公式按相邻顺序认领；同一公式的后编号优先，不能重复吸收前编号。"""
    first = LayoutRegion("display_formula", (20, 20, 60, 40), 0.9)
    second = LayoutRegion("display_formula", (20, 60, 60, 80), 0.9)
    first_number = LayoutRegion("formula_number", (160, 20, 180, 40), 0.9)
    second_number = LayoutRegion("formula_number", (160, 60, 180, 80), 0.9)
    original = [first, first_number, second, second_number]
    prepared = pdf._prepare_regions(original)
    assert [region.label for region in prepared] == ["display_formula", "display_formula"]
    assert [region.bbox for region in prepared] == [(20, 20, 180, 40), (20, 60, 180, 80)]
    assert original[0].bbox == (20, 20, 60, 40) and original[2].bbox == (20, 60, 60, 80)
    prepared = pdf._prepare_regions([first_number, second, second_number])
    assert [region.label for region in prepared] == ["formula_number", "display_formula"]
    assert prepared[0] == first_number and prepared[1].bbox == (20, 60, 180, 80)


@pytest.mark.parametrize("separator_label", ["text", "table"])
def test_formula_number_does_not_cross_content_or_table(separator_label):
    """即使编号几何上靠近公式，也不能跨越阅读顺序中的正文或表格配对。"""
    equation = LayoutRegion("display_formula", (10, 20, 60, 40), 0.9)
    number = LayoutRegion("formula_number", (63, 20, 80, 40), 0.9)
    separator = LayoutRegion(separator_label, (100, 60, 180, 90), 0.9)
    assert pdf._prepare_regions([equation, separator, number]) == [equation, separator, number]
    assert pdf._prepare_regions([number, separator, equation]) == [number, separator, equation]


def test_formula_pairing_runs_after_removing_containers_and_invalid_regions():
    """过滤行内检测、重复文本、表内编号和无效框之后才决定公式编号归属。"""
    equation = LayoutRegion("display_formula", (10, 20, 60, 40), 0.9)
    number = LayoutRegion("formula_number", (150, 20, 180, 40), 0.9)
    ignored = [
        LayoutRegion("inline_formula", (100, 10, 120, 15), 0.9),
        LayoutRegion("unsupported", (100, 60, 180, 90), 0.9),
        LayoutRegion("text", (100, 60, 100, 90), 0.9),
    ]
    prepared = pdf._prepare_regions([equation, *ignored, number])
    assert len(prepared) == 1 and prepared[0].bbox == (10, 20, 180, 40)
    table = LayoutRegion("table", (100, 15, 195, 50), 0.9)
    assert pdf._prepare_regions([equation, number, table]) == [equation, table]


@pytest.mark.parametrize("label,expected", [("header_image", "header"), ("footer_image", "footer")])
@pytest.mark.parametrize("has_text", [False, True])
def test_marginal_images_use_text_semantics_and_default_export_filters(label, expected, has_text):
    """页眉页脚图片进入 OCR 文本路径，无识别也保留辅助类型，不出现在默认正文导出。"""
    from docvortex.schema import ModelJson

    rgb = np.full((100, 200, 3), 255, dtype=np.uint8)
    rgb[5:10, 5:10] = 0
    quad = np.array([[2, 2], [100, 2], [100, 12], [2, 12]], dtype=np.float32)
    text_model = Mock()
    text_model.predict.side_effect = [[TextLine(quad, "MARGINAL", 0.99)] if has_text else [], [TextLine(quad, "BODY", 0.99)]]
    blocks, diagnostics = pdf._analyze_page(
        rgb, [LayoutRegion(label, (0, 0, 200, 20), 0.9), LayoutRegion("text", (0, 40, 200, 80), 0.9)], text_model, 3
    )
    assert [block["type"] for block in blocks] == [expected, "text"]
    assert blocks[0]["content"] == ([{"type": "text", "content": "MARGINAL"}] if has_text else [])
    assert "image_base64" not in blocks[0]
    assert text_model.predict.call_count == 2
    assert [item.code for item in diagnostics] == ([] if has_text else ["ocr_text_empty"])
    model = ModelJson(
        pages=[blocks], page_index_map=[3], metadata={"file_suffix": "pdf", "producer": {"name": "test", "version": "1"}}
    )
    result = docvortex.postprocess_document(model)
    assert result.middle_json.pages[0].blocks[0].type == expected and not result.assets
    for target in ("markdown", "html"):
        content = docvortex.render_artifact(result.middle_json, target).content.decode()
        assert "BODY" in content and "MARGINAL" not in content


@pytest.mark.parametrize("label", ["header_image", "footer_image", "header", "footer"])
def test_blank_marginal_retains_type(label):
    """均匀空白页眉页脚区域也保留空 Span，不能改成正文图或被无条件丢弃。"""
    text_model = Mock()
    text_model.predict.return_value = []
    blocks, diagnostics = pdf._analyze_page(
        np.full((100, 200, 3), 255, dtype=np.uint8), [LayoutRegion(label, (0, 0, 200, 20), 0.9)], text_model, 0
    )
    assert len(blocks) == 1 and blocks[0]["type"] == label.replace("_image", "")
    assert blocks[0]["content"] == [] and "image_base64" not in blocks[0] and not diagnostics


@pytest.mark.parametrize("label", ["header", "footer"])
def test_duplicate_marginal_text_and_image_labels_are_recognized_once(label):
    """同框页眉页脚文字与图片检测按既有去重规则只识别一次。"""
    regions = [LayoutRegion(label, (0, 0, 200, 20), 0.8), LayoutRegion(label + "_image", (0, 0, 200, 20), 0.9)]
    assert len(pdf._prepare_regions(regions)) == 1


@pytest.mark.parametrize("classification", ["txt", "ocr"])
def test_auto_routes_and_preserves_caller_document(monkeypatch, classification):
    """默认模式按分类路由，调用后不关闭调用方持有的文档。"""
    monkeypatch.setattr(PDFDocument, "classify", Mock(return_value=classification))
    ocr = Mock(return_value=([[]], ()))
    monkeypatch.setattr(pdf, "analyze_pdf_ocr", ocr)
    with PDFDocument(_pdf_bytes()) as document:
        result = docvortex.analyze(document)
        assert document.page_count == 1
    assert ocr.call_count == (classification == "ocr")
    assert len(result.model_json.pages) == 1


def test_forced_txt_skips_classification_and_ocr(monkeypatch):
    """强制文本路径和底层原生模型完全不经过自动分类及 OCR。"""
    monkeypatch.setattr(PDFDocument, "classify", Mock(side_effect=AssertionError("classify forbidden")))
    monkeypatch.setattr(pdf, "get_runtime", Mock(side_effect=AssertionError("OCR forbidden")))
    assert docvortex.parse(_pdf_bytes(), parse_mode="txt").middle_json.pages
    from docvortex.analyzers.native import PdfModel

    with PDFDocument(_pdf_bytes()) as document:
        assert PdfModel().predict(document)


def test_auto_classifies_only_selected_pages(monkeypatch):
    """自动分类必须看到抽页后的文档，不能用整份原文的分类替代所选页。"""

    def classify(document):
        """核验分类实际收到一页，再选择 OCR。"""
        assert document.page_count == 1
        return "ocr"

    monkeypatch.setattr(PDFDocument, "classify", classify)
    ocr = Mock(return_value=([[]], ()))
    monkeypatch.setattr(pdf, "analyze_pdf_ocr", ocr)
    result = docvortex.analyze(_pdf_bytes(3), page_range="3")
    assert result.model_json.page_index_map == [2] and ocr.call_args.args[1] == [2]


def test_forced_ocr_page_map_and_cli_forwarding(tmp_path, monkeypatch):
    """强制 OCR 绕过分类，分析器接收抽页映射，CLI 参数传到公开入口。"""
    monkeypatch.setattr(PDFDocument, "classify", Mock(side_effect=AssertionError("classify forbidden")))
    ocr = Mock(return_value=([[]], ()))
    monkeypatch.setattr(pdf, "analyze_pdf_ocr", ocr)
    result = docvortex.parse(_pdf_bytes(3), page_range="2", parse_mode="ocr", keep_model_json=True)
    assert ocr.call_args.args[1] == [1]
    assert result.model_json.page_index_map == [1]
    assert result.middle_json.pages[0].page_idx == 1
    source = tmp_path / "source.pdf"
    source.write_bytes(_pdf_bytes())
    result = CliRunner().invoke(main, ["convert", str(source), "-o", str(tmp_path / "result.md"), "--parse-mode", "ocr"])
    assert result.exit_code == 0, result.output
    assert ocr.call_count == 2


@pytest.mark.parametrize("mode", ["txt", "ocr", "invalid"])
def test_non_pdf_explicit_modes_rejected(mode):
    """非 PDF 不接受显式 PDF 模式，非法字符串统一返回稳定参数错误。"""
    with pytest.raises(DocumentError) as captured:
        docvortex.analyze(b"<p>Hello</p>", file_suffix="html", parse_mode=mode)
    assert captured.value.code == "parse_mode_invalid"
    assert docvortex.analyze(b"<p>Hello</p>", file_suffix="html").model_json.pages


def test_blank_fallback_and_image_lifetime(monkeypatch):
    """空白页保持记录，推理失败或完成后都关闭页图，不关闭调用方文档。"""
    image = Image.new("RGB", (100, 100), "white")
    model = Mock()
    model.predict.return_value = []
    text = Mock()
    text.predict.return_value = []
    monkeypatch.setattr(pdf, "get_runtime", Mock(return_value=(model, text)))
    document = SimpleNamespace(page_count=1, render_page=Mock(return_value=SimpleNamespace(pil_image=image)))
    assert pdf.analyze_pdf_ocr(document) == ([[]], ())
    with pytest.raises(ValueError):
        image.getpixel((0, 0))
    image = Image.new("RGB", (100, 100), "white")
    document.render_page.return_value.pil_image = image
    model.predict.side_effect = RuntimeError("inference broken")
    with pytest.raises(DocumentError, match="page 8") as captured:
        pdf.analyze_pdf_ocr(document, [7])
    assert captured.value.code == "ocr_inference_failed"
    with pytest.raises(ValueError):
        image.getpixel((0, 0))


def test_runtime_is_cached_by_process(monkeypatch, tmp_path):
    """同进程只创建一套模型，进程身份变化后重新创建会话。"""
    from docvortex.analyzers.ocr import layout, text

    runtime._cached_runtime.cache_clear()
    layout_factory, text_factory = Mock(), Mock()
    monkeypatch.setattr(layout, "LayoutModel", layout_factory)
    monkeypatch.setattr(text, "TextModel", text_factory)
    assert runtime._cached_runtime(str(tmp_path), 1) == runtime._cached_runtime(str(tmp_path), 1)
    assert layout_factory.call_count == text_factory.call_count == 1
    runtime._cached_runtime(str(tmp_path), 2)
    assert layout_factory.call_count == text_factory.call_count == 2
    runtime._cached_runtime.cache_clear()
