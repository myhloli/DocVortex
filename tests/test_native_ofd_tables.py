"""OFD 数值表格内核的冻结差分、预算位置、动态钩子和完整公开输出回归。"""

from __future__ import annotations

from dataclasses import asdict, replace
import math
from pathlib import Path
import random
from unittest.mock import Mock

import pytest

import _ofd_table_oracle as oracle
from _ofd_test_utils import build_ofd_package, page_xml, path_object, text_object
from docvortex import parse
from docvortex._compute_backend import get_native
from docvortex.analyzers.native.ofd import reading_order, table
from docvortex.analyzers.native.ofd.errors import OfdResourceLimitError
from docvortex.analyzers.native.ofd.models import AxisLine, TextLine

ROOT = Path(__file__).resolve().parents[1]


def _axis(horizontal: bool, coordinate: float, start: float, end: float) -> AxisLine:
    """构造零厚度中心线，避免测试自身额外引入中心坐标平均误差。"""
    bbox = (start, coordinate, end, coordinate) if horizontal else (coordinate, start, coordinate, end)
    return AxisLine(bbox, "horizontal" if horizontal else "vertical", 0.1, 0, None)


def _snapshot(lines: list[AxisLine]) -> list[tuple]:
    """用冻结助手打包数值预期，不复用候选的快路保护或坐标解释。"""
    return [(line.orientation == "horizontal", oracle._line_coord(line), *oracle._line_interval(line)) for line in lines]


def _components_expected(lines: list[AxisLine], count: int, limit: int) -> tuple:
    """执行原两两比较，保留首个失败的计数和完整错误消息。"""
    oracle.MAX_TABLE_INTERSECTION_CHECKS = limit
    budget = oracle.OfdTableBudget(count)
    try:
        groups = oracle._components(lines, budget)
        identities = {id(line): index for index, line in enumerate(lines)}
        return [[identities[id(line)] for line in group] for group in groups], budget.intersection_check_count, False
    except OfdResourceLimitError:
        return [], budget.intersection_check_count, True


def _cells_expected(xs: list[float], ys: list[float], lines: list[AxisLine], count: int, limit: int) -> tuple:
    """执行原逐格全线扫描，区分拒绝候选和预算异常。"""
    oracle.MAX_TABLE_INTERSECTION_CHECKS = limit
    budget = oracle.OfdTableBudget(count)
    try:
        cells = oracle._logical_cells(xs, ys, lines, budget)
        value = None if cells is None else [[cell.row, cell.column, cell.end_row, cell.end_column] for cell in cells]
        return value, budget.intersection_check_count, False
    except OfdResourceLimitError:
        return None, budget.intersection_check_count, True


def _grid(xs: list[float], ys: list[float]) -> list[AxisLine]:
    """构造受控完整网格，分隔线可在调用方删去或裁短。"""
    return [_axis(True, y, xs[0], xs[-1]) for y in ys] + [_axis(False, x, ys[0], ys[-1]) for x in xs]


@pytest.mark.parametrize("seed", range(128))
def test_native_components_match_frozen_pairs(seed: int) -> None:
    """固定种子覆盖平行、交叉、碎片、重复和打乱顺序，连同虚拟预算逐项比较。"""
    rng = random.Random(seed)
    lines = []
    for _ in range(rng.randrange(1, 55)):
        coordinate = rng.randrange(-12, 13) * rng.choice([0.2, 0.6, 0.8, 3.0])
        start = rng.randrange(-12, 13) * 0.6
        end = start + rng.choice([0.0, 0.2, 0.8, 1.6, 10.0])
        lines.append(_axis(bool(rng.randrange(2)), coordinate, start, end))
    pairs = len(lines) * (len(lines) - 1) // 2
    for count, limit in [(0, 0), (4, 3), (2, pairs + 1), (2, pairs + 2), (3, 2_000_000)]:
        assert get_native().ofd_table_components(_snapshot(lines), count, limit) == _components_expected(lines, count, limit)


