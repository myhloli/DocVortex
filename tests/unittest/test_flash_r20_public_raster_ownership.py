"""用上一轮已封存的真实纸面墨迹检查完整设备、原件节点映射及公开导出。"""

import gzip
from hashlib import file_digest
import json
from os import environ
from pathlib import Path

import pytest

from docvortex import parse
from tools.score_flash_graphic_holdout import main_dom, markdown_representation, visible_figures
from tools.score_flash_visual_ownership import envelope, part_result, witness_match


@pytest.mark.parametrize(
    ("slug", "variable", "physical"),
    [
        ("sogeti-brand", "DOCVORTEX_TEST_SOGETI_BRAND", 43),
        ("bsh-siemens", "DOCVORTEX_TEST_BSH_SIEMENS", 23),
    ],
)
@pytest.mark.parametrize("options", [{}, {"parse_mode": "txt"}], ids=["default", "native"])
def test_published_device_owns_its_visible_parts(slug, variable, physical, options, tmp_path):
    """真实成品中的设备与旁边另一产品须各有唯一图片，并保留全部可见物理见证。"""
    gold = json.loads(
        gzip.decompress(
            (
                Path(__file__).resolve().parents[1] / "fixtures" / "flash_layout_gt_r19_visual_ownership_difficulty_v1.json.gz"
            ).read_bytes()
        )
    )
    source = gold["sources"][slug]
    if not (filename := environ.get(variable)):
        pytest.skip(f"Set {variable} to the reviewed original: {source['source_url']}")
    with Path(filename).open("rb") as stream:
        assert file_digest(stream, "sha256").hexdigest() == source["sha256"]
    result = parse(Path(filename), **options)
    data = result.to_dict()
    assert len(data["pages"]) == source["pages"]
    if slug == "sogeti-brand":
        extension = data["extensions"]["docvortex_pdf_structure"]
        # 平板、更新后的屏幕及横幅指向同一成品。左侧手机继续独立可寻址。
        owner = extension["blocks"]["4767:0"]
        for node in ("4771:0", "4773:0", "4774:0"):
            assert extension["nodes"][node]["role"] == "Figure"
            assert extension["blocks"][node] == owner
        assert extension["blocks"]["4776:0"] != owner

    html_path, md_path = tmp_path / "document.html", tmp_path / "document.md"
    result.export(html_path, output_format="html")
    result.export(md_path, output_format="markdown")
    _, references = markdown_representation(md_path.read_text())
    page_nodes = main_dom(html_path.read_text())[physical]
    crops = visible_figures(data["pages"][physical - 1], page_nodes, tmp_path, references)
    sample = source["annotated"][str(physical)]
    criteria = gold["geometry_rules"]
    subjects = []
    for group in sample["groups"]:
        scope = envelope(group["parts"], criteria["scope_padding"])
        if group["id"] == "adjacent-separate-phone":
            # R19 的左手机将火箭分开导出，单独核验纸面可见且原来由完整手机承载的刘海和头像。
            witness = next(w for w in group["parts"][0]["witnesses"] if w["name"] == "phone-notch-and-profile")
            (mobile,) = [crop for crop in crops if witness_match(witness, crop, criteria)["passed"]]
            assert mobile["linked"]
            subjects.append(mobile["index"])
            continue
        owners = [part_result(part, scope, crops, criteria) for part in group["parts"]]
        assert all(part["passed"] for part in owners)
        (image_index,) = {entry["index"] for part in owners for entry in part["owners"]}
        subjects.append(image_index)
    assert len(set(subjects)) == len(subjects)
    for forbidden in sample.get("forbidden_witnesses", []):
        assert not any(witness_match(forbidden, crop, criteria)["passed"] for crop in crops)
