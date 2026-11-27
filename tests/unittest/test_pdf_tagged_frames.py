"""用有真实软蒙版和独立结构节点的 PDF 检查图层合成、读序与结构关联。"""

from io import BytesIO

from PIL import Image, ImageDraw
from pypdf.generic import DecodedStreamObject, NameObject, NumberObject
import pytest

from _tagged_pdf_fixtures import TaggedPdfBuilder, pdf_dictionary
from docvortex import parse


def _raster_xobject(builder, name, image):
    """将源色与可选的 8 位软蒙版作为独立 XObject 写入页面。"""

    def stream(pixels, space):
        """写入带像素尺寸、色彩空间和实际字节的原生 PDF 图像。"""
        layer = DecodedStreamObject()
        layer.set_data(pixels.tobytes())
        layer.update(
            pdf_dictionary(
                Type=NameObject("/XObject"),
                Subtype=NameObject("/Image"),
                Width=NumberObject(pixels.width),
                Height=NumberObject(pixels.height),
                ColorSpace=NameObject(space),
                BitsPerComponent=NumberObject(8),
            )
        )
        return layer

    obj = stream(image.convert("RGB"), "/DeviceRGB")
    if image.mode == "RGBA":
        obj[NameObject("/SMask")] = builder.writer._add_object(stream(image.getchannel("A"), "/DeviceGray"))
    builder.pages[0]["/Resources"][NameObject("/XObject")][NameObject("/" + name)] = builder.writer._add_object(obj)


def _marked_phone_document(case, *, left=12, scale=1):
    """真实纸面先画两个异色屏幕，再画有透明留边的外框，结构树另外给出顺序。"""
    page = TaggedPdfBuilder()
    page.pages[0]["/Resources"][NameObject("/XObject")] = pdf_dictionary()
    frame = Image.new("RGBA", (128, 192), (24, 24, 24, 0))
    ink = ImageDraw.Draw(frame)
    ink.rounded_rectangle((15, 5, 112, 184), radius=10, fill=(29, 29, 29, 255))
    ink.rounded_rectangle((19, 11, 108, 177), radius=6, fill=(0, 0, 0, 0))
    if case == "opaque":
        frame = Image.new("RGB", (128, 192), (40, 80, 142))
    elif case == "opaque-hole":
        frame = Image.new("RGBA", (128, 192), (40, 80, 142, 255))
        ImageDraw.Draw(frame).rectangle((44, 51, 72, 83), fill=(40, 80, 142, 0))
    elif case == "opaque-translucent":
        frame = Image.new("RGBA", (128, 192), (40, 80, 142, 170))
    _raster_xobject(page, "Border", frame)
    _raster_xobject(page, "Left", Image.new("RGB", (90, 167), "#bb2113"))
    _raster_xobject(page, "Right", Image.new("RGB", (90, 167), "#1b609b"))

    def place(name, x, y, w, h, *, alt=None):
        """为本次物理放置建立单独的 MCID 和 Figure 身份。"""
        mcid = page.next_mcid[0]
        page.next_mcid[0] += 1
        status = ""
        if case == "composed" and name != "Border":
            status = f"0.2 0.2 0.2 rg {x + 8 * scale} {y + h - 1} {w - 16 * scale} {11 * scale} re f "
        page.commands[0].extend(f"/Figure <</MCID {mcid}>> BDC q {w} 0 0 {h} {x} {y} cm /{name} Do Q {status}EMC\n".encode())
        ref = pdf_dictionary(Type=NameObject("/MCR"), Pg=page.pages[0].indirect_reference, MCID=NumberObject(mcid))
        return page.node("Figure", [ref], alt=alt)

    starts = (left, left + 129 * scale)
    subjects = [
        place(name, x + 19 * scale, 117 + 14 * scale, 90 * scale, 167 * scale)
        for x, name in zip(starts, ("Left", "Right"), strict=True)
    ]
    if case == "competing":
        extra = place("Right", left + 19 * scale, 117 + 14 * scale, 90 * scale, 167 * scale, alt="Independent inset")
    frames = [
        place("Border", x, 117, 128 * scale, 192 * scale, alt="Named exterior" if case == "named" else None) for x in starts
    ]
    nodes = [*frames, *reversed(subjects)]
    if case == "competing":
        nodes.append(extra)
    if case == "separate":
        nodes = [page.node("Sect", frames), page.node("Sect", subjects)]
    nodes.extend(page.node("P", [page.text(f"Editable independent paragraph {i}.", x=25, y=95 - 15 * i)]) for i in range(3))
    return page.finish([page.node("Sect", nodes)]), frames, subjects