@pytest.mark.parametrize("seed", range(128))
def test_native_cells_match_frozen_grid_scan(seed: int) -> None:
    """生成完整、合并、局部边和非矩形网格，精确比较首次拒绝及首次预算失败的位置。"""
    rng = random.Random(seed)
    xs = [rng.choice([-100.0, 0.0, 3.0])]
    ys = [rng.choice([-50.0, 0.0, 4.0])]
    for _ in range(rng.randrange(1, 8)):
        xs.append(xs[-1] + rng.choice([0.4, 0.8, 1.0, 3.0]))
    for _ in range(rng.randrange(1, 8)):
        ys.append(ys[-1] + rng.choice([0.4, 0.8, 1.0, 4.0]))
    lines = _grid(xs, ys)
    for _ in range(rng.randrange(8)):
        line = lines.pop(rng.randrange(len(lines)))
        if rng.randrange(2):
            start, end = oracle._line_interval(line)
            lines.append(_axis(line.orientation == "horizontal", oracle._line_coord(line), start + (end - start) / 2, end))
        if not lines:
            break
    rng.shuffle(lines)
    for count, limit in [(0, 0), (4, 3), (0, 20), (3, 100), (2, 2_000_000)]:
        assert get_native().ofd_table_logical_cells(xs, ys, _snapshot(lines), count, limit) == _cells_expected(
            xs, ys, lines, count, limit
        )


@pytest.mark.parametrize("offset", [0.0, -1000.0, 1e12, -1e12, 2.0**51, -(2.0**51), 1e-300])
def test_native_index_keeps_float_tolerance_neighbors(offset: float) -> None:
    """覆盖容差邻界及大坐标的舍入，索引预筛不能漏掉原谓词接受的线对。"""
    coordinates = [offset, offset + 0.6, math.nextafter(offset + 0.6, -math.inf), math.nextafter(offset + 0.6, math.inf)]
    lines = [_axis(True, coordinate, -10.0, 10.0) for coordinate in coordinates]
    lines += [
        _axis(False, coordinate, -10.0, 10.0)
        for coordinate in [-10.8, math.nextafter(-10.8, -math.inf), 10.8, math.nextafter(10.8, math.inf)]
    ]
    for ordered in [lines, list(reversed(lines))]:
        assert get_native().ofd_table_components(_snapshot(ordered), 0, 100) == _components_expected(ordered, 0, 100)
    xs, ys = [-10.0, 0.0, 10.0], [-10.0, 0.6, 10.0]
    assert get_native().ofd_table_logical_cells(xs, ys, _snapshot(lines), 0, 1000) == _cells_expected(xs, ys, lines, 0, 1000)


@pytest.mark.parametrize("sign", [-1.0, 1.0])
@pytest.mark.parametrize("tiny", [1e-300, 1e-17, math.ulp(0.6) / 4, math.ulp(0.6) / 2])
def test_native_index_preserves_tolerance_cancellation(sign: float, tiny: float) -> None:
    """容差减法舍入允许零点另一侧的微小坐标，单纯外扩查询端点一个ULP会漏掉它。"""
    coordinate, other = sign * 0.6, -sign * tiny
    lines = [_axis(True, coordinate, 0.0, 2.0), _axis(True, other, 0.0, 2.0)]
    for ordered in [lines, list(reversed(lines))]:
        assert get_native().ofd_table_components(_snapshot(ordered), 0, 100) == _components_expected(ordered, 0, 100)
    xs, ys = [-2.0, coordinate, 2.0], [0.0, 2.0]
    vertical = [_axis(False, other, 0.0, 2.0)]
    assert get_native().ofd_table_logical_cells(xs, ys, _snapshot(vertical), 0, 100) == _cells_expected(
        xs, ys, vertical, 0, 100
    )
    normalized = oracle._normalize_axis_lines(
        [
            _axis(True, -0.7, 0.0, 2.0),
            _axis(True, -0.5, 0.0, 2.0),
            _axis(True, -0.05, 0.0, 2.0),
            _axis(True, math.nextafter(0.05, math.inf), 0.0, 2.0),
        ]
    )
    assert len(normalized) == 2
    assert get_native().ofd_table_components(_snapshot(normalized), 0, 100) == _components_expected(normalized, 0, 100)


def test_native_empty_and_short_interval_behavior() -> None:
    """无边、无比较、零长线以及短于覆盖容差的边仍与原实现相同。"""
    native = get_native()
    assert native.ofd_table_components([], 9, 2) == ([], 9, False)
    for gap in [0.8, math.nextafter(0.8, -math.inf), math.nextafter(0.8, math.inf)]:
        for lines in [[], [_axis(False, 1.0, 0.0, 0.0)]]:
            xs, ys = [0.0, 1.0, 2.0], [0.0, gap]
            assert native.ofd_table_logical_cells(xs, ys, _snapshot(lines), 0, 100) == _cells_expected(xs, ys, lines, 0, 100)


