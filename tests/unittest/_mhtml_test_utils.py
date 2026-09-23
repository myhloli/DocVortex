"""构建不依赖网络或用户目录的可复现 MIME 测试归档。"""

from __future__ import annotations

import base64
from email import policy
from email.message import EmailMessage
from io import BytesIO
import quopri

from PIL import Image


def image_bytes(color: str = "red") -> bytes:
    """生成可实际解码的微型 PNG，便于检验素材字节不变。"""
    stream = BytesIO()
    Image.new("RGB", (12, 8), color).save(stream, format="PNG")
    return stream.getvalue()


def mime_part(
    payload: bytes,
    media: str = "text/html",
    *,
    location: str | None = None,
    cid: str | None = None,
    charset: str | None = None,
    encoding: str = "base64",
) -> EmailMessage:
    """构造指定编码、地址和标识的 MIME 叶子部件。"""
    part = EmailMessage(policy=policy.SMTP)
    part.set_type(media)
    if charset:
        part.set_param("charset", charset)
    if location:
        part["Content-Location"] = location
    if cid:
        part["Content-ID"] = f"<{cid}>"
    part["Content-Transfer-Encoding"] = encoding
    if encoding == "base64":
        part.set_payload(base64.b64encode(payload).decode("ascii"))
    elif encoding == "quoted-printable":
        part.set_payload(quopri.encodestring(payload).decode("ascii"))
    else:
        part.set_payload(payload)
    return part


def related(*parts: EmailMessage, start: str | None = None, location: str | None = None) -> EmailMessage:
    """构造根或嵌套 related 容器，并保持给定部件顺序。"""
    message = EmailMessage(policy=policy.SMTP)
    message.set_type("multipart/related")
    message.set_param("type", "text/html")
    message.set_boundary("fixture-" + str(id(message)))
    if start:
        message.set_param("start", f"<{start}>")
    if location:
        message["Content-Location"] = location
    for part in parts:
        message.attach(part)
    return message


def build_mhtml_fixture() -> bytes:
    """提供格式矩阵共用的标题、中文正文、表格和内嵌图片。"""
    body = '<html><head><title>Archive</title></head><body><h1>Archive</h1><p>中文正文 Native conversion</p><img src="cid:figure" alt="Figure"><table><tr><td>A</td><td>B</td></tr></table></body></html>'
    return related(mime_part(body.encode()), mime_part(image_bytes(), "image/png", cid="figure")).as_bytes()
