"""验证独立引擎只提供七种目标，不反向加载宿主专用 renderer。"""

from __future__ import annotations

from importlib import util
from pathlib import Path

from click.testing import CliRunner
import pytest

from docvortex.api import render
from docvortex.cli import main
from docvortex.render import RenderFormat
from docvortex.schema import MiddleJson


@pytest.mark.parametrize("target", ["content_list", "content_list_v2"])
def test_host_formats_are_not_available(target: str, tmp_path: Path) -> None:
    """枚举、模块、API 和 CLI 均不再接受宿主专用输出。"""
    assert len(RenderFormat) == 7
    assert util.find_spec(f"docvortex.render.{target}") is None
    with pytest.raises(ValueError):
        RenderFormat(target)
    document = MiddleJson(
        pages=[], metadata={"file_suffix": "html", "producer": {"name": "docvortex", "version": "0.2.0"}}, is_full_document=True
    )
    with pytest.raises(ValueError):
        render(document, target)
    source = tmp_path / "input.html"
    source.write_text("<p>Example</p>")
    output = tmp_path / "output.json"
    result = CliRunner().invoke(main, ["convert", str(source), "--format", target, "--output", str(output)])
    assert result.exit_code != 0 and "Invalid value" in result.output
    assert not output.exists()
