"""可脱离 PDFium 生命周期的页面证据；可变字符仅在兼容边界物化。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Literal

from .native_contracts import PDFLinkAnnotation, PDFPageTextGeometry, PDFPageVectorGeometry


@dataclass(frozen=True, eq=False)
class PDFPageSnapshot:
    """持有一页独立证据；公开字符访问返回副本，不把可变对象泄漏给后续消费者。"""

    page_index: int
    page_size: tuple[float, float]
    rotation: Literal[0, 90, 180, 270]
    _geometry: PDFPageTextGeometry = field(repr=False, compare=False)
    _vectors: PDFPageVectorGeometry = field(repr=False, compare=False)
    _links: tuple[PDFLinkAnnotation, ...] = field(repr=False, compare=False)
    _owner: object = field(repr=False, compare=False)

    @property
    def text_geometry(self) -> PDFPageTextGeometry:
        """兼容 Python 字符字典的可变约定，同时隔离消费者之间的修改。"""
        return deepcopy(self._geometry)

    @property
    def vector_geometry(self) -> PDFPageVectorGeometry:
        """返回独立矢量记录，防止调用方修改嵌套坐标后污染缓存。"""
        return deepcopy(self._vectors)

    @property
    def link_annotations(self) -> tuple[PDFLinkAnnotation, ...]:
        """返回独立链接证据，不访问已关闭的页面或文档。"""
        return deepcopy(self._links)

    def validate_page(self, page: object) -> None:
        """拒绝将另一份文档或另一页的快照用于当前页面。"""
        document = getattr(page, "pdf_doc", None)
        if getattr(document, "_snapshot_owner", None) is not self._owner or getattr(page, "_idx", None) != self.page_index:
            raise ValueError("snapshot belongs to a different PDF page")
