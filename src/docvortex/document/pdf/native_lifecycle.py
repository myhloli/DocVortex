"""PDF 原生子资源关闭，保持原生提取算法与资源语义。"""

from __future__ import annotations
import logging


logger = logging.getLogger("docvortex.document.pdf.document")


def _try_close(obj: object) -> None:
    if callable(close := getattr(obj, "close", None)):
        try:
            close()
        except Exception:
            pass
