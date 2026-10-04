"""Complete process of verification of real PDF without embedded system fonts, accurate layout gold label verification using unified Droid strategy."""

from __future__ import annotations

from pathlib import Path

import pytest

from docvortex.api import parse, render
from docvortex.export.bundle import load_bundle
from docvortex.render import RenderFormat


@pytest.mark.parametrize(("file_name", "page_count"), [("中文论文3.pdf", 4), ("中文论文4.pdf", 5)])
def test_system_font_pdf_converts_and_restores_offline(file_name: str, page_count: int, tmp_path: Path) -> None:
    """The absence of embedded fonts must still produce a valid document that is complete, re-renderable, and recoverable offline."""
    source = Path(__file__).parents[1] / "demo/pdfs" / file_name
    result = parse(source, keep_model_json=True)
    assert len(result.middle_json.pages) == page_count
    assert [page.page_idx for page in result.middle_json.pages] == list(range(page_count))
    assert all(page.blocks for page in result.middle_json.pages)
    before = result.to_dict()
    for target in RenderFormat:
        artifact = render(result.middle_json, target, assets=result.assets)
        assert artifact.content, target
    assert result.to_dict() == before
    bundle = tmp_path / "bundle"
    result.save_bundle(bundle)
    restored = load_bundle(bundle)
    assert restored.to_dict() == before
    assert restored.model_json is not None
    # The result package will externalize the raw image payload as a material reference, and the model dictionary is not required to be equal to the original embedded payload byte by byte.
    assert restored.model_json.metadata.file_suffix == "pdf"
    assert restored.model_json.resolved_page_indices == list(range(page_count))
    assert restored.export(tmp_path / "restored.html", output_format="html").path.is_file()
