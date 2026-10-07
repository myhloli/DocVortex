"""验证两个真实识别入口的 ONNX 线程限制与惰性加载边界。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


def test_detection_and_language_use_bounded_cpu_sessions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """真实文件检测及语言识别均须使用 4/1 CPU 会话，文件检测仍复用同一实例。"""
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
        """执行真实模型构造并采集会话，核验两个业务入口实际传入的运行参数。"""
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
    """仅导入检测与语言工具时，不应加载 Magika 或 ONNX Runtime。"""
    code = """
import sys
import docvortex.document.detection
import docvortex.foundation.language
assert 'magika' not in sys.modules
assert 'onnxruntime' not in sys.modules
"""
    result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_language_scope_reuses_only_model_and_recomputes_every_input(monkeypatch):
    """同一转换只构造一个真实模型，但不同文字及重复文字均执行原识别方法。"""
    from docvortex.foundation import language, magika as runtime

    samples = ['{"name": "first", "value": 1}', 'print("different input")', '{"name": "first", "value": 1}']
    expected = [language.guess_code_language(value) for value in samples]
    with language._code_language_model_scope():
        first = language._code_language_classifier()
        assert language._code_language_classifier() is first
        actual = [language.guess_code_language(value) for value in samples]
        with language._code_language_model_scope():
            assert language._code_language_classifier() is not first
        assert language._code_language_classifier() is first
    assert actual == expected
    assert language._CODE_LANGUAGE_MODEL_SCOPE.get() is None
    assert isinstance(first, runtime.Magika)


def test_language_scope_restores_context_after_failure():
    """异常退出和嵌套调用均不能把模型状态带入下一份文档。"""
    from docvortex.foundation import language

    with pytest.raises(RuntimeError):
        with language._code_language_model_scope():
            assert language._CODE_LANGUAGE_MODEL_SCOPE.get() == {}
            raise RuntimeError("abort current conversion")
    assert language._CODE_LANGUAGE_MODEL_SCOPE.get() is None


def test_language_scope_custom_factory_keeps_per_call_construction(monkeypatch):
    """替换为函数工厂时保留每次新实例及输入调用，不能假定工厂具有类的方法。"""
    from types import SimpleNamespace
    from docvortex.foundation import language, magika as runtime

    calls = []

    def factory():
        """用构造序号模拟定制识别器状态，验证复用必须完整回退。"""
        calls.append("construct")
        number = len(calls)

        def identify(data):
            """每次记录原输入并返回该实例独立的结果。"""
            calls.append(data)
            return SimpleNamespace(prediction=SimpleNamespace(output=SimpleNamespace(label=f"kind{number}")))

        return SimpleNamespace(identify_bytes=identify)

    monkeypatch.setattr(runtime, "Magika", factory)
    with language._code_language_model_scope():
        assert language.guess_code_language("first") == "kind1"
        assert language.guess_code_language("second") == "kind3"
    assert calls == ["construct", b"first", "construct", b"second"]
