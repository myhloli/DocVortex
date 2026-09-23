"""为文件类型和代码语言识别提供限制线程数的 Magika。"""

import time

import onnxruntime as ort
from magika import Magika as BaseMagika
from magika import __version__ as _MAGIKA_VERSION


class Magika(BaseMagika):
    """仅将 Magika 的 CPU 会话固定为 4/1，保留模型与识别行为。"""

    def _init_onnx_session(self) -> ort.InferenceSession:
        """Magika 尚无公开会话参数入口，在此集中限制算子内和算子间线程数。"""
        started = time.perf_counter()
        ort.disable_telemetry_events()
        options = ort.SessionOptions()
        options.intra_op_num_threads = 4
        options.inter_op_num_threads = 1
        session = ort.InferenceSession(self._model_path, sess_options=options, providers=["CPUExecutionProvider"])
        elapsed_ms = 1000 * (time.perf_counter() - started)
        self._log.debug(f'ONNX DL model "{self._model_path}" loaded in {elapsed_ms:.03f} ms')
        return session

    def get_module_version(self) -> str:
        """保持上游版本标识，避免基类根据子类模块名误报 DocVortex 版本。"""
        return _MAGIKA_VERSION


__all__ = ["Magika"]
