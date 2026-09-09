"""验证同字体局部正文复核及真实论文的上下标输出。"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from bs4 import BeautifulSoup

from docvortex.analyzers.native.pdf._script_geometry import classify_char_script_roles
from docvortex.analyzers.native.pdf.geometry import _rotate_bbox_from_upright
from docvortex.analyzers.native.pdf.inline.scripts import _refine_math_script_tokens, _script_line_char_roles
from docvortex.api import parse

_ROOT = Path(__file__).parents[1]
_FIXTURE = _ROOT / "tests/fixtures/pdf_mixed_font_script_line.json"


def _fixture() -> tuple[list[dict[str, Any]], dict[int, tuple[float, ...]], dict[int, tuple[float, ...]]]:
    """读取最小原始字符片段，为每个测试提供独立副本。"""
    data = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    chars, tight, origins = [], {}, {}
    for row in data["chars"]:
        char = dict(row)
        box, origin = char.pop("tight"), char.pop("origin")
        chars.append(char)
        if box is not None:
            tight[char["char_idx"]] = tuple(box)
        if origin is not None:
            origins[char["char_idx"]] = tuple(origin)
    return chars, tight, origins


@pytest.mark.parametrize("halfwidth", [False, True])
@pytest.mark.parametrize("angle", [0, 90, 180, 270])
def test_target_roles_are_width_and_rotation_independent(halfwidth: bool, angle: int) -> None:
    """半角/全角及四种旋转下，单位为正文，完整括号引用为上标，原始输入不变。"""
    chars, tight, origins = _fixture()
    size = (595.0, 842.0)
    for char in chars:
        if halfwidth and "！" <= char["char"] <= "～":
            char["char"] = chr(ord(char["char"]) - 0xFEE0)
        char["bbox"] = _rotate_bbox_from_upright(tuple(char["bbox"]), size, angle)
    tight = {index: _rotate_bbox_from_upright(box, size, angle) for index, box in tight.items()}
    rotated_origins = {}
    for index, (x, y) in origins.items():
        rotated_origins[index] = {0: (x, y), 90: (size[0] - y, x), 180: (size[0] - x, size[1] - y), 270: (y, size[1] - x)}[
            angle
        ]
    before = deepcopy((chars, tight, rotated_origins))
    line = SimpleNamespace(chars=chars, angle=angle, inline_math_regions=[])
    ordered, roles, _, _ = _script_line_char_roles(line, size, tight, rotated_origins, set())
    actual = {str(c["char_idx"]): role for c, role in zip(ordered, roles) if 1353 <= c["char_idx"] <= 1360}
    assert actual == json.loads(_FIXTURE.read_text(encoding="utf-8"))["expected"]
    assert (chars, tight, rotated_origins) == before


@pytest.mark.parametrize("boundary", ["(", "+", " ", "中"])
def test_reference_cannot_cross_text_boundaries(boundary: str) -> None:
    """候选串内只剩一个正文参考时，不借用括号、运算符、空白或 CJK 外的样本。"""
    chars, tight, origins = _fixture()
    next(c for c in chars if c["char_idx"] == 1354)["char"] = boundary
    roles = classify_char_script_roles(chars, tight_bboxes=tight, origins=origins)
    assert [r for c, r in zip(chars, roles) if c["char_idx"] in {1355, 1356}] == ["sub", "sub"]


@pytest.mark.parametrize("mode", ["missing", "one-reference", "subset-boundary"])
def test_insufficient_font_evidence_keeps_original_classification(mode: str) -> None:
    """缺少字体、仅一个参考或不同子集字体时，保留原有几何判定而不强制抹除。"""
    chars, tight, origins = _fixture()
    for char in chars:
        if mode == "missing":
            char.pop("font", None)
        elif char["char_idx"] in ({1353, 1357} if mode == "one-reference" else {1353, 1354, 1357}):
            char["font"] = {**char["font"], "name": "another-subset-font"}
    roles = classify_char_script_roles(chars, tight_bboxes=tight, origins=origins)
    assert [r for c, r in zip(chars, roles) if c["char_idx"] in {1355, 1356}] == ["sub", "sub"]


def test_body_reference_is_not_forced_to_superscript() -> None:
    """同基线的普通数字引用保持正文，不因方括号模式而强制升为上标。"""
    chars, tight, origins = _fixture()
    body = next(c for c in chars if c["char_idx"] == 1353)
    for char in chars:
        index = char["char_idx"]
        if index not in {1358, 1359, 1360}:
            continue
        char["bbox"][1], char["bbox"][3] = body["bbox"][1], body["bbox"][3]
        tight[index] = (tight[index][0], tight[1353][1], tight[index][2], tight[1353][3])
        origins[index] = (origins[index][0], origins[1353][1])
    _, roles, _, _ = _script_line_char_roles(
        SimpleNamespace(chars=chars, angle=0, inline_math_regions=[]), (595, 842), tight, origins, set()
    )
    assert all(role == "body" for char, role in zip(chars, roles) if 1353 <= char["char_idx"] <= 1360)


def test_real_paper_exports_correct_unit_and_complete_citation(tmp_path: Path) -> None:
    """公开 Model/Middle/HTML/Markdown 保留完整正文单位和整体引用上标。"""
    source = _ROOT / "demo/pdfs/中文论文4.pdf"
    assert hashlib.sha256(source.read_bytes()).hexdigest() == json.loads(_FIXTURE.read_text(encoding="utf-8"))["source_sha256"]
    result = parse(source, keep_model_json=True)
    model = result.model_json.to_dict(skip_defaults=False)
    middle = result.middle_json.to_dict(skip_defaults=False)
    for pages, raw in [(model["pages"], True), (middle["pages"], False)]:
        blocks = [b for p in pages for b in (p if raw else p["blocks"])]
        content = next(b["content"] for b in blocks if "850hPa" in json.dumps(b.get("content"), ensure_ascii=False))
        assert any("12gpm" in s.get("content", "") and not s.get("styles") for s in content)
        assert any(s.get("content") == "［8］" and s.get("styles") == ["superscript"] for s in content)
    author_block = next(
        b
        for b in model["pages"][4]
        if "TangJiaping"
        in "".join(
            s.get("content", "") for s in b.get("content", []) if isinstance(s, dict) and isinstance(s.get("content"), str)
        )
    )
    assert all("subscript" not in span.get("styles", []) for span in author_block["content"])
    assert any(span.get("content") == "1，2" and span.get("styles") == ["superscript"] for span in author_block["content"])
    for target, suffix in [("html", "html"), ("markdown", "md")]:
        path = tmp_path / f"paper.{suffix}"
        result.export(path, output_format=target)
        markup = path.read_text(encoding="utf-8")
        assert "<sup>［8］</sup>" in markup
        assert "12gpm" in markup and "<sub>12</sub>" not in markup
        if target == "html":
            paragraph = next(p for p in BeautifulSoup(markup, "html.parser").find_all("p") if "850hPa" in p.get_text())
            assert not paragraph.find_all("sub")


@pytest.mark.parametrize("halfwidth", [False, True])
@pytest.mark.parametrize("word", ["TangJiaping", "Sample"])
def test_numeric_superscript_does_not_rebase_body_word(word: str, halfwidth: bool) -> None:
    """正文词语后已有数字上标时，下伸字形不应成为新的 token 切分点。"""
    text = word + "1"
    chars, tight, origins = [], {}, {}
    for index, char in enumerate(text):
        shift = -3.0 if char.isdigit() else 0.6 if char in "gp" else 0.0
        source = char if halfwidth else chr(ord(char) + 0xFEE0)
        box = (index * 6.0, shift, index * 6.0 + 5.0, 8.0 + shift)
        chars.append({"char": source, "char_idx": index, "bbox": box})
        tight[index] = box
        origins[index] = (index * 6.0, 10.0 + shift)
    expected = ["body"] * len(word) + ["sup"]
    assert _refine_math_script_tokens(chars, expected, tight, origins, formula_region=False) == expected
