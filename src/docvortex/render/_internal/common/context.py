"""在一次内部渲染调用中传递独占副本，不缓存用户的可变文档。"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator

from ....schema import MiddleJson


@dataclass(slots=True)
class _OwnedRenderDocument:
    """只允许一个规划器消费当前调用已经隔离的文档。"""

    document: MiddleJson
    consumed: bool = False


_owned_document: ContextVar[_OwnedRenderDocument | None] = ContextVar("docvortex_owned_render_document", default=None)


@contextmanager
def owned_render_document(document: MiddleJson) -> Iterator[None]:
    """限定独占文档的调用范围，异常和重入后均恢复外层上下文。"""
    token = _owned_document.set(_OwnedRenderDocument(document))
    try:
        yield
    finally:
        _owned_document.reset(token)


def claim_owned_document(document: MiddleJson) -> bool:
    """仅复用当前调用的精确对象一次，回调重入必须重新隔离。"""
    context = _owned_document.get()
    if context is None or context.document is not document or context.consumed:
        return False
    context.consumed = True
    return True


__all__ = ["owned_render_document", "claim_owned_document"]
