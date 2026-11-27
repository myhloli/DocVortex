"""验证可移植的 Flash PDF 布局 GT，并按原件 SHA256 生成本地重放索引。"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from graphlib import TopologicalSorter
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, model_validator

from docvortex.schema import BlockType

DEFAULT_GT = Path(__file__).resolve().parents[1] / "tests/fixtures/flash_layout_gt_v1_1.json.gz"
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Unit = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
ScoreField = Literal["bbox", "type", "content_anchors", "table_dimensions"]


class Record(BaseModel):
    """只接受明确声明的 GT 字段，避免打包本机路径与原件。"""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)


class ContentAnchor(Record):
    text: str = Field(min_length=1)


class TableDimensions(Record):
    rows: PositiveInt
    columns: PositiveInt


class Region(Record):
    id: str = Field(min_length=1)
    type: str | None
    bbox: tuple[Unit, Unit, Unit, Unit]
    scored_fields: list[ScoreField]
    content_anchors: list[ContentAnchor] = Field(default_factory=list)
    table_dimensions: TableDimensions | None = None
    teacher: Literal["pp", "vlm"]
    source_index: int = Field(ge=0)
    source_type: str
    type_teacher: str | None = None
    type_source_index: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def check_region(self) -> Self:
        """检验非退化框、教师序号和计分维度与对应真值的一致性。"""
        left, top, right, bottom = self.bbox
        if left >= right or top >= bottom:
            raise ValueError(f"区域 {self.id} 的 bbox 面积为零或为负")
        if self.teacher == "pp" and self.source_index == 0:
            raise ValueError("PP 的最终顺序从 1 开始")
        if len(set(self.scored_fields)) != len(self.scored_fields) or ("type" in self.scored_fields and not self.type):
            raise ValueError("计分字段重复或缺少已标类型")
        if ("content_anchors" in self.scored_fields) != bool(self.content_anchors):
            raise ValueError("内容锚点与计分掩码不符")
        if ("table_dimensions" in self.scored_fields) != bool(self.table_dimensions):
            raise ValueError("表格尺寸与计分掩码不符")
        if (self.type_teacher is None) != (self.type_source_index is None):
            raise ValueError("细分类复核教师和来源序号需配对")
        if self.table_dimensions and self.type != "table":
            raise ValueError("只有表格可声明行列数")
        return self


class Relation(Record):
    kind: Literal["before", "caption_of", "footnote_of", "outside_bodies"]
    pairs: list[tuple[str, str]] = Field(default_factory=list)
    regions: list[str] = Field(default_factory=list)
    bodies: list[str] = Field(default_factory=list)


class Page(Record):
    document_id: str
    pdf_sha256: Digest
    source_bytes: PositiveInt
    page_count: PositiveInt
    physical_page: PositiveInt
    family_id: str = Field(min_length=1)
    split: Literal["dev", "validation", "test"]
    cohort: str = Field(min_length=1)
    render_sha256: Digest
    size_px: tuple[PositiveInt, PositiveInt]
    full_page_review: bool
    exhaustive_types: list[str]
    visual_table_count: int | None = Field(default=None, ge=0)
    blank_verified: bool = False
    regions: list[Region]
    relations: list[Relation]

    @model_validator(mode="after")
    def check_page(self) -> Self:
        """校验物理页、空页、引用关系、父子类型和阅读顺序的无环性。"""
        for label in set(self.exhaustive_types) | {r.type for r in self.regions if r.type is not None}:
            BlockType(label)
        if self.physical_page > self.page_count or self.document_id != f"d-{self.pdf_sha256[:16]}":
            raise ValueError("页码或按 SHA256 派生的文档编号不符")
        if self.blank_verified and self.regions:
            raise ValueError("已核实空白页含有区域")
        if self.visual_table_count is not None and self.visual_table_count != sum(r.type == "table" for r in self.regions):
            raise ValueError("整页复核的表格数与已标区域不符")
        items = {r.id: r for r in self.regions}
        if len(items) != len(self.regions):
            raise ValueError("区域编号重复")
        order = TopologicalSorter()
        for rel in self.relations:
            if rel.kind == "outside_bodies":
                if not rel.bodies or not rel.regions or rel.pairs or set(rel.bodies) & set(rel.regions):
                    raise ValueError("排除归属关系缺少主体或成员不互斥")
                if not set(rel.bodies + rel.regions) <= items.keys():
                    raise ValueError("排除归属引用了未知区域")
                continue
            if not rel.pairs or rel.bodies or rel.regions:
                raise ValueError("非空关系应使用 pairs")
            for child, parent in rel.pairs:
                if child == parent or child not in items or parent not in items:
                    raise ValueError("关系包含自引用或未知区域")
                if rel.kind == "before":
                    order.add(parent, child)
                else:
                    role = rel.kind.removesuffix("_of")
                    if items[child].type != f"{items[parent].type}_{role}":
                        raise ValueError("附注或标题的类型与父块不一致")
        tuple(order.static_order())
        return self


class Gold(Record):
    schema_name: Literal["flash_layout_gt"] = Field(alias="schema")
    version: str
    annotation_sha256: Digest
    pp_weights_sha256: Digest
    vlm_weights_sha256: Digest
    vlm_projector_sha256: Digest
    pages: list[Page] = Field(min_length=1)

    @model_validator(mode="after")
    def check_families(self) -> Self:
        """验证同一原件身份稳定、每个物理页唯一、每个文档家族只进入一折。"""
        families = {}
        documents = {}
        pages = set()
        for page in self.pages:
            if families.setdefault(page.family_id, page.split) != page.split:
                raise ValueError(f"文档家族跨折 {page.family_id}")
            identity = (page.pdf_sha256, page.family_id, page.source_bytes, page.page_count)
            if documents.setdefault(page.document_id, identity) != identity:
                raise ValueError(f"文档身份冲突 {page.document_id}")
            key = (page.document_id, page.physical_page)
            if key in pages:
                raise ValueError(f"文档与物理页重复 {key}")
            pages.add(key)
        return self


def read_json(path: Path) -> bytes:
    """同时支持明文 JSON 和可复现压缩的仓库夹具。"""
    return gzip.decompress(path.read_bytes()) if path.suffix == ".gz" else path.read_bytes()


def locate_sources(pages: list[Page], manifests: list[Path], roots: list[Path]) -> dict[str, Path]:
    """只采信匹配的文件内容哈希，清单和目录都可以给出重命名后的原件。"""
    required = {p.pdf_sha256 for p in pages}
    sizes = {p.source_bytes for p in pages}
    paths: dict[str, Path] = {}
    seen: set[Path] = set()

    def check(candidate: Path) -> None:
        """按大小预筛，再读实际文件校验原始 SHA256。"""
        path = candidate.resolve()
        if path in seen or not path.is_file() or path.stat().st_size not in sizes:
            return
        seen.add(path)
        with path.open("rb") as stream:
            sha = hashlib.file_digest(stream, "sha256").hexdigest()
        if sha in required:
            paths.setdefault(sha, path)

    for manifest in manifests:
        data = json.loads(read_json(manifest))
        entries = data.get("documents", data.get("pages"))
        if not isinstance(entries, list):
            raise ValueError(f"源清单需要 documents 或 pages 列表 {manifest}")
        for row in entries:
            if row.get("pdf_sha256", row.get("sha256")) in required:
                check(manifest.parent / (row.get("pdf_path") or row["path"]))
    for root in roots:
        if not root.is_dir():
            raise ValueError(f"原件目录不存在 {root}")
        for candidate in root.rglob("*"):
            if len(paths) == len(required):
                break
            if candidate.suffix.lower() == ".pdf":
                check(candidate)
    missing = sorted(required - paths.keys())
    if missing:
        raise ValueError(f"有 {len(missing)} 份源文件未通过 SHA256 匹配，例 {missing[0]}")
    return paths


def main() -> None:
    """验证新版金标；指定原件后可输出含实际 PDF 路径的本机索引。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gt", type=Path, default=DEFAULT_GT, help="待验证的 JSON 或 JSON.GZ")
    parser.add_argument("--manifest", type=Path, action="append", default=[], help="含原件路径和 SHA256 的清单，可重复")
    parser.add_argument("--source-root", type=Path, action="append", default=[], help="按内容匹配 PDF 的目录，可重复")
    parser.add_argument("--export-index", type=Path, help="写入含完整区域、关系、本地原件路径的 JSON")
    args = parser.parse_args()
    packed = read_json(args.gt)
    gold = Gold.model_validate_json(packed)
    paths = locate_sources(gold.pages, args.manifest, args.source_root) if args.manifest or args.source_root else {}
    if args.export_index:
        if not paths:
            parser.error("--export-index 需要 --manifest 或 --source-root 校验原件")
        if args.export_index.resolve() in {args.gt.resolve(), *(p.resolve() for p in args.manifest), *paths.values()}:
            parser.error("本地索引输出路径与输入重合")
        data = json.loads(packed)
        for page in data["pages"]:
            page["pdf_path"] = str(paths[page["pdf_sha256"]])
        args.export_index.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"GT {gold.version}，{len(gold.pages)} 页，{len({p.family_id for p in gold.pages})} 个家族，已校验 {len(paths)} 份原件"
    )


if __name__ == "__main__":
    main()