def test_python_budget_is_preserved_on_shared_and_partial_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    """跨次调用保留同一预算，局部断边先于后续超限，预算错误发生时计数恰好加一。"""
    lines = _grid([0.0, 5.0, 10.0], [0.0, 5.0, 10.0])
    budget = table.OfdTableBudget()
    monkeypatch.setattr(table, "MAX_TABLE_INTERSECTION_CHECKS", 20)
    assert len(table._components(lines, budget)) == 1
    assert budget.intersection_check_count == 15
    with pytest.raises(OfdResourceLimitError, match="max_table_intersection_checks=20"):
        table._logical_cells([0.0, 5.0, 10.0], [0.0, 5.0, 10.0], lines, budget)
    assert budget.intersection_check_count == 21
    lines[4] = _axis(False, 5.0, 2.0, 10.0)
    budget = table.OfdTableBudget()
    assert table._logical_cells([0.0, 5.0, 10.0], [0.0, 5.0, 10.0], lines, budget) is None
    assert budget.intersection_check_count == 6


@pytest.mark.parametrize(
    "override",
    [
        "touches",
        "coordinate",
        "interval",
        "covers",
        "coord-tolerance",
        "intersection-tolerance",
        "budget-class",
        "budget-alias",
        "budget-method",
        "float-budget",
        "negative-budget",
        "cell-type",
        "unknown-orientation",
        "large-int",
        "nonfinite",
        "reversed",
    ],
)
def test_native_adapter_preserves_custom_contracts(monkeypatch: pytest.MonkeyPatch, override: str) -> None:
    """自定义方法、容差、对象或非默认数字类型仍执行原扫描，不跳过可观察的钩子。"""
    lines = _grid([0.0, 5.0, 10.0], [0.0, 5.0, 10.0])
    budget = table.OfdTableBudget()
    if override in {"touches", "coordinate", "interval", "covers"}:
        name = {"touches": "_touches", "coordinate": "_line_coord", "interval": "_line_interval", "covers": "_covers_interval"}[
            override
        ]
        monkeypatch.setattr(table, name, Mock(wraps=getattr(table, name)))
    elif override == "coord-tolerance":
        monkeypatch.setattr(table, "_COORD_TOLERANCE", 0.7)
    elif override == "intersection-tolerance":
        monkeypatch.setattr(table, "_INTERSECTION_TOLERANCE", 0.9)
    elif override == "budget-class":

        class CustomBudget(table.OfdTableBudget):
            """模拟用户预算子类，仍由逐项方法负责计费。"""

        budget = CustomBudget()
    elif override == "budget-alias":
        monkeypatch.setattr(table, "OfdTableBudget", Mock())
    elif override == "budget-method":
        original = table.OfdTableBudget.charge_intersection_check

        def charge(self):
            """保留计费结果，标记动态替换的方法不能被批量计费绕过。"""
            original(self)

        monkeypatch.setattr(table.OfdTableBudget, "charge_intersection_check", charge)
    elif override == "float-budget":
        budget.intersection_check_count = 0.0
    elif override == "negative-budget":
        budget.intersection_check_count = -1
    elif override == "cell-type":
        monkeypatch.setattr(table, "OfdTableCell", oracle.OfdTableCell)
    elif override == "unknown-orientation":
        lines[0] = replace(lines[0], orientation="diagonal")
    elif override == "large-int":
        lines[0] = replace(lines[0], bbox=(0, 0, 2**70, 0))
    elif override == "nonfinite":
        lines[0] = replace(lines[0], bbox=(0.0, math.nan, 10.0, math.nan))
    elif override == "reversed":
        lines[0] = replace(lines[0], bbox=(10.0, 0.0, 0.0, 0.0))
    native = get_native()
    if override not in {"covers", "cell-type"}:
        spy = Mock(side_effect=AssertionError("unexpected native components"))
        monkeypatch.setattr(native, "ofd_table_components", spy)
        table._components(lines, budget)
        spy.assert_not_called()
    if override != "touches":
        if override == "negative-budget":
            budget.intersection_check_count = -1
        spy = Mock(side_effect=AssertionError("unexpected native topology"))
        monkeypatch.setattr(native, "ofd_table_logical_cells", spy)
        table._logical_cells([0.0, 5.0, 10.0], [0.0, 5.0, 10.0], lines, budget)
        spy.assert_not_called()


