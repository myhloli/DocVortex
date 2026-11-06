"""读取 Tagged PDF 的文档级语义树，返回可脱离解析器使用的自有记录。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pypdf.generic import ArrayObject, BooleanObject, DictionaryObject, IndirectObject, NullObject

from ...result import Diagnostic

STRUCTURE_EXTENSION = "docvortex_pdf_structure"
MAX_STRUCTURE_DEPTH = 128
MAX_STRUCTURE_NODES = 100_000


def dereference(value: Any) -> Any:
    """解析间接引用，普通值直接返回。"""
    return value.get_object() if isinstance(value, IndirectObject) else value


def object_identity(value: Any, default: str = "") -> str:
    """以原件对象号和代数标识资源，直接对象使用调用方提供的路径。"""
    reference = value if isinstance(value, IndirectObject) else getattr(value, "indirect_reference", None)
    return f"{reference.idnum}:{reference.generation}" if reference is not None else default


def plain_value(value: Any, depth: int = 0) -> Any:
    """有界复制属性，间接资源只保存身份，避免把整份 PDF 放入扩展。"""
    if depth > 8:
        return None
    if isinstance(value, IndirectObject):
        return {"object": object_identity(value)}
    if isinstance(value, BooleanObject):
        return bool(value)
    if isinstance(value, NullObject):
        return None
    if isinstance(value, DictionaryObject):
        return {str(key).lstrip("/"): plain_value(item, depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain_value(item, depth + 1) for item in value[:4096]]
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, int) and not isinstance(value, bool):
        return int(value)
    if isinstance(value, float):
        return float(value)
    if isinstance(value, (str, bool)) or value is None:
        return value
    return str(value)


@dataclass(frozen=True)
class ContentReference:
    """一个有作用域的标记内容引用或注释对象引用。"""

    page_index: int | None
    stream: str
    mcid: int | None = None
    object_id: str | None = None


@dataclass(frozen=True)
class StructureNode:
    """保留源节点角色、属性和交错排列的内容引用及子节点。"""

    identity: str
    role: str
    raw_role: str
    order: int
    page_index: int | None
    children: tuple[StructureNode | ContentReference, ...]
    language: str | None = None
    alt: str | None = None
    actual_text: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    namespace: str | None = None
    mathml: tuple[bytes, ...] = ()
    heading_depth: int = 1
    valid: bool = True
    references: tuple[str, ...] = ()


@dataclass(frozen=True)
class PDFStructure:
    """文档结构快照及可追溯诊断，全部字段均独立于 PDF 句柄。"""

    roots: tuple[StructureNode, ...]
    diagnostics: tuple[Diagnostic, ...]
    declared_ua: str | None = None

    def nodes(self):
        """按源树先序迭代节点，内容引用的交错顺序仍由 children 保留。"""
        pending = list(reversed(self.roots))
        while pending:
            node = pending.pop()
            yield node
            pending.extend(child for child in reversed(node.children) if isinstance(child, StructureNode))

    def page_membership(self) -> dict[str, frozenset[int]]:
        """按完整原件引用汇总节点页归属，供选页过滤和跨页替代范围检查复用。"""
        membership: dict[str, frozenset[int]] = {}
        for node in reversed(list(self.nodes())):
            pages: set[int] = set()
            for child in node.children:
                if isinstance(child, StructureNode):
                    pages.update(membership.get(child.identity, ()))
                elif child.page_index is not None:
                    pages.add(child.page_index)
            membership[node.identity] = frozenset(pages)
        pending = [(node, frozenset()) for node in self.roots]
        while pending:
            node, inherited = pending.pop()
            if not membership[node.identity]:
                membership[node.identity] = inherited or (
                    frozenset((node.page_index,)) if node.page_index is not None else frozenset()
                )
            pending.extend((child, membership[node.identity]) for child in node.children if isinstance(child, StructureNode))
        return membership


class _StructureReader:
    """在一次文档读取中解析角色映射、属性类和有界结构引用。"""

    def __init__(self, reader, root):
        """建立原页对象索引，供 Pg 及 ParentTree 一致性校验复用。"""
        self.reader = reader
        self.root = root
        self.page_ids = {object_identity(page): index for index, page in enumerate(reader.pages)}
        self.role_map = dereference(root.get("/RoleMap", {}))
        self.class_map = dereference(root.get("/ClassMap", {}))
        if not isinstance(self.class_map, dict):
            self.class_map = {}
        self.seen: set[str] = set()
        self.order = 0
        self.diagnostics: list[Diagnostic] = []
        self.parents: dict[int, Any] = {}
        self._number_tree(root.get("/ParentTree"), set(), 0)
        self.owner_pages: dict[str, set[int]] = {}
        for page_index, page in enumerate(reader.pages):
            parent_key = page.get("/StructParents")
            owners = dereference(self.parents.get(parent_key, []))
            if isinstance(owners, ArrayObject):
                for owner in owners:
                    self.owner_pages.setdefault(object_identity(owner), set()).add(page_index)

    def warn(self, message: str, page_index: int | None = None) -> None:
        """记录受损结构的局部原因，不把可选语义问题升级为文档加载失败。"""
        self.diagnostics.append(Diagnostic("pdf_structure_invalid", message, page_index))

    def _number_tree(self, value, visited: set[str], depth: int) -> None:
        """有界展开 ParentTree 的数字树，拒绝重复引用。"""
        if value is None or depth >= MAX_STRUCTURE_DEPTH:
            return
        key = object_identity(value)
        if key and key in visited:
            self.warn("ParentTree contains a repeated reference")
            return
        if key:
            visited.add(key)
        node = dereference(value)
        if not isinstance(node, DictionaryObject):
            return
        numbers = dereference(node.get("/Nums", []))
        for index in range(0, min(len(numbers) - 1, MAX_STRUCTURE_NODES * 2), 2):
            if isinstance(numbers[index], int):
                self.parents[int(numbers[index])] = numbers[index + 1]
        for child in dereference(node.get("/Kids", [])):
            self._number_tree(child, visited, depth + 1)

    def _role(self, element) -> tuple[str, str | None, bool]:
        """按命名空间映射和全局 RoleMap 解析角色，并识别映射环。"""
        role = str(element.get("/S", "")).lstrip("/")
        namespace = dereference(element.get("/NS"))
        visited: set[tuple[str, str]] = set()
        for _ in range(MAX_STRUCTURE_DEPTH):
            namespace_id = object_identity(namespace, str(namespace.get("/NS", ""))) if isinstance(namespace, dict) else ""
            key = namespace_id, role
            if key in visited:
                return role, str(namespace.get("/NS", "")) if namespace else None, False
            visited.add(key)
            mapping = dereference(namespace.get("/RoleMapNS", {})) if isinstance(namespace, dict) else self.role_map
            target = dereference(mapping.get("/" + role)) if isinstance(mapping, dict) else None
            if target is None:
                return role, str(namespace.get("/NS", "")) if namespace else None, True
            if isinstance(target, ArrayObject) and len(target) == 2:
                role, namespace = str(target[0]).lstrip("/"), dereference(target[1])
            else:
                role = str(target).lstrip("/")
        return role, None, False

    def _attributes(self, element) -> dict[str, Any]:
        """先应用属性类，再以节点直接属性覆盖同属主字段。"""
        result: dict[str, Any] = {}
        classes = dereference(element.get("/C", []))
        classes = classes if isinstance(classes, list) else [classes]
        sources = [self.class_map.get(name) for name in classes if isinstance(name, str)]
        sources.append(element.get("/A"))
        for source in sources:
            source = dereference(source)
            attributes = source if isinstance(source, list) else [source]
            for attribute in attributes:
                attribute = dereference(attribute)
                if isinstance(attribute, DictionaryObject):
                    owner = str(attribute.get("/O", "")).lstrip("/")
                    result.setdefault(owner, {}).update(plain_value(attribute))
        return result

    def _mathml(self, element) -> tuple[bytes, ...]:
        """读取节点内嵌关联文件，限制解码后大小并只接收数学 XML。"""
        output = []
        for reference in dereference(element.get("/AF", [])):
            specification = dereference(reference)
            if not isinstance(specification, dict):
                continue
            embedded = dereference(specification.get("/EF", {}))
            if not isinstance(embedded, dict):
                continue
            stream = dereference(embedded.get("/UF", embedded.get("/F")))
            if stream is None or not hasattr(stream, "get_data"):
                continue
            data = stream.get_data()
            filename = str(specification.get("/UF", specification.get("/F", ""))).lower()
            subtype = str(stream.get("/Subtype", "")).lower()
            if len(data) <= 4 * 1024 * 1024 and (
                filename.endswith((".xml", ".mml", ".mathml")) or "mathml" in subtype or b"math" in data[:4096].lower()
            ):
                output.append(data)
        return tuple(output)

    def _parent_matches(self, page_index, mcid: int, owner: str, stream=None) -> bool:
        """存在反向父树条目时校验所属节点，缺失条目允许由正向结构读取。"""
        if stream is None and page_index is None:
            return False
        container = dereference(stream) if stream is not None else self.reader.pages[page_index]
        if not isinstance(container, DictionaryObject):
            return False
        parents = dereference(self.parents.get(container.get("/StructParents")))
        if not isinstance(parents, ArrayObject) or not 0 <= mcid < len(parents):
            return True
        expected = object_identity(parents[mcid])
        return not expected or expected == owner

    def node(self, value, path: str, page_index=None, language=None, depth=0, heading_depth=0):
        """按 K 的原始顺序读取节点，受损子项产生诊断并保留其他兄弟。"""
        identity = object_identity(value, path)
        if identity in self.seen or depth >= MAX_STRUCTURE_DEPTH or self.order >= MAX_STRUCTURE_NODES:
            self.warn(f"Repeated or excessive structure node {identity}", page_index)
            return None
        self.seen.add(identity)
        element = dereference(value)
        if not isinstance(element, DictionaryObject):
            self.warn(f"Invalid structure node {identity}", page_index)
            return None
        if "/Pg" in element:
            page_index = self.page_ids.get(object_identity(element.raw_get("/Pg")))
        if page_index is None and len(self.owner_pages.get(identity, ())) == 1:
            page_index = next(iter(self.owner_pages[identity]))
        language = str(element["/Lang"]) if "/Lang" in element else language
        role, namespace, valid = self._role(element)
        order = self.order
        self.order += 1
        children = []
        kids = dereference(element.get("/K", []))
        kids = kids if isinstance(kids, list) else [kids]
        for index, child in enumerate(kids):
            resolved = dereference(child)
            kind = resolved.get("/Type") if isinstance(resolved, dict) else None
            if isinstance(resolved, int):
                valid = valid and self._parent_matches(page_index, int(resolved), identity)
                children.append(ContentReference(page_index, f"page:{page_index}", int(resolved)))
            elif kind in {"/MCR", "/OBJR"}:
                child_page = self.page_ids.get(object_identity(resolved.raw_get("/Pg"))) if "/Pg" in resolved else page_index
                stream = object_identity(resolved.get("/Stm"), f"page:{child_page}")
                if kind == "/MCR" and isinstance(resolved.get("/MCID"), int):
                    valid = valid and self._parent_matches(child_page, int(resolved["/MCID"]), identity, resolved.get("/Stm"))
                    children.append(ContentReference(child_page, stream, int(resolved["/MCID"])))
                elif kind == "/OBJR":
                    children.append(ContentReference(child_page, stream, object_id=object_identity(resolved.get("/Obj"))))
                else:
                    valid = False
            elif isinstance(resolved, DictionaryObject):
                nested = self.node(
                    child, f"{identity}/{index}", page_index, language, depth + 1, heading_depth + (role == "Sect")
                )
                if nested is not None:
                    children.append(nested)
                else:
                    valid = False
            else:
                valid = False
        if not valid:
            self.warn(f"Invalid role mapping or child reference in {identity}", page_index)
        references = dereference(element.get("/Ref", []))
        references = references if isinstance(references, list) else [references]
        return StructureNode(
            identity,
            role,
            str(element.get("/S", "")).lstrip("/"),
            order,
            page_index,
            tuple(children),
            language,
            str(element["/Alt"]) if "/Alt" in element else None,
            str(element["/ActualText"]) if "/ActualText" in element else None,
            self._attributes(element),
            namespace,
            self._mathml(element),
            heading_depth,
            valid,
            tuple(object_identity(reference) for reference in references),
        )


def read_pdf_structure(document) -> PDFStructure | None:
    """从共享对象读取器获取结构，普通 PDF 仅检查 Catalog。"""
    reader = document._get_object_reader()
    if reader is None:
        return None
    catalog = dereference(reader.trailer.get("/Root"))
    if not isinstance(catalog, DictionaryObject):
        return None
    root = dereference(catalog.get("/StructTreeRoot"))
    if not isinstance(root, DictionaryObject):
        return None
    parser = _StructureReader(reader, root)
    roots = dereference(root.get("/K", []))
    roots = roots if isinstance(roots, list) else [roots]
    result = []
    language = catalog.get("/Lang")
    for index, source in enumerate(roots):
        try:
            node = parser.node(source, f"root/{index}", language=str(language) if language else None)
            if node is not None:
                result.append(node)
        except (ValueError, TypeError, KeyError, RecursionError) as error:
            parser.warn(f"Cannot read structure root {index}: {error}")
    declared = None
    try:
        xmp = reader.xmp_metadata
        if xmp is not None:
            from lxml import etree

            metadata = etree.fromstring(xmp.stream.get_data(), etree.XMLParser(resolve_entities=False, no_network=True))
            values = metadata.xpath("//*[local-name()='part' and namespace-uri()='http://www.aiim.org/pdfua/ns/id/']/text()")
            values += metadata.xpath("//@*[local-name()='part' and namespace-uri()='http://www.aiim.org/pdfua/ns/id/']")
            declared = str(values[0]) if values else None
    except (ValueError, TypeError, KeyError):
        pass
    return PDFStructure(tuple(result), tuple(parser.diagnostics), declared)
