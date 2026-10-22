"""验证固定字库的 ABI、异常隔离和进程生命周期，不污染其他测试的 PDFium 状态。"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from docvortex.document.pdf.font_runtime import PdfiumFontError, _cjk_charset, _load_font


@pytest.mark.parametrize(
    ("face", "charset", "expected"),
    [
        (b"anything", 134, 134),
        (b"anything", 136, 136),
        (b"anything", 128, 128),
        (b"anything", 129, 129),
        (b"ABCDEF+SimSun-Bold", 0, 134),
        (b"@ABCDEF+SimSun", 1, 134),
        ("宋体".encode("gb18030"), 1, 134),
        (b"MingLiU", 0, 136),
        (b"MS-PGothic", 1, 128),
        (b"Malgun Gothic", 1, 129),
        (b"private_GBK", 1, 134),
        (b"Helvetica", 0, None),
        (b"UnknownFont", 1, None),
        (b"SimSun", 2, None),
        (b"SimSun", 178, None),
    ],
)
def test_cjk_request_classification(face: bytes, charset: int, expected: int | None) -> None:
    """覆盖明确字符集、子集字体名及别名，并保护符号/其他语言请求。"""
    assert _cjk_charset(face, charset) == expected


@pytest.mark.parametrize("missing", [False, True])
def test_font_resource_hash_is_enforced(monkeypatch: pytest.MonkeyPatch, missing: bool) -> None:
    """损坏的发行资源必须显式失败，不能交由系统字体兜底。"""
    from docvortex.document.pdf import font_runtime

    class BrokenResource:
        """仅替换资源读取，保持测试不修改安装文件。"""

        def joinpath(self, *parts: str) -> BrokenResource:
            """返回同一资源替身以模拟缺失或损坏的字体。"""
            return self

        def read_text(self, *, encoding: str) -> str:
            """提供正确清单，让测试到达字节校验。"""
            return json.dumps(
                {"policy": "droid-cjk-v1", "file": "DroidSansFallbackFull.ttf", "sha256": font_runtime._FONT_SHA256}
            )

        def read_bytes(self) -> bytes:
            """返回不匹配固定哈希的内容。"""
            if missing:
                raise FileNotFoundError("bundled font missing")
            return b"broken font"

    monkeypatch.setattr(font_runtime.resources, "files", lambda package: BrokenResource())
    with pytest.raises(PdfiumFontError, match="missing" if missing else "SHA256"):
        _load_font()


def _run(code: str) -> str:
    """在新解释器中检查全局 PDFium 生命周期，避免回调状态污染测试进程。"""
    completed = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def test_runtime_initialization_is_thread_safe_and_idempotent() -> None:
    """并发首次访问只安装一次提供器，返回同一不可变身份。"""
    _run("""
from concurrent.futures import ThreadPoolExecutor
from docvortex.document.pdf import initialize_pdfium_runtime
from docvortex.document.pdf import pdfium
with ThreadPoolExecutor(max_workers=8) as pool:
    results=list(pool.map(lambda _:initialize_pdfium_runtime(),range(32)))
assert all(value is results[0] for value in results)
assert results[0].font_policy=='droid-cjk-v1'
assert pdfium._font_provider.pid > 0
""")


def test_cleanup_does_not_initialize_fonts() -> None:
    """只有关闭操作时不应加载字库或安装原生回调。"""
    _run("""
from docvortex.document.pdf import pdfium
class Child:
    def close(self):
        # 仅模拟关闭资源，不访问 PDFium。
        pass
pdfium.close_pdfium_document(Child())
pdfium.close_pdfium_child(Child())
assert pdfium._font_provider is None
""")


def test_import_does_not_read_font_resources() -> None:
    """公共 PDF 模块可导入，但字库只在首次实际访问时加载。"""
    _run("""
from unittest.mock import patch
with patch('importlib.resources.files',side_effect=AssertionError('font read during import')):
    from docvortex.document.pdf import PDFDocument,initialize_pdfium_runtime
""")


def test_render_worker_initializes_its_own_font_runtime(tmp_path: Path) -> None:
    """实际重建 spawn 进程池，验证每代 worker 都有自己的固定字体状态。"""
    script = tmp_path / "worker.py"
    script.write_text(
        '''
import json,os
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context
from docvortex.document.pdf import initialize_pdfium_runtime
from docvortex.document.pdf.images import _initialize_pdf_render_worker

def identity():
    """返回当前 worker 的运行时身份。"""
    return (os.getpid(), initialize_pdfium_runtime().font_sha256)

if __name__=='__main__':
    values=[]
    for _ in range(2):
        with ProcessPoolExecutor(max_workers=1,mp_context=get_context('spawn'),initializer=_initialize_pdf_render_worker) as pool:
            values.append(pool.submit(identity).result(timeout=30))
    assert values[0][0]!=values[1][0]
    assert values[0][1]==values[1][1]
    print(json.dumps(values))
''',
        encoding="utf-8",
    )
    completed = subprocess.run([sys.executable, "-I", str(script)], capture_output=True, text=True, timeout=90)
    assert completed.returncode == 0, completed.stderr
    assert len(json.loads(completed.stdout)) == 2
