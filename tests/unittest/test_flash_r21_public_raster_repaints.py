"""实物回归覆盖局部位图重绘的像素归属、隐层和可编辑文字。"""

from hashlib import file_digest
from os import environ
from pathlib import Path

import pytest

from docvortex import parse
from tools.score_flash_graphic_holdout import load, main_dom, markdown_representation, visible_figures
from tools.score_flash_visual_ownership import html_text, score_page, witness_match


@pytest.mark.parametrize("options", [{}, {"parse_mode": "txt", "pdf_structure": "auto"}], ids=["default", "native"])
def test_partial_repaint_owns_updated_banner_and_original_profile(options, tmp_path):
    """新版横幅与下方原截图须各出现一次、同属一件；浏览器标签和左栏文字保持独立。"""
    gold = load(Path(__file__).resolve().parents[1] / "fixtures/flash_layout_gt_r20_visual_ownership_v1.json.gz")
    source = gold["sources"]["eagle-eye-brand"]
    filename = environ.get("DOCVORTEX_TEST_EAGLE_EYE_BRAND")
    if not filename:
        pytest.skip(f"Set DOCVORTEX_TEST_EAGLE_EYE_BRAND to the reviewed original: {source['source_url']}")
    with Path(filename).open("rb") as stream:
        assert file_digest(stream, "sha256").hexdigest() == source["sha256"]

    result = parse(Path(filename), **options)
    pages = result.to_dict()["pages"]
    assert len(pages) == source["pages"]
    html_path, md_path = tmp_path / "document.html", tmp_path / "document.md"
    result.export(html_path, output_format="html")
    result.export(md_path, output_format="markdown")
    html = html_path.read_text()
    md, references = markdown_representation(md_path.read_text())
    crops = visible_figures(pages[22], main_dom(html)[23], tmp_path, references)
    sample = source["annotated"]["23"]
    criteria = gold["geometry_rules"]
    scored = score_page(sample, pages[22], html_text(html)[23], md, crops, criteria)
    assert len(crops) == 2 and all(crop["linked"] for crop in crops)
    assert all(group["passed"] and group["owner_count"] == 1 for group in scored["groups"]), scored["groups"]
    assert all(overlap >= 0.9 for overlap in scored["geometry"].values())
    assert all(anchor["passed"] for anchor in scored["protected"]), scored["protected"]
    for stale in sample["forbidden_witnesses"]:
        assert all(not witness_match(stale, crop, criteria)["passed"] for crop in crops)
