"""验证非PDF安装门禁拒绝空结果、损坏语义及错误来源，而不是只看进程退出。"""

from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from tools.verify_nonpdf_wheel import installed_identity, verify_output


def fixture_output() -> dict:
    """构造含正文及HTML表格的两层协议，用来暴露正文锚点丢失。"""
    return {
        "model": {
            "pages": [
                [
                    {"type": "text", "content": [{"type": "text", "content": "保留正文"}]},
                    {"type": "table", "content": "<table><tr><td>first &amp; second</td></tr></table>"},
                ]
            ]
        },
        "middle": {"pages": [{"blocks": [{"type": "table"}]}]},
        "assets": {},
        "diagnostics": [],
    }


def test_text_and_table_semantics_are_both_verified() -> None:
    """正文和表格必须同时通过固定语义检查，HTML实体不会被误当正文丢失。"""
    verify_output(fixture_output(), {"page_count": 1, "required_text": ["保留正文", "first & second"]})


@pytest.mark.parametrize("fault", ["empty-model", "empty-middle", "wrong-page", "lost-text", "lost-table"])
def test_successful_parse_with_lost_content_is_rejected(fault: str) -> None:
    """即使原生调用或解析返回成功，页结构或正文损坏仍必须失败。"""
    data = deepcopy(fixture_output())
    if fault == "empty-model":
        data["model"]["pages"] = [[]]
    elif fault == "empty-middle":
        data["middle"]["pages"][0]["blocks"] = []
    elif fault == "wrong-page":
        data["middle"]["pages"].append({"blocks": [{}]})
    elif fault == "lost-text":
        data["model"]["pages"][0].pop(0)
    else:
        data["model"]["pages"][0].pop()
    with pytest.raises(AssertionError):
        verify_output(data, {"page_count": 1, "required_text": ["保留正文", "first & second"]})


def test_source_checkout_is_not_accepted_as_installed_wheel(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """故意把模块定位到环境外源码，门禁须在执行任何解析之前拒绝。"""
    import docvortex

    monkeypatch.setattr(docvortex, "__file__", str(tmp_path / "src/docvortex/__init__.py"))
    with pytest.raises(AssertionError):
        installed_identity()


def test_portable_semantic_expectations_bind_all_real_input_bytes() -> None:
    """十四种真实格式的锚点绑定仓库输入字节，不能静默换用另一文档。"""
    root = Path(__file__).resolve().parents[1]
    document = json.loads((root / "tests/fixtures/nonpdf_wheel_expectations.json").read_text(encoding="utf-8"))
    assert document["schema_version"] == 1 and len(document["cases"]) == 14
    formats = set()
    for case in document["cases"]:
        source = root / case["path"]
        assert source.resolve().is_relative_to(root)
        assert hashlib.sha256(source.read_bytes()).hexdigest() == case["sha256"]
        assert case["required_text"] and case["page_count"] > 0
        formats.add(case["file_suffix"])
    assert formats == {"doc", "docx", "ppt", "pptx", "xls", "xlsx", "rtf", "odt", "ods", "odp", "html", "mhtml", "epub", "ofd"}
