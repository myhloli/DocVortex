"""验证原生 wheel 冒烟检查会拒绝空输出、正文缺失及密集表格损坏。"""

from copy import deepcopy

import pytest

from tools.verify_native_wheel import DENSE_EXPECTATIONS, verify_output


def dense_output() -> dict:
    """构造全部 840 个单元格的最小结果，隔离验证器而不重新运行 PDF 算法。"""
    cells = [f"{row:03d}-{column}-{row * 7 + column:04d}" for row in range(120) for column in range(7)]
    return {
        "input_sha256": "fixture",
        "model": {"pages": [[{"type": "table"}]]},
        "middle": {"pages": [{"blocks": [{"type": "table"}]}]},
        "markdown": " ".join(cells) + " Synthetic benchmark. All values are generated.",
        "assets": {},
        "diagnostics": [],
    }


def test_dense_table_semantics_and_baseline_are_verified() -> None:
    """正确单元格顺序可同时通过固定语义和完整冻结输出检查。"""
    data = dense_output()
    verify_output(data, DENSE_EXPECTATIONS, deepcopy(data))


@pytest.mark.parametrize(
    "failure", ["empty-model", "empty-middle", "empty-markdown", "missing-cell", "duplicate-cell", "reordered-cell"]
)
def test_incomplete_or_corrupt_native_output_is_rejected(failure: str) -> None:
    """原生调用成功但正文或表格损坏时，安装冒烟仍必须失败。"""
    data = dense_output()
    if failure == "empty-model":
        data["model"]["pages"] = [[]]
    elif failure == "empty-middle":
        data["middle"]["pages"][0]["blocks"] = []
    elif failure == "empty-markdown":
        data["markdown"] = "\n "
    elif failure == "missing-cell":
        data["markdown"] = data["markdown"].replace("119-6-0839", "")
    elif failure == "duplicate-cell":
        data["markdown"] += " 119-6-0839"
    else:
        data["markdown"] = data["markdown"].replace("000-0-0000 000-1-0001", "000-1-0001 000-0-0000")
    with pytest.raises(AssertionError):
        verify_output(data, DENSE_EXPECTATIONS)


def test_unknown_input_requires_an_explicit_oracle() -> None:
    """任意未登记 PDF 不能仅凭非空输出宣告通过。"""
    with pytest.raises(AssertionError, match="Unknown PDF"):
        verify_output(dense_output(), None)


def test_full_baseline_checks_diagnostics_and_assets() -> None:
    """完整基线比较覆盖正文之外的诊断和素材，防止验证退化为文本搜索。"""
    data = dense_output()
    baseline = deepcopy(data)
    baseline["assets"]["image.jpg"] = "missing asset hash"
    with pytest.raises(AssertionError, match="frozen output baseline"):
        verify_output(data, None, baseline)
