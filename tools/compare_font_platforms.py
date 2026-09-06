"""比较固定 PDFium/font 的平台产物，严格验证 CJK 几何及有序语义。"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

_TOLERANCE = 1e-3


def _read(path: Path) -> Any:
    """以明确编码读取不依赖当前平台区域设置的产物。"""
    return json.loads(path.read_text(encoding="utf-8"))


def compare_geometry(reference: dict[str, Any], candidate: dict[str, Any], font_hash: str) -> dict[str, Any]:
    """所有字符索引/Unicode 必须一致；被接管字形的 loose/tight/origin 使用 PDF point 容差。"""
    assert reference["source_sha256"] == candidate["source_sha256"], "Source PDF differs"
    assert len(reference["pages"]) == len(candidate["pages"]), "Page count differs"
    count = 0
    maximum = 0.0
    other_changes = 0
    for page_index, (before_page, after_page) in enumerate(zip(reference["pages"], candidate["pages"])):
        assert before_page["size"] == after_page["size"] and before_page["rotation"] == after_page["rotation"]
        assert len(before_page["chars"]) == len(after_page["chars"]), (page_index, "Char count differs")
        for before, after in zip(before_page["chars"], after_page["chars"]):
            assert (before["index"], before["unicode"]) == (after["index"], after["unicode"]), (page_index, before["index"])
            source_font = reference["fonts"][before["font"]]
            target_font = candidate["fonts"][after["font"]]
            selected = source_font["data_sha256"] == font_hash
            assert selected == (target_font["data_sha256"] == font_hash), "Font policy differs"
            if not selected:
                other_changes += any(before[field] != after[field] for field in ("loose", "tight", "origin"))
                continue
            assert source_font["embedded"] == target_font["embedded"] == 0
            count += 1
            for field in ("loose", "tight", "origin"):
                left, right = before[field], after[field]
                assert (left is None) == (right is None), (page_index, before["index"], field)
                if left is None:
                    continue
                assert len(left) == len(right)
                difference = max(abs(a - b) for a, b in zip(left, right))
                assert math.isfinite(difference) and difference <= _TOLERANCE, (page_index, before["index"], field, difference)
                maximum = max(maximum, difference)
    assert count > 0, "The fixture did not exercise the bundled CJK font"
    return {"cjk_chars": count, "max_cjk_delta_points": maximum, "other_font_geometry_changes": other_changes}


def _semantics(model: dict[str, Any]) -> list[list[tuple[Any, ...]]]:
    """按原阅读顺序比较块类型、文本/表格结构及角度，素材像素单独供视觉审阅。"""
    return [[(block["type"], block.get("content"), block.get("angle")) for block in page] for page in model["pages"]]


def main() -> None:
    """以一个平台为参考，检查全部其他平台并保存可审计的最大几何差。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directories", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    assert len(args.directories) >= 2
    reference = args.directories[0]
    metadata = _read(reference / "metadata.json")
    report = {"runtime": metadata["runtime"], "tolerance_points": _TOLERANCE, "comparisons": []}
    for candidate in args.directories[1:]:
        target = _read(candidate / "metadata.json")
        assert target["runtime"] == metadata["runtime"], "PDFium or font identity differs"
        assert target["documents"].keys() == metadata["documents"].keys()
        for key in metadata["documents"]:
            result = compare_geometry(
                _read(reference / key / "geometry.json"),
                _read(candidate / key / "geometry.json"),
                metadata["runtime"]["font_sha256"],
            )
            assert _semantics(_read(reference / key / "model.json")) == _semantics(_read(candidate / key / "model.json")), (
                key,
                "Semantic order differs",
            )
            report["comparisons"].append(
                {
                    "reference": metadata["system"],
                    "candidate": target["system"],
                    "document": key,
                    **result,
                    "semantics_equal": True,
                }
            )
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
