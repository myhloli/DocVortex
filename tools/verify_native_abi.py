"""不导入应用依赖，直接在当前 CPython 上检查实际 wheel 中的原生 ABI。"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import subprocess
import tempfile
import zipfile


def verify_binary(binary: Path) -> None:
    """在独立进程中执行内核，让 Windows 在退出后释放已加载 DLL。"""
    spec = importlib.util.spec_from_file_location("_native", binary)
    assert spec is not None and spec.loader is not None
    native = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(native)
    assert native.PROTOCOL_VERSION == 6
    baseline = native.BaselineGeometryCandidates(
        [(0, 10), (0, 10)], [0, 0], [(0, 0, 10, 10), (11, 0, 20, 10)], [10, 10], [None, None]
    )
    assert baseline.rows(0, 64, 8192) == [[1], []]
    assert native.inline_script_matches([((10, 0, 12, 4), 4, 4, 1, False, 0, 0), ((0, 2, 10, 12), 10, 10, 1, False, 1, 0)]) == [
        (0, 1, False, False)
    ]
    annotations = native.AnnotationGeometry([(0, (0, 0, 2, 2), (0, 0, 2, 2))])
    assert annotations.aggregate([0], [], None) == ([(0, [0, 0, 0, 0], 1)], [0, 0, 0, 0])
    columns = native.StableColumnClusters(sys.version_info >= (3, 12))
    assert columns.extend([[(0.0, 1.0)], [(0.0, 1.0)]], 3.0) == (1, 1.0)
    metrics = native.TableNoteMetrics([(0, 0.0, 1.0), (1, 1.0, 2.0), (2, 2.0, 3.0), (3, 3.0, 4.0)])
    assert metrics.height_for_rows(metrics.prepare_rows([[]]), 0, 1, 100.0, 200.0, 1.25) == 4.0
    rows = native.TableRowGeometry([(0.0, 0.0, 2.0, 2.0), (1.0, 1.0, 3.0, 4.0)])
    assert list(rows.union_indices(0, 2)) == [0, 0, 1, 1]
    assert rows.first_after(2.0) == 1
    assert native.PDFIUM_RECORD_BATCH_SIZE == 1024
    assert native.BaselineCandidates([(0, 1), (1, 2), (3, 4)], [0, 0, 0]).rows(0, 3, 8192) == [[1], [], []]
    assert native.TableNoteMetrics([(0, 0, 1), (1, 1, 2), (2, 2, 3), (3, 3, 4)]).height(100, 200, [], 1.25) == 4.0
    assert native.mapping_runs([]) == []
    assert native.anchor_pairs([], False) == []
    assert native.title_gaps([(0, None, (0, 0, 10, 10), 10, False)]) == [(None, None)]
    assert native.line_neighbors([(0, (0, 0, 10, 10), 10, 10)]) == [(None, None)]
    try:
        native.read_pdfium_chars([0] * 10, 0, 0, False)
    except ValueError:
        pass
    else:
        raise AssertionError("null PDFium bridge arguments were accepted")
    assert list(native.script_roles([((0, 0, 5, 10), (0, 1, 5, 9), (0, 9), 4 | 256, 0)])) == [0]
    assert native.ordered_clusters([0.0, 0.5, 2.0], 0.5, 0.0, False) == [[0, 1], [2]]
    assert native.table_row_occupancy([[10.0]], [0.0, 10.0, 20.0]) == [[0]]
    assert native.typography_metrics([(0.0, 0.0, 5.0, 10.0)], [0], [400.0], [0], 1.0)[:5] == (10.0, 5.0, 0, 1.0, 400.0)
    assert native.lane_gap([], [], [], []) == (0.35, 0.0)
    assert native.grid_parents(4, [(0, 1)]) == [0, 0, 2, 3]
    assert native.coverage_batch([(0, 1, 0, 10)], [(0, [1], 0, 10, 0)]) == [1.0]


def verify(directory: Path) -> None:
    """解包唯一构建产物，子进程验证结束后再删除临时二进制。"""
    assert sys.version_info[:2] == (3, 14), sys.version
    wheels = list(directory.glob("*.whl"))
    assert len(wheels) == 1, wheels
    with tempfile.TemporaryDirectory(prefix="docvortex-abi-") as temporary:
        with zipfile.ZipFile(wheels[0]) as archive:
            members = [
                name for name in archive.namelist() if name.startswith("docvortex/_native.") and name.endswith((".so", ".pyd"))
            ]
            assert len(members) == 1, members
            binary = Path(temporary) / Path(members[0]).name
            binary.write_bytes(archive.read(members[0]))
        subprocess.run([sys.executable, str(Path(__file__).resolve()), "--binary", str(binary)], check=True)
    print(f"CPython {sys.version_info.major}.{sys.version_info.minor} ABI and native kernels verified: {wheels[0].name}")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--binary":
        verify_binary(Path(sys.argv[2]))
    else:
        verify(Path(sys.argv[1]))
