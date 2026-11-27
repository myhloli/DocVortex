"""核对两张真实表之间的源行，并用贯穿的格线及长表项检验相反的选择。"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from _flash_pdf_test_utils import _text_line
from docvortex.analyzers.native.pdf.models import _AxisLine, _PageSource, _TableCandidate
from docvortex.analyzers.native.pdf.table_detection import _exclude_prose_spanning_rule_drafts
from docvortex.analyzers.native.pdf.table_rules import _RuleCandidateDraft


@pytest.mark.parametrize("case", [0, 1], ids=["parallel-prose-columns", "two-physical-grids"])
@pytest.mark.parametrize("scale,dx", [(1, 0), (0.7, 46)])
@pytest.mark.parametrize("control", ["prose", "long-cell", "labels"])
def test_separate_tables_do_not_claim_intervening_prose(case, scale, dx, control):
    """独立正文只能否决跨区草稿；保留短表，贯穿真格线或非叙述字段时保留原合并能力。"""
    path = Path(__file__).parents[1] / "fixtures/flash_table_prose_gap.json"
    sample = json.loads(path.read_text())[case]

    def transform(box):
        """同时偏移和缩放冻结坐标，验证区带判定不依赖原件绝对位置。"""
        x0, y0, x1, y1 = box
        return x0 * scale + dx, y0 * scale, x1 * scale + dx, y1 * scale

    lines = []
    for index, item in enumerate(sample["lines"]):
        text = item["text"] if control != "labels" else str(index)
        lines.append(
            _text_line(
                text,
                transform(item["box"]),
                index,
                ink_bbox=transform(item["ink"]) if item["ink"] else None,
                effective_height=scale * item["height"],
            )
        )
    width, height = sample["size"]
    edges = [_AxisLine(transform(box), 0.5 * scale, "horizontal") for box in sample["rules"]]
    context = SimpleNamespace(median_height=sample["em"] * scale, angle=0)
    captions = [SimpleNamespace(bbox=transform(box)) for box in sample["captions"]]
    drafts = [
        _RuleCandidateDraft(
            context, [edges[first], edges[last]], [], None if caption is None else captions[caption], [], False, 1
        )
        for first, last, caption in sample["drafts"]
    ]
    grids = [_TableCandidate(transform(box), transform(box), 0, 100, core_bbox=transform(box)) for box in sample["grids"]]
    rules = list(edges)
    if control == "long-cell":
        rules.append(_AxisLine(transform((width / 2, 0, width / 2, height)), scale, "vertical"))
    source = _PageSource((width * scale + dx, height * scale), lines, [], rules)
    result = _exclude_prose_spanning_rule_drafts(source, [*drafts, *grids])
    surviving = {id(item) for item in result}
    top, bottom = (v * scale for v in sample["gap"])
    assert all(id(grid) in surviving for grid in grids)
    spanning = [item for item in drafts if item.boundaries[0].bbox[1] <= top and item.boundaries[-1].bbox[3] >= bottom]
    assert spanning
    assert all((id(item) in surviving) == (control != "prose") for item in spanning)
    assert all(id(item) in surviving for item in drafts if item not in spanning)
