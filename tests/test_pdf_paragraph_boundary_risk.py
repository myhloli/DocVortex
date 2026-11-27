"""验证段界错位的重复证据以及正常首行缩进、短末行和栏间屏障。"""

import pytest

from docvortex.analyzers.native.pdf.layout_paragraph_risk import paragraph_boundary_risk


def _text(rows, content="unfinished"):
    """用等高原生行构建可平移的最小正文块。"""
    return {"type": "text", "content": content, "lines": [{"bbox": (x, y, right, y + 0.016)} for x, y, right in rows]}


def test_repeated_displaced_breaks_identify_undersegmentation():
    """同栏里重复的满行与短续行错位独立触发，一处仅记录弱证据。"""
    blocks = [
        _text([(0.17, 0.10, 0.80)]),
        _text([(0.13, 0.13, 0.38), (0.17, 0.16, 0.81)], [{"type": "text", "content": "未完"}]),
        _text([(0.13, 0.19, 0.44), (0.17, 0.22, 0.8)]),
    ]
    assert paragraph_boundary_risk(blocks[:2])[0] == 30
    assert paragraph_boundary_risk(blocks)[0] == 60


@pytest.mark.parametrize("change", ["terminal", "gap", "rotation", "title", "long_line", "true_indent"])
def test_legitimate_boundaries_do_not_trigger(change):
    """句末、分节空白、旋转文本、标题、正常换行与首行缩进均不能构成该证据。"""
    previous = _text([(0.17, 0.10, 0.80)])
    following = _text([(0.13, 0.13, 0.38), (0.17, 0.16, 0.81)])
    if change == "terminal":
        previous["content"] = "已完整。"
    elif change == "gap":
        previous["lines"][0]["bbox"] = (0.17, 0.01, 0.80, 0.025)
    elif change == "rotation":
        following["angle"] = 90
    elif change == "title":
        following["type"] = "paragraph_title"
    elif change == "long_line":
        following["lines"][0]["bbox"] = (0.13, 0.13, 0.79, 0.146)
    else:
        following["lines"].reverse()
    assert paragraph_boundary_risk([previous, following]) is None
