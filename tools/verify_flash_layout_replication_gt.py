"""验证独立复制集及其局部表格断言，使用源 SHA256 解析可选的本地原件。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, PositiveInt, model_validator

if __package__:
    from .verify_flash_layout_gt import (
        Digest,
        Gold,
        Page,
        Record,
        Region,
        Relation,
        ScoreField,
        Unit,
        locate_sources,
        read_json,
    )
else:
    from verify_flash_layout_gt import Digest, Gold, Page, Record, Region, Relation, ScoreField, Unit, locate_sources, read_json

DEFAULT_GT = Path(__file__).resolve().parents[1] / "tests/fixtures/flash_layout_gt_r2_replication_v1.json.gz"
BBox = tuple[Unit, Unit, Unit, Unit]
Index = Annotated[int, Field(ge=0)]


class PartialTableDimensions(Record):
    rows: PositiveInt | None = None
    columns: PositiveInt | None = None

    @model_validator(mode="after")
    def check_dimensions(self) -> Self:
        """至少登记一个从源页读出的维度，允许只核验行数或列数。"""
        if self.rows is None and self.columns is None:
            raise ValueError("表格至少需要一个已核验维度")
        return self


class CellAssertion(Record):
    row_0based: Index | None = None
    row_from_end: PositiveInt | None = None
    column_0based: Index
    text: str
    rowspan: PositiveInt | None = None
    colspan: PositiveInt | None = None

    @model_validator(mode="after")
    def check_empty_cell_position(self) -> Self:
        """可用空字符串标记已核对的空格，必须指定行列以区别缺格和任意其他空白格。"""
        if self.row_from_end is not None and self.row_0based is not None:
            raise ValueError("单元格只能使用一种行定位方式")
        if self.row_from_end is not None and (self.rowspan or 1) > self.row_from_end:
            raise ValueError("倒数定位的单元格跨度超出表尾")
        positioned = self.row_0based is not None or self.row_from_end is not None
        if not self.text.strip() and (self.text != "" or not positioned):
            raise ValueError("空格断言需使用空字符串并指定实际行列")
        if (self.rowspan is None) != (self.colspan is None):
            raise ValueError("单元格跨度需要同时指定 rowspan 与 colspan")
        if self.rowspan is not None and not positioned:
            raise ValueError("跨度断言需给出单元格左上角的行列")
        return self


class RowMember(Record):
    """只指定同一逻辑行中的一个列值，用于表内分节标题有多种合法计行方式的原件。"""

    column_0based: Index
    text: str


class ReplicationRegion(Region):
    # 独立公开标注或原件人工框可以直接作为来源，保留原记录编号。
    teacher: Literal["pp", "vlm", "source"]
    scored_fields: list[ScoreField | Literal["cell_assertions", "row_assertions"]]
    table_dimensions: PartialTableDimensions | None = None
    cell_assertions: list[CellAssertion] = Field(default_factory=list)
    row_assertions: list[Annotated[list[RowMember], Field(min_length=2)]] = Field(default_factory=list)
    accepted_alternative_bboxes: list[BBox] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_local_assertions(self) -> Self:
        """校验候选可接受边界以及逐格断言在已知表格维度内的位置。"""
        if self.accepted_alternative_bboxes and "bbox" not in self.scored_fields:
            raise ValueError("等价区域几何需要 bbox 掩码")
        for x0, y0, x1, y1 in self.accepted_alternative_bboxes:
            if x0 >= x1 or y0 >= y1:
                raise ValueError(f"区域 {self.id} 存在无效等价框")
        if bool(self.cell_assertions) != ("cell_assertions" in self.scored_fields):
            raise ValueError("单元格断言与评分掩码不一致")
        if bool(self.row_assertions) != ("row_assertions" in self.scored_fields):
            raise ValueError("同行断言与评分掩码不一致")
        if (self.row_assertions or self.cell_assertions) and self.type != "table":
            raise ValueError("只对表体评分单元格")
        for members in self.row_assertions:
            columns = {member.column_0based for member in members}
            if len(columns) != len(members):
                raise ValueError("同行断言中的逻辑列必须互不相同")
            if (
                self.table_dimensions
                and self.table_dimensions.columns is not None
                and max(columns) >= self.table_dimensions.columns
            ):
                raise ValueError(f"区域 {self.id} 的同行断言列越界")
        for cell in self.cell_assertions:
            shape = self.table_dimensions
            if shape and shape.columns is not None and cell.column_0based + (cell.colspan or 1) > shape.columns:
                raise ValueError(f"区域 {self.id} 的单元格列越界")
            if shape and shape.rows is not None and cell.row_from_end is not None and cell.row_from_end > shape.rows:
                raise ValueError(f"区域 {self.id} 的倒数行越界")
            if (
                shape
                and shape.rows is not None
                and cell.row_0based is not None
                and cell.row_0based + (cell.rowspan or 1) > shape.rows
            ):
                raise ValueError(f"区域 {self.id} 的单元格行越界")
        return self


class ReplicationRelation(Relation):
    kind: Literal["before", "caption_of", "footnote_of", "outside_bodies", "distinct_objects"]


class ForbiddenTableCombination(Record):
    covered_regions: list[str] = Field(min_length=2)
    min_coverage_per_region: Annotated[float, Field(gt=0, le=1)]


class ReplicationPage(Page):
    # 出版数据集给出的原件路径，可用于从公共档案中按成员名取回同一页。
    source_archive_member: str | None = None
    split: Literal["replication"]
    stratum: Literal["grid", "lightly_ruled", "tinted", "negative"]
    domain: str = Field(min_length=1)
    regions: list[ReplicationRegion]
    relations: list[ReplicationRelation]
    protected_non_tables: list[str] = Field(default_factory=list)
    forbidden_table_combinations: list[ForbiddenTableCombination] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_page(self) -> Self:
        """复用原有页级验证，并核验复制集的独立对象、负例和禁止合并区域。"""
        shared = [r for r in self.relations if r.kind != "distinct_objects"]
        Page.check_page(self.model_copy(update={"relations": shared}))
        items = {r.id: r for r in self.regions}
        for rel in self.relations:
            if rel.kind == "distinct_objects" and (
                rel.pairs
                or rel.bodies
                or len(rel.regions) < 2
                or len(set(rel.regions)) != len(rel.regions)
                or not set(rel.regions) <= items.keys()
            ):
                raise ValueError("distinct_objects 需要至少两个独立、有效的同页区域")
        if len(set(self.protected_non_tables)) != len(self.protected_non_tables) or any(
            rid not in items or items[rid].type == "table" for rid in self.protected_non_tables
        ):
            raise ValueError("受保护的非表区域重复、缺失或与表体冲突")
        for guard in self.forbidden_table_combinations:
            if len(set(guard.covered_regions)) != len(guard.covered_regions) or not set(guard.covered_regions) <= items.keys():
                raise ValueError("禁止整体归表的区域重复或缺失")
        return self


class ReplicationGold(Gold):
    schema_name: Literal["flash_layout_gt_replication"] = Field(alias="schema")
    manifest_sha256: Digest
    pp_weights_sha256: Digest | None = None
    vlm_weights_sha256: Digest | None = None
    vlm_projector_sha256: Digest | None = None
    pages: list[ReplicationPage] = Field(min_length=1)

    @model_validator(mode="after")
    def check_teacher_weights(self) -> Self:
        """只要求实际提供区域或类别标注的模型权重，原件直标以标注与原件哈希追溯。"""
        teachers = {
            teacher
            for page in self.pages
            for region in page.regions
            for teacher in (region.teacher, region.type_teacher)
        }
        if "pp" in teachers and self.pp_weights_sha256 is None:
            raise ValueError("PP 标注缺少模型权重哈希")
        if (self.vlm_weights_sha256 is None) != (self.vlm_projector_sha256 is None):
            raise ValueError("VLM 权重和投影权重哈希需同时提供")
        if "vlm" in teachers and self.vlm_weights_sha256 is None:
            raise ValueError("VLM 标注缺少模型权重哈希")
        return self


def main() -> None:
    """独立验证补充夹具；有本地原件时核对 SHA 并导出可重放的补充索引。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gt", type=Path, default=DEFAULT_GT)
    parser.add_argument("--manifest", type=Path, action="append", default=[])
    parser.add_argument("--source-root", type=Path, action="append", default=[])
    parser.add_argument("--export-index", type=Path)
    args = parser.parse_args()
    blob = read_json(args.gt)
    gold = ReplicationGold.model_validate_json(blob)
    paths = locate_sources(gold.pages, args.manifest, args.source_root) if args.manifest or args.source_root else {}
    if args.export_index:
        if not paths:
            parser.error("--export-index 需要先以 --manifest 或 --source-root 校验原件")
        if args.export_index.resolve() in {args.gt.resolve(), *(f.resolve() for f in args.manifest), *paths.values()}:
            parser.error("本地索引输出路径与输入重合")
        result = json.loads(blob)
        for page in result["pages"]:
            page["pdf_path"] = str(paths[page["pdf_sha256"]])
        args.export_index.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    regions = [r for p in gold.pages for r in p.regions]
    print(
        f"独立复制集 {gold.version}，{len(gold.pages)} 页，{len({p.family_id for p in gold.pages})} 个家族，"
        f"{sum(r.type == 'table' for r in regions)} 个表区，{sum(len(r.cell_assertions) for r in regions)} 个单元格断言，"
        f"其中 {sum(c.rowspan is not None for r in regions for c in r.cell_assertions)} 个要求精确跨度，"
        f"{sum(len(r.row_assertions) for r in regions)} 个同行断言，"
        f"已校验 {len(paths)} 份原件"
    )


if __name__ == "__main__":
    main()
