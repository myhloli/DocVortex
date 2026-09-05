"""独立文档引擎的可定位错误，不依赖宿主 HTTP 或任务状态。"""

from __future__ import annotations


class DocumentError(ValueError):
    """携带稳定错误码的文档输入或处理错误。"""

    def __init__(self, code: str, message: str, param: str | None = None) -> None:
        """保存供命令行及上层应用映射的错误信息。"""
        super().__init__(message)
        self.code = code
        self.message = message
        self.param = param


class InvalidRequestError(DocumentError):
    """表示页范围或格式选项不符合文档输入契约。"""


__all__ = ["DocumentError", "InvalidRequestError"]
