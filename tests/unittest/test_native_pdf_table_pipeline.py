# Copyright (c) Opendatalab. All rights reserved.
"""验证 Native PDF 表格结构恢复和 Flash OCR 表格投影。"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from docvortex.analyzers.native.pdf import models as flash_models
from docvortex.analyzers.native.pdf import table_materialization
from docvortex.analyzers.native.pdf import tables as flash_tables


def test_flash_materialization_prefers_native_html_and_keeps_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 Flash 只替换表体 content，不改变候选认领语义。"""

    source = flash_models._PageSource(
        page_size=(100.0, 100.0),
        lines=[
            flash_models._LineItem(
                text="A B",
                bbox=(10.0, 20.0, 80.0, 30.0),
                angle=0,
                source_index=7,
                effective_height=10.0,
            )
        ],
        chars=[],
        drawing_lines=[],
    )
    candidate = flash_models._TableCandidate(
        bbox=(0.0, 10.0, 90.0, 40.0),
        local_bbox=(0.0, 10.0, 90.0, 40.0),
        angle=0,
        score=1.0,
        core_bbox=(0.0, 10.0, 90.0, 40.0),
        line_indices={7},
    )
    html = "<table><tbody><tr><td>A</td><td>B</td></tr></tbody></table>"
    projection = MagicMock(return_value="fallback")
    monkeypatch.setattr(table_materialization, "_recover_native_table_html", MagicMock(return_value=html))
    monkeypatch.setattr(table_materialization, "project_pdf_table_text", projection)

    blocks, annotations, claimed = flash_tables._materialize_table_blocks(
        source,
        [candidate],
    )

    assert annotations == []
    assert claimed == {7}
    assert blocks[0]["content"] == html
    assert "cell_merge" not in blocks[0]
    projection.assert_not_called()
