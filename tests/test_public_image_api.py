"""验证公共图片输出、像素保真、资源释放与载荷遍历契约。"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from io import BytesIO
import subprocess
import sys
from unittest.mock import Mock

from PIL import Image
import pytest
from reportlab.pdfgen.canvas import Canvas

from docvortex.assets import ImageArtifact, ImageFormat, parse_image_data_uri_strict, transcode_image
from docvortex.content.tree import iter_image_payloads
from docvortex.document.pdf import PDFDocument
from docvortex.document.pdf.native_contracts import PDFPageImage
from docvortex.foundation.image import crop_pil_image
from docvortex.foundation.image_encoding import encode_image
from docvortex.schema import ChartBlock, ChartBodyBlock, EquationBlock, ImageBlock, ImageBodyBlock, TableBlock, TableBodyBlock


def _detail_pdf(rotation: int = 0) -> bytes:
    """生成带彩色细线与高对比边缘的矢量页面，避免输入压缩干扰断言。"""
    buffer = BytesIO()
    canvas = Canvas(buffer, pagesize=(120, 80))
    canvas.setPageRotation(rotation)
    for index in range(100):
        canvas.setFillColorRGB(index % 2, (index % 3) / 2, (index % 5) / 4)
        canvas.rect(index + 10, 8, 0.7, 64, fill=1, stroke=0)
    canvas.save()
    return buffer.getvalue()


@pytest.mark.parametrize("image_format,extension", [("jpeg", "jpg"), ("png", "png"), ("webp", "webp")])
@pytest.mark.parametrize("bbox", [None, (0.1, 0.2, 0.8, 0.7)])
def test_image_output_survives_document_close(image_format: ImageFormat, extension: str, bbox: tuple | None) -> None:
    """输出字节独立于文档生命周期，元数据与实际编码尺寸一致。"""
    with PDFDocument(_detail_pdf()) as document:
        artifact = document.render_image(0, bbox=bbox, image_format=image_format)
    assert isinstance(artifact, ImageArtifact)
    assert artifact.extension == extension
    assert artifact.mime_type == f"image/{image_format}"
    with Image.open(BytesIO(artifact.data)) as image:
        assert image.format == image_format.upper()
        assert image.size == (artifact.width, artifact.height)
    with pytest.raises(FrozenInstanceError):
        artifact.width = 1


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
@pytest.mark.parametrize("bbox", [(0.13, 0.17, 0.83, 0.77), (-0.1, -0.1, 1.1, 1.1)])
def test_png_crop_preserves_rendered_pixels(rotation: int, bbox: tuple) -> None:
    """PNG 裁剪与同一次配置的原始页面像素相同，包括旋转及边界裁剪。"""
    with PDFDocument(_detail_pdf(rotation)) as document:
        rendered = document.render_page(0, scale=1.5).pil_image
        try:
            with crop_pil_image(bbox, rendered) as expected:
                artifact = document.render_image(0, bbox=bbox, image_format="png", scale=1.5)
                with Image.open(BytesIO(artifact.data)) as actual:
                    assert actual.mode == expected.mode
                    assert actual.size == expected.size
                    assert actual.tobytes() == expected.tobytes()
        finally:
            rendered.close()
        with Image.open(BytesIO(document.crop_image(bbox, 0))) as legacy:
            assert legacy.format == "JPEG"


@pytest.mark.parametrize("bbox", [(0, 0, 0, 1), (0, float("nan"), 1, 1), (2, 2, 3, 3)])
def test_invalid_region_rejected(bbox: tuple) -> None:
    """拒绝空区域、非有限坐标和完全超出页面的区域。"""
    with PDFDocument(_detail_pdf()) as document:
        with pytest.raises(ValueError):
            document.render_image(0, bbox=bbox)


@pytest.mark.parametrize("failure", [False, True])
@pytest.mark.parametrize("with_crop", [False, True])
def test_render_closes_owned_images(monkeypatch: pytest.MonkeyPatch, failure: bool, with_crop: bool) -> None:
    """真实图像在编码成功和异常时均关闭，区域副本也不泄漏。"""
    import docvortex.document.pdf.document as implementation

    page = Image.new("RGB", (20, 20))
    crop = Image.new("RGB", (10, 10))
    monkeypatch.setattr(PDFDocument, "render_page", Mock(return_value=PDFPageImage(page, 1)))
    monkeypatch.setattr(implementation, "crop_pil_image", Mock(return_value=crop))
    if failure:
        monkeypatch.setattr(implementation, "encode_image", Mock(side_effect=OSError("encode failed")))
    with PDFDocument(b"") as document:
        if failure:
            with pytest.raises(OSError, match="encode failed"):
                document.render_image(0, bbox=(0, 0, 0.5, 0.5) if with_crop else None)
        else:
            document.render_image(0, bbox=(0, 0, 0.5, 0.5) if with_crop else None)
    with pytest.raises(ValueError):
        page.getpixel((0, 0))
    if with_crop:
        with pytest.raises(ValueError):
            crop.getpixel((0, 0))
    else:
        crop.close()


@pytest.mark.parametrize("failure", [False, True])
def test_transcode_closes_conversion_copy(monkeypatch: pytest.MonkeyPatch, failure: bool) -> None:
    """透明图片转 JPEG 时关闭转换副本，并由转码入口关闭源图。"""
    import docvortex.foundation.image_encoding as implementation

    source = Image.new("RGBA", (5, 4), (200, 30, 50, 0))
    converted = source.convert("RGB")
    monkeypatch.setattr(Image, "open", Mock(return_value=source))
    monkeypatch.setattr(source, "convert", Mock(return_value=converted))
    if failure:
        monkeypatch.setattr(implementation, "image_to_bytes", Mock(side_effect=OSError("encode failed")))
        with pytest.raises(OSError):
            transcode_image(b"input")
    else:
        artifact = transcode_image(b"input")
        assert (artifact.width, artifact.height) == (5, 4)
    for image in (source, converted):
        with pytest.raises(ValueError):
            image.getpixel((0, 0))


def test_public_asset_validation() -> None:
    """公共入口沿用严格签名校验，拒绝损坏图片与未知输出格式。"""
    from docvortex.foundation.image_encoding import image_to_b64str

    with Image.new("RGBA", (3, 2), (20, 50, 90, 100)) as image:
        uri = image_to_b64str(image, "PNG")
        data, extension = parse_image_data_uri_strict(uri)
        assert extension == "png"
        artifact = transcode_image(data)
        with Image.open(BytesIO(artifact.data)) as jpeg:
            assert jpeg.mode == "RGB"
        with pytest.raises(ValueError):
            encode_image(image, image_format="gif")
    for invalid in ("invalid", "data:image/png;base64,!!!!", uri.replace("image/png", "image/jpeg")):
        with pytest.raises(ValueError):
            parse_image_data_uri_strict(invalid)
    with pytest.raises(OSError):
        transcode_image(b"broken")


def test_payload_order_and_tree_immutability() -> None:
    """图表、表格、图片和公式按前序输出载荷，并保留原树内容。"""
    image = ImageBodyBlock(type="image_body", content="", index=0)
    chart = ChartBodyBlock(type="chart_body", content="", index=3)
    table = TableBodyBlock(type="table_body", content="", index=1)
    roots = [
        ImageBlock(type="image", content=[image], index=0),
        TableBlock(type="table", content=[table], index=1),
        EquationBlock(type="equation", content="x=1", index=2),
        ChartBlock(type="chart", content=[chart], index=3),
    ]
    before = [block.model_dump() for block in roots]
    payloads = [list(iter_image_payloads(block)) for block in roots]
    assert payloads[0] == [image]
    assert payloads[1] == [table]
    assert payloads[3] == [chart]
    assert payloads[2] == [roots[2]]
    assert [block.model_dump() for block in roots] == before


def test_public_assets_import_without_model_dependencies() -> None:
    """素材公共入口不导入 PDF 渲染、MinerU 或模型重依赖。"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from docvortex.assets import ImageArtifact, ImageFormat, transcode_image; "
            "import sys; assert not {'mineru', 'torch', 'transformers', 'cv2', 'pypdfium2', 'PIL'} & sys.modules.keys()",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
