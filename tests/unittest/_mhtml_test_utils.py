"""Build a reproducible MIME test archive that does not rely on network or user directories."""

from __future__ import annotations

import base64
from email import policy
from email.message import EmailMessage
from io import BytesIO
import quopri

from PIL import Image


def image_bytes(color: str = "red") -> bytes:
    """Generates a tiny PNG that can actually be decoded, making it easy to verify that the material is byte unchanged."""
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
    """Constructs a MIME leaf part specifying the encoding, address, and identity."""
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
    """Constructs a root or nested related container, maintaining the given parts order."""
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
    """Provides common titles, Chinese text, tables and inline images for the format matrix."""
    body = '<html><head><title>Archive</title></head><body><h1>Archive</h1><p>中文正文 Native conversion</p><img src="cid:figure" alt="Figure"><table><tr><td>A</td><td>B</td></tr></table></body></html>'
    return related(mime_part(body.encode()), mime_part(image_bytes(), "image/png", cid="figure")).as_bytes()
