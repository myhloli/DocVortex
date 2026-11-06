"""保留原生结构块的成员和边界，并在最终对象化前生成来源关联。"""

from __future__ import annotations

from copy import deepcopy

STRUCTURE_KEY = "_pdf_structure"


def finalize_structure_block(source: dict, index: int) -> dict:
    """把结构装配器的 raw 类别转成现有 Middle block，递归保留已确认成员。"""
    block = deepcopy(source)
    block["index"] = index
    kind = block["type"]
    if kind in {"list", "index"}:
        block["content"] = [finalize_structure_block(child, child_index) for child_index, child in enumerate(block["content"])]
        if kind == "list":
            block["sub_type"] = "text"
    elif kind in {"image", "table", "code"}:
        body = {
            key: block[key] for key in ("bbox", "index", "content", "image_base64", "image_path", "image_url") if key in block
        }
        body["type"] = kind + "_body"
        block["content"] = [body]
        for key in ("image_base64", "image_path", "image_url"):
            block.pop(key, None)
        if kind == "code":
            block["sub_type"] = "code"
            block.setdefault("guess_lang", "txt")
    return block


def process_structure_blocks(page_model_list: list[dict], *, use_bbox: bool) -> list[dict]:
    """分别处理可信结构与规则块，并将规则结果索引映回原始页面序列。"""
    from .page_blocks import process_page_blocks

    ordinary = [block for block in page_model_list if STRUCTURE_KEY not in block]
    positions = [index for index, block in enumerate(page_model_list) if STRUCTURE_KEY not in block]
    output = process_page_blocks(ordinary, use_bbox=use_bbox) if ordinary else []

    def remap(block):
        """把视觉组及子块的局部索引一并映射，保持父块与 body 的契约。"""
        index = block.get("index")
        if type(index) is int and 0 <= index < len(positions):
            block["index"] = positions[index]
        for child in block.get("content", []):
            if isinstance(child, dict) and child.get("type") not in {"text", "equation_inline", "hyperlink", "code_inline"}:
                remap(child)

    for block in output:
        remap(block)
    trusted = [finalize_structure_block(block, index) for index, block in enumerate(page_model_list) if STRUCTURE_KEY in block]
    visuals = {block[STRUCTURE_KEY]["node_id"]: block for block in trusted if block["type"] in {"image", "table", "code"}}
    for block in trusted:
        parent = visuals.get(block[STRUCTURE_KEY].get("visual_parent"))
        if parent is not None:
            parent["content"].append(block)
        else:
            if block[STRUCTURE_KEY].get("visual_parent"):
                block["type"] = "text"
            output.append(block)
    return output


def finish_structure_pages(pages: list[dict], extension: dict | None) -> None:
    """按源节点身份标记跨页续接，并记录后处理完成后的块路径。"""
    previous: dict[tuple[str, str], tuple[int, dict]] = {}
    links: dict[str, list[dict]] = {}

    def visit(block, page_index, path):
        """递归关联列表和图表成员，跨页续接仅接受相同类别与相同源节点。"""
        marker = block.get(STRUCTURE_KEY)
        if marker:
            identity = marker["node_id"]
            links.setdefault(identity, []).append({"page_idx": page_index, "block_path": path})
            kind = block["type"]
            if kind in {"text", "ref_text", "list", "table"}:
                block.pop("continues_prev", None)
                if len(path) == 1:
                    key = marker.get("logical_id", identity), kind
                    prior = previous.get(key)
                    if prior is not None and prior[0] + 1 == page_index:
                        block["continues_prev"] = True
                    previous[key] = page_index, block
        content = block.get("content")
        if isinstance(content, list):
            for index, child in enumerate(content):
                if isinstance(child, dict) and isinstance(child.get("content"), (str, list)):
                    visit(child, page_index, [*path, index])

    for page in pages:
        for index, block in enumerate(page["blocks"]):
            visit(block, page["page_idx"], [index])
    if extension is not None:
        extension["blocks"] = links


__all__ = []