@pytest.mark.parametrize(("left", "scale", "case"), [(12, 1, "normal"), (29, 0.82, "normal"), (12, 1, "composed")])
def test_tagged_aperture_unites_the_correct_screen_and_keeps_both_node_paths(tmp_path, left, scale, case):
    """输出两个完整屏幕，真实 PNG 同时含边框与各自颜色；两个原节点指向该图。"""
    data, frames, subjects = _marked_phone_document(case, left=left, scale=scale)
    result = parse(data, keep_model_json=True)
    page = result.to_dict()["pages"][0]
    images = [b for b in page["blocks"] if b["type"] == "image"]
    assert len(images) == 2
    assert images[0]["bbox"][0] < images[1]["bbox"][0]
    result.export(tmp_path / "doc.html", output_format="html")
    result.export(tmp_path / "doc.md", output_format="markdown")
    import numpy as np

    for b, red in zip(images, (True, False), strict=True):
        path = b["content"][0]["image_path"]
        assert path in (tmp_path / "doc.html").read_text() and path in (tmp_path / "doc.md").read_text()
        with Image.open(BytesIO(result.assets[path])) as im:
            ar = np.asarray(im.convert("RGB")).astype(int)
        assert (ar.max(axis=2) < 70).mean() > 0.025
        colour = ar[:, :, 0] - ar[:, :, 2]
        assert ((colour > 70) if red else (colour < -70)).mean() > 0.43
    extension = result.to_dict()["extensions"]["docvortex_pdf_structure"]["blocks"]
    for frame, subject in zip(frames, subjects, strict=True):
        assert extension[f"{frame.idnum}:0"] == extension[f"{subject.idnum}:0"]
    assert "Editable independent paragraph 1" in (tmp_path / "doc.md").read_text()


@pytest.mark.parametrize(
    ("case", "expected"), [("named", 4), ("opaque-hole", 4), ("opaque-translucent", 4), ("separate", 4), ("competing", 4)]
)
def test_independent_tagged_figures_keep_their_images(case, expected):
    """独立命名、真透明开孔、跨结构组和同槽竞争均须保留原独立含义。"""
    source, _, _ = _marked_phone_document(case)
    result = parse(source, keep_model_json=True)
    images = [b for b in result.to_dict()["pages"][0]["blocks"] if b["type"] == "image"]
    assert len(images) == expected


def test_opaque_sibling_cover_leaves_only_visible_frame_pixels():
    """整层实心覆盖时仅导出原纸的两个图面，早画异色节点和 MCID 继续可追溯。"""
    import numpy as np

    source, frames, subjects = _marked_phone_document("opaque")
    result = parse(source, keep_model_json=True)
    page = result.to_dict()["pages"][0]
    images = [b for b in page["blocks"] if b["type"] == "image"]
    assert len(images) == len(result.assets) == 2
    for image in images:
        with Image.open(BytesIO(result.assets[image["content"][0]["image_path"]])) as png:
            pixels = np.asarray(png.convert("RGB"))
        assert np.all(pixels[3:-3, 3:-3] == (40, 80, 142))
    extension = result.to_dict()["extensions"]["docvortex_pdf_structure"]
    for node in frames:
        assert extension["blocks"][f"{node.idnum}:0"]
    for index, node in enumerate(subjects):
        identity = f"{node.idnum}:0"
        assert not extension["blocks"].get(identity)
        assert extension["nodes"][identity]["children"][0]["mcid"] == index
        assert any(member["mcid"] == index for member in extension["members"]["0"])
