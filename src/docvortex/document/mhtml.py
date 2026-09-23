"""受限 MIME 归档、根文档选择及分作用域的资源索引。"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass, field
from email import policy
from email.message import Message
from email.parser import BytesParser
from urllib.parse import unquote, urldefrag, urljoin, urlsplit

from docvortex.document.contracts import HtmlSourceContext
from docvortex.errors import DocumentError
from docvortex.result import Diagnostic

MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_DECODED_BYTES = 256 * 1024 * 1024
MAX_PARTS = 4096
MAX_DEPTH = 32
HTML_MEDIA_TYPES = frozenset({"text/html", "application/xhtml+xml"})


class MhtmlParseError(DocumentError):
    """表示归档结构、根文档或 MIME 编码无效。"""

    def __init__(self, message: str) -> None:
        """提供可供 CLI 和上层调用方识别的稳定错误码。"""
        super().__init__("mhtml_invalid", message)


class MhtmlResourceLimitError(DocumentError):
    """表示 MIME 容器或累计解码内容超出预算。"""

    def __init__(self, message: str) -> None:
        """把归档预算错误映射为统一资源限制错误码。"""
        super().__init__("resource_limit", message)


def resolve_uri(base: str, reference: str) -> str:
    """按基址解析引用，保留查询参数、片段和百分号编码以区分归档子页面。"""
    # 使用虚拟 HTTP 基址处理没有原站地址的归档；此地址仅用于内部索引。
    return urljoin(base or "https://mhtml.invalid/", reference.strip())


def content_id(value: str) -> str:
    """规范 MIME Content-ID 的外围空白和尖括号，保留标识符大小写。"""
    return value.strip().removeprefix("<").removesuffix(">")


@dataclass(eq=False)
class ResourceScope:
    """限制引用只查询当前 related 容器及其外层，避免串用 iframe 资源。"""

    parent: ResourceScope | None = None
    locations: dict[str, ArchivePart] = field(default_factory=dict)
    ids: dict[str, ArchivePart] = field(default_factory=dict)


@dataclass(eq=False)
class ArchivePart:
    """保存部件头信息和按需解码缓存。"""

    message: Message
    scope: ResourceScope
    location: str
    payload: bytes | None = None
    error: str | None = None

    @property
    def media_type(self) -> str:
        """返回资源的 MIME 类型，调用方无需访问邮件对象。"""
        return self.message.get_content_type()

    @property
    def charset(self) -> str | None:
        """返回资源声明的字符集，缺失时交由调用方按用途推断。"""
        return self.message.get_content_charset()

    @property
    def source_uri(self) -> str:
        """返回已按容器基址解析的资源地址。"""
        return self.location


class MhtmlArchive:
    """持有根 HTML 和只读 MIME 资源，不创建临时文件或请求网络。"""

    def __init__(self, data: bytes, source_context: HtmlSourceContext | None = None) -> None:
        """校验容器、选择根文档并建立轻量资源索引。"""
        if len(data) > MAX_ARCHIVE_BYTES:
            raise MhtmlResourceLimitError(f"MHTML archive exceeds {MAX_ARCHIVE_BYTES} bytes")
        try:
            self.message = BytesParser(policy=policy.default).parsebytes(data)
        except (ValueError, RecursionError) as exc:
            raise MhtmlParseError(f"Cannot parse MHTML container: {exc}") from exc
        if self.message.get_content_type() != "multipart/related":
            raise MhtmlParseError("MHTML requires a multipart/related container")
        self.diagnostics: list[Diagnostic] = []
        self._reported: set[tuple[str, str]] = set()
        self._decoded_bytes = 0
        self._parts: dict[int, ArchivePart] = {}
        self._parents: dict[int, Message] = {}
        self._validate_tree()
        root = self._select_root(self.message)
        self._root_message = root
        context = source_context or HtmlSourceContext()
        fallback = str(self.message.get("Snapshot-Content-Location", "")) or context.source_uri or ""
        ancestors = [root]
        while id(ancestors[-1]) in self._parents:
            ancestors.append(self._parents[id(ancestors[-1])])
        source_uri = fallback
        try:
            for ancestor in reversed(ancestors):
                if location := str(ancestor.get("Content-Location", "")):
                    source_uri = urljoin(source_uri, location)
        except ValueError as exc:
            raise MhtmlParseError(f"Invalid MHTML source URI: {exc}") from exc
        self.source_context = HtmlSourceContext(
            source_uri=source_uri or None,
            transport_encoding=root.get_content_charset() or context.transport_encoding,
        )
        self._index(self.message, ResourceScope(), fallback, root=True)
        self.root = self._parts[id(root)]
        self.html = self.decode(self.root)
        self.subject = str(self.message.get("Subject", "")).strip() or None

    def _validate_tree(self) -> None:
        """迭代校验部件数量、深度和边界缺陷，不展开任何资源内容。"""
        stack = [(self.message, 1)]
        count = 0
        while stack:
            message, depth = stack.pop()
            count += 1
            if count > MAX_PARTS or depth > MAX_DEPTH:
                raise MhtmlResourceLimitError("MHTML part count or nesting depth exceeds limit")
            if message.get_content_maintype() == "multipart":
                if not message.is_multipart() or message.defects:
                    raise MhtmlParseError("Malformed MHTML multipart boundary")
                for child in message.get_payload():
                    self._parents[id(child)] = message
                    stack.append((child, depth + 1))
            elif message.is_multipart():
                raise MhtmlParseError("Nested email messages are not MHTML resources")

    def _select_root(self, message: Message) -> Message:
        """遵循 related 的 start/首部件规则和 alternative 的最后可用 HTML 规则。"""
        media = message.get_content_type()
        if media in HTML_MEDIA_TYPES:
            return message
        children = message.get_payload() if message.is_multipart() else []
        if media == "multipart/related" and children:
            start = message.get_param("start")
            target = children[0]
            if start is not None:
                matches = [child for child in children if content_id(str(child.get("Content-ID", ""))) == content_id(start)]
                if len(matches) != 1:
                    raise MhtmlParseError("MHTML start must identify exactly one root part")
                target = matches[0]
            return self._select_root(target)
        if media == "multipart/alternative":
            for child in reversed(children):
                if child.get_content_type() in HTML_MEDIA_TYPES | {"multipart/related", "multipart/alternative"}:
                    return self._select_root(child)
        raise MhtmlParseError("MHTML root does not contain a supported HTML document")

    def _index(self, message: Message, scope: ResourceScope, base: str, *, root: bool = False) -> None:
        """建立按 related 容器隔离的地址索引，并拒绝同一作用域中的歧义标识。"""
        location = str(message.get("Content-Location", ""))
        try:
            uri = urljoin(base, location) if location else base
        except ValueError:
            self.report("mhtml_resource_invalid", location)
            uri = base
            location = ""
        if message is self._root_message:
            uri = self.source_context.source_uri or ""
        if message.is_multipart():
            if message.get_content_type() == "multipart/related":
                scope = ResourceScope(None if root else scope)
            for child in message.get_payload():
                self._index(child, scope, uri)
            return
        # 没有容器基址时，相对部件地址相对于主文档；不要把主文档的相对路径再次拼接。
        if location and message is not self._root_message:
            uri = resolve_uri(base or self.source_context.source_uri or "", location)
        elif location:
            uri = resolve_uri("", uri)
        part = ArchivePart(message, scope, uri)
        self._parts[id(message)] = part
        cid = content_id(str(message.get("Content-ID", "")))
        for index, key in ((scope.locations, uri if location else ""), (scope.ids, cid)):
            if key:
                if key in index:
                    raise MhtmlParseError(f"Duplicate MHTML resource identifier: {key}")
                index[key] = part

    def find(self, reference: str, *, base_href: str | None = None) -> ArchivePart | None:
        """在根文档可见的作用域内解析 CID 或 Content-Location 引用。"""
        reference = reference.strip()
        try:
            is_cid = urlsplit(reference).scheme.casefold() == "cid"
            base = resolve_uri(self.source_context.source_uri or "", base_href) if base_href else self.source_context.source_uri
            key = content_id(unquote(reference[4:])) if is_cid else resolve_uri(base or "", reference)
        except ValueError:
            return None
        scope: ResourceScope | None = self.root.scope
        while scope is not None:
            index = scope.ids if is_cid else scope.locations
            part = index.get(key)
            if part is None and is_cid:
                # Blink 用 Content-Location: cid:... 保存内联 CSS，标准 Content-ID 优先。
                part = scope.locations.get(reference)
            if part is None and not is_cid:
                part = index.get(urldefrag(key)[0])
            if part is not None:
                return part
            scope = scope.parent
        return None

    def decode(self, part: ArchivePart) -> bytes:
        """按需严格解码资源并累计一次字节预算，缓存成功结果和失败原因。"""
        if part.error is not None:
            raise MhtmlParseError(part.error)
        if part.payload is not None:
            return part.payload
        encoding = str(part.message.get("Content-Transfer-Encoding", "7bit")).strip().casefold()
        try:
            if encoding not in {"base64", "quoted-printable", "7bit", "8bit", "binary"}:
                raise ValueError(f"Unsupported transfer encoding: {encoding}")
            if encoding == "base64":
                encoded = part.message.get_payload().encode("ascii")
                payload = base64.b64decode(re.sub(rb"[\t\r\n ]", b"", encoded), validate=True)
            else:
                if encoding == "quoted-printable" and re.search(r"=(?![0-9A-Fa-f]{2}|\r?\n)", part.message.get_payload()):
                    raise ValueError("Invalid quoted-printable escape")
                payload = part.message.get_payload(decode=True)
            if not isinstance(payload, bytes) or part.message.defects:
                raise ValueError("Malformed MIME part")
        except (ValueError, UnicodeError, binascii.Error) as exc:
            part.error = f"Cannot decode MHTML part: {exc}"
            raise MhtmlParseError(part.error) from exc
        self._decoded_bytes += len(payload)
        if self._decoded_bytes > MAX_DECODED_BYTES:
            raise MhtmlResourceLimitError(f"MHTML decoded data exceeds {MAX_DECODED_BYTES} bytes")
        part.payload = payload
        return payload

    def report(self, code: str, reference: str) -> None:
        """同一资源问题只报告一次，避免重复图片引用刷满诊断。"""
        key = (code, reference)
        if key not in self._reported:
            self._reported.add(key)
            self.diagnostics.append(Diagnostic(code, f"MHTML resource: {reference}"))


__all__ = ["ArchivePart", "MhtmlArchive", "MhtmlParseError", "MhtmlResourceLimitError"]
