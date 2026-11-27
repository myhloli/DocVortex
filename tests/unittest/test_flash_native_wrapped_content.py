"""保持带编号清单、嵌套模板、跨行强调和悬挂标题的原生成员边界。"""

from dataclasses import replace

import pytest
from _flash_pdf_test_utils import _text_line

from docvortex.analyzers.native.pdf import code_blocks, models
from docvortex.analyzers.native.pdf.line_layout import _should_connect_text_rows
from docvortex.analyzers.native.pdf.text_assembly.continuity import merge_overlapping_member_blocks
from docvortex.analyzers.native.pdf.title_analysis.structural import restore_hanging_title_rows
from docvortex.document.pdf.native_contracts import PDFPathInfo


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("算法 24：遍历\n 8 while queued\n 9     visit(node)\n10 return values", True),
        ("Algorithm 2: Comparison\n1 Method A  0.7\n2 Method B  0.6\n3 Method C  0.5", False),
        ("Algorithm 12: Evaluation\n1 if ready\n3 return early\n9 end", False),
        ("<table><tr><td>Algorithm 4: search</td></tr></table>", False),
    ],
)
def test_algorithm_projection_requires_independent_listing_evidence(content, expected):
    """允许不同语言、起始号与两位行号，保留行号不连续或只有数据列的比较表。"""
    assert code_blocks.is_numbered_algorithm_projection(content) is expected


@pytest.mark.parametrize("variant", ["balanced", "wrong_close", "prose_after", "missing_frame", "occupied"])
def test_template_members_require_balanced_directives_in_one_frame(variant):
    """改变措辞、缩进、字族和框位置；不匹配的闭合、后续散文或已有认领阻止整框成码。"""
    texts = ["Preview", "{% if active %}", "{{ display_name }}", "{% endif %}"]
    if variant == "wrong_close":
        texts[-1] = "{% endblock %}"
    if variant == "prose_after":
        texts.append("An independent explanation.")
    lines = [
        _text_line(text, (85 + (8 if i == 2 else 0), 40 + 9 * i, 169, 48 + 9 * i), i, font_signature=("Body", 0))
        for i, text in enumerate(texts)
    ]
    box = (80, 39, 180, lines[-1].bbox[3] + 2)
    path = PDFPathInfo(box, 5, True, False, 2, 0, (220, 223, 231, 255))
    source = models._PageSource((450, 610), lines, [], [], path_infos=[] if variant == "missing_frame" else [path])
    candidates = code_blocks._detect_code_candidates(source, [], {1} if variant == "occupied" else set())
    assert [c.line_indices for c in candidates] == ([{0, 1, 2, 3}] if variant == "balanced" else [])


@pytest.mark.parametrize("variant", ["same_run", "different_run", "new_paragraph", "barrier"])
def test_mixed_row_font_at_wrap_preserves_local_boundaries(variant):
    """两行主导字体不同，实际边缘字体相同才能续排，水平分隔线和明确句末仍阻断。"""
    before = _text_line(
        "Several requirements apply" + ("." if variant == "new_paragraph" else ""),
        (32, 50, 212, 60),
        0,
        visual_row_id=0,
        font_signature=("Display", 0),
        dominant_font_weight=700,
        font_coverage=0.8,
    )
    after = _text_line(
        "whenever a document wraps",
        (32, 62, 212, 72),
        1,
        visual_row_id=1,
        font_signature=("Italic", 64),
        dominant_font_weight=400,
        font_coverage=0.9,
    )
    before.edge_fonts = (before.font_signature, ("Other", 64) if variant == "different_run" else after.font_signature)
    after.edge_fonts = (after.font_signature, after.font_signature)
    pair = [(before, before.bbox), (after, after.bbox)]
    lane = models._TextLane(32, 212, pair)
    box = (31, 60.8, 213, 61.2)
    rules = [models._LocalAxisLine(box, box, "horizontal", 0.4)] if variant == "barrier" else []
    assert _should_connect_text_rows(*pair, lane, 2.0, 0.0, [], rules) is (variant == "same_run")


def test_numbered_title_wrap_follows_label_instead_of_number():
    """三行同式题名沿标签左缘折行；字号相同但字体变化的正文留在标题之外。"""
    head = _text_line(
        "12 A long section",
        (42, 50, 177, 62),
        1,
        font_signature=("Heading", 0),
        font_coverage=1.0,
        semantic_type="paragraph_title",
    )
    head.native_title_label_left = 66
    second = replace(head, text="that continues across", source_index=2, semantic_type=None, bbox=(66, 65, 178, 77))
    third = replace(second, text="several lines", source_index=3, bbox=(66, 80, 136, 92))
    body = replace(third, text="A new paragraph", source_index=4, bbox=(66, 95, 149, 107), font_signature=("Body", 0))
    # 已有明确成员的题名不能在这一步被分配到另一个段组。
    grouped = replace(head, paragraph_group=19)
    restore_hanging_title_rows([grouped, second])
    assert grouped.paragraph_group == 19 and second.semantic_type is None
    rows = [head, second, third, body]
    restore_hanging_title_rows(rows)
    assert [r.semantic_type for r in rows] == ["paragraph_title"] * 3 + [None]
    assert head.paragraph_group is not None and head.paragraph_group == second.paragraph_group == third.paragraph_group
    assert body.paragraph_group is None


@pytest.mark.parametrize("protected", [True, False])
def test_recovered_inline_math_uses_ink_for_wrap(protected):
    """宽松下标框跨越行界时按实际墨迹续接；已确认段界仍然阻止合并。"""
    tail = _text_line("we compute the distance", (20, 20, 135, 42), 1, effective_height=10, ink_bbox=(20, 20, 135, 30))
    math = _text_line("β ∈", (20, 30, 40, 56), 2, effective_height=10, ink_bbox=(20, 32, 37, 40))
    rest = _text_line("the domain", (80, 31, 135, 41), 3, effective_height=10, ink_bbox=(80, 32, 135, 40))
    first = {"type": "text", "content": tail.text, "bbox": tail.bbox, "_text_lines": [tail]}
    second = {
        "type": "text",
        "content": "β ∈ the domain",
        "bbox": (20, 30, 135, 56),
        "_text_lines": [math, rest],
        "_paragraph_formula_context": True,
        "_protected_hard_break_before": protected,
    }
    for block in (first, second):
        block["_local_line_bboxes"] = [line.bbox for line in block["_text_lines"]]
    out = merge_overlapping_member_blocks([first, second], (220, 300))
    assert len(out) == (2 if protected else 1)
    if not protected:
        assert {line.source_index for line in out[0]["_text_lines"]} == {1, 2, 3}