def _public_output(result) -> tuple:
    """保留完整两层结构、原始素材字节和诊断，不能只比较识别到的表格数量。"""
    return (
        result.model_json.model_dump(mode="json"),
        result.middle_json.model_dump(mode="json"),
        dict(result.assets),
        [asdict(item) for item in result.diagnostics],
    )


def _public_fixture() -> bytes:
    """通过原生路径与真实文字构造两行两列，覆盖公开解析的表格恢复入口。"""
    paths = "".join(
        path_object(i, boundary="0 0 100 100", data=data)
        for i, data in enumerate(
            ["M 10 10 L 90 10", "M 10 40 L 90 40", "M 10 70 L 90 70", "M 10 10 L 10 70", "M 50 10 L 50 70", "M 90 10 L 90 70"]
        )
    )
    body = "".join(
        text_object(100 + i, value, boundary=box, size=3, y=3)
        for i, (value, box) in enumerate([("A", "20 20 10 5"), ("B", "60 20 10 5"), ("C", "20 50 10 5"), ("D", "60 50 10 5")])
    )
    return build_ofd_package([("Pages/Page_0/Content.xml", page_xml(paths + body))])


@pytest.mark.parametrize("kind", ["fixture", "real"])
def test_public_ofd_entry_uses_native_and_matches_frozen_tables(monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    """真实和合成 OFD 的公开入口命中两个原生阶段，并与冻结表格恢复完整对照。"""
    source = _public_fixture() if kind == "fixture" else (ROOT / "demo/ofd/ofd_01.ofd").read_bytes()
    native = get_native()
    before = native.ofd_table_stats()
    actual = parse(source, file_suffix="ofd", keep_model_json=True)
    after = native.ofd_table_stats()
    assert after[0] > before[0] and after[1] > before[1] and after[4] == before[4]
    oracle.MAX_TABLE_INTERSECTION_CHECKS = 2_000_000
    monkeypatch.setattr(reading_order, "recover_tables", oracle.recover_tables)
    expected = parse(source, file_suffix="ofd", keep_model_json=True)
    assert _public_output(actual) == _public_output(expected)


def test_normalization_and_recovery_keep_original_float_coordinates() -> None:
    """坐标平均和聚类仍由原 Python 执行，返回字段和对象消耗集合保持一致。"""
    xs, ys = [0.0, 5.0, 10.0], [0.0, 5.0, 10.0]
    lines = _grid(xs, ys)
    lines += [_axis(True, math.nextafter(5.0, math.inf), 0.0, 10.0)]
    text = [
        TextLine("A", (1.0, 1.0, 2.0, 2.0), [], 0, 1, 0, 1, "Body", None),
        TextLine("B", (6.0, 6.0, 7.0, 7.0), [], 0, 1, 1, 2, "Body", None),
    ]
    actual_budget, expected_budget = table.OfdTableBudget(), oracle.OfdTableBudget()
    oracle.MAX_TABLE_INTERSECTION_CHECKS = 2_000_000
    actual = table.recover_tables(lines, text, actual_budget)
    expected = oracle.recover_tables(lines, text, expected_budget)
    assert [asdict(item) for item in actual] == [asdict(item) for item in expected]
    assert actual_budget.intersection_check_count == expected_budget.intersection_check_count


def test_large_grid_preserves_default_document_budget_failure() -> None:
    """默认两百万预算下，大网格仍在原计数失败，索引加速不能把被拒绝的输入变成成功。"""
    axes = [float(index * 2) for index in range(81)]
    lines = _grid(axes, axes)
    text = [
        TextLine("A", (0.5, 0.5, 1.0, 1.0), [], 0, 1, 0, 1, "Body", None),
        TextLine("B", (2.5, 2.5, 3.0, 3.0), [], 0, 1, 1, 2, "Body", None),
    ]
    oracle.MAX_TABLE_INTERSECTION_CHECKS = 2_000_000
    for recover, budget in [(table.recover_tables, table.OfdTableBudget()), (oracle.recover_tables, oracle.OfdTableBudget())]:
        with pytest.raises(OfdResourceLimitError, match="max_table_intersection_checks=2000000"):
            recover(lines, text, budget)
        assert budget.intersection_check_count == 2_000_001
