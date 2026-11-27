"""复核图片归属评分中公开主标题、图注和真实父块的角色。"""

from tools.score_flash_visual_ownership import html_text, local_text_hits, norm


def test_heading_and_caption_are_counted_only_under_their_visible_owner():
    """覆盖 HTML 渲染器的 p 图注、文档主标题、隐藏图注和另一父图同文的情况。"""
    html = """
    <div class="docvortex-block" data-page-idx="0" data-block-index="0" data-block-type="doc_title">
      <h1>Trail guide</h1></div>
    <div class="docvortex-block" data-page-idx="0" data-block-index="7" data-block-type="image"><figure>
      <p class="docvortex-caption" data-block-index="8" data-block-type="image_caption">Waveform</p>
      <p hidden class="docvortex-caption" data-block-type="image_caption">Hidden line</p></figure></div>
    <div class="docvortex-block" data-page-idx="0" data-block-index="9" data-block-type="image"><figure>
      <figcaption><p class="docvortex-caption" data-block-type="image_caption">Waveform</p></figcaption></figure></div>
    """
    visible = html_text(html)[1]
    assert visible == [
        (0, "doc_title", norm("Trail guide")),
        (7, "caption", norm("Waveform")),
        (9, "caption", norm("Waveform")),
    ]
    for text, role, index, typ in (("Trail guide", "heading", 0, "doc_title"), ("Waveform", "caption", 7, "image_caption")):
        box = [0.1, 0.2, 0.4, 0.3]
        ref = {"role": role, "text": text, "box": box}
        rows = [{"bbox": box, "type": typ, "text": norm(text), "index": index, "public_index": index}]
        hits = local_text_hits(ref, rows, visible, {"text_position_overlap": 0.6})
        assert len(hits) == 1 and hits[0]["expected_role"] and hits[0]["count"] == 1


def test_teacher_counts_same_part_name_in_distinct_groups(tmp_path):
    """同页两个组里同名的主图和图例均须保留在教师评分分母中。"""
    import json
    from tools.score_flash_visual_ownership import independent_boundaries

    boxes = [[0.1, 0.1, 0.4, 0.4], [0.6, 0.6, 0.9, 0.9]]
    groups = [
        {"id": group, "parts": [{"id": role, "role": role, "box": b} for role in ("primary", "legend")]}
        for group, b in zip(("top", "bottom"), boxes, strict=True)
    ]
    page = {"groups": groups, "source_image_sha256": "sample-image"}
    source = {"sha256": "a" * 64, "annotated": {"1": page}}
    (tmp_path / "vlm").mkdir()
    pred = {"image_sha256": "sample-image", "boxes": [{"type": "image", "bbox": b} for b in boxes]}
    (tmp_path / "vlm" / ("d-" + "a" * 16 + "-p1.json")).write_text(json.dumps(pred))
    outcome = independent_boundaries({"test": source}, tmp_path, {"geometry_iou_report": 0.5, "part_coverage": 0.85})
    assert outcome["vlm"]["primary_geometry_at_0.5"] == [2, 2]
    assert outcome["vlm"]["legend_geometry_coverage"] == [2, 2]
