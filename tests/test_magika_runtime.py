"""Verify ONNX thread limit and lazy loading bounds for two real recognition entries."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


def test_detection_and_language_use_bounded_cpu_sessions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both real file detection and language recognition must use the 4/1 CPU session, and file detection still reuses the same instance."""
    import magika
    import onnxruntime as ort

    from docvortex.document import detection
    from docvortex.foundation.language import guess_code_language

    sessions: list[ort.InferenceSession] = []
    original_session = ort.InferenceSession

    def record_session(
        model_path: str | Path,
        sess_options: ort.SessionOptions | None = None,
        providers: list[str] | None = None,
    ) -> ort.InferenceSession:
        """Execute real model construction and collect sessions, and verify the actual operating parameters passed in by the two business portals."""
        session = original_session(model_path, sess_options=sess_options, providers=providers)
        sessions.append(session)
        return session

    monkeypatch.setattr(ort, "InferenceSession", record_session)
    source = tmp_path / "sample.html"
    source.write_text("<!doctype html><html><head><title>Example</title></head><body><p>Hello</p></body></html>")
    detection._magika.cache_clear()
    try:
        assert detection.guess_suffix_by_path(source) == "html"
        assert detection.guess_suffix_by_bytes(source.read_bytes(), str(source)) == "html"
        assert guess_code_language('{"name": "example", "values": [1, 2, 3], "enabled": true}') == "json"
        assert detection._magika().get_module_version() == magika.__version__
        assert len(sessions) == 2
        for session in sessions:
            options = session.get_session_options()
            assert (options.intra_op_num_threads, options.inter_op_num_threads) == (4, 1)
            assert session.get_providers() == ["CPUExecutionProvider"]
    finally:
        detection._magika.cache_clear()


def test_detection_and_language_imports_keep_magika_lazy() -> None:
    """When importing only detection and language tools, Magika or ONNX Runtime should not be loaded."""
    code = """
import sys
import docvortex.document.detection
import docvortex.foundation.language
assert 'magika' not in sys.modules
assert 'onnxruntime' not in sys.modules
"""
    result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
