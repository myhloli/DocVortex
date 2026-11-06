"""保存渲染节点的物理来源，随渲染计划合并而独立于语义协议。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from ....schema import BBox, BlockBase


@dataclass(frozen=True, slots=True)
class SourceRef:
    """使用原始页、顶层索引和子路径唯一标识一个物理区域。"""

    page_idx: int
    block_index: int
    child_path: tuple[int, ...]
    bbox: BBox

    @property
    def key(self) -> str:
        """生成仅含固定字符的文档内定位标识。"""
        suffix = "".join(f"-c{part}" for part in self.child_path)
        return f"p{self.page_idx}-b{self.block_index}{suffix}"

    def as_dict(self) -> dict[str, object]:
        """生成可安全序列化为 HTML 属性的来源描述。"""
        return asdict(self)


@dataclass(slots=True)
class SourceNode:
    """与当前计划中的块树平行保存原始来源，避免复制块时丢失身份。"""

    refs: list[SourceRef] = field(default_factory=list)
    children: list[SourceNode] = field(default_factory=list)

    @classmethod
    def capture(cls, block: BlockBase, page_idx: int, block_index: int, path: tuple[int, ...] = ()) -> SourceNode:
        """在任何合并之前按原始路径捕获几何信息。"""
        content = getattr(block, "content", None)
        children = [item for item in content if isinstance(item, BlockBase)] if isinstance(content, list) else []
        return cls(
            refs=[SourceRef(page_idx, block_index, path, block.bbox)] if block.bbox is not None else [],
            children=[cls.capture(child, page_idx, block_index, (*path, position)) for position, child in enumerate(children)],
        )

    def bind(self, block: BlockBase, mapping: dict[int, list[SourceRef]]) -> None:
        """把完成合并的平行树绑定到本次渲染对象，绑定仅在调用内存活。"""
        mapping[id(block)] = self.refs
        content = getattr(block, "content", None)
        children = [item for item in content if isinstance(item, BlockBase)] if isinstance(content, list) else []
        for child, source in zip(children, self.children, strict=True):
            source.bind(child, mapping)


__all__ = ["SourceNode", "SourceRef"]
